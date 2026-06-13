#!/usr/bin/env python3
"""Read-only inventory of production data sources visible to Hermes/v5.

This report answers "what can the system actually see?" before source
reliability decides whether those inputs are good enough to trust.
"""
import argparse
import json
import os
import subprocess
from collections import Counter
from datetime import datetime


DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
REPORT_FILE = os.environ.get("DATA_SOURCE_INVENTORY_REPORT_FILE", "/tmp/data_source_inventory_report.json")
MAX_FILE_AGE_MINUTES = float(os.environ.get("DATA_SOURCE_INVENTORY_MAX_FILE_AGE_MINUTES", "120"))
SIM_PORTFOLIO_ID = int(os.environ.get("QM_SIM_PORTFOLIO_ID", os.environ.get("QM_PORTFOLIO_ID", "8")))
USER_PORTFOLIO_IDS = [
    int(value.strip())
    for value in os.environ.get("QM_USER_PORTFOLIO_IDS", os.environ.get("QM_USER_PORTFOLIO_ID", "")).split(",")
    if value.strip().isdigit()
]

CORE_TABLES = (
    "stocks",
    "klines",
    "engine_signal_scores",
    "engine_feature_runs",
    "portfolios",
    "positions",
    "sim_trades",
)
CRITICAL_TABLES = {"stocks", "klines", "portfolios", "positions"}

CONTEXT_REPORT_FILES = [
    ("data_health", "/tmp/data_health_report.json", "data_health_report_v1", ("generated_at", "checked_at")),
    ("kline_source_granularity", "/tmp/kline_source_granularity_report.json", "kline_source_granularity_report_v1", ("generated_at",)),
    ("market_context", "/tmp/market_context_report.json", "market_context_report_v1", ("generated_at",)),
    ("intraday_kline_batch", "/tmp/intraday_kline_batch.json", "intraday_kline_batch_report_v1", ("generated_at",)),
    ("intraday_context", "/tmp/intraday_context_report.json", "intraday_context_report_v1", ("generated_at",)),
    (
        "intraday_timeframe_quality",
        "/tmp/intraday_timeframe_quality_report.json",
        "intraday_timeframe_quality_report_v1",
        ("generated_at",),
    ),
    ("external_market_context", "/tmp/external_market_context_report.json", "external_market_context_report_v1", ("generated_at",)),
    ("event_catalysts", "/tmp/event_catalyst_report.json", "event_catalyst_report_v1", ("generated_at",)),
    ("event_catalyst_signals", "/tmp/event_catalyst_signal_report.json", "event_catalyst_signal_report_v1", ("generated_at",)),
    ("market_sentiment", "/tmp/market_sentiment_report.json", "market_sentiment_report_v1", ("generated_at",)),
    ("fundamentals_context", "/tmp/fundamentals_context_report.json", "fundamentals_context_report_v1", ("generated_at",)),
    ("trusted_source_discovery", "/tmp/trusted_source_discovery_report.json", "trusted_source_discovery_report_v1", ("generated_at",)),
    ("trusted_source_preflight", "/tmp/trusted_source_preflight_report.json", "trusted_source_preflight_report_v1", ("generated_at",)),
    ("source_reliability", "/tmp/source_reliability_report.json", "source_reliability_report_v1", ("generated_at",)),
    ("portfolio_report", "/tmp/portfolio_report.json", "portfolio_context_report_v1", ("generated_at",)),
    ("simulation_performance", "/tmp/simulation_performance_report.json", "simulation_performance_report_v1", ("generated_at",)),
    ("execution_readiness", "/tmp/execution_readiness_report.json", "execution_readiness_report_v1", ("generated_at",)),
]

INPUT_PAYLOAD_FILES = [
    ("external_market_context_json", "/tmp/external_market_context_inputs.json"),
    ("external_market_context_jsonl", "/tmp/external_market_context_inputs.jsonl"),
    ("market_sentiment_json", "/tmp/market_sentiment_inputs.json"),
    ("market_sentiment_jsonl", "/tmp/market_sentiment_inputs.jsonl"),
    ("fundamentals_context_json", "/tmp/fundamentals_context_inputs.json"),
    ("fundamentals_context_jsonl", "/tmp/fundamentals_context_inputs.jsonl"),
    ("market_index_context_json", "/tmp/market_index_context_inputs.json"),
    ("watchlist_json", "/root/rt_signal_watchlist.json"),
]

