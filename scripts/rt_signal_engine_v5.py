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
def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


POLL_INTERVAL = 3       # 每3秒拉一次報價
FULL_SCAN_INTERVAL = 30 # 每30秒做一次全量條件檢查
SIGNAL_COOLDOWN = 1800  # 同一信號30分鐘內唔重複觸發
ALERT_FILE = "/tmp/rt_signal_alert.json"
ALERT_QUEUE_FILE = "/tmp/rt_signal_alerts.jsonl"
STATE_FILE = "/tmp/rt_signal_state.json"
SESSION_KEY_RETENTION_DAYS = 7
STATE_BACKFILL_ALERT_QUEUE_LIMIT = env_int("RT_SIGNAL_STATE_BACKFILL_ALERT_QUEUE_LIMIT", 5000)
WATCHLIST_FILE = os.environ.get("RT_SIGNAL_WATCHLIST_FILE", "/root/rt_signal_watchlist.json")
STRATEGY_CONFIG_FILE = os.environ.get("RT_SIGNAL_STRATEGY_CONFIG_FILE", "/root/rt_signal_strategy_config.json")
MAX_QUOTE_AGE_SECONDS = 15 * 60
MAX_QUOTE_FUTURE_SKEW_SECONDS = 120
MIN_SIGNAL_HISTORY_BARS = 30
MIN_VOLUME_SESSION_FRACTION = 0.05
MOMENTUM_THRESHOLD_PCT = 5.0
VOLUME_ANOMALY_RATIO = 3.0
BUY_CONFIRMATION_MIN_SCORE = 0.45
SELL_CONFIRMATION_MAX_SCORE = -0.45
SESSION_MOMENTUM_THRESHOLD_PCT = 3.0
SESSION_MOMENTUM_SCORE_DELTA = 0.4
BOLLINGER_BREAKOUT_BUY_MIN_CHANGE_PCT = 2.0
BOLLINGER_BREAKOUT_BUY_MIN_SCORE = 0.8
BOLLINGER_BREAKOUT_BUY_MIN_SUPPORTING_FACTORS = 3
MIN_SUPPORTING_FACTOR_COUNT = {
    "BUY": 2,
    "SELL": 2,
}
MIN_AVG_DAILY_TURNOVER = {
    "HK": 100_000.0,
    "US": 100_000.0,
}
MARKET_BREADTH_MIN_SAMPLE = 10
MARKET_BREADTH_RISK_OFF_MAX_ADVANCER_PCT = 35.0
MARKET_BREADTH_RISK_OFF_MIN_DECLINER_PCT = 50.0
MARKET_BREADTH_RISK_OFF_MAX_AVG_CHANGE_PCT = -0.6
BUY_REALTIME_ALIGNMENT_MIN_CHANGE_PCT = 0.0
TRIGGER_EXECUTION_PRIORITY = {
    "BUY": {
        "急漲": 60,
        "布林上軌動量突破": 55,
        "站上MA5": 45,
        "MA金叉": 40,
        "布林下軌突破": 25,
        "RSI超賣": 20,
    },
    "SELL": {
        "MA死叉": 55,
        "跌破MA5": 45,
        "急跌": 40,
        "RSI超買": 25,
        "布林上軌突破": 20,
    },
}
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
            "BUY": {"min_full_score": BUY_CONFIRMATION_MIN_SCORE},
            "SELL": {"max_full_score": SELL_CONFIRMATION_MAX_SCORE}
        },
        "confirmation_requirements": {
            "BUY": {"min_supporting_factor_count": MIN_SUPPORTING_FACTOR_COUNT["BUY"]},
            "SELL": {"min_supporting_factor_count": MIN_SUPPORTING_FACTOR_COUNT["SELL"]}
        },
        "momentum_breakout_model": {
            "enabled": True,
            "large_move_buy_pct": MOMENTUM_THRESHOLD_PCT,
            "large_move_sell_enabled": False,
            "same_session_momentum_pct": SESSION_MOMENTUM_THRESHOLD_PCT,
            "same_session_score_delta": SESSION_MOMENTUM_SCORE_DELTA,
            "bollinger_buy_min_change_pct": BOLLINGER_BREAKOUT_BUY_MIN_CHANGE_PCT,
            "bollinger_buy_min_score": BOLLINGER_BREAKOUT_BUY_MIN_SCORE,
            "bollinger_buy_min_supporting_factors": BOLLINGER_BREAKOUT_BUY_MIN_SUPPORTING_FACTORS
        },
        "risk_model": {
            "atr_stop_multiple": 2.0,
            "atr_take_profit_multiple": 3.0,
            "min_rr_ratio": 1.2
        },
        "liquidity_model": {
            "min_avg_daily_turnover": dict(MIN_AVG_DAILY_TURNOVER)
        },
        "market_breadth_model": {
            "enabled": False,
            "block_new_buy_in_risk_off": True,
            "min_sample_size": MARKET_BREADTH_MIN_SAMPLE,
            "risk_off_max_advancer_pct": MARKET_BREADTH_RISK_OFF_MAX_ADVANCER_PCT,
            "risk_off_min_decliner_pct": MARKET_BREADTH_RISK_OFF_MIN_DECLINER_PCT,
            "risk_off_max_avg_change_pct": MARKET_BREADTH_RISK_OFF_MAX_AVG_CHANGE_PCT,
        },
        "realtime_alignment": {
            "block_buy_when_change_pct_below": BUY_REALTIME_ALIGNMENT_MIN_CHANGE_PCT,
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

def score_contribution(category, direction, score_delta, reason):
    direction = str(direction or "").upper()
    score_delta = as_float(score_delta)
    if direction not in ("BUY", "SELL") or score_delta is None or score_delta == 0:
        return None
    if direction == "BUY" and score_delta < 0:
        return None
    if direction == "SELL" and score_delta > 0:
        return None
    return {
        "category": str(category or "").strip().lower(),
        "direction": direction,
        "score_delta": round(score_delta, 4),
        "reason": str(reason or ""),
    }

def normalize_score_contributions(contributions):
    normalized = []
    for contribution in contributions or []:
        if not isinstance(contribution, dict):
            continue
        raw_delta = contribution.get("score_delta")
        if raw_delta is None and contribution.get("points") is not None:
            raw_points = as_float(contribution.get("points"))
            raw_direction = str(contribution.get("direction") or "").upper()
            raw_delta = -raw_points if raw_direction == "SELL" and raw_points is not None else raw_points
        item = score_contribution(
            contribution.get("category"),
            contribution.get("direction"),
            raw_delta,
            contribution.get("reason"),
        )
        if item and item["category"] and item["reason"]:
            normalized.append(item)
    return normalized

def score_result(score, reasons, contributions):
    return {
        "score": score,
        "reasons": reasons,
        "factor_contributions": normalize_score_contributions(contributions),
    }

def unpack_score_result(raw_result):
    if isinstance(raw_result, dict):
        return (
            raw_result.get("score"),
            raw_result.get("reasons") if isinstance(raw_result.get("reasons"), list) else [],
            normalize_score_contributions(raw_result.get("factor_contributions")),
        )
    if isinstance(raw_result, (list, tuple)):
        score = raw_result[0] if len(raw_result) >= 1 else None
        reasons = raw_result[1] if len(raw_result) >= 2 and isinstance(raw_result[1], list) else []
        contributions = raw_result[2] if len(raw_result) >= 3 else None
        return score, reasons, normalize_score_contributions(contributions)
    return None, [], []

def valid_watchlist_symbol(symbol, market=None):
    market = str(market or "").upper()
    if market == "HK":
        return bool(HK_SYMBOL_RE.match(symbol))
    if market == "US":
        return bool(US_SYMBOL_RE.match(symbol))
    return bool(HK_SYMBOL_RE.match(symbol) or US_SYMBOL_RE.match(symbol))

def infer_market_from_symbol(symbol):
    symbol = str(symbol or "").strip().upper()
    if HK_SYMBOL_RE.match(symbol):
        return "HK"
    if US_SYMBOL_RE.match(symbol):
        return "US"
    return ""

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
        "confirmation_requirements": config.get("confirmation_requirements"),
        "momentum_breakout_model": config.get("momentum_breakout_model"),
        "risk_model": config.get("risk_model"),
        "liquidity_model": config.get("liquidity_model"),
        "market_breadth_model": config.get("market_breadth_model"),
        "realtime_alignment": config.get("realtime_alignment"),
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
    for key in (
        "confirmation_thresholds",
        "confirmation_requirements",
        "momentum_breakout_model",
        "risk_model",
        "liquidity_model",
        "market_breadth_model",
        "realtime_alignment",
        "emission",
        "trigger_overrides",
    ):
        if isinstance(override.get(key), dict):
            merged.setdefault(key, {})
            for sub_key, value in override[key].items():
                if isinstance(value, dict) and isinstance(merged[key].get(sub_key), dict):
                    merged[key][sub_key].update(value)
                else:
                    merged[key][sub_key] = value
    return merged

def normalize_global_score_threshold(value, default, warning_code, warnings, direction):
    threshold = as_float(value)
    invalid = threshold is None or threshold < -1 or threshold > 1
    if direction == "BUY" and threshold is not None and threshold < default:
        invalid = True
    if direction == "SELL" and threshold is not None and threshold > default:
        invalid = True
    if invalid:
        warnings.append(warning_code)
        return default
    return threshold

def normalize_override_score_threshold(override, key, field, warnings):
    if field not in override:
        return
    threshold = as_float(override.get(field))
    invalid = threshold is None or threshold < -1 or threshold > 1
    if field == "min_full_score" and threshold is not None and threshold < BUY_CONFIRMATION_MIN_SCORE:
        invalid = True
    if field == "max_full_score" and threshold is not None and threshold > SELL_CONFIRMATION_MAX_SCORE:
        invalid = True
    if invalid:
        warnings.append(f"invalid_trigger_{field}:{key}")
        override.pop(field, None)
        return
    override[field] = threshold

def normalize_strategy_config(config):
    config = merge_strategy_config(default_strategy_config(), config)
    warnings = []
    cooldown = as_int(config.get("signal_cooldown_seconds"), SIGNAL_COOLDOWN)
    if cooldown is None or cooldown <= 0:
        warnings.append("invalid_signal_cooldown_seconds_using_default")
        cooldown = SIGNAL_COOLDOWN
    config["signal_cooldown_seconds"] = cooldown

    volume_ratio = as_float(config.get("volume_anomaly_ratio"), VOLUME_ANOMALY_RATIO)
    if volume_ratio is None or volume_ratio < VOLUME_ANOMALY_RATIO:
        warnings.append("invalid_volume_anomaly_ratio_using_default")
        volume_ratio = VOLUME_ANOMALY_RATIO
    config["volume_anomaly_ratio"] = volume_ratio

    thresholds = config.setdefault("confirmation_thresholds", {})
    buy = thresholds.setdefault("BUY", {})
    sell = thresholds.setdefault("SELL", {})
    buy["min_full_score"] = normalize_global_score_threshold(
        buy.get("min_full_score"),
        BUY_CONFIRMATION_MIN_SCORE,
        "invalid_buy_min_full_score_using_default",
        warnings,
        "BUY",
    )
    sell["max_full_score"] = normalize_global_score_threshold(
        sell.get("max_full_score"),
        SELL_CONFIRMATION_MAX_SCORE,
        "invalid_sell_max_full_score_using_default",
        warnings,
        "SELL",
    )

    requirements = config.setdefault("confirmation_requirements", {})
    for side, default in MIN_SUPPORTING_FACTOR_COUNT.items():
        side_req = requirements.setdefault(side, {})
        count = as_int(side_req.get("min_supporting_factor_count"), default)
        if count is None or count < default:
            warnings.append(f"invalid_{side.lower()}_min_supporting_factor_count_using_default")
            count = default
        side_req["min_supporting_factor_count"] = count

    momentum_breakout = config.setdefault("momentum_breakout_model", {})
    momentum_breakout["enabled"] = as_bool(momentum_breakout.get("enabled"), True)
    large_move_buy_pct = as_float(momentum_breakout.get("large_move_buy_pct"), MOMENTUM_THRESHOLD_PCT)
    if large_move_buy_pct is None or large_move_buy_pct < MOMENTUM_THRESHOLD_PCT:
        warnings.append("invalid_momentum_breakout_large_move_buy_pct_using_default")
        large_move_buy_pct = MOMENTUM_THRESHOLD_PCT
    momentum_breakout["large_move_buy_pct"] = large_move_buy_pct
    momentum_breakout["large_move_sell_enabled"] = as_bool(
        momentum_breakout.get("large_move_sell_enabled"),
        False,
    )
    same_session_momentum_pct = as_float(
        momentum_breakout.get("same_session_momentum_pct"),
        SESSION_MOMENTUM_THRESHOLD_PCT,
    )
    if same_session_momentum_pct is None or same_session_momentum_pct < SESSION_MOMENTUM_THRESHOLD_PCT:
        warnings.append("invalid_same_session_momentum_pct_using_default")
        same_session_momentum_pct = SESSION_MOMENTUM_THRESHOLD_PCT
    momentum_breakout["same_session_momentum_pct"] = same_session_momentum_pct
    same_session_score_delta = as_float(
        momentum_breakout.get("same_session_score_delta"),
        SESSION_MOMENTUM_SCORE_DELTA,
    )
    if (
        same_session_score_delta is None
        or same_session_score_delta < 0
        or same_session_score_delta > SESSION_MOMENTUM_SCORE_DELTA
    ):
        warnings.append("invalid_same_session_score_delta_using_default")
        same_session_score_delta = SESSION_MOMENTUM_SCORE_DELTA
    momentum_breakout["same_session_score_delta"] = same_session_score_delta
    bollinger_buy_min_change_pct = as_float(
        momentum_breakout.get("bollinger_buy_min_change_pct"),
        BOLLINGER_BREAKOUT_BUY_MIN_CHANGE_PCT,
    )
    if (
        bollinger_buy_min_change_pct is None
        or bollinger_buy_min_change_pct < BOLLINGER_BREAKOUT_BUY_MIN_CHANGE_PCT
    ):
        warnings.append("invalid_bollinger_buy_min_change_pct_using_default")
        bollinger_buy_min_change_pct = BOLLINGER_BREAKOUT_BUY_MIN_CHANGE_PCT
    momentum_breakout["bollinger_buy_min_change_pct"] = bollinger_buy_min_change_pct
    bollinger_buy_min_score = as_float(
        momentum_breakout.get("bollinger_buy_min_score"),
        BOLLINGER_BREAKOUT_BUY_MIN_SCORE,
    )
    if bollinger_buy_min_score is None or bollinger_buy_min_score < BUY_CONFIRMATION_MIN_SCORE:
        warnings.append("invalid_bollinger_buy_min_score_using_default")
        bollinger_buy_min_score = BOLLINGER_BREAKOUT_BUY_MIN_SCORE
    momentum_breakout["bollinger_buy_min_score"] = bollinger_buy_min_score
    bollinger_buy_min_supporting_factors = as_int(
        momentum_breakout.get("bollinger_buy_min_supporting_factors"),
        BOLLINGER_BREAKOUT_BUY_MIN_SUPPORTING_FACTORS,
    )
    if (
        bollinger_buy_min_supporting_factors is None
        or bollinger_buy_min_supporting_factors < BOLLINGER_BREAKOUT_BUY_MIN_SUPPORTING_FACTORS
    ):
        warnings.append("invalid_bollinger_buy_min_supporting_factors_using_default")
        bollinger_buy_min_supporting_factors = BOLLINGER_BREAKOUT_BUY_MIN_SUPPORTING_FACTORS
    momentum_breakout["bollinger_buy_min_supporting_factors"] = bollinger_buy_min_supporting_factors

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
    if risk["min_rr_ratio"] is None or risk["min_rr_ratio"] < 1.2:
        warnings.append("invalid_min_rr_ratio_using_default")
        risk["min_rr_ratio"] = 1.2

    liquidity = config.setdefault("liquidity_model", {})
    raw_min_turnover = (
        liquidity.get("min_avg_daily_turnover")
        if isinstance(liquidity.get("min_avg_daily_turnover"), dict)
        else {}
    )
    normalized_min_turnover = {}
    for market, default in MIN_AVG_DAILY_TURNOVER.items():
        value = raw_min_turnover.get(market)
        if value is None:
            value = raw_min_turnover.get(market.lower())
        value = as_float(value, default)
        if value is None or value < default:
            warnings.append(f"invalid_min_avg_daily_turnover_{market.lower()}_using_default")
            value = default
        normalized_min_turnover[market] = value
    liquidity["min_avg_daily_turnover"] = normalized_min_turnover

    breadth = config.setdefault("market_breadth_model", {})
    breadth["enabled"] = as_bool(breadth.get("enabled"), False)
    breadth["block_new_buy_in_risk_off"] = as_bool(
        breadth.get("block_new_buy_in_risk_off"),
        True,
    )
    breadth["min_sample_size"] = as_int(breadth.get("min_sample_size"), MARKET_BREADTH_MIN_SAMPLE)
    if breadth["min_sample_size"] is None or breadth["min_sample_size"] < 1:
        warnings.append("invalid_market_breadth_min_sample_size_using_default")
        breadth["min_sample_size"] = MARKET_BREADTH_MIN_SAMPLE
    breadth["risk_off_max_advancer_pct"] = as_float(
        breadth.get("risk_off_max_advancer_pct"),
        MARKET_BREADTH_RISK_OFF_MAX_ADVANCER_PCT,
    )
    if breadth["risk_off_max_advancer_pct"] is None or not 0 <= breadth["risk_off_max_advancer_pct"] <= 100:
        warnings.append("invalid_market_breadth_risk_off_max_advancer_pct_using_default")
        breadth["risk_off_max_advancer_pct"] = MARKET_BREADTH_RISK_OFF_MAX_ADVANCER_PCT
    breadth["risk_off_min_decliner_pct"] = as_float(
        breadth.get("risk_off_min_decliner_pct"),
        MARKET_BREADTH_RISK_OFF_MIN_DECLINER_PCT,
    )
    if breadth["risk_off_min_decliner_pct"] is None or not 0 <= breadth["risk_off_min_decliner_pct"] <= 100:
        warnings.append("invalid_market_breadth_risk_off_min_decliner_pct_using_default")
        breadth["risk_off_min_decliner_pct"] = MARKET_BREADTH_RISK_OFF_MIN_DECLINER_PCT
    breadth["risk_off_max_avg_change_pct"] = as_float(
        breadth.get("risk_off_max_avg_change_pct"),
        MARKET_BREADTH_RISK_OFF_MAX_AVG_CHANGE_PCT,
    )
    if (
        breadth["risk_off_max_avg_change_pct"] is None
        or breadth["risk_off_max_avg_change_pct"] < -20
        or breadth["risk_off_max_avg_change_pct"] > 20
    ):
        warnings.append("invalid_market_breadth_risk_off_max_avg_change_pct_using_default")
        breadth["risk_off_max_avg_change_pct"] = MARKET_BREADTH_RISK_OFF_MAX_AVG_CHANGE_PCT

    realtime_alignment = config.setdefault("realtime_alignment", {})
    buy_min_change = as_float(
        realtime_alignment.get("block_buy_when_change_pct_below"),
        BUY_REALTIME_ALIGNMENT_MIN_CHANGE_PCT,
    )
    if buy_min_change is None or buy_min_change < -20 or buy_min_change > 20:
        warnings.append("invalid_realtime_alignment_buy_min_change_pct_using_default")
        buy_min_change = BUY_REALTIME_ALIGNMENT_MIN_CHANGE_PCT
    realtime_alignment["block_buy_when_change_pct_below"] = buy_min_change

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
        normalize_override_score_threshold(override, key, "min_full_score", warnings)
        normalize_override_score_threshold(override, key, "max_full_score", warnings)
        if "enabled" in override:
            override["enabled"] = as_bool(override.get("enabled"), True)
        if "cooldown_seconds" in override:
            override["cooldown_seconds"] = as_int(override.get("cooldown_seconds"))
            if override["cooldown_seconds"] is None or override["cooldown_seconds"] <= 0:
                warnings.append(f"invalid_trigger_cooldown_seconds:{key}")
                override.pop("cooldown_seconds", None)
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
    env_buy_factor_count = env.get("RT_SIGNAL_BUY_MIN_SUPPORTING_FACTOR_COUNT")
    env_sell_factor_count = env.get("RT_SIGNAL_SELL_MIN_SUPPORTING_FACTOR_COUNT")
    env_volume = env.get("RT_SIGNAL_VOLUME_ANOMALY_RATIO")
    env_hk_turnover = env.get("RT_SIGNAL_HK_MIN_AVG_DAILY_TURNOVER")
    env_us_turnover = env.get("RT_SIGNAL_US_MIN_AVG_DAILY_TURNOVER")
    env_cooldown = env.get("RT_SIGNAL_COOLDOWN_SECONDS")
    env_emit_unconfirmed = env.get("RT_SIGNAL_EMIT_UNCONFIRMED_DIRECTIONAL_AS_WATCH")
    if env_buy is not None:
        config.setdefault("confirmation_thresholds", {}).setdefault("BUY", {})["min_full_score"] = env_buy
        source = "env"
    if env_sell is not None:
        config.setdefault("confirmation_thresholds", {}).setdefault("SELL", {})["max_full_score"] = env_sell
        source = "env"
    if env_buy_factor_count is not None:
        config.setdefault("confirmation_requirements", {}).setdefault("BUY", {})[
            "min_supporting_factor_count"
        ] = env_buy_factor_count
        source = "env"
    if env_sell_factor_count is not None:
        config.setdefault("confirmation_requirements", {}).setdefault("SELL", {})[
            "min_supporting_factor_count"
        ] = env_sell_factor_count
        source = "env"
    if env_volume is not None:
        config["volume_anomaly_ratio"] = env_volume
        source = "env"
    if env_hk_turnover is not None:
        config.setdefault("liquidity_model", {}).setdefault("min_avg_daily_turnover", {})["HK"] = env_hk_turnover
        source = "env"
    if env_us_turnover is not None:
        config.setdefault("liquidity_model", {}).setdefault("min_avg_daily_turnover", {})["US"] = env_us_turnover
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

def alert_timeframe_metadata():
    return {
        "timeframe_scope": "completed_daily_ohlcv_with_realtime_quote",
        "primary_timeframe": "1d",
        "realtime_input": "single_quote_temporary_bar",
        "intraday_minute_bars_used": False,
        "intraday_evidence_policy": "external_read_only_context_only",
    }

def alert_daily_history_metadata(indicators):
    return {
        "daily_history_policy": getattr(
            indicators,
            "history_policy",
            "completed_daily_before_market_date",
        ),
        "daily_history_market": getattr(indicators, "history_market", None),
        "daily_history_cutoff_date": getattr(indicators, "history_cutoff_date", None),
        "daily_history_latest_date": getattr(indicators, "latest_daily_date", None),
        "daily_history_bar_count": indicator_history_bar_count(indicators),
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
                    "volume_unit": "board_lot",
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
    market = str(quote.get("market") or "").strip().upper()
    if market not in ("HK", "US"):
        return None, "missing_or_invalid_market"

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
            "time": quote_time_text(quote.get("time")),
            "market": market,
        }
    )
    return normalized, None

