#!/usr/bin/env python3
"""Persist rt_signal_outcome_report evaluations into a DB audit table.

Default mode is dry-run. Apply mode requires a reviewed schema hash and writes
only outcome evidence; it never changes strategy config or submits orders.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime


REPORT_FILE = os.environ.get("RT_SIGNAL_OUTCOME_REPORT_FILE", "/tmp/rt_signal_outcome_report.json")
STORE_REPORT_FILE = os.environ.get("RT_SIGNAL_OUTCOME_EVENT_STORE_REPORT_FILE", "/tmp/rt_signal_outcome_event_store_report.json")
DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
DEFAULT_TABLE = os.environ.get("RT_SIGNAL_OUTCOME_EVENT_TABLE", "rt_signal_outcome_events")
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


def load_outcome_report(path=REPORT_FILE):
    payload = load_json_file(path, {})
    warnings = []
    if not payload:
        warnings.append(f"outcome_report_missing:{path}")
    elif payload.get("schema") != "rt_signal_outcome_report_v1":
        warnings.append("outcome_report_schema_invalid")
    return payload, {"path": path, "exists": bool(payload)}, warnings


def report_evaluations(report):
    evaluations = report.get("evaluations")
    if isinstance(evaluations, list):
        return [item for item in evaluations if isinstance(item, dict)], False
    recent = report.get("recent_evaluations")
    if isinstance(recent, list):
        return [item for item in recent if isinstance(item, dict)], True
    return [], False


def primary_outcome(evaluation, primary_horizon):
    outcomes = evaluation.get("outcomes") if isinstance(evaluation.get("outcomes"), dict) else {}
    if primary_horizon and primary_horizon in outcomes:
        return primary_horizon, outcomes.get(primary_horizon) or {}
    for key in sorted(outcomes):
        return key, outcomes.get(key) or {}
    status = evaluation.get("status")
    if status:
        fallback = {"status": status}
        if evaluation.get("reason"):
            fallback["reason"] = evaluation.get("reason")
        return primary_horizon, fallback
    return primary_horizon, {}


def build_events(report):
    evaluations, recent_only = report_evaluations(report)
    primary_horizon = report.get("primary_horizon") or "1d"
    events = []
    duplicate_count = 0
    seen = set()
    for item in evaluations:
        sid = str(item.get("signal_id") or "").strip()
        if not sid:
            continue
        if sid in seen:
            duplicate_count += 1
            continue
        seen.add(sid)
        horizon, outcome = primary_outcome(item, primary_horizon)
        events.append(
            {
                "signal_id": sid,
                "evaluation": item,
                "primary_horizon": horizon,
                "primary_outcome": outcome,
            }
        )
    return events, {"duplicate_count": duplicate_count, "recent_only": recent_only}


def schema_sql(table_name=DEFAULT_TABLE):
    table = quote_ident(table_name)
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    signal_id TEXT PRIMARY KEY,
    symbol TEXT,
    market TEXT,
    signal_type TEXT,
    trigger_name TEXT,
    confirmed BOOLEAN,
    status TEXT,
    reason TEXT,
    strategy_config_id TEXT,
    watchlist_id TEXT,
    signal_date TEXT,
    generated_at TEXT,
    latest_kline_date TEXT,
    available_future_days INTEGER,
    primary_horizon TEXT,
    primary_status TEXT,
    mark_date TEXT,
    signed_close_return_pct NUMERIC,
    win BOOLEAN,
    target_hit BOOLEAN,
    stop_hit BOOLEAN,
    first_hit TEXT,
    report_generated_at TEXT,
    evaluation_json JSONB NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    seen_count INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS {table}_strategy_watchlist_idx
    ON {table} (strategy_config_id, watchlist_id);
CREATE INDEX IF NOT EXISTS {table}_trigger_idx
    ON {table} (signal_type, trigger_name);
CREATE INDEX IF NOT EXISTS {table}_status_idx
    ON {table} (status, primary_status);
CREATE INDEX IF NOT EXISTS {table}_symbol_idx
    ON {table} (symbol);
""".strip()


def schema_hash(table_name=DEFAULT_TABLE):
    return hashlib.sha256(schema_sql(table_name).encode("utf-8")).hexdigest()[:16]


def batch_hash(events, table_name=DEFAULT_TABLE):
    seed = {
        "table": table_name,
        "schema_hash": schema_hash(table_name),
        "signal_ids": [event["signal_id"] for event in events],
    }
    return hashlib.sha256(stable_json(seed).encode("utf-8")).hexdigest()[:16]


