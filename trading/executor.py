"""Shioaji order placement, wrapped behind a two-factor safety gate.

SIMULATION IS THE ONLY MODE THIS CAN RUN IN until BOTH of these are true:
  1. --live is passed on the command line, AND
  2. the environment variable TRADING_LIVE_CONFIRM is set to the exact
     string config.LIVE_CONFIRM_VALUE (put it in .env, never hardcode it)
Missing either one keeps every order in Shioaji's simulation environment -
a bare --live typo or a stray env var with the wrong value both fail safe
into simulation, not into "live by accident".

Orders are full lots (StockOrderLot.Common, 1張=1000股, cash/現股, limit/ROD) -
trying this instead of odd-lot (零股) because odd-lot's ~3-minute batched
matching made stop-loss slippage a real risk; full lots trade in the
regular continuous session instead. The trade-off is position size is no
longer a fixed NTD amount - see config.MAX_TOTAL_POSITION_COST_NTD, which
caps total open-position cost instead. Don't switch the lot type back
without the user's say; see trading/README.md for the history here.
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
    """Shares per trade under the current full-lot sizing (1張)."""
    return config.ORDER_LOT_SHARES


def lot_cost(price):
    """What one full lot costs at `price` - used to check
    config.MAX_TOTAL_POSITION_COST_NTD before buying."""
    return price * config.ORDER_LOT_SHARES


def buy(api, contract, price, shares):
    import shioaji as sj

    order = api.Order(
        action=sj.Action.Buy,
        price=price,
        quantity=shares,
        price_type=sj.StockPriceType.LMT,
        order_type=sj.OrderType.ROD,
        order_lot=sj.StockOrderLot.Common,
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
        order_lot=sj.StockOrderLot.Common,
        order_cond=sj.StockOrderCond.Cash,
    )
    return api.place_order(contract, order)
