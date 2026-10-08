"""Backtest for the ORB day-trade strategy over real historical minute bars.

Reuses the EXACT same pure functions daytrade_monitor.py calls live
(minute_data.* and signals.check_entry/check_exit) - the only thing this
script does differently is replay them against a growing slice of
HISTORICAL bars instead of "today's bars so far", and keep an in-memory
trade ledger instead of positions_state.json (no persistence needed for a
one-shot backtest, and this must never touch the real paper-trading state).

No look-ahead: at simulated minute t, every signal input is computed from
bars up to and including t only - never later bars - by literally slicing
each stock's day to `day_df[day_df.index <= t]` before calling the same
functions trading/daytrade/minute_data.py already exposes.

Fees are modeled for a realistic P&L, not just raw price return:
  - 手續費 (brokerage) 0.1425% each way (buy and sell)
  - 證交稅 (transaction tax) 0.15% on sell - the HALVED rate that applies
    specifically to 現股當沖 (cash day-trade), not the normal 0.3%
These are standard published rates, not brokerage-specific discounts -
actual cost may be lower if the user's broker discounts commission.

Usage:
    python -m trading.daytrade.backtest --days 30
    python -m trading.daytrade.backtest --days 30 --codes 2330 2454  # small subset, for validation
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import db
from .. import executor
from . import config, minute_data, signals

FEE_RATE = 0.001425        # 手續費, each way
TAX_RATE_DAYTRADE_SELL = 0.0015  # 證交稅 on sell, 現股當沖 halved rate


def load_universe(conn):
    codes = [r[0] for r in conn.execute("SELECT DISTINCT stock_id FROM concept_map")]
    names = dict(conn.execute('SELECT "公司代號", "公司簡稱" FROM company_info'))
    return sorted(codes), names


def net_return(entry_price, exit_price, shares):
    cost = entry_price * shares * (1 + FEE_RATE)
    proceeds = exit_price * shares * (1 - FEE_RATE - TAX_RATE_DAYTRADE_SELL)
    pnl = proceeds - cost
    return pnl, pnl / cost


def run(bars_by_code, names):
    """bars_by_code: {code: DataFrame} (minute_data.bars_for_range output,
    spanning the whole backtest window). Returns (trades, days_simulated)."""
    all_days = sorted(set().union(*(set(df.index.normalize()) for df in bars_by_code.values())))
    open_positions = {}  # code -> {entry_price, entry_time, shares}
    trades = []

    for day in all_days:
        day_views = {code: df[df.index.normalize() == day] for code, df in bars_by_code.items()}
        day_views = {c: d for c, d in day_views.items() if not d.empty}
        all_ts = sorted(set().union(*(set(d.index) for d in day_views.values())))

        for t in all_ts:
            # --- exits ---
            for code in list(open_positions.keys()):
                day_df = day_views.get(code)
                if day_df is None:
                    continue
                view = day_df[day_df.index <= t]
                if view.empty:
                    continue
                close = minute_data.latest_close(view)
                now_hhmm = minute_data.latest_time_hhmm(view)
                pos = open_positions[code]
                cond = signals.check_exit(pos["entry_price"], close, now_hhmm)
                if cond["exit_signal"]:
                    reason = next(k for k in ("stop_loss", "take_profit", "force_flatten") if cond[k])
                    pnl, net_ret = net_return(pos["entry_price"], close, pos["shares"])
                    trades.append({
                        "code": code, "name": names.get(code, code),
                        "entry_date": day.date().isoformat(), "entry_time": pos["entry_time"].strftime("%H:%M"),
                        "entry_price": pos["entry_price"], "exit_time": t.strftime("%H:%M"),
                        "exit_price": close, "exit_reason": reason,
                        "gross_return": cond["unrealized_return"], "net_return": round(net_ret, 4),
                        "net_pnl": round(pnl, 0),
                    })
                    del open_positions[code]

            # --- entries ---
            candidates = []
            for code, day_df in day_views.items():
                if code in open_positions:
                    continue
                view = day_df[day_df.index <= t]
                if view.empty:
                    continue
                now_hhmm = minute_data.latest_time_hhmm(view)
                if now_hhmm is not None and now_hhmm >= config.FORCE_FLATTEN_TIME:
                    continue
                close = minute_data.latest_close(view)
                orh = minute_data.opening_range_high(view, config.ORB_WINDOW_MINUTES)
                vr = minute_data.minute_volume_ratio(view)
                tv = minute_data.cumulative_trade_value(view)
                cond = signals.check_entry(close, orh, vr, tv)
                if cond["entry_signal"]:
                    candidates.append((code, close, vr))
            candidates.sort(key=lambda item: item[2], reverse=True)

            running_cost = sum(p["entry_price"] * p["shares"] for p in open_positions.values())
            for code, close, vr in candidates:
                cost = close * config.ORDER_LOT_SHARES
                if running_cost + cost > config.MAX_TOTAL_POSITION_COST_NTD:
                    continue
                open_positions[code] = {"entry_price": close, "entry_time": t, "shares": config.ORDER_LOT_SHARES}
                running_cost += cost

        # end-of-day safety net: force_flatten should have closed everything
        # by 13:00 already, but if a stock's last bar that day is before
        # 13:00 (early halt) there'd be nothing left to trigger it - close
        # at the day's last available price rather than silently carrying
        # a position into the next day (which would misrepresent a
        # same-day-only strategy).
        for code in list(open_positions.keys()):
            day_df = day_views.get(code)
            if day_df is None or day_df.empty:
                continue
            close = minute_data.latest_close(day_df)
            pos = open_positions[code]
            pnl, net_ret = net_return(pos["entry_price"], close, pos["shares"])
            trades.append({
                "code": code, "name": names.get(code, code),
                "entry_date": day.date().isoformat(), "entry_time": pos["entry_time"].strftime("%H:%M"),
                "entry_price": pos["entry_price"], "exit_time": day_df.index[-1].strftime("%H:%M"),
                "exit_price": close, "exit_reason": "end_of_day_safety_net",
                "gross_return": round((close - pos["entry_price"]) / pos["entry_price"], 4),
                "net_return": round(net_ret, 4), "net_pnl": round(pnl, 0),
            })
            del open_positions[code]

    return trades, all_days


def summarize(trades):
    if not trades:
        return "沒有任何交易訊號成立。"
    n = len(trades)
    wins = [t for t in trades if t["net_return"] > 0]
    total_pnl = sum(t["net_pnl"] for t in trades)
    avg_net_return = sum(t["net_return"] for t in trades) / n
    avg_gross_return = sum(t["gross_return"] for t in trades) / n
    best = max(trades, key=lambda t: t["net_return"])
    worst = min(trades, key=lambda t: t["net_return"])
    reasons = {}
    for t in trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1

    lines = [
        f"總交易次數：{n}",
        f"勝率（扣手續費/證交稅後）：{len(wins)}/{n} = {len(wins) / n * 100:.1f}%",
        f"總損益（含手續費0.1425%雙邊+當沖證交稅0.15%）：NT${total_pnl:,.0f}",
        f"平均每筆報酬（含費用）：{avg_net_return * 100:+.2f}%",
        f"平均每筆報酬（不含費用，純價格變動）：{avg_gross_return * 100:+.2f}%",
        f"最佳單筆：{best['code']} {best['name']} {best['net_return']*100:+.2f}%（{best['entry_date']}）",
        f"最差單筆：{worst['code']} {worst['name']} {worst['net_return']*100:+.2f}%（{worst['entry_date']}）",
        "出場原因分布：" + "、".join(f"{k}:{v}" for k, v in reasons.items()),
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="calendar days back from today")
    parser.add_argument("--codes", nargs="*", default=None, help="subset of stock codes (default: full concept_map universe)")
    parser.add_argument("--db", default=db.DB_PATH)
    parser.add_argument("--out", default="trading/daytrade/backtest_result.txt")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    all_codes, names = load_universe(conn)
    conn.close()
    codes = args.codes if args.codes else all_codes
    print(f"universe: {len(codes)} stocks")

    end = date.today()
    start = end - timedelta(days=args.days)

    api, live = executor.login(False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        bars_by_code = {}
        for code in codes:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            bars = minute_data.bars_for_range(api, contract, start.isoformat(), end.isoformat())
            if not bars.empty:
                bars_by_code[code] = bars
        api.logout()
    print(f"{len(bars_by_code)}/{len(codes)} stocks had historical minute bars")

    trades, all_days = run(bars_by_code, names)
    summary = summarize(trades)

    lines = [
        f"=== 當沖策略回測：{start.isoformat()} ~ {end.isoformat()}（{len(all_days)}個交易日，{len(bars_by_code)}檔股票）===",
        "",
        summary,
        "",
        "-- 交易明細 --",
    ]
    for t in trades:
        lines.append(
            f"{t['entry_date']} {t['code']} {t['name']}｜{t['entry_time']} 進場@{t['entry_price']:.2f} -> "
            f"{t['exit_time']} 出場@{t['exit_price']:.2f}｜原因:{t['exit_reason']}｜"
            f"淨報酬{t['net_return']*100:+.2f}%｜淨損益NT${t['net_pnl']:,.0f}"
        )
    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
