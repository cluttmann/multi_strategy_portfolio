# Personal Investment Policy, Strategy Playbook & FIRE Plan

**Owner:** Carl Johannes
**Version:** 2.1
**Stand:** 25. September 2026
**Repository:** `cluttmann/multi_strategy_portfolio`
**Zweck:** Einzige maßgebliche Quelle für Portfolio-Architektur, Hebelregeln, Broker-Strategien und FIRE-Plan.

> **Änderung v2.1:** Mix8 hält für die Signale EEM/GLD/TLT nun EET/UGLD/UBT
> (je 2× täglich); IEF bleibt 1×. Mehrere Sleeves dürfen dieselben ETFs halten.
> Die gemeinsame Ausführung und getrennte Bestandsbuchführung sind in
> [shared-etf-operations.md](shared-etf-operations.md) beschrieben.
> Die Ergebnisse in Teil IV stammen weiterhin vom 23.09.2026 **vor** dieser
> Umstellung. Die isolierten Kandidatentests ersetzen keinen neuen kanonischen
> Gesamtdepot-Backtest; UGLD besitzt nur kurze echte Historie.

> **Änderungen gegenüber v1.1 (12.09.2026):** Der Alpaca-Teil war überholt. Fünf
> Strategien wurden zwischen dem 21. und 23.09.2026 aufgelöst (HFEA, SPXL 200-SMA,
> 9-Sig, Dual Momentum, Regime SSO), zwei sind neu (Mix8 Top-2, World-Trend), eine
> kehrt unter anderer Zielsetzung zurück (S&P-Trend 3×). Neu hinzugekommen:
> Teil IV mit sämtlichen Backtest-Ergebnissen und deren Methodik, das jährliche
> Januar-Rebalancing, und die Regel, dass Margin nur durch Einzahlungen abgebaut
> wird. Alles außerhalb von Alpaca ist inhaltlich unverändert aus v1.1 übernommen.

---

## Executive Summary

Das Portfolio besteht aus drei brokerspezifischen Umsetzungen plus separaten Altersvorsorgesäulen:

1. **Alpaca** — systematisches Multi-Strategie-Depot mit **vier** Live-Strategien.
2. **Scalable Capital** — strategischer Kern, 50/50 aus 2× MSCI ACWI Trendstrategie und modifiziertem Euro-HFEA.
3. **Trade Republic** — langfristige 2× MSCI World Strategie mit 255-Tage-SMA.
4. **bAV** — betriebliche Altersvorsorge, Beiträge nur bis FIRE.
5. **Gesetzliche Rente** — Beiträge nur bis FIRE.
6. **Altersvorsorgedepot** — geplant ab 2027 bis Alter 67.

Die Ansparphase akzeptiert bewusst hohe Aktien- und Hebelquoten. Das Risiko wird
gesteuert über Assetklassen-Diversifikation, Trendfilter, Momentum, Volatilitäts-
Targeting, Drawdown-Stops, defensive Cash-/Anleihe-Instrumente, begrenzte Broker-
Margin, eine absolute Schuldenobergrenze und eine geplante Entschuldungsphase vor
dem Ruhestand.

**FIRE-Ziel:** Ruhestand rund 15 Jahre ab 2026, etwa mit 48.
**Lebensstil:** 4.000 EUR netto monatlich in heutiger Kaufkraft.
**Kapitalziel:** **1,40 Mio. EUR liquides, schuldenfreies Nettovermögen in heutigem Geld** (≈ 1,884 Mio. EUR nominal 2041 bei 2 % Inflation).

---

# Teil I — Investment Policy

## 1. Zweck

Dieses Dokument definiert die strategischen Regeln des liquiden Portfolios über
Alpaca, Scalable Capital und Trade Republic, sowie den FIRE-Anspar- und
Entnahmeplan. Es dokumentiert die beabsichtigte Struktur und Entscheidungslogik,
nicht eine Momentaufnahme der Bestände.

Klar zu trennen sind: **systematische Regeln**, **diskretionäre Entscheidungen**,
**Finanzierungs-/Hebelregeln**, **Annahmen der Ruhestandsplanung** und
**Broker-Bestände zu einem Zeitpunkt**.

## 2. Portfolio-Architektur

| Plattform / Säule | Rolle | Kernansatz |
|---|---|---|
| **Alpaca** | Systematisches Multi-Strategie-Depot | **Vier** automatisierte Strategien mit unterschiedlichen Signalen und Risikotreibern |
| **Scalable Capital** | Strategischer gehebelter Kern | 50 % Euro-HFEA + 50 % 2× MSCI ACWI SMA |
| **Trade Republic** | Langfristiges gehebeltes Trendportfolio | Ziel: 100 % 2× MSCI World mit 255-Tage-SMA |
| **DBX0AN** | Defensives EUR-Instrument | Risk-off für Scalable und Trade Republic |
| **bAV** | Betriebliches Altersvorsorgekapital | Beiträge bis FIRE, danach keine neuen |
| **Gesetzliche Rente** | Lebenslanges Renteneinkommen | Beiträge bis FIRE, Ansprüche bleiben |
| **Altersvorsorgedepot** | Gefördertes privates Altersvorsorgekapital | Geplant ab 2027 bis Alter 67 |

Das Portfolio ist über **Strategien und Exposures** zu verstehen, nicht über die
Liste der bei einem Broker gehaltenen ETFs.

## 3. Quelle der Wahrheit

### Alpaca

Alpaca ist codegetrieben. Maßgeblich ist der Produktionscode auf `main` in
`cluttmann/multi_strategy_portfolio`.

Historische README-Abschnitte, Research-Dateien, Backtests, Design-Dokumente und
Altcode definieren **für sich genommen keine aktive Strategie**. Eine Strategie
ist nur dann aktiv, wenn sie Teil der aktuellen Produktionsallokation und des
Ausführungsflusses ist.

Zum Stand dieser Version gibt es genau **vier aktive Alpaca-Strategien**.

**Bei Widerspruch zwischen diesem Dokument und dem Produktionscode gilt für
Alpaca der Code.**

Konkret maßgeblich in [`main.py`](../main.py):
`strategy_allocations` (Gewichte), `STRATEGY_SYMBOLS` (Ticker-Eigentum),
`SLEEVES` (Sleeve-Register), sowie `aaa_config`, `mix8_config`,
`world_trend_config`, `spx_trend_config` (Regeln je Sleeve).

### Scalable Capital und Trade Republic

Für Scalable und Trade Republic ist **diese Policy** die strategische Quelle der
Wahrheit. Parqet und Broker-Bestände dienen der Umsetzungskontrolle.

## 4. Systematisch vs. diskretionär

**Systematisch** — mechanisch zu befolgen:

- Alpaca-Produktionsregeln und -Signale
- Alpaca-Margin-Gates
- Jährliches Januar-Rebalancing bei Alpaca
- Scalable 2× MSCI ACWI 250-Tage-SMA
- Trade Republic 2× MSCI World 255-Tage-SMA
- Risk-off-Rotationen innerhalb dieser Strategien
- Scalable Maximum-LTV-Politik

**Diskretionär** — bleibt Ermessenssache, solange hier nicht anders festgelegt:

- Zeitpunkt der Migration der Trade-Republic-Altbestände
- Zusätzliche Beiträge über den Sparplan hinaus
- Künftige Änderungen der Strategiearchitektur
- Neue externe Finanzierungsentscheidungen
- Exakte Zeitpunkte der FIRE-Entschuldungstrades im dreijährigen Gleitpfad

Die geplante Trade-Republic-Migration rund um eine spürbare Korrektur (etwa −10 %
oder mehr) ist **kein mechanisches Signal**.

---

# Teil II — Hebel- und Finanzierungspolitik

## 5. Drei Formen von Hebel

Sie dürfen niemals als gleichwertig behandelt werden.

### 5.1 In ETFs eingebetteter Hebel

Mehrere Strategien nutzen gehebelte ETFs mit 2×- oder 3×-Tagesexposure: 2× MSCI
ACWI, 2× MSCI World, 2× / 3× S&P 500, 2× Nasdaq-100, gehebelte Treasury-ETFs,
2× Gold, 2× Small Caps, 2× Emerging Markets.

Dieser Hebel sitzt **im ETF** und ist getrennt von Broker-Margin und externen
Krediten. Weil täglich zurückgesetzte Hebelprodukte pfadabhängig sind, bedeutet
ein nominelles „2×" oder „3×" **kein einfaches Vielfaches der Index-CAGR** über
lange Zeiträume.

### 5.2 Alpaca Broker-Margin

Siehe Abschnitt 6.

