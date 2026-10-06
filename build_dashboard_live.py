"""Same dashboard as build_dashboard.py, but prices/%change come from
Shioaji's live snapshot instead of twstock.db's end-of-day data - group
membership (which stocks belong to which industry/concept) still comes from
the database, only the price layer is swapped to real-time.

Setup: see shioaji_test.py's docstring (.env with SHIOAJI_API_KEY/SECRET_KEY).

Usage:
    python build_dashboard_live.py
    python build_dashboard_live.py --live   # real account instead of simulation
"""
import argparse
import json
import os
import sqlite3
import statistics
import sys
import warnings
from datetime import datetime

import db
import utils
import volume_alert

CHUNK = 400


def group_membership(conn):
    industries = {}
    for code, industry in conn.execute('SELECT "公司代號", "產業別" FROM industry_map WHERE "產業別" IS NOT NULL'):
        industries.setdefault(industry, []).append(code)

    concepts = {}
    for code, concept in conn.execute("SELECT stock_id, concept FROM concept_map"):
        concepts.setdefault(concept, []).append(code)

    return industries, concepts


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="use real account instead of simulation mode")
    parser.add_argument("--db", default=db.DB_PATH)
    parser.add_argument("--template", default="dashboard.html.template")
    parser.add_argument("--out", default="dashboard.html")
    parser.add_argument("--push-json", default="dashboard_push.json",
                         help="also write the raw data dict here, for pushing into the "
                              "artifact's live db (see README's 即時連線 section)")
    parser.add_argument("--volume-ratio-alert", type=float, default=3.0,
                         help="toast-alert a stock once its 量比 (volume_ratio) reaches this "
                              "(default 3.0x normal pace); 0 disables alerting")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    industries, concepts = group_membership(conn)
    names = dict(conn.execute('SELECT "公司代號", "公司簡稱" FROM company_info'))

    all_codes = sorted(set().union(*industries.values(), *concepts.values()))
    print(f"{len(all_codes)} unique stocks to snapshot "
          f"({len(industries)} industries, {len(concepts)} concepts)")

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

        contracts = [api.Contracts.Stocks.get(c) for c in all_codes]
        found = [(code, c) for code, c in zip(all_codes, contracts) if c is not None]
        missing = [code for code, c in zip(all_codes, contracts) if c is None]

        live = {}
        for i in range(0, len(found), CHUNK):
            chunk = found[i:i + CHUNK]
            snaps = api.snapshots([c for _, c in chunk])
            for (code, _), snap in zip(chunk, snaps):
                if snap.close and snap.close > 0:
                    live[code] = {
                        "close": snap.close,
                        "pct": round(snap.change_rate, 2),
                        "volume_ratio": snap.volume_ratio,
                    }

        api.logout()

    if missing:
        print(f"{len(missing)} codes had no Shioaji contract (skipped)")
    print(f"{len(live)} stocks had a usable live snapshot")

    if args.volume_ratio_alert > 0:
        alerts = volume_alert.check_and_alert(live, names, threshold=args.volume_ratio_alert)
        if alerts:
            print(f"volume alert: {len(alerts)} new -> " +
                  ", ".join(f"{name}({code}) {vr:.1f}x" for code, name, vr in alerts))

    def build_groups(membership):
        result = {}
        for label, codes in membership.items():
            stocks = []
            for code in codes:
                if code in live:
                    stocks.append({"code": code, "name": names.get(code, code),
                                    "close": live[code]["close"], "pct": live[code]["pct"]})
            if not stocks:
                continue
            # median, not mean - a mean is skewed by a single limit-up/limit-down
            # stock into implying the whole group moved when it didn't
            median_pct = round(statistics.median(s["pct"] for s in stocks), 2)
            result[label] = {"n_stocks": len(stocks), "median_pct": median_pct, "stocks": stocks}
        return result

    data = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M") + " 盤中即時更新",
        "industries": build_groups(industries),
        "concepts": build_groups(concepts),
    }
    conn.close()

    with open(args.push_json, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    with open(args.template, encoding="utf-8") as f:
        template = f.read()

    data_json = json.dumps(data, ensure_ascii=False).replace("</script", "<\\/script")
    final = template.replace("__DATA_JSON__", data_json)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(final)

    print(f"built {args.out} and {args.push_json}: "
          f"{len(data['industries'])} industries, {len(data['concepts'])} concepts")


if __name__ == "__main__":
    main()
