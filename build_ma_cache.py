"""Pre-computes MA5/MA10/MA20/MA60(季線)/MA240(年線) (daily closing-price
moving averages) for every concept_map stock, cached to ma_cache.json
(gitignored). The dashboard's 均線總覽 tab reads this cache instead of
recomputing it on every refresh - Shioaji's kbars() (needed for historical
daily closes, same as trading/ma_data.py uses for the swing strategy's own
MA5/20/60) is per-stock and not batchable like snapshots(), so fetching it
for ~180 concept_map stocks takes a few minutes - far too slow for the live
dashboard's 1-minute refresh cadence. Run this on its own, slower schedule
instead (once per day, e.g. before market open); build_dashboard_live.py
just reads whatever's in the cache and overlays the live current price.

MA240 (年線) needs ~240 trading days of history (~335+ calendar days with
weekends/holidays) - far more than trading/ma_data.py's daily_closes()
gives (DAYS_BACK=90, sized for the swing strategy's own MA60 ceiling, not
touched here so that production code path stays exactly as validated).
daily_closes_long() below fetches its own longer window directly instead.

Usage:
    python build_ma_cache.py
    python build_ma_cache.py --live   # real account instead of simulation
"""
import argparse
import json
import os
import sqlite3
import sys
import warnings
from datetime import date, timedelta

import pandas as pd

import db
import utils
from trading import ma_data

MA_PERIODS = (5, 10, 20, 60, 240)
REQUIRED_PERIODS = (5, 10, 20, 60)  # MA240 (年線) is best-effort, not required to
# keep a stock - as of 2026-10, Shioaji's simulation history depth per stock
# varies (seen 167-230 calendar days of trading history on different stocks on
# the same day) and none currently reach the ~240 trading days MA240 needs;
# requiring it would empty the whole cache. Ships as null until a stock's
# available history crosses that line - see dashboard.html.template's JS,
# which already renders a missing MA as "—" and excludes it from the blend.
LONG_DAYS_BACK = 400  # calendar days - comfortably covers 240 trading days
OUT_FILE = "ma_cache.json"


def daily_closes_long(api, contract, days_back=LONG_DAYS_BACK):
    """Same resample-to-daily-close logic as trading/ma_data.py's
    daily_closes(), just with a longer, configurable lookback - needed for
    MA240 (年線), which that function's fixed 90-day window can't reach."""
    end = date.today()
    start = end - timedelta(days=days_back)
    kbars = api.kbars(contract, start=start.isoformat(), end=end.isoformat())
    if not kbars.ts:
        return pd.Series(dtype=float)
    df = pd.DataFrame({"ts": pd.to_datetime(kbars.ts), "close": kbars.Close}).set_index("ts")
    return df["close"].resample("1D").last().dropna()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="use real account instead of simulation mode")
    parser.add_argument("--db", default=db.DB_PATH)
    parser.add_argument("--out", default=OUT_FILE)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    codes = sorted(code for code, in conn.execute("SELECT DISTINCT stock_id FROM concept_map"))
    names = dict(conn.execute('SELECT "公司代號", "公司簡稱" FROM company_info'))
    conn.close()
    print(f"{len(codes)} concept_map stocks")

    utils.load_env()
    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY")
    if not api_key or not secret_key:
        print("Missing credentials - see shioaji_test.py's docstring for .env setup")
        sys.exit(1)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import shioaji as sj

        api = sj.Shioaji(simulation=not args.live)
        api.login(api_key=api_key, secret_key=secret_key)

        result = {}
        for code in codes:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            closes = daily_closes_long(api, contract)
            mas = ma_data.compute_mas(closes, MA_PERIODS)
            if all(p in mas for p in REQUIRED_PERIODS):
                result[code] = {"name": names.get(code, code),
                                 **{f"ma{p}": mas.get(p) for p in MA_PERIODS}}  # ma240 may be None

        api.logout()

    n_with_240 = sum(1 for v in result.values() if v.get("ma240") is not None)
    print(f"{len(result)}/{len(codes)} 檔有足夠歷史資料算出MA5/10/20/60"
          f"（其中{n_with_240}檔歷史夠長、也算出了MA240）")

    out = {"date": date.today().isoformat(), "stocks": result}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"saved: {args.out}")


if __name__ == "__main__":
    main()
