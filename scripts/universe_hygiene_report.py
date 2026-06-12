#!/usr/bin/env python3
"""Read-only hygiene report for the active HK/US stock universe."""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime


DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
REPORT_FILE = os.environ.get("UNIVERSE_HYGIENE_REPORT_FILE", "/tmp/universe_hygiene_report.json")
SEVERE_STALE_DAYS = int(os.environ.get("UNIVERSE_HYGIENE_SEVERE_STALE_DAYS", "30"))
REVIEW_STALE_DAYS = int(os.environ.get("UNIVERSE_HYGIENE_REVIEW_STALE_DAYS", "3"))
MIN_HISTORY_ROWS_120D = int(os.environ.get("UNIVERSE_HYGIENE_MIN_HISTORY_ROWS_120D", "20"))

_COLUMN_CACHE = {}


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


def table_columns(table):
    if table in _COLUMN_CACHE:
        return _COLUMN_CACHE[table]
    r = psql(
        f"""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = '{sql_quote(table)}'
        """
    )
    cols = {row[0] for row in rows(r.stdout)} if r.returncode == 0 else set()
    _COLUMN_CACHE[table] = cols
    return cols


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


def rate(part, whole):
    return round(part / whole * 100, 2) if whole else 0.0


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def lag_days(latest_market_date, symbol_date):
    latest = parse_date(latest_market_date)
    symbol = parse_date(symbol_date)
    if not latest or not symbol:
        return None
    return (latest - symbol).days


def market_code(exchange):
    return "HK" if exchange == "HKEX" else "US"


def valid_symbol_format(row):
    symbol = str(row.get("symbol") or "")
    exchange = row.get("exchange")
    if exchange == "HKEX":
        return bool(re.fullmatch(r"\d{5}", symbol))
    if exchange in ("NASDAQ", "NYSE"):
        return bool(re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol))
    return True


def fetch_universe_rows():
    data_source_expr = "k.data_source" if "data_source" in table_columns("klines") else "'missing'"
    sql = """
        WITH active AS (
            SELECT CASE WHEN exchange = 'HKEX' THEN 'HK' ELSE 'US' END AS market,
                   symbol, name, exchange, list_date
            FROM stocks
            WHERE is_active = true
              AND exchange IN ('HKEX','NASDAQ','NYSE')
        ),
        latest AS (
            SELECT DISTINCT ON (a.symbol)
                   a.symbol,
                   k.timestamp::date AS latest_date,
                   k.close_price,
                   k.volume,
                   {data_source_expr} AS data_source
            FROM active a
            LEFT JOIN klines k
              ON k.symbol = a.symbol
             AND k.interval = 'day'
            ORDER BY a.symbol, k.timestamp DESC NULLS LAST
        ),
        history AS (
            SELECT a.symbol,
                   count(k.*) FILTER (WHERE k.timestamp::date >= CURRENT_DATE - INTERVAL '120 days') AS history_rows_120d,
                   count(k.*) FILTER (
                       WHERE k.timestamp::date >= CURRENT_DATE - INTERVAL '20 days'
                         AND COALESCE(k.volume, 0) <= 0
                   ) AS zero_volume_rows_20d
            FROM active a
            LEFT JOIN klines k
              ON k.symbol = a.symbol
             AND k.interval = 'day'
            GROUP BY a.symbol
        )
        SELECT a.market, a.symbol, a.name, a.exchange, a.list_date,
               l.latest_date, l.close_price, l.volume, l.data_source,
               COALESCE(h.history_rows_120d, 0), COALESCE(h.zero_volume_rows_20d, 0)
        FROM active a
        LEFT JOIN latest l ON l.symbol = a.symbol
        LEFT JOIN history h ON h.symbol = a.symbol
        ORDER BY a.market, a.symbol
    """.format(data_source_expr=data_source_expr)
    r = psql(sql)
    if r.returncode != 0:
        return [], [f"universe_hygiene_query_failed:{r.stderr.strip()}"]
    parsed = []
    for row in rows(r.stdout):
        if len(row) < 11:
            continue
        parsed.append(
            {
                "market": row[0],
                "symbol": row[1],
                "name": row[2],
                "exchange": row[3],
                "list_date": row[4],
                "latest_date": row[5],
                "latest_close": as_float(row[6]),
                "latest_volume": as_float(row[7]),
                "data_source": row[8],
                "history_rows_120d": as_int(row[9]),
                "zero_volume_rows_20d": as_int(row[10]),
            }
        )
    return parsed, []


