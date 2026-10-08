"""Append-only day-trade activity log. daytrade_monitor.py runs every minute
but most minutes nothing happens, so (same philosophy as
trading/intraday_monitor.py) this only writes when something actually did -
a new ORB entry or an exit - appending a timestamped line to one growing
file per day, not a fresh file per minute-long run.
"""
import os
from datetime import date, datetime


def append(new_entries, exits, out_dir="trading/daytrade/reports"):
    """new_entries: list of {code, name, close, or_high, volume_ratio, trade_value}.
    exits: list of trading.positions.close_position() records with 'name' added.
    Returns the file path written to, or None if there was nothing to log."""
    if not new_entries and not exits:
        return None
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{date.today().isoformat()}.txt")
    ts = datetime.now().strftime("%H:%M:%S")
    lines = []
    for e in new_entries:
        lines.append(
            f"[{ts}] 進場 {e['code']} {e['name']}｜突破開盤區間高點{e['or_high']:.2f} -> {e['close']:.2f}"
            f"｜量比{e['volume_ratio']:.2f}倍｜當日成交金額{e['trade_value']/1e8:.2f}億"
        )
    for rec in exits:
        lines.append(
            f"[{ts}] 出場 {rec['code']} {rec['name']}｜{rec['entry_price']} -> {rec['exit_price']}"
            f"｜實現損益{rec['realized_return'] * 100:+.2f}%｜原因：{rec['exit_reason']}"
        )
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path
