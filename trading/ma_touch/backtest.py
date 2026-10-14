"""Backtest for the ACTUAL ma_touch production rule (signals.py's
check_entry/check_exit, config.py's thresholds, called directly - not
reimplemented) over real daily OHLCV history. Every test run so far for
this strategy (test_intraday_replay.py, test_top20_volatility.py) was a
SINGLE DAY's minute-by-minute replay - useful for catching the whipsaw
bug, but one day proves nothing about whether this strategy has any real
edge. This mirrors trading/swing_backtest.py's methodology (same fee
model, same "reuse the production signal functions directly" discipline)
against the concept_map universe over a real multi-week window.

No ranking function exists for this strategy (unlike swing's
rank_candidates) - mirrors scan.py exactly: iterate codes in sorted order,
buy whichever qualifying candidates fit the budget, first-come-first-served.

Usage:
    python -m trading.ma_touch.backtest --days 30
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import pandas as pd

from .. import daily_scan, executor, positions
from ..daytrade import minute_data
from . import config, signals

FEE_RATE = 0.001425
TAX_RATE_NORMAL_SELL = 0.003  # normal (non-day-trade) 證交稅 - multi-day holds

WARMUP_DAYS = 60  # extra calendar days fetched before the eval window - MA20
                   # needs 20 trading days (~28 calendar days) plus the
                   # 5-day volume baseline; 60 is a comfortable margin


def daily_ohlcv(api, contract, start, end):
    bars = minute_data.bars_for_range(api, contract, start, end)
    if bars.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume", "Amount"])
    daily = bars.resample("1D").agg({"Open": "first", "High": "max", "Low": "min",
                                      "Close": "last", "Volume": "sum", "Amount": "sum"})
    daily = daily.dropna(subset=["Open"])
    daily.index = daily.index.date
    return daily


def prep(daily):
    daily = daily.copy()
    for p in config.MA_PERIODS:
        daily[f"ma{p}"] = daily["Close"].rolling(p).mean()
    daily["volume_ratio"] = daily["Volume"] / daily["Volume"].shift(1).rolling(5).mean()
    daily["prev_close"] = daily["Close"].shift(1)
    return daily


def net_return(entry_price, exit_price, shares):
    cost = entry_price * shares * (1 + FEE_RATE)
    proceeds = exit_price * shares * (1 - FEE_RATE - TAX_RATE_NORMAL_SELL)
    pnl = proceeds - cost
    return pnl, pnl / cost


def shares_to_buy(price, remaining_budget):
    """Mirrors scan.py's own shares_to_buy() - 1 full lot if it fits, else skip."""
    cost = price * config.ORDER_LOT_SHARES
    return config.ORDER_LOT_SHARES if cost <= remaining_budget else 0


def run(stock_data, names, eval_dates, params=None):
    """params: optional dict overriding any of touch_tolerance/volume_ratio_min/
    min_trade_value_ntd/stop_loss_pct/take_profit_pct/max_hold_days - every
    key defaults to config.py's own value (signals.py's functions already
    accept these as kwargs, so a grid search can vary them here without
    ever touching config.py)."""
    params = params or {}
    entry_kwargs = {k: params[k] for k in ("touch_tolerance", "volume_ratio_min", "min_trade_value_ntd") if k in params}
    exit_kwargs = {k: params[k] for k in ("stop_loss_pct", "take_profit_pct", "max_hold_days") if k in params}

    open_positions = {}
    trades = []
    budget_skips = 0

    for d in eval_dates:
        # --- exits ---
        for code in list(open_positions):
            df = stock_data[code]
            if d not in df.index:
                continue
            row = df.loc[d]
            if pd.isna(row["Close"]):
                continue
            pos = open_positions[code]
            held = positions.days_held(pos["entry_date"], as_of=d)
            mas = {p: row[f"ma{p}"] for p in config.MA_PERIODS if pd.notna(row[f"ma{p}"])}
            exit_cond = signals.check_exit(pos["entry_price"], row["Close"], held, mas, **exit_kwargs)
            if exit_cond["exit_signal"]:
                reason = next(k for k in ("support_broken", "stop_loss", "take_profit", "max_hold_days")
                              if exit_cond[k])
                pnl, net_ret = net_return(pos["entry_price"], row["Close"], pos["shares"])
                trades.append({"code": code, "name": pos["name"], "entry_date": pos["entry_date"],
                                "exit_date": d.isoformat(), "entry_price": pos["entry_price"],
                                "exit_price": row["Close"], "reason": reason, "held_days": held,
                                "net_return": net_ret, "net_pnl": pnl})
                del open_positions[code]

        # --- entries (no ranking - sorted-code order, same as scan.py) ---
        running_cost = sum(p["entry_price"] * p["shares"] for p in open_positions.values())
        for code, df in stock_data.items():
            if code in open_positions or d not in df.index:
                continue
            row = df.loc[d]
            if pd.isna(row["Close"]) or pd.isna(row["volume_ratio"]) or pd.isna(row["Amount"]) \
                    or pd.isna(row["prev_close"]):
                continue
            mas = {p: row[f"ma{p}"] for p in config.MA_PERIODS if pd.notna(row[f"ma{p}"])}
            if not mas:
                continue
            cond = signals.check_entry(row["prev_close"], row["Close"], mas, row["volume_ratio"], row["Amount"],
                                        **entry_kwargs)
            if not cond["entry_signal"]:
                continue
            shares = shares_to_buy(row["Close"], config.MAX_TOTAL_POSITION_COST_NTD - running_cost)
            if shares < 1:
                budget_skips += 1
                continue
            open_positions[code] = {"name": names.get(code, code), "entry_price": row["Close"],
                                     "shares": shares, "entry_date": d.isoformat()}
            running_cost += row["Close"] * shares

    # force-close anything still open at the end of the window
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
        return "沒有任何交易（這段期間沒有股票同時滿足碰觸支撐/站回/量增/流動性四條件）。"
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


