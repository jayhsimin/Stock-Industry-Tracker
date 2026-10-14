"""One-off test: replays the MA5/10/20 支撐/破位 strategy's signals (signals.py,
unmodified) minute-by-minute across TODAY's full trading session, instead of
checking only once at end-of-day close the way scan.py does. Answers "would
checking more often during the day change what this strategy would have
done today" - diagnostic only, not a new production entry point.

Uses a SEPARATE, throwaway position-state file (TEST_STATE_FILE) so it can
never touch trading/ma_touch/positions_state.json (today's real entries from
the actual daily scan.py run stay untouched). Still goes through
trading/positions.py's open_position()/close_position(), so every simulated
open/close during this replay still appends its normal line to
trading/trade_log.txt - that log is the actual deliverable here, the test
state file is deleted at the end of this run.

MA5/10/20 themselves come from today's ma_cache.json (already built this
morning) - they're daily moving averages and don't change intraday, only
the entry/exit CHECK is done at minute granularity. volume_ratio/trade_value
use the minute-bar-based equivalents from trading/daytrade/minute_data.py
(today's minute pace vs today's own earlier minutes), not the daily
snapshot figures signals.py's config thresholds were tuned against - so
results here are indicative, not an apples-to-apples validation of the
daily-close version.

Usage:
    python -m trading.ma_touch.test_intraday_replay
"""
import json
import os
import sqlite3
import warnings

from .. import daily_scan, executor, positions
from ..daytrade import minute_data
from . import signals

MA_CACHE_PATH = "ma_cache.json"
TEST_STATE_FILE = "trading/ma_touch/_intraday_test_positions_state.json"


def main():
    import db
    conn = sqlite3.connect(db.DB_PATH)
    code_concepts, names = daily_scan.load_universe(conn)
    conn.close()
    codes = sorted(code_concepts.keys())

    if not os.path.exists(MA_CACHE_PATH):
        print("ma_cache.json not found - run build_ma_cache.py first")
        return
    with open(MA_CACHE_PATH, encoding="utf-8") as f:
        ma_cache = json.load(f)
    print(f"ma_cache date: {ma_cache.get('date')} ({len(ma_cache.get('stocks', {}))} 檔)")

    # start clean - this test's own throwaway state, never the real pool's
    if os.path.exists(TEST_STATE_FILE):
        os.remove(TEST_STATE_FILE)

    api, live = executor.login(False)
    tested = 0
    entries = 0
    exits = 0
    still_open = []

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for code in codes:
            cache_entry = ma_cache.get("stocks", {}).get(code)
            if cache_entry is None:
                continue
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            bars = minute_data.today_bars(api, contract)
            if len(bars) < 10:
                continue
            tested += 1
            mas = {p: cache_entry[f"ma{p}"] for p in (5, 10, 20)}
            name = cache_entry["name"]
            open_pos = None  # {"entry_price": ...} - this replay's own in-memory position

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

    print(f"測試了 {tested} 檔有今日分K資料的股票")
    print(f"今日盤中訊號：{entries} 次進場、{exits} 次出場")
    if still_open:
        print(f"收盤時仍處於訊號開倉狀態（本測試不強制平倉，因為這策略設計上是跨日持有）：")
        for code, name, entry_price in still_open:
            print(f"  {code}{name}｜進場@{entry_price}")
    print("結果已寫入 trading/trade_log.txt（標籤：MA支撐/破位-盤中測試）")

    if os.path.exists(TEST_STATE_FILE):
        os.remove(TEST_STATE_FILE)


if __name__ == "__main__":
    main()
