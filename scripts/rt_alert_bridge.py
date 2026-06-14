#!/usr/bin/env python3
"""Realtime v5 alert notification bridge for Hermes/operator review."""
import json
import os
import shlex
import subprocess
import sys


REMOTE_HOST = os.environ.get("RT_ALERT_REMOTE", "root@38.76.164.106")
ALERT_FILE = os.environ.get("RT_ALERT_FILE", "/tmp/rt_signal_alert.json")
ALERT_QUEUE_FILE = os.environ.get("RT_ALERT_QUEUE_FILE", "/tmp/rt_signal_alerts.jsonl")
SENT_FILE = os.environ.get("RT_ALERT_SENT_FILE", "/tmp/rt_signal_sent.json")
EXECUTION_MODE = os.environ.get("RT_ALERT_EXECUTION_MODE", "notify").lower()
REQUIRE_CONFIRMED = os.environ.get("RT_ALERT_REQUIRE_CONFIRMED", "1") != "0"

PASSTHROUGH_ENV_KEYS = (
    "RT_ORDER_EXECUTE_PILOT_ENABLED",
    "RT_ORDER_PILOT_MAX_ORDER_NOTIONAL_HKD",
    "RT_ORDER_PILOT_MAX_ORDER_RISK_HKD",
    "RT_ORDER_PILOT_MAX_DAILY_SUBMITTED_ORDERS",
    "RT_ORDER_PILOT_ALLOWED_MARKETS",
    "RT_ORDER_US_BROKER",
    "ALPACA_TRADING_BASE_URL",
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
    "ALPACA_API_KEY_ID",
    "ALPACA_API_SECRET_KEY",
)


def is_local_mode(remote_host=None):
    return str(remote_host or REMOTE_HOST).strip().lower() in ("local", "localhost", "127.0.0.1")


def run_cmd(cmd, timeout=15):
    if is_local_mode():
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return result.stdout.strip()
        except Exception:
            return ""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", REMOTE_HOST, cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def read_text_file(path):
    if is_local_mode():
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""
    return run_cmd(f"cat {shlex.quote(path)} 2>/dev/null")


def write_text_file(path, text):
    if is_local_mode():
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        return ""
    return run_cmd(f"printf %s {shlex.quote(text)} > {shlex.quote(path)}")


def load_alerts():
    """Prefer append-only JSONL queue; keep latest JSON file fallback."""
    queue_raw = ""
    if is_local_mode():
        try:
            with open(ALERT_QUEUE_FILE, encoding="utf-8") as f:
                queue_raw = "".join(f.readlines()[-500:])
        except Exception:
            queue_raw = ""
    else:
        queue_raw = run_cmd(f"tail -n 500 {shlex.quote(ALERT_QUEUE_FILE)} 2>/dev/null")

    alerts = []
    for line in queue_raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            alerts.append(item)
    if alerts:
        return alerts

    raw = read_text_file(ALERT_FILE)
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            return [loaded]
        return loaded if isinstance(loaded, list) else []
    except Exception:
        return []


def alert_key(alert):
    return alert.get("signal_id") or f"{alert.get('symbol')}_{alert.get('trigger')}_{alert.get('time','')}"


def write_sent(alerts):
    payload = json.dumps(alerts[-1000:], ensure_ascii=False)
    write_text_file(SENT_FILE, payload)


def fmt_number(value):
    try:
        return f"{float(value):.0f}"
    except Exception:
        return str(value)


def summarize_intake(raw):
    try:
        payload = json.loads(raw)
    except Exception:
        return raw.strip()
    lines = []
    for result in payload.get("results", []):
        status = result.get("status", "?")
        sid = result.get("signal_id", "?")
        plan = result.get("plan") or {}
        reasons = result.get("reasons") or []
        hermes = result.get("hermes") or {}
        backend = result.get("order_backend")
        backend_text = f" backend={backend}" if backend else ""
        if plan:
            lines.append(
                f"{status}: {plan.get('side','?').upper()} {plan.get('symbol','?')} "
                f"x{plan.get('quantity','?')} notional_hkd={fmt_number(plan.get('notional_hkd','?'))} "
                f"hermes={hermes.get('status','?')}{backend_text} signal={sid}"
            )
        elif reasons:
            lines.append(f"{status}: {sid} reasons={','.join(reasons)} hermes={hermes.get('status','?')}{backend_text}")
        else:
            lines.append(f"{status}: {sid} hermes={hermes.get('status','?')}{backend_text}")
    return "\n".join(lines)


