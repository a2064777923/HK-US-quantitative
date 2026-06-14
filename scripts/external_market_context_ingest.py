#!/usr/bin/env python3
"""Append validated external news/macro/event context items for Hermes."""
import argparse
import json
import os
import sys

try:
    import external_market_context_report as context_report
except ImportError:
    from scripts import external_market_context_report as context_report


OUTPUT_JSONL_FILE = os.environ.get(
    "EXTERNAL_MARKET_CONTEXT_INPUT_JSONL_FILE",
    "/tmp/external_market_context_inputs.jsonl",
)


def load_json_file(path):
    with open(path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    if isinstance(loaded, dict):
        raw_items = loaded.get("items") or loaded.get("contexts")
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


def item_key(item):
    normalized = context_report.normalize_item(item)
    return (
        normalized.get("id"),
        normalized.get("published_at"),
        normalized.get("title"),
    )


def load_existing_keys(path):
    if not path or not os.path.exists(path):
        return set()
    keys = set()
    for item in load_jsonl_file(path):
        keys.add(item_key(item))
    return keys


def validate_item(item):
    normalized = context_report.normalize_item(item)
    reasons = []
    if not normalized["title"]:
        reasons.append("missing_title")
    if not normalized["published_at"]:
        reasons.append("missing_published_at")
    elif context_report.parse_timestamp(normalized["published_at"]) is None:
        reasons.append("invalid_published_at")
    if normalized["category"] not in context_report.VALID_CATEGORIES:
        reasons.append("invalid_category")
    if normalized["sentiment"] not in context_report.VALID_SENTIMENTS:
        reasons.append("invalid_sentiment")
    if normalized["impact_score"] < 0 or normalized["impact_score"] > 1:
        reasons.append("impact_score_out_of_range")
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
        "schema": "external_market_context_ingest_v1",
        "output_file": output_file,
        "input_count": len(items),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "duplicate_count": duplicate_count,
        "accepted": accepted,
        "rejected": rejected,
        "source": {
            "writes_external_context_jsonl": True,
            "submits_orders": False,
            "changes_strategy": False,
            "changes_alert_queue": False,
        },
    }


def build_text_report(payload):
    lines = [
        (
            f"External context ingest accepted={payload['accepted_count']} "
            f"rejected={payload['rejected_count']} duplicates={payload['duplicate_count']} "
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
    parser.add_argument("--item-json", action="append", default=[], help="one external context JSON object")
    parser.add_argument("--input-file", action="append", default=[], help="JSON object/list or {items:[...]}")
    parser.add_argument("--input-jsonl-file", action="append", default=[], help="JSONL input file")
    parser.add_argument("--output-jsonl-file", default=OUTPUT_JSONL_FILE)
    parser.add_argument("--allow-invalid", action="store_true", help="append invalid items with ingest_warnings")
    parser.add_argument("--dry-run", action="store_true", help="validate and report without writing")
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
    if not args.dry_run and payload["accepted"]:
        append_jsonl(args.output_jsonl_file, payload["accepted"])
    payload["dry_run"] = bool(args.dry_run)
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
