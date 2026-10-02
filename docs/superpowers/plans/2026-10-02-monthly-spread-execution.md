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
5,35 10-15 1-7 * * America/New_York (off reconcile's ten-minute schedule, first eligible
at 10:35 to avoid opening noise), bounded lease collision retry for monthly/daily.
Update reporting/operational docs. Preserve deployment waves and no manual
Cloud Build submission. Use free IEX; the user rejected the 99 USD/month SIP tariff. Wider venue spreads may expire pending entries.

### Task 3: Review and deploy (root)

Independently review ledger safety, attempts, partial cancellation, cash and daily
priority. Full tests; push approved main deployment; watch regional build and
function revisions, scheduler configs and quote preflight. Snapshot production
ledger before rollout; verify no unexplained quantities/cash after rollout.

### Task 4: Free passive EET buy limits (latest user steering)

The user rejects the 99 USD SIP tariff and asks to protect purchase prices with
limit orders despite misleadingly wide IEX spreads. Root chooses a conservative
passive EET buy policy: limit = min(fresh IEX ask, fresh IEX bid * 1.001), rounded
DOWN to cents. This is a maximum purchase price, not a measured NBBO spread or a
fill guarantee. Use the SAME 10-basis-point bid cap for every retry; never raise
it toward a wide ask or midpoint. A current quote can move the absolute limit on
a later attempt after cancellation confirmation. Do not change other symbols,
EET sell limits, risk exits, funding, ownership, or daily interruption semantics.

Own execution/quotes.py, execution/monthly.py, execution/controller.py, and a new
tests/test_passive_eet_limits.py. Keep the execution_policy monthly-iex-v1 engine
identifier for compatibility; newly built monthly plans persist a separate
eet_buy_price_policy='iex-bid-cap-v1' marker BEFORE the first attempt. Old active
plans lacking this marker retain the reviewed midpoint/spread policy. Unknown
markers fail closed. All source/age/depth checks still apply. Persist the chosen
price policy/reference in actual attempts. Wide but valid IEX quotes may submit
a passive bid-based EET buy; invalid or stale quotes may not. Non-EET intents
continue independently. Tests must prove wide-ask independence, cent rounding,
no escalation on retry, fixed budget/partial-fill reservation, legacy behavior,
unknown-policy refusal, plan-marker persistence, and daily interruption safety.

Write failing regressions first, focused tests then full suite, commit only
owned files and report RED/GREEN plus exact commit in task-4-report.md. No live
requests, Firestore writes, deploy, or subagents. Root owns docs/rollout. This
addition follows the explicit request to implement everything and the user's
new order-based cost constraint. The cost of this conservative choice is a
possibly unfilled EET allocation; cash remains available after expiry.

### Task 5: Repair first scheduled production run

The first real monthly call at 17:05 Berlin failed before storing a plan or
funding: Controller.valuation_prices supplied three positional arguments to
main.update_market_data, whose contract is (symbol, env='live'). Correct that
contract and cover cache misses with a strict adapter regression. Audit adjacent
bot calls for the same mismatch without widening the strategy scope.

The simultaneous five-minute reconcile hit the account lease and incorrectly
created a failed incident. Only the exact SafetyStop 'Another executor owns this
account' should return pending/account_busy HTTP 200 without failed or recovered
incident state or Telegram. Other errors remain visible HTTP 500; keep fencing
and the current Scheduler timings. The next background tick performs the real
reconciliation. Implementer owns controller, main and relevant regression tests;
root owns docs, deployment and GET-only live verification. RED/GREEN, full suite,
independent review and a new immutable commit are required before the push.

### Task 6: Recover actual trade fees using the current broker contract

The 17:35 scheduled run submitted seven real limit orders and six filled;
EET remains resting at 105.58 USD. Recovery then stops on an observed 0.047874
USD cash debit. The current fee code rounds SEC/TAF on each FILL and omits CAT.
Alpaca's brokerage fee schedule, revised September 17, 2026, instead aggregates
each fee type daily per account using exact fractional quantities, then rounds
each type up to cents. Today's owned fills give REG .03 + TAF .01 + CAT .01 =
.05 USD, leaving only .002126 USD cent rounding against actual broker cash.
Source: https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf,
pages 3–4; corroborated by the official Regulatory Fees documentation.

Implement a narrow new daily fee policy for fills dated 2026-10-02 through
2026-12-31. Preserve historical per-fill accrual and receipt state. New account
fees are explicit reserve cash/debt events; fixed monthly funding is unchanged.
Require owned confirmed fills and a matching observed cash debit under the
unchanged .02 reconciliation gate. Persist daily raw/rounded targets, charged
amounts, outstanding receipts and IDs for incremental fills and replay safety.
TAF cap applies per trade before daily aggregation; CAT applies to buys and sells.
New receipt coverage must preserve date/type and prevent double booking, while
legacy settlements remain compatible. No cash reset, tolerance increase, extra
funding, manual order/cancel, or production ledger mutation by the implementer.

Cover actual split DBC fills and SHV plus purchases, replay, partial/late fills,
cent boundaries, buy-only CAT, capped split TAF, unknown or missing ownership,
unexplained cash, date bounds, old accrued rows, and delayed fee receipts.
RED/GREEN, full suite, immutable commit and independent review precede root's
authorized automatic deployment and GET-only live recovery verification.
