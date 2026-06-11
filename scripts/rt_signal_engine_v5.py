#!/usr/bin/env python3
"""
實時信號引擎 v5.0
- 每3秒拉取實時報價（騰訊API批量查詢）
- 條件觸發器：RSI/布林/均線/成交量異動
- 觸發時先跑完整多因子分析
- 即時發送通知（寫入文件，由外部腳本推送）
"""
import subprocess, json, time, os, sys, math, urllib.request
from datetime import datetime, timedelta
from collections import defaultdict
from threading import Thread, Lock

# ========== 配置 ==========
POLL_INTERVAL = 3       # 每3秒拉一次報價
FULL_SCAN_INTERVAL = 30 # 每30秒做一次全量條件檢查
SIGNAL_COOLDOWN = 1800  # 同一信號30分鐘內唔重複觸發
ALERT_FILE = "/tmp/rt_signal_alert.json"
STATE_FILE = "/tmp/rt_signal_state.json"

# 股票池 — 港股+美股
HK_WATCHLIST = [
    "00700","03690","01810","09896","00916","02015","02208","07226","01918",
    "03888","00177","03328","03968","00929","06690","00948","02328","00959",
    "09866","03988","01398","00945","00939","00148","00656","01244","09988",
    "09618","00005","00016","00002","00003","00006","00012","00017","00019",
    "00027","00241","00267","00288","00291","00386","00388","00669","00762",
    "00823","00857","00868","00881","00883","01775","02007","02013","02018",
    "02313","02319","02382","02388","06098","06160","06862","09626","09961",
]
US_WATCHLIST = [
    "AAPL","MSFT","NVDA","TSLA","AMD","META","AMZN","GOOGL","NFLX",
    "PDD","NOK","ARAY","BABA","JD","NIO","LI","BIDU","NTES","V","JPM",
    "BAC","GS","JNJ","UNH","PFE","INTC","CRM","ADBE","XPEV","ZH","BILI","IQ",
]

# ========== 數據層 ==========
def db(sql):
    try:
        r = subprocess.run(
            ["docker","exec","quantmind-db","psql","-U","quantmind","-d","quantmind","-t","-A","-c",sql],
            capture_output=True, text=True, timeout=30
        )
        return r.stdout.strip()
    except:
        return ""

def fetch_hk_quotes(symbols):
    """批量拉取港股實時報價 — 騰訊API"""
    if not symbols: return {}
    batch = ",".join(f"hk{s}" for s in symbols)
    url = f"http://qt.gtimg.cn/q={batch}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://finance.qq.com"})
        txt = urllib.request.urlopen(req, timeout=5).read().decode("gbk","ignore")
        results = {}
        for line in txt.strip().split("\n"):
            if "~" not in line: continue
            parts = line.split("~")
            if len(parts) < 45: continue
            sym = parts[2].split(".")[0]  # 去掉.OQ等後綴
            try:
                results[sym] = {
                    "price": float(parts[3]) if parts[3] else 0,
                    "open": float(parts[5]) if parts[5] else 0,
                    "high": float(parts[33]) if parts[33] else 0,
                    "low": float(parts[34]) if parts[34] else 0,
                    "prev_close": float(parts[4]) if parts[4] else 0,
                    "volume": float(parts[6]) if parts[6] else 0,  # 手
                    "amount": float(parts[37]) if parts[37] else 0,  # 萬元
                    "change_pct": float(parts[32]) if parts[32] else 0,
                    "time": parts[30],
                }
            except (ValueError, IndexError):
                continue
        return results
    except Exception as e:
        return {}

