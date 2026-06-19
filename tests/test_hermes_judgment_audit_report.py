import unittest
from datetime import datetime
from pathlib import Path
import tempfile
import json

from scripts import hermes_judgment_audit_report as audit


def context_review(**overrides):
    item = {
        "technical_signal_reviewed": True,
        "portfolio_risk_reviewed": True,
        "strategy_evidence_reviewed": True,
        "data_health_reviewed": True,
        "execution_readiness_reviewed": True,
        "market_context_reviewed": True,
        "intraday_context_reviewed": True,
        "external_market_context_reviewed": True,
        "event_catalysts_reviewed": True,
        "event_catalyst_signals_reviewed": True,
        "market_sentiment_reviewed": True,
        "fundamentals_context_reviewed": True,
        "source_reliability_reviewed": True,
        "simulation_performance_reviewed": True,
        "cron_wiring_reviewed": True,
        "notes": ["all packet context reviewed before supporting trade judgment"],
    }
    item.update(overrides)
    return item


def judgment(signal_id="sig-1", decision="approve", **extra):
    item = {
        "schema": "hermes_trade_judgment_v1",
        "packet_id": "packet-1",
        "signal_id": signal_id,
        "decision": decision,
        "confidence": 0.9,
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "supporting_factors": ["unit test support"],
        "opposing_factors": ["unit test opposition"],
        "risk_notes": ["unit test risk"],
        "context_review": context_review(),
    }
    item.update(extra)
    return item


def review_item(signal_id="sig-1", eligible=True, side="BUY", market="US"):
    return {
        "signal_id": signal_id,
        "eligible_for_approval": eligible,
        "blocking_reasons": [] if eligible else ["no_order_plan"],
        "alert": {
            "signal_id": signal_id,
            "symbol": "AAPL" if market == "US" else "00700",
            "market": market,
            "signal_type": side,
            "trigger": "unit-test",
            "confirmed": True,
        },
    }


def packet(items=None, market_regime="risk_on", outcome_ok=True, cross_market=None, native_index_context=None):
    if outcome_ok:
        strategy = {
            "schema": "rt_signal_outcome_report_v1",
            "overall": {"horizons": {"1d": {"resolved_count": 40, "avg_signed_close_return_pct": 0.2, "win_rate_pct": 55}}},
            "by_trigger": [
                {
                    "key": "BUY:unit-test",
                    "horizons": {"1d": {"resolved_count": 8, "avg_signed_close_return_pct": 0.3, "win_rate_pct": 62}},
                }
            ],
        }
    else:
        strategy = {
            "schema": "rt_signal_outcome_report_v1",
            "overall": {"horizons": {"1d": {"resolved_count": 0, "avg_signed_close_return_pct": None, "win_rate_pct": 0}}},
            "by_trigger": [],
        }
    return {
        "packet_id": "packet-1",
        "generated_at": "2026-06-12T10:00:00",
        "health": {"status": "OK"},
        "market_context": {
            "schema": "market_context_report_v1",
            "markets": {
                "US": {
                    "regime": market_regime,
                    "notes": ["buy_signals_against_weak_breadth"] if market_regime == "risk_off" else [],
                    **({"cross_market": cross_market} if cross_market else {}),
                    **({"native_index_context": native_index_context} if native_index_context else {}),
                }
            },
        },
        "strategy_evidence": strategy,
        "strategy_learning_brief": {
            "schema": "hermes_strategy_learning_brief_v1",
            "read_only": True,
            "submits_orders": False,
            "hermes_alpha_evidence": {
                "schema": "hermes_alpha_evidence_summary_v1",
                "status": "SUPPORTIVE",
                "approved_or_reduced_resolved_count": 8,
                "rejected_or_held_resolved_count": 7,
                "approval_vs_rejection_delta_pct": 1.2,
                "reasons": [],
            },
        },
        "review_items": items if items is not None else [review_item()],
    }


def conflicting_cross_market():
    return {
        "schema": "market_context_cross_market_v1",
        "status": "OK",
        "breadth_regime": "risk_off",
        "sentiment_direction": "risk_on",
        "sentiment_score": 0.68,
        "alignment": "conflicts_with_breadth",
        "notes": ["real_index_or_volatility_sentiment_conflicts_with_breadth_proxy"],
        "indicators": [{"name": "VIX", "direction": "risk_on"}, {"name": "SPY daily return", "direction": "risk_on"}],
    }


def conflicting_native_index():
    return {
        "schema": "market_context_native_index_v1",
        "status": "OK",
        "breadth_regime": "risk_off",
        "index_direction": "risk_on",
        "alignment": "conflicts_with_breadth",
        "primary_index": {
            "symbol": "^GSPC",
            "name": "S&P 500 Index",
            "provider_grade": "public_fallback",
        },
    }


def packet_with_negative_event_catalyst():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["event_catalysts"] = {
        "schema": "event_catalyst_report_v1",
        "status": "RISK",
        "summary": {"candidate_count": 1, "negative_candidate_count": 1},
        "candidates": [
            {
                "id": "event-1",
                "scope": "symbol",
                "sentiment": "negative",
                "impact_score": 0.91,
                "matched_symbols": ["AAPL"],
                "matched_markets": ["US"],
                "title": "Negative unit-test catalyst",
            }
        ],
    }
    return payload


def packet_with_challenge_buy_event_signal():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["event_catalyst_signals"] = {
        "schema": "event_catalyst_signal_report_v1",
        "status": "RISK",
        "summary": {"signal_count": 1, "related_v5_signal_count": 1},
        "signals": [
            {
                "schema": "event_catalyst_signal_v1",
                "signal_id": "event:challenge-1",
                "event_catalyst_id": "event-1",
                "review_signal_type": "CHALLENGE_BUY_REVIEW",
                "direction": "negative_catalyst",
                "priority": "critical",
                "sentiment": "negative",
                "related_v5_signal_ids": ["sig-1"],
                "related_v5_alerts": [{"signal_id": "sig-1", "symbol": "AAPL", "signal_type": "BUY"}],
                "execution_candidate": False,
                "eligible_for_order_intake": False,
            }
        ],
    }
    return payload


def packet_with_support_buy_event_signal():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["event_catalyst_signals"] = {
        "schema": "event_catalyst_signal_report_v1",
        "status": "OK",
        "summary": {"signal_count": 1, "related_v5_signal_count": 1},
        "signals": [
            {
                "schema": "event_catalyst_signal_v1",
                "signal_id": "event:support-1",
                "event_catalyst_id": "event-1",
                "review_signal_type": "SUPPORT_BUY_REVIEW",
                "direction": "positive_catalyst",
                "priority": "high",
                "sentiment": "positive",
                "related_v5_signal_ids": ["sig-1"],
                "related_v5_alerts": [{"signal_id": "sig-1", "symbol": "AAPL", "signal_type": "BUY"}],
                "execution_candidate": False,
                "eligible_for_order_intake": False,
            }
        ],
    }
    return payload