### 5.3 Externe Kredite

Scalable Wertpapierkredit (Abschnitt 7) und C24-Konsumkredit (Abschnitt 8).

## 6. Alpaca-Margin

Alpaca darf bis zu **+10 % zusätzliches Kontoexposure** über Broker-Margin nutzen.

Margin wird **nur** freigegeben, wenn **alle vier** Produktionsgates bestehen
([`check_margin_conditions`](../main.py), `margin_control_config`):

| # | Gate | Bedingung | Parameter |
|---|---|---|---|
| 1 | **Markttrend** | SPY über 200-Tage-SMA (mit Band) | — |
| 2 | **Finanzierungskosten** | FRED-Leitzins + Spread ≤ 8,0 % | `max_margin_rate: 0.08`, Spread 2,5 % unter 35k $, 1,0 % darüber |
| 3 | **Puffer** | Maintenance-Puffer ≥ 5 % | `min_buffer_pct: 0.05` |
| 4 | **Hebel** | **Positionswert / Eigenkapital** < 1,14× | `max_leverage: 1.14` |

> **Korrigiert 2026-09-24.** Gate 4 rechnete `portfolio_value / equity`. Alpaca
> liefert diese beiden Felder **synonym** — der Quotient ist bei jedem regulären
> Konto exakt **1,0000**, egal wie viel Margin läuft. Das Gate konnte nie
> auslösen. Gemessen am Livekonto: `equity` 13.188,55 = `portfolio_value`
> 13.188,55, `long_market_value` 14.507,87 → **echter Hebel 1,1000**. Jetzt wird
> der Bruttopositionswert (long + |short|) gegen das Eigenkapital gemessen. Ist
> kein Positionswert lesbar, gilt der Hebel als **unendlich** und das Gate fällt —
> nach derselben Regel, nach der Datenfehler nie in den aggressiveren Pfad führen.
>
> **Praktische Wirkung war begrenzt**, weil `_margin_budget` unabhängig davon bei
> +10 % deckelt (`margin = equity × 0,10 − bereits genutzt`). Genau deshalb steht
> das Konto bei exakt 1,1000 und nicht höher. Der Gurt war kaputt, der Hosenträger
> hielt. Ab jetzt hält beides.

Fällt ein Gate, ist `target_margin = 0` und das System investiert cash-only
beziehungsweise baut Hebel ab.

**Datenfehler führen nie in den aggressiveren Pfad.** Schlägt ein Datenabruf fehl
(FRED, Alpaca, SMA), schaltet das Gate auf Cash-Only. Das ist bewusst so und darf
nicht durch Fallback-Defaults „repariert" werden. `get_fred_rate` liest den
FOMC-Zielkorridor aus drei unabhängigen Quellen (FRED-JSON-API → `fredgraph.csv`
→ NY Fed `markets.newyorkfed.org`), je zwei Versuche, mit 0–25 %-Plausibilitäts-
grenze und 10-Tage-Aktualitätsprüfung. Scheitern **alle**, liefert die Funktion
`None` und das Gate geht cash-only. Ein hartkodierter Satz oder ein gecachter
Wert wäre ein Verstoß gegen diese Regel.

**Margin ist ein Konto-Overlay, keine fünfte Strategie.**

### 6.1 Margin-Abbau (neu, 23.09.2026)

**Margin-Schuld wird ausschließlich durch neue Einzahlungen abgebaut, niemals
durch Verkaufserlöse aus dem Rebalancing.**

Technisch: `_rebalance_buy_budget` berechnet das Kaufbudget als
*Monatsbudget von vor den Verkäufen + tatsächlicher Verkaufserlös*, nicht aus dem
Netto-Cash. Ohne diese Regel verrechnet `max(0, cash)` den Erlös gegen die
Schuld — bei 1.500 $ Schuld und 2.262 $ Erlös wären nur 762 $ (34 %) investiert
worden, und der abgebende Sleeve stünde danach prozentual wieder über Ziel.

Fehlt der Kontostand von vor den Verkäufen (Datenfehler), greift die alte
konservative Regel: lieber zu wenig kaufen als zu viel.

## 7. Scalable Wertpapierkredit

**Kernregel:** `Scalable-Kredit / Bruttowert der Scalable-Wertpapiere ≤ 20 %`
(Nenner vor Abzug des Kredits).

**Absolute Obergrenze:** **250.000 EUR**. Das ist eine persönliche Strategie-
grenze, keine Annahme über die tatsächlich eingeräumte Linie. Die Live-Kreditlinie
des Brokers gilt immer.

**Zinssatz:** zuletzt ca. 3,49 % p. a. variabel. Der FIRE-Plan darf **nicht**
unterstellen, dass dieser Satz dauerhaft bleibt. Stresstests müssen deutlich
höhere Sätze abbilden.

**Zweck der 20-%-Regel:** großen Abstand zur Belehnungsgrenze halten,
Margin-Call- und Zwangsverkaufsrisiko senken, externen Hebel deutlich unter dem
ETF-internen Hebel halten, und erhebliche Marktrückgänge verkraften, bevor
Finanzierung zum dominanten Risiko wird.

**Verhalten im Drawdown:** Die 20 % sind **kein Auftrag, im Crash mechanisch
nachzuhebeln.** Steigt die LTV über 20 %, weil Kurse fallen:

- nicht automatisch mehr aufnehmen,
- Kreditaufnahme stoppen,
- Verhältnis durch neue Beiträge und Markterholung zurückführen,
- nur entschulden, wenn Broker-Sicherheit oder der Ruhestandsgleitpfad es verlangt.

Ziel ist die Vermeidung prozyklischer Kreditaufnahme und erzwungener Trades.

## 8. Externer Konsumkredit (C24)

- Ursprungsbetrag: **30.000 EUR**
- Laufzeit: **84 Monate**
- Monatsrate: **429,68 EUR**
- Keine geplanten Ersatz- oder Aufstockungskredite
- Verwendung: investiert ins Scalable-Portfolio
- Wirtschaftliche Zurechnung: **gesamtes Scalable-Portfolio**, nicht ein einzelner Sleeve
- Zinssatz laut Repository-Policy ca. 5,29 % p. a.; **maßgeblich ist der unterschriebene Kreditvertrag**

**Cashflow-Regel:** Die Rate liegt **innerhalb** des FIRE-Cashflow-Budgets. Nach
vollständiger Tilgung wird sie nicht anderweitig verbraucht, sondern wird zu
zusätzlichem Investitionsspielraum bei gleichbleibendem Gesamtsparbudget.

---

# Teil III — Strategy Playbook

## A. Alpaca Multi-Strategie-Depot

### A0. Zielallokation

| Strategie | Zielgewicht | Rolle |
|---|---:|---|
| **Mix8 Top-2** | **42,50 %** | Breiteste Momentum-Rotation, bester Sleeve auf CAGR und Sharpe |
| **7-Asset-Rotator (AAA)** | **21,25 %** | Taktischer Assetklassen-Diversifikator |
| **World-Trend** | **21,25 %** | Globales Aktien- plus Gold-Trendfolgen, geringste Korrelation zum Rest |
| **S&P-Trend 3×** | **15,00 %** | Hält das Depot in langen US-Haussen mit |
| **Summe** | **100,00 %** | |

**Herleitung der Gewichte** (gesetzt 23.09.2026): Basis AAA / World-Trend / Mix8
= 1 : 1 : 2 aus einem 66-Mix-Raster auf 1994–2026 und 2000–2026 nach deutscher
Steuer, jeder Rotator auf der exakten Live-Sizing-Logik. Darauf 15 % S&P-Trend 3×,
pro rata aus den drei anderen entnommen, damit das Depot in langen Aktienhaussen
nicht zurückfällt. Der S&P-Trend senkt den Anteil der 5-Jahres-Fenster hinter dem
MSCI World von 23 % auf 12 %.

Die Gewichte steuern die **Aufteilung der Monatseinzahlung**. Der Monatslauf
investiert jeweils das **gesamte verfügbare Kontoguthaben**.

---

### A1. Mix8 Top-2 — 42,50 %

**Zweck.** Die breiteste Momentum-Rotation im Depot: acht Kandidaten über
US-Aktien, Nasdaq, Industrieländer ex USA, Schwellenländer, Gold, zwei
Laufzeitbänder Staatsanleihen und Managed Futures. Sie darf sich jeden Monat die
zwei stärksten aussuchen und sonst in Cash gehen. Über beide Testfenster, vor wie
nach Steuern, der beste Sleeve auf CAGR **und** Sharpe — deshalb das größte
Gewicht.

