#!/usr/bin/env python3
"""Read-only fundamentals producer for Hermes fundamentals context."""
import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime


OUTPUT_FILE = os.environ.get("FUNDAMENTALS_CONTEXT_INPUT_FILE", "/tmp/fundamentals_context_inputs.json")
WATCHLIST_FILE = os.environ.get("RT_SIGNAL_WATCHLIST_FILE", "/root/rt_signal_watchlist.json")
YAHOO_QUOTE_SUMMARY_URL = os.environ.get(
    "FUNDAMENTALS_YAHOO_QUOTE_SUMMARY_URL",
    "https://query1.finance.yahoo.com/v10/finance/quoteSummary",
)
TENCENT_QUOTE_URL = os.environ.get(
    "FUNDAMENTALS_TENCENT_QUOTE_URL",
    "https://qt.gtimg.cn/q=",
)
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("FUNDAMENTALS_FETCH_TIMEOUT_SECONDS", "10"))
DEFAULT_LIMIT = int(os.environ.get("FUNDAMENTALS_CONTEXT_SYMBOL_LIMIT", "80"))
YAHOO_MODULES = "summaryDetail,defaultKeyStatistics,financialData,price"


DEFAULT_SYMBOLS = [
    "00700",
    "09988",
    "03690",
    "01810",
    "AAPL",
    "MSFT",
    "NVDA",
    "TSLA",
    "GOOGL",
    "AMZN",
    "META",
]


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


def normalize_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace(";", ",").replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        raw = [value]
    result = []
    seen = set()
    for item in raw:
        text = str(item).strip().upper()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def symbols_from_watchlist_payload(payload, market):
    candidates = [
        payload.get(market),
        payload.get(market.lower()),
        payload.get(f"{market}_WATCHLIST"),
        payload.get(f"{market.lower()}_watchlist"),
    ]
    for parent_key in ("markets", "watchlists"):
        parent = payload.get(parent_key)
        if isinstance(parent, dict):
            item = parent.get(market) or parent.get(market.lower())
            if isinstance(item, dict):
                candidates.append(item.get("symbols"))
            else:
                candidates.append(item)
    for candidate in candidates:
        symbols = normalize_list(candidate)
        if symbols:
            return symbols
    return []


def load_watchlist(path=WATCHLIST_FILE):
    warnings = []
    if not path or not os.path.exists(path):
        return [], [f"watchlist_missing:{path}"]
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        return [], [f"watchlist_read_failed:{exc}"]
    if not isinstance(payload, dict):
        return [], ["watchlist_invalid_type"]
    symbols = symbols_from_watchlist_payload(payload, "HK") + symbols_from_watchlist_payload(payload, "US")
    return symbols, warnings


def parse_symbol_list(value):
    return normalize_list(value)


def is_hk_symbol(symbol):
    return symbol[:1].isdigit() and len(symbol) == 5


def provider_symbol(symbol):
    symbol = str(symbol or "").strip().upper()
    if is_hk_symbol(symbol):
        return f"{int(symbol):04d}.HK"
    return symbol


def tencent_provider_symbol(symbol):
    symbol = str(symbol or "").strip().upper()
    if is_hk_symbol(symbol):
        return f"hk{symbol}"
    return f"us{symbol.split('.')[0]}"


def market_for_symbol(symbol):
    return "HK" if is_hk_symbol(symbol) else "US"


def raw_value(value):
    if isinstance(value, dict):
        if "raw" in value:
            return value.get("raw")
        if "fmt" in value:
            return value.get("fmt")
    return value


def as_float(value, default=None):
    try:
        value = raw_value(value)
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_yahoo_quote_summary(symbol, timeout=DEFAULT_TIMEOUT_SECONDS):
    encoded = urllib.parse.quote(symbol, safe="")
    query = urllib.parse.urlencode({"modules": YAHOO_MODULES})
    url = f"{YAHOO_QUOTE_SUMMARY_URL}/{encoded}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    result = ((payload.get("quoteSummary") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        error = (payload.get("quoteSummary") or {}).get("error") or {}
        raise ValueError(f"no_quote_summary:{error}")
    return result


def parse_tencent_provider_symbol(value):
    text = str(value or "").strip()
    if text.startswith("v_"):
        text = text[2:]
    if "=" in text:
        text = text.split("=", 1)[0]
    return text.strip().lower()


