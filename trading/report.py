"""Builds the plain-text daily analysis report: new entry signals found
after close, current open positions' status, and anything that exited
today. daily_scan.py calls build() and writes the result to
trading/reports/YYYY-MM-DD.txt (gitignored directory) plus stdout.
"""
import os
from datetime import date


def _fmt_pct(x):
    return f"{x * 100:+.2f}%"


def build(run_date, new_signals, open_status, exits_today, budget_skipped=None):
    """new_signals: list of (code, name, conditions_dict) that passed check_entry.
    open_status: list of (code, name, entry_price, current_price, days_held, exit_conditions_dict).
    exits_today: list of trading.positions.close_position() return records.
    budget_skipped: list of (code, name, close_price, score) that passed check_entry
    but got 0 shares from executor.shares_to_buy() - the remaining budget couldn't
    cover even 1 odd-lot share at that price (budget fully committed elsewhere,
    not a price ceiling - see config.py)."""
    budget_skipped = budget_skipped or []
    lines = [f"=== 題材回檔量增策略 - {run_date} 收盤後報告 ===", ""]

    lines.append(f"-- 新進場訊號 ({len(new_signals)}) --")
    if not new_signals:
        lines.append("（無）")
    for code, name, cond in new_signals:
        lines.append(f"{code} {name} -> 題材轉強/回檔支撐/量增 全部成立｜排序分數 {cond['score']:.2f}")

    if budget_skipped:
        lines.append("")
        lines.append(f"-- 訊號成立但資金上限已滿，跳過 ({len(budget_skipped)}) --")
        for code, name, close_price, score in budget_skipped:
            lines.append(f"{code} {name}｜股價 NT${close_price:,.2f}｜剩餘預算不足買進1股｜排序分數 {score:.2f}")

    lines.append("")
    lines.append(f"-- 持倉狀態 ({len(open_status)}) --")
    if not open_status:
        lines.append("（無持倉）")
    for code, name, entry_price, current_price, held, exit_cond in open_status:
        ret = exit_cond["unrealized_return"]
        flag = " <- 將出場" if exit_cond["exit_signal"] else ""
        lines.append(
            f"{code} {name}｜進場{entry_price} 現價{current_price} 損益{_fmt_pct(ret)}｜"
            f"持有{held}個交易日{flag}"
        )

    lines.append("")
    lines.append(f"-- 今日出場 ({len(exits_today)}) --")
    if not exits_today:
        lines.append("（無）")
    for rec in exits_today:
        lines.append(
            f"{rec['code']} {rec['name']}｜{rec['entry_price']} -> {rec['exit_price']}｜"
            f"實現損益{_fmt_pct(rec['realized_return'])}｜原因：{rec['exit_reason']}"
        )

    lines.append("")
    lines.append("※ 全部為模擬單（整股或零股），非真實交易。")
    return "\n".join(lines)


def save(text, run_date=None, out_dir="trading/reports"):
    run_date = run_date or date.today().isoformat()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{run_date}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path
