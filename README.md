# Stock Industry Tracker

台股資料管線 + 分析工具組：每天從證交所/櫃買中心官方免費 API 抓**全部上市(TWSE) + 上櫃(TPEx)股票**的
基本面 + 籌碼面資料存進本機 SQLite，再疊上官方產業分類、自建題材分類、全市場漲跌幅排行、盤中即時報價
（永豐金 Shioaji）、成交量異常警示、互動式泡泡圖儀表板。目標是「新聞/題材出現時，能用真實資料驗證、
而不是憑印象猜」。

**整體架構**：

```
TWSE/TPEx OpenAPI  ──┐
                     ├─> fetch_daily.py ─> twstock.db (SQLite)
Shioaji(選用,即時) ──┘         │
                                ├─ industry_map (view：官方產業分類)
                                └─ concept_map  (表：自建題材分類)

twstock.db ─> stock_lookup.py / industry_report.py / concept_report.py / market_movers.py（查詢、分析）
twstock.db ─> build_dashboard(_live).py ─> dashboard.html（泡泡圖，可發布成 Artifact）
Shioaji    ─> volume_alert.py（成交量異常 → Windows 通知）
```

資料來源全部免費：TWSE/TPEx 官方 API（`openapi.twse.com.tw` + `www.tpex.org.tw/openapi`）**不需要任何
API Key**，也不用一檔一檔股票去打 — 每個資料集一次呼叫就拿到全市場資料；Shioaji（盤中即時報價/成交量
警示用）需要永豐金證券帳戶才能用，是選用功能，沒有也不影響上面的每日資料管線。

TWSE 跟 TPEx 對同一種資料用的欄位名稱不一樣（例如股價，TWSE 叫 `Code`/`ClosingPrice`，TPEx 叫
`SecuritiesCompanyCode`/`Close`），`datasets.py` 裡把 TPEx 的欄位改名對齊 TWSE 的欄位，兩個市場的資料
合併存進同一批表（`stock_price`、`institutional_investors`...），所以 `stock_lookup.py`／
`industry_report.py`／`concept_report.py` 完全不用管股票是上市還是上櫃，查詢邏輯共用。

## 安裝

```bash
pip install -r requirements.txt
```

## 執行

```bash
python fetch_daily.py                    # 抓「今天」的資料
python fetch_daily.py --date 2026-09-24   # 抓指定日期（用來補特定一天的資料）
```

每次執行都是 upsert（存在就更新、不存在就新增），可以放心重複執行不會產生重複資料。

**限制：這些 TWSE/TPEx 端點只回傳「最新一天」的全市場快照，沒有歷史區間查詢參數**，
所以歷史資料是靠每天執行累積出來的，無法一次補齊過去好幾年的資料。
如果之後需要一次性補歷史資料，可以另外接 FinMind（`https://finmind.github.io/`），
它有台股歷史資料，免費帳號可查歷史區間，但全市場一次撈需要付費 Backer 方案，免費版要一檔一檔股票查。

## 排程（每天自動跑）

Windows 工作排程器範例（收盤三大法人資料約晚上 8 點後才會更新，建議排在 21:00 之後跑）：

```
schtasks /create /tn "TWStockDaily" /tr "python C:\Users\z2012\Desktop\S_N\fetch_daily.py" /sc daily /st 21:30
```

## 資料表

| 資料表 | 內容 | 更新頻率 |
|---|---|---|
| `stock_price` | 個股日成交資訊（開高低收、成交量額），上市+上櫃合併 | 每日 |
| `stock_valuation` | 個股日本益比、殖利率、股價淨值比，上市+上櫃合併 | 每日 |
| `margin_trading` | 個股融資融券餘額，上市+上櫃合併 | 每日 |
| `institutional_investors` | 三大法人（外資、投信、自營商）買賣超，上市+上櫃合併 | 每日 |
| `company_info` | 公司基本資料（產業別、負責人、上市/上櫃日期等），上市+上櫃合併 | 不常變 |
| `monthly_revenue` | 每月營收彙總表，上市+上櫃合併 | 每月 |
| `dividend` | 股利分派情形（目前只有上市，上櫃的欄位格式不同還沒接）| 不定期公告 |
| `income_statement_{ci,fh,bd,ins,mim,basi}` | 綜合損益表，依產業（一般業/金控/證期/保險/異業/金融）分表，上市+上櫃合併 | 每季 |
| `balance_sheet_{ci,fh,bd,ins,mim,basi}` | 資產負債表，依產業分表，上市+上櫃合併 | 每季 |

