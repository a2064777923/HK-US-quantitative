#!/usr/bin/env python3
"""組合回測 v4 — 每5日掃描一次信號，高效版"""
import csv, json, statistics, sys
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

print("載入CSV...", flush=True)
sd = defaultdict(lambda: {'d':[],'o':[],'h':[],'l':[],'c':[],'v':[]})
with open('/tmp/all_klines.csv') as f:
    for row in csv.DictReader(f):
        s=row['symbol']; sd[s]['d'].append(row['dt']); sd[s]['o'].append(float(row['open_price']))
        sd[s]['h'].append(float(row['high_price'])); sd[s]['l'].append(float(row['low_price']))
        sd[s]['c'].append(float(row['close_price'])); sd[s]['v'].append(float(row['volume']))
syms=[s for s in sd if len(sd[s]['d'])>=200]
print(f"載入完成: {len(syms)}隻", flush=True)

def rsi(c,p=14):
    if len(c)<p+1: return None
    g=[max(c[i]-c[i-1],0) for i in range(1,len(c))]
    l=[max(c[i-1]-c[i],0) for i in range(1,len(c))]
    ag=sum(g[:p])/p; al=sum(l[:p])/p
    for i in range(p,len(g)): ag=(ag*(p-1)+g[i])/p; al=(al*(p-1)+l[i])/p
    return 100-(100/(1+ag/al)) if al>0 else 100

def score(closes,highs,lows,vols):
    n=len(closes)
    if n<30: return None
    c=closes[-1]
    ma5=sum(closes[-5:])/5 if n>=5 else None
    ma10=sum(closes[-10:])/10 if n>=10 else None
    ma20=sum(closes[-20:])/20 if n>=20 else None
    t=0
    if ma5 and ma10 and ma20:
        if c>ma5>ma10>ma20: t=0.8
        elif c>ma5 and c>ma10: t=0.4
        elif c<ma5<ma10<ma20: t=-0.8
        elif c<ma5 and c<ma10: t=-0.4
        if n>=25:
            m=sum(closes[-25:-5])/20
            if m>0:
                s=(ma20-m)/m
                if s>0.03: t+=0.2
                elif s<-0.03: t-=0.2
    m=0; r=rsi(closes)
    if r:
        if r>70: m-=0.3
        elif r>55: m+=0.3
        elif r<30: m+=0.3
        elif r<45: m-=0.2
    if n>=35:
        def ema(d,p):
            k=2/(p+1); r=[d[0]]
            for i in range(1,len(d)): r.append(d[i]*k+r[-1]*(1-k))
            return r
        ef=ema(closes,12); es=ema(closes,26)
        ml=[ef[i]-es[i] for i in range(n)]; sl=ema(ml,9); hist=[ml[i]-sl[i] for i in range(n)]
        if hist[-1]>0 and ml[-1]>0: m+=0.3
        elif hist[-1]>0: m+=0.1
        elif hist[-1]<0 and ml[-1]<0: m-=0.3
        elif hist[-1]<0: m-=0.1
    s=0
    if n>=20:
        w=closes[-20:]; ma=sum(w)/20; std=(sum((x-ma)**2 for x in w)/20)**.5
        if c<=ma-2*std*1.02: s+=0.3
        elif c>=ma+2*std*0.98: s-=0.2
    v=0
    if n>=20:
        a20=sum(vols[-20:])/20
        if a20>0:
            vr=vols[-1]/a20
            if vr>1.5 and c>closes[-2]: v+=0.2
            elif vr>2.0: v+=0.1
            elif vr<0.5: v-=0.1
    return max(-1,min(1,t+m+s+v))

def ch_stop(h,l,c,mult=2):
    if len(c)<23: return None
    trs=[max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])) for i in range(len(c)-22,len(c))]
    return max(h[-22:])-mult*(sum(trs)/22)

# 日期索引
print("建立索引...", flush=True)
d2i={sym:{sd[sym]['d'][i]:i for i in range(len(sd[sym]['d']))} for sym in syms}
all_d=sorted(set().union(*[set(sd[s]['d']) for s in syms]))
print(f"交易日: {all_d[0]}~{all_d[-1]} ({len(all_d)}天)", flush=True)

# ========== 參數 ==========
BUY=0.65; SELL=0.35; SLIP=0.002; INIT=100000.0
MAXA=0.15; MINA=0.03; MAXE=0.90; CD=3; SCAN_INTERVAL=5

# ========== 回測 ==========
cap=INIT; pos={}; cd={}; trades=[]; nav=[]; scan_day=0
print("開始回測...", flush=True)

