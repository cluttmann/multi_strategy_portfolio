# Weg zu 50% CAGR — Design

**Datum:** 2026-07-30
**Kontext:** `quant/` — ML/LLM-Quant-Desk, Alpaca-Paper-Konto, getrennt vom ETF-Bot.
**Ausgangslage:** Die Portfolio-Sharpe-Obergrenze bei heutiger Sleeve-Qualität
liegt bei S_p → S̄/√ρ̄ ≈ 1,25, also ≈27%/Jahr unter Reg-T (1,9×) —
unabhängig von der Zahl weiterer Sleeves gleicher Güte (Regel R8,
`quant/research/kill_registry.yaml`; siehe `quant/ROAD_TO_50.md`). Aktiver
Kern: XSR (LightGBM-Ranker) + EOMT (Monatsend-Duration) + DTRD
(Cross-Asset-TSMOM); VOLC/ONX pausiert wegen Redundanz mit dem ETF-Bot
(`quant/FINDINGS.md`).

Drei unabhängige Hebel, um über die Decke zu kommen: höhere
Durchschnitts-Sharpe pro Sleeve, echte Orthogonalität (ρ ≈ 0 zu allem
Bestehenden), oder strukturelle Hebelwirkung jenseits des Reg-T-Cash-Hebels.
Dieses Dokument spezifiziert alle drei parallel.

## Entscheidungen aus dem Brainstorming

- **SPY-Ticker-Sperre** (der ETF-Bot hält SPY-Exposure siebenfach) **gilt nur
  für Aktienpositionen, nicht für Optionskontrakte** auf SPY als Basiswert.
  Beobachtungspflicht bleibt: ρ(Optionen-Sleeve, Bot) wird gemessen wie bei
  VOLC/ONX, weil ein Crash beide gleichzeitig trifft.
- **Tail-Risk-Sizing für Optionen-Sleeves wird erst nach echten
  Backtest-Zahlen festgelegt**, nicht vorab spezifiziert.
- **Reihenfolge: erst validieren, dann bauen** (Ansatz B+C). Die
  Options-Ausführungsschicht wird nicht gebaut, bevor ein Kandidat G5 (DSR)
  und G7 (Orthogonalität) der Discovery-Pipeline bestanden hat — genau das
  Muster, das bei XSR/EOMT/DTRD/ONX schon funktioniert. Die
  Exekutions-Kosten-Diagnose (Hebel 1) ist die einzige Sofortmaßnahme, weil
  sie keine neue Forschung braucht.
- Alle Zahlen bleiben **brutto/vor Abgeltungsteuer** wie bisher in
  `FINDINGS.md`. Turnover-Reduktion hilft in Deutschland nicht steuerlich
  (keine Haltefrist-Regel wie in den USA, Abgeltungsteuer ist flat 26,375%
  ohne Teilfreistellung für Optionen/Einzeltitel) — nur gegen
  Transaktionskosten.

## Hebel 1 — Exekutionskosten (kein neuer Sleeve, keine Gates)

**Befund vor diesem Design:** die gemessenen 9,9–10bp Slippage (XSR, ONX)
sind KEIN Spread-Kosten-Problem — beide Sleeves nutzen bereits `opg`/`cls`
(Auktions-Orders), genau um die Immediacy-Prämie zu vermeiden, an der
IMOM/GAP/CAT/PEAD gestorben sind. Die Slippage ist die Abweichung unseres
Fills vom offiziellen Auktions-Print (`quant/ops/cost_monitor.py`).

**Diagnose zuerst.** `cost_monitor.py` um eine Aufschlüsselung der Slippage
erweitern nach: Ordergröße als % des 20-Tage-Auktionsvolumens (aus
`eod_bars`), Symbol-Liquiditätstier, Sleeve. Drei mögliche Befunde, drei
verschiedene Fixes:

1. **Größenabhängig** (Slippage steigt mit % ADV) → Order-Cap als % des
   Auktionsvolumens, ggf. über 2 Tage strecken.
