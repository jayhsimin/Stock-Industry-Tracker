# 趨勢線通道均值回歸策略（正式上線，模擬單）

跟波段（`trading/`）、當沖（`trading/daytrade/`）、MA支撐/破位（`trading/ma_touch/`）完全隔離的
**第四個**獨立模組。2026-10-14從`draft`提升為正式排程，**目前只能下模擬單**。

## 策略概念（使用者指定）

在價格圖上找相對高低點（擺動點），把相鄰的swing high連成一條上緣趨勢線、swing low連成一條下緣趨勢線
（不是水平的滾動最高/最低，是真正有斜率的2點直線，延伸到今天）。收盤價**跌破下緣**→買進（賭反彈
回到通道內）；收盤價**突破上緣**→賣出。跟一般的突破順勢策略方向相反，是均值回歸。

## 規則

**擺動點判定**：bar i的High若是`[i-3, i+3]`窗口內最大值即為swing high（Low同理為swing low）；一個
擺動點要等3天後才算「確認」（避免look-ahead——compute_channel()的核心邏輯，見`trading/
channel_backtest.py`）。

**進場**：收盤價 < 下緣趨勢線（當天延伸值）、且當日成交金額 ≥ NT$1億。

**出場（任一觸發）**：
- 突破上緣（策略自己的出場機制）
- 停損：未實現報酬 ≤ -8%
- 持有超過10天

## 驗證記錄（詳見`trading/decisions.json`）

這是目前這個專案驗證最紮實的策略：
- 180天：總損益+NT$13,556，train(+16,510)/test(+23,115)切分後兩段都正
- **300天**（Shioaji歷史資料深度擴大後重測）：106筆交易、勝率58.5%、+NT$46,263；**4段獨立驗證
  （每段從零部位重跑）全部4段都正**（+93,694/+18,062/+16,497/+1,326）——全對話測過最一致的結果
- 加熔斷機制（連續虧損暫停進場）：實測反而拖累績效，**已否決**，不採用
- 改成分鐘級當沖版（`trading/daytrade/channel_backtest.py`）：**明確失敗**（-NT$321,018），優勢
  只存在日線尺度

## 資金/風險隔離

- 自己的資金池：`config.MAX_TOTAL_POSITION_COST_NTD = NT$200,000`
- 自己的持倉檔：`trading/channel/positions_state.json`（gitignore）
- 下單走`trading/executor.py`同一套兩段式防呆閘門
- 核心通道計算（`compute_channel()`）直接`import`自`trading/channel_backtest.py`，不重新實作
  ——跟回測/交叉驗證用的是同一份程式碼，保證正式規則跟驗證過的規則不會drift

## 排程

`TWTradingChannelScan`：平日14:35自動執行`python -m trading.channel.scan`（跟波段策略的
`TWTradingDailyScan`14:30錯開5分鐘，避免同時登入Shioaji衝突）。

## 手動測試

```bash
python -m trading.channel.scan                      # 模擬單進出場（預設，安全）
python -m trading.channel_backtest --days 180        # 正式規則回測
python -m trading.channel_cv --days 180 --multi-period  # 多段獨立驗證
```
