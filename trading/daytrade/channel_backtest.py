"""Day-trade conversion of the trendline-channel strategy
(trading/channel_backtest.py) - same rule (connect relative swing highs
into an upper trendline, swing lows into a lower trendline, buy on a
break below the lower line, sell on a break above the upper line), but
computed from MINUTE bars within a single trading day instead of daily
bars across days, and force-flattened by config.FORCE_FLATTEN_TIME (no
overnight hold) like every other day-trade signal in this module.

Reuses trading/channel_backtest.py's compute_channel() UNCHANGED - it
only cares about High/Low/Close columns and row order, not what time
granularity they're at, so feeding it one day's minute bars instead of a
multi-day daily-resampled frame gives the same no-look-ahead swing-point-
confirmation logic for free. PIVOT_K is re-tuned for minutes (see
PIVOT_K_MINUTES below) - the daily version's ±3 TRADING DAYS has no
natural minute equivalent, so this is a deliberately separate constant.

Universe: whole market via trading/daytrade/watchlist.py's liquidity+
volatility ranking, reusing trading/daytrade/backtest.py's
load_whole_market()/precompute() directly - NOT concept_map, same reason
as every other day-trade signal here (see that module's docstring: thin
concept_map stocks were traced to bad day-trade entries before).

Usage:
    python -m trading.daytrade.channel_backtest --days 30
    python -m trading.daytrade.channel_backtest --days 30 --codes 2330 2454
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import pandas as pd

import db
from .. import channel_backtest as swing_cb
from .. import executor
from . import config
from .backtest import load_whole_market, precompute

FEE_RATE = 0.001425
TAX_RATE_DAYTRADE_SELL = 0.0015  # halved 證交稅 for 現股當沖

PIVOT_K_MINUTES = 5  # minutes on each side required to confirm an intraday swing point


def net_return(entry_price, exit_price, shares):
    cost = entry_price * shares * (1 + FEE_RATE)
    proceeds = exit_price * shares * (1 - FEE_RATE - TAX_RATE_DAYTRADE_SELL)
    pnl = proceeds - cost
    return pnl, pnl / cost


def replay(all_days, day_views_by_day, watchlists_by_day, names):
    """Minute-by-minute, no-look-ahead - mirrors trading/daytrade/
    backtest.py's replay() structure, with the channel rule swapped in
    for patterns.evaluate_entry()."""
    open_positions = {}
    trades = []

    for day in all_days:
        day_views = day_views_by_day[day]
        today_watchlist = watchlists_by_day[day.date().isoformat()]

        # compute_channel() once per stock per day (not per minute) - it
        # already only uses bars up to and including each row's own
        # position (confirmed with a PIVOT_K_MINUTES delay), so this is
        # the same no-look-ahead guarantee the daily version has.
        channels = {}
        for code in today_watchlist:
            day_df = day_views.get(code)
            if day_df is None or len(day_df) < 2 * PIVOT_K_MINUTES + 10:
                continue
            channels[code] = swing_cb.compute_channel(day_df, k=PIVOT_K_MINUTES)

        all_ts = sorted(set().union(*(set(d.index) for d in day_views.values()))) if day_views else []

        for t in all_ts:
            now_hhmm = t.strftime("%H:%M")
            force_flatten = now_hhmm >= config.FORCE_FLATTEN_TIME

            # --- exits: upper-line break, or force-flatten ---
            for code in list(open_positions.keys()):
                ch = channels.get(code)
                if ch is None or t not in ch.index:
                    continue
                row = ch.loc[t]
                close = float(row["Close"])
                pos = open_positions[code]
                reason = None
                if force_flatten:
                    reason = "force_flatten"
                elif pd.notna(row["upper_line"]) and close > row["upper_line"]:
                    reason = "upper_line_break"
                if reason:
                    pnl, net_ret = net_return(pos["entry_price"], close, pos["shares"])
                    trades.append({
                        "code": code, "name": names.get(code, code),
                        "entry_date": day.date().isoformat(), "entry_time": pos["entry_time"].strftime("%H:%M"),
                        "entry_price": pos["entry_price"], "exit_time": t.strftime("%H:%M"),
                        "exit_price": close, "exit_reason": reason,
                        "net_return": round(net_ret, 4), "net_pnl": round(pnl, 0),
                    })
                    del open_positions[code]

            if force_flatten:
                continue  # no new entries once flattening has started

            # --- entries: lower-line break, only today's watchlist ---
            running_cost = sum(p["entry_price"] * p["shares"] for p in open_positions.values())
            for code in today_watchlist:
                if code in open_positions:
                    continue
                ch = channels.get(code)
                if ch is None or t not in ch.index:
                    continue
                row = ch.loc[t]
                if pd.isna(row["lower_line"]) or row["Close"] >= row["lower_line"]:
                    continue
                close = float(row["Close"])
                cost = close * config.ORDER_LOT_SHARES
                if running_cost + cost > config.MAX_TOTAL_POSITION_COST_NTD:
                    continue
                open_positions[code] = {"entry_price": close, "entry_time": t, "shares": config.ORDER_LOT_SHARES}
                running_cost += cost

        # end-of-day safety net - force_flatten should already have closed
        # everything by FORCE_FLATTEN_TIME, this only catches a stock whose
        # last bar that day is earlier than that (e.g. an early halt)
        for code in list(open_positions.keys()):
            ch = channels.get(code)
            if ch is None or ch.empty:
                continue
            last = ch.iloc[-1]
            close = float(last["Close"])
            pos = open_positions[code]
            pnl, net_ret = net_return(pos["entry_price"], close, pos["shares"])
            trades.append({
                "code": code, "name": names.get(code, code),
                "entry_date": day.date().isoformat(), "entry_time": pos["entry_time"].strftime("%H:%M"),
                "entry_price": pos["entry_price"], "exit_time": ch.index[-1].strftime("%H:%M"),
                "exit_price": close, "exit_reason": "end_of_day_safety_net",
                "net_return": round(net_ret, 4), "net_pnl": round(pnl, 0),
            })
            del open_positions[code]

    return trades


def summarize(trades):
    if not trades:
        return "沒有任何交易（這段期間觀察清單裡沒有股票跌破下緣趨勢線）。"
    n = len(trades)
    wins = sum(1 for t in trades if t["net_return"] > 0)
    total_pnl = sum(t["net_pnl"] for t in trades)
    avg_ret = sum(t["net_return"] for t in trades) / n
    by_reason = {}
    for t in trades:
        by_reason.setdefault(t["exit_reason"], []).append(t)
    reason_lines = "\n".join(
        f"  {r}: {len(ts)}筆｜勝率{sum(1 for t in ts if t['net_return'] > 0)}/{len(ts)}｜"
        f"平均報酬{sum(t['net_return'] for t in ts) / len(ts) * 100:+.2f}%"
        for r, ts in sorted(by_reason.items())
    )
    return (f"總交易次數：{n}｜勝率：{wins}/{n}={wins/n*100:.1f}%｜"
            f"總損益（含手續費0.1425%雙邊+當沖證交稅0.15%）：NT${total_pnl:,.0f}｜"
            f"平均報酬：{avg_ret*100:+.2f}%\n-- 依出場原因 --\n{reason_lines}")


def daily_breakdown(trades):
    if not trades:
        return "（無交易）"
    by_day = {}
    for t in trades:
        by_day.setdefault(t["entry_date"], []).append(t)
    lines = []
    for day, ts in sorted(by_day.items()):
        n = len(ts)
        wins = sum(1 for t in ts if t["net_return"] > 0)
        pnl = sum(t["net_pnl"] for t in ts)
        lines.append(f"  {day}：{n}筆｜勝率{wins}/{n}={wins/n*100:.0f}%｜損益NT${pnl:,.0f}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="calendar days back from today")
    parser.add_argument("--codes", nargs="*", default=None,
                         help="subset of stock codes (default: whole market, same universe build_watchlist.py scans)")
    parser.add_argument("--db", default=db.DB_PATH)
    parser.add_argument("--out", default="trading/daytrade/channel_backtest_result.txt")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    all_codes, names = load_whole_market(conn)
    conn.close()
    codes = args.codes if args.codes else all_codes
    print(f"universe: {len(codes)} stocks")

    end = date.today()
    start = end - timedelta(days=args.days)

    from . import minute_data
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
    print(f"{len(all_days)} trading days, watchlist size {config.WATCHLIST_SIZE}")

    trades = replay(all_days, day_views_by_day, watchlists_by_day, names)

    lines = [
        f"=== 趨勢線通道策略 - 當沖版（分鐘級，跌破下緣買進/突破上緣賣出，{config.FORCE_FLATTEN_TIME}強制平倉）："
        f"{start.isoformat()} ~ {end.isoformat()}（{len(all_days)}個交易日，全市場{len(bars_by_code)}檔股票，"
        f"每日觀察清單{config.WATCHLIST_SIZE}檔）===",
        "",
        f"擺動點判定窗口=±{PIVOT_K_MINUTES}分鐘｜單筆上限=1張｜資金上限=NT${config.MAX_TOTAL_POSITION_COST_NTD:,.0f}",
        "",
        summarize(trades),
        "",
        "-- 按日分期檢視 --",
        daily_breakdown(trades),
        "",
        "-- 交易明細 --",
    ]
    for t in sorted(trades, key=lambda x: (x["entry_date"], x["entry_time"])):
        lines.append(
            f"{t['entry_date']} {t['entry_time']} {t['code']}{t['name']}｜進場@{t['entry_price']:.2f} -> "
            f"{t['exit_time']} 出場@{t['exit_price']:.2f}｜{t['exit_reason']}｜"
            f"{t['net_return']*100:+.2f}%｜NT${t['net_pnl']:,.0f}"
        )

    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
