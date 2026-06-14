import unittest

from scripts import event_catalyst_signal_report as report


def event_payload(status="OK", candidates=None):
    return {
        "schema": "event_catalyst_report_v1",
        "status": status,
        "generated_at": "2026-06-12T10:00:00",
        "candidates": candidates or [],
    }


def catalyst(**overrides):
    payload = {
        "id": "cat-1",
        "scope": "symbol",
        "category": "news",
        "title": "TSLA positive delivery surprise",
        "summary": "Deliveries beat expectations.",
        "published_at": "2026-06-12T09:55:00",
        "age_minutes": 10,
        "sentiment": "positive",
        "impact_score": 0.85,
        "matched_symbols": ["TSLA"],
        "matched_markets": [],
        "url": "https://example.test/tsla",
    }
    payload.update(overrides)
    return payload


def alert(symbol="TSLA", side="BUY", **overrides):
    payload = {
        "signal_id": f"sig-{symbol}-{side}",
        "symbol": symbol,
        "market": "US",
        "signal_type": side,
        "trigger": "站上MA5",
        "confirmed": True,
        "full_score": 0.8 if side == "BUY" else -0.8,
        "generated_at": "2026-06-12T10:01:00",
        "strategy_config_id": "cfg",
        "watchlist_id": "wl",
    }
    payload.update(overrides)
    return payload