SEVERITY_RANK = {"INFO": 0, "WARN": 1, "ERROR": 2}
_COLUMN_CACHE = {}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
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


def run_cmd(args, timeout=90):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": str(exc)})()


def psql(sql, timeout=90):
    return run_cmd(
        [
            "docker",
            "exec",
            DB_CONTAINER,
            "psql",
            "-U",
            DB_USER,
            "-d",
            DB_NAME,
            "-t",
            "-A",
            "-F",
            "\t",
            "-c",
            sql,
        ],
        timeout=timeout,
    )


def rows(stdout):
    return [line.rstrip("\n").split("\t") for line in stdout.splitlines() if line.strip()]


def sql_quote(value):
    return str(value).replace("'", "''")


def safe_identifier(value):
    text = str(value or "")
    if not text.replace("_", "").isalnum():
        raise ValueError(f"unsafe identifier:{text}")
    return text


def as_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def bounded(values, limit=20):
    return [str(value)[:240] for value in (values or [])[:limit]]


def table_columns(table):
    table = safe_identifier(table)
    if table in _COLUMN_CACHE:
        return _COLUMN_CACHE[table]
    r = psql(
        f"""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = '{sql_quote(table)}'
        """
    )
    cols = {row[0] for row in rows(r.stdout)} if r.returncode == 0 else set()
    _COLUMN_CACHE[table] = cols
    return cols


def first_existing(table, candidates, fallback=None):
    cols = table_columns(table)
    for candidate in candidates:
        if candidate in cols:
            return candidate
    return fallback


def scalar(sql, timeout=90):
    r = psql(sql, timeout=timeout)
    if r.returncode != 0:
        return None, r.stderr.strip()[:240]
    parsed = rows(r.stdout)
    if not parsed or not parsed[0]:
        return None, None
    return parsed[0][0], None


def fetch_table_summaries(tables=CORE_TABLES):
    summaries = []
    warnings = []
    _COLUMN_CACHE.clear()
    for table in tables:
        table = safe_identifier(table)
        cols = table_columns(table)
        if not cols:
            summaries.append({"table": table, "exists": False, "columns": [], "row_count": None})
            continue
        row_count, error = scalar(f"SELECT count(*) FROM {table}")
        if error:
            warnings.append(f"table_count_failed:{table}:{error}")
        timestamp_col = first_existing(table, ("updated_at", "created_at", "timestamp", "trade_date"))
        min_value = None
        max_value = None
        if timestamp_col:
            min_value, min_error = scalar(f"SELECT min({timestamp_col}) FROM {table}", timeout=120)
            max_value, max_error = scalar(f"SELECT max({timestamp_col}) FROM {table}", timeout=120)
            if min_error:
                warnings.append(f"table_min_timestamp_failed:{table}:{min_error}")
            if max_error:
                warnings.append(f"table_max_timestamp_failed:{table}:{max_error}")
        summaries.append(
            {
                "table": table,
                "exists": True,
                "columns": sorted(cols),
                "column_count": len(cols),
                "row_count": as_int(row_count, 0) if row_count is not None else None,
                "primary_time_column": timestamp_col,
                "min_time": min_value,
                "max_time": max_value,
            }
        )
    return summaries, warnings


