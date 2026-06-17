#!/usr/bin/env python3
"""Persist rt_order_intake decisions into an idempotent DB audit table.

Default mode is dry-run. Apply mode requires a reviewed schema hash and writes
only audit events; it never submits orders or modifies intake state.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime


STATE_FILE = os.environ.get("RT_ORDER_STATE_FILE", "/tmp/rt_order_intake_state.json")
REPORT_FILE = os.environ.get("RT_ORDER_INTAKE_EVENT_STORE_REPORT_FILE", "/tmp/rt_order_intake_event_store_report.json")
DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
DEFAULT_TABLE = os.environ.get("RT_ORDER_INTAKE_EVENT_TABLE", "rt_order_intake_events")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALPACA_TRADING_BASE_URL = os.environ.get(
    "ALPACA_TRADING_BASE_URL",
    os.environ.get("ALPACA_BASE_URL", os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets/v2")),
).rstrip("/")
ALPACA_API_KEY_ID = (
    os.environ.get("APCA_API_KEY_ID")
    or os.environ.get("ALPACA_API_KEY_ID")
    or os.environ.get("ALPACA_API_KEY")
    or os.environ.get("ALPACA_KEY_ID", "")
)
ALPACA_API_SECRET_KEY = (
    os.environ.get("APCA_API_SECRET_KEY")
    or os.environ.get("ALPACA_API_SECRET_KEY")
    or os.environ.get("ALPACA_SECRET_KEY", "")
)
BROKER_RECONCILIATION_BROKER = os.environ.get(
    "RT_ORDER_INTAKE_RECONCILE_BROKER",
    os.environ.get("RT_ORDER_US_BROKER", "auto"),
).strip().lower()
ALPACA_RECONCILIATION_ORDER_LIMIT = int(os.environ.get("RT_ORDER_ALPACA_RECONCILIATION_ORDER_LIMIT", "50"))
SYSTEM_CLIENT_ORDER_PREFIX = os.environ.get("RT_ORDER_CLIENT_ORDER_ID_PREFIX", "qm-")


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


def unique_text(values):
    seen = set()
    result = []
    for value in values or []:
        text = str(value if value is not None else "").strip()
        if text and text.lower() not in ("none", "null") and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def order_result_ids(order_result):
    ids = []
    if isinstance(order_result, dict):
        for key in ("order_id", "id", "client_order_id", "clientOrderId"):
            ids.append(order_result.get(key))
        for nested_key in ("data", "order", "result"):
            nested = order_result.get(nested_key)
            if isinstance(nested, dict):
                for key in ("order_id", "id", "client_order_id", "clientOrderId"):
                    ids.append(nested.get(key))
    elif order_result is not None:
        ids.append(order_result)
    return unique_text(ids)


def alpaca_auth_available():
    return bool(ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY)


def alpaca_request(path, method="GET", data=None):
    url = ALPACA_TRADING_BASE_URL + path
    body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {
        "Content-Type": "application/json",
        "Apca-Api-Key-Id": ALPACA_API_KEY_ID,
        "Apca-Api-Secret-Key": ALPACA_API_SECRET_KEY,
    }
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def fetch_alpaca_orders(limit=ALPACA_RECONCILIATION_ORDER_LIMIT):
    query = urllib.parse.urlencode(
        {
            "status": "all",
            "limit": int(limit),
            "direction": "desc",
        }
    )
    result = alpaca_request(f"/orders?{query}")
    return result if isinstance(result, list) else []


def broker_order_ids(order):
    if not isinstance(order, dict):
        return []
    return unique_text([order.get("id"), order.get("client_order_id"), order.get("clientOrderId")])


def broker_client_order_id(order):
    if not isinstance(order, dict):
        return ""
    return str(order.get("client_order_id") or order.get("clientOrderId") or "").strip()


def is_system_broker_order(order):
    client_order_id = broker_client_order_id(order)
    return bool(client_order_id and client_order_id.startswith(SYSTEM_CLIENT_ORDER_PREFIX))


def compact_broker_order(order):
    if not isinstance(order, dict):
        return {}
    return {
        "id": order.get("id"),
        "client_order_id": order.get("client_order_id") or order.get("clientOrderId"),
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "qty": order.get("qty") or order.get("quantity"),
        "status": order.get("status"),
        "submitted_at": order.get("submitted_at"),
        "filled_at": order.get("filled_at"),
    }


def submitted_state_order_ids(events):
    order_ids = []
    submitted = []
    for event in events:
        decision = event.get("decision") if isinstance(event, dict) else {}
        if not isinstance(decision, dict) or decision.get("status") != "submitted":
            continue
        row = {
            "ledger": event.get("ledger"),
            "signal_id": event.get("signal_id"),
            "symbol": plan_value(decision, "symbol") or (decision.get("alert") or {}).get("symbol"),
            "side": plan_value(decision, "side") or (decision.get("alert") or {}).get("signal_type"),
            "submitted_at": decision.get("submitted_at"),
            "order_ids": order_result_ids(decision.get("order_result")),
        }
        submitted.append(row)
        order_ids.extend(row["order_ids"])
    return unique_text(order_ids), submitted


def broker_reconciliation(
    events,
    broker=BROKER_RECONCILIATION_BROKER,
    order_limit=ALPACA_RECONCILIATION_ORDER_LIMIT,
    order_fetcher=None,
):
    broker = str(broker or "auto").strip().lower()
    if broker in ("", "none", "disabled", "quantmind-sim", "quantmind_sim"):
        return {
            "schema": "rt_order_intake_broker_reconciliation_v1",
            "status": "NOT_CONFIGURED",
            "broker": broker or "none",
            "broker_order_count": 0,
            "matched_order_count": 0,
            "unmatched_broker_order_count": 0,
            "state_submitted_count": 0,
            "state_order_id_count": 0,
            "sample_state_order_ids": [],
            "unmatched_broker_orders": [],
            "reason_codes": ["broker_reconciliation_not_configured"],
            "read_only": True,
        }
    if broker == "auto":
        broker = "alpaca-paper" if alpaca_auth_available() or order_fetcher is not None else "none"
        if broker == "none":
            return {
                "schema": "rt_order_intake_broker_reconciliation_v1",
                "status": "NOT_CONFIGURED",
                "broker": "auto",
                "broker_order_count": 0,
                "matched_order_count": 0,
                "unmatched_broker_order_count": 0,
                "state_submitted_count": 0,
                "state_order_id_count": 0,
                "sample_state_order_ids": [],
                "unmatched_broker_orders": [],
                "reason_codes": ["broker_reconciliation_not_configured"],
                "read_only": True,
            }
    if broker not in ("alpaca-paper", "alpaca_paper"):
        return {
            "schema": "rt_order_intake_broker_reconciliation_v1",
            "status": "NOT_CONFIGURED",
            "broker": broker,
            "broker_order_count": 0,
            "matched_order_count": 0,
            "unmatched_broker_order_count": 0,
            "state_submitted_count": 0,
            "state_order_id_count": 0,
            "sample_state_order_ids": [],
            "unmatched_broker_orders": [],
            "reason_codes": ["unsupported_broker_for_reconciliation"],
            "read_only": True,
        }
    if order_fetcher is None and not alpaca_auth_available():
        return {
            "schema": "rt_order_intake_broker_reconciliation_v1",
            "status": "MISSING_CREDENTIALS",
            "broker": "alpaca-paper",
            "broker_order_count": 0,
            "matched_order_count": 0,
            "unmatched_broker_order_count": 0,
            "state_submitted_count": 0,
            "state_order_id_count": 0,
            "sample_state_order_ids": [],
            "unmatched_broker_orders": [],
            "reason_codes": ["alpaca_paper_credentials_missing"],
            "read_only": True,
        }

    state_order_ids, submitted = submitted_state_order_ids(events)
    state_order_id_set = set(state_order_ids)
    try:
        broker_orders = order_fetcher(order_limit) if order_fetcher else fetch_alpaca_orders(order_limit)
    except Exception as exc:
        return {
            "schema": "rt_order_intake_broker_reconciliation_v1",
            "status": "DEGRADED",
            "broker": "alpaca-paper",
            "broker_order_count": 0,
            "matched_order_count": 0,
            "unmatched_broker_order_count": 0,
            "state_submitted_count": len(submitted),
            "state_order_id_count": len(state_order_ids),
            "sample_state_order_ids": state_order_ids[:10],
            "unmatched_broker_orders": [],
            "reason_codes": ["broker_order_query_failed"],
            "error": str(exc),
            "read_only": True,
        }
    if not isinstance(broker_orders, list):
        broker_orders = []

    system_broker_orders = []
    external_broker_orders = []
    matched = []
    unmatched = []
    for order in broker_orders:
        if is_system_broker_order(order):
            system_broker_orders.append(order)
        else:
            external_broker_orders.append(order)
            continue
        ids = broker_order_ids(order)
        if ids and any(order_id in state_order_id_set for order_id in ids):
            matched.append(order)
        else:
            unmatched.append(order)

    reasons = []
    status = "OK"
    if unmatched:
        status = "FAIL"
        reasons.append("broker_orders_missing_from_intake_state")
    return {
        "schema": "rt_order_intake_broker_reconciliation_v1",
        "status": status,
        "broker": "alpaca-paper",
        "order_limit": int(order_limit),
        "system_client_order_prefix": SYSTEM_CLIENT_ORDER_PREFIX,
        "broker_order_count": len(broker_orders),
        "system_broker_order_count": len(system_broker_orders),
        "external_broker_order_count": len(external_broker_orders),
        "matched_order_count": len(matched),
        "unmatched_broker_order_count": len(unmatched),
        "state_submitted_count": len(submitted),
        "state_order_id_count": len(state_order_ids),
        "sample_state_order_ids": state_order_ids[:10],
        "unmatched_broker_orders": [compact_broker_order(order) for order in unmatched[:10]],
        "external_broker_orders": [compact_broker_order(order) for order in external_broker_orders[:10]],
        "reason_codes": reasons,
        "read_only": True,
    }


def normalize_state(raw):
    if not isinstance(raw, dict):
        raw = {}
    processed = raw.get("processed") if isinstance(raw.get("processed"), dict) else {}
    dry_runs = raw.get("dry_runs") if isinstance(raw.get("dry_runs"), dict) else {}
    return {"processed": processed, "dry_runs": dry_runs}


def load_state(path=STATE_FILE):
    warnings = []
    if not os.path.exists(path):
        return {"processed": {}, "dry_runs": {}}, {"path": path, "exists": False}, [f"state_file_missing:{path}"]
    raw = load_json_file(path, {})
    if not isinstance(raw, dict):
        warnings.append("state_file_invalid_json_object")
    state = normalize_state(raw)
    stats = {
        "path": path,
        "exists": True,
        "processed_count": len(state["processed"]),
        "dry_run_count": len(state["dry_runs"]),
    }
    return state, stats, warnings


def decision_event_key(ledger, signal_id, decision):
    seed = {
        "ledger": ledger,
        "signal_id": signal_id,
        "status": decision.get("status"),
        "mode": decision.get("mode"),
        "checked_at": decision.get("checked_at"),
        "submitted_at": decision.get("submitted_at"),
        "error_at": decision.get("error_at"),
        "decision": decision,
    }
    return hashlib.sha256(stable_json(seed).encode("utf-8")).hexdigest()[:24]


def build_events(state):
    events = []
    duplicate_count = 0
    seen = set()
    for ledger in ("dry_runs", "processed"):
        items = state.get(ledger) or {}
        for signal_id, decision in sorted(items.items()):
            if not isinstance(decision, dict):
                continue
            sid = str(decision.get("signal_id") or signal_id)
            key = decision_event_key(ledger, sid, decision)
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            events.append({"event_key": key, "ledger": ledger, "signal_id": sid, "decision": decision})
    return events, {"duplicate_count": duplicate_count}


def gate_status(decision, key):
    value = decision.get(key)
    return value.get("status") if isinstance(value, dict) else None


def plan_value(decision, key):
    plan = decision.get("plan")
    return plan.get(key) if isinstance(plan, dict) else None


def schema_sql(table_name=DEFAULT_TABLE):
    table = quote_ident(table_name)
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    event_key TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    ledger TEXT NOT NULL,
    mode TEXT,
    status TEXT,
    symbol TEXT,
    side TEXT,
    quantity INTEGER,
    price_reference NUMERIC,
    notional_hkd NUMERIC,
    risk_hkd NUMERIC,
    checked_at TEXT,
    submitted_at TEXT,
    error_at TEXT,
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    strategy_evidence_status TEXT,
    symbol_conflict_status TEXT,
    market_context_status TEXT,
    hermes_status TEXT,
    order_id TEXT,
    order_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    order_result JSONB,
    decision_json JSONB NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    seen_count INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS {table}_signal_ledger_idx
    ON {table} (signal_id, ledger);
CREATE INDEX IF NOT EXISTS {table}_status_idx
    ON {table} (status);
CREATE INDEX IF NOT EXISTS {table}_symbol_idx
    ON {table} (symbol);
CREATE INDEX IF NOT EXISTS {table}_submitted_idx
    ON {table} (submitted_at);
CREATE INDEX IF NOT EXISTS {table}_order_id_idx
    ON {table} (order_id);
""".strip()


