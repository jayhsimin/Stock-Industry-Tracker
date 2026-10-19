"""Backtest for the "quiet strength" signal found by
price_volume_pattern_study.py - a SEPARATE, hypothetical strategy, not
production. That script bucketed historical stock-days by (prior-day
%change, volume vs the stock's own trailing 20-day average) and measured
forward EXCESS return (vs. that day's cross-sectional average across the
whole concept_map universe, to strip out generic market/theme drift).
The one bucket that came back significant AND same-sign in both halves of
a chronological train/test split, at every horizon (1/3/5/10 days), was:

    單日大漲 >= +5%，但當日成交量 < 自身20日均量的0.7倍（量縮）

i.e. a big up day on BELOW-average volume - no blow-off/no short-squeeze
spike, read as "quiet accumulation" rather than a one-day pop that's about
to mean-revert. This script turns that into an actual tradeable rule with
entries/exits/position sizing/fees, not just descriptive statistics, since
a bucket's average forward return doesn't by itself prove a position-sized,
cost-aware strategy makes money (overlapping stock-day samples inflate the
statistical study's significance - see its docstring).

Entry: pct_change >= CHANGE_THRESHOLD and vol_ratio < VOL_RATIO_THRESHOLD,
today's Amount >= MIN_TRADE_VALUE_NTD (same liquidity floor used
project-wide). Buy at that day's close (same simplifying convention
channel_backtest.py/swing_backtest.py already use - signal and fill share
a bar).

Exit: EITHER a stop-loss (unrealized <= STOP_LOSS_PCT, checked every day)
OR a fixed holding period (HOLD_DAYS trading days) - this directly mirrors
what the statistical study measured (average return exactly N days later),
so this backtest is a faithful test of whether that number survives real
trade-level accounting (fees, one lot at a time, a shared budget cap), not
a different strategy shape.

Usage:
    python -m trading.quiet_strength_backtest --days 250
    python -m trading.quiet_strength_backtest --days 250 --hold-days 10
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import pandas as pd

from . import daily_scan, executor, positions
from .channel_backtest import daily_ohlcv

FEE_RATE = 0.001425
TAX_RATE_NORMAL_SELL = 0.003

WARMUP_DAYS = 40          # extra calendar days so the 20d volume average is full from day 1
VOL_AVG_WINDOW = 20
CHANGE_THRESHOLD = 0.05    # prior-day %change >= +5%
VOL_RATIO_THRESHOLD = 0.7  # today's volume < 0.7x its own 20d average
HOLD_DAYS = 5              # fixed holding period - matches the horizon with the strongest,
                            # most consistent train/test significance in the statistical study
STOP_LOSS_PCT = -0.05      # risk control not present in the pure statistical study - added here
                            # since every other strategy in this project has one; same magnitude
                            # as the production swing strategy's
ORDER_LOT_SHARES = 1_000
MAX_TOTAL_POSITION_COST_NTD = 200_000
MIN_TRADE_VALUE_NTD = 100_000_000  # same absolute liquidity floor used project-wide


def compute_signal(daily, vol_window=VOL_AVG_WINDOW, change_threshold=CHANGE_THRESHOLD,
                    vol_ratio_threshold=VOL_RATIO_THRESHOLD):
    """Adds pct_change/vol_ratio/entry_signal columns. vol_ratio's baseline
    average EXCLUDES today (shift(1)) so there's no look-ahead."""
    df = daily.copy()
    df["pct_change"] = df["Close"].pct_change()
    vol_avg = df["Volume"].rolling(vol_window).mean().shift(1)
    df["vol_ratio"] = df["Volume"] / vol_avg
    df["entry_signal"] = (df["pct_change"] >= change_threshold) & (df["vol_ratio"] < vol_ratio_threshold)
    return df


def net_return(entry_price, exit_price, shares):
    cost = entry_price * shares * (1 + FEE_RATE)
    proceeds = exit_price * shares * (1 - FEE_RATE - TAX_RATE_NORMAL_SELL)
    pnl = proceeds - cost
    return pnl, pnl / cost


def shares_to_buy(price, remaining_budget):
    cost = price * ORDER_LOT_SHARES
    return ORDER_LOT_SHARES if cost <= remaining_budget else 0


