#!/usr/bin/env python3
"""Read-only audit of simulation loss postmortem notes."""
import argparse
import json
import os
from collections import Counter
from datetime import datetime


SIMULATION_PERFORMANCE_FILE = os.environ.get(
    "SIMULATION_PERFORMANCE_REPORT_FILE",
    "/tmp/simulation_performance_report.json",
)
NOTE_FILE = os.environ.get("SIMULATION_POSTMORTEM_NOTE_FILE", "/tmp/simulation_postmortem_notes.jsonl")
REPORT_FILE = os.environ.get(
    "SIMULATION_POSTMORTEM_AUDIT_REPORT_FILE",
    "/tmp/simulation_postmortem_audit_report.json",
)

DEFAULT_REQUIRED_LEARNING_FIELDS = (
    "symbol",
    "closed_at",
    "entry_signal_id_or_trade_id",
    "exit_reason",
    "failure_category",
    "market_context_status",
    "intraday_context_status",
    "event_or_news_context_ids",
    "fundamentals_context_status",
    "source_reliability_status",
    "lesson",
    "proposed_change",
    "promotion_gate",
)
REQUIRED_NOTE_FIELDS = (
    "schema",
    "portfolio_id",
    "symbol",
    "target_type",
    "reviewed_at",
    "reviewer",
    "read_only",
    "submits_orders",
    "changes_strategy",
    "changes_portfolio",
    "auto_apply",
) + DEFAULT_REQUIRED_LEARNING_FIELDS

CLOSED_TARGET_TYPES = {"closed_trade", "worst_closed_symbol", "symbol_postmortem"}
OPEN_TARGET_TYPES = {"open_position", "high_risk_open_position", "position_risk"}
PLACEHOLDER_STRINGS = {
    "iso-8601 datetime",
    "reviewed market context status",
    "reviewed intraday context status",
    "reviewed fundamentals status",
    "reviewed source reliability status",
    "specific lesson learned from this loss or risk item",
}


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
    default = {} if default is None else default
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else default
    except Exception:
        return default


def load_jsonl_or_json(path):
    if not path or not os.path.exists(path):
        return [], [f"postmortem_note_file_missing:{path}"]
    try:
        raw = open(path, "r", encoding="utf-8").read().strip()
    except Exception as exc:
        return [], [f"postmortem_note_file_read_failed:{exc}"]
    if not raw:
        return [], [f"postmortem_note_file_empty:{path}"]
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, list):
            return [item for item in loaded if isinstance(item, dict)], []
        if isinstance(loaded, dict):
            for key in ("notes", "items", "postmortems"):
                if isinstance(loaded.get(key), list):
                    return [item for item in loaded[key] if isinstance(item, dict)], []
            return [loaded], []
    except json.JSONDecodeError:
        pass

    notes = []
    warnings = []
    for idx, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"postmortem_note_bad_jsonl_line:{idx}")
            continue
        if isinstance(item, dict):
            notes.append(item)
        else:
            warnings.append(f"postmortem_note_non_object_line:{idx}")
    return notes, warnings


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def normalized_symbol(value):
    return str(value or "").strip().upper()


def canonical_target_type(value):
    text = str(value or "").strip().lower()
    if text in CLOSED_TARGET_TYPES:
        return "closed_trade"
    if text in OPEN_TARGET_TYPES:
        return "open_position"
    return text or "unknown"


def target_id(target_type, symbol):
    return f"{canonical_target_type(target_type)}:{normalized_symbol(symbol)}"


def required_targets(simulation_performance):
    payload = simulation_performance if isinstance(simulation_performance, dict) else {}
    status = str(payload.get("status") or "MISSING").upper()
    postmortem = payload.get("failure_postmortem") if isinstance(payload.get("failure_postmortem"), dict) else {}
    targets = []
    if status not in ("FAIL", "WARN") and postmortem.get("status") != "ACTION_REQUIRED":
        return targets

    for row in payload.get("worst_closed_symbols") or []:
        if not isinstance(row, dict):
            continue
        symbol = normalized_symbol(row.get("symbol"))
        if not symbol or as_float(row.get("pnl_hkd_est"), 0.0) >= 0:
            continue
        targets.append(
            {
                "target_id": target_id("closed_trade", symbol),
                "target_type": "closed_trade",
                "symbol": symbol,
                "priority": "high",
                "reason": "worst_closed_symbol_negative_pnl",
                "evidence": row,
            }
        )

    for row in payload.get("open_position_risk") or []:
        if not isinstance(row, dict) or row.get("priority") != "high":
            continue
        symbol = normalized_symbol(row.get("symbol"))
        if not symbol:
            continue
        targets.append(
            {
                "target_id": target_id("open_position", symbol),
                "target_type": "open_position",
                "symbol": symbol,
                "priority": "high",
                "reason": "high_priority_open_position_risk",
                "evidence": row,
            }
        )

    deduped = {}
    for row in targets:
        deduped.setdefault(row["target_id"], row)
    return list(deduped.values())


