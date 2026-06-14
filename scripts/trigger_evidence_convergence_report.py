#!/usr/bin/env python3
"""Read-only convergence report for forward outcomes and v5 replay trigger risk."""
import argparse
import json
import os
import sys
from datetime import datetime


STRATEGY_REVIEW_REPORT_FILE = os.environ.get("STRATEGY_REVIEW_REPORT_FILE", "/tmp/strategy_review_report.json")
V5_REPLAY_STRATEGY_REVIEW_REPORT_FILE = os.environ.get(
    "V5_REPLAY_STRATEGY_REVIEW_REPORT_FILE",
    "/tmp/v5_replay_strategy_review_report.json",
)
REPORT_FILE = os.environ.get("TRIGGER_EVIDENCE_CONVERGENCE_REPORT_FILE", "/tmp/trigger_evidence_convergence_report.json")
FORWARD_RISK_POLICIES = {"disable_execution_review", "tighten_thresholds", "shadow_only"}
REPLAY_RISK_POLICIES = {"tighten_thresholds", "shadow_only"}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{datetime.now().strftime('%Y%m%d%H%M%S%f')}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def load_json_file(path, default=None):
    default = {} if default is None else default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else default
    except Exception:
        return default


def as_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def check(status, code, detail, data=None):
    return {"status": status, "code": code, "detail": detail, "data": data or {}}


def forward_key(row):
    key = row.get("key")
    if key:
        return str(key)
    return f"{str(row.get('signal_type') or 'UNKNOWN').upper()}:{row.get('trigger') or 'UNKNOWN'}"


def replay_key(row):
    key = row.get("strategy_key")
    if key:
        return str(key)
    return f"{str(row.get('signal_type') or 'UNKNOWN').upper()}:{row.get('trigger') or 'UNKNOWN'}"


def policy_rank(policy):
    return {
        "disable_execution_review": 0,
        "tighten_thresholds": 1,
        "shadow_only": 2,
        "diagnostic_only": 3,
        "candidate_allow_after_other_gates": 4,
        None: 9,
    }.get(policy, 8)


def compact_forward(row):
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    sample = row.get("sample") if isinstance(row.get("sample"), dict) else {}
    return {
        "policy": row.get("policy"),
        "reasons": row.get("reasons") or [],
        "sample": {
            "outcome_count": sample.get("outcome_count"),
            "quality_count": sample.get("quality_count"),
            "confirmed_rate_pct": sample.get("confirmed_rate_pct"),
        },
        "metrics": {
            "resolved_count": metrics.get("resolved_count"),
            "avg_signed_close_return_pct": metrics.get("avg_signed_close_return_pct"),
            "win_rate_pct": metrics.get("win_rate_pct"),
            "target_hit_rate_pct": metrics.get("target_hit_rate_pct"),
            "stop_hit_rate_pct": metrics.get("stop_hit_rate_pct"),
            "validation_pass_rate_pct": metrics.get("validation_pass_rate_pct"),
            "packet_eligible_rate_pct": metrics.get("packet_eligible_rate_pct"),
        },
    }


def compact_replay(row):
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    return {
        "policy": row.get("policy"),
        "promotion_eligible": row.get("promotion_eligible"),
        "markets": row.get("markets") or [],
        "reasons": row.get("reasons") or [],
        "metrics": {
            "alert_count": metrics.get("alert_count"),
            "alert_rate_per_100_bars": metrics.get("alert_rate_per_100_bars"),
            "execution_candidate_count": metrics.get("execution_candidate_count"),
            "execution_candidate_rate_per_100_bars": metrics.get("execution_candidate_rate_per_100_bars"),
            "directional_confirmation_ratio_pct": metrics.get("directional_confirmation_ratio_pct"),
            "directional_downgrade_ratio_pct": metrics.get("directional_downgrade_ratio_pct"),
        },
    }