def fetch_kline_source_rows():
    cols = table_columns("klines")
    if not cols:
        return [], ["klines_table_missing"]
    if "interval" not in cols or "symbol" not in cols or "timestamp" not in cols:
        return [], ["klines_required_columns_missing"]
    data_source_expr = "COALESCE(NULLIF(data_source, ''), 'missing')" if "data_source" in cols else "'missing'"
    granularity_expr = (
        "COALESCE(NULLIF(source_granularity, ''), 'missing')" if "source_granularity" in cols else "'missing'"
    )
    sql = f"""
        SELECT COALESCE(NULLIF(interval, ''), 'missing') AS interval,
               {data_source_expr} AS data_source,
               {granularity_expr} AS source_granularity,
               count(*) AS row_count,
               count(DISTINCT symbol) AS symbol_count,
               min(timestamp) AS min_timestamp,
               max(timestamp) AS max_timestamp
        FROM klines
        GROUP BY 1, 2, 3
        ORDER BY 1, row_count DESC, data_source
        LIMIT 200
    """
    r = psql(sql, timeout=120)
    if r.returncode != 0:
        return [], [f"kline_source_inventory_query_failed:{r.stderr.strip()[:240]}"]
    parsed = []
    for row in rows(r.stdout):
        if len(row) < 7:
            continue
        parsed.append(
            {
                "interval": row[0],
                "data_source": row[1],
                "source_granularity": row[2],
                "row_count": as_int(row[3]),
                "symbol_count": as_int(row[4]),
                "min_timestamp": row[5],
                "max_timestamp": row[6],
            }
        )
    return parsed, []


def fetch_signal_source_rows():
    cols = table_columns("engine_signal_scores")
    if not cols:
        return [], ["engine_signal_scores_table_missing"]
    required = {"model_version", "feature_version", "trade_date"}
    if not required.issubset(cols):
        return [], ["engine_signal_scores_required_columns_missing"]
    sql = """
        SELECT COALESCE(NULLIF(model_version, ''), 'missing') AS model_version,
               COALESCE(NULLIF(feature_version, ''), 'missing') AS feature_version,
               count(*) AS row_count,
               count(DISTINCT symbol) AS symbol_count,
               min(trade_date) AS min_trade_date,
               max(trade_date) AS max_trade_date
        FROM engine_signal_scores
        GROUP BY 1, 2
        ORDER BY row_count DESC
        LIMIT 100
    """
    r = psql(sql, timeout=120)
    if r.returncode != 0:
        return [], [f"signal_source_inventory_query_failed:{r.stderr.strip()[:240]}"]
    parsed = []
    for row in rows(r.stdout):
        if len(row) < 6:
            continue
        parsed.append(
            {
                "model_version": row[0],
                "feature_version": row[1],
                "row_count": as_int(row[2]),
                "symbol_count": as_int(row[3]),
                "min_trade_date": row[4],
                "max_trade_date": row[5],
            }
        )
    return parsed, []


def fetch_portfolio_rows():
    if not table_columns("portfolios"):
        return [], ["portfolios_table_missing"]
    position_cols = table_columns("positions")
    name_expr = first_existing("portfolios", ("name", "portfolio_name"), "''")
    if position_cols:
        quantity_expr = first_existing("positions", ("quantity", "shares"), "0")
        sql = f"""
            SELECT p.id, {name_expr} AS name,
                   count(pos.*) AS position_count,
                   count(pos.*) FILTER (WHERE COALESCE({quantity_expr}, 0) <> 0) AS open_position_count
            FROM portfolios p
            LEFT JOIN positions pos ON pos.portfolio_id = p.id
            GROUP BY p.id, name
            ORDER BY p.id
        """
    else:
        sql = f"SELECT id, {name_expr} AS name, 0 AS position_count, 0 AS open_position_count FROM portfolios ORDER BY id"
    r = psql(sql)
    if r.returncode != 0:
        return [], [f"portfolio_inventory_query_failed:{r.stderr.strip()[:240]}"]
    parsed = []
    for row in rows(r.stdout):
        if len(row) < 4:
            continue
        portfolio_id = as_int(row[0])
        parsed.append(
            {
                "portfolio_id": portfolio_id,
                "name": row[1],
                "role_hint": "simulation" if portfolio_id == SIM_PORTFOLIO_ID else "user_or_other",
                "position_count": as_int(row[2]),
                "open_position_count": as_int(row[3]),
            }
        )
    return parsed, []


def report_timestamp(payload, keys):
    for key in keys:
        parsed = parse_timestamp(payload.get(key))
        if parsed:
            return key, parsed, payload.get(key)
    return None, None, None


def count_collection_items(loaded):
    if isinstance(loaded, list):
        return len(loaded)
    if not isinstance(loaded, dict):
        return None
    for key in ("items", "contexts", "indicators", "fundamentals", "signals", "actions", "providers"):
        if isinstance(loaded.get(key), list):
            return len(loaded[key])
    return None


