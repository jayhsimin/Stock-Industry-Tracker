"""Cross-validated grid search for a ~1-month-hold swing strategy driven
by 型態訊號 (pattern signals): candlestick reversal (hammer/bullish
engulfing) and true/false breakout confirmation, both from
trading/breakout_signals.py, evaluated against a grid of exit parameters
(stop-loss/take-profit/max-hold-days around ~1 month).

Proper discipline, because this project has repeatedly found small/in-
sample results reverse at scale (see trading/decisions.json's 真假突破
confirmation entry): split the historical window chronologically into
TRAIN (parameter selection) and TEST (out-of-sample, frozen parameters).
The reported "best" result is the TEST performance of whichever config
scored best on TRAIN - never pick based on TEST, that's the whole point.

Usage:
    python -m trading.swing_pattern_cv --days 250
"""
import argparse
import itertools
import sqlite3
import warnings
from datetime import date, timedelta

import pandas as pd

from . import breakout_signals, config, executor
from .daytrade import minute_data

FEE_RATE = 0.001425
TAX_RATE_NORMAL_SELL = 0.003
MIN_TRADES_TO_CONSIDER = 10  # a config with fewer train trades than this
                               # is too thin a sample to trust - skip it
                               # rather than let a lucky handful of trades win


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


def replay(daily_df, entry_signal, code, name, stop_loss_pct, take_profit_pct, max_hold_days,
           date_range=None):
    """date_range: (start_date, end_date) inclusive - only ENTRIES within
    this range count (an entry just before the boundary can still exit
    after it - that's a real trade, not excluded, just not double-counted
    across folds)."""
    closes = daily_df["Close"]
    trades = []
    open_pos = None
    for i in range(len(daily_df)):
        day = daily_df.index[i].date()
        close = closes.iloc[i]
        if open_pos is not None:
            held_days = i - open_pos["entry_idx"]
            ret = (close - open_pos["entry_price"]) / open_pos["entry_price"]
            reason = None
            if ret <= stop_loss_pct:
                reason = "stop_loss"
            elif ret >= take_profit_pct:
                reason = "take_profit"
            elif held_days >= max_hold_days:
                reason = "max_hold_days"
            if reason:
                pnl, net_ret = net_return(open_pos["entry_price"], close, config.ORDER_LOT_SHARES)
                trades.append({"code": code, "name": name, "entry_date": open_pos["entry_date"],
                                "exit_date": day.isoformat(), "reason": reason, "held_days": held_days,
                                "net_return": net_ret, "net_pnl": pnl})
                open_pos = None
            continue
        if date_range and not (date_range[0] <= day <= date_range[1]):
            continue
        if bool(entry_signal.iloc[i]):
            open_pos = {"entry_idx": i, "entry_price": close, "entry_date": day.isoformat()}
    return trades


