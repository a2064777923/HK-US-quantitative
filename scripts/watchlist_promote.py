#!/usr/bin/env python3
"""Hash-confirmed promotion helper for realtime v5 watchlist proposals."""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime

try:
    import watchlist_diff_report as diff
    import rt_signal_engine_v5 as rt
except ImportError:
    from scripts import watchlist_diff_report as diff
    from scripts import rt_signal_engine_v5 as rt


REPORT_FILE = os.environ.get("WATCHLIST_DIFF_REPORT_FILE", "/tmp/watchlist_diff_report.json")
TARGET_WATCHLIST_FILE = os.environ.get("RT_SIGNAL_WATCHLIST_FILE", "/root/rt_signal_watchlist.json")
BACKUP_DIR = os.environ.get("RT_SIGNAL_WATCHLIST_BACKUP_DIR", "/tmp/rt_signal_watchlist_backups")


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


def live_symbols(payload):
    return {
        market: diff.symbols_for(payload, market)
        for market in sorted((payload.get("markets") or {}).keys())
    }


def engine_watchlist_id(payload):
    return rt.watchlist_digest(
        {
            "HK": diff.symbols_for(payload, "HK"),
            "US": diff.symbols_for(payload, "US"),
        }
    )


def validate_report(report):
    reasons = []
    if report.get("schema") != "watchlist_diff_report_v1":
        reasons.append("report_schema_invalid")
    source = report.get("source") or {}
    if source.get("read_only") is not True:
        reasons.append("report_source_not_read_only")
    if source.get("auto_applies_watchlist") is not False:
        reasons.append("report_source_must_not_auto_apply")
    proposal = report.get("proposal") or {}
    if proposal.get("schema") != "rt_signal_watchlist_change_proposal_v1":
        reasons.append("proposal_schema_invalid")
    proposal_source = proposal.get("source") or {}
    if proposal_source.get("manual_review_required") is not True:
        reasons.append("proposal_manual_review_required_missing")
    if proposal_source.get("auto_applied") is not False:
        reasons.append("proposal_must_not_be_auto_applied")
    if proposal_source.get("does_not_submit_orders") is not True:
        reasons.append("proposal_safety_does_not_submit_orders_missing")
    expected_hash = proposal.get("proposal_hash")
    actual_hash = diff.proposal_hash_for_payload(proposal)
    if not expected_hash:
        reasons.append("proposal_hash_missing")
    elif actual_hash != expected_hash:
        reasons.append("proposal_hash_mismatch")
    return proposal, reasons


def proposed_watchlist(current, proposal):
    current_markets = {market: diff.symbols_for(current, market) for market in ("HK", "US")}
    proposal_markets = proposal.get("markets") or {}
    out = {}
    for market in ("HK", "US"):
        current_symbols = current_markets.get(market) or []
        changes = proposal_markets.get(market) or {}
        remove = {str(symbol).upper() for symbol in changes.get("remove_symbols") or []}
        add = [str(symbol).upper() for symbol in changes.get("add_symbols") or []]
        merged = [symbol for symbol in current_symbols if symbol not in remove]
        for symbol in add:
            if symbol and symbol not in merged:
                merged.append(symbol)
        out[market] = merged
    return {
        "schema": "rt_signal_watchlist_v1",
        "generated_at": now_iso(),
        "source": {
            "promoted_from": "watchlist_diff_report_v1",
            "proposal_hash": proposal.get("proposal_hash"),
            "manual_review_required": True,
            "auto_applied": False,
            "restart_required_for_live_engine": True,
        },
        "markets": {
            market: {"symbols": out.get(market) or []}
            for market in ("HK", "US")
        },
    }


def diff_summary(current, proposed):
    rows = []
    for market in ("HK", "US"):
        cur = set(diff.symbols_for(current, market))
        prop = set(diff.symbols_for(proposed, market))
        rows.append(
            {
                "market": market,
                "current_count": len(cur),
                "proposed_count": len(prop),
                "add_symbols": sorted(prop - cur),
                "remove_symbols": sorted(cur - prop),
            }
        )
    return rows


