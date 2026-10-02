"""The free IEX bid cap protects price, without claiming a consolidated spread."""
import datetime as dt
from decimal import Decimal

import pytest

from execution.monthly import remaining, reservations
from execution.quotes import execution_limit, QuoteDeferred
from test_monthly_execution import NOW, fixture, quote
from test_shared_integration import controller_fixture


PRICE_POLICY='iex-bid-cap-v1'


def passive_fixture(quote_getter=None):
    ex,store,broker,plan=fixture(quote_getter)
    plan['eet_buy_price_policy']=PRICE_POLICY
    return ex,store,broker,plan


def eet_rows(store):
    return [row for row in store.read()['active']['orders'] if row['symbol']=='EET']


@pytest.mark.parametrize('ask',['100.4','105','1000'])
def test_wide_iex_ask_never_raises_passive_purchase_price(ask):
    ex,store,broker,plan=passive_fixture(lambda symbol:quote('100.001',ask) if symbol=='EET' else quote())
    assert ex.run(plan)['status']=='pending'
    assert broker.submits==2
    row=eet_rows(store)[0]
    assert Decimal(row['limit_price'])==Decimal('100.10')
    assert Decimal(row['limit_price'])<=Decimal('100.001')*Decimal('1.001')
    assert Decimal(row['qty'])*Decimal(row['limit_price'])<=Decimal('50')
    assert row['price_policy']==PRICE_POLICY
    assert row['price_reference']=={'source':'iex','kind':'bid','price':'100.001'}
    # The other sleeve uses its original price rule even on a marked plan.
    other=next(row for row in store.read()['active']['orders'] if row['symbol']=='UBT')
    assert other['limit_price']=='100.20'
    assert other['price_policy']=='iex-midpoint-v1'
    assert other['price_reference']=={'source':'iex','kind':'midpoint','price':'100.0'}


@pytest.mark.parametrize('bid,ask,expected',[
    ('100.001','100.099','100.09'),
    ('99.999','110','100.09'),
    ('100','100','100.00'),
])
def test_passive_buy_rounds_down_and_never_exceeds_bid_cap_or_ask(bid,ask,expected):
    ex,store,broker,plan=passive_fixture(lambda symbol:quote(bid,ask) if symbol=='EET' else quote())
    ex.run(plan)
    limit=Decimal(eet_rows(store)[0]['limit_price'])
    assert limit==Decimal(expected)
    assert limit<=min(Decimal(ask),Decimal(bid)*Decimal('1.001'))


def test_every_confirmed_retry_uses_same_concession_and_fixed_original_budget():
    time=[NOW]
    ex,store,broker,plan=passive_fixture(lambda symbol:quote('100','105',stamp=time[0].isoformat()) if symbol=='EET' else quote(stamp=time[0].isoformat()))
    ex.now=lambda:time[0]
    assert ex.run(plan)['status']=='pending'
    original_id=store.read()['active']['id']
    for _ in range(3):
        time[0]+=dt.timedelta(seconds=301)
        assert ex.run(plan)['status']=='pending'
    rows=eet_rows(store)
    assert len(rows)==4
    assert [row['attempt_number'] for row in rows]==[0,1,2,3]
    assert all(Decimal(row['limit_price'])==Decimal('100.10') for row in rows)
    assert all(row['status']=='canceled' for row in rows[:-1])
    assert len({row['client_order_id'] for row in rows})==4
    assert all(Decimal(row['qty'])*Decimal(row['limit_price'])<=50 for row in rows)
    assert store.read()['active']['id']==original_id
    assert store.read()['active']['intents'][0]['budget']=='50'
    assert store.read()['portfolios']['a']['cash']=='50'
    assert store.read()['portfolios']['reserve']['cash']=='0'


def test_fresh_bid_can_change_absolute_limit_only_after_confirmed_cancel():
    time=[NOW];bid=['100']
    ex,store,broker,plan=passive_fixture(lambda symbol:quote(bid[0],'110',stamp=time[0].isoformat()) if symbol=='EET' else quote(stamp=time[0].isoformat()))
    ex.now=lambda:time[0]
    ex.run(plan)
    cancel=broker.cancel
    broker.cancel=lambda order_id:None
    time[0]+=dt.timedelta(seconds=301);bid[0]='101'
    ex.run(plan)
    assert len(eet_rows(store))==1 and broker.submits==2
    assert reservations(store.read()['active'],'a')>0
    broker.cancel=cancel
    ex.run(plan)
    rows=eet_rows(store)
    assert len(rows)==2 and rows[0]['status']=='canceled'
    assert rows[1]['limit_price']=='101.10'
    assert rows[1]['price_reference']['price']=='101'
    assert Decimal(rows[1]['limit_price'])<=Decimal('101')*Decimal('1.001')


