#!/usr/bin/env python3
"""
分段回測：覆蓋熊市/震盪/牛市，真實滑點，組合層面
只用有5年+歷史嘅股票（覆蓋2022熊市）
"""
import subprocess, json, sys, statistics
from datetime import datetime, timedelta

DB_CMD = ['docker', 'exec', 'quantmind-db', 'psql', '-U', 'quantmind', '-d', 'quantmind', '-t', '-A', '-c']

def db(sql):
    r = subprocess.run(DB_CMD + [sql], capture_capture=True, text=True, timeout=30) if False else subprocess.run(DB_CMD + [sql], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]; losses = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period; avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))

def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal: return None, None, None
    def ema(data, period):
        k = 2 / (period + 1); result = [data[0]]
        for i in range(1, len(data)): result.append(data[i] * k + result[-1] * (1 - k))
        return result
    ef = ema(closes, fast); es = ema(closes, slow)
    ml = [ef[i] - es[i] for i in range(len(closes))]
    sl = ema(ml, signal); hist = [ml[i] - sl[i] for i in range(len(closes))]
    return ml[-1], sl[-1], hist[-1]

def calc_ma(closes, period):
    if len(closes) < period: return None
    return sum(closes[-period:]) / period

def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1: return None
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
    return sum(trs[-period:]) / period if len(trs) >= period else None

def calc_bollinger(closes, period=20, num_std=2):
    if len(closes) < period: return None, None, None
    w = closes[-period:]; ma = sum(w)/period; std = (sum((x-ma)**2 for x in w)/period)**0.5
    return ma+num_std*std, ma, ma-num_std*2

def score_stock(closes, highs, lows, volumes):
    if len(closes) < 30: return None, []
    current = closes[-1]; reasons = []
    ma5 = calc_ma(closes, 5); ma10 = calc_ma(closes, 10); ma20 = calc_ma(closes, 20)
    trend = 0
    if ma5 and ma10 and ma20:
        if current > ma5 > ma10 > ma20: trend = 0.8; reasons.append("多頭排列")
        elif current > ma5 and current > ma10: trend = 0.4
        elif current < ma5 < ma10 < ma20: trend = -0.8; reasons.append("空頭排列")
        elif current < ma5 and current < ma10: trend = -0.4
        if len(closes) >= 25:
            m20_5 = sum(closes[-25:-5])/20
            if ma20 and m20_5 > 0:
                s = (ma20-m20_5)/m20_5
                if s > 0.03: trend += 0.2
                elif s < -0.03: trend -= 0.2
    mom = 0; rsi = calc_rsi(closes)
    if rsi:
        if rsi > 70: mom -= 0.3
        elif rsi > 55: mom += 0.3
        elif rsi < 30: mom += 0.3
        elif rsi < 45: mom -= 0.2
    ml, sl, hist = calc_macd(closes)
    if ml is not None:
        if hist > 0 and ml > 0: mom += 0.3
        elif hist > 0: mom += 0.1
        elif hist < 0 and ml < 0: mom -= 0.3
        elif hist < 0: mom -= 0.1
    struct = 0; up, bm, lo = calc_bollinger(closes)
    if up and lo:
        if current <= lo*1.02: struct += 0.3
        elif current >= up*0.98: struct -= 0.2
    vol_s = 0
    if len(volumes) >= 20:
        avg20 = sum(volumes[-20:])/20
        if avg20 > 0:
            vr = volumes[-1]/avg20
            if vr > 1.5 and current > closes[-2]: vol_s += 0.2
            elif vr > 2.0: vol_s += 0.1
            elif vr < 0.5: vol_s -= 0.1
    total = trend + mom + struct + vol_s
    total = max(-1, min(1, total))
    return total, reasons

def chandelier_stop(highs, lows, closes, period=22, mult=2):
    atr = calc_atr(highs, lows, closes, period)
    if not atr: return None
    return max(highs[-period:]) - mult * atr

def get_klines(symbol, start_date=None, end_date=None, days=5000):
    date_filter = ""
    if start_date and end_date:
        date_filter = f"AND timestamp::date >= '{start_date}' AND timestamp::date <= '{end_date}'"
    elif start_date:
        date_filter = f"AND timestamp::date >= '{start_date}'"
    raw = db(f"SELECT timestamp::date,open_price,high_price,low_price,close_price,volume FROM klines WHERE symbol='{symbol}' AND interval='day' {date_filter} ORDER BY timestamp ASC LIMIT {days}")
    rows = []
    for line in raw.split('\n'):
        if not line.strip(): continue
        p = line.split('|')
        if len(p) >= 6:
            try: rows.append({'date':p[0],'open':float(p[1]),'high':float(p[2]),'low':float(p[3]),'close':float(p[4]),'volume':float(p[5])})
            except: continue
    return rows

# ========== 參數 ==========
BUY_THRESHOLD = 0.65   # 提高門檻
SELL_THRESHOLD = 0.35
SLIPPAGE_PCT = 0.002   # 0.2% 滑點
INITIAL_CAPITAL = 100000
MAX_POSITIONS = 10
POSITION_SIZE = 0.1    # 每倉10%

