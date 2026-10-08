"""Simulated-position journal: open/close positions, compute holding
period and P&L, persisted to a local JSON file (trading/positions_state.json,
gitignored). This is the single source of truth for "what does the
strategy currently hold" - daily_scan.py and intraday_monitor.py both
read/write through here, never the raw file directly.
"""
import json
import os
from datetime import date, timedelta

from . import config


def _load(state_file=config.POSITIONS_STATE_FILE):
    if not os.path.exists(state_file):
        return {"open": {}, "closed": []}
    with open(state_file, encoding="utf-8") as f:
        return json.load(f)


def _save(state, state_file=config.POSITIONS_STATE_FILE):
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def open_position(code, name, entry_price, shares, entry_date=None,
                   state_file=config.POSITIONS_STATE_FILE):
    """No-op (returns False) if this code is already held - one simulated
    position per stock at a time, never pyramid into an existing one."""
    state = _load(state_file)
    if code in state["open"]:
        return False
    state["open"][code] = {
        "name": name,
        "entry_price": entry_price,
        "shares": shares,
        "entry_date": entry_date or date.today().isoformat(),
        "simulated": True,
    }
    _save(state, state_file)
    return True


def close_position(code, exit_price, exit_reason, exit_date=None,
                    state_file=config.POSITIONS_STATE_FILE):
    """Returns the closed-trade record, or None if `code` wasn't open."""
    state = _load(state_file)
    pos = state["open"].pop(code, None)
    if pos is None:
        return None
    realized_return = (exit_price - pos["entry_price"]) / pos["entry_price"]
    record = {
        **pos,
        "code": code,
        "exit_price": exit_price,
        "exit_date": exit_date or date.today().isoformat(),
        "exit_reason": exit_reason,
        "realized_return": round(realized_return, 4),
    }
    state["closed"].append(record)
    _save(state, state_file)
    return record


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
