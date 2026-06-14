#!/usr/bin/env python3
"""Build a read-only reliability report from local backtest artifacts.

This script summarizes local-only HK/US dataset metadata and backtest JSON
results. It intentionally does not fetch data, read credentials, mutate server
state, change strategy config, write alerts, submit simulation orders, or
promote any v5/Hermes behavior.
"""
import argparse
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime


DEFAULT_METADATA_FILE = os.environ.get("LOCAL_BACKTEST_METADATA_FILE", "/tmp/hk_us_dataset_metadata.json")
DEFAULT_REALISTIC_RESULT_FILE = os.environ.get("LOCAL_BACKTEST_REALISTIC_RESULT_FILE", "/tmp/portfolio_bt_realistic.json")
DEFAULT_COMBINED_RESULT_FILE = os.environ.get("LOCAL_BACKTEST_COMBINED_RESULT_FILE", "/tmp/portfolio_bt_v4.json")
DEFAULT_OUTPUT_FILE = os.environ.get("LOCAL_BACKTEST_RELIABILITY_REPORT_FILE", "/tmp/local_backtest_reliability_report.json")

STATUS_RANK = {"OK": 0, "INFO": 0, "WARN": 1, "FAIL": 2}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def as_float(value, default=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def round_or_none(value, digits=2):
    value = as_float(value)
    return round(value, digits) if value is not None else None


def load_json(path):
    last_error = None
    for encoding in ("utf-8", "utf-8-sig", "cp950", "gbk"):
        try:
            with open(path, encoding=encoding) as handle:
                return json.load(handle)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise last_error


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


def worst_status(statuses):
    result = "OK"
    for status in statuses:
        if STATUS_RANK.get(status, 0) > STATUS_RANK.get(result, 0):
            result = status
    return result


def check(status, code, detail, data=None):
    return {"status": status, "code": code, "detail": detail, "data": data or {}}


def date_span_years(start, end):
    try:
        start_dt = datetime.fromisoformat(str(start)[:10])
        end_dt = datetime.fromisoformat(str(end)[:10])
    except (TypeError, ValueError):
        return None
    days = max((end_dt - start_dt).days, 0)
    return round(days / 365.25, 2)


def market_coverage(metadata, market):
    source = (metadata.get("sources") or {}).get(market) or {}
    rows = ((metadata.get("coverage") or {}).get(market) or [])
    first_values = [str(item.get("first")) for item in rows if item.get("first")]
    last_values = [str(item.get("last")) for item in rows if item.get("last")]
    row_counts = [as_int(item.get("rows")) for item in rows]
    sparse = [item.get("symbol") for item in rows if as_int(item.get("rows")) < 200]
    return {
        "provider": source.get("provider"),
        "feed": source.get("feed"),
        "adjustment": source.get("adjustment"),
        "symbol_count_requested": as_int(source.get("symbol_count_requested")),
        "symbol_count_covered": len(rows),
        "row_count": as_int(source.get("row_count")),
        "first": min(first_values) if first_values else None,
        "last": max(last_values) if last_values else None,
        "min_rows_per_symbol": min(row_counts) if row_counts else 0,
        "max_rows_per_symbol": max(row_counts) if row_counts else 0,
        "sparse_symbol_count": len(sparse),
        "sparse_symbols_sample": sparse[:10],
        "errors": source.get("errors") or {},
        "warnings": source.get("warnings") or [],
        "error": source.get("error"),
    }


def dataset_assessment(metadata):
    date_range = metadata.get("date_range") or {}
    hk = market_coverage(metadata, "HK")
    us = market_coverage(metadata, "US")
    total_symbols = hk["symbol_count_covered"] + us["symbol_count_covered"]
    total_rows = hk["row_count"] + us["row_count"]
    checks = []

    storage = metadata.get("storage_policy") or {}
    if storage:
        if storage.get("raw_data_local_only") is True and storage.get("commit_raw_csv_to_git") is False:
            checks.append(check("OK", "raw_data_local_only", "Raw bars are documented as local-only research data.", storage))
        else:
            checks.append(check("FAIL", "raw_data_storage_policy_unsafe", "Raw data storage policy is present but unsafe.", storage))
    else:
        checks.append(
            check(
                "WARN",
                "raw_data_storage_policy_missing",
                "Raw data storage policy is missing from this legacy artifact; treat it as a documentation gap, not proof of unsafe storage.",
                {},
            )
        )

    span = date_span_years(date_range.get("start"), date_range.get("end"))
    if span is None:
        checks.append(check("WARN", "dataset_date_range_unparseable", "Dataset date range could not be parsed.", date_range))
    elif span >= 5:
        checks.append(check("OK", "dataset_span_sufficient_for_baseline", "Dataset spans at least five calendar years.", {"years": span}))
    else:
        checks.append(check("WARN", "dataset_span_short", "Dataset is too short for robust cycle coverage.", {"years": span}))

    if total_symbols >= 200:
        checks.append(check("OK", "universe_breadth_strong", "Local universe breadth is strong for broad research.", {"symbols": total_symbols}))
    elif total_symbols >= 80:
        checks.append(check("WARN", "universe_breadth_baseline_only", "Universe is useful for a baseline but still narrow for institutional claims.", {"symbols": total_symbols}))
    else:
        checks.append(check("FAIL", "universe_breadth_too_small", "Universe is too small for reliable promotion evidence.", {"symbols": total_symbols}))

    if hk["symbol_count_covered"] <= 0 or us["symbol_count_covered"] <= 0:
        checks.append(check("FAIL", "missing_market_coverage", "Both HK and US coverage are required for this system.", {"HK": hk, "US": us}))

    feed = str(us.get("feed") or "").lower()
    if feed and feed != "sip":
        checks.append(
            check(
                "WARN",
                "us_feed_not_full_market",
                "US bars use a non-SIP feed; useful for baselines but not full-market institutional evidence.",
                {"feed": us.get("feed")},
            )
        )

    if str(hk.get("provider") or "").startswith("tencent"):
        checks.append(
            check(
                "WARN",
                "hk_public_provider_needs_cross_vendor_validation",
                "Tencent HK bars are useful but should be cross-validated before claiming production-grade data authority.",
                {"provider": hk.get("provider")},
            )
        )

    if not metadata.get("intraday_outputs"):
        checks.append(
            check(
                "INFO",
                "daily_backtest_only",
                "This report uses daily-bar backtests; intraday bars remain supplemental research evidence.",
            )
        )

    return {
        "date_range": {
            "start": date_range.get("start"),
            "end": date_range.get("end"),
            "span_years": span,
        },
        "markets": {"HK": hk, "US": us},
        "total_symbol_count": total_symbols,
        "total_row_count": total_rows,
        "intraday_outputs": metadata.get("intraday_outputs") or [],
        "checks": checks,
        "status": worst_status([item["status"] for item in checks]),
    }


def infer_market(trade):
    market = str(trade.get("m") or "").upper()
    if market in {"HK", "US"}:
        return market
    symbol = str(trade.get("s") or "").strip().upper()
    if symbol.isdigit():
        return "HK"
    if symbol:
        return "US"
    return "UNKNOWN"


def grouped_trade_stats(trades, key_fn):
    groups = defaultdict(list)
    for trade in trades:
        groups[key_fn(trade)].append(trade)
    rows = []
    for key, items in groups.items():
        wins = [trade for trade in items if as_float(trade.get("pn"), 0) > 0]
        pnl = sum(as_float(trade.get("pn"), 0) or 0 for trade in items)
        rows.append(
            {
                "key": key,
                "trade_count": len(items),
                "win_rate_pct": round(len(wins) / len(items) * 100, 2) if items else 0,
                "pnl": round(pnl, 2),
            }
        )
    return sorted(rows, key=lambda row: (-row["trade_count"], str(row["key"])))


def trade_distribution(trades):
    wins = [trade for trade in trades if as_float(trade.get("pn"), 0) > 0]
    losses = [trade for trade in trades if as_float(trade.get("pn"), 0) <= 0]
    win_pct = [as_float(trade.get("pc"), 0) or 0 for trade in wins]
    loss_pct = [as_float(trade.get("pc"), 0) or 0 for trade in losses]
    avg_win_pct = sum(win_pct) / len(win_pct) if win_pct else 0
    avg_loss_pct = sum(loss_pct) / len(loss_pct) if loss_pct else 0
    consecutive_losses = 0
    max_consecutive_losses = 0
    for trade in trades:
        if as_float(trade.get("pn"), 0) <= 0:
            consecutive_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
        else:
            consecutive_losses = 0
    return {
        "trade_count": len(trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "avg_win_pct": round(avg_win_pct, 2),
        "avg_loss_pct": round(avg_loss_pct, 2),
        "payoff_ratio": round(abs(avg_win_pct / avg_loss_pct), 2) if avg_loss_pct else None,
        "max_consecutive_losses": max_consecutive_losses,
        "by_market": grouped_trade_stats(trades, infer_market),
        "by_exit_reason": grouped_trade_stats(trades, lambda trade: str(trade.get("r") or "UNKNOWN")),
    }


def year_consistency(years):
    rows = []
    for year, item in sorted((years or {}).items()):
        pnl = as_float((item or {}).get("p"), 0) or 0
        rows.append(
            {
                "year": str(year),
                "trade_count": as_int((item or {}).get("c")),
                "pnl": round(pnl, 2),
                "win_rate_pct": round_or_none((item or {}).get("wr")),
                "hk_trades": as_int((item or {}).get("hk")),
                "us_trades": as_int((item or {}).get("us")),
            }
        )
    positive = [row for row in rows if row["pnl"] > 0]
    negative = [row for row in rows if row["pnl"] <= 0]
    return {
        "year_count": len(rows),
        "positive_year_count": len(positive),
        "negative_or_flat_year_count": len(negative),
        "positive_year_ratio": round(len(positive) / len(rows), 3) if rows else None,
        "weakest_year": min(rows, key=lambda row: row["pnl"]) if rows else None,
        "strongest_year": max(rows, key=lambda row: row["pnl"]) if rows else None,
        "rows": rows,
    }


def curve_span(result):
    curve = result.get("equity") or result.get("nav") or []
    dates = [str(item.get("d")) for item in curve if item.get("d")]
    return {
        "point_count": len(curve),
        "first": min(dates) if dates else None,
        "last": max(dates) if dates else None,
    }


def backtest_checks(metrics, consistency):
    checks = []
    trades = as_int(metrics.get("trades"))
    sharpe = as_float(metrics.get("sharpe"))
    dd = as_float(metrics.get("max_drawdown_pct"))
    ret = as_float(metrics.get("total_return_pct"))
    annual = as_float(metrics.get("annual_return_pct"))
    positive_year_ratio = as_float(consistency.get("positive_year_ratio"))

    if trades >= 800:
        checks.append(check("OK", "trade_sample_size_strong", "Trade count is strong enough for baseline analysis.", {"trades": trades}))
    elif trades >= 300:
        checks.append(check("WARN", "trade_sample_size_baseline_only", "Trade count is useful but not enough alone for promotion.", {"trades": trades}))
    else:
        checks.append(check("FAIL", "trade_sample_size_too_small", "Trade count is too small for reliability evidence.", {"trades": trades}))

    if ret is not None and ret > 0 and (annual is None or annual > 0):
        checks.append(check("OK", "returns_positive", "Backtest returns are positive.", {"total_return_pct": ret, "annual_return_pct": annual}))
    else:
        checks.append(check("FAIL", "returns_not_positive", "Backtest return profile is not positive.", {"total_return_pct": ret, "annual_return_pct": annual}))

    if sharpe is None:
        checks.append(check("WARN", "sharpe_missing", "Sharpe ratio is missing.", {}))
    elif sharpe >= 1.0:
        checks.append(check("OK", "sharpe_acceptable", "Sharpe ratio is acceptable for a baseline strategy.", {"sharpe": sharpe}))
    elif sharpe >= 0.7:
        checks.append(check("WARN", "sharpe_marginal", "Sharpe ratio is positive but marginal for promotion evidence.", {"sharpe": sharpe}))
    else:
        checks.append(check("FAIL", "sharpe_too_low", "Sharpe ratio is too low for reliability claims.", {"sharpe": sharpe}))

    if dd is None:
        checks.append(check("WARN", "drawdown_missing", "Max drawdown is missing.", {}))
    elif dd <= 15:
        checks.append(check("OK", "drawdown_controlled", "Max drawdown is controlled in this run.", {"max_drawdown_pct": dd}))
    elif dd <= 25:
        checks.append(check("WARN", "drawdown_moderate", "Max drawdown needs stress and sizing review.", {"max_drawdown_pct": dd}))
    else:
        checks.append(check("FAIL", "drawdown_too_high", "Max drawdown is too high for stable-profit claims.", {"max_drawdown_pct": dd}))

    if consistency.get("year_count", 0) < 3:
        checks.append(check("WARN", "annual_consistency_short", "Not enough annual buckets for consistency assessment.", consistency))
    elif positive_year_ratio is not None and positive_year_ratio >= 0.8:
        checks.append(check("OK", "annual_consistency_positive", "Most annual buckets are profitable.", consistency))
    elif positive_year_ratio is not None and positive_year_ratio >= 0.6:
        checks.append(check("WARN", "annual_consistency_mixed", "Annual consistency is mixed and needs regime validation.", consistency))
    else:
        checks.append(check("FAIL", "annual_consistency_weak", "Annual consistency is too weak for promotion.", consistency))

    return checks


def backtest_assessment(name, result):
    summary = result.get("summary") or {}
    trades = result.get("trades") or []
    annual_return = summary.get("annual", summary.get("cagr"))
    metrics = {
        "initial_capital": round_or_none(summary.get("init")),
        "final_capital": round_or_none(summary.get("final")),
        "total_return_pct": round_or_none(summary.get("ret")),
        "annual_return_pct": round_or_none(annual_return),
        "sharpe": round_or_none(summary.get("sharpe")),
        "sortino": round_or_none(summary.get("sortino")),
        "max_drawdown_pct": round_or_none(summary.get("dd")),
        "calmar": round_or_none(summary.get("calmar")),
        "trades": as_int(summary.get("trades"), len(trades)),
        "win_rate_pct": round_or_none(summary.get("wr")),
    }
    consistency = year_consistency(result.get("years") or {})
    distribution = trade_distribution(trades)
    checks = backtest_checks(metrics, consistency)
    return {
        "name": name,
        "metrics": metrics,
        "curve": curve_span(result),
        "annual_consistency": consistency,
        "trade_distribution": distribution,
        "checks": checks,
        "status": worst_status([item["status"] for item in checks]),
    }


def recommendations(dataset, backtests):
    items = [
        {
            "priority": "HIGH",
            "code": "do_not_promote_strategy_from_single_local_backtest",
            "action": "Use this report as research evidence only; do not change v5 thresholds, order intake, simulation execution, or cron from it alone.",
        },
        {
            "priority": "HIGH",
            "code": "run_walk_forward_and_out_of_sample_validation",
            "action": "Add rolling train/test windows and recent holdout periods before any strategy-config promotion.",
        },
        {
            "priority": "HIGH",
            "code": "align_backtest_signal_definition_with_v5",
            "action": "Verify that the backtest scoring, risk geometry, scan cadence, costs, and v5 alert/intake rules match closely enough before using results as Hermes support.",
        },
        {
            "priority": "MEDIUM",
            "code": "expand_universe_and_regime_coverage",
            "action": "Broaden HK/US symbols and report per-market, per-year, and stressed-regime attribution.",
        },
        {
            "priority": "MEDIUM",
            "code": "upgrade_and_cross_validate_sources",
            "action": "Cross-check HK bars against another vendor and upgrade US evidence beyond IEX when subscription and licensing allow.",
        },
    ]
    if dataset["markets"]["US"].get("feed") and str(dataset["markets"]["US"].get("feed")).lower() != "sip":
        items.append(
            {
                "priority": "MEDIUM",
                "code": "upgrade_us_feed_before_institutional_claims",
                "action": "Treat Alpaca IEX as baseline-only and rerun validation on SIP or another full-market source before institutional-grade claims.",
            }
        )
    if any(bt["status"] != "OK" for bt in backtests):
        items.append(
            {
                "priority": "MEDIUM",
                "code": "investigate_weaker_backtest_variant",
                "action": "Compare the weaker backtest variant against the stronger fixed-size run to identify sizing, drawdown, and stop-loss drag.",
            }
        )
    return items


def build_report(metadata, realistic_result, combined_result, source_files=None):
    dataset = dataset_assessment(metadata)
    backtests = [
        backtest_assessment("portfolio_backtest_realistic", realistic_result),
        backtest_assessment("portfolio_backtest_combined", combined_result),
    ]
    statuses = [dataset["status"]] + [item["status"] for item in backtests]
    evidence_status = worst_status(statuses)
    hard_fail = evidence_status == "FAIL"
    overall_status = "INSUFFICIENT_EVIDENCE" if hard_fail else "RESEARCH_USEFUL_WITH_LIMITATIONS"
    checks = []
    if hard_fail:
        checks.append(
            check(
                "FAIL",
                "local_backtest_evidence_has_hard_failures",
                "One or more dataset/backtest checks failed; use only as diagnostic context.",
            )
        )
    else:
        checks.append(
            check(
                "WARN",
                "local_backtest_not_promotion_ready",
                "Positive local backtests are research evidence, not live/simulation promotion authority.",
            )
        )

    report = {
        "schema": "local_backtest_reliability_report_v1",
        "generated_at": now_iso(),
        "source": {
            "source_files": source_files or {},
            "read_only_inputs": True,
            "writes_output_only": True,
            "local_only": True,
            "mutates_server": False,
            "mutates_git": False,
            "changes_v5": False,
            "changes_order_intake": False,
            "changes_simulation": False,
            "uses_credentials": False,
        },
        "summary": {
            "overall_status": overall_status,
            "promotion_ready": False,
            "hermes_use": "research_evidence_only",
            "dataset_status": dataset["status"],
            "backtest_status_counts": dict(Counter(item["status"] for item in backtests)),
            "best_backtest_by_sharpe": max(
                backtests,
                key=lambda item: as_float(item["metrics"].get("sharpe"), -999) or -999,
            )["name"],
            "message": "Useful local research evidence, but not proof of stable profitability or permission to alter production/simulation gates.",
        },
        "dataset": dataset,
        "backtests": backtests,
        "reliability_checks": checks,
        "recommendations": recommendations(dataset, backtests),
        "hermes_contract": {
            "contract": "research_evidence_only",
            "allowed_use": [
                "summarize baseline performance context",
                "identify research risks and next validation work",
                "challenge or support hypotheses before separate promotion review",
            ],
            "forbidden_use": [
                "do not approve live or simulation execution from this report alone",
                "do not bypass rt_order_intake gates",
                "do not change v5 thresholds or watchlists automatically",
                "do not treat local CSVs as live data-health authority",
                "do not copy raw local data to GitHub or the production server by default",
            ],
        },
    }
    return report


def text_report(payload):
    lines = []
    summary = payload.get("summary") or {}
    dataset = payload.get("dataset") or {}
    lines.append(f"Local backtest reliability: {summary.get('overall_status')}")
    lines.append(f"Hermes use: {summary.get('hermes_use')} promotion_ready={summary.get('promotion_ready')}")
    lines.append(
        "Dataset: "
        f"symbols={dataset.get('total_symbol_count')} rows={dataset.get('total_row_count')} "
        f"range={(dataset.get('date_range') or {}).get('start')}..{(dataset.get('date_range') or {}).get('end')}"
    )
    for backtest in payload.get("backtests") or []:
        metrics = backtest.get("metrics") or {}
        lines.append(
            f"{backtest.get('name')}: status={backtest.get('status')} "
            f"ret={metrics.get('total_return_pct')}% annual={metrics.get('annual_return_pct')}% "
            f"sharpe={metrics.get('sharpe')} dd={metrics.get('max_drawdown_pct')}% "
            f"trades={metrics.get('trades')} wr={metrics.get('win_rate_pct')}%"
        )
    warnings = []
    for section in [dataset] + list(payload.get("backtests") or []):
        for item in section.get("checks") or []:
            if item.get("status") in {"WARN", "FAIL"}:
                warnings.append(item.get("code"))
    if warnings:
        lines.append("Warnings: " + ", ".join(warnings[:12]))
    lines.append("Contract: research evidence only; no v5/order-intake/simulation/cron mutation.")
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata-file", default=DEFAULT_METADATA_FILE)
    parser.add_argument("--realistic-result-file", default=DEFAULT_REALISTIC_RESULT_FILE)
    parser.add_argument("--combined-result-file", default=DEFAULT_COMBINED_RESULT_FILE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--text", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    metadata = load_json(args.metadata_file)
    realistic_result = load_json(args.realistic_result_file)
    combined_result = load_json(args.combined_result_file)
    report = build_report(
        metadata,
        realistic_result,
        combined_result,
        source_files={
            "metadata_file": os.path.abspath(args.metadata_file),
            "realistic_result_file": os.path.abspath(args.realistic_result_file),
            "combined_result_file": os.path.abspath(args.combined_result_file),
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