def quote_volume_as_shares(quote):
    if not isinstance(quote, dict):
        return None
    volume = as_float(quote.get("volume"))
    if volume is None:
        return None
    if volume <= 0:
        return 0
    raw_unit = quote.get("volume_unit")
    unit = str(raw_unit if raw_unit not in (None, "") else "shares").strip().lower()
    if unit in ("share", "shares"):
        return volume
    if unit in ("board_lot", "board_lots", "lot", "lots", "hand", "hands"):
        lot_size = as_float(
            quote.get("lot_size")
            or quote.get("board_lot_size")
            or quote.get("volume_lot_size")
        )
        if lot_size is None or lot_size <= 0:
            return None
        return volume * lot_size
    return None

def quote_time_text(value):
    if value in (None, ""):
        return ""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return str(isoformat()).strip()
        except Exception:
            pass
    return str(value).strip()

def parse_quote_datetime(value, assume_today_for_time_only=False, market=None):
    if not value:
        return None
    value = str(value).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y%m%d%H%M%S",
        "%Y%m%d%H%M",
    ):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    if assume_today_for_time_only:
        try:
            parsed = datetime.strptime(value, "%H:%M:%S")
            today = datetime.now()
            return parsed.replace(year=today.year, month=today.month, day=today.day)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None and market:
            tz = timezone_for_market(market, reference=parsed)
            if tz is not None:
                return parsed.astimezone(tz).replace(tzinfo=None)
        return parsed.replace(tzinfo=None)
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

