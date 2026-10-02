"""Regressions for Alpaca's daily account fee aggregation and receipt overlap."""
from copy import deepcopy
from decimal import Decimal

import pytest

from execution.broker import Executor, sync_activities
from execution.fees import accrue_account_fees, accrue_regulatory_fees
from execution.ledger import Ledger, SafetyStop, reconcile_state, totals
from test_execution import MemoryStore, seeded


def fill(aid, order_id, side, qty, price, *, stamp='2026-10-02T15:35:35Z', symbol='DBC'):
    return dict(id=aid, activity_type='FILL', transaction_time=stamp,
                order_id=order_id, side=side, symbol=symbol, qty=qty, price=price)


# Scrubbed IDs, exact executed quantities/prices from the failed October recovery.
OCTOBER_FILLS = [
    fill('dbc-1', 'dbc', 'sell', '32', '32.1928'),
    fill('dbc-2', 'dbc', 'sell', '.994892982', '32.1928'),
    fill('shv-1', 'shv', 'sell', '.317258', '110.082', symbol='SHV'),
    fill('qld-1', 'qld', 'buy', '1', '98.225', symbol='QLD'),
    fill('qld-2', 'qld', 'buy', '.653749', '98.225', symbol='QLD'),
    fill('sgov-1', 'sgov', 'buy', '.242121', '100.438', symbol='SGOV'),
    fill('wldu-1', 'wldu', 'buy', '3', '17.3427', symbol='WLDU'),
    fill('spxl-1', 'spxl', 'buy', '.334111', '288.082', symbol='SPXL'),
]


def prepared(activities=OCTOBER_FILLS, reserve_cash='0'):
    state = seeded()
    state['env'] = 'live'
    state['portfolios']['reserve']['cash'] = reserve_cash
    state['order_owners'] = {a['order_id']: 'mix8' for a in activities}
    store = MemoryStore(state)
    ledger = Ledger(store)
    return store, ledger, ledger.acquire()


def day(store, date='2026-10-02'):
    return store.read()['regulatory_fee_days'][date]


def receipt(aid, typ, amount, *, date='2026-10-02'):
    return dict(id=aid, activity_type='FEE', activity_sub_type=typ,
                date=date, net_amount=amount, created_at='2026-10-03T00:40:00Z')


def test_actual_split_sells_and_five_buy_fills_accrue_daily_types_once_to_reserve():
    store, ledger, token = prepared()
    before = store.read()
    cash = totals(before)[1] - Decimal('.0478741878296')
    assert accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, cash)
    assert {k: Decimal(v) for k, v in day(store)['raw'].items()} == {
        'REG': Decimal('.022600721150506749760'),
        'TAF': Decimal('.006495869441490'), 'CAT': Decimal('.000115626395946')}
    assert day(store)['target'] == {'REG': '0.03', 'TAF': '0.01', 'CAT': '0.01'}
    assert day(store)['charged'] == day(store)['target']
    assert day(store)['outstanding'] == day(store)['target']
    assert len(day(store)['fills']) == 8
    assert store.read()['portfolios']['reserve']['debt'] == '0.05'
    assert store.read()['portfolios']['mix8'] == before['portfolios']['mix8']
    assert store.read()['portfolios']['aaa'] == before['portfolios']['aaa']
    assert not store.read().get('fee_accruals')
    snapshot = store.read()
    assert not accrue_regulatory_fees(ledger, token, OCTOBER_FILLS * 2, cash)
    assert store.read() == snapshot


def test_late_partial_fill_books_only_increment_of_cumulative_daily_ceil():
    first = fill('first', 'split', 'sell', '5', '1000')
    second = fill('second', 'split', 'sell', '5', '1000')
    store, ledger, token = prepared([first, second], reserve_cash='100')
    assert accrue_regulatory_fees(ledger, token, [first], '99.87')
    assert day(store)['target'] == {'REG': '0.11', 'TAF': '0.01', 'CAT': '0.01'}
    # The overlap page can omit the earlier fill; its durable fact remains.
    assert accrue_regulatory_fees(ledger, token, [second], '99.77')
    assert day(store)['target'] == {'REG': '0.21', 'TAF': '0.01', 'CAT': '0.01'}
    assert store.read()['portfolios']['reserve']['cash'] == '99.77'
    assert not accrue_regulatory_fees(ledger, token, [first, second], '99.77')


