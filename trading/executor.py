"""Shioaji order placement, wrapped behind a two-factor safety gate.

SIMULATION IS THE ONLY MODE THIS CAN RUN IN until BOTH of these are true:
  1. --live is passed on the command line, AND
  2. the environment variable TRADING_LIVE_CONFIRM is set to the exact
     string config.LIVE_CONFIRM_VALUE (put it in .env, never hardcode it)
Missing either one keeps every order in Shioaji's simulation environment -
a bare --live typo or a stray env var with the wrong value both fail safe
into simulation, not into "live by accident".

buy()/sell() place cash/現股 limit/ROD orders and pick the order_lot
(StockOrderLot.Common vs IntradayOdd) automatically from the share count -
a multiple of config.ORDER_LOT_SHARES goes through as full lot(s), anything
else as odd-lot (零股). This was full-lot-only for a while (odd-lot's
~3-minute batched matching makes stop-loss slippage a real risk) but was
reverted 2026-10, user-specified, to stop MAX_TOTAL_POSITION_COST_NTD from
excluding a stock outright just because one full lot of it costs more than
the whole budget - see trading/README.md for the history and
shares_to_buy()'s own docstring for the current sizing rule.
"""
import os
import warnings

from . import config


def is_live_confirmed():
    return os.environ.get(config.LIVE_CONFIRM_ENV_VAR) == config.LIVE_CONFIRM_VALUE


def login(cli_live_flag):
    """Returns a logged-in Shioaji api instance. `simulation` is computed
    here, once, from both gate conditions - every other module should get
    its api handle from this function rather than constructing its own.

    Uses SHIOAJI_API_KEY_all/SHIOAJI_SECRET_KEY_all, NOT the plain
    SHIOAJI_API_KEY/SHIOAJI_SECRET_KEY that realtime_quote.py and
    build_dashboard_live.py use - that key pair was deliberately scoped to
    market-data-only permission (see the 2026-10 conversation comparing
    the two key pairs) and place_order rejects it with a 401
    "Token doesn't have permission" even in simulation mode. Trading
    needs the full-permission key."""
    import utils

    utils.load_env()
    api_key = os.environ.get("SHIOAJI_API_KEY_all")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY_all")
    if not api_key or not secret_key:
        raise RuntimeError("Missing SHIOAJI_API_KEY_all/SHIOAJI_SECRET_KEY_all in .env - "
                            "trading needs the full-permission key pair, not the data-only one")

    live = bool(cli_live_flag) and is_live_confirmed()
    if cli_live_flag and not is_live_confirmed():
        print(f"[safety] --live was passed but {config.LIVE_CONFIRM_ENV_VAR} is not set to the "
              f"confirmation value - staying in SIMULATION mode.")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import shioaji as sj

        api = sj.Shioaji(simulation=not live)
        api.login(api_key=api_key, secret_key=secret_key)

    print(f"[mode] {'LIVE - REAL ORDERS' if live else 'simulation (no real money)'}")
    return api, live


def lot_shares():
    """Shares in one full lot (1張) - still the preferred size when the
    budget covers it, see shares_to_buy()."""
    return config.ORDER_LOT_SHARES


def lot_cost(price):
    """What one full lot costs at `price`."""
    return price * config.ORDER_LOT_SHARES


def shares_to_buy(price, remaining_budget):
    """How many shares to buy at `price` given `remaining_budget` (NTD) -
    a full lot if it fits, otherwise whatever odd-lot quantity (1-999
    shares) the remaining budget affords, PROVIDED that's still worth at
    least config.MIN_ODD_LOT_VALUE_NTD - otherwise 0 (skip rather than buy
    a negligible sliver; user-specified). No candidate is skipped just
    because its share price is high relative to the budget, though - buy()
    auto-selects IntradayOdd for any non-full-lot share count this returns."""
    if price <= 0:
        return 0
    if lot_cost(price) <= remaining_budget:
        return config.ORDER_LOT_SHARES
    odd_shares = int(remaining_budget // price)
    if odd_shares * price < config.MIN_ODD_LOT_VALUE_NTD:
        return 0
    return odd_shares


def _order_lot_for(shares):
    import shioaji as sj

    return sj.StockOrderLot.Common if shares % config.ORDER_LOT_SHARES == 0 else sj.StockOrderLot.IntradayOdd


def buy(api, contract, price, shares):
    import shioaji as sj

    order = api.Order(
        action=sj.Action.Buy,
        price=price,
        quantity=shares,
        price_type=sj.StockPriceType.LMT,
        order_type=sj.OrderType.ROD,
        order_lot=_order_lot_for(shares),
        order_cond=sj.StockOrderCond.Cash,
    )
    return api.place_order(contract, order)


def sell(api, contract, price, shares):
    import shioaji as sj

    order = api.Order(
        action=sj.Action.Sell,
        price=price,
        quantity=shares,
        price_type=sj.StockPriceType.LMT,
        order_type=sj.OrderType.ROD,
        order_lot=_order_lot_for(shares),
        order_cond=sj.StockOrderCond.Cash,
    )
    return api.place_order(contract, order)


def short_sell(api, contract, price, shares):
    """融券賣出 - opens a short position. Needs a 信用交易帳戶 (margin
    trading account) with 融資融券 permission approved by the broker -
    a separate account-level prerequisite from 現股當沖 (see
    trading/daytrade/README.md), not something this code can satisfy.
    Also needs the stock to actually have 融券 availability (不是所有
    股票都能融券，額度也可能用罄) - Shioaji will reject the order if not,
    same as any other broker-side rejection; this function doesn't
    pre-check availability."""
    import shioaji as sj

    order = api.Order(
        action=sj.Action.Sell,
        price=price,
        quantity=shares,
        price_type=sj.StockPriceType.LMT,
        order_type=sj.OrderType.ROD,
        order_lot=sj.StockOrderLot.Common,
        order_cond=sj.StockOrderCond.ShortSelling,
    )
    return api.place_order(contract, order)


def cover_buy(api, contract, price, shares):
    """融資買進回補 - closes a short position same-day (資券當沖的回補
    腳)。order_cond is MarginTrading (not ShortSelling) - this is the
    user-specified mechanism: short via 融券, cover via 融資, letting
    TWSE's day-trade netting rule settle both legs same-day without
    needing the shares to actually deliver first."""
    import shioaji as sj

    order = api.Order(
        action=sj.Action.Buy,
        price=price,
        quantity=shares,
        price_type=sj.StockPriceType.LMT,
        order_type=sj.OrderType.ROD,
        order_lot=sj.StockOrderLot.Common,
        order_cond=sj.StockOrderCond.MarginTrading,
    )
    return api.place_order(contract, order)
