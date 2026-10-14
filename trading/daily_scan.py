"""After-close entry point: scan the concept_map universe for new entry
signals (decided today, submitted as ROD orders so Shioaji queues them for
tomorrow's session), check open positions for holding-period/theme-weakened
exits (stop-loss/take-profit are intraday_monitor.py's job, not this one -
they need live price, not an end-of-day snapshot), and write the daily
report.

Usage:
    python -m trading.daily_scan                  # simulation (default, safe)
    python -m trading.daily_scan --live            # still simulation unless
                                                    # TRADING_LIVE_CONFIRM is also set - see executor.py
"""
import argparse
import sqlite3
import statistics
import sys
import warnings
from datetime import date

import db
from . import config, executor, ma_data, positions, report, signals


def load_universe(conn):
    """{code: [concept, ...]} and {code: name} for every concept_map stock."""
    code_concepts = {}
    for code, concept in conn.execute("SELECT stock_id, concept FROM concept_map"):
        code_concepts.setdefault(code, []).append(concept)
    names = dict(conn.execute('SELECT "公司代號", "公司簡稱" FROM company_info'))
    return code_concepts, names


def concept_median_pcts(code_concepts, live):
    """{concept: median_pct_today} across all its members we have a live
    quote for - this is the same median-not-mean logic the dashboard uses
    (build_dashboard_live.py), computed here from live snapshots directly
    rather than twstock.db, since twstock.db has no same-day data until
    after TWSE/TPEx publish (hours after close)."""
    by_concept = {}
    for code, concepts in code_concepts.items():
        if code not in live:
            continue
        for c in concepts:
            by_concept.setdefault(c, []).append(live[code]["pct"])
    return {c: round(statistics.median(pcts), 2) for c, pcts in by_concept.items()}


def theme_pct_for_stock(code, code_concepts, concept_median, mode):
    """mode='max': optimistic (used for entry - ANY hot theme qualifies).
    mode='min': pessimistic (used for exit - ANY weakening theme triggers)."""
    values = [concept_median[c] for c in code_concepts.get(code, []) if c in concept_median]
    if not values:
        return None
    return max(values) if mode == "max" else min(values)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                         help="attempt live trading - still needs TRADING_LIVE_CONFIRM, see executor.py")
    parser.add_argument("--db", default=db.DB_PATH)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    code_concepts, names = load_universe(conn)
    conn.close()
    all_codes = sorted(code_concepts.keys())
    print(f"universe: {len(all_codes)} concept_map stocks")

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
                    live_quotes[code] = {
                        "close": snap.close,
                        "pct": round(snap.change_rate, 2),
                        "volume_ratio": snap.volume_ratio,
                        "trade_value": snap.total_amount,
                    }
        print(f"{len(live_quotes)} live quotes")

        concept_median = concept_median_pcts(code_concepts, live_quotes)

        open_positions = positions.list_open()

        # --- exits: holding-period / theme-weakened only (stop/take-profit
        # need live intraday price - intraday_monitor.py's job) ---
        exits_today = []
        open_status = []
        for code, pos in list(open_positions.items()):
            if code not in live_quotes:
                continue
            current_price = live_quotes[code]["close"]
            held = positions.days_held(pos["entry_date"])
            # theme_weakened retired 2026-10: a 182-stock/6-month backtest of
            # the production rule (trading/swing_backtest.py) showed it fired
            # on 81% of exits at an avg -0.81% (avg hold only 2.1 days), while
            # removing it raised total P&L from +NT$62,919 to +NT$90,100 and
            # win rate from 35.9% to 47.4% - the daily concept-median is too
            # noisy at 1-day granularity to be a useful exit signal. Passed
            # as None, same idiom intraday_monitor.py already uses to skip it
            # intraday - see trading/decisions.json.
            exit_cond = signals.check_exit(pos["entry_price"], current_price, held, theme_median_pct=None)
            open_status.append((code, pos["name"], pos["entry_price"], current_price, held, exit_cond))
            if exit_cond["exit_signal"]:
                contract = contracts.get(code)
                shares = pos["shares"]
                if contract is not None:
                    executor.sell(api, contract, current_price, shares)
                reason = next(k for k in ("stop_loss", "take_profit", "max_hold_days", "theme_weakened")
                              if exit_cond[k])
                rec = positions.close_position(code, current_price, reason)
                if rec:
                    rec["name"] = pos["name"]
                    exits_today.append(rec)

        # --- new entry signals (skip stocks already held) ---
        # Collect every qualifying candidate first, then rank them (signals.
        # rank_candidates - theme strength/volume spike/MA precision, see
        # config.RANK_WEIGHTS) before applying the budget cap, so when more
        # candidates qualify than the cap allows, the strongest ones buy
        # first instead of whichever stock code happens to be smallest.
        candidates = []
        for code in all_codes:
            if code in open_positions or code not in live_quotes or contracts.get(code) is None:
                continue
            mas = ma_data.compute_mas(ma_data.daily_closes(api, contracts[code]), config.MA_PERIODS)
            if not mas:
                continue
            theme_pct = theme_pct_for_stock(code, code_concepts, concept_median, mode="max")
            q = live_quotes[code]
            cond = signals.check_entry(q["close"], q["volume_ratio"], mas, theme_pct, q["trade_value"])
            if not cond["entry_signal"]:
                continue
            candidates.append({
                "code": code,
                "name": names.get(code, code),
                "cond": cond,
                "close": q["close"],
                "theme_pct": theme_pct,
                "volume_ratio": q["volume_ratio"],
                "ma_distance_pct": cond["ma_distance_pct"],
            })

        new_signals = []
        budget_skipped = []
        running_cost = positions.total_open_cost()  # includes positions opened earlier in this same run
        for c in signals.rank_candidates(candidates):
            code, name, cond = c["code"], c["name"], c["cond"]
            cond["score"] = c["score"]
            remaining_budget = config.MAX_TOTAL_POSITION_COST_NTD - running_cost
            shares = executor.shares_to_buy(c["close"], remaining_budget)
            if shares < 1:
                budget_skipped.append((code, name, c["close"], c["score"]))
                continue
            cost = c["close"] * shares

            executor.buy(api, contracts[code], c["close"], shares)
            positions.open_position(code, name, c["close"], shares)
            running_cost += cost
            new_signals.append((code, name, cond))

        api.logout()

    run_date = date.today().isoformat()
    text = report.build(run_date, new_signals, open_status, exits_today, budget_skipped)
    path = report.save(text, run_date)
    print(text)
    print(f"\nsaved: {path}")


if __name__ == "__main__":
    main()
