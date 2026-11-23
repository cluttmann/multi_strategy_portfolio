from decimal import Decimal
import pytest
import main
from execution.ledger import new_state, SafetyStop
from execution.controller import position_value, target_orders


def test_shared_position_value_uses_own_shares():
    s=new_state('x','paper',{'aaa':{'EET':'5'},'mix8':{'EET':'8'}},{}, {}, '2026-09-25T00:00:00Z')
    assert position_value(s,'mix8',{'EET':Decimal('100')})['total_value']==800
    assert position_value(s,'aaa',{'EET':Decimal('100')})['total_value']==500


def test_shared_exit_sells_only_own_position_and_buys_with_worst_proceeds():
    s=new_state('x','paper',{'world_trend':{'UGLD':'10'},'mix8':{'UGLD':'20'}},{}, {}, '2026-09-25T00:00:00Z')
    assets={t:{'tradable':True,'fractionable':True,'status':'active'} for t in ['UGLD','USFR']}
    rows=target_orders(s,{'world_trend':{'USFR':1000}}, {'UGLD':Decimal('100'),'USFR':Decimal('50')},assets)
    assert rows[0]['strategy']=='world_trend' and rows[0]['symbol']=='UGLD' and rows[0]['qty']=='10'
    sells=sum(Decimal(r['qty'])*Decimal(r['limit_price']) for r in rows if r['side']=='sell')
    buys=sum(Decimal(r['qty'])*Decimal(r['limit_price']) for r in rows if r['side']=='buy')
    assert buys<=sells


def test_missing_selected_product_volatility_stops_trades(monkeypatch):
    monkeypatch.setattr(main,'_rotator_momentum',lambda *a,**kw:0.1)
    monkeypatch.setattr(main,'_rotator_realized_vol',lambda *a,**kw:None)
    with pytest.raises(main.RotatorDataError): main.plan_rotator_weights({},main.mix8_config)


def test_small_old_etf_balance_is_fully_liquidated_on_replacement():
    s=new_state('x','paper',{'mix8':{'EEM':'0.000018'}},{}, {}, '2026-09-25T00:00:00Z')
    rows=target_orders(s,{'mix8':{'EEM':0}}, {'EEM':Decimal('60')},{'EEM':{'tradable':True,'fractionable':True,'status':'active'}})
    assert rows[0]['qty']=='0.000018'

from execution.controller import Controller
from test_execution import MemoryStore, FakeBroker

class Bot:
    SLEEVES=main.SLEEVES
    strategy_allocations=main.strategy_allocations
    aaa_config=main.aaa_config
    mix8_config=main.mix8_config
    TREND_SLEEVES=main.TREND_SLEEVES
    rebalance_config=main.rebalance_config
    validate_force_environment=staticmethod(main.validate_force_environment)
    world_trend_target_weights=staticmethod(main.world_trend_target_weights)
    def get_execution_price(self,api,s,env,require_fresh=False): return 100
    def check_trading_day(self,mode): return True
    def check_margin_conditions(self,api,env): return {'errors':[], 'target_margin':0,'allowed':False}
    def calculate_rebalanced_allocations(self,api): return {'adjusted_allocations':self.strategy_allocations}
    def plan_rotator_weights(self,api,cfg):
        return {'weights':{cfg['candidates'][2][1]:1},'cash_weight':0,'picks':[cfg['candidates'][2][1]],'realized_vols':{'x':.2}}
    def trend_signals(self,cfg): return {s:{'on':True} for _,s in cfg['legs']}

class PlanningBroker(FakeBroker):
    def positions(self): return [dict(symbol=s,qty='100',current_price='100') for s in ['EET','UGLD','SPXL']]
    def asset(self,s): return {'tradable':True,'fractionable':True,'status':'active'}
    def account(self): return {'id':'account','cash':'100','equity':'10100'}


def controller_fixture():
    b=Bot();s=new_state('account','paper',{'aaa':{'EET':'30'},'mix8':{'EET':'40'},'world_trend':{'UGLD':'20'},'spx_trend':{'SPXL':'10'}},
                       {'reserve':'100'}, {}, '2026-09-25T19:00:00Z')
    store=MemoryStore(s);broker=PlanningBroker();c=Controller(b,{},'paper',store,broker)
    return c,store,broker


