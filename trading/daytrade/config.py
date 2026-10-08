"""Day-trade strategy parameters - a separate pool/config from trading/config.py
(the swing strategy): different risk tolerance (tighter stop/take, same-day
flatten) and all signal inputs come from minute bars, not daily data or
Shioaji snapshots. See trading/daytrade/README.md for the full rule set.
"""

# --- Entry: Opening Range Breakout (ORB) -----------------------------------
ORB_WINDOW_MINUTES = 5          # 09:00-09:05 opening range
VOLUME_RATIO_MIN = 2.0          # latest minute's volume vs today's own per-minute average so far
MIN_TRADE_VALUE_NTD = 100_000_000  # cumulative turnover today (sum of minute bars' Amount)

# --- Exit -------------------------------------------------------------------
STOP_LOSS_PCT = -0.02
TAKE_PROFIT_PCT = 0.03
FORCE_FLATTEN_TIME = "13:00"    # flatten everything by this time regardless of P&L - no overnight hold

# --- Position sizing - separate pool from the swing strategy ---------------
ORDER_LOT_SHARES = 1_000
MAX_TOTAL_POSITION_COST_NTD = 600_000

# --- Live-trading safety gate - same mechanism as trading/config.py, reused
# via trading.executor directly (not duplicated here).

# --- State files (gitignored) -----------------------------------------------
POSITIONS_STATE_FILE = "trading/daytrade/positions_state.json"
