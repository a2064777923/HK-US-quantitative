#!/usr/bin/env python3
"""
實時信號引擎 v5.0
- 每3秒拉取實時報價（騰訊API批量查詢）
- 條件觸發器：RSI/布林/均線/成交量異動
- 觸發時先跑完整多因子分析
- 即時發送通知（寫入文件，由外部腳本推送）
"""
import hashlib, subprocess, json, time, os, sys, math, re, urllib.request
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from threading import Thread, Lock

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python versions without zoneinfo
    ZoneInfo = None

# ========== 配置 ==========
POLL_INTERVAL = 3       # 每3秒拉一次報價
FULL_SCAN_INTERVAL = 30 # 每30秒做一次全量條件檢查
SIGNAL_COOLDOWN = 1800  # 同一信號30分鐘內唔重複觸發
ALERT_FILE = "/tmp/rt_signal_alert.json"
ALERT_QUEUE_FILE = "/tmp/rt_signal_alerts.jsonl"
STATE_FILE = "/tmp/rt_signal_state.json"
WATCHLIST_FILE = os.environ.get("RT_SIGNAL_WATCHLIST_FILE", "/root/rt_signal_watchlist.json")
STRATEGY_CONFIG_FILE = os.environ.get("RT_SIGNAL_STRATEGY_CONFIG_FILE", "/root/rt_signal_strategy_config.json")
MIN_SIGNAL_HISTORY_BARS = 30
MIN_VOLUME_SESSION_FRACTION = 0.05
VOLUME_ANOMALY_RATIO = 3.0
HK_SYMBOL_RE = re.compile(r"^\d{5}$")
US_SYMBOL_RE = re.compile(r"^(?=.{1,10}$)[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)?$")

# 股票池 — 港股+美股
HK_WATCHLIST = [
    "00700","03690","01810","09896","00916","02015","02208","07226","01918",
    "03888","00177","03328","03968","00929","06690","00948","02328","00959",
    "09866","03988","01398","00945","00939","00148","00656","01244","09988",
    "09618","00005","00016","00002","00003","00006","00012","00017","00019",
    "00027","00241","00267","00288","00291","00386","00388","00669","00762",
    "00823","00857","00868","00881","00883","01775","02007","02013","02018",
    "02313","02319","02382","02388","06098","06160","06862","09626","09961",
]
US_WATCHLIST = [
    "AAPL","MSFT","NVDA","TSLA","AMD","META","AMZN","GOOGL","NFLX",
    "PDD","NOK","ARAY","BABA","JD","NIO","LI","BIDU","NTES","V","JPM",
    "BAC","GS","JNJ","UNH","PFE","INTC","CRM","ADBE","XPEV","ZH","BILI","IQ",
]

def default_strategy_config():
    return {
        "schema": "rt_signal_strategy_config_v1",
        "version": "default-v5-compatible",
        "description": "Default realtime v5 strategy config matching legacy hard-coded behavior.",
        "signal_cooldown_seconds": SIGNAL_COOLDOWN,
        "volume_anomaly_ratio": VOLUME_ANOMALY_RATIO,
        "confirmation_thresholds": {
            "BUY": {"min_full_score": 0.25},
            "SELL": {"max_full_score": -0.25}
        },
        "risk_model": {
            "atr_stop_multiple": 2.0,
            "atr_take_profit_multiple": 3.0,
            "min_rr_ratio": 1.2
        },
        "emission": {
            "emit_unconfirmed_directional_as_watch": True
        },
        "trigger_overrides": {}
    }

def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def as_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default

def as_int(value, default=None):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default

def as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    return default

def valid_watchlist_symbol(symbol, market=None):
    market = str(market or "").upper()
    if market == "HK":
        return bool(HK_SYMBOL_RE.match(symbol))
    if market == "US":
        return bool(US_SYMBOL_RE.match(symbol))
    return bool(HK_SYMBOL_RE.match(symbol) or US_SYMBOL_RE.match(symbol))

def normalize_symbol_list(value, market=None, rejected=None):
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[\s,;]+", value)
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        return []

    symbols = []
    seen = set()
    for item in raw_items:
        symbol = str(item).strip().upper()
        if not symbol or symbol in seen:
            continue
        if not valid_watchlist_symbol(symbol, market=market):
            if rejected is not None:
                rejected.append(symbol)
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols

def symbols_from_watchlist_payload(payload, market, rejected=None):
    if not isinstance(payload, dict):
        return []
    candidates = [
        payload.get(market),
        payload.get(market.lower()),
        payload.get(f"{market}_WATCHLIST"),
        payload.get(f"{market.lower()}_watchlist"),
    ]
    for parent_key in ("markets", "watchlists"):
        parent = payload.get(parent_key)
        if isinstance(parent, dict):
            item = parent.get(market) or parent.get(market.lower())
            if isinstance(item, dict):
                candidates.append(item.get("symbols"))
            else:
                candidates.append(item)
    for candidate in candidates:
        symbols = normalize_symbol_list(candidate, market=market, rejected=rejected)
        if symbols:
            return symbols
    return []