def test_monthly_plan_keeps_shared_ownership_and_funding_sums_once():
    c,store,broker=controller_fixture()
    plan=c.build(store.read(),'monthly','2026-10')
    assert sum(Decimal(v) for v in plan['funding'].values())==100
    for row in plan['orders']:
        if row['side']=='sell':
            assert Decimal(row['qty'])<=Decimal(store.read()['portfolios'][row['strategy']]['positions'].get(row['symbol'],0))
    t=c.ledger.acquire();c.ledger.start(t,plan);c.ledger.start(t,plan)
    assert store.read()['portfolios']['mix8']['cash']=='42.500000'


def test_monthly_cache_miss_uses_real_market_refresh_contract(monkeypatch):
    """Run real cache helpers; broad mock signatures cannot hide adapter drift."""
    from types import SimpleNamespace
    c,store,broker=controller_fixture();cached={};downloads=[]
    class Document:
        def __init__(self,key):self.key=key
        def get(self):
            return SimpleNamespace(exists=self.key in cached,to_dict=lambda:dict(cached[self.key]))
        def set(self,data):cached[self.key]=dict(data)
    class Collection:
        def __init__(self,name):self.name=name
        def document(self,symbol):return Document((self.name,symbol))
    class Database:
        def collection(self,name):return Collection(name)
    def environment(env='live'):return {'ENV':env}
    def historical(api,symbol,days=365,raw=False):
        assert api['ENV']=='paper' and days==500 and not raw
        downloads.append(symbol)
        return [100.]*300
    def latest(api,symbol):
        assert api['ENV']=='paper'
        return 123.45
    def weights(api,cfg):
        return {'weights':{'UBT':1},'cash_weight':0,'picks':['UBT'],'realized_vols':{'UBT':.2}}
    monkeypatch.setattr(main,'get_firestore_client',lambda:Database())
    monkeypatch.setattr(main,'set_alpaca_environment',environment)
    monkeypatch.setattr(main,'get_alpaca_historical_bars',historical)
    monkeypatch.setattr(main,'get_latest_trade',latest)
    c.bot.get_all_market_data=main.get_all_market_data
    c.bot.update_market_data=main.update_market_data
    c.bot.plan_rotator_weights=weights
    plan=c.build(store.read(),'monthly','2026-10')
    assert set(downloads)=={'UBT','WLDU'}
    assert plan['prices']['UBT']=='123.45' and plan['prices']['WLDU']=='123.45'
    assert set(cached)=={('market-data-paper','UBT'),('market-data-paper','WLDU')}
    assert all(data['price']==123.45 for data in cached.values())
    assert any(row['symbol']=='UBT' and row['side']=='buy' for row in plan['orders'])
    # Planning neither starts orders nor repeats refreshes while the cache is fresh.
    assert store.read()['active'] is None and broker.submits==0
    assert store.read()['portfolios']['reserve']['cash']=='100'
    c.build(store.read(),'monthly','2026-10')
    assert len(downloads)==2


def test_daily_gold_exit_cannot_touch_mix8_gold():
    c,store,broker=controller_fixture()
    store.state['portfolios']['mix8']['positions']={'UGLD':'40'}
    c.bot.trend_signals=lambda cfg:{s:{'on':False} for _,s in cfg['legs']}
    p=c.build(store.read(),'daily','2026-09-25')
    sells=[r for r in p['orders'] if r['symbol']=='UGLD' and r['side']=='sell']
    assert len(sells)==1 and sells[0]['qty']=='20' and sells[0]['strategy']=='world_trend'