def packet_with_stale_event_catalyst_signal_coverage():
    item = review_item("sig-1", eligible=True, side="BUY", market="US")
    item["context_digest"] = {
        "schema": "hermes_review_item_context_digest_v1",
        "event_catalyst_signals": {"status": "STALE", "relevant_signal_count": 0, "signals": []},
        "required_judgment_attention": [
            "event_catalyst_signal_coverage_limit_requires_acknowledgement",
        ],
    }
    payload = packet(items=[item])
    payload["event_catalyst_signals"] = {
        "schema": "event_catalyst_signal_report_v1",
        "status": "STALE",
        "summary": {"signal_count": 0, "related_v5_signal_count": 0},
        "signals": [],
    }
    return payload


def packet_with_stale_event_catalyst_coverage():
    item = review_item("sig-1", eligible=True, side="BUY", market="US")
    item["context_digest"] = {
        "schema": "hermes_review_item_context_digest_v1",
        "event_catalysts": {"status": "STALE", "relevant_candidate_count": 0, "candidates": []},
        "required_judgment_attention": [
            "event_catalyst_coverage_limit_requires_acknowledgement",
        ],
    }
    payload = packet(items=[item])
    payload["event_catalysts"] = {
        "schema": "event_catalyst_report_v1",
        "status": "STALE",
        "summary": {"candidate_count": 0, "negative_candidate_count": 0},
        "candidates": [],
    }
    return payload


def packet_with_risk_context_coverage_limits():
    item = review_item("sig-1", eligible=True, side="BUY", market="US")
    item["context_digest"] = {
        "schema": "hermes_review_item_context_digest_v1",
        "external_market_context": {"status": "RISK", "relevant_item_count": 0, "items": []},
        "event_catalysts": {"status": "RISK", "relevant_candidate_count": 0, "candidates": []},
        "event_catalyst_signals": {"status": "RISK", "relevant_signal_count": 0, "signals": []},
        "market_sentiment": {"status": "RISK", "relevant_indicator_count": 0, "indicators": []},
        "fundamentals_context": {"status": "RISK", "relevant_item_count": 0, "items": []},
        "required_judgment_attention": [
            "external_market_context_coverage_limit_requires_acknowledgement",
            "event_catalyst_coverage_limit_requires_acknowledgement",
            "event_catalyst_signal_coverage_limit_requires_acknowledgement",
            "market_sentiment_coverage_limit_requires_acknowledgement",
            "fundamentals_context_coverage_limit_requires_acknowledgement",
        ],
    }
    payload = packet(items=[item])
    payload["external_market_context"] = {"schema": "external_market_context_report_v1", "status": "RISK", "items": []}
    payload["event_catalysts"] = {"schema": "event_catalyst_report_v1", "status": "RISK", "candidates": []}
    payload["event_catalyst_signals"] = {
        "schema": "event_catalyst_signal_report_v1",
        "status": "RISK",
        "signals": [],
    }
    payload["market_sentiment"] = {"schema": "market_sentiment_report_v1", "status": "RISK", "indicators": []}
    payload["fundamentals_context"] = {"schema": "fundamentals_context_report_v1", "status": "RISK", "items": []}
    return payload


def packet_with_stale_market_context_coverage():
    item = review_item("sig-1", eligible=True, side="BUY", market="US")
    item["context_digest"] = {
        "schema": "hermes_review_item_context_digest_v1",
        "market_context": {"status": "STALE", "market": "US", "regime": "risk_on"},
        "required_judgment_attention": [
            "market_context_coverage_limit_requires_acknowledgement",
        ],
    }
    payload = packet(items=[item])
    payload["market_context"] = {
        "schema": "market_context_report_v1",
        "status": "STALE",
        "markets": {"US": {"regime": "risk_on"}},
    }
    return payload


def packet_with_negative_external_context():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["external_market_context"] = {
        "schema": "external_market_context_report_v1",
        "status": "RISK",
        "summary": {"fresh_item_count": 1, "negative_high_impact_count": 1},
        "items": [
            {
                "id": "macro-risk-1",
                "category": "macro",
                "source": "official_macro",
                "provider": "official_macro",
                "title": "Unexpected hawkish policy headline",
                "summary": "Policy shock weakens risk appetite for US equities.",
                "published_at": "2026-06-12T10:00:00",
                "markets": ["US"],
                "sentiment": "negative",
                "impact_score": 0.82,
            }
        ],
    }
    return payload


def packet_with_positive_external_context():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["external_market_context"] = {
        "schema": "external_market_context_report_v1",
        "status": "OK",
        "summary": {"fresh_item_count": 1, "positive_high_impact_count": 1},
        "items": [
            {
                "id": "macro-support-1",
                "category": "macro",
                "source": "official_macro",
                "provider": "official_macro",
                "title": "Unexpected ceasefire improves risk appetite",
                "summary": "Ceasefire lowers oil shock risk and supports US equities.",
                "published_at": "2026-06-12T10:00:00",
                "markets": ["US"],
                "sentiment": "positive",
                "impact_score": 0.86,
                "stale": False,
            }
        ],
    }
    return payload


def packet_with_stale_external_market_context_coverage():
    item = review_item("sig-1", eligible=True, side="BUY", market="US")
    item["context_digest"] = {
        "schema": "hermes_review_item_context_digest_v1",
        "external_market_context": {"status": "STALE", "relevant_item_count": 0, "items": []},
        "required_judgment_attention": [
            "external_market_context_coverage_limit_requires_acknowledgement",
        ],
    }
    payload = packet(items=[item])
    payload["external_market_context"] = {
        "schema": "external_market_context_report_v1",
        "status": "STALE",
        "summary": {"fresh_item_count": 0},
        "items": [],
    }
    return payload


def packet_with_risk_off_market_sentiment():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["market_sentiment"] = {
        "schema": "market_sentiment_report_v1",
        "status": "RISK",
        "summary": {"fresh_indicator_count": 1, "risk_off_count": 1, "overall_score": -0.42},
        "indicators": [
            {
                "id": "vix-risk-off",
                "indicator_type": "volatility",
                "name": "VIX",
                "source": "unit_test",
                "observed_at": "2026-06-12T10:00:00",
                "markets": ["US"],
                "direction": "risk_off",
                "score": -0.42,
                "summary": "VIX spike indicates risk-off sentiment.",
            }
        ],
    }
    return payload