def test_partial_and_cancel_time_fills_book_before_reusing_reserved_budget():
    time=[NOW]
    ex,store,broker,plan=passive_fixture(lambda symbol:quote('100','110',stamp=time[0].isoformat()) if symbol=='EET' else quote(stamp=time[0].isoformat()))
    ex.now=lambda:time[0]
    ex.run(plan)
    first=next(order for order in broker.orders.values() if order['symbol']=='EET')
    first.update(filled_qty='.1',filled_avg_price='100',status='partially_filled')
    broker.qty['EET']=Decimal('.1');broker.cash-=10
    ex.run(plan)
    active=store.read()['active']
    assert remaining(active,active['intents'][0])==40
    assert reservations(active,'a')>Decimal('39')
    assert len(eet_rows(store))==1
    cancel=broker.cancel
    def late_fill(order_id):
        if order_id==first['id']:
            first.update(filled_qty='.2',filled_avg_price='100')
            broker.qty['EET']=Decimal('.2');broker.cash-=10
        cancel(order_id)
    broker.cancel=late_fill;time[0]+=dt.timedelta(seconds=301)
    ex.run(plan)
    rows=eet_rows(store);active=store.read()['active']
    assert Decimal(rows[0]['booked_value'])==20 and rows[0]['status']=='canceled'
    assert Decimal(rows[1]['qty'])*Decimal(rows[1]['limit_price'])<=30
    assert rows[1]['limit_price']=='100.10'
    assert remaining(active,active['intents'][0])==30
    assert reservations(active,'a')<=30
    assert store.read()['portfolios']['a']['cash']=='30.0'


@pytest.mark.parametrize('bad_quote',[
    quote('100','110',source='sip'),
    quote('100','110',stamp=(NOW-dt.timedelta(seconds=31)).isoformat()),
    dict(quote('100','110'),bs=0),
    dict(quote('100','110'),**{'as':0}),
    quote('101','100'),
    quote('NaN','110'),
    dict(quote('100','110'),t='malformed'),
])
def test_bid_cap_still_refuses_invalid_quotes_without_blocking_other_sleeve(bad_quote):
    ex,store,broker,plan=passive_fixture(lambda symbol:bad_quote if symbol=='EET' else quote())
    result=ex.run(plan)
    assert result['status']=='data_error' and result['errors']
    assert broker.submits==1 and not eet_rows(store)
    assert store.read()['active']['orders'][0]['symbol']=='UBT'
    assert store.read()['active']['intents'][0]['status']=='data_error'


@pytest.mark.parametrize('unknown',['future-policy',None,False])
def test_unknown_marker_fails_only_affected_eet_buy(unknown):
    ex,store,broker,plan=passive_fixture()
    plan['eet_buy_price_policy']=unknown
    result=ex.run(plan)
    assert result['status']=='data_error'
    assert 'price policy' in result['errors'][0].lower()
    assert broker.submits==1 and not eet_rows(store)
    assert store.read()['active']['orders'][0]['symbol']=='UBT'


def test_missing_marker_keeps_existing_midpoint_and_spread_policy():
    ex,store,broker,plan=fixture()
    ex.run(plan)
    first=eet_rows(store)[0]
    assert first['limit_price']=='100.10'
    assert first['price_policy']=='iex-midpoint-v1'
    assert first['price_reference']=={'source':'iex','kind':'midpoint','price':'100.0'}
    assert 'eet_buy_price_policy' not in store.read()['active']
    ex,store,broker,plan=fixture(lambda symbol:quote('100','110') if symbol=='EET' else quote())
    assert ex.run(plan)['status']=='pending'
    assert broker.submits==1 and not eet_rows(store)
    assert store.read()['active']['intents'][0]['reason']=='spread_above_hard_limit'


