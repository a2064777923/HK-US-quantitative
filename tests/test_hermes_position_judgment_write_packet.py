import unittest

from scripts import hermes_position_judgment_write_packet as packet


def work_item(review_id, role="user", symbol="AAPL", urgency="high"):
    return {
        "review_id": review_id,
        "review_thread_key": ":".join(str(review_id).split(":")[:3]),
        "portfolio_id": 3 if role == "user" else 8,
        "role": role,
        "symbol": symbol,
        "market": "US",
        "urgency": urgency,
        "recommended_action": "reduce_or_exit_review",
        "allowed_decisions": ["hold", "watch", "reduce", "exit", "trail_stop"],
        "required_attention_codes": [
            "high_urgency_position_requires_contextual_rationale",
            "position_dynamic_management_requires_review",
            "position_intraday_evidence_requires_discussion",
        ],
        "context_summary": {
            "position": {
                "quantity": 10,
                "current_price": 180,
                "unrealized_pnl_pct": -12.5,
                "latest_daily_change_pct": -4.2,
                "stop_distance_pct": -2.1,
                "price_snapshot_age_hours": 0.4,
            },
            "latest_signal": {
                "side": "SELL",
                "score": -0.6,
                "trade_date": "2026-06-18",
                "risk_flags": ["跌破MA5"],
            },
            "dynamic_management": {
                "target_status": "below_signal_stop",
                "review_focus": ["review_reduce_or_exit_before_adding_exposure"],
                "distance_to_signal_take_profit_pct": -1.0,
                "distance_above_signal_stop_loss_pct": -2.1,
                "price_snapshot_fresh": True,
            },
            "intraday_position_evidence": {
                "alignment": "supports_recommended_action",
                "action_intent": "risk_reduction",
                "status": "OK",
                "session_momentum": "strong_down",
                "session_change_pct": -3.1,
                "support_codes": ["session_down_supports_reduce_exit"],
                "challenge_codes": [],
                "limit_codes": [],
            },
        },
        "required_output_fields": {
            "schema": "hermes_position_judgment_v1",
            "packet_id": "packet-1",
            "review_id": review_id,
            "review_thread_key": ":".join(str(review_id).split(":")[:3]),
            "reviewed_recommended_action": "reduce_or_exit_review",
            "portfolio_id": 3 if role == "user" else 8,
            "role": role,
            "symbol": symbol,
            "reviewer": "hermes",
            "advisory_only": True,
            "submits_orders": False,
            "manual_only": "<true when decision is reduce, exit, or trail_stop>",
            "context_review": {
                "position_context_reviewed": True,
                "portfolio_risk_reviewed": True,
                "market_context_reviewed": True,
                "external_context_reviewed": True,
                "intraday_context_reviewed": True,
                "notes": ["<Hermes summary>"],
            },
            "position_attention_acknowledged": True,
            "position_attention_codes": [
                "high_urgency_position_requires_contextual_rationale",
                "position_dynamic_management_requires_review",
                "position_intraday_evidence_requires_discussion",
            ],
            "position_attention_effect_policy": {
                "all_codes_must_be_listed_in_position_attention_codes": True,
                "notes_must_summarize_all_attention_codes": True,
                "detailed_effects_required_for_codes": [
                    "high_urgency_position_requires_contextual_rationale",
                    "position_dynamic_management_requires_review",
                ],
            },
            "append_jsonl_object_to": "/tmp/hermes_position_judgments.jsonl",
        },
    }


class HermesPositionJudgmentWritePacketTests(unittest.TestCase):
    def test_build_report_contains_only_unjudged_high_urgency_work_items(self):
        packet_payload = {
            "schema": "hermes_signal_review_packet_v1",
            "packet_id": "packet-1",
            "position_judgment_worklist": {
                "schema": "hermes_position_judgment_worklist_v1",
                "items": [
                    work_item("simulation:8:MSFT:2026-06-18:reduce_or_exit_review", role="simulation", symbol="MSFT"),
                    work_item("user:3:AAPL:2026-06-18:reduce_or_exit_review", role="user", symbol="AAPL"),
                    work_item("user:3:PDD:2026-06-18:reduce_or_exit_review", role="user", symbol="PDD", urgency="medium"),
                ],
            },
        }
        audit_payload = {
            "schema": "hermes_position_judgment_audit_report_v1",
            "coverage": {
                "position_review_item_count": 3,
                "judged_review_count": 0,
                "unjudged_high_urgency_review_count": 2,
                "unjudged_high_urgency_examples": [
                    {"review_id": "simulation:8:MSFT:2026-06-18:reduce_or_exit_review"},
                    {"review_id": "user:3:AAPL:2026-06-18:reduce_or_exit_review"},
                ],
            },
        }

        payload = packet.build_report(packet_payload, audit_payload)

        self.assertEqual(payload["schema"], "hermes_position_judgment_write_packet_v1")
        self.assertEqual(payload["status"], "ACTION_REQUIRED")
        self.assertTrue(payload["source"]["read_only"])
        self.assertTrue(payload["source"]["draft_only"])
        self.assertFalse(payload["source"]["writes_judgments"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertEqual(payload["summary"]["pending_item_count"], 2)
        self.assertEqual(
            [item["review_id"] for item in payload["items"]],
            [
                "user:3:AAPL:2026-06-18:reduce_or_exit_review",
                "simulation:8:MSFT:2026-06-18:reduce_or_exit_review",
            ],
        )
        first = payload["items"][0]
        self.assertEqual(first["required_output_fields"]["schema"], "hermes_position_judgment_v1")
        self.assertTrue(first["required_output_fields"]["advisory_only"])
        self.assertFalse(first["required_output_fields"]["submits_orders"])
        self.assertIn("decision", first["must_complete_fields"])
        self.assertIn("position_attention_effects", first["must_complete_fields"])
        self.assertEqual(first["context_summary"]["dynamic_management"]["target_status"], "below_signal_stop")
        self.assertEqual(
            first["context_summary"]["intraday_position_evidence"]["alignment"],
            "supports_recommended_action",
        )
        self.assertIn(
            "position_dynamic_management_requires_review",
            first["required_detailed_effect_codes"],
        )
        self.assertIn("after_append_command", payload["validation"])
        self.assertIn("Templates and required_output_fields are not judgments.", payload["hard_rules"])


if __name__ == "__main__":
    unittest.main()
