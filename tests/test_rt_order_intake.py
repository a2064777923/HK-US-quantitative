import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

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


def fresh_sell_alert(signal_id="sig-sell", symbol="00700"):
    alert = fresh_alert(signal_id, symbol)
    alert.update(
        {
            "signal_type": "SELL",
            "full_score": -0.7,
            "entry_price": 300,
            "stop_loss": 330,
            "take_profit": 270,
        }
    )
    return alert


def judgment(signal_id, decision="approve", **extra):
    item = {
        "schema": "hermes_trade_judgment_v1",
        "packet_id": "packet-test",
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


def hermes_packet(signal_id, eligible=True, **extra):
    item = {
        "signal_id": signal_id,
        "eligible_for_approval": eligible,
        "recommended_judgment": "approve_or_reduce_allowed_after_llm_review" if eligible else "reject_or_hold",
        "blocking_reasons": [] if eligible else ["intraday_signal_evidence_challenges_signal"],
        "alert": {"signal_id": signal_id},
    }
    item.update(extra.pop("review_item", {}))
    packet = {
        "schema": "hermes_signal_review_packet_v1",
        "packet_id": "packet-test",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "review_items": [item],
        "execution_readiness": {
            "schema": "execution_readiness_report_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "READY",
            "ready_for_execute": True,
        },
    }
    packet.update(extra)
    return packet


class RtOrderIntakeTests(unittest.TestCase):
    def setUp(self):
        intake.REQUIRE_HERMES_JUDGMENT = True
        intake.REQUIRE_HERMES_PACKET_ELIGIBLE = True
        intake.MIN_HERMES_CONFIDENCE = 0.6
        intake.MAX_HERMES_PACKET_AGE_MINUTES = 30
        intake.REQUIRE_EXECUTION_READINESS = True
        intake.MAX_READINESS_REPORT_AGE_HOURS = 2
        intake.REQUIRE_STRATEGY_EVIDENCE = True
        intake.REQUIRE_MARKET_CONTEXT = True
        intake.MIN_MARKET_EXCEPTION_CONFIDENCE = 0.8
        intake.PILOT_EXECUTION_ENABLED = True
        intake.PILOT_MAX_ORDER_NOTIONAL_HKD = 500_000
        intake.PILOT_MAX_ORDER_RISK_HKD = 50_000
        intake.PILOT_MAX_DAILY_SUBMITTED_ORDERS = 10
        intake.PILOT_ALLOWED_MARKETS = {"HK", "US"}
        intake.US_ORDER_BROKER = "quantmind-sim"
        intake.ALPACA_API_KEY_ID = ""
        intake.ALPACA_API_SECRET_KEY = ""
        intake.ALPACA_TRADING_BASE_URL = "https://paper-api.alpaca.markets/v2"
        self.context = {
            "cash_hkd": 1_000_000,
            "equity_hkd": 1_000_000,
            "positions": {},
            "broker_context": {"backend": "quantmind-sim"},
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
        broker_reconciliation_gate=(True, {"status": "PASS"}),
        hermes_packet_gate=(True, {"status": "PASS", "packet_id": "packet-test"}),
        context_result=None,
        pilot_enabled=None,
        pilot_notional_cap=None,
        pilot_risk_cap=None,
        pilot_daily_cap=None,
        pilot_markets=None,
    ):
        context_result = context_result or ("token", self.context, [])
        if pilot_enabled is not None:
            intake.PILOT_EXECUTION_ENABLED = pilot_enabled
        if pilot_notional_cap is not None:
            intake.PILOT_MAX_ORDER_NOTIONAL_HKD = pilot_notional_cap
        if pilot_risk_cap is not None:
            intake.PILOT_MAX_ORDER_RISK_HKD = pilot_risk_cap
        if pilot_daily_cap is not None:
            intake.PILOT_MAX_DAILY_SUBMITTED_ORDERS = pilot_daily_cap
        if pilot_markets is not None:
            intake.PILOT_ALLOWED_MARKETS = set(pilot_markets)
        patches = [
            patch.object(intake, "health_gate", return_value=(True, {"status": "OK"})),
            patch.object(intake, "execution_readiness_gate", return_value=readiness_gate),
            patch.object(intake, "order_intake_broker_reconciliation_gate", return_value=broker_reconciliation_gate),
            patch.object(intake, "strategy_evidence_gate", return_value=strategy_gate),
            patch.object(intake, "symbol_conflict_gate", return_value=conflict_gate),
            patch.object(intake, "fetch_context_for_backend", return_value=context_result),
            patch.object(intake, "market_context_gate", return_value=market_gate),
        ]
        if hermes_packet_gate is not None:
            patches.append(patch.object(intake, "hermes_packet_gate", return_value=hermes_packet_gate))
        with ExitStack() as stack:
            submit = None
            for item in patches:
                stack.enter_context(item)
            if submit_result is not None:
                submit = stack.enter_context(patch.object(intake, "submit_order", return_value=submit_result))
            result = intake.process_alert(alert, mode, state, state_file, judgment_file)
            if submit_result is not None:
                return result, submit
            return result, None

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

    def test_state_lock_preserves_concurrent_submitted_and_dry_run_updates(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            alert = fresh_alert("sig-lock-race")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))
            Path(state_file).write_text(
                json.dumps(
                    {
                        "processed": {
                            alert["signal_id"]: {
                                "signal_id": alert["signal_id"],
                                "status": "submitted",
                                "submitted_at": "2026-06-18T03:00:00",
                                "order_result": {"order_id": "submitted-ok"},
                            }
                        },
                        "dry_runs": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            stale_state = {"processed": {}, "dry_runs": {}}

            with patch.object(intake, "health_gate", return_value=(True, {"status": "OK"})), \
                patch.object(intake, "execution_readiness_gate", return_value=(True, {"status": "PASS"})), \
                patch.object(intake, "strategy_evidence_gate", return_value=(True, {"status": "PASS"})), \
                patch.object(intake, "symbol_conflict_gate", return_value=(True, {"status": "PASS"})), \
                patch.object(intake, "fetch_context_for_backend", return_value=("token", self.context, [])), \
                patch.object(intake, "market_context_gate", return_value=(True, {"status": "PASS"})):
                dry_result = intake.process_alert(
                    alert,
                    "dry-run",
                    stale_state,
                    state_file,
                    judgment_file,
                )

            self.assertEqual(dry_result["status"], "duplicate")
            self.assertNotIn(alert["signal_id"], stale_state.get("dry_runs", {}))
            state_after = intake.load_state(state_file)
            self.assertEqual(state_after["processed"][alert["signal_id"]]["status"], "submitted")
            self.assertNotIn(alert["signal_id"], state_after["dry_runs"])

    def test_validate_alert_requires_execution_candidate_true(self):
        not_candidate = fresh_alert("sig-not-candidate")
        not_candidate["execution_candidate"] = False
        missing_candidate = fresh_alert("sig-missing-candidate")
        missing_candidate.pop("execution_candidate")

        self.assertIn("not_execution_candidate", intake.validate_alert(not_candidate))
        self.assertIn("not_execution_candidate", intake.validate_alert(missing_candidate))

    def test_save_json_atomic_uses_unique_temp_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            temp_names = []

            original_mkstemp = intake.tempfile.mkstemp

            def tracked_mkstemp(*args, **kwargs):
                fd, tmp = original_mkstemp(*args, **kwargs)
                temp_names.append(tmp)
                return fd, tmp

            with patch.object(intake.tempfile, "mkstemp", side_effect=tracked_mkstemp):
                intake.save_json_atomic(str(path), {"id": str(uuid4())})

            self.assertTrue(path.exists())
            self.assertEqual(len(temp_names), 1)
            self.assertNotEqual(temp_names[0], str(path) + ".tmp")
            self.assertFalse(Path(temp_names[0]).exists())

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

    def test_execute_requires_matching_eligible_hermes_packet_item(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            packet_file = Path(td) / "packet.json"
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-packet-block")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))
            packet_file.write_text(
                json.dumps(
                    hermes_packet(
                        alert["signal_id"],
                        eligible=False,
                        review_item={
                            "blocking_reasons": [
                                "intraday_signal_evidence_challenges_signal",
                                "intraday_challenge:latest_15m_down",
                            ]
                        },
                    )
                ),
                encoding="utf-8",
            )

            with patch.object(intake, "HERMES_REVIEW_PACKET_FILE", str(packet_file)):
                result, submit = self.run_with_common_patches(
                    alert,
                    "execute",
                    state,
                    state_file,
                    judgment_file,
                    submit_result={"order_id": "should-not-submit"},
                    hermes_packet_gate=None,
                )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("hermes_packet_gate_failed", result["reasons"])
            self.assertIn("hermes_review_item_not_eligible", result["hermes_packet"]["reasons"])
            self.assertIn(
                "hermes:intraday_signal_evidence_challenges_signal",
                result["hermes_packet"]["reasons"],
            )
            submit.assert_not_called()

    def test_execute_rejects_duplicate_hermes_packet_review_items_for_signal(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            packet_file = Path(td) / "packet.json"
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-packet-duplicate")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))
            payload = hermes_packet(alert["signal_id"], eligible=True)
            duplicate = dict(payload["review_items"][0])
            duplicate["eligible_for_approval"] = False
            duplicate["blocking_reasons"] = ["duplicate-conflicting-review-item"]
            payload["review_items"].append(duplicate)
            packet_file.write_text(json.dumps(payload), encoding="utf-8")

            with patch.object(intake, "HERMES_REVIEW_PACKET_FILE", str(packet_file)):
                result, submit = self.run_with_common_patches(
                    alert,
                    "execute",
                    state,
                    state_file,
                    judgment_file,
                    submit_result={"order_id": "should-not-submit"},
                    hermes_packet_gate=None,
                )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("hermes_packet_gate_failed", result["reasons"])
            self.assertIn("hermes_packet_duplicate_review_items", result["hermes_packet"]["reasons"])
            self.assertEqual(result["hermes_packet"]["duplicate_review_item_count"], 2)
            submit.assert_not_called()

    def test_execute_blocks_when_hermes_packet_missing(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            packet_file = Path(td) / "missing-packet.json"
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-packet-missing")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))

            with patch.object(intake, "HERMES_REVIEW_PACKET_FILE", str(packet_file)):
                result, submit = self.run_with_common_patches(
                    alert,
                    "execute",
                    state,
                    state_file,
                    judgment_file,
                    submit_result={"order_id": "should-not-submit"},
                    hermes_packet_gate=None,
                )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("hermes_packet_gate_failed", result["reasons"])
            self.assertIn("hermes_packet_missing_or_invalid", result["hermes_packet"]["reasons"])
            submit.assert_not_called()

    def test_execute_allows_eligible_hermes_packet_and_judgment(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            packet_file = Path(td) / "packet.json"
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-packet-pass")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))
            packet_file.write_text(json.dumps(hermes_packet(alert["signal_id"])), encoding="utf-8")

            with patch.object(intake, "HERMES_REVIEW_PACKET_FILE", str(packet_file)):
                result, submit = self.run_with_common_patches(
                    alert,
                    "execute",
                    state,
                    state_file,
                    judgment_file,
                    submit_result={"order_id": "packet-ok"},
                    hermes_packet_gate=None,
                )

            self.assertEqual(result["status"], "submitted")
            self.assertEqual(result["hermes_packet"]["status"], "PASS")
            submit.assert_called_once()

    def test_execute_requires_judgment_packet_id_to_match_current_eligible_packet(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            packet_file = Path(td) / "packet.json"
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-packet-id-mismatch")
            self.write_judgments(
                judgment_file,
                judgment(alert["signal_id"], packet_id="old-packet"),
            )
            packet_file.write_text(
                json.dumps(hermes_packet(alert["signal_id"], packet_id="current-packet")),
                encoding="utf-8",
            )

            with patch.object(intake, "HERMES_REVIEW_PACKET_FILE", str(packet_file)):
                result, submit = self.run_with_common_patches(
                    alert,
                    "execute",
                    state,
                    state_file,
                    judgment_file,
                    submit_result={"order_id": "should-not-submit"},
                    hermes_packet_gate=None,
                )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("hermes_judgment_gate_failed", result["reasons"])
            self.assertIn("judgment_packet_id_mismatch", result["hermes"]["reasons"])
            self.assertEqual(result["hermes_packet"]["packet_id"], "current-packet")
            submit.assert_not_called()

    def test_execute_requires_judgment_packet_id_when_packet_gate_is_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            packet_file = Path(td) / "packet.json"
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-packet-id-missing")
            approved = judgment(alert["signal_id"])
            approved.pop("packet_id")
            self.write_judgments(judgment_file, approved)
            packet_file.write_text(json.dumps(hermes_packet(alert["signal_id"])), encoding="utf-8")

            with patch.object(intake, "HERMES_REVIEW_PACKET_FILE", str(packet_file)):
                result, submit = self.run_with_common_patches(
                    alert,
                    "execute",
                    state,
                    state_file,
                    judgment_file,
                    submit_result={"order_id": "should-not-submit"},
                    hermes_packet_gate=None,
                )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("hermes_judgment_gate_failed", result["reasons"])
            self.assertIn("judgment_missing_packet_id", result["hermes"]["reasons"])
            submit.assert_not_called()

    def test_execute_rejects_judgment_with_mismatched_reviewed_alert_identity(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            packet_file = Path(td) / "packet.json"
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-reviewed-identity", symbol="AAPL")
            self.write_judgments(
                judgment_file,
                judgment(
                    alert["signal_id"],
                    reviewed_symbol="MSFT",
                    reviewed_signal_type="SELL",
                    reviewed_trigger="other-trigger",
                ),
            )
            packet_file.write_text(json.dumps(hermes_packet(alert["signal_id"])), encoding="utf-8")

            with patch.object(intake, "HERMES_REVIEW_PACKET_FILE", str(packet_file)):
                result, submit = self.run_with_common_patches(
                    alert,
                    "execute",
                    state,
                    state_file,
                    judgment_file,
                    submit_result={"order_id": "should-not-submit"},
                    hermes_packet_gate=None,
                )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("hermes_judgment_gate_failed", result["reasons"])
            self.assertIn("judgment_reviewed_symbol_mismatch", result["hermes"]["reasons"])
            self.assertIn("judgment_reviewed_signal_type_mismatch", result["hermes"]["reasons"])
            self.assertIn("judgment_reviewed_trigger_mismatch", result["hermes"]["reasons"])
            submit.assert_not_called()

    def test_dry_run_reports_hermes_packet_would_block_execute_without_rejecting(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            packet_file = Path(td) / "packet.json"
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-packet-dry-run")
            packet_file.write_text(
                json.dumps(hermes_packet(alert["signal_id"], eligible=False)),
                encoding="utf-8",
            )

            with patch.object(intake, "HERMES_REVIEW_PACKET_FILE", str(packet_file)):
                result, _submit = self.run_with_common_patches(
                    alert,
                    "dry-run",
                    state,
                    state_file,
                    judgment_file,
                    hermes_packet_gate=None,
                )

            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["hermes_packet"]["status"], "DRY_RUN_ONLY")
            self.assertTrue(result["hermes_packet"]["would_block_execute"])
            self.assertIn("hermes_review_item_not_eligible", result["hermes_packet"]["reasons"])

    def test_dry_run_reports_duplicate_hermes_packet_review_items_would_block_execute(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            packet_file = Path(td) / "packet.json"
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-packet-duplicate-dry")
            payload = hermes_packet(alert["signal_id"], eligible=True)
            payload["review_items"].append(dict(payload["review_items"][0]))
            packet_file.write_text(json.dumps(payload), encoding="utf-8")

            with patch.object(intake, "HERMES_REVIEW_PACKET_FILE", str(packet_file)):
                result, _submit = self.run_with_common_patches(
                    alert,
                    "dry-run",
                    state,
                    state_file,
                    judgment_file,
                    hermes_packet_gate=None,
                )

            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["hermes_packet"]["status"], "DRY_RUN_ONLY")
            self.assertTrue(result["hermes_packet"]["would_block_execute"])
            self.assertIn("hermes_packet_duplicate_review_items", result["hermes_packet"]["reasons"])

    def test_hermes_packet_gate_rejects_stale_packet(self):
        alert = fresh_alert("sig-stale-packet")
        old_generated_at = (datetime.now() - timedelta(minutes=45)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as td:
            packet_file = Path(td) / "packet.json"
            packet_file.write_text(
                json.dumps(hermes_packet(alert["signal_id"], generated_at=old_generated_at)),
                encoding="utf-8",
            )

            ok, payload = intake.hermes_packet_gate(alert, "execute", str(packet_file))

        self.assertFalse(ok)
        self.assertEqual(payload["status"], "REJECTED")
        self.assertIn("hermes_packet_stale", payload["reasons"])

    def test_hermes_packet_gate_allows_reviewed_sell_risk_reduction_blockers_only_when_fresh(self):
        alert = fresh_sell_alert("sig-sell-packet-risk-reduction")
        blockers = [
            "execution_readiness_would_block_execute",
            "execution_readiness:execution_readiness_status_blocked",
            "strategy_evidence_would_block_execute",
            "strategy_evidence:overall_outcome_sample_below_30",
        ]
        with tempfile.TemporaryDirectory() as td:
            packet_file = Path(td) / "packet.json"
            packet_file.write_text(
                json.dumps(
                    hermes_packet(
                        alert["signal_id"],
                        eligible=False,
                        review_item={"blocking_reasons": blockers},
                    )
                ),
                encoding="utf-8",
            )

            ok, payload = intake.hermes_packet_gate(alert, "execute", str(packet_file))

        self.assertTrue(ok)
        self.assertEqual(payload["status"], "RISK_REDUCTION_REVIEW_ALLOWED")
        self.assertEqual(payload["blocking_reasons"], blockers)

    def test_stale_packet_does_not_allow_sell_risk_reduction_exception(self):
        alert = fresh_sell_alert("sig-sell-stale-packet")
        old_generated_at = (datetime.now() - timedelta(minutes=45)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as td:
            packet_file = Path(td) / "packet.json"
            packet_file.write_text(
                json.dumps(
                    hermes_packet(
                        alert["signal_id"],
                        eligible=False,
                        generated_at=old_generated_at,
                        review_item={"blocking_reasons": ["execution_readiness_would_block_execute"]},
                    )
                ),
                encoding="utf-8",
            )

            ok, payload = intake.hermes_packet_gate(alert, "execute", str(packet_file))

        self.assertFalse(ok)
        self.assertEqual(payload["status"], "REJECTED")
        self.assertIn("hermes_packet_stale", payload["reasons"])
        self.assertIn("hermes_review_item_not_eligible", payload["reasons"])

    def test_hermes_request_preserves_intraday_quote_evidence_contract(self):
        alert = fresh_alert("sig-intraday-contract", "AAPL")
        alert.update(
            {
                "market": "US",
                "factor_evidence_basis": {
                    "completed_daily_ohlcv": 2,
                    "current_session_quote": 1,
                },
                "factor_contributions": [
                    {
                        "category": "momentum",
                        "raw_category": "same_session_momentum",
                        "evidence_basis": "current_session_quote",
                        "direction": "BUY",
                        "score_delta": 0.4,
                        "reason": "當日動量+7.2%",
                    }
                ],
                "current_session_quote_evidence": {
                    "schema": "current_session_quote_evidence_v1",
                    "used_in_full_score": True,
                    "provisional": True,
                    "mutates_completed_daily_history": False,
                    "replaces_completed_daily_bar": False,
                },
                "intraday_evidence_policy": "single_quote_current_session_plus_external_read_only_context",
                "completed_daily_mutation_allowed": False,
                "partial_daily_bar_used_as_completed_daily": False,
            }
        )
        plan = {
            "symbol": "AAPL",
            "side": "buy",
            "quantity": 1,
            "price_reference": 100,
            "risk_hkd": 39,
            "notional_hkd": 780,
        }

        request = intake.build_judgment_request(alert, plan, self.context)

        alert_request = request["alert"]
        self.assertEqual(alert_request["factor_evidence_basis"]["current_session_quote"], 1)
        self.assertEqual(alert_request["factor_contributions"][0]["raw_category"], "same_session_momentum")
        self.assertTrue(alert_request["current_session_quote_evidence"]["used_in_full_score"])
        self.assertFalse(alert_request["current_session_quote_evidence"]["mutates_completed_daily_history"])
        self.assertFalse(alert_request["partial_daily_bar_used_as_completed_daily"])

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

    def test_blocked_readiness_still_blocks_new_buy_exposure_after_hermes(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-buy-readiness-still-blocks")
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
                        "blocking_gates": [
                            {"gate": "simulation_portfolio_performance", "status": "BLOCK"}
                        ],
                        "reasons": ["execution_readiness_status_blocked"],
                    },
                ),
            )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("execution_readiness_gate_failed", result["reasons"])
            self.assertEqual(result["risk_reduction_override"]["status"], "REJECTED")
            self.assertIn("not_sell_alert", result["risk_reduction_override"]["reasons"])
            submit.assert_not_called()

    def test_blocked_readiness_can_allow_reviewed_existing_position_sell(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_sell_alert("sig-risk-reduction-sell")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))
            context = dict(self.context)
            context["positions"] = {"00700": {"quantity": 300, "last_price": 300, "status": "holding"}}

            result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "risk-reduction"},
                context_result=("token", context, []),
                readiness_gate=(
                    False,
                    {
                        "status": "REJECTED",
                        "readiness_status": "BLOCKED",
                        "ready_for_execute": False,
                        "blocking_gates": [
                            {"gate": "simulation_portfolio_performance", "status": "BLOCK"},
                            {"gate": "simulation_trade_review", "status": "BLOCK"},
                        ],
                        "reasons": ["execution_readiness_status_blocked"],
                    },
                ),
                strategy_gate=(
                    False,
                    {
                        "status": "REJECTED",
                        "reasons": [
                            "strategy_evidence_includes_diagnostic_candidates_without_executable_cohort",
                            "overall_outcome_sample_below_30",
                            "execution_candidate_trigger_outcome_missing",
                        ],
                    },
                ),
                pilot_notional_cap=1_000,
                pilot_risk_cap=1,
            )

            self.assertEqual(result["status"], "submitted")
            self.assertEqual(result["plan"]["side"], "sell")
            self.assertEqual(result["risk_reduction_override"]["status"], "PASS")
            self.assertTrue(result["pilot_execution"]["risk_reduction_sell"])
            submit.assert_called_once()

    def test_risk_reduction_sell_still_requires_hermes_judgment(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "missing.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_sell_alert("sig-sell-missing-hermes")
            context = dict(self.context)
            context["positions"] = {"00700": {"quantity": 300, "last_price": 300, "status": "holding"}}

            result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "should-not-submit"},
                context_result=("token", context, []),
                readiness_gate=(
                    False,
                    {
                        "status": "REJECTED",
                        "readiness_status": "BLOCKED",
                        "ready_for_execute": False,
                        "blocking_gates": [{"gate": "simulation_trade_review", "status": "BLOCK"}],
                        "reasons": ["execution_readiness_status_blocked"],
                    },
                ),
            )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("hermes_judgment_gate_failed", result["reasons"])
            self.assertNotIn("risk_reduction_override", result)
            submit.assert_not_called()

    def test_risk_reduction_sell_does_not_override_data_health_blockers(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_sell_alert("sig-sell-data-block")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))
            context = dict(self.context)
            context["positions"] = {"00700": {"quantity": 300, "last_price": 300, "status": "holding"}}

            result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "should-not-submit"},
                context_result=("token", context, []),
                readiness_gate=(
                    False,
                    {
                        "status": "REJECTED",
                        "readiness_status": "BLOCKED",
                        "ready_for_execute": False,
                        "blocking_gates": [{"gate": "data_health", "status": "BLOCK"}],
                        "reasons": ["execution_readiness_status_blocked"],
                    },
                ),
            )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("execution_readiness_gate_failed", result["reasons"])
            self.assertIn(
                "readiness_has_non_risk_reduction_blockers:data_health",
                result["risk_reduction_override"]["reasons"],
            )
            submit.assert_not_called()

    def test_risk_reduction_sell_does_not_override_stale_readiness_report(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_sell_alert("sig-sell-stale-readiness")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))
            context = dict(self.context)
            context["positions"] = {"00700": {"quantity": 300, "last_price": 300, "status": "holding"}}

            result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "should-not-submit"},
                context_result=("token", context, []),
                readiness_gate=(
                    False,
                    {
                        "status": "REJECTED",
                        "readiness_status": "BLOCKED",
                        "ready_for_execute": False,
                        "blocking_gates": [{"gate": "simulation_trade_review", "status": "BLOCK"}],
                        "reasons": [
                            "execution_readiness_status_blocked",
                            "execution_readiness_ready_for_execute_false",
                            "execution_readiness_stale",
                        ],
                    },
                ),
            )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("execution_readiness_gate_failed", result["reasons"])
            self.assertIn(
                "readiness_has_non_risk_reduction_reasons:execution_readiness_stale",
                result["risk_reduction_override"]["reasons"],
            )
            submit.assert_not_called()

    def test_risk_reduction_sell_does_not_override_negative_strategy_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_sell_alert("sig-sell-bad-outcome")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))
            context = dict(self.context)
            context["positions"] = {"00700": {"quantity": 300, "last_price": 300, "status": "holding"}}

            result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "should-not-submit"},
                context_result=("token", context, []),
                strategy_gate=(
                    False,
                    {
                        "status": "REJECTED",
                        "reasons": ["overall_avg_return_not_positive"],
                    },
                ),
            )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("strategy_evidence_gate_failed", result["reasons"])
            self.assertIn(
                "strategy_evidence_block_not_sample_only",
                result["risk_reduction_override"]["reasons"],
            )
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

    def test_alpaca_broker_reconciliation_gate_blocks_unmatched_broker_orders(self):
        report = {
            "schema": "rt_order_intake_event_store_report_v1",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "lineage_summary": {
                "schema": "rt_order_intake_lineage_summary_v1",
                "status": "NO_SUBMITTED_ORDERS",
                "submitted_count": 0,
            },
            "broker_reconciliation": {
                "schema": "rt_order_intake_broker_reconciliation_v1",
                "status": "FAIL",
                "broker": "alpaca-paper",
                "broker_order_count": 5,
                "matched_order_count": 0,
                "unmatched_broker_order_count": 5,
                "reason_codes": ["broker_orders_missing_from_intake_state"],
            },
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "event_store.json"
            path.write_text(json.dumps(report), encoding="utf-8")

            ok, payload = intake.order_intake_broker_reconciliation_gate("alpaca-paper", "execute", str(path))

        self.assertFalse(ok)
        self.assertEqual(payload["status"], "REJECTED")
        self.assertIn("broker_reconciliation_missing_orders", payload["reasons"])
        self.assertEqual(payload["broker_reconciliation"]["unmatched_broker_order_count"], 5)

    def test_non_alpaca_broker_reconciliation_gate_not_required(self):
        ok, payload = intake.order_intake_broker_reconciliation_gate("quantmind-sim", "execute", "missing.json")

        self.assertTrue(ok)
        self.assertEqual(payload["status"], "NOT_REQUIRED")

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

    def test_execute_requires_pilot_enablement(self):
        intake.PILOT_EXECUTION_ENABLED = False
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-pilot-disabled")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))

            result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "should-not-submit"},
            )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("pilot_execution_gate_failed", result["reasons"])
            self.assertIn("pilot_execution_not_enabled", result["pilot_execution"]["reasons"])
            submit.assert_not_called()

    def test_execute_shrinks_pilot_order_above_notional_cap(self):
        intake.PILOT_MAX_ORDER_NOTIONAL_HKD = 50_000
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-pilot-notional")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))

            result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "pilot-order"},
            )

            self.assertEqual(result["status"], "submitted")
            self.assertLessEqual(result["plan"]["notional_hkd"], 50_000)
            self.assertLess(result["plan"]["quantity"], result["original_plan"]["quantity"])
            self.assertEqual(result["pilot_cap"]["status"], "CAPPED")
            self.assertEqual(result["pilot_execution"]["status"], "PASS")
            submit.assert_called_once()

    def test_pilot_caps_shrink_buy_plan_before_execute(self):
        intake.PILOT_MAX_ORDER_NOTIONAL_HKD = 10_000
        intake.PILOT_MAX_ORDER_RISK_HKD = 1_000
        intake.PILOT_ALLOWED_MARKETS = {"US"}
        intake.PILOT_EXECUTION_ENABLED = True
        alert = fresh_alert("sig-pilot-cap-shrink", "AAPL")
        alert.update({"market": "US", "entry_price": 100, "stop_loss": 95, "take_profit": 112})
        plan = {"symbol": "AAPL", "side": "buy", "quantity": 300, "notional_hkd": 234000, "risk_hkd": 11700}

        capped, cap_gate = intake.apply_pilot_caps_to_plan(alert, plan)

        self.assertIsNotNone(capped)
        self.assertEqual(cap_gate["status"], "CAPPED")
        self.assertLessEqual(capped["notional_hkd"], 10_000)
        self.assertLessEqual(capped["risk_hkd"], 1_000)
        self.assertIn("pilot_capped_from", capped)

    def test_pilot_caps_reject_when_rounding_to_zero(self):
        intake.PILOT_MAX_ORDER_NOTIONAL_HKD = 1
        intake.PILOT_MAX_ORDER_RISK_HKD = 1
        intake.PILOT_ALLOWED_MARKETS = {"US"}
        intake.PILOT_EXECUTION_ENABLED = True
        alert = fresh_alert("sig-pilot-cap-zero", "AAPL")
        alert.update({"market": "US", "entry_price": 100, "stop_loss": 95, "take_profit": 112})
        plan = {"symbol": "AAPL", "side": "buy", "quantity": 1, "notional_hkd": 100, "risk_hkd": 5}

        capped, cap_gate = intake.apply_pilot_caps_to_plan(alert, plan)

        self.assertIsNotNone(cap_gate)
        self.assertEqual(cap_gate["reason"], "pilot_cap_quantity_zero")
        self.assertEqual(capped, plan)

    def test_execute_rejects_pilot_daily_submitted_order_cap(self):
        intake.PILOT_MAX_DAILY_SUBMITTED_ORDERS = 1
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            state["processed"]["existing"] = {
                "status": "submitted",
                "submitted_at": datetime.now().isoformat(timespec="seconds"),
            }
            intake.save_json_atomic(state_file, state)
            alert = fresh_alert("sig-pilot-daily-cap")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))

            result, submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                submit_result={"order_id": "should-not-submit"},
            )

            self.assertEqual(result["status"], "rejected")
            self.assertIn("pilot_daily_submitted_order_cap_reached", result["pilot_execution"]["reasons"])
            submit.assert_not_called()

    def test_dry_run_reports_pilot_would_block_without_rejecting(self):
        intake.PILOT_EXECUTION_ENABLED = False
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-pilot-dry-run")

            result, _ = self.run_with_common_patches(
                alert,
                "dry-run",
                state,
                state_file,
                judgment_file,
            )

            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["pilot_execution"]["status"], "DRY_RUN_ONLY")
            self.assertTrue(result["pilot_execution"]["would_block_execute"])

    def test_us_execute_can_route_to_alpaca_paper_when_configured(self):
        intake.US_ORDER_BROKER = "alpaca-paper"
        intake.ALPACA_API_KEY_ID = "paper-key"
        intake.ALPACA_API_SECRET_KEY = "paper-secret"
        self.context = {
            "cash_hkd": 1_000_000,
            "equity_hkd": 1_000_000,
            "positions": {},
            "broker_context": {"backend": "alpaca-paper", "account_ok": True, "positions_ok": True},
        }
        alert = fresh_alert("sig-alpaca-paper", "AAPL")
        alert.update({"market": "US", "entry_price": 100, "stop_loss": 95, "take_profit": 112})
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))

            with patch.object(
                intake,
                "submit_alpaca_paper_order",
                return_value={"id": "alpaca-order", "broker": "alpaca-paper"},
            ) as alpaca_submit, patch.object(intake, "submit_order") as qm_submit:
                result, _submit = self.run_with_common_patches(
                    alert,
                    "execute",
                    state,
                    state_file,
                    judgment_file,
                )

            self.assertEqual(result["status"], "submitted")
            self.assertEqual(result["order_backend"], "alpaca-paper")
            self.assertEqual(result["order_result"]["id"], "alpaca-order")
            self.assertEqual(result["order_result"]["client_order_id"], "qm-sig-alpaca-paper")
            self.assertEqual(result["plan"]["client_order_id"], "qm-sig-alpaca-paper")
            alpaca_submit.assert_called_once_with("AAPL", "buy", result["plan"]["quantity"], "sig-alpaca-paper")
            qm_submit.assert_not_called()

    def test_alpaca_execute_rejects_when_broker_reconciliation_fails(self):
        intake.US_ORDER_BROKER = "alpaca-paper"
        intake.ALPACA_API_KEY_ID = "paper-key"
        intake.ALPACA_API_SECRET_KEY = "paper-secret"
        self.context = {
            "cash_hkd": 1_000_000,
            "equity_hkd": 1_000_000,
            "positions": {},
            "broker_context": {"backend": "alpaca-paper", "account_ok": True, "positions_ok": True},
        }
        alert = fresh_alert("sig-alpaca-recon-fail", "AAPL")
        alert.update({"market": "US", "entry_price": 100, "stop_loss": 95, "take_profit": 112})
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))

            with patch.object(
                intake,
                "submit_alpaca_paper_order",
                return_value={"id": "should-not-submit", "broker": "alpaca-paper"},
            ) as alpaca_submit:
                result, _submit = self.run_with_common_patches(
                    alert,
                    "execute",
                    state,
                    state_file,
                    judgment_file,
                    broker_reconciliation_gate=(
                        False,
                        {
                            "status": "REJECTED",
                            "broker_reconciliation": {
                                "status": "FAIL",
                                "unmatched_broker_order_count": 5,
                            },
                            "reasons": ["broker_reconciliation_missing_orders"],
                        },
                    ),
                )

        self.assertEqual(result["status"], "rejected")
        self.assertIn("broker_reconciliation_gate_failed", result["reasons"])
        self.assertEqual(result["broker_reconciliation"]["broker_reconciliation"]["status"], "FAIL")
        alpaca_submit.assert_not_called()

    def test_alpaca_paper_submit_uses_pilot_capped_quantity(self):
        intake.US_ORDER_BROKER = "alpaca-paper"
        intake.ALPACA_API_KEY_ID = "paper-key"
        intake.ALPACA_API_SECRET_KEY = "paper-secret"
        intake.PILOT_MAX_ORDER_NOTIONAL_HKD = 1_500
        intake.PILOT_MAX_ORDER_RISK_HKD = 150
        intake.PILOT_ALLOWED_MARKETS = {"US"}
        alert = fresh_alert("sig-alpaca-capped", "BAC")
        alert.update({"market": "US", "entry_price": 57.08, "stop_loss": 54.68, "take_profit": 60.69})
        self.context = {
            "cash_hkd": 780_000,
            "equity_hkd": 780_000,
            "positions": {},
            "broker_context": {"backend": "alpaca-paper", "account_ok": True, "positions_ok": True},
        }
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))

            with patch.object(
                intake,
                "submit_alpaca_paper_order",
                return_value={"id": "alpaca-capped", "broker": "alpaca-paper"},
            ) as alpaca_submit:
                result, _submit = self.run_with_common_patches(
                    alert,
                    "execute",
                    state,
                    state_file,
                    judgment_file,
                )

        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["plan"]["quantity"], 3)
        self.assertEqual(result["original_plan"]["quantity"], 175)
        self.assertEqual(result["plan"]["client_order_id"], "qm-sig-alpaca-capped")
        self.assertEqual(result["order_result"]["client_order_id"], "qm-sig-alpaca-capped")
        alpaca_submit.assert_called_once_with("BAC", "buy", 3, "sig-alpaca-capped")

    def test_alpaca_client_order_id_is_traceable_and_bounded(self):
        cid = intake.alpaca_client_order_id("20260618:BAC:布林上軌動量突破:BUY:989847")
        self.assertTrue(cid.startswith("qm-"))
        self.assertLessEqual(len(cid), 48)
        self.assertNotIn(":", cid)

    def test_alpaca_submit_returns_requested_client_order_id_when_api_omits_it(self):
        with patch.object(intake, "alpaca_request", return_value={"id": "broker-id"}) as request:
            result = intake.submit_alpaca_paper_order("BAC", "buy", 3, "sig-client-id")

        self.assertEqual(result["id"], "broker-id")
        self.assertEqual(result["client_order_id"], "qm-sig-client-id")
        self.assertEqual(result["broker"], "alpaca-paper")
        sent = request.call_args.kwargs["data"]
        self.assertEqual(sent["client_order_id"], "qm-sig-client-id")

    def test_execute_rejects_when_required_gate_is_disabled(self):
        intake.REQUIRE_EXECUTION_READINESS = False
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            alert = fresh_alert("sig-disabled-readiness")
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))

            with patch.object(intake, "submit_order") as submit:
                result = intake.process_alert(alert, "execute", state, state_file, judgment_file)

        self.assertEqual(result["status"], "rejected")
        self.assertIn("execute_gate_contract_failed", result["reasons"])
        self.assertIn("execution_readiness_gate_disabled", result["execute_gate_contract"]["reasons"])
        submit.assert_not_called()

    def test_us_alpaca_paper_requires_credentials(self):
        intake.US_ORDER_BROKER = "alpaca-paper"
        alert = fresh_alert("sig-alpaca-missing", "AAPL")
        alert.update({"market": "US", "entry_price": 100, "stop_loss": 95, "take_profit": 112})
        with tempfile.TemporaryDirectory() as td:
            state_file = str(Path(td) / "state.json")
            judgment_file = str(Path(td) / "judgments.jsonl")
            state = intake.load_state(state_file)
            self.write_judgments(judgment_file, judgment(alert["signal_id"]))

            result, _submit = self.run_with_common_patches(
                alert,
                "execute",
                state,
                state_file,
                judgment_file,
                context_result=(
                    "",
                    {
                        "cash_hkd": 100_000,
                        "equity_hkd": 100_000,
                        "positions": {},
                        "broker_context": {
                            "backend": "alpaca-paper",
                            "account_ok": False,
                            "positions_ok": False,
                        },
                    },
                    ["alpaca_paper_credentials_missing; using default empty account context"],
                ),
            )

            self.assertEqual(result["status"], "rejected")
            self.assertEqual(result["order_backend"], "alpaca-paper")
            self.assertIn("broker_context_gate_failed", result["reasons"])
            self.assertIn("alpaca_account_context_unavailable", result["broker_context"]["reasons"])
            self.assertIn("alpaca_paper_credentials_missing", result["broker_context"]["warnings"][0])

    def test_fetch_alpaca_context_converts_usd_account_to_hkd_context(self):
        intake.ALPACA_API_KEY_ID = "paper-key"
        intake.ALPACA_API_SECRET_KEY = "paper-secret"

        def fake_request(path, method="GET", data=None):
            if path == "/account":
                return {"cash": "10000", "equity": "12000"}
            if path == "/positions":
                return [{"symbol": "AAPL", "qty": "3", "avg_entry_price": "100", "current_price": "110"}]
            return {}

        with patch.object(intake, "alpaca_request", side_effect=fake_request):
            token, context, warnings = intake.fetch_alpaca_context()

        self.assertEqual(token, "alpaca-paper")
        self.assertEqual(warnings, [])
        self.assertAlmostEqual(context["cash_hkd"], 78_000)
        self.assertAlmostEqual(context["equity_hkd"], 93_600)
        self.assertEqual(context["positions"]["AAPL"]["quantity"], 3)
        self.assertTrue(context["broker_context"]["account_ok"])
        self.assertTrue(context["broker_context"]["positions_ok"])

    def test_loads_existing_alpaca_env_aliases(self):
        env = {
            "APCA_API_KEY_ID": None,
            "APCA_API_SECRET_KEY": None,
            "ALPACA_API_KEY_ID": None,
            "ALPACA_API_SECRET_KEY": None,
            "ALPACA_KEY_ID": None,
            "ALPACA_API_KEY": "alias-key",
            "ALPACA_SECRET_KEY": "alias-secret",
            "ALPACA_TRADING_BASE_URL": None,
            "ALPACA_BASE_URL": "https://paper-api.alpaca.markets/v2",
        }
        old = {key: os.environ.get(key) for key in env}
        try:
            for key, value in env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            import importlib

            loaded = importlib.reload(intake)
            loaded_values = (
                loaded.ALPACA_API_KEY_ID,
                loaded.ALPACA_API_SECRET_KEY,
                loaded.ALPACA_TRADING_BASE_URL,
            )
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            import importlib

            importlib.reload(intake)

        self.assertEqual(loaded_values[0], "alias-key")
        self.assertEqual(loaded_values[1], "alias-secret")
        self.assertEqual(loaded_values[2], "https://paper-api.alpaca.markets/v2")

    def test_alpaca_context_gate_blocks_execute_when_account_query_failed(self):
        context = {
            "cash_hkd": 100_000,
            "equity_hkd": 100_000,
            "positions": {},
            "broker_context": {"backend": "alpaca-paper", "account_ok": False, "positions_ok": True},
        }

        ok, payload = intake.broker_context_gate(
            "alpaca-paper",
            context,
            ["alpaca_account_query_failed: timeout"],
            "execute",
        )

        self.assertFalse(ok)
        self.assertEqual(payload["status"], "REJECTED")
        self.assertIn("alpaca_account_context_unavailable", payload["reasons"])


if __name__ == "__main__":
    unittest.main()
