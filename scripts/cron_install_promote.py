#!/usr/bin/env python3
"""Hash-confirmed installer for cron_audit_report read-only cron lines."""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

try:
    import cron_audit_report as audit
except ImportError:
    from scripts import cron_audit_report as audit


CRON_AUDIT_REPORT_FILE = os.environ.get("CRON_AUDIT_REPORT_FILE", "/tmp/cron_audit_report.json")
REPORT_FILE = os.environ.get("CRON_INSTALL_PROMOTION_REPORT_FILE", "/tmp/cron_install_promotion_report.json")
BACKUP_DIR = os.environ.get("CRON_INSTALL_BACKUP_DIR", "/tmp/crontab_backups")


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


def load_crontab_text():
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return "", [f"crontab_read_failed:{exc}"]
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return "", [f"crontab_read_failed:{detail or result.returncode}"]
    return result.stdout, []


def install_crontab(text):
    result = subprocess.run(["crontab", "-"], input=text, capture_output=True, text=True, timeout=10)
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "status": "installed" if result.returncode == 0 else "install_failed",
    }


def backup_crontab(text, backup_dir=BACKUP_DIR):
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, f"crontab.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def active_line_set(text):
    return set(audit.active_cron_lines(text))


def proposed_install_lines(plan):
    rows = plan.get("install_lines") if isinstance(plan.get("install_lines"), list) else []
    lines = []
    for row in rows:
        if isinstance(row, dict) and row.get("recommended_cron"):
            lines.append(str(row["recommended_cron"]).strip())
    return [line for line in lines if line]


def validate_plan(payload):
    reasons = []
    if payload.get("schema") != "cron_audit_report_v1":
        reasons.append("cron_audit_schema_invalid")
    if payload.get("status") == "FAIL":
        reasons.append("cron_audit_has_dangerous_enabled_jobs")
    plan = payload.get("installation_plan") if isinstance(payload.get("installation_plan"), dict) else {}
    if plan.get("schema") != "read_only_cron_installation_plan_v1":
        reasons.append("installation_plan_schema_invalid")
    if plan.get("auto_applied") is not False:
        reasons.append("installation_plan_must_be_manual")
    contract = plan.get("operator_contract") if isinstance(plan.get("operator_contract"), dict) else {}
    required_false = {
        "submits_orders": False,
        "uses_execute_mode": False,
        "uses_apply_flags": False,
        "enables_alert_sim": False,
        "enables_legacy_sim": False,
    }
    for key, expected in required_false.items():
        if contract.get(key) is not expected:
            reasons.append(f"unsafe_operator_contract:{key}")
    if contract.get("does_not_edit_crontab") is not True:
        reasons.append("operator_contract_must_mark_plan_read_only")
    if plan.get("rejected_line_count"):
        reasons.append("installation_plan_has_rejected_lines")
    lines = proposed_install_lines(plan)
    rejected_lines = plan.get("rejected_lines") if isinstance(plan.get("rejected_lines"), list) else []
    plan_hash_input = {
        "install_lines": plan.get("install_lines") if isinstance(plan.get("install_lines"), list) else [],
        "rejected_lines": rejected_lines,
    }
    actual_plan_hash = audit.stable_hash(plan_hash_input)
    expected_plan_hash = plan.get("proposal_hash")
    if not expected_plan_hash:
        reasons.append("installation_plan_hash_missing")
    elif expected_plan_hash != actual_plan_hash:
        reasons.append("installation_plan_hash_mismatch")
    if plan.get("status") == "not_required" and not lines:
        reasons.append("installation_plan_not_required")
    if not lines:
        reasons.append("install_lines_missing")
    unsafe = []
    for line in lines:
        unsafe_tokens = audit.unsafe_install_line(line)
        if unsafe_tokens:
            unsafe.append({"line": line, "unsafe_tokens": unsafe_tokens})
    for row in unsafe:
        reasons.append("unsafe_install_line:" + ",".join(row["unsafe_tokens"]))
    return plan, lines, unsafe, reasons


