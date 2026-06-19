#!/usr/bin/env python3
"""Realtime v5 alert notification bridge for Hermes/operator review."""
import json
import os
import shlex
import subprocess
import sys
import time


REMOTE_HOST = os.environ.get("RT_ALERT_REMOTE", "root@38.76.164.106")
ALERT_FILE = os.environ.get("RT_ALERT_FILE", "/tmp/rt_signal_alert.json")
ALERT_QUEUE_FILE = os.environ.get("RT_ALERT_QUEUE_FILE", "/tmp/rt_signal_alerts.jsonl")
SENT_FILE = os.environ.get("RT_ALERT_SENT_FILE", "/tmp/rt_signal_sent.json")
POSITION_REVIEW_SENT_FILE = os.environ.get("RT_POSITION_REVIEW_SENT_FILE", "/tmp/rt_position_review_sent.json")
OPERATOR_ACTION_SENT_FILE = os.environ.get("RT_OPERATOR_ACTION_SENT_FILE", "/tmp/rt_operator_action_sent.json")
HERMES_REVIEW_PACKET_FILE = os.environ.get("HERMES_REVIEW_PACKET_FILE", "/tmp/hermes_signal_review_packet.json")
EXECUTION_MODE = os.environ.get("RT_ALERT_EXECUTION_MODE", "notify").lower()
REQUIRE_CONFIRMED = os.environ.get("RT_ALERT_REQUIRE_CONFIRMED", "1") != "0"
SEND_FEISHU = os.environ.get("RT_ALERT_SEND_FEISHU", "0") == "1"
INCLUDE_PACKET_CONTEXT = os.environ.get("RT_ALERT_INCLUDE_PACKET_CONTEXT", "1") != "0"
INCLUDE_POSITION_REVIEW = os.environ.get("RT_ALERT_INCLUDE_POSITION_REVIEW", "1") != "0"
REQUIRE_PACKET_ELIGIBLE = os.environ.get("RT_ALERT_REQUIRE_PACKET_ELIGIBLE", "1") != "0"
NOTIFY_INELIGIBLE_SIGNALS = os.environ.get("RT_ALERT_NOTIFY_INELIGIBLE_SIGNALS", "0") == "1"
MARK_INELIGIBLE_SENT = os.environ.get("RT_ALERT_MARK_INELIGIBLE_SENT", "1") != "0"
POSITION_REVIEW_URGENCY = {
    item.strip().lower()
    for item in os.environ.get("RT_POSITION_REVIEW_URGENCY", "high,medium").split(",")
    if item.strip()
}
POSITION_REVIEW_ROLES = {
    item.strip().lower()
    for item in os.environ.get("RT_POSITION_REVIEW_ROLES", "user").split(",")
    if item.strip()
}
POSITION_REVIEW_LIMIT = int(os.environ.get("RT_POSITION_REVIEW_LIMIT", "20"))
POSITION_REVIEW_REMINDER_HOURS = float(os.environ.get("RT_POSITION_REVIEW_REMINDER_HOURS", "24"))
OPERATOR_ACTION_PRIORITIES = {
    item.strip().upper()
    for item in os.environ.get("RT_OPERATOR_ACTION_PRIORITIES", "P0").split(",")
    if item.strip()
}
OPERATOR_ACTION_LIMIT = int(os.environ.get("RT_OPERATOR_ACTION_LIMIT", "3"))
OPERATOR_ACTION_REMINDER_HOURS = float(os.environ.get("RT_OPERATOR_ACTION_REMINDER_HOURS", "24"))
OPERATOR_ACTION_SUPPRESSED_IDS = {
    item.strip()
    for item in os.environ.get(
        "RT_OPERATOR_ACTION_SUPPRESSED_IDS",
        "write_high_urgency_position_judgments",
    ).split(",")
    if item.strip()
}
POSITION_REVIEW_NOTICE_CONTRACT_VERSION = "position_review_notice_v2_jsonl_instruction"

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
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_BASE_URL",
)

