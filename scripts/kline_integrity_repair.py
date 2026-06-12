#!/usr/bin/env python3
"""Hash-confirmed repair tool for invalid latest daily K-lines.

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
from datetime import date, datetime

try:
    import data_health_report as data_health
except ImportError:
    from scripts import data_health_report as data_health


DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
REPORT_FILE = os.environ.get("KLINE_INTEGRITY_REPAIR_FILE", "/tmp/kline_integrity_repair.json")
BACKUP_DIR = os.environ.get("KLINE_INTEGRITY_BACKUP_DIR", "/tmp/kline_integrity_backups")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def run_cmd(args, timeout=90):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": str(exc)})()


def psql(sql, timeout=90):
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


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def round_price(value):
    return round(float(value), 6)


def kline_errors(row):
    return data_health.latest_ohlc_errors(
        {
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
        }
    )


def today_iso():
    return date.today().isoformat()


def is_today(row):
    return str(row.get("date") or "")[:10] == today_iso()


def fetch_invalid_latest_rows():
    sql = """
        WITH active AS (
            SELECT symbol, exchange
            FROM stocks
            WHERE is_active = true
              AND exchange IN ('HKEX','NASDAQ','NYSE')
        ),
        latest AS (
            SELECT DISTINCT ON (a.symbol)
                   a.symbol, a.exchange, k.timestamp::date AS date,
                   k.open_price, k.high_price, k.low_price, k.close_price,
                   k.volume, k.amount, k.change_percent
            FROM active a
            JOIN klines k ON k.symbol = a.symbol AND k.interval = 'day'
            ORDER BY a.symbol, k.timestamp DESC
        )
        SELECT symbol, exchange, date, open_price, high_price, low_price, close_price, volume, amount, change_percent
        FROM latest
        WHERE open_price <= 0
           OR high_price <= 0
           OR low_price <= 0
           OR close_price <= 0
           OR high_price < low_price
           OR close_price < low_price
           OR close_price > high_price
           OR open_price < low_price
           OR open_price > high_price
        ORDER BY exchange, symbol
    """
    r = psql(sql)
    if r.returncode != 0:
        raise RuntimeError(f"invalid kline query failed: {r.stderr.strip()}")
    parsed = []
    for row in rows(r.stdout):
        if len(row) < 10:
            continue
        parsed.append(
            {
                "symbol": row[0],
                "exchange": row[1],
                "market": "hk" if row[1] == "HKEX" else "us",
                "date": row[2],
                "open": as_float(row[3]),
                "high": as_float(row[4]),
                "low": as_float(row[5]),
                "close": as_float(row[6]),
                "volume": as_float(row[7], 0.0),
                "amount": as_float(row[8], 0.0),
                "change_percent": as_float(row[9], 0.0),
            }
        )
    return parsed


def tencent_symbol_candidates(row):
    symbol = row["symbol"]
    if row["exchange"] == "HKEX":
        return [("hk", symbol)]
    candidates = [("us", f"{symbol}.OQ"), ("us", f"{symbol}.N"), ("us", symbol)]
    return candidates


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


def fetch_tencent_day(row, count=10):
    warnings = []
    for market, code in tencent_symbol_candidates(row):
        url = f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param={market}{code},day,,,{count},qfq"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com"},
            )
            with urllib.request.urlopen(req, timeout=12) as response:
                payload = json.loads(response.read().decode())
        except Exception as exc:
            warnings.append(f"fetch_failed:{market}{code}:{exc}")
            continue
        node = (payload.get("data") or {}).get(f"{market}{code}") or {}
        day_rows = node.get("day") or []
        parsed = [parse_tencent_kline(item) for item in day_rows]
        by_date = {item["date"]: item for item in parsed if item}
        candidate = by_date.get(row["date"])
        if candidate:
            candidate["source_code"] = f"{market}{code}"
            return candidate, warnings
        warnings.append(f"source_missing_date:{market}{code}:{row['date']}")
    return None, warnings


def repair_action(row, replacement):
    before = {key: row.get(key) for key in ("date", "open", "high", "low", "close", "volume", "amount", "change_percent")}
    after = {
        "date": replacement["date"],
        "open": round_price(replacement["open"]),
        "high": round_price(replacement["high"]),
        "low": round_price(replacement["low"]),
        "close": round_price(replacement["close"]),
        "volume": round_price(replacement.get("volume", 0.0)),
        "amount": round_price(replacement.get("amount", 0.0)),
        "change_percent": round_price(replacement.get("change_percent", 0.0)),
    }
    errors_after = kline_errors(after)
    return {
        "action": "update_kline",
        "symbol": row["symbol"],
        "exchange": row["exchange"],
        "date": row["date"],
        "source": "tencent_day",
        "source_code": replacement.get("source_code"),
        "errors_before": kline_errors(row),
        "errors_after": errors_after,
        "before": before,
        "after": after,
    }


def delete_provisional_action(row, reason):
    before = {key: row.get(key) for key in ("date", "open", "high", "low", "close", "volume", "amount", "change_percent")}
    return {
        "action": "delete_provisional_kline",
        "symbol": row["symbol"],
        "exchange": row["exchange"],
        "date": row["date"],
        "source": "latest_day_quarantine",
        "reason": reason,
        "errors_before": kline_errors(row),
        "before": before,
    }


def plan_hash(actions):
    stable = json.dumps(actions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def build_report(rows_in=None):
    invalid_rows = rows_in if rows_in is not None else fetch_invalid_latest_rows()
    warnings = []
    actions = []
    unresolved = []
    for row in invalid_rows:
        replacement, fetch_warnings = fetch_tencent_day(row)
        warnings.extend(fetch_warnings)
        if not replacement:
            if is_today(row):
                actions.append(delete_provisional_action(row, "source_replacement_missing_for_current_day"))
            else:
                unresolved.append({"symbol": row["symbol"], "date": row["date"], "reason": "source_replacement_missing"})
            continue
        action = repair_action(row, replacement)
        if action["errors_after"]:
            if is_today(row):
                actions.append(delete_provisional_action(row, "source_replacement_invalid_for_current_day"))
            else:
                unresolved.append(
                    {
                        "symbol": row["symbol"],
                        "date": row["date"],
                        "reason": "source_replacement_invalid",
                        "errors_after": action["errors_after"],
                    }
                )
            continue
        if action["before"] != action["after"]:
            actions.append(action)
    digest = plan_hash(actions)
    return {
        "schema": "kline_integrity_repair_report_v1",
        "generated_at": now_iso(),
        "mode": "dry-run",
        "plan_hash": digest,
        "summary": {
            "invalid_latest_count": len(invalid_rows),
            "repair_action_count": len(actions),
            "unresolved_count": len(unresolved),
        },
        "actions": actions,
        "unresolved": unresolved,
        "warnings": warnings[:50],
        "apply_contract": {
            "dry_run_default": True,
            "apply_requires": "--apply --confirm-plan-hash <plan_hash>",
            "does_not_submit_orders": True,
            "updates": ["klines latest day rows only", "delete invalid current-day provisional rows only"],
        },
    }


def backup_current_rows(actions, backup_dir=BACKUP_DIR):
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(backup_dir, f"kline_integrity_{stamp}.json")
    if not actions:
        save_json_atomic(path, {"generated_at": now_iso(), "rows": []})
        return path
    pairs = ", ".join(
        f"('{sql_quote(action['symbol'])}'::text, '{sql_quote(action['date'])}'::date)"
        for action in actions
    )
    query = f"""
        WITH targets(symbol, day) AS (VALUES {pairs})
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


