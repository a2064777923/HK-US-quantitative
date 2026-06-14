#!/usr/bin/env python3
"""
Feishu Notification Helper
Send trading signals and order notifications to Feishu
"""
import json
import os
import urllib.request
from datetime import datetime

FEISHU_ENV_FILE = os.environ.get("FEISHU_ENV_FILE", "/root/.quantmind_env")
FEISHU_REQUIRED_KEYS = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID")

_token_cache = {"token": None, "expires": 0}


def strip_env_value(value):
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1].strip()
    return text


def env_values_from_file(path):
    values = {}
    if not path or not os.path.exists(path):
        return values
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in FEISHU_REQUIRED_KEYS:
            parsed = strip_env_value(value)
            if parsed:
                values[key] = parsed
    return values


def feishu_config():
    file_values = env_values_from_file(os.environ.get("FEISHU_ENV_FILE") or FEISHU_ENV_FILE)
    return {
        key: os.environ.get(key) or file_values.get(key) or ""
        for key in FEISHU_REQUIRED_KEYS
    }


def get_tenant_token():
    """Get Feishu tenant access token"""
    import time
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires"]:
        return _token_cache["token"]

    config = feishu_config()
    if not config["FEISHU_APP_ID"] or not config["FEISHU_APP_SECRET"]:
        print("[FEISHU] Missing FEISHU_APP_ID or FEISHU_APP_SECRET")
        return None

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": config["FEISHU_APP_ID"], "app_secret": config["FEISHU_APP_SECRET"]}).encode()
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
    config = feishu_config()
    token = get_tenant_token()
    if not token:
        print("[FEISHU] No token, skipping")
        return False

    chat_id = chat_id or config["FEISHU_CHAT_ID"]
    if not chat_id:
        print("[FEISHU] Missing FEISHU_CHAT_ID")
        return False
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
