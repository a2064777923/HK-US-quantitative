#!/usr/bin/env python3
"""
大幅擴充港股+美股池
港股: 用akshare抓全部港股列表
美股: 用akshare抓NASDAQ+NYSE全部
"""
import subprocess, time
from datetime import datetime

def db(sql):
    r = subprocess.run(
        ['docker', 'exec', 'quantmind-db', 'psql', '-U', 'quantmind', '-d', 'quantmind', '-t', '-A', '-c', sql],
        capture_output=True, text=True, timeout=30
    )
    return r.stdout.strip()

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def main():
    import akshare as ak

    # 1. 清理A股（用戶唔需要）
    log("清理A股記錄...")
    db("DELETE FROM klines WHERE symbol IN (SELECT symbol FROM stocks WHERE exchange IN ('SSE','SZSE'))")
    db("DELETE FROM engine_signal_scores WHERE symbol IN (SELECT symbol FROM stocks WHERE exchange IN ('SSE','SZSE'))")
    db("UPDATE stocks SET is_active=false WHERE exchange IN ('SSE','SZSE')")
    a_count = db("SELECT count(*) FROM stocks WHERE is_active=true AND exchange IN ('SSE','SZSE')")
    log(f"A股已停用: {a_count} remaining")

    # 2. 擴充港股
    log("=== 獲取全部港股列表 ===")
    try:
        # akshare: stock_hk_spot_em() - 東方財富港股即時行情（包含所有港股）
        df = ak.stock_hk_spot_em()
        log(f"港股總數: {len(df)}")
        
        added = 0
        for _, row in df.iterrows():
            code = str(row.get('代码', '')).strip()
            name = str(row.get('名称', '')).strip().replace("'", "''")
            if not code or len(code) != 5:
                continue
            # Skip warrants, CBBCs (牛熊證), etc
            if code.startswith(('1', '2', '3', '4', '5', '6', '7', '8', '9')):
                # Valid stock codes start with 0
                if not code.startswith('0'):
                    # Some valid codes start with 1-9 (e.g., ETFs like 2800, 3067)
                    pass
            
            price = float(row.get('最新价', 0) or 0)
            if price <= 0:
                continue
            
            sql = f"""INSERT INTO stocks (symbol, name, exchange, currency, is_active, created_at, updated_at)
                      VALUES ('{code}', '{name}', 'HKEX', 'HKD', true, NOW(), NOW())
                      ON CONFLICT (symbol) DO UPDATE SET name='{name}', is_active=true, updated_at=NOW()"""
            db(sql)
            added += 1
        
        log(f"港股寫入: {added} 隻")
    except Exception as e:
        log(f"港股獲取失敗: {e}")
        import traceback
        traceback.print_exc()

    # 3. 擴充美股
    log("=== 獲取全部美股列表 ===")
    try:
        # NASDAQ
        df_nasdaq = ak.stock_us_spot_em()
        log(f"美股總數: {len(df_nasdaq)}")
        
        added_us = 0
        for _, row in df_nasdaq.iterrows():
            code = str(row.get('代码', '')).strip()
            name = str(row.get('名称', '')).strip().replace("'", "''")
            if not code:
                continue
            
            # Determine exchange from code suffix or field
            exchange = 'NASDAQ'
            if '.N' in code or '.NYSE' in code:
                exchange = 'NYSE'
            elif '.OQ' in code or '.NASDAQ' in code:
                exchange = 'NASDAQ'
            
            # Clean symbol (remove suffix)
            sym = code.split('.')[0] if '.' in code else code
            
            price = float(row.get('最新价', 0) or 0)
            if price <= 0:
                continue
            
            sql = f"""INSERT INTO stocks (symbol, name, exchange, currency, is_active, created_at, updated_at)
                      VALUES ('{sym}', '{name}', '{exchange}', 'USD', true, NOW(), NOW())
                      ON CONFLICT (symbol) DO UPDATE SET name='{name}', is_active=true, updated_at=NOW()"""
            db(sql)
            added_us += 1
        
        log(f"美股寫入: {added_us} 隻")
    except Exception as e:
        log(f"美股獲取失敗: {e}")
        import traceback
        traceback.print_exc()

    # Summary
    total = db("SELECT count(*) FROM stocks WHERE is_active=true")
    hk = db("SELECT count(*) FROM stocks WHERE is_active=true AND exchange='HKEX'")
    us = db("SELECT count(*) FROM stocks WHERE is_active=true AND exchange IN ('NASDAQ','NYSE')")
    log(f"=== 總計: {total} (港股:{hk} 美股:{us}) ===")

if __name__ == '__main__':
    main()
