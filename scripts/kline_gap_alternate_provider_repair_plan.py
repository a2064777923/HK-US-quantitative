#!/usr/bin/env python3
"""Read-only repair-candidate plan for alternate-provider daily K-line gaps."""
import argparse
import hashlib
import json
import os
from datetime import datetime

try:
    import kline_gap_alternate_provider_probe as probe
except ImportError:
    from scripts import kline_gap_alternate_provider_probe as probe


KLINE_DAILY_GAP_REPAIR_FILE = os.environ.get("KLINE_DAILY_GAP_REPAIR_FILE", "/tmp/kline_daily_gap_repair.json")
REPORT_FILE = os.environ.get(
    "KLINE_GAP_ALTERNATE_PROVIDER_REPAIR_PLAN_FILE",
    "/tmp/kline_gap_alternate_provider_repair_plan.json",
)
DEFAULT_LIMIT = int(os.environ.get("KLINE_GAP_ALT_REPAIR_SYMBOL_LIMIT", "20"))
MAX_ZERO_VOLUME_PCT = float(os.environ.get("KLINE_GAP_ALT_REPAIR_MAX_ZERO_VOLUME_PCT", "20"))
MAX_FLAT_OHLC_PCT = float(os.environ.get("KLINE_GAP_ALT_REPAIR_MAX_FLAT_OHLC_PCT", "50"))


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


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def rate(part, whole):
    return round(part / whole * 100.0, 2) if whole else 0.0


def round_price(value):
    return round(float(value), 6)


def normalize_gap_row(row, previous_close=None):
    open_price = round_price(row.get("open"))
    high = round_price(row.get("high"))
    low = round_price(row.get("low"))
    close = round_price(row.get("close"))
    volume = probe.as_int(row.get("volume"), 0)
    amount = round(close * volume, 6)
    if previous_close and previous_close > 0:
        change_percent = round((close - previous_close) / previous_close * 100.0, 6)
    elif open_price > 0:
        change_percent = round((close - open_price) / open_price * 100.0, 6)
    else:
        change_percent = 0.0
    return {
        "date": probe.parse_date(row.get("date")),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": amount,
        "change_percent": change_percent,
    }


def gap_rows_for_item(item, rows):
    latest_daily = probe.parse_date(item.get("latest_daily_date"))
    target_end = probe.parse_date(item.get("target_end_date"))
    previous_close = probe.as_float(item.get("latest_daily_close"))
    normalized = []
    invalid = []
    for row in sorted(rows or [], key=lambda item: str(item.get("date") or "")):
        row_date = probe.parse_date(row.get("date"))
        if not row_date:
            continue
        if latest_daily and row_date <= latest_daily:
            previous_close = probe.as_float(row.get("close"), previous_close)
            continue
        if target_end and row_date > target_end:
            continue
        errors = probe.kline_errors(row)
        if errors:
            invalid.append({"date": row_date, "errors": errors})
            continue
        try:
            normalized_row = normalize_gap_row(row, previous_close=previous_close)
        except (TypeError, ValueError):
            invalid.append({"date": row_date, "errors": ["parse_failed"]})
            continue
        normalized.append(normalized_row)
        previous_close = normalized_row["close"]
    return normalized, invalid


def quality_summary(rows, invalid_rows=None):
    rows = rows or []
    invalid_rows = invalid_rows or []
    zero_volume = [row for row in rows if probe.as_int(row.get("volume"), 0) <= 0]
    flat_ohlc = [
        row
        for row in rows
        if probe.as_float(row.get("open")) == probe.as_float(row.get("high"))
        == probe.as_float(row.get("low")) == probe.as_float(row.get("close"))
    ]
    reasons = []
    zero_pct = rate(len(zero_volume), len(rows))
    flat_pct = rate(len(flat_ohlc), len(rows))
    if invalid_rows:
        reasons.append("invalid_alternate_provider_rows")
    if rows and zero_pct > MAX_ZERO_VOLUME_PCT:
        reasons.append("zero_volume_gap_rows_above_threshold")
    if rows and flat_pct > MAX_FLAT_OHLC_PCT:
        reasons.append("flat_ohlc_gap_rows_above_threshold")
    status = "PASS" if rows and not reasons else "REVIEW"
    if invalid_rows and not rows:
        status = "BLOCK"
    return {
        "status": status,
        "row_count": len(rows),
        "invalid_row_count": len(invalid_rows),
        "zero_volume_count": len(zero_volume),
        "zero_volume_pct": zero_pct,
        "flat_ohlc_count": len(flat_ohlc),
        "flat_ohlc_pct": flat_pct,
        "max_zero_volume_pct": MAX_ZERO_VOLUME_PCT,
        "max_flat_ohlc_pct": MAX_FLAT_OHLC_PCT,
        "reasons": reasons,
        "invalid_rows": invalid_rows[:20],
    }


