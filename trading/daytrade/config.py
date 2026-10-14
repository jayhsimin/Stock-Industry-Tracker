"""Day-trade strategy parameters - a separate pool/config from trading/config.py
(the swing strategy): different risk tolerance (tighter stop/take, same-day
flatten) and all signal inputs come from minute bars, not daily data or
Shioaji snapshots. See trading/daytrade/README.md for the full rule set.
"""

# --- Entry: Opening Range Breakout (ORB) -----------------------------------
ORB_WINDOW_MINUTES = 5          # 09:00-09:05 opening range
VOLUME_RATIO_MIN = 3.0          # latest minute's volume vs today's own per-minute average so far
                                 # raised from 2.0 after the 2026-10 backtest showed 72% of
                                 # trades just drifted to the 13:00 force-flatten with near-
                                 # zero price movement - a stronger volume filter should cut
                                 # down on low-conviction "breakouts" that don't continue
MIN_TRADE_VALUE_NTD = 100_000_000  # cumulative turnover today (sum of minute bars' Amount)
MIN_OR_RANGE_PCT = 0.01         # opening range (high-low)/low must be >= 1% - same backtest
                                 # finding: a breakout above a dead-flat open has nothing
                                 # behind it; this rejects stocks whose first 5 minutes barely
                                 # moved at all, regardless of whether the later "breakout" clears

# --- Exit -------------------------------------------------------------------
STOP_LOSS_PCT = -0.02
TAKE_PROFIT_PCT = 0.03
FORCE_FLATTEN_TIME = "13:00"    # flatten everything by this time regardless of P&L - no overnight hold

# --- Pre-market watchlist selection -----------------------------------------
# Which stocks to even consider today, picked from the WHOLE market (not
# concept_map's theme-curated universe) by yesterday's liquidity + volatility.
# Added after the 2026-10 backtest traced most bad trades back to thinly-
# traded, low-volatility concept_map stocks that happened to clear the ORB
# conditions on noise, not genuine momentum.
WATCHLIST_SIZE = 30
WATCHLIST_WEIGHTS = {"liquidity": 0.7, "volatility": 0.3}  # user-specified
WATCHLIST_STATE_FILE = "trading/daytrade/watchlist_state.json"

# --- News-driven watchlist (alternative to the liquidity/volatility one
# above) - selection comes from overnight bullish catalyst news instead,
# entry timing from patterns.reversal_signal (K棒反轉型態) instead of ORB.
# See news_watchlist.py's docstring for why this isn't fully automated yet.
NEWS_WATCHLIST_SIZE = 5
NEWS_WATCHLIST_STATE_FILE = "trading/daytrade/news_watchlist_state.json"
NEWS_BASELINE_DAYS = 5  # "相對討論度" baseline window - today's mention count vs
                         # this stock's own average over the preceding N windows,
                         # same 量比-style idea as minute_volume_ratio

# --- AI supply-chain single-stock selection (ai_chain_watchlist.py) ---------
# User-specified 3-step pipeline: (1) concept_map's AI伺服器 membership,
# (2) yesterday's trading value >= this "大量" bar, (3) among survivors,
# the single stock with the highest cnyes news-mention count in the
# overnight window wins - exactly ONE stock traded per day, not a list.
AI_CHAIN_CONCEPTS = ("AI伺服器", "ABF載板", "PCB印刷電路板", "封測")  # 2026-10 擴大，原本只有
                                        # AI伺服器(9檔)選股過度集中在少數大型股；加入上下游製造
                                        # 業題材增加多樣性，使用者明確指定要加這三個
AI_CHAIN_SELECT_TOP_N = 3  # 2026-10再收緊：10檔交易次數太多，把K棒反轉型態本身沒有優勢的
                            # 問題放大成38倍虧損；改成只留相對討論度最高的3檔
AI_CHAIN_TARGET_EXPOSURE_NTD = 100_000  # 取代固定1張(1000股) - 固定張數在台達電(1700元)
                                          # 跟一般股票(50-100元)上曝險差30倍以上，是前一版
                                          # 回測虧損被放大的主因之一
AI_CHAIN_MAX_PRICE_MULTIPLIER = 1.5  # 股價超過 (曝險金額*此倍數/1000股) 就跳過這檔——
                                       # 零股當沖會員臨3分鐘批次撮合、賣不掉的問題(已在
                                       # trading/daytrade/README.md記錄過)，所以高價股
                                       # 寧可不交易，不是硬塞零股
AI_CHAIN_MIN_VOLUME_NTD = 100_000_000  # reusing the same NT$1億 "大量" bar
                                        # already established for day-trade
                                        # liquidity elsewhere in this module -
                                        # flagged here, not silently reused
AI_CHAIN_STATE_FILE = "trading/daytrade/ai_chain_state.json"  # separate cache
                                        # from news_watchlist_state.json - this
                                        # pipeline is cheap enough (9 stocks, 1
                                        # news call) to compute inline in
                                        # daytrade_monitor.py, not pre-market
AI_CHAIN_POSITIONS_STATE_FILE = "trading/daytrade/ai_chain_positions_state.json"
AI_CHAIN_MAX_TOTAL_POSITION_COST_NTD = 600_000  # own budget pool, separate from
                                        # the liquidity+orb task's - so each
                                        # design's live paper-trading performance
                                        # can be evaluated independently, not
                                        # competing for the same simulated capital

# --- Position sizing - separate pool from the swing strategy ---------------
ORDER_LOT_SHARES = 1_000
MAX_TOTAL_POSITION_COST_NTD = 600_000

# --- Live-trading safety gate - same mechanism as trading/config.py, reused
# via trading.executor directly (not duplicated here).

# --- State files (gitignored) -----------------------------------------------
POSITIONS_STATE_FILE = "trading/daytrade/positions_state.json"
