# QuantMind v4 — 港美股量化交易系統

全自動量化交易系統，支持港股+美股，整合技術面、消息面、大市環境多維度分析。

## 架構

```
┌─────────────────────────────────────────────────────────┐
│                    QuantMind v4                          │
├─────────────┬─────────────┬──────────────┬──────────────┤
│ K線數據層    │ 信號分析層   │ 策略執行層    │ 通知層       │
│             │             │              │              │
│ kline_batch │ signal_v4   │ strategy_run │ feishu_notify│
│ (騰訊API)   │ (5大因子)    │ (自動下單)    │ (飛書推送)   │
├─────────────┴─────────────┴──────────────┴──────────────┤
│                    PostgreSQL + Redis                    │
└─────────────────────────────────────────────────────────┘
```

## 核心文件

| 文件 | 功能 | 說明 |
|------|------|------|
| `scripts/signal_engine_v4.py` | 信號引擎 v4 | 多維度分析：技術面+支撐阻力+掛單價+小時預測 |
| `scripts/kline_batch.py` | K線批量更新 | 騰訊財經API，批量寫入，支持港股+美股 |
| `scripts/quantmind_strategy_runner.py` | 策略執行 | 自動止損止盈+信號驅動下單+飛書通知 |
| `scripts/feishu_notify.py` | 飛書通知 | 發送交易信號和訂單通知到飛書群 |
| `scripts/expand_hk_us.py` | 股票池擴充 | 用akshare獲取港股+美股完整列表 |
| `scripts/update_portfolio_prices.py` | 價格更新 | 更新持倉現價 |
| `scripts/heartbeat_refresh.sh` | 心跳 | 保持策略狀態活躍 |
| `scripts/quantmind_sim_trader.py` | 模擬交易 | 模擬訂單執行 |
| `scripts/quantmind_daily_pipeline.py` | 每日管線 | K線更新+特徵生成+模型推理 |
| `scripts/generate_signals.py` | 信號生成(舊) | v2版，已被v4取代 |

## 信號引擎 v4 — 評分邏輯

### 5大評分因子（總分 0~1）

| 因子 | 權重 | 計算方式 |
|------|------|---------|
| **趨勢** | 30% | MA5/10/20排列 + MA20斜率 |
| **動量** | 25% | RSI(14) + MACD(12/26/9) + 5日動量 |
| **結構** | 20% | 布林帶(20,2σ)位置 + ATR波動率 |
| **量能** | 15% | 20日均量比 + 量價配合 |
| **位置** | 10% | 60日價格分位 |

### 信號判定
- **BUY**: score ≥ 0.62（且趨勢不能強空頭）
- **SELL**: score ≤ 0.38
- **HOLD**: 0.38 < score < 0.62

### 硬規則（安全網）
- 強空頭（趨勢 ≤ -0.6）→ 強制壓分到 0.45 以下，禁止 BUY
- MACD + RSI 雙重確認：MACD<0 且 RSI<40 → 壓分到 0.48 以下

### 掛單價計算
- **買入價**: 第一支撐位，或現價回調 0.5×ATR（最多等3%回調）
- **止損價**: 第二支撐位，或入場價 - 2×ATR（最多虧8%）
- **止盈價**: 第一阻力位，或入場價 + 3×ATR
- **風險回報比**: reward/risk，< 1.5 標記為低質量信號

### 小時級預測
- 基於日均波動率 ÷ 6.5（交易小時數）
- 結合MA趨勢方向作為drift
- 預測未來 1-4 小時價格區間

### 支撐阻力計算
- 近20日高低點
- Classic Pivot Points
- MA5/10/20 作為動態支撐阻力
- 布林帶上下軌

## 數據源

| 數據 | 來源 | 頻率 |
|------|------|------|
| 港股K線 | 騰訊財經 proxy.finance.qq.com | 30分鐘 |
| 美股K線 | 騰訊財經 proxy.finance.qq.com | 30分鐘 |
| A股市場環境 | 悟道MCP (東方財富) | 實時 |
| 消息面 | 財联社快讯 + 全網熱榜 | 實時 |
| 基本面 | 悟道MCP (財務摘要+估值) | 日級 |

## 自動化排程 (Crontab)

```cron
# 心跳（每2分鐘）
*/2 * * * * /root/heartbeat_refresh.sh

# K線更新 + 信號生成（交易時段每30分鐘）
0,30 9-16 * * 1-5 python3 /root/kline_batch.py && python3 /root/signal_engine_v4.py

# 策略執行（交易時段每5分鐘）
*/5 9-16 * * 1-5 python3 /root/quantmind_strategy_runner.py

# 收市後全量更新
30 16 * * 1-5 python3 /root/kline_batch.py && python3 /root/signal_engine_v4.py

# 持倉價格更新（每15分鐘）
*/15 9-16 * * 1-5 python3 /root/update_portfolio_prices.py
```

## 環境要求

- Python 3.10+
- PostgreSQL (Docker: quantmind-db)
- Redis (Docker: quantmind-redis)
- akshare (股票池擴充用)
- 飛書 App (通知用)

## 部署

```bash
# 1. 上傳腳本到服務器
scp scripts/*.py root@your-server:/root/
scp scripts/*.sh root@your-server:/root/

# 2. 安裝依賴
pip install akshare --break-system-packages

# 3. 配置飛書 (修改 feishu_notify.py 中的 APP_ID/SECRET/CHAT_ID)

# 4. 設置 crontab
crontab crontab.txt

# 5. 首次運行：擴充股票池 + 更新K線
python3 /root/expand_hk_us.py
python3 /root/kline_batch.py
python3 /root/signal_engine_v4.py
```

## 策略參數

```python
MAX_POSITIONS = 10          # 最大持倉數
POSITION_SIZE_PCT = 0.10    # 每倉佔總資產 10%
MIN_SCORE_HK = 0.620        # 港股 BUY 門檻
MIN_SCORE_US = 0.620        # 美股 BUY 門檻
STOP_LOSS_PCT = -0.08       # 止損 -8%
TAKE_PROFIT_PCT = 0.15      # 止盈 +15%
```

## 風險聲明

本系統僅供研究學習，模擬交易不涉及真實資金。量化策略存在過擬合風險，歷史表現不代表未來收益。
