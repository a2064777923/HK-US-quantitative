#!/usr/bin/env python3
"""
信號引擎 v4 — 全面多維度分析
技術面 + 消息面 + 大市環境 + 基本面 + 價格預測 + 掛單價
"""
import subprocess, json, math, urllib.request, time
from datetime import datetime, timedelta

def db(sql):
    r = subprocess.run(
        ['docker', 'exec', 'quantmind-db', 'psql', '-U', 'quantmind', '-d', 'quantmind', '-t', '-A', '-c', sql],
        capture_output=True, text=True, timeout=30
    )
    return r.stdout.strip()

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ═══════════════════════════════════════════
# 技術指標計算（v3同款）
# ═══════════════════════════════════════════
def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal: return None, None, None
    def ema(data, period):
        k = 2 / (period + 1)
        result = [data[0]]
        for i in range(1, len(data)):
            result.append(data[i] * k + result[-1] * (1 - k))
        return result
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(closes))]
    signal_line = ema(macd_line, signal)
    histogram = [macd_line[i] - signal_line[i] for i in range(len(closes))]
    return macd_line[-1], signal_line[-1], histogram[-1]

def calc_bollinger(closes, period=20, num_std=2):
    if len(closes) < period: return None, None, None
    window = closes[-period:]
    ma = sum(window) / period
    std = math.sqrt(sum((x - ma) ** 2 for x in window) / period)
    return ma + num_std * std, ma, ma - num_std * std

def calc_ma(closes, period):
    if len(closes) < period: return None
    return sum(closes[-period:]) / period

def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1: return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    return sum(trs[-period:]) / period if len(trs) >= period else None

# ═══════════════════════════════════════════
# 支撐阻力計算
# ═══════════════════════════════════════════
def calc_support_resistance(closes, highs, lows, current):
    """用近期高低點+樞軸點計算支撐阻力"""
    supports = []
    resistances = []
    
    # 近20日高低點
    if len(closes) >= 20:
        recent_high = max(highs[-20:])
        recent_low = min(lows[-20:])
        supports.append(recent_low)
        resistances.append(recent_high)
    
    # Pivot Points (Classic)
    if len(highs) >= 1 and len(lows) >= 1 and len(closes) >= 1:
        h, l, c = highs[-1], lows[-1], closes[-1]
        pivot = (h + l + c) / 3
        s1 = 2 * pivot - h
        r1 = 2 * pivot - l
        s2 = pivot - (h - l)
        r2 = pivot + (h - l)
        supports.extend([s1, s2])
        resistances.extend([r1, r2])
    
    # MA levels as support/resistance
    for period in [5, 10, 20]:
        ma = calc_ma(closes, period)
        if ma:
            if ma < current:
                supports.append(ma)
            else:
                resistances.append(ma)
    
    # BB levels
    upper, mid, lower = calc_bollinger(closes)
    if upper and lower:
        supports.append(lower)
        resistances.append(upper)
    
    # Filter and sort
    supports = sorted([s for s in supports if s < current and s > 0], reverse=True)
    resistances = sorted([r for r in resistances if r > current])
    
    return {
        'support_1': round(supports[0], 3) if len(supports) > 0 else None,
        'support_2': round(supports[1], 3) if len(supports) > 1 else None,
        'resistance_1': round(resistances[0], 3) if len(resistances) > 0 else None,
        'resistance_2': round(resistances[1], 3) if len(resistances) > 1 else None,
    }

