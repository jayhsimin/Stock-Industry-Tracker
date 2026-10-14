"""Multi-stock selection, user-specified pipeline (not Claude-invented):
(1) must belong to the AI supply chain - concept_map's `AI伺服器` theme
plus upstream/downstream manufacturing themes (`ABF載板`/`PCB印刷電路板`/
`封測`, added 2026-10 after the original AI伺服器-only pool of 9 stocks
turned out too concentrated - see trading/decisions.json), (2) yesterday's
trading value must clear a "大量" bar, (3) among survivors, rank by
RELATIVE news discussion (today's cnyes mention count vs that stock's own
recent average - see news_api.discussion_ratio_ranking(), the same 量比-
style idea as minute_data.minute_volume_ratio, specifically to reduce the
raw-mention-count bias toward mega-caps that show up in nearly every
article regardless of a real same-day catalyst) and take the top N
(config.AI_CHAIN_SELECT_TOP_N = 10, widened from the original single-stock
version per user request).
"""
from datetime import date, timedelta

from . import config, minute_data, news_api, watchlist


def ai_chain_codes(conn):
    """Union of concept_map membership across config.AI_CHAIN_CONCEPTS -
    not a broader hand-wavy "anything AI-adjacent" set. Widening this
    further (記憶體/散熱/電源供應器 etc. are all arguably AI-chain too) is
    a deliberate decision to make with the user, not something to expand
    here on its own."""
    placeholders = ",".join("?" for _ in config.AI_CHAIN_CONCEPTS)
    rows = conn.execute(
        f"SELECT DISTINCT stock_id FROM concept_map WHERE concept IN ({placeholders})",
        config.AI_CHAIN_CONCEPTS,
    )
    return [r[0] for r in rows]


def _volume_filter(api, codes, as_of_date, min_volume_ntd):
    """{code: amount} for codes whose most recent trading day (strictly
    before as_of_date) cleared min_volume_ntd. Uses the same kbars-based
    daily aggregate watchlist.py uses for its liquidity scoring - not
    twstock.db, for the same staleness reasons documented there."""
    end = as_of_date - timedelta(days=1)
    start = end - timedelta(days=6)  # pad for weekends/holidays
    bars_by_code = {}
    for code in codes:
        contract = api.Contracts.Stocks.get(code)
        if contract is None:
            continue
        bars = minute_data.bars_for_range(api, contract, start.isoformat(), end.isoformat())
        if not bars.empty:
            bars_by_code[code] = bars
    aggs = watchlist.previous_day_aggregates(bars_by_code)
    return {code: a["amount"] for code, a in aggs.items() if a["amount"] >= min_volume_ntd}


def select_stocks(api, conn, as_of_date=None, min_volume_ntd=config.AI_CHAIN_MIN_VOLUME_NTD,
                   top_n=config.AI_CHAIN_SELECT_TOP_N):
    """Returns (selections, detail) where selections is a list of
    {code, name, reason} dicts (up to top_n, possibly fewer or empty),
    ranked by relative news discussion among volume-qualified candidates.
    detail explains the funnel at each step for logging/reporting."""
    as_of_date = as_of_date or date.today()
    names = dict(conn.execute('SELECT "公司代號", "公司簡稱" FROM company_info'))
    chain_codes = ai_chain_codes(conn)

    high_volume = _volume_filter(api, chain_codes, as_of_date, min_volume_ntd)
    if not high_volume:
        return [], {"step": "volume_filter", "reason": "AI供應鏈候選股裡沒有任何一檔前一天成交金額達標",
                    "chain_codes": chain_codes, "min_volume_ntd": min_volume_ntd}

    ranked = news_api.discussion_ratio_ranking(list(high_volume), as_of_date, top_n=top_n)
    if not ranked:
        # nothing in the news at all among volume-qualified candidates -
        # fall back to ranking by volume alone, flagged as such
        fallback = sorted(high_volume.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
        selections = [{"code": c, "name": names.get(c, c),
                       "reason": f"通過大量門檻但窗口內無新聞提及，依成交金額排序（NT${amt/1e8:.1f}億）"}
                      for c, amt in fallback]
        return selections, {"step": "volume_fallback", "candidates": list(high_volume)}

    selections = [{"code": c, "name": names.get(c, c),
                   "reason": f"相對新聞討論度{ratio:.1f}倍（今日{today_c}篇 vs 過去{config.NEWS_BASELINE_DAYS}天均{avg}篇）"}
                  for c, ratio, today_c, avg in ranked]
    return selections, {"step": "news_ratio", "candidates": list(high_volume), "ranked": ranked}
