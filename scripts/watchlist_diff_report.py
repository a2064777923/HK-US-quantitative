#!/usr/bin/env python3
"""Read-only diff between live v5 watchlist and ranked candidate watchlist."""
import argparse
import hashlib
import json
import os
from datetime import datetime


LIVE_WATCHLIST_FILE = os.environ.get("RT_SIGNAL_WATCHLIST_FILE", "/root/rt_signal_watchlist.json")
CANDIDATE_WATCHLIST_FILE = os.environ.get(
    "RT_SIGNAL_WATCHLIST_CANDIDATE_FILE",
    "/tmp/rt_signal_watchlist_candidate.json",
)
UNIVERSE_RANK_REPORT_FILE = os.environ.get("UNIVERSE_RANK_REPORT_FILE", "/tmp/universe_rank_report.json")
UNIVERSE_HYGIENE_REPORT_FILE = os.environ.get("UNIVERSE_HYGIENE_REPORT_FILE", "/tmp/universe_hygiene_report.json")
REPORT_FILE = os.environ.get("WATCHLIST_DIFF_REPORT_FILE", "/tmp/watchlist_diff_report.json")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_json(path, default=None):
    default = {} if default is None else default
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else default
    except Exception:
        return default


def symbols_for(payload, market):
    market_payload = ((payload or {}).get("markets") or {}).get(market) or {}
    symbols = market_payload.get("symbols") or []
    return [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]


def stable_hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def proposal_hash_for_payload(proposal):
    return stable_hash(
        {
            "schema": proposal.get("schema"),
            "markets": proposal.get("markets") or {},
        }
    )


def ranked_lookup(universe_payload):
    by_market_symbol = {}
    for market, payload in ((universe_payload or {}).get("markets") or {}).items():
        ranked_items = list(payload.get("ranked_symbols") or [])
        ranked_items.extend(payload.get("top_ranked") or [])
        for item in ranked_items:
            symbol = str(item.get("symbol") or "").upper()
            if symbol and (market, symbol) not in by_market_symbol:
                by_market_symbol[(market, symbol)] = item
    return by_market_symbol


def hygiene_lookup(hygiene_payload):
    by_market_symbol = {}
    for market, payload in ((hygiene_payload or {}).get("markets") or {}).items():
        items = []
        for key in ("active_symbols", "all_problem_symbols", "high_priority_candidates", "refetch_candidates"):
            items.extend(payload.get(key) or [])
        for item in items:
            symbol = str(item.get("symbol") or "").upper()
            if symbol and (market, symbol) not in by_market_symbol:
                by_market_symbol[(market, symbol)] = item
    return by_market_symbol


def active_symbols_for_market(hygiene_payload, market):
    payload = ((hygiene_payload or {}).get("markets") or {}).get(market) or {}
    symbols = []
    for item in payload.get("active_symbols") or []:
        symbol = str(item.get("symbol") or "").upper()
        if symbol:
            symbols.append(symbol)
    return sorted(set(symbols))


def ranked_symbols_for_market(universe_payload, market):
    payload = ((universe_payload or {}).get("markets") or {}).get(market) or {}
    symbols = []
    ranked_items = list(payload.get("ranked_symbols") or [])
    ranked_items.extend(payload.get("top_ranked") or [])
    for item in ranked_items:
        symbol = str(item.get("symbol") or "").upper()
        if symbol:
            symbols.append(symbol)
    return sorted(set(symbols))


def ranked_coverage(market, universe_payload, hygiene_payload, hygiene_by_symbol):
    active = set(active_symbols_for_market(hygiene_payload, market))
    ranked = set(ranked_symbols_for_market(universe_payload, market))
    active_not_ranked = sorted(active - ranked)
    ranked_not_active = sorted(ranked - active)
    active_not_ranked_context = [
        {
            "symbol": symbol,
            "hygiene": hygiene_context(market, symbol, hygiene_by_symbol),
        }
        for symbol in active_not_ranked[:80]
    ]
    return {
        "active_symbol_count": len(active),
        "ranked_symbol_count": len(ranked),
        "active_not_ranked_count": len(active_not_ranked),
        "ranked_not_active_count": len(ranked_not_active),
        "active_not_ranked_symbols": active_not_ranked[:120],
        "ranked_not_active_symbols": ranked_not_active[:120],
        "active_not_ranked_context": active_not_ranked_context,
    }


def hygiene_context(market, symbol, lookup):
    item = lookup.get((market, symbol)) or {}
    if not item:
        return None
    issues = item.get("issues") or []
    return {
        "severity": item.get("severity"),
        "recommended_action": item.get("recommended_action"),
        "issues": issues,
        "latest_date": item.get("latest_date"),
        "market_latest_date": item.get("market_latest_date"),
        "lag_days_vs_market_latest": item.get("lag_days_vs_market_latest"),
    }