def load_watchlist_file(path):
    if not path:
        return {}, ["watchlist_file_not_configured"]
    if not os.path.exists(path):
        return {}, [f"watchlist_file_missing:{path}"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        return {}, [f"watchlist_file_invalid:{exc}"]
    rejected = {"HK": [], "US": []}
    watchlists = {
        "HK": symbols_from_watchlist_payload(payload, "HK", rejected=rejected["HK"]),
        "US": symbols_from_watchlist_payload(payload, "US", rejected=rejected["US"]),
    }
    warnings = []
    for market, symbols in rejected.items():
        if symbols:
            sample = ",".join(symbols[:5])
            suffix = f":{len(symbols)}" if len(symbols) > 5 else ""
            warnings.append(f"watchlist_file_invalid_symbols:{market}:{sample}{suffix}")
    return watchlists, warnings

def watchlist_digest(watchlists):
    seed = {
        "HK": watchlists.get("HK", []),
        "US": watchlists.get("US", []),
    }
    return hashlib.sha256(json.dumps(seed, sort_keys=True).encode("utf-8")).hexdigest()[:16]

def strategy_config_digest(config):
    seed = {
        "signal_cooldown_seconds": config.get("signal_cooldown_seconds"),
        "volume_anomaly_ratio": config.get("volume_anomaly_ratio"),
        "confirmation_thresholds": config.get("confirmation_thresholds"),
        "risk_model": config.get("risk_model"),
        "emission": config.get("emission"),
        "trigger_overrides": config.get("trigger_overrides"),
    }
    return hashlib.sha256(json.dumps(seed, sort_keys=True).encode("utf-8")).hexdigest()[:16]

def merge_strategy_config(base, override):
    merged = json.loads(json.dumps(base))
    if not isinstance(override, dict):
        return merged
    for key in ("version", "description"):
        if key in override:
            merged[key] = override[key]
    for key in ("signal_cooldown_seconds", "volume_anomaly_ratio"):
        if key in override:
            merged[key] = override[key]
    for key in ("confirmation_thresholds", "risk_model", "emission", "trigger_overrides"):
        if isinstance(override.get(key), dict):
            merged.setdefault(key, {})
            for sub_key, value in override[key].items():
                if isinstance(value, dict) and isinstance(merged[key].get(sub_key), dict):
                    merged[key][sub_key].update(value)
                else:
                    merged[key][sub_key] = value
    return merged

def normalize_strategy_config(config):
    config = merge_strategy_config(default_strategy_config(), config)
    warnings = []
    cooldown = as_int(config.get("signal_cooldown_seconds"), SIGNAL_COOLDOWN)
    if cooldown is None or cooldown <= 0:
        warnings.append("invalid_signal_cooldown_seconds_using_default")
        cooldown = SIGNAL_COOLDOWN
    config["signal_cooldown_seconds"] = cooldown

    volume_ratio = as_float(config.get("volume_anomaly_ratio"), VOLUME_ANOMALY_RATIO)
    if volume_ratio is None or volume_ratio <= 0:
        warnings.append("invalid_volume_anomaly_ratio_using_default")
        volume_ratio = VOLUME_ANOMALY_RATIO
    config["volume_anomaly_ratio"] = volume_ratio

    thresholds = config.setdefault("confirmation_thresholds", {})
    buy = thresholds.setdefault("BUY", {})
    sell = thresholds.setdefault("SELL", {})
    buy["min_full_score"] = as_float(buy.get("min_full_score"), 0.25)
    sell["max_full_score"] = as_float(sell.get("max_full_score"), -0.25)

    risk = config.setdefault("risk_model", {})
    risk["atr_stop_multiple"] = as_float(risk.get("atr_stop_multiple"), 2.0)
    risk["atr_take_profit_multiple"] = as_float(risk.get("atr_take_profit_multiple"), 3.0)
    risk["min_rr_ratio"] = as_float(risk.get("min_rr_ratio"), 1.2)
    if risk["atr_stop_multiple"] is None or risk["atr_stop_multiple"] <= 0:
        warnings.append("invalid_atr_stop_multiple_using_default")
        risk["atr_stop_multiple"] = 2.0
    if risk["atr_take_profit_multiple"] is None or risk["atr_take_profit_multiple"] <= 0:
        warnings.append("invalid_atr_take_profit_multiple_using_default")
        risk["atr_take_profit_multiple"] = 3.0
    if risk["min_rr_ratio"] is None or risk["min_rr_ratio"] <= 0:
        warnings.append("invalid_min_rr_ratio_using_default")
        risk["min_rr_ratio"] = 1.2

    emission = config.setdefault("emission", {})
    emission["emit_unconfirmed_directional_as_watch"] = as_bool(
        emission.get("emit_unconfirmed_directional_as_watch"),
        True,
    )

    overrides = config.setdefault("trigger_overrides", {})
    for key, override in list(overrides.items()):
        if not isinstance(override, dict):
            warnings.append(f"invalid_trigger_override:{key}")
            overrides.pop(key, None)
            continue
        if "min_full_score" in override:
            override["min_full_score"] = as_float(override.get("min_full_score"))
        if "max_full_score" in override:
            override["max_full_score"] = as_float(override.get("max_full_score"))
        if "cooldown_seconds" in override:
            override["cooldown_seconds"] = as_int(override.get("cooldown_seconds"))
    config["config_id"] = strategy_config_digest(config)
    return config, warnings

def load_strategy_config(env=None, file_path=None):
    env = env if env is not None else os.environ
    file_path = file_path if file_path is not None else env.get("RT_SIGNAL_STRATEGY_CONFIG_FILE", STRATEGY_CONFIG_FILE)
    config = default_strategy_config()
    source = "fallback_default"
    warnings = []
    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                config = merge_strategy_config(config, loaded)
                source = "file"
            else:
                warnings.append(f"strategy_config_file_invalid:{file_path}")
        except Exception as exc:
            warnings.append(f"strategy_config_file_invalid:{exc}")
    else:
        warnings.append(f"strategy_config_file_missing:{file_path}")

    env_buy = env.get("RT_SIGNAL_BUY_MIN_FULL_SCORE")
    env_sell = env.get("RT_SIGNAL_SELL_MAX_FULL_SCORE")
    env_volume = env.get("RT_SIGNAL_VOLUME_ANOMALY_RATIO")
    env_cooldown = env.get("RT_SIGNAL_COOLDOWN_SECONDS")
    env_emit_unconfirmed = env.get("RT_SIGNAL_EMIT_UNCONFIRMED_DIRECTIONAL_AS_WATCH")
    if env_buy is not None:
        config.setdefault("confirmation_thresholds", {}).setdefault("BUY", {})["min_full_score"] = env_buy
        source = "env"
    if env_sell is not None:
        config.setdefault("confirmation_thresholds", {}).setdefault("SELL", {})["max_full_score"] = env_sell
        source = "env"
    if env_volume is not None:
        config["volume_anomaly_ratio"] = env_volume
        source = "env"
    if env_cooldown is not None:
        config["signal_cooldown_seconds"] = env_cooldown
        source = "env"
    if env_emit_unconfirmed is not None:
        config.setdefault("emission", {})["emit_unconfirmed_directional_as_watch"] = env_emit_unconfirmed
        source = "env"

    config, normalize_warnings = normalize_strategy_config(config)
    warnings.extend(normalize_warnings)
    context = {
        "schema": "rt_signal_strategy_config_runtime_v1",
        "strategy_config_id": config.get("config_id"),
        "loaded_at": now_iso(),
        "source": source,
        "source_file": file_path,
        "version": config.get("version"),
        "warnings": warnings,
    }
    return config, context

def load_watchlists(env=None, file_path=None):
    env = env if env is not None else os.environ
    file_path = file_path if file_path is not None else env.get("RT_SIGNAL_WATCHLIST_FILE", WATCHLIST_FILE)
    watchlists = {"HK": list(HK_WATCHLIST), "US": list(US_WATCHLIST)}
    sources = {"HK": "fallback_hardcoded", "US": "fallback_hardcoded"}
    warnings = []

    file_watchlists, file_warnings = load_watchlist_file(file_path)
    warnings.extend(file_warnings)
    for market in ("HK", "US"):
        symbols = file_watchlists.get(market) or []
        if symbols:
            watchlists[market] = symbols
            sources[market] = "file"
        elif not file_warnings:
            warnings.append(f"watchlist_file_missing_market:{market}")

    for market, env_key in (("HK", "RT_SIGNAL_HK_WATCHLIST"), ("US", "RT_SIGNAL_US_WATCHLIST")):
        if env_key not in env:
            continue
        rejected = []
        symbols = normalize_symbol_list(env.get(env_key), market=market, rejected=rejected)
        if rejected:
            sample = ",".join(rejected[:5])
            suffix = f":{len(rejected)}" if len(rejected) > 5 else ""
            warnings.append(f"watchlist_env_invalid_symbols:{env_key}:{sample}{suffix}")
        if symbols:
            watchlists[market] = symbols
            sources[market] = "env"
        else:
            warnings.append(f"watchlist_env_empty:{env_key}")

    context = {
        "schema": "rt_signal_watchlist_runtime_v1",
        "watchlist_id": watchlist_digest(watchlists),
        "loaded_at": now_iso(),
        "source_file": file_path,
        "markets": {
            market: {
                "source": sources[market],
                "count": len(watchlists[market]),
                "sample": watchlists[market][:10],
            }
            for market in ("HK", "US")
        },
        "warnings": warnings,
    }
    return watchlists["HK"], watchlists["US"], context

def alert_watchlist_metadata(context, market):
    context = context or {}
    market = str(market or "").upper()
    info = ((context.get("markets") or {}).get(market) or {})
    return {
        "watchlist_id": context.get("watchlist_id"),
        "watchlist_source": info.get("source"),
        "watchlist_count": info.get("count"),
    }

def alert_strategy_metadata(context):
    context = context or {}
    return {
        "strategy_config_id": context.get("strategy_config_id"),
        "strategy_config_source": context.get("source"),
        "strategy_config_version": context.get("version"),
    }

# ========== 數據層 ==========
def db(sql):
    try:
        r = subprocess.run(
            ["docker","exec","quantmind-db","psql","-U","quantmind","-d","quantmind","-t","-A","-c",sql],
            capture_output=True, text=True, timeout=30
        )
        return r.stdout.strip()
    except:
        return ""

def fetch_hk_quotes(symbols):
    """批量拉取港股實時報價 — 騰訊API"""
    if not symbols: return {}
    batch = ",".join(f"hk{s}" for s in symbols)
    url = f"http://qt.gtimg.cn/q={batch}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://finance.qq.com"})
        txt = urllib.request.urlopen(req, timeout=5).read().decode("gbk","ignore")
        results = {}
        for line in txt.strip().split("\n"):
            if "~" not in line: continue
            parts = line.split("~")
            if len(parts) < 45: continue
            sym = parts[2].split(".")[0]  # 去掉.OQ等後綴
            try:
                results[sym] = {
                    "price": float(parts[3]) if parts[3] else 0,
                    "open": float(parts[5]) if parts[5] else 0,
                    "high": float(parts[33]) if parts[33] else 0,
                    "low": float(parts[34]) if parts[34] else 0,
                    "prev_close": float(parts[4]) if parts[4] else 0,
                    "volume": float(parts[6]) if parts[6] else 0,  # 手
                    "amount": float(parts[37]) if parts[37] else 0,  # 萬元
                    "change_pct": float(parts[32]) if parts[32] else 0,
                    "time": parts[30],
                    "market": "HK",
                }
            except (ValueError, IndexError):
                continue
        return results
    except Exception as e:
        return {}

