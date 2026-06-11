#!/usr/bin/env python3
"""
Portfolio Price Updater with Currency Conversion
Updates all position prices and converts USD to HKD
"""
import subprocess
import json
import re
import urllib.request
from datetime import datetime

# Exchange rates (update periodically)
USD_TO_HKD = 7.80

def db(sql):
    r = subprocess.run(
        ['docker', 'exec', 'quantmind-db', 'psql', '-U', 'quantmind', '-d', 'quantmind', '-t', '-A', '-c', sql],
        capture_output=True, text=True, timeout=30
    )
    return r.stdout.strip()

def get_hk_price(symbol):
    """Get HK stock price from Sina"""
    try:
        url = f'http://qt.gtimg.cn/q=hk{symbol}'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        txt = urllib.request.urlopen(req, timeout=5).read().decode('gbk', 'ignore')
        parts = txt.split('~')
        if len(parts) > 3 and parts[3]:
            return float(parts[3])
    except:
        pass
    return None

def get_us_price(symbol):
    """Get US stock price from Sina"""
    try:
        url = f'https://hq.sinajs.cn/list=gb_{symbol.lower()}'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://finance.sina.com.cn'
        })
        txt = urllib.request.urlopen(req, timeout=5).read().decode('gb2312', 'ignore')
        m = re.search(r'"([^"]*)"', txt)
        if m:
            parts = m.group(1).split(',')
            if parts[0]:
                return float(parts[0])  # Current price is first field
    except:
        pass
    return None

def update_positions():
    print(f"[{datetime.now()}] Updating position prices...")
    
    # Get all active positions
    positions = db("""
        SELECT p.id, p.symbol, s.exchange, s.currency, p.quantity
        FROM positions p
        LEFT JOIN stocks s ON p.symbol = s.symbol
        WHERE p.quantity > 0
    """)
    
    if not positions:
        print("No positions found")
        return
    
    updated = 0
    for line in positions.split('\n'):
        if not line:
            continue
        parts = line.split('|')
        if len(parts) < 5:
            continue
        
        pos_id, symbol, exchange, currency, quantity = parts
        quantity = int(quantity)
        
        # Get current price
        if exchange == 'HKEX':
            price = get_hk_price(symbol)
        else:
            price = get_us_price(symbol)
        
        if price is None:
            print(f"  ⚠️ Cannot get price for {symbol}")
            continue
        
        # Convert to HKD if USD
        if currency == 'USD':
            price_hkd = price * USD_TO_HKD
        else:
            price_hkd = price
        
        # Update position
        market_value = quantity * price_hkd
        db(f"""
            UPDATE positions 
            SET current_price = {price_hkd:.4f},
                market_value = {market_value:.2f},
                unrealized_pnl = {market_value:.2f} - total_cost,
                unrealized_pnl_rate = CASE 
                    WHEN total_cost > 0 THEN ({market_value:.2f} - total_cost) / total_cost 
                    ELSE 0 
                END,
                updated_at = NOW()
            WHERE id = {pos_id}
        """)
        
        currency_label = f" (${price:.2f} USD)" if currency == 'USD' else ""
        print(f"  ✅ {symbol}: HKD {price_hkd:.2f}{currency_label}")
        updated += 1
    
    # Update portfolio totals
    db("""
        UPDATE portfolios 
        SET 
            current_capital = (SELECT COALESCE(SUM(market_value), 0) FROM positions WHERE portfolio_id = 3 AND quantity > 0) + available_cash,
            total_value = (SELECT COALESCE(SUM(market_value), 0) FROM positions WHERE portfolio_id = 3 AND quantity > 0) + available_cash,
            total_pnl = (SELECT COALESCE(SUM(market_value), 0) FROM positions WHERE portfolio_id = 3 AND quantity > 0) + available_cash - initial_capital,
            total_return = CASE 
                WHEN initial_capital > 0 THEN ((SELECT COALESCE(SUM(market_value), 0) FROM positions WHERE portfolio_id = 3 AND quantity > 0) + available_cash - initial_capital) / initial_capital
                ELSE 0
            END,
            updated_at = NOW()
        WHERE id = 3
    """)
    
    # Get updated portfolio value
    result = db("SELECT total_value, total_pnl, total_return FROM portfolios WHERE id = 3")
    if result:
        val, pnl, ret = result.split('|')
        print(f"\n📊 Portfolio Updated:")
        print(f"  Total Value: HKD {float(val):,.2f}")
        print(f"  Total P&L: HKD {float(pnl):,.2f}")
        print(f"  Return: {float(ret)*100:.2f}%")
    
    print(f"\n✅ Updated {updated} positions")

if __name__ == '__main__':
    update_positions()
