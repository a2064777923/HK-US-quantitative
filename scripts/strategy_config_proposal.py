#!/usr/bin/env python3
"""Read-only proposal generator for realtime v5 strategy config changes."""
import argparse
import json
import os
import sys
from datetime import datetime

try:
    import rt_signal_engine_v5 as rt
except ImportError:
    from scripts import rt_signal_engine_v5 as rt


STRATEGY_REVIEW_REPORT_FILE = os.environ.get("STRATEGY_REVIEW_REPORT_FILE", "/tmp/strategy_review_report.json")
CURRENT_CONFIG_FILE = os.environ.get("RT_SIGNAL_STRATEGY_CONFIG_FILE", "/root/rt_signal_strategy_config.json")
PROPOSAL_FILE = os.environ.get("RT_SIGNAL_STRATEGY_CONFIG_PROPOSAL_FILE", "/tmp/rt_signal_strategy_config_proposal.json")


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


def signal_threshold(config, signal_type):
    thresholds = config.get("confirmation_thresholds") or {}
    if signal_type == "SELL":
        return rt.as_float((thresholds.get("SELL") or {}).get("max_full_score"), -0.25)
    return rt.as_float((thresholds.get("BUY") or {}).get("min_full_score"), 0.25)


def proposed_tightened_threshold(config, row):
    signal_type = str(row.get("signal_type") or "").upper()
    key = row.get("key")
    existing = ((config.get("trigger_overrides") or {}).get(key) or {})
    if signal_type == "SELL":
        base = rt.as_float(existing.get("max_full_score"), signal_threshold(config, "SELL"))
        return {"max_full_score": round(max(base - 0.10, -0.85), 4)}
    base = rt.as_float(existing.get("min_full_score"), signal_threshold(config, "BUY"))
    return {"min_full_score": round(min(base + 0.10, 0.85), 4)}


def apply_policy_to_config(config, row):
    proposed = json.loads(json.dumps(config))
    overrides = proposed.setdefault("trigger_overrides", {})
    key = row.get("key")
    if not key:
        return proposed, None
    policy = row.get("policy")
    current_override = dict(overrides.get(key) or {})
    change = {
        "key": key,
        "policy": policy,
        "from": dict(current_override),
        "reasons": row.get("reasons") or [],
    }
    if policy == "disable_execution_review":
        current_override["enabled"] = False
        current_override["review_mode"] = "disabled_pending_rework"
    elif policy == "tighten_thresholds":
        current_override["enabled"] = current_override.get("enabled", True)
        current_override.update(proposed_tightened_threshold(proposed, row))
        current_override["review_mode"] = "tightened_pending_retest"
    elif policy == "shadow_only":
        current_override["enabled"] = current_override.get("enabled", True)
        current_override["review_mode"] = "shadow_only_pending_sample"
    else:
        return proposed, None
    current_override["strategy_review_reasons"] = row.get("reasons") or []
    overrides[key] = current_override
    change["to"] = dict(current_override)
    return proposed, change


def proposal_hash(proposed_config):
    normalized, _warnings = rt.normalize_strategy_config(proposed_config)
    return normalized["config_id"]


def build_report(strategy_review=None, current_config=None):
    strategy_review = strategy_review if strategy_review is not None else load_json_file(STRATEGY_REVIEW_REPORT_FILE)
    if current_config is None:
        current_config = load_json_file(CURRENT_CONFIG_FILE, rt.default_strategy_config())
    current_config, config_warnings = rt.normalize_strategy_config(current_config)
    proposed_config = json.loads(json.dumps(current_config))
    changes = []
    for row in strategy_review.get("trigger_policies") or []:
        proposed_config, change = apply_policy_to_config(proposed_config, row)
        if change:
            changes.append(change)
    proposed_config["schema"] = "rt_signal_strategy_config_v1"
    proposed_config["version"] = f"proposal-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    proposed_config["description"] = "Candidate strategy config generated from strategy_review_report.py. Manual review required."
    proposed_config, proposed_warnings = rt.normalize_strategy_config(proposed_config)
    return {
        "schema": "rt_signal_strategy_config_proposal_v1",
        "generated_at": now_iso(),
        "source": {
            "read_only": True,
            "manual_review_required": True,
            "auto_applied": False,
            "strategy_review_report_file": STRATEGY_REVIEW_REPORT_FILE,
            "current_config_file": CURRENT_CONFIG_FILE,
            "current_config_id": current_config.get("config_id"),
            "strategy_review_schema": strategy_review.get("schema"),
            "strategy_review_policy": (strategy_review.get("overall_policy") or {}).get("policy"),
        },
        "proposal_hash": proposal_hash(proposed_config),
        "change_count": len(changes),
        "changes": changes,
        "proposed_config": proposed_config,
        "promotion": {
            "copy_target": CURRENT_CONFIG_FILE,
            "requires_operator_review": True,
            "restart_required": "rt_signal_engine_v5.service",
            "do_not_auto_apply": True,
        },
        "warnings": config_warnings + proposed_warnings,
    }


def build_text_report(payload):
    lines = [
        f"Strategy config proposal {payload['generated_at']}",
        f"changes={payload['change_count']} current={payload['source']['current_config_id']} proposal={payload['proposal_hash']}",
        f"auto_applied={payload['source']['auto_applied']} manual_review_required={payload['source']['manual_review_required']}",
    ]
    for change in payload.get("changes", [])[:12]:
        lines.append(f"  {change['key']}: {change['policy']} -> {change['to']}")
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-review-file", default=STRATEGY_REVIEW_REPORT_FILE)
    parser.add_argument("--current-config-file", default=CURRENT_CONFIG_FILE)
    parser.add_argument("--output", default=PROPOSAL_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    global STRATEGY_REVIEW_REPORT_FILE, CURRENT_CONFIG_FILE
    STRATEGY_REVIEW_REPORT_FILE = args.strategy_review_file
    CURRENT_CONFIG_FILE = args.current_config_file
    payload = build_report()
    if args.output:
        save_json_atomic(args.output, payload)
    text = build_text_report(payload)
    if args.text:
        print(text)
    elif args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text)
        print("\n--- JSON ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
