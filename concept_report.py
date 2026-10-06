"""Which self-curated 題材/concepts (see concept_map.py) actually rallied or
fell on a given trading day - ranked by average % change, not raw points
(a NT$5 stock moving NT$1 and a NT$500 stock moving NT$1 are very different
moves; STOCK_DAY_ALL only gives point change, so this computes % itself).

Usage:
    python concept_report.py --date 2026-09-24
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

# pct change = Change / (Close - Change) * 100 ; NULLIF guards div-by-zero
PCT_CHANGE = (
    'CAST(NULLIF(sp."Change", \'\') AS REAL) * 100.0 / '
    'NULLIF(CAST(NULLIF(sp."ClosingPrice", \'\') AS REAL) '
    '- CAST(NULLIF(sp."Change", \'\') AS REAL), 0)'
)


def concept_performance(conn, iso_date):
    sql = f"""
        SELECT cm.concept AS concept,
               COUNT(*) AS n_stocks,
               SUM(CASE WHEN {PCT_CHANGE} > 0 THEN 1 ELSE 0 END) AS n_up,
               SUM(CASE WHEN {PCT_CHANGE} < 0 THEN 1 ELSE 0 END) AS n_down,
               ROUND(AVG({PCT_CHANGE}), 2) AS avg_pct_change,
               GROUP_CONCAT(sp.Code || ' ' || sp.Name || ' ' ||
                            ROUND({PCT_CHANGE}, 1) || '%', ', ') AS detail
        FROM concept_map cm
        JOIN stock_price sp ON cm.stock_id = sp.Code AND sp.iso_date = ?
        GROUP BY cm.concept
        HAVING COUNT(*) >= 1
        ORDER BY avg_pct_change DESC
    """
    return conn.execute(sql, (iso_date,)).fetchall()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=utils.today_iso(), help="ISO date, default: today")
    parser.add_argument("--db", default=db.DB_PATH)
    parser.add_argument("--detail", action="store_true", help="also print each stock's % change")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    rows = concept_performance(conn, args.date)

    if not rows:
        print(f"no price data for {args.date} joined against concept_map "
              f"(wrong date, or concept_map empty - check both)")
        return

    print(f"{args.date} 題材漲跌幅排行（依平均漲跌%排序）\n")
    print("concept | n_stocks | n_up | n_down | avg_pct_change")
    for concept, n, n_up, n_down, avg_pct, detail in rows:
        print(f"{concept} | {n} | {n_up} | {n_down} | {avg_pct}")
        if args.detail:
            print(f"    {detail}")

    conn.close()


if __name__ == "__main__":
    main()