**Universum.** Signal auf dem **ungehebelten** Symbol, gehalten wird das Produkt:

| Signal | Gehalten | Exposure |
|---|---|---|
| SPY | **SSO** | 2× S&P 500 |
| QQQ | **QLD** | 2× Nasdaq-100 |
| EFA | **EFO** | 2× Industrieländer ex USA |
| EEM | **EET** | 2× Schwellenländer |
| GLD | **UGLD** | 2× Gold |
| IEF | **IEF** | 1× US-Treasuries 7–10 J |
| TLT | **UBT** | 2× US-Treasuries 20+ J |
| KMLM | **KMLM** | 1× Managed Futures |

**Defensiv:** SGOV (0–3 Monate T-Bills).

**Regeln.**

1. **Monatlich** am ersten Handelstag (Tag 1–7) auswerten.
2. Momentum = Mittel aus 3-, 6- und 12-Monats-Rendite (63 / 126 / 252 Handelstage), je **1/3 gewichtet**, gerechnet auf dem Signalsymbol.
3. **DD-Stop zuerst:** liegt der Sleeve-NAV mehr als **30 %** unter seinem Hochpunkt, alles nach SGOV, Hochpunkt zurücksetzen, Monat beenden.
4. **Top 2** nach Score auswählen, aber nur mit **positivem** Score (`min_score = 0`). Kein positiver Kandidat → alles SGOV.
5. **Inverse-Volatilitätsgewichtung** über die Ausgewählten, realisierte Vol der **gehaltenen Produkte** über **60 Handelstage**. Fehlende Produktvolatilität stoppt die Ausführung.
6. **Vol-Target 25 %** annualisiert: erwartete Portfoliovol aus Gewichten × Produktvol; Skalierungsfaktor `min(1, 0,25 / erwartete Vol)`. Der Produkthebel ist darin enthalten und wird nicht nochmals multipliziert.
7. Rest nach **SGOV**.
8. Trades unter **5 $** werden übersprungen.

---

### A2. 7-Asset-Rotator (AAA) — 21,25 %

**Zweck.** Der taktische Assetklassen-Diversifikator. Anders als die aktienlastigen
Sleeves kann er zwischen Aktien, Staatsanleihen zweier Laufzeiten, Gold und
Rohstoffen rotieren. Hält drei statt zwei Positionen und reagiert mit reinem
6-Monats-Momentum träger als Mix8 — das ist beabsichtigt, damit sich die beiden
Rotatoren nicht identisch verhalten.

**Universum.**

| Signal | Gehalten | Exposure |
|---|---|---|
| SPY | **NTSD** | **reines Aktienexposure**, gemessen 0,86 US + 0,59 international = 1,45× — *keine* Anleihen |
| IWM | **SAA** | 2× US Small Caps |
| EEM | **EET** | 2× Schwellenländer |
| TLT | **UBT** | 2× US-Treasuries 20+ J |
| IEF | **UST** | 2× US-Treasuries 7–10 J |
| GLD | **UGL** | 2× Gold |
| DBC | **DBC** | breite Rohstoffe |

**Defensiv:** SHV (kurzlaufende Treasuries).

**Regeln.** Wie Mix8, mit vier Abweichungen:

- Momentum = **reine 6-Monats-Rendite** (126 Handelstage), keine Mischung.
- **Top 3** statt Top 2.
- Defensiv **SHV** statt SGOV.
- Alles übrige identisch: DD-Stop 30 %, `min_score = 0`, Inverse-Vol über 60 Tage, Vol-Target 25 %, Toleranz 5 $.

**Gemeinsame ETFs:** AAA behält UGL als Goldprodukt. EET und UBT können zugleich
in AAA und Mix8 liegen; UGLD zugleich in World-Trend und Mix8. Das Account-Ledger
führt Mengen, wirtschaftlichen Einstand und Cash je Sleeve getrennt; der Broker
hält die Summe. Steuerliches FIFO bleibt auf Konto-/Ticker-Ebene.

---

### A3. World-Trend — 21,25 %

**Zweck.** Das einzige Bein ohne US-Momentum-Logik und mit der geringsten
Korrelation zum Rest (0,47 zu S&P-Trend, 0,60 zu Mix8). Zwei gleich große
Hälften, jede mit eigenem Trendfilter: global diversifizierte Aktien und Gold.
Live seit 23.09.2026.

**Beine.**

| Signal (EODHD) | Gehalten | Exposure | Anteil |
|---|---|---|---|
| `URTH.US` | **WLDU** | **2× VT** (Vanguard Total World) — siehe Warnung unten | 50 % |
| `GLD.US` | **UGLD** | 2× Gold | 50 % |

**Defensiv:** USFR (Floating-Rate Treasuries).

**Regeln.**

1. **Täglich** auswerten, 15:50 New Yorker Zeit, Montag bis Freitag.
2. Je Bein: Signal auf dem **ungehebelten** Index gegen dessen **150-Tage-SMA**.
3. **1-%-Band** um die SMA und **3 aufeinanderfolgende Bestätigungstage**, bevor der Zustand kippt.
4. Bein an → seine 50 % in das gehebelte Produkt. Bein aus → seine 50 % in **USFR**.
5. Beide Beine sind unabhängig. Ein Bein an, eines aus = 50 % Produkt, 50 % USFR.

> **⚠️ Signal und Instrument haben nicht dasselbe Universum.** WLDU hebelt 2× den
> **Vanguard Total World Stock ETF (VT)**, das Signal läuft auf **URTH (MSCI
> World)**. Regression über 03–09/2026: Beta **1,982 auf VT bei R² 0,9898**,
> gegen URTH nur R² 0,9565; in der gemeinsamen Regression fällt URTH auf −0,08.
> **VT enthält Schwellenländer und Small Caps, URTH nicht.**
> ρ(VT, URTH) = 0,984 — der Trendzustand stimmt praktisch immer überein, aber es
> ist eine bewusste Abweichung. **Der Backtest modelliert `world` als MSCI World
> (URTHSIM), also das Signal, nicht das gehaltene Instrument.** Offene
> Entscheidung: Signal auf `VT.US` umstellen, damit beide dasselbe Universum
> haben.

**Datenquelle: EODHD, nicht Alpaca.** Grund: URTH handelt auf IEX nur ~5.700
Stück/Tag; IEX zeigte am 01.05.2023 und 18.05.2023 Schlusskurse 7 % neben dem
konsolidierten Kurs sowie an 9 von 1.027 Tagen einen abweichenden Bandzustand.
EODHDs `adjusted_close` ist Total Return — genau das, worauf der Backtest lief.

**Datenschutzschalter:** `max_live_jump = 0,20` (Gold fiel am 30.01.2026 real
10 % — die Prüfung fängt nur Tickerwechsel und kaputte Prints ab),
`max_quote_age_minutes = 120`.

---

### A4. S&P-Trend 3× — 15,00 %

**Zweck.** Nicht die Sharpe zu maximieren — dafür ist er neutral bis leicht
negativ —, sondern zu verhindern, dass das Depot in langen US-Haussen hinter dem
MSCI World zurückbleibt. Bei 15 % sinkt der Anteil der 5-Jahres-Fenster hinter
dem MSCI World von **23 % auf 12 %** (1994–2026), und 2009–2021 steigt von
10,4 % auf 12,4 % p. a. (MSCI World 12,3 %). Preis: 2022 −18 % statt −14 %.

Dies ist der am 22.09.2026 unter Max-Sharpe-Zielsetzung aufgelöste SPXL-SMA-Sleeve,
zurückgeholt unter einer anderen, ausdrücklich formulierten Zielsetzung.

**Bein:** Signal `SPY.US` (EODHD, Total Return) → **SPXL** (3× S&P 500).
**Defensiv:** BIL (1–3 Monate T-Bills).

**Regeln.** Täglich, **200-Tage-SMA**, **1-%-Band**, **1 Bestätigungstag**
(schneller als World-Trend). SPY über Band → SPXL, unter Band → BIL.

---

### A5. Depotregeln

**Monatslauf.** `monthly_invest_all` feuert **12:00 New Yorker Zeit an Tag 1–7**
und ruft alle Sleeve-Funktionen **in einem Prozess** auf. Er prüft die
Margin-Gates **einmal**, berechnet die Budgetaufteilung **einmal** und übergibt
jedem Sleeve sein vorab zugeteiltes Budget.

> Für Produktionsläufe immer `monthly_invest_all` verwenden. Die verbleibenden
> manuellen Monatsrouten delegieren ebenfalls an den gemeinsamen, idempotenten
> Orchestrator; eigene Brokerorders außerhalb des Ledgers sind gesperrt.

