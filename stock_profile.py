"""Per-stock fundamentals profile - everything twstock.db knows about one
company, organized by category, for the dashboard's 個股基本面 tab:
公司基本資料, 現價/估值(本益比), 融資融券歷史(資券比/使用率/估計斷頭距離),
月營收, 損益表/EPS, 資產負債表, 股利, 重要行事曆(股東會/除權息),
歷史價格點位(年度/月度高低點).

margin_call_distance() estimates how far the current price is from a
融資/融券 forced-liquidation trigger, using MARGIN_CALL_DROP_PCT (22%) and
SHORT_CALL_RISE_PCT (46%) - user-supplied rules of thumb, not fetched from
any dataset (real thresholds depend on each stock's own 融資成數/融券保證金
成數) - applied against monthly_trading_stats' latest WeightedAvgPriceAB as
a proxy for "average cost of currently open positions," since margin_trading
has no per-position entry price. TWSE-only, same limit as price_points.

margin_history() returns the FULL daily 融資融券 time series for the stock
(no row-count cap, unlike every other category here) - margin_trading is
the one table in this pipeline that genuinely accumulates real daily
history (one row per day fetch_daily.py has actually run), so it's worth
showing uncapped for the tab's draggable-range trend chart.

Every category here is a plain twstock.db query - no Shioaji dependency,
unlike ma_cache.json - so this covers the WHOLE market (every company_info
company on either market), not just concept_map's curated ~180 stocks.

Two categories are genuinely shallow right now and will only get deeper as
fetch_daily.py keeps running on real calendar days (same pattern already
documented for monthly_revenue in CLAUDE.md - these are snapshot-only
endpoints, not historical-query ones):
  - 月營收/損益表/資產負債表: however many months/quarters have actually
    been fetched so far (could be just 1-2 right now)
  - 歷史價格點位 (annual_trading_stats/monthly_trading_stats): THIS YEAR's
    and THIS MONTH's high/low only (confirmed 2026-11: both endpoints
    return a single current period for every TWSE security, not multi-year
    depth) - still better than stock_price's own thin history today, but
    not a "5-year chart."
  - annual_trading_stats/monthly_trading_stats are TWSE-only (no TPEx
    equivalent found) - TPEx-listed codes get an empty list there, not an
    error.

Usage:
    python stock_profile.py 2330
    python stock_profile.py 2330 --json
"""
import argparse
import json
import sqlite3
import sys

import db
import utils
from datasets import INDUSTRY_SUFFIXES

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

MAX_REVENUE_MONTHS = 12
MAX_FINANCIAL_PERIODS = 4
MAX_DIVIDEND_YEARS = 5
MAX_CALENDAR_ROWS = 6
MAX_ANNUAL_YEARS = 6
MAX_MONTHLY_ROWS = 12

# User-supplied rules of thumb (2026-11 conversation), NOT pulled from any
# TWSE/TPEx dataset - real 融資維持率/融券維持率 thresholds depend on each
# stock's own 融資成數/融券保證金成數, which this pipeline doesn't fetch.
# Treat margin_call_distance()'s output as an estimate, never the exact
# regulatory trigger price.
MARGIN_CALL_DROP_PCT = 0.22   # 融資：股價自基準價下跌約此比例觸發斷頭
SHORT_CALL_RISE_PCT = 0.46    # 融券：股價自基準價上漲約此比例觸發斷頭


