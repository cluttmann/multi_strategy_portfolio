import copy
from decimal import Decimal
import pytest
from execution.ledger import Ledger, SafetyStop, new_state, apply_order, reconcile_state

class MemoryStore:
    def __init__(self, state): self.state=copy.deepcopy(state); self.events=[]
    def read(self): return copy.deepcopy(self.state)
    def mutate(self, kind, fn):
        candidate=self.read(); result=fn(candidate)
        self.state=candidate; self.events.append((kind,copy.deepcopy(candidate)))
        return result

def seeded():
    return new_state('account','paper', {'aaa':{'UBT':'60'},'mix8':{'UBT':'40'}},
                     {'aaa':'0','mix8':'0','reserve':'100'}, {}, '2026-09-25T19:00:00Z')

def order(qty='15',filled='15',status='filled'):
    return dict(id='broker-1',client_order_id='cid',symbol='UBT',side='sell',qty=qty,
                filled_qty=filled,filled_avg_price='10',status=status)

def test_shared_sale_and_duplicate_fill_never_consume_other_owner():
    s=seeded(); row=dict(strategy='mix8',symbol='UBT',side='sell',qty='15',client_order_id='cid',booked_qty='0',booked_value='0')
    apply_order(s,row,order())
    apply_order(s,row,order())
    assert s['portfolios']['aaa']['positions']['UBT']=='60'
    assert s['portfolios']['mix8']['positions']['UBT']=='25'
    assert s['portfolios']['mix8']['cash']=='150'

def test_partial_then_cancel_books_only_confirmed_execution():
    s=seeded(); row=dict(strategy='mix8',symbol='UBT',side='sell',qty='15',client_order_id='cid',booked_qty='0',booked_value='0')
    apply_order(s,row,order(filled='4',status='partially_filled'))
    apply_order(s,row,order(filled='6',status='canceled'))
    assert s['portfolios']['mix8']['positions']['UBT']=='34'
    assert s['portfolios']['mix8']['cash']=='60'
    with pytest.raises(SafetyStop): apply_order(s,row,order(filled='4'))

def test_cannot_sell_another_strategys_shares():
    store=MemoryStore(seeded()); ledger=Ledger(store, clock=lambda:100)
    token=ledger.acquire()
    with pytest.raises(SafetyStop):
        ledger.start(token,dict(action='test',period='2026-09-25',orders=[dict(strategy='mix8',symbol='UBT',side='sell',qty='41',limit_price='10')]))
    assert store.read()['active'] is None

def test_reconcile_fails_on_unknown_quantity_or_cash():
    s=seeded()
    reconcile_state(s,[dict(symbol='UBT',qty='100')],'100')
    with pytest.raises(SafetyStop): reconcile_state(s,[dict(symbol='UBT',qty='101')],'100')
    with pytest.raises(SafetyStop): reconcile_state(s,[dict(symbol='UBT',qty='100')],'101')

def test_stale_worker_is_fenced_and_active_intent_survives_takeover():
    now=[100]; store=MemoryStore(seeded()); ledger=Ledger(store,clock=lambda:now[0])
    a=ledger.acquire(); ledger.start(a,dict(action='test',period='2026-09-25',orders=[dict(strategy='mix8',symbol='UBT',side='sell',qty='2',limit_price='10')]))
    now[0]=1000; b=ledger.acquire()
    with pytest.raises(SafetyStop): ledger.mark_submitting(a,0)
    ledger.mark_submitting(b,0)
    assert store.read()['active']['orders'][0]['status']=='submitting'

def test_run_funding_is_once_and_completed_period_cannot_replay():
    store=MemoryStore(seeded()); l=Ledger(store,clock=lambda:100); t=l.acquire()
    plan=dict(action='monthly',period='2026-09',funding={'mix8':'50'},orders=[])
    l.start(t,plan); l.start(t,plan)
    assert store.read()['portfolios']['mix8']['cash']=='50'
    l.complete(t); l.start(t,plan)
    assert store.read()['portfolios']['mix8']['cash']=='50'
    assert store.read()['active'] is None

from execution.broker import Executor

class FakeBroker:
    def __init__(self): self.orders={}; self.submits=0; self.lost=False
    def clock(self): return {'is_open':True}
    def open_orders(self): return [o for o in self.orders.values() if o['status'] not in ('filled','canceled')]
    def by_client_id(self,cid): return self.orders.get(cid)
    def submit(self,row):
        self.submits+=1
        o=dict(id='broker-1',client_order_id=row['client_order_id'],symbol=row['symbol'],side=row['side'],
               qty=row['qty'],filled_qty=row['qty'],filled_avg_price='10',status='filled')
        self.orders[row['client_order_id']]=o
        if self.lost: self.lost=False; raise TimeoutError('response lost after broker acceptance')
        return o
    def positions(self): return [dict(symbol='UBT',qty=str(100-sum(Decimal(o['filled_qty']) for o in self.orders.values())))]
    def account(self): return {'cash':str(100+sum(Decimal(o['filled_qty'])*10 for o in self.orders.values())),'id':'account'}
    def activities(self,after): return []

