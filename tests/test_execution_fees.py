from decimal import Decimal
import pytest
from test_execution import MemoryStore,seeded
from execution.ledger import Ledger,totals,SafetyStop
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