def fetch_us_quotes(symbols):
    """批量拉取美股實時報價 — 騰訊API"""
    if not symbols: return {}
    batch = ",".join(f"us{s}" for s in symbols)
    url = f"http://qt.gtimg.cn/q={batch}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://finance.qq.com"})
        txt = urllib.request.urlopen(req, timeout=5).read().decode("gbk","ignore")
        results = {}
        for line in txt.strip().split("\n"):
            if "~" not in line: continue
            parts = line.split("~")
            if len(parts) < 45: continue
            sym = parts[2].split(".")[0]
            try:
                results[sym] = {
                    "price": float(parts[3]) if parts[3] else 0,
                    "open": float(parts[5]) if parts[5] else 0,
                    "high": float(parts[33]) if parts[33] else 0,
                    "low": float(parts[34]) if parts[34] else 0,
                    "prev_close": float(parts[4]) if parts[4] else 0,
                    "volume": float(parts[6]) if parts[6] else 0,
                    "amount": float(parts[37]) if parts[37] else 0,
                    "change_pct": float(parts[32]) if parts[32] else 0,
                    "time": parts[30],
                }
            except (ValueError, IndexError):
                continue
        return results
    except:
        return {}

# ========== 增量指標計算 ==========
class IncrementalIndicators:
    """每隻股票嘅增量指標 — 只更新最新數據點"""
    def __init__(self, symbol):
        self.symbol = symbol
        self.closes = []
        self.highs = []
        self.lows = []
        self.volumes = []
        self.rsi_14 = None
        self.rsi_gains = []
        self.rsi_losses = []
        self.ma5 = None
        self.ma10 = None
        self.ma20 = None
        self.bb_upper = None
        self.bb_mid = None
        self.bb_lower = None
        self.macd_dif = None
        self.macd_dea = None
        self.macd_hist = None
        self.ema_fast = None
        self.ema_slow = None
        self.atr_14 = None
        self.loaded = False

    def load_history(self, days=100):
        """從DB載入歷史K線"""
        raw = db(f"SELECT close_price, high_price, low_price, volume FROM klines WHERE symbol='{self.symbol}' AND interval='day' ORDER BY timestamp DESC LIMIT {days}")
        rows = []
        for line in raw.split("\n"):
            if not line.strip(): continue
            p = line.split("|")
            if len(p) >= 4:
                try:
                    rows.append((float(p[0]), float(p[1]), float(p[2]), float(p[3])))
                except:
                    continue
        rows.reverse()
        for c, h, l, v in rows:
            self._update(c, h, l, v)
        self.loaded = True

    def _update(self, close, high, low, volume):
        """增量更新一個數據點"""
        self.closes.append(close)
        self.highs.append(high)
        self.lows.append(low)
        self.volumes.append(volume)

        n = len(self.closes)

        # RSI (增量)
        if n >= 2:
            change = self.closes[-1] - self.closes[-2]
            gain = max(change, 0)
            loss = max(-change, 0)
            self.rsi_gains.append(gain)
            self.rsi_losses.append(loss)
            if len(self.rsi_gains) >= 14:
                if len(self.rsi_gains) == 14:
                    avg_gain = sum(self.rsi_gains[-14:]) / 14
                    avg_loss = sum(self.rsi_losses[-14:]) / 14
                else:
                    prev_avg_gain = self._prev_avg_gain
                    prev_avg_loss = self._prev_avg_loss
                    avg_gain = (prev_avg_gain * 13 + gain) / 14
                    avg_loss = (prev_avg_loss * 13 + loss) / 14
                self._prev_avg_gain = avg_gain
                self._prev_avg_loss = avg_loss
                self.rsi_14 = 100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss > 0 else 100

        # MA (增量)
        if n >= 5: self.ma5 = sum(self.closes[-5:]) / 5
        if n >= 10: self.ma10 = sum(self.closes[-10:]) / 10
        if n >= 20:
            self.ma20 = sum(self.closes[-20:]) / 20
            w = self.closes[-20:]
            std = (sum((x - self.ma20)**2 for x in w) / 20) ** 0.5
            self.bb_upper = self.ma20 + 2 * std
            self.bb_mid = self.ma20
            self.bb_lower = self.ma20 - 2 * std

        # MACD (增量 EMA)
        if n >= 2:
            k_fast = 2 / 13; k_slow = 2 / 27; k_signal = 2 / 10
            if self.ema_fast is None:
                self.ema_fast = close
                self.ema_slow = close
            else:
                self.ema_fast = close * k_fast + self.ema_fast * (1 - k_fast)
                self.ema_slow = close * k_slow + self.ema_slow * (1 - k_slow)
            self.macd_dif = self.ema_fast - self.ema_slow
            if self.macd_dea is None:
                self.macd_dea = self.macd_dif
            else:
                self.macd_dea = self.macd_dif * k_signal + self.macd_dea * (1 - k_signal)
            self.macd_hist = self.macd_dif - self.macd_dea

        # ATR (增量)
        if n >= 15:
            trs = []
            for i in range(max(1, n-14), n):
                tr = max(
                    self.highs[i] - self.lows[i],
                    abs(self.highs[i] - self.closes[i-1]),
                    abs(self.lows[i] - self.closes[i-1])
                )
                trs.append(tr)
            self.atr_14 = sum(trs) / len(trs)

    def update_realtime(self, price, high, low, volume):
        """用實時價格更新（唔修改最後一根K線，而係加一個虛擬數據點）"""
        # 暫時用實時價格更新，下次K線更新時會被真實數據替換
        if self.closes:
            self._update(price, high, low, volume)

    def get_score(self):
        """計算多因子分數 (-1 to +1)"""
        if not self.closes or len(self.closes) < 30:
            return None, []

        c = self.closes[-1]
        score = 0
        reasons = []

        # 趨勢
        if self.ma5 and self.ma10 and self.ma20:
            if c > self.ma5 > self.ma10 > self.ma20:
                score += 0.8; reasons.append("多頭排列")
            elif c > self.ma5 and c > self.ma10:
                score += 0.4
            elif c < self.ma5 < self.ma10 < self.ma20:
                score -= 0.8; reasons.append("空頭排列")
            elif c < self.ma5 and c < self.ma10:
                score -= 0.4

        # RSI
        if self.rsi_14 is not None:
            if self.rsi_14 > 70:
                score -= 0.3; reasons.append(f"RSI偏高({self.rsi_14:.0f})")
            elif self.rsi_14 > 55:
                score += 0.3
            elif self.rsi_14 < 30:
                score += 0.3; reasons.append(f"RSI超賣({self.rsi_14:.0f})")
            elif self.rsi_14 < 45:
                score -= 0.2

        # MACD
        if self.macd_hist is not None and self.macd_dif is not None:
            if self.macd_hist > 0 and self.macd_dif > 0:
                score += 0.3; reasons.append("MACD金叉+正值")
            elif self.macd_hist > 0:
                score += 0.1
            elif self.macd_hist < 0 and self.macd_dif < 0:
                score -= 0.3
            elif self.macd_hist < 0:
                score -= 0.1

        # 布林帶
        if self.bb_upper and self.bb_lower:
            if c <= self.bb_lower * 1.02:
                score += 0.3; reasons.append("觸及布林下軌")
            elif c >= self.bb_upper * 0.98:
                score -= 0.2; reasons.append("觸及布林上軌")

        # 成交量
        if len(self.volumes) >= 20:
            avg_vol = sum(self.volumes[-20:]) / 20
            if avg_vol > 0:
                vr = self.volumes[-1] / avg_vol
                if vr > 2.0:
                    score += 0.2; reasons.append(f"放量{vr:.1f}倍")
                elif vr > 1.5 and c > self.closes[-2]:
                    score += 0.1

        # 動量
        if len(self.closes) >= 5:
            mom = (c / self.closes[-5] - 1) * 100
            if abs(mom) > 5:
                tag = "上升" if mom > 0 else "下降"
                reasons.append(f"5日動量{mom:+.1f}%")

        return max(-1, min(1, score)), reasons


