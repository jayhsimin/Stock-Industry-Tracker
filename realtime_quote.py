"""Intraday real-time quotes via 永豐金 Shioaji - to look at *right now* price
action for a stock/industry/concept list while the market is open, as a
supplement to the end-of-day chip/fundamental data in twstock.db (which only
publishes hours after close). Read-only: no order placement anywhere here.

Setup: see shioaji_test.py's docstring (.env with SHIOAJI_API_KEY/SECRET_KEY).

Usage:
    python realtime_quote.py --codes 2330 2317
    python realtime_quote.py --names "長榮" "陽明"
    python realtime_quote.py --industry "半導體業"
    python realtime_quote.py --concept "AI伺服器"
    python realtime_quote.py --concept "AI伺服器" --live   # real account instead of simulation
"""
import argparse
import os
import sqlite3
import sys
import warnings

import db
import utils
from stock_lookup import resolve_names, codes_by_industry, codes_by_concept

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--codes", nargs="*", default=[])
    parser.add_argument("--names", nargs="*", default=[])
    parser.add_argument("--industry", nargs="*", default=[])
    parser.add_argument("--concept", nargs="*", default=[])
    parser.add_argument("--live", action="store_true", help="use real account instead of simulation mode")
    parser.add_argument("--db", default=db.DB_PATH)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    codes = list(dict.fromkeys(args.codes))  # de-dupe, keep order

    if args.names:
        resolved, missing, weak = resolve_names(conn, args.names)
        codes += [c for c in resolved if c not in codes]
        for m in missing:
            print(f"[not found] name matched no listed company: {m}")
        for query, code, full_name in weak:
            print(f"[weak match] '{query}' only matched full legal name of {code} {full_name}")

    for industry in args.industry:
        resolved, missing = codes_by_industry(conn, [industry])
        codes += [c for c in resolved if c not in codes]
        for m in missing:
            print(f"[not found] no such 產業別: {m}")

    for concept in args.concept:
        resolved, missing = codes_by_concept(conn, [concept])
        codes += [c for c in resolved if c not in codes]
        for m in missing:
            print(f"[not found] no such concept: {m}")

    conn.close()

    if not codes:
        print("no candidate codes given (use --codes / --names / --industry / --concept)")
        return

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

        contracts, not_found = [], []
        for code in codes:
            c = api.Contracts.Stocks.get(code)
            (contracts if c else not_found).append(c or code)

        for code in not_found:
            print(f"[not found] no Shioaji contract for code {code}")

        snapshots = api.snapshots(contracts) if contracts else []
        api.logout()

    rows = []
    for s in snapshots:
        change_rate = getattr(s, "change_rate", 0) or 0
        rows.append((s.code, s.close, s.change_price, change_rate, s.total_volume, s.buy_price, s.sell_price))
    rows.sort(key=lambda r: abs(r[3]), reverse=True)

    print(f"\n{len(rows)} stocks, sorted by |change_rate%| descending (as of now, intraday):")
    print("code | close | change | change% | total_volume | buy_price | sell_price")
    for code, close, change, change_rate, vol, buy, sell in rows:
        print(f"{code} | {close} | {change} | {change_rate}% | {vol} | {buy} | {sell}")


if __name__ == "__main__":
    main()
