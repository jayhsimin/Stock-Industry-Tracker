"""Pull one day's snapshot of TWSE-listed (上市) fundamentals + chip data
into a local SQLite database (twstock.db).

Usage:
    python fetch_daily.py                 # today
    python fetch_daily.py --date 2026-09-24
"""
import argparse
import sys

import db
import twse_client
import utils
from datasets import ALL_DATASETS


def fetch_dataset(conn, ds: dict, run_date: str) -> int:
    records = twse_client.fetch_json(ds["url"])

    if ds.get("remap"):
        mapping, mode = ds["remap"]
        remap_fn = twse_client.remap_full if mode == "full" else twse_client.remap_partial
        records = [remap_fn(r, mapping) for r in records]

    if ds["name"] == "institutional_investors_tpex":
        # TPEx doesn't split dealer buy/sell into self-trading vs hedging
        # like TWSE does; fill the overall dealer-net column (which TWSE
        # rows do have) from the self-trading figure as the closest proxy.
        for r in records:
            r.setdefault("自營商買賣超股數", r.get("自營商買賣超股數(自行買賣)", ""))

    if ds.get("date_field"):
        for r in records:
            r["iso_date"] = utils.roc_to_iso(r[ds["date_field"]])
    elif "iso_date" in ds["key_cols"]:
        for r in records:
            r["iso_date"] = run_date

    return db.upsert_records(conn, ds["table"], records, ds["key_cols"])


def fetch_institutional_investors(conn, run_date: str) -> int:
    records = twse_client.fetch_institutional_investors(run_date)
    for r in records:
        r["iso_date"] = run_date
    return db.upsert_records(conn, "institutional_investors", records, ["證券代號", "iso_date"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=utils.today_iso(), help="ISO date, default: today")
    parser.add_argument("--db", default=db.DB_PATH, help="SQLite file path")
    args = parser.parse_args()

    conn = db.get_conn(args.db)
    total_ok, total_fail = 0, 0

    for ds in ALL_DATASETS:
        try:
            n = fetch_dataset(conn, ds, args.date)
            print(f"[ok]   {ds['name']:<24} {n} rows -> {ds['table']}")
            total_ok += 1
        except Exception as exc:
            print(f"[fail] {ds['name']:<24} {exc}", file=sys.stderr)
            total_fail += 1

    try:
        n = fetch_institutional_investors(conn, args.date)
        print(f"[ok]   {'institutional_investors':<24} {n} rows -> institutional_investors")
        total_ok += 1
    except Exception as exc:
        print(f"[fail] {'institutional_investors':<24} {exc}", file=sys.stderr)
        total_fail += 1

    db.ensure_views(conn)
    conn.close()
    print(f"\nDone. {total_ok} datasets ok, {total_fail} failed. DB: {args.db}")
    if total_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
