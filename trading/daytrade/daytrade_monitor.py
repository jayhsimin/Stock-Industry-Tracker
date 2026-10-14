"""Day-trade main loop - meant to be called every 1 minute during the
trading session (09:00-13:05ish), same cadence as build_dashboard_live.py's
scheduled task. Every run: checks open day-trade positions for stop-loss/
take-profit/force-flatten, then (if before the force-flatten time) scans
today's watchlist for a fresh entry signal and buys it, subject to the
budget cap.

Two independent choices, both selectable from the command line:
  --source {liquidity, news, ai_chain}  which stocks are even candidates today
  --signal {orb, reversal, reversal_short, reversal_both, momentum,
            rolling_breakout}  how entries fire, and which direction
Defaults (liquidity + orb) match the original, currently-scheduled
TWTradingDaytradeMonitor task - running this with no flags changes
nothing. --source news --signal reversal is the 2026-10 "找題材股再用
K棒反轉型態判斷進場" experiment; see trading/daytrade/news_watchlist.py
for why news selection isn't automated end-to-end yet. --signal
reversal_both trades BOTH directions (多空都做) - 融券賣出 (short) on a
bearish reversal (shooting star/bearish engulfing), 現股買進 (long) on a
bullish one - see trading/executor.py's short_sell()/cover_buy() and the
信用交易帳戶 (margin account) prerequisite note in that module's docstring.
--source ai_chain trades at most ONE stock/day (see
trading/daytrade/ai_chain_watchlist.py): concept_map's AI伺服器 members,
filtered to yesterday's 大量 (NT$1億+), then the single highest news-
discussion stock among survivors - cheap enough (9 stocks + 1 news call)
to compute inline here on cache miss, unlike --source liquidity's
whole-market pre-market scan.

The watchlist itself is NOT built here - see build_watchlist.py's
docstring for why (scanning the whole market takes minutes, far too slow
for a 1-minute cadence). If today's watchlist cache is missing or stale,
this still checks existing open positions for exits (never silently skip
risk management) but logs new entries as unavailable rather than falling
back to some other universe.

Unlike the swing strategy, there's no separate "after close" scan - entries
and exits both happen intraday here, so one script does both.

Usage:
    python -m trading.daytrade.daytrade_monitor
    python -m trading.daytrade.daytrade_monitor --source news --signal reversal
    python -m trading.daytrade.daytrade_monitor --live   # still simulation
                                                          # unless TRADING_LIVE_CONFIRM
                                                          # is also set - see trading/executor.py
"""
import argparse
import sqlite3
import warnings
from datetime import date

