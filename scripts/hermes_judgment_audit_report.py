#!/usr/bin/env python3
"""Read-only audit of Hermes trade judgments against the latest review packet."""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

try:
    import rt_order_intake as intake
except ImportError:
    from scripts import rt_order_intake as intake


JUDGMENT_FILE = os.environ.get("RT_ORDER_JUDGMENT_FILE", "/tmp/hermes_trade_judgments.jsonl")
PACKET_FILE = os.environ.get("HERMES_REVIEW_PACKET_FILE", "/tmp/hermes_signal_review_packet.json")
PACKET_ARCHIVE_DIR = os.environ.get("HERMES_REVIEW_PACKET_ARCHIVE_DIR", "/tmp/hermes_review_packet_archive")
REPORT_FILE = os.environ.get("HERMES_JUDGMENT_AUDIT_FILE", "/tmp/hermes_judgment_audit_report.json")
MAX_JUDGMENT_AGE_MINUTES = int(os.environ.get("RT_ORDER_MAX_JUDGMENT_AGE_MINUTES", "240"))
REQUIRED_CONTEXT_REVIEW_FLAGS = (
    "technical_signal_reviewed",
    "portfolio_risk_reviewed",
    "strategy_evidence_reviewed",
    "data_health_reviewed",
    "execution_readiness_reviewed",
    "market_context_reviewed",
    "intraday_context_reviewed",
    "external_market_context_reviewed",
    "event_catalysts_reviewed",
    "event_catalyst_signals_reviewed",
    "market_sentiment_reviewed",
    "fundamentals_context_reviewed",
    "source_reliability_reviewed",
    "simulation_performance_reviewed",
    "cron_wiring_reviewed",
)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else default
    except Exception:
        return default


def safe_file_stem(value):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or ""))[:120]


def packet_archive_path(packet_id, archive_dir=PACKET_ARCHIVE_DIR):
    stem = safe_file_stem(packet_id)
    if not stem or not archive_dir:
        return ""
    return os.path.join(archive_dir, f"{stem}.json")


def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def decision_is_approval(judgment):
    return str(judgment.get("decision", "")).strip().lower() in ("approve", "reduce")


def packet_review_maps(packet):
    items = packet.get("review_items") if isinstance(packet, dict) else []
    by_id = {}
    eligible = set()
    for item in items or []:
        sid = str(item.get("signal_id", ""))
        if not sid:
            continue
        by_id[sid] = item
        if item.get("eligible_for_approval"):
            eligible.add(sid)
    return by_id, eligible


def packet_for_judgment(judgment, latest_packet, archive_dir=PACKET_ARCHIVE_DIR):
    packet_id = str(judgment.get("packet_id", "")).strip()
    if not packet_id:
        return latest_packet, "latest_packet_fallback", ["judgment_missing_packet_id"]

    archive_path = packet_archive_path(packet_id, archive_dir)
    archived = load_json_file(archive_path, {}) if archive_path else {}
    if archived:
        return archived, "packet_archive", []

    if isinstance(latest_packet, dict) and str(latest_packet.get("packet_id", "")) == packet_id:
        return latest_packet, "latest_packet_matching_packet_id", []

    return latest_packet, "latest_packet_fallback", ["packet_archive_missing_for_packet_id"]


def market_regime_for_item(packet, item):
    alert = item.get("alert") or {}
    market = str(alert.get("market") or "").upper()
    if market not in ("HK", "US"):
        symbol = str(alert.get("symbol", ""))
        market = "HK" if symbol[:1].isdigit() and len(symbol) == 5 else "US"
    market_payload = ((packet.get("market_context") or {}).get("markets") or {}).get(market) or {}
    return market, market_payload.get("regime"), market_payload


def market_cross_context_for_item(packet, item):
    market, _regime, market_payload = market_regime_for_item(packet, item)
    cross = market_payload.get("cross_market") if isinstance(market_payload.get("cross_market"), dict) else {}
    return market, cross


def flattened_judgment_text(judgment):
    parts = []
    for key in ("supporting_factors", "opposing_factors", "risk_notes", "event_catalyst_risk_notes"):
        value = judgment.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    review = judgment.get("context_review")
    if isinstance(review, dict):
        notes = review.get("notes")
        if isinstance(notes, list):
            parts.extend(str(item) for item in notes)
    if judgment.get("market_regime_exception_reason"):
        parts.append(str(judgment.get("market_regime_exception_reason")))
    return " ".join(parts).lower()


def list_text(value):
    if not isinstance(value, list):
        return ""
    return " ".join(str(item) for item in value if str(item).strip()).lower()


def cross_market_conflict_acknowledgement_reasons(judgment, cross_market):
    if not isinstance(cross_market, dict) or cross_market.get("alignment") != "conflicts_with_breadth":
        return []
    text = flattened_judgment_text(judgment)
    has_breadth = any(term in text for term in ("breadth", "stock-pool", "stock pool", "ma20", "大市", "廣度"))
    has_cross_market = any(term in text for term in ("cross-market", "cross market", "sentiment", "index", "etf", "vix", "risk appetite", "情緒", "指數"))
    reasons = []
    if not has_breadth:
        reasons.append("cross_market_conflict_breadth_not_discussed")
    if not has_cross_market:
        reasons.append("cross_market_conflict_sentiment_not_discussed")
    return reasons


def native_index_context_for_item(packet, item):
    _market, _regime, market_payload = market_regime_for_item(packet, item)
    native = market_payload.get("native_index_context") if isinstance(market_payload.get("native_index_context"), dict) else {}
    return native


def native_index_conflict_acknowledgement_reasons(judgment, native_index_context):
    if not isinstance(native_index_context, dict) or native_index_context.get("alignment") != "conflicts_with_breadth":
        return []
    text = flattened_judgment_text(judgment)
    has_breadth = any(term in text for term in ("breadth", "stock-pool", "stock pool", "ma20", "大市", "廣度"))
    has_native_index = any(
        term in text
        for term in ("native index", "benchmark", "index", "etf", "hsi", "hang seng", "s&p", "spx", "指數")
    )
    reasons = []
    if not has_breadth:
        reasons.append("native_index_conflict_breadth_not_discussed")
    if not has_native_index:
        reasons.append("native_index_conflict_index_not_discussed")
    return reasons


def relevant_negative_event_catalysts(packet, item):
    catalysts = packet.get("event_catalysts") if isinstance(packet.get("event_catalysts"), dict) else {}
    if str(catalysts.get("status") or "").upper() != "RISK":
        return []
    alert = item.get("alert") or {}
    symbol = str(alert.get("symbol") or "").strip().upper()
    market = str(alert.get("market") or "").strip().upper()
    if market not in ("HK", "US"):
        market = "HK" if symbol[:1].isdigit() and len(symbol) == 5 else "US"

    relevant = []
    for candidate in catalysts.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("sentiment") or "").lower() != "negative":
            continue
        scope = str(candidate.get("scope") or "").lower()
        matched_symbols = {str(value).strip().upper() for value in candidate.get("matched_symbols") or []}
        matched_markets = {str(value).strip().upper() for value in candidate.get("matched_markets") or []}
        if scope == "symbol" and symbol and symbol in matched_symbols:
            relevant.append(candidate)
        elif scope == "market" and market and market in matched_markets:
            relevant.append(candidate)
        elif not scope and (
            (symbol and symbol in matched_symbols)
            or (not matched_symbols and market and market in matched_markets)
        ):
            relevant.append(candidate)
    return relevant


