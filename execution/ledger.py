"""Account ledger. All monetary values are decimal strings, never binary floats.

The account document is the serialization point. Each successful mutation also
creates an immutable revision snapshot in the SAME Firestore transaction.
Broker I/O never runs in a transaction callback.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
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


def reconcile_state(state, positions, cash):
    expected, expected_cash = totals(state)
    actual = {p['symbol']: dec(p['qty']) for p in positions}
    diffs = {s: text(actual.get(s, ZERO)-expected.get(s, ZERO)) for s in set(actual)|set(expected)
             if abs(actual.get(s, ZERO)-expected.get(s, ZERO)) > Decimal('0.000000001')}
    if diffs:
        raise SafetyStop(f'Unexplained broker quantities: {diffs}')
    # Alpaca account cash is cent-rounded; fills use sub-cent average prices.
    if abs(dec(cash)-expected_cash) > Decimal('0.02'):
        raise SafetyStop(f'Unexplained broker cash: {dec(cash)-expected_cash}')
    return {'positions': {s:text(q) for s,q in expected.items()},
            'cash': text(expected_cash), 'cash_rounding':text(dec(cash)-expected_cash)}


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
        from google.cloud import firestore
        @firestore.transactional
        def update(tx):
            snap=self.ref.get(transaction=tx)
            if not snap.exists: raise SafetyStop('Missing ledger')
            state=snap.to_dict()
            result=fn(state)
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
            if s['last_completed'].get(plan['action'],'')>=plan['period']: return
            p=deepcopy(plan)
            p['id']=hashlib.sha256(f"{s['account_id']}|{s['env']}|{p['action']}|{p['period']}".encode()).hexdigest()[:32]
            p['created_at']=utcnow()
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

    def mark_submitting(self,token,index):
        def mark(s):
            self.check(s,token); row=s['active']['orders'][index]
            if row['status']!='planned': raise SafetyStop('Intent already claimed')
            p=s['portfolios'][row['strategy']]
            if row['side']=='sell' and dec(row['qty'])>dec(p['positions'].get(row['symbol'],0)):
                raise SafetyStop('Insufficient owned shares')
            if row['side']=='buy' and dec(row['qty'])*dec(row['limit_price'])>dec(p['cash']):
                raise SafetyStop('Insufficient strategy cash; confirmed proceeds only')
            row['status']='submitting';row['submitted_at']=utcnow()
            return deepcopy(row)
        return self.store.mutate('intent',mark)

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
            s['last_completed'][p['action']]=p['period']
            if p['action']=='monthly' and p.get('margin_retry'):
                from zoneinfo import ZoneInfo
                tomorrow=dt.datetime.now(ZoneInfo('America/New_York')).date()+dt.timedelta(days=1)
                s['monthly_retry']={'period':p['period'],'after':tomorrow.isoformat()}
            elif p['action'] in ('monthly','monthly-funding'):
                s.pop('monthly_retry',None)
            if s.get('pending_actions',{}).get(p['action'])==p['period']:
                s['pending_actions'].pop(p['action'])
            p['completed_at']=utcnow(); s['active']=None
            return p
        return self.store.mutate('complete',complete)
