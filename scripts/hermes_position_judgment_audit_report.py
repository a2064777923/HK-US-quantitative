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
MIN_POSITION_ATTENTION_EFFECTS = int(os.environ.get("HERMES_POSITION_MIN_ATTENTION_EFFECTS", "3"))
MAX_POSITION_ATTENTION_EFFECTS_REQUIRED = int(os.environ.get("HERMES_POSITION_MAX_ATTENTION_EFFECTS_REQUIRED", "4"))
VALID_DECISIONS = {"hold", "watch", "reduce", "exit", "trail_stop"}
ACTION_RANKS = {
    "hold": 0,
    "watch": 0,
    "risk_review": 1,
    "take_profit_or_trailing_stop_review": 2,
    "reduce_or_exit_review": 3,
    "exit_review": 4,
}
REQUIRED_CONTEXT_REVIEW_FLAGS = (
    "position_context_reviewed",
    "portfolio_risk_reviewed",
    "market_context_reviewed",
    "external_context_reviewed",
    "intraday_context_reviewed",
)


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


def review_thread_key_for(value):
    if not isinstance(value, dict):
        return ""
    explicit = str(value.get("review_thread_key") or "").strip()
    if explicit:
        return explicit
    role = str(value.get("role") or "").strip()
    portfolio_id = as_int(value.get("portfolio_id"))
    symbol = str(value.get("symbol") or "").strip().upper()
    if not role or portfolio_id is None or not symbol:
        return ""
    return f"{role}:{portfolio_id}:{symbol}"


def reviewed_action_from_id(review_id):
    parts = str(review_id or "").split(":")
    return parts[-1] if len(parts) >= 5 else ""


def reviewed_action_for_judgment(judgment):
    return str(
        (judgment or {}).get("reviewed_recommended_action")
        or (judgment or {}).get("recommended_action")
        or reviewed_action_from_id((judgment or {}).get("review_id"))
        or ""
    ).strip()


def action_rank(action):
    return ACTION_RANKS.get(str(action or "").strip(), 1)


def current_action_covered_by_judgment(judgment, item):
    current_action = str((item or {}).get("recommended_action") or reviewed_action_from_id((item or {}).get("review_id")) or "").strip()
    reviewed_action = reviewed_action_for_judgment(judgment)
    if not current_action:
        return True, []
    if not reviewed_action:
        return False, ["thread_match_missing_reviewed_action"]
    if action_rank(current_action) > action_rank(reviewed_action):
        return False, ["thread_match_current_action_escalated"]
    return True, []


def judgment_expiry_minutes(judgment):
    expiry = (judgment or {}).get("expiry_minutes", MAX_JUDGMENT_AGE_MINUTES)
    try:
        return int(expiry)
    except (TypeError, ValueError):
        return MAX_JUDGMENT_AGE_MINUTES


def judgment_is_expired(judgment, now):
    reviewed_at = intake.parse_time((judgment or {}).get("reviewed_at") or (judgment or {}).get("created_at"))
    if not reviewed_at:
        return False
    return now - reviewed_at > timedelta(minutes=judgment_expiry_minutes(judgment))


def packet_position_review_maps(packet):
    position_review = (packet or {}).get("position_review") or {}
    items = position_review.get("items") if isinstance(position_review, dict) else []
    by_id = {}
    for item in items or []:
        rid = str(item.get("review_id", "")).strip()
        if rid:
            by_id[rid] = item
    return by_id


def packet_position_review_thread_map(packet):
    position_review = (packet or {}).get("position_review") or {}
    items = position_review.get("items") if isinstance(position_review, dict) else []
    by_thread = {}
    for item in items or []:
        key = review_thread_key_for(item)
        if key and key not in by_thread:
            by_thread[key] = item
    return by_thread


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
    if not review_thread_key_for(judgment):
        reasons.append("missing_review_thread_key")
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


def user_action_advice_acknowledgement_reasons(judgment, item):
    if (item or {}).get("role") != "user":
        return []
    decision = str(judgment.get("decision", "")).strip().lower()
    if decision not in ("reduce", "exit", "trail_stop"):
        return []
    reasons = []
    if judgment.get("manual_only") is not True:
        reasons.append("user_action_advice_requires_manual_only_acknowledgement")
    return reasons


