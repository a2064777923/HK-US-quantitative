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
EXECUTION_READINESS_REPORT_FILE = os.environ.get(
    "EXECUTION_READINESS_REPORT_FILE",
    "/tmp/execution_readiness_report.json",
)
SOURCE_RELIABILITY_REPORT_FILE = os.environ.get(
    "SOURCE_RELIABILITY_REPORT_FILE",
    "/tmp/source_reliability_report.json",
)
SIMULATION_PERFORMANCE_REPORT_FILE = os.environ.get(
    "SIMULATION_PERFORMANCE_REPORT_FILE",
    "/tmp/simulation_performance_report.json",
)
STRATEGY_LEARNING_REPORT_FILE = os.environ.get(
    "STRATEGY_LEARNING_REPORT_FILE",
    "/tmp/strategy_learning_report.json",
)


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


def as_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def proposal_change_counts(proposal):
    markets = proposal.get("markets") if isinstance(proposal.get("markets"), dict) else {}
    add_count = 0
    remove_count = 0
    for changes in markets.values():
        if not isinstance(changes, dict):
            continue
        add_count += len(changes.get("add_symbols") or [])
        remove_count += len(changes.get("remove_symbols") or [])
    return {"add_count": add_count, "remove_count": remove_count, "change_count": add_count + remove_count}


def compact_execution_readiness_context(payload, proposal_hash):
    payload = payload if isinstance(payload, dict) else {}
    blocking = payload.get("blocking_gates") if isinstance(payload.get("blocking_gates"), list) else []
    warnings = payload.get("warning_gates") if isinstance(payload.get("warning_gates"), list) else []
    matching_watchlist_warning = False
    non_watchlist_warnings = []
    for row in warnings:
        if not isinstance(row, dict):
            continue
        if row.get("gate") == "watchlist_proposal":
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            if data.get("proposal_hash") == proposal_hash:
                matching_watchlist_warning = True
            else:
                non_watchlist_warnings.append(row.get("gate"))
        else:
            non_watchlist_warnings.append(row.get("gate"))
    return {
        "schema": "rt_signal_watchlist_promotion_readiness_context_v1",
        "present": bool(payload),
        "source_report_file": EXECUTION_READINESS_REPORT_FILE,
        "status": payload.get("status") or "MISSING",
        "ready_for_execute": payload.get("ready_for_execute"),
        "blocking_gate_names": [row.get("gate") for row in blocking if isinstance(row, dict)],
        "warning_gate_names": [row.get("gate") for row in warnings if isinstance(row, dict)],
        "matching_watchlist_warning": matching_watchlist_warning,
        "non_watchlist_warning_gate_names": non_watchlist_warnings,
    }


def compact_source_reliability_context(payload):
    payload = payload if isinstance(payload, dict) else {}
    return {
        "schema": "rt_signal_watchlist_promotion_source_reliability_context_v1",
        "present": bool(payload),
        "source_report_file": SOURCE_RELIABILITY_REPORT_FILE,
        "status": payload.get("status") or "MISSING",
        "summary": payload.get("summary") if isinstance(payload.get("summary"), dict) else {},
        "recommendations": payload.get("recommendations") if isinstance(payload.get("recommendations"), list) else [],
    }


def compact_simulation_performance_context(payload):
    payload = payload if isinstance(payload, dict) else {}
    return {
        "schema": "rt_signal_watchlist_promotion_simulation_context_v1",
        "present": bool(payload),
        "source_report_file": SIMULATION_PERFORMANCE_REPORT_FILE,
        "status": payload.get("status") or "MISSING",
        "summary": payload.get("summary") if isinstance(payload.get("summary"), dict) else {},
        "reason_codes": payload.get("reason_codes") if isinstance(payload.get("reason_codes"), list) else [],
        "remediation_plan": payload.get("remediation_plan") if isinstance(payload.get("remediation_plan"), dict) else {},
    }


