#!/usr/bin/env python3
"""Read-only audit of Hermes trade judgments against the latest review packet."""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

try:
    import rt_order_intake as intake
except ImportError:
    from scripts import rt_order_intake as intake


JUDGMENT_FILE = os.environ.get("RT_ORDER_JUDGMENT_FILE", "/tmp/hermes_trade_judgments.jsonl")
PACKET_FILE = os.environ.get("HERMES_REVIEW_PACKET_FILE", "/tmp/hermes_signal_review_packet.json")
PACKET_ARCHIVE_DIR = os.environ.get("HERMES_REVIEW_PACKET_ARCHIVE_DIR", "/tmp/hermes_review_packet_archive")
REPORT_FILE = os.environ.get("HERMES_JUDGMENT_AUDIT_FILE", "/tmp/hermes_judgment_audit_report.json")
MAX_JUDGMENT_AGE_MINUTES = int(os.environ.get("RT_ORDER_MAX_JUDGMENT_AGE_MINUTES", "240"))


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else default
    except Exception:
        return default


def safe_file_stem(value):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or ""))[:120]


def packet_archive_path(packet_id, archive_dir=PACKET_ARCHIVE_DIR):
    stem = safe_file_stem(packet_id)
    if not stem or not archive_dir:
        return ""
    return os.path.join(archive_dir, f"{stem}.json")


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def decision_is_approval(judgment):
    return str(judgment.get("decision", "")).strip().lower() in ("approve", "reduce")


def packet_review_maps(packet):
    items = packet.get("review_items") if isinstance(packet, dict) else []
    by_id = {}
    eligible = set()
    for item in items or []:
        sid = str(item.get("signal_id", ""))
        if not sid:
            continue
        by_id[sid] = item
        if item.get("eligible_for_approval"):
            eligible.add(sid)
    return by_id, eligible


def packet_for_judgment(judgment, latest_packet, archive_dir=PACKET_ARCHIVE_DIR):
    packet_id = str(judgment.get("packet_id", "")).strip()
    if not packet_id:
        return latest_packet, "latest_packet_fallback", ["judgment_missing_packet_id"]

    archive_path = packet_archive_path(packet_id, archive_dir)
    archived = load_json_file(archive_path, {}) if archive_path else {}
    if archived:
        return archived, "packet_archive", []

    if isinstance(latest_packet, dict) and str(latest_packet.get("packet_id", "")) == packet_id:
        return latest_packet, "latest_packet_matching_packet_id", []

    return latest_packet, "latest_packet_fallback", ["packet_archive_missing_for_packet_id"]


def market_regime_for_item(packet, item):
    alert = item.get("alert") or {}
    market = str(alert.get("market") or "").upper()
    if market not in ("HK", "US"):
        symbol = str(alert.get("symbol", ""))
        market = "HK" if symbol[:1].isdigit() and len(symbol) == 5 else "US"
    market_payload = ((packet.get("market_context") or {}).get("markets") or {}).get(market) or {}
    return market, market_payload.get("regime"), market_payload


def strategy_evidence_reasons(packet, item):
    evidence = packet.get("strategy_evidence") or {}
    if evidence.get("schema") != "rt_signal_outcome_report_v1":
        return ["strategy_evidence_missing_or_invalid"]
    alert = item.get("alert") or {}
    horizon = os.environ.get("RT_ORDER_STRATEGY_EVIDENCE_HORIZON", "1d")
    reasons = []
    overall = ((evidence.get("overall") or {}).get("horizons") or {}).get(horizon) or {}
    if not overall:
        reasons.append(f"strategy_evidence_horizon_missing_{horizon}")
    else:
        reasons.extend(intake.metric_reasons(overall, "resolved_count", intake.MIN_OUTCOME_SAMPLE, "overall"))

    trigger_key = f"{str(alert.get('signal_type', '')).upper()}:{alert.get('trigger') or 'UNKNOWN'}"
    trigger_metric = {}
    for row in evidence.get("by_trigger") or []:
        if row.get("key") == trigger_key:
            trigger_metric = ((row.get("horizons") or {}).get(horizon) or {})
            break
    if intake.MIN_TRIGGER_OUTCOME_SAMPLE > 0:
        if not trigger_metric:
            reasons.append("trigger_outcome_missing")
        else:
            reasons.extend(intake.metric_reasons(trigger_metric, "resolved_count", intake.MIN_TRIGGER_OUTCOME_SAMPLE, "trigger"))
    return reasons