def present(value, allow_empty_list=False):
    if value is None or value == "":
        return False
    if isinstance(value, list):
        return allow_empty_list or bool(value)
    if isinstance(value, dict):
        return bool(value)
    return True


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def contains_placeholder(value):
    if isinstance(value, str):
        text = value.strip()
        lower = text.lower()
        if not text:
            return False
        if text.startswith("<") and text.endswith(">"):
            return True
        if "<replace:" in lower or "<copy " in lower or "<required" in lower:
            return True
        return lower in PLACEHOLDER_STRINGS
    if isinstance(value, list):
        return any(contains_placeholder(item) for item in value)
    if isinstance(value, dict):
        return any(contains_placeholder(item) for item in value.values())
    return False


def validate_note(note, required_fields=None):
    required_fields = tuple(required_fields or REQUIRED_NOTE_FIELDS)
    reasons = []
    if note.get("schema") != "simulation_trade_postmortem_note_v1":
        reasons.append("schema_invalid")
    for field in required_fields:
        if field == "event_or_news_context_ids":
            if field not in note or not isinstance(note.get(field), list):
                reasons.append(f"required_field_missing:{field}")
            elif contains_placeholder(note.get(field)):
                reasons.append(f"placeholder_value_not_replaced:{field}")
            continue
        if not present(note.get(field)):
            reasons.append(f"required_field_missing:{field}")
        elif contains_placeholder(note.get(field)):
            reasons.append(f"placeholder_value_not_replaced:{field}")
    if note.get("draft_only") is True:
        reasons.append("draft_only_note_cannot_pass_audit")
    if note.get("read_only") is not True:
        reasons.append("read_only_must_be_true")
    if note.get("submits_orders") is not False:
        reasons.append("submits_orders_must_be_false")
    if note.get("changes_strategy") is not False:
        reasons.append("changes_strategy_must_be_false")
    if note.get("changes_portfolio") is not False:
        reasons.append("changes_portfolio_must_be_false")
    if note.get("auto_apply") is not False:
        reasons.append("auto_apply_must_be_false")
    if not parse_time(note.get("reviewed_at")):
        reasons.append("reviewed_at_invalid")
    if canonical_target_type(note.get("target_type")) not in ("closed_trade", "open_position"):
        reasons.append("target_type_invalid")
    gate = str(note.get("promotion_gate") or "").strip().lower()
    if gate and not ("manual" in gate or "hash" in gate):
        reasons.append("promotion_gate_must_require_manual_or_hash_review")
    return reasons


def note_target_id(note):
    return target_id(note.get("target_type"), note.get("symbol"))


def audit_notes(notes, targets, required_fields=None):
    target_ids = {row["target_id"] for row in targets}
    rows = []
    for index, note in enumerate(notes or []):
        tid = note_target_id(note)
        reasons = validate_note(note, required_fields=required_fields)
        if tid not in target_ids:
            reasons.append("note_does_not_match_required_target")
        rows.append(
            {
                "index": index,
                "status": "FAIL" if reasons else "PASS",
                "target_id": tid,
                "symbol": normalized_symbol(note.get("symbol")),
                "target_type": canonical_target_type(note.get("target_type")),
                "reviewed_at": note.get("reviewed_at"),
                "reviewer": note.get("reviewer"),
                "failure_category": note.get("failure_category"),
                "reasons": reasons,
            }
        )
    return rows


def note_contract(required_fields=None):
    required_fields = list(required_fields or REQUIRED_NOTE_FIELDS)
    return {
        "schema": "simulation_postmortem_note_contract_v1",
        "note_file": NOTE_FILE,
        "append_jsonl_object": {
            "schema": "simulation_trade_postmortem_note_v1",
            "portfolio_id": "<copy from simulation_performance.summary.portfolio_id>",
            "symbol": "<required target symbol>",
            "target_type": "closed_trade|open_position",
            "reviewed_at": "ISO-8601 datetime",
            "reviewer": "hermes|operator",
            "read_only": True,
            "submits_orders": False,
            "changes_strategy": False,
            "changes_portfolio": False,
            "auto_apply": False,
            "closed_at": "closed trade timestamp/date, or open_position for open holdings",
            "entry_signal_id_or_trade_id": "signal id, trade id, or explicit unknown",
            "exit_reason": "exit reason, or open_position_not_closed for open holdings",
            "failure_category": "entry_timing|signal_quality|stop_exit_policy|position_sizing|stale_data_source_issue|event_or_news_surprise|fundamentals_surprise|market_regime_shift|execution_quality|portfolio_risk_management|other",
            "market_context_status": "reviewed market context status",
            "intraday_context_status": "reviewed intraday context status",
            "event_or_news_context_ids": [],
            "fundamentals_context_status": "reviewed fundamentals status",
            "source_reliability_status": "reviewed source reliability status",
            "lesson": "specific lesson learned from this loss or risk item",
            "proposed_change": "none, or a concrete proposal that remains manual/hash-confirmed",
            "promotion_gate": "manual_and_hash_confirmed_before_strategy_or_watchlist_change",
        },
        "required_fields": required_fields,
        "hard_rules": [
            "Postmortem notes are audit artifacts only; they do not submit orders, change portfolio state, or alter strategy settings.",
            "A note may propose a change, but promotion_gate must keep any strategy/watchlist/config change manual and hash-confirmed.",
            "One PASS note is required for every negative worst_closed_symbols target and every high-priority open_position_risk target before loss-recovery actions can be treated as reviewed.",
        ],
    }


