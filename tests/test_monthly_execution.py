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

def test_crash_after_atomic_attempt_claim_never_leaves_planned_or_reposts():
    ex,store,broker,p=fixture();original=store.mutate;crashed=[False]
    def crash(kind,fn):
        result=original(kind,fn)
        if kind=='monthly_attempt' and not crashed[0]:
            crashed[0]=True
            raise RuntimeError('crash immediately after durable claim')
        return result
    store.mutate=crash
    result=ex.run(p)
    assert result['status']=='data_error'
    first=store.read()['active']['orders'][0]
    assert first['status']=='submitting' and first['submitted_at']
    before=broker.submits
    assert ex.run(p)['status']=='data_error'
    assert broker.submits==before


def test_reconcile_finalizes_monthly_completion_once():
    from test_shared_integration import controller_fixture
    c,store,broker=controller_fixture();messages=[];audit=[]
    run={'id':'completed-run','action':'monthly','period':'2026-10','orders':[]}
    c.executor.run=lambda:{'status':'complete','run':run}
    c.publish=lambda:None
    c.bot.send_telegram_message=lambda message:messages.append(message) or True
    c.bot.mark_monthly_run_complete=lambda env,clean:audit.append((env,clean))
    c.reconcile();c.reconcile()
    assert len(messages)==1 and audit==[('paper',True)]


def test_reconcile_expiry_reports_real_remainder_without_clean_marker():
    from test_shared_integration import controller_fixture
    c,store,broker=controller_fixture();messages=[];audit=[]
    run={'id':'expired-run','action':'monthly','period':'2026-10','orders':[],
         'intents':[{'id':'x','strategy':'aaa','symbol':'EET','side':'buy','budget':'123.45','status':'expired','reason':'three_trading_day_expiry'}]}
    c.executor.run=lambda:{'status':'expired','run':run}
    c.publish=lambda:None;c.bot.send_telegram_message=lambda message:messages.append(message) or True
    c.bot.mark_monthly_run_complete=lambda env,clean:audit.append((env,clean))
    c.reconcile()
    assert '$123.45' in messages[0] and 'EET' in messages[0] and not audit

def test_slow_margin_validation_cannot_submit_old_quote():
    ex,store,broker,p=fixture();time=[NOW];ex.now=lambda:time[0]
    ex.quote_getter=lambda symbol:quote(stamp=time[0].isoformat())
    def slow_margin(state):time[0]+=dt.timedelta(seconds=31);return True
    ex.margin_validator=slow_margin
    ages=[];submit=broker.submit
    def checked(row):ages.append((time[0]-dt.datetime.fromisoformat(row['quote']['t'])).total_seconds());return submit(row)
    broker.submit=checked
    ex.run(p)
    assert ages==[0,0]


def test_slow_claim_write_aborts_before_post_and_is_recoverable():
    ex,store,broker,p=fixture();time=[NOW];ex.now=lambda:time[0]
    ex.quote_getter=lambda symbol:quote(stamp=time[0].isoformat())
    original=store.mutate
    def slow_claim(kind,fn):
        result=original(kind,fn)
        if kind=='monthly_attempt':time[0]+=dt.timedelta(seconds=31)
        return result
    store.mutate=slow_claim
    assert ex.run(p)['status']=='data_error'
    assert broker.submits==0
    assert all(row['never_submitted'] and row['status']=='canceled' for row in store.read()['active']['orders'])
    # Restore a healthy database and advance the scheduled retry.
    store.mutate=original;time[0]+=dt.timedelta(minutes=5)
    assert ex.run(p)['status']=='pending' and broker.submits==2

def test_daily_risk_on_invalidates_obsolete_pending_monthly_sell():
    ex,store,broker,p=fixture();p['metadata']={'a':{}}
    store.state['portfolios']['a']['positions']={'EET':'1'}
    broker.qty={'EET':Decimal(1)}
    p['intents']=[{'id':'sell','strategy':'a','symbol':'EET','side':'sell','qty':'1','fractionable':True,'risk_exit':True}]
    ex.run(p)
    daily={'action':'daily','period':'2026-10-02','orders':[],'metadata':{'a':{'leg_states':{'EET':True}}}}
    ex.run(interrupt_builder=lambda state,broker:daily)
    intent=store.read()['suspended_monthly']['intents'][0]
    assert intent['status']=='invalidated' and intent['reason']=='daily_risk_on'
    assert ex.run()['status']=='expired'
    assert broker.submits==1 and store.read()['portfolios']['a']['positions']['EET']=='1'