for di, date in enumerate(all_d):
    # NAV
    nv=cap
    for s,p in pos.items():
        if s in d2i and date in d2i[s]: nv+=sd[s]['c'][d2i[s][date]]*p['sh']
        else: nv+=p['ep']*p['sh']
    nav.append({'d':date,'n':nv,'p':len(pos)})
    
    # 止損每日檢查
    tc=[]
    for s,p in list(pos.items()):
        if s not in d2i or date not in d2i[s]: continue
        ci=d2i[s][date]
        if ci>=22:
            hh=sd[s]['h'][max(0,ci-21):ci+1]; hl=sd[s]['l'][max(0,ci-21):ci+1]; hc=sd[s]['c'][max(0,ci-21):ci+1]
            cs=ch_stop(hh,hl,hc)
            if cs and cs>p['sl']: p['sl']=cs
        # 止損
        if sd[s]['l'][ci]<=p['sl']:
            ep=max(sd[s]['o'][ci],p['sl'])*(1-SLIP); pnl=(ep-p['ep'])*p['sh']; cap+=ep*p['sh']
            trades.append({'s':s,'ed':p['ed'],'xd':date,'ep':p['ep'],'xp':round(ep,4),'pn':round(pnl,2),'pc':round((ep/p['ep']-1)*100,2),'r':'止損','sc':p['sc']})
            tc.append(s); cd[s]=date
    for s in tc: del pos[s]
    
    # SELL信號（每5日檢查）
    if di % SCAN_INTERVAL == 0:
        tc2=[]
        for s,p in list(pos.items()):
            if s in tc: continue
            if s not in d2i or date not in d2i[s]: continue
            ci=d2i[s][date]
            sc=score(sd[s]['c'][:ci+1],sd[s]['h'][:ci+1],sd[s]['l'][:ci+1],sd[s]['v'][:ci+1])
            if sc is not None and sc<=SELL:
                ep=sd[s]['o'][ci]*(1-SLIP); pnl=(ep-p['ep'])*p['sh']; cap+=ep*p['sh']
                trades.append({'s':s,'ed':p['ed'],'xd':date,'ep':p['ep'],'xp':round(ep,4),'pn':round(pnl,2),'pc':round((ep/p['ep']-1)*100,2),'r':'SELL','sc':p['sc']})
                tc2.append(s)
        for s in tc2: del pos[s]
    
    # 開倉（每5日掃描）
    if di % SCAN_INTERVAL == 0:
        exp=sum(p['ep']*p['sh'] for p in pos.values())
        av=nv*MAXE-exp
        if av>=nv*0.05:
            cands=[]
            for sym in syms:
                if sym in pos: continue
                if sym in cd and (di - all_d.index(cd[sym]) if cd[sym] in all_d else 999)<CD: continue
                if sym not in d2i or date not in d2i[sym]: continue
                ci=d2i[sym][date]
                if ci<60: continue
                sc=score(sd[sym]['c'][:ci+1],sd[sym]['h'][:ci+1],sd[sym]['l'][:ci+1],sd[sym]['v'][:ci+1])
                if sc is not None and sc>=BUY:
                    atr=None
                    if ci>=14:
                        trs=[max(sd[sym][chr(104)][j]-sd[sym][chr(108)][j],abs(sd[sym][chr(104)][j]-sd[sym][chr(99)][j-1]),abs(sd[sym][chr(108)][j]-sd[sym][chr(99)][j-1])) for j in range(max(1,ci-13),ci+1)]
                        atr=sum(trs)/len(trs)
                    ap=atr/sd[sym]['c'][ci] if atr and sd[sym]['c'][ci]>0 else 0.02
                    cands.append((sym,sc,ap))
            cands.sort(key=lambda x:x[1],reverse=True)
            for sym,sc,ap in cands:
                if av<nv*0.03: break
                ci=d2i[sym][date]
                ep=sd[sym]['o'][ci]*(1+SLIP)
                base=MINA+(MAXA-MINA)*((sc-BUY)/(1-BUY))
                va=max(0.5,min(1.5,0.02/ap))
                al2=min(nv*base*va,av)
                sh=int(al2/ep)
                if sh<=0: continue
                co=ep*sh
                if co>av: sh=int(av/ep); co=ep*sh
                if sh<=0: continue
                hh=sd[sym]['h'][max(0,ci-21):ci+1]; hl=sd[sym]['l'][max(0,ci-21):ci+1]; hc=sd[sym]['c'][max(0,ci-21):ci+1]
                cs=ch_stop(hh,hl,hc); sl=cs if cs else ep*0.92
                pos[sym]={'ep':ep,'sh':sh,'sl':sl,'ed':date,'sc':sc}
                cap-=co; av-=co
    
    if di % 100 == 0: print(f"  Day {di}/{len(all_d)}: NAV={nv:,.0f} positions={len(pos)} trades={len(trades)}", flush=True)