**Beitrags-Tilt (11 Monate im Jahr).** Neue Einzahlungen werden Richtung
untergewichteter Sleeves gekippt (`rebalance_config`):

- `aggressiveness: 2.0` — aggressiver Tilt
- `max_single_strategy_pct: 0.50` — Deckel je Sleeve = max(50 %, 1,5 × Zielgewicht) der Monatseinzahlung
- `min_floor_pct_of_target: 0.50` — jeder Sleeve bekommt mindestens die Hälfte seines Zielanteils, damit der Tilt kleine Allokationen nicht aushungert

**Jährliches Rebalancing im Januar.** `annual_rebalance_month: 1`. Im Januar
stellt der normale Monatslauf **alle Sleeves auf Zielallokation**, statt nur die
Einzahlung zu kippen:

1. Ist-Werte direkt aus den Positionen lesen (wirft bei Lesefehler, statt leere Sleeves anzunehmen)
2. Je Sleeve `Zielquote × (Summe Sleeves + Monatsbudget) − Ist` → positiv = auffüllen, negativ = abgeben
3. Übergewichtete verkaufen **zuerst**
4. Untergewichtete kaufen aus Monatsbudget **+ tatsächlichem Verkaufserlös** (siehe 6.1)
5. Bringen die Verkäufe weniger als geplant, werden die Käufe anteilig gekappt

Belegt durch Backtest 23.09.2026: in **65 von 65** Fünfzehnjahresfenstern besser
als der reine Tilt, **+0,25 bis +0,41 pp IRR nach Steuern**, schlechtester MaxDD
**−25,4 % statt −29,4 %**. Der Tilt allein ließ S&P-Trend bis auf 27 % laufen.

**Die Margin-Gates blockieren das Januar-Rebalancing nicht.** `_is_rebalance_run`
hebelt `_contribution_gate` bewusst aus — das Rebalancing muss gerade nach einem
Drawdown laufen, wenn die Gewichte am weitesten verrutscht sind.

**Ticker-Eigentum.** Jeder ETF gehört genau einem Sleeve (`STRATEGY_SYMBOLS`).
Das hält die Kostenbasis-Zuordnung eindeutig und verhindert, dass zwei Strategien
dieselbe Position gegeneinander handeln.

**Tägliche Trendprüfung.** `daily_trend_sleeves` feuert **15:50 New Yorker Zeit,
Mo–Fr** und wertet World-Trend und S&P-Trend aus.

**Wächter.** `audit_monthly_run` feuert **14:00 New Yorker Zeit an Tag 8** und
meldet, wenn der Monatslauf nicht als abgeschlossen markiert ist.

### A6. Datenquellen und Infrastruktur

| Zweck | Quelle | Anmerkung |
|---|---|---|
| Ausführung, Positionen, Kontostand | Alpaca (Live) | |
| Kurse für Rotatoren | Alpaca IEX-Feed | 5-Minuten-Cache in Firestore, `get_all_market_data` — **nicht umgehen** |
| Trendsignale (World-Trend, S&P-Trend) | **EODHD** | Total Return, siehe A3 |
| Leitzins für Margin-Gate 2 | FRED, `fredgraph.csv`, NY Fed | drei unabhängige Quellen, kein Default |
| Strategiezustand | Firestore `strategy-balances-{env}` | |
| Benachrichtigungen | Telegram | jedes bedeutsame Ereignis |

**Region:** `europe-west3`, GCP-Projekt `trading-436516`.
**Deployment:** Push auf `main` löst den Cloud-Build-Trigger aus; kein manuelles
`gcloud builds submit`.

---

## B. Scalable Capital

Zwei gleich große strategische Sleeves: **50 % Euro-HFEA + 50 % 2× MSCI ACWI
250-Tage-SMA**. Der Wertpapierkredit wird auf Kontoebene gesteuert und keinem
Sleeve zugeordnet.

### B1. Euro-HFEA — 50 % des Scalable-Portfolios

| Asset | Anteil im Sleeve | Anteil Gesamt-Scalable | Instrument | ISIN |
|---|---:|---:|---|---|
| 2× MSCI World | 50 % | **25 %** | Amundi MSCI World (2x) Leveraged UCITS ETF Acc | `FR0014010HV4` |
| Gold | 25 % | **12,5 %** | EUWAX Gold II | `DE000EWG2LD7` |
| US-Langläufer | 12,5 % | **6,25 %** | iShares $ Treasury Bond 20+yr UCITS ETF | `IE00BFM6TC58` |
| EUR-Langläufer | 12,5 % | **6,25 %** | Amundi Euro Government Bond 25+Y UCITS ETF | `LU1686832194` |

**Rebalancing:** Zielallokation ist bindend. Keine feste Kalenderfrequenz als
harte Regel. Neue Beiträge dürfen zur Wiederherstellung genutzt werden, bevor
verkauft wird.

**Rolle:** Modifizierte europäische HFEA-Umsetzung. Zweck ist **nicht** maximaler
Aktienhebel, sondern die Kombination aus gehebelten Industrieländeraktien, Gold,
US-Duration und EUR-Duration — Renditetreiber, die sich über Makroregime hinweg
unterschiedlich verhalten sollen.

### B2. 2× MSCI ACWI mit 250-Tage-SMA — 50 % des Scalable-Portfolios

**Risk-on:** Scalable MSCI AC World Leveraged Daily Swap, ISIN `LU3386643970`
(≈ 2× MSCI ACWI, €STR-finanziert, Net TR, TER 0,45 %).

**Signal:** auf dem **ungehebelten MSCI ACWI in EUR**, nicht auf dem gehebelten
ETF. **250-Tage-SMA** mit kleinem Rauschband.

**Risk-off:** Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C, `LU0290358497`
(WKN DBX0AN).

**Zustandslogik:** ACWI im Risk-on → 2× ACWI halten. Risk-off → DBX0AN. **Ein
Kursrückgang allein ist kein Kaufsignal** — der SMA-Zustand bestimmt das Asset.

**Fensterwahl ist gemessen, nicht geraten.** 250 Tage: nach deutscher Steuer
13,17 % CAGR / Sharpe 0,402 gegen 12,68 % / 0,379 bei 255 Tagen, und ab 1988
ebenfalls das Optimum des langsamen Plateaus. Beide liegen im langsamen Plateau
(240–280 Tage); die schnelle Hälfte (160 Tage) gewinnt vor Steuern in 74 % der
Bootstrap-Pfade, nach Steuern ist es ein Gleichstand, weil sie 37 % mehr handelt.

### B3. Konto-Hebel-Overlay

1. Brutto-LTV ≤ **20 %**
2. Absolute Schuldenobergrenze **250.000 EUR**
3. Live-Kreditlinie des Brokers sticht die theoretische Grenze immer
4. **Kein automatisches Nachhebeln im Drawdown**
5. Zinsen sind Teil des 2.500-EUR-Monatsbudgets
6. Vor FIRE wird die Schuld bewusst zurückgeführt

---

## C. Trade Republic

**Strategisches Ziel:** 100 % des Trade-Republic-Kapitals in der 2× MSCI World
255-Tage-SMA-Strategie.

**Risk-on:** Amundi MSCI World (2x) Leveraged UCITS ETF Acc, `FR0014010HV4`.
**Signal:** ungehebelter MSCI World in EUR, Referenz `EUNL.XETRA`, **255-Tage-SMA**.
**Risk-off:** DBX0AN, `LU0290358497`.

**Altbestände.** Trade Republic enthält noch Positionen aus früheren Ansätzen
(1× MSCI World, Emerging Markets, World Value, World Quality, World Momentum,
World Small Caps). Das sind **keine eigenständigen strategischen Sleeves mehr**,
sondern Altkapital, das auf die Migration wartet.

**Migrationsregel.** Zeitpunkt diskretionär; Absicht ist eine Migration rund um
eine spürbare Korrektur (etwa −10 % oder mehr). Das ist **ausdrücklich keine
automatische −10-%-Handelsregel.** Zum Migrationszeitpunkt tritt das Kapital in
den **aktuellen Zustand** des SMA-Systems ein: Risk-on → 2× World, Risk-off →
zuerst DBX0AN. **Ein Crash bedeutet also nicht automatisch, 2× World zu kaufen.**

### C1. EUR-SMA-Alerts laufen auf der XETRA-Zeitscheibe

Die Alerts für beide Euro-Produkte (`acwi_eur_sma_*`, `world_eur_sma_*`) laufen
über **EODHD** auf dem **17:30-XETRA-Schluss** von `IUSQ.XETRA` (250 Tage) und
`EUNL.XETRA` (255 Tage) — nicht auf dem offiziellen MSCI-EUR-Schluss um 22:00.