def fetch_us_quotes(symbols):
    """批量拉取美股實時報價 — 騰訊API"""
    if not symbols: return {}
    batch = ",".join(f"us{s}" for s in symbols)
    url = f"http://qt.gtimg.cn/q={batch}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://finance.qq.com"})
        txt = urllib.request.urlopen(req, timeout=5).read().decode("gbk","ignore")
        results = {}
        for line in txt.strip().split("\n"):
            if "~" not in line: continue
            parts = line.split("~")
            if len(parts) < 45: continue
            sym = parts[2].split(".")[0]
            try:
                results[sym] = {
                    "price": float(parts[3]) if parts[3] else 0,
                    "open": float(parts[5]) if parts[5] else 0,
                    "high": float(parts[33]) if parts[33] else 0,
                    "low": float(parts[34]) if parts[34] else 0,
                    "prev_close": float(parts[4]) if parts[4] else 0,
                    "volume": float(parts[6]) if parts[6] else 0,
                    "amount": float(parts[37]) if parts[37] else 0,
                    "change_pct": float(parts[32]) if parts[32] else 0,
                    "time": parts[30],
                    "market": "US",
                }
            except (ValueError, IndexError):
                continue
        return results
    except:
        return {}

def normalize_quote(quote):
    """Return a finite realtime quote payload or a rejection reason."""
    if not isinstance(quote, dict):
        return None, "quote_not_dict"
    price = as_float(quote.get("price"))
    if price is None:
        return None, "missing_or_invalid_price"
    if price <= 0:
        return None, "non_positive_price"

    high = as_float(quote.get("high"), price)
    low = as_float(quote.get("low"), price)
    prev_close = as_float(quote.get("prev_close"))
    volume = as_float(quote.get("volume"), 0) or 0
    amount = as_float(quote.get("amount"), 0) or 0
    change_pct = as_float(quote.get("change_pct"))
    if change_pct is None and prev_close is not None and prev_close > 0:
        change_pct = (price / prev_close - 1.0) * 100.0
    if change_pct is None:
        change_pct = 0
    if high <= 0:
        high = price
    if low <= 0:
        low = price
    if volume < 0:
        volume = 0
    if amount < 0:
        amount = 0

    normalized = dict(quote)
    normalized.update(
        {
            "price": price,
            "high": max(high, price),
            "low": min(low, price),
            "prev_close": prev_close if prev_close is not None and prev_close > 0 else 0,
            "volume": volume,
            "amount": amount,
            "change_pct": change_pct,
        }
    )
    return normalized, None

def parse_quote_datetime(value):
    if not value:
        return None
    value = str(value).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y%m%d%H%M%S",
        "%Y%m%d%H%M",
        "%H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt == "%H:%M:%S":
                today = datetime.now()
                parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
            return parsed
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None

def session_elapsed_minutes(market, dt):
    """Return elapsed regular-session minutes for quote-local time."""
    if not dt:
        return None
    t = dt.hour * 60 + dt.minute + dt.second / 60
    market = str(market or "").upper()
    if market == "HK":
        if t < 570:
            return 0
        if t <= 720:
            return t - 570
        if t < 780:
            return 150
        if t <= 960:
            return 150 + (t - 780)
        return 330
    if market == "US":
        if t < 570:
            return 0
        if t <= 960:
            return t - 570
        return 390
    return None

def regular_session_minutes(market):
    market = str(market or "").upper()
    if market == "HK":
        return 330
    if market == "US":
        return 390
    return None

def hk_regular_session_open_hkt(dt):
    if not dt or dt.weekday() >= 5:
        return False
    minute = dt.hour * 60 + dt.minute + dt.second / 60
    return 570 <= minute <= 720 or 780 <= minute <= 960

def nth_weekday_of_month(year, month, weekday, nth):
    day = datetime(year, month, 1)
    offset = (weekday - day.weekday()) % 7
    return day + timedelta(days=offset + 7 * (nth - 1))

def us_dst_active_for_utc(value):
    if not value:
        return False
    utc_value = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    year = utc_value.year
    start_local = nth_weekday_of_month(year, 3, 6, 2).replace(hour=2, minute=0, second=0, microsecond=0)
    end_local = nth_weekday_of_month(year, 11, 6, 1).replace(hour=2, minute=0, second=0, microsecond=0)
    start_utc = start_local.replace(tzinfo=timezone(timedelta(hours=-5))).astimezone(timezone.utc)
    end_utc = end_local.replace(tzinfo=timezone(timedelta(hours=-4))).astimezone(timezone.utc)
    return start_utc <= utc_value < end_utc

