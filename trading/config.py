"""All tunable strategy/risk parameters in one place - change numbers here,
not scattered through the other trading/ modules.

Strategy: buy a stock when (1) its concept_map theme is currently trending
up, (2) its price has pulled back to a key moving average (support test,
not chasing), and (3) volume is picking up (confirms buyers stepping in at
support). Exit on stop-loss, take-profit, max holding period, or the theme
turning negative.
"""

# --- Position sizing ---------------------------------------------------
# 2026-10: reverted to allowing odd-lot (零股) orders, user-specified. A full
# lot (ORDER_LOT_SHARES) is still bought when the remaining budget covers
# it - full lots trade in the regular continuous matching session, so a
# stop-loss/take-profit on a full-lot position still fills promptly, unlike
# odd-lot's ~3-minute batched matching (the reason this was full-lot-only
# before). But when the remaining budget can't cover a full lot,
# executor.shares_to_buy() now buys whatever odd-lot quantity (1-999 shares)
# it CAN afford instead of skipping the candidate outright - user-specified:
# no stock should be excluded just because of its share price, since an
# odd-lot order can always size down to fit whatever budget is left.
# executor.buy()/sell() pick StockOrderLot.Common vs IntradayOdd automatically
# from the share count, so callers never need to choose explicitly.
ORDER_LOT_SHARES = 1_000               # 1 張 (board lot)
MAX_TOTAL_POSITION_COST_NTD = 200_000  # sum of entry_price * shares across ALL open positions
MIN_ODD_LOT_VALUE_NTD = 10_000  # an odd-lot buy only happens if the remaining budget covers at
# least this much NTD of the stock - user-specified: a candidate should still be skipped (not
# bought as some negligible 1-2 share sliver) if there isn't enough budget left for a real
# position, even though odd-lot orders technically accept any 1-999 share quantity.

# --- Exit rules ----------------------------------------------------------
STOP_LOSS_PCT = -0.05       # exit if unrealized return <= this
TAKE_PROFIT_PCT = 0.10      # exit if unrealized return >= this
MAX_HOLD_DAYS = 5           # exit if held longer than this many trading days

# --- Entry rules -----------------------------------------------------------
MA_TOLERANCE = 0.02         # "pulled back to the MA" = close within +/-2% of it
MA_PERIODS = (5, 20, 60)    # 周線/月線/季線 - ANY one within tolerance qualifies
VOLUME_RATIO_MIN = 1.5      # Shioaji volume_ratio must reach at least this
MIN_TRADE_VALUE_NTD = 100_000_000  # today's cumulative turnover (Shioaji snapshot's
# total_amount, NTD) must reach at least this - volume_ratio alone is relative to a
# stock's OWN past-5-day average, so a thinly-traded stock can pass it on a tiny
# absolute base (e.g. 50 lots) and still be too illiquid to trade cleanly; this is
# a separate, absolute floor. NT$1億/day, user-specified.

# --- Candidate ranking -----------------------------------------------------
# When more stocks pass the 3 entry conditions than the budget cap allows,
# rank them instead of buying in arbitrary (stock-code) order: each factor
# is min-max normalized across that day's qualifying candidates, then
# combined with these weights (must sum to 1.0) into one score - higher
# score buys first. User-specified weights, not Claude-chosen.
RANK_WEIGHTS = {
    "theme": 0.50,         # concept_map theme's median %change today - stronger theme wins
    "volume": 0.35,        # Shioaji volume_ratio - bigger volume spike wins
    "ma_precision": 0.15,  # closeness to the nearest MA5/20/60 - tighter pullback wins
}

# --- Universe --------------------------------------------------------------
# Only concept_map's curated, business-verified stocks - not the whole
# market. Keeps the candidate list small enough for per-stock kbars() calls
# and keeps risk to names we've actually vetted a real theme relationship for.

# --- Live-trading safety gate -----------------------------------------------
# Both of these must be true before executor.py ever passes simulation=False
# to Shioaji - a bare --live flag alone is NOT enough. See executor.py.
LIVE_CONFIRM_ENV_VAR = "TRADING_LIVE_CONFIRM"
LIVE_CONFIRM_VALUE = "I_UNDERSTAND_THIS_PLACES_REAL_ORDERS"

# --- State files (gitignored) -----------------------------------------------
POSITIONS_STATE_FILE = "trading/positions_state.json"
