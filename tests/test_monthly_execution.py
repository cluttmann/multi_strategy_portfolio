import datetime as dt
from decimal import Decimal
import pytest
from execution.quotes import validate_quote, execution_limit, QuoteDeferred
from execution.ledger import SafetyStop, Ledger, new_state
from execution.broker import Executor
from test_execution import MemoryStore

NOW=dt.datetime(2026,10,2,15,tzinfo=dt.timezone.utc)
def quote(bid='99.8',ask='100.2',source='iex',stamp=None):
    return {'bp':bid,'ap':ask,'bs':100,'as':100,'t':stamp or NOW.isoformat(),'source':source}

@pytest.mark.parametrize('source',['sip','delayed_sip',None])
def test_execution_requires_actual_iex_source(source):
    with pytest.raises(SafetyStop):validate_quote(quote(source=source),NOW)

def test_execution_rejects_31_second_quote_and_zero_depth():
    for q in [quote(stamp=(NOW-dt.timedelta(seconds=31)).isoformat()),dict(quote(),bs=0)]:
        with pytest.raises(SafetyStop):validate_quote(q,NOW)

def test_eet_limit_rounding_never_exceeds_concession():
    q=quote('100.001','100.401')
    mid=validate_quote(q,NOW)
    buy=execution_limit(q,'EET','buy',0,NOW)
    sell=execution_limit(q,'EET','sell',0,NOW)
    assert buy<=mid*Decimal('1.001') and sell>=mid*Decimal('.999')
    assert buy<Decimal(q['ap']) and sell>Decimal(q['bp'])
    assert execution_limit(q,'EET','buy',1,NOW)<=mid*Decimal('1.0015')

def test_wide_eet_is_deferred_not_a_safety_data_failure():
    with pytest.raises(QuoteDeferred):execution_limit(quote('99','101'),'EET','buy',0,NOW)

class MonthlyBroker:
    def __init__(self):self.orders={};self.submits=0;self.cash=Decimal(100);self.qty={};self.lost=False
    def clock(self):return {'is_open':True}
    def positions(self):return [{'symbol':s,'qty':str(q),'current_price':'100'} for s,q in self.qty.items() if q]
    def account(self):return {'id':'account','cash':str(self.cash),'buying_power':str(self.cash),'equity':'100'}
    def activities(self,after):return []
    def open_orders(self):return [o for o in self.orders.values() if o['status'] not in ('filled','canceled','expired','rejected')]
    def by_client_id(self,cid):return self.orders.get(cid)
    def submit(self,row):
        self.submits+=1
        o={**row,'id':str(self.submits),'filled_qty':'0','filled_avg_price':None,'status':'new'}
        self.orders[row['client_order_id']]=o
        if self.lost:self.lost=False;raise TimeoutError('lost acknowledgement')
        return o
    def cancel(self,oid):
        for o in self.orders.values():
            if o['id']==oid:o['status']='canceled'


def fixture(quote_getter=None):
    s=new_state('account','paper',{'a':{},'b':{}},{'reserve':'100'}, {}, NOW.isoformat())
    store=MemoryStore(s);broker=MonthlyBroker();ledger=Ledger(store)
    ex=Executor(ledger,broker,quote_getter=quote_getter or (lambda s:quote()),margin_validator=lambda state:True,now=lambda:NOW)
    p={'action':'monthly','period':'2026-10','execution_policy':'monthly-iex-v1','orders':[],
       'funding':{'a':'50','b':'50'},'funding_debt':{'a':'0','b':'0'},'margin_budget':'0','transfers':{},
       'intents':[{'id':'a-EET-buy','strategy':'a','symbol':'EET','side':'buy','budget':'50','fractionable':True},
                  {'id':'b-UBT-buy','strategy':'b','symbol':'UBT','side':'buy','budget':'50','fractionable':True}]}
    return ex,store,broker,p

def test_wide_symbol_does_not_block_other_sleeve_or_repeat_funding():
    ex,store,broker,p=fixture(lambda s:quote('99','101') if s=='EET' else quote())
    assert ex.run(p)['status']=='pending'
    assert broker.submits==1 and store.read()['active']['orders'][0]['symbol']=='UBT'
    assert store.read()['portfolios']['a']['cash']=='50'
    ex.run(p)
    assert broker.submits==1 and store.read()['portfolios']['a']['cash']=='50'
    assert store.read()['active']['intents'][0]['reason']=='spread_above_hard_limit'

