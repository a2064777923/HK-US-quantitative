#!/usr/bin/env python3
"""Read-only audit for Hermes/v5 cron wiring.

This report does not install, edit, or remove crontab entries. It only makes
configuration drift visible to Hermes and the operator.
"""
import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime


REPORT_FILE = os.environ.get("CRON_AUDIT_REPORT_FILE", "/tmp/cron_audit_report.json")
FEISHU_ENV_FILE = os.environ.get("FEISHU_ENV_FILE", "/root/.quantmind_env")
ALERT_SENT_FILE = os.environ.get("RT_ALERT_SENT_FILE", "/tmp/rt_signal_sent.json")
POSITION_REVIEW_SENT_FILE = os.environ.get("RT_POSITION_REVIEW_SENT_FILE", "/tmp/rt_position_review_sent.json")
FEISHU_REQUIRED_ENV = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID")

REQUIRED_READ_ONLY_JOBS = [
    {
        "name": "system_health",
        "tokens": ["system_health_check.py", "/tmp/quantmind_system_health.json"],
        "why": "fresh system health is required before execution can be trusted",
        "recommended_cron": "*/5 * * * * /usr/bin/python3 /root/system_health_check.py --output /tmp/quantmind_system_health.json >> /tmp/quantmind_health.log 2>&1",
    },
    {
        "name": "data_health",
        "tokens": ["data_health_report.py", "/tmp/data_health_report.json"],
        "why": "K-line, signal, and feature-run integrity must be current",
        "recommended_cron": "*/15 * * * * /usr/bin/python3 /root/data_health_report.py --output /tmp/data_health_report.json --text >> /tmp/data_health_report.log 2>&1",
    },
    {
        "name": "data_source_inventory",
        "tokens": ["data_source_inventory_report.py", "/tmp/data_source_inventory_report.json"],
        "why": "Hermes needs a visible ledger of DB tables, K-line provenance, context files, and provider payloads before claiming data-source awareness",
        "recommended_cron": "*/10 * * * * /usr/bin/python3 /root/data_source_inventory_report.py --output /tmp/data_source_inventory_report.json --text >> /tmp/data_source_inventory_report.log 2>&1",
    },
    {
        "name": "kline_source_granularity",
        "tokens": ["kline_source_granularity_report.py", "/tmp/kline_source_granularity_report.json"],
        "why": "Hermes needs a hash-gated provenance proposal before claiming minute rows are full OHLCV or snapshot-only",
        "recommended_cron": "*/30 * * * * /usr/bin/python3 /root/kline_source_granularity_report.py --output /tmp/kline_source_granularity_report.json --text >> /tmp/kline_source_granularity_report.log 2>&1",
    },
    {
        "name": "intraday_kline_batch",
        "tokens": ["intraday_kline_batch.py", "/tmp/intraday_kline_batch.json"],
        "why": "current-session minute K-line producer plans must be visible before Hermes claims intraday collection coverage",
        "recommended_cron": "*/5 * * * * /usr/bin/python3 /root/intraday_kline_batch.py --output /tmp/intraday_kline_batch.json --text >> /tmp/intraday_kline_batch.log 2>&1",
    },
    {
        "name": "intraday_context",
        "tokens": ["intraday_context_report.py", "/tmp/intraday_context_report.json"],
        "why": "Hermes needs fresh minute-bar confirmation or contradiction context after intraday producer refreshes",
        "recommended_cron": "*/5 * * * * /usr/bin/python3 /root/intraday_context_report.py --output /tmp/intraday_context_report.json --text >> /tmp/intraday_context_report.log 2>&1",
    },
    {
        "name": "intraday_timeframe_quality",
        "tokens": ["intraday_timeframe_quality_report.py", "/tmp/intraday_timeframe_quality_report.json"],
        "why": "Hermes needs a compact 5m/15m/30m/60m quality gate before treating finer-grained evidence as confirmation",
        "recommended_cron": "*/5 * * * * /usr/bin/python3 /root/intraday_timeframe_quality_report.py --output /tmp/intraday_timeframe_quality_report.json --text >> /tmp/intraday_timeframe_quality_report.log 2>&1",
    },
    {
        "name": "intraday_market_session_overrides",
        "tokens": [
            "intraday_market_session_overrides_report.py",
            "/tmp/intraday_market_session_overrides_report.json",
        ],
        "why": "Hermes needs read-only validation of holiday and half-day session overrides before trusting intraday market-session context",
        "recommended_cron": "*/30 * * * * /usr/bin/python3 /root/intraday_market_session_overrides_report.py --output /tmp/intraday_market_session_overrides_report.json --text >> /tmp/intraday_market_session_overrides_report.log 2>&1",
    },
    {
        "name": "source_reliability",
        "tokens": ["source_reliability_report.py", "/tmp/source_reliability_report.json"],
        "why": "Hermes needs a unified matrix of degraded, fallback, stale, missing, or failed source layers",
        "recommended_cron": "*/10 * * * * /usr/bin/python3 /root/source_reliability_report.py --output /tmp/source_reliability_report.json --text >> /tmp/source_reliability_report.log 2>&1",
    },
    {
        "name": "trusted_source_preflight",
        "tokens": ["trusted_source_preflight.py", "/tmp/trusted_source_preflight_report.json"],
        "why": "Hermes needs read-only validation of Wudao, broker, official, sentiment, and fundamentals payloads before trusting them as structured context",
        "recommended_cron": "*/10 * * * * /usr/bin/python3 /root/trusted_source_preflight.py --output /tmp/trusted_source_preflight_report.json --text >> /tmp/trusted_source_preflight.log 2>&1",
    },
    {
        "name": "trusted_source_discovery",
        "tokens": ["trusted_source_discovery_report.py", "/tmp/trusted_source_discovery_report.json"],
        "why": "Hermes needs an inventory of configured Wudao, InfoHub, broker, official, and vendor source adapters",
        "recommended_cron": "*/10 * * * * /usr/bin/python3 /root/trusted_source_discovery_report.py --output /tmp/trusted_source_discovery_report.json --text >> /tmp/trusted_source_discovery_report.log 2>&1",
    },
    {
        "name": "market_context",
        "tokens": ["market_context_report.py", "/tmp/market_context_report.json"],
        "why": "Hermes needs market-regime context before new exposure",
        "recommended_cron": "*/30 * * * * /usr/bin/python3 /root/market_context_report.py --output /tmp/market_context_report.json --text >> /tmp/market_context_report.log 2>&1",
    },
    {
        "name": "external_market_context",
        "tokens": [
            "external_market_context_producer.py",
            "--include-infohub",
            "--infohub-url http://127.0.0.1:8899",
            "external_market_context_report.py",
        ],
        "why": "Hermes needs current-event/news/macro context",
        "recommended_cron": "*/5 * * * * /usr/bin/python3 /root/external_market_context_producer.py --include-infohub --infohub-url http://127.0.0.1:8899 --output /tmp/external_market_context_inputs.json --text >> /tmp/external_market_context_producer.log 2>&1 && /usr/bin/python3 /root/external_market_context_report.py --output /tmp/external_market_context_report.json --text >> /tmp/external_market_context_report.log 2>&1",
    },
    {
        "name": "event_catalysts",
        "tokens": ["event_catalyst_report.py", "/tmp/event_catalyst_report.json"],
        "why": "watchlist-linked events must be visible before trade review",
        "recommended_cron": "*/5 * * * * /usr/bin/python3 /root/event_catalyst_report.py --output /tmp/event_catalyst_report.json --text >> /tmp/event_catalyst_report.log 2>&1",
    },
    {
        "name": "event_catalyst_signals",
        "tokens": ["event_catalyst_signal_report.py", "/tmp/event_catalyst_signal_report.json"],
        "why": "watchlist-linked events should become read-only review signals for Hermes to support or challenge v5 alerts",
        "recommended_cron": "*/5 * * * * /usr/bin/python3 /root/event_catalyst_signal_report.py --output /tmp/event_catalyst_signal_report.json --text >> /tmp/event_catalyst_signal_report.log 2>&1",
    },
    {
        "name": "market_sentiment",
        "tokens": ["market_sentiment_producer.py", "market_sentiment_report.py"],
        "why": "Hermes needs quantified volatility and risk-appetite context",
        "recommended_cron": "*/5 * * * * /usr/bin/python3 /root/market_sentiment_producer.py --output /tmp/market_sentiment_inputs.json --text >> /tmp/market_sentiment_producer.log 2>&1 && /usr/bin/python3 /root/market_sentiment_report.py --output /tmp/market_sentiment_report.json --text >> /tmp/market_sentiment_report.log 2>&1",
    },
    {
        "name": "market_index_context",
        "tokens": ["market_index_context_producer.py", "/tmp/market_index_context_inputs.json"],
        "why": "Hermes needs 20/50-day HK/US benchmark index or ETF history before treating breadth as market regime evidence",
        "recommended_cron": "*/30 * * * * /usr/bin/python3 /root/market_index_context_producer.py --output /tmp/market_index_context_inputs.json --text >> /tmp/market_index_context_producer.log 2>&1",
    },
    {
        "name": "portfolio_report",
        "tokens": ["portfolio_report.py", "/tmp/portfolio_report.json"],
        "why": "simulation and user portfolio context must be fresh",
        "recommended_cron": "*/15 * * * * /usr/bin/python3 /root/portfolio_report.py --output /tmp/portfolio_report.json --text >> /tmp/portfolio_report.log 2>&1",
    },
    {
        "name": "watchlist_diff",
        "tokens": ["watchlist_diff_report.py", "/tmp/watchlist_diff_report.json"],
        "why": "watchlist proposal drift affects v5 evidence scope",
        "recommended_cron": "5 * * * * /usr/bin/python3 /root/watchlist_diff_report.py --output /tmp/watchlist_diff_report.json --text >> /tmp/watchlist_diff_report.log 2>&1",
    },
    {
        "name": "alert_quality",
        "tokens": ["alert_quality_report.py", "/tmp/rt_alert_quality_report.json"],
        "why": "alert noise and confirmation quality must be monitored",
        "recommended_cron": "*/15 * * * * /usr/bin/python3 /root/alert_quality_report.py --output /tmp/rt_alert_quality_report.json --text >> /tmp/rt_alert_quality_report.log 2>&1",
    },
    {
        "name": "rt_signal_outcome",
        "tokens": ["rt_signal_outcome_report.py", "/tmp/rt_signal_outcome_report.json"],
        "why": "forward evidence is required before execute readiness",
        "recommended_cron": "*/30 * * * * /usr/bin/python3 /root/rt_signal_outcome_report.py --output /tmp/rt_signal_outcome_report.json --text >> /tmp/rt_signal_outcome_report.log 2>&1",
    },
    {
        "name": "rt_alert_event_store",
        "tokens": ["rt_alert_event_store.py", "/tmp/rt_alert_event_store_report.json"],
        "why": "v5 alert JSONL rows must have a durability bridge before execution can be audited",
        "recommended_cron": "*/5 * * * * /usr/bin/python3 /root/rt_alert_event_store.py --output /tmp/rt_alert_event_store_report.json --text >> /tmp/rt_alert_event_store.log 2>&1",
    },
    {
        "name": "rt_signal_outcome_event_store",
        "tokens": ["rt_signal_outcome_event_store.py", "/tmp/rt_signal_outcome_event_store_report.json"],
        "why": "forward outcome evidence should be persistable per signal for strategy review",
        "recommended_cron": "*/30 * * * * /usr/bin/python3 /root/rt_signal_outcome_event_store.py --output /tmp/rt_signal_outcome_event_store_report.json --text >> /tmp/rt_signal_outcome_event_store.log 2>&1",
    },
    {
        "name": "kline_daily_gap_repair",
        "tokens": ["kline_daily_gap_repair.py", "/tmp/kline_daily_gap_repair.json"],
        "why": "minute-fresh/daily-stale gaps must be visible before trusting outcome evidence",
        "recommended_cron": "*/30 * * * * /usr/bin/python3 /root/kline_daily_gap_repair.py --output /tmp/kline_daily_gap_repair.json --text >> /tmp/kline_daily_gap_repair.log 2>&1",
    },
    {
        "name": "universe_hygiene",
        "tokens": ["universe_hygiene_report.py", "/tmp/universe_hygiene_report.json"],
        "why": "active stock-universe stale/mapping candidates must be visible before classifying data gaps",
        "recommended_cron": "10 * * * * /usr/bin/python3 /root/universe_hygiene_report.py --output /tmp/universe_hygiene_report.json --text >> /tmp/universe_hygiene_report.log 2>&1",
    },
    {
        "name": "kline_gap_source_diagnostic",
        "tokens": ["kline_gap_source_diagnostic_report.py", "/tmp/kline_gap_source_diagnostic_report.json"],
        "why": "unresolved daily K-line gaps need source/mapping/universe classification for Hermes review",
        "recommended_cron": "15,45 * * * * /usr/bin/python3 /root/kline_gap_source_diagnostic_report.py --output /tmp/kline_gap_source_diagnostic_report.json --text >> /tmp/kline_gap_source_diagnostic_report.log 2>&1",
    },
    {
        "name": "kline_gap_alternate_provider_probe",
        "tokens": ["kline_gap_alternate_provider_probe.py", "/tmp/kline_gap_alternate_provider_probe.json"],
        "why": "unresolved daily K-line gaps need an alternate-provider comparison before source/mapping decisions",
        "recommended_cron": "20,50 * * * * /usr/bin/python3 /root/kline_gap_alternate_provider_probe.py --output /tmp/kline_gap_alternate_provider_probe.json --text >> /tmp/kline_gap_alternate_provider_probe.log 2>&1",
    },
    {
        "name": "kline_gap_alternate_provider_repair_plan",
        "tokens": ["kline_gap_alternate_provider_repair_plan.py", "/tmp/kline_gap_alternate_provider_repair_plan.json"],
        "why": "alternate-provider rows need quality-gated repair-candidate review before any manual DB repair",
        "recommended_cron": "25,55 * * * * /usr/bin/python3 /root/kline_gap_alternate_provider_repair_plan.py --output /tmp/kline_gap_alternate_provider_repair_plan.json --text >> /tmp/kline_gap_alternate_provider_repair_plan.log 2>&1",
    },
    {
        "name": "strategy_learning",
        "tokens": ["strategy_learning_report.py", "/tmp/strategy_learning_report.json"],
        "why": "Hermes judgment effect and intake coverage must be measured",
        "recommended_cron": "*/30 * * * * /usr/bin/python3 /root/strategy_learning_report.py --output /tmp/strategy_learning_report.json --text >> /tmp/strategy_learning_report.log 2>&1",
    },
    {
        "name": "simulation_performance",
        "tokens": ["simulation_performance_report.py", "/tmp/simulation_performance_report.json"],
        "why": "realized simulation PnL must gate new exposure",
        "recommended_cron": "*/30 * * * * /usr/bin/python3 /root/simulation_performance_report.py --output /tmp/simulation_performance_report.json --text >> /tmp/simulation_performance_report.log 2>&1",
    },
    {
        "name": "simulation_postmortem_audit",
        "tokens": ["simulation_postmortem_audit_report.py", "/tmp/simulation_postmortem_audit_report.json"],
        "why": "simulation loss postmortem notes must be present and safe before strategy changes",
        "recommended_cron": "*/30 * * * * /usr/bin/python3 /root/simulation_postmortem_audit_report.py --output /tmp/simulation_postmortem_audit_report.json --text >> /tmp/simulation_postmortem_audit_report.log 2>&1",
    },
    {
        "name": "simulation_postmortem_note_draft",
        "tokens": ["simulation_postmortem_note_draft_report.py", "/tmp/simulation_postmortem_note_draft_report.json"],
        "why": "Hermes/operator should get safe read-only draft notes for missing simulation postmortems",
        "recommended_cron": "*/30 * * * * /usr/bin/python3 /root/simulation_postmortem_note_draft_report.py --output /tmp/simulation_postmortem_note_draft_report.json --text >> /tmp/simulation_postmortem_note_draft_report.log 2>&1",
    },
    {
        "name": "execution_readiness",
        "tokens": ["execution_readiness_report.py", "/tmp/execution_readiness_report.json"],
        "why": "aggregate readiness must be continuously refreshed",
        "recommended_cron": "*/30 * * * * /usr/bin/python3 /root/execution_readiness_report.py --output /tmp/execution_readiness_report.json --text >> /tmp/execution_readiness_report.log 2>&1",
    },
    {
        "name": "operator_action_queue",
        "tokens": ["operator_action_queue_report.py", "/tmp/operator_action_queue_report.json"],
        "why": "Hermes/operator remediation priorities should stay current before review packets are read",
        "recommended_cron": "*/5 * * * * /usr/bin/python3 /root/operator_action_queue_report.py --output /tmp/operator_action_queue_report.json --text >> /tmp/operator_action_queue_report.log 2>&1",
    },
    {
        "name": "hermes_judgment_audit",
        "tokens": ["hermes_judgment_audit_report.py", "/tmp/hermes_judgment_audit_report.json"],
        "why": "Hermes trade judgments must stay schema-valid and auditable",
        "recommended_cron": "*/10 * * * * /usr/bin/python3 /root/hermes_judgment_audit_report.py --output /tmp/hermes_judgment_audit_report.json --text >> /tmp/hermes_judgment_audit_report.log 2>&1",
    },
    {
        "name": "hermes_judgment_event_store",
        "tokens": ["hermes_judgment_event_store.py", "/tmp/hermes_judgment_event_store_report.json"],
        "why": "Hermes trade judgments should be persistable with audit status for later review",
        "recommended_cron": "*/10 * * * * /usr/bin/python3 /root/hermes_judgment_event_store.py --output /tmp/hermes_judgment_event_store_report.json --text >> /tmp/hermes_judgment_event_store.log 2>&1",
    },
    {
        "name": "rt_order_intake_event_store",
        "tokens": ["rt_order_intake_event_store.py", "/tmp/rt_order_intake_event_store_report.json"],
        "why": "intake dry-run and processed decisions should be persistable for execution audits",
        "recommended_cron": "*/10 * * * * /usr/bin/python3 /root/rt_order_intake_event_store.py --output /tmp/rt_order_intake_event_store_report.json --text >> /tmp/rt_order_intake_event_store.log 2>&1",
    },
    {
        "name": "hermes_position_judgment_audit",
        "tokens": ["hermes_position_judgment_audit_report.py", "/tmp/hermes_position_judgment_audit_report.json"],
        "why": "user/simulation holding advice must stay advisory-only and auditable",
        "recommended_cron": "*/10 * * * * /usr/bin/python3 /root/hermes_position_judgment_audit_report.py --output /tmp/hermes_position_judgment_audit_report.json --text >> /tmp/hermes_position_judgment_audit_report.log 2>&1",
    },
    {
        "name": "hermes_review_packet",
        "tokens": ["hermes_review_packet.py", "/tmp/hermes_signal_review_packet.json"],
        "why": "Hermes needs a fresh combined review packet",
        "recommended_cron": "* * * * * /bin/bash -lc \"cd /root && [ -f /root/.quantmind_env ] && . /root/.quantmind_env; /usr/bin/python3 /root/hermes_review_packet.py --output /tmp/hermes_signal_review_packet.json >> /tmp/hermes_review_packet.log 2>&1\"",
    },
    {
        "name": "rt_alert_bridge_notify",
        "tokens": ["rt_alert_bridge.py", "RT_ALERT_EXECUTION_MODE=notify", "RT_ALERT_REMOTE=local"],
        "why": "Feishu/operator notifications must read local v5 alerts and Hermes packets without depending on server self-SSH",
        "recommended_cron": "* * * * * RT_ALERT_REMOTE=local RT_ALERT_EXECUTION_MODE=notify RT_ALERT_REQUIRE_CONFIRMED=1 /usr/bin/python3 /root/rt_alert_bridge.py >> /tmp/rt_alert_bridge.log 2>&1",
    },
]