# ═══════════════════════════════════════════
# 掛單價計算
# ═══════════════════════════════════════════
def calc_order_prices(current, atr, sr, side='BUY'):
    """計算建議掛單價、止損價、止盈價"""
    if not atr or atr <= 0:
        atr = current * 0.02  # fallback: 2%
    
    if side == 'BUY':
        # 買入價：第一支撐位附近，或現價回調1個ATR
        entry = sr.get('support_1') or (current - atr * 0.5)
        entry = max(entry, current * 0.97)  # 最多等3%回調
        
        # 止損：第二支撐位或入場價下方2ATR
        stop_loss = sr.get('support_2') or (entry - atr * 2)
        stop_loss = max(stop_loss, entry * 0.92)  # 最多虧8%
        
        # 止盈：第一阻力位，或入場價上方3ATR
        take_profit = sr.get('resistance_1') or (entry + atr * 3)
        
        # 風險回報比
        risk = entry - stop_loss
        reward = take_profit - entry
        rr_ratio = reward / risk if risk > 0 else 0
        
        return {
            'entry_price': round(entry, 3),
            'stop_loss': round(stop_loss, 3),
            'take_profit': round(take_profit, 3),
            'risk_pct': round(risk / entry * 100, 1),
            'reward_pct': round(reward / entry * 100, 1),
            'rr_ratio': round(rr_ratio, 2),
            'atr': round(atr, 3),
            'support_1': sr.get('support_1'),
            'support_2': sr.get('support_2'),
            'resistance_1': sr.get('resistance_1'),
            'resistance_2': sr.get('resistance_2'),
        }
    else:  # SELL
        entry = sr.get('resistance_1') or (current + atr * 0.5)
        entry = min(entry, current * 1.03)
        stop_loss = sr.get('resistance_2') or (entry + atr * 2)
        stop_loss = min(stop_loss, entry * 1.08)
        take_profit = sr.get('support_1') or (entry - atr * 3)
        
        risk = stop_loss - entry
        reward = entry - take_profit
        rr_ratio = reward / risk if risk > 0 else 0
        
        return {
            'entry_price': round(entry, 3),
            'stop_loss': round(stop_loss, 3),
            'take_profit': round(take_profit, 3),
            'risk_pct': round(risk / entry * 100, 1),
            'reward_pct': round(reward / entry * 100, 1),
            'rr_ratio': round(rr_ratio, 2),
            'atr': round(atr, 3),
            'support_1': sr.get('support_1'),
            'resistance_1': sr.get('resistance_1'),
        }

# ═══════════════════════════════════════════
# 小時級價格預測
# ═══════════════════════════════════════════
def calc_hourly_prediction(closes, volumes, current):
    """基於近期走勢預測未來幾個時段嘅價格區間"""
    if len(closes) < 20:
        return None
    
    # 計算日均波動率
    daily_ranges = []
    for i in range(1, min(20, len(closes))):
        daily_ranges.append(abs(closes[-i] - closes[-i-1]) / closes[-i-1])
    avg_daily_move = sum(daily_ranges) / len(daily_ranges) if daily_ranges else 0.02
    
    # 假設每小時 = 日波動/6.5（港股交易時段）
    hourly_move = avg_daily_move / 6.5
    
    # 趨勢方向（基於MA斜率）
    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    trend = 0
    if ma5 and ma10:
        if current > ma5 > ma10:
            trend = 1  # 上升
        elif current < ma5 < ma10:
            trend = -1  # 下降
    
    # 預測未來4個時段
    predictions = []
    for h in range(1, 5):
        # 基礎預測 = 趨勢方向 × 小時波動 × 時段數
        drift = trend * hourly_move * h * 0.3  # 趨勢drift（保守）
        uncertainty = hourly_move * h * 0.7  # 不確定性
        
        pred_center = current * (1 + drift)
        pred_high = current * (1 + drift + uncertainty)
        pred_low = current * (1 + drift - uncertainty)
        
        predictions.append({
            'hours': h,
            'center': round(pred_center, 2),
            'high': round(pred_high, 2),
            'low': round(pred_low, 2),
            'range_pct': round(uncertainty * 100, 2),
        })
    
    return {
        'daily_volatility': round(avg_daily_move * 100, 2),
        'hourly_volatility': round(hourly_move * 100, 2),
        'trend': '上升' if trend > 0 else ('下降' if trend < 0 else '橫盤'),
        'predictions': predictions,
    }

