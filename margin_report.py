"""Market-wide leverage + bull/bear positioning from twstock.db's
margin_trading table (TWSE's MI_MARGN + TPEx's tpex_mainboard_margin_balance,
merged - see datasets.py/CLAUDE.md).

This is EOD-only data (TWSE/TPEx publish 融資融券 balances once after close;
Shioaji has no live feed for it), so build_dashboard.py and
build_dashboard_live.py both call build_margin_data() the same way against
twstock.db - unlike ma_table/live price, there is no "live" variant here.

Every margin_trading row already carries both 今日餘額 (today's balance) and
前日餘額 (previous day's balance), so day-over-day change needs no extra
date lookup or self-join - it's arithmetic on a single row. Balances are in
張 (board lots of 1000 shares); NT$ amounts are derived by multiplying by
1000 and by that day's closing price from stock_price.

Usage:
    python margin_report.py --date 2026-10-27
    python margin_report.py --date 2026-10-27 --top 10
"""
import argparse
import sqlite3
import statistics
import sys

import db
import utils

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def _num(col, alias="mt"):
    return f'CAST(NULLIF({alias}."{col}", \'\') AS REAL)'


MARGIN_SQL = f"""
    SELECT mt."股票代號", mt."股票名稱",
           {_num('融資今日餘額')}, {_num('融資前日餘額')}, {_num('融資限額')},
           {_num('融券今日餘額')}, {_num('融券前日餘額')}, {_num('融券限額')},
           CAST(NULLIF(sp."ClosingPrice", '') AS REAL)
    FROM margin_trading mt
    INNER JOIN company_info ci ON mt."股票代號" = ci."公司代號"
    LEFT JOIN stock_price sp ON mt."股票代號" = sp.Code AND sp.iso_date = mt.iso_date
    WHERE mt.iso_date = ?
"""


def fetch_rows(conn, iso_date):
    """Raw per-stock margin_trading rows for iso_date, joined to that same
    day's closing price - skips any row missing either (no 今日餘額 means
    this stock wasn't margin-eligible that day; no close means stock_price
    hasn't been fetched for this date yet). INNER JOIN company_info excludes
    ETFs/bonds (e.g. 00679B) the same way market_movers.py does - they're
    margin-eligible too but aren't companies, and would otherwise dominate
    the "biggest lot change" rankings the way warrants dominated %change."""
    rows = []
    for code, name, bal, prev_bal, quota, short_bal, short_prev, short_quota, close in \
            conn.execute(MARGIN_SQL, (iso_date,)):
        if bal is None or close is None:
            continue
        rows.append({
            "code": code, "name": name, "close": close,
            "margin_balance_lots": bal, "margin_prev_lots": prev_bal or 0.0,
            "margin_quota_lots": quota,
            "short_balance_lots": short_bal or 0.0, "short_prev_lots": short_prev or 0.0,
            "short_quota_lots": short_quota,
        })
    return rows


def enrich(row):
    """Adds every derived figure (NT$ amounts, day-over-day change, 資券比,
    quota usage%) to a raw fetch_rows() row."""
    r = dict(row)
    r["margin_balance_ntd"] = r["margin_balance_lots"] * 1000 * r["close"]
    r["margin_change_lots"] = r["margin_balance_lots"] - r["margin_prev_lots"]
    r["short_balance_ntd"] = r["short_balance_lots"] * 1000 * r["close"]
    r["short_change_lots"] = r["short_balance_lots"] - r["short_prev_lots"]
    r["short_margin_ratio"] = (
        r["short_balance_lots"] / r["margin_balance_lots"] if r["margin_balance_lots"] else None
    )
    r["margin_usage_pct"] = (
        r["margin_balance_lots"] / r["margin_quota_lots"] * 100
        if r["margin_quota_lots"] else None
    )
    r["short_usage_pct"] = (
        r["short_balance_lots"] / r["short_quota_lots"] * 100
        if r["short_quota_lots"] else None
    )
    return r


def _top(rows, key, n, reverse=True, require_min_balance_lots=0):
    """Top-n rows by key (None values excluded), as plain dicts for JSON -
    require_min_balance_lots filters out barely-margin-eligible penny
    positions from ratio-based rankings (a 2-lot vs 1-lot stock can show a
    2.0 資券比 that's just noise, not a real bull/bear signal)."""
    candidates = [r for r in rows if r.get(key) is not None
                  and r["margin_balance_lots"] >= require_min_balance_lots]
    candidates.sort(key=lambda r: r[key], reverse=reverse)
    out = []
    for r in candidates[:n]:
        out.append({
            "code": r["code"], "name": r["name"], "close": round(r["close"], 2),
            "margin_balance_lots": round(r["margin_balance_lots"]),
            "short_balance_lots": round(r["short_balance_lots"]),
            "margin_change_lots": round(r["margin_change_lots"]),
            "short_change_lots": round(r["short_change_lots"]),
            "short_margin_ratio": round(r["short_margin_ratio"], 4) if r["short_margin_ratio"] is not None else None,
            "margin_usage_pct": round(r["margin_usage_pct"], 1) if r["margin_usage_pct"] is not None else None,
        })
    return out