def select_review_item_for_judgment(
    judgment,
    packet_review_by_id,
    now=None,
    latest_review_by_id=None,
    latest_review_by_thread=None,
):
    review_id = str(judgment.get("review_id", "")).strip()
    packet_review_by_id = packet_review_by_id or {}
    latest_review_by_id = latest_review_by_id or {}
    latest_review_by_thread = latest_review_by_thread or {}

    if review_id and review_id in latest_review_by_id:
        item = latest_review_by_id[review_id]
        return item, {
            "match_type": "latest_review_id",
            "current_packet_coverage": True,
            "covered_review_id": item.get("review_id"),
            "thread_rejected_reasons": [],
        }

    thread_key = review_thread_key_for(judgment)
    if thread_key and thread_key in latest_review_by_thread and not judgment_is_expired(judgment, now or datetime.now()):
        item = latest_review_by_thread[thread_key]
        covered, rejected = current_action_covered_by_judgment(judgment, item)
        if covered:
            return item, {
                "match_type": "latest_review_thread_key",
                "current_packet_coverage": True,
                "covered_review_id": item.get("review_id"),
                "thread_rejected_reasons": [],
            }
        if review_id and review_id not in packet_review_by_id:
            return None, {
                "match_type": "none",
                "current_packet_coverage": False,
                "covered_review_id": None,
                "thread_rejected_reasons": rejected,
            }

    if review_id and review_id in packet_review_by_id:
        item = packet_review_by_id[review_id]
        return item, {
            "match_type": "packet_review_id",
            "current_packet_coverage": False,
            "covered_review_id": item.get("review_id"),
            "thread_rejected_reasons": [],
        }

    return None, {
        "match_type": "none",
        "current_packet_coverage": False,
        "covered_review_id": None,
        "thread_rejected_reasons": [],
    }


def context_review_reasons(judgment, item):
    if not isinstance((item or {}).get("context_digest"), dict):
        return []
    review = judgment.get("context_review")
    if not isinstance(review, dict):
        return ["context_review_missing"]
    reasons = []
    for flag in REQUIRED_CONTEXT_REVIEW_FLAGS:
        if review.get(flag) is not True:
            reasons.append(f"context_review_missing_{flag}")
    notes = review.get("notes")
    if notes is not None and not isinstance(notes, list):
        reasons.append("context_review_notes_invalid")
    return reasons


def position_attention_for_item(item):
    digest = (item or {}).get("context_digest") if isinstance((item or {}).get("context_digest"), dict) else {}
    values = digest.get("position_attention") if isinstance(digest.get("position_attention"), list) else []
    return [str(value).strip() for value in values if str(value).strip()]


def required_position_attention_effect_codes(attention):
    attention = [str(value).strip() for value in attention or [] if str(value).strip()]
    if not attention:
        return set()
    max_required = max(as_int(MAX_POSITION_ATTENTION_EFFECTS_REQUIRED, 4) or 4, 1)
    min_required = min(max(as_int(MIN_POSITION_ATTENTION_EFFECTS, 3) or 3, 1), len(attention), max_required)
    priority_prefixes = (
        "high_urgency_",
        "position_exit_or_reduce_",
        "position_dynamic_management_",
        "position_intraday_",
        "position_risk_off_",
    )
    selected = []
    for code in attention:
        if len(selected) >= min_required:
            break
        if code.startswith(priority_prefixes):
            selected.append(code)
    for code in attention:
        if len(selected) >= min_required:
            break
        if code not in selected:
            selected.append(code)
    return set(selected)


def position_attention_acknowledgement_reasons(judgment, item):
    attention = position_attention_for_item(item)
    if not attention:
        return []
    reasons = []
    if judgment.get("position_attention_acknowledged") is not True:
        reasons.append("missing_position_attention_acknowledgement")
    acknowledged = {
        str(value).strip()
        for value in (judgment.get("position_attention_codes") or [])
        if str(value).strip()
    } if isinstance(judgment.get("position_attention_codes"), list) else set()
    if not set(attention).issubset(acknowledged):
        reasons.append("position_attention_codes_missing_or_unmatched")
    notes = judgment.get("position_attention_notes")
    if isinstance(notes, str):
        notes_present = bool(notes.strip())
    else:
        notes_present = isinstance(notes, list) and bool(notes)
    if not notes_present:
        reasons.append("position_attention_notes_missing")
    effects = judgment.get("position_attention_effects")
    if not isinstance(effects, list) or not effects:
        reasons.append("position_attention_effects_missing")
    else:
        effect_codes = {
            str(effect.get("code") or "").strip()
            for effect in effects
            if isinstance(effect, dict) and str(effect.get("code") or "").strip()
        }
        required_effect_codes = required_position_attention_effect_codes(attention)
        if acknowledged:
            required_effect_codes &= acknowledged
        missing_required_effects = required_effect_codes - effect_codes
        if missing_required_effects:
            reasons.append("position_attention_effects_missing_or_unmatched")
        for effect in effects:
            if not isinstance(effect, dict):
                reasons.append("position_attention_effect_invalid")
                continue
            if not str(effect.get("effect") or "").strip():
                reasons.append("position_attention_effect_detail_missing")
            if not str(effect.get("decision_impact") or "").strip():
                reasons.append("position_attention_effect_decision_impact_missing")
    return reasons


