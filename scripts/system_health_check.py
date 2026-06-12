#!/usr/bin/env python3
"""Read-only health checks for the QuantMind/Hermes trading stack."""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
PORTFOLIO_ID = int(os.environ.get("QM_PORTFOLIO_ID", "8"))
ALERT_FILE = os.environ.get("RT_ALERT_FILE", "/tmp/rt_signal_alert.json")
ALERT_QUEUE_FILE = os.environ.get("RT_ALERT_QUEUE_FILE", "/tmp/rt_signal_alerts.jsonl")
DATA_HEALTH_REPORT_FILE = os.environ.get("DATA_HEALTH_REPORT_FILE", "/tmp/data_health_report.json")

SEVERITY = {"OK": 0, "WARN": 1, "FAIL": 2}
_COLUMN_CACHE = {}
REQUIRED_DIRECTIONAL_ALERT_FIELDS = (
    "signal_id",
    "generated_at",
    "confirmed",
    "full_score",
    "entry_price",
    "stop_loss",
    "take_profit",
    "rr_ratio",
)


def run_cmd(args, timeout=10):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": str(exc)})()


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def psql(sql, timeout=20):
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
            "-c",
            sql,
        ],
        timeout=timeout,
    )


def rows(stdout):
    return [line.strip().split("|") for line in stdout.splitlines() if line.strip()]


def sql_quote(value):
    return str(value).replace("'", "''")


def as_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def date_prefix(value):
    if value in (None, ""):
        return ""
    return str(value).strip()[:10]


def table_columns(table):
    if table in _COLUMN_CACHE:
        return _COLUMN_CACHE[table]
    r = psql(
        f"""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = '{sql_quote(table)}'
        """
    )
    cols = {row[0] for row in rows(r.stdout)} if r.returncode == 0 else set()
    _COLUMN_CACHE[table] = cols
    return cols


def first_existing(table, candidates, fallback):
    cols = table_columns(table)
    for candidate in candidates:
        if candidate in cols:
            return candidate
    return fallback


def add(checks, name, status, detail, data=None):
    checks.append({"name": name, "status": status, "detail": detail, "data": data or {}})


def data_health_payload():
    try:
        import data_health_report
    except ImportError:
        from scripts import data_health_report

    return data_health_report.build_report()


def normalize_alert_list(loaded):
    if isinstance(loaded, list):
        return [item for item in loaded if isinstance(item, dict)]
    if isinstance(loaded, dict):
        return [loaded]
    return []


def directional_alert_contract_errors(alert):
    side = str(alert.get("signal_type", "")).upper()
    if side not in ("BUY", "SELL"):
        return []
    missing = []
    for field in REQUIRED_DIRECTIONAL_ALERT_FIELDS:
        if field not in alert or alert.get(field) in (None, ""):
            missing.append(f"missing_{field}")
    return missing


def alert_contract_check(checks, alerts):
    directional = []
    bad = []
    for alert in alerts:
        side = str(alert.get("signal_type", "")).upper()
        if side not in ("BUY", "SELL"):
            continue
        sid = alert.get("signal_id") or f"{alert.get('symbol', '?')}:{alert.get('trigger', '?')}:{alert.get('time', '')}"
        directional.append(sid)
        errors = directional_alert_contract_errors(alert)
        if errors:
            bad.append({"signal_id": sid, "errors": errors})

    if bad:
        add(
            checks,
            "alert_contract",
            "FAIL",
            f"{len(bad)}/{len(directional)} directional alerts missing v5 contract fields",
            {"bad": bad[:10], "directional_count": len(directional)},
        )
    elif directional:
        add(checks, "alert_contract", "OK", f"{len(directional)} directional alerts satisfy v5 contract")
    else:
        add(checks, "alert_contract", "WARN", "no directional BUY/SELL alerts available for contract check")


def latest_kline_check(checks):
    sql = """
        SELECT s.exchange, count(DISTINCT k.symbol), max(k.timestamp::date)
        FROM klines k
        JOIN stocks s ON s.symbol = k.symbol
        WHERE k.interval = 'day'
        AND s.is_active = true
        AND s.exchange IN ('HKEX','NASDAQ','NYSE')
        GROUP BY s.exchange
        ORDER BY s.exchange
    """
    r = psql(sql)
    if r.returncode != 0:
        add(checks, "klines", "FAIL", r.stderr.strip())
        return ""
    parsed = rows(r.stdout)
    if not parsed:
        add(checks, "klines", "FAIL", "no active HK/US daily klines found")
        return ""
    latest_dates = [p[2] for p in parsed if len(p) >= 3 and p[2]]
    latest = max(latest_dates) if latest_dates else ""
    detail = "; ".join(f"{p[0]} symbols={p[1]} latest={p[2]}" for p in parsed if len(p) >= 3)
    add(checks, "klines", "OK", detail, {"latest": latest, "markets": parsed})
    return latest


