I have the grounding I need. Writing the document now.

# ROAD_TO_50 — Entscheidungsdokument des Head of Quant Research
**Datum 2026-07-24 · Konto PA3IN7QIGPSE (Alpaca Paper, $100k) · Burn-in Tag ~8**

---

## 0. LAGE: Die Basis im Briefing ist zu hoch — und zwei unserer eigenen Gates sind offen

Ich habe vor der Bewertung der Kandidaten unsere eigenen Zahlen nachgerechnet. Drei Korrekturen, die jede Zahl in diesem Dokument verändern:

### 0.1 Korrigierte Sleeve-Tabelle

| Sleeve | Briefing | Repo-Wahrheit | Beleg |
|---|---|---|---|
| XSR | 0.69 / +14.4% | 0.69 @5bp, **0.39 @10bp**; nach dem **in G1 vorgeschriebenen 2%/J-Leihkosten-Haircut: 0.59 / +12.4%** | `quant/FINDINGS.md`; `quant/backtest/portfolio_sim.py:26` (`COST_BPS=5`, **keine Zeile Leihkosten**) vs. `quant/DESIGN.md:142` (G1 fordert 2%/J) |
| ONX | 1.06 full / 0.40 ab 2022 | bestätigt. Zusatz: **2022+ bei 1.9x Hebel kumuliert 0.0%** | `FINDINGS.md` |
| VOLC | 0.64 | bestätigt — aber **Executor nicht gebaut, Sleeve verdient nichts** | `quant/RUNBOOK.md` |
| CTREND | 1.19 full / 0.75 ab 2021 | Registry führt **1.19 als „AUSGESCHLOSSEN"** (Binance 2017+ ist auf Alpaca nicht handelbar). Validierte Zahl: **0.60 / +17.0% ab 2021**. **Executor nicht gebaut** | `quant/research/trials_registry.py` (Backfill-Liste), `FINDINGS.md`, `RUNBOOK.md` |

**Kombinierter Sharpe, nachgerechnet** (S_p = S̄·√(N/(1+(N−1)ρ̄))):

| Annahme | S̄ | ρ̄=0.10 | ρ̄=0.15 | ρ̄=0.20 |
|---|---|---|---|---|
| Briefing (ONX full, keine Leihkosten) | 0.72 | **1.27** | 1.20 | 1.11 |
| Regime-ehrlich (ONX 0.45, XSR 0.59) | 0.57 | 1.00 | **0.95** | 0.90 |
| dito nach Backtest→Live-Haircut 20–30% | — | 0.70–0.80 | **0.67–0.76** | 0.63–0.72 |

Die 1.27 des Briefings ist exakt rekonstruierbar — man braucht dafür Voll-Sample-ONX, null Leihkosten und ρ̄=0.10. **Planungswert ist 0.95. Bei Live-Evidenz-Vorbehalt 0.75.**

### 0.2 Offenes Gate G1/G9: XSRs Short-Bein ist ungeprüft

`portfolio_sim.py` rechnet 5 bp/Seite und **null Leihkosten** auf ein 1x-Short-Buch. G1 schreibt 2%/J vor, G9 verlangt einen Short-Realismus-Report. Rechnung: XSR-Vol = 14.4%/0.69 = **20.9%**; 2% Leihkosten = **−0.096 Sharpe**. Bei 10–15% Specials im Short-Bein kommt real mehr. Muravyev/Pearson/Pollet (JF 2025) sind die externe Bestätigung der Größenordnung (162 Anomalien: +0.14%/Mon → −0.01%/Mon nach Fees). **XSR ist 0.59, nicht 0.69** — bis das Gegenteil gemessen ist.

### 0.3 Offenes Gate G5: nur ONX besteht den Deflated Sharpe

`trials_registry.py` implementiert G5 (DSR>0.95) und enthält 30 protokollierte Versuche. Ich habe die DSRs mit der im Code vorgesehenen within-family-Varianz gerechnet:

| Familie | Varianten | sd_trials | SR* | DSR | Urteil nach G5 |
|---|---|---|---|---|---|
| ONX | 5 | 0.13 | 0.26 | **0.995** | besteht |
| XSR (nur Feature/Label-Achse) | 7 | 0.065 | 0.14 | **0.996** | besteht |
| XSR (inkl. Modell-Zoo) | 11 | 0.303 | 0.63 | **0.611** | fällt durch |
| VOLC | 2 → globale Varianz | 0.588 | 1.23 | **0.030** | nicht berechenbar |
| CTREND | 2 → globale Varianz | 0.588 | 1.23 | **0.454** | nicht berechenbar |

Zwei Konsequenzen, die niemand in den 12 Berichten gesehen hat:

1. **Der Deflator ist derzeit diskretionär.** XSR springt zwischen DSR 0.996 und 0.611, je nachdem ob der Modell-Zoo als „vergleichbarer Versuch" zählt. Das muss **pro Familie vorregistriert** werden, sonst ist G5 wertlos.
2. **Eine neue Familie mit nur einer Variante ist per Konstruktion unpassierbar.** Der Code fällt dann auf die globale Varianz zurück (sd 0.588 → SR* 1.23) und fordert bei n=2600 Beobachtungen **Sharpe 1.74**. Kein Kandidat dieses Dokuments erreicht das. Der einzige Weg durch G5:

| sd_trials (= Breite des vorregistrierten Sweeps) | benötigter Sharpe @ n=2600 (10 J.) | @ n=5900 (24 J.) | @ n=1300 (5 J.) |
|---|---|---|---|
| 0.10 (3–4 enge Varianten) | 0.72 | **0.55** | 1.00 |
| 0.15 | 0.83 | 0.65 | 1.04 |
| 0.20 | 0.93 | 0.76 | 1.14 |
| 0.588 (Rückfall, 1 Variante) | 1.74 | 1.57 | 1.95 |

**Damit ist die Debatte zwischen den Verifikations-Linsen entschieden.** Die Verdikte empfahlen, die Aufnahmelatte via marginalem Test von 0.5 auf 0.1–0.3 zu senken. Das ist portfoliotheoretisch richtig und statistisch falsch: der marginale Test sagt, ob ein Sleeve *hilft*; G5 sagt, ob er *existiert*. **Bindend ist G5, und G5 verlangt ~0.55–0.83 bei langem Sample und einem engen, vorher festgelegten Variantensatz.** Kandidaten mit <5 Jahren Historie (ACT13D, DATREV, FLOWX) können einen Sharpe-Gate prinzipiell nicht bestehen — sie sind nur als Event-Studie mit CAR-Gate validierbar, nie als vol-getargetetes Sleeve.

### 0.4 Der teuerste offene Posten ist kein Kandidat

`RUNBOOK.md`: **„VOLC/CTREND executors: not yet built."** Live laufen XSR + ONX. Zwei der vier „validierten Sleeves" verdienen nichts.

S_p(live, 2 Sleeves, S̄=0.52, ρ̄=0.15) = **0.69**. S_p(4 Sleeves) = **0.95**. Das sind **+0.26 kombinierter Sharpe für ~25 h Executor-Arbeit, ohne eine einzige neue Forschungsfrage** — mehr als der beste Neukandidat dieses Dokuments (+0.13) und mehr als die gesamte XSR-Härtung (+0.04). Das ist Priorität 0.

---

## 1. TESTBATTERIE — 9 Kandidaten, die alle drei Verifikations-Linsen überlebt haben

Aufnahmekriterium: „TESTEN" in allen drei Verdikten (bzw. 2/3 mit hartem Gate). Zwei Blöcke: **A = Null-Kosten-Härtung** (kein neuer Sleeve, kein neuer Turnover, Kill-Risiko heißt „bringt nichts", nie „kostet Geld"), **B = echte neue Sleeves**.

Vorbemerkung zur Kostenheuristik, die aus den drei Linsen hervorgeht und die ich zur Suchregel mache:
> **Verwirf jeden Kandidaten, dessen Zahler „der Ungeduldige" ist — außer der Fill kommt aus einer Auktion oder die Haltedauer beträgt Wochen.** Wir sind mit IEX-Realtime (~2% Volumenanteil), Sekunden-Latenz, ohne Rebates und ohne Queue-Priorität strukturell Liquiditäts**nehmer**. Wer eine Immediacy-Prämie ernten will und den Spread kreuzt, zahlt genau die Größe, die er verdienen will. Genau daran sind CAT, IMOM, GAP und PEAD gestorben; genau deshalb haben ONX (Auktion) und XSR-5d (mehrtägig) überlebt.

### BLOCK A — Härtung, null inkrementelle Kosten

---

