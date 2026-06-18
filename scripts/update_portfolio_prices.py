#!/usr/bin/env python3
"""Refresh portfolio position price snapshots.

HK holdings prefer Tencent realtime quotes. US holdings prefer Sina realtime
quotes, then fall back to Tencent daily-kline snapshots if realtime is
unavailable. The script updates valuation fields only.
"""
import os, subprocess, json, re, urllib.request
from datetime import datetime

USD_TO_HKD = float(os.environ.get("USD_TO_HKD", "7.80"))
PORTFOLIO_ID = int(os.environ.get("QM_PRICE_UPDATE_PORTFOLIO_ID", os.environ.get("QM_PORTFOLIO_ID", "8")))
_COLUMN_CACHE = {}

def user_portfolio_ids():
    ids = set()
    for name in ("QM_HOLDINGS_PORTFOLIO_ID", "QM_USER_PORTFOLIO_ID", "QM_USER_PORTFOLIO_IDS"):
        raw = os.environ.get(name, "")
        for item in str(raw).split(","):
            item = item.strip()
            if item.isdigit():
                ids.add(int(item))
    return ids or {3}

def position_status_sql(alias=None):
    prefix = f"{alias}." if alias else ""
    if int(PORTFOLIO_ID) in user_portfolio_ids():
        return f"{prefix}status = 'holding'"
    return f"{prefix}status IN ('active','holding')"

def is_user_portfolio():
    return int(PORTFOLIO_ID) in user_portfolio_ids()

def db(sql):
    r = subprocess.run(
        ['docker', 'exec', 'quantmind-db', 'psql', '-U', 'quantmind', '-d', 'quantmind', '-t', '-A', '-c', sql],
        capture_output=True, text=True, timeout=30
    )
    return r.stdout.strip()

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def sql_quote(value):
    return str(value).replace("'", "''")

def table_columns(table):
    if table in _COLUMN_CACHE:
        return _COLUMN_CACHE[table]
    raw = db(f"""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = '{sql_quote(table)}'
    """)
    cols = {line.strip() for line in raw.splitlines() if line.strip()}
    _COLUMN_CACHE[table] = cols
    return cols

def is_hk_symbol(symbol):
    return symbol[:1].isdigit() and len(symbol) == 5

def value_hkd(symbol, qty, price):
    value = qty * price
    return value if is_hk_symbol(symbol) else value * USD_TO_HKD

def update_position_snapshot(symbol, price, pos):
    cols = table_columns("positions")
    qty = float(pos.get("qty", 0))
    cost = float(pos.get("cost", 0))
    market_value = value_hkd(symbol, qty, price)
    unrealized_pnl = value_hkd(symbol, qty, price - cost)
    total_cost = value_hkd(symbol, qty, cost)
    unrealized_pnl_rate = unrealized_pnl / total_cost if total_cost > 0 else 0

    sets = []
    if "current_price" in cols:
        sets.append(f"current_price = {price}")
    if "last_price" in cols:
        sets.append(f"last_price = {price}")
    if "market_value" in cols:
        sets.append(f"market_value = {market_value}")
    if "unrealized_pnl" in cols:
        sets.append(f"unrealized_pnl = {unrealized_pnl}")
    if "unrealized_pnl_rate" in cols:
        sets.append(f"unrealized_pnl_rate = {unrealized_pnl_rate}")
    if "updated_at" in cols:
        sets.append("updated_at = NOW()")
    if not sets:
        return

    db(f"""
        UPDATE positions
        SET {', '.join(sets)}
        WHERE portfolio_id = {PORTFOLIO_ID}
        AND symbol = '{sql_quote(symbol)}'
        AND {position_status_sql()}
    """)

def update_portfolio_totals():
    portfolio_cols = table_columns("portfolios")
    if "current_capital" not in portfolio_cols and "total_value" not in portfolio_cols:
        return
    raw = db(f"""
        SELECT COALESCE(p.available_cash, 0), COALESCE(SUM(pos.market_value), 0)
        FROM portfolios p
        LEFT JOIN positions pos
          ON pos.portfolio_id = p.id
         AND {position_status_sql(alias='pos')}
        WHERE p.id = {PORTFOLIO_ID}
        GROUP BY p.available_cash
    """)
    if not raw:
        return
    parts = raw.split("|")
    if len(parts) < 2:
        return
    try:
        available_cash = float(parts[0] or 0)
        positions_value = float(parts[1] or 0)
    except ValueError:
        return
    total = available_cash + positions_value
    sets = []
    if "current_capital" in portfolio_cols:
        sets.append(f"current_capital = {total}")
    if "total_value" in portfolio_cols:
        sets.append(f"total_value = {total}")
    if "updated_at" in portfolio_cols:
        sets.append("updated_at = NOW()")
    if sets:
        db(f"UPDATE portfolios SET {', '.join(sets)} WHERE id = {PORTFOLIO_ID}")
        log(f"組合總值更新: cash={available_cash:,.2f} positions={positions_value:,.2f} total={total:,.2f}")

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

