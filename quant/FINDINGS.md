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

## CORRECTION (2026-07-11 afternoon): the search space was NOT exhausted

A systematic sweep of current Alpaca docs (prompted by the account owner)
found capabilities the original program missed — the venue landscape itself
changed in 2026:

1. **24/5 overnight session (Blue Ocean ATS)** — live since ~Feb 2026,
   enabled by default, ONX universe is `overnight_tradable`. Free real-time
   `overnight` feed; BOATS historical bars from 2026-01. A five-month-old
   venue is the least-arbitraged surface available, and our Benzinga corpus
   covers the same window → overnight news-reaction strategies (act at 21:00
   ET) are testable for the first time. Study: `overnight_session_study`.
2. **Options Level 3 on paper** with free greeks/IV chain snapshots
   (indicative feed) + bars/trades history since 2024-02 (~2.4y). Defined-
   risk spreads = non-Reg-T leverage. No historical quotes exist anywhere →
   the chain-snapshot archiver (started 2026-07-11) becomes proprietary
   backtest data.
3. **Historical auctions endpoint** (official MOO/MOC prints) → ONX backtest
   fidelity upgrade. **Corporate-actions API** (recent years) + own warehouse
   → reverse-split-drift / spin-off event studies. **Screeners** solve the
   30-symbol websocket cap. **PDT abolished** — no day-trade throttling.

Status: three genuinely new, honest strategy families queued (OVN overnight
session, OPT defined-risk vol, event studies). None validated yet; the
50%/yr verdict below stands **for the space tested so far** and is now
conditional rather than terminal.

## Second wave (2026-07-11 afternoon): families 11–12, model zoo, v2, deployment

- **XSR v2 (fundamentals features)**: at 50% coverage Sharpe 0.50→0.54,
  CAGR +9.7%→+10.5%, G3 2x-cost stress 0.20→0.25 (now at the bar). Final
  full-coverage number pending overnight quota reset. **Data beat models.**
- **Model zoo** (identical purged folds 2019–2026): GBM Sharpe 0.49 vs
  Ridge 0.03, torch-MLP −0.38, ensemble 0.11. Model class is NOT the
  bottleneck; question closed.
- **PEAD** (family 11, real earnings dates + SUE, 102k liquid events):
  L/S 20d spread +1.64% (2003–09) → +0.1–0.3% (2016–26). **KILLED**; SUE
  stays as an XSR feature.
- **Options premium selling** (family 12, weekly 2%-OTM put spreads
  SPY/QQQ 2024–26, punitive costs): 83% win rate, −4.3% P&L/risk avg —
  tail weeks exceed premia. **KILLED** as designed; chain-snapshot archive
  compounds toward a future quote-accurate study.
- **Deployment layer complete**: ONX executor (cls/opg), XSR executor (opg,
  equity-scaled sizing, tranche-band turnover control), ops/daily.sh
  pipeline. Both executors dry-run tested against the live paper account.

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


## XSR v2 FINAL (75% Fundamentals-Abdeckung, 2026-07-12)

Nach vollständigem Fundamentals-Backfill (4.420 Symbole):
- **net@5bp: Sharpe 0.69, CAGR +14.4%** (v1 ohne Fundamentals: 0.50/+9.7%; v2@50%: 0.54/+10.5%)
- net@10bp (2x-Kosten-Stress): Sharpe 0.39, +6.7% — passt G3 jetzt KOMFORTABEL (war grenzwertig)
- gross Sharpe 0.98; 75% positive Jahre; Alpha faktorrein (FF5+Mom, t=2.19)
- **Bestätigt die Deep-Research-These empirisch: Daten (Fundamentals) schlugen jeden Modelltausch.**
  Sharpe 0.50→0.69 durch Daten; 6 Modellklassen brachten 0.

## Horizont-Experiment (Deep-Research #1-Befund)

| h | Sharpe@5bp | Sharpe@10bp | Turnover |
|---|---|---|---|
| 5d | 0.61 | 0.32 | 0.55 |
| 10d | 0.56 | 0.40 | 0.28 |
| 21d | 0.52 | 0.44 | 0.14 |

Ranking kippt bei der Kostenannahme: 5d gewinnt bei 5bp, 21d bei 10bp
(4x weniger Turnover). Deploy 5d, 21d als kostenrobuster Fallback — Burn-in
misst echte Fills als Entscheider.

## TimesFM / Foundation-Models — VERWORFEN (2026-07-25)

Zwei unabhängige Recherchen + lokaler Pilot auf unseren Daten. Verdikt: nein.
- Korpus (Wikipedia-Pageviews, Google Trends) trägt KEINE Marktinformation;
  was drin ist (M4-Finance, FRED-MD via LOTSA) ist Kontamination, kein Signal.
