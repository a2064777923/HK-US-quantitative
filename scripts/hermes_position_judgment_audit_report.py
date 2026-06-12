#!/usr/bin/env python3
"""Read-only audit of Hermes advisory judgments for position_review items."""
import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

try:
    import rt_order_intake as intake
except ImportError:
    from scripts import rt_order_intake as intake


JUDGMENT_FILE = os.environ.get("HERMES_POSITION_JUDGMENT_FILE", "/tmp/hermes_position_judgments.jsonl")
PACKET_FILE = os.environ.get("HERMES_REVIEW_PACKET_FILE", "/tmp/hermes_signal_review_packet.json")
PACKET_ARCHIVE_DIR = os.environ.get("HERMES_REVIEW_PACKET_ARCHIVE_DIR", "/tmp/hermes_review_packet_archive")
REPORT_FILE = os.environ.get(
    "HERMES_POSITION_JUDGMENT_AUDIT_FILE",
    "/tmp/hermes_position_judgment_audit_report.json",
)
MAX_JUDGMENT_AGE_MINUTES = int(os.environ.get("HERMES_POSITION_MAX_JUDGMENT_AGE_MINUTES", "1440"))
VALID_DECISIONS = {"hold", "watch", "reduce", "exit", "trail_stop"}


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


def load_jsonl_or_json(path):
    if not os.path.exists(path):
        return []
    try:
        raw = open(path, "r", encoding="utf-8").read().strip()
    except Exception:
        return []
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, list):
            return [item for item in loaded if isinstance(item, dict)]
        if isinstance(loaded, dict):
            for key in ("judgments", "items", "decisions"):
                if isinstance(loaded.get(key), list):
                    return [item for item in loaded[key] if isinstance(item, dict)]
            return [loaded]
    except json.JSONDecodeError:
        pass

    judgments = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            judgments.append(item)
    return judgments


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


def as_int(value, default=None):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def packet_position_review_maps(packet):
    position_review = (packet or {}).get("position_review") or {}
    items = position_review.get("items") if isinstance(position_review, dict) else []
    by_id = {}
    for item in items or []:
        rid = str(item.get("review_id", "")).strip()
        if rid:
            by_id[rid] = item
    return by_id


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


def validate_judgment_contract(judgment):
    reasons = []
    if judgment.get("schema") != "hermes_position_judgment_v1":
        reasons.append("schema_invalid")
    if not str(judgment.get("packet_id", "")).strip():
        reasons.append("missing_packet_id")
    if not str(judgment.get("review_id", "")).strip():
        reasons.append("missing_review_id")
    if not str(judgment.get("symbol", "")).strip():
        reasons.append("missing_symbol")
    if as_int(judgment.get("portfolio_id")) is None:
        reasons.append("portfolio_id_invalid")
    if str(judgment.get("role", "")).strip() not in ("user", "simulation"):
        reasons.append("role_invalid")
    decision = str(judgment.get("decision", "")).strip().lower()
    if decision not in VALID_DECISIONS:
        reasons.append("decision_invalid")
    confidence = as_float(judgment.get("confidence"))
    if confidence is None or confidence < 0 or confidence > 1:
        reasons.append("confidence_invalid")
    if judgment.get("advisory_only") is not True:
        reasons.append("advisory_only_must_be_true")
    if judgment.get("submits_orders") is not False:
        reasons.append("submits_orders_must_be_false")
    reviewed_at = intake.parse_time(judgment.get("reviewed_at") or judgment.get("created_at"))
    if not reviewed_at:
        reasons.append("reviewed_at_invalid")
    if not isinstance(judgment.get("supporting_factors"), list) or not judgment.get("supporting_factors"):
        reasons.append("supporting_factors_missing")
    if not isinstance(judgment.get("opposing_factors"), list) or not judgment.get("opposing_factors"):
        reasons.append("opposing_factors_missing")
    if not isinstance(judgment.get("risk_notes"), list) or not judgment.get("risk_notes"):
        reasons.append("risk_notes_missing")
    if decision in ("reduce", "exit") and as_float(judgment.get("max_exit_quantity"), 0) < 0:
        reasons.append("max_exit_quantity_invalid")
    return reasons


