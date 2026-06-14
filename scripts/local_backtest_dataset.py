#!/usr/bin/env python3
"""Build local-only HK/US bar files for backtests.

Raw market data belongs in the operator's local data directory, not in git and
not on the production server by default. This script writes the CSV filenames
expected by the existing backtests and a small metadata file that explains what
was fetched.
"""
import argparse
import csv
import json
import os
import time
from datetime import datetime
from urllib.parse import urlencode

import requests

try:
    from rt_signal_engine_v5 import HK_WATCHLIST, US_WATCHLIST
except ImportError:
    from scripts.rt_signal_engine_v5 import HK_WATCHLIST, US_WATCHLIST


DEFAULT_OUTPUT_DIR = os.environ.get("LOCAL_BACKTEST_DATA_DIR", "/tmp")
DEFAULT_START_DATE = os.environ.get("LOCAL_BACKTEST_START_DATE", "2021-01-01")
DEFAULT_END_DATE = os.environ.get("LOCAL_BACKTEST_END_DATE", datetime.now().date().isoformat())
TENCENT_KLINE_URL = os.environ.get(
    "LOCAL_BACKTEST_TENCENT_KLINE_URL",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
)
ALPACA_DATA_BASE_URL = os.environ.get("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets")
DAILY_FIELDS = ["symbol", "dt", "open_price", "high_price", "low_price", "close_price", "volume"]
BAR_FIELDS = ["symbol", "timestamp", "open_price", "high_price", "low_price", "close_price", "volume"]


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def normalize_symbols(values):
    result = []
    seen = set()
    for value in values or []:
        for item in str(value or "").replace(";", ",").split(","):
            symbol = item.strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                result.append(symbol)
    return result


def valid_ohlc(open_price, high_price, low_price, close_price):
    values = (open_price, high_price, low_price, close_price)
    return all(value is not None and value > 0 for value in values) and (
        high_price >= low_price and low_price <= open_price <= high_price and low_price <= close_price <= high_price
    )


def row_in_range(date_text, start_date, end_date):
    return str(start_date) <= str(date_text) <= str(end_date)


def tencent_daily_url(symbol, count):
    param = f"hk{symbol},day,,,{int(count)},qfq"
    return TENCENT_KLINE_URL + "?" + urlencode({"param": param})


def parse_tencent_daily_rows(symbol, payload, start_date, end_date):
    node = (payload.get("data") or {}).get(f"hk{symbol}") or {}
    raw_rows = node.get("qfqday") or node.get("day") or []
    rows = []
    invalid_count = 0
    for raw in raw_rows:
        if not isinstance(raw, list) or len(raw) < 6:
            invalid_count += 1
            continue
        date_text = str(raw[0])[:10]
        if not row_in_range(date_text, start_date, end_date):
            continue
        try:
            open_price = float(raw[1])
            close_price = float(raw[2])
            high_price = float(raw[3])
            low_price = float(raw[4])
            volume = float(raw[5] or 0)
        except (TypeError, ValueError):
            invalid_count += 1
            continue
        if not valid_ohlc(open_price, high_price, low_price, close_price):
            invalid_count += 1
            continue
        rows.append(
            {
                "symbol": symbol,
                "dt": date_text,
                "open_price": open_price,
                "high_price": high_price,
                "low_price": low_price,
                "close_price": close_price,
                "volume": volume,
            }
        )
    return rows, invalid_count


