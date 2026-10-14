"""Parameter grid search for the MA5/10/20 支撐/破位 strategy, with a
chronological train/test split - NOT a blind "try everything, report
whatever looks best" search. This project already got burned by that
exact trap once (see decisions.json's 2026-10 K棒反轉+交叉驗證finding:
a 136-combo grid search's best-by-train config scored +4.88% avg return
on train, +0.00% on held-out test). Parameters here are selected ONLY on
the TRAIN slice; the number reported as "the result" is the test slice's
performance under that same config, never re-optimized.

Usage:
    python -m trading.ma_touch.cross_validate --days 180
"""
import argparse
import sqlite3
from datetime import date, timedelta
from itertools import product

from .. import daily_scan
from . import backtest

TRAIN_FRACTION = 0.6
MIN_TRADES_TO_CONSIDER = 10  # a combo's train performance only counts if it
# produced at least this many trades - same discipline as the earlier K棒
# reversal grid search, to avoid crowning a config that "won" on 2 lucky trades

GRID = {
    "touch_tolerance": [0.010, 0.015, 0.020],
    "volume_ratio_min": [1.3, 1.5, 1.8],
    "stop_loss_pct": [-0.05, -0.08],
    "take_profit_pct": [0.08, 0.12],
    "max_hold_days": [5, 8],
}