import db
from .. import executor, positions
from . import ai_chain_watchlist, config, minute_data, news_watchlist, patterns, report, signals, watchlist


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                         help="attempt live trading - still needs TRADING_LIVE_CONFIRM, see executor.py")
    parser.add_argument("--source", choices=["liquidity", "news", "ai_chain"], default="liquidity",
                         help="liquidity (default): watchlist.py's pre-market scan. "
                              "news: today's news_watchlist.py selection. "
                              "ai_chain: ai_chain_watchlist.py's single-stock pick.")
    parser.add_argument("--signal", choices=list(patterns.PATTERN_FUNCS), default="orb",
                         help="orb (default), or one of patterns.py's alternatives")
    args = parser.parse_args()

    api, live = executor.login(args.live)

    if args.source == "news":
        watch_codes, watch_names, selections = news_watchlist.load_cache()
        if watch_codes is None:
            print("[news_watchlist] no cache for today - a news search + news_watchlist.save_cache() "
                  "call must happen first (currently a manual/Claude-session step, see that module's "
                  "docstring). Existing open positions will still be checked for exits.")
            watch_codes, watch_names = [], {}
        else:
            print(f"news watchlist: {len(watch_codes)} stocks (today's overnight-catalyst selection)")
    elif args.source == "ai_chain":
        watch_codes, watch_names, _ = news_watchlist.load_cache(state_file=config.AI_CHAIN_STATE_FILE)
        if watch_codes is None:
            # cheap enough (~27 stocks + a handful of news calls) to compute
            # right here on cache miss, unlike the whole-market liquidity watchlist
            conn = sqlite3.connect(db.DB_PATH)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                selections, detail = ai_chain_watchlist.select_stocks(api, conn, as_of_date=date.today())
            conn.close()
            if not selections:
                print(f"[ai_chain] {detail['reason']} - no stocks to trade today.")
                watch_codes, watch_names = [], {}
            else:
                print(f"[ai_chain] selected {len(selections)} stocks: "
                      + ", ".join(f"{s['code']}{s['name']}" for s in selections))
                cache_selections = [{"code": s["code"], "name": s["name"], "headline": s["reason"],
                                      "source_url": "ai_chain_watchlist", "published_at": date.today().isoformat(),
                                      "reason": s["reason"]} for s in selections]
                news_watchlist.save_cache(cache_selections, state_file=config.AI_CHAIN_STATE_FILE)
                watch_codes = [s["code"] for s in selections]
                watch_names = {s["code"]: s["name"] for s in selections}
        else:
            print(f"[ai_chain] cached picks for today: {len(watch_codes)} stocks")
    else:
        watch_codes, watch_names = watchlist.load_cache()
        if watch_codes is None:
            print("[watchlist] no cache for today - run build_watchlist.py first. "
                  "Existing open positions will still be checked for exits.")
            watch_codes, watch_names = [], {}
        else:
            print(f"watchlist: {len(watch_codes)} stocks (built pre-market from yesterday's data)")
    names = dict(watch_names)

    # ai_chain gets its own position pool/budget cap, separate from the
    # liquidity+orb task's - so each design's live paper-trading track
    # record can be evaluated independently instead of competing for the
    # same simulated capital.
    if args.source == "ai_chain":
        pos_state_file = config.AI_CHAIN_POSITIONS_STATE_FILE
        budget_cap = config.AI_CHAIN_MAX_TOTAL_POSITION_COST_NTD
    else:
        pos_state_file = config.POSITIONS_STATE_FILE
        budget_cap = config.MAX_TOTAL_POSITION_COST_NTD

    open_positions = positions.list_open(state_file=pos_state_file)
    # Always still fetch bars for already-open positions even if they fell
    # off today's watchlist (shouldn't normally happen - force-flatten
    # empties positions by 13:00 daily - but exits must never silently stop
    # being checked just because a stock isn't on today's list).
    codes_needed = sorted(set(watch_codes) | set(open_positions.keys()))

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        contracts = {c: api.Contracts.Stocks.get(c) for c in codes_needed}

        per_code = {}
        for code in codes_needed:
            contract = contracts.get(code)
            if contract is None:
                continue
            bars = minute_data.today_bars(api, contract)
            if bars.empty:
                continue
            per_code[code] = {
                "bars": bars,
                "close": minute_data.latest_close(bars),
                "or_high": minute_data.opening_range_high(bars, config.ORB_WINDOW_MINUTES),
                "or_range_pct": minute_data.opening_range_pct(bars, config.ORB_WINDOW_MINUTES),
                "volume_ratio": minute_data.minute_volume_ratio(bars),
                "trade_value": minute_data.cumulative_trade_value(bars),
                "now_hhmm": minute_data.latest_time_hhmm(bars),
            }
        print(f"{len(per_code)} stocks had usable minute bars today")

        # --- exits: stop-loss / take-profit / force-flatten ---
        exits_now = []
        for code, pos in list(open_positions.items()):
            d = per_code.get(code)
            if d is None or d["close"] is None:
                continue
            side = pos.get("side", "long")
            cond = signals.check_exit(pos["entry_price"], d["close"], d["now_hhmm"], side=side)
            if cond["exit_signal"]:
                contract = contracts.get(code)
                if contract is not None:
                    if side == "long":
                        executor.sell(api, contract, d["close"], pos["shares"])
                    else:
                        executor.cover_buy(api, contract, d["close"], pos["shares"])
                reason = next(k for k in ("stop_loss", "take_profit", "force_flatten") if cond[k])
                rec = positions.close_position(code, d["close"], reason, state_file=pos_state_file)
                if rec:
                    rec["name"] = pos["name"]
                    exits_now.append(rec)

        # --- entries: args.signal's entry trigger, budget-capped, tie-broken
        # by volume_ratio (the only ranking factor that exists for this
        # strategy - no theme/MA-distance factors like the swing strategy) ---
        new_entries = []
        candidates = []
        for code in watch_codes:
            d = per_code.get(code)
            if d is None or code in open_positions or d["close"] is None:
                continue
            # Don't open anything that's about to be force-flattened anyway -
            # entering at 12:59 just to exit at 13:00 is pointless, not wrong,
            # but there's no reason to pay the spread for it.
            if d["now_hhmm"] is not None and d["now_hhmm"] >= config.FORCE_FLATTEN_TIME:
                continue
            # ai_chain uses fixed-EXPOSURE sizing (config.AI_CHAIN_TARGET_EXPOSURE_NTD),
            # not fixed-lot - a high-priced stock whose single lot would blow
            # past the target exposure is skipped rather than traded anyway
            # (same reasoning net_return_EXPOSURE research found: 2308台達電-class
            # stocks distorted the 2026-10 backtest's P&L).
            if args.source == "ai_chain":
                max_lot_cost = config.AI_CHAIN_TARGET_EXPOSURE_NTD * config.AI_CHAIN_MAX_PRICE_MULTIPLIER
                if d["close"] * config.ORDER_LOT_SHARES > max_lot_cost:
                    continue
            entry_signal, _, _, side = patterns.evaluate_entry(args.signal, d["bars"])
            if entry_signal:
                candidates.append((code, d, side))
        candidates.sort(key=lambda item: item[1]["volume_ratio"], reverse=True)

        running_cost = positions.total_open_cost(state_file=pos_state_file)
        for code, d, side in candidates:
            shares = config.ORDER_LOT_SHARES
            cost = d["close"] * shares
            if running_cost + cost > budget_cap:
                continue
            contract = contracts.get(code)
            if contract is None:
                continue
            if side == "long":
                executor.buy(api, contract, d["close"], shares)
            else:
                executor.short_sell(api, contract, d["close"], shares)
            positions.open_position(code, names.get(code, code), d["close"], shares, side=side,
                                     state_file=pos_state_file)
            running_cost += cost
            new_entries.append({"code": code, "name": names.get(code, code), "side": side,
                                 **{k: v for k, v in d.items() if k != "bars"}})

        api.logout()

    path = report.append(new_entries, exits_now)
    print(f"entries: {len(new_entries)}, exits: {len(exits_now)}"
          + (f" -> logged to {path}" if path else " (nothing to log)"))


if __name__ == "__main__":
    main()