def packet_with_risk_on_market_sentiment():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["market_sentiment"] = {
        "schema": "market_sentiment_report_v1",
        "status": "OK",
        "summary": {"fresh_indicator_count": 1, "risk_on_count": 1, "overall_score": 0.35},
        "indicators": [
            {
                "id": "vix-risk-on",
                "indicator_type": "volatility",
                "name": "VIX",
                "source": "unit_test",
                "observed_at": "2026-06-12T10:00:00",
                "markets": ["US"],
                "direction": "risk_on",
                "score": 0.35,
                "stale": False,
                "summary": "VIX eased and supports risk appetite.",
            }
        ],
    }
    return payload


def packet_with_stale_market_sentiment_coverage():
    item = review_item("sig-1", eligible=True, side="BUY", market="US")
    item["context_digest"] = {
        "schema": "hermes_review_item_context_digest_v1",
        "market_sentiment": {"status": "STALE", "relevant_indicator_count": 0, "indicators": []},
        "required_judgment_attention": [
            "market_sentiment_coverage_limit_requires_acknowledgement",
        ],
    }
    payload = packet(items=[item])
    payload["market_sentiment"] = {
        "schema": "market_sentiment_report_v1",
        "status": "STALE",
        "summary": {"fresh_indicator_count": 0},
        "indicators": [],
    }
    return payload


def packet_with_partial_fundamentals():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["fundamentals_context"] = {
        "schema": "fundamentals_context_report_v1",
        "status": "RISK",
        "summary": {
            "item_count": 1,
            "fresh_item_count": 1,
            "partial_item_count": 1,
            "fallback_item_count": 1,
            "by_source": {"tencent_quote_snapshot": 1},
        },
        "items": [
            {
                "symbol": "AAPL",
                "market": "US",
                "source": "tencent_quote_snapshot",
                "provider_symbol": "usAAPL",
                "pe_ttm": 31.4,
                "fundamental_completeness": {
                    "level": "partial",
                    "available_metrics": ["pe_ttm"],
                    "missing_metrics": ["pb", "ps", "roe_pct", "earnings_growth_pct", "debt_to_equity"],
                },
                "valuation_flags": ["partial_fundamentals"],
            }
        ],
    }
    return payload


def packet_with_supportive_fundamentals():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["fundamentals_context"] = {
        "schema": "fundamentals_context_report_v1",
        "status": "OK",
        "summary": {"item_count": 1, "fresh_item_count": 1, "full_item_count": 1, "risky_item_count": 0},
        "items": [
            {
                "symbol": "AAPL",
                "market": "US",
                "source": "broker_fundamentals_snapshot",
                "as_of": "2026-06-12T10:00:00",
                "stale": False,
                "pe_ttm": 24.0,
                "pb": 6.0,
                "ps": 7.5,
                "roe_pct": 28.0,
                "revenue_growth_pct": 9.0,
                "earnings_growth_pct": 12.0,
                "debt_to_equity": 0.5,
                "valuation_flags": [],
                "fundamental_completeness": {
                    "level": "full",
                    "available_metrics": [
                        "pe_ttm",
                        "pb",
                        "ps",
                        "roe_pct",
                        "revenue_growth_pct",
                        "earnings_growth_pct",
                        "debt_to_equity",
                    ],
                    "missing_metrics": [],
                },
            }
        ],
    }
    return payload


def packet_with_stale_fundamentals_context_coverage():
    item = review_item("sig-1", eligible=True, side="BUY", market="US")
    item["context_digest"] = {
        "schema": "hermes_review_item_context_digest_v1",
        "fundamentals_context": {"status": "STALE", "relevant_item_count": 0, "items": []},
        "required_judgment_attention": [
            "fundamentals_context_coverage_limit_requires_acknowledgement",
        ],
    }
    payload = packet(items=[item])
    payload["fundamentals_context"] = {
        "schema": "fundamentals_context_report_v1",
        "status": "STALE",
        "summary": {"fresh_item_count": 0},
        "items": [],
    }
    return payload


def packet_with_degraded_source_reliability():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["source_reliability"] = {
        "schema": "source_reliability_report_v1",
        "status": "DEGRADED",
        "summary": {"component_count": 8, "degraded_or_worse_count": 1},
        "components": [
            {
                "name": "external_market_context",
                "reliability_status": "DEGRADED",
                "reasons": ["external_context_only_public_fallback_sources"],
            }
        ],
        "recommendations": [
            "wire_structured_wudao_infohub_or_broker_context_before_claiming_full_event_awareness"
        ],
    }
    return payload


def packet_with_warn_simulation_performance():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["simulation_performance"] = {
        "schema": "simulation_performance_report_v1",
        "status": "WARN",
        "reason_codes": ["simulation_portfolio_risk_high"],
        "recommendations": ["prioritize_high_risk_position_reviews_before_new_buy_review"],
        "remediation_plan": {
            "schema": "simulation_strategy_remediation_v1",
            "status": "operator_review_required",
            "proposal_hash": "simwarn123456789",
        },
    }
    return payload


def packet_with_weak_hermes_alpha_evidence(status="INSUFFICIENT"):
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["strategy_learning_brief"] = {
        "schema": "hermes_strategy_learning_brief_v1",
        "read_only": True,
        "submits_orders": False,
        "hermes_alpha_evidence": {
            "schema": "hermes_alpha_evidence_summary_v1",
            "status": status,
            "approved_or_reduced_resolved_count": 1,
            "rejected_or_held_resolved_count": 4,
            "approval_vs_rejection_delta_pct": None,
            "reasons": [
                "approved_or_reduced_audit_pass_sample_below_minimum",
                "approval_vs_rejection_delta_missing",
            ],
        },
    }
    return payload


def packet_without_hermes_alpha_evidence():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload.pop("strategy_learning_brief", None)
    return payload


def packet_with_intraday_challenge():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["review_items"][0]["context_digest"] = {
        "schema": "hermes_review_item_context_digest_v1",
        "intraday_context": {
            "schema": "hermes_review_item_intraday_context_digest_v1",
            "status": "OK",
            "session": {"momentum": "strong_down", "change_pct": -1.4},
            "notes": ["intraday_session_down_against_new_buy_review"],
        },
        "intraday_signal_evidence": {
            "schema": "hermes_review_item_intraday_signal_evidence_v1",
            "read_only": True,
            "submits_orders": False,
            "signal_type": "BUY",
            "status": "OK",
            "alignment": "challenges_signal",
            "timeframe_alignment": "mixed_bearish",
            "support_codes": [],
            "challenge_codes": ["session_down_challenges_buy"],
            "conflict_codes": [],
            "quality_codes": [],
            "limit_codes": [],
            "codes": ["session_down_challenges_buy"],
            "requires_judgment_acknowledgement": True,
        },
        "required_judgment_attention": ["intraday_context_challenges_buy_requires_discussion"],
    }
    return payload