**Grund:** Gehandelt wird bis 17:30 CET. Um 17:15 sind erst 1,75 von 6,5
US-Handelsstunden gelaufen; eine Hochrechnung auf 22:00 hätte bei ~60 %
US-Gewicht eine Unsicherheit von 0,4–0,5 % — die Hälfte des 1-%-Bands. Auf der
17:30-Scheibe wird nichts geschätzt: Kurs **und** SMA liegen auf derselben
Zeitscheibe wie die Ausführung.

**Drei Lauf-Rollen:** `advisory` (15:00–17:00, Vorwarnung, schreibt bewusst
**keinen** State — sonst erzeugt ein am Band zitternder Intraday-Kurs
Whipsaw-Alerts, die der Backtest nie hatte), `decisive` (17:20, Handelsalarm,
schreibt State), `reconcile` (21:00, Abgleich auf den echten Schluss).
`reconcile` ist zugleich der tägliche Lebendtest — beide Signale stehen ~10 %
über der Linie, der Alert schweigt also auf Monate, und ein schweigender Alert
ist von einem kaputten nicht zu unterscheiden.

**Zweiter Telegram-Kanal** über `public_chat_secret` (nur am
`acwi_eur_sma_decisive`-Job): dorthin gehen **ausschließlich echte Crossings** —
nicht die Vorwarnungen, nicht die Reconcile-Korrekturen, nicht die Datenfehler.

Jeder Datenfehler → Telegram mit `❗` + HTTP 500 + **kein** State-Schreiben.
Keine zweite Quelle als Fallback, nach derselben Regel wie bei den Margin-Gates.

---

# Teil IV — Backtest-Ergebnisse (Alpaca)

## 12. Methodik

Alle Zahlen in diesem Teil stammen aus **einer** Rechnung mit identischen
Annahmen. Frühere Zahlen in Code-Kommentaren, README-Ständen oder
Research-Dateien können abweichen und sind **überholt**.

| Annahme | Wert |
|---|---|
| **Engine** | Lot-Ledger mit FIFO je Ticker, gegen die Referenzimplementierung auf **0,00 pp bei 0 bp Kosten** validiert |
| **Ausführung** | Rotatoren monatlich, Trendfilter täglich, Positionen driften zwischen den Läufen (wie live) |
| **Hebelmodell** | Täglicher Reset; Finanzierung = (L−1) × echter Fed-Funds-Satz (FRED) + Spread |
| **Spreads** | Gegen die **echten** ETFs CAGR-geeicht: Aktien 3× 1,72 %, Anleihen 3× 0,60 %, SSO 0,55 %, QLD 0,40 %, EET 0,92 %, EFO 0,94 %, UGL 1,53 %, UBT −0,43 %, UST −0,26 % |
| **TER** | 0,95 % (3×), 0,93 % (2×), 0,75 % (1,5×), 0,09 % (1×) |
| **Handelskosten** | 5 bp je gehandelter Seite auf tatsächlichen Umschlag |
| **Cash** | verdient 3-Monats-T-Bill |
| **Sharpe** | excess of cash, annualisiert |
| **Steuer** | 26,375 % (25 % + Soli), Kirchensteuer 0, Sparerpauschbetrag 0, Teilfreistellung je Ticker aus [`tax/config.py`](../tax/config.py), Verlustvortrag, Steuer am Jahresende aus dem Depot bezahlt |
| **Nicht modelliert** | Ausschüttungen und Vorabpauschale (nur realisierte Kursgewinne), Slippage über 5 bp hinaus |

**Warum die Spread-Eichung zählt.** Eine pauschale Spread-Tabelle je Hebelstufe
behandelt Anleihen-LETFs wie Aktien-LETFs. Anleihen-LETFs finanzieren sich über
Treasury-Futures nahe dem Repo-Satz — TMF wurde dadurch um 4 pp/Jahr zu teuer
gerechnet. Die Eichung korrigiert **in beide Richtungen**: SPXL wird dadurch
minimal schlechter, nicht besser.

**Warum Testfolio höhere Zahlen zeigt.** Testfolios `UPROSIM` unterstellt eine
Finanzierung von Fed Funds + 0,85 %. Gegen den **echten** UPRO über 2009–2026
(reale CAGR 32,82 %) getestet liefert diese Annahme 35,16 % — **+2,33 pp zu
hoch**. Die hier verwendete Eichung liegt mit 31,95 % um 0,88 pp darunter, also
leicht konservativ. **Testfolio-Zahlen taugen nicht als Zielvorgabe.**

## 13. Ergebnisse 2000–2026 (26,6 Jahre)

| Sleeve | Gewicht | CAGR vor St. | Vol | Sharpe | MaxDD | CAGR nach St. | Sharpe nach | MaxDD nach |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 7-Asset-Rotator | 21,25 % | 14,42 % | 21,9 % | 0,63 | −34,4 % | 11,65 % | 0,52 | −35,3 % |
| World-Trend | 21,25 % | 13,33 % | 19,0 % | 0,65 | −34,0 % | 10,92 % | 0,53 | −35,5 % |
| Mix8 Top-2 | 42,50 % | 16,00 % | 21,7 % | 0,70 | −34,0 % | 13,30 % | 0,59 | −36,7 % |
| S&P-Trend 3× | 15,00 % | 13,08 % | 32,7 % | 0,48 | −55,8 % | 11,52 % | 0,44 | −60,0 % |
| **Depot** | **100 %** | **15,55 %** | **19,6 %** | **0,73** | **−26,9 %** | **13,06 %** | **0,62** | **−26,9 %** |

**Der wichtigste Wert steht in der letzten Zeile:** Der Depot-Drawdown von
−26,9 % liegt **7,5 Prozentpunkte flacher** als der beste Einzel-Sleeve und
**33 Punkte** flacher als S&P-Trend allein. Das ist der Diversifikationseffekt
und der eigentliche Grund für die Aufteilung.

### Korrelationen (nach Steuern)

| | AAA | World-Trend | Mix8 | S&P-Trend |
|---|---:|---:|---:|---:|
| **7-Asset-Rotator** | 1,00 | 0,65 | 0,73 | 0,54 |
| **World-Trend** | 0,65 | 1,00 | 0,60 | 0,47 |
| **Mix8 Top-2** | 0,73 | 0,60 | 1,00 | 0,68 |
| **S&P-Trend 3×** | 0,54 | 0,47 | 0,68 | 1,00 |

Mittlere Korrelation **0,59**. Daraus folgt eine theoretische Sharpe-Obergrenze
von rund S̄/√ρ̄ ≈ 0,77 nach Steuern. Das Depot liegt bei 0,62 — der Abstand ist
**nicht** durch Umgewichten zu holen, sondern nur über einen Sleeve, der mit den
anderen weniger gemein hat.

## 13a. Ergebnisse 1994–2026 (32,2 Jahre)

Das früheste gemeinsame Fenster aller vier Sleeves, limitiert durch Emerging
Markets (Juni 1994). Es enthält die Hausse 1994–1999 **vor** dem Dotcom-Crash
und ist deshalb freundlicher als das Fenster ab 2000.

| Sleeve | Gewicht | CAGR vor St. | Vol | Sharpe | MaxDD | CAGR nach St. | Sharpe nach |
|---|---:|---:|---:|---:|---:|---:|---:|
| 7-Asset-Rotator | 21,25 % | 15,51 % | 21,2 % | 0,66 | −34,4 % | 12,50 % | 0,54 |
| World-Trend | 21,25 % | 11,29 % | 18,1 % | 0,54 | −34,0 % | 9,31 % | 0,43 |
| Mix8 Top-2 | 42,50 % | 18,54 % | 22,7 % | 0,75 | −34,0 % | 15,39 % | 0,63 |
| S&P-Trend 3× | 15,00 % | 15,84 % | 36,4 % | 0,52 | −57,4 % | 14,14 % | 0,47 |
| **Depot** | **100 %** | **16,96 %** | **20,1 %** | **0,75** | **−26,9 %** | **14,31 %** | **0,63** |

**Fünf Verlustjahre von 33.** Schlechtestes −18,9 % (2022), bestes +55,4 % (2003).
Mittlere Korrelation 0,61.

**Der einzige Sleeve, der mit dem längeren Fenster schlechter wird, ist
World-Trend** (13,33 → 11,29 % vor Steuern, Sharpe 0,65 → 0,54). Grund: Gold
befand sich 1994–2000 in einem Bärenmarkt und Nicht-US-Aktien liefen deutlich
hinter den USA. Wer die Gewichtung von World-Trend allein auf dem 2000er-Fenster
begründet, überschätzt den Sleeve.

