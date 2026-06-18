import unittest
from unittest.mock import patch

from scripts import rt_signal_outcome_report as report


def alert(
    signal_id,
    symbol,
    side,
    entry=100,
    trigger="MA",
    confirmed=True,
    strategy_config_id=None,
    watchlist_id=None,
):
    item = {
        "signal_id": signal_id,
        "symbol": symbol,
        "market": "US",
        "signal_type": side,
        "trigger": trigger,
        "confirmed": confirmed,
        "full_score": 0.7 if confirmed else 0.1,
        "entry_price": entry,
        "stop_loss": entry * 0.95 if side == "BUY" else entry * 1.05,
        "take_profit": entry * 1.10 if side == "BUY" else entry * 0.90,
        "rr_ratio": 2.0,
        "quote_time": "2026-06-10 14:30:00",
        "generated_at": "2026-06-11T02:30:00",
    }
    if strategy_config_id:
        item.update(
            {
                "strategy_config_id": strategy_config_id,
                "strategy_config_source": "file",
                "strategy_config_version": f"{strategy_config_id}-version",
            }
        )
    if watchlist_id:
        item.update(
            {
                "watchlist_id": watchlist_id,
                "watchlist_source": "file",
                "watchlist_count": 10,
            }
        )
    return item


def downgraded_watch_alert(signal_id="dw1", symbol="AAPL", candidate_side="BUY", entry=100):
    item = alert(signal_id, symbol, candidate_side, entry=entry, strategy_config_id="cfg-a", watchlist_id="wl-a")
    item.update(
        {
            "signal_type": "WATCH",
            "candidate_signal_type": candidate_side,
            "entry_price": None,
            "stop_loss": None,
            "take_profit": None,
            "rr_ratio": None,
            "candidate_entry_price": entry,
            "candidate_stop_loss": entry * 0.95 if candidate_side == "BUY" else entry * 1.05,
            "candidate_take_profit": entry * 1.10 if candidate_side == "BUY" else entry * 0.90,
            "candidate_rr_ratio": 2.0,
            "execution_candidate": False,
            "execution_blocked_reasons": ["strategy_review_disabled_pending_rework"],
            "suppressed_directional_reason": "strategy_review_disabled_pending_rework",
            "trigger_review_mode": "disabled_pending_rework",
            "strategy_policy_disabled_observation": True,
        }
    )
    return item