def timezone_for_market(market, reference=None):
    market = str(market or "").upper()
    if ZoneInfo:
        try:
            if market == "US":
                return ZoneInfo("America/New_York")
            if market == "HK":
                return ZoneInfo("Asia/Hong_Kong")
        except Exception:
            pass
    if market == "HK":
        return timezone(timedelta(hours=8))
    if market == "US":
        offset = -4 if reference is not None and us_dst_active_for_utc(reference) else -5
        return timezone(timedelta(hours=offset))
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

def market_local_now(market, now=None):
    now = now or datetime.now()
    market = str(market or "").upper()
    if market == "HK":
        if getattr(now, "tzinfo", None):
            return now.astimezone(timezone_for_market("HK", reference=now)).replace(tzinfo=None)
        return now
    if market == "US":
        if getattr(now, "tzinfo", None):
            return now.astimezone(timezone_for_market("US", reference=now)).replace(tzinfo=None)
        eastern = us_eastern_datetime_from_hkt(now)
        return eastern.replace(tzinfo=None) if eastern is not None else None
    return None

def parse_quote_datetime_for_freshness(value, market, now=None):
    parsed = parse_quote_datetime(value, market=market)
    if parsed is not None:
        return parsed
    market_now = market_local_now(market, now=now)
    if market_now is None or not value:
        return None
    value = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            clock = datetime.strptime(value, fmt)
            return market_now.replace(
                hour=clock.hour,
                minute=clock.minute,
                second=clock.second,
                microsecond=0,
            )
        except ValueError:
            continue
    return None

def quote_freshness(quote, now=None, max_age_seconds=MAX_QUOTE_AGE_SECONDS):
    if not isinstance(quote, dict):
        return False, "quote_not_dict", None
    market = str(quote.get("market") or "").upper()
    market_now = market_local_now(market, now=now)
    quote_dt = parse_quote_datetime_for_freshness(quote.get("time"), market, now=now)
    if market_now is None or quote_dt is None:
        return False, "missing_or_unparseable_quote_time", None
    age_seconds = (market_now - quote_dt).total_seconds()
    if age_seconds > max_age_seconds:
        return False, "stale_quote_time", age_seconds
    if age_seconds < -MAX_QUOTE_FUTURE_SKEW_SECONDS:
        return False, "future_quote_time", age_seconds
    return True, None, age_seconds

def cumulative_volume_ratio(quote_volume, avg_daily_volume, market, quote_time=None):
    """Compare cumulative intraday volume with expected cumulative daily volume."""
    quote_volume = as_float(quote_volume)
    avg_daily_volume = as_float(avg_daily_volume)
    if quote_volume is None or avg_daily_volume is None:
        return None
    if quote_volume <= 0 or avg_daily_volume <= 0:
        return None

    dt = parse_quote_datetime(quote_time, assume_today_for_time_only=True, market=market)
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

def completed_bollinger_bands(closes):
    if not isinstance(closes, list) or len(closes) < 20:
        return None, None
    window = closes[-20:]
    ma20 = sum(window) / 20
    std = (sum((x - ma20)**2 for x in window) / 20) ** 0.5
    return ma20 + 2 * std, ma20 - 2 * std

def completed_moving_average(closes, window):
    return completed_moving_average_at(closes, window, offset=0)

def completed_moving_average_at(closes, window, offset=0):
    if not isinstance(closes, list) or len(closes) < window:
        return None
    offset = as_int(offset, 0) or 0
    if offset < 0 or len(closes) < window + offset:
        return None
    end = len(closes) - offset
    start = end - window
    if start < 0:
        return None
    return sum(closes[start:end]) / window