def test_cancel_confirms_terminal_before_retry_id_and_reserves_cash():
    ex,store,broker,p=fixture();ex.run(p)
    original=[r['client_order_id'] for r in store.read()['active']['orders']]
    ex.now=lambda:NOW+dt.timedelta(seconds=301)
    ex.quote_getter=lambda symbol:quote(stamp=ex.now().isoformat())
    ex.run(p)
    attempts=store.read()['active']['orders']
    assert all(r['status']=='canceled' for r in attempts[:2])
    assert len(attempts)==4 and all(r['client_order_id'] not in original for r in attempts[2:])
    assert broker.submits==4

def test_ambiguous_submit_is_never_reposted():
    ex,store,broker,p=fixture();broker.lost=True
    assert ex.run(p)['status']=='data_error'
    ex.run(p)
    assert broker.submits==2
    assert len(store.read()['active']['orders'])==2

def test_expiry_preserves_funds_and_does_not_claim_clean_completion():
    ex,store,broker,p=fixture(lambda s:quote('90','110'));ex.run(p)
    ex.now=lambda:dt.datetime(2026,10,7,15,tzinfo=dt.timezone.utc)
    result=ex.run(p)
    assert result['status']=='expired'
    assert store.read()['active'] is None
    assert store.read()['last_completed']=={}
    assert store.read()['portfolios']['a']['cash']=='50'
    assert store.read()['last_expired']['monthly']=='2026-10'

def test_partial_cancel_and_late_fill_are_booked_before_new_attempt_budget():
    ex,store,broker,p=fixture();ex.run(p)
    first=next(iter(broker.orders.values()))
    first.update(filled_qty='.1',filled_avg_price='100',status='partially_filled')
    broker.qty['EET']=Decimal('.1');broker.cash-=10
    ex.now=lambda:NOW+dt.timedelta(seconds=301)
    ex.quote_getter=lambda symbol:quote(stamp=ex.now().isoformat())
    ex.run(p)
    active=store.read()['active'];rows=[r for r in active['orders'] if r['symbol']=='EET']
    assert rows[0]['booked_qty']=='.1' or Decimal(rows[0]['booked_qty'])==Decimal('.1')
    assert Decimal(rows[1]['qty'])*Decimal(rows[1]['limit_price'])<=40
    assert store.read()['portfolios']['a']['cash']=='40.0'


def test_unconfirmed_cancel_retains_reservation_and_never_reposts():
    ex,store,broker,p=fixture();ex.run(p)
    broker.cancel=lambda oid:None
    ex.now=lambda:NOW+dt.timedelta(seconds=301)
    ex.quote_getter=lambda symbol:quote(stamp=ex.now().isoformat())
    assert ex.run(p)['status']=='pending'
    assert broker.submits==2


def test_bad_quote_is_visible_but_other_sleeve_remains_eligible():
    ex,store,broker,p=fixture(lambda s:dict(quote(),bs=0) if s=='EET' else quote())
    result=ex.run(p)
    assert result['status']=='data_error' and result['errors']
    assert broker.submits==1
    assert store.read()['active']['intents'][0]['status']=='data_error'


def test_fresh_buying_power_and_margin_permission_gate_each_buy():
    ex,store,broker,p=fixture();broker.account=lambda:{'id':'account','cash':'100','buying_power':'0'}
    assert ex.run(p)['status']=='pending' and broker.submits==0
    assert all(i['reason']=='awaiting_cash_or_margin_permission' for i in store.read()['active']['intents'])


def test_expired_month_cannot_reallocate_or_restart_funding():
    ex,store,broker,p=fixture(lambda s:quote('90','110'));ex.run(p)
    ex.now=lambda:dt.datetime(2026,10,7,15,tzinfo=dt.timezone.utc)
    ex.run(p)
    assert ex.run(p)['status']=='already_complete'
    assert store.read()['portfolios']['a']['cash']=='50'


def test_pending_monthly_daily_risk_off_cancels_before_suspension():
    ex,store,broker,p=fixture();p['metadata']={'a':{'leg_states':{'EET':True}},'b':{}}
    ex.run(p)
    daily={'action':'daily','period':'2026-10-02','orders':[],'metadata':{'a':{'leg_states':{'EET':False}}}}
    result=ex.run(interrupt_builder=lambda state,broker:daily)
    assert result['status']=='complete'
    suspended=store.read()['suspended_monthly']
    assert suspended['intents'][0]['status']=='invalidated'
    assert all(row['status']=='canceled' for row in suspended['orders'])
    assert store.read()['portfolios']['a']['cash']=='50'
    ex.run()
    assert store.read()['active']['id']==suspended['id']
    assert all(row['symbol']!='EET' for row in list(broker.orders.values())[2:])


def test_retry_does_not_start_before_1030_but_funding_is_durable():
    ex,store,broker,p=fixture();ex.now=lambda:NOW.replace(hour=13)
    assert ex.run(p)['status']=='pending'
    assert broker.submits==0 and store.read()['portfolios']['a']['cash']=='50'


