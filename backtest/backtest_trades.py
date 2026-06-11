#!/usr/bin/env python3
"""提取全部回測交易明細"""
import subprocess, json, sys
from datetime import datetime

DB_CMD = ['docker', 'exec', 'quantmind-db', 'psql', '-U', 'quantmind', '-d', 'quantmind', '-t', '-A', '-c']

def db(sql):
    r = subprocess.run(DB_CMD + [sql], capture_output=True, text=True, timeout=30)
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
    return ma+num_std*std, ma, ma-num_std*std

def score_stock(closes, highs, lows, volumes):
    if len(closes) < 30: return None, []
    current = closes[-1]; reasons = []
    ma5 = calc_ma(closes, 5); ma10 = calc_ma(closes, 10); ma20 = calc_ma(closes, 20)
    trend = 0
    if ma5 and ma10 and ma20:
        if current > ma5 > ma10 > ma20: trend = 0.8; reasons.append("多頭")
        elif current > ma5 and current > ma10: trend = 0.4
        elif current < ma5 < ma10 < ma20: trend = -0.8; reasons.append("空頭")
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
        avg_v = sum(volumes[-20:])/20
        if avg_v > 0:
            vr = volumes[-1]/avg_v
            if vr > 1.5 and current > closes[-2]: vol_s += 0.3
            elif vr > 1.5 and current < closes[-2]: vol_s -= 0.2
    pos = 0
    if len(closes) >= 60:
        h60, l60 = max(closes[-60:]), min(closes[-60:])
        if h60 > l60:
            p = (current-l60)/(h60-l60)
            if p < 0.2: pos += 0.3
            elif p > 0.85: pos -= 0.2
    raw = trend*0.30 + mom*0.25 + struct*0.20 + vol_s*0.15 + pos*0.10
    final = (raw+1)/2
    if trend <= -0.6: final = min(final, 0.45)
    return max(0, min(1, final)), reasons

def chandelier_stop(highs, lows, closes, period=22, mult=3):
    atr = calc_atr(highs, lows, closes, period)
    if not atr: return None
    return max(highs[-period:]) - mult * atr

def get_klines(symbol, days=500):
    raw = db(f"SELECT timestamp::date,open_price,high_price,low_price,close_price,volume FROM klines WHERE symbol='{symbol}' AND interval='day' ORDER BY timestamp DESC LIMIT {days}")
    rows = []
    for line in raw.split('\n'):
        if not line.strip(): continue
        p = line.split('|')
        if len(p) >= 6:
            try: rows.append({'date':p[0],'open':float(p[1]),'high':float(p[2]),'low':float(p[3]),'close':float(p[4]),'volume':float(p[5])})
            except: continue
    rows.reverse(); return rows

