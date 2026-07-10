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


def run(eod=True, news=True, minute=True):
    if eod:
        # Catch up on any missed days, up to a week back.
        for i in range(7, 0, -1):
            d = dt.date.today() - dt.timedelta(days=i)
            if d.weekday() < 5:
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