2. **Routing-Artefakt** (Slippage konstant, unabhängig von Größe) → prüfen,
   ob Alpacas `opg`/`cls`-Routing die primäre Börsen-Auktion erreicht oder
   nur einen IEX-Proxy.
3. **Messfehler** (Benchmark-Zeitpunkt ≠ tatsächlicher Auktions-Print) →
   Benchmark in `cost_monitor.py` korrigieren; in diesem Fall ist das echte
   Alpha besser als gedacht.

Kein neuer Sleeve, keine Discovery-Gates — nur Diagnose + gezielter Fix an
bestehendem, bereits validiertem Code.

## Hebel 2 — MERGARB (Merger-Arbitrage, echte Orthogonalität)

Läuft nach demselben Muster wie ACT13D (`quant/data/sec_13d_ingest.py`) und
durch dieselbe Discovery-Pipeline. Spec bereits vorregistriert in
`quant/research/hypothesis_queue.yaml` (id: MERGARB).

**Daten:** neuer Ingester `quant/data/merger_ingest.py` — SEC-EDGAR-
Quartalsindex nach Formularen `SC TO-T`/`SC TO-I` (Tender Offer), `DEFM14A`
(Fusions-Proxy), `425` (Kommunikation zur Übernahme), `SC 14D9`
(Zielgesellschafts-Antwort). CIK→Ticker-Join wie bei ACT13D. Neue Tabelle
`quant.merger_deals` (target, acquirer, Ankündigungsdatum, Angebotspreis,
Cash-vs-Aktientausch, erwarteter/tatsächlicher Abschluss).

**Extraktion:** Deal-Preis zuerst per Regex aus dem Filing-Text (deckt die
meisten Cash-Deals ab: `"$X.XX per share in cash"`), LLM-Fallback nur für
komplexere Aktientausch-Verhältnisse.

**Signal:** ab Ankündigung long das Ziel, Positionsgröße ∝ Spread /
erwartete Resthaltedauer (annualisiert), halten bis Closing oder Bruch
(erkannt über 8-K-Terminierungsfilings oder Kurslücke). **Nur Cash-Deals in
Phase 1** — Aktientausch-Deals brauchen ein Hedge-Bein gegen den Käufer
(Phase 2, falls Phase 1 die Gates besteht).

**Prüfung:** die drei vorregistrierten Vorhersagen aus
`hypothesis_queue.yaml` (Monotonie Spread↔Rendite, Cash>Aktientausch,
Bruchrate steigt mit VIX) plus **R9-Benchmark** — gegen einen
Small-/Mid-Cap-Index, nicht gegen SPY (Zielgesellschaften sind i.d.R.
kleiner als Käufer). Dann durch G0–G8 wie jeder andere Kandidat.

## Hebel 3 — Optionen (struktureller Hebel jenseits Reg-T)

Der einzige der drei Hebel, der die Reg-T-Cash-Hebel-Decke (1,9×)
grundsätzlich umgehen kann — Optionsprämien-Hebel ist nicht an dieselbe
Margin-Grenze gebunden wie Aktienpositionen. QNT-Konto hat bereits
Level-3-Optionsfreigabe (`options_approved_level: 3`), ausreichend für
definierte-Risiko-Spreads.

### OPTPREM (Prämienverkauf) — `quant/research/options_phase_a.py` härten

- Bereits vorhanden: Short-Put-Spread SPY/QQQ, 2,4 Jahre Bars-Historie
  (2024-02+), Auktions-unabhängige Bar-Close-Bewertung, bewusst punitiver
  Kostenhaircut (15% des Credits + $0,02/Leg).
- **Variantenraster vorregistrieren, bevor es läuft** — Lektion aus dem
  G5-Vorfall (XSR sprang zwischen DSR 0,996/0,611, je nachdem ob der
  Modell-Zoo mitzählte). Fester Sweep: {OTM 1,5/2/3%} × {Breite 1/2%} ×
  {VIX-Filter an/aus} = 12 Varianten, `sd_trials` daraus berechnet, nicht
  nachträglich gewählt.
