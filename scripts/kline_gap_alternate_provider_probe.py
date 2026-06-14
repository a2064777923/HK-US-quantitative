#!/usr/bin/env python3
"""Read-only alternate-provider probe for unresolved daily K-line gaps."""
import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone


KLINE_DAILY_GAP_REPAIR_FILE = os.environ.get("KLINE_DAILY_GAP_REPAIR_FILE", "/tmp/kline_daily_gap_repair.json")
REPORT_FILE = os.environ.get(
    "KLINE_GAP_ALTERNATE_PROVIDER_PROBE_FILE",
    "/tmp/kline_gap_alternate_provider_probe.json",
)
YAHOO_CHART_URL = os.environ.get(
    "KLINE_GAP_PROBE_YAHOO_CHART_URL",
    "https://query1.finance.yahoo.com/v8/finance/chart",
)
DEFAULT_RANGE = os.environ.get("KLINE_GAP_PROBE_YAHOO_RANGE", "5y")
DEFAULT_INTERVAL = os.environ.get("KLINE_GAP_PROBE_YAHOO_INTERVAL", "1d")
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("KLINE_GAP_PROBE_TIMEOUT_SECONDS", "8"))
DEFAULT_LIMIT = int(os.environ.get("KLINE_GAP_PROBE_SYMBOL_LIMIT", "20"))


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