def us_eastern_datetime_from_hkt(dt):
    if not dt:
        return None
    hkt = timezone(timedelta(hours=8))
    hkt_value = dt.astimezone(hkt) if getattr(dt, "tzinfo", None) else dt.replace(tzinfo=hkt)
    utc_value = hkt_value.astimezone(timezone.utc)
    if ZoneInfo:
        try:
            return utc_value.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            pass
    offset = -4 if us_dst_active_for_utc(utc_value) else -5
    return utc_value.astimezone(timezone(timedelta(hours=offset)))

def us_regular_session_open_hkt(dt):
    """Return US regular-session state for an HKT timestamp, with DST-aware NY conversion."""
    eastern = us_eastern_datetime_from_hkt(dt)
    if not eastern or eastern.weekday() >= 5:
        return False
    minute = eastern.hour * 60 + eastern.minute + eastern.second / 60
    return 570 <= minute <= 960

def market_open_flags_hkt(dt=None):
    dt = dt or datetime.now()
    return hk_regular_session_open_hkt(dt), us_regular_session_open_hkt(dt)

def cumulative_volume_ratio(quote_volume, avg_daily_volume, market, quote_time=None):
    """Compare cumulative intraday volume with expected cumulative daily volume."""
    quote_volume = as_float(quote_volume)
    avg_daily_volume = as_float(avg_daily_volume)
    if quote_volume is None or avg_daily_volume is None:
        return None
    if quote_volume <= 0 or avg_daily_volume <= 0:
        return None

    dt = parse_quote_datetime(quote_time)
    if not dt:
        return None
    elapsed = session_elapsed_minutes(market, dt)
    session_minutes = regular_session_minutes(market)
    if elapsed is None or session_minutes is None:
        return None
    if elapsed <= 0:
        return None
    fraction = max(min(elapsed / session_minutes, 1.0), MIN_VOLUME_SESSION_FRACTION)
    expected_cumulative = avg_daily_volume * fraction
    if expected_cumulative <= 0:
        return None
    return quote_volume / expected_cumulative

def normalize_daily_bar(close, high, low, volume):
    close = as_float(close)
    high = as_float(high)
    low = as_float(low)
    volume = as_float(volume)
    if close is None or high is None or low is None or volume is None:
        return None
    if close <= 0 or high <= 0 or low <= 0 or volume < 0:
        return None
    if high < low or close > high or close < low:
        return None
    return close, high, low, volume