def group_membership(conn):
    industries = {}
    for code, industry in conn.execute('SELECT "公司代號", "產業別" FROM industry_map WHERE "產業別" IS NOT NULL'):
        industries.setdefault(industry, []).append(code)
    concepts = {}
    for code, concept in conn.execute("SELECT stock_id, concept FROM concept_map"):
        concepts.setdefault(concept, []).append(code)
    return industries, concepts


def aggregate_groups(rows, membership):
    """Per concept/industry: 資券比 median (bull/bear tilt of the group) +
    summed 融資/融券 lot change (which theme's leverage is building up)."""
    by_code = {r["code"]: r for r in rows}
    result = {}
    for label, codes in membership.items():
        members = [by_code[c] for c in codes if c in by_code]
        if not members:
            continue
        ratios = [r["short_margin_ratio"] for r in members if r["short_margin_ratio"] is not None]
        result[label] = {
            "n_stocks": len(members),
            "median_short_margin_ratio": round(statistics.median(ratios), 4) if ratios else None,
            "margin_change_lots": round(sum(r["margin_change_lots"] for r in members)),
            "short_change_lots": round(sum(r["short_change_lots"] for r in members)),
        }
    return result


def pick_latest_usable_date(conn, max_lookback=10):
    """margin_trading's own MAX(iso_date) can have ZERO rows joinable
    against stock_price even though both tables have plenty of rows for
    that date - confirmed 2026-11: a flaky TPEx stock_price fetch that day
    left stock_price with TWSE-only rows for one date while margin_trading
    had TPEx-only rows for it (TWSE's legacy MI_MARGN has no embedded date
    and gets stamped with the run date, while TPEx's margin endpoint
    carries its own embedded date - the two markets can end up dated a day
    apart after a partial/failed run), so TWSE-code stock_price rows never
    match TPEx-code margin_trading rows for that date and the join is
    empty. Walks margin_trading's distinct dates newest-first and returns
    the first one that actually has >0 joinable rows, instead of blindly
    trusting MAX(iso_date) and silently returning no data for the whole
    融資融券 tab."""
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT iso_date FROM margin_trading ORDER BY iso_date DESC LIMIT ?", (max_lookback,)
    )]
    for d in dates:
        n = conn.execute(
            'SELECT COUNT(*) FROM margin_trading mt '
            'INNER JOIN stock_price sp ON mt."股票代號" = sp.Code AND sp.iso_date = mt.iso_date '
            "WHERE mt.iso_date = ?", (d,),
        ).fetchone()[0]
        if n > 0:
            return d
    return dates[0] if dates else None


MARKET_HISTORY_SQL = """
    SELECT mt.iso_date,
           SUM({margin}) AS total_margin_lots,
           SUM({short}) AS total_short_lots
    FROM margin_trading mt
    INNER JOIN company_info ci ON mt."股票代號" = ci."公司代號"
    GROUP BY mt.iso_date
    ORDER BY mt.iso_date ASC
""".format(margin=_num('融資今日餘額'), short=f"COALESCE({_num('融券今日餘額')}, 0)")


MARKET_HISTORY_PER_STOCK_SQL = """
    SELECT mt."股票代號", {margin}, {short}
    FROM margin_trading mt
    INNER JOIN company_info ci ON mt."股票代號" = ci."公司代號"
    WHERE mt.iso_date = ?
""".format(margin=_num('融資今日餘額'), short=f"COALESCE({_num('融券今日餘額')}, 0)")


