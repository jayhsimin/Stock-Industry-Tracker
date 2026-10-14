"""After-close entry point for the trendline-channel strategy - scans the
concept_map universe (same universe every validation run used - see
trading/channel_backtest.py/channel_cv.py) for a break below the lower
trendline, and checks open positions for upper-line-break/stop-loss/
max-hold exits.

Fully isolated from swing (trading/), day-trade (trading/daytrade/) and
ma_touch (trading/ma_touch/): own config (trading/channel/config.py), own
signal functions (trading/channel/signals.py), own position-state file
and own budget cap. Reuses trading/positions.py (its own state_file) and
trading/executor.py (same two-factor live-trading safety gate as every
other strategy) directly rather than duplicating them.

Needs a full daily OHLCV history per stock (not just today's close) to
confirm swing points and build the trendlines - same Shioaji kbars() pull
trading/channel_backtest.py uses (daily_ohlcv(), reused unchanged).

Usage:
    python -m trading.channel.scan                  # simulation (default, safe)
    python -m trading.channel.scan --live            # still simulation unless
                                                       # TRADING_LIVE_CONFIRM is also set
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import pandas as pd

from .. import channel_backtest as backtest
from .. import daily_scan, executor, positions
from . import config, signals


def shares_to_buy(price, remaining_budget):
    """1 full lot if the remaining budget covers it, otherwise 0 (skip) -
    same simple sizing rule channel_backtest.py validated with."""
    cost = price * config.ORDER_LOT_SHARES
    return config.ORDER_LOT_SHARES if cost <= remaining_budget else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                         help="attempt live trading - still needs TRADING_LIVE_CONFIRM, see executor.py")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    import db
    conn = sqlite3.connect(args.db or db.DB_PATH)
    code_concepts, names = daily_scan.load_universe(conn)
    conn.close()
    all_codes = sorted(code_concepts.keys())
    print(f"universe: {len(all_codes)} concept_map stocks")

    end = date.today()
    start = end - timedelta(days=config.WARMUP_DAYS)

    api, live = executor.login(args.live)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        contracts = {c: api.Contracts.Stocks.get(c) for c in all_codes}

        channels = {}
        for code, contract in contracts.items():
            if contract is None:
                continue
            daily = backtest.daily_ohlcv(api, contract, start.isoformat(), end.isoformat())
            if len(daily) < 2 * config.PIVOT_K + 10:
                continue
            channels[code] = signals.compute_channel(daily)
        print(f"{len(channels)}/{len(all_codes)} 檔算出通道（足夠歷史資料）")

        open_positions = positions.list_open(state_file=config.POSITIONS_STATE_FILE)

        # --- exits ---
        exits_today = []
        open_status = []
        for code, pos in list(open_positions.items()):
            ch = channels.get(code)
            if ch is None or ch.empty:
                continue
            last = ch.iloc[-1]
            current_price = float(last["Close"])
            held = positions.days_held(pos["entry_date"])
            exit_cond = signals.check_exit(pos["entry_price"], current_price, held, last["upper_line"])
            open_status.append((code, pos["name"], pos["entry_price"], current_price, held, exit_cond))
            if exit_cond["exit_signal"]:
                contract = contracts.get(code)
                shares = pos["shares"]
                if contract is not None:
                    executor.sell(api, contract, current_price, shares)
                reason = next(k for k in ("upper_line_break", "stop_loss", "max_hold_days") if exit_cond[k])
                rec = positions.close_position(code, current_price, reason, state_file=config.POSITIONS_STATE_FILE)
                if rec:
                    rec["name"] = pos["name"]
                    exits_today.append(rec)

        # --- entries ---
        new_signals = []
        budget_skipped = []
        running_cost = positions.total_open_cost(state_file=config.POSITIONS_STATE_FILE)
        for code in sorted(channels.keys()):
            if code in open_positions:
                continue
            ch = channels[code]
            if ch.empty:
                continue
            last = ch.iloc[-1]
            close = float(last["Close"])
            amount = float(last["Amount"]) if pd.notna(last.get("Amount")) else None
            cond = signals.check_entry(close, last["lower_line"], amount)
            if not cond["entry_signal"]:
                continue
            shares = shares_to_buy(close, config.MAX_TOTAL_POSITION_COST_NTD - running_cost)
            if shares < 1:
                budget_skipped.append((code, names.get(code, code), close))
                continue
            contract = contracts.get(code)
            if contract is not None:
                executor.buy(api, contract, close, shares)
            positions.open_position(code, names.get(code, code), close, shares,
                                     state_file=config.POSITIONS_STATE_FILE)
            running_cost += close * shares
            new_signals.append((code, names.get(code, code), cond))

        api.logout()

    run_date = date.today().isoformat()
    print(f"=== 趨勢線通道策略 - {run_date} ===")
    print(f"新進場 ({len(new_signals)}): " + ", ".join(f"{c}{n}" for c, n, _ in new_signals) if new_signals else "新進場: 無")
    print(f"今日出場 ({len(exits_today)}): "
          + ", ".join(f"{r['code']}{r['name']}({r['exit_reason']})" for r in exits_today) if exits_today else "今日出場: 無")
    print(f"持倉中 ({len(open_status)}): "
          + ", ".join(f"{c}{n}" for c, n, *_ in open_status) if open_status else "持倉中: 無")
    if budget_skipped:
        print(f"資金上限已滿跳過 ({len(budget_skipped)}): " + ", ".join(f"{c}{n}" for c, n, _ in budget_skipped))
    print("※ 全部為模擬單，非真實交易。")


if __name__ == "__main__":
    main()