# ========== 增量指標計算 ==========
class IncrementalIndicators:
    """每隻股票嘅增量指標 — 只更新最新數據點"""
    def __init__(self, symbol):
        self.symbol = symbol
        self.closes = []
        self.highs = []
        self.lows = []
        self.volumes = []
        self.rsi_14 = None
        self.rsi_gains = []
        self.rsi_losses = []
        self.ma5 = None
        self.ma10 = None
        self.ma20 = None
        self.bb_upper = None
        self.bb_mid = None
        self.bb_lower = None
        self.macd_dif = None
        self.macd_dea = None
        self.macd_hist = None
        self.ema_fast = None
        self.ema_slow = None
        self.atr_14 = None
        self.rt_close = None
        self.rt_high = None
        self.rt_low = None
        self.rt_volume = None
        self.rt_updated_at = None
        self.loaded = False

    def load_history(self, days=100):
        """從DB載入歷史K線"""
        query_symbol = str(self.symbol or "").upper()
        if not valid_watchlist_symbol(query_symbol):
            self.loaded = False
            return False
        days = as_int(days, 100)
        if days is None or days <= 0:
            days = 100
        raw = db(
            f"""
            WITH daily_bar AS (
                SELECT DISTINCT ON (timestamp::date)
                       timestamp::date AS trade_date,
                       close_price, high_price, low_price, volume
                FROM klines
                WHERE symbol='{query_symbol}' AND interval='day'
                ORDER BY timestamp::date, timestamp DESC
            )
            SELECT close_price, high_price, low_price, volume
            FROM daily_bar
            ORDER BY trade_date DESC LIMIT {days}
            """
        )
        rows = []
        for line in raw.split("\n"):
            if not line.strip(): continue
            p = line.split("|")
            if len(p) >= 4:
                row = normalize_daily_bar(p[0], p[1], p[2], p[3])
                if row is not None:
                    rows.append(row)
        rows.reverse()
        for c, h, l, v in rows:
            self._update(c, h, l, v)
        self.loaded = True
        return True

    def _update(self, close, high, low, volume):
        """增量更新一個數據點"""
        self.closes.append(close)
        self.highs.append(high)
        self.lows.append(low)
        self.volumes.append(volume)

        n = len(self.closes)

        # RSI (增量)
        if n >= 2:
            change = self.closes[-1] - self.closes[-2]
            gain = max(change, 0)
            loss = max(-change, 0)
            self.rsi_gains.append(gain)
            self.rsi_losses.append(loss)
            if len(self.rsi_gains) >= 14:
                if len(self.rsi_gains) == 14:
                    avg_gain = sum(self.rsi_gains[-14:]) / 14
                    avg_loss = sum(self.rsi_losses[-14:]) / 14
                else:
                    prev_avg_gain = self._prev_avg_gain
                    prev_avg_loss = self._prev_avg_loss
                    avg_gain = (prev_avg_gain * 13 + gain) / 14
                    avg_loss = (prev_avg_loss * 13 + loss) / 14
                self._prev_avg_gain = avg_gain
                self._prev_avg_loss = avg_loss
                self.rsi_14 = self.rsi_from_averages(avg_gain, avg_loss)

        # MA (增量)
        if n >= 5: self.ma5 = sum(self.closes[-5:]) / 5
        if n >= 10: self.ma10 = sum(self.closes[-10:]) / 10
        if n >= 20:
            self.ma20 = sum(self.closes[-20:]) / 20
            w = self.closes[-20:]
            std = (sum((x - self.ma20)**2 for x in w) / 20) ** 0.5
            self.bb_upper = self.ma20 + 2 * std
            self.bb_mid = self.ma20
            self.bb_lower = self.ma20 - 2 * std

        # MACD (增量 EMA)
        if n >= 2:
            k_fast = 2 / 13; k_slow = 2 / 27; k_signal = 2 / 10
            if self.ema_fast is None:
                self.ema_fast = close
                self.ema_slow = close
            else:
                self.ema_fast = close * k_fast + self.ema_fast * (1 - k_fast)
                self.ema_slow = close * k_slow + self.ema_slow * (1 - k_slow)
            self.macd_dif = self.ema_fast - self.ema_slow
            if self.macd_dea is None:
                self.macd_dea = self.macd_dif
            else:
                self.macd_dea = self.macd_dif * k_signal + self.macd_dea * (1 - k_signal)
            self.macd_hist = self.macd_dif - self.macd_dea

        # ATR (增量)
        if n >= 15:
            trs = []
            for i in range(max(1, n-14), n):
                tr = max(
                    self.highs[i] - self.lows[i],
                    abs(self.highs[i] - self.closes[i-1]),
                    abs(self.lows[i] - self.closes[i-1])
                )
                trs.append(tr)
            self.atr_14 = sum(trs) / len(trs)

    def update_realtime(self, price, high, low, volume):
        """用一根臨時日內bar更新指標，唔污染歷史日線序列。"""
        if not self.closes:
            return False
        price = as_float(price)
        if price is None or price <= 0:
            return False
        high = as_float(high, price)
        low = as_float(low, price)
        volume = as_float(volume, 0) or 0
        if high <= 0:
            high = price
        if low <= 0:
            low = price
        if volume < 0:
            volume = 0
        self.rt_close = price
        self.rt_high = max(high, price)
        self.rt_low = min(low, price)
        self.rt_volume = volume
        self.rt_updated_at = datetime.now().isoformat(timespec="seconds")
        self._recalculate_realtime_indicators()
        return True

    def _series(self):
        """返回歷史日線 + 當前臨時bar；不修改持久歷史序列。"""
        if self.rt_close is None:
            return self.closes, self.highs, self.lows, self.volumes
        return (
            self.closes + [self.rt_close],
            self.highs + [self.rt_high],
            self.lows + [self.rt_low],
            self.volumes + [self.rt_volume or 0],
        )

    def _recalculate_realtime_indicators(self):
        """從歷史+臨時bar重算展示/觸發用指標，避免每次tick append造成漂移。"""
        closes, highs, lows, volumes = self._series()
        n = len(closes)
        if n == 0:
            return

        if n >= 5: self.ma5 = sum(closes[-5:]) / 5
        if n >= 10: self.ma10 = sum(closes[-10:]) / 10
        if n >= 20:
            self.ma20 = sum(closes[-20:]) / 20
            w = closes[-20:]
            std = (sum((x - self.ma20)**2 for x in w) / 20) ** 0.5
            self.bb_upper = self.ma20 + 2 * std
            self.bb_mid = self.ma20
            self.bb_lower = self.ma20 - 2 * std

        if n >= 15:
            deltas = [closes[i] - closes[i-1] for i in range(1, n)]
            gains = [max(d, 0) for d in deltas]
            losses = [max(-d, 0) for d in deltas]
            avg_gain = sum(gains[:14]) / 14
            avg_loss = sum(losses[:14]) / 14
            for i in range(14, len(gains)):
                avg_gain = (avg_gain * 13 + gains[i]) / 14
                avg_loss = (avg_loss * 13 + losses[i]) / 14
            self.rsi_14 = self.rsi_from_averages(avg_gain, avg_loss)

            trs = []
            for i in range(max(1, n-14), n):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i] - closes[i-1])
                )
                trs.append(tr)
            self.atr_14 = sum(trs) / len(trs) if trs else None

        if n >= 2:
            def ema(data, period):
                k = 2 / (period + 1)
                out = [data[0]]
                for value in data[1:]:
                    out.append(value * k + out[-1] * (1 - k))
                return out
            ema_fast = ema(closes, 12)
            ema_slow = ema(closes, 26)
            macd_line = [ema_fast[i] - ema_slow[i] for i in range(n)]
            signal_line = ema(macd_line, 9)
            self.macd_dif = macd_line[-1]
            self.macd_dea = signal_line[-1]
            self.macd_hist = self.macd_dif - self.macd_dea

    @staticmethod
    def rsi_from_averages(avg_gain, avg_loss):
        if avg_loss > 0:
            return 100 - (100 / (1 + avg_gain / avg_loss))
        if avg_gain > 0:
            return 100
        return 50

    def score_volume_ratio(self, volumes, quote_context=None):
        if self.rt_close is not None:
            historical_volumes = self.volumes[-20:]
            if not historical_volumes:
                return None
            avg_vol = sum(historical_volumes) / len(historical_volumes)
            if avg_vol <= 0 or not isinstance(quote_context, dict):
                return None
            return cumulative_volume_ratio(
                self.rt_volume or 0,
                avg_vol,
                quote_context.get("market"),
                quote_context.get("time"),
            )

        if len(volumes) < 20:
            return None
        avg_vol = sum(volumes[-20:]) / 20
        if avg_vol <= 0:
            return None
        return volumes[-1] / avg_vol

    def get_score(self, quote_context=None):
        """計算多因子分數 (-1 to +1)"""
        closes, highs, lows, volumes = self._series()
        if not closes or len(closes) < MIN_SIGNAL_HISTORY_BARS:
            return None, []

        c = closes[-1]
        score = 0
        reasons = []

        # 趨勢
        if self.ma5 and self.ma10 and self.ma20:
            if c > self.ma5 > self.ma10 > self.ma20:
                score += 0.8; reasons.append("多頭排列")
            elif c > self.ma5 and c > self.ma10:
                score += 0.4
            elif c < self.ma5 < self.ma10 < self.ma20:
                score -= 0.8; reasons.append("空頭排列")
            elif c < self.ma5 and c < self.ma10:
                score -= 0.4

        # RSI
        if self.rsi_14 is not None:
            if self.rsi_14 > 70:
                score -= 0.3; reasons.append(f"RSI偏高({self.rsi_14:.0f})")
            elif self.rsi_14 > 55:
                score += 0.3
            elif self.rsi_14 < 30:
                score += 0.3; reasons.append(f"RSI超賣({self.rsi_14:.0f})")
            elif self.rsi_14 < 45:
                score -= 0.2

        # MACD
        if self.macd_hist is not None and self.macd_dif is not None:
            if self.macd_hist > 0 and self.macd_dif > 0:
                score += 0.3; reasons.append("MACD金叉+正值")
            elif self.macd_hist > 0:
                score += 0.1
            elif self.macd_hist < 0 and self.macd_dif < 0:
                score -= 0.3
            elif self.macd_hist < 0:
                score -= 0.1

        # 布林帶
        if self.bb_upper and self.bb_lower:
            if c <= self.bb_lower * 1.02:
                score += 0.3; reasons.append("觸及布林下軌")
            elif c >= self.bb_upper * 0.98:
                score -= 0.2; reasons.append("觸及布林上軌")

        # 成交量
        vr = self.score_volume_ratio(volumes, quote_context=quote_context)
        if vr is not None:
            if vr > 2.0:
                score += 0.2; reasons.append(f"放量{vr:.1f}倍")
            elif vr > 1.5 and c > closes[-2]:
                score += 0.1

        # 動量
        if len(closes) >= 5:
            mom = (c / closes[-5] - 1) * 100
            if abs(mom) > 5:
                tag = "上升" if mom > 0 else "下降"
                reasons.append(f"5日動量{mom:+.1f}%")

        return max(-1, min(1, score)), reasons


def indicator_history_lengths(indicators):
    lengths = {}
    for name in ("closes", "highs", "lows", "volumes"):
        series = getattr(indicators, name, None)
        if not isinstance(series, list):
            return {}
        lengths[name] = len(series)
    return lengths