def test_buy_only_cat_evidence_survives_noise_until_cumulative_debit_exceeds_gate():
    first = fill('small', 'buy', 'buy', '1', '10')
    later = fill('large', 'buy', 'buy', '7000', '10')
    store, ledger, token = prepared([first, later], reserve_cash='100')
    assert not accrue_regulatory_fees(ledger, token, [first], '99.99')
    assert day(store)['target'] == {'REG': '0.00', 'TAF': '0.00', 'CAT': '0.01'}
    assert day(store)['charged'] == {'REG': '0', 'TAF': '0', 'CAT': '0'}
    assert store.read()['portfolios']['reserve']['cash'] == '100'
    assert accrue_regulatory_fees(ledger, token, [later], '99.97')
    assert day(store)['charged']['CAT'] == '0.03'
    assert store.read()['portfolios']['reserve']['cash'] == '99.97'


def test_taf_trade_cap_applies_across_split_fills_before_daily_rounding():
    fills = [fill('large-1', 'trade', 'sell', '30000', '.1'),
             fill('large-2', 'trade', 'sell', '30000', '.1')]
    store, ledger, token = prepared(fills, reserve_cash='100')
    assert accrue_regulatory_fees(ledger, token, fills, '89.90')
    assert day(store)['target'] == {'REG': '0.13', 'TAF': '9.79', 'CAT': '0.18'}


def test_unmatched_cash_keeps_target_visible_without_charging_any_portfolio():
    store, ledger, token = prepared(reserve_cash='100')
    before = store.read()['portfolios']
    assert not accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, '97')
    assert store.read()['portfolios'] == before
    assert day(store)['target'] == {'REG': '0.03', 'TAF': '0.01', 'CAT': '0.01'}
    assert day(store)['charged'] == {'REG': '0', 'TAF': '0', 'CAT': '0'}
    with pytest.raises(SafetyStop, match='Unexplained broker cash'):
        reconcile_state(store.read(), [{'symbol': 'UBT', 'qty': '100'}], '97')


@pytest.mark.parametrize('change', [
    {'order_id': 'manual'}, {'side': 'unknown'}, {'qty': '0'}, {'qty': '-1'},
    {'price': '0'}, {'price': 'NaN'}, {'transaction_time': '2027-01-02T15:00:00Z'},
    {'transaction_time': '2026-10-02T15:00:00'}, {'transaction_time': 'bad'},
])
def test_invalid_or_unowned_fill_proof_fails_before_any_fee_write(change):
    store, ledger, token = prepared()
    bad = dict(OCTOBER_FILLS[0], **change)
    before = store.read()
    with pytest.raises(SafetyStop):
        accrue_regulatory_fees(ledger, token, [bad], '-.05')
    assert store.read() == before


@pytest.mark.parametrize('missing', ['id', 'order_id', 'qty', 'price', 'side', 'symbol', 'transaction_time'])
def test_missing_fill_evidence_fails_closed(missing):
    store, ledger, token = prepared()
    bad = deepcopy(OCTOBER_FILLS[0])
    del bad[missing]
    with pytest.raises(SafetyStop):
        accrue_regulatory_fees(ledger, token, [bad], '-.05')
    assert not store.read().get('regulatory_fee_days')


@pytest.mark.parametrize('change', [
    {'qty': '31'}, {'price': '32.19'}, {'side': 'buy'}, {'order_id': 'shv'},
    {'symbol': 'SHV'}, {'transaction_time': '2026-10-03T15:00:00Z'},
])
def test_reused_fill_id_with_changed_economics_or_day_fails_even_inside_cash_gate(change):
    store, ledger, token = prepared()
    assert accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, '-.05')
    before = store.read()
    with pytest.raises(SafetyStop, match='fill'):
        accrue_regulatory_fees(ledger, token, [dict(OCTOBER_FILLS[0], **change)], '-.05')
    assert store.read() == before