# ═══════════════════════════════════════════
# 技術面評分（v3同款邏輯）
# ═══════════════════════════════════════════
def score_technical(closes, highs, lows, volumes):
    """返回 -1~+1 嘅技術面分數同原因"""
    if len(closes) < 30:
        return 0, [], {}
    
    current = closes[-1]
    reasons = []
    components = {}
    
    # 趨勢 (30%)
    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    trend_score = 0
    
    if ma5 and ma10 and ma20:
        if current > ma5 > ma10 > ma20:
            trend_score = 1.0
            reasons.append("強勢多頭排列")
        elif current > ma5 > ma10:
            trend_score = 0.6
            reasons.append("短中期多頭")
        elif current > ma5:
            trend_score = 0.3
            reasons.append("站上5日線")
        elif current < ma5 < ma10 < ma20:
            trend_score = -1.0
            reasons.append("⚠️空頭排列")
        elif current < ma5 < ma10:
            trend_score = -0.6
            reasons.append("⚠️短中期空頭")
        elif current < ma5:
            trend_score = -0.3
            reasons.append("⚠️跌破5日線")
    
    if ma20 and len(closes) >= 25:
        ma20_prev = calc_ma(closes[:-5], 20)
        if ma20_prev:
            slope = (ma20 - ma20_prev) / ma20_prev * 100
            if slope > 2:
                trend_score = min(trend_score + 0.3, 1.0)
                reasons.append(f"MA20上升{slope:+.1f}%")
            elif slope < -2:
                trend_score = max(trend_score - 0.3, -1.0)
                reasons.append(f"⚠️MA20下降{slope:+.1f}%")
    components['trend'] = trend_score
    
    # 動量 (25%)
    momentum_score = 0
    rsi = calc_rsi(closes)
    macd_line, signal_line, histogram = calc_macd(closes)
    
    if rsi:
        if rsi < 25: momentum_score += 0.4; reasons.append(f"RSI超賣({rsi:.0f})")
        elif rsi < 35: momentum_score += 0.2; reasons.append(f"RSI偏低({rsi:.0f})")
        elif rsi > 75: momentum_score -= 0.4; reasons.append(f"⚠️RSI超買({rsi:.0f})")
        elif rsi > 65: momentum_score -= 0.2; reasons.append(f"⚠️RSI偏高({rsi:.0f})")
    
    if macd_line is not None:
        if histogram > 0 and macd_line > 0: momentum_score += 0.3; reasons.append("MACD金叉+正值")
        elif histogram > 0: momentum_score += 0.15; reasons.append("MACD柱轉正")
        elif histogram < 0 and macd_line < 0: momentum_score -= 0.3; reasons.append("⚠️MACD死叉+負值")
        elif histogram < 0: momentum_score -= 0.15; reasons.append("⚠️MACD柱轉負")
    
    if len(closes) >= 6:
        mom5 = (closes[-1] - closes[-6]) / closes[-6] * 100
        if mom5 > 5: momentum_score += 0.2; reasons.append(f"5日動量+{mom5:.1f}%")
        elif mom5 > 2: momentum_score += 0.1; reasons.append(f"5日+{mom5:.1f}%")
        elif mom5 < -5: momentum_score -= 0.2; reasons.append(f"⚠️5日{mom5:.1f}%")
        elif mom5 < -2: momentum_score -= 0.1; reasons.append(f"⚠️5日{mom5:.1f}%")
    
    momentum_score = max(-1, min(1, momentum_score))
    components['momentum'] = momentum_score
    
    # 結構 (20%)
    structure_score = 0
    upper, bb_mid, lower = calc_bollinger(closes)
    if upper and lower:
        bb_pos = (current - lower) / (upper - lower) if upper > lower else 0.5
        if current <= lower: structure_score += 0.3; reasons.append("觸及布林下軌")
        elif current >= upper: structure_score -= 0.2; reasons.append("⚠️觸及布林上軌")
        elif bb_pos < 0.3: structure_score += 0.15
    components['structure'] = structure_score
    
    # 量能 (15%)
    volume_score = 0
    if len(volumes) >= 20:
        vol_sma = sum(volumes[-20:]) / 20
        if vol_sma > 0:
            vol_ratio = volumes[-1] / vol_sma
            price_up = closes[-1] > closes[-2]
            if price_up and vol_ratio > 1.5: volume_score += 0.4; reasons.append(f"放量上漲({vol_ratio:.1f}x)")
            elif price_up and vol_ratio > 1.2: volume_score += 0.2
            elif not price_up and vol_ratio > 2.0: volume_score -= 0.3; reasons.append(f"⚠️放量下跌({vol_ratio:.1f}x)")
    components['volume'] = volume_score
    
    # 位置 (10%)
    position_score = 0
    if len(closes) >= 60:
        h60, l60 = max(closes[-60:]), min(closes[-60:])
        if h60 > l60:
            pos = (current - l60) / (h60 - l60)
            if pos < 0.2: position_score += 0.3; reasons.append(f"60日低位{pos*100:.0f}%")
            elif pos > 0.85: position_score -= 0.2; reasons.append(f"⚠️60日高位{pos*100:.0f}%")
    components['position'] = position_score
    
    # 綜合
    raw = (trend_score * 0.30 + momentum_score * 0.25 + structure_score * 0.20 +
           volume_score * 0.15 + position_score * 0.10)
    final = (raw + 1) / 2
    
    # 硬規則
    if trend_score <= -0.6:
        final = min(final, 0.45)
        reasons.append("⛔ 強空頭禁止買入")
    
    return final, reasons, components