class EventCatalystSignalReportTests(unittest.TestCase):
    def test_positive_symbol_event_supports_related_buy_alert(self):
        payload = report.build_report(
            event_catalysts=event_payload(candidates=[catalyst()]),
            alerts=[alert()],
        )

        self.assertEqual(payload["schema"], "event_catalyst_signal_report_v1")
        self.assertEqual(payload["status"], "OK")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["writes_alert_queue"])
        self.assertEqual(payload["summary"]["signal_count"], 1)
        signal = payload["signals"][0]
        self.assertEqual(signal["review_signal_type"], "SUPPORT_BUY_REVIEW")
        self.assertEqual(signal["related_v5_signal_ids"], ["sig-TSLA-BUY"])
        self.assertFalse(signal["execution_candidate"])
        self.assertFalse(signal["eligible_for_order_intake"])

    def test_negative_symbol_event_challenges_related_buy_alert_and_sets_risk(self):
        payload = report.build_report(
            event_catalysts=event_payload(
                status="RISK",
                candidates=[catalyst(id="cat-neg", sentiment="negative", title="TSLA regulatory probe")],
            ),
            alerts=[alert()],
        )

        self.assertEqual(payload["status"], "RISK")
        signal = payload["signals"][0]
        self.assertEqual(signal["review_signal_type"], "CHALLENGE_BUY_REVIEW")
        self.assertEqual(signal["priority"], "critical")
        self.assertIn(
            "require_hermes_to_challenge_related_buy_signals_with_negative_event_context",
            payload["recommendations"],
        )

    def test_negative_symbol_event_supports_related_sell_alert(self):
        payload = report.build_report(
            event_catalysts=event_payload(
                status="RISK",
                candidates=[catalyst(id="cat-neg", sentiment="negative")],
            ),
            alerts=[alert(side="SELL")],
        )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["signals"][0]["review_signal_type"], "SUPPORT_SELL_REVIEW")

    def test_market_event_matches_alert_market_when_no_symbol_scope(self):
        payload = report.build_report(
            event_catalysts=event_payload(
                candidates=[
                    catalyst(
                        id="macro-us",
                        scope="market",
                        sentiment="negative",
                        matched_symbols=[],
                        matched_markets=["US"],
                    )
                ]
            ),
            alerts=[alert(symbol="NVDA", side="BUY")],
        )

        self.assertEqual(payload["status"], "RISK")
        self.assertEqual(payload["signals"][0]["markets"], ["US"])
        self.assertEqual(payload["signals"][0]["related_v5_signal_ids"], ["sig-NVDA-BUY"])

    def test_positive_market_event_splits_buy_support_from_sell_challenge(self):
        payload = report.build_report(
            event_catalysts=event_payload(
                candidates=[
                    catalyst(
                        id="macro-us",
                        scope="market",
                        sentiment="positive",
                        matched_symbols=[],
                        matched_markets=["US"],
                    )
                ]
            ),
            alerts=[
                alert(symbol="AAPL", side="BUY", signal_id="sig-AAPL-BUY"),
                alert(symbol="MSFT", side="SELL", signal_id="sig-MSFT-SELL"),
            ],
        )

        by_type = {signal["review_signal_type"]: signal for signal in payload["signals"]}
        self.assertEqual(payload["summary"]["signal_count"], 2)
        self.assertEqual(payload["summary"]["related_v5_signal_count"], 2)
        self.assertEqual(by_type["SUPPORT_BUY_REVIEW"]["related_v5_signal_ids"], ["sig-AAPL-BUY"])
        self.assertEqual(by_type["CHALLENGE_SELL_REVIEW"]["related_v5_signal_ids"], ["sig-MSFT-SELL"])

    def test_negative_market_event_splits_buy_challenge_from_sell_support(self):
        payload = report.build_report(
            event_catalysts=event_payload(
                status="RISK",
                candidates=[
                    catalyst(
                        id="macro-us",
                        scope="market",
                        sentiment="negative",
                        matched_symbols=[],
                        matched_markets=["US"],
                    )
                ],
            ),
            alerts=[
                alert(symbol="AAPL", side="BUY", signal_id="sig-AAPL-BUY"),
                alert(symbol="MSFT", side="SELL", signal_id="sig-MSFT-SELL"),
            ],
        )

        by_type = {signal["review_signal_type"]: signal for signal in payload["signals"]}
        self.assertEqual(payload["status"], "RISK")
        self.assertEqual(payload["summary"]["signal_count"], 2)
        self.assertEqual(payload["summary"]["related_v5_signal_count"], 2)
        self.assertEqual(by_type["CHALLENGE_BUY_REVIEW"]["related_v5_signal_ids"], ["sig-AAPL-BUY"])
        self.assertEqual(by_type["SUPPORT_SELL_REVIEW"]["related_v5_signal_ids"], ["sig-MSFT-SELL"])
        self.assertEqual(
            {alert["signal_type"] for alert in by_type["CHALLENGE_BUY_REVIEW"]["related_v5_alerts"]},
            {"BUY"},
        )
        self.assertEqual(
            {alert["signal_type"] for alert in by_type["SUPPORT_SELL_REVIEW"]["related_v5_alerts"]},
            {"SELL"},
        )

    def test_current_scope_filters_old_strategy_and_watchlist_alerts(self):
        old_scope_alert = alert(
            symbol="NVDA",
            signal_id="sig-old-scope",
            strategy_config_id="cfg-old",
            watchlist_id="wl-old",
            generated_at="2026-06-12T09:58:00",
        )
        current_scope_alert = alert(
            symbol="AAPL",
            signal_id="sig-current-scope",
            strategy_config_id="cfg-current",
            watchlist_id="wl-current",
            generated_at="2026-06-12T10:01:00",
        )

        payload = report.build_report(
            event_catalysts=event_payload(
                candidates=[
                    catalyst(
                        id="macro-us",
                        scope="market",
                        sentiment="negative",
                        matched_symbols=[],
                        matched_markets=["US"],
                    )
                ]
            ),
            alerts=[old_scope_alert, current_scope_alert],
        )

        signal = payload["signals"][0]
        self.assertEqual(signal["related_v5_signal_ids"], ["sig-current-scope"])
        self.assertEqual(payload["summary"]["alert_sample_scope"]["mode"], "latest_strategy_config_and_watchlist")
        self.assertEqual(payload["summary"]["alert_sample_scope"]["strategy_config_id"], "cfg-current")
        self.assertEqual(payload["summary"]["alert_sample_scope"]["excluded_directional_alert_count"], 1)

    def test_alerts_outside_event_window_are_not_related(self):
        payload = report.build_report(
            event_catalysts=event_payload(candidates=[catalyst()]),
            alerts=[
                alert(
                    signal_id="sig-old",
                    generated_at="2026-06-12T01:00:00",
                )
            ],
            alert_window_minutes=60,
        )

        signal = payload["signals"][0]
        self.assertEqual(signal["review_signal_type"], "POSITIVE_CATALYST_REVIEW")
        self.assertEqual(signal["related_v5_signal_ids"], [])

    def test_related_alerts_are_deduped_and_limited_by_nearest_event_time(self):
        payload = report.build_report(
            event_catalysts=event_payload(candidates=[catalyst(id="macro-us", scope="market", matched_symbols=[], matched_markets=["US"])]),
            alerts=[
                alert(symbol="AAPL", signal_id="sig-1", generated_at="2026-06-12T08:00:00"),
                alert(symbol="MSFT", signal_id="sig-2", generated_at="2026-06-12T09:57:00"),
                alert(symbol="NVDA", signal_id="sig-2", generated_at="2026-06-12T09:58:00"),
                alert(symbol="AMZN", signal_id="sig-3", generated_at="2026-06-12T09:59:00"),
            ],
            max_related_alerts=2,
            sample_scope_mode="all",
        )

        signal = payload["signals"][0]
        self.assertEqual(signal["related_v5_signal_ids"], ["sig-2", "sig-3"])
        self.assertEqual(signal["related_v5_alerts"][0]["symbol"], "NVDA")
        self.assertEqual(signal["related_v5_alerts"][0]["relevance_reason"], "market_match")

    def test_related_count_counts_unique_alert_ids_after_side_split(self):
        payload = report.build_report(
            event_catalysts=event_payload(
                status="RISK",
                candidates=[
                    catalyst(
                        id="macro-us",
                        scope="market",
                        sentiment="negative",
                        matched_symbols=[],
                        matched_markets=["US"],
                    ),
                    catalyst(
                        id="macro-us-2",
                        scope="market",
                        sentiment="mixed",
                        matched_symbols=[],
                        matched_markets=["US"],
                    ),
                ],
            ),
            alerts=[alert(symbol="AAPL", side="BUY", signal_id="sig-AAPL-BUY")],
        )

        self.assertEqual(payload["summary"]["signal_count"], 2)
        self.assertEqual(payload["summary"]["related_v5_signal_count"], 1)

    def test_missing_event_catalyst_report_stays_missing(self):
        payload = report.build_report(event_catalysts={}, alerts=[])

        self.assertEqual(payload["status"], "MISSING")
        self.assertEqual(payload["summary"]["signal_count"], 0)
        self.assertIn("wire_event_catalyst_report_before_event_signal_review", payload["recommendations"])


if __name__ == "__main__":
    unittest.main()