def normalize_hk_symbol(symbol):
    text = str(symbol or "").strip().upper()
    return text.zfill(5) if text.isdigit() and len(text) < 5 else text

def fetch_hk_realtime_price(symbol):
    """Tencent realtime quote endpoint used before daily-kline fallback."""
    code = normalize_hk_symbol(symbol)
    req = urllib.request.Request(
        f"http://qt.gtimg.cn/q=hk{code}",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com"},
    )
    try:
        data = urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="ignore")
        match = re.search(r'v_hk(\d+)="(.*)"', data)
        if not match:
            return None
        fields = match.group(2).split("~")
        if len(fields) <= 3:
            return None
        price = float(fields[3]) if fields[3] else 0
        return price if price > 0 else None
    except Exception:
        return None

def sina_code(symbol):
    return "gb_" + str(symbol or "").strip().lower().replace(".", "_").replace("-", "_")

def fetch_us_realtime_price(symbol):
    """Sina realtime US quote endpoint used before daily-kline fallback."""
    code = sina_code(symbol)
    req = urllib.request.Request(
        f"http://hq.sinajs.cn/list={code}",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
    )
    try:
        data = urllib.request.urlopen(req, timeout=10).read().decode("gb18030", errors="ignore")
        match = re.search(r'var hq_str_gb_[a-z0-9_]+="(.*)"', data, re.IGNORECASE)
        if not match:
            return None
        fields = match.group(1).split(",")
        if len(fields) < 2:
            return None
        price = float(fields[1]) if fields[1] else 0
        return price if price > 0 else None
    except Exception:
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
    statuses = db(f"""
        SELECT status, count(*)
        FROM positions
        WHERE portfolio_id = {PORTFOLIO_ID}
        GROUP BY status
        ORDER BY status
    """)
    if statuses:
        log("Positions狀態: " + "; ".join(s.replace("|", "=") for s in statuses.splitlines() if s.strip()))

    raw = db(f"""
        SELECT symbol, quantity, avg_cost, exchange
        FROM positions
        WHERE portfolio_id = {PORTFOLIO_ID}
        AND {position_status_sql()}
        AND quantity > 0
    """)
    if not raw:
        if is_user_portfolio():
            log("用戶持倉positions表為空，按DB唯一真相跳過價格更新；不從sim_trades重建")
            update_portfolio_totals()
            return
        # Try to get from trades
        log("Positions表為空，從trades重建...")
        trades = db(f"SELECT symbol, side, price, quantity FROM sim_trades WHERE portfolio_id={PORTFOLIO_ID} ORDER BY created_at")
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
            try:
                qty = int(float(p[1] or 0))
                cost = float(p[2] or 0)
            except ValueError:
                continue
            positions[p[0]] = {'qty': qty, 'cost': cost, 'exchange': p[3] if len(p)>3 else ''}
    
    log(f"更新 {len(positions)} 隻持倉價格...")
    
    for sym, pos in positions.items():
        if pos['qty'] <= 0:
            continue
        
        # Determine market
        if is_hk_symbol(sym):
            px = fetch_hk_realtime_price(sym) or fetch_tencent_price(sym, "hk")
        else:
            px = fetch_us_realtime_price(sym) or fetch_us_price(sym)
        
        if px and px > 0:
            cost = pos['cost']
            pnl_rate = (px / cost - 1) if cost > 0 else 0
            market_value = value_hkd(sym, pos['qty'], px)
            unrealized_pnl = value_hkd(sym, pos['qty'], px - cost)
            
            log(f"  {sym}: ${px:.2f} (cost=${cost:.2f} pnl={pnl_rate*100:+.1f}% value_hkd={market_value:,.0f})")
            update_position_snapshot(sym, px, pos)
            db(f"SELECT redis_command('SET', 'quantmind:price:{sql_quote(sym)}', '{px}')")
        else:
            log(f"  {sym}: 無法獲取報價")
    update_portfolio_totals()
    log("價格更新完成")

if __name__ == '__main__':
    update_redis_prices()
