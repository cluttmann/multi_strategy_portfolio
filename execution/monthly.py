"""Durable monthly quote attempts. Every submitted row stays in the fill journal."""
from copy import deepcopy
import datetime as dt
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo
from .ledger import SafetyStop, dec, text, TERMINAL
from .quotes import execution_limit, QuoteDeferred, EET_SPREAD_SOFT
from .timestamps import parse_timestamp

POLICY='monthly-iex-v1'
NY=ZoneInfo('America/New_York')


def remaining(plan,intent):
    rows=[r for r in plan['orders'] if r.get('intent_id')==intent['id']]
    field='booked_value' if intent['side']=='buy' else 'booked_qty'
    cap=dec(intent['budget'] if intent['side']=='buy' else intent['qty'])
    return max(Decimal(0),cap-sum((dec(r.get(field,0)) for r in rows),Decimal(0)))


def reservations(plan,strategy=None):
    return sum((max(Decimal(0),dec(r['qty'])*dec(r['limit_price'])-dec(r.get('booked_value',0)))
                for r in plan['orders'] if r['side']=='buy' and r['status'] not in TERMINAL
                and (strategy is None or r['strategy']==strategy)),Decimal(0))


def mutate(ex,token,kind,fn):
    def update(state):ex.ledger.check(state,token);return fn(state,state['active'])
    return ex.ledger.store.mutate(kind,update)


def defer(ex,token,intent_id,reason,status='pending'):
    def update(state,plan):
        intent=next(i for i in plan['intents'] if i['id']==intent_id)
        intent.update(status=status,reason=reason,next_attempt_at=(ex.now()+dt.timedelta(minutes=5)).isoformat())
    mutate(ex,token,'monthly_deferred',update)


def cancel_open(ex,token,predicate=lambda row:True):
    """No cash reuse until broker confirms terminal and all cumulative fills book."""
    pending=False
    for index,row in enumerate(ex.ledger.store.read()['active']['orders']):
        if row['status'] in TERMINAL or not predicate(row):continue
        order=ex.broker.by_client_id(row['client_order_id'])
        if order is None:raise SafetyStop('Submission ambiguous; no second POST')
        ex.ledger.record(token,index,order)
        if order['status'] not in TERMINAL:
            ex.broker.cancel(order['id'])
            order=ex.broker.by_client_id(row['client_order_id'])
            if order is None:raise SafetyStop('Order missing after cancel request')
            ex.ledger.record(token,index,order)
            if order['status'] not in TERMINAL:pending=True
    return not pending


def expiry_reason(ex,plan):
    now=ex.now().astimezone(NY);created=parse_timestamp(plan['created_at']).astimezone(NY).date()
    if now.day>7 or now.strftime('%Y-%m')!=plan['period']:return 'outside_monthly_day_1_7_window'
    if hasattr(ex.broker,'trading_days'):
        dates=ex.broker.trading_days(created.isoformat(),now.date().isoformat())
        count=len(dates)
    else:
        count=sum((created+dt.timedelta(days=i)).weekday()<5 for i in range((now.date()-created).days+1))
    return 'three_trading_day_expiry' if count>3 else None


def finish(ex,token,status):
    def archive(state,plan):
        plan['execution_status']=status;plan['completed_at']=ex.now().isoformat()
        for key,updates in plan.get('metadata',{}).items():state['portfolios'][key]['metadata'].update(updates)
        if status=='complete':
            state['last_completed'][plan['action']]=plan['period']
            if plan.get('margin_retry'):
                tomorrow=ex.now().astimezone(NY).date()+dt.timedelta(days=1)
                state['monthly_retry']={'period':plan['period'],'after':tomorrow.isoformat()}
            else:state.pop('monthly_retry',None)
        else:state.setdefault('last_expired',{})[plan['action']]=plan['period']
        state['active']=None
        return deepcopy(plan)
    run=mutate(ex,token,'complete',archive)
    return {'status':status,'run':run}