def test_retry_of_existing_legacy_plan_does_not_adopt_new_builder_policy():
    ex,store,broker,plan=fixture();ex.run(plan)
    old_id=store.read()['active']['id']
    plan['eet_buy_price_policy']=PRICE_POLICY
    ex.now=lambda:NOW+dt.timedelta(seconds=301)
    ex.quote_getter=lambda symbol:quote(stamp=ex.now().isoformat())
    ex.run(plan)
    rows=eet_rows(store)
    assert [row['limit_price'] for row in rows]==['100.10','100.15']
    assert rows[-1]['price_policy']=='iex-midpoint-v1'
    assert 'eet_buy_price_policy' not in store.read()['active']
    assert store.read()['active']['id']==old_id


def test_lost_acknowledgement_recovers_exact_passive_attempt_without_second_post():
    ex,store,broker,plan=passive_fixture(lambda symbol:quote('100','110') if symbol=='EET' else quote())
    broker.lost=True
    assert ex.run(plan)['status']=='data_error'
    rows=eet_rows(store);original_id=rows[0]['client_order_id']
    claim=next(event for kind,event in store.events if kind=='monthly_attempt')
    assert claim['active']['orders'][0]['status']=='submitting'
    assert rows[0]['status']=='new' and broker.submits==2
    assert rows[0]['price_policy']==PRICE_POLICY
    assert ex.run(plan)['status']=='pending'
    assert broker.submits==2 and len(eet_rows(store))==1
    assert eet_rows(store)[0]['client_order_id']==original_id
    assert eet_rows(store)[0]['status']=='new'


def test_unfilled_passive_entry_expires_to_owned_cash():
    ex,store,broker,plan=passive_fixture(lambda symbol:quote('100','110') if symbol=='EET' else quote())
    ex.run(plan)
    assert len(eet_rows(store))==1
    ex.now=lambda:dt.datetime(2026,10,7,15,tzinfo=dt.timezone.utc)
    result=ex.run(plan)
    assert result['status']=='expired' and store.read()['active'] is None
    assert all(row['status']=='canceled' for row in result['run']['orders'])
    assert result['run']['eet_buy_price_policy']==PRICE_POLICY
    assert store.read()['portfolios']['a']['cash']=='50'
    assert store.read()['last_completed']=={}
    assert broker.submits==2


@pytest.mark.parametrize('action',['monthly','monthly-funding'])
def test_controller_persists_policy_marker_before_any_attempt(action):
    controller,store,broker=controller_fixture()
    if action=='monthly-funding':
        for key in ('aaa','mix8'):
            store.state['portfolios'][key]['metadata']['last_momentum_check']={'weights':{'EET':1},'cash_weight':0}
    plan=controller.build(store.read(),action,'2026-10')
    assert plan['execution_policy']=='monthly-iex-v1'
    assert plan['eet_buy_price_policy']==PRICE_POLICY
    token=controller.ledger.acquire()
    try:
        controller.ledger.start(token,plan)
        persisted=store.read()['active']
        assert persisted['eet_buy_price_policy']==PRICE_POLICY
        assert persisted['orders']==[] and broker.submits==0
        start=next(event for kind,event in store.events if kind=='start')
        assert start['active']['eet_buy_price_policy']==PRICE_POLICY
    finally:controller.ledger.release(token)


def test_marker_is_durable_before_quote_lookup_and_submission():
    ex,store,broker,plan=passive_fixture()
    def get_quote(symbol):
        assert store.read()['active']['eet_buy_price_policy']==PRICE_POLICY
        assert any(kind=='start' and event['active']['eet_buy_price_policy']==PRICE_POLICY for kind,event in store.events)
        return quote('100','110') if symbol=='EET' else quote()
    ex.quote_getter=get_quote
    ex.run(plan)
    assert len(eet_rows(store))==1


