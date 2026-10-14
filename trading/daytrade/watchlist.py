"""Pre-market stock selection for the day-trade strategy: pick the day's
watchlist from the PREVIOUS trading day's liquidity + volatility, across
the whole market (not concept_map's theme-curated ~171 stocks, which were
picked for business-relationship reasons, not for being actively-traded/
volatile day-trade candidates - this was the root cause the 2026-10
backtest traced bad trades back to).

Deliberately not twstock.db for "yesterday's" data: fetch_daily.py has
never run on a daily schedule (see CLAUDE.md), so twstock.db's stock_price
table is often missing or stale for the actual most-recent trading day.
Shioaji kbars() is used instead, consistently with the rest of this
module - same reasoning as minute_data.py avoiding snapshot().
"""
import json
import os
from datetime import date

import pandas as pd

from . import config


def daily_aggregate(bars):
    """Resample one stock's minute bars to one row per trading day:
    Open/High/Low/Close (first/max/min/last) and Volume/Amount (sum).
    Returns a DataFrame indexed by date, oldest first."""
    if bars.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume", "Amount"])
    daily = bars.resample("1D").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last",
        "Volume": "sum", "Amount": "sum",
    })
    return daily.dropna(subset=["Open"])


def _normalize(values):
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def score_and_rank(agg_by_code, names, top_n=config.WATCHLIST_SIZE, weights=config.WATCHLIST_WEIGHTS):
    """agg_by_code: {code: {"amount": float, "range_pct": float}} - ONE
    day's figures per stock (amount = that day's turnover, range_pct =
    (high-low)/open that day). Returns [(code, name, score), ...] for the
    top `top_n`, descending by score. Liquidity (成交金額) and volatility
    ((最高-最低)/開盤) are each min-max normalized across the whole
    universe passed in, then combined by `weights`."""
    codes = list(agg_by_code.keys())
    if not codes:
        return []
    amounts = [agg_by_code[c]["amount"] for c in codes]
    ranges = [agg_by_code[c]["range_pct"] for c in codes]
    liq_scores = _normalize(amounts)
    vol_scores = _normalize(ranges)
    scored = []
    for code, liq, vol in zip(codes, liq_scores, vol_scores):
        score = weights["liquidity"] * liq + weights["volatility"] * vol
        scored.append((code, names.get(code, code), round(score, 4)))
    scored.sort(key=lambda item: item[2], reverse=True)
    return scored[:top_n]


def previous_day_aggregates(bars_by_code):
    """{code: {"amount", "range_pct"}} for each code's MOST RECENT
    complete trading day in its bars - used by build_watchlist.py (bars
    span just the last couple of calendar days) and by backtest.py (bars
    span the whole window; caller passes only the slice up to the target
    day)."""
    result = {}
    for code, bars in bars_by_code.items():
        agg = daily_aggregate(bars)
        if agg.empty:
            continue
        last = agg.iloc[-1]
        if not last["Open"]:
            continue
        result[code] = {
            "amount": float(last["Amount"]),
            "range_pct": float((last["High"] - last["Low"]) / last["Open"]),
        }
    return result


def save_cache(top, state_file=config.WATCHLIST_STATE_FILE, as_of=None):
    """top: [(code, name, score), ...] from score_and_rank(). Written by
    build_watchlist.py, read back by daytrade_monitor.py."""
    state = {
        "date": (as_of or date.today()).isoformat(),
        "codes": [c for c, _, _ in top],
        "detail": [{"code": c, "name": n, "score": s} for c, n, s in top],
    }
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return state_file


def load_cache(state_file=config.WATCHLIST_STATE_FILE, today=None):
    """Returns (codes, names) from today's cached watchlist, or (None, None)
    if there's no cache yet or it's from a previous day - daytrade_monitor.py
    must treat that as "nothing to trade today" rather than silently
    falling back to some other universe."""
    today = today or date.today().isoformat()
    if not os.path.exists(state_file):
        return None, None
    with open(state_file, encoding="utf-8") as f:
        state = json.load(f)
    if state.get("date") != today:
        return None, None
    names = {d["code"]: d["name"] for d in state.get("detail", [])}
    return state.get("codes", []), names
