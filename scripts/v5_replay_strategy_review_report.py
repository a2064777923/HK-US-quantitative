#!/usr/bin/env python3
"""Read-only trigger policy hints derived from v5 local replay quality."""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime


V5_LOCAL_REPLAY_REPORT_FILE = os.environ.get("V5_LOCAL_REPLAY_REPORT_FILE", "/tmp/v5_local_replay_report.json")
REPORT_FILE = os.environ.get(
    "V5_REPLAY_STRATEGY_REVIEW_REPORT_FILE",
    "/tmp/v5_replay_strategy_review_report.json",
)
MIN_REPLAY_ALERT_SAMPLE = int(os.environ.get("V5_REPLAY_STRATEGY_REVIEW_MIN_ALERT_SAMPLE", "10"))

POLICY_RANK = {
    "disable_execution_review": 0,
    "tighten_thresholds": 1,
    "shadow_only": 2,
    "diagnostic_only": 3,
    "candidate_allow_after_other_gates": 4,
}


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


def ratio_pct(numerator, denominator):
    if not denominator:
        return None
    return round(float(numerator) / float(denominator) * 100.0, 2)


def strategy_key(candidate_signal_type, trigger):
    side = str(candidate_signal_type or "UNKNOWN").upper()
    return f"{side}:{trigger or 'UNKNOWN'}"


def check(status, code, detail, data=None):
    return {"status": status, "code": code, "detail": detail, "data": data or {}}


def replay_policy_for_trigger(row):
    reasons = list(row.get("reasons") or [])
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    candidate_signal_type = str(row.get("candidate_signal_type") or "").upper()
    alert_count = as_int(metrics.get("alert_count"))
    execution_rate = as_float(metrics.get("execution_candidate_rate_per_100_bars"))
    alert_rate = as_float(metrics.get("alert_rate_per_100_bars"))
    confirmation_ratio = as_float(metrics.get("directional_confirmation_ratio_pct"))
    downgrade_ratio = as_float(metrics.get("directional_downgrade_ratio_pct"))
    policy_reasons = []

    if candidate_signal_type not in ("BUY", "SELL"):
        return "diagnostic_only", ["non_directional_replay_trigger_is_not_execution_candidate"]
    if alert_count < MIN_REPLAY_ALERT_SAMPLE:
        return "shadow_only", [f"replay_trigger_sample_below_{MIN_REPLAY_ALERT_SAMPLE}"]

    if execution_rate is not None and execution_rate > 0:
        policy_reasons.append("replay_execution_candidates_exist")
    if "trigger_execution_candidate_density_high" in reasons:
        policy_reasons.append("replay_execution_candidate_density_high")
    if "trigger_replay_alert_density_high" in reasons:
        policy_reasons.append("replay_alert_density_high")
    if "trigger_directional_confirmation_ratio_low" in reasons:
        policy_reasons.append("replay_directional_confirmation_ratio_low")
    if "trigger_directional_downgrade_ratio_high" in reasons:
        policy_reasons.append("replay_directional_downgrade_ratio_high")

    if "replay_execution_candidate_density_high" in policy_reasons:
        return "tighten_thresholds", policy_reasons
    if execution_rate is not None and execution_rate > 0 and confirmation_ratio is not None and confirmation_ratio < 35.0:
        policy_reasons.append("replay_low_density_execution_candidates_have_weak_confirmation")
    if alert_rate is not None and alert_rate > 0 and downgrade_ratio is not None and downgrade_ratio > 60.0:
        policy_reasons.append("replay_noisy_directional_candidates_should_stay_shadow")
        return "shadow_only", policy_reasons
    if policy_reasons:
        return "shadow_only", policy_reasons
    return "candidate_allow_after_other_gates", []


