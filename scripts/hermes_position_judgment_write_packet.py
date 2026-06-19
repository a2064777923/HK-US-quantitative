#!/usr/bin/env python3
"""Compact read-only packet for Hermes advisory position judgment writing."""
import argparse
import json
import os
from datetime import datetime


PACKET_FILE = os.environ.get("HERMES_REVIEW_PACKET_FILE", "/tmp/hermes_signal_review_packet.json")
AUDIT_FILE = os.environ.get(
    "HERMES_POSITION_JUDGMENT_AUDIT_FILE",
    "/tmp/hermes_position_judgment_audit_report.json",
)
REPORT_FILE = os.environ.get(
    "HERMES_POSITION_JUDGMENT_WRITE_PACKET_FILE",
    "/tmp/hermes_position_judgment_write_packet.json",
)
JUDGMENT_FILE = os.environ.get("HERMES_POSITION_JUDGMENT_FILE", "/tmp/hermes_position_judgments.jsonl")
DEFAULT_LIMIT = int(os.environ.get("HERMES_POSITION_JUDGMENT_WRITE_PACKET_LIMIT", "20"))


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    tmp = f"{path}.{os.getpid()}.{datetime.now().strftime('%Y%m%d%H%M%S%f')}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def load_json_file(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else default
    except Exception:
        return default


def safe_dict(value):
    return value if isinstance(value, dict) else {}


def safe_list(value):
    return value if isinstance(value, list) else []


def urgency_rank(value):
    return {"high": 0, "medium": 1, "low": 2}.get(str(value or "").strip().lower(), 9)


def role_rank(value):
    return {"user": 0, "simulation": 1}.get(str(value or "").strip().lower(), 9)


def review_sort_key(item):
    return (
        urgency_rank(item.get("urgency")),
        role_rank(item.get("role")),
        str(item.get("symbol") or ""),
        str(item.get("review_id") or ""),
    )


def compact_action_plan(context_summary):
    context_summary = safe_dict(context_summary)
    dynamic = safe_dict(context_summary.get("dynamic_management"))
    intraday = safe_dict(context_summary.get("intraday_position_evidence"))
    position = safe_dict(context_summary.get("position"))
    latest_signal = safe_dict(context_summary.get("latest_signal"))
    return {
        "position": {
            "quantity": position.get("quantity"),
            "current_price": position.get("current_price"),
            "unrealized_pnl_pct": position.get("unrealized_pnl_pct"),
            "latest_daily_change_pct": position.get("latest_daily_change_pct"),
            "stop_distance_pct": position.get("stop_distance_pct"),
            "price_snapshot_age_hours": position.get("price_snapshot_age_hours"),
        },
        "latest_signal": {
            "side": latest_signal.get("side"),
            "score": latest_signal.get("score"),
            "trade_date": latest_signal.get("trade_date"),
            "risk_flags": safe_list(latest_signal.get("risk_flags"))[:6],
        },
        "dynamic_management": {
            "target_status": dynamic.get("target_status"),
            "review_focus": safe_list(dynamic.get("review_focus"))[:4],
            "distance_to_signal_take_profit_pct": dynamic.get("distance_to_signal_take_profit_pct"),
            "distance_above_signal_stop_loss_pct": dynamic.get("distance_above_signal_stop_loss_pct"),
            "price_snapshot_fresh": dynamic.get("price_snapshot_fresh"),
        },
        "intraday_position_evidence": {
            "alignment": intraday.get("alignment"),
            "action_intent": intraday.get("action_intent"),
            "status": intraday.get("status"),
            "session_momentum": intraday.get("session_momentum"),
            "session_change_pct": intraday.get("session_change_pct"),
            "support_codes": safe_list(intraday.get("support_codes"))[:4],
            "challenge_codes": safe_list(intraday.get("challenge_codes"))[:4],
            "limit_codes": safe_list(intraday.get("limit_codes"))[:4],
        },
    }


def pending_work_items(packet, audit, limit=DEFAULT_LIMIT):
    worklist = safe_dict(packet.get("position_judgment_worklist"))
    rows = [
        item
        for item in safe_list(worklist.get("items"))
        if isinstance(item, dict) and item.get("review_id")
    ]
    coverage = safe_dict(audit.get("coverage"))
    unjudged_ids = {
        str(item.get("review_id") or "").strip()
        for item in safe_list(coverage.get("unjudged_high_urgency_examples"))
        if isinstance(item, dict) and str(item.get("review_id") or "").strip()
    }
    if unjudged_ids:
        rows = [item for item in rows if str(item.get("review_id") or "").strip() in unjudged_ids]
    else:
        rows = [item for item in rows if str(item.get("urgency") or "").strip().lower() == "high"]
    rows.sort(key=review_sort_key)
    return rows[: max(int(limit or 0), 0)]


