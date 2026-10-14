"""Alternative day-trade entry signals based on candlestick/chart patterns,
tested side-by-side with the ORB breakout in signals.py - the 2026-10
backtests found ORB has no real edge even on genuinely liquid/volatile
watchlist stocks (see trading/daytrade/README.md's 訊號比較). Each function
here answers ONLY "does the pattern fire on the latest bar" - the shared
volume/liquidity quality filters are applied separately (see
check_entry_with_pattern) so every signal type is compared under the same
quality bar, not just whichever pattern happens to also demand more volume.

ORB/momentum/rolling_breakout stay long-only. reversal_signal now covers
BOTH directions (bullish hammer/engulfing for long, bearish shooting-star/
engulfing for short) - see reversal_signal_both()/is_shooting_star()/
is_bearish_engulfing() - to support 資券當沖 (short via ShortSelling,
cover via MarginTrading; see trading/executor.py's short_sell()/
cover_buy()), added per user request to trade both directions.
"""
from .. import breakout_signals
from . import config, minute_data, signals


def _bar_shape(o, h, l, c):
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    return body, upper_wick, lower_wick


def is_hammer(bar, min_lower_wick_ratio=2.0, max_upper_wick_ratio=0.5):
    """錘子線：小實體靠近當根K棒區間上緣、下影線長（表示低點有買盤承接）。
    `bar`: 一列有 Open/High/Low/Close 的資料（dict 或 pandas Series 都可）。"""
    o, h, l, c = bar["Open"], bar["High"], bar["Low"], bar["Close"]
    rng = h - l
    if rng <= 0:
        return False
    body, upper_wick, lower_wick = _bar_shape(o, h, l, c)
    floor = max(body, rng * 0.05)  # avoid div-by-zero on a doji (body==0)
    return bool(lower_wick >= min_lower_wick_ratio * floor and upper_wick <= max_upper_wick_ratio * floor)


def is_bullish_engulfing(prev_bar, bar):
    """吞噬型態：前一根是陰線、這一根是陽線，且這一根的實體完全蓋住前一根實體。"""
    prev_bearish = prev_bar["Close"] < prev_bar["Open"]
    bullish = bar["Close"] > bar["Open"]
    engulfs = bar["Open"] <= prev_bar["Close"] and bar["Close"] >= prev_bar["Open"]
    return bool(prev_bearish and bullish and engulfs)


def is_shooting_star(bar, min_upper_wick_ratio=2.0, max_lower_wick_ratio=0.5):
    """射擊之星（倒錘子線）：小實體靠近當根K棒區間下緣、上影線長（表示高點
    有賣壓進場），是錘子線的鏡射——用在做空進場。"""
    o, h, l, c = bar["Open"], bar["High"], bar["Low"], bar["Close"]
    rng = h - l
    if rng <= 0:
        return False
    body, upper_wick, lower_wick = _bar_shape(o, h, l, c)
    floor = max(body, rng * 0.05)
    return bool(upper_wick >= min_upper_wick_ratio * floor and lower_wick <= max_lower_wick_ratio * floor)


def is_bearish_engulfing(prev_bar, bar):
    """空方吞噬型態：前一根是陽線、這一根是陰線，且這一根的實體完全蓋住
    前一根實體——多方吞噬型態的鏡射。"""
    prev_bullish = prev_bar["Close"] > prev_bar["Open"]
    bearish = bar["Close"] < bar["Open"]
    engulfs = bar["Open"] >= prev_bar["Close"] and bar["Close"] <= prev_bar["Open"]
    return bool(prev_bullish and bearish and engulfs)


def reversal_signal(bars):
    """K棒反轉型態（多方）：最新一根K棒出現錘子線或吞噬型態，任一成立即視為訊號。"""
    if len(bars) < 2:
        return False
    cur = bars.iloc[-1]
    prev = bars.iloc[-2]
    return bool(is_hammer(cur) or is_bullish_engulfing(prev, cur))


def reversal_signal_short(bars):
    """K棒反轉型態（空方）：最新一根K棒出現射擊之星或空方吞噬型態，任一
    成立即視為放空訊號——跟reversal_signal方向相反，條件結構相同。"""
    if len(bars) < 2:
        return False
    cur = bars.iloc[-1]
    prev = bars.iloc[-2]
    return bool(is_shooting_star(cur) or is_bearish_engulfing(prev, cur))


def momentum_signal(bars, n=3):
    """連續同向K棒：最新n根都是陽線（收盤>開盤），且最高價一根比一根高
    （不是只是陽線，還要動能持續往上）。"""
    if len(bars) < n:
        return False
    window = bars.iloc[-n:]
    bullish_all = bool((window["Close"] > window["Open"]).all())
    rising_highs = bool((window["High"].diff().iloc[1:] > 0).all())
    return bullish_all and rising_highs


def true_breakout_signal(bars, resistance_lookback=20, atr_period=14, volume_ma_period=20,
                          confirm_periods=2):
    """Minute-bar adaptation of trading/breakout_signals.py's true/false
    breakout methodology (https://chainstockinsight.com/true-vs-false-
    breakout-guide/) - the SAME function (it's bar-granularity-agnostic,
    just rolling windows over OHLCV), with every period scaled down from
    days to minutes: 20-day resistance/volume-MA -> 20-MINUTE, ATR(14
    days) -> ATR(14 minutes), "2-3 trading days confirmation" -> 2-3
    MINUTES. Needs resistance_lookback+atr_period+confirm_periods minutes
    of bars before it can fire at all."""
    entry_signal, _ = breakout_signals.true_breakout_entry_signal(
        bars, resistance_lookback=resistance_lookback, atr_period=atr_period,
        volume_ma_period=volume_ma_period, confirm_days=confirm_periods,
    )
    return bool(entry_signal.iloc[-1]) if len(entry_signal) else False


