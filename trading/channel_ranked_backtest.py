"""Improves the channel strategy's entry selection with a multi-factor
candidate ranking - user-specified: when the channel's price signal (break
below the lower trendline) flags a candidate, score it on fundamentals,
industry trend and chip/institutional flow, weight those by experience, and
buy the best-ranked candidate(s) first instead of channel_backtest.py's
current "buy whichever qualifies, in code-sorted order, until the shared
budget runs out" rule. This is a SEPARATE, hypothetical refinement, not
production - channel_backtest.py/channel_cv.py's validated numbers are
unaffected; trading/channel/ (the live-scheduled module) is untouched until
this proves out.

Everything here reuses channel_backtest.py's core pieces UNCHANGED
(daily_ohlcv/compute_channel/net_return/shares_to_buy, same constants) so
the only thing this script can possibly be testing is the entry-ordering
change, not a different channel/exit rule.

Three factors, each turned into a percentile rank (0-1) WITHIN that day's
candidate pool (not globally) before weighting, since fundamentals/industry/
chip are on wildly different scales (%, %, raw NT$ flow):

  fundamentals - latest available monthly_revenue YoY growth (去年同月增減%).
  industry     - the candidate's official 產業別 peers' (other concept_map
                 stocks in industry_map, excluding the candidate itself)
                 average trailing 5-day price return, computed from the SAME
                 daily OHLCV already fetched for the whole universe - no
                 extra API calls, and no look-ahead (only data up to and
                 including day d).
  chip         - trailing 10-day net 三大法人買賣超 (trading.swing_backtest's
                 load_institutional_flow(), reused directly).

**IMPORTANT CAVEAT, read before trusting any backtest number for the
fundamentals factor specifically**: monthly_revenue is a current-snapshot-
only table (confirmed no historical-query support, same limitation as
stock_price/stock_valuation - see CLAUDE.md). This backtest can only use
"whichever revenue report is latest as of when the backtest is RUN," applied
uniformly across the whole historical window - NOT what was actually known
on each historical day. That is look-ahead bias. The chip and industry
factors use genuine point-in-time historical data and are backtest-honest;
fundamentals is not, and its weight should ultimately be validated by a
forward/live paper-trading run, not trusted from this backtest alone.

Usage:
    python -m trading.channel_ranked_backtest --days 250                       # baseline (unranked)
    python -m trading.channel_ranked_backtest --days 250 --rank                # ranked, unlimited entries/day
    python -m trading.channel_ranked_backtest --days 250 --rank --max-entries 1   # only the single best candidate/day
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import pandas as pd

from . import daily_scan, executor, positions, swing_backtest
from .channel_backtest import (
    FEE_RATE, MAX_HOLD_DAYS, MAX_TOTAL_POSITION_COST_NTD, MIN_TRADE_VALUE_NTD,
    ORDER_LOT_SHARES, PIVOT_K, STOP_LOSS_PCT, TAX_RATE_NORMAL_SELL, WARMUP_DAYS,
    compute_channel, daily_ohlcv, net_return, shares_to_buy,
)

INSTITUTIONAL_FLOW_DAYS = 10
INDUSTRY_TREND_LOOKBACK = 5

# Initial "經驗" weights - NOT yet validated, see channel_ranked_cv.py for the
# grid search that checks whether this guess (or any other combination) beats
# the unranked baseline. fundamentals/chip weighted higher than industry
# since a sector-wide move is a weaker individual-stock signal than the
# company's own revenue trend or whether institutions are actually buying it.
DEFAULT_WEIGHTS = {"fundamentals": 0.4, "industry": 0.25, "chip": 0.35}


def load_fundamentals(conn):
    """{code: revenue YoY growth %} from monthly_revenue's latest row per
    company. See module docstring's caveat - this is always "latest as of
    now," not point-in-time historical."""
    rows = conn.execute(
        'SELECT "公司代號", "營業收入-去年同月增減(%)", "資料年月" FROM monthly_revenue'
    ).fetchall()
    best = {}
    for code, growth, ym in rows:
        try:
            g = float(growth)
        except (TypeError, ValueError):
            continue
        if code not in best or ym > best[code][1]:
            best[code] = (g, ym)
    return {code: g for code, (g, _ym) in best.items()}


def load_industry_map(conn):
    return dict(conn.execute('SELECT "公司代號", "產業別" FROM industry_map'))