def weekly_breakdown(trades):
    if not trades:
        return "（無交易）"
    by_week = {}
    for t in trades:
        wk = date.fromisoformat(t["entry_date"]).isocalendar()[1]
        by_week.setdefault(wk, []).append(t)
    lines = []
    for wk, ts in sorted(by_week.items()):
        n = len(ts)
        wins = sum(1 for t in ts if t["net_return"] > 0)
        avg_ret = sum(t["net_return"] for t in ts) / n
        pnl = sum(t["net_pnl"] for t in ts)
        lines.append(f"  第{wk}週：{n}筆｜勝率{wins}/{n}={wins/n*100:.0f}%｜平均報酬{avg_ret*100:+.2f}%｜損益NT${pnl:,.0f}")
    return "\n".join(lines)


def fetch_universe(codes, fetch_start, end):
    """{code: prepped daily df} for every code with enough history -
    shared by main() and cross_validate.py so the (slow, Shioaji-bound)
    fetch happens identically in both places."""
    api, live = executor.login(False)
    stock_data = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for code in codes:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            daily = daily_ohlcv(api, contract, fetch_start.isoformat(), end.isoformat())
            if len(daily) < 25:  # need 20+ trading days of history before eval can even start
                continue
            stock_data[code] = prep(daily)
        api.logout()
    return stock_data


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="evaluation window in calendar days (default ~1 month)")
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default="trading/ma_touch/backtest_result.txt")
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

    stock_data = fetch_universe(codes, fetch_start, end)
    print(f"{len(stock_data)}/{len(codes)} 檔有足夠的歷史資料（含熱身期）")

    if not stock_data:
        print("no stocks with enough history - aborting")
        return

    all_dates = sorted(set().union(*(set(df.index) for df in stock_data.values())))
    eval_dates = [d for d in all_dates if d >= eval_start]
    print(f"回測區間：{eval_start.isoformat()} ~ {end.isoformat()}（{len(eval_dates)}個交易日）")

    trades, budget_skips = run(stock_data, names, eval_dates)

    lines = [
        f"=== MA5/10/20 支撐/破位策略正式規則回測：{eval_start.isoformat()} ~ {end.isoformat()}（{len(stock_data)}檔股票）===",
        "",
        "沿用正式規則：trading/ma_touch/signals.py check_entry/check_exit，config.py 參數原封不動",
        f"（碰觸容忍度=±{config.MA_TOUCH_TOLERANCE*100:.1f}%｜量比門檻={config.VOLUME_RATIO_MIN}｜"
        f"流動性門檻=NT${config.MIN_TRADE_VALUE_NTD:,.0f}｜停損={config.STOP_LOSS_PCT*100:.0f}%｜"
        f"停利={config.TAKE_PROFIT_PCT*100:.0f}%｜最長持有={config.MAX_HOLD_DAYS}天｜預算上限=NT${config.MAX_TOTAL_POSITION_COST_NTD:,.0f}）",
        f"因預算上限被跳過的候選進場次數：{budget_skips}",
        "",
        summarize(trades),
        "",
        "-- 按週分期檢視（避免單一區間的結果只是運氣）--",
        weekly_breakdown(trades),
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
