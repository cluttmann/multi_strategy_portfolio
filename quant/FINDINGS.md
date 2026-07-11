# Quant research program — findings (session 2026-07-11)

**Mandate**: ML/LLM-driven active trading on the Alpaca paper account, aspirational
target ≥50%/yr CAGR. **Method**: pre-registered validation gauntlet (DESIGN.md §4),
purged walk-forward, survivorship-free data, honest costs, out-of-sample
confirmation sets, split-sample regime checks.

## Data infrastructure built (permanent)

- BigQuery `trading-436516.quant`: 55.9M EOD rows (30.5k symbols incl. 58k
  delisted names, 2000–2026), 30M SIP minute bars (28 ETFs, 2016–2026), 2.03M
  Benzinga news items (2016–2026, timestamp-authoritative).
- Walk-forward training harness, cost-stressed portfolio simulator,
  data-quality audit, daily incremental updaters.
- Execution engine: Alpaca broker wrapper (`QNT-` order lineage), Firestore
  ledger, fail-closed risk gates + drawdown ladder, Telegram, ONX executor
  with verified `cls`/`opg` auction order flow (dry-run tested).

## Strategy verdicts (all net of costs)

| Family | Full-sample | 2022–2026 regime | Verdict |
|---|---|---|---|
| ONX 3x-ETF overnight (28-ETF universe, trend gate) | +29.5%, Sharpe 1.06 | +7.6%, Sharpe 0.39 | validated, **decayed ~4x post-2021** |
| XSR ML cross-sectional ranker (IC>0 in 24/24 years) | +9.7%, Sharpe 0.50 | ~+8%/yr | validated, at G2 bar; corr 0.00 to ONX |
| VOLC vol carry (SVXY, contango gate) | +17.4%, Sharpe 0.64 | ~+9%/yr | validated, modest |
| CTREND crypto TSMOM (2021+) | +17.0%, Sharpe 0.60 | — | validated, modest |
| IMOM ETF intraday momentum | rule: 0bp gross | — | **killed** (dead post-publication) |
| GAP overnight-gap drift/fade | IC .17→.03 at 2022 | negative every year | **killed** (arbitraged) |
| PAIR 3x short-short decay harvest | −3%..−4% | negative | **killed** (borrow prices the edge) |
| DOW calendar refinement of ONX | vacuous rule | refuted | **killed** |
| CAT LLM catalyst drift (137k FinBERT-scored overnight events) | gross Sharpe 0.71 (+19.7%); net −30.6%/yr | IC>0 all 8 years | **killed** (real signal, edge 4x smaller than costs; G8 FinBERT floor) |

## The 50%/yr question — answered

- Highest honest full-sample configuration: ONX at 1.9–2.0x gross =
  **+43–44%/yr, MaxDD −69/−71%**. ≥50% requires ~2.3x+, breaching the 1.9x
  Reg-T overnight cap.
- Regime honesty: three independent anomaly families (IMOM, GAP, ONX) show
  the identical post-2021 decay signature. In the only regime that predicts
  the future (2022+), ONX at 1.9x compounds to **0.0%**.
- Forward-looking honest expectation for the deployable stack (ONX 1.3–1.5x +
  XSR 0.5–0.6x + VOLC/CTREND satellites): **~10–18%/yr at Sharpe ~0.8–1.1,
  MaxDD ~−35–50%** (paper; live typically worse by 2–6% CAGR).
- **Conclusion: no honest ≥50%/yr configuration exists in this search space.**
  Every remaining route to a printed "+50%" is data snooping; the gauntlet
  exists precisely to prevent it. This document is the negative result, and
  it is load-bearing: it prevents deploying fiction.

## The honest path to larger numbers (in order)

1. Deploy the validated stack on paper at G10 burn-in size; measure live
   Sharpe for 3–6 months. Live evidence is the only basis for re-levering.
2. Build CAT (LLM catalyst sleeve) — the genuine LLM differentiator;
   panel-estimated +2–6% account CAGR if it survives its gauntlet.
3. Re-test new anomalies as they emerge — the platform validates or kills a
   candidate family in hours.
4. Options overlays (Alpaca Level 3) unlock non-Reg-T leverage but have no
   validatable history until ~2027 (data starts Feb 2024).

## Notable data landmines (do not re-learn these)

- EODHD bulk endpoint costs 100 quota units/request; per-symbol costs 1.
- EODHD delisted series can be corrupted (LAN: +939,458x fake returns) —
  `portfolio_sim`'s |ret|>50% artifact guard is mandatory.
- Alpaca free tier: explicit `end` inside the recent-SIP window → 403;
  Benzinga corpus is the only timestamp-authoritative news source.