def build_work_item(item):
    required = safe_dict(item.get("required_output_fields"))
    context = safe_dict(item.get("context_summary"))
    attention_codes = safe_list(item.get("required_attention_codes"))
    return {
        "review_id": item.get("review_id"),
        "review_thread_key": item.get("review_thread_key"),
        "portfolio_id": item.get("portfolio_id"),
        "role": item.get("role"),
        "symbol": item.get("symbol"),
        "market": item.get("market"),
        "urgency": item.get("urgency"),
        "recommended_action": item.get("recommended_action"),
        "allowed_decisions": safe_list(item.get("allowed_decisions")),
        "required_attention_codes": attention_codes,
        "required_detailed_effect_codes": safe_list(
            safe_dict(required.get("position_attention_effect_policy")).get("detailed_effects_required_for_codes")
        ),
        "context_summary": compact_action_plan(context),
        "required_output_fields": {
            key: required.get(key)
            for key in (
                "schema",
                "packet_id",
                "review_id",
                "review_thread_key",
                "reviewed_recommended_action",
                "portfolio_id",
                "role",
                "symbol",
                "reviewer",
                "advisory_only",
                "submits_orders",
                "manual_only",
                "context_review",
                "position_attention_acknowledged",
                "position_attention_codes",
                "position_attention_effect_policy",
                "append_jsonl_object_to",
            )
            if required.get(key) not in (None, "", [], {})
        },
        "must_complete_fields": [
            "decision",
            "confidence",
            "reviewed_at",
            "supporting_factors",
            "opposing_factors",
            "risk_notes",
            "context_review.notes",
            "position_attention_notes",
            "position_attention_effects",
        ],
        "decision_guidance": [
            "Choose hold/watch/reduce/exit/trail_stop only after reviewing context_summary and full packet item if needed.",
            "For role=user, reduce/exit/trail_stop is manual-only advice and must set manual_only=true.",
            "Do not write order_submission=true, do not call order intake, and do not mutate positions.",
        ],
    }


def build_report(packet=None, audit=None, limit=DEFAULT_LIMIT, judgment_file=JUDGMENT_FILE):
    packet = packet if isinstance(packet, dict) else load_json_file(PACKET_FILE, {})
    audit = audit if isinstance(audit, dict) else load_json_file(AUDIT_FILE, {})
    coverage = safe_dict(audit.get("coverage"))
    raw_items = pending_work_items(packet, audit, limit=limit)
    items = [build_work_item(item) for item in raw_items]
    return {
        "schema": "hermes_position_judgment_write_packet_v1",
        "generated_at": now_iso(),
        "status": "ACTION_REQUIRED" if items else "OK",
        "source": {
            "read_only": True,
            "draft_only": True,
            "submits_orders": False,
            "changes_portfolio": False,
            "changes_strategy": False,
            "writes_judgments": False,
            "packet_file": PACKET_FILE,
            "audit_file": AUDIT_FILE,
            "packet_id": packet.get("packet_id"),
        },
        "judgment_file": judgment_file,
        "coverage": {
            "position_review_item_count": coverage.get("position_review_item_count"),
            "judged_review_count": coverage.get("judged_review_count"),
            "unjudged_high_urgency_review_count": coverage.get("unjudged_high_urgency_review_count"),
        },
        "summary": {
            "pending_item_count": len(raw_items),
            "included_item_count": len(items),
            "limit": limit,
        },
        "append_contract": {
            "append_jsonl_object_to": judgment_file,
            "schema": "hermes_position_judgment_v1",
            "required_after_hermes_review": [
                "schema",
                "packet_id",
                "review_id",
                "review_thread_key",
                "reviewed_recommended_action",
                "portfolio_id",
                "role",
                "symbol",
                "decision",
                "confidence",
                "reviewed_at",
                "reviewer",
                "advisory_only=true",
                "submits_orders=false",
                "supporting_factors[]",
                "opposing_factors[]",
                "risk_notes[]",
                "context_review",
                "position_attention_acknowledged",
                "position_attention_codes[]",
                "position_attention_notes",
                "position_attention_effects[]",
            ],
        },
        "items": items,
        "validation": {
            "after_append_command": (
                "/usr/bin/python3 /root/hermes_position_judgment_audit_report.py "
                "--output /tmp/hermes_position_judgment_audit_report.json --text"
            ),
            "success_condition": "coverage.unjudged_high_urgency_review_count decreases and appended rows PASS audit",
        },
        "hard_rules": [
            "This packet is a compact write aid only; it is not a completed judgment.",
            "Hermes must replace every placeholder and append completed JSONL objects itself.",
            "Do not submit orders, call rt_order_intake.py, mutate portfolio rows, or change strategy from this path.",
            "Templates and required_output_fields are not judgments.",
        ],
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Hermes position judgment write packet {payload.get('generated_at')} status={payload.get('status')}",
        (
            f"pending={summary.get('pending_item_count')} included={summary.get('included_item_count')} "
            f"judgment_file={payload.get('judgment_file')}"
        ),
    ]
    for item in payload.get("items") or []:
        ctx = safe_dict(item.get("context_summary"))
        position = safe_dict(ctx.get("position"))
        dynamic = safe_dict(ctx.get("dynamic_management"))
        intraday = safe_dict(ctx.get("intraday_position_evidence"))
        lines.append(
            "{role}:{symbol} {action} urgency={urgency} pnl={pnl} day={day} stop_dist={stop} "
            "dynamic={dynamic} intraday={intraday}".format(
                role=item.get("role"),
                symbol=item.get("symbol"),
                action=item.get("recommended_action"),
                urgency=item.get("urgency"),
                pnl=position.get("unrealized_pnl_pct"),
                day=position.get("latest_daily_change_pct"),
                stop=position.get("stop_distance_pct"),
                dynamic=dynamic.get("target_status"),
                intraday=intraday.get("alignment"),
            )
        )
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet-file", default=PACKET_FILE)
    parser.add_argument("--audit-file", default=AUDIT_FILE)
    parser.add_argument("--judgment-file", default=JUDGMENT_FILE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--text", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    global PACKET_FILE, AUDIT_FILE
    PACKET_FILE = args.packet_file
    AUDIT_FILE = args.audit_file
    payload = build_report(
        packet=load_json_file(args.packet_file, {}),
        audit=load_json_file(args.audit_file, {}),
        limit=args.limit,
        judgment_file=args.judgment_file,
    )
    if args.output:
        save_json_atomic(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.text:
        print(build_text_report(payload))
    else:
        print(build_text_report(payload))
        print("\n--- JSON ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
