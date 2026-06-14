import unittest

from scripts import event_catalyst_report as report


def external_context(status="OK", items=None):
    return {
        "schema": "external_market_context_report_v1",
        "status": status,
        "generated_at": "2026-06-12T10:30:00",
        "summary": {"fresh_item_count": len(items or [])},
        "items": items or [],
    }


def item(**extra):
    payload = {
        "id": "news-1",
        "category": "news",
        "source": "wudao",
        "title": "TSLA delivery headline",
        "summary": "Fresh company event for a watchlist symbol.",
        "published_at": "2026-06-12T10:00:00",
        "age_minutes": 15,
        "stale": False,
        "sentiment": "positive",
        "impact_score": 0.82,
        "markets": ["US"],
        "symbols": ["TSLA"],
        "tags": ["deliveries"],
    }
    payload.update(extra)
    return payload


WATCHLIST = {
    "HK": ["00700", "03690"],
    "US": ["TSLA", "NVDA"],
}


class EventCatalystReportTests(unittest.TestCase):
    def test_symbol_specific_positive_catalyst_is_reported(self):
        payload = report.build_report(
            external_context=external_context(items=[item()]),
            watchlist=WATCHLIST,
        )

        self.assertEqual(payload["schema"], "event_catalyst_report_v1")
        self.assertEqual(payload["status"], "OK")
        self.assertTrue(payload["source"]["read_only"])
        self.assertFalse(payload["source"]["submits_orders"])
        self.assertFalse(payload["source"]["changes_alert_queue"])
        self.assertEqual(payload["summary"]["candidate_count"], 1)
        self.assertEqual(payload["summary"]["symbol_candidate_count"], 1)
        self.assertEqual(payload["candidates"][0]["matched_symbols"], ["TSLA"])
        self.assertEqual(payload["candidates"][0]["hermes_use"], "symbol_specific_review")

    def test_negative_market_wide_catalyst_sets_risk(self):
        payload = report.build_report(
            external_context=external_context(
                status="RISK",
                items=[
                    item(
                        id="macro-1",
                        category="macro",
                        title="US rate shock",
                        sentiment="negative",
                        impact_score=0.9,
                        symbols=[],
                        markets=["US"],
                    )
                ],
            ),
            watchlist=WATCHLIST,
        )

        self.assertEqual(payload["status"], "RISK")
        self.assertEqual(payload["summary"]["market_candidate_count"], 1)
        self.assertEqual(payload["summary"]["negative_candidate_count"], 1)
        self.assertEqual(payload["candidates"][0]["matched_markets"], ["US"])
        self.assertEqual(payload["candidates"][0]["market_watch_symbol_count"], 2)
        self.assertIn(
            "require_hermes_explicit_risk_note_for_negative_watchlist_catalysts",
            payload["recommendations"],
        )

    def test_stale_or_low_impact_items_are_ignored(self):
        payload = report.build_report(
            external_context=external_context(
                items=[
                    item(id="stale-1", stale=True, impact_score=0.95),
                    item(id="low-1", impact_score=0.2),
                    item(id="off-watchlist", symbols=["AAPL"], markets=[]),
                    item(id="off-watchlist-market", symbols=["AAPL"], markets=["US"]),
                ],
            ),
            watchlist=WATCHLIST,
        )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["summary"]["candidate_count"], 0)
        self.assertIn("no_fresh_high_impact_watchlist_catalysts_detected", payload["recommendations"])

    def test_missing_external_context_stays_missing(self):
        payload = report.build_report(external_context={}, watchlist=WATCHLIST)

        self.assertEqual(payload["status"], "MISSING")
        self.assertEqual(payload["summary"]["candidate_count"], 0)
        self.assertIn(
            "hermes_should_not_claim_watchlist_event_awareness_without_catalysts",
            payload["recommendations"],
        )

    def test_missing_watchlist_stays_missing_even_with_external_items(self):
        payload = report.build_report(external_context=external_context(items=[item()]), watchlist={"HK": [], "US": []})

        self.assertEqual(payload["status"], "MISSING")
        self.assertEqual(payload["summary"]["watchlist_symbol_count"], 0)

    def test_external_context_fail_propagates_fail(self):
        payload = report.build_report(
            external_context=external_context(status="FAIL", items=[]),
            watchlist=WATCHLIST,
        )

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["summary"]["external_context_status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
