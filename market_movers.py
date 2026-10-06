"""Find the biggest movers across the ENTIRE market (all TWSE+TPEx stocks in
twstock.db, not just ones already in concept_map), then classify them by
official industry + self-curated concept to see which industry/theme is
actually driving the day's action - and, given enough accumulated daily
history, whether a theme is heating up (發酵) or fading (資金衰退) over time.

(Shioaji's ChangePercentRank scanner was tried first but its results skewed
toward thin-volume stocks clustered at exactly +-10%, not a clean ranked
list - our own EOD data is cleaner and already has the industry/concept
joins built, so this uses twstock.db instead.)

Usage:
    python market_movers.py --date 2026-09-29 --top 30
    python market_movers.py --date 2026-09-29 --top 30 --min-volume 1000000
    python market_movers.py --days 5 --top 30      # industry trend across the last N available dates
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

PCT_CHANGE = (
    'CAST(NULLIF(sp."Change", \'\') AS REAL) * 100.0 / '
    'NULLIF(CAST(NULLIF(sp."ClosingPrice", \'\') AS REAL) '
    '- CAST(NULLIF(sp."Change", \'\') AS REAL), 0)'
)

MOVERS_SQL = f"""
    SELECT sp.Code, sp.Name,
           im."產業別" AS industry,
           CAST(NULLIF(sp."ClosingPrice", '') AS REAL) AS close,
           ROUND({PCT_CHANGE}, 2) AS pct_change,
           CAST(NULLIF(sp."TradeVolume", '') AS INTEGER) AS volume,
           (SELECT GROUP_CONCAT(concept, ',') FROM concept_map cm WHERE cm.stock_id = sp.Code) AS concepts
    FROM stock_price sp
    INNER JOIN company_info ci ON sp.Code = ci."公司代號"
    LEFT JOIN industry_map im ON sp.Code = im."公司代號"
    WHERE sp.iso_date = ?
      AND CAST(NULLIF(sp."TradeVolume", '') AS INTEGER) >= ?
      AND {PCT_CHANGE} IS NOT NULL
    ORDER BY ABS({PCT_CHANGE}) DESC
    LIMIT ?
"""


def top_movers(conn, iso_date, top_n, min_volume):
    return conn.execute(MOVERS_SQL, (iso_date, min_volume, top_n)).fetchall()


def industry_representation(movers):
    counts = {}
    for row in movers:
        industry = row[2] or "(未分類)"
        counts[industry] = counts.get(industry, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)


def available_dates(conn, limit):
    rows = conn.execute(
        "SELECT DISTINCT iso_date FROM stock_price ORDER BY iso_date DESC LIMIT ?", (limit,)
    ).fetchall()
    return [r[0] for r in rows][::-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=utils.today_iso(), help="ISO date, default: today")
    parser.add_argument("--top", type=int, default=30, help="how many top movers to show")
    parser.add_argument("--min-volume", type=int, default=100_000,
                         help="exclude thin-volume stocks below this share count (default 100,000)")
    parser.add_argument("--days", type=int, default=0,
                         help="instead of one date, show industry representation trend across the last N available dates")
    parser.add_argument("--db", default=db.DB_PATH)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)

    if args.days:
        dates = available_dates(conn, args.days)
        if not dates:
            print("no data in stock_price yet")
            return
        print(f"產業在每日漲跌幅前{args.top}名的出現次數（依日期，只能看到目前累積到的 {len(dates)} 天）\n")
        per_date = {}
        all_industries = set()
        for d in dates:
            movers = top_movers(conn, d, args.top, args.min_volume)
            rep = dict(industry_representation(movers))
            per_date[d] = rep
            all_industries.update(rep.keys())

        header = "產業".ljust(14) + "".join(d[5:].rjust(8) for d in dates)
        print(header)
        for industry in sorted(all_industries, key=lambda i: -sum(per_date[d].get(i, 0) for d in dates)):
            row = industry.ljust(14) + "".join(str(per_date[d].get(industry, 0)).rjust(8) for d in dates)
            print(row)

        if len(dates) < 3:
            print("\n(目前資料庫只累積了這幾天，趨勢還看不出什麼——每天跑 fetch_daily.py 幾天後再回來看會更有意義)")
        conn.close()
        return

    movers = top_movers(conn, args.date, args.top, args.min_volume)
    if not movers:
        print(f"no data for {args.date} (or everything got filtered by --min-volume)")
        return

    print(f"{args.date} 漲跌幅前{len(movers)}名（成交量 >= {args.min_volume:,} 股）\n")
    print("code | name | industry | close | pct_change | volume | concepts")
    for code, name, industry, close, pct, volume, concepts in movers:
        print(f"{code} | {name} | {industry or '(未分類)'} | {close} | {pct}% | {volume} | {concepts or ''}")

    print(f"\n== 產業在前{len(movers)}名裡的出現次數 ==")
    for industry, count in industry_representation(movers):
        print(f"{industry}: {count}")

    conn.close()


if __name__ == "__main__":
    main()
