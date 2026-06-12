#!/usr/bin/env python3
"""
信號引擎 v4 — 全面多維度分析
技術面 + 消息面 + 大市環境 + 基本面 + 價格預測 + 掛單價
"""
import argparse, subprocess, json, math, urllib.request, time, os, sys
from datetime import datetime, timedelta

TENANT_ID = os.environ.get("QM_TENANT_ID", "default")
USER_ID = os.environ.get("QM_USER_ID", "10000002")
MODEL_VERSION = "signal_v4"
FEATURE_VERSION = "v4_full"
MODEL_NAME = "technical_signal_engine"
DAILY_SIGNAL_READY_TIME = os.environ.get("SIGNAL_V4_DAILY_SIGNAL_READY_TIME", "16:15")
ALLOW_INTRADAY_DAILY_SIGNAL = os.environ.get("SIGNAL_V4_ALLOW_INTRADAY_DAILY", "0") == "1"
DB_ERRORS = []
_COLUMN_CACHE = {}

def db(sql, timeout=30):
    r = subprocess.run(
        ['docker', 'exec', 'quantmind-db', 'psql', '-U', 'quantmind', '-d', 'quantmind', '-t', '-A', '-c', sql],
        capture_output=True, text=True, timeout=timeout
    )
    if r.returncode != 0:
        err = r.stderr.strip()
        DB_ERRORS.append(err)
        print(f"[DB] SQL failed: {err}", flush=True)
    return r.stdout.strip()

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def sql_quote(value):
    return str(value).replace("'", "''")

def jsonb_literal(value):
    return sql_quote(json.dumps(value, ensure_ascii=False, default=str))

def parse_yyyymmdd(value):
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None

def parse_hhmm_minutes(value):
    try:
        hour, minute = str(value).split(":", 1)
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return 16 * 60 + 15

def minutes_since_midnight(value):
    return value.hour * 60 + value.minute

def daily_signal_write_block(trade_date, now=None):
    if ALLOW_INTRADAY_DAILY_SIGNAL:
        return False, ""
    now = now or datetime.now()
    parsed_date = parse_yyyymmdd(trade_date)
    if not parsed_date or parsed_date != now.date():
        return False, ""
    ready_minutes = parse_hhmm_minutes(DAILY_SIGNAL_READY_TIME)
    if minutes_since_midnight(now) < ready_minutes:
        return True, f"current_session_before_daily_signal_ready_time_{DAILY_SIGNAL_READY_TIME}"
    return False, ""

def table_columns(table):
    if table in _COLUMN_CACHE:
        return _COLUMN_CACHE[table]
    raw = db(f"""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = '{sql_quote(table)}'
    """)
    cols = {line.strip() for line in raw.splitlines() if line.strip()}
    _COLUMN_CACHE[table] = cols
    return cols

def first_existing(table, candidates):
    cols = table_columns(table)
    for candidate in candidates:
        if candidate in cols:
            return candidate
    return None

def latest_kline_date():
    raw = db("""
        SELECT max(k.timestamp::date)
        FROM klines k
        JOIN stocks s ON k.symbol = s.symbol
        WHERE k.interval = 'day'
        AND s.is_active = true
        AND s.exchange IN ('HKEX','NASDAQ','NYSE')
    """)
    return raw.strip() if raw else ""

def run_id_for(trade_date):
    return f"signal_v4_{trade_date.replace('-', '')}"

def feature_run_count_columns():
    return {
        "expected": first_existing("engine_feature_runs", ("expected_count", "expected_symbols")),
        "ready": first_existing("engine_feature_runs", ("ready_count", "ready_symbols")),
        "missing": first_existing("engine_feature_runs", ("missing_count", "missing_symbols")),
    }

