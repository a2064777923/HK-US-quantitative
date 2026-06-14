#!/usr/bin/env python3
"""Append validated market sentiment indicators for Hermes."""
import argparse
import json
import os
import sys

try:
    import market_sentiment_report as sentiment_report
except ImportError:
    from scripts import market_sentiment_report as sentiment_report


OUTPUT_JSONL_FILE = os.environ.get(
    "MARKET_SENTIMENT_INPUT_JSONL_FILE",
    "/tmp/market_sentiment_inputs.jsonl",
)


def load_json_file(path):
    with open(path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    if isinstance(loaded, dict):
        raw_items = loaded.get("indicators") or loaded.get("items")
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


def indicator_key(item):
    normalized = sentiment_report.normalize_indicator(item)
    return (
        normalized.get("id"),
        normalized.get("indicator_type"),
        normalized.get("observed_at"),
        tuple(normalized.get("markets") or []),
    )


def load_existing_keys(path):
    if not path or not os.path.exists(path):
        return set()
    keys = set()
    for item in load_jsonl_file(path):
        keys.add(indicator_key(item))
    return keys


def validate_indicator(item):
    normalized = sentiment_report.normalize_indicator(item)
    reasons = []
    if not normalized["name"]:
        reasons.append("missing_name")
    if not normalized["observed_at"]:
        reasons.append("missing_observed_at")
    elif sentiment_report.parse_timestamp(normalized["observed_at"]) is None:
        reasons.append("invalid_observed_at")
    raw_type = str(item.get("indicator_type") or item.get("type") or "macro").strip().lower()
    if raw_type not in sentiment_report.VALID_INDICATOR_TYPES:
        reasons.append("invalid_indicator_type")
    raw_direction = str(item.get("direction") or "unknown").strip().lower()
    if raw_direction not in sentiment_report.VALID_DIRECTIONS:
        reasons.append("invalid_direction")
    raw_score = sentiment_report.as_float(item.get("score"))
    if raw_score is not None and (raw_score < -1 or raw_score > 1):
        reasons.append("score_out_of_range")
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
        reasons, normalized = validate_indicator(raw)
        key = indicator_key(raw)
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
        "schema": "market_sentiment_ingest_v1",
        "output_file": output_file,
        "input_count": len(items),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "duplicate_count": duplicate_count,
        "accepted": accepted,
        "rejected": rejected,
        "source": {
            "writes_market_sentiment_jsonl": True,
            "submits_orders": False,
            "changes_strategy": False,
            "changes_alert_queue": False,
        },
    }


def build_text_report(payload):
    lines = [
        (
            f"Market sentiment ingest accepted={payload['accepted_count']} "
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
    parser.add_argument("--indicator-json", action="append", default=[], help="one market sentiment indicator JSON object")
    parser.add_argument("--input-file", action="append", default=[], help="JSON object/list or {indicators:[...]}")
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
    for raw in args.indicator_json:
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