class RtSignalOutcomeReportTests(unittest.TestCase):
    def test_fetch_klines_reads_canonical_daily_bars(self):
        captured = {}

        def fake_psql(sql):
            captured["sql"] = sql
            return type(
                "Result",
                (),
                {"returncode": 0, "stdout": "2026-06-10\t99\t101\t98\t100\n", "stderr": ""},
            )()

        with patch.object(report, "psql", side_effect=fake_psql):
            klines, warnings = report.fetch_klines({"AAPL": "2026-06-10"})

        sql = captured["sql"]
        normalized = " ".join(sql.split())
        self.assertEqual(warnings, [])
        self.assertEqual(klines["AAPL"][0]["date"], "2026-06-10")
        self.assertIn("WITH daily_bar AS", sql)
        self.assertIn("SELECT DISTINCT ON (timestamp::date)", sql)
        self.assertIn("ORDER BY timestamp::date, timestamp DESC", normalized)
        self.assertIn("FROM daily_bar ORDER BY trade_date ASC", normalized)

    def test_buy_outcome_uses_future_daily_klines(self):
        klines = {
            "AAPL": [
                {"date": "2026-06-10", "open": 99, "high": 120, "low": 90, "close": 101},
                {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
                {"date": "2026-06-12", "open": 110, "high": 113, "low": 108, "close": 111},
                {"date": "2026-06-15", "open": 111, "high": 115, "low": 107, "close": 112},
            ]
        }

        payload = report.build_report([alert("b1", "AAPL", "BUY")], klines_by_symbol=klines, horizons=(1, 3))
        item = payload["recent_evaluations"][0]

        self.assertEqual(item["status"], "resolved")
        self.assertEqual(item["available_future_days"], 3)
        self.assertEqual(item["outcomes"]["1d"]["signed_close_return_pct"], 10.0)
        self.assertTrue(item["outcomes"]["1d"]["target_hit"])
        self.assertFalse(item["outcomes"]["1d"]["stop_hit"])
        self.assertEqual(payload["schema"], "rt_signal_outcome_report_v1")
        self.assertEqual(payload["raw_alert_count"], 1)
        self.assertEqual(payload["directional_alert_count"], 1)
        self.assertEqual(payload["evaluated_signal_count"], 1)
        self.assertEqual(payload["resolved_signal_count"], 1)
        self.assertEqual(payload["pending_signal_count"], 0)
        self.assertEqual(payload["primary_horizon"], "1d")
        self.assertEqual(payload["primary_horizon_metric"]["resolved_count"], 1)
        self.assertEqual(payload["outcome_maturity"]["resolved_count"], 1)
        self.assertEqual(payload["outcome_maturity"]["pending_or_invalid_count"], 0)
        self.assertEqual(payload["outcome_maturity"]["latest_kline_date"], "2026-06-15")
        self.assertEqual(payload["primary_horizon_metric"]["avg_max_favorable_pct"], 12.0)
        self.assertEqual(payload["primary_horizon_metric"]["avg_max_adverse_pct"], 1.0)
        self.assertEqual(payload["primary_horizon_metric"]["favorable_to_adverse_ratio"], 12.0)
        self.assertEqual(payload["status"], "INSUFFICIENT_SAMPLE")
        self.assertEqual(payload["overall"]["horizons"]["1d"]["win_rate_pct"], 100.0)
        self.assertEqual(payload["evaluations"], payload["recent_evaluations"])
        self.assertEqual(payload["evaluations"][0]["signal_id"], "b1")

    def test_sell_outcome_inverts_return_direction(self):
        klines = {
            "TSLA": [
                {"date": "2026-06-10", "open": 100, "high": 101, "low": 98, "close": 100},
                {"date": "2026-06-11", "open": 99, "high": 100, "low": 89, "close": 90},
            ]
        }

        payload = report.build_report([alert("s1", "TSLA", "SELL")], klines_by_symbol=klines, horizons=(1,))
        outcome = payload["recent_evaluations"][0]["outcomes"]["1d"]

        self.assertEqual(outcome["signed_close_return_pct"], 10.0)
        self.assertTrue(outcome["target_hit"])
        self.assertFalse(outcome["stop_hit"])

    def test_downgraded_watch_candidate_is_evaluated_by_candidate_side(self):
        klines = {
            "AAPL": [
                {"date": "2026-06-10", "open": 99, "high": 101, "low": 98, "close": 100},
                {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
            ]
        }

        payload = report.build_report(
            [downgraded_watch_alert("dw1", "AAPL", "BUY")],
            klines_by_symbol=klines,
            horizons=(1,),
        )
        item = payload["recent_evaluations"][0]
        by_trigger = {row["key"]: row for row in payload["by_trigger"]}

        self.assertEqual(payload["directional_alert_count"], 1)
        self.assertEqual(payload["downgraded_directional_alert_count"], 1)
        self.assertEqual(payload["counts"]["by_signal_type"], {"WATCH": 1})
        self.assertEqual(payload["counts"]["by_candidate_signal_type"], {"BUY": 1})
        self.assertEqual(item["signal_type"], "BUY")
        self.assertEqual(item["emitted_signal_type"], "WATCH")
        self.assertEqual(item["candidate_signal_type"], "BUY")
        self.assertTrue(item["downgraded_directional"])
        self.assertEqual(item["entry_price"], 100)
        self.assertEqual(item["stop_loss"], 95)
        self.assertAlmostEqual(item["take_profit"], 110)
        self.assertEqual(item["rr_ratio"], 2.0)
        self.assertFalse(item["execution_candidate"])
        self.assertEqual(item["trigger_review_mode"], "disabled_pending_rework")
        self.assertEqual(item["outcomes"]["1d"]["signed_close_return_pct"], 10.0)
        self.assertIn("BUY:MA", by_trigger)
        self.assertEqual(by_trigger["BUY:MA"]["horizons"]["1d"]["resolved_count"], 1)

    def test_execution_candidate_cohort_excludes_diagnostic_directional_rows(self):
        executable = alert("exec-1", "AAPL", "BUY", strategy_config_id="cfg-a", watchlist_id="wl-a")
        executable["execution_candidate"] = True
        diagnostic = downgraded_watch_alert("diag-1", "MSFT", "BUY", entry=100)
        klines = {
            "AAPL": [
                {"date": "2026-06-10", "open": 99, "high": 101, "low": 98, "close": 100},
                {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
            ],
            "MSFT": [
                {"date": "2026-06-10", "open": 99, "high": 101, "low": 98, "close": 100},
                {"date": "2026-06-11", "open": 100, "high": 101, "low": 89, "close": 90},
            ],
        }

        payload = report.build_report([executable, diagnostic], klines_by_symbol=klines, horizons=(1,))
        execution = payload["execution_candidate"]
        by_trigger = {row["key"]: row for row in execution["by_trigger"]}

        self.assertEqual(payload["counts"]["execution_candidate_count"], 1)
        self.assertEqual(payload["counts"]["non_execution_candidate_count"], 1)
        self.assertEqual(payload["execution_candidate_count"], 1)
        self.assertEqual(payload["non_execution_candidate_count"], 1)
        self.assertEqual(payload["downgraded_directional_alert_count"], 1)
        self.assertEqual(execution["counts"]["evaluated_signal_count"], 1)
        self.assertEqual(execution["counts"]["resolved_signal_count"], 1)
        self.assertEqual(execution["counts"]["non_execution_candidate_count"], 1)
        self.assertEqual(execution["overall"]["scope"], "execution_candidate")
        self.assertTrue(execution["overall"]["execution_candidate"])
        self.assertEqual(execution["overall"]["horizons"]["1d"]["resolved_count"], 1)
        self.assertEqual(execution["overall"]["horizons"]["1d"]["avg_signed_close_return_pct"], 10.0)
        self.assertEqual(payload["execution_candidate_overall"], execution["overall"])
        self.assertEqual(payload["execution_candidate_by_trigger"], execution["by_trigger"])
        self.assertEqual(by_trigger["BUY:MA"]["count"], 1)
        self.assertEqual(by_trigger["BUY:MA"]["horizons"]["1d"]["avg_signed_close_return_pct"], 10.0)

    def test_intraday_minutes_resolve_ambiguous_same_day_threshold_order(self):
        klines = {
            "AAPL": [
                {"date": "2026-06-10", "open": 99, "high": 101, "low": 98, "close": 100},
                {"date": "2026-06-11", "open": 100, "high": 112, "low": 94, "close": 106},
            ]
        }
        minute_rows = {
            "AAPL|2026-06-11": [
                {
                    "timestamp": "2026-06-11T10:00:00",
                    "open": 100,
                    "high": 111,
                    "low": 99,
                    "close": 110,
                    "data_source": "broker_minute_ohlcv",
                    "source_granularity": "minute_ohlcv",
                },
                {
                    "timestamp": "2026-06-11T10:05:00",
                    "open": 110,
                    "high": 111,
                    "low": 94,
                    "close": 95,
                    "data_source": "broker_minute_ohlcv",
                    "source_granularity": "minute_ohlcv",
                },
            ]
        }

        payload = report.build_report(
            [alert("b1", "AAPL", "BUY")],
            klines_by_symbol=klines,
            intraday_klines_by_symbol_date=minute_rows,
            horizons=(1,),
        )
        outcome = payload["recent_evaluations"][0]["outcomes"]["1d"]

        self.assertEqual(outcome["first_hit"], "ambiguous_same_day")
        self.assertEqual(outcome["first_hit_date"], "2026-06-11")
        self.assertEqual(outcome["intraday_sequence"]["status"], "RESOLVED")
        self.assertEqual(outcome["intraday_sequence"]["first_hit"], "target")
        self.assertEqual(outcome["intraday_sequence"]["source_fidelity"]["status"], "FULL_OHLC")
        self.assertEqual(
            outcome["intraday_sequence"]["reason"],
            "target_touched_before_stop_on_minute_bars",
        )
        self.assertEqual(payload["intraday_sequence_summary"]["resolved_count"], 1)
        self.assertEqual(payload["intraday_sequence_summary"]["first_hit_counts"], {"target": 1})
        self.assertEqual(payload["primary_horizon_metric"]["first_hit_counts"], {"ambiguous_same_day": 1})
        self.assertEqual(
            payload["primary_horizon_metric"]["effective_first_hit_counts"],
            {"intraday_target": 1},
        )
        self.assertEqual(payload["primary_horizon_metric"]["effective_target_first_rate_pct"], 100.0)
        self.assertEqual(payload["primary_horizon_metric"]["effective_stop_first_rate_pct"], 0.0)
        self.assertIn("Intraday sequence: ambiguous_daily=1 resolved=1", report.build_text_report(payload))

    def test_snapshot_intraday_minutes_do_not_resolve_threshold_order(self):
        klines = {
            "AAPL": [
                {"date": "2026-06-10", "open": 99, "high": 101, "low": 98, "close": 100},
                {"date": "2026-06-11", "open": 100, "high": 112, "low": 94, "close": 106},
            ]
        }
        minute_rows = {
            "AAPL|2026-06-11": [
                {
                    "timestamp": "2026-06-11T10:00:00",
                    "open": 111,
                    "high": 111,
                    "low": 111,
                    "close": 111,
                    "data_source": "tencent_minute_query",
                    "source_granularity": "minute_snapshot_price",
                },
                {
                    "timestamp": "2026-06-11T10:05:00",
                    "open": 95,
                    "high": 95,
                    "low": 95,
                    "close": 95,
                    "data_source": "tencent_minute_query",
                    "source_granularity": "minute_snapshot_price",
                },
            ]
        }

        payload = report.build_report(
            [alert("b1", "AAPL", "BUY")],
            klines_by_symbol=klines,
            intraday_klines_by_symbol_date=minute_rows,
            horizons=(1,),
        )
        outcome = payload["recent_evaluations"][0]["outcomes"]["1d"]

        self.assertEqual(outcome["first_hit"], "ambiguous_same_day")
        self.assertEqual(outcome["intraday_sequence"]["status"], "LOW_FIDELITY")
        self.assertEqual(outcome["intraday_sequence"]["first_hit"], None)
        self.assertEqual(outcome["intraday_sequence"]["sampled_first_hit"], "target")
        self.assertEqual(outcome["intraday_sequence"]["source_fidelity"]["status"], "LOW_FIDELITY")
        self.assertIn(
            "low_fidelity_source_granularity:minute_snapshot_price",
            outcome["intraday_sequence"]["source_fidelity"]["reasons"],
        )
        self.assertEqual(payload["intraday_sequence_summary"]["low_fidelity_count"], 1)
        self.assertEqual(
            payload["primary_horizon_metric"]["effective_first_hit_counts"],
            {"ambiguous_intraday_low_fidelity": 1},
        )
        self.assertEqual(payload["primary_horizon_metric"]["effective_target_first_rate_pct"], 0.0)
        self.assertEqual(payload["primary_horizon_metric"]["effective_unresolved_first_hit_rate_pct"], 100.0)
        self.assertIn("collect_full_ohlcv_minute_path_evidence:1", payload["recommendations"])

    def test_missing_intraday_minutes_keep_ambiguous_same_day_conservative(self):
        klines = {
            "AAPL": [
                {"date": "2026-06-10", "open": 99, "high": 101, "low": 98, "close": 100},
                {"date": "2026-06-11", "open": 100, "high": 112, "low": 94, "close": 106},
            ]
        }

        payload = report.build_report(
            [alert("b1", "AAPL", "BUY")],
            klines_by_symbol=klines,
            intraday_klines_by_symbol_date={},
            horizons=(1,),
        )
        outcome = payload["recent_evaluations"][0]["outcomes"]["1d"]

        self.assertEqual(outcome["first_hit"], "ambiguous_same_day")
        self.assertEqual(outcome["intraday_sequence"]["status"], "MISSING")
        self.assertEqual(
            outcome["intraday_sequence"]["reason"],
            "missing_intraday_rows_for_ambiguous_daily_bar",
        )
        self.assertEqual(payload["intraday_sequence_summary"]["missing_count"], 1)
        self.assertEqual(
            payload["primary_horizon_metric"]["effective_first_hit_counts"],
            {"ambiguous_intraday_missing": 1},
        )
        self.assertEqual(payload["primary_horizon_metric"]["effective_unresolved_first_hit_rate_pct"], 100.0)
        self.assertIn(
            "collect_minute_klines_to_resolve_ambiguous_stop_target:1",
            payload["recommendations"],
        )

    def test_intraday_signal_context_summarizes_no_lookahead_alignment(self):
        klines = {
            "AAPL": [
                {"date": "2026-06-10", "open": 99, "high": 101, "low": 98, "close": 100},
                {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
            ]
        }
        minute_rows = {
            "AAPL|2026-06-10": [
                {"timestamp": "2026-06-10T14:00:00", "open": 100, "high": 100.5, "low": 99.9, "close": 100.0},
                {"timestamp": "2026-06-10T14:25:00", "open": 100.0, "high": 102.0, "low": 99.9, "close": 101.5},
                {"timestamp": "2026-06-10T14:30:00", "open": 101.5, "high": 103.0, "low": 101.4, "close": 102.5},
                {"timestamp": "2026-06-10T14:31:00", "open": 80.0, "high": 80.0, "low": 79.0, "close": 79.0},
            ]
        }

        payload = report.build_report(
            [alert("b1", "AAPL", "BUY")],
            klines_by_symbol=klines,
            intraday_signal_klines_by_symbol_date=minute_rows,
            horizons=(1,),
        )
        context = payload["recent_evaluations"][0]["intraday_signal_context"]
        by_alignment = {row["key"]: row for row in payload["by_intraday_signal_alignment"]}

        self.assertEqual(context["schema"], "intraday_signal_context_v1")
        self.assertEqual(context["alignment"], "supports_signal")
        self.assertEqual(context["latest_timestamp"], "2026-06-10T14:30:00")
        self.assertEqual(context["row_count"], 3)
        votes_by_timeframe = {row["timeframe"]: row for row in context["votes"]}
        self.assertEqual(votes_by_timeframe["latest_15m_before_signal"]["direction"], "up")
        self.assertEqual(votes_by_timeframe["latest_15m_before_signal"]["row_count"], 2)
        self.assertEqual(votes_by_timeframe["latest_30m_before_signal"]["direction"], "up")
        self.assertEqual(votes_by_timeframe["latest_30m_before_signal"]["row_count"], 2)
        self.assertEqual(payload["intraday_signal_context_summary"]["coverage_pct"], 100.0)
        self.assertEqual(payload["intraday_signal_context_summary"]["alignment_counts"], {"supports_signal": 1})
        self.assertEqual(by_alignment["supports_signal"]["horizons"]["1d"]["resolved_count"], 1)
        self.assertIn("Intraday signal context: coverage=100.0%", report.build_text_report(payload))

    def test_intraday_signal_context_uses_canonical_alignment_names(self):
        klines = {
            "AAPL": [
                {"date": "2026-06-10", "open": 99, "high": 101, "low": 98, "close": 100},
                {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
            ]
        }
        minute_rows = {
            "AAPL|2026-06-10": [
                {"timestamp": "2026-06-10T14:00:00", "open": 100, "high": 100, "low": 100, "close": 100},
                {"timestamp": "2026-06-10T14:25:00", "open": 98, "high": 98, "low": 98, "close": 98},
                {"timestamp": "2026-06-10T14:26:00", "open": 98, "high": 98, "low": 98, "close": 98},
                {"timestamp": "2026-06-10T14:30:00", "open": 99, "high": 99, "low": 99, "close": 99},
            ]
        }

        payload = report.build_report(
            [alert("b1", "AAPL", "BUY")],
            klines_by_symbol=klines,
            intraday_signal_klines_by_symbol_date=minute_rows,
            horizons=(1,),
        )

        context = payload["recent_evaluations"][0]["intraday_signal_context"]
        self.assertEqual(context["alignment"], "conflicting_timeframes")
        self.assertEqual(
            payload["intraday_signal_context_summary"]["alignment_counts"],
            {"conflicting_timeframes": 1},
        )

    def test_intraday_signal_context_summary_normalizes_legacy_alignment_names(self):
        summary = report.intraday_signal_context_summary(
            [
                {"signal_id": "old-conflict", "intraday_signal_context": {"status": "OK", "alignment": "conflicting_intraday_context"}},
                {"signal_id": "old-missing", "intraday_signal_context": {"status": "MISSING", "alignment": "missing_minute_rows_before_signal"}},
            ]
        )

        self.assertEqual(
            summary["alignment_counts"],
            {"conflicting_timeframes": 1, "unavailable_or_stale": 1},
        )

    def test_fetch_intraday_signal_context_klines_limits_rows_to_signal_timestamp(self):
        captured = {}

        def fake_psql(sql, **_kwargs):
            if "information_schema.columns" in sql:
                return type(
                    "Result",
                    (),
                    {"returncode": 0, "stdout": "data_source\n", "stderr": ""},
                )()
            captured["sql"] = sql
            return type(
                "Result",
                (),
                {"returncode": 0, "stdout": "2026-06-10 14:30:00\t100\t101\t99\t100.5\t1000\ttencent_min\n", "stderr": ""},
            )()

        cutoff = report.parse_timestamp("2026-06-10T14:30:00")
        with patch.object(report, "psql", side_effect=fake_psql):
            rows, warnings = report.fetch_intraday_signal_context_klines([("AAPL", "2026-06-10", cutoff)])

        normalized = " ".join(captured["sql"].split())
        self.assertEqual(warnings, [])
        self.assertIn("timestamp <= '2026-06-10 14:30:00'::timestamp", normalized)
        self.assertEqual(rows["AAPL|2026-06-10"][0]["timestamp"], "2026-06-10 14:30:00")

    def test_pending_when_no_future_kline_exists(self):
        klines = {
            "AAPL": [
                {"date": "2026-06-10", "open": 99, "high": 101, "low": 98, "close": 100},
            ]
        }

        payload = report.build_report([alert("b1", "AAPL", "BUY")], klines_by_symbol=klines, horizons=(1,))
        item = payload["recent_evaluations"][0]

        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["reason"], "no_future_daily_klines")
        self.assertEqual(payload["overall"]["horizons"]["1d"]["pending_count"], 1)
        self.assertEqual(payload["status"], "PENDING")
        self.assertEqual(payload["resolved_signal_count"], 0)
        self.assertEqual(payload["pending_or_invalid_count"], 1)
        self.assertEqual(payload["pending_reasons"], {"no_future_daily_klines": 1})
        self.assertEqual(payload["outcome_maturity"]["schema"], "outcome_maturity_summary_v1")
        self.assertEqual(payload["outcome_maturity"]["needed_future_days"], 1)
        self.assertEqual(payload["outcome_maturity"]["latest_signal_date"], "2026-06-10")
        self.assertEqual(payload["outcome_maturity"]["latest_kline_date"], "2026-06-10")
        self.assertEqual(payload["outcome_maturity"]["min_missing_future_days_for_pending"], 1)
        self.assertEqual(payload["outcome_maturity"]["earliest_primary_horizon_date_for_pending"], "2026-06-11")
        self.assertEqual(payload["outcome_maturity"]["earliest_primary_horizon_trading_date_for_pending"], "2026-06-11")
        self.assertEqual(
            payload["outcome_maturity"]["calendar_pending_reason_counts"],
            {"waiting_for_next_trading_day": 1},
        )
        self.assertEqual(payload["outcome_maturity"]["pending_examples"][0]["missing_future_days"], 1)
        self.assertEqual(payload["primary_recommendation"], "outcome_sample_not_ready_keep_collecting_daily_klines")
        self.assertEqual(
            payload["recommendations"],
            [
                "outcome_sample_not_ready_keep_collecting_daily_klines",
                "collect_signal_time_minute_context:1",
            ],
        )

    def test_weekend_pending_waits_for_next_trading_day(self):
        friday = alert("fri", "AAPL", "BUY")
        friday["quote_time"] = "2026-06-12 14:30:00"
        friday["generated_at"] = "2026-06-12T14:30:00"
        klines = {
            "AAPL": [
                {"date": "2026-06-12", "open": 99, "high": 101, "low": 98, "close": 100},
            ]
        }

        payload = report.build_report([friday], klines_by_symbol=klines, horizons=(1,))
        maturity = payload["outcome_maturity"]
        text = report.build_text_report(payload)

        self.assertEqual(payload["status"], "PENDING")
        self.assertEqual(maturity["earliest_primary_horizon_date_for_pending"], "2026-06-13")
        self.assertEqual(maturity["earliest_primary_horizon_trading_date_for_pending"], "2026-06-15")
        self.assertEqual(maturity["calendar_pending_reason_counts"], {"waiting_for_next_trading_day": 1})
        self.assertEqual(
            maturity["pending_examples"][0]["calendar_pending_reason"],
            "waiting_for_next_trading_day",
        )
        self.assertIn("earliest_trading_horizon=2026-06-15", text)
        self.assertIn("waiting_for_next_trading_day=1", text)

    def test_sparse_future_klines_are_classified_as_kline_gap(self):
        monday = alert("gap", "AAPL", "BUY")
        monday["quote_time"] = "2026-06-08 14:30:00"
        monday["generated_at"] = "2026-06-08T14:30:00"
        klines = {
            "AAPL": [
                {"date": "2026-06-08", "open": 99, "high": 101, "low": 98, "close": 100},
                {"date": "2026-06-10", "open": 101, "high": 102, "low": 98, "close": 101},
            ]
        }

        payload = report.build_report([monday], klines_by_symbol=klines, horizons=(2,))
        maturity = payload["outcome_maturity"]

        self.assertEqual(payload["status"], "PENDING")
        self.assertEqual(maturity["earliest_primary_horizon_trading_date_for_pending"], "2026-06-10")
        self.assertEqual(maturity["calendar_pending_reason_counts"], {"kline_gap_or_missing_symbol": 1})
        self.assertEqual(
            maturity["pending_examples"][0]["calendar_pending_reason"],
            "kline_gap_or_missing_symbol",
        )

    def test_missing_symbol_klines_are_attributed_in_maturity_summary(self):
        diagnostics = {
            "MISSING": {
                "symbol": "MISSING",
                "status": "not_found_in_stocks_no_klines",
                "stock_found": False,
                "day_kline_count": 0,
                "latest_kline_date": None,
            }
        }

        payload = report.build_report(
            [alert("missing-kline-1", "MISSING", "BUY"), alert("missing-kline-2", "MISSING", "BUY")],
            klines_by_symbol={"MISSING": []},
            horizons=(1,),
            symbol_kline_diagnostics=diagnostics,
        )
        maturity = payload["outcome_maturity"]
        text = report.build_text_report(payload)

        self.assertEqual(payload["pending_reasons"], {"missing_symbol_klines": 2})
        self.assertEqual(maturity["missing_symbol_kline_count"], 2)
        self.assertEqual(maturity["missing_symbol_kline_unique_symbol_count"], 1)
        self.assertEqual(maturity["missing_symbol_kline_symbols"], ["MISSING"])
        self.assertEqual(
            maturity["missing_symbol_kline_diagnostics"][0]["status"],
            "not_found_in_stocks_no_klines",
        )
        self.assertEqual(maturity["missing_symbol_kline_diagnostics"][0]["affected_signal_count"], 2)
        self.assertEqual(
            maturity["missing_symbol_kline_diagnostics"][0]["signal_ids"],
            ["missing-kline-1", "missing-kline-2"],
        )
        self.assertIn("Missing symbol K-lines: not_found_in_stocks_no_klines=2", text)

    def test_missing_symbol_maturity_surfaces_minute_fresh_daily_stale_gap(self):
        diagnostics = {
            "00959": {
                "symbol": "00959",
                "status": "stock_found_has_day_klines_before_signal_date",
                "stock_found": True,
                "exchange": "HKEX",
                "is_active": "true",
                "day_kline_count": 2000,
                "latest_kline_date": "2025-06-25",
                "minute_kline_count": 666,
                "latest_minute_date": "2026-06-12",
                "daily_refresh_gap": True,
            }
        }

        payload = report.build_report(
            [alert("missing-kline-1", "00959", "BUY")],
            klines_by_symbol={"00959": []},
            horizons=(1,),
            symbol_kline_diagnostics=diagnostics,
        )

        diagnostic = payload["outcome_maturity"]["missing_symbol_kline_diagnostics"][0]
        text = report.build_text_report(payload)
        self.assertTrue(diagnostic["daily_refresh_gap"])
        self.assertEqual(diagnostic["latest_minute_date"], "2026-06-12")
        self.assertEqual(diagnostic["minute_kline_count"], 666)
        self.assertIn("repair_daily_kline_refresh_gap_for_minute_fresh_symbols:1", payload["recommendations"])
        self.assertIn("Daily refresh gaps: 00959", text)

    def test_missing_symbol_maturity_links_to_daily_gap_repair_plan(self):
        diagnostics = {
            "01918": {
                "symbol": "01918",
                "status": "stock_found_has_day_klines_before_signal_date",
                "latest_kline_date": "2026-06-11",
                "latest_minute_date": "2026-06-12",
                "daily_refresh_gap": True,
            },
            "00959": {
                "symbol": "00959",
                "status": "stock_found_has_day_klines_before_signal_date",
                "latest_kline_date": "2025-06-25",
                "latest_minute_date": "2026-06-12",
                "daily_refresh_gap": True,
            },
        }
        repair = {
            "schema": "kline_daily_gap_repair_report_v1",
            "status": "PARTIAL",
            "generated_at": "2026-06-12T22:57:30",
            "plan_hash": "3a427dd004186bea",
            "summary": {"repair_action_count": 1, "unresolved_count": 1},
            "recommendations": ["operator_may_apply_hash_confirmed_daily_gap_plan_after_review"],
            "actions": [
                {
                    "symbol": "01918",
                    "row_count": 1,
                    "latest_daily_date": "2026-06-11",
                    "target_end_date": "2026-06-12",
                    "source_code": "hk01918",
                }
            ],
            "unresolved": [
                {
                    "symbol": "00959",
                    "reason": "source_gap_rows_missing",
                    "latest_daily_date": "2025-06-25",
                    "target_end_date": "2026-06-12",
                    "latest_source_date": "2025-06-25",
                    "source_reaches_target_end": False,
                    "source_after_latest_daily": False,
                }
            ],
            "apply_contract": {
                "dry_run_default": True,
                "does_not_submit_orders": True,
                "manual_apply_command": "/usr/bin/python3 /root/kline_daily_gap_repair.py --apply --confirm-plan-hash 3a427dd004186bea --text",
            },
        }

        payload = report.build_report(
            [alert("gap-actionable", "01918", "SELL"), alert("gap-unresolved", "00959", "BUY")],
            klines_by_symbol={"01918": [], "00959": []},
            horizons=(1,),
            symbol_kline_diagnostics=diagnostics,
            kline_daily_gap_repair=repair,
        )

        rows = {
            item["symbol"]: item
            for item in payload["outcome_maturity"]["missing_symbol_kline_diagnostics"]
        }
        self.assertEqual(rows["01918"]["daily_gap_repair_status"], "actionable")
        self.assertEqual(rows["01918"]["daily_gap_repair_action"]["row_count"], 1)
        self.assertEqual(rows["00959"]["daily_gap_repair_status"], "unresolved")
        self.assertEqual(rows["00959"]["daily_gap_repair_unresolved"]["reason"], "source_gap_rows_missing")
        context = payload["kline_daily_gap_repair_context"]
        self.assertEqual(context["schema"], "outcome_daily_gap_repair_context_v1")
        self.assertEqual(context["actionable_missing_symbol_count"], 1)
        self.assertEqual(context["unresolved_missing_symbol_count"], 1)
        self.assertIn("apply_reviewed_daily_gap_plan_for_outcome_symbols:1", payload["recommendations"])
        self.assertIn("review_unresolved_daily_gap_symbols_for_source_or_mapping:1", payload["recommendations"])
        self.assertNotIn("repair_daily_kline_refresh_gap_for_minute_fresh_symbols:2", payload["recommendations"])
        text = report.build_text_report(payload)
        self.assertIn("Daily gap repair mapping: actionable=1, unresolved=1", text)
        self.assertIn("Daily gap repair plan: status=PARTIAL hash=3a427dd004186bea", text)

    def test_missing_symbol_maturity_links_to_gap_source_diagnostic(self):
        diagnostics = {
            "00959": {
                "symbol": "00959",
                "status": "stock_found_has_day_klines_before_signal_date",
                "latest_kline_date": "2025-06-25",
                "latest_minute_date": "2026-06-12",
                "daily_refresh_gap": True,
            }
        }
        repair = {
            "schema": "kline_daily_gap_repair_report_v1",
            "status": "UNRESOLVED",
            "generated_at": "2026-06-12T22:57:30",
            "plan_hash": "3a427dd004186bea",
            "summary": {"repair_action_count": 0, "unresolved_count": 1},
            "unresolved": [
                {
                    "symbol": "00959",
                    "reason": "source_gap_rows_missing",
                    "latest_daily_date": "2025-06-25",
                    "target_end_date": "2026-06-12",
                    "latest_source_date": "2025-06-25",
                }
            ],
        }
        source_diag = {
            "schema": "kline_gap_source_diagnostic_report_v1",
            "status": "ACTION_REQUIRED",
            "generated_at": "2026-06-12T23:51:00",
            "source": {
                "read_only": True,
                "submits_orders": False,
                "applies_kline_repairs": False,
                "changes_watchlists": False,
                "changes_stock_universe": False,
                "auto_excludes_from_evidence": False,
            },
            "summary": {
                "unresolved_count": 1,
                "classified_count": 1,
                "category_counts": {"active_universe_or_symbol_mapping_issue": 1},
                "confidence_counts": {"high": 1},
            },
            "classifications": [
                {
                    "symbol": "00959",
                    "market": "HK",
                    "category": "active_universe_or_symbol_mapping_issue",
                    "confidence": "high",
                    "recommended_action": "review_active_universe_and_symbol_mapping_before_trusting_symbol",
                    "reason": "source_gap_rows_missing",
                    "latest_daily_date": "2025-06-25",
                    "target_end_date": "2026-06-12",
                    "latest_source_date": "2025-06-25",
                    "source_lag_days_vs_target": 352,
                    "daily_lag_days_vs_target": 352,
                    "hygiene": {
                        "recommended_action": "candidate_deactivate_or_symbol_mapping",
                        "issues": ["latest_kline_stale_ge_30d", "no_history_rows_120d"],
                    },
                }
            ],
            "recommendations": ["review_active_universe_or_symbol_mapping_for_unresolved_gap_symbols"],
            "warnings": [],
        }

        payload = report.build_report(
            [alert("gap-source", "00959", "BUY")],
            klines_by_symbol={"00959": []},
            horizons=(1,),
            symbol_kline_diagnostics=diagnostics,
            kline_daily_gap_repair=repair,
            kline_gap_source_diagnostic=source_diag,
        )

        row = payload["outcome_maturity"]["missing_symbol_kline_diagnostics"][0]
        context = payload["kline_gap_source_diagnostic_context"]
        text = report.build_text_report(payload)

        self.assertEqual(row["daily_gap_repair_status"], "unresolved")
        self.assertEqual(row["daily_gap_source_diagnostic_status"], "classified")
        self.assertEqual(row["daily_gap_source_category"], "active_universe_or_symbol_mapping_issue")
        self.assertEqual(row["daily_gap_source_confidence"], "high")
        self.assertEqual(
            row["daily_gap_source_hygiene"]["recommended_action"],
            "candidate_deactivate_or_symbol_mapping",
        )
        self.assertEqual(context["schema"], "outcome_kline_gap_source_diagnostic_context_v1")
        self.assertEqual(context["classified_missing_symbol_count"], 1)
        self.assertEqual(context["active_universe_or_mapping_missing_symbol_count"], 1)
        self.assertEqual(context["category_affected_signal_counts"], {"active_universe_or_symbol_mapping_issue": 1})
        self.assertNotIn("repair_daily_kline_refresh_gap_for_minute_fresh_symbols:1", payload["recommendations"])
        self.assertIn("review_unresolved_daily_gap_symbols_for_source_or_mapping:1", payload["recommendations"])
        self.assertIn("review_active_universe_or_mapping_for_outcome_symbols:1", payload["recommendations"])
        self.assertIn("Daily gap source diagnostic: active_universe_or_symbol_mapping_issue=1", text)
        self.assertIn("Daily gap source plan: status=ACTION_REQUIRED classified=1 active_or_mapping=1", text)

    def test_dedupes_signal_ids(self):
        alerts = [alert("b1", "AAPL", "BUY"), alert("b1", "AAPL", "BUY")]
        payload = report.build_report(alerts, klines_by_symbol={"AAPL": []}, horizons=(1,))

        self.assertEqual(payload["counts"]["directional_alert_count"], 2)
        self.assertEqual(payload["counts"]["evaluated_signal_count"], 1)
        self.assertEqual(payload["counts"]["duplicate_signal_count"], 1)
        self.assertEqual(payload["directional_alert_count"], 2)
        self.assertEqual(payload["evaluated_signal_count"], 1)
        self.assertEqual(payload["duplicate_signal_count"], 1)

    def test_evaluation_preserves_strategy_and_watchlist_metadata(self):
        item = alert("b1", "AAPL", "BUY", strategy_config_id="cfg-a", watchlist_id="wl-a")
        evaluated = report.evaluate_alert(
            item,
            [
                {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
            ],
            horizons=(1,),
        )

        self.assertEqual(evaluated["strategy_config_id"], "cfg-a")
        self.assertEqual(evaluated["strategy_config_source"], "file")
        self.assertEqual(evaluated["strategy_config_version"], "cfg-a-version")
        self.assertEqual(evaluated["watchlist_id"], "wl-a")
        self.assertEqual(evaluated["watchlist_source"], "file")
        self.assertEqual(evaluated["watchlist_count"], 10)

    def test_groups_outcomes_by_strategy_config_and_watchlist(self):
        klines = {
            "AAPL": [
                {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
            ],
            "MSFT": [
                {"date": "2026-06-11", "open": 101, "high": 102, "low": 94, "close": 95},
            ],
        }
        alerts = [
            alert("b1", "AAPL", "BUY", strategy_config_id="cfg-a", watchlist_id="wl-a"),
            alert("b2", "MSFT", "BUY", strategy_config_id="cfg-a", watchlist_id="wl-b"),
        ]

        payload = report.build_report(alerts, klines_by_symbol=klines, horizons=(1,), sample_scope_mode="all")
        by_config = {row["key"]: row for row in payload["by_strategy_config"]}
        by_watchlist = {row["key"]: row for row in payload["by_watchlist"]}
        by_config_trigger = {row["key"]: row for row in payload["by_strategy_config_trigger"]}

        self.assertEqual(by_config["cfg-a"]["count"], 2)
        self.assertEqual(by_config["cfg-a"]["horizons"]["1d"]["resolved_count"], 2)
        self.assertEqual(by_config["cfg-a"]["horizons"]["1d"]["win_rate_pct"], 50.0)
        self.assertEqual(by_config["cfg-a"]["version_counts"], {"cfg-a-version": 2})
        self.assertEqual(by_watchlist["wl-a"]["count"], 1)
        self.assertEqual(by_watchlist["wl-b"]["count"], 1)
        self.assertEqual(by_config_trigger["cfg-a|BUY:MA"]["strategy_config_id"], "cfg-a")

    def test_missing_metadata_is_grouped_explicitly(self):
        payload = report.build_report(
            [alert("b1", "AAPL", "BUY")],
            klines_by_symbol={
                "AAPL": [
                    {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
                ]
            },
            horizons=(1,),
        )

        self.assertEqual(payload["counts"]["missing_watchlist_metadata_count"], 1)
        self.assertEqual(payload["counts"]["missing_strategy_config_metadata_count"], 1)
        self.assertEqual(payload["by_strategy_config"][0]["key"], "missing")
        self.assertEqual(payload["by_watchlist"][0]["key"], "missing")

    def test_current_sample_scope_excludes_legacy_missing_metadata(self):
        legacy = alert("old", "AAPL", "BUY")
        current = alert("new", "MSFT", "BUY", strategy_config_id="cfg-a", watchlist_id="wl-a")
        klines = {
            "AAPL": [
                {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
            ],
            "MSFT": [
                {"date": "2026-06-11", "open": 101, "high": 112, "low": 99, "close": 110},
            ],
        }

        with patch.object(report.rt_runtime_scope, "current_runtime_sample_scope", return_value={"mode": "runtime_scope_unavailable"}):
            payload = report.build_report([legacy, current], klines_by_symbol=klines, horizons=(1,))

        self.assertEqual(payload["sample_scope"]["mode"], "latest_strategy_config_and_watchlist")
        self.assertEqual(payload["sample_scope"]["excluded_alert_count"], 1)
        self.assertEqual(payload["sample_scope"]["excluded_directional_alert_count"], 1)
        self.assertEqual(payload["raw_alert_count"], 1)
        self.assertEqual(payload["directional_alert_count"], 1)
        self.assertEqual(payload["evaluated_signal_count"], 1)
        self.assertEqual(payload["resolved_signal_count"], 1)
        self.assertEqual(payload["counts"]["missing_watchlist_metadata_count"], 0)
        self.assertEqual(payload["counts"]["missing_strategy_config_metadata_count"], 0)
        self.assertEqual(payload["by_strategy_config"][0]["key"], "cfg-a")
        self.assertEqual(payload["by_watchlist"][0]["key"], "wl-a")

    def test_current_sample_scope_prefers_runtime_strategy_watchlist_over_latest_old_alert(self):
        runtime = alert("runtime", "MSFT", "BUY", strategy_config_id="cfg-runtime", watchlist_id="wl-current")
        old_latest = alert("old-latest", "AAPL", "BUY", strategy_config_id="cfg-old", watchlist_id="wl-current")
        klines = {
            "AAPL": [{"date": "2026-06-11", "open": 100, "high": 112, "low": 99, "close": 110}],
            "MSFT": [{"date": "2026-06-11", "open": 100, "high": 112, "low": 99, "close": 110}],
        }
        runtime_scope = {
            "mode": "runtime_strategy_config_and_watchlist",
            "strategy_config_id": "cfg-runtime",
            "watchlist_id": "wl-current",
        }

        with patch.object(report.rt_runtime_scope, "current_runtime_sample_scope", return_value=runtime_scope):
            payload = report.build_report([runtime, old_latest], klines_by_symbol=klines, horizons=(1,))

        self.assertEqual(payload["sample_scope"]["mode"], "runtime_strategy_config_and_watchlist")
        self.assertEqual(payload["sample_scope"]["strategy_config_id"], "cfg-runtime")
        self.assertEqual(payload["sample_scope"]["excluded_alert_count"], 1)
        self.assertEqual(payload["raw_alert_count"], 1)
        self.assertEqual(payload["evaluated_signal_count"], 1)


if __name__ == "__main__":
    unittest.main()
