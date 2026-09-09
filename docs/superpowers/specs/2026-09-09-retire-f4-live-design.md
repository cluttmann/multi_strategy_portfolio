# F4 live retirement and reallocation

Date: 2026-09-09
Branch: `codex/retire-f4-live`

## Decision

Retire the World 40/30/30 (F4) sleeve from the live Alpaca portfolio. The
existing MSCI ACWI strategy in the separate Scalable account already supplies
the intended international-equity role, while the corrected F4/GOLY evidence
is too short to justify retaining a duplicate sleeve.

The remaining six strategy weights are the old weights renormalized after
removing F4's 18% allocation:

| Strategy | New target |
|---|---:|
| HFEA | 18.29% |
| SPXL SMA | 18.29% |
| 9-Sig | 6.10% |
| Dual Momentum | 24.39% |
| Regime SSO | 14.64% |
| 7-Asset Rotator | 18.29% |

The rounded values sum exactly to 100.00%.

## Permanent retirement

- Remove `f4_allo` from the production allocation registry.
- Remove F4 from ticker ownership, contribution rebalancing, the monthly
  orchestrator, the monthly audit, Cloud Function routes, and local CLI actions.
- Remove the F4 deploy and scheduler definitions from `cloudbuild.yaml`.
- Add an idempotent Cloud Build cleanup step that deletes the two stale F4 Cloud
  Functions and the quarterly F4 scheduler after the six-strategy code is live.
- Preserve the historical research implementation and README results as a
  clearly labelled discontinued strategy; do not rewrite the historical record.

## One-time live migration

The migration is a local admin command, not a public Cloud Function. It has a
read-only dry-run mode and an execution mode protected by the exact confirmation
token `RETIRE_F4_LIVE`.

Execution order:

1. Read a fresh live account, position, market-clock, and open-order snapshot.
2. Abort unless the US market is open and there are no open orders.
3. Sell the complete available quantities of `WLDU`, `GOLY`, and `TLT`.
4. Wait strictly for every sell to fill; abort on rejection, cancellation,
   expiry, or timeout.
5. Re-read positions and abort before buys if any material F4 quantity remains.
6. Re-run the existing four-way margin gate on the post-sale account.
7. Calculate six-strategy contribution tilts with the existing underweight
   algorithm and cap total buys at actual gross F4 sale proceeds.
8. Run the six existing monthly strategy engines with that single shared budget.
9. Stop after any strategy exception or lingering open order; never attempt an
   automatic rollback of already-filled market orders.
10. Reconcile Firestore cost basis and mark the historical F4 state retired.

The gate remains fail-closed. If margin is not approved, only positive cash may
be invested. If the account remains cash-negative after the sale, no buys occur
and the proceeds simply reduce margin debt.

## Auditability and idempotency

The command writes a JSON audit file under `/tmp` before the first order and
refreshes it after each sell and each strategy run. A second invocation with no
remaining F4 positions returns `already_retired` and cannot create another set
of buys. The historical Firestore document is retained with zero positions,
the actual proceeds, order IDs, and retirement timestamp.

## Deployment and live gate

All code and documentation are committed, fast-forwarded to `main`, and pushed.
The existing `main` push trigger performs the deployment; no manual
`gcloud builds submit` is used. Live liquidation is blocked until the regional
Cloud Build is green and the deployed function/scheduler inventory confirms the
six-strategy version and removal of both F4 functions plus its scheduler.

The final live dry-run reports current sell quantities, estimated proceeds,
margin decision, total permitted reallocation, and per-strategy budgets. Actual
orders require a separate explicit user approval after that report.
