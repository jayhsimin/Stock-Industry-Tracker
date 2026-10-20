"""Validates the user's "price vs big-holder% divergence" stock-selection
idea - 大戶(>400張)持股比例增加且散戶(人數)減少、同時股價下降(逢低吃貨訊號)，
或完全相反的情況(出貨疑慮) - as a forward-return-predictive signal.

Data source: a SEPARATE, pre-existing project (`Mysterious_Pyramid`, found
via filesystem search 2026-10-20 - NOT built for this project) already
scrapes norway.twsthr.info's per-stock "明細" page weekly (集保戶股權分散表,
mirroring TDCC's own archive) via a Windows-scheduled task
(MysteriousPyramid_WeeklyStockUpdate, Sat 22:00 - confirmed registered and
last-run exit code 0) into one CSV per stock
(`Mysterious_Pyramid/output/<code>_history.csv`, ~26 weeks/~6 months,
newest-row-first is NOT assumed - this script sorts by date itself).
decision_price_chip_divergence.py in that project already classifies
latest-vs-previous-week; this script instead walks EVERY week-over-week
transition across the whole ~6-month history and measures forward returns,
to check whether the classification actually predicts anything, not just
describe the latest week.

This is genuinely point-in-time weekly history (not a current-snapshot-
only feed like twstock.db's monthly_revenue), so unlike
channel_ranked_backtest.py's fundamentals factor, this one can be
backtested honestly - BUT only ~26 weeks exist, a much thinner sample than
the 250-trading-day backtests used elsewhere in this project. Read any
"validated" result here as provisional.

Signal classification each week-over-week transition (mirrors
Mysterious_Pyramid/decision_price_chip_divergence.py's rule exactly):
  accumulation_signal - 收盤價下降 AND >400張大股東持股% 上升 (使用者描述的主情境)
  distribution_risk   - 收盤價上升 AND >400張大股東持股% 下降 (完全相反的情境)
  neutral              - 其餘(同向或持平) - excluded from the signal buckets

Forward return measured at 1/2/4 weeks ahead using the SAME weekly 收盤價
series (signal and forward return share one cadence, no need to
cross-reference the project's separate daily-price CSVs), both raw and
market-neutral (minus that week's cross-sectional average across the
whole universe - same anti-drift discipline as price_volume_pattern_study.py).

Usage:
    python -m trading.chip_divergence_study
    python -m trading.chip_divergence_study --data-dir "C:/Users/z2012/Desktop/Mysterious_Pyramid/output"
"""
import argparse
import glob
import os

import numpy as np
import pandas as pd

DEFAULT_DATA_DIR = r"C:\Users\z2012\Desktop\Mysterious_Pyramid\output"
HORIZONS = (1, 2, 4)  # weeks ahead
MIN_N = 15            # below this, a bucket's stats aren't reported (thin ~26-week sample)
TRAIN_FRACTION = 0.6

NUMERIC_COLS = ["總股東人數", ">400張大股東持有百分比", "收盤價"]


def load_history(data_dir):
    """{code: DataFrame sorted ascending by date, columns: date(Timestamp),
    price, holder_pct, shareholders}. Skips files with no usable rows."""
    result = {}
    for path in sorted(glob.glob(os.path.join(data_dir, "*_history.csv"))):
        code = os.path.basename(path).replace("_history.csv", "")
        df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
        df.columns = [c.strip() for c in df.columns]
        if "資料日期" not in df.columns:
            continue
        for c in df.columns:
            df[c] = df[c].str.strip()
        df["date"] = pd.to_datetime(df["資料日期"], format="%Y%m%d", errors="coerce")
        for c in NUMERIC_COLS:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date")
        df = df.rename(columns={"總股東人數": "shareholders", ">400張大股東持有百分比": "holder_pct", "收盤價": "price"})
        df = df[["date", "price", "holder_pct", "shareholders"]].drop_duplicates(subset="date")
        if len(df) >= 2:
            result[code] = df.reset_index(drop=True)
    return result


