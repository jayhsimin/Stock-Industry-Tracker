"""Validates quiet_strength_backtest.py's "大漲+量縮" strategy against the
same recurring trap this project keeps finding across every other strategy
tested: a single continuous run can look good while being carried by one
lucky stretch, or by budget/position state that doesn't actually repeat
once you reset to a clean slate. Splits the window two ways: a single
chronological train(60%)/test(40%) check, and a 4-independent-period check
(each period run from zero positions/zero budget) to see whether the
statistical study's train/test agreement (price_volume_pattern_study.py)
still holds once real trade-level accounting (fees, one lot at a time, a
shared budget cap, a fixed holding period) is in the picture.

Usage:
    python -m trading.quiet_strength_cv --days 250
    python -m trading.quiet_strength_cv --days 250 --multi-period
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

from . import quiet_strength_backtest as qs
from . import daily_scan, executor

TRAIN_FRACTION = 0.6
N_PERIODS = 4


def stats(trades):
    if not trades:
        return {"n": 0, "win_rate": None, "avg_return": None, "total_pnl": 0}
    n = len(trades)
    wins = sum(1 for t in trades if t["net_return"] > 0)
    avg_ret = sum(t["net_return"] for t in trades) / n
    total_pnl = sum(t["net_pnl"] for t in trades)
    return {"n": n, "win_rate": wins / n, "avg_return": avg_ret, "total_pnl": total_pnl}


def fmt(s):
    if s["n"] == 0:
        return "0筆交易"
    return f"{s['n']}筆｜勝率{s['win_rate']*100:.1f}%｜平均報酬{s['avg_return']*100:+.2f}%｜損益NT${s['total_pnl']:,.0f}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=250)
    parser.add_argument("--hold-days", type=int, default=qs.HOLD_DAYS)
    parser.add_argument("--stop-loss", type=float, default=qs.STOP_LOSS_PCT)
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--multi-period", action="store_true",
                         help=f"split into {N_PERIODS} independent chunks instead of one train/test split")
    parser.add_argument("--out", default="trading/quiet_strength_cv_result.txt")
    args = parser.parse_args()

    import db
    conn = sqlite3.connect(args.db or db.DB_PATH)
    code_concepts, names = daily_scan.load_universe(conn)
    conn.close()
    codes = args.codes if args.codes else sorted(code_concepts.keys())
    print(f"universe: {len(codes)} concept_map stocks")

    eval_start = date.today() - timedelta(days=args.days)
    fetch_start = eval_start - timedelta(days=qs.WARMUP_DAYS)
    end = date.today()

    api, live = executor.login(False)
    stock_data = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for code in codes:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            daily = qs.daily_ohlcv(api, contract, fetch_start.isoformat(), end.isoformat())
            if len(daily) < qs.VOL_AVG_WINDOW + args.hold_days + 10:
                continue
            stock_data[code] = qs.compute_signal(daily)
        api.logout()
    print(f"{len(stock_data)}/{len(codes)} 檔有足夠的歷史資料（含熱身期）")

    all_dates = sorted(set().union(*(set(df.index) for df in stock_data.values())))
    eval_dates = [d for d in all_dates if d >= eval_start]

    if args.multi_period:
        chunk = len(eval_dates) / N_PERIODS
        periods = []
        for i in range(N_PERIODS):
            lo = int(i * chunk)
            hi = int((i + 1) * chunk) if i < N_PERIODS - 1 else len(eval_dates)
            periods.append((f"第{i+1}/{N_PERIODS}段", eval_dates[lo:hi]))
        title = f"{N_PERIODS}段獨立分期驗證"
    else:
        split_idx = int(len(eval_dates) * TRAIN_FRACTION)
        periods = [("Train", eval_dates[:split_idx]), ("Test", eval_dates[split_idx:])]
        title = "Train/Test驗證"

    lines = [f"=== 大漲+量縮策略 {title}：{eval_start.isoformat()} ~ {end.isoformat()}"
             f"（{len(stock_data)}檔股票，持有{args.hold_days}天，停損{args.stop_loss*100:.0f}%）===",
             "", "每段都從零部位、零資金佔用獨立重跑，不是連續跑一次再切開看", ""]

    for label, dates in periods:
        if not dates:
            continue
        trades, _ = qs.run(stock_data, names, dates, hold_days=args.hold_days, stop_loss_pct=args.stop_loss)
        s = stats(trades)
        lines.append(f"-- {label}（{dates[0].isoformat()} ~ {dates[-1].isoformat()}，{len(dates)}天）--")
        lines.append(f"  {fmt(s)}")
        print(f"{label}: {fmt(s)}")
        lines.append("")

    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
