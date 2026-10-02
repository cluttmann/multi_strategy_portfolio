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
Wiederholung ersetzt deshalb keine marktweite Quote. EET-Käufe können weiterhin
offen bleiben und auslaufen, selbst wenn andere Handelsplätze engere Quotes
stellen.

## EET-Limits und Wiederholungen

Für normale monatliche EET-Orders gilt eine Spread-Zielgröße von 0,30 % und
eine harte Grenze von 0,50 % über den vollständigen Bid/Ask-Spread. Die erste
Order weicht höchstens 0,10 % vom Quote-Mittelpunkt ab, spätere Versuche
höchstens 0,15 %. Kauf-Limits werden abgerundet, Verkaufs-Limits aufgerundet.
Eine Order kann innerhalb des Spreads stehen bleiben. Ein Fill ist nicht
garantiert. Risikoverkäufe folgen dem bestehenden Ausstiegspfad.

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