def backup_target(path, backup_dir=BACKUP_DIR):
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.basename(path.rstrip(os.sep)) or "rt_signal_watchlist.json"
    backup_path = os.path.join(backup_dir, f"{base}.{stamp}.bak")
    if os.path.exists(path):
        shutil.copy2(path, backup_path)
    else:
        save_json_atomic(backup_path, {"missing_original": path, "backed_up_at": now_iso()})
    return backup_path


def build_report(
    report_file=REPORT_FILE,
    target_watchlist_file=TARGET_WATCHLIST_FILE,
    apply=False,
    confirm_proposal_hash="",
):
    source_report = load_json_file(report_file)
    current = load_json_file(target_watchlist_file)
    proposal, validation_reasons = validate_report(source_report)
    expected_hash = proposal.get("proposal_hash")
    current_hash = diff.stable_hash(live_symbols(current))
    report_live_hash = (source_report.get("source") or {}).get("live_watchlist_hash")
    reasons = list(validation_reasons)
    if report_live_hash and current_hash != report_live_hash:
        reasons.append("target_watchlist_hash_changed_since_report")
    if apply and not confirm_proposal_hash:
        reasons.append("confirm_proposal_hash_required")
    if apply and confirm_proposal_hash and confirm_proposal_hash != expected_hash:
        reasons.append("confirm_proposal_hash_mismatch")

    proposed = proposed_watchlist(current, proposal)
    current_watchlist_id = engine_watchlist_id(current)
    proposed_watchlist_id = engine_watchlist_id(proposed)
    changes = diff_summary(current, proposed)
    change_count = sum(len(row["add_symbols"]) + len(row["remove_symbols"]) for row in changes)
    status = "dry_run"
    applied = False
    backup_file = None
    if apply:
        if reasons:
            status = "blocked"
        else:
            backup_file = backup_target(target_watchlist_file)
            save_json_atomic(target_watchlist_file, proposed)
            applied = True
            status = "applied_restart_required"
    elif reasons:
        status = "invalid_proposal"

    return {
        "schema": "rt_signal_watchlist_promotion_report_v1",
        "generated_at": now_iso(),
        "mode": "apply" if apply else "dry-run",
        "status": status,
        "report_file": report_file,
        "target_watchlist_file": target_watchlist_file,
        "proposal_hash": expected_hash,
        "confirm_proposal_hash": confirm_proposal_hash,
        "current_watchlist_hash": current_hash,
        "report_live_watchlist_hash": report_live_hash,
        "current_watchlist_id": current_watchlist_id,
        "proposed_watchlist_id": proposed_watchlist_id,
        "change_count": change_count,
        "changes": changes,
        "validation_reasons": reasons,
        "applied": applied,
        "backup_file": backup_file,
        "restart_required": applied,
        "proposed_watchlist": proposed,
        "safety": {
            "dry_run_by_default": True,
            "requires_confirm_proposal_hash": True,
            "requires_unchanged_target_hash": True,
            "backs_up_target_before_apply": True,
            "does_not_restart_services": True,
            "does_not_submit_orders": True,
        },
    }


def build_text_report(payload):
    lines = [
        f"Watchlist promotion {payload['generated_at']}",
        (
            f"mode={payload['mode']} status={payload['status']} proposal={payload['proposal_hash']} "
            f"changes={payload['change_count']}"
        ),
    ]
    if payload.get("validation_reasons"):
        lines.append("Reasons: " + ", ".join(payload["validation_reasons"]))
    for row in payload.get("changes") or []:
        lines.append(
            f"  {row['market']}: current={row['current_count']} proposed={row['proposed_count']} "
            f"add={len(row['add_symbols'])} remove={len(row['remove_symbols'])}"
        )
    if payload.get("backup_file"):
        lines.append(f"Backup: {payload['backup_file']}")
    if payload.get("restart_required"):
        lines.append("Restart required: rt_signal_engine_v5.service must be restarted manually after review.")
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-file", default=REPORT_FILE)
    parser.add_argument("--target-watchlist-file", default=TARGET_WATCHLIST_FILE)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-proposal-hash", default="")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(
        report_file=args.report_file,
        target_watchlist_file=args.target_watchlist_file,
        apply=args.apply,
        confirm_proposal_hash=args.confirm_proposal_hash,
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
    return 0 if payload["status"] in ("dry_run", "applied_restart_required") else 2


if __name__ == "__main__":
    sys.exit(main())