def classify_symbol(row, market_latest_date):
    latest_date = row.get("latest_date")
    history_rows = as_int(row.get("history_rows_120d"))
    lag = lag_days(market_latest_date, latest_date)
    issues = []
    severity = "ok"
    action = "keep_active"

    if not valid_symbol_format(row):
        issues.append("symbol_format_unusual_for_exchange")
        severity = "high"
        action = "candidate_remove_from_stock_universe"

    if not latest_date:
        issues.append("missing_daily_klines")
        severity = "high"
        if action == "keep_active":
            action = "candidate_refetch_or_deactivate"
    elif lag is not None and lag >= SEVERE_STALE_DAYS:
        issues.append(f"latest_kline_stale_ge_{SEVERE_STALE_DAYS}d")
        severity = "high"
        if action == "keep_active":
            action = "candidate_deactivate_or_symbol_mapping"
    elif lag is not None and lag >= REVIEW_STALE_DAYS:
        issues.append(f"latest_kline_stale_ge_{REVIEW_STALE_DAYS}d")
        severity = "medium" if severity == "ok" else severity
        if action == "keep_active":
            action = "candidate_refetch_then_review"
    elif lag is not None and lag >= 1:
        issues.append("latest_kline_one_day_behind_market")
        severity = "low" if severity == "ok" else severity
        if action == "keep_active":
            action = "monitor_or_refetch_after_close"

    if history_rows == 0:
        issues.append("no_history_rows_120d")
        severity = "high"
        if action == "keep_active":
            action = "candidate_refetch_or_deactivate"
    elif history_rows < MIN_HISTORY_ROWS_120D:
        issues.append(f"history_rows_120d_below_{MIN_HISTORY_ROWS_120D}")
        severity = "medium" if severity in ("ok", "low") else severity
        if action == "keep_active":
            action = "candidate_refetch_then_review"

    if as_int(row.get("zero_volume_rows_20d")) >= 5:
        issues.append("frequent_zero_volume_rows_20d")
        severity = "medium" if severity in ("ok", "low") else severity
        if action == "keep_active":
            action = "candidate_liquidity_review"

    if not issues:
        issues.append("healthy_active_symbol")

    return {
        **row,
        "market_latest_date": market_latest_date,
        "lag_days_vs_market_latest": lag,
        "severity": severity,
        "recommended_action": action,
        "issues": issues,
    }


def summarize_market(market, rows_in):
    latest_dates = Counter(row.get("latest_date") for row in rows_in if row.get("latest_date"))
    market_latest_date = max(latest_dates) if latest_dates else None
    classified = [classify_symbol(row, market_latest_date) for row in rows_in]
    issue_counts = Counter()
    action_counts = Counter()
    severity_counts = Counter()
    for item in classified:
        action_counts[item["recommended_action"]] += 1
        severity_counts[item["severity"]] += 1
        for issue in item.get("issues") or []:
            issue_counts[issue] += 1

    problematic = [item for item in classified if item["recommended_action"] != "keep_active"]
    high_priority_actions = [
        "candidate_remove_from_stock_universe",
        "candidate_deactivate_or_symbol_mapping",
        "candidate_refetch_or_deactivate",
    ]
    active_symbols = [
        {
            "symbol": item.get("symbol"),
            "severity": item.get("severity"),
            "recommended_action": item.get("recommended_action"),
            "issues": item.get("issues") or [],
            "latest_date": item.get("latest_date"),
            "market_latest_date": item.get("market_latest_date"),
            "lag_days_vs_market_latest": item.get("lag_days_vs_market_latest"),
            "history_rows_120d": item.get("history_rows_120d"),
            "zero_volume_rows_20d": item.get("zero_volume_rows_20d"),
        }
        for item in classified
    ]
    return {
        "market": market,
        "latest_date": market_latest_date,
        "active_symbol_count": len(rows_in),
        "healthy_symbol_count": action_counts.get("keep_active", 0),
        "problem_symbol_count": len(problematic),
        "problem_symbol_pct": rate(len(problematic), len(rows_in)),
        "latest_date_distribution": dict(latest_dates),
        "severity_counts": dict(severity_counts),
        "issue_counts": dict(issue_counts),
        "recommended_action_counts": dict(action_counts),
        "high_priority_candidates": [
            item for item in problematic if item["recommended_action"] in high_priority_actions
        ][:100],
        "refetch_candidates": [
            item
            for item in problematic
            if item["recommended_action"] in ("candidate_refetch_then_review", "monitor_or_refetch_after_close")
        ][:100],
        "active_symbols": active_symbols,
        "all_problem_symbols": problematic[:300],
    }


