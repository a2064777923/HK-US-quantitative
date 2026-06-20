import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from scripts import hermes_position_judgment_audit_report as audit


def context_review(**overrides):
    item = {
        "position_context_reviewed": True,
        "portfolio_risk_reviewed": True,
        "market_context_reviewed": True,
        "external_context_reviewed": True,
        "intraday_context_reviewed": True,
        "notes": ["position context digest reviewed before advisory judgment"],
    }
    item.update(overrides)
    return item


def position_item(review_id="simulation:8:00929:2026-06-12:reduce_or_exit_review", **extra):
    item = {
        "review_id": review_id,
        "portfolio_id": 8,
        "role": "simulation",
        "symbol": "00929",
        "urgency": "medium",
        "recommended_action": "risk_review",
        "execution_policy": {
            "advice_only": False,
            "review_only": True,
            "submits_orders": False,
            "requires_separate_order_path": True,
        },
    }
    item.update(extra)
    return item


def position_item_with_context(review_id="simulation:8:00929:2026-06-12:reduce_or_exit_review", **extra):
    item = position_item(review_id, **extra)
    item["context_digest"] = {
        "schema": "hermes_position_review_context_digest_v1",
        "read_only": True,
        "advisory_only": True,
        "submits_orders": False,
        "symbol": item.get("symbol"),
        "position_attention": [
            "position_negative_external_context_requires_discussion",
            "position_source_reliability_limit_requires_discussion",
        ],
    }
    return item


def packet(items=None, packet_id="packet-1"):
    return {
        "schema": "hermes_signal_review_packet_v1",
        "packet_id": packet_id,
        "generated_at": "2026-06-12T10:00:00",
        "position_review": {
            "schema": "portfolio_position_review_v1",
            "review_only": True,
            "submits_orders": False,
            "items": items if items is not None else [position_item()],
        },
    }


def packet_with_worklist(items=None, packet_id="packet-1", worklist_items=None):
    payload = packet(items=items, packet_id=packet_id)
    payload["position_judgment_worklist"] = {
        "schema": "hermes_position_judgment_worklist_v1",
        "advisory_only": True,
        "submits_orders": False,
        "judgment_file": "/tmp/hermes_position_judgments.jsonl",
        "items": worklist_items or [],
    }
    return payload


def judgment(review_id="simulation:8:00929:2026-06-12:reduce_or_exit_review", decision="watch", **extra):
    item = {
        "schema": "hermes_position_judgment_v1",
        "packet_id": "packet-1",
        "review_id": review_id,
        "portfolio_id": 8,
        "role": "simulation",
        "symbol": "00929",
        "decision": decision,
        "confidence": 0.82,
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "advisory_only": True,
        "submits_orders": False,
        "supporting_factors": ["unit test support"],
        "opposing_factors": ["unit test opposition"],
        "risk_notes": ["unit test risk"],
    }
    item.update(extra)
    item.setdefault(
        "review_thread_key",
        f"{item.get('role')}:{item.get('portfolio_id')}:{str(item.get('symbol')).upper()}",
    )
    item.setdefault("reviewed_recommended_action", audit.reviewed_action_from_id(item.get("review_id")))
    item.setdefault("reviewed_urgency", "medium")
    return item


def position_attention_acknowledgement(*codes):
    selected_codes = list(codes) or [
        "position_negative_external_context_requires_discussion",
        "position_source_reliability_limit_requires_discussion",
    ]
    return {
        "position_attention_acknowledged": True,
        "position_attention_codes": selected_codes,
        "position_attention_notes": ["position attention items were reflected in advisory risk notes"],
        "position_attention_effects": [
            {
                "code": code,
                "effect": f"{code} was reviewed as holding-specific risk context",
                "decision_impact": "kept the advice conservative and prevented adding exposure",
            }
            for code in selected_codes
        ],
    }


