"""All tunable parameters for the MA5/10/20 支撐/破位 strategy - a THIRD,
fully isolated strategy alongside the existing swing (trading/) and
day-trade (trading/daytrade/) ones. Deliberately separate config/signals/
positions-state/budget from both, so running this can never affect what
they're doing - see trading/ma_touch/README.md for why this exists and the
research it's based on.

Strategy (user-specified concept, grounded against both English and
Chinese trading-education sources - see decisions.json's rationale for the
links): a moving average acts as dynamic support while price holds above
it; a clean close below ALL of MA5/MA10/MA20 flips them from support to
resistance, and the move tends to continue until the stock finds a new
base - so this strategy enters on a confirmed bounce off one of those
lines and treats a full breakdown as an immediate exit, not a normal
%-based stop to wait out.
"""

# --- Universe -----------------------------------------------------------
# Same concept_map universe as the swing strategy (reused for convenience/
# known liquidity), but this strategy's signal is pure price action - it
# does NOT use concept_map's theme/median-%change condition at all.

# --- MA periods -----------------------------------------------------------
MA_PERIODS = (5, 10, 20)   # same periods as the dashboard's 均線總覽 tab -
# that tab is pure visualization and doesn't feed this strategy, but it's
# what originally prompted the user to ask for this strategy, so the
# periods are deliberately kept the same for a consistent mental model.

# --- Entry rule -------------------------------------------------------------
MA_TOUCH_TOLERANCE = 0.015   # "touched the MA" = close within +/-1.5% of it -
# tighter than the swing strategy's 2% since this is reacting to a single
# day's touch, not a multi-day basing zone
VOLUME_RATIO_MIN = 1.5       # 2026-10: raised from 1.3 to match the swing
# strategy's threshold - see decisions.json. The original 1.3 was set lower
# than swing's on the theory that this fires on a single reclaim day rather
# than a whole pullback, but the 1-month backtest that found this strategy
# unprofitable didn't isolate that assumption, so it's no longer treated as
# load-bearing - raised back to the one other volume threshold in this
# project that's actually been validated (swing's 1.5, see swing_backtest.py).
MIN_TRADE_VALUE_NTD = 100_000_000   # same absolute liquidity floor as the
# swing strategy (NT$1億/day) - user-specified floor, reused here for the
# same reason: volume_ratio alone is relative to a thin stock's own tiny base

# --- Exit rules -----------------------------------------------------------
STOP_LOSS_PCT = -0.06        # slightly wider than swing's -5% since the
# support_broken exit below is meant to catch a failed support bounce
# faster than a plain %-stop would anyway
TAKE_PROFIT_PCT = 0.10
MAX_HOLD_DAYS = 5
# support_broken: close below ALL of MA5/10/20 that were live that day -
# fires as its own exit condition (see signals.py), independent of
# STOP_LOSS_PCT, specifically because the research says a clean breakdown
# tends to keep going, so this doesn't wait for a fixed loss threshold

# --- Sizing / budget --------------------------------------------------------
# Own pool, own cap - never shares budget with trading/config.py's swing
# pool or trading/daytrade/config.py's two pools.
ORDER_LOT_SHARES = 1_000
MAX_TOTAL_POSITION_COST_NTD = 200_000

# --- Live-trading safety gate -----------------------------------------------
# Same two-factor gate as every other strategy in this project - see
# trading/executor.py. Reused directly, not reimplemented.
LIVE_CONFIRM_ENV_VAR = "TRADING_LIVE_CONFIRM"
LIVE_CONFIRM_VALUE = "I_UNDERSTAND_THIS_PLACES_REAL_ORDERS"

# --- State file (gitignored) ------------------------------------------------
POSITIONS_STATE_FILE = "trading/ma_touch/positions_state.json"
