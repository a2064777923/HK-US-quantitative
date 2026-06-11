#!/usr/bin/env python3
"""
QuantMind P5 模擬交易引擎
根據模型推理信號自動下單到 P5 模擬盤
"""
import json, urllib.request, sys, os
from datetime import datetime

API_BASE = "https://notopenai.asia/api/v1"
PORTFOLIO_ID = 5  # 快斗量化模拟盘
MAX_POSITIONS = 10  # 最多持倉數
POSITION_SIZE_PCT = 0.08  # 每倉佔比 8%
MIN_SCORE = 0.04  # 最低 fusion_score 門檻
COMMISSION_RATE = 0.0003  # 佣金 0.03%
STAMP_DUTY_RATE = 0.001  # 印花稅 0.1% (港股賣出)

def get_token():
    login_data = json.dumps({"username": "admin", "password": "admin123"}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/auth/login",
        data=login_data,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode()).get("access_token", "")

def api_get(token, path):
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

def api_post(token, path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

def get_signals(token):
    """Get today's inference signals"""
    try:
        # Try engine signal scores directly from DB
        import subprocess
        cmd = '''ssh -o ConnectTimeout=5 root@38.76.164.106 "docker exec quantmind-db psql -U quantmind -d quantmind -t -A -c \\"SELECT symbol, fusion_score, signal_side, trade_date FROM engine_signal_scores WHERE trade_date = (SELECT max(trade_date) FROM engine_signal_scores) ORDER BY fusion_score DESC LIMIT 30;\\""'''
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        signals = []
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) >= 4:
                signals.append({
                    "symbol": parts[0],
                    "score": float(parts[1]),
                    "side": parts[2],
                    "date": parts[3]
                })
        return signals
    except Exception as e:
        print(f"  ❌ 獲取信號失敗: {e}")
        return []

def get_current_positions(token):
    """Get current P5 positions"""
    try:
        positions = api_get(token, f"/simulation/positions?portfolio_id={PORTFOLIO_ID}")
        return positions if isinstance(positions, list) else []
    except:
        return []

def get_account_info(token):
    """Get P5 account info"""
    try:
        return api_get(token, f"/simulation/account?portfolio_id={PORTFOLIO_ID}")
    except:
        return {}

def submit_order(token, symbol, side, quantity, price=None):
    """Submit a simulation order"""
    order = {
        "portfolio_id": PORTFOLIO_ID,
        "symbol": symbol,
        "side": side,
        "order_type": "market",
        "quantity": quantity,
        "trading_mode": "simulation"
    }
    if price:
        order["price"] = price
    
    try:
        result = api_post(token, "/simulation/orders", order)
        return result
    except Exception as e:
        return {"error": str(e)}

def run():
    print(f"[{datetime.now()}] P5 模擬交易引擎啟動")
    print("=" * 50)
    
    # 1. Get token
    try:
        token = get_token()
        print(f"  ✅ 登錄成功")
    except Exception as e:
        print(f"  ❌ 登錄失敗: {e}")
        return
    
    # 2. Get signals
    signals = get_signals(token)
    buy_signals = [s for s in signals if s["side"] == "BUY" and s["score"] >= MIN_SCORE]
    print(f"  📊 信號: {len(signals)} 總計, {len(buy_signals)} 買入信號 (score>={MIN_SCORE})")
    
    if not buy_signals:
        print("  ⚠️ 無買入信號，跳過")
        return
    
    # 3. Get current account & positions
    account = get_account_info(token)
    positions = get_current_positions(token)
    cash = float(account.get("cash_balance", account.get("available_cash", 100000)))
    total_value = float(account.get("total_value", 100000))
    position_symbols = set()
    
    if isinstance(positions, list):
        position_symbols = {p.get("symbol") for p in positions if p.get("status") == "active"}
    
    print(f"  💰 現金: {cash:,.0f}, 總值: {total_value:,.0f}")
    print(f"  📦 現有持倉: {len(position_symbols)} 隻")
    
    # 4. Calculate target positions
    target_size = total_value * POSITION_SIZE_PCT
    
    # Filter out already-held stocks
    new_buys = [s for s in buy_signals if s["symbol"] not in position_symbols]
    available_slots = MAX_POSITIONS - len(position_symbols)
    
    if available_slots <= 0:
        print(f"  ⚠️ 已滿倉 ({MAX_POSITIONS} 隻)，跳過")
        return
    
    new_buys = new_buys[:available_slots]
    print(f"  🎯 新增買入: {len(new_buys)} 隻")
    
    # 5. Submit orders
    orders_placed = 0
    for signal in new_buys:
        symbol = signal["symbol"]
        score = signal["score"]
        
        # Get latest price from klines
        try:
            import subprocess
            cmd = f'''ssh -o ConnectTimeout=5 root@38.76.164.106 "docker exec quantmind-db psql -U quantmind -d quantmind -t -A -c \\"SELECT close_price FROM klines WHERE symbol='{symbol}' ORDER BY timestamp DESC LIMIT 1;\\""'''
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            price = float(r.stdout.strip())
        except:
            print(f"  ⚠️ {symbol} 無法獲取價格，跳過")
            continue
        
        if price <= 0:
            continue
        
        # Calculate quantity (港股每手)
        quantity = int(target_size / price)
        if quantity <= 0:
            continue
        
        # Submit order
        result = submit_order(token, symbol, "buy", quantity, price)
        
        if "error" in result:
            print(f"  ❌ {symbol} 下單失敗: {result['error'][:80]}")
        else:
            status = result.get("status", "unknown")
            print(f"  ✅ {symbol} 下單成功: {quantity}股 @ {price:.2f} (score={score:.4f}, status={status})")
            orders_placed += 1
    
    print(f"\n{'=' * 50}")
    print(f"  📝 本次下單: {orders_placed} 筆")
    print(f"  💰 剩餘現金估計: {cash - orders_placed * target_size:,.0f}")

if __name__ == "__main__":
    run()
