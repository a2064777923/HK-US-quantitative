#!/usr/bin/env python3
"""Read-only strategy review policy derived from v5 outcomes and alert quality."""
import argparse
import json
import os
import sys
from datetime import datetime


OUTCOME_REPORT_FILE = os.environ.get("RT_SIGNAL_OUTCOME_REPORT_FILE", "/tmp/rt_signal_outcome_report.json")
ALERT_QUALITY_REPORT_FILE = os.environ.get("ALERT_QUALITY_REPORT_FILE", "/tmp/rt_alert_quality_report.json")
REPORT_FILE = os.environ.get("STRATEGY_REVIEW_REPORT_FILE", "/tmp/strategy_review_report.json")
DEFAULT_HORIZON = os.environ.get("STRATEGY_REVIEW_HORIZON", "1d")
MIN_TRIGGER_SAMPLE = int(os.environ.get("STRATEGY_REVIEW_MIN_TRIGGER_SAMPLE", "10"))
MIN_OVERALL_SAMPLE = int(os.environ.get("STRATEGY_REVIEW_MIN_OVERALL_SAMPLE", "30"))
MIN_WIN_RATE_PCT = float(os.environ.get("STRATEGY_REVIEW_MIN_WIN_RATE_PCT", "45"))
MIN_AVG_RETURN_PCT = float(os.environ.get("STRATEGY_REVIEW_MIN_AVG_RETURN_PCT", "0"))
MIN_VALIDATION_PASS_RATE_PCT = float(os.environ.get("STRATEGY_REVIEW_MIN_VALIDATION_PASS_RATE_PCT", "50"))
MIN_ELIGIBLE_RATE_PCT = float(os.environ.get("STRATEGY_REVIEW_MIN_ELIGIBLE_RATE_PCT", "25"))


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


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def split_trigger_key(key):
    parts = str(key or "").split(":", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return str(key or "UNKNOWN"), "UNKNOWN"


def quality_by_trigger(quality_report):
    result = {}
    for row in (quality_report or {}).get("trigger_quality") or []:
        key = f"{str(row.get('signal_type', 'UNKNOWN')).upper()}:{row.get('trigger') or 'UNKNOWN'}"
        result[key] = row
    return result


def outcome_by_trigger(outcome_report):
    return {
        row.get("key"): row
        for row in (outcome_report or {}).get("by_trigger") or []
        if row.get("key")
    }


def reasoned_policy(outcome_row, quality_row=None, horizon=DEFAULT_HORIZON):
    quality_row = quality_row or {}
    metric = ((outcome_row or {}).get("horizons") or {}).get(horizon) or {}
    resolved = as_int(metric.get("resolved_count"))
    avg_return = as_float(metric.get("avg_signed_close_return_pct"))
    win_rate = as_float(metric.get("win_rate_pct"))
    target_rate = as_float(metric.get("target_hit_rate_pct"), 0.0)
    stop_rate = as_float(metric.get("stop_hit_rate_pct"), 0.0)
    validation_rate = as_float(quality_row.get("validation_pass_rate_pct"))
    eligible_rate = None
    if as_int(quality_row.get("packet_review_count")) > 0:
        eligible_rate = quality_row.get("packet_eligible_count", 0) / quality_row.get("packet_review_count", 1) * 100
    marked_move = as_float(quality_row.get("avg_signed_move_pct"))
    reasons = []
    policy_rank = 0

    if resolved < MIN_TRIGGER_SAMPLE:
        reasons.append(f"trigger_outcome_sample_below_{MIN_TRIGGER_SAMPLE}")
        policy_rank = max(policy_rank, 1)
    else:
        if avg_return is None:
            reasons.append("trigger_avg_return_missing")
            policy_rank = max(policy_rank, 2)
        elif avg_return <= MIN_AVG_RETURN_PCT:
            reasons.append("trigger_avg_return_not_positive")
            policy_rank = max(policy_rank, 3)
        if win_rate is None:
            reasons.append("trigger_win_rate_missing")
            policy_rank = max(policy_rank, 2)
        elif win_rate < MIN_WIN_RATE_PCT:
            reasons.append(f"trigger_win_rate_below_{MIN_WIN_RATE_PCT:g}")
            policy_rank = max(policy_rank, 3)
        if stop_rate > target_rate and stop_rate >= 30:
            reasons.append("stop_hit_rate_exceeds_target_hit_rate")
            policy_rank = max(policy_rank, 3)

    if validation_rate is not None and validation_rate < MIN_VALIDATION_PASS_RATE_PCT:
        reasons.append(f"validation_pass_rate_below_{MIN_VALIDATION_PASS_RATE_PCT:g}")
        policy_rank = max(policy_rank, 2)
    if eligible_rate is not None and eligible_rate < MIN_ELIGIBLE_RATE_PCT:
        reasons.append(f"packet_eligible_rate_below_{MIN_ELIGIBLE_RATE_PCT:g}")
        policy_rank = max(policy_rank, 2)
    if marked_move is not None and marked_move < 0:
        reasons.append("negative_intraday_queue_mark")
        policy_rank = max(policy_rank, 2)

    if policy_rank >= 3:
        policy = "disable_execution_review"
    elif policy_rank == 2:
        policy = "tighten_thresholds"
    elif policy_rank == 1:
        policy = "shadow_only"
    else:
        policy = "candidate_allow_after_other_gates"

    return policy, reasons, {
        "resolved_count": resolved,
        "avg_signed_close_return_pct": avg_return,
        "win_rate_pct": win_rate,
        "target_hit_rate_pct": target_rate,
        "stop_hit_rate_pct": stop_rate,
        "validation_pass_rate_pct": validation_rate,
        "packet_eligible_rate_pct": round(eligible_rate, 2) if eligible_rate is not None else None,
        "avg_signed_queue_mark_pct": marked_move,
    }


def build_trigger_policies(outcome_report, quality_report, horizon=DEFAULT_HORIZON):
    outcomes = outcome_by_trigger(outcome_report)
    qualities = quality_by_trigger(quality_report)
    keys = sorted(set(outcomes) | set(qualities))
    rows = []
    for key in keys:
        side, trigger = split_trigger_key(key)
        outcome_row = outcomes.get(key) or {"key": key, "count": 0, "horizons": {}}
        quality_row = qualities.get(key) or {}
        policy, reasons, metrics = reasoned_policy(outcome_row, quality_row, horizon=horizon)
        rows.append(
            {
                "key": key,
                "signal_type": side,
                "trigger": trigger,
                "policy": policy,
                "execution_allowed_by_report": policy == "candidate_allow_after_other_gates",
                "review_required": policy != "candidate_allow_after_other_gates",
                "reasons": reasons,
                "sample": {
                    "outcome_count": as_int(outcome_row.get("count")),
                    "quality_count": as_int(quality_row.get("count")),
                    "confirmed_rate_pct": as_float(quality_row.get("confirmed_rate_pct")),
                },
                "metrics": metrics,
            }
        )
    rank = {
        "disable_execution_review": 0,
        "tighten_thresholds": 1,
        "shadow_only": 2,
        "candidate_allow_after_other_gates": 3,
    }
    return sorted(rows, key=lambda row: (rank.get(row["policy"], 9), -row["sample"]["outcome_count"], row["key"]))


def overall_policy(outcome_report, quality_report, trigger_policies, horizon=DEFAULT_HORIZON):
    overall_metric = (((outcome_report or {}).get("overall") or {}).get("horizons") or {}).get(horizon) or {}
    resolved = as_int(overall_metric.get("resolved_count"))
    reasons = []
    if resolved < MIN_OVERALL_SAMPLE:
        reasons.append(f"overall_outcome_sample_below_{MIN_OVERALL_SAMPLE}")
    avg_return = as_float(overall_metric.get("avg_signed_close_return_pct"))
    if resolved >= MIN_OVERALL_SAMPLE and (avg_return is None or avg_return <= MIN_AVG_RETURN_PCT):
        reasons.append("overall_avg_return_not_positive")
    win_rate = as_float(overall_metric.get("win_rate_pct"))
    if resolved >= MIN_OVERALL_SAMPLE and (win_rate is None or win_rate < MIN_WIN_RATE_PCT):
        reasons.append(f"overall_win_rate_below_{MIN_WIN_RATE_PCT:g}")
    if any(row["policy"] == "disable_execution_review" for row in trigger_policies):
        reasons.append("one_or_more_triggers_need_disable_review")
    if (quality_report or {}).get("symbol_conflicts"):
        reasons.append("symbol_conflicts_present_in_alert_queue")

    if reasons:
        policy = "keep_shadow_or_dry_run"
    else:
        policy = "candidate_for_limited_paper_execution_review"
    return {
        "policy": policy,
        "execution_allowed_by_report": False,
        "horizon": horizon,
        "reasons": reasons,
        "metrics": {
            "resolved_count": resolved,
            "avg_signed_close_return_pct": avg_return,
            "win_rate_pct": win_rate,
            "quality_validation_pass_rate_pct": ((quality_report or {}).get("directional_quality") or {}).get(
                "validation_pass_rate_pct"
            ),
            "packet_eligible_rate_pct": ((quality_report or {}).get("packet_review") or {}).get("eligible_rate_pct"),
        },
    }


def build_recommendations(payload):
    recs = []
    overall = payload["overall_policy"]
    if overall["policy"] == "keep_shadow_or_dry_run":
        recs.append("keep_alert_sim_disabled_until_strategy_review_passes")
    disabled = [row["key"] for row in payload["trigger_policies"] if row["policy"] == "disable_execution_review"]
    tightened = [row["key"] for row in payload["trigger_policies"] if row["policy"] == "tighten_thresholds"]
    shadow = [row["key"] for row in payload["trigger_policies"] if row["policy"] == "shadow_only"]
    for key in disabled[:8]:
        recs.append(f"disable_or_rework_trigger:{key}")
    for key in tightened[:8]:
        recs.append(f"tighten_trigger_thresholds:{key}")
    if shadow and not disabled and not tightened:
        recs.append("collect_more_forward_outcomes_before_execution_review")
    if not recs:
        recs.append("strategy_review_clean_continue_limited_shadow_observation")
    return recs


def build_report(outcome_report=None, quality_report=None, horizon=DEFAULT_HORIZON):
    outcome_report = outcome_report if outcome_report is not None else load_json_file(OUTCOME_REPORT_FILE)
    quality_report = quality_report if quality_report is not None else load_json_file(ALERT_QUALITY_REPORT_FILE)
    warnings = []
    if (outcome_report or {}).get("schema") != "rt_signal_outcome_report_v1":
        warnings.append("outcome_report_missing_or_invalid")
    if not isinstance(quality_report, dict) or "trigger_quality" not in quality_report:
        warnings.append("alert_quality_report_missing_or_invalid")

    trigger_policies = build_trigger_policies(outcome_report or {}, quality_report or {}, horizon=horizon)
    payload = {
        "schema": "strategy_review_report_v1",
        "generated_at": now_iso(),
        "source": {
            "read_only": True,
            "auto_applies_strategy_changes": False,
            "outcome_report_file": OUTCOME_REPORT_FILE,
            "alert_quality_report_file": ALERT_QUALITY_REPORT_FILE,
            "horizon": horizon,
            "min_trigger_sample": MIN_TRIGGER_SAMPLE,
            "min_overall_sample": MIN_OVERALL_SAMPLE,
            "min_win_rate_pct": MIN_WIN_RATE_PCT,
            "min_avg_return_pct": MIN_AVG_RETURN_PCT,
        },
        "overall_policy": overall_policy(outcome_report or {}, quality_report or {}, trigger_policies, horizon=horizon),
        "trigger_policies": trigger_policies,
        "warnings": warnings,
    }
    payload["recommendations"] = build_recommendations(payload)
    return payload


def build_text_report(payload):
    overall = payload["overall_policy"]
    lines = [
        f"Strategy review report {payload['generated_at']}",
        f"overall={overall['policy']} reasons={overall['reasons']}",
    ]
    for row in payload["trigger_policies"][:12]:
        lines.append(
            f"  {row['key']}: policy={row['policy']} resolved={row['metrics']['resolved_count']} "
            f"avg={row['metrics']['avg_signed_close_return_pct']} win={row['metrics']['win_rate_pct']} "
            f"reasons={row['reasons']}"
        )
    lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--outcome-report-file", default=OUTCOME_REPORT_FILE)
    parser.add_argument("--alert-quality-file", default=ALERT_QUALITY_REPORT_FILE)
    parser.add_argument("--horizon", default=DEFAULT_HORIZON)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    global OUTCOME_REPORT_FILE, ALERT_QUALITY_REPORT_FILE
    OUTCOME_REPORT_FILE = args.outcome_report_file
    ALERT_QUALITY_REPORT_FILE = args.alert_quality_file
    payload = build_report(horizon=args.horizon)
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