def _stock_call_distances(conn):
    """{code: stock_profile.margin_call_distance(...) result} computed from
    EACH STOCK'S OWN latest close price (stock_price) and its
    monthly_trading_stats basis - the fixed "today's risk" half of the
    market-wide 斷頭風險 series below. Imports stock_profile rather than
    reimplementing the same formula a second time.

    Deliberately does NOT filter to one shared as_of_date (confirmed
    2026-11: a single stock_price date can be entirely TWSE-origin or
    entirely TPEx-origin depending on which market's fetch happened to run
    that day - same root cause as pick_latest_usable_date's docstring - so
    picking one date here, e.g. pick_latest_usable_date()'s choice, zeroed
    out EVERY result because that particular date's stock_price rows were
    all TPEx while monthly_trading_stats is TWSE-only). Per-stock MAX
    (iso_date) is the same approach stock_profile.current_price_and_
    valuation() already uses and sidesteps the whole-market date-alignment
    problem entirely."""
    import stock_profile

    rows = conn.execute(
        'SELECT sp.Code, CAST(NULLIF(sp."ClosingPrice",\'\') AS REAL), '
        '       CAST(NULLIF(mts."WeightedAvgPriceAB",\'\') AS REAL) '
        'FROM stock_price sp '
        'INNER JOIN company_info ci ON sp.Code = ci."公司代號" '
        'INNER JOIN (SELECT Code, MAX(iso_date) AS max_date FROM stock_price GROUP BY Code) latest '
        '    ON sp.Code = latest.Code AND sp.iso_date = latest.max_date '
        'LEFT JOIN monthly_trading_stats mts ON sp.Code = mts.Code'
    ).fetchall()
    out = {}
    for code, close, basis in rows:
        dist = stock_profile.margin_call_distance(close, basis)
        if dist:
            out[code] = dist
    return out


def build_market_history(conn, max_dates=120):
    """Market-wide 融資/融券 totals (in 張, no NT$) for EVERY margin_trading
    date, not just ones with a usable stock_price join, PLUS a lot-weighted
    average 斷頭風險 (distance-to-margin-call) series - powers the 全市場
    融資融券概況 card's historical trend chart.

    The 張-count totals deliberately do NOT join stock_price the way
    build_margin_data/fetch_rows does: margin_trading genuinely supports
    historical --date backfills (see CLAUDE.md), but STOCK_DAY_ALL/
    BWIBBU_ALL do NOT - a `fetch_daily.py --date <past>` run fills in
    margin_trading for that date while leaving stock_price at 0 rows for it
    (confirmed 2026-11: 13 of 19 margin_trading dates have zero matching
    stock_price rows). Requiring that join would reduce this chart to ~4
    points instead of the ~19 margin_trading actually has.

    avg_margin_distance_pct/avg_short_distance_pct are a deliberate HYBRID,
    not a true historical backtest: each stock's own distance-to-call
    (stock_profile.margin_call_distance) is computed ONCE from TODAY's
    close price and this month's weighted-average basis (twstock.db has no
    usable historical close price for most dates, and monthly_trading_stats
    is itself a single-period snapshot, not a per-date series - there is no
    "what was this stock's distance to call on 2026-10-01" to compute).
    What DOES vary by historical date is which stocks were carrying how
    much margin/short balance (margin_trading's backfill is real), so each
    date's point is TODAY's fixed per-stock distances re-weighted by THAT
    DATE's actual 張 balance mix - i.e. "given today's prices, how would
    the market's aggregate margin-call exposure have looked as balance
    composition shifted across stocks over time," not "what the real risk
    was back then." Label this in the UI; don't present it as a true
    backtest."""
    rows = conn.execute(MARKET_HISTORY_SQL).fetchall()
    distances = _stock_call_distances(conn)

    out = []
    for iso_date, margin_lots, short_lots in rows[-max_dates:]:
        if margin_lots is None:
            continue
        short_lots = short_lots or 0.0
        point = {
            "date": iso_date,
            "total_margin_lots": round(margin_lots),
            "total_short_lots": round(short_lots),
            "market_short_margin_ratio": round(short_lots / margin_lots, 4) if margin_lots else None,
            "avg_margin_distance_pct": None,
            "avg_short_distance_pct": None,
        }
        if distances:
            per_stock = conn.execute(MARKET_HISTORY_PER_STOCK_SQL, (iso_date,)).fetchall()
            m_num = m_den = s_num = s_den = 0.0
            for code, margin_bal, short_bal in per_stock:
                dist = distances.get(code)
                if not dist:
                    continue
                if margin_bal:
                    m_num += margin_bal * dist["margin_distance_pct"]
                    m_den += margin_bal
                if short_bal:
                    s_num += short_bal * dist["short_distance_pct"]
                    s_den += short_bal
            if m_den:
                point["avg_margin_distance_pct"] = round(m_num / m_den, 2)
            if s_den:
                point["avg_short_distance_pct"] = round(s_num / s_den, 2)
        out.append(point)
    return out