def stats(trades):
    if not trades:
        return {"n": 0, "win_rate": None, "avg_return": None, "total_pnl": 0}
    n = len(trades)
    wins = sum(1 for t in trades if t["net_return"] > 0)
    avg_ret = sum(t["net_return"] for t in trades) / n
    total_pnl = sum(t["net_pnl"] for t in trades)
    return {"n": n, "win_rate": wins / n, "avg_return": avg_ret, "total_pnl": total_pnl}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=180, help="total evaluation window in calendar days")
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default="trading/ma_touch/cross_validate_result.txt")
    args = parser.parse_args()

    import db
    conn = sqlite3.connect(args.db or db.DB_PATH)
    code_concepts, names = daily_scan.load_universe(conn)
    conn.close()
    codes = args.codes if args.codes else sorted(code_concepts.keys())
    print(f"universe: {len(codes)} concept_map stocks")

    eval_start = date.today() - timedelta(days=args.days)
    fetch_start = eval_start - timedelta(days=backtest.WARMUP_DAYS)
    end = date.today()

    stock_data = backtest.fetch_universe(codes, fetch_start, end)
    print(f"{len(stock_data)}/{len(codes)} 檔有足夠的歷史資料（含熱身期）")
    if not stock_data:
        print("no stocks with enough history - aborting")
        return

    all_dates = sorted(set().union(*(set(df.index) for df in stock_data.values())))
    eval_dates = [d for d in all_dates if d >= eval_start]
    split_idx = int(len(eval_dates) * TRAIN_FRACTION)
    train_dates, test_dates = eval_dates[:split_idx], eval_dates[split_idx:]
    print(f"回測區間：{eval_start.isoformat()} ~ {end.isoformat()}（{len(eval_dates)}個交易日）")
    print(f"train: {train_dates[0]} ~ {train_dates[-1]} ({len(train_dates)}天) / "
          f"test: {test_dates[0]} ~ {test_dates[-1]} ({len(test_dates)}天)")

    keys = list(GRID.keys())
    combos = list(product(*(GRID[k] for k in keys)))
    print(f"grid: {len(combos)} 組參數組合，只用train切片評分")

    eligible = []
    for values in combos:
        params = dict(zip(keys, values))
        train_trades, _ = backtest.run(stock_data, names, train_dates, params=params)
        s = stats(train_trades)
        if s["n"] >= MIN_TRADES_TO_CONSIDER:
            eligible.append((params, s))

    print(f"{len(eligible)}/{len(combos)} 組在train上達到最低交易次數門檻({MIN_TRADES_TO_CONSIDER}筆)")

    lines = [
        f"=== MA5/10/20 支撐/破位 參數交叉驗證：{eval_start.isoformat()} ~ {end.isoformat()}（{len(stock_data)}檔股票）===",
        "",
        f"Train: {train_dates[0]} ~ {train_dates[-1]}（{len(train_dates)}天）",
        f"Test:  {test_dates[0]} ~ {test_dates[-1]}（{len(test_dates)}天）",
        f"Grid: {len(combos)}組參數組合（touch_tolerance/volume_ratio_min/stop_loss_pct/take_profit_pct/max_hold_days）",
        f"{len(eligible)}組在train上交易次數達到門檻({MIN_TRADES_TO_CONSIDER}筆)，只從這些裡面選最佳參數",
        "",
    ]

    # baseline: current config.py defaults, no override
    base_train, _ = backtest.run(stock_data, names, train_dates, params={})
    base_test, _ = backtest.run(stock_data, names, test_dates, params={})
    bs_train, bs_test = stats(base_train), stats(base_test)
    lines.append("-- 現行config.py預設參數（對照組，未經grid search挑選）--")
    lines.append(f"  Train: {bs_train['n']}筆｜勝率{(bs_train['win_rate'] or 0)*100:.1f}%｜"
                  f"平均報酬{(bs_train['avg_return'] or 0)*100:+.2f}%｜損益NT${bs_train['total_pnl']:,.0f}")
    lines.append(f"  Test:  {bs_test['n']}筆｜勝率{(bs_test['win_rate'] or 0)*100:.1f}%｜"
                  f"平均報酬{(bs_test['avg_return'] or 0)*100:+.2f}%｜損益NT${bs_test['total_pnl']:,.0f}")
    lines.append("")

    if not eligible:
        lines.append("沒有任何參數組合在train上達到最低交易次數門檻 - 無法挑選，直接結束。")
    else:
        eligible.sort(key=lambda item: item[1]["avg_return"], reverse=True)
        best_params, best_train_stats = eligible[0]
        lines.append("-- Train上表現最好的參數組合（只看這個，不要被其他組合誘惑）--")
        lines.append(f"  參數: {best_params}")
        lines.append(f"  Train: {best_train_stats['n']}筆｜勝率{best_train_stats['win_rate']*100:.1f}%｜"
                      f"平均報酬{best_train_stats['avg_return']*100:+.2f}%｜損益NT${best_train_stats['total_pnl']:,.0f}")

        test_trades, _ = backtest.run(stock_data, names, test_dates, params=best_params)
        test_s = stats(test_trades)
        lines.append("")
        lines.append("-- 同一組參數在Test（held-out，從未用來選參數）上的真實表現 --")
        if test_s["n"] == 0:
            lines.append("  Test: 0筆交易 - 這組參數在held-out期間完全沒有觸發過進場訊號")
        else:
            lines.append(f"  Test: {test_s['n']}筆｜勝率{test_s['win_rate']*100:.1f}%｜"
                          f"平均報酬{test_s['avg_return']*100:+.2f}%｜損益NT${test_s['total_pnl']:,.0f}")
        lines.append("")
        lines.append("-- 判讀 --")
        if test_s["n"] >= MIN_TRADES_TO_CONSIDER and test_s["avg_return"] is not None and test_s["avg_return"] > 0:
            lines.append("Test仍是正報酬，且樣本量足夠 - 這組參數的優勢比較不像是純粹在train上碰到的噪音。"
                          "但仍建議累積更多歷史資料後再次驗證，不要只因為這一次test是正的就直接上線。")
        else:
            lines.append("Test轉負、或樣本量不足 - train上看起來最好的參數組合沒有通過held-out驗證，"
                          "這正是過度配適的訊號：在train切片上調參數調出來的「最佳解」，換到沒看過的"
                          "資料就站不住。不建議採用這組參數，也不建議再繼續調更多參數組合找「這次剛好"
                          "test也是正的」的設定 - 那只是用更大的grid換取更高的巧合機率。")

    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