def trigger_policy_row(row):
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    policy, policy_reasons = replay_policy_for_trigger(row)
    candidate_signal_type = str(row.get("candidate_signal_type") or "UNKNOWN").upper()
    trigger = row.get("trigger") or "UNKNOWN"
    return {
        "key": row.get("key") or f"{row.get('market') or 'UNKNOWN'}:{candidate_signal_type}:{trigger}",
        "strategy_key": strategy_key(candidate_signal_type, trigger),
        "market": row.get("market") or "UNKNOWN",
        "signal_type": candidate_signal_type,
        "trigger": trigger,
        "policy": policy,
        "execution_allowed_by_report": False,
        "review_required": policy != "candidate_allow_after_other_gates",
        "promotion_eligible": False,
        "reasons": policy_reasons,
        "replay_reasons": row.get("reasons") or [],
        "metrics": {
            "denominator_bars": metrics.get("denominator_bars"),
            "alert_count": metrics.get("alert_count"),
            "pct_of_all_alerts": metrics.get("pct_of_all_alerts"),
            "alert_rate_per_100_bars": metrics.get("alert_rate_per_100_bars"),
            "execution_candidate_count": metrics.get("execution_candidate_count"),
            "execution_candidate_rate_per_100_bars": metrics.get("execution_candidate_rate_per_100_bars"),
            "confirmed_directional_count": metrics.get("confirmed_directional_count"),
            "downgraded_directional_count": metrics.get("downgraded_directional_count"),
            "directional_confirmation_ratio_pct": metrics.get("directional_confirmation_ratio_pct"),
            "directional_downgrade_ratio_pct": metrics.get("directional_downgrade_ratio_pct"),
            "execution_candidate_ratio_pct": metrics.get("execution_candidate_ratio_pct"),
        },
        "hermes_note": "Replay policy is challenge context only; require forward outcomes before strategy promotion.",
    }


def combined_policy(rows):
    if not rows:
        return "candidate_allow_after_other_gates"
    return min((row.get("policy") or "candidate_allow_after_other_gates" for row in rows), key=lambda value: POLICY_RANK.get(value, 99))


def strategy_trigger_summary(trigger_policies):
    grouped = defaultdict(list)
    for row in trigger_policies:
        grouped[row["strategy_key"]].append(row)
    summaries = []
    for key, rows in grouped.items():
        counts = Counter()
        denominator_bars = 0
        markets = []
        reasons = Counter()
        for row in rows:
            metrics = row.get("metrics") or {}
            counts["alert_count"] += as_int(metrics.get("alert_count"))
            counts["execution_candidate_count"] += as_int(metrics.get("execution_candidate_count"))
            counts["confirmed_directional_count"] += as_int(metrics.get("confirmed_directional_count"))
            counts["downgraded_directional_count"] += as_int(metrics.get("downgraded_directional_count"))
            denominator_bars += as_int(metrics.get("denominator_bars"))
            markets.append(row.get("market"))
            reasons.update(row.get("reasons") or [])
        signal_type, trigger = key.split(":", 1) if ":" in key else ("UNKNOWN", key)
        policy = combined_policy(rows)
        summaries.append(
            {
                "strategy_key": key,
                "signal_type": signal_type,
                "trigger": trigger,
                "policy": policy,
                "promotion_eligible": False,
                "market_count": len(set(markets)),
                "markets": sorted(set(markets)),
                "market_policy_keys": [row.get("key") for row in rows],
                "reasons": sorted(reasons),
                "metrics": {
                    "denominator_bars": denominator_bars,
                    "alert_count": counts["alert_count"],
                    "alert_rate_per_100_bars": ratio_pct(counts["alert_count"], denominator_bars),
                    "execution_candidate_count": counts["execution_candidate_count"],
                    "execution_candidate_rate_per_100_bars": ratio_pct(
                        counts["execution_candidate_count"],
                        denominator_bars,
                    ),
                    "directional_confirmation_ratio_pct": ratio_pct(
                        counts["confirmed_directional_count"],
                        counts["alert_count"],
                    ),
                    "directional_downgrade_ratio_pct": ratio_pct(
                        counts["downgraded_directional_count"],
                        counts["alert_count"],
                    ),
                },
            }
        )
    return sorted(
        summaries,
        key=lambda row: (
            POLICY_RANK.get(row["policy"], 99),
            -(row["metrics"].get("alert_count") or 0),
            row["strategy_key"],
        ),
    )