# 清倉
for s,p in list(pos.items()):
    if s in sd:
        lp=sd[s]['c'][-1]*(1-SLIP); pnl=(lp-p['ep'])*p['sh']; cap+=lp*p['sh']
        trades.append({'s':s,'ed':p['ed'],'xd':sd[s]['d'][-1],'ep':p['ep'],'xp':round(lp,4),'pn':round(pnl,2),'pc':round((lp/p['ep']-1)*100,2),'r':'結束','sc':p['sc']})

# ========== 結果 ==========
fin=cap; pk=0; mdd=0
for d in nav:
    if d['n']>pk: pk=d['n']
    dd=(pk-d['n'])/pk*100 if pk>0 else 0
    if dd>mdd: mdd=dd

w=[t for t in trades if t['pn']>0]; l=[t for t in trades if t['pn']<=0]
wr=len(w)/len(trades)*100 if trades else 0
aw=sum(t['pc'] for t in w)/len(w) if w else 0
al=sum(t['pc'] for t in l)/len(l) if l else 0
dr=[(nav[i]['n']/nav[i-1]['n']-1) for i in range(1,len(nav))]
yrs=len(all_d)/252
cagr=((fin/INIT)**(1/yrs)-1)*100 if yrs>0 else 0
mn=sum(dr)/len(dr)
sd_r=(sum((r-mn)**2 for r in dr)/len(dr))**.5
sh=mn/sd_r*(252**.5) if sd_r>0 else 0
ddr=[r for r in dr if r<0]
so=mn/(sum((r-mn)**2 for r in ddr)/len(ddr))**.5*(252**.5) if ddr else 0
mp=max(d['p'] for d in nav); ap=sum(d['p'] for d in nav)/len(nav)
cl=0;mcl=0
for t in trades:
    if t['pn']<=0: cl+=1; mcl=max(mcl,cl)
    else: cl=0

ys=defaultdict(lambda:{'c':0,'p':0,'w':0})
for t in trades:
    y=t['xd'][:4]; ys[y]['c']+=1; ys[y]['p']+=t['pn']
    if t['pn']>0: ys[y]['w']+=1

rs=defaultdict(lambda:{'c':0,'p':0,'w':0})
for t in trades:
    rs[t['r']]['c']+=1; rs[t['r']]['p']+=t['pn']
    if t['pn']>0: rs[t['r']]['w']+=1

print("\n"+"="*80)
print("📊 組合回測結果（無倉位上限+動態倉位+0.2%滑點+每5日掃描）")
print("="*80)
print(f"股票池: {len(syms)}隻 | {all_d[0]}~{all_d[-1]} ({len(all_d)}天)")
print(f"${INIT:,.0f} → ${fin:,.0f} | 總回報 {(fin/INIT-1)*100:.1f}%")
print(f"CAGR: {cagr:.1f}% | Sharpe: {sh:.2f} | Sortino: {so:.2f}")
print(f"MaxDD: {mdd:.1f}% | Calmar: {cagr/mdd:.2f}" if mdd>0 else f"MaxDD: {mdd:.1f}%")
print(f"最大連續虧損: {mcl}筆")
print(f"交易: {len(trades)}筆 | 勝率: {wr:.1f}% | 盈+{aw:.1f}% 虧{al:.1f}% | 盈虧比 {abs(aw/al):.1f}:1" if al!=0 else "")
print(f"期望值: {(wr/100*aw+(1-wr/100)*al):.2f}%/筆")
print(f"最大倉位: {mp}隻 | 平均: {ap:.1f}隻")
print(f"\n按年份:")
for y in sorted(ys):
    s=ys[y]; print(f"  {y}: {s['c']}筆 勝率{s['w']/s['c']*100:.0f}% P&L ${s['p']:,.0f}")
print(f"\n按出場:")
for r in sorted(rs,key=lambda x:-rs[x]['c']):
    s=rs[r]; print(f"  {r}: {s['c']}筆 勝率{s['w']/s['c']*100:.0f}% P&L ${s['p']:,.0f}")

with open('/tmp/portfolio_bt_v4.json','w',encoding='utf-8') as f:
    json.dump({'summary':{'init':INIT,'final':round(fin,2),'ret':round((fin/INIT-1)*100,2),'cagr':round(cagr,2),'sharpe':round(sh,2),'sortino':round(so,2),'dd':round(mdd,2),'calmar':round(cagr/mdd,2) if mdd>0 else 0,'trades':len(trades),'wr':round(wr,1),'mp':mp,'ap':round(ap,1),'mcl':mcl},'trades':trades,'nav':nav,'years':{y:{'c':s['c'],'p':round(s['p'],2),'wr':round(s['w']/s['c']*100,1)} for y,s in ys.items()}},f,ensure_ascii=False,indent=2,default=str)
print("\n已存 /tmp/portfolio_bt_v4.json", flush=True)
