#!/usr/bin/env python3
"""
修復版：用騰訊API更新持倉價格（唔再依賴被封嘅Sina）
"""
import subprocess, json, urllib.request, re
from datetime import datetime

USD_TO_HKD = 7.80

def db(sql):
    r = subprocess.run(
        ['docker', 'exec', 'quantmind-db', 'psql', '-U', 'quantmind', '-d', 'quantmind', '-t', '-A', '-c', sql],
        capture_output=True, text=True, timeout=30
    )
    return r.stdout.strip()

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def fetch_tencent_price(code, market="hk"):
    """騰訊API獲取即時報價"""
    param = f"{market}{code},day,,,1,qfq"
    url = f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param={param}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://finance.qq.com"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        key = f"{market}{code}"
        sd = data.get("data",{}).get(key,{})
        klines = sd.get("qfqday") or sd.get("day") or []
        if klines and len(klines[-1]) >= 3:
            return float(klines[-1][2])  # close price
    except:
        pass
    return None

def fetch_us_price(symbol):
    """美股：嘗試多個suffix"""
    for suffix in [".OQ", ".N", ""]:
        px = fetch_tencent_price(f"{symbol}{suffix}", "us")
        if px:
            return px
    return None

def update_redis_prices():
    """更新Redis中嘅模擬盤價格"""
    # Get positions from Redis
    raw = db("SELECT symbol, quantity, avg_cost, exchange FROM positions WHERE portfolio_id=8 AND status='active'")
    if not raw:
        # Try to get from trades
        log("Positions表為空，從trades重建...")
        trades = db("SELECT symbol, side, price, quantity FROM sim_trades WHERE portfolio_id=8 ORDER BY created_at")
        if not trades:
            log("無交易記錄")
            return
        
        # Build positions from trades
        pos = {}
        for line in trades.split('\n'):
            if not line.strip(): continue
            p = line.split('|')
            sym, side, price, qty = p[0], p[1], float(p[2]), int(p[3])
            if sym not in pos:
                pos[sym] = {'qty': 0, 'cost': 0}
            if side == 'buy':
                old_cost = pos[sym]['cost'] * pos[sym]['qty']
                pos[sym]['qty'] += qty
                pos[sym]['cost'] = (old_cost + price * qty) / pos[sym]['qty']
            elif side == 'sell':
                pos[sym]['qty'] -= qty
        
        positions = pos
    else:
        positions = {}
        for line in raw.split('\n'):
            if not line.strip(): continue
            p = line.split('|')
            positions[p[0]] = {'qty': int(p[1]), 'cost': float(p[2]), 'exchange': p[3] if len(p)>3 else ''}
    
    log(f"更新 {len(positions)} 隻持倉價格...")
    
    for sym, pos in positions.items():
        if pos['qty'] <= 0:
            continue
        
        # Determine market
        if sym[0].isdigit() and len(sym) == 5:
            px = fetch_tencent_price(sym, "hk")
        else:
            px = fetch_us_price(sym)
        
        if px and px > 0:
            cost = pos['cost']
            pnl_rate = (px / cost - 1) if cost > 0 else 0
            market_value = pos['qty'] * px
            unrealized_pnl = (px - cost) * pos['qty']
            
            log(f"  {sym}: ${px:.2f} (cost=${cost:.2f} pnl={pnl_rate*100:+.1f}%)")
            
            # Update via sim_trader API or direct Redis
            # Direct Redis update
            redis_update = f"""
import redis, json
r = redis.Redis(host='redis', port=6379, decode_responses=True)
key = 'simulation:account:default:10000003'
data = json.loads(r.get(key) or '{{}}')
if 'positions' in data and '{sym}' in data['positions']:
    data['positions']['{sym}']['last_price'] = {px}
    data['positions']['{sym}']['current_price'] = {px}
    r.set(key, json.dumps(data))
    print('Updated {sym} = {px}')
else:
    print('{sym} not found in Redis')
"""
            # Write to temp file and execute in Redis container
            db(f"SELECT redis_command('SET', 'quantmind:price:{sym}', '{px}')")
        else:
            log(f"  {sym}: 無法獲取報價")
    
    log("價格更新完成")

if __name__ == '__main__':
    update_redis_prices()
