"""Entry/exit logic for the MA5/10/20 支撐/破位 strategy - pure functions,
no Shioaji/DB access, same convention as trading/signals.py so this can be
unit-tested with fake data the same way.

Concept (user-specified, grounded against moving-average support/
resistance research - see decisions.json for the sources): while price
holds above a moving average, it tends to act as dynamic support - dips
down to touch it get bought. Once price closes cleanly below EVERY MA that
was acting as support, the line(s) flip from support to resistance and
the decline tends to continue until the stock finds a new base - so this
treats a full breakdown as its own hard exit, separate from (and often
firing before) a plain %-based stop-loss.
"""
from . import config


def touched_mas(close, mas, tolerance=config.MA_TOUCH_TOLERANCE):
    """{period: ma_value} for every MA the close is within `tolerance` of."""
    return {p: ma for p, ma in mas.items() if ma and abs(close - ma) / ma <= tolerance}


def check_entry(prev_close, close, mas, volume_ratio, trade_value,
                 touch_tolerance=config.MA_TOUCH_TOLERANCE,
                 volume_ratio_min=config.VOLUME_RATIO_MIN,
                 min_trade_value_ntd=config.MIN_TRADE_VALUE_NTD):
    """mas: {5: ma5, 10: ma10, 20: ma20} (periods matching config.MA_PERIODS).

    2026-10 redesign (see decisions.json): a 1-month backtest of the
    original same-day version (touch AND reclaim on the same close) came
    back at -1.47% avg return / 21.4% win rate, with its OWN support_broken
    exit firing on HALF of all trades at 0% win - i.e. most "bounces" were
    single-day noise that immediately failed. This version requires the
    bounce to show real follow-through across two days instead of being
    confirmed by one bar:
    - touched_support: YESTERDAY's close was within touch_tolerance of at
      least one MA (the pullback that tested support)
    - reclaimed_support: TODAY's close is both higher than yesterday's
      close AND at/above every MA yesterday touched - the bounce has to
      actually follow through, not just still be hovering near the line
    - volume_confirm / liquidity_ok: checked on today (the confirmation
      day), same convention as every other strategy in this project."""
    touched = touched_mas(prev_close, mas, touch_tolerance)
    conditions = {
        "touched_support": bool(touched),
        "reclaimed_support": bool(touched) and close > prev_close
                              and all(close >= ma for ma in touched.values()),
        "volume_confirm": volume_ratio is not None and volume_ratio >= volume_ratio_min,
        "liquidity_ok": trade_value is not None and trade_value >= min_trade_value_ntd,
    }
    conditions["entry_signal"] = all(conditions.values())
    return conditions


def check_exit(entry_price, current_price, days_held, mas,
                stop_loss_pct=config.STOP_LOSS_PCT,
                take_profit_pct=config.TAKE_PROFIT_PCT,
                max_hold_days=config.MAX_HOLD_DAYS):
    """support_broken: current_price is below EVERY live MA (not just one) -
    every moving average that was available has flipped to resistance.
    Fires independently of stop_loss_pct - the whole point of this
    strategy is to exit a failed bounce on the breakdown itself rather
    than wait for a fixed loss threshold, since the research says that
    kind of move tends to keep going."""
    valid_mas = [ma for ma in mas.values() if ma]
    unrealized_return = (current_price - entry_price) / entry_price
    conditions = {
        "stop_loss": unrealized_return <= stop_loss_pct,
        "take_profit": unrealized_return >= take_profit_pct,
        "max_hold_days": days_held > max_hold_days,
        "support_broken": bool(valid_mas) and all(current_price < ma for ma in valid_mas),
    }
    conditions["exit_signal"] = any(conditions.values())
    conditions["unrealized_return"] = round(unrealized_return, 4)
    return conditions
