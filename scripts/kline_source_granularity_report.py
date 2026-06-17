#!/usr/bin/env python3
"""Dry-run-first proposal for K-line source_granularity provenance.

The report closes a specific intraday reliability gap: minute rows can exist in
klines, but Hermes cannot know whether they are full OHLCV bars or snapshot-like
points unless source_granularity is persisted.
"""
import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime


DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
REPORT_FILE = os.environ.get("KLINE_SOURCE_GRANULARITY_REPORT_FILE", "/tmp/kline_source_granularity_report.json")
BACKUP_DIR = os.environ.get("KLINE_SOURCE_GRANULARITY_BACKUP_DIR", "/tmp/kline_source_granularity_backups")

MISSING_VALUES = {"", "missing", "null", "unknown", "none"}

GRANULARITY_RULES = [
    {
        "id": "tencent_minute_snapshot",
        "intervals": ("min", "1m"),
        "data_sources": ("tencent_min", "tencent_minute_query"),
        "source_granularity": "minute_snapshot_price",
        "fidelity": "low_fidelity_snapshot",
        "reason": "Tencent public minute endpoint stores one price point per minute; high/low are not independently observed.",
    },
    {
        "id": "trusted_minute_ohlcv",
        "intervals": ("min", "1m"),
        "data_sources": (
            "broker_minute_ohlcv",
            "vendor_minute_ohlcv",
            "official_exchange_minute_ohlcv",
            "polygon_minute_ohlcv",
            "alpaca_minute_ohlcv",
            "futu_minute_ohlcv",
            "ibkr_minute_ohlcv",
            "full_minute_ohlcv",
        ),
        "source_granularity": "minute_ohlcv",
        "fidelity": "full_ohlcv_candidate",
        "reason": "Broker/vendor/official minute OHLCV source labels can support full path evidence after provider review.",
    },
    {
        "id": "daily_ohlcv",
        "intervals": ("day", "1d"),
        "data_sources": (
            "tencent",
            "tencent_day",
            "tencent_hk",
            "tencent_us",
            "tencent_day_repair",
            "alpaca_market_data",
            "alpaca_daily_ohlcv",
            "yfinance",
            "yahoo_chart",
        ),
        "source_granularity": "daily_ohlcv",
        "fidelity": "daily_bar",
        "reason": "Daily K-line rows are completed daily representative OHLCV bars; data_source still carries provider/repair provenance.",
    },
]


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


def run_cmd(args, timeout=120, input_text=None):
    try:
        return subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout)
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
    return [line.rstrip("\n").split("\t") for line in str(stdout or "").splitlines() if line.strip()]


def sql_quote(value):
    return str(value).replace("'", "''")


def sql_in(values):
    return ",".join(f"'{sql_quote(value)}'" for value in values) or "''"


def as_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("T", " "))
    except Exception:
        return None


def min_dt(values):
    parsed = [parse_dt(value) for value in values if value]
    parsed = [value for value in parsed if value is not None]
    return min(parsed).isoformat(sep=" ", timespec="seconds") if parsed else None


def max_dt(values):
    parsed = [parse_dt(value) for value in values if value]
    parsed = [value for value in parsed if value is not None]
    return max(parsed).isoformat(sep=" ", timespec="seconds") if parsed else None