def compute_industry_trend(stock_data, industry_map, lookback=INDUSTRY_TREND_LOOKBACK):
    """{code: Series(date -> that code's industry PEERS' (excluding itself)
    average trailing `lookback`-day return)}, computed once for the whole
    window - no look-ahead, each day's value only uses closes up to that day."""
    closes = pd.DataFrame({code: df["Close"] for code, df in stock_data.items()})
    returns_n = closes / closes.shift(lookback) - 1
    by_industry = {}
    for code in closes.columns:
        by_industry.setdefault(industry_map.get(code), []).append(code)
    result = {}
    for code in closes.columns:
        peers = [c for c in by_industry.get(industry_map.get(code), []) if c != code]
        result[code] = returns_n[peers].mean(axis=1) if peers else pd.Series(dtype=float)
    return result


def percentile_rank(values):
    """{code: value_or_None} -> {code: percentile rank in [0,1], or None if
    that code had no usable value}. Missing values are excluded from the
    ranking (not treated as worst-case zero) so one missing factor doesn't
    unfairly tank a candidate that's otherwise fine on the other two."""
    items = [(c, v) for c, v in values.items() if v is not None and pd.notna(v)]
    if not items:
        return {c: None for c in values}
    items.sort(key=lambda x: x[1])
    n = len(items)
    ranked = {c: (i / (n - 1) if n > 1 else 0.5) for i, (c, _v) in enumerate(items)}
    return {c: ranked.get(c) for c in values}


def composite_scores(candidates, fundamentals, industry_trend, inst_flow, d, weights):
    """{code: weighted composite score in [0,1]} for this day's candidates
    only. A candidate with no data on ANY factor gets a neutral 0.5 rather
    than being dropped - missing chip data for TPEx-listed stocks shouldn't
    disqualify an otherwise-good candidate (though concept_map is currently
    all-TWSE, so this mostly matters for robustness, not today's actual data)."""
    fund_vals = {c: fundamentals.get(c) for c in candidates}
    ind_vals = {c: industry_trend[c].get(d) if c in industry_trend else None for c in candidates}
    chip_vals = {c: inst_flow[c].get(d) if c in inst_flow else None for c in candidates}
    fund_r, ind_r, chip_r = percentile_rank(fund_vals), percentile_rank(ind_vals), percentile_rank(chip_vals)
    scores = {}
    for c in candidates:
        parts, wsum = [], 0.0
        for r, w in ((fund_r, weights["fundamentals"]), (ind_r, weights["industry"]), (chip_r, weights["chip"])):
            v = r.get(c)
            if v is None:
                continue
            parts.append(v * w)
            wsum += w
        scores[c] = (sum(parts) / wsum) if wsum > 0 else 0.5
    return scores


def collect_candidates(stock_data, d, open_positions):
    cands = []
    for code, df in stock_data.items():
        if code in open_positions or d not in df.index:
            continue
        row = df.loc[d]
        if pd.isna(row["Close"]) or pd.isna(row["lower_line"]) or pd.isna(row["Amount"]):
            continue
        if row["Close"] >= row["lower_line"]:
            continue
        if row["Amount"] < MIN_TRADE_VALUE_NTD:
            continue
        cands.append(code)
    return cands


def run(stock_data, names, eval_dates, fundamentals=None, industry_trend=None, inst_flow=None,
        weights=None, rank_mode=False, max_new_entries_per_day=None):
    """rank_mode=False reproduces channel_backtest.run()'s exact behavior
    (code-sorted order) - the baseline for comparison. rank_mode=True scores
    and sorts each day's candidates by composite_scores() before allocating
    budget. max_new_entries_per_day caps how many NEW positions open per day
    regardless of remaining budget - the literal "only buy the single best
    target" reading of the user's request, tested as its own option rather
    than assumed to help."""
    weights = weights or DEFAULT_WEIGHTS
    fundamentals, industry_trend, inst_flow = fundamentals or {}, industry_trend or {}, inst_flow or {}
    open_positions = {}
    trades = []
    budget_skips = 0

    for d in eval_dates:
        for code in list(open_positions):
            df = stock_data[code]
            if d not in df.index:
                continue
            row = df.loc[d]
            if pd.isna(row["Close"]):
                continue
            pos = open_positions[code]
            held = positions.days_held(pos["entry_date"], as_of=d)
            unrealized = (row["Close"] - pos["entry_price"]) / pos["entry_price"]
            reason = None
            if pd.notna(row["upper_line"]) and row["Close"] > row["upper_line"]:
                reason = "upper_line_break"
            elif unrealized <= STOP_LOSS_PCT:
                reason = "stop_loss"
            elif held > MAX_HOLD_DAYS:
                reason = "max_hold_days"
            if reason:
                pnl, net_ret = net_return(pos["entry_price"], row["Close"], pos["shares"])
                trades.append({"code": code, "name": pos["name"], "entry_date": pos["entry_date"],
                                "exit_date": d.isoformat(), "entry_price": pos["entry_price"],
                                "exit_price": row["Close"], "reason": reason, "held_days": held,
                                "net_return": net_ret, "net_pnl": pnl})
                del open_positions[code]

        candidates = collect_candidates(stock_data, d, open_positions)
        if rank_mode:
            scores = composite_scores(candidates, fundamentals, industry_trend, inst_flow, d, weights)
            candidates = sorted(candidates, key=lambda c: scores[c], reverse=True)
        else:
            candidates = sorted(candidates)
        if max_new_entries_per_day is not None:
            candidates = candidates[:max_new_entries_per_day]

        running_cost = sum(p["entry_price"] * p["shares"] for p in open_positions.values())
        for code in candidates:
            row = stock_data[code].loc[d]
            shares = shares_to_buy(row["Close"], MAX_TOTAL_POSITION_COST_NTD - running_cost)
            if shares < 1:
                budget_skips += 1
                continue
            open_positions[code] = {"name": names.get(code, code), "entry_price": row["Close"],
                                     "shares": shares, "entry_date": d.isoformat()}
            running_cost += row["Close"] * shares

    for code, pos in open_positions.items():
        last_valid = stock_data[code]["Close"].dropna()
        if last_valid.empty:
            continue
        exit_price = last_valid.iloc[-1]
        exit_date = last_valid.index[-1]
        held = positions.days_held(pos["entry_date"], as_of=exit_date)
        pnl, net_ret = net_return(pos["entry_price"], exit_price, pos["shares"])
        trades.append({"code": code, "name": pos["name"], "entry_date": pos["entry_date"],
                        "exit_date": exit_date.isoformat(), "entry_price": pos["entry_price"],
                        "exit_price": exit_price, "reason": "end_of_window_safety_net",
                        "held_days": held, "net_return": net_ret, "net_pnl": pnl})

    return trades, budget_skips


