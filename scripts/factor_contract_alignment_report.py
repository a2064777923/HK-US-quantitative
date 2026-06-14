#!/usr/bin/env python3
"""Read-only report comparing v5 signal factors with local backtests.

The goal is not to prove profitability. The report makes factor-contract drift
visible so Hermes and operators do not treat a local backtest as direct proof
of the realtime v5 alert path when scoring, trigger, or risk semantics differ.
"""
import argparse
import ast
import json
import os
import re
from collections import Counter
from datetime import datetime


DEFAULT_V5_FILE = os.environ.get("FACTOR_ALIGNMENT_V5_FILE", "scripts/rt_signal_engine_v5.py")
DEFAULT_REALISTIC_FILE = os.environ.get(
    "FACTOR_ALIGNMENT_REALISTIC_BACKTEST_FILE",
    "backtest/portfolio_backtest_realistic.py",
)
DEFAULT_COMBINED_FILE = os.environ.get(
    "FACTOR_ALIGNMENT_COMBINED_BACKTEST_FILE",
    "backtest/portfolio_backtest_combined.py",
)
DEFAULT_OUTPUT_FILE = os.environ.get("FACTOR_CONTRACT_ALIGNMENT_REPORT_FILE", "/tmp/factor_contract_alignment_report.json")

STATUS_RANK = {"OK": 0, "INFO": 0, "WARN": 1, "FAIL": 2}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def save_json_atomic(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{datetime.now().strftime('%Y%m%d%H%M%S%f')}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def as_number(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = as_number(node.operand)
        return -value if value is not None else None
    return None


def assigned_numbers(source):
    values = {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return values
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = as_number(node.value)
        if value is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                values[target.id] = value
    return values


def has_any(source, patterns):
    return any(pattern in source for pattern in patterns)


def regex_any(source, patterns):
    return any(re.search(pattern, source, flags=re.MULTILINE) for pattern in patterns)


def extract_v5_contract(source):
    constants = assigned_numbers(source)
    factors = {
        "trend_ma_stack": has_any(source, ["c > ma5 > ma10 > ma20", "c>ma5>ma10>ma20"]),
        "rsi": "rsi_14" in source and regex_any(source, [r"rsi_14\s*>\s*70", r"rsi_14\s*<\s*30"]),
        "macd": "macd_hist" in source and "macd_dif" in source,
        "bollinger": "signal_bollinger_bands" in source,
        "volume": "score_volume_ratio" in source,
        "directional_volume": "放量下跌" in source or "c < prior_close" in source,
        "momentum_5d": "MOMENTUM_THRESHOLD_PCT" in source and "lookback_close(closes, 5)" in source,
        "ma20_slope": "ma20-m" in source or "ma20 - m" in source,
    }
    return {
        "name": "rt_signal_engine_v5",
        "scoring_method": "IncrementalIndicator.get_score",
        "factors": factors,
        "thresholds": {
            "buy_confirmation_min_score": constants.get("BUY_CONFIRMATION_MIN_SCORE"),
            "sell_confirmation_max_score": constants.get("SELL_CONFIRMATION_MAX_SCORE"),
            "volume_anomaly_ratio": constants.get("VOLUME_ANOMALY_RATIO"),
            "momentum_threshold_pct": constants.get("MOMENTUM_THRESHOLD_PCT"),
            "min_signal_history_bars": constants.get("MIN_SIGNAL_HISTORY_BARS"),
        },
        "trigger_model": {
            "event_triggered": "triggered.append" in source and "class TriggerEngine" in source,
            "score_confirmation": "def is_confirmed" in source,
            "watch_downgrade": "emit_unconfirmed_directional_as_watch" in source,
        },
        "risk_model": {
            "execution_candidate": "execution_candidate" in source,
            "risk_geometry": "def risk_geometry" in source,
            "risk_reward_ratio": "def risk_reward_ratio" in source,
            "min_rr_ratio": "min_rr_ratio" in source,
            "atr_stop_take_profit": "atr_stop_multiple" in source and "atr_take_profit_multiple" in source,
            "trailing_chandelier_stop": "ch_stop" in source or "Chandelier" in source,
        },
        "data_basis": {
            "completed_daily_with_realtime_quote": "completed_daily_ohlcv_with_realtime_quote" in source,
            "single_quote_temporary_bar": "single_quote_temporary_bar" in source,
            "daily_only": False,
        },
    }


def extract_backtest_contract(name, source):
    constants = assigned_numbers(source)
    factors = {
        "trend_ma_stack": has_any(source, ["c>ma5>ma10>ma20", "c > ma5 > ma10 > ma20"]),
        "rsi": regex_any(source, [r"def\s+rsi", r"\br\s*=\s*rsi"]),
        "macd": "hist" in source and "ema(closes,12)" in source.replace(" ", ""),
        "bollinger": "closes[-20:]" in source and "std" in source,
        "volume": "vr=vols[-1]/a20" in source.replace(" ", "") or "vr = vols[-1]/a20" in source,
        "directional_volume": "c<closes[-2]" in source.replace(" ", "") or "c < closes[-2]" in source,
        "momentum_5d": "MOMENTUM_THRESHOLD_PCT" in source or "lookback_close(closes, 5)" in source,
        "ma20_slope": "ma20-m" in source or "ma20 - m" in source,
    }
    scan_interval = constants.get("SCAN") if constants.get("SCAN") is not None else constants.get("SCAN_INTERVAL")
    return {
        "name": name,
        "scoring_method": "local score()",
        "factors": factors,
        "thresholds": {
            "buy_score": constants.get("BUY"),
            "sell_score": constants.get("SELL"),
            "scan_interval_days": scan_interval,
            "slippage": constants.get("SLIP"),
        },
        "trigger_model": {
            "periodic_score_scan": scan_interval is not None,
            "score_confirmation": "sc >= BUY" in source or "sc>=BUY" in source,
            "watch_downgrade": False,
            "event_triggered": False,
        },
        "risk_model": {
            "execution_candidate": "execution_candidate" in source,
            "risk_geometry": "risk_geometry" in source,
            "risk_reward_ratio": "rr_ratio" in source,
            "min_rr_ratio": "min_rr_ratio" in source,
            "atr_stop_take_profit": "atr" in source.lower(),
            "trailing_chandelier_stop": "ch_stop" in source,
        },
        "data_basis": {
            "completed_daily_with_realtime_quote": False,
            "single_quote_temporary_bar": False,
            "daily_only": True,
        },
    }


def worst_status(statuses):
    status = "OK"
    for item in statuses:
        if STATUS_RANK.get(item, 0) > STATUS_RANK.get(status, 0):
            status = item
    return status


def check(status, code, detail, data=None):
    return {"status": status, "code": code, "detail": detail, "data": data or {}}


def compare_factor_sets(v5, backtests):
    checks = []
    v5_factors = {key for key, enabled in (v5.get("factors") or {}).items() if enabled}
    for backtest in backtests:
        bt_factors = {key for key, enabled in (backtest.get("factors") or {}).items() if enabled}
        common = sorted(v5_factors & bt_factors)
        v5_only = sorted(v5_factors - bt_factors)
        bt_only = sorted(bt_factors - v5_factors)
        if len(common) >= 5 and (v5_only or bt_only):
            status = "WARN"
            code = "factor_family_partial_alignment"
            detail = "Core factor families overlap, but the v5 and backtest scoring contracts are not identical."
        elif len(common) >= 5:
            status = "OK"
            code = "factor_family_aligned"
            detail = "Core factor families are aligned at the static contract level."
        else:
            status = "FAIL"
            code = "factor_family_mismatch"
            detail = "Backtest factor families do not cover enough of v5 scoring to support v5 claims."
        checks.append(
            check(
                status,
                f"{backtest['name']}:{code}",
                detail,
                {"common": common, "v5_only": v5_only, "backtest_only": bt_only},
            )
        )
    return checks


def compare_thresholds(v5, backtests):
    checks = []
    v5_thresholds = v5.get("thresholds") or {}
    for backtest in backtests:
        bt_thresholds = backtest.get("thresholds") or {}
        buy = bt_thresholds.get("buy_score")
        sell = bt_thresholds.get("sell_score")
        v5_buy = v5_thresholds.get("buy_confirmation_min_score")
        v5_sell = v5_thresholds.get("sell_confirmation_max_score")
        aligned = buy == v5_buy and sell == v5_sell
        checks.append(
            check(
                "OK" if aligned else "WARN",
                f"{backtest['name']}:{'score_thresholds_aligned' if aligned else 'score_thresholds_drift'}",
                "Backtest score thresholds match v5 confirmation thresholds."
                if aligned
                else "Backtest score thresholds differ from v5 full-score confirmation thresholds.",
                {
                    "backtest_buy_score": buy,
                    "backtest_sell_score": sell,
                    "v5_buy_min_full_score": v5_buy,
                    "v5_sell_max_full_score": v5_sell,
                },
            )
        )
    return checks


def compare_trigger_and_risk(v5, backtests):
    checks = []
    v5_trigger = v5.get("trigger_model") or {}
    v5_risk = v5.get("risk_model") or {}
    for backtest in backtests:
        bt_trigger = backtest.get("trigger_model") or {}
        bt_risk = backtest.get("risk_model") or {}
        same_trigger = bool(v5_trigger.get("event_triggered")) == bool(bt_trigger.get("event_triggered"))
        checks.append(
            check(
                "OK" if same_trigger else "WARN",
                f"{backtest['name']}:{'trigger_model_aligned' if same_trigger else 'trigger_model_drift'}",
                "Backtest and v5 use the same trigger model."
                if same_trigger
                else "Backtest uses periodic score scans while v5 uses event triggers plus full-score confirmation.",
                {"v5": v5_trigger, "backtest": bt_trigger},
            )
        )
        missing_risk = [
            key
            for key in ("execution_candidate", "risk_geometry", "risk_reward_ratio", "min_rr_ratio")
            if v5_risk.get(key) and not bt_risk.get(key)
        ]
        checks.append(
            check(
                "WARN" if missing_risk else "OK",
                f"{backtest['name']}:{'risk_execution_contract_drift' if missing_risk else 'risk_execution_contract_aligned'}",
                "Backtest does not model all v5 risk/execution gates."
                if missing_risk
                else "Backtest models the v5 risk/execution gates visible to the static check.",
                {"missing_from_backtest": missing_risk, "v5": v5_risk, "backtest": bt_risk},
            )
        )
    return checks


def compare_data_basis(v5, backtests):
    checks = []
    for backtest in backtests:
        checks.append(
            check(
                "WARN",
                f"{backtest['name']}:data_basis_drift",
                "Backtests use completed daily bars, while v5 scores completed daily history with a temporary realtime quote.",
                {"v5": v5.get("data_basis") or {}, "backtest": backtest.get("data_basis") or {}},
            )
        )
    return checks


def duplicate_score_check(backtests):
    enabled = [backtest["name"] for backtest in backtests if backtest.get("scoring_method") == "local score()"]
    if len(enabled) > 1:
        return [
            check(
                "WARN",
                "duplicated_backtest_score_implementations",
                "Multiple backtests carry their own local score() implementation, increasing drift risk.",
                {"backtests": enabled},
            )
        ]
    return []


def recommendations(checks):
    codes = [item["code"] for item in checks if item["status"] in ("WARN", "FAIL")]
    recs = [
        {
            "priority": "HIGH",
            "code": "do_not_treat_current_local_backtests_as_direct_v5_proof",
            "action": "Use local backtests as baseline research evidence only until a v5-compatible replay backtest exists.",
        },
        {
            "priority": "HIGH",
            "code": "add_v5_compatible_replay_backtest",
            "action": "Replay v5 triggers, full-score thresholds, risk geometry, min-RR gate, and execution_candidate semantics on historical bars.",
        },
    ]
    if any("factor_family_partial_alignment" in code or "factor_family_mismatch" in code for code in codes):
        recs.append(
            {
                "priority": "MEDIUM",
                "code": "extract_or_test_shared_factor_contract",
                "action": "Create a shared factor-contract test so backtest and v5 factor definitions cannot drift silently.",
            }
        )
    if any("risk_execution_contract_drift" in code for code in codes):
        recs.append(
            {
                "priority": "MEDIUM",
                "code": "backtest_v5_execution_gates",
                "action": "Model execution_candidate, risk geometry, min RR, and WATCH downgrades in the next research backtest.",
            }
        )
    return recs


def build_report(v5_source, realistic_source, combined_source, source_files=None):
    v5 = extract_v5_contract(v5_source)
    backtests = [
        extract_backtest_contract("portfolio_backtest_realistic", realistic_source),
        extract_backtest_contract("portfolio_backtest_combined", combined_source),
    ]
    checks = []
    checks.extend(compare_factor_sets(v5, backtests))
    checks.extend(compare_thresholds(v5, backtests))
    checks.extend(compare_trigger_and_risk(v5, backtests))
    checks.extend(compare_data_basis(v5, backtests))
    checks.extend(duplicate_score_check(backtests))
    status = worst_status([item["status"] for item in checks])
    overall = "ALIGNED_FOR_V5_RESEARCH" if status == "OK" else "PARTIAL_ALIGNMENT_REQUIRES_CAUTION"
    return {
        "schema": "factor_contract_alignment_report_v1",
        "generated_at": now_iso(),
        "source": {
            "source_files": source_files or {},
            "read_only": True,
            "uses_static_source_analysis": True,
            "imports_backtest_modules": False,
            "uses_credentials": False,
            "mutates_server": False,
            "changes_v5": False,
            "changes_backtests": False,
            "submits_orders": False,
        },
        "summary": {
            "overall_status": overall,
            "promotion_ready": False,
            "hermes_use": "research_alignment_context_only",
            "check_status_counts": dict(Counter(item["status"] for item in checks)),
            "message": "Local backtests are useful baseline evidence, but static contract drift must be considered before treating them as v5 proof.",
        },
        "contracts": {"v5": v5, "backtests": backtests},
        "checks": checks,
        "recommendations": recommendations(checks),
        "hermes_contract": {
            "contract": "research_alignment_context_only",
            "allowed_use": [
                "explain how much local backtests support the current v5 signal contract",
                "identify scoring, trigger, risk, and data-basis drift before strategy promotion",
            ],
            "forbidden_use": [
                "do not approve execution from this report alone",
                "do not override execution readiness, source reliability, or rt_order_intake gates",
                "do not change v5 thresholds or watchlists automatically",
            ],
        },
    }


def text_report(payload):
    summary = payload.get("summary") or {}
    lines = [
        f"Factor contract alignment: {summary.get('overall_status')}",
        f"Hermes use: {summary.get('hermes_use')} promotion_ready={summary.get('promotion_ready')}",
    ]
    for item in payload.get("checks") or []:
        if item.get("status") in ("WARN", "FAIL"):
            lines.append(f"{item.get('status')}: {item.get('code')} - {item.get('detail')}")
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--v5-file", default=DEFAULT_V5_FILE)
    parser.add_argument("--realistic-file", default=DEFAULT_REALISTIC_FILE)
    parser.add_argument("--combined-file", default=DEFAULT_COMBINED_FILE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--text", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    report = build_report(
        read_text(args.v5_file),
        read_text(args.realistic_file),
        read_text(args.combined_file),
        source_files={
            "v5_file": os.path.abspath(args.v5_file),
            "realistic_file": os.path.abspath(args.realistic_file),
            "combined_file": os.path.abspath(args.combined_file),
        },
    )
    save_json_atomic(args.output, report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.text:
        print(text_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