- Definitive Studie (Rahimikia et al. 2511.18578, 18.14M Beobachtungen, 10k
  Titel, OOS 2001-2023): TimesFM-500M zero-shot R²_OOS **-2.80%**, Rendite
  -1.47%. Chronos -1.37%, Moirai-2 -1.91%, Toto -114%. Dezil-Spreads oft
  INVERTIERT (Chronos-Bolt-Tiny: bestes Dezil -29%, schlechtestes +52%) →
  die Modelle sind naive Momentum-Extrapolatoren, Tagesrenditen sind Reversal.
- Eigener Pilot (kontaminationsfrei 2025-09..2026-07): IC -0.052,
  rank-corr -0.18 zu 21d-Momentum → schwacher Reversion-Tilt, redundant zu
  unserem Reversal-Feature.
- Kovariaten-Schnittstelle ist wörtlich Ridge (`xreg_lib.py`) → wir haben
  Ridge mit Sharpe 0.03 gemessen. Univariat, kein Querschnitts-Mechanismus.
  Kein MPS-Support. Alles vor Nov 2023 kontaminiert.
- WARNUNG aus derselben Studie, die uns betrifft: bei 20bps Kosten gehen ALLE
  Modelle inkl. Gradient-Boosting-Baseline Sharpe-NEGATIV (CatBoost 6.46 →
  -3.13). Das ist eine Aussage über tägliches Querschnitts-Trading, nicht über
  TSFMs. Unsere 5-Tage-Tranchen senken den Turnover 4x — deshalb überleben wir
  bei 5bps (0.60) und marginal bei 10bps (0.22). Bei 20bps wären wir negativ.
  Der Burn-in muss die effektiven Kosten messen; XSRs Lebensfähigkeit hängt
  daran.
- Einzige verwertbare Erkenntnis: Log-HAR schlägt 8 von 9 TSFMs auf
  realisierter Vol → daraus folgte der HAR-Test unten.
- Falls je ein finanz-natives Modell getestet wird: Kronos (MIT, 102M, native
  MPS) oder FinTexts 360 Apache-2.0-Checkpoints — NICHT TimesFM.

## HAR-RV Volatilitätsprognose (2026-07-25) — H1 stark, H2 marginal

Log-HAR (1d/5d/22d Parkinson-RV, expanding-window OLS, 8.9M Zeilen):
- **H1 BESTÄTIGT, überwältigend**: Rank-IC der Forward-5d-Vol 0.849 (Log-HAR)
  vs 0.744 (Trailing-21d), Differenz +0.105, **t = +93.2** über 5.598 Tage.
  HAR sagt Volatilität deutlich besser vorher — robustes Messergebnis.
- **H2 grenzwertig**: HAR-Vol statt vol_63d in der Inverse-Vol-Gewichtung
  hebt Sharpe 0.604 → 0.636 (Δ+0.032), genau auf der vorregistrierten
  Schwelle. **DSR 0.418 → fällt durch** (12 XSR-Varianten, SR* 0.68).
- ENTSCHEIDUNG: HAR-Vol wird als RISIKOMODELL übernommen (bessere
  Vol-Schätzung ist ein Messfortschritt, kein gefitteter Alpha-Parameter,
  t=93 ist eindeutig) — aber KEIN Sharpe-Gewinn wird behauptet, solange der
  DSR nicht besteht.

## ONX-Kostensensitivität (2026-07-25) — der Sleeve ist knapper als gedacht

Anlass: Live-Kostenmonitor misst an den ersten echten Fills 10.0bp Slippage
gegen den offiziellen Schlussprint (YINN 3.6 / DFEN 9.2 / CURE 16.8bp,
notional-gewichtet 10.0bp). ONX rechnete mit 4bp ROUND-TRIP.

**Break-even-Kosten im aktuellen Regime (2022-2026): 8.3-9.8bp Round-Trip.**
Das Brutto-Edge beträgt nur 8.3bp/Tag (Top-8) bzw. 9.2bp (Top-28).

Sharpe-Matrix (Universum × Round-Trip-Kosten), Regime 2022-2026:
| Top-N | 4bp | 10bp | 20bp | 30bp |
|---|---|---|---|---|
| 5 | 0.28 | -0.10 | -0.74 | -1.38 |
| **8 (live)** | **0.30** | **-0.12** | **-0.82** | **-1.52** |
| 15 | 0.44 | -0.02 | -0.79 | -1.55 |
| 28 | 0.42 | -0.06 | -0.86 | -1.66 |

