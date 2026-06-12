#!/usr/bin/env python3
"""Persist realtime v5 alert JSONL rows into an idempotent DB event table.

Default mode is dry-run. Apply mode creates/updates an audit table only after a
reviewed schema hash is provided. It never submits orders or changes strategy.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import deque
from datetime import datetime


ALERT_QUEUE_FILE = os.environ.get("RT_ALERT_QUEUE_FILE", "/tmp/rt_signal_alerts.jsonl")
REPORT_FILE = os.environ.get("RT_ALERT_EVENT_STORE_REPORT_FILE", "/tmp/rt_alert_event_store_report.json")
DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
DEFAULT_TABLE = os.environ.get("RT_ALERT_EVENT_TABLE", "rt_signal_alert_events")
DEFAULT_SCAN_LIMIT = int(os.environ.get("RT_ALERT_EVENT_SCAN_LIMIT", "2000"))
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


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


def sql_number(value):
    if value in (None, ""):
        return "NULL"
    try:
        return str(float(value))
    except (TypeError, ValueError):
        return "NULL"


def stable_json(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def alert_signal_id(alert):
    return alert.get("signal_id") or ":".join(
        str(alert.get(k, "")) for k in ("symbol", "signal_type", "trigger", "generated_at", "time")
    )


def load_jsonl_tail(path=ALERT_QUEUE_FILE, limit=DEFAULT_SCAN_LIMIT):
    rows = deque(maxlen=limit if limit and limit > 0 else None)
    warnings = []
    total_lines = 0
    invalid_lines = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                total_lines = line_no
                line = line.strip()
                if not line:
                    continue
                try:
                    loaded = json.loads(line)
                except json.JSONDecodeError:
                    invalid_lines += 1
                    continue
                if isinstance(loaded, dict):
                    rows.append(loaded)
                else:
                    invalid_lines += 1
    except FileNotFoundError:
        warnings.append(f"queue_file_missing:{path}")
    except Exception as exc:
        warnings.append(f"queue_file_read_failed:{exc}")
    if invalid_lines:
        warnings.append(f"invalid_jsonl_lines:{invalid_lines}")
    return list(rows), {"path": path, "total_lines": total_lines, "loaded_rows": len(rows), "invalid_lines": invalid_lines}, warnings


def dedupe_alerts(alerts):
    deduped = {}
    skipped = []
    duplicate_count = 0
    for alert in alerts:
        sid = str(alert_signal_id(alert)).strip()
        if not sid:
            skipped.append({"reason": "missing_signal_id", "alert": alert})
            continue
        if sid in deduped:
            duplicate_count += 1
        deduped[sid] = alert
    return [deduped[key] for key in sorted(deduped)], {"duplicate_count": duplicate_count, "skipped": skipped}


def schema_sql(table_name=DEFAULT_TABLE):
    table = quote_ident(table_name)
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    signal_id TEXT PRIMARY KEY,
    source TEXT,
    symbol TEXT,
    market TEXT,
    signal_type TEXT,
    candidate_signal_type TEXT,
    trigger_name TEXT,
    confirmed BOOLEAN,
    execution_candidate BOOLEAN,
    full_score NUMERIC,
    price NUMERIC,
    entry_price NUMERIC,
    stop_loss NUMERIC,
    take_profit NUMERIC,
    rr_ratio NUMERIC,
    strategy_config_id TEXT,
    watchlist_id TEXT,
    generated_at TEXT,
    quote_time TEXT,
    suppressed_directional_reason TEXT,
    alert_json JSONB NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    seen_count INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS {table}_strategy_watchlist_idx
    ON {table} (strategy_config_id, watchlist_id);
CREATE INDEX IF NOT EXISTS {table}_symbol_generated_idx
    ON {table} (symbol, generated_at);
CREATE INDEX IF NOT EXISTS {table}_signal_type_idx
    ON {table} (signal_type);
""".strip()


def schema_hash(table_name=DEFAULT_TABLE):
    return hashlib.sha256(schema_sql(table_name).encode("utf-8")).hexdigest()[:16]


def batch_hash(alerts, table_name=DEFAULT_TABLE):
    seed = {
        "table": table_name,
        "signal_ids": [alert_signal_id(alert) for alert in alerts],
        "schema_hash": schema_hash(table_name),
    }
    return hashlib.sha256(stable_json(seed).encode("utf-8")).hexdigest()[:16]


