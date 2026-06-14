#!/usr/bin/env python3
"""Read-only producer for HK/US index and ETF history used by market context."""
import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone


OUTPUT_FILE = os.environ.get("MARKET_INDEX_CONTEXT_INPUT_FILE", "/tmp/market_index_context_inputs.json")
YAHOO_CHART_URL = os.environ.get("MARKET_INDEX_YAHOO_CHART_URL", "https://query1.finance.yahoo.com/v8/finance/chart")
DEFAULT_RANGE = os.environ.get("MARKET_INDEX_YAHOO_RANGE", "6mo")
DEFAULT_INTERVAL = os.environ.get("MARKET_INDEX_YAHOO_INTERVAL", "1d")
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("MARKET_INDEX_FETCH_TIMEOUT_SECONDS", "10"))

DEFAULT_INDEXES = [
    {
        "provider_symbol": "^GSPC",
        "name": "S&P 500 Index",
        "market": "US",
        "index_type": "benchmark_index",
    },
    {
        "provider_symbol": "^IXIC",
        "name": "NASDAQ Composite",
        "market": "US",
        "index_type": "benchmark_index",
    },
    {
        "provider_symbol": "SPY",
        "name": "SPDR S&P 500 ETF",
        "market": "US",
        "index_type": "broad_market_etf",
    },
    {
        "provider_symbol": "QQQ",
        "name": "Invesco QQQ ETF",
        "market": "US",
        "index_type": "growth_etf",
    },
    {
        "provider_symbol": "^HSI",
        "name": "Hang Seng Index",
        "market": "HK",
        "index_type": "benchmark_index",
    },
    {
        "provider_symbol": "2800.HK",
        "name": "Tracker Fund of Hong Kong",
        "market": "HK",
        "index_type": "broad_market_etf",
    },
]


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


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
    configs = list(DEFAULT_INDEXES)
    for symbol in extra_symbols or []:
        configs.append(
            {
                "provider_symbol": symbol,
                "name": f"{symbol} index or ETF snapshot",
                "market": "GLOBAL",
                "index_type": "operator_extra",
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
    quote = (((result.get("indicators") or {}).get("quote") or [{}])[0]) or {}
    rows = []
    for idx, ts in enumerate(timestamps):
        close = value_at(quote.get("close"), idx)
        if close is None:
            continue
        try:
            row = {
                "date": datetime.fromtimestamp(int(ts), timezone.utc).date().isoformat(),
                "open": as_float(value_at(quote.get("open"), idx)),
                "high": as_float(value_at(quote.get("high"), idx)),
                "low": as_float(value_at(quote.get("low"), idx)),
                "close": float(close),
                "volume": as_float(value_at(quote.get("volume"), idx)),
            }
        except (TypeError, ValueError):
            continue
        rows.append(row)
    if len(rows) < 20:
        raise ValueError(f"not_enough_daily_points:{len(rows)}")
    return rows


def value_at(values, idx):
    if not isinstance(values, list) or idx >= len(values):
        return None
    return values[idx]


def as_float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def pct_change(value, previous):
    if previous in (None, 0):
        return None
    return (value / previous - 1.0) * 100.0


def index_snapshot(config, series, observed_at):
    ordered = sorted(series, key=lambda row: row["date"])
    latest = ordered[-1]
    previous = ordered[-2] if len(ordered) >= 2 else {}
    change = pct_change(latest.get("close"), previous.get("close"))
    symbol = config["provider_symbol"]
    return {
        "provider_symbol": symbol,
        "symbol": symbol,
        "name": config["name"],
        "market": config["market"],
        "index_type": config["index_type"],
        "source": "yahoo_chart_snapshot",
        "provider": "yahoo_chart",
        "observed_at": observed_at,
        "data_as_of": latest.get("date"),
        "history_days": len(ordered),
        "latest_close": round(latest["close"], 4),
        "previous_close": round(previous["close"], 4) if previous.get("close") is not None else None,
        "return_1d_pct": round(change, 4) if change is not None else None,
        "series": ordered,
    }


def build_snapshot(fetch_chart=fetch_yahoo_chart, now=None, extra_symbols=None):
    observed_at = (now or datetime.now()).isoformat(timespec="seconds")
    indexes = []
    warnings = []
    for config in symbol_configs(extra_symbols=extra_symbols):
        symbol = config["provider_symbol"]
        try:
            indexes.append(index_snapshot(config, fetch_chart(symbol), observed_at))
        except Exception as exc:
            warnings.append(f"fetch_failed:{symbol}:{exc}")
    return {
        "schema": "market_index_context_producer_v1",
        "generated_at": observed_at,
        "indexes": indexes,
        "warnings": warnings,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_strategy": False,
            "changes_alert_queue": False,
            "writes_database": False,
            "output_file": OUTPUT_FILE,
            "provider": "yahoo_chart",
            "provider_grade": "public_fallback",
            "range": DEFAULT_RANGE,
            "interval": DEFAULT_INTERVAL,
        },
    }


def build_text_report(payload):
    by_market = {}
    for item in payload.get("indexes") or []:
        market = item.get("market") or "UNKNOWN"
        by_market[market] = by_market.get(market, 0) + 1
    return (
        f"Market index context producer generated={payload.get('generated_at')} "
        f"indexes={len(payload.get('indexes') or [])} "
        f"warnings={len(payload.get('warnings') or [])} "
        f"markets={by_market}"
    )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=OUTPUT_FILE)
    parser.add_argument("--extra-symbols", default=os.environ.get("MARKET_INDEX_EXTRA_SYMBOLS", ""))
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