def ensure_feature_run(run_id, trade_date, expected_count):
    quality = {
        "engine": "signal_engine_v4",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date_source": "max_active_hk_us_kline_date",
    }
    count_cols = feature_run_count_columns()
    expected_col = count_cols["expected"]
    ready_col = count_cols["ready"]
    missing_col = count_cols["missing"]

    columns = [
        "run_id", "tenant_id", "user_id", "trade_date", "model_name", "model_version",
        "feature_version", "feature_dim", "status", "source", "quality",
    ]
    values = [
        f"'{sql_quote(run_id)}'",
        f"'{sql_quote(TENANT_ID)}'",
        f"'{sql_quote(USER_ID)}'",
        f"'{sql_quote(trade_date)}'",
        f"'{MODEL_NAME}'",
        f"'{MODEL_VERSION}'",
        f"'{FEATURE_VERSION}'",
        "52",
        "'feature_ready'",
        "'signal_engine_v4'",
        f"'{jsonb_literal(quality)}'::jsonb",
    ]
    updates = [
        "status = EXCLUDED.status",
        "source = EXCLUDED.source",
        "quality = EXCLUDED.quality",
    ]
    for col, value in (
        (expected_col, expected_count),
        (ready_col, 0),
        (missing_col, expected_count),
    ):
        if col:
            columns.append(col)
            values.append(str(value))
            updates.append(f"{col} = EXCLUDED.{col}")

    db(f"""
        INSERT INTO engine_feature_runs ({', '.join(columns)})
        VALUES ({', '.join(values)})
        ON CONFLICT (run_id) DO UPDATE SET
            {', '.join(updates)}
    """)

def finalize_feature_run(run_id, expected_count, ready_count, side_counts):
    missing_count = max(expected_count - ready_count, 0)
    quality = {
        "engine": "signal_engine_v4",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ready_count": ready_count,
        "missing_count": missing_count,
        "side_counts": side_counts,
    }
    count_cols = feature_run_count_columns()
    ready_col = count_cols["ready"]
    missing_col = count_cols["missing"]
    assignments = [
        "status = 'signal_ready'",
        f"quality = '{jsonb_literal(quality)}'::jsonb",
    ]
    if ready_col:
        assignments.append(f"{ready_col} = {ready_count}")
    if missing_col:
        assignments.append(f"{missing_col} = {missing_count}")
    db(f"""
        UPDATE engine_feature_runs
        SET {', '.join(assignments)}
        WHERE run_id = '{sql_quote(run_id)}'
    """)

def upsert_signal_score(result, trade_date, run_id, quality):
    quality_json = jsonb_literal(quality)
    db(f"""
        INSERT INTO engine_signal_scores (
            run_id, tenant_id, user_id, trade_date, symbol, model_version,
            feature_version, fusion_score, signal_side, quality, expected_price
        )
        VALUES (
            '{sql_quote(run_id)}', '{sql_quote(TENANT_ID)}', '{sql_quote(USER_ID)}',
            '{sql_quote(trade_date)}', '{sql_quote(result['symbol'])}',
            '{MODEL_VERSION}', '{FEATURE_VERSION}', {result['score']},
            '{result['side']}', '{quality_json}'::jsonb, {result['price']}
        )
        ON CONFLICT (
            tenant_id, user_id, trade_date, symbol, model_version, feature_version, run_id
        ) DO UPDATE SET
            fusion_score = EXCLUDED.fusion_score,
            signal_side = EXCLUDED.signal_side,
            quality = EXCLUDED.quality,
            expected_price = EXCLUDED.expected_price
    """)

def candidate_stocks_for_date(trade_date):
    return db(f"""
        SELECT k.symbol, s.exchange FROM klines k
        JOIN stocks s ON k.symbol = s.symbol
        WHERE k.interval = 'day' AND s.is_active = true
        AND s.exchange IN ('HKEX','NASDAQ','NYSE')
        GROUP BY k.symbol, s.exchange
        HAVING count(*) >= 30 AND max(k.timestamp::date) = '{sql_quote(trade_date)}'::date
        ORDER BY k.symbol
    """)

def parse_stock_rows(raw):
    stocks = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) >= 2:
            stocks.append({"symbol": parts[0], "exchange": parts[1]})
    return stocks

def build_preflight_payload():
    DB_ERRORS.clear()
    _COLUMN_CACHE.clear()
    trade_date = latest_kline_date()
    run_id = run_id_for(trade_date) if trade_date else ""
    raw_stocks = candidate_stocks_for_date(trade_date) if trade_date else ""
    stocks = parse_stock_rows(raw_stocks)
    write_blocked, block_reason = daily_signal_write_block(trade_date) if trade_date else (False, "")
    count_cols = feature_run_count_columns()
    signal_cols = table_columns("engine_signal_scores")
    feature_cols = table_columns("engine_feature_runs")
    status = "FAIL" if DB_ERRORS or not trade_date or not stocks else "OK"
    if status == "OK" and write_blocked:
        status = "WARN"
    return {
        "status": status,
        "trade_date": trade_date,
        "run_id": run_id,
        "candidate_count": len(stocks),
        "write_blocked": write_blocked,
        "block_reason": block_reason,
        "daily_signal_ready_time": DAILY_SIGNAL_READY_TIME,
        "sample_symbols": stocks[:10],
        "feature_run_count_columns": count_cols,
        "schema_checks": {
            "engine_feature_runs_has_run_id": "run_id" in feature_cols,
            "engine_feature_runs_has_status": "status" in feature_cols,
            "engine_signal_scores_has_run_id": "run_id" in signal_cols,
            "engine_signal_scores_has_quality": "quality" in signal_cols,
        },
        "db_errors": list(DB_ERRORS),
        "writes_database": False,
    }

