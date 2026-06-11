#!/usr/bin/env python3
"""
實時信號通知橋接 v2 — SSH到服務器讀alert文件，過濾有操作意義嘅信號，格式化後輸出到stdout
由Hermes cron job每分鐘調用
推送邏輯：
- 只推BUY/SELL信號（唔推WATCH）
- 必須有entry_price/stop_loss/take_profit
- 同時執行模擬倉操作
"""
import subprocess, json, sys

def ssh_cmd(cmd):
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "root@38.76.164.106", cmd],
            capture_output=True, text=True, timeout=10
        )
        return r.stdout.strip()
    except:
        return ""

# 讀alert文件
raw = ssh_cmd("cat /tmp/rt_signal_alert.json 2>/dev/null")
if not raw:
    sys.exit(0)  # 冇信號，靜默退出

try:
    alerts = json.loads(raw)
except:
    sys.exit(0)

if not alerts:
    sys.exit(0)

# 讀已發送記錄
sent_raw = ssh_cmd("cat /tmp/rt_signal_sent.json 2>/dev/null")
try:
    sent = json.loads(sent_raw) if sent_raw else []
except:
    sent = []

sent_keys = {f"{a['symbol']}_{a['trigger']}_{a.get('time','')}" for a in sent}

new_alerts = []
for a in alerts:
    key = f"{a['symbol']}_{a['trigger']}_{a.get('time','')}"
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
    # 必須有入場/止盈/止損
    if not a.get("entry_price") or not a.get("stop_loss") or not a.get("take_profit"):
        continue
    actionable.append(a)

if not actionable:
    # 更新已發送記錄但唔輸出
    ssh_cmd("cp /tmp/rt_signal_alert.json /tmp/rt_signal_sent.json")
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
    lines.append(f"└─ 當前：${a['price']:.2f} ({a.get('change_pct',0):+.1f}%) | {a['time']}")
    lines.append("")

# 執行模擬倉操作
sim_result = ssh_cmd("cd /root && python3 quantmind_sim_trader.py 2>&1 | tail -5")
if sim_result:
    lines.append(f"📊 **模擬倉執行結果：**")
    lines.append(sim_result)

print("\n".join(lines))

# 更新已發送記錄
ssh_cmd("cp /tmp/rt_signal_alert.json /tmp/rt_signal_sent.json")
