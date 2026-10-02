# Monatsausführung und Spread-Grenzen

Stand: 02.10.2026. Die Monatsausführung läuft über `monthly_invest_all` und
den gemeinsamen Account-Ledger. Die vier aktiven Sleeves behalten ihre
getrennten Positionen, Cash-Bestände und Schulden.

Ein Monatsplan reserviert sein Budget einmal und speichert feste Ziele. Die
Wiederholungen verwenden diesen Plan. Ein breiter Spread eines ETFs hält die
übrigen ausführbaren Orders nicht auf. Verkäufe und anschließende Käufe werden
anhand bestätigter Fills verbucht; offene Kauforders reservieren ihren Betrag.

## Kostenloser Quote-Feed

Ausführungskurse kommen ausdrücklich aus Alpaca IEX. Der kostenpflichtige
SIP-Tarif wird nicht aktiviert. Quote-Quelle, Bid/Ask, Stückzahl und Zeitstempel
werden gespeichert. Für eine neue Order sind positive Bid/Ask und Stückzahlen,
ein ungekreuzter Markt und ein höchstens 30 Sekunden alter Quote erforderlich.
Es gibt keinen Rückfall auf Last-Trade-Preise oder verzögerte SIP-Quotes.

IEX bildet nur einen Handelsplatz ab. Seine Spreads können deutlich weiter als
die marktweiten SIP-Spreads sein: Im untersuchten Beispiel vom 01.10.2026
standen 3,44 % auf IEX einem SIP-Spread von 0,43 % gegenüber. Eine korrekte
Wiederholung ersetzt deshalb keine marktweite Quote. Die neue Kaufregel schützt
einen gewählten Höchstpreis, ohne einen marktweiten Spread vorzutäuschen.
EET-Käufe können offen bleiben und auslaufen.

## EET-Limits und Wiederholungen

Neue Monatspläne speichern die Kaufregel `iex-bid-cap-v1`. Für normale
EET-Käufe gilt: höchstens der kleinere Wert aus aktueller IEX-Ask und
IEX-Bid × 1,001, auf Cent abgerundet. Beispiel: Bid 109,83 USD und Ask
114,14 USD ergeben ein Kauf-Limit von 109,93 USD. Eine hohe Ask zieht dieses
Limit nicht nach oben; der breite IEX-Spread allein verhindert die passive
Order nicht. Jeder Wiederholungsversuch bleibt bei derselben 0,10-%-Grenze
zum dann frischen Bid. Der absolute Preis kann sich mit dem Bid bewegen.
Die 0,10 % sind eine Preiszugabe zum beobachteten Bid, kein gemessener
marktweiter Spread und keine garantierte Ausführung.

Bestehende Pläne ohne diesen gespeicherten Marker behalten ihre ursprüngliche
Regel: vollständiger EET-Spread höchstens 0,50 %, zunächst höchstens 0,10 %,
später 0,15 % Abstand vom Mittelpunkt. Normale EET-Verkäufe verwenden weiterhin
diese Regel und runden auf Cent auf. Risikoverkäufe folgen dem bestehenden
Ausstiegspfad. Es erfolgt kein Wechsel zu einer Market-Order, wenn ein Kauf
unbefüllt bleibt.

Der Scheduler ruft den Monatsplan an den Kalendertagen 1–7 um Minute 05 und
35 zwischen 10 und 15 Uhr New Yorker Zeit auf. Neue monatliche Einstiege
sind nur zwischen 10:30 und 15:30 erlaubt; die 10:05-Auslösung wartet.
Der gemeinsame Reconcile prüft zusätzlich alle fünf Minuten bestehende Pläne
und Fills. Kurze Lease-Kollisionen werden begrenzt wiederholt.

Nach fünf Minuten ist ein Versuch zur Stornierung fällig. Die nächste
Ausführung prüft den Brokerstatus, fordert ein Storno an und muss einen
terminalen Status bestätigen, bevor sie eine neue Order-ID anlegt. Späte
Teilfüllungen zählen gegen den ursprünglichen Restbetrag. Ein unklarer
Submit-Ausgang wird anhand derselben Client-Order-ID geklärt; der POST wird
nicht blind wiederholt.
Die tatsächliche Stornozeit hängt vom nächsten Scheduler-Aufruf und der
Brokerbestätigung ab; fünf Minuten sind die Fälligkeitsgrenze.

Spätestens nach drei Handelstagen beziehungsweise nach dem Monatsfenster
werden die verbleibenden Einstiege beendet. Cash und bestätigte Positionen
bleiben im Ledger. Ein ausgelaufener Rest wird als solcher gespeichert und
nicht als sauber abgeschlossener Monatslauf gemeldet. Tägliche Trendsignale
können einen wartenden Monatsplan unter dem Account-Lease unterbrechen und
überholte monatliche Käufe ungültig machen.

Die Live-Freigabe erfordert grüne Tests, unabhängige Prüfung, erfolgreichen
regionalen Cloud Build und verifizierte Function-/Scheduler-Konfigurationen.
