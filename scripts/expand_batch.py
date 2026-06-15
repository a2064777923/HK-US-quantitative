#!/usr/bin/env python3
"""Batch insert HK equities from pre-parsed JSON + US from NASDAQ API"""
import json, subprocess, time, requests
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
    log(f"  {desc} all attempts failed!")
    return None

def insert_batch(rows):
    """Insert a batch of (symbol, name, exchange, currency) tuples"""
    if not rows:
        return 0
    values = []
    for sym, name, exch, cur in rows:
        name_esc = name.replace("'", "''")
        values.append(f"('{sym}', '{name_esc}', '{exch}', '{cur}', true, NOW(), NOW())")
    sql = (
        "INSERT INTO stocks (symbol, name, exchange, currency, is_active, created_at, updated_at) VALUES "
        + ",".join(values)
        + " ON CONFLICT (symbol) DO UPDATE SET name=EXCLUDED.name, is_active=true, "
        + "exchange=EXCLUDED.exchange, currency=EXCLUDED.currency, updated_at=NOW()"
    )
    r = db(sql, timeout=60)
    return len(rows)

def main():
    log("=" * 60)
    log("股票池全量擴充 v2 (batch)")
    log("=" * 60)

    before = {}
    before["hk"] = db("SELECT count(*) FROM stocks WHERE is_active=true AND exchange='HKEX'")
    before["us"] = db("SELECT count(*) FROM stocks WHERE is_active=true AND exchange IN ('NASDAQ','NYSE')")
    log(f"擴充前: 港股={before['hk']}, 美股={before['us']}")

    # === HK ===
    log("=== 寫入港股 (HKEX) ===")
    start = time.time()
    with open("/tmp/hk_equities.json") as f:
        equities = json.load(f)
    log(f"  港股候選: {len(equities)}")

    batch_size = 50
    hk_inserted = 0
    for i in range(0, len(equities), batch_size):
        batch = [(code, name, "HKEX", cur) for code, name, cur in equities[i:i+batch_size]]
        hk_inserted += insert_batch(batch)
        if (i // batch_size) % 10 == 0:
            log(f"  進度: {hk_inserted}/{len(equities)}")

    log(f"  港股完成: {hk_inserted} 隻, 耗時 {time.time()-start:.1f}s")

    # === US ===
    log("=== 寫入美股 (NASDAQ + NYSE) ===")
    start = time.time()
    us_inserted = 0

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

        batch = []
        for item in rows:
            symbol = item.get("symbol", "").strip()
            name = item.get("name", "").strip().replace("'", "''")
            if not symbol:
                continue
            symbol = symbol.replace(".", "-")
            batch.append((symbol, name, exchange, "USD"))
            if len(batch) >= 50:
                us_inserted += insert_batch(batch)
                batch = []
        if batch:
            us_inserted += insert_batch(batch)
        log(f"  {exchange}: 寫入完成")

    log(f"  美股完成: {us_inserted} 隻, 耗時 {time.time()-start:.1f}s")

    # === Summary ===
    after_hk = db("SELECT count(*) FROM stocks WHERE is_active=true AND exchange='HKEX'")
    after_us = db("SELECT count(*) FROM stocks WHERE is_active=true AND exchange IN ('NASDAQ','NYSE')")
    after_total = db("SELECT count(*) FROM stocks WHERE is_active=true")

    log("=" * 60)
    log(f"擴充完成!")
    log(f"  港股: {before['hk']} -> {after_hk} (+{int(after_hk)-int(before['hk'])})")
    log(f"  美股: {before['us']} -> {after_us} (+{int(after_us)-int(before['us'])})")
    log(f"  總計: {after_total}")
    log("=" * 60)

if __name__ == "__main__":
    main()