def candidate_status(quality):
    if quality.get("status") == "PASS":
        return "manual_repair_candidate_after_operator_comparison"
    if quality.get("status") == "BLOCK":
        return "blocked_invalid_alternate_rows"
    return "review_only_quality_not_sufficient_for_repair_plan"


def build_candidate(item, rows):
    provider_symbol = probe.yahoo_provider_symbol(item.get("symbol"), item.get("market"))
    gap_rows, invalid_rows = gap_rows_for_item(item, rows)
    quality = quality_summary(gap_rows, invalid_rows=invalid_rows)
    return {
        "symbol": item.get("symbol"),
        "market": item.get("market"),
        "provider": "yahoo_chart",
        "provider_symbol": provider_symbol,
        "primary_source": "tencent_day",
        "latest_daily_date": probe.parse_date(item.get("latest_daily_date")),
        "latest_primary_source_date": probe.parse_date(item.get("latest_source_date")),
        "target_end_date": probe.parse_date(item.get("target_end_date")),
        "primary_source_lag_days_vs_target": probe.date_lag_days(
            item.get("target_end_date"),
            item.get("latest_source_date"),
        ),
        "status": candidate_status(quality),
        "quality": quality,
        "row_count": len(gap_rows),
        "rows": gap_rows,
        "row_preview": gap_rows[:5],
        "required_operator_review": [
            "compare_alternate_provider_rows_with_exchange_or_broker_history",
            "confirm_zero_volume_or_flat_ohlc_rows_are_not_provider_carry_forward",
            "confirm_symbol_mapping_and_listing_status",
            "only_then_create_or_run_a_separate_hash_confirmed_db_repair",
        ],
    }


def build_issue(item, category, detail):
    return {
        "symbol": item.get("symbol"),
        "market": item.get("market"),
        "provider_symbol": probe.yahoo_provider_symbol(item.get("symbol"), item.get("market")),
        "category": category,
        "detail": detail,
        "latest_daily_date": probe.parse_date(item.get("latest_daily_date")),
        "latest_primary_source_date": probe.parse_date(item.get("latest_source_date")),
        "target_end_date": probe.parse_date(item.get("target_end_date")),
    }


