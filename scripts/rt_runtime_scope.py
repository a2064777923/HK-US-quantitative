#!/usr/bin/env python3
"""Runtime v5 strategy/watchlist scope shared by read-only reports."""

import os

try:
    import rt_signal_engine_v5 as v5
except ImportError:  # pragma: no cover - package import path in tests
    from scripts import rt_signal_engine_v5 as v5


SCOPE_MODES = {
    "latest_strategy_config_and_watchlist",
    "runtime_strategy_config_and_watchlist",
}


def filters_strategy_watchlist(scope):
    return (scope or {}).get("mode") in SCOPE_MODES


def current_runtime_sample_scope(env=None):
    env = env if env is not None else os.environ
    try:
        _config, strategy_context = v5.load_strategy_config(env=env)
        _hk, _us, watchlist_context = v5.load_watchlists(env=env)
    except Exception as exc:
        return {
            "mode": "runtime_scope_unavailable",
            "strategy_config_id": None,
            "watchlist_id": None,
            "latest_signal_id": None,
            "warnings": [f"runtime_scope_load_failed:{exc}"],
        }

    strategy_source = strategy_context.get("source")
    strategy_config_id = strategy_context.get("strategy_config_id")
    watchlist_id = watchlist_context.get("watchlist_id")
    market_sources = [
        (info or {}).get("source")
        for info in (watchlist_context.get("markets") or {}).values()
        if isinstance(info, dict)
    ]
    has_runtime_watchlist = any(source in {"file", "env"} for source in market_sources)
    if not strategy_config_id or strategy_source == "fallback_default" or not watchlist_id or not has_runtime_watchlist:
        return {
            "mode": "runtime_scope_unavailable",
            "strategy_config_id": None,
            "watchlist_id": None,
            "latest_signal_id": None,
            "warnings": [
                "runtime_strategy_or_watchlist_not_authoritative",
                f"strategy_source:{strategy_source or 'missing'}",
                "watchlist_sources:" + ",".join(sorted({str(source or 'missing') for source in market_sources})),
            ],
        }

    return {
        "mode": "runtime_strategy_config_and_watchlist",
        "strategy_config_id": str(strategy_config_id),
        "strategy_config_version": strategy_context.get("version"),
        "watchlist_id": str(watchlist_id),
        "latest_signal_id": None,
        "scope_source": "runtime_v5_config_and_watchlist",
        "strategy_config_source": strategy_source,
        "strategy_config_file": strategy_context.get("source_file"),
        "watchlist_file": watchlist_context.get("source_file"),
        "warnings": (strategy_context.get("warnings") or []) + (watchlist_context.get("warnings") or []),
    }
