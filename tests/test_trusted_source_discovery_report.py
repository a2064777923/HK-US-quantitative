import tempfile
import unittest
from pathlib import Path

from scripts import trusted_source_discovery_report as discovery


def fake_probe_factory(reachable_urls=None):
    reachable_urls = set(reachable_urls or [])

    def fake_probe(url, timeout_seconds=1.5):
        return {
            "url": url,
            "configured": bool(url),
            "reachable": url in reachable_urls,
            "reason": "tcp_connect_ok" if url in reachable_urls else "tcp_connect_failed:fake",
        }

    return fake_probe


class TrustedSourceDiscoveryReportTests(unittest.TestCase):
    def test_missing_sources_report_missing_capabilities_without_side_effects(self):
        payload = discovery.build_report(
            env={},
            files={},
            infohub_url="",
            probe_tcp_func=fake_probe_factory(),
        )

        self.assertEqual(payload["schema"], "trusted_source_discovery_report_v1")
        self.assertEqual(payload["status"], "MISSING")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["writes_ingest_files"])
        self.assertFalse(payload["source"]["prints_secret_values"])
        by_capability = {row["capability"]: row for row in payload["capabilities"]}
        self.assertEqual(by_capability["trusted_event_context"]["status"], "MISSING")
        self.assertEqual(by_capability["full_fundamentals_context"]["status"], "MISSING")
        self.assertIn("configure_wudao_broker_or_official_event_source", payload["recommendations"])

    def test_infohub_reachable_is_fallback_context_not_full_trusted_coverage(self):
        payload = discovery.build_report(
            env={"EXTERNAL_CONTEXT_INFOHUB_URL": "http://127.0.0.1:8899"},
            files={},
            probe_tcp_func=fake_probe_factory({"http://127.0.0.1:8899"}),
        )

        self.assertEqual(payload["status"], "WARN")
        providers = {row["provider"]: row for row in payload["providers"]}
        self.assertEqual(providers["infohub"]["status"], "READY_TO_VALIDATE_PAYLOAD")
        by_capability = {row["capability"]: row for row in payload["capabilities"]}
        self.assertEqual(by_capability["infohub_public_context"]["status"], "READY_TO_VALIDATE_PAYLOAD")
        self.assertEqual(by_capability["trusted_event_context"]["status"], "MISSING")
        self.assertIn(
            "treat_infohub_public_context_as_fallback_until_trusted_provider_payloads_pass_preflight",
            payload["recommendations"],
        )

    def test_configured_secrets_are_redacted_and_capabilities_become_configured_unverified(self):
        env = {
            "WUDAO_API_KEY": "should-not-appear",
            "BROKER_API_BASE": "https://broker.example.test",
            "BROKER_API_KEY": "broker-secret",
            "FUNDAMENTALS_API_BASE": "https://fundamentals.example.test",
            "FUNDAMENTALS_API_KEY": "fund-secret",
        }
        payload = discovery.build_report(
            env=env,
            files={},
            infohub_url="",
            probe_tcp_func=fake_probe_factory(),
        )

        serialized = str(payload)
        self.assertNotIn("should-not-appear", serialized)
        self.assertNotIn("broker-secret", serialized)
        self.assertNotIn("fund-secret", serialized)
        providers = {row["provider"]: row for row in payload["providers"]}
        self.assertTrue(providers["wudao"]["configured"])
        self.assertIn("WUDAO_API_KEY", providers["wudao"]["env"]["present_env_keys"])
        self.assertTrue(providers["broker"]["configured"])
        self.assertTrue(providers["fundamentals_vendor"]["configured"])
        by_capability = {row["capability"]: row for row in payload["capabilities"]}
        self.assertEqual(by_capability["trusted_event_context"]["status"], "CONFIGURED_UNVERIFIED")
        self.assertEqual(by_capability["full_fundamentals_context"]["status"], "CONFIGURED_UNVERIFIED")
        self.assertIn("run_dry_run_export_and_trusted_source_preflight_for_configured_sources", payload["recommendations"])

    def test_input_file_summary_counts_json_items_and_jsonl_lines(self):
        with tempfile.TemporaryDirectory() as td:
            json_path = Path(td) / "external.json"
            jsonl_path = Path(td) / "external.jsonl"
            json_path.write_text('{"schema":"x","items":[{},{}],"warnings":["w"]}', encoding="utf-8")
            jsonl_path.write_text('{"id":1}\n{"id":2}\n', encoding="utf-8")

            payload = discovery.build_report(
                env={},
                files={"external_json": str(json_path), "external_jsonl": str(jsonl_path)},
                infohub_url="",
                probe_tcp_func=fake_probe_factory(),
            )

        files = payload["input_files"]
        self.assertEqual(files["external_json"]["item_count"], 2)
        self.assertEqual(files["external_json"]["warnings_count"], 1)
        self.assertEqual(files["external_jsonl"]["line_count"], 2)


if __name__ == "__main__":
    unittest.main()