def plan_hash(candidates):
    stable_rows = []
    for candidate in candidates:
        stable_rows.append(
            {
                "symbol": candidate.get("symbol"),
                "provider_symbol": candidate.get("provider_symbol"),
                "status": candidate.get("status"),
                "quality": candidate.get("quality"),
                "rows": candidate.get("rows") or [],
            }
        )
    stable = json.dumps(stable_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def report_status(candidates, issues, warnings):
    if warnings and not candidates:
        return "WARN"
    if any(candidate.get("status") == "manual_repair_candidate_after_operator_comparison" for candidate in candidates):
        return "ACTION_REQUIRED"
    if candidates:
        return "REVIEW"
    if issues:
        return "WARN"
    return "OK"


def build_recommendations(candidates, issues):
    if not candidates and not issues:
        return ["alternate_provider_repair_plan_not_required"]
    recs = ["review_alternate_provider_repair_candidates_before_any_db_repair"]
    if any(candidate.get("status") == "manual_repair_candidate_after_operator_comparison" for candidate in candidates):
        recs.append("operator_may_design_separate_hash_confirmed_repair_after_row_comparison")
    if any(candidate.get("status") == "review_only_quality_not_sufficient_for_repair_plan" for candidate in candidates):
        recs.append("do_not_repair_zero_volume_or_flat_ohlc_alternate_rows_without_external_confirmation")
    if any(issue.get("category") == "alternate_provider_fetch_failed" for issue in issues):
        recs.append("retry_failed_alternate_provider_fetches_before_mapping_or_deactivation_decision")
    return recs


def build_report(kline_gap_repair=None, fetch_chart=probe.fetch_yahoo_chart, limit=DEFAULT_LIMIT):
    warnings = []
    gap_payload = kline_gap_repair if kline_gap_repair is not None else load_json(KLINE_DAILY_GAP_REPAIR_FILE)
    if not gap_payload:
        warnings.append("kline_daily_gap_repair_report_missing")
        gap_payload = {}
    unresolved = list(gap_payload.get("unresolved") or [])[:limit]
    candidates = []
    issues = []
    for item in unresolved:
        provider_symbol = probe.yahoo_provider_symbol(item.get("symbol"), item.get("market"))
        try:
            rows = fetch_chart(provider_symbol)
        except Exception as exc:
            issues.append(build_issue(item, "alternate_provider_fetch_failed", str(exc)))
            continue
        candidate = build_candidate(item, rows)
        if candidate["row_count"] > 0 or candidate["quality"]["invalid_row_count"] > 0:
            candidates.append(candidate)
        else:
            issues.append(build_issue(item, "alternate_provider_gap_rows_missing", "no_valid_gap_rows_after_latest_daily"))
    digest = plan_hash(candidates)
    status = report_status(candidates, issues, warnings)
    manual_candidates = [
        candidate
        for candidate in candidates
        if candidate.get("status") == "manual_repair_candidate_after_operator_comparison"
    ]
    review_only = [
        candidate
        for candidate in candidates
        if candidate.get("status") == "review_only_quality_not_sufficient_for_repair_plan"
    ]
    blocked = [
        candidate
        for candidate in candidates
        if candidate.get("status") == "blocked_invalid_alternate_rows"
    ]
    generated_at = now_iso()
    return {
        "schema": "kline_gap_alternate_provider_repair_plan_v1",
        "generated_at": generated_at,
        "status": status,
        "mode": "read-only-plan",
        "plan_hash": digest,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_crontab": False,
            "applies_kline_repairs": False,
            "changes_watchlists": False,
            "changes_stock_universe": False,
            "auto_applies_repairs": False,
            "auto_uses_alternate_provider_for_repairs": False,
            "auto_excludes_from_evidence": False,
            "provider": "yahoo_chart",
            "primary_provider": "tencent_day",
            "kline_daily_gap_repair_status": gap_payload.get("status"),
            "kline_daily_gap_repair_plan_hash": gap_payload.get("plan_hash"),
            "symbol_limit": limit,
        },
        "summary": {
            "unresolved_count": len(gap_payload.get("unresolved") or []),
            "evaluated_count": len(unresolved),
            "candidate_count": len(candidates),
            "manual_repair_candidate_count": len(manual_candidates),
            "review_only_count": len(review_only),
            "blocked_candidate_count": len(blocked),
            "issue_count": len(issues),
            "planned_row_count": sum(candidate.get("row_count", 0) for candidate in manual_candidates),
            "review_only_row_count": sum(candidate.get("row_count", 0) for candidate in review_only),
        },
        "candidates": candidates,
        "issues": issues,
        "operator_contract": {
            "manual_review_required": True,
            "manual_apply_command": None,
            "why_no_apply_command": "alternate-provider rows require independent comparison before any DB repair tool exists",
            "post_review_next_step": "create_or_run_separate_hash_confirmed_repair_only_after_operator_approves_rows",
            "does_not_submit_orders": True,
            "does_not_change_crontab": True,
            "does_not_change_watchlists": True,
            "does_not_change_stock_universe": True,
        },
        "recommendations": build_recommendations(candidates, issues),
        "warnings": warnings,
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"K-line alternate-provider repair candidate plan {payload.get('generated_at')} status={payload.get('status')}",
        (
            f"candidates={summary.get('candidate_count')} manual={summary.get('manual_repair_candidate_count')} "
            f"review_only={summary.get('review_only_count')} issues={summary.get('issue_count')} "
            f"plan_hash={payload.get('plan_hash')}"
        ),
    ]
    for item in (payload.get("candidates") or [])[:20]:
        quality = item.get("quality") or {}
        lines.append(
            "  {symbol}: {status} rows={rows} zero_volume={zero}% flat_ohlc={flat}%".format(
                symbol=item.get("symbol"),
                status=item.get("status"),
                rows=item.get("row_count"),
                zero=quality.get("zero_volume_pct"),
                flat=quality.get("flat_ohlc_pct"),
            )
        )
    if payload.get("issues"):
        lines.append(
            "Issues: "
            + ", ".join(
                f"{item.get('symbol')}:{item.get('category')}" for item in (payload.get("issues") or [])[:20]
            )
        )
    lines.append("Recommendations: " + ", ".join(payload.get("recommendations") or []))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kline-daily-gap-repair-file", default=KLINE_DAILY_GAP_REPAIR_FILE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(
        kline_gap_repair=load_json(args.kline_daily_gap_repair_file),
        limit=args.limit,
    )
    if args.output:
        save_json_atomic(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.text:
        print(build_text_report(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(build_text_report(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
