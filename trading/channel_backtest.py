"""Trendline-channel mean-reversion backtest - a SEPARATE, hypothetical
strategy, not the production swing/day-trade/ma_touch ones. User-specified
concept: find relative (swing) highs and lows on the price chart, connect
each into its own trendline (not a flat rolling high/low - an actual
sloped 2-point line through the two most recent swing points), then trade
the channel as a mean-reversion bet: BUY when price breaks BELOW the
lower trendline (expecting reversion back up into the channel), SELL when
price breaks ABOVE the upper trendline. This is the opposite of a
breakout-following strategy - buying weakness, selling strength.

Swing-point detection: bar i is a swing high if its High is the max over
the window [i-K, i+K] (K=3 bars either side by default - a "fractal"
pivot), swing low similarly for Low. A swing point can only be CONFIRMED
once K bars have passed after it (you need to see K bars on both sides to
know it's a local extreme), so the channel used to decide day T's
entry/exit only ever uses swing points confirmed by day T - no look-ahead.
The trendline is a straight line through the two most recently confirmed
swing points of that type, extrapolated to day T's bar position.

No ranking/budget-sharing across candidates - mirrors the simplest
"buy whichever qualifies, 1 lot if the shared budget allows" pattern
already used by trading/ma_touch/scan.py, not swing's theme-ranked
version, since there's no natural ranking signal for a pure price-channel
rule.

Usage:
    python -m trading.channel_backtest --days 180
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import daily_scan, executor, positions
from .daytrade import minute_data

FEE_RATE = 0.001425
TAX_RATE_NORMAL_SELL = 0.003

WARMUP_DAYS = 60   # extra calendar days so the first swing points are
                     # already confirmable on day 1 of the eval window
PIVOT_K = 3          # bars on each side required to confirm a swing point
STOP_LOSS_PCT = -0.08   # wider than the swing strategy's -5% - a channel
# mean-reversion trade that keeps going against you needs more room before
# conceding the bet failed, since the channel itself is still "valid"
# right up until price makes a new confirmed swing extreme
MAX_HOLD_DAYS = 10      # also wider than swing's 5 - waiting for price to
# cross back to the OPPOSITE trendline can genuinely take longer than a
# single week
ORDER_LOT_SHARES = 1_000
MAX_TOTAL_POSITION_COST_NTD = 200_000
MIN_TRADE_VALUE_NTD = 100_000_000  # same absolute liquidity floor used project-wide

# --- circuit breaker ---------------------------------------------------
# 2026-10: added after the 4-period validation found one clearly bad
# ~1-month stretch (27.3% win rate) sitting between two strong ones -
# portfolio-wide (not per-stock), since a single stock's losing streak
# doesn't necessarily mean the channel rule itself has turned bad, but a
# cluster of losses across MANY stocks in a short window is a more
# meaningful signal that conditions (market-wide) have turned unfavorable
# for this kind of mean-reversion bet.
STREAK_LOOKBACK = 8        # only start judging once this many trades have closed
STREAK_WIN_RATE_MIN = 0.35  # below this win rate over the last STREAK_LOOKBACK
                             # closed trades (portfolio-wide), pause new entries
PAUSE_DAYS = 10             # how many calendar days to pause new entries for
                             # once triggered, before re-checking


def daily_ohlcv(api, contract, start, end):
    bars = minute_data.bars_for_range(api, contract, start, end)
    if bars.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume", "Amount"])
    daily = bars.resample("1D").agg({"Open": "first", "High": "max", "Low": "min",
                                      "Close": "last", "Volume": "sum", "Amount": "sum"})
    daily = daily.dropna(subset=["Open"])
    daily.index = daily.index.date
    return daily


def compute_channel(daily, k=PIVOT_K):
    """Adds upper_line/lower_line columns - the sloped 2-point trendline
    through the most recent two swing highs/lows CONFIRMED as of that
    day, evaluated at that day's bar position. NaN until two swing points
    of that type have been confirmed."""
    high = daily["High"].to_numpy()
    low = daily["Low"].to_numpy()
    n = len(high)

    raw_high = np.zeros(n, dtype=bool)
    raw_low = np.zeros(n, dtype=bool)
    for i in range(k, n - k):
        window_high = high[i - k:i + k + 1]
        window_low = low[i - k:i + k + 1]
        if high[i] == window_high.max():
            raw_high[i] = True
        if low[i] == window_low.min():
            raw_low[i] = True

    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    sh_pos, sh_val, sl_pos, sl_val = [], [], [], []

    for t in range(n):
        confirm_idx = t - k
        if confirm_idx >= 0:
            if raw_high[confirm_idx]:
                sh_pos.append(confirm_idx)
                sh_val.append(high[confirm_idx])
            if raw_low[confirm_idx]:
                sl_pos.append(confirm_idx)
                sl_val.append(low[confirm_idx])
        if len(sh_pos) >= 2 and sh_pos[-1] != sh_pos[-2]:
            slope = (sh_val[-1] - sh_val[-2]) / (sh_pos[-1] - sh_pos[-2])
            upper[t] = sh_val[-1] + slope * (t - sh_pos[-1])
        if len(sl_pos) >= 2 and sl_pos[-1] != sl_pos[-2]:
            slope = (sl_val[-1] - sl_val[-2]) / (sl_pos[-1] - sl_pos[-2])
            lower[t] = sl_val[-1] + slope * (t - sl_pos[-1])

    daily = daily.copy()
    daily["upper_line"] = upper
    daily["lower_line"] = lower
    return daily


def net_return(entry_price, exit_price, shares):
    cost = entry_price * shares * (1 + FEE_RATE)
    proceeds = exit_price * shares * (1 - FEE_RATE - TAX_RATE_NORMAL_SELL)
    pnl = proceeds - cost
    return pnl, pnl / cost


def shares_to_buy(price, remaining_budget):
    cost = price * ORDER_LOT_SHARES
    return ORDER_LOT_SHARES if cost <= remaining_budget else 0


def run(stock_data, names, eval_dates, use_circuit_breaker=False):
    """use_circuit_breaker: pause new entries portfolio-wide whenever the
    last STREAK_LOOKBACK closed trades' win rate drops below
    STREAK_WIN_RATE_MIN, for PAUSE_DAYS - see the circuit-breaker constants'
    comment for why this is portfolio-wide, not per-stock. Off by default
    so plain run() calls (channel_cv.py, the original backtest) are
    unaffected unless explicitly asked for."""
    open_positions = {}
    trades = []
    budget_skips = 0
    paused_until = None  # a date; new entries blocked while d <= paused_until
    pause_days_skipped = 0

    for d in eval_dates:
        # --- exits: upper-line break (the strategy's own signal), plus a
        # stop-loss/max-hold safety net since a channel that keeps trending
        # against the position could otherwise never trigger a "sell" ---
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

        # --- circuit breaker: re-evaluate whether to pause/resume new
        # entries based on the trailing win rate across ALL stocks'
        # closed trades so far (checked every day, using only trades
        # closed as of today - no look-ahead) ---
        if use_circuit_breaker:
            closed_so_far = [t for t in trades if t["exit_date"] <= d.isoformat()]
            if paused_until is not None and d > paused_until:
                paused_until = None  # pause period elapsed, re-armed
            if paused_until is None and len(closed_so_far) >= STREAK_LOOKBACK:
                recent = closed_so_far[-STREAK_LOOKBACK:]
                recent_win_rate = sum(1 for t in recent if t["net_return"] > 0) / len(recent)
                if recent_win_rate < STREAK_WIN_RATE_MIN:
                    paused_until = d + timedelta(days=PAUSE_DAYS)
            if paused_until is not None:
                pause_days_skipped += 1
                continue  # still checked exits above; just skip new entries today

        # --- entries: lower-line break ---
        running_cost = sum(p["entry_price"] * p["shares"] for p in open_positions.values())
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

    return trades, budget_skips, pause_days_skipped


def summarize(trades):
    if not trades:
        return "沒有任何交易（這段期間沒有股票跌破下緣趨勢線）。"
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
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default="trading/channel_backtest_result.txt")
    parser.add_argument("--circuit-breaker", action="store_true",
                         help=f"pause new entries portfolio-wide for {PAUSE_DAYS} days whenever the last "
                              f"{STREAK_LOOKBACK} closed trades' win rate drops below {STREAK_WIN_RATE_MIN*100:.0f}%")
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
            if len(daily) < 2 * PIVOT_K + 10:
                continue
            stock_data[code] = compute_channel(daily)
        api.logout()
    print(f"{len(stock_data)}/{len(codes)} 檔有足夠的歷史資料（含熱身期）")

    if not stock_data:
        print("no stocks with enough history - aborting")
        return

    all_dates = sorted(set().union(*(set(df.index) for df in stock_data.values())))
    eval_dates = [d for d in all_dates if d >= eval_start]
    print(f"回測區間：{eval_start.isoformat()} ~ {end.isoformat()}（{len(eval_dates)}個交易日）")

    trades, budget_skips, pause_days_skipped = run(stock_data, names, eval_dates,
                                                      use_circuit_breaker=args.circuit_breaker)

    cb_note = f"（啟用熔斷機制：共{pause_days_skipped}個交易日因連續虧損暫停進場）" if args.circuit_breaker else ""
    lines = [
        f"=== 趨勢線通道均值回歸策略（找相對高低點連線，跌破下緣買進/突破上緣賣出）{cb_note}："
        f"{eval_start.isoformat()} ~ {end.isoformat()}（{len(stock_data)}檔股票）===",
        "",
        f"擺動點判定窗口=±{PIVOT_K}天｜停損={STOP_LOSS_PCT*100:.0f}%｜最長持有={MAX_HOLD_DAYS}天｜"
        f"流動性門檻=NT${MIN_TRADE_VALUE_NTD:,.0f}｜預算上限=NT${MAX_TOTAL_POSITION_COST_NTD:,.0f}",
        f"因預算上限被跳過的候選進場次數：{budget_skips}",
        "",
        summarize(trades),
        "",
        "-- 按月份分期檢視（避免單一區間的結果只是運氣）--",
        monthly_breakdown(trades),
        "",
        "-- 交易明細 --",
    ]
    for t in sorted(trades, key=lambda x: x["entry_date"]):
        lines.append(
            f"{t['entry_date']} {t['code']}{t['name']}｜進場@{t['entry_price']:.2f} -> "
            f"{t['exit_date']} 出場@{t['exit_price']:.2f}｜{t['reason']}｜持有{t['held_days']}天｜"
            f"{t['net_return']*100:+.2f}%｜NT${t['net_pnl']:,.0f}"
        )

    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