def summarize_json_file(name, path, expected_schema=None, timestamp_keys=("generated_at",), now=None, max_age_minutes=MAX_FILE_AGE_MINUTES):
    now = now or datetime.now()
    row = {
        "name": name,
        "path": path,
        "exists": bool(path and os.path.exists(path)),
        "expected_schema": expected_schema,
    }
    if not row["exists"]:
        return row
    try:
        row["size_bytes"] = os.path.getsize(path)
        row["modified_at"] = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
        if path.endswith(".jsonl"):
            line_count = 0
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        line_count += 1
            row["line_count"] = line_count
            return row
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            row["schema"] = loaded.get("schema")
            row["status"] = loaded.get("status")
            row["summary"] = loaded.get("summary") if isinstance(loaded.get("summary"), dict) else {}
            row["item_count"] = count_collection_items(loaded)
            _key, ts, ts_raw = report_timestamp(loaded, timestamp_keys)
            row["timestamp"] = ts_raw
            row["age_minutes"] = round((now - ts).total_seconds() / 60, 2) if ts else None
            row["stale"] = bool(ts and (row["age_minutes"] > max_age_minutes or row["age_minutes"] < -5))
            row["schema_valid"] = expected_schema is None or loaded.get("schema") == expected_schema
        elif isinstance(loaded, list):
            row["item_count"] = len(loaded)
    except Exception as exc:
        row["read_error"] = str(exc)[:240]
    return row


def fetch_context_file_rows(now=None, max_age_minutes=MAX_FILE_AGE_MINUTES):
    return [
        summarize_json_file(name, path, schema, keys, now=now, max_age_minutes=max_age_minutes)
        for name, path, schema, keys in CONTEXT_REPORT_FILES
    ]


def fetch_input_file_rows(now=None):
    now = now or datetime.now()
    rows_out = []
    for name, path in INPUT_PAYLOAD_FILES:
        rows_out.append(summarize_json_file(name, path, None, ("generated_at",), now=now, max_age_minutes=24 * 60))
    return rows_out


def source_token_counts(kline_source_rows):
    counts = Counter()
    for row in kline_source_rows or []:
        source = str(row.get("data_source") or "missing")
        counts[source] += as_int(row.get("row_count"))
    return dict(sorted(counts.items()))


def weakness(code, severity, detail, evidence=None):
    return {"code": code, "severity": severity, "detail": detail, "evidence": evidence or {}}