REQUIRED_INTAKE_GATE_ENV = (
    "RT_ORDER_REQUIRE_EXECUTION_READINESS",
    "RT_ORDER_REQUIRE_STRATEGY_EVIDENCE",
    "RT_ORDER_REQUIRE_HERMES_JUDGMENT",
    "RT_ORDER_REQUIRE_MARKET_CONTEXT",
    "RT_ORDER_REQUIRE_NO_SYMBOL_CONFLICT",
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


def load_json_file(path, default):
    raw = read_text_file(path)
    if not raw:
        return default
    try:
        loaded = json.loads(raw)
        return loaded if loaded is not None else default
    except Exception:
        return default


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


def signal_id(alert):
    return alert.get("signal_id") or alert_key(alert)


def write_sent(alerts):
    payload = json.dumps(alerts[-1000:], ensure_ascii=False)
    write_text_file(SENT_FILE, payload)


def write_position_review_sent(rows):
    payload = json.dumps(rows[-1000:], ensure_ascii=False)
    write_text_file(POSITION_REVIEW_SENT_FILE, payload)


def write_operator_action_sent(rows):
    payload = json.dumps(rows[-1000:], ensure_ascii=False)
    write_text_file(OPERATOR_ACTION_SENT_FILE, payload)


def sent_record_time(record):
    if not isinstance(record, dict):
        return 0.0
    try:
        return float(record.get("sent_at_epoch") or 0)
    except (TypeError, ValueError):
        return 0.0


def position_review_thread_key(item):
    if not isinstance(item, dict):
        return None
    key = item.get("review_thread_key")
    if key:
        return str(key)
    role = item.get("role")
    portfolio_id = item.get("portfolio_id")
    symbol = item.get("symbol")
    if role and portfolio_id is not None and symbol:
        return f"{role}:{portfolio_id}:{symbol}"
    review_id = item.get("review_id")
    if review_id:
        parts = str(review_id).split(":")
        if len(parts) >= 3:
            return ":".join(parts[:3])
    return None


def urgency_rank(value):
    return {"low": 1, "medium": 2, "high": 3}.get(str(value or "").lower(), 0)


def position_action_rank(value):
    return {
        "hold_watch_review": 0,
        "risk_review": 1,
        "take_profit_or_trailing_stop_review": 2,
        "reduce_or_exit_review": 3,
        "exit_review": 4,
    }.get(str(value or ""), 0)


def position_review_escalated(item, sent_record):
    if not isinstance(sent_record, dict):
        return True
    if urgency_rank(item.get("urgency")) > urgency_rank(sent_record.get("urgency")):
        return True
    return position_action_rank(item.get("recommended_action")) > position_action_rank(
        sent_record.get("recommended_action")
    )


def intraday_position_evidence_fingerprint(evidence):
    if not isinstance(evidence, dict) or not evidence:
        return ""
    stable = {
        "action_intent": evidence.get("action_intent"),
        "alignment": evidence.get("alignment"),
        "status": evidence.get("status"),
        "timeframe_alignment": evidence.get("timeframe_alignment"),
        "dominant_direction": evidence.get("dominant_direction"),
        "support_codes": evidence.get("support_codes") if isinstance(evidence.get("support_codes"), list) else [],
        "challenge_codes": evidence.get("challenge_codes") if isinstance(evidence.get("challenge_codes"), list) else [],
        "conflict_codes": evidence.get("conflict_codes") if isinstance(evidence.get("conflict_codes"), list) else [],
        "quality_codes": evidence.get("quality_codes") if isinstance(evidence.get("quality_codes"), list) else [],
        "limit_codes": evidence.get("limit_codes") if isinstance(evidence.get("limit_codes"), list) else [],
    }
    return json.dumps(stable, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def position_review_notice_fingerprint(item):
    if not isinstance(item, dict):
        return ""
    work_item = item.get("position_judgment_work_item") if isinstance(item.get("position_judgment_work_item"), dict) else {}
    if not work_item:
        return ""
    required_output = work_item.get("required_output_fields") if isinstance(work_item.get("required_output_fields"), dict) else {}
    attention = work_item.get("required_attention_codes") if isinstance(work_item.get("required_attention_codes"), list) else []
    context_summary = work_item.get("context_summary") if isinstance(work_item.get("context_summary"), dict) else {}
    intraday_evidence = intraday_position_evidence_fingerprint(context_summary.get("intraday_position_evidence"))
    return "|".join(
        [
            POSITION_REVIEW_NOTICE_CONTRACT_VERSION,
            str(work_item.get("schema") or "").strip(),
            str(required_output.get("schema") or "").strip(),
            str(required_output.get("advisory_only")),
            str(required_output.get("submits_orders")),
            ",".join(str(code) for code in attention[:10]),
            intraday_evidence,
        ]
    )


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


def send_feishu_text(text):
    try:
        from feishu_notify import send_feishu_message

        return bool(send_feishu_message(text))
    except Exception as exc:
        print(f"[FEISHU] bridge delivery failed: {exc}", file=sys.stderr)
        return False


def env_assignments(keys):
    assignments = []
    for key in keys:
        value = os.environ.get(key)
        if value:
            assignments.append(f"{key}={shlex.quote(value)}")
    return " ".join(assignments)


def intake_env_assignments(mode):
    assignments = []
    passthrough = env_assignments(PASSTHROUGH_ENV_KEYS)
    if passthrough:
        assignments.append(passthrough)
    if mode in ("alert-dry-run", "alert-sim"):
        assignments.append(" ".join(f"{key}=1" for key in REQUIRED_INTAKE_GATE_ENV))
    return " ".join(assignments)


def run_order_intake(alert, mode):
    payload = json.dumps(alert, ensure_ascii=False)
    intake_mode = "execute" if mode == "alert-sim" else "dry-run"
    passthrough = intake_env_assignments(mode)
    prefix = f"{passthrough} " if passthrough else ""
    cmd = (
        f"cd /root || exit 1; [ -f /root/.quantmind_env ] && . /root/.quantmind_env; "
        f"{prefix}RT_ORDER_EXECUTION_MODE={intake_mode} "
        f"python3 rt_order_intake.py --alert-json {shlex.quote(payload)} 2>&1"
    )
    return run_cmd(cmd)


def base_actionable_alerts(alerts):
    rows = []
    for alert in alerts:
        if alert.get("signal_type") not in ("BUY", "SELL"):
            continue
        if alert.get("execution_candidate") is not True:
            continue
        if REQUIRE_CONFIRMED and not alert.get("confirmed", True):
            continue
        if not alert.get("entry_price") or not alert.get("stop_loss") or not alert.get("take_profit"):
            continue
        rows.append(alert)
    return rows


def packet_readiness_reasons(packet):
    if not REQUIRE_PACKET_ELIGIBLE:
        return []
    if not isinstance(packet, dict) or packet.get("schema") != "hermes_signal_review_packet_v1":
        return ["hermes_packet_missing_or_invalid"]
    readiness = packet.get("execution_readiness") if isinstance(packet.get("execution_readiness"), dict) else {}
    reasons = []
    if readiness.get("schema") and readiness.get("schema") != "execution_readiness_report_v1":
        reasons.append("execution_readiness_schema_invalid")
    if readiness.get("status") != "READY":
        reasons.append(f"execution_readiness_status_{str(readiness.get('status') or 'missing').lower()}")
    if readiness.get("ready_for_execute") is not True:
        reasons.append("execution_readiness_ready_for_execute_false")
    return reasons


def alert_review_reasons(alert, packet, items_by_signal):
    reasons = packet_readiness_reasons(packet)
    if not REQUIRE_PACKET_ELIGIBLE:
        return reasons
    item = matching_review_item(alert, items_by_signal)
    if not item:
        reasons.append("hermes_review_item_missing")
        return reasons
    if item.get("eligible_for_approval") is not True:
        reasons.append("hermes_review_item_not_eligible")
        for reason in item.get("blocking_reasons") or []:
            if reason:
                reasons.append(f"hermes:{reason}")
    return reasons


def actionable_alerts(alerts, packet=None):
    packet = packet if isinstance(packet, dict) else {}
    items_by_signal = review_items_by_signal(packet)
    rows = []
    for alert in base_actionable_alerts(alerts):
        if not alert_review_reasons(alert, packet, items_by_signal):
            rows.append(alert)
    return rows


def ineligible_actionable_alerts(alerts, packet=None):
    packet = packet if isinstance(packet, dict) else {}
    items_by_signal = review_items_by_signal(packet)
    rows = []
    for alert in base_actionable_alerts(alerts):
        if alert_review_reasons(alert, packet, items_by_signal):
            rows.append(alert)
    return rows


def review_items_by_signal(packet):
    if not INCLUDE_PACKET_CONTEXT or not isinstance(packet, dict):
        return {}
    rows = packet.get("review_items")
    if not isinstance(rows, list):
        return {}
    return {
        str(item.get("signal_id")): item
        for item in rows
        if isinstance(item, dict) and item.get("signal_id")
    }


def short_list(values, limit=4):
    rows = []
    for value in values or []:
        if value in (None, ""):
            continue
        rows.append(str(value))
        if len(rows) >= limit:
            break
    return ",".join(rows)


def factor_evidence_basis_line(alert):
    basis = alert.get("factor_evidence_basis") if isinstance(alert, dict) else {}
    if not isinstance(basis, dict) or not basis:
        return ""
    parts = []
    for key in ("completed_daily_ohlcv", "current_session_quote", "unspecified"):
        value = basis.get(key)
        if value not in (None, "", 0):
            parts.append(f"{key}={value}")
    for key, value in sorted(basis.items()):
        if key in ("completed_daily_ohlcv", "current_session_quote", "unspecified"):
            continue
        if value not in (None, "", 0):
            parts.append(f"{key}={value}")
    if not parts:
        return ""
    current_session = alert.get("current_session_quote_evidence")
    suffix = ""
    if isinstance(current_session, dict) and current_session.get("used_in_full_score"):
        suffix = " quote_in_score=true"
    return "├─ 證據來源：" + " ".join(parts[:4]) + suffix


def compact_source_components(source_limits):
    components = source_limits.get("components") if isinstance(source_limits, dict) else []
    rows = []
    for component in components if isinstance(components, list) else []:
        if not isinstance(component, dict):
            continue
        name = component.get("name")
        reasons = component.get("reasons") if isinstance(component.get("reasons"), list) else []
        if name and reasons:
            rows.append(f"{name}:{short_list(reasons, limit=2)}")
        elif name:
            rows.append(str(name))
        if len(rows) >= 3:
            break
    return " ".join(rows)


def matching_review_item(alert, items_by_signal):
    sid = signal_id(alert)
    return items_by_signal.get(str(sid)) if sid else None


def build_hermes_context_lines(alert, packet, items_by_signal):
    if not INCLUDE_PACKET_CONTEXT:
        return []
    if not isinstance(packet, dict) or packet.get("schema") != "hermes_signal_review_packet_v1":
        return ["├─ Hermes審核狀態：MISSING"]
    item = matching_review_item(alert, items_by_signal)
    if not item:
        return ["├─ Hermes審核：NO_MATCH（僅技術信號，未匹配Hermes packet review_item）"]

    lines = [
        "├─ Hermes審核：eligible={eligible} judgment={judgment}".format(
            eligible=item.get("eligible_for_approval"),
            judgment=item.get("recommended_judgment", "?"),
        )
    ]
    blockers = item.get("blocking_reasons") if isinstance(item.get("blocking_reasons"), list) else []
    if blockers:
        lines.append(f"├─ Hermes阻塞：{short_list(blockers, limit=4)}")

    readiness = packet.get("execution_readiness") if isinstance(packet.get("execution_readiness"), dict) else {}
    if readiness:
        lines.append(
            "├─ 執行準備：{status} ready={ready}".format(
                status=readiness.get("status", "?"),
                ready=readiness.get("ready_for_execute", "?"),
            )
        )
    simulation = packet.get("simulation_performance") if isinstance(packet.get("simulation_performance"), dict) else {}
    if simulation:
        sim_text = f"├─ 模擬表現：{simulation.get('status', '?')}"
        reasons = short_list(simulation.get("reason_codes") or [], limit=3)
        if reasons:
            sim_text += f" reasons={reasons}"
        lines.append(sim_text)
    learning = packet.get("strategy_learning_brief") if isinstance(packet.get("strategy_learning_brief"), dict) else {}
    alpha = learning.get("hermes_alpha_evidence") if isinstance(learning.get("hermes_alpha_evidence"), dict) else {}
    if alpha:
        lines.append(f"├─ Hermes Alpha：{alpha.get('status', '?')}")

    digest = item.get("context_digest") if isinstance(item.get("context_digest"), dict) else {}
    market = digest.get("market_context") if isinstance(digest.get("market_context"), dict) else {}
    if market:
        lines.append(f"├─ 市場：{market.get('regime', '?')}/{market.get('risk_level', '?')}")
    intraday = digest.get("intraday_signal_evidence") if isinstance(digest.get("intraday_signal_evidence"), dict) else {}
    if intraday:
        evidence = f"├─ 分鐘證據：{intraday.get('alignment', '?')}"
        codes = short_list(intraday.get("codes") or [], limit=3)
        if codes:
            evidence += f" codes={codes}"
        if intraday.get("requires_judgment_acknowledgement"):
            evidence += " ack=required"
        lines.append(evidence)
    external = digest.get("external_market_context") if isinstance(digest.get("external_market_context"), dict) else {}
    if external and external.get("status") not in (None, "OK", "PASS"):
        lines.append(f"├─ 新聞/宏觀覆蓋：{external.get('status')}")
    catalysts = digest.get("event_catalysts") if isinstance(digest.get("event_catalysts"), dict) else {}
    if catalysts and catalysts.get("status") not in (None, "OK", "PASS"):
        lines.append(
            "├─ 事件覆蓋：{status} negative={negative} positive={positive}".format(
                status=catalysts.get("status"),
                negative=catalysts.get("negative_candidate_count", 0),
                positive=catalysts.get("positive_candidate_count", 0),
            )
        )
    event_signals = digest.get("event_catalyst_signals") if isinstance(digest.get("event_catalyst_signals"), dict) else {}
    if event_signals and (
        event_signals.get("challenge_buy_count") is not None or event_signals.get("support_buy_count") is not None
    ):
        lines.append(
            "├─ 事件審核：challenge={challenge} support={support}".format(
                challenge=event_signals.get("challenge_buy_count", 0),
                support=event_signals.get("support_buy_count", 0),
            )
        )
    sentiment = digest.get("market_sentiment") if isinstance(digest.get("market_sentiment"), dict) else {}
    if sentiment and sentiment.get("status") not in (None, "OK", "PASS"):
        lines.append(f"├─ 情緒覆蓋：{sentiment.get('status')}")
    fundamentals = digest.get("fundamentals_context") if isinstance(digest.get("fundamentals_context"), dict) else {}
    if fundamentals and fundamentals.get("status") not in (None, "OK", "PASS"):
        lines.append(f"├─ 基本面覆蓋：{fundamentals.get('status')}")
    source_limits = digest.get("source_limits") if isinstance(digest.get("source_limits"), dict) else {}
    source_status = source_limits.get("source_reliability_status")
    if source_status and source_status not in ("OK", "PASS"):
        detail = compact_source_components(source_limits)
        suffix = f" {detail}" if detail else ""
        lines.append(f"├─ 來源可靠性：{source_status}{suffix}")
    return lines


def position_review_items(packet):
    if not INCLUDE_POSITION_REVIEW:
        return []
    worklist_by_id = position_judgment_worklist_by_review_id(packet)
    review = packet.get("position_review") if isinstance(packet, dict) else {}
    if not isinstance(review, dict):
        return []
    items = review.get("items")
    if not isinstance(items, list):
        return []
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        review_id = item.get("review_id")
        if not review_id:
            continue
        role = str(item.get("role") or "").lower()
        if POSITION_REVIEW_ROLES and not ({"*", "all"} & POSITION_REVIEW_ROLES) and role not in POSITION_REVIEW_ROLES:
            continue
        urgency = str(item.get("urgency") or "").lower()
        if POSITION_REVIEW_URGENCY and urgency not in POSITION_REVIEW_URGENCY:
            continue
        work_item = worklist_by_id.get(str(review_id))
        if work_item:
            merged = dict(item)
            merged["position_judgment_work_item"] = work_item
            rows.append(merged)
        else:
            rows.append(item)
    rows.sort(key=lambda row: {"high": 0, "medium": 1, "low": 2}.get(str(row.get("urgency") or "").lower(), 9))
    return rows[: max(POSITION_REVIEW_LIMIT, 0)]


def position_judgment_worklist_by_review_id(packet):
    worklist = packet.get("position_judgment_worklist") if isinstance(packet, dict) else {}
    if not isinstance(worklist, dict):
        return {}
    rows = {}
    for item in worklist.get("items") or []:
        if not isinstance(item, dict):
            continue
        review_id = str(item.get("review_id") or "").strip()
        if review_id:
            rows[review_id] = item
    return rows


def pending_position_reviews(packet, sent_rows, now_epoch=None):
    now_epoch = time.time() if now_epoch is None else now_epoch
    sent_by_thread = {}
    for row in sent_rows:
        key = position_review_thread_key(row)
        if not key:
            continue
        if key not in sent_by_thread or sent_record_time(row) >= sent_record_time(sent_by_thread[key]):
            sent_by_thread[key] = row
    reminder_seconds = POSITION_REVIEW_REMINDER_HOURS * 3600
    pending = []
    for item in position_review_items(packet):
        thread_key = position_review_thread_key(item)
        last_sent = sent_by_thread.get(thread_key)
        last_sent_at = sent_record_time(last_sent)
        notice_fingerprint = position_review_notice_fingerprint(item)
        last_notice_fingerprint = str((last_sent or {}).get("notice_fingerprint") or "")
        if (
            last_sent is None
            or position_review_escalated(item, last_sent)
            or (notice_fingerprint and notice_fingerprint != last_notice_fingerprint)
            or (reminder_seconds > 0 and now_epoch - last_sent_at >= reminder_seconds)
        ):
            pending.append(item)
    return pending


def operator_action_key(item):
    if not isinstance(item, dict):
        return None
    action_id = str(item.get("id") or "").strip()
    return action_id or None


def compact_postmortem_targets(action, limit=6):
    evidence = action.get("evidence") if isinstance(action, dict) else {}
    if not isinstance(evidence, dict):
        return []
    rows = []
    for target in evidence.get("postmortem_note_write_plan") or []:
        if not isinstance(target, dict):
            continue
        target_id = str(target.get("target_id") or "").strip()
        if not target_id:
            symbol = str(target.get("symbol") or "").strip().upper()
            target_type = str(target.get("target_type") or "").strip()
            target_id = f"{target_type}:{symbol}" if target_type and symbol else symbol
        if target_id:
            rows.append(target_id)
        if len(rows) >= limit:
            break
    return rows


def operator_action_fingerprint(item):
    if not isinstance(item, dict):
        return ""
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    stable = {
        "id": item.get("id"),
        "priority": item.get("priority"),
        "category": item.get("category"),
        "title": item.get("title"),
        "summary": item.get("summary"),
        "recommended_next_step": item.get("recommended_next_step"),
        "blockers": item.get("blockers") if isinstance(item.get("blockers"), list) else [],
        "operator_effect": item.get("operator_effect") if isinstance(item.get("operator_effect"), dict) else {},
        "postmortem_targets": compact_postmortem_targets(item, limit=20),
        "evidence_status": evidence.get("status") or evidence.get("audit_status"),
        "reason_codes": evidence.get("reason_codes") if isinstance(evidence.get("reason_codes"), list) else [],
        "remediation_proposal_hash": evidence.get("remediation_proposal_hash"),
    }
    return json.dumps(stable, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def operator_action_items(packet):
    queue = packet.get("operator_action_queue") if isinstance(packet, dict) else {}
    if not isinstance(queue, dict) or queue.get("status") not in ("ACTION_REQUIRED", "WARN", "FAIL"):
        return []
    rows = []
    for item in queue.get("actions") or []:
        if not isinstance(item, dict):
            continue
        action_id = operator_action_key(item)
        if not action_id or action_id in OPERATOR_ACTION_SUPPRESSED_IDS:
            continue
        priority = str(item.get("priority") or "").upper()
        if OPERATOR_ACTION_PRIORITIES and not ({"*", "ALL"} & OPERATOR_ACTION_PRIORITIES) and priority not in OPERATOR_ACTION_PRIORITIES:
            continue
        rows.append(item)
    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    rows.sort(key=lambda row: (priority_order.get(str(row.get("priority") or "").upper(), 9), str(row.get("id") or "")))
    return rows[: max(OPERATOR_ACTION_LIMIT, 0)]


def pending_operator_actions(packet, sent_rows, now_epoch=None):
    now_epoch = time.time() if now_epoch is None else now_epoch
    sent_by_action = {}
    for row in sent_rows:
        action_id = str(row.get("action_id") or row.get("id") or "").strip() if isinstance(row, dict) else ""
        if not action_id:
            continue
        if action_id not in sent_by_action or sent_record_time(row) >= sent_record_time(sent_by_action[action_id]):
            sent_by_action[action_id] = row
    reminder_seconds = OPERATOR_ACTION_REMINDER_HOURS * 3600
    pending = []
    for item in operator_action_items(packet):
        action_id = operator_action_key(item)
        last_sent = sent_by_action.get(action_id)
        last_sent_at = sent_record_time(last_sent)
        fingerprint = operator_action_fingerprint(item)
        last_fingerprint = str((last_sent or {}).get("fingerprint") or "")
        if (
            last_sent is None
            or (fingerprint and fingerprint != last_fingerprint)
            or (reminder_seconds > 0 and now_epoch - last_sent_at >= reminder_seconds)
        ):
            pending.append(item)
    return pending


def compact_operator_effect(effect):
    if not isinstance(effect, dict) or not effect:
        return ""
    keys = (
        "submits_orders",
        "changes_portfolio",
        "changes_strategy",
        "changes_strategy_config",
        "changes_execution_mode",
        "writes_judgments",
        "writes_postmortem_notes",
    )
    parts = [f"{key}={str(effect.get(key)).lower()}" for key in keys if key in effect]
    return " ".join(parts)


def build_operator_action_output(items, packet):
    queue = packet.get("operator_action_queue") if isinstance(packet.get("operator_action_queue"), dict) else {}
    summary = queue.get("summary") if isinstance(queue.get("summary"), dict) else {}
    counts = summary.get("priority_counts") if isinstance(summary.get("priority_counts"), dict) else {}
    count_text = ",".join(f"{key}={value}" for key, value in sorted(counts.items())) if counts else "?"
    lines = [
        "🧯 **Hermes系統待辦（不下單）**",
        "此區只提示系統阻塞、模擬復盤、資料/流程待辦；不代表交易信號通過，也不會提交模擬或券商訂單。",
        f"本次提醒待辦：{len(items)}（queue_actions={summary.get('action_count', '?')}, priorities={count_text}）",
    ]
    for item in items:
        lines.append("")
        lines.append(
            "⚙️ **{action_id}** priority={priority}".format(
                action_id=item.get("id", "?"),
                priority=item.get("priority", "?"),
            )
        )
        if item.get("title"):
            lines.append(f"├─ 標題：{item.get('title')}")
        if item.get("summary"):
            lines.append(f"├─ 摘要：{item.get('summary')}")
        blockers = item.get("blockers") if isinstance(item.get("blockers"), list) else []
        if blockers:
            lines.append(f"├─ 阻塞：{short_list(blockers, limit=4)}")
        targets = compact_postmortem_targets(item)
        if targets:
            lines.append(f"├─ 復盤目標：postmortem_targets={','.join(targets)}")
        effect = compact_operator_effect(item.get("operator_effect"))
        if effect:
            lines.append(f"├─ 操作影響：{effect}")
        if item.get("recommended_next_step"):
            lines.append(f"├─ 下一步：{item.get('recommended_next_step')}")
        if item.get("operator_command"):
            lines.append(f"├─ 建議命令：{item.get('operator_command')}")
        lines.append("└─ 安全要求：operator_review=true；order_submission=false；execution_mode_change=false")
    return "\n".join(lines)


def mark_operator_actions_sent(sent, items):
    now_epoch = time.time()
    latest_by_action = {}
    passthrough = []
    for row in sent:
        if not isinstance(row, dict):
            continue
        action_id = str(row.get("action_id") or row.get("id") or "").strip()
        if not action_id:
            passthrough.append(row)
            continue
        current = latest_by_action.get(action_id)
        if current is None or sent_record_time(row) >= sent_record_time(current):
            latest_by_action[action_id] = row
    for item in items:
        action_id = operator_action_key(item)
        if not action_id:
            continue
        latest_by_action[action_id] = {
            "action_id": action_id,
            "priority": item.get("priority"),
            "sent_at_epoch": now_epoch,
            "fingerprint": operator_action_fingerprint(item),
        }
    rows = passthrough + list(latest_by_action.values())
    rows.sort(key=sent_record_time)
    write_operator_action_sent(rows[-1000:])


def fmt_pct(value):
    try:
        return f"{float(value):+.1f}%"
    except Exception:
        return "?"


def fmt_optional(value):
    if value in (None, ""):
        return "?"
    try:
        return f"{float(value):.4g}"
    except Exception:
        return str(value)


def fmt_hours(value):
    try:
        return f"{float(value):.1f}h"
    except Exception:
        return "?"


def compact_decision_points(points, limit=3):
    rows = []
    for point in points or []:
        if not isinstance(point, dict):
            continue
        decision = point.get("decision")
        if not decision:
            continue
        parts = [str(decision)]
        quantity = point.get("quantity_hint")
        if quantity not in (None, ""):
            parts.append(f"qty={fmt_optional(quantity)}")
        price = point.get("price_reference")
        if price not in (None, ""):
            parts.append(f"ref={fmt_optional(price)}")
        condition = point.get("condition")
        if condition:
            parts.append(str(condition))
        rows.append(" ".join(parts))
        if len(rows) >= limit:
            break
    return "; ".join(rows)


def compact_intraday_contract(contract):
    if not isinstance(contract, dict) or not contract:
        return ""
    timeframes = ",".join(str(value) for value in (contract.get("required_timeframes") or [])[:4])
    checks = ",".join(str(value) for value in (contract.get("required_checks") or [])[:3])
    decision_use = contract.get("decision_use")
    parts = []
    if timeframes:
        parts.append(f"tf={timeframes}")
    if decision_use:
        parts.append(f"use={decision_use}")
    if checks:
        parts.append(f"checks={checks}")
    return " ".join(parts)


def compact_intraday_position_evidence(evidence):
    if not isinstance(evidence, dict) or not evidence:
        return ""
    parts = []
    alignment = evidence.get("alignment")
    action_intent = evidence.get("action_intent")
    status = evidence.get("status")
    if alignment:
        parts.append(f"align={alignment}")
    if action_intent:
        parts.append(f"intent={action_intent}")
    if status:
        parts.append(f"status={status}")
    session = evidence.get("session_momentum")
    session_change = evidence.get("session_change_pct")
    if session or session_change not in (None, ""):
        parts.append(f"session={session or '?'} {fmt_pct(session_change)}")
    timeframe = evidence.get("timeframe_alignment")
    if timeframe:
        parts.append(f"tf={timeframe}")
    code_parts = []
    for label, key in (("support", "support_codes"), ("challenge", "challenge_codes"), ("limit", "limit_codes")):
        values = evidence.get(key)
        if isinstance(values, list) and values:
            code_parts.append(f"{label}={','.join(str(value) for value in values[:3])}")
    if code_parts:
        parts.append(" ".join(code_parts))
    return " ".join(parts)


def compact_worklist_summary(work_item):
    if not isinstance(work_item, dict) or not work_item:
        return []
    required = work_item.get("required_output_fields") if isinstance(work_item.get("required_output_fields"), dict) else {}
    context_summary = work_item.get("context_summary") if isinstance(work_item.get("context_summary"), dict) else {}
    dynamic = context_summary.get("dynamic_management") if isinstance(context_summary.get("dynamic_management"), dict) else {}
    intraday_position = compact_intraday_position_evidence(context_summary.get("intraday_position_evidence"))
    attention = work_item.get("required_attention_codes") if isinstance(work_item.get("required_attention_codes"), list) else []
    lines = [
        "├─ Hermes優先入口：position_judgment_worklist.items[]（短上下文+必填欄位；仍需審核後填寫）",
    ]
    if required:
        lines.append(
            "├─ Worklist輸出：schema={schema} advisory_only={advisory} submits_orders={orders}".format(
                schema=required.get("schema", "?"),
                advisory=required.get("advisory_only", "?"),
                orders=required.get("submits_orders", "?"),
            )
        )
        lines.append(
            "├─ JSONL寫入：append一行到/tmp/hermes_position_judgments.jsonl；必填reviewed_at,confidence,"
            "decision,supporting_factors,opposing_factors,risk_notes,context_review"
        )
    if dynamic:
        lines.append(
            "├─ Worklist動態：status={status} pnl={pnl} day={day} dist_stop={dist_stop} px_age={age}".format(
                status=dynamic.get("target_status", "?"),
                pnl=fmt_pct(dynamic.get("unrealized_pnl_pct")),
                day=fmt_pct(dynamic.get("latest_daily_change_pct")),
                dist_stop=fmt_pct(dynamic.get("distance_above_signal_stop_loss_pct")),
                age=fmt_hours(dynamic.get("price_snapshot_age_hours")),
            )
        )
    if intraday_position:
        lines.append(f"├─ Worklist盤中證據：{intraday_position}")
    if attention:
        lines.append(f"├─ Worklist必回應：{','.join(str(code) for code in attention[:6])}")
    return lines


def build_position_review_output(items, packet):
    audit = packet.get("position_judgment_audit") if isinstance(packet.get("position_judgment_audit"), dict) else {}
    coverage = audit.get("coverage") if isinstance(audit.get("coverage"), dict) else {}
    high_count = sum(1 for item in items if str(item.get("urgency") or "").lower() == "high")
    medium_count = sum(1 for item in items if str(item.get("urgency") or "").lower() == "medium")
    roles = sorted({str(item.get("role") or "?").lower() for item in items})
    roles_text = ",".join(roles) if roles else "?"
    lines = [
        "🧭 **Hermes持倉審核待辦（不下單）**",
        "此區只提示持倉風險與審核要求，不代表已通過 Hermes 交易審批，也不會提交模擬或券商訂單。",
        f"本次提醒持倉：{len(items)}（high={high_count}, medium={medium_count}, roles={roles_text}）",
    ]
    unjudged = coverage.get("unjudged_high_urgency_review_count")
    if unjudged is not None and unjudged != high_count:
        lines.append(f"packet全局未審核高優先級：{unjudged}（可能包含本次已過濾角色）")
    for item in items:
        position = item.get("position") if isinstance(item.get("position"), dict) else {}
        latest_signal = item.get("latest_signal") if isinstance(item.get("latest_signal"), dict) else {}
        advisory_plan = item.get("advisory_plan") if isinstance(item.get("advisory_plan"), dict) else {}
        digest = item.get("context_digest") if isinstance(item.get("context_digest"), dict) else {}
        attention = digest.get("position_attention") if isinstance(digest.get("position_attention"), list) else []
        work_item = item.get("position_judgment_work_item") if isinstance(item.get("position_judgment_work_item"), dict) else {}
        lines.append("")
        lines.append(
            f"⚠️ **{item.get('symbol','?')}** {item.get('role','?')} "
            f"urgency={item.get('urgency','?')} review_action={item.get('recommended_action','?')}"
        )
        lines.append(
            "├─ 持倉：qty={qty} pnl={pnl} stop_distance={stop}".format(
                qty=position.get("quantity", "?"),
                pnl=fmt_pct(position.get("unrealized_pnl_pct")),
                stop=fmt_pct(position.get("stop_distance_pct")),
            )
        )
        if latest_signal:
            lines.append(
                "├─ 最新信號：{side} score={score}".format(
                    side=latest_signal.get("side", "?"),
                    score=latest_signal.get("score", "?"),
                )
            )
        if advisory_plan:
            refs = advisory_plan.get("reference_prices") if isinstance(advisory_plan.get("reference_prices"), dict) else {}
            lines.append(
                "├─ 審核草案：{action} add_allowed={add_allowed} qty_hint={qty} sig_stop_ref={stop} sig_target_ref={target} trail_floor={trail}".format(
                    action=advisory_plan.get("primary_action", "?"),
                    add_allowed=advisory_plan.get("add_allowed_after_review", "?"),
                    qty=fmt_optional(advisory_plan.get("manual_max_quantity_hint")),
                    stop=fmt_optional(refs.get("signal_stop_loss")),
                    target=fmt_optional(refs.get("signal_take_profit")),
                    trail=fmt_optional(refs.get("trailing_stop_floor_reference")),
                )
            )
            dynamic = (
                advisory_plan.get("dynamic_management_context")
                if isinstance(advisory_plan.get("dynamic_management_context"), dict)
                else {}
            )
            if dynamic:
                lines.append(
                    "├─ 動態管理：status={status} pnl={pnl} day={day} dist_tp={dist_tp} dist_stop={dist_stop} px_age={age} trail_ref={trail}".format(
                        status=dynamic.get("target_status", "?"),
                        pnl=fmt_pct(dynamic.get("unrealized_pnl_pct")),
                        day=fmt_pct(dynamic.get("latest_daily_change_pct")),
                        dist_tp=fmt_pct(dynamic.get("distance_to_signal_take_profit_pct")),
                        dist_stop=fmt_pct(dynamic.get("distance_above_signal_stop_loss_pct")),
                        age=fmt_hours(dynamic.get("price_snapshot_age_hours")),
                        trail=fmt_optional(dynamic.get("trail_floor_reference")),
                    )
                )
            decision_points = compact_decision_points(advisory_plan.get("operator_decision_points"))
            if decision_points:
                lines.append(f"├─ 候選動作：{decision_points}")
            intraday_contract = compact_intraday_contract(advisory_plan.get("intraday_review_contract"))
            if intraday_contract:
                lines.append(f"├─ 盤中審核：{intraday_contract}")
        if attention:
            lines.append(f"├─ 必須回應風險：{','.join(str(code) for code in attention[:6])}")
        lines.extend(compact_worklist_summary(work_item))
        lines.append(
            "├─ 審核ID：review_id={review_id} judgment_file=/tmp/hermes_position_judgments.jsonl".format(
                review_id=item.get("review_id")
            )
        )
        lines.append("└─ 審核要求：context_review五項=true；position_attention_acknowledged=true；allowed=hold|watch|reduce|exit|trail_stop；order_submission=false")
    return "\n".join(lines)


def build_output(actionable, execution_mode, packet=None, title=None, run_intake=True):
    packet = packet if isinstance(packet, dict) else {}
    items_by_signal = review_items_by_signal(packet)
    title = title or "🎯 **Hermes可審操作候選**"
    lines = [f"{title}\n"]
    for alert in actionable:
        icon = "🟢" if alert.get("signal_type") == "BUY" else "🔴"
        lines.append(f"{icon} **{alert['symbol']}** — {alert['signal_type']}")
        lines.append(f"├─ 觸發：{alert['trigger']} ({alert['detail']})")
        lines.append(f"├─ 入場價：${alert['entry_price']}")
        lines.append(f"├─ 止盈：${alert['take_profit']}")
        lines.append(f"├─ 止損：${alert['stop_loss']}")
        lines.append(f"├─ 風險回報：{alert.get('rr_ratio', '?')}")
        lines.append(f"├─ 多因子分：{alert.get('full_score', '?')} | 確認：{alert.get('confirmed', True)}")
        evidence_line = factor_evidence_basis_line(alert)
        if evidence_line:
            lines.append(evidence_line)
        lines.extend(build_hermes_context_lines(alert, packet, items_by_signal))
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
    elif execution_mode in ("alert-dry-run", "alert-sim") and run_intake:
        lines.append(f"📊 **Alert-specific intake（{execution_mode}）：**")
        for alert in actionable:
            result = run_order_intake(alert, execution_mode)
            lines.append(summarize_intake(result) if result else f"{alert.get('signal_id', alert.get('symbol'))}: intake無輸出")
    elif execution_mode in ("alert-dry-run", "alert-sim"):
        lines.append("📎 模擬倉：未執行（Hermes/安全門未放行，僅診斷）")
    else:
        lines.append(f"⚠️ 未知執行模式：{execution_mode}，已跳過模擬倉操作")
    return "\n".join(lines)


def mark_alerts_sent(sent, new_alerts):
    sent.extend(new_alerts)
    write_sent(sent)


def unique_alerts(alerts):
    rows = []
    seen = set()
    for alert in alerts:
        key = alert_key(alert)
        if key in seen:
            continue
        seen.add(key)
        rows.append(alert)
    return rows


def mark_position_reviews_sent(sent, items):
    now_epoch = time.time()
    existing = compact_position_review_sent(sent)
    existing_keys = {position_review_thread_key(row) for row in existing if position_review_thread_key(row)}
    for item in items:
        review_id = item.get("review_id")
        if not review_id:
            continue
        thread_key = position_review_thread_key(item)
        row = {
            "review_id": review_id,
            "review_thread_key": thread_key,
            "symbol": item.get("symbol"),
            "urgency": item.get("urgency"),
            "recommended_action": item.get("recommended_action"),
            "sent_at_epoch": now_epoch,
            "notice_fingerprint": position_review_notice_fingerprint(item),
        }
        if thread_key in existing_keys:
            existing = [old for old in existing if position_review_thread_key(old) != thread_key]
        existing.append(row)
        existing_keys.add(thread_key)
    write_position_review_sent(existing)


def compact_position_review_sent(sent):
    latest_by_thread = {}
    passthrough = []
    for row in sent:
        if not isinstance(row, dict):
            continue
        thread_key = position_review_thread_key(row)
        if not thread_key:
            passthrough.append(row)
            continue
        current = latest_by_thread.get(thread_key)
        if current is None or sent_record_time(row) >= sent_record_time(current):
            latest_by_thread[thread_key] = row
    compacted = passthrough + list(latest_by_thread.values())
    compacted.sort(key=sent_record_time)
    return compacted[-1000:]


def main():
    alerts = load_alerts()
    packet = load_json_file(HERMES_REVIEW_PACKET_FILE, {})

    sent_raw = read_text_file(SENT_FILE)
    try:
        sent = json.loads(sent_raw) if sent_raw else []
    except Exception:
        sent = []
    if not isinstance(sent, list):
        sent = []

    sent_keys = {alert_key(alert) for alert in sent if isinstance(alert, dict)}
    new_alerts = [alert for alert in alerts if alert_key(alert) not in sent_keys]

    actionable = actionable_alerts(new_alerts, packet)
    diagnostic_alerts = ineligible_actionable_alerts(new_alerts, packet) if NOTIFY_INELIGIBLE_SIGNALS else []
    position_sent_raw = read_text_file(POSITION_REVIEW_SENT_FILE)
    try:
        position_sent = json.loads(position_sent_raw) if position_sent_raw else []
    except Exception:
        position_sent = []
    if not isinstance(position_sent, list):
        position_sent = []
    compacted_position_sent = compact_position_review_sent(position_sent)
    if compacted_position_sent != position_sent:
        write_position_review_sent(compacted_position_sent)
    position_sent = compacted_position_sent
    pending_reviews = pending_position_reviews(packet, position_sent)

    operator_sent_raw = read_text_file(OPERATOR_ACTION_SENT_FILE)
    try:
        operator_sent = json.loads(operator_sent_raw) if operator_sent_raw else []
    except Exception:
        operator_sent = []
    if not isinstance(operator_sent, list):
        operator_sent = []
    pending_operator = pending_operator_actions(packet, operator_sent)

    if not new_alerts and not pending_reviews and not pending_operator:
        return 0

    outputs = []
    if actionable:
        outputs.append(build_output(actionable, EXECUTION_MODE, packet=packet))
    if diagnostic_alerts:
        outputs.append(
            build_output(
                diagnostic_alerts,
                EXECUTION_MODE,
                packet=packet,
                title="🧪 **候選信號（安全門未放行）**",
                run_intake=False,
            )
        )
    if pending_reviews:
        outputs.append(build_position_review_output(pending_reviews, packet))
    if pending_operator:
        outputs.append(build_operator_action_output(pending_operator, packet))

    text = "\n\n".join(outputs)
    if text:
        print(text)
        if SEND_FEISHU and not send_feishu_text(text):
            return 2

    alerts_to_mark_sent = new_alerts if MARK_INELIGIBLE_SENT else unique_alerts(actionable + diagnostic_alerts)
    if alerts_to_mark_sent:
        mark_alerts_sent(sent, alerts_to_mark_sent)
    if pending_reviews:
        mark_position_reviews_sent(position_sent, pending_reviews)
    if pending_operator:
        mark_operator_actions_sent(operator_sent, pending_operator)
    return 0


if __name__ == "__main__":
    sys.exit(main())