def summarize(trades):
    if not trades:
        return "沒有任何交易。"
    n = len(trades)
    wins = sum(1 for t in trades if t["net_return"] > 0)
    total_pnl = sum(t["net_pnl"] for t in trades)
    avg_ret = sum(t["net_return"] for t in trades) / n
    return (f"總交易次數：{n}｜勝率：{wins}/{n}={wins/n*100:.1f}%｜"
            f"總損益：NT${total_pnl:,.0f}｜平均報酬：{avg_ret*100:+.2f}%")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=250)
    parser.add_argument("--rank", action="store_true", help="use composite-score ranked entry order")
    parser.add_argument("--max-entries", type=int, default=None, help="cap new entries per day (quality gate)")
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default="trading/channel_ranked_backtest_result.txt")
    args = parser.parse_args()

    import db
    conn = sqlite3.connect(args.db or db.DB_PATH)
    code_concepts, names = daily_scan.load_universe(conn)
    fundamentals = load_fundamentals(conn)
    industry_map = load_industry_map(conn)
    inst_flow = swing_backtest.load_institutional_flow(conn, flow_days=INSTITUTIONAL_FLOW_DAYS)
    conn.close()
    codes = args.codes if args.codes else sorted(code_concepts.keys())
    print(f"universe: {len(codes)} concept_map stocks｜{len(fundamentals)} 檔有營收資料｜"
          f"{len(inst_flow)} 檔有三大法人歷史資料")

    eval_start = date.today() - timedelta(days=args.days)
    fetch_start = eval_start - timedelta(days=WARMUP_DAYS)
    end = date.today()

    api, live = executor.login(False)
    stock_data = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for code in codes:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            daily = daily_ohlcv(api, contract, fetch_start.isoformat(), end.isoformat())
            if len(daily) < 2 * PIVOT_K + 10:
                continue
            stock_data[code] = compute_channel(daily)
        api.logout()
    print(f"{len(stock_data)}/{len(codes)} 檔有足夠的歷史資料（含熱身期）")

    industry_trend = compute_industry_trend(stock_data, industry_map)

    all_dates = sorted(set().union(*(set(df.index) for df in stock_data.values())))
    eval_dates = [d for d in all_dates if d >= eval_start]
    print(f"回測區間：{eval_start.isoformat()} ~ {end.isoformat()}（{len(eval_dates)}個交易日）"
          f"｜模式：{'ranked' if args.rank else 'baseline(unranked)'}"
          + (f"｜每日最多新進場{args.max_entries}檔" if args.max_entries else ""))

    trades, budget_skips = run(stock_data, names, eval_dates, fundamentals=fundamentals,
                                industry_trend=industry_trend, inst_flow=inst_flow,
                                rank_mode=args.rank, max_new_entries_per_day=args.max_entries)

    lines = [summarize(trades), f"因資金上限或每日上限被擋掉的進場次數：{budget_skips}"]
    text = "\n".join(lines)
    print(text)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
