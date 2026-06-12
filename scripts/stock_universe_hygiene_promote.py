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
DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
SAFE_AUTO_ACTIONS = {
    "candidate_remove_from_stock_universe",
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
        for item in summary.get("high_priority_candidates") or []:
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
):
    symbols = symbols or []
    report = load_json_file(report_file)
    validation_reasons = validate_report(report)
    candidates = proposal_candidates(report)
    digest = proposal_hash(candidates)
    allow_actions = allow_action or list(SAFE_AUTO_ACTIONS)
    selected, rejected = selected_candidates(candidates, symbols, allow_actions)
    protected_positions = []
    protection_warnings = []
    if apply and selected:
        selected_symbols = [item.get("symbol") for item in selected]
        protected_positions, protection_warnings = fetch_open_position_symbols(selected_symbols)
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
        "protected_positions": protected_positions,
        "protection_warnings": protection_warnings,
        "validation_reasons": reasons,
        "applied": applied,
        "apply_result": apply_result,
        "safety": {
            "dry_run_by_default": True,
            "requires_confirm_proposal_hash": True,
            "requires_explicit_symbol_selection": True,
            "blocks_open_position_symbols": True,
            "backs_up_stocks_before_apply": True,
            "allowed_actions": sorted(allow_actions),
            "does_not_submit_orders": True,
            "does_not_restart_services": True,
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
    if payload.get("rejected_symbols"):
        lines.append("Rejected: " + json.dumps(payload["rejected_symbols"], ensure_ascii=False))
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