# ═══════════════════════════════════════════
# 技術指標計算（v3同款）
# ═══════════════════════════════════════════
def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal: return None, None, None
    def ema(data, period):
        k = 2 / (period + 1)
        result = [data[0]]
        for i in range(1, len(data)):
            result.append(data[i] * k + result[-1] * (1 - k))
        return result
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [ema_fast[i] - ema_slow[i] for i in range(len(closes))]
    signal_line = ema(macd_line, signal)
    histogram = [macd_line[i] - signal_line[i] for i in range(len(closes))]
    return macd_line[-1], signal_line[-1], histogram[-1]

def calc_bollinger(closes, period=20, num_std=2):
    if len(closes) < period: return None, None, None
    window = closes[-period:]
    ma = sum(window) / period
    std = math.sqrt(sum((x - ma) ** 2 for x in window) / period)
    return ma + num_std * std, ma, ma - num_std * std

def calc_ma(closes, period):
    if len(closes) < period: return None
    return sum(closes[-period:]) / period

def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1: return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    return sum(trs[-period:]) / period if len(trs) >= period else None

# ═══════════════════════════════════════════
# 支撐阻力計算
# ═══════════════════════════════════════════
def calc_support_resistance(closes, highs, lows, current):
    """用近期高低點+樞軸點計算支撐阻力"""
    supports = []
    resistances = []
    
    # 近20日高低點
    if len(closes) >= 20:
        recent_high = max(highs[-20:])
        recent_low = min(lows[-20:])
        supports.append(recent_low)
        resistances.append(recent_high)
    
    # Pivot Points (Classic)
    if len(highs) >= 1 and len(lows) >= 1 and len(closes) >= 1:
        h, l, c = highs[-1], lows[-1], closes[-1]
        pivot = (h + l + c) / 3
        s1 = 2 * pivot - h
        r1 = 2 * pivot - l
        s2 = pivot - (h - l)
        r2 = pivot + (h - l)
        supports.extend([s1, s2])
        resistances.extend([r1, r2])
    
    # MA levels as support/resistance
    for period in [5, 10, 20]:
        ma = calc_ma(closes, period)
        if ma:
            if ma < current:
                supports.append(ma)
            else:
                resistances.append(ma)
    
    # BB levels
    upper, mid, lower = calc_bollinger(closes)
    if upper and lower:
        supports.append(lower)
        resistances.append(upper)
    
    # Filter and sort
    supports = sorted([s for s in supports if s < current and s > 0], reverse=True)
    resistances = sorted([r for r in resistances if r > current])
    
    return {
        'support_1': round(supports[0], 3) if len(supports) > 0 else None,
        'support_2': round(supports[1], 3) if len(supports) > 1 else None,
        'resistance_1': round(resistances[0], 3) if len(resistances) > 0 else None,
        'resistance_2': round(resistances[1], 3) if len(resistances) > 1 else None,
    }