def fetch_tencent_hk_daily(symbol, start_date, end_date, count=1700, session=None):
    session = session or requests
    try:
        response = session.get(
            tencent_daily_url(symbol, count),
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com"},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return [], {"symbol": symbol, "reason": f"fetch_failed:{exc}"}
    if payload.get("code") != 0:
        return [], {"symbol": symbol, "reason": f"provider_code:{payload.get('code')}:{payload.get('msg')}"}
    rows, invalid_count = parse_tencent_daily_rows(symbol, payload, start_date, end_date)
    if not rows:
        return [], {"symbol": symbol, "reason": "no_valid_rows", "invalid_row_count": invalid_count}
    warning = {"symbol": symbol, "reason": "invalid_rows_skipped", "invalid_row_count": invalid_count} if invalid_count else None
    return rows, warning


def alpaca_credentials(env=None):
    env = env or os.environ
    key = env.get("APCA_API_KEY_ID") or env.get("ALPACA_API_KEY_ID") or env.get("ALPACA_KEY_ID")
    secret = env.get("APCA_API_SECRET_KEY") or env.get("ALPACA_API_SECRET_KEY") or env.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("missing_alpaca_market_data_credentials")
    return key, secret


def alpaca_headers(env=None):
    key, secret = alpaca_credentials(env=env)
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def parse_alpaca_bar(symbol, item, timestamp_field):
    try:
        timestamp = str(item.get("t") or "")
        open_price = float(item.get("o"))
        high_price = float(item.get("h"))
        low_price = float(item.get("l"))
        close_price = float(item.get("c"))
        volume = float(item.get("v") or 0)
    except (TypeError, ValueError):
        return None
    if not timestamp or not valid_ohlc(open_price, high_price, low_price, close_price):
        return None
    key = timestamp[:10] if timestamp_field == "dt" else timestamp
    return {
        "symbol": symbol.upper(),
        timestamp_field: key,
        "open_price": open_price,
        "high_price": high_price,
        "low_price": low_price,
        "close_price": close_price,
        "volume": volume,
    }


def fetch_alpaca_bars(
    symbols,
    start_date,
    end_date,
    timeframe="1Day",
    feed="iex",
    adjustment="all",
    session=None,
    env=None,
    limit=10000,
    timestamp_field="dt",
):
    symbols = normalize_symbols(symbols)
    if not symbols:
        return []
    session = session or requests
    url = ALPACA_DATA_BASE_URL.rstrip("/") + "/v2/stocks/bars"
    params = {
        "symbols": ",".join(symbols),
        "timeframe": timeframe,
        "start": start_date,
        "end": end_date,
        "limit": int(limit),
        "adjustment": adjustment,
        "feed": feed,
    }
    headers = alpaca_headers(env=env)
    rows = []
    page_token = None
    page_count = 0
    while True:
        request_params = dict(params)
        if page_token:
            request_params["page_token"] = page_token
        response = session.get(url, headers=headers, params=request_params, timeout=60)
        if response.status_code != 200:
            raise RuntimeError(f"alpaca_status:{response.status_code}:{str(response.text)[:300]}")
        payload = response.json()
        for symbol, items in (payload.get("bars") or {}).items():
            for item in items or []:
                row = parse_alpaca_bar(symbol, item, timestamp_field)
                if row and row_in_range(row[timestamp_field][:10], start_date, end_date):
                    rows.append(row)
        page_token = payload.get("next_page_token")
        page_count += 1
        if not page_token:
            break
        if page_count > 100:
            raise RuntimeError("alpaca_pagination_limit_exceeded")
        time.sleep(0.2)
    return rows


def write_csv(path, rows, fields):
    rows = sorted(rows, key=lambda row: (row.get("symbol") or "", row.get("dt") or row.get("timestamp") or ""))
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})
    return rows


def coverage(rows, timestamp_field="dt"):
    by_symbol = {}
    for row in rows:
        symbol = row.get("symbol")
        timestamp = row.get(timestamp_field)
        if not symbol or not timestamp:
            continue
        item = by_symbol.setdefault(symbol, {"symbol": symbol, "rows": 0, "first": timestamp, "last": timestamp})
        item["rows"] += 1
        item["first"] = min(item["first"], timestamp)
        item["last"] = max(item["last"], timestamp)
    return sorted(by_symbol.values(), key=lambda row: row["symbol"])


def output_paths(output_dir):
    return {
        "hk_csv": os.path.join(output_dir, "hk_klines_v2.csv"),
        "us_csv": os.path.join(output_dir, "us_klines.csv"),
        "all_csv": os.path.join(output_dir, "all_klines.csv"),
        "metadata_json": os.path.join(output_dir, "hk_us_dataset_metadata.json"),
    }


