#!/usr/bin/env python3
"""Read-only discovery for trusted Hermes context source wiring.

This report answers a different question from trusted_source_preflight.py:
preflight validates payload contents, while discovery checks whether the server
appears wired to usable source adapters at all. It never prints secret values,
writes ingest files, edits cron, changes strategy, repairs data, or submits
orders.
"""
import argparse
import json
import os
import socket
from datetime import datetime


REPORT_FILE = os.environ.get("TRUSTED_SOURCE_DISCOVERY_REPORT_FILE", "/tmp/trusted_source_discovery_report.json")
DEFAULT_INFOHUB_URL = os.environ.get("EXTERNAL_CONTEXT_INFOHUB_URL", "http://127.0.0.1:8899")
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("TRUSTED_SOURCE_DISCOVERY_TIMEOUT_SECONDS", "1.5"))

DEFAULT_FILES = {
    "external_json": os.environ.get("EXTERNAL_MARKET_CONTEXT_INPUT_FILE", "/tmp/external_market_context_inputs.json"),
    "external_jsonl": os.environ.get("EXTERNAL_MARKET_CONTEXT_INPUT_JSONL_FILE", "/tmp/external_market_context_inputs.jsonl"),
    "sentiment_json": os.environ.get("MARKET_SENTIMENT_INPUT_FILE", "/tmp/market_sentiment_inputs.json"),
    "sentiment_jsonl": os.environ.get("MARKET_SENTIMENT_INPUT_JSONL_FILE", "/tmp/market_sentiment_inputs.jsonl"),
    "fundamentals_json": os.environ.get("FUNDAMENTALS_CONTEXT_INPUT_FILE", "/tmp/fundamentals_context_inputs.json"),
    "fundamentals_jsonl": os.environ.get("FUNDAMENTALS_CONTEXT_INPUT_JSONL_FILE", "/tmp/fundamentals_context_inputs.jsonl"),
}

ENV_GROUPS = {
    "wudao": (
        "WUDAO_MCP_URL",
        "WUDAO_MCP_ENDPOINT",
        "WUDAO_API_BASE",
        "WUDAO_API_KEY",
        "WUDAO_TOKEN",
        "CLAUDE_MCP_WUDAO_URL",
    ),
    "infohub": (
        "EXTERNAL_CONTEXT_INFOHUB_URL",
        "INFOHUB_URL",
        "INFO_HUB_URL",
    ),
    "broker": (
        "BROKER_API_BASE",
        "BROKER_API_KEY",
        "RT_ORDER_US_BROKER",
        "ALPACA_TRADING_BASE_URL",
        "ALPACA_BASE_URL",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "ALPACA_API_KEY_ID",
        "ALPACA_API_SECRET_KEY",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "ALPACA_KEY_ID",
        "FUTU_HOST",
        "FUTU_PORT",
        "IBKR_HOST",
        "IBKR_PORT",
        "QM_API_USER",
        "QM_API_PASSWORD",
    ),
    "official_macro": (
        "OFFICIAL_MACRO_API_BASE",
        "FRED_API_KEY",
        "HKMA_API_BASE",
        "EXCHANGE_CALENDAR_API_BASE",
    ),
    "fundamentals_vendor": (
        "FUNDAMENTALS_API_BASE",
        "FUNDAMENTALS_API_KEY",
        "FACTSET_API_KEY",
        "REFINITIV_API_KEY",
        "MORNINGSTAR_API_KEY",
    ),
}

CAPABILITIES = {
    "trusted_event_context": ("wudao", "broker", "official_macro"),
    "capital_flow_context": ("wudao", "broker"),
    "market_sentiment_context": ("broker", "official_macro"),
    "full_fundamentals_context": ("fundamentals_vendor", "broker", "wudao"),
    "infohub_public_context": ("infohub",),
}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
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


def redact_env(env=None):
    env = env if env is not None else os.environ
    groups = {}
    for group, names in ENV_GROUPS.items():
        present = []
        missing = []
        for name in names:
            value = env.get(name)
            if value not in (None, ""):
                present.append(name)
            else:
                missing.append(name)
        groups[group] = {
            "configured": bool(present),
            "present_env_keys": sorted(present),
            "missing_env_keys": sorted(missing),
            "secret_values_redacted": True,
        }
    return groups


