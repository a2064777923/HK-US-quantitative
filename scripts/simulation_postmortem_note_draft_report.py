#!/usr/bin/env python3
"""Read-only draft helper for simulation postmortem notes."""
import argparse
import json
import os
from datetime import datetime

try:
    import simulation_postmortem_audit_report as audit
except ImportError:
    from scripts import simulation_postmortem_audit_report as audit


SIMULATION_PERFORMANCE_FILE = os.environ.get(
    "SIMULATION_PERFORMANCE_REPORT_FILE",
    "/tmp/simulation_performance_report.json",
)
SIMULATION_POSTMORTEM_AUDIT_FILE = os.environ.get(
    "SIMULATION_POSTMORTEM_AUDIT_REPORT_FILE",
    "/tmp/simulation_postmortem_audit_report.json",
)
MARKET_CONTEXT_FILE = os.environ.get("MARKET_CONTEXT_REPORT_FILE", "/tmp/market_context_report.json")
INTRADAY_CONTEXT_FILE = os.environ.get("INTRADAY_CONTEXT_REPORT_FILE", "/tmp/intraday_context_report.json")
EXTERNAL_MARKET_CONTEXT_FILE = os.environ.get(
    "EXTERNAL_MARKET_CONTEXT_REPORT_FILE",
    "/tmp/external_market_context_report.json",
)
EVENT_CATALYST_REPORT_FILE = os.environ.get("EVENT_CATALYST_REPORT_FILE", "/tmp/event_catalyst_report.json")
MARKET_SENTIMENT_REPORT_FILE = os.environ.get("MARKET_SENTIMENT_REPORT_FILE", "/tmp/market_sentiment_report.json")
FUNDAMENTALS_CONTEXT_FILE = os.environ.get(
    "FUNDAMENTALS_CONTEXT_REPORT_FILE",
    "/tmp/fundamentals_context_report.json",
)
SOURCE_RELIABILITY_FILE = os.environ.get("SOURCE_RELIABILITY_REPORT_FILE", "/tmp/source_reliability_report.json")
NOTE_FILE = os.environ.get("SIMULATION_POSTMORTEM_NOTE_FILE", "/tmp/simulation_postmortem_notes.jsonl")
REPORT_FILE = os.environ.get(
    "SIMULATION_POSTMORTEM_NOTE_DRAFT_REPORT_FILE",
    "/tmp/simulation_postmortem_note_draft_report.json",
)


FAILURE_CATEGORY_HINTS = {
    "closed_trade": "entry_timing|signal_quality|stop_exit_policy|position_sizing|stale_data_source_issue|event_or_news_surprise|fundamentals_surprise|market_regime_shift|execution_quality|portfolio_risk_management|other",
    "open_position": "portfolio_risk_management|stop_exit_policy|market_regime_shift|event_or_news_surprise|fundamentals_surprise|position_sizing|other",
}


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


def load_json_file(path, default=None):
    default = {} if default is None else default
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else default
    except Exception:
        return default


def safe_dict(value):
    return value if isinstance(value, dict) else {}


def safe_list(value):
    return value if isinstance(value, list) else []


def normalized_symbol(value):
    return audit.normalized_symbol(value)


def portfolio_id(performance):
    summary = safe_dict(performance.get("summary"))
    if summary.get("portfolio_id") is not None:
        return summary.get("portfolio_id")
    return 8


def context_statuses(contexts):
    market_context = safe_dict(contexts.get("market_context"))
    intraday_context = safe_dict(contexts.get("intraday_context"))
    external_context = safe_dict(contexts.get("external_market_context"))
    event_catalysts = safe_dict(contexts.get("event_catalysts"))
    sentiment = safe_dict(contexts.get("market_sentiment"))
    fundamentals = safe_dict(contexts.get("fundamentals_context"))
    source = safe_dict(contexts.get("source_reliability"))
    return {
        "market_context_status": market_context.get("status") or "UNKNOWN",
        "intraday_context_status": intraday_context.get("status") or "UNKNOWN",
        "external_market_context_status": external_context.get("status") or "UNKNOWN",
        "event_catalyst_status": event_catalysts.get("status") or "UNKNOWN",
        "market_sentiment_status": sentiment.get("status") or "UNKNOWN",
        "fundamentals_context_status": fundamentals.get("status") or "UNKNOWN",
        "source_reliability_status": source.get("status") or "UNKNOWN",
    }


def latest_symbol_context_ids(symbol, contexts):
    symbol = normalized_symbol(symbol)
    ids = []
    for source_name, key in (
        ("external_market_context", "items"),
        ("event_catalysts", "items"),
        ("market_sentiment", "indicators"),
    ):
        payload = safe_dict(contexts.get(source_name))
        for row in safe_list(payload.get(key)):
            if not isinstance(row, dict):
                continue
            row_symbols = []
            for field in ("symbol", "ticker", "provider_symbol"):
                if row.get(field):
                    row_symbols.append(row.get(field))
            for field in ("symbols", "tickers", "related_symbols", "matched_symbols", "watchlist_symbols"):
                value = row.get(field)
                if isinstance(value, list):
                    row_symbols.extend(value)
                elif value:
                    row_symbols.append(value)
            normalized = {
                normalized_symbol(item)
                for item in row_symbols
                if normalized_symbol(item)
            }
            if symbol and symbol in normalized:
                ids.append(
                    {
                        "source": source_name,
                        "id": row.get("id") or row.get("url") or row.get("title"),
                        "sentiment": row.get("sentiment"),
                        "impact_score": row.get("impact_score") or row.get("score"),
                    }
                )
    return ids[:10]