# ========== 條件觸發器 ==========
class TriggerEngine:
    """條件觸發器 — 只有滿足條件先觸發完整分析"""
    def __init__(self):
        self.alerts = []
        self.cooldowns = {}  # key -> last_trigger_time

    def check(self, symbol, indicators, quote):
        """檢查所有觸發條件"""
        if not indicators.closes or len(indicators.closes) < 20:
            return

        c = quote["price"]
        if c <= 0:
            return

        now = time.time()
        triggered = []

        # 1. RSI 極端值
        if indicators.rsi_14 is not None:
            if indicators.rsi_14 <= 30:
                triggered.append(("RSI超賣", f"RSI={indicators.rsi_14:.0f}", "BUY"))
            elif indicators.rsi_14 >= 70:
                triggered.append(("RSI超買", f"RSI={indicators.rsi_14:.0f}", "SELL"))

        # 2. 布林帶突破
        if indicators.bb_upper and indicators.bb_lower:
            if c <= indicators.bb_lower:
                triggered.append(("布林下軌突破", f"價格${c} < 下軌${indicators.bb_lower:.2f}", "BUY"))
            elif c >= indicators.bb_upper:
                triggered.append(("布林上軌突破", f"價格${c} > 上軌${indicators.bb_upper:.2f}", "SELL"))

        # 3. 均線金叉/死叉
        if indicators.ma5 and indicators.ma10 and len(indicators.closes) >= 2:
            prev_c = indicators.closes[-2]
            if c > indicators.ma5 and prev_c <= indicators.ma5:
                triggered.append(("站上MA5", f"${c} > MA5=${indicators.ma5:.2f}", "BUY"))
            if c < indicators.ma5 and prev_c >= indicators.ma5:
                triggered.append(("跌破MA5", f"${c} < MA5=${indicators.ma5:.2f}", "SELL"))

        if indicators.ma10 and indicators.ma20:
            if indicators.ma10 > indicators.ma20 and len(indicators.closes) >= 21:
                prev_ma10 = sum(indicators.closes[-11:-1]) / 10
                prev_ma20 = sum(indicators.closes[-21:-1]) / 20
                if prev_ma10 <= prev_ma20:
                    triggered.append(("MA金叉", f"MA10上穿MA20", "BUY"))

        # 4. 成交量異動
        if len(indicators.volumes) >= 20:
            avg_vol = sum(indicators.volumes[-20:]) / 20
            if avg_vol > 0 and quote.get("volume", 0) > 0:
                # 用實時成交量同歷史均量比較
                vol_ratio = quote["volume"] / (avg_vol / 240) if avg_vol > 0 else 0  # 粗略估算
                if vol_ratio > 3:
                    triggered.append(("成交量異動", f"量比={vol_ratio:.1f}", "WATCH"))

        # 5. 大幅波動
        if abs(quote.get("change_pct", 0)) >= 5:
            direction = "急漲" if quote["change_pct"] > 0 else "急跌"
            triggered.append((direction, f"{quote['change_pct']:+.1f}%", "WATCH"))

        # 冷卻期檢查 + 觸發
        for trigger_name, detail, signal_type in triggered:
            key = f"{symbol}_{trigger_name}_{datetime.now().strftime('%Y%m%d')}"
            if key in self.cooldowns and now - self.cooldowns[key] < SIGNAL_COOLDOWN:
                continue
            self.cooldowns[key] = now
            
            # 計算入場/止盈/止損 (基於ATR)
            atr = indicators.atr_14 if indicators.atr_14 else c * 0.02  # 默認2%
            
            if signal_type == "BUY":
                entry_price = round(c, 2)
                stop_loss = round(c - 2 * atr, 2)  # 2倍ATR止損
                take_profit = round(c + 3 * atr, 2)  # 3倍ATR止盈 (1.5:1風險回報)
                rr_ratio = round(3 * atr / (2 * atr), 2) if atr > 0 else 1.5
            elif signal_type == "SELL":
                entry_price = round(c, 2)
                stop_loss = round(c + 2 * atr, 2)  # 2倍ATR止損
                take_profit = round(c - 3 * atr, 2)  # 3倍ATR止盈
                rr_ratio = round(3 * atr / (2 * atr), 2) if atr > 0 else 1.5
            else:  # WATCH
                entry_price = round(c, 2)
                stop_loss = None
                take_profit = None
                rr_ratio = None
            
            self.alerts.append({
                "symbol": symbol,
                "trigger": trigger_name,
                "detail": detail,
                "signal_type": signal_type,
                "price": c,
                "change_pct": quote.get("change_pct", 0),
                "time": datetime.now().strftime("%H:%M:%S"),
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "rr_ratio": rr_ratio,
                "atr": round(atr, 3),
            })


