# Präregistrierung — Holistische Strategiesuche, 2026-09-17

Vorab festgelegt, **bevor** irgendein Backtest lief. Zweck: das Ranking soll
hinterher nicht das Ergebnis von Nachjustieren sein.

## Frage

Wenn das gesamte Depot auf dem Prüfstand steht und **keine** Ticker vorbelegt
sind — welche Strategien tragen eine gehebelte, weltweit über Assetklassen,
Regionen und Sektoren gestreute Allokation?

## Harte Designregeln

1. **Hebelkonsistenz.** Innerhalb einer Strategie wird auf *jedes* risikoreiche
   Bein derselbe Hebel angewandt. Kein 2× US neben 1× EM. Cash bleibt 1×.
   Hebelstufen: 1,0 / 1,5 / 2,0 / 2,5 / 3,0.
2. **Hebel wird modelliert, nicht unterstellt.** Tagesreset auf die
   Total-Return-Reihe, Finanzierung = Fed Funds + Spread, minus Kostenquote.
   Das Modell wird gegen 14 reale gehebelte ETFs kalibriert; Trackingfehler
   wird berichtet.
3. **Kein Bitcoin, keine Krypto.**
4. **Keine Ticker-Vorbelegung.** Bestehende Live-Strategien haben keinen Vorrang.
5. **Ein gemeinsames Fenster** für das Hauptranking: 2000-01-03 bis 2026-09-11.
   Enthält Dotcom, Finanzkrise, Covid, 2022, 2025. Familien, die es nicht
   abdecken können (HAA: TIPS-Canary ab 2000-07), laufen auf eigenem Fenster
   und werden **getrennt** ausgewiesen, nie ins Hauptranking gemischt.
6. **Kosten:** 5 bp je gehandelter Seite. Zusätzlich Stresslauf bei 15 und 30 bp.
7. **Ausführung:** Signal am Monatsschluss, Ausführung am Schluss des nächsten
   Handelstags. Kein Handel auf dem Kurs, der das Signal erzeugt hat.

## Universum (vorab fixiert)

Assetklassen: US Large, US Small, US Small Value, US Large Value, Nasdaq-100,
Europa, Japan, Pazifik, Emerging Markets, REITs, Gold, Silber, Rohstoffe (GSCI),
Treasuries 7-10J, Treasuries 20J+, TIPS, IG-Corporates, High Yield, Aggregate,
Managed Futures, Cash.

Sektoren (9, SPDR ab 1998-12): Technologie, Energie, Financials, Healthcare,
Industrials, Consumer Staples, Consumer Discretionary, Utilities, Materials.

## Strategiefamilien (vorab fixiert, mit Quelle)

| # | Familie | Idee | Quelle |
|---|---|---|---|
| A | Statische Gewichte | Risk Parity by design, kein Timing | Dalio; Browne; Bogle |
| B | Absoluter Trend (SMA/EMA) | Time-Series-Momentum je Asset | Faber 2007; Moskowitz/Ooi/Pedersen 2012 |
| C | Relatives Momentum Top-N | Cross-Sectional-Momentum | Jegadeesh/Titman 1993 |
| D | Dual Momentum | relativ + absolut kombiniert | Antonacci 2014 |
| E | Canary / Protective | Frühwarnsignal schaltet defensiv | Keller & Keuning (VAA/PAA/BAA/HAA) |
| F | Adaptive Asset Allocation | Momentum + Inverse-Vol + Vol-Ziel | Butler/Philbrick/Gordillo 2012; Moreira/Muir 2017 |
| G | Risk Parity | Inverse-Vol / ERC / HRP | Qian; Maillard 2010; López de Prado 2016 |
| H | Sektorrotation | Momentum über 9 US-Sektoren | Moskowitz/Grinblatt 1999 |
| I | Regionenrotation | Momentum über 5 Weltregionen | Asness/Liew/Stevens 1997 |
| J | Trend + Diversifikator | Aktien-Trend plus Gold/Managed Futures | Hurst/Ooi/Pedersen 2017 |

## Auswertung — vorab festgelegt

- **Primärmaß ist nicht der rohe Sharpe.** Gerankt wird nach dem
  **Deflated Sharpe Ratio** mit dem *tatsächlichen* N aller gerechneten
  Konfigurationen (Bailey/López de Prado 2014). Ein Ergebnis, das die Korrektur
  nicht besteht, kommt nicht in die Top-25.
- **Out-of-Sample-Split:** 2000-2014 (in-sample) / 2015-2026 (out-of-sample).
  Beide Hälften werden ausgewiesen. Strategien, deren Sharpe um mehr als 0,25
  einbricht, werden markiert.
- **Krisenfenster** einzeln: Dotcom, Finanzkrise, Covid, 2022, 2025.
- **Korrelationsmatrix** der Top-Kandidaten — für die Portfoliokonstruktion
  zählt Orthogonalität, nicht Einzel-Sharpe.
- Erwartung, vorab notiert: Bei mehreren hundert Konfigurationen wird die
  Deflation **die Mehrheit der Spitzenwerte kassieren**. Das ist das erwartete
  Ergebnis, kein Fehlschlag.

## Was diese Studie nicht leistet

Kein Vorwärtstest. Keine EUR-/Nachsteuerrechnung. Keine Order. Reihen vor
ETF-Auflage sind Modelle. Die Auswahl der Familien ist selbst retrospektiv —
sie stammt aus der Literatur, aber die Literatur kennt die Krisen auch.
