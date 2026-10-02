"""Alpaca adapter and single-account executor. POST is deliberately never retried."""
from copy import deepcopy
from decimal import Decimal
import datetime as dt
import time
import requests
from .ledger import SafetyStop, dec, text, reconcile_state, TERMINAL
from .fees import accrue_regulatory_fees, accrue_account_fees, settle_fee, settle_account_fee
from .timestamps import parse_timestamp

class AlpacaBroker:
    def __init__(self, api):
        self.base=api['BASE_URL'].rstrip('/')
        self.headers={'APCA-API-KEY-ID':api['API_KEY'],'APCA-API-SECRET-KEY':api['SECRET_KEY']}

    def request(self, method, path, **kwargs):
        r=requests.request(method,self.base+path,headers=self.headers,timeout=(5,25),**kwargs)
        if r.status_code==404 and method=='GET' and path=='/v2/orders:by_client_order_id': return None
        r.raise_for_status()
        return r.json() if r.content else None

    def account(self): return self.request('GET','/v2/account')
    def positions(self): return self.request('GET','/v2/positions')
    def clock(self): return self.request('GET','/v2/clock')
    def asset(self,symbol): return self.request('GET',f'/v2/assets/{symbol}')
    def open_orders(self): return self.request('GET','/v2/orders',params={'status':'open','limit':500})
    def by_client_id(self,cid): return self.request('GET','/v2/orders:by_client_order_id',params={'client_order_id':cid})
    def submit(self,row):
        return self.request('POST','/v2/orders',json={k:row[k] for k in ('symbol','qty','side','limit_price','client_order_id')} |
                            {'type':'limit','time_in_force':'day'})
    def cancel(self,order_id): return self.request('DELETE',f'/v2/orders/{order_id}')
    def trading_days(self,start,end):
        return self.request('GET','/v2/calendar',params={'start':start,'end':end})

    def activities(self,after):
        rows=[]; token=None
        while True:
            params={'after':after,'direction':'asc','page_size':100}
            if token: params['page_token']=token
            page=self.request('GET','/v2/account/activities',params=params)
            rows.extend(page)
            if len(page)<100: return rows
            nxt=page[-1]['id']
            if token==nxt: raise SafetyStop('Activity pagination did not advance')
            token=nxt


def sync_activities(ledger,token,activities,marks=None):
    """Account cash events go to an explicit reserve, never guessed sleeve income.

    Splits, corrections and unknown/manual trades stop for evidence-based repair.
    Fills are already booked from cumulative order executions, not booked twice.
    """
    def ingest(s):
        ledger.check(s,token)
        seen=set(s.get('activity_ids',[])); owners=s.get('order_owners',{})
        for a in activities:
            aid=a['id']
            if aid in seen: continue
            typ=a.get('activity_type')
            if typ=='FILL':
                if a.get('order_id') not in owners:
                    raise SafetyStop(f"Unattributed broker fill: {aid}")
            elif typ in {'DIV','INT','FEE','CFEE','CSD','CSW','DIVNRA','DIVFT','DIVTX','DIVCGL','DIVCGS'}:
                # Named account cash events remain in reserve; this is explicit
                # centralized cash attribution, not fabricated record-date shares.
                if typ in {'FEE','CFEE'}:amt=settle_fee(s,a)
                elif typ=='INT':amt=settle_account_fee(s,a)
                else:amt=dec(a['net_amount'])
                r=s['portfolios']['reserve']; net=dec(r['cash'])-dec(r['debt'])+amt
                r['cash']=text(max(Decimal(0),net));r['debt']=text(max(Decimal(0),-net))
                # Positive account cash income first repays attributed debt;
                # only the reserve left afterwards is new allocation money.
                if amt>0 and dec(r['cash'])>0:
                    debtors={k:dec(p['debt']) for k,p in s['portfolios'].items() if dec(p['debt'])>0}
                    total=sum(debtors.values());pay=min(dec(r['cash']),total);left=pay
                    for i,(key,debt) in enumerate(debtors.items()):
                        repay=pay*debt/total if i<len(debtors)-1 else left;left-=repay
                        port=s['portfolios'][key]
                        if typ=='CSD':
                            if not marks or any(sym not in marks for sym,q in port['positions'].items() if dec(q)>0):
                                raise SafetyStop('Deposit allocation needs current NAV marks')
                            nav=sum(dec(q)*dec(marks[sym]) for sym,q in port['positions'].items() if dec(q)>0)+dec(port['cash'])-debt
                            peak=dec(port['metadata'].get('peak_nav',nav))
                            if nav>0:port['metadata']['peak_nav']=text(peak*(nav+repay)/nav)
                            port['net_contributions']=text(dec(port['net_contributions'])+repay)
                        port['debt']=text(debt-repay)
                    r['cash']=text(dec(r['cash'])-pay)

            else:
                raise SafetyStop(f"Unresolved broker activity {typ}: {aid}")
            seen.add(aid)
            s.setdefault('activity_ids',[]).append(aid)
        # Keep an overlap window. Older history is retained in immutable events.
        if activities:
            dated=[a.get('transaction_time') or (a.get('date','')+'T00:00:00Z') for a in activities]
            newest=max(dated)
            if newest>s.get('activity_through',''): s['activity_through']=newest
        s['activity_ids']=s['activity_ids'][-2000:]
    if activities: ledger.store.mutate('activities',ingest)


