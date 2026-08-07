# Finale Daten-Roadmap — Quant-Desk (Stand 2026-07-10)

Dedupliziert über alle fünf Domänen-Reports. Wichtigste Querschnitts-Entscheidung vorab: **Insider-Daten (Form 4) beziehen wir über die SEC-Bulk-Datasets, NICHT über den EODHD-Endpunkt** — inhaltlich identisch, aber die EODHD-Variante würde 200–400k Calls gegen unsere bereits ausgeschöpfte 100k/Tag-Quota kosten, die SEC-ZIPs kosten null. Analog gilt: alles, was quota-frei ist (SEC, FINRA, FRED, CBOE, Binance S3, Alpaca), läuft parallel; die knappe EODHD-Quota reservieren wir für die billigen 1-Call-Endpunkte.

---

## 1. TOP-10-PRIORITÄTEN

Sortierung: (A) direkter Lift für validierte Sleeves ONX/XSR/VOLC/CTREND → (B) neue XSR-Features → (C) neue Strategieräume.

| # | Quelle & Endpunkt | Feature-Zweck | Aufwand | Quota/Kosten |
|---|---|---|---|---|
| **1** | **FRED-Tiefe** (Key vorhanden): `api.stlouisfed.org/fred/series/observations` — `BAMLH0A0HYM2` (HY-OAS), `BAMLC0A0CM`, `NFCI`, `STLFSI4`, `T10Y2Y`/`T10Y3M`, `DFII10`, `T10YIE`, `DTWEXBGS`, `OVXCLS`/`GVZCLS`/`EVZCLS`, `WALCL`, `RRPONTSYD` | Der stärkste fehlende Regime-Block: HY-OAS-Δ5d + NFCI-Z als Gates für **VOLC/CTREND**, Cross-Asset-Vol-Dispersion, Liquiditäts-Proxy. **Pflicht: ALFRED-Vintages (`realtime_start`) gegen Revisions-Lookahead.** | **4–8 h** | Frei, 120 req/min. Bestes Aufwand/Nutzen der ganzen Roadmap. |
| **2** | **FINRA Daily Short Sale Volume**: `cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt` | `short_vol/total_vol`-Ratio + Rolling-Z als Feature 37 im LightGBM-Ranker (**ONX/XSR**); akademisch belegter 1–20d-Prädiktor. Nur als Ratio/Z nutzen (erfasst ~50–60 % des Volumens). | **8–16 h** (~4.200 Files seit 2009) | Frei, kein Key; 1 req/s reicht. |
| **3** | **EODHD Earnings-Kalender + Trends**: `GET /api/calendar/earnings` (volle Historie, actual vs. estimate, **BMO/AMC-Timing**) + `GET /api/calendar/trends` | (i) Fixt das Announcement-Day-Alignment unserer bestehenden **SUE-Features** — Hygiene am validierten Ranker; (ii) Estimate-Revision-Momentum als neues Feature; (iii) "Tage bis Earnings" als Risiko-Feature. Trends hat keine Historie → Snapshots sofort starten. | **4–8 h** | **1 Call/Request** — Voll-Backfill <500 Calls, passt in jede Nacht-Tranche. |
| **4** | **CBOE Free CSVs**: `cdn.cboe.com/api/global/us_indices/daily_prices/{VIX9D,VIX3M,VIX6M,VVIX,SKEW,OVX,GVZ}_History.csv` + tägliche P/C-Ratios + VX-Futures-Settlements (2004+) | **VOLC-Upgrade**: VIX9D/VIX-Invertierung (besser als unser VIX/VIX3M-Proxy), VVIX, SKEW, Equity-P/C-Z, echter VX-Roll-Yield als Carry-Feature. | **8–16 h** | Frei, kein Key; URLs gelegentlich instabil → Scraper defensiv. |
| **5** | **Binance Vision**: `data.binance.vision/data/spot/monthly/klines/...` (Spot ab 2017-08, survivorship-frei) + `data/futures/um/monthly/fundingRate` (ab 2020) | **Der CTREND-Fix**: schließt die Preislücke vor Alpaca 2021+ → Validierung über 2018-Bär + 2020-Crash + 2021-Bull. Funding-Rate als eigenes Carry/Sentiment-Feature. Parser-Falle: Header-Zeilen + µs-Timestamps ab 2025. | **4–8 h** | Frei, statisches S3, keine Limits, ToS sauber. |
| **6** | **Alpaca Auctions**: `GET data.alpaca.markets/v2/stocks/auctions` (ab 2016-01-04 verifiziert) | Close-Auction-Volumenanteil (MOC/Tagesvolumen) = Institutionsfluss-Proxy als **ONX-Feature**; saubere Overnight/Intraday-Return-Zerlegung für die BOATS-Forschung. | **8–16 h** (~30M Zeilen, batchbar) | Frei (Basic), 200 req/min → Nacht-Batches. |
| **7** | **SEC EDGAR Form 4 Bulk**: `sec.gov/data-research/sec-markets-data/insider-transactions-data-sets` (geparste TSVs ab 2006) + Daily-Index-Inkrement; CIK-Mapping via `sec.gov/files/company_tickers.json` | Neues orthogonales **XSR/ONX-Ranker-Feature-Set**: Netto-Insider-Käufe 30/90d, Cluster-Buys (≥2 Officers), CEO/CFO-Käufe. Ersetzt den EODHD-Form4-Endpunkt vollständig (Quota-Ersparnis 200–400k Calls). | **24–40 h** (Bulk trivial, Rollen-Klassifikation + Inkrement braucht Sorgfalt) | Frei; 10 req/s + User-Agent-Pflicht nur beim Inkrement, Bulk-ZIPs umgehen das. |
| **8** | **Borrow-Snapshot-Crons (2 Quellen, HEUTE starten)**: IBKR `ftp://shortstock@ftp3.interactivebrokers.com/usa.txt` + Alpaca `GET /v2/assets` (`borrow_status`, `margin_requirement`) | Borrow-Fee-Level + HTB↔ETB-Transitionen + Margin-Requirement-Sprünge = **Short-Realisierbarkeits-Filter und Crowding-Signal für XSR-Short-Legs**. Keine Historie nachkaufbar — Wert entsteht nur durch Sammeln, jede Woche Verzögerung ist verlorene Historie. | **2–4 h** gesamt | Beides frei; 1–4 Snapshots/Tag → BQ-Append. Signal nutzbar nach 3–6 Monaten. |
| **9** | **Alpaca Lücken-Backfills**: BOATS-Bars `GET /v2/stocks/bars?feed=boats` ab **2024-09-16** (wir haben nur 2026+) + News `GET /v1beta1/news?sort=asc` ab **2015** (wir haben 2016+) | 16 Monate mehr Overnight-Session-Daten → Gap-/Overnight-Momentum-**Strategieraum** mit 3× Datenbasis; +1 Jahr Benzinga für alle News-Features. Bestehender Ingest-Code, nur `start` ändern. | **4–5 h** | Frei (Basic). Real-time-BOATS bleibt ATP-gesperrt — egal für Research. |
| **10** | **Kalshi API**: `api.elections.kalshi.com/trade-api/v2/markets` + `/markets/{ticker}/candlesticks` + `/historical/markets` | CPI-/Fed-Funds-**Bracket-Verteilungen** (nicht nur Binär-Odds wie Polymarket) → implizierter Erwartungswert + Varianz als Regime-Features; Surprise = Print − Kalshi-Erwartung am Event-Tag. Collector im Stil unseres Polymarket-Sammlers. | **16–24 h** | Frei, Marktdaten ohne Key, ~10–30 req/s, CFTC-reguliert → ToS sauber. 100 Items/Page. |

