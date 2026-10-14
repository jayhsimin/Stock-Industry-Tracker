"""Shared technical-indicator building blocks for the true/false-breakout
methodology (https://chainstockinsight.com/true-vs-false-breakout-guide/),
used by both trading/signals.py (daily bars, swing) and
trading/daytrade/patterns.py (minute bars, day-trade). These are pure
functions over OHLCV pandas Series/DataFrames - period counts are bars,
not calendar days, so the SAME function works on daily or minute data;
only the period PARAMETER differs between the two callers (e.g. ATR(14)
on daily closes vs ATR(14) on minute closes means a very different span
of real time).
"""
import pandas as pd


def atr(high, low, close, period=14):
    """Average True Range - True Range = max(high-low, |high-prev_close|,
    |low-prev_close|), then a rolling mean over `period` bars."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def rsi(close, period=14):
    """Standard Wilder RSI: 100 - 100/(1+RS), RS = avg gain / avg loss
    over `period` bars (using a simple rolling mean, not Wilder's
    smoothing - a documented simplification, close enough for a
    confirmation filter rather than a precision RSI reading)."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    result = 100 - 100 / (1 + rs)
    # avg_loss==0 with gains present (pure uptrend) -> RSI=100, not NaN from
    # the div-by-zero guard above; both zero (flat) -> conventional 50
    result = result.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    result = result.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
    return result


def macd(close, fast=12, slow=26, signal=9):
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line


def volume_ma_ratio(volume, period=20):
    """Today's/this-bar's volume vs the trailing `period`-bar average
    (EXCLUDING the current bar, so it's a real baseline, not including
    itself) - the breakout guide's "20日均量" volume-confirmation check,
    generalized to any period."""
    baseline = volume.shift(1).rolling(period).mean()
    return volume / baseline


def candle_structure(open_, high, low, close):
    """Per-bar close position within the bar's own range (0=at low,
    1=at high) and upper-wick-to-body ratio - the guide's "收盤落在振幅
    上緣25%" and "上影線超過實體2倍" false-breakout checks. Returns
    (close_position, upper_wick_ratio) as Series; upper_wick_ratio is
    inf (not NaN) for a doji (body==0) with any upper wick, so comparisons
    like `upper_wick_ratio > 2` still correctly flag it as a bad structure
    rather than silently passing on a NaN."""
    rng = (high - low).replace(0, float("nan"))
    close_position = (close - low) / rng
    body = (close - open_).abs()
    upper_wick = high - pd.concat([open_, close], axis=1).max(axis=1)
    upper_wick_ratio = upper_wick / body.replace(0, float("inf"))
    return close_position, upper_wick_ratio


def breakout_level(resistance, atr_value, atr_multiplier=1.0):
    """The guide's ATR dynamic threshold: a breakout only counts once
    price clears resistance by at least `atr_multiplier` x ATR - not
    just resistance itself, which the guide treats as noise-level."""
    return resistance + atr_multiplier * atr_value
