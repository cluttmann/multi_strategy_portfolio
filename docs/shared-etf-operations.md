# Shared ETF execution — operations

Live migration: 25 September 2026. Mix8 now maps EEM→EET, GLD→UGLD and
TLT→UBT; IEF remains unleveraged. Signals still use EEM, GLD and TLT. Product
volatility drives weights, so replacement is not dollar-for-dollar doubling.
The historical README backtest numbers preceding this migration describe the
old product mix. The new isolated comparisons include synthetic pre-inception
history; they are not a verified full-portfolio after-tax return forecast.

## Ownership and accounting

`execution-accounts-{env}/{account_id}` is authoritative. Portfolio quantities
sum to the broker, including an explicit `legacy` sleeve for retired remnants.
Each successful transaction creates an immutable `events/{revision}` snapshot.
Completed plans also live in `runs/{run_id}`. Decimal values are stored as strings.
`strategy-balances-{env}` is a derived reporting view, never the ownership source.

The migration used the previous exclusive symbol map and broker quantities.
Positive unassigned cash belongs to `reserve`; initial debt is explicitly
allocated in proportion to actual gross sleeve value, including legacy assets.
Initial peak NAV is scaled by the same debt adjustment to preserve drawdown.
Basis is economic weighted-average strategy basis, not a separate tax FIFO.
The existing account-wide tax FIFO and actual broker activity export stay intact.
Legacy `total_invested` is retained as historical data; it is not overwritten
with aggregate broker cost basis for a shared ETF.

A strategy may sell only its own shares. A buy must fit its own confirmed cash
at the limit price. Contributions, borrowing and annual transfers are journalled
once. New margin capacity excludes already earmarked sleeve cash. New account
cash first repays attributed debt; external deposit repayment adjusts the NAV
peak for the flow. Other identifiable cash income/expenses goes through the
explicit account reserve. Dividend income is centralized cash attribution, not
an invented record-date allocation to today's shared ETF owners.

## Execution and recovery

All real monthly/manual entry points use the monthly account orchestrator.
Daily trend jobs share the same executor. `submit_order` rejects real credentials
outside that path; the retired allocation script cannot close shared positions.
An expiring lease fences state mutations. Every order intent and client ID is
committed before POST. Broker POST is never retried. Recovery queries that ID.
A possibly submitted order that cannot be found blocks, rather than creating
another order. Partial and late cancel fills update cumulative deltas once.
The plan does not complete until all orders are filled and the broker reconciles.

Orders use day limits, 0.5% around an IEX bid/ask midpoint, with a maximum 0.9%
quoted spread and a five-minute source timestamp limit. Quotes are in the same
market-data collection with independent timestamps; refreshing quotes does not
refresh SMA data. Position NAV uses the broker valuation multiplied by owned
ledger quantity. Missing prices/volatility, unexplained holdings, unknown manual
fills/corporate actions and unresolved cancellations stop new orders visibly.

`shared_etf_reconcile` runs every ten minutes on weekdays 09:00–16:59 New York.
It reconciles, resumes an existing plan, and executes an explicitly authorized
pending upgrade only while the market is open. New plans are never created on a
closed market. A partial canceled/expired/rejected order requires reconciliation
and a reviewed repair of the remaining plan; it is not silently resubmitted.

Margin-data failure still permits the scheduled rotation with zero new funding.
The next daily monthly-scheduler attempt within days 1–7 retries only funding,
using the already chosen monthly weights, without replaying rotation or funding.
Annual transfers use net equity and a 1% proceeds buffer; rounding can leave a
small residual rather than borrowing against unconfirmed proceeds.

Cash reconciliation compares the broker's cent-rounded account balance with the
ledger balance rounded once to cents (`ROUND_HALF_UP`). The permitted difference
remains two reported cents. The exact ledger is never changed to force agreement;
the recovery result exposes both `cash_rounding` (raw difference) and
`cash_difference_cents` (difference at reported precision). Unexpected sub-cent
broker account cash and a three-cent reported difference still stop execution.
This establishes rounding-compatible agreement, not exact fee settlement.

Before the first monthly buy, recovery refreshes all existing order IDs and books
confirmed sell fills, including partial fills. Only those confirmed, sleeve-owned
proceeds can fund buys in the same invocation. Cash-related waits are reconsidered
without the quote retry delay; quote/spread waits, open reservations and all margin
gates retain their existing constraints. No POST is retried by this refresh.

