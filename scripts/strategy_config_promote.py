#!/usr/bin/env python3
"""Hash-confirmed promotion tool for realtime v5 strategy config proposals."""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

try:
    import rt_signal_engine_v5 as rt
except ImportError:
    from scripts import rt_signal_engine_v5 as rt


PROPOSAL_FILE = os.environ.get("RT_SIGNAL_STRATEGY_CONFIG_PROPOSAL_FILE", "/tmp/rt_signal_strategy_config_proposal.json")
TARGET_CONFIG_FILE = os.environ.get("RT_SIGNAL_STRATEGY_CONFIG_FILE", "/root/rt_signal_strategy_config.json")
BACKUP_DIR = os.environ.get("RT_SIGNAL_STRATEGY_CONFIG_BACKUP_DIR", "/tmp/rt_signal_strategy_config_backups")
SERVICE_NAME = os.environ.get("RT_SIGNAL_ENGINE_SERVICE", "rt_signal_engine_v5.service")


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


def proposal_hash_for_config(config):
    normalized, _warnings = rt.normalize_strategy_config(config)
    return normalized.get("config_id")


def validate_proposal(proposal):
    reasons = []
    if proposal.get("schema") != "rt_signal_strategy_config_proposal_v1":
        reasons.append("proposal_schema_invalid")
    if (proposal.get("source") or {}).get("auto_applied") is not False:
        reasons.append("proposal_source_must_be_manual")
    if (proposal.get("source") or {}).get("manual_review_required") is not True:
        reasons.append("proposal_manual_review_required_missing")
    proposed_config = proposal.get("proposed_config")
    if not isinstance(proposed_config, dict):
        reasons.append("proposed_config_missing")
        return None, reasons
    normalized, warnings = rt.normalize_strategy_config(proposed_config)
    reasons.extend(f"proposed_config_warning:{warning}" for warning in warnings)
    expected_hash = proposal.get("proposal_hash")
    actual_hash = proposal_hash_for_config(normalized)
    if not expected_hash:
        reasons.append("proposal_hash_missing")
    elif actual_hash != expected_hash:
        reasons.append("proposal_hash_mismatch")
    return normalized, reasons


def diff_summary(current_config, proposed_config):
    current_config, _ = rt.normalize_strategy_config(current_config or {})
    proposed_config, _ = rt.normalize_strategy_config(proposed_config or {})
    fields = [
        "signal_cooldown_seconds",
        "volume_anomaly_ratio",
        "confirmation_thresholds",
        "risk_model",
        "trigger_overrides",
    ]
    changes = []
    for field in fields:
        if current_config.get(field) != proposed_config.get(field):
            changes.append(
                {
                    "field": field,
                    "current": current_config.get(field),
                    "proposed": proposed_config.get(field),
                }
            )
    return changes


def backup_target(path, backup_dir=BACKUP_DIR):
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.basename(path.rstrip(os.sep)) or "rt_signal_strategy_config.json"
    backup_path = os.path.join(backup_dir, f"{base}.{stamp}.bak")
    if os.path.exists(path):
        shutil.copy2(path, backup_path)
    else:
        save_json_atomic(backup_path, {"missing_original": path, "backed_up_at": now_iso()})
    return backup_path


def restart_service(service_name=SERVICE_NAME):
    result = subprocess.run(["systemctl", "restart", service_name], capture_output=True, text=True, timeout=30)
    return {
        "service": service_name,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "status": "restarted" if result.returncode == 0 else "restart_failed",
    }


def build_report(
    proposal_file=PROPOSAL_FILE,
    target_config_file=TARGET_CONFIG_FILE,
    apply=False,
    confirm_proposal_hash="",
    restart=False,
):
    proposal = load_json_file(proposal_file)
    current_config = load_json_file(target_config_file, rt.default_strategy_config())
    proposed_config, validation_reasons = validate_proposal(proposal)
    expected_hash = proposal.get("proposal_hash")
    reasons = list(validation_reasons)
    if apply and not confirm_proposal_hash:
        reasons.append("confirm_proposal_hash_required")
    if apply and confirm_proposal_hash and confirm_proposal_hash != expected_hash:
        reasons.append("confirm_proposal_hash_mismatch")
    current_id = proposal_hash_for_config(current_config)
    changes = diff_summary(current_config, proposed_config or {})
    status = "dry_run"
    backup_file = None
    restart_result = None
    applied = False
    if apply:
        if reasons:
            status = "blocked"
        else:
            backup_file = backup_target(target_config_file)
            save_json_atomic(target_config_file, proposed_config)
            applied = True
            status = "applied"
            if restart:
                restart_result = restart_service()
                if restart_result["status"] != "restarted":
                    status = "applied_restart_failed"
    elif reasons:
        status = "invalid_proposal"

    return {
        "schema": "rt_signal_strategy_config_promotion_report_v1",
        "generated_at": now_iso(),
        "mode": "apply" if apply else "dry-run",
        "status": status,
        "proposal_file": proposal_file,
        "target_config_file": target_config_file,
        "current_config_id": current_id,
        "proposal_hash": expected_hash,
        "confirm_proposal_hash": confirm_proposal_hash,
        "change_count": len(changes),
        "changes": changes,
        "validation_reasons": reasons,
        "applied": applied,
        "backup_file": backup_file,
        "restart_requested": restart,
        "restart_result": restart_result,
        "safety": {
            "dry_run_by_default": True,
            "requires_confirm_proposal_hash": True,
            "backs_up_target_before_apply": True,
            "restart_requires_explicit_flag": True,
        },
    }


def build_text_report(payload):
    lines = [
        f"Strategy config promotion {payload['generated_at']}",
        (
            f"mode={payload['mode']} status={payload['status']} current={payload['current_config_id']} "
            f"proposal={payload['proposal_hash']} changes={payload['change_count']}"
        ),
    ]
    if payload.get("validation_reasons"):
        lines.append("Reasons: " + ", ".join(payload["validation_reasons"]))
    for change in payload.get("changes", [])[:12]:
        lines.append(f"  {change['field']}: current={change['current']} proposed={change['proposed']}")
    if payload.get("backup_file"):
        lines.append(f"Backup: {payload['backup_file']}")
    if payload.get("restart_result"):
        lines.append(f"Restart: {payload['restart_result']}")
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proposal-file", default=PROPOSAL_FILE)
    parser.add_argument("--target-config-file", default=TARGET_CONFIG_FILE)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-proposal-hash", default="")
    parser.add_argument("--restart-service", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(
        proposal_file=args.proposal_file,
        target_config_file=args.target_config_file,
        apply=args.apply,
        confirm_proposal_hash=args.confirm_proposal_hash,
        restart=args.restart_service,
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
    return 0 if payload["status"] in ("dry_run", "applied") else 2


if __name__ == "__main__":
    sys.exit(main())
