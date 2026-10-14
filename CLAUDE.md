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
python fetch_daily.py --date 2026-09-24  # backfill/refetch a specific date (upsert-safe, re-run anytime)

python industry_report.py --date 2026-09-24              # 4 reports grouped by official 產業別
python concept_report.py --date 2026-09-24 [--detail]    # ranks self-curated concepts by avg %change

python stock_lookup.py --codes 2603 2609
python stock_lookup.py --names "長榮" "陽明"
python stock_lookup.py --industry "半導體業" "金融保險業"
python stock_lookup.py --concept "AI伺服器"
python stock_lookup.py --list-industries

python concept_map.py add --concept "AI伺服器" --codes 2382 3231 --note "..."
python concept_map.py show --concept "AI伺服器"
python concept_map.py list
```

There is no test suite or linter configured in this project.

## Architecture

**Data flow:** `fetch_daily.py` iterates the endpoint registry in `datasets.py`, calls
`twse_client.fetch_json()` for each, and hands the resulting list of dicts to
`db.upsert_records()`, which creates the destination table on first use (columns inferred
from whatever keys the API returned) and upserts by the dataset's declared key columns.
Every dataset in `datasets.py` covers **all** companies on its market (TWSE or TPEx) in a
single HTTP call — there is no per-stock looping anywhere in this codebase.

**Everything is stored as TEXT.** Source APIs return JSON strings for numbers; `db.py`
never casts. Any numeric query must `CAST(NULLIF(col, '') AS REAL)` (empty string, not
NULL, is how "no value" shows up) — see the `NUM`/`PCT_CHANGE` SQL fragments in
`industry_report.py` / `concept_report.py` for the pattern.

**Two API response shapes, one client.** `twse_client.fetch_json()` normalizes both: bare
JSON arrays (`openapi.twse.com.tw` endpoints) and the legacy `www.twse.com.tw` shape
(`{"stat", "fields", "data"}`, where `stat != "OK"` means no data for that date/query yet —
a holiday or not-yet-published, not an error, and returns `[]` rather than raising).

**Legacy-endpoint numbers have thousands-separator commas.** The T86 institutional-investors
endpoint (`fetch_institutional_investors`) returns e.g. `"6,070,992"`; SQLite's
`CAST(... AS REAL)` silently truncates at the comma to `6.0`. `twse_client.py` strips commas
at ingestion time. If a new legacy (`www.twse.com.tw`) endpoint is ever added, check for this
before trusting any aggregate built on it.

**ROC-calendar dates.** TWSE embeds dates as ROC-calendar strings (e.g. `"1150924"` =
2026-09-24) in datasets that carry their own `Date` field; `utils.roc_to_iso()` converts
these. Datasets with no embedded date (`margin_trading`, `institutional_investors`) get
stamped with whatever `--date` was passed to `fetch_daily.py` instead — check
`datasets.py`'s `date_field`/`date_is_roc` per dataset before assuming a table's `iso_date`
means what you think.

**Publish-time lag + holidays.** `stock_price`/`stock_valuation` (STOCK_DAY_ALL/BWIBBU_ALL)
publish later in the evening than `margin_trading`/`institutional_investors` for the same
trading day — don't assume "today" has all tables populated. The market can also be closed
for several consecutive days (TWSE's `holidaySchedule` endpoint has the calendar); check
before assuming the most recent calendar weekday has trading data.

**TWSE and TPEx merge into the same tables under different field names.** TPEx uses
different field names than TWSE for equivalent data (e.g. TWSE's price dataset has
`Code`/`ClosingPrice`, TPEx's has `SecuritiesCompanyCode`/`Close`). Every TPEx entry in
`datasets.py` carries a `remap` = `(mapping_dict, mode)` applied in `fetch_daily.py` before
the date/upsert logic:
- `mode="full"` (`twse_client.remap_full`) — build a clean record using **only** the
  mapped fields. Required for `stock_price`, `stock_valuation`, `margin_trading`,
  `institutional_investors`, since `stock_lookup.py`/`industry_report.py`/
  `concept_report.py` hardcode those tables' column names and must not care which market
  a row came from.
- `mode="partial"` (`twse_client.remap_partial`) — rename only the mapped (key) fields,
  leave the rest as-is. Used for `company_info` and the income/balance-statement tables,
  where extra source-specific columns are harmless.
- `db._ensure_table` evolves existing tables with `ALTER TABLE ADD COLUMN` for any new key
  a batch introduces, rather than only creating the table once — required because TWSE and
  TPEx rows landing in the same table rarely have byte-identical column sets even after
  remapping.
- TPEx doesn't split dealer buy/sell into self-trading vs. hedging like TWSE does;
  `fetch_daily.py` fills the overall `自營商買賣超股數` column from the self-trading figure
  as the closest available proxy for TPEx rows, and the hedging-specific columns are left
  empty for them (absent data, not wrong data).
- TWSE's institutional-investor data comes from a legacy endpoint requiring an explicit
  `date` query param (`twse_client.fetch_institutional_investors`, special-cased in
  `fetch_daily.py`); TPEx's equivalent (`tpex_3insti_daily_trading`) is a normal
  latest-snapshot endpoint with an embedded `Date` field and goes through the standard
  `fetch_dataset()` path like everything else — don't assume the two markets' institutional
  data is fetched the same way if you touch this.
- `dividend` is TWSE-only — TPEx's equivalent (`mopsfin_t187ap39_O`) has a different enough
  column set that it wasn't worth reconciling; add it the same `datasets.py` way if needed.

**`industry_map` is a view, not a table.** Official 產業別 lives in `company_info` only as
a numeric code (not human-readable). `db.ensure_views()` (called at the end of every
`fetch_daily.py` run) rebuilds `industry_map` from `monthly_revenue`'s human-readable 產業別,
taking each company's latest reporting month. Query this view for official-industry grouping,
never `company_info."產業別"` directly.

**`concept_map` is hand-curated, not derived.** No free official or scraped source of
theme/concept classification exists for TWSE (FinMind has one — `TaiwanStockIndustryChain`
— but it's paid Sponsor-tier). `concept_map` (concept, stock_id, note, added_date) is built
manually via `concept_map.py add`, one theme at a time, only once a news analysis has
verified the business relationship — it starts empty and only grows through vetted use, by
design. **A stock can and normally does belong to multiple concepts** (e.g. 力積電 is both
記憶體 and 晶圓代工) — the schema's primary key is `(concept, stock_id)`, not `stock_id`
alone, specifically to allow this.

**`stock_lookup.py` is the shared validation/enrichment layer** underneath `--codes`,
`--names`, `--industry`, and `--concept`. All four resolve to a set of stock codes and go
through the same `LOOKUP_SQL` (joins `company_info` + `industry_map` + today's
`stock_price`/`stock_valuation`/`institutional_investors`/`margin_trading`) before being
sorted by `|三大法人買賣超|` descending. Two things to preserve if you touch this file:
- `--names` matching is two-tier: short name (`公司簡稱`) first, and only falls back to
  substring-matching the full legal name (`公司名稱`) — flagged `[weak match]` — because
  full-name substring overlap produces real false positives between unrelated companies
  (e.g. "李長榮科技" contains "長榮" but has no relation to the Evergreen/長榮 shipping group).
- Candidates are deduped through a `code -> set(sources)` dict before lookup, not a flat
  list, so a stock matching multiple `--industry`/`--concept` args is shown once (with a
  `matched_via` column) instead of once per matching source.

**Shioaji (永豐金) is query-only, intentionally.** `realtime_quote.py` adds intraday price
on top of `twstock.db`'s end-of-day data, reusing `stock_lookup.py`'s `resolve_names`/
`codes_by_industry`/`codes_by_concept` directly (imported, not duplicated) so the same
`--codes`/`--names`/`--industry`/`--concept` selectors work identically across both scripts.
No order-placement code (`place_order` etc.) exists anywhere in this repo — that was an
explicit scope decision (real-money risk, needs risk controls not yet designed), not an
oversight. Don't add trading/execution code without the user explicitly asking for it and
discussing risk controls first. Credentials load via `utils.load_env()` (a minimal
hand-rolled `.env` reader, no `python-dotenv` dependency) from `.env` (gitignored,
`.env.example` documents the two required keys); `simulation=True` is the default in both
`shioaji_test.py` and `realtime_quote.py` — note that Shioaji's simulation mode still
returns real market quotes, only order execution is simulated, so it's safe for all
query-only use.

**`market_movers.py` filters out warrants/ETFs via `INNER JOIN company_info`, not just
`industry_map`.** `stock_price` contains every security TWSE/TPEx reports, including
warrants — derivatives whose %change is leveraged and routinely swings ±100%+, which
completely dominates a naive "biggest %change" ranking (confirmed: the unfiltered query
returned 20/20 warrants). `company_info` only has actual companies, so inner-joining it
is the filter. Shioaji's `scanners(ChangePercentRank)` was tried as an alternative and
rejected — its results skewed toward thin-volume stocks clustered at exactly ±10%, not a
clean ranking; our own EOD data is more reliable for this.

**`trading/` is a deliberately isolated module - a real (user-specified, not
Claude-invented) swing strategy with a hard simulation-only gate.** See
`trading/README.md` for the full rule set; the one fact that must never
regress: `trading/executor.py`'s order functions only ever reach
`simulation=False` when BOTH `--live` is passed AND the environment variable
named in `trading/config.py` (`LIVE_CONFIRM_ENV_VAR`) equals its exact
confirmation string - never loosen this to a single condition. Trading needs
the `SHIOAJI_API_KEY_all`/`SHIOAJI_SECRET_KEY_all` key pair (full permission),
not the data-only pair `realtime_quote.py`/`build_dashboard_live.py` use -
`place_order` rejects the data-only key with a 401 even in simulation. A
fresh Shioaji account also needs one-time CA/trading-agreement activation
(`activate_ca()` or done on the broker's own platform) before any order
succeeds - a `406 Please sign <account> first` means that step is still
outstanding on the account, not a code bug; never attempt to handle a
user's CA certificate or password yourself. `ma_data.py` resamples
Shioaji's minute-bar `kbars()` to daily closes itself - there is no native
daily-OHLC mode for stocks in this API.

**`trading/daytrade/` is a second, independent strategy (ORB day-trade) - own config, own
position pool, own minute-K-driven data layer, but it reuses `trading/executor.py` and
`trading/positions.py` directly rather than duplicating them.** Because it's meant to
re-run every 1 minute during the session, every signal input comes from `api.kbars()` for
TODAY only (`trading/daytrade/minute_data.py`) - opening-range high, minute-level volume
ratio, cumulative turnover - never from `api.snapshots()`, whose `volume_ratio`/
`total_amount` are day-over-day figures, not minute-level ones; recomputing fresh from
today's bars every run also means there's no cross-run state file to maintain for the
opening range itself (unlike `positions_state.json`, which must persist since positions
genuinely span runs). `trading/daytrade/backtest.py` replays the exact same
`minute_data.py`/`signals.py` functions against historical kbars (growing per-minute slice,
no look-ahead) instead of duplicating the entry/exit logic. Candidates come from
`trading/daytrade/watchlist.py` - the whole market's (not `concept_map`'s) prior-day
liquidity+volatility, scored and cached once pre-market by `build_watchlist.py` (scanning
~2000 stocks takes ~10-12 min, far too slow for the 1-minute `daytrade_monitor.py` cadence)
- a 2026-10 backtest traced most losing trades back to thin, low-volatility `concept_map`
stocks that cleared the ORB conditions on noise, which is why the universe isn't
`concept_map` here like the swing strategy. `backtest.py` rebuilds each simulated day's
watchlist from only STRICTLY EARLIER bars (no look-ahead at the stock-selection level,
not just the minute-by-minute replay level). See `trading/daytrade/README.md` for the
full rule set.

**Day-trade supports both long and short (`--signal reversal_both`) via 資券當沖, not just
long.** `trading/executor.py`'s `short_sell()`/`cover_buy()` open with `StockOrderCond.
ShortSelling` and close with `StockOrderCond.MarginTrading` - the user-specified mechanism
(open via 融券, close via 融資), not something to "simplify" back to plain Cash orders.
`trading/positions.py`'s `open_position()`/`close_position()` and `trading/daytrade/
signals.py`'s `check_exit()` both take a `side` ("long"/"short") that flips which price
direction is a win - never assume `unrealized_return`/`realized_return` means "price went
up" without checking `side` first. A short needs a separate 信用交易帳戶 (margin account)
from 現股當沖's - another broker-side approval this code can't satisfy. `trading/daytrade/
patterns.py`'s `is_shooting_star()`/`is_bearish_engulfing()` are the bearish mirrors of the
long-side hammer/engulfing patterns.

**`trading/daytrade/news_api.py` is the programmatic (API-based) news-selection track**,
confirmed against cnyes (鉅亨網)'s free, keyless `tw_stock_news` category endpoint - it
genuinely supports historical `startAt`/`endAt` date-range queries and per-article stock-
code tags (unlike TWSE's own `/opendata/t187ap04_L` material-announcements endpoint, which
only ever returns the current day's batch, no date param - confirmed by direct testing,
2026-10). Ranks candidates by raw mention-count in the window, which skews toward mega-caps
(2330 etc.) that get name-dropped in nearly every market-commentary piece regardless of a
real same-day catalyst - a known bias, not yet corrected. `news_watchlist.py` is the parallel
manual-WebSearch track (today, a Claude-session-driven step); both write the same cache
shape so `daytrade_monitor.py --source news` doesn't care which track populated it.

**`--source ai_chain` (`trading/daytrade/ai_chain_watchlist.py`) trades at most ONE stock
per day, not a list.** Pipeline: `concept_map`'s `AI伺服器` members (9 stocks, not expanded
to adjacent AI-chain themes like ABF/PCB/封測) -> filter to yesterday's trading value >=
`config.AI_CHAIN_MIN_VOLUME_NTD` -> among survivors, whichever has the most cnyes
`tw_stock_news` mentions in `news_api.trading_day_window()` wins; falls back to highest
volume if nothing shows up in the news at all. Cheap enough (9 stocks + 1 news call) that
`daytrade_monitor.py` computes it inline on a cache miss rather than needing a separate
pre-market script like `build_watchlist.py`.

**`trading/decisions.json` is the strategy rulebook's single source of truth, rendered by
the dashboard's 交易決策 tab - not just the swing strategy's.** `{"strategies": {id:
{name, status, schedule, decided_date}}, "decisions": [{strategy, category, title, rule,
rationale, decided_date}]}`; `category` is one of `entry`/`exit`/`sizing`/`ranking`. Both
`build_dashboard.py` and `build_dashboard_live.py` load it via their own `load_decisions()`
and embed it as `data["decisions"]` alongside the bubble-chart data; `dashboard.html.
template`'s JS renders it read-only (no write-back to the file from the browser). Any new
trading-strategy decision (a new condition, a changed threshold, a new strategy like the
`daytrade` one) should be appended here, not just left in conversation history or
`trading/README.md` prose - this file is what makes past decisions (and why they were made)
visible on the dashboard instead of needing to be re-derived from chat/git history.

## File structure

- `fetch_daily.py` — daily orchestrator; also calls `db.ensure_views()`
- `datasets.py` — endpoint registry (url, table, date handling, key columns, TPEx
  field-remap maps) for both markets, except the T86 special case which lives directly in
  `twse_client.py`
- `twse_client.py` — HTTP layer: response-shape normalization, comma-stripping, T86 fetch,
  TPEx field remap helpers (`remap_full`/`remap_partial`)
- `db.py` — generic upsert (dynamic TEXT-typed table creation) + `industry_map` view
- `utils.py` — ROC-date conversion, date helpers
- `industry_report.py` — 4 reports grouped by official `industry_map`
- `concept_report.py` — ranks `concept_map` themes by average %change (computed from raw
  point `Change`, since TWSE doesn't provide percentage directly)
- `stock_lookup.py` — shared lookup/validation/enrichment (see Architecture above)
- `concept_map.py` — CRUD for the hand-curated `concept_map` table
- `shioaji_test.py` — first-contact Shioaji connectivity test, dumps raw responses
- `realtime_quote.py` — intraday quotes via Shioaji; query-only (see Architecture above)
- `market_movers.py` — whole-market %change ranking from twstock.db (not limited to
  concept_map's tracked stocks) with industry/concept classification, plus a day-over-day
  industry-representation trend view
- `dashboard.html.template` — bubble-chart dashboard shell + 交易決策 (decisions log) +
  均線總覽 (MA5/10/20 table) tabs; `__DATA_JSON__` is the data injection point, never edit
  the built `dashboard.html` directly
- `build_dashboard.py` / `build_dashboard_live.py` — fill the template from twstock.db
  (EOD) or Shioaji (intraday) respectively, both producing `dashboard.html` (gitignored
  build output). `build_dashboard_live.py` also writes `dashboard_push.json` (gitignored)
  - the raw data dict, for the Artifact `db`-push path (see below).
- `build_ma_cache.py` — pre-computes MA5/10/20 for every `concept_map` stock into
  `ma_cache.json` (gitignored), for the 均線總覽 tab. Runs on its own, slower, once-a-day
  schedule (not every dashboard refresh) because MA needs Shioaji's per-stock `kbars()` -
  not batchable like `snapshots()` - which takes a few minutes across ~180 stocks, far too
  slow for the live dashboard's 1-minute cadence. `build_dashboard_live.py` just reads
  whatever's in the cache and overlays the current run's live price; `build_dashboard.py`
  (EOD, no live price available) always ships an empty 均線總覽 table.
- `refresh_dashboard.bat` — entry point for the `TWDashboardLiveRefresh` Windows
  scheduled task (every 1 min); cds into the project dir and runs
  `build_dashboard_live.py`
- `trading/` — isolated swing-trading module (config/signals/positions/executor/
  daily_scan/intraday_monitor/report); see Architecture above and `trading/README.md`
- `volume_alert.py` — Windows toast alerts on Shioaji's `volume_ratio` (量比), called from
  `build_dashboard_live.py` every run. Once-per-trading-day per stock
  (`volume_alert_state.json`, gitignored, resets on date change) and the child
  `powershell.exe` call is launched with `creationflags=subprocess.CREATE_NO_WINDOW` and
  the temp `.ps1` is written `utf-8-sig` (not `utf-8`) - both were real bugs found by
  testing (a visible console flash per alert, and garbled Chinese toast text from Windows
  PowerShell 5.1 reading a BOM-less script in the system codepage instead of UTF-8) -
  don't revert either "fix" while refactoring. **Never test this against the full
  ~2000-stock universe with a near-zero threshold** - it previously queued ~140 toasts
  in under two minutes before the run could be stopped; verify with a direct
  `volume_alert.send_toast(...)` call instead.

**Two separate auto-refresh mechanisms exist for the dashboard - don't conflate them.**

1. **Pure-local (currently active):** Windows Task Scheduler job `TWDashboardLiveRefresh`
   runs `refresh_dashboard.bat` -> `build_dashboard_live.py` every minute, rewriting
   `dashboard.html` on disk. The template's own JS (`RELOAD_SECONDS`, a `setInterval`
   countdown ending in `location.reload()`) reloads the open browser tab in step, so the
   viewer sees fresh data with **zero Claude session involvement** once set up - this is
   the user's explicit choice after asking whether local-machine resources could replace
   the agent-driven loop. The tradeoff they accepted: viewing is local-file-only (no
   claude.ai link to share/view elsewhere). Check/stop it with
   `Get-ScheduledTaskInfo -TaskName "TWDashboardLiveRefresh"` /
   `Unregister-ScheduledTask -TaskName "TWDashboardLiveRefresh"`.
2. **Artifact `db` capability (built, available, not the active mechanism):** the
   published Artifact declares `capabilities: {db: {}}` and its page subscribes to
   `dashboard/latest`'s `onSnapshot`, re-rendering on push with no reload. But a browser
   can't reach Shioaji directly (CSP; Shioaji isn't a browser protocol anyway), and
   `write_db` is only callable through the Artifact tool - so keeping this path "live"
   requires a Claude-session-side loop (`ScheduleWakeup`, 60s floor) that runs until
   explicitly stopped. The user tried this first, asked about the resource cost, then
   asked whether local resources could do it instead - hence mechanism 1. Don't re-offer
   the session loop as the default; it's the fallback for when a shareable/cross-device
   link matters more than zero Claude usage.

`build_dashboard_live.py` always writes both `dashboard.html` (full page, for mechanism 1
or a one-off republish) and `dashboard_push.json` (the bare data dict, for mechanism 2's
`write_db` push) - one run serves either path.
