#!/usr/bin/env python3
"""Alert-specific order intake for rt_signal_engine_v5.

Default mode is dry-run. Execute mode must be enabled explicitly with
RT_ORDER_EXECUTION_MODE=execute or --mode execute.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

API_BASE = os.environ.get("QM_API_BASE", "https://notopenai.asia/api/v1").rstrip("/")
API_USER = os.environ.get("QM_API_USER", "kaitosim")
API_PASSWORD = os.environ.get("QM_API_PASSWORD", "")
PORTFOLIO_ID = int(os.environ.get("QM_PORTFOLIO_ID", "8"))

STATE_FILE = os.environ.get("RT_ORDER_STATE_FILE", "/tmp/rt_order_intake_state.json")
ALERT_QUEUE_FILE = os.environ.get("RT_ALERT_QUEUE_FILE", "/tmp/rt_signal_alerts.jsonl")
ALERT_FILE = os.environ.get("RT_ALERT_FILE", "/tmp/rt_signal_alert.json")
JUDGMENT_FILE = os.environ.get("RT_ORDER_JUDGMENT_FILE", "/tmp/hermes_trade_judgments.jsonl")
STRATEGY_EVIDENCE_FILE = os.environ.get("RT_SIGNAL_OUTCOME_REPORT_FILE", "/tmp/rt_signal_outcome_report.json")
MARKET_CONTEXT_FILE = os.environ.get("MARKET_CONTEXT_REPORT_FILE", "/tmp/market_context_report.json")

USD_TO_HKD = float(os.environ.get("USD_TO_HKD", "7.80"))
POSITION_SIZE_PCT = float(os.environ.get("RT_ORDER_POSITION_SIZE_PCT", "0.10"))
MAX_RISK_PCT = float(os.environ.get("RT_ORDER_MAX_RISK_PCT", "0.01"))
MAX_POSITIONS = int(os.environ.get("RT_ORDER_MAX_POSITIONS", "10"))
MIN_BUY_SCORE = float(os.environ.get("RT_ORDER_MIN_BUY_SCORE", "0.25"))
MAX_SELL_SCORE = float(os.environ.get("RT_ORDER_MAX_SELL_SCORE", "-0.25"))
MIN_RR_RATIO = float(os.environ.get("RT_ORDER_MIN_RR_RATIO", "1.2"))
MAX_ALERT_AGE_MINUTES = int(os.environ.get("RT_ORDER_MAX_ALERT_AGE_MINUTES", "60"))
SELL_FRACTION = float(os.environ.get("RT_ORDER_SELL_FRACTION", "1.0"))
DEFAULT_EQUITY_HKD = float(os.environ.get("RT_ORDER_DEFAULT_EQUITY_HKD", "100000"))
REQUIRE_HERMES_JUDGMENT = os.environ.get("RT_ORDER_REQUIRE_HERMES_JUDGMENT", "1") != "0"
MIN_HERMES_CONFIDENCE = float(os.environ.get("RT_ORDER_MIN_HERMES_CONFIDENCE", "0.60"))
MAX_JUDGMENT_AGE_MINUTES = int(os.environ.get("RT_ORDER_MAX_JUDGMENT_AGE_MINUTES", "240"))
REQUIRE_STRATEGY_EVIDENCE = os.environ.get("RT_ORDER_REQUIRE_STRATEGY_EVIDENCE", "1") != "0"
STRATEGY_EVIDENCE_HORIZON = os.environ.get("RT_ORDER_STRATEGY_EVIDENCE_HORIZON", "1d")
MIN_OUTCOME_SAMPLE = int(os.environ.get("RT_ORDER_MIN_OUTCOME_SAMPLE", "30"))
MIN_TRIGGER_OUTCOME_SAMPLE = int(os.environ.get("RT_ORDER_MIN_TRIGGER_OUTCOME_SAMPLE", "5"))
MIN_OUTCOME_WIN_RATE_PCT = float(os.environ.get("RT_ORDER_MIN_OUTCOME_WIN_RATE_PCT", "45"))
MIN_OUTCOME_AVG_RETURN_PCT = float(os.environ.get("RT_ORDER_MIN_OUTCOME_AVG_RETURN_PCT", "0"))
MAX_OUTCOME_REPORT_AGE_HOURS = int(os.environ.get("RT_ORDER_MAX_OUTCOME_REPORT_AGE_HOURS", "72"))
REQUIRE_MARKET_CONTEXT = os.environ.get("RT_ORDER_REQUIRE_MARKET_CONTEXT", "1") != "0"
MAX_MARKET_CONTEXT_AGE_HOURS = int(os.environ.get("RT_ORDER_MAX_MARKET_CONTEXT_AGE_HOURS", "72"))
MIN_MARKET_EXCEPTION_CONFIDENCE = float(os.environ.get("RT_ORDER_MIN_MARKET_EXCEPTION_CONFIDENCE", "0.80"))
REQUIRE_NO_SYMBOL_CONFLICT = os.environ.get("RT_ORDER_REQUIRE_NO_SYMBOL_CONFLICT", "1") != "0"
SYMBOL_CONFLICT_QUEUE_SCAN_LIMIT = int(os.environ.get("RT_ORDER_CONFLICT_QUEUE_SCAN_LIMIT", "1000"))

LOT_SIZES_HK = {
    "00700": 100,
    "00388": 100,
    "01810": 200,
    "03690": 100,
    "09618": 100,
    "09626": 100,
    "09896": 200,
    "09988": 100,
    "09961": 100,
}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def load_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_state(path):
    state = load_json_file(path, {"processed": {}, "dry_runs": {}})
    if not isinstance(state, dict):
        state = {"processed": {}, "dry_runs": {}}
    if "processed" not in state or not isinstance(state["processed"], dict):
        state["processed"] = {}
    if "dry_runs" not in state or not isinstance(state["dry_runs"], dict):
        state["dry_runs"] = {}
    return state


def signal_id(alert):
    return alert.get("signal_id") or ":".join(
        str(alert.get(k, "")) for k in ("symbol", "signal_type", "trigger", "generated_at", "time")
    )


def parse_time(value):
    if not value:
        return None
    value = str(value).strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def is_hk_symbol(symbol):
    return symbol[:1].isdigit() and len(symbol) == 5


def alert_market(alert):
    market = str(alert.get("market", "")).strip().upper()
    if market in ("HK", "US"):
        return market
    symbol = str(alert.get("symbol", "")).strip().upper()
    return "HK" if is_hk_symbol(symbol) else "US"


def fx_to_hkd(symbol):
    return 1.0 if is_hk_symbol(symbol) else USD_TO_HKD


def lot_size(symbol):
    if is_hk_symbol(symbol):
        return LOT_SIZES_HK.get(symbol, int(os.environ.get("RT_ORDER_DEFAULT_HK_LOT", "100")))
    return 1


def round_down_lot(quantity, symbol):
    lot = lot_size(symbol)
    if lot <= 1:
        return int(quantity)
    return int(quantity // lot) * lot


def first_number(mapping, keys, default=0.0):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return default


def normalize_positions(raw_positions):
    if isinstance(raw_positions, dict):
        items = raw_positions.items()
    elif isinstance(raw_positions, list):
        items = ((p.get("symbol"), p) for p in raw_positions if isinstance(p, dict))
    else:
        items = []

    positions = {}
    for symbol, pos in items:
        if not symbol or not isinstance(pos, dict):
            continue
        status = pos.get("status", "holding")
        if status not in ("active", "holding", None, ""):
            continue
        quantity = first_number(pos, ("volume", "quantity", "qty", "shares"))
        if quantity <= 0:
            continue
        positions[str(symbol)] = {
            "quantity": quantity,
            "cost_price": first_number(pos, ("cost_price", "avg_cost", "average_price", "price")),
            "last_price": first_number(pos, ("last_price", "current_price", "price")),
            "status": status or "holding",
        }
    return positions


def normalize_account(account, positions):
    if isinstance(account, dict) and isinstance(account.get("data"), dict):
        account = account["data"]
    if not isinstance(account, dict):
        account = {}
    cash = first_number(account, ("cash", "available_cash", "cash_balance", "current_capital"), DEFAULT_EQUITY_HKD)
    equity = first_number(account, ("total_asset", "total_value", "equity", "nav", "current_capital"), 0.0)
    if equity <= 0:
        equity = cash
        for symbol, pos in positions.items():
            equity += pos["quantity"] * pos["last_price"] * fx_to_hkd(symbol)
    return {"cash_hkd": cash, "equity_hkd": equity, "positions": positions}


def api_request(path, token=None, method="GET", data=None):
    url = API_BASE + path
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def get_token():
    if not API_PASSWORD:
        return ""
    result = api_request("/auth/login", method="POST", data={"username": API_USER, "password": API_PASSWORD})
    return result.get("access_token", "")


def fetch_context():
    warnings = []
    token = get_token()
    if not token:
        warnings.append("api_password_missing_or_login_failed; using default empty account context")
        return token, normalize_account({"cash": DEFAULT_EQUITY_HKD, "total_asset": DEFAULT_EQUITY_HKD}, {}), warnings

    encoded = urllib.parse.urlencode({"portfolio_id": PORTFOLIO_ID})
    account = {}
    positions = {}
    try:
        account = api_request(f"/simulation/account?{encoded}", token=token)
    except Exception as exc:
        warnings.append(f"account_query_with_portfolio_failed: {exc}")
        try:
            account = api_request("/simulation/account", token=token)
        except Exception as second_exc:
            warnings.append(f"account_query_failed: {second_exc}")

    raw_positions = {}
    if isinstance(account, dict):
        raw_account = account.get("data", account)
        if isinstance(raw_account, dict):
            raw_positions = raw_account.get("positions", {})
    try:
        pos_result = api_request(f"/simulation/positions?{encoded}", token=token)
        raw_positions = pos_result.get("data", pos_result) if isinstance(pos_result, dict) else pos_result
    except Exception as exc:
        warnings.append(f"positions_query_failed: {exc}")

    positions = normalize_positions(raw_positions)
    return token, normalize_account(account, positions), warnings


def submit_order(token, symbol, side, quantity, price=None):
    order = {
        "portfolio_id": PORTFOLIO_ID,
        "symbol": symbol,
        "side": side.lower(),
        "order_type": "market",
        "quantity": int(quantity),
        "trading_mode": "simulation",
    }
    if price is not None and os.environ.get("RT_ORDER_INCLUDE_PRICE", "0") == "1":
        order["price"] = price
    return api_request("/simulation/orders", token=token, method="POST", data=order)


def health_gate(mode):
    if mode != "execute" and os.environ.get("RT_ORDER_REQUIRE_HEALTH", "0") != "1":
        return True, {"status": "SKIPPED", "detail": "health gate skipped in dry-run"}
    script = os.path.join(os.path.dirname(__file__), "system_health_check.py")
    if not os.path.exists(script):
        return mode != "execute", {"status": "WARN", "detail": "system_health_check.py not found"}
    r = subprocess.run([sys.executable, script, "--json"], capture_output=True, text=True, timeout=30)
    try:
        payload = json.loads(r.stdout)
    except Exception:
        payload = {"status": "FAIL", "detail": r.stderr.strip() or "health check produced invalid JSON"}
    ok = payload.get("status") != "FAIL"
    return ok, payload


def metric_reasons(metric, sample_key, min_sample, prefix):
    reasons = []
    resolved = int(float(metric.get("resolved_count", 0) or 0))
    if resolved < min_sample:
        reasons.append(f"{prefix}_outcome_sample_below_{min_sample}")
        return reasons
    avg_return = metric.get("avg_signed_close_return_pct")
    win_rate = metric.get("win_rate_pct")
    try:
        if avg_return is None or float(avg_return) <= MIN_OUTCOME_AVG_RETURN_PCT:
            reasons.append(f"{prefix}_avg_return_not_positive")
    except (TypeError, ValueError):
        reasons.append(f"{prefix}_avg_return_missing")
    try:
        if win_rate is None or float(win_rate) < MIN_OUTCOME_WIN_RATE_PCT:
            reasons.append(f"{prefix}_win_rate_below_{MIN_OUTCOME_WIN_RATE_PCT:g}")
    except (TypeError, ValueError):
        reasons.append(f"{prefix}_win_rate_missing")
    return reasons


def find_trigger_outcome(report, alert):
    key = f"{str(alert.get('signal_type', '')).upper()}:{alert.get('trigger') or 'UNKNOWN'}"
    for row in report.get("by_trigger") or []:
        if row.get("key") == key:
            return key, row
    return key, {}


def is_directional(alert):
    return str((alert or {}).get("signal_type", "")).strip().upper() in ("BUY", "SELL")


def load_jsonl_tail(path, limit):
    if not os.path.exists(path):
        return [], [f"missing_queue:{path}"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as exc:
        return [], [f"queue_read_failed:{path}:{exc}"]
    if limit and limit > 0:
        lines = lines[-limit:]
    alerts = []
    warnings = []
    for idx, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"bad_jsonl_line:{idx}")
            continue
        if isinstance(item, dict):
            alerts.append(item)
    return alerts, warnings


def alert_scope(alert):
    strategy_config_id = (alert or {}).get("strategy_config_id")
    watchlist_id = (alert or {}).get("watchlist_id")
    if strategy_config_id and watchlist_id:
        return {
            "mode": "strategy_config_and_watchlist",
            "strategy_config_id": str(strategy_config_id),
            "watchlist_id": str(watchlist_id),
        }
    generated_at = parse_time((alert or {}).get("generated_at"))
    return {
        "mode": "signal_date",
        "signal_date": generated_at.date().isoformat() if generated_at else None,
    }


def same_alert_scope(candidate, scope):
    if scope.get("mode") == "strategy_config_and_watchlist":
        return (
            str(candidate.get("strategy_config_id") or "") == scope.get("strategy_config_id")
            and str(candidate.get("watchlist_id") or "") == scope.get("watchlist_id")
        )
    if scope.get("mode") == "signal_date" and scope.get("signal_date"):
        generated_at = parse_time(candidate.get("generated_at"))
        return generated_at is not None and generated_at.date().isoformat() == scope.get("signal_date")
    return True


def symbol_conflict_gate(alert, mode, queue_file=ALERT_QUEUE_FILE):
    """Reject execute when current-scope queue has opposite directional alerts for the same symbol."""
    if not REQUIRE_NO_SYMBOL_CONFLICT:
        return True, {"status": "DISABLED", "queue_file": queue_file}
    if not is_directional(alert):
        return True, {"status": "NOT_REQUIRED", "reason": "non_directional_alert", "queue_file": queue_file}

    symbol = str(alert.get("symbol", "")).strip().upper()
    side = str(alert.get("signal_type", "")).strip().upper()
    sid = signal_id(alert)
    scope = alert_scope(alert)
    queue_alerts, warnings = load_jsonl_tail(queue_file, SYMBOL_CONFLICT_QUEUE_SCAN_LIMIT)
    reasons = []
    if warnings and not queue_alerts:
        reasons.extend(warnings)

    opposite = []
    for candidate in queue_alerts:
        if not is_directional(candidate):
            continue
        if signal_id(candidate) == sid:
            continue
        if str(candidate.get("symbol", "")).strip().upper() != symbol:
            continue
        candidate_side = str(candidate.get("signal_type", "")).strip().upper()
        if candidate_side == side:
            continue
        if not same_alert_scope(candidate, scope):
            continue
        opposite.append(
            {
                "signal_id": signal_id(candidate),
                "symbol": candidate.get("symbol"),
                "signal_type": candidate_side,
                "trigger": candidate.get("trigger"),
                "confirmed": candidate.get("confirmed"),
                "generated_at": candidate.get("generated_at"),
                "strategy_config_id": candidate.get("strategy_config_id"),
                "watchlist_id": candidate.get("watchlist_id"),
            }
        )

    if opposite:
        reasons.append("symbol_conflict_opposite_direction_in_queue")
    payload = {
        "status": "PASS" if not reasons else "REJECTED",
        "queue_file": queue_file,
        "scan_limit": SYMBOL_CONFLICT_QUEUE_SCAN_LIMIT,
        "scope": scope,
        "symbol": symbol,
        "signal_type": side,
        "opposite_count": len(opposite),
        "opposite_alerts": opposite[:20],
        "warnings": warnings,
        "reasons": reasons,
    }
    if mode != "execute":
        payload["status"] = "DRY_RUN_ONLY"
        payload["would_block_execute"] = bool(reasons)
        return True, payload
    return not reasons, payload


def strategy_evidence_gate(alert, mode, report_file=STRATEGY_EVIDENCE_FILE):
    """Require sufficient forward outcome evidence before execute mode."""
    if not REQUIRE_STRATEGY_EVIDENCE:
        return True, {"status": "DISABLED", "report_file": report_file}

    report = load_json_file(report_file, {})
    reasons = []
    if not isinstance(report, dict) or not report:
        reasons.append("strategy_evidence_missing")
        report = {}
    elif report.get("schema") != "rt_signal_outcome_report_v1":
        reasons.append("strategy_evidence_schema_invalid")

    generated_at = parse_time(report.get("generated_at")) if report else None
    if report and not generated_at:
        reasons.append("strategy_evidence_generated_at_missing")
    elif generated_at and datetime.now() - generated_at > timedelta(hours=MAX_OUTCOME_REPORT_AGE_HOURS):
        reasons.append("strategy_evidence_stale")

    horizon = STRATEGY_EVIDENCE_HORIZON
    overall_metric = {}
    trigger_metric = {}
    trigger_key, trigger_row = find_trigger_outcome(report, alert) if report else ("", {})
    if report:
        overall_metric = ((report.get("overall") or {}).get("horizons") or {}).get(horizon) or {}
        if not overall_metric:
            reasons.append(f"strategy_evidence_horizon_missing_{horizon}")
        else:
            reasons.extend(metric_reasons(overall_metric, "resolved_count", MIN_OUTCOME_SAMPLE, "overall"))

        trigger_metric = ((trigger_row.get("horizons") or {}).get(horizon) or {}) if trigger_row else {}
        if MIN_TRIGGER_OUTCOME_SAMPLE > 0:
            if not trigger_metric:
                reasons.append("trigger_outcome_missing")
            else:
                reasons.extend(
                    metric_reasons(
                        trigger_metric,
                        "resolved_count",
                        MIN_TRIGGER_OUTCOME_SAMPLE,
                        "trigger",
                    )
                )

    payload = {
        "status": "PASS" if not reasons else "REJECTED",
        "report_file": report_file,
        "generated_at": report.get("generated_at") if report else None,
        "horizon": horizon,
        "min_outcome_sample": MIN_OUTCOME_SAMPLE,
        "min_trigger_outcome_sample": MIN_TRIGGER_OUTCOME_SAMPLE,
        "min_outcome_win_rate_pct": MIN_OUTCOME_WIN_RATE_PCT,
        "min_outcome_avg_return_pct": MIN_OUTCOME_AVG_RETURN_PCT,
        "trigger_key": trigger_key,
        "overall_metric": overall_metric,
        "trigger_metric": trigger_metric,
        "recommendations": report.get("recommendations") if report else [],
        "reasons": reasons,
    }
    if mode != "execute":
        payload["status"] = "DRY_RUN_ONLY"
        payload["would_block_execute"] = bool(reasons)
        return True, payload
    return not reasons, payload


def market_exception_from_judgment(judgment):
    if not isinstance(judgment, dict):
        return False, ["missing_market_regime_exception"]
    reasons = []
    if judgment.get("market_regime_exception") is not True:
        reasons.append("missing_market_regime_exception")
    reason_text = str(judgment.get("market_regime_exception_reason", "")).strip()
    if len(reason_text) < 20:
        reasons.append("market_regime_exception_reason_too_short")
    try:
        confidence = float(judgment.get("confidence"))
        if confidence < MIN_MARKET_EXCEPTION_CONFIDENCE:
            reasons.append(f"market_exception_confidence_below_{MIN_MARKET_EXCEPTION_CONFIDENCE:g}")
    except (TypeError, ValueError):
        reasons.append("market_exception_confidence_missing")
    return not reasons, reasons


def market_context_gate(alert, plan, mode, hermes_gate=None, report_file=MARKET_CONTEXT_FILE):
    """Block new BUY execution in risk-off markets unless Hermes documents an exception."""
    if not REQUIRE_MARKET_CONTEXT:
        return True, {"status": "DISABLED", "report_file": report_file}
    if not plan or plan.get("side") != "buy":
        return True, {"status": "NOT_REQUIRED", "reason": "non_buy_plan", "report_file": report_file}

    report = load_json_file(report_file, {})
    reasons = []
    if not isinstance(report, dict) or not report:
        reasons.append("market_context_missing")
        report = {}
    elif report.get("schema") != "market_context_report_v1":
        reasons.append("market_context_schema_invalid")

    generated_at = parse_time(report.get("generated_at")) if report else None
    if report and not generated_at:
        reasons.append("market_context_generated_at_missing")
    elif generated_at and datetime.now() - generated_at > timedelta(hours=MAX_MARKET_CONTEXT_AGE_HOURS):
        reasons.append("market_context_stale")

    market = alert_market(alert)
    market_summary = ((report.get("markets") or {}).get(market) or {}) if report else {}
    if report and not market_summary:
        reasons.append(f"market_context_missing_market_{market}")

    regime = market_summary.get("regime")
    risk_level = market_summary.get("risk_level")
    if regime == "risk_off":
        reasons.append("market_regime_risk_off")
    if "buy_signals_against_weak_breadth" in (market_summary.get("notes") or []):
        reasons.append("buy_signals_against_weak_breadth")

    judgment = (hermes_gate or {}).get("judgment")
    exception_ok = False
    exception_reasons = []
    if any(reason in reasons for reason in ("market_regime_risk_off", "buy_signals_against_weak_breadth")):
        exception_ok, exception_reasons = market_exception_from_judgment(judgment)
        if exception_ok:
            reasons = [
                reason
                for reason in reasons
                if reason not in ("market_regime_risk_off", "buy_signals_against_weak_breadth")
            ]
        else:
            reasons.extend(exception_reasons)

    payload = {
        "status": "PASS" if not reasons else "REJECTED",
        "report_file": report_file,
        "generated_at": report.get("generated_at") if report else None,
        "market": market,
        "regime": regime,
        "risk_level": risk_level,
        "recommendations": report.get("recommendations") if report else [],
        "market_summary": {
            "latest_date": market_summary.get("latest_date"),
            "breadth": market_summary.get("breadth"),
            "returns": market_summary.get("returns"),
            "risk": market_summary.get("risk"),
            "v4_signal_summary": market_summary.get("v4_signal_summary"),
            "notes": market_summary.get("notes"),
        },
        "exception_accepted": exception_ok,
        "exception_reasons": exception_reasons,
        "min_market_exception_confidence": MIN_MARKET_EXCEPTION_CONFIDENCE,
        "reasons": reasons,
    }
    if mode != "execute":
        payload["status"] = "DRY_RUN_ONLY"
        payload["would_block_execute"] = bool(reasons)
        return True, payload
    return not reasons, payload


def load_judgments(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except Exception:
        return []
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, list):
            return [item for item in loaded if isinstance(item, dict)]
        if isinstance(loaded, dict):
            for key in ("judgments", "decisions", "items"):
                if isinstance(loaded.get(key), list):
                    return [item for item in loaded[key] if isinstance(item, dict)]
            return [loaded]
    except json.JSONDecodeError:
        pass

    judgments = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            judgments.append(item)
    return judgments


def latest_judgment(sid, path):
    matches = [j for j in load_judgments(path) if str(j.get("signal_id", "")) == str(sid)]
    if not matches:
        return None

    def sort_key(item):
        reviewed = parse_time(item.get("reviewed_at") or item.get("created_at"))
        return reviewed or datetime.min

    return sorted(matches, key=sort_key)[-1]


def build_judgment_request(alert, plan, context):
    return {
        "schema": "hermes_trade_judgment_v1",
        "required_fields": {
            "signal_id": signal_id(alert),
            "decision": "approve|reject|reduce|hold",
            "confidence": "0.0-1.0",
            "reviewed_at": "ISO-8601 datetime",
            "supporting_factors": ["why this trade is acceptable"],
            "opposing_factors": ["what could invalidate the trade"],
            "risk_notes": ["position sizing, event risk, market condition"],
        },
        "optional_fields": {
            "max_quantity": "required when decision=reduce",
            "expiry_minutes": "judgment-specific validity window",
            "reviewer": "Hermes agent identifier",
            "market_regime_exception": "true only when approving a new BUY despite risk_off market_context",
            "market_regime_exception_reason": "specific reason for overriding risk_off breadth context",
        },
        "alert": {
            "symbol": alert.get("symbol"),
            "signal_type": alert.get("signal_type"),
            "trigger": alert.get("trigger"),
            "full_score": alert.get("full_score"),
            "full_reasons": alert.get("full_reasons", []),
            "entry_price": alert.get("entry_price"),
            "stop_loss": alert.get("stop_loss"),
            "take_profit": alert.get("take_profit"),
            "rr_ratio": alert.get("rr_ratio"),
            "generated_at": alert.get("generated_at"),
        },
        "proposed_plan": plan,
        "portfolio_context": {
            "cash_hkd": context["cash_hkd"],
            "equity_hkd": context["equity_hkd"],
            "positions": sorted(context["positions"].keys()),
        },
        "execution_gates": {
            "requires_system_health_ok": True,
            "requires_strategy_evidence": REQUIRE_STRATEGY_EVIDENCE,
            "strategy_evidence_horizon": STRATEGY_EVIDENCE_HORIZON,
            "min_outcome_sample": MIN_OUTCOME_SAMPLE,
            "min_trigger_outcome_sample": MIN_TRIGGER_OUTCOME_SAMPLE,
            "requires_market_context": REQUIRE_MARKET_CONTEXT,
            "min_market_exception_confidence": MIN_MARKET_EXCEPTION_CONFIDENCE,
            "requires_no_symbol_conflict": REQUIRE_NO_SYMBOL_CONFLICT,
        },
    }


def evaluate_hermes_judgment(alert, plan, context, mode, judgment_file):
    sid = signal_id(alert)
    request = build_judgment_request(alert, plan, context)
    if mode != "execute" and os.environ.get("RT_ORDER_SHOW_HERMES_REQUEST", "1") == "0":
        return True, plan, {"status": "SKIPPED", "request": request}
    if mode != "execute":
        judgment = latest_judgment(sid, judgment_file)
        return True, plan, {"status": "DRY_RUN_ONLY", "judgment": judgment, "request": request}
    if not REQUIRE_HERMES_JUDGMENT:
        return True, plan, {"status": "DISABLED", "request": request}

    judgment = latest_judgment(sid, judgment_file)
    if not judgment:
        return False, plan, {
            "status": "MISSING",
            "reasons": ["missing_hermes_judgment"],
            "judgment_file": judgment_file,
            "request": request,
        }

    reasons = []
    decision = str(judgment.get("decision", "")).strip().lower()
    if decision not in ("approve", "reduce"):
        reasons.append(f"decision_{decision or 'missing'}")

    try:
        confidence = float(judgment.get("confidence"))
        if confidence < MIN_HERMES_CONFIDENCE:
            reasons.append("confidence_below_threshold")
    except (TypeError, ValueError):
        reasons.append("missing_confidence")

    reviewed_at = parse_time(judgment.get("reviewed_at") or judgment.get("created_at"))
    if not reviewed_at:
        reasons.append("missing_reviewed_at")
    else:
        expiry_minutes = judgment.get("expiry_minutes", MAX_JUDGMENT_AGE_MINUTES)
        try:
            expiry_minutes = int(expiry_minutes)
        except (TypeError, ValueError):
            expiry_minutes = MAX_JUDGMENT_AGE_MINUTES
        if datetime.now() - reviewed_at > timedelta(minutes=expiry_minutes):
            reasons.append("judgment_expired")

    adjusted_plan = dict(plan)
    if decision == "reduce":
        try:
            max_quantity = int(float(judgment.get("max_quantity")))
        except (TypeError, ValueError):
            reasons.append("missing_max_quantity_for_reduce")
            max_quantity = 0
        if max_quantity > 0:
            reduced = round_down_lot(min(plan["quantity"], max_quantity), plan["symbol"])
            if reduced <= 0:
                reasons.append("reduced_quantity_zero")
            else:
                ratio = reduced / plan["quantity"]
                adjusted_plan["quantity"] = reduced
                adjusted_plan["notional_hkd"] = plan["notional_hkd"] * ratio
                adjusted_plan["risk_hkd"] = plan["risk_hkd"] * ratio
                adjusted_plan["hermes_reduced_from"] = plan["quantity"]

    if reasons:
        return False, plan, {
            "status": "REJECTED",
            "reasons": reasons,
            "judgment": judgment,
            "request": request,
        }

    return True, adjusted_plan, {
        "status": "APPROVED",
        "decision": decision,
        "judgment": judgment,
        "request": request,
    }


def validate_alert(alert):
    reasons = []
    symbol = str(alert.get("symbol", "")).strip().upper()
    side = str(alert.get("signal_type", "")).strip().upper()
    if not symbol:
        reasons.append("missing_symbol")
    if side not in ("BUY", "SELL"):
        reasons.append("unsupported_signal_type")
    if alert.get("execution_candidate") is not True:
        reasons.append("not_execution_candidate")
    if os.environ.get("RT_ORDER_REQUIRE_CONFIRMED", "1") != "0" and not alert.get("confirmed", False):
        reasons.append("not_confirmed")

    for key in ("entry_price", "stop_loss", "take_profit"):
        try:
            value = float(alert.get(key))
            if value <= 0:
                reasons.append(f"invalid_{key}")
        except (TypeError, ValueError):
            reasons.append(f"invalid_{key}")

    full_score = alert.get("full_score")
    try:
        score = float(full_score)
        if side == "BUY" and score < MIN_BUY_SCORE:
            reasons.append("buy_score_below_threshold")
        if side == "SELL" and score > MAX_SELL_SCORE:
            reasons.append("sell_score_above_threshold")
    except (TypeError, ValueError):
        reasons.append("missing_full_score")

    try:
        rr_ratio = float(alert.get("rr_ratio"))
        if rr_ratio < MIN_RR_RATIO:
            reasons.append("rr_ratio_below_threshold")
    except (TypeError, ValueError):
        reasons.append("missing_rr_ratio")

    generated_at = parse_time(alert.get("generated_at"))
    if generated_at:
        age = datetime.now() - generated_at
        if age > timedelta(minutes=MAX_ALERT_AGE_MINUTES):
            reasons.append("alert_too_old")
    else:
        reasons.append("missing_generated_at")

    if not reasons:
        entry = float(alert["entry_price"])
        stop = float(alert["stop_loss"])
        take = float(alert["take_profit"])
        if side == "BUY" and not (stop < entry < take):
            reasons.append("invalid_buy_price_geometry")
        if side == "SELL" and not (take < entry < stop):
            reasons.append("invalid_sell_price_geometry")

    return reasons


def build_order_plan(alert, context):
    symbol = str(alert["symbol"]).strip().upper()
    side = str(alert["signal_type"]).strip().upper()
    entry = float(alert["entry_price"])
    stop = float(alert["stop_loss"])
    fx = fx_to_hkd(symbol)
    positions = context["positions"]
    held = positions.get(symbol)

    if side == "SELL":
        if not held:
            return None, ["sell_without_position"]
        quantity = round_down_lot(held["quantity"] * SELL_FRACTION, symbol)
        if quantity <= 0:
            return None, ["sell_quantity_zero"]
        return {
            "symbol": symbol,
            "side": "sell",
            "quantity": quantity,
            "price_reference": entry,
            "risk_hkd": 0.0,
            "notional_hkd": quantity * entry * fx,
        }, []

    if held:
        return None, ["already_holding_symbol"]
    if len(positions) >= MAX_POSITIONS:
        return None, ["max_positions_reached"]

    risk_per_share_hkd = abs(entry - stop) * fx
    if risk_per_share_hkd <= 0:
        return None, ["risk_per_share_zero"]
    equity = context["equity_hkd"]
    cash = context["cash_hkd"]
    max_loss_hkd = equity * MAX_RISK_PCT
    max_alloc_hkd = min(equity * POSITION_SIZE_PCT, cash)
    quantity_by_risk = max_loss_hkd / risk_per_share_hkd
    quantity_by_alloc = max_alloc_hkd / (entry * fx)
    quantity = round_down_lot(min(quantity_by_risk, quantity_by_alloc), symbol)
    if quantity <= 0:
        return None, ["quantity_zero_after_risk_and_lot_rounding"]

    notional_hkd = quantity * entry * fx
    if notional_hkd > cash:
        return None, ["insufficient_cash_after_rounding"]
    return {
        "symbol": symbol,
        "side": "buy",
        "quantity": quantity,
        "price_reference": entry,
        "risk_hkd": quantity * risk_per_share_hkd,
        "notional_hkd": notional_hkd,
    }, []


def record_processed(state, sid, payload, state_file):
    state["processed"][sid] = payload
    save_json_atomic(state_file, state)


def record_dry_run(state, sid, payload, state_file):
    state["dry_runs"][sid] = payload
    save_json_atomic(state_file, state)


def record_decision(state, sid, payload, state_file, mode):
    if mode == "execute":
        record_processed(state, sid, payload, state_file)
    else:
        record_dry_run(state, sid, payload, state_file)


def process_alert(alert, mode, state, state_file, judgment_file=JUDGMENT_FILE):
    sid = signal_id(alert)
    if sid in state["processed"]:
        return {
            "signal_id": sid,
            "status": "duplicate",
            "detail": "signal_id already processed",
            "previous": state["processed"][sid],
        }

    validation_errors = validate_alert(alert)
    if validation_errors:
        decision = {
            "signal_id": sid,
            "status": "rejected",
            "reasons": validation_errors,
            "checked_at": now_iso(),
        }
        record_decision(state, sid, decision, state_file, mode)
        return decision

    health_ok, health = health_gate(mode)
    if not health_ok:
        decision = {
            "signal_id": sid,
            "status": "rejected",
            "reasons": ["health_gate_failed"],
            "health": health,
            "checked_at": now_iso(),
        }
        record_decision(state, sid, decision, state_file, mode)
        return decision

    strategy_ok, strategy_gate = strategy_evidence_gate(alert, mode)
    if not strategy_ok:
        decision = {
            "signal_id": sid,
            "status": "rejected",
            "reasons": ["strategy_evidence_gate_failed"],
            "strategy_evidence": strategy_gate,
            "checked_at": now_iso(),
        }
        record_decision(state, sid, decision, state_file, mode)
        return decision

    conflict_ok, conflict_gate = symbol_conflict_gate(alert, mode)
    if not conflict_ok:
        decision = {
            "signal_id": sid,
            "status": "rejected",
            "reasons": ["symbol_conflict_gate_failed"],
            "strategy_evidence": strategy_gate,
            "symbol_conflict": conflict_gate,
            "checked_at": now_iso(),
        }
        record_decision(state, sid, decision, state_file, mode)
        return decision

    token, context, context_warnings = fetch_context()
    plan, plan_errors = build_order_plan(alert, context)
    if plan_errors:
        decision = {
            "signal_id": sid,
            "status": "rejected",
            "reasons": plan_errors,
            "warnings": context_warnings,
            "strategy_evidence": strategy_gate,
            "symbol_conflict": conflict_gate,
            "context": {
                "cash_hkd": context["cash_hkd"],
                "equity_hkd": context["equity_hkd"],
                "positions": sorted(context["positions"].keys()),
            },
            "checked_at": now_iso(),
        }
        record_decision(state, sid, decision, state_file, mode)
        return decision

    hermes_ok, plan, hermes_gate = evaluate_hermes_judgment(alert, plan, context, mode, judgment_file)
    if not hermes_ok:
        decision = {
            "signal_id": sid,
            "status": "rejected",
            "reasons": ["hermes_judgment_gate_failed"],
            "hermes": hermes_gate,
            "strategy_evidence": strategy_gate,
            "symbol_conflict": conflict_gate,
            "context": {
                "cash_hkd": context["cash_hkd"],
                "equity_hkd": context["equity_hkd"],
                "positions": sorted(context["positions"].keys()),
            },
            "checked_at": now_iso(),
        }
        record_decision(state, sid, decision, state_file, mode)
        return decision

    market_ok, market_gate = market_context_gate(alert, plan, mode, hermes_gate)
    if not market_ok:
        decision = {
            "signal_id": sid,
            "status": "rejected",
            "reasons": ["market_context_gate_failed"],
            "market_context": market_gate,
            "strategy_evidence": strategy_gate,
            "symbol_conflict": conflict_gate,
            "hermes": hermes_gate,
            "context": {
                "cash_hkd": context["cash_hkd"],
                "equity_hkd": context["equity_hkd"],
                "positions": sorted(context["positions"].keys()),
            },
            "checked_at": now_iso(),
        }
        record_decision(state, sid, decision, state_file, mode)
        return decision

    base = {
        "signal_id": sid,
        "status": "dry_run" if mode != "execute" else "inflight",
        "mode": mode,
        "plan": plan,
        "warnings": context_warnings,
        "context": {
            "cash_hkd": context["cash_hkd"],
            "equity_hkd": context["equity_hkd"],
            "positions": sorted(context["positions"].keys()),
        },
        "strategy_evidence": strategy_gate,
        "symbol_conflict": conflict_gate,
        "market_context": market_gate,
        "hermes": hermes_gate,
        "alert": {
            "symbol": alert.get("symbol"),
            "signal_type": alert.get("signal_type"),
            "trigger": alert.get("trigger"),
            "full_score": alert.get("full_score"),
            "generated_at": alert.get("generated_at"),
        },
        "checked_at": now_iso(),
    }

    if mode != "execute":
        record_dry_run(state, sid, base, state_file)
        return base

    if not token:
        decision = dict(base)
        decision.update({"status": "rejected", "reasons": ["api_token_missing"]})
        record_processed(state, sid, decision, state_file)
        return decision

    record_processed(state, sid, base, state_file)
    try:
        result = submit_order(token, plan["symbol"], plan["side"], plan["quantity"], plan["price_reference"])
        final = dict(base)
        final.update({"status": "submitted", "order_result": result, "submitted_at": now_iso()})
        record_processed(state, sid, final, state_file)
        return final
    except Exception as exc:
        final = dict(base)
        final.update({"status": "error", "error": str(exc), "error_at": now_iso()})
        record_processed(state, sid, final, state_file)
        return final


def load_alerts_from_args(args):
    if args.alert_json:
        loaded = json.loads(args.alert_json)
        return loaded if isinstance(loaded, list) else [loaded]
    if args.alert_file:
        loaded = load_json_file(args.alert_file, [])
        return loaded if isinstance(loaded, list) else [loaded]
    path = args.queue_file or ALERT_QUEUE_FILE
    alerts = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-args.limit :]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                alerts.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not alerts and os.path.exists(ALERT_FILE):
        loaded = load_json_file(ALERT_FILE, [])
        alerts = loaded if isinstance(loaded, list) else [loaded]
    return alerts[-args.limit :]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--alert-json", help="one alert JSON object or a JSON list")
    parser.add_argument("--alert-file", help="JSON file containing one alert object or a list")
    parser.add_argument("--queue-file", help="JSONL alert queue; defaults to RT_ALERT_QUEUE_FILE")
    parser.add_argument("--state-file", default=STATE_FILE)
    parser.add_argument("--judgment-file", default=JUDGMENT_FILE)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--mode",
        choices=("dry-run", "execute"),
        default=os.environ.get("RT_ORDER_EXECUTION_MODE", "dry-run"),
    )
    args = parser.parse_args()

    state = load_state(args.state_file)
    alerts = load_alerts_from_args(args)
    results = [process_alert(alert, args.mode, state, args.state_file, args.judgment_file) for alert in alerts]
    payload = {"mode": args.mode, "count": len(results), "results": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if any(result.get("status") in ("error", "rejected") for result in results):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
