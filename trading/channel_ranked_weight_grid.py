"""Diagnostic follow-up to channel_ranked_cv.py's inconclusive result (the
blended composite looked much better than baseline in a continuous run and
3/4 independent periods, but WORSE than baseline in a train/test holdout
split - contradictory signals). Isolates which of the three factors
(fundamentals/industry/chip) is actually contributing anything real by
testing each ALONE (weight 1.0 on just that factor) plus the equal-weight
and the original "experience" guess, all through the SAME train(60%)/test
(40%) split channel_ranked_cv.py uses. A factor that doesn't show the same
sign in both halves is noise for that factor, not a tuning problem.

Usage:
    python -m trading.channel_ranked_weight_grid --days 250
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

from . import channel_ranked_backtest as cr
from . import daily_scan, executor, swing_backtest
from .channel_backtest import PIVOT_K, WARMUP_DAYS, compute_channel, daily_ohlcv

TRAIN_FRACTION = 0.6

WEIGHT_GRID = [
    ("fundamentals-only", {"fundamentals": 1.0, "industry": 0.0, "chip": 0.0}),
    ("industry-only", {"fundamentals": 0.0, "industry": 1.0, "chip": 0.0}),
    ("chip-only", {"fundamentals": 0.0, "industry": 0.0, "chip": 1.0}),
    ("equal", {"fundamentals": 1 / 3, "industry": 1 / 3, "chip": 1 / 3}),
    ("經驗guess(0.4/0.25/0.35)", cr.DEFAULT_WEIGHTS),
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
    return f"{s['n']}筆｜勝率{s['win_rate']*100:.1f}%｜損益NT${s['total_pnl']:,.0f}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=250)
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default="trading/channel_ranked_weight_grid_result.txt")
    args = parser.parse_args()

    import db
    conn = sqlite3.connect(args.db or db.DB_PATH)
    code_concepts, names = daily_scan.load_universe(conn)
    fundamentals = cr.load_fundamentals(conn)
    industry_map = cr.load_industry_map(conn)
    inst_flow = swing_backtest.load_institutional_flow(conn, flow_days=cr.INSTITUTIONAL_FLOW_DAYS)
    conn.close()
    codes = args.codes if args.codes else sorted(code_concepts.keys())

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
    split_idx = int(len(eval_dates) * TRAIN_FRACTION)
    train_dates, test_dates = eval_dates[:split_idx], eval_dates[split_idx:]

    lines = [f"=== 排序因子拆解：各因子單獨測試（train/test），{eval_start.isoformat()} ~ {end.isoformat()} ===", ""]
    for label, weights in WEIGHT_GRID:
        train_trades, _ = cr.run(stock_data, names, train_dates, fundamentals=fundamentals,
                                  industry_trend=industry_trend, inst_flow=inst_flow,
                                  weights=weights, rank_mode=True)
        test_trades, _ = cr.run(stock_data, names, test_dates, fundamentals=fundamentals,
                                 industry_trend=industry_trend, inst_flow=inst_flow,
                                 weights=weights, rank_mode=True)
        ts, tt = stats(train_trades), stats(test_trades)
        agree = tt["n"] > 0 and ts["n"] > 0 and (tt["total_pnl"] > 0) == (ts["total_pnl"] > 0)
        line = f"[{label}] train: {fmt(ts)} / test: {fmt(tt)}" + ("  <- 同方向" if agree else "")
        lines.append(line)
        print(line)

    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
