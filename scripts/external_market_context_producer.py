#!/usr/bin/env python3
"""Read-only RSS producer for external market context.

The producer writes a snapshot consumed by external_market_context_report.py.
It does not submit orders, change strategy config, or write the v5 alert queue.
"""
import argparse
import email.utils
import html
import json
import os
import re
import urllib.request
from urllib.parse import urlencode
import xml.etree.ElementTree as ET
from datetime import datetime


OUTPUT_FILE = os.environ.get("EXTERNAL_MARKET_CONTEXT_INPUT_FILE", "/tmp/external_market_context_inputs.json")
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("EXTERNAL_CONTEXT_FETCH_TIMEOUT_SECONDS", "10"))
DEFAULT_LIMIT_PER_FEED = int(os.environ.get("EXTERNAL_CONTEXT_LIMIT_PER_FEED", "12"))
DEFAULT_INFOHUB_URL = os.environ.get("EXTERNAL_CONTEXT_INFOHUB_URL", "http://127.0.0.1:8899")
DEFAULT_INFOHUB_LIMIT = int(os.environ.get("EXTERNAL_CONTEXT_INFOHUB_LIMIT", "12"))
DEFAULT_FEEDS = [
    {
        "source": "google_news_us_market",
        "url": "https://news.google.com/rss/search?q=stock%20market%20economy%20when%3A6h&hl=en-US&gl=US&ceid=US:en",
        "markets": ["US", "GLOBAL"],
        "category": "macro",
    },
    {
        "source": "google_news_hk_china_market",
        "url": "https://news.google.com/rss/search?q=Hong%20Kong%20stocks%20China%20market%20when%3A12h&hl=en-US&gl=US&ceid=US:en",
        "markets": ["HK", "CN", "GLOBAL"],
        "category": "macro",
    },
    {
        "source": "marketwatch_topstories",
        "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "markets": ["US", "GLOBAL"],
        "category": "news",
    },
    {
        "source": "cnbc_top_news",
        "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "markets": ["US", "GLOBAL"],
        "category": "macro",
    },
]
DEFAULT_INFOHUB_REQUESTS = [
    {
        "name": "infohub_macro_global",
        "path": "/macro",
        "params": {"keyword": "global markets geopolitical today"},
        "markets": ["GLOBAL", "US", "HK", "CN"],
        "category": "macro",
    },
    {
        "name": "infohub_macro_hk_china",
        "path": "/macro",
        "params": {"keyword": "Hong Kong stocks China market"},
        "markets": ["HK", "CN", "GLOBAL"],
        "category": "macro",
    },
    {
        "name": "infohub_news_all",
        "path": "/news",
        "params": {"source": "all"},
        "markets": ["GLOBAL", "US", "HK", "CN"],
        "category": "news",
    },
]

