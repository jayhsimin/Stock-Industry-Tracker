"""Ready-made per-industry summaries built on top of twstock.db.

Usage:
    python industry_report.py                    # today
    python industry_report.py --date 2026-09-24
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

# NULLIF(col,'') before CAST: several source columns come back as '' when
# TWSE has no value for that stock/day, and CAST('' AS REAL) would silently
# become 0 and skew averages/sums.
NUM = lambda col: f'CAST(NULLIF("{col}", \'\') AS REAL)'  # noqa: E731


def price_performance(conn, iso_date):
    sql = f"""
        SELECT im."產業別" AS industry,
               COUNT(*) AS n_stocks,
               SUM(CASE WHEN {NUM('Change')} > 0 THEN 1 ELSE 0 END) AS n_up,
               SUM(CASE WHEN {NUM('Change')} < 0 THEN 1 ELSE 0 END) AS n_down,
               ROUND(AVG({NUM('Change')}), 2) AS avg_change,
               SUM({NUM('TradeValue')}) AS total_trade_value
        FROM stock_price sp
        LEFT JOIN industry_map im ON sp.Code = im."公司代號"
        WHERE sp.iso_date = ? AND im."產業別" IS NOT NULL
        GROUP BY im."產業別"
        ORDER BY total_trade_value DESC
    """
    return conn.execute(sql, (iso_date,)).fetchall()


def institutional_flows(conn, iso_date):
    sql = f"""
        SELECT im."產業別" AS industry,
               COUNT(*) AS n_stocks,
               SUM({NUM('三大法人買賣超股數')}) AS total_net,
               SUM({NUM('外陸資買賣超股數(不含外資自營商)')}) AS foreign_net,
               SUM({NUM('投信買賣超股數')}) AS trust_net,
               SUM({NUM('自營商買賣超股數')}) AS dealer_net
        FROM institutional_investors ii
        LEFT JOIN industry_map im ON ii."證券代號" = im."公司代號"
        WHERE ii.iso_date = ? AND im."產業別" IS NOT NULL
        GROUP BY im."產業別"
        ORDER BY total_net DESC
    """
    return conn.execute(sql, (iso_date,)).fetchall()


def valuation_summary(conn, iso_date):
    sql = f"""
        SELECT im."產業別" AS industry,
               COUNT(*) AS n_stocks,
               ROUND(AVG({NUM('PEratio')}), 2) AS avg_per,
               ROUND(AVG({NUM('DividendYield')}), 2) AS avg_yield,
               ROUND(AVG({NUM('PBratio')}), 2) AS avg_pbr
        FROM stock_valuation sv
        LEFT JOIN industry_map im ON sv.Code = im."公司代號"
        WHERE sv.iso_date = ? AND im."產業別" IS NOT NULL
        GROUP BY im."產業別"
        ORDER BY avg_per
    """
    return conn.execute(sql, (iso_date,)).fetchall()


def margin_change(conn, iso_date):
    sql = f"""
        SELECT im."產業別" AS industry,
               COUNT(*) AS n_stocks,
               SUM({NUM('融資今日餘額')} - {NUM('融資前日餘額')}) AS margin_balance_change
        FROM margin_trading mt
        LEFT JOIN industry_map im ON mt."股票代號" = im."公司代號"
        WHERE mt.iso_date = ? AND im."產業別" IS NOT NULL
        GROUP BY im."產業別"
        ORDER BY margin_balance_change DESC
    """
    return conn.execute(sql, (iso_date,)).fetchall()


def print_table(title, rows, headers):
    print(f"\n== {title} ==")
    if not rows:
        print("(no data for this date)")
        return
    print(" | ".join(headers))
    for row in rows:
        print(" | ".join("" if v is None else str(v) for v in row))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=utils.today_iso(), help="ISO date, default: today")
    parser.add_argument("--db", default=db.DB_PATH)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)

    print_table(
        f"{args.date} 產業當日漲跌 / 成交金額（依成交金額排序）",
        price_performance(conn, args.date),
        ["產業", "檔數", "上漲", "下跌", "平均漲跌", "總成交金額"],
    )
    print_table(
        f"{args.date} 產業三大法人買賣超（依合計買賣超排序）",
        institutional_flows(conn, args.date),
        ["產業", "檔數", "三大法人合計", "外資", "投信", "自營商"],
    )
    print_table(
        f"{args.date} 產業估值（PER 由低到高）",
        valuation_summary(conn, args.date),
        ["產業", "檔數", "平均PER", "平均殖利率%", "平均PBR"],
    )
    print_table(
        f"{args.date} 產業融資餘額變化（依增加金額排序）",
        margin_change(conn, args.date),
        ["產業", "檔數", "融資餘額變化(股)"],
    )

    conn.close()


if __name__ == "__main__":
    main()
