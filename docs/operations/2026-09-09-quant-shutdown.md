# Quant-Desk stillgelegt (2026-09-09)

Auf Nutzerwunsch wurde der Quant-Desk in `trading-436516`, Region
`europe-west3`, stillgelegt:

- Alle 12 `quant-*` Cloud Scheduler sind PAUSED (frisch verifiziert).
- Cloud Run Job `quant-desk` gelöscht; anschließende Job-Liste bestätigt Abwesenheit.
- Vor Löschung waren alle 331 vorhandenen Executions beendet.
- Kein lokaler Quant-LaunchAgent, Crontab oder laufender Quant-Python-Prozess gefunden.
- Die 9 Index-Alert-Scheduler bleiben ENABLED.
- Der ältere ETF-Bot und sein Build-Trigger bleiben bis zur Scope-Klärung unverändert.
- Keine Orders oder Positionen verändert, keine Broker-Konten geschlossen.
  Quant-Paper-Konto PA3IN7QIGPSE: ACTIVE, 122 Positionen, 0 offene Orders.
- EODHD Billing live geprüft: Fundamentals EUR 59.99 monatlich, nächste
  Abbuchung 2026-09-11; EOD All World EUR 19.99 monatlich, nächste Abbuchung
  2026-09-10. Beide aktiv. Kündigung übernimmt laut Nutzerwunsch der Nutzer.
- SMA-Alerts verwenden ausschließlich EODHD `/eod/` und `/real-time/`;
  diese sind im All-World-Paket enthalten. Fundamentals dafür nicht erforderlich.

Konfiguration, API-Zustand und Verifikationsdaten liegen lokal unter
`/tmp/hfea-shutdown-20260909/`. Dieser Ordner enthält private Kontodaten und
ist kein Bestandteil des Repositorys. Historische Forschungs-/Handelsdaten
wurden erhalten. Quant-Job oder Scheduler nicht ohne erneuten Nutzerauftrag
reaktivieren.

Offen: Nutzerklärung, ob auch der ältere ETF-Bot stillgelegt werden soll und
ob „PayPal-Account“ tatsächlich den Alpaca-Paper-Account bezeichnet.
