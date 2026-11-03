"""Pre-computes every company's fundamentals profile (see stock_profile.py)
to stock_profiles_cache.json (gitignored), for the dashboard's 個股基本面
tab - same caching pattern as build_ma_cache.py.

Unlike ma_cache.json, nothing here needs Shioaji (every category is a plain
twstock.db query covering the whole market, not just concept_map's curated
~180 stocks) - but building all ~2000 companies' profiles still takes
~15-20 seconds, too slow to redo on every one-minute live dashboard refresh,
so this is cached the same way. Re-run this once a day after fetch_daily.py
(same schedule spirit as build_ma_cache.py); build_dashboard.py and
build_dashboard_live.py both just read whatever's in the cache.

Usage:
    python build_stock_profiles_cache.py
"""
import argparse
import json
from datetime import date

import db
import stock_profile

OUT_FILE = "stock_profiles_cache.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=db.DB_PATH)
    parser.add_argument("--out", default=OUT_FILE)
    args = parser.parse_args()

    conn = db.get_conn(args.db)
    profiles = stock_profile.build_all_profiles(conn)
    conn.close()

    out = {"date": date.today().isoformat(), "profiles": profiles}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print(f"{len(profiles)} 檔個股基本面資料已快取 -> {args.out}")


if __name__ == "__main__":
    main()
