#!/usr/bin/env python3
"""Hash-confirmed repair tool for minute-fresh but daily-stale K-line gaps.

Dry-run is the default. Applying requires both --apply and a matching
--confirm-plan-hash value from the current report.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

try:
    import data_health_report as data_health
except ImportError:
    from scripts import data_health_report as data_health


DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
REPORT_FILE = os.environ.get("KLINE_DAILY_GAP_REPAIR_FILE", "/tmp/kline_daily_gap_repair.json")
BACKUP_DIR = os.environ.get("KLINE_DAILY_GAP_BACKUP_DIR", "/tmp/kline_daily_gap_backups")
FETCH_COUNT = int(os.environ.get("KLINE_DAILY_GAP_FETCH_COUNT", "800"))
FETCH_WORKERS = int(os.environ.get("KLINE_DAILY_GAP_FETCH_WORKERS", "8"))
DATA_SOURCE = "tencent_day_repair"


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


def run_cmd(args, timeout=120):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
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


def rows(stdout):
    return [line.rstrip("\n").split("\t") for line in stdout.splitlines() if line.strip()]


def sql_quote(value):
    return str(value).replace("'", "''")


def parse_date(value):
    text = str(value or "")[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return text
    except ValueError:
        return None


def min_date(*values):
    parsed = [parse_date(value) for value in values]
    parsed = [value for value in parsed if value]
    return min(parsed) if parsed else None


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


def round_price(value):
    return round(float(value), 6)


def normalize_source_row(row):
    return {
        "date": parse_date(row.get("date")),
        "open": round_price(row.get("open")),
        "high": round_price(row.get("high")),
        "low": round_price(row.get("low")),
        "close": round_price(row.get("close")),
        "volume": as_int(row.get("volume"), 0),
        "amount": round_price(row.get("amount", 0.0)),
        "change_percent": round_price(row.get("change_percent", 0.0)),
    }


def kline_errors(row):
    return data_health.latest_ohlc_errors(
        {
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
        }
    )


def fetch_gap_candidates(symbols=None, limit=None):
    symbol_filter = ""
    if symbols:
        quoted = ", ".join(f"'{sql_quote(symbol)}'" for symbol in sorted(set(symbols)))
        symbol_filter = f"AND symbol IN ({quoted})"
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    sql = f"""
        WITH active_all AS (
            SELECT CASE WHEN exchange = 'HKEX' THEN 'HK' ELSE 'US' END AS market,
                   exchange,
                   symbol
            FROM stocks
            WHERE is_active = true
              AND exchange IN ('HKEX','NASDAQ','NYSE')
        ),
        active AS (
            SELECT *
            FROM active_all
            WHERE true
              {symbol_filter}
        ),
        daily AS (
            SELECT a.symbol,
                   count(k.*) AS day_kline_count,
                   min(k.timestamp::date) AS earliest_daily_date,
                   max(k.timestamp::date) AS latest_daily_date
            FROM active a
            LEFT JOIN klines k ON k.symbol = a.symbol AND k.interval = 'day'
            GROUP BY a.symbol
        ),
        minute AS (
            SELECT a.symbol,
                   count(k.*) AS minute_kline_count,
                   max(k.timestamp::date) AS latest_minute_date
            FROM active a
            LEFT JOIN klines k ON k.symbol = a.symbol AND k.interval = 'min'
            GROUP BY a.symbol
        ),
        market_latest AS (
            SELECT a.market, max(k.timestamp::date) AS market_latest_daily_date
            FROM active_all a
            JOIN klines k ON k.symbol = a.symbol AND k.interval = 'day'
            GROUP BY a.market
        )
        SELECT a.market, a.exchange, a.symbol,
               COALESCE(d.day_kline_count, 0),
               d.earliest_daily_date,
               d.latest_daily_date,
               COALESCE(m.minute_kline_count, 0),
               m.latest_minute_date,
               ml.market_latest_daily_date
        FROM active a
        LEFT JOIN daily d ON d.symbol = a.symbol
        LEFT JOIN minute m ON m.symbol = a.symbol
        LEFT JOIN market_latest ml ON ml.market = a.market
        WHERE m.latest_minute_date IS NOT NULL
          AND (d.latest_daily_date IS NULL OR m.latest_minute_date > d.latest_daily_date)
        ORDER BY a.market, d.latest_daily_date NULLS FIRST, a.symbol
        {limit_clause}
    """
    r = psql(sql)
    if r.returncode != 0:
        raise RuntimeError(f"daily gap query failed: {r.stderr.strip()}")
    parsed = []
    for row in rows(r.stdout):
        if len(row) < 9:
            continue
        latest_daily = parse_date(row[5])
        latest_minute = parse_date(row[7])
        market_latest = parse_date(row[8])
        target_end = min_date(latest_minute, market_latest) or latest_minute
        parsed.append(
            {
                "market": row[0],
                "exchange": row[1],
                "symbol": row[2],
                "day_kline_count": as_int(row[3]),
                "earliest_daily_date": parse_date(row[4]),
                "latest_daily_date": latest_daily,
                "minute_kline_count": as_int(row[6]),
                "latest_minute_date": latest_minute,
                "market_latest_daily_date": market_latest,
                "target_end_date": target_end,
            }
        )
    return parsed


def tencent_symbol_candidates(candidate):
    symbol = candidate["symbol"]
    if candidate.get("exchange") == "HKEX":
        return [("hk", symbol)]
    return [("us", f"{symbol}.OQ"), ("us", f"{symbol}.N"), ("us", symbol)]


def parse_tencent_kline(raw):
    if len(raw) < 6:
        return None
    open_price = as_float(raw[1])
    close = as_float(raw[2])
    high = as_float(raw[3])
    low = as_float(raw[4])
    volume = as_float(raw[5], 0.0)
    if None in (open_price, close, high, low):
        return None
    return {
        "date": raw[0],
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": close * volume if close is not None and volume is not None else 0.0,
        "change_percent": ((close - open_price) / open_price * 100) if open_price and open_price > 0 else 0.0,
    }


def fetch_tencent_day_rows(candidate, count=FETCH_COUNT):
    warnings = []
    attempts = []
    for market, code in tencent_symbol_candidates(candidate):
        url = f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param={market}{code},day,,,{count},qfq"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com"},
            )
            with urllib.request.urlopen(req, timeout=15) as response:
                payload = json.loads(response.read().decode())
        except Exception as exc:
            warnings.append(f"fetch_failed:{market}{code}:{exc}")
            attempts.append({"source_code": f"{market}{code}", "status": "fetch_failed", "error": str(exc)})
            continue
        node = (payload.get("data") or {}).get(f"{market}{code}") or {}
        raw_rows = node.get("qfqday") or node.get("day") or []
        parsed = [parse_tencent_kline(item) for item in raw_rows]
        parsed = [item for item in parsed if item and parse_date(item.get("date"))]
        attempt = source_coverage(f"{market}{code}", parsed)
        attempts.append(attempt)
        if parsed:
            for item in parsed:
                item["source_code"] = f"{market}{code}"
                item["source_attempts"] = attempts
            return sorted(parsed, key=lambda item: item["date"]), warnings, attempts
        warnings.append(f"source_empty:{market}{code}")
    return [], warnings, attempts


def source_coverage(source_code, source_rows):
    dates = sorted(parse_date(item.get("date")) for item in source_rows if parse_date(item.get("date")))
    return {
        "source_code": source_code,
        "status": "has_rows" if dates else "empty",
        "row_count": len(dates),
        "earliest_source_date": dates[0] if dates else None,
        "latest_source_date": dates[-1] if dates else None,
    }


def source_attempts_from_rows(source_rows):
    if source_rows and isinstance(source_rows[0].get("source_attempts"), list):
        return source_rows[0]["source_attempts"]
    by_source = {}
    for row in source_rows:
        source_code = row.get("source_code") or "unknown"
        by_source.setdefault(source_code, []).append(row)
    return [source_coverage(source_code, rows) for source_code, rows in sorted(by_source.items())]


def source_diagnostic(candidate, source_rows, invalid_rows=None, source_attempts=None):
    latest_daily = candidate.get("latest_daily_date")
    target_end = candidate.get("target_end_date")
    attempts = source_attempts if source_attempts is not None else source_attempts_from_rows(source_rows)
    source_latest_dates = [item.get("latest_source_date") for item in attempts if item.get("latest_source_date")]
    latest_source_date = max(source_latest_dates) if source_latest_dates else None
    return {
        "source_attempts": attempts,
        "latest_source_date": latest_source_date,
        "source_reaches_target_end": bool(latest_source_date and target_end and latest_source_date >= target_end),
        "source_after_latest_daily": bool(latest_source_date and latest_daily and latest_source_date > latest_daily),
        "source_row_count": len(source_rows),
        "invalid_source_rows": (invalid_rows or [])[:10],
    }


def plan_action(candidate, source_rows, source_attempts=None):
    latest_daily = candidate.get("latest_daily_date")
    target_end = candidate.get("target_end_date")
    if not target_end:
        return None, {"symbol": candidate.get("symbol"), "reason": "target_end_date_missing"}
    if latest_daily and latest_daily >= target_end:
        return None, {
            "symbol": candidate.get("symbol"),
            "reason": "daily_catches_up_to_safe_target",
            "latest_daily_date": latest_daily,
            "target_end_date": target_end,
        }

    normalized_rows = []
    invalid_rows = []
    for raw in source_rows:
        row_date = parse_date(raw.get("date"))
        if not row_date:
            continue
        if latest_daily and row_date <= latest_daily:
            continue
        if row_date > target_end:
            continue
        try:
            normalized = normalize_source_row(raw)
        except (TypeError, ValueError):
            invalid_rows.append({"date": row_date, "reason": "parse_failed"})
            continue
        errors = kline_errors(normalized)
        if errors:
            invalid_rows.append({"date": row_date, "errors": errors})
            continue
        normalized_rows.append(normalized)

    normalized_rows = sorted(normalized_rows, key=lambda item: item["date"])
    if not normalized_rows:
        diagnostic = source_diagnostic(
            candidate,
            source_rows,
            invalid_rows=invalid_rows,
            source_attempts=source_attempts,
        )
        return None, {
            "symbol": candidate.get("symbol"),
            "reason": "source_gap_rows_missing",
            "latest_daily_date": latest_daily,
            "target_end_date": target_end,
            **diagnostic,
        }
    if normalized_rows[-1]["date"] < target_end:
        diagnostic = source_diagnostic(
            candidate,
            source_rows,
            invalid_rows=invalid_rows,
            source_attempts=source_attempts,
        )
        return None, {
            "symbol": candidate.get("symbol"),
            "reason": "source_does_not_reach_target_end",
            "latest_valid_gap_row_date": normalized_rows[-1]["date"],
            "target_end_date": target_end,
            "row_count": len(normalized_rows),
            **diagnostic,
        }

    return (
        {
            "action": "upsert_daily_gap_klines",
            "symbol": candidate["symbol"],
            "exchange": candidate.get("exchange"),
            "market": candidate.get("market"),
            "latest_daily_date": latest_daily,
            "latest_minute_date": candidate.get("latest_minute_date"),
            "market_latest_daily_date": candidate.get("market_latest_daily_date"),
            "target_end_date": target_end,
            "source": "tencent_day",
            "source_code": source_rows[-1].get("source_code") if source_rows else None,
            "source_row_count": len(source_rows),
            "row_count": len(normalized_rows),
            "rows": normalized_rows,
        },
        None,
    )


def plan_hash(actions):
    stable = json.dumps(actions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def report_status(actions, unresolved, warnings):
    if warnings and not actions and not unresolved:
        return "WARN"
    if actions and unresolved:
        return "PARTIAL"
    if actions:
        return "ACTIONABLE"
    if unresolved:
        return "UNRESOLVED"
    return "OK"


def build_recommendations(status, actions, unresolved):
    if status == "OK":
        return ["daily_gap_repair_not_required"]
    recs = []
    if actions:
        recs.append("operator_may_apply_hash_confirmed_daily_gap_plan_after_review")
    if unresolved:
        recs.append("investigate_unresolved_daily_gap_symbols_before_trusting_outcome_evidence")
        if any(item.get("reason") == "source_gap_rows_missing" for item in unresolved):
            recs.append("review_source_coverage_or_symbol_mapping_for_unresolved_gap_symbols")
        if any(item.get("reason") == "source_does_not_reach_target_end" for item in unresolved):
            recs.append("do_not_patch_unresolved_symbols_from_minute_bars")
    if status == "WARN":
        recs.append("inspect_daily_gap_repair_warnings")
    return recs


def manual_apply_command(plan_hash_value, output="/tmp/kline_daily_gap_repair_apply.json"):
    return (
        "/usr/bin/python3 /root/kline_daily_gap_repair.py "
        f"--output {output} "
        "--apply "
        f"--confirm-plan-hash {plan_hash_value} "
        "--text"
    )


def build_report(candidates=None, fetch_count=FETCH_COUNT, symbols=None, limit=None):
    candidates = candidates if candidates is not None else fetch_gap_candidates(symbols=symbols, limit=limit)
    warnings = []
    actions = []
    unresolved = []
    for candidate, source_rows, fetch_warnings, source_attempts in fetch_candidate_source_rows(candidates, fetch_count):
        warnings.extend(fetch_warnings)
        action, issue = plan_action(candidate, source_rows, source_attempts=source_attempts)
        if action:
            actions.append(action)
        elif issue:
            unresolved.append(issue)
    digest = plan_hash(actions)
    status = report_status(actions, unresolved, warnings)
    return {
        "schema": "kline_daily_gap_repair_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "mode": "dry-run",
        "plan_hash": digest,
        "summary": {
            "candidate_count": len(candidates),
            "repair_action_count": len(actions),
            "planned_row_count": sum(action.get("row_count", 0) for action in actions),
            "unresolved_count": len(unresolved),
        },
        "recommendations": build_recommendations(status, actions, unresolved),
        "actions": actions,
        "unresolved": unresolved,
        "warnings": warnings[:80],
        "apply_contract": {
            "dry_run_default": True,
            "apply_requires": "--apply --confirm-plan-hash <plan_hash>",
            "backs_up_existing_rows_before_apply": True,
            "does_not_submit_orders": True,
            "does_not_change_crontab": True,
            "does_not_change_watchlists": True,
            "does_not_change_strategy": True,
            "updates": ["klines interval=day rows for planned symbol/date gaps only"],
            "manual_apply_command": manual_apply_command(digest) if actions else None,
            "post_apply_verification_commands": [
                "/usr/bin/python3 /root/data_health_report.py --output /tmp/data_health_report.json --text",
                "/usr/bin/python3 /root/system_health_check.py --output /tmp/quantmind_system_health.json",
                "/usr/bin/python3 /root/readiness_refresh.py --skip-network-producers --output /tmp/readiness_refresh_report.json --text",
            ],
        },
    }


def normalize_fetch_result(fetched):
    if len(fetched) == 2:
        source_rows, fetch_warnings = fetched
        source_attempts = None
    else:
        source_rows, fetch_warnings, source_attempts = fetched
    return source_rows, fetch_warnings, source_attempts


def fetch_candidate_source_rows(candidates, fetch_count=FETCH_COUNT, workers=FETCH_WORKERS):
    if not candidates:
        return []
    worker_count = max(1, min(int(workers or 1), len(candidates)))
    if worker_count == 1:
        return [
            (candidate, *normalize_fetch_result(fetch_tencent_day_rows(candidate, count=fetch_count)))
            for candidate in candidates
        ]

    def fetch_one(candidate):
        return candidate, *normalize_fetch_result(fetch_tencent_day_rows(candidate, count=fetch_count))

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        return list(executor.map(fetch_one, candidates))


def backup_current_rows(actions, backup_dir=BACKUP_DIR):
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(backup_dir, f"kline_daily_gap_{stamp}.json")
    pairs = []
    for action in actions:
        for row in action.get("rows") or []:
            pairs.append((action["symbol"], row["date"]))
    if not pairs:
        save_json_atomic(path, {"generated_at": now_iso(), "rows": []})
        return path
    values = ", ".join(
        f"('{sql_quote(symbol)}'::text, '{sql_quote(day)}'::date)" for symbol, day in sorted(set(pairs))
    )
    query = f"""
        WITH targets(symbol, day) AS (VALUES {values})
        SELECT COALESCE(jsonb_agg(row_to_json(k)), '[]'::jsonb)::text
        FROM klines k
        JOIN targets t ON t.symbol = k.symbol AND t.day = k.timestamp::date
        WHERE k.interval = 'day'
    """
    r = psql(query)
    if r.returncode != 0:
        raise RuntimeError(f"backup query failed: {r.stderr.strip()}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(r.stdout.strip() or "[]")
        f.write("\n")
    return path


def sql_values_for_action(action):
    values = []
    for row in action.get("rows") or []:
        values.append(
            "('{symbol}','day','{date}',{open},{high},{low},{close},{volume},{amount},{change_percent},'{source}',NOW())".format(
                symbol=sql_quote(action["symbol"]),
                date=sql_quote(row["date"]),
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                amount=row["amount"],
                change_percent=row["change_percent"],
                source=DATA_SOURCE,
            )
        )
    return values


def sql_for_action(action):
    values = sql_values_for_action(action)
    if not values:
        return ""
    return """
        INSERT INTO klines (
            symbol, interval, timestamp, open_price, high_price, low_price, close_price,
            volume, amount, change_percent, data_source, created_at
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
            created_at = NOW();
    """.format(values=",\n".join(values))


def build_sql_script(actions):
    statements = ["BEGIN;"]
    for action in actions:
        statement = sql_for_action(action).strip()
        if statement:
            statements.append(statement)
    statements.append("COMMIT;")
    return "\n".join(statements) + "\n"


def apply_actions(actions, backup_dir=BACKUP_DIR):
    if not actions:
        return {"status": "noop", "reason": "no_actions"}
    backup_file = backup_current_rows(actions, backup_dir=backup_dir)
    script = build_sql_script(actions)
    r = subprocess.run(
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
        input=script,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return {
        "status": "applied" if r.returncode == 0 else "failed",
        "backup_file": backup_file,
        "stdout": r.stdout[-4000:],
        "stderr": r.stderr[-4000:],
    }


def build_text_report(payload):
    summary = payload["summary"]
    lines = [
        f"K-line daily gap repair {payload['generated_at']}",
        (
            f"status={payload.get('status')} mode={payload['mode']} plan_hash={payload['plan_hash']} "
            f"candidates={summary['candidate_count']} actions={summary['repair_action_count']} "
            f"rows={summary['planned_row_count']} unresolved={summary['unresolved_count']}"
        ),
    ]
    for action in payload.get("actions", [])[:20]:
        lines.append(
            "  upsert {symbol} latest_daily={latest_daily} target_end={target_end} rows={rows} source={source}".format(
                symbol=action["symbol"],
                latest_daily=action.get("latest_daily_date"),
                target_end=action.get("target_end_date"),
                rows=action.get("row_count"),
                source=action.get("source_code"),
            )
        )
    if payload.get("unresolved"):
        lines.append("unresolved=" + json.dumps(payload["unresolved"][:10], ensure_ascii=False))
    if payload.get("warnings"):
        lines.append("warnings=" + json.dumps(payload["warnings"][:5], ensure_ascii=False))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    parser.add_argument("--apply", action="store_true", help="apply current plan after hash confirmation")
    parser.add_argument("--confirm-plan-hash", default="", help="required with --apply")
    parser.add_argument("--backup-dir", default=BACKUP_DIR)
    parser.add_argument("--fetch-count", type=int, default=FETCH_COUNT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--symbol", action="append", default=[], help="limit repair planning to one symbol; repeatable")
    return parser.parse_args()


def main():
    args = parse_args()
    symbols = [symbol.strip().upper() for symbol in args.symbol if symbol.strip()]
    payload = build_report(fetch_count=args.fetch_count, symbols=symbols or None, limit=args.limit)
    if args.apply:
        if not args.confirm_plan_hash or args.confirm_plan_hash != payload["plan_hash"]:
            payload["apply_result"] = {
                "status": "rejected",
                "reason": "confirm_plan_hash_missing_or_mismatch",
                "expected_plan_hash": payload["plan_hash"],
            }
        else:
            payload["apply_result"] = apply_actions(payload["actions"], backup_dir=args.backup_dir)
            if payload["apply_result"].get("status") == "applied":
                payload["mode"] = "apply"

    if args.output:
        save_json_atomic(args.output, payload)

    if args.text:
        print(build_text_report(payload))
        if payload.get("apply_result"):
            print("apply_result=" + json.dumps(payload["apply_result"], ensure_ascii=False))
    elif args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(build_text_report(payload))
        print("\n--- JSON ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    result = payload.get("apply_result", {})
    if args.apply and result.get("status") not in ("applied", "noop"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