def packet_with_intraday_quality_degraded():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["review_items"][0]["context_digest"] = {
        "schema": "hermes_review_item_context_digest_v1",
        "intraday_context": {
            "schema": "hermes_review_item_intraday_context_digest_v1",
            "status": "OK",
            "quality": {
                "schema": "intraday_symbol_quality_v1",
                "status": "WARN",
                "large_gap_count": 1,
                "notes": ["intraday_minute_gap_detected"],
            },
            "notes": ["intraday_context_quality_degraded_requires_disclosure"],
        },
        "required_judgment_attention": ["intraday_context_quality_degraded_requires_disclosure"],
    }
    return payload


def packet_with_intraday_market_closed():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["review_items"][0]["context_digest"] = {
        "schema": "hermes_review_item_context_digest_v1",
        "intraday_context": {
            "schema": "hermes_review_item_intraday_context_digest_v1",
            "status": "CLOSED",
            "market_session": {
                "schema": "intraday_market_session_v1",
                "phase": "AFTER_CLOSE",
                "is_regular_session_open": False,
            },
            "notes": ["intraday_market_not_open_requires_session_context"],
        },
        "required_judgment_attention": ["intraday_market_not_open_requires_session_context"],
    }
    return payload


def packet_with_intraday_market_session_override_warning():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["review_items"][0]["context_digest"] = {
        "schema": "hermes_review_item_context_digest_v1",
        "intraday_context": {
            "schema": "hermes_review_item_intraday_context_digest_v1",
            "status": "OK",
            "market_session": {
                "schema": "intraday_market_session_v1",
                "phase": "REGULAR",
                "is_regular_session_open": True,
            },
            "notes": [],
        },
        "intraday_market_session_overrides": {
            "schema": "hermes_review_item_intraday_market_session_overrides_digest_v1",
            "status": "WARN",
            "report_status": "WARN",
            "market": "US",
            "warnings": ["US:no_future_session_overrides_or_closed_dates"],
            "recommendations": ["review_intraday_market_session_override_coverage_for_holidays_and_half_days"],
        },
        "required_judgment_attention": ["intraday_market_session_overrides_limit_requires_disclosure"],
    }
    return payload


def packet_with_intraday_minute_producer_limit():
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    payload["review_items"][0]["context_digest"] = {
        "schema": "hermes_review_item_context_digest_v1",
        "intraday_context": {
            "schema": "hermes_review_item_intraday_context_digest_v1",
            "status": "OK",
            "notes": [],
        },
        "intraday_minute_producer": {
            "schema": "hermes_review_item_intraday_minute_producer_digest_v1",
            "status": "ACTIONABLE",
            "mode": "dry-run",
            "plan_hash": "intraday-plan",
            "provider_contract": "unofficial_public_web_endpoint_unversioned_best_effort",
            "notes": [
                "intraday_minute_apply_pending",
                "intraday_minute_public_fallback_provider",
                "intraday_minute_producer_dry_run_default",
            ],
        },
        "required_judgment_attention": ["intraday_minute_producer_limit_requires_acknowledgement"],
    }
    return payload


def packet_with_current_session_quote_evidence(use_factor_basis=True):
    payload = packet(items=[review_item("sig-1", eligible=True, side="BUY", market="US")])
    alert = payload["review_items"][0]["alert"]
    alert["current_session_quote_evidence"] = {
        "schema": "current_session_quote_evidence_v1",
        "used_in_full_score": True,
        "used_for_realtime_alignment": True,
        "used_for_trigger_detection": True,
        "basis": "latest_realtime_quote_vs_previous_completed_close",
        "provisional": True,
        "mutates_completed_daily_history": False,
        "replaces_completed_daily_bar": False,
    }
    if use_factor_basis:
        alert["factor_evidence_basis"] = {
            "completed_daily_ohlcv": 2,
            "current_session_quote": 1,
        }
    return payload


