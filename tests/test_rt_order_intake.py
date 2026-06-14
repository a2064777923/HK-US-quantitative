import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from scripts import rt_order_intake as intake


def fresh_alert(signal_id="sig-1", symbol="00700"):
    return {
        "signal_id": signal_id,
        "symbol": symbol,
        "signal_type": "BUY",
        "trigger": "unit-test",
        "confirmed": True,
        "execution_candidate": True,
        "full_score": 0.7,
        "entry_price": 300,
        "stop_loss": 290,
        "take_profit": 330,
        "rr_ratio": 3.0,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def judgment(signal_id, decision="approve", **extra):
    item = {
        "schema": "hermes_trade_judgment_v1",
        "signal_id": signal_id,
        "decision": decision,
        "confidence": 0.8,
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "supporting_factors": ["unit test approval"],
        "opposing_factors": ["none"],
        "risk_notes": ["default risk cap"],
    }
    item.update(extra)
    return item


class RtOrderIntakeTests(unittest.TestCase):
    def setUp(self):
        intake.REQUIRE_HERMES_JUDGMENT = True
        intake.MIN_HERMES_CONFIDENCE = 0.6
        intake.REQUIRE_EXECUTION_READINESS = True
        intake.MAX_READINESS_REPORT_AGE_HOURS = 2
        intake.REQUIRE_STRATEGY_EVIDENCE = True
        intake.REQUIRE_MARKET_CONTEXT = True
        intake.MIN_MARKET_EXCEPTION_CONFIDENCE = 0.8
        self.context = {
            "cash_hkd": 1_000_000,
            "equity_hkd": 1_000_000,
            "positions": {},
        }

    def write_judgments(self, path, *items):
        Path(path).write_text(
            "\n".join(json.dumps(item) for item in items),
            encoding="utf-8",
        )

    def run_with_common_patches(
        self,
        alert,
        mode,
        state,
        state_file,
        judgment_file,
        submit_result=None,
        strategy_gate=(True, {"status": "PASS"}),
        conflict_gate=(True, {"status": "PASS"}),
        market_gate=(True, {"status": "PASS"}),
        readiness_gate=(True, {"status": "PASS"}),
    ):
        patches = [
            patch.object(intake, "health_gate", return_value=(True, {"status": "OK"})),
            patch.object(intake, "execution_readiness_gate", return_value=readiness_gate),
            patch.object(intake, "strategy_evidence_gate", return_value=strategy_gate),
            patch.object(intake, "symbol_conflict_gate", return_value=conflict_gate),
            patch.object(intake, "fetch_context", return_value=("token", self.context, [])),
            patch.object(intake, "market_context_gate", return_value=market_gate),
        ]
        if submit_result is not None:
            patches.append(patch.object(intake, "submit_order", return_value=submit_result))
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            if submit_result is not None:
                with patches[6] as submit:
                    result = intake.process_alert(alert, mode, state, state_file, judgment_file)
                    return result, submit
            return intake.process_alert(alert, mode, state, state_file, judgment_file), None

    def test_dry_run_does_not_consume_signal_for_execute(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-dry-then-execute")

            dry_result, _ = self.run_with_common_patches(
                alert, "dry-run", state, state_file, judgment_file
            )

            self.assertEqual(dry_result["status"], "dry_run")
            self.assertIn(alert["signal_id"], state["dry_runs"])
            self.assertNotIn(alert["signal_id"], state["processed"])

            self.write_judgments(judgment_file, judgment(alert["signal_id"]))
            execute_result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "ok"},
            )

            self.assertEqual(execute_result["status"], "submitted")
            self.assertIn(alert["signal_id"], state["processed"])
            submit.assert_called_once()

    def test_validate_alert_requires_execution_candidate_true(self):
        not_candidate = fresh_alert("sig-not-candidate")
        not_candidate["execution_candidate"] = False
        missing_candidate = fresh_alert("sig-missing-candidate")
        missing_candidate.pop("execution_candidate")

        self.assertIn("not_execution_candidate", intake.validate_alert(not_candidate))
        self.assertIn("not_execution_candidate", intake.validate_alert(missing_candidate))

    def test_execute_requires_matching_hermes_judgment(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "missing.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-missing-judgment")

            result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "should-not-submit"},
            )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("hermes_judgment_gate_failed", result["reasons"])
            submit.assert_not_called()

    def test_execute_requires_strategy_evidence_gate(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-strategy-gate")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))

            result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "should-not-submit"},
                strategy_gate=(
                    False,
                    {
                        "status": "REJECTED",
                        "reasons": ["overall_outcome_sample_below_30"],
                        "would_block_execute": True,
                    },
                ),
            )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("strategy_evidence_gate_failed", result["reasons"])
            self.assertIn("execution_readiness", result)
            self.assertIn("overall_outcome_sample_below_30", result["strategy_evidence"]["reasons"])
            submit.assert_not_called()

    def test_execute_requires_execution_readiness_gate(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-readiness-gate")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))

            result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "should-not-submit"},
                readiness_gate=(
                    False,
                    {
                        "status": "REJECTED",
                        "readiness_status": "BLOCKED",
                        "ready_for_execute": False,
                        "reasons": [
                            "execution_readiness_status_blocked",
                            "execution_readiness_ready_for_execute_false",
                        ],
                        "would_block_execute": True,
                    },
                ),
            )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("execution_readiness_gate_failed", result["reasons"])
            self.assertEqual(result["execution_readiness"]["readiness_status"], "BLOCKED")
            submit.assert_not_called()

    def test_execution_readiness_gate_blocks_execute_when_report_is_blocked(self):
        report = {
            "schema": "execution_readiness_report_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "BLOCKED",
            "ready_for_execute": False,
            "blocking_gates": [
                {
                    "gate": "simulation_performance_attribution",
                    "status": "BLOCK",
                    "detail": "simulation performance report status is FAIL",
                    "data": {"large": "omitted from compact intake payload"},
                }
            ],
            "warning_gates": [],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "readiness.json"
            path.write_text(json.dumps(report), encoding="utf-8")

            ok, payload = intake.execution_readiness_gate("execute", str(path))

        self.assertFalse(ok)
        self.assertEqual(payload["status"], "REJECTED")
        self.assertIn("execution_readiness_status_blocked", payload["reasons"])
        self.assertIn("execution_readiness_ready_for_execute_false", payload["reasons"])
        self.assertEqual(payload["blocking_gates"][0]["gate"], "simulation_performance_attribution")
        self.assertNotIn("data", payload["blocking_gates"][0])

    def test_execution_readiness_gate_dry_run_reports_would_block_execute(self):
        report = {
            "schema": "execution_readiness_report_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "WARN",
            "ready_for_execute": False,
            "blocking_gates": [],
            "warning_gates": [
                {
                    "gate": "source_reliability",
                    "status": "WARN",
                    "detail": "source reliability is DEGRADED",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "readiness.json"
            path.write_text(json.dumps(report), encoding="utf-8")

            ok, payload = intake.execution_readiness_gate("dry-run", str(path))

        self.assertTrue(ok)
        self.assertEqual(payload["status"], "DRY_RUN_ONLY")
        self.assertTrue(payload["would_block_execute"])
        self.assertIn("execution_readiness_status_warn", payload["reasons"])
        self.assertEqual(payload["warning_gates"][0]["gate"], "source_reliability")

    def test_execute_requires_market_context_gate(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-market-gate")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))

            result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "should-not-submit"},
                market_gate=(
                    False,
                    {
                        "status": "REJECTED",
                        "reasons": ["market_regime_risk_off"],
                        "would_block_execute": True,
                    },
                ),
            )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("market_context_gate_failed", result["reasons"])
            self.assertIn("market_regime_risk_off", result["market_context"]["reasons"])
            submit.assert_not_called()

    def test_execute_requires_symbol_conflict_gate(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-conflict-gate")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))

            result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "should-not-submit"},
                conflict_gate=(
                    False,
                    {
                        "status": "REJECTED",
                        "reasons": ["symbol_conflict_opposite_direction_in_queue"],
                        "would_block_execute": True,
                    },
                ),
            )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("symbol_conflict_gate_failed", result["reasons"])
            self.assertIn("symbol_conflict_opposite_direction_in_queue", result["symbol_conflict"]["reasons"])
            submit.assert_not_called()

    def test_symbol_conflict_gate_blocks_execute_for_current_scope_opposite_alert(self):
        current = fresh_alert("sig-current", "AAPL")
        current.update({"market": "US", "strategy_config_id": "cfg", "watchlist_id": "wl"})
        opposite = fresh_alert("sig-opposite", "AAPL")
        opposite.update(
            {
                "market": "US",
                "signal_type": "SELL",
                "stop_loss": 310,
                "take_profit": 270,
                "strategy_config_id": "cfg",
                "watchlist_id": "wl",
            }
        )
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            queue.write_text(
                "\n".join(json.dumps(item) for item in (opposite, current)),
                encoding="utf-8",
            )

            ok, payload = intake.symbol_conflict_gate(current, "execute", str(queue))

        self.assertFalse(ok)
        self.assertEqual(payload["status"], "REJECTED")
        self.assertIn("symbol_conflict_opposite_direction_in_queue", payload["reasons"])
        self.assertEqual(payload["opposite_count"], 1)
        self.assertEqual(payload["opposite_alerts"][0]["signal_id"], "sig-opposite")

    def test_symbol_conflict_gate_ignores_other_strategy_scope(self):
        current = fresh_alert("sig-current", "AAPL")
        current.update({"market": "US", "strategy_config_id": "cfg-current", "watchlist_id": "wl"})
        opposite = fresh_alert("sig-opposite", "AAPL")
        opposite.update(
            {
                "market": "US",
                "signal_type": "SELL",
                "stop_loss": 310,
                "take_profit": 270,
                "strategy_config_id": "cfg-old",
                "watchlist_id": "wl",
            }
        )
        with tempfile.TemporaryDirectory() as td:
            queue = Path(td) / "alerts.jsonl"
            queue.write_text(
                "\n".join(json.dumps(item) for item in (opposite, current)),
                encoding="utf-8",
            )

            ok, payload = intake.symbol_conflict_gate(current, "execute", str(queue))

        self.assertTrue(ok)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["opposite_count"], 0)

    def test_strategy_evidence_gate_blocks_execute_when_sample_is_pending(self):
        report = {
            "schema": "rt_signal_outcome_report_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "overall": {
                "horizons": {
                    "1d": {
                        "resolved_count": 0,
                        "pending_count": 40,
                        "avg_signed_close_return_pct": None,
                        "win_rate_pct": 0,
                    }
                }
            },
            "by_trigger": [],
            "recommendations": ["outcome_sample_not_ready_keep_collecting_daily_klines"],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "outcome.json"
            path.write_text(json.dumps(report), encoding="utf-8")

            ok, payload = intake.strategy_evidence_gate(fresh_alert("sig-pending"), "execute", str(path))

        self.assertFalse(ok)
        self.assertEqual(payload["status"], "REJECTED")
        self.assertIn("overall_outcome_sample_below_30", payload["reasons"])
        self.assertIn("trigger_outcome_missing", payload["reasons"])

    def test_strategy_evidence_gate_passes_with_positive_overall_and_trigger_sample(self):
        report = {
            "schema": "rt_signal_outcome_report_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "overall": {
                "horizons": {
                    "1d": {
                        "resolved_count": 35,
                        "avg_signed_close_return_pct": 0.42,
                        "win_rate_pct": 54.3,
                    }
                }
            },
            "by_trigger": [
                {
                    "key": "BUY:unit-test",
                    "horizons": {
                        "1d": {
                            "resolved_count": 6,
                            "avg_signed_close_return_pct": 0.31,
                            "win_rate_pct": 50.0,
                        }
                    },
                }
            ],
            "recommendations": ["continue_shadow_observation_before_enabling_alert_sim"],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "outcome.json"
            path.write_text(json.dumps(report), encoding="utf-8")

            ok, payload = intake.strategy_evidence_gate(fresh_alert("sig-pass"), "execute", str(path))

        self.assertTrue(ok)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["reasons"], [])

    def test_strategy_evidence_gate_blocks_mixed_diagnostic_outcomes_without_executable_cohort(self):
        report = {
            "schema": "rt_signal_outcome_report_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "downgraded_directional_alert_count": 4,
            "counts": {"downgraded_directional_alert_count": 4},
            "overall": {
                "horizons": {
                    "1d": {
                        "resolved_count": 35,
                        "avg_signed_close_return_pct": 0.42,
                        "win_rate_pct": 54.3,
                    }
                }
            },
            "by_trigger": [
                {
                    "key": "BUY:unit-test",
                    "horizons": {
                        "1d": {
                            "resolved_count": 6,
                            "avg_signed_close_return_pct": 0.31,
                            "win_rate_pct": 50.0,
                        }
                    },
                }
            ],
            "recommendations": ["downgraded_candidate_outcomes_are_research_only"],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "outcome.json"
            path.write_text(json.dumps(report), encoding="utf-8")

            ok, payload = intake.strategy_evidence_gate(fresh_alert("sig-mixed"), "execute", str(path))

        self.assertFalse(ok)
        self.assertEqual(payload["status"], "REJECTED")
        self.assertEqual(payload["evidence_metric_scope"], "execution_candidate")
        self.assertTrue(payload["execution_candidate_evidence_required"])
        self.assertEqual(payload["diagnostic_candidate_outcome_count"], 4)
        self.assertIn(
            "strategy_evidence_includes_diagnostic_candidates_without_executable_cohort",
            payload["reasons"],
        )
        self.assertIn("strategy_evidence_horizon_missing_1d", payload["reasons"])
        self.assertEqual(payload["all_candidate_overall_metric"]["resolved_count"], 35)

    def test_strategy_evidence_gate_uses_executable_cohort_when_diagnostic_outcomes_exist(self):
        report = {
            "schema": "rt_signal_outcome_report_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "counts": {"downgraded_directional_alert_count": 2},
            "overall": {
                "horizons": {
                    "1d": {
                        "resolved_count": 80,
                        "avg_signed_close_return_pct": 0.18,
                        "win_rate_pct": 52.5,
                    }
                }
            },
            "execution_candidate": {
                "overall": {
                    "horizons": {
                        "1d": {
                            "resolved_count": 34,
                            "avg_signed_close_return_pct": 0.39,
                            "win_rate_pct": 55.9,
                        }
                    }
                },
                "by_trigger": [
                    {
                        "key": "BUY:unit-test",
                        "horizons": {
                            "1d": {
                                "resolved_count": 7,
                                "avg_signed_close_return_pct": 0.27,
                                "win_rate_pct": 57.1,
                            }
                        },
                    }
                ],
            },
            "by_trigger": [
                {
                    "key": "BUY:unit-test",
                    "horizons": {
                        "1d": {
                            "resolved_count": 14,
                            "avg_signed_close_return_pct": 0.12,
                            "win_rate_pct": 50.0,
                        }
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "outcome.json"
            path.write_text(json.dumps(report), encoding="utf-8")

            ok, payload = intake.strategy_evidence_gate(fresh_alert("sig-executable"), "execute", str(path))

        self.assertTrue(ok)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["reasons"], [])
        self.assertEqual(payload["evidence_metric_scope"], "execution_candidate")
        self.assertEqual(payload["overall_metric_source"], "execution_candidate.overall")
        self.assertEqual(payload["trigger_metric_source"], "execution_candidate.by_trigger")
        self.assertEqual(payload["overall_metric"]["resolved_count"], 34)
        self.assertEqual(payload["all_candidate_overall_metric"]["resolved_count"], 80)

    def test_market_context_gate_blocks_risk_off_buy_without_exception(self):
        report = {
            "schema": "market_context_report_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "markets": {
                "HK": {
                    "regime": "risk_off",
                    "risk_level": "medium",
                    "latest_date": "2026-06-11",
                    "breadth": {"above_ma20_pct": 18.0},
                    "returns": {"avg_5d_pct": -3.5},
                    "risk": {"avg_volatility_20d_pct": 2.8},
                    "v4_signal_summary": {"by_side": {"BUY": 17}},
                    "notes": ["buy_signals_against_weak_breadth"],
                }
            },
            "recommendations": ["HK:risk_off_require_reduced_or_rejected_new_buys"],
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "market.json"
            path.write_text(json.dumps(report), encoding="utf-8")

            ok, payload = intake.market_context_gate(
                fresh_alert("sig-market-block"),
                {"side": "buy"},
                "execute",
                {"judgment": judgment("sig-market-block")},
                str(path),
            )

        self.assertFalse(ok)
        self.assertEqual(payload["status"], "REJECTED")
        self.assertIn("market_regime_risk_off", payload["reasons"])
        self.assertIn("missing_market_regime_exception", payload["reasons"])

    def test_market_context_gate_allows_documented_high_confidence_exception(self):
        report = {
            "schema": "market_context_report_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "markets": {
                "HK": {
                    "regime": "risk_off",
                    "risk_level": "medium",
                    "latest_date": "2026-06-11",
                    "breadth": {"above_ma20_pct": 18.0},
                    "returns": {"avg_5d_pct": -3.5},
                    "risk": {"avg_volatility_20d_pct": 2.8},
                    "v4_signal_summary": {"by_side": {"BUY": 17}},
                    "notes": ["buy_signals_against_weak_breadth"],
                }
            },
            "recommendations": ["HK:risk_off_require_reduced_or_rejected_new_buys"],
        }
        approved = judgment(
            "sig-market-pass",
            confidence=0.85,
            market_regime_exception=True,
            market_regime_exception_reason="Company-specific catalyst offsets weak breadth for a reduced probe position.",
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "market.json"
            path.write_text(json.dumps(report), encoding="utf-8")

            ok, payload = intake.market_context_gate(
                fresh_alert("sig-market-pass"),
                {"side": "buy"},
                "execute",
                {"judgment": approved},
                str(path),
            )

        self.assertTrue(ok)
        self.assertEqual(payload["status"], "PASS")
        self.assertTrue(payload["exception_accepted"])
        self.assertEqual(payload["reasons"], [])

    def test_reduce_judgment_respects_hk_lot_size(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-reduce")
            self.write_judgments(judgment_file, judgment(alert["signal_id"], "reduce", max_quantity=150))

            result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "reduced"},
            )

            self.assertEqual(result["status"], "submitted")
            self.assertEqual(result["plan"]["quantity"], 100)
            self.assertEqual(result["plan"]["hermes_reduced_from"], 300)
            submit.assert_called_once()
            self.assertEqual(submit.call_args.args[3], 100)

    def test_reduce_below_one_lot_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-reduce-zero")
            self.write_judgments(judgment_file, judgment(alert["signal_id"], "reduce", max_quantity=50))

            result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "should-not-submit"},
            )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("hermes_judgment_gate_failed", result["reasons"])
            self.assertIn("reduced_quantity_zero", result["hermes"]["reasons"])
            submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