def run_monthly(ex,token):
    data_errors=[]
    try:
        plan=ex.ledger.store.read()['active']
        expiry=expiry_reason(ex,plan)
        if expiry:
            if not cancel_open(ex,token,lambda row:not row.get('risk_exit')):
                return {'status':'pending','reason':'expiry_cancel_unconfirmed','run_id':plan['id']}
            for intent in plan['intents']:
                if not intent.get('risk_exit') and intent.get('status') not in ('complete','invalidated'):
                    defer(ex,token,intent['id'],expiry,'expired')
            # Risk exits retain priority even once new monthly entries expire.
            plan=ex.ledger.store.read()['active']
            if not any(i.get('risk_exit') and i.get('status') not in ('complete','invalidated') for i in plan['intents']):
                return finish(ex,token,'expired')
        # A scheduler invocation never waits on a quiet ETF; its durable attempt
        # age is bounded independently of this process lifetime.
        for row in plan['orders']:
            if row['status'] not in TERMINAL and (ex.now()-parse_timestamp(row['submitted_at'])).total_seconds()>=300:
                cancel_open(ex,token,lambda r,cid=row['client_order_id']:r['client_order_id']==cid)
        now=ex.now().astimezone(NY)
        entry_hours=dt.time(10,30)<=now.time().replace(tzinfo=None)<=dt.time(15,30)
        if not ex.broker.clock()['is_open'] or (not entry_hours and not any(i.get('risk_exit') for i in plan['intents'])):
            return {'status':'pending','reason':'outside_retry_hours','run_id':plan['id']}
        if ex.quote_getter is None:raise SafetyStop('Monthly IEX quote getter unavailable')
        for original in sorted(plan['intents'],key=lambda i:i['side']=='buy'):
            plan=ex.ledger.store.read()['active'];intent=next(i for i in plan['intents'] if i['id']==original['id'])
            if intent.get('status') in ('invalidated','expired','complete'):continue
            if not entry_hours and not intent.get('risk_exit'):continue
            rows=[r for r in plan['orders'] if r.get('intent_id')==intent['id']]
            if intent.get('next_attempt_at') and ex.now()<parse_timestamp(intent['next_attempt_at']) and not intent.get('risk_exit'):continue
            if any(r['status'] not in TERMINAL for r in rows):continue
            left=remaining(plan,intent)
            if left<=0 or (intent['side']=='buy' and left<1):
                defer(ex,token,intent['id'],'remainder_below_minimum','complete');continue
            try:
                quote=ex.quote_getter(intent['symbol'])
                limit=execution_limit(quote,intent['symbol'],intent['side'],len(rows),ex.now(),intent.get('risk_exit',False))
                quantum=Decimal('.000001') if intent.get('fractionable') else Decimal(1)
                state=ex.ledger.store.read();port=state['portfolios'][intent['strategy']]
                if intent['side']=='buy':
                    transfers=plan.get('transfers',{})
                    if not plan.get('transfers_done'):
                        if any(dec(state['portfolios'][k]['cash'])+dec(v)<0 for k,v in transfers.items()):
                            if dec(transfers.get(intent['strategy'],0))>0:
                                defer(ex,token,intent['id'],'awaiting_confirmed_transfer_proceeds');continue
                        else:
                            ex.ledger.transfer(token);state=ex.ledger.store.read();port=state['portfolios'][intent['strategy']]
                    account=ex.broker.account()
                    if account.get('trading_blocked') or account.get('account_blocked'):raise SafetyStop('Broker account trading blocked')
                    permission=ex.margin_validator(state) if ex.margin_validator else False
                    allowed=bool(permission.get('allowed')) and not permission.get('errors') if isinstance(permission,dict) else bool(permission)
                    transfer_reserve=max(Decimal(0),-dec(transfers.get(intent['strategy'],0))) if not state['active'].get('transfers_done') else Decimal(0)
                    sleeve=max(Decimal(0),dec(port['cash'])-reservations(state['active'],intent['strategy'])-transfer_reserve)
                    if not allowed:sleeve=min(sleeve,max(Decimal(0),dec(port['cash'])-dec(port['debt'])))
                    power=max(Decimal(0),dec(account.get('buying_power',0)))
                    if not allowed:power=min(power,max(Decimal(0),dec(account['cash'])-reservations(state['active'])))
                    elif isinstance(permission,dict):
                        # Durable funding is a maximum, never permission to keep
                        # borrowing after today's gate tightens. Count broker
                        # debt already used and every still-open reservation.
                        current_cap=max(Decimal(0),dec(account['equity'])*dec(permission.get('target_margin',0)))
                        used=max(Decimal(0),-dec(account['cash']))
                        fresh_capacity=max(Decimal(0),dec(account['cash']))+max(Decimal(0),current_cap-used)
                        power=min(power,max(Decimal(0),fresh_capacity-reservations(state['active'])))
                    qty=(min(left,sleeve,power)/limit).quantize(quantum,rounding=ROUND_DOWN)
                    if qty<=0 or qty*limit<1:
                        # A nonfractional residual too small for one share is a
                        # successful bounded outcome; cash remains sleeve-owned.
                        if left<limit and not intent.get('fractionable'):
                            defer(ex,token,intent['id'],'whole_share_cash_residual','complete')
                        else:defer(ex,token,intent['id'],'awaiting_cash_or_margin_permission')
                        continue
                else:
                    have=dec(port['positions'].get(intent['symbol'],0))
                    qty=min(left,have)
                    if not intent.get('full_liquidation'):qty=qty.quantize(quantum,rounding=ROUND_DOWN)
                    if qty<=0:defer(ex,token,intent['id'],'remainder_below_minimum','complete');continue
                def append(state,active):
                    index=len(active['orders'])
                    row={'strategy':intent['strategy'],'symbol':intent['symbol'],'side':intent['side'],
                         'qty':text(qty),'limit_price':text(limit),'intent_id':intent['id'],
                         'client_order_id':f"se-{active['id']}-{index:03d}",'status':'planned','booked_qty':'0','booked_value':'0',
                         'quote':deepcopy(quote),'attempt_number':len(rows),'risk_exit':bool(intent.get('risk_exit')),
                         'quote_band':'above_soft' if intent['symbol']=='EET' and (dec(quote['ap'])-dec(quote['bp']))/((dec(quote['ap'])+dec(quote['bp']))/2)>EET_SPREAD_SOFT else 'within_soft'}
                    active['orders'].append(row)
                    return index
                index=mutate(ex,token,'monthly_attempt',append)
                row=ex.ledger.mark_submitting(token,index,submitted_at=ex.now().isoformat())
                # An exception cannot lead to a second POST: submitting remains
                # durable and recovery resolves this exact client order id.
                order=ex.broker.submit(row);ex.ledger.record(token,index,order)
                defer(ex,token,intent['id'],'order_submitted')
            except QuoteDeferred as exc:defer(ex,token,intent['id'],str(exc))
            except Exception as exc:
                defer(ex,token,intent['id'],str(exc),'data_error');data_errors.append(f"{intent['symbol']}: {exc}")
        ex.recover(token)
        plan=ex.ledger.store.read()['active']
        for intent in plan['intents']:
            if intent.get('status')=='invalidated':continue
            if not any(r['status'] not in TERMINAL for r in plan['orders'] if r.get('intent_id')==intent['id']):
                left=remaining(plan,intent)
                if left<=0 or (intent['side']=='buy' and left<1):defer(ex,token,intent['id'],'filled','complete')
        plan=ex.ledger.store.read()['active']
        if all(i.get('status') in ('complete','invalidated','expired') for i in plan['intents']):
            ex.ledger.transfer(token)
            return finish(ex,token,'expired' if any(i.get('status') in ('invalidated','expired') for i in plan['intents']) else 'complete')
        return {'status':'data_error' if data_errors else 'pending','run_id':plan['id'],
                'errors':data_errors,'intents':deepcopy(plan['intents'])}
    except Exception as exc:
        mutate(ex,token,'monthly_data_error',lambda state,active:active.update(last_error=str(exc)))
        return {'status':'data_error','run_id':plan['id'],'errors':[str(exc)]}
