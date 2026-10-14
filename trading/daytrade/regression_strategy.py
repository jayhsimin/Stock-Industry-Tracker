"""Data-driven (regression-based) day-trade strategy discovery, per
https://gorich.tw/day-trading-and-scalping-guide/ - that article is mostly
conceptual (no concrete thresholds), naming: 開盤走勢偏向 (first-15-minute
bias), VWAP (intraday long/short reference), 動能/突破追價 (momentum
breakout), and 周轉率/振幅/成交量選股 (turnover/amplitude/volume stock
screening). Tick-level bid/ask imbalance ("內外盤比") is also mentioned but
NOT implemented here - Shioaji's kbars() is OHLCV only, no tick-level
bid/ask data, so that part of the article's framework isn't testable with
this project's data source.

This operationalizes those concepts as REGRESSION FEATURES (not hand-picked
thresholds like the rest of trading/daytrade/) and fits a linear model of
forward N-minute return on them, across a liquid-stock universe over the
last `--days` trading days. The fitted coefficients - which ones are
statistically significant, and in which direction - are the "found"
strategy: a positive, significant coefficient on a feature means that
feature should be part of the entry rule; main() prints the full
statsmodels summary (coefficients, t-stats, p-values, R²) so this is
inspectable, not a black box.

KNOWN LIMITATION: only 10 days by default is a small sample for a
multi-feature regression - treat any "significant" coefficient as a
hypothesis to re-validate over a longer window (see backtest.py's pattern
of retesting over 90 days), not a proven edge, same caveat this project
has run into with every hand-picked signal so far.

Usage:
    python -m trading.daytrade.regression_strategy --days 10
    python -m trading.daytrade.regression_strategy --days 10 --universe-size 100
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd
import statsmodels.api as sm

import db
from .. import executor
from . import minute_data, watchlist

FORWARD_MINUTES = 10  # predict return over the next N minutes
OPEN_WINDOW = 15      # "開盤八法" - first 15 minutes sets the day's bias


def load_universe(conn, size):
    """Top `size` stocks by yesterday's trading value (成交金額) - the
    article's 選股篩選 step (turnover/volume), computed once from the
    most recent available day via the same kbars-based aggregate
    watchlist.py already uses."""
    codes = [r[0] for r in conn.execute('SELECT "公司代號" FROM company_info')]
    names = dict(conn.execute('SELECT "公司代號", "公司簡稱" FROM company_info'))
    return codes, names


def rank_universe_by_liquidity(api, codes, names, size):
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=6)
    bars_by_code = {}
    for code in codes:
        contract = api.Contracts.Stocks.get(code)
        if contract is None:
            continue
        bars = minute_data.bars_for_range(api, contract, start.isoformat(), end.isoformat())
        if not bars.empty:
            bars_by_code[code] = bars
    aggs = watchlist.previous_day_aggregates(bars_by_code)
    ranked = sorted(aggs.items(), key=lambda kv: kv[1]["amount"], reverse=True)
    top = [code for code, _ in ranked[:size]]
    return top, {c: aggs[c] for c in top}


def build_features_for_stock_day(bars, prior_amount, prior_amplitude):
    """bars: one stock's bars for ONE trading day. Returns a DataFrame,
    one row per minute (minus the last FORWARD_MINUTES, which have no
    forward return yet, and the first OPEN_WINDOW, which have no
    finalized opening bias yet) with all regression features + target."""
    if len(bars) <= OPEN_WINDOW + FORWARD_MINUTES:
        return None

    close = bars["Close"]
    high = bars["High"]
    volume = bars["Volume"]
    amount = bars["Amount"]

    # Shioaji kbars' Volume is in 張 (board lots, x1000 shares), NOT raw
    # shares - confirmed empirically (Amount/Volume ~= Close*1000 on real
    # 2330 data). Every OTHER use of Volume in this project only ever
    # compares volume to itself (minute_volume_ratio) or sums Amount
    # directly (cumulative_trade_value), so the x1000 factor cancels out
    # and never mattered - this VWAP calc is the first place that divides
    # Amount by Volume directly, which is why this bug only lives here.
    cum_amount = amount.cumsum()
    cum_shares = volume.cumsum() * 1000
    vwap = cum_amount / cum_shares
    vwap_dev = (close - vwap) / vwap

    open_px = bars["Open"].iloc[0]
    open15_close = close.iloc[OPEN_WINDOW - 1]
    open15_high = high.iloc[:OPEN_WINDOW].max()
    open15_low = bars["Low"].iloc[:OPEN_WINDOW].min()
    open15_ret = (open15_close - open_px) / open_px
    open15_range = (open15_high - open15_low) / open_px

    running_high = high.shift(1).cummax()
    mom_breakout = (close > running_high).astype(float)

    vol_ratio = volume / volume.shift(1).expanding().mean()

    minutes_since_open = np.arange(len(bars))

    forward_close = close.shift(-FORWARD_MINUTES)
    forward_return = (forward_close - close) / close

    df = pd.DataFrame({
        "vwap_dev": vwap_dev,
        "open15_ret": open15_ret,
        "open15_range": open15_range,
        "mom_breakout": mom_breakout,
        "vol_ratio": vol_ratio,
        "prior_day_amount_1e8": prior_amount / 1e8,
        "prior_day_amplitude": prior_amplitude,
        "minutes_since_open": minutes_since_open,
        "forward_return": forward_return,
    })
    return df.iloc[OPEN_WINDOW:-FORWARD_MINUTES]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--universe-size", type=int, default=100)
    parser.add_argument("--out", default="trading/daytrade/regression_result.txt")
    args = parser.parse_args()

    conn = sqlite3.connect(db.DB_PATH)
    all_codes, names = load_universe(conn, args.universe_size)
    conn.close()

    end = date.today()
    start = end - timedelta(days=args.days)

    api, live = executor.login(False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        universe, liquidity = rank_universe_by_liquidity(api, all_codes, names, args.universe_size)
        print(f"universe: top {len(universe)} by yesterday's trading value")

        bars_by_code = {}
        for code in universe:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            bars = minute_data.bars_for_range(api, contract, start.isoformat(), end.isoformat())
            if not bars.empty:
                bars_by_code[code] = bars
        api.logout()
    print(f"{len(bars_by_code)}/{len(universe)} stocks had historical minute bars")

    rows = []
    for code, df in bars_by_code.items():
        days_present = sorted(set(df.index.normalize()))
        daily = watchlist.daily_aggregate(df)
        for i, day in enumerate(days_present):
            if i == 0:
                continue  # no prior day in this window to compute prior_day_* from
            prior_day = days_present[i - 1]
            prior_row = daily.loc[daily.index.normalize() == prior_day]
            if prior_row.empty:
                continue
            prior_amount = float(prior_row["Amount"].iloc[0])
            prior_amplitude = float((prior_row["High"].iloc[0] - prior_row["Low"].iloc[0]) / prior_row["Open"].iloc[0])
            day_bars = df[df.index.normalize() == day]
            feat = build_features_for_stock_day(day_bars, prior_amount, prior_amplitude)
            if feat is not None and not feat.empty:
                feat["code"] = code
                feat["day"] = day.date().isoformat()
                rows.append(feat)

    if not rows:
        print("no usable rows - not enough history in this window")
        return
    data = pd.concat(rows, ignore_index=True).dropna()
    print(f"regression dataset: {len(data)} stock-minute rows across {data['code'].nunique()} stocks")

    feature_cols = ["vwap_dev", "open15_ret", "open15_range", "mom_breakout",
                     "vol_ratio", "prior_day_amount_1e8", "prior_day_amplitude", "minutes_since_open"]
    X = sm.add_constant(data[feature_cols])
    y = data["forward_return"]
    model = sm.OLS(y, X).fit()

    lines = [
        f"=== 回歸分析：過去{args.days}天、{data['code'].nunique()}檔股票、{len(data)}筆分鐘資料 ===",
        f"預測目標：未來{FORWARD_MINUTES}分鐘報酬率",
        "",
        str(model.summary()),
        "",
        "-- 顯著(p<0.05)且方向對當沖有意義的特徵 --",
    ]
    for feat in feature_cols:
        coef, pval = model.params[feat], model.pvalues[feat]
        sig = "***顯著***" if pval < 0.05 else "不顯著"
        lines.append(f"{feat}: 係數={coef:.6f}, p值={pval:.4f} [{sig}]")

    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
