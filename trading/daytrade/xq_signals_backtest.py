"""Backtest for day-trade methods sourced from XQ全球贏家's 當沖策略 article
category (https://www.xq.com.tw/xstrader/category/交易策略/當沖策略/, fetched 2026-10-15).

Of the 10 articles on that page: 2 are writing-tips/background posts with no
concrete rule (極短線交易策略撰寫技巧, 現股當沖腳本的奧義), 1 is a screening
indicator only with no complete entry/exit system and needs 外盤量(buy-side
executed volume) which Shioaji's kbars() doesn't carry historically - only
live snapshots have buy_volume/sell_volume, not usable for a 6-month backtest
without a tick-level pull this project doesn't have (尋找外盤量比重很高的股票) -
and 1 (開高不拉回後的創新高) had its rule text truncated in the fetched excerpt.
That leaves 4 implementable signals, each adapted here:

1. triple_red_signal - 當沖腳本2/多頭市場開盤暴量一分鐘K三連紅: bar 3 of the
   day, 3 non-decreasing closes, volume > 2x the prior bars' average, closing
   at the day's high so far. Source stop/profit: +-4%.
2. range_breakout_5min_signal - 當沖腳本1/五分鐘線狹幅整理後帶量突破: 100
   5-minute bars (spans multiple days - this script has the full per-stock
   series, not a day-fragmented view, so the lookback is genuine) range
   within 1.5%, breakout 0.5% above the range high, volume > 1.5x the
   range's average. Source gave NO exit rule - this backtest applies
   config.STOP_LOSS_PCT/TAKE_PROFIT_PCT as a fallback, clearly not from the
   source.
3. open_three_new_highs_signal - 雲端策略中心精進版之12/開盤五分鐘三創新高:
   bar 6 of the day, gap up vs yesterday's close, >=3 of bars 2-6 making a
   new high+close vs the bar before, 3-day change <3%. Source stop/profit:
   +-6%. The source's own backtest held positions OVERNIGHT ("屬於隔日沖的
   交易策略") - adapted here to close by FORCE_FLATTEN_TIME like every other
   signal in this module (this project's day-trade mechanism doesn't carry
   positions overnight), not a literal reproduction.
4. reversal_rebound_signal - 雲端策略中心精進版之11/開盤反轉買進訊號: gap
   down, rebound >=2% off the day's low while still above yesterday's close,
   prior 20-day decline >20% AND 10-day decline >7%, before 09:30, 5-day
   liquidity >=1000 lots, today's volume-so-far > 20% of yesterday's. Source
   day-trade exit: hold 48 five-minute bars (~4 hours) - adapted to just
   FORCE_FLATTEN_TIME since entering near the open and holding 4 hours
   reaches the close anyway.

Market-wide filters each source article also used (TAIEX close above its own
5/10-day moving average) are DROPPED here - would need a new index-data fetch
this project doesn't have; noted as a real simplification, not silently
applied as if tested.

Watchlist/universe, fees, and the whole-market fetch all reuse this
project's established day-trade conventions (trading/daytrade/watchlist.py's
liquidity+volatility ranking rebuilt from strictly-prior bars each day, same
FEE_RATE/TAX_RATE_DAYTRADE_SELL as trading/daytrade/backtest.py).

Usage:
    python -m trading.daytrade.xq_signals_backtest --days 180
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import db
from .. import executor
from . import config, minute_data, watchlist
from .backtest import load_whole_market

FEE_RATE = 0.001425
TAX_RATE_DAYTRADE_SELL = 0.0015

FALLBACK_STOP_LOSS_PCT = config.STOP_LOSS_PCT
FALLBACK_TAKE_PROFIT_PCT = config.TAKE_PROFIT_PCT

SIGNAL_EXITS = {
    "triple_red": {"stop_loss_pct": -0.04, "take_profit_pct": 0.04},
    "range_breakout_5min": {"stop_loss_pct": FALLBACK_STOP_LOSS_PCT, "take_profit_pct": FALLBACK_TAKE_PROFIT_PCT},
    "open_three_new_highs": {"stop_loss_pct": -0.06, "take_profit_pct": 0.06},
    "reversal_rebound": {"stop_loss_pct": -0.99, "take_profit_pct": 9.99},  # effectively force_flatten-only
}

SIGNAL_LABELS = {
    "triple_red": "開盤暴量一分鐘K三連紅",
    "range_breakout_5min": "五分鐘線狹幅整理後帶量突破",
    "open_three_new_highs": "開盤五分鐘三創新高",
    "reversal_rebound": "開盤反轉買進訊號",
}


def net_return(entry_price, exit_price, shares):
    cost = entry_price * shares * (1 + FEE_RATE)
    proceeds = exit_price * shares * (1 - FEE_RATE - TAX_RATE_DAYTRADE_SELL)
    pnl = proceeds - cost
    return pnl, pnl / cost


def triple_red_signal(today_bars):
    if len(today_bars) != 3:
        return False
    non_decreasing = bool((today_bars["Close"].diff().iloc[1:] >= 0).all())
    cur = today_bars.iloc[-1]
    prior_avg_vol = float(today_bars["Volume"].iloc[:-1].mean())
    volume_surge = prior_avg_vol > 0 and cur["Volume"] > prior_avg_vol * 2
    at_high = cur["Close"] >= today_bars["High"].max()
    return bool(non_decreasing and volume_surge and at_high)


def range_breakout_5min_signal(full_series_so_far, length=100, range_ratio=0.015,
                                breakout_ratio=0.005, vol_ratio=1.5):
    five = full_series_so_far.resample("5min").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna(subset=["Open"])
    if len(five) < length + 1:
        return False
    prior = five.iloc[-(length + 1):-1]
    cur = five.iloc[-1]
    range_high, range_low = prior["High"].max(), prior["Low"].min()
    if range_low <= 0:
        return False
    tight = (range_high - range_low) / range_low <= range_ratio
    breakout = cur["Close"] > range_high * (1 + breakout_ratio)
    vol_ok = cur["Volume"] > prior["Volume"].mean() * vol_ratio
    return bool(tight and breakout and vol_ok)


def open_three_new_highs_signal(today_bars, prev_close, close_3d_ago):
    if len(today_bars) != 6:
        return False
    first_close = today_bars["Close"].iloc[0]
    gap_up = prev_close is not None and first_close > prev_close
    window = today_bars.iloc[1:6]
    new_highs = int(((window["High"] > window["High"].shift(1)) & (window["Close"] > window["Close"].shift(1))).sum())
    cur_close = today_bars["Close"].iloc[-1]
    small_move = close_3d_ago is not None and close_3d_ago > 0 and abs(cur_close - close_3d_ago) / close_3d_ago < 0.03
    return bool(gap_up and new_highs >= 3 and small_move)


def reversal_rebound_signal(today_bars, prev_close, close_10d_ago, close_20d_ago,
                             avg_vol_5d, prev_day_volume):
    cur = today_bars.iloc[-1]
    open_price = today_bars["Open"].iloc[0]
    day_low = today_bars["Low"].min()
    now_hhmm = today_bars.index[-1].strftime("%H%M%S")
    gap_down = prev_close is not None and open_price < prev_close
    rebound = day_low > 0 and prev_close is not None and cur["Close"] > day_low * 1.02 and cur["Close"] > prev_close
    decline20 = close_20d_ago is not None and close_20d_ago > 0 and cur["Close"] * 1.2 < close_20d_ago
    decline10 = close_10d_ago is not None and close_10d_ago > 0 and cur["Close"] * 1.07 < close_10d_ago
    before_930 = now_hhmm < "093000"
    liquidity = avg_vol_5d is not None and avg_vol_5d >= 1000
    today_vol_so_far = today_bars["Volume"].sum()
    volume_ok = prev_day_volume is not None and today_vol_so_far > prev_day_volume * 0.2
    return bool(gap_down and rebound and decline20 and decline10 and before_930 and liquidity and volume_ok)


def daily_closes_and_volumes(full_bars):
    """One row per calendar day: Close (last), Volume (sum) - for the
    cross-day lookups (prev_close, N-day-ago close, 5-day avg volume) every
    signal here except triple_red needs. Volume here is in 股 (shares) as
    Shioaji reports it; the source scripts' lot-based thresholds (>500,
    >=1000) are reinterpreted in 張 - divided by 1000 below at the call site."""
    daily = full_bars.resample("1D").agg({"Close": "last", "Volume": "sum"}).dropna(subset=["Close"])
    daily.index = daily.index.date
    return daily


def replay(signal_type, codes, bars_by_code, names):
    open_positions = {}
    trades = []
    exits = SIGNAL_EXITS[signal_type]

    daily_by_code = {c: daily_closes_and_volumes(b) for c, b in bars_by_code.items()}
    all_days = sorted(set().union(*(set(b.index.normalize()) for b in bars_by_code.values())))

    for day in all_days:
        day_date = day.date()
        day_views = {c: b[b.index.normalize() == day] for c, b in bars_by_code.items()}
        day_views = {c: v for c, v in day_views.items() if not v.empty}

        prior_bars = {c: b[b.index.normalize() < day] for c, b in bars_by_code.items()}
        prior_aggs = watchlist.previous_day_aggregates(prior_bars)
        today_watchlist = {c for c, _, _ in watchlist.score_and_rank(
            prior_aggs, names, top_n=config.WATCHLIST_SIZE, weights=config.WATCHLIST_WEIGHTS
        )} if prior_aggs else set()

        all_ts = sorted(set().union(*(set(v.index) for v in day_views.values()))) if day_views else []

        for t in all_ts:
            now_hhmm = t.strftime("%H:%M")
            force_flatten = now_hhmm >= config.FORCE_FLATTEN_TIME

            for code in list(open_positions.keys()):
                dv = day_views.get(code)
                if dv is None:
                    continue
                view = dv[dv.index <= t]
                if view.empty:
                    continue
                close = minute_data.latest_close(view)
                pos = open_positions[code]
                unrealized = (close - pos["entry_price"]) / pos["entry_price"]
                reason = None
                if force_flatten:
                    reason = "force_flatten"
                elif unrealized <= exits["stop_loss_pct"]:
                    reason = "stop_loss"
                elif unrealized >= exits["take_profit_pct"]:
                    reason = "take_profit"
                if reason:
                    pnl, net_ret = net_return(pos["entry_price"], close, pos["shares"])
                    trades.append({"code": code, "name": names.get(code, code), "entry_date": day_date.isoformat(),
                                    "entry_time": pos["entry_time"].strftime("%H:%M"), "entry_price": pos["entry_price"],
                                    "exit_time": t.strftime("%H:%M"), "exit_price": close, "exit_reason": reason,
                                    "net_return": round(net_ret, 4), "net_pnl": round(pnl, 0)})
                    del open_positions[code]

            if force_flatten:
                continue

            running_cost = sum(p["entry_price"] * p["shares"] for p in open_positions.values())
            for code in today_watchlist:
                if code in open_positions:
                    continue
                dv = day_views.get(code)
                if dv is None:
                    continue
                view = dv[dv.index <= t]
                if view.empty:
                    continue

                fired = False
                if signal_type == "triple_red":
                    fired = triple_red_signal(view)
                elif signal_type == "range_breakout_5min":
                    full_so_far = bars_by_code[code][bars_by_code[code].index <= t]
                    fired = range_breakout_5min_signal(full_so_far)
                else:
                    daily = daily_by_code.get(code)
                    if daily is None or day_date not in daily.index:
                        continue
                    pos_in_daily = daily.index.get_loc(day_date)
                    if pos_in_daily < 1:
                        continue
                    prev_close = float(daily["Close"].iloc[pos_in_daily - 1])
                    prev_day_volume = float(daily["Volume"].iloc[pos_in_daily - 1])
                    if signal_type == "open_three_new_highs":
                        close_3d_ago = float(daily["Close"].iloc[pos_in_daily - 3]) if pos_in_daily >= 3 else None
                        fired = open_three_new_highs_signal(view, prev_close, close_3d_ago)
                    elif signal_type == "reversal_rebound":
                        close_10d_ago = float(daily["Close"].iloc[pos_in_daily - 10]) if pos_in_daily >= 10 else None
                        close_20d_ago = float(daily["Close"].iloc[pos_in_daily - 20]) if pos_in_daily >= 20 else None
                        avg_vol_5d = float(daily["Volume"].iloc[max(0, pos_in_daily - 5):pos_in_daily].mean() / 1000) \
                            if pos_in_daily >= 1 else None
                        fired = reversal_rebound_signal(view, prev_close, close_10d_ago, close_20d_ago,
                                                         avg_vol_5d, prev_day_volume / 1000 if prev_day_volume else None)

                if not fired:
                    continue
                close = minute_data.latest_close(view)
                cost = close * config.ORDER_LOT_SHARES
                if running_cost + cost > config.MAX_TOTAL_POSITION_COST_NTD:
                    continue
                open_positions[code] = {"entry_price": close, "entry_time": t, "shares": config.ORDER_LOT_SHARES}
                running_cost += cost

        for code in list(open_positions.keys()):
            dv = day_views.get(code)
            if dv is None or dv.empty:
                continue
            close = minute_data.latest_close(dv)
            pos = open_positions[code]
            pnl, net_ret = net_return(pos["entry_price"], close, pos["shares"])
            trades.append({"code": code, "name": names.get(code, code), "entry_date": day_date.isoformat(),
                            "entry_time": pos["entry_time"].strftime("%H:%M"), "entry_price": pos["entry_price"],
                            "exit_time": dv.index[-1].strftime("%H:%M"), "exit_price": close,
                            "exit_reason": "end_of_day_safety_net", "net_return": round(net_ret, 4),
                            "net_pnl": round(pnl, 0)})
            del open_positions[code]

    return trades


def stats(trades):
    if not trades:
        return None
    n = len(trades)
    wins = [t for t in trades if t["net_return"] > 0]
    total_pnl = sum(t["net_pnl"] for t in trades)
    avg_ret = sum(t["net_return"] for t in trades) / n
    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    return {"n": n, "win_rate": len(wins) / n, "total_pnl": total_pnl, "avg_ret": avg_ret, "reasons": reasons}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument("--db", default=db.DB_PATH)
    parser.add_argument("--out", default="trading/daytrade/xq_signals_backtest_result.txt")
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

    lines = [f"=== XQ全球贏家當沖策略分類頁方法回測：{start.isoformat()} ~ {end.isoformat()}"
             f"（全市場{len(bars_by_code)}檔股票，每日觀察清單{config.WATCHLIST_SIZE}檔）===", "",
             "來源：https://www.xq.com.tw/xstrader/category/交易策略/當沖策略/",
             "10篇文章中4篇可實作測試（略過2篇寫作技巧文、1篇需要外盤量tick資料無法回測、1篇規則被截斷）；",
             "大盤濾網(加權指數站上均線)未實作；全部策略都套用本專案13:00強制平倉，不留倉過夜。", ""]

    for signal_type in SIGNAL_EXITS:
        trades = replay(signal_type, codes, bars_by_code, names)
        s = stats(trades)
        label = SIGNAL_LABELS[signal_type]
        if s is None:
            lines.append(f"-- {label} -- 沒有任何交易訊號成立。")
            print(f"[{signal_type}] 0 trades")
            continue
        lines.append(f"-- {label} --")
        lines.append(f"  總交易次數：{s['n']}｜勝率：{s['win_rate']*100:.1f}%｜"
                      f"總損益：NT${s['total_pnl']:,.0f}｜平均報酬：{s['avg_ret']*100:+.2f}%")
        lines.append("  出場原因：" + "、".join(f"{k}:{v}" for k, v in s["reasons"].items()))
        lines.append("")
        print(f"[{signal_type}] {s['n']} trades, win={s['win_rate']*100:.1f}%, pnl=NT${s['total_pnl']:,.0f}")

    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
