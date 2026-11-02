# Execution recovery implementation plan

**Goal:** Recover the approved November run without cash plugs, preserve trading limits, and avoid repeated incident alerts.

**Architecture:** Keep the exact Decimal ledger unchanged. Reconcile its cash at the broker's reported cent precision, retaining the two-cent limit and reporting both differences. Refresh confirmed fills between sale and purchase phases. Use one persisted incident gate across monthly and recovery entry points.

**Authorization:** User approved implementation with “Go” after the three proposed changes. Execute inline and request independent review before deployment.

## Constraints and review focus

- No manual orders, forced execution, clearing intents, position resets, or guessed cash adjustments.
- A three-cent reported cash difference, unknown activity, unknown orders, invalid quotes, expired leases, and insufficient owned funds still block execution.
- Validate positive/negative cent rounding, partial fills, lost acknowledgements, same-sleeve proceeds, concurrent schedulers, and failed Telegram delivery.
- Do not change strategies, margin gates, quote freshness/spread limits, dependencies, or unrelated local tax work.

## Tasks

- [x] Add failing cash reconciliation tests for the observed balance and both signed/tie boundaries; compare only the expected balance at cent precision and preserve raw values.
- [x] Add failing tests for same-invocation reuse of confirmed sale proceeds, no reuse of unconfirmed proceeds, and continuing quote retry delays; add one reconciliation barrier before buys and reconsider only funding-related deferrals.
- [x] Add failing tests for stable quote incident identity, shared monthly/reconcile notifications, normal lease contention, and no false recovery while an intent remains in error; consolidate alert delivery without changing failure HTTP status.
- [ ] Run the full suite in the workspace and a clean committed snapshot, review independently, and document operational semantics.
- [ ] Snapshot live state; push scoped commits through the existing regional Cloud Build trigger; verify deployed source, a regular worker invocation, remaining orders, and broker/ledger agreement.