def overall_policy(replay_payload, trigger_policies):
    summary = replay_payload.get("summary") if isinstance(replay_payload.get("summary"), dict) else {}
    replay_quality = replay_payload.get("replay_quality") if isinstance(replay_payload.get("replay_quality"), dict) else {}
    quality_status = str(replay_quality.get("status") or "MISSING").upper()
    reasons = []
    if summary.get("promotion_ready") is not True:
        reasons.append("v5_local_replay_not_promotion_ready")
    if quality_status in ("WARN", "FAIL", "MISSING"):
        reasons.append(f"v5_local_replay_quality_{quality_status.lower()}")
    if any(row.get("policy") in ("disable_execution_review", "tighten_thresholds") for row in trigger_policies):
        reasons.append("one_or_more_replay_triggers_need_threshold_review")
    if any(row.get("policy") == "shadow_only" for row in trigger_policies):
        reasons.append("one_or_more_replay_triggers_should_remain_shadow_only")
    return {
        "policy": "keep_shadow_or_dry_run" if reasons else "candidate_for_limited_paper_execution_review",
        "execution_allowed_by_report": False,
        "promotion_eligible": False,
        "reasons": reasons,
        "metrics": {
            "replay_status": summary.get("overall_status"),
            "quality_status": quality_status,
            "symbol_count": summary.get("symbol_count"),
            "evaluated_bars": summary.get("evaluated_bars"),
            "alert_count": summary.get("alert_count"),
            "execution_candidate_count": summary.get("execution_candidate_count"),
            "downgraded_directional_count": summary.get("downgraded_directional_count"),
        },
    }


def build_recommendations(payload):
    recs = ["do_not_promote_strategy_config_from_replay_only"]
    tighten = [row["strategy_key"] for row in payload.get("strategy_trigger_summary") or [] if row.get("policy") == "tighten_thresholds"]
    shadow = [row["strategy_key"] for row in payload.get("strategy_trigger_summary") or [] if row.get("policy") == "shadow_only"]
    diagnostic = [row["strategy_key"] for row in payload.get("strategy_trigger_summary") or [] if row.get("policy") == "diagnostic_only"]
    for key in tighten[:8]:
        recs.append(f"review_threshold_tightening_with_forward_outcomes:{key}")
    for key in shadow[:8]:
        recs.append(f"keep_replay_noisy_trigger_shadow_until_forward_evidence:{key}")
    if diagnostic:
        recs.append("keep_non_directional_replay_triggers_diagnostic_only")
    recs.append("rerun_forward_outcome_and_simulation_review_before_any_config_promotion")
    return recs