def run(stock_data, names, eval_dates, hold_days=HOLD_DAYS, stop_loss_pct=STOP_LOSS_PCT):
    open_positions = {}
    trades = []
    budget_skips = 0

    for d in eval_dates:
        # --- exits: stop-loss or fixed holding period ---
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
            if unrealized <= stop_loss_pct:
                reason = "stop_loss"
            elif held >= hold_days:
                reason = "hold_days_exit"
            if reason:
                pnl, net_ret = net_return(pos["entry_price"], row["Close"], pos["shares"])
                trades.append({"code": code, "name": pos["name"], "entry_date": pos["entry_date"],
                                "exit_date": d.isoformat(), "entry_price": pos["entry_price"],
                                "exit_price": row["Close"], "reason": reason, "held_days": held,
                                "net_return": net_ret, "net_pnl": pnl})
                del open_positions[code]

        # --- entries ---
        running_cost = sum(p["entry_price"] * p["shares"] for p in open_positions.values())
        for code, df in stock_data.items():
            if code in open_positions or d not in df.index:
                continue
            row = df.loc[d]
            if not row.get("entry_signal", False):
                continue
            if pd.isna(row["Amount"]) or row["Amount"] < MIN_TRADE_VALUE_NTD:
                continue
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
        return "沒有任何交易（這段期間沒有股票觸發大漲+量縮訊號）。"
    n = len(trades)
    wins = sum(1 for t in trades if t["net_return"] > 0)
    total_pnl = sum(t["net_pnl"] for t in trades)
    avg_ret = sum(t["net_return"] for t in trades) / n
    avg_held = sum(t["held_days"] for t in trades) / n
    by_reason = {}
    for t in trades:
        by_reason.setdefault(t["reason"], []).append(t)
    reason_lines = "\n".join(
        f"  {r}: {len(ts)}筆｜勝率{sum(1 for t in ts if t['net_return'] > 0)}/{len(ts)}｜"
        f"平均報酬{sum(t['net_return'] for t in ts) / len(ts) * 100:+.2f}%"
        for r, ts in sorted(by_reason.items())
    )
    return (f"總交易次數：{n}｜勝率：{wins}/{n}={wins/n*100:.1f}%｜"
            f"總損益：NT${total_pnl:,.0f}｜平均報酬：{avg_ret*100:+.2f}%｜平均持有：{avg_held:.1f}天\n"
            f"-- 依出場原因 --\n{reason_lines}")


def monthly_breakdown(trades):
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
        pnl = sum(t["net_pnl"] for t in ts)
        lines.append(f"  {month}：{n}筆｜勝率{wins}/{n}={wins/n*100:.0f}%｜平均報酬{avg_ret*100:+.2f}%｜損益NT${pnl:,.0f}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=250)
    parser.add_argument("--hold-days", type=int, default=HOLD_DAYS)
    parser.add_argument("--stop-loss", type=float, default=STOP_LOSS_PCT)
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default="trading/quiet_strength_backtest_result.txt")
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
    stock_data = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for code in codes:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            daily = daily_ohlcv(api, contract, fetch_start.isoformat(), end.isoformat())
            if len(daily) < VOL_AVG_WINDOW + args.hold_days + 10:
                continue
            stock_data[code] = compute_signal(daily)
        api.logout()
    print(f"{len(stock_data)}/{len(codes)} 檔有足夠的歷史資料（含熱身期）")

    if not stock_data:
        print("no stocks with enough history - aborting")
        return

    all_dates = sorted(set().union(*(set(df.index) for df in stock_data.values())))
    eval_dates = [d for d in all_dates if d >= eval_start]
    print(f"回測區間：{eval_start.isoformat()} ~ {end.isoformat()}（{len(eval_dates)}個交易日）")

    trades, budget_skips = run(stock_data, names, eval_dates, hold_days=args.hold_days, stop_loss_pct=args.stop_loss)

    lines = [
        f"大漲(>=+{CHANGE_THRESHOLD*100:.0f}%)+量縮(<{VOL_RATIO_THRESHOLD}x均量) 策略回測",
        f"回測區間：{eval_start.isoformat()} ~ {end.isoformat()}｜股票數：{len(stock_data)}｜"
        f"持有天數：{args.hold_days}｜停損：{args.stop_loss*100:.0f}%",
        f"因資金上限({MAX_TOTAL_POSITION_COST_NTD:,}元)被擋掉的進場次數：{budget_skips}",
        "",
        summarize(trades),
        "",
        "-- 月別績效 --",
        monthly_breakdown(trades),
    ]
    text = "\n".join(lines)
    print(text)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