Monthly funding rounds available cash and new margin down to microdollars,
leaving smaller residuals unused. The debt split
is bounded by that sleeve's funding and the cash/debt still available, so a
sub-microdollar division residual cannot invent a cash requirement when reserve
cash is zero. The strict ledger funding checks remain in force. Fully invested
rotator weights clamp their floating-point defensive residual to zero rather
than emitting a tiny negative target. Regression tests cover zero reserve,
sub-microdollar reserve, mixed cash/margin, and fully invested inverse-vol weights.

## Regulatory fees

Live cash can withhold selling fees before the FEE activity exists. Confirmed
fills plus the 2026 SEC/TAF schedule are accrued only when they explain the
observed cash decrement within broker cent rounding. The exact fill IDs and fee
provision are journalled. Posted REG/TAF/CAT receipts consume that provision
without debiting twice; after all three receipts, any unused provision releases.
Unmatched cash movements still stop. The accrual rates apply April–December 2026;
update against the published fee schedule before extending them to later dates.
Parqet exports only posted broker fees; never export an estimated ledger accrual.

Alpaca also withholds account fees, including margin interest, in `cash` before
the monthly `INT/MGN` activity is posted. The live bootstrap records the broker's
then-current `accrued_fees` as a baseline and a pending amount already included
in initial cash. On each run, a later increase is provisioned in the account
reserve only when the observed broker cash debit matches within the two-cent
broker rounding tolerance.
The posted `INT/MGN` receipt consumes the provision instead of debiting twice.
If the baseline is missing or the cash movement does not match, reconciliation
still stops. Each provision records the broker cash and the before/after fee
counter in the immutable ledger journal.

The reconcile route sends one Telegram stop alert per unresolved error class,
then one recovery alert when the broker and ledger agree again. It continues to
return HTTP 500 on every failed run so Cloud Scheduler and logs retain the
failure signal. A changed cash discrepancy remains the same alert incident;
the full amount stays in Cloud Run logs.

Monthly and managed strategy failures now share that incident gate. Changing quote
ages or error ordering do not create new incidents. A deferred, unresolved data
error remains a failed result until a successful attempt or confirmed order
recovery resolves it; an early retry cannot falsely announce recovery. Normal
account-lease contention returns `pending/account_busy` after bounded retries,
preserving the lease and avoiding a spurious stop notification.

## Commands

Run from the repository with the existing local credentials; output directories
contain private account data and must remain outside git.

```bash
python3 scripts/shared_etf_rollout.py --env live --action snapshot --output /tmp/shared-etf-audit
python3 scripts/shared_etf_rollout.py --env live --action reconcile --output /tmp/shared-etf-audit
python3 scripts/shared_etf_rollout.py --env live --action resume --output /tmp/shared-etf-audit
```

`initialize` creates the ledger once and refuses overwrite. `preview` prepares the
Mix8 upgrade without writing or trading. `execute` uses the durable upgrade key;
repeating a completed key cannot trade again. `authorize` records a pending
upgrade for the market-open worker; it is not required after a completed upgrade.

Before repairing an ambiguous submission: pause the two trading schedulers plus
`shared_etf_reconcile`, stop any local executor, wait for Cloud Function requests
to finish, retrieve the exact client order ID and account activities, and reconcile
against the journal. Never reset positions from target allocation or clear a
pending intent merely because a lease expired. A fully rejected/canceled order
may be replanned only after all fills and cash are booked and its broker state is
terminal. A new retry needs a new journalled attempt identity.

## Deployment / rollback

Push main; the regional `europe-west3` Cloud Build trigger deploys the package.
Do not manually submit a second build. Keep trading schedulers paused during the
migration; verify every trading function revision and the reconciliation route,
then resume jobs. Index-alert jobs are independent.
After shared positions exist, deploying the old exclusive-ownership code is not
a safe rollback. Pause execution and recover the journal; reversal trades require
a separate investment decision. Never delete the new ledger as rollback.

### Exhausted monthly funding

A monthly allocation now terminates as `complete_with_shortfall` when all orders
are broker-confirmed terminal, all sale intents are complete, transfers have been
booked, and every remaining buy has less than $1 of owned sleeve cash. Funding
waits and recovered submitted orders are eligible; quote errors, spread deferrals,
unfinished sales, unresolved transfers and usable cash behind a temporary broker
or margin gate remain pending/error outcomes.

The original buy budget is unchanged. The archived run and
`last_monthly_outcome[action]` retain each intent's exact `remaining_budget` and
`available_cash`; the intent is `unfunded` with reason
`confirmed_funding_exhausted`. Archival, the terminal month marker, shortfall
record and result outbox are committed atomically. No cash or debt is added.
The month cannot restart or request funding again. Telegram and the ledger audit
explicitly report completion with an unfunded target remainder, rather than full
target attainment. Closure needs no quote or open market once recovery has
confirmed the final broker state.
