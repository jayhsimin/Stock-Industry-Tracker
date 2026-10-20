"""Validates channel_ranked_backtest.py's multi-factor candidate ranking
against the same recurring trap this project keeps finding: a single
continuous run can look better (or worse) by chance. Compares three modes -
baseline (unranked, channel_backtest.py's current behavior), ranked
(composite-score priority, unlimited entries/day), and ranked+top1 (only
ever buy the single best-scored candidate each day) - across a chronological
train(60%)/test(40%) split and a 4-independent-period split (each period run
from zero positions/zero budget).

Remember the fundamentals-factor caveat from channel_ranked_backtest.py's
docstring: monthly_revenue has no historical depth, so "ranked" here is
really only testing the industry+chip factors' contribution honestly; the
fundamentals contribution is backtest-optimistic by construction.

Usage:
    python -m trading.channel_ranked_cv --days 250
    python -m trading.channel_ranked_cv --days 250 --multi-period
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

from . import channel_ranked_backtest as cr
from . import daily_scan, executor, swing_backtest
from .channel_backtest import PIVOT_K, WARMUP_DAYS, compute_channel, daily_ohlcv

TRAIN_FRACTION = 0.6
N_PERIODS = 4

MODES = [
    ("baseline(unranked)", dict(rank_mode=False, max_new_entries_per_day=None)),
    ("ranked", dict(rank_mode=True, max_new_entries_per_day=None)),
    ("ranked+top1/day", dict(rank_mode=True, max_new_entries_per_day=1)),
]


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
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--multi-period", action="store_true",
                         help=f"split into {N_PERIODS} independent chunks instead of one train/test split")
    parser.add_argument("--out", default="trading/channel_ranked_cv_result.txt")
    args = parser.parse_args()

    import db
    conn = sqlite3.connect(args.db or db.DB_PATH)
    code_concepts, names = daily_scan.load_universe(conn)
    fundamentals = cr.load_fundamentals(conn)
    industry_map = cr.load_industry_map(conn)
    inst_flow = swing_backtest.load_institutional_flow(conn, flow_days=cr.INSTITUTIONAL_FLOW_DAYS)
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

    industry_trend = cr.compute_industry_trend(stock_data, industry_map)

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

    lines = [f"=== 通道策略候選排序 {title}：{eval_start.isoformat()} ~ {end.isoformat()}（{len(stock_data)}檔股票）===",
             "", "每段都從零部位、零資金佔用獨立重跑；三種模式共用同一段資料/同一組fundamentals快照", ""]

    for label, dates in periods:
        if not dates:
            continue
        lines.append(f"-- {label}（{dates[0].isoformat()} ~ {dates[-1].isoformat()}，{len(dates)}天）--")
        print(f"-- {label} --")
        for mode_name, mode_kwargs in MODES:
            trades, skips = cr.run(stock_data, names, dates, fundamentals=fundamentals,
                                    industry_trend=industry_trend, inst_flow=inst_flow, **mode_kwargs)
            s = stats(trades)
            lines.append(f"  [{mode_name}] {fmt(s)}（budget_skips={skips}）")
            print(f"  [{mode_name}] {fmt(s)}（budget_skips={skips}）")
        lines.append("")

    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
