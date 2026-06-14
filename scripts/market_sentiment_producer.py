#!/usr/bin/env python3
"""Read-only producer for basic market sentiment indicators.

The producer writes a snapshot consumed by market_sentiment_report.py. It does
not submit orders, change strategy config, or write the v5 alert queue.
"""
import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime


OUTPUT_FILE = os.environ.get("MARKET_SENTIMENT_INPUT_FILE", "/tmp/market_sentiment_inputs.json")
YAHOO_CHART_URL = os.environ.get("MARKET_SENTIMENT_YAHOO_CHART_URL", "https://query1.finance.yahoo.com/v8/finance/chart")
DEFAULT_RANGE = os.environ.get("MARKET_SENTIMENT_YAHOO_RANGE", "7d")
DEFAULT_INTERVAL = os.environ.get("MARKET_SENTIMENT_YAHOO_INTERVAL", "1d")
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("MARKET_SENTIMENT_FETCH_TIMEOUT_SECONDS", "10"))

DEFAULT_SYMBOLS = [
    {
        "provider_symbol": "^VIX",
        "name": "VIX",
        "indicator_type": "volatility",
        "markets": ["US", "GLOBAL"],
        "unit": "index",
    },
    {
        "provider_symbol": "SPY",
        "name": "SPY daily return",
        "indicator_type": "risk_appetite",
        "markets": ["US"],
        "unit": "pct",
    },
    {
        "provider_symbol": "QQQ",
        "name": "QQQ daily return",
        "indicator_type": "risk_appetite",
        "markets": ["US"],
        "unit": "pct",
    },
    {
        "provider_symbol": "^HSI",
        "name": "Hang Seng Index daily return",
        "indicator_type": "risk_appetite",
        "markets": ["HK"],
        "unit": "pct",
    },
    {
        "provider_symbol": "2800.HK",
        "name": "Tracker Fund of Hong Kong daily return",
        "indicator_type": "risk_appetite",
        "markets": ["HK"],
        "unit": "pct",
    },
]


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def clamp(value, low=-1.0, high=1.0):
    return max(low, min(high, value))


def direction_from_score(score):
    if score >= 0.25:
        return "risk_on"
    if score <= -0.25:
        return "risk_off"
    return "neutral"


def save_json_atomic(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def parse_symbol_list(value):
    if not value:
        return []
    result = []
    for raw in str(value).replace(";", ",").split(","):
        symbol = raw.strip()
        if symbol:
            result.append(symbol)
    return result


def symbol_configs(extra_symbols=None):
    configs = list(DEFAULT_SYMBOLS)
    for symbol in extra_symbols or []:
        configs.append(
            {
                "provider_symbol": symbol,
                "name": f"{symbol} daily return",
                "indicator_type": "risk_appetite",
                "markets": ["GLOBAL"],
                "unit": "pct",
            }
        )
    return configs


def fetch_yahoo_chart(symbol, range_value=DEFAULT_RANGE, interval=DEFAULT_INTERVAL, timeout=DEFAULT_TIMEOUT_SECONDS):
    encoded = urllib.parse.quote(symbol, safe="")
    query = urllib.parse.urlencode({"range": range_value, "interval": interval})
    url = f"{YAHOO_CHART_URL}/{encoded}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        error = (payload.get("chart") or {}).get("error") or {}
        raise ValueError(f"no_chart_result:{error}")
    timestamps = result.get("timestamp") or []
    quotes = (((result.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    rows = []
    for ts, close in zip(timestamps, quotes):
        if close is None:
            continue
        try:
            rows.append((int(ts), float(close)))
        except (TypeError, ValueError):
            continue
    if len(rows) < 2:
        raise ValueError("not_enough_close_points")
    return rows


def pct_change(value, previous):
    if previous in (None, 0):
        return None
    return (value / previous - 1.0) * 100.0


def vix_score(value, change_pct):
    level_component = (22.0 - value) / 12.0
    change_component = -(change_pct or 0.0) / 12.0
    return clamp(0.7 * level_component + 0.3 * change_component)


def return_score(change_pct):
    return clamp((change_pct or 0.0) / 2.5)


def indicator_from_chart(config, rows, observed_at):
    symbol = config["provider_symbol"]
    previous_ts, previous = rows[-2]
    latest_ts, latest = rows[-1]
    change_pct = pct_change(latest, previous)
    if change_pct is None:
        change_pct = 0.0
    latest_date = datetime.fromtimestamp(latest_ts).date().isoformat()
    previous_date = datetime.fromtimestamp(previous_ts).date().isoformat()
    if config["indicator_type"] == "volatility":
        score = vix_score(latest, change_pct)
        value = latest
        previous_value = previous
        summary = (
            f"{config['name']} latest={latest:.2f} previous={previous:.2f} "
            f"change={change_pct:+.2f}% data_as_of={latest_date}."
        )
    else:
        score = return_score(change_pct)
        value = change_pct
        previous_value = None
        summary = (
            f"{config['name']} latest={latest:.2f} previous={previous:.2f} "
            f"return={change_pct:+.2f}% data_as_of={latest_date}."
        )
    return {
        "id": f"yahoo-{symbol}-{latest_date}",
        "indicator_type": config["indicator_type"],
        "name": config["name"],
        "source": "yahoo_chart",
        "observed_at": observed_at,
        "markets": config["markets"],
        "direction": direction_from_score(score),
        "score": round(score, 4),
        "value": round(value, 4),
        "previous_value": round(previous_value, 4) if previous_value is not None else None,
        "change": round(change_pct, 4),
        "unit": config["unit"],
        "summary": summary,
        "tags": ["producer:yahoo_chart", f"provider_symbol:{symbol}", f"previous_date:{previous_date}"],
    }


def build_snapshot(fetch_chart=fetch_yahoo_chart, now=None, extra_symbols=None):
    observed_at = (now or datetime.now()).isoformat(timespec="seconds")
    indicators = []
    warnings = []
    for config in symbol_configs(extra_symbols=extra_symbols):
        symbol = config["provider_symbol"]
        try:
            rows = fetch_chart(symbol)
            indicators.append(indicator_from_chart(config, rows, observed_at))
        except Exception as exc:
            warnings.append(f"fetch_failed:{symbol}:{exc}")
    return {
        "schema": "market_sentiment_producer_v1",
        "generated_at": observed_at,
        "indicators": indicators,
        "warnings": warnings,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_strategy": False,
            "changes_alert_queue": False,
            "output_file": OUTPUT_FILE,
            "provider": "yahoo_chart",
            "range": DEFAULT_RANGE,
            "interval": DEFAULT_INTERVAL,
        },
    }


def build_text_report(payload):
    by_direction = {}
    for indicator in payload.get("indicators") or []:
        direction = indicator.get("direction") or "unknown"
        by_direction[direction] = by_direction.get(direction, 0) + 1
    return (
        f"Market sentiment producer generated={payload.get('generated_at')} "
        f"indicators={len(payload.get('indicators') or [])} "
        f"warnings={len(payload.get('warnings') or [])} "
        f"directions={by_direction}"
    )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=OUTPUT_FILE)
    parser.add_argument("--extra-symbols", default=os.environ.get("MARKET_SENTIMENT_EXTRA_SYMBOLS", ""))
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    parser.add_argument("--dry-run", action="store_true", help="fetch and print without writing")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_snapshot(extra_symbols=parse_symbol_list(args.extra_symbols))
    payload["source"]["output_file"] = args.output
    if args.output and not args.dry_run:
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
