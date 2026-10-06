"""Validate & enrich a candidate stock list against twstock.db.

Intended use: after reading a news article and guessing which stocks a
theme might affect (by code or by company name), run this to (a) confirm
the guess is a real, currently-listed stock and (b) pull today's momentum
(price change, institutional flows, margin change, valuation) as evidence
the theme is actually showing up in the market.

Usage:
    python stock_lookup.py --codes 2603 2609 2615
    python stock_lookup.py --names "長榮" "陽明" "萬海"
    python stock_lookup.py --industry "半導體業" "金融保險業"
    python stock_lookup.py --concept "AI伺服器"          # see concept_map.py to build these
    python stock_lookup.py --codes 2603 2609 --date 2026-09-24
    python stock_lookup.py --list-industries
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

LOOKUP_SQL = """
    SELECT
        ci."公司代號" AS code,
        ci."公司簡稱" AS name,
        im."產業別" AS industry,
        sp."ClosingPrice" AS close,
        sp."Change" AS change,
        sv."PEratio" AS per,
        sv."DividendYield" AS yield,
        ii."三大法人買賣超股數" AS inst_net,
        ii."外陸資買賣超股數(不含外資自營商)" AS foreign_net,
        ii."投信買賣超股數" AS trust_net,
        ii."自營商買賣超股數" AS dealer_net,
        CAST(NULLIF(mt."融資今日餘額", '') AS REAL)
            - CAST(NULLIF(mt."融資前日餘額", '') AS REAL) AS margin_change
    FROM company_info ci
    LEFT JOIN industry_map im ON ci."公司代號" = im."公司代號"
    LEFT JOIN stock_price sp ON ci."公司代號" = sp.Code AND sp.iso_date = ?
    LEFT JOIN stock_valuation sv ON ci."公司代號" = sv.Code AND sv.iso_date = ?
    LEFT JOIN institutional_investors ii
        ON ci."公司代號" = ii."證券代號" AND ii.iso_date = ?
    LEFT JOIN margin_trading mt ON ci."公司代號" = mt."股票代號" AND mt.iso_date = ?
    WHERE ci."公司代號" = ?
"""

def resolve_names(conn, names):
    """Return (codes_found, names_not_found, weak_matches).

    Matches on 公司簡稱 (the market-recognized short name) first - this is
    high confidence. Only falls back to substring-matching the full legal
    name (公司名稱) when no short-name match exists, and flags those as
    weak: two unrelated companies can share characters in their full legal
    name (e.g. "李長榮科技" contains "長榮" but has nothing to do with the
    Evergreen/長榮 shipping group), so weak matches need a human sanity check.
    """
    all_rows = conn.execute('SELECT "公司代號", "公司簡稱", "公司名稱" FROM company_info').fetchall()
    codes, missing, weak = [], [], []
    for name in names:
        short_matches = [r[0] for r in all_rows if name in (r[1] or "")]
        if short_matches:
            codes.extend(short_matches)
            continue
        long_matches = [(r[0], r[1]) for r in all_rows if name in (r[2] or "")]
        if long_matches:
            codes.extend(c for c, _ in long_matches)
            weak.extend((name, c, n) for c, n in long_matches)
        else:
            missing.append(name)
    return codes, missing, weak


def codes_by_industry(conn, industries):
    """Return (codes_found, industries_not_found). Exact match against
    industry_map's 產業別 - use --list-industries to see the valid values."""
    codes, missing = [], []
    for industry in industries:
        rows = conn.execute(
            'SELECT "公司代號" FROM industry_map WHERE "產業別" = ?', (industry,)
        ).fetchall()
        if rows:
            codes.extend(r[0] for r in rows)
        else:
            missing.append(industry)
    return codes, missing