def parse_url_host_port(url):
    text = str(url or "").strip()
    if not text:
        return None, None
    if "://" in text:
        text = text.split("://", 1)[1]
    host_port = text.split("/", 1)[0]
    if "@" in host_port:
        host_port = host_port.rsplit("@", 1)[1]
    if ":" in host_port:
        host, port = host_port.rsplit(":", 1)
        try:
            return host, int(port)
        except ValueError:
            return host, None
    return host_port, 80


def probe_tcp(url, timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
    host, port = parse_url_host_port(url)
    if not host or not port:
        return {
            "url": url,
            "configured": bool(url),
            "reachable": False,
            "reason": "host_or_port_missing",
        }
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            reachable = True
            reason = "tcp_connect_ok"
    except Exception as exc:
        reachable = False
        reason = f"tcp_connect_failed:{type(exc).__name__}"
    return {
        "url": url,
        "host": host,
        "port": port,
        "configured": True,
        "reachable": reachable,
        "reason": reason,
    }


def file_summary(path):
    row = {
        "path": path,
        "exists": bool(path and os.path.exists(path)),
    }
    if not row["exists"]:
        return row
    try:
        row["size_bytes"] = os.path.getsize(path)
        row["modified_at"] = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
        if path.endswith(".json"):
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                row["schema"] = loaded.get("schema")
                for key in ("items", "contexts", "indicators", "fundamentals"):
                    if isinstance(loaded.get(key), list):
                        row["item_count"] = len(loaded[key])
                        break
                row["warnings_count"] = len(loaded.get("warnings") or []) if isinstance(loaded.get("warnings"), list) else 0
            elif isinstance(loaded, list):
                row["item_count"] = len(loaded)
        elif path.endswith(".jsonl"):
            count = 0
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        count += 1
            row["line_count"] = count
    except Exception as exc:
        row["read_error"] = str(exc)[:240]
    return row


def file_discovery(files=None):
    files = files or DEFAULT_FILES
    return {name: file_summary(path) for name, path in files.items()}


def provider_status(provider, env_groups, endpoint_probes):
    configured = bool((env_groups.get(provider) or {}).get("configured"))
    reachable = any(probe.get("reachable") for probe in endpoint_probes.get(provider, []))
    if configured and reachable:
        status = "READY_TO_VALIDATE_PAYLOAD"
    elif configured:
        status = "CONFIGURED_UNVERIFIED"
    elif reachable:
        status = "DISCOVERED_ENDPOINT_ONLY"
    else:
        status = "MISSING"
    return {
        "provider": provider,
        "status": status,
        "configured": configured,
        "reachable": reachable,
        "env": env_groups.get(provider) or {},
        "endpoint_probes": endpoint_probes.get(provider, []),
    }


def capability_summary(providers):
    by_provider = {row["provider"]: row for row in providers}
    rows = []
    for capability, provider_names in CAPABILITIES.items():
        candidates = [by_provider[name] for name in provider_names if name in by_provider]
        configured = [row["provider"] for row in candidates if row.get("configured") or row.get("reachable")]
        ready = [row["provider"] for row in candidates if row.get("status") == "READY_TO_VALIDATE_PAYLOAD"]
        if ready:
            status = "READY_TO_VALIDATE_PAYLOAD"
        elif configured:
            status = "CONFIGURED_UNVERIFIED"
        else:
            status = "MISSING"
        rows.append(
            {
                "capability": capability,
                "status": status,
                "candidate_providers": list(provider_names),
                "configured_or_reachable_providers": configured,
                "ready_providers": ready,
            }
        )
    return rows


def classify_overall(capabilities):
    statuses = [row["status"] for row in capabilities]
    critical = {
        row["capability"]: row["status"]
        for row in capabilities
        if row["capability"]
        in ("trusted_event_context", "capital_flow_context", "market_sentiment_context", "full_fundamentals_context")
    }
    if any(status == "READY_TO_VALIDATE_PAYLOAD" for status in critical.values()) and all(
        status != "MISSING" for status in critical.values()
    ):
        return "OK"
    if any(status != "MISSING" for status in statuses):
        return "WARN"
    return "MISSING"


def recommendations(capabilities):
    recs = []
    by_capability = {row["capability"]: row for row in capabilities}
    if by_capability.get("trusted_event_context", {}).get("status") == "MISSING":
        recs.append("configure_wudao_broker_or_official_event_source")
    if by_capability.get("capital_flow_context", {}).get("status") == "MISSING":
        recs.append("configure_northbound_southbound_or_broker_flow_source")
    if by_capability.get("market_sentiment_context", {}).get("status") == "MISSING":
        recs.append("configure_broker_or_official_market_sentiment_source")
    if by_capability.get("full_fundamentals_context", {}).get("status") == "MISSING":
        recs.append("configure_broker_vendor_official_or_wudao_fundamentals_source")
    if any(row["status"] == "CONFIGURED_UNVERIFIED" for row in capabilities):
        recs.append("run_dry_run_export_and_trusted_source_preflight_for_configured_sources")
    if by_capability.get("infohub_public_context", {}).get("status") != "MISSING":
        recs.append("treat_infohub_public_context_as_fallback_until_trusted_provider_payloads_pass_preflight")
    if not recs:
        recs.append("trusted_source_discovery_clean")
    return sorted(set(recs))


def build_report(
    env=None,
    files=None,
    infohub_url=None,
    probe_tcp_func=probe_tcp,
    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
):
    env = env if env is not None else os.environ
    infohub_url = infohub_url if infohub_url is not None else env.get("EXTERNAL_CONTEXT_INFOHUB_URL", DEFAULT_INFOHUB_URL)
    env_groups = redact_env(env)
    endpoint_probes = {
        "infohub": [probe_tcp_func(infohub_url, timeout_seconds=timeout_seconds)] if infohub_url else [],
        "wudao": [],
        "broker": [],
        "official_macro": [],
        "fundamentals_vendor": [],
    }
    for provider, url_keys in {
        "wudao": ("WUDAO_MCP_URL", "WUDAO_MCP_ENDPOINT", "WUDAO_API_BASE", "CLAUDE_MCP_WUDAO_URL"),
        "broker": (
            "BROKER_API_BASE",
            "FUTU_HOST",
            "IBKR_HOST",
            "ALPACA_TRADING_BASE_URL",
            "ALPACA_BASE_URL",
        ),
        "official_macro": ("OFFICIAL_MACRO_API_BASE", "HKMA_API_BASE", "EXCHANGE_CALENDAR_API_BASE"),
        "fundamentals_vendor": ("FUNDAMENTALS_API_BASE",),
    }.items():
        for key in url_keys:
            value = env.get(key)
            if value:
                endpoint_probes[provider].append(probe_tcp_func(value, timeout_seconds=timeout_seconds))

    providers = [
        provider_status(provider, env_groups, endpoint_probes)
        for provider in ("wudao", "infohub", "broker", "official_macro", "fundamentals_vendor")
    ]
    capabilities = capability_summary(providers)
    status = classify_overall(capabilities)
    return {
        "schema": "trusted_source_discovery_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_strategy": False,
            "changes_alert_queue": False,
            "changes_crontab": False,
            "writes_ingest_files": False,
            "repairs_data": False,
            "prints_secret_values": False,
            "timeout_seconds": timeout_seconds,
        },
        "summary": {
            "provider_count": len(providers),
            "configured_provider_count": len([row for row in providers if row.get("configured")]),
            "reachable_provider_count": len([row for row in providers if row.get("reachable")]),
            "capability_count": len(capabilities),
            "missing_capability_count": len([row for row in capabilities if row["status"] == "MISSING"]),
        },
        "providers": providers,
        "capabilities": capabilities,
        "input_files": file_discovery(files),
        "recommendations": recommendations(capabilities),
        "hermes_use": [
            "Use discovery to understand which source adapters appear configured before asking operators for payloads.",
            "Discovery does not prove payload quality; trusted_source_preflight and downstream reports remain authoritative.",
            "Configured or reachable sources must still export JSON/JSONL payloads and pass preflight before Hermes can cite them as trusted evidence.",
        ],
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Trusted source discovery {payload['generated_at']} status={payload['status']}",
        (
            f"providers={summary.get('provider_count')} configured={summary.get('configured_provider_count')} "
            f"reachable={summary.get('reachable_provider_count')} missing_capabilities={summary.get('missing_capability_count')}"
        ),
    ]
    for capability in payload.get("capabilities") or []:
        lines.append(
            "  {status} {capability}: providers={providers}".format(
                status=capability.get("status"),
                capability=capability.get("capability"),
                providers=",".join(capability.get("configured_or_reachable_providers") or []),
            )
        )
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--infohub-url", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(infohub_url=args.infohub_url, timeout_seconds=args.timeout_seconds)
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
    return 0 if payload["status"] in ("OK", "WARN", "MISSING") else 2


if __name__ == "__main__":
    raise SystemExit(main())
