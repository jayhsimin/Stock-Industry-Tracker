# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A daily data pipeline + analysis toolkit for Taiwan stocks, covering both TWSE-listed
(上市) and TPEx-listed (上櫃). It pulls fundamentals + chip/trading data from TWSE's and
TPEx's free official APIs into a local SQLite DB (`twstock.db`), then layers on
official-industry and self-curated theme/concept classification so news-driven "which
stocks does this affect" questions can be answered by querying real data instead of
guessing from memory alone. See `README.md` for full usage docs and worked examples; this
file is the architecture map.

## Commands

```bash
pip install -r requirements.txt        # only dependency: requests

python fetch_daily.py                  # pull today's snapshot into twstock.db
python fetch_daily.py --date 2026-09-24  # backfill/refetch a specific date (upsert-safe) -
                                          # only real for datasets with historical query
                                          # support (margin_trading, institutional_investors);
                                          # stock_price/stock_valuation CANNOT be backfilled

python industry_report.py --date 2026-09-24              # 4 reports grouped by official 產業別
python concept_report.py --date 2026-09-24 [--detail]    # ranks self-curated concepts by avg %change
python margin_report.py --date 2026-10-27                # market-wide leverage + bull/bear tilt
python stock_profile.py 2330                              # one company's full fundamentals profile

python stock_lookup.py --codes 2603 2609
python stock_lookup.py --names "長榮" "陽明"
python stock_lookup.py --industry "半導體業" "金融保險業"
python stock_lookup.py --concept "AI伺服器"
python stock_lookup.py --list-industries

python concept_map.py add --concept "AI伺服器" --codes 2382 3231 --note "..."
```

No test suite or linter is configured in this project.

## Architecture

**Data flow:** `fetch_daily.py` iterates the endpoint registry in `datasets.py`, calls
`twse_client.fetch_json()`, and hands each list of dicts to `db.upsert_records()`, which
creates the table on first use and upserts by the dataset's declared key columns. Every
dataset covers **all** companies on its market in one HTTP call — no per-stock looping
anywhere in this codebase.

