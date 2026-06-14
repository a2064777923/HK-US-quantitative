#!/usr/bin/env python3
"""Validate and append broker/vendor fundamentals context for Hermes.

Default mode is dry-run. Use --append only after reviewing accepted/rejected
rows. This writes only the fundamentals JSONL input file; it does not submit
orders, change strategy, write alerts, edit cron, or repair market data.
"""
import argparse
import json
import os
import sys

try:
    import fundamentals_context_report as fundamentals_report
except ImportError:
    from scripts import fundamentals_context_report as fundamentals_report


OUTPUT_JSONL_FILE = os.environ.get(
    "FUNDAMENTALS_CONTEXT_INPUT_JSONL_FILE",
    "/tmp/fundamentals_context_inputs.jsonl",
)


def load_json_file(path):
    with open(path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    if isinstance(loaded, dict):
        raw_items = loaded.get("items") or loaded.get("fundamentals")
        if isinstance(raw_items, list):
            return [item for item in raw_items if isinstance(item, dict)]
        return [loaded]
    if isinstance(loaded, list):
        return [item for item in loaded if isinstance(item, dict)]
    return []


def load_jsonl_file(path):
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                items.append(item)
    return items


def raw_with_source(raw):
    item = dict(raw or {})
    if not str(item.get("source") or "").strip():
        for key in ("provider", "producer"):
            value = str(item.get(key) or "").strip()
            if value:
                item["source"] = value
                break
    return item


def normalize_item(raw):
    return fundamentals_report.normalize_item(raw_with_source(raw))


def item_key(item):
    normalized = normalize_item(item)
    return (
        normalized.get("market"),
        normalized.get("symbol"),
        normalized.get("as_of"),
        normalized.get("source"),
        normalized.get("provider_symbol"),
    )


def load_existing_keys(path):
    if not path or not os.path.exists(path):
        return set()
    keys = set()
    for item in load_jsonl_file(path):
        keys.add(item_key(item))
    return keys


def raw_metric_value(item, field):
    if field == "pe_ttm":
        return item.get("pe_ttm") if item.get("pe_ttm") not in (None, "") else item.get("pe")
    if field == "roe_pct":
        return item.get("roe_pct") if item.get("roe_pct") not in (None, "") else item.get("roe")
    return item.get(field)


def validate_item(item):
    normalized = normalize_item(item)
    reasons = []
    if not normalized["symbol"]:
        reasons.append("missing_symbol")
    if not normalized["as_of"]:
        reasons.append("missing_as_of")
    elif fundamentals_report.parse_timestamp(normalized["as_of"]) is None:
        reasons.append("invalid_as_of")
    if not normalized["source"] or normalized["source"] == "unknown":
        reasons.append("missing_source")

    metric_count = 0
    for field in fundamentals_report.METRIC_FIELDS:
        raw_value = raw_metric_value(item, field)
        if raw_value in (None, ""):
            continue
        metric_count += 1
        if fundamentals_report.as_float(raw_value) is None:
            reasons.append(f"invalid_metric:{field}")
    if metric_count == 0:
        reasons.append("missing_all_metrics")
    return reasons, normalized


def append_jsonl(path, items):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def build_ingest(items, output_file=OUTPUT_JSONL_FILE, allow_invalid=False):
    existing = load_existing_keys(output_file)
    accepted = []
    rejected = []
    duplicate_count = 0
    seen_batch = set()
    for raw in items:
        reasons, normalized = validate_item(raw)
        key = item_key(raw)
        if key in existing or key in seen_batch:
            duplicate_count += 1
            continue
        seen_batch.add(key)
        if reasons and not allow_invalid:
            rejected.append({"item": normalized, "reasons": reasons})
            continue
        if reasons:
            normalized["ingest_warnings"] = reasons
        accepted.append(normalized)
    return {
        "schema": "fundamentals_context_ingest_v1",
        "output_file": output_file,
        "input_count": len(items),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "duplicate_count": duplicate_count,
        "accepted": accepted,
        "rejected": rejected,
        "source": {
            "writes_fundamentals_context_jsonl": True,
            "default_dry_run": True,
            "submits_orders": False,
            "changes_strategy": False,
            "changes_alert_queue": False,
            "changes_crontab": False,
            "repairs_data": False,
        },
    }


def build_text_report(payload):
    lines = [
        (
            f"Fundamentals context ingest accepted={payload['accepted_count']} "
            f"rejected={payload['rejected_count']} duplicates={payload['duplicate_count']} "
            f"dry_run={payload.get('dry_run', True)} appended={payload.get('appended', False)} "
            f"output={payload['output_file']}"
        )
    ]
    if payload["rejected"]:
        reason_counts = {}
        for row in payload["rejected"]:
            for reason in row.get("reasons") or []:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        lines.append("Rejected reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(reason_counts.items())))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--item-json", action="append", default=[], help="one fundamentals JSON object")
    parser.add_argument("--input-file", action="append", default=[], help="JSON object/list or {items:[...]}")
    parser.add_argument("--input-jsonl-file", action="append", default=[], help="JSONL input file")
    parser.add_argument("--output-jsonl-file", default=OUTPUT_JSONL_FILE)
    parser.add_argument("--allow-invalid", action="store_true", help="append invalid items with ingest_warnings")
    parser.add_argument("--append", action="store_true", help="append accepted rows; default is dry-run validation")
    parser.add_argument("--dry-run", action="store_true", help="force validation-only mode")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    items = []
    for raw in args.item_json:
        items.append(json.loads(raw))
    for path in args.input_file:
        items.extend(load_json_file(path))
    for path in args.input_jsonl_file:
        items.extend(load_jsonl_file(path))
    payload = build_ingest(items, output_file=args.output_jsonl_file, allow_invalid=args.allow_invalid)
    should_append = bool(args.append and not args.dry_run)
    if should_append and payload["accepted"]:
        append_jsonl(args.output_jsonl_file, payload["accepted"])
    payload["dry_run"] = not should_append
    payload["appended"] = bool(should_append and payload["accepted"])
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.text:
        print(build_text_report(payload))
    else:
        print(build_text_report(payload))
        print("\n--- JSON ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2 if payload["rejected_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
