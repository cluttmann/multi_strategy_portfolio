from decimal import Decimal
import pytest
from test_execution import MemoryStore,seeded
from execution.ledger import Ledger,totals,SafetyStop
from execution import fees
from execution.fees import accrue_regulatory_fees
from execution.broker import sync_activities

FILLS=[{'id':'a','activity_type':'FILL','transaction_time':'2026-09-25T19:56:59Z','order_id':'o','side':'sell','symbol':'EEM','qty':'44','price':'67.9151'},
       {'id':'b','activity_type':'FILL','transaction_time':'2026-09-25T19:56:59Z','order_id':'o','side':'sell','symbol':'EEM','qty':'0.741255','price':'67.9151'}]

def prepared():
    s=seeded();s['env']='live';s['order_owners']={'o':'mix8'};s['portfolios']['mix8']['cash']='3038.6068074505'
    st=MemoryStore(s);l=Ledger(st);t=l.acquire();return st,l,t

def test_observed_sell_fee_is_accrued_once_only_when_cash_matches():
    st,l,t=prepared();cash=totals(st.read())[1]-Decimal('.0968074505')
    assert accrue_regulatory_fees(l,t,FILLS,cash)
    assert st.read()['portfolios']['mix8']['cash']=='3038.5068074505'
    assert not accrue_regulatory_fees(l,t,FILLS,cash)

def test_unrelated_cash_difference_is_never_classified_as_a_fee():
    st,l,t=prepared();cash=totals(st.read())[1]-Decimal('3')
    assert not accrue_regulatory_fees(l,t,FILLS,cash)
    assert not st.read().get('fee_accruals')

def test_posted_fee_is_not_double_booked_and_unused_provision_is_released():
    st,l,t=prepared();cash=totals(st.read())[1]-Decimal('.10')
    accrue_regulatory_fees(l,t,FILLS,cash)
    events=[{'id':'fee-'+typ,'activity_type':'FEE','activity_sub_type':typ,'date':'2026-09-25','created_at':'2026-09-26T00:40:00Z','net_amount':amt}
            for typ,amt in [('REG','-0.07'),('TAF','-0.01'),('CAT','-0.01')]]
    sync_activities(l,t,events);sync_activities(l,t,events)
    assert totals(st.read())[1]==Decimal('3138.5168074505')
    assert st.read()['fee_accruals'][0]['remaining']=='0'


def test_daily_accrued_fee_debit_is_booked_once_against_observed_cash():
    st,l,t=prepared()
    def seed(s):
        s['portfolios']['reserve']['cash']='0'
        s['account_fee_tracker']={'accrued_fees':'5.239978819444630009','pending':'5.24'}
    st.mutate('seed',seed)
    cash=totals(st.read())[1]-Decimal('0.2425978659')
    account={'cash':str(cash),'accrued_fees':'5.477286597222466201'}
    assert fees.accrue_account_fees(l,t,account)
    assert st.read()['portfolios']['reserve']['debt']=='0.24'
    assert st.read()['account_fee_tracker']['pending']=='5.48'
    assert not fees.accrue_account_fees(l,t,account)
    assert st.read()['portfolios']['reserve']['debt']=='0.24'


def test_unmatched_accrued_fee_change_still_stops_cash_reconciliation():
    st,l,t=prepared()
    st.mutate('seed',lambda s:s.update(account_fee_tracker={'accrued_fees':'5.23','pending':'5.23'}))
    account={'cash':str(totals(st.read())[1]-Decimal('2.24')),'accrued_fees':'5.47'}
    assert not fees.accrue_account_fees(l,t,account)
    assert st.read()['portfolios']['reserve']['debt']=='0'
    assert st.read()['account_fee_tracker']['accrued_fees']=='5.23'


def test_monthly_margin_interest_receipt_consumes_existing_provision():
    st,l,t=prepared()
    st.mutate('seed',lambda s:s.update(account_fee_tracker={'accrued_fees':'5.48','pending':'5.48'}))
    before=totals(st.read())[1]
    activity={'id':'margin-interest','activity_type':'INT','activity_sub_type':'MGN',
              'date':'2026-10-01','net_amount':'-5.48'}
    sync_activities(l,t,[activity])
    sync_activities(l,t,[activity])
    assert totals(st.read())[1]==before
    assert Decimal(st.read()['account_fee_tracker']['pending'])==0