def signal_check(checks, latest_kline_date):
    sql = """
        SELECT trade_date, count(*),
               count(*) FILTER (WHERE signal_side = 'BUY'),
               count(*) FILTER (WHERE signal_side = 'HOLD'),
               count(*) FILTER (WHERE signal_side = 'SELL')
        FROM engine_signal_scores
        WHERE model_version = 'signal_v4'
        AND feature_version = 'v4_full'
        GROUP BY trade_date
        ORDER BY trade_date DESC
        LIMIT 5
    """
    r = psql(sql)
    if r.returncode != 0:
        add(checks, "signals", "FAIL", r.stderr.strip())
        return ""
    parsed = rows(r.stdout)
    if not parsed:
        add(checks, "signals", "FAIL", "no signal_v4/v4_full rows found")
        return ""
    latest = parsed[0][0]
    status = "OK"
    detail_status = "fresh"
    if latest_kline_date and latest < latest_kline_date:
        status = "FAIL"
        detail_status = f"stale versus klines latest={latest_kline_date}"
    detail = f"latest={latest} {detail_status}; recent={parsed}"
    add(checks, "signals", status, detail, {"latest": latest, "recent": parsed})
    return latest


def data_health_check(checks):
    try:
        payload = data_health_payload()
    except Exception as exc:
        add(checks, "data_health", "WARN", f"data health report unavailable: {exc}")
        return

    status = payload.get("status") if payload.get("status") in SEVERITY else "WARN"
    markets = payload.get("markets") or {}
    market_bits = []
    for market, summary in sorted(markets.items()):
        market_bits.append(
            "{market}={status} latest={latest} coverage={coverage}% invalid_ohlc={invalid}".format(
                market=market,
                status=summary.get("status"),
                latest=summary.get("latest_date"),
                coverage=((summary.get("coverage") or {}).get("latest_date_coverage_pct")),
                invalid=((summary.get("integrity") or {}).get("invalid_latest_ohlc_count")),
            )
        )
    detail_parts = market_bits or ["no market data health summary"]
    feature_run = payload.get("feature_run") or {}
    if feature_run:
        latest = feature_run.get("latest") or {}
        notes = feature_run.get("notes") or []
        detail_parts.append(
            "feature_run={status} run_id={run_id} trade_date={trade_date} notes={notes}".format(
                status=feature_run.get("status"),
                run_id=latest.get("run_id"),
                trade_date=latest.get("trade_date"),
                notes=",".join(notes) if notes else "none",
            )
        )
    detail = "; ".join(detail_parts)
    add(
        checks,
        "data_health",
        status,
        detail,
        {
            "schema": payload.get("schema"),
            "generated_at": payload.get("generated_at"),
            "recommendations": (payload.get("recommendations") or [])[:10],
            "markets": markets,
            "feature_run": feature_run,
        },
    )


def feature_run_check(checks):
    expected_expr = first_existing("engine_feature_runs", ("expected_count", "expected_symbols"), "NULL")
    ready_expr = first_existing("engine_feature_runs", ("ready_count", "ready_symbols"), "NULL")
    missing_expr = first_existing("engine_feature_runs", ("missing_count", "missing_symbols"), "NULL")
    sql = """
        SELECT run_id, trade_date, status, {expected_expr}, {ready_expr}, {missing_expr}
        FROM engine_feature_runs
        WHERE run_id LIKE 'signal_v4_%'
        ORDER BY trade_date DESC, run_id DESC
        LIMIT 3
    """.format(expected_expr=expected_expr, ready_expr=ready_expr, missing_expr=missing_expr)
    r = psql(sql)
    if r.returncode != 0:
        add(checks, "feature_runs", "WARN", r.stderr.strip())
        return
    parsed = rows(r.stdout)
    if not parsed:
        add(checks, "feature_runs", "WARN", "no signal_v4 feature run rows found")
        return
    latest = parsed[0]
    status = "OK" if len(latest) >= 6 and latest[2] == "signal_ready" and latest[4] != "0" else "WARN"
    add(checks, "feature_runs", status, f"latest={latest}; recent={parsed}", {"recent": parsed})


