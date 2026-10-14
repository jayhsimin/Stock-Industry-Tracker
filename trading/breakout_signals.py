"""True/false breakout confirmation entry rule (daily bars) for the swing
strategy, per https://chainstockinsight.com/true-vs-false-breakout-guide/.
An ALTERNATIVE to trading/signals.py's existing "回檔支撐+量增" (pullback-
to-support) entry rule, not yet wired into trading/daily_scan.py's
production path - see trading/breakout_backtest.py for A/B validation
against the existing rule before considering a swap (same "test before
trusting" discipline as every other strategy change in this project).

Simplifications from the article's prose, documented explicitly:
  - RSI/MACD "divergence" is simplified to "RSI > 50 and MACD histogram > 0"
    (momentum pointing the same way as the breakout), not true peak-to-peak
    divergence detection (which needs a swing-high/low finder) - still
    catches the article's core point (don't breakout into weakening
    momentum) without the extra complexity.
  - The "support/resistance flip holds" check compares each day's close to
    that SAME day's rolling 20-day-high resistance (which barely moves
    day to day), not a frozen snapshot of the exact level at the breakout
    moment - a reasonable approximation, not the literal reading.
"""
import pandas as pd

from . import ta


def true_breakout_entry_signal(daily_df, resistance_lookback=20, atr_period=14, atr_multiplier=1.0,
                                volume_ma_period=20, volume_ratio_min=1.5,
                                space_pct=0.03, close_position_min=0.75,
                                upper_wick_max_ratio=2.0, confirm_days=2, rsi_period=14):
    """daily_df: DataFrame with Open/High/Low/Close/Volume, one row per
    trading day, oldest first. Returns (entry_signal, detail) where
    entry_signal is a boolean Series (same index) - True on the day
    CONFIRMATION completes (not the breakout day itself - entry is
    deliberately `confirm_days-1` days after the actual breakout, once it
    has held)."""
    o, h, l, c, v = daily_df["Open"], daily_df["High"], daily_df["Low"], daily_df["Close"], daily_df["Volume"]

    resistance = h.shift(1).rolling(resistance_lookback).max()
    atr_val = ta.atr(h, l, c, atr_period).shift(1)
    threshold = ta.breakout_level(resistance, atr_val, atr_multiplier)

    vol_ratio = ta.volume_ma_ratio(v, volume_ma_period)
    close_pos, upper_wick = ta.candle_structure(o, h, l, c)
    rsi_val = ta.rsi(c, rsi_period)
    _, _, macd_hist = ta.macd(c)

    is_breakout_day = (
        (c > threshold)
        & (c >= resistance * (1 + space_pct))
        & (vol_ratio >= volume_ratio_min)
        & (close_pos >= close_position_min)
        & (upper_wick <= upper_wick_max_ratio)
        & (rsi_val > 50)
        & (macd_hist > 0)
    )

    # "holding above resistance" must compare every confirmation day's close
    # to the SAME resistance level that was in effect AT THE BREAKOUT MOMENT
    # (frozen), not each day's freshly-recomputed rolling resistance - the
    # breakout day's own high enters the 20-day lookback the very next day
    # and would otherwise drag resistance up past the breakout price itself,
    # making "holding" impossible to satisfy for any confirm_days > 1.
    frozen_resistance = resistance.shift(confirm_days - 1)
    breakout_n_ago = is_breakout_day.shift(confirm_days - 1).astype("boolean").fillna(False)
    held_since = pd.Series(True, index=c.index)
    for offset in range(confirm_days):
        held_since &= (c.shift(offset) > frozen_resistance).astype("boolean").fillna(False)
    entry_signal = (breakout_n_ago & held_since).astype(bool)

    detail = {
        "resistance": resistance, "threshold": threshold, "vol_ratio": vol_ratio,
        "close_pos": close_pos, "upper_wick": upper_wick, "rsi": rsi_val,
        "macd_hist": macd_hist, "is_breakout_day": is_breakout_day,
    }
    return entry_signal, detail


def candlestick_reversal_entry_signal(daily_df, min_lower_wick_ratio=2.0, max_upper_wick_ratio=0.5):
    """Vectorized daily-bar version of trading/daytrade/patterns.py's
    is_hammer()/is_bullish_engulfing() (same formulas, whole-Series at
    once instead of per-bar) - for the 2026-10 "型態訊號" swing-strategy
    cross-validation, where recomputing per parameter-grid-point needs to
    be fast across ~180 stocks x hundreds of days."""
    o, h, l, c = daily_df["Open"], daily_df["High"], daily_df["Low"], daily_df["Close"]
    rng = h - l
    body = (c - o).abs()
    upper_wick = h - pd.concat([o, c], axis=1).max(axis=1)
    lower_wick = pd.concat([o, c], axis=1).min(axis=1) - l
    floor = pd.concat([body, rng * 0.05], axis=1).max(axis=1)
    is_hammer = (lower_wick >= min_lower_wick_ratio * floor) & (upper_wick <= max_upper_wick_ratio * floor) & (rng > 0)

    prev_o, prev_c = o.shift(1), c.shift(1)
    prev_bearish = prev_c < prev_o
    bullish = c > o
    engulfs = (o <= prev_c) & (c >= prev_o)
    is_bullish_engulfing = prev_bearish & bullish & engulfs

    return (is_hammer | is_bullish_engulfing).fillna(False)


def stop_loss_price(daily_df, breakout_day_idx, resistance_at_breakout):
    """SFP-style stop: the lower of (resistance that was broken) or (the
    breakout candle's own low) - whichever is higher protection. Returns
    a price level, not a percentage."""
    breakout_low = daily_df["Low"].iloc[breakout_day_idx]
    return min(resistance_at_breakout, breakout_low)