def build_weaknesses(table_summaries, kline_source_rows, context_file_rows, input_file_rows, warnings):
    weaknesses = []
    by_table = {row.get("table"): row for row in table_summaries or []}
    missing_critical = sorted(table for table in CRITICAL_TABLES if not (by_table.get(table) or {}).get("exists"))
    if missing_critical:
        weaknesses.append(
            weakness(
                "critical_database_tables_missing",
                "ERROR",
                "Required DB tables are not visible to the inventory report.",
                {"tables": missing_critical},
            )
        )
    missing_optional = sorted(
        table for table in CORE_TABLES if table not in CRITICAL_TABLES and not (by_table.get(table) or {}).get("exists")
    )
    if missing_optional:
        weaknesses.append(
            weakness(
                "optional_database_tables_missing",
                "WARN",
                "Some audit or simulation tables are not visible.",
                {"tables": missing_optional},
            )
        )
    klines = by_table.get("klines") or {}
    if klines.get("exists") and as_int(klines.get("row_count")) <= 0:
        weaknesses.append(
            weakness("kline_table_empty", "ERROR", "K-line table is visible but has no rows.", {"table": "klines"})
        )
    missing_source_rows = [
        row for row in kline_source_rows or [] if str(row.get("data_source") or "").lower() in ("", "missing", "null")
    ]
    if missing_source_rows:
        weaknesses.append(
            weakness(
                "kline_data_source_missing",
                "WARN",
                "Some K-line rows have missing data_source provenance.",
                {"rows": missing_source_rows[:10]},
            )
        )
    minute_rows = [row for row in kline_source_rows or [] if row.get("interval") in ("min", "1m")]
    if minute_rows and all(str(row.get("source_granularity") or "missing") == "missing" for row in minute_rows):
        weaknesses.append(
            weakness(
                "minute_source_granularity_missing",
                "WARN",
                "Minute rows are visible but lack source_granularity, so path evidence cannot prove full-OHLCV fidelity.",
                {"intervals": sorted({row.get("interval") for row in minute_rows})},
            )
        )
    missing_context = [row.get("name") for row in context_file_rows or [] if not row.get("exists")]
    stale_context = [row.get("name") for row in context_file_rows or [] if row.get("stale")]
    invalid_context = [row.get("name") for row in context_file_rows or [] if row.get("exists") and row.get("schema_valid") is False]
    if missing_context:
        weaknesses.append(
            weakness(
                "context_reports_missing",
                "WARN",
                "Some Hermes/v5 context reports are not present on disk.",
                {"reports": missing_context[:20]},
            )
        )
    if stale_context:
        weaknesses.append(
            weakness(
                "context_reports_stale",
                "WARN",
                "Some Hermes/v5 context reports are older than the inventory freshness threshold.",
                {"reports": stale_context[:20]},
            )
        )
    if invalid_context:
        weaknesses.append(
            weakness(
                "context_report_schema_mismatch",
                "WARN",
                "Some context report schemas do not match the expected contract.",
                {"reports": invalid_context[:20]},
            )
        )
    external_inputs = [row for row in input_file_rows or [] if row.get("name", "").startswith("external_market_context")]
    if external_inputs and not any(row.get("exists") for row in external_inputs):
        weaknesses.append(
            weakness(
                "external_context_input_payloads_missing",
                "WARN",
                "No external market context input JSON/JSONL payload is visible.",
                {"files": [row.get("path") for row in external_inputs]},
            )
        )
    if warnings:
        weaknesses.append(
            weakness(
                "inventory_query_warnings",
                "WARN",
                "One or more inventory probes returned warnings.",
                {"warnings": bounded(warnings)},
            )
        )
    return weaknesses


def classify_status(weaknesses):
    severities = [row.get("severity") for row in weaknesses or []]
    if "ERROR" in severities:
        return "FAIL"
    if "WARN" in severities:
        return "DEGRADED"
    return "OK"


def build_recommendations(weaknesses):
    recs = []
    for row in weaknesses or []:
        code = row.get("code")
        if code == "critical_database_tables_missing":
            recs.append("restore_or_configure_core_database_tables_before_trusting_hermes_context")
        elif code == "optional_database_tables_missing":
            recs.append("review_missing_audit_or_simulation_tables_before_claiming_full_operational_visibility")
        elif code == "kline_table_empty":
            recs.append("load_or_repair_kline_history_before_running_v5_or_hermes_reviews")
        elif code == "kline_data_source_missing":
            recs.append("backfill_or_explain_kline_data_source_provenance")
        elif code == "minute_source_granularity_missing":
            recs.append("persist_minute_source_granularity_before_using_intraday_path_evidence_as_full_ohlcv")
        elif code == "context_reports_missing":
            recs.append("refresh_missing_context_reports_before_hermes_review")
        elif code == "context_reports_stale":
            recs.append("refresh_stale_context_reports_before_hermes_review")
        elif code == "context_report_schema_mismatch":
            recs.append("fix_context_report_schema_mismatch_before_embedding_in_packet")
        elif code == "external_context_input_payloads_missing":
            recs.append("wire_external_market_context_input_payloads_or_disclose_limited_event_awareness")
        elif code == "inventory_query_warnings":
            recs.append("inspect_inventory_probe_warnings_before_claiming_data_visibility")
    return sorted(set(recs)) or ["data_source_inventory_clean"]


