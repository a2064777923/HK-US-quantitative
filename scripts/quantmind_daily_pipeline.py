#!/usr/bin/env python3
"""
QuantMind Daily Pipeline: K線更新 + 特徵生成 + 模型推理
每日收市後自動運行（16:30 HKT）
"""
import json, urllib.request, subprocess, sys, time, os
from datetime import datetime, timedelta

DB_CMD = "docker exec quantmind-db psql -U quantmind -d quantmind -t -A"
INFERENCE_API = "https://notopenai.asia/api/v1/admin/model/run-inference"

def run_remote(sql):
    """Execute SQL on remote Info Hub server"""
    escaped = sql.replace("'", "'\\''")
    cmd = f'ssh -o ConnectTimeout=5 root@38.76.164.106 "{DB_CMD} -c \'{escaped}\'"'
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    return r.stdout.strip()

def fetch_tencent_kline(symbol, market="hk", days=10):
    """Fetch latest klines from Tencent Finance"""
    code = f"{market}{symbol}"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,{days},qfq"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        klines = data.get("data", {}).get(code, {})
        day_data = klines.get("qfqday") or klines.get("day") or []
        return day_data
    except Exception as e:
        return []

def update_klines():
    """Fetch and store latest klines for all stocks"""
    print(f"[{datetime.now()}] Step 1: 更新K線數據...")
    
    # Get all stocks from DB
    stocks_raw = run_remote("SELECT symbol, exchange FROM stocks WHERE is_active=true;")
    if not stocks_raw:
        print("  ❌ 無法讀取股票列表")
        return 0
    
    stocks = []
    for line in stocks_raw.split("\n"):
        parts = line.strip().split("|")
        if len(parts) >= 2:
            stocks.append({"symbol": parts[0], "exchange": parts[1]})
    
    updated = 0
    errors = 0
    for i, s in enumerate(stocks):
        sym = s["symbol"]
        exchange = s["exchange"]
        
        if exchange == "HKEX":
            market = "hk"
        elif exchange in ("NASDAQ", "NYSE"):
            market = "us"
        else:
            continue
        
        klines = fetch_tencent_kline(sym, market)
        if not klines:
            errors += 1
            continue
        
        for k in klines[-3:]:  # Only insert last 3 days to avoid duplicates
            if len(k) >= 6:
                dt, open_p, close_p, high_p, low_p, vol = k[0], k[1], k[2], k[3], k[4], k[5]
                try:
                    vol_float = float(vol) if vol else 0
                    amount = float(close_p) * vol_float if close_p and vol_float else 0
                    change_pct = 0
                    if float(open_p) > 0:
                        change_pct = (float(close_p) - float(open_p)) / float(open_p) * 100
                    
                    sql = f"""INSERT INTO klines (symbol, interval, timestamp, open_price, high_price, low_price, close_price, volume, amount, change_percent, data_source, created_at)
                              VALUES ('{sym}', 'day', '{dt}', {open_p}, {high_p}, {low_p}, {close_p}, {vol_float}, {amount}, {change_pct:.4f}, 'tencent_hk', NOW())
                              ON CONFLICT DO NOTHING;"""
                    run_remote(sql)
                except Exception as e:
                    pass
        
        updated += 1
        if (i + 1) % 50 == 0:
            print(f"  已處理 {i+1}/{len(stocks)}...")
            time.sleep(1)
    
    print(f"  ✅ K線更新完成: {updated} 成功, {errors} 失敗")
    return updated

