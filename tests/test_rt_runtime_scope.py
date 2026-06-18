import unittest
from unittest.mock import patch

from scripts import rt_runtime_scope as runtime_scope


class RuntimeScopeTests(unittest.TestCase):
    def test_current_runtime_sample_scope_uses_loaded_strategy_and_watchlist(self):
        with patch.object(
            runtime_scope.v5,
            "load_strategy_config",
            return_value=(
                {"config_id": "cfg-runtime", "version": "v5.5-test"},
                {"strategy_config_id": "cfg-runtime", "source": "file", "version": "v5.5-test", "warnings": []},
            ),
        ), patch.object(
            runtime_scope.v5,
            "load_watchlists",
            return_value=(
                ["00700"],
                ["09988"],
                {
                    "watchlist_id": "wl-runtime",
                    "source_file": "/root/rt_signal_watchlist.json",
                    "markets": {
                        "HK": {"source": "file", "count": 1, "sample": ["00700"]},
                        "US": {"source": "file", "count": 1, "sample": ["PDD"]},
                    },
                    "warnings": [],
                },
            ),
        ):
            payload = runtime_scope.current_runtime_sample_scope()

        self.assertEqual(payload["mode"], "runtime_strategy_config_and_watchlist")
        self.assertEqual(payload["strategy_config_id"], "cfg-runtime")
        self.assertEqual(payload["watchlist_id"], "wl-runtime")
        self.assertEqual(payload["strategy_config_version"], "v5.5-test")
        self.assertEqual(payload["scope_source"], "runtime_v5_config_and_watchlist")

    def test_current_runtime_sample_scope_returns_unavailable_for_fallback_default(self):
        with patch.object(
            runtime_scope.v5,
            "load_strategy_config",
            return_value=(
                {"config_id": "fallback", "version": "v5-default"},
                {"strategy_config_id": "fallback", "source": "fallback_default", "version": "v5-default", "warnings": []},
            ),
        ), patch.object(
            runtime_scope.v5,
            "load_watchlists",
            return_value=(
                ["00700"],
                ["09988"],
                {
                    "watchlist_id": "wl-fallback",
                    "source_file": "",
                    "markets": {
                        "HK": {"source": "fallback_hardcoded", "count": 1, "sample": ["00700"]},
                        "US": {"source": "fallback_hardcoded", "count": 1, "sample": ["PDD"]},
                    },
                    "warnings": [],
                },
            ),
        ):
            payload = runtime_scope.current_runtime_sample_scope()

        self.assertEqual(payload["mode"], "runtime_scope_unavailable")
        self.assertIn("runtime_strategy_or_watchlist_not_authoritative", payload["warnings"])


if __name__ == "__main__":
    unittest.main()
