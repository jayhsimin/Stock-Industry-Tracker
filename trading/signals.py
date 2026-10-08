"""Entry/exit rule evaluation - pure functions, no Shioaji or database
access here on purpose, so the strategy logic itself can be unit-tested
with plain numbers (see trading/README.md's verification steps) without
touching a live connection.
"""
from . import config


def is_near_ma_support(close, mas, tolerance=config.MA_TOLERANCE):
    """mas: {period: ma_value} e.g. {5: 101.2, 20: 98.5, 60: 95.0}.
    True if close sits within `tolerance` (fractional, e.g. 0.02 = 2%) of
    ANY one of them - the user's rule is 回檔至月線/季線/周線任一條,
    i.e. OR across the three, not all three at once."""
    for ma in mas.values():
        if ma and abs(close - ma) / ma <= tolerance:
            return True
    return False


def ma_distance_pct(close, mas):
    """Smallest relative distance from close to any single MA in mas -
    the 回檔精準度 ranking input (smaller = closer = more precise pullback).
    Independent of is_near_ma_support's pass/fail tolerance check: a stock
    sitting exactly on its MA and one sitting right at the +/-2% edge both
    pass entry, but this distinguishes them for ranking."""
    distances = [abs(close - ma) / ma for ma in mas.values() if ma]
    return min(distances) if distances else None


def check_entry(close, volume_ratio, mas, theme_median_pct, trade_value=None,
                 volume_ratio_min=config.VOLUME_RATIO_MIN,
                 ma_tolerance=config.MA_TOLERANCE,
                 min_trade_value_ntd=config.MIN_TRADE_VALUE_NTD):
    """Returns every condition's pass/fail (not just the final verdict) so
    a daily report can show *why* a candidate did or didn't qualify.

    trade_value: today's cumulative turnover in NTD (Shioaji snapshot's
    total_amount) - an absolute liquidity floor, separate from
    volume_increase's relative volume_ratio (which a thinly-traded stock can
    clear on a tiny absolute base and still be too illiquid to trade)."""
    conditions = {
        "theme_trending_up": theme_median_pct is not None and theme_median_pct > 0,
        "near_ma_support": is_near_ma_support(close, mas, ma_tolerance),
        "volume_increase": volume_ratio is not None and volume_ratio >= volume_ratio_min,
        "liquidity_ok": trade_value is not None and trade_value >= min_trade_value_ntd,
    }
    conditions["entry_signal"] = all(conditions.values())
    conditions["ma_distance_pct"] = ma_distance_pct(close, mas)
    return conditions


def _normalize(values):
    """Min-max scale to [0, 1]. All-equal input (including a single value)
    scores everyone 0.5 - there's nothing to rank them by, so ties stand."""
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def rank_candidates(candidates, weights=config.RANK_WEIGHTS):
    """candidates: list of dicts each with 'theme_pct', 'volume_ratio',
    'ma_distance_pct' (plus whatever else the caller wants carried through,
    e.g. 'code'/'name'). Attaches a 'score' to each dict and returns the
    list sorted descending by it - used when more stocks pass check_entry
    than the budget cap allows, so the strongest candidates buy first
    instead of whichever stock code happens to be smallest.

    Each factor is normalized across THIS candidate set (today's qualifiers),
    not against a fixed scale - theme %change and volume_ratio have no
    natural ceiling, so relative ranking within the day's pool is what's
    meaningful. ma_distance_pct is inverted first (smaller distance = better)."""
    if not candidates:
        return []
    theme_scores = _normalize([c["theme_pct"] for c in candidates])
    volume_scores = _normalize([c["volume_ratio"] for c in candidates])
    ma_scores = _normalize([-c["ma_distance_pct"] for c in candidates])
    for c, t, v, m in zip(candidates, theme_scores, volume_scores, ma_scores):
        c["score"] = round(
            weights["theme"] * t + weights["volume"] * v + weights["ma_precision"] * m, 4
        )
    return sorted(candidates, key=lambda c: c["score"], reverse=True)


def check_exit(entry_price, current_price, days_held, theme_median_pct,
               stop_loss_pct=config.STOP_LOSS_PCT,
               take_profit_pct=config.TAKE_PROFIT_PCT,
               max_hold_days=config.MAX_HOLD_DAYS):
    """Any one of these firing is enough to exit - the user's rule is OR,
    not AND, across stop-loss/take-profit/max-hold/theme-weakened."""
    unrealized_return = (current_price - entry_price) / entry_price
    conditions = {
        "stop_loss": unrealized_return <= stop_loss_pct,
        "take_profit": unrealized_return >= take_profit_pct,
        "max_hold_days": days_held > max_hold_days,
        "theme_weakened": theme_median_pct is not None and theme_median_pct < 0,
    }
    conditions["exit_signal"] = any(conditions.values())
    conditions["unrealized_return"] = round(unrealized_return, 4)
    return conditions