欄位名稱直接沿用證交所原始欄位（中文），存成 TEXT，方便日後知道欄位確切意義；做數值運算時用
`CAST(欄位 AS REAL)` 或 `CAST(欄位 AS INTEGER)` 轉型。

## 依產業分類

`company_info` 裡的「產業別」只是數字代碼，沒有可讀名稱；`monthly_revenue`（月營收表）裡的「產業別」則是
可讀的中文分類（如「半導體業」「電子零組件業」），且每家有營收公告的上市公司都有。`fetch_daily.py`
每次執行後會重建一個 `industry_map` view（依 `公司代號` 取該公司最新一期的產業別），可以直接 join 使用：

```sql
SELECT sp.Code, sp.Name, im."產業別",
       CAST(sp.ClosingPrice AS REAL) AS close
FROM stock_price sp
LEFT JOIN industry_map im ON sp.Code = im."公司代號"
WHERE sp.iso_date = '2026-09-24'
ORDER BY im."產業別", sp.Code;
```

ETF、TDR、存託憑證等沒有月營收公告的標的，`產業別` 會是 NULL（這些本來就不是「個股」）。

### 現成的產業分析報表

```bash
python industry_report.py                    # 今天
python industry_report.py --date 2026-09-24
```

會印出四張表：各產業當日漲跌家數/平均漲跌/成交金額、各產業三大法人買賣超合計、
各產業平均 PER/殖利率/PBR、各產業融資餘額變化。都是依 `industry_map` group by 產業別算出來的。

## 分析範例

```python
import sqlite3
import pandas as pd

conn = sqlite3.connect("twstock.db")
df = pd.read_sql("""
    SELECT Code, Name, iso_date,
           CAST(ClosingPrice AS REAL) AS close,
           CAST(TradeVolume AS INTEGER) AS volume
    FROM stock_price
    WHERE Code = '2330'
    ORDER BY iso_date
""", conn)
```

## 題材/概念股分類（自建，逐步累積）

TWSE/MOPS 官方只有正式產業別（前面的 `industry_map`），沒有「AI伺服器」「航運三雄」這種市場題材分類；
FinMind 有對應資料集（`TaiwanStockIndustryChain`）但要付費 Sponsor 方案才能用。所以改成自己建：
每次新聞分析驗證過某個題材真的對應哪些個股（依實際業務關聯，不是瞎猜），就存進 `concept_map` 表，
越用越完整，而且是我們自己驗證過的，不是爬別人網站的過期資料。

```bash
python concept_map.py add --concept "AI伺服器" --codes 2382 3231 2317 6669 --note "AI伺服器組裝/代工供應鏈"
python concept_map.py show --concept "AI伺服器"
python concept_map.py list
python concept_map.py remove --concept "AI伺服器" --codes 2382

python stock_lookup.py --concept "AI伺服器"     # 跟 --industry 用法一樣，抓當日動能
```

`add` 會先檢查代號是否存在於 `company_info`，不存在的直接跳過不會存進去。

## 新聞題材 → 概念股篩選（人工在對話中執行）

流程：把新聞貼給 Claude Code → Claude 依知識判斷可能的題材與候選個股（代號或公司名稱）→
用 `stock_lookup.py` 驗證這些股票真的存在，並抓出當天實際動能（漲跌、三大法人買賣超、融資變化、估值）→
依動能佐證判斷這個題材是不是真的在發動、該留意哪幾檔。

```bash
python stock_lookup.py --codes 2603 2609 2615          # 用代號查
python stock_lookup.py --names "長榮" "陽明" "萬海"       # 用公司名稱查（模糊比對）
python stock_lookup.py --codes 2603 --date 2026-09-24   # 查指定日期
```

輸出會標出：
- `[not found]`：代號/名稱在 `company_info` 裡找不到（可能是幻覺、代號打錯、或已下市）
- `[weak match]`：名稱只比對到公司全名（非市場慣用簡稱），例如查「長榮」也會弱比對到「李長榮科技(4989)」
  「長榮國際儲運(2607)」— 這兩家跟長榮海運集團沒有實質關係，純粹全名字面重疊，要人工二次確認再採用

## 全市場漲跌幅排行 + 產業/題材發酵追蹤

`market_movers.py` 從 `twstock.db` 自己算全市場（上市+上櫃）漲跌幅排行，不只看我們已經分類過的股票，
而是看「今天漲跌幅最大的到底是誰」，再反查這些股票的產業/題材，找出「現在是什麼在發酵」：

```bash
python market_movers.py --date 2026-09-29 --top 30              # 當天漲跌幅前30名 + 產業分布
python market_movers.py --days 5 --top 30                       # 近N個交易日，產業在排行榜裡的出現次數趨勢
```

