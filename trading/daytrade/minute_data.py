"""Today's 1-minute bars for the day-trade strategy - everything here reads
Shioaji's kbars() for TODAY only (not the multi-day resample-to-daily that
trading/ma_data.py does for the swing strategy's MA5/20/60).

Deliberately NOT using api.snapshots() for day-trade: a snapshot's
volume_ratio compares today's pace to a trailing 5-day DAILY average, and
total_amount is just a running total - neither is a minute-level figure.
Since the day-trade monitor re-runs every 1 minute, every signal input here
is recomputed fresh from the day's actual minute bars each time, so there's
no state file to maintain across runs (unlike the swing strategy's
positions_state.json, which persists because open/closed positions
genuinely span runs - the bars themselves don't need to).
"""
from datetime import date

import pandas as pd


def bars_for_range(api, contract, start, end):
    """1-minute bars for [start, end] (ISO date strings, inclusive) as a
    DataFrame (ts index, Open/High/Low/Close/Volume/Amount columns), oldest
    first. Empty if Shioaji has nothing for that range. Used directly by
    today_bars() (range = today) and by backtest.py (range = a historical
    window, one call per stock instead of one per day)."""
    kbars = api.kbars(contract, start=start, end=end)
    if not kbars.ts:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume", "Amount"])
    df = pd.DataFrame({
        "ts": pd.to_datetime(kbars.ts),
        "Open": kbars.Open, "High": kbars.High, "Low": kbars.Low,
        "Close": kbars.Close, "Volume": kbars.Volume, "Amount": kbars.Amount,
    })
    return df.set_index("ts")


def today_bars(api, contract):
    """Today's 1-minute bars - see bars_for_range(). Empty before the
    market opens or if Shioaji has nothing for today yet."""
    today = date.today().isoformat()
    return bars_for_range(api, contract, today, today)


def _opening_range_window(bars, window_minutes):
    """The first `window_minutes` of today's bars, or None until today's
    bars span MORE than the window - i.e. the opening range isn't
    finalized yet (this is the only guard needed against trading during
    the OR window itself)."""
    if bars.empty:
        return None
    first_ts = bars.index[0]
    cutoff = first_ts + pd.Timedelta(minutes=window_minutes)
    if bars.index[-1] < cutoff:
        return None
    window = bars[bars.index < cutoff]
    return window if not window.empty else None


def opening_range_high(bars, window_minutes):
    """Max High across the opening range window. None if not finalized yet."""
    window = _opening_range_window(bars, window_minutes)
    return float(window["High"].max()) if window is not None else None


def opening_range_low(bars, window_minutes):
    """Min Low across the opening range window. None if not finalized yet."""
    window = _opening_range_window(bars, window_minutes)
    return float(window["Low"].min()) if window is not None else None


def opening_range_pct(bars, window_minutes):
    """(high - low) / low across the opening range window - how much the
    stock actually moved in the first window_minutes, independent of
    direction. Used as a quality filter: a breakout above a dead-flat
    opening range has nothing behind it. None if not finalized yet."""
    window = _opening_range_window(bars, window_minutes)
    if window is None:
        return None
    low = float(window["Low"].min())
    high = float(window["High"].max())
    return (high - low) / low if low else None


def latest_close(bars):
    return float(bars["Close"].iloc[-1]) if not bars.empty else None


def latest_time_hhmm(bars):
    """The last bar's own timestamp, not wall-clock time - ties exit timing
    (force-flatten) to the market's own data instead of the machine's clock."""
    return bars.index[-1].strftime("%H:%M") if not bars.empty else None


def minute_volume_ratio(bars):
    """The latest completed minute's Volume vs the average per-minute Volume
    of every earlier minute today (excluding itself) - a minute-K-based
    volume-spike check, independent of Shioaji snapshot's day-over-day
    volume_ratio. None if there isn't at least one earlier minute to compare
    against yet."""
    if len(bars) < 2:
        return None
    latest = bars["Volume"].iloc[-1]
    baseline = bars["Volume"].iloc[:-1].mean()
    if not baseline:
        return None
    return latest / baseline


def cumulative_trade_value(bars):
    """Sum of Amount (NTD) across all of today's bars so far - today's
    cumulative turnover, computed from minute bars instead of Shioaji
    snapshot's total_amount."""
    return float(bars["Amount"].sum()) if not bars.empty else 0.0