def test_confirmed_daily_fills_credit_original_monthly_budget():
    ex,store,broker,p=fixture();p['metadata']={'a':{}}
    ex.run(p)
    daily={'action':'daily','period':'2026-10-02',
           'orders':[{'strategy':'a','symbol':'EET','side':'buy','qty':'.2','limit_price':'100'}],
           'metadata':{'a':{'leg_states':{'EET':True}}}}
    submit=broker.submit
    def filled_daily(row):
        o=submit(row)
        if row.get('intent_id') is None:
            o.update(status='filled',filled_qty=row['qty'],filled_avg_price='100')
            broker.qty['EET']=broker.qty.get('EET',Decimal(0))+Decimal(row['qty'])
            broker.cash-=Decimal(row['qty'])*100
        return o
    broker.submit=filled_daily
    assert ex.run(interrupt_builder=lambda state,broker:daily)['status']=='complete'
    suspended=store.read()['suspended_monthly']
    assert Decimal(suspended['intents'][0]['external_booked_value'])==20
    assert store.read()['portfolios']['a']['cash']=='30.0'
    ex.now=lambda:NOW+dt.timedelta(seconds=301);ex.quote_getter=lambda s:quote(stamp=ex.now().isoformat())
    ex.run()
    latest=store.read()['active']['orders'][-2]
    assert latest['symbol']=='EET' and Decimal(latest['qty'])*Decimal(latest['limit_price'])<=30

def test_expiry_after_risk_exit_does_not_attempt_unfunded_annual_transfer():
    ex,store,broker,p=fixture();store.state['portfolios']['a']['positions']={'EET':'1'}
    broker.qty={'EET':Decimal(1)}
    p['transfers']={'a':'-200','b':'200'}
    p['intents']=[{'id':'exit','strategy':'a','symbol':'EET','side':'sell','qty':'1','fractionable':True,'full_liquidation':True,'risk_exit':True},
                  {'id':'entry','strategy':'b','symbol':'UBT','side':'buy','budget':'50','fractionable':True}]
    ex.run(p)
    ex.now=lambda:dt.datetime(2026,10,7,15,tzinfo=dt.timezone.utc)
    ex.quote_getter=lambda s:quote(stamp=ex.now().isoformat())
    submit=broker.submit
    def filled_exit(row):
        o=submit(row);o.update(status='filled',filled_qty='1',filled_avg_price='100')
        broker.qty['EET']=Decimal(0);broker.cash+=100
        return o
    broker.submit=filled_exit
    result=ex.run(p)
    assert result['status']=='expired' and store.read()['active'] is None
    assert store.read()['portfolios']['a']['cash']=='150'
    assert store.read()['portfolios']['b']['cash']=='50'
    assert not result['run'].get('transfers_done')


def test_opposing_shared_etf_order_waits_for_terminal_but_other_symbol_trades():
    ex,store,broker,p=fixture();store.state['portfolios']['a']['positions']={'EET':'1'};broker.qty={'EET':Decimal(1)}
    p['intents']=[{'id':'sell','strategy':'a','symbol':'EET','side':'sell','qty':'1','fractionable':True},
                  {'id':'buy','strategy':'b','symbol':'EET','side':'buy','budget':'25','fractionable':True},
                  {'id':'other','strategy':'b','symbol':'UBT','side':'buy','budget':'25','fractionable':True}]
    ex.run(p)
    assert [(o['symbol'],o['side']) for o in broker.orders.values()]==[('EET','sell'),('UBT','buy')]
    assert store.read()['active']['intents'][1]['reason']=='opposing_order_open'


def broker_rejection(status=403,json_body=None,content_type='application/json'):
    import requests
    response=requests.Response();response.status_code=status
    response.headers['Content-Type']=content_type
    response.url='https://paper-api.alpaca.markets/v2/orders'
    response.request=requests.Request('POST',response.url).prepare()
    import json
    response._content=json.dumps(json_body or {'code':40310000,'message':'asset not fractionable'}).encode()
    return requests.HTTPError('broker rejected order',response=response)