def summarize(trades):
    if not trades:
        return {"n": 0, "win_rate": 0, "total_pnl": 0, "avg_return": 0}
    n = len(trades)
    wins = sum(1 for t in trades if t["net_return"] > 0)
    return {
        "n": n, "win_rate": wins / n, "total_pnl": sum(t["net_pnl"] for t in trades),
        "avg_return": sum(t["net_return"] for t in trades) / n,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=250)
    parser.add_argument("--train-frac", type=float, default=0.6)
    parser.add_argument("--codes", nargs="*", default=None, help="subset of stock codes (default: full concept_map universe)")
    parser.add_argument("--out", default="trading/swing_pattern_cv_result.txt")
    args = parser.parse_args()

    import db
    conn = sqlite3.connect(db.DB_PATH)
    codes = args.codes if args.codes else [r[0] for r in conn.execute("SELECT DISTINCT stock_id FROM concept_map")]
    names = dict(conn.execute('SELECT "公司代號", "公司簡稱" FROM company_info'))
    conn.close()
    print(f"universe: {len(codes)} concept_map stocks")

    end = date.today()
    start = end - timedelta(days=args.days)

    api, live = executor.login(False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        daily_by_code = {}
        for code in codes:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            daily = daily_ohlcv(api, contract, start.isoformat(), end.isoformat())
            if len(daily) >= 30:
                daily_by_code[code] = daily
        api.logout()
    print(f"{len(daily_by_code)}/{len(codes)} 檔有足夠的歷史資料")

    all_days = sorted(set().union(*(set(df.index.date) for df in daily_by_code.values())))
    split_idx = int(len(all_days) * args.train_frac)
    train_range = (all_days[0], all_days[split_idx - 1])
    test_range = (all_days[split_idx], all_days[-1])
    print(f"訓練期：{train_range[0]} ~ {train_range[1]}（{split_idx}天）")
    print(f"測試期：{test_range[0]} ~ {test_range[1]}（{len(all_days)-split_idx}天）")

    # --- entry-signal grid: precompute once per entry config, reused across every exit config ---
    entry_configs = [("reversal", {})]
    for atr_mult, vol_min, space, confirm in itertools.product([0.5, 1.0], [1.5, 2.0], [0.01, 0.02], [1, 2]):
        entry_configs.append(("true_breakout", {
            "atr_multiplier": atr_mult, "volume_ratio_min": vol_min,
            "space_pct": space, "confirm_days": confirm,
        }))
    print(f"進場參數組合：{len(entry_configs)}（1種反轉型態 + {len(entry_configs)-1}種真假突破參數）")

    exit_configs = list(itertools.product([-0.08, -0.10], [0.15, 0.20], [18, 22]))
    print(f"出場參數組合：{len(exit_configs)}")

    entry_signals_by_config = {}
    for pattern, params in entry_configs:
        key = (pattern, tuple(sorted(params.items())))
        entry_signals_by_config[key] = {}
        for code, df in daily_by_code.items():
            if pattern == "reversal":
                sig = breakout_signals.candlestick_reversal_entry_signal(df)
            else:
                sig, _ = breakout_signals.true_breakout_entry_signal(df, **params)
            entry_signals_by_config[key][code] = sig

    results = []
    for (pattern, params_tuple), sig_by_code in entry_signals_by_config.items():
        params = dict(params_tuple)
        for stop_loss_pct, take_profit_pct, max_hold_days in exit_configs:
            train_trades, test_trades = [], []
            for code, df in daily_by_code.items():
                sig = sig_by_code[code]
                name = names.get(code, code)
                train_trades += replay(df, sig, code, name, stop_loss_pct, take_profit_pct,
                                        max_hold_days, date_range=train_range)
                test_trades += replay(df, sig, code, name, stop_loss_pct, take_profit_pct,
                                       max_hold_days, date_range=test_range)
            train_stats = summarize(train_trades)
            test_stats = summarize(test_trades)
            results.append({
                "pattern": pattern, "params": params,
                "stop_loss_pct": stop_loss_pct, "take_profit_pct": take_profit_pct,
                "max_hold_days": max_hold_days,
                "train": train_stats, "test": test_stats, "test_trades": test_trades,
            })

    # select by TRAIN avg_return among configs with enough train trades -
    # NEVER select by test performance
    eligible = [r for r in results if r["train"]["n"] >= MIN_TRADES_TO_CONSIDER]
    eligible.sort(key=lambda r: r["train"]["avg_return"], reverse=True)

    lines = [
        f"=== 型態訊號波段策略交叉驗證：{start.isoformat()} ~ {end.isoformat()}"
        f"（{len(daily_by_code)}檔股票，{len(entry_configs)}種進場參數 x {len(exit_configs)}種出場參數"
        f" = {len(results)}種組合）===",
        f"訓練期：{train_range[0]} ~ {train_range[1]}｜測試期：{test_range[0]} ~ {test_range[1]}",
        f"（選參數只看訓練期表現，測試期數字是真正的驗證結果，不是用來挑參數的）",
        "",
        f"-- 訓練期表現前10名（決定參數用，{len(eligible)}/{len(results)}種組合有足夠樣本>={MIN_TRADES_TO_CONSIDER}筆）--",
    ]
    for r in eligible[:10]:
        lines.append(
            f"{r['pattern']} {r['params']}｜停損{r['stop_loss_pct']*100:.0f}%/停利{r['take_profit_pct']*100:.0f}%/"
            f"持有{r['max_hold_days']}天｜訓練:{r['train']['n']}筆 勝率{r['train']['win_rate']*100:.1f}% "
            f"平均{r['train']['avg_return']*100:+.2f}%"
        )

    if eligible:
        best = eligible[0]
        lines += [
            "",
            "-- 最佳參數（依訓練期選出）在測試期的真實表現 --",
            f"型態：{best['pattern']} {best['params']}",
            f"出場：停損{best['stop_loss_pct']*100:.0f}% / 停利{best['take_profit_pct']*100:.0f}% / "
            f"最長持有{best['max_hold_days']}天",
            f"訓練期：{best['train']['n']}筆｜勝率{best['train']['win_rate']*100:.1f}%｜"
            f"平均報酬{best['train']['avg_return']*100:+.2f}%｜總損益NT${best['train']['total_pnl']:,.0f}",
            f"測試期（樣本外真實驗證）：{best['test']['n']}筆｜勝率{best['test']['win_rate']*100:.1f}%｜"
            f"平均報酬{best['test']['avg_return']*100:+.2f}%｜總損益NT${best['test']['total_pnl']:,.0f}",
            "",
            "-- 測試期交易明細 --",
        ]
        for t in sorted(best["test_trades"], key=lambda x: x["entry_date"]):
            lines.append(f"{t['entry_date']} {t['code']}{t['name']} -> {t['exit_date']}｜{t['reason']}｜"
                         f"持有{t['held_days']}天｜{t['net_return']*100:+.2f}%｜NT${t['net_pnl']:,.0f}")
    else:
        lines.append("沒有任何組合在訓練期累積到足夠的樣本數。")

    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
