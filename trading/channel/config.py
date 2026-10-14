"""Own config for the trendline-channel strategy, promoted 2026-10 from
draft to live-scheduled status (see trading/decisions.json) - isolated
from swing/day-trade/ma_touch, same pattern as trading/ma_touch/config.py.

Core parameters (PIVOT_K, STOP_LOSS_PCT, MAX_HOLD_DAYS, etc.) are pulled
directly from trading/channel_backtest.py rather than restated here, so
this can never silently drift from what was actually validated (180-day
and 300-day train/test + 4-period independent validation - see
decisions.json for the numbers). If you want to change one of these,
change it in channel_backtest.py and re-run the validation scripts
(channel_backtest.py/channel_cv.py) first - don't just edit the value here.
"""
from .. import channel_backtest as _backtest

PIVOT_K = _backtest.PIVOT_K
STOP_LOSS_PCT = _backtest.STOP_LOSS_PCT
MAX_HOLD_DAYS = _backtest.MAX_HOLD_DAYS
MIN_TRADE_VALUE_NTD = _backtest.MIN_TRADE_VALUE_NTD
ORDER_LOT_SHARES = _backtest.ORDER_LOT_SHARES
MAX_TOTAL_POSITION_COST_NTD = _backtest.MAX_TOTAL_POSITION_COST_NTD
WARMUP_DAYS = _backtest.WARMUP_DAYS

# --- Live-trading safety gate -----------------------------------------------
# Same two-factor gate as every other strategy - see trading/executor.py.
LIVE_CONFIRM_ENV_VAR = "TRADING_LIVE_CONFIRM"
LIVE_CONFIRM_VALUE = "I_UNDERSTAND_THIS_PLACES_REAL_ORDERS"

# --- State file (gitignored) ------------------------------------------------
POSITIONS_STATE_FILE = "trading/channel/positions_state.json"