NEGATIVE_KEYWORDS = {
    "crash",
    "selloff",
    "sell-off",
    "plunge",
    "slump",
    "tumble",
    "recession",
    "inflation",
    "tariff",
    "sanction",
    "war",
    "conflict",
    "missile",
    "attack",
    "default",
    "bankruptcy",
    "downgrade",
    "hawkish",
    "rate hike",
    "oil spike",
    "risk-off",
}
POSITIVE_KEYWORDS = {
    "rally",
    "surge",
    "jump",
    "record high",
    "beat",
    "upgrade",
    "stimulus",
    "ceasefire",
    "truce",
    "deal",
    "cut rates",
    "rate cut",
    "dovish",
    "risk-on",
    "eases",
    "cooling inflation",
}
HIGH_IMPACT_KEYWORDS = {
    "fed",
    "federal reserve",
    "tariff",
    "china",
    "hong kong",
    "taiwan",
    "war",
    "ceasefire",
    "oil",
    "inflation",
    "jobs report",
    "cpi",
    "gdp",
    "earnings",
    "nasdaq",
    "s&p 500",
    "dow",
    "hang seng",
}
SYMBOL_ALIASES = {
    "AAPL": ["apple"],
    "MSFT": ["microsoft"],
    "NVDA": ["nvidia"],
    "TSLA": ["tesla"],
    "AMD": ["amd"],
    "META": ["meta", "facebook"],
    "AMZN": ["amazon"],
    "GOOGL": ["google", "alphabet"],
    "BABA": ["alibaba"],
    "JD": ["jd.com"],
    "NIO": ["nio"],
    "LI": ["li auto"],
    "PDD": ["pdd", "pinduoduo"],
    "00700": ["tencent"],
    "09988": ["alibaba"],
    "03690": ["meituan"],
    "01810": ["xiaomi"],
    "09618": ["jd.com"],
}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def save_json_atomic(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{datetime.now().strftime('%Y%m%d%H%M%S%f')}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def strip_html(value):
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_datetime(value, fallback=None):
    if not value:
        return fallback
    try:
        parsed = email.utils.parsedate_to_datetime(str(value))
        if parsed.tzinfo:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except Exception:
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return fallback


def parse_feed_xml(xml_text):
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is not None:
        return [child for child in channel.findall("item")]
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall("atom:entry", ns)
    if entries:
        return entries
    return []


def child_text(node, names):
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text
        child = node.find(f"{{http://www.w3.org/2005/Atom}}{name}")
        if child is not None and child.text:
            return child.text
    return ""


def child_link(node):
    link = child_text(node, ("link",))
    if link:
        return link.strip()
    atom_link = node.find("{http://www.w3.org/2005/Atom}link")
    if atom_link is not None:
        return atom_link.attrib.get("href", "")
    return ""


def fetch_feed(url, timeout=DEFAULT_TIMEOUT_SECONDS):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_json(url, timeout=DEFAULT_TIMEOUT_SECONDS):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def build_url(base_url, path, params=None):
    url = f"{str(base_url).rstrip('/')}/{str(path).lstrip('/')}"
    params = params or {}
    if params:
        url = f"{url}?{urlencode(params)}"
    return url


def keyword_hits(text, keywords):
    lower = text.lower()
    return sorted(keyword for keyword in keywords if keyword in lower)


def infer_markets(text, defaults):
    lower = text.lower()
    markets = set(defaults or [])
    if any(key in lower for key in ("hong kong", "hang seng", "hsbc", "h-shares")):
        markets.add("HK")
    if any(key in lower for key in ("china", "beijing", "yuan", "renminbi")):
        markets.add("CN")
    if any(key in lower for key in ("nasdaq", "s&p", "dow", "wall street", "fed", "treasury")):
        markets.add("US")
    if not markets:
        markets.add("GLOBAL")
    return sorted(markets)


def infer_symbols(text):
    lower = text.lower()
    symbols = []
    for symbol, aliases in SYMBOL_ALIASES.items():
        if any(alias in lower for alias in aliases):
            symbols.append(symbol)
    return sorted(set(symbols))


def infer_category(text, default):
    lower = text.lower()
    if any(key in lower for key in ("fed", "inflation", "cpi", "gdp", "jobs", "tariff", "rate", "treasury")):
        return "macro"
    if any(key in lower for key in ("flow", "inflow", "outflow", "etf", "fund")):
        return "capital_flow"
    if any(key in lower for key in ("earnings", "merger", "acquisition", "delivery", "guidance")):
        return "event"
    return default if default in {"news", "macro", "capital_flow", "event", "sentiment"} else "news"


def score_item(text, source):
    negative = keyword_hits(text, NEGATIVE_KEYWORDS)
    positive = keyword_hits(text, POSITIVE_KEYWORDS)
    high = keyword_hits(text, HIGH_IMPACT_KEYWORDS)
    if negative and positive:
        sentiment = "mixed"
    elif negative:
        sentiment = "negative"
    elif positive:
        sentiment = "positive"
    else:
        sentiment = "neutral"
    impact = 0.35
    if high:
        impact += 0.25
    if negative or positive:
        impact += 0.15
    if source.startswith("google_news"):
        impact += 0.05
    return sentiment, min(round(impact, 4), 0.95), {
        "negative": negative,
        "positive": positive,
        "high_impact": high,
    }


def item_from_feed_node(node, feed, generated_at):
    title = strip_html(child_text(node, ("title",)))
    summary = strip_html(child_text(node, ("description", "summary", "content")))
    url = child_link(node)
    published_raw = child_text(node, ("pubDate", "published", "updated"))
    published = parse_datetime(published_raw, fallback=datetime.fromisoformat(generated_at))
    text = f"{title} {summary}"
    sentiment, impact_score, hits = score_item(text, feed["source"])
    markets = infer_markets(text, feed.get("markets") or [])
    symbols = infer_symbols(text)
    category = infer_category(text, feed.get("category") or "news")
    tags = ["producer:rss", f"source:{feed['source']}"]
    for kind, values in hits.items():
        tags.extend(f"{kind}:{value}" for value in values[:5])
    return {
        "id": url or f"{feed['source']}:{title[:80]}",
        "category": category,
        "source": feed["source"],
        "title": title,
        "summary": summary[:500],
        "published_at": published.isoformat(timespec="seconds") if published else generated_at,
        "sentiment": sentiment,
        "impact_score": impact_score,
        "markets": markets,
        "symbols": symbols,
        "url": url,
        "tags": tags,
    }


def source_token(value):
    token = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "unknown").strip().lower())
    return re.sub(r"_+", "_", token).strip("_") or "unknown"


