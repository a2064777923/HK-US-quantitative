import json
import os
import tempfile
import unittest

from scripts import local_backtest_reliability_report as report


def sample_metadata(symbol_count=90, raw_local_only=True, us_feed="iex"):
    hk_count = symbol_count // 2
    us_count = symbol_count - hk_count
    return {
        "schema": "hk_us_local_backtest_dataset_v1",
        "generated_at": "2026-06-14T12:00:00",
        "date_range": {"start": "2021-01-01", "end": "2026-06-14"},
        "storage_policy": {
            "raw_data_local_only": raw_local_only,
            "commit_raw_csv_to_git": False if raw_local_only else True,
            "copy_to_server_by_default": False,
            "keep_broad_fine_grained_raw_data_locally": True,
            "production_consumes_compact_reports_only": True,
            "server_sync_requires_explicit_promotion": True,
            "required_manifest_fields": [
                "provider",
                "feed",
                "adjustment",
                "timezone",
                "path",
                "retrieved_at",
                "coverage",
                "gaps",
                "freshness",
                "license_scope",
                "checksum",
            ],
            "large_file_retention": {
                "prefer_partitioned_compressed_formats": ["parquet_zstd", "csv_gzip"],
                "partition_keys": ["provider", "market", "timeframe", "symbol", "date"],
                "dedupe_keys": ["provider", "symbol", "timestamp", "adjustment"],
            },
        },
        "sources": {
            "HK": {
                "provider": "tencent_newfqkline",
                "adjustment": "qfq",
                "symbol_count_requested": hk_count,
                "row_count": hk_count * 1000,
                "warnings": [],
            },
            "US": {
                "provider": "alpaca_market_data",
                "feed": us_feed,
                "adjustment": "all",
                "symbol_count_requested": us_count,
                "row_count": us_count * 1000,
            },
        },
        "coverage": {
            "HK": [
                {"symbol": f"{index:05d}", "rows": 1000, "first": "2021-01-04", "last": "2026-06-12"}
                for index in range(hk_count)
            ],
            "US": [
                {"symbol": f"US{index}", "rows": 1000, "first": "2021-01-04", "last": "2026-06-12"}
                for index in range(us_count)
            ],
        },
        "intraday_outputs": [],
    }


def sample_backtest(trades=900, sharpe=1.1, drawdown=12.0, ret=200.0, include_market=True):
    trade_rows = []
    for index in range(trades):
        win = index % 2 == 0
        trade = {
            "s": "AAPL" if index % 3 else "00700",
            "ed": "2021-01-04",
            "xd": f"{2021 + (index % 6)}-06-01",
            "pn": 100.0 if win else -55.0,
            "pc": 2.0 if win else -1.1,
            "r": "SELL" if win else "止損",
        }
        if include_market:
            trade["m"] = "US" if index % 3 else "HK"
        trade_rows.append(trade)
    return {
        "summary": {
            "init": 100000.0,
            "final": 100000.0 * (1 + ret / 100),
            "ret": ret,
            "annual": 35.0,
            "sharpe": sharpe,
            "dd": drawdown,
            "trades": trades,
            "wr": 50.0,
        },
        "trades": trade_rows,
        "equity": [{"d": "2021-01-04", "eq": 100000}, {"d": "2026-06-12", "eq": 300000}],
        "years": {str(year): {"c": 100, "p": 1000.0, "wr": 50.0, "hk": 40, "us": 60} for year in range(2021, 2027)},
    }


def sample_manifest(path, market="HK", checksum=None):
    return {
        "provider": "tencent_newfqkline" if market == "HK" else "alpaca_market_data",
        "market": market,
        "feed": None if market != "US" else "iex",
        "timeframe": "1d",
        "adjustment": "qfq" if market == "HK" else "all",
        "timezone": "Asia/Hong_Kong" if market == "HK" else "America/New_York",
        "path": path,
        "retrieved_at": "2026-06-14T12:00:00",
        "coverage": {"row_count": 2, "symbol_count": 1, "first": "2026-06-10", "last": "2026-06-11"},
        "gaps": [],
        "freshness": {"retrieved_at": "2026-06-14T12:00:00", "scope": "local_dataset_build"},
        "license_scope": "operator_local_research_use",
        "checksum": checksum or report.file_checksum(path),
        "file_size_bytes": os.path.getsize(path),
    }