def test_daily_exit_plans_through_real_quote_path_with_quiet_defensive_etf(monkeypatch):
    """Live 2026-11-18 to 11-20: a confirmed WLDU exit never became an order.

    At 15:52 ET USFR's newest IEX quote was 137 s old (BIL 34 s on 11-20):
    an unchanged T-bill ETF market, not a broken one. The daily plan runs the
    real main.get_execution_price path here, not a fixed-price stub.
    """
    import datetime as dt
    import requests
    quotes={'WLDU':('15.06','15.19',1),'USFR':('50.51','50.52',137)}
    def latest(url,**kwargs):
        symbol=url.split('/')[-3];bid,ask,age=quotes[symbol]
        stamp=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=age)).isoformat()
        class Response:
            def raise_for_status(self):pass
            def json(self):return {'quote':{'bp':bid,'ap':ask,'bs':300,'as':300,'t':stamp}}
        return Response()
    class Database:
        def collection(self,name):return self
        def document(self,symbol):return self
        def set(self,data,merge=False):pass
    monkeypatch.setattr(requests,'get',latest)
    monkeypatch.setattr(main,'get_all_market_data',lambda symbol,env='live':{})
    monkeypatch.setattr(main,'get_auth_headers',lambda api:{})
    monkeypatch.setattr(main,'get_firestore_client',lambda:Database())

    class LiveQuoteBot(Bot):
        get_execution_price=staticmethod(main.get_execution_price)
        def trend_signals(self,cfg):return {s:{'on':False} for _,s in cfg['legs']}
    class ExitBroker(PlanningBroker):
        def positions(self):
            return [dict(symbol='WLDU',qty='93',current_price='15.12'),
                    dict(symbol='USFR',qty='33.45243',current_price='50.515')]
        def asset(self,s):return {'tradable':True,'fractionable':s!='WLDU','status':'active'}
    s=new_state('account','live',{'world_trend':{'WLDU':'93','USFR':'33.45243'}},
                {k:'0' for k in list(main.SLEEVES)+['reserve']},{},'2026-11-18T19:00:00Z',
                metadata={'world_trend':{'leg_states':{'WLDU':True,'UGLD':False}},
                          'spx_trend':{'leg_states':{'SPXL':False}}})
    store=MemoryStore(s);c=Controller(LiveQuoteBot(),{},'live',store,ExitBroker())
    p=c.build(store.read(),'daily','2026-11-18')
    sells=[r for r in p['orders'] if r['side']=='sell']
    assert [(r['strategy'],r['symbol'],r['qty']) for r in sells]==[('world_trend','WLDU','93')]
    assert any(r['symbol']=='USFR' and r['side']=='buy' for r in p['orders'])
    assert p['metadata']['world_trend']['leg_states']=={'WLDU':False,'UGLD':False}


def test_annual_rebalance_transfers_conserve_cash_and_buy_within_floor():
    c,store,broker=controller_fixture()
    p=c.build(store.read(),'monthly','2027-01',annual=True)
    assert abs(sum(Decimal(v) for v in p['transfers'].values()))<Decimal('0.000001')
    assert any(Decimal(v)<0 for v in p['transfers'].values())
    assert any(Decimal(v)>0 for v in p['transfers'].values())
    projected={k:Decimal(port['cash'])+Decimal(p['funding'].get(k,0))+Decimal(p['transfers'].get(k,0)) for k,port in store.read()['portfolios'].items()}
    for row in p['orders']:
        amount=Decimal(row['qty'])*Decimal(row['limit_price'])
        projected[row['strategy']]+=amount if row['side']=='sell' else -amount
    assert all(v>=0 for v in projected.values())


def test_closed_market_never_persists_plan_or_funding():
    c,store,broker=controller_fixture();broker.clock=lambda:{'is_open':False}
    result=c.execute('mix8-upgrade')
    assert result['status']=='awaiting_market'
    assert store.read()['active'] is None
    assert store.read()['portfolios']['mix8']['cash']=='0'


def test_new_margin_is_not_counted_as_nav_contribution():
    c,store,broker=controller_fixture()
    store.state['portfolios']['reserve']['cash']='0'
    c.bot.check_margin_conditions=lambda api,env:{'errors':[],'target_margin':.1,'allowed':True}
    broker.account=lambda:{'id':'account','cash':'0','equity':'10000'}
    p=c.build(store.read(),'monthly','2026-10')
    assert sum(Decimal(v) for v in p['funding_debt'].values())==1000
    # Mix8's starting net NAV is 4000. Borrowing buys assets AND adds debt.
    assert Decimal(p['metadata']['mix8']['peak_nav'])==4000