def item_from_infohub_item(raw_item, request_spec, generated_at):
    title = strip_html(raw_item.get("title") or raw_item.get("headline") or "")
    summary = strip_html(raw_item.get("summary") or raw_item.get("snippet") or raw_item.get("description") or "")
    url = raw_item.get("link") or raw_item.get("url") or ""
    published_raw = raw_item.get("published") or raw_item.get("published_at") or raw_item.get("time")
    published = parse_datetime(published_raw, fallback=datetime.fromisoformat(generated_at))
    raw_source = str(raw_item.get("source") or request_spec["name"])
    text = f"{title} {summary}"
    source = f"{request_spec['name']}_{source_token(raw_source)}"
    sentiment, impact_score, hits = score_item(text, source)
    markets = infer_markets(text, request_spec.get("markets") or [])
    symbols = infer_symbols(text)
    category = infer_category(text, request_spec.get("category") or "news")
    tags = [
        "producer:infohub",
        "provider:infohub_public_rss_bridge",
        f"infohub_endpoint:{request_spec['path']}",
        f"raw_source:{raw_source}",
    ]
    for kind, values in hits.items():
        tags.extend(f"{kind}:{value}" for value in values[:5])
    return {
        "id": raw_item.get("id") or url or f"{source}:{title[:80]}",
        "category": category,
        "source": source,
        "provider": "infohub_public_rss_bridge",
        "producer": "infohub",
        "title": title,
        "summary": summary[:500],
        "published_at": published.isoformat(timespec="seconds") if published else generated_at,
        "sentiment": sentiment,
        "impact_score": impact_score,
        "markets": markets,
        "symbols": symbols,
        "url": url,
        "tags": tags,
    }


def fetch_infohub_items(
    fetch_json_func=fetch_json,
    infohub_url=DEFAULT_INFOHUB_URL,
    requests=None,
    generated_at=None,
    limit=DEFAULT_INFOHUB_LIMIT,
):
    generated_at = generated_at or now_iso()
    requests = requests or DEFAULT_INFOHUB_REQUESTS
    items = []
    warnings = []
    for request_spec in requests:
        params = dict(request_spec.get("params") or {})
        params.setdefault("limit", limit)
        url = build_url(infohub_url, request_spec["path"], params)
        try:
            payload = fetch_json_func(url)
            raw_items = payload.get("items") if isinstance(payload, dict) else []
            if not isinstance(raw_items, list):
                warnings.append(f"fetch_failed:{request_spec['name']}:items_not_list")
                continue
            for raw_item in raw_items[:limit]:
                if not isinstance(raw_item, dict):
                    continue
                item = item_from_infohub_item(raw_item, request_spec, generated_at)
                if item.get("title"):
                    items.append(item)
        except Exception as exc:
            warnings.append(f"fetch_failed:{request_spec['name']}:{exc}")
    return items, warnings