class HermesPositionJudgmentAuditReportTests(unittest.TestCase):
    def test_no_position_judgments_is_not_a_failure(self):
        payload = audit.build_report([], packet())

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["counts"]["judgment_count"], 0)
        self.assertEqual(payload["coverage"]["unjudged_high_urgency_review_count"], 0)
        self.assertEqual(payload["recommendations"], ["no_position_judgments_observed_yet"])

    def test_unjudged_high_urgency_position_review_warns_for_coverage_gap(self):
        high = position_item(
            "simulation:8:00929:2026-06-12:reduce_or_exit_review",
            urgency="high",
            recommended_action="reduce_or_exit_review",
        )

        payload = audit.build_report([], packet([high]))

        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["coverage"]["high_urgency_review_count"], 1)
        self.assertEqual(payload["coverage"]["unjudged_high_urgency_review_count"], 1)
        self.assertEqual(
            payload["coverage"]["unjudged_high_urgency_examples"][0]["review_id"],
            "simulation:8:00929:2026-06-12:reduce_or_exit_review",
        )
        self.assertIn("write_position_judgments_for_high_urgency_reviews:1", payload["recommendations"])

    def test_unjudged_high_urgency_coverage_includes_matching_worklist_item(self):
        review_id = "simulation:8:00929:2026-06-12:reduce_or_exit_review"
        high = position_item(
            review_id,
            urgency="high",
            recommended_action="reduce_or_exit_review",
        )
        work_item = {
            "review_id": review_id,
            "review_thread_key": "simulation:8:00929",
            "portfolio_id": 8,
            "role": "simulation",
            "symbol": "00929",
            "urgency": "high",
            "recommended_action": "reduce_or_exit_review",
            "context_summary": {
                "intraday_live_context": {
                    "status": "OK",
                    "latest_price": 3.2,
                    "session": {"change_pct": -2.1, "momentum": "strong_down"},
                    "policy": "current_session_or_last_session_context_only_not_completed_daily_ohlcv",
                }
            },
            "required_output_fields": {
                "schema": "hermes_position_judgment_v1",
                "advisory_only": True,
                "submits_orders": False,
            },
        }

        payload = audit.build_report([], packet_with_worklist([high], worklist_items=[work_item]))

        work_items = payload["coverage"]["unjudged_high_urgency_work_items"]
        self.assertEqual(len(work_items), 1)
        self.assertEqual(work_items[0]["review_id"], review_id)
        self.assertEqual(work_items[0]["context_summary"]["intraday_live_context"]["latest_price"], 3.2)
        self.assertEqual(
            work_items[0]["context_summary"]["intraday_live_context"]["policy"],
            "current_session_or_last_session_context_only_not_completed_daily_ohlcv",
        )
        self.assertEqual(work_items[0]["required_output_fields"]["schema"], "hermes_position_judgment_v1")
        self.assertTrue(work_items[0]["required_output_fields"]["advisory_only"])
        self.assertFalse(work_items[0]["required_output_fields"]["submits_orders"])

    def test_judged_high_urgency_position_review_clears_coverage_gap(self):
        high = position_item(
            "simulation:8:00929:2026-06-12:reduce_or_exit_review",
            urgency="high",
            recommended_action="reduce_or_exit_review",
        )
        item = judgment(
            "simulation:8:00929:2026-06-12:reduce_or_exit_review",
            decision="watch",
            opposing_factors=["support held above stop", "liquidity risk makes immediate exit worse"],
            risk_notes=["review again next session", "do not add exposure before review"],
        )

        payload = audit.build_report([item], packet([high]))

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["coverage"]["unjudged_high_urgency_review_count"], 0)

    def test_latest_review_id_match_does_not_require_archived_packet(self):
        high = position_item(
            "simulation:8:00929:2026-06-18:reduce_or_exit_review",
            urgency="high",
            recommended_action="reduce_or_exit_review",
        )
        item = judgment(
            "simulation:8:00929:2026-06-18:reduce_or_exit_review",
            packet_id="older-packet-id",
            decision="watch",
            reviewed_at="2026-06-18T09:55:00",
            opposing_factors=["support held above stop", "liquidity risk makes immediate exit worse"],
            risk_notes=["review again next session", "do not add exposure before review"],
        )

        payload = audit.build_report(
            [item],
            packet([high], packet_id="latest-packet"),
            now=datetime(2026, 6, 18, 10, 0),
            packet_archive_dir="/tmp/does-not-exist-for-test",
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["packet_source"], "latest_packet_review_id")
        self.assertEqual(row["match_type"], "latest_review_id")
        self.assertNotIn("packet_archive_missing_for_packet_id", row["reasons"])
        self.assertEqual(payload["coverage"]["unjudged_high_urgency_review_count"], 0)

    def test_unexpired_thread_judgment_can_cover_refreshed_same_position_review(self):
        current = position_item(
            "simulation:8:00929:2026-06-18:reduce_or_exit_review",
            urgency="high",
            recommended_action="reduce_or_exit_review",
        )
        prior = judgment(
            "simulation:8:00929:2026-06-17:reduce_or_exit_review",
            packet_id="old-packet",
            reviewed_at="2026-06-18T09:30:00",
            reviewed_urgency="high",
            decision="watch",
            opposing_factors=["support held above stop", "liquidity risk makes immediate exit worse"],
            risk_notes=["review again next session", "do not add exposure before review"],
        )

        payload = audit.build_report(
            [prior],
            packet([current], packet_id="latest-packet"),
            now=datetime(2026, 6, 18, 10, 0),
            packet_archive_dir="/tmp/does-not-exist-for-test",
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["audit_scope"], "current_packet")
        self.assertEqual(row["packet_source"], "latest_packet_thread_key")
        self.assertEqual(row["match_type"], "latest_review_thread_key")
        self.assertEqual(row["covered_review_id"], "simulation:8:00929:2026-06-18:reduce_or_exit_review")
        self.assertEqual(payload["coverage"]["unjudged_high_urgency_review_count"], 0)

    def test_thread_judgment_does_not_cover_escalated_current_urgency(self):
        current = position_item(
            "simulation:8:00929:2026-06-18:reduce_or_exit_review",
            urgency="high",
            recommended_action="reduce_or_exit_review",
        )
        prior = judgment(
            "simulation:8:00929:2026-06-17:reduce_or_exit_review",
            packet_id="old-packet",
            reviewed_at="2026-06-18T09:30:00",
            reviewed_recommended_action="reduce_or_exit_review",
            reviewed_urgency="medium",
            decision="watch",
            opposing_factors=["support held above stop", "liquidity risk makes immediate exit worse"],
            risk_notes=["review again next session", "do not add exposure before review"],
        )

        payload = audit.build_report(
            [prior],
            packet([current], packet_id="latest-packet"),
            now=datetime(2026, 6, 18, 10, 0),
            packet_archive_dir="/tmp/does-not-exist-for-test",
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("thread_match_current_urgency_escalated", row["reasons"])
        self.assertEqual(row["audit_scope"], "historical_packet")
        self.assertEqual(payload["coverage"]["unjudged_high_urgency_review_count"], 1)

    def test_thread_judgment_missing_reviewed_urgency_does_not_cover_current_urgency(self):
        current = position_item(
            "simulation:8:00929:2026-06-18:reduce_or_exit_review",
            urgency="high",
            recommended_action="reduce_or_exit_review",
        )
        prior = judgment(
            "simulation:8:00929:2026-06-17:reduce_or_exit_review",
            packet_id="old-packet",
            reviewed_at="2026-06-18T09:30:00",
            reviewed_recommended_action="reduce_or_exit_review",
            decision="watch",
            opposing_factors=["support held above stop", "liquidity risk makes immediate exit worse"],
            risk_notes=["review again next session", "do not add exposure before review"],
        )
        prior.pop("reviewed_urgency")

        payload = audit.build_report(
            [prior],
            packet([current], packet_id="latest-packet"),
            now=datetime(2026, 6, 18, 10, 0),
            packet_archive_dir="/tmp/does-not-exist-for-test",
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("thread_match_missing_reviewed_urgency", row["reasons"])
        self.assertEqual(row["audit_scope"], "historical_packet")
        self.assertEqual(payload["coverage"]["unjudged_high_urgency_review_count"], 1)

    def test_thread_judgment_does_not_cover_escalated_current_action(self):
        current = position_item(
            "simulation:8:00929:2026-06-18:exit_review",
            urgency="high",
            recommended_action="exit_review",
        )
        prior = judgment(
            "simulation:8:00929:2026-06-17:reduce_or_exit_review",
            packet_id="old-packet",
            reviewed_at="2026-06-18T09:30:00",
            reviewed_recommended_action="reduce_or_exit_review",
            decision="watch",
            opposing_factors=["support held above stop", "liquidity risk makes immediate exit worse"],
            risk_notes=["review again next session", "do not add exposure before review"],
        )

        payload = audit.build_report(
            [prior],
            packet([current], packet_id="latest-packet"),
            now=datetime(2026, 6, 18, 10, 0),
            packet_archive_dir="/tmp/does-not-exist-for-test",
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("thread_match_current_action_escalated", row["reasons"])
        self.assertEqual(row["audit_scope"], "historical_packet")
        self.assertEqual(payload["coverage"]["unjudged_high_urgency_review_count"], 1)

    def test_historical_or_failed_judgments_do_not_cover_current_high_urgency_review(self):
        high = position_item(
            "simulation:8:00929:2026-06-18:reduce_or_exit_review",
            urgency="high",
            recommended_action="reduce_or_exit_review",
        )
        historical = judgment(
            "simulation:8:00929:2026-06-12:reduce_or_exit_review",
            packet_id="old-packet",
            decision="watch",
            reviewed_at="2026-06-12T10:00:00",
        )
        failed_current = judgment(
            "simulation:8:00929:2026-06-18:reduce_or_exit_review",
            packet_id="latest-packet",
            decision="watch",
            opposing_factors=["too thin"],
            risk_notes=["too thin"],
        )

        payload = audit.build_report(
            [historical, failed_current],
            packet([high], packet_id="latest-packet"),
            now=datetime(2026, 6, 18, 10, 0),
            packet_archive_dir="/tmp/does-not-exist-for-test",
        )

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["coverage"]["judged_review_count"], 0)
        self.assertEqual(payload["coverage"]["failed_current_judgment_review_count"], 1)
        self.assertEqual(payload["coverage"]["unjudged_high_urgency_review_count"], 1)
        self.assertEqual(
            payload["coverage"]["unjudged_high_urgency_examples"][0]["review_id"],
            "simulation:8:00929:2026-06-18:reduce_or_exit_review",
        )

    def test_clean_position_judgment_passes_against_packet_item(self):
        payload = audit.build_report([judgment()], packet())
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])
        self.assertEqual(payload["recommendations"], ["position_judgment_audit_clean_continue_advisory_review"])

    def test_enriched_position_review_requires_context_review(self):
        payload = audit.build_report([judgment()], packet([position_item_with_context()]))
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("context_review_missing", row["reasons"])
        self.assertIn("position_judgments_require_context_review_for_enriched_items", payload["recommendations"])

    def test_enriched_position_review_context_review_passes(self):
        payload = audit.build_report(
            [
                judgment(
                    context_review=context_review(),
                    **position_attention_acknowledgement(),
                )
            ],
            packet([position_item_with_context()]),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_position_attention_requires_structured_acknowledgement(self):
        payload = audit.build_report(
            [judgment(context_review=context_review())],
            packet([position_item_with_context()]),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("missing_position_attention_acknowledgement", row["reasons"])
        self.assertIn("position_attention_codes_missing_or_unmatched", row["reasons"])
        self.assertIn("position_attention_notes_missing", row["reasons"])
        self.assertIn("position_attention_effects_missing", row["reasons"])
        self.assertIn("position_attention_requires_structured_acknowledgement", payload["recommendations"])

    def test_position_attention_acknowledgement_must_cover_all_codes(self):
        payload = audit.build_report(
            [
                judgment(
                    context_review=context_review(),
                    **position_attention_acknowledgement(
                        "position_negative_external_context_requires_discussion"
                    ),
                )
            ],
            packet([position_item_with_context()]),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("position_attention_codes_missing_or_unmatched", row["reasons"])
        self.assertNotIn("position_attention_effects_missing_or_unmatched", row["reasons"])

    def test_position_attention_effects_must_explain_highlighted_codes(self):
        ack = position_attention_acknowledgement()
        ack["position_attention_effects"] = [
            {
                "code": "position_negative_external_context_requires_discussion",
                "effect": "",
                "decision_impact": "",
            },
            {
                "code": "position_source_reliability_limit_requires_discussion",
                "effect": "source reliability was degraded",
                "decision_impact": "kept advice as watch instead of add",
            },
        ]
        payload = audit.build_report(
            [
                judgment(
                    context_review=context_review(),
                    **ack,
                )
            ],
            packet([position_item_with_context()]),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("position_attention_effect_detail_missing", row["reasons"])
        self.assertIn("position_attention_effect_decision_impact_missing", row["reasons"])

    def test_position_attention_effects_do_not_need_one_row_per_attention_code(self):
        item = position_item_with_context()
        item["context_digest"]["position_attention"] = [
            "high_urgency_position_requires_contextual_rationale",
            "position_dynamic_management_requires_review",
            "position_intraday_evidence_requires_discussion",
            "position_negative_external_context_requires_discussion",
            "position_source_reliability_limit_requires_discussion",
        ]
        attention_codes = item["context_digest"]["position_attention"]
        ack = position_attention_acknowledgement(*attention_codes)
        ack["position_attention_effects"] = [
            {
                "code": "high_urgency_position_requires_contextual_rationale",
                "effect": "high urgency requires immediate advisory review",
                "decision_impact": "kept advice conservative",
            },
            {
                "code": "position_dynamic_management_requires_review",
                "effect": "dynamic stop and target distances were reviewed",
                "decision_impact": "prevented adding exposure",
            },
            {
                "code": "position_intraday_evidence_requires_discussion",
                "effect": "intraday evidence was discussed as timing context",
                "decision_impact": "adjusted urgency without submitting orders",
            },
        ]
        payload = audit.build_report(
            [
                judgment(
                    context_review=context_review(),
                    **ack,
                )
            ],
            packet([item]),
        )

        row = payload["judgments"][0]
        self.assertEqual(payload["status"], "OK")
        self.assertNotIn("position_attention_effects_missing_or_unmatched", row["reasons"])

    def test_position_attention_notes_accepts_non_empty_string_for_legacy_judgments(self):
        ack = position_attention_acknowledgement()
        ack["position_attention_notes"] = "position attention items were reviewed"

        payload = audit.build_report(
            [
                judgment(
                    context_review=context_review(),
                    **ack,
                )
            ],
            packet([position_item_with_context()]),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(row["status"], "PASS")
        self.assertNotIn("position_attention_notes_missing", row["reasons"])

    def test_enriched_position_review_partial_context_review_flags_missing_fields(self):
        payload = audit.build_report(
            [
                judgment(
                    context_review=context_review(external_context_reviewed=False),
                    **position_attention_acknowledgement(),
                )
            ],
            packet([position_item_with_context()]),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("context_review_missing_external_context_reviewed", row["reasons"])
        self.assertIn("position_judgments_require_context_review_for_enriched_items", payload["recommendations"])

    def test_orphan_review_id_is_flagged(self):
        payload = audit.build_report(
            [
                judgment(
                    "missing-review",
                    review_thread_key="simulation:8:MISSING",
                    symbol="MISSING",
                )
            ],
            packet(),
        )
        row = payload["judgments"][0]

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("orphan_position_judgment_not_in_packet", row["reasons"])

    def test_missing_advisory_flags_are_flagged(self):
        item = judgment(advisory_only=False, submits_orders=True)

        payload = audit.build_report([item], packet())
        row = payload["judgments"][0]

        self.assertEqual(row["status"], "FAIL")
        self.assertIn("advisory_only_must_be_true", row["reasons"])
        self.assertIn("submits_orders_must_be_false", row["reasons"])

    def test_user_action_decision_requires_manual_only_acknowledgement(self):
        review_id = "user:7:AAPL:2026-06-12:risk_review"
        packet_payload = packet(
            [
                position_item(
                    review_id,
                    portfolio_id=7,
                    role="user",
                    symbol="AAPL",
                    execution_policy={
                        "advice_only": True,
                        "review_only": True,
                        "submits_orders": False,
                        "requires_separate_order_path": True,
                    },
                )
            ]
        )
        item = judgment(review_id, decision="reduce", portfolio_id=7, role="user", symbol="AAPL")

        payload = audit.build_report([item], packet_payload)
        row = payload["judgments"][0]

        self.assertEqual(row["status"], "FAIL")
        self.assertIn("user_action_advice_requires_manual_only_acknowledgement", row["reasons"])
        self.assertIn(
            "user_position_action_advice_requires_manual_only_acknowledgement",
            payload["recommendations"],
        )

    def test_user_action_decision_with_manual_only_acknowledgement_passes(self):
        review_id = "user:7:AAPL:2026-06-12:risk_review"
        packet_payload = packet(
            [
                position_item(
                    review_id,
                    portfolio_id=7,
                    role="user",
                    symbol="AAPL",
                    urgency="high",
                    recommended_action="reduce_or_exit_review",
                    execution_policy={
                        "advice_only": True,
                        "review_only": True,
                        "submits_orders": False,
                        "requires_separate_order_path": True,
                    },
                )
            ]
        )
        item = judgment(
            review_id,
            decision="reduce",
            portfolio_id=7,
            role="user",
            symbol="AAPL",
            manual_only=True,
            max_exit_quantity=3,
        )

        payload = audit.build_report([item], packet_payload)
        row = payload["judgments"][0]

        self.assertEqual(row["status"], "PASS")
        self.assertNotIn("user_action_advice_requires_manual_only_acknowledgement", row["reasons"])

    def test_high_urgency_hold_watch_requires_stronger_rationale(self):
        review_id = "simulation:8:00929:2026-06-12:reduce_or_exit_review"
        packet_payload = packet(
            [
                position_item(
                    review_id,
                    urgency="high",
                    recommended_action="reduce_or_exit_review",
                )
            ]
        )

        payload = audit.build_report([judgment(review_id, decision="hold")], packet_payload)
        row = payload["judgments"][0]

        self.assertEqual(row["status"], "FAIL")
        self.assertIn("high_urgency_hold_or_watch_requires_strong_rationale", row["reasons"])
        self.assertIn("high_urgency_hold_missing_opposing_detail", row["reasons"])

    def test_high_urgency_hold_watch_can_pass_with_explicit_rationale(self):
        review_id = "simulation:8:00929:2026-06-12:reduce_or_exit_review"
        packet_payload = packet(
            [
                position_item(
                    review_id,
                    urgency="high",
                    recommended_action="reduce_or_exit_review",
                )
            ]
        )
        item = judgment(
            review_id,
            decision="watch",
            opposing_factors=["support held above stop", "liquidity risk makes immediate exit worse"],
            risk_notes=["review again next session", "do not add exposure before review"],
        )

        payload = audit.build_report([item], packet_payload)
        row = payload["judgments"][0]

        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["reasons"], [])

    def test_packet_id_uses_archived_packet_instead_of_latest_packet(self):
        archived = packet([position_item("simulation:8:00177:2026-06-12:risk_review", symbol="00177")], "archived-packet")
        latest = packet([position_item("simulation:8:00929:2026-06-12:risk_review")], "latest-packet")
        item = judgment(
            "simulation:8:00177:2026-06-12:risk_review",
            packet_id="archived-packet",
            symbol="00177",
        )

        with tempfile.TemporaryDirectory() as td:
            archive_path = Path(td) / "archived-packet.json"
            archive_path.write_text(json.dumps(archived), encoding="utf-8")

            payload = audit.build_report([item], latest, packet_archive_dir=td)

        row = payload["judgments"][0]
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["packet_source"], "packet_archive")
        self.assertEqual(row["reasons"], [])
        self.assertEqual(row["audit_scope"], "historical_packet")
        self.assertEqual(payload["status"], "OK")

    def test_historical_packet_failures_warn_without_blocking_current_packet_scope(self):
        latest = packet([position_item("simulation:8:00929:2026-06-12:risk_review")], "latest-packet")
        item = judgment(
            "simulation:8:00177:2026-06-12:risk_review",
            packet_id="missing-old-packet",
            decision="hold",
            reviewed_at="2026-06-01T10:00:00",
        )

        payload = audit.build_report(
            [item],
            latest,
            now=datetime(2026, 6, 18, 10, 0),
            packet_archive_dir="/tmp/does-not-exist-for-test",
        )
        row = payload["judgments"][0]

        self.assertEqual(row["audit_scope"], "historical_packet")
        self.assertEqual(row["status"], "FAIL")
        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["counts"]["current_status_counts"], {})
        self.assertEqual(payload["counts"]["historical_status_counts"]["FAIL"], 1)
        self.assertEqual(
            payload["recommendations"],
            ["no_current_packet_position_judgments_observed"],
        )
        self.assertIn(
            "retain_packet_archive_for_position_judgment_audit",
            payload["historical_recommendations"],
        )
        self.assertEqual(payload["counts"]["current_reason_counts"], {})
        self.assertEqual(payload["counts"]["historical_reason_counts"]["judgment_expired"], 1)

    def test_duplicate_review_judgments_are_flagged(self):
        items = [judgment(), judgment(decision="hold")]

        payload = audit.build_report(items, packet())

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["counts"]["status_counts"]["FAIL"], 2)
        self.assertIn("duplicate_position_judgments_for_review", payload["counts"]["reason_counts"])


if __name__ == "__main__":
    unittest.main()
