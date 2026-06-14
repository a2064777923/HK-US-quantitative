# HK-US Quantitative Trading System

港股+美股量化交易系統 — 信號引擎、組合回測、模擬交易、飛書通知

## 📊 回測結果摘要

### 現實版回測（固定$1萬/筆、唔複利、2021-2026）
| 指標 | 數值 |
|------|------|
| 初始資金 | $100,000 |
| 最終資金 | $335,814 |
| 總回報 | 235.8% |
| 年化回報 | **42.4%** |
| MaxDD | **12.4%** |
| Sharpe | **1.09** |
| 交易筆數 | 1,232 |
| 勝率 | 44.2% |
| 盈虧比 | 2.4:1 |
| 期望值 | +2.11%/筆 |

### 每年表現
| 年份 | 交易 | 港股/美股 | 勝率 | P&L |
|------|------|----------|------|-----|
| 2021 | 173 | 158/15 | 46% | +$31,765 |
| 2022 | 251 | 230/21 | 41% | +$38,875 |
| 2023 | 249 | 228/21 | 42% | +$40,577 |
| 2024 | 233 | 208/25 | 45% | +$58,552 |
| 2025 | 223 | 209/14 | 43% | +$19,379 |
| 2026 | 103 | 92/11 | 52% | +$46,668 |

### 純美股組合回測（64年，1962-2026）
| 指標 | 數值 |
|------|------|
| CAGR | 19.1% |
| MaxDD | 13.5% |
| Sharpe | 1.55 |
| Calmar | 1.41 |

## 🏗️ 系統架構

```
├── backtest/
│   ├── backtest_trades.py          # 單股回測
│   ├── segment_backtest.py         # 分段回測（熊市/震盪/牛市）
│   ├── portfolio_backtest_combined.py  # 港股+美股組合回測（複利版）
│   └── portfolio_backtest_realistic.py # 現實版回測（固定倉位）
├── scripts/
│   ├── signal_engine_v4.py         # 信號引擎v4（RSI/MACD/ATR/布林/動量）
│   ├── kline_batch.py              # K線批量更新（騰訊API）
│   ├── generate_signals.py         # 信號生成
│   ├── quantmind_strategy_runner.py # 策略執行器
│   ├── quantmind_sim_trader.py     # 模擬交易
│   ├── feishu_notify.py            # 飛書通知
│   └── quantmind_daily_pipeline.py # 每日數據流水線
├── config/
│   ├── config.template.json        # 配置模板
│   └── crontab.txt                 # 定時任務
├── docs/
│   └── scoring_logic.md            # 評分邏輯文檔
└── results/
    ├── portfolio_bt_combined_summary.json
    ├── portfolio_bt_realistic_summary.json
    └── segment_backtest_results.json
```

## 📈 信號引擎 v4

### 評分維度（-1 到 +1）
1. **趨勢 (Trend)**: 多頭/空頭排列、均線斜率
2. **動量 (Momentum)**: RSI、MACD柱狀圖
3. **結構 (Structure)**: 布林帶位置
4. **成交量 (Volume)**: 量比、放量突破

### 買賣門檻
- **BUY**: score >= 0.65
- **SELL**: score <= 0.35
- **止損**: Chandelier Trailing Stop (ATR × 2)

### 動態倉位管理
- 單股倉位: 3%-15%（按信號強度調整）
- 波動率調整: ATR越高倉位越小
- 最多同時持倉: 10-16隻
- 冷卻期: 止損後3日唔再買同一隻

## 📊 數據覆蓋

| 市場 | 股票數 | 歷史數據 |
|------|--------|---------|
| 港股 | 242隻 | 2018-2026 (2000日) |
| 美股 | 32隻 | 1962-2026 (16000+日) |

### 本地回測數據

Raw K線數據只應保存在本地資料目錄，默認 `/tmp`，不要提交到 GitHub，也不要默認同步到服務器。可重跑入口：

```bash
APCA_API_KEY_ID=... APCA_API_SECRET_KEY=... \
python3 scripts/local_backtest_dataset.py --output-dir /tmp --start-date 2021-01-01 --end-date 2026-06-14
```

輸出文件：

- `/tmp/hk_klines_v2.csv` - `portfolio_backtest_realistic.py` 的港股輸入；
- `/tmp/us_klines.csv` - `portfolio_backtest_realistic.py` 的美股輸入；
- `/tmp/all_klines.csv` - `portfolio_backtest_combined.py` 的合併輸入；
- `/tmp/hk_us_dataset_metadata.json` - 來源、覆蓋、local-only 存儲策略。

US Alpaca 默認使用 `feed=iex`，適合快速基線；需要機構級全市場覆蓋時應使用已授權的 SIP/高質量 vendor feed 並保留 metadata。可選的 US 小時線/分鐘線也應落本地 raw data 目錄，例如：

```bash
APCA_API_KEY_ID=... APCA_API_SECRET_KEY=... \
python3 scripts/local_backtest_dataset.py --output-dir /tmp --us-intraday-timeframe 1Hour
```

把本地數據和回測結果整理成可靠性報告：

```bash
python3 scripts/local_backtest_reliability_report.py \
  --metadata-file /tmp/hk_us_dataset_metadata.json \
  --realistic-result-file /tmp/portfolio_bt_realistic.json \
  --combined-result-file /tmp/portfolio_bt_v4.json \
  --output /tmp/local_backtest_reliability_report.json --text
```

這份報告只用來支撐研究判斷，不會改 v5、Hermes、模擬倉或任何 cron。

再做一層 v5 本地 replay，只把觸發/確認/風控分布喂給 Hermes：

```bash
python3 scripts/v5_local_replay_report.py \
  --hk-csv /tmp/hk_klines_v2.csv \
  --us-csv /tmp/us_klines.csv \
  --output /tmp/v5_local_replay_report.json --text
```

這份 replay 也是研究上下文，不是 PnL 回測，不會寫 alert queue、下單、或改生產門檻。

檢查本地回測與 v5 實時引擎的因子契約是否對齊：

```bash
python3 scripts/factor_contract_alignment_report.py \
  --output /tmp/factor_contract_alignment_report.json --text
```

若報告顯示 `PARTIAL_ALIGNMENT_REQUIRES_CAUTION`，代表本地回測仍可作研究基線，但不能直接當成 v5 replay 證據。

## 🚀 部署

### 依賴
```bash
pip install -r requirements.txt
```

### 數據更新
```bash
# K線批量更新（每30分鐘）
python3 scripts/kline_batch.py

# 信號生成
python3 scripts/generate_signals.py

# 策略執行
python3 scripts/quantmind_strategy_runner.py
```

### 定時任務
參考 `config/crontab.txt`

## ⚠️ 風險提示

1. 回測結果唔代表未來表現
2. 港股數據只有8年，樣本偏短
3. 美股長期數據有幸存者偏差（只揀咗最後嘅赢家）
4. 實際交易有滑點、流動性、衝擊成本等問題
5. 建議先用模擬盤驗證至少3個月

## 📝 優化方向

1. **提高港股BUY門檻** — 港股勝率偏低（43%），可試0.70
2. **市場情緒過濾** — 大市跌時減少開倉
3. **分鐘級回測** — 用分鐘數據做更精確嘅入場/出場
4. **行業輪動** — 按行業/概念過濾信號
5. **風險平價** — 按波動率分配倉位而非固定金額