**Everything is stored as TEXT** — source APIs return JSON strings; `db.py` never casts.
Any numeric query needs `CAST(NULLIF(col, '') AS REAL)` (empty string, not NULL, means "no
value"). See `industry_report.py`'s `NUM`/`PCT_CHANGE` fragments for the pattern.

**Two API response shapes, one client.** `twse_client.fetch_json()` normalizes both: bare
JSON arrays (`openapi.twse.com.tw`) and the legacy `www.twse.com.tw` shape
(`{"stat","fields","data"}`, `stat != "OK"` → no data yet, returns `[]` not an error).

**Legacy-endpoint numbers have thousands-separator commas** (T86 institutional-investors:
`"6,070,992"`, which `CAST AS REAL` would silently truncate to `6.0`). `twse_client.py`
strips commas at ingestion. Check for this before trusting any aggregate on a new legacy
(`www.twse.com.tw`) endpoint.

**ROC-calendar dates.** TWSE embeds ROC strings (`"1150924"` = 2026-09-24) in datasets with
their own `Date` field; `utils.roc_to_iso()` converts. Datasets with no embedded date
(`margin_trading`, `institutional_investors`) get stamped with whatever `--date` was passed
instead — check `datasets.py`'s `date_field`/`date_is_roc` before trusting a table's
`iso_date`.

**Publish lag + holidays.** `stock_price`/`stock_valuation` publish later in the evening
than `margin_trading`/`institutional_investors` for the same day. The market can also close
for several consecutive days (TWSE's `holidaySchedule` has the calendar) — don't assume the
latest weekday has data.

**`STOCK_DAY_ALL`/`BWIBBU_ALL` have NO real historical-query support — `--date` is silently
ignored.** They always return whatever TWSE currently considers "latest," regardless of the
requested date. `margin_trading`/`institutional_investors` genuinely do honor `--date` (no
embedded `Date` field, so `fetch_daily.py` stamps whatever was passed). **A
`stock_price`/`stock_valuation` gap from not running `fetch_daily.py` daily is permanent and
cannot be backfilled** — only daily-scheduled runs accumulate real history for these two.

**`warrant_info`/`warrant_trading` cover 權證, both markets.** TWSE's `t187ap37_L`/
`t187ap42_L` and TPEx's `mopsfin_t187ap37_O`/`mopsfin_t187ap42_O` already share identical
Chinese field names for `warrant_info` (no remap); `warrant_trading` only differs in one
field (`WARRANT_TRADING_TPEX_MAP`, mode `"partial"`). `warrant_info` upserts by `權證代號`
alone (like `company_info`); `warrant_trading` is dated by `交易日期`. TWSE's
`t187ap42_L` can return `[]` even when `STOCK_DAY_ALL` has same-day data — a publish-timing
quirk, not a bug; treated the same as any other "no data yet" response.

**TWSE and TPEx merge into the same tables under different field names.** Every TPEx entry
in `datasets.py` carries a `remap = (mapping_dict, mode)` applied before upsert:
- `mode="full"` (`twse_client.remap_full`) — build a clean record using **only** the mapped
  fields. Required wherever downstream SQL hardcodes column names: `stock_price`,
  `stock_valuation`, `margin_trading`, `institutional_investors`, `ex_dividend_schedule`.
- `mode="partial"` (`remap_partial`) — rename only the key fields, keep the rest. Used for
  `company_info` and income/balance-statement tables, where extra columns are harmless.
- `db._ensure_table` evolves tables with `ALTER TABLE ADD COLUMN` on every batch (not just
  table creation) since TWSE/TPEx rows rarely have identical column sets even after remap.
- TPEx doesn't split dealer buy/sell into self-trading vs. hedging; `fetch_daily.py` fills
  the overall `自營商買賣超股數` from the self-trading figure for TPEx rows, leaving
  hedging-specific columns empty (absent, not wrong).
- TWSE's institutional-investor data is a legacy endpoint needing an explicit `date` param
  (special-cased in `fetch_daily.py`); TPEx's (`tpex_3insti_daily_trading`) is a normal
  latest-snapshot endpoint through the standard `fetch_dataset()` path.
- `dividend` and `annual_trading_stats`/`monthly_trading_stats` are TWSE-only (no TPEx
  equivalent reconciled/found); TPEx codes just get empty results there, not errors.

**`industry_map` is a view, not a table.** Official 產業別 lives in `company_info` only as
a numeric code. `db.ensure_views()` (end of every `fetch_daily.py` run) rebuilds
`industry_map` from `monthly_revenue`'s human-readable 產業別 (each company's latest
reporting month). Always query this view for official-industry grouping, never
`company_info."產業別"` directly — it's just a number.

**`concept_map` is hand-curated, not derived** — no free official/scraped TWSE theme
classification exists (FinMind's is paid Sponsor-tier). Built one theme at a time via
`concept_map.py add`, only after a news analysis verifies the real business relationship.
**A stock can belong to multiple concepts** — PK is `(concept, stock_id)`, not `stock_id`.

**`stock_lookup.py` is the shared validation/enrichment layer** under `--codes`/`--names`/
`--industry`/`--concept`, joining `company_info` + `industry_map` + today's
`stock_price`/`stock_valuation`/`institutional_investors`/`margin_trading`, sorted by
`|三大法人買賣超|` descending. Two things to preserve: (1) `--names` is two-tier — short
name (`公司簡稱`) first, full-name substring match flagged `[weak match]` (full-name overlap
gives real false positives, e.g. "長榮" matching "李長榮科技" which is unrelated); (2)
candidates dedupe through `code -> set(sources)` before lookup so a stock matching multiple
`--industry`/`--concept` args shows once with a `matched_via` column.

**Shioaji (永豐金) is query-only, intentionally.** `realtime_quote.py` reuses
`stock_lookup.py`'s selector functions directly (imported, not duplicated). No
order-placement code exists outside `trading/` — don't add trading/execution code without
the user explicitly asking and discussing risk controls first. `simulation=True` is the
default everywhere outside `trading/`; Shioaji's simulation mode still returns real market
quotes, only order execution is simulated.

**`market_movers.py` filters warrants/ETFs via `INNER JOIN company_info`, not just
`industry_map`** — `stock_price` contains every security including leveraged warrants that
dominate a naive %change ranking. Shioaji's `scanners()` was tried and rejected (skewed
toward thin-volume stocks clustered at exactly ±10%); our own EOD data is more reliable.

**`margin_report.py` computes the 融資融券 tab's market-wide leverage/bull-bear data,
entirely from `margin_trading`** (TWSE MI_MARGN + TPEx equivalent). Every row carries both
今日餘額 and 前日餘額, so day-over-day change is arithmetic on one row, no self-join needed.
Balances are in 張 (1000-share lots); NT$ = `張 × 1000 × close`. `fetch_rows()` INNER JOINs
`company_info` to exclude ETFs/bonds the same way `market_movers.py` does. Three gotchas
found building this, all still live risks if touched carelessly:
- **`pick_latest_usable_date()`** exists because `margin_trading`'s own `MAX(iso_date)` can
  have zero rows joinable against `stock_price` — a partial/failed fetch can leave the two
  markets dated a day apart (TWSE's legacy endpoint stamps the run date; TPEx carries its
  own embedded date), so TWSE-code price rows never match TPEx-code margin rows for that
  date. It walks dates newest-first for the first one with actually-joinable rows. Always
  use it instead of a raw `MAX(iso_date)` query.
- **`build_market_history()`'s 張-count totals deliberately skip the `stock_price` join** —
  `margin_trading` genuinely backfills historical dates but `stock_price` does not, so
  requiring that join would cut 19 real dates down to ~4. 資券比/張-counts don't need price.
- **`avg_margin_distance_pct`/`avg_short_distance_pct` (市場斷頭風險) are a deliberate
  hybrid, not a backtest**: each stock's distance-to-call is computed ONCE from today's
  price (no historical price series exists to compute it per-date from), then re-weighted
  per historical date by that date's real margin balance — "today's prices applied to how
  exposure was distributed over time," not "the actual risk back then." Must stay disclosed
  in the UI. `_stock_call_distances()` uses each stock's own latest `stock_price` date
  (`MAX(iso_date) GROUP BY Code`), not one shared date — a shared date can land on a
  TPEx-only snapshot with zero overlap against TWSE-only `monthly_trading_stats`.

**`stock_profile.py` assembles one company's full fundamentals profile** for the 個股基本面
tab — company info, 融資融券狀況 (shown first, per request), current price/PE, monthly
revenue, income/balance statement, dividends, shareholder-meeting/ex-dividend calendar,
annual/monthly price range. Pure SQL, no Shioaji, so it covers the whole market (not just
`concept_map`'s ~180). `margin_call_distance()` estimates 融資/融券 forced-liquidation
distance using **user-supplied** thresholds (`MARGIN_CALL_DROP_PCT`=22%,
`SHORT_CALL_RISE_PCT`=46%, not a regulatory figure — no dataset here has real per-stock
融資成數/融券保證金成數 or per-position entry price), applied to `monthly_trading_stats`'
latest `WeightedAvgPriceAB` as a cost-basis proxy. Always a labeled estimate, never exact.
`margin_history()` is uncapped (unlike every other category) since `margin_trading` is the
one table that genuinely accumulates real daily history. Four datasets support this:
`shareholder_meetings` (TWSE `t187ap41_L` + TPEx `t187ap41_O`, identical fields, keyed on
公司代號+股東常臨時會+開會日期 since one company can have several meetings; `dividend`'s own
股東會日期 is often blank, this is the reliable source), `ex_dividend_schedule` (TWSE
`TWT48U_ALL` + TPEx `tpex_exright_prepost`, remapped — a rolling **near-term preannouncement
only**, past dates just drop off the list), `annual_trading_stats`/`monthly_trading_stats`
(TWSE `FMNPTK_ALL`/`FMSRFK_ALL`, confirmed **single-period snapshots** not multi-year
history — every row in one fetch shares the same Year/Month; real depth only grows as
`fetch_daily.py` keeps running). `company_info."產業別"` is numeric — `company_info()` LEFT
JOINs `industry_map` for the real name. `build_stock_profiles_cache.py` precomputes all
~2000 profiles to `stock_profiles_cache.json` (~15-20s, too slow for live refresh) — both
dashboard builders just read the cache.

**The dashboard's focus+context brush history chart is one generic function,
`drawHistoryChart(opts)`** (`dashboard.html.template`), reused by 個股基本面's per-stock
融資融券 chart and the market-wide chart — `opts` carries element selectors, the data array,
and a `metric` config ({label, decimals, suffix, dataDesc, formula, meaning}). Fix it once
for both charts; don't fork per-caller. Every chart (including the bubble chart) renders a
資料內容/公式/代表意義 explainer box below it via `chartExplainerHtml()`, updated whenever
the active metric/mode changes.

**`trading/` is a deliberately isolated module — a real (user-specified) swing strategy
with a hard simulation-only gate.** See `trading/README.md` for the full rule set. The one
fact that must never regress: `trading/executor.py`'s order functions only reach
`simulation=False` when BOTH `--live` is passed AND the env var in `trading/config.py`
(`LIVE_CONFIRM_ENV_VAR`) equals its exact confirmation string — never loosen to one
condition. Needs the full-permission `SHIOAJI_API_KEY_all`/`SHIOAJI_SECRET_KEY_all` pair,
not the data-only pair other scripts use. A `406 Please sign <account> first` means CA
activation is still outstanding on the broker's side, not a code bug — never handle a
user's CA certificate/password directly. `ma_data.py` resamples Shioaji minute-bar
`kbars()` to daily closes itself (no native daily-OHLC mode for stocks).

**`trading/daytrade/` is a second, independent strategy (ORB day-trade)** reusing
`executor.py`/`positions.py` but with its own config/data layer. Every signal input comes
from `api.kbars()` for TODAY only (never `api.snapshots()`, whose `volume_ratio`/
`total_amount` are day-over-day, not minute-level). `backtest.py` replays the same
`minute_data.py`/`signals.py` functions against historical kbars with no look-ahead.
Candidates come from `watchlist.py` — whole-market (not `concept_map`) liquidity+volatility,
cached once pre-market by `build_watchlist.py` (~10-12 min for ~2000 stocks, too slow for
the 1-minute monitor cadence) — a backtest traced losing trades to thin `concept_map`
stocks clearing ORB conditions on noise, hence the wider universe. Supports long AND short
via 資券當沖 (`short_sell()`/`cover_buy()` use `ShortSelling`/`MarginTrading` order conds,
not plain Cash) — functions taking a `side` ("long"/"short") flip which price direction is
a win, never assume "up" without checking `side`. `news_api.py` is the programmatic track
(cnyes `tw_stock_news`, genuinely supports date-range queries, unlike TWSE's own
announcements endpoint) — mention-count ranking skews toward mega-caps, a known bias.
`--source ai_chain` trades at most ONE stock/day: `concept_map`'s AI伺服器 members →
volume filter → most cnyes mentions wins, cheap enough to compute inline on cache miss.

**`trading/decisions.json` is the strategy rulebook's single source of truth**, rendered
read-only by the dashboard's 交易決策 tab (not just the swing strategy's). Any new
trading-strategy decision should be appended here, not left only in conversation/git
history — it's what makes past decisions (and why) visible without re-deriving from chat.

## File structure

- `fetch_daily.py` — daily orchestrator; also calls `db.ensure_views()`
- `datasets.py` — endpoint registry for both markets (T86 lives in `twse_client.py`)
- `twse_client.py` — HTTP layer: response normalization, comma-stripping, T86 fetch, remap helpers
- `db.py` — generic upsert + `industry_map` view
- `utils.py` — ROC-date conversion, date helpers
- `industry_report.py` / `concept_report.py` / `margin_report.py` / `stock_profile.py` —
  the four standalone report/profile builders (see Architecture above)
- `stock_lookup.py` — shared lookup/validation/enrichment
- `concept_map.py` — CRUD for the hand-curated `concept_map` table
- `shioaji_test.py` — first-contact Shioaji connectivity test
- `realtime_quote.py` — intraday quotes via Shioaji, query-only
- `market_movers.py` — whole-market %change ranking + industry-representation trend
- `dashboard.html.template` — 題材雷達/交易決策/均線總覽/融資融券/個股基本面 tabs;
  `__DATA_JSON__` is the data injection point — never edit built `dashboard.html` directly
- `build_dashboard.py` / `build_dashboard_live.py` — fill the template from twstock.db (EOD)
  or Shioaji (intraday), both producing `dashboard.html`; the live build also writes
  `dashboard_push.json` for the Artifact `db`-push path
- `build_ma_cache.py` — pre-computes MA5/10/20 for `concept_map` stocks into `ma_cache.json`
  (gitignored, once-a-day — Shioaji `kbars()` isn't batchable, takes minutes for ~180 stocks)
- `build_stock_profiles_cache.py` — pre-computes all companies' profiles into
  `stock_profiles_cache.json` (gitignored, once-a-day, ~15-20s pure SQL)
- `refresh_dashboard.bat` — entry point for the `TWDashboardLiveRefresh` Windows scheduled
  task (every 1 min)
- `trading/` — isolated swing-trading module; see Architecture above and `trading/README.md`
- `volume_alert.py` — Windows toast on Shioaji `volume_ratio`, once/trading-day/stock
  (`volume_alert_state.json`); PowerShell child uses `CREATE_NO_WINDOW` + `utf-8-sig` script
  encoding (both fixed real bugs — console flash, garbled Chinese toast text — don't revert).
  **Never test against the full ~2000-stock universe with a near-zero threshold** — queues
  one toast per stock; verify with a direct `volume_alert.send_toast(...)` call instead.

**Two separate dashboard auto-refresh mechanisms — don't conflate them.**
1. **Pure-local (active):** `TWDashboardLiveRefresh` task runs `build_dashboard_live.py`
   every minute, rewriting `dashboard.html` on disk; the page's own `RELOAD_SECONDS`
   countdown does `location.reload()` to pick it up — zero Claude involvement once set up,
   but viewing is local-file-only. Active/selected-stock tab state is preserved across that
   reload via `localStorage` (`twDashboardActivePage`/`twDashboardActiveProfileCode`) — a
   reload no longer bounces the viewer back to 題材雷達.
2. **Artifact `db` push (built, not active):** the Artifact subscribes to `dashboard/latest`
   via `onSnapshot`, but a browser can't reach Shioaji and `write_db` is Claude-tool-only, so
   keeping it live needs a session-side `ScheduleWakeup` loop. Don't re-offer this as the
   default; it's the fallback when a shareable link matters more than zero Claude usage.