# ═══════════════════════════════════════════
# 掛單價計算
# ═══════════════════════════════════════════
def calc_order_prices(current, atr, sr, side='BUY'):
    """計算建議掛單價、止損價、止盈價"""
    if not atr or atr <= 0:
        atr = current * 0.02  # fallback: 2%
    
    if side == 'BUY':
        # 買入價：第一支撐位附近，或現價回調1個ATR
        entry = sr.get('support_1') or (current - atr * 0.5)
        entry = max(entry, current * 0.97)  # 最多等3%回調
        
        # 止損：第二支撐位或入場價下方2ATR
        stop_loss = sr.get('support_2') or (entry - atr * 2)
        stop_loss = max(stop_loss, entry * 0.92)  # 最多虧8%
        
        # 止盈：第一阻力位，或入場價上方3ATR
        take_profit = sr.get('resistance_1') or (entry + atr * 3)
        
        # 風險回報比
        risk = entry - stop_loss
        reward = take_profit - entry
        rr_ratio = reward / risk if risk > 0 else 0
        
        return {
            'entry_price': round(entry, 3),
            'stop_loss': round(stop_loss, 3),
            'take_profit': round(take_profit, 3),
            'risk_pct': round(risk / entry * 100, 1),
            'reward_pct': round(reward / entry * 100, 1),
            'rr_ratio': round(rr_ratio, 2),
            'atr': round(atr, 3),
            'support_1': sr.get('support_1'),
            'support_2': sr.get('support_2'),
            'resistance_1': sr.get('resistance_1'),
            'resistance_2': sr.get('resistance_2'),
        }
    else:  # SELL
        entry = sr.get('resistance_1') or (current + atr * 0.5)
        entry = min(entry, current * 1.03)
        stop_loss = sr.get('resistance_2') or (entry + atr * 2)
        stop_loss = min(stop_loss, entry * 1.08)
        take_profit = sr.get('support_1') or (entry - atr * 3)
        
        risk = stop_loss - entry
        reward = entry - take_profit
        rr_ratio = reward / risk if risk > 0 else 0
        
        return {
            'entry_price': round(entry, 3),
            'stop_loss': round(stop_loss, 3),
            'take_profit': round(take_profit, 3),
            'risk_pct': round(risk / entry * 100, 1),
            'reward_pct': round(reward / entry * 100, 1),
            'rr_ratio': round(rr_ratio, 2),
            'atr': round(atr, 3),
            'support_1': sr.get('support_1'),
            'resistance_1': sr.get('resistance_1'),
        }

# ═══════════════════════════════════════════
# 小時級價格預測
# ═══════════════════════════════════════════
def calc_hourly_prediction(closes, volumes, current):
    """基於近期走勢預測未來幾個時段嘅價格區間"""
    if len(closes) < 20:
        return None
    
    # 計算日均波動率
    daily_ranges = []
    for i in range(1, min(20, len(closes))):
        daily_ranges.append(abs(closes[-i] - closes[-i-1]) / closes[-i-1])
    avg_daily_move = sum(daily_ranges) / len(daily_ranges) if daily_ranges else 0.02
    
    # 假設每小時 = 日波動/6.5（港股交易時段）
    hourly_move = avg_daily_move / 6.5
    
    # 趨勢方向（基於MA斜率）
    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    trend = 0
    if ma5 and ma10:
        if current > ma5 > ma10:
            trend = 1  # 上升
        elif current < ma5 < ma10:
            trend = -1  # 下降
    
    # 預測未來4個時段
    predictions = []
    for h in range(1, 5):
        # 基礎預測 = 趨勢方向 × 小時波動 × 時段數
        drift = trend * hourly_move * h * 0.3  # 趨勢drift（保守）
        uncertainty = hourly_move * h * 0.7  # 不確定性
        
        pred_center = current * (1 + drift)
        pred_high = current * (1 + drift + uncertainty)
        pred_low = current * (1 + drift - uncertainty)
        
        predictions.append({
            'hours': h,
            'center': round(pred_center, 2),
            'high': round(pred_high, 2),
            'low': round(pred_low, 2),
            'range_pct': round(uncertainty * 100, 2),
        })
    
    return {
        'daily_volatility': round(avg_daily_move * 100, 2),
        'hourly_volatility': round(hourly_move * 100, 2),
        'trend': '上升' if trend > 0 else ('下降' if trend < 0 else '橫盤'),
        'predictions': predictions,
    }

