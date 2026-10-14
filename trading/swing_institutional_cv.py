"""Chronological train/test validation for swing_backtest.py's
--require-institutional variant - checking whether the 2026-10-13 finding
(baseline -NT$73,573 vs +institutional-flow +NT$37,394 over the same
182-stock, 180-day window) holds up on a held-out slice, or whether it's
another case of this project's recurring overfitting trap (see
decisions.json's K棒反轉 and ma_touch findings). Unlike those, there's no
parameter being tuned here (INSTITUTIONAL_FLOW_DAYS stays fixed at 3) -
this just checks whether the same fixed rule generalizes, split
chronologically so test is genuinely never seen before scoring.

Usage:
    python -m trading.swing_institutional_cv --days 180
"""
import argparse
import sqlite3
import warnings
from datetime import date, timedelta

from . import daily_scan, executor, swing_backtest as sb

TRAIN_FRACTION = 0.6
N_PERIODS = 4  # for --multi-period: how many roughly-equal contiguous
# chunks to split eval_dates into, each run independently - checks whether
# the institutional-flow condition's effect is consistent period over
# period, not just an artifact of where the one train/test boundary falls


def stats(trades):
    if not trades:
        return {"n": 0, "win_rate": None, "avg_return": None, "total_pnl": 0}
    n = len(trades)
    wins = sum(1 for t in trades if t["net_return"] > 0)
    avg_ret = sum(t["net_return"] for t in trades) / n
    total_pnl = sum(t["net_pnl"] for t in trades)
    return {"n": n, "win_rate": wins / n, "avg_return": avg_ret, "total_pnl": total_pnl}


def fmt(s):
    if s["n"] == 0:
        return "0筆交易"
    return f"{s['n']}筆｜勝率{s['win_rate']*100:.1f}%｜平均報酬{s['avg_return']*100:+.2f}%｜損益NT${s['total_pnl']:,.0f}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--out", default="trading/swing_institutional_cv_result.txt")
    parser.add_argument("--multi-period", action="store_true",
                         help=f"split into {N_PERIODS} roughly-equal contiguous chunks instead of a single "
                              "60/40 train/test split, to check whether the effect is consistent period over "
                              "period rather than an artifact of where the one split boundary falls")
    args = parser.parse_args()

    import db
    conn = sqlite3.connect(args.db or db.DB_PATH)
    code_concepts, names = daily_scan.load_universe(conn)
    inst_flow = sb.load_institutional_flow(conn)
    conn.close()
    codes = args.codes if args.codes else sorted(code_concepts.keys())
    print(f"universe: {len(codes)} concept_map stocks, institutional data for "
          f"{sum(1 for c in codes if c in inst_flow)}/{len(codes)} of them")

    eval_start = date.today() - timedelta(days=args.days)
    fetch_start = eval_start - timedelta(days=sb.WARMUP_DAYS)
    end = date.today()

    api, live = executor.login(False)
    stock_data = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for code in codes:
            contract = api.Contracts.Stocks.get(code)
            if contract is None:
                continue
            daily = sb.daily_ohlcv(api, contract, fetch_start.isoformat(), end.isoformat())
            if len(daily) < 65:
                continue
            stock_data[code] = sb.prep(daily)
        api.logout()
    print(f"{len(stock_data)}/{len(codes)} 檔有足夠的歷史資料（含熱身期）")

    all_dates = sorted(set().union(*(set(df.index) for df in stock_data.values())))
    eval_dates = [d for d in all_dates if d >= eval_start]

    if args.multi_period:
        chunk = len(eval_dates) / N_PERIODS
        periods = []
        for i in range(N_PERIODS):
            lo, hi = int(i * chunk), int((i + 1) * chunk) if i < N_PERIODS - 1 else len(eval_dates)
            periods.append((f"第{i+1}/{N_PERIODS}段", eval_dates[lo:hi]))
        title = f"{N_PERIODS}段獨立分期檢視（避免單一train/test切點誤導）"
        note = ("注意：這裡沒有調參數挑最佳解 - INSTITUTIONAL_FLOW_DAYS固定是3天；每段都是從零部位、零資金"
                "佔用獨立重跑（不是連續跑一次再切開看），檢查效果是不是每段都一致，而不是只取決於切點"
                "剛好落在哪裡")
    else:
        split_idx = int(len(eval_dates) * TRAIN_FRACTION)
        periods = [("Train", eval_dates[:split_idx]), ("Test", eval_dates[split_idx:])]
        title = "train/test驗證"
        note = ("注意：這裡沒有調參數挑最佳解 - INSTITUTIONAL_FLOW_DAYS固定是3天，只是檢查同一條固定規則"
                "在train跟test上是否都站得住，不是從多個選項裡挑出來的結果")

    for label, dates in periods:
        print(f"{label}: {dates[0]} ~ {dates[-1]} ({len(dates)}天)")

    lines = [
        f"=== 波段策略：籌碼面確認條件 {title}：{eval_start.isoformat()} ~ {end.isoformat()}"
        f"（{len(stock_data)}檔股票）===",
        "",
        note,
        "",
    ]

    for label, dates in periods:
        base_trades, _ = sb.run(stock_data, code_concepts, names, dates, inst_flow=None)
        inst_trades, _ = sb.run(stock_data, code_concepts, names, dates, inst_flow=inst_flow)
        base_s, inst_s = stats(base_trades), stats(inst_trades)
        lines.append(f"-- {label}（{dates[0].isoformat()} ~ {dates[-1].isoformat()}，{len(dates)}天）--")
        lines.append(f"  基準（無籌碼確認）：{fmt(base_s)}")
        lines.append(f"  +籌碼確認：        {fmt(inst_s)}")
        lines.append("")

    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nsaved: {args.out}")


if __name__ == "__main__":
    main()