def test_accepted_order_with_lost_response_recovers_without_second_post():
    store=MemoryStore(seeded()); broker=FakeBroker(); broker.lost=True
    ex=Executor(Ledger(store),broker)
    plan=dict(action='test',period='2026-09-25',orders=[dict(strategy='mix8',symbol='UBT',side='sell',qty='2',limit_price='9')])
    with pytest.raises(TimeoutError): ex.run(plan)
    ex.run(plan)
    assert broker.submits==1
    assert store.read()['portfolios']['mix8']['positions']['UBT']=='38'
    ex.run(plan)
    assert broker.submits==1

def test_unresolved_submission_is_never_reposted_or_skipped():
    store=MemoryStore(seeded()); broker=FakeBroker(); l=Ledger(store)
    t=l.acquire(); l.start(t,dict(action='test',period='2026-09-25',orders=[dict(strategy='mix8',symbol='UBT',side='sell',qty='2',limit_price='9')]))
    l.mark_submitting(t,0); l.release(t)
    with pytest.raises(SafetyStop,match='ambiguous'): Executor(l,broker).run(None)
    assert broker.submits==0


def test_partial_fill_at_cancel_then_late_fill_still_books_exactly_once():
    s=seeded(); row=dict(strategy='mix8',symbol='UBT',side='sell',qty='15',client_order_id='cid',booked_qty='0',booked_value='0')
    apply_order(s,row,order(filled='3',status='canceled'))
    apply_order(s,row,order(filled='5',status='canceled'))
    assert s['portfolios']['mix8']['cash']=='50'
    assert s['portfolios']['aaa']['positions']['UBT']=='60'


def test_market_closes_during_planning_does_not_reserve_money():
    store=MemoryStore(seeded()); broker=FakeBroker();broker.clock=lambda:{'is_open':False}
    ex=Executor(Ledger(store),broker)
    p=dict(action='test',period='2026-09-25',funding={'mix8':'10'},orders=[dict(strategy='mix8',symbol='UBT',side='sell',qty='2',limit_price='9')])
    assert ex.run(p)['status']=='awaiting_market'
    assert store.read()['active'] is None
    assert store.read()['portfolios']['mix8']['cash']=='0'

from execution.broker import sync_activities

def test_cash_event_books_once_and_unknown_manual_fill_stops():
    store=MemoryStore(seeded());l=Ledger(store);t=l.acquire()
    a={'id':'div1','activity_type':'DIV','net_amount':'10','date':'2026-09-25'}
    sync_activities(l,t,[a]);sync_activities(l,t,[a])
    assert store.read()['portfolios']['reserve']['cash']=='110'
    with pytest.raises(SafetyStop,match='Unattributed'):
        sync_activities(l,t,[{'id':'fillx','activity_type':'FILL','order_id':'manual'}])

def test_firestore_write_failure_prevents_order_submission():
    store=MemoryStore(seeded());broker=FakeBroker();l=Ledger(store)
    original=store.mutate
    def broken(kind,fn):
        if kind=='intent':raise RuntimeError('database offline')
        return original(kind,fn)
    store.mutate=broken
    p=dict(action='test',period='2026-09-25',orders=[dict(strategy='mix8',symbol='UBT',side='sell',qty='2',limit_price='9')])
    with pytest.raises(RuntimeError,match='database offline'):Executor(l,broker).run(p)
    assert broker.submits==0

def test_competing_executor_cannot_acquire_a_live_lease():
    store=MemoryStore(seeded());first=Ledger(store);second=Ledger(store)
    first.acquire()
    with pytest.raises(SafetyStop,match='Another executor'):second.acquire()


def test_deposit_pays_attributed_debt_and_preserves_drawdown_ratio():
    s=seeded();s['portfolios']['reserve']['cash']='0'
    s['portfolios']['mix8']['debt']='100'
    s['portfolios']['mix8']['metadata']['peak_nav']='400'
    # Mix8 40 shares * 10 - 100 debt = 300 NAV, 25% below peak.
    st=MemoryStore(s);l=Ledger(st);t=l.acquire()
    sync_activities(l,t,[{'id':'deposit','activity_type':'CSD','date':'2026-09-25','net_amount':'50'}],marks={'UBT':'10'})
    assert st.read()['portfolios']['mix8']['debt']=='50'
    assert st.read()['portfolios']['reserve']['cash']=='0'
    peak=Decimal(st.read()['portfolios']['mix8']['metadata']['peak_nav'])
    assert abs(Decimal('350')/peak-Decimal('.75'))<Decimal('.00000001')


def test_stale_persisted_plan_cannot_submit_new_orders():
    store=MemoryStore(seeded());broker=FakeBroker();l=Ledger(store)
    t=l.acquire();l.start(t,dict(action='old',period='2026-09-24',planned_at='2026-09-24T12:00:00Z',orders=[dict(strategy='mix8',symbol='UBT',side='sell',qty='2',limit_price='9')]))
    l.release(t)
    with pytest.raises(SafetyStop,match='stale'):Executor(l,broker).run()
    assert broker.submits==0


def test_broker_nanosecond_migration_timestamp_replays_utc_midnight_fees():
    s=seeded()
    s['started_at']='2026-09-25T15:55:42.978124906-04:00'
    s['activity_through']='2026-09-25T19:58:59.123456789Z'
    broker=FakeBroker();queries=[]
    broker.activities=lambda after:queries.append(after) or []
    result=Executor(Ledger(MemoryStore(s)),broker).run()
    assert result=={'status':'reconciled'}
    assert queries==['2026-09-25T00:00:00+00:00']
    assert broker.submits==0