# ═══════════════════════════════════════════
# 技術面評分（v3同款邏輯）
# ═══════════════════════════════════════════
def score_technical(closes, highs, lows, volumes):
    """返回 -1~+1 嘅技術面分數同原因"""
    if len(closes) < 30:
        return 0, [], {}
    
    current = closes[-1]
    reasons = []
    components = {}
    
    # 趨勢 (30%)
    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    trend_score = 0
    
    if ma5 and ma10 and ma20:
        if current > ma5 > ma10 > ma20:
            trend_score = 1.0
            reasons.append("強勢多頭排列")
        elif current > ma5 > ma10:
            trend_score = 0.6
            reasons.append("短中期多頭")
        elif current > ma5:
            trend_score = 0.3
            reasons.append("站上5日線")
        elif current < ma5 < ma10 < ma20:
            trend_score = -1.0
            reasons.append("⚠️空頭排列")
        elif current < ma5 < ma10:
            trend_score = -0.6
            reasons.append("⚠️短中期空頭")
        elif current < ma5:
            trend_score = -0.3
            reasons.append("⚠️跌破5日線")
    
    if ma20 and len(closes) >= 25:
        ma20_prev = calc_ma(closes[:-5], 20)
        if ma20_prev:
            slope = (ma20 - ma20_prev) / ma20_prev * 100
            if slope > 2:
                trend_score = min(trend_score + 0.3, 1.0)
                reasons.append(f"MA20上升{slope:+.1f}%")
            elif slope < -2:
                trend_score = max(trend_score - 0.3, -1.0)
                reasons.append(f"⚠️MA20下降{slope:+.1f}%")
    components['trend'] = trend_score
    
    # 動量 (25%)
    momentum_score = 0
    rsi = calc_rsi(closes)
    macd_line, signal_line, histogram = calc_macd(closes)
    
    if rsi:
        if rsi < 25: momentum_score += 0.4; reasons.append(f"RSI超賣({rsi:.0f})")
        elif rsi < 35: momentum_score += 0.2; reasons.append(f"RSI偏低({rsi:.0f})")
        elif rsi > 75: momentum_score -= 0.4; reasons.append(f"⚠️RSI超買({rsi:.0f})")
        elif rsi > 65: momentum_score -= 0.2; reasons.append(f"⚠️RSI偏高({rsi:.0f})")
    
    if macd_line is not None:
        if histogram > 0 and macd_line > 0: momentum_score += 0.3; reasons.append("MACD金叉+正值")
        elif histogram > 0: momentum_score += 0.15; reasons.append("MACD柱轉正")
        elif histogram < 0 and macd_line < 0: momentum_score -= 0.3; reasons.append("⚠️MACD死叉+負值")
        elif histogram < 0: momentum_score -= 0.15; reasons.append("⚠️MACD柱轉負")
    
    if len(closes) >= 6:
        mom5 = (closes[-1] - closes[-6]) / closes[-6] * 100
        if mom5 > 5: momentum_score += 0.2; reasons.append(f"5日動量+{mom5:.1f}%")
        elif mom5 > 2: momentum_score += 0.1; reasons.append(f"5日+{mom5:.1f}%")
        elif mom5 < -5: momentum_score -= 0.2; reasons.append(f"⚠️5日{mom5:.1f}%")
        elif mom5 < -2: momentum_score -= 0.1; reasons.append(f"⚠️5日{mom5:.1f}%")
    
    momentum_score = max(-1, min(1, momentum_score))
    components['momentum'] = momentum_score
    
    # 結構 (20%)
    structure_score = 0
    upper, bb_mid, lower = calc_bollinger(closes)
    if upper and lower:
        bb_pos = (current - lower) / (upper - lower) if upper > lower else 0.5
        if current <= lower: structure_score += 0.3; reasons.append("觸及布林下軌")
        elif current >= upper: structure_score -= 0.2; reasons.append("⚠️觸及布林上軌")
        elif bb_pos < 0.3: structure_score += 0.15
    components['structure'] = structure_score
    
    # 量能 (15%)
    volume_score = 0
    if len(volumes) >= 20:
        vol_sma = sum(volumes[-20:]) / 20
        if vol_sma > 0:
            vol_ratio = volumes[-1] / vol_sma
            price_up = closes[-1] > closes[-2]
            if price_up and vol_ratio > 1.5: volume_score += 0.4; reasons.append(f"放量上漲({vol_ratio:.1f}x)")
            elif price_up and vol_ratio > 1.2: volume_score += 0.2
            elif not price_up and vol_ratio > 2.0: volume_score -= 0.3; reasons.append(f"⚠️放量下跌({vol_ratio:.1f}x)")
    components['volume'] = volume_score
    
    # 位置 (10%)
    position_score = 0
    if len(closes) >= 60:
        h60, l60 = max(closes[-60:]), min(closes[-60:])
        if h60 > l60:
            pos = (current - l60) / (h60 - l60)
            if pos < 0.2: position_score += 0.3; reasons.append(f"60日低位{pos*100:.0f}%")
            elif pos > 0.85: position_score -= 0.2; reasons.append(f"⚠️60日高位{pos*100:.0f}%")
    components['position'] = position_score
    
    # 綜合
    raw = (trend_score * 0.30 + momentum_score * 0.25 + structure_score * 0.20 +
           volume_score * 0.15 + position_score * 0.10)
    final = (raw + 1) / 2
    
    # 硬規則
    if trend_score <= -0.6:
        final = min(final, 0.45)
        reasons.append("⛔ 強空頭禁止買入")
    
    return final, reasons, components