@pytest.mark.parametrize('cash', ['0', '0.0000001', '0.01', '100'])
def test_monthly_funding_preserves_cash_and_debt_with_subcent_residuals(cash):
    c,store,broker=controller_fixture()
    store.state['portfolios']['reserve']['cash']=cash
    store.state['portfolios']['aaa']['debt']='828.1409999999999999999999998'
    c.bot.check_margin_conditions=lambda api,env:{'errors':[],'target_margin':.1,'allowed':True}
    broker.account=lambda:{'id':'account','cash':cash,'equity':'13522.16'}
    c.bot.calculate_rebalanced_allocations=lambda api:{'adjusted_allocations':{
        'aaa_allo':.19994521048217345,'world_trend_allo':.1975760163302812,
        'mix8_allo':.45962555971835617,'spx_trend_allo':.14285321346918928}}
    before=store.read()
    plan=c.build(before,'monthly','2026-11')
    token=c.ledger.acquire()
    c.ledger.start(token,plan)
    after=store.read()
    assert Decimal(after['portfolios']['reserve']['cash'])==(Decimal('0.0000001') if cash=='0.0000001' else 0)
    assert sum(Decimal(v) for v in plan['funding_debt'].values())==Decimal('524.075000')
    for key in c.bot.SLEEVES:
        assert 0<=Decimal(plan['funding_debt'][key])<=Decimal(plan['funding'][key])
    from execution.ledger import totals
    assert abs(totals(after)[1]-totals(before)[1])<Decimal('1e-23')


def test_monthly_cash_plus_margin_rounding_cannot_overallocate_reserve():
    c,store,broker=controller_fixture()
    store.state['portfolios']['reserve']['cash']='8473.91'
    store.state['portfolios']['aaa']['debt']='5749.469999999999999988414691'
    c.bot.check_margin_conditions=lambda api,env:{'errors':[],'target_margin':.1,'allowed':True}
    broker.account=lambda:{'id':'account','cash':'8473.91','equity':'100000'}
    c.bot.calculate_rebalanced_allocations=lambda api:{'adjusted_allocations':dict(zip(
        main.strategy_allocations,[.25726618694171135,.23002558040806693,.36703368068401804,.14567455196620368]))}
    plan=c.build(store.read(),'monthly','2026-11')
    token=c.ledger.acquire();c.ledger.start(token,plan)
    assert Decimal(store.read()['portfolios']['reserve']['cash'])==0
    assert sum(Decimal(v) for v in plan['funding'].values())==Decimal('12724.44')
    assert sum(Decimal(v) for v in plan['funding_debt'].values())==Decimal('4250.53')

from execution.migration import bootstrap_state


def test_bootstrap_preserves_retired_dust_and_exclusive_owners():
    positions=[{'symbol':'EET','qty':'5','cost_basis':'400','market_value':'500'},
               {'symbol':'EEM','qty':'3','cost_basis':'210','market_value':'240'},
               {'symbol':'TQQQ','qty':'0.000006','cost_basis':'0.0004','market_value':'0.0005'}]
    s=bootstrap_state({'id':'a','cash':'-74'},positions,[],{'aaa':['EET'],'mix8':['EEM']},{},'paper','2026-09-25T19:00:00Z')
    assert s['portfolios']['aaa']['positions']=={'EET':'5'}
    assert s['portfolios']['mix8']['positions']=={'EEM':'3'}
    assert s['portfolios']['legacy']['positions']=={'TQQQ':'0.000006'}
    assert sum(Decimal(p['debt']) for p in s['portfolios'].values())==74


def test_bootstrap_refuses_shared_ownership_without_evidence_and_open_orders():
    pos=[{'symbol':'EET','qty':'5','cost_basis':'400','market_value':'500'}]
    with pytest.raises(SafetyStop): bootstrap_state({'id':'a','cash':'0'},pos,[],{'aaa':['EET'],'mix8':['EET']},{},'paper','2026-09-25T19:00:00Z')
    with pytest.raises(SafetyStop): bootstrap_state({'id':'a','cash':'0'},pos,[{'id':'open'}],{'aaa':['EET']},{},'paper','2026-09-25T19:00:00Z')


def test_live_bootstrap_tracks_already_withheld_monthly_fees():
    pos=[{'symbol':'EET','qty':'5','cost_basis':'400','market_value':'500'}]
    account={'id':'a','cash':'-74','accrued_fees':'5.239978819444630009'}
    s=bootstrap_state(account,pos,[],{'aaa':['EET']},{},'live','2026-09-25T19:00:00Z')
    assert s['account_fee_tracker']['accrued_fees']==account['accrued_fees']
    assert s['account_fee_tracker']['pending']=='5.24'
    with pytest.raises(SafetyStop,match='accrued_fees'):
        bootstrap_state({'id':'a','cash':'-74'},pos,[],{'aaa':['EET']},{},'live','2026-09-25T19:00:00Z')