**Knapp dahinter (Backlog, in Reihenfolge):** EODHD Economic Events (`/api/economic-events`, 1 Call/Req, <4 h — Event-Dummies für VOLC + Polymarket-Kreuzvalidierung 2023+); CoinMetrics Community (MVRV etc., 0,5 d — **ToS-Hinweis: CC BY-NC**, für internes Research ok) + Fear&Greed (`api.alternative.me/fng/?limit=0`, <1 h); CFTC COT (Socrata `publicreporting.cftc.gov/resource/yw9f-hn96.json`, 1 d); Coinbase-USD-Serie 2015+ (0,5 d); Alpaca Corporate-Actions-Full-Pull (0,5 d — `worthless_removal` als Bankruptcy-Label + Survivorship-Audit); EODHD GSPC.INDX-Konstituenten (0,5 d — survivorship-sauberes Trainings-Universum); Options-Bars-Aggregation ab 2024-01 (2–4 d, nur Ranker-Universe); EODHD Dividenden-Details + Alpaca `settlement_date` für die Tax-Engine (Ops, kein Alpha).

**Sonderposten außerhalb des Desk-Rankings:** XETRA-EOD des Xtrackers 2× ACWI UCITS via `GET /api/eod/{SYM}.XETRA` (Stunden, 1 Call/Tag) — ersetzt die modellierten Daten der aktiven privaten ACWI-SMA-Strategie durch echte Kurse. Trivial, diese Woche mitnehmen.

