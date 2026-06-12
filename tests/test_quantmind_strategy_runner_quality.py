import unittest

from scripts import quantmind_strategy_runner as runner


class QuantmindStrategyRunnerQualityTests(unittest.TestCase):
    def test_signal_quality_gate_accepts_adequate_rr(self):
        quality = {"order_prices": {"rr_ratio": 2.2}, "risk_flags": []}

        ok, reasons, rr, flags = runner.signal_quality_gate(quality)

        self.assertTrue(ok)
        self.assertEqual(reasons, [])
        self.assertEqual(rr, 2.2)
        self.assertEqual(flags, [])

    def test_signal_quality_gate_rejects_low_rr(self):
        quality = {"order_prices": {"rr_ratio": 1.2}, "risk_flags": []}

        ok, reasons, rr, _ = runner.signal_quality_gate(quality)

        self.assertFalse(ok)
        self.assertEqual(rr, 1.2)
        self.assertIn("rr_ratio_below_1.5", reasons)

    def test_signal_quality_gate_rejects_blocking_risk_flag(self):
        quality = {"order_prices": {"rr_ratio": 3.0}, "risk_flags": ["風險回報比偏低(0)"]}

        ok, reasons, rr, flags = runner.signal_quality_gate(quality)

        self.assertFalse(ok)
        self.assertEqual(rr, 3.0)
        self.assertEqual(flags, ["風險回報比偏低(0)"])
        self.assertIn("風險回報比偏低(0)", reasons)

    def test_legacy_new_entries_disabled_by_default(self):
        self.assertFalse(runner.legacy_new_entries_enabled({}))
        self.assertFalse(runner.legacy_new_entries_enabled({"QM_STRATEGY_ALLOW_NEW_POSITIONS": "0"}))
        self.assertTrue(runner.legacy_new_entries_enabled({"QM_STRATEGY_ALLOW_NEW_POSITIONS": "1"}))

    def test_select_new_entry_candidates_blocks_when_legacy_gate_disabled(self):
        signals = [{"symbol": "00700"}, {"symbol": "09988"}]
        positions = {}

        candidates = runner.select_new_entry_candidates(signals, positions, allow_new_entries=False)

        self.assertEqual(candidates, [])

    def test_select_new_entry_candidates_filters_held_and_available_slots(self):
        signals = [{"symbol": "00700"}, {"symbol": "09988"}, {"symbol": "03690"}]
        positions = {"00700": {"volume": 100}, "00005": {"volume": 400}}

        candidates = runner.select_new_entry_candidates(
            signals,
            positions,
            max_positions=3,
            allow_new_entries=True,
        )

        self.assertEqual(candidates, [{"symbol": "09988"}])


if __name__ == "__main__":
    unittest.main()