def audit_judgment(
    judgment,
    packet,
    review_by_id,
    now=None,
    packet_source="latest_packet",
    packet_reasons=None,
    latest_review_by_id=None,
    latest_review_by_thread=None,
):
    now = now or datetime.now()
    review_id = str(judgment.get("review_id", "")).strip()
    reasons = validate_judgment_contract(judgment)
    item, match = select_review_item_for_judgment(
        judgment,
        review_by_id,
        now=now,
        latest_review_by_id=latest_review_by_id,
        latest_review_by_thread=latest_review_by_thread,
    )
    reasons.extend(match.get("thread_rejected_reasons") or [])
    if match.get("current_packet_coverage") and match.get("match_type") == "latest_review_id":
        packet_source = "latest_packet_review_id"
    elif match.get("current_packet_coverage") and match.get("match_type") == "latest_review_thread_key":
        packet_source = "latest_packet_thread_key"
    else:
        reasons.extend(packet_reasons or [])
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
        reasons.extend(context_review_reasons(judgment, item))
        reasons.extend(position_attention_acknowledgement_reasons(judgment, item))
        reasons.extend(user_action_advice_acknowledgement_reasons(judgment, item))
        if item.get("urgency") == "high" and decision in ("hold", "watch"):
            if len(judgment.get("opposing_factors") or []) < 2 or len(judgment.get("risk_notes") or []) < 2:
                reasons.append("high_urgency_hold_or_watch_requires_strong_rationale")
                reasons.append("high_urgency_hold_missing_opposing_detail")

    reviewed_at = intake.parse_time(judgment.get("reviewed_at") or judgment.get("created_at"))
    if reviewed_at:
        if judgment_is_expired(judgment, now):
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
        "review_thread_key": review_thread_key_for(judgment),
        "match_type": match.get("match_type"),
        "covered_review_id": match.get("covered_review_id") or review_id,
        "current_packet_coverage": bool(match.get("current_packet_coverage")),
        "thread_rejected_reasons": match.get("thread_rejected_reasons") or [],
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
    }


def duplicate_review_counts(judgments):
    counts = Counter(str(item.get("review_id", "")).strip() for item in judgments if item.get("review_id"))
    return {rid: count for rid, count in counts.items() if count > 1}


def row_in_current_packet_scope(row, latest_packet_id):
    latest_packet_id = str(latest_packet_id or "").strip()
    packet_id = str((row or {}).get("packet_id") or "").strip()
    if not latest_packet_id:
        return True
    if not packet_id:
        return True
    return packet_id == latest_packet_id


def duplicate_review_counts_from_rows(rows):
    counts = Counter(str(row.get("covered_review_id") or row.get("review_id") or "").strip() for row in rows if row.get("covered_review_id") or row.get("review_id"))
    return {rid: count for rid, count in counts.items() if count > 1}


