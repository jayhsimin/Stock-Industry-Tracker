"""Backtest for the ORB day-trade strategy over real historical minute bars.

Reuses the EXACT same pure functions daytrade_monitor.py calls live
(minute_data.* and signals.check_entry/check_exit) - the only thing this
script does differently is replay them against a growing slice of
HISTORICAL bars instead of "today's bars so far", and keep an in-memory
trade ledger instead of positions_state.json (no persistence needed for a
one-shot backtest, and this must never touch the real paper-trading state).

No look-ahead: at simulated minute t, every signal input is computed from
bars up to and including t only - never later bars - by literally slicing
each stock's day to `day_df[day_df.index <= t]` before calling the same
functions trading/daytrade/minute_data.py already exposes.

Fees are modeled for a realistic P&L, not just raw price return:
  - 手續費 (brokerage) 0.1425% each way (buy and sell)
  - 證交稅 (transaction tax) 0.15% on sell - the HALVED rate that applies
    specifically to 現股當沖 (cash day-trade), not the normal 0.3%
These are standard published rates, not brokerage-specific discounts -
actual cost may be lower if the user's broker discounts commission.

Usage:
    python -m trading.daytrade.backtest --days 30
    python -m trading.daytrade.backtest --days 30 --codes 2330 2454  # small subset, for validation
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import db
from .. import executor
from . import config, minute_data, patterns, signals, watchlist

SIGNAL_TYPES = ("orb", "reversal", "reversal_both", "momentum", "rolling_breakout", "true_breakout", "volume_spike")
SIGNAL_LABELS = {
    "orb": "開盤區間突破(ORB)",
    "reversal": "K棒反轉型態-只做多(錘子線/吞噬)",
    "reversal_both": "K棒反轉型態-多空都做(含融券)",
    "momentum": "連續同向K棒(動能確認)",
    "rolling_breakout": "滾動範圍突破",
    "true_breakout": "真假突破確認法(分鐘級ATR/量能/RSI)",
    "volume_spike": "爆量觸發(無型態判斷，停損3%/停利8%)",
}

# 2026-10使用者指定：爆量進場用自己的停損/停利(-3%/+8%)，不是config.py
# 的全域預設(-2%/+3%) - 其他訊號都不受影響，只有列在這裡的signal_type
# 會在replay()裡被覆寫。
SIGNAL_EXIT_OVERRIDES = {
    "volume_spike": {"stop_loss_pct": -0.03, "take_profit_pct": 0.08},
}

FEE_RATE = 0.001425        # 手續費, each way
TAX_RATE_DAYTRADE_SELL = 0.0015  # 證交稅 on sell, 現股當沖 halved rate
# 融券借券費/利息等額外成本未建模 - 不同股票費率差異很大且不確定，先用跟現股
# 當沖同樣的費用結構近似，意味著空單這裡的淨損益可能比真實情況稍微樂觀一點。


def load_whole_market(conn):
    """The whole market (company_info), same universe build_watchlist.py
    scans - NOT concept_map. The 2026-10 backtest traced most losing
    trades back to thin/low-volatility concept_map stocks; the fix is
    picking candidates by liquidity+volatility instead, so the backtest
    has to draw from the same universe the live watchlist does."""
    codes = [r[0] for r in conn.execute('SELECT "公司代號" FROM company_info')]
    names = dict(conn.execute('SELECT "公司代號", "公司簡稱" FROM company_info'))
    return sorted(codes), names


def net_return(entry_price, exit_price, shares, side="long"):
    """side="long": buy at entry_price then sell at exit_price (tax on the
    sell, i.e. the exit). side="short": 融券賣出 at entry_price (tax on
    THIS leg, since selling is the opening move) then 融資買進回補 at
    exit_price (no tax, just the brokerage fee). Return % is relative to
    entry notional (entry_price*shares) for both sides, for a consistent,
    comparable scale - only the pnl numerator differs by side."""
    if side == "long":
        cost = entry_price * shares * (1 + FEE_RATE)
        proceeds = exit_price * shares * (1 - FEE_RATE - TAX_RATE_DAYTRADE_SELL)
    else:
        proceeds = entry_price * shares * (1 - FEE_RATE - TAX_RATE_DAYTRADE_SELL)
        cost = exit_price * shares * (1 + FEE_RATE)
    pnl = proceeds - cost
    return pnl, pnl / (entry_price * shares)


def precompute(bars_by_code, names, watchlist_size=config.WATCHLIST_SIZE, watchlist_weights=config.WATCHLIST_WEIGHTS):
    """The expensive, signal-independent setup: which trading days exist,
    each day's per-stock bars, and each day's watchlist (rebuilt from ONLY
    bars strictly before that day - no look-ahead at the stock-selection
    level, not just the minute-by-minute replay level). Computed ONCE and
    shared across every signal type's replay() call, since a 2000-stock
    whole-market scan is the dominant cost here, not the replay itself."""
    all_days = sorted(set().union(*(set(df.index.normalize()) for df in bars_by_code.values())))
    day_views_by_day = {}
    watchlists_by_day = {}

    for day in all_days:
        day_views = {code: df[df.index.normalize() == day] for code, df in bars_by_code.items()}
        day_views_by_day[day] = {c: d for c, d in day_views.items() if not d.empty}

        prior_bars = {code: df[df.index.normalize() < day] for code, df in bars_by_code.items()}
        prior_aggs = watchlist.previous_day_aggregates(prior_bars)
        today_watchlist = {c for c, _, _ in watchlist.score_and_rank(
            prior_aggs, names, top_n=watchlist_size, weights=watchlist_weights
        )} if prior_aggs else set()
        watchlists_by_day[day.date().isoformat()] = sorted(today_watchlist)

    return all_days, day_views_by_day, watchlists_by_day


def _entry_condition(signal_type, view):
    """Thin wrapper over patterns.evaluate_entry() - the single place both
    this backtest and daytrade_monitor.py's live loop decide entries, so
    the two can never drift apart."""
    return patterns.evaluate_entry(signal_type, view)


def replay(signal_type, all_days, day_views_by_day, watchlists_by_day, names):
    """Minute-by-minute entry/exit simulation for ONE signal type, reusing
    the day_views/watchlists precompute() already built. No look-ahead:
    every signal input at simulated minute t comes from
    `day_df[day_df.index <= t]` only, same discipline as
    daytrade_monitor.py's live "bars so far"."""
    open_positions = {}  # code -> {entry_price, entry_time, shares}
    trades = []
    exit_overrides = SIGNAL_EXIT_OVERRIDES.get(signal_type, {})

    for day in all_days:
        day_views = day_views_by_day[day]
        today_watchlist = watchlists_by_day[day.date().isoformat()]
        all_ts = sorted(set().union(*(set(d.index) for d in day_views.values()))) if day_views else []

        for t in all_ts:
            # --- exits ---
            for code in list(open_positions.keys()):
                day_df = day_views.get(code)
                if day_df is None:
                    continue
                view = day_df[day_df.index <= t]
                if view.empty:
                    continue
                close = minute_data.latest_close(view)
                now_hhmm = minute_data.latest_time_hhmm(view)
                pos = open_positions[code]
                cond = signals.check_exit(pos["entry_price"], close, now_hhmm, side=pos["side"], **exit_overrides)
                if cond["exit_signal"]:
                    reason = next(k for k in ("stop_loss", "take_profit", "force_flatten") if cond[k])
                    pnl, net_ret = net_return(pos["entry_price"], close, pos["shares"], side=pos["side"])
                    trades.append({
                        "code": code, "name": names.get(code, code), "side": pos["side"],
                        "entry_date": day.date().isoformat(), "entry_time": pos["entry_time"].strftime("%H:%M"),
                        "entry_price": pos["entry_price"], "exit_time": t.strftime("%H:%M"),
                        "exit_price": close, "exit_reason": reason,
                        "gross_return": cond["unrealized_return"], "net_return": round(net_ret, 4),
                        "net_pnl": round(pnl, 0),
                    })
                    del open_positions[code]

            # --- entries: only today's watchlist, not the whole market ---
            candidates = []
            for code in today_watchlist:
                day_df = day_views.get(code)
                if day_df is None or code in open_positions:
                    continue
                view = day_df[day_df.index <= t]
                if view.empty:
                    continue
                now_hhmm = minute_data.latest_time_hhmm(view)
                if now_hhmm is not None and now_hhmm >= config.FORCE_FLATTEN_TIME:
                    continue
                entry_signal, sort_key, close, side = _entry_condition(signal_type, view)
                if entry_signal:
                    candidates.append((code, close, sort_key, side))
            candidates.sort(key=lambda item: item[2], reverse=True)

            running_cost = sum(p["entry_price"] * p["shares"] for p in open_positions.values())
            for code, close, _, side in candidates:
                cost = close * config.ORDER_LOT_SHARES
                if running_cost + cost > config.MAX_TOTAL_POSITION_COST_NTD:
                    continue
                open_positions[code] = {"entry_price": close, "entry_time": t,
                                         "shares": config.ORDER_LOT_SHARES, "side": side}
                running_cost += cost

        # end-of-day safety net: force_flatten should have closed everything
        # by 13:00 already, but if a stock's last bar that day is before
        # 13:00 (early halt) there'd be nothing left to trigger it - close
        # at the day's last available price rather than silently carrying
        # a position into the next day (which would misrepresent a
        # same-day-only strategy).
        for code in list(open_positions.keys()):
            day_df = day_views.get(code)
            if day_df is None or day_df.empty:
                continue
            close = minute_data.latest_close(day_df)
            pos = open_positions[code]
            pnl, net_ret = net_return(pos["entry_price"], close, pos["shares"], side=pos["side"])
            raw_return = (close - pos["entry_price"]) / pos["entry_price"]
            trades.append({
                "code": code, "name": names.get(code, code), "side": pos["side"],
                "entry_date": day.date().isoformat(), "entry_time": pos["entry_time"].strftime("%H:%M"),
                "entry_price": pos["entry_price"], "exit_time": day_df.index[-1].strftime("%H:%M"),
                "exit_price": close, "exit_reason": "end_of_day_safety_net",
                "gross_return": round(raw_return if pos["side"] == "long" else -raw_return, 4),
                "net_return": round(net_ret, 4), "net_pnl": round(pnl, 0),
            })
            del open_positions[code]

    return trades