def compact_strategy_learning_context(payload, proposal_hash):
    payload = payload if isinstance(payload, dict) else {}
    audit_effect = (
        payload.get("audit_pass_judgment_effect")
        if isinstance(payload.get("audit_pass_judgment_effect"), dict)
        else {}
    )
    approved = audit_effect.get("approved_or_reduced") if isinstance(audit_effect.get("approved_or_reduced"), dict) else {}
    rejected = audit_effect.get("rejected_or_held") if isinstance(audit_effect.get("rejected_or_held"), dict) else {}
    coverage = payload.get("judgment_audit_coverage") if isinstance(payload.get("judgment_audit_coverage"), dict) else {}
    sizing = (
        payload.get("sizing_blocker_remediation")
        if isinstance(payload.get("sizing_blocker_remediation"), dict)
        else {}
    )
    return {
        "schema": "rt_signal_watchlist_promotion_learning_context_v1",
        "present": bool(payload),
        "source_report_file": STRATEGY_LEARNING_REPORT_FILE,
        "has_audit_pass_judgment_effect": bool(audit_effect),
        "audit_pass_judgment_effect": {
            "sample_filter": audit_effect.get("sample_filter"),
            "approved_resolved_count": as_int(approved.get("resolved_count")),
            "rejected_resolved_count": as_int(rejected.get("resolved_count")),
        },
        "judgment_audit_coverage": {
            "audit_report_available": coverage.get("audit_report_available"),
            "audit_report_truncated": coverage.get("audit_report_truncated"),
            "audit_fail_count": as_int(coverage.get("audit_fail_count")),
            "audit_missing_count": as_int(coverage.get("audit_missing_count")),
        },
        "sizing_blocker_remediation": {
            "sizing_blocker_count": as_int(sizing.get("sizing_blocker_count")),
            "covered_by_watchlist_removal_count": as_int(sizing.get("covered_by_watchlist_removal_count")),
            "uncovered_count": as_int(sizing.get("uncovered_count")),
            "watchlist_proposal_hash": sizing.get("watchlist_proposal_hash"),
            "matches_current_proposal": not sizing.get("watchlist_proposal_hash")
            or sizing.get("watchlist_proposal_hash") == proposal_hash,
        },
    }


