"""Exploratory statistics, NOT a trading strategy - user-prompted research
question: "用歷史股價上漲下跌時的價量或漲幅，看有沒有規律性或統計上可以穩定
賺錢的交易區間". This script buckets every historical stock-day by
(prior-day %change, volume-vs-its-own-20d-average ratio) and measures the
FORWARD return distribution (1/3/5/10 trading days later) inside each
bucket - i.e. "after a day like this, what tends to happen next, and is
that pattern strong/consistent enough to be more than noise."

No entries/exits/positions/budget here (unlike channel_backtest.py,
swing_backtest.py etc.) - this is pure descriptive statistics over the
whole concept_map universe, meant to surface CANDIDATE patterns. Anything
that looks promising here would still need its own isolated
signals.py/backtest with proper trade-level P&L before being treated as a
strategy - this script's job is only to narrow down whether that's worth
doing at all.

Anti-overfitting discipline (same one used all session for every other
strategy): the full-window bucket table is advisory only. A bucket is
only reported as a real candidate if ITS SIGN AND SIGNIFICANCE AGREE
across a chronological train/test split - a bucket that looks great in
one half and reverses in the other is noise, not a pattern, no matter how
good the full-sample number looks.

Usage:
    python -m trading.price_volume_pattern_study --days 250
    python -m trading.price_volume_pattern_study --days 250 --horizon 5
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import daily_scan, executor
from .channel_backtest import daily_ohlcv

FEE_RATE = 0.001425
TAX_RATE_NORMAL_SELL = 0.003
ROUND_TRIP_COST = FEE_RATE * 2 + TAX_RATE_NORMAL_SELL  # ~0.00585, buy+sell fee + sell tax

VOL_AVG_WINDOW = 20   # trailing days used as each stock's own "normal volume" baseline
WARMUP_DAYS = 40       # extra calendar days fetched so VOL_AVG_WINDOW is full from day 1
HORIZONS = (1, 3, 5, 10)
MIN_BUCKET_N = 30      # below this, a bucket's stats aren't reported at all

CHANGE_BOUNDS = [(-np.inf, -0.05), (-0.05, -0.02), (-0.02, -0.005),
                 (-0.005, 0.005), (0.005, 0.02), (0.02, 0.05), (0.05, np.inf)]
CHANGE_LABELS = ["<=-5%", "-5~-2%", "-2~-0.5%", "-0.5~+0.5%", "+0.5~+2%", "+2~+5%", ">=+5%"]

VOL_BOUNDS = [(0, 0.7), (0.7, 1.2), (1.2, 2.0), (2.0, 3.0), (3.0, np.inf)]
VOL_LABELS = ["<0.7x", "0.7-1.2x", "1.2-2x", "2-3x", ">=3x"]


def build_features(daily):
    """daily: DataFrame indexed by date with Close/Volume (from channel_backtest.daily_ohlcv).
    Adds pct_change, vol_ratio (today's volume / trailing 20d average, baseline
    EXCLUDES today so there's no look-ahead), and fwd_<h> forward returns."""
    df = daily.copy()
    df["pct_change"] = df["Close"].pct_change()
    df["vol_avg"] = df["Volume"].rolling(VOL_AVG_WINDOW).mean().shift(1)
    df["vol_ratio"] = df["Volume"] / df["vol_avg"]
    for h in HORIZONS:
        df[f"fwd_{h}"] = df["Close"].shift(-h) / df["Close"] - 1
    return df


def collect_rows(stock_data):
    """{code: features_df} -> one flat DataFrame, one row per stock-day with a
    usable pct_change/vol_ratio (date kept so callers can do a chronological split).

    Also adds excess_<h> = fwd_<h> minus that DAY's cross-sectional average
    fwd_<h> across the whole universe - raw fwd_<h> is dominated by whatever
    the whole universe (concept_map's largely AI-theme stocks) was doing over
    the window (e.g. one long bull run makes almost every bucket look
    "positive" regardless of the bucket's own condition); excess_<h> asks the
    sharper question "did THIS condition beat the average stock that same
    day", which is what actually isolates a bucket-specific effect from
    generic market/theme drift."""
    rows = []
    for code, df in stock_data.items():
        valid = df.dropna(subset=["pct_change", "vol_ratio"])
        for dt, r in valid.iterrows():
            row = {"code": code, "date": dt, "pct_change": r["pct_change"], "vol_ratio": r["vol_ratio"]}
            for h in HORIZONS:
                row[f"fwd_{h}"] = r[f"fwd_{h}"]
            rows.append(row)
    rows = pd.DataFrame(rows)
    for h in HORIZONS:
        benchmark = rows.groupby("date")[f"fwd_{h}"].transform("mean")
        rows[f"excess_{h}"] = rows[f"fwd_{h}"] - benchmark
    return rows


def bucket_table(rows, col, min_n=MIN_BUCKET_N):
    """change_bucket x vol_bucket -> n / mean of `col` (a fwd_<h> or excess_<h>
    column) / win rate / t-stat / net mean after round-trip cost (FEE*2 + sell
    tax) - i.e. "would this actually have been worth trading, not just
    directionally right." """
    sub_all = rows.dropna(subset=[col])
    results = []
    for clabel, (clo, chi) in zip(CHANGE_LABELS, CHANGE_BOUNDS):
        for vlabel, (vlo, vhi) in zip(VOL_LABELS, VOL_BOUNDS):
            mask = (sub_all["pct_change"] >= clo) & (sub_all["pct_change"] < chi) \
                 & (sub_all["vol_ratio"] >= vlo) & (sub_all["vol_ratio"] < vhi)
            sub = sub_all.loc[mask, col]
            n = len(sub)
            if n < min_n:
                continue
            mean = sub.mean()
            std = sub.std()
            results.append({
                "change_bucket": clabel, "vol_bucket": vlabel, "n": n,
                "mean_fwd_return": mean,
                "win_rate": (sub > 0).mean(),
                "t_stat": mean / (std / np.sqrt(n)) if std > 0 else 0.0,
                "net_mean_after_cost": mean - ROUND_TRIP_COST,
            })
    cols = ["change_bucket", "vol_bucket", "n", "mean_fwd_return", "win_rate", "t_stat", "net_mean_after_cost"]
    return pd.DataFrame(results, columns=cols)


def univariate_table(rows, col, by, bounds, labels, min_n=MIN_BUCKET_N):
    """Same stats but bucketed on only ONE axis (by='pct_change' or 'vol_ratio') -
    easier to read than the 2D grid, useful as a first-pass sanity check."""
    sub_all = rows.dropna(subset=[col])
    results = []
    for label, (lo, hi) in zip(labels, bounds):
        sub = sub_all.loc[(sub_all[by] >= lo) & (sub_all[by] < hi), col]
        n = len(sub)
        if n < min_n:
            continue
        mean = sub.mean()
        std = sub.std()
        results.append({
            "bucket": label, "n": n, "mean_fwd_return": mean,
            "win_rate": (sub > 0).mean(),
            "t_stat": mean / (std / np.sqrt(n)) if std > 0 else 0.0,
            "net_mean_after_cost": mean - ROUND_TRIP_COST,
        })
    return pd.DataFrame(results, columns=["bucket", "n", "mean_fwd_return", "win_rate", "t_stat", "net_mean_after_cost"])


def chronological_split(rows, frac=0.6):
    dates = sorted(rows["date"].unique())
    cut = dates[int(len(dates) * frac)]
    return rows[rows["date"] < cut], rows[rows["date"] >= cut]


def consistent_candidates(rows, col, min_n=MIN_BUCKET_N, min_abs_t=2.0):
    """The ONLY output that should be acted on: buckets whose (change, volume)
    combination has |t| >= min_abs_t AND the SAME SIGN in both the train half
    and the test half of a chronological split. Anything else is reported in
    the full-sample tables for visibility but explicitly not trusted."""
    train, test = chronological_split(rows)
    train_tab = bucket_table(train, col, min_n=min_n // 2 or 1)
    test_tab = bucket_table(test, col, min_n=min_n // 2 or 1)
    merged = train_tab.merge(test_tab, on=["change_bucket", "vol_bucket"], suffixes=("_train", "_test"))
    if merged.empty:
        return merged
    agree = (
        (merged["t_stat_train"].abs() >= min_abs_t) &
        (merged["t_stat_test"].abs() >= min_abs_t) &
        (np.sign(merged["mean_fwd_return_train"]) == np.sign(merged["mean_fwd_return_test"]))
    )
    return merged[agree].sort_values("t_stat_test", key=abs, ascending=False)


def format_table(df, title):
    if df.empty:
        return f"-- {title} --\n（沒有樣本數 >= {MIN_BUCKET_N} 的區間）\n"
    lines = [f"-- {title} --"]
    for _, r in df.iterrows():
        bucket = f"{r.get('change_bucket', r.get('bucket', ''))}" + (f" x {r['vol_bucket']}" if "vol_bucket" in r else "")
        lines.append(
            f"  {bucket:<22} n={int(r['n']):>5}  平均遠期報酬={r['mean_fwd_return']*100:+6.2f}%  "
            f"勝率={r['win_rate']*100:5.1f}%  t={r['t_stat']:+5.2f}  扣成本後={r['net_mean_after_cost']*100:+6.2f}%"
        )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=250)
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default="trading/price_volume_pattern_result.txt")
    args = parser.parse_args()

    import db
    conn = sqlite3.connect(args.db or db.DB_PATH)
    code_concepts, names = daily_scan.load_universe(conn)
    conn.close()
    codes = args.codes if args.codes else sorted(code_concepts.keys())
    print(f"universe: {len(codes)} concept_map stocks")

    fetch_start = date.today() - timedelta(days=args.days + WARMUP_DAYS)
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
            if len(daily) < VOL_AVG_WINDOW + max(HORIZONS) + 10:
                continue
            stock_data[code] = build_features(daily)
        api.logout()
    print(f"{len(stock_data)}/{len(codes)} 檔有足夠歷史資料")

    if not stock_data:
        print("no stocks with enough history - aborting")
        return

    rows = collect_rows(stock_data)
    print(f"{len(rows)} 個股票-日樣本點，日期範圍 {rows['date'].min()} ~ {rows['date'].max()}")

    out_lines = [
        f"回測區間：約 {args.days} 天｜樣本股票數：{len(stock_data)}｜樣本點數：{len(rows)}",
        f"來回交易成本假設（手續費x2+賣出稅）：{ROUND_TRIP_COST*100:.3f}%",
        "",
    ]

    drift_line = "整體市場漂移（全樣本原始遠期報酬平均，未扣成本）：" + "｜".join(
        f"{h}日={rows[f'fwd_{h}'].mean()*100:+.2f}%" for h in HORIZONS
    )
    out_lines.append(drift_line)
    out_lines.append("（這條是整個題材股宇宙在這段期間的平均走勢 - 如果下面「原始報酬」每個區間都是正的，"
                      "很可能只是反映這段期間整體在漲，不是該區間本身有特別的訊號。下面「市場中性」版本"
                      "把這段漂移扣掉，只看「比當天其他股票多賺/少賺多少」，才是真正該看的。")
    out_lines.append("")

    out_lines.append("===== [市場中性/主要結論] 單變數：前一日漲跌幅 vs 當天全市場平均的超額遠期報酬 =====")
    for h in HORIZONS:
        out_lines.append(format_table(
            univariate_table(rows, f"excess_{h}", "pct_change", CHANGE_BOUNDS, CHANGE_LABELS),
            f"前一日漲跌幅 -> 未來{h}日超額報酬"
        ))

    out_lines.append("===== [市場中性/主要結論] 單變數：量比 vs 當天全市場平均的超額遠期報酬 =====")
    for h in HORIZONS:
        out_lines.append(format_table(
            univariate_table(rows, f"excess_{h}", "vol_ratio", VOL_BOUNDS, VOL_LABELS),
            f"量比 -> 未來{h}日超額報酬"
        ))

    out_lines.append("===== [市場中性] 雙變數：漲跌幅 x 量比 的超額報酬（全樣本，僅供參考，未經train/test驗證） =====")
    for h in HORIZONS:
        out_lines.append(format_table(bucket_table(rows, f"excess_{h}"), f"漲跌幅x量比 -> 未來{h}日超額報酬"))

    out_lines.append("===== [市場中性] 通過 train/test 一致性檢驗的候選區間（同方向且|t|>=2，才算穩定） =====")
    any_candidate = False
    for h in HORIZONS:
        cand = consistent_candidates(rows, f"excess_{h}")
        if not cand.empty:
            any_candidate = True
        lines = [f"-- 未來{h}日（超額報酬） --"]
        if cand.empty:
            lines.append("  （無：train/test兩段方向或顯著性不一致，視為噪音）")
        else:
            for _, r in cand.iterrows():
                lines.append(
                    f"  {r['change_bucket']} x {r['vol_bucket']}: "
                    f"train 平均超額={r['mean_fwd_return_train']*100:+.2f}%(t={r['t_stat_train']:+.2f},n={int(r['n_train'])}) / "
                    f"test 平均超額={r['mean_fwd_return_test']*100:+.2f}%(t={r['t_stat_test']:+.2f},n={int(r['n_test'])})"
                )
        out_lines.append("\n".join(lines))
    if not any_candidate:
        out_lines.append("\n結論（市場中性版）：扣掉整體市場漂移後，沒有找到train/test都顯著且方向一致的價量區間 - "
                          "也就是說，原始報酬表看起來的「規律」主要是整段期間的市場/題材漂移，不是真正該價量組合"
                          "本身帶來的超額優勢，統計上不足以當作穩定可交易的規律。")

    out_lines.append("")
    out_lines.append("===== [原始報酬，含市場漂移，僅供對照 - 不建議直接採信] =====")
    for h in HORIZONS:
        out_lines.append(format_table(bucket_table(rows, f"fwd_{h}"), f"漲跌幅x量比 -> 未來{h}日原始報酬"))

    report = "\n".join(out_lines)
    print(report)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
