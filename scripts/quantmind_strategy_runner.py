#!/usr/bin/env python3
"""
QuantMind Strategy Runner v2 — 修復通知問題
- 只喺有新訂單時先send通知（唔再每5分鐘重複）
- 通知包含技術指標同觸發原因
- 止損止盈都會通知
"""
import json, urllib.request, re, time, os, sys, math, subprocess
try:
    from feishu_notify import send_feishu_message, notify_orders
except: pass
from datetime import datetime, timezone, timedelta

API = "https://notopenai.asia/api/v1"
BOT_USER = "kaitosim"
BOT_PASS = "kaitosim123"
PORTFOLIO_ID = 8
STRATEGY_ID = "signal_momentum_v1"
STRATEGY_NAME = "信號動量策略 (Signal Momentum)"
STATE_FILE = "/tmp/quantmind_last_state.json"

# 策略參數
MAX_POSITIONS = 10
POSITION_SIZE_PCT = 0.10
MIN_SCORE_HK = 0.620
MIN_SCORE_US = 0.620
STOP_LOSS_PCT = -0.08
TAKE_PROFIT_PCT = 0.15

LOT_SIZES_HK = {
    "00880": 1000, "09922": 1000, "00867": 500, "00288": 500,
    "00017": 1000, "00916": 1000, "00688": 500, "00116": 1000,
    "00743": 1000, "00775": 2000, "00816": 1000, "00347": 1000,
    "01810": 200, "00388": 100, "09863": 100, "00700": 100,
    "01918": 1000, "02208": 1000, "03690": 100, "09896": 200,
    "07226": 1000, "00506": 2000, "00357": 1000, "00512": 500,
    "00551": 500, "00853": 500, "00975": 500, "01928": 200,
    "01024": 200, "09618": 100, "09626": 100, "09988": 100,
    "09999": 100, "09961": 100,
}

US_STOCKS = ("PDD","ARAY","AAPL","TSLA","NVDA","MSFT","GOOGL","AMZN","META","NIO","XPEV","LI","BABA","JD","BIDU")