def positions_check(checks):
    sql = f"""
        SELECT status, count(*), COALESCE(sum(quantity), 0)
        FROM positions
        WHERE portfolio_id = {PORTFOLIO_ID}
        GROUP BY status
        ORDER BY status
    """
    r = psql(sql)
    if r.returncode != 0:
        add(checks, "positions", "FAIL", r.stderr.strip())
        return
    parsed = rows(r.stdout)
    if not parsed:
        add(checks, "positions", "WARN", f"portfolio {PORTFOLIO_ID} has no position rows")
        return
    live_count = 0
    for status, count, *_ in parsed:
        if status in ("active", "holding"):
            live_count += int(float(count))
    status = "OK" if live_count > 0 else "WARN"
    add(checks, "positions", status, f"portfolio={PORTFOLIO_ID} statuses={parsed}", {"statuses": parsed})


def portfolio_check(checks):
    sql = f"""
        SELECT id, available_cash, current_capital, updated_at
        FROM portfolios
        WHERE id = {PORTFOLIO_ID}
    """
    r = psql(sql)
    if r.returncode != 0:
        add(checks, "portfolio", "FAIL", r.stderr.strip())
        return
    parsed = rows(r.stdout)
    if not parsed:
        add(checks, "portfolio", "FAIL", f"portfolio {PORTFOLIO_ID} not found")
        return
    add(checks, "portfolio", "OK", f"portfolio={parsed[0]}", {"portfolio": parsed[0]})


def ledger_reconcile_payload():
    try:
        import sim_position_reconcile
    except ImportError:
        from scripts import sim_position_reconcile

    return sim_position_reconcile.build_report(PORTFOLIO_ID)


VALUATION_ACTIONS = {"update_portfolio_totals"}
VALUATION_FIELDS = {"current_price", "market_value", "unrealized_pnl", "unrealized_pnl_rate", "weight"}


def is_valuation_only_action(action):
    action_type = action.get("action")
    diff_fields = set(action.get("diff_fields") or [])
    return action_type in VALUATION_ACTIONS or (
        action_type == "update_open_position" and diff_fields and diff_fields <= VALUATION_FIELDS
    )


def has_fresh_live_valuation_snapshot(action):
    if action.get("action") != "update_open_position":
        return True
    current = action.get("current") or {}
    expected = action.get("expected") or {}
    if as_float(current.get("current_price")) <= 0 or as_float(current.get("market_value")) <= 0:
        return False
    updated_date = date_prefix(current.get("updated_at"))
    expected_price_date = date_prefix(expected.get("price_date"))
    if not updated_date or not expected_price_date:
        return False
    return updated_date >= expected_price_date


def valuation_drift_has_fresh_live_snapshot(actions):
    open_position_actions = [action for action in actions if action.get("action") == "update_open_position"]
    if not open_position_actions:
        return False
    return all(has_fresh_live_valuation_snapshot(action) for action in open_position_actions)