def validate_judgment_contract(judgment):
    reasons = []
    if judgment.get("schema") != "hermes_trade_judgment_v1":
        reasons.append("schema_invalid")
    sid = str(judgment.get("signal_id", "")).strip()
    if not sid:
        reasons.append("missing_signal_id")
    decision = str(judgment.get("decision", "")).strip().lower()
    if decision not in ("approve", "reject", "reduce", "hold"):
        reasons.append("decision_invalid")
    confidence = as_float(judgment.get("confidence"))
    if confidence is None or confidence < 0 or confidence > 1:
        reasons.append("confidence_invalid")
    reviewed_at = intake.parse_time(judgment.get("reviewed_at") or judgment.get("created_at"))
    if not reviewed_at:
        reasons.append("reviewed_at_invalid")
    if not isinstance(judgment.get("supporting_factors"), list) or not judgment.get("supporting_factors"):
        reasons.append("supporting_factors_missing")
    if not isinstance(judgment.get("opposing_factors"), list) or not judgment.get("opposing_factors"):
        reasons.append("opposing_factors_missing")
    if not isinstance(judgment.get("risk_notes"), list) or not judgment.get("risk_notes"):
        reasons.append("risk_notes_missing")
    if decision == "reduce":
        try:
            if int(float(judgment.get("max_quantity"))) <= 0:
                reasons.append("max_quantity_invalid")
        except (TypeError, ValueError):
            reasons.append("max_quantity_missing")
    if judgment.get("market_regime_exception") is True:
        ok, exception_reasons = intake.market_exception_from_judgment(judgment)
        if not ok:
            reasons.extend(exception_reasons)
    return reasons


def audit_judgment(judgment, packet, review_by_id, eligible_ids, now=None, packet_source="latest_packet", packet_reasons=None):
    now = now or datetime.now()
    sid = str(judgment.get("signal_id", "")).strip()
    reasons = validate_judgment_contract(judgment)
    reasons.extend(packet_reasons or [])
    item = review_by_id.get(sid)
    approval = decision_is_approval(judgment)
    if not item:
        reasons.append("orphan_judgment_not_in_latest_packet")
    else:
        alert = item.get("alert") or {}
        if approval and sid not in eligible_ids:
            reasons.append("approval_for_ineligible_review_item")
        if approval and alert.get("confirmed") is not True:
            reasons.append("approval_for_unconfirmed_alert")
        if approval and (packet.get("health") or {}).get("status") == "FAIL":
            reasons.append("approval_while_health_fail")
        if approval and str(alert.get("signal_type", "")).upper() == "BUY":
            market, regime, _market_payload = market_regime_for_item(packet, item)
            if regime == "risk_off":
                ok, exception_reasons = intake.market_exception_from_judgment(judgment)
                if not ok:
                    reasons.append(f"{market}_risk_off_buy_approval_without_exception")
                    reasons.extend(exception_reasons)
        if approval:
            for reason in strategy_evidence_reasons(packet, item):
                reasons.append(f"approval_with_{reason}")

    reviewed_at = intake.parse_time(judgment.get("reviewed_at") or judgment.get("created_at"))
    if reviewed_at:
        expiry = judgment.get("expiry_minutes", MAX_JUDGMENT_AGE_MINUTES)
        try:
            expiry = int(expiry)
        except (TypeError, ValueError):
            expiry = MAX_JUDGMENT_AGE_MINUTES
        if now - reviewed_at > timedelta(minutes=expiry):
            reasons.append("judgment_expired")

    return {
        "signal_id": sid,
        "decision": str(judgment.get("decision", "")).strip().lower(),
        "reviewed_at": judgment.get("reviewed_at") or judgment.get("created_at"),
        "confidence": as_float(judgment.get("confidence")),
        "packet_id": str(judgment.get("packet_id", "")).strip(),
        "packet_source": packet_source,
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
    }


def duplicate_signal_counts(judgments):
    counts = Counter(str(item.get("signal_id", "")).strip() for item in judgments if item.get("signal_id"))
    return {sid: count for sid, count in counts.items() if count > 1}


