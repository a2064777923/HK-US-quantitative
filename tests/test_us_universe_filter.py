import unittest

from scripts.us_universe_filter import is_supported_us_equity, normalize_us_symbol


class USUniverseFilterTests(unittest.TestCase):
    def test_normalizes_supported_common_symbols(self):
        self.assertEqual(normalize_us_symbol("brk.b"), "BRK-B")
        self.assertEqual(normalize_us_symbol(" aapl "), "AAPL")

    def test_rejects_special_symbol_formats(self):
        self.assertEqual(normalize_us_symbol("ABR^D"), "")
        self.assertEqual(normalize_us_symbol("BRK/B"), "")
        self.assertEqual(normalize_us_symbol("1ABC"), "")
        self.assertEqual(normalize_us_symbol("ABC$"), "")

    def test_rejects_non_common_stock_instrument_names(self):
        self.assertFalse(is_supported_us_equity({"symbol": "ABR-D", "name": "Arbor Realty Trust Preferred Stock"}))
        self.assertFalse(is_supported_us_equity({"symbol": "XYZW", "name": "Example Warrant"}))
        self.assertFalse(is_supported_us_equity({"symbol": "XYZU", "name": "Example Acquisition Corp. Units"}))
        self.assertFalse(is_supported_us_equity({"symbol": "XYZR", "name": "Example Acquisition Corp Rights"}))
        self.assertFalse(is_supported_us_equity({"symbol": "ABC", "name": "Example Notes Due 2030"}))
        self.assertFalse(is_supported_us_equity({"symbol": "ABC", "name": "Example 9.875% Senior Notes Due 2030"}))
        self.assertTrue(is_supported_us_equity({"symbol": "MSFT", "name": "Microsoft Corporation Common Stock"}))
        self.assertTrue(is_supported_us_equity({"symbol": "BABA", "name": "Alibaba Group Holding Limited American Depositary Shares"}))
        self.assertTrue(is_supported_us_equity({"symbol": "NOK", "name": "Nokia Corporation Sponsored American Depositary Shares"}))
        self.assertTrue(is_supported_us_equity({"symbol": "UNH", "name": "UnitedHealth Group Incorporated Common Stock"}))


if __name__ == "__main__":
    unittest.main()