def simulation_ledger_check(checks):
    try:
        payload = ledger_reconcile_payload()
    except Exception as exc:
        add(checks, "simulation_ledger", "WARN", f"ledger reconcile check unavailable: {exc}")
        return

    summary = payload.get("summary") or {}
    action_count = int(summary.get("action_count") or 0)
    warnings = payload.get("warnings") or []
    if action_count:
        actions = payload.get("actions", []) or []
        structural_actions = []
        valuation_only_actions = []
        for action in actions:
            if is_valuation_only_action(action):
                valuation_only_actions.append(action)
            else:
                structural_actions.append(action)
        if not structural_actions:
            if valuation_drift_has_fresh_live_snapshot(valuation_only_actions):
                add(
                    checks,
                    "simulation_ledger",
                    "OK",
                    (
                        f"portfolio {PORTFOLIO_ID} positions structurally match sim_trades; "
                        f"fresh live valuation differs from daily-kline reconcile baseline; action_count={action_count}"
                    ),
                    {
                        "plan_hash": payload.get("plan_hash"),
                        "summary": summary,
                        "valuation_status": "fresh_live_snapshot",
                        "valuation_actions": valuation_only_actions[:20],
                    },
                )
                return
            add(
                checks,
                "simulation_ledger",
                "WARN",
                f"portfolio {PORTFOLIO_ID} valuation fields drift from reconcile baseline; action_count={action_count}",
                {
                    "plan_hash": payload.get("plan_hash"),
                    "summary": summary,
                    "valuation_actions": valuation_only_actions[:20],
                },
            )
            return
        add(
            checks,
            "simulation_ledger",
            "FAIL",
            f"portfolio {PORTFOLIO_ID} positions drift from sim_trades; action_count={action_count}",
            {
                "plan_hash": payload.get("plan_hash"),
                "summary": summary,
                "actions": structural_actions[:20],
                "valuation_actions": valuation_only_actions[:20],
            },
        )
        return
    if warnings:
        add(
            checks,
            "simulation_ledger",
            "WARN",
            f"portfolio {PORTFOLIO_ID} ledger check has warnings",
            {"plan_hash": payload.get("plan_hash"), "summary": summary, "warnings": warnings[:10]},
        )
        return
    add(
        checks,
        "simulation_ledger",
        "OK",
        f"portfolio {PORTFOLIO_ID} positions match sim_trades",
        {"plan_hash": payload.get("plan_hash"), "summary": summary},
    )


def alert_files_check(checks):
    parsed_lines = 0
    bad_lines = 0
    contract_alerts = []
    if os.path.exists(ALERT_QUEUE_FILE):
        with open(ALERT_QUEUE_FILE, "r", encoding="utf-8") as f:
            tail = f.readlines()[-50:]
        for line in tail:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                contract_alerts.extend(normalize_alert_list(item))
                parsed_lines += 1
            except json.JSONDecodeError:
                bad_lines += 1
        status = "OK" if bad_lines == 0 else "FAIL"
        add(checks, "alert_queue", status, f"tail_json={parsed_lines} bad_lines={bad_lines}")
    else:
        add(checks, "alert_queue", "WARN", f"{ALERT_QUEUE_FILE} does not exist")

    if os.path.exists(ALERT_FILE):
        try:
            with open(ALERT_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            latest_alerts = normalize_alert_list(loaded)
            contract_alerts.extend(latest_alerts)
            count = len(latest_alerts)
            add(checks, "alert_latest", "OK", f"{ALERT_FILE} parseable alerts={count}")
        except Exception as exc:
            add(checks, "alert_latest", "FAIL", f"{ALERT_FILE} parse failed: {exc}")
    else:
        add(checks, "alert_latest", "WARN", f"{ALERT_FILE} does not exist")

    alert_contract_check(checks, contract_alerts)


def process_check(checks):
    active = run_cmd(["systemctl", "is-active", "rt_signal_engine_v5"], timeout=5)
    if active.returncode == 0 and active.stdout.strip() == "active":
        main_pid = run_cmd(["systemctl", "show", "rt_signal_engine_v5", "-p", "MainPID", "--value"], timeout=5)
        pid = main_pid.stdout.strip()
        add(checks, "rt_signal_engine_v5", "OK", f"systemd active MainPID={pid or '?'}")
        return

    r = run_cmd(["pgrep", "-af", "/root/rt_signal_engine_v5.py"], timeout=5)
    if r.returncode == 0 and r.stdout.strip():
        add(checks, "rt_signal_engine_v5", "WARN", "process running outside systemd: " + r.stdout.strip())
    else:
        add(checks, "rt_signal_engine_v5", "WARN", "process not found")


def build_payload():
    _COLUMN_CACHE.clear()
    checks = []
    latest_kline = latest_kline_check(checks)
    signal_check(checks, latest_kline)
    data_health_check(checks)
    feature_run_check(checks)
    positions_check(checks)
    portfolio_check(checks)
    simulation_ledger_check(checks)
    alert_files_check(checks)
    process_check(checks)

    overall = max((check["status"] for check in checks), key=lambda s: SEVERITY[s])
    payload = {
        "status": overall,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "checks": checks,
    }
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--output", help="write machine-readable JSON to this file atomically")
    args = parser.parse_args()

    payload = build_payload()

    if args.output:
        save_json_atomic(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"QuantMind/Hermes health: {payload['status']} @ {payload['checked_at']}")
        for check in payload["checks"]:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0 if payload["status"] in ("OK", "WARN") else 2


if __name__ == "__main__":
    sys.exit(main())
