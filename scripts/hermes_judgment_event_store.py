#!/usr/bin/env python3
"""Persist Hermes trade judgments into an idempotent DB audit table.

Default mode is dry-run. Apply mode requires a reviewed schema hash and writes
only audit events; it never submits orders or changes intake state.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime


JUDGMENT_FILE = os.environ.get("RT_ORDER_JUDGMENT_FILE", "/tmp/hermes_trade_judgments.jsonl")
AUDIT_FILE = os.environ.get("HERMES_JUDGMENT_AUDIT_FILE", "/tmp/hermes_judgment_audit_report.json")
REPORT_FILE = os.environ.get("HERMES_JUDGMENT_EVENT_STORE_REPORT_FILE", "/tmp/hermes_judgment_event_store_report.json")
DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
DEFAULT_TABLE = os.environ.get("HERMES_JUDGMENT_EVENT_TABLE", "hermes_trade_judgment_events")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_json_file(path, default=None):
    default = {} if default is None else default
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else default
    except Exception:
        return default


def run_cmd(args, input_text=None, timeout=120):
    try:
        return subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": str(exc)})()


def psql_script(script, timeout=120):
    return run_cmd(
        [
            "docker",
            "exec",
            "-i",
            DB_CONTAINER,
            "psql",
            "-U",
            DB_USER,
            "-d",
            DB_NAME,
            "-v",
            "ON_ERROR_STOP=1",
        ],
        input_text=script,
        timeout=timeout,
    )


def quote_ident(name):
    if not IDENT_RE.match(str(name or "")):
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return name


def sql_quote(value):
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def sql_bool(value):
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return "NULL"


def sql_int(value):
    if value in (None, ""):
        return "NULL"
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return "NULL"


def sql_number(value):
    if value in (None, ""):
        return "NULL"
    try:
        return str(float(value))
    except (TypeError, ValueError):
        return "NULL"


def stable_json(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_judgment_items(loaded):
    if isinstance(loaded, list):
        return [item for item in loaded if isinstance(item, dict)]
    if isinstance(loaded, dict):
        for key in ("judgments", "decisions", "items"):
            value = loaded.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [loaded]
    return []


def load_judgments(path=JUDGMENT_FILE):
    judgments = []
    warnings = []
    stats = {"path": path, "total_lines": 0, "invalid_lines": 0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                stats["total_lines"] = line_no
                line = line.strip()
                if not line:
                    continue
                try:
                    loaded = json.loads(line)
                except json.JSONDecodeError:
                    stats["invalid_lines"] += 1
                    continue
                judgments.extend(normalize_judgment_items(loaded))
    except FileNotFoundError:
        warnings.append(f"judgment_file_missing:{path}")
    except Exception as exc:
        warnings.append(f"judgment_file_read_failed:{exc}")
    if stats["invalid_lines"]:
        warnings.append(f"invalid_jsonl_lines:{stats['invalid_lines']}")
    stats["loaded_judgments"] = len(judgments)
    return judgments, stats, warnings


def judgment_key(judgment):
    seed = {
        "schema": judgment.get("schema"),
        "packet_id": judgment.get("packet_id"),
        "signal_id": judgment.get("signal_id"),
        "decision": judgment.get("decision"),
        "reviewed_at": judgment.get("reviewed_at") or judgment.get("created_at"),
        "judgment": judgment,
    }
    return hashlib.sha256(stable_json(seed).encode("utf-8")).hexdigest()[:24]


def audit_key(row):
    return (
        str(row.get("packet_id") or ""),
        str(row.get("signal_id") or ""),
        str(row.get("reviewed_at") or ""),
        str(row.get("decision") or ""),
    )


def judgment_audit_key(judgment):
    return (
        str(judgment.get("packet_id") or ""),
        str(judgment.get("signal_id") or ""),
        str(judgment.get("reviewed_at") or judgment.get("created_at") or ""),
        str(judgment.get("decision") or "").strip().lower(),
    )


def audit_index(audit_payload):
    rows = audit_payload.get("judgments") if isinstance(audit_payload, dict) else []
    out = {}
    for row in rows or []:
        if isinstance(row, dict):
            out[audit_key(row)] = row
    return out


def schema_sql(table_name=DEFAULT_TABLE):
    table = quote_ident(table_name)
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    judgment_key TEXT PRIMARY KEY,
    packet_id TEXT,
    signal_id TEXT,
    decision TEXT,
    confidence NUMERIC,
    reviewed_at TEXT,
    reviewer TEXT,
    expiry_minutes INTEGER,
    max_quantity INTEGER,
    market_regime_exception BOOLEAN,
    audit_status TEXT,
    audit_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    packet_source TEXT,
    judgment_json JSONB NOT NULL,
    audit_json JSONB,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    seen_count INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS {table}_packet_signal_idx
    ON {table} (packet_id, signal_id);
CREATE INDEX IF NOT EXISTS {table}_signal_reviewed_idx
    ON {table} (signal_id, reviewed_at);
CREATE INDEX IF NOT EXISTS {table}_decision_idx
    ON {table} (decision);
CREATE INDEX IF NOT EXISTS {table}_audit_status_idx
    ON {table} (audit_status);
""".strip()