def symbol_from_tencent_parts(parts, provider_symbol=""):
    if len(parts) > 2 and parts[2]:
        raw = parts[2].strip().upper().split(".")[0]
        if raw:
            return raw
    provider_symbol = parse_tencent_provider_symbol(provider_symbol)
    if provider_symbol.startswith("hk"):
        return provider_symbol[2:].upper()
    if provider_symbol.startswith("us"):
        return provider_symbol[2:].upper()
    return provider_symbol.upper()


def market_from_tencent_provider(provider_symbol, symbol=""):
    provider_symbol = parse_tencent_provider_symbol(provider_symbol)
    if provider_symbol.startswith("hk"):
        return "HK"
    if provider_symbol.startswith("us"):
        return "US"
    return market_for_symbol(symbol)


def tencent_pe_value(value):
    pe = as_float(value)
    if pe == 0:
        return None
    return pe


def item_from_tencent(symbol, provider, parts, observed_at):
    market = market_from_tencent_provider(provider, symbol)
    name = parts[1].strip() if len(parts) > 1 else symbol
    currency = ""
    if market == "HK":
        currency = "HKD"
    elif len(parts) > 35 and parts[35].strip() and not as_float(parts[35]):
        currency = parts[35].strip().upper()
    elif market == "US":
        currency = "USD"
    return {
        "symbol": symbol,
        "market": market,
        "name": name or symbol,
        "source": "tencent_quote_snapshot",
        "provider_symbol": provider,
        "as_of": observed_at,
        "currency": currency,
        "market_cap": None,
        "pe_ttm": tencent_pe_value(parts[39] if len(parts) > 39 else None),
        "pb": None,
        "ps": None,
        "roe_pct": None,
        "revenue_growth_pct": None,
        "earnings_growth_pct": None,
        "dividend_yield_pct": None,
        "debt_to_equity": None,
        "summary": (
            f"Tencent quote snapshot partial fundamentals for {provider}; "
            "currently maps only conservative quote-level valuation fields."
        ),
    }


def parse_tencent_quote_text(text, requested_symbols=None, observed_at=None):
    observed_at = observed_at or now_iso()
    requested = normalize_list(requested_symbols or [])
    requested_by_symbol = {symbol: symbol for symbol in requested}
    requested_by_provider = {tencent_provider_symbol(symbol).lower(): symbol for symbol in requested}
    items = {}
    warnings = []
    for idx, line in enumerate(str(text or "").strip().splitlines()):
        if "~" not in line:
            continue
        parts = line.split("~")
        provider = parse_tencent_provider_symbol(parts[0] if parts else "")
        if len(parts) < 40:
            warnings.append(f"tencent_quote_short_line:{idx}:{provider or 'unknown'}")
            continue
        symbol = symbol_from_tencent_parts(parts, provider)
        symbol = requested_by_provider.get(provider, requested_by_symbol.get(symbol, symbol))
        if not symbol:
            warnings.append(f"tencent_quote_missing_symbol:{idx}:{provider or 'unknown'}")
            continue
        item = item_from_tencent(symbol, provider or tencent_provider_symbol(symbol), parts, observed_at)
        if item.get("pe_ttm") is None:
            warnings.append(f"tencent_quote_missing_pe:{symbol}:{provider or tencent_provider_symbol(symbol)}")
        items[symbol] = item
    return items, warnings