class LocalBacktestReliabilityReportTests(unittest.TestCase):
    def test_positive_baseline_still_not_promotion_ready(self):
        payload = report.build_report(sample_metadata(), sample_backtest(), sample_backtest(sharpe=0.85, drawdown=19.0))

        self.assertEqual(payload["schema"], "local_backtest_reliability_report_v1")
        self.assertEqual(payload["summary"]["overall_status"], "RESEARCH_USEFUL_WITH_LIMITATIONS")
        self.assertFalse(payload["summary"]["promotion_ready"])
        self.assertEqual(payload["hermes_contract"]["contract"], "research_evidence_only")
        self.assertTrue(payload["source"]["read_only_inputs"])
        self.assertFalse(payload["source"]["changes_v5"])
        self.assertIn(
            "do_not_promote_strategy_from_single_local_backtest",
            [item["code"] for item in payload["recommendations"]],
        )
        self.assertEqual(payload["dataset"]["status"], "WARN")
        self.assertTrue(payload["dataset"]["raw_data_lake_contract"]["production_consumes_compact_reports_only"])
        self.assertIn(
            "raw_lake_manifest_contract_complete",
            [item["code"] for item in payload["dataset"]["checks"]],
        )
        self.assertIn(
            "hk_manifest_unavailable",
            [item["code"] for item in payload["dataset"]["checks"]],
        )
        self.assertEqual(payload["backtests"][0]["trade_distribution"]["by_market"][0]["key"], "US")

    def test_embedded_manifest_checksum_is_verified_against_local_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "hk_klines_v2.csv")
            with open(csv_path, "w", encoding="utf-8") as handle:
                handle.write("symbol,dt,open_price,high_price,low_price,close_price,volume\n")
                handle.write("00700,2026-06-10,450,456,448,455,1000\n")

            metadata = sample_metadata()
            metadata["manifests"] = {"HK": sample_manifest(csv_path, market="HK")}

            payload = report.build_report(metadata, sample_backtest(), sample_backtest())
            codes = [item["code"] for item in payload["dataset"]["checks"]]

            self.assertIn("hk_manifest_complete", codes)
            self.assertIn("hk_manifest_checksum_verified", codes)
            self.assertNotIn("hk_manifest_validation_failed", codes)

    def test_manifest_checksum_mismatch_is_a_hard_dataset_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "hk_klines_v2.csv")
            with open(csv_path, "w", encoding="utf-8") as handle:
                handle.write("symbol,dt,open_price,high_price,low_price,close_price,volume\n")
                handle.write("00700,2026-06-10,450,456,448,455,1000\n")
            manifest = sample_manifest(csv_path, market="HK")
            with open(csv_path, "a", encoding="utf-8") as handle:
                handle.write("00700,2026-06-11,455,458,452,457,1100\n")

            metadata = sample_metadata()
            metadata["manifests"] = {"HK": manifest}

            payload = report.build_report(metadata, sample_backtest(), sample_backtest())
            codes = [item["code"] for item in payload["dataset"]["checks"]]

            self.assertEqual(payload["dataset"]["status"], "FAIL")
            self.assertIn("hk_manifest_checksum_mismatch", codes)
            self.assertIn("hk_manifest_validation_failed", codes)

    def test_hard_data_and_performance_failures_make_evidence_insufficient(self):
        weak_backtest = sample_backtest(trades=20, sharpe=0.2, drawdown=35.0, ret=-10.0)

        payload = report.build_report(sample_metadata(symbol_count=12, raw_local_only=False), weak_backtest, weak_backtest)

        self.assertEqual(payload["summary"]["overall_status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(payload["dataset"]["status"], "FAIL")
        self.assertIn(
            "raw_data_storage_policy_unsafe",
            [item["code"] for item in payload["dataset"]["checks"]],
        )
        self.assertIn(
            "trade_sample_size_too_small",
            [item["code"] for item in payload["backtests"][0]["checks"]],
        )

    def test_missing_legacy_storage_policy_is_warning_not_hard_failure(self):
        metadata = sample_metadata()
        metadata.pop("storage_policy")

        payload = report.build_report(metadata, sample_backtest(), sample_backtest())

        self.assertEqual(payload["summary"]["overall_status"], "RESEARCH_USEFUL_WITH_LIMITATIONS")
        self.assertIn(
            "raw_data_storage_policy_missing",
            [item["code"] for item in payload["dataset"]["checks"]],
        )

    def test_legacy_storage_policy_without_raw_lake_contract_warns(self):
        metadata = sample_metadata()
        metadata["storage_policy"] = {
            "raw_data_local_only": True,
            "commit_raw_csv_to_git": False,
            "copy_to_server_by_default": False,
        }

        payload = report.build_report(metadata, sample_backtest(), sample_backtest())
        codes = [item["code"] for item in payload["dataset"]["checks"]]

        self.assertEqual(payload["summary"]["overall_status"], "RESEARCH_USEFUL_WITH_LIMITATIONS")
        self.assertIn("raw_lake_summary_boundary_missing", codes)
        self.assertIn("raw_lake_manifest_contract_incomplete", codes)

    def test_main_writes_report_without_fetching_or_requiring_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata_path = os.path.join(tmp, "metadata.json")
            realistic_path = os.path.join(tmp, "realistic.json")
            combined_path = os.path.join(tmp, "combined.json")
            output_path = os.path.join(tmp, "report.json")
            with open(metadata_path, "w", encoding="utf-8") as handle:
                json.dump(sample_metadata(), handle)
            with open(realistic_path, "w", encoding="utf-8") as handle:
                json.dump(sample_backtest(), handle)
            with open(combined_path, "w", encoding="utf-8") as handle:
                json.dump(sample_backtest(include_market=False), handle)

            rc = report.main(
                [
                    "--metadata-file",
                    metadata_path,
                    "--realistic-result-file",
                    realistic_path,
                    "--combined-result-file",
                    combined_path,
                    "--output",
                    output_path,
                ]
            )

            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(output_path))
            with open(output_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertTrue(payload["source"]["uses_credentials"] is False)
            self.assertEqual(payload["source"]["source_files"]["metadata_file"], os.path.abspath(metadata_path))

    def test_load_json_accepts_legacy_windows_encoded_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy.json")
            with open(path, "w", encoding="cp950") as handle:
                json.dump({"reason": "止損"}, handle, ensure_ascii=False)

            payload = report.load_json(path)

            self.assertEqual(payload["reason"], "止損")


if __name__ == "__main__":
    unittest.main()