def event_catalyst_acknowledgement_reasons(judgment, relevant_catalysts):
    if not relevant_catalysts:
        return []
    reasons = []
    relevant_ids = {str(item.get("id") or "").strip() for item in relevant_catalysts if item.get("id")}
    acknowledged_ids = {
        str(value).strip()
        for value in (judgment.get("event_catalyst_ids") or [])
        if str(value).strip()
    } if isinstance(judgment.get("event_catalyst_ids"), list) else set()
    if judgment.get("event_catalyst_risk_acknowledged") is not True:
        reasons.append("missing_event_catalyst_risk_acknowledgement")
    if relevant_ids and not (acknowledged_ids & relevant_ids):
        reasons.append("event_catalyst_ids_missing_or_unmatched")
    notes = judgment.get("event_catalyst_risk_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("event_catalyst_risk_notes_missing")
    return reasons


def relevant_challenge_buy_event_signals(packet, item):
    signal_report = packet.get("event_catalyst_signals") if isinstance(packet.get("event_catalyst_signals"), dict) else {}
    alert = item.get("alert") or {}
    if str(alert.get("signal_type") or "").upper() != "BUY":
        return []
    sid = str(item.get("signal_id") or alert.get("signal_id") or "").strip()
    if not sid:
        return []
    relevant = []
    for signal in signal_report.get("signals") or []:
        if not isinstance(signal, dict):
            continue
        if signal.get("review_signal_type") != "CHALLENGE_BUY_REVIEW":
            continue
        related_ids = {str(value).strip() for value in signal.get("related_v5_signal_ids") or [] if str(value).strip()}
        if sid in related_ids:
            relevant.append(signal)
    return relevant


def relevant_support_buy_event_signals(packet, item):
    signal_report = packet.get("event_catalyst_signals") if isinstance(packet.get("event_catalyst_signals"), dict) else {}
    alert = item.get("alert") or {}
    if str(alert.get("signal_type") or "").upper() != "BUY":
        return []
    sid = str(item.get("signal_id") or alert.get("signal_id") or "").strip()
    if not sid:
        return []
    relevant = []
    for signal in signal_report.get("signals") or []:
        if not isinstance(signal, dict):
            continue
        if signal.get("review_signal_type") != "SUPPORT_BUY_REVIEW":
            continue
        related_ids = {str(value).strip() for value in signal.get("related_v5_signal_ids") or [] if str(value).strip()}
        if sid in related_ids:
            relevant.append(signal)
    return relevant


def event_catalyst_signal_acknowledgement_reasons(judgment, relevant_signals):
    if not relevant_signals:
        return []
    reasons = []
    relevant_signal_ids = {str(item.get("signal_id") or "").strip() for item in relevant_signals if item.get("signal_id")}
    acknowledged_signal_ids = {
        str(value).strip()
        for value in (judgment.get("event_catalyst_signal_ids") or [])
        if str(value).strip()
    } if isinstance(judgment.get("event_catalyst_signal_ids"), list) else set()
    if judgment.get("event_catalyst_risk_acknowledged") is not True:
        reasons.append("missing_event_catalyst_signal_risk_acknowledgement")
    if relevant_signal_ids and not (acknowledged_signal_ids & relevant_signal_ids):
        reasons.append("event_catalyst_signal_ids_missing_or_unmatched")
    notes = judgment.get("event_catalyst_risk_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("event_catalyst_signal_risk_notes_missing")
    return reasons


def event_catalyst_support_acknowledgement_reasons(judgment, relevant_signals):
    if not relevant_signals:
        return []
    reasons = []
    relevant_signal_ids = {str(item.get("signal_id") or "").strip() for item in relevant_signals if item.get("signal_id")}
    acknowledged_signal_ids = {
        str(value).strip()
        for value in (judgment.get("event_catalyst_support_signal_ids") or [])
        if str(value).strip()
    } if isinstance(judgment.get("event_catalyst_support_signal_ids"), list) else set()
    if judgment.get("event_catalyst_support_acknowledged") is not True:
        reasons.append("missing_event_catalyst_support_acknowledgement")
    if relevant_signal_ids and not (acknowledged_signal_ids & relevant_signal_ids):
        reasons.append("event_catalyst_support_signal_ids_missing_or_unmatched")
    notes = judgment.get("event_catalyst_support_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("event_catalyst_support_notes_missing")
    return reasons


def event_catalyst_signal_coverage_attention(item):
    attention = set(required_attention(item))
    if "event_catalyst_signal_coverage_limit_requires_acknowledgement" not in attention:
        return {}
    digest = item.get("context_digest") if isinstance(item.get("context_digest"), dict) else {}
    event_signals = digest.get("event_catalyst_signals") if isinstance(digest.get("event_catalyst_signals"), dict) else {}
    return {
        "attention": ["event_catalyst_signal_coverage_limit_requires_acknowledgement"],
        "status": event_signals.get("status"),
    }


def event_catalyst_coverage_attention(item):
    attention = set(required_attention(item))
    if "event_catalyst_coverage_limit_requires_acknowledgement" not in attention:
        return {}
    digest = item.get("context_digest") if isinstance(item.get("context_digest"), dict) else {}
    catalysts = digest.get("event_catalysts") if isinstance(digest.get("event_catalysts"), dict) else {}
    return {
        "attention": ["event_catalyst_coverage_limit_requires_acknowledgement"],
        "status": catalysts.get("status"),
    }


def event_catalyst_coverage_acknowledgement_reasons(judgment, coverage_attention):
    if not coverage_attention:
        return []
    reasons = []
    if judgment.get("event_catalyst_coverage_acknowledged") is not True:
        reasons.append("missing_event_catalyst_coverage_acknowledgement")
    notes = judgment.get("event_catalyst_coverage_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("event_catalyst_coverage_notes_missing")
    acknowledged_status = str(judgment.get("event_catalyst_coverage_status") or "").strip().upper()
    expected_status = str(coverage_attention.get("status") or "").strip().upper()
    if expected_status and acknowledged_status and acknowledged_status != expected_status:
        reasons.append("event_catalyst_coverage_status_mismatch")
    return reasons


def event_catalyst_signal_coverage_acknowledgement_reasons(judgment, coverage_attention):
    if not coverage_attention:
        return []
    reasons = []
    if judgment.get("event_catalyst_signal_coverage_acknowledged") is not True:
        reasons.append("missing_event_catalyst_signal_coverage_acknowledgement")
    notes = judgment.get("event_catalyst_signal_coverage_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("event_catalyst_signal_coverage_notes_missing")
    acknowledged_status = str(judgment.get("event_catalyst_signal_coverage_status") or "").strip().upper()
    expected_status = str(coverage_attention.get("status") or "").strip().upper()
    if expected_status and acknowledged_status and acknowledged_status != expected_status:
        reasons.append("event_catalyst_signal_coverage_status_mismatch")
    return reasons


def market_context_coverage_attention(item):
    attention = set(required_attention(item))
    if "market_context_coverage_limit_requires_acknowledgement" not in attention:
        return {}
    digest = item.get("context_digest") if isinstance(item.get("context_digest"), dict) else {}
    market_context = digest.get("market_context") if isinstance(digest.get("market_context"), dict) else {}
    return {
        "attention": ["market_context_coverage_limit_requires_acknowledgement"],
        "status": market_context.get("status"),
    }


def market_context_coverage_acknowledgement_reasons(judgment, coverage_attention):
    if not coverage_attention:
        return []
    reasons = []
    if judgment.get("market_context_coverage_acknowledged") is not True:
        reasons.append("missing_market_context_coverage_acknowledgement")
    notes = judgment.get("market_context_coverage_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("market_context_coverage_notes_missing")
    acknowledged_status = str(judgment.get("market_context_coverage_status") or "").strip().upper()
    expected_status = str(coverage_attention.get("status") or "").strip().upper()
    if expected_status and acknowledged_status and acknowledged_status != expected_status:
        reasons.append("market_context_coverage_status_mismatch")
    return reasons


def normalize_market(value):
    text = str(value or "").strip().upper()
    if text in ("HK", "HKG", "HKEX"):
        return "HK"
    if text in ("US", "USA", "NYSE", "NASDAQ"):
        return "US"
    if text in ("GLOBAL", "ALL"):
        return text
    return text


def sentiment_item_markets(item):
    values = []
    for key in ("markets", "market", "matched_markets"):
        value = (item or {}).get(key)
        if isinstance(value, list):
            values.extend(value)
        elif value not in (None, ""):
            values.append(value)
    return {normalize_market(value) for value in values if normalize_market(value)}


def sentiment_item_is_relevant(item, market):
    markets = sentiment_item_markets(item)
    return not markets or "GLOBAL" in markets or "ALL" in markets or market in markets


def sentiment_item_is_risk(item):
    direction = str((item or {}).get("direction") or (item or {}).get("sentiment") or "").strip().lower()
    score = as_float((item or {}).get("score"), 0.0)
    return direction in ("risk_off", "negative") or (score is not None and score < 0)


def sentiment_item_is_positive_support(item):
    direction = str((item or {}).get("direction") or (item or {}).get("sentiment") or "").strip().lower()
    score = as_float((item or {}).get("score"), 0.0)
    return (
        direction in ("risk_on", "positive")
        and (item or {}).get("stale") is not True
        and score is not None
        and score >= 0.25
    )


def relevant_risk_market_sentiment(packet, item):
    alert = item.get("alert") or {}
    if str(alert.get("signal_type") or "").upper() != "BUY":
        return []
    symbol = str(alert.get("symbol") or "").strip()
    market = normalize_market(alert.get("market"))
    if market not in ("HK", "US"):
        market = "HK" if symbol[:1].isdigit() and len(symbol) == 5 else "US"

    rows = []
    seen = set()
    digest = item.get("context_digest") if isinstance(item.get("context_digest"), dict) else {}
    digest_sentiment = digest.get("market_sentiment") if isinstance(digest.get("market_sentiment"), dict) else {}
    sources = []
    if isinstance(digest_sentiment.get("indicators"), list):
        sources.append(digest_sentiment.get("indicators") or [])
    sentiment_payload = packet.get("market_sentiment") if isinstance(packet.get("market_sentiment"), dict) else {}
    if isinstance(sentiment_payload.get("indicators"), list):
        sources.append(sentiment_payload.get("indicators") or [])

    for indicators in sources:
        for indicator in indicators:
            if not isinstance(indicator, dict):
                continue
            key = (
                str(indicator.get("id") or indicator.get("name") or indicator.get("indicator_type") or ""),
                str(indicator.get("observed_at") or indicator.get("published_at") or ""),
                tuple(sorted(sentiment_item_markets(indicator))),
            )
            if key in seen:
                continue
            seen.add(key)
            if sentiment_item_is_relevant(indicator, market) and sentiment_item_is_risk(indicator):
                rows.append(indicator)
    return rows


def relevant_positive_market_sentiment(packet, item):
    alert = item.get("alert") or {}
    if str(alert.get("signal_type") or "").upper() != "BUY":
        return []
    symbol = str(alert.get("symbol") or "").strip()
    market = normalize_market(alert.get("market"))
    if market not in ("HK", "US"):
        market = "HK" if symbol[:1].isdigit() and len(symbol) == 5 else "US"

    rows = []
    seen = set()
    digest = item.get("context_digest") if isinstance(item.get("context_digest"), dict) else {}
    digest_sentiment = digest.get("market_sentiment") if isinstance(digest.get("market_sentiment"), dict) else {}
    sources = []
    if isinstance(digest_sentiment.get("indicators"), list):
        sources.append(digest_sentiment.get("indicators") or [])
    sentiment_payload = packet.get("market_sentiment") if isinstance(packet.get("market_sentiment"), dict) else {}
    if isinstance(sentiment_payload.get("indicators"), list):
        sources.append(sentiment_payload.get("indicators") or [])

    for indicators in sources:
        for indicator in indicators:
            if not isinstance(indicator, dict):
                continue
            key = (
                str(indicator.get("id") or indicator.get("name") or indicator.get("indicator_type") or ""),
                str(indicator.get("observed_at") or indicator.get("published_at") or ""),
                tuple(sorted(sentiment_item_markets(indicator))),
            )
            if key in seen:
                continue
            seen.add(key)
            if sentiment_item_is_relevant(indicator, market) and sentiment_item_is_positive_support(indicator):
                rows.append(indicator)
    return rows


def market_sentiment_acknowledgement_reasons(judgment, relevant_indicators):
    if not relevant_indicators:
        return []
    reasons = []
    if judgment.get("market_sentiment_risk_acknowledged") is not True:
        reasons.append("missing_market_sentiment_risk_acknowledgement")

    expected_ids = {
        str(item.get("id") or item.get("name") or item.get("indicator_type") or "").strip()
        for item in relevant_indicators
        if str(item.get("id") or item.get("name") or item.get("indicator_type") or "").strip()
    }
    acknowledged_ids = {
        str(value).strip()
        for value in (judgment.get("market_sentiment_indicator_ids") or [])
        if str(value).strip()
    } if isinstance(judgment.get("market_sentiment_indicator_ids"), list) else set()
    if expected_ids and not (acknowledged_ids & expected_ids):
        reasons.append("market_sentiment_indicator_ids_missing_or_unmatched")

    notes = judgment.get("market_sentiment_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("market_sentiment_notes_missing")
    return reasons


def market_sentiment_support_acknowledgement_reasons(judgment, relevant_indicators):
    if not relevant_indicators:
        return []
    reasons = []
    if judgment.get("market_sentiment_support_acknowledged") is not True:
        reasons.append("missing_market_sentiment_support_acknowledgement")

    expected_ids = {
        str(item.get("id") or item.get("name") or item.get("indicator_type") or "").strip()
        for item in relevant_indicators
        if str(item.get("id") or item.get("name") or item.get("indicator_type") or "").strip()
    }
    acknowledged_ids = {
        str(value).strip()
        for value in (judgment.get("market_sentiment_support_indicator_ids") or [])
        if str(value).strip()
    } if isinstance(judgment.get("market_sentiment_support_indicator_ids"), list) else set()
    if expected_ids and not (acknowledged_ids & expected_ids):
        reasons.append("market_sentiment_support_indicator_ids_missing_or_unmatched")

    notes = judgment.get("market_sentiment_support_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("market_sentiment_support_notes_missing")
    return reasons


def market_sentiment_coverage_attention(item):
    attention = set(required_attention(item))
    if "market_sentiment_coverage_limit_requires_acknowledgement" not in attention:
        return {}
    digest = item.get("context_digest") if isinstance(item.get("context_digest"), dict) else {}
    sentiment = digest.get("market_sentiment") if isinstance(digest.get("market_sentiment"), dict) else {}
    return {
        "attention": ["market_sentiment_coverage_limit_requires_acknowledgement"],
        "status": sentiment.get("status"),
    }


def market_sentiment_coverage_acknowledgement_reasons(judgment, coverage_attention):
    if not coverage_attention:
        return []
    reasons = []
    if judgment.get("market_sentiment_coverage_acknowledged") is not True:
        reasons.append("missing_market_sentiment_coverage_acknowledgement")
    notes = judgment.get("market_sentiment_coverage_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("market_sentiment_coverage_notes_missing")
    acknowledged_status = str(judgment.get("market_sentiment_coverage_status") or "").strip().upper()
    expected_status = str(coverage_attention.get("status") or "").strip().upper()
    if expected_status and acknowledged_status and acknowledged_status != expected_status:
        reasons.append("market_sentiment_coverage_status_mismatch")
    return reasons


def symbol_tokens(value, market=None):
    raw = str(value or "").strip().upper()
    if not raw:
        return set()
    tokens = {raw}
    base = raw
    for suffix in (".HK", ".US"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            tokens.add(base)
            break
    if base.isdigit():
        stripped = base.lstrip("0") or "0"
        tokens.add(stripped)
        tokens.add(base.zfill(4))
        tokens.add(base.zfill(5))
        if normalize_market(market) == "HK":
            tokens.add(f"{base.zfill(4)}.HK")
            tokens.add(f"{base.zfill(5)}.HK")
    return {token for token in tokens if token}


def external_item_symbols(item, market=None):
    values = []
    for key in ("symbols", "symbol", "tickers", "ticker", "matched_symbols"):
        value = (item or {}).get(key)
        if isinstance(value, list):
            values.extend(value)
        elif value not in (None, ""):
            values.append(value)
    tokens = set()
    for value in values:
        tokens.update(symbol_tokens(value, market=market))
    return tokens


def external_item_is_relevant(item, symbol, market):
    item_symbols = external_item_symbols(item, market=market)
    alert_symbols = symbol_tokens(symbol, market=market)
    if item_symbols and alert_symbols and item_symbols & alert_symbols:
        return True
    return sentiment_item_is_relevant(item, market)


def external_item_is_negative(item):
    sentiment = str((item or {}).get("sentiment") or (item or {}).get("direction") or "").strip().lower()
    score = as_float((item or {}).get("impact_score"), as_float((item or {}).get("score"), 0.0))
    return sentiment == "negative" and (score is None or score >= 0)


def external_item_is_positive_support(item):
    sentiment = str((item or {}).get("sentiment") or (item or {}).get("direction") or "").strip().lower()
    score = as_float((item or {}).get("impact_score"), as_float((item or {}).get("score"), 0.0))
    return sentiment == "positive" and (item or {}).get("stale") is not True and score is not None and score >= 0.7


def relevant_negative_external_context(packet, item):
    alert = item.get("alert") or {}
    if str(alert.get("signal_type") or "").upper() != "BUY":
        return []
    symbol = str(alert.get("symbol") or "").strip()
    market = normalize_market(alert.get("market"))
    if market not in ("HK", "US"):
        market = "HK" if symbol[:1].isdigit() and len(symbol) == 5 else "US"

    rows = []
    seen = set()
    digest = item.get("context_digest") if isinstance(item.get("context_digest"), dict) else {}
    digest_external = digest.get("external_market_context") if isinstance(digest.get("external_market_context"), dict) else {}
    sources = []
    if isinstance(digest_external.get("items"), list):
        sources.append(digest_external.get("items") or [])
    external_payload = packet.get("external_market_context") if isinstance(packet.get("external_market_context"), dict) else {}
    if isinstance(external_payload.get("items"), list):
        sources.append(external_payload.get("items") or [])

    for external_items in sources:
        for external_item in external_items:
            if not isinstance(external_item, dict):
                continue
            key = (
                str(external_item.get("id") or external_item.get("title") or external_item.get("url") or ""),
                str(external_item.get("published_at") or external_item.get("observed_at") or ""),
                tuple(sorted(sentiment_item_markets(external_item))),
                tuple(sorted(external_item_symbols(external_item, market=market))),
            )
            if key in seen:
                continue
            seen.add(key)
            if external_item_is_relevant(external_item, symbol, market) and external_item_is_negative(external_item):
                rows.append(external_item)
    return rows


def relevant_positive_external_context(packet, item):
    alert = item.get("alert") or {}
    if str(alert.get("signal_type") or "").upper() != "BUY":
        return []
    symbol = str(alert.get("symbol") or "").strip()
    market = normalize_market(alert.get("market"))
    if market not in ("HK", "US"):
        market = "HK" if symbol[:1].isdigit() and len(symbol) == 5 else "US"

    rows = []
    seen = set()
    digest = item.get("context_digest") if isinstance(item.get("context_digest"), dict) else {}
    digest_external = digest.get("external_market_context") if isinstance(digest.get("external_market_context"), dict) else {}
    sources = []
    if isinstance(digest_external.get("items"), list):
        sources.append(digest_external.get("items") or [])
    external_payload = packet.get("external_market_context") if isinstance(packet.get("external_market_context"), dict) else {}
    if isinstance(external_payload.get("items"), list):
        sources.append(external_payload.get("items") or [])

    for external_items in sources:
        for external_item in external_items:
            if not isinstance(external_item, dict):
                continue
            key = (
                str(external_item.get("id") or external_item.get("title") or external_item.get("url") or ""),
                str(external_item.get("published_at") or external_item.get("observed_at") or ""),
                tuple(sorted(sentiment_item_markets(external_item))),
                tuple(sorted(external_item_symbols(external_item, market=market))),
            )
            if key in seen:
                continue
            seen.add(key)
            if external_item_is_relevant(external_item, symbol, market) and external_item_is_positive_support(external_item):
                rows.append(external_item)
    return rows


def external_context_acknowledgement_reasons(judgment, relevant_items):
    if not relevant_items:
        return []
    reasons = []
    if judgment.get("external_market_context_risk_acknowledged") is not True:
        reasons.append("missing_external_market_context_risk_acknowledgement")

    expected_ids = {
        str(item.get("id") or item.get("title") or item.get("url") or "").strip()
        for item in relevant_items
        if str(item.get("id") or item.get("title") or item.get("url") or "").strip()
    }
    acknowledged_ids = {
        str(value).strip()
        for value in (judgment.get("external_market_context_ids") or [])
        if str(value).strip()
    } if isinstance(judgment.get("external_market_context_ids"), list) else set()
    if expected_ids and not (acknowledged_ids & expected_ids):
        reasons.append("external_market_context_ids_missing_or_unmatched")

    notes = judgment.get("external_market_context_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("external_market_context_notes_missing")
    return reasons


def external_context_support_acknowledgement_reasons(judgment, relevant_items):
    if not relevant_items:
        return []
    reasons = []
    if judgment.get("external_market_context_support_acknowledged") is not True:
        reasons.append("missing_external_market_context_support_acknowledgement")

    expected_ids = {
        str(item.get("id") or item.get("title") or item.get("url") or "").strip()
        for item in relevant_items
        if str(item.get("id") or item.get("title") or item.get("url") or "").strip()
    }
    acknowledged_ids = {
        str(value).strip()
        for value in (judgment.get("external_market_context_support_ids") or [])
        if str(value).strip()
    } if isinstance(judgment.get("external_market_context_support_ids"), list) else set()
    if expected_ids and not (acknowledged_ids & expected_ids):
        reasons.append("external_market_context_support_ids_missing_or_unmatched")

    notes = judgment.get("external_market_context_support_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("external_market_context_support_notes_missing")
    return reasons


def external_market_context_coverage_attention(item):
    attention = set(required_attention(item))
    if "external_market_context_coverage_limit_requires_acknowledgement" not in attention:
        return {}
    digest = item.get("context_digest") if isinstance(item.get("context_digest"), dict) else {}
    external = digest.get("external_market_context") if isinstance(digest.get("external_market_context"), dict) else {}
    return {
        "attention": ["external_market_context_coverage_limit_requires_acknowledgement"],
        "status": external.get("status"),
    }


def external_market_context_coverage_acknowledgement_reasons(judgment, coverage_attention):
    if not coverage_attention:
        return []
    reasons = []
    if judgment.get("external_market_context_coverage_acknowledged") is not True:
        reasons.append("missing_external_market_context_coverage_acknowledgement")
    notes = judgment.get("external_market_context_coverage_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("external_market_context_coverage_notes_missing")
    acknowledged_status = str(judgment.get("external_market_context_coverage_status") or "").strip().upper()
    expected_status = str(coverage_attention.get("status") or "").strip().upper()
    if expected_status and acknowledged_status and acknowledged_status != expected_status:
        reasons.append("external_market_context_coverage_status_mismatch")
    return reasons


def relevant_partial_fundamentals(packet, item):
    fundamentals = packet.get("fundamentals_context") if isinstance(packet.get("fundamentals_context"), dict) else {}
    if str(fundamentals.get("schema") or "") != "fundamentals_context_report_v1":
        return []
    alert = item.get("alert") or {}
    if str(alert.get("signal_type") or "").upper() != "BUY":
        return []
    symbol = str(alert.get("symbol") or "").strip().upper()
    if not symbol:
        return []
    relevant = []
    for row in fundamentals.get("items") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").strip().upper() != symbol:
            continue
        completeness = row.get("fundamental_completeness") if isinstance(row.get("fundamental_completeness"), dict) else {}
        flags = {str(flag) for flag in row.get("valuation_flags") or []}
        source = str(row.get("source") or "")
        if (
            completeness.get("level") in ("partial", "empty")
            or "partial_fundamentals" in flags
            or source == "tencent_quote_snapshot"
        ):
            relevant.append(row)
    return relevant


def relevant_supportive_fundamentals(packet, item):
    fundamentals = packet.get("fundamentals_context") if isinstance(packet.get("fundamentals_context"), dict) else {}
    if str(fundamentals.get("schema") or "") != "fundamentals_context_report_v1":
        return []
    alert = item.get("alert") or {}
    if str(alert.get("signal_type") or "").upper() != "BUY":
        return []
    symbol = str(alert.get("symbol") or "").strip().upper()
    if not symbol:
        return []
    relevant = []
    for row in fundamentals.get("items") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").strip().upper() != symbol:
            continue
        completeness = row.get("fundamental_completeness") if isinstance(row.get("fundamental_completeness"), dict) else {}
        flags = {str(flag) for flag in row.get("valuation_flags") or []}
        if row.get("stale") is not True and completeness.get("level") == "full" and not flags:
            relevant.append(row)
    return relevant


def fundamentals_limit_acknowledgement_reasons(judgment, relevant_rows):
    if not relevant_rows:
        return []
    reasons = []
    if judgment.get("fundamentals_context_limit_acknowledged") is not True:
        reasons.append("missing_fundamentals_context_limit_acknowledgement")
    expected_symbols = {str(row.get("symbol") or "").strip().upper() for row in relevant_rows if row.get("symbol")}
    acknowledged_symbols = {
        str(value).strip().upper()
        for value in (judgment.get("fundamentals_context_symbols") or [])
        if str(value).strip()
    } if isinstance(judgment.get("fundamentals_context_symbols"), list) else set()
    if expected_symbols and not (acknowledged_symbols & expected_symbols):
        reasons.append("fundamentals_context_symbols_missing_or_unmatched")

    expected_missing = set()
    for row in relevant_rows:
        completeness = row.get("fundamental_completeness") if isinstance(row.get("fundamental_completeness"), dict) else {}
        expected_missing.update(str(value) for value in completeness.get("missing_metrics") or [] if str(value))
    acknowledged_missing = {
        str(value).strip()
        for value in (judgment.get("fundamentals_context_missing_metrics") or [])
        if str(value).strip()
    } if isinstance(judgment.get("fundamentals_context_missing_metrics"), list) else set()
    if expected_missing and not (acknowledged_missing & expected_missing):
        reasons.append("fundamentals_context_missing_metrics_not_discussed")

    notes = judgment.get("fundamentals_context_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("fundamentals_context_notes_missing")
    return reasons


def fundamentals_support_acknowledgement_reasons(judgment, relevant_rows):
    if not relevant_rows:
        return []
    reasons = []
    if judgment.get("fundamentals_context_support_acknowledged") is not True:
        reasons.append("missing_fundamentals_context_support_acknowledgement")

    expected_symbols = {
        str(row.get("symbol") or "").strip().upper()
        for row in relevant_rows
        if str(row.get("symbol") or "").strip()
    }
    acknowledged_symbols = {
        str(value).strip().upper()
        for value in (judgment.get("fundamentals_context_support_symbols") or [])
        if str(value).strip()
    } if isinstance(judgment.get("fundamentals_context_support_symbols"), list) else set()
    if expected_symbols and not (acknowledged_symbols & expected_symbols):
        reasons.append("fundamentals_context_support_symbols_missing_or_unmatched")

    expected_metrics = set()
    for row in relevant_rows:
        completeness = row.get("fundamental_completeness") if isinstance(row.get("fundamental_completeness"), dict) else {}
        expected_metrics.update(str(value) for value in completeness.get("available_metrics") or [] if str(value))
    acknowledged_metrics = {
        str(value).strip()
        for value in (judgment.get("fundamentals_context_support_metrics") or [])
        if str(value).strip()
    } if isinstance(judgment.get("fundamentals_context_support_metrics"), list) else set()
    if expected_metrics and not (acknowledged_metrics & expected_metrics):
        reasons.append("fundamentals_context_support_metrics_missing_or_unmatched")

    notes = judgment.get("fundamentals_context_support_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("fundamentals_context_support_notes_missing")
    return reasons


def fundamentals_context_coverage_attention(item):
    attention = set(required_attention(item))
    if "fundamentals_context_coverage_limit_requires_acknowledgement" not in attention:
        return {}
    digest = item.get("context_digest") if isinstance(item.get("context_digest"), dict) else {}
    fundamentals = digest.get("fundamentals_context") if isinstance(digest.get("fundamentals_context"), dict) else {}
    return {
        "attention": ["fundamentals_context_coverage_limit_requires_acknowledgement"],
        "status": fundamentals.get("status"),
    }


def fundamentals_context_coverage_acknowledgement_reasons(judgment, coverage_attention):
    if not coverage_attention:
        return []
    reasons = []
    if judgment.get("fundamentals_context_coverage_acknowledged") is not True:
        reasons.append("missing_fundamentals_context_coverage_acknowledgement")
    notes = judgment.get("fundamentals_context_coverage_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("fundamentals_context_coverage_notes_missing")
    acknowledged_status = str(judgment.get("fundamentals_context_coverage_status") or "").strip().upper()
    expected_status = str(coverage_attention.get("status") or "").strip().upper()
    if expected_status and acknowledged_status and acknowledged_status != expected_status:
        reasons.append("fundamentals_context_coverage_status_mismatch")
    return reasons


def degraded_source_reliability(packet):
    payload = packet.get("source_reliability") if isinstance(packet.get("source_reliability"), dict) else {}
    if not payload:
        return {}
    status = str(payload.get("status") or "").upper()
    degraded_statuses = {"DEGRADED", "STALE", "MISSING", "FAIL"}
    components = []
    for component in payload.get("components") or []:
        if not isinstance(component, dict):
            continue
        reliability_status = str(component.get("reliability_status") or component.get("status") or "").upper()
        reasons = [
            str(reason)
            for reason in (component.get("reasons") or [])
            if str(reason).strip()
        ]
        if reliability_status in degraded_statuses or reasons:
            components.append(
                {
                    "name": str(component.get("name") or component.get("component") or "").strip(),
                    "status": reliability_status,
                    "reasons": reasons,
                }
            )
    top_reasons = [
        str(reason)
        for reason in (payload.get("recommendations") or payload.get("reasons") or [])
        if str(reason).strip()
    ]
    if status in degraded_statuses or components:
        return {
            "status": status or "UNKNOWN",
            "components": components,
            "reasons": top_reasons,
        }
    return {}


def source_reliability_acknowledgement_reasons(judgment, source_reliability):
    if not source_reliability:
        return []
    reasons = []
    if judgment.get("source_reliability_limit_acknowledged") is not True:
        reasons.append("missing_source_reliability_limit_acknowledgement")

    expected_reasons = set(source_reliability.get("reasons") or [])
    expected_components = set()
    for component in source_reliability.get("components") or []:
        name = str(component.get("name") or "").strip()
        if name:
            expected_components.add(name)
        expected_reasons.update(component.get("reasons") or [])

    acknowledged_reasons = {
        str(value).strip()
        for value in (judgment.get("source_reliability_reasons") or [])
        if str(value).strip()
    } if isinstance(judgment.get("source_reliability_reasons"), list) else set()
    acknowledged_components = {
        str(value).strip()
        for value in (judgment.get("source_reliability_components") or [])
        if str(value).strip()
    } if isinstance(judgment.get("source_reliability_components"), list) else set()

    if expected_reasons and not (acknowledged_reasons & expected_reasons):
        reasons.append("source_reliability_reasons_missing_or_unmatched")
    if expected_components and not (acknowledged_components & expected_components):
        reasons.append("source_reliability_components_missing_or_unmatched")

    notes = judgment.get("source_reliability_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("source_reliability_notes_missing")
    return reasons


def weak_simulation_performance(packet):
    payload = packet.get("simulation_performance") if isinstance(packet.get("simulation_performance"), dict) else {}
    if not payload:
        return {}
    if payload.get("schema") != "simulation_performance_report_v1":
        return {}
    status = str(payload.get("status") or "").strip().upper()
    if status not in ("WARN", "FAIL"):
        return {}
    reason_codes = [
        str(reason)
        for reason in (payload.get("reason_codes") or [])
        if str(reason).strip()
    ]
    recommendations = [
        str(reason)
        for reason in (payload.get("recommendations") or [])
        if str(reason).strip()
    ]
    remediation = payload.get("remediation_plan") if isinstance(payload.get("remediation_plan"), dict) else {}
    return {
        "status": status,
        "reason_codes": reason_codes,
        "recommendations": recommendations,
        "remediation_plan_hash": remediation.get("proposal_hash"),
    }


def simulation_performance_acknowledgement_reasons(judgment, simulation_performance):
    if not simulation_performance:
        return []
    reasons = []
    if judgment.get("simulation_performance_acknowledged") is not True:
        reasons.append("missing_simulation_performance_acknowledgement")

    acknowledged_status = str(judgment.get("simulation_performance_status") or "").strip().upper()
    expected_status = str(simulation_performance.get("status") or "").strip().upper()
    if expected_status and acknowledged_status and acknowledged_status != expected_status:
        reasons.append("simulation_performance_status_mismatch")
    if expected_status and not acknowledged_status:
        reasons.append("simulation_performance_status_missing")

    expected_reasons = set(simulation_performance.get("reason_codes") or [])
    acknowledged_reasons = {
        str(value).strip()
        for value in (judgment.get("simulation_performance_reason_codes") or [])
        if str(value).strip()
    } if isinstance(judgment.get("simulation_performance_reason_codes"), list) else set()
    if expected_reasons and not (acknowledged_reasons & expected_reasons):
        reasons.append("simulation_performance_reason_codes_missing_or_unmatched")

    notes = judgment.get("simulation_performance_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("simulation_performance_notes_missing")
    return reasons


def required_attention(item):
    digest = item.get("context_digest") if isinstance(item.get("context_digest"), dict) else {}
    return [str(value) for value in digest.get("required_judgment_attention") or [] if str(value).strip()]


def intraday_context_attention(item):
    attention = set(required_attention(item))
    relevant = [
        value
        for value in sorted(attention)
        if value
        in (
            "intraday_context_missing_or_stale_requires_disclosure",
            "intraday_context_challenges_buy_requires_discussion",
            "intraday_context_challenges_sell_requires_discussion",
            "intraday_context_quality_degraded_requires_disclosure",
            "intraday_context_timeframe_conflict_requires_disclosure",
            "intraday_minute_producer_limit_requires_acknowledgement",
            "intraday_market_not_open_requires_session_context",
            "intraday_market_session_overrides_limit_requires_disclosure",
        )
    ]
    if not relevant:
        return {}
    digest = item.get("context_digest") if isinstance(item.get("context_digest"), dict) else {}
    intraday = digest.get("intraday_context") if isinstance(digest.get("intraday_context"), dict) else {}
    return {
        "attention": relevant,
        "status": intraday.get("status"),
        "notes": intraday.get("notes") or intraday.get("hermes_notes") or [],
    }


def intraday_context_acknowledgement_reasons(judgment, intraday_attention):
    if not intraday_attention:
        return []
    reasons = []
    if judgment.get("intraday_context_acknowledged") is not True:
        reasons.append("missing_intraday_context_acknowledgement")
    notes = judgment.get("intraday_context_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("intraday_context_notes_missing")
    acknowledged_status = str(judgment.get("intraday_context_status") or "").strip().upper()
    expected_status = str(intraday_attention.get("status") or "").strip().upper()
    if expected_status and acknowledged_status and acknowledged_status != expected_status:
        reasons.append("intraday_context_status_mismatch")
    return reasons


def intraday_signal_evidence_attention(item):
    digest = item.get("context_digest") if isinstance(item.get("context_digest"), dict) else {}
    evidence = (
        digest.get("intraday_signal_evidence")
        if isinstance(digest.get("intraday_signal_evidence"), dict)
        else {}
    )
    if not evidence.get("requires_judgment_acknowledgement"):
        return {}
    return {
        "alignment": evidence.get("alignment"),
        "codes": [str(value).strip() for value in evidence.get("codes") or [] if str(value).strip()],
    }


def intraday_signal_evidence_acknowledgement_reasons(judgment, evidence_attention):
    if not evidence_attention:
        return []
    reasons = []
    if judgment.get("intraday_signal_evidence_acknowledged") is not True:
        reasons.append("missing_intraday_signal_evidence_acknowledgement")

    expected_alignment = str(evidence_attention.get("alignment") or "").strip()
    acknowledged_alignment = str(judgment.get("intraday_signal_evidence_alignment") or "").strip()
    if expected_alignment and not acknowledged_alignment:
        reasons.append("intraday_signal_evidence_alignment_missing")
    elif expected_alignment and acknowledged_alignment and acknowledged_alignment != expected_alignment:
        reasons.append("intraday_signal_evidence_alignment_mismatch")

    expected_codes = set(evidence_attention.get("codes") or [])
    acknowledged_codes = {
        str(value).strip()
        for value in (judgment.get("intraday_signal_evidence_codes") or [])
        if str(value).strip()
    } if isinstance(judgment.get("intraday_signal_evidence_codes"), list) else set()
    if expected_codes and not (acknowledged_codes & expected_codes):
        reasons.append("intraday_signal_evidence_codes_missing_or_unmatched")

    notes = judgment.get("intraday_signal_evidence_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("intraday_signal_evidence_notes_missing")
    return reasons


def current_session_quote_evidence_attention(item):
    alert = item.get("alert") if isinstance(item.get("alert"), dict) else {}
    current_session = (
        alert.get("current_session_quote_evidence")
        if isinstance(alert.get("current_session_quote_evidence"), dict)
        else {}
    )
    basis = str(current_session.get("basis") or "").strip()
    used_in_full_score = current_session.get("used_in_full_score") is True
    factor_basis = alert.get("factor_evidence_basis") if isinstance(alert.get("factor_evidence_basis"), dict) else {}
    current_session_factor_count = int(as_float(factor_basis.get("current_session_quote"), 0) or 0)
    score_impact = (
        current_session.get("score_impact")
        if isinstance(current_session.get("score_impact"), dict)
        else {}
    )
    score_impact_factor_count = int(as_float(score_impact.get("factor_count"), 0) or 0)
    if not used_in_full_score and current_session_factor_count <= 0 and score_impact_factor_count <= 0:
        return {}
    return {
        "basis": basis,
        "used_in_full_score": used_in_full_score,
        "factor_count": max(current_session_factor_count, score_impact_factor_count),
        "score_impact_factor_count": score_impact_factor_count,
        "provisional": current_session.get("provisional") is True,
        "mutates_completed_daily_history": current_session.get("mutates_completed_daily_history") is True,
        "replaces_completed_daily_bar": current_session.get("replaces_completed_daily_bar") is True,
    }


def current_session_quote_notes_discuss_score_impact(notes):
    text = list_text(notes)
    if not text:
        return False
    has_impact = any(
        term in text
        for term in (
            "score",
            "impact",
            "score_impact",
            "points",
            "delta",
            "factor",
            "分數",
            "得分",
            "貢獻",
            "贡献",
            "因子",
        )
    )
    has_weight = any(
        term in text
        for term in (
            "dominat",
            "support",
            "supported",
            "only supported",
            "not dominate",
            "主導",
            "主导",
            "支撐",
            "支撑",
            "支持",
            "輔助",
            "辅助",
        )
    )
    return has_impact and has_weight


def current_session_quote_evidence_acknowledgement_reasons(judgment, evidence_attention):
    if not evidence_attention:
        return []
    reasons = []
    if judgment.get("current_session_quote_evidence_acknowledged") is not True:
        reasons.append("missing_current_session_quote_evidence_acknowledgement")

    expected_basis = str(evidence_attention.get("basis") or "").strip()
    acknowledged_basis = str(judgment.get("current_session_quote_evidence_basis") or "").strip()
    if expected_basis and not acknowledged_basis:
        reasons.append("current_session_quote_evidence_basis_missing")
    elif expected_basis and acknowledged_basis and acknowledged_basis != expected_basis:
        reasons.append("current_session_quote_evidence_basis_mismatch")

    notes = judgment.get("current_session_quote_evidence_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("current_session_quote_evidence_notes_missing")
    elif (
        int(as_float(evidence_attention.get("score_impact_factor_count"), 0) or 0) > 0
        and not current_session_quote_notes_discuss_score_impact(notes)
    ):
        reasons.append("current_session_quote_score_impact_notes_missing")
    return reasons


def context_review_reasons(judgment):
    review = judgment.get("context_review")
    if not isinstance(review, dict):
        return ["context_review_missing"]
    reasons = []
    for flag in REQUIRED_CONTEXT_REVIEW_FLAGS:
        if review.get(flag) is not True:
            reasons.append(f"context_review_missing_{flag}")
    notes = review.get("notes")
    if notes is not None and not isinstance(notes, list):
        reasons.append("context_review_notes_invalid")
    return reasons


def strategy_evidence_reasons(packet, item):
    evidence = packet.get("strategy_evidence") or {}
    if evidence.get("schema") != "rt_signal_outcome_report_v1":
        return ["strategy_evidence_missing_or_invalid"]
    alert = item.get("alert") or {}
    horizon = os.environ.get("RT_ORDER_STRATEGY_EVIDENCE_HORIZON", "1d")
    reasons = []
    overall = ((evidence.get("overall") or {}).get("horizons") or {}).get(horizon) or {}
    if not overall:
        reasons.append(f"strategy_evidence_horizon_missing_{horizon}")
    else:
        reasons.extend(intake.metric_reasons(overall, "resolved_count", intake.MIN_OUTCOME_SAMPLE, "overall"))

    trigger_key = f"{str(alert.get('signal_type', '')).upper()}:{alert.get('trigger') or 'UNKNOWN'}"
    trigger_metric = {}
    for row in evidence.get("by_trigger") or []:
        if row.get("key") == trigger_key:
            trigger_metric = ((row.get("horizons") or {}).get(horizon) or {})
            break
    if intake.MIN_TRIGGER_OUTCOME_SAMPLE > 0:
        if not trigger_metric:
            reasons.append("trigger_outcome_missing")
        else:
            reasons.extend(intake.metric_reasons(trigger_metric, "resolved_count", intake.MIN_TRIGGER_OUTCOME_SAMPLE, "trigger"))
    return reasons


def weak_hermes_alpha_evidence(packet):
    brief = packet.get("strategy_learning_brief") if isinstance(packet.get("strategy_learning_brief"), dict) else {}
    if not brief:
        return {
            "schema": "hermes_alpha_evidence_summary_v1",
            "status": "MISSING",
            "reasons": ["strategy_learning_brief_missing"],
        }
    evidence = brief.get("hermes_alpha_evidence") if isinstance(brief.get("hermes_alpha_evidence"), dict) else {}
    if not evidence:
        return {
            "schema": "hermes_alpha_evidence_summary_v1",
            "status": "MISSING",
            "reasons": ["hermes_alpha_evidence_missing"],
        }
    status = str(evidence.get("status") or "").strip().upper()
    if status not in ("INSUFFICIENT", "NEGATIVE", "MISSING", "INVALID"):
        return {}
    return evidence


def hermes_alpha_evidence_acknowledgement_reasons(judgment, evidence):
    if not evidence:
        return []
    reasons = []
    if judgment.get("hermes_alpha_evidence_acknowledged") is not True:
        reasons.append("missing_hermes_alpha_evidence_acknowledgement")

    expected_status = str(evidence.get("status") or "").strip().upper()
    acknowledged_status = str(judgment.get("hermes_alpha_evidence_status") or "").strip().upper()
    if expected_status and not acknowledged_status:
        reasons.append("hermes_alpha_evidence_status_missing")
    elif expected_status and acknowledged_status and acknowledged_status != expected_status:
        reasons.append("hermes_alpha_evidence_status_mismatch")

    expected_reasons = {str(value).strip() for value in evidence.get("reasons") or [] if str(value).strip()}
    acknowledged_reasons = {
        str(value).strip()
        for value in (judgment.get("hermes_alpha_evidence_reasons") or [])
        if str(value).strip()
    } if isinstance(judgment.get("hermes_alpha_evidence_reasons"), list) else set()
    if expected_reasons and not (acknowledged_reasons & expected_reasons):
        reasons.append("hermes_alpha_evidence_reasons_missing_or_unmatched")

    notes = judgment.get("hermes_alpha_evidence_notes")
    if not isinstance(notes, list) or not notes:
        reasons.append("hermes_alpha_evidence_notes_missing")
    return reasons


def validate_judgment_contract(judgment):
    reasons = []
    if judgment.get("schema") != "hermes_trade_judgment_v1":
        reasons.append("schema_invalid")
    sid = str(judgment.get("signal_id", "")).strip()
    if not sid:
        reasons.append("missing_signal_id")
    decision = str(judgment.get("decision", "")).strip().lower()
    if decision not in ("approve", "reject", "reduce", "hold"):
        reasons.append("decision_invalid")
    confidence = as_float(judgment.get("confidence"))
    if confidence is None or confidence < 0 or confidence > 1:
        reasons.append("confidence_invalid")
    reviewed_at = intake.parse_time(judgment.get("reviewed_at") or judgment.get("created_at"))
    if not reviewed_at:
        reasons.append("reviewed_at_invalid")
    if not isinstance(judgment.get("supporting_factors"), list) or not judgment.get("supporting_factors"):
        reasons.append("supporting_factors_missing")
    if not isinstance(judgment.get("opposing_factors"), list) or not judgment.get("opposing_factors"):
        reasons.append("opposing_factors_missing")
    if not isinstance(judgment.get("risk_notes"), list) or not judgment.get("risk_notes"):
        reasons.append("risk_notes_missing")
    if decision == "reduce":
        try:
            if int(float(judgment.get("max_quantity"))) <= 0:
                reasons.append("max_quantity_invalid")
        except (TypeError, ValueError):
            reasons.append("max_quantity_missing")
    if judgment.get("market_regime_exception") is True:
        ok, exception_reasons = intake.market_exception_from_judgment(judgment)
        if not ok:
            reasons.extend(exception_reasons)
    if decision in ("approve", "reduce"):
        reasons.extend(context_review_reasons(judgment))
    return reasons


def reviewed_alert_identity_reasons(judgment, alert):
    reasons = []
    checks = (
        ("reviewed_symbol", str((alert or {}).get("symbol") or "").strip().upper(), "judgment_reviewed_symbol_mismatch"),
        (
            "reviewed_signal_type",
            str((alert or {}).get("signal_type") or "").strip().upper(),
            "judgment_reviewed_signal_type_mismatch",
        ),
        ("reviewed_trigger", str((alert or {}).get("trigger") or "").strip(), "judgment_reviewed_trigger_mismatch"),
    )
    for field, expected, reason in checks:
        actual = str((judgment or {}).get(field) or "").strip()
        if not actual:
            continue
        if field in ("reviewed_symbol", "reviewed_signal_type"):
            actual = actual.upper()
        if expected and actual != expected:
            reasons.append(reason)
    return reasons


def audit_judgment(judgment, packet, review_by_id, eligible_ids, now=None, packet_source="latest_packet", packet_reasons=None):
    now = now or datetime.now()
    sid = str(judgment.get("signal_id", "")).strip()
    reasons = validate_judgment_contract(judgment)
    reasons.extend(packet_reasons or [])
    item = review_by_id.get(sid)
    approval = decision_is_approval(judgment)
    if not item:
        reasons.append("orphan_judgment_not_in_latest_packet")
    else:
        alert = item.get("alert") or {}
        reasons.extend(reviewed_alert_identity_reasons(judgment, alert))
        if approval and sid not in eligible_ids:
            reasons.append("approval_for_ineligible_review_item")
        if approval and alert.get("confirmed") is not True:
            reasons.append("approval_for_unconfirmed_alert")
        if approval and (packet.get("health") or {}).get("status") == "FAIL":
            reasons.append("approval_while_health_fail")
        if approval and str(alert.get("signal_type", "")).upper() == "BUY":
            market, regime, _market_payload = market_regime_for_item(packet, item)
            if regime == "risk_off":
                ok, exception_reasons = intake.market_exception_from_judgment(judgment)
                if not ok:
                    reasons.append(f"{market}_risk_off_buy_approval_without_exception")
                    reasons.extend(exception_reasons)
            _market, cross_market = market_cross_context_for_item(packet, item)
            reasons.extend(cross_market_conflict_acknowledgement_reasons(judgment, cross_market))
            native_index_context = native_index_context_for_item(packet, item)
            reasons.extend(native_index_conflict_acknowledgement_reasons(judgment, native_index_context))
            relevant_catalysts = relevant_negative_event_catalysts(packet, item)
            reasons.extend(event_catalyst_acknowledgement_reasons(judgment, relevant_catalysts))
            relevant_event_signals = relevant_challenge_buy_event_signals(packet, item)
            reasons.extend(event_catalyst_signal_acknowledgement_reasons(judgment, relevant_event_signals))
            relevant_support_event_signals = relevant_support_buy_event_signals(packet, item)
            reasons.extend(event_catalyst_support_acknowledgement_reasons(judgment, relevant_support_event_signals))
            relevant_external = relevant_negative_external_context(packet, item)
            reasons.extend(external_context_acknowledgement_reasons(judgment, relevant_external))
            relevant_positive_external = relevant_positive_external_context(packet, item)
            reasons.extend(external_context_support_acknowledgement_reasons(judgment, relevant_positive_external))
            relevant_sentiment = relevant_risk_market_sentiment(packet, item)
            reasons.extend(market_sentiment_acknowledgement_reasons(judgment, relevant_sentiment))
            relevant_positive_sentiment = relevant_positive_market_sentiment(packet, item)
            reasons.extend(market_sentiment_support_acknowledgement_reasons(judgment, relevant_positive_sentiment))
            relevant_fundamentals = relevant_partial_fundamentals(packet, item)
            reasons.extend(fundamentals_limit_acknowledgement_reasons(judgment, relevant_fundamentals))
            relevant_fundamentals_support = relevant_supportive_fundamentals(packet, item)
            reasons.extend(fundamentals_support_acknowledgement_reasons(judgment, relevant_fundamentals_support))
        if approval:
            market_coverage_attention = market_context_coverage_attention(item)
            reasons.extend(market_context_coverage_acknowledgement_reasons(judgment, market_coverage_attention))
            external_coverage_attention = external_market_context_coverage_attention(item)
            reasons.extend(
                external_market_context_coverage_acknowledgement_reasons(judgment, external_coverage_attention)
            )
            event_coverage_attention = event_catalyst_coverage_attention(item)
            reasons.extend(event_catalyst_coverage_acknowledgement_reasons(judgment, event_coverage_attention))
            coverage_attention = event_catalyst_signal_coverage_attention(item)
            reasons.extend(event_catalyst_signal_coverage_acknowledgement_reasons(judgment, coverage_attention))
            sentiment_coverage_attention = market_sentiment_coverage_attention(item)
            reasons.extend(
                market_sentiment_coverage_acknowledgement_reasons(judgment, sentiment_coverage_attention)
            )
            fundamentals_coverage_attention = fundamentals_context_coverage_attention(item)
            reasons.extend(
                fundamentals_context_coverage_acknowledgement_reasons(judgment, fundamentals_coverage_attention)
            )
            intraday_attention = intraday_context_attention(item)
            reasons.extend(intraday_context_acknowledgement_reasons(judgment, intraday_attention))
            intraday_evidence_attention = intraday_signal_evidence_attention(item)
            reasons.extend(
                intraday_signal_evidence_acknowledgement_reasons(judgment, intraday_evidence_attention)
            )
            current_session_quote_attention = current_session_quote_evidence_attention(item)
            reasons.extend(
                current_session_quote_evidence_acknowledgement_reasons(
                    judgment,
                    current_session_quote_attention,
                )
            )
            source_reliability = degraded_source_reliability(packet)
            reasons.extend(source_reliability_acknowledgement_reasons(judgment, source_reliability))
            simulation_performance = weak_simulation_performance(packet)
            reasons.extend(simulation_performance_acknowledgement_reasons(judgment, simulation_performance))
            alpha_evidence = weak_hermes_alpha_evidence(packet)
            reasons.extend(hermes_alpha_evidence_acknowledgement_reasons(judgment, alpha_evidence))
            for reason in strategy_evidence_reasons(packet, item):
                reasons.append(f"approval_with_{reason}")

    reviewed_at = intake.parse_time(judgment.get("reviewed_at") or judgment.get("created_at"))
    if reviewed_at:
        expiry = judgment.get("expiry_minutes", MAX_JUDGMENT_AGE_MINUTES)
        try:
            expiry = int(expiry)
        except (TypeError, ValueError):
            expiry = MAX_JUDGMENT_AGE_MINUTES
        if now - reviewed_at > timedelta(minutes=expiry):
            reasons.append("judgment_expired")

    return {
        "signal_id": sid,
        "decision": str(judgment.get("decision", "")).strip().lower(),
        "reviewed_at": judgment.get("reviewed_at") or judgment.get("created_at"),
        "confidence": as_float(judgment.get("confidence")),
        "packet_id": str(judgment.get("packet_id", "")).strip(),
        "packet_source": packet_source,
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
    }


def duplicate_signal_counts(judgments):
    counts = Counter(str(item.get("signal_id", "")).strip() for item in judgments if item.get("signal_id"))
    return {sid: count for sid, count in counts.items() if count > 1}


def row_in_current_packet_scope(row, latest_packet_id):
    """Only the current packet should decide current readiness.

    Older packet judgments remain visible for audit/history, but they should
    not permanently fail the live readiness gate after the packet rolls.
    Missing packet_id stays in current scope because it cannot be traced.
    """
    latest_packet_id = str(latest_packet_id or "").strip()
    packet_id = str((row or {}).get("packet_id") or "").strip()
    if not latest_packet_id:
        return True
    if not packet_id:
        return True
    return packet_id == latest_packet_id


def duplicate_signal_counts_from_rows(rows):
    counts = Counter(str(row.get("signal_id", "")).strip() for row in rows if row.get("signal_id"))
    return {sid: count for sid, count in counts.items() if count > 1}


def build_report(judgments=None, packet=None, now=None, packet_archive_dir=PACKET_ARCHIVE_DIR):
    now = now or datetime.now()
    judgments = intake.load_judgments(JUDGMENT_FILE) if judgments is None else judgments
    latest_packet = load_json_file(PACKET_FILE, {}) if packet is None else packet
    latest_packet_id = latest_packet.get("packet_id") if isinstance(latest_packet, dict) else None
    latest_review_by_id, latest_eligible_ids = packet_review_maps(latest_packet)
    rows = []
    packet_source_counts = Counter()
    for judgment in judgments:
        judgment_packet, packet_source, packet_reasons = packet_for_judgment(
            judgment,
            latest_packet,
            archive_dir=packet_archive_dir,
        )
        packet_source_counts[packet_source] += 1
        review_by_id, eligible_ids = packet_review_maps(judgment_packet)
        rows.append(
            audit_judgment(
                judgment,
                judgment_packet,
                review_by_id,
                eligible_ids,
                now=now,
                packet_source=packet_source,
                packet_reasons=packet_reasons,
            )
        )
    current_rows = []
    historical_rows = []
    for row in rows:
        if row_in_current_packet_scope(row, latest_packet_id):
            row["audit_scope"] = "current_packet"
            current_rows.append(row)
        else:
            row["audit_scope"] = "historical_packet"
            historical_rows.append(row)

    reason_counts = Counter()
    current_reason_counts = Counter()
    historical_reason_counts = Counter()
    decision_counts = Counter()
    status_counts = Counter()
    current_status_counts = Counter()
    historical_status_counts = Counter()
    for row in rows:
        status_counts[row["status"]] += 1
        decision_counts[row["decision"] or "missing"] += 1
        for reason in row["reasons"]:
            reason_counts[reason] += 1
    for row in current_rows:
        current_status_counts[row["status"]] += 1
        for reason in row["reasons"]:
            current_reason_counts[reason] += 1
    for row in historical_rows:
        historical_status_counts[row["status"]] += 1
        for reason in row["reasons"]:
            historical_reason_counts[reason] += 1

    duplicates = duplicate_signal_counts_from_rows(current_rows)
    for sid, count in duplicates.items():
        reason_counts["duplicate_judgments_for_signal"] += count - 1
        current_reason_counts["duplicate_judgments_for_signal"] += count - 1
    historical_duplicates = duplicate_signal_counts_from_rows(historical_rows)
    status = "FAIL" if current_status_counts.get("FAIL") or duplicates else "OK"
    if status == "OK" and (historical_status_counts.get("FAIL") or historical_duplicates):
        status = "WARN"
    recommendations = build_recommendations(
        current_rows,
        current_reason_counts,
        empty_recommendation=(
            "no_current_packet_trade_judgments_observed" if rows else "no_hermes_judgments_observed_yet"
        ),
    )
    historical_recommendations = (
        build_recommendations(
            historical_rows,
            historical_reason_counts,
            empty_recommendation="no_historical_trade_judgments_observed",
        )
        if historical_rows
        else []
    )

    payload = {
        "schema": "hermes_judgment_audit_report_v1",
        "generated_at": now_iso(),
        "status": status,
        "source": {
            "judgment_file": JUDGMENT_FILE,
            "packet_file": PACKET_FILE,
            "packet_archive_dir": packet_archive_dir,
            "latest_packet_id": latest_packet.get("packet_id") if isinstance(latest_packet, dict) else None,
            "latest_packet_generated_at": latest_packet.get("generated_at") if isinstance(latest_packet, dict) else None,
        },
        "counts": {
            "judgment_count": len(judgments),
            "review_item_count": len(latest_review_by_id),
            "eligible_review_item_count": len(latest_eligible_ids),
            "status_counts": dict(status_counts),
            "current_status_counts": dict(current_status_counts),
            "historical_status_counts": dict(historical_status_counts),
            "decision_counts": dict(decision_counts),
            "reason_counts": dict(reason_counts),
            "current_reason_counts": dict(current_reason_counts),
            "historical_reason_counts": dict(historical_reason_counts),
            "duplicate_signal_ids": duplicates,
            "historical_duplicate_signal_ids": historical_duplicates,
            "packet_source_counts": dict(packet_source_counts),
            "current_packet_scope_count": len(current_rows),
            "historical_packet_scope_count": len(historical_rows),
        },
        "judgments": rows[-100:],
        "recommendations": recommendations,
        "historical_recommendations": historical_recommendations,
    }
    return payload


def build_recommendations(rows, reason_counts, empty_recommendation="no_hermes_judgments_observed_yet"):
    if not rows:
        return [empty_recommendation]
    recs = []
    critical = [
        "approval_for_ineligible_review_item",
        "approval_for_unconfirmed_alert",
        "approval_while_health_fail",
        "orphan_judgment_not_in_latest_packet",
    ]
    for reason in critical:
        if reason_counts.get(reason):
            recs.append(f"fix_or_reject_judgments:{reason}")
    if any("risk_off_buy_approval_without_exception" in reason for reason in reason_counts):
        recs.append("risk_off_buy_approvals_require_market_regime_exception")
    if reason_counts.get("missing_market_context_coverage_acknowledgement") or reason_counts.get(
        "market_context_coverage_notes_missing"
    ) or reason_counts.get("market_context_coverage_status_mismatch"):
        recs.append("market_context_coverage_limits_require_structured_acknowledgement")
    if reason_counts.get("cross_market_conflict_breadth_not_discussed") or reason_counts.get(
        "cross_market_conflict_sentiment_not_discussed"
    ):
        recs.append("cross_market_conflicts_require_explicit_breadth_and_sentiment_discussion")
    if reason_counts.get("native_index_conflict_breadth_not_discussed") or reason_counts.get(
        "native_index_conflict_index_not_discussed"
    ):
        recs.append("native_index_conflicts_require_explicit_breadth_and_index_discussion")
    if reason_counts.get("missing_event_catalyst_risk_acknowledgement") or reason_counts.get(
        "event_catalyst_ids_missing_or_unmatched"
    ) or reason_counts.get("event_catalyst_risk_notes_missing"):
        recs.append("negative_event_catalyst_buy_approvals_require_structured_acknowledgement")
    if reason_counts.get("missing_event_catalyst_signal_risk_acknowledgement") or reason_counts.get(
        "event_catalyst_signal_ids_missing_or_unmatched"
    ) or reason_counts.get("event_catalyst_signal_risk_notes_missing"):
        recs.append("challenge_buy_event_signals_require_structured_acknowledgement")
    if reason_counts.get("missing_event_catalyst_support_acknowledgement") or reason_counts.get(
        "event_catalyst_support_signal_ids_missing_or_unmatched"
    ) or reason_counts.get("event_catalyst_support_notes_missing"):
        recs.append("support_buy_event_signals_require_structured_acknowledgement")
    if reason_counts.get("missing_event_catalyst_coverage_acknowledgement") or reason_counts.get(
        "event_catalyst_coverage_notes_missing"
    ) or reason_counts.get("event_catalyst_coverage_status_mismatch"):
        recs.append("event_catalyst_coverage_limits_require_structured_acknowledgement")
    if reason_counts.get("missing_event_catalyst_signal_coverage_acknowledgement") or reason_counts.get(
        "event_catalyst_signal_coverage_notes_missing"
    ) or reason_counts.get("event_catalyst_signal_coverage_status_mismatch"):
        recs.append("event_catalyst_signal_coverage_limits_require_structured_acknowledgement")
    if reason_counts.get("missing_external_market_context_risk_acknowledgement") or reason_counts.get(
        "external_market_context_ids_missing_or_unmatched"
    ) or reason_counts.get("external_market_context_notes_missing"):
        recs.append("negative_external_context_buy_approvals_require_structured_acknowledgement")
    if reason_counts.get("missing_external_market_context_support_acknowledgement") or reason_counts.get(
        "external_market_context_support_ids_missing_or_unmatched"
    ) or reason_counts.get("external_market_context_support_notes_missing"):
        recs.append("positive_external_context_buy_support_requires_structured_acknowledgement")
    if reason_counts.get("missing_external_market_context_coverage_acknowledgement") or reason_counts.get(
        "external_market_context_coverage_notes_missing"
    ) or reason_counts.get("external_market_context_coverage_status_mismatch"):
        recs.append("external_market_context_coverage_limits_require_structured_acknowledgement")
    if reason_counts.get("missing_market_sentiment_risk_acknowledgement") or reason_counts.get(
        "market_sentiment_indicator_ids_missing_or_unmatched"
    ) or reason_counts.get("market_sentiment_notes_missing"):
        recs.append("market_sentiment_risk_approvals_require_structured_acknowledgement")
    if reason_counts.get("missing_market_sentiment_support_acknowledgement") or reason_counts.get(
        "market_sentiment_support_indicator_ids_missing_or_unmatched"
    ) or reason_counts.get("market_sentiment_support_notes_missing"):
        recs.append("positive_market_sentiment_buy_support_requires_structured_acknowledgement")
    if reason_counts.get("missing_market_sentiment_coverage_acknowledgement") or reason_counts.get(
        "market_sentiment_coverage_notes_missing"
    ) or reason_counts.get("market_sentiment_coverage_status_mismatch"):
        recs.append("market_sentiment_coverage_limits_require_structured_acknowledgement")
    if reason_counts.get("context_review_missing") or any(
        reason.startswith("context_review_missing_") for reason in reason_counts
    ):
        recs.append("approve_reduce_judgments_require_structured_context_review")
    if reason_counts.get("missing_fundamentals_context_limit_acknowledgement") or reason_counts.get(
        "fundamentals_context_symbols_missing_or_unmatched"
    ) or reason_counts.get("fundamentals_context_missing_metrics_not_discussed") or reason_counts.get(
        "fundamentals_context_notes_missing"
    ):
        recs.append("partial_fundamentals_buy_approvals_require_structured_limitation_acknowledgement")
    if reason_counts.get("missing_fundamentals_context_support_acknowledgement") or reason_counts.get(
        "fundamentals_context_support_symbols_missing_or_unmatched"
    ) or reason_counts.get("fundamentals_context_support_metrics_missing_or_unmatched") or reason_counts.get(
        "fundamentals_context_support_notes_missing"
    ):
        recs.append("supportive_fundamentals_buy_approvals_require_structured_acknowledgement")
    if reason_counts.get("missing_fundamentals_context_coverage_acknowledgement") or reason_counts.get(
        "fundamentals_context_coverage_notes_missing"
    ) or reason_counts.get("fundamentals_context_coverage_status_mismatch"):
        recs.append("fundamentals_context_coverage_limits_require_structured_acknowledgement")
    if reason_counts.get("missing_source_reliability_limit_acknowledgement") or reason_counts.get(
        "source_reliability_reasons_missing_or_unmatched"
    ) or reason_counts.get("source_reliability_components_missing_or_unmatched") or reason_counts.get(
        "source_reliability_notes_missing"
    ):
        recs.append("source_reliability_degraded_approvals_require_structured_limitation_acknowledgement")
    if reason_counts.get("missing_simulation_performance_acknowledgement") or reason_counts.get(
        "simulation_performance_status_missing"
    ) or reason_counts.get("simulation_performance_status_mismatch") or reason_counts.get(
        "simulation_performance_reason_codes_missing_or_unmatched"
    ) or reason_counts.get("simulation_performance_notes_missing"):
        recs.append("simulation_performance_warnings_require_structured_acknowledgement")
    if reason_counts.get("missing_hermes_alpha_evidence_acknowledgement") or reason_counts.get(
        "hermes_alpha_evidence_status_missing"
    ) or reason_counts.get("hermes_alpha_evidence_status_mismatch") or reason_counts.get(
        "hermes_alpha_evidence_reasons_missing_or_unmatched"
    ) or reason_counts.get("hermes_alpha_evidence_notes_missing"):
        recs.append("weak_hermes_alpha_evidence_requires_structured_acknowledgement")
    if reason_counts.get("missing_intraday_context_acknowledgement") or reason_counts.get(
        "intraday_context_notes_missing"
    ) or reason_counts.get("intraday_context_status_mismatch"):
        recs.append("intraday_context_attention_requires_structured_acknowledgement")
    if reason_counts.get("missing_intraday_signal_evidence_acknowledgement") or reason_counts.get(
        "intraday_signal_evidence_alignment_missing"
    ) or reason_counts.get("intraday_signal_evidence_alignment_mismatch") or reason_counts.get(
        "intraday_signal_evidence_codes_missing_or_unmatched"
    ) or reason_counts.get("intraday_signal_evidence_notes_missing"):
        recs.append("intraday_signal_evidence_requires_structured_acknowledgement")
    if reason_counts.get("missing_current_session_quote_evidence_acknowledgement") or reason_counts.get(
        "current_session_quote_evidence_basis_missing"
    ) or reason_counts.get("current_session_quote_evidence_basis_mismatch") or reason_counts.get(
        "current_session_quote_evidence_notes_missing"
    ) or reason_counts.get(
        "current_session_quote_score_impact_notes_missing"
    ):
        recs.append("current_session_quote_evidence_requires_structured_acknowledgement")
    if any(reason.startswith("approval_with_") for reason in reason_counts):
        recs.append("approvals_conflict_with_execution_gates_keep_alert_sim_disabled")
    if reason_counts.get("judgment_expired"):
        recs.append("refresh_or_ignore_expired_judgments")
    if reason_counts.get("judgment_missing_packet_id"):
        recs.append("include_packet_id_in_future_judgments")
    if reason_counts.get("packet_archive_missing_for_packet_id"):
        recs.append("retain_packet_archive_for_judgment_audit")
    if not recs:
        recs.append("judgment_audit_clean_continue_review_only_observation")
    return recs


def build_text_report(payload):
    counts = payload["counts"]
    lines = [
        f"Hermes judgment audit {payload['generated_at']}",
        (
            f"judgments={counts['judgment_count']} review_items={counts['review_item_count']} "
            f"eligible={counts['eligible_review_item_count']} status={counts['status_counts']}"
        ),
    ]
    if counts["reason_counts"]:
        lines.append("Reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(counts["reason_counts"].items())))
    lines.append("Recommendations: " + ", ".join(payload["recommendations"]))
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--judgment-file", default=JUDGMENT_FILE)
    parser.add_argument("--packet-file", default=PACKET_FILE)
    parser.add_argument("--packet-archive-dir", default=PACKET_ARCHIVE_DIR)
    parser.add_argument("--output", default=REPORT_FILE)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    return parser.parse_args()


def main():
    args = parse_args()
    global JUDGMENT_FILE, PACKET_FILE, PACKET_ARCHIVE_DIR
    JUDGMENT_FILE = args.judgment_file
    PACKET_FILE = args.packet_file
    PACKET_ARCHIVE_DIR = args.packet_archive_dir
    payload = build_report()
    if args.output:
        save_json_atomic(args.output, payload)
    text = build_text_report(payload)
    if args.text:
        print(text)
    elif args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text)
        print("\n--- JSON ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
