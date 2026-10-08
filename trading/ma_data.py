"""Historical moving averages for the pullback-to-support entry check.

twstock.db doesn't have 60 trading days of history yet (fetch_daily.py has
never run as a daily schedule), so MA5/20/60 come from Shioaji's kbars()
instead - pulled per-stock, only for the (small, ~150-stock) concept_map
universe, not the whole market.

Shioaji's kbars() returns minute bars (columnar: ts/Open/High/Low/Close/
Volume/Amount arrays), not daily bars directly - there is no "give me daily
OHLC" mode for stocks. We resample to daily closes ourselves.
"""
from datetime import date, timedelta

import pandas as pd

DAYS_BACK = 90  # calendar days of history to pull - enough trading days for MA60


def daily_closes(api, contract):
    """Fetch ~DAYS_BACK calendar days of minute bars and resample to one
    closing price per trading day. Returns a pandas Series indexed by date,
    oldest first. Empty Series if Shioaji returns nothing (e.g. new listing)."""
    end = date.today()
    start = end - timedelta(days=DAYS_BACK)
    kbars = api.kbars(contract, start=start.isoformat(), end=end.isoformat())
    if not kbars.ts:
        return pd.Series(dtype=float)

    df = pd.DataFrame({"ts": pd.to_datetime(kbars.ts), "close": kbars.Close})
    df = df.set_index("ts")
    return df["close"].resample("1D").last().dropna()


def compute_mas(closes, periods=(5, 20, 60)):
    """closes: pandas Series from daily_closes(). Returns {period: ma_value}
    only for periods that have enough history; an early-listed stock with
    < 60 trading days just won't get a MA60 entry (that's correct, not a bug -
    there's no 季線 to test support against yet)."""
    return {
        p: round(closes.tail(p).mean(), 2)
        for p in periods
        if len(closes) >= p
    }


def fetch_mas_for_universe(api, codes_to_contracts, periods=(5, 20, 60)):
    """codes_to_contracts: {code: Shioaji contract}. Returns {code: {period: ma}},
    skipping any stock Shioaji has no contract or no kbars for."""
    result = {}
    for code, contract in codes_to_contracts.items():
        if contract is None:
            continue
        closes = daily_closes(api, contract)
        mas = compute_mas(closes, periods)
        if mas:
            result[code] = mas
    return result