def schema_hash(table_name=DEFAULT_TABLE):
    return hashlib.sha256(schema_sql(table_name).encode("utf-8")).hexdigest()[:16]


def batch_hash(events, table_name=DEFAULT_TABLE):
    seed = {
        "table": table_name,
        "schema_hash": schema_hash(table_name),
        "event_keys": [event["event_key"] for event in events],
    }
    return hashlib.sha256(stable_json(seed).encode("utf-8")).hexdigest()[:16]


def upsert_sql(event, table_name=DEFAULT_TABLE):
    table = quote_ident(table_name)
    decision = event["decision"]
    order_result = decision.get("order_result")
    order_ids = order_result_ids(order_result)
    values = {
        "event_key": sql_quote(event["event_key"]),
        "signal_id": sql_quote(event["signal_id"]),
        "ledger": sql_quote(event["ledger"]),
        "mode": sql_quote(decision.get("mode")),
        "status": sql_quote(decision.get("status")),
        "symbol": sql_quote(plan_value(decision, "symbol") or (decision.get("alert") or {}).get("symbol")),
        "side": sql_quote(plan_value(decision, "side") or (decision.get("alert") or {}).get("signal_type")),
        "quantity": sql_int(plan_value(decision, "quantity")),
        "price_reference": sql_number(plan_value(decision, "price_reference")),
        "notional_hkd": sql_number(plan_value(decision, "notional_hkd")),
        "risk_hkd": sql_number(plan_value(decision, "risk_hkd")),
        "checked_at": sql_quote(decision.get("checked_at")),
        "submitted_at": sql_quote(decision.get("submitted_at")),
        "error_at": sql_quote(decision.get("error_at")),
        "reasons": sql_quote(stable_json(decision.get("reasons") or [])) + "::jsonb",
        "strategy_evidence_status": sql_quote(gate_status(decision, "strategy_evidence")),
        "symbol_conflict_status": sql_quote(gate_status(decision, "symbol_conflict")),
        "market_context_status": sql_quote(gate_status(decision, "market_context")),
        "hermes_status": sql_quote(gate_status(decision, "hermes")),
        "order_id": sql_quote(order_ids[0]) if order_ids else "NULL",
        "order_ids": sql_quote(stable_json(order_ids)) + "::jsonb",
        "order_result": (sql_quote(stable_json(order_result)) + "::jsonb") if isinstance(order_result, dict) else "NULL",
        "decision_json": sql_quote(stable_json(decision)) + "::jsonb",
    }
    columns = list(values)
    assignments = [f"{column} = EXCLUDED.{column}" for column in columns if column != "event_key"]
    assignments.extend([f"last_seen_at = NOW()", f"seen_count = {table}.seen_count + 1"])
    return (
        f"INSERT INTO {table} ({', '.join(columns)})\n"
        f"VALUES ({', '.join(values[column] for column in columns)})\n"
        "ON CONFLICT (event_key) DO UPDATE SET\n    "
        + ",\n    ".join(assignments)
        + ";"
    )


