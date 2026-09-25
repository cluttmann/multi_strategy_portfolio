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

## Regulatory fees

Live cash can withhold selling fees before the FEE activity exists. Confirmed
fills plus the 2026 SEC/TAF schedule are accrued only when they explain the
observed cash decrement within broker cent rounding. The exact fill IDs and fee
provision are journalled. Posted REG/TAF/CAT receipts consume that provision
without debiting twice; after all three receipts, any unused provision releases.
Unmatched cash movements still stop. The accrual rates apply April–December 2026;
update against the published fee schedule before extending them to later dates.
Parqet exports only posted broker fees; never export an estimated ledger accrual.

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