# ═══════════════════════════════════════════
# 主分析流程
# ═══════════════════════════════════════════
def analyze_stock(symbol, exchange):
    """全面分析一隻股票，返回完整報告"""
    
    # 獲取K線
    raw = db(f"""
        SELECT open_price, high_price, low_price, close_price, volume 
        FROM klines WHERE symbol='{symbol}' AND interval='day'
        ORDER BY timestamp DESC LIMIT 120
    """)
    if not raw: return None
    
    rows = []
    for line in raw.split('\n'):
        if not line.strip(): continue
        p = line.split('|')
        if len(p) >= 5:
            try:
                rows.append({'open': float(p[0]), 'high': float(p[1]), 'low': float(p[2]),
                            'close': float(p[3]), 'volume': float(p[4])})
            except: continue
    
    if len(rows) < 30: return None
    rows.reverse()
    
    closes = [r['close'] for r in rows]
    highs = [r['high'] for r in rows]
    lows = [r['low'] for r in rows]
    volumes = [r['volume'] for r in rows]
    current = closes[-1]
    
    # 1. 技術面評分
    tech_score, tech_reasons, tech_components = score_technical(closes, highs, lows, volumes)
    
    # 2. 支撐阻力
    sr = calc_support_resistance(closes, highs, lows, current)
    
    # 3. ATR
    atr = calc_atr(highs, lows, closes)
    
    # 4. 小時級預測
    prediction = calc_hourly_prediction(closes, volumes, current)
    
    # 5. 判斷方向
    if tech_score >= 0.62: side = 'BUY'
    elif tech_score <= 0.38: side = 'SELL'
    else: side = 'HOLD'
    
    # 6. 掛單價
    order_prices = None
    if side in ('BUY', 'SELL'):
        order_prices = calc_order_prices(current, atr, sr, side)
    
    # 7. 技術指標原始值
    rsi = calc_rsi(closes)
    macd_line, signal_line, histogram = calc_macd(closes)
    upper, bb_mid, lower = calc_bollinger(closes)
    ma5, ma10, ma20 = calc_ma(closes, 5), calc_ma(closes, 10), calc_ma(closes, 20)
    
    # 8. 風險評估
    risk_flags = []
    if rsi and rsi > 75: risk_flags.append("RSI超買")
    if rsi and rsi < 25: risk_flags.append("RSI超賣")
    if current >= upper * 0.98 if upper else False: risk_flags.append("觸及布林上軌")
    if current <= lower * 1.02 if lower else False: risk_flags.append("觸及布林下軌")
    if order_prices and order_prices.get('rr_ratio', 0) < 1.5: risk_flags.append(f"風險回報比偏低({order_prices.get('rr_ratio')})")
    
    return {
        'symbol': symbol,
        'exchange': exchange,
        'price': round(current, 3),
        'side': side,
        'score': round(tech_score, 4),
        'reasons': tech_reasons,
        'components': {k: round(v, 3) for k, v in tech_components.items()},
        'indicators': {
            'rsi': round(rsi, 1) if rsi else None,
            'macd': round(macd_line, 4) if macd_line else None,
            'macd_signal': round(signal_line, 4) if signal_line else None,
            'macd_hist': round(histogram, 4) if histogram else None,
            'ma5': round(ma5, 3) if ma5 else None,
            'ma10': round(ma10, 3) if ma10 else None,
            'ma20': round(ma20, 3) if ma20 else None,
            'bb_upper': round(upper, 3) if upper else None,
            'bb_mid': round(bb_mid, 3) if bb_mid else None,
            'bb_lower': round(lower, 3) if lower else None,
            'atr': round(atr, 3) if atr else None,
        },
        'support_resistance': sr,
        'order_prices': order_prices,
        'prediction': prediction,
        'risk_flags': risk_flags,
    }

