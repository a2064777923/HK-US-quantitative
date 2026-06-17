"""US stock-universe symbol and instrument filters."""

EXCLUDED_US_NAME_TERMS = (
    "preferred stock",
    "preference share",
    "perpetual preferred",
    "depositary shares",
    "warrant",
    "right to purchase",
    "unit expiring",
    "notes due",
    "debentures due",
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
    return not any(term in name for term in EXCLUDED_US_NAME_TERMS)