def test_reused_fill_id_with_changed_owner_fails_even_after_activity_overlap_pruning():
    store, ledger, token = prepared()
    assert accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, '-.05')
    store.mutate('owner_changed', lambda s: s['order_owners'].update(dbc='aaa'))
    with pytest.raises(SafetyStop, match='fill'):
        accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, '-.05')


def test_fills_use_new_york_trading_day_and_receipts_cover_that_same_day():
    fills = [fill('overnight', 'trade', 'sell', '32', '32.1928',
                  stamp='2026-10-03T00:30:00+00:00')]
    store, ledger, token = prepared(fills)
    assert accrue_regulatory_fees(ledger, token, fills, '-.05')
    assert set(store.read()['regulatory_fee_days']) == {'2026-10-02'}
    sync_activities(ledger, token, [receipt('sec', 'REG', '-.03')])
    assert day(store)['outstanding']['REG'] == '0.00'
    assert store.read()['portfolios']['reserve']['debt'] == '0.05'


def test_small_receipts_leave_unsettled_provision_visible_after_all_types_arrive():
    store, ledger, token = prepared()
    assert accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, '-.05')
    receipts = [receipt('sec-small', 'REG', '-.02'), receipt('taf', 'TAF', '-.01'),
                receipt('cat', 'CAT', '-.01')]
    sync_activities(ledger, token, receipts)
    assert day(store)['outstanding'] == {'REG': '0.01', 'TAF': '0.00', 'CAT': '0.00'}
    assert store.read()['portfolios']['reserve']['debt'] == '0.05'
    sync_activities(ledger, token, [receipt('sec-late', 'REG', '-.01')])
    assert day(store)['outstanding']['REG'] == '0.00'
    assert day(store)['charged']['REG'] == '0.03'
    # Global overlap IDs are bounded; the permanent receipt journal still dedups.
    store.mutate('overlap_pruned', lambda s: s.update(activity_ids=[]))
    sync_activities(ledger, token, receipts)
    assert store.read()['portfolios']['reserve']['debt'] == '0.05'


def test_larger_actual_receipt_books_only_uncovered_amount_once():
    store, ledger, token = prepared()
    assert accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, '-.05')
    event = receipt('sec', 'REG', '-.04')
    sync_activities(ledger, token, [event])
    assert day(store)['charged']['REG'] == '0.04'
    assert day(store)['outstanding']['REG'] == '0.00'
    assert store.read()['portfolios']['reserve']['debt'] == '0.06'
    store.mutate('overlap_pruned', lambda s: s.update(activity_ids=[]))
    sync_activities(ledger, token, [event])
    assert store.read()['portfolios']['reserve']['debt'] == '0.06'
    assert not accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, '-.06')


@pytest.mark.parametrize('change', [
    {'net_amount': '-.04'}, {'activity_sub_type': 'TAF'}, {'date': '2026-10-03'},
    {'activity_sub_type': 'OTHER'}, {'net_amount': '.03'}, {'date': '2026-09-25'},
])
def test_reused_receipt_id_with_changed_amount_type_or_day_fails_after_overlap_pruning(change):
    store, ledger, token = prepared()
    sync_activities(ledger, token, [receipt('sec', 'REG', '-.03')])
    store.mutate('overlap_pruned', lambda s: s.update(activity_ids=[]))
    before = store.read()
    with pytest.raises(SafetyStop, match='receipt'):
        sync_activities(ledger, token, [receipt('sec', 'REG', '-.03') | change])
    assert store.read() == before


def test_receipts_before_provision_are_real_charges_and_never_accrued_again():
    store, ledger, token = prepared()
    events = [receipt('sec', 'REG', '-.03'), receipt('taf', 'TAF', '-.01'),
              receipt('cat', 'CAT', '-.01')]
    sync_activities(ledger, token, events)
    assert store.read()['portfolios']['reserve']['debt'] == '0.05'
    assert not accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, '-.05')
    assert day(store)['target'] == day(store)['charged']
    assert day(store)['outstanding'] == {'REG': '0', 'TAF': '0', 'CAT': '0'}