@pytest.mark.parametrize('policy',[PRICE_POLICY,'future-policy'])
def test_price_marker_does_not_change_eet_sell_or_risk_exit_policy(policy):
    ex,store,broker,plan=passive_fixture()
    plan['eet_buy_price_policy']=policy
    store.state['portfolios']['a']['positions']={'EET':'1'};broker.qty={'EET':Decimal(1)}
    plan['intents']=[{'id':'sell','strategy':'a','symbol':'EET','side':'sell','qty':'1','fractionable':True}]
    ex.run(plan)
    assert eet_rows(store)[0]['limit_price']=='99.90'
    assert eet_rows(store)[0]['price_policy']=='iex-midpoint-v1'
    # A normal EET sell still defers on the same hard spread threshold.
    with pytest.raises(QuoteDeferred):
        execution_limit(quote('99','101'),'EET','sell',0,NOW,eet_buy_price_policy=policy)
    # Risk exits keep their independent legacy midpoint/concession rule.
    assert execution_limit(quote('99.6','100.4'),'EET','sell',0,NOW,True,eet_buy_price_policy=policy)==Decimal('99.60')


def test_daily_risk_off_cannot_resume_passive_eet_buy_or_reuse_unconfirmed_cash():
    time=[NOW]
    ex,store,broker,plan=passive_fixture(lambda symbol:quote('100','110',stamp=time[0].isoformat()) if symbol=='EET' else quote(stamp=time[0].isoformat()))
    plan['metadata']={'a':{'leg_states':{'EET':True}},'b':{}};ex.now=lambda:time[0]
    ex.run(plan)
    daily={'action':'daily','period':'2026-10-02','orders':[],'metadata':{'a':{'leg_states':{'EET':False}}}}
    cancel=broker.cancel;broker.cancel=lambda order_id:None
    result=ex.run(interrupt_builder=lambda state,broker:daily)
    assert result['status']=='pending' and result['reason']=='daily_risk_cancel_unconfirmed'
    assert store.read()['active']['daily_interruption']['status']=='needs_recovery'
    assert reservations(store.read()['active'],'a')>0
    assert store.read()['active']['intents'][0]['status']=='invalidated'
    assert ex.run()['reason']=='daily_recovery_required' and broker.submits==2
    broker.cancel=cancel
    assert ex.run(interrupt_builder=lambda state,broker:daily)['status']=='complete'
    suspended=store.read()['suspended_monthly']
    assert suspended['eet_buy_price_policy']==PRICE_POLICY
    assert all(row['status']=='canceled' for row in suspended['orders'])
    time[0]+=dt.timedelta(seconds=301)
    ex.run()
    assert store.read()['active']['id']==suspended['id']
    assert store.read()['active']['eet_buy_price_policy']==PRICE_POLICY
    assert len(eet_rows(store))==1
    assert store.read()['portfolios']['a']['cash']=='50'


def test_confirmed_daily_buy_fill_credits_fixed_passive_monthly_budget():
    ex,store,broker,plan=passive_fixture(lambda symbol:quote('100','110') if symbol=='EET' else quote())
    plan['metadata']={'a':{},'b':{}}
    ex.run(plan)
    daily={'action':'daily','period':'2026-10-02',
           'orders':[{'strategy':'a','symbol':'EET','side':'buy','qty':'.2','limit_price':'100'}],
           'metadata':{'a':{'leg_states':{'EET':True}}}}
    submit=broker.submit
    def filled_daily(row):
        order=submit(row)
        if row.get('intent_id') is None:
            order.update(status='filled',filled_qty=row['qty'],filled_avg_price='100')
            broker.qty['EET']=broker.qty.get('EET',Decimal(0))+Decimal(row['qty'])
            broker.cash-=Decimal(row['qty'])*100
        return order
    broker.submit=filled_daily
    assert ex.run(interrupt_builder=lambda state,broker:daily)['status']=='complete'
    suspended=store.read()['suspended_monthly']
    assert suspended['eet_buy_price_policy']==PRICE_POLICY
    assert Decimal(suspended['intents'][0]['external_booked_value'])==20
    assert store.read()['portfolios']['a']['cash']=='30.0'
    ex.now=lambda:NOW+dt.timedelta(seconds=301)
    ex.quote_getter=lambda symbol:quote('100','110',stamp=ex.now().isoformat()) if symbol=='EET' else quote(stamp=ex.now().isoformat())
    ex.run()
    latest=eet_rows(store)[-1]
    assert latest['limit_price']=='100.10'
    assert latest['price_policy']==PRICE_POLICY
    assert Decimal(latest['qty'])*Decimal(latest['limit_price'])<=30
    assert store.read()['active']['intents'][0]['budget']=='50'
