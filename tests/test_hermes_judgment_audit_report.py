import unittest
from datetime import datetime
from pathlib import Path
import tempfile
import json

from scripts import hermes_judgment_audit_report as audit


def context_review(**overrides):
    review = {flag: True for flag in audit.REQUIRED_CONTEXT_REVIEW_FLAGS}
    review["notes"] = ["unit test reviewed all required context"]
    review.update(overrides)
    return review


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
    }
    if decision in ("approve", "reduce"):
        item["context_review"] = context_review()
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


def packet(items=None, market_regime="risk_on", outcome_ok=True):
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
            "markets": {"US": {"regime": market_regime, "notes": ["buy_signals_against_weak_breadth"] if market_regime == "risk_off" else []}},
        },
        "strategy_evidence": strategy,
        "review_items": items if items is not None else [review_item()],
    }


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

    def test_missing_packet_id_is_flagged(self):
        item = judgment("sig-1")
        item.pop("packet_id")

        payload = audit.build_report([item], packet(market_regime="risk_on", outcome_ok=True))

        row = payload["judgments"][0]
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("judgment_missing_packet_id", row["reasons"])

    def test_approval_requires_structured_context_review(self):
        item = judgment("sig-1")
        item.pop("context_review")

        payload = audit.build_report([item], packet(market_regime="risk_on", outcome_ok=True))
        row = payload["judgments"][0]

        self.assertEqual(row["status"], "FAIL")
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
                    ),
                )
            ],
            packet(market_regime="risk_on", outcome_ok=True),
        )
        row = payload["judgments"][0]

        self.assertEqual(row["status"], "FAIL")
        self.assertIn("context_review_missing_external_market_context_reviewed", row["reasons"])
        self.assertIn("context_review_missing_market_sentiment_reviewed", row["reasons"])

    def test_schema_required_context_review_flags_match_audit(self):
        schema_path = Path(__file__).resolve().parents[1] / "config" / "hermes_trade_judgment.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        required = schema["$defs"]["required_context_review"]

        self.assertEqual(set(required["required"]), set(audit.REQUIRED_CONTEXT_REVIEW_FLAGS))
        for flag in audit.REQUIRED_CONTEXT_REVIEW_FLAGS:
            self.assertEqual(required["properties"][flag], {"const": True})


if __name__ == "__main__":
    unittest.main()