DANGEROUS_ENABLED_PATTERNS = [
    "RT_ALERT_EXECUTION_MODE=alert-sim",
    "RT_ALERT_EXECUTION_MODE=legacy-sim",
    "rt_order_intake.py --mode execute",
    "quantmind_sim_trader.py",
    "rt_alert_bridge.py --mode execute",
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


def active_cron_lines(text):
    lines = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def load_crontab_text():
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return "", [f"crontab_read_failed:{exc}"]
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return "", [f"crontab_read_failed:{detail or result.returncode}"]
    return result.stdout, []


def job_present(lines, tokens):
    return any(all(token in line for token in tokens) for line in lines)


def dangerous_lines(lines):
    matches = []
    for line in lines:
        for pattern in DANGEROUS_ENABLED_PATTERNS:
            if pattern in line:
                matches.append({"pattern": pattern, "line": line})
    return matches


def stable_hash(payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def unsafe_install_line(line):
    text = str(line or "")
    unsafe_tokens = [
        "RT_ALERT_EXECUTION_MODE=alert-sim",
        "RT_ALERT_EXECUTION_MODE=legacy-sim",
        "RT_ORDER_EXECUTION_MODE=execute",
        "--mode execute",
        " --apply",
        " quantmind_sim_trader.py",
        "rt_alert_bridge.py --mode execute",
    ]
    return [token.strip() for token in unsafe_tokens if token in text]


def load_optional_text(path):
    if not path or not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(), None
    except Exception as exc:
        return None, str(exc)


def strip_env_value(value):
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1].strip()
    return text


def env_keys_from_file_text(text):
    keys = set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in FEISHU_REQUIRED_ENV and strip_env_value(value):
            keys.add(key)
    return keys


def active_line_with_tokens(lines, tokens):
    return [line for line in lines if all(token in line for token in tokens)]


def sent_state_file_status(name, path, raw):
    row = {
        "name": name,
        "path": path,
        "status": "MISSING",
        "record_count": 0,
        "reason": "sent_state_file_missing_first_run_ok",
    }
    if raw is None:
        return row
    try:
        payload = json.loads(raw) if str(raw).strip() else []
    except Exception as exc:
        row["status"] = "WARN"
        row["reason"] = f"sent_state_json_invalid:{exc}"
        return row
    if not isinstance(payload, list):
        row["status"] = "WARN"
        row["reason"] = "sent_state_not_json_list"
        return row
    row["status"] = "OK"
    row["record_count"] = len(payload)
    row["reason"] = None
    return row


def alert_delivery_audit(lines, env=None, env_file_text=None, sent_file_texts=None, env_file_error=None):
    env = dict(os.environ if env is None else env)
    sent_file_texts = dict(sent_file_texts or {})
    bridge_lines = active_line_with_tokens(lines, ["rt_alert_bridge.py", "RT_ALERT_EXECUTION_MODE=notify"])
    local_bridge_lines = [line for line in bridge_lines if "RT_ALERT_REMOTE=local" in line]
    feishu_lines = [line for line in bridge_lines if "RT_ALERT_SEND_FEISHU=1" in line]
    dangerous_bridge_lines = active_line_with_tokens(lines, ["rt_alert_bridge.py"])
    dangerous_bridge_lines = [
        line
        for line in dangerous_bridge_lines
        if "RT_ALERT_EXECUTION_MODE=alert-sim" in line or "RT_ALERT_EXECUTION_MODE=legacy-sim" in line
    ]

    if env_file_text is None and env_file_error is None:
        env_file_text, env_file_error = load_optional_text(env.get("FEISHU_ENV_FILE") or FEISHU_ENV_FILE)
    env_keys = {key for key in FEISHU_REQUIRED_ENV if env.get(key)}
    file_keys = env_keys_from_file_text(env_file_text)
    present_keys = sorted(env_keys | file_keys)
    missing_keys = [key for key in FEISHU_REQUIRED_ENV if key not in present_keys]

    sent_files = {
        "alert_sent": (env.get("RT_ALERT_SENT_FILE") or ALERT_SENT_FILE),
        "position_review_sent": (env.get("RT_POSITION_REVIEW_SENT_FILE") or POSITION_REVIEW_SENT_FILE),
    }
    sent_rows = []
    for name, path in sent_files.items():
        if path in sent_file_texts:
            raw = sent_file_texts[path]
        elif name in sent_file_texts:
            raw = sent_file_texts[name]
        else:
            raw, _error = load_optional_text(path)
        sent_rows.append(sent_state_file_status(name, path, raw))

    warnings = []
    recommendations = []
    if not bridge_lines:
        warnings.append("rt_alert_bridge_notify_cron_missing")
        recommendations.append("install_rt_alert_bridge_notify_cron_before_claiming_feishu_operator_delivery")
    elif not local_bridge_lines:
        warnings.append("rt_alert_bridge_notify_not_local_mode")
        recommendations.append("use_rt_alert_remote_local_to_avoid_server_self_ssh_delivery_failure")
    if dangerous_bridge_lines:
        warnings.append("dangerous_rt_alert_bridge_execution_cron_enabled")
        recommendations.append("disable_dangerous_rt_alert_bridge_execution_cron")
    if feishu_lines and missing_keys:
        warnings.append("feishu_delivery_enabled_but_credentials_missing")
        recommendations.append("configure_feishu_env_before_enabling_rt_alert_send_feishu")
    if feishu_lines and not any("/root/.quantmind_env" in line or "FEISHU_ENV_FILE" in line for line in feishu_lines):
        warnings.append("feishu_delivery_enabled_without_explicit_env_source")
        recommendations.append("source_quantmind_env_in_feishu_cron_or_set_feishu_env_file")
    if env_file_error and feishu_lines:
        warnings.append("feishu_env_file_unreadable")
        recommendations.append("fix_feishu_env_file_permissions_or_path")
    invalid_sent = [row for row in sent_rows if row.get("status") == "WARN"]
    if invalid_sent:
        warnings.append("alert_delivery_sent_state_invalid")
        recommendations.append("repair_or_rotate_invalid_rt_alert_sent_state_files")

    if dangerous_bridge_lines:
        status = "FAIL"
    elif warnings:
        status = "WARN"
    else:
        status = "OK"

    return {
        "schema": "alert_delivery_audit_v1",
        "status": status,
        "bridge_notify_present": bool(bridge_lines),
        "bridge_notify_local_mode": bool(local_bridge_lines),
        "bridge_notify_line_count": len(bridge_lines),
        "feishu_delivery_enabled": bool(feishu_lines),
        "feishu_delivery_line_count": len(feishu_lines),
        "feishu_config": {
            "required_keys": list(FEISHU_REQUIRED_ENV),
            "present_keys": present_keys,
            "missing_keys": missing_keys,
            "env_file_path": env.get("FEISHU_ENV_FILE") or FEISHU_ENV_FILE,
            "env_file_present": env_file_text is not None,
            "env_file_read_error": env_file_error,
            "values_redacted": True,
        },
        "sent_state": {
            "status": "WARN" if invalid_sent else "OK",
            "files": sent_rows,
        },
        "warnings": warnings,
        "recommendations": recommendations,
    }


def cron_installation_plan(missing):
    lines = []
    rejected = []
    for job in missing or []:
        line = job.get("recommended_cron")
        unsafe = unsafe_install_line(line)
        row = {
            "name": job.get("name"),
            "why": job.get("why"),
            "recommended_cron": line,
        }
        if unsafe:
            rejected.append({**row, "unsafe_tokens": unsafe})
        else:
            lines.append(row)

    plan_input = {
        "install_lines": lines,
        "rejected_lines": rejected,
    }
    status = "not_required"
    if rejected:
        status = "blocked_unsafe_recommended_lines"
    elif lines:
        status = "operator_review_required"
    return {
        "schema": "read_only_cron_installation_plan_v1",
        "status": status,
        "proposal_hash": stable_hash(plan_input),
        "manual_review_required": bool(lines or rejected),
        "auto_applied": False,
        "install_line_count": len(lines),
        "rejected_line_count": len(rejected),
        "install_lines": lines,
        "rejected_lines": rejected,
        "operator_contract": {
            "read_only": True,
            "does_not_edit_crontab": True,
            "submits_orders": False,
            "uses_execute_mode": False,
            "uses_apply_flags": False,
            "enables_alert_sim": False,
            "enables_legacy_sim": False,
            "requires_operator_manual_install": bool(lines),
            "requires_crontab_backup_before_install": bool(lines),
        },
        "manual_install_workflow": {
            "pre_install_commands": [
                "crontab -l > /root/crontab_backup_$(date +%Y%m%d_%H%M%S)_before_hermes_v5_read_only.txt",
                "/usr/bin/python3 /root/cron_audit_report.py --output /tmp/cron_audit_report.json --text",
            ],
            "install_method": "operator manually reviews install_lines and edits crontab; this report never edits crontab",
            "post_install_verification_commands": [
                "/usr/bin/python3 /root/cron_audit_report.py --output /tmp/cron_audit_report.json --text",
                "/usr/bin/python3 /root/readiness_refresh.py --skip-network-producers --output /tmp/readiness_refresh_report.json --text",
                "/usr/bin/python3 /root/execution_readiness_report.py --output /tmp/execution_readiness_report.json --text",
            ],
        },
        "hermes_use": [
            "Hermes may cite proposal_hash and install_lines as operator guidance for missing read-only context jobs.",
            "This plan is not proof that cron has been changed; only a later cron_audit status can prove installation.",
            "Do not approve execution because a plan exists. Execution remains gated by readiness and rt_order_intake.py.",
        ],
    }


def build_report(crontab_text=None, warnings=None, env=None, env_file_text=None, sent_file_texts=None):
    warnings = list(warnings or [])
    if crontab_text is None:
        crontab_text, load_warnings = load_crontab_text()
        warnings.extend(load_warnings)
    lines = active_cron_lines(crontab_text)
    required = []
    missing = []
    for job in REQUIRED_READ_ONLY_JOBS:
        present = job_present(lines, job["tokens"])
        row = {
            "name": job["name"],
            "present": present,
            "tokens": job["tokens"],
            "why": job["why"],
            "recommended_cron": job["recommended_cron"],
        }
        required.append(row)
        if not present:
            missing.append(row)
    dangerous = dangerous_lines(lines)
    alert_delivery = alert_delivery_audit(
        lines,
        env=env,
        env_file_text=env_file_text,
        sent_file_texts=sent_file_texts,
    )
    if dangerous:
        status = "FAIL"
    elif alert_delivery.get("status") == "FAIL":
        status = "FAIL"
    elif missing or alert_delivery.get("status") == "WARN":
        status = "WARN"
    else:
        status = "OK"
    recommendations = []
    if dangerous:
        recommendations.append("disable_dangerous_execution_cron_before_any_review")
    if missing:
        recommendations.append("install_missing_read_only_cron_jobs_from_config_hermes_v5_crontab")
    recommendations.extend(alert_delivery.get("recommendations") or [])
    if not recommendations:
        recommendations.append("cron_wiring_matches_required_read_only_contract")
    installation_plan = cron_installation_plan(missing)
    return {
        "schema": "cron_audit_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_crontab": False,
            "active_line_count": len(lines),
        },
        "summary": {
            "required_job_count": len(REQUIRED_READ_ONLY_JOBS),
            "present_required_job_count": len(required) - len(missing),
            "missing_required_job_count": len(missing),
            "dangerous_enabled_count": len(dangerous),
            "alert_delivery_status": alert_delivery.get("status"),
        },
        "required_jobs": required,
        "missing_required_jobs": missing,
        "dangerous_enabled_jobs": dangerous,
        "alert_delivery": alert_delivery,
        "installation_plan": installation_plan,
        "recommendations": recommendations,
        "warnings": warnings,
        "hermes_use": [
            "Use this to detect drift between documented read-only jobs and the actual crontab.",
            "Missing read-only jobs explain stale or missing readiness inputs; do not treat the absence as execution permission.",
            "Any dangerous enabled job must be disabled before considering simulation execution.",
            "Use alert_delivery to verify the notify bridge, optional Feishu credentials, and sent-state health without sending messages.",
        ],
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Cron audit report {payload['generated_at']} status={payload['status']}",
        (
            f"required={summary.get('required_job_count')} "
            f"present={summary.get('present_required_job_count')} "
            f"missing={summary.get('missing_required_job_count')} "
            f"dangerous={summary.get('dangerous_enabled_count')}"
        ),
    ]
    missing = [job.get("name") for job in payload.get("missing_required_jobs") or []]
    if missing:
        lines.append("Missing required read-only jobs: " + ", ".join(missing))
        plan = payload.get("installation_plan") or {}
        lines.append(
            "Installation plan: status={status} hash={hash} lines={count}".format(
                status=plan.get("status"),
                hash=plan.get("proposal_hash"),
                count=plan.get("install_line_count"),
            )
        )
        lines.append("Recommended read-only cron for missing jobs:")
        for job in payload.get("missing_required_jobs") or []:
            lines.append(f"  {job.get('name')}: {job.get('recommended_cron')}")
    dangerous = payload.get("dangerous_enabled_jobs") or []
    if dangerous:
        lines.append("Dangerous enabled jobs: " + ", ".join(row["pattern"] for row in dangerous))
    delivery = payload.get("alert_delivery") or {}
    if delivery:
        lines.append(
            "Alert delivery: status={status} bridge={bridge} local={local} feishu={feishu} sent_state={sent}".format(
                status=delivery.get("status"),
                bridge=str(delivery.get("bridge_notify_present")).lower(),
                local=str(delivery.get("bridge_notify_local_mode")).lower(),
                feishu=str(delivery.get("feishu_delivery_enabled")).lower(),
                sent=(delivery.get("sent_state") or {}).get("status"),
            )
        )
        missing_keys = ((delivery.get("feishu_config") or {}).get("missing_keys") or [])
        if delivery.get("feishu_delivery_enabled") and missing_keys:
            lines.append("Feishu missing env keys: " + ",".join(missing_keys))
        if delivery.get("warnings"):
            lines.append("Alert delivery warnings: " + ", ".join(delivery["warnings"]))
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--crontab-file", help="audit this file instead of live crontab")
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    crontab_text = None
    warnings = []
    if args.crontab_file:
        try:
            with open(args.crontab_file, encoding="utf-8") as f:
                crontab_text = f.read()
        except Exception as exc:
            crontab_text = ""
            warnings.append(f"crontab_file_read_failed:{exc}")
    payload = build_report(crontab_text=crontab_text, warnings=warnings)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
