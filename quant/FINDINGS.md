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

**BEFUND 1 (meine erste Darstellung war falsch, korrigiert) — TEILFÜLLUNGEN.**
Ich hatte geschrieben, Alpaca markiere gefüllte Orders als "expired". Falsch.
Die Prüfung der BESTELLTEN Menge zeigt die banale Wahrheit: es sind
Teilfüllungen, und `expired` bezieht sich auf den nicht ausgeführten REST —
völlig normales Verhalten, kein Bug:
    AEM  2 bestellt →  1 gefüllt      DRN  79 bestellt → 29 gefüllt
    AR  16 bestellt →  2 gefüllt      UDOW 13 bestellt →  6 gefüllt
    ASST 40 bestellt → 18 gefüllt     DPST  6 bestellt →  0 gefüllt
`status="filled"` gibt es nur bei 100 % (YINN/DFEN/CURE).
Unser Code-Bug war trotzdem echt: Reconcile UND Kostenmonitor filterten auf
`status == "filled"` und übersahen damit alle Teilfüllungen → Ledger blieb
leer trotz 10 offener Positionen. Gefixt via `broker.sleeve_fills_today()`
(prüft `filled_qty`, nie `status`).

**DAS EIGENTLICHE PROBLEM — Füllquote.** ONX 61 %, **XSR 36 %** der bestellten
Stück. Ein Sleeve, der nur ein Drittel seines Zielbuchs aufbaut, handelt eine
andere Strategie als die getestete. Die Füllquote ist damit die wichtigste
Burn-in-Kennzahl und ab jetzt die Hauptmetrik des Kostenmonitors (Alarm <50 %).
Ursache offen: Alpacas Paper-Engine modelliert Auktionsliquidität grob; echte
MOO/MOC-Orders dieser Größe (1-79 Stück) würden real vollständig füllen.

**BUG 2 (gefährlich, vor dem ersten Montag gefunden) — Verdopplungsrisiko.**
Weil das Ledger leer blieb, hätte `xsr_live.execute()` am Montag
`delta = target − 0` gerechnet und die bestehenden Positionen VERDOPPELT.
Gefixt: `execute()`/`rebalance()` lesen jetzt die echten Broker-Positionen
(gefiltert auf das persistierte `symbol_universe` des Sleeves) statt des
Ledgers. Verifiziert: XSR erkennt AEM 1 / AR 2 / ASST −18 und bildet korrekte
Deltas (ASST −22 statt −40).

**MESSGRENZE — Auktionskosten sind im Paper-Konto nicht messbar.** Slippage
gegen den offiziellen Print: ONX (cls) 9,9bp (nur Fills ≥500$), XSR gar nicht
auswertbar (alle Fills zu klein). Die zuerst berichteten 53,6bp für XSR waren
Tick-Rauschen auf 1-2-Stück-Teilfüllungen und sind zurückgezogen; der Monitor
rechnet Slippage jetzt nur über Fills ≥500$. Beide Datenquellen (EODHD, Alpaca-SIP) stimmen exakt
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

## Familie 16 (DTRD) VALIDIERT — Cross-Asset-Trendfolge über ETFs

Mechanismus: Risikotransfer-Gebühr an Hedger (Rohstoff-Produzenten,
Duration-Hedger) + langsame Makro-Diffusion. KEIN Informationsvorsprung →
verfällt nicht durch Publikation (anders als IMOM/GAP/PEAD).
Universum: 30 ETFs Anleihen/Rohstoffe/Währungen/Intl-Aktien/Immobilien —
bewusst OHNE US-Aktien und OHNE Krypto. Long/flat, Vol-Target 10 %, MONATLICH.

| | Sharpe | CAGR | MaxDD |
|---|---|---|---|
| Training 2004–2019 (Lookback 126d gewählt) | 0.90 | +5.1% | −10.5% |
| **HOLDOUT 2020–2026 (nie gefittet)** | **0.43** | **+3.1%** | −15.0% |
| Gesamt 2004–2026 | 0.73 | +4.5% | −15.0% |

Der Holdout-Wert 0.43 deckt sich exakt mit der vorab genannten Erwartung
(0.35–0.45) und mit den Live-Zahlen der Industrie (SG CTA Index Sharpe 0.61
seit 2000, DBMF 2020–24 ≈ 0.35–0.40). Kein Overfit-Verdacht.
**DSR 0.988 → besteht G5** (enger vorregistrierter Variantensatz, SR* 0.25).
**Orthogonalität exzellent: ρ(XSR) = −0.001, ρ(ONX) = +0.041, ρ(VOLC) = +0.353.**
Monatliche Umschichtung → kostenimmun gegen den Wall, der IMOM/GAP/CAT/PEAD
getötet hat.