def target_evidence_summary(target):
    evidence = safe_dict(target.get("evidence"))
    keys = (
        "symbol",
        "name",
        "market",
        "closed_at",
        "entry_trade_ids",
        "entry_order_ids",
        "exit_trade_id",
        "exit_order_id",
        "closed_trade_signal_traceability",
        "pnl_hkd_est",
        "unrealized_pnl_hkd",
        "unrealized_pnl_pct",
        "win_rate_pct",
        "closed_trade_count",
        "recommendation",
        "recommendation_reasons",
        "signal_side",
    )
    return {key: evidence.get(key) for key in keys if key in evidence}


def first_value(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def required_fields(performance, postmortem_audit):
    contract = safe_dict(postmortem_audit.get("note_contract"))
    fields = contract.get("required_fields")
    if isinstance(fields, list) and fields:
        return audit.unique_fields(fields)
    return audit.required_note_fields(performance)


def field_value(field, target, performance, target_type):
    evidence = safe_dict(target.get("evidence"))
    summary = safe_dict(performance.get("summary"))
    traceability = safe_dict(summary.get("closed_trade_signal_traceability"))
    if field == "entry_order_id":
        return first_value(evidence.get("entry_order_ids")) or evidence.get("entry_order_id") or "unknown_legacy_or_external"
    if field == "signal_lineage_status":
        return traceability.get("status") or "UNKNOWN"
    if field == "next_evidence_required":
        return "lineage_qualified_v5_closed_trade_sample_and_forward_outcome_recovery"
    if field == "closed_at":
        return evidence.get("closed_at") or ("open_position" if target_type == "open_position" else "<replace: closed trade date/time>")
    return None


def apply_required_field_placeholders(draft, target, performance, postmortem_audit, target_type):
    for field in required_fields(performance, postmortem_audit):
        if field in draft:
            continue
        value = field_value(field, target, performance, target_type)
        draft[field] = value if value not in (None, "") else f"<required: {field}>"
    return draft


def draft_object(target, performance, contexts, generated_at):
    target_type = audit.canonical_target_type(target.get("target_type"))
    symbol = normalized_symbol(target.get("symbol"))
    statuses = context_statuses(contexts)
    event_ids = latest_symbol_context_ids(symbol, contexts)
    draft = {
        "schema": "simulation_trade_postmortem_note_v1",
        "draft_only": True,
        "portfolio_id": portfolio_id(performance),
        "symbol": symbol,
        "target_type": target_type,
        "reviewed_at": "<replace: reviewed ISO datetime>",
        "reviewer": "hermes",
        "read_only": True,
        "submits_orders": False,
        "changes_strategy": False,
        "changes_portfolio": False,
        "auto_apply": False,
        "closed_at": "<replace: closed trade date/time or open_position>",
        "entry_signal_id_or_trade_id": "<replace: trade id, signal id, or explicit unknown>",
        "exit_reason": "open_position_not_closed" if target_type == "open_position" else "<replace: stop_loss|take_profit|manual_exit|unknown>",
        "failure_category": f"<replace one: {FAILURE_CATEGORY_HINTS.get(target_type, FAILURE_CATEGORY_HINTS['closed_trade'])}>",
        "market_context_status": statuses["market_context_status"],
        "intraday_context_status": statuses["intraday_context_status"],
        "event_or_news_context_ids": [row["id"] for row in event_ids if row.get("id")],
        "fundamentals_context_status": statuses["fundamentals_context_status"],
        "source_reliability_status": statuses["source_reliability_status"],
        "lesson": "<replace: concrete lesson from evidence and context>",
        "proposed_change": "none",
        "promotion_gate": "manual_and_hash_confirmed_before_strategy_or_watchlist_change",
        "draft_context": {
            "schema": "simulation_postmortem_note_draft_context_v1",
            "target_id": target.get("target_id"),
            "target_reason": target.get("reason"),
            "generated_at": generated_at,
            "simulation_performance_status": performance.get("status") or "MISSING",
            "performance_summary": safe_dict(performance.get("summary")),
            "target_evidence": target_evidence_summary(target),
            "context_statuses": statuses,
            "matched_context_ids": event_ids,
            "operator_reminder": (
                "Replace all placeholders and remove draft_only before appending to the JSONL note file."
            ),
        },
    }
    return draft


def missing_targets(postmortem_audit, performance):
    targets = safe_list(postmortem_audit.get("missing_required_targets"))
    if targets:
        return targets
    return audit.required_targets(performance)


def build_report(
    simulation_performance=None,
    simulation_postmortem_audit=None,
    market_context=None,
    intraday_context=None,
    external_market_context=None,
    event_catalysts=None,
    market_sentiment=None,
    fundamentals_context=None,
    source_reliability=None,
):
    performance = safe_dict(simulation_performance)
    postmortem_audit = safe_dict(simulation_postmortem_audit)
    contexts = {
        "market_context": safe_dict(market_context),
        "intraday_context": safe_dict(intraday_context),
        "external_market_context": safe_dict(external_market_context),
        "event_catalysts": safe_dict(event_catalysts),
        "market_sentiment": safe_dict(market_sentiment),
        "fundamentals_context": safe_dict(fundamentals_context),
        "source_reliability": safe_dict(source_reliability),
    }
    generated_at = now_iso()
    targets = missing_targets(postmortem_audit, performance)
    drafts = [
        apply_required_field_placeholders(
            draft_object(target, performance, contexts, generated_at),
            target,
            performance,
            postmortem_audit,
            audit.canonical_target_type(target.get("target_type")),
        )
        for target in targets
    ]
    status = "ACTION_REQUIRED" if drafts else "OK"
    return {
        "schema": "simulation_postmortem_note_draft_report_v1",
        "generated_at": generated_at,
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_strategy": False,
            "changes_portfolio": False,
            "writes_note_file": False,
            "note_file": NOTE_FILE,
            "simulation_performance_file": SIMULATION_PERFORMANCE_FILE,
            "simulation_postmortem_audit_file": SIMULATION_POSTMORTEM_AUDIT_FILE,
        },
        "summary": {
            "draft_count": len(drafts),
            "target_count": len(targets),
            "simulation_performance_status": performance.get("status") or "MISSING",
            "simulation_postmortem_audit_status": postmortem_audit.get("status") or "MISSING",
        },
        "append_instructions": {
            "note_file": NOTE_FILE,
            "manual_only": True,
            "remove_draft_only_before_append": True,
            "must_replace_all_placeholders": True,
            "post_append_verification_command": (
                "/usr/bin/python3 /root/simulation_postmortem_audit_report.py "
                "--output /tmp/simulation_postmortem_audit_report.json --text"
            ),
            "forbidden_effects": [
                "Do not submit orders.",
                "Do not change portfolio state.",
                "Do not change strategy/watchlist/config from a draft.",
                "Do not append a draft object without replacing placeholders and removing draft_only.",
            ],
        },
        "drafts": drafts,
        "recommendations": (
            [f"complete_and_append_simulation_postmortem_notes:{len(drafts)}"]
            if drafts
            else ["simulation_postmortem_note_drafts_not_required"]
        ),
        "hermes_use": [
            "Use draft_jsonl_object as a starting point only.",
            "Replace placeholders with actual reasoning, remove draft_only, append one JSON object per line, then rerun the audit.",
            "Draft generation is read-only and is not a strategy promotion or trade decision.",
        ],
    }