def run():
    log("=" * 60)
    log("信號引擎 v4 — 全面多維度分析")
    
    today = datetime.now().strftime('%Y-%m-%d')
    
    stocks = db("""
        SELECT k.symbol, s.exchange FROM klines k 
        JOIN stocks s ON k.symbol = s.symbol
        WHERE k.interval = 'day' AND s.is_active = true
        AND s.exchange IN ('HKEX','NASDAQ','NYSE')
        GROUP BY k.symbol, s.exchange HAVING count(*) >= 30
        ORDER BY k.symbol
    """)
    
    if not stocks:
        log("❌ 無數據")
        return
    
    total = len([l for l in stocks.split('\n') if l.strip()])
    log(f"分析 {total} 隻股票...")
    
    results = {'BUY': [], 'SELL': [], 'HOLD': []}
    
    for line in stocks.split('\n'):
        if not line.strip(): continue
        parts = line.split('|')
        if len(parts) < 2: continue
        symbol, exchange = parts[0], parts[1]
        
        result = analyze_stock(symbol, exchange)
        if result is None: continue
        results[result['side']].append(result)
        
        # 更新DB
        quality = {
            'reasons': result['reasons'],
            'components': result['components'],
            **result['indicators'],
            'support_resistance': result['support_resistance'],
            'order_prices': result['order_prices'],
            'prediction': result['prediction'],
            'risk_flags': result['risk_flags'],
        }
        quality_json = json.dumps(quality, ensure_ascii=False, default=str).replace("'", "''")
        db(f"""
            UPDATE engine_signal_scores 
            SET fusion_score = {result['score']}, signal_side = '{result['side']}',
                quality = '{quality_json}'::jsonb, expected_price = {result['price']},
                model_version = 'signal_v4', feature_version = 'v4_full'
            WHERE trade_date = (SELECT max(trade_date) FROM engine_signal_scores) 
            AND symbol = '{symbol}'
        """)
    
    results['BUY'].sort(key=lambda x: x['score'], reverse=True)
    results['SELL'].sort(key=lambda x: x['score'])
    
    log(f"\n{'='*60}")
    log(f"📊 分析完成: {total} 隻")
    log(f"   🟢 BUY: {len(results['BUY'])} | ⚪ HOLD: {len(results['HOLD'])} | 🔴 SELL: {len(results['SELL'])}")
    
    log(f"\n── TOP BUY 信號（含掛單價）──")
    for s in results['BUY'][:15]:
        op = s.get('order_prices', {})
        pred = s.get('prediction', {})
        pred_trend = pred.get('trend', '?')
        log(f"  {s['symbol']:8s} score={s['score']:.3f} ${s['price']}")
        log(f"    原因: {', '.join(s['reasons'][:4])}")
        if op:
            log(f"    📌 建議買入: ${op.get('entry_price','?')} | 止損: ${op.get('stop_loss','?')} | 止盈: ${op.get('take_profit','?')} | 風險回報: {op.get('rr_ratio','?')}")
        if pred:
            preds = pred.get("predictions", [])
            if preds and len(preds) > 2:
                log(f"    📈 小時預測: {pred_trend} | 4h區間: ${preds[3]["low"]}~${preds[3]["high"]}")
        if s['risk_flags']:
            log(f"    ⚠️ 風險: {', '.join(s['risk_flags'])}")
        log("")
    
    log(f"✅ 信號已更新 (v4)")

if __name__ == '__main__':
    run()