def build_report(
    table_summaries=None,
    kline_source_rows=None,
    signal_source_rows=None,
    portfolio_rows=None,
    context_file_rows=None,
    input_file_rows=None,
    warnings=None,
    now=None,
    max_file_age_minutes=MAX_FILE_AGE_MINUTES,
):
    now = now or datetime.now()
    warnings = list(warnings or [])
    if table_summaries is None:
        table_summaries, table_warnings = fetch_table_summaries()
        warnings.extend(table_warnings)
    if kline_source_rows is None:
        kline_source_rows, kline_warnings = fetch_kline_source_rows()
        warnings.extend(kline_warnings)
    if signal_source_rows is None:
        signal_source_rows, signal_warnings = fetch_signal_source_rows()
        warnings.extend(signal_warnings)
    if portfolio_rows is None:
        portfolio_rows, portfolio_warnings = fetch_portfolio_rows()
        warnings.extend(portfolio_warnings)
    if context_file_rows is None:
        context_file_rows = fetch_context_file_rows(now=now, max_age_minutes=max_file_age_minutes)
    if input_file_rows is None:
        input_file_rows = fetch_input_file_rows(now=now)

    weaknesses = build_weaknesses(table_summaries, kline_source_rows, context_file_rows, input_file_rows, warnings)
    status = classify_status(weaknesses)
    table_status_counts = Counter("present" if row.get("exists") else "missing" for row in table_summaries or [])
    context_status_counts = Counter(
        "missing"
        if not row.get("exists")
        else "stale"
        if row.get("stale")
        else "schema_mismatch"
        if row.get("schema_valid") is False
        else "present"
        for row in context_file_rows or []
    )
    source_counts = source_token_counts(kline_source_rows)
    return {
        "schema": "data_source_inventory_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "queries_database": True,
            "reads_tmp_context_files": True,
            "submits_orders": False,
            "writes_database": False,
            "changes_crontab": False,
            "changes_strategy": False,
            "changes_portfolio": False,
            "repairs_data": False,
            "db_container": DB_CONTAINER,
            "db_name": DB_NAME,
            "secret_values_redacted": True,
            "max_file_age_minutes": max_file_age_minutes,
        },
        "summary": {
            "table_count": len(table_summaries or []),
            "table_status_counts": dict(table_status_counts),
            "context_file_count": len(context_file_rows or []),
            "context_file_status_counts": dict(context_status_counts),
            "input_payload_file_count": len(input_file_rows or []),
            "present_input_payload_file_count": len([row for row in input_file_rows or [] if row.get("exists")]),
            "kline_source_counts": source_counts,
            "kline_source_token_count": len(source_counts),
            "signal_source_count": len(signal_source_rows or []),
            "portfolio_count": len(portfolio_rows or []),
            "weakness_count": len(weaknesses),
            "error_weakness_count": len([row for row in weaknesses if row.get("severity") == "ERROR"]),
            "warning_weakness_count": len([row for row in weaknesses if row.get("severity") == "WARN"]),
        },
        "database": {
            "tables": table_summaries or [],
            "kline_sources": kline_source_rows or [],
            "signal_sources": signal_source_rows or [],
            "portfolios": portfolio_rows or [],
            "configured_portfolio_scope": {
                "simulation_portfolio_id": SIM_PORTFOLIO_ID,
                "user_portfolio_ids": USER_PORTFOLIO_IDS,
                "separate_user_and_simulation_ids": all(pid != SIM_PORTFOLIO_ID for pid in USER_PORTFOLIO_IDS),
            },
        },
        "files": {
            "context_reports": context_file_rows or [],
            "input_payloads": input_file_rows or [],
        },
        "weaknesses": weaknesses,
        "recommendations": build_recommendations(weaknesses),
        "hermes_use": [
            "Use this inventory to verify which DB tables, K-line provenance fields, context reports, and provider payload files are visible.",
            "Visibility is not trust: source_reliability_report.py remains the quality gate for freshness, fallback providers, and incomplete provenance.",
            "Missing context reports or missing provider payloads must be disclosed as limited awareness, not treated as neutral market evidence.",
        ],
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Data source inventory {payload.get('generated_at')} status={payload.get('status')}",
        (
            f"tables={summary.get('table_status_counts', {})} "
            f"context_files={summary.get('context_file_status_counts', {})} "
            f"kline_sources={summary.get('kline_source_counts', {})} "
            f"weaknesses={summary.get('weakness_count')}"
        ),
    ]
    for row in payload.get("weaknesses") or []:
        lines.append(f"  {row.get('severity')} {row.get('code')}: {row.get('detail')}")
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--max-file-age-minutes", type=float, default=MAX_FILE_AGE_MINUTES)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(max_file_age_minutes=args.max_file_age_minutes)
    if args.output:
        save_json_atomic(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.text or not args.output:
        print(build_text_report(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