def audit_judgment(judgment, packet, review_by_id, now=None, packet_source="latest_packet", packet_reasons=None):
    now = now or datetime.now()
    review_id = str(judgment.get("review_id", "")).strip()
    reasons = validate_judgment_contract(judgment)
    reasons.extend(packet_reasons or [])
    item = review_by_id.get(review_id)
    decision = str(judgment.get("decision", "")).strip().lower()
    if not item:
        reasons.append("orphan_position_judgment_not_in_packet")
    else:
        if str(judgment.get("symbol", "")).upper() != str(item.get("symbol", "")).upper():
            reasons.append("symbol_mismatch_with_review_item")
        if as_int(judgment.get("portfolio_id")) != as_int(item.get("portfolio_id")):
            reasons.append("portfolio_id_mismatch_with_review_item")
        if str(judgment.get("role", "")).strip() != str(item.get("role", "")).strip():
            reasons.append("role_mismatch_with_review_item")
        item_policy = item.get("execution_policy") or {}
        if item_policy.get("submits_orders") is not False:
            reasons.append("review_item_execution_policy_not_review_only")
        if item.get("role") == "user" and decision in ("reduce", "exit", "trail_stop"):
            reasons.append("user_position_decision_must_remain_advice_only")
        if item.get("urgency") == "high" and decision in ("hold", "watch"):
            if len(judgment.get("opposing_factors") or []) < 2 or len(judgment.get("risk_notes") or []) < 2:
                reasons.append("high_urgency_hold_or_watch_requires_strong_rationale")
                reasons.append("high_urgency_hold_missing_opposing_detail")

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
        "review_id": review_id,
        "portfolio_id": as_int(judgment.get("portfolio_id")),
        "role": str(judgment.get("role", "")).strip(),
        "symbol": str(judgment.get("symbol", "")).strip().upper(),
        "decision": decision,
        "confidence": as_float(judgment.get("confidence")),
        "reviewed_at": judgment.get("reviewed_at") or judgment.get("created_at"),
        "packet_id": str(judgment.get("packet_id", "")).strip(),
        "packet_source": packet_source,
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
    }


def duplicate_review_counts(judgments):
    counts = Counter(str(item.get("review_id", "")).strip() for item in judgments if item.get("review_id"))
    return {rid: count for rid, count in counts.items() if count > 1}


def build_recommendations(rows, reason_counts):
    if not rows:
        return ["no_position_judgments_observed_yet"]
    recs = []
    critical = (
        "schema_invalid",
        "orphan_position_judgment_not_in_packet",
        "advisory_only_must_be_true",
        "submits_orders_must_be_false",
        "symbol_mismatch_with_review_item",
        "portfolio_id_mismatch_with_review_item",
        "role_mismatch_with_review_item",
    )
    for reason in critical:
        if reason_counts.get(reason):
            recs.append(f"fix_position_judgments:{reason}")
    if reason_counts.get("judgment_missing_packet_id") or reason_counts.get("missing_packet_id"):
        recs.append("include_packet_id_in_position_judgments")
    if reason_counts.get("packet_archive_missing_for_packet_id"):
        recs.append("retain_packet_archive_for_position_judgment_audit")
    if reason_counts.get("high_urgency_hold_or_watch_requires_strong_rationale"):
        recs.append("review_high_urgency_hold_watch_rationale")
    if reason_counts.get("judgment_expired"):
        recs.append("refresh_expired_position_judgments")
    if reason_counts.get("duplicate_position_judgments_for_review"):
        recs.append("dedupe_position_judgments_keep_latest_review_id_only")
    if not recs:
        recs.append("position_judgment_audit_clean_continue_advisory_review")
    return recs


def build_report(judgments=None, packet=None, now=None, packet_archive_dir=PACKET_ARCHIVE_DIR):
    now = now or datetime.now()
    judgments = load_jsonl_or_json(JUDGMENT_FILE) if judgments is None else judgments
    latest_packet = load_json_file(PACKET_FILE, {}) if packet is None else packet
    latest_review_by_id = packet_position_review_maps(latest_packet)
    rows = []
    packet_source_counts = Counter()
    for judgment in judgments:
        judgment_packet, packet_source, packet_reasons = packet_for_judgment(
            judgment,
            latest_packet,
            archive_dir=packet_archive_dir,
        )
        packet_source_counts[packet_source] += 1
        review_by_id = packet_position_review_maps(judgment_packet)
        rows.append(
            audit_judgment(
                judgment,
                judgment_packet,
                review_by_id,
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

    duplicates = duplicate_review_counts(judgments)
    if duplicates:
        for row in rows:
            if row.get("review_id") in duplicates:
                row["status"] = "FAIL"
                row["reasons"] = sorted(
                    set((row.get("reasons") or []) + ["duplicate_position_judgments_for_review"])
                )
        reason_counts = Counter()
        status_counts = Counter()
        for row in rows:
            status_counts[row["status"]] += 1
            for reason in row["reasons"]:
                reason_counts[reason] += 1

    status = "FAIL" if status_counts.get("FAIL") or duplicates else "OK"

    return {
        "schema": "hermes_position_judgment_audit_report_v1",
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
            "position_review_item_count": len(latest_review_by_id),
            "status_counts": dict(status_counts),
            "decision_counts": dict(decision_counts),
            "reason_counts": dict(reason_counts),
            "duplicate_review_ids": duplicates,
            "packet_source_counts": dict(packet_source_counts),
        },
        "judgments": rows[-100:],
        "recommendations": build_recommendations(rows, reason_counts),
    }


def build_text_report(payload):
    counts = payload["counts"]
    lines = [
        f"Hermes position judgment audit {payload['generated_at']}",
        (
            f"judgments={counts['judgment_count']} position_reviews={counts['position_review_item_count']} "
            f"status={counts['status_counts']} decisions={counts['decision_counts']}"
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