---

## 2. BigQuery Public Datasets — sofort nutzbar, null Ingestion

Direkt aus `trading-436516` per SQL joinbar mit `quant.*`:

1. **GDELT**: `gdelt-bq.gdeltv2.{events,eventmentions,gkg}_partitioned` (15-Min-Updates, GKG ab 2015). Tages-Aggregate (Goldstein/AvgTone je Land/Thema, `ECON_*`-Themen-Counts) per **Scheduled Query** nach `quant.gdelt_daily` materialisieren. **Pflicht: `_PARTITIONTIME`-Filter + Spaltenauswahl** — GKG ist 3,6 TB, eine unvorsichtige Query frisst das 1-TB-Freikontingent. Ticker-Matching über `Organizations` nur für Large Caps (noisy).
2. **On-Chain**: `bigquery-public-data.crypto_bitcoin` (verifiziert frisch bis 2026-07-11) + `crypto_ethereum` — Tagesaggregate (aktive Adressen, Tx-Count, Fees, USDT/USDC-`token_transfers`, NVT-Proxy mit Binance-Preisen) einmalig nach `quant.onchain_daily` materialisieren; danach Pennies. Auf `block_timestamp_month` partitions-filtern (`transactions` ≈ 2 TB Full-Scan). Macht Etherscan/Blockchain.com/mempool.space komplett überflüssig.
3. **`bigquery-public-data.sec_quarterly_financials`**: XBRL ab 2009 — nicht als Ersatz für EODHD-Fundamentals, sondern als **Point-in-Time-/Restatement-Kreuzvalidierung unserer SUE-Features**. Stunden-Aufwand, reine SQL.
4. **`bigquery-public-data.google_trends`** (`top_terms`): kein per-Keyword-Abruf → nur als Meme-Stock-/Crowding-Flag ("Ticker in Top-25-Trending"). Klein, ehrlich begrenzt.

---

## 3. Explizit VERWORFEN

**Redundant zu Vorhandenem / zu Gratisquellen:**
- EODHD Insider-Endpunkt (`/api/sec-filings/.../form4`) — identische Quelle wie SEC-Bulk, kostet 200–400k Calls unserer erschöpften Quota. SEC gewinnt.
- EODHD Sentiment-API — nur Aggregation über News, die wir via Benzinga 2016+ komplett haben; keine neue Information.
- EODHD Historical Market Cap (wöchentlich, ab 2020) — SharesOutstanding × Preis rechnen wir präziser selbst.
- EODHD Macro Indicators (jährlich) — nutzlos für Daily-ML; JST deckt Langfrist-Makro.
- EODHD Live-Delayed-Quotes, Mutual-Fund-Fundamentals, Search/Logos/IPO-Kalender — Alpaca SIP besser bzw. kein Feature-Wert.
- Etherscan, Blockchain.com-Charts, mempool.space — 100 % redundant zu BQ `crypto_ethereum`/`crypto_bitcoin`.
- Bybit Public Data — redundant zu Binance für den CTREND-Kernbedarf; nur bei konkretem Cross-Exchange-Feature reaktivieren.
- Alpaca Screener/Movers — Echtzeit-Snapshot ohne Historie, redundant zu EODHD-EOD.
- BLS/BEA-Daten-APIs — identische Serien via FRED; nur der Release-**Kalender** bleibt (im Economic-Events-Backlog abgedeckt).

