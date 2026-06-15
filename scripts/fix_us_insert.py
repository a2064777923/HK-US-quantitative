#!/usr/bin/env python3
"""Re-insert failed US stocks (after widening name column to varchar(500))"""
import requests, subprocess

def db(sql):
    r = subprocess.run(
        ["docker", "exec", "quantmind-db", "psql", "-U", "quantmind", "-d", "quantmind", "-t", "-A", "-c", sql],
        capture_output=True, text=True, timeout=30
    )
    return r.stdout.strip()

# Get existing US stocks
existing = set(db("SELECT symbol FROM stocks WHERE exchange IN ('NASDAQ','NYSE')").split("\n"))
print(f"Existing US: {len(existing)}")

total_new = 0
for exchange in ["NASDAQ", "NYSE"]:
    url = f"https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&exchange={exchange}"
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    rows = r.json().get("data", {}).get("table", {}).get("rows", [])
    
    batch_values = []
    for item in rows:
        symbol = item.get("symbol", "").strip().replace(".", "-")
        name = item.get("name", "").strip().replace("'", "''")[:499]
        if not symbol or symbol in existing:
            continue
        batch_values.append(f"('{symbol}', '{name}', '{exchange}', 'USD', true, NOW(), NOW())")
        if len(batch_values) >= 50:
            sql = (
                "INSERT INTO stocks (symbol, name, exchange, currency, is_active, created_at, updated_at) VALUES "
                + ",".join(batch_values)
                + " ON CONFLICT (symbol) DO UPDATE SET name=EXCLUDED.name, is_active=true, updated_at=NOW()"
            )
            db(sql)
            total_new += len(batch_values)
            batch_values = []
    if batch_values:
        sql = (
            "INSERT INTO stocks (symbol, name, exchange, currency, is_active, created_at, updated_at) VALUES "
            + ",".join(batch_values)
            + " ON CONFLICT (symbol) DO UPDATE SET name=EXCLUDED.name, is_active=true, updated_at=NOW()"
        )
        db(sql)
        total_new += len(batch_values)
    print(f"{exchange}: processed {len(rows)}, new = {total_new}")

# Verify SPCX
spcx = db("SELECT symbol, name, exchange FROM stocks WHERE symbol='SPCX'")
print(f"\nSPCX: {spcx}")

# Final count
us_count = db("SELECT count(*) FROM stocks WHERE is_active=true AND exchange IN ('NASDAQ','NYSE')")
hk_count = db("SELECT count(*) FROM stocks WHERE is_active=true AND exchange='HKEX'")
print(f"Final: 港股={hk_count}, 美股={us_count}")