def fetch_kline_columns():
    sql = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'klines'
        ORDER BY ordinal_position
    """
    result = psql(sql, timeout=30)
    if result.returncode != 0:
        return [], [f"kline_column_query_failed:{result.stderr.strip()}"]
    return [row[0] for row in rows(result.stdout) if row], []


def fetch_source_rows(columns):
    if not columns:
        return [], ["kline_table_missing_or_columns_unavailable"]
    granularity_expr = (
        "COALESCE(NULLIF(source_granularity, ''), 'missing')"
        if "source_granularity" in set(columns)
        else "'missing'"
    )
    sql = f"""
        SELECT COALESCE(NULLIF(interval, ''), 'missing') AS interval,
               COALESCE(NULLIF(data_source, ''), 'missing') AS data_source,
               {granularity_expr} AS source_granularity,
               COUNT(*) AS row_count,
               COUNT(DISTINCT symbol) AS symbol_count,
               MIN(timestamp) AS min_timestamp,
               MAX(timestamp) AS max_timestamp
        FROM klines
        GROUP BY 1,2,3
        ORDER BY 1,2,3
    """
    result = psql(sql)
    if result.returncode != 0:
        return [], [f"kline_source_granularity_query_failed:{result.stderr.strip()}"]
    output = []
    for row in rows(result.stdout):
        if len(row) < 7:
            continue
        output.append(
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
    return output, []


def normalized(value):
    return str(value or "").strip().lower()


def is_missing_granularity(value):
    return normalized(value) in MISSING_VALUES


def rows_for_rule(source_rows, rule):
    intervals = {normalized(value) for value in rule["intervals"]}
    sources = {normalized(value) for value in rule["data_sources"]}
    return [
        row
        for row in source_rows or []
        if normalized(row.get("interval")) in intervals
        and normalized(row.get("data_source")) in sources
        and is_missing_granularity(row.get("source_granularity"))
    ]


def aggregate_rule_rows(rule_rows):
    return {
        "estimated_row_count": sum(as_int(row.get("row_count")) for row in rule_rows),
        "estimated_symbol_count": sum(as_int(row.get("symbol_count")) for row in rule_rows),
        "min_timestamp": min_dt(row.get("min_timestamp") for row in rule_rows),
        "max_timestamp": max_dt(row.get("max_timestamp") for row in rule_rows),
        "source_breakdown": [
            {
                "interval": row.get("interval"),
                "data_source": row.get("data_source"),
                "source_granularity": row.get("source_granularity"),
                "row_count": row.get("row_count"),
                "symbol_count": row.get("symbol_count"),
                "min_timestamp": row.get("min_timestamp"),
                "max_timestamp": row.get("max_timestamp"),
            }
            for row in rule_rows
        ],
    }


def add_column_action():
    return {
        "id": "add_klines_source_granularity_column",
        "action": "add_column",
        "target": "klines.source_granularity",
        "changes_schema": True,
        "writes_database": True,
        "sql": (
            "ALTER TABLE klines ADD COLUMN IF NOT EXISTS source_granularity TEXT;\n"
            "COMMENT ON COLUMN klines.source_granularity IS "
            "'K-line bar fidelity/provenance such as minute_snapshot_price, minute_ohlcv, or daily_ohlcv';"
        ),
    }


def backfill_action(rule, rule_rows):
    agg = aggregate_rule_rows(rule_rows)
    return {
        "id": f"backfill_{rule['id']}",
        "action": "backfill_source_granularity",
        "target": "klines.source_granularity",
        "intervals": list(rule["intervals"]),
        "data_sources": list(rule["data_sources"]),
        "source_granularity": rule["source_granularity"],
        "fidelity": rule["fidelity"],
        "reason": rule["reason"],
        "changes_schema": False,
        "writes_database": True,
        **agg,
        "sql": (
            "UPDATE klines\n"
            f"SET source_granularity = '{sql_quote(rule['source_granularity'])}'\n"
            f"WHERE interval IN ({sql_in(rule['intervals'])})\n"
            f"  AND COALESCE(data_source, '') IN ({sql_in(rule['data_sources'])})\n"
            "  AND (source_granularity IS NULL OR source_granularity = '');"
        ),
    }


def unmapped_missing_rows(source_rows):
    mapped = set()
    for rule in GRANULARITY_RULES:
        for interval in rule["intervals"]:
            for source in rule["data_sources"]:
                mapped.add((normalized(interval), normalized(source)))
    issues = []
    for row in source_rows or []:
        if not is_missing_granularity(row.get("source_granularity")):
            continue
        key = (normalized(row.get("interval")), normalized(row.get("data_source")))
        if key in mapped:
            continue
        if as_int(row.get("row_count")) <= 0:
            continue
        issues.append(
            {
                "interval": row.get("interval"),
                "data_source": row.get("data_source"),
                "source_granularity": row.get("source_granularity"),
                "row_count": row.get("row_count"),
                "symbol_count": row.get("symbol_count"),
                "reason": "no_safe_granularity_mapping_for_source",
            }
        )
    return issues


def proposal_hash(actions):
    stable_actions = [
        {
            "id": action.get("id"),
            "action": action.get("action"),
            "target": action.get("target"),
            "intervals": action.get("intervals"),
            "data_sources": action.get("data_sources"),
            "source_granularity": action.get("source_granularity"),
            "estimated_row_count": action.get("estimated_row_count"),
            "sql": action.get("sql"),
        }
        for action in actions or []
    ]
    stable = json.dumps(stable_actions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def build_sql_script(actions):
    statements = ["BEGIN;"]
    for action in actions or []:
        sql = str(action.get("sql") or "").strip()
        if sql:
            statements.append(sql)
    statements.append("COMMIT;")
    return "\n".join(statements) + "\n"


def build_proposal(columns, source_rows):
    actions = []
    if "source_granularity" not in set(columns or []):
        actions.append(add_column_action())
    for rule in GRANULARITY_RULES:
        rule_rows = rows_for_rule(source_rows, rule)
        if rule_rows:
            actions.append(backfill_action(rule, rule_rows))
    digest = proposal_hash(actions)
    return {
        "schema": "kline_source_granularity_proposal_v1",
        "proposal_hash": digest,
        "manual_review_required": bool(actions),
        "auto_applied": False,
        "action_count": len(actions),
        "estimated_backfill_row_count": sum(as_int(action.get("estimated_row_count")) for action in actions),
        "actions": actions,
        "sql_script": build_sql_script(actions) if actions else None,
        "apply_command": (
            "/usr/bin/python3 /root/kline_source_granularity_report.py "
            f"--apply --confirm-proposal-hash {digest} "
            "--output /tmp/kline_source_granularity_report.json --text"
            if actions
            else None
        ),
        "operator_contract": {
            "dry_run_default": True,
            "apply_requires": "--apply --confirm-proposal-hash <proposal_hash>",
            "backs_up_metadata_before_apply": True,
            "does_not_submit_orders": True,
            "does_not_change_portfolios": True,
            "does_not_change_strategy": True,
            "does_not_change_crontab": True,
            "does_not_change_ohlcv_prices_or_volumes": True,
            "updates_only": ["klines.source_granularity", "optional klines.source_granularity column"],
        },
    }


def classify_status(columns, proposal, issues, warnings):
    if not columns:
        return "FAIL"
    if proposal.get("action_count"):
        return "ACTION_REQUIRED"
    if issues:
        return "REVIEW"
    if warnings:
        return "WARN"
    return "OK"


def build_recommendations(status, proposal, issues):
    recs = []
    action_ids = {action.get("id") for action in proposal.get("actions") or []}
    if "add_klines_source_granularity_column" in action_ids:
        recs.append("operator_review_hash_confirmed_source_granularity_column_addition")
    if any(str(action_id or "").startswith("backfill_") for action_id in action_ids):
        recs.append("operator_may_apply_hash_confirmed_source_granularity_backfill_after_review")
    if any(action.get("source_granularity") == "minute_snapshot_price" for action in proposal.get("actions") or []):
        recs.append("treat_tencent_minute_rows_as_snapshot_not_full_ohlcv_after_backfill")
    if issues:
        recs.append("do_not_infer_source_granularity_for_unmapped_sources")
    if status == "OK":
        recs.append("kline_source_granularity_contract_clean")
    return sorted(set(recs))


def build_report(columns=None, source_rows=None, warnings=None):
    warnings = list(warnings or [])
    if columns is None:
        columns, column_warnings = fetch_kline_columns()
        warnings.extend(column_warnings)
    if source_rows is None:
        source_rows, source_warnings = fetch_source_rows(columns)
        warnings.extend(source_warnings)
    proposal = build_proposal(columns, source_rows)
    issues = unmapped_missing_rows(source_rows)
    status = classify_status(columns, proposal, issues, warnings)
    return {
        "schema": "kline_source_granularity_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "dry_run_default": True,
            "read_only": True,
            "queries_database": True,
            "submits_orders": False,
            "writes_database": False,
            "changes_schema": False,
            "changes_strategy": False,
            "changes_crontab": False,
            "does_not_change_ohlcv_prices_or_volumes": True,
            "db_container": DB_CONTAINER,
            "db_name": DB_NAME,
        },
        "summary": {
            "kline_table_visible": bool(columns),
            "source_granularity_column_exists": "source_granularity" in set(columns or []),
            "source_group_count": len(source_rows or []),
            "proposal_action_count": proposal.get("action_count"),
            "estimated_backfill_row_count": proposal.get("estimated_backfill_row_count"),
            "unmapped_missing_granularity_group_count": len(issues),
        },
        "database": {
            "klines_columns": columns or [],
            "source_granularity_groups": source_rows or [],
        },
        "proposal": proposal,
        "issues": issues,
        "warnings": warnings[:100],
        "recommendations": build_recommendations(status, proposal, issues),
        "hermes_use": [
            "Use this report to verify whether minute K-lines can disclose full-OHLCV versus snapshot fidelity.",
            "A source_granularity backfill is provenance only; it must not change prices, volumes, signals, positions, or strategy.",
            "Tencent minute rows should be labelled minute_snapshot_price, which keeps them advisory and prevents full path-evidence claims.",
        ],
    }


def unsafe_sql_actions(actions):
    unsafe = []
    for action in actions or []:
        sql = str(action.get("sql") or "").upper()
        if any(token in sql for token in ("DROP ", "DELETE ", "TRUNCATE ", "INSERT INTO", "CREATE TABLE")):
            unsafe.append({"id": action.get("id"), "reason": "unsafe_sql_token"})
        if action.get("action") == "backfill_source_granularity":
            if "UPDATE KLINES" not in sql or "SOURCE_GRANULARITY" not in sql:
                unsafe.append({"id": action.get("id"), "reason": "backfill_sql_scope_invalid"})
        elif action.get("action") == "add_column":
            if "ALTER TABLE KLINES ADD COLUMN IF NOT EXISTS SOURCE_GRANULARITY" not in sql:
                unsafe.append({"id": action.get("id"), "reason": "add_column_sql_scope_invalid"})
        else:
            unsafe.append({"id": action.get("id"), "reason": "unknown_action_type"})
    return unsafe


def backup_metadata(payload, backup_dir=BACKUP_DIR):
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, f"kline_source_granularity_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    save_json_atomic(path, {"generated_at": now_iso(), "pre_apply_report": payload})
    return path


def execute_sql_script(sql_script):
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
        input_text=sql_script,
        timeout=240,
    )


def apply_payload(payload, confirm_proposal_hash=""):
    proposal = payload.get("proposal") if isinstance(payload.get("proposal"), dict) else {}
    actions = proposal.get("actions") if isinstance(proposal.get("actions"), list) else []
    reasons = []
    expected_hash = proposal.get("proposal_hash")
    if not actions:
        reasons.append("no_proposal_actions_to_apply")
    if not confirm_proposal_hash:
        reasons.append("confirm_proposal_hash_required")
    elif expected_hash != confirm_proposal_hash:
        reasons.append("confirm_proposal_hash_mismatch")
    unsafe = unsafe_sql_actions(actions)
    if unsafe:
        reasons.append("unsafe_sql_actions")
    if reasons:
        return {
            "schema": "kline_source_granularity_apply_result_v1",
            "status": "blocked",
            "expected_proposal_hash": expected_hash,
            "confirm_proposal_hash": confirm_proposal_hash,
            "validation_reasons": reasons,
            "unsafe_actions": unsafe,
            "applied": False,
        }
    backup_file = backup_metadata(payload)
    result = execute_sql_script(proposal.get("sql_script") or build_sql_script(actions))
    applied = result.returncode == 0
    return {
        "schema": "kline_source_granularity_apply_result_v1",
        "status": "applied" if applied else "failed",
        "expected_proposal_hash": expected_hash,
        "confirm_proposal_hash": confirm_proposal_hash,
        "validation_reasons": [] if applied else ["sql_execution_failed"],
        "backup_file": backup_file,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
        "applied": applied,
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    proposal = payload.get("proposal") or {}
    lines = [
        f"K-line source granularity report {payload.get('generated_at')} status={payload.get('status')}",
        (
            f"column_exists={summary.get('source_granularity_column_exists')} "
            f"actions={summary.get('proposal_action_count')} "
            f"backfill_rows={summary.get('estimated_backfill_row_count')} "
            f"unmapped={summary.get('unmapped_missing_granularity_group_count')}"
        ),
    ]
    if proposal.get("proposal_hash"):
        lines.append(f"proposal_hash={proposal.get('proposal_hash')}")
    for action in proposal.get("actions") or []:
        detail = action.get("estimated_row_count")
        lines.append(f"  {action.get('id')}: {action.get('action')} rows={detail}")
    for issue in payload.get("issues") or []:
        lines.append(f"  issue {issue.get('interval')} {issue.get('data_source')}: rows={issue.get('row_count')}")
    if payload.get("apply_result"):
        result = payload["apply_result"]
        lines.append(
            f"apply_result status={result.get('status')} applied={result.get('applied')} "
            f"reasons={','.join(result.get('validation_reasons') or [])}"
        )
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    parser.add_argument("--apply", action="store_true", help="apply the current proposal after hash confirmation")
    parser.add_argument("--confirm-proposal-hash", default="", help="required with --apply")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    payload = build_report()
    if args.apply:
        payload["source"]["read_only"] = False
        payload["source"]["writes_database"] = True
        payload["source"]["changes_schema"] = any(
            action.get("changes_schema") for action in (payload.get("proposal") or {}).get("actions") or []
        )
        payload["apply_result"] = apply_payload(payload, confirm_proposal_hash=args.confirm_proposal_hash)
        if payload["apply_result"].get("status") == "applied":
            payload["status"] = "APPLIED"
        else:
            payload["status"] = "BLOCKED" if payload["apply_result"].get("status") == "blocked" else "FAIL"
    if args.output:
        save_json_atomic(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.text or not args.output:
        print(build_text_report(payload))
    if args.apply and payload.get("apply_result", {}).get("status") != "applied":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
