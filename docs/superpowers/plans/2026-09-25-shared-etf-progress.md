# SDD ledger — plan: docs/superpowers/plans/2026-09-25-shared-etf.md
Baseline: 148 tests passed; main 32766bb.
Pre-flight: Task 1 Store/Executor consumed by Task 2 controller and Task 3 CLI; one shared account ledger and broker adapter.
Ruling: User's latest approval authorizes live deployment and trades; no repeated permission checkpoint.
Ruling: Selected trio is EET/UGLD/UBT, IEF unchanged, as stated in recommendation and current commentary.
Ruling: Legacy positions remain explicitly in a legacy sleeve, never silently reassigned.
Task 1: complete — cumulative fills, shared ownership, fencing, durable claims, lost response recovery, own-cash limits proved by tests.
Task 2: complete — all real entry points use managed controller; deployment source includes execution package; shared valuation/audit.
Task 3: live bootstrap and conversion complete at 2026-09-25T19:59:12Z; cloud rollout still pending.
Paper evidence: 0.05 EEM sold and 0.02 EET bought; AAA stayed at 1.245201 EET; replay returned already_complete. Full paper conversion preflight blocked on a wide quote for a 13-cent legacy KMLM remnant; no order from that plan.
Live evidence: 44.741255 EEM sold; 16.764829 EET, 0.401025 QLD and 10.866122 SGOV bought; all filled by 19:58:59Z. AAA retained 4.98290874 EET. No open orders and quantities/cash reconciled.
Final review: fresh gpt-6-astra reviewer found one Important annual net-equity/margin issue; corrected with dedicated RED→GREEN test. No immediate cutover blocker or Critical finding. Operational verification and economic merits were explicitly outside the review; parent verifies broker, deployment and treats historical backtests as limited research.
Ruling: Quote source timestamps use the existing five-minute policy, not an arbitrary 90 seconds that rejected unchanged quiet-ETF quotes. Bid/ask spread and limit-price caps still apply; missing/wide quotes stop.
Ruling: First live sell withheld $0.10 before fee receipts. SEC/TAF rates verified against June 2026 Alpaca schedule, calculated per the two fills and matched broker cash within cent rounding. Journalled accrual allowed existing plan to resume, with no repeat sale. Dedicated automatic accrual/settlement tests added.
Ruling: Position NAV uses broker valuation with owned quantity; executable order quotes use the market-data cache. IEX can show an old last trade despite a current bid/ask.
Ruling: Preserve cash-only rotation on margin-data errors and retry funding alone; no duplicate rotation/contribution. Existing sleeve residuals do not create new margin capacity.