def stats(trades):
    """Numeric summary used both by summarize()'s text block and by
    main()'s cross-signal comparison table. None if there were no trades."""
    if not trades:
        return None
    n = len(trades)
    wins = [t for t in trades if t["net_return"] > 0]
    total_pnl = sum(t["net_pnl"] for t in trades)
    avg_net_return = sum(t["net_return"] for t in trades) / n
    avg_gross_return = sum(t["gross_return"] for t in trades) / n
    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    return {
        "n": n, "win_rate": len(wins) / n, "total_pnl": total_pnl,
        "avg_net_return": avg_net_return, "avg_gross_return": avg_gross_return,
        "best": max(trades, key=lambda t: t["net_return"]),
        "worst": min(trades, key=lambda t: t["net_return"]),
        "reasons": reasons,
    }


def summarize(trades):
    s = stats(trades)
    if s is None:
        return "沒有任何交易訊號成立。"
    lines = [
        f"總交易次數：{s['n']}",
        f"勝率（扣手續費/證交稅後）：{int(round(s['win_rate']*s['n']))}/{s['n']} = {s['win_rate']*100:.1f}%",
        f"總損益（含手續費0.1425%雙邊+當沖證交稅0.15%）：NT${s['total_pnl']:,.0f}",
        f"平均每筆報酬（含費用）：{s['avg_net_return']*100:+.2f}%",
        f"平均每筆報酬（不含費用，純價格變動）：{s['avg_gross_return']*100:+.2f}%",
        f"最佳單筆：{s['best']['code']} {s['best']['name']} {s['best']['net_return']*100:+.2f}%（{s['best']['entry_date']}）",
        f"最差單筆：{s['worst']['code']} {s['worst']['name']} {s['worst']['net_return']*100:+.2f}%（{s['worst']['entry_date']}）",
        "出場原因分布：" + "、".join(f"{k}:{v}" for k, v in s["reasons"].items()),
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="calendar days back from today")
    parser.add_argument("--codes", nargs="*", default=None,
                         help="subset of stock codes (default: whole market, same universe build_watchlist.py scans)")
    parser.add_argument("--db", default=db.DB_PATH)
    parser.add_argument("--out", default="trading/daytrade/backtest_result.txt")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    all_codes, names = load_whole_market(conn)
    conn.close()
    codes = args.codes if args.codes else all_codes
    print(f"universe: {len(codes)} stocks")

    end = date.today()
    start = end - timedelta(days=args.days)

    api, live = executor.login(False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        bars_by_code = {}
        for code in codes:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            bars = minute_data.bars_for_range(api, contract, start.isoformat(), end.isoformat())
            if not bars.empty:
                bars_by_code[code] = bars
        api.logout()
    print(f"{len(bars_by_code)}/{len(codes)} stocks had historical minute bars")

    all_days, day_views_by_day, watchlists_by_day = precompute(bars_by_code, names)

    results = {}
    for signal_type in SIGNAL_TYPES:
        trades = replay(signal_type, all_days, day_views_by_day, watchlists_by_day, names)
        results[signal_type] = {"trades": trades, "stats": stats(trades)}
        print(f"[{signal_type}] {len(trades)} trades")

    lines = [
        f"=== 當沖訊號比較回測：{start.isoformat()} ~ {end.isoformat()}"
        f"（{len(all_days)}個交易日，全市場{len(bars_by_code)}檔股票，每日觀察清單{config.WATCHLIST_SIZE}檔）===",
        "",
        "-- 四種訊號總覽比較（同一份資料、同一套量增/流動性門檻，只有方向判斷不同）--",
    ]
    header = f"{'訊號':<24}{'交易數':>6}{'勝率':>8}{'總損益':>14}{'平均報酬(含費)':>14}{'平均報酬(純價格)':>16}"
    lines.append(header)
    for signal_type in SIGNAL_TYPES:
        s = results[signal_type]["stats"]
        label = SIGNAL_LABELS[signal_type]
        if s is None:
            lines.append(f"{label:<24}{'0':>6}{'-':>8}{'-':>14}{'-':>14}{'-':>16}")
        else:
            lines.append(
                f"{label:<24}{s['n']:>6}{s['win_rate']*100:>7.1f}%"
                f"{'NT$' + format(s['total_pnl'], ',.0f'):>14}"
                f"{s['avg_net_return']*100:>13.2f}%{s['avg_gross_return']*100:>15.2f}%"
            )

    lines += ["", "-- 每日觀察清單（依前一天流動性70%+振幅30%選出，四種訊號共用同一份）--"]
    for d, codes_that_day in watchlists_by_day.items():
        label = "、".join(f"{c}{names.get(c, '')}" for c in codes_that_day[:10])
        more = f" 等{len(codes_that_day)}檔" if len(codes_that_day) > 10 else ""
        lines.append(f"{d}: {label}{more}" if codes_that_day else f"{d}: （無，首日無前一天資料）")

    for signal_type in SIGNAL_TYPES:
        trades = results[signal_type]["trades"]
        lines += ["", f"-- {SIGNAL_LABELS[signal_type]} 詳細摘要 --", summarize(trades), "", "交易明細："]
        for t in trades:
            side_label = "多" if t.get("side", "long") == "long" else "空"
            lines.append(
                f"{t['entry_date']} {t['code']} {t['name']}｜[{side_label}]{t['entry_time']} 進場@{t['entry_price']:.2f} -> "
                f"{t['exit_time']} 出場@{t['exit_price']:.2f}｜原因:{t['exit_reason']}｜"
                f"淨報酬{t['net_return']*100:+.2f}%｜淨損益NT${t['net_pnl']:,.0f}"
            )

    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
