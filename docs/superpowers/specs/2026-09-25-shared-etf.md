# Entwurf: gemeinsam gehaltene ETFs sicher ausführen

Stand: 25.09.2026. Architekturvorschlag für das vorhandene Alpaca-Konto.
Diese Datei beschreibt eine Umsetzung; sie ändert weder Handelslogik noch
Konfiguration, Firestore, Scheduler oder Brokerpositionen.

## Ziel und Grenze

Mix8, AAA und World-Trend dürfen denselben ETF halten. Jede Strategie verfügt
ausschließlich über ihre intern zugeordneten Stücke und ihr zugewiesenes Budget.
Netzwerkfehler, doppelte Scheduler-Aufrufe und Prozessabbrüche müssen erkennbar
sein und anhand belegter Brokerereignisse aufgearbeitet werden. Eine absolute
Garantie für Broker, Cloud und Kursverfügbarkeit ist nicht möglich.

Der Tausch im Mix8 ist eine separate Konfigurationsentscheidung:

- Variante A: EEM → EET, GLD → UGLD, TLT → UBT; IEF bleibt 1×.
- Variante B: GLD → UGLD, TLT → UBT, IEF → UST; EEM bleibt 1×.

Die Bestandsarchitektur unterstützt beide. Die bisherige isolierte Rechnung
spricht bei CAGR und Sharpe eher für Variante A; die absolute Abweichung zur
kanonischen Backtest-Basis und die Gesamtdepotwirkung sind noch offen.

## Konkrete Lücken im aktuellen Code

- `main.py:2733`: `get_rotator_position_value` rechnet jedem Nutzer eines
  Tickers den gesamten Brokerbestand zu; bei Lesefehlern liefert es Nullen.
- `main.py:1079` und `main.py:2321`: Jahresrebalancing und Strategieübersicht
  nutzen dieselbe exklusive Ticker-Zuordnung.
- `main.py:1261`: der Kostenbasis-Abgleich ersetzt `total_invested` durch die
  Broker-Kostenbasis aller zugeordneten Ticker. Das kann gemeinsame Bestände
  doppelt zuordnen und vermischt Nettoeinzahlungen mit Anschaffungskosten.
- `main.py:531`: Orderaufträge tragen keine von uns vorab persistierte
  `client_order_id`.
- `main.py:1145` / `1172`: Firestore-Fehler werden abgefangen; Schreiben kann
  ohne gespeicherten Zustand weiterlaufen, Lesen mit leerem Zustand.
- `main.py:3347`: eine Zeitüberschreitung beim Warten auf Ausführung führt zu
  keinem verbindlichen Endzustand. `main.py:3160` verwendet dann geschätzte
  Verkaufserlöse als Kaufbudget.
- `main.py:2753`: fehlende Produktvolatilität kann zu Ersatzgewichten führen.
  Für die neuen 2×-Produkte muss die notwendige Preis-/Volatilitätshistorie
  vorliegen; ansonsten bleibt der Handelsplan gesperrt.

## Empfohlene erste Version

### Ein Bestand je Strategie und Wertpapier

Ein dauerhaftes Buchungsjournal speichert Käufe, Verkäufe, Gebühren,
Cashbewegungen und Korrekturen mit eindeutiger Ereigniskennung. Daraus werden
Bestand und Cash je `(Konto, Umgebung, Strategie, Ticker)` abgeleitet.
Stückzahlen und Geldbeträge verwenden Dezimalarithmetik mit expliziten
Rundungsregeln. Bruchteilsreste werden gebucht, nicht verschwinden gelassen.

Beispiel: Alpaca hält 100 UBT. Davon gehören intern 60 AAA und 40 Mix8.
Verkauft Mix8 15, ergibt der abgeglichene Zustand AAA 60 + Mix8 25 = Alpaca 85.
Eine Order darf niemals mehr verkaufen als den freien, nicht bereits für eine
andere Order reservierten Bestand derselben Strategie.

Nach Verarbeitung aller bekannten Ausführungen muss gelten:

1. Summe der internen Stücke je Ticker = bestätigter Brokerbestand.
2. Jede Ausführung wird genau einmal gebucht und genau einer Strategie zugeordnet.
3. Cash, Gebühren, reservierte Budgets und Marginverbindlichkeit sind erklärt.
4. Fehlende Daten oder ungeklärte Abweichungen sperren neue Orders.

