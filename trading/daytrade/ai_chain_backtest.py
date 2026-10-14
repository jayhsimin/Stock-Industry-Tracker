"""Backtest for --source ai_chain: each historical trading day, replay the
SAME pipeline ai_chain_watchlist.select_stocks() uses live (AI供應鏈
concept_map membership across config.AI_CHAIN_CONCEPTS -> prior-day 大量
filter -> top N by RELATIVE news-discussion ratio among survivors), then
replay K棒反轉型態 entry/exit for each selected stock on that day. No
look-ahead: the volume filter only ever sees bars strictly before the
target day; the relative-ratio baseline only ever looks at PRECEDING
days' mention counts, never the target day's own future.

News mention counts are precomputed ONCE per calendar day across the
whole backtest window (one cnyes fetch per day, not one per
target-day-times-baseline-days) and reused for every day's ratio
calculation that needs them as a baseline - avoids redundant API calls
across a multi-week backtest.

Usage:
    python -m trading.daytrade.ai_chain_backtest --days 30
    python -m trading.daytrade.ai_chain_backtest --days 30 --signal reversal_both
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import db
from .. import executor
from . import ai_chain_watchlist, config, minute_data, news_api, patterns, signals, watchlist

FEE_RATE = 0.001425
TAX_RATE_DAYTRADE_SELL = 0.0015


def net_return(entry_price, exit_price, shares, side="long"):
    if side == "long":
        cost = entry_price * shares * (1 + FEE_RATE)
        proceeds = exit_price * shares * (1 - FEE_RATE - TAX_RATE_DAYTRADE_SELL)
    else:
        proceeds = entry_price * shares * (1 - FEE_RATE - TAX_RATE_DAYTRADE_SELL)
        cost = exit_price * shares * (1 + FEE_RATE)
    pnl = proceeds - cost
    return pnl, pnl / (entry_price * shares)


def precompute_news_counts(chain_codes, calendar_days):
    """{calendar_date: {code: count}} - one cnyes fetch per calendar day
    (including weekends, since a Monday's baseline needs Friday/weekend
    windows too), reused as both "today" and "baseline" lookups by
    select_for_day() so nothing gets fetched twice."""
    cache = {}
    for d in calendar_days:
        start, end = news_api.trading_day_window(d)
        articles = news_api.fetch_window(start, end)
        cache[d] = news_api.window_counts(articles, chain_codes)
    return cache


def select_for_day(bars_by_code, day, news_cache, min_volume_ntd=config.AI_CHAIN_MIN_VOLUME_NTD,
                    baseline_days=config.NEWS_BASELINE_DAYS, top_n=config.AI_CHAIN_SELECT_TOP_N):
    """Same logic as ai_chain_watchlist.select_stocks(), fed pre-fetched
    bars/news counts instead of calling Shioaji/cnyes live for each day."""
    prior_bars = {code: df[df.index.normalize() < day] for code, df in bars_by_code.items()}
    aggs = watchlist.previous_day_aggregates(prior_bars)
    high_volume = {c for c, a in aggs.items() if a["amount"] >= min_volume_ntd}
    if not high_volume:
        return [], "no_volume"

    day_date = day.date()
    today_counts = news_cache.get(day_date, {})

    baseline_days_list = []
    cursor = day_date
    while len(baseline_days_list) < baseline_days:
        cursor -= timedelta(days=1)
        if cursor.weekday() >= 5:
            continue
        baseline_days_list.append(cursor)

    ranked = []
    for code in high_volume:
        today_c = today_counts.get(code, 0)
        if today_c == 0:
            continue
        baseline_vals = [news_cache.get(d, {}).get(code, 0) for d in baseline_days_list]
        baseline_avg = sum(baseline_vals) / len(baseline_vals) if baseline_vals else 0
        ratio = today_c / max(baseline_avg, 0.5)
        ranked.append((code, ratio, today_c, baseline_avg))
    ranked.sort(key=lambda r: r[1], reverse=True)

    if not ranked:
        fallback = sorted(high_volume, key=lambda c: aggs[c]["amount"], reverse=True)[:top_n]
        return fallback, "volume_fallback"
    return [code for code, *_ in ranked[:top_n]], "news_ratio"


def replay_single_day(signal_type, bars_by_code, code, name, day,
                       target_exposure_ntd=config.AI_CHAIN_TARGET_EXPOSURE_NTD,
                       max_price_multiplier=config.AI_CHAIN_MAX_PRICE_MULTIPLIER):
    """Entry/exit for ONE stock on ONE day - growing-slice replay, same
    no-look-ahead discipline as backtest.py. Position sizing is fixed-
    EXPOSURE (target_exposure_ntd), not fixed-lot: always a whole board
    lot (1000 shares - odd-lot's 3-min batching makes same-day round trips
    unreliable, see trading/daytrade/README.md), but skipped entirely if
    one lot would cost more than target_exposure_ntd*max_price_multiplier -
    a high-priced stock (2308台達電 etc.) simply isn't traded rather than
    forced into a lot size that blows past the target exposure."""
    df = bars_by_code.get(code)
    if df is None:
        return []
    day_bars = df[df.index.normalize() == day]
    if day_bars.empty:
        return []
    max_lot_cost = target_exposure_ntd * max_price_multiplier
    trades = []
    open_pos = None
    for i in range(2, len(day_bars) + 1):
        view = day_bars.iloc[:i]
        now_hhmm = minute_data.latest_time_hhmm(view)
        close = minute_data.latest_close(view)
        if open_pos is not None:
            cond = signals.check_exit(open_pos["entry_price"], close, now_hhmm, side=open_pos["side"])
            if cond["exit_signal"]:
                reason = next(k for k in ("stop_loss", "take_profit", "force_flatten") if cond[k])
                pnl, net_ret = net_return(open_pos["entry_price"], close, open_pos["shares"], open_pos["side"])
                trades.append({"code": code, "name": name, "date": day.date().isoformat(),
                                "side": open_pos["side"], "entry_time": open_pos["entry_time"],
                                "entry_price": open_pos["entry_price"], "exit_time": now_hhmm,
                                "exit_price": close, "reason": reason, "net_return": net_ret, "net_pnl": pnl})
                open_pos = None
            continue
        if now_hhmm is not None and now_hhmm >= config.FORCE_FLATTEN_TIME:
            continue
        if close * config.ORDER_LOT_SHARES > max_lot_cost:
            continue  # too expensive for the target exposure - skip this stock today
        entry_signal, _, _, side = patterns.evaluate_entry(signal_type, view)
        if entry_signal:
            open_pos = {"entry_price": close, "entry_time": now_hhmm,
                        "shares": config.ORDER_LOT_SHARES, "side": side}
    if open_pos is not None:
        close = minute_data.latest_close(day_bars)
        pnl, net_ret = net_return(open_pos["entry_price"], close, open_pos["shares"], open_pos["side"])
        trades.append({"code": code, "name": name, "date": day.date().isoformat(),
                        "side": open_pos["side"], "entry_time": open_pos["entry_time"],
                        "exit_time": minute_data.latest_time_hhmm(day_bars), "exit_price": close,
                        "entry_price": open_pos["entry_price"], "reason": "end_of_day_safety_net",
                        "net_return": net_ret, "net_pnl": pnl})
    return trades


def summarize(trades):
    if not trades:
        return "沒有任何交易。"
    n = len(trades)
    wins = sum(1 for t in trades if t["net_return"] > 0)
    total_pnl = sum(t["net_pnl"] for t in trades)
    avg_ret = sum(t["net_return"] for t in trades) / n
    return (f"總交易次數：{n}｜勝率：{wins}/{n}={wins/n*100:.1f}%｜"
            f"總損益：NT${total_pnl:,.0f}｜平均報酬：{avg_ret*100:+.2f}%")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--signal", default="rolling_breakout",
                         choices=["reversal", "reversal_both", "rolling_breakout", "momentum", "orb"],
                         help="rolling_breakout (default, 2026-10 redesign): a news-catalyst stock "
                              "should be checked for CONTINUATION, not reversal - see "
                              "trading/decisions.json's 當沖策略整合設計 entry")
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default="trading/daytrade/ai_chain_backtest_result.txt")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db or db.DB_PATH)
    chain_codes = ai_chain_watchlist.ai_chain_codes(conn)
    names = dict(conn.execute('SELECT "公司代號", "公司簡稱" FROM company_info'))
    conn.close()
    print(f"AI供應鏈候選股（{'+'.join(config.AI_CHAIN_CONCEPTS)}）: {len(chain_codes)} 檔")

    end = date.today()
    start = end - timedelta(days=args.days)

    api, live = executor.login(False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        bars_by_code = {}
        for code in chain_codes:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            bars = minute_data.bars_for_range(api, contract, start.isoformat(), end.isoformat())
            if not bars.empty:
                bars_by_code[code] = bars
        api.logout()
    print(f"{len(bars_by_code)}/{len(chain_codes)} 檔有歷史分鐘K")

    all_days = sorted(set().union(*(set(df.index.normalize()) for df in bars_by_code.values())))

    # precompute news counts for every calendar day that could ever be
    # needed (trading days plus the baseline lookback before the first one)
    calendar_days = set()
    for day in all_days:
        calendar_days.add(day.date())
        cursor = day.date()
        for _ in range(config.NEWS_BASELINE_DAYS + 2):
            cursor -= timedelta(days=1)
            calendar_days.add(cursor)
    print(f"抓取 {len(calendar_days)} 個日曆天的新聞計數...")
    news_cache = precompute_news_counts(chain_codes, sorted(calendar_days))

    all_trades = []
    daily_picks = []
    for day in all_days:
        prior_has_data = any((df.index.normalize() < day).any() for df in bars_by_code.values())
        if not prior_has_data:
            daily_picks.append((day.date().isoformat(), [], "no_prior_data"))
            continue
        codes, reason = select_for_day(bars_by_code, day, news_cache)
        daily_picks.append((day.date().isoformat(), [f"{c}{names.get(c, c)}" for c in codes], reason))
        for code in codes:
            trades = replay_single_day(args.signal, bars_by_code, code, names.get(code, code), day)
            all_trades.extend(trades)

    lines = [
        f"=== AI供應鏈選股回測（擴大候選池+相對討論度+每天選{config.AI_CHAIN_SELECT_TOP_N}檔）："
        f"{start.isoformat()} ~ {end.isoformat()}（{len(all_days)}個交易日，候選池{len(bars_by_code)}檔，"
        f"訊號={args.signal}）===",
        "",
        summarize(all_trades),
        "",
        "-- 每日選股 --",
    ]
    for d, picks, reason in daily_picks:
        lines.append(f"{d}: {'、'.join(picks) if picks else '（無）'}｜原因:{reason}")
    lines += ["", "-- 交易明細 --"]
    for t in all_trades:
        side_label = "多" if t["side"] == "long" else "空"
        lines.append(
            f"{t['date']} {t['code']}{t['name']}｜[{side_label}]{t['entry_time']}@{t['entry_price']:.2f} -> "
            f"{t['exit_time']}@{t['exit_price']:.2f}｜{t['reason']}｜{t['net_return']*100:+.2f}%｜NT${t['net_pnl']:,.0f}"
        )

    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
