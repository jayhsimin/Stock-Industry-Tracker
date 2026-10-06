"""Export industry + concept performance data from twstock.db and splice it
into dashboard.html.template to produce dashboard.html - a bubble-chart view
of which industry/concept is moving and by how much, with per-stock drill-down.

The output is a static snapshot: re-run this (and re-publish the artifact)
to refresh it with newer data. It does not read twstock.db live in the browser
- artifacts can't reach a local SQLite file.

Usage:
    python build_dashboard.py --date 2026-09-29
    python build_dashboard.py              # latest date available in stock_price
"""
import argparse
import json
import sqlite3
import statistics

import db

PCT = (
    'CAST(NULLIF(sp."Change", \'\') AS REAL) * 100.0 / '
    'NULLIF(CAST(NULLIF(sp."ClosingPrice", \'\') AS REAL) '
    '- CAST(NULLIF(sp."Change", \'\') AS REAL), 0)'
)

STOCKS_SQL_TEMPLATE = """
    SELECT sp.Code, sp.Name,
           CAST(NULLIF(sp."ClosingPrice", '') AS REAL) AS close,
           ROUND({pct}, 2) AS pct_change
    FROM stock_price sp
    INNER JOIN company_info ci ON sp.Code = ci."公司代號"
    {extra_join}
    WHERE sp.iso_date = ? AND {pct} IS NOT NULL AND {where}
"""


def fetch_groups(conn, iso_date, extra_join, where, param):
    sql = STOCKS_SQL_TEMPLATE.format(pct=PCT, extra_join=extra_join, where=where)
    return conn.execute(sql, (iso_date, param)).fetchall()


def build_data(conn, iso_date):
    industries = {}
    for (industry,) in conn.execute('SELECT DISTINCT "產業別" FROM industry_map WHERE "產業別" IS NOT NULL'):
        rows = fetch_groups(
            conn, iso_date,
            'INNER JOIN industry_map im ON sp.Code = im."公司代號"',
            'im."產業別" = ?', industry,
        )
        if not rows:
            continue
        pct_vals = [r[3] for r in rows]
        industries[industry] = {
            "n_stocks": len(rows),
            # median, not mean - a mean is skewed by a single limit-up/limit-down
            # stock into implying the whole group moved when it didn't
            "median_pct": round(statistics.median(pct_vals), 2),
            "stocks": [{"code": r[0], "name": r[1], "close": r[2], "pct": r[3]} for r in rows],
        }

    concepts = {}
    for (concept,) in conn.execute("SELECT DISTINCT concept FROM concept_map"):
        rows = fetch_groups(
            conn, iso_date,
            'INNER JOIN concept_map cm ON sp.Code = cm.stock_id',
            "cm.concept = ?", concept,
        )
        if not rows:
            continue
        pct_vals = [r[3] for r in rows]
        concepts[concept] = {
            "n_stocks": len(rows),
            "median_pct": round(statistics.median(pct_vals), 2),
            "stocks": [{"code": r[0], "name": r[1], "close": r[2], "pct": r[3]} for r in rows],
        }

    return {"date": iso_date, "industries": industries, "concepts": concepts}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=None, help="ISO date, default: latest available in stock_price")
    parser.add_argument("--db", default=db.DB_PATH)
    parser.add_argument("--template", default="dashboard.html.template")
    parser.add_argument("--out", default="dashboard.html")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    iso_date = args.date or conn.execute(
        "SELECT MAX(iso_date) FROM stock_price"
    ).fetchone()[0]

    data = build_data(conn, iso_date)
    conn.close()

    with open(args.template, encoding="utf-8") as f:
        template = f.read()

    data_json = json.dumps(data, ensure_ascii=False).replace("</script", "<\\/script")
    final = template.replace("__DATA_JSON__", data_json)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(final)

    n_i, n_c = len(data["industries"]), len(data["concepts"])
    print(f"built {args.out} for {iso_date}: {n_i} industries, {n_c} concepts")


if __name__ == "__main__":
    main()