def completed_ma_cross_state(closes):
    current_ma10 = completed_moving_average_at(closes, 10, offset=0)
    current_ma20 = completed_moving_average_at(closes, 20, offset=0)
    previous_ma10 = completed_moving_average_at(closes, 10, offset=1)
    previous_ma20 = completed_moving_average_at(closes, 20, offset=1)
    if None in (current_ma10, current_ma20, previous_ma10, previous_ma20):
        return None
    return {
        "current_ma10": current_ma10,
        "current_ma20": current_ma20,
        "previous_ma10": previous_ma10,
        "previous_ma20": previous_ma20,
    }

def lookback_close(closes, bars):
    if not isinstance(closes, list) or len(closes) <= bars:
        return None
    return closes[-(bars + 1)]

def quote_change_pct(quote_context):
    if not isinstance(quote_context, dict):
        return None
    return as_float(quote_context.get("change_pct"))

def market_breadth_context_from_quotes(quotes, now=None):
    grouped = defaultdict(list)
    for quote in (quotes or {}).values():
        normalized, _error = normalize_quote(quote)
        if normalized is None:
            continue
        if now is not None:
            fresh, _reason, _age = quote_freshness(normalized, now=now)
            if not fresh:
                continue
        market = str(normalized.get("market") or "").upper()
        change_pct = as_float(normalized.get("change_pct"))
        if market in ("HK", "US") and change_pct is not None:
            grouped[market].append(change_pct)

    result = {}
    for market, changes in grouped.items():
        sample_count = len(changes)
        if sample_count <= 0:
            continue
        advancers = sum(1 for value in changes if value > 0)
        decliners = sum(1 for value in changes if value < 0)
        flat = sample_count - advancers - decliners
        result[market] = {
            "schema": "rt_market_breadth_context_v1",
            "market": market,
            "sample_count": sample_count,
            "advancer_count": advancers,
            "decliner_count": decliners,
            "flat_count": flat,
            "advancer_pct": round(advancers / sample_count * 100.0, 2),
            "decliner_pct": round(decliners / sample_count * 100.0, 2),
            "avg_change_pct": round(sum(changes) / sample_count, 4),
        }
    return result

def market_breadth_model_enabled(model):
    return as_bool((model or {}).get("enabled"), False)

def market_breadth_status(context, model):
    if not market_breadth_model_enabled(model):
        return "disabled"
    if not isinstance(context, dict):
        return "missing"
    sample_count = as_int(context.get("sample_count"), 0) or 0
    min_sample = as_int((model or {}).get("min_sample_size"), MARKET_BREADTH_MIN_SAMPLE) or MARKET_BREADTH_MIN_SAMPLE
    if sample_count < min_sample:
        return "insufficient_sample"
    advancer_pct = as_float(context.get("advancer_pct"))
    decliner_pct = as_float(context.get("decliner_pct"))
    avg_change_pct = as_float(context.get("avg_change_pct"))
    max_advancer = as_float(
        (model or {}).get("risk_off_max_advancer_pct"),
        MARKET_BREADTH_RISK_OFF_MAX_ADVANCER_PCT,
    )
    min_decliner = as_float(
        (model or {}).get("risk_off_min_decliner_pct"),
        MARKET_BREADTH_RISK_OFF_MIN_DECLINER_PCT,
    )
    max_avg_change = as_float(
        (model or {}).get("risk_off_max_avg_change_pct"),
        MARKET_BREADTH_RISK_OFF_MAX_AVG_CHANGE_PCT,
    )
    if advancer_pct is None or decliner_pct is None or avg_change_pct is None:
        return "missing_metrics"
    weak_breadth = advancer_pct <= max_advancer and decliner_pct >= min_decliner
    broad_selloff = avg_change_pct <= max_avg_change and decliner_pct >= min_decliner
    if weak_breadth or broad_selloff:
        return "risk_off"
    return "neutral"

def market_breadth_blocks_new_buy(signal_type, context, model):
    if str(signal_type or "").upper() != "BUY":
        return False, market_breadth_status(context, model)
    if not as_bool((model or {}).get("block_new_buy_in_risk_off"), True):
        return False, market_breadth_status(context, model)
    status = market_breadth_status(context, model)
    return status == "risk_off", status

def trigger_execution_priority(signal_type, trigger_name):
    priorities = TRIGGER_EXECUTION_PRIORITY.get(str(signal_type or "").upper()) or {}
    return int(priorities.get(str(trigger_name or ""), 0))

def quote_momentum_breakout_model(quote_context):
    if not isinstance(quote_context, dict):
        return {}
    model = quote_context.get("momentum_breakout_model")
    return model if isinstance(model, dict) else {}

def momentum_breakout_enabled(quote_context):
    return as_bool(quote_momentum_breakout_model(quote_context).get("enabled"), True)

def same_session_momentum_threshold(quote_context):
    model = quote_momentum_breakout_model(quote_context)
    return (
        as_float(model.get("same_session_momentum_pct"), SESSION_MOMENTUM_THRESHOLD_PCT)
        or SESSION_MOMENTUM_THRESHOLD_PCT
    )

def same_session_momentum_score_delta(quote_context):
    model = quote_momentum_breakout_model(quote_context)
    return (
        as_float(model.get("same_session_score_delta"), SESSION_MOMENTUM_SCORE_DELTA)
        or SESSION_MOMENTUM_SCORE_DELTA
    )

def bollinger_buy_min_change_pct(quote_context):
    model = quote_momentum_breakout_model(quote_context)
    return (
        as_float(model.get("bollinger_buy_min_change_pct"), BOLLINGER_BREAKOUT_BUY_MIN_CHANGE_PCT)
        or BOLLINGER_BREAKOUT_BUY_MIN_CHANGE_PCT
    )

def bollinger_buy_min_score(quote_context):
    model = quote_momentum_breakout_model(quote_context)
    return (
        as_float(model.get("bollinger_buy_min_score"), BOLLINGER_BREAKOUT_BUY_MIN_SCORE)
        or BOLLINGER_BREAKOUT_BUY_MIN_SCORE
    )

def bollinger_buy_min_supporting_factors(quote_context):
    model = quote_momentum_breakout_model(quote_context)
    return (
        as_int(
            model.get("bollinger_buy_min_supporting_factors"),
            BOLLINGER_BREAKOUT_BUY_MIN_SUPPORTING_FACTORS,
        )
        or BOLLINGER_BREAKOUT_BUY_MIN_SUPPORTING_FACTORS
    )

def strong_upper_band_buy_context(
    quote_context,
    provisional_score,
    buy_categories,
    five_day_momentum_pct=None,
):
    if not momentum_breakout_enabled(quote_context):
        return False
    buy_category_count = len(set(buy_categories or []))
    change_pct = quote_change_pct(quote_context)
    if (
        change_pct is not None
        and change_pct >= bollinger_buy_min_change_pct(quote_context)
        and buy_category_count >= 1
    ):
        return True
    if (
        five_day_momentum_pct is not None
        and five_day_momentum_pct >= MOMENTUM_THRESHOLD_PCT
        and buy_category_count >= 1
    ):
        return True
    if (
        provisional_score is not None
        and provisional_score >= bollinger_buy_min_score(quote_context)
        and buy_category_count >= bollinger_buy_min_supporting_factors(quote_context)
    ):
        return True
    return False

def signal_bollinger_bands(indicators):
    if getattr(indicators, "rt_close", None) is not None:
        upper, lower = completed_bollinger_bands(getattr(indicators, "closes", []))
        if upper is not None and lower is not None:
            return upper, lower
    return getattr(indicators, "bb_upper", None), getattr(indicators, "bb_lower", None)

