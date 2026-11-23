"""Account ledger. All monetary values are decimal strings, never binary floats.

The account document is the serialization point. Each successful mutation also
creates an immutable revision snapshot in the SAME Firestore transaction.
Broker I/O never runs in a transaction callback.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import datetime as dt
import hashlib
import time
import uuid

ZERO = Decimal('0')
TERMINAL = {'filled', 'canceled', 'expired', 'rejected'}

class SafetyStop(RuntimeError):
    pass


def dec(value):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise SafetyStop('Invalid decimal') from exc
    if not d.is_finite():
        raise SafetyStop('Non-finite decimal')
    return d


def text(d):
    return format(dec(d), 'f')


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def new_state(account_id, env, holdings, cash, debt, started_at, metadata=None):
    keys = set(holdings) | set(cash) | set(debt) | {'reserve'}
    return {'schema': 1, 'account_id': account_id, 'env': env,
            'started_at': started_at, 'revision': 0, 'lease': None,
            'active': None, 'last_completed': {}, 'halted': False,
            'activity_ids': [], 'order_owners': {}, 'activity_through': started_at,
            'portfolios': {k: {'positions': {s: text(q) for s,q in holdings.get(k, {}).items()},
                               'cash': text(cash.get(k, 0)), 'debt': text(debt.get(k, 0)),
                               'basis': {}, 'realized': '0', 'net_contributions': '0',
                               'metadata': deepcopy((metadata or {}).get(k, {}))}
                           for k in keys}}


def totals(state):
    positions, cash = {}, ZERO
    for p in state['portfolios'].values():
        cash += dec(p['cash']) - dec(p['debt'])
        for s,q in p['positions'].items():
            positions[s] = positions.get(s, ZERO) + dec(q)
    return positions, cash


def pending_regulatory_fees(state):
    """Modeled daily REG/TAF/CAT not yet charged against the ledger (>= 0)."""
    total=ZERO
    for row in (state.get('regulatory_fee_days') or {}).values():
        for typ,target in (row.get('target') or {}).items():
            total+=max(ZERO,dec(target)-dec((row.get('charged') or {}).get(typ,0)))
    return total


def reconcile_state(state, positions, cash):
    expected, expected_cash = totals(state)
    actual = {p['symbol']: dec(p['qty']) for p in positions}
    diffs = {s: text(actual.get(s, ZERO)-expected.get(s, ZERO)) for s in set(actual)|set(expected)
             if abs(actual.get(s, ZERO)-expected.get(s, ZERO)) > Decimal('0.000000001')}
    if diffs:
        raise SafetyStop(f'Unexplained broker quantities: {diffs}')
    # Compare reported balances at the broker's cent precision. Keep the exact
    # ledger untouched: normalizing the comparison is not a cash adjustment.
    broker_cash=dec(cash)
    if broker_cash!=broker_cash.quantize(Decimal('.01')):
        raise SafetyStop('Broker cash is not cent-rounded')
    reported_cash=expected_cash.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
    difference=broker_cash-reported_cash
    # Alpaca withholds REG/TAF/CAT intraday by its own rounding; the documented
    # daily model can miss it by cents (2026-11-23: 93 WLDU sold, model 0.06,
    # withheld ~0.04, so the fee accrual did not match and the run stopped
    # before the SPXL exit). A broker shortfall up to the still-unbooked modeled
    # fee is that withholding; the receipt books it when it arrives.
    pending=pending_regulatory_fees(state)
    if difference > Decimal('0.02') or difference < -(Decimal('0.02')+pending):
        raise SafetyStop(f'Unexplained broker cash: {broker_cash-expected_cash}')
    return {'positions': {s:text(q) for s,q in expected.items()},
            'cash': text(expected_cash), 'cash_rounding':text(broker_cash-expected_cash),
            'cash_reported':text(reported_cash),'cash_difference_cents':text(difference)}


def apply_order(state, row, order):
    """Apply a cumulative execution once; terminal partial fills are still fills."""
    for key in ('symbol','side','client_order_id'):
        if order.get(key) != row[key]:
            raise SafetyStop(f'Order identity mismatch: {key}')
    if dec(order['qty']) != dec(row['qty']):
        raise SafetyStop('Broker order quantity differs from persisted intent')
    q, oldq = dec(order.get('filled_qty',0)), dec(row.get('booked_qty',0))
    value = q * dec(order.get('filled_avg_price') or 0)
    oldvalue = dec(row.get('booked_value',0))
    if q < oldq or q > dec(row['qty']) or (q > 0 and value <= 0):
        raise SafetyStop('Non-monotonic or invalid cumulative fill')
    if q == oldq and value != oldvalue:
        raise SafetyStop('Broker revised an already booked fill')
    dq,dv = q-oldq,value-oldvalue
    p = state['portfolios'][row['strategy']]
    sym = row['symbol']; have = dec(p['positions'].get(sym,0))
    basis = dec(p.get('basis',{}).get(sym,0))
    if row['side'] == 'sell':
        if dq > have:
            raise SafetyStop('Fill exceeds strategy-owned quantity')
        removed_basis = basis*dq/have if have else ZERO
        p['positions'][sym] = text(have-dq)
        p['cash'] = text(dec(p['cash'])+dv)
        p['basis'][sym] = text(basis-removed_basis)
        p['realized'] = text(dec(p['realized'])+dv-removed_basis)
    else:
        if dv > dec(p['cash'])+Decimal('0.000001'):
            raise SafetyStop('Buy fill exceeds reserved strategy cash')
        p['positions'][sym] = text(have+dq)
        p['cash'] = text(dec(p['cash'])-dv)
        p['basis'][sym] = text(basis+dv)
    state.setdefault('order_owners', {})[order['id']] = row['strategy']
    row.update(booked_qty=text(q), booked_value=text(value), broker_id=order['id'],
               status=order['status'], broker_updated_at=str(order.get('updated_at') or ''))


class FirestoreStore:
    def __init__(self, db, account_id, env):
        self.db=db
        self.ref=db.collection(f'execution-accounts-{env}').document(account_id)

    def read(self):
        doc=self.ref.get()
        if not doc.exists:
            raise SafetyStop('Account ledger not initialized; migration required')
        return doc.to_dict()

    def initialize(self, state):
        from google.cloud import firestore
        @firestore.transactional
        def create(tx):
            if self.ref.get(transaction=tx).exists:
                raise SafetyStop('Account ledger already exists')
            tx.create(self.ref,state)
            tx.create(self.ref.collection('events').document('000000000000'),
                      {'kind':'bootstrap','at':utcnow(),'state':state})
        create(self.db.transaction())

    def mutate(self, kind, fn):
        from google.api_core import exceptions
        seen={}
        for attempt in (1,2):
            try:
                return self._mutate_once(kind,fn,seen)
            except exceptions.InvalidArgument as exc:
                # The commit RPC retry resends the same transaction id; if the
                # server already ended it, Firestore answers "expired or no longer
                # valid" (2026-11-11 16:05 UTC, lease acquire). Every commit bumps
                # the revision, so an unchanged revision proves nothing landed and
                # the transaction may run once more. Anything else stays a stop.
                message=str(exc).lower()
                if attempt==2 or 'revision' not in seen or 'transaction' not in message \
                        or not ('expired' in message or 'no longer valid' in message):
                    raise
                snap=self.ref.get()
                if not snap.exists or snap.to_dict().get('revision',0)!=seen['revision']:
                    raise

    def _mutate_once(self, kind, fn, seen):
        from google.cloud import firestore
        @firestore.transactional
        def update(tx):
            # Only a callback that ran to completion leaves a revision behind:
            # an "expired" from the rollback after a stop is no commit failure.
            seen.clear()
            snap=self.ref.get(transaction=tx)
            if not snap.exists: raise SafetyStop('Missing ledger')
            state=snap.to_dict()
            read=state.get('revision',0)
            result=fn(state)
            seen['revision']=read
            state['revision']=state.get('revision',0)+1
            state['updated_at']=utcnow()
            tx.set(self.ref,state)
            tx.create(self.ref.collection('events').document(f"{state['revision']:012d}"),
                      {'kind':kind,'at':state['updated_at'],'state':state})
            if kind=='complete' and result:
                tx.create(self.ref.collection('runs').document(result['id']),result)
            return result
        return update(self.db.transaction())


class Ledger:
    def __init__(self, store, clock=time.time):
        self.store=store; self.clock=clock

    def acquire(self):
        token=uuid.uuid4().hex
        def claim(s):
            if s.get('halted'): raise SafetyStop('Account trading is halted')
            lease=s.get('lease')
            if lease and lease['until'] > self.clock(): raise SafetyStop('Another executor owns this account')
            s['lease']={'token':token,'until':self.clock()+180}
        self.store.mutate('acquire',claim)
        return token

    def check(self,s,token):
        lease=s.get('lease') or {}
        if s.get('halted') or lease.get('token')!=token or lease.get('until',0)<=self.clock():
            raise SafetyStop('Executor lease expired or fenced')
        lease['until']=self.clock()+180

    def release(self,token):
        def release(s):
            if (s.get('lease') or {}).get('token')==token: s['lease']=None
        self.store.mutate('release',release)

    def start(self,token,plan):
        def start(s):
            self.check(s,token)
            if s['active']:
                if (s['active']['action'],s['active']['period'])!=(plan['action'],plan['period']):
                    raise SafetyStop('Prior plan must finish before another can start')
                return
            if max(s['last_completed'].get(plan['action'],''),s.get('last_expired',{}).get(plan['action'],''))>=plan['period']: return
            p=deepcopy(plan)
            seed=f"{s['account_id']}|{s['env']}|{p['action']}|{p['period']}"
            # A plan abandoned earlier the same period already owns the base
            # client_order_ids; a rebuilt plan must not reuse them.
            attempt=s.get('abandoned_periods',{}).get(f"{p['action']}|{p['period']}",0)
            if attempt:seed+=f"|retry{attempt}"
            p['id']=hashlib.sha256(seed.encode()).hexdigest()[:32]
            p['created_at']=utcnow()
            if p.get('execution_policy')=='monthly-iex-v1':
                p['valuation_order_preview']=p['orders']
                p['orders']=[]
            for i,row in enumerate(p['orders']):
                port=s['portfolios'][row['strategy']]
                if row['side'] not in ('sell','buy') or dec(row['qty'])<=0 or dec(row['limit_price'])<=0:
                    raise SafetyStop('Invalid order intent')
                if row['side']=='sell' and dec(row['qty'])>dec(port['positions'].get(row['symbol'],0)):
                    raise SafetyStop('Order exceeds strategy-owned quantity')
                row.update(client_order_id=f"se-{p['id']}-{i:03d}",status='planned',booked_qty='0',booked_value='0')
            reserve=s['portfolios']['reserve']
            margin=dec(p.get('margin_budget',0))
            for key,value in p.get('funding',{}).items():
                amt=dec(value)
                if amt<0: raise SafetyStop('Negative funding')
                port=s['portfolios'][key]
                borrow=dec(p['funding_debt'][key]) if 'funding_debt' in p else max(ZERO,amt-dec(reserve['cash']))
                existing=amt-borrow
                if borrow<0 or existing<0 or existing>dec(reserve['cash']):
                    raise SafetyStop('Invalid cash/debt funding allocation')
                if borrow>margin: raise SafetyStop('Funding exceeds approved account budget')
                margin-=borrow
                reserve['cash']=text(dec(reserve['cash'])-existing)
                port['cash']=text(dec(port['cash'])+amt)
                port['debt']=text(dec(port['debt'])+borrow)
                port['net_contributions']=text(dec(port['net_contributions'])+amt-borrow)
            s['active']=p
        return self.store.mutate('start',start)

    def abandon_unsubmitted(self,token,reason):
        """Close a non-monthly plan whose remaining rows were never submitted.

        Filled rows stay booked exactly as they are (positions and cash already
        carry them). Unsubmitted rows are dropped, plan metadata is NOT applied:
        the next daily build sees the real holdings and replans from scratch.
        2026-11-23: a 4-cent fee mismatch stopped the plan after the WLDU fill;
        the planned SPXL exit then went stale and would have blocked every later
        run of every sleeve."""
        def abandon(s):
            self.check(s,token);p=s['active']
            if not p or p.get('execution_policy')=='monthly-iex-v1':
                raise SafetyStop('Only a daily-style plan can be abandoned')
            rest=[r for r in p['orders'] if r['status']!='filled']
            if any(r['status']!='planned' for r in rest):
                raise SafetyStop('Plan has submitted or terminal orders; cannot abandon')
            key=f"{p['action']}|{p['period']}"
            periods=s.setdefault('abandoned_periods',{});periods[key]=periods.get(key,0)+1
            log=s.setdefault('abandoned_plans',[])
            log.append({'id':p['id'],'action':p['action'],'period':p['period'],'reason':reason,
                        'abandoned_at':utcnow(),'filled':[r['client_order_id'] for r in p['orders'] if r['status']=='filled'],
                        'dropped':[{k:r[k] for k in ('strategy','symbol','side','qty')} for r in rest]})
            del log[:-20]
            s['active']=None
            return deepcopy(log[-1])
        return self.store.mutate('abandon_unsubmitted',abandon)

    def mark_submitting(self,token,index,submitted_at=None):
        def mark(s):
            self.check(s,token); row=s['active']['orders'][index]
            if row['status']!='planned': raise SafetyStop('Intent already claimed')
            p=s['portfolios'][row['strategy']]
            if row['side']=='sell' and dec(row['qty'])>dec(p['positions'].get(row['symbol'],0)):
                raise SafetyStop('Insufficient owned shares')
            if row['side']=='buy' and dec(row['qty'])*dec(row['limit_price'])>dec(p['cash']):
                raise SafetyStop('Insufficient strategy cash; confirmed proceeds only')
            row['status']='submitting';row['submitted_at']=submitted_at or utcnow()
            return deepcopy(row)
        return self.store.mutate('intent',mark)

    def reject(self,token,index,evidence):
        def rejected(state):
            self.check(state,token);row=state['active']['orders'][index]
            if row['status']!='submitting' or dec(row.get('booked_qty',0))!=0:
                raise SafetyStop('Rejection proof does not match an unfilled submission claim')
            row.update(status='rejected',no_order_proven=True,rejection=deepcopy(evidence))
        self.store.mutate('broker_order_rejected',rejected)

    def record(self,token,index,order):
        def record(s):
            self.check(s,token); apply_order(s,s['active']['orders'][index],order)
        self.store.mutate('broker_order',record)

    def transfer(self,token):
        def transfer(s):
            self.check(s,token); plan=s['active']
            if plan.get('transfers_done'): return
            transfers=plan.get('transfers',{})
            if abs(sum(dec(v) for v in transfers.values()))>Decimal('0.000000001'):
                raise SafetyStop('Transfers do not conserve cash')
            for key,value in transfers.items():
                p=s['portfolios'][key]; amount=dec(value)
                if dec(p['cash'])+amount<0: raise SafetyStop('Annual transfer exceeds confirmed proceeds')
                p['cash']=text(dec(p['cash'])+amount)
                p['net_contributions']=text(dec(p['net_contributions'])+amount)
            plan['transfers_done']=True
        self.store.mutate('transfers',transfer)

    def complete(self,token):
        def complete(s):
            self.check(s,token); p=s['active']
            if any(r['status']!='filled' for r in p['orders']):
                raise SafetyStop('Plan has unfinished or incompletely executed orders')
            for key,updates in p.get('metadata',{}).items():
                s['portfolios'][key]['metadata'].update(updates)
            if p['action']=='daily' and s.get('suspended_monthly'):
                s['suspended_monthly']['daily_interruption']={'status':'recovered','period':p['period'],'recovered_at':utcnow()}
                # Daily trades may consume already-funded monthly cash or fulfill
                # an outstanding rotation. Credit actual fills against the fixed
                # intent cap; do not allocate any fresh monthly funding.
                for row in p['orders']:
                    for intent in s['suspended_monthly'].get('intents',[]):
                        if (intent['strategy'],intent['symbol'],intent['side'])==(row['strategy'],row['symbol'],row['side']):
                            for field in ('booked_qty','booked_value'):
                                key='external_'+field
                                intent[key]=text(dec(intent.get(key,0))+dec(row.get(field,0)))
            s['last_completed'][p['action']]=p['period']
            if p['action']=='monthly' and p.get('margin_retry'):
                from zoneinfo import ZoneInfo
                tomorrow=dt.datetime.now(ZoneInfo('America/New_York')).date()+dt.timedelta(days=1)
                s['monthly_retry']={'period':p['period'],'after':tomorrow.isoformat()}
            elif p['action'] in ('monthly','monthly-funding'):
                s.pop('monthly_retry',None)
            if s.get('pending_actions',{}).get(p['action'])==p['period']:
                s['pending_actions'].pop(p['action'])
            p['completed_at']=utcnow()
            from .notifications import queue_result
            queue_result(s,p,'complete')
            s['active']=None
            return p
        return self.store.mutate('complete',complete)
