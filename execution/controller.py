"""Strategy signals -> immutable account plan -> serialized execution."""
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import datetime as dt
from zoneinfo import ZoneInfo
from .ledger import dec, text, SafetyStop, Ledger, FirestoreStore
from .broker import AlpacaBroker, Executor

VERSION='shared-etf-2026-09-v1'
ZERO=Decimal(0)


def position_value(state,key,prices):
    p=state['portfolios'][key]
    by={s:{'shares':float(dec(q)),'value':float(dec(q)*dec(prices[s]))}
        for s,q in p['positions'].items() if dec(q)!=0}
    gross=sum(dec(q)*dec(prices[s]) for s,q in p['positions'].items() if dec(q)!=0)+dec(p['cash'])
    return {'total_value':float(gross),'net_value':float(gross-dec(p['debt'])),
            'cash':float(dec(p['cash'])),'debt':float(dec(p['debt'])),'by_symbol':by}


def target_orders(state,targets,prices,assets,funding=None,transfers=None):
    """Worst-case limit proceeds bound all buys. No margin is implicitly added."""
    cash={k:dec(p['cash'])+dec((funding or {}).get(k,0)) for k,p in state['portfolios'].items()}
    sells,buys=[],[]
    for key,tgt in targets.items():
        p=state['portfolios'][key]
        symbols=set(tgt)|{s for s,q in p['positions'].items() if dec(q)>0}
        for sym in sorted(symbols):
            have=dec(p['positions'].get(sym,0)); price=dec(prices[sym]); want=dec(tgt.get(sym,0))
            if price<=0 or want<0: raise SafetyStop('Invalid price or target')
            asset=assets[sym]
            if not asset.get('tradable') or asset.get('status')!='active': raise SafetyStop(f'{sym} is not tradable')
            quantum=Decimal('0.000001') if asset.get('fractionable') else Decimal(1)
            delta=want-have*price
            if delta < -5 or (want==0 and have>0):
                # Full liquidation preserves the exact broker-supported fraction.
                qty=have if want==0 else min(have,(-delta/price).quantize(quantum,rounding=ROUND_DOWN))
                limit=(price*Decimal('0.995')).quantize(Decimal('.01'),rounding=ROUND_DOWN)
                if qty>0:
                    row={'strategy':key,'symbol':sym,'side':'sell','qty':text(qty),'limit_price':text(limit)}
                    sells.append(row);cash[key]+=qty*limit
            elif delta>5:
                buys.append((key,sym,delta,quantum))
    for key,amount in (transfers or {}).items():
        cash[key]+=dec(amount)
        if cash[key]<0: raise SafetyStop(f'{key}: insufficient confirmed-proceeds floor for annual transfer')
    orders=list(sells)
    for key,sym,delta,quantum in buys:
        limit=(dec(prices[sym])*Decimal('1.005')).quantize(Decimal('.01'),rounding=ROUND_UP)
        qty=(min(delta,cash[key])/limit).quantize(quantum,rounding=ROUND_DOWN)
        if qty<=0 or qty*limit<1: continue
        orders.append({'strategy':key,'symbol':sym,'side':'buy','qty':text(qty),'limit_price':text(limit)})
        cash[key]-=qty*limit
    return orders


