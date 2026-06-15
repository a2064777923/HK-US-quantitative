#!/usr/bin/env python3
"""
expand_universe_v2.py - 全量擴充港股+美股股票池
數據源:
  - 港股: HKEX 官方 ListOfSecurities.xlsx (最權威)
  - 美股: NASDAQ API (NASDAQ + NYSE)
去重策略: ON CONFLICT DO UPDATE (idempotent)
"""
import subprocess, time, io, json, sys
from datetime import datetime

def db(sql, timeout=30):
    r = subprocess.run(
        ["docker", "exec", "quantmind-db", "psql", "-U", "quantmind", "-d", "quantmind", "-t", "-A", "-c", sql],
        capture_output=True, text=True, timeout=timeout
    )
    if r.returncode != 0:
        print(f"  DB ERROR: {r.stderr[:200]}", flush=True)
    return r.stdout.strip()

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def retry(fn, max_attempts=3, delay=5, desc=""):
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            log(f"  {desc} attempt {attempt}/{max_attempts} failed: {e}")
            if attempt < max_attempts:
                wait = delay * attempt
                log(f"  Retrying in {wait}s...")
                time.sleep(wait)
    log(f"  {desc} all {max_attempts} attempts failed!")
    return None

def expand_hk():
    """用 HKEX 官方 xlsx 擴充港股"""
    import requests, openpyxl
    
    log("=== 獲取 HKEX 官方港股列表 ===")
    url = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
    
    def fetch():
        r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.content
    
    content = retry(fetch, max_attempts=5, delay=10, desc="HKEX下載")
    if not content:
        log("HKEX 下載失敗，跳過")
        return 0
    
    wb = openpyxl.load_workbook(io.BytesIO(content))
    ws = wb.active
    
    added = 0
    skipped = 0
    for row in ws.iter_rows(min_row=4, values_only=True):
        code = str(row[0]).strip() if row[0] else ""
        name = str(row[1]).strip().replace("'", "''") if row[1] else ""
        category = str(row[2]).strip() if row[2] else ""
        currency = str(row[16]).strip() if row[16] else "HKD"
        
        # 只要股票（Equity），跳過ETF/牛熊證/債券/權證
        if "Equity" not in category:
            skipped += 1
            continue
        
        if not code or len(code) != 5:
            skipped += 1
            continue
        
        if currency not in ("HKD", "CNY", "USD"):
            currency = "HKD"
        
        sql = (
            f"INSERT INTO stocks (symbol, name, exchange, currency, is_active, created_at, updated_at) "
            f"VALUES ('{code}', '{name}', 'HKEX', '{currency}', true, NOW(), NOW()) "
            f"ON CONFLICT (symbol) DO UPDATE SET name='{name}', is_active=true, "
            f"exchange='HKEX', currency='{currency}', updated_at=NOW()"
        )
        db(sql)
        added += 1
    
    log(f"港股: 寫入 {added} 隻, 跳過 {skipped} (非Equity)")
    return added

def expand_us():
    """用 NASDAQ API 擴充美股 (NASDAQ + NYSE)"""
    import requests
    
    log("=== 獲取美股列表 (NASDAQ + NYSE) ===")
    total_added = 0
    
    for exchange in ["NASDAQ", "NYSE"]:
        log(f"  拉取 {exchange}...")
        
        def fetch(ex=exchange):
            url = f"https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&exchange={ex}"
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.json()
        
        data = retry(fetch, max_attempts=5, delay=10, desc=f"{exchange} API")
        if not data:
            log(f"  {exchange} 獲取失敗，跳過")
            continue
        
        rows = data.get("data", {}).get("table", {}).get("rows", [])
        log(f"  {exchange}: 收到 {len(rows)} 隻")
        
        added = 0
        for item in rows:
            symbol = item.get("symbol", "").strip()
            name = item.get("name", "").strip().replace("'", "''")
            
            if not symbol:
                continue
            
            # Handle BRK.B -> BRK-B format
            symbol = symbol.replace(".", "-")
            
            sql = (
                f"INSERT INTO stocks (symbol, name, exchange, currency, is_active, created_at, updated_at) "
                f"VALUES ('{symbol}', '{name}', '{exchange}', 'USD', true, NOW(), NOW()) "
                f"ON CONFLICT (symbol) DO UPDATE SET name='{name}', is_active=true, "
                f"exchange='{exchange}', updated_at=NOW()"
            )
            db(sql)
            added += 1
        
        total_added += added
        log(f"  {exchange}: 寫入 {added} 隻")
    
    return total_added

def main():
    log("=" * 60)
    log("股票池全量擴充 v2")
    log("=" * 60)
    
    # 先睇下現狀
    before_hk = db("SELECT count(*) FROM stocks WHERE is_active=true AND exchange='HKEX'")
    before_us = db("SELECT count(*) FROM stocks WHERE is_active=true AND exchange IN ('NASDAQ','NYSE')")
    log(f"擴充前: 港股={before_hk}, 美股={before_us}")
    
    # 擴充港股
    hk_added = expand_hk()
    
    # 擴充美股
    us_added = expand_us()
    
    # 總結
    after_hk = db("SELECT count(*) FROM stocks WHERE is_active=true AND exchange='HKEX'")
    after_us = db("SELECT count(*) FROM stocks WHERE is_active=true AND exchange IN ('NASDAQ','NYSE')")
    after_total = db("SELECT count(*) FROM stocks WHERE is_active=true")
    
    log("=" * 60)
    log(f"擴充完成!")
    log(f"  港股: {before_hk} -> {after_hk} (+{int(after_hk)-int(before_hk)})")
    log(f"  美股: {before_us} -> {after_us} (+{int(after_us)-int(before_us)})")
    log(f"  總計: {after_total}")
    log("=" * 60)

if __name__ == "__main__":
    main()