def fetch_tencent_quote_snapshot(symbols, observed_at=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    selected = normalize_list(symbols)
    if not selected:
        return {}, []
    providers = [tencent_provider_symbol(symbol) for symbol in selected]
    url = f"{TENCENT_QUOTE_URL}{','.join(providers)}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("gbk", "ignore")
    return parse_tencent_quote_text(text, requested_symbols=selected, observed_at=observed_at)


def item_from_yahoo(symbol, provider, result, observed_at):
    summary = result.get("summaryDetail") or {}
    stats = result.get("defaultKeyStatistics") or {}
    financial = result.get("financialData") or {}
    price = result.get("price") or {}
    return {
        "symbol": symbol,
        "market": market_for_symbol(symbol),
        "name": raw_value(price.get("shortName") or price.get("longName")) or symbol,
        "source": "yahoo_quote_summary",
        "provider_symbol": provider,
        "as_of": observed_at,
        "currency": raw_value(price.get("currency")) or "",
        "market_cap": as_float(price.get("marketCap") or summary.get("marketCap")),
        "pe_ttm": as_float(summary.get("trailingPE") or stats.get("trailingPE")),
        "pb": as_float(stats.get("priceToBook")),
        "ps": as_float(stats.get("priceToSalesTrailing12Months")),
        "roe_pct": pct_from_ratio(as_float(financial.get("returnOnEquity"))),
        "revenue_growth_pct": pct_from_ratio(as_float(financial.get("revenueGrowth"))),
        "earnings_growth_pct": pct_from_ratio(as_float(financial.get("earningsGrowth"))),
        "dividend_yield_pct": pct_from_ratio(as_float(summary.get("dividendYield"))),
        "debt_to_equity": ratio_from_percent(as_float(financial.get("debtToEquity"))),
        "summary": f"Yahoo quoteSummary fundamentals snapshot for {provider}.",
    }


def pct_from_ratio(value):
    return round(value * 100.0, 4) if value is not None else None


def ratio_from_percent(value):
    return round(value / 100.0, 4) if value is not None else None


def unique_symbols(primary, fallback=None, limit=DEFAULT_LIMIT):
    result = []
    seen = set()
    source = list(primary or []) or list(fallback or [])
    for symbol in source:
        text = str(symbol or "").strip().upper()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
        if len(result) >= limit:
            break
    return result


def build_snapshot(
    symbols=None,
    watchlist_file=WATCHLIST_FILE,
    fetch_summary=fetch_yahoo_quote_summary,
    fetch_tencent_quotes=fetch_tencent_quote_snapshot,
    now=None,
    limit=DEFAULT_LIMIT,
    sleep_seconds=0.0,
):
    observed_at = (now or datetime.now()).isoformat(timespec="seconds")
    warnings = []
    watchlist_symbols, watchlist_warnings = load_watchlist(watchlist_file) if watchlist_file else ([], [])
    warnings.extend(watchlist_warnings)
    selected = unique_symbols(symbols or watchlist_symbols, fallback=DEFAULT_SYMBOLS, limit=limit)
    items = []
    yahoo_failed = []
    for symbol in selected:
        provider = provider_symbol(symbol)
        try:
            result = fetch_summary(provider)
            items.append(item_from_yahoo(symbol, provider, result, observed_at))
        except Exception as exc:
            warnings.append(f"fetch_failed:{symbol}:{provider}:{exc}")
            yahoo_failed.append(symbol)
        if sleep_seconds:
            time.sleep(sleep_seconds)
    if yahoo_failed and fetch_tencent_quotes:
        try:
            fallback_items, fallback_warnings = fetch_tencent_quotes(yahoo_failed, observed_at=observed_at)
            warnings.extend(fallback_warnings or [])
            for symbol in yahoo_failed:
                item = (fallback_items or {}).get(symbol)
                if item:
                    items.append(item)
                    warnings.append(f"fallback_provider_used:{symbol}:tencent_quote_snapshot_partial")
                else:
                    warnings.append(f"tencent_fetch_missing:{symbol}:{tencent_provider_symbol(symbol)}")
        except Exception as exc:
            warnings.append(f"tencent_fetch_failed:{exc}")
    return {
        "schema": "fundamentals_context_producer_v1",
        "generated_at": observed_at,
        "items": items,
        "warnings": warnings,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_strategy": False,
            "changes_alert_queue": False,
            "output_file": OUTPUT_FILE,
            "provider": "yahoo_quote_summary+tencent_quote_snapshot_fallback",
            "watchlist_file": watchlist_file,
            "symbol_count": len(selected),
        },
    }


def build_text_report(payload):
    return (
        f"Fundamentals producer generated={payload.get('generated_at')} "
        f"items={len(payload.get('items') or [])} "
        f"warnings={len(payload.get('warnings') or [])} "
        f"provider={payload.get('source', {}).get('provider')}"
    )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=OUTPUT_FILE)
    parser.add_argument("--watchlist-file", default=WATCHLIST_FILE)
    parser.add_argument("--symbols", default=os.environ.get("FUNDAMENTALS_CONTEXT_SYMBOLS", ""))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    parser.add_argument("--dry-run", action="store_true", help="fetch and print without writing")
    return parser.parse_args()


def main():
    args = parse_args()
    symbols = parse_symbol_list(args.symbols)
    payload = build_snapshot(
        symbols=symbols or None,
        watchlist_file=args.watchlist_file,
        limit=args.limit,
        sleep_seconds=args.sleep_seconds,
    )
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
