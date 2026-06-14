import argparse
import json
import os
import tempfile
import unittest

from scripts import local_backtest_dataset as dataset


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


class LocalBacktestDatasetTests(unittest.TestCase):
    def test_parse_tencent_daily_rows_filters_invalid_rows(self):
        payload = {
            "data": {
                "hk00700": {
                    "qfqday": [
                        ["2026-06-10", "450", "455", "456", "448", "1000"],
                        ["2026-06-11", "0", "455", "456", "448", "1000"],
                        ["2020-01-01", "1", "1", "1", "1", "1"],
                    ]
                }
            }
        }

        rows, invalid = dataset.parse_tencent_daily_rows("00700", payload, "2026-01-01", "2026-12-31")

        self.assertEqual(len(rows), 1)
        self.assertEqual(invalid, 1)
        self.assertEqual(rows[0]["symbol"], "00700")
        self.assertEqual(rows[0]["dt"], "2026-06-10")
        self.assertEqual(rows[0]["open_price"], 450.0)
        self.assertEqual(rows[0]["close_price"], 455.0)

    def test_fetch_alpaca_bars_reads_credentials_from_environment_and_paginates(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "bars": {
                            "AAPL": [
                                {
                                    "t": "2026-06-10T04:00:00Z",
                                    "o": 100,
                                    "h": 105,
                                    "l": 99,
                                    "c": 104,
                                    "v": 123,
                                }
                            ]
                        },
                        "next_page_token": "next",
                    }
                ),
                FakeResponse(
                    {
                        "bars": {
                            "MSFT": [
                                {
                                    "t": "2026-06-11T04:00:00Z",
                                    "o": 200,
                                    "h": 205,
                                    "l": 199,
                                    "c": 204,
                                    "v": 456,
                                }
                            ]
                        }
                    }
                ),
            ]
        )

        rows = dataset.fetch_alpaca_bars(
            ["AAPL", "MSFT"],
            "2026-06-01",
            "2026-06-30",
            session=session,
            env={"APCA_API_KEY_ID": "key", "APCA_API_SECRET_KEY": "secret"},
        )

        self.assertEqual([row["symbol"] for row in rows], ["AAPL", "MSFT"])
        self.assertEqual(rows[0]["dt"], "2026-06-10")
        self.assertEqual(session.calls[0]["headers"]["APCA-API-KEY-ID"], "key")
        self.assertEqual(session.calls[1]["params"]["page_token"], "next")

    def test_build_dataset_writes_local_only_metadata_and_backtest_csvs(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "code": 0,
                        "data": {
                            "hk00700": {
                                "qfqday": [["2026-06-10", "450", "455", "456", "448", "1000"]]
                            }
                        },
                    }
                ),
                FakeResponse(
                    {
                        "bars": {
                            "AAPL": [
                                {
                                    "t": "2026-06-10T04:00:00Z",
                                    "o": 100,
                                    "h": 105,
                                    "l": 99,
                                    "c": 104,
                                    "v": 123,
                                }
                            ]
                        }
                    }
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                output_dir=tmp,
                start_date="2026-06-01",
                end_date="2026-06-30",
                hk_symbol=["00700"],
                us_symbol=["AAPL"],
                skip_default_watchlist=True,
                skip_hk=False,
                skip_us=False,
                require_us=True,
                tencent_count=10,
                alpaca_feed="iex",
                alpaca_adjustment="all",
                us_intraday_timeframe=[],
                intraday_start_date=None,
                intraday_end_date=None,
                fetch_sleep_seconds=0,
            )

            metadata = dataset.build_dataset(
                args,
                session=session,
                env={"APCA_API_KEY_ID": "key", "APCA_API_SECRET_KEY": "secret"},
            )

            self.assertTrue(os.path.exists(os.path.join(tmp, "hk_klines_v2.csv")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "us_klines.csv")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "all_klines.csv")))
            self.assertTrue(metadata["storage_policy"]["raw_data_local_only"])
            self.assertFalse(metadata["storage_policy"]["commit_raw_csv_to_git"])
            self.assertFalse(metadata["storage_policy"]["copy_to_server_by_default"])
            self.assertEqual(metadata["sources"]["HK"]["row_count"], 1)
            self.assertEqual(metadata["sources"]["US"]["row_count"], 1)


if __name__ == "__main__":
    unittest.main()