Offene Orders und noch nicht eingelesene Ausführungen sind explizite
Zwischenzustände. Der Abgleich verarbeitet zuerst deren Brokerereignisse;
er überschreibt keine internen Anteile anhand aktueller Zielgewichte.

### Ein zentraler Weg für alle Orders

Monatliche Rotatoren, tägliche Trendwechsel, Jahresrebalancing und manuelle
Bot-Routen erzeugen versionierte Handelsabsichten. Ein zentraler Executor
führt sie pro Konto serialisiert aus. Nur dieser Executor erhält die
Berechtigung, Brokerorders zu senden. Die vorhandenen direkten Orderpfade
müssen vollständig umgestellt werden.

In Version 1 gehört jede Brokerorder genau einer Strategie. Die Reihenfolge
ist eindeutig; gegensätzliche Orders auf denselben Ticker laufen nicht parallel.
Dadurch ist die Zuordnung auch bei Teilfüllungen direkt nachvollziehbar.
Eine spätere Saldierung zwischen Strategien wäre eine eigene Erweiterung mit
Regeln für interne Übertragungen, Bewertungen und Kostenverteilung.

Kontoweite Limits prüfen das gemeinsame Budget und die Margin. Die
strategiespezifischen Grenzen prüfen die eigenen Stücke und Mittel. Käufe aus
Verkäufen werden nur durch bestätigte Ausführungen finanziert. Teilfüllungen
geben nur den tatsächlich bestätigten Betrag frei.

### Wiederaufnahme statt erneuter Ausführung

Ein Handelslauf erhält einen stabilen Schlüssel aus Konto, Umgebung,
Strategie, Termin und Aktion sowie eine gespeicherte Konfigurationsversion.
Einzahlungen und Budgets werden diesem Lauf einmalig zugeordnet.

Die Orderabsicht und ihre `client_order_id` werden vor dem Netzwerkaufruf
gespeichert. Antwortet der Broker nicht eindeutig, bleibt die Order ungeklärt.
Der Wiederanlauf sucht die vorhandene Order unter derselben Kennung und
liest Status und Ausführungen nach. Er erzeugt keine neue Kennung für eine
möglicherweise bereits angenommene Order.

Eine abgelaufene Prozesssperre allein erlaubt keine neuen Orders: erst
ungeklärte Aufträge und Brokerbestand aufarbeiten. Versionierte Sperren und
atomare Zustandswechsel verhindern, dass ein alter Worker nach Übernahme
weitere Absichten erzeugt. Wiederholte Zustellung bleibt trotzdem zu erwarten
und wird zusätzlich über die persistierte Orderkennung abgefangen.

Das Journal berücksichtigt Teilfüllung, vollständige Füllung, Ablehnung,
Stornierung und Verfall sowie Ausführungen während einer Stornierungsanfrage.
Ereignisse dürfen doppelt oder verspätet eintreffen. Der Lauf ist erst
abgeschlossen, wenn die Aufträge geklärt und die Bestände abgeglichen sind.

Firestore-Transaktionen speichern Zustandswechsel und Reservierungen;
Brokeraufrufe erfolgen außerhalb der Transaktion, da Firestore die
Transaktionsfunktion bei Konflikten wiederholen darf. Broker und Firestore
bilden keine gemeinsame atomare Transaktion; diese Lücke decken persistierte
Absichten, Wiederaufnahme und Abgleich ab.

### Bewertung, Steuern und Berichte

NAV, Drawdown-Stopp und Jahresrebalancing bewerten nur die eigenen Stücke und
das eigene Cash einschließlich erklärter Verbindlichkeiten. Nettoeinzahlungen,
Anschaffungskosten und realisierte Gewinne bleiben unterschiedliche Größen.
`STRATEGY_SYMBOLS` wird zum erlaubten Universum; es bestimmt kein Eigentum mehr.

