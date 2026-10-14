"""Backfills TWSE's institutional-investor data (三大法人買賣超, T86) into
twstock.db's institutional_investors table for a range of past calendar
days - needed because twstock.db normally only has today's snapshot
(fetch_daily.py has never run on a continuous daily schedule), but T86 is
one of the few TWSE endpoints that genuinely supports an explicit `date`
query param for historical days (confirmed by direct test - unlike
monthly_revenue, which is a current-month-only snapshot).

TWSE only - TPEx's equivalent (tpex_3insti_daily_trading, used for the
daily production fetch) is a latest-snapshot endpoint with no date param,
same limitation as monthly_revenue, so TPEx-listed stocks in concept_map
can't get historical institutional data this way. Built for testing
whether adding an institutional-flow condition to the swing strategy
helps - see trading/decisions.json once that backtest runs.

One HTTP call per calendar day (T86 returns ALL TWSE stocks at once,
unlike Shioaji's per-stock kbars()) - a 180-day backfill is ~180 requests,
not several thousand.

Usage:
    python backfill_institutional.py --days 180
"""
import argparse
import sys
from datetime import date, timedelta

import db
import fetch_daily


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=180, help="calendar days back from today")
    parser.add_argument("--db", default=db.DB_PATH)
    args = parser.parse_args()

    conn = db.get_conn(args.db)
    end = date.today()
    start = end - timedelta(days=args.days)

    ok, empty, failed = 0, 0, 0
    d = start
    while d <= end:
        iso = d.isoformat()
        try:
            n = fetch_daily.fetch_institutional_investors(conn, iso)
            if n:
                ok += 1
            else:
                empty += 1  # holiday or not-yet-published, not an error
        except Exception as exc:
            failed += 1
            print(f"[fail] {iso}: {exc}", file=sys.stderr)
        d += timedelta(days=1)

    conn.close()
    print(f"done: {ok} days with data, {empty} empty (holidays/weekends), {failed} failed")


if __name__ == "__main__":
    main()
