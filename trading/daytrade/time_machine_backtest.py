"""Validates time_machine.py's Monte Carlo pre-trade probability estimate
against REAL historical ORB day-trade outcomes - does a higher predicted
"probability of hitting take-profit before stop-loss" actually correlate
with a better real net_return? If not, this is a curiosity, not a filter
worth wiring into daytrade_monitor.py.

Reuses backtest.py's exact replay() output (the real ORB trade log - same
entries/exits daytrade_monitor.py would have made live) rather than
inventing a parallel simulation, so "predicted vs actual" is a fair
comparison of the same trades. For each trade, the Monte Carlo pool is
built ONLY from that stock's minute bars strictly BEFORE the trade's own
entry_date (default: the prior DEFAULT_LOOKBACK_DAYS trading days) - no
look-ahead, same discipline as backtest.py's own watchlist rebuild.

Usage:
    python -m trading.daytrade.time_machine_backtest --days 30
    python -m trading.daytrade.time_machine_backtest --days 45 --codes 2330 2454 3034 ...
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd

import db
from .. import executor
from . import backtest, config, minute_data, time_machine

TRAIN_FRACTION = 0.6
N_BUCKETS = 4


def build_pool_for_trade(bars_by_code, code, entry_date_iso, lookback_days=time_machine.DEFAULT_LOOKBACK_DAYS):
    """This stock's minute bars from the `lookback_days` trading days
    strictly before entry_date_iso - never including the entry day itself
    or anything later."""
    df = bars_by_code.get(code)
    if df is None or df.empty:
        return np.array([])
    entry_date = date.fromisoformat(entry_date_iso)
    prior = df[df.index.normalize() < pd.Timestamp(entry_date)]
    if prior.empty:
        return np.array([])
    prior_days = sorted(set(prior.index.normalize()))[-lookback_days:]
    prior = prior[prior.index.normalize().isin(prior_days)]
    return time_machine.build_return_pool(prior)


def annotate_trades(trades, bars_by_code):
    """Adds predicted_prob_tp/predicted_prob_sl/pool_size to each trade
    dict in place (only for trades where the pool was big enough to
    simulate from) and returns the annotated subset."""
    rng = np.random.default_rng(7)
    annotated = []
    for t in trades:
        pool = build_pool_for_trade(bars_by_code, t["code"], t["entry_date"])
        minutes_remaining = time_machine.minutes_until_flatten(t["entry_time"])
        result = time_machine.simulate_remaining_session(
            t["entry_price"], minutes_remaining, pool, side=t["side"], n_sims=2000, rng=rng
        )
        if result is None:
            continue
        row = dict(t)
        row["predicted_prob_tp"] = result["prob_take_profit_first"]
        row["predicted_prob_sl"] = result["prob_stop_loss_first"]
        row["pool_size"] = len(pool)
        annotated.append(row)
    return annotated


def bucket_report(rows, n_buckets=N_BUCKETS):
    """Sorts by predicted_prob_tp ascending, splits into n_buckets equal
    groups, reports each bucket's mean predicted prob vs mean ACTUAL
    net_return and win rate - the core "does the prediction mean
    anything" check."""
    if not rows:
        return "（沒有足夠樣本）"
    rows = sorted(rows, key=lambda r: r["predicted_prob_tp"])
    n = len(rows)
    lines = []
    for i in range(n_buckets):
        lo = i * n // n_buckets
        hi = (i + 1) * n // n_buckets if i < n_buckets - 1 else n
        chunk = rows[lo:hi]
        if not chunk:
            continue
        mean_pred = sum(r["predicted_prob_tp"] for r in chunk) / len(chunk)
        mean_actual = sum(r["net_return"] for r in chunk) / len(chunk)
        win_rate = sum(1 for r in chunk if r["net_return"] > 0) / len(chunk)
        lines.append(f"  第{i+1}/{n_buckets}組(預測機率低到高) n={len(chunk)}｜"
                      f"平均預測TP機率={mean_pred*100:.1f}%｜實際平均報酬={mean_actual*100:+.2f}%｜"
                      f"實際勝率={win_rate*100:.1f}%")
    return "\n".join(lines)


