"""Bridge intraday fee withholding to delayed REG/TAF/CAT activity receipts.

Source: https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf
Revised September 17, 2026: exact fractional quantities, aggregate each fee type
daily per account, then round each type up to cents. The new policy starts on
October 2; earlier per-fill provisions and their settlement remain unchanged.
Never invent a cash correction: accrual requires owned fills and a matching
observed cash debit. No rates are inferred beyond December 31, 2026.
"""
from copy import deepcopy
import datetime as dt
from decimal import Decimal,ROUND_HALF_UP,ROUND_UP
from zoneinfo import ZoneInfo
from .ledger import SafetyStop,dec,text,totals
from .timestamps import parse_timestamp

DAILY_POLICY='alpaca-daily-2026-09-17-v1'
DAILY_START='2026-10-02'
DAILY_END='2026-12-31'
FEE_TYPES=('REG','TAF','CAT')
NY=ZoneInfo('America/New_York')


def _date(value):
    try:
        if not isinstance(value,str) or dt.date.fromisoformat(value).isoformat()!=value:
            raise ValueError('Expected ISO date')
    except (ValueError,TypeError) as exc:
        raise SafetyStop('Invalid regulatory fee date') from exc
    return value


def _fill_fact(fill,owners,portfolios):
    """Store immutable executed facts; ownership alone cannot hide bad data."""
    try:
        for key in ('id','order_id','side','symbol','transaction_time'):
            if not isinstance(fill[key],str) or not fill[key].strip():
                raise ValueError('Missing fill identity')
        stamp=parse_timestamp(fill['transaction_time']).astimezone(dt.timezone.utc)
        date=stamp.astimezone(NY).date().isoformat()
        owner=owners[fill['order_id']]
        if owner not in portfolios or fill['side'] not in {'buy','sell'}:
            raise ValueError('Invalid fill ownership or side')
        qty=dec(fill['qty']);price=dec(fill['price'])
        if qty<=0 or price<=0:raise ValueError('Non-positive fill')
    except (KeyError,ValueError,TypeError) as exc:
        raise SafetyStop('Missing, invalid or unowned regulatory fee fill') from exc
    if not ('2026-04-01'<=date<=DAILY_END):
        raise SafetyStop('Unsupported regulatory fee fill date')
    # Ignore insignificant trailing zeros without rounding exact fractional facts.
    def exact(value):
        out=text(value)
        return out.rstrip('0').rstrip('.') if '.' in out else out
    return {'date':date,'order_id':fill['order_id'],'strategy':owner,
            'side':fill['side'],'symbol':fill['symbol'],'qty':exact(qty),
            'price':exact(price),'transaction_time':stamp.isoformat()}


def _new_day():
    return {'policy':DAILY_POLICY,'fills':{},'raw':{k:'0' for k in FEE_TYPES},
            'target':{k:'0' for k in FEE_TYPES},
            'charged':{k:'0' for k in FEE_TYPES},
            'outstanding':{k:'0' for k in FEE_TYPES},'receipts':{}}


def _check_day(date,row):
    if not (DAILY_START<=_date(date)<=DAILY_END) or row.get('policy')!=DAILY_POLICY:
        raise SafetyStop('Unsupported daily regulatory fee policy')
    for key in ('target','charged','outstanding'):
        if set(row[key])!=set(FEE_TYPES) or any(dec(v)<0 for v in row[key].values()):
            raise SafetyStop('Invalid daily regulatory fee amounts')
    if any(dec(row['outstanding'][k])>dec(row['charged'][k]) for k in FEE_TYPES):
        raise SafetyStop('Daily regulatory fee provision exceeds charged amount')


def _daily_targets(row):
    proceeds=Decimal(0);shares=Decimal(0);sell_orders={}
    for fact in row['fills'].values():
        qty=dec(fact['qty']);shares+=qty
        if fact['side']=='sell':
            proceeds+=qty*dec(fact['price'])
            sell_orders[fact['order_id']]=sell_orders.get(fact['order_id'],Decimal(0))+qty
    raw={'REG':proceeds*dec('.00002060'),
         'TAF':sum((min(dec('9.79'),q*dec('.000195')) for q in sell_orders.values()),Decimal(0)),
         'CAT':shares*dec('.000003')}
    return ({k:text(v) for k,v in raw.items()},
            {k:text(v.quantize(dec('.01'),rounding=ROUND_UP)) for k,v in raw.items()})


def _receipt_proof(activity):
    try:
        aid=activity['id']
        if not isinstance(aid,str) or not aid.strip():raise ValueError('Missing ID')
        return {'date':_date(activity.get('date')),
                'activity_type':activity['activity_type'],
                'type':activity.get('activity_sub_type'),
                'amount':text(-dec(activity['net_amount']))}
    except (KeyError,ValueError,TypeError) as exc:
        raise SafetyStop('Invalid regulatory fee receipt') from exc