def build_report_from_files(args):
    return build_report(
        simulation_performance=load_json_file(args.simulation_performance_file),
        simulation_postmortem_audit=load_json_file(args.simulation_postmortem_audit_file),
        market_context=load_json_file(args.market_context_file),
        intraday_context=load_json_file(args.intraday_context_file),
        external_market_context=load_json_file(args.external_market_context_file),
        event_catalysts=load_json_file(args.event_catalyst_file),
        market_sentiment=load_json_file(args.market_sentiment_file),
        fundamentals_context=load_json_file(args.fundamentals_context_file),
        source_reliability=load_json_file(args.source_reliability_file),
    )


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Simulation postmortem note drafts {payload['generated_at']} status={payload['status']}",
        (
            f"drafts={summary.get('draft_count')} "
            f"performance={summary.get('simulation_performance_status')} "
            f"audit={summary.get('simulation_postmortem_audit_status')}"
        ),
    ]
    for draft in payload.get("drafts", [])[:8]:
        context = draft.get("draft_context") or {}
        lines.append(
            f"  {context.get('target_id')} reason={context.get('target_reason')} "
            f"symbol={draft.get('symbol')} target_type={draft.get('target_type')}"
        )
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulation-performance-file", default=SIMULATION_PERFORMANCE_FILE)
    parser.add_argument("--simulation-postmortem-audit-file", default=SIMULATION_POSTMORTEM_AUDIT_FILE)
    parser.add_argument("--market-context-file", default=MARKET_CONTEXT_FILE)
    parser.add_argument("--intraday-context-file", default=INTRADAY_CONTEXT_FILE)
    parser.add_argument("--external-market-context-file", default=EXTERNAL_MARKET_CONTEXT_FILE)
    parser.add_argument("--event-catalyst-file", default=EVENT_CATALYST_REPORT_FILE)
    parser.add_argument("--market-sentiment-file", default=MARKET_SENTIMENT_REPORT_FILE)
    parser.add_argument("--fundamentals-context-file", default=FUNDAMENTALS_CONTEXT_FILE)
    parser.add_argument("--source-reliability-file", default=SOURCE_RELIABILITY_FILE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report_from_files(args)
    if args.output:
        save_json_atomic(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.text:
        print(build_text_report(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(build_text_report(payload))


if __name__ == "__main__":
    main()
