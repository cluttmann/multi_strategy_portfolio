# Monthly quote-aware execution

User approved implementing and deploying the preceding EET research proposal,
with production ready by 2026-10-02 15:30 Europe/Berlin (13:30 UTC).
Source design: /Users/carl/Coding/hfea_strategy/output/eet-execution-audit-2026-10-02/report.md.

## Global constraints

- Worktree: /Users/carl/.codex/worktrees/monthly-spread-retries/hfea_strategy.
- Keep legacy active-plan recovery backward compatible; do not reset or infer ownership.
- Funding is reserved once, ownership stays per sleeve, broker POST never blindly retries.
- No live orders, Firestore writes, or deployments from implementer agents.
- Preserve monthly orchestrator, conservative margin gates, annual confirmed-proceeds transfers.
- Do not include AI co-author trailers. Do not edit tax files or unrelated research.
- Root owns main.py wrapper/config integration, cloudbuild.yaml, runtime access and deployment.

### Task 1: Implement durable quote-aware monthly execution

Own execution/*.py and tests for execution/shared integration. Read the current
controller, broker and ledger fully. Write meaningful failing regression tests first.
Implement an incremental backward-compatible path for newly built monthly plans:

1. Monthly plan/funding must persist once before execution quote eligibility checks.
   Its fixed per-sleeve dollar targets and maximum funding are durable. New quote
   attempts must never reallocate or repeat monthly funding. Build must not abort
   the entire plan because a selected instrument's spread is temporarily wide.
2. Execution quotes explicitly use free IEX, never delayed_sip or last-trade fallback.
   Cache preserves quote source and independent timestamps and rejects a cached quote from another source.
   Newly requested quotes must have positive bid/ask/depth, uncrossed market and
   source age <=30 seconds. Subscription/access or malformed data failures remain
   visible safety/data errors; no fake midpoint, no implicit switch to IEX.
3. EET monthly execution: full IEX spread soft .003, hard .005; first attempt price
   concession max .001 from midpoint, later attempt max .0015. Buys round limits
   DOWN, sells UP so the bound is never exceeded. Passively rest inside the spread
   when necessary. Non-EET limits must also use fresh quotes; preserve reasonable
   existing bounds unless the interface requires a tighter policy. Monthly risk
   exits and daily risk exits must not inherit slow-entry waiting/expiry rules.
4. Deferred quote cases should be returned as awaiting_quotes/pending, with durable
   reasons and next attempts, not SafetyStop for a normal wide spread. One blocked
   symbol/sleeve must not block unrelated eligible trades. Bound each attempt to
   5 minutes; confirm cancel/expire terminal state and book late/partial fills before
   a new attempt ID. No duplicate POST after timeout/ambiguous accept. Queued/open
   orders' notional reservations must prevent overcommit of sleeve and broker cash.
5. Before each buy validate fresh broker buying power, owned sleeve cash, and
   current margin permission without expanding the stored monthly funding cap.
   Sell-funded buys and annual transfers must wait for actual proceeds. Preserve
   original quantity support for fractional and nonfractional ETFs.
6. Monthly pending must not starve daily trend checks. A daily risk-off signal
   must invalidate/cancel an obsolete pending monthly trend buy (confirm terminal
   state before reuse of cash). Serialize via account lease; preserve all confirmed
   ownership and fills. Legacy active plans stay recoverable exactly as before.
7. Retry during 10:30–15:30 America/New_York, at most three trading days from plan
   creation and inside the existing day 1–7 window. After expiry, stop only the
   unfilled remainder, preserve funds/positions, journal and visibly report it;
   never silently mark the month cleanly complete. Return structured result for
   completed, pending, expired and data-error paths. Publish interim ledger holdings.
8. Expose a read-only quote getter usable by root preflight. Root will arrange
   scheduler repetitions and verify free IEX access. Notify root quickly
   about the chosen API/storage design and any blockers. Deadline for code/tests:
   preferably 13:00 UTC, leaving rollout time.

Run focused tests while iterating, then full tests once. Commit only owned paths
and task plan if helpful; root will review before deploying. No subagents.
Write report with RED/GREEN evidence, storage compatibility, run/quote interfaces,
remaining risks and commit SHA to the task report path supplied by root.

### Task 2: Runtime integration and infrastructure (root)

Wire any new quote getter / retry statuses to main. Repeated monthly scheduler:
5,35 10-15 1-7 * America/New_York (off reconcile's ten-minute schedule, first eligible
at 10:35 to avoid opening noise), bounded lease collision retry for monthly/daily.
Update reporting/operational docs. Preserve deployment waves and no manual
Cloud Build submission. Use free IEX; the user rejected the 99 USD/month SIP tariff. Wider venue spreads may expire pending entries.

### Task 3: Review and deploy (root)

Independently review ledger safety, attempts, partial cancellation, cash and daily
priority. Full tests; push approved main deployment; watch regional build and
function revisions, scheduler configs and quote preflight. Snapshot production
ledger before rollout; verify no unexplained quantities/cash after rollout.
