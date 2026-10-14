"""After-close entry point for the MA5/10/20 支撐/破位 strategy - scans the
concept_map universe (same stock list as the swing strategy, reused for
convenience/known liquidity only - this strategy's signal is pure price
action, not a theme bet) for a confirmed MA bounce, and checks open
positions for stop-loss/take-profit/max-hold/support-broken exits.

Fully isolated from the swing (trading/) and day-trade (trading/daytrade/)
strategies: own config (trading/ma_touch/config.py), own signal functions
(trading/ma_touch/signals.py), own position-state file and own budget cap.
Reuses trading/positions.py (with its own state_file) and trading/executor.py
(same two-factor live-trading safety gate as every other strategy) directly
rather than duplicating them - running this can add rows to its own state
file and its own line in trading/trade_log.txt, but touches nothing the
swing or day-trade strategies read or write.

NOT wired into any Windows scheduled task yet - this strategy hasn't been
backtested, so it isn't running automatically. See trading/ma_touch/README.md.

Usage:
    python -m trading.ma_touch.scan                  # simulation (default, safe)
    python -m trading.ma_touch.scan --live            # still simulation unless
                                                       # TRADING_LIVE_CONFIRM is also set
"""
import argparse
import sqlite3
import warnings
from datetime import date

from .. import daily_scan, executor, ma_data, positions
from . import config, signals


def shares_to_buy(price, remaining_budget):
    """1 full lot if the remaining budget covers it, otherwise 0 (skip) -
    deliberately simpler than trading/executor.py's shares_to_buy() (no
    odd-lot fallback): this is a brand-new, unvalidated strategy, so it
    starts from the plainest sizing rule rather than inheriting swing's
    most recent refinement by default."""
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
    print(f"universe: {len(all_codes)} concept_map stocks (price-action only - no theme condition)")

    api, live = executor.login(args.live)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        contracts = {c: api.Contracts.Stocks.get(c) for c in all_codes}

        live_quotes = {}
        CHUNK = 400
        found = [(c, ct) for c, ct in contracts.items() if ct is not None]
        for i in range(0, len(found), CHUNK):
            chunk = found[i:i + CHUNK]
            snaps = api.snapshots([ct for _, ct in chunk])
            for (code, _), snap in zip(chunk, snaps):
                if snap.close and snap.close > 0:
                    live_quotes[code] = {"close": snap.close, "volume_ratio": snap.volume_ratio,
                                          "trade_value": snap.total_amount}
        print(f"{len(live_quotes)} live quotes")

        open_positions = positions.list_open(state_file=config.POSITIONS_STATE_FILE)

        # --- exits ---
        exits_today = []
        open_status = []
        for code, pos in list(open_positions.items()):
            if code not in live_quotes or contracts.get(code) is None:
                continue
            current_price = live_quotes[code]["close"]
            held = positions.days_held(pos["entry_date"])
            mas = ma_data.compute_mas(ma_data.daily_closes(api, contracts[code]), config.MA_PERIODS)
            exit_cond = signals.check_exit(pos["entry_price"], current_price, held, mas)
            open_status.append((code, pos["name"], pos["entry_price"], current_price, held, exit_cond))
            if exit_cond["exit_signal"]:
                shares = pos["shares"]
                executor.sell(api, contracts[code], current_price, shares)
                reason = next(k for k in ("support_broken", "stop_loss", "take_profit", "max_hold_days")
                              if exit_cond[k])
                rec = positions.close_position(code, current_price, reason, state_file=config.POSITIONS_STATE_FILE)
                if rec:
                    rec["name"] = pos["name"]
                    exits_today.append(rec)

        # --- entries ---
        new_signals = []
        budget_skipped = []
        running_cost = positions.total_open_cost(state_file=config.POSITIONS_STATE_FILE)
        for code in all_codes:
            if code in open_positions or code not in live_quotes or contracts.get(code) is None:
                continue
            closes = ma_data.daily_closes(api, contracts[code])
            mas = ma_data.compute_mas(closes, config.MA_PERIODS)
            if not mas or len(closes) < 2:
                continue
            # kbars' own last daily close is TODAY's if this scan runs after
            # close (as intended) - "yesterday" (the day that tested support)
            # is the one before that, iloc[-2]; fall back to iloc[-1] only if
            # kbars hasn't picked up today's bar yet for some reason.
            is_today = closes.index[-1].date() == date.today()
            prev_close = float(closes.iloc[-2]) if is_today else float(closes.iloc[-1])
            q = live_quotes[code]
            cond = signals.check_entry(prev_close, q["close"], mas, q["volume_ratio"], q["trade_value"])
            if not cond["entry_signal"]:
                continue
            shares = shares_to_buy(q["close"], config.MAX_TOTAL_POSITION_COST_NTD - running_cost)
            if shares < 1:
                budget_skipped.append((code, names.get(code, code), q["close"]))
                continue
            executor.buy(api, contracts[code], q["close"], shares)
            positions.open_position(code, names.get(code, code), q["close"], shares,
                                     state_file=config.POSITIONS_STATE_FILE)
            running_cost += q["close"] * shares
            new_signals.append((code, names.get(code, code), cond))

        api.logout()

    run_date = date.today().isoformat()
    print(f"=== MA5/10/20 支撐/破位策略 - {run_date} ===")
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
