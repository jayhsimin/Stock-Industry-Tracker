"""Entry/exit rule evaluation for the day-trade strategy - pure functions,
no Shioaji/pandas here on purpose, same reasoning as trading/signals.py:
testable with plain numbers. minute_data.py is what turns kbars into the
plain values these take.
"""
from . import config


def check_entry(close, or_high, volume_ratio, trade_value,
                 volume_ratio_min=config.VOLUME_RATIO_MIN,
                 min_trade_value_ntd=config.MIN_TRADE_VALUE_NTD):
    """or_high: opening_range_high() - None means the opening range hasn't
    finalized yet (still inside the first ORB_WINDOW_MINUTES), so there's
    nothing to break out of and this always fails closed."""
    conditions = {
        "orb_breakout": or_high is not None and close > or_high,
        "volume_increase": volume_ratio is not None and volume_ratio >= volume_ratio_min,
        "liquidity_ok": trade_value is not None and trade_value >= min_trade_value_ntd,
    }
    conditions["entry_signal"] = all(conditions.values())
    return conditions


def check_exit(entry_price, current_price, now_hhmm,
                stop_loss_pct=config.STOP_LOSS_PCT,
                take_profit_pct=config.TAKE_PROFIT_PCT,
                force_flatten_time=config.FORCE_FLATTEN_TIME):
    """now_hhmm: the latest minute bar's OWN timestamp ("HH:MM", zero-padded,
    so lexical >= comparison works) - not wall-clock time, so force-flatten
    is tied to the market's own data instead of the machine's clock."""
    unrealized_return = (current_price - entry_price) / entry_price
    conditions = {
        "stop_loss": unrealized_return <= stop_loss_pct,
        "take_profit": unrealized_return >= take_profit_pct,
        "force_flatten": now_hhmm is not None and now_hhmm >= force_flatten_time,
    }
    conditions["exit_signal"] = any(conditions.values())
    conditions["unrealized_return"] = round(unrealized_return, 4)
    return conditions
