"""US stock-universe symbol and instrument filters."""

import re


EXCLUDED_US_NAME_PATTERNS = (
    r"\bpreferred stock\b",
    r"\bpreferred shares?\b",
    r"\bpreference shares?\b",
    r"\bperpetual preferred\b",
    r"\bdepositary shares?\b.*\bpreferred\b",
    r"\bwarrants?\b",
    r"\bright(s)?\b",
    r"\bright to purchase\b",
    r"\bunits?\b",
    r"\bunit expiring\b",
    r"\bsenior notes?\b",
    r"\bnotes? due\b",
    r"\bdebentures? due\b",
)


def normalize_us_symbol(raw_symbol):
    symbol = str(raw_symbol or "").strip().upper()
    if not symbol:
        return ""
    symbol = symbol.replace(".", "-")
    if "^" in symbol or "/" in symbol:
        return ""
    if not symbol[0].isalpha():
        return ""
    if not all(ch.isalnum() or ch == "-" for ch in symbol):
        return ""
    return symbol[:12]


def is_supported_us_equity(item):
    symbol = normalize_us_symbol(item.get("symbol", ""))
    if not symbol:
        return False
    name = str(item.get("name") or "").lower()
    return not any(re.search(pattern, name) for pattern in EXCLUDED_US_NAME_PATTERNS)
