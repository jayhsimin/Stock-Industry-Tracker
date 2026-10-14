"""Entry/exit logic for the trendline-channel strategy - pure functions,
same convention as every other strategy's signals.py (no Shioaji/DB
access, unit-testable with fake data). The actual swing-point/trendline
math (compute_channel()) stays in trading/channel_backtest.py and is
reused UNCHANGED here - trading/channel_cv.py and trading/daytrade/
channel_backtest.py also import it from there, so duplicating it here
would risk the validated version and the live version drifting apart.

Concept (user-specified): connect relative swing highs into an upper
trendline, swing lows into a lower trendline; buy when price breaks BELOW
the lower line (mean-reversion bet), sell when it breaks ABOVE the upper
line - the opposite of a breakout-following strategy.
"""
import pandas as pd

from .. import channel_backtest as backtest
from . import config


def compute_channel(daily, k=config.PIVOT_K):
    return backtest.compute_channel(daily, k=k)


def check_entry(close, lower_line, trade_value, min_trade_value_ntd=config.MIN_TRADE_VALUE_NTD):
    """lower_line_break: today's close is below the lower trendline.
    liquidity_ok: same absolute-turnover-floor convention every other
    strategy in this project uses."""
    conditions = {
        "lower_line_break": pd.notna(lower_line) and close < lower_line,
        "liquidity_ok": trade_value is not None and trade_value >= min_trade_value_ntd,
    }
    conditions["entry_signal"] = all(conditions.values())
    return conditions


def check_exit(entry_price, current_price, days_held, upper_line,
                stop_loss_pct=config.STOP_LOSS_PCT, max_hold_days=config.MAX_HOLD_DAYS):
    """upper_line_break: the strategy's own designed exit (price broke back
    above the upper trendline - the mean-reversion bet paid off).
    stop_loss/max_hold_days: safety net in case the channel keeps trending
    against the position instead of reverting."""
    unrealized_return = (current_price - entry_price) / entry_price
    conditions = {
        "upper_line_break": pd.notna(upper_line) and current_price > upper_line,
        "stop_loss": unrealized_return <= stop_loss_pct,
        "max_hold_days": days_held > max_hold_days,
    }
    conditions["exit_signal"] = any(conditions.values())
    conditions["unrealized_return"] = round(unrealized_return, 4)
    return conditions