PORTFOLIO-WIRKUNG, ehrlich: 4 → 5 Sleeves hebt S_p nur von 0.81 auf 0.85
(+0.03), weil DTRDs Sharpe (0.43) UNTER dem Stack-Mittel liegt. CAGR@24 % Vol
19.5 % → 20.3 %. Ein orthogonaler Sleeve mit unterdurchschnittlichem Sharpe
verbessert die Robustheit, nicht die Rendite.

**Arithmetik nach 16 Familien:** 5 validierte Sleeves, Mittel-Sharpe 0.48
(aktuelles Regime). Für 50 % CAGR bei tragbarem Risiko braucht es S_p ≈ 1.8 —
bei diesem Qualitätsmittel wären das ~14 Sleeves. Trefferquote 5/16 = 31 % →
~29 weitere Familien zu testen. Das ist der ehrliche Weg, und er ist lang.

## Discovery-Pipeline + Familien 17–19 + korrigierte Portfolio-Arithmetik (2026-07-25)

### Was jetzt läuft
Die Pipeline ist geschlossen: `quant/research/discovery.py` schickt jeden
Kandidaten aus `hypothesis_queue.yaml` durch **G0–G8** (Abbruch beim ersten
Fehlschlag, billige Gates zuerst). Neu und entscheidend ist **G8 (Live-Pfad)**:
ohne importierbare `live_signal`-Funktion, die ein Gewichts-Dict mit Gross ≤ 1.0
zurückgibt, kann ein Kandidat nicht befördert werden. Beförderung schreibt nach
`quant/sleeves/promoted.yaml`, das `registry.py` beim Import liest — damit ist
"validiert" nicht mehr von "handelbar" trennbar (die Lehre aus VOLC/EOMT).
In der Cloud läuft der Task `discovery` sonntags 19:00 NY im **Melde-Modus**:
Gates rechnen, Telegram meldet, Beförderung bleibt ein Commit.

### Drei Familien getestet, drei verworfen
| Familie | Ergebnis | Gescheitert an |
|---|---|---|
| **RESID-MR** (17) | Voll −0.30, Holdout −0.14, 2022+ −0.41; alle 3 Horizonte negativ | G3; Vorhersage (c) verletzt (residual schlechter als roh) |
| **ACT13D** (18) | abnormal vs. SPY −0.83 % (21T) → −15.59 % (252T, t=−25.4) | G2; Vorhersagen (a) und (c) widerlegt |
| **CARRY** (19) | Voll 0.58, Holdout 0.39, 2022+ 0.52 — aber ρ(DTRD) = **+0.78** | G5 (DSR 0.902) **und** G7 (ΔS_p +0.010) |

ACT13Ds negatives Vorzeichen ist mit hoher Wahrscheinlichkeit
**Benchmark-Fehlspezifikation**, nicht Wertvernichtung durch Aktivisten:
13D-Ziele sind Small-Cap-Value (Median-ADV im untersten Tier $0.0M), und
"abnormal vs. SPY" misst über 2007–2026 vor allem den Small-minus-Large-Spread.
Daraus die neue Regel **R9**: Event-Studien gegen ein größen-/stilbereinigtes
Benchmark, nie gegen SPY allein.

Nebenprodukt: `sec_13d_filings` in BigQuery, 12.483 initiale 13D + 41.000
Änderungsmeldungen, 2007-01 bis heute, mit täglichem Refresh.

### Vier eigene Rechenfehler, die die Basis verzerrt hatten
1. **`portfolio_delta` benutzte die Durchschnittsformel** S̄·√(N/(1+(N−1)ρ̄)),
   die Gleichgewichtung *und* einen einheitlichen ρ voraussetzt. Dadurch bekam
   ACT13D (ρ≈0, Sharpe 0.50) ein **negatives** ΔS_p — mathematisch unmöglich,
   die quadratische Form ist monoton. Jetzt S_p = √(sᵀC⁻¹s), long-only, 30 %
   geschrumpft.
2. **EOMT ist eine Monatsreihe**, wurde aber mit √252 annualisiert: Sharpe
   **2.36 statt 0.52** (Faktor 4.6) — und dominierte damit jede
   Portfolio-Rechnung.