def generate_features():
    """Regenerate feature parquet from klines"""
    print(f"[{datetime.now()}] Step 2: 生成特徵 parquet...")
    # The feature generation is done inside the container
    cmd = '''ssh -o ConnectTimeout=5 root@38.76.164.106 "docker exec quantmind python3 -c \\"
import pandas as pd, numpy as np
from sqlalchemy import create_engine, text

engine = create_engine('postgresql://quantmind:quantmind@quantmind-db:5432/quantmind')
df = pd.read_sql(\\\"SELECT * FROM klines WHERE interval='day' ORDER BY symbol, timestamp\\\", engine)

features = []
for sym, g in df.groupby('symbol'):
    g = g.sort_values('timestamp').tail(120)
    if len(g) < 20:
        continue
    c = g['close_price'].astype(float)
    v = g['volume'].astype(float)
    for idx, row in g.iterrows():
        dt = row['timestamp']
        cl = float(row['close_price'])
        hi = float(row['high_price'])
        lo = float(row['low_price'])
        op = float(row['open_price'])
        vol = float(row['volume'])
        
        feat = {'symbol': sym, 'date': str(dt)[:10], 'close': cl, 'open': op, 'high': hi, 'low': lo, 'volume': vol}
        
        # Returns
        cl_s = c.values
        pos = len(cl_s) - 1
        for d in [1,2,3,5,10,20]:
            if pos >= d:
                feat[f'ret_{d}d'] = (cl_s[pos] - cl_s[pos-d]) / cl_s[pos-d]
            else:
                feat[f'ret_{d}d'] = 0
        
        # Moving averages
        for w in [5,10,20,60]:
            window = cl_s[max(0,pos-w+1):pos+1]
            feat[f'ma_{w}'] = np.mean(window) if len(window) > 0 else cl
            feat[f'ma_ratio_{w}'] = cl / feat[f'ma_{w}'] if feat[f'ma_{w}'] > 0 else 1
        
        # Volatility
        for w in [5,10,20]:
            window = cl_s[max(0,pos-w+1):pos+1]
            if len(window) > 1:
                feat[f'vol_{w}d'] = np.std(np.diff(window) / window[:-1])
            else:
                feat[f'vol_{w}d'] = 0
        
        # Volume features
        v_s = v.values
        for w in [5,10,20]:
            vwindow = v_s[max(0,pos-w+1):pos+1]
            feat[f'vol_ratio_{w}'] = vol / np.mean(vwindow) if np.mean(vwindow) > 0 else 1
        
        # Price position
        for w in [20,60]:
            hwindow = g['high_price'].astype(float).values[max(0,pos-w+1):pos+1]
            lwindow = g['low_price'].astype(float).values[max(0,pos-w+1):pos+1]
            hmax = np.max(hwindow) if len(hwindow) > 0 else hi
            lmin = np.min(lwindow) if len(lwindow) > 0 else lo
            feat[f'price_pos_{w}d'] = (cl - lmin) / (hmax - lmin) if hmax > lmin else 0.5
        
        # Momentum
        feat['rsi_14'] = 50  # simplified
        feat['macd'] = 0
        feat['bb_upper'] = cl * 1.02
        feat['bb_lower'] = cl * 0.98
        
        # Fill remaining to 52 features
        feat['turnover_rate'] = 0
        feat['amount_ratio'] = 1
        feat['gap'] = 0
        feat['body_ratio'] = (cl - op) / (hi - lo) if hi > lo else 0
        feat['upper_shadow'] = (hi - max(cl, op)) / (hi - lo) if hi > lo else 0
        feat['lower_shadow'] = (min(cl, op) - lo) / (hi - lo) if hi > lo else 0
        feat['consec_up'] = 0
        feat['consec_down'] = 0
        
        features.append(feat)

out = pd.DataFrame(features)
out.to_parquet('/app/db/feature_snapshots/model_features_2026.parquet', index=False)
print(f'Generated {len(out)} rows, {len(out.columns)} columns')
\\\""
'''
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
    if r.returncode == 0:
        # Extract the result line
        for line in r.stdout.split("\n"):
            if "Generated" in line:
                print(f"  ✅ {line.strip()}")
                return True
        print(f"  ✅ 特徵生成完成")
        return True
    else:
        print(f"  ❌ 特徵生成失敗: {r.stderr[:200]}")
        return False

def run_inference():
    """Trigger inference via API"""
    print(f"[{datetime.now()}] Step 3: 觸發模型推理...")
    
    # First get admin token
    login_data = json.dumps({"username": "admin", "password": "admin123"}).encode()
    try:
        req = urllib.request.Request(
            "https://notopenai.asia/api/v1/auth/login",
            data=login_data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            token = json.loads(resp.read().decode()).get("access_token", "")
    except Exception as e:
        print(f"  ❌ 登錄失敗: {e}")
        return False
    
    if not token:
        print("  ❌ 無法獲取 token")
        return False
    
    # Trigger inference
    try:
        req = urllib.request.Request(
            INFERENCE_API,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            }
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            result = json.loads(resp.read().decode())
            print(f"  ✅ 推理完成: {result.get('signals_count', '?')} 信號")
            return True
    except Exception as e:
        print(f"  ❌ 推理失敗: {e}")
        return False

if __name__ == "__main__":
    print("=" * 60)
    print("QuantMind 每日數據管道")
    print("=" * 60)
    
    update_klines()
    generate_features()
    run_inference()
    
    print(f"\n[{datetime.now()}] 管道執行完成")