def dedupe(items):
    deduped = []
    seen = set()
    for item in items:
        key = (item.get("id"), item.get("title"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def build_snapshot(
    fetch_feed_func=fetch_feed,
    feeds=None,
    now=None,
    limit_per_feed=DEFAULT_LIMIT_PER_FEED,
    include_infohub=False,
    infohub_url=DEFAULT_INFOHUB_URL,
    fetch_json_func=fetch_json,
    include_rss=True,
):
    generated_at = (now or datetime.now()).isoformat(timespec="seconds")
    feeds = feeds or DEFAULT_FEEDS
    items = []
    warnings = []
    if include_rss:
        for feed in feeds:
            try:
                xml_text = fetch_feed_func(feed["url"])
                nodes = parse_feed_xml(xml_text)
                for node in nodes[:limit_per_feed]:
                    item = item_from_feed_node(node, feed, generated_at)
                    if item.get("title"):
                        items.append(item)
            except Exception as exc:
                warnings.append(f"fetch_failed:{feed.get('source')}:{exc}")
    if include_infohub:
        infohub_items, infohub_warnings = fetch_infohub_items(
            fetch_json_func=fetch_json_func,
            infohub_url=infohub_url,
            generated_at=generated_at,
            limit=limit_per_feed,
        )
        items.extend(infohub_items)
        warnings.extend(infohub_warnings)
    return {
        "schema": "external_market_context_producer_v1",
        "generated_at": generated_at,
        "items": dedupe(items),
        "warnings": warnings,
        "source": {
            "read_only": True,
            "submits_orders": False,
            "changes_strategy": False,
            "changes_alert_queue": False,
            "output_file": OUTPUT_FILE,
            "provider": "rss+infohub" if include_infohub and include_rss else "infohub" if include_infohub else "rss",
            "feed_count": len(feeds) if include_rss else 0,
            "include_infohub": bool(include_infohub),
            "infohub_url": infohub_url if include_infohub else None,
            "limit_per_feed": limit_per_feed,
        },
    }


def parse_feed_overrides(value):
    if not value:
        return []
    feeds = []
    for idx, raw in enumerate(str(value).split(",")):
        url = raw.strip()
        if not url:
            continue
        feeds.append({"source": f"custom_rss_{idx + 1}", "url": url, "markets": ["GLOBAL"], "category": "news"})
    return feeds


def build_text_report(payload):
    by_sentiment = {}
    by_category = {}
    for item in payload.get("items") or []:
        by_sentiment[item.get("sentiment") or "unknown"] = by_sentiment.get(item.get("sentiment") or "unknown", 0) + 1
        by_category[item.get("category") or "news"] = by_category.get(item.get("category") or "news", 0) + 1
    return (
        f"External context producer generated={payload.get('generated_at')} "
        f"items={len(payload.get('items') or [])} warnings={len(payload.get('warnings') or [])} "
        f"sentiment={by_sentiment} categories={by_category}"
    )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=OUTPUT_FILE)
    parser.add_argument("--feed-url", action="append", default=[], help="custom RSS/Atom feed URL")
    parser.add_argument("--limit-per-feed", type=int, default=DEFAULT_LIMIT_PER_FEED)
    parser.add_argument("--include-infohub", action="store_true", help="also read Info Hub HTTP context endpoints")
    parser.add_argument("--infohub-url", default=DEFAULT_INFOHUB_URL)
    parser.add_argument("--no-rss", action="store_true", help="skip built-in RSS feeds")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    parser.add_argument("--text", action="store_true", help="emit text only")
    parser.add_argument("--dry-run", action="store_true", help="fetch and print without writing")
    return parser.parse_args()


def main():
    args = parse_args()
    feeds = DEFAULT_FEEDS
    if args.feed_url:
        feeds = [{"source": f"custom_rss_{idx + 1}", "url": url, "markets": ["GLOBAL"], "category": "news"} for idx, url in enumerate(args.feed_url)]
    payload = build_snapshot(
        feeds=feeds,
        limit_per_feed=args.limit_per_feed,
        include_infohub=args.include_infohub,
        infohub_url=args.infohub_url,
        include_rss=not args.no_rss,
    )
    payload["source"]["output_file"] = args.output
    if args.output and not args.dry_run:
        save_json_atomic(args.output, payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.text:
        print(build_text_report(payload))
    else:
        print(build_text_report(payload))
        print("\n--- JSON ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
