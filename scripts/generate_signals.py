#!/usr/bin/env python3
"""
Signal Generator v2 — UPDATE quality field on existing records
"""
import subprocess, json
from datetime import datetime

def db(sql):
    r = subprocess.run(
        ['docker', 'exec', 'quantmind-db', 'psql', '-U', 'quantmind', '-d', 'quantmind', '-t', '-A', '-c', sql],
        capture_output=True, text=True, timeout=30
    )
    return r.stdout.strip()

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calculate_ma(closes, period):
    if len(closes) < period: return None
    return sum(closes[-period:]) / period

def generate():
    print(f"[{datetime.now()}] Generating signal quality v2...")
    
    today = db("SELECT max(trade_date) FROM engine_signal_scores")
    print(f"Latest trade_date: {today}")
    
    symbols = db(f"""
        SELECT DISTINCT symbol FROM engine_signal_scores 
        WHERE trade_date = '{today}'
    """)
    if not symbols:
        print("No signals found")
        return
    
    count = 0
    for line in symbols.split('\n'):
        if not line.strip(): continue
        symbol = line.strip()
        
        klines = db(f"""
            SELECT close_price FROM klines 
            WHERE symbol = '{symbol}' AND interval = 'day'
            ORDER BY timestamp DESC LIMIT 30
        """)
        if not klines: continue
        
        closes = [float(x) for x in klines.split('\n') if x]
        if len(closes) < 20: continue
        closes.reverse()
        current = closes[-1]
        
        ma5 = calculate_ma(closes, 5)
        ma10 = calculate_ma(closes, 10)
        ma20 = calculate_ma(closes, 20)
        rsi = calculate_rsi(closes)
        
        reasons = []
        if ma5 and ma10 and ma20:
            if current > ma5 > ma10 > ma20: reasons.append("強勢多頭排列(MA5>MA10>MA20)")
            elif current > ma5 > ma10: reasons.append("短中期均線多頭(MA5>MA10)")
            elif current > ma5: reasons.append("站上5日線")
            elif current < ma5 < ma10 < ma20: reasons.append("空頭排列(MA5<MA10<MA20)")
            elif current < ma5 < ma10: reasons.append("短中期均線空頭")
            elif current < ma5: reasons.append("跌破5日線")
        
        if rsi < 30: reasons.append(f"RSI超賣({rsi:.1f})")
        elif rsi < 40: reasons.append(f"RSI偏低({rsi:.1f})")
        elif rsi > 70: reasons.append(f"RSI超買({rsi:.1f})")
        elif rsi > 60: reasons.append(f"RSI偏高({rsi:.1f})")
        
        momentum = 0
        if len(closes) >= 5:
            momentum = (closes[-1] - closes[-5]) / closes[-5]
            if momentum > 0.05: reasons.append(f"5日動量強({momentum*100:+.1f}%)")
            elif momentum > 0.02: reasons.append(f"5日動量正({momentum*100:+.1f}%)")
            elif momentum < -0.05: reasons.append(f"5日動量弱({momentum*100:+.1f}%)")
            elif momentum < -0.02: reasons.append(f"5日動量負({momentum*100:+.1f}%)")
        
        vol_ratio = 1.0
        volumes = db(f"""
            SELECT volume FROM klines 
            WHERE symbol = '{symbol}' AND interval = 'day'
            ORDER BY timestamp DESC LIMIT 10
        """)
        if volumes:
            vols = [float(x) for x in volumes.split('\n') if x]
            if len(vols) >= 5:
                avg5 = sum(vols[:5]) / 5
                avg10 = sum(vols) / 10
                vol_ratio = avg5 / avg10 if avg10 > 0 else 1.0
                if vol_ratio > 1.3: reasons.append(f"放量({vol_ratio:.1f}x)")
                elif vol_ratio < 0.7: reasons.append(f"縮量({vol_ratio:.1f}x)")
        
        quality = {
            "rsi": round(rsi, 1),
            "ma5": round(ma5, 3) if ma5 else None,
            "ma10": round(ma10, 3) if ma10 else None,
            "ma20": round(ma20, 3) if ma20 else None,
            "momentum_5d": round(momentum * 100, 2),
            "vol_ratio": round(vol_ratio, 2),
            "price": round(current, 3),
            "reasons": reasons
        }
        quality_json = json.dumps(quality, ensure_ascii=False).replace("'", "''")
        
        # UPDATE existing records (not INSERT)
        db(f"""
            UPDATE engine_signal_scores 
            SET quality = '{quality_json}'::jsonb, expected_price = {current}
            WHERE trade_date = '{today}' AND symbol = '{symbol}'
        """)
        count += 1
    
    print(f"✅ Updated quality for {count} symbols")

if __name__ == '__main__':
    generate()
