import json
import tempfile
import unittest
from pathlib import Path

from scripts import watchlist_diff_report as report


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class WatchlistDiffReportTests(unittest.TestCase):
    def test_diff_report_explains_removals_with_universe_blockers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live = root / "live.json"
            candidate = root / "candidate.json"
            universe = root / "universe.json"
            write_json(
                live,
                {
                    "schema": "rt_signal_watchlist_v1",
                    "markets": {
                        "HK": {"symbols": ["00700", "03988"]},
                        "US": {"symbols": ["AAPL"]},
                    },
                },
            )
            write_json(
                candidate,
                {
                    "schema": "rt_signal_watchlist_v1",
                    "source": {"manual_review_required": True, "auto_applied": False},
                    "markets": {
                        "HK": {"symbols": ["03988", "06690"]},
                        "US": {"symbols": ["AAPL"]},
                    },
                },
            )
            write_json(
                universe,
                {
                    "schema": "universe_rank_report_v1",
                    "markets": {
                        "HK": {
                            "top_ranked": [
                                {
                                    "symbol": "00700",
                                    "universe_score": 82.0,
                                    "include_candidate": False,
                                    "sim_tradability": "allocation_below_one_lot",
                                    "min_lot_notional_hkd": 46180.0,
                                    "sim_max_alloc_hkd": 10000.0,
                                    "blockers": ["sim_allocation_below_one_lot"],
                                },
                                {
                                    "symbol": "06690",
                                    "universe_score": 77.0,
                                    "include_candidate": True,
                                    "sim_tradability": "allocation_tradable",
                                    "blockers": [],
                                },
                            ]
                        }
                    },
                },
            )

            payload = report.build_report(str(live), str(candidate), str(universe))

        hk = payload["markets"]["HK"]
        self.assertEqual(payload["schema"], "watchlist_diff_report_v1")
        self.assertTrue(payload["source"]["read_only"])
        self.assertTrue(payload["source"]["manual_review_required"])
        self.assertFalse(payload["source"]["auto_applies_watchlist"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertEqual(hk["add_symbols"], ["06690"])
        self.assertEqual(hk["remove_symbols"], ["00700"])
        self.assertEqual(hk["remove_blocker_counts"]["sim_allocation_below_one_lot"], 1)
        self.assertEqual(hk["remove_context"][0]["sim_tradability"], "allocation_below_one_lot")
        self.assertEqual(payload["proposal"]["schema"], "rt_signal_watchlist_change_proposal_v1")
        self.assertFalse(payload["proposal"]["source"]["auto_applied"])
        self.assertTrue(payload["proposal"]["source"]["manual_review_required"])
        self.assertTrue(payload["proposal"]["source"]["does_not_restart_services"])
        self.assertTrue(payload["proposal"]["source"]["does_not_submit_orders"])
        self.assertEqual(payload["proposal"]["markets"]["HK"]["add_symbols"], ["06690"])
        self.assertEqual(payload["proposal"]["markets"]["HK"]["remove_symbols"], ["00700"])
        self.assertRegex(payload["proposal"]["proposal_hash"], r"^[0-9a-f]{16}$")
        self.assertIn("HK:review_removing_sim_allocation_below_one_lot_symbols", payload["recommendations"])
        self.assertIn("HK:manual_review_1_candidate_additions", payload["recommendations"])
        self.assertEqual(payload["markets"]["US"]["unchanged_symbols"], ["AAPL"])

    def test_matching_watchlists_get_clean_recommendation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live = root / "live.json"
            candidate = root / "candidate.json"
            universe = root / "universe.json"
            payload = {"schema": "rt_signal_watchlist_v1", "markets": {"HK": {"symbols": ["03988"]}}}
            write_json(live, payload)
            write_json(candidate, payload)
            write_json(universe, {"schema": "universe_rank_report_v1", "markets": {}})

            result = report.build_report(str(live), str(candidate), str(universe))

        self.assertEqual(result["recommendations"], ["watchlist_candidate_matches_live_watchlist"])
        self.assertEqual(result["markets"]["HK"]["add_count"], 0)
        self.assertEqual(result["markets"]["HK"]["remove_count"], 0)

    def test_diff_uses_full_ranked_symbols_when_symbol_not_in_top_ranked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live = root / "live.json"
            candidate = root / "candidate.json"
            universe = root / "universe.json"
            write_json(live, {"schema": "rt_signal_watchlist_v1", "markets": {"HK": {"symbols": ["00005"]}}})
            write_json(candidate, {"schema": "rt_signal_watchlist_v1", "markets": {"HK": {"symbols": []}}})
            write_json(
                universe,
                {
                    "schema": "universe_rank_report_v1",
                    "markets": {
                        "HK": {
                            "top_ranked": [],
                            "ranked_symbols": [
                                {
                                    "symbol": "00005",
                                    "universe_score": 31.0,
                                    "include_candidate": False,
                                    "blockers": ["bottom_quartile_liquidity"],
                                }
                            ],
                        }
                    },
                },
            )

            result = report.build_report(str(live), str(candidate), str(universe))

        context = result["markets"]["HK"]["remove_context"][0]
        self.assertEqual(context["symbol"], "00005")
        self.assertEqual(context["universe_score"], 31.0)
        self.assertEqual(context["blockers"], ["bottom_quartile_liquidity"])
        self.assertNotIn("not_in_active_or_ranked_universe", context["blockers"])

    def test_diff_uses_hygiene_context_for_symbols_missing_from_ranked_universe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live = root / "live.json"
            candidate = root / "candidate.json"
            universe = root / "universe.json"
            hygiene = root / "hygiene.json"
            write_json(live, {"schema": "rt_signal_watchlist_v1", "markets": {"HK": {"symbols": ["03333"]}}})
            write_json(candidate, {"schema": "rt_signal_watchlist_v1", "markets": {"HK": {"symbols": []}}})
            write_json(universe, {"schema": "universe_rank_report_v1", "markets": {"HK": {"ranked_symbols": []}}})
            write_json(
                hygiene,
                {
                    "schema": "universe_hygiene_report_v1",
                    "markets": {
                        "HK": {
                            "all_problem_symbols": [
                                {
                                    "symbol": "03333",
                                    "severity": "high",
                                    "recommended_action": "candidate_deactivate_or_symbol_mapping",
                                    "issues": ["latest_kline_stale_ge_30d", "no_history_rows_120d"],
                                    "latest_date": "2024-01-29",
                                    "market_latest_date": "2026-06-12",
                                    "lag_days_vs_market_latest": 865,
                                }
                            ]
                        }
                    },
                },
            )

            result = report.build_report(str(live), str(candidate), str(universe), str(hygiene))

        context = result["markets"]["HK"]["remove_context"][0]
        self.assertEqual(
            context["blockers"],
            ["hygiene:latest_kline_stale_ge_30d", "hygiene:no_history_rows_120d"],
        )
        self.assertEqual(context["hygiene"]["recommended_action"], "candidate_deactivate_or_symbol_mapping")
        self.assertEqual(result["markets"]["HK"]["hygiene_problem_remove_count"], 1)
        self.assertIn(
            "HK:review_removing_universe_hygiene_problem_symbols:1",
            result["recommendations"],
        )

    def test_diff_distinguishes_healthy_active_symbol_missing_from_ranked_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live = root / "live.json"
            candidate = root / "candidate.json"
            universe = root / "universe.json"
            hygiene = root / "hygiene.json"
            write_json(live, {"schema": "rt_signal_watchlist_v1", "markets": {"HK": {"symbols": ["00001"]}}})
            write_json(candidate, {"schema": "rt_signal_watchlist_v1", "markets": {"HK": {"symbols": []}}})
            write_json(universe, {"schema": "universe_rank_report_v1", "markets": {"HK": {"ranked_symbols": []}}})
            write_json(
                hygiene,
                {
                    "schema": "universe_hygiene_report_v1",
                    "markets": {
                        "HK": {
                            "active_symbols": [
                                {
                                    "symbol": "00001",
                                    "severity": "ok",
                                    "recommended_action": "keep_active",
                                    "issues": ["healthy_active_symbol"],
                                }
                            ]
                        }
                    },
                },
            )

            result = report.build_report(str(live), str(candidate), str(universe), str(hygiene))

        context = result["markets"]["HK"]["remove_context"][0]
        self.assertEqual(context["blockers"], ["active_universe_not_ranked"])
        self.assertEqual(context["hygiene"]["recommended_action"], "keep_active")
        self.assertEqual(result["markets"]["HK"]["ranked_coverage"]["active_not_ranked_symbols"], ["00001"])
        self.assertIn(
            "HK:investigate_active_symbols_missing_from_ranked_universe",
            result["recommendations"],
        )
        self.assertIn(
            "HK:ranked_coverage_missing_active_symbols:1",
            result["recommendations"],
        )

    def test_diff_marks_symbol_missing_from_both_ranked_and_active_universe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live = root / "live.json"
            candidate = root / "candidate.json"
            universe = root / "universe.json"
            hygiene = root / "hygiene.json"
            write_json(live, {"schema": "rt_signal_watchlist_v1", "markets": {"US": {"symbols": ["ZZZZ"]}}})
            write_json(candidate, {"schema": "rt_signal_watchlist_v1", "markets": {"US": {"symbols": []}}})
            write_json(universe, {"schema": "universe_rank_report_v1", "markets": {"US": {"ranked_symbols": []}}})
            write_json(hygiene, {"schema": "universe_hygiene_report_v1", "markets": {"US": {"active_symbols": []}}})

            result = report.build_report(str(live), str(candidate), str(universe), str(hygiene))

        context = result["markets"]["US"]["remove_context"][0]
        self.assertEqual(context["blockers"], ["not_in_active_or_ranked_universe"])
        self.assertEqual(
            result["proposal"]["markets"]["US"]["remove_symbols_missing_active_universe"],
            ["ZZZZ"],
        )
        self.assertIn(
            "US:review_live_watchlist_symbols_missing_from_active_universe",
            result["recommendations"],
        )

    def test_ranked_coverage_reports_ranked_symbols_not_in_active_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live = root / "live.json"
            candidate = root / "candidate.json"
            universe = root / "universe.json"
            hygiene = root / "hygiene.json"
            write_json(live, {"schema": "rt_signal_watchlist_v1", "markets": {"US": {"symbols": []}}})
            write_json(candidate, {"schema": "rt_signal_watchlist_v1", "markets": {"US": {"symbols": []}}})
            write_json(
                universe,
                {
                    "schema": "universe_rank_report_v1",
                    "markets": {"US": {"ranked_symbols": [{"symbol": "GHOST"}]}},
                },
            )
            write_json(hygiene, {"schema": "universe_hygiene_report_v1", "markets": {"US": {"active_symbols": []}}})

            result = report.build_report(str(live), str(candidate), str(universe), str(hygiene))

        coverage = result["markets"]["US"]["ranked_coverage"]
        self.assertEqual(coverage["ranked_symbol_count"], 1)
        self.assertEqual(coverage["ranked_not_active_symbols"], ["GHOST"])

    def test_proposal_hash_ignores_generated_at(self):
        markets = {
            "HK": {
                "add_symbols": ["00001"],
                "remove_symbols": ["00700"],
                "add_count": 1,
                "remove_count": 1,
                "remove_context": [{"symbol": "00700", "blockers": ["sim_allocation_below_one_lot"]}],
            }
        }

        first = report.build_proposal(markets, "2026-06-12T10:00:00")
        second = report.build_proposal(markets, "2026-06-12T11:00:00")

        self.assertNotEqual(first["generated_at"], second["generated_at"])
        self.assertEqual(first["proposal_hash"], second["proposal_hash"])


if __name__ == "__main__":
    unittest.main()
