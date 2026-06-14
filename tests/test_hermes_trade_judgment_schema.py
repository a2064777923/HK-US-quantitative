import json
import unittest
from pathlib import Path

from scripts import hermes_judgment_audit_report as audit


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "config" / "hermes_trade_judgment.schema.json"


class HermesTradeJudgmentSchemaTests(unittest.TestCase):
    def load_schema(self):
        return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_required_context_review_definition_matches_audit_flags(self):
        schema = self.load_schema()
        definition = schema["$defs"]["required_context_review"]

        self.assertEqual(tuple(definition["required"]), audit.REQUIRED_CONTEXT_REVIEW_FLAGS)
        for flag in audit.REQUIRED_CONTEXT_REVIEW_FLAGS:
            self.assertEqual(definition["properties"][flag], {"const": True})

    def test_approve_and_reduce_require_strict_context_review_definition(self):
        schema = self.load_schema()
        rules = schema["allOf"]
        decision_rules = {
            rule["if"]["properties"]["decision"].get("const"): rule
            for rule in rules
            if "decision" in rule.get("if", {}).get("properties", {})
            and "const" in rule["if"]["properties"]["decision"]
        }

        self.assertEqual(
            decision_rules["approve"]["then"]["properties"]["context_review"]["$ref"],
            "#/$defs/required_context_review",
        )
        self.assertEqual(
            decision_rules["reduce"]["then"]["properties"]["context_review"]["$ref"],
            "#/$defs/required_context_review",
        )
        self.assertIn("context_review", decision_rules["approve"]["then"]["required"])
        self.assertIn("context_review", decision_rules["reduce"]["then"]["required"])
        self.assertIn("max_quantity", decision_rules["reduce"]["then"]["required"])

    def test_market_sentiment_acknowledgement_fields_are_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["market_sentiment_risk_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["market_sentiment_indicator_ids"]["type"], "array")
        self.assertEqual(properties["market_sentiment_notes"]["type"], "array")
        self.assertEqual(properties["market_sentiment_support_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["market_sentiment_support_indicator_ids"]["type"], "array")
        self.assertEqual(properties["market_sentiment_support_notes"]["type"], "array")

    def test_market_sentiment_coverage_fields_are_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["market_sentiment_coverage_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["market_sentiment_coverage_status"]["type"], "string")
        self.assertEqual(properties["market_sentiment_coverage_notes"]["type"], "array")

    def test_external_market_context_acknowledgement_fields_are_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["external_market_context_risk_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["external_market_context_ids"]["type"], "array")
        self.assertEqual(properties["external_market_context_notes"]["type"], "array")

    def test_external_market_context_support_fields_are_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["external_market_context_support_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["external_market_context_support_ids"]["type"], "array")
        self.assertEqual(properties["external_market_context_support_notes"]["type"], "array")

    def test_external_market_context_coverage_fields_are_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["external_market_context_coverage_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["external_market_context_coverage_status"]["type"], "string")
        self.assertEqual(properties["external_market_context_coverage_notes"]["type"], "array")

    def test_market_context_coverage_fields_are_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["market_context_coverage_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["market_context_coverage_status"]["type"], "string")
        self.assertEqual(properties["market_context_coverage_notes"]["type"], "array")

    def test_fundamentals_context_support_fields_are_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["fundamentals_context_support_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["fundamentals_context_support_symbols"]["type"], "array")
        self.assertEqual(properties["fundamentals_context_support_metrics"]["type"], "array")
        self.assertEqual(properties["fundamentals_context_support_notes"]["type"], "array")

    def test_fundamentals_context_coverage_fields_are_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["fundamentals_context_coverage_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["fundamentals_context_coverage_status"]["type"], "string")
        self.assertEqual(properties["fundamentals_context_coverage_notes"]["type"], "array")

    def test_event_catalyst_support_fields_are_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["event_catalyst_support_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["event_catalyst_support_signal_ids"]["type"], "array")
        self.assertEqual(properties["event_catalyst_support_notes"]["type"], "array")

    def test_event_catalyst_signal_coverage_fields_are_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["event_catalyst_signal_coverage_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["event_catalyst_signal_coverage_status"]["type"], "string")
        self.assertEqual(properties["event_catalyst_signal_coverage_notes"]["type"], "array")

    def test_event_catalyst_coverage_fields_are_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["event_catalyst_coverage_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["event_catalyst_coverage_status"]["type"], "string")
        self.assertEqual(properties["event_catalyst_coverage_notes"]["type"], "array")

    def test_simulation_performance_acknowledgement_fields_are_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["simulation_performance_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["simulation_performance_status"]["type"], "string")
        self.assertEqual(properties["simulation_performance_reason_codes"]["type"], "array")
        self.assertEqual(properties["simulation_performance_notes"]["type"], "array")

    def test_hermes_alpha_evidence_fields_are_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["hermes_alpha_evidence_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["hermes_alpha_evidence_status"]["type"], "string")
        self.assertEqual(properties["hermes_alpha_evidence_reasons"]["type"], "array")
        self.assertEqual(properties["hermes_alpha_evidence_notes"]["type"], "array")

    def test_intraday_signal_evidence_fields_are_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["intraday_signal_evidence_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["intraday_signal_evidence_alignment"]["type"], "string")
        self.assertEqual(properties["intraday_signal_evidence_codes"]["type"], "array")
        self.assertEqual(properties["intraday_signal_evidence_notes"]["type"], "array")


if __name__ == "__main__":
    unittest.main()
