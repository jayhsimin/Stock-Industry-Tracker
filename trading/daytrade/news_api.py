"""Programmatic news-driven stock selection via cnyes (鉅亨網)'s public
JSON news API - no login/key needed, confirmed via direct testing
(2026-10-11/12) to support historical date-range queries (startAt/endAt
as unix timestamps) and per-article stock-code tags.

This is the "has an API, a future unattended agent could call this
exact same function" track, as distinct from news_watchlist.py's
current manual-WebSearch path. Both write to the SAME cache shape via
news_watchlist.save_cache()/load_cache() - daytrade_monitor.py and
backtest.py don't need to know which track produced a given day's
selection.

Endpoint: https://api.cnyes.com/media/api/v1/newslist/category/tw_stock_news
Confirmed fields per article: `stock` (list of tagged codes - 4-digit
numeric = TW equity, everything else like "US-..." or ETF codes like
"00712" is NOT a TW single-stock code and is excluded), `publishAt`
(unix timestamp), `title`.
"""
import json
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta

from . import config

API_URL = "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock_news"


def _is_tw_stock_code(code):
    """4-digit numeric TW equity code - excludes ETFs (5-digit 00xxxA
    codes, AND plain 4-digit 00xx codes like 0050/0056 - TWSE reserves the
    00-prefix for ETFs/ETNs, not operating companies) and foreign tickers
    (US-...). Found by testing: 2026-10-08's real API response included
    "0050" (元大台灣50) until this leading-zero check was added."""
    return code.isdigit() and len(code) == 4 and not code.startswith("00")


def fetch_window(start_dt, end_dt, max_pages=10, page_size=100):
    """All tw_stock_news articles with publishAt in [start_dt, end_dt)
    (both datetime). Paginates until exhausted or max_pages reached."""
    start_ts, end_ts = int(start_dt.timestamp()), int(end_dt.timestamp())
    articles = []
    for page in range(1, max_pages + 1):
        url = f"{API_URL}?startAt={start_ts}&endAt={end_ts}&limit={page_size}&page={page}"
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        items = data["items"]["data"]
        articles.extend(items)
        if page >= data["items"]["last_page"] or not items:
            break
        time.sleep(0.2)  # be polite - this is someone else's free public API
    return articles


def window_counts(articles, candidate_codes=None):
    """{code: count} of how many articles tagged each code in `articles`.
    If candidate_codes given, restrict to just those (and skip the
    is-this-a-real-TW-code check, since the caller already knows its own
    universe); otherwise counts every valid TW code found."""
    counts = Counter()
    for a in articles:
        for code in a.get("stock", []):
            if candidate_codes is not None:
                if code in candidate_codes:
                    counts[code] += 1
            elif _is_tw_stock_code(code):
                counts[code] += 1
    return counts


def discussion_ratio_ranking(candidate_codes, trading_day_date, baseline_days=config.NEWS_BASELINE_DAYS, top_n=10):
    """Relative discussion volume per candidate: today's window mention
    count divided by that stock's own average mention count over the
    preceding `baseline_days` same-length windows - the same "today vs
    own recent average" idea already used for 量比 elsewhere in this
    project (minute_data.minute_volume_ratio), applied to news instead of
    volume, specifically to reduce the raw-count bias toward mega-caps
    that get name-dropped in nearly every article regardless of a real
    same-day catalyst (see trading/decisions.json's news-API findings).
    Returns [(code, ratio, today_count, baseline_avg), ...] sorted desc,
    only for codes with today_count > 0 - a stock with zero mentions today
    has nothing to rank, regardless of its baseline."""
    start, end = trading_day_window(trading_day_date)
    today_counts = window_counts(fetch_window(start, end), candidate_codes)

    baseline_counts = {code: [] for code in candidate_codes}
    cursor_day = trading_day_date
    collected = 0
    while collected < baseline_days:
        cursor_day -= timedelta(days=1)
        if cursor_day.weekday() >= 5:
            continue
        b_start, b_end = trading_day_window(cursor_day)
        b_counts = window_counts(fetch_window(b_start, b_end), candidate_codes)
        for code in candidate_codes:
            baseline_counts[code].append(b_counts.get(code, 0))
        collected += 1

    ranked = []
    for code in candidate_codes:
        today_c = today_counts.get(code, 0)
        if today_c == 0:
            continue
        baseline_avg = sum(baseline_counts[code]) / len(baseline_counts[code]) if baseline_counts[code] else 0
        ratio = today_c / max(baseline_avg, 0.5)  # floor: avoids a near-zero baseline producing an inflated/infinite ratio
        ranked.append((code, ratio, today_c, round(baseline_avg, 2)))
    ranked.sort(key=lambda r: r[1], reverse=True)
    return ranked[:top_n]


def rank_by_discussion(articles, top_n=config.NEWS_WATCHLIST_SIZE):
    """{code: count} of how many articles tagged each TW stock code,
    sorted descending. This is the API track's literal, mechanical
    definition of "討論度" (discussion volume) - article count per code
    in the window, nothing more clever than that."""
    counts = Counter()
    titles_by_code = {}
    for a in articles:
        for code in a.get("stock", []):
            if _is_tw_stock_code(code):
                counts[code] += 1
                titles_by_code.setdefault(code, []).append(a["title"])
    ranked = counts.most_common(top_n)
    return ranked, titles_by_code


def trading_day_window(trading_day_date, market_close_hhmm="13:30", market_open_hhmm="09:00"):
    """[previous close, this day's open) as (start, end) datetimes -
    "overnight catalyst news" window definition shared by every news-
    selection caller (select_for_trading_day() here, ai_chain_watchlist.py's
    news-heat ranking) so they all mean the same thing by "discussion
    volume in the window". Doesn't know TWSE holidays - same approximation
    caveat as trading/positions.py's days_held()."""
    end = datetime.combine(trading_day_date, datetime.strptime(market_open_hhmm, "%H:%M").time())
    prev_day = trading_day_date - timedelta(days=1)
    while prev_day.weekday() >= 5:
        prev_day -= timedelta(days=1)
    start = datetime.combine(prev_day, datetime.strptime(market_close_hhmm, "%H:%M").time())
    return start, end


def select_for_trading_day(trading_day_date, market_close_hhmm="13:30", market_open_hhmm="09:00",
                            top_n=config.NEWS_WATCHLIST_SIZE):
    """trading_day_date: date of the session to pick stocks FOR. Returns a
    list of dicts matching news_watchlist.save_cache()'s `selections` shape."""
    start, end = trading_day_window(trading_day_date, market_close_hhmm, market_open_hhmm)
    articles = fetch_window(start, end)
    ranked, titles_by_code = rank_by_discussion(articles, top_n)

    selections = []
    for code, count in ranked:
        selections.append({
            "code": code,
            "name": None,  # filled in by the caller from company_info - this module doesn't touch twstock.db
            "headline": titles_by_code[code][0],
            "source_url": "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock_news",
            "published_at": f"article_count={count} in window",
            "reason": f"cnyes tw_stock_news：窗口內共{count}篇報導提及此股（{len(titles_by_code[code])}則標題，"
                      f"例如「{titles_by_code[code][0]}」）",
        })
    return selections, len(articles)
