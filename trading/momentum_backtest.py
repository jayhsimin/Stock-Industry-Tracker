"""Price-momentum factor backtest - a SEPARATE, hypothetical strategy, not
the existing swing/day-trade strategies in trading/. The user asked, setting
aside the existing rule-based strategy, what approach a stock investor would
take to target a 10%+ return, and to simulate it over the past ~6 months.
Revenue-momentum was the first idea, but TWSE's monthly_revenue open-data
endpoint is a current-month-only snapshot (no historical date range) and
Shioaji has no fundamentals API either - there's no accessible multi-month
revenue history to backtest against. This is the fallback the user picked:
classic PRICE momentum (buy whichever stocks already have the strongest
trailing relative strength, rebalance periodically) - needs only price
history, which Shioaji's kbars() already provides ~250 calendar days of.

Design (mine, not derived from signals.py/concept_map's rules - a genuinely
different, independent strategy):
- Universe: concept_map's stocks (the curated universe already used
  elsewhere in this project - picked for convenience/known liquidity, not
  because of any theme bet; momentum here is pure price, no theme signal)
- Momentum score at each rebalance: cumulative return from t-60 to t-5
  trading days - skips the most recent week to dodge short-term reversal
  noise, a scaled-down version of the academic "12-1 month" momentum factor
  (12 months isn't available - Shioaji's history only goes back ~250 days)
- Rebalance every 20 trading days (~1 calendar month): liquidate the whole
  portfolio, re-rank, equal-weight the new top N - full turnover each
  period (more fee drag than only trading the names that changed, but
  matches how the academic momentum factor is actually measured)
- No per-stock stop-loss - pure factor rotation; a tight stop would
  conflate two different risk mechanisms and isn't part of what's being
  tested here
- Fees: same convention as trading/breakout_backtest.py's swing trades
  (FEE_RATE each way + normal, non-day-trade 證交稅 on the sell leg) -
  these are multi-week holds, not day-trades

Usage:
    python -m trading.momentum_backtest --days 182 --top-n 10
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import pandas as pd

from . import daily_scan, executor
from .daytrade import minute_data

FEE_RATE = 0.001425
TAX_RATE_NORMAL_SELL = 0.003

WARMUP_DAYS = 120       # extra calendar days fetched before the eval window
LOOKBACK_DAYS = 60      # trading days of trailing return for the momentum score
SKIP_DAYS = 5           # most recent trading days excluded from the score
REBALANCE_EVERY = 20    # trading days between rebalances (~1 calendar month)
INITIAL_CAPITAL = 1_000_000  # illustrative, separate from the swing/day-trade pools


def daily_closes(api, contract, start, end):
    bars = minute_data.bars_for_range(api, contract, start, end)
    if bars.empty:
        return pd.Series(dtype=float)
    closes = bars["Close"].resample("1D").last().dropna()
    closes.index = closes.index.date
    return closes


def net_return(entry_price, exit_price, shares):
    cost = entry_price * shares * (1 + FEE_RATE)
    proceeds = exit_price * shares * (1 - FEE_RATE - TAX_RATE_NORMAL_SELL)
    pnl = proceeds - cost
    return pnl, pnl / cost


def momentum_return(closes_df, code, idx):
    """Raw cumulative return from t-LOOKBACK_DAYS to t-SKIP_DAYS, or None if
    there isn't enough history. Shared by all three factors below - reversal
    and low-vol both still need the plain return number (reversal ranks by
    it inverted, low-vol needs the same window to compute volatility over)."""
    if idx < LOOKBACK_DAYS:
        return None
    p_recent = closes_df[code].iloc[idx - SKIP_DAYS]
    p_old = closes_df[code].iloc[idx - LOOKBACK_DAYS]
    if pd.isna(p_recent) or pd.isna(p_old) or p_old <= 0:
        return None
    return (p_recent - p_old) / p_old


def factor_value(closes_df, code, idx, factor):
    """Returns (rank_score, display_value) - rank_score is always "higher
    picked first" regardless of factor, so run() doesn't need to know which
    factor it's ranking; display_value is the real-world number to show in
    reports (raw momentum % for momentum/reversal, raw volatility for
    lowvol). None if there isn't enough history to compute it.

    - momentum: classic "buy the strongest trailing performers" factor
    - reversal: short-term reversal - the flip side of momentum, buy the
      WORST trailing performers (bet on a bounce) - motivated directly by
      the momentum backtest's own result: June's biggest winners became
      July's biggest losers, so betting the other way is worth checking
    - lowvol: low-volatility anomaly - ignores return direction entirely,
      buys whichever stocks moved the LEAST (lowest daily-return std) over
      the lookback window, trading the chance of a big gain for avoiding
      the momentum factor's crash risk
    """
    ret = momentum_return(closes_df, code, idx)
    if ret is None:
        return None
    if factor == "momentum":
        return ret, ret
    if factor == "reversal":
        return -ret, ret
    if factor == "lowvol":
        window = closes_df[code].iloc[idx - LOOKBACK_DAYS: idx - SKIP_DAYS + 1]
        daily_rets = window.pct_change().dropna()
        if len(daily_rets) < 10:
            return None
        vol = daily_rets.std()
        return -vol, vol
    raise ValueError(f"unknown factor: {factor}")


def run(closes_df, eval_dates, top_n, rebalance_every=REBALANCE_EVERY, factor="momentum"):
    rebalance_dates = eval_dates[0::rebalance_every]
    cash = INITIAL_CAPITAL
    holdings = {}  # code -> {shares, entry_price, entry_date}
    trades = []
    rebalance_log = []

    for d in rebalance_dates:
        idx = closes_df.index.get_loc(d)

        # liquidate everything at today's close
        for code, pos in holdings.items():
            price = closes_df[code].iloc[idx]
            if pd.isna(price):
                continue
            pnl, net_ret = net_return(pos["entry_price"], price, pos["shares"])
            cash += pos["entry_price"] * pos["shares"] * (1 + FEE_RATE) + pnl  # return cost basis + pnl
            trades.append({"code": code, "entry_date": pos["entry_date"], "exit_date": d.isoformat(),
                            "entry_price": pos["entry_price"], "exit_price": price,
                            "net_return": net_ret, "net_pnl": pnl})
        holdings = {}

        # rank by the chosen factor as of today, no look-ahead
        scores = []
        for code in closes_df.columns:
            price_today = closes_df[code].iloc[idx]
            if pd.isna(price_today):
                continue
            result = factor_value(closes_df, code, idx, factor)
            if result is None:
                continue
            rank_score, display_value = result
            scores.append((code, display_value, price_today, rank_score))
        scores.sort(key=lambda x: x[3], reverse=True)
        scores = [(c, s, p) for c, s, p, _ in scores]
        picks = scores[:top_n]
        rebalance_log.append({"date": d.isoformat(), "n_candidates": len(scores),
                               "picks": [(c, s) for c, s, _ in picks]})

        if not picks:
            continue
        allocation = cash / len(picks)
        for code, score, price in picks:
            shares = allocation / price
            cash -= allocation * (1 + FEE_RATE)
            holdings[code] = {"shares": shares, "entry_price": price, "entry_date": d.isoformat()}

    # force-close at the last available date
    last_date = closes_df.index[-1]
    last_idx = len(closes_df.index) - 1
    for code, pos in holdings.items():
        price = closes_df[code].iloc[last_idx]
        if pd.isna(price):
            continue
        pnl, net_ret = net_return(pos["entry_price"], price, pos["shares"])
        cash += pos["entry_price"] * pos["shares"] * (1 + FEE_RATE) + pnl
        trades.append({"code": code, "entry_date": pos["entry_date"], "exit_date": last_date.isoformat(),
                        "entry_price": pos["entry_price"], "exit_price": price,
                        "net_return": net_ret, "net_pnl": pnl})

    return trades, rebalance_log, cash


def period_breakdown(trades):
    if not trades:
        return "（無交易）"
    by_month = {}
    for t in trades:
        by_month.setdefault(t["entry_date"][:7], []).append(t)
    lines = []
    for month, ts in sorted(by_month.items()):
        n = len(ts)
        wins = sum(1 for t in ts if t["net_return"] > 0)
        avg_ret = sum(t["net_return"] for t in ts) / n
        lines.append(f"  {month}：{n}檔｜勝率{wins}/{n}={wins/n*100:.0f}%｜平均報酬{avg_ret*100:+.2f}%")
    return "\n".join(lines)


FACTOR_LABELS = {
    "momentum": "價格動能（買過去強勢股）",
    "reversal": "短期反轉（買過去最弱勢股，賭反彈）",
    "lowvol": "低波動（買過去波動最小的股票，不論漲跌方向）",
}


def format_result(eval_start, end, n_universe, top_n, rebalance_every, factor, trades, rebalance_log, final_cash, names):
    total_return = (final_cash - INITIAL_CAPITAL) / INITIAL_CAPITAL
    n = len(trades)
    wins = sum(1 for t in trades if t["net_return"] > 0)
    lines = [
        f"=== {FACTOR_LABELS[factor]}因子回測（獨立於現有波段/當沖策略）：{eval_start.isoformat()} ~ {end.isoformat()}"
        f"（{n_universe}檔候選股票）===",
        "",
        f"策略：每{rebalance_every}個交易日全部換股，持有過去{LOOKBACK_DAYS}個交易日"
        f"(排除最近{SKIP_DAYS}天)窗口內，依「{FACTOR_LABELS[factor]}」排名前{top_n}檔，等權重配置",
        f"假設起始資金：NT${INITIAL_CAPITAL:,.0f}（純為示意，跟現有波段/當沖策略的資金池無關）",
        f"手續費：買賣各{FEE_RATE*100:.3f}%｜賣出證交稅：{TAX_RATE_NORMAL_SELL*100:.2f}%（非當沖稅率，持有數週以上）",
        f"換股次數：{len(rebalance_log)}",
        "",
        f"期末資金：NT${final_cash:,.0f}｜總報酬：{total_return*100:+.2f}%"
        f"｜{'有達到10%目標' if total_return >= 0.10 else '未達10%目標'}",
        "",
        f"個股進出總筆數：{n}" + (f"｜勝率：{wins}/{n}={wins/n*100:.1f}%" if n else ""),
        "",
        "-- 按月份分期檢視（每個進場月份的個股勝率/平均報酬）--",
        period_breakdown(trades),
        "",
        "-- 每次換股的選股結果 --",
    ]
    value_fmt = "{:.2%}" if factor == "lowvol" else "{:+.1%}"
    for r in rebalance_log:
        picks_str = "、".join(f"{c}({names.get(c, c)}){value_fmt.format(s)}" for c, s in r["picks"])
        lines.append(f"{r['date']}（候選{r['n_candidates']}檔）：{picks_str}")

    lines.append("")
    lines.append("-- 個股進出明細 --")
    for t in sorted(trades, key=lambda x: x["entry_date"]):
        lines.append(
            f"{t['entry_date']} {t['code']}{names.get(t['code'], '')}｜進場@{t['entry_price']:.2f} -> "
            f"{t['exit_date']} 出場@{t['exit_price']:.2f}｜{t['net_return']*100:+.2f}%｜NT${t['net_pnl']:,.0f}"
        )
    return lines, total_return


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=182, help="evaluation window in calendar days (default ~6 months)")
    parser.add_argument("--top-n", type=int, default=10, help="number of stocks held at a time")
    parser.add_argument("--rebalance-days", type=int, default=REBALANCE_EVERY,
                         help="trading days between rebalances")
    parser.add_argument("--factor", choices=sorted(FACTOR_LABELS), default="momentum")
    parser.add_argument("--compare", action="store_true",
                         help="fetch data once, then test a small grid of top-n/rebalance-days combos (momentum "
                              "factor only) instead of a single run - checks sensitivity to those two knobs")
    parser.add_argument("--compare-factors", action="store_true",
                         help="fetch data once, then test momentum vs reversal vs lowvol at top_n=15/every 20 "
                              "days (the best momentum setting found) - checks whether a genuinely different "
                              "factor does better than momentum on this same window")
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default="trading/momentum_backtest_result.txt")
    args = parser.parse_args()

    import db
    conn = sqlite3.connect(args.db or db.DB_PATH)
    code_concepts, names = daily_scan.load_universe(conn)
    conn.close()
    codes = args.codes if args.codes else sorted(code_concepts.keys())
    print(f"universe: {len(codes)} concept_map stocks")

    eval_start = date.today() - timedelta(days=args.days)
    fetch_start = eval_start - timedelta(days=WARMUP_DAYS)
    end = date.today()

    api, live = executor.login(False)
    stock_closes = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for code in codes:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            closes = daily_closes(api, contract, fetch_start.isoformat(), end.isoformat())
            if len(closes) < LOOKBACK_DAYS + 5:
                continue
            stock_closes[code] = closes
        api.logout()
    print(f"{len(stock_closes)}/{len(codes)} 檔有足夠的歷史資料（含熱身期）")

    if not stock_closes:
        print("no stocks with enough history - aborting")
        return

    closes_df = pd.DataFrame(stock_closes).sort_index().ffill(limit=3)
    eval_dates = [d for d in closes_df.index if d >= eval_start]
    print(f"回測區間：{eval_start.isoformat()} ~ {end.isoformat()}（{len(eval_dates)}個交易日）")

    if args.compare:
        variants = [(10, 20, args.factor), (15, 20, args.factor), (10, 60, args.factor),
                    (15, 60, args.factor), (20, 60, args.factor)]
    elif args.compare_factors:
        variants = [(15, 20, "momentum"), (15, 20, "reversal"), (15, 20, "lowvol")]
    else:
        variants = [(args.top_n, args.rebalance_days, args.factor)]

    all_lines = []
    summary_rows = []
    for top_n, rebalance_every, factor in variants:
        trades, rebalance_log, final_cash = run(closes_df, eval_dates, top_n, rebalance_every, factor)
        lines, total_return = format_result(eval_start, end, len(stock_closes), top_n, rebalance_every, factor,
                                             trades, rebalance_log, final_cash, names)
        n = len(trades)
        wins = sum(1 for t in trades if t["net_return"] > 0)
        summary_rows.append(
            f"factor={factor}｜top_n={top_n}｜每{rebalance_every}天換股｜換股{len(rebalance_log)}次｜交易{n}筆｜"
            f"勝率{(wins/n*100 if n else 0):.1f}%｜總報酬{total_return*100:+.2f}%"
            f"｜{'有' if total_return >= 0.10 else '未'}達10%目標"
        )
        all_lines.append(f"\n{'='*70}\n")
        all_lines.extend(lines)

    if args.compare or args.compare_factors:
        title = "參數敏感度比較（同一份歷史資料，不同top_n/換股頻率組合）" if args.compare \
            else "因子比較（同一份歷史資料，momentum vs reversal vs lowvol）"
        header = [f"=== {title} ===", ""] + summary_rows
        text = "\n".join(header) + "\n" + "\n".join(all_lines)
    else:
        text = "\n".join(all_lines)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print("\n".join(summary_rows) if (args.compare or args.compare_factors) else text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
