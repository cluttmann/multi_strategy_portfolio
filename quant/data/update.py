"""Daily incremental data update — run every trading evening (or via cron).

    python3 -m quant.data.update --all

EOD:    one bulk call for yesterday (100 quota units — negligible daily).
News:   resumes from the newest stored item.
Minute: tops up the IMOM ETF universe.
"""

import argparse
import datetime as dt
import sys

from quant.data import eodhd_ingest, minute_ingest, news_ingest


def run(eod=True, news=True, minute=True, max_gap_days=90):
    if eod:
        # Selbstheilend: schließt JEDE Lücke seit dem letzten Stand in BQ
        # (nicht nur 7 Tage — ein ausgefallener Scheduler darf keine
        # dauerhafte Lücke hinterlassen). Cap gegen Endlos-Backfills.
        from quant.config import T_EOD
        from quant.data.bq import scalar
        last = scalar(f"SELECT MAX(date) FROM `{T_EOD}`")
        start = (last + dt.timedelta(days=1)) if last else (
            dt.date.today() - dt.timedelta(days=7))
        start = max(start, dt.date.today() - dt.timedelta(days=max_gap_days))
        end = dt.date.today() - dt.timedelta(days=1)
        days = [start + dt.timedelta(days=i)
                for i in range((end - start).days + 1)]
        days = [d for d in days if d.weekday() < 5]
        print(f"eod: {len(days)} fehlende Handelstage ({start} → {end})")
        for d in days:
            try:
                eodhd_ingest.update(d.isoformat())
            except Exception as e:  # noqa: BLE001 — day-level best effort
                print(f"eod {d}: {e}")
    if minute:
        minute_ingest.backfill(minute_ingest.IMOM_ETFS, "2016-01-01")
    if news:
        news_ingest.backfill("2016-01-01")  # resume logic makes this incremental


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true")
    p.add_argument("--eod", action="store_true")
    p.add_argument("--news", action="store_true")
    p.add_argument("--minute", action="store_true")
    args = p.parse_args()
    if args.all:
        run()
    elif args.eod or args.news or args.minute:
        run(eod=args.eod, news=args.news, minute=args.minute)
    else:
        p.print_help()
        sys.exit(1)