def classify_transitions(history):
    """{code: df} -> flat DataFrame, one row per week-over-week transition
    with columns code/date(=the LATER week's date)/signal/price/fwd_<h>
    (forward %return h weeks after `date`, from the same weekly series)."""
    rows = []
    for code, df in history.items():
        price = df["price"].to_numpy()
        holder = df["holder_pct"].to_numpy()
        dates = df["date"].to_numpy()
        n = len(df)
        for i in range(1, n):
            if any(pd.isna(x) for x in (price[i], price[i - 1], holder[i], holder[i - 1])):
                continue
            price_up, price_down = price[i] > price[i - 1], price[i] < price[i - 1]
            holder_up, holder_down = holder[i] > holder[i - 1], holder[i] < holder[i - 1]
            if price_down and holder_up:
                signal = "accumulation_signal"
            elif price_up and holder_down:
                signal = "distribution_risk"
            else:
                signal = "neutral"
            row = {"code": code, "date": dates[i], "signal": signal, "price": price[i]}
            for h in HORIZONS:
                row[f"fwd_{h}"] = (price[i + h] / price[i] - 1) if i + h < n and pd.notna(price[i + h]) else None
            rows.append(row)
    return pd.DataFrame(rows)


def add_excess_returns(rows):
    """Adds excess_<h> = fwd_<h> minus that week's cross-sectional mean
    fwd_<h> across the WHOLE universe (not just the signal buckets) - see
    price_volume_pattern_study.py's docstring for why this matters: a
    single ~6-month window can look like it has a pattern when it's really
    just reflecting a broad market/theme move that period happened to have."""
    rows = rows.copy()
    for h in HORIZONS:
        benchmark = rows.groupby("date")[f"fwd_{h}"].transform("mean")
        rows[f"excess_{h}"] = rows[f"fwd_{h}"] - benchmark
    return rows


def bucket_stats(rows, signal, col, min_n=MIN_N):
    sub = rows[rows["signal"] == signal][col].dropna()
    n = len(sub)
    if n < min_n:
        return None
    mean, std = sub.mean(), sub.std()
    return {"n": n, "mean": mean, "win_rate": (sub > 0).mean(),
            "t_stat": mean / (std / np.sqrt(n)) if std > 0 else 0.0}


def chronological_split(rows, frac=TRAIN_FRACTION):
    dates = sorted(rows["date"].unique())
    cut = dates[int(len(dates) * frac)]
    return rows[rows["date"] < cut], rows[rows["date"] >= cut]


def fmt(s):
    if s is None:
        return f"（樣本不足，n<{MIN_N}）"
    return f"n={s['n']}｜平均={s['mean']*100:+.2f}%｜勝率={s['win_rate']*100:.1f}%｜t={s['t_stat']:+.2f}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--out", default="trading/chip_divergence_study_result.txt")
    args = parser.parse_args()

    if not os.path.isdir(args.data_dir):
        print(f"找不到資料目錄: {args.data_dir}")
        return

    history = load_history(args.data_dir)
    print(f"{len(history)} 檔股票有可用的籌碼歷史資料")
    if not history:
        return

    rows = classify_transitions(history)
    rows = add_excess_returns(rows)
    print(f"{len(rows)} 筆週轉換樣本，日期範圍 {rows['date'].min()} ~ {rows['date'].max()}")
    print(rows["signal"].value_counts().to_string())

    lines = [f"樣本：{len(history)}檔股票｜{len(rows)}筆週轉換｜"
             f"{rows['date'].min().date()} ~ {rows['date'].max().date()}", ""]

    lines.append("===== 全樣本（市場中性超額報酬） =====")
    for signal in ("accumulation_signal", "distribution_risk", "neutral"):
        lines.append(f"-- {signal} --")
        for h in HORIZONS:
            lines.append(f"  未來{h}週: {fmt(bucket_stats(rows, signal, f'excess_{h}'))}")
        lines.append("")

    lines.append("===== Train/Test 一致性檢驗（僅市場中性，同方向且|t|>=1.5才算穩定——"
                  "樣本只有~26週，門檻比250天回測的研究寬鬆） =====")
    train, test = chronological_split(rows)
    any_candidate = False
    for signal in ("accumulation_signal", "distribution_risk"):
        for h in HORIZONS:
            s_train = bucket_stats(train, signal, f"excess_{h}", min_n=MIN_N // 2 or 1)
            s_test = bucket_stats(test, signal, f"excess_{h}", min_n=MIN_N // 2 or 1)
            if s_train is None or s_test is None:
                continue
            agree = (s_train["mean"] > 0) == (s_test["mean"] > 0) and abs(s_train["t_stat"]) >= 1.5 and abs(s_test["t_stat"]) >= 1.5
            if agree:
                any_candidate = True
                lines.append(f"[{signal}] 未來{h}週 - train: {fmt(s_train)} / test: {fmt(s_test)}  <- 同方向且顯著")
    if not any_candidate:
        lines.append("（無：沒有任何 信號x時間窗 組合在train/test兩段同方向且都顯著 - 視為噪音，樣本量也偏薄）")

    text = "\n".join(lines)
    print("\n" + text)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
