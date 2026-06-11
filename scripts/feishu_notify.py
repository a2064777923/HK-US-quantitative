#!/usr/bin/env python3
"""
Feishu Notification Helper
Send trading signals and order notifications to Feishu
"""
import json
import urllib.request
import os
from datetime import datetime

# Feishu config - read from environment or use defaults
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "cli_aa9dc590df389cdb")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "0mSI7VevDJ00zmMZz0NZkcGuku3DsFJI")
FEISHU_CHAT_ID = os.environ.get("FEISHU_CHAT_ID", "oc_5558f127a10b24c1322c85c2b236678f")

_token_cache = {"token": None, "expires": 0}

def get_tenant_token():
    """Get Feishu tenant access token"""
    import time
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires"]:
        return _token_cache["token"]
    
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            token = result.get("tenant_access_token")
            expire = result.get("expire", 7200)
            _token_cache["token"] = token
            _token_cache["expires"] = now + expire - 300
            return token
    except Exception as e:
        print(f"[FEISHU] Token error: {e}")
        return None

def send_feishu_message(text, chat_id=None):
    """Send text message to Feishu chat"""
    token = get_tenant_token()
    if not token:
        print("[FEISHU] No token, skipping")
        return False
    
    chat_id = chat_id or FEISHU_CHAT_ID
    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    body = json.dumps({
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text})
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("code") == 0:
                print(f"[FEISHU] Message sent OK")
                return True
            else:
                print(f"[FEISHU] Error: {result}")
                return False
    except Exception as e:
        print(f"[FEISHU] Send error: {e}")
        return False

def notify_signals(signals, account_info=None):
    """Send signal notification"""
    ts = datetime.now().strftime("%H:%M")
    lines = [f"📊 策略信號 ({ts})", ""]
    
    for s in signals[:10]:
        side = "🟢" if s.get("side") == "BUY" else "🔴"
        lines.append(f"{side} {s['symbol']} {s.get('name','')} score={s.get('score',0):.3f}")
    
    if account_info:
        lines.append("")
        lines.append(f"💰 現金: HKD {account_info.get('cash',0):,.0f}")
        lines.append(f"📦 持倉: {account_info.get('positions',0)} 隻")
    
    return send_feishu_message("\n".join(lines))

def notify_orders(orders):
    """Send order execution notification"""
    ts = datetime.now().strftime("%H:%M")
    lines = [f"🚀 交易執行 ({ts})", ""]
    
    for o in orders:
        status = "✅" if o.get("status") == "filled" else "❌"
        lines.append(f"{status} {o.get('side','').upper()} {o['symbol']} {o.get('quantity',0)}股 @ ${o.get('price',0):.2f}")
    
    return send_feishu_message("\n".join(lines))

if __name__ == "__main__":
    # Test
    send_feishu_message("🧪 測試通知 - 策略引擎連接正常")
