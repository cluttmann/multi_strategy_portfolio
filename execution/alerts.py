"""Persisted, transition-based alerts for the shared account executor."""
from decimal import Decimal,ROUND_HALF_UP


def incident_key(error):
    message=str(error)
    if message.startswith('Unexplained broker cash:'):
        return 'broker_cash_mismatch'
    return f'{type(error).__name__}:{message}'


def failure_transition(previous,error):
    key=incident_key(error)
    same=previous.get('active') and previous.get('key')==key
    return {'active':True,'key':key,'count':previous.get('count',0)+1 if same else 1},not same


def success_transition(previous):
    recovered=bool(previous.get('active'))
    return {'active':False,'key':None,'count':0},recovered


def failure_message(error):
    message=str(error)
    if message.startswith('Unexplained broker cash:'):
        try:
            amount=abs(Decimal(message.split(':',1)[1].strip())).quantize(
                Decimal('.01'),rounding=ROUND_HALF_UP)
            return f'❗ Shared ETF executor stopped: broker cash differs from ledger by ${amount:.2f}. Trading is blocked until reconciled.'
        except Exception:
            pass
    return f'❗ Shared ETF executor stopped: {message}'


class FirestoreIncidentGate:
    def __init__(self,db,env):
        self.db=db
        self.ref=db.collection(f'execution-alerts-{env}').document('shared_etf_reconcile')

    def failed(self,error):
        from google.cloud import firestore
        @firestore.transactional
        def update(tx):
            snap=self.ref.get(transaction=tx)
            state,notify=failure_transition(snap.to_dict() if snap.exists else {},error)
            tx.set(self.ref,state)
            return notify
        return update(self.db.transaction())

    def recovered(self):
        from google.cloud import firestore
        @firestore.transactional
        def update(tx):
            snap=self.ref.get(transaction=tx)
            state,notify=success_transition(snap.to_dict() if snap.exists else {})
            if notify:tx.set(self.ref,state)
            return notify
        return update(self.db.transaction())

    def retry_notification(self,error):
        """Allow the next run to retry if Telegram rejected the alert."""
        from google.cloud import firestore
        @firestore.transactional
        def update(tx):
            snap=self.ref.get(transaction=tx)
            if snap.exists and snap.to_dict().get('key')==incident_key(error):
                state=snap.to_dict();state['active']=False;tx.set(self.ref,state)
        update(self.db.transaction())
