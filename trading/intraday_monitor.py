"""Per-minute check of OPEN positions only (not the whole universe) for
stop-loss/take-profit - the two exit conditions that need live price and
can't wait for daily_scan.py's end-of-day pass. Meant to be called from
the same per-minute cadence as build_dashboard_live.py's scheduled task
(see README's 排程 section), not scheduled separately.

theme_weakened is intentionally NOT checked here (passed as None) - that
condition is evaluated once daily in daily_scan.py against the day's
median, not every minute against a noisy intraday snapshot.

Usage:
    python -m trading.intraday_monitor
    python -m trading.intraday_monitor --live   # still simulation unless
                                                  # TRADING_LIVE_CONFIRM is also set
"""
import argparse
import warnings

from . import executor, positions, signals


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                         help="attempt live trading - still needs TRADING_LIVE_CONFIRM, see executor.py")
    args = parser.parse_args()

    open_positions = positions.list_open()
    if not open_positions:
        print("no open positions - nothing to check")
        return

    api, live = executor.login(args.live)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for code, pos in list(open_positions.items()):
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            snaps = api.snapshots([contract])
            if not snaps or not snaps[0].close:
                continue
            current_price = snaps[0].close
            held = positions.days_held(pos["entry_date"])
            cond = signals.check_exit(pos["entry_price"], current_price, held, theme_median_pct=None)

            if cond["exit_signal"]:
                reason = next(k for k in ("stop_loss", "take_profit", "max_hold_days") if cond[k])
                executor.sell(api, contract, current_price, pos["shares"])
                positions.close_position(code, current_price, reason)
                print(f"[exit] {code} {pos['name']} {reason} @ {current_price} "
                      f"({cond['unrealized_return'] * 100:+.2f}%)")

        api.logout()


if __name__ == "__main__":
    main()
