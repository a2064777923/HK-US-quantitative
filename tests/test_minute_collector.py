import unittest

from scripts import minute_collector


class MinuteCollectorTests(unittest.TestCase):
    def test_row_values_include_source_granularity_when_supported(self):
        row, last_price = minute_collector.row_values(
            "00700",
            "0930 300.5 1000",
            "2026-06-18",
            None,
            include_source_granularity=True,
        )

        self.assertEqual(last_price, 300.5)
        self.assertIn("'tencent_min'", row)
        self.assertIn("'minute_snapshot_price'", row)

    def test_row_values_omit_source_granularity_for_legacy_schema(self):
        row, _ = minute_collector.row_values(
            "00700",
            "0930 300.5 1000",
            "2026-06-18",
            None,
            include_source_granularity=False,
        )

        self.assertIn("'tencent_min'", row)
        self.assertNotIn("'minute_snapshot_price'", row)


if __name__ == "__main__":
    unittest.main()
