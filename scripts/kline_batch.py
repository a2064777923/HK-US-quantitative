#!/usr/bin/env python3
"""
K線批量更新 — 每隻股票一次SQL寫入所有K線（唔再逐條INSERT）
"""
import subprocess, json, time, urllib.request, tempfile, os
from datetime import datetime

def db(sql):
    r = subprocess.run(
        ['docker', 'exec', 'quantmind-db', 'psql', '-U', 'quantmind', '-d', 'quantmind', '-t', '-A', '-c', sql],
        capture_output=True, text=True, timeout=30
    )
    return r.stdout.strip()

def db_batch(sql_file):
    """Execute SQL file inside container"""
    # Copy file to container then execute
    r = subprocess.run(
        ['docker', 'cp', sql_file, 'quantmind-db:/tmp/batch.sql'],
        capture_output=True, timeout=10
    )
    r2 = subprocess.run(
        ['docker', 'exec', 'quantmind-db', 'psql', '-U', 'quantmind', '-d', 'quantmind', '-f', '/tmp/batch.sql'],
        capture_output=True, text=True, timeout=60
    )
    return r2.returncode

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def fetch_kline(code, market="hk", count=2000):
    param = f"{market}{code},day,,,{count},qfq"
    url = f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param={param}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://finance.qq.com"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        if data.get("code") != 0: return None
        key = f"{market}{code}"
        sd = data.get("data",{}).get(key,{})
        return sd.get("qfqday") or sd.get("day") or []
    except:
        return None

def build_sql_inserts(symbol, klines, source):
    """Build batch INSERT SQL for all klines of one stock"""
    rows = []
    for k in klines:
        if len(k) < 6: continue
        dt = k[0]
        try:
            o, c, h, l = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            v = float(k[5]) if k[5] else 0
            a = c * v
            chg = ((c - o) / o * 100) if o > 0 else 0
            rows.append(f"('{symbol}','day','{dt}',{o},{h},{l},{c},{v},{a},{chg:.4f},'{source}',NOW())")
        except:
            continue
    if not rows: return None
    values = ",".join(rows)
    return f"""INSERT INTO klines (symbol,interval,timestamp,open_price,high_price,low_price,close_price,volume,amount,change_percent,data_source,created_at)
               VALUES {values} ON CONFLICT DO NOTHING;"""

def process_batch(symbols, market, source):
    """Process a batch of symbols, writing SQL file per 10 stocks"""
    ok, fail = 0, 0
    sql_buf = []
    batch_size = 10
    
    for i, sym in enumerate(symbols):
        klines = fetch_kline(sym, market)
        if not klines:
            fail += 1
            continue
        
        sql = build_sql_inserts(sym, klines, source)
        if sql:
            sql_buf.append(sql)
        ok += 1
        
        # Write batch every N stocks
        if len(sql_buf) >= batch_size:
            _flush_batch(sql_buf)
            sql_buf = []
        
        if (i+1) % 50 == 0:
            log(f"  進度: {i+1}/{len(symbols)} (ok={ok} fail={fail})")
        time.sleep(0.2)
    
    # Flush remaining
    if sql_buf:
        _flush_batch(sql_buf)
    
    return ok, fail

def _flush_batch(sql_buf):
    """Write SQL buffer to temp file and execute"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as f:
        f.write("BEGIN;\n")
        for s in sql_buf:
            f.write(s + "\n")
        f.write("COMMIT;\n")
        tmp = f.name
    try:
        db_batch(tmp)
    finally:
        os.unlink(tmp)

def main():
    log("=" * 50)
    log("K線批量更新")
    
    # 港股
    syms = db("SELECT symbol FROM stocks WHERE is_active=true AND exchange='HKEX' ORDER BY symbol")
    hk_list = [s.strip() for s in syms.split('\n') if s.strip()]
    log(f"港股: {len(hk_list)} 隻")
    hk_ok, hk_fail = process_batch(hk_list, "hk", "tencent")
    log(f"港股完成: {hk_ok} ok, {hk_fail} fail")
    
    # 美股
    syms = db("SELECT symbol FROM stocks WHERE is_active=true AND exchange IN ('NASDAQ','NYSE') ORDER BY symbol")
    us_list = [s.strip() for s in syms.split('\n') if s.strip()]
    log(f"美股: {len(us_list)} 隻")
    
    us_ok, us_fail = 0, 0
    for sym in us_list:
        for suffix in [".OQ", ".N", ""]:
            klines = fetch_kline(f"{sym}{suffix}", "us")
            if klines:
                sql = build_sql_inserts(sym, klines, "tencent_us")
                if sql:
                    _flush_batch([sql])
                us_ok += 1
                break
        else:
            us_fail += 1
        time.sleep(0.3)
    
    log(f"美股完成: {us_ok} ok, {us_fail} fail")
    
    total = db("SELECT count(DISTINCT symbol) FROM klines WHERE interval='day'")
    latest = db("SELECT max(timestamp) FROM klines WHERE interval='day'")
    log(f"=== 總計: {total} 隻, 最新: {latest} ===")

if __name__ == '__main__':
    main()