# ========== 主循環 ==========
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {"cooldowns": {}, "date": ""}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def send_alert(alerts):
    """寫入alert文件，由外部腳本推送"""
    with open(ALERT_FILE, "w") as f:
        json.dump(alerts, f, ensure_ascii=False, indent=2)

def main():
    log("=" * 60)
    log("實時信號引擎 v5.0 啟動")
    log(f"港股: {len(HK_WATCHLIST)}隻 | 美股: {len(US_WATCHLIST)}隻")
    log("=" * 60)

    # 初始化指標
    indicators = {}
    all_symbols = [(s, "HK") for s in HK_WATCHLIST] + [(s, "US") for s in US_WATCHLIST]

    log("載入歷史K線...")
    for sym, market in all_symbols:
        ind = IncrementalIndicators(sym)
        ind.load_history(100)
        indicators[sym] = ind
    log(f"載入完成: {len(indicators)}隻股票")

    trigger = TriggerEngine()
    state = load_state()
    trigger.cooldowns = state.get("cooldowns", {})

    last_full_scan = 0
    cycle = 0

    while True:
        now = time.time()
        cycle += 1

        # 判斷交易時間
        dt = datetime.now()
        weekday = dt.weekday()
        h, m = dt.hour, dt.minute
        t = h * 60 + m

        hk_open = weekday < 5 and (570 <= t <= 720 or 780 <= t <= 960)  # 9:30-12:00, 13:00-16:00
        us_open = weekday < 5 and (t >= 1290 or t <= 240)  # 21:30-04:00 (next day)

        if not hk_open and not us_open:
            if cycle % 100 == 0:
                log(f"非交易時間 (HK:{hk_open} US:{us_open}), 等待...")
            time.sleep(30)
            continue

        # 拉取實時報價
        hk_quotes = {}
        us_quotes = {}
        if hk_open:
            hk_quotes = fetch_hk_quotes(HK_WATCHLIST)
        if us_open:
            us_quotes = fetch_us_quotes(US_WATCHLIST)

        all_quotes = {**hk_quotes, **us_quotes}

        if not all_quotes:
            if cycle % 100 == 0:
                log("冇報價數據")
            time.sleep(POLL_INTERVAL)
            continue

        # 全量條件檢查（每30秒一次）
        if now - last_full_scan >= FULL_SCAN_INTERVAL:
            trigger.alerts = []
            for sym, quote in all_quotes.items():
                if sym in indicators:
                    # 用實時價格更新指標（增量）
                    indicators[sym].update_realtime(
                        quote["price"], quote["high"], quote["low"], quote["volume"]
                    )
                    trigger.check(sym, indicators[sym], quote)

            if trigger.alerts:
                log(f"🚨 觸發 {len(trigger.alerts)} 個信號!")
                for alert in trigger.alerts:
                    log(f"  {alert['symbol']} {alert['trigger']}: {alert['detail']} [{alert['signal_type']}]")
                send_alert(trigger.alerts)

                # 更新冷卻狀態
                state["cooldowns"] = trigger.cooldowns
                state["date"] = dt.strftime("%Y-%m-%d")
                save_state(state)
            else:
                if cycle % 100 == 0:
                    log(f"掃描完成: {len(all_quotes)}隻報價, 0個觸發")

            last_full_scan = now

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