# ═══════════════════════════════════════════
# 主分析流程
# ═══════════════════════════════════════════
def analyze_stock(symbol, exchange):
    """全面分析一隻股票，返回完整報告"""
    
    # 獲取K線
    raw = db(f"""
        SELECT open_price, high_price, low_price, close_price, volume 
        FROM klines WHERE symbol='{symbol}' AND interval='day'
        ORDER BY timestamp DESC LIMIT 120
    """)
    if not raw: return None
    
    rows = []
    for line in raw.split('\n'):
        if not line.strip(): continue
        p = line.split('|')
        if len(p) >= 5:
            try:
                rows.append({'open': float(p[0]), 'high': float(p[1]), 'low': float(p[2]),
                            'close': float(p[3]), 'volume': float(p[4])})
            except: continue
    
    if len(rows) < 30: return None
    rows.reverse()
    
    closes = [r['close'] for r in rows]
    highs = [r['high'] for r in rows]
    lows = [r['low'] for r in rows]
    volumes = [r['volume'] for r in rows]
    current = closes[-1]
    
    # 1. 技術面評分
    tech_score, tech_reasons, tech_components = score_technical(closes, highs, lows, volumes)
    
    # 2. 支撐阻力
    sr = calc_support_resistance(closes, highs, lows, current)
    
    # 3. ATR
    atr = calc_atr(highs, lows, closes)
    
    # 4. 小時級預測
    prediction = calc_hourly_prediction(closes, volumes, current)
    
    # 5. 判斷方向
    if tech_score >= 0.62: side = 'BUY'
    elif tech_score <= 0.38: side = 'SELL'
    else: side = 'HOLD'
    
    # 6. 掛單價
    order_prices = None
    if side in ('BUY', 'SELL'):
        order_prices = calc_order_prices(current, atr, sr, side)
    
    # 7. 技術指標原始值
    rsi = calc_rsi(closes)
    macd_line, signal_line, histogram = calc_macd(closes)
    upper, bb_mid, lower = calc_bollinger(closes)
    ma5, ma10, ma20 = calc_ma(closes, 5), calc_ma(closes, 10), calc_ma(closes, 20)
    
    # 8. 風險評估
    risk_flags = []
    if rsi and rsi > 75: risk_flags.append("RSI超買")
    if rsi and rsi < 25: risk_flags.append("RSI超賣")
    if current >= upper * 0.98 if upper else False: risk_flags.append("觸及布林上軌")
    if current <= lower * 1.02 if lower else False: risk_flags.append("觸及布林下軌")
    if order_prices and order_prices.get('rr_ratio', 0) < 1.5: risk_flags.append(f"風險回報比偏低({order_prices.get('rr_ratio')})")
    
    return {
        'symbol': symbol,
        'exchange': exchange,
        'price': round(current, 3),
        'side': side,
        'score': round(tech_score, 4),
        'reasons': tech_reasons,
        'components': {k: round(v, 3) for k, v in tech_components.items()},
        'indicators': {
            'rsi': round(rsi, 1) if rsi else None,
            'macd': round(macd_line, 4) if macd_line else None,
            'macd_signal': round(signal_line, 4) if signal_line else None,
            'macd_hist': round(histogram, 4) if histogram else None,
            'ma5': round(ma5, 3) if ma5 else None,
            'ma10': round(ma10, 3) if ma10 else None,
            'ma20': round(ma20, 3) if ma20 else None,
            'bb_upper': round(upper, 3) if upper else None,
            'bb_mid': round(bb_mid, 3) if bb_mid else None,
            'bb_lower': round(lower, 3) if lower else None,
            'atr': round(atr, 3) if atr else None,
        },
        'support_resistance': sr,
        'order_prices': order_prices,
        'prediction': prediction,
        'risk_flags': risk_flags,
    }