class Executor:
    def __init__(self,ledger,broker,poll_seconds=2,timeout=90,quote_getter=None,margin_validator=None,now=None):
        self.ledger=ledger;self.broker=broker;self.poll_seconds=poll_seconds;self.timeout=timeout
        self.quote_getter=quote_getter;self.margin_validator=margin_validator
        self.now=now or (lambda:dt.datetime.now(dt.timezone.utc))

    def recover(self,token):
        s=self.ledger.store.read(); active=s.get('active')
        if active:
            for i,row in enumerate(active['orders']):
                if row['status']=='planned' or row.get('never_submitted'): continue
                order=self.broker.by_client_id(row['client_order_id'])
                if order is None:
                    raise SafetyStop(f"Submission ambiguous; no second POST: {row['client_order_id']}")
                self.ledger.record(token,i,order)
        s=self.ledger.store.read()
        active_cids={r['client_order_id'] for r in (s.get('active') or {}).get('orders',[])}
        unknown=[o['id'] for o in self.broker.open_orders() if o['client_order_id'] not in active_cids]
        if unknown: raise SafetyStop(f'Unmanaged open broker orders: {unknown}')
        # Replay with overlap to catch late same-day settlement/cash entries.
        through=parse_timestamp(s['activity_through'])
        # Fee activity dates are midnight UTC, even when the broker clock that
        # seeded the ledger carried a New York offset.
        floor=parse_timestamp(s['started_at']).astimezone(dt.timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
        after=max(floor,through-dt.timedelta(days=7)).isoformat()
        activities=self.broker.activities(after)
        positions=self.broker.positions()
        marks={p['symbol']:p['current_price'] for p in positions if p.get('current_price')}
        sync_activities(self.ledger,token,activities,marks=marks)
        s=self.ledger.store.read(); account=self.broker.account()
        if account['id']!=s['account_id']: raise SafetyStop('Wrong broker account')
        accrue_regulatory_fees(self.ledger,token,activities,account['cash'])
        accrue_account_fees(self.ledger,token,account)
        return reconcile_state(self.ledger.store.read(),self.broker.positions(),account['cash'])

    def run(self,plan=None,builder=None,interrupt_builder=None):
        t=self.ledger.acquire()
        try:
            try:self.recover(t)
            except Exception as exc:
                failed=self.ledger.store.read().get('active')
                if failed and failed.get('execution_policy')=='monthly-iex-v1':
                    def error(s):self.ledger.check(s,t);s['active']['last_error']=str(exc)
                    self.ledger.store.mutate('monthly_recovery_error',error)
                    return {'status':'data_error','run_id':failed['id'],'errors':[str(exc)]}
                raise
            state=self.ledger.store.read()
            if not state['active'] and state.get('suspended_monthly') and plan is None:
                def resume_before_signals(s):
                    self.ledger.check(s,t);s['active']=s.pop('suspended_monthly')
                self.ledger.store.mutate('monthly_resume',resume_before_signals)
                state=self.ledger.store.read()
            if state.get('active',{} ) and state['active'].get('execution_policy')=='monthly-iex-v1' and interrupt_builder:
                daily=interrupt_builder(state,self.broker)
                if daily is not None:
                    from .monthly import cancel_open
                    if not cancel_open(self,t):return {'status':'pending','reason':'daily_risk_cancel_unconfirmed','run_id':state['active']['id']}
                    self.recover(t)
                    # Cancels can carry late fills. Rebuild daily quantities from
                    # those confirmed holdings, never the pre-cancel snapshot.
                    daily=interrupt_builder(self.ledger.store.read(),self.broker)
                    if daily is None:return {'status':'pending','reason':'daily_already_complete','run_id':state['active']['id']}
                    def suspend(s):
                        self.ledger.check(s,t)
                        monthly=s['active']
                        states={(key,sym):bool(on) for key,meta in daily.get('metadata',{}).items() for sym,on in meta.get('leg_states',{}).items()}
                        for intent in monthly['intents']:
                            signal=states.get((intent['strategy'],intent['symbol']))
                            if intent['side']=='buy' and signal is False:
                                intent.update(status='invalidated',reason='daily_risk_off')
                            elif intent['side']=='sell' and intent.get('risk_exit') and signal is True:
                                intent.update(status='invalidated',reason='daily_risk_on')
                        for key,meta in daily.get('metadata',{}).items():monthly['metadata'][key]=deepcopy(meta)
                        s['suspended_monthly']=monthly;s['active']=None
                    self.ledger.store.mutate('monthly_suspend_for_daily',suspend)
                    plan=daily;builder=None
            state=self.ledger.store.read()
            if not state['active']:
                if builder: plan=builder(state,self.broker)
                if plan is None: return {'status':'reconciled'}
                if plan.get('execution_policy')!='monthly-iex-v1' and plan['orders'] and not self.broker.clock()['is_open']:
                    return {'status':'awaiting_market'}
                self.ledger.start(t,plan)
            active=self.ledger.store.read()['active']
            if active is None: return {'status':'already_complete'}
            if active.get('execution_policy')=='monthly-iex-v1':
                from .monthly import run_monthly
                return run_monthly(self,t)
            if not self.broker.clock()['is_open'] and active['orders']:
                return {'status':'awaiting_market','run_id':active['id']}
            for i in range(len(active['orders'])):
                row=self.ledger.store.read()['active']['orders'][i]
                if row['side']=='buy': self.ledger.transfer(t)
                if row['status']=='filled': continue
                if row['status'] in TERMINAL:
                    raise SafetyStop(f"Order {row['client_order_id']} {row['status']}; partial fills booked, plan needs repair")
                if row['status']=='planned':
                    planned_at=active.get('planned_at')
                    if planned_at:
                        age=(dt.datetime.now(dt.timezone.utc)-parse_timestamp(planned_at)).total_seconds()
                        if age>300: raise SafetyStop('Remaining plan is stale; reconcile and refresh before submitting')
                    # Persist claim BEFORE POST. A crash here blocks until explicit
                    # resolution, rather than risking a duplicate order.
                    row=self.ledger.mark_submitting(t,i)
                    order=self.broker.submit(row)
                    self.ledger.record(t,i,order)
                deadline=time.monotonic()+self.timeout
                while True:
                    row=self.ledger.store.read()['active']['orders'][i]
                    if row['status']=='filled': break
                    if row['status'] in TERMINAL: raise SafetyStop(f"Order ended {row['status']}; review remaining plan")
                    if time.monotonic()>=deadline: raise SafetyStop('Order remains open; resume the SAME persisted plan')
                    time.sleep(self.poll_seconds)
                    order=self.broker.by_client_id(row['client_order_id'])
                    if order is None: raise SafetyStop('Submitted broker order disappeared')
                    self.ledger.record(t,i,order)
                self.recover(t)
            self.recover(t)
            self.ledger.transfer(t)
            result=self.ledger.complete(t)
            return {'status':'complete','run':result}
        finally:
            self.ledger.release(t)