def upsert_sql(event, report, table_name=DEFAULT_TABLE):
    table = quote_ident(table_name)
    item = event["evaluation"]
    outcome = event.get("primary_outcome") or {}
    values = {
        "signal_id": sql_quote(event["signal_id"]),
        "symbol": sql_quote(item.get("symbol")),
        "market": sql_quote(item.get("market")),
        "signal_type": sql_quote(item.get("signal_type")),
        "trigger_name": sql_quote(item.get("trigger")),
        "confirmed": sql_bool(item.get("confirmed")),
        "status": sql_quote(item.get("status")),
        "reason": sql_quote(item.get("reason")),
        "strategy_config_id": sql_quote(item.get("strategy_config_id")),
        "watchlist_id": sql_quote(item.get("watchlist_id")),
        "signal_date": sql_quote(item.get("signal_date")),
        "generated_at": sql_quote(item.get("generated_at")),
        "latest_kline_date": sql_quote(item.get("latest_kline_date")),
        "available_future_days": sql_int(item.get("available_future_days")),
        "primary_horizon": sql_quote(event.get("primary_horizon")),
        "primary_status": sql_quote(outcome.get("status")),
        "mark_date": sql_quote(outcome.get("mark_date")),
        "signed_close_return_pct": sql_number(outcome.get("signed_close_return_pct")),
        "win": sql_bool(outcome.get("win")),
        "target_hit": sql_bool(outcome.get("target_hit")),
        "stop_hit": sql_bool(outcome.get("stop_hit")),
        "first_hit": sql_quote(outcome.get("first_hit")),
        "report_generated_at": sql_quote(report.get("generated_at")),
        "evaluation_json": sql_quote(stable_json(item)) + "::jsonb",
    }
    columns = list(values)
    assignments = [f"{column} = EXCLUDED.{column}" for column in columns if column != "signal_id"]
    assignments.extend([f"last_seen_at = NOW()", f"seen_count = {table}.seen_count + 1"])
    return (
        f"INSERT INTO {table} ({', '.join(columns)})\n"
        f"VALUES ({', '.join(values[column] for column in columns)})\n"
        "ON CONFLICT (signal_id) DO UPDATE SET\n    "
        + ",\n    ".join(assignments)
        + ";"
    )


def build_sql_script(events, report, table_name=DEFAULT_TABLE):
    statements = ["BEGIN;", schema_sql(table_name)]
    statements.extend(upsert_sql(event, report, table_name) for event in events)
    statements.append("COMMIT;")
    return "\n".join(statements) + "\n"


def event_summary(events):
    by_status = {}
    by_primary_status = {}
    by_trigger = {}
    resolved_count = 0
    pending_count = 0
    win_count = 0
    for event in events:
        item = event["evaluation"]
        outcome = event.get("primary_outcome") or {}
        status = str(item.get("status") or "missing")
        primary_status = str(outcome.get("status") or "missing")
        trigger = f"{item.get('signal_type')}:{item.get('trigger') or 'UNKNOWN'}"
        by_status[status] = by_status.get(status, 0) + 1
        by_primary_status[primary_status] = by_primary_status.get(primary_status, 0) + 1
        by_trigger[trigger] = by_trigger.get(trigger, 0) + 1
        if status == "resolved":
            resolved_count += 1
        else:
            pending_count += 1
        if outcome.get("win") is True:
            win_count += 1
    return {
        "by_status": dict(sorted(by_status.items())),
        "by_primary_status": dict(sorted(by_primary_status.items())),
        "by_trigger": dict(sorted(by_trigger.items())),
        "resolved_count": resolved_count,
        "pending_count": pending_count,
        "win_count": win_count,
    }


def build_report(
    outcome_report_file=REPORT_FILE,
    table_name=DEFAULT_TABLE,
    apply=False,
    confirm_schema_hash="",
):
    reasons = []
    try:
        quote_ident(table_name)
    except ValueError:
        reasons.append("invalid_table_name")
    outcome_report, report_stats, warnings = load_outcome_report(outcome_report_file)
    events, event_stats = build_events(outcome_report)
    if event_stats["recent_only"]:
        warnings.append("outcome_report_missing_full_evaluations_using_recent_only")
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
            apply_result = {"status": "noop", "reason": "no_outcome_events_to_ingest"}
        else:
            result = psql_script(build_sql_script(events, outcome_report, table_name), timeout=180)
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
        "schema": "rt_signal_outcome_event_store_report_v1",
        "generated_at": now_iso(),
        "mode": "apply" if apply else "dry-run",
        "status": status,
        "outcome_report_file": outcome_report_file,
        "table_name": table_name,
        "schema_hash": current_schema_hash,
        "confirm_schema_hash": confirm_schema_hash,
        "batch_hash": current_batch_hash,
        "report_stats": report_stats,
        "source_report": {
            "schema": outcome_report.get("schema"),
            "generated_at": outcome_report.get("generated_at"),
            "status": outcome_report.get("status"),
            "sample_scope": outcome_report.get("sample_scope"),
            "evaluated_signal_count": outcome_report.get("evaluated_signal_count"),
            "resolved_signal_count": outcome_report.get("resolved_signal_count"),
            "pending_signal_count": outcome_report.get("pending_signal_count"),
            "primary_horizon": outcome_report.get("primary_horizon"),
            "primary_recommendation": outcome_report.get("primary_recommendation"),
        },
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
            "idempotent_on_signal_id": True,
            "does_not_submit_orders": True,
            "does_not_change_strategy_config": True,
            "does_not_restart_services": True,
            "writes_audit_table_only": True,
        },
    }


def build_text_report(payload):
    lines = [
        f"RT signal outcome event store {payload['generated_at']}",
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
    lines.append("by_status=" + json.dumps(payload["event_summary"]["by_status"], ensure_ascii=False))
    lines.append("by_primary_status=" + json.dumps(payload["event_summary"]["by_primary_status"], ensure_ascii=False))
    if payload.get("apply_result"):
        lines.append("apply_result=" + json.dumps(payload["apply_result"], ensure_ascii=False))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--outcome-report-file", default=REPORT_FILE)
    parser.add_argument("--table-name", default=DEFAULT_TABLE)
    parser.add_argument("--output", default=STORE_REPORT_FILE)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-schema-hash", default="")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(
        outcome_report_file=args.outcome_report_file,
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