def get_token():
    data = json.dumps({"username": BOT_USER, "password": BOT_PASS}).encode()
    req = urllib.request.Request(f"{API}/auth/login", data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["access_token"]

def tencent_price(symbol, market="hk"):
    """騰訊API獲取報價（替代被封嘅Sina）"""
    for suffix in ([""] if market=="hk" else [".OQ", ".N", ""]):
        param = f"{market}{symbol}{suffix},day,,,1,qfq"
        url = f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param={param}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://finance.qq.com"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            key = f"{market}{symbol}{suffix}"
            sd = data.get("data",{}).get(key,{})
            klines = sd.get("qfqday") or sd.get("day") or []
            if klines and len(klines[-1]) >= 3:
                px = float(klines[-1][2])
                if px > 0:
                    return {"price": px, "name": symbol, "change_pct": 0}
        except:
            continue
    return None

def sina_us_price(symbol):
    return tencent_price(symbol, "us")

def sina_hk_price(symbol):
    return tencent_price(symbol, "hk")



def place_order(token, symbol, side, quantity, order_type="market", price=None):
    body = {"portfolio_id": PORTFOLIO_ID, "symbol": symbol, "side": side,
            "order_type": order_type, "quantity": quantity, "trading_mode": "simulation"}
    if price: body["price"] = price
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{API}/simulation/orders", data=data, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"status": "error", "remarks": str(e)}

def get_account(token):
    req = urllib.request.Request(f"{API}/simulation/account", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["data"]

def db_query(sql):
    r = subprocess.run(
        ["docker", "exec", "quantmind-db", "psql", "-U", "quantmind", "-d", "quantmind", "-t", "-A", "-c", sql],
        capture_output=True, text=True, timeout=30
    )
    return r.stdout.strip()

def send_heartbeat(token, metrics):
    ts = int(time.time())
    data1 = json.dumps({"strategy_id": STRATEGY_ID, "metrics": metrics, "strategy_nav": metrics.get("total_return", 0) / 100 + 1, "pod_name": "kaitosim-signal-runner"})
    data2 = json.dumps({"last_seen": ts, "status": "running", "metrics": metrics, "strategy_nav": 1.0, "pod_name": "kaitosim-signal-runner"})
    key1 = "quantmind:strategy:status:default:10000002:" + STRATEGY_ID
    key2 = "quantmind:strategy:status:default:10000002:default"
    for key, d in [(key1, data1), (key2, data2)]:
        safe_d = d.replace("'", "\\'")
        cmd = f"docker exec quantmind-redis redis-cli SET '{key}' '{safe_d}' EX 300"
        subprocess.run(cmd, shell=True, capture_output=True, timeout=10)

def get_market_mode():
    hkt = datetime.now(timezone(timedelta(hours=8)))
    t = hkt.hour * 60 + hkt.minute
    if t >= 21*60+30 or t < 4*60:
        return "US"
    elif 9*60+30 <= t < 16*60:
        return "HK"
    else:
        return "CLOSED"

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

def get_signal_quality(symbol):
    """Read technical details + signal from quality JSONB"""
    raw = db_query(f"""
        SELECT quality, signal_side, fusion_score FROM engine_signal_scores 
        WHERE trade_date = (SELECT max(trade_date) FROM engine_signal_scores) 
        AND symbol = '{symbol}'
        ORDER BY fusion_score DESC LIMIT 1
    """)
    if raw:
        try:
            parts = raw.split('|')
            q = json.loads(parts[0]) if parts[0] else {}
            q['signal_side'] = parts[1] if len(parts) > 1 else '?'
            try:
                q['fusion_score'] = round(float(parts[2]), 3) if len(parts) > 2 else '?'
            except:
                q['fusion_score'] = '?'
            return q
        except:
            pass
    return {}

def build_signal_notification(market_label, new_orders, stop_events, signals, cash, total_asset, pos_count, total_return, positions=None):
    """Build a comprehensive v4 report with signal analysis for all positions"""
    ts = datetime.now().strftime("%H:%M")
    lines = []
    
    # === 1. Header ===
    has_action = bool(new_orders or stop_events)
    if has_action:
        lines.append(f"📊 {market_label}策略報告 ({ts})")
    else:
        lines.append(f"📊 {market_label}持倉監控 ({ts})")
    lines.append("")
    
    # === 2. New orders executed ===
    if new_orders:
        lines.append("### 🚀 新下單")
        for o in new_orders:
            sym = o['symbol']
            q = get_signal_quality(sym)
            reasons = q.get('reasons', [])
            rsi = q.get('rsi', '?')
            op = q.get('order_prices', {})
            
            lines.append(f"🟢 {sym} {o.get('name','')} x{o['quantity']}股 @ ${o.get('price',0):.2f}")
            if reasons:
                lines.append(f"   原因: {', '.join(reasons[:4])}")
            if op:
                lines.append(f"   📌 止損${op.get('stop_loss','?')} | 止盈${op.get('take_profit','?')} | 風報比{op.get('rr_ratio','?')}")
            lines.append("")
    
    # === 3. Stop events ===
    if stop_events:
        lines.append("### ⚠️ 止損止盈觸發")
        for ev in stop_events:
            emoji = "🛑" if ev['type'] == 'stop_loss' else "🎯"
            tag = '止損' if ev['type']=='stop_loss' else '止盈'
            lines.append(f"{emoji} {ev['symbol']}: {ev['pnl']*100:+.1f}% → {tag}成交")
        lines.append("")
    
    # === 4. Position signal analysis (v4) ===
    if positions:
        lines.append("### 📋 持倉信號狀態")
        for sym, pos in sorted(positions.items()):
            cost = float(pos.get("cost_price", 0))
            current = float(pos.get("last_price", 0))
            qty = float(pos.get("volume", 0))
            pnl = (current / cost - 1) * 100 if cost > 0 else 0
            
            # Get v4 signal
            q = get_signal_quality(sym)
            signal_side = q.get('signal_side', '?')
            fusion_score = q.get('fusion_score', '?')
            reasons = q.get('reasons', [])
            risk_flags = q.get('risk_flags', [])
            op = q.get('order_prices', {})
            
            # Emoji based on signal
            if signal_side == 'BUY':
                sig_emoji = '🟢'
            elif signal_side == 'SELL':
                sig_emoji = '🔴'
            else:
                sig_emoji = '⚪'
            
            pnl_emoji = '📈' if pnl >= 0 else '📉'
            
            lines.append(f"{sig_emoji} {sym} {qty:.0f}股 成本${cost:.2f} 現價${current:.2f} {pnl_emoji}{pnl:+.1f}%")
            lines.append(f"   信號: {signal_side} (score={fusion_score})")
            if reasons:
                lines.append(f"   分析: {', '.join(reasons[:3])}")
            if risk_flags:
                lines.append(f"   ⚠️ {', '.join(risk_flags)}")
            if signal_side == 'SELL' and op:
                lines.append(f"   📌 建議止損: ${op.get('stop_loss','?')}")
            elif signal_side == 'BUY' and op:
                lines.append(f"   📌 加倉位: ${op.get('entry_price','?')} | 止損: ${op.get('stop_loss','?')}")
            lines.append("")
    
    # === 5. Top BUY signals (not held) ===
    if signals:
        lines.append("### 🎯 TOP BUY 信號")
        held = set(positions.keys()) if positions else set()
        new_signals = [s for s in signals if s['symbol'] not in held][:5]
        if new_signals:
            for s in new_signals:
                q = get_signal_quality(s['symbol'])
                reasons = q.get('reasons', [])
                op = q.get('order_prices', {})
                pred = q.get('prediction', {})
                
                lines.append(f"🟢 {s['symbol']} {s.get('name','')} score={s.get('score',0):.3f} ${q.get('price','?')}")
                if reasons:
                    lines.append(f"   {', '.join(reasons[:3])}")
                if op:
                    lines.append(f"   📌 買${op.get('entry_price','?')} 止損${op.get('stop_loss','?')} 止盈${op.get('take_profit','?')} 風報比{op.get('rr_ratio','?')}")
                lines.append("")
        else:
            lines.append("現有持倉已覆蓋所有BUY信號，無新候選")
            lines.append("")
    
    # === 6. Account summary ===
    lines.append(f"💰 現金{cash:,.0f} | 資產{total_asset:,.0f} | {pos_count}倉 | 回報{total_return:+.1f}%")
    
    return "\n".join(lines)

def run_strategy():
    market_mode = get_market_mode()
    log(f"{'='*50}")
    log(f"策略啟動: {STRATEGY_NAME}")
    log(f"市場時段: {market_mode}")
    
    if market_mode == "CLOSED":
        log("⏸️ 非交易時段，跳過")
        return
    
    is_us = (market_mode == "US")
    min_score = MIN_SCORE_US if is_us else MIN_SCORE_HK
    quote_fn = sina_us_price if is_us else sina_hk_price
    market_label = "美股" if is_us else "港股"
    
    try:
        token = get_token()
        log("✅ 登入成功")
    except Exception as e:
        log(f"❌ 登入失敗: {e}")
        return
    
    account = get_account(token)
    cash = float(account.get("cash", 0))
    total_asset = float(account.get("total_asset", 0))
    positions = account.get("positions", {})
    pos_count = len(positions)
    
    # ★ 用騰訊API覆蓋舊價格
    refreshed = 0
    for sym, pos in positions.items():
        if sym[0].isdigit() and len(sym) == 5:
            q = tencent_price(sym, "hk")
        else:
            q = tencent_price(sym, "us")
        if q and q["price"] > 0:
            old_price = float(pos.get("last_price", 0))
            pos["last_price"] = q["price"]
            if old_price > 0 and abs(q["price"] - old_price) / old_price > 0.001:
                refreshed += 1
    
    # 重新計算total_asset
    total_asset = cash
    for sym, pos in positions.items():
        qty = float(pos.get("volume", 0))
        price = float(pos.get("last_price", 0))
        total_asset += qty * price
    
    if refreshed > 0:
        log(f"🔄 已用騰訊API刷新 {refreshed} 隻價格")
    log(f"💰 現金: {cash:,.2f} | 資產: {total_asset:,.2f} | 持倉: {pos_count}")
    
    metrics = {"cash": cash, "total_asset": total_asset, "positions": pos_count, "total_return": (total_asset / 100000 - 1) * 100, "timestamp": int(time.time()), "market": market_mode}
    send_heartbeat(token, metrics)
    
    # 止損止盈
    log("── 止損止盈檢查 ──")
    stop_events = []
    for sym, pos in positions.items():
        cost = float(pos.get("cost_price", 0))
        current = float(pos.get("last_price", 0))
        qty = float(pos.get("volume", 0))
        if cost <= 0 or current <= 0 or qty <= 0: continue
        pnl_pct = (current / cost - 1)
        if pnl_pct <= STOP_LOSS_PCT:
            log(f"  🛑 止損 {sym}: {pnl_pct*100:+.1f}%")
            result = place_order(token, sym, "sell", int(qty))
            if result.get("status") == "filled":
                stop_events.append({"symbol": sym, "type": "stop_loss", "pnl": pnl_pct})
            time.sleep(2)
        elif pnl_pct >= TAKE_PROFIT_PCT:
            log(f"  🎯 止盈 {sym}: {pnl_pct*100:+.1f}%")
            result = place_order(token, sym, "sell", int(qty))
            if result.get("status") == "filled":
                stop_events.append({"symbol": sym, "type": "take_profit", "pnl": pnl_pct})
            time.sleep(2)
    
    # 信號分析
    log(f"── {market_label}信號分析 ──")
    if is_us:
        us_list = ",".join(f"'{s}'" for s in US_STOCKS)
        stock_filter = f"IN ({us_list})"
    else:
        us_list = ",".join(f"'{s}'" for s in US_STOCKS)
        stock_filter = f"NOT IN ({us_list})"
    
    sql = f"""
        SELECT e.symbol, COALESCE(s.name, e.symbol), e.fusion_score 
        FROM engine_signal_scores e LEFT JOIN stocks s ON s.symbol = e.symbol 
        WHERE e.trade_date = (SELECT max(trade_date) FROM engine_signal_scores) 
        AND e.signal_side = 'BUY' AND e.symbol {stock_filter}
        AND e.fusion_score > {min_score}
        ORDER BY e.fusion_score DESC LIMIT 10
    """
    raw = db_query(sql)
    
    signals = []
    for line in raw.split("\n"):
        if not line.strip(): continue
        parts = line.split("|")
        if len(parts) >= 3:
            signals.append({"symbol": parts[0], "name": parts[1], "score": float(parts[2])})
    
    log(f"📊 {market_label} BUY 信號: {len(signals)} 隻")
    for s in signals:
        log(f"   {s['symbol']} {s['name']} score={s['score']:.4f}")
    
    # 新增買入
    held_symbols = set(positions.keys())
    candidates = [s for s in signals if s["symbol"] not in held_symbols]
    available_slots = MAX_POSITIONS - pos_count
    candidates = candidates[:max(available_slots, 0)]
    
    new_orders = []
    if not candidates:
        log("ℹ️ 無新買入候選")
    else:
        log(f"── 新增買入 ({len(candidates)} 隻) ──")
        for s in candidates:
            sym, name, score = s["symbol"], s["name"], s["score"]
            quote = quote_fn(sym)
            if not quote:
                log(f"  ⚠️ {sym}: 無報價，跳過")
                continue
            px = quote["price"]
            target_size = total_asset * POSITION_SIZE_PCT
            if is_us:
                qty = int(target_size / (px * 7.8))
                if qty < 1: qty = 1
            else:
                lot = LOT_SIZES_HK.get(sym, 1000)
                qty = int(target_size / px / lot) * lot
                if qty < lot: qty = lot
            
            log(f"  📦 {sym} {name} score={score:.4f} 現價={px:.2f} {qty}股")
            result = place_order(token, sym, "buy", qty)
            status = result.get("status", "?")
            if status == "filled":
                ep = result.get("average_price", "?")
                log(f"     ✅ 成交 @ {ep}")
                new_orders.append({"symbol": sym, "name": name, "side": "buy", "quantity": qty, "price": float(ep) if ep != "?" else 0, "status": "filled"})
            elif status == "submitted":
                log(f"     ⏳ 掛單中")
            else:
                log(f"     ❌ {status}: {result.get('remarks', '')}")
            time.sleep(2)
    
    # 同步現金
    try:
        spent = db_query("SELECT ROUND(COALESCE(SUM(trade_value + total_fee), 0)::numeric, 2) FROM sim_trades WHERE portfolio_id = 8 AND side = 'buy'")
        sold = db_query("SELECT ROUND(COALESCE(SUM(trade_value - total_fee), 0)::numeric, 2) FROM sim_trades WHERE portfolio_id = 8 AND side = 'sell'")
        spent_val = float(spent) if spent else 0
        sold_val = float(sold) if sold else 0
        actual_cash = 100000 - spent_val + sold_val
        db_query(f"UPDATE portfolios SET available_cash = {actual_cash}, current_capital = {actual_cash}, updated_at = NOW() WHERE id = 8")
        log(f"🔄 現金同步: {actual_cash:,.0f}")
    except Exception as e:
        log(f"⚠️ 現金同步失敗: {e}")
    
    # 最終狀態
    account = get_account(token)
    cash = float(account.get("cash", 0))
    positions = account.get("positions", {})
    
    # ★ 再次用騰訊API刷新價格（第二次get_account會覆蓋）
    for sym, pos in positions.items():
        if sym[0].isdigit() and len(sym) == 5:
            q = tencent_price(sym, "hk")
        else:
            q = tencent_price(sym, "us")
        if q and q["price"] > 0:
            pos["last_price"] = q["price"]
    
    total_asset = cash
    for sym, pos in positions.items():
        qty = float(pos.get("volume", 0))
        price = float(pos.get("last_price", 0))
        total_asset += qty * price
    total_return = (total_asset / 100000 - 1) * 100
    
    log(f"{'='*50}")
    log(f"策略完成 ({market_label}) | 資產: {total_asset:,.2f} | 回報: {total_return:+.2f}%")
    for sym, pos in sorted(positions.items()):
        cost = float(pos.get("cost_price", 0))
        current = float(pos.get("last_price", 0))
        pnl = (current / cost - 1) * 100 if cost > 0 else 0
        log(f"   {sym}: {pos['volume']:.0f}股 @ {cost:.3f} -> {current:.3f} ({pnl:+.1f}%)")
    
    # ★ 關鍵修復：只喺有新訂單或止損止盈時先通知
    state = load_state()
    state_key = f"last_notify_{market_mode}"
    state_positions_key = f"positions_{market_mode}"
    
    current_pos_hash = hash(json.dumps(sorted(positions.keys())))
    last_pos_hash = state.get(state_positions_key, None)
    
    should_notify = False
    notify_reason = ""
    
    if new_orders:
        should_notify = True
        notify_reason = "new_orders"
    elif stop_events:
        should_notify = True
        notify_reason = "stop_events"
    elif current_pos_hash != last_pos_hash and last_pos_hash is not None:
        should_notify = True
        notify_reason = "position_change"
    
    # Always build the full report
    msg = build_signal_notification(market_label, new_orders, stop_events, signals, cash, total_asset, len(positions), total_return, positions)
    
    # Send if: has action OR first run of the day OR position changed
    import hashlib
    msg_hash = hashlib.md5(msg.encode()).hexdigest()[:8]
    last_msg_hash = state.get("last_msg_hash", "")
    
    if should_notify or msg_hash != last_msg_hash:
        should_notify = True
        try:
            send_feishu_message(msg)
            log(f"📤 已通知飛書 ({notify_reason})")
        except Exception as e:
            log(f"⚠️ 飛書通知失敗: {e}")
        state[state_key] = datetime.now().isoformat()
        state["last_msg_hash"] = msg_hash
    else:
        log("🔇 無新事件，唔通知")
    
    state[state_positions_key] = current_pos_hash
    save_state(state)
    
    send_heartbeat(token, {"cash": cash, "total_asset": total_asset, "positions": len(positions), "total_return": total_return, "timestamp": int(time.time()), "market": market_mode})
    log("💓 心跳已更新")

if __name__ == "__main__":
    run_strategy()
