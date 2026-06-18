import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PositionPriceCronTests(unittest.TestCase):
    def assert_user_price_cron_covers_hk_and_us_sessions(self, path):
        text = path.read_text(encoding="utf-8")
        user_lines = [
            line
            for line in text.splitlines()
            if "QM_PRICE_UPDATE_PORTFOLIO_ID=3" in line
            and "update_portfolio_prices.py" in line
            and not line.lstrip().startswith("#")
        ]

        joined = "\n".join(user_lines)
        self.assertIn("9-16,21-23", joined)
        self.assertIn("0-5", joined)
        self.assertIn("* * 2-6", joined)
        self.assertEqual(len(user_lines), 2)

    def test_crontab_template_updates_user_prices_during_us_session(self):
        self.assert_user_price_cron_covers_hk_and_us_sessions(ROOT / "config" / "crontab.txt")

    def test_hermes_crontab_template_updates_user_prices_during_us_session(self):
        self.assert_user_price_cron_covers_hk_and_us_sessions(ROOT / "config" / "hermes_v5_crontab.txt")


if __name__ == "__main__":
    unittest.main()