# ========== 時間段 ==========
PERIODS = {
    "2022熊市": ("2022-01-01", "2022-12-31"),
    "2023震盪": ("2023-01-01", "2023-12-31"),
    "2024牛市": ("2024-01-01", "2024-12-31"),
    "2025至今": ("2025-01-01", "2026-06-11"),
    "全程2022-2026": ("2022-01-01", "2026-06-11"),
}

# ========== 撈有長期歷史嘅股票 ==========
raw = db("SELECT symbol, count(*) as cnt FROM klines WHERE interval='day' GROUP BY symbol HAVING count(*) >= 1000 ORDER BY symbol")
all_symbols = []
for line in raw.split('\n'):
    if not line.strip(): continue
    p = line.split('|')
    if len(p) >= 2: all_symbols.append(p[0])

print(f"有1000+日歷史嘅股票: {len(all_symbols)}隻")
print(f"股票列表: {', '.join(all_symbols)}")
print()

# ========== 組合回測函數 ==========
def portfolio_backtest(symbols, start_date, end_date, label):
    capital = INITIAL_CAPITAL
    positions = {}  # symbol -> {entry_price, shares, stop_loss, entry_date, score}
    trades = []
    daily_nav = []
    signal_count = 0
    no_signal_days = 0
    total_days = 0
    
    # 獲取所有股票嘅K線
    all_klines = {}
    for sym in symbols:
        klines = get_klines(sym, start_date, end_date)
        if len(klines) >= 90:
            all_klines[sym] = klines
    
    if not all_klines:
        return None
    
    # 搵出共同交易日
    all_dates = set()
    for sym, klines in all_klines.items():
        for k in klines:
            all_dates.add(k['date'])
    all_dates = sorted(all_dates)
    
    if len(all_dates) < 30:
        return None
    
    # 按日遍歷
    prev_nav = INITIAL_CAPITAL
    for date in all_dates:
        total_days += 1
        # 計算當日持倉市值
        nav = capital
        for sym, pos in positions.items():
            if sym in all_klines:
                day_data = [k for k in all_klines[sym] if k['date'] == date]
                if day_data:
                    nav += day_data[0]['close'] * pos['shares']
                else:
                    nav += pos['entry_price'] * pos['shares']
        
        daily_nav.append({'date': date, 'nav': nav})
        
        # 檢查止損
        to_sell = []
        for sym, pos in list(positions.items()):
            if sym not in all_klines: continue
            day_data = [k for k in all_klines[sym] if k['date'] == date]
            if not day_data: continue
            today = day_data[0]
            
            # 更新trailing stop
            cs_data = [k for k in all_klines[sym] if k['date'] <= date]
            if len(cs_data) >= 22:
                hh = [k['high'] for k in cs_data]
                hl = [k['low'] for k in cs_data]
                hc = [k['close'] for k in cs_data]
                cs = chandelier_stop(hh, hl, hc, mult=2)
                if cs and cs > pos['stop_loss']:
                    pos['stop_loss'] = cs
            
            # 止損觸發
            if today['low'] <= pos['stop_loss']:
                exit_price = max(today['open'], pos['stop_loss'])
                exit_price_after_slip = exit_price * (1 - SLIPPAGE_PCT)
                pnl = (exit_price_after_slip - pos['entry_price']) * pos['shares']
                capital += exit_price_after_slip * pos['shares']
                trades.append({
                    'symbol': sym, 'entry_date': pos['entry_date'], 'exit_date': date,
                    'entry_price': pos['entry_price'], 'exit_price': exit_price_after_slip,
                    'pnl': round(pnl, 2), 'pnl_pct': round((exit_price_after_slip/pos['entry_price']-1)*100, 2),
                    'reason': '止損', 'hold_days': (datetime.strptime(date, '%Y-%m-%d') - datetime.strptime(pos['entry_date'], '%Y-%m-%d')).days
                })
                to_sell.append(sym)
        
        for sym in to_sell:
            del positions[sym]
        
        # 如果有空位，掃描信號
        if len(positions) < MAX_POSITIONS:
            best_signal = None
            best_score = 0
            for sym, klines in all_klines.items():
                if sym in positions: continue
                day_idx = None
                for idx, k in enumerate(klines):
                    if k['date'] == date:
                        day_idx = idx
                        break
                if day_idx is None or day_idx < 60: continue
                
                hc = [k['close'] for k in klines[:day_idx+1]]
                hh = [k['high'] for k in klines[:day_idx+1]]
                hl = [k['low'] for k in klines[:day_idx+1]]
                hv = [k['volume'] for k in klines[:day_idx+1]]
                score, reasons = score_stock(hc, hh, hl, hv)
                signal_count += 1
                
                if score and score >= BUY_THRESHOLD and score > best_score:
                    best_score = score
                    best_signal = (sym, klines[day_idx], score, reasons)
            
            if best_signal:
                sym, today, score, reasons = best_signal
                entry_price = today['open'] * (1 + SLIPPAGE_PCT)
                alloc = capital * POSITION_SIZE
                shares = int(alloc / entry_price)
                if shares > 0:
                    cost = entry_price * shares
                    if cost <= capital:
                        hh = [k['high'] for k in all_klines[sym][:day_idx+1]]
                        hl = [k['low'] for k in all_klines[sym][:day_idx+1]]
                        hc = [k['close'] for k in all_klines[sym][:day_idx+1]]
                        cs = chandelier_stop(hh, hl, hc, mult=2)
                        sl = cs if cs else entry_price * 0.92
                        positions[sym] = {
                            'entry_price': entry_price, 'shares': shares,
                            'stop_loss': sl, 'entry_date': date, 'score': score
                        }
                        capital -= cost
            else:
                no_signal_days += 1
    
    # 清倉
    for sym, pos in positions.items():
        if sym in all_klines and all_klines[sym]:
            last_price = all_klines[sym][-1]['close'] * (1 - SLIPPAGE_PCT)
            pnl = (last_price - pos['entry_price']) * pos['shares']
            capital += last_price * pos['shares']
            trades.append({
                'symbol': sym, 'entry_date': pos['entry_date'], 'exit_date': all_klines[sym][-1]['date'],
                'entry_price': pos['entry_price'], 'exit_price': last_price,
                'pnl': round(pnl, 2), 'pnl_pct': round((last_price/pos['entry_price']-1)*100, 2),
                'reason': '結束', 'hold_days': 0
            })
    
    # 計算指標
    final_nav = capital
    total_return = (final_nav / INITIAL_CAPITAL - 1) * 100
    
    # MaxDD
    peak = 0; max_dd = 0
    for d in daily_nav:
        if d['nav'] > peak: peak = d['nav']
        dd = (peak - d['nav']) / peak * 100 if peak > 0 else 0
        if dd > max_dd: max_dd = dd
    
    # 交易統計
    win_trades = [t for t in trades if t['pnl'] > 0]
    lose_trades = [t for t in trades if t['pnl'] <= 0]
    win_rate = len(win_trades) / len(trades) * 100 if trades else 0
    avg_win = statistics.mean([t['pnl_pct'] for t in win_trades]) if win_trades else 0
    avg_lose = statistics.mean([t['pnl_pct'] for t in lose_trades]) if lose_trades else 0
    
    years = len(all_dates) / 252
    cagr = ((final_nav / INITIAL_CAPITAL) ** (1/years) - 1) * 100 if years > 0 else 0
    
    return {
        'label': label,
        'period': f"{all_dates[0]} ~ {all_dates[-1]}",
        'trading_days': total_days,
        'stocks_available': len(all_klines),
        'total_trades': len(trades),
        'win_rate': round(win_rate, 1),
        'avg_win_pct': round(avg_win, 2),
        'avg_lose_pct': round(avg_lose, 2),
        'total_return': round(total_return, 2),
        'cagr': round(cagr, 2),
        'max_dd': round(max_dd, 2),
        'calmar': round(cagr / max_dd, 2) if max_dd > 0 else 0,
        'final_nav': round(final_nav, 2),
        'no_signal_days': no_signal_days,
        'no_signal_pct': round(no_signal_days / total_days * 100, 1) if total_days > 0 else 0,
        'trades': trades,
    }