def build_recommendations(rows, reason_counts, empty_recommendation="no_position_judgments_observed_yet"):
    if not rows:
        return [empty_recommendation]
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
    if reason_counts.get("context_review_missing") or any(
        reason.startswith("context_review_missing_") for reason in reason_counts
    ):
        recs.append("position_judgments_require_context_review_for_enriched_items")
    if reason_counts.get("missing_position_attention_acknowledgement") or reason_counts.get(
        "position_attention_codes_missing_or_unmatched"
    ) or reason_counts.get("position_attention_notes_missing") or reason_counts.get(
        "position_attention_effects_missing"
    ) or reason_counts.get("position_attention_effects_missing_or_unmatched") or reason_counts.get(
        "position_attention_effect_invalid"
    ) or reason_counts.get("position_attention_effect_detail_missing") or reason_counts.get(
        "position_attention_effect_decision_impact_missing"
    ):
        recs.append("position_attention_requires_structured_acknowledgement")
    if reason_counts.get("user_action_advice_requires_manual_only_acknowledgement"):
        recs.append("user_position_action_advice_requires_manual_only_acknowledgement")
    if reason_counts.get("judgment_expired"):
        recs.append("refresh_expired_position_judgments")
    if reason_counts.get("duplicate_position_judgments_for_review"):
        recs.append("dedupe_position_judgments_keep_latest_review_id_only")
    if not recs:
        recs.append("position_judgment_audit_clean_continue_advisory_review")
    return recs


def packet_position_judgment_worklist_items(packet):
    worklist = (packet or {}).get("position_judgment_worklist")
    items = worklist.get("items") if isinstance(worklist, dict) else []
    return [item for item in items or [] if isinstance(item, dict) and item.get("review_id")]


def coverage_summary(review_by_id, rows, worklist_items=None):
    current_pass_judged_ids = {
        str(row.get("covered_review_id") or row.get("review_id") or "").strip()
        for row in rows
        if row.get("audit_scope") == "current_packet"
        and row.get("status") == "PASS"
        and str(row.get("covered_review_id") or row.get("review_id") or "").strip() in (review_by_id or {})
    }
    current_failed_judged_ids = {
        str(row.get("covered_review_id") or row.get("review_id") or "").strip()
        for row in rows
        if row.get("audit_scope") == "current_packet"
        and row.get("status") != "PASS"
        and str(row.get("covered_review_id") or row.get("review_id") or "").strip() in (review_by_id or {})
    }
    high_priority = [
        item
        for item in (review_by_id or {}).values()
        if str(item.get("urgency") or "").lower() == "high"
    ]
    unjudged_high = [
        item
        for item in high_priority
        if str(item.get("review_id") or "").strip() not in current_pass_judged_ids
    ]
    unjudged_high_ids = {
        str(item.get("review_id") or "").strip()
        for item in unjudged_high
        if str(item.get("review_id") or "").strip()
    }
    work_items = [
        item
        for item in (worklist_items or [])
        if str(item.get("review_id") or "").strip() in unjudged_high_ids
    ]
    return {
        "schema": "hermes_position_judgment_coverage_v1",
        "position_review_item_count": len(review_by_id or {}),
        "judged_review_count": len(current_pass_judged_ids),
        "failed_current_judgment_review_count": len(current_failed_judged_ids),
        "unjudged_review_count": max(len(review_by_id or {}) - len(current_pass_judged_ids), 0),
        "high_urgency_review_count": len(high_priority),
        "unjudged_high_urgency_review_count": len(unjudged_high),
        "unjudged_high_urgency_examples": [
            {
                "review_id": item.get("review_id"),
                "portfolio_id": item.get("portfolio_id"),
                "role": item.get("role"),
                "symbol": item.get("symbol"),
                "recommended_action": item.get("recommended_action"),
                "urgency": item.get("urgency"),
            }
            for item in unjudged_high[:20]
        ],
        "unjudged_high_urgency_work_items": work_items[:20],
    }


def append_coverage_recommendations(recs, coverage):
    if (coverage or {}).get("unjudged_high_urgency_review_count"):
        rec = (
            "write_position_judgments_for_high_urgency_reviews:"
            + str(coverage["unjudged_high_urgency_review_count"])
        )
        if rec not in recs:
            recs.append(rec)
    return recs