3. **Korrelationen standen teils auf 22 gemeinsamen Beobachtungen.** Jetzt auf
   Monatsrenditen, erst ab 36 gemeinsamen Monaten, sonst Prior 0.20; negative ρ
   auf 0 gekappt.
4. **Der DSR deflationierte mit dem globalen Versuchszähler** (54 Versuche über
   18 unabhängige Familien) statt mit den familieninternen. Korrigiert:
   XSR 0.458 → **0.804**, DTRD 0.790 → **0.861**, EOMT 0.963 → **0.976**,
   ONX 0.374 → 0.650, VOLC 0.141 → 0.724. Das **löst das offene XSR-Rätsel**
   (DSR 0.561) aus ROAD_TO_50 §0.3. Der Programm-Selektionseffekt wird separat
   als Trefferquote ausgewiesen (5 von 18 Familien).

### Die Zahl, die die Suchstrategie ändert
`quant/research/portfolio_math.py`: S_p konvergiert für N→∞ gegen **S̄/√ρ̄**.
Bei heute Ø-Sharpe 0.51 und ρ̄ = 0.16 ist das **1.25** — eine Rendite-Obergrenze
von **≈27 %/Jahr unter Reg-T, unabhängig von der Zahl der Sleeves.**

| zusätzliche Sleeves heutiger Qualität | N | S_p | Rendite |
|---:|---:|---:|---:|
| +5 | 10 | 1.02 | +21.8 % |
| +50 | 55 | 1.20 | +25.6 % |

50 % brauchen S_p ≥ 2.34, also Ø-Sharpe ≥ 0.94 (Verdoppelung der Qualität)
ODER ρ̄ ≤ 0.047 (ein Drittel des heutigen) ODER Hebel 4.3× (2.3× über Reg-T).
Daraus Regel **R8**: nach Qualität und Orthogonalität suchen, nicht nach Anzahl.
**Ehrliche Erwartung unverändert-präziser: 15–25 %/Jahr, Mittelfall 22 %.**

### Zwei stille Datenfehler
- **`daily.sh` behauptete im Kommentar "FRED refresh", das `--fred`-Flag fehlte
  aber** — `fred_series` stand 15 Tage still. Aufgefallen, weil CARRY den
  Geldmarktsatz braucht. Zusätzlich 13D-Refresh und Insider in den Tagesloop.
- **Die SEC hat mit der XML-Pflicht (Dez. 2024) "SC 13D" zu "SCHEDULE 13D"
  umbenannt.** Ein Filter auf das alte Label sieht ab 2025 null Events und läuft
  grün weiter. `FRED_RE` in `sec_13d_ingest.py` kennt beide Labels.
- Randnotiz: FRED hat `EVZCLS` (EuroCurrency-Vol) im März 2025 eingestellt —
  Upstream-Abkündigung, kein Fehler bei uns.

### Deployment-Lehre
Der erste Deploy der Pipeline scheiterte am fehlenden PyYAML im Cloud-Image.
Der eigentliche Befund war schwerer: `registry.py` importiert yaml auf
Modulebene, also hätte derselbe Fehler den Lauf `quant-etf-rebalance` am selben
Tag um 15:47 NY **komplett abgebrochen** — VOLC/EOMT/DTRD hätten nicht
gehandelt. Behoben, und der yaml-Import ist jetzt gekapselt: fehlt das Paket,
entfällt nur das befördertes Register. **Neue Cloud-Tasks vor dem
Handelsfenster smoke-testen, nicht danach.**

### Weiter offen
- **ONX-Round-Trip-Kosten**: 14 Tage Burn-in ergaben nur 7 ONX-Fills (4 über
  der 500-$-Messschwelle, 9.9bp Entry-Slippage) und 3 XSR-Fills. Für eine
  getrennte `opg`-Exit-Messung reicht das nicht — braucht mehr Burn-in-Zeit,
  ist nicht durch Rechnen lösbar.
- **MERGARB** (Merger-Arbitrage) ist der einzige eingereihte Kandidat, der R8
  strukturell erfüllen kann: die Auszahlung hängt am Deal-Ausgang, nicht an
  Markt-/Zins-/Vol-Faktoren, also ρ nahe null zu allen fünf bestehenden
  Sleeves. Braucht einen Deal-Ingester (SEC 425 / DEFM14A / SC 14D9 mit
  LLM-Extraktion der Deal-Terms) nach dem Muster von `sec_13d_ingest.py`.
- **BAB** nach R8 ohne Test zurückgestellt (erwartetes ΔS_p ≈ +0.01, dieselbe
  Größenordnung wie das bereits gescheiterte CARRY).