def schema_hash(table_name=DEFAULT_TABLE):
    return hashlib.sha256(schema_sql(table_name).encode("utf-8")).hexdigest()[:16]


def batch_hash(events, table_name=DEFAULT_TABLE):
    seed = {
        "table": table_name,
        "schema_hash": schema_hash(table_name),
        "judgment_keys": [event["judgment_key"] for event in events],
    }
    return hashlib.sha256(stable_json(seed).encode("utf-8")).hexdigest()[:16]


def build_events(judgments, audit_payload=None):
    index = audit_index(audit_payload or {})
    events = []
    seen = set()
    duplicate_count = 0
    for judgment in judgments:
        key = judgment_key(judgment)
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        audit_row = index.get(judgment_audit_key(judgment)) or {}
        events.append(
            {
                "judgment_key": key,
                "judgment": judgment,
                "audit": audit_row,
            }
        )
    return events, {"duplicate_count": duplicate_count}


def upsert_sql(event, table_name=DEFAULT_TABLE):
    table = quote_ident(table_name)
    judgment = event["judgment"]
    audit_row = event.get("audit") or {}
    values = {
        "judgment_key": sql_quote(event["judgment_key"]),
        "packet_id": sql_quote(judgment.get("packet_id")),
        "signal_id": sql_quote(judgment.get("signal_id")),
        "decision": sql_quote(str(judgment.get("decision", "")).strip().lower() or None),
        "confidence": sql_number(judgment.get("confidence")),
        "reviewed_at": sql_quote(judgment.get("reviewed_at") or judgment.get("created_at")),
        "reviewer": sql_quote(judgment.get("reviewer")),
        "expiry_minutes": sql_int(judgment.get("expiry_minutes")),
        "max_quantity": sql_int(judgment.get("max_quantity")),
        "market_regime_exception": sql_bool(judgment.get("market_regime_exception")),
        "audit_status": sql_quote(audit_row.get("status")),
        "audit_reasons": sql_quote(stable_json(audit_row.get("reasons") or [])) + "::jsonb",
        "packet_source": sql_quote(audit_row.get("packet_source")),
        "judgment_json": sql_quote(stable_json(judgment)) + "::jsonb",
        "audit_json": (sql_quote(stable_json(audit_row)) + "::jsonb") if audit_row else "NULL",
    }
    columns = list(values)
    assignments = [
        f"{column} = EXCLUDED.{column}"
        for column in columns
        if column != "judgment_key"
    ]
    assignments.extend([f"last_seen_at = NOW()", f"seen_count = {table}.seen_count + 1"])
    return (
        f"INSERT INTO {table} ({', '.join(columns)})\n"
        f"VALUES ({', '.join(values[column] for column in columns)})\n"
        "ON CONFLICT (judgment_key) DO UPDATE SET\n    "
        + ",\n    ".join(assignments)
        + ";"
    )


def build_sql_script(events, table_name=DEFAULT_TABLE):
    statements = ["BEGIN;", schema_sql(table_name)]
    statements.extend(upsert_sql(event, table_name) for event in events)
    statements.append("COMMIT;")
    return "\n".join(statements) + "\n"


def event_summary(events):
    by_decision = {}
    by_audit_status = {}
    approval_count = 0
    for event in events:
        judgment = event["judgment"]
        decision = str(judgment.get("decision", "missing")).strip().lower() or "missing"
        by_decision[decision] = by_decision.get(decision, 0) + 1
        if decision in ("approve", "reduce"):
            approval_count += 1
        status = (event.get("audit") or {}).get("status") or "missing"
        by_audit_status[status] = by_audit_status.get(status, 0) + 1
    return {
        "by_decision": dict(sorted(by_decision.items())),
        "by_audit_status": dict(sorted(by_audit_status.items())),
        "approval_count": approval_count,
    }


