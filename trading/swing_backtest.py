"""Backtest for the ACTUAL production swing strategy - trading/signals.py's
check_entry/check_exit/rank_candidates and trading/config.py's live constants,
called directly (not reimplemented), against daily OHLCV history. Every prior
backtest in this project (candlestick reversal, ORB, true/false breakout,
regression features, cross-validated grid search) tested a different/
experimental entry rule - none of them tested the rule currently running live
via daily_scan.py. This one does.

Usage:
    python -m trading.swing_backtest --days 182            # ~6 months (default)
    python -m trading.swing_backtest --days 182 --codes 2330 2317  # quick smoke test
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

import pandas as pd

from . import config, daily_scan, executor, positions, signals
from .daytrade import minute_data

FEE_RATE = 0.001425
TAX_RATE_NORMAL_SELL = 0.003  # normal (non-day-trade) 證交稅 - these are
                                # real multi-day holds, not same-day round trips

WARMUP_DAYS = 120  # extra calendar days fetched before the eval window so
                    # MA60 / the 5-day volume baseline are already populated
                    # on day 1 of the window, not building up mid-backtest


def daily_ohlcv(api, contract, start, end):
    bars = minute_data.bars_for_range(api, contract, start, end)
    if bars.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume", "Amount"])
    daily = bars.resample("1D").agg({"Open": "first", "High": "max", "Low": "min",
                                      "Close": "last", "Volume": "sum", "Amount": "sum"})
    daily = daily.dropna(subset=["Open"])
    daily.index = daily.index.date
    return daily


def prep(daily):
    """Adds the columns the production signals need, each computed as-of its
    own row from that row and earlier ones only (no look-ahead)."""
    daily = daily.copy()
    daily["pct"] = daily["Close"].pct_change() * 100
    for p in config.MA_PERIODS:
        daily[f"ma{p}"] = daily["Close"].rolling(p).mean()
    daily["volume_ratio"] = daily["Volume"] / daily["Volume"].shift(1).rolling(5).mean()
    return daily


def net_return(entry_price, exit_price, shares):
    cost = entry_price * shares * (1 + FEE_RATE)
    proceeds = exit_price * shares * (1 - FEE_RATE - TAX_RATE_NORMAL_SELL)
    pnl = proceeds - cost
    return pnl, pnl / cost


INSTITUTIONAL_FLOW_DAYS = 3  # trailing days summed for the institutional-flow condition


def load_institutional_flow(conn, flow_days=INSTITUTIONAL_FLOW_DAYS):
    """{code: Series(date -> trailing flow_days-day SUM of 三大法人買賣超股數)},
    TWSE-listed stocks only - TPEx's institutional dataset is a latest-
    snapshot endpoint with no historical date param (same limitation as
    monthly_revenue), so TPEx-listed concept_map stocks simply won't appear
    here; callers must treat "code not in this dict" as "can't evaluate
    this condition", not as a pass or fail. See backfill_institutional.py
    for how the underlying table gets its historical rows (not populated
    by the normal fetch_daily.py daily run, which only ever has today)."""
    rows = conn.execute(
        'SELECT "證券代號", iso_date, "三大法人買賣超股數" FROM institutional_investors'
    ).fetchall()
    by_code = {}
    for code, iso_date, flow in rows:
        try:
            by_code.setdefault(code, {})[date.fromisoformat(iso_date)] = float(flow)
        except (TypeError, ValueError):
            continue
    result = {}
    for code, series_dict in by_code.items():
        s = pd.Series(series_dict).sort_index()
        result[code] = s.rolling(flow_days).sum()
    return result


def run(stock_data, code_concepts, names, eval_dates, disable_theme_exit=False, inst_flow=None):
    """stock_data: {code: prepped daily df, indexed by date}. Replays the
    exact production decision day by day, oldest first, applying the same
    budget cap (config.MAX_TOTAL_POSITION_COST_NTD) daily_scan.py does.

    disable_theme_exit: experimental variant, NOT the production rule -
    passes theme_median_pct=None into check_exit so its theme_weakened
    condition can never fire (signals.check_exit's own logic, untouched;
    this just withholds the one input it needs), isolating how much of the
    strategy's P&L the theme_weakened exit is responsible for.

    inst_flow: experimental variant, NOT the production rule - optional
    {code: Series(date->trailing institutional flow)} from
    load_institutional_flow(). When given, a candidate only qualifies if
    it ALSO has positive trailing institutional flow on that day - this is
    an extra AND on top of signals.check_entry()'s own result, not a
    change to signals.py itself. A code with no institutional data at all
    (TPEx-listed) is excluded outright rather than silently passing."""
    open_positions = {}
    trades = []
    budget_skips = 0

    for d in eval_dates:
        live_today = {}
        for code, df in stock_data.items():
            if d in df.index:
                row = df.loc[d]
                if pd.notna(row["pct"]):
                    live_today[code] = {"pct": row["pct"]}
        concept_median = daily_scan.concept_median_pcts(code_concepts, live_today)

        # --- exits: same conditions as daily_scan.py's holding-period/
        # theme-weakened check, PLUS stop-loss/take-profit (intraday_monitor.py's
        # job live, but a daily backtest has no intraday feed to defer to, so
        # it's folded in here against the day's close) ---
        for code in list(open_positions):
            df = stock_data[code]
            if d not in df.index:
                continue
            row = df.loc[d]
            if pd.isna(row["Close"]):
                continue
            pos = open_positions[code]
            held = positions.days_held(pos["entry_date"], as_of=d)
            theme_pct = None if disable_theme_exit else \
                daily_scan.theme_pct_for_stock(code, code_concepts, concept_median, mode="min")
            exit_cond = signals.check_exit(pos["entry_price"], row["Close"], held, theme_pct)
            if exit_cond["exit_signal"]:
                reason = next(k for k in ("stop_loss", "take_profit", "max_hold_days", "theme_weakened")
                              if exit_cond[k])
                pnl, net_ret = net_return(pos["entry_price"], row["Close"], pos["shares"])
                trades.append({"code": code, "name": pos["name"], "entry_date": pos["entry_date"],
                                "exit_date": d.isoformat(), "entry_price": pos["entry_price"],
                                "exit_price": row["Close"], "reason": reason, "held_days": held,
                                "net_return": net_ret, "net_pnl": pnl})
                del open_positions[code]

        # --- entries: collect every qualifying candidate, rank them (same
        # signals.rank_candidates weights as production), then apply the
        # budget cap strongest-first - identical order to daily_scan.py ---
        candidates = []
        for code, df in stock_data.items():
            if code in open_positions or d not in df.index:
                continue
            row = df.loc[d]
            if pd.isna(row["Close"]) or pd.isna(row["volume_ratio"]) or pd.isna(row["Amount"]):
                continue
            mas = {p: row[f"ma{p}"] for p in config.MA_PERIODS if pd.notna(row[f"ma{p}"])}
            if not mas:
                continue
            theme_pct = daily_scan.theme_pct_for_stock(code, code_concepts, concept_median, mode="max")
            cond = signals.check_entry(row["Close"], row["volume_ratio"], mas, theme_pct, row["Amount"])
            if not cond["entry_signal"]:
                continue
            if inst_flow is not None:
                code_flow = inst_flow.get(code)
                if code_flow is None or d not in code_flow.index or pd.isna(code_flow.loc[d]) \
                        or code_flow.loc[d] <= 0:
                    continue
            candidates.append({"code": code, "close": row["Close"], "theme_pct": theme_pct,
                                "volume_ratio": row["volume_ratio"], "ma_distance_pct": cond["ma_distance_pct"]})

        running_cost = sum(p["entry_price"] * p["shares"] for p in open_positions.values())
        for c in signals.rank_candidates(candidates):
            remaining_budget = config.MAX_TOTAL_POSITION_COST_NTD - running_cost
            shares = executor.shares_to_buy(c["close"], remaining_budget)
            if shares < 1:
                budget_skips += 1
                continue
            cost = c["close"] * shares
            open_positions[c["code"]] = {"name": names.get(c["code"], c["code"]), "entry_price": c["close"],
                                          "shares": shares, "entry_date": d.isoformat()}
            running_cost += cost

    # force-close anything still open at the end of the window, at its last
    # known close, so the backtest doesn't silently drop unrealized P&L
    for code, pos in open_positions.items():
        last_valid = stock_data[code]["Close"].dropna()
        if last_valid.empty:
            continue
        exit_price = last_valid.iloc[-1]
        exit_date = last_valid.index[-1]
        held = positions.days_held(pos["entry_date"], as_of=exit_date)
        pnl, net_ret = net_return(pos["entry_price"], exit_price, pos["shares"])
        trades.append({"code": code, "name": pos["name"], "entry_date": pos["entry_date"],
                        "exit_date": exit_date.isoformat(), "entry_price": pos["entry_price"],
                        "exit_price": exit_price, "reason": "end_of_window_safety_net",
                        "held_days": held, "net_return": net_ret, "net_pnl": pnl})

    return trades, budget_skips


def summarize(trades):
    if not trades:
        return "沒有任何交易（這段期間沒有股票同時滿足題材一致性/回檔支撐/量增三條件）。"
    n = len(trades)
    wins = sum(1 for t in trades if t["net_return"] > 0)
    total_pnl = sum(t["net_pnl"] for t in trades)
    avg_ret = sum(t["net_return"] for t in trades) / n
    avg_held = sum(t["held_days"] for t in trades) / n
    by_reason = {}
    for t in trades:
        by_reason.setdefault(t["reason"], []).append(t)
    reason_lines = "\n".join(
        f"  {r}: {len(ts)}筆｜勝率{sum(1 for t in ts if t['net_return'] > 0)}/{len(ts)}｜"
        f"平均報酬{sum(t['net_return'] for t in ts) / len(ts) * 100:+.2f}%"
        for r, ts in sorted(by_reason.items())
    )
    return (f"總交易次數：{n}｜勝率：{wins}/{n}={wins/n*100:.1f}%｜"
            f"總損益：NT${total_pnl:,.0f}｜平均報酬：{avg_ret*100:+.2f}%｜平均持有：{avg_held:.1f}天\n"
            f"-- 依出場原因 --\n{reason_lines}")


def period_breakdown(trades):
    """Win rate/avg return by calendar month of entry - a single blended
    number over one window can look good purely by luck (see decisions.json's
    overfitting finding from the earlier cross-validated grid search); this
    shows whether the result holds up period over period instead of being
    concentrated in one lucky stretch."""
    if not trades:
        return "（無交易）"
    by_month = {}
    for t in trades:
        by_month.setdefault(t["entry_date"][:7], []).append(t)
    lines = []
    for month, ts in sorted(by_month.items()):
        n = len(ts)
        wins = sum(1 for t in ts if t["net_return"] > 0)
        avg_ret = sum(t["net_return"] for t in ts) / n
        pnl = sum(t["net_pnl"] for t in ts)
        lines.append(f"  {month}：{n}筆｜勝率{wins}/{n}={wins/n*100:.0f}%｜平均報酬{avg_ret*100:+.2f}%｜損益NT${pnl:,.0f}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=182, help="evaluation window in calendar days (default ~6 months)")
    parser.add_argument("--codes", nargs="*", default=None, help="subset of stock codes (default: full concept_map universe)")
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default="trading/swing_backtest_result.txt")
    parser.add_argument("--with-theme-exit", action="store_true",
                         help="restore the retired theme_weakened exit condition, for comparison against the "
                              "current production default (disabled 2026-10, see trading/decisions.json)")
    parser.add_argument("--require-institutional", action="store_true",
                         help="experimental: also require positive trailing %d-day 三大法人買賣超 flow to "
                              "enter (TWSE stocks only - see backfill_institutional.py; needs that backfill "
                              "to have been run first, not the production rule)" % INSTITUTIONAL_FLOW_DAYS)
    args = parser.parse_args()

    import db
    conn = sqlite3.connect(args.db or db.DB_PATH)
    code_concepts, names = daily_scan.load_universe(conn)
    inst_flow = load_institutional_flow(conn) if args.require_institutional else None
    conn.close()
    codes = args.codes if args.codes else sorted(code_concepts.keys())
    print(f"universe: {len(codes)} concept_map stocks")
    if inst_flow is not None:
        print(f"institutional flow data available for {len(inst_flow)} TWSE-listed codes")

    eval_start = date.today() - timedelta(days=args.days)
    fetch_start = eval_start - timedelta(days=WARMUP_DAYS)
    end = date.today()

    api, live = executor.login(False)
    stock_data = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for code in codes:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            daily = daily_ohlcv(api, contract, fetch_start.isoformat(), end.isoformat())
            if len(daily) < 65:  # need 60+ trading days of history before eval can even start
                continue
            stock_data[code] = prep(daily)
        api.logout()
    print(f"{len(stock_data)}/{len(codes)} 檔有足夠的歷史資料（含熱身期）")

    if not stock_data:
        print("no stocks with enough history - aborting")
        return

    all_dates = sorted(set().union(*(set(df.index) for df in stock_data.values())))
    eval_dates = [d for d in all_dates if d >= eval_start]
    print(f"回測區間：{eval_start.isoformat()} ~ {end.isoformat()}（{len(eval_dates)}個交易日）")

    trades, budget_skips = run(stock_data, code_concepts, names, eval_dates,
                                 disable_theme_exit=not args.with_theme_exit, inst_flow=inst_flow)

    variant_note = "（對照組：恢復theme_weakened出場 - 正式規則已於2026-10停用，見decisions.json）" if args.with_theme_exit else ""
    if inst_flow is not None:
        variant_note += f"（實驗變體：額外要求過去{INSTITUTIONAL_FLOW_DAYS}日三大法人買賣超為正，僅TWSE股票）"
    lines = [
        f"=== 現行波段策略正式規則回測（題材一致性+回檔支撐+量增）{variant_note}：{eval_start.isoformat()} ~ {end.isoformat()}（{len(stock_data)}檔股票）===",
        "",
        "沿用正式規則：trading/signals.py check_entry/check_exit/rank_candidates，trading/config.py 參數原封不動",
        f"（STOP_LOSS={config.STOP_LOSS_PCT*100:.0f}%｜TAKE_PROFIT={config.TAKE_PROFIT_PCT*100:.0f}%｜MAX_HOLD={config.MAX_HOLD_DAYS}天｜"
        f"VOLUME_RATIO_MIN={config.VOLUME_RATIO_MIN}｜MA_TOLERANCE=±{config.MA_TOLERANCE*100:.0f}%｜"
        f"MIN_TRADE_VALUE=NT${config.MIN_TRADE_VALUE_NTD:,.0f}｜預算上限=NT${config.MAX_TOTAL_POSITION_COST_NTD:,.0f}）",
        "注意：停損/停利用當日收盤價判定（正式環境中這兩項是intraday_monitor.py即時盤中價觸發，"
        "日線回測沒有盤中資料可用，可能低估真正的停損觸發速度/滑價）",
        f"因預算上限被跳過的候選進場次數：{budget_skips}",
        "",
        summarize(trades),
        "",
        "-- 按月份分期檢視（避免單一區間的結果只是運氣）--",
        period_breakdown(trades),
        "",
        "-- 交易明細 --",
    ]
    for t in sorted(trades, key=lambda x: x["entry_date"]):
        lines.append(
            f"{t['entry_date']} {t['code']}{t['name']}｜進場@{t['entry_price']:.2f} -> "
            f"{t['exit_date']} 出場@{t['exit_price']:.2f}｜{t['reason']}｜持有{t['held_days']}天｜"
            f"{t['net_return']*100:+.2f}%｜NT${t['net_pnl']:,.0f}"
        )

    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
