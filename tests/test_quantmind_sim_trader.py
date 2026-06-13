import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from scripts import quantmind_sim_trader as trader


class QuantmindSimTraderSafetyTests(unittest.TestCase):
    def test_legacy_sim_trader_disabled_by_default(self):
        self.assertFalse(trader.legacy_sim_trader_enabled({}))
        self.assertFalse(trader.legacy_sim_trader_enabled({"QM_LEGACY_SIM_TRADER_ENABLE": "0"}))
        self.assertTrue(trader.legacy_sim_trader_enabled({"QM_LEGACY_SIM_TRADER_ENABLE": "1"}))

    def test_legacy_api_credentials_use_environment_only(self):
        user, password = trader.legacy_api_credentials({})

        self.assertEqual(user, "")
        self.assertEqual(password, "")

        user, password = trader.legacy_api_credentials(
            {
                "QM_LEGACY_SIM_API_USER": "legacy-user",
                "QM_LEGACY_SIM_API_PASSWORD": "legacy-password",
                "QM_API_USER": "fallback-user",
                "QM_API_PASSWORD": "fallback-password",
            }
        )

        self.assertEqual(user, "legacy-user")
        self.assertEqual(password, "legacy-password")

    def test_run_returns_before_login_when_disabled(self):
        out = io.StringIO()
        with (
            patch.object(trader, "legacy_sim_trader_enabled", return_value=False),
            patch.object(trader, "get_token") as get_token,
            patch.object(trader, "get_signals") as get_signals,
            patch.object(trader, "submit_order") as submit_order,
            redirect_stdout(out),
        ):
            trader.run()

        self.assertIn("legacy sim trader disabled", out.getvalue())
        get_token.assert_not_called()
        get_signals.assert_not_called()
        submit_order.assert_not_called()

    def test_get_token_requires_explicit_credentials(self):
        with patch.object(trader, "legacy_api_credentials", return_value=("", "")):
            with self.assertRaises(RuntimeError) as ctx:
                trader.get_token()

        self.assertIn("missing API credentials", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
