"""FirestoreStore.mutate against a stand-in for Firestore transactions.

2026-11-11 16:05 UTC the lease acquisition failed with "400 The referenced
transaction has expired or is no longer valid". The ledger events prove the
commit never landed: revision 12758 (release 16:00) was followed directly by
12759 (acquire 16:10). Firestore's own commit RPC retry resends the same
transaction id; once the server has ended that transaction, the resend fails
with exactly this error. The executor stopped for one tick and recovered.
"""
import copy

import pytest
from google.api_core import exceptions
from google.cloud import firestore

from execution.ledger import FirestoreStore, Ledger, SafetyStop, new_state

EXPIRED = 'The referenced transaction has expired or is no longer valid.'


class Snap:
    def __init__(self, data):
        self._data = copy.deepcopy(data)
        self.exists = data is not None

    def to_dict(self):
        return copy.deepcopy(self._data)


class Ref:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def get(self, transaction=None):
        self.db.reads.append(self.path)
        return Snap(self.db.docs.get(self.path))

    def collection(self, name):
        return Collection(self.db, f'{self.path}/{name}')


class Collection:
    def __init__(self, db, path):
        self.db, self.path = db, path

    def document(self, doc_id):
        return Ref(self.db, f'{self.path}/{doc_id}')


class Tx:
    def __init__(self, db):
        self.db, self.writes = db, []

    def set(self, ref, data):
        self.writes.append((ref.path, copy.deepcopy(data)))

    def create(self, ref, data):
        if ref.path in self.db.docs:
            raise exceptions.AlreadyExists('exists')
        self.writes.append((ref.path, copy.deepcopy(data)))


class Db:
    """`commits` scripts each commit: 'ok', 'fail' (nothing written, then the
    error) or 'applied' (written, but the client still sees the error)."""

    def __init__(self, state, commits=()):
        self.docs = {'execution-accounts-paper/acct': copy.deepcopy(state)}
        self.commits, self.attempts, self.reads = list(commits), 0, []
        self.rollback_expired = False
        self.error = exceptions.InvalidArgument(EXPIRED)

    def collection(self, name):
        return Collection(self, name)

    def transaction(self):
        return Tx(self)


def transactional(fn):
    def run(tx):
        try:
            result = fn(tx)
        except Exception:
            if tx.db.rollback_expired:
                raise exceptions.InvalidArgument(EXPIRED)   # rollback fails too
            raise
        tx.db.attempts += 1
        outcome = tx.db.commits.pop(0) if tx.db.commits else 'ok'
        if outcome in ('ok', 'applied'):
            for path, data in tx.writes:
                tx.db.docs[path] = data
        if outcome != 'ok':
            raise tx.db.error
        return result
    return run


@pytest.fixture(autouse=True)
def fake_transactions(monkeypatch):
    monkeypatch.setattr(firestore, 'transactional', transactional)


def seeded():
    return new_state('acct', 'paper', {'aaa': {'UBT': '60'}}, {'aaa': '0', 'reserve': '100'},
                     {}, '2026-09-25T19:00:00Z')


def stored(db):
    return db.docs['execution-accounts-paper/acct']


def events(db):
    return sorted(p for p in db.docs if '/events/' in p)


def test_lease_commit_that_never_landed_is_retried_once():
    db = Db(seeded(), commits=['fail'])
    Ledger(FirestoreStore(db, 'acct', 'paper'), clock=lambda: 100).acquire()
    assert db.attempts == 2
    assert stored(db)['revision'] == 1
    assert stored(db)['lease']['until'] == 280
    assert events(db) == ['execution-accounts-paper/acct/events/000000000001']


def test_commit_that_landed_is_not_applied_twice():
    db = Db(seeded(), commits=['applied'])
    with pytest.raises(exceptions.InvalidArgument):
        FirestoreStore(db, 'acct', 'paper').mutate('acquire', lambda s: s.update(lease={'until': 1}))
    assert db.attempts == 1
    assert stored(db)['revision'] == 1


def test_second_expired_transaction_still_stops():
    db = Db(seeded(), commits=['fail', 'fail'])
    with pytest.raises(exceptions.InvalidArgument):
        FirestoreStore(db, 'acct', 'paper').mutate('acquire', lambda s: None)
    assert db.attempts == 2
    assert stored(db)['revision'] == 0
    assert events(db) == []


def test_other_invalid_argument_is_not_retried():
    db = Db(seeded(), commits=['fail'])
    db.error = exceptions.InvalidArgument('Document exceeds the maximum size')
    with pytest.raises(exceptions.InvalidArgument, match='maximum size'):
        FirestoreStore(db, 'acct', 'paper').mutate('acquire', lambda s: None)
    assert db.attempts == 1


def test_safety_stop_inside_the_transaction_is_not_retried():
    db = Db(seeded())
    calls = []

    def halt(state):
        calls.append(1)
        raise SafetyStop('Account trading is halted')
    with pytest.raises(SafetyStop):
        FirestoreStore(db, 'acct', 'paper').mutate('acquire', halt)
    assert calls == [1]
    assert db.attempts == 0


def test_expired_rollback_after_a_stop_is_not_mistaken_for_a_failed_commit():
    """The callback stopped; the "expired" comes from the rollback. Retrying
    would rerun a stop that has nothing to do with the commit."""
    db = Db(seeded())
    db.rollback_expired = True
    calls = []

    def halt(state):
        calls.append(1)
        raise SafetyStop('Account trading is halted')
    with pytest.raises(exceptions.InvalidArgument) as raised:
        FirestoreStore(db, 'acct', 'paper').mutate('acquire', halt)
    assert isinstance(raised.value.__context__, SafetyStop)
    assert calls == [1]