def upsert_sql(alert, table_name=DEFAULT_TABLE):
    table = quote_ident(table_name)
    sid = alert_signal_id(alert)
    alert_json = stable_json(alert)
    values = {
        "signal_id": sql_quote(sid),
        "source": sql_quote(alert.get("source")),
        "symbol": sql_quote(str(alert.get("symbol", "")).upper() if alert.get("symbol") else None),
        "market": sql_quote(alert.get("market")),
        "signal_type": sql_quote(str(alert.get("signal_type", "")).upper() if alert.get("signal_type") else None),
        "candidate_signal_type": sql_quote(alert.get("candidate_signal_type")),
        "trigger_name": sql_quote(alert.get("trigger")),
        "confirmed": sql_bool(alert.get("confirmed")),
        "execution_candidate": sql_bool(alert.get("execution_candidate")),
        "full_score": sql_number(alert.get("full_score")),
        "price": sql_number(alert.get("price")),
        "entry_price": sql_number(alert.get("entry_price")),
        "stop_loss": sql_number(alert.get("stop_loss")),
        "take_profit": sql_number(alert.get("take_profit")),
        "rr_ratio": sql_number(alert.get("rr_ratio")),
        "strategy_config_id": sql_quote(alert.get("strategy_config_id")),
        "watchlist_id": sql_quote(alert.get("watchlist_id")),
        "generated_at": sql_quote(alert.get("generated_at")),
        "quote_time": sql_quote(alert.get("quote_time") or alert.get("time")),
        "suppressed_directional_reason": sql_quote(alert.get("suppressed_directional_reason")),
        "alert_json": sql_quote(alert_json) + "::jsonb",
    }
    columns = list(values)
    assignments = [
        f"{column} = EXCLUDED.{column}"
        for column in columns
        if column not in ("signal_id",)
    ]
    assignments.extend(["last_seen_at = NOW()", "seen_count = {table}.seen_count + 1".format(table=table)])
    return (
        f"INSERT INTO {table} ({', '.join(columns)})\n"
        f"VALUES ({', '.join(values[column] for column in columns)})\n"
        "ON CONFLICT (signal_id) DO UPDATE SET\n    "
        + ",\n    ".join(assignments)
        + ";"
    )


def build_sql_script(alerts, table_name=DEFAULT_TABLE):
    statements = ["BEGIN;", schema_sql(table_name)]
    statements.extend(upsert_sql(alert, table_name) for alert in alerts)
    statements.append("COMMIT;")
    return "\n".join(statements) + "\n"


def event_summary(alerts):
    by_type = {}
    by_scope = {}
    for alert in alerts:
        side = str(alert.get("signal_type", "UNKNOWN")).upper() or "UNKNOWN"
        by_type[side] = by_type.get(side, 0) + 1
        scope = (
            str(alert.get("strategy_config_id") or "missing"),
            str(alert.get("watchlist_id") or "missing"),
        )
        key = "|".join(scope)
        by_scope[key] = by_scope.get(key, 0) + 1
    return {"by_signal_type": dict(sorted(by_type.items())), "by_scope": dict(sorted(by_scope.items()))}


def build_report(
    queue_file=ALERT_QUEUE_FILE,
    table_name=DEFAULT_TABLE,
    scan_limit=DEFAULT_SCAN_LIMIT,
    apply=False,
    confirm_schema_hash="",
):
    reasons = []
    try:
        quote_ident(table_name)
    except ValueError:
        reasons.append("invalid_table_name")
    alerts, queue_stats, warnings = load_jsonl_tail(queue_file, scan_limit)
    deduped, dedupe_stats = dedupe_alerts(alerts)
    current_schema_hash = schema_hash(table_name) if not reasons else ""
    current_batch_hash = batch_hash(deduped, table_name) if not reasons else ""
    if apply and not confirm_schema_hash:
        reasons.append("confirm_schema_hash_required")
    if apply and confirm_schema_hash and confirm_schema_hash != current_schema_hash:
        reasons.append("confirm_schema_hash_mismatch")
    apply_result = None
    applied = False
    status = "dry_run"
    if apply:
        if reasons:
            status = "blocked"
        elif not deduped:
            status = "noop"
            apply_result = {"status": "noop", "reason": "no_alerts_to_ingest"}
        else:
            sql = build_sql_script(deduped, table_name)
            result = psql_script(sql, timeout=180)
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
        "schema": "rt_alert_event_store_report_v1",
        "generated_at": now_iso(),
        "mode": "apply" if apply else "dry-run",
        "status": status,
        "queue_file": queue_file,
        "table_name": table_name,
        "schema_hash": current_schema_hash,
        "confirm_schema_hash": confirm_schema_hash,
        "batch_hash": current_batch_hash,
        "scan_limit": scan_limit,
        "queue_stats": queue_stats,
        "raw_alert_count": len(alerts),
        "event_count": len(deduped),
        "duplicate_count": dedupe_stats["duplicate_count"],
        "skipped_count": len(dedupe_stats["skipped"]),
        "skipped": dedupe_stats["skipped"][:20],
        "event_summary": event_summary(deduped),
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
        f"RT alert event store {payload['generated_at']}",
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
    lines.append("by_signal_type=" + json.dumps(payload["event_summary"]["by_signal_type"], ensure_ascii=False))
    if payload.get("apply_result"):
        lines.append("apply_result=" + json.dumps(payload["apply_result"], ensure_ascii=False))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-file", default=ALERT_QUEUE_FILE)
    parser.add_argument("--table-name", default=DEFAULT_TABLE)
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-schema-hash", default="")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(
        queue_file=args.queue_file,
        table_name=args.table_name,
        scan_limit=args.scan_limit,
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