**Tot / faktisch unzugänglich:**
- pytrends / Google Trends per Keyword — Repo archiviert 04/2025, offizielles API invite-only, Ersatz-Anbieter $50–150/Monat. Wikipedia-Pageviews wäre der saubere Ersatz (Backlog, nicht Top-10).
- StockTwits — API seit Jahren zu, keine Registrierungen.
- Metaculus — exponiert keine sauberen Community-Verteilungen mehr, Fragen-Horizont irrelevant.
- Alpaca Crypto-Cross-Venue (`loc=eu/global`) und Perps-REST-Historie — empirisch nicht existent/kaputt; Live-WS-Collector bleibt einziger Perps-Zugang.

**ToS-/Grauzonen-Probleme:**
- Congress-Trading (Senate eFD / House Clerk) — Anti-Scraping-Hürden, 30–45d-Lag, Range-Beträge, schwache Ex-post-Evidenz, freie Mirrors tot. Aufwand/Nutzen klar negativ.
- Reddit-API für Trading-Signale — kommerziell-grau laut ToS, WSB-Alpha post-2021 arbitriert.
- CoinGecko free — 365-Tage-Historien-Cap seit 2024-Policy → für Backtests wertlos.

**Aufwand > Nutzen (vorerst):**
- USAspending (Awards vorab bekannt, Ticker-Mapping signalarm), FDA/clinicaltrials.gov (PDUFA-Termine nicht strukturiert; höchstens Biotech-Ausschlussfilter), Google Patents (Assignee-Mapping = Wochen, Faktor-Horizont Jahre), App-Store-/Web-Traffic-Proxies (zu grob), EDGAR-Volltext-NLP + Earnings-Call-Transkripte (Wochen-Projekte; Transkript-POC erst, wenn ein Text-Feature explizit auf der Roadmap steht), 13F (45d-Lag, schwächer als Form 4 — hinter #7 zurückgestellt), Deribit-IV-Historie (paid; unser Alpaca-IV-Archiv seit 11.7. baut die Historie selbst auf).
- **Alpaca ATP ($99/Mo): NICHT kaufen.** Alle historischen Stock-Daten sind auf Basic bereits unbeschränkt (verifiziert); ATP kauft nur Options-NBBO + Vollmarkt-Realtime — lohnt erst mit validiertem Options-/Intraday-Sleeve.
- **EODHD-Upgrades** (Screener, Technicals, Intraday, Marketplace-Options, Extended Fundamentals): alles 403, alles entweder selbst gerechnet, durch Alpaca gedeckt oder ohne validierten Use Case.

---

## 4. Ausführungsplan

**Heute Nacht (alles quota-frei bzw. <500 EODHD-Calls, parallelisierbar):**
1. **Sofort (vor Mitternacht): beide Borrow-Snapshot-Crons live schalten** (IBKR-FTP + Alpaca-Assets → BQ-Append) — Historie unwiederbringlich. Dazu EODHD-Trends-Snapshot-Cron (auch snapshot-only).
2. FRED-Loader um die ~15 Serien aus #1 erweitern, Voll-Backfill inkl. ALFRED-Vintages (Key + Pipeline existieren).
3. FINRA-Short-Volume-Backfill-Loop starten (4.200 Files, 1 req/s → läuft über Nacht durch).
4. Binance-Vision-S3-Download (Spot-Daily/Hourly 2017+ + Funding 2020+) → unzip → BQ-Load.
5. Alpaca BOATS-Backfill 2024-09→2026 + News-Backfill 2015 (bestehender Code, `start` ändern).
6. Fear&Greed-Historie (1 Call) mitnehmen.
7. EODHD-Nacht-Tranche: Earnings-Kalender-Backfill (<500 Calls) + Delisting-Liste (1 Call) + XETRA-ACWI-Ticker.

**Diese Woche:**
- **Tag 2:** CBOE-CSVs (VIX-Familie, P/C, VX-Futures) ingestieren; BQ Scheduled Queries für `quant.gdelt_daily` + `quant.onchain_daily` aufsetzen; prüfen, ob Analyst-Ratings schon in unseren gespeicherten Fundamentals-JSONs liegen (dann Kosten null).
- **Tag 3:** FRED-/FINRA-/Binance-Features in den Feature-Store schreiben; SUE-Alignment-Fix mit BMO/AMC-Timing gegen den Ranker validieren; CoinMetrics + Economic-Events-Backfill.
- **Tag 3–4:** Alpaca-Auctions-Backfill 2016+ in Nacht-Batches (200 req/min); Corporate-Actions-Full-Pull nebenher (0,5 d).
- **Tag 4–5:** SEC-Form-4-Bulk-Backfill (quartalsweise TSVs 2006+) + CIK↔Ticker-Mapping + tägliches Inkrement aufsetzen.
- **Ende Woche / Anfang nächste:** Kalshi-Collector (Klon des Polymarket-Sammlers) + Candlestick-Backfill; danach erster Retrain des Rankers mit den neuen Features (Short-Volume-Ratio, Insider-Netto, Revision-Momentum) und VOLC-Re-Test mit VIX9D/VVIX/SKEW-Block.

**Quota-Regel für alles Weitere:** EODHD-Backfills nur nachts in Tranchen (Fundamentals = 10 Calls, News/Sentiment = 5, Kalender/EOD = 1); bei Bedarf `extraLimit` bei EODHD anfragen. Alles andere in dieser Roadmap läuft an der EODHD-Quota vorbei.

---

## 5. Modell-Evaluationsprotokoll für die neuen Daten (verbindlich)

Erkenntnis aus dem Modell-Zoo (6 Fitter, identische Folds): Fitter-Tausch bei
gleicher Information ≈ 0; neue Information hebt. Neue MODELLE sind nur
gerechtfertigt, wenn neue Daten eine Struktur haben, die GBM nicht
ausdrücken kann. Deshalb zweistufig:

**Stufe 1 — Feature-Block-Ablation (immer zuerst, Modell bleibt LightGBM):**
Jeder neue Datenblock geht als Feature-Gruppe in XSR v3, mit vorregistrierter
Ablation auf identischen Folds. Aufnahme-Kriterium: **≥ +0,02 OOS-Sharpe**
gegenüber dem Panel ohne den Block (sonst raus, dokumentiert).
Geplante Blöcke: (a) FINRA Short-Ratio-Z, (b) Insider-Netto-Käufe 30/90d
(SEC Form 4), (c) Earnings-Timing (Tage-bis/seit, BMO/AMC-Fix der SUE),
(d) Borrow-Status-Transitionen (ab ~3 Monaten Historie), (e) FRED-Regime-Z
(Interaktions-Features), (f) News-Embedding-Ton 30d, (g) VIX-Termstruktur.

**Stufe 2 — Architektur-Eskalation (nur bei struktureller Rechtfertigung):**

| Datenstruktur | Kandidat-Modell | Trigger |
|---|---|---|
| Sequenzen (Funding-Historie, Regime-Pfade) | Temporal (ALSTM/TCN) | Sequenz-Aggregat-Features schlagen in Stufe 1 an, verlieren aber offensichtlich Information durch Aggregation |
| Graphen (News-Co-Mentions, Peer-Effekte) | Cross-Sectional Attention / GAT | simples Peer-Momentum-Feature (GBM) zeigt ≥ +0,02 Sharpe |
| Text roh (2M+ News, GDELT) | Embedding-Encoder + Tabular-Head | Embedding-Aggregat-Features (GBM) schlagen an |
| Chart-Muster | CNN auf OHLC-Bildern (JKX 2023) | unabhängig testbar — publizierte Evidenz als Prior; nach v3 |
| Krypto-Entries (jetzt 2017+) | ML-Meta-Filter auf CTREND | genug Events erst mit Binance-Historie — jetzt testbar |

Jede Eskalation läuft durch denselben Zoo-Vergleich (identische Folds,
identische Portfolio-Simulation) wie GBM/Ridge/MLP. Deployment eines neuen
Modells nur bei **≥ +0,05 Sharpe gegenüber GBM auf denselben Features**
(Komplexitätskosten eingepreist).