def signal_moving_averages(indicators):
    if getattr(indicators, "rt_close", None) is not None:
        closes = getattr(indicators, "closes", [])
        ma5 = completed_moving_average(closes, 5)
        ma10 = completed_moving_average(closes, 10)
        ma20 = completed_moving_average(closes, 20)
        if ma5 is not None and ma10 is not None and ma20 is not None:
            return ma5, ma10, ma20
    return (
        getattr(indicators, "ma5", None),
        getattr(indicators, "ma10", None),
        getattr(indicators, "ma20", None),
    )

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
        self.history_market = infer_market_from_symbol(symbol)
        self.history_cutoff_date = None
        self.latest_daily_date = None
        self.history_policy = "completed_daily_before_market_date"
        self.loaded = False

    def load_history(self, days=100, market=None, now=None):
        """從DB載入歷史K線"""
        query_symbol = str(self.symbol or "").upper()
        if not valid_watchlist_symbol(query_symbol):
            self.loaded = False
            return False
        self.history_market = str(market or infer_market_from_symbol(query_symbol)).upper()
        market_now = market_local_now(self.history_market, now=now)
        self.history_cutoff_date = market_now.date().isoformat() if market_now is not None else None
        days = as_int(days, 100)
        if days is None or days <= 0:
            days = 100
        cutoff_clause = (
            f"AND timestamp::date < DATE '{self.history_cutoff_date}'"
            if self.history_cutoff_date
            else ""
        )
        raw = db(
            f"""
            WITH daily_bar AS (
                SELECT DISTINCT ON (timestamp::date)
                       timestamp::date AS trade_date,
                       close_price, high_price, low_price, volume
                FROM klines
                WHERE symbol='{query_symbol}' AND interval='day'
                  {cutoff_clause}
                ORDER BY timestamp::date, timestamp DESC
            )
            SELECT trade_date, close_price, high_price, low_price, volume
            FROM daily_bar
            ORDER BY trade_date DESC LIMIT {days}
            """
        )
        rows = []
        for line in raw.split("\n"):
            if not line.strip(): continue
            p = line.split("|")
            if len(p) >= 5:
                trade_date = str(p[0] or "")[:10]
                row = normalize_daily_bar(p[1], p[2], p[3], p[4])
            elif len(p) >= 4:
                trade_date = None
                row = normalize_daily_bar(p[0], p[1], p[2], p[3])
            else:
                row = None
                trade_date = None
            if row is not None:
                rows.append((trade_date, row))
        rows.reverse()
        latest_daily_date = None
        for trade_date, (c, h, l, v) in rows:
            self._update(c, h, l, v)
            if trade_date:
                latest_daily_date = trade_date
        self.latest_daily_date = latest_daily_date
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
            quote_for_volume = dict(quote_context)
            quote_for_volume["volume"] = self.rt_volume or 0
            quote_volume = quote_volume_as_shares(quote_for_volume)
            if quote_volume is None:
                return None
            return cumulative_volume_ratio(
                quote_volume,
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

    def get_score_evidence(self, quote_context=None):
        """計算多因子分數和機器可用的因子貢獻。"""
        closes, highs, lows, volumes = self._series()
        if not closes or len(closes) < MIN_SIGNAL_HISTORY_BARS:
            return score_result(None, [], [])

        c = closes[-1]
        score = 0
        reasons = []
        contributions = []

        def add(delta, category, direction, reason):
            nonlocal score
            score += delta
            reasons.append(reason)
            contribution = score_contribution(category, direction, delta, reason)
            if contribution:
                contributions.append(contribution)

        # 趨勢
        ma5, ma10, ma20 = signal_moving_averages(self)
        if ma5 and ma10 and ma20:
            if c > ma5 > ma10 > ma20:
                add(0.8, "trend", "BUY", "多頭排列")
            elif c > ma5 and c > ma10:
                add(0.4, "trend", "BUY", "短均線偏強")
            elif c < ma5 < ma10 < ma20:
                add(-0.8, "trend", "SELL", "空頭排列")
            elif c < ma5 and c < ma10:
                add(-0.4, "trend", "SELL", "短均線偏弱")

        # RSI
        if self.rsi_14 is not None:
            if self.rsi_14 > 70:
                add(-0.3, "rsi", "SELL", f"RSI偏高({self.rsi_14:.0f})")
            elif self.rsi_14 > 55:
                add(0.3, "rsi", "BUY", f"RSI偏強({self.rsi_14:.0f})")
            elif self.rsi_14 < 30:
                add(0.3, "rsi", "BUY", f"RSI超賣({self.rsi_14:.0f})")
            elif self.rsi_14 < 45:
                add(-0.2, "rsi", "SELL", f"RSI偏弱({self.rsi_14:.0f})")

        # MACD
        if self.macd_hist is not None and self.macd_dif is not None:
            if self.macd_hist > 0 and self.macd_dif > 0:
                add(0.3, "macd", "BUY", "MACD金叉+正值")
            elif self.macd_hist > 0:
                add(0.1, "macd", "BUY", "MACD柱轉正")
            elif self.macd_hist < 0 and self.macd_dif < 0:
                add(-0.3, "macd", "SELL", "MACD死叉+負值")
            elif self.macd_hist < 0:
                add(-0.1, "macd", "SELL", "MACD柱轉負")

        # 布林帶
        bb_upper, bb_lower = signal_bollinger_bands(self)
        if bb_upper and bb_lower:
            if c <= bb_lower * 1.02:
                add(0.3, "bollinger", "BUY", "觸及布林下軌")
            elif c >= bb_upper * 0.98:
                buy_categories = contribution_categories("BUY", contributions)
                five_day_momentum_pct = None
                base_close = lookback_close(closes, 5)
                if base_close is not None and base_close > 0:
                    five_day_momentum_pct = (c / base_close - 1) * 100
                if strong_upper_band_buy_context(
                    quote_context,
                    score,
                    buy_categories,
                    five_day_momentum_pct=five_day_momentum_pct,
                ):
                    add(0.2, "bollinger", "BUY", "布林上軌動量突破")
                else:
                    add(-0.2, "bollinger", "SELL", "觸及布林上軌")

        # 成交量
        vr = self.score_volume_ratio(volumes, quote_context=quote_context)
        if vr is not None:
            prior_close = closes[-2] if len(closes) >= 2 else None
            if vr > 2.0 and prior_close is not None and c > prior_close:
                add(0.2, "volume", "BUY", f"放量上漲{vr:.1f}倍")
            elif vr > 2.0 and prior_close is not None and c < prior_close:
                add(-0.2, "volume", "SELL", f"放量下跌{vr:.1f}倍")
            elif vr > 1.5 and prior_close is not None and c > prior_close:
                add(0.1, "volume", "BUY", f"溫和放量上漲{vr:.1f}倍")

        # 當日/日內動量反轉。這是獨立於長線技術分的短週期確認，不污染日線歷史。
        change_pct = quote_change_pct(quote_context)
        if (
            momentum_breakout_enabled(quote_context)
            and change_pct is not None
            and change_pct >= same_session_momentum_threshold(quote_context)
        ):
            add(
                same_session_momentum_score_delta(quote_context),
                "same_session_momentum",
                "BUY",
                f"當日動量{change_pct:+.1f}%",
            )

        # 動量
        base_close = lookback_close(closes, 5)
        if base_close is not None and base_close > 0:
            mom = (c / base_close - 1) * 100
            if abs(mom) > MOMENTUM_THRESHOLD_PCT + 1e-9:
                add(0.2 if mom > 0 else -0.2, "momentum", "BUY" if mom > 0 else "SELL", f"5日動量{mom:+.1f}%")

        return score_result(max(-1, min(1, score)), reasons, contributions)

    def get_score(self, quote_context=None):
        evidence = self.get_score_evidence(quote_context)
        return evidence["score"], evidence["reasons"]


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

def average_daily_turnover(closes, volumes, lookback=20):
    if not isinstance(closes, list) or not isinstance(volumes, list):
        return None
    if len(closes) < lookback or len(volumes) < lookback:
        return None
    notional = 0.0
    for close, volume in zip(closes[-lookback:], volumes[-lookback:]):
        close = as_float(close)
        volume = as_float(volume)
        if close is None or volume is None or close <= 0 or volume < 0:
            return None
        notional += close * volume
    return notional / lookback

def contribution_categories(signal_type, contributions):
    signal_type = str(signal_type or "").upper()
    categories = set()
    for contribution in normalize_score_contributions(contributions):
        if contribution.get("direction") == signal_type:
            category = str(contribution.get("category") or "").strip().lower()
            if category:
                categories.add(category)
    return sorted(categories)

def legacy_reason_factor_categories(signal_type, reasons):
    signal_type = str(signal_type or "").upper()
    categories = set()
    for reason in reasons or []:
        text = str(reason or "")
        if signal_type == "BUY":
            if text.startswith(("多頭排列", "短均線偏強")):
                categories.add("trend")
            elif text.startswith(("RSI偏強", "RSI超賣")):
                categories.add("rsi")
            elif text.startswith(("MACD金叉", "MACD柱轉正")):
                categories.add("macd")
            elif text.startswith(("觸及布林下軌", "布林上軌動量突破")):
                categories.add("bollinger")
            elif text.startswith(("放量上漲", "溫和放量上漲")):
                categories.add("volume")
            elif text.startswith(("5日動量+", "當日動量+")):
                categories.add("momentum")
        elif signal_type == "SELL":
            if text.startswith(("空頭排列", "短均線偏弱")):
                categories.add("trend")
            elif text.startswith(("RSI偏高", "RSI偏弱")):
                categories.add("rsi")
            elif text.startswith(("MACD死叉", "MACD柱轉負")):
                categories.add("macd")
            elif text.startswith("觸及布林上軌"):
                categories.add("bollinger")
            elif text.startswith("放量下跌"):
                categories.add("volume")
            elif text.startswith("5日動量-"):
                categories.add("momentum")
    return sorted(categories)

def supporting_factor_categories(signal_type, contributions=None, reasons=None):
    categories = contribution_categories(signal_type, contributions)
    if categories:
        return categories
    return legacy_reason_factor_categories(signal_type, reasons)

def alert_signal_date(quote_time=None, generated_at=None, market=None):
    parsed_quote_time = parse_quote_datetime(quote_time, market=market)
    if parsed_quote_time is not None:
        return parsed_quote_time.strftime("%Y%m%d")
    generated_at = generated_at or datetime.now()
    return generated_at.strftime("%Y%m%d")


def parse_generated_at_epoch(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.timestamp()
    except Exception:
        return None


def signal_date_from_alert(alert):
    if not isinstance(alert, dict):
        return None
    signal_id = str(alert.get("signal_id") or "")
    prefix = signal_id.split(":", 1)[0]
    if re.match(r"^\d{8}$", prefix):
        return prefix
    generated_at = None
    try:
        generated_at = datetime.fromisoformat(str(alert.get("generated_at")).replace("Z", "+00:00"))
    except Exception:
        generated_at = None
    return alert_signal_date(
        alert.get("quote_time") or alert.get("time"),
        generated_at=generated_at,
        market=alert.get("market"),
    )


def alert_session_key_from_record(alert):
    if not isinstance(alert, dict):
        return None
    symbol = str(alert.get("symbol") or "").strip().upper()
    signal_type = str(alert.get("signal_type") or "").strip().upper()
    trigger_name = str(alert.get("trigger") or "").strip()
    signal_date = signal_date_from_alert(alert)
    if not (symbol and signal_type and trigger_name and signal_date):
        return None
    return f"{signal_date}:{symbol}:{signal_type}:{trigger_name}"


# ========== 條件觸發器 ==========
class TriggerEngine:
    """條件觸發器 — 只有滿足條件先觸發完整分析"""
    def __init__(self, watchlist_context=None, strategy_config=None, strategy_context=None):
        self.alerts = []
        self.cooldowns = {}  # key -> last_trigger_time
        self.emitted_session_keys = {}  # date:symbol:side:trigger -> first_emit_time
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

    def trigger_disabled_observation(self, signal_type, trigger_name):
        override = self.trigger_override(signal_type, trigger_name)
        return (
            override.get("enabled", True) is False
            and self.trigger_review_mode(signal_type, trigger_name).startswith("disabled_pending_rework")
        )

    def trigger_cooldown_seconds(self, signal_type, trigger_name):
        override = self.trigger_override(signal_type, trigger_name)
        cooldown = as_int(override.get("cooldown_seconds"), self.strategy_config.get("signal_cooldown_seconds"))
        return cooldown if cooldown and cooldown > 0 else SIGNAL_COOLDOWN

    def alert_cooldown_key(self, symbol, signal_type, trigger_name):
        return f"{str(symbol or '').upper()}:{self.trigger_key(signal_type, trigger_name)}"

    def alert_session_key(self, symbol, signal_type, trigger_name, signal_date):
        return f"{signal_date}:{str(symbol or '').upper()}:{self.trigger_key(signal_type, trigger_name)}"

    def alert_signal_id(self, symbol, trigger_name, signal_type, now, cooldown_seconds, signal_date=None):
        bucket_seconds = cooldown_seconds if cooldown_seconds and cooldown_seconds > 0 else SIGNAL_COOLDOWN
        date_prefix = signal_date or datetime.now().strftime("%Y%m%d")
        return (
            f"{date_prefix}:{symbol}:{trigger_name}:{signal_type}:"
            f"{int(now // bucket_seconds)}"
        )

    def volume_anomaly_ratio(self):
        return as_float(self.strategy_config.get("volume_anomaly_ratio"), VOLUME_ANOMALY_RATIO) or VOLUME_ANOMALY_RATIO

    def momentum_breakout_model(self):
        model = self.strategy_config.get("momentum_breakout_model")
        return model if isinstance(model, dict) else {}

    def momentum_breakout_enabled(self):
        return as_bool(self.momentum_breakout_model().get("enabled"), True)

    def large_move_buy_pct(self):
        return (
            as_float(self.momentum_breakout_model().get("large_move_buy_pct"), MOMENTUM_THRESHOLD_PCT)
            or MOMENTUM_THRESHOLD_PCT
        )

    def large_move_sell_enabled(self):
        return as_bool(self.momentum_breakout_model().get("large_move_sell_enabled"), False)

    def quote_scoring_context(self, quote):
        context = dict(quote or {})
        context["momentum_breakout_model"] = self.momentum_breakout_model()
        return context

    def market_breadth_model(self):
        model = self.strategy_config.get("market_breadth_model")
        return model if isinstance(model, dict) else {}

    def market_breadth_decision(self, signal_type, quote):
        context = quote.get("market_breadth") if isinstance(quote, dict) else None
        return market_breadth_blocks_new_buy(signal_type, context, self.market_breadth_model())

    def buy_realtime_alignment_min_change_pct(self):
        model = self.strategy_config.get("realtime_alignment") or {}
        return as_float(
            model.get("block_buy_when_change_pct_below"),
            BUY_REALTIME_ALIGNMENT_MIN_CHANGE_PCT,
        )

    def risk_multiple(self, key, default):
        return as_float((self.strategy_config.get("risk_model") or {}).get(key), default) or default

    def min_rr_ratio(self):
        return as_float((self.strategy_config.get("risk_model") or {}).get("min_rr_ratio"), 1.2) or 1.2

    def min_supporting_factor_count(self, signal_type):
        signal_type = str(signal_type or "").upper()
        default = MIN_SUPPORTING_FACTOR_COUNT.get(signal_type)
        if default is None:
            return None
        requirements = self.strategy_config.get("confirmation_requirements") or {}
        side_req = requirements.get(signal_type) if isinstance(requirements.get(signal_type), dict) else {}
        return as_int(side_req.get("min_supporting_factor_count"), default) or default

    def min_avg_daily_turnover(self, market):
        liquidity = self.strategy_config.get("liquidity_model") or {}
        thresholds = (
            liquidity.get("min_avg_daily_turnover")
            if isinstance(liquidity.get("min_avg_daily_turnover"), dict)
            else {}
        )
        market = str(market or "").upper()
        default = MIN_AVG_DAILY_TURNOVER.get(market)
        if default is None:
            return None
        return as_float(thresholds.get(market), default) or default

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
            threshold = as_float(
                override.get("min_full_score"),
                as_float((thresholds.get("BUY") or {}).get("min_full_score"), BUY_CONFIRMATION_MIN_SCORE),
            )
            return full_score >= threshold
        threshold = as_float(
            override.get("max_full_score"),
            as_float((thresholds.get("SELL") or {}).get("max_full_score"), SELL_CONFIRMATION_MAX_SCORE),
        )
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
        symbol = str(symbol or "").strip().upper()
        if not valid_watchlist_symbol(symbol, market=quote.get("market")):
            return
        if not indicator_signal_ready(indicators):
            return

        c = quote["price"]

        now = time.time()
        triggered = []
        score_getter = getattr(indicators, "get_score_evidence", None)
        scoring_context = self.quote_scoring_context(quote)
        raw_score_result = score_getter(scoring_context) if callable(score_getter) else indicators.get_score(scoring_context)
        full_score, full_reasons, factor_contributions = unpack_score_result(raw_score_result)
        full_score = as_float(full_score)
        full_reasons = full_reasons if isinstance(full_reasons, list) else []

        # 1. RSI 極端值
        if indicators.rsi_14 is not None:
            if indicators.rsi_14 <= 30:
                triggered.append(("RSI超賣", f"RSI={indicators.rsi_14:.0f}", "BUY"))
            elif indicators.rsi_14 >= 70:
                triggered.append(("RSI超買", f"RSI={indicators.rsi_14:.0f}", "SELL"))

        # 2. 布林帶突破
        bb_upper, bb_lower = signal_bollinger_bands(indicators)
        if bb_upper and bb_lower:
            if c <= bb_lower:
                triggered.append(("布林下軌突破", f"價格${c} < 下軌${bb_lower:.2f}", "BUY"))
            elif c >= bb_upper:
                buy_categories = supporting_factor_categories(
                    "BUY",
                    factor_contributions,
                    reasons=full_reasons,
                )
                if strong_upper_band_buy_context(
                    scoring_context,
                    full_score,
                    buy_categories,
                ):
                    triggered.append(("布林上軌動量突破", f"價格${c} > 上軌${bb_upper:.2f}", "BUY"))
                else:
                    triggered.append(("布林上軌突破", f"價格${c} > 上軌${bb_upper:.2f}", "SELL"))

        # 3. 均線金叉/死叉
        if indicators.ma5 and indicators.ma10 and len(indicators.closes) >= 5:
            prev_c = indicators.closes[-1]
            prev_ma5 = completed_moving_average(indicators.closes, 5)
            if prev_ma5 is not None and c > prev_ma5 and prev_c <= prev_ma5:
                triggered.append(("站上MA5", f"${c} > MA5=${prev_ma5:.2f}", "BUY"))
            if prev_ma5 is not None and c < prev_ma5 and prev_c >= prev_ma5:
                triggered.append(("跌破MA5", f"${c} < MA5=${prev_ma5:.2f}", "SELL"))

        ma_cross = completed_ma_cross_state(getattr(indicators, "closes", []))
        if ma_cross:
            current_ma10 = ma_cross["current_ma10"]
            current_ma20 = ma_cross["current_ma20"]
            previous_ma10 = ma_cross["previous_ma10"]
            previous_ma20 = ma_cross["previous_ma20"]
            if current_ma10 > current_ma20 and previous_ma10 <= previous_ma20:
                triggered.append(("MA金叉", f"MA10={current_ma10:.2f} > MA20={current_ma20:.2f}", "BUY"))
            if current_ma10 < current_ma20 and previous_ma10 >= previous_ma20:
                triggered.append(("MA死叉", f"MA10={current_ma10:.2f} < MA20={current_ma20:.2f}", "SELL"))

        # 4. 成交量異動
        if len(indicators.volumes) >= 20:
            avg_vol = sum(indicators.volumes[-20:]) / 20
            quote_volume = quote_volume_as_shares(quote)
            if avg_vol > 0 and quote_volume is not None and quote_volume > 0:
                vol_ratio = cumulative_volume_ratio(
                    quote_volume,
                    avg_vol,
                    quote.get("market"),
                    quote.get("time"),
                )
                if vol_ratio is not None and vol_ratio > self.volume_anomaly_ratio():
                    triggered.append(("成交量異動", f"量比={vol_ratio:.1f}", "WATCH"))

        # 5. 大幅波動
        change_pct = as_float(quote.get("change_pct"), 0) or 0
        if (
            self.momentum_breakout_enabled()
            and change_pct >= self.large_move_buy_pct()
        ):
            triggered.append(("急漲", f"{change_pct:+.1f}%", "BUY"))
        elif (
            change_pct <= -MOMENTUM_THRESHOLD_PCT
            and self.large_move_sell_enabled()
        ):
            triggered.append(("急跌", f"{change_pct:+.1f}%", "SELL"))
        elif abs(change_pct) >= MOMENTUM_THRESHOLD_PCT:
            direction = "急漲" if change_pct > 0 else "急跌"
            triggered.append((direction, f"{change_pct:+.1f}%", "WATCH"))

        # 冷卻期檢查 + 觸發
        candidate_rows = []
        for trigger_name, detail, signal_type in triggered:
            trigger_review_mode = self.trigger_review_mode(signal_type, trigger_name)
            trigger_disabled_observation = self.trigger_disabled_observation(signal_type, trigger_name)
            if not self.trigger_enabled(signal_type, trigger_name) and not trigger_disabled_observation:
                continue
            cooldown_seconds = self.trigger_cooldown_seconds(signal_type, trigger_name)
            
            # 計算入場/止盈/止損 (基於ATR)
            atr = as_float(indicators.atr_14)
            atr_valid = atr is not None and atr > 0
            stop_multiple = self.risk_multiple("atr_stop_multiple", 2.0)
            take_profit_multiple = self.risk_multiple("atr_take_profit_multiple", 3.0)
            
            confirmed = (
                self.is_confirmed(signal_type, trigger_name, full_score)
                if signal_type in ("BUY", "SELL")
                else False
            )
            factor_confluence_categories = supporting_factor_categories(
                signal_type,
                factor_contributions,
                reasons=full_reasons,
            )
            min_factor_count = self.min_supporting_factor_count(signal_type)
            if signal_type in ("BUY", "SELL"):
                factor_confluence_valid = (
                    min_factor_count is not None
                    and len(factor_confluence_categories) >= min_factor_count
                )
                factor_confluence_reason = None if factor_confluence_valid else "supporting_factor_count_below_minimum"
            else:
                factor_confluence_valid = False
                factor_confluence_reason = "not_directional_candidate"
            candidate_entry_price = self.round_risk_price(c)
            if signal_type == "BUY" and atr_valid:
                candidate_stop_loss = self.round_risk_price(c - stop_multiple * atr, reference_price=c)
                candidate_take_profit = self.round_risk_price(c + take_profit_multiple * atr, reference_price=c)
            elif signal_type == "SELL" and atr_valid:
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
            avg_daily_turnover = average_daily_turnover(indicators.closes, indicators.volumes)
            min_avg_daily_turnover = self.min_avg_daily_turnover(quote.get("market"))
            risk_geometry_valid, risk_geometry_reason = self.risk_geometry(
                signal_type,
                candidate_entry_price,
                candidate_stop_loss,
                candidate_take_profit,
            )
            if signal_type not in ("BUY", "SELL"):
                risk_geometry_valid = False
                risk_geometry_reason = "not_directional_candidate"
            elif not atr_valid:
                risk_geometry_valid = False
                risk_geometry_reason = "missing_or_invalid_atr"
            liquidity_geometry_valid = True
            liquidity_geometry_reason = None
            if signal_type in ("BUY", "SELL"):
                if avg_daily_turnover is None or min_avg_daily_turnover is None:
                    liquidity_geometry_valid = False
                    liquidity_geometry_reason = "missing_or_invalid_avg_daily_turnover"
                elif avg_daily_turnover <= 0 or min_avg_daily_turnover <= 0:
                    liquidity_geometry_valid = False
                    liquidity_geometry_reason = "non_positive_avg_daily_turnover"
                elif avg_daily_turnover < min_avg_daily_turnover:
                    liquidity_geometry_valid = False
                    liquidity_geometry_reason = "avg_daily_turnover_below_minimum"
                if not liquidity_geometry_valid:
                    risk_geometry_valid = False
                    risk_geometry_reason = liquidity_geometry_reason
            else:
                liquidity_geometry_valid = False
                liquidity_geometry_reason = "not_directional_candidate"
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
            trigger_shadow_only = self.trigger_shadow_only(signal_type, trigger_name)
            if signal_type in ("BUY", "SELL") and trigger_disabled_observation:
                emitted_signal_type = "WATCH"
                suppressed_directional_reason = "strategy_review_disabled_pending_rework"
            elif signal_type in ("BUY", "SELL") and trigger_shadow_only:
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
            if signal_type in ("BUY", "SELL") and confirmed and risk_geometry_valid and not factor_confluence_valid:
                emitted_signal_type = "WATCH"
                suppressed_directional_reason = factor_confluence_reason
            market_breadth_blocked, market_breadth_signal_status = self.market_breadth_decision(signal_type, quote)
            if (
                signal_type == "BUY"
                and emitted_signal_type in ("BUY", "SELL")
                and market_breadth_blocked
            ):
                emitted_signal_type = "WATCH"
                suppressed_directional_reason = "market_breadth_risk_off"
            buy_realtime_alignment_min_change_pct = self.buy_realtime_alignment_min_change_pct()
            buy_realtime_alignment_blocked = (
                signal_type == "BUY"
                and change_pct < buy_realtime_alignment_min_change_pct
            )
            if (
                signal_type == "BUY"
                and emitted_signal_type in ("BUY", "SELL")
                and buy_realtime_alignment_blocked
            ):
                emitted_signal_type = "WATCH"
                suppressed_directional_reason = "buy_realtime_direction_misaligned"

            execution_candidate = (
                emitted_signal_type in ("BUY", "SELL")
                and confirmed
                and risk_geometry_valid
                and factor_confluence_valid
                and not market_breadth_blocked
                and not buy_realtime_alignment_blocked
            )
            execution_blocked_reasons = []
            if signal_type not in ("BUY", "SELL"):
                execution_blocked_reasons.append("not_directional_candidate")
            else:
                if not confirmed:
                    execution_blocked_reasons.append("not_confirmed")
                if trigger_disabled_observation:
                    execution_blocked_reasons.append("strategy_review_disabled_pending_rework")
                if trigger_shadow_only:
                    execution_blocked_reasons.append("strategy_review_shadow_only")
                if not risk_geometry_valid:
                    execution_blocked_reasons.append(f"risk_geometry_invalid:{risk_geometry_reason}")
                if confirmed and not factor_confluence_valid:
                    execution_blocked_reasons.append(f"factor_confluence_invalid:{factor_confluence_reason}")
                if signal_type == "BUY" and market_breadth_blocked:
                    execution_blocked_reasons.append("market_breadth_risk_off")
                if signal_type == "BUY" and buy_realtime_alignment_blocked:
                    execution_blocked_reasons.append("buy_realtime_direction_misaligned")

            market = quote.get("market", "")
            generated_at = datetime.now()
            signal_date = alert_signal_date(quote.get("time"), generated_at=generated_at, market=market)
            directional_session_key = self.alert_session_key(symbol, emitted_signal_type, trigger_name, signal_date)
            directional_cooldown_key = self.alert_cooldown_key(symbol, emitted_signal_type, trigger_name)
            directional_on_cooldown = (
                directional_cooldown_key in self.cooldowns
                and now - self.cooldowns[directional_cooldown_key] < cooldown_seconds
            )
            directional_emission_allowed = (
                directional_session_key not in self.emitted_session_keys
                and not directional_on_cooldown
            )
            directional_score = abs(full_score) if signal_type in ("BUY", "SELL") and full_score is not None else 0.0
            rr_rank = candidate_rr_ratio if candidate_rr_ratio is not None else 0.0
            candidate_rows.append(
                {
                    "execution_quality_rank": (
                        1 if execution_candidate and directional_emission_allowed else 0,
                        trigger_execution_priority(signal_type, trigger_name),
                        len(factor_confluence_categories),
                        directional_score,
                        rr_rank,
                    ),
                    "trigger_name": trigger_name,
                    "detail": detail,
                    "signal_type": signal_type,
                    "trigger_review_mode": trigger_review_mode,
                    "trigger_disabled_observation": trigger_disabled_observation,
                    "trigger_shadow_only": trigger_shadow_only,
                    "cooldown_seconds": cooldown_seconds,
                    "atr": atr,
                    "confirmed": confirmed,
                    "risk_geometry_valid": risk_geometry_valid,
                    "risk_geometry_reason": risk_geometry_reason,
                    "liquidity_geometry_valid": liquidity_geometry_valid,
                    "liquidity_geometry_reason": liquidity_geometry_reason,
                    "factor_confluence_valid": factor_confluence_valid,
                    "factor_confluence_reason": factor_confluence_reason,
                    "factor_confluence_categories": factor_confluence_categories,
                    "min_factor_count": min_factor_count,
                    "market_breadth_blocked": market_breadth_blocked,
                    "market_breadth_signal_status": market_breadth_signal_status,
                    "buy_realtime_alignment_min_change_pct": buy_realtime_alignment_min_change_pct,
                    "buy_realtime_alignment_blocked": buy_realtime_alignment_blocked,
                    "emitted_signal_type": emitted_signal_type,
                    "suppressed_directional_reason": suppressed_directional_reason,
                    "execution_candidate": execution_candidate,
                    "execution_blocked_reasons": execution_blocked_reasons,
                    "candidate_entry_price": candidate_entry_price,
                    "candidate_stop_loss": candidate_stop_loss,
                    "candidate_take_profit": candidate_take_profit,
                    "candidate_rr_ratio": candidate_rr_ratio,
                    "avg_daily_turnover": avg_daily_turnover,
                    "min_avg_daily_turnover": min_avg_daily_turnover,
                    "min_rr_ratio": min_rr_ratio,
                    "market": market,
                    "generated_at": generated_at,
                    "signal_date": signal_date,
                    "directional_emission_allowed": directional_emission_allowed,
                }
            )

        selected_execution_by_direction = {}
        for row in candidate_rows:
            if (
                row["execution_candidate"]
                and row["directional_emission_allowed"]
                and row["signal_type"] in ("BUY", "SELL")
            ):
                direction = row["signal_type"]
                current = selected_execution_by_direction.get(direction)
                if current is None or row["execution_quality_rank"] > current["execution_quality_rank"]:
                    selected_execution_by_direction[direction] = row

        for row in candidate_rows:
            trigger_name = row["trigger_name"]
            detail = row["detail"]
            signal_type = row["signal_type"]
            trigger_review_mode = row["trigger_review_mode"]
            trigger_disabled_observation = row["trigger_disabled_observation"]
            trigger_shadow_only = row["trigger_shadow_only"]
            cooldown_seconds = row["cooldown_seconds"]
            atr = row["atr"]
            confirmed = row["confirmed"]
            risk_geometry_valid = row["risk_geometry_valid"]
            risk_geometry_reason = row["risk_geometry_reason"]
            liquidity_geometry_valid = row["liquidity_geometry_valid"]
            liquidity_geometry_reason = row["liquidity_geometry_reason"]
            factor_confluence_valid = row["factor_confluence_valid"]
            factor_confluence_reason = row["factor_confluence_reason"]
            factor_confluence_categories = row["factor_confluence_categories"]
            min_factor_count = row["min_factor_count"]
            market_breadth_signal_status = row["market_breadth_signal_status"]
            buy_realtime_alignment_min_change_pct = row["buy_realtime_alignment_min_change_pct"]
            buy_realtime_alignment_blocked = row["buy_realtime_alignment_blocked"]
            emitted_signal_type = row["emitted_signal_type"]
            suppressed_directional_reason = row["suppressed_directional_reason"]
            execution_candidate = row["execution_candidate"]
            execution_blocked_reasons = list(row["execution_blocked_reasons"])
            candidate_entry_price = row["candidate_entry_price"]
            candidate_stop_loss = row["candidate_stop_loss"]
            candidate_take_profit = row["candidate_take_profit"]
            candidate_rr_ratio = row["candidate_rr_ratio"]
            avg_daily_turnover = row["avg_daily_turnover"]
            min_avg_daily_turnover = row["min_avg_daily_turnover"]
            min_rr_ratio = row["min_rr_ratio"]
            market = row["market"]
            generated_at = row["generated_at"]
            signal_date = row["signal_date"]

            selected_execution = selected_execution_by_direction.get(signal_type)
            if execution_candidate and signal_type in ("BUY", "SELL"):
                if not row["directional_emission_allowed"]:
                    continue
                if selected_execution is None:
                    continue
                if selected_execution is not row:
                    emitted_signal_type = "WATCH"
                    suppressed_directional_reason = "same_scan_directional_duplicate"
                    execution_candidate = False
                    execution_blocked_reasons.append("same_scan_directional_duplicate")

            if execution_candidate:
                entry_price = candidate_entry_price
                stop_loss = candidate_stop_loss
                take_profit = candidate_take_profit
                rr_ratio = candidate_rr_ratio
            else:
                entry_price = None
                stop_loss = None
                take_profit = None
                rr_ratio = None

            session_key = self.alert_session_key(symbol, emitted_signal_type, trigger_name, signal_date)
            if session_key in self.emitted_session_keys:
                continue

            key = self.alert_cooldown_key(symbol, emitted_signal_type, trigger_name)
            if key in self.cooldowns and now - self.cooldowns[key] < cooldown_seconds:
                continue
            self.cooldowns[key] = now
            self.emitted_session_keys[session_key] = now

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
                **alert_timeframe_metadata(),
                **alert_daily_history_metadata(indicators),
                "trigger": trigger_name,
                "detail": detail,
                "signal_type": emitted_signal_type,
                "candidate_signal_type": signal_type,
                "trigger_review_mode": trigger_review_mode or None,
                "strategy_policy_shadow_only": trigger_shadow_only,
                "strategy_policy_disabled_observation": trigger_disabled_observation,
                "suppressed_directional_reason": suppressed_directional_reason,
                "execution_candidate": execution_candidate,
                "execution_blocked_reasons": execution_blocked_reasons,
                "confirmed": confirmed,
                "risk_geometry_valid": risk_geometry_valid,
                "risk_geometry_reason": risk_geometry_reason,
                "liquidity_geometry_valid": liquidity_geometry_valid,
                "liquidity_geometry_reason": liquidity_geometry_reason,
                "factor_confluence_valid": factor_confluence_valid,
                "factor_confluence_reason": factor_confluence_reason,
                "factor_confluence_categories": factor_confluence_categories,
                "factor_confluence_supporting_count": len(factor_confluence_categories),
                "factor_confluence_min_count": min_factor_count,
                "market_breadth_status": market_breadth_signal_status,
                "market_breadth": quote.get("market_breadth"),
                "buy_realtime_alignment_min_change_pct": buy_realtime_alignment_min_change_pct,
                "buy_realtime_alignment_blocked": buy_realtime_alignment_blocked,
                "full_score": round(full_score, 3) if full_score is not None else None,
                "full_reasons": full_reasons,
                "factor_contributions": factor_contributions,
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
                "avg_daily_turnover": round(avg_daily_turnover, 2) if avg_daily_turnover is not None else None,
                "min_avg_daily_turnover": min_avg_daily_turnover,
                "min_rr_ratio": min_rr_ratio,
                "atr": round(atr, 3) if atr is not None else None,
            })


