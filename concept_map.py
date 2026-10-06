"""Self-curated 題材/概念股 (theme/concept stock) mapping.

There is no free, officially-maintained concept-stock dataset for TWSE
(FinMind has one - TaiwanStockIndustryChain - but it's Sponsor-tier paid).
Instead we build our own, incrementally: every time a news-driven analysis
in the Claude Code conversation confirms a theme -> stock relationship is
real (checked against actual business/revenue exposure, not just guessed),
save it here. It starts empty and only grows as accurate as the analysis
that feeds it - never scraped, never auto-generated.

Usage:
    python concept_map.py add --concept "AI伺服器" --codes 2382 3231 2317 --note "AI伺服器組裝供應鏈"
    python concept_map.py show --concept "AI伺服器"
    python concept_map.py list
    python concept_map.py remove --concept "AI伺服器" --codes 2382
"""
import argparse
import sqlite3
import sys

import db
import utils

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS concept_map (
            concept TEXT,
            stock_id TEXT,
            note TEXT,
            added_date TEXT,
            PRIMARY KEY (concept, stock_id)
        )
        """
    )
    conn.commit()


def valid_codes(conn, codes):
    """Split codes into (valid, invalid) against company_info."""
    known = {r[0] for r in conn.execute('SELECT "公司代號" FROM company_info').fetchall()}
    valid = [c for c in codes if c in known]
    invalid = [c for c in codes if c not in known]
    return valid, invalid


def cmd_add(conn, args):
    ensure_table(conn)
    valid, invalid = valid_codes(conn, args.codes)
    for c in invalid:
        print(f"[skip] {c} not in company_info - not adding (typo / delisted / hallucinated?)")
    if not valid:
        print("nothing added")
        return
    rows = [(args.concept, code, args.note or "", utils.today_iso()) for code in valid]
    conn.executemany(
        "INSERT INTO concept_map (concept, stock_id, note, added_date) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(concept, stock_id) DO UPDATE SET note=excluded.note, added_date=excluded.added_date",
        rows,
    )
    conn.commit()
    print(f"added/updated {len(valid)} stocks under concept '{args.concept}'")


def cmd_remove(conn, args):
    ensure_table(conn)
    for code in args.codes:
        conn.execute("DELETE FROM concept_map WHERE concept = ? AND stock_id = ?", (args.concept, code))
    conn.commit()
    print(f"removed {len(args.codes)} stocks from concept '{args.concept}'")


def cmd_show(conn, args):
    ensure_table(conn)
    rows = conn.execute(
        """
        SELECT cm.stock_id, ci."公司簡稱", cm.note, cm.added_date
        FROM concept_map cm
        LEFT JOIN company_info ci ON cm.stock_id = ci."公司代號"
        WHERE cm.concept = ?
        ORDER BY cm.stock_id
        """,
        (args.concept,),
    ).fetchall()
    if not rows:
        print(f"no stocks under concept '{args.concept}' yet")
        return
    print(f"{args.concept} ({len(rows)} stocks)")
    for code, name, note, added in rows:
        print(f"  {code} {name} - {note} (added {added})")


def cmd_list(conn, args):
    ensure_table(conn)
    rows = conn.execute(
        "SELECT concept, COUNT(*) AS n FROM concept_map GROUP BY concept ORDER BY n DESC"
    ).fetchall()
    if not rows:
        print("no concepts saved yet - use 'add' to start building the list")
        return
    for concept, n in rows:
        print(f"{concept} ({n})")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=db.DB_PATH)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("--concept", required=True)
    p_add.add_argument("--codes", nargs="+", required=True)
    p_add.add_argument("--note", default="")

    p_remove = sub.add_parser("remove")
    p_remove.add_argument("--concept", required=True)
    p_remove.add_argument("--codes", nargs="+", required=True)

    p_show = sub.add_parser("show")
    p_show.add_argument("--concept", required=True)

    sub.add_parser("list")

    args = parser.parse_args()
    conn = sqlite3.connect(args.db)

    {"add": cmd_add, "remove": cmd_remove, "show": cmd_show, "list": cmd_list}[args.cmd](conn, args)

    conn.close()


if __name__ == "__main__":
    main()