def merged_crontab(current_text, install_lines):
    existing = active_line_set(current_text)
    additions = [line for line in install_lines if line not in existing]
    base = current_text.rstrip()
    section = []
    if additions:
        section.append("# Hermes v5 read-only evidence jobs installed from cron_audit_report")
        section.extend(additions)
    if not section:
        return current_text, additions
    if base:
        return base + "\n\n" + "\n".join(section) + "\n", additions
    return "\n".join(section) + "\n", additions


def build_report(
    cron_audit_file=CRON_AUDIT_REPORT_FILE,
    apply=False,
    confirm_proposal_hash="",
    current_crontab_text=None,
):
    payload = load_json_file(cron_audit_file)
    plan, install_lines, unsafe_lines, validation_reasons = validate_plan(payload)
    expected_hash = plan.get("proposal_hash")
    reasons = list(validation_reasons)
    if apply and not confirm_proposal_hash:
        reasons.append("confirm_proposal_hash_required")
    if apply and confirm_proposal_hash and confirm_proposal_hash != expected_hash:
        reasons.append("confirm_proposal_hash_mismatch")
    if current_crontab_text is None:
        current_crontab_text, load_warnings = load_crontab_text()
        reasons.extend(load_warnings)
    merged_text, additions = merged_crontab(current_crontab_text, install_lines)
    if apply and not additions:
        reasons.append("no_new_cron_lines_to_install")

    status = "dry_run"
    backup_file = None
    install_result = None
    applied = False
    if apply:
        if reasons:
            status = "blocked"
        else:
            backup_file = backup_crontab(current_crontab_text)
            install_result = install_crontab(merged_text)
            applied = install_result["status"] == "installed"
            status = "applied" if applied else "install_failed"
            if not applied:
                reasons.append("crontab_install_failed")
    elif reasons:
        status = "blocked"

    return {
        "schema": "read_only_cron_install_promotion_report_v1",
        "generated_at": now_iso(),
        "mode": "apply" if apply else "dry-run",
        "status": status,
        "cron_audit_file": cron_audit_file,
        "proposal_hash": expected_hash,
        "confirm_proposal_hash": confirm_proposal_hash,
        "install_line_count": len(install_lines),
        "new_install_line_count": len(additions),
        "install_lines": install_lines,
        "new_install_lines": additions,
        "unsafe_lines": unsafe_lines,
        "validation_reasons": reasons,
        "applied": applied,
        "backup_file": backup_file,
        "install_result": install_result,
        "safety": {
            "dry_run_by_default": True,
            "requires_confirm_proposal_hash": True,
            "backs_up_crontab_before_apply": True,
            "rejects_execute_mode": True,
            "rejects_apply_flags": True,
            "rejects_alert_sim": True,
            "does_not_submit_orders": True,
        },
    }


def build_text_report(payload):
    lines = [
        f"Read-only cron install promotion {payload['generated_at']}",
        (
            f"mode={payload['mode']} status={payload['status']} proposal={payload.get('proposal_hash')} "
            f"lines={payload.get('install_line_count')} new={payload.get('new_install_line_count')}"
        ),
    ]
    if payload.get("validation_reasons"):
        lines.append("Reasons: " + ", ".join(payload["validation_reasons"]))
    for line in payload.get("new_install_lines", [])[:30]:
        lines.append(f"  {line}")
    if payload.get("backup_file"):
        lines.append(f"Backup: {payload['backup_file']}")
    if payload.get("install_result"):
        lines.append(f"Install: {payload['install_result']}")
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cron-audit-file", default=CRON_AUDIT_REPORT_FILE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-proposal-hash", default="")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(
        cron_audit_file=args.cron_audit_file,
        apply=args.apply,
        confirm_proposal_hash=args.confirm_proposal_hash,
    )
    if args.output:
        save_json_atomic(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.text:
        print(build_text_report(payload))
    else:
        print(build_text_report(payload))
        print("\n--- JSON ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] in ("dry_run", "applied") else 2


if __name__ == "__main__":
    sys.exit(main())