### Welches Fenster gilt?

**Beide.** Sie beantworten verschiedene Fragen:

- **Ab 2000** ist die konservative Rechnung — der Start liegt auf dem Dotcom-Hoch.
- **Ab 1994** ist die vollständige Rechnung und die, die mit externen Backtests vergleichbar ist.

Wo die Fenster sich widersprechen (vor allem bei World-Trend), ist die
Aufteilung **nicht** durch die Datenlage bestimmt. Die Gewichte 1:1:2 wurden
deshalb bewusst auf **beiden** Fenstern gerastert.

### Krisen (Depot nach Steuern)

| Ereignis | ab 2000 gerechnet | ab 1994 gerechnet | MaxDD im Zeitraum |
|---|---:|---:|---:|
| Dotcom (03/2000 – 10/2002) | +11,5 % | **−11,3 %** | −23,4 % |
| Finanzkrise (10/2007 – 03/2009) | −2,2 % | −2,2 % | −18,3 % |
| Covid (02/2020 – 03/2020) | −22,4 % | −22,4 % | −23,6 % |
| Zinswende 2022 | −18,9 % | −18,9 % | −17,2 % |

> **Die Dotcom-Zahl des 2000er-Fensters ist geschönt und darf nicht zitiert
> werden.** Die Rotatoren brauchen 252 Handelstage Vorlauf, bevor sie ein
> 12-Monats-Momentum bilden können. Startet die Rechnung im Januar 2000, sitzen
> sie während des Crash-Beginns noch in Cash — nicht wegen ihrer Regeln, sondern
> mangels Historie. **Maßgeblich ist −11,3 % aus dem 1994er-Fenster.**

### Jahresrenditen (Depot nach Steuern)

| Jahr | | Jahr | | Jahr | |
|---|---:|---|---:|---|---:|
| 2000 | +8,2 % | 2009 | +20,7 % | 2018 | −5,4 % |
| 2001 | +1,9 % | 2010 | +7,0 % | 2019 | +6,4 % |
| 2002 | +4,0 % | 2011 | +1,0 % | 2020 | +28,5 % |
| 2003 | +54,6 % | 2012 | +0,4 % | 2021 | +37,7 % |
| 2004 | +0,5 % | 2013 | +46,2 % | 2022 | **−18,9 %** |
| 2005 | +4,4 % | 2014 | +12,9 % | 2023 | +13,8 % |
| 2006 | +23,7 % | 2015 | −17,6 % | 2024 | +33,6 % |
| 2007 | +13,1 % | 2016 | +12,3 % | 2025 | +40,9 % |
| 2008 | +3,5 % | 2017 | +39,3 % | 2026* | +13,1 % |

\* bis 11.09.2026. **Drei Verlustjahre von 27.** Schlechtestes −18,9 % (2022),
bestes +54,6 % (2003).

## 14. Was diese Zahlen nicht sind

- **Keine Zusage.** Vier Sleeves, jeder mit Parametern, die auf Daten gewählt wurden, die auch im Test stecken. Erwarte weniger.
- **Nicht das gleiche Fenster für alle Bausteine.** Die frühestmögliche gemeinsame Historie ist **Juni 1994**, limitiert durch Emerging Markets. Das Fenster ab 2000 beginnt auf dem Dotcom-Hoch und ist die konservativere Rechnung.
- **Ohne Ausschüttungen und Vorabpauschale.** Die Nachsteuerzahlen sind damit noch leicht zu gut, ungleichmäßig über die Sleeves — die Anleihebeine schütten am meisten aus.
- **UGLD hat 4 Monate echte Historie** (aufgelegt 27.05.2026) und ~14.500 $ IEX-Tagesvolumen. Die 2×-Gold-Modellierung stützt sich auf UGL.
- **NTSD wird als 1,5× US-Aktien modelliert — das ist in der Höhe fast richtig, in der Zusammensetzung falsch.** Regression über 03–09/2026: **0,857 SPY + 0,593 EFA, R² 0,974**; nimmt man IEF hinzu, bekommt es Beta **0,000**. NTSD hält also ~1,45× **reines Aktienexposure**, davon 41 % international, und **keine Treasuries**. Das Modell bildet die Gesamthöhe ab, verbucht aber den internationalen Teil als US. Folge: **die Auslandsdiversifikation des 7-Asset-Rotators ist im Backtest unterschätzt, die US-Konzentration überschätzt.**

---

# Teil V — FIRE-Plan

## 15. Ziel

**Zeitpunkt:** rund 15 Jahre ab 2026, also etwa 2041 / Alter 48. Der exakte
Zeitpunkt hängt am realen, schuldenfreien Portfolioziel, nicht am Kalender.

**Lebensstil:** **4.000 EUR netto monatlich in heutiger Kaufkraft** = 48.000 EUR
netto real pro Jahr. Das ist das Lebensstilziel, **nicht** die Brutto-Entnahme;
Steuern sind separat hochzurechnen.

## 16. Kapitalziel

| | Real (heutiges Geld) | Nominal 2041 (2 % Inflation) |
|---|---:|---:|
| **Bevorzugtes Ziel** | **1,40 Mio. EUR** | **≈ 1,884 Mio. EUR** |
| Untere Planungsgrenze | ≈ 1,25 Mio. EUR | ≈ 1,682 Mio. EUR |

Die untere Grenze ist **nicht** das bevorzugte Ziel, sondern der Bereich, in dem
der Plan noch funktionieren kann, wenn Ausgaben flexibel sind,
Guyton-Klinger-Kürzungen akzeptiert werden, Rente und bAV wie prognostiziert
kommen und die ersten Ruhestandsjahre keine schwere Negativsequenz bringen.

## 17. Warum Guyton-Klinger

Die Arbeit von Guyton und Klinger (2006) testete dynamische Entnahmeregeln über
40-Jahres-Zeiträume. Für Portfolios mit mindestens ~65 % Aktien berichtete sie
anfängliche Entnahmeraten von **5,2–5,6 %** bei sehr hoher Erfolgswahrschein-
lichkeit — **wenn** die Entscheidungsregeln angewandt werden.

> Guyton-Klinger ist **keine „5,2-%-Regel".** Die höhere Startrate wird durch die
> Bereitschaft erkauft, die Ausgaben zu ändern, wenn sich das Portfolio
> verschlechtert.

### Die Regeln

**Anfängliche Entnahmerate** = Brutto-Entnahme im ersten Jahr / liquides
Nettoportfolio.

**Inflationsregel (modifiziert):** Normalerweise steigt die Entnahme mit der
Inflation. Nach einem Jahr mit negativer Portfoliorendite entfällt die
Inflationsanpassung, **wenn** die aktuelle Entnahmerate über der anfänglichen
liegt. Es gibt **kein Nachholen**.

**Kapitalerhaltungsregel:** Steigt die aktuelle Entnahmerate über **120 %** der
anfänglichen, wird die geplante Jahresentnahme um **10 % gekürzt**.
Bei 4,0 % Startrate → Schwelle 4,8 %. Bei 4,5 % → 5,4 %.

**Wohlstandsregel:** Fällt die aktuelle Entnahmerate unter **80 %** der
anfänglichen, wird die Entnahme um **10 % erhöht**.
Bei 4,0 % → Schwelle 3,2 %. Bei 4,5 % → 3,6 %.

### Warum der Plan konservativer ist als 5,2 %

1. **Horizont.** Ruhestand mit 48 kann deutlich länger dauern als die untersuchten 40 Jahre.
2. **Portfoliostruktur.** Gehebelte ETFs, systematische Trend-/Momentum-Regeln, externer Hebel und steuerliche Realisierung aus taktischen Strategien — nicht die Struktur der Originalstudie.
3. **Ausgabenziel.** 4.000 EUR netto sollen ein komfortabler Normalzustand sein, keine Zahl, die schon in einem gewöhnlich schwachen Markt sofort 10 % Kürzung verlangt.
4. **Steuerunsicherheit.** Für 48.000 EUR netto braucht es mehr als 48.000 EUR brutto. Das deutsche Steuerrecht 2041 ist heute unbekannt.
5. **Sequenzrisiko.** Die ersten 10–20 Jahre entscheiden. Deshalb der Eintritt ohne Schulden, mit niedrigerer Startrate und den Guardrails als zusätzlichem Sicherheitsnetz.

### Operatives Entnahmeziel