def parse_date(value):
    text = str(value or "")[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return text
    except ValueError:
        return None


def date_lag_days(later, earlier):
    later_date = parse_date(later)
    earlier_date = parse_date(earlier)
    if not later_date or not earlier_date:
        return None
    return (datetime.strptime(later_date, "%Y-%m-%d") - datetime.strptime(earlier_date, "%Y-%m-%d")).days


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


def yahoo_provider_symbol(symbol, market=None):
    text = str(symbol or "").strip().upper()
    market = str(market or "").upper()
    if market == "HK" or (text.isdigit() and len(text) == 5):
        try:
            return f"{int(text):04d}.HK"
        except ValueError:
            return text
    return text


def kline_errors(row):
    errors = []
    open_price = as_float(row.get("open"))
    high = as_float(row.get("high"))
    low = as_float(row.get("low"))
    close = as_float(row.get("close"))
    for name, value in (("open", open_price), ("high", high), ("low", low), ("close", close)):
        if value is None:
            errors.append(f"missing_{name}")
        elif value <= 0:
            errors.append(f"non_positive_{name}")
    if high is not None and low is not None and high < low:
        errors.append("high_below_low")
    if high is not None and low is not None and close is not None and not (low <= close <= high):
        errors.append("close_outside_high_low")
    if high is not None and low is not None and open_price is not None and not (low <= open_price <= high):
        errors.append("open_outside_high_low")
    return errors


def parse_yahoo_chart(payload):
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        error = (payload.get("chart") or {}).get("error") or {}
        raise ValueError(f"no_chart_result:{error}")
    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote") or [{}])[0]) or {}
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    rows = []
    for idx, stamp in enumerate(timestamps):
        try:
            day = datetime.fromtimestamp(int(stamp), timezone.utc).date().isoformat()
        except Exception:
            continue
        rows.append(
            {
                "date": day,
                "open": opens[idx] if idx < len(opens) else None,
                "high": highs[idx] if idx < len(highs) else None,
                "low": lows[idx] if idx < len(lows) else None,
                "close": closes[idx] if idx < len(closes) else None,
                "volume": volumes[idx] if idx < len(volumes) else None,
            }
        )
    return sorted(rows, key=lambda item: item["date"])


def fetch_yahoo_chart(provider_symbol, timeout=DEFAULT_TIMEOUT_SECONDS):
    encoded = urllib.parse.quote(provider_symbol, safe="")
    query = urllib.parse.urlencode(
        {
            "range": DEFAULT_RANGE,
            "interval": DEFAULT_INTERVAL,
            "includePrePost": "false",
            "events": "history",
        }
    )
    url = f"{YAHOO_CHART_URL}/{encoded}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return parse_yahoo_chart(payload)


def compact_row(row):
    return {
        "date": row.get("date"),
        "open": as_float(row.get("open")),
        "high": as_float(row.get("high")),
        "low": as_float(row.get("low")),
        "close": as_float(row.get("close")),
        "volume": as_int(row.get("volume"), None),
    }


def analyze_symbol(item, fetch_chart=fetch_yahoo_chart):
    symbol = item.get("symbol")
    market = item.get("market")
    provider = yahoo_provider_symbol(symbol, market)
    latest_daily = parse_date(item.get("latest_daily_date"))
    latest_primary_source = parse_date(item.get("latest_source_date"))
    target_end = parse_date(item.get("target_end_date"))
    base = {
        "symbol": symbol,
        "market": market,
        "provider": "yahoo_chart",
        "provider_symbol": provider,
        "primary_source": "tencent_day",
        "primary_source_attempts": (item.get("source_attempts") or [])[:6],
        "primary_latest_source_date": latest_primary_source,
        "latest_daily_date": latest_daily,
        "target_end_date": target_end,
        "primary_source_lag_days_vs_target": date_lag_days(target_end, latest_primary_source),
        "daily_lag_days_vs_target": date_lag_days(target_end, latest_daily),
    }
    try:
        rows = fetch_chart(provider)
    except Exception as exc:
        return {
            **base,
            "status": "fetch_failed",
            "category": "alternate_provider_fetch_failed",
            "confidence": "low",
            "recommended_action": "retry_alternate_provider_probe_before_universe_change",
            "error": str(exc),
            "alternate_row_count": 0,
            "alternate_latest_date": None,
            "alternate_reaches_target_end": False,
            "alternate_after_primary_source": False,
            "alternate_after_daily": False,
            "alternate_gap_row_count": 0,
            "invalid_alternate_gap_row_count": 0,
            "alternate_sample_gap_rows": [],
        }

    valid_rows = [row for row in rows or [] if parse_date(row.get("date"))]
    latest_alternate = max((row["date"] for row in valid_rows), default=None)
    gap_rows = []
    invalid_gap_rows = []
    for row in valid_rows:
        row_date = parse_date(row.get("date"))
        if latest_daily and row_date <= latest_daily:
            continue
        if target_end and row_date > target_end:
            continue
        errors = kline_errors(row)
        if errors:
            invalid_gap_rows.append({"date": row_date, "errors": errors})
        else:
            gap_rows.append(row)

    reaches_target = bool(latest_alternate and target_end and latest_alternate >= target_end)
    after_primary = bool(latest_alternate and latest_primary_source and latest_alternate > latest_primary_source)
    after_daily = bool(latest_alternate and latest_daily and latest_alternate > latest_daily)
    if invalid_gap_rows and not gap_rows:
        category = "alternate_provider_rows_invalid"
        action = "block_alternate_provider_repair_until_rows_validate"
        confidence = "high"
    elif not valid_rows:
        category = "alternate_provider_no_daily_rows"
        action = "review_listing_status_symbol_mapping_or_provider_support"
        confidence = "medium"
    elif reaches_target and gap_rows:
        category = "alternate_provider_has_current_daily_rows"
        action = "review_provider_disagreement_before_manual_repair_or_symbol_mapping"
        confidence = "high"
    elif after_primary:
        category = "alternate_provider_partial_catchup"
        action = "compare_provider_rows_and_refetch_before_stock_universe_change"
        confidence = "medium"
    elif latest_alternate and latest_primary_source and latest_alternate == latest_primary_source:
        category = "providers_agree_symbol_stale_or_suspended"
        action = "review_listing_status_or_deactivate_candidate_before_trusting_symbol"
        confidence = "high"
    elif latest_alternate and latest_primary_source and latest_alternate < latest_primary_source:
        category = "alternate_provider_older_than_primary"
        action = "prefer_primary_source_diagnostic_and_review_universe_hygiene"
        confidence = "medium"
    else:
        category = "alternate_provider_inconclusive"
        action = "manual_provider_comparison_required"
        confidence = "low"
    return {
        **base,
        "status": "probed",
        "category": category,
        "confidence": confidence,
        "recommended_action": action,
        "alternate_row_count": len(valid_rows),
        "alternate_latest_date": latest_alternate,
        "alternate_reaches_target_end": reaches_target,
        "alternate_after_primary_source": after_primary,
        "alternate_after_daily": after_daily,
        "alternate_lag_days_vs_target": date_lag_days(target_end, latest_alternate),
        "alternate_gap_row_count": len(gap_rows),
        "invalid_alternate_gap_row_count": len(invalid_gap_rows),
        "alternate_sample_gap_rows": [compact_row(row) for row in gap_rows[:5]],
        "invalid_alternate_gap_rows": invalid_gap_rows[:5],
    }


def report_status(probes, warnings):
    if warnings:
        return "WARN"
    if not probes:
        return "OK"
    high = sum(1 for item in probes if item.get("confidence") == "high")
    failed = sum(1 for item in probes if item.get("status") == "fetch_failed")
    if high:
        return "ACTION_REQUIRED"
    if failed == len(probes):
        return "WARN"
    return "REVIEW"


def build_recommendations(probes):
    if not probes:
        return ["alternate_provider_probe_not_required"]
    categories = {item.get("category") for item in probes}
    recs = ["review_alternate_provider_probe_before_changing_universe_or_repairing_gaps"]
    if "alternate_provider_has_current_daily_rows" in categories:
        recs.append("compare_yahoo_daily_rows_against_primary_provider_before_any_manual_repair")
    if "providers_agree_symbol_stale_or_suspended" in categories:
        recs.append("prioritize_listing_status_or_deactivation_review_for_symbols_stale_across_providers")
    if "alternate_provider_fetch_failed" in categories:
        recs.append("retry_failed_alternate_provider_probes_before_final_mapping_decision")
    if "alternate_provider_rows_invalid" in categories:
        recs.append("block_alternate_provider_repair_until_rows_validate")
    return recs


def build_report(kline_gap_repair=None, fetch_chart=fetch_yahoo_chart, limit=DEFAULT_LIMIT):
    warnings = []
    gap_payload = kline_gap_repair if kline_gap_repair is not None else load_json(KLINE_DAILY_GAP_REPAIR_FILE)
    if not gap_payload:
        warnings.append("kline_daily_gap_repair_report_missing")
        gap_payload = {}
    unresolved = list(gap_payload.get("unresolved") or [])[:limit]
    probes = [analyze_symbol(item, fetch_chart=fetch_chart) for item in unresolved]
    category_counts = {}
    confidence_counts = {}
    for item in probes:
        category = item.get("category") or "unknown"
        confidence = item.get("confidence") or "unknown"
        category_counts[category] = category_counts.get(category, 0) + 1
        confidence_counts[confidence] = confidence_counts.get(confidence, 0) + 1
    generated_at = now_iso()
    status = report_status(probes, warnings)
    return {
        "schema": "kline_gap_alternate_provider_probe_v1",
        "generated_at": generated_at,
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_crontab": False,
            "applies_kline_repairs": False,
            "changes_watchlists": False,
            "changes_stock_universe": False,
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
            "probed_count": len(probes),
            "category_counts": category_counts,
            "confidence_counts": confidence_counts,
            "alternate_current_count": category_counts.get("alternate_provider_has_current_daily_rows", 0),
            "providers_agree_stale_count": category_counts.get("providers_agree_symbol_stale_or_suspended", 0),
            "fetch_failed_count": category_counts.get("alternate_provider_fetch_failed", 0),
        },
        "probes": probes,
        "recommendations": build_recommendations(probes),
        "warnings": warnings,
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"K-line gap alternate provider probe {payload.get('generated_at')} status={payload.get('status')}",
        "unresolved={unresolved} probed={probed} categories={categories}".format(
            unresolved=summary.get("unresolved_count"),
            probed=summary.get("probed_count"),
            categories=summary.get("category_counts") or {},
        ),
    ]
    for item in (payload.get("probes") or [])[:20]:
        lines.append(
            "  {symbol}: {category} alt_latest={alt} primary_latest={primary} target={target}".format(
                symbol=item.get("symbol"),
                category=item.get("category"),
                alt=item.get("alternate_latest_date"),
                primary=item.get("primary_latest_source_date"),
                target=item.get("target_end_date"),
            )
        )
    lines.append("Recommendations: " + ", ".join(payload.get("recommendations") or []))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
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
