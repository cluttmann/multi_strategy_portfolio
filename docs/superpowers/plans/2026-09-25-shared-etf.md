# Shared ETF execution implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship and migrate shared ETF ownership, then replace Mix8 EEM/GLD/TLT with EET/UGLD/UBT in live trading.
**Architecture:** A Firestore account ledger with immutable revision events owns quantities, cash and debt. Persisted plans and deterministic order IDs serialize every trading writer and resume confirmed broker executions. Existing signal functions feed the controller; raw orders are disabled in production.
**Tech Stack:** Python, Decimal, Firestore transactions, Alpaca REST, pytest, existing Cloud Build trigger.
**Spec:** docs/superpowers/specs/2026-09-25-shared-etf.md

## Global Constraints

- IEF remains 1x. Unleveraged signals and portfolio allocations remain unchanged.
- No external calls inside Firestore transactions. No trades after unexplained reconciliation differences.
- Each order belongs to exactly one strategy. Tax lots stay account-wide.
- Preserve daily, monthly and annual behavior; every real trading entry point uses the executor.
- Push main for deployment; no manual Cloud Build submit. No live --force.
- User has explicitly authorized implementation, deployment, migration and trades.

## Review Focus

- Lease expires between intent persistence and submission: a successor must not move past a possibly submitted order.
- Cancel/expire with partial fills and delayed activities: reconcile quantities/cash before any later order.
- A migration sees retired dust or old paper positions: preserve them in an explicit legacy sleeve.
- A crash after funding or fills: retry must neither fund nor fill twice.
- Month-end interest, dividends and external deposits: ingest identifiable cash events; unknown corporate actions stop execution.

### Task 1: Transactional ledger and executor
**Files:** execution/ledger.py, execution/broker.py, tests/test_execution.py
**Interfaces:** Store.read()/mutate(callback); Executor.run(plan), reconcile(); plan contains action/period/orders/state updates.
- [ ] Write behavioral tests: shared sale leaves the other owner untouched; over-sale denied; cumulative partial fills book once; unknown quantity/cash stops; stale lease fences mutation; response loss resumes by CID.
- [ ] Run `python3 -m pytest tests/test_execution.py -q` and observe the missing module/behavior failure.
- [ ] Implement Decimal ledger, Firestore revision journal, account lease, durable plans, bounded REST calls, broker ID recovery and terminal-state handling.
- [ ] Run the task tests to green and commit.

### Task 2: All strategy writers and reporting
**Files:** execution/controller.py, main.py, cloudbuild.yaml, tests/test_shared_integration.py
**Interfaces:** controller monthly/daily/cutover produce durable plans using Task 1; position_value reads only virtual holdings.
- [ ] Write tests for monthly retries, daily shared gold sale, annual transfers, missing product volatility and closed-market refusal.
- [ ] Observe failures with `python3 -m pytest tests/test_shared_integration.py -q`.
- [ ] Route all real APIs to controller; fail raw order entry; include execution package in deploy; use ledger ownership for allocation/cost basis/audit. Configure the selected three substitutions with old symbols retained only for liquidation.
- [ ] Pass task tests and entire existing suite; commit.

### Task 3: Operational migration and verification
**Files:** scripts/shared_etf_rollout.py, docs/shared-etf-operations.md
**Interfaces:** CLI snapshot/initialize/preview/execute/reconcile against the same Task 1/2 implementation.
- [ ] Test bootstrap rejects ambiguous ownership and nonzero open orders, preserves legacy quantities, and reads back exactly.
- [ ] Implement bootstrap with snapshots, explicit initial cash/debt attribution, migrated peaks and state. Preserve immutable raw broker evidence outside git.
- [ ] Paper execute and reconcile a shared position; verify duplicate replay; run monthly/daily/annual controller integration scenarios.
- [ ] Fresh whole-branch review; fix all material findings with failing tests first.
- [ ] Freeze trade schedulers during live cutover; snapshot and initialize; push main, verify regional build and function revisions; execute the approved Mix8 conversion in an open market, reconcile fills and re-enable schedules. If market has closed, leave a reviewed durable plan for next open and a follow-up to verify fills; report that execution remains pending.
