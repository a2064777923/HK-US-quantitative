#!/usr/bin/env python3
"""Hash-confirmed promotion tool for active stock-universe hygiene candidates."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime


REPORT_FILE = os.environ.get("UNIVERSE_HYGIENE_REPORT_FILE", "/tmp/universe_hygiene_report.json")
BACKUP_DIR = os.environ.get("STOCK_UNIVERSE_HYGIENE_BACKUP_DIR", "/tmp/stock_universe_hygiene_backups")
WATCHLIST_FILE = os.environ.get("RT_SIGNAL_WATCHLIST_FILE", "/root/rt_signal_watchlist.json")
DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
SAFE_AUTO_ACTIONS = {
    "candidate_remove_from_stock_universe",
}
MANUAL_REVIEW_ACTIONS = {
    "candidate_deactivate_or_symbol_mapping",
    "candidate_refetch_or_deactivate",
}
_COLUMN_CACHE = {}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def load_json_file(path, default=None):
    default = {} if default is None else default
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else default
    except Exception:
        return default


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def run_cmd(args, input_text=None, timeout=90):
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


def rows(stdout):
    return [line.rstrip("\n").split("\t") for line in stdout.splitlines() if line.strip()]


def sql_quote(value):
    return str(value).replace("'", "''")


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


def proposal_candidates(report):
    out = []
    for market, summary in sorted((report.get("markets") or {}).items()):
        source_items = summary.get("all_problem_symbols") or summary.get("high_priority_candidates") or []
        for item in source_items:
            if item.get("recommended_action") not in SAFE_AUTO_ACTIONS | MANUAL_REVIEW_ACTIONS:
                continue
            out.append(
                {
                    "market": market,
                    "symbol": item.get("symbol"),
                    "exchange": item.get("exchange"),
                    "name": item.get("name"),
                    "recommended_action": item.get("recommended_action"),
                    "issues": item.get("issues") or [],
                    "lag_days_vs_market_latest": item.get("lag_days_vs_market_latest"),
                    "latest_date": item.get("latest_date"),
                    "history_rows_120d": item.get("history_rows_120d"),
                }
            )
    return sorted(out, key=lambda item: (item.get("market") or "", item.get("symbol") or ""))


def proposal_hash(candidates):
    stable = json.dumps(candidates, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def validate_report(report):
    reasons = []
    if report.get("schema") != "universe_hygiene_report_v1":
        reasons.append("report_schema_invalid")
    source = report.get("source") or {}
    if source.get("auto_applies_stock_changes") is not False:
        reasons.append("report_source_must_be_read_only")
    proposal = report.get("proposal") or {}
    if proposal.get("schema") != "stock_universe_hygiene_proposal_v1":
        reasons.append("proposal_schema_invalid")
    proposal_source = proposal.get("source") or {}
    if proposal_source.get("manual_review_required") is not True:
        reasons.append("proposal_manual_review_required_missing")
    if proposal_source.get("auto_applied") is not False:
        reasons.append("proposal_must_not_be_auto_applied")
    return reasons


def selected_candidates(candidates, symbols, allow_actions):
    requested = {str(symbol).upper() for symbol in symbols}
    allow_actions = set(allow_actions or SAFE_AUTO_ACTIONS)
    selected = []
    rejected = []
    by_symbol = {str(item.get("symbol") or "").upper(): item for item in candidates}
    for symbol in sorted(requested):
        item = by_symbol.get(symbol)
        if not item:
            rejected.append({"symbol": symbol, "reason": "symbol_not_in_high_priority_candidates"})
            continue
        if item.get("recommended_action") not in allow_actions:
            rejected.append(
                {
                    "symbol": symbol,
                    "reason": "recommended_action_not_allowed",
                    "recommended_action": item.get("recommended_action"),
                }
            )
            continue
        selected.append(item)
    return selected, rejected


def collect_symbols_from_payload(payload):
    symbols = set()

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "symbol" and isinstance(child, str):
                    symbols.add(child.strip().upper())
                elif key == "symbols" and isinstance(child, list):
                    for item in child:
                        if isinstance(item, str):
                            symbols.add(item.strip().upper())
                walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return {symbol for symbol in symbols if symbol}


def fetch_watchlist_symbols(path=WATCHLIST_FILE):
    if not path:
        return set(), []
    payload = load_json_file(path, {})
    if not payload:
        return set(), []
    return collect_symbols_from_payload(payload), []


def selected_watchlist_protections(selected, watchlist_symbols):
    watchlist_symbols = {str(symbol).upper() for symbol in watchlist_symbols or []}
    protected = []
    for item in selected or []:
        symbol = str(item.get("symbol") or "").upper()
        if symbol and symbol in watchlist_symbols:
            protected.append(
                {
                    "symbol": item.get("symbol"),
                    "market": item.get("market"),
                    "exchange": item.get("exchange"),
                    "recommended_action": item.get("recommended_action"),
                }
            )
    return protected


def manual_review_required_candidates(candidates, symbols, allow_actions):
    requested = {str(symbol).upper() for symbol in symbols or []}
    allow_actions = set(allow_actions or SAFE_AUTO_ACTIONS)
    rows = []
    for item in candidates:
        symbol = str(item.get("symbol") or "").upper()
        action = item.get("recommended_action")
        if requested and symbol not in requested:
            continue
        if action not in MANUAL_REVIEW_ACTIONS or action in allow_actions:
            continue
        rows.append(
            {
                "symbol": item.get("symbol"),
                "market": item.get("market"),
                "exchange": item.get("exchange"),
                "name": item.get("name"),
                "recommended_action": action,
                "issues": item.get("issues") or [],
                "latest_date": item.get("latest_date"),
                "lag_days_vs_market_latest": item.get("lag_days_vs_market_latest"),
                "history_rows_120d": item.get("history_rows_120d"),
                "required_operator_decision": "refetch_or_fix_symbol_mapping_before_deactivate",
                "why_not_auto_allowed": "ordinary_stale_symbol_may_be_mapping_or_provider_issue",
            }
        )
    return rows


def operator_review_plan(review_rows, proposal_digest):
    if not review_rows:
        return {
            "schema": "stock_universe_hygiene_operator_review_plan_v1",
            "status": "not_required",
            "review_required_count": 0,
            "items": [],
            "commands": [],
        }
    symbols = [row["symbol"] for row in review_rows if row.get("symbol")]
    commands = []
    for symbol in symbols[:20]:
        commands.append(
            {
                "symbol": symbol,
                "dry_run_command": (
                    "/usr/bin/python3 /root/stock_universe_hygiene_promote.py "
                    "--report-file /tmp/universe_hygiene_report.json "
                    f"--symbol {symbol} --allow-action candidate_deactivate_or_symbol_mapping --text"
                ),
                "hash_confirmed_apply_template": (
                    "/usr/bin/python3 /root/stock_universe_hygiene_promote.py "
                    "--report-file /tmp/universe_hygiene_report.json "
                    f"--symbol {symbol} --allow-action candidate_deactivate_or_symbol_mapping "
                    f"--apply --confirm-proposal-hash {proposal_digest} --text"
                ),
            }
        )
    return {
        "schema": "stock_universe_hygiene_operator_review_plan_v1",
        "status": "operator_review_required",
        "review_required_count": len(review_rows),
        "items": review_rows,
        "commands": commands,
        "pre_apply_checklist": [
            "confirm_symbol_has_no_active_or_holding_position",
            "confirm_symbol_not_required_by_user_portfolio_or_current_v5_watchlist",
            "try_refetch_or_provider_symbol_mapping_before_deactivation",
            "if_deactivation_is_chosen_pass_explicit_allow_action_and_matching_proposal_hash",
            "rerun_universe_hygiene_data_health_outcome_readiness_and_hermes_packet_after_apply",
        ],
        "safety": {
            "manual_review_required": True,
            "does_not_auto_apply": True,
            "does_not_submit_orders": True,
            "does_not_change_watchlists": True,
            "apply_requires_explicit_allow_action": True,
            "apply_requires_confirm_proposal_hash": True,
        },
    }


def fetch_open_position_symbols(symbols):
    symbols = [str(symbol).upper() for symbol in symbols if str(symbol or "").strip()]
    if not symbols:
        return [], []
    if not table_columns("positions"):
        return [], ["positions_table_missing_for_open_position_protection"]
    qty_expr = first_existing("positions", ("quantity", "volume", "qty"), "0")
    status_expr = first_existing("positions", ("status",), "'holding'")
    quoted = ", ".join(f"'{sql_quote(symbol)}'" for symbol in symbols)
    r = psql(
        f"""
        SELECT upper(symbol), portfolio_id, {status_expr}, {qty_expr}
        FROM positions
        WHERE upper(symbol) IN ({quoted})
          AND COALESCE(({qty_expr})::numeric, 0) > 0
          AND {status_expr} IN ('active','holding')
        ORDER BY upper(symbol), portfolio_id
        """
    )
    if r.returncode != 0:
        return [], [f"open_position_check_failed:{r.stderr.strip()}"]
    protected = []
    for row in rows(r.stdout):
        if len(row) >= 4:
            protected.append(
                {
                    "symbol": row[0],
                    "portfolio_id": row[1],
                    "status": row[2],
                    "quantity": row[3],
                }
            )
    return protected, []


def backup_current_rows(symbols, backup_dir=BACKUP_DIR):
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(backup_dir, f"stocks_{stamp}.json")
    if not symbols:
        save_json_atomic(path, {"generated_at": now_iso(), "rows": []})
        return path
    quoted = ", ".join(f"'{sql_quote(symbol)}'" for symbol in symbols)
    query = f"""
        SELECT COALESCE(jsonb_agg(row_to_json(s)), '[]'::jsonb)::text
        FROM stocks s
        WHERE upper(s.symbol) IN ({quoted})
    """
    r = psql(query)
    if r.returncode != 0:
        raise RuntimeError(f"backup query failed: {r.stderr.strip()}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(r.stdout.strip() or "[]")
        f.write("\n")
    return path


def sql_for_deactivate(item):
    symbol = sql_quote(item["symbol"])
    return (
        "UPDATE stocks SET "
        "is_active = false, "
        "updated_at = NOW() "
        f"WHERE upper(symbol) = upper('{symbol}') AND is_active = true;"
    )


def apply_deactivations(candidates, backup_dir=BACKUP_DIR):
    if not candidates:
        return {"status": "noop", "reason": "no_selected_candidates"}
    symbols = [item["symbol"] for item in candidates]
    backup_file = backup_current_rows(symbols, backup_dir=backup_dir)
    statements = ["BEGIN;"]
    for item in candidates:
        statements.append(sql_for_deactivate(item))
    statements.append("COMMIT;")
    result = psql_script("\n".join(statements) + "\n")
    return {
        "status": "applied" if result.returncode == 0 else "failed",
        "backup_file": backup_file,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }


def build_report(
    report_file=REPORT_FILE,
    symbols=None,
    apply=False,
    confirm_proposal_hash="",
    allow_action=None,
    backup_dir=BACKUP_DIR,
    watchlist_file=WATCHLIST_FILE,
):
    symbols = symbols or []
    report = load_json_file(report_file)
    validation_reasons = validate_report(report)
    candidates = proposal_candidates(report)
    digest = proposal_hash(candidates)
    allow_actions = allow_action or list(SAFE_AUTO_ACTIONS)
    selected, rejected = selected_candidates(candidates, symbols, allow_actions)
    review_rows = manual_review_required_candidates(candidates, symbols, allow_actions)
    review_plan = operator_review_plan(review_rows, digest)
    protected_positions = []
    protection_warnings = []
    protected_watchlist_symbols = []
    watchlist_symbols = set()
    watchlist_warnings = []
    if apply and selected:
        selected_symbols = [item.get("symbol") for item in selected]
        protected_positions, protection_warnings = fetch_open_position_symbols(selected_symbols)
        watchlist_symbols, watchlist_warnings = fetch_watchlist_symbols(watchlist_file)
        protected_watchlist_symbols = selected_watchlist_protections(selected, watchlist_symbols)
    reasons = list(validation_reasons)
    if apply and not confirm_proposal_hash:
        reasons.append("confirm_proposal_hash_required")
    if apply and confirm_proposal_hash and confirm_proposal_hash != digest:
        reasons.append("confirm_proposal_hash_mismatch")
    if apply and not symbols:
        reasons.append("symbol_selection_required")
    if rejected:
        reasons.append("one_or_more_symbols_rejected")
    if protection_warnings:
        reasons.append("open_position_protection_unavailable")
    if protected_positions:
        reasons.append("selected_symbol_has_open_position")
    if protected_watchlist_symbols:
        reasons.append("selected_symbol_in_watchlist")

    status = "dry_run"
    apply_result = None
    applied = False
    if apply:
        if reasons:
            status = "blocked"
        else:
            apply_result = apply_deactivations(selected, backup_dir=backup_dir)
            applied = apply_result.get("status") == "applied"
            status = "applied" if applied else apply_result.get("status", "failed")
    elif reasons:
        status = "invalid_selection" if not validation_reasons else "invalid_report"

    return {
        "schema": "stock_universe_hygiene_promotion_report_v1",
        "generated_at": now_iso(),
        "mode": "apply" if apply else "dry-run",
        "status": status,
        "report_file": report_file,
        "proposal_hash": digest,
        "confirm_proposal_hash": confirm_proposal_hash,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "selected_candidates": selected,
        "rejected_symbols": rejected,
        "operator_review_required_candidates": review_rows,
        "operator_review_plan": review_plan,
        "protected_positions": protected_positions,
        "protected_watchlist_symbols": protected_watchlist_symbols,
        "protection_warnings": protection_warnings,
        "watchlist_warnings": watchlist_warnings,
        "validation_reasons": reasons,
        "applied": applied,
        "apply_result": apply_result,
        "safety": {
            "dry_run_by_default": True,
            "requires_confirm_proposal_hash": True,
            "requires_explicit_symbol_selection": True,
            "blocks_open_position_symbols": True,
            "blocks_watchlist_symbols": True,
            "backs_up_stocks_before_apply": True,
            "allowed_actions": sorted(allow_actions),
            "default_allowed_actions": sorted(SAFE_AUTO_ACTIONS),
            "manual_review_actions": sorted(MANUAL_REVIEW_ACTIONS),
            "does_not_submit_orders": True,
            "does_not_restart_services": True,
            "does_not_change_watchlists": True,
        },
    }


def build_plan_from_report_payload(report, symbols=None, allow_action=None):
    """Build a read-only promotion plan from an already-loaded hygiene report."""
    symbols = symbols or []
    report = report if isinstance(report, dict) else {}
    validation_reasons = validate_report(report)
    candidates = proposal_candidates(report)
    digest = proposal_hash(candidates)
    allow_actions = allow_action or list(SAFE_AUTO_ACTIONS)
    selected, rejected = selected_candidates(candidates, symbols, allow_actions)
    review_rows = manual_review_required_candidates(candidates, symbols, allow_actions)
    reasons = list(validation_reasons)
    if rejected:
        reasons.append("one_or_more_symbols_rejected")
    status = "dry_run"
    if reasons:
        status = "invalid_selection" if not validation_reasons else "invalid_report"
    return {
        "schema": "stock_universe_hygiene_promotion_report_v1",
        "generated_at": now_iso(),
        "mode": "dry-run",
        "status": status,
        "report_file": None,
        "proposal_hash": digest,
        "confirm_proposal_hash": "",
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "selected_candidates": selected,
        "rejected_symbols": rejected,
        "operator_review_required_candidates": review_rows,
        "operator_review_plan": operator_review_plan(review_rows, digest),
        "protected_positions": [],
        "protected_watchlist_symbols": [],
        "protection_warnings": [],
        "watchlist_warnings": [],
        "validation_reasons": reasons,
        "applied": False,
        "apply_result": None,
        "safety": {
            "dry_run_by_default": True,
            "read_only_payload_build": True,
            "queries_database": False,
            "requires_confirm_proposal_hash": True,
            "requires_explicit_symbol_selection": True,
            "blocks_open_position_symbols_on_apply": True,
            "blocks_watchlist_symbols_on_apply": True,
            "backs_up_stocks_before_apply": True,
            "allowed_actions": sorted(allow_actions),
            "default_allowed_actions": sorted(SAFE_AUTO_ACTIONS),
            "manual_review_actions": sorted(MANUAL_REVIEW_ACTIONS),
            "does_not_submit_orders": True,
            "does_not_restart_services": True,
            "does_not_change_watchlists": True,
            "does_not_change_stock_universe": True,
        },
    }


def build_text_report(payload):
    lines = [
        f"Stock universe hygiene promotion {payload['generated_at']}",
        (
            f"mode={payload['mode']} status={payload['status']} hash={payload['proposal_hash']} "
            f"candidates={payload['candidate_count']} selected={payload['selected_count']}"
        ),
    ]
    if payload.get("validation_reasons"):
        lines.append("Reasons: " + ", ".join(payload["validation_reasons"]))
    for item in payload.get("selected_candidates", [])[:30]:
        lines.append(
            f"  deactivate {item.get('symbol')} {item.get('exchange')} "
            f"action={item.get('recommended_action')} issues={item.get('issues')}"
        )
    review_plan = payload.get("operator_review_plan") or {}
    if review_plan.get("review_required_count"):
        lines.append(
            "Operator review required: "
            + ", ".join(
                f"{item.get('symbol')}:{item.get('recommended_action')}"
                for item in (review_plan.get("items") or [])[:30]
            )
        )
        lines.append("Checklist: " + ", ".join(review_plan.get("pre_apply_checklist") or []))
    if payload.get("rejected_symbols"):
        lines.append("Rejected: " + json.dumps(payload["rejected_symbols"], ensure_ascii=False))
    if payload.get("protected_watchlist_symbols"):
        lines.append("Watchlist protected: " + json.dumps(payload["protected_watchlist_symbols"], ensure_ascii=False))
    if payload.get("apply_result"):
        lines.append("apply_result=" + json.dumps(payload["apply_result"], ensure_ascii=False))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-file", default=REPORT_FILE)
    parser.add_argument("--symbol", action="append", default=[], help="explicit symbol to deactivate if eligible")
    parser.add_argument("--allow-action", action="append", default=[], help="extra recommended_action allowed for apply")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-proposal-hash", default="")
    parser.add_argument("--backup-dir", default=BACKUP_DIR)
    parser.add_argument("--watchlist-file", default=WATCHLIST_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    allow_actions = sorted(SAFE_AUTO_ACTIONS | set(args.allow_action or []))
    payload = build_report(
        report_file=args.report_file,
        symbols=args.symbol,
        apply=args.apply,
        confirm_proposal_hash=args.confirm_proposal_hash,
        allow_action=allow_actions,
        backup_dir=args.backup_dir,
        watchlist_file=args.watchlist_file,
    )
    text = build_text_report(payload)
    if args.text:
        print(text)
    elif args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text)
        print("\n--- JSON ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] in ("dry_run", "applied", "noop") else 2


if __name__ == "__main__":
    sys.exit(main())