def rolling_breakout_signal(bars, window=10, max_range_pct=0.015):
    """滾動範圍突破：前`window`根K棒的高低區間先收斂到夠小（整理），
    最新一根的收盤價再突破這個區間的高點——跟ORB類似，但「區間」是隨時間
    滑動的，不是固定釘在開盤前5分鐘。"""
    if len(bars) < window + 1:
        return False
    prior = bars.iloc[-(window + 1):-1]
    cur = bars.iloc[-1]
    lo, hi = float(prior["Low"].min()), float(prior["High"].max())
    if lo <= 0:
        return False
    range_pct = (hi - lo) / lo
    return bool(range_pct <= max_range_pct and cur["Close"] > hi)


def volume_spike_signal(bars):
    """使用者指定：不看任何K棒型態/區間/連續性，只要量能瞬間放大就直接
    進場 - pattern永遠成立(True)，真正的進場條件完全來自
    check_entry_with_pattern()已經會套用的volume_increase(量比門檻，
    config.VOLUME_RATIO_MIN=3.0倍，即「爆大量」)/liquidity_ok兩個共用
    品質門檻，沒有額外的型態判斷。出場用專屬的停損-3%/停利8%
    （backtest.py的SIGNAL_EXIT_OVERRIDES），不是config.py的全域-2%/3%，
    因為這是使用者針對這個訊號specifically指定的數字。"""
    return True


PATTERN_FUNCS = {
    "orb": None,  # handled separately in signals.check_entry - kept for the comparison table only
    "reversal": reversal_signal,
    "reversal_short": reversal_signal_short,
    "reversal_both": None,  # handled separately in evaluate_entry() - checks both directions
    "momentum": momentum_signal,
    "rolling_breakout": rolling_breakout_signal,
    "true_breakout": true_breakout_signal,
    "volume_spike": volume_spike_signal,
}

# Which side opening on this signal's trigger means - everything except
# reversal_short is long-only (unchanged from before shorting was added).
SIGNAL_SIDES = {
    "orb": "long", "reversal": "long", "reversal_short": "short",
    "momentum": "long", "rolling_breakout": "long", "true_breakout": "long",
    "volume_spike": "long",
}


def check_entry_with_pattern(pattern_fn, bars, volume_ratio, trade_value,
                              volume_ratio_min=config.VOLUME_RATIO_MIN,
                              min_trade_value_ntd=config.MIN_TRADE_VALUE_NTD):
    """Same quality filters as signals.check_entry (volume_increase,
    liquidity_ok), but the direction/entry trigger comes from `pattern_fn`
    instead of an ORB breakout - this is what makes the pattern-vs-ORB
    comparison apples-to-apples rather than confounded by different filters."""
    conditions = {
        "pattern": pattern_fn(bars),
        "volume_increase": volume_ratio is not None and volume_ratio >= volume_ratio_min,
        "liquidity_ok": trade_value is not None and trade_value >= min_trade_value_ntd,
    }
    conditions["entry_signal"] = all(conditions.values())
    return conditions


def evaluate_entry(signal_type, bars):
    """Single entry point both backtest.py and daytrade_monitor.py call -
    one place for "how do we decide to enter" so the two never drift apart.
    `bars`: everything available as of now (today_bars() live, or a
    growing historical slice in the backtest). Returns
    (entry_signal: bool, sort_key: float, close: float, side: "long"|"short") -
    sort_key is volume_ratio, used to rank candidates when more than one
    fires in the same minute and the budget cap can't take them all.

    signal_type == "reversal_both" checks BOTH reversal_signal (long) and
    reversal_signal_short (short) and opens whichever one fires - this is
    the 多空都做 mode. The two patterns are mutually exclusive by
    construction (a hammer and a shooting star can't both describe the
    same bar), so at most one ever fires."""
    close = minute_data.latest_close(bars)
    vr = minute_data.minute_volume_ratio(bars)
    tv = minute_data.cumulative_trade_value(bars)
    if signal_type == "orb":
        orh = minute_data.opening_range_high(bars, config.ORB_WINDOW_MINUTES)
        or_pct = minute_data.opening_range_pct(bars, config.ORB_WINDOW_MINUTES)
        cond = signals.check_entry(close, orh, vr, tv, or_pct)
        return cond["entry_signal"], (vr or 0), close, "long"
    if signal_type == "reversal_both":
        long_cond = check_entry_with_pattern(reversal_signal, bars, vr, tv)
        if long_cond["entry_signal"]:
            return True, (vr or 0), close, "long"
        short_cond = check_entry_with_pattern(reversal_signal_short, bars, vr, tv)
        return short_cond["entry_signal"], (vr or 0), close, "short"
    cond = check_entry_with_pattern(PATTERN_FUNCS[signal_type], bars, vr, tv)
    return cond["entry_signal"], (vr or 0), close, SIGNAL_SIDES[signal_type]
