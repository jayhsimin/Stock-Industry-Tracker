"""One-off test: picks TODAY's top 20 whole-market stocks by combined
liquidity+volatility score (reusing trading/daytrade/watchlist.py's
score_and_rank()), then replays the MA5/10/20 支撐/破位 strategy's signals
(trading/ma_touch/signals.py, unmodified) minute-by-minute across today's
session for just those 20 names - same methodology as
test_intraday_replay.py, just on a deliberately volume/volatility-selected
universe instead of concept_map's theme-curated list.

Whole market (not concept_map) because that's this project's established
reason for ranking by liquidity+volatility in the first place - see
watchlist.py's own docstring: concept_map was found to contain thin,
low-volatility names for exactly this kind of ranking.

Ranking uses batched snapshots() (CHUNK=400, same pattern as
daily_scan.py/build_dashboard_live.py) instead of per-stock kbars() -
Shioaji's snapshot already carries today's high/low/open/total_amount,
which is exactly what score_and_rank() needs (range_pct, amount), so the
whole-market ranking step costs ~5 batched calls instead of ~2000
individual ones. The first version of this script used per-stock kbars()
for the ranking step too and reliably hit `401 Token is expired` partway
through a ~2000-stock loop - this rewrite avoids that entirely rather than
working around it with a mid-loop re-login. kbars() is still used, but
only for the 20 selected stocks (today's minute bars for the replay, plus
a longer daily-close history for MA5/10/20) - cheap enough not to matter.

Uses its own throwaway position-state file so trading/ma_touch/
positions_state.json (today's real daily-scan entries) is never touched;
every simulated open/close still appends its normal line to
trading/trade_log.txt, which is this test's actual deliverable.

Usage:
    python -m trading.ma_touch.test_top20_volatility
"""
import os
import warnings

import db
from .. import executor, ma_data, positions
from ..daytrade import build_watchlist, minute_data, watchlist
from . import signals

TOP_N = 20
CHUNK = 400
TEST_STATE_FILE = "trading/ma_touch/_top20_test_positions_state.json"


def main():
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    codes, names = build_watchlist.load_whole_market(conn)
    conn.close()
    print(f"universe: {len(codes)} whole-market stocks")

    api, live = executor.login(False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        contracts = {c: api.Contracts.Stocks.get(c) for c in codes}
        found = {c: ct for c, ct in contracts.items() if ct is not None}
        print(f"{len(found)}/{len(codes)} codes had a Shioaji contract")

        # today's aggregate (amount, range_pct) straight from the snapshot -
        # no per-stock kbars() needed for the ranking step
        aggs = {}
        found_items = list(found.items())
        for i in range(0, len(found_items), CHUNK):
            chunk = found_items[i:i + CHUNK]
            snaps = api.snapshots([ct for _, ct in chunk])
            for (code, _), snap in zip(chunk, snaps):
                if snap.close and snap.close > 0 and snap.open:
                    aggs[code] = {"amount": float(snap.total_amount),
                                  "range_pct": float((snap.high - snap.low) / snap.open)}
        print(f"{len(aggs)}/{len(found)} stocks had a usable snapshot")

        top20 = watchlist.score_and_rank(aggs, names, top_n=TOP_N)
        print(f"today's top {TOP_N} by liquidity+volatility:")
        for code, name, score in top20:
            print(f"  {code} {name}  score={score}")

        if os.path.exists(TEST_STATE_FILE):
            os.remove(TEST_STATE_FILE)

        entries = 0
        exits = 0
        still_open = []
        skipped_no_ma = []

        for code, name, score in top20:
            bars = minute_data.today_bars(api, found[code])
            if bars is None or len(bars) < 10:
                continue
            closes = ma_data.daily_closes(api, found[code])
            mas = ma_data.compute_mas(closes, (5, 10, 20))
            if len(mas) < 3:
                skipped_no_ma.append((code, name))
                continue

            open_pos = None
            for i in range(1, len(bars)):
                window = bars.iloc[:i + 1]
                price = float(window["Close"].iloc[-1])
                if open_pos is None:
                    volume_ratio = minute_data.minute_volume_ratio(window)
                    trade_value = minute_data.cumulative_trade_value(window)
                    cond = signals.check_entry(price, mas, volume_ratio, trade_value)
                    if cond["entry_signal"]:
                        positions.open_position(code, name, price, 1000, state_file=TEST_STATE_FILE)
                        open_pos = {"entry_price": price}
                        entries += 1
                else:
                    exit_cond = signals.check_exit(open_pos["entry_price"], price, 0, mas)
                    if exit_cond["exit_signal"]:
                        reason = next(k for k in ("support_broken", "stop_loss", "take_profit", "max_hold_days")
                                      if exit_cond[k])
                        positions.close_position(code, price, reason, state_file=TEST_STATE_FILE)
                        open_pos = None
                        exits += 1

            if open_pos is not None:
                still_open.append((code, name, open_pos["entry_price"]))

        api.logout()

    print(f"\n訊號測試：{entries}次進場、{exits}次出場")
    if skipped_no_ma:
        print(f"沒有足夠歷史資料算MA5/10/20而跳過 ({len(skipped_no_ma)}): "
              + ", ".join(f"{c}{n}" for c, n in skipped_no_ma))
    if still_open:
        print(f"收盤時仍處於訊號開倉狀態 ({len(still_open)}):")
        for code, name, entry_price in still_open:
            print(f"  {code}{name}｜進場@{entry_price}")
    print("結果已寫入 trading/trade_log.txt（標籤：MA支撐/破位-Top20測試）")

    if os.path.exists(TEST_STATE_FILE):
        os.remove(TEST_STATE_FILE)


if __name__ == "__main__":
    main()
