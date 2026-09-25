"""Bridge intraday fee withholding to delayed REG/TAF/CAT activity receipts.

Source: https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf
June 2026 schedule: SEC .00002060 of proceeds, TAF .000195/share,
rounded up cents. Never invent a cash correction: accrual requires confirmed
sell fills AND a matching observed cash decrement. Rates outside 2026 fail closed.
"""
from decimal import Decimal,ROUND_UP
from .ledger import dec,text,totals


def accrue_regulatory_fees(ledger,token,activities,broker_cash):
    state=ledger.store.read()
    if state['env']!='live': return False
    difference=dec(broker_cash)-totals(state)[1]
    if difference>=-dec('.02'):return False
    seen={fid for a in state.get('fee_accruals',[]) for fid in a['fill_ids']}
    owners=state.get('order_owners',{});groups={}
    for fill in activities:
        if fill.get('activity_type')!='FILL' or fill.get('side')!='sell' or fill['id'] in seen:continue
        date=fill['transaction_time'][:10]
        if not ('2026-04-01'<=date<='2026-12-31') or fill.get('order_id') not in owners:return False
        q=dec(fill['qty']);price=dec(fill['price'])
        amount=(q*price*dec('.00002060')).quantize(dec('.01'),rounding=ROUND_UP)
        amount+=min(dec('9.79'),(q*dec('.000195')).quantize(dec('.01'),rounding=ROUND_UP))
        row=groups.setdefault(fill['order_id'],{'order_id':fill['order_id'],'strategy':owners[fill['order_id']],
                                                'date':date,'amount':'0','fill_ids':[],'source':'2026 SEC/TAF schedule + observed broker cash'})
        row['amount']=text(dec(row['amount'])+amount);row['fill_ids'].append(fill['id'])
    fee=sum(dec(g['amount']) for g in groups.values())
    if not fee or abs(difference+fee)>dec('.02'):return False
    def accrue(s):
        ledger.check(s,token)
        if totals(s)[1]!=totals(state)[1]:raise RuntimeError('Fee accrual state changed')
        for group in groups.values():
            p=s['portfolios'][group['strategy']];amount=dec(group['amount'])
            if dec(p['cash'])<amount:raise RuntimeError('Fee exceeds own sale proceeds')
            p['cash']=text(dec(p['cash'])-amount)
            group['remaining']=group['amount']
            s.setdefault('fee_accruals',[]).append(group)
    ledger.store.mutate('regulatory_fee_accrual',accrue)
    return True


def settle_fee(state,activity):
    """Return cash amount not already provisioned; retain a receipt trail."""
    amount=dec(activity['net_amount']);date=activity.get('date');subtype=activity.get('activity_sub_type')
    if amount>=0 or subtype not in {'REG','TAF','CAT'}:return amount
    outstanding=[a for a in state.get('fee_accruals',[]) if a['date']==date]
    if not outstanding:return amount
    for accrual in outstanding:
        matched=min(-amount,dec(accrual['remaining']))
        accrual['remaining']=text(dec(accrual['remaining'])-matched);amount+=matched
        if matched:accrual.setdefault('receipt_ids',[]).append(activity['id'])
    types=state.setdefault('fee_settlement_types',{}).setdefault(date,[])
    if subtype not in types:types.append(subtype)
    if set(types)>={'REG','TAF','CAT'}:
        for accrual in outstanding:
            p=state['portfolios'][accrual['strategy']]
            p['cash']=text(dec(p['cash'])+dec(accrual['remaining']))
            accrual['remaining']='0';accrual['settled']=True
    return amount