def build_margin_data(conn, iso_date, top_n=15):
    """Full 融資融券 tab payload: market-wide totals + bull/bear tilt,
    stock-level rankings, and concept/industry group aggregation. Returns
    {"date": None, ...empty...} if margin_trading has no rows for iso_date
    (not yet fetched, or a market holiday) rather than raising."""
    raw = fetch_rows(conn, iso_date)
    if not raw:
        return {"date": iso_date, "summary": None, "rankings": {}, "groups": {}}

    rows = [enrich(r) for r in raw]

    total_margin_lots = sum(r["margin_balance_lots"] for r in rows)
    total_margin_ntd = sum(r["margin_balance_ntd"] for r in rows)
    total_short_lots = sum(r["short_balance_lots"] for r in rows)
    total_short_ntd = sum(r["short_balance_ntd"] for r in rows)

    summary = {
        "n_stocks": len(rows),
        "total_margin_lots": round(total_margin_lots),
        "total_margin_ntd": round(total_margin_ntd),
        "total_short_lots": round(total_short_lots),
        "total_short_ntd": round(total_short_ntd),
        # 資券比 (short/margin ratio): higher = more bearish bets relative to
        # bullish leverage outstanding. This is a LEVEL (today's standing
        # balances), not a day's flow.
        "market_short_margin_ratio": round(total_short_lots / total_margin_lots, 4) if total_margin_lots else None,
        # today's flow: net new leverage opened/closed market-wide today
        "margin_change_lots": round(sum(r["margin_change_lots"] for r in rows)),
        "short_change_lots": round(sum(r["short_change_lots"] for r in rows)),
        "margin_change_ntd": round(sum(r["margin_change_lots"] * 1000 * r["close"] for r in rows)),
        "short_change_ntd": round(sum(r["short_change_lots"] * 1000 * r["close"] for r in rows)),
    }

    rankings = {
        # 融資餘額增加最多：散戶加碼做多最兇的個股
        "margin_increase": _top(rows, "margin_change_lots", top_n),
        # 融資餘額減少最多：散戶出清/被斷頭最兇的個股
        "margin_decrease": _top(rows, "margin_change_lots", top_n, reverse=False),
        # 融券餘額增加最多：放空力道最強的個股（軋空候選的反面）
        "short_increase": _top(rows, "short_change_lots", top_n),
        # 融券餘額減少最多：空頭回補最多的個股
        "short_decrease": _top(rows, "short_change_lots", top_n, reverse=False),
        # 資券比最高：空方相對多方槓桿最重的個股
        "short_margin_ratio": _top(rows, "short_margin_ratio", top_n, require_min_balance_lots=10),
        # 融資使用率最高：最接近融資限額、最可能被迫去槓桿的個股
        "margin_usage": _top(rows, "margin_usage_pct", top_n),
    }

    industries, concepts = group_membership(conn)
    groups = {
        "industries": aggregate_groups(rows, industries),
        "concepts": aggregate_groups(rows, concepts),
    }

    return {"date": iso_date, "summary": summary, "rankings": rankings, "groups": groups}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=None,
                         help="ISO date, default: latest margin_trading date with usable stock_price overlap")
    parser.add_argument("--db", default=db.DB_PATH)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    iso_date = args.date or pick_latest_usable_date(conn)
    data = build_margin_data(conn, iso_date, top_n=args.top)
    conn.close()

    if data["summary"] is None:
        print(f"no margin_trading data for {iso_date}")
        return

    s = data["summary"]
    print(f"{iso_date} 全市場融資融券總覽（{s['n_stocks']} 檔）\n")
    print(f"融資餘額：{s['total_margin_lots']:,} 張（約 NT${s['total_margin_ntd']/1e8:.1f} 億），"
          f"今日變化 {s['margin_change_lots']:+,} 張")
    print(f"融券餘額：{s['total_short_lots']:,} 張（約 NT${s['total_short_ntd']/1e8:.1f} 億），"
          f"今日變化 {s['short_change_lots']:+,} 張")
    if s["market_short_margin_ratio"] is not None:
        print(f"市場資券比：{s['market_short_margin_ratio']:.4f}")

    def print_top(title, ranking_key, field):
        print(f"\n{title}")
        for r in data["rankings"][ranking_key]:
            print(f"  {r['code']} {r['name']}  {field}={r.get(field)}")

    print_top("融資增加最多", "margin_increase", "margin_change_lots")
    print_top("融券增加最多", "short_increase", "short_change_lots")
    print_top("資券比最高", "short_margin_ratio", "short_margin_ratio")
    print_top("融資使用率最高", "margin_usage", "margin_usage_pct")


if __name__ == "__main__":
    main()