def symbol_context(market, symbol, ranking_lookup, hygiene_by_symbol=None):
    hygiene_by_symbol = hygiene_by_symbol or {}
    item = ranking_lookup.get((market, symbol)) or {}
    hygiene = hygiene_context(market, symbol, hygiene_by_symbol)
    blockers = item.get("blockers") or []
    if not blockers and hygiene:
        if hygiene.get("recommended_action") == "keep_active":
            blockers = ["active_universe_not_ranked"]
        else:
            blockers = [f"hygiene:{issue}" for issue in hygiene.get("issues") or []]
            if not blockers:
                blockers = ["hygiene_problem_symbol"]
    if not blockers:
        blockers = ["not_in_active_or_ranked_universe"]
    return {
        "symbol": symbol,
        "universe_score": item.get("universe_score"),
        "include_candidate": item.get("include_candidate"),
        "sim_tradability": item.get("sim_tradability"),
        "min_lot_notional_hkd": item.get("min_lot_notional_hkd"),
        "sim_max_alloc_hkd": item.get("sim_max_alloc_hkd"),
        "blockers": blockers,
        "reasons": item.get("reasons") or [],
        "hygiene": hygiene,
    }


def diff_market(market, live_symbols, candidate_symbols, ranking_lookup, hygiene_by_symbol=None, coverage=None):
    live_set = set(live_symbols)
    candidate_set = set(candidate_symbols)
    additions = sorted(candidate_set - live_set)
    removals = sorted(live_set - candidate_set)
    unchanged = sorted(live_set & candidate_set)
    removal_context = [symbol_context(market, symbol, ranking_lookup, hygiene_by_symbol) for symbol in removals]
    addition_context = [symbol_context(market, symbol, ranking_lookup, hygiene_by_symbol) for symbol in additions]
    blocker_counts = {}
    for item in removal_context:
        for blocker in item.get("blockers") or []:
            blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
    hygiene_problem_remove_count = len([item for item in removal_context if item.get("hygiene")])
    return {
        "market": market,
        "live_count": len(live_symbols),
        "candidate_count": len(candidate_symbols),
        "unchanged_count": len(unchanged),
        "add_count": len(additions),
        "remove_count": len(removals),
        "unchanged_symbols": unchanged,
        "add_symbols": additions,
        "remove_symbols": removals,
        "add_context": addition_context[:50],
        "remove_context": removal_context[:80],
        "remove_blocker_counts": blocker_counts,
        "hygiene_problem_remove_count": hygiene_problem_remove_count,
        "ranked_coverage": coverage or {},
    }


def build_proposal(markets, generated_at):
    proposal_markets = {}
    for market, payload in sorted(markets.items()):
        remove_context = payload.get("remove_context") or []
        missing_active = [
            item["symbol"]
            for item in remove_context
            if "not_in_active_or_ranked_universe" in (item.get("blockers") or [])
        ]
        proposal_markets[market] = {
            "add_symbols": payload.get("add_symbols") or [],
            "remove_symbols": payload.get("remove_symbols") or [],
            "remove_symbols_missing_active_universe": missing_active,
            "review_required": bool(payload.get("add_count") or payload.get("remove_count")),
        }
    proposal = {
        "schema": "rt_signal_watchlist_change_proposal_v1",
        "generated_at": generated_at,
        "source": {
            "report_schema": "watchlist_diff_report_v1",
            "manual_review_required": True,
            "auto_applied": False,
            "does_not_restart_services": True,
            "does_not_submit_orders": True,
        },
        "markets": proposal_markets,
    }
    proposal["proposal_hash"] = proposal_hash_for_payload(proposal)
    return proposal