def codes_by_concept(conn, concepts):
    """Return (codes_found, concepts_not_found) from our self-curated
    concept_map table (see concept_map.py)."""
    codes, missing = [], []
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='concept_map'"
    ).fetchone()
    for concept in concepts:
        rows = [] if not has_table else conn.execute(
            "SELECT stock_id FROM concept_map WHERE concept = ?", (concept,)
        ).fetchall()
        if rows:
            codes.extend(r[0] for r in rows)
        else:
            missing.append(concept)
    return codes, missing


def list_industries(conn):
    rows = conn.execute(
        'SELECT "產業別", COUNT(*) AS n FROM industry_map GROUP BY "產業別" ORDER BY n DESC'
    ).fetchall()
    for industry, n in rows:
        print(f"{industry} ({n})")


def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def lookup(conn, codes, iso_date):
    found, not_found = [], []
    for code in codes:
        row = conn.execute(LOOKUP_SQL, (iso_date, iso_date, iso_date, iso_date, code)).fetchone()
        if row is None:
            not_found.append(code)
        else:
            found.append(row)
    return found, not_found


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--codes", nargs="*", default=[], help="4-digit stock codes")
    parser.add_argument("--names", nargs="*", default=[], help="company names (partial match ok)")
    parser.add_argument("--industry", nargs="*", default=[], help="產業別 exact name(s), see --list-industries")
    parser.add_argument("--concept", nargs="*", default=[], help="self-curated concept name(s), see concept_map.py")
    parser.add_argument("--list-industries", action="store_true", help="print valid 產業別 values and exit")
    parser.add_argument("--date", default=utils.today_iso(), help="ISO date, default: today")
    parser.add_argument("--db", default=db.DB_PATH)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)

    if args.list_industries:
        list_industries(conn)
        return

    # code -> set of "why it's in this query" labels. A stock can legitimately
    # match more than one industry/concept/name (e.g. 力積電 is both 記憶體
    # and 晶圓代工) - track sources instead of a flat list so it's shown once,
    # not once per matching source.
    sources: dict[str, set] = {}

    for code in args.codes:
        sources.setdefault(code, set()).add(f"code:{code}")

    if args.names:
        resolved, missing_names, weak = resolve_names(conn, args.names)
        for code in resolved:
            sources.setdefault(code, set()).add("name")
        for m in missing_names:
            print(f"[not found] name matched no listed company: {m}")
        for query, code, full_name in weak:
            print(f"[weak match] '{query}' only matched full legal name of {code} {full_name} "
                  f"- verify it's actually related before trusting this")

    if args.industry:
        for industry in args.industry:
            resolved, missing = codes_by_industry(conn, [industry])
            for code in resolved:
                sources.setdefault(code, set()).add(f"產業:{industry}")
            for m in missing:
                print(f"[not found] no such 產業別 (see --list-industries): {m}")

    if args.concept:
        for concept in args.concept:
            resolved, missing = codes_by_concept(conn, [concept])
            for code in resolved:
                sources.setdefault(code, set()).add(f"題材:{concept}")
            for m in missing:
                print(f"[not found] no such concept yet (see concept_map.py add): {m}")

    if not sources:
        print("no candidate codes given (use --codes / --names / --industry / --concept)")
        return

    found, not_found = lookup(conn, list(sources.keys()), args.date)
    found.sort(key=lambda row: abs(_safe_float(row[7])), reverse=True)

    for code in not_found:
        print(f"[not found] code not in company_info (delisted / typo / hallucinated?): {code}")

    if found:
        headers = ["code", "name", "industry", "close", "change", "PER", "yield%",
                   "inst_net", "foreign_net", "trust_net", "dealer_net", "margin_change", "matched_via"]
        print(f"\n{len(found)} stocks, sorted by |三大法人買賣超| descending:")
        print(" | ".join(headers))
        for row in found:
            matched_via = ",".join(sorted(sources.get(row[0], set())))
            print(" | ".join("" if v is None else str(v) for v in row) + " | " + matched_via)

    conn.close()


if __name__ == "__main__":
    main()
