"""Simulated-position journal: open/close positions, compute holding
period and P&L, persisted to a local JSON file (trading/positions_state.json,
gitignored). This is the single source of truth for "what does the
strategy currently hold" - daily_scan.py and intraday_monitor.py both
read/write through here, never the raw file directly.

Every open_position()/close_position() call also appends one line to
TRADE_LOG_FILE (trading/trade_log.txt, gitignored) - a flat, append-only,
real-time record of every simulated trade across EVERY strategy that
shares this module (swing, day-trade liquidity/ORB, ai_chain - see
SOURCE_LABELS), since all of them call through here rather than writing
their own state. User-specified 2026-10: a single running document, not
another per-day report file.
"""
import datetime
import json
import os
from datetime import date, timedelta

from . import config

FEE_RATE = 0.001425              # 手續費, each way
TAX_RATE_NORMAL_SELL = 0.003     # 證交稅 on sell, multi-day hold
TAX_RATE_DAYTRADE_SELL = 0.0015  # 證交稅 on sell, halved rate for same-day 現股當沖

TRADE_LOG_FILE = "trading/trade_log.txt"

SOURCE_LABELS = {
    "trading/positions_state.json": "波段(swing)",
    "trading/daytrade/positions_state.json": "當沖-liquidity/ORB",
    "trading/daytrade/ai_chain_positions_state.json": "當沖-ai_chain",
    "trading/ma_touch/positions_state.json": "MA支撐/破位(草案)",
    "trading/ma_touch/_intraday_test_positions_state.json": "MA支撐/破位-盤中測試(非正式)",
    "trading/ma_touch/_top20_test_positions_state.json": "MA支撐/破位-Top20量能波動測試(非正式)",
    "trading/channel/positions_state.json": "趨勢線通道",
}


def _log_trade(line, state_file):
    source = SOURCE_LABELS.get(state_file, state_file)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(os.path.dirname(TRADE_LOG_FILE) or ".", exist_ok=True)
    with open(TRADE_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{ts}｜{source}｜{line}\n")


def _load(state_file=config.POSITIONS_STATE_FILE):
    if not os.path.exists(state_file):
        return {"open": {}, "closed": []}
    with open(state_file, encoding="utf-8") as f:
        return json.load(f)


def _save(state, state_file=config.POSITIONS_STATE_FILE):
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def open_position(code, name, entry_price, shares, entry_date=None, side="long",
                   state_file=config.POSITIONS_STATE_FILE):
    """No-op (returns False) if this code is already held - one simulated
    position per stock at a time, never pyramid into an existing one.

    side: "long" (bought first - default, unchanged behavior for every
    existing caller) or "short" (融券賣出 first - see trading/executor.py's
    short_sell()/cover_buy() and trading/daytrade's bidirectional K棒反轉
    signal)."""
    state = _load(state_file)
    if code in state["open"]:
        return False
    state["open"][code] = {
        "name": name,
        "entry_price": entry_price,
        "shares": shares,
        "entry_date": entry_date or date.today().isoformat(),
        "side": side,
        "simulated": True,
    }
    _save(state, state_file)
    _log_trade(f"[開倉] {code}{name}｜@{entry_price}｜{shares}股｜side={side}", state_file)
    return True


def net_return(entry_price, exit_price, shares, side="long", is_daytrade=False):
    """Fee+tax-adjusted P&L - the live-tracking equivalent of every backtest
    script's own net_return() (channel_backtest.py, swing_backtest.py, etc.),
    so a reported % here means the same thing a backtest's % means: what you'd
    actually keep after FEE_RATE each way plus the sell leg's transaction tax
    (halved for a same-day 現股當沖 exit vs. a normal multi-day hold's sell
    tax). For a short position the tax is charged on the SELL leg, which is
    the ENTRY (融券賣出), not the exit (回補買回) - mirrors
    trading/daytrade/backtest.py's convention. This is for P&L REPORTING
    only - signals.py's own check_exit() functions compare RAW (pre-fee)
    price moves against their stop-loss/take-profit thresholds, since
    that's how every strategy was backtested and validated; don't feed this
    into an exit decision."""
    tax_rate = TAX_RATE_DAYTRADE_SELL if is_daytrade else TAX_RATE_NORMAL_SELL
    if side == "long":
        cost = entry_price * shares * (1 + FEE_RATE)
        proceeds = exit_price * shares * (1 - FEE_RATE - tax_rate)
    else:
        proceeds = entry_price * shares * (1 - FEE_RATE - tax_rate)
        cost = exit_price * shares * (1 + FEE_RATE)
    pnl = proceeds - cost
    return pnl, pnl / cost


def close_position(code, exit_price, exit_reason, exit_date=None,
                    state_file=config.POSITIONS_STATE_FILE):
    """Returns the closed-trade record, or None if `code` wasn't open.
    realized_return/realized_pnl are NET of fees+tax (see net_return()) -
    same-day entry/exit is treated as a 現股當沖 exit (halved sell tax)."""
    state = _load(state_file)
    pos = state["open"].pop(code, None)
    if pos is None:
        return None
    side = pos.get("side", "long")  # old records predating `side` default to long
    exit_date = exit_date or date.today().isoformat()
    is_daytrade = exit_date == pos["entry_date"]
    pnl, realized_return = net_return(pos["entry_price"], exit_price, pos["shares"],
                                       side=side, is_daytrade=is_daytrade)
    record = {
        **pos,
        "code": code,
        "exit_price": exit_price,
        "exit_date": exit_date,
        "exit_reason": exit_reason,
        "realized_return": round(realized_return, 4),
        "realized_pnl": round(pnl, 0),
    }
    state["closed"].append(record)
    _save(state, state_file)
    _log_trade(f"[平倉] {code}{pos.get('name', '')}｜@{pos['entry_price']}->{exit_price}｜"
               f"{realized_return*100:+.2f}%(已扣手續費/稅)｜原因:{exit_reason}", state_file)
    return record


def unrealized(entry_price, current_price, shares, side="long"):
    """Same fee+tax-adjusted P&L as net_return(), for an OPEN position
    marked to the current price - i.e. "what you'd net if you closed right
    now." Always uses the normal (non-daytrade) sell tax rate, since every
    pool that can still be open when this is called (swing/channel/ma_touch)
    is a multi-day hold by construction - the day-trade pool always flattens
    before end of day, so it never has open positions to mark."""
    return net_return(entry_price, current_price, shares, side=side, is_daytrade=False)


def days_held(entry_date_str, as_of=None):
    """Trading days elapsed since entry (Mon-Fri approximation - doesn't
    account for TWSE holidays, which is fine for a 5-day hold-limit rule;
    see utils/fetch_daily.py's holiday handling if exactness ever matters)."""
    as_of = as_of or date.today()
    entry = date.fromisoformat(entry_date_str)
    span = (as_of - entry).days
    return sum(1 for i in range(1, span + 1) if (entry + timedelta(days=i)).weekday() < 5)


def total_open_cost(state_file=config.POSITIONS_STATE_FILE):
    """Sum of entry_price * shares across every currently-open position -
    checked against config.MAX_TOTAL_POSITION_COST_NTD before buying."""
    open_positions = _load(state_file)["open"]
    return sum(p["entry_price"] * p["shares"] for p in open_positions.values())


def list_open(state_file=config.POSITIONS_STATE_FILE):
    return _load(state_file)["open"]


def list_closed(state_file=config.POSITIONS_STATE_FILE):
    return _load(state_file)["closed"]