def recommendations(status, missing_targets, failed_notes, targets):
    recs = []
    if missing_targets:
        recs.append(f"write_simulation_postmortem_notes:{len(missing_targets)}")
    if failed_notes:
        recs.append(f"repair_invalid_simulation_postmortem_notes:{len(failed_notes)}")
    if status == "OK" and targets:
        recs.append("simulation_postmortem_notes_cover_required_targets")
    if status == "OK" and not targets:
        recs.append("simulation_postmortem_notes_not_required")
    return recs or ["inspect_simulation_postmortem_audit"]


def build_report(simulation_performance=None, notes=None, note_warnings=None):
    performance = simulation_performance if isinstance(simulation_performance, dict) else {}
    postmortem = performance.get("failure_postmortem") if isinstance(performance.get("failure_postmortem"), dict) else {}
    required_record = (
        postmortem.get("required_learning_record")
        if isinstance(postmortem.get("required_learning_record"), dict)
        else {}
    )
    learning_fields = required_record.get("required_fields")
    required_fields = list(REQUIRED_NOTE_FIELDS)
    if isinstance(learning_fields, list):
        for field in learning_fields:
            if isinstance(field, str) and field not in required_fields:
                required_fields.append(field)

    targets = required_targets(performance)
    note_rows = list(notes or [])
    audits = audit_notes(note_rows, targets, required_fields=required_fields)
    pass_target_ids = {row["target_id"] for row in audits if row.get("status") == "PASS"}
    missing_targets = [row for row in targets if row["target_id"] not in pass_target_ids]
    failed_notes = [row for row in audits if row.get("status") == "FAIL"]
    reason_counts = Counter(reason for row in failed_notes for reason in row.get("reasons") or [])

    if failed_notes:
        status = "FAIL"
    elif missing_targets:
        status = "WARN"
    else:
        status = "OK"

    return {
        "schema": "simulation_postmortem_audit_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_strategy": False,
            "changes_portfolio": False,
            "simulation_performance_file": SIMULATION_PERFORMANCE_FILE,
            "note_file": NOTE_FILE,
        },
        "simulation_performance_status": performance.get("status") or "MISSING",
        "note_contract": note_contract(required_fields=required_fields),
        "coverage": {
            "required_target_count": len(targets),
            "covered_target_count": len(pass_target_ids & {row["target_id"] for row in targets}),
            "missing_target_count": len(missing_targets),
            "note_count": len(note_rows),
            "pass_note_count": len([row for row in audits if row.get("status") == "PASS"]),
            "failed_note_count": len(failed_notes),
            "reason_counts": dict(reason_counts),
        },
        "required_targets": targets,
        "missing_required_targets": missing_targets,
        "note_audits": audits,
        "warnings": list(note_warnings or []),
        "recommendations": recommendations(status, missing_targets, failed_notes, targets),
        "hermes_use": [
            "Use this audit to verify that simulation-loss lessons were recorded before proposing strategy changes.",
            "WARN means required postmortem notes are missing; FAIL means submitted notes are unsafe or incomplete.",
            "This report is read-only and does not submit orders, mutate portfolios, or promote strategy settings.",
        ],
    }


def build_report_from_files(args):
    performance = load_json_file(args.simulation_performance_file)
    notes, warnings = load_jsonl_or_json(args.note_file)
    return build_report(performance, notes, note_warnings=warnings)


def build_text_report(payload):
    coverage = payload.get("coverage") or {}
    lines = [
        f"Simulation postmortem audit {payload['generated_at']} status={payload['status']}",
        (
            f"targets={coverage.get('required_target_count')} covered={coverage.get('covered_target_count')} "
            f"missing={coverage.get('missing_target_count')} notes={coverage.get('note_count')} "
            f"failed_notes={coverage.get('failed_note_count')}"
        ),
    ]
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulation-performance-file", default=SIMULATION_PERFORMANCE_FILE)
    parser.add_argument("--note-file", default=NOTE_FILE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report_from_files(args)
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
    return 0 if payload["status"] in ("OK", "WARN") else 2


if __name__ == "__main__":
    raise SystemExit(main())