def _rows_as_dicts(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def company_info(conn, code):
    # 產業別 in company_info is a numeric code, not human-readable (see
    # CLAUDE.md) - LEFT JOIN industry_map for the real name; some newly
    # listed/edge-case codes have no industry_map row yet, hence LEFT not
    # INNER.
    cur = conn.execute(
        'SELECT ci."公司代號",ci."公司名稱",ci."公司簡稱",im."產業別",ci."上市日期",'
        'ci."董事長",ci."總經理",ci."實收資本額",ci."普通股每股面額",ci."網址" '
        'FROM company_info ci LEFT JOIN industry_map im ON ci."公司代號" = im."公司代號" '
        'WHERE ci."公司代號" = ?',
        (code,),
    )
    rows = _rows_as_dicts(cur)
    return rows[0] if rows else None


def current_price_and_valuation(conn, code):
    # everything in twstock.db is stored as TEXT (see CLAUDE.md) - CAST
    # here so callers (margin_call_distance in particular) get real
    # numbers, not strings that happen to render fine but break on `<=`.
    price_row = conn.execute(
        'SELECT iso_date, CAST(NULLIF("ClosingPrice",\'\') AS REAL), CAST(NULLIF("Change",\'\') AS REAL),'
        ' CAST(NULLIF("OpeningPrice",\'\') AS REAL), CAST(NULLIF("HighestPrice",\'\') AS REAL),'
        ' CAST(NULLIF("LowestPrice",\'\') AS REAL) '
        "FROM stock_price WHERE Code = ? ORDER BY iso_date DESC LIMIT 1",
        (code,),
    ).fetchone()
    val_row = conn.execute(
        'SELECT iso_date, CAST(NULLIF("PEratio",\'\') AS REAL), CAST(NULLIF("PBratio",\'\') AS REAL),'
        ' CAST(NULLIF("DividendYield",\'\') AS REAL) '
        "FROM stock_valuation WHERE Code = ? ORDER BY iso_date DESC LIMIT 1",
        (code,),
    ).fetchone()
    out = {}
    if price_row:
        out.update({
            "price_date": price_row[0], "close": price_row[1], "change": price_row[2],
            "open": price_row[3], "high": price_row[4], "low": price_row[5],
        })
    if val_row:
        out.update({
            "valuation_date": val_row[0], "pe_ratio": val_row[1],
            "pb_ratio": val_row[2], "dividend_yield": val_row[3],
        })
    return out or None


def margin_history(conn, code):
    """Full daily 融資融券 time series for this stock from margin_trading -
    unlike monthly_revenue/income_statement/annual_trading_stats, this table
    genuinely accumulates one real row per day fetch_daily.py has actually
    run (confirmed 2026-11: 19 distinct dates so far, growing daily), so
    this is real history, not a snapshot. Returned oldest-first for
    charting. 融資使用率/資券比 are computed here rather than stored, same
    derived-figures approach as margin_report.py."""
    rows = conn.execute(
        'SELECT iso_date,'
        ' CAST(NULLIF("融資今日餘額",\'\') AS REAL),'
        ' CAST(NULLIF("融資限額",\'\') AS REAL),'
        ' CAST(NULLIF("融券今日餘額",\'\') AS REAL),'
        ' CAST(NULLIF("融券限額",\'\') AS REAL)'
        ' FROM margin_trading WHERE "股票代號" = ? ORDER BY iso_date ASC',
        (code,),
    ).fetchall()
    out = []
    for iso_date, margin_bal, margin_quota, short_bal, short_quota in rows:
        if margin_bal is None:
            continue
        short_bal = short_bal or 0.0
        out.append({
            "date": iso_date,
            "margin_balance_lots": margin_bal,
            "short_balance_lots": short_bal,
            "short_margin_ratio": round(short_bal / margin_bal, 4) if margin_bal else None,
            "margin_usage_pct": round(margin_bal / margin_quota * 100, 2) if margin_quota else None,
            "short_usage_pct": round(short_bal / short_quota * 100, 2) if short_quota else None,
        })
    return out


def monthly_revenue(conn, code, limit=MAX_REVENUE_MONTHS):
    cur = conn.execute(
        'SELECT "資料年月","營業收入-當月營收","營業收入-去年當月營收",'
        '"營業收入-去年同月增減(%)","累計營業收入-當月累計營收",'
        '"累計營業收入-去年累計營收","累計營業收入-前期比較增減(%)" '
        'FROM monthly_revenue WHERE "公司代號" = ? ORDER BY "資料年月" DESC LIMIT ?',
        (code, limit),
    )
    return _rows_as_dicts(cur)


def _find_financial_table(conn, prefix, code):
    """income_statement_*/balance_sheet_* are split by industry segment
    (INDUSTRY_SUFFIXES) - a company's data lives in exactly one. Probe
    each until one has rows for this code."""
    for suffix in INDUSTRY_SUFFIXES:
        table = f"{prefix}_{suffix}"
        n = conn.execute(f'SELECT COUNT(*) FROM {table} WHERE "公司代號" = ?', (code,)).fetchone()[0]
        if n:
            return table
    return None


def income_statement(conn, code, limit=MAX_FINANCIAL_PERIODS):
    table = _find_financial_table(conn, "income_statement", code)
    if not table:
        return []
    cur = conn.execute(
        f'SELECT "年度","季別","營業收入","營業毛利（毛損）淨額","營業利益（損失）",'
        f'"本期淨利（淨損）","基本每股盈餘（元）" FROM {table} '
        f'WHERE "公司代號" = ? ORDER BY "年度" DESC, "季別" DESC LIMIT ?',
        (code, limit),
    )
    return _rows_as_dicts(cur)


def balance_sheet(conn, code, limit=MAX_FINANCIAL_PERIODS):
    table = _find_financial_table(conn, "balance_sheet", code)
    if not table:
        return []
    cur = conn.execute(
        f'SELECT "年度","季別","資產總計","負債總計","權益總計","每股參考淨值" '
        f'FROM {table} WHERE "公司代號" = ? ORDER BY "年度" DESC, "季別" DESC LIMIT ?',
        (code, limit),
    )
    return _rows_as_dicts(cur)


def dividend_history(conn, code, limit=MAX_DIVIDEND_YEARS):
    cur = conn.execute(
        'SELECT "股利年度","股利所屬期間","股東配發-盈餘分配之現金股利(元/股)",'
        '"股東配發-盈餘轉增資配股(元/股)","董事會（擬議）股利分派日","股東會日期" '
        'FROM dividend WHERE "公司代號" = ? ORDER BY "股利年度" DESC LIMIT ?',
        (code, limit),
    )
    return _rows_as_dicts(cur)


def important_calendar(conn, code, today_iso, limit=MAX_CALENDAR_ROWS):
    """股東會 + 除權除息 events merged into one chronological list, each
    tagged upcoming=True/False against today_iso. 股東會日期 isn't reliably
    filled in on the dividend table (often blank in practice) - this uses
    the dedicated shareholder_meetings table instead."""
    events = []
    for kind, rows, label in [
        ("股東會", conn.execute(
            'SELECT "開會日期" AS d, "股東常(臨時)會" AS kind, "開會地點" AS loc, "是否改選董監" AS note '
            'FROM shareholder_meetings WHERE "公司代號" = ? ORDER BY "開會日期" DESC LIMIT ?',
            (code, limit)), None),
        ("除權除息", conn.execute(
            'SELECT iso_date AS d, "Exdividend" AS kind, "CashDividend" AS cash, "StockDividendRatio" AS stock '
            "FROM ex_dividend_schedule WHERE Code = ? ORDER BY iso_date DESC LIMIT ?",
            (code, limit)), None),
    ]:
        for row in _rows_as_dicts(rows):
            iso = utils.roc_to_iso(row["d"]) if kind == "股東會" else row["d"]
            events.append({
                "type": kind, "date": iso, "detail": row,
                "upcoming": bool(iso) and iso >= today_iso,
            })
    events.sort(key=lambda e: e["date"] or "", reverse=True)
    return events[:limit * 2]


def price_points(conn, code, annual_years=MAX_ANNUAL_YEARS, monthly_rows=MAX_MONTHLY_ROWS):
    """TWSE-only (annual_trading_stats/monthly_trading_stats have no TPEx
    equivalent) - returns empty lists for a TPEx code, not an error."""
    annual = _rows_as_dicts(conn.execute(
        'SELECT "Year","HighestPrice","HDate","LowestPrice","LDate","AvgClosingPrice" '
        "FROM annual_trading_stats WHERE Code = ? ORDER BY Year DESC LIMIT ?",
        (code, annual_years),
    ))
    monthly = _rows_as_dicts(conn.execute(
        'SELECT "Month","HighestPrice","LowestPrice","WeightedAvgPriceAB" '
        "FROM monthly_trading_stats WHERE Code = ? ORDER BY Month DESC LIMIT ?",
        (code, monthly_rows),
    ))
    return {"annual": annual, "monthly": monthly}


def margin_call_distance(current_close, basis_price):
    """How far (%) current_close is from an ESTIMATED 融資/融券 forced-
    liquidation price, using basis_price as a stand-in for "average cost
    of currently open positions." TWSE/TPEx's margin_trading balance data
    has no per-position entry price (it's an aggregate 張 balance, not
    trade records), so there's no way to know the TRUE cost basis from
    this pipeline's data - basis_price is meant to be monthly_trading_
    stats' latest WeightedAvgPriceAB (this month's officially-published
    volume-weighted average price), a reasonable proxy since 融資/融券
    positions turn over on the order of weeks, not years. Returns None
    (not a number with a misleading false precision) if either input is
    missing - e.g. a TPEx code with no monthly_trading_stats row, or a
    newly-listed stock with no current price yet."""
    if not current_close or not basis_price or current_close <= 0 or basis_price <= 0:
        return None
    margin_call_price = round(basis_price * (1 - MARGIN_CALL_DROP_PCT), 2)
    short_call_price = round(basis_price * (1 + SHORT_CALL_RISE_PCT), 2)
    return {
        "basis_price": round(basis_price, 2),
        "margin_call_price": margin_call_price,
        # positive = current price is still above the estimated 融資斷頭價
        # (i.e. room left to fall); negative = already past it
        "margin_distance_pct": round((current_close - margin_call_price) / current_close * 100, 2),
        "short_call_price": short_call_price,
        # positive = current price is still below the estimated 融券斷頭價
        # (room left to rise); negative = already past it
        "short_distance_pct": round((short_call_price - current_close) / current_close * 100, 2),
    }


def build_profile(conn, code, today_iso=None):
    today_iso = today_iso or utils.today_iso()
    info = company_info(conn, code)
    if info is None:
        return None
    current = current_price_and_valuation(conn, code)
    pp = price_points(conn, code)
    basis_price = None
    if pp["monthly"]:
        try:
            basis_price = float(pp["monthly"][0]["WeightedAvgPriceAB"])
        except (TypeError, ValueError):
            basis_price = None
    current_close = current.get("close") if current else None
    return {
        "company": info,
        "current": current,
        "margin_history": margin_history(conn, code),
        "margin_call": margin_call_distance(current_close, basis_price),
        "monthly_revenue": monthly_revenue(conn, code),
        "income_statement": income_statement(conn, code),
        "balance_sheet": balance_sheet(conn, code),
        "dividends": dividend_history(conn, code),
        "calendar": important_calendar(conn, code, today_iso),
        "price_points": pp,
    }


def build_all_profiles(conn):
    """{code: profile} for every company in company_info - the dashboard
    tab's search works over this full set client-side."""
    codes = [r[0] for r in conn.execute('SELECT "公司代號" FROM company_info')]
    today_iso = utils.today_iso()
    return {code: build_profile(conn, code, today_iso) for code in codes}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("code", help="stock code, e.g. 2330")
    parser.add_argument("--db", default=db.DB_PATH)
    parser.add_argument("--json", action="store_true", help="dump raw JSON instead of a formatted summary")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    profile = build_profile(conn, args.code)
    conn.close()

    if profile is None:
        print(f"{args.code}: 查無公司基本資料")
        return

    if args.json:
        print(json.dumps(profile, ensure_ascii=False, indent=2))
        return

    c = profile["company"]
    print(f"{c['公司代號']} {c['公司名稱']}（{c['公司簡稱']}）- {c['產業別']}")
    print(f"上市日期：{c['上市日期']}　董事長：{c['董事長']}　總經理：{c['總經理']}")
    cur = profile["current"]
    if cur:
        print(f"\n現價 {cur.get('close')}（{cur.get('price_date')}）"
              f"　本益比 {cur.get('pe_ratio')}　股價淨值比 {cur.get('pb_ratio')}"
              f"　殖利率 {cur.get('dividend_yield')}%")
    mh = profile["margin_history"]
    if mh:
        latest = mh[-1]
        print(f"\n融資融券（{latest['date']}，共 {len(mh)} 個交易日歷史）：")
        print(f"  融資餘額 {latest['margin_balance_lots']:.0f} 張　融券餘額 {latest['short_balance_lots']:.0f} 張　"
              f"資券比 {latest['short_margin_ratio']}　融資使用率 {latest['margin_usage_pct']}%")
    mc = profile["margin_call"]
    if mc:
        print(f"  估計斷頭價（基準價 {mc['basis_price']}，本月加權均價）：")
        print(f"    融資斷頭價 {mc['margin_call_price']}　距離 {mc['margin_distance_pct']:+.2f}%"
              f"{'（已跌破估計斷頭價）' if mc['margin_distance_pct'] < 0 else ''}")
        print(f"    融券斷頭價 {mc['short_call_price']}　距離 {mc['short_distance_pct']:+.2f}%"
              f"{'（已漲破估計斷頭價）' if mc['short_distance_pct'] < 0 else ''}")
    if profile["monthly_revenue"]:
        print(f"\n月營收（最近 {len(profile['monthly_revenue'])} 期）：")
        for r in profile["monthly_revenue"]:
            print(f"  {r['資料年月']}　當月營收 {r['營業收入-當月營收']}　"
                  f"去年同月增減 {r['營業收入-去年同月增減(%)']}%")
    if profile["income_statement"]:
        print(f"\n損益表/EPS（最近 {len(profile['income_statement'])} 期）：")
        for r in profile["income_statement"]:
            print(f"  {r['年度']}Q{r['季別']}　營收 {r['營業收入']}　"
                  f"本期淨利 {r['本期淨利（淨損）']}　EPS {r['基本每股盈餘（元）']}")
    if profile["dividends"]:
        print(f"\n股利（最近 {len(profile['dividends'])} 年度）：")
        for r in profile["dividends"]:
            print(f"  {r['股利年度']}　現金股利 {r['股東配發-盈餘分配之現金股利(元/股)']} 元/股")
    if profile["calendar"]:
        print("\n重要行事曆：")
        for e in profile["calendar"]:
            tag = "未來" if e["upcoming"] else "已過"
            print(f"  [{tag}] {e['date']}　{e['type']}")
    pp = profile["price_points"]
    if pp["annual"]:
        print("\n年度價格區間：")
        for r in pp["annual"]:
            print(f"  {r['Year']}年　最高 {r['HighestPrice']}（{r['HDate']}）　"
                  f"最低 {r['LowestPrice']}（{r['LDate']}）")
    else:
        print("\n年度價格區間：無資料（可能是 TPEx 上櫃股票，此資料集僅 TWSE 上市股票有提供）")


if __name__ == "__main__":
    main()