def promotion_blockers(proposal, execution_readiness, source_reliability, simulation_performance, strategy_learning):
    proposal_hash = proposal.get("proposal_hash")
    changes = proposal_change_counts(proposal)
    readiness = compact_execution_readiness_context(execution_readiness, proposal_hash)
    source = compact_source_reliability_context(source_reliability)
    simulation = compact_simulation_performance_context(simulation_performance)
    learning = compact_strategy_learning_context(strategy_learning, proposal_hash)
    blockers = []

    if readiness["status"] == "READY" and readiness.get("ready_for_execute") is True:
        pass
    elif (
        readiness["status"] == "WARN"
        and not readiness["blocking_gate_names"]
        and readiness["matching_watchlist_warning"]
        and not readiness["non_watchlist_warning_gate_names"]
    ):
        pass
    else:
        blockers.append(
            {
                "code": "execution_readiness_not_clean_blocks_watchlist_promotion",
                "detail": "watchlist promotion is allowed only when readiness is READY or the only warning is the same watchlist proposal",
                "readiness_status": readiness["status"],
                "blocking_gate_names": readiness["blocking_gate_names"],
                "warning_gate_names": readiness["warning_gate_names"],
            }
        )

    if source["status"] not in ("OK", "PASS"):
        blockers.append(
            {
                "code": "source_reliability_not_ok_blocks_watchlist_promotion",
                "detail": "watchlist changes depend on clean source quality and universe evidence",
                "source_reliability_status": source["status"],
                "recommendations": source["recommendations"],
            }
        )

    if simulation["status"] not in ("OK", "PASS"):
        blockers.append(
            {
                "code": "simulation_performance_not_ok_blocks_watchlist_promotion",
                "detail": "watchlist changes must not be promoted while simulation performance is not clean",
                "simulation_status": simulation["status"],
                "reason_codes": simulation["reason_codes"],
            }
        )

    if not learning["present"]:
        blockers.append(
            {
                "code": "strategy_learning_missing_blocks_watchlist_promotion",
                "detail": "watchlist promotion requires current strategy learning context",
            }
        )
    if not learning["has_audit_pass_judgment_effect"]:
        blockers.append(
            {
                "code": "strategy_learning_audit_pass_effect_missing_blocks_watchlist_promotion",
                "detail": "raw Hermes judgment effect is diagnostic only and cannot support watchlist promotion",
            }
        )
    coverage = learning["judgment_audit_coverage"]
    if (
        coverage.get("audit_report_available") is not True
        or coverage.get("audit_report_truncated")
        or coverage.get("audit_fail_count")
        or coverage.get("audit_missing_count")
    ):
        blockers.append(
            {
                "code": "strategy_learning_judgment_audit_gaps_block_watchlist_promotion",
                "detail": "strategy learning judgment audit coverage must be complete before watchlist promotion",
                "judgment_audit_coverage": coverage,
            }
        )
    effect = learning["audit_pass_judgment_effect"]
    if effect.get("approved_resolved_count", 0) < 5 or effect.get("rejected_resolved_count", 0) < 5:
        blockers.append(
            {
                "code": "strategy_learning_audit_pass_sample_too_small_blocks_watchlist_promotion",
                "detail": "audit-pass Hermes learning sample is too small for universe/watchlist changes",
                "approved_resolved_count": effect.get("approved_resolved_count"),
                "rejected_resolved_count": effect.get("rejected_resolved_count"),
            }
        )
    sizing = learning["sizing_blocker_remediation"]
    if sizing.get("sizing_blocker_count") and not sizing.get("matches_current_proposal"):
        blockers.append(
            {
                "code": "sizing_blocker_remediation_hash_mismatch_blocks_watchlist_promotion",
                "detail": "strategy learning references a different watchlist proposal hash",
                "learning_watchlist_proposal_hash": sizing.get("watchlist_proposal_hash"),
                "proposal_hash": proposal_hash,
            }
        )

    return blockers, {
        "changes": changes,
        "execution_readiness": readiness,
        "source_reliability": source,
        "simulation_performance": simulation,
        "strategy_learning": learning,
    }


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
    execution_readiness_file=EXECUTION_READINESS_REPORT_FILE,
    source_reliability_file=SOURCE_RELIABILITY_REPORT_FILE,
    simulation_performance_file=SIMULATION_PERFORMANCE_REPORT_FILE,
    strategy_learning_file=STRATEGY_LEARNING_REPORT_FILE,
    apply=False,
    confirm_proposal_hash="",
):
    source_report = load_json_file(report_file)
    current = load_json_file(target_watchlist_file)
    execution_readiness = load_json_file(execution_readiness_file)
    source_reliability = load_json_file(source_reliability_file)
    simulation_performance = load_json_file(simulation_performance_file)
    strategy_learning = load_json_file(strategy_learning_file)
    proposal, validation_reasons = validate_report(source_report)
    blockers, promotion_context = promotion_blockers(
        proposal,
        execution_readiness,
        source_reliability,
        simulation_performance,
        strategy_learning,
    )
    expected_hash = proposal.get("proposal_hash")
    current_hash = diff.stable_hash(live_symbols(current))
    report_live_hash = (source_report.get("source") or {}).get("live_watchlist_hash")
    reasons = list(validation_reasons)
    reasons.extend(f"proposal_promotion_blocker:{blocker.get('code', 'unknown')}" for blocker in blockers)
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
        "promotion_blockers": blockers,
        "promotion_context": promotion_context,
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
    if payload.get("promotion_blockers"):
        lines.append(
            "Promotion blockers: "
            + ", ".join(blocker.get("code", "unknown") for blocker in payload["promotion_blockers"])
        )
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
    parser.add_argument("--execution-readiness-file", default=EXECUTION_READINESS_REPORT_FILE)
    parser.add_argument("--source-reliability-file", default=SOURCE_RELIABILITY_REPORT_FILE)
    parser.add_argument("--simulation-performance-file", default=SIMULATION_PERFORMANCE_REPORT_FILE)
    parser.add_argument("--strategy-learning-file", default=STRATEGY_LEARNING_REPORT_FILE)
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
        execution_readiness_file=args.execution_readiness_file,
        source_reliability_file=args.source_reliability_file,
        simulation_performance_file=args.simulation_performance_file,
        strategy_learning_file=args.strategy_learning_file,
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