def build_sql_script(events, table_name=DEFAULT_TABLE):
    statements = ["BEGIN;", schema_sql(table_name)]
    statements.extend(upsert_sql(event, table_name) for event in events)
    statements.append("COMMIT;")
    return "\n".join(statements) + "\n"


def event_summary(events):
    by_ledger = {}
    by_status = {}
    by_mode = {}
    submitted_count = 0
    rejected_count = 0
    error_count = 0
    for event in events:
        decision = event["decision"]
        ledger = event["ledger"]
        status = str(decision.get("status") or "missing")
        mode = str(decision.get("mode") or ("execute" if ledger == "processed" else "dry-run"))
        by_ledger[ledger] = by_ledger.get(ledger, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        by_mode[mode] = by_mode.get(mode, 0) + 1
        if status == "submitted":
            submitted_count += 1
        elif status == "rejected":
            rejected_count += 1
        elif status == "error":
            error_count += 1
    return {
        "by_ledger": dict(sorted(by_ledger.items())),
        "by_status": dict(sorted(by_status.items())),
        "by_mode": dict(sorted(by_mode.items())),
        "submitted_count": submitted_count,
        "rejected_count": rejected_count,
        "error_count": error_count,
    }


def lineage_summary(events):
    submitted = []
    submitted_with_order_id = []
    submitted_missing_order_id = []
    processed_with_order_id = []
    order_ids = []
    for event in events:
        decision = event["decision"]
        if decision.get("status") != "submitted":
            continue
        ids = order_result_ids(decision.get("order_result"))
        row = {
            "ledger": event.get("ledger"),
            "signal_id": event.get("signal_id"),
            "symbol": plan_value(decision, "symbol") or (decision.get("alert") or {}).get("symbol"),
            "side": plan_value(decision, "side") or (decision.get("alert") or {}).get("signal_type"),
            "submitted_at": decision.get("submitted_at"),
            "order_ids": ids,
        }
        submitted.append(row)
        if ids:
            submitted_with_order_id.append(row)
            order_ids.extend(ids)
            if event.get("ledger") == "processed":
                processed_with_order_id.append(row)
        else:
            submitted_missing_order_id.append(row)

    reasons = []
    if submitted and submitted_missing_order_id:
        reasons.append("submitted_order_id_missing")
    status = "NO_SUBMITTED_ORDERS"
    if submitted:
        status = "OK" if not reasons else "DEGRADED"
    return {
        "schema": "rt_order_intake_lineage_summary_v1",
        "status": status,
        "submitted_count": len(submitted),
        "submitted_with_order_id_count": len(submitted_with_order_id),
        "submitted_missing_order_id_count": len(submitted_missing_order_id),
        "processed_submitted_with_order_id_count": len(processed_with_order_id),
        "unique_order_id_count": len(unique_text(order_ids)),
        "sample_order_ids": unique_text(order_ids)[:10],
        "reason_codes": reasons,
        "sample_submitted": submitted[:10],
    }


def build_report(
    state_file=STATE_FILE,
    table_name=DEFAULT_TABLE,
    apply=False,
    confirm_schema_hash="",
    broker=BROKER_RECONCILIATION_BROKER,
    broker_order_limit=ALPACA_RECONCILIATION_ORDER_LIMIT,
    order_fetcher=None,
):
    reasons = []
    try:
        quote_ident(table_name)
    except ValueError:
        reasons.append("invalid_table_name")
    state, state_stats, warnings = load_state(state_file)
    events, event_stats = build_events(state)
    broker_recon = broker_reconciliation(
        events,
        broker=broker,
        order_limit=broker_order_limit,
        order_fetcher=order_fetcher,
    )
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
            apply_result = {"status": "noop", "reason": "no_intake_events_to_ingest"}
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
        "schema": "rt_order_intake_event_store_report_v1",
        "generated_at": now_iso(),
        "mode": "apply" if apply else "dry-run",
        "status": status,
        "state_file": state_file,
        "table_name": table_name,
        "schema_hash": current_schema_hash,
        "confirm_schema_hash": confirm_schema_hash,
        "batch_hash": current_batch_hash,
        "state_stats": state_stats,
        "event_count": len(events),
        "duplicate_count": event_stats["duplicate_count"],
        "event_summary": event_summary(events),
        "lineage_summary": lineage_summary(events),
        "broker_reconciliation": broker_recon,
        "warnings": warnings,
        "validation_reasons": reasons,
        "applied": applied,
        "apply_result": apply_result,
        "safety": {
            "dry_run_by_default": True,
            "requires_confirm_schema_hash": True,
            "idempotent_on_event_key": True,
            "does_not_submit_orders": True,
            "does_not_change_intake_state": True,
            "does_not_change_strategy_config": True,
            "does_not_restart_services": True,
            "writes_audit_table_only": True,
        },
    }