**重要**：`--date` 用 `company_info` 過濾掉權證/ETF（這類衍生商品漲跌幅動輒 ±100%，會洗版真正的個股排行，
一開始沒過濾時前20名全部都是權證，加了 `INNER JOIN company_info` 才修正）。另外本來想用 Shioaji 的
`scanners()`（內建漲跌幅排行榜）來做，但實測結果偏向極低成交量的股票卡在剛好±10%，資料不乾淨，
改用我們自己完整的 TWSE/TPEx 收盤資料更可靠。

**`--days` 的趨勢追蹤目前參考價值有限**，因為 `twstock.db` 還只累積了兩天資料（還沒設排程每天自動跑）——
要看出「某題材連續好幾天在排行榜出現次數增加=發酵」「出現次數減少=退燒」這種趨勢，需要每天執行
`fetch_daily.py` 累積歷史，建議現在就排程起來（見下面「排程」那節）。

## 盤中即時報價（永豐金 Shioaji）

`twstock.db` 裡的價格/籌碼資料都是**收盤後才發布**的，盤中沒有即時性。如果有永豐金證券帳戶，
可以用 Shioaji API 補上盤中即時報價，跟 `--codes`/`--names`/`--industry`/`--concept` 選股邏輯共用：

```bash
pip install shioaji
copy .env.example .env   # 填入 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY，.env 已在 .gitignore 不會被提交

python shioaji_test.py --codes 2330              # 先打一次看原始資料長什麼樣子
python realtime_quote.py --concept "AI伺服器"      # 盤中即時報價，依漲跌幅排序
python realtime_quote.py --industry "半導體業" --live   # --live 用真帳戶，預設是模擬模式（報價一樣是真的，只有下單是模擬）
```

## 泡泡圖儀表板（題材雷達）

`dashboard.html.template` + `build_dashboard.py`/`build_dashboard_live.py` 產生一個可互動的泡泡圖
Artifact：泡泡大小 = 漲跌幅度，紅漲綠跌，可切「官方產業」/「自建題材」，點泡泡看該分類完整個股清單。

```bash
python build_dashboard.py                  # 用 twstock.db 的收盤資料（每日快照）
python build_dashboard_live.py             # 用 Shioaji 盤中即時報價（分組仍然來自資料庫，只有價格換成即時）
```

兩者都輸出 `dashboard.html`（已加進 `.gitignore`，是產生物不是原始碼），把這個檔案發布成 Artifact 即可。
`dashboard.html.template` 裡的 `__DATA_JSON__` 是資料注入點，不要直接編輯 `dashboard.html`——改
`.template` 再重新產生。

### 自動刷新 — 兩種做法

**A. 純本機方案（目前在用，零 Claude 用量）**

Windows 工作排程器每分鐘執行一次 `refresh_dashboard.bat`（呼叫 `build_dashboard_live.py`），
重新產生本機的 `dashboard.html`；頁面本身內建一個倒數計時器（`RELOAD_SECONDS`，預設60秒），
時間到就 `location.reload()`，瀏覽器重新讀取磁碟上最新的檔案——全程不需要 Claude session 介入，
排程設定好之後會一直跑到你手動停止為止。

```powershell
# 查看排程狀態
Get-ScheduledTaskInfo -TaskName "TWDashboardLiveRefresh"

# 暫停/停止
Disable-ScheduledTask -TaskName "TWDashboardLiveRefresh"
Unregister-ScheduledTask -TaskName "TWDashboardLiveRefresh" -Confirm:$false   # 完全刪除
```

直接在瀏覽器開啟本機的 `C:\Users\z2012\Desktop\S_N\dashboard.html` 即可看到每分鐘自動刷新的畫面。
缺點：只能在這台電腦上看，沒有 claude.ai 連結可以分享或跨裝置查看。

**B. Artifact + db 即時推播（有雲端連結，但需要 Claude session 持續跑）**

Artifact 宣告了 `db` capability，頁面載入後會訂閱 `dashboard/latest` 這份共享文件的即時推播
(`onSnapshot`)——只要那份文件被更新，所有已經打開的分頁會自動重繪泡泡圖，不用重新整理。
但「抓即時報價」跟「推送」這兩步都需要透過 Claude（瀏覽器連不到 Shioaji，且 `write_db` 只能
透過 Claude Code 工具呼叫），所以要自動化的話只能靠 Claude session 內的排程迴圈（`ScheduleWakeup`
最短間隔60秒），持續消耗 session 資源直到手動停止。日常手動刷新流程：

