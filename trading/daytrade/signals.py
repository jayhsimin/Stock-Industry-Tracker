"""Entry/exit rule evaluation for the day-trade strategy - pure functions,
no Shioaji/pandas here on purpose, same reasoning as trading/signals.py:
testable with plain numbers. minute_data.py is what turns kbars into the
plain values these take.
"""
from . import config


def check_entry(close, or_high, volume_ratio, trade_value, or_range_pct=None,
                 volume_ratio_min=config.VOLUME_RATIO_MIN,
                 min_trade_value_ntd=config.MIN_TRADE_VALUE_NTD,
                 min_or_range_pct=config.MIN_OR_RANGE_PCT):
    """or_high: opening_range_high() - None means the opening range hasn't
    finalized yet (still inside the first ORB_WINDOW_MINUTES), so there's
    nothing to break out of and this always fails closed.

    or_range_pct: minute_data.opening_range_pct() - how much the stock
    actually moved during the opening range, independent of breakout
    direction. Added after the 2026-10 backtest showed most "breakouts"
    above a flat opening range just drift to force-flatten without real
    follow-through; requiring the OR itself to have some range filters
    those out before the breakout check even matters."""
    conditions = {
        "orb_breakout": or_high is not None and close > or_high,
        "volume_increase": volume_ratio is not None and volume_ratio >= volume_ratio_min,
        "liquidity_ok": trade_value is not None and trade_value >= min_trade_value_ntd,
        "or_range_ok": or_range_pct is not None and or_range_pct >= min_or_range_pct,
    }
    conditions["entry_signal"] = all(conditions.values())
    return conditions


def check_exit(entry_price, current_price, now_hhmm, side="long",
                stop_loss_pct=config.STOP_LOSS_PCT,
                take_profit_pct=config.TAKE_PROFIT_PCT,
                force_flatten_time=config.FORCE_FLATTEN_TIME):
    """now_hhmm: the latest minute bar's OWN timestamp ("HH:MM", zero-padded,
    so lexical >= comparison works) - not wall-clock time, so force-flatten
    is tied to the market's own data instead of the machine's clock.

    side: "long" (bought first, profits when price rises) or "short"
    (融券賣出 first, profits when price FALLS - unrealized_return is
    sign-flipped so stop_loss_pct/take_profit_pct mean the same thing for
    both sides: a loss is still "unrealized_return <= stop_loss_pct")."""
    raw_return = (current_price - entry_price) / entry_price
    unrealized_return = raw_return if side == "long" else -raw_return
    conditions = {
        "stop_loss": unrealized_return <= stop_loss_pct,
        "take_profit": unrealized_return >= take_profit_pct,
        "force_flatten": now_hhmm is not None and now_hhmm >= force_flatten_time,
    }
    conditions["exit_signal"] = any(conditions.values())
    conditions["unrealized_return"] = round(unrealized_return, 4)
    return conditions
