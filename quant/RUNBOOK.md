# Quant desk runbook — paper go-live

> **STATUS 2026-07-14: CLOUD-DEPLOYED & LIVE (Burn-in).** Läuft serverseitig
> (Mac-unabhängig) auf GCP `trading-436516`, region europe-west3, Konto
> PA3IN7QIGPSE ($100k). Cloud Run Job `quant-desk` (Image in Artifact
> Registry, Secrets aus Secret Manager, Modelle aus GCS
> `trading-436516-quant-models`), getriggert von 7 Cloud-Scheduler-Jobs
> (Zeitzone America/New_York, DST-korrekt) mit TASK-Override.
> Verwaltung: `gcloud scheduler jobs list --location=europe-west3`;
> Deploy-Skript: `quant/cloud/`. Rebuild: `gcloud builds submit
> --config=quant/cloud/cloudbuild.yaml .`. Handelstag-Guard + Burn-in 25 %
> wie zuvor. launchd (Mac) ist abgebaut.

## Daily schedule (all times Europe/Berlin; ET in parens)

| Time | What | Command |
|---|---|---|
| 03:00 (21:00 ET) | Daily ops: data, archives, features | `zsh quant/ops/daily.sh` |
| 14:30 (08:30 ET) | XSR plan (scores from last complete day) | `python3 -m quant.execution.xsr_live --plan` |
| 15:00 (09:00 ET) | XSR execute (opg orders → opening auction) | `python3 -m quant.execution.xsr_live --execute` |
| 15:15 (09:15 ET) | ONX exit (opg sells of overnight book) | `python3 -m quant.execution.onx_live --exit` |
| 16:15 (10:15 ET) | Reconcile both sleeves | `... xsr_live --reconcile && ... onx_live --reconcile` |
| 21:45 (15:45 ET) | ONX decide (trend gates, liquidity picks) | `python3 -m quant.execution.onx_live --decide` |
| 21:50 (15:50 ET) | ONX enter (cls buys → closing auction) | `python3 -m quant.execution.onx_live --enter` |

VOLC/CTREND executors: not yet built (small allocations; next iteration).

## Burn-in rules (DESIGN.md G10)

- First 20 trading days at 25% of target sleeve sizes.
- Kill checks: live fill slippage ≤ 1.5x cost model; live hit-rate/IC within
  2σ of backtest; else de-risk to zero + post-mortem.
- Drawdown ladder (automatic in risk.py): −8% from HWM → half gross;
  −12% → flat + manual-restart-only.

## Scheduling options

1. **launchd on this Mac** (fastest): plists calling the commands above.
   Fragile if the Mac sleeps — acceptable for burn-in.
2. **Cloud Run + Cloud Scheduler** (production): containerize `quant/`,
   mirror the ETF bot's scheduler pattern. Do this after burn-in validates.

## State & monitoring

- Firestore: `qnt-ledger/{onx,xsr}` (positions/plans), `qnt-risk/state`
  (HWM, halts). Manual kill: set `manual_halt: true` in `qnt-risk/state`.
- Every action posts to Telegram (🧪 QNT prefix).
- Shared-account rule: quant never touches BOT_TICKERS; all orders carry
  `QNT-` client-order-ids.