def env_assignments(keys):
    assignments = []
    for key in keys:
        value = os.environ.get(key)
        if value:
            assignments.append(f"{key}={shlex.quote(value)}")
    return " ".join(assignments)


def run_order_intake(alert, mode):
    payload = json.dumps(alert, ensure_ascii=False)
    intake_mode = "execute" if mode == "alert-sim" else "dry-run"
    passthrough = env_assignments(PASSTHROUGH_ENV_KEYS)
    prefix = f"{passthrough} " if passthrough else ""
    cmd = (
        f"cd /root || exit 1; [ -f /root/.quantmind_env ] && . /root/.quantmind_env; "
        f"{prefix}RT_ORDER_EXECUTION_MODE={intake_mode} "
        f"python3 rt_order_intake.py --alert-json {shlex.quote(payload)} 2>&1"
    )
    return run_cmd(cmd)


def actionable_alerts(alerts):
    rows = []
    for alert in alerts:
        if alert.get("signal_type") not in ("BUY", "SELL"):
            continue
        if REQUIRE_CONFIRMED and not alert.get("confirmed", True):
            continue
        if not alert.get("entry_price") or not alert.get("stop_loss") or not alert.get("take_profit"):
            continue
        rows.append(alert)
    return rows


def build_output(actionable, execution_mode):
    lines = ["🎯 **實時操作信號**\n"]
    for alert in actionable:
        icon = "🟢" if alert.get("signal_type") == "BUY" else "🔴"
        lines.append(f"{icon} **{alert['symbol']}** — {alert['signal_type']}")
        lines.append(f"├─ 觸發：{alert['trigger']} ({alert['detail']})")
        lines.append(f"├─ 入場價：${alert['entry_price']}")
        lines.append(f"├─ 止盈：${alert['take_profit']}")
        lines.append(f"├─ 止損：${alert['stop_loss']}")
        lines.append(f"├─ 風險回報：{alert.get('rr_ratio', '?')}")
        lines.append(f"├─ 多因子分：{alert.get('full_score', '?')} | 確認：{alert.get('confirmed', True)}")
        lines.append(
            f"└─ 當前：${float(alert.get('price', alert['entry_price'])):.2f} "
            f"({alert.get('change_pct', 0):+.1f}%) | {alert.get('time','')}"
        )
        lines.append("")

    if execution_mode == "legacy-sim":
        sim_result = run_cmd("cd /root && python3 quantmind_sim_trader.py 2>&1 | tail -5")
        if sim_result:
            lines.append("📊 **模擬倉執行結果（legacy-sim）：**")
            lines.append(sim_result)
    elif execution_mode == "notify":
        lines.append("📎 模擬倉：未執行（RT_ALERT_EXECUTION_MODE=notify）")
    elif execution_mode in ("alert-dry-run", "alert-sim"):
        lines.append(f"📊 **Alert-specific intake（{execution_mode}）：**")
        for alert in actionable:
            result = run_order_intake(alert, execution_mode)
            lines.append(summarize_intake(result) if result else f"{alert.get('signal_id', alert.get('symbol'))}: intake無輸出")
    else:
        lines.append(f"⚠️ 未知執行模式：{execution_mode}，已跳過模擬倉操作")
    return "\n".join(lines)


def main():
    alerts = load_alerts()
    if not alerts:
        return 0

    sent_raw = read_text_file(SENT_FILE)
    try:
        sent = json.loads(sent_raw) if sent_raw else []
    except Exception:
        sent = []
    if not isinstance(sent, list):
        sent = []

    sent_keys = {alert_key(alert) for alert in sent if isinstance(alert, dict)}
    new_alerts = [alert for alert in alerts if alert_key(alert) not in sent_keys]
    if not new_alerts:
        return 0

    actionable = actionable_alerts(new_alerts)
    if not actionable:
        sent.extend(new_alerts)
        write_sent(sent)
        return 0

    print(build_output(actionable, EXECUTION_MODE))
    sent.extend(new_alerts)
    write_sent(sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