Bevorzugte anfängliche Brutto-Entnahmerate: **4,0–4,5 %** des realen liquiden
Nettovermögens. Bei 1,40 Mio. EUR real: 4,0 % = 56.000 EUR brutto/Jahr,
4,5 % = 63.000 EUR.

Das vereinfachte Planungsmodell unterstellt, dass ~56.000 EUR brutto rund
48.000 EUR netto tragen. **Das ist zum tatsächlichen Ruhestandszeitpunkt neu zu
rechnen** — mit dem dann geltenden Steuerrecht und der dann vorliegenden
Kostenbasis.

## 18. Ansparplan

**Monatsbudget: 2.500 EUR**, all-in für den liquiden FIRE-Plan. Es enthält die
C24-Rate, die Scalable-Zinsen und die direkten Portfoliobeiträge. Es enthält
**nicht** den vollen bAV-Beitrag (läuft separat über die Gehaltsabrechnung).

- **Solange C24 läuft:** 2.500 − C24-Rate − Scalable-Zinsen = direkter Portfoliobeitrag
- **Nach Tilgung:** 2.500 − Scalable-Zinsen = direkter Portfoliobeitrag

**Renditeannahme:** **12 % nominal p. a.** als primäre Planungsannahme. Das liegt
unter der im Backtest gemessenen Bandbreite und ist ein bewusster Abschlag. Die
Annahme ist **nicht garantiert**; Entscheidungen sind zusätzlich bei 8 %, 10 %
und 11 % zu prüfen. **Die FIRE-Entscheidung darf nicht an einer einzigen
deterministischen CAGR hängen.**

**Inflation:** 2 % p. a. Alle Ziele sind nominal **und** in heutiger Kaufkraft zu
verfolgen; die reale Zahl ist die wichtigere.

## 19. 15-Jahres-Basisszenario

Unter den modellierten Annahmen (Startvermögen Mitte 70k EUR nach Abzug des
30k-Kredits, 2.500 EUR monatlich, ein Konsumkredit ohne Aufstockung, Scalable-LTV
bis 20 %, Schuldendeckel 250k EUR, 12 % nominal, 2 % Inflation, 15 Jahre):

**Unmittelbar vor der Entschuldung:** Bruttovermögen ≈ 2,136 Mio. EUR,
Scalable-Schuld 250k, C24 0, liquides Nettovermögen ≈ **1,886 Mio. EUR**.

**Nach Tilgung:** Der Verkauf von 250k zur Tilgung von 250k mindert das
Nettovermögen nicht nochmals. Vermögen ≈ 1,886 Mio., Schulden 0, Nettovermögen
≈ **1,886 Mio. EUR** ≈ **1,40 Mio. EUR in heutiger Kaufkraft** — fast genau das
bevorzugte Ziel.

> **Wichtig:** Ein bewusster dreijähriger Entschuldungs-Gleitpfad reduziert den
> Hebel früher als dieses Modell mit konstantem Hebel und führt daher zu einem
> etwas niedrigeren Erwartungswert — bei deutlich geringerem Sequenzrisiko.

## 20. Entschuldung vor FIRE

**Ziel:** Der Ruhestand beginnt mit **0 EUR externer Konsumschuld und 0 EUR
Scalable-Margin-Schuld.**

**Gleitpfad.**

- **T−3 Jahre:** keine Erhöhung des Scalable-Kredits mehr; nach Kursgewinnen oder -verlusten **nicht** auf 20 % nachhebeln; mehr Cashflow in Schuldenabbau lenken, wo sinnvoll.
- **T−2 bis T−1:** Kombination aus Sparraten, natürlich anfallendem Cash aus SMA-Risk-off-Übergängen, steuerlich günstigen Verkäufen und ausgewählten Portfolioverkäufen.
- **FIRE-Datum:** Ziel Scalable-Schuld = **0 EUR**.

Bei stark gedrückten Märkten darf der Zeitpunkt angepasst werden, statt an einem
willkürlichen Datum einen großen Verkauf zu erzwingen — solange die
Margin-Sicherheit hoch bleibt. Der Abbau ist ein **Gleitpfad, kein starrer
Jahresbetrag.**

**Warum vor dem Ruhestand entschulden.** In der Ansparphase lautet das Ziel
weitgehend „erwartetes risikoadjustiertes Endvermögen maximieren". Im Ruhestand
ändert es sich zu „die Wahrscheinlichkeit minimieren, dass eine ungünstige frühe
Sequenz den Plan dauerhaft beschädigt". Der Wegfall der Schuld beseitigt
variable Zinsen, Zwangsverkaufsrisiko aus Broker-Haircuts, vereinfacht die
Guyton-Klinger-Entnahmen und senkt das Sequenzrisiko.

---

# Teil VI — Altersvorsorgesäulen

## 21. bAV

**Beitragsstruktur:** Entgeltumwandlung 200 EUR/Monat + Arbeitgeberanteil
100 EUR/Monat = **300 EUR/Monat**. Anbieter: Generali. Planungsstand zuletzt
≈ 10.000 EUR.

**Bei FIRE stoppen die Beiträge.** Das vorhandene Kapital bleibt bis zum
Auszahlungszeitpunkt investiert.

Frühere Planungsannahme (4 % p. a., weitere 15 Beitragsjahre): ≈ **91k EUR
nominal bei FIRE**, vor künftigen Gebühren, vertraglichen Garantien, Steuern und
Krankenversicherungsbeiträgen. Ohne weitere Beiträge bis ~67 grob ≈ **190k EUR
nominal**.

**Die bAV zählt nicht als liquides FIRE-Brückenkapital mit 48.** Sie ist eine
Reserve für die spätere Lebensphase. Zum Renteneintritt ist die Kapitalwahl
gegen die vertragliche Verrentung zu prüfen, nach dem dann geltenden Recht.

## 22. Gesetzliche Rente

**Stand 2026** (Daten bis 31.12.2025): Anwartschaft **453,35 EUR/Monat** beim
damaligen Rentenwert, **11,1142 Entgeltpunkte**, regulärer Rentenbeginn
**01.08.2060**.

**Bei FIRE stoppen die Beiträge**; erworbene Ansprüche bleiben. Da das Einkommen
derzeit an oder über der Beitragsbemessungsgrenze liegt, rechnet das Modell mit
nahezu maximalem jährlichem Punktezuwachs bis FIRE. Grobe Projektion nach
weiteren 15 Beitragsjahren: ≈ **40 Entgeltpunkte**, entsprechend rund
**1,6–1,7k EUR brutto/Monat** in heutiger Rentenwertlogik.

**Das ist eine Planungsschätzung, keine Zusage.** Rentenformel,
Beitragsbemessungsgrenze, Rentenwert, Steuern und Kranken-/Pflegebeiträge können
sich bis 2060 ändern.

**Rolle im FIRE-Plan:** Die gesetzliche Rente finanziert die **erste Phase
nicht**. Das liquide Portfolio muss **Alter 48 → 67** ohne sie überbrücken. Ab
Rentenbeginn sinkt die nötige Entnahme deutlich: Bei ~1,2–1,35k EUR netto real
Rente muss das Portfolio nur noch ~2,65–2,8k EUR netto real tragen, vor bAV und
Altersvorsorgedepot.

## 23. Altersvorsorgedepot

**Start:** 01.01.2027. **Eigenbeitrag:** 1.800 EUR/Jahr. **Grundzulage:**
540 EUR/Jahr. Zusammen **2.340 EUR** jährlich vor Rendite. Kinderzulagen sind im
Basismodell nicht enthalten.

**Bis FIRE** besteht die Berechtigung über die normale Beschäftigung.

**Nach FIRE** ist geplant, das Depot bis ~67 weiterzuführen. Da reine
Kapitaleinkünfte die Berechtigung nicht begründen, soll eine **echte
selbständige Beratungstätigkeit** erhalten bleiben. Nach den ab 2027 geltenden
Regeln sind Selbständige mit qualifizierenden Einkünften nach **§ 15 EStG** oder
**§ 18 Abs. 1 Nr. 1–3 EStG** und abgegebener Steuererklärung grundsätzlich
unmittelbar zulageberechtigt.

**Operativer Zielkorridor:** ~**3.000–5.000 EUR** echtes Beratungseinkommen pro
Jahr. Das ist ein Komfortbereich, **keine gesetzliche Mindestgrenze**. Die
Tätigkeit kann in der bestehenden Einzelunternehmer-/Freiberuflerstruktur
laufen; eine UG oder GmbH ist dafür nicht erforderlich. Die
Kleinunternehmerregelung ist eine Umsatzsteuerregelung und bleibt nutzbar, wenn
die Voraussetzungen dann erfüllt sind. **Recht und Steuern sind bei FIRE neu zu
prüfen.**