# ========== 跑回測 ==========
results = []
for label, (start, end) in PERIODS.items():
    print(f"正在回測: {label} ({start} ~ {end})...")
    r = portfolio_backtest(all_symbols, start, end, label)
    if r:
        results.append(r)
        print(f"  交易{r['total_trades']}筆 | 勝率{r['win_rate']}% | 回報{r['total_return']}% | MaxDD {r['max_dd']}% | 無信號天數{r['no_signal_pct']}%")
    else:
        print(f"  數據不足")
    print()

# ========== 輸出結果 ==========
print("=" * 80)
print("分段回測結果總結")
print("=" * 80)
for r in results:
    print(f"\n📊 {r['label']}")
    print(f"   時段: {r['period']}")
    print(f"   可用股票: {r['stocks_available']}隻")
    print(f"   交易筆數: {r['total_trades']}")
    print(f"   勝率: {r['win_rate']}%")
    print(f"   平均盈利: {r['avg_win_pct']}% | 平均虧損: {r['avg_lose_pct']}%")
    print(f"   總回報: {r['total_return']}%")
    print(f"   CAGR: {r['cagr']}%")
    print(f"   MaxDD: {r['max_dd']}%")
    print(f"   Calmar: {r['calmar']}")
    print(f"   無信號天數佔比: {r['no_signal_pct']}%")

# 存結果
with open('/tmp/segment_backtest_results.json', 'w') as f:
    json.dump(results, f, ensure_ascii=False, indent=2, default=str)
print("\n結果已存到 /tmp/segment_backtest_results.json")