def _known_receipt(days,activity):
    """Audit replay before the generic activity-ID filter can hide a revision."""
    aid=activity.get('id')
    for date,row in days.items():
        _check_day(date,row)
        if aid in row['receipts']:
            proof=_receipt_proof(activity);recorded=row['receipts'][aid]
            if (any(recorded[k]!=v for k,v in proof.items() if k!='amount') or
                    dec(recorded['amount'])!=dec(proof['amount'])):
                raise SafetyStop('Conflicting regulatory fee receipt identity')
            return True
    return False


def accrue_account_fees(ledger,token,account):
    """Book a cash debit only when Alpaca's accrued-fee change explains it.

    The broker withholds these charges before its monthly INT/MGN receipt is
    available. An absent baseline or a mismatched debit remains a safety stop.
    """
    state=ledger.store.read()
    if state['env']!='live':return False
    tracker=state.get('account_fee_tracker')
    if not tracker or 'accrued_fees' not in account:return False
    current=dec(account['accrued_fees']);previous=dec(tracker['accrued_fees'])
    difference=dec(account['cash'])-totals(state)[1]
    if current<previous:
        if abs(difference)>dec('.02'):return False
        def reset(s):
            ledger.check(s,token)
            if s.get('account_fee_tracker')!=tracker:raise RuntimeError('Fee tracker changed')
            s['account_fee_tracker']['accrued_fees']=text(current)
        ledger.store.mutate('account_fee_cycle',reset)
        return True
    delta=current-previous
    if delta<=0 or difference>=-dec('.02'):return False
    charge=(-difference).quantize(dec('.01'),rounding=ROUND_HALF_UP)
    if charge<=0 or abs(charge-delta)>dec('.02') or abs(difference+charge)>dec('.02'):
        return False
    def accrue(s):
        ledger.check(s,token)
        if s.get('account_fee_tracker')!=tracker or totals(s)[1]!=totals(state)[1]:
            raise RuntimeError('Account fee accrual state changed')
        reserve=s['portfolios']['reserve'];net=dec(reserve['cash'])-dec(reserve['debt'])-charge
        reserve['cash']=text(max(Decimal(0),net));reserve['debt']=text(max(Decimal(0),-net))
        s['account_fee_tracker']['accrued_fees']=text(current)
        s['account_fee_tracker']['pending']=text(dec(tracker['pending'])+charge)
        s['account_fee_tracker'].setdefault('cash_debits',[]).append(
            {'amount':text(charge),'accrued_fees_from':text(previous),
             'accrued_fees_to':text(current),'broker_cash':text(account['cash'])})
    ledger.store.mutate('account_fee_accrual',accrue)
    return True


def settle_account_fee(state,activity):
    """Avoid booking the monthly margin-interest receipt a second time."""
    amount=dec(activity['net_amount'])
    tracker=state.get('account_fee_tracker')
    if (not tracker or activity.get('activity_type')!='INT' or
            activity.get('activity_sub_type')!='MGN' or amount>=0):return amount
    covered=min(-amount,dec(tracker['pending']))
    tracker['pending']=text(dec(tracker['pending'])-covered)
    if covered:tracker.setdefault('receipt_ids',[]).append(activity['id'])
    return amount+covered


def _accrue_legacy_regulatory_fees(ledger,token,activities,broker_cash):
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