Das bestehende steuerliche FIFO-Ledger bleibt auf Konto-/Wertpapierebene.
Eine intern einer Strategie zugeordnete Verkaufsorder kann steuerlich ältere
Lots desselben Wertpapiers verbrauchen. Die ökonomische Strategiezuordnung
darf deshalb keine unabhängige steuerliche FIFO-Rechnung je Strategie vortäuschen.
Berichte zeigen Strategieergebnis und kontoweit realisierte
Steuerereignisse entsprechend ihrer jeweils dokumentierten Zuordnung.

Splits, Dividenden, Gebühren, Marginzinsen, Ein-/Auszahlungen und manuelle
Brokertrades werden eingelesen und zugeordnet. Unbekannte Bewegungen oder
Corporate Actions führen zu einem Klärungszustand. Sheet und Parqet übernehmen
weiterhin reale Brokerereignisse; interne Zuordnungen sind keine zusätzlichen
Käufe oder Verkäufe für den Import.

## Einführung und Nachweis

1. **Ledger und Executor zunächst mit heutigen Produkten bauen.** Die
   Signal-/Volatilitätsberechnung bleibt getrennt von Planung und Ausführung.
   Alle Handelsrouten werden über denselben Bestands- und Orderpfad geführt.
2. **Fehlerfälle gezielt simulieren.** Doppelte Monatsläufe, paralleler
   Tageslauf, Teilfüllung, Antwortverlust nach Annahme, Absturz nach Füllung
   vor Speicherung, verspätete Ausführung nach Storno, Firestore-Ausfall,
   abgelaufene Sperre und manuelle Position müssen die Invarianten erhalten
   oder sichtbar stoppen. Wiederholung derselben Ereignisse verändert den
   bereits gebuchten Zustand nicht.
3. **Paper-Konto vollständig durchlaufen.** Monatsrotation, täglicher
   Trendwechsel und Jahresrebalancing einschließlich gemeinsamem Ticker,
   Teilfüllungen und Wiederaufnahme prüfen. Paper belegt keine echten Spreads
   oder Ausführungsqualität; diese fließen separat in Kostenstresstests ein.
4. **Parallelabgleich mit dem Live-Konto.** Ein separater Schattenbestand
   liest reale Aktivitäten und vergleicht die heutigen exklusiven Zuordnungen.
   Er sendet keine Orders. Alle Bestands- und Cashdifferenzen müssen erklärt sein.
5. **Migration vorbereiten.** Neue Aufträge kurz sperren, laufende Aufträge
   abschließen oder ihren Zustand klären, Broker und Firestore sichern,
   aktuelle Stücke nach der bisherigen exklusiven Zuordnung importieren.
   Bestehende Margin und freies Cash explizit zuordnen; Unklarheiten nicht schätzen.
   Anfangs-NAV und historische Spitzen für DD-Stops konsistent übernehmen.
6. **Anlageentscheidung abschließen.** Kanonische Backtest-Basis reproduzieren
   und die gewählte Mix8-Variante im gesamten Depot samt gemeinsamen
   Steuer-Lots prüfen. Ein isolierter Mix8-Vorteil wird nicht unverändert zum
   Depotvorteil, und der UGLD-Verlauf vor Mai 2026 bleibt modelliert.
7. **Kontrolliert aktivieren.** Erst wenn die neue Ausführung mit heutigem
   Portfolio stimmt, die gewählte Produktkonfiguration aktivieren. Signale
   bleiben auf ungehebelten Reihen, Gewichte nutzen die tatsächliche
   Volatilität der gehaltenen Produkte. Jede erste Order und ihr Endbestand
   müssen zum gespeicherten Plan passen.

Ein Rückfall auf alten Code mit exklusivem Ticker-Eigentum ist nach der
Migration kein sicherer Rollback. Der Rückweg beginnt mit Handelssperre,
Brokerabgleich und Wiederherstellung des neuen Journals; eine spätere
Rückumschichtung ist eine eigene Handelsentscheidung.

## Primärquellen zu den Schnittstellen

- [Alpaca: Order anlegen und anhand der Client Order ID abrufen](https://docs.alpaca.markets/us/docs/working-with-orders)
- [Alpaca: Order-Lebenszyklus und Zustände](https://docs.alpaca.markets/us/docs/orders-at-alpaca)
- [Firestore: atomare Transaktionen und mögliche Wiederholung der Transaktionsfunktion](https://firebase.google.com/docs/firestore/manage-data/transactions)