def convergence_for(forward, replay):
    forward_policy = (forward or {}).get("policy")
    replay_policy = (replay or {}).get("policy")
    forward_metrics = (forward or {}).get("metrics") if isinstance((forward or {}).get("metrics"), dict) else {}
    resolved = as_int(forward_metrics.get("resolved_count"))
    reasons = []
    confidence = "LOW"

    if not forward:
        reasons.append("forward_strategy_review_missing_for_trigger")
        status = "FORWARD_MISSING"
    elif resolved <= 0:
        reasons.append("forward_outcome_sample_missing")
        status = "INSUFFICIENT_FORWARD_SAMPLE"
    elif any(str(reason).startswith("trigger_outcome_sample_below_") for reason in (forward or {}).get("reasons") or []):
        reasons.append("forward_outcome_sample_below_policy_minimum")
        status = "INSUFFICIENT_FORWARD_SAMPLE"
    elif not replay:
        reasons.append("replay_strategy_review_missing_for_trigger")
        status = "REPLAY_MISSING"
    elif forward_policy in FORWARD_RISK_POLICIES and replay_policy in REPLAY_RISK_POLICIES:
        reasons.append("forward_and_replay_both_flag_trigger_risk")
        status = "CONVERGED_RISK"
        confidence = "HIGH" if resolved >= 10 else "MEDIUM"
    elif forward_policy == "candidate_allow_after_other_gates" and replay_policy in REPLAY_RISK_POLICIES:
        reasons.append("forward_allows_but_replay_flags_noise")
        status = "REPLAY_CHALLENGES_FORWARD"
        confidence = "MEDIUM" if resolved >= 10 else "LOW"
    elif forward_policy in FORWARD_RISK_POLICIES and replay_policy == "candidate_allow_after_other_gates":
        reasons.append("forward_flags_risk_but_replay_does_not")
        status = "FORWARD_CHALLENGES_REPLAY"
        confidence = "MEDIUM" if resolved >= 10 else "LOW"
    elif forward_policy == "candidate_allow_after_other_gates" and replay_policy == "candidate_allow_after_other_gates":
        reasons.append("forward_and_replay_both_clean")
        status = "CONVERGED_CLEAN"
        confidence = "MEDIUM" if resolved >= 10 else "LOW"
    else:
        reasons.append("mixed_or_diagnostic_trigger_evidence")
        status = "MIXED"

    avg_return = as_float(forward_metrics.get("avg_signed_close_return_pct"))
    win_rate = as_float(forward_metrics.get("win_rate_pct"))
    if avg_return is not None and avg_return <= 0:
        reasons.append("forward_avg_return_not_positive")
    if win_rate is not None and win_rate < 45:
        reasons.append("forward_win_rate_below_45")
    return {"status": status, "confidence": confidence, "reasons": reasons}


def build_rows(strategy_review, replay_review):
    forward_rows = {
        forward_key(row): row
        for row in (strategy_review.get("trigger_policies") or [])
        if isinstance(row, dict)
    }
    replay_rows = {
        replay_key(row): row
        for row in (replay_review.get("strategy_trigger_summary") or [])
        if isinstance(row, dict)
    }
    keys = sorted(set(forward_rows) | set(replay_rows))
    rows = []
    for key in keys:
        forward = forward_rows.get(key)
        replay = replay_rows.get(key)
        convergence = convergence_for(forward, replay)
        side, trigger = key.split(":", 1) if ":" in key else ("UNKNOWN", key)
        rows.append(
            {
                "key": key,
                "signal_type": side,
                "trigger": trigger,
                "status": convergence["status"],
                "confidence": convergence["confidence"],
                "reasons": convergence["reasons"],
                "forward": compact_forward(forward or {}),
                "replay": compact_replay(replay or {}),
                "promotion_eligible": False,
                "hermes_use": "challenge_or_support_context_only",
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            {
                "CONVERGED_RISK": 0,
                "REPLAY_CHALLENGES_FORWARD": 1,
                "FORWARD_CHALLENGES_REPLAY": 2,
                "INSUFFICIENT_FORWARD_SAMPLE": 3,
                "REPLAY_MISSING": 4,
                "FORWARD_MISSING": 5,
                "MIXED": 6,
                "CONVERGED_CLEAN": 7,
            }.get(row["status"], 9),
            policy_rank((row.get("forward") or {}).get("policy")),
            -(as_int(((row.get("forward") or {}).get("metrics") or {}).get("resolved_count"))),
            -(as_int(((row.get("replay") or {}).get("metrics") or {}).get("alert_count"))),
            row["key"],
        ),
    )


def build_summary(rows, checks):
    status_counts = {}
    confidence_counts = {}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        confidence_counts[row["confidence"]] = confidence_counts.get(row["confidence"], 0) + 1
    if any(item["status"] == "FAIL" for item in checks):
        status = "MISSING"
    elif status_counts.get("CONVERGED_RISK") or status_counts.get("REPLAY_CHALLENGES_FORWARD"):
        status = "REVIEW_REQUIRED"
    elif status_counts.get("CONVERGED_CLEAN"):
        status = "SUPPORTIVE_WITH_LIMITS"
    else:
        status = "INSUFFICIENT"
    return {
        "status": status,
        "promotion_ready": False,
        "promotion_eligible": False,
        "trigger_count": len(rows),
        "status_counts": status_counts,
        "confidence_counts": confidence_counts,
        "converged_risk_count": status_counts.get("CONVERGED_RISK", 0),
        "replay_challenges_forward_count": status_counts.get("REPLAY_CHALLENGES_FORWARD", 0),
        "insufficient_forward_sample_count": status_counts.get("INSUFFICIENT_FORWARD_SAMPLE", 0),
    }