# ========== 主循環 ==========
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def default_state():
    return {"cooldowns": {}, "emitted_session_keys": {}, "date": ""}


def sanitize_timestamp_mapping(value):
    cleaned = {}
    if not isinstance(value, dict):
        return cleaned
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip()
        timestamp = as_float(raw_value)
        if key and timestamp is not None and timestamp >= 0:
            cleaned[key] = timestamp
    return cleaned

def prune_emitted_session_keys(mapping, now=None, retention_days=SESSION_KEY_RETENTION_DAYS):
    cleaned = sanitize_timestamp_mapping(mapping)
    try:
        retention_days = int(retention_days)
    except (TypeError, ValueError):
        retention_days = SESSION_KEY_RETENTION_DAYS
    if retention_days <= 0:
        return cleaned
    today = (now or datetime.now()).date()
    cutoff = today - timedelta(days=retention_days)
    pruned = {}
    for key, value in cleaned.items():
        raw_date = key.split(":", 1)[0]
        try:
            key_date = datetime.strptime(raw_date, "%Y%m%d").date()
        except (TypeError, ValueError):
            pruned[key] = value
            continue
        if key_date >= cutoff:
            pruned[key] = value
    return pruned

def normalize_state(payload):
    state = default_state()
    if not isinstance(payload, dict):
        return state

    state["cooldowns"] = sanitize_timestamp_mapping(payload.get("cooldowns"))
    state["emitted_session_keys"] = sanitize_timestamp_mapping(payload.get("emitted_session_keys"))

    date = str(payload.get("date") or "").strip()
    if date:
        state["date"] = date
    return state