def build_proposal(markets, generated_at):
    def symbols_for(actions):
        out = {}
        for market, summary in sorted(markets.items()):
            symbols = [
                item["symbol"]
                for item in summary.get("all_problem_symbols") or []
                if item["recommended_action"] in actions
            ]
            out[market] = symbols
        return out

    return {
        "schema": "stock_universe_hygiene_proposal_v1",
        "generated_at": generated_at,
        "source": {
            "report_schema": "universe_hygiene_report_v1",
            "manual_review_required": True,
            "auto_applied": False,
            "does_not_change_stocks_table": True,
        },
        "candidate_deactivate_or_remap": symbols_for(
            {
                "candidate_remove_from_stock_universe",
                "candidate_deactivate_or_symbol_mapping",
                "candidate_refetch_or_deactivate",
            }
        ),
        "candidate_refetch_or_monitor": symbols_for(
            {
                "candidate_refetch_then_review",
                "monitor_or_refetch_after_close",
                "candidate_liquidity_review",
            }
        ),
    }


def build_recommendations(markets):
    recs = []
    for market, summary in sorted(markets.items()):
        high = len(summary.get("high_priority_candidates") or [])
        refetch = len(summary.get("refetch_candidates") or [])
        if high:
            recs.append(f"{market}:manual_review_high_priority_universe_hygiene:{high}")
        if refetch:
            recs.append(f"{market}:refetch_or_monitor_stale_symbols:{refetch}")
        if summary["problem_symbol_pct"] >= 20:
            recs.append(f"{market}:active_universe_problem_rate_above_20pct")
    if not recs:
        recs.append("active_universe_hygiene_clean")
    return recs


def build_summary(markets):
    active_count = sum(summary.get("active_symbol_count", 0) for summary in markets.values())
    problem_count = sum(summary.get("problem_symbol_count", 0) for summary in markets.values())
    high_priority_count = sum(len(summary.get("high_priority_candidates") or []) for summary in markets.values())
    refetch_count = sum(len(summary.get("refetch_candidates") or []) for summary in markets.values())
    return {
        "active_symbol_count": active_count,
        "problem_symbol_count": problem_count,
        "problem_symbol_pct": rate(problem_count, active_count),
        "high_priority_count": high_priority_count,
        "refetch_or_monitor_count": refetch_count,
    }


def report_status(markets, warnings):
    if warnings:
        return "WARN"
    summary = build_summary(markets)
    if summary["problem_symbol_count"] > 0:
        return "WARN"
    return "OK"


def build_report(universe_rows=None):
    warnings = []
    _COLUMN_CACHE.clear()
    if universe_rows is None:
        universe_rows, fetch_warnings = fetch_universe_rows()
        warnings.extend(fetch_warnings)

    rows_by_market = defaultdict(list)
    for row in universe_rows or []:
        market = row.get("market") or market_code(row.get("exchange"))
        if market:
            rows_by_market[market].append(row)

    markets = {
        market: summarize_market(market, rows_in)
        for market, rows_in in sorted(rows_by_market.items())
    }
    generated_at = now_iso()
    summary = build_summary(markets)
    return {
        "schema": "universe_hygiene_report_v1",
        "generated_at": generated_at,
        "status": report_status(markets, warnings),
        "summary": summary,
        "active_symbol_count": summary["active_symbol_count"],
        "problem_count": summary["problem_symbol_count"],
        "high_priority_count": summary["high_priority_count"],
        "source": {
            "read_only": True,
            "stock_table": "stocks",
            "kline_table": "klines",
            "manual_review_required": True,
            "auto_applies_stock_changes": False,
            "severe_stale_days": SEVERE_STALE_DAYS,
            "review_stale_days": REVIEW_STALE_DAYS,
            "min_history_rows_120d": MIN_HISTORY_ROWS_120D,
        },
        "markets": markets,
        "proposal": build_proposal(markets, generated_at),
        "recommendations": build_recommendations(markets),
        "warnings": warnings,
    }


def build_text_report(payload):
    lines = [f"Universe hygiene report {payload['generated_at']}"]
    for market, summary in sorted((payload.get("markets") or {}).items()):
        lines.append(
            f"{market}: active={summary['active_symbol_count']} latest={summary['latest_date']} "
            f"problems={summary['problem_symbol_count']} ({summary['problem_symbol_pct']}%) "
            f"actions={summary['recommended_action_counts']}"
        )
        top = summary.get("high_priority_candidates") or []
        if top:
            lines.append(
                "  high_priority: "
                + ", ".join(
                    f"{item['symbol']}:{item['recommended_action']}:{item.get('lag_days_vs_market_latest')}"
                    for item in top[:10]
                )
            )
    lines.append("Recommendations: " + ", ".join(payload.get("recommendations") or []))
    if payload.get("warnings"):
        lines.append("Warnings: " + ", ".join(payload["warnings"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report()
    if args.output:
        save_json_atomic(args.output, payload)
    text = build_text_report(payload)
    if args.text:
        print(text)
    elif args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text)
        print("\n--- JSON ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
