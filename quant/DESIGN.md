# FINAL MASTER ARCHITECTURE — QNT ML Anomaly Desk v1

**Decision authority:** Head of Quant Research. **Date:** 2026-07-11. **Venue:** Alpaca paper (~$9,073, shared account, `QNT-` order lineage, 23-ticker exclusion set). **Synthesis of:** 4 designs (xs-ml-ranker, news-llm-event, intraday-anomaly, anomaly-regime-meta) + 3 judge verdicts.

**Decision summary:** Build 4 sleeves in strict priority order (IMOM → XSR → GAP → CAT), one HMM regime monitor as a non-trading overlay, and a slim shared platform. All three judges converged on the same skeleton; where they diverged (news sleeve form), I rule below. Every duplicated idea is merged into exactly one implementation. Every sleeve ships only through the validation gauntlet in §4.

---

## 1. Final sleeve lineup (build priority order)

### Sleeve 1 — IMOM: ETF Intraday Momentum (first live deployment)

The Gao-Han-Li-Zhou (2018 JFE) market intraday momentum effect: the first 30 minutes' return on liquid index/sector ETFs predicts the last 30 minutes' return, driven by late-informed trading, leveraged-ETF rebalancing, and MOC clustering. A hard rule (|first-half-hour return| > 25bps → trade its sign at 15:30, exit at the close) generates candidates; a deliberately tiny LightGBM meta-filter (depth ≤ 3, ≤ 200 trees, min_child_samples ≥ 100) predicts P(trade clears cost) and gates entries at P > 0.55. Fixed 27-ETF universe (QQQ, IWM, DIA, sector SPDRs, GLD, SLV, EEM, EFA, FXI, EWZ, HYG, LQD, IEF, GDX, XOP, KRE, SMH, XBI) — zero survivorship issues, no bot-ticker overlap. Execution is the auction doctrine: marketable-limit entry at 15:30, `cls` (market-on-close) exit filling at the official SIP auction print, sidestepping IEX quote quality entirely. Flat overnight every day. This sleeve is chosen first not for its return but because it is the smallest-overfit-surface anomaly in our feasible set and it proves the entire pipeline (ingestion → walk-forward harness → risk checks → execution → reconciliation → Telegram) end to end in ~2 weeks.

- **Model:** rule core + tiny LightGBM meta-filter; rule-only baseline always reported separately.
- **Data:** Alpaca SIP minute bars 2016→present (27 symbols, one-time backfill); FRED VIXCLS; live IEX minute-bar websocket.
- **Horizon:** 30 minutes (15:30 → close).
- **Honest net Sharpe:** 0.4–0.9 standalone. **Account CAGR contribution:** 1–4% (invested ~30 min/day at 20% capital allocation; per-trade net edge ~2–4bps).
- **Trades/day:** 0–4 round trips (avg ~2).
- **Sleeve capital:** 20% of equity, up to 2x intraday on high-conviction days.

### Sleeve 2 — XSR: Cross-Sectional LightGBM Ranker (flagship)