def read_recent_alert_queue(path=None, limit=None):
    path = path or ALERT_QUEUE_FILE
    try:
        limit = int(limit if limit is not None else STATE_BACKFILL_ALERT_QUEUE_LIMIT)
    except (TypeError, ValueError):
        limit = STATE_BACKFILL_ALERT_QUEUE_LIMIT
    if limit <= 0:
        return []

    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rows.append(line)
                if len(rows) > limit:
                    rows.pop(0)
    except Exception:
        return []

    alerts = []
    for line in rows:
        try:
            alert = json.loads(line)
        except Exception:
            continue
        if isinstance(alert, dict):
            alerts.append(alert)
    return alerts


def backfill_emitted_session_keys_from_alert_queue(
    existing=None,
    path=None,
    now=None,
    retention_days=SESSION_KEY_RETENTION_DAYS,
):
    merged = sanitize_timestamp_mapping(existing)
    added = 0
    fallback_epoch = time.time()
    for alert in read_recent_alert_queue(path=path):
        key = alert_session_key_from_record(alert)
        if not key or key in merged:
            continue
        merged[key] = parse_generated_at_epoch(alert.get("generated_at")) or fallback_epoch
        added += 1
    return prune_emitted_session_keys(merged, now=now, retention_days=retention_days), added


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
        with open(tmp, "w", encoding="utf-8") as f:
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
        loaded = ind.load_history(100, market=market)
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
    trigger.emitted_session_keys, backfilled_session_key_count = backfill_emitted_session_keys_from_alert_queue(
        state.get("emitted_session_keys", {})
    )
    if backfilled_session_key_count:
        state["emitted_session_keys"] = trigger.emitted_session_keys
        save_state(state)
        log(f"回填同日去重狀態: {backfilled_session_key_count} keys from alert queue")

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
            market_breadth_by_market = market_breadth_context_from_quotes(all_quotes, now=dt)
            for sym, quote in all_quotes.items():
                if sym in indicators:
                    quote, _quote_error = normalize_quote(quote)
                    if quote is None:
                        continue
                    fresh, _freshness_reason, _quote_age_seconds = quote_freshness(quote, now=dt)
                    if not fresh:
                        continue
                    breadth = market_breadth_by_market.get(str(quote.get("market") or "").upper())
                    if breadth:
                        quote["market_breadth"] = breadth
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
                trigger.emitted_session_keys = prune_emitted_session_keys(trigger.emitted_session_keys, now=dt)
                state["emitted_session_keys"] = trigger.emitted_session_keys
                state["date"] = dt.strftime("%Y-%m-%d")
                save_state(state)
            else:
                if cycle % 100 == 0:
                    log(f"掃描完成: {len(all_quotes)}隻報價, 0個觸發")

            last_full_scan = now

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