DREI HARTE SCHLUSSFOLGERUNGEN:
1. **Der Liquiditätsfilter rettet ONX NICHT.** Break-even ist bei Top-5
   (8.4bp) praktisch identisch zu Top-28 (9.2bp) — das Brutto-Edge skaliert
   nicht mit Liquidität, nur die Kosten. Die naheliegende Rettung existiert
   nicht.
2. **Schon bei 10bp Round-Trip ist ONX im aktuellen Regime tot** (-0.12).
   Die 4bp-Annahme des Backtests war der einzige Grund für Sharpe 1.06.
3. **Alpacas Paper-Engine füllt MOC-Orders NICHT zum Auktionsprint.** Unsere
   Kauforders lagen systematisch ÜBER dem offiziellen Schluss. Bei
   Ordergrößen von ~$900 in Namen mit $10M+ ADV (0.01% des Tagesvolumens)
   ist echter Market Impact ≈ 0 — die gemessene Slippage ist also mindestens
   teilweise Simulator-Artefakt, nicht Marktrealität. Das ist gleichzeitig
   eine gute und eine schlechte Nachricht: der Paper-Burn-in untertreibt ONX
   möglicherweise systematisch, aber wir können die echten Kosten im
   Paper-Konto GAR NICHT sauber messen.

KONSEQUENZ: ONX bleibt live (Burn-in-Größe 25%), aber die Allokation wird
NICHT erhöht, bis die Round-Trip-Kosten über ≥30 Fills belastbar geschätzt
sind. Der ehrliche Erwartungswert liegt zwischen Sharpe 0.30 (bei 4bp) und
-0.12 (bei 10bp) im aktuellen Regime — die Bandbreite ist größer als der
Sleeve selbst. Alle Auktions-Backtests des Programms (ONX, EOMT, VOLC) teilen
diese Kostenannahme und sind entsprechend zu relativieren.

## Paper-Umgebung: zwei Bugs und eine Messgrenze (2026-07-25, Markt geschlossen)

Der Burn-in ist Fr 2026-07-24 tatsächlich angelaufen (ONX 7 Fills, XSR 3 Fills,
Equity 100.015 $). Die Prüfung am geschlossenen Markt fand drei Dinge:

**BUG 1 — Alpaca markiert gefüllte Auktionsorders als "expired".** Verifiziert:
7 von 10 echten Fills trugen `status=expired` mit `filled_qty>0` (z.B. DRN:
29 Stück @ 11,83 gefüllt, Status expired). Unsere Reconcile-Funktionen UND der
Kostenmonitor filterten auf `status == "filled"` → 70 % der Fills unsichtbar,
Ledger blieb leer, während 10 Positionen offen waren. Gefixt: es wird nur noch
auf `filled_qty` geprüft (`broker.sleeve_fills_today()`).

**BUG 2 (gefährlich, vor dem ersten Montag gefunden) — Verdopplungsrisiko.**
Weil das Ledger leer blieb, hätte `xsr_live.execute()` am Montag
`delta = target − 0` gerechnet und die bestehenden Positionen VERDOPPELT.
Gefixt: `execute()`/`rebalance()` lesen jetzt die echten Broker-Positionen
(gefiltert auf das persistierte `symbol_universe` des Sleeves) statt des
Ledgers. Verifiziert: XSR erkennt AEM 1 / AR 2 / ASST −18 und bildet korrekte
Deltas (ASST −22 statt −40).

**MESSGRENZE — Auktionskosten sind im Paper-Konto nicht messbar.** Slippage
gegen den offiziellen Print: ONX (cls) 8,3bp, **XSR (opg) 53,6bp** (AEM 31,
AR 88, ASST 58bp). Beide Datenquellen (EODHD, Alpaca-SIP) stimmen exakt
überein, es ist kein Datenfehler. Aber: die AEM-Order war **1 Aktie** in einem
Titel mit Millionen Stück Tagesvolumen — echter Market Impact ist exakt null.
Alpacas Paper-Engine simuliert die Auktion also gar nicht, sondern füllt
spread-gekreuzt. KONSEQUENZ: Das G10-Gate (Live-Slippage ≤1,5× Modell) ist im
Paper-Konto für Auktionsorders NICHT auswertbar und wird immer Alarm schlagen —
nicht weil die Strategie schlecht ist, sondern weil der Simulator grob ist.
Der Kostenmonitor bleibt aktiv (er misst Trends und findet Ausreißer), aber
seine Absolutwerte sind eine OBERGRENZE, keine Kostenschätzung. Die belastbare
Burn-in-Kennzahl ist deshalb der Vergleich realisierter Sleeve-P&L gegen die
Backtest-Erwartung — nicht Fill-vs-Print.