def build_report(
    judgment_file=JUDGMENT_FILE,
    audit_file=AUDIT_FILE,
    table_name=DEFAULT_TABLE,
    apply=False,
    confirm_schema_hash="",
):
    reasons = []
    try:
        quote_ident(table_name)
    except ValueError:
        reasons.append("invalid_table_name")
    judgments, judgment_stats, warnings = load_judgments(judgment_file)
    audit_payload = load_json_file(audit_file, {})
    if audit_payload and audit_payload.get("schema") != "hermes_judgment_audit_report_v1":
        warnings.append("audit_report_schema_invalid")
    elif not audit_payload:
        warnings.append(f"audit_report_missing:{audit_file}")
    events, event_stats = build_events(judgments, audit_payload)
    current_schema_hash = schema_hash(table_name) if not reasons else ""
    current_batch_hash = batch_hash(events, table_name) if not reasons else ""
    if apply and not confirm_schema_hash:
        reasons.append("confirm_schema_hash_required")
    if apply and confirm_schema_hash and confirm_schema_hash != current_schema_hash:
        reasons.append("confirm_schema_hash_mismatch")

    status = "dry_run"
    apply_result = None
    applied = False
    if apply:
        if reasons:
            status = "blocked"
        elif not events:
            status = "noop"
            apply_result = {"status": "noop", "reason": "no_judgments_to_ingest"}
        else:
            result = psql_script(build_sql_script(events, table_name), timeout=180)
            applied = result.returncode == 0
            status = "applied" if applied else "failed"
            apply_result = {
                "status": status,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            }
    elif reasons:
        status = "invalid"

    return {
        "schema": "hermes_judgment_event_store_report_v1",
        "generated_at": now_iso(),
        "mode": "apply" if apply else "dry-run",
        "status": status,
        "judgment_file": judgment_file,
        "audit_file": audit_file,
        "table_name": table_name,
        "schema_hash": current_schema_hash,
        "confirm_schema_hash": confirm_schema_hash,
        "batch_hash": current_batch_hash,
        "judgment_stats": judgment_stats,
        "judgment_count": len(judgments),
        "event_count": len(events),
        "duplicate_count": event_stats["duplicate_count"],
        "event_summary": event_summary(events),
        "warnings": warnings,
        "validation_reasons": reasons,
        "applied": applied,
        "apply_result": apply_result,
        "safety": {
            "dry_run_by_default": True,
            "requires_confirm_schema_hash": True,
            "idempotent_on_judgment_key": True,
            "does_not_submit_orders": True,
            "does_not_change_intake_state": True,
            "does_not_change_strategy_config": True,
            "does_not_restart_services": True,
            "writes_audit_table_only": True,
        },
    }


def build_text_report(payload):
    lines = [
        f"Hermes judgment event store {payload['generated_at']}",
        (
            f"mode={payload['mode']} status={payload['status']} table={payload['table_name']} "
            f"schema_hash={payload['schema_hash']} events={payload['event_count']} "
            f"duplicates={payload['duplicate_count']} batch={payload['batch_hash']}"
        ),
    ]
    if payload.get("validation_reasons"):
        lines.append("Reasons: " + ", ".join(payload["validation_reasons"]))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    lines.append("by_decision=" + json.dumps(payload["event_summary"]["by_decision"], ensure_ascii=False))
    lines.append("by_audit_status=" + json.dumps(payload["event_summary"]["by_audit_status"], ensure_ascii=False))
    if payload.get("apply_result"):
        lines.append("apply_result=" + json.dumps(payload["apply_result"], ensure_ascii=False))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--judgment-file", default=JUDGMENT_FILE)
    parser.add_argument("--audit-file", default=AUDIT_FILE)
    parser.add_argument("--table-name", default=DEFAULT_TABLE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-schema-hash", default="")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(
        judgment_file=args.judgment_file,
        audit_file=args.audit_file,
        table_name=args.table_name,
        apply=args.apply,
        confirm_schema_hash=args.confirm_schema_hash,
    )
    if args.output:
        save_json_atomic(args.output, payload)
    if args.text:
        print(build_text_report(payload))
    elif args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(build_text_report(payload))
        print("\n--- JSON ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] in ("dry_run", "applied", "noop") else 2


if __name__ == "__main__":
    sys.exit(main())