def test_definitive_broker_rejection_tombstone_recovers_without_false_ambiguity():
    ex,store,broker,p=fixture();submit=broker.submit
    def reject(row):
        if row['symbol']=='EET':raise broker_rejection()
        return submit(row)
    broker.submit=reject
    result=ex.run(p)
    row=store.read()['active']['orders'][0]
    assert result['status']=='data_error' and row['status']=='rejected' and row['no_order_proven']
    assert row['rejection']['http_status']==403 and row['rejection']['code']==40310000
    assert ex.run(p)['status']=='pending'
    assert broker.submits==1


def test_daily_risk_off_is_persisted_when_second_signal_read_fails():
    ex,store,broker,p=fixture();p['metadata']={'a':{}};ex.run(p)
    calls=[]
    def daily(state,broker):
        calls.append(True)
        if len(calls)>1:raise SafetyStop('daily feed unavailable after cancellation')
        return {'action':'daily','period':'2026-10-02','orders':[],'metadata':{'a':{'leg_states':{'EET':False}}}}
    with pytest.raises(SafetyStop):ex.run(interrupt_builder=daily)
    active=store.read()['active']
    assert active['intents'][0]['status']=='invalidated'
    assert active['daily_interruption']['status']=='needs_recovery'
    before=broker.submits
    assert ex.run()['status'] in ('pending','data_error')
    assert broker.submits==before


@pytest.mark.parametrize('delivery',[None,503,False])
def test_failed_telegram_delivery_stays_durable_after_archive_and_retries(delivery):
    from test_shared_integration import controller_fixture
    c,store,broker=controller_fixture();messages=[]
    c.publish=lambda:None;c.bot.mark_monthly_run_complete=lambda env,clean:None
    run={'id':'notice-run','action':'monthly','period':'2026-10','orders':[]}
    c.bot.send_telegram_message=lambda message:messages.append(message) or delivery
    c.executor.run=lambda:{'status':'complete','run':run}
    first=c.reconcile()
    assert 'notice-run' not in store.read().get('reported_runs',[])
    assert store.read()['pending_notifications']['notice-run']
    assert first.get('notification_errors')
    c.executor.run=lambda:{'status':'reconciled'}
    c.bot.send_telegram_message=lambda message:messages.append(message) or 200
    c.reconcile()
    assert len(messages)==2 and 'notice-run' in store.read()['reported_runs']
    assert not store.read()['pending_notifications']
    c.reconcile();assert len(messages)==2


@pytest.mark.parametrize('status',[400,401,403,422])
def test_only_known_broker_json_order_rejections_are_definitive(status):
    from execution.broker import definitive_order_rejection
    error=broker_rejection(status,{'code':status*100000+10000,'message':'invalid order quantity'})
    assert definitive_order_rejection(error)['http_status']==status


@pytest.mark.parametrize('kind',['html_proxy','wrong_host','wrong_method','wrong_code','duplicate_id','server_error','timeout'])
def test_proxy_or_transport_uncertainty_never_proves_no_order(kind):
    from execution.broker import definitive_order_rejection
    error=broker_rejection()
    if kind=='html_proxy':error.response.headers['Content-Type']='text/html'
    elif kind=='wrong_host':error.response.request.url='https://proxy.example/v2/orders'
    elif kind=='wrong_method':error.response.request.method='GET'
    elif kind=='wrong_code':error=broker_rejection(json_body={'code':123,'message':'proxy error'})
    elif kind=='duplicate_id':error=broker_rejection(json_body={'code':40310000,'message':'duplicate client_order_id'})
    elif kind=='server_error':error=broker_rejection(status=503)
    elif kind=='timeout':error=TimeoutError('POST response missing')
    assert definitive_order_rejection(error) is None


def test_urgent_exit_cancels_opposing_buy_and_confirms_before_post():
    ex,store,broker,p=fixture();store.state['portfolios']['a']['positions']={'EET':'1'};broker.qty={'EET':Decimal(1)}
    p['intents']=[{'id':'buy','strategy':'b','symbol':'EET','side':'buy','budget':'50','fractionable':True}]
    ex.run(p)
    store.state['active']['intents'].append({'id':'exit','strategy':'a','symbol':'EET','side':'sell','qty':'1','fractionable':True,'risk_exit':True})
    ex.run()
    orders=list(broker.orders.values())
    assert orders[0]['status']=='canceled' and orders[1]['side']=='sell'
    assert broker.submits==2