def build_text_report(payload):
    lines = [
        f"RT order intake event store {payload['generated_at']}",
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
    lines.append("by_ledger=" + json.dumps(payload["event_summary"]["by_ledger"], ensure_ascii=False))
    lines.append("by_status=" + json.dumps(payload["event_summary"]["by_status"], ensure_ascii=False))
    lineage = payload.get("lineage_summary") or {}
    lines.append(
        "lineage="
        + json.dumps(
            {
                "status": lineage.get("status"),
                "submitted_with_order_id_count": lineage.get("submitted_with_order_id_count"),
                "submitted_missing_order_id_count": lineage.get("submitted_missing_order_id_count"),
            },
            ensure_ascii=False,
        )
    )
    if payload.get("apply_result"):
        lines.append("apply_result=" + json.dumps(payload["apply_result"], ensure_ascii=False))
    broker_recon = payload.get("broker_reconciliation") or {}
    if broker_recon:
        lines.append(
            "broker_reconciliation="
            + json.dumps(
                {
                    "status": broker_recon.get("status"),
                    "broker": broker_recon.get("broker"),
                    "broker_order_count": broker_recon.get("broker_order_count"),
                    "system_broker_order_count": broker_recon.get("system_broker_order_count"),
                    "external_broker_order_count": broker_recon.get("external_broker_order_count"),
                    "matched_order_count": broker_recon.get("matched_order_count"),
                    "unmatched_broker_order_count": broker_recon.get("unmatched_broker_order_count"),
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", default=STATE_FILE)
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
        state_file=args.state_file,
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