def build_report(strategy_review=None, replay_strategy_review=None):
    strategy_review = strategy_review if isinstance(strategy_review, dict) else load_json_file(STRATEGY_REVIEW_REPORT_FILE)
    replay_strategy_review = (
        replay_strategy_review
        if isinstance(replay_strategy_review, dict)
        else load_json_file(V5_REPLAY_STRATEGY_REVIEW_REPORT_FILE)
    )
    checks = []
    if strategy_review.get("schema") != "strategy_review_report_v1":
        checks.append(
            check(
                "FAIL",
                "strategy_review_report_missing_or_invalid",
                "A valid strategy_review_report_v1 payload is required for forward outcome evidence.",
                {"source_file": STRATEGY_REVIEW_REPORT_FILE},
            )
        )
    else:
        checks.append(check("OK", "strategy_review_report_loaded", "Forward strategy review report loaded."))
    if replay_strategy_review.get("schema") != "v5_replay_strategy_review_report_v1":
        checks.append(
            check(
                "WARN",
                "v5_replay_strategy_review_missing_or_invalid",
                "Replay strategy review is unavailable; convergence will rely only on forward evidence.",
                {"source_file": V5_REPLAY_STRATEGY_REVIEW_REPORT_FILE},
            )
        )
    else:
        checks.append(check("OK", "v5_replay_strategy_review_loaded", "Replay strategy review report loaded."))

    rows = build_rows(strategy_review or {}, replay_strategy_review or {})
    payload = {
        "schema": "trigger_evidence_convergence_report_v1",
        "generated_at": now_iso(),
        "source": {
            "read_only": True,
            "auto_applies_strategy_changes": False,
            "not_strategy_config_proposal_input": True,
            "strategy_review_report_file": STRATEGY_REVIEW_REPORT_FILE,
            "strategy_review_schema": strategy_review.get("schema"),
            "v5_replay_strategy_review_report_file": V5_REPLAY_STRATEGY_REVIEW_REPORT_FILE,
            "v5_replay_strategy_review_schema": replay_strategy_review.get("schema"),
        },
        "operator_contract": {
            "read_only": True,
            "submits_orders": False,
            "writes_alert_queue": False,
            "changes_strategy_config": False,
            "changes_execution_mode": False,
            "promotion_eligible": False,
        },
        "summary": build_summary(rows, checks),
        "trigger_evidence": rows,
        "checks": checks,
        "recommendations": recommendations(rows),
        "warnings": [
            "convergence_report_is_not_strategy_config_input",
            "forward_outcome_remains_authoritative_for_strategy_promotion",
            "replay_evidence_can_challenge_or_cap_confidence_but_cannot_approve_execution",
        ],
    }
    return payload


def recommendations(rows):
    recs = []
    converged = [row["key"] for row in rows if row.get("status") == "CONVERGED_RISK"]
    replay_challenges = [row["key"] for row in rows if row.get("status") == "REPLAY_CHALLENGES_FORWARD"]
    insufficient = [row["key"] for row in rows if row.get("status") == "INSUFFICIENT_FORWARD_SAMPLE"]
    for key in converged[:8]:
        recs.append(f"prioritize_trigger_rework_or_threshold_review:{key}")
    for key in replay_challenges[:8]:
        recs.append(f"cap_hermes_confidence_until_forward_and_replay_align:{key}")
    if insufficient:
        recs.append("collect_more_forward_outcomes_before_config_promotion")
    if not recs:
        recs.append("continue_shadow_observation_until_convergence_is_supportive")
    return recs


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Trigger evidence convergence {payload.get('generated_at')}",
        (
            f"status={summary.get('status')} triggers={summary.get('trigger_count')} "
            f"converged_risk={summary.get('converged_risk_count')} "
            f"replay_challenges={summary.get('replay_challenges_forward_count')}"
        ),
    ]
    for row in (payload.get("trigger_evidence") or [])[:12]:
        forward = row.get("forward") or {}
        replay = row.get("replay") or {}
        lines.append(
            f"  {row.get('key')}: status={row.get('status')} confidence={row.get('confidence')} "
            f"forward={forward.get('policy')} replay={replay.get('policy')} reasons={row.get('reasons')}"
        )
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"][:12]))
    failures = [item.get("code") for item in payload.get("checks") or [] if item.get("status") == "FAIL"]
    if failures:
        lines.append("Failures: " + ", ".join(failures))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-review-file", default=STRATEGY_REVIEW_REPORT_FILE)
    parser.add_argument("--v5-replay-strategy-review-file", default=V5_REPLAY_STRATEGY_REVIEW_REPORT_FILE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    global STRATEGY_REVIEW_REPORT_FILE, V5_REPLAY_STRATEGY_REVIEW_REPORT_FILE
    STRATEGY_REVIEW_REPORT_FILE = args.strategy_review_file
    V5_REPLAY_STRATEGY_REVIEW_REPORT_FILE = args.v5_replay_strategy_review_file
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
    return 0 if payload["summary"]["status"] != "MISSING" else 2


if __name__ == "__main__":
    sys.exit(main())