```bash
python build_dashboard_live.py   # 這會順便產生 dashboard_push.json
```
然後請 Claude 把 `dashboard_push.json` 的內容用 `write_db`（collection `dashboard`、doc_id `latest`）
寫進去即可，已開啟的分頁會立刻跳變，不需要重新 publish `dashboard.html`
（只有樣板/程式邏輯改變時才需要重新 publish）。

**目前只做到「查詢」，沒有下單功能**——`place_order` 等交易相關 API 完全沒有接，這是刻意的：
自動下單涉及真錢風險，需要額外設計風控（部位大小、停損、單日虧損上限）才能碰，不是現階段範圍。
三大法人買賣超、融資融券這些籌碼資料 Shioaji 不提供，仍然要用 `twstock.db`（TWSE/TPEx 收盤後資料）。

## 成交量異常警示（Windows 通知）

`volume_alert.py` 每次 `build_dashboard_live.py` 執行（排程每分鐘一次）都會檢查 Shioaji 快照的
`volume_ratio`（量比：今日每分鐘均量 ÷ 過去5日每分鐘均量），超過門檻就跳出 Windows 原生通知，不需要
額外安裝套件：

```bash
python build_dashboard_live.py --volume-ratio-alert 3.0 --min-price-alert 100   # 預設3倍/股價>100，量比門檻0=停用
```

同一檔股票**一天只會通知一次**（記錄在 `volume_alert_state.json`，跨日自動重置），避免量一直維持在高檔
時每分鐘都跳通知。通知用的是借用 PowerShell 自己註冊好的 App ID 發送（自訂 App ID 在未註冊時會被
Windows 悄悄擋掉不顯示），且會強制不開子視窗（`CREATE_NO_WINDOW`），執行時不會閃出任何視窗。

⚠️ **測試這個功能時門檻千萬不要設太低**——全市場有上千檔股票在檢查，門檻設到連正常量都會觸發的話，
會在短時間內對大量股票各發一次通知，瀏覽器/桌面會被洗版。要測試就先用 `python -c` 直接呼叫
`volume_alert.send_toast(...)` 測單一筆通知，不要直接拿全市場跑低門檻。

## 檔案結構

- `fetch_daily.py` — 主程式，執行每日抓取
- `datasets.py` — 各資料集的 API 端點與主鍵設定
- `twse_client.py` — 呼叫證交所/櫃買 API 的共用函式 + TPEx 欄位改名工具(`remap_full`/`remap_partial`)
- `db.py` — SQLite 通用 upsert 邏輯（含自動 `ALTER TABLE` 新增欄位，讓上市/上櫃資料能合併進同一張表）+ `industry_map` view
- `utils.py` — 民國年轉西元年等小工具
- `industry_report.py` — 依產業分類的四張現成報表
- `stock_lookup.py` — 驗證候選股票代號/名稱/產業/題材是否存在 + 抓當日動能
- `concept_map.py` — 自建題材/概念股對照表（管理 concept_map 表）
- `shioaji_test.py` — Shioaji API 第一次連線測試，印出原始回應內容
- `realtime_quote.py` — 盤中即時報價（Shioaji），沿用 stock_lookup.py 的選股邏輯，純查詢不下單
- `utils.load_env()` — 極簡 .env 讀取（沒有額外依賴 python-dotenv）
- `dashboard.html.template` — 泡泡圖儀表板的 HTML/CSS/JS 樣板（`__DATA_JSON__` 是資料注入點）
- `build_dashboard.py` / `build_dashboard_live.py` — 把 twstock.db（收盤）或 Shioaji（即時）資料
  灌進樣板，產生 `dashboard.html`（產生物，已 gitignore）
- `refresh_dashboard.bat` — Windows 工作排程器呼叫的進入點，cd 進專案目錄後跑
  `build_dashboard_live.py`（排程本身用 `Register-ScheduledTask` 設定，工作名稱
  `TWDashboardLiveRefresh`，每分鐘跑一次）
- `market_movers.py` — 全市場（不限已分類股票）漲跌幅排行 + 產業分布，過濾掉權證/ETF
- `volume_alert.py` — 成交量異常（量比）Windows 通知，見上面「成交量異常警示」一節

## 環境需求

- Python 3.10+（用到 `dict[str, set]` 等型別標註語法）
- Windows（`realtime_quote.py`/`volume_alert.py`/排程腳本都是 Windows 專用；核心資料管線
  `fetch_daily.py` 本身是跨平台的純 Python + SQLite，不依賴 Windows）
- 選用：永豐金證券帳戶（Shioaji 盤中即時報價/成交量警示才需要，核心每日資料管線不需要）
