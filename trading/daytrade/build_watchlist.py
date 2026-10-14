"""Pre-market watchlist builder - run ONCE per day BEFORE the market opens
(see trading/daytrade/README.md's 執行節奏 - a separate, earlier schedule
from daytrade_monitor.py's every-1-minute one), not from the monitor
itself: scanning the whole market's kbars (~2000 stocks) takes several
minutes, far too slow to redo on a 1-minute cadence. Caches the selection
to watchlist_state.json (date-keyed); daytrade_monitor.py just reads that.

Usage:
    python -m trading.daytrade.build_watchlist
"""
import sqlite3
import warnings
from datetime import date, timedelta

import db
from .. import executor
from . import minute_data, watchlist


def load_whole_market(conn):
    """The WHOLE market (company_info), not concept_map's theme-curated
    ~171 stocks - this is specifically about finding genuinely liquid/
    volatile names, independent of any theme relationship."""
    codes = [r[0] for r in conn.execute('SELECT "公司代號" FROM company_info')]
    names = dict(conn.execute('SELECT "公司代號", "公司簡稱" FROM company_info'))
    return sorted(codes), names


def main():
    conn = sqlite3.connect(db.DB_PATH)
    codes, names = load_whole_market(conn)
    conn.close()
    print(f"universe: {len(codes)} whole-market stocks")

    # Last few calendar days (not just yesterday) so a weekend/holiday
    # doesn't leave us with zero bars - previous_day_aggregates() only
    # uses the most recent day actually present per stock anyway.
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=5)

    api, live = executor.login(False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        bars_by_code = {}
        for code in codes:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            bars = minute_data.bars_for_range(api, contract, start.isoformat(), end.isoformat())
            if not bars.empty:
                bars_by_code[code] = bars
        api.logout()
    print(f"{len(bars_by_code)}/{len(codes)} stocks had recent bars")

    aggs = watchlist.previous_day_aggregates(bars_by_code)
    top = watchlist.score_and_rank(aggs, names)
    path = watchlist.save_cache(top)

    print(f"watchlist ({len(top)} stocks):")
    for code, name, score in top:
        print(f"  {code} {name}  score={score}")
    print(f"saved: {path}")


if __name__ == "__main__":
    main()