def build_dataset(args, session=None, env=None):
    output_dir = os.path.abspath(args.output_dir)
    paths = output_paths(output_dir)
    hk_symbols = normalize_symbols(args.hk_symbol) or ([] if args.skip_default_watchlist else list(HK_WATCHLIST))
    us_symbols = normalize_symbols(args.us_symbol) or ([] if args.skip_default_watchlist else list(US_WATCHLIST))

    hk_rows = []
    hk_warnings = []
    if not args.skip_hk:
        for index, symbol in enumerate(hk_symbols, 1):
            rows, warning = fetch_tencent_hk_daily(
                symbol,
                args.start_date,
                args.end_date,
                count=args.tencent_count,
                session=session,
            )
            hk_rows.extend(rows)
            if warning:
                hk_warnings.append(warning)
            if args.fetch_sleep_seconds > 0 and index < len(hk_symbols):
                time.sleep(args.fetch_sleep_seconds)

    us_rows = []
    us_error = None
    if not args.skip_us:
        try:
            us_rows = fetch_alpaca_bars(
                us_symbols,
                args.start_date,
                args.end_date,
                timeframe="1Day",
                feed=args.alpaca_feed,
                adjustment=args.alpaca_adjustment,
                session=session,
                env=env,
                timestamp_field="dt",
            )
        except Exception as exc:
            if args.require_us:
                raise
            us_error = str(exc)

    hk_rows = write_csv(paths["hk_csv"], hk_rows, DAILY_FIELDS)
    us_rows = write_csv(paths["us_csv"], us_rows, DAILY_FIELDS)
    all_rows = write_csv(paths["all_csv"], hk_rows + us_rows, DAILY_FIELDS)

    intraday_outputs = []
    for timeframe in args.us_intraday_timeframe or []:
        rows = fetch_alpaca_bars(
            us_symbols,
            args.intraday_start_date or args.start_date,
            args.intraday_end_date or args.end_date,
            timeframe=timeframe,
            feed=args.alpaca_feed,
            adjustment=args.alpaca_adjustment,
            session=session,
            env=env,
            timestamp_field="timestamp",
        )
        safe_timeframe = timeframe.replace("/", "_")
        path = os.path.join(output_dir, f"us_bars_{safe_timeframe}.csv")
        rows = write_csv(path, rows, BAR_FIELDS)
        intraday_outputs.append(
            {
                "timeframe": timeframe,
                "path": path,
                "row_count": len(rows),
                "symbol_count": len({row["symbol"] for row in rows}),
                "coverage": coverage(rows, timestamp_field="timestamp"),
            }
        )

    metadata = {
        "schema": "hk_us_local_backtest_dataset_v1",
        "generated_at": now_iso(),
        "date_range": {"start": args.start_date, "end": args.end_date},
        "storage_policy": {
            "raw_data_local_only": True,
            "commit_raw_csv_to_git": False,
            "copy_to_server_by_default": False,
        },
        "outputs": paths,
        "sources": {
            "HK": {
                "provider": "tencent_newfqkline",
                "adjustment": "qfq",
                "symbol_count_requested": len(hk_symbols) if not args.skip_hk else 0,
                "row_count": len(hk_rows),
                "warnings": hk_warnings,
            },
            "US": {
                "provider": "alpaca_market_data",
                "feed": args.alpaca_feed,
                "adjustment": args.alpaca_adjustment,
                "symbol_count_requested": len(us_symbols) if not args.skip_us else 0,
                "row_count": len(us_rows),
                "error": us_error,
                "note": "IEX is suitable for quick baselines; use SIP only when subscription and freshness rules allow it.",
            },
        },
        "coverage": {"HK": coverage(hk_rows), "US": coverage(us_rows)},
        "intraday_outputs": intraday_outputs,
        "backtest_inputs": {
            "portfolio_backtest_realistic.py": [paths["hk_csv"], paths["us_csv"]],
            "portfolio_backtest_combined.py": [paths["all_csv"]],
        },
    }
    with open(paths["metadata_json"], "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
    return metadata


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=DEFAULT_END_DATE)
    parser.add_argument("--hk-symbol", action="append", default=[], help="HK symbol or comma-separated HK symbols")
    parser.add_argument("--us-symbol", action="append", default=[], help="US symbol or comma-separated US symbols")
    parser.add_argument("--skip-default-watchlist", action="store_true")
    parser.add_argument("--skip-hk", action="store_true")
    parser.add_argument("--skip-us", action="store_true")
    parser.add_argument("--require-us", action="store_true", help="fail if Alpaca daily fetch fails")
    parser.add_argument("--tencent-count", type=int, default=1700)
    parser.add_argument("--alpaca-feed", default=os.environ.get("ALPACA_DATA_FEED", "iex"))
    parser.add_argument("--alpaca-adjustment", default=os.environ.get("ALPACA_DATA_ADJUSTMENT", "all"))
    parser.add_argument("--us-intraday-timeframe", action="append", default=[])
    parser.add_argument("--intraday-start-date")
    parser.add_argument("--intraday-end-date")
    parser.add_argument("--fetch-sleep-seconds", type=float, default=0.15)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    metadata = build_dataset(args)
    if args.json:
        print(json.dumps(metadata, ensure_ascii=False, indent=2))
    else:
        hk = metadata["sources"]["HK"]
        us = metadata["sources"]["US"]
        print(
            "Local dataset ready: "
            f"HK symbols={len(metadata['coverage']['HK'])} rows={hk['row_count']} "
            f"US symbols={len(metadata['coverage']['US'])} rows={us['row_count']} "
            f"dir={os.path.abspath(args.output_dir)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