def accrue_regulatory_fees(ledger,token,activities,broker_cash):
    """Accrue only the documented cumulative daily fee still absent from cash.

    Facts survive overlap expiry and sub-cent cash noise. A later partial fill
    increases a daily target, never starts another rounding bucket. Actual fee
    receipts count as charges too, including receipts seen before these fills.
    """
    state=ledger.store.read()
    if state['env']!='live':return False
    owners=state.get('order_owners',{});portfolios=state['portfolios']
    previous=state.get('regulatory_fee_days',{})
    days=deepcopy(previous);seen={}
    order_days={}
    for date,row in days.items():
        _check_day(date,row)
        for aid,fact in row['fills'].items():
            if fact['date']!=date or owners.get(fact['order_id'])!=fact['strategy']:
                raise SafetyStop('Regulatory fee fill ownership or date changed')
            if aid in seen and seen[aid]!=fact:
                raise SafetyStop('Conflicting regulatory fee fill identity')
            seen[aid]=fact
            order_days.setdefault(fact['order_id'],date)
            if order_days[fact['order_id']]!=date:
                raise SafetyStop('Regulatory fee fill spans trading days')
    legacy_seen={fid for a in state.get('fee_accruals',[]) for fid in a['fill_ids']}
    legacy=[]
    for fill in activities:
        if _known_receipt(days,fill):continue
        if (fill.get('activity_type') in {'FEE','CFEE'} and
                fill.get('activity_sub_type') in FEE_TYPES):
            proof=_receipt_proof(fill);date=proof['date']
            if date>=DAILY_START:
                if date>DAILY_END or dec(proof['amount'])<0:
                    raise SafetyStop('Unsupported regulatory fee receipt date or reversal')
                if any(a['date']==date for a in state.get('fee_accruals',[])):
                    raise SafetyStop('Daily receipt has ambiguous legacy provision coverage')
                if fill['id'] not in state.get('activity_ids',[]):
                    raise SafetyStop('Regulatory fee receipt has not been ingested')
                # Old sync_activities posted this receipt directly when there
                # was no provision. Import proof of that debit, never debit again.
                _settle_daily_fee({'regulatory_fee_days':days},fill,date,
                                  proof['type'],-dec(proof['amount']))
                days[date]['receipts'][fill['id']]['already_booked_activity']=True
        if fill.get('activity_type')!='FILL':continue
        # Existing legacy rows lack full immutable facts; preserve their replay.
        if fill.get('id') in legacy_seen:continue
        fact=_fill_fact(fill,owners,portfolios);aid=fill['id'];date=fact['date']
        if aid in seen:
            if seen[aid]!=fact:raise SafetyStop('Conflicting regulatory fee fill identity')
            continue
        if date<DAILY_START:
            legacy.append(fill);continue
        order_days.setdefault(fact['order_id'],date)
        if order_days[fact['order_id']]!=date:
            raise SafetyStop('Regulatory fee fill spans trading days')
        seen[aid]=fact
        days.setdefault(date,_new_day())['fills'][aid]=fact
    if not days:
        return _accrue_legacy_regulatory_fees(ledger,token,legacy,broker_cash)
    # Do not guess which policy explains a mixed old/new unbooked debit.
    if any(a['side']=='sell' for a in legacy):
        raise SafetyStop('Unbooked legacy fills mixed with daily regulatory fees')
    increments={}
    for date,row in days.items():
        row['raw'],row['target']=_daily_targets(row)
        increments[date]={k:max(Decimal(0),dec(row['target'][k])-dec(row['charged'][k]))
                          for k in FEE_TYPES}
    fee=sum((v for amounts in increments.values() for v in amounts.values()),Decimal(0))
    difference=dec(broker_cash)-totals(state)[1]
    matched=fee>0 and difference<-dec('.02') and abs(difference+fee)<=dec('.02')
    if matched:
        for date,amounts in increments.items():
            row=days[date]
            for typ,amount in amounts.items():
                row['charged'][typ]=text(dec(row['charged'][typ])+amount)
                row['outstanding'][typ]=text(dec(row['outstanding'][typ])+amount)
            if any(amounts.values()):
                row.setdefault('cash_debits',[]).append(
                    {'amount':text(sum(amounts.values(),Decimal(0))),
                     'by_type':{k:text(v) for k,v in amounts.items()},
                     'broker_cash':text(broker_cash),'cash_difference':text(difference)})
    if days!=previous:
        def persist(s):
            ledger.check(s,token)
            if (s.get('regulatory_fee_days',{})!=previous or
                    s.get('order_owners',{})!=owners or totals(s)[1]!=totals(state)[1]):
                raise SafetyStop('Daily regulatory fee accrual state changed')
            if matched:
                reserve=s['portfolios']['reserve']
                net=dec(reserve['cash'])-dec(reserve['debt'])-fee
                reserve['cash']=text(max(Decimal(0),net))
                reserve['debt']=text(max(Decimal(0),-net))
            s['regulatory_fee_days']=days
        ledger.store.mutate('daily_regulatory_fee_accrual' if matched else 'daily_regulatory_fee_evidence',persist)
    return matched


def _settle_daily_fee(state,activity,date,subtype,amount):
    days=state.setdefault('regulatory_fee_days',{})
    aid=activity.get('id');proof=_receipt_proof(activity)
    if _known_receipt(days,activity):return Decimal(0)
    if any(a['date']==date for a in state.get('fee_accruals',[])):
        raise SafetyStop('Daily receipt has ambiguous legacy provision coverage')
    if any(aid in row['fills'] for row in days.values()):
        raise SafetyStop('Regulatory fee receipt reuses a fill identity')
    row=days.setdefault(date,_new_day())
    covered=min(-amount,dec(row['outstanding'][subtype]))
    uncovered=-amount-covered
    row['outstanding'][subtype]=text(dec(row['outstanding'][subtype])-covered)
    row['charged'][subtype]=text(dec(row['charged'][subtype])+uncovered)
    row['receipts'][aid]=proof|{'covered':text(covered),'uncovered':text(uncovered)}
    # Lower receipts do not prove finality. Keep any remaining provision visible.
    return -uncovered


def settle_fee(state,activity):
    """Return cash amount not already provisioned; retain a receipt trail."""
    amount=dec(activity['net_amount']);date=activity.get('date');subtype=activity.get('activity_sub_type')
    if _known_receipt(state.get('regulatory_fee_days',{}),activity):return Decimal(0)
    if subtype not in FEE_TYPES:return amount
    date=_date(date)
    if date>=DAILY_START:
        if date>DAILY_END or amount>0:
            raise SafetyStop('Unsupported regulatory fee receipt date or reversal')
        return _settle_daily_fee(state,activity,date,subtype,amount)
    if amount>=0:return amount
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