def test_annual_transfers_use_net_equity_with_tilted_margin_funding():
    c,store,broker=controller_fixture()
    c.bot.check_margin_conditions=lambda api,env:{'errors':[],'target_margin':.1,'allowed':True}
    broker.account=lambda:{'id':'account','cash':'100','equity':'10000'}
    c.bot.calculate_rebalanced_allocations=lambda api:{'adjusted_allocations':{'aaa_allo':.10,'world_trend_allo':.10,'mix8_allo':.70,'spx_trend_allo':.10}}
    p=c.build(store.read(),'monthly','2027-01',annual=True)
    # Net equity 10,100; AAA target 2,146.25, current 3,000, net new cash 10.
    # Borrowing 100 is debt, not net new equity. Transfer keeps 1% execution buffer.
    assert Decimal(p['transfers']['aaa'])==Decimal('-855.11250000')


def test_margin_data_failure_still_rotates_without_new_money():
    c,store,broker=controller_fixture()
    c.bot.check_margin_conditions=lambda api,env:{'errors':['FRED unavailable'],'target_margin':0,'allowed':False}
    p=c.build(store.read(),'monthly','2026-10')
    assert p['orders']
    assert all(Decimal(v)==0 for v in p['funding'].values())
    assert p['margin_retry'] is True


def test_funding_retry_only_adds_to_monthly_weights_without_rotating_again():
    c,store,broker=controller_fixture()
    store.state['portfolios']['aaa']['metadata']['last_momentum_check']={'weights':{'EET':1},'cash_weight':0}
    store.state['portfolios']['mix8']['metadata']['last_momentum_check']={'weights':{'EET':1},'cash_weight':0}
    p=c.build(store.read(),'monthly-funding','2026-10')
    assert not any(r['side']=='sell' for r in p['orders'])
    assert sum(Decimal(v) for v in p['funding'].values())==100


def test_earmarked_sleeve_cash_is_not_counted_again_as_new_margin():
    c,store,broker=controller_fixture()
    store.state['portfolios']['reserve']['cash']='0'
    store.state['portfolios']['mix8'].update(cash='100',debt='1000')
    c.bot.check_margin_conditions=lambda api,env:{'errors':[],'target_margin':.1,'allowed':True}
    broker.account=lambda:{'id':'account','cash':'-900','equity':'10000'}
    p=c.build(store.read(),'monthly','2026-10')
    assert Decimal(p['margin_budget'])==0


# --- Risk exits never wait on quote quality (2026-11-23) ----------------------

class _BadQuoteBot(Bot):
    """Every symbol in `bad` fails the strict IEX check (stale or wide spread)."""
    def __init__(self,bad,on=None):self.bad=set(bad);self.on=on or {}
    def get_execution_price(self,api,s,env,require_fresh=False):
        if s in self.bad:raise SafetyStop(f'{s}: quote spread exceeds 0.9%')
        return {'WLDU':Decimal('15'),'USFR':Decimal('50'),'SPXL':Decimal('230'),'BIL':Decimal('91.6')}.get(s,Decimal('100'))
    def trend_signals(self,cfg):return {s:{'on':self.on.get(s,False)} for _,s in cfg['legs']}


class _LiveLikeBroker(PlanningBroker):
    def positions(self):
        return [dict(symbol='WLDU',qty='93',current_price='14.80'),dict(symbol='USFR',qty='33.45243',current_price='50.52'),
                dict(symbol='SPXL',qty='8.201427',current_price='228.70')]
    def asset(self,s):return {'tradable':True,'fractionable':s!='WLDU','status':'active'}


def _live_like_state():
    return new_state('account','live',{'world_trend':{'WLDU':'93','USFR':'33.45243'},'spx_trend':{'SPXL':'8.201427'}},
                     {k:'0' for k in list(main.SLEEVES)+['reserve']},{},'2026-11-23T19:00:00Z',
                     metadata={'world_trend':{'leg_states':{'WLDU':True,'UGLD':False}},'spx_trend':{'leg_states':{'SPXL':True}}})