**A1 · BORROW-AUDIT — XSRs Short-Bein gegen Leihkosten prüfen (G1/G9 schließen)**
- **Mechanismus**: Kein Alpha. Die Leihgebühr ist der Preis, den informierte Shorts zahlen; unser Simulator berechnet ihn nicht. Es geht darum, ob der Anker-Sleeve die Zahl hat, die wir glauben.
- **Erwarteter Effekt**: **−0.05 bis −0.12 Sharpe** auf XSR (nicht positiv — das ist der Punkt). Bei Konzentration in Specials mehr.
- **Orthogonalität**: n/a. **Datenquelle**: haben wir — `quant.borrow_snapshots` (läuft täglich seit ~2026-07-12, `ops/daily.sh` Schritt 7/8), `quant.finra_short_volume`, `_staging/preds_wf_v2_full.parquet`.
- **Aufwand**: 6–10 h. **Kill-Risiko**: 25% (dass der Effekt <0.03 ist).
- **Erster Test, exakt**: Join `_staging/preds_wf_v2_full.parquet` (Short-Dezil, täglich) auf `quant.borrow_snapshots` per `symbol`/`date`; Fee-Proxy für die Historie aus Utilization × Short-Interest × log_adv schätzen (kalibriert an den 12 Tagen echter Snapshots). **Hypothese**: Der ADV-gewichtete Fee-Mittelwert des Short-Beins liegt **über** dem des Long-Beins und über General Collateral (~30 bp). **Vorzeichen a priori: positiv** (Short-Bein teurer). **Gate**: Wenn >10% des Short-Beins in Namen >3% Fee sitzt oder >60% des Netto-Alphas aus Shorts in <$50M-ADV-Namen kommt (G9), wird XSRs publizierte Zahl korrigiert und das Live-Veto scharf geschaltet.

---

**A2 · ORTHO-AUDIT — echte ρ-Matrix + Tail-ρ + marginaler Aufnahmetest**
- **Mechanismus**: Wir behaupten ρ̄ „0.0–0.36" und rechnen mit einer RSS-Obergrenze. `combined_portfolio.py` berechnet die Tageskorrelationsmatrix bereits — sie wurde nie gegen die Behauptung gestellt.
- **Erwarteter Effekt**: 0 Sharpe, aber es kalibriert jede Zahl in diesem Dokument und verdoppelt/halbiert die Akzeptanzfläche der Pipeline.
- **Datenquelle**: haben wir (`quant/research/combined_portfolio.py --run`). **Aufwand**: 4 h. **Kill-Risiko**: 15%.
- **Erster Test, exakt**: `combined_portfolio.py --run`, dann **zusätzlich** die auf die 5% schlechtesten SPY-Tage konditionierte Korrelationsmatrix. **Hypothese**: Tages-ρ̄ liegt bei 0.10–0.20, **Tail-ρ̄ bei 0.6–0.9**, weil ONX (long Übernacht-Beta), VOLC (short Sprung) und XSRs Long-Bein im gleichen Ereignis brechen. **Vorzeichen a priori: Tail-ρ ≫ Tages-ρ.** Konsequenz: effektive Sleeve-Zahl im Tail ≈ 2, nicht 4; CTREND (long Konvexität) ist der einzige strukturell andere Payoff — und der ist im Krypto-Spot-Wrapper gefesselt (→ A9). **Nebenprodukt**: die Aufnahmeschwelle S_neu > ρ·S_p, gültig **ausschließlich für Netto-Sharpes nach Spread, Slippage UND Leihkosten**, mit t>2 auf dem Netto-Sharpe.

---

**A3 · AUCREV — Auktions-Dislokation als ONX-Conditioner (statt Tagesrendite)**
- **Mechanismus**: Der Schlussauktions-Anteil am Tagesvolumen wuchs 3.1% (2010) → 7.5% (2018) → ~10–13% heute, getrieben von preisunelastischem Index-/ETF-MOC-Zwang. Bogousslavsky/Muravyev (JFM 2023): 21% (NYSE) / 43% (Nasdaq) der Auktionsrendite kehren **über Nacht** um. Das erklärt ONX besser als eine Übernacht-Risikoprämie — und es ist der **einzige Mechanismus im gesamten Feld mit strukturellem Rückenwind statt Zerfall**.
- **Erwarteter Effekt**: ONX 2022+ von 0.39 auf **0.55–0.85**. Kein neuer Sleeve.
- **Orthogonalität**: n/a (schärft den Sleeve mit dem größten Live-Gewicht).
- **Datenquelle**: `quant.minute_bars` (30M Zeilen, exakt die 28 ONX-ETFs) — **haben wir**. Auktions-Prints: Alpaca `/v2/stocks/auctions`, gratis, ab 2016-01-04 verifiziert, **noch nicht ingestiert** — steht als Posten #6 in unserem eigenen `DATA_ROADMAP.md` mit 8–16 h.
- **Aufwand**: 6–10 h nur auf Minutenbars; +8–16 h für den Auktions-Ingest (der ohnehin fällig ist). **Kill-Risiko**: 40%.
- **Erster Test, exakt**: Anschluss an `quant/research/letf_rebalance_flow.py` — dort existiert der Conditioner-Harness schon und die Registry führt „ONX/Zwangsfluss-Conditioning, Sharpe 0.93, KANDIDAT". Ersetze `r_day` als Conditioning-Variable durch **ret(15:30→16:00)** aus `quant.minute_bars`, danach durch **(Auktionsprint − 15:50-Mid)/Spread**. **Hypothese H1'**: Die Übernachtrendite ist mit der Late-Day-Drift **stärker negativ** korreliert als mit der Tagesrendite. **Vorzeichen a priori: negativ, und |corr| größer als der bestehende Wert.** **Gate**: 2022–2026 muss separat besser sein als die 0.39 des unkonditionierten V2 — ein Filter, der nur das Voll-Sample hebt, ist verworfen. Kostenseitig unangreifbar: identische Trades, nur seltener.

---

**A4 · LABEL-LADDER — XSR-Labelhorizont 21/63/126 d + No-Trade-Band**
- **Mechanismus**: Kurzhorizont-ML-Alpha ist überwiegend flüchtige Mikrostruktur-Prämie, die der Turnover frisst. Längere Labels selektieren langsamere, persistentere Signale. Blitz/Hanauer/Hoogteijling/Howard (JFDS 2023, US-Aktien, **post-2004, netto nach Kosten**): 1M-Label netto ≈ null ab 2004, 3/6/12M-Label signifikant positiv. Beste Evidenzqualität im gesamten Feld.
- **Wichtiger Eigenbefund**: Das Experiment ist bei uns **halb schon gelaufen** (`FINDINGS.md`, `_staging/preds_h5/h10/h21.parquet`):

| h | Sharpe@5bp | Sharpe@10bp | Turnover | Sharpe-Verlust pro bp |
|---|---|---|---|---|
| 5d | 0.61 | 0.32 | 0.55 | **0.058** |
| 10d | 0.56 | 0.40 | 0.28 | 0.032 |
| 21d | 0.52 | 0.44 | 0.14 | **0.016** |

Die Kostengeraden kreuzen bei **6.9 bp/Seite (5d↔10d)** und **7.1 bp/Seite (5d↔21d)**. Das ist die entscheidende Zahl: **liegen die echten All-in-Fills über ~7 bp/Seite, ist das deployte 5d-Label bereits heute die falsche Wahl.** Der Burn-in misst genau das.
- **Erwarteter Effekt**: **+0.10 bis +0.25 auf XSR**, zu ~2/3 aus Turnover-Senkung. Bei Fills >7 bp deutlich mehr.
- **Orthogonalität**: 0 (es *ist* XSR). **Datenquelle**: alles vorhanden. **Aufwand**: 10–20 h (63d/126d-Label fehlen noch). **Kill-Risiko**: 30%.
- **Erster Test, exakt**: `quant/models/horizon_experiment.py` um h=63 und h=126 erweitern, identische purged Folds; dann Rang-Ensemble über Horizonte; dann No-Trade-Band in `portfolio_sim.py` (`BAND_MULT` ist der Hebel, existiert). **Hypothese**: 63d/126d liefern **≥60% des 5d-Brutto-Alphas** bei ≤1/4 Turnover. **Vorzeichen a priori: Brutto sinkt, Netto steigt, und die Steigung Sharpe/bp fällt monoton mit h.** **Diagnostisches Nebenprodukt, unabhängig vom Sharpe**: FF5+Mom-Regression auf die Langhorizont-Version. Steigt R² deutlich über 0.002, war XSRs behauptete Faktorreinheit ein Kurzfrist-Artefakt.

---

**A5 · NETSHARPE-LOSS (LambdaRank-Stufe) — Kosten in die Zielfunktion**
- **Mechanismus**: Ein Modell, das Ränge und Turnover-Strafe direkt optimiert, bestraft automatisch Signale, die nur in teuren Namen leben. Jensen/Kelly/Malamud/Pedersen (RFS 2026): +20% Netto-Sharpe / +60% Nutzen gegen kosten-agnostisches ML. Attention Factors 2025 am identischen Signal: OU-Threshold netto **−6.45** vs. trainierte Policy **+2.3** — der ganze Unterschied steckt in der Zielfunktion, nicht im Modell. Passt exakt zu unserem abgeschlossenen Modell-Zoo (Architekturtausch = 0).
- **Erwarteter Effekt**: +0.08 bis +0.20 auf XSR. **Überlappt mit A4 — nicht addieren; gemeinsam realistisch +0.15 bis +0.30.**
- **Datenquelle**: vorhanden. Spread-/Impact-Modell pro Name aus `quant.minute_bars` (Corwin-Schultz/Amihud) — `amihud_21d` ist schon Feature.
- **Aufwand**: **nur die 8-h-Zwischenstufe** (`objective=lambdarank` + Turnover-Strafe in `quant/models/train_ranker.py`). **Die End-to-End-Variante ausdrücklich nicht** — unser torch-MLP bei −0.38 ist die Warnung. **Kill-Risiko**: 25%.
- **Erster Test, exakt**: `train_ranker.py` von Regression auf `objective=lambdarank` mit Query = Handelstag, Label = Dezil-Rang von `fwd_ret_5d`; identische Folds. **Hypothese**: gleicher Brutto-IC, **niedrigerer Turnover**. **Vorzeichen a priori: Turnover sinkt, Netto-Sharpe@10bp steigt stärker als Netto@5bp.**

