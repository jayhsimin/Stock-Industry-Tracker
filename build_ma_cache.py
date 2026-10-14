"""Pre-computes MA5/MA10/MA20 (daily closing-price moving averages) for
every concept_map stock, cached to ma_cache.json (gitignored). The
dashboard's 均線總覽 tab reads this cache instead of recomputing it on
every refresh - Shioaji's kbars() (needed for historical daily closes,
same as trading/ma_data.py uses for the swing strategy's own MA5/20/60)
is per-stock and not batchable like snapshots(), so fetching it for ~180
concept_map stocks takes a few minutes - far too slow for the live
dashboard's 1-minute refresh cadence. Run this on its own, slower schedule
instead (once per day, e.g. before market open); build_dashboard_live.py
just reads whatever's in the cache and overlays the live current price.

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
from datetime import date

import db
import utils
from trading import ma_data

MA_PERIODS = (5, 10, 20)
OUT_FILE = "ma_cache.json"


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
            closes = ma_data.daily_closes(api, contract)
            mas = ma_data.compute_mas(closes, MA_PERIODS)
            if len(mas) == len(MA_PERIODS):  # only keep stocks with enough history for all 3
                result[code] = {"name": names.get(code, code), **{f"ma{p}": mas[p] for p in MA_PERIODS}}

        api.logout()

    print(f"{len(result)}/{len(codes)} 檔有足夠歷史資料算出完整MA5/10/20")

    out = {"date": date.today().isoformat(), "stocks": result}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"saved: {args.out}")


if __name__ == "__main__":
    main()
