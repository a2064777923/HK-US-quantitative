#!/usr/bin/env python3
"""
實時信號通知橋接 v2 — SSH到服務器讀alert文件，過濾有操作意義嘅信號，格式化後輸出到stdout
由Hermes cron job每分鐘調用
推送邏輯：
- 只推BUY/SELL信號（唔推WATCH）
- 默認只通知；如要保留舊模擬倉行為，設 RT_ALERT_EXECUTION_MODE=legacy-sim
- alert-dry-run 會逐條調用 rt_order_intake.py 做無下單評估
- alert-sim 會逐條調用 rt_order_intake.py execute 模式
- 必須有entry_price/stop_loss/take_profit
"""
import subprocess, json, sys, os, shlex

REMOTE_HOST = os.environ.get("RT_ALERT_REMOTE", "root@38.76.164.106")
ALERT_FILE = os.environ.get("RT_ALERT_FILE", "/tmp/rt_signal_alert.json")
ALERT_QUEUE_FILE = os.environ.get("RT_ALERT_QUEUE_FILE", "/tmp/rt_signal_alerts.jsonl")
SENT_FILE = os.environ.get("RT_ALERT_SENT_FILE", "/tmp/rt_signal_sent.json")
EXECUTION_MODE = os.environ.get("RT_ALERT_EXECUTION_MODE", "notify").lower()
REQUIRE_CONFIRMED = os.environ.get("RT_ALERT_REQUIRE_CONFIRMED", "1") != "0"

def ssh_cmd(cmd):
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", REMOTE_HOST, cmd],
            capture_output=True, text=True, timeout=10
        )
        return r.stdout.strip()
    except:
        return ""

def load_alerts():
    """優先讀append-only JSONL queue，兼容舊版latest JSON文件。"""
    queue_raw = ssh_cmd(f"tail -n 500 {shlex.quote(ALERT_QUEUE_FILE)} 2>/dev/null")
    alerts = []
    for line in queue_raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            alerts.append(json.loads(line))
        except:
            continue
    if alerts:
        return alerts

    raw = ssh_cmd(f"cat {shlex.quote(ALERT_FILE)} 2>/dev/null")
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, list) else []
    except:
        return []

def alert_key(alert):
    return alert.get("signal_id") or f"{alert.get('symbol')}_{alert.get('trigger')}_{alert.get('time','')}"

def write_sent(alerts):
    payload = json.dumps(alerts[-1000:], ensure_ascii=False)
    ssh_cmd(f"printf %s {shlex.quote(payload)} > {shlex.quote(SENT_FILE)}")

def fmt_number(value):
    try:
        return f"{float(value):.0f}"
    except:
        return str(value)

def summarize_intake(raw):
    try:
        payload = json.loads(raw)
    except:
        return raw.strip()
    lines = []
    for result in payload.get("results", []):
        status = result.get("status", "?")
        sid = result.get("signal_id", "?")
        plan = result.get("plan") or {}
        reasons = result.get("reasons") or []
        hermes = result.get("hermes") or {}
        if plan:
            lines.append(
                f"{status}: {plan.get('side','?').upper()} {plan.get('symbol','?')} "
                f"x{plan.get('quantity','?')} notional_hkd={fmt_number(plan.get('notional_hkd','?'))} "
                f"hermes={hermes.get('status','?')} signal={sid}"
            )
        elif reasons:
            lines.append(f"{status}: {sid} reasons={','.join(reasons)} hermes={hermes.get('status','?')}")
        else:
            lines.append(f"{status}: {sid} hermes={hermes.get('status','?')}")
    return "\n".join(lines)

def run_order_intake(alert, mode):
    payload = json.dumps(alert, ensure_ascii=False)
    intake_mode = "execute" if mode == "alert-sim" else "dry-run"
    cmd = (
        f"cd /root || exit 1; [ -f /root/.quantmind_env ] && . /root/.quantmind_env; "
        f"RT_ORDER_EXECUTION_MODE={intake_mode} "
        f"python3 rt_order_intake.py --alert-json {shlex.quote(payload)} 2>&1"
    )
    return ssh_cmd(cmd)

alerts = load_alerts()
if not alerts:
    sys.exit(0)  # 冇信號，靜默退出

# 讀已發送記錄
sent_raw = ssh_cmd(f"cat {shlex.quote(SENT_FILE)} 2>/dev/null")
try:
    sent = json.loads(sent_raw) if sent_raw else []
except:
    sent = []

sent_keys = {alert_key(a) for a in sent}

new_alerts = []
for a in alerts:
    key = alert_key(a)
    if key not in sent_keys:
        new_alerts.append(a)

if not new_alerts:
    sys.exit(0)  # 全部已發送

# 過濾 — 只保留有操作意義嘅信號
actionable = []
for a in new_alerts:
    # 只要BUY/SELL，唔要WATCH
    if a.get("signal_type") not in ("BUY", "SELL"):
        continue
    # 默認只推完整分數確認過的方向信號；舊alert缺字段時按已確認處理
    if REQUIRE_CONFIRMED and not a.get("confirmed", True):
        continue
    # 必須有入場/止盈/止損
    if not a.get("entry_price") or not a.get("stop_loss") or not a.get("take_profit"):
        continue
    actionable.append(a)

if not actionable:
    # 更新已發送記錄但唔輸出
    sent.extend(new_alerts)
    write_sent(sent)
    sys.exit(0)

# 格式化輸出
lines = ["🎯 **實時操作信號**\n"]

for a in actionable:
    icon = "🟢" if a.get("signal_type") == "BUY" else "🔴"
    lines.append(f"{icon} **{a['symbol']}** — {a['signal_type']}")
    lines.append(f"├─ 觸發：{a['trigger']} ({a['detail']})")
    lines.append(f"├─ 入場價：${a['entry_price']}")
    lines.append(f"├─ 止盈：${a['take_profit']}")
    lines.append(f"├─ 止損：${a['stop_loss']}")
    lines.append(f"├─ 風險回報：{a.get('rr_ratio', '?')}")
    lines.append(f"├─ 多因子分：{a.get('full_score', '?')} | 確認：{a.get('confirmed', True)}")
    lines.append(f"└─ 當前：${a['price']:.2f} ({a.get('change_pct',0):+.1f}%) | {a.get('time','')}")
    lines.append("")

if EXECUTION_MODE == "legacy-sim":
    # 保留舊行為：只觸發現有sim_trader，該腳本仍會讀DB信號，不會逐條消費本alert。
    sim_result = ssh_cmd("cd /root && python3 quantmind_sim_trader.py 2>&1 | tail -5")
    if sim_result:
        lines.append(f"📊 **模擬倉執行結果（legacy-sim）：**")
        lines.append(sim_result)
elif EXECUTION_MODE == "notify":
    lines.append("📎 模擬倉：未執行（RT_ALERT_EXECUTION_MODE=notify）")
elif EXECUTION_MODE in ("alert-dry-run", "alert-sim"):
    lines.append(f"📊 **Alert-specific intake（{EXECUTION_MODE}）：**")
    for a in actionable:
        result = run_order_intake(a, EXECUTION_MODE)
        if result:
            lines.append(summarize_intake(result))
        else:
            lines.append(f"{a.get('signal_id', a.get('symbol'))}: intake無輸出")
else:
    lines.append(f"⚠️ 未知執行模式：{EXECUTION_MODE}，已跳過模擬倉操作")

print("\n".join(lines))

# 更新已發送記錄
sent.extend(new_alerts)
write_sent(sent)