- Mehr Historie über `quant/data/options_archiver.py`s tägliche Snapshots
  (IV/Greeks — nicht rückwirkend beschaffbar, nur die Bars reichen bis
  2024-02 zurück).
- SPY bleibt im Universum (Entscheidung oben) — ρ(OPTPREM, Bot) wird
  trotzdem gemessen wie bei VOLC/ONX.

### OPTCONV (long Convexity, neu) — `quant/research/options_leaps_study.py`

- Systematischer Screen: LEAPS/Debit-Spreads kaufen, wenn IV-Percentile (aus
  den Archiver-Snapshots) niedrig gegen die trailing realisierte Vol steht —
  statistisch "billige" Konvexität statt naives Long-Optionen-Kaufen (das
  verbrennt strukturell Prämie).
- **Ehrliche Einschränkung:** IV-Historie existiert nirgends rückwirkend —
  dieser Kandidat ist an die zukünftige Datensammlung gebunden, nicht sofort
  testbar wie MERGARB oder OPTPREM. Braucht vermutlich 6–12 Monate
  Archiver-Daten, bevor G3 (Holdout) sinnvoll rechenbar ist.
- Rolle im Portfolio vermutlich nicht als Renditequelle (Prämienkäufer
  bluten in ruhigen Phasen), sondern als Tail-Hedge wie XSR — muss gegen den
  echten Bot gemessen werden (ρ in Crash-Regimen), nicht nur gegen die
  anderen Quant-Sleeves.

### Ausführungsschicht (spezifiziert, NICHT jetzt gebaut)

Multi-Leg-Order-Submission (Alpaca `order_class: mleg`), OCC-Symbolaufbau
(bereits in `options_phase_a.occ()` vorhanden), Expiry-Rollover,
Ledger-Erweiterung (aktuell nur `{symbol: int_qty}` — Optionen brauchen
Contract/Strike/Expiry/Right). **Wird nicht gebaut, solange kein Kandidat G5
und G7 bestanden hat.**

## Reihenfolge

- **Phase 1 (sofort, läuft auf vorhandener Historie, keine Gates nötig):**
  Kosten-Diagnose (Hebel 1) erweitern; MERGARB-Ingester bauen;
  OPTPREM-Variantenraster vorregistrieren und Backtest starten.
- **Phase 2:** MERGARB und OPTPREM durch G0–G8 schicken; Kosten-Fix
  umsetzen, falls die Diagnose einen konkreten Hebel zeigt.
- **Phase 3 (parallel, langsamer):** `options_archiver.py` weiterlaufen
  lassen — OPTCONV braucht Monate an IV-Historie.
- **Phase 4:** nur für Kandidaten, die G5 UND G7 bestehen → `promoted.yaml`,
  danach erst die Options-Ausführungsschicht bauen.

## Ehrliche Erwartung

Dieses Design kann die 50% nicht versprechen, nur den Weg dorthin öffnen.
Selbst im optimistischen Fall — MERGARB validiert bei Sharpe ~0,6–0,7 (wie
in Mitchell/Pulvino, JF 2001), OPTPREM validiert bei Sharpe ~0,5–0,8,
Kosten-Fix hebt XSR Richtung 0,4–0,5 — landen wir eher bei **30–40%/Jahr
brutto**, nicht verlässlich bei 50%. 50% bleibt an signifikanten
Optionshebel UND signifikantes akzeptiertes Tail-Risiko gebunden, beides
erst nach echten Zahlen entscheidbar. Alle Zahlen brutto/vor
Abgeltungsteuer.

## Testing/Verification

Jeder Kandidat durchläuft dieselbe Discovery-Pipeline (G0–G8) wie
XSR/EOMT/DTRD/ONX/VOLC. Das Kosten-Variantenraster (Hebel 1) und das
OPTPREM-Variantenraster werden vor dem ersten Lauf fixiert, nicht
nachträglich gewählt. Jede Entscheidung landet in `FINDINGS.md` +
`kill_registry.yaml`.
