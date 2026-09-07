# EUR-SMA-Alert für MSCI ACWI und MSCI World (XETRA-Zeitscheibe)

Datum: 2026-09-07
Branch: `feature/acwi-world-eur-sma-alert`

## Problem

Carl hält zwei gehebelte Euro-Produkte: den 2× MSCI ACWI (Scalable/Xtrackers,
LU3386643970, „Scalumbo") und einen 2× MSCI World. Beide werden über ein
SMA-Gate auf dem **ungehebelten** Index gesteuert (so rechnet die Studie in
`research/sma_sweep_acwi.py`, `gate(sig_r=r1x, risk_r=r2x, …)`).

Es fehlt ein verlässlicher Alert auf das Gate. Der bestehende
`urth_255sma_alert` misst URTH in **USD** zu **New Yorker** Zeiten, die
Handelsentscheidung fällt aber in **EUR** an der **XETRA um 17:30 CET**. Zwei
verschiedene Zeitscheiben für dieselbe Entscheidung.

Erschwerend: Alpaca führt keine EUR-Variante des ACWI, und der Kern-Constraint
ist die Handelszeit — Carl kann nur bis 17:30 CET verkaufen und will „kurz vor
Ende" raus, nicht am nächsten Morgen.

## Entscheidung: kein Schätzer, sondern eine passende Zeitscheibe

Der naheliegende Weg — den offiziellen MSCI-EUR-Net-TR-Schluss (fixiert
~22:00 CET nach US-Schluss) aus einem Live-Proxy hochrechnen — wurde **verworfen**.

Um 17:15 CET sind erst ~1,75 von 6,5 US-Handelsstunden gelaufen. Die
verbleibenden ~4,75 Stunden bei ~60 % US-Gewicht im ACWI ergeben eine
Schätzunsicherheit von grob 0,4–0,5 % auf den *aktuellen Stand* — bei einer
Bandbreite von 1,0 % (`BAND = 0.01` in `sma_sweep_acwi.py:37`) also die Hälfte
des Schwellenwerts. Ein Alert, dessen Unsicherheit halb so groß ist wie sein
Schwellenwert, ist kein Alert.

**Stattdessen wird das Signal auf genau der Zeitscheibe definiert, auf der
gehandelt wird.** Kurs *und* SMA laufen beide auf dem 17:30-XETRA-Schluss eines
EUR-notierten, thesaurierenden 1×-Trackers. Der Schätzfehler ist damit nicht
klein, sondern nicht existent — es wird nichts geschätzt.

### Der ehrliche Preis

Die Signalreihe ist ein 17:30-gesampelter Fondspreis, die Studie lief auf einem
22:00-fixierten Net-TR-Index. Zwei Abweichungen:

1. **Zeitversatz ~4,5 h.** Ein konstanter Phasenversatz, keine Drift — er
   mittelt sich über ein 250-Tage-Fenster praktisch heraus. Vgl. die Notiz in
   `msci-index-eod-api`: LSE-Schluss vs. US-Schluss ergibt Tageskorrelation 0,64,
   aber Wochenkorrelation 0,90 — reines Timing-Artefakt.
2. **Fonds-TER ~0,20 %/Jahr.** Über eine SMA-Halbwertsbreite (~125 Tage) ein
   Versatz von ~0,10 %.

Beides eine Größenordnung unter dem 1-%-Band. Weil die Tracker **thesaurierend**
sind, sind sie Total Return per Konstruktion — kein Dividenden-Bias.

Zum Vergleich die verworfene Alternative Alpaca-`ACWI` × FX: `get_alpaca_historical_bars`
zieht Bars mit `adjustment: "split"` ([main.py:452](../../../main.py#L452)), also
**ohne Dividenden**. Gegen eine Net-TR-Studie wären das ~1,8 %/Jahr, über ein
250d-Fenster ≈ 0,9 % — praktisch das gesamte Band. Zusätzlich ist der IEX-Feed
für diese Ticker unbrauchbar: gemessen am 2026-09-07 lag die ACWI-Quote bei
bid 156,85 / ask 166,65 (6 % breit), FXE-Trades waren 90 Minuten alt.

## Datenquelle

**EODHD**, eine Quelle für beide Legs. Token liegt bestätigt im Secret Manager
von `trading-436516`; `get_secret_or_env("EODHD_TOKEN")` genügt.

| Symbol | Instrument | Fenster |
|---|---|---:|
| `IUSQ.XETRA` | iShares Core MSCI ACWI UCITS ETF Acc (EUR) | 250d |
| `EUNL.XETRA` | iShares Core MSCI World UCITS ETF Acc (EUR) | 255d |

Fensterwahl: ACWI 250d ist die Mitte des langsamen Plateaus (240–280d) aus der
EUR-Neurechnung vom 2026-08-12. World 255d übernimmt das Fenster des zu
ersetzenden `urth_255sma_alert`; die Studie maß für den World post-1988
240d (Commit `c42382d`) — 15 Tage Unterschied liegen innerhalb desselben
Plateaus, deshalb kein Einwand.

Datenqualität geprüft (2026-09-07): `IUSQ.XETRA` liefert 681 Bars seit 2024-01,
keine Nullvolumen-Tage, Lücken nur zu Ostern und Weihnachten (max. 6 Kalendertage).

## Signaldefinition

```
hist = GET /api/eod/{sym}?from=today-600d&period=d  → adjusted_close
hist = [bar für bar in hist wenn bar.date < heute]      # heutigen Bar verwerfen
live = GET /api/real-time/{sym}                     → close, timestamp

series = hist[-(n-1):] + [live]        # advisory / decisive
series = hist_inkl_heute[-n:]          # reconcile (echter Schluss)

sma    = mean(series)
diff   = (live / sma - 1) * 100
state  = "above"  wenn diff >  band
         "below"  wenn diff < -band
         "neutral" sonst                # band = noise_threshold = 1.0
```

Der heutige EOD-Bar wird bewusst verworfen und durch den Live-Kurs ersetzt,
damit der Tag nicht doppelt im Fenster steht. Die SMA schließt den heutigen Wert
ein — so rechnet auch die Studie (`sma = _rolling_mean(level, n)`, ausgewertet
auf demselben Index wie `level`).

### Referenzwerte zur Verifikation (gemessen 2026-09-07, 17:36 CET)

| | Live | SMA | Abstand | Zustand | Ausstiegslinie (SMA−1 %) |
|---|---:|---:|---:|:---:|---:|
| `IUSQ.XETRA` @250d | 107,4800 € | 97,4657 € | +10,27 % | above | 96,4910 € |
| `EUNL.XETRA` @255d | 127,3300 € | 115,9443 € | +9,82 % | above | 114,7849 € |

Die Implementierung muss diese Zahlen reproduzieren (bis auf den seither
gelaufenen Kurs).

### Plausibilitätswächter

Ein stiller falscher Kurs ist der gefährlichste Fehlermodus — er erzeugt ein
Signal, das echt aussieht. Deshalb wird **jede** dieser Bedingungen als Fehler
behandelt (siehe Fehlerverhalten), nicht als Randfall:

- Real-time-Payload enthält `"NA"` (EODHD liefert das für unbekannte Ticker mit
  HTTP 200 — geprüft: `IWDA.XETRA` und `SWRD.XETRA` antworten so).
- `len(series) != n` nach dem Aufbau.
- Neuester historischer Bar älter als 10 Kalendertage (beobachtete Maximallücke: 6).
- `abs(live / hist[-1] - 1) > 0.08` — impliziert Tickerwechsel, Split oder Fehlprint.
- Für den `decisive`-Lauf zusätzlich: Live-Zeitstempel nicht von heute.

## Läufe und Rollen

Drei Rollen pro Symbol, alle in `Europe/Berlin` (die bestehenden vier Alerts
laufen in `America/New_York` — hier falsch, weil der Anker der XETRA-Schluss ist
und die Sommerzeitumstellungen nicht deckungsgleich sind).

| Cron | Rolle | Meldet | Schreibt State |
|---|---|---|:---:|
| `0 15-17 * * 1-5` | `advisory` | nur wenn Abstand ins ±1-%-Band läuft oder das Vorzeichen gegen den gespeicherten State kippt | **nein** |
| `20 17 * * 1-5` | `decisive` | Zustandswechsel (Handelsalarm) | ja |
| `45 17 * * 1-5` | `reconcile` | nur wenn der echte Schluss einen anderen Zustand ergibt als der 17:20-Lauf | ja |

**Warum advisory keinen State schreibt:** ein am Band zitternder Intraday-Kurs
würde sonst Whipsaw-Alerts erzeugen, den der Backtest nie hatte — der schaut
ausschließlich auf Schlusskurse. Die Vorwarnung ist Komfort, nicht Signal.

**Warum es reconcile gibt:** der 17:20-Kurs ist ~15 min verzögert, die
Schlussauktion kann ihn noch bewegen. Meist unter 0,15 %, am Bandrand kann es
den Zustand kippen. Ohne diesen Lauf driftet der gespeicherte State langsam von
der Wahrheit weg. Zweitfunktion: der Lauf ist ein täglicher Lebendtest der
gesamten Kette. Beide Signale stehen aktuell ~10 % über der Linie, der Alert
wird also auf Monate schweigen — und ein schweigender Alert ist von einem
kaputten nicht zu unterscheiden.

## Fehlerverhalten

Jeder Fehler — HTTP, `"NA"`, verletzter Plausibilitätswächter — führt zu:

1. Telegram-Meldung mit `❗`-Präfix, die Symbol, Rolle und Ursache nennt.
2. HTTP 500.
3. **Kein State-Schreiben.** Der letzte bekannte Zustand bleibt stehen.

Es gibt **keine zweite Quelle als Fallback**. Das folgt derselben Regel, die
CLAUDE.md für die Margin-Gates festlegt: eine zweite *Quelle* ist kein
*Default*. Ein Alert, der bei Datenausfall stillschweigend „alles in Ordnung"
sagt, ist schlimmer als keiner.

## Codeänderungen

### `main.py`

1. **`fetch_eodhd_eod_series(symbol, days=600)`** — neuer Helper. Gibt
   `[(date, adjusted_close)]` zurück oder wirft.
2. **`fetch_eodhd_realtime(symbol)`** — neuer Helper. Gibt `(close, timestamp)`
   zurück oder wirft (inkl. `"NA"`-Erkennung).
3. **`check_unified_index_alert`** ([main.py:3239](../../../main.py#L3239)) bekommt
   drei neue Request-Parameter:
   - `source`: `"alpaca"` (Default, unverändertes Verhalten) | `"eodhd"`
   - `currency_symbol`: `"$"` (Default) | `"€"` — die Nachrichten haben `$`
     hardcodiert, das muss für EUR raus
   - `run_role`: `"decisive"` (Default) | `"advisory"` | `"reconcile"`

   Nur der `sma_crossing`-Zweig ist betroffen. Der `ath_drop`-Zweig und der
   gesamte Alpaca-Pfad bleiben unverändert — die vier bestehenden Alerts dürfen
   sich nicht anders verhalten.

   Der `in_last_hour` / `was_last_hour_alert_sent_today`-Zweig gilt nur für
   `source="alpaca"`. Er ist US-Handelszeiten-Logik; für die XETRA-Zeitscheibe
   übernimmt `run_role` diese Funktion.

4. **State-Machine unverändert.** `get_index_sma_state` / `save_index_sma_state`
   sind schon auf `(symbol, sma_period)` verschlüsselt, also laufen
   `IUSQ.XETRA`@250 und `EUNL.XETRA`@255 kollisionsfrei nebeneinander.

5. **Nachrichteninhalt** um die Ausstiegslinie erweitern: Abstand in Prozent
   *und* der absolute Kurs, bei dem das Band gerissen wird. Das ist die Zahl,
   die man am Handelstag braucht.

### CLI-Fix (Vorbedingung fürs Testen)

`--action index_alert` ist **heute kaputt**: `run_local` hat `request="test"`
([main.py:6214](../../../main.py#L6214)), `check_unified_index_alert` greift auf
`request.content_type` zu, ein String hat das nicht. Außerdem gibt es keine
Argumente für Symbol oder Fenster.

Zu ergänzen: `--index_symbol`, `--index_name`, `--sma_period`, `--source`,
`--run_role`, `--noise_threshold`, `--alert_type` plus ein minimales
Mock-Request-Objekt mit `content_type` und `get_json()`.

### `cloudbuild.yaml`

- **6 neue Scheduler-Blöcke in Wave D** (2 Symbole × 3 Rollen), Muster wie die
  bestehenden `update … || create …`-Blöcke, aber mit
  `--time-zone='Europe/Berlin'`.
- **`urth_255sma_alert` entfernen**, mit Retirement-Kommentar im Stil der
  RSSB/WTIP- und Regime-World-Blöcke.
- **Kein neuer Deploy-Schritt, keine neue Cloud Function.** Die Function-Liste
  bleibt unverändert, damit sind beide in CLAUDE.md dokumentierten Fallen
  (fehlender Entry Point / gelöschter Route-Handler) umgangen. `fn-light`
  kopiert `main.py` vollständig und braucht nur `requests` — bereits vorhanden.

### Manueller Schritt: alten Scheduler löschen

`cloudbuild.yaml` kennt nur `create`/`update` — das Entfernen des Blocks löscht
den Job **nicht**. Sonst feuert `urth_255sma_alert` weiter und sendet
widersprüchliche USD-Alerts neben den neuen EUR-Alerts:

```bash
gcloud scheduler jobs delete urth_255sma_alert --location=europe-west3
```

`msci_drop_alert` bleibt bestehen — das ist der ATH-Drop-Alert (Kreditsignal),
ein anderer `alert_type`, von dieser Änderung nicht betroffen.

## Test

Lokal, nach dem CLI-Fix:

```bash
python3 main.py --action index_alert --source eodhd --run_role advisory \
  --index_symbol IUSQ.XETRA --index_name "MSCI ACWI (EUR)" --sma_period 250 --env paper

python3 main.py --action index_alert --source eodhd --run_role advisory \
  --index_symbol EUNL.XETRA --index_name "MSCI World (EUR)" --sma_period 255 --env paper
```

Erwartung: reproduziert die Referenztabelle oben, Zustand `above`, keine
Telegram-Nachricht (kein Zustandswechsel, Abstand außerhalb des Bands).

Zusätzlich zu prüfen:

- **Regression Alpaca-Pfad:** `--source alpaca --index_symbol SPY --sma_period 200`
  muss sich exakt wie vorher verhalten.
- **Wächter:** unbekanntes Symbol (`SWRD.XETRA` → `"NA"`) muss laut scheitern,
  nicht stillschweigend `neutral` liefern.
- **Rollen:** `advisory` darf den Firestore-State nicht verändern (vorher/nachher
  vergleichen).

## Offene Punkte

Keine. Token bestätigt vorhanden, Datenquelle live verifiziert, Fenster
entschieden, alter Alert zum Ersetzen benannt.

## Bewusst nicht enthalten (YAGNI)

- Keine Backtest-Messung, was die 17:30-Ausführung gegenüber einem Morgen-Alert
  wert ist. Carl hat entschieden, kurz vor Schluss zu verkaufen; die Asymmetrie
  beim Ausstieg (Gap-down-Risiko genau an Verkaufstagen) reicht als Begründung.
- Keine zweite Datenquelle, kein Cache. Zwei HTTP-Calls pro Lauf, fünf Läufe pro
  Symbol und Tag — das rechtfertigt keine Cache-Schicht.
- Kein eigener Telegram-Kanal. Haupt-Kanal wie die bestehenden vier Alerts.
- Der 2× MSCI World finanziert laut Commit `c42382d` in **USD** statt €STR
  (−1,08 pp/Jahr gegenüber €STR-Finanzierung). Das ist ein Produktbefund, kein
  Alert-Befund, und ändert am Gate nichts.