def test_unknown_ambiguous_accept_returns_data_error_without_repost():
    ex,store,broker,p=fixture();broker.lost=True;ex.run(p)
    broker.orders.clear()
    result=ex.run(p)
    assert result['status']=='data_error' and 'ambiguous' in result['errors'][0].lower()
    assert broker.submits==2

def test_reduced_margin_gate_caps_unused_original_borrowing():
    ex,store,broker,p=fixture()
    p['funding_debt']={'a':'50','b':'50'};p['margin_budget']='100'
    store.state['portfolios']['reserve']['cash']='0';broker.cash=Decimal(0)
    broker.account=lambda:{'id':'account','cash':'0','buying_power':'1000','equity':'100'}
    ex.margin_validator=lambda state:{'allowed':True,'errors':[],'target_margin':'.1'}
    result=ex.run(p)
    assert result['status']=='pending'
    active=store.read()['active']
    assert sum(Decimal(r['qty'])*Decimal(r['limit_price']) for r in active['orders'])<=10
    assert sum(Decimal(v['debt']) for v in store.read()['portfolios'].values())==100

def test_iex_getter_rejects_other_cached_feed_and_readonly_never_writes(monkeypatch):
    from execution.quotes import get_quote
    import requests
    class Bot:
        def get_all_market_data(self,*a):return {'execution_quote':quote(source='sip')}
        def get_auth_headers(self,api):return {}
        def get_firestore_client(self):raise AssertionError('read-only getter must not write')
    calls=[]
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'quote':{k:v for k,v in quote().items() if k!='source'}}
    monkeypatch.setattr(requests,'get',lambda url,**kwargs:calls.append(kwargs) or Response())
    result=get_quote(Bot(),{},'EET','paper',persist=False,now=NOW)
    assert result['source']=='iex' and calls[0]['params']=={'feed':'iex'}


def test_sell_funded_buy_waits_for_actual_cash_not_planned_proceeds():
    ex,store,broker,p=fixture();store.state['portfolios']['reserve']['cash']='0'
    store.state['portfolios']['a']['positions']={'EET':'1'}
    broker.qty={'EET':Decimal(1)};broker.cash=Decimal(0)
    p.update(funding={},funding_debt={},intents=[
        {'id':'sell','strategy':'a','symbol':'EET','side':'sell','qty':'1','fractionable':True,'full_liquidation':True},
        {'id':'buy','strategy':'a','symbol':'UBT','side':'buy','budget':'100','fractionable':True}])
    ex.run(p)
    assert broker.submits==1
    first=next(iter(broker.orders.values()));first.update(status='filled',filled_qty='1',filled_avg_price='100')
    broker.qty['EET']=Decimal(0);broker.cash=Decimal(100)
    ex.now=lambda:NOW+dt.timedelta(seconds=301);ex.quote_getter=lambda s:quote(stamp=ex.now().isoformat())
    ex.run(p)
    assert broker.submits==2
    assert store.read()['portfolios']['a']['cash']=='100'


def test_nonfractional_buy_residual_stays_owned_cash():
    ex,store,broker,p=fixture()
    for i in p['intents']:i['fractionable']=False
    result=ex.run(p)
    assert result['status']=='complete' and broker.submits==0
    assert store.read()['portfolios']['a']['cash']=='50'
    assert all(i['reason']=='whole_share_cash_residual' for i in result['run']['intents'])


def test_daily_cancel_unconfirmed_never_suspends_or_reuses_cash():
    ex,store,broker,p=fixture();p['metadata']={'a':{}}
    ex.run(p);broker.cancel=lambda oid:None
    daily={'action':'daily','period':'2026-10-02','orders':[],'metadata':{'a':{'leg_states':{'EET':False}}}}
    result=ex.run(interrupt_builder=lambda state,broker:daily)
    assert result['status']=='pending' and result['reason']=='daily_risk_cancel_unconfirmed'
    assert store.read()['active']['action']=='monthly'
    assert not store.read().get('suspended_monthly')


def test_daily_signal_runs_even_when_monthly_plan_is_suspended():
    ex,store,broker,p=fixture();p['metadata']={'a':{}}
    ex.run(p)
    calls=[]
    daily={'action':'daily','period':'2026-10-02','orders':[],'metadata':{'a':{'leg_states':{'EET':False}}}}
    ex.run(interrupt_builder=lambda state,broker:daily)
    assert store.read().get('suspended_monthly')
    next_daily={**daily,'period':'2026-10-03'}
    def build(state,broker):calls.append(state['active']['action']);return next_daily
    assert ex.run(interrupt_builder=build)['status']=='complete'
    assert calls==['monthly','monthly']
    assert store.read()['last_completed']['daily']=='2026-10-03'