def build_report(v5_local_replay=None):
    replay_payload = v5_local_replay if isinstance(v5_local_replay, dict) else load_json_file(V5_LOCAL_REPLAY_REPORT_FILE)
    checks = []
    if replay_payload.get("schema") != "v5_local_replay_report_v1":
        checks.append(
            check(
                "FAIL",
                "v5_local_replay_report_missing_or_invalid",
                "A valid v5_local_replay_report_v1 payload is required.",
                {"source_file": V5_LOCAL_REPLAY_REPORT_FILE},
            )
        )
        trigger_policies = []
    else:
        checks.append(
            check(
                "OK",
                "v5_local_replay_report_loaded",
                "Replay strategy review consumed a valid local replay report.",
                {"source_file": V5_LOCAL_REPLAY_REPORT_FILE},
            )
        )
        breakdown = replay_payload.get("replay_breakdown") if isinstance(replay_payload.get("replay_breakdown"), dict) else {}
        trigger_groups = breakdown.get("trigger_groups") if isinstance(breakdown.get("trigger_groups"), list) else []
        trigger_policies = [trigger_policy_row(row) for row in trigger_groups if isinstance(row, dict)]

    trigger_policies = sorted(
        trigger_policies,
        key=lambda row: (
            POLICY_RANK.get(row.get("policy"), 99),
            -(row.get("metrics") or {}).get("alert_count", 0),
            row.get("key") or "",
        ),
    )
    strategy_summaries = strategy_trigger_summary(trigger_policies)
    payload = {
        "schema": "v5_replay_strategy_review_report_v1",
        "generated_at": now_iso(),
        "source": {
            "read_only": True,
            "auto_applies_strategy_changes": False,
            "promotion_eligible": False,
            "not_strategy_config_proposal_input": True,
            "v5_local_replay_report_file": V5_LOCAL_REPLAY_REPORT_FILE,
            "v5_local_replay_schema": replay_payload.get("schema"),
            "v5_local_replay_status": ((replay_payload.get("summary") or {}).get("overall_status"))
            if isinstance(replay_payload.get("summary"), dict)
            else None,
        },
        "operator_contract": {
            "read_only": True,
            "submits_orders": False,
            "writes_alert_queue": False,
            "changes_strategy_config": False,
            "changes_execution_mode": False,
            "promotion_eligible": False,
            "requires_forward_outcome_before_promotion": True,
        },
        "overall_policy": overall_policy(replay_payload, trigger_policies),
        "replay_trigger_policies": trigger_policies,
        "strategy_trigger_summary": strategy_summaries,
        "summary": {
            "status": "RESEARCH_REVIEW_ONLY" if not any(item["status"] == "FAIL" for item in checks) else "MISSING",
            "promotion_ready": False,
            "promotion_eligible": False,
            "trigger_policy_count": len(trigger_policies),
            "strategy_trigger_count": len(strategy_summaries),
            "tighten_thresholds_count": sum(1 for row in strategy_summaries if row.get("policy") == "tighten_thresholds"),
            "shadow_only_count": sum(1 for row in strategy_summaries if row.get("policy") == "shadow_only"),
            "diagnostic_only_count": sum(1 for row in strategy_summaries if row.get("policy") == "diagnostic_only"),
            "candidate_allow_count": sum(
                1 for row in strategy_summaries if row.get("policy") == "candidate_allow_after_other_gates"
            ),
        },
        "checks": checks,
        "warnings": [
            "replay_policy_is_not_forward_outcome_evidence",
            "market_specific_replay_noise_must_not_be_forced_into_global_trigger_overrides_without_validation",
        ],
        "recommendations": [],
    }
    payload["recommendations"] = build_recommendations(payload)
    return payload


def build_text_report(payload):
    summary = payload.get("summary") or {}
    overall = payload.get("overall_policy") or {}
    lines = [
        f"V5 replay strategy review {payload.get('generated_at')}",
        (
            f"status={summary.get('status')} promotion_eligible={summary.get('promotion_eligible')} "
            f"overall={overall.get('policy')} triggers={summary.get('trigger_policy_count')} "
            f"strategy_triggers={summary.get('strategy_trigger_count')}"
        ),
    ]
    for row in (payload.get("strategy_trigger_summary") or [])[:12]:
        metrics = row.get("metrics") or {}
        lines.append(
            f"  {row.get('strategy_key')}: policy={row.get('policy')} "
            f"alerts={metrics.get('alert_count')} alert_rate={metrics.get('alert_rate_per_100_bars')} "
            f"exec_rate={metrics.get('execution_candidate_rate_per_100_bars')} reasons={row.get('reasons')}"
        )
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"][:12]))
    failures = [item.get("code") for item in payload.get("checks") or [] if item.get("status") == "FAIL"]
    if failures:
        lines.append("Failures: " + ", ".join(failures))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--v5-local-replay-file", default=V5_LOCAL_REPLAY_REPORT_FILE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    global V5_LOCAL_REPLAY_REPORT_FILE
    V5_LOCAL_REPLAY_REPORT_FILE = args.v5_local_replay_file
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
