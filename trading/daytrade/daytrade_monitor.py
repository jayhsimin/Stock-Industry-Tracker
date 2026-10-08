"""Day-trade main loop - meant to be called every 1 minute during the
trading session (09:00-13:05ish), same cadence as build_dashboard_live.py's
scheduled task. Every run: checks open day-trade positions for stop-loss/
take-profit/force-flatten, then (if the opening range has finalized and
it's before the force-flatten time) scans the concept_map universe for a
fresh ORB breakout and buys it, subject to the budget cap.

Unlike the swing strategy, there's no separate "after close" scan - entries
and exits both happen intraday here, so one script does both.

Usage:
    python -m trading.daytrade.daytrade_monitor
    python -m trading.daytrade.daytrade_monitor --live   # still simulation
                                                          # unless TRADING_LIVE_CONFIRM
                                                          # is also set - see trading/executor.py
"""
import argparse
import sqlite3
import warnings

import db
from .. import executor, positions
from . import config, minute_data, report, signals


def load_universe(conn):
    """Same concept_map universe as the swing strategy (~170 stocks), not
    the whole market - keeps the per-minute kbars() calls (one per stock)
    within the same resource budget the existing scripts already use."""
    codes = [r[0] for r in conn.execute("SELECT DISTINCT stock_id FROM concept_map")]
    names = dict(conn.execute('SELECT "公司代號", "公司簡稱" FROM company_info'))
    return sorted(codes), names


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                         help="attempt live trading - still needs TRADING_LIVE_CONFIRM, see executor.py")
    parser.add_argument("--db", default=db.DB_PATH)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    all_codes, names = load_universe(conn)
    conn.close()
    print(f"universe: {len(all_codes)} concept_map stocks")

    api, live = executor.login(args.live)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        contracts = {c: api.Contracts.Stocks.get(c) for c in all_codes}

        per_code = {}
        for code in all_codes:
            contract = contracts.get(code)
            if contract is None:
                continue
            bars = minute_data.today_bars(api, contract)
            if bars.empty:
                continue
            per_code[code] = {
                "close": minute_data.latest_close(bars),
                "or_high": minute_data.opening_range_high(bars, config.ORB_WINDOW_MINUTES),
                "volume_ratio": minute_data.minute_volume_ratio(bars),
                "trade_value": minute_data.cumulative_trade_value(bars),
                "now_hhmm": minute_data.latest_time_hhmm(bars),
            }
        print(f"{len(per_code)} stocks had usable minute bars today")

        open_positions = positions.list_open(state_file=config.POSITIONS_STATE_FILE)

        # --- exits: stop-loss / take-profit / force-flatten ---
        exits_now = []
        for code, pos in list(open_positions.items()):
            d = per_code.get(code)
            if d is None or d["close"] is None:
                continue
            cond = signals.check_exit(pos["entry_price"], d["close"], d["now_hhmm"])
            if cond["exit_signal"]:
                contract = contracts.get(code)
                if contract is not None:
                    executor.sell(api, contract, d["close"], pos["shares"])
                reason = next(k for k in ("stop_loss", "take_profit", "force_flatten") if cond[k])
                rec = positions.close_position(code, d["close"], reason, state_file=config.POSITIONS_STATE_FILE)
                if rec:
                    rec["name"] = pos["name"]
                    exits_now.append(rec)

        # --- entries: fresh ORB breakout, budget-capped, tie-broken by
        # volume_ratio (the only ranking factor that exists for this
        # strategy - no theme/MA-distance factors like the swing strategy) ---
        new_entries = []
        candidates = []
        for code, d in per_code.items():
            if code in open_positions or d["close"] is None:
                continue
            # Don't open anything that's about to be force-flattened anyway -
            # entering at 12:59 just to exit at 13:00 is pointless, not wrong,
            # but there's no reason to pay the spread for it.
            if d["now_hhmm"] is not None and d["now_hhmm"] >= config.FORCE_FLATTEN_TIME:
                continue
            cond = signals.check_entry(d["close"], d["or_high"], d["volume_ratio"], d["trade_value"])
            if cond["entry_signal"]:
                candidates.append((code, d))
        candidates.sort(key=lambda item: item[1]["volume_ratio"], reverse=True)

        running_cost = positions.total_open_cost(state_file=config.POSITIONS_STATE_FILE)
        for code, d in candidates:
            shares = config.ORDER_LOT_SHARES
            cost = d["close"] * shares
            if running_cost + cost > config.MAX_TOTAL_POSITION_COST_NTD:
                continue
            contract = contracts.get(code)
            if contract is None:
                continue
            executor.buy(api, contract, d["close"], shares)
            positions.open_position(code, names.get(code, code), d["close"], shares,
                                     state_file=config.POSITIONS_STATE_FILE)
            running_cost += cost
            new_entries.append({"code": code, "name": names.get(code, code), **d})

        api.logout()

    path = report.append(new_entries, exits_now)
    print(f"entries: {len(new_entries)}, exits: {len(exits_now)}"
          + (f" -> logged to {path}" if path else " (nothing to log)"))


if __name__ == "__main__":
    main()