def spearman_like_correlation(rows):
    """Simple rank correlation between predicted_prob_tp and net_return,
    no scipy dependency (this project's only declared dependency is
    requests) - ranks both arrays and computes Pearson correlation of the
    ranks, which is exactly Spearman's rho."""
    if len(rows) < 3:
        return None
    pred = np.array([r["predicted_prob_tp"] for r in rows])
    actual = np.array([r["net_return"] for r in rows])
    pred_rank = pred.argsort().argsort().astype(float)
    actual_rank = actual.argsort().argsort().astype(float)
    if pred_rank.std() == 0 or actual_rank.std() == 0:
        return 0.0
    return float(np.corrcoef(pred_rank, actual_rank)[0, 1])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--codes", nargs="*", default=None,
                         help="subset of stock codes (default: a liquid ~80-stock sample, not the whole market, "
                              "to keep this validation's fetch time reasonable)")
    parser.add_argument("--db", default=db.DB_PATH)
    parser.add_argument("--out", default="trading/daytrade/time_machine_backtest_result.txt")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    all_codes, names = backtest.load_whole_market(conn)
    conn.close()
    codes = args.codes if args.codes else all_codes[:80]
    print(f"universe: {len(codes)} stocks")

    end = date.today()
    start = end - timedelta(days=args.days + time_machine.DEFAULT_LOOKBACK_DAYS * 2)

    api, live = executor.login(False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        bars_by_code = {}
        for code in codes:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            bars = minute_data.bars_for_range(api, contract, start.isoformat(), end.isoformat())
            if not bars.empty:
                bars_by_code[code] = bars
        api.logout()
    print(f"{len(bars_by_code)}/{len(codes)} stocks had historical minute bars")

    eval_start = end - timedelta(days=args.days)
    eval_bars_by_code = {c: df[df.index.normalize() >= pd.Timestamp(eval_start)] for c, df in bars_by_code.items()}
    eval_bars_by_code = {c: df for c, df in eval_bars_by_code.items() if not df.empty}

    all_days, day_views_by_day, watchlists_by_day = backtest.precompute(eval_bars_by_code, names)
    trades = backtest.replay("orb", all_days, day_views_by_day, watchlists_by_day, names)
    print(f"{len(trades)} 筆真實ORB交易（回測區間：{eval_start.isoformat()} ~ {end.isoformat()}）")

    annotated = annotate_trades(trades, bars_by_code)
    print(f"{len(annotated)}/{len(trades)} 筆有足夠的歷史分鐘資料可以模擬")

    lines = [f"=== Time Machine 驗證：{eval_start.isoformat()} ~ {end.isoformat()}（{len(annotated)}筆可模擬交易）===", ""]

    lines.append("===== 全樣本：依預測機率分組 =====")
    lines.append(bucket_report(annotated))
    corr = spearman_like_correlation(annotated)
    lines.append(f"\n全樣本 Spearman相關係數(預測TP機率 vs 實際報酬)：{corr:+.3f}" if corr is not None else "\n樣本不足計算相關係數")

    sorted_by_date = sorted(annotated, key=lambda r: (r["entry_date"], r["entry_time"]))
    split = int(len(sorted_by_date) * TRAIN_FRACTION)
    train, test = sorted_by_date[:split], sorted_by_date[split:]
    lines.append("\n===== Train/Test 一致性檢驗 =====")
    train_corr = spearman_like_correlation(train)
    test_corr = spearman_like_correlation(test)
    lines.append(f"Train (n={len(train)}) Spearman: {train_corr:+.3f}" if train_corr is not None else "Train: 樣本不足")
    lines.append(f"Test  (n={len(test)}) Spearman: {test_corr:+.3f}" if test_corr is not None else "Test: 樣本不足")
    lines.append("")
    lines.append("-- Train 分組 --")
    lines.append(bucket_report(train))
    lines.append("-- Test 分組 --")
    lines.append(bucket_report(test))

    text = "\n".join(lines)
    print("\n" + text)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