def run():
    DB_ERRORS.clear()
    log("=" * 60)
    log("信號引擎 v4 — 全面多維度分析")

    trade_date = latest_kline_date()
    if not trade_date:
        log("❌ 無法取得最新K線日期")
        return False
    run_id = run_id_for(trade_date)
    log(f"trade_date={trade_date} run_id={run_id}")
    write_blocked, block_reason = daily_signal_write_block(trade_date)
    if write_blocked:
        log(f"⏸️ 跳過盤中日線信號寫入: {block_reason}")
        log("   K線更新可以繼續；signal_v4 daily run 需等收市後完整日K。")
        return True

    stocks = candidate_stocks_for_date(trade_date)
    
    if not stocks:
        log(f"❌ {trade_date} 無足夠K線數據")
        return False
    
    total = len([l for l in stocks.split('\n') if l.strip()])
    log(f"分析 {total} 隻股票...")
    ensure_feature_run(run_id, trade_date, total)
    if DB_ERRORS:
        log("❌ 建立feature run失敗，停止寫入信號")
        return False
    
    results = {'BUY': [], 'SELL': [], 'HOLD': []}
    processed = 0
    
    for line in stocks.split('\n'):
        if not line.strip(): continue
        parts = line.split('|')
        if len(parts) < 2: continue
        symbol, exchange = parts[0], parts[1]
        
        result = analyze_stock(symbol, exchange)
        if result is None: continue
        results[result['side']].append(result)
        
        # 更新DB
        quality = {
            'reasons': result['reasons'],
            'components': result['components'],
            **result['indicators'],
            'support_resistance': result['support_resistance'],
            'order_prices': result['order_prices'],
            'prediction': result['prediction'],
            'risk_flags': result['risk_flags'],
            'price': result['price'],
            'exchange': result['exchange'],
            'trade_date': trade_date,
            'run_id': run_id,
        }
        upsert_signal_score(result, trade_date, run_id, quality)
        if DB_ERRORS:
            log(f"❌ 寫入 {symbol} 信號失敗，停止本輪run")
            return False
        processed += 1
    
    results['BUY'].sort(key=lambda x: x['score'], reverse=True)
    results['SELL'].sort(key=lambda x: x['score'])
    finalize_feature_run(run_id, total, processed, {k: len(v) for k, v in results.items()})
    if DB_ERRORS:
        log("❌ finalize feature run失敗")
        return False

    log(f"\n{'='*60}")
    log(f"📊 分析完成: {processed}/{total} 隻")
    log(f"   🟢 BUY: {len(results['BUY'])} | ⚪ HOLD: {len(results['HOLD'])} | 🔴 SELL: {len(results['SELL'])}")
    
    log(f"\n── TOP BUY 信號（含掛單價）──")
    for s in results['BUY'][:15]:
        op = s.get('order_prices', {})
        pred = s.get('prediction', {})
        pred_trend = pred.get('trend', '?')
        log(f"  {s['symbol']:8s} score={s['score']:.3f} ${s['price']}")
        log(f"    原因: {', '.join(s['reasons'][:4])}")
        if op:
            log(f"    📌 建議買入: ${op.get('entry_price','?')} | 止損: ${op.get('stop_loss','?')} | 止盈: ${op.get('take_profit','?')} | 風險回報: {op.get('rr_ratio','?')}")
        if pred:
            preds = pred.get("predictions", [])
            if preds and len(preds) > 3:
                log(f"    📈 小時預測: {pred_trend} | 4h區間: ${preds[3]['low']}~${preds[3]['high']}")
        if s['risk_flags']:
            log(f"    ⚠️ 風險: {', '.join(s['risk_flags'])}")
        log("")
    
    log(f"✅ 信號已更新 (v4) run_id={run_id}")
    return True

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", action="store_true", help="validate DB/schema/date inputs without writing")
    parser.add_argument("--json", action="store_true", help="emit JSON for --preflight")
    return parser.parse_args()

def main():
    args = parse_args()
    if args.preflight:
        payload = build_preflight_payload()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                f"signal_engine_v4 preflight: {payload['status']} "
                f"trade_date={payload['trade_date'] or 'none'} "
                f"run_id={payload['run_id'] or 'none'} "
                f"candidate_count={payload['candidate_count']} "
                f"write_blocked={payload['write_blocked']} "
                f"count_columns={payload['feature_run_count_columns']}"
            )
            if payload["block_reason"]:
                print("block_reason=" + payload["block_reason"])
            if payload["db_errors"]:
                print("db_errors=" + "; ".join(payload["db_errors"]))
        return 0 if payload["status"] == "OK" else 2
    return 0 if run() else 1

if __name__ == '__main__':
    sys.exit(main())
