#!/usr/bin/env python3
"""Dry-run-first intraday minute K-line fetch planner.

The default mode fetches current-day minute snapshots and writes a hash-stamped
plan. Applying the plan requires --apply plus a matching --confirm-plan-hash.
Only klines.interval='min' rows are touched; daily bars and trading paths are
never changed by this tool.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8+ normally has zoneinfo.
    ZoneInfo = None


DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
REPORT_FILE = os.environ.get("INTRADAY_KLINE_BATCH_REPORT_FILE", "/tmp/intraday_kline_batch.json")
WATCHLIST_FILE = os.environ.get("RT_SIGNAL_WATCHLIST_FILE", "/root/rt_signal_watchlist.json")
BACKUP_DIR = os.environ.get("INTRADAY_KLINE_BACKUP_DIR", "/tmp/intraday_kline_backups")
TENCENT_MINUTE_URL = os.environ.get(
    "INTRADAY_TENCENT_MINUTE_URL",
    "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}",
)
FETCH_TIMEOUT_SECONDS = float(os.environ.get("INTRADAY_KLINE_FETCH_TIMEOUT_SECONDS", "10"))
FETCH_SLEEP_SECONDS = float(os.environ.get("INTRADAY_KLINE_FETCH_SLEEP_SECONDS", "0.15"))
MAX_SYMBOLS_PER_MARKET = int(os.environ.get("INTRADAY_KLINE_MAX_SYMBOLS_PER_MARKET", "80"))
DATA_SOURCE = "tencent_minute_query"


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    if not path:
        return
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


def run_cmd(args, timeout=120, input_text=None):
    try:
        return subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": str(exc)})()


def psql(sql, timeout=120):
    return run_cmd(
        [
            "docker",
            "exec",
            DB_CONTAINER,
            "psql",
            "-U",
            DB_USER,
            "-d",
            DB_NAME,
            "-t",
            "-A",
            "-F",
            "\t",
            "-c",
            sql,
        ],
        timeout=timeout,
    )


_TABLE_COLUMNS_CACHE = {}


def table_columns(table):
    table = str(table)
    if table in _TABLE_COLUMNS_CACHE:
        return _TABLE_COLUMNS_CACHE[table]
    sql = f"""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = '{sql_quote(table)}'
    """
    result = psql(sql, timeout=30)
    if result.returncode != 0:
        _TABLE_COLUMNS_CACHE[table] = set()
        return set()
    cols = {row[0] for row in rows(result.stdout) if row}
    _TABLE_COLUMNS_CACHE[table] = cols
    return cols


def rows(stdout):
    return [line.rstrip("\n").split("\t") for line in str(stdout or "").splitlines() if line.strip()]


def sql_quote(value):
    return str(value).replace("'", "''")


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def round_price(value):
    return round(float(value), 6)


def normalize_symbol_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[\s,;]+", value)
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = [value]
    result = []
    seen = set()
    for item in raw_items:
        symbol = str(item or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return result


def market_for_symbol(symbol):
    text = str(symbol or "").strip().upper()
    if text.startswith("HK:"):
        return "HK", text.split(":", 1)[1]
    if text.startswith("US:"):
        return "US", text.split(":", 1)[1]
    if text.isdigit() and len(text) == 5:
        return "HK", text
    return "US", text


def symbols_from_watchlist_payload(payload, market):
    if not isinstance(payload, dict):
        return []
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
        symbols = normalize_symbol_list(candidate)
        if symbols:
            return symbols
    return []


def load_watchlist_symbols(path=WATCHLIST_FILE):
    if not path:
        return {"HK": [], "US": []}, ["watchlist_file_not_configured"]
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        return {"HK": [], "US": []}, [f"watchlist_file_unreadable:{path}:{exc}"]
    if not isinstance(payload, dict):
        return {"HK": [], "US": []}, [f"watchlist_file_invalid:{path}"]
    return {
        "HK": symbols_from_watchlist_payload(payload, "HK"),
        "US": symbols_from_watchlist_payload(payload, "US"),
    }, []


def fetch_active_symbols(limit_per_market=MAX_SYMBOLS_PER_MARKET):
    sql = f"""
        SELECT CASE WHEN exchange = 'HKEX' THEN 'HK' ELSE 'US' END AS market,
               symbol
        FROM stocks
        WHERE is_active = true
          AND exchange IN ('HKEX','NASDAQ','NYSE')
        ORDER BY market, symbol
    """
    result = psql(sql)
    if result.returncode != 0:
        return {"HK": [], "US": []}, [f"active_symbol_query_failed:{result.stderr.strip()}"]
    symbols = {"HK": [], "US": []}
    for row in rows(result.stdout):
        if len(row) < 2:
            continue
        market = "HK" if row[0] == "HK" else "US"
        if len(symbols[market]) < limit_per_market:
            symbols[market].append(str(row[1] or "").strip().upper())
    return symbols, []


def merge_symbols_by_market(symbols_by_market):
    return {
        "HK": normalize_symbol_list((symbols_by_market or {}).get("HK"))[:MAX_SYMBOLS_PER_MARKET],
        "US": normalize_symbol_list((symbols_by_market or {}).get("US"))[:MAX_SYMBOLS_PER_MARKET],
    }


def symbols_from_cli(values):
    result = {"HK": [], "US": []}
    for value in values or []:
        for raw in normalize_symbol_list(value):
            market, symbol = market_for_symbol(raw)
            if symbol:
                result[market].append(symbol)
    return merge_symbols_by_market(result)


def timezone_for_market(market):
    market = str(market or "").upper()
    if ZoneInfo:
        try:
            return ZoneInfo("America/New_York") if market == "US" else ZoneInfo("Asia/Hong_Kong")
        except Exception:
            pass
    return timezone(timedelta(hours=-5 if market == "US" else 8))


def local_observed_at(market, observed_at=None):
    tz = timezone_for_market(market)
    if observed_at is None:
        return datetime.now(tz)
    if observed_at.tzinfo:
        return observed_at.astimezone(tz)
    return observed_at.replace(tzinfo=tz)


def market_fetch_state(market, observed_at=None):
    local_dt = local_observed_at(market, observed_at=observed_at)
    open_dt = local_dt.replace(hour=9, minute=30, second=0, microsecond=0)
    if local_dt.weekday() >= 5:
        return {
            "fetchable": False,
            "reason": "market_closed_weekend",
            "local_observed_at": local_dt.isoformat(timespec="seconds"),
            "market_date": local_dt.date().isoformat(),
        }
    if local_dt < open_dt:
        return {
            "fetchable": False,
            "reason": "market_not_open_yet",
            "local_observed_at": local_dt.isoformat(timespec="seconds"),
            "market_date": local_dt.date().isoformat(),
        }
    return {
        "fetchable": True,
        "reason": "market_session_started",
        "local_observed_at": local_dt.isoformat(timespec="seconds"),
        "market_date": local_dt.date().isoformat(),
    }


def parse_hhmm_timestamp(value, market, observed_at=None):
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}", text):
        return None
    hour = int(text[:2])
    minute = int(text[2:])
    if hour > 23 or minute > 59:
        return None
    state = market_fetch_state(market, observed_at=observed_at)
    if not state["fetchable"]:
        return None
    base = local_observed_at(market, observed_at=observed_at)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0, tzinfo=None)


def parse_provider_line(line, market, observed_at=None):
    parts = str(line or "").split()
    if len(parts) < 3:
        return None, "invalid_minute_line"
    timestamp = parse_hhmm_timestamp(parts[0], market, observed_at=observed_at)
    price = as_float(parts[1])
    cumulative_volume = as_float(parts[2])
    cumulative_amount = as_float(parts[3]) if len(parts) >= 4 else None
    if timestamp is None:
        return None, "invalid_minute_timestamp"
    if price is None or price <= 0:
        return None, "invalid_minute_price"
    if cumulative_volume is None or cumulative_volume < 0:
        return None, "invalid_minute_volume"
    return (
        {
            "timestamp": timestamp.isoformat(sep=" ", timespec="seconds"),
            "price": round_price(price),
            "cumulative_volume": round(float(cumulative_volume), 4),
            "cumulative_amount": round(float(cumulative_amount), 4) if cumulative_amount is not None else None,
        },
        None,
    )


def rows_from_provider_lines(lines, symbol, market, source_code, observed_at=None):
    parsed = []
    warnings = []
    previous_volume = None
    previous_amount = None
    for line in lines or []:
        item, error = parse_provider_line(line, market, observed_at=observed_at)
        if error:
            warnings.append(f"{source_code}:{error}:{str(line)[:40]}")
            continue
        minute_volume = item["cumulative_volume"]
        if previous_volume is not None and item["cumulative_volume"] >= previous_volume:
            minute_volume = item["cumulative_volume"] - previous_volume
        minute_amount = item["cumulative_amount"]
        if (
            item["cumulative_amount"] is not None
            and previous_amount is not None
            and item["cumulative_amount"] >= previous_amount
        ):
            minute_amount = item["cumulative_amount"] - previous_amount
        price = item["price"]
        parsed.append(
            {
                "symbol": symbol,
                "market": market,
                "timestamp": item["timestamp"],
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": round(float(minute_volume or 0.0), 4),
                "amount": round(float(minute_amount or 0.0), 4) if minute_amount is not None else 0.0,
                "change_percent": 0.0,
                "data_source": DATA_SOURCE,
                "source_code": source_code,
                "source_granularity": "minute_snapshot_price",
                "source_volume_type": "cumulative_delta",
            }
        )
        previous_volume = item["cumulative_volume"]
        if item["cumulative_amount"] is not None:
            previous_amount = item["cumulative_amount"]
    return parsed, warnings


def parse_tencent_minute_response(text, source_code, symbol, market, observed_at=None):
    warnings = []
    try:
        payload = json.loads(text)
    except Exception as exc:
        return [], [f"{source_code}:json_parse_failed:{exc}"]
    if payload.get("code") not in (0, "0", None):
        warnings.append(f"{source_code}:provider_code:{payload.get('code')}:{payload.get('msg')}")
    node = (payload.get("data") or {}).get(source_code)
    if not isinstance(node, dict):
        return [], warnings + [f"{source_code}:missing_symbol_node"]
    data_parent = node.get("data") if isinstance(node.get("data"), dict) else {}
    lines = data_parent.get("data") if isinstance(data_parent, dict) else []
    if not isinstance(lines, list):
        return [], warnings + [f"{source_code}:minute_lines_invalid"]
    rows_out, parse_warnings = rows_from_provider_lines(
        lines,
        symbol=symbol,
        market=market,
        source_code=source_code,
        observed_at=observed_at,
    )
    return rows_out, warnings + parse_warnings


def provider_symbol_candidates(symbol, market):
    symbol = str(symbol or "").strip().upper()
    if market == "HK":
        return [f"hk{symbol}"]
    base = symbol.split(".", 1)[0]
    candidates = [f"us{base}", f"us{base}.OQ", f"us{base}.N"]
    seen = []
    for item in candidates:
        if item not in seen:
            seen.append(item)
    return seen


def fetch_tencent_minute_rows(symbol, market, timeout=FETCH_TIMEOUT_SECONDS, observed_at=None):
    warnings = []
    attempts = []
    for source_code in provider_symbol_candidates(symbol, market):
        url = TENCENT_MINUTE_URL.format(code=source_code)
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8", "ignore")
        except Exception as exc:
            warnings.append(f"{source_code}:fetch_failed:{exc}")
            attempts.append({"source_code": source_code, "status": "fetch_failed", "error": str(exc)})
            continue
        parsed, parse_warnings = parse_tencent_minute_response(
            text,
            source_code=source_code,
            symbol=symbol,
            market=market,
            observed_at=observed_at,
        )
        warnings.extend(parse_warnings)
        attempts.append({"source_code": source_code, "status": "has_rows" if parsed else "empty", "row_count": len(parsed)})
        if parsed:
            return parsed, warnings, attempts
    return [], warnings, attempts


def parse_timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("T", " ").replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def valid_ohlc(row):
    o = as_float(row.get("open"))
    h = as_float(row.get("high"))
    low = as_float(row.get("low"))
    c = as_float(row.get("close"))
    if any(value is None or value <= 0 for value in (o, h, low, c)):
        return False
    return h >= low and low <= o <= h and low <= c <= h


def normalize_kline_row(row):
    timestamp = parse_timestamp(row.get("timestamp"))
    if timestamp is None:
        raise ValueError("invalid_timestamp")
    if not valid_ohlc(row):
        raise ValueError("invalid_ohlc")
    close = as_float(row.get("close"))
    volume = as_float(row.get("volume"), 0.0) or 0.0
    amount = as_float(row.get("amount"), 0.0) or 0.0
    return {
        "timestamp": timestamp.isoformat(sep=" ", timespec="seconds"),
        "open": round_price(row.get("open")),
        "high": round_price(row.get("high")),
        "low": round_price(row.get("low")),
        "close": round_price(close),
        "volume": round(float(volume), 4),
        "amount": round(float(amount), 4),
        "change_percent": round(float(as_float(row.get("change_percent"), 0.0) or 0.0), 6),
        "data_source": str(row.get("data_source") or DATA_SOURCE),
        "source_code": row.get("source_code"),
        "source_granularity": row.get("source_granularity") or "minute_snapshot_price",
    }


def plan_action(symbol, market, source_rows, attempts=None):
    valid_rows = []
    invalid_rows = []
    seen = set()
    for raw in source_rows or []:
        try:
            row = normalize_kline_row(raw)
        except ValueError as exc:
            invalid_rows.append({"timestamp": raw.get("timestamp"), "reason": str(exc)})
            continue
        key = row["timestamp"]
        if key in seen:
            invalid_rows.append({"timestamp": key, "reason": "duplicate_provider_timestamp"})
            continue
        seen.add(key)
        valid_rows.append(row)
    valid_rows = sorted(valid_rows, key=lambda item: item["timestamp"])
    if not valid_rows:
        return None, {
            "symbol": symbol,
            "market": market,
            "reason": "minute_source_rows_missing",
            "source_attempts": attempts or [],
            "invalid_source_rows": invalid_rows[:20],
        }
    return (
        {
            "action": "upsert_intraday_minute_klines",
            "symbol": symbol,
            "market": market,
            "interval": "min",
            "source": "tencent_minute_query",
            "source_code": valid_rows[-1].get("source_code"),
            "source_granularity": "minute_snapshot_price",
            "source_limitation": "provider returns one price point per minute; OHLC high/low are not independently observed",
            "row_count": len(valid_rows),
            "first_timestamp": valid_rows[0]["timestamp"],
            "latest_timestamp": valid_rows[-1]["timestamp"],
            "rows": valid_rows,
            "source_attempts": attempts or [],
            "invalid_source_rows": invalid_rows[:20],
        },
        None,
    )


def plan_hash(actions):
    stable = json.dumps(actions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def report_status(actions, unresolved, warnings):
    if actions and unresolved:
        return "PARTIAL"
    if actions:
        return "ACTIONABLE"
    if unresolved:
        return "UNRESOLVED"
    if warnings:
        return "WARN"
    return "OK"


def build_recommendations(status, actions, unresolved, skipped=None):
    if status == "OK":
        if skipped:
            return ["wait_for_market_session_before_intraday_minute_fetch"]
        return ["intraday_minute_fetch_plan_not_required_or_no_symbols"]
    recs = []
    if actions:
        recs.append("operator_may_apply_hash_confirmed_intraday_minute_plan_after_review")
        recs.append("rerun_intraday_context_after_intraday_minute_apply")
    if unresolved:
        recs.append("review_intraday_minute_provider_coverage_or_symbol_mapping")
    if any(action.get("market") == "US" and action.get("row_count", 0) < 30 for action in actions):
        recs.append("treat_sparse_us_minute_rows_as_execution_quality_only")
    return recs or ["inspect_intraday_minute_fetch_warnings"]


def manual_apply_command(plan_hash_value, output="/tmp/intraday_kline_batch_apply.json"):
    return (
        "/usr/bin/python3 /root/intraday_kline_batch.py "
        f"--output {output} "
        "--apply "
        f"--confirm-plan-hash {plan_hash_value} "
        "--text"
    )


def resolve_symbols(symbols_by_market=None, symbols=None, watchlist_file=WATCHLIST_FILE, fallback_active=True):
    warnings = []
    if symbols_by_market is not None:
        return merge_symbols_by_market(symbols_by_market), warnings
    cli_symbols = symbols_from_cli(symbols)
    if cli_symbols["HK"] or cli_symbols["US"]:
        return cli_symbols, warnings
    watchlist, watch_warnings = load_watchlist_symbols(watchlist_file)
    warnings.extend(watch_warnings)
    watchlist = merge_symbols_by_market(watchlist)
    if watchlist["HK"] or watchlist["US"] or not fallback_active:
        return watchlist, warnings
    active, active_warnings = fetch_active_symbols(limit_per_market=MAX_SYMBOLS_PER_MARKET)
    warnings.extend(active_warnings)
    return merge_symbols_by_market(active), warnings


def build_report(
    symbols_by_market=None,
    symbols=None,
    watchlist_file=WATCHLIST_FILE,
    fetcher=None,
    observed_at=None,
    fallback_active=True,
    fetch_sleep_seconds=FETCH_SLEEP_SECONDS,
):
    selected, warnings = resolve_symbols(
        symbols_by_market=symbols_by_market,
        symbols=symbols,
        watchlist_file=watchlist_file,
        fallback_active=fallback_active,
    )
    fetcher = fetcher or fetch_tencent_minute_rows
    actions = []
    unresolved = []
    skipped = []
    for market in ("HK", "US"):
        state = market_fetch_state(market, observed_at=observed_at)
        if not state["fetchable"]:
            for symbol in selected.get(market) or []:
                skipped.append(
                    {
                        "symbol": symbol,
                        "market": market,
                        "reason": state["reason"],
                        "local_observed_at": state["local_observed_at"],
                        "market_date": state["market_date"],
                    }
                )
            continue
        for symbol in selected.get(market) or []:
            fetched = fetcher(symbol, market, observed_at=observed_at)
            if len(fetched) == 2:
                source_rows, fetch_warnings = fetched
                attempts = []
            else:
                source_rows, fetch_warnings, attempts = fetched
            warnings.extend(fetch_warnings)
            action, issue = plan_action(symbol, market, source_rows, attempts=attempts)
            if action:
                actions.append(action)
            elif issue:
                unresolved.append(issue)
            if fetch_sleep_seconds:
                time.sleep(fetch_sleep_seconds)
    digest = plan_hash(actions)
    status = report_status(actions, unresolved, warnings)
    return {
        "schema": "intraday_kline_batch_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "mode": "dry-run",
        "plan_hash": digest,
        "source": {
            "dry_run_default": True,
            "provider": "tencent_minute_query",
            "provider_contract": "unofficial_public_web_endpoint_unversioned_best_effort",
            "submits_orders": False,
            "changes_strategy": False,
            "changes_alert_queue": False,
            "changes_crontab": False,
            "repairs_daily_klines": False,
            "updates_only": ["klines interval=min rows when --apply is hash-confirmed"],
            "watchlist_file": watchlist_file,
            "max_symbols_per_market": MAX_SYMBOLS_PER_MARKET,
        },
        "summary": {
            "market_count": len([market for market in ("HK", "US") if selected.get(market)]),
            "requested_symbol_count": sum(len(selected.get(market) or []) for market in ("HK", "US")),
            "action_count": len(actions),
            "planned_row_count": sum(action.get("row_count", 0) for action in actions),
            "unresolved_count": len(unresolved),
            "skipped_symbol_count": len(skipped),
            "invalid_source_row_count": sum(len(action.get("invalid_source_rows") or []) for action in actions)
            + sum(len(item.get("invalid_source_rows") or []) for item in unresolved),
            "hk_action_count": len([action for action in actions if action.get("market") == "HK"]),
            "us_action_count": len([action for action in actions if action.get("market") == "US"]),
            "sparse_us_action_count": len(
                [action for action in actions if action.get("market") == "US" and action.get("row_count", 0) < 30]
            ),
        },
        "selected_symbols": selected,
        "recommendations": build_recommendations(status, actions, unresolved, skipped=skipped),
        "actions": actions,
        "unresolved": unresolved,
        "skipped": skipped[:200],
        "warnings": warnings[:100],
        "apply_contract": {
            "dry_run_default": True,
            "apply_requires": "--apply --confirm-plan-hash <plan_hash>",
            "backs_up_existing_rows_before_apply": True,
            "does_not_submit_orders": True,
            "does_not_change_crontab": True,
            "does_not_change_watchlists": True,
            "does_not_change_strategy": True,
            "repairs_daily_klines": False,
            "updates": ["klines interval=min rows for planned symbols/timestamps only"],
            "manual_apply_command": manual_apply_command(digest) if actions else None,
            "post_apply_verification_commands": [
                "/usr/bin/python3 /root/intraday_context_report.py --output /tmp/intraday_context_report.json --text",
                "/usr/bin/python3 /root/data_health_report.py --output /tmp/data_health_report.json --text",
                "/usr/bin/python3 /root/source_reliability_report.py --output /tmp/source_reliability_report.json --text",
                "/usr/bin/python3 /root/execution_readiness_report.py --output /tmp/execution_readiness_report.json --text",
            ],
        },
        "hermes_use": [
            "Use this report to decide whether minute K-line collection is available and reviewable.",
            "Do not treat this dry-run plan as evidence that DB minute rows already exist.",
            "After hash-confirmed apply, use intraday_context_report.py as the compact Hermes decision digest.",
            "Do not use minute rows to relax daily data-health, strategy-evidence, or readiness blocks.",
        ],
    }


def backup_current_rows(actions, backup_dir=BACKUP_DIR):
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(backup_dir, f"intraday_kline_{stamp}.json")
    pairs = []
    for action in actions or []:
        symbol = action.get("symbol")
        for row in action.get("rows") or []:
            pairs.append((symbol, row.get("timestamp")))
    if not pairs:
        save_json_atomic(path, {"generated_at": now_iso(), "rows": []})
        return path
    values = ", ".join(
        f"('{sql_quote(symbol)}'::text, '{sql_quote(ts)}'::timestamp)"
        for symbol, ts in sorted(set(pairs))
        if symbol and ts
    )
    query = f"""
        WITH targets(symbol, ts) AS (VALUES {values})
        SELECT COALESCE(jsonb_agg(row_to_json(k)), '[]'::jsonb)::text
        FROM klines k
        JOIN targets t ON t.symbol = k.symbol AND t.ts = k.timestamp
        WHERE k.interval = 'min'
    """
    result = psql(query)
    if result.returncode != 0:
        raise RuntimeError(f"backup query failed: {result.stderr.strip()}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(result.stdout.strip() or "[]")
        f.write("\n")
    return path


def sql_values_for_action(action, include_source_granularity=False):
    values = []
    for row in action.get("rows") or []:
        source = sql_quote(row.get("data_source") or action.get("source") or DATA_SOURCE)
        source_granularity = sql_quote(
            row.get("source_granularity") or action.get("source_granularity") or "minute_snapshot_price"
        )
        provenance_values = f"'{source}','{source_granularity}',NOW()" if include_source_granularity else f"'{source}',NOW()"
        values.append(
            "('{symbol}','min','{timestamp}',{open},{high},{low},{close},{volume},{amount},{change_percent},{provenance_values})".format(
                symbol=sql_quote(action["symbol"]),
                timestamp=sql_quote(row["timestamp"]),
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                amount=row["amount"],
                change_percent=row["change_percent"],
                provenance_values=provenance_values,
            )
        )
    return values


def sql_for_action(action, kline_columns=None):
    include_source_granularity = "source_granularity" in set(kline_columns or [])
    values = sql_values_for_action(action, include_source_granularity=include_source_granularity)
    if not values:
        return ""
    source_granularity_insert = ", source_granularity" if include_source_granularity else ""
    source_granularity_update = (
        "            source_granularity = EXCLUDED.source_granularity,\n"
        if include_source_granularity
        else ""
    )
    return """
        INSERT INTO klines (
            symbol, interval, timestamp, open_price, high_price, low_price, close_price,
            volume, amount, change_percent, data_source{source_granularity_insert}, created_at
        )
        VALUES {values}
        ON CONFLICT (symbol, interval, timestamp) DO UPDATE SET
            open_price = EXCLUDED.open_price,
            high_price = EXCLUDED.high_price,
            low_price = EXCLUDED.low_price,
            close_price = EXCLUDED.close_price,
            volume = EXCLUDED.volume,
            amount = EXCLUDED.amount,
            change_percent = EXCLUDED.change_percent,
            data_source = EXCLUDED.data_source,
{source_granularity_update}            created_at = NOW();
    """.format(
        values=",\n".join(values),
        source_granularity_insert=source_granularity_insert,
        source_granularity_update=source_granularity_update,
    )


def build_sql_script(actions, kline_columns=None):
    statements = ["BEGIN;"]
    for action in actions or []:
        statement = sql_for_action(action, kline_columns=kline_columns).strip()
        if statement:
            statements.append(statement)
    statements.append("COMMIT;")
    return "\n".join(statements) + "\n"


def apply_actions(actions, backup_dir=BACKUP_DIR):
    if not actions:
        return {"status": "noop", "reason": "no_actions"}
    backup_file = backup_current_rows(actions, backup_dir=backup_dir)
    script = build_sql_script(actions, kline_columns=table_columns("klines"))
    result = run_cmd(
        [
            "docker",
            "exec",
            "-i",
            DB_CONTAINER,
            "psql",
            "-U",
            DB_USER,
            "-d",
            DB_NAME,
            "-v",
            "ON_ERROR_STOP=1",
        ],
        input_text=script,
        timeout=180,
    )
    return {
        "status": "applied" if result.returncode == 0 else "failed",
        "backup_file": backup_file,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }


def build_text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Intraday K-line batch {payload['generated_at']}",
        (
            f"status={payload.get('status')} mode={payload.get('mode')} plan_hash={payload.get('plan_hash')} "
            f"symbols={summary.get('requested_symbol_count')} actions={summary.get('action_count')} "
            f"rows={summary.get('planned_row_count')} unresolved={summary.get('unresolved_count')}"
        ),
    ]
    for action in payload.get("actions", [])[:20]:
        lines.append(
            "  upsert-min {symbol} market={market} rows={rows} first={first} latest={latest} source={source}".format(
                symbol=action.get("symbol"),
                market=action.get("market"),
                rows=action.get("row_count"),
                first=action.get("first_timestamp"),
                latest=action.get("latest_timestamp"),
                source=action.get("source_code"),
            )
        )
    if payload.get("unresolved"):
        lines.append("unresolved=" + json.dumps(payload["unresolved"][:10], ensure_ascii=False))
    if payload.get("warnings"):
        lines.append("warnings=" + json.dumps(payload["warnings"][:8], ensure_ascii=False))
    if payload.get("apply_result"):
        lines.append("apply_result=" + json.dumps(payload["apply_result"], ensure_ascii=False))
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--watchlist-file", default=WATCHLIST_FILE)
    parser.add_argument("--symbol", action="append", default=[], help="symbol or MARKET:symbol; repeatable")
    parser.add_argument("--no-active-fallback", action="store_true", help="do not fall back to active stocks when watchlist is empty")
    parser.add_argument("--apply", action="store_true", help="apply current plan after hash confirmation")
    parser.add_argument("--confirm-plan-hash", default="", help="required with --apply")
    parser.add_argument("--backup-dir", default=BACKUP_DIR)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    payload = build_report(
        symbols=args.symbol,
        watchlist_file=args.watchlist_file,
        fallback_active=not args.no_active_fallback,
    )
    if args.apply:
        if not args.confirm_plan_hash or args.confirm_plan_hash != payload["plan_hash"]:
            payload["apply_result"] = {
                "status": "rejected",
                "reason": "confirm_plan_hash_missing_or_mismatch",
                "expected_plan_hash": payload["plan_hash"],
            }
            payload["status"] = "APPLY_REJECTED"
            exit_code = 2
        else:
            payload["mode"] = "apply"
            payload["apply_result"] = apply_actions(payload.get("actions") or [], backup_dir=args.backup_dir)
            payload["status"] = "APPLIED" if payload["apply_result"].get("status") in ("applied", "noop") else "APPLY_FAILED"
            exit_code = 0 if payload["status"] == "APPLIED" else 2
    else:
        exit_code = 0
    save_json_atomic(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.text:
        print(build_text_report(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
