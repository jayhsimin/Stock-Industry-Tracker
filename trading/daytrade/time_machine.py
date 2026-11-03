"""Monte Carlo "rest of the session" simulator for the day-trade strategy -
a Taiwan/Shioaji-native reimplementation of the "Time Machine" idea from
Jakeschincariol/day-trader-skill (a US-market, Alpaca/Yahoo-Finance-based
Claude Code skill found not directly usable for this project - wrong
market, wrong broker, wrong packaging model; see 2026-10 conversation).
Only the CONCEPT is reused here, rebuilt from scratch against this
project's own Shioaji minute-bar infrastructure (minute_data.py), not a
line of the original code.

Idea: at the moment a day-trade entry signal fires, bootstrap-resample the
stock's OWN recent minute-to-minute returns (real historical minute bars,
not a synthetic/assumed distribution) to simulate many possible paths for
the rest of today's session, and see how often each one hits take-profit
before stop-loss. This is a PRE-TRADE probability estimate, not a new
entry/exit rule - trading/daytrade/signals.py's check_entry()/check_exit()
are unchanged; this only asks "given how this stock has actually moved
recently, how often would this specific stop/target pair have resolved in
my favor from here." See time_machine_backtest.py for whether that
estimate actually correlates with real trade outcomes before trusting it
for anything beyond curiosity.
"""
import numpy as np
import pandas as pd

from . import config

DEFAULT_N_SIMS = 10_000
DEFAULT_LOOKBACK_DAYS = 10
MINUTES_PER_SESSION = 270  # 09:00-13:30, matches FORCE_FLATTEN_TIME's 13:00 flatten
                            # plus the half hour trailing settlement window


def build_return_pool(bars):
    """{bars: DataFrame with a Close column, minute-indexed, possibly
    spanning many days} -> 1D array of minute-over-minute %returns,
    dropping the first bar of each calendar day (a close-to-open jump
    across days is not a same-session minute-to-minute move)."""
    if bars.empty or len(bars) < 2:
        return np.array([])
    dates = pd.Series(bars.index.date, index=bars.index)
    same_day = (dates == dates.shift(1)).to_numpy()
    rets = (bars["Close"] / bars["Close"].shift(1) - 1).to_numpy()
    rets = rets[same_day]
    return rets[~np.isnan(rets)]


def simulate_remaining_session(entry_price, minutes_remaining, return_pool, side="long",
                                stop_loss_pct=config.STOP_LOSS_PCT,
                                take_profit_pct=config.TAKE_PROFIT_PCT,
                                n_sims=DEFAULT_N_SIMS, rng=None):
    """Bootstrap-resamples `return_pool` (i.i.d., with replacement) to build
    n_sims simulated minute-by-minute price paths of length
    minutes_remaining starting from entry_price, and classifies each path
    by whichever of take_profit/stop_loss it crosses FIRST (side-aware,
    same sign convention as signals.check_exit) - "neither" if it reaches
    the end of the simulated window without crossing either.

    Returns {prob_take_profit_first, prob_stop_loss_first, prob_neither,
    mean_final_return, n_sims, minutes_remaining} - None if return_pool is
    too small to resample from (fewer than 20 observations is too thin a
    sample of this stock's own behavior to bootstrap from credibly)."""
    if minutes_remaining <= 0 or len(return_pool) < 20:
        return None
    rng = rng or np.random.default_rng()
    draws = rng.choice(return_pool, size=(n_sims, minutes_remaining), replace=True)
    if side == "short":
        draws = -draws
    paths = np.cumprod(1 + draws, axis=1) - 1  # cumulative return at each minute, per sim

    hit_tp = paths >= take_profit_pct
    hit_sl = paths <= stop_loss_pct
    first_tp = np.where(hit_tp.any(axis=1), hit_tp.argmax(axis=1), np.iinfo(np.int64).max)
    first_sl = np.where(hit_sl.any(axis=1), hit_sl.argmax(axis=1), np.iinfo(np.int64).max)

    tp_first = first_tp < first_sl
    sl_first = first_sl < first_tp
    neither = ~tp_first & ~sl_first

    return {
        "prob_take_profit_first": float(tp_first.mean()),
        "prob_stop_loss_first": float(sl_first.mean()),
        "prob_neither": float(neither.mean()),
        "mean_final_return": float(paths[:, -1].mean()),
        "n_sims": n_sims,
        "minutes_remaining": minutes_remaining,
    }


def minutes_until_flatten(now_hhmm, force_flatten_time=config.FORCE_FLATTEN_TIME):
    """Minutes from now_hhmm ("HH:MM") to force_flatten_time, both treated
    as today's times - 0 if already past flatten time."""
    now_h, now_m = (int(x) for x in now_hhmm.split(":"))
    end_h, end_m = (int(x) for x in force_flatten_time.split(":"))
    return max(0, (end_h * 60 + end_m) - (now_h * 60 + now_m))
