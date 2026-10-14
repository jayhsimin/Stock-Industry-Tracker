"""Backtest for breakout_signals.py's true/false breakout entry rule
(daily bars) against the swing strategy's existing stop-loss/take-profit/
max-hold exit rules (trading/signals.py's check_exit, theme_weakened
condition omitted here since this isolates the NEW entry rule, not a
full re-test of the existing concept_map-theme exit logic).

Needs full daily OHLCV (not just closes like trading/ma_data.py gives) -
resamples Shioaji kbars itself rather than extending ma_data.py, to avoid
changing that module's existing (working, relied-upon) output shape.

Usage:
    python -m trading.breakout_backtest --days 120
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import pandas as pd

from . import breakout_signals, config, executor, signals
from .daytrade import minute_data

FEE_RATE = 0.001425
TAX_RATE_NORMAL_SELL = 0.003  # normal (not day-trade) 證交稅 - these are
                                # real multi-day holds, not same-day round trips


def daily_ohlcv(api, contract, start, end):
    bars = minute_data.bars_for_range(api, contract, start, end)
    if bars.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    daily = bars.resample("1D").agg({"Open": "first", "High": "max", "Low": "min",
                                      "Close": "last", "Volume": "sum"})
    return daily.dropna(subset=["Open"])


def net_return(entry_price, exit_price, shares):
    cost = entry_price * shares * (1 + FEE_RATE)
    proceeds = exit_price * shares * (1 - FEE_RATE - TAX_RATE_NORMAL_SELL)
    pnl = proceeds - cost
    return pnl, pnl / cost


def replay(daily_df, code, name):
    entry_signal, _ = breakout_signals.true_breakout_entry_signal(daily_df)
    closes = daily_df["Close"]
    trades = []
    open_pos = None
    for i in range(len(daily_df)):
        day = daily_df.index[i]
        close = closes.iloc[i]
        if open_pos is not None:
            held_days = i - open_pos["entry_idx"]
            ret = (close - open_pos["entry_price"]) / open_pos["entry_price"]
            exit_reason = None
            if ret <= config.STOP_LOSS_PCT:
                exit_reason = "stop_loss"
            elif ret >= config.TAKE_PROFIT_PCT:
                exit_reason = "take_profit"
            elif held_days >= config.MAX_HOLD_DAYS:
                exit_reason = "max_hold_days"
            if exit_reason:
                pnl, net_ret = net_return(open_pos["entry_price"], close, open_pos["shares"])
                trades.append({"code": code, "name": name,
                                "entry_date": daily_df.index[open_pos["entry_idx"]].date().isoformat(),
                                "exit_date": day.date().isoformat(), "entry_price": open_pos["entry_price"],
                                "exit_price": close, "reason": exit_reason, "held_days": held_days,
                                "net_return": net_ret, "net_pnl": pnl})
                open_pos = None
            continue
        if bool(entry_signal.iloc[i]):
            open_pos = {"entry_idx": i, "entry_price": close, "shares": config.ORDER_LOT_SHARES}
    if open_pos is not None:
        close = closes.iloc[-1]
        pnl, net_ret = net_return(open_pos["entry_price"], close, open_pos["shares"])
        trades.append({"code": code, "name": name,
                        "entry_date": daily_df.index[open_pos["entry_idx"]].date().isoformat(),
                        "exit_date": daily_df.index[-1].date().isoformat(), "entry_price": open_pos["entry_price"],
                        "exit_price": close, "reason": "end_of_window_safety_net",
                        "held_days": len(daily_df) - 1 - open_pos["entry_idx"],
                        "net_return": net_ret, "net_pnl": pnl})
    return trades


def summarize(trades):
    if not trades:
        return "沒有任何交易（這段期間沒有股票滿足真突破確認條件）。"
    n = len(trades)
    wins = sum(1 for t in trades if t["net_return"] > 0)
    total_pnl = sum(t["net_pnl"] for t in trades)
    avg_ret = sum(t["net_return"] for t in trades) / n
    avg_held = sum(t["held_days"] for t in trades) / n
    return (f"總交易次數：{n}｜勝率：{wins}/{n}={wins/n*100:.1f}%｜"
            f"總損益：NT${total_pnl:,.0f}｜平均報酬：{avg_ret*100:+.2f}%｜平均持有：{avg_held:.1f}天")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=120, help="calendar days back - breakout needs 20+ day history before it can even fire once")
    parser.add_argument("--codes", nargs="*", default=None, help="subset of stock codes (default: full concept_map universe)")
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default="trading/breakout_backtest_result.txt")
    args = parser.parse_args()

    import db
    conn = sqlite3.connect(args.db or db.DB_PATH)
    codes = args.codes if args.codes else [r[0] for r in conn.execute("SELECT DISTINCT stock_id FROM concept_map")]
    names = dict(conn.execute('SELECT "公司代號", "公司簡稱" FROM company_info'))
    conn.close()
    print(f"universe: {len(codes)} concept_map stocks")

    end = date.today()
    start = end - timedelta(days=args.days)

    api, live = executor.login(False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        all_trades = []
        n_with_data = 0
        for code in codes:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            daily = daily_ohlcv(api, contract, start.isoformat(), end.isoformat())
            if len(daily) < 25:
                continue
            n_with_data += 1
            trades = replay(daily, code, names.get(code, code))
            all_trades.extend(trades)
        api.logout()
    print(f"{n_with_data}/{len(codes)} 檔有足夠的歷史資料")

    lines = [
        f"=== 真假突破確認法回測（波段/日線）：{start.isoformat()} ~ {end.isoformat()}（{n_with_data}檔股票）===",
        "",
        summarize(all_trades),
        "",
        "-- 交易明細 --",
    ]
    for t in sorted(all_trades, key=lambda x: x["entry_date"]):
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