def sql_for_action(action):
    if action["action"] == "delete_provisional_kline":
        return (
            "DELETE FROM klines "
            f"WHERE symbol = '{sql_quote(action['symbol'])}' "
            "AND interval = 'day' "
            f"AND timestamp::date = '{sql_quote(action['date'])}'::date;"
        )
    after = action["after"]
    amount = after["amount"]
    return (
        "UPDATE klines SET "
        f"open_price = {after['open']}, "
        f"high_price = {after['high']}, "
        f"low_price = {after['low']}, "
        f"close_price = {after['close']}, "
        f"volume = {after['volume']}, "
        f"amount = {amount}, "
        f"change_percent = {after['change_percent']}, "
        "data_source = 'tencent_day_repair', "
        "created_at = NOW() "
        f"WHERE symbol = '{sql_quote(action['symbol'])}' "
        "AND interval = 'day' "
        f"AND timestamp::date = '{sql_quote(action['date'])}'::date;"
    )


def build_sql_script(actions):
    statements = ["BEGIN;"]
    for action in actions:
        statements.append(sql_for_action(action))
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
        timeout=120,
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
        f"K-line integrity repair {payload['generated_at']}",
        (
            f"mode={payload['mode']} plan_hash={payload['plan_hash']} "
            f"invalid={summary['invalid_latest_count']} actions={summary['repair_action_count']} "
            f"unresolved={summary['unresolved_count']}"
        ),
    ]
    for action in payload.get("actions", [])[:20]:
        if action["action"] == "delete_provisional_kline":
            lines.append(
                f"  delete_provisional {action['symbol']} {action['date']} "
                f"errors={action['errors_before']} reason={action.get('reason')}"
            )
        else:
            lines.append(
                f"  update {action['symbol']} {action['date']} "
                f"errors={action['errors_before']} source={action.get('source_code')}"
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
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report()
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