def build_report(judgments=None, packet=None, now=None, packet_archive_dir=PACKET_ARCHIVE_DIR):
    now = now or datetime.now()
    judgments = intake.load_judgments(JUDGMENT_FILE) if judgments is None else judgments
    latest_packet = load_json_file(PACKET_FILE, {}) if packet is None else packet
    latest_review_by_id, latest_eligible_ids = packet_review_maps(latest_packet)
    rows = []
    packet_source_counts = Counter()
    for judgment in judgments:
        judgment_packet, packet_source, packet_reasons = packet_for_judgment(
            judgment,
            latest_packet,
            archive_dir=packet_archive_dir,
        )
        packet_source_counts[packet_source] += 1
        review_by_id, eligible_ids = packet_review_maps(judgment_packet)
        rows.append(
            audit_judgment(
                judgment,
                judgment_packet,
                review_by_id,
                eligible_ids,
                now=now,
                packet_source=packet_source,
                packet_reasons=packet_reasons,
            )
        )
    reason_counts = Counter()
    decision_counts = Counter()
    status_counts = Counter()
    for row in rows:
        status_counts[row["status"]] += 1
        decision_counts[row["decision"] or "missing"] += 1
        for reason in row["reasons"]:
            reason_counts[reason] += 1

    duplicates = duplicate_signal_counts(judgments)
    for sid, count in duplicates.items():
        reason_counts["duplicate_judgments_for_signal"] += count - 1
    status = "FAIL" if status_counts.get("FAIL") or duplicates else "OK"

    payload = {
        "schema": "hermes_judgment_audit_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "judgment_file": JUDGMENT_FILE,
            "packet_file": PACKET_FILE,
            "packet_archive_dir": packet_archive_dir,
            "latest_packet_id": latest_packet.get("packet_id") if isinstance(latest_packet, dict) else None,
            "latest_packet_generated_at": latest_packet.get("generated_at") if isinstance(latest_packet, dict) else None,
        },
        "counts": {
            "judgment_count": len(judgments),
            "review_item_count": len(latest_review_by_id),
            "eligible_review_item_count": len(latest_eligible_ids),
            "status_counts": dict(status_counts),
            "decision_counts": dict(decision_counts),
            "reason_counts": dict(reason_counts),
            "duplicate_signal_ids": duplicates,
            "packet_source_counts": dict(packet_source_counts),
        },
        "judgments": rows[-100:],
        "recommendations": build_recommendations(rows, reason_counts),
    }
    return payload


def build_recommendations(rows, reason_counts):
    if not rows:
        return ["no_hermes_judgments_observed_yet"]
    recs = []
    critical = [
        "approval_for_ineligible_review_item",
        "approval_for_unconfirmed_alert",
        "approval_while_health_fail",
        "orphan_judgment_not_in_latest_packet",
    ]
    for reason in critical:
        if reason_counts.get(reason):
            recs.append(f"fix_or_reject_judgments:{reason}")
    if any("risk_off_buy_approval_without_exception" in reason for reason in reason_counts):
        recs.append("risk_off_buy_approvals_require_market_regime_exception")
    if any(reason.startswith("approval_with_") for reason in reason_counts):
        recs.append("approvals_conflict_with_execution_gates_keep_alert_sim_disabled")
    if reason_counts.get("judgment_expired"):
        recs.append("refresh_or_ignore_expired_judgments")
    if reason_counts.get("judgment_missing_packet_id"):
        recs.append("include_packet_id_in_future_judgments")
    if reason_counts.get("packet_archive_missing_for_packet_id"):
        recs.append("retain_packet_archive_for_judgment_audit")
    if not recs:
        recs.append("judgment_audit_clean_continue_review_only_observation")
    return recs


def build_text_report(payload):
    counts = payload["counts"]
    lines = [
        f"Hermes judgment audit {payload['generated_at']}",
        (
            f"judgments={counts['judgment_count']} review_items={counts['review_item_count']} "
            f"eligible={counts['eligible_review_item_count']} status={counts['status_counts']}"
        ),
    ]
    if counts["reason_counts"]:
        lines.append("Reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(counts["reason_counts"].items())))
    lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--judgment-file", default=JUDGMENT_FILE)
    parser.add_argument("--packet-file", default=PACKET_FILE)
    parser.add_argument("--packet-archive-dir", default=PACKET_ARCHIVE_DIR)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    global JUDGMENT_FILE, PACKET_FILE, PACKET_ARCHIVE_DIR
    JUDGMENT_FILE = args.judgment_file
    PACKET_FILE = args.packet_file
    PACKET_ARCHIVE_DIR = args.packet_archive_dir
    payload = build_report()
    if args.output:
        save_json_atomic(args.output, payload)
    text = build_text_report(payload)
    if args.text:
        print(text)
    elif args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text)
        print("\n--- JSON ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