**Langfristiger Zulagenwert.** 1.800 EUR über 19 FIRE-Jahre (48→67):
Eigenbeiträge 34.200 EUR + Zulagen 10.260 EUR = **44.460 EUR** vor Rendite. Ab
2027 über ~33 volle Jahre bis 67: ≈ 59.400 EUR + 17.820 EUR = **77.220 EUR** vor
Rendite. Beides unterstellt eine unveränderte Grundzulage von 540 EUR, was über
Jahrzehnte nicht garantiert ist.

---

# Teil VII — Risiko, Kontrolle, Referenzen

## 24. Dominantes Portfoliorisiko

Trotz mehrerer Strategien bleibt der dominante Renditetreiber **gehebeltes
globales Aktien-Beta**.

> **Die Zahl der Strategien darf nicht mit echter wirtschaftlicher
> Diversifikation verwechselt werden.**

Die wichtigsten Diversifikatoren sind Gold, langlaufende Staatsanleihen, Managed
Futures, Rohstoffe, defensive Cash-/Treasury-ETFs, Trendfilter, Momentumfilter
und Drawdown-Stops.

## 25. Wesentliche Risiken

| Risiko | Beschreibung |
|---|---|
| **Schneller Crash** | SMA-Systeme reagieren erst nach Kursverfall. Ein plötzlicher Crash kann erhebliche Verluste erzeugen, bevor ein Risk-off-Signal auslöst. |
| **Whipsaw** | Seitwärtsmärkte um die SMA erzeugen wiederholte Wechsel und realisierte Steuerkosten. Bänder mindern das, beseitigen es nicht. |
| **Pfadabhängigkeit** | Täglich zurückgesetzter Hebel kann in volatilen Pfaden hinter dem einfachen Vielfachen zurückbleiben. |
| **Aktien-/Anleihe-Korrelation** | Langläufer hedgen Aktien nicht immer. 2022 fielen beide. |
| **Basisrisiko der Diversifikatoren** | Gold und Managed Futures können einen konkreten Aktien-Drawdown verfehlen oder lange underperformen. |
| **Margin-Haircut** | Scalable kann Beleihungswerte im Stress senken. Die Kreditlinie kann schneller schrumpfen als der Marktwert. |
| **Zinsrisiko** | Der Scalable-Satz ist variabel. Die Strategie muss bei deutlich höheren Kosten als 3,49 % tragfähig bleiben. |
| **Steuerdrag** | SMA-, Momentum- und Regimestrategien realisieren Gewinne. Die 12-%-Planungsrendite ist gegen die tatsächlichen Nachsteuer-Live-Ergebnisse zu prüfen. |
| **Sequenzrisiko** | Dieselbe langfristige CAGR erzeugt je nach Reihenfolge sehr unterschiedliche Ruhestandsergebnisse. |
| **Implementierungsrisiko (neu)** | Mix8 trägt 42,5 %. Fällt diese eine Strategie aus oder war sie überangepasst, trifft das fast die Hälfte des Alpaca-Depots. |

## 26. FIRE-Checkliste (Go / No-Go)

Der Ruhestand darf **nicht** allein durch den Kalender ausgelöst werden.

1. Liquides schuldenfreies Nettoportfolio ≥ **1,40 Mio. EUR real**
2. C24-Schuld = 0
3. Scalable-Kredit = 0 oder auf einem sofortigen, risikoarmen Tilgungspfad
4. 4.000 EUR netto monatlich bei **4,0–4,5 %** anfänglicher Brutto-Entnahme finanzierbar
5. Guyton-Klinger-Guardrails **vorab akzeptiert**
6. bAV-Ansprüche intakt
7. Rentenprognose aktualisiert
8. Altersvorsorgedepot-Berechtigung / Beratungsstruktur aktualisiert
9. Steuerplanung auf das dann geltende Recht aktualisiert
10. Ein schweres Drawdown-Szenario direkt nach Renteneintritt lässt den Plan noch tragfähig

## 27. Jährliche Überprüfung

Mindestens einmal jährlich aktualisieren: liquides Nettovermögen,
Bruttovermögen, alle externen Schulden, Scalable-LTV, Auslastung der
Live-Kreditlinie, tatsächliche Finanzierungssätze, Portfolio-XIRR/TTWROR,
Strategie-vs.-Backtest-Abgleich, Sparquote, inflationsbereinigtes FIRE-Ziel,
bAV-Stand, aktuelle Renteninformation, Altersvorsorgedepot-Stand und
Zulagenregeln, prognostizierte Erstjahres-Entnahmerate.

**Woran der Plan zu messen ist:**

1. Nettovermögen nach allen Verbindlichkeiten, nicht Brutto-Depotwert
2. Reale Kaufkraft, nicht nominale Portfoliogröße
3. Risiko auf Portfolioebene, nicht Anzahl der Strategien
4. Live-Ergebnisse nach Kosten, nicht nur Backtests
5. Fähigkeit, eine schlechte Sequenz zu überstehen, nicht nur die Durchschnitts-CAGR
6. Schuldenfreie Ruhestandsbereitschaft, nicht maximaler Hebel im Ziel

## 28. Aufgelöste und Alt-Strategien

**Nicht als aktive Strategien behandeln:**

| Strategie | Aufgelöst | Grund |
|---|---|---|
| Alpaca HFEA (UPRO/TMF/KMLM) | 22.09.2026 | Schlug SPY nur um 0,05 Sharpe bei identischem −56 %-Drawdown |
| Alpaca SPXL 200-SMA | 22.09.2026 | Unter Max-Sharpe neutral; kehrt als **S&P-Trend 3×** unter anderer Zielsetzung zurück |
| Alpaca Dual Momentum | 23.09.2026 | Von Mix8 mit breiterem Universum abgelöst |
| Alpaca Regime SSO | 21.09.2026 | |
| Alpaca 9-Sig (TQQQ/AGG) | 21.09.2026 | |
| Alpaca World 40/30/30 (F4) | 09.09.2026 | Unzureichende Langfristevidenz nach korrigiertem Proxy-Audit |
| Alpaca Regime World | 12.05.2026 | |
| Alpaca RSSB / WTIP | 11.05.2026 | |
| Trade Republic 1× World / EM / Faktor-ETFs | — | Altbestände, warten auf Migration; **keine** eigenständigen Sleeves |

## 29. Referenzhierarchie

**Alpaca:** 1. Produktionscode auf `main` → 2. Firestore-Zustandsdaten →
3. diese Policy → 4. Parqet-Bestände und -Performance.
*Bei Widerspruch gewinnt der Produktionscode.*

**Scalable Capital:** 1. diese Policy → 2. Kreditbildschirm/Vertrag des Brokers →
3. Parqet zur Umsetzungskontrolle.

**Trade Republic:** 1. diese Policy → 2. SMA-Monitoring-Implementierung →
3. Broker / Parqet.

**FIRE:** 1. diese Policy → 2. geltendes Steuer- und Rentenrecht zum
Entscheidungszeitpunkt → 3. aktuelle bAV-Vertragsdaten → 4. aktuelle
DRV-Renteninformation → 5. aktuelle Altersvorsorgedepot-Regeln.

## 30. Quellen

**Guyton-Klinger:** Jonathan T. Guyton, William J. Klinger, *„Decision Rules and
Maximum Initial Withdrawal Rates"*, Journal of Financial Planning, März 2006.

**Altersvorsorgedepot:** BMF-/Bundestags-Rahmen 2026 — neue Produkte ab 2027,
bis 1.800 EUR geförderter Eigenbeitrag, bis 540 EUR Grundzulage, qualifizierende
Selbständige unmittelbar zulageberechtigt. **Vor Inanspruchnahme 2041 neu
prüfen.**

---

## Schlussstatement

Das Portfolio ist in der Ansparphase bewusst aggressiv. Sein Ziel ist **nicht**,
den Hebel dauerhaft zu maximieren.

> **Diversifizierten systematischen Hebel nutzen, um in rund 15 Jahren etwa
> 1,40 Mio. EUR reales, liquides, schuldenfreies FIRE-Kapital aufzubauen — und
> dann von Vermögensmaximierung auf Kapitalerhalt und dynamische
> Guyton-Klinger-Entnahmen umzuschalten.**

Die Ansparstruktur darf komplex sein. Das Ruhestandsziel ist bewusst einfach:

> **4.000 EUR netto monatlich in heutiger Kaufkraft, keine externen Schulden,
> später ergänzt durch gesetzliche Rente, bAV und Altersvorsorgedepot.**