def build_report(judgments=None, packet=None, now=None, packet_archive_dir=PACKET_ARCHIVE_DIR):
    now = now or datetime.now()
    judgments = load_jsonl_or_json(JUDGMENT_FILE) if judgments is None else judgments
    latest_packet = load_json_file(PACKET_FILE, {}) if packet is None else packet
    latest_packet_id = latest_packet.get("packet_id") if isinstance(latest_packet, dict) else None
    latest_review_by_id = packet_position_review_maps(latest_packet)
    latest_review_by_thread = packet_position_review_thread_map(latest_packet)
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
                latest_review_by_id=latest_review_by_id,
                latest_review_by_thread=latest_review_by_thread,
            )
        )
    current_rows = []
    historical_rows = []
    for row in rows:
        if row.get("current_packet_coverage") or row_in_current_packet_scope(row, latest_packet_id):
            row["audit_scope"] = "current_packet"
            current_rows.append(row)
        else:
            row["audit_scope"] = "historical_packet"
            historical_rows.append(row)

    reason_counts = Counter()
    current_reason_counts = Counter()
    historical_reason_counts = Counter()
    decision_counts = Counter()
    status_counts = Counter()
    current_status_counts = Counter()
    historical_status_counts = Counter()
    for row in rows:
        status_counts[row["status"]] += 1
        decision_counts[row["decision"] or "missing"] += 1
        for reason in row["reasons"]:
            reason_counts[reason] += 1
    for row in current_rows:
        current_status_counts[row["status"]] += 1
        for reason in row["reasons"]:
            current_reason_counts[reason] += 1
    for row in historical_rows:
        historical_status_counts[row["status"]] += 1
        for reason in row["reasons"]:
            historical_reason_counts[reason] += 1

    duplicates = duplicate_review_counts_from_rows(current_rows)
    historical_duplicates = duplicate_review_counts_from_rows(historical_rows)
    if duplicates:
        for row in rows:
            if row.get("review_id") in duplicates:
                row["status"] = "FAIL"
                row["reasons"] = sorted(
                    set((row.get("reasons") or []) + ["duplicate_position_judgments_for_review"])
                )
        reason_counts = Counter()
        current_reason_counts = Counter()
        historical_reason_counts = Counter()
        status_counts = Counter()
        current_status_counts = Counter()
        historical_status_counts = Counter()
        for row in rows:
            status_counts[row["status"]] += 1
            for reason in row["reasons"]:
                reason_counts[reason] += 1
            if row.get("audit_scope") == "current_packet":
                current_status_counts[row["status"]] += 1
                for reason in row["reasons"]:
                    current_reason_counts[reason] += 1
            else:
                historical_status_counts[row["status"]] += 1
                for reason in row["reasons"]:
                    historical_reason_counts[reason] += 1

    coverage = coverage_summary(
        latest_review_by_id,
        rows,
        worklist_items=packet_position_judgment_worklist_items(latest_packet),
    )
    status = "FAIL" if current_status_counts.get("FAIL") or duplicates else "OK"
    if status == "OK" and coverage.get("unjudged_high_urgency_review_count"):
        status = "WARN"
    if status == "OK" and (historical_status_counts.get("FAIL") or historical_duplicates):
        status = "WARN"
    recommendations = append_coverage_recommendations(
        build_recommendations(
            current_rows,
            current_reason_counts,
            empty_recommendation=(
                "no_current_packet_position_judgments_observed"
                if rows
                else "no_position_judgments_observed_yet"
            ),
        ),
        coverage,
    )
    historical_recommendations = (
        build_recommendations(
            historical_rows,
            historical_reason_counts,
            empty_recommendation="no_historical_position_judgments_observed",
        )
        if historical_rows
        else []
    )

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
            "current_status_counts": dict(current_status_counts),
            "historical_status_counts": dict(historical_status_counts),
            "decision_counts": dict(decision_counts),
            "reason_counts": dict(reason_counts),
            "current_reason_counts": dict(current_reason_counts),
            "historical_reason_counts": dict(historical_reason_counts),
            "duplicate_review_ids": duplicates,
            "historical_duplicate_review_ids": historical_duplicates,
            "packet_source_counts": dict(packet_source_counts),
            "current_packet_scope_count": len(current_rows),
            "historical_packet_scope_count": len(historical_rows),
        },
        "coverage": coverage,
        "judgments": rows[-100:],
        "recommendations": recommendations,
        "historical_recommendations": historical_recommendations,
    }


def build_text_report(payload):
    counts = payload["counts"]
    coverage = payload.get("coverage") or {}
    lines = [
        f"Hermes position judgment audit {payload['generated_at']}",
        (
            f"judgments={counts['judgment_count']} position_reviews={counts['position_review_item_count']} "
            f"status={counts['status_counts']} decisions={counts['decision_counts']}"
        ),
        (
            f"coverage=judged:{coverage.get('judged_review_count')} "
            f"unjudged:{coverage.get('unjudged_review_count')} "
            f"high_unjudged:{coverage.get('unjudged_high_urgency_review_count')}"
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