def indicator_history_bar_count(indicators):
    lengths = indicator_history_lengths(indicators)
    return min(lengths.values()) if lengths else 0

def indicator_signal_ready(indicators):
    lengths = indicator_history_lengths(indicators)
    return (
        len(lengths) == 4
        and len(set(lengths.values())) == 1
        and lengths["closes"] >= MIN_SIGNAL_HISTORY_BARS
    )

def alert_signal_date(quote_time=None, generated_at=None):
    parsed_quote_time = parse_quote_datetime(quote_time)
    if parsed_quote_time is not None:
        return parsed_quote_time.strftime("%Y%m%d")
    generated_at = generated_at or datetime.now()
    return generated_at.strftime("%Y%m%d")


# ========== 條件觸發器 ==========
class TriggerEngine:
    """條件觸發器 — 只有滿足條件先觸發完整分析"""
    def __init__(self, watchlist_context=None, strategy_config=None, strategy_context=None):
        self.alerts = []
        self.cooldowns = {}  # key -> last_trigger_time
        self.watchlist_context = watchlist_context or {}
        self.strategy_config, default_context = load_strategy_config(env={}, file_path="")
        if strategy_config is not None:
            self.strategy_config, _warnings = normalize_strategy_config(strategy_config)
            default_context = {
                "schema": "rt_signal_strategy_config_runtime_v1",
                "strategy_config_id": self.strategy_config.get("config_id"),
                "loaded_at": now_iso(),
                "source": "inline",
                "source_file": "",
                "version": self.strategy_config.get("version"),
                "warnings": [],
            }
        self.strategy_context = strategy_context or default_context

    def trigger_key(self, signal_type, trigger_name):
        return f"{str(signal_type or '').upper()}:{trigger_name or 'UNKNOWN'}"

    def trigger_override(self, signal_type, trigger_name):
        overrides = self.strategy_config.get("trigger_overrides") or {}
        return overrides.get(self.trigger_key(signal_type, trigger_name)) or overrides.get(trigger_name) or {}

    def trigger_enabled(self, signal_type, trigger_name):
        override = self.trigger_override(signal_type, trigger_name)
        return override.get("enabled", True) is not False

    def trigger_review_mode(self, signal_type, trigger_name):
        override = self.trigger_override(signal_type, trigger_name)
        return str(override.get("review_mode") or "").strip().lower()

    def trigger_shadow_only(self, signal_type, trigger_name):
        return self.trigger_review_mode(signal_type, trigger_name).startswith("shadow_only")

    def trigger_cooldown_seconds(self, signal_type, trigger_name):
        override = self.trigger_override(signal_type, trigger_name)
        cooldown = as_int(override.get("cooldown_seconds"), self.strategy_config.get("signal_cooldown_seconds"))
        return cooldown if cooldown and cooldown > 0 else SIGNAL_COOLDOWN

    def alert_cooldown_key(self, symbol, signal_type, trigger_name):
        return f"{str(symbol or '').upper()}:{self.trigger_key(signal_type, trigger_name)}"

    def alert_signal_id(self, symbol, trigger_name, signal_type, now, cooldown_seconds, signal_date=None):
        bucket_seconds = cooldown_seconds if cooldown_seconds and cooldown_seconds > 0 else SIGNAL_COOLDOWN
        date_prefix = signal_date or datetime.now().strftime("%Y%m%d")
        return (
            f"{date_prefix}:{symbol}:{trigger_name}:{signal_type}:"
            f"{int(now // bucket_seconds)}"
        )

    def volume_anomaly_ratio(self):
        return as_float(self.strategy_config.get("volume_anomaly_ratio"), VOLUME_ANOMALY_RATIO) or VOLUME_ANOMALY_RATIO

    def risk_multiple(self, key, default):
        return as_float((self.strategy_config.get("risk_model") or {}).get(key), default) or default

    def min_rr_ratio(self):
        return as_float((self.strategy_config.get("risk_model") or {}).get("min_rr_ratio"), 1.2) or 1.2

    def emit_unconfirmed_directional_as_watch(self):
        return as_bool(
            (self.strategy_config.get("emission") or {}).get("emit_unconfirmed_directional_as_watch"),
            True,
        )

    def is_confirmed(self, signal_type, trigger_name, full_score):
        signal_type = str(signal_type or "").upper()
        if signal_type not in ("BUY", "SELL"):
            return True
        if full_score is None:
            return False
        thresholds = self.strategy_config.get("confirmation_thresholds") or {}
        override = self.trigger_override(signal_type, trigger_name)
        if signal_type == "BUY":
            threshold = as_float(override.get("min_full_score"), as_float((thresholds.get("BUY") or {}).get("min_full_score"), 0.25))
            return full_score >= threshold
        threshold = as_float(override.get("max_full_score"), as_float((thresholds.get("SELL") or {}).get("max_full_score"), -0.25))
        return full_score <= threshold

    @staticmethod
    def risk_geometry(signal_type, entry_price, stop_loss, take_profit):
        signal_type = str(signal_type or "").upper()
        if signal_type not in ("BUY", "SELL"):
            return True, None
        try:
            entry = float(entry_price)
            stop = float(stop_loss)
            take = float(take_profit)
        except (TypeError, ValueError):
            return False, "missing_or_invalid_risk_price"
        if not (math.isfinite(entry) and math.isfinite(stop) and math.isfinite(take)):
            return False, "missing_or_invalid_risk_price"
        if entry <= 0 or stop <= 0 or take <= 0:
            return False, "non_positive_risk_price"
        if signal_type == "BUY" and not (stop < entry < take):
            return False, "invalid_buy_risk_geometry"
        if signal_type == "SELL" and not (take < entry < stop):
            return False, "invalid_sell_risk_geometry"
        return True, None

    @staticmethod
    def risk_reward_ratio(signal_type, entry_price, stop_loss, take_profit):
        signal_type = str(signal_type or "").upper()
        try:
            entry = float(entry_price)
            stop = float(stop_loss)
            take = float(take_profit)
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(entry) and math.isfinite(stop) and math.isfinite(take)):
            return None
        if signal_type == "BUY":
            risk = entry - stop
            reward = take - entry
        elif signal_type == "SELL":
            risk = stop - entry
            reward = entry - take
        else:
            return None
        if risk <= 0 or reward <= 0:
            return None
        return round(reward / risk, 2)

    @staticmethod
    def risk_price_decimals(reference_price):
        price = as_float(reference_price)
        if price is None or price >= 1:
            return 2
        if price >= 0.1:
            return 3
        return 4

    @classmethod
    def round_risk_price(cls, value, reference_price=None):
        price = as_float(value)
        if price is None:
            return None
        decimals = cls.risk_price_decimals(reference_price if reference_price is not None else price)
        return round(price, decimals)

    def check(self, symbol, indicators, quote):
        """檢查所有觸發條件"""
        quote, _quote_error = normalize_quote(quote)
        if quote is None:
            return
        if not indicator_signal_ready(indicators):
            return

        c = quote["price"]

        now = time.time()
        triggered = []
        full_score, full_reasons = indicators.get_score(quote)
        full_score = as_float(full_score)
        full_reasons = full_reasons if isinstance(full_reasons, list) else []

        # 1. RSI 極端值
        if indicators.rsi_14 is not None:
            if indicators.rsi_14 <= 30:
                triggered.append(("RSI超賣", f"RSI={indicators.rsi_14:.0f}", "BUY"))
            elif indicators.rsi_14 >= 70:
                triggered.append(("RSI超買", f"RSI={indicators.rsi_14:.0f}", "SELL"))

        # 2. 布林帶突破
        if indicators.bb_upper and indicators.bb_lower:
            if c <= indicators.bb_lower:
                triggered.append(("布林下軌突破", f"價格${c} < 下軌${indicators.bb_lower:.2f}", "BUY"))
            elif c >= indicators.bb_upper:
                triggered.append(("布林上軌突破", f"價格${c} > 上軌${indicators.bb_upper:.2f}", "SELL"))

        # 3. 均線金叉/死叉
        if indicators.ma5 and indicators.ma10 and len(indicators.closes) >= 5:
            prev_c = indicators.closes[-1]
            prev_ma5 = sum(indicators.closes[-5:]) / 5
            if c > indicators.ma5 and prev_c <= prev_ma5:
                triggered.append(("站上MA5", f"${c} > MA5=${indicators.ma5:.2f}", "BUY"))
            if c < indicators.ma5 and prev_c >= prev_ma5:
                triggered.append(("跌破MA5", f"${c} < MA5=${indicators.ma5:.2f}", "SELL"))

        if indicators.ma10 and indicators.ma20:
            if len(indicators.closes) >= 20:
                prev_ma10 = sum(indicators.closes[-10:]) / 10
                prev_ma20 = sum(indicators.closes[-20:]) / 20
                if indicators.ma10 > indicators.ma20 and prev_ma10 <= prev_ma20:
                    triggered.append(("MA金叉", f"MA10上穿MA20", "BUY"))
                if indicators.ma10 < indicators.ma20 and prev_ma10 >= prev_ma20:
                    triggered.append(("MA死叉", f"MA10下穿MA20", "SELL"))

        # 4. 成交量異動
        if len(indicators.volumes) >= 20:
            avg_vol = sum(indicators.volumes[-20:]) / 20
            if avg_vol > 0 and quote.get("volume", 0) > 0:
                vol_ratio = cumulative_volume_ratio(
                    quote.get("volume"),
                    avg_vol,
                    quote.get("market"),
                    quote.get("time"),
                )
                if vol_ratio is not None and vol_ratio > self.volume_anomaly_ratio():
                    triggered.append(("成交量異動", f"量比={vol_ratio:.1f}", "WATCH"))

        # 5. 大幅波動
        if abs(quote.get("change_pct", 0)) >= 5:
            direction = "急漲" if quote["change_pct"] > 0 else "急跌"
            triggered.append((direction, f"{quote['change_pct']:+.1f}%", "WATCH"))

        # 冷卻期檢查 + 觸發
        for trigger_name, detail, signal_type in triggered:
            if not self.trigger_enabled(signal_type, trigger_name):
                continue
            cooldown_seconds = self.trigger_cooldown_seconds(signal_type, trigger_name)
            
            # 計算入場/止盈/止損 (基於ATR)
            atr = as_float(indicators.atr_14)
            if atr is None or atr <= 0:
                atr = c * 0.02  # 默認2%
            stop_multiple = self.risk_multiple("atr_stop_multiple", 2.0)
            take_profit_multiple = self.risk_multiple("atr_take_profit_multiple", 3.0)
            
            confirmed = self.is_confirmed(signal_type, trigger_name, full_score)
            candidate_entry_price = self.round_risk_price(c)
            if signal_type == "BUY":
                candidate_stop_loss = self.round_risk_price(c - stop_multiple * atr, reference_price=c)
                candidate_take_profit = self.round_risk_price(c + take_profit_multiple * atr, reference_price=c)
            elif signal_type == "SELL":
                candidate_stop_loss = self.round_risk_price(c + stop_multiple * atr, reference_price=c)
                candidate_take_profit = self.round_risk_price(c - take_profit_multiple * atr, reference_price=c)
            else:
                candidate_stop_loss = None
                candidate_take_profit = None
            candidate_rr_ratio = self.risk_reward_ratio(
                signal_type,
                candidate_entry_price,
                candidate_stop_loss,
                candidate_take_profit,
            )
            risk_geometry_valid, risk_geometry_reason = self.risk_geometry(
                signal_type,
                candidate_entry_price,
                candidate_stop_loss,
                candidate_take_profit,
            )
            min_rr_ratio = self.min_rr_ratio() if signal_type in ("BUY", "SELL") else None
            if (
                signal_type in ("BUY", "SELL")
                and risk_geometry_valid
                and candidate_rr_ratio is not None
                and min_rr_ratio is not None
                and candidate_rr_ratio < min_rr_ratio
            ):
                risk_geometry_valid = False
                risk_geometry_reason = "rr_ratio_below_minimum"

            emitted_signal_type = signal_type
            suppressed_directional_reason = None
            trigger_review_mode = self.trigger_review_mode(signal_type, trigger_name)
            trigger_shadow_only = self.trigger_shadow_only(signal_type, trigger_name)
            if signal_type in ("BUY", "SELL") and trigger_shadow_only:
                emitted_signal_type = "WATCH"
                suppressed_directional_reason = "strategy_review_shadow_only"
            if (
                signal_type in ("BUY", "SELL")
                and not confirmed
                and self.emit_unconfirmed_directional_as_watch()
                and emitted_signal_type in ("BUY", "SELL")
            ):
                emitted_signal_type = "WATCH"
                suppressed_directional_reason = "unconfirmed_directional"
            if signal_type in ("BUY", "SELL") and not risk_geometry_valid:
                emitted_signal_type = "WATCH"
                suppressed_directional_reason = risk_geometry_reason

            key = self.alert_cooldown_key(symbol, emitted_signal_type, trigger_name)
            if key in self.cooldowns and now - self.cooldowns[key] < cooldown_seconds:
                continue
            self.cooldowns[key] = now

            if emitted_signal_type in ("BUY", "SELL"):
                entry_price = candidate_entry_price
                stop_loss = candidate_stop_loss
                take_profit = candidate_take_profit
                rr_ratio = candidate_rr_ratio
            else:
                entry_price = candidate_entry_price
                stop_loss = None
                take_profit = None
                rr_ratio = None
            
            market = quote.get("market", "")
            generated_at = datetime.now()
            signal_date = alert_signal_date(quote.get("time"), generated_at=generated_at)
            self.alerts.append({
                "signal_id": self.alert_signal_id(
                    symbol,
                    trigger_name,
                    emitted_signal_type,
                    now,
                    cooldown_seconds,
                    signal_date=signal_date,
                ),
                "source": "rt_signal_engine_v5",
                "symbol": symbol,
                "market": market,
                **alert_watchlist_metadata(self.watchlist_context, market),
                **alert_strategy_metadata(self.strategy_context),
                "trigger": trigger_name,
                "detail": detail,
                "signal_type": emitted_signal_type,
                "candidate_signal_type": signal_type,
                "trigger_review_mode": trigger_review_mode or None,
                "strategy_policy_shadow_only": trigger_shadow_only,
                "suppressed_directional_reason": suppressed_directional_reason,
                "execution_candidate": emitted_signal_type in ("BUY", "SELL") and confirmed,
                "confirmed": confirmed,
                "risk_geometry_valid": risk_geometry_valid,
                "risk_geometry_reason": risk_geometry_reason,
                "full_score": round(full_score, 3) if full_score is not None else None,
                "full_reasons": full_reasons[:5],
                "price": c,
                "change_pct": quote.get("change_pct", 0),
                "quote_time": quote.get("time", ""),
                "time": generated_at.strftime("%H:%M:%S"),
                "generated_at": generated_at.isoformat(timespec="seconds"),
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "rr_ratio": rr_ratio,
                "candidate_entry_price": candidate_entry_price,
                "candidate_stop_loss": candidate_stop_loss,
                "candidate_take_profit": candidate_take_profit,
                "candidate_rr_ratio": candidate_rr_ratio,
                "min_rr_ratio": min_rr_ratio,
                "atr": round(atr, 3),
            })