Daily cross-sectional ranking of ~1,200–1,800 US common stocks on ~24 frozen, prior-backed price/volume features (short-term reversal, 12-2 momentum, idiosyncratic vol, abnormal volume, Amihud illiquidity, overnight/intraday decomposition, lottery-max, FRED macro conditioning), all rank-transformed within date; label = 5-day forward return, rank-transformed and beta-residualized. Long top ~20 / short bottom ~20 with hysteresis turnover control (enter above 90th percentile, exit below 70th; ~20–30% one-way daily turnover). This is xs-ml-ranker Sleeve 1 upgraded with the transplants the judges mandated: (a) **correlation-to-sector-ETF assignment** (each stock mapped monthly to the sector ETF with highest 120d return correlation) replacing the nonexistent EODHD sector field, used for residualization features and sector caps; (b) **sector-hedged residual-dislocation features and a no-news conditioning feature** absorbed from anomaly-regime-meta S1 (which is killed as a standalone sleeve — its next-morning entry forfeits the overnight reversion that XSR's near-close entry captures); (c) fail-closed news blocklist: names with M&A/FDA/offering/litigation headlines (Gemini keyword classes on the Alpaca REST corpus, classification failure = block) are ineligible for the reversal-driven extremes; (d) **whole-share rounding simulated in the backtest**, not footnoted — at ~$340/position the short book is chunky and residual net must be modeled; (e) the 15:45-provisional-features-vs-trained-on-close mismatch gets a **pre-registered quantified ablation** (retrain on synthetic 15:45 closes; if OOS IC degrades > 25%, the production model trains on 15:45-constructed features), not a "documented as noise" waiver.

- **Model:** LightGBM regressor on ranked labels (num_leaves 31, depth 5, min_data_in_leaf 500, L2 10); purged expanding-window walk-forward, monthly retrain, 10-day embargo.
- **Data:** EODHD bulk EOD point-in-time universe 2012→present (D0 layer, §3); Alpaca SIP daily cross-check; FRED; Alpaca news REST for the blocklist.
- **Horizon:** ~5 trading days (hysteresis-extended).
- **Honest net Sharpe:** 0.6–1.1. **Account CAGR contribution:** 4–12% at ~0.8–1.0x account gross.
- **Trades/day:** 10–25 orders.
- **Sleeve capital:** 40% of equity, long-short up to 2x its allocation (~0.8x account gross), executed 15:45–15:58 via marketable limits.

### Sleeve 3 — GAP: Overnight Gap Drift-vs-Fade (the ONE merged gap engine)

Three designs proposed this trade; we build it exactly once, taking the best component from each. Overnight gaps resolve non-randomly: hard-catalyst gaps (earnings, guidance, FDA) drift (PEAD); no-news/soft-news gaps in mid-caps revert (overnight illiquidity + retail open flow). The exploitable object is the classification, learned cross-sectionally. Base implementation = intraday-anomaly Sleeve 2: point-in-time top-600 liquid universe (monthly from EODHD bulk), candidates = |gap| between 1.5x and 6x own 20d vol, **entry at 09:35 after the opening range forms** (never the open print — robust to IEX-vs-SIP basis), features include the first-5-minute tape (or5 return/volume), news flags/recency/FinBERT sentiment from the Alpaca REST corpus only, exit via `cls` auction order at 15:55. Transplants: news-llm-event Sleeve B's signed-regression regime framing and its **never-fade blacklist** (never fade `mna_target`, `fda_*`, or guidance events with quantitative surprise > 5%); anomaly-regime-meta S2's fail-closed news classifier; the **opening-window Corwin-Schultz spread model** (opening spreads are 2–4x midday — using daily-average spreads would fabricate the edge) and the 2x-cost stress gate. The sub-$5M-ADV "fade universe" from news-llm-event Sleeve B is rejected as untradeable under its own cost model.

- **Model:** LightGBM cross-sectional regressor (depth ≤ 4, ≤ 400 trees), top-k/bottom-k daily selection; purged walk-forward, 24-month train window, 10-day embargo, frozen hyperparameters.
- **Data:** EODHD bulk universe; Alpaca SIP minute bars (candidate-days ± 60d only, ~90% volume reduction); Alpaca news REST (timestamp-authoritative); FinBERT + Gemini classification.
- **Horizon:** one session (09:35 → close), flat overnight.
- **Honest net Sharpe:** 0.3–0.9, with a genuine 30–40% probability the honest walk-forward says no edge and it never deploys. **Account CAGR contribution:** 2–6% if deployed.
- **Trades/day:** 4–10 round trips.
- **Sleeve capital:** 25% of equity, up to 1.8x intraday, dollar-balanced long/short preferred.

### Sleeve 4 — CAT: Fresh Catalyst Drift (LLM-differentiated news sleeve, gated hardest)

News-llm-event Sleeve A essentially wholesale — it is the only sleeve where our stack has a real differentiator (LLM event-quality discrimination), and the only one whose backtest corpus is byte-identical to the live feed (Alpaca REST Benzinga history vs the same Benzinga websocket). Websocket story → dedup → FinBERT + entity-masked Gemini structured scoring (event_type taxonomy frozen pre-backtest, materiality, quantitative surprise, novelty-on-the-fly vs trailing 5d same-symbol stories) → hard rule gate (materiality ≥ 6, primary source, |sentiment| ≥ 2, |z_jump| ≥ 2, direction agreement) → LightGBM meta-label filter (trade only P ≥ 0.58). Trade WITH the confirmed reaction; earliest fill = first full minute bar ≥ 60s post-signal (the first-minute move is surrendered by construction); marketable limits capped at IEX quote ± 10–20bps; ATR stops; exit T+1 close (extend to T+3 on continued drift). Two scope corrections from the judges: (1) **LLM cost bomb defused** — Gemini scores only a pre-filtered candidate set (symbol-match + channel/source heuristics + FinBERT screen + price/volume reaction filter first), ~100–300k historical stories rather than millions, cached by (content-hash, prompt-version) in BigQuery; (2) the 1–2M-story embedding backfill is killed — novelty computed on-the-fly for candidates only. Deployment is additionally gated on the **4-week shadow-mode latency reconciliation** (§4, G8) — if measured live websocket latency invalidates the backtest entry assumption, the sleeve dies before its first order.

- **Model:** meta-labeling (frozen rule gate + LightGBM binary filter); purged walk-forward with symbol-blocked folds, 5-day purge/embargo.
- **Data:** Alpaca news REST 2016→present (only timestamp-bearing corpus; EODHD news = count/confirmation features only); Alpaca SIP minute bars; Vertex Gemini Flash + local FinBERT; live Benzinga websocket.
- **Horizon:** ~1.5 days average.
- **Honest net Sharpe:** 0.5–1.1 if it survives; ~30–50% probability the gauntlet or shadow mode kills it (this is the most heavily arbitraged signal class in systematic trading and we are seconds-latency). **Account CAGR contribution:** 2–6% if deployed. The FinBERT-only ablation is the deployable floor number, not the Gemini-enhanced one.
- **Trades/day:** 2–6.
- **Sleeve capital:** 15% of equity, max 6 concurrent positions.

### Overlay (not a sleeve) — HMM Regime Monitor

3-state Gaussian HMM (diagonal covariance) on cross-sectional dispersion, mean pairwise correlation, correlation shock, realized vol, VIX, HY OAS, breadth. **Causally decoded (filtered posteriors only — smoothing is lookahead), hysteresis band (gate on P(stress) > 0.6, release < 0.4), 2-day confirmation, stress-state validated against 2008/2011/2015/2018/2020/2022.** Ships as monitoring only: it is permitted to gate sleeve sizing (XSR × 0.5, GAP disabled in stress) **only after** demonstrating ≥ +0.1 net Sharpe improvement on the affected sleeve's 2016+ walk-forward, per its own pre-registered gate. The tradable rotation leg is killed. Posterior published daily to Firestore + the Telegram digest regardless.

**Combined at full deployment:** ~20–50 orders/day; overnight gross ≈ 1.0x (XSR + CAT), intraday peak ≈ 1.8x — comfortably inside the hard 1.9x overnight / 2.5x intraday guards, with headroom to scale XSR later on demonstrated live edge.

---

## 2. Explicitly rejected ideas (kill list, with reasons)

Unanimous or 2-of-3 judge kills, all adopted:

1. **ONX overnight/intraday tilt** (xs-ml-ranker S3) — self-assessed net Sharpe −0.2 to +0.7 where the cost model IS the strategy; 30 orders/day of activity theater; would force the flagship to degross to fit Reg-T. A certain cost for an expected-zero payoff. (3/3 judges.)
2. **Intraday News-Shock Continuation** (intraday-anomaly S3) — 40–50% self-assessed death probability at the most-harvested point in news alpha; REST-vs-websocket timestamp fiction; duplicates CAT with worse corpus discipline. Its shadow-mode reconciliation tooling survives as shared infrastructure (§4 G8); the sleeve does not. (3/3.)
3. **HMM tradable rotation leg** (anomaly-regime-meta S3) — Sharpe 0.3–0.6 defensive beta-timing that conceptually duplicates the owner's existing 7-sleeve regime bot and exploits no irregularity. Overlay only. (3/3.)
4. **Duplicate gap sleeves** — xs-ml-ranker's NRV gap aspects, news-llm-event Sleeve B as a standalone build, anomaly-regime-meta S2 as a standalone build. One merged GAP engine only; three parallel builds = triple data-snooping surface, zero orthogonal alpha, and they would net against themselves in one account. (3/3.)
5. **Stale/Soft News Fade** (news-llm-event Sleeve C) standalone + the 1–2M-story embedding backfill — decayed 15-year-old Tetlock effect, short leg structurally flattered by paper fills, most expensive build item in the packet for a Sharpe 0.3–0.8 sleeve. Novelty becomes one on-the-fly feature in CAT/GAP. (3/3.)
6. **NRV as implemented** (xs-ml-ranker S2) — trains news-conditioning labels on EODHD historical news whose timestamps are crawl-time: a built-in leakage machine. Concept (no-news fade) survives inside XSR's conditioning and GAP's classifier, on the Alpaca REST corpus. (3/3.)
7. **EODHD "symbol-list sector field"** — does not exist outside the 403'd fundamentals API; wishful data. Replaced by correlation-to-sector-ETF assignment. (2/3 flagged, verified.)
8. **EODHD news as a timestamp-bearing source anywhere** — demoted to count/confirmation features only, permanently. (3/3.)
9. **Hedge/exponentiated-gradient adaptive sleeve reweighting** in year one — 90d realized Sharpe is ~90% noise at these Sharpe levels even clipped. Equal-risk-contribution static allocation until ≥ 6 months of live sleeve PnL. (3/3.)
10. **Full OMS with intent netting, internal crossing, pro-rata attribution as a precondition for first trade** — 8–12 engineering days of cathedral before any congregation. Slimmed to: Firestore ledger + hard pre-trade limits + drawdown ladder + reconciler, in-process. Netting revisited only if ≥ 2 sleeves start overlapping symbols materially. (2/3.)
11. **Isolation Forest / k-means unsupervised gating** — unfalsifiable researcher degrees of freedom "sold as robustness"; default OFF unless an ablation shows it beats plain |resid_z| gating out-of-sample. (2/3.)
12. **NRV/CAT-style ML meta-models trained before their rule cores prove out** — rule-based core must be profitable standalone in walk-forward; ML is pruning, never the load-bearing wall, on few-thousand-event samples.
13. **Kelly sizing at any layer** — with < 1 year of live PnL, Kelly fractions are estimation-error amplifiers (adopted from anomaly-regime-meta's own reasoning).
14. **Sub-$5M-ADV / sub-$5 fade universes** — "less arbitraged" because untradeable at our entitlements; excluded by the cost model that prices them at ≥ 12bps half-spread.
15. **Pre-open IEX quote midpoints as gap-decision inputs across ~500 names** — thin, stale, and the 30-symbol quote-stream cap forces REST sweeps; GAP decides at 09:35 off the realized tape instead.

---

## 3. Shared platform architecture

### 3.1 BigQuery data warehouse (all research and training reads BQ only)

**Dataset `qnt_market`** (raw, append-only, immutable — snapshots never re-downloaded over old partitions):

| Table | Partition | Contents |
|---|---|---|
| `eod_bulk` | date | EODHD bulk EOD, entire US market per historical date (incl. later-delisted names). ~3,800 calls backfill 2012→present. Survivorship-bias foundation. |
| `minute_bars` | date | Alpaca SIP minute bars: full history for the 27 IMOM ETFs; lazily backfilled candidate-days ± 60d for GAP/CAT universes. |
| `news_raw` | date(created_at) | Alpaca REST Benzinga corpus 2016→present: id, created_at (law: never updated_at), symbols[], headline, summary, content_hash, source, channels. EODHD news in a sibling table flagged `timestamp_authoritative=false`. |
| `news_ws_log` | date | Live websocket arrivals: story id, created_at, ws_received_at — the latency-reconciliation evidence base, logged from day one. |
| `fred_series` | — | VIXCLS, T10Y2Y, BAMLH0A0HYM2, DFF; pulled with 1-day lag discipline. |

**Dataset `qnt_features`** (feature store): `xsr_daily_panel` (date-partitioned wide table; every feature computed with data ≤ that date's close; labels live in separate views joined only at train time — never stored beside same-date features), `gap_event_panel`, `cat_event_panel`, `sector_map` (month, symbol, sector_etf, 120d corr — the correlation-based assignment), `llm_scores` (content_hash, prompt_version, model_id, masked flag, structured outputs — cached, reproducible, version-pinned).

**Dataset `qnt_research`**: `trials_ledger` (every configuration ever run: run_id, config_hash, hyperparams, OOS metrics — feeds PBO/DSR accounting in §4), `backtest_folds`, `ablations`.

**Dataset `qnt_live`**: `signals_candidates` (**every candidate logged whether traded or not**, with skip reason — this is what makes live-vs-backtest drift measurable), `orders`, `fills` (with fill-vs-SIP-mid slippage computed after the 15-min embargo), `pnl_sleeve_daily`, `model_registry` (model_id, sleeve, git hash, config hash, train window, feature list, GCS artifact path, validation metrics, activated_at).

### 3.2 Feature store approach
Nightly Cloud Run job (01:30 UTC, after EODHD EOD finality): ingest bulk EOD → rebuild point-in-time universe → compute feature panel → append to BQ. Point-in-time law: the universe for any date is rebuilt from that date's bulk file only; delisting penalty applied conservatively against the position (last close for disappearing longs, −20% penalty whichever side hurts); corporate-action screens on close/adjusted_close ratio jumps. Event panels (GAP/CAT) built from `news_raw` + `minute_bars` with the timestamp law: created_at only, +5s processing latency added to every historical event, earliest fill = first full minute bar ≥ 60s post-signal.

### 3.3 Training cadence and model registry
Monthly retrain (first weekend), expanding window, purged + embargoed, hyperparameters frozen at design freeze (changes require a new `trials_ledger` entry and re-run of the §4 gauntlet). A challenger model activates only if it beats the incumbent on the trailing 12-month OOS window; activation = registry row flip; rollback = flip back. Artifacts in GCS keyed by content hash. LLM prompts version-pinned; all historical classifications cached in `llm_scores` so backtests are bit-reproducible.

### 3.4 Execution engine
- **One always-on Cloud Run service `qnt-market-daemon`** (min-instances 1): holds the Benzinga news websocket (from day one, for CAT later and latency logging immediately) and the IEX minute-bar websocket for the 27 IMOM ETFs plus dynamic event subscriptions. Quotes for order pegging come from **REST latest-quote batch snapshots**, never the quote stream (30-symbol quote/trade channel cap; bar channel is uncapped per judge verification — both re-probed in Phase 0 before any build commitment).
- **Cloud Scheduler decision jobs** hitting the daemon: 09:31–09:35 GAP decision; 15:25 IMOM features; 15:30 IMOM entry; 15:35 XSR scoring; 15:45–15:58 XSR execution window; 15:55 GAP `cls` exits; 16:30 EOD reconcile + PnL + digest; 01:30 UTC ingest.
- **Order lifecycle:** sleeve emits intent → in-process risk module validates against the hard-limit table (below) → order submitted with `client_order_id = QNT-{SLEEVE}-{yyyymmdd}-{seq}` → fill/partial/cancel tracked → Firestore ledger updated transactionally → BQ append → Telegram. Order types: auction orders (`opg`/`cls`, SIP prints) wherever the strategy allows; otherwise marketable limits pegged to REST quotes with capped chase (retry once, then abandon). Never blind market orders at news prints. Never `close_all_positions`.
- **Shared-account hygiene:** hard-coded 23-ticker exclusion set; Firestore ledger is the source of truth; reconciler every 10 minutes compares (Alpaca positions minus the ETF bot's STRATEGY_SYMBOLS) vs the sum of QNT ledgers — drift > 1 share ⇒ Telegram alert + per-symbol trading pause.

### 3.5 State management (Firestore, all `qnt-` prefixed)
`qnt-ledger-{sleeve}` (positions, cost basis, allocation), `qnt-risk-state` (HWM, drawdown, kill flags, gross/net, HMM posterior), `qnt-run-locks` (idempotency, mirroring the existing bot's `quarterly-runs` pattern), `qnt-signals-pending`. Coexists cleanly with the ETF bot's collections.

### 3.6 Hard risk limits (enforced pre-trade, fail-closed)

| Control | Limit | Breach action |
|---|---|---|
| Gross exposure | 1.9x overnight (Reg-T guard), 2.5x intraday | reject risk-increasing orders |
| Net exposure | ±60% equity | reject |
| Per-name | 10% equity; 1 position per name across all sleeves | clip |
| Per-sector (corr-assigned ETF bucket) | 25% net | clip |
| Per-sleeve daily loss | 1.5–2% of account (per sleeve spec) | sleeve stands down for the day |
| Account drawdown from HWM | −8%: all gross × 0.5; −12%: flat everything, halt | automatic; human restart only |
| Data quality | stale EODHD (> 26h), quote staleness, clock skew, classifier failure | affected sleeve stands down (fail-closed, matching the owner's existing margin-gate philosophy) |
| Order rejects > 5% / slippage EWMA > 2x cost model | — | halt sleeve / de-risk warn |

### 3.7 Monitoring, Telegram, kill switches
Telegram (existing pattern): every order, fill, reject, risk event, kill trigger, staleness stand-down; nightly digest (per-sleeve PnL, exposure, live hit-rate vs backtest, slippage vs cost model, regime posterior, allocation). Weekly human review notebook from BQ: fill-slippage distributions, model calibration curves, feature-drift PSI vs training window, candidate-vs-traded attribution. Kill-switch layers: (1) fail-closed data gates; (2) per-position stops; (3) per-sleeve daily loss limits; (4) account drawdown ladder; (5) manual halt flag in `qnt-risk-state` checked before every order, settable via a Telegram command; (6) pre-registered per-sleeve live kill criteria — a sleeve that trips its kill rule twice is **retired, not retuned**; reinstatement is a human decision documented in the registry.

---

## 4. Validation gauntlet (every sleeve, pre-registered before its backtest runs)

A sleeve deploys to paper only if it passes **all** gates. A sleeve failing any gate is reported dead — not tuned until green.

- **G1 — Data integrity:** point-in-time universe from bulk snapshots; conservative delisting penalties; whole-share rounding and short-side fractional restrictions simulated (not footnoted); borrow haircut 2%/yr on shorts + hard HTB-proxy exclusions.
- **G2 — Walk-forward performance:** purged expanding-window walk-forward, ≥ 6 out-of-sample folds (annual, 2017→2025 minimum; 2016→ for minute-bar sleeves), purge ≥ 2x label horizon, embargo ≥ 5 trading days. **Concatenated OOS net Sharpe ≥ 0.5** at the base cost model (opening-window spreads for open-adjacent sleeves, liquidity-bucketed half-spreads elsewhere).
- **G3 — Cost stress:** at **2x the full cost model**, net Sharpe ≥ 0.25 and cumulative net PnL > 0. For post-publication anomalies (IMOM explicitly): positive net edge in the **2022–2025 subsample alone at 2x costs**.
- **G4 — Fold consistency:** **positive net PnL in ≥ 60% of walk-forward folds**; no single fold contributes > 40% of cumulative net PnL; survivable (> −10% on sleeve capital) in each of 2018, 2020, 2022; PnL reported with top-5 days removed.
- **G5 — Overfitting statistics:** **CSCV probability of backtest overfitting (PBO) < 20%**; **Deflated Sharpe Ratio > 0 at 95% confidence**, computed against the full `trials_ledger` count of configurations ever run for that sleeve (every run is logged; there is no untracked search).
- **G6 — Leakage battery:** shuffled-label test collapses Sharpe to |t| < 1; extra-lag test degrades Sharpe smoothly and materially (< 20% degradation on a 1-day lag for a short-horizon signal ⇒ halt and audit for leakage; a cliff at minutes ⇒ harvesting the untradeable move); prediction-decile monotonicity Spearman ≥ 0.8; intraday triggers must survive ±30s timing / ±5bps price jitter with < 25% edge change.
- **G7 — Baseline dominance:** the ML layer must beat its pre-registered rule-only baseline by **≥ 0.1 net OOS Sharpe**, else the rule deploys and the model does not. (GAP's null is the two-rule "fade small-cap soft gaps / follow large-cap hard gaps" baseline; IMOM's is the raw 25bps rule; CAT's rule gate must be profitable standalone.)
- **G8 — News-sleeve addenda (GAP news features, CAT):** timestamp law compliance (created_at only, +5s latency, ≥ 60s entry delay); randomized-sentiment/news-flag null distribution — realized Sharpe > 95th percentile of ≥ 200 null runs; entity-masked vs unmasked Gemini audit on ≥ 2,000 stories with the **FinBERT-only ablation as the deployable floor number**; **mandatory 4-week shadow mode** logging live websocket arrival vs REST timestamps, backtest re-run with the measured latency distribution — all gates must still pass, and median measured latency > 3 min kills the sleeve.
- **G9 — Short-book realism:** if > 60% of net alpha comes from shorts in < $50M-ADV names, reject the model version; short-leg results reported with the borrow haircut and flagged as paper-flattered.
- **G10 — Live burn-in:** first 20 trading days at 25% of target sleeve size; live fill slippage ≤ 1.5x cost model and live hit-rate/IC within 2σ of backtest, else automatic de-risk to zero and post-mortem before scale-up.

**Post-deployment kill rules (standing):** trailing-60d live IC/hit-rate 3σ below backtest ⇒ halve; 20 consecutive days of negative trailing live IC ⇒ halve; sleeve 60d PnL < −2.5x its allocated vol budget ⇒ allocation to zero, signals shadow-tracked; two kill trips ⇒ retired.

---

## 5. Build roadmap

**Phase 0 — Foundations (days 1–5), buildable immediately:**
BQ datasets + tables above; EODHD bulk backfill 2012→present (~3,800 calls, well under quota); Alpaca SIP minute backfill for the 27 IMOM ETFs; Alpaca news REST backfill 2016→present into `news_raw`; FRED puller; entitlement probes re-verified in code (bar-channel symbol caps, `opg`/`cls` TIF acceptance on paper, quote REST batch limits); Firestore ledger + risk module + reconciler + Telegram skeleton; market daemon deployed with news websocket logging into `news_ws_log` (shadow-mode evidence starts accruing now, months before CAT needs it). **Deliverable:** populated warehouse, running reconciler, first Telegram digest.

**Phase 1 — IMOM (days 3–14, overlapping):**
Feature/label pipeline; rule backtest; walk-forward harness (the shared harness all sleeves reuse); meta-filter; §4 gauntlet run; execution path (15:25 cron → 15:30 entry → `cls` exit). **Deliverable: first live paper orders ~day 12–15**, proving the entire platform. Gate: G3's 2022–2025-at-2x-costs subsample.

**Phase 2 — XSR (days 10–30):**
Point-in-time universe builder; correlation sector map; 24-feature panel; LightGBM + purged CV; cost model; whole-share simulation; 15:45-features ablation; sanity battery; gauntlet; execution window integration. **Deliverable: flagship live ~day 30** at burn-in size. In parallel: GAP's candidate-day minute-bar ingestion starts.

**Phase 3 — GAP + HMM monitor (days 30–48):**
Gap event panel; news joins (REST corpus); FinBERT batch + Gemini classifier with 200-headline hand-labeled eval set; opening-window spread model; gauntlet incl. the two-rule null; 09:35 executor + `cls` exits. HMM monitor (4–6 days) ships monitoring-only, posterior in the digest, its +0.1-Sharpe overlay gate evaluated on XSR's completed walk-forward. **Deliverable: GAP live ~day 45–48 if and only if gates pass** (30–40% chance they don't — that outcome is reported as a result, not a failure).

**Phase 4 — CAT (days 40–70):**
LLM scoring pipeline with pre-filter tiering, masking harness, cached scores; event backtest on the REST corpus; meta-model; full gauntlet incl. G8 nulls and contamination audit; 4 weeks shadow mode (latency log already months deep by now); live only after shadow reconciliation passes. **Deliverable: CAT live ~day 65–70 or a documented kill.**

**Phase 5 — Ongoing operations:**
ERC allocation across live sleeves (bootstrap: IMOM 20 / XSR 40 / GAP 25 / CAT 15); monthly retrains; weekly review notebook; 60-day per-sleeve evaluations against pre-registered kills; adaptive allocation reconsidered only after ≥ 6 months of live sleeve PnL; HMM overlay activation decision after its gate evaluation; only after 6 months of positive live combined Sharpe do we discuss leverage or new sleeves (minute-bar microstructure, dynamic-subscription news variants).

---

## 6. Honest overall expectation — and the 50%/yr question

**Central estimate for the combined v1 system at paper scale:** net Sharpe **0.7–1.2**, net CAGR **8–20%** on the ~$9k account at ≤ 1.6x average gross, max drawdown 8–15%, ~20–50 orders/day at full deployment. There is a **25–35% probability the whole system is approximately flat after honest costs** — all four designers and all three judges independently converged on this band, and I endorse it. Two structural caveats stack on top: paper fills flatter results by an estimated +2–6% CAGR versus live (no borrow fees, no buy-ins, optimistic fills), and one or two of the four sleeves (GAP, CAT most likely) may never clear the gauntlet at all — the roadmap treats a documented kill as a successful outcome of the research process.

**On 50%/yr:** the aspiration is not supported by the evidence, and I will not represent otherwise. The arithmetic: 50%/yr at a survivable ~18–20% vol requires a sustained net Sharpe of ~2.5–3.0. Institutional stat-arb desks with SIP execution, sub-millisecond latency, borrow desks, and alternative data run 1.5–3.0 gross before leverage; we operate at seconds-latency on a free IEX feed with daily/minute bars and no fundamentals. Alternatively, 50% from a Sharpe ~1.0 book requires ~4x permanent leverage, which is ruinous drawdown math (a routine 2-sigma month is a −25%+ account event) and violates Reg-T overnight anyway. Every backtest that appears to promise 50%/yr from this data will be overfitting, and the gauntlet in §4 exists precisely to catch it. The honest path toward higher returns is sequential: (1) prove positive live paper Sharpe over 6+ months against the pre-registered gates; (2) add genuinely orthogonal sleeves on the proven platform; (3) only then scale gross exposure on demonstrated live edge — a well-executed end state for this program is a live Sharpe ≥ 1 system compounding 15–25% in good years, which at this account size is an excellent research outcome and the strongest possible foundation for ever deploying real capital. This framing goes verbatim into every report to the account owner.