def test_urgent_exit_does_not_post_until_opposing_cancel_is_confirmed():
    ex,store,broker,p=fixture();store.state['portfolios']['a']['positions']={'EET':'1'};broker.qty={'EET':Decimal(1)}
    p['intents']=[{'id':'buy','strategy':'b','symbol':'EET','side':'buy','budget':'50','fractionable':True}]
    ex.run(p);broker.cancel=lambda oid:None
    store.state['active']['intents'].append({'id':'exit','strategy':'a','symbol':'EET','side':'sell','qty':'1','fractionable':True,'risk_exit':True})
    assert ex.run()['status']=='pending' and broker.submits==1
    assert store.read()['active']['intents'][1]['reason']=='opposing_cancel_unconfirmed'


def test_notifications_are_queued_in_the_same_financial_archive_mutation():
    ex,store,broker,p=fixture()
    for intent in p['intents']:intent['fractionable']=False
    ex.run(p)
    complete=[state for kind,state in store.events if kind=='complete'][0]
    assert complete['active'] is None
    assert complete['pending_notifications']


def test_daily_failure_barrier_clears_only_after_successful_daily_recovery():
    ex,store,broker,p=fixture();p['metadata']={'a':{}};ex.run(p)
    calls=[]
    daily={'action':'daily','period':'2026-10-02','orders':[],'metadata':{'a':{'leg_states':{'EET':False}}}}
    def fail_second(state,broker):
        calls.append(True)
        if len(calls)==2:raise SafetyStop('feed failure')
        return daily
    with pytest.raises(SafetyStop):ex.run(interrupt_builder=fail_second)
    assert ex.run()['reason']=='daily_recovery_required'
    assert ex.run(interrupt_builder=lambda state,broker:daily)['status']=='complete'
    assert store.read()['suspended_monthly']['daily_interruption']['status']=='recovered'


def test_reconcile_supplies_fresh_daily_recovery_before_monthly_resume():
    from test_shared_integration import controller_fixture
    c,store,broker=controller_fixture();calls=[]
    store.state['active']={'action':'monthly','daily_interruption':{'status':'needs_recovery'}}
    c.publish=lambda:None
    c.build=lambda state,action,period,annual:calls.append(action) or {'action':'daily'}
    def run(**kwargs):
        assert kwargs['interrupt_builder'](store.read(),broker)['action']=='daily'
        return {'status':'pending'}
    c.executor.run=run
    assert c.reconcile()['status']=='pending' and calls==['daily']


def test_telegram_exception_is_retryable_without_persisting_secret_url():
    from test_shared_integration import controller_fixture
    c,store,broker=controller_fixture();c.publish=lambda:None
    c.bot.mark_monthly_run_complete=lambda env,clean:None
    c.executor.run=lambda:{'status':'complete','run':{'id':'exception-run','action':'daily','period':'2026-10-02','orders':[]}}
    def broken(message):raise RuntimeError('https://api.telegram.org/botSECRET/sendMessage')
    c.bot.send_telegram_message=broken
    result=c.reconcile()
    assert result['status']=='data_error' and 'exception-run' in store.read()['pending_notifications']
    assert 'SECRET' not in str(store.read()) and 'SECRET' not in str(result)


def test_definitive_rejection_of_daily_legacy_order_is_known_not_ambiguous():
    from test_execution import seeded,FakeBroker
    store=MemoryStore(seeded());broker=FakeBroker();attempts=[]
    def rejected(row):attempts.append(row['client_order_id']);raise broker_rejection()
    broker.submit=rejected
    ex=Executor(Ledger(store),broker)
    plan={'action':'daily','period':'2026-10-02','orders':[{'strategy':'mix8','symbol':'UBT','side':'sell','qty':'2','limit_price':'9'}]}
    with pytest.raises(SafetyStop,match='rejected'):ex.run(plan)
    row=store.read()['active']['orders'][0]
    assert row['no_order_proven'] and row['status']=='rejected'
    with pytest.raises(SafetyStop,match='rejected'):ex.run()
    assert len(attempts)==1


def test_rejection_evidence_redacts_any_echoed_request_credentials():
    from execution.broker import definitive_order_rejection
    error=broker_rejection(json_body={'code':40310000,'message':'key TOPSECRET refused'})
    error.response.request.headers['APCA-API-KEY-ID']='TOPSECRET'
    evidence=definitive_order_rejection(error)
    assert 'TOPSECRET' not in evidence['message'] and '[redacted]' in evidence['message']