---

**A6 · CFUND — Binance-Perp-Funding als Crowding-/Vol-Gate auf CTREND**
- **Mechanismus**: Die Funding-Rate ist der beobachtbare Preis gehebelter Long-Nachfrage. Extremes Funding = überfüllte, liquidationsfragile Positionierung → erhöhte Kaskaden-Wahrscheinlichkeit. Zahler: gehebelte Retail-Longs auf Perp-Börsen.
- **Ehrlichkeit**: Als **Richtungssignal tot** — Presto Labs (BTC-Perp Binance 2021–2024): zeitgleiches R²=12.5% (p=1.9e-115), **Forward-R² = 0.0** auf 7 d. Als **Vol-/Kaskaden-Prädiktor** belegt (Coinbase Institutional 2024). Der eigentliche Edge (delta-neutraler Funding-Carry, MDPI 2026 Sharpe 6.45) ist strukturell unzugänglich: Alpaca hat keine Perps.
- **Erwarteter Effekt**: **+0.10 bis +0.30 auf CTREND**, primär MaxDD-Reduktion. **Datenquelle**: **haben wir und nutzen es nicht** — `quant.binance_funding` wird täglich in `ops/daily.sh` Schritt 8/8 fortgeschrieben.
- **Aufwand**: 6–10 h. **Kill-Risiko**: 45% („bringt nichts"), Kostenrisiko **null** (skaliert nur bestehende Positionen, in Stressphasen nach unten → turnover-senkend).
- **Erster Test, exakt**: Join `quant.binance_funding` (8h-Rate → Tages-Summe, 90d-Z-Score) auf die CTREND-Renditereihe aus `combined_portfolio.crypto_trend()`. **Hypothese**: Funding-Z > +2 prognostiziert **erhöhte 5-Tage-realisierte Vol** und einen **fetteren linken Tail** der CTREND-Rendite. **Vorzeichen a priori: Vol positiv, 5%-Quantil der Rendite negativ; der Rendite-MITTELWERT ist ausdrücklich NICHT vorhergesagt** (sonst reproduzieren wir Prestos Nullbefund). **Gate**: MaxDD-Reduktion ≥20% bei Sharpe-Verlust ≤0.05.

---

**A7 · VOLTERM — CBOE-Gratisdaten als zweites VOLC-Gate**
- **Mechanismus**: VOLCs Gate ist heute nur VIX3M/VIX-Contango. Contango kann positiv sein, während VVIX schon schreit — das sind exakt 05.02.2018 und Feb 2020. VOLC ist der Sleeve mit dem teuersten Tail im Buch (SVXY −0.5x, Volmageddon-Simulation −48% an **einem** Tag).
- **Erwarteter Effekt**: +0.05 bis +0.25 Sharpe, **wichtiger: substanziell kleinerer MaxDD**, und der ist über das Gap-Budget (§2) in Hebel auf *alle* Sleeves umrechenbar.
- **Datenquelle**: **frei, nicht ingestiert** — CBOE-CSVs (VIX9D/VIX3M/VVIX/SKEW/OVX + VX-Settlements 2004+), Posten #4 unseres `DATA_ROADMAP.md`, dort mit 8–16 h veranschlagt. Zusätzlich COR1M/COR3M (Implied Correlation) als Beifang.
- **Aufwand**: 8–12 h inkl. Ingest. **Kill-Risiko**: 45%.
- **Erster Test, exakt**: Ingest nach `quant.cboe_indices`, dann `combined_portfolio.vol_carry()` um ein zweites Veto erweitern. **Hypothese**: Tage mit Contango>3% **und** VVIX im oberen Dezil haben eine deutlich schlechtere SVXY-Folgerendite als Tage mit Contango>3% und VVIX-Median. **Vorzeichen a priori: negativ.** **Bewertung primär auf Tail (5%-Quantil, MaxDD), nicht auf Sharpe** — der Nutzen wird sonst an n=3 Ereignissen gemessen und ist unfalsifizierbar. Schwellenwert (VVIX-Perzentil 90) **vor** dem Lauf fixieren.

---

**A8 · RESMOM-AUDIT — 12-1-Residual-Momentum als XSR-Feature (2 h, nicht als Sleeve)**
- **Mechanismus**: Unterreaktion auf firmenspezifische Information; Faktor-/Beta-Rauschen und der crowded Factor-Momentum-Teil sind herausgerechnet. Blitz/Huij/Martens (JEF 2011): ~2x risikoadjustierter Gewinn bei ~halber Vol; Blitz/Hanauer/Vidojevic (JPM 2020) international robust; Hanauer/Windmüller (JBF 2023) zeigt Netto-Verbesserung **in Large Caps**.
- **Ich habe den Audit gemacht statt ihn zu empfehlen**: `quant/features/daily_features.py:29` enthält `mom_12m_ex1m` und `beta_63d`, aber **kein residualisiertes Momentum**. Die Lücke ist echt. (Namenskollision beachten: NICHT das gekillte IMOM.)
- **Ausdrücklich kein Sleeve** — standalone 0.3–0.5 bei ρ 0.6–0.8 zu XSR = Portfoliobeitrag ≈ 0.
- **Aufwand**: 2–6 h, null inkrementelle Kosten. **Kill-Risiko**: 40%.
- **Erster Test, exakt**: FF3-Regression über 36M-Fenster auf `quant.eod_bars`, Residual-Momentum 12-1 t-skaliert, als 30. Spalte in `CS_FEATURES`. **Hypothese**: +0.02 bis +0.06 Netto-Sharpe im v3-Ablations-Harness. **Vorzeichen a priori: positiv.** Schwelle wie Block A/B: **<+0.02 = verworfen** (wie FINRA-Short-Volume 0.585 und FRED-Regime 0.655 in der Registry).

---

**A9 · K1 — CTREND-Exposure von Krypto-Spot auf IBIT/ETHA (Kapitaleffizienz)**
- **Mechanismus**: Kein Edge, Beseitigung einer Reibung. Alpaca-Krypto ist **non-marginable, 100% Maintenance** — unser einziger Sleeve mit strukturell anderem Payoff (long Konvexität) läuft im teuersten Wrapper des Kontos. Zusätzlich rechnet `combined_portfolio.crypto_trend()` **25 bp/Seite** und rebalanciert die Inverse-Vol-Gewichte **täglich ohne Band**. IBIT/ETHA kosten 1–2 bp und sind marginfähig.
- **Erwarteter Effekt**: 0 Sharpe-Alpha, aber −2 bis −4 Prozentpunkte Gebührenlast auf CTREND und **Verdopplung seiner Reg-T-Effizienz** (§2). **Ausdrücklich NICHT BITX/ETHU**: 1.85% TER + CME-Roll + Konstant-2x-Drag ≈ σ² ≈ 25%/J bei BTC-Vol 50%, und 2x liegt über BTCs eigenem Kelly (S/σ ≈ 1.6x).
- **Aufwand**: 12–20 h — **identisch mit der Arbeit, den fehlenden CTREND-Executor zu bauen**, und dann als Equity-Executor, der die XSR/ONX-Plumbing wiederverwendet. **Kill-Risiko**: 25%.
- **Erster Test, exakt**: `turn.sum()` in `crypto_trend()` messen (1 h → exakte Gebührenzahl), dann ±15%-No-Trade-Band, dann Signal-Execution auf Freitag-Close umstellen und gegen die Spot-Variante rechnen. **Hypothese**: Bandbreite senkt Turnover >50% bei Sharpe-Verlust <0.05; der Wochenend-Signalverlust (IBIT handelt nur US-Session) kostet <0.10 Sharpe. **Vorzeichen a priori: Gebührenersparnis > Wochenend-Tracking-Verlust.** Wenn nicht, bleibt Spot — dann aber mit Band.

### BLOCK B — echte neue Sleeves

---

**B1 · EOMT — Monatsend-Duration-Ernte in Treasury-ETFs** ← **bester Neukandidat**
- **Mechanismus**: Index-gebundene Real-Money-Investoren (Lebensversicherer, passive Bond-Fonds) müssen am Monatsend-Rebalancing-Datum Duration verlängern — die Indizes nehmen neu emittierte lange Papiere auf. Kalendarisch erzwungen, preis-insensitiv. Zahler: benchmark-gebundene Anleihenkäufer, die Immediacy am letzten Handelstag kaufen. **Kein Informations-Edge, keine Risikoübernahme im Aktien-Tail.**
- **Evidenz**: Hartley & Schwarz (1990–2018): 10y-Note über die letzten 3 Tage **+0.25%/Monat, Sharpe ≈1.0 nach Transaktionskosten**, 55–70% positive Monate, leicht **positive** Schiefe. ETF-Replikation (TLT/IEF/SHY, 1999–2019): letzte 3 Tage +0.13%/Tag vs. +0.01% sonst. **Post-2020-Bestätigung des Mechanismus** (nicht des Returns): NY Fed 2024 — Handelsvolumen in Benchmark-Treasuries am letzten Monatstag seit 2020 **~46% höher**.
- **Erwarteter Netto-Sharpe**: **0.50–0.65**. Standalone nur 2–4% CAGR (15–20% Time-in-Market) — es ist ein **Sharpe-Verdichter, kein Renditetreiber**.
- **Orthogonalität**: die beste im Feld, |ρ| <0.15 zu allen vier, und **kein gemeinsamer Tail-Faktor** (Bond-Flow-Kalender). Marginaler Beitrag: **S_p 0.95 → 1.08 (+0.13)**, CAGR@k=0.30 von 23.0% auf 29.5%. **Strategischer Zusatzwert: 80% der Tage kein Reg-T-Notional gebunden** — und Notional ist unsere bindende Restriktion (§2).
- **Datenquelle**: vollständig vorhanden, `quant.eod_bars` (TLT/IEF 2002+, EDV 2007+, TMF 2009+). **Aufwand**: 6–10 h. **Kill-Risiko**: 40%.
- **Erster Test, exakt**: Aus `quant.eod_bars` Entry Close T−3 / T−4, Exit Close des letzten Handelstags, TLT/IEF/EDV, 2002–2019 als Trainingssample. **Hypothese**: Mittlere Überrendite der letzten 3 Handelstage ist **positiv und steigt mit der Duration** (IEF < TLT < EDV). **Vorzeichen a priori: positiv, monoton in Duration.** **Gates, vorab fixiert**: (1) **2020–2026 ist striktes Holdout** — kein Parameter darf darauf gefittet werden (es gibt kein publiziertes Post-2019-OOS, das ist unser eigenes); (2) Kostenmodell 3–5 bp Round-Trip über MOC/MOO; (3) vorregistrierter Variantensatz von genau 4 (T−2/T−3/T−4/T−5 Entry) → sd_trials klein → G5-Schwelle bei n≈5900 ca. **0.55**; (4) 2022/23-Ratenvol-Fenster separat ausweisen.

---

**B2 · DTRD — Cross-Asset-TSMOM ohne Aktien und ohne Krypto**
- **Mechanismus**: Trendfolgeprämie als Bezahlung für Risikotransfer an Hedger (Produzenten, Duration-Hedger) plus langsame Makro-Informationsdiffusion. Es ist eine **Risikotransfer-Gebühr, kein Informationsvorsprung** → verfällt nicht durch Publikation.
- **Evidenz, ehrlich die Live-Zahl statt des Prospekts**: **SG CTA Index Sharpe 0.61 seit Jan 2000** — 25 Jahre echtes OOS einer $300-Mrd-Industrie, netto nach Managergebühren. DBMF live 2020–2024: 2020 +1.8%, 2021 +11.4%, 2022 +21.5%, 2023 −8.9%, 2024 +7.3% → **Sharpe ≈ 0.35–0.40 netto**. Gegenevidenz eingepreist: Huang/Li/Wang/Zhou (JFE 2020) — 47 von 55 Assets mit t<1.65; Kurzfrist-Trend (<1 Woche) ist seit 1990 zerfallen, 6–12M-Trend nicht.
- **Erwarteter Netto-Sharpe**: **0.35–0.45**. Ich erwarte 0.40, nicht die 0.72 der Replikations-Papiere.
- **Orthogonalität**: hoch, und — das ist der Hauptgrund — es addiert das **einzige Payoff-Profil, das dem Buch im Tail fehlt: long Konvexität.** ρ zu VOLC wahrscheinlich negativ; **2022, das Jahr, das ONX/GAP/PEAD zerlegte, war das beste CTA-Jahr seit Dekaden.** Marginaler Beitrag bei ρ=0.15: S_p 0.95 → 0.99 (+0.04). Klein im Sharpe, wertvoll im Drawdown.
- **Disziplin als Aufnahmebedingung**: **kein Aktien-Leg** (sonst ONX/XSR-Beta), **kein Krypto-Leg** (sonst CTREND-Duplikat, ρ 0.3–0.5). Nur Duration (TLT/IEF/TMF/UBT), Metalle (GLD/SLV/UGL), Rohstoffe (DBC/PDBC), USD (UUP/UDN).
- **Datenquelle**: vollständig vorhanden (`quant.eod_bars`; Prä-Auflage-Splicing über `research/extended_data.py` im ETF-Repo). **Aufwand**: 15–25 h. **Kill-Risiko**: 35% — **niedrigstes im Feld**, und nach 10 Kills braucht die Pipeline einen sicheren kleinen Treffer.
- **Erster Test, exakt**: 12-1- und 6-1-Momentum auf `quant.eod_bars`, monatliches Rebalancing, inverse-Vol + 15%-Vol-Target. **Hypothese**: Netto-Sharpe 0.30–0.50 bei 6–12 Round-Trips/Jahr/Position und Kostenlast <0.8%/J. **Vorzeichen a priori: positiv; Korrelation zu VOLC negativ; 2022 positiv.** **Gate**: 2022 muss positiv sein — der einzige Grund, diesen Sleeve zu halten, ist das Regime, in dem der Rest bricht.

---

**B3 · CBASIS — long IBIT / short BITO (CME-Roll- und TER-Keil)**
- **Mechanismus**: BITO hält CME-Front-Month-Futures und muss monatlich rollen; in Contango verliert es bei jedem Roll, plus 0.95% TER gegen IBIT 0.25%. Wir stehen der Gegenseite des Retail-/401k-Halters gegenüber, der BTC-Exposure im Futures-Wrapper kauft. **Bestandsertrag, kein Trade-Ertrag** → Turnover ≈ null.
- **Evidenz, zweifach**: BITO underperformte Spot-BTC **−8.4% in 2025**; arXiv 2605.29309 messen den Keil CME-Carry minus fee-adjustierter IBIT-Options-Carry auf **Mean +2.58% / Median +2.52% p.a., SD 4.72%, P5 −4.77%, persistent** (386 Bucket-Obs).
- **Erwarteter Netto-Sharpe**: **0.45–0.60** bei niedriger Vol. Marginaler Beitrag: S_p 0.95 → 1.01 (+0.06).
- **Orthogonalität**: sehr hoch, delta-neutral zu BTC → ρ ~0.0–0.2 zu CTREND, ~0 zu allem anderen.
- **Datenquelle**: **komplett vorhanden** — `quant.eod_bars` (BITO 2021-10+, IBIT 2024-01+), `quant.binance_daily` für die Prä-IBIT-Basisschätzung. **Aufwand**: 4–8 h — **billigster definitiver Test des Dokuments.** **Kill-Risiko**: 50%.
- **Erster Test, exakt, in dieser Reihenfolge**: **Gate 0 (5 Minuten, vor allem anderen)**: `quant.borrow_snapshots` auf BITO abfragen. **Kostet BITO mehr als ~150 bp/J Leihgebühr, ist der Kandidat tot, bevor der Backtest anfängt** — genau daran ist PAIR gestorben. **Gate 1**: Spread-Zeitreihe `log(IBIT) − log(BITO)` aus `quant.eod_bars`. **Hypothese**: Der Spread driftet **monoton positiv** mit einer annualisierten Rate von 2.5–8% und einer Spread-Vol ≤7%. **Vorzeichen a priori: positiv, aber regimeabhängig — 2022 war die CME-Basis ~0 bis negativ, dann invertiert der Carry.** Der 2022-Test ist der eigentliche Kill-Test, nicht der Mittelwert. **Nebenbedingung**: zwei Legs = doppeltes Reg-T-Notional für ~5% Vol; Aufnahme nur mit ≤10% des Notional-Budgets (§2).

---

**B4 · ACT13D — Schedule-13D-Post-Filing-Drift, Strukturbruch 05.02.2024** *(nur als Event-Studie)*
- **Mechanismus**: Der Aktivist akkumuliert vor dem Filing; bezahlt wird vom passiven Verkäufer, der vor Offenlegung abgibt. **Der Punkt ist der Regimebruch**: seit 05.02.2024 muss Schedule 13D binnen **5 Geschäftstagen** statt 10 Kalendertagen eingereicht werden. Weniger legale Verzögerung → weniger ist beim Filing eingepreist → **mehr Drift bleibt danach übrig.** Die gesamte Zerfalls-Literatur zu 13D-Drift datiert aus dem 10-Tage-Regime. Das ist das **einzige Argument im ganzen Feld, das den Zerfall regulatorisch umkehrt** statt ihn wegzuhoffen.
- **Erwarteter Netto-Sharpe**: 0.25–0.35 — aber siehe Gate.
- **Orthogonalität**: hoch, idiosynkratisch, ρ 0.1–0.2 zu XSR (Value-Overlap), ~0 zu ONX/VOLC/CTREND.
- **Kosten binden nicht**: 20–40 Tage Haltedauer, 1 Round-Trip, Large/Mid-Cap 4–10 bp gegen 100–300 bp Zieldrift = **Faktor 15–40 Sicherheitsmarge**. Es stirbt am Alpha oder gar nicht.
- **Datenquelle**: EDGAR `SC 13D`-Index, frei, vollständig ab 1994; Kurse aus `quant.eod_bars` (survivorship-frei inkl. 58k Delistings). **Aufwand**: 16–24 h. **Kill-Risiko**: 60%.
- **Erster Test, exakt**: EDGAR-Full-Index crawlen, Filer als Aktivist/passiv klassifizieren, CAR(+1,+20) und CAR(+1,+40) gegen FF3 auf `quant.eod_bars`, Universum >$1 Mrd Marktkap. **Zwei Subsamples getrennt: 2015-01/2024 (10-Tage-Regime) vs. 02/2024–2026 (5-Tage-Regime).** **Hypothese**: CAR im neuen Regime **höher** als im alten. **Vorzeichen a priori: Differenz positiv.** Ist sie nicht positiv, ist die Hypothese widerlegt und man bricht nach zwei Tagen ab. **Harte Ehrlichkeit zum Gate**: Das neue Regime hat ~2.4 Jahre und ~250–375 Filings in liquiden Namen. Bei n≈600 Tagesbeobachtungen fordert G5 einen Sharpe von **~1.4** — unerreichbar. **Dieser Kandidat kann daher nur als Event-Studie mit CAR-t-Gate (t>3) validiert werden, niemals als vol-getargetetes Sleeve.** Bei Erfolg ist er ein XSR-Feature („Übernahme-/Aktivistenwahrscheinlichkeit"), kein Sleeve Nr. 5.

---

**B5 · RESID-MR — Faktor-Residual-Reversion auf Top-500** *(nur mit 15-h-Abbruch-Gate)*
- Ich nehme ihn auf, weil er der **einzige Aktien-Kandidat ist, der kein verkleidetes XSR ist** (Reversion auf Faktor-Residuen statt Persistenz im Querschnitt) und weil sein Ertrag sich in High-VIX-Perioden konzentriert (Nagel, RFS 2012) — also **negatives Tail-Beta zu VOLC**, unserem teuersten Tail.
- **Ich nehme ihn mit einem negativen Kostenurteil auf**: Bei täglichem Umschlag von 300–500 Residualpositionen sind ~125 Volltourns/J × 3 bp = **~3.8%/J**, gegen die 9.52% Netto des Papiers. Nominell 2.5x Deckung — mit IEX-degradiertem Buch und Market-Order-Execution als Liquiditätsnehmer unter 2x. Zusätzlich: DLSA Sharpe ~4 ist **frictionless**, Attention Factors netto 2.3 zeigt **kein Post-2015-Subsample**, und der **vanilla** Short-Term-Reversal ist 2020–2024 in den meisten Regionen verschwunden. Nur residualisierte/turnover-neutralisierte Versionen überlebten.
- **Aufwand**: **15 h bis zur Abbruch-Entscheidung**, nicht 90. **Kill-Risiko**: 60%.
- **Erster Test, exakt**: 30 rollierende PCA-Faktoren auf Tagesrenditen der Top-500 aus `quant.eod_bars`, Z-Score-Threshold auf Residuen, **echte Spread-Schätzer aus `quant.minute_bars`**, Ein-/Ausstieg über MOC/MOO. **Hypothese**: Netto-Sharpe ≥0.3 unkonditional. **Vorzeichen a priori: positiv, und stark steigend im oberen VIX-Tercil.** **Gate: netto <0.3 nach 15 h → Familie tot, kein Deep Learning.** Erst nach allen Null-Kosten-Posten anfangen.

---

## 2. KONSTRUKTIONS-HEBEL — was ohne neue Sleeves erreichbar ist

### 2.1 Die Kennzahl, die keiner der 12 Berichte geliefert hat: Rendite pro Reg-T-Notional-Dollar

Bei uns bindet **Notional, nicht Risiko**. XSR ist intern 2x brutto (`portfolio_sim.py:GROSS_LEVERAGE=2.0`); Krypto-Spot ist non-marginable und verbraucht Eigenkapital 1:1, blockiert also zusätzlich die 2x-Kapazität darauf.

| Sleeve | Netto-Rendite pro **allokiertem** $ | Notional-Multiplikator | **Rendite pro Notional-$** | Executor live? |
|---|---|---|---|---|
| XSR (nach Leihkosten) | +12.4% | 2.0 | **6.2%** | ✓ |
| ONX (2022+) | +7.6% | 1.0–1.5 | **5.1–7.6%** | ✓ |
| ONX (Voll-Sample) | +29.5% | 1.0 | 29.5% | ✓ |
| VOLC | +17.4% | 1.0 | **17.4%** | ✗ **nicht gebaut** |
| CTREND (Krypto-Spot) | +17.0% | 1.0 nominal, **2.0 Opportunität** | **8.5%** | ✗ **nicht gebaut** |
| CTREND (via IBIT/ETHA, → A9) | +17.0…19% | 1.0 | **17.0–19%** | — |

**Die zwei notional-effizientesten Sleeves, die wir besitzen, laufen nicht** — und der effizienteste von beiden wird zusätzlich durch seinen Wrapper halbiert. Das ist der größte Konstruktionsfehler im Buch, und er kostet keinen Forschungstag.

### 2.2 Notional-Budget, konkret

Eigenkapital $100k. Reg-T über Nacht: marginables Notional ≤ 2 × (Eigenkapital − non-marginable Positionen).

| | heute (CTREND als Spot) | nach A9 (CTREND via IBIT/ETHA) |
|---|---|---|
| Kapazität | 2 × (100k − 15k) = **170k** | 2 × 100k = **200k** |
| XSR (40% Kapital, 2x brutto) | 80k | 80k |
| ONX (30% Kapital, 1.5x) | 45k | 45k |
| VOLC (15%) | 15k | 15k |
| CTREND (15%) | (15k Eigenkapital, 0 Notional) | 15k |
| **Auslastung** | 140k / 170k = **0.82** | 155k / 200k = **0.78** |
| **maximaler Skalierungsfaktor** | **1.21x** | **1.29x** |

Aus der Vorwärtserwartung des Repos (10–18% CAGR bei Sharpe 0.8–1.1) folgt eine implizite Portfolio-Vol von 12–18%, also **k ≈ 0.16 heute** (nicht 0.20). Damit:

- **k_max unter Reg-T ≈ 0.19 (heute) bzw. 0.21 (nach A9).**
- Bei S_p = 0.95 ergibt k=0.19 → **CAGR 15.5% bei 18% Vol**. Das deckt sich exakt mit `FINDINGS.md` (10–18%) — die Arithmetik ist intern konsistent.

### 2.3 Kelly-Gitter (CAGR = S²·(k − k²/2), Vol = k·S)

| S_p | k=0.20 | k=0.25 | k=0.30 | k=0.35 | k=0.40 | Vol@k=0.30 | **max CAGR (Voll-Kelly) = S²/2** |
|---|---|---|---|---|---|---|---|
| 0.85 (Live-Haircut) | 13.0% | 15.8% | 18.4% | 20.9% | 23.1% | 26% | **36.1%** |
| **0.95 (Planungswert)** | 16.2% | 19.7% | **23.0%** | 26.1% | 28.9% | 28% | **45.1%** |
| 1.06 | 20.2% | 24.6% | 28.7% | 32.4% | 36.0% | 32% | 56.2% |
| 1.27 (Briefing) | 29.0% | 35.3% | 41.1% | 46.6% | 51.6% | 38% | 80.6% |

### 2.4 Das Gap-Budget — die Nebenbedingung, ohne die k ein Ruin-Generator ist

Kelly setzt kontinuierliches Rebalancing ohne Zwangsliquidation voraus. Der echte Killer im Reg-T-Konto ist der **Ein-Tages-Gap**, nicht die Jahresvol. Regel: **Σ (Position × plausibler 1-Tages-Schock) ≤ 25–30% Eigenkapital.** Kalibrierung an echten Tagen: SVXY −0.5x simuliert **−48%** (Volmageddon), 3x-LETF **−45%** bei SPX −15%, BTC **−25%** → 2x −50%. Weil das Tail-ρ nach A2 bei 0.6–0.9 liegt, treten diese Schocks **gemeinsam** ein. Bei k=0.30 summiert das Buch auf ~−27% an einem Tail-Tag: überlebbar. Bei k=0.40 auf ~−54%: Margin-Call auf dem Tief = permanenter Kapitalverlust, den kein Kelly-Modell abbildet.

### 2.5 Was Konstruktion konkret liefert

| Maßnahme | Aufwand | Effekt | Kill-Risiko |
|---|---|---|---|
| **P0: VOLC- + CTREND-Executor bauen** | 25 h | **S_p 0.69 → 0.95**; CAGR 8.2% → 15.5% | 20% |
| A9 (IBIT/ETHA-Wrapper) | 12–20 h | k_max 0.19 → 0.21; −2…−4 pp Gebühren auf CTREND | 25% |
| K2/K3 (explizites k + Gap-Budget) | 20–30 h | k von 0.16 auf k_max; +1.5…3 CAGR-Punkte, MaxDD +8–12 pp | 25% |
| K7 (±15%-No-Trade-Band statt Kalender-Rebalancing) | 4 h | +1…2 CAGR-Punkte; der Term k²σ²/2 ist bei 28% Vol **7 Punkte/J** und wurde nie als Position behandelt | 15% |
| A2/K6 (marginaler Aufnahmetest, netto, t>2) | 4 h | verdoppelt die Akzeptanzfläche der Pipeline | 15% |
| FIN-LEV (Finanzierungsanalyse) | 10–20 h | entscheidet, ob die Zielzahl existiert | 30% |
| **Summe Konstruktion, ohne neue Sleeves** | **~95 h** | **S_p 0.69 → 0.95; CAGR 8% → 17–19% bei 20–22% Vol, MaxDD 35–45%** | — |

**Zu FIN-LEV, ehrliches Erwartungsergebnis: ein Nein.** Portfolio Margin bietet Alpaca nicht. Box-Spreads als synthetische Finanzierung nahe SOFR sind mit Level 3 formal möglich, aber Alpaca bietet Optionen auf **ETFs/Aktien mit amerikanischer Ausübung** — die ITM-Short-Legs tragen Early-Assignment-Risiko. Das ist keine Finanzierung, das ist eine Zeitbombe im Depot; echte Boxes brauchen europäisch ausgeübte Index-Optionen. Futures sind ausgeschlossen. **Erwartetes Ergebnis: effektiv 1.2–1.3x über heute, harte Decke bei k ≈ 0.21.**

---

## 3. DER PFAD ZU 50% — die quantifizierte Kette

### 3.1 Die Kette, Stufe für Stufe

```
HEUTE LIVE:      2 Sleeves (XSR 0.59, ONX 0.45), ρ̄=0.15  → S_p 0.69
                 k=0.16 (implizit)                        → CAGR  8%, Vol 11%, MaxDD ~25%

+ P0 (2 Executors, 25 h)                                  → S_p 0.95
+ K1/K2/K3/K7 (Konstruktion, 70 h), k → 0.19–0.21         → CAGR 16–19%, Vol 18–20%, MaxDD 35–45%

+ Block A vollständig erfolgreich (XSR +0.15, ONX +0.20,
  CTREND +0.20, VOLC +0.15 → S̄ 0.57→0.74)                → S_p 1.23
                                                          → CAGR 24–27% bei k=0.20, Vol 25%, MaxDD 45%

+ Block B: alle 5 Neukandidaten bestehen (EOMT 0.55/ρ.05,
  DTRD 0.40/ρ.15, CBASIS 0.45/ρ.10, ACT13D 0.30/ρ.10,
  RESID-MR 0.40/ρ.25)                                     → S_p 1.28–1.45
                                                          → CAGR 28–32% bei Vol ≤28% (MaxDD-Limit 50%)
                                                          → CAGR 41–47% bei k=0.30–0.35, Vol 38–46%,
                                                             MaxDD 65–85%  ← Reg-T verbietet das ohnehin

− Backtest→Live-Haircut 20–30% auf alles Neue             → realistisch S_p 1.00–1.15
                                                          → CAGR 19–24% bei Vol 20–23%, MaxDD 38–48%
```

### 3.2 Wie viel Sharpe 50% CAGR wirklich braucht

| Ziel-Vol | benötigter S_p für 50% CAGR | erwarteter MaxDD (1.5–2×Vol) | erreichbar? |
|---|---|---|---|
| 30% (MaxDD ~50%) | **1.82** | 45–60% | ρ̄-Decke bei S̄=0.70/ρ̄=0.15 liegt bei **1.81** → nur mit **12+ Sleeves à 0.70 und ρ̄≤0.05** |
| 38% | 1.51 | 57–76% | 8 Sleeves à 0.70 bei ρ̄=0.05 (=1.70) — aber Reg-T erlaubt 38% Vol nicht |
| 42% | 1.40 | 63–84% | ruinös + außerhalb Reg-T |
| 49% | 1.27 (Briefing-Wert) | 74–98% | ruinös |

**Und die Zahl, die alles entscheidet:** Bei ehrlichem S_p = 0.95 ist die **maximal erreichbare CAGR bei Voll-Kelly 45.1%** — 50% ist nicht schwer, sondern **mathematisch unerreichbar, bei jedem Hebel**. Bei S_p = 1.00 ist 50% *exakt* Voll-Kelly (k=1.0, Vol 100%, ~50% Wahrscheinlichkeit einer Halbierung). Bei S_p = 1.06 braucht 50% k=0.67 → 71% Vol.

### 3.3 Wahrscheinlichkeiten, unverblümt

| Ergebnis | Wahrscheinlichkeit | Begründung |
|---|---|---|
| **50% CAGR nachhaltig (3+ Jahre) bei MaxDD ≤50%** | **<5%** | Braucht S_p ≈1.8, also ~12 Sleeves à 0.70 bei ρ̄≤0.05. Unsere Trefferquote ist 4/14 (29%) bei einem **Sleeve-Mittel von 0.57, nicht 0.70**. 8 zusätzliche Erfolge = ~28 weitere getestete Familien ≈ 850 h ≈ 5 Monate Vollzeit — und post-2020-Kandidaten sind systematisch schwächer (Chen & Welch 2026: publizierte Non-Micro-Anomalien post-2005 = 7 bp/Monat). |
| **50% in einem einzelnen Kalenderjahr** | **15–25%** | Rechtes Ende der Verteilung eines gut gebauten 20–25%-Plans, getrieben von CTREND in einem Krypto-Bullenjahr. Das ist ein Tail-Outcome, kein Plan. |
| **50% CAGR erreichbar, aber nur bei ruinösem Risiko** | — | Bei S_p=1.27 (der optimistischste verteidigbare Wert): k=0.38, **Vol 49%, erwarteter MaxDD 74–98%, P(Halbierung) 15–20%.** Reg-T lässt diesen Hebel überdies nicht zu (k_max ≈ 0.21). |
| **Höchster Wert bei tragbarem Risiko (MaxDD ≤50%)** | — | **28–32% CAGR** — *wenn die gesamte Testbatterie trägt und die Finanzierungskapazität wächst.* **Realistisch, mit Live-Haircut und der Reg-T-Decke k≈0.21: 18–24% CAGR bei 20–23% Vol und 38–48% MaxDD.** |

### 3.4 Die Zahl, die ich in DESIGN.md schreiben würde

> **Zielkorridor: 18–24% CAGR bei 20–23% Vol und erwartetem MaxDD 38–48%; Stretch 28–32% falls die Testbatterie vollständig trägt. Das 50%-Ziel wird als Zielzahl gestrichen und als Tail-Outcome geführt.**

Zur Einordnung, ohne Selbstbetrug in die andere Richtung: 18–24% netto schlägt D.E. Shaw Composite (12.7%/J über 23 Jahre) um Faktor ~1.6, Citadel Wellington (19.5%/J über 35 Jahre) auf Augenhöhe, den Hedgefonds-Median (Sharpe 0.62) klar. Der einzige dokumentierte systematische Fall von ≥50% netto über 5+ Jahre ist Medallion — und dessen Zahl wird 2024 akademisch auf 31.8% *brutto* nach unten korrigiert. Bei ~10.000 getrackten Fonds ist die Basisrate für „systematisch, reproduzierbar, 50% netto, 5 Jahre" nicht klein, sie ist **≈1**.

---

## 4. KILL-LISTE — was aus den 12 Berichten NICHT getestet wird

**Gestorben am Liquiditäts-Paradox** (der Zahler ist „der Ungeduldige", und wir sind strukturell Liquiditätsnehmer):
- **EODR** (Cross-Sectional End-of-Day Reversal): 3.78 bp/Tag value-weighted gegen 2–3 bp Kosten = 1.3–1.9x, bei 250 Round-Trips/J. Die 6.86 bp equal-weighted und 14.71 bp im kleinsten Size-Quintil sind Microcap-getrieben. Die ganze These hängt an passiven Limits mit 50–65% Fill-Rate — und passive Fills in einem Reversal-Trade sind **adverse selektiert** (man wird gefüllt, wenn die Reversion nicht kommt). Die Autoren schreiben selbst, es sei Market-Maker-Terrain. 400–800 Orders/Tag Operationsrisiko obendrauf.
- **AUCF-standalone** (Auktions-Dislokation als eigener Fade-Sleeve): Der MOC-Cutoff liegt um 15:50, **vor** dem Print, den man faden will; der Imbalance-Feed ist kostenpflichtig; nach dem Print bleibt nur After-Hours mit 5–30 bp Spread. Die Auktionsdaten werden trotzdem ingestiert — für A3 und als XSR-Feature.
- **RESREV-HighVIX**: Wird als kostenschonend verkauft und ist die teuerste Variante. Nagels Befund lautet, dass die Prämie steigt, **weil sich Intermediäre zurückziehen und die Spreads sich weiten** — die Prämie *ist* der Bid-Ask. Wer ihn kreuzt, zahlt sie zweimal.
- **PREQ** (Pre-Earnings-Liquiditätsprämie): Die Prämie skaliert **per Konstruktion** mit der Spreadbreite, lebt also in Mid/Small. Nur ~2x Deckung im liquiden Tier. Zulässig ausschließlich als 3-Feature-Block im v3-Ablations-Harness (20 h, +0.02-Schwelle), nie als Sleeve.

**Gestorben am Zerfall (mit publiziertem Nachweis):**
- **Publizierte Querschnitts-Anomalien als Klasse** (Net-Share-Issuance, Accruals, Asset Growth, Profitability, BAB, iHML): Chen & Welch 2026, ~200 Anomalien — Median 48 bp/Mon bis 2005 → **7 bp** bei post-2005 UND Non-Micro gleichzeitig. „Useless to non-micro-cap portfolio managers in the 21st century." Suchraum geschlossen.
- **Alle Short-Interest-Varianten und Short-Legs als Alpha (LOANFEE)**: Muravyev/Pearson/Pollet (JF 2025) — 162 Anomalien +0.14%/Mon → **−0.01%/Mon** nach Leihkosten. Selbstaufhebend: die Leihgebühr *ist* der Preis. Zusatzproblem: unsere Borrow-Historie ist 12 Tage alt, das Feature ist nicht walk-forward-testbar. Die Lehre aus PAIR ist „Leihgebühr als Kostenveto" (= A1), nicht als Alpha.
- **Index-Inklusion/Rebalancing**: Greenwood & Sammon (JF 2025) — S&P-500-Additions 7.4% (90er) → **0.3%** (2010–2020), Deletions 0.1%, kein Prä-Ankündigungs-Drift. Eine publizierte Null nachrechnen.
- **Pre-FOMC-Drift, Earnings-Announcement-Premium, Buyback-Langhorizont-Drift, Retail-Order-Imbalance (BJZZ)**: alle mit publiziertem Verschwinde-Nachweis. BJZZ zusätzlich methodisch erledigt (JF 2024: 35% Trefferquote der Retail-Identifikation, 28% falsches Vorzeichen).
- **EDGAR-TEXT (Lazy Prices)**: JF 2020 publiziert, Sample 1995–2014, keine überzeugende Post-2020-Replikation, flächendeckende kommerzielle NLP-Adoption seit 2021. 30–50 h dreckiges Section-Parsing für +0.02…0.06. Schlechtestes Sharpe pro Stunde.
- **NEWSDIFF / Customer Momentum**: Identischer Fehlermodus wie CAT. Der handelbare Teil ist der schwache; Customer Momentum dreht das Vorzeichen bei Kontrolle relativer Größe.
- **XCRYPTO / CXREL**: ρ 0.4–0.7 zu CTREND **und** 30–50 bp Round-Trip auf Alpaca (15/25 bp Maker/Taker). Bei wöchentlichem Rebal 9–12%/J Gebühren; bei monatlichem *ist* es CTREND. Als Breadth-Erweiterung innerhalb CTREND erlaubt (8 h), nie als Sleeve.
- **VXTS**: Funktioniert wahrscheinlich und trägt nichts — ρ 0.6–0.85 zu VOLC → marginale Schwelle 0.64–0.90, die publizierte IR ist 0.404. Redundanz, die als Sleeve Nr. 5 gezählt würde, ist teurer als ein Fehlschlag.
- **SESSION-VRP**: Sekundärquellen berichten das **Vorzeichen** der Overnight/Intraday-Asymmetrie widersprüchlich; 2 Crossings/Tag in SVXY = ~500 Round-Trips/J = 25%/J Drag gegen einen Sleeve, der +17.4% verdient. ρ ~0.8 zu VOLC. Maximal eine 6-h-Messung, kein Projekt.
- **VETP-REBAL**: Der publizierte Effekt (+0.91%/+1.38% pro SD, JBF 2021) sitzt in **VIX-Futures**, die wir nicht handeln können. Über die ETPs handeln wir das Instrument, *das* rebalanciert — wir stehen auf derselben Seite wie der Flow. Die Reversion zu halten heißt over-night, damit fällt das „kein Borrow, weil intraday"-Argument. UVXY-AUM ist über 5 Jahre um ~$500M geschrumpft.

**Gestorben an Datenmangel / Nicht-Falsifizierbarkeit:**
- **CXVOL, CARRYBOX, alle Options-Sleeves**: Unsere Ketten-Snapshots starten 11.07.2026 (`options_archiver`, läuft täglich). Frühestens 2027/2028 backtestbar. CXVOL zusätzlich: ATM-Optionen auf TLT/GLD/EEM/HYG sind **nicht** penny-wide (1–2 Vol-Punkte pro Seite), und der Edge sitzt genau in den Namen mit dem breitesten Spread.
- **Dealer-Gamma/GEX, 0DTE, OpEx-Pinning**: datenblockiert plus 5–17% Spread der Prämie. 0DTE ist OPT bei 5–10x Frequenz.
- **KALSHI-DIST**: Evidenz überraschend sauber (NBER WP 34702; arXiv 2604.01431: t=3.63 auf 5d-BTC-Vol, R² gegen Fed-Funds-Futures nur 2.3%) — aber die API liefert nur ~100 Tage Historie, kein purged walk-forward über Regime. **Aktion: Logger starten (8–12 h, kein Kapital), Bewertung in 12 Monaten**, genau wie bei den Optionsketten.
- **DATREV**: Regime ~2 Jahre alt, n=10–15 leihbare Namen, kein purged walk-forward möglich; der Tail ist Prämien-**Expansion** (Nov 2024: 3.4x→6x in Wochen) auf einem 2-Jahres-Sample; Gegenseite sind Convertible-Desks, die die Kapitalstruktur besitzen.

**Gestorben an Kapazität, ToS oder Steuer:**
- **POLY-GEO / POLY-INFORMED**: Die Detektor-Literatur ist stark (Mitts & Ofir: 210.718 geflaggte Wallet-Markt-Paare, 69.9% Trefferquote, >60 SD), dokumentiert aber **explizit keinen handelbaren Spillover auf Finanzmärkte**. 10–40 Events/Jahr mit US-Asset-Bezug → bei n≈15/J ist ein Sharpe nach 3 Jahren nicht von Null unterscheidbar. Volumen migriert nach Kalshi (Jan 2026: $9.5 Mrd vs. $3.3 Mrd), dessen API keine Wallet-IDs liefert. **Kein Kapital.** Erlaubt: `MIN_VOLUME=100_000` in `polymarket_ingest.py` senken, weil frische Geo-Märkte darunter liegen.
- **SPAC-Arb, Odd-Lot-Tender**: Alpaca berechnet **$100 pro Voluntary Corporate Action**; bei $3k Exposure sind das 30–100% des Ertrags. Auf einem Paper-Konto prinzipiell nicht validierbar.
- **Dividenden-Capture, DIVM**: Berlin/Abgeltungsteuer + 15% US-Quellensteuer → der Ex-Day-Drop trifft zu 100%, die Dividende kommt zu 85%. By construction negativ.
- **ADR-/Dual-Listing-Arb, FX-Carry, Länder-ETF-Momentum, CMDCARRY, BXCARRY**: kein Instrument (FX/Futures), 17 Jahre OOS-Versagen (Länder-ETF ab 2008/09), oder überwiegend Duration-/Beta-Kontamination (BXCARRY: 0.606 vs. 0.494 passiv, beide aus dem Bondbull). BXCARRY-Rest: **eine Zeile in EOMT** (Carry entscheidet IEF vs. TLT vs. EDV).
- **CAP-EDGE (Rang 1500–3500)**: Beste Evidenz, schlechtestes Timing. 50 Round-Trips/J × 30–80 bp = 20–40%/J Kostenlast bei 5d-Tranchen. Zulässig **erst nachdem A4 den Horizont auf 63/126 d verlängert hat**, und dann long-only mit Dezil-Kostenmodell.
- **ML-Zoo-Erweiterungen (CHART-CNN, XS-ATTN, META-LABEL, Triple-Barrier, Regime-Modellwahl, GNN)**: Von uns selbst und von den Qlib-Benchmarks widerlegt — auf human-engineerten Tabellenfeatures ist LightGBM ≥ Transformer/ALSTM/TCN. CHART-CNNs relevante Zahl für Top-1500 ist der **value-weighted** Sharpe 0.5 (brutto), nicht die 2.4 equal-weighted. META-LABEL: Null-Benchmark fehlt in der Literatur — 2 h Vol-Targeting-Test zuerst.
- **K8 (HMM-Regime-Allokation)**: 4 Sleeves × ~5 Jahre = ~60 Monatsbeobachtungen für eine 3-State-Matrix mit 4 Mittelwertvektoren. Wir haben FRED-Makro-Regime-Interaktionen in der Registry bereits bei 0.655 (<+0.02) verworfen. **K9 (4x Intraday)**: alle vier Sleeves halten über Nacht. Kein CAGR-Beitrag.
- **BAR-REVISION** (die 10 Leichen nach marginalem Sharpe neu bewerten): Die Arithmetik ist richtig, die Anwendung nicht. Der Test setzt S_neu > 0 voraus; unsere Kills sind netto **negativ**, nicht „zu klein": CAT −1.10, PEAD −0.20, PAIR −0.30, IMOM −1.31, RSPLIT −0.40, OPT −0.50 (`trials_registry.py`). Bei negativem Netto hilft kein ρ. GAP (0.12) und OVN-Fade (−0.20) werden erst reaktivierbar, wenn **Ausführung oder Horizont sich ändert** (A3-Auktionsexekution, A4-Horizont) — in dieser Reihenfolge, nicht als Leichenschau mit gesenkter Latte.

---

## 5. REIHENFOLGE — nach (Sharpe-Erwartung × Orthogonalität) / Aufwand

Bewertungsmaß: ΔS_p pro 10 Arbeitsstunden. dCAGR/dS_p ≈ 2·S_p·(k−k²/2) ≈ 0.33 bei S_p=0.95, k=0.19.

| Rang | Posten | Aufwand | ΔS_p | ΔS_p / 10 h | ΔCAGR |
|---|---|---|---|---|---|
| **1** | **P0: VOLC- + CTREND-Executor bauen** | 25 h | **+0.26** | **0.104** | **+7.3 pp** |
| **2** | A2 ORTHO-AUDIT (ρ̄ + Tail-ρ + Aufnahmetest) | 4 h | kalibrierend | — | — |
| **3** | A1 BORROW-AUDIT (G1/G9 schließen) | 8 h | **−0.03** (Korrektur) | — | Schutz für 0.59 |
| **4** | K7 No-Trade-Band (Vol-Drag-Hygiene) | 4 h | 0 | — | +1…2 pp |
| **5** | B3 CBASIS (nach Borrow-Gate 0) | 6 h | +0.06 | 0.100 | +2.0 pp |
| **6** | B1 EOMT | 8 h | **+0.13** | **0.163** | +4.3 pp |
| **7** | A6 CFUND (Daten liegen ungenutzt) | 8 h | +0.03…0.08 | 0.069 | +1.8 pp |
| **8** | A3 AUCREV (ONX-Conditioner) | 8 h (+12 h Ingest) | +0.06…0.19 | 0.078 | +4.1 pp |
| **9** | A5 LambdaRank-Stufe | 8 h | +0.03…0.08 | 0.069 | +1.8 pp |
| **10** | A7 VOLTERM (CBOE-Ingest + Gate) | 10 h | +0.02…0.10 | 0.060 | +2.0 pp |
| **11** | A8 RESMOM-Feature-Audit | 4 h | +0.01…0.02 | 0.038 | +0.5 pp |
| **12** | A4 LABEL-LADDER (63d/126d) | 15 h | +0.04…0.10 | 0.047 | +2.3 pp |
| **13** | A9/K1 IBIT-Wrapper (fällt mit P0 zusammen) | 15 h | 0 | — | +1.5 pp (k) |
| **14** | K2/K3 Kelly-Regler + Gap-Budget | 25 h | 0 | — | +1.5…3 pp |
| **15** | B2 DTRD | 20 h | +0.04 | 0.020 | +1.3 pp (+Tail) |
| **16** | FIN-LEV | 15 h | 0 | — | entscheidet die Zielzahl |
| **17** | B4 ACT13D (nur Event-Studie) | 20 h | ≤+0.02 | 0.010 | +0.5 pp |
| **18** | B5 RESID-MR (15-h-Gate) | 15 h | +0.01 | 0.007 | +0.3 pp |

### Diese Woche (~45 h)

1. **P0 — die zwei fehlenden Executors** (25 h). Nicht verhandelbar und vor allem anderen. Wir suchen Sleeve Nr. 5, während zwei von vier validierten Sleeves nichts verdienen. +0.26 S_p, mehr als die gesamte Testbatterie zusammen, ohne eine einzige neue Forschungsfrage. Baue CTREND direkt als **IBIT/ETHA-Executor** (A9) — dann fallen Kapitaleffizienz, Gebührensenkung und Executor-Bau in dieselbe Arbeit.
2. **A2 ORTHO-AUDIT** (4 h). `combined_portfolio.py --run` plus Tail-ρ. Jede Zahl in diesem Dokument hängt an ρ̄, und wir haben sie, ohne sie zu kennen.
3. **A1 BORROW-AUDIT** (8 h). Bevor wir Sleeve Nr. 5 suchen, muss der Referenzwert stimmen; der marginale Aufnahmetest hängt vollständig daran.
4. **B3 CBASIS Gate 0** (5 Minuten) + Backtest bei bestandenem Gate (6 h). Billigster definitiver Test des Dokuments, zwei EOD-Reihen, die wir haben.
5. **K7 No-Trade-Band** (4 h). Der Term k²σ²/2 ist bei 28% Vol sieben CAGR-Punkte und wurde nie als Position behandelt.

### Woche 2–3 (~45 h)

6. **B1 EOMT** (8 h) — bester Neukandidat: 8–10x Kostenmarge, echte Tail-Orthogonalität, 80% der Tage kein Notional-Verbrauch. **2020–2026 als striktes Holdout, vorregistrierter 4-Varianten-Sweep.**
7. **A6 CFUND** (8 h) + **A3 AUCREV** (8 h, plus Auktions-Ingest, der ohnehin fällig ist) + **A7 VOLTERM** (10 h) als ein Sprint: drei Gates auf drei Sleeves, null zusätzlicher Turnover, alle drei Datenquellen entweder im Haus oder gratis und in unserem eigenen `DATA_ROADMAP.md` bereits terminiert.
8. **A5 LambdaRank + A8 RESMOM-Audit** (12 h) im XSR-Harness.
9. **FIN-LEV** (15 h) — parallel, keine Forschung. Wenn 1.3x über heute die Decke ist, fällt das 50%-Ziel formal, und zwar hier und nicht durch zwei weitere gescheiterte Familien.

### Danach (~75 h)

10. **A4 LABEL-LADDER** (15 h) — nach den ersten 20 Burn-in-Tagen, weil die gemessenen echten Fills entscheiden: liegen sie über **7.1 bp/Seite**, ist die Umstellung auf 21d/63d nicht optional, sondern überfällig.
11. **K2/K3** (25 h) — Kelly-Regler mit Gap-Budget, **dimensioniert mit S=0.95, gehofft auf 1.20**.
12. **B2 DTRD** (20 h) — der einzige Kandidat, der long Konvexität hinzufügt; ohne Aktien-Leg, ohne Krypto-Leg.
13. **B4 ACT13D** (20 h) als reine Event-Studie mit CAR-Gate; **B5 RESID-MR** (15 h) mit hartem Abbruch bei netto <0.3.

### Zwei Verfahrensregeln, ab sofort verbindlich

1. **Jeder Kandidat wird vor dem ersten Lauf in `trials_registry` eingetragen — mit vorregistriertem Variantensatz und vorregistriertem Deflator-Set.** Ein neuer Kandidat mit einer einzigen Variante fällt auf die globale Varianz zurück und braucht dann Sharpe 1.74 — er kann G5 nicht bestehen. Enge Sweeps (3–4 Varianten, sd≈0.10) senken die Schwelle auf **0.72 bei n=2600 bzw. 0.55 bei n=5900**. Das ist der reale Maßstab der Testbatterie, nicht die 0.2 aus dem marginalen Test.
2. **Der marginale Aufnahmetest (S_neu > ρ·S_p) gilt ausschließlich für Netto-Sharpes nach Spread, Slippage UND Leihkosten, mit t>2.** Sonst wird er zur Wiederbelebungsmaschine für genau die Familien, die korrekt gestorben sind.

---

**Kernsatz für die Akte:** Wir haben kein Ideenproblem, wir haben ein Ausführungs- und ein Messproblem. Zwei von vier Sleeves laufen nicht, das Short-Bein des dritten ist ungeprüft, die Korrelationsmatrix ist gerechnet und nie gelesen, und zwei unserer eigenen Gates (G1-Leihkosten, G5-Deflator) sind offen. Das sind ~40 Arbeitsstunden und der größte Renditehebel im Buch. Die 50% sind bei ehrlichem kombinierten Sharpe von 0.95 mathematisch unerreichbar — die Obergrenze bei Voll-Kelly ist 45%, und Voll-Kelly heißt 95% Vol. **Baut den 20–24%-Plan; 50% ist dessen rechter Verteilungsrand, nicht dessen Ziel.**

---

**Relevante Dateien:** `/Users/carl/Coding/hfea_strategy/quant/FINDINGS.md` · `/Users/carl/Coding/hfea_strategy/quant/DESIGN.md` (G1–G10) · `/Users/carl/Coding/hfea_strategy/quant/RUNBOOK.md` (VOLC/CTREND-Executors fehlen) · `/Users/carl/Coding/hfea_strategy/quant/backtest/portfolio_sim.py` (`COST_BPS=5.0`, `STRESS_BPS=10.0`, keine Leihkosten) · `/Users/carl/Coding/hfea_strategy/quant/research/combined_portfolio.py` (ρ-Matrix + Hebelleiter, `MARGIN_RATE=0.07`) · `/Users/carl/Coding/hfea_strategy/quant/research/trials_registry.py` (DSR/G5, 30 Versuche) · `/Users/carl/Coding/hfea_strategy/quant/research/letf_rebalance_flow.py` (Conditioner-Harness für A3) · `/Users/carl/Coding/hfea_strategy/quant/features/daily_features.py` (20 CS_FEATURES, kein Residual-Momentum/Flow/Auktion) · `/Users/carl/Coding/hfea_strategy/quant/features/xsr_v2_features.py` (9 Fundamentals) · `/Users/carl/Coding/hfea_strategy/quant/models/horizon_experiment.py` (A4-Ausgangspunkt) · `/Users/carl/Coding/hfea_strategy/quant/DATA_ROADMAP.md` (#4 CBOE, #6 Alpaca-Auktionen, #8 Borrow) · `/Users/carl/Coding/hfea_strategy/quant/ops/daily.sh` (Schritt 8/8 schreibt `binance_funding` fort)