class Controller:
    def __init__(self,bot,api,env,store=None,broker=None):
        self.bot=bot;self.api=api;self.env=env
        self.broker=broker or AlpacaBroker(api)
        account=self.broker.account()
        if account.get('trading_blocked') or account.get('account_blocked'):
            raise SafetyStop('Broker account trading blocked')
        self.store=store or FirestoreStore(bot.get_firestore_client(),account['id'],env)
        self.ledger=Ledger(self.store);self.executor=Executor(self.ledger,self.broker)

    def prices(self,state,extra=None,trading=False):
        if extra is None:
            # NAV is marked at the broker's position valuation, but quantity is
            # always the strategy's own ledger quantity. Order prices below
            # require fresh executable bid/ask quotes instead of old trades.
            marks={p['symbol']:dec(p['current_price']) for p in self.broker.positions()}
            symbols={s for k,p in state['portfolios'].items() if k in self.bot.SLEEVES for s,q in p['positions'].items() if dec(q)>0}
            if any(s not in marks or marks[s]<=0 for s in symbols):
                raise SafetyStop('Broker valuation missing for owned position')
            return {s:marks[s] for s in symbols}
        return {s:dec(self.bot.get_execution_price(self.api,s,self.env,require_fresh=trading)) for s in sorted(set(extra))}

    def values(self):
        s=self.store.read(); prices=self.prices(s)
        return {k:position_value(s,k,prices) for k in self.bot.SLEEVES}

    def owned(self,cfg):
        vd=self.values()[cfg['strategy_key']]
        for sym in self.bot.STRATEGY_SYMBOLS[cfg['strategy_key']]:
            vd['by_symbol'].setdefault(sym,{'shares':0.,'value':0.})
        return vd

    def build(self,state,action,period,annual=False):
        bot=self.bot
        if state['last_completed'].get(action,'')>=period: return None
        configs={c['strategy_key']:c for c in [bot.aaa_config,bot.mix8_config]+bot.TREND_SLEEVES}
        monthly=action in ('monthly','monthly-funding')
        keys=list(configs) if monthly else (['mix8'] if action=='mix8-upgrade' else [c['strategy_key'] for c in bot.TREND_SLEEVES])
        prices=self.prices(state)
        values={k:position_value(state,k,prices) for k in bot.SLEEVES}
        funding={}; funding_debt={}; transfers={};raw={};margin=ZERO; gate=None
        if monthly:
            gate=bot.check_margin_conditions(self.api,env=self.env)
            if gate.get('errors') and action=='monthly-funding':
                raise SafetyStop('Margin inputs unavailable: '+'; '.join(gate['errors']))
            # Account cash includes sleeve residuals; only reserve is new money.
            account=self.broker.account()
            available=max(ZERO,min(dec(account['cash']),dec(state['portfolios']['reserve']['cash'])))
            # Own sleeve cash is already earmarked; investing it will consume
            # broker cash. It must not also create additional margin capacity.
            outstanding_debt=sum(dec(p['debt']) for p in state['portfolios'].values())
            margin=max(ZERO,dec(account['equity'])*dec(gate.get('target_margin',0))-outstanding_debt)
            if gate.get('errors'): available=margin=ZERO
            amount=available+margin
            # Reuse the existing tilt math against virtual holdings.
            allocation=bot.calculate_rebalanced_allocations(self.api)['adjusted_allocations']
            remaining=amount
            for i,k in enumerate(bot.SLEEVES):
                v=(amount*dec(allocation[bot.SLEEVES[k][0]])).quantize(Decimal('.000001'),rounding=ROUND_DOWN) if i<len(bot.SLEEVES)-1 else remaining
                funding[k]=text(v);remaining-=v
            debt_rest=margin
            for i,k in enumerate(bot.SLEEVES):
                debt=(margin*dec(funding[k])/amount).quantize(Decimal('.000001'),rounding=ROUND_DOWN) if amount and i<len(bot.SLEEVES)-1 else debt_rest
                funding_debt[k]=text(debt);debt_rest-=debt
            if annual and action=='monthly':
                # Transfer net equity; debt remains explicitly assigned.
                total=sum(dec(values[k]['net_value']) for k in bot.SLEEVES)+available
                raw={k:dec(bot.strategy_allocations[bot.SLEEVES[k][0]])*total-dec(values[k]['net_value'])-(dec(funding[k])-dec(funding_debt[k])) for k in bot.SLEEVES}
                # Conservatively cap the rebalance by limit-sale proceeds (1%).
                donors={k:-v*Decimal('.99') for k,v in raw.items() if v<0}
                need=sum(v for v in raw.values() if v>0); pool=sum(donors.values())
                transfers={k:text(-v) for k,v in donors.items()}
                recipients=[k for k,v in raw.items() if v>0];rest=pool
                for i,k in enumerate(recipients):
                    v=pool*raw[k]/need if i<len(recipients)-1 else rest
                    transfers[k]=text(v);rest-=v
        targets={};metadata={};details={}
        for k in keys:
            cfg=configs[k];p=state['portfolios'][k];vd=values[k]
            gross=dec(vd['total_value']);net=dec(vd['net_value']);add=dec(funding.get(k,0))+dec(transfers.get(k,0))
            meta=deepcopy(p.get('metadata',{}))
            target_add=dec(funding.get(k,0))+min(dec(transfers.get(k,0)),raw.get(k,ZERO)) if raw.get(k,ZERO)<0 else add
            nav_add=add-dec(funding_debt.get(k,0))
            if k in ('aaa','mix8'):
                oldpeak=dec(meta.get('peak_nav',net));peak=max(oldpeak,net)
                dd=net/peak-1 if peak>0 else ZERO
                stopped=dd < -dec(cfg['dd_threshold'])
                if action=='monthly-funding':
                    detail=meta['last_momentum_check']
                    weights=({cfg['defensive']:1.} if detail.get('dd_triggered') else
                             {**detail['weights'],cfg['defensive']:detail.get('cash_weight',1-sum(detail['weights'].values()))})
                elif stopped:
                    weights={cfg['defensive']:1.};peak=net; detail={'dd_triggered':True,'drawdown':float(dd)}
                else:
                    detail=bot.plan_rotator_weights(self.api,cfg)
                    weights={**detail['weights'],cfg['defensive']:detail['cash_weight']}
                    detail.update(dd_triggered=False,drawdown=float(dd))
                if action=='monthly-funding':
                    current={s:dec(q)*prices[s] for s,q in p['positions'].items() if dec(q)>0}
                    targets[k]={s:text(current.get(s,ZERO)+dec(weights.get(s,0))*(add+dec(p['cash']))) for s in set(current)|set(weights)}
                else:
                    targets[k]={s:text(dec(w)*max(ZERO,gross+target_add)) for s,w in weights.items()}
                meta['peak_nav']=text(peak*(net+nav_add)/net if net>0 else max(ZERO,net+nav_add))
                meta['last_momentum_check']=detail
            else:
                signals=bot.trend_signals(cfg); weights=bot.world_trend_target_weights(cfg,signals)
                states={sym:bool(signals[sym]['on']) for _,sym in cfg['legs']}
                old=meta.get('leg_states')
                mismatch=any((weights[s]>0 and vd['by_symbol'].get(s,{}).get('value',0)/max(float(gross),1)<.05) or
                             (weights[s]==0 and vd['by_symbol'].get(s,{}).get('value',0)/max(float(gross),1)>.05) for _,s in cfg['legs'])
                if action=='daily' and (old!=states or mismatch):
                    targets[k]={s:text(dec(w)*gross) for s,w in weights.items()}
                elif monthly:
                    current={s:dec(q)*prices[s] for s,q in p['positions'].items() if dec(q)>0}
                    if target_add<0:
                        factor=max(ZERO,(gross+target_add)/gross) if gross>0 else ZERO
                        targets[k]={s:text(v*factor) for s,v in current.items()}
                    else:
                        targets[k]={s:text(current.get(s,ZERO)+dec(weights.get(s,0))*(add+dec(p['cash']))) for s in set(current)|set(weights)}
                meta.update(leg_states=states,last_signal=signals)
            meta['last_signal_check_date']=period;metadata[k]=meta
        symbols={s for tgt in targets.values() for s,v in tgt.items() if dec(v)>0}
        symbols|={s for k in targets for s,q in state['portfolios'][k]['positions'].items() if dec(q)>0}
        prices.update(self.prices(state,symbols,trading=True))
        assets={s:self.broker.asset(s) for s in symbols}
        # Zero target entries with no holdings do not require an asset/price.
        targets={k:{s:v for s,v in tgt.items() if s in symbols} for k,tgt in targets.items()}
        orders=target_orders(state,targets,prices,assets,funding,transfers)
        for row in orders:
            metadata[row['strategy']]['last_trade_date']=dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
        return {'action':action,'period':period,'version':VERSION,'orders':orders,'funding':funding,
                'margin_budget':text(margin),'funding_debt':funding_debt,'transfers':transfers,'metadata':metadata,
                'margin_retry':bool((gate or {}).get('errors')),
                'targets':targets,'prices':{s:text(prices[s]) for s in symbols},'margin_gate':gate,
                'planned_at':dt.datetime.now(dt.timezone.utc).isoformat()}

    def execute(self,action,force=False,annual=None,period=None):
        self.bot.validate_force_environment(self.env,force)
        if action=='reconcile': return self.reconcile()
        now=dt.datetime.now(ZoneInfo('America/New_York'))
        period=period or now.strftime('%Y-%m' if action in ('monthly','monthly-funding') else '%Y-%m-%d')
        state=self.store.read()
        retry=state.get('monthly_retry') or {}
        if action=='monthly' and retry.get('period')==period and now.date().isoformat()>=retry.get('after',''):
            action='monthly-funding'
        if not state.get('active'):
            if action in ('monthly','monthly-funding') and not force and (now.day>7 or not self.bot.check_trading_day(mode='daily')):
                return {'status':'outside_monthly_window'}
            if action=='daily' and not force and not self.bot.check_trading_day(mode='daily'):
                return {'status':'market_closed_day'}
            # Never persist a stale trading plan overnight. Recovery of an
            # existing active plan is separate and remains allowed.
            if not self.broker.clock()['is_open']:
                return {'status':'awaiting_market'}
        annual=(now.month==self.bot.rebalance_config['annual_rebalance_month']) if annual is None else annual
        result=self.executor.run(builder=lambda s,b:self.build(s,action,period,annual))
        self.publish()
        if result['status']=='complete':
            run=result['run']
            if run['action'] in ('monthly','monthly-funding'):
                self.bot.mark_monthly_run_complete(self.env,clean=not run.get('margin_retry'))
            fills=[f"{r['strategy']}: {r['side']} {r['booked_qty']} {r['symbol']} (${float(dec(r['booked_value'])):.2f})" for r in run['orders']]
            self.bot.send_telegram_message('✅ Shared ETF execution ('+self.env+'): '+run['action']+' '+run['period']+'\n'+'\n'.join(fills or ['Keine Trades nötig.'])+'\nBroker und Strategiebestände abgeglichen.')
        return result

    def reconcile(self):
        state=self.store.read()
        if state.get('active'):
            result=self.executor.run()
        else:
            result=self.executor.run()
            state=self.store.read()
            for action,period in state.get('pending_actions',{}).items():
                if action!='mix8-upgrade': raise SafetyStop('Unknown pending action')
                result=self.execute(action,period=period)
        self.publish()
        return result

    def publish(self):
        s=self.store.read();vals=self.values();db=self.bot.get_firestore_client()
        batch=db.batch()
        for key in self.bot.SLEEVES:
            p=s['portfolios'][key];v=vals[key]
            data={**p['metadata'],'current_positions':{sym:float(dec(q)) for sym,q in p['positions'].items()},
                  'current_values':{sym:row['value'] for sym,row in v['by_symbol'].items()},
                  'virtual_cash':float(dec(p['cash'])),'margin_debt':float(dec(p['debt'])),
                  'net_nav':v['net_value'],'virtual_cost_basis':float(sum(dec(b) for b in p['basis'].values())),
                  'net_contributions_since_migration':float(dec(p['net_contributions'])),
                  'execution_revision':s['revision'],'ownership_source':'execution-ledger'}
            batch.set(db.collection(f'strategy-balances-{self.env}').document(key),data,merge=True)
        batch.commit()


def get_controller(bot,api,env=None):
    env=env or api.get('ENV') or ('paper' if 'paper' in api['BASE_URL'] else 'live')
    return Controller(bot,api,env)
