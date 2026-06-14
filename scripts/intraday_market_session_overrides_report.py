#!/usr/bin/env python3
"""Read-only validator for intraday market-session override files."""
import argparse
import json
import os
from datetime import datetime, timedelta


REPORT_FILE = os.environ.get(
    "INTRADAY_MARKET_SESSION_OVERRIDES_REPORT_FILE",
    "/tmp/intraday_market_session_overrides_report.json",
)
OVERRIDES_FILE = os.environ.get(
    "INTRADAY_MARKET_SESSION_OVERRIDES_FILE",
    "/root/intraday_market_sessions.json",
)
EXPECTED_MARKETS = ("HK", "US")
MIN_FUTURE_COVERAGE_DAYS = int(os.environ.get("INTRADAY_MARKET_SESSION_MIN_FUTURE_COVERAGE_DAYS", "30"))


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


def parse_date(value):
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def parse_hhmm(value):
    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour * 60 + minute


def normalize_session_windows(value):
    windows = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            start = parse_hhmm(item.get("open") or item.get("start"))
            end = parse_hhmm(item.get("close") or item.get("end"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            start = parse_hhmm(item[0])
            end = parse_hhmm(item[1])
        else:
            start = end = None
        if start is None or end is None or start >= end:
            windows.append({"valid": False, "raw": item})
        else:
            windows.append({"valid": True, "open_minutes": start, "close_minutes": end, "raw": item})
    return windows


def date_keys(value):
    if isinstance(value, dict):
        return list(value.keys())
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def market_payloads(payload):
    if not isinstance(payload, dict):
        return {}
    parent = payload.get("markets") if isinstance(payload.get("markets"), dict) else payload
    return parent if isinstance(parent, dict) else {}


def validate_market(market, payload, today):
    warnings = []
    errors = []
    closed_dates = date_keys(payload.get("closed_dates"))
    invalid_dates = []
    past_dates = []
    future_dates = []
    for text in closed_dates:
        parsed = parse_date(text)
        if not parsed:
            invalid_dates.append(text)
        elif parsed < today:
            past_dates.append(text)
        else:
            future_dates.append(text)

    session_rows = []
    for key in ("half_days", "session_overrides", "special_sessions"):
        parent = payload.get(key)
        if not isinstance(parent, dict):
            continue
        for date_text, item in parent.items():
            parsed = parse_date(date_text)
            if not parsed:
                invalid_dates.append(date_text)
                continue
            raw_windows = item.get("session_windows") or item.get("sessions") if isinstance(item, dict) else item
            windows = normalize_session_windows(raw_windows)
            bad_windows = [row for row in windows if not row.get("valid")]
            if not windows or bad_windows:
                errors.append(f"{market}:{key}:{date_text}:invalid_session_windows")
            if parsed < today:
                past_dates.append(date_text)
            else:
                future_dates.append(date_text)
            session_rows.append(
                {
                    "source_key": key,
                    "date": date_text,
                    "valid_window_count": len([row for row in windows if row.get("valid")]),
                    "invalid_window_count": len(bad_windows),
                }
            )

    if invalid_dates:
        errors.append(f"{market}:invalid_date_keys")
    if not future_dates:
        warnings.append(f"{market}:no_future_session_overrides_or_closed_dates")

    horizon = today + timedelta(days=MIN_FUTURE_COVERAGE_DAYS)
    coverage_until = max((parse_date(value) for value in future_dates if parse_date(value)), default=None)
    if coverage_until is None or coverage_until < horizon:
        warnings.append(f"{market}:future_override_coverage_lt_{MIN_FUTURE_COVERAGE_DAYS}d")

    return {
        "market": market,
        "status": "FAIL" if errors else "WARN" if warnings else "OK",
        "closed_date_count": len(closed_dates),
        "future_entry_count": len(set(future_dates)),
        "past_entry_count": len(set(past_dates)),
        "invalid_date_keys": sorted(set(invalid_dates)),
        "session_override_entries": session_rows,
        "coverage_until": coverage_until.isoformat() if coverage_until else None,
        "warnings": warnings,
        "errors": errors,
    }


def load_json_file(path):
    if not path:
        return None, ["overrides_file_not_configured"]
    if not os.path.exists(path):
        return None, [f"overrides_file_missing:{path}"]
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        return None, [f"overrides_file_unreadable:{path}:{exc}"]
    if not isinstance(payload, dict):
        return None, [f"overrides_file_invalid_root:{path}"]
    return payload, []


def build_report(overrides_file=OVERRIDES_FILE, payload=None, now=None):
    now = now or datetime.now()
    today = now.date()
    load_warnings = []
    if payload is None:
        payload, load_warnings = load_json_file(overrides_file)

    markets = {}
    errors = []
    warnings = list(load_warnings)
    if payload is None:
        status = "WARN"
    else:
        raw_markets = market_payloads(payload)
        for market in EXPECTED_MARKETS:
            item = raw_markets.get(market) or raw_markets.get(market.lower())
            if not isinstance(item, dict):
                markets[market] = {
                    "market": market,
                    "status": "WARN",
                    "warnings": [f"{market}:market_session_override_missing"],
                    "errors": [],
                    "closed_date_count": 0,
                    "future_entry_count": 0,
                    "past_entry_count": 0,
                    "invalid_date_keys": [],
                    "session_override_entries": [],
                    "coverage_until": None,
                }
                continue
            markets[market] = validate_market(market, item, today)
        errors = [error for row in markets.values() for error in row.get("errors") or []]
        warnings.extend([warning for row in markets.values() for warning in row.get("warnings") or []])
        if errors:
            status = "FAIL"
        elif warnings:
            status = "WARN"
        else:
            status = "OK"

    return {
        "schema": "intraday_market_session_overrides_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_crontab": False,
            "changes_strategy": False,
            "changes_watchlists": False,
            "repairs_klines": False,
            "overrides_file": overrides_file,
            "expected_markets": list(EXPECTED_MARKETS),
            "min_future_coverage_days": MIN_FUTURE_COVERAGE_DAYS,
        },
        "summary": {
            "market_count": len(markets),
            "ok_market_count": len([row for row in markets.values() if row.get("status") == "OK"]),
            "warning_market_count": len([row for row in markets.values() if row.get("status") == "WARN"]),
            "failed_market_count": len([row for row in markets.values() if row.get("status") == "FAIL"]),
            "warning_count": len(warnings),
            "error_count": len(errors),
        },
        "markets": markets,
        "warnings": warnings,
        "errors": errors,
        "recommendations": recommendations(status, warnings, errors),
        "hermes_use": [
            "Use this report to judge whether intraday market-session overrides are configured and syntactically safe.",
            "This report does not prove exchange-calendar completeness; it only validates the operator-maintained override file.",
            "Do not treat this report as permission to trade or to relax readiness, data-health, or source-reliability gates.",
        ],
    }


def recommendations(status, warnings, errors):
    recs = []
    if errors:
        recs.append("fix_intraday_market_session_override_schema_before_trusting_calendar")
    if warnings:
        recs.append("review_intraday_market_session_override_coverage_for_holidays_and_half_days")
    if status == "OK":
        recs.append("intraday_market_session_overrides_validated")
    return sorted(set(recs))


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Intraday market session overrides report {payload['generated_at']} status={payload['status']}",
        (
            f"markets={summary.get('market_count')} ok={summary.get('ok_market_count')} "
            f"warn={summary.get('warning_market_count')} fail={summary.get('failed_market_count')} "
            f"warnings={summary.get('warning_count')} errors={summary.get('error_count')}"
        ),
    ]
    for market, row in sorted((payload.get("markets") or {}).items()):
        lines.append(
            f"{market}: status={row.get('status')} future_entries={row.get('future_entry_count')} "
            f"coverage_until={row.get('coverage_until')}"
        )
    if payload.get("recommendations"):
        lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"][:8]))
    if payload.get("errors"):
        lines.append("Errors: " + ", ".join(payload["errors"][:8]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--overrides-file", default=OVERRIDES_FILE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(overrides_file=args.overrides_file)
    save_json_atomic(args.output, payload)
    if args.json or not args.text:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.text:
        print(build_text_report(payload))
    return 0 if payload["status"] in ("OK", "WARN") else 2


if __name__ == "__main__":
    raise SystemExit(main())
