# F4 Live Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove F4 from the deployed six-strategy portfolio and provide a guarded, auditable one-time live liquidation and reallocation command.

**Architecture:** Production strategy registries and orchestration become six-strategy only. A standalone local admin script imports the existing strategy engines, sells F4 first, recalculates the existing margin gate after fills, caps the shared reallocation budget at actual sale proceeds, and invokes only the six remaining engines. Cloud Build deploys the new orchestrator and idempotently deletes stale F4 functions and scheduler.

**Tech Stack:** Python 3.10, pytest, Alpaca Trading API, Firestore, Google Cloud Functions, Cloud Build, Cloud Scheduler.

**Spec:** [2026-09-09-retire-f4-live-design.md](../specs/2026-09-09-retire-f4-live-design.md)

## Global Constraints

- Never use the generic CLI `--force` flag with `--env live`.
- Never submit a live order before the user approves the final dry-run.
- Never buy until all F4 sell orders are terminally filled and a fresh position read shows no material F4 remainder.
- Reuse `check_margin_conditions`; no fallback, cached default, or bypass is allowed.
- Cap the one-time buy budget at actual gross F4 sale proceeds.
- Preserve the historical `strategy-balances-live/f4` document and mark it retired instead of deleting it.
- Do not run `gcloud builds submit`; a push to `main` starts the regional trigger.
- Commit messages follow repository style and contain no AI co-author trailer.

---

### Task 1: Lock the six-strategy production registry

**Files:**
- Modify: `main.py`
- Create: `tests/test_f4_retirement.py`

**Interfaces:**
- Consumes: existing `strategy_allocations`, `STRATEGY_SYMBOLS`, `get_all_strategy_values`, and `calculate_rebalanced_allocations`.
- Produces: six allocations summing to `1.0`; no active `f4` ownership/value/allocation entry.

- [ ] **Step 1: Write failing registry tests**

```python
def test_active_allocations_exclude_f4_and_sum_to_one():
    assert "f4_allo" not in main.strategy_allocations
    assert sum(main.strategy_allocations.values()) == pytest.approx(1.0)

def test_active_ticker_ownership_excludes_f4():
    assert "f4" not in main.STRATEGY_SYMBOLS
```

- [ ] **Step 2: Run `python3 -m pytest tests/test_f4_retirement.py -v`**

Expected: FAIL because both F4 keys still exist.

- [ ] **Step 3: Replace allocations with `0.1829, 0.1829, 0.0610, 0.2439, 0.1464, 0.1829` and remove F4 from active registries and value/rebalance maps.**

- [ ] **Step 4: Re-run the focused test and confirm PASS.**

- [ ] **Step 5: Commit with `portfolio: F4 aus aktiven Strategien entfernen`.**

### Task 2: Add pure retirement planning helpers

**Files:**
- Create: `scripts/retire_f4_live.py`
- Modify: `tests/test_f4_retirement.py`

**Interfaces:**
- Produces:
  - `F4_SYMBOLS: tuple[str, ...]`
  - `extract_f4_positions(positions: list[dict]) -> list[dict]`
  - `cap_investment_calculation(calculation: dict, proceeds: float) -> dict`
  - `project_post_sale_margin(margin_result: dict, estimated_proceeds: float) -> dict`

- [ ] **Step 1: Add failing tests proving unavailable quantities are excluded, the capped strategy budgets preserve their allocation ratios, and a projected sale first repays negative cash.**

- [ ] **Step 2: Run the focused tests and confirm failures are caused by missing helpers.**

- [ ] **Step 3: Implement the pure helpers without network or Firestore access.**

- [ ] **Step 4: Re-run the focused tests and confirm PASS.**

- [ ] **Step 5: Commit with `trading: F4-Aufloesung sicher planen`.**

### Task 3: Add guarded execution and audit trail

**Files:**
- Modify: `scripts/retire_f4_live.py`
- Modify: `tests/test_f4_retirement.py`

**Interfaces:**
- Produces:
  - `build_dry_run(api, env="live") -> dict`
  - `execute_retirement(api, env="live", confirmation=None, audit_path=None) -> dict`
  - CLI flags `--env`, `--dry-run`, `--execute`, `--confirm`, and `--audit-path`.

- [ ] **Step 1: Add failing tests for wrong confirmation, closed market, existing open orders, empty/already-retired F4, sell failure, residual F4 after sells, margin-gated zero buys, proceeds cap, and six-strategy execution order.**

- [ ] **Step 2: Run the focused tests and confirm expected failures.**

- [ ] **Step 3: Implement strict fill polling, pre/post snapshots, `/tmp` JSON audit persistence, Firestore retirement marking, six-engine execution, pending-order checks, and final cost-basis reconciliation.**

- [ ] **Step 4: Re-run the focused tests and confirm PASS.**

- [ ] **Step 5: Commit with `trading: F4 live sicher aufloesen und umschichten`.**

### Task 4: Retire F4 deployment surfaces

**Files:**
- Modify: `main.py`
- Modify: `cloudbuild.yaml`
- Modify: `tests/test_f4_retirement.py`

**Interfaces:**
- Production exposes no `monthly_buy_f4` or `quarterly_rebalance_f4` route/CLI action.
- Cloud Build contains one idempotent `retire-f4-services` cleanup step.

- [ ] **Step 1: Add failing source-contract tests that reject active F4 routes/actions/orchestrator calls and require the cleanup commands.**

- [ ] **Step 2: Run the focused tests and confirm FAIL.**

- [ ] **Step 3: Remove F4 from the orchestrator, audit, routes, and CLI; remove its deploy/scheduler blocks; repair all Wave C/D `waitFor` dependencies; add idempotent delete commands for both functions and the scheduler.**

- [ ] **Step 4: Run focused tests and a YAML parse check; confirm PASS.**

- [ ] **Step 5: Commit with `deploy: F4 Functions und Scheduler retiren`.**

### Task 5: Update documentation and verify the branch

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-09-09-retire-f4-live-design.md`
- Modify: `docs/superpowers/plans/2026-09-09-retire-f4-live.md`

**Interfaces:**
- README identifies six active strategies and F4 as discontinued on 2026-09-09.

- [ ] **Step 1: Update current allocation, architecture, deployment matrix, CLI examples, and F4 headings while preserving backtest material as historical evidence.**

- [ ] **Step 2: Run `python3 -m pytest tests/test_f4_retirement.py -v`.**

- [ ] **Step 3: Run `python3 -m pytest tests/ -v`.**

- [ ] **Step 4: Audit route-to-entry-point names and ensure allocations sum exactly to one.**

- [ ] **Step 5: Commit with `docs: F4-Retirement dokumentieren`.**

### Task 6: Merge, deploy, verify, and prepare live dry-run

**Files:**
- No additional source changes expected.

**Interfaces:**
- Produces a green `main` Cloud Build and a read-only live retirement report.

- [ ] **Step 1: Review the complete diff and confirm the worktree is clean.**

- [ ] **Step 2: Fast-forward local `main` to `codex/retire-f4-live` and push `main` to `origin`.**

- [ ] **Step 3: Wait for the `europe-west3` Cloud Build trigger to appear and finish successfully.**

- [ ] **Step 4: Verify `monthly_invest_all` runs the new revision; verify `monthly_buy_f4`, `quarterly_rebalance_f4`, and scheduler `quarterly_rebalance_f4` no longer exist.**

- [ ] **Step 5: Run `python3 scripts/retire_f4_live.py --env live --dry-run`, report the fresh sell quantities and capped per-strategy budgets, and wait for explicit approval before `--execute`.**
