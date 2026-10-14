"""News-driven stock selection for the day-trade strategy - an alternative
to watchlist.py's liquidity+volatility selection. Finds bullish catalyst
news published between yesterday's market close and today's market open,
ranks candidate stocks by how many independent sources discuss them, and
picks the top N (user-specified: 5) for that day's trading.

Current limitation (by design, not an oversight): there is no automated
news-fetching code here. Finding and reading news requires a web search/
fetch step that only the Claude session doing this interactively can do
right now (see WebSearch/WebFetch) - a human-in-the-loop (well, Claude-in-
the-loop) step, run once each morning before today's build_watchlist-style
schedule would fire. save_cache()/load_cache() exist so that step's output
can still be consumed by daytrade_monitor.py exactly like
watchlist.load_cache() is. Turning the news search itself into something
that runs unattended (an autonomous agent step) is explicitly a LATER
step, not done here.
"""
import json
import os
from datetime import date

from . import config


def save_cache(selections, as_of=None, state_file=config.NEWS_WATCHLIST_STATE_FILE):
    """selections: [{code, name, headline, source_url, published_at, reason}, ...].
    Written by whoever (currently: a Claude session) did today's news
    search; read back by daytrade_monitor.py."""
    state = {
        "date": (as_of or date.today()).isoformat(),
        "codes": [s["code"] for s in selections],
        "selections": selections,
    }
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return state_file


def load_cache(state_file=config.NEWS_WATCHLIST_STATE_FILE, today=None):
    """Returns (codes, names, selections) from today's cached news
    watchlist, or (None, None, None) if there's no cache yet or it's from
    a previous day - same "fail closed, don't silently fall back to a
    different universe" contract as watchlist.load_cache()."""
    today = today or date.today().isoformat()
    if not os.path.exists(state_file):
        return None, None, None
    with open(state_file, encoding="utf-8") as f:
        state = json.load(f)
    if state.get("date") != today:
        return None, None, None
    selections = state.get("selections", [])
    names = {s["code"]: s["name"] for s in selections}
    return state.get("codes", []), names, selections