def backtest(symbol, klines, lookback=60, chandelier_mult=2, use_cost=True):
    if len(klines) < lookback + 30: return []
    trades = []; pos = None
    for i in range(lookback, len(klines)):
        hc = [k['close'] for k in klines[:i]]; hh = [k['high'] for k in klines[:i]]
        hl = [k['low'] for k in klines[:i]]; hv = [k['volume'] for k in klines[:i]]
        score, reasons = score_stock(hc, hh, hl, hv)
        if score is None: continue
        today = klines[i]; ep = today['open']
        if pos is None:
            if score >= 0.62:
                cs = chandelier_stop(hh, hl, hc, mult=chandelier_mult)
                sl = cs if cs else ep * 0.92
                cost = 0
                if use_cost and symbol[0].isdigit():
                    tv = ep * 1000; cost = max(tv*0.001, 50) + tv*0.0013 + tv*0.000077
                pos = {'entry_price':ep,'entry_date':today['date'],'stop_loss':sl,'score':score,'buy_cost':cost,'idx':i}
        else:
            cs = chandelier_stop(hh, hl, hc, mult=chandelier_mult)
            if cs: pos['stop_loss'] = max(pos['stop_loss'], cs)
            exit_p = None; reason = None
            if today['low'] <= pos['stop_loss']:
                exit_p = max(today['open'], pos['stop_loss']); reason = '止損'
            elif score <= 0.38:
                exit_p = ep; reason = 'SELL信號'
            if exit_p:
                sell_cost = 0
                if use_cost and symbol[0].isdigit():
                    tv = exit_p * 1000; sell_cost = max(tv*0.001, 50) + tv*0.0013 + tv*0.000077
                total_cost = pos['buy_cost'] + sell_cost
                net_pnl = (exit_p - pos['entry_price']) * 1000 - total_cost
                net_pnl_pct = net_pnl / (pos['entry_price'] * 1000)
                trades.append({
                    'symbol': symbol,
                    'entry_date': pos['entry_date'], 'exit_date': today['date'],
                    'entry_price': pos['entry_price'], 'exit_price': exit_p,
                    'qty': 1000,
                    'gross_pnl': round((exit_p-pos['entry_price'])*1000, 2),
                    'cost': round(total_cost, 2),
                    'net_pnl': round(net_pnl, 2),
                    'net_pnl_pct': round(net_pnl_pct, 4),
                    'hold_days': i - pos['idx'],
                    'reason': reason,
                    'entry_score': round(pos['score'], 3),
                    'exit_score': round(score, 3),
                })
                pos = None
    if pos:
        lp = klines[-1]['close']
        sell_cost = 0
        if use_cost and symbol[0].isdigit():
            tv = lp * 1000; sell_cost = max(tv*0.001, 50) + tv*0.0013 + tv*0.000077
        total_cost = pos['buy_cost'] + sell_cost
        net_pnl = (lp - pos['entry_price']) * 1000 - total_cost
        trades.append({
            'symbol': symbol,
            'entry_date': pos['entry_date'], 'exit_date': klines[-1]['date'],
            'entry_price': pos['entry_price'], 'exit_price': lp,
            'qty': 1000,
            'gross_pnl': round((lp-pos['entry_price'])*1000, 2),
            'cost': round(total_cost, 2),
            'net_pnl': round(net_pnl, 2),
            'net_pnl_pct': round(net_pnl/(pos['entry_price']*1000), 4),
            'hold_days': len(klines) - pos['idx'],
            'reason': '數據結束',
            'entry_score': round(pos['score'], 3),
            'exit_score': 0,
        })
    return trades

# 主流程
raw = db("SELECT symbol, count(*) as cnt FROM klines WHERE interval='day' GROUP BY symbol HAVING count(*) >= 300 ORDER BY symbol")
symbols = []
for line in raw.split('\n'):
    if not line.strip(): continue
    p = line.split('|')
    if len(p) >= 2: symbols.append(p[0])

all_trades = []
for sym in symbols:
    klines = get_klines(sym, 500)
    if len(klines) < 200: continue
    trades = backtest(sym, klines, chandelier_mult=2, use_cost=True)
    all_trades.extend(trades)

# 輸出CSV
all_trades.sort(key=lambda x: x['entry_date'])
print("symbol,entry_date,exit_date,entry_price,exit_price,qty,gross_pnl,cost,net_pnl,net_pnl_pct,hold_days,reason,entry_score,exit_score")
for t in all_trades:
    print(f"{t['symbol']},{t['entry_date']},{t['exit_date']},{t['entry_price']},{t['exit_price']},{t['qty']},{t['gross_pnl']},{t['cost']},{t['net_pnl']},{t['net_pnl_pct']},{t['hold_days']},{t['reason']},{t['entry_score']},{t['exit_score']}")

# 統計
wins = [t for t in all_trades if t['net_pnl'] > 0]
losses = [t for t in all_trades if t['net_pnl'] <= 0]
print(f"\n# 總計: {len(all_trades)} 筆交易 | 贏: {len(wins)} 輸: {len(losses)} | 勝率: {len(wins)/len(all_trades)*100:.1f}%")
print(f"# 總盈虧: ${sum(t['net_pnl'] for t in all_trades):,.0f}")
print(f"# 總成本: ${sum(t['cost'] for t in all_trades):,.0f}")
print(f"# 平均持有: {sum(t['hold_days'] for t in all_trades)/len(all_trades):.1f} 日")
