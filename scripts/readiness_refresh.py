#!/usr/bin/env python3
"""Refresh read-only Hermes/v5 evidence reports in dependency order.

This is a manual/operator helper. It does not edit crontab, submit orders,
apply event stores, promote watchlists, or call execute mode.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime


REPORT_FILE = os.environ.get("READINESS_REFRESH_REPORT_FILE", "/tmp/readiness_refresh_report.json")
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("READINESS_REFRESH_STEP_TIMEOUT_SECONDS", "120"))

STEPS = [
    {
        "name": "cron_audit",
        "cmd": ["cron_audit_report.py", "--output", "/tmp/cron_audit_report.json", "--text"],
        "network": False,
    },
    {
        "name": "system_health",
        "cmd": ["system_health_check.py", "--output", "/tmp/quantmind_system_health.json"],
        "network": False,
    },
    {
        "name": "data_health",
        "cmd": ["data_health_report.py", "--output", "/tmp/data_health_report.json", "--text"],
        "network": False,
    },
    {
        "name": "data_source_inventory",
        "cmd": [
            "data_source_inventory_report.py",
            "--output",
            "/tmp/data_source_inventory_report.json",
            "--text",
        ],
        "network": False,
    },
    {
        "name": "kline_source_granularity",
        "cmd": [
            "kline_source_granularity_report.py",
            "--output",
            "/tmp/kline_source_granularity_report.json",
            "--text",
        ],
        "network": False,
    },
    {
        "name": "external_market_context_producer",
        "cmd": [
            "external_market_context_producer.py",
            "--include-infohub",
            "--infohub-url",
            "http://127.0.0.1:8899",
            "--output",
            "/tmp/external_market_context_inputs.json",
            "--text",
        ],
        "network": True,
    },
    {
        "name": "external_market_context",
        "cmd": [
            "external_market_context_report.py",
            "--output",
            "/tmp/external_market_context_report.json",
            "--text",
        ],
        "network": False,
    },
    {
        "name": "event_catalysts",
        "cmd": ["event_catalyst_report.py", "--output", "/tmp/event_catalyst_report.json", "--text"],
        "network": False,
    },
    {
        "name": "event_catalyst_signals",
        "cmd": ["event_catalyst_signal_report.py", "--output", "/tmp/event_catalyst_signal_report.json", "--text"],
        "network": False,
    },
    {
        "name": "market_sentiment_producer",
        "cmd": ["market_sentiment_producer.py", "--output", "/tmp/market_sentiment_inputs.json", "--text"],
        "network": True,
    },
    {
        "name": "market_sentiment",
        "cmd": ["market_sentiment_report.py", "--output", "/tmp/market_sentiment_report.json", "--text"],
        "network": False,
    },
    {
        "name": "market_index_context_producer",
        "cmd": [
            "market_index_context_producer.py",
            "--output",
            "/tmp/market_index_context_inputs.json",
            "--text",
        ],
        "network": True,
    },
    {
        "name": "fundamentals_context_producer",
        "cmd": [
            "fundamentals_context_producer.py",
            "--output",
            "/tmp/fundamentals_context_inputs.json",
            "--text",
        ],
        "network": True,
    },
    {
        "name": "fundamentals_context",
        "cmd": [
            "fundamentals_context_report.py",
            "--output",
            "/tmp/fundamentals_context_report.json",
            "--text",
        ],
        "network": False,
    },
    {
        "name": "trusted_source_discovery",
        "cmd": [
            "trusted_source_discovery_report.py",
            "--output",
            "/tmp/trusted_source_discovery_report.json",
            "--text",
        ],
        "network": False,
    },
    {
        "name": "trusted_source_preflight",
        "cmd": [
            "trusted_source_preflight.py",
            "--output",
            "/tmp/trusted_source_preflight_report.json",
            "--text",
        ],
        "network": False,
    },
    {
        "name": "market_context",
        "cmd": ["market_context_report.py", "--output", "/tmp/market_context_report.json", "--text"],
        "network": False,
    },
    {
        "name": "intraday_kline_batch",
        "cmd": ["intraday_kline_batch.py", "--output", "/tmp/intraday_kline_batch.json", "--text"],
        "network": True,
    },
    {
        "name": "intraday_context",
        "cmd": ["intraday_context_report.py", "--output", "/tmp/intraday_context_report.json", "--text"],
        "network": False,
    },
    {
        "name": "intraday_timeframe_quality",
        "cmd": [
            "intraday_timeframe_quality_report.py",
            "--output",
            "/tmp/intraday_timeframe_quality_report.json",
            "--text",
        ],
        "network": False,
    },
    {
        "name": "intraday_market_session_overrides",
        "cmd": [
            "intraday_market_session_overrides_report.py",
            "--output",
            "/tmp/intraday_market_session_overrides_report.json",
            "--text",
        ],
        "network": False,
    },
    {
        "name": "portfolio_report",
        "cmd": ["portfolio_report.py", "--output", "/tmp/portfolio_report.json", "--text"],
        "network": False,
    },
    {
        "name": "watchlist_diff",
        "cmd": ["watchlist_diff_report.py", "--output", "/tmp/watchlist_diff_report.json", "--text"],
        "network": False,
    },
    {
        "name": "alert_quality",
        "cmd": ["alert_quality_report.py", "--output", "/tmp/rt_alert_quality_report.json", "--text"],
        "network": False,
    },
    {
        "name": "kline_daily_gap_repair",
        "cmd": ["kline_daily_gap_repair.py", "--output", "/tmp/kline_daily_gap_repair.json", "--text"],
        "network": True,
    },
    {
        "name": "universe_hygiene",
        "cmd": ["universe_hygiene_report.py", "--output", "/tmp/universe_hygiene_report.json", "--text"],
        "network": False,
    },
    {
        "name": "kline_gap_source_diagnostic",
        "cmd": [
            "kline_gap_source_diagnostic_report.py",
            "--output",
            "/tmp/kline_gap_source_diagnostic_report.json",
            "--text",
        ],
        "network": False,
    },
    {
        "name": "kline_gap_alternate_provider_probe",
        "cmd": [
            "kline_gap_alternate_provider_probe.py",
            "--output",
            "/tmp/kline_gap_alternate_provider_probe.json",
            "--text",
        ],
        "network": True,
    },
    {
        "name": "kline_gap_alternate_provider_repair_plan",
        "cmd": [
            "kline_gap_alternate_provider_repair_plan.py",
            "--output",
            "/tmp/kline_gap_alternate_provider_repair_plan.json",
            "--text",
        ],
        "network": True,
    },
    {
        "name": "rt_signal_outcome",
        "cmd": ["rt_signal_outcome_report.py", "--output", "/tmp/rt_signal_outcome_report.json", "--text"],
        "network": False,
    },
    {
        "name": "rt_alert_event_store",
        "cmd": ["rt_alert_event_store.py", "--output", "/tmp/rt_alert_event_store_report.json", "--text"],
        "network": False,
    },
    {
        "name": "rt_signal_outcome_event_store",
        "cmd": [
            "rt_signal_outcome_event_store.py",
            "--output",
            "/tmp/rt_signal_outcome_event_store_report.json",
            "--text",
        ],
        "network": False,
    },
    {
        "name": "source_reliability",
        "cmd": ["source_reliability_report.py", "--output", "/tmp/source_reliability_report.json", "--text"],
        "network": False,
    },
    {
        "name": "hermes_judgment_audit",
        "cmd": ["hermes_judgment_audit_report.py", "--output", "/tmp/hermes_judgment_audit_report.json", "--text"],
        "network": False,
    },
    {
        "name": "strategy_learning",
        "cmd": ["strategy_learning_report.py", "--output", "/tmp/strategy_learning_report.json", "--text"],
        "network": False,
    },
    {
        "name": "simulation_performance",
        "cmd": ["simulation_performance_report.py", "--output", "/tmp/simulation_performance_report.json", "--text"],
        "network": False,
    },
    {
        "name": "simulation_postmortem_audit",
        "cmd": [
            "simulation_postmortem_audit_report.py",
            "--output",
            "/tmp/simulation_postmortem_audit_report.json",
            "--text",
        ],
        "network": False,
    },
    {
        "name": "simulation_postmortem_note_draft",
        "cmd": [
            "simulation_postmortem_note_draft_report.py",
            "--output",
            "/tmp/simulation_postmortem_note_draft_report.json",
            "--text",
        ],
        "network": False,
    },
    {
        "name": "hermes_judgment_event_store",
        "cmd": [
            "hermes_judgment_event_store.py",
            "--output",
            "/tmp/hermes_judgment_event_store_report.json",
            "--text",
        ],
        "network": False,
    },
    {
        "name": "rt_order_intake_event_store",
        "cmd": [
            "rt_order_intake_event_store.py",
            "--output",
            "/tmp/rt_order_intake_event_store_report.json",
            "--text",
        ],
        "network": False,
    },
    {
        "name": "hermes_position_judgment_audit",
        "cmd": [
            "hermes_position_judgment_audit_report.py",
            "--output",
            "/tmp/hermes_position_judgment_audit_report.json",
            "--text",
        ],
        "network": False,
    },
    {
        "name": "execution_readiness",
        "cmd": ["execution_readiness_report.py", "--output", "/tmp/execution_readiness_report.json", "--text"],
        "network": False,
    },
    {
        "name": "hermes_review_packet_seed",
        "cmd": [
            "hermes_review_packet.py",
            "--output",
            "/tmp/hermes_signal_review_packet.json",
            "--ephemeral-state",
            "--no-archive",
        ],
        "network": False,
    },
    {
        "name": "operator_action_queue",
        "cmd": ["operator_action_queue_report.py", "--output", "/tmp/operator_action_queue_report.json", "--text"],
        "network": False,
    },
    {
        "name": "hermes_review_packet",
        "cmd": ["hermes_review_packet.py", "--output", "/tmp/hermes_signal_review_packet.json", "--ephemeral-state"],
        "network": False,
    },
]


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


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


def selected_steps(skip_network_producers=False, only=None, skip=None):
    only = set(expand_step_names(only))
    skip = set(expand_step_names(skip))
    steps = []
    for step in STEPS:
        if only and step["name"] not in only:
            continue
        if step["name"] in skip:
            continue
        if skip_network_producers and step.get("network"):
            continue
        steps.append(step)
    return steps


def expand_step_names(values):
    names = []
    for value in values or []:
        for part in str(value).split(","):
            name = part.strip()
            if name:
                names.append(name)
    return names


def command_for_step(step, scripts_dir):
    script = os.path.join(scripts_dir, step["cmd"][0])
    return [sys.executable, script] + list(step["cmd"][1:])


def run_step(step, scripts_dir, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, dry_run=False):
    cmd = command_for_step(step, scripts_dir)
    if dry_run:
        return {
            "name": step["name"],
            "status": "DRY_RUN",
            "cmd": cmd,
            "returncode": None,
            "duration_seconds": 0.0,
            "stdout_tail": "",
            "stderr_tail": "",
        }
    started = datetime.now()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
        status = "PASS" if result.returncode == 0 else "FAIL"
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        returncode = result.returncode
    except subprocess.TimeoutExpired as exc:
        status = "FAIL"
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + f"\ntimeout_after_seconds:{timeout_seconds}"
        returncode = None
    duration = round((datetime.now() - started).total_seconds(), 3)
    return {
        "name": step["name"],
        "status": status,
        "cmd": cmd,
        "returncode": returncode,
        "duration_seconds": duration,
        "stdout_tail": tail_text(stdout),
        "stderr_tail": tail_text(stderr),
    }


def tail_text(value, max_chars=2000):
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def build_report(
    steps=None,
    scripts_dir=None,
    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    dry_run=False,
):
    scripts_dir = scripts_dir or os.path.dirname(__file__)
    step_results = []
    for step in steps or STEPS:
        step_results.append(run_step(step, scripts_dir, timeout_seconds=timeout_seconds, dry_run=dry_run))
    failed = [step for step in step_results if step["status"] == "FAIL"]
    status = "FAIL" if failed else "DRY_RUN" if dry_run else "OK"
    return {
        "schema": "readiness_refresh_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_crontab": False,
            "uses_apply_flags": False,
            "uses_execute_mode": False,
            "scripts_dir": scripts_dir,
            "timeout_seconds": timeout_seconds,
            "dry_run": dry_run,
        },
        "summary": {
            "step_count": len(step_results),
            "passed_count": len([step for step in step_results if step["status"] == "PASS"]),
            "failed_count": len(failed),
            "dry_run_count": len([step for step in step_results if step["status"] == "DRY_RUN"]),
        },
        "steps": step_results,
        "failed_steps": failed,
        "recommendations": recommendations(status, failed, dry_run=dry_run),
    }


def recommendations(status, failed_steps, dry_run=False):
    if dry_run:
        return ["review_planned_read_only_refresh_steps_before_running"]
    if status == "OK":
        return ["read_only_evidence_refresh_completed"]
    return [f"inspect_failed_refresh_step:{step['name']}" for step in failed_steps]


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Readiness refresh report {payload['generated_at']} status={payload['status']}",
        (
            f"steps={summary.get('step_count')} passed={summary.get('passed_count')} "
            f"failed={summary.get('failed_count')} dry_run={summary.get('dry_run_count')}"
        ),
    ]
    for step in payload.get("steps") or []:
        lines.append(f"  {step['status']} {step['name']} rc={step.get('returncode')} secs={step.get('duration_seconds')}")
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--scripts-dir", default=os.path.dirname(__file__))
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--skip-network-producers", action="store_true")
    parser.add_argument("--only", action="append", default=[], help="run only this step name; may be repeated")
    parser.add_argument("--skip", action="append", default=[], help="skip this step name; may be repeated")
    parser.add_argument("--dry-run", action="store_true", help="show planned commands without running them")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    steps = selected_steps(
        skip_network_producers=args.skip_network_producers,
        only=args.only,
        skip=args.skip,
    )
    payload = build_report(
        steps=steps,
        scripts_dir=args.scripts_dir,
        timeout_seconds=args.timeout_seconds,
        dry_run=args.dry_run,
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
    return 0 if payload["status"] in ("OK", "DRY_RUN") else 2


if __name__ == "__main__":
    raise SystemExit(main())