def test_october_accrual_and_receipts_preserve_legacy_september_rows_exactly():
    store, ledger, token = prepared()
    legacy = {'date': '2026-09-25', 'strategy': 'mix8', 'order_id': 'old',
              'amount': '.10', 'remaining': '.10', 'fill_ids': ['old-fill'],
              'receipt_ids': [], 'source': '2026 SEC/TAF schedule + observed broker cash'}
    store.mutate('legacy', lambda s: s.update(fee_accruals=[legacy]))
    assert accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, '-.05')
    sync_activities(ledger, token, [receipt('sec', 'REG', '-.03'), receipt('taf', 'TAF', '-.01'),
                                  receipt('cat', 'CAT', '-.01')])
    assert store.read()['fee_accruals'] == [legacy]


def test_other_day_receipt_cannot_consume_this_days_provision():
    store, ledger, token = prepared()
    assert accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, '-.05')
    sync_activities(ledger, token, [receipt('next-day', 'REG', '-.03', date='2026-10-03')])
    assert day(store)['outstanding']['REG'] == '0.03'
    assert store.read()['portfolios']['reserve']['debt'] == '0.08'


def test_joint_interest_and_regulatory_debit_remains_a_visible_safety_stop():
    store, ledger, token = prepared(reserve_cash='100')
    store.mutate('margin_baseline', lambda s: s.update(
        account_fee_tracker={'accrued_fees': '5.23', 'pending': '5.23'}))
    account = {'id': 'account', 'cash': '99.71', 'accrued_fees': '5.47'}
    assert not accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, account['cash'])
    assert not accrue_account_fees(ledger, token, account)
    assert store.read()['portfolios']['reserve']['cash'] == '100'
    assert store.read()['account_fee_tracker']['pending'] == '5.23'
    with pytest.raises(SafetyStop, match='Unexplained broker cash'):
        reconcile_state(store.read(), [{'symbol': 'UBT', 'qty': '100'}], account['cash'])


def test_recover_ingests_real_fill_evidence_then_explains_october_cash_without_posting():
    store, ledger, token = prepared(reserve_cash='100')

    class ReadOnlyBroker:
        def open_orders(self): return []
        def activities(self, after): return deepcopy(OCTOBER_FILLS)
        def positions(self): return [{'symbol': 'UBT', 'qty': '100', 'current_price': '10'}]
        def account(self): return {'id': 'account', 'cash': '99.9521258121704'}

    executor = Executor(ledger, ReadOnlyBroker())
    result = executor.recover(token)
    assert result['cash'] == '99.95'
    assert result['cash_rounding'] == '0.0021258121704'
    executor.recover(token)
    assert store.read()['portfolios']['reserve']['cash'] == '99.95'
    assert len(day(store)['fills']) == 8


def test_accrual_audits_changed_receipt_even_when_global_activity_ids_skip_it():
    store, ledger, token = prepared()
    event = receipt('sec', 'REG', '-.03')
    sync_activities(ledger, token, [event])
    changed = event | {'net_amount': '-.04'}
    sync_activities(ledger, token, [changed])  # The general overlap path skips it.
    before = store.read()
    with pytest.raises(SafetyStop, match='receipt'):
        accrue_regulatory_fees(ledger, token, [changed] + OCTOBER_FILLS, '-.05')
    assert store.read() == before