class HermesJudgmentAuditReportTests(unittest.TestCase):
    def test_no_judgments_is_not_a_failure(self):
        payload = audit.build_report([], packet())

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["counts"]["judgment_count"], 0)
        self.assertEqual(payload["recommendations"], ["no_hermes_judgments_observed_yet"])

    def test_orphan_judgment_is_flagged(self):
        payload = audit.build_report([judgment("missing")], packet())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("orphan_judgment_not_in_latest_packet", row["reasons"])

    def test_approval_against_ineligible_risk_off_and_unresolved_evidence_is_flagged(self):
        payload = audit.build_report(
            [judgment("sig-1")],
            packet(items=[review_item("sig-1", eligible=False)], market_regime="risk_off", outcome_ok=False),
        )
        row = payload["judgments"][0]

        self.assertEqual(row["status"], "FAIL")
        self.assertIn("approval_for_ineligible_review_item", row["reasons"])
        self.assertIn("US_risk_off_buy_approval_without_exception", row["reasons"])
        self.assertIn("approval_with_overall_outcome_sample_below_30", row["reasons"])
        self.assertIn("approval_with_trigger_outcome_missing", row["reasons"])

    def test_clean_approval_passes_when_packet_gates_are_consistent(self):
        payload = audit.build_report([judgment("sig-1")], packet(market_regime="risk_on", outcome_ok=True))
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])
        self.assertEqual(payload["recommendations"], ["judgment_audit_clean_continue_review_only_observation"])

    def test_approval_requires_structured_context_review(self):
        item = judgment("sig-1")
        item.pop("context_review")

        payload = audit.build_report([item], packet(market_regime="risk_on", outcome_ok=True))
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("context_review_missing", row["reasons"])
        self.assertIn(
            "approve_reduce_judgments_require_structured_context_review",
            payload["recommendations"],
        )

    def test_partial_context_review_flags_missing_fields(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    context_review=context_review(
                        external_market_context_reviewed=False,
                        market_sentiment_reviewed=False,
                        fundamentals_context_reviewed=False,
                    ),
                )
            ],
            packet(market_regime="risk_on", outcome_ok=True),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("context_review_missing_external_market_context_reviewed", row["reasons"])
        self.assertIn("context_review_missing_market_sentiment_reviewed", row["reasons"])
        self.assertIn("context_review_missing_fundamentals_context_reviewed", row["reasons"])

    def test_context_review_requires_event_catalyst_signal_flag(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    context_review=context_review(event_catalyst_signals_reviewed=False),
                )
            ],
            packet(market_regime="risk_on", outcome_ok=True),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("context_review_missing_event_catalyst_signals_reviewed", row["reasons"])

    def test_context_review_requires_intraday_context_flag(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    context_review=context_review(intraday_context_reviewed=False),
                )
            ],
            packet(market_regime="risk_on", outcome_ok=True),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("context_review_missing_intraday_context_reviewed", row["reasons"])

    def test_negative_event_catalyst_buy_approval_requires_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_negative_event_catalyst())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_event_catalyst_risk_acknowledgement", row["reasons"])
        self.assertIn("event_catalyst_ids_missing_or_unmatched", row["reasons"])
        self.assertIn("event_catalyst_risk_notes_missing", row["reasons"])
        self.assertIn(
            "negative_event_catalyst_buy_approvals_require_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_negative_event_catalyst_acknowledged_buy_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    event_catalyst_risk_acknowledged=True,
                    event_catalyst_ids=["event-1"],
                    event_catalyst_risk_notes=["Reviewed event-1 and reduced confidence until follow-up news confirms impact."],
                )
            ],
            packet_with_negative_event_catalyst(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_challenge_buy_event_signal_requires_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_challenge_buy_event_signal())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_event_catalyst_signal_risk_acknowledgement", row["reasons"])
        self.assertIn("event_catalyst_signal_ids_missing_or_unmatched", row["reasons"])
        self.assertIn("event_catalyst_signal_risk_notes_missing", row["reasons"])
        self.assertIn(
            "challenge_buy_event_signals_require_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_challenge_buy_event_signal_acknowledged_buy_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    event_catalyst_risk_acknowledged=True,
                    event_catalyst_signal_ids=["event:challenge-1"],
                    event_catalyst_risk_notes=["Reviewed event:challenge-1 and kept size constrained by the event risk."],
                )
            ],
            packet_with_challenge_buy_event_signal(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_support_buy_event_signal_requires_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_support_buy_event_signal())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_event_catalyst_support_acknowledgement", row["reasons"])
        self.assertIn("event_catalyst_support_signal_ids_missing_or_unmatched", row["reasons"])
        self.assertIn("event_catalyst_support_notes_missing", row["reasons"])
        self.assertIn(
            "support_buy_event_signals_require_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_support_buy_event_signal_acknowledged_buy_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    event_catalyst_support_acknowledged=True,
                    event_catalyst_support_signal_ids=["event:support-1"],
                    event_catalyst_support_notes=[
                        "Reviewed event:support-1 as BUY support; it improves context but does not override gates."
                    ],
                )
            ],
            packet_with_support_buy_event_signal(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_stale_event_catalyst_signal_coverage_requires_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_stale_event_catalyst_signal_coverage())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_event_catalyst_signal_coverage_acknowledgement", row["reasons"])
        self.assertIn("event_catalyst_signal_coverage_notes_missing", row["reasons"])
        self.assertIn(
            "event_catalyst_signal_coverage_limits_require_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_stale_event_catalyst_signal_coverage_acknowledged_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    event_catalyst_signal_coverage_acknowledged=True,
                    event_catalyst_signal_coverage_status="STALE",
                    event_catalyst_signal_coverage_notes=[
                        "Event-catalyst signal report is stale, so absence of review signals was not treated as evidence of no event risk."
                    ],
                )
            ],
            packet_with_stale_event_catalyst_signal_coverage(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_stale_event_catalyst_coverage_requires_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_stale_event_catalyst_coverage())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_event_catalyst_coverage_acknowledgement", row["reasons"])
        self.assertIn("event_catalyst_coverage_notes_missing", row["reasons"])
        self.assertIn(
            "event_catalyst_coverage_limits_require_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_stale_event_catalyst_coverage_acknowledged_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    event_catalyst_coverage_acknowledged=True,
                    event_catalyst_coverage_status="STALE",
                    event_catalyst_coverage_notes=[
                        "Event-catalyst report is stale, so absence of watchlist catalysts was not treated as no event risk."
                    ],
                )
            ],
            packet_with_stale_event_catalyst_coverage(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_risk_coverage_statuses_require_structured_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_risk_context_coverage_limits())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_external_market_context_coverage_acknowledgement", row["reasons"])
        self.assertIn("missing_event_catalyst_coverage_acknowledgement", row["reasons"])
        self.assertIn("missing_event_catalyst_signal_coverage_acknowledgement", row["reasons"])
        self.assertIn("missing_market_sentiment_coverage_acknowledgement", row["reasons"])
        self.assertIn("missing_fundamentals_context_coverage_acknowledgement", row["reasons"])

    def test_risk_coverage_statuses_acknowledged_pass(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    external_market_context_coverage_acknowledged=True,
                    external_market_context_coverage_status="RISK",
                    external_market_context_coverage_notes=[
                        "External context report is RISK, so absence of matched items was not treated as benign."
                    ],
                    event_catalyst_coverage_acknowledged=True,
                    event_catalyst_coverage_status="RISK",
                    event_catalyst_coverage_notes=[
                        "Event catalyst report is RISK, so absence of matched catalysts was not treated as no event risk."
                    ],
                    event_catalyst_signal_coverage_acknowledged=True,
                    event_catalyst_signal_coverage_status="RISK",
                    event_catalyst_signal_coverage_notes=[
                        "Event review-signal report is RISK, so absence of linked review signals was not treated as support."
                    ],
                    market_sentiment_coverage_acknowledged=True,
                    market_sentiment_coverage_status="RISK",
                    market_sentiment_coverage_notes=[
                        "Market sentiment report is RISK, so absence of matched indicators was not treated as normal risk appetite."
                    ],
                    fundamentals_context_coverage_acknowledged=True,
                    fundamentals_context_coverage_status="RISK",
                    fundamentals_context_coverage_notes=[
                        "Fundamentals report is RISK, so absence of symbol rows was not treated as neutral fundamentals."
                    ],
                )
            ],
            packet_with_risk_context_coverage_limits(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_stale_market_context_coverage_requires_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_stale_market_context_coverage())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_market_context_coverage_acknowledgement", row["reasons"])
        self.assertIn("market_context_coverage_notes_missing", row["reasons"])
        self.assertIn(
            "market_context_coverage_limits_require_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_stale_market_context_coverage_acknowledged_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    market_context_coverage_acknowledged=True,
                    market_context_coverage_status="STALE",
                    market_context_coverage_notes=[
                        "Market context report is stale, so absence of risk_off breadth was not treated as benign market evidence."
                    ],
                )
            ],
            packet_with_stale_market_context_coverage(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_negative_external_context_buy_approval_requires_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_negative_external_context())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_external_market_context_risk_acknowledgement", row["reasons"])
        self.assertIn("external_market_context_ids_missing_or_unmatched", row["reasons"])
        self.assertIn("external_market_context_notes_missing", row["reasons"])
        self.assertIn(
            "negative_external_context_buy_approvals_require_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_negative_external_context_acknowledged_buy_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    external_market_context_risk_acknowledged=True,
                    external_market_context_ids=["macro-risk-1"],
                    external_market_context_notes=[
                        "Reviewed macro-risk-1; policy shock caps confidence and prevents any size increase."
                    ],
                )
            ],
            packet_with_negative_external_context(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_positive_external_context_buy_support_requires_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_positive_external_context())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_external_market_context_support_acknowledgement", row["reasons"])
        self.assertIn("external_market_context_support_ids_missing_or_unmatched", row["reasons"])
        self.assertIn("external_market_context_support_notes_missing", row["reasons"])
        self.assertIn(
            "positive_external_context_buy_support_requires_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_positive_external_context_acknowledged_buy_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    external_market_context_support_acknowledged=True,
                    external_market_context_support_ids=["macro-support-1"],
                    external_market_context_support_notes=[
                        "Reviewed macro-support-1; ceasefire improves risk appetite but does not override readiness gates."
                    ],
                )
            ],
            packet_with_positive_external_context(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_stale_external_market_context_coverage_requires_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_stale_external_market_context_coverage())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_external_market_context_coverage_acknowledgement", row["reasons"])
        self.assertIn("external_market_context_coverage_notes_missing", row["reasons"])
        self.assertIn(
            "external_market_context_coverage_limits_require_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_stale_external_market_context_coverage_acknowledged_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    external_market_context_coverage_acknowledged=True,
                    external_market_context_coverage_status="STALE",
                    external_market_context_coverage_notes=[
                        "External context report is stale, so absence of news or macro items was not treated as evidence of no event risk."
                    ],
                )
            ],
            packet_with_stale_external_market_context_coverage(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_risk_off_market_sentiment_buy_approval_requires_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_risk_off_market_sentiment())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_market_sentiment_risk_acknowledgement", row["reasons"])
        self.assertIn("market_sentiment_indicator_ids_missing_or_unmatched", row["reasons"])
        self.assertIn("market_sentiment_notes_missing", row["reasons"])
        self.assertIn(
            "market_sentiment_risk_approvals_require_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_risk_off_market_sentiment_acknowledged_buy_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    market_sentiment_risk_acknowledged=True,
                    market_sentiment_indicator_ids=["vix-risk-off"],
                    market_sentiment_notes=[
                        "VIX risk-off context was reviewed; confidence is capped and size is not increased."
                    ],
                )
            ],
            packet_with_risk_off_market_sentiment(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_risk_on_market_sentiment_buy_support_requires_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_risk_on_market_sentiment())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_market_sentiment_support_acknowledgement", row["reasons"])
        self.assertIn("market_sentiment_support_indicator_ids_missing_or_unmatched", row["reasons"])
        self.assertIn("market_sentiment_support_notes_missing", row["reasons"])
        self.assertIn(
            "positive_market_sentiment_buy_support_requires_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_risk_on_market_sentiment_acknowledged_buy_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    market_sentiment_support_acknowledged=True,
                    market_sentiment_support_indicator_ids=["vix-risk-on"],
                    market_sentiment_support_notes=[
                        "Reviewed vix-risk-on as risk-on support; confidence is not increased beyond readiness gates."
                    ],
                )
            ],
            packet_with_risk_on_market_sentiment(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_stale_market_sentiment_coverage_requires_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_stale_market_sentiment_coverage())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_market_sentiment_coverage_acknowledgement", row["reasons"])
        self.assertIn("market_sentiment_coverage_notes_missing", row["reasons"])
        self.assertIn(
            "market_sentiment_coverage_limits_require_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_stale_market_sentiment_coverage_acknowledged_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    market_sentiment_coverage_acknowledged=True,
                    market_sentiment_coverage_status="STALE",
                    market_sentiment_coverage_notes=[
                        "Market sentiment report is stale, so absence of risk-off indicators was not treated as evidence that risk appetite is normal."
                    ],
                )
            ],
            packet_with_stale_market_sentiment_coverage(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_partial_fundamentals_buy_approval_requires_structured_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_partial_fundamentals())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_fundamentals_context_limit_acknowledgement", row["reasons"])
        self.assertIn("fundamentals_context_symbols_missing_or_unmatched", row["reasons"])
        self.assertIn("fundamentals_context_missing_metrics_not_discussed", row["reasons"])
        self.assertIn("fundamentals_context_notes_missing", row["reasons"])
        self.assertIn(
            "partial_fundamentals_buy_approvals_require_structured_limitation_acknowledgement",
            payload["recommendations"],
        )

    def test_partial_fundamentals_acknowledged_buy_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    fundamentals_context_limit_acknowledged=True,
                    fundamentals_context_symbols=["AAPL"],
                    fundamentals_context_missing_metrics=["pb", "ps", "roe_pct", "earnings_growth_pct"],
                    fundamentals_context_notes=[
                        "AAPL fundamentals are Tencent fallback only; PE is available but PB, PS, ROE and growth were not verified, so confidence is not increased."
                    ],
                )
            ],
            packet_with_partial_fundamentals(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_supportive_fundamentals_buy_support_requires_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_supportive_fundamentals())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_fundamentals_context_support_acknowledgement", row["reasons"])
        self.assertIn("fundamentals_context_support_symbols_missing_or_unmatched", row["reasons"])
        self.assertIn("fundamentals_context_support_metrics_missing_or_unmatched", row["reasons"])
        self.assertIn("fundamentals_context_support_notes_missing", row["reasons"])
        self.assertIn(
            "supportive_fundamentals_buy_approvals_require_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_supportive_fundamentals_acknowledged_buy_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    fundamentals_context_support_acknowledged=True,
                    fundamentals_context_support_symbols=["AAPL"],
                    fundamentals_context_support_metrics=["pe_ttm", "roe_pct", "earnings_growth_pct"],
                    fundamentals_context_support_notes=[
                        "AAPL full fundamentals were reviewed; PE, ROE and earnings growth support the BUY but do not override readiness gates."
                    ],
                )
            ],
            packet_with_supportive_fundamentals(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_stale_fundamentals_context_coverage_requires_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_stale_fundamentals_context_coverage())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_fundamentals_context_coverage_acknowledgement", row["reasons"])
        self.assertIn("fundamentals_context_coverage_notes_missing", row["reasons"])
        self.assertIn(
            "fundamentals_context_coverage_limits_require_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_stale_fundamentals_context_coverage_acknowledged_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    fundamentals_context_coverage_acknowledged=True,
                    fundamentals_context_coverage_status="STALE",
                    fundamentals_context_coverage_notes=[
                        "Fundamentals report is stale, so absence of valuation or profitability items was not treated as neutral."
                    ],
                )
            ],
            packet_with_stale_fundamentals_context_coverage(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_degraded_source_reliability_approval_requires_structured_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_degraded_source_reliability())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_source_reliability_limit_acknowledgement", row["reasons"])
        self.assertIn("source_reliability_components_missing_or_unmatched", row["reasons"])
        self.assertIn("source_reliability_reasons_missing_or_unmatched", row["reasons"])
        self.assertIn("source_reliability_notes_missing", row["reasons"])
        self.assertIn(
            "source_reliability_degraded_approvals_require_structured_limitation_acknowledgement",
            payload["recommendations"],
        )

    def test_degraded_source_reliability_acknowledged_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    source_reliability_limit_acknowledged=True,
                    source_reliability_components=["external_market_context"],
                    source_reliability_reasons=["external_context_only_public_fallback_sources"],
                    source_reliability_notes=[
                        "External context is public fallback only, so the BUY is not upgraded for unverified event awareness."
                    ],
                )
            ],
            packet_with_degraded_source_reliability(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_warn_simulation_performance_approval_requires_structured_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_warn_simulation_performance())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_simulation_performance_acknowledgement", row["reasons"])
        self.assertIn("simulation_performance_status_missing", row["reasons"])
        self.assertIn("simulation_performance_reason_codes_missing_or_unmatched", row["reasons"])
        self.assertIn("simulation_performance_notes_missing", row["reasons"])
        self.assertIn(
            "simulation_performance_warnings_require_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_warn_simulation_performance_acknowledged_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    simulation_performance_acknowledged=True,
                    simulation_performance_status="WARN",
                    simulation_performance_reason_codes=["simulation_portfolio_risk_high"],
                    simulation_performance_notes=[
                        "Simulation portfolio risk is high, so the BUY is reviewed with reduced confidence and no size increase."
                    ],
                )
            ],
            packet_with_warn_simulation_performance(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_weak_hermes_alpha_evidence_approval_requires_structured_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_weak_hermes_alpha_evidence())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_hermes_alpha_evidence_acknowledgement", row["reasons"])
        self.assertIn("hermes_alpha_evidence_status_missing", row["reasons"])
        self.assertIn("hermes_alpha_evidence_reasons_missing_or_unmatched", row["reasons"])
        self.assertIn("hermes_alpha_evidence_notes_missing", row["reasons"])
        self.assertIn(
            "weak_hermes_alpha_evidence_requires_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_missing_hermes_alpha_evidence_approval_requires_structured_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_without_hermes_alpha_evidence())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_hermes_alpha_evidence_acknowledgement", row["reasons"])
        self.assertIn("hermes_alpha_evidence_status_missing", row["reasons"])
        self.assertIn("hermes_alpha_evidence_reasons_missing_or_unmatched", row["reasons"])
        self.assertIn("hermes_alpha_evidence_notes_missing", row["reasons"])

    def test_weak_hermes_alpha_evidence_acknowledged_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    hermes_alpha_evidence_acknowledged=True,
                    hermes_alpha_evidence_status="INSUFFICIENT",
                    hermes_alpha_evidence_reasons=["approved_or_reduced_audit_pass_sample_below_minimum"],
                    hermes_alpha_evidence_notes=[
                        "Hermes approval alpha is not proven; this approval is based on current gates only and does not increase size."
                    ],
                )
            ],
            packet_with_weak_hermes_alpha_evidence(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_intraday_challenge_buy_approval_requires_structured_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_intraday_challenge())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_intraday_context_acknowledgement", row["reasons"])
        self.assertIn("intraday_context_notes_missing", row["reasons"])
        self.assertIn("missing_intraday_signal_evidence_acknowledgement", row["reasons"])
        self.assertIn("intraday_signal_evidence_alignment_missing", row["reasons"])
        self.assertIn("intraday_signal_evidence_codes_missing_or_unmatched", row["reasons"])
        self.assertIn("intraday_signal_evidence_notes_missing", row["reasons"])
        self.assertIn(
            "intraday_context_attention_requires_structured_acknowledgement",
            payload["recommendations"],
        )
        self.assertIn(
            "intraday_signal_evidence_requires_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_intraday_quality_degraded_approval_requires_structured_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_intraday_quality_degraded())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_intraday_context_acknowledgement", row["reasons"])
        self.assertIn("intraday_context_notes_missing", row["reasons"])

    def test_intraday_market_closed_approval_requires_structured_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_intraday_market_closed())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_intraday_context_acknowledgement", row["reasons"])
        self.assertIn("intraday_context_notes_missing", row["reasons"])

    def test_intraday_market_session_override_warning_requires_structured_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_intraday_market_session_override_warning())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_intraday_context_acknowledgement", row["reasons"])
        self.assertIn("intraday_context_notes_missing", row["reasons"])
        self.assertIn(
            "intraday_context_attention_requires_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_intraday_minute_producer_limit_requires_structured_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_intraday_minute_producer_limit())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_intraday_context_acknowledgement", row["reasons"])
        self.assertIn("intraday_context_notes_missing", row["reasons"])
        self.assertIn(
            "intraday_context_attention_requires_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_intraday_challenge_acknowledged_buy_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    intraday_context_acknowledged=True,
                    intraday_context_status="OK",
                    intraday_context_notes=[
                        "AAPL was down intraday against the BUY; confidence is capped and size is not increased."
                    ],
                    intraday_signal_evidence_acknowledged=True,
                    intraday_signal_evidence_alignment="challenges_signal",
                    intraday_signal_evidence_codes=["session_down_challenges_buy"],
                    intraday_signal_evidence_notes=[
                        "Reviewed session_down_challenges_buy as intraday evidence challenging the BUY."
                    ],
                )
            ],
            packet_with_intraday_challenge(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_intraday_minute_producer_limit_acknowledged_buy_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    intraday_context_acknowledged=True,
                    intraday_context_status="OK",
                    intraday_context_notes=[
                        "Minute producer was dry-run ACTIONABLE only, public fallback, and not proof of DB coverage; confidence is not increased."
                    ],
                )
            ],
            packet_with_intraday_minute_producer_limit(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_intraday_market_closed_acknowledged_buy_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    intraday_context_acknowledged=True,
                    intraday_context_status="CLOSED",
                    intraday_context_notes=[
                        "US regular session is closed, so the minute context is last-session evidence only."
                    ],
                )
            ],
            packet_with_intraday_market_closed(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_intraday_market_session_override_warning_acknowledged_buy_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    intraday_context_acknowledged=True,
                    intraday_context_status="OK",
                    intraday_context_notes=[
                        "US calendar overrides are incomplete, so the regular-session minute context is treated as partial and does not raise confidence."
                    ],
                )
            ],
            packet_with_intraday_market_session_override_warning(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_current_session_quote_evidence_approval_requires_structured_acknowledgement(self):
        payload = audit.build_report([judgment("sig-1")], packet_with_current_session_quote_evidence())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_current_session_quote_evidence_acknowledgement", row["reasons"])
        self.assertIn("current_session_quote_evidence_basis_missing", row["reasons"])
        self.assertIn("current_session_quote_evidence_notes_missing", row["reasons"])
        self.assertIn(
            "current_session_quote_evidence_requires_structured_acknowledgement",
            payload["recommendations"],
        )

    def test_current_session_quote_factor_basis_approval_requires_structured_acknowledgement(self):
        payload = packet_with_current_session_quote_evidence()
        payload["review_items"][0]["alert"]["current_session_quote_evidence"]["used_in_full_score"] = False

        report = audit.build_report([judgment("sig-1")], payload)
        row = report["judgments"][0]

        self.assertEqual(report["status"], "FAIL")
        self.assertIn("missing_current_session_quote_evidence_acknowledgement", row["reasons"])

    def test_current_session_quote_evidence_acknowledged_approval_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    current_session_quote_evidence_acknowledged=True,
                    current_session_quote_evidence_basis="latest_realtime_quote_vs_previous_completed_close",
                    current_session_quote_evidence_notes=[
                        "Same-session quote momentum is provisional and may change before close; it does not replace completed daily OHLCV."
                    ],
                )
            ],
            packet_with_current_session_quote_evidence(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_cross_market_conflict_buy_approval_requires_explicit_discussion(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    market_regime_exception=True,
                    market_regime_exception_reason="Company-specific catalyst supports only a reduced probe position after review.",
                )
            ],
            packet(market_regime="risk_off", outcome_ok=True, cross_market=conflicting_cross_market()),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("cross_market_conflict_breadth_not_discussed", row["reasons"])
        self.assertIn("cross_market_conflict_sentiment_not_discussed", row["reasons"])
        self.assertIn(
            "cross_market_conflicts_require_explicit_breadth_and_sentiment_discussion",
            payload["recommendations"],
        )

    def test_cross_market_conflict_discussed_buy_approval_passes_audit_rule(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    market_regime_exception=True,
                    market_regime_exception_reason=(
                        "Stock-pool breadth is risk_off, but VIX and index ETF sentiment are risk_on; "
                        "this supports only a reduced probe after reviewing the cross-market conflict."
                    ),
                    opposing_factors=["Breadth remains weak with many stocks below MA20."],
                    risk_notes=["Cross-market sentiment from VIX/index ETF is risk_on but conflicts with breadth."],
                    context_review=context_review(
                        notes=["Reviewed cross-market conflict between stock-pool breadth and VIX/index sentiment."]
                    ),
                )
            ],
            packet(market_regime="risk_off", outcome_ok=True, cross_market=conflicting_cross_market()),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertNotIn("cross_market_conflict_breadth_not_discussed", row["reasons"])
        self.assertNotIn("cross_market_conflict_sentiment_not_discussed", row["reasons"])

    def test_native_index_conflict_buy_approval_requires_explicit_discussion(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    market_regime_exception=True,
                    market_regime_exception_reason="Company-specific catalyst supports only a reduced probe position after review.",
                )
            ],
            packet(market_regime="risk_off", outcome_ok=True, native_index_context=conflicting_native_index()),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("native_index_conflict_breadth_not_discussed", row["reasons"])
        self.assertIn("native_index_conflict_index_not_discussed", row["reasons"])
        self.assertIn(
            "native_index_conflicts_require_explicit_breadth_and_index_discussion",
            payload["recommendations"],
        )

    def test_native_index_conflict_discussed_buy_approval_passes_audit_rule(self):
        payload = audit.build_report(
            [
                judgment(
                    "sig-1",
                    market_regime_exception=True,
                    market_regime_exception_reason=(
                        "Stock-pool breadth is risk_off, but the native index benchmark is risk_on; "
                        "only a reduced probe is justified after reviewing that conflict."
                    ),
                    opposing_factors=["Breadth remains weak with many stocks below MA20."],
                    risk_notes=["Native index benchmark evidence conflicts with stock-pool breadth."],
                    context_review=context_review(
                        notes=["Reviewed native index benchmark conflict against stock-pool breadth."]
                    ),
                )
            ],
            packet(market_regime="risk_off", outcome_ok=True, native_index_context=conflicting_native_index()),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertNotIn("native_index_conflict_breadth_not_discussed", row["reasons"])
        self.assertNotIn("native_index_conflict_index_not_discussed", row["reasons"])

    def test_packet_id_uses_archived_packet_instead_of_latest_packet(self):
        archived_packet = packet(items=[review_item("archived-sig")], market_regime="risk_on", outcome_ok=True)
        archived_packet["packet_id"] = "archived-packet"
        latest_packet = packet(items=[review_item("other-sig")], market_regime="risk_on", outcome_ok=True)
        latest_packet["packet_id"] = "latest-packet"
        item = judgment("archived-sig", packet_id="archived-packet")

        with tempfile.TemporaryDirectory() as td:
            archive_path = Path(td) / "archived-packet.json"
            archive_path.write_text(json.dumps(archived_packet), encoding="utf-8")

            payload = audit.build_report([item], latest_packet, packet_archive_dir=td)

        row = payload["judgments"][0]
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["packet_source"], "packet_archive")
        self.assertEqual(row["reasons"], [])
        self.assertEqual(row["audit_scope"], "historical_packet")
        self.assertEqual(payload["status"], "OK")

    def test_historical_packet_failures_warn_without_blocking_current_packet_scope(self):
        latest_packet = packet(items=[review_item("current-sig")], market_regime="risk_on", outcome_ok=True)
        latest_packet["packet_id"] = "latest-packet"
        old = judgment(
            "old-sig",
            packet_id="missing-old-packet",
            decision="approve",
            reviewed_at="2026-06-01T10:00:00",
        )

        payload = audit.build_report(
            [old],
            latest_packet,
            now=datetime(2026, 6, 18, 10, 0),
            packet_archive_dir="/tmp/does-not-exist-for-test",
        )
        row = payload["judgments"][0]

        self.assertEqual(row["audit_scope"], "historical_packet")
        self.assertEqual(row["status"], "FAIL")
        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["counts"]["current_status_counts"], {})
        self.assertEqual(payload["counts"]["historical_status_counts"]["FAIL"], 1)

    def test_missing_packet_id_is_flagged(self):
        item = judgment("sig-1")
        item.pop("packet_id")

        payload = audit.build_report([item], packet(market_regime="risk_on", outcome_ok=True))

        row = payload["judgments"][0]
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("judgment_missing_packet_id", row["reasons"])


if __name__ == "__main__":
    unittest.main()
