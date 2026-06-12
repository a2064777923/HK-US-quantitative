#!/usr/bin/env python3
"""Reconcile simulation positions from sim_trades.

Dry-run is the default. Applying requires both --apply and a matching
--confirm-plan-hash value from a prior dry-run report.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime


DB_CONTAINER = os.environ.get("QM_DB_CONTAINER", "quantmind-db")
DB_USER = os.environ.get("QM_DB_USER", "quantmind")
DB_NAME = os.environ.get("QM_DB_NAME", "quantmind")
PORTFOLIO_ID = int(os.environ.get("QM_SIM_PORTFOLIO_ID", os.environ.get("QM_PORTFOLIO_ID", "8")))
OUTPUT_FILE = os.environ.get("SIM_POSITION_RECONCILE_REPORT_FILE", "/tmp/sim_position_reconcile_report.json")
BACKUP_DIR = os.environ.get("SIM_POSITION_RECONCILE_BACKUP_DIR", "/tmp/sim_position_reconcile_backups")
USD_TO_HKD = float(os.environ.get("USD_TO_HKD", "7.80"))

_COLUMN_CACHE = {}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def run_cmd(args, input_text=None, timeout=60):
    try:
        return subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": str(exc)})()


def psql(sql, timeout=60):
    return run_cmd(
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
            "-t",
            "-A",
            "-F",
            "\t",
            "-c",
            sql,
        ],
        timeout=timeout,
    )


def psql_script(sql, timeout=90):
    return run_cmd(
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
            "-f",
            "-",
        ],
        input_text=sql,
        timeout=timeout,
    )


def rows(stdout):
    return [line.rstrip("\n").split("\t") for line in stdout.splitlines() if line.strip()]


def sql_quote(value):
    return str(value).replace("'", "''")


def sql_text(value):
    if value is None:
        return "NULL"
    return f"'{sql_quote(value)}'"


def sql_num(value):
    try:
        return str(float(value))
    except (TypeError, ValueError):
        return "0"


def as_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value, default=0):
    return int(round(as_float(value, default)))


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def is_hk_symbol(symbol):
    return str(symbol)[:1].isdigit() and len(str(symbol)) == 5


def market_for_symbol(symbol, stock=None):
    exchange = str((stock or {}).get("exchange") or "").upper()
    if exchange == "HKEX" or is_hk_symbol(symbol):
        return "HK"
    return "US"


def fx_to_hkd(symbol):
    return 1.0 if is_hk_symbol(symbol) else USD_TO_HKD


def default_exchange(symbol):
    return "HKEX" if is_hk_symbol(symbol) else "NASDAQ"


def default_currency(symbol):
    return "HKD" if is_hk_symbol(symbol) else "USD"


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


def first_existing(table, candidates, fallback="NULL"):
    cols = table_columns(table)
    for candidate in candidates:
        if candidate in cols:
            return candidate
    return fallback


def sql_in(values):
    return ",".join(sql_text(v) for v in values)


def fetch_trades(portfolio_id):
    fee_expr = first_existing("sim_trades", ("total_fee", "fee", "commission"), "0")
    created_expr = first_existing("sim_trades", ("executed_at", "created_at", "trade_time"), "created_at")
    r = psql(
        f"""
        SELECT symbol, side::text, price, quantity, trade_value, {fee_expr}, {created_expr}
        FROM sim_trades
        WHERE portfolio_id = {int(portfolio_id)}
        ORDER BY {created_expr} ASC, id ASC
        """,
        timeout=90,
    )
    if r.returncode != 0:
        raise RuntimeError(f"sim_trades query failed: {r.stderr.strip()}")
    trades = []
    for row in rows(r.stdout):
        if len(row) < 7:
            continue
        trades.append(
            {
                "symbol": str(row[0]).upper(),
                "side": str(row[1]).lower(),
                "price": as_float(row[2]),
                "quantity": as_float(row[3]),
                "trade_value": as_float(row[4]),
                "fee": as_float(row[5]),
                "created_at": row[6],
            }
        )
    return trades


def fetch_positions(portfolio_id):
    cols = table_columns("positions")
    if not cols:
        return []
    r = psql(
        f"""
        SELECT id, symbol, COALESCE(symbol_name, ''), COALESCE(exchange, ''),
               COALESCE(quantity, 0), COALESCE(available_quantity, 0),
               COALESCE(frozen_quantity, 0), COALESCE(avg_cost, 0),
               COALESCE(current_price, 0), COALESCE(market_value, 0),
               COALESCE(unrealized_pnl, 0), COALESCE(unrealized_pnl_rate, 0),
               COALESCE(status, ''), COALESCE(currency, ''), opened_at, updated_at
        FROM positions
        WHERE portfolio_id = {int(portfolio_id)}
        ORDER BY symbol, id
        """,
        timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"positions query failed: {r.stderr.strip()}")
    result = []
    for row in rows(r.stdout):
        if len(row) < 16:
            continue
        result.append(
            {
                "id": as_int(row[0]),
                "symbol": str(row[1]).upper(),
                "symbol_name": row[2],
                "exchange": row[3],
                "quantity": as_float(row[4]),
                "available_quantity": as_float(row[5]),
                "frozen_quantity": as_float(row[6]),
                "avg_cost": as_float(row[7]),
                "current_price": as_float(row[8]),
                "market_value": as_float(row[9]),
                "unrealized_pnl": as_float(row[10]),
                "unrealized_pnl_rate": as_float(row[11]),
                "status": row[12],
                "currency": row[13],
                "opened_at": row[14],
                "updated_at": row[15],
            }
        )
    return result


def fetch_portfolio(portfolio_id):
    r = psql(
        f"""
        SELECT id, COALESCE(initial_capital, 100000), COALESCE(available_cash, 0),
               COALESCE(current_capital, 0), COALESCE(total_value, 0)
        FROM portfolios
        WHERE id = {int(portfolio_id)}
        """,
        timeout=30,
    )
    parsed = rows(r.stdout) if r.returncode == 0 else []
    if not parsed:
        return {"id": portfolio_id, "missing": True, "initial_capital": 100000.0, "available_cash": 0.0}
    row = parsed[0]
    return {
        "id": as_int(row[0], portfolio_id),
        "initial_capital": as_float(row[1], 100000.0),
        "available_cash": as_float(row[2]),
        "current_capital": as_float(row[3]),
        "total_value": as_float(row[4]),
    }


def fetch_stock_meta(symbols):
    if not symbols:
        return {}
    r = psql(
        f"""
        SELECT symbol, COALESCE(name, ''), COALESCE(exchange, ''), COALESCE(currency, '')
        FROM stocks
        WHERE symbol IN ({sql_in(symbols)})
        """,
        timeout=60,
    )
    meta = {}
    for row in rows(r.stdout) if r.returncode == 0 else []:
        if len(row) >= 4:
            meta[str(row[0]).upper()] = {
                "name": row[1],
                "exchange": row[2],
                "currency": row[3],
            }
    return meta


def fetch_latest_prices(symbols):
    if not symbols:
        return {}
    r = psql(
        f"""
        SELECT DISTINCT ON (symbol) symbol, close_price, timestamp::date
        FROM klines
        WHERE interval = 'day'
          AND symbol IN ({sql_in(symbols)})
        ORDER BY symbol, timestamp DESC
        """,
        timeout=90,
    )
    prices = {}
    for row in rows(r.stdout) if r.returncode == 0 else []:
        if len(row) >= 3:
            prices[str(row[0]).upper()] = {"price": as_float(row[1]), "date": row[2]}
    return prices


def derive_expected_positions(trades):
    positions = {}
    warnings = []
    for trade in trades:
        symbol = trade["symbol"]
        side = trade["side"]
        qty = trade["quantity"]
        price = trade["price"]
        if not symbol or qty <= 0 or price <= 0:
            warnings.append({"symbol": symbol, "reason": "invalid_trade_row", "trade": trade})
            continue
        pos = positions.setdefault(
            symbol,
            {
                "symbol": symbol,
                "quantity": 0.0,
                "avg_cost": 0.0,
                "realized_pnl_quote": 0.0,
                "first_opened_at": trade.get("created_at"),
                "last_trade_at": trade.get("created_at"),
            },
        )
        pos["last_trade_at"] = trade.get("created_at")
        if side == "buy":
            old_cost = pos["quantity"] * pos["avg_cost"]
            new_qty = pos["quantity"] + qty
            pos["avg_cost"] = (old_cost + qty * price) / new_qty if new_qty > 0 else 0.0
            pos["quantity"] = new_qty
            if not pos.get("first_opened_at"):
                pos["first_opened_at"] = trade.get("created_at")
        elif side == "sell":
            matched = min(pos["quantity"], qty)
            if qty > pos["quantity"] + 1e-9:
                warnings.append(
                    {
                        "symbol": symbol,
                        "reason": "sell_quantity_exceeds_derived_position",
                        "sell_quantity": qty,
                        "derived_quantity_before_sell": pos["quantity"],
                    }
                )
            pos["realized_pnl_quote"] += matched * (price - pos["avg_cost"])
            pos["quantity"] = max(pos["quantity"] - qty, 0.0)
            if pos["quantity"] <= 1e-9:
                pos["quantity"] = 0.0
                pos["avg_cost"] = 0.0
        else:
            warnings.append({"symbol": symbol, "reason": f"unsupported_trade_side:{side}", "trade": trade})
    expected = {symbol: pos for symbol, pos in positions.items() if pos["quantity"] > 1e-9}
    return expected, warnings


def enrich_expected_positions(expected, stock_meta, prices):
    enriched = {}
    for symbol, pos in sorted(expected.items()):
        stock = stock_meta.get(symbol, {})
        latest = prices.get(symbol, {})
        current_price = latest.get("price") or pos["avg_cost"]
        qty = pos["quantity"]
        avg_cost = pos["avg_cost"]
        fx = fx_to_hkd(symbol)
        market_value_hkd = qty * current_price * fx
        total_cost_hkd = qty * avg_cost * fx
        unrealized_hkd = market_value_hkd - total_cost_hkd
        enriched[symbol] = {
            **pos,
            "quantity": round(qty, 6),
            "avg_cost": round(avg_cost, 6),
            "symbol_name": stock.get("name") or symbol,
            "exchange": stock.get("exchange") or default_exchange(symbol),
            "currency": stock.get("currency") or default_currency(symbol),
            "current_price": round(current_price, 6),
            "price_source": "latest_kline_close" if latest.get("price") else "avg_cost_fallback",
            "price_date": latest.get("date"),
            "total_cost_hkd": round(total_cost_hkd, 6),
            "market_value_hkd": round(market_value_hkd, 6),
            "unrealized_pnl_hkd": round(unrealized_hkd, 6),
            "unrealized_pnl_rate": round(unrealized_hkd / total_cost_hkd, 8) if total_cost_hkd > 0 else 0.0,
            "realized_pnl_hkd": round(pos.get("realized_pnl_quote", 0.0) * fx, 6),
        }
    total_value = sum(item["market_value_hkd"] for item in enriched.values())
    for item in enriched.values():
        item["weight"] = round(item["market_value_hkd"] / total_value, 8) if total_value > 0 else 0.0
    return enriched


def active_position(row):
    return row.get("status") in ("active", "holding") and row.get("quantity", 0) > 0


def current_position_maps(current_rows):
    all_by_symbol = {}
    active_by_symbol = {}
    for row in current_rows:
        symbol = row["symbol"]
        all_by_symbol.setdefault(symbol, row)
        if active_position(row):
            active_by_symbol[symbol] = row
    return all_by_symbol, active_by_symbol


def nearly_equal(left, right, tolerance=0.0001):
    return abs(as_float(left) - as_float(right)) <= tolerance


def row_diff_reasons(current, expected):
    reasons = []
    checks = (
        ("quantity", expected["quantity"], 0.001),
        ("avg_cost", expected["avg_cost"], 0.0001),
        ("current_price", expected["current_price"], 0.0001),
        ("market_value", expected["market_value_hkd"], 0.01),
        ("unrealized_pnl", expected["unrealized_pnl_hkd"], 0.01),
        ("unrealized_pnl_rate", expected["unrealized_pnl_rate"], 0.0001),
    )
    for field, expected_value, tolerance in checks:
        if not nearly_equal(current.get(field), expected_value, tolerance):
            reasons.append(field)
    if current.get("status") not in ("active", "holding"):
        reasons.append("status")
    if str(current.get("exchange") or "") != str(expected.get("exchange") or ""):
        reasons.append("exchange")
    return reasons


def build_plan(portfolio, current_rows, expected):
    all_by_symbol, active_by_symbol = current_position_maps(current_rows)
    actions = []
    expected_symbols = set(expected)
    for symbol, item in sorted(expected.items()):
        current = active_by_symbol.get(symbol) or all_by_symbol.get(symbol)
        if not current:
            actions.append({"action": "insert_open_position", "symbol": symbol, "expected": item})
            continue
        reasons = row_diff_reasons(current, item)
        if reasons:
            actions.append(
                {
                    "action": "update_open_position",
                    "symbol": symbol,
                    "position_id": current.get("id"),
                    "diff_fields": reasons,
                    "current": current,
                    "expected": item,
                }
            )

    for symbol, current in sorted(active_by_symbol.items()):
        if symbol not in expected_symbols:
            actions.append(
                {
                    "action": "close_stale_position",
                    "symbol": symbol,
                    "position_id": current.get("id"),
                    "current": current,
                    "expected": None,
                }
            )

    positions_value = round(sum(item["market_value_hkd"] for item in expected.values()), 2)
    computed_total = round(portfolio.get("available_cash", 0.0) + positions_value, 2)
    portfolio_action = {
        "action": "update_portfolio_totals",
        "portfolio_id": portfolio.get("id"),
        "available_cash_hkd": round(portfolio.get("available_cash", 0.0), 2),
        "positions_value_hkd": positions_value,
        "computed_total_value_hkd": computed_total,
        "current_portfolio_total_value_hkd": round(portfolio.get("total_value", 0.0), 2),
        "initial_capital_hkd": round(portfolio.get("initial_capital", 0.0), 2),
    }
    if not nearly_equal(portfolio_action["computed_total_value_hkd"], portfolio_action["current_portfolio_total_value_hkd"], 0.01):
        actions.append(portfolio_action)

    return actions


def plan_hash(actions):
    stable = json.dumps(actions, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def assignment_sql(assignments):
    return ", ".join(f"{column} = {value}" for column, value in assignments)


def open_position_assignments(item, include_opened_at=False):
    qty = as_int(item["quantity"])
    avg = item["avg_cost"]
    current_price = item["current_price"]
    assignments = [
        ("symbol_name", sql_text(item.get("symbol_name"))),
        ("exchange", sql_text(item.get("exchange"))),
        ("side", sql_text("long")),
        ("quantity", str(qty)),
        ("available_quantity", str(qty)),
        ("frozen_quantity", "0"),
        ("avg_cost", sql_num(avg)),
        ("total_cost", sql_num(item["total_cost_hkd"])),
        ("current_price", sql_num(current_price)),
        ("market_value", sql_num(item["market_value_hkd"])),
        ("unrealized_pnl", sql_num(item["unrealized_pnl_hkd"])),
        ("unrealized_pnl_rate", sql_num(item["unrealized_pnl_rate"])),
        ("realized_pnl", sql_num(item.get("realized_pnl_hkd", 0))),
        ("weight", sql_num(item.get("weight", 0))),
        ("status", sql_text("holding")),
        ("closed_at", "NULL"),
        ("currency", sql_text(item.get("currency"))),
        ("updated_at", "NOW()"),
    ]
    if include_opened_at:
        assignments.append(("opened_at", "NOW()"))
    return assignments


def sql_for_action(action, portfolio_id):
    kind = action["action"]
    if kind == "insert_open_position":
        item = action["expected"]
        columns = [
            "portfolio_id",
            "symbol",
            "symbol_name",
            "exchange",
            "side",
            "quantity",
            "available_quantity",
            "frozen_quantity",
            "avg_cost",
            "total_cost",
            "current_price",
            "market_value",
            "unrealized_pnl",
            "unrealized_pnl_rate",
            "realized_pnl",
            "weight",
            "status",
            "opened_at",
            "updated_at",
            "currency",
        ]
        qty = as_int(item["quantity"])
        values = [
            str(int(portfolio_id)),
            sql_text(item["symbol"]),
            sql_text(item.get("symbol_name")),
            sql_text(item.get("exchange")),
            sql_text("long"),
            str(qty),
            str(qty),
            "0",
            sql_num(item["avg_cost"]),
            sql_num(item["total_cost_hkd"]),
            sql_num(item["current_price"]),
            sql_num(item["market_value_hkd"]),
            sql_num(item["unrealized_pnl_hkd"]),
            sql_num(item["unrealized_pnl_rate"]),
            sql_num(item.get("realized_pnl_hkd", 0)),
            sql_num(item.get("weight", 0)),
            sql_text("holding"),
            "NOW()",
            "NOW()",
            sql_text(item.get("currency")),
        ]
        return f"INSERT INTO positions ({', '.join(columns)}) VALUES ({', '.join(values)});"
    if kind == "update_open_position":
        sets = assignment_sql(open_position_assignments(action["expected"]))
        return f"UPDATE positions SET {sets} WHERE id = {int(action['position_id'])};"
    if kind == "close_stale_position":
        return (
            "UPDATE positions SET quantity = 0, available_quantity = 0, frozen_quantity = 0, "
            "market_value = 0, unrealized_pnl = 0, unrealized_pnl_rate = 0, weight = 0, "
            "status = 'closed', closed_at = NOW(), updated_at = NOW() "
            f"WHERE id = {int(action['position_id'])};"
        )
    if kind == "update_portfolio_totals":
        total = action["computed_total_value_hkd"]
        initial = action["initial_capital_hkd"] or 100000
        total_pnl = total - initial
        total_return = total_pnl / initial if initial else 0
        return (
            "UPDATE portfolios SET "
            f"current_capital = {sql_num(total)}, total_value = {sql_num(total)}, "
            f"total_pnl = {sql_num(total_pnl)}, total_return = {sql_num(total_return)}, "
            "updated_at = NOW() "
            f"WHERE id = {int(action['portfolio_id'])};"
        )
    raise ValueError(f"unsupported action: {kind}")


def build_sql_script(actions, portfolio_id):
    statements = ["BEGIN;"]
    for action in actions:
        statements.append(sql_for_action(action, portfolio_id))
    statements.append("COMMIT;")
    return "\n".join(statements) + "\n"


def backup_current_state(portfolio_id, backup_dir=BACKUP_DIR):
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(backup_dir, f"portfolio_{portfolio_id}_{stamp}.json")
    query = f"""
        SELECT jsonb_build_object(
            'portfolio', (SELECT row_to_json(p) FROM portfolios p WHERE p.id = {int(portfolio_id)}),
            'positions', COALESCE((SELECT jsonb_agg(row_to_json(pos)) FROM positions pos WHERE pos.portfolio_id = {int(portfolio_id)}), '[]'::jsonb)
        )::text
    """
    r = psql(query, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"backup query failed: {r.stderr.strip()}")
    raw = r.stdout.strip() or "{}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw)
        f.write("\n")
    return path


def build_report(portfolio_id=PORTFOLIO_ID):
    trades = fetch_trades(portfolio_id)
    current_rows = fetch_positions(portfolio_id)
    portfolio = fetch_portfolio(portfolio_id)
    expected_raw, warnings = derive_expected_positions(trades)
    symbols = sorted(set(expected_raw) | {row["symbol"] for row in current_rows})
    stock_meta = fetch_stock_meta(symbols)
    prices = fetch_latest_prices(symbols)
    expected = enrich_expected_positions(expected_raw, stock_meta, prices)
    actions = build_plan(portfolio, current_rows, expected)
    digest = plan_hash(actions)
    return {
        "schema": "sim_position_reconcile_report_v1",
        "generated_at": now_iso(),
        "portfolio_id": portfolio_id,
        "mode": "dry-run",
        "plan_hash": digest,
        "summary": {
            "trade_count": len(trades),
            "current_position_row_count": len(current_rows),
            "current_active_position_count": len([row for row in current_rows if active_position(row)]),
            "expected_open_position_count": len(expected),
            "action_count": len(actions),
            "by_action": action_counts(actions),
        },
        "portfolio": portfolio,
        "expected_open_positions": expected,
        "current_positions": current_rows,
        "actions": actions,
        "warnings": warnings,
        "apply_contract": {
            "dry_run_default": True,
            "apply_requires": "--apply --confirm-plan-hash <plan_hash>",
            "does_not_submit_orders": True,
            "updates": ["positions table", "portfolio total/current_capital summary"],
        },
    }


def action_counts(actions):
    counts = {}
    for action in actions:
        key = action.get("action", "unknown")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def build_text_report(payload):
    summary = payload["summary"]
    lines = [
        f"Sim position reconcile {payload['generated_at']} P{payload['portfolio_id']}",
        (
            f"mode={payload['mode']} plan_hash={payload['plan_hash']} "
            f"trades={summary['trade_count']} current_active={summary['current_active_position_count']} "
            f"expected_open={summary['expected_open_position_count']} actions={summary['action_count']}"
        ),
        f"by_action={summary['by_action']}",
    ]
    for action in payload.get("actions", [])[:20]:
        if action["action"] == "update_portfolio_totals":
            lines.append(
                "  update_portfolio_totals "
                f"{action['current_portfolio_total_value_hkd']} -> {action['computed_total_value_hkd']}"
            )
        else:
            lines.append(f"  {action['action']} {action.get('symbol')} fields={action.get('diff_fields', [])}")
    if payload.get("warnings"):
        lines.append("warnings=" + json.dumps(payload["warnings"][:5], ensure_ascii=False))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--portfolio-id", type=int, default=PORTFOLIO_ID)
    parser.add_argument("--output", default=OUTPUT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    parser.add_argument("--apply", action="store_true", help="apply the current plan after hash confirmation")
    parser.add_argument("--confirm-plan-hash", default="", help="required with --apply")
    parser.add_argument("--backup-dir", default=BACKUP_DIR)
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(args.portfolio_id)
    applied = False
    if args.apply:
        if not args.confirm_plan_hash or args.confirm_plan_hash != payload["plan_hash"]:
            payload["apply_result"] = {
                "status": "rejected",
                "reason": "confirm_plan_hash_missing_or_mismatch",
                "expected_plan_hash": payload["plan_hash"],
            }
        elif not payload["actions"]:
            payload["apply_result"] = {"status": "noop", "reason": "no_actions"}
        else:
            backup = backup_current_state(args.portfolio_id, args.backup_dir)
            script = build_sql_script(payload["actions"], args.portfolio_id)
            r = psql_script(script, timeout=120)
            payload["apply_result"] = {
                "status": "applied" if r.returncode == 0 else "failed",
                "backup_file": backup,
                "stdout": r.stdout[-4000:],
                "stderr": r.stderr[-4000:],
            }
            applied = r.returncode == 0
            if applied:
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