def _plan(bot):
    from test_execution import MemoryStore
    store=MemoryStore(_live_like_state());c=Controller(bot,{},'live',store,_LiveLikeBroker())
    return c.build(store.read(),'daily','2026-11-23')


def test_bad_quote_on_exiting_leg_still_sells_at_broker_mark_with_wide_limit():
    p=_plan(_BadQuoteBot({'WLDU','SPXL'}))
    sells={r['symbol']:r for r in p['orders'] if r['side']=='sell'}
    assert sells['WLDU']['qty']=='93' and sells['WLDU']['limit_price']==str((Decimal('14.80')*Decimal('0.97')).quantize(Decimal('.01'),rounding='ROUND_DOWN'))
    assert sells['SPXL']['qty']=='8.201427'
    assert set(p['degraded_quotes'])=={'WLDU','SPXL'}


def test_stale_defensive_quote_never_blocks_the_exit_and_buys_nothing():
    p=_plan(_BadQuoteBot({'USFR','BIL'}))
    sells=[(r['symbol'],r['qty']) for r in p['orders'] if r['side']=='sell']
    assert ('WLDU','93') in sells and ('SPXL','8.201427') in sells
    assert not any(r['side']=='buy' for r in p['orders'])
    assert not any(r['symbol']=='USFR' for r in p['orders'])


def test_everything_stale_still_exits():
    p=_plan(_BadQuoteBot({'WLDU','SPXL','USFR','BIL'}))
    assert {r['symbol'] for r in p['orders'] if r['side']=='sell'}=={'WLDU','SPXL'}
    assert not any(r['side']=='buy' for r in p['orders'])


def test_good_quotes_keep_tight_limits_and_buy_defensive():
    p=_plan(_BadQuoteBot(set()))
    wldu=[r for r in p['orders'] if r['symbol']=='WLDU'][0]
    assert wldu['limit_price']==str((Decimal('15')*Decimal('0.995')).quantize(Decimal('.01'),rounding='ROUND_DOWN'))
    assert {r['symbol'] for r in p['orders'] if r['side']=='buy'}=={'USFR','BIL'}
    assert p['degraded_quotes']=={}


def test_bad_quote_on_reentry_buy_does_not_buy_blind():
    from test_execution import MemoryStore
    s=new_state('account','live',{'world_trend':{'USFR':'60'}},{k:'0' for k in list(main.SLEEVES)+['reserve']},{},
                '2026-11-23T19:00:00Z',metadata={'world_trend':{'leg_states':{'WLDU':False,'UGLD':False}}})
    class B(_LiveLikeBroker):
        def positions(self):return [dict(symbol='USFR',qty='60',current_price='50.52')]
    bot=_BadQuoteBot({'WLDU'},on={'WLDU':True})
    store=MemoryStore(s);c=Controller(bot,{},'live',store,B())
    p=c.build(store.read(),'daily','2026-11-23')
    assert not any(r['symbol']=='WLDU' for r in p['orders'])


def test_unexecuted_exit_alert_names_the_open_position(monkeypatch):
    sent=[]
    class Pos:
        def __init__(self,api):pass
        def positions(self):return [{'symbol':'WLDU','market_value':'1376'},{'symbol':'SPXL','market_value':'12'}]
    import execution.broker
    monkeypatch.setattr(execution.broker,'AlpacaBroker',Pos)
    monkeypatch.setattr(main,'trend_signals',lambda cfg:{p:{'on':False,'diff_pct':-4.3} for _,p in cfg['legs']})
    monkeypatch.setattr(main,'send_telegram_message',lambda m:sent.append(m))
    out=main._alert_unexecuted_exits({},'live')
    assert len(out)==1 and 'WLDU' in out[0] and sent and '🚨' in sent[0]


def test_unexecuted_exit_alert_silent_when_signal_on(monkeypatch):
    sent=[]
    class Pos:
        def __init__(self,api):pass
        def positions(self):return [{'symbol':'WLDU','market_value':'1376'}]
    import execution.broker
    monkeypatch.setattr(execution.broker,'AlpacaBroker',Pos)
    monkeypatch.setattr(main,'trend_signals',lambda cfg:{p:{'on':True} for _,p in cfg['legs']})
    monkeypatch.setattr(main,'send_telegram_message',lambda m:sent.append(m))
    assert main._alert_unexecuted_exits({},'live')==[] and sent==[]
