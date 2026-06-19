import json
import unittest
from pathlib import Path

from scripts import hermes_position_judgment_audit_report as audit


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "config" / "hermes_position_judgment.schema.json"


class HermesPositionJudgmentSchemaTests(unittest.TestCase):
    def load_schema(self):
        return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_context_review_definition_matches_position_audit_flags(self):
        schema = self.load_schema()
        definition = schema["$defs"]["required_context_review"]

        self.assertEqual(tuple(definition["required"]), audit.REQUIRED_CONTEXT_REVIEW_FLAGS)
        for flag in audit.REQUIRED_CONTEXT_REVIEW_FLAGS:
            self.assertEqual(definition["properties"][flag], {"const": True})

    def test_context_review_property_uses_required_definition(self):
        schema = self.load_schema()

        self.assertEqual(
            schema["properties"]["context_review"]["$ref"],
            "#/$defs/required_context_review",
        )

    def test_position_attention_acknowledgement_fields_are_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["position_attention_acknowledged"]["type"], "boolean")
        self.assertEqual(properties["position_attention_codes"]["type"], "array")
        self.assertEqual(properties["position_attention_notes"]["type"], "array")
        self.assertEqual(properties["position_attention_effects"]["type"], "array")
        effect_schema = properties["position_attention_effects"]["items"]
        self.assertEqual(effect_schema["required"], ["code", "effect", "decision_impact"])

    def test_manual_only_field_is_in_schema(self):
        schema = self.load_schema()
        properties = schema["properties"]

        self.assertEqual(properties["manual_only"]["type"], "boolean")
        self.assertIn("role=user", properties["manual_only"]["description"])


if __name__ == "__main__":
    unittest.main()