def test_upgrade_imports_proven_already_posted_receipts_without_debiting_again():
    store, ledger, token = prepared()
    events = [receipt('sec', 'REG', '-.03'), receipt('taf', 'TAF', '-.01'),
              receipt('cat', 'CAT', '-.01')]
    # State left by the prior implementation: receipts seen and cash already debited.
    store.mutate('old_ingest', lambda s: (s.update(activity_ids=['sec', 'taf', 'cat']),
                                        s['portfolios']['reserve'].update(debt='0.05')))
    before = store.read()['portfolios']
    assert not accrue_regulatory_fees(ledger, token, events + OCTOBER_FILLS, '-.05')
    assert store.read()['portfolios'] == before
    assert day(store)['charged'] == {'REG': '0.03', 'TAF': '0.01', 'CAT': '0.01'}
    assert day(store)['outstanding'] == {'REG': '0', 'TAF': '0', 'CAT': '0'}
    store.mutate('overlap_pruned', lambda s: s.update(activity_ids=[]))
    sync_activities(ledger, token, events)
    assert store.read()['portfolios'] == before


def test_upgrade_cannot_guess_coverage_of_new_date_legacy_provisions():
    store, ledger, token = prepared()
    store.mutate('old_ingest', lambda s: s.update(
        activity_ids=['sec'], fee_accruals=[{'date': '2026-10-02', 'strategy': 'mix8',
        'amount': '.08', 'remaining': '.05', 'fill_ids': [], 'receipt_ids': ['sec']}]))
    before = store.read()
    with pytest.raises(SafetyStop, match='legacy'):
        accrue_regulatory_fees(ledger, token, [receipt('sec', 'REG', '-.03')] + OCTOBER_FILLS, '-.05')
    assert store.read() == before


@pytest.mark.parametrize('event', [
    receipt('bad-date', 'REG', '-.03', date='2027-01-02'),
    receipt('no-date', 'REG', '-.03', date=None),
    receipt('invalid-date', 'REG', '-.03', date='2026-10-32'),
    receipt('credit', 'REG', '.03'),
])
def test_unsupported_regulatory_receipt_dates_and_reversals_fail_closed(event):
    store, ledger, token = prepared()
    before = store.read()
    with pytest.raises(SafetyStop):
        sync_activities(ledger, token, [event])
    assert store.read() == before


def test_day_order_cannot_reset_its_taf_cap_on_another_trading_day():
    first = fill('day-one', 'order', 'sell', '30000', '.1')
    later = fill('day-two', 'order', 'sell', '30000', '.1', stamp='2026-10-03T15:30:00Z')
    store, ledger, token = prepared([first, later], reserve_cash='100')
    assert accrue_regulatory_fees(ledger, token, [first], '94.00')
    before = store.read()
    with pytest.raises(SafetyStop, match='trading day'):
        accrue_regulatory_fees(ledger, token, [later], '94.00')
    assert store.read() == before


def test_cutover_end_uses_new_york_day_before_refusing_unverified_future_rates():
    last = fill('last', 'order', 'sell', '32', '32.1928', stamp='2027-01-01T02:00:00Z')
    store, ledger, token = prepared([last])
    assert accrue_regulatory_fees(ledger, token, [last], '-.05')
    assert set(store.read()['regulatory_fee_days']) == {'2026-12-31'}


def test_zero_receipt_is_visible_evidence_without_releasing_provision():
    store, ledger, token = prepared()
    assert accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, '-.05')
    sync_activities(ledger, token, [receipt('sec-zero', 'REG', '0')])
    assert day(store)['outstanding']['REG'] == '0.03'
    assert 'sec-zero' in day(store)['receipts']
    assert store.read()['portfolios']['reserve']['debt'] == '0.05'


def test_lease_fencing_prevents_both_new_evidence_and_fee_debit():
    store, ledger, token = prepared()
    store.mutate('takeover', lambda s: s['lease'].update(token='another-executor'))
    before = store.read()
    with pytest.raises(SafetyStop, match='fenced'):
        accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, '-.05')
    assert store.read() == before


def test_unknown_daily_policy_cannot_be_used_for_another_cash_accrual():
    store, ledger, token = prepared()
    assert accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, '-.05')
    store.mutate('unknown_policy', lambda s: s['regulatory_fee_days']['2026-10-02'].update(policy='unknown'))
    before = store.read()
    with pytest.raises(SafetyStop, match='policy'):
        accrue_regulatory_fees(ledger, token, OCTOBER_FILLS, '-.05')
    assert store.read() == before
