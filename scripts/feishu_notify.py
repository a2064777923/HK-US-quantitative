#!/usr/bin/env python3
"""
Feishu Notification Helper
Send trading signals and order notifications to Feishu.
"""
import json
import os
import urllib.request
from datetime import datetime

FEISHU_ENV_FILE = os.environ.get("FEISHU_ENV_FILE", "/root/.quantmind_env")
FEISHU_REQUIRED_KEYS = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID")
REQUIRED_FEISHU_ENV = FEISHU_REQUIRED_KEYS

_token_cache = {"token": None, "expires": 0, "app_id": None}


def strip_env_value(value):
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1].strip()
    return text


def _strip_env_value(value):
    return strip_env_value(value)


def env_values_from_file(path):
    values = {}
    if not path or not os.path.exists(path):
        return values
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as exc:
        print(f"[FEISHU] Env file unreadable: {path}: {exc}")
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


def load_env_file(path=None):
    """Load missing Feishu credentials from a simple KEY=VALUE env file."""
    values = env_values_from_file(path if path is not None else os.environ.get("FEISHU_ENV_FILE") or FEISHU_ENV_FILE)
    loaded = []
    for key, value in values.items():
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def feishu_config(chat_id=None):
    values = env_values_from_file(os.environ.get("FEISHU_ENV_FILE") or FEISHU_ENV_FILE)
    config = {
        key: (os.environ.get(key) or values.get(key) or "").strip()
        for key in FEISHU_REQUIRED_KEYS
    }
    if chat_id:
        config["FEISHU_CHAT_ID"] = str(chat_id).strip()
    return config


def feishu_api_config(chat_id=None):
    config = feishu_config(chat_id=chat_id)
    return {
        "app_id": config["FEISHU_APP_ID"],
        "app_secret": config["FEISHU_APP_SECRET"],
        "chat_id": config["FEISHU_CHAT_ID"],
    }


def missing_config_keys(config):
    if "app_id" in config:
        return [
            key
            for key, value in (
                ("FEISHU_APP_ID", config.get("app_id")),
                ("FEISHU_APP_SECRET", config.get("app_secret")),
                ("FEISHU_CHAT_ID", config.get("chat_id")),
            )
            if not value
        ]
    return [key for key in FEISHU_REQUIRED_KEYS if not config.get(key)]


def get_tenant_token(app_id=None, app_secret=None):
    """Get Feishu tenant access token."""
    import time

    config = feishu_api_config()
    app_id = app_id or config["app_id"]
    app_secret = app_secret or config["app_secret"]
    missing = []
    if not app_id:
        missing.append("FEISHU_APP_ID")
    if not app_secret:
        missing.append("FEISHU_APP_SECRET")
    if missing:
        print("[FEISHU] Missing config: " + ",".join(missing))
        return None

    now = time.time()
    if _token_cache.get("token") and _token_cache.get("app_id") == app_id and now < _token_cache.get("expires", 0):
        return _token_cache["token"]

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            token = result.get("tenant_access_token")
            if not token:
                print(f"[FEISHU] Token missing: {result}")
                return None
            expire = result.get("expire", 7200)
            _token_cache["token"] = token
            _token_cache["expires"] = now + expire - 300
            _token_cache["app_id"] = app_id
            return token
    except Exception as e:
        print(f"[FEISHU] Token error: {e}")
        return None


def send_feishu_message(text, chat_id=None):
    """Send text message to Feishu chat."""
    config = feishu_api_config(chat_id=chat_id)
    missing = missing_config_keys(config)
    if missing:
        print("[FEISHU] Missing config: " + ",".join(missing))
        return False

    token = get_tenant_token(app_id=config["app_id"], app_secret=config["app_secret"])
    if not token:
        print("[FEISHU] No token, skipping")
        return False

    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    body = json.dumps(
        {
            "receive_id": config["chat_id"],
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("code") == 0:
                print("[FEISHU] Message sent OK")
                return True
            print(f"[FEISHU] Error: {result}")
            return False
    except Exception as e:
        print(f"[FEISHU] Send error: {e}")
        return False


def notify_signals(signals, account_info=None):
    """Send signal notification."""
    ts = datetime.now().strftime("%H:%M")
    lines = [f"策略信號 ({ts})", ""]

    for s in signals[:10]:
        side = "BUY" if s.get("side") == "BUY" else "SELL"
        lines.append(f"{side} {s['symbol']} {s.get('name','')} score={s.get('score',0):.3f}")

    if account_info:
        lines.append("")
        lines.append(f"現金: HKD {account_info.get('cash',0):,.0f}")
        lines.append(f"持倉: {account_info.get('positions',0)}")

    return send_feishu_message("\n".join(lines))


def notify_orders(orders):
    """Send order execution notification."""
    ts = datetime.now().strftime("%H:%M")
    lines = [f"交易執行 ({ts})", ""]

    for o in orders:
        status = "OK" if o.get("status") == "filled" else "FAIL"
        lines.append(f"{status} {o.get('side','').upper()} {o['symbol']} {o.get('quantity',0)} @ {o.get('price',0):.2f}")

    return send_feishu_message("\n".join(lines))


if __name__ == "__main__":
    send_feishu_message("test notification - strategy engine connected")