# ========== 主循環 ==========
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def default_state():
    return {"cooldowns": {}, "date": ""}

def normalize_state(payload):
    state = default_state()
    if not isinstance(payload, dict):
        return state

    cooldowns = payload.get("cooldowns")
    if isinstance(cooldowns, dict):
        for raw_key, raw_value in cooldowns.items():
            key = str(raw_key or "").strip()
            value = as_float(raw_value)
            if key and value is not None and value >= 0:
                state["cooldowns"][key] = value

    date = str(payload.get("date") or "").strip()
    if date:
        state["date"] = date
    return state

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return normalize_state(json.load(f))
    except (OSError, json.JSONDecodeError):
        return default_state()

def save_state(state):
    payload = normalize_state(state)
    tmp = f"{STATE_FILE}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, allow_nan=False)
        os.replace(tmp, STATE_FILE)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass

def send_alert(alerts):
    """寫入最新alert文件，同時追加到事件隊列供Hermes無損消費。"""
    latest_payload = json.dumps(alerts, ensure_ascii=False, indent=2, allow_nan=False)
    queue_lines = [json.dumps(alert, ensure_ascii=False, allow_nan=False) for alert in alerts]

    with open(ALERT_QUEUE_FILE, "a", encoding="utf-8") as f:
        for line in queue_lines:
            f.write(line + "\n")

    tmp = ALERT_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(latest_payload)
        os.replace(tmp, ALERT_FILE)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise

def main():
    hk_watchlist, us_watchlist, watchlist_context = load_watchlists()
    strategy_config, strategy_context = load_strategy_config()
    log("=" * 60)
    log("實時信號引擎 v5.0 啟動")
    log(
        f"港股: {len(hk_watchlist)}隻 ({watchlist_context['markets']['HK']['source']}) | "
        f"美股: {len(us_watchlist)}隻 ({watchlist_context['markets']['US']['source']}) | "
        f"watchlist_id={watchlist_context['watchlist_id']}"
    )
    log(
        f"strategy_config_id={strategy_context['strategy_config_id']} "
        f"({strategy_context['source']}, version={strategy_context.get('version')})"
    )
    for warning in watchlist_context.get("warnings") or []:
        log(f"watchlist warning: {warning}")
    for warning in strategy_context.get("warnings") or []:
        log(f"strategy config warning: {warning}")
    log("=" * 60)

    # 初始化指標
    indicators = {}
    all_symbols = [(s, "HK") for s in hk_watchlist] + [(s, "US") for s in us_watchlist]

    log("載入歷史K線...")
    skipped_history = []
    for sym, market in all_symbols:
        ind = IncrementalIndicators(sym)
        loaded = ind.load_history(100)
        if indicator_signal_ready(ind):
            indicators[sym] = ind
        else:
            reason = "load_failed" if not loaded else "insufficient_daily_history"
            skipped_history.append((sym, market, indicator_history_bar_count(ind), reason))
    log(
        f"載入完成: signal_ready={len(indicators)} skipped={len(skipped_history)} "
        f"min_daily_bars={MIN_SIGNAL_HISTORY_BARS}"
    )
    if skipped_history:
        sample = ", ".join(
            f"{sym}/{market}:{bars}:{reason}"
            for sym, market, bars, reason in skipped_history[:10]
        )
        suffix = " ..." if len(skipped_history) > 10 else ""
        log(f"歷史K線不足跳過: {sample}{suffix}")

    trigger = TriggerEngine(
        watchlist_context=watchlist_context,
        strategy_config=strategy_config,
        strategy_context=strategy_context,
    )
    state = load_state()
    trigger.cooldowns = state.get("cooldowns", {})

    last_full_scan = 0
    cycle = 0

    while True:
        now = time.time()
        cycle += 1

        # 判斷交易時間
        dt = datetime.now()
        hk_open, us_open = market_open_flags_hkt(dt)

        if not hk_open and not us_open:
            if cycle % 100 == 0:
                log(f"非交易時間 (HK:{hk_open} US:{us_open}), 等待...")
            time.sleep(30)
            continue

        # 拉取實時報價
        hk_quotes = {}
        us_quotes = {}
        if hk_open:
            hk_quotes = fetch_hk_quotes(hk_watchlist)
        if us_open:
            us_quotes = fetch_us_quotes(us_watchlist)

        all_quotes = {**hk_quotes, **us_quotes}

        if not all_quotes:
            if cycle % 100 == 0:
                log("冇報價數據")
            time.sleep(POLL_INTERVAL)
            continue

        # 全量條件檢查（每30秒一次）
        if now - last_full_scan >= FULL_SCAN_INTERVAL:
            trigger.alerts = []
            for sym, quote in all_quotes.items():
                if sym in indicators:
                    quote, _quote_error = normalize_quote(quote)
                    if quote is None:
                        continue
                    # 用實時價格更新指標（增量）
                    indicators[sym].update_realtime(
                        quote["price"], quote["high"], quote["low"], quote["volume"]
                    )
                    trigger.check(sym, indicators[sym], quote)

            if trigger.alerts:
                log(f"🚨 觸發 {len(trigger.alerts)} 個信號!")
                for alert in trigger.alerts:
                    log(f"  {alert['symbol']} {alert['trigger']}: {alert['detail']} [{alert['signal_type']}]")
                send_alert(trigger.alerts)

                # 更新冷卻狀態
                state["cooldowns"] = trigger.cooldowns
                state["date"] = dt.strftime("%Y-%m-%d")
                save_state(state)
            else:
                if cycle % 100 == 0:
                    log(f"掃描完成: {len(all_quotes)}隻報價, 0個觸發")

            last_full_scan = now

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