def build_recommendations(markets):
    recs = []
    for market, payload in sorted(markets.items()):
        if payload["remove_blocker_counts"].get("sim_allocation_below_one_lot"):
            recs.append(f"{market}:review_removing_sim_allocation_below_one_lot_symbols")
        if payload["remove_blocker_counts"].get("stale_latest_kline"):
            recs.append(f"{market}:review_removing_stale_live_watchlist_symbols")
        if payload["remove_blocker_counts"].get("active_universe_not_ranked"):
            recs.append(f"{market}:investigate_active_symbols_missing_from_ranked_universe")
        coverage = payload.get("ranked_coverage") or {}
        if coverage.get("active_not_ranked_count"):
            recs.append(f"{market}:ranked_coverage_missing_active_symbols:{coverage['active_not_ranked_count']}")
        if payload["remove_blocker_counts"].get("not_in_active_or_ranked_universe"):
            recs.append(f"{market}:review_live_watchlist_symbols_missing_from_active_universe")
        hygiene_count = payload.get("hygiene_problem_remove_count", 0)
        if hygiene_count:
            recs.append(f"{market}:review_removing_universe_hygiene_problem_symbols:{hygiene_count}")
        if payload["add_count"]:
            recs.append(f"{market}:manual_review_{payload['add_count']}_candidate_additions")
        if payload["remove_count"]:
            recs.append(f"{market}:manual_review_{payload['remove_count']}_candidate_removals")
    if not recs:
        recs.append("watchlist_candidate_matches_live_watchlist")
    return recs


def build_report(
    live_watchlist_file=LIVE_WATCHLIST_FILE,
    candidate_watchlist_file=CANDIDATE_WATCHLIST_FILE,
    universe_rank_report_file=UNIVERSE_RANK_REPORT_FILE,
    universe_hygiene_report_file=UNIVERSE_HYGIENE_REPORT_FILE,
):
    live = load_json(live_watchlist_file)
    candidate = load_json(candidate_watchlist_file)
    universe = load_json(universe_rank_report_file)
    hygiene = load_json(universe_hygiene_report_file)
    lookup = ranked_lookup(universe)
    hygiene_by_symbol = hygiene_lookup(hygiene)
    markets = {}
    for market in sorted(set((live.get("markets") or {}).keys()) | set((candidate.get("markets") or {}).keys())):
        coverage = ranked_coverage(market, universe, hygiene, hygiene_by_symbol)
        markets[market] = diff_market(
            market,
            symbols_for(live, market),
            symbols_for(candidate, market),
            lookup,
            hygiene_by_symbol,
            coverage,
        )
    live_symbols = {market: symbols_for(live, market) for market in sorted((live.get("markets") or {}).keys())}
    candidate_symbols = {
        market: symbols_for(candidate, market)
        for market in sorted((candidate.get("markets") or {}).keys())
    }
    generated_at = now_iso()
    proposal = build_proposal(markets, generated_at)
    payload = {
        "schema": "watchlist_diff_report_v1",
        "generated_at": generated_at,
        "source": {
            "read_only": True,
            "manual_review_required": True,
            "auto_applies_watchlist": False,
            "submits_orders": False,
            "live_watchlist_file": live_watchlist_file,
            "candidate_watchlist_file": candidate_watchlist_file,
            "universe_rank_report_file": universe_rank_report_file,
            "universe_hygiene_report_file": universe_hygiene_report_file,
            "live_watchlist_hash": stable_hash(live_symbols),
            "candidate_watchlist_hash": stable_hash(candidate_symbols),
            "candidate_source": candidate.get("source") or {},
        },
        "markets": markets,
        "proposal": proposal,
        "recommendations": build_recommendations(markets),
    }
    return payload


def build_text_report(payload):
    lines = [f"Watchlist diff report {payload['generated_at']}"]
    for market, summary in sorted((payload.get("markets") or {}).items()):
        lines.append(
            f"{market}: live={summary['live_count']} candidate={summary['candidate_count']} "
            f"add={summary['add_count']} remove={summary['remove_count']} unchanged={summary['unchanged_count']}"
        )
        if summary.get("add_symbols"):
            lines.append("  add: " + ", ".join(summary["add_symbols"][:20]))
        if summary.get("remove_symbols"):
            lines.append("  remove: " + ", ".join(summary["remove_symbols"][:20]))
        if summary.get("remove_blocker_counts"):
            lines.append("  remove_blockers: " + json.dumps(summary["remove_blocker_counts"], ensure_ascii=False, sort_keys=True))
    lines.append("Recommendations: " + ", ".join(payload.get("recommendations") or []))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-watchlist-file", default=LIVE_WATCHLIST_FILE)
    parser.add_argument("--candidate-watchlist-file", default=CANDIDATE_WATCHLIST_FILE)
    parser.add_argument("--universe-rank-report-file", default=UNIVERSE_RANK_REPORT_FILE)
    parser.add_argument("--universe-hygiene-report-file", default=UNIVERSE_HYGIENE_REPORT_FILE)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    payload = build_report(
        live_watchlist_file=args.live_watchlist_file,
        candidate_watchlist_file=args.candidate_watchlist_file,
        universe_rank_report_file=args.universe_rank_report_file,
        universe_hygiene_report_file=args.universe_hygiene_report_file,
    )
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
    raise SystemExit(main())
