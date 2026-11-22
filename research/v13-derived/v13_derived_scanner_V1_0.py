#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V13-derived KRW read-only scanner. Python 3.10+, standard library only."""
import argparse, bisect, csv, io, json, math, os, re, statistics, threading, time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError
VERSION='1.0'
BASE='https://api.bithumb.com/v1'
KST=timezone(timedelta(hours=9)); DAY=86400000; MIN_LIQ=500_000_000
FEE=.0025
STABLES=set('USDT USDC DAI TUSD FDUSD USD1 PYUSD USDE USDD RLUSD EURC EURI'.split())
RULES={'BOX':'박스 돌파 후 눌림','BRK':'추세 고점 돌파','PB':'추세 돌파 후 눌림','DIP':'과매도 반등'}
LOCK=threading.Lock(); RATE=threading.Lock(); NEXT=0.; BLOCK=0.; CACHE={}; JOB={'state':'idle'}; RESULTS={}
class DataError(Exception): pass

def stamp(t): return datetime.fromtimestamp(t/1000,KST).strftime('%Y-%m-%d %H:%M')
def get(path,params=None):
    global NEXT,BLOCK
    with RATE:
        if time.monotonic()<BLOCK: raise DataError('API 제한 중: 추가 요청 중단')
        time.sleep(max(0,NEXT-time.monotonic())); NEXT=time.monotonic()+.45
        try:
            with urlopen(Request(BASE+path+('?' + urlencode(params) if params else ''),headers={'User-Agent':'KRW-V13-Research/1.0','Accept':'application/json'}),timeout=15) as res:
                obj=json.loads(res.read(4000000))
        except HTTPError as e:
            if e.code in (418,429): BLOCK=time.monotonic()+300; raise DataError('API 제한 HTTP '+str(e.code)+' — 중단')
            raise DataError('HTTP '+str(e.code))
        except Exception as e: raise DataError(str(e)[:200])
    if not isinstance(obj,list): raise DataError('API 응답 형식 오류')
    return obj

def universe(n):
    markets=[x['market'] for x in get('/market/all') if x['market'].startswith('KRW-') and x['market'][4:] not in STABLES]
    ticks=[]
    for i in range(0,len(markets),100): ticks+=get('/ticker',{'markets':','.join(markets[i:i+100])})
    ranked=sorted([(x['market'],float(x.get('acc_trade_price_24h',0))) for x in ticks if x.get('market') in markets],key=lambda x:(-x[1],x[0]))
    return [m for m,v in ranked if v>=MIN_LIQ][:n]

def candles(m,tf,start,end,fetch=get):
    step=tf*60000; key=(m,tf,start,end)
    if fetch is get and key in CACHE and time.monotonic()-CACHE[key][0]<600: return CACHE[key][1]
    out={}; cursor=end
    for _ in range(100):
        if cursor<=start: break
        page=fetch('/candles/minutes/'+str(tf),{'market':m,'count':200,'to':stamp(cursor).replace(' ','T')+':00'})
        if not page: break
        times=[]
        for x in page:
            try:
                t=int(datetime.fromisoformat(x['candle_date_time_utc']).replace(tzinfo=timezone.utc).timestamp()*1000); times.append(t)
                o,h,l,c,v,q=[float(x[k]) for k in ('opening_price','high_price','low_price','trade_price','candle_acc_trade_volume','candle_acc_trade_price')]
                if start<=t and t+step<=end and all(map(math.isfinite,(o,h,l,c,v,q))) and 0<l<=min(o,c)<=max(o,c)<=h and min(v,q)>=0: out[t]=(t,o,h,l,c,v,q)
            except (ValueError,KeyError,TypeError,OverflowError): continue
        if not times or min(times)>=cursor: raise DataError('캔들 페이지 진행 실패')
        cursor=min(times)
    else: raise DataError('페이지 제한 초과')
    rows=sorted(out.values())
    if not rows: raise DataError('완료봉 없음')
    if fetch is get:
        if len(CACHE)>80: CACHE.clear()
        CACHE[key]=(time.monotonic(),rows)
    return rows

def ema(v,n):
    out=[]; x=v[0]; a=2/(n+1)
    for p in v: x+=a*(p-x); out.append(x)
    return out

class Series:
    def __init__(self,rows,tf):
        self.r=rows; self.tf=tf; self.step=tf*60000; self.t=[r[0]+self.step for r in rows]; self.idx={t:i for i,t in enumerate(self.t)}
        self.o,self.h,self.l,self.c,self.v,self.q=map(list,zip(*(r[1:] for r in rows)))
        self.n={5:240,15:96,60:200,240:50}[tf]; self.ma=ema(self.c,self.n); self.slow=ema(self.c,self.n*4)
        tr=[max(h-l,abs(h-self.c[max(i-1,0)]),abs(l-self.c[max(i-1,0)])) for i,(h,l) in enumerate(zip(self.h,self.l))]
        self.atr=[None if i<13 else sum(tr[i-13:i+1])/14 for i in range(len(tr))]
        self.va=[None if i<20 else sum(self.v[i-20:i])/20 for i in range(len(tr))]
        self.gaps=[0]
        for i in range(1,len(rows)): self.gaps.append(self.gaps[-1]+int(self.t[i]-self.t[i-1]!=self.step))
    def cont(self,i,j): return 0<=i<=j<len(self.t) and self.gaps[i]==self.gaps[j]
    def at(self,t): return self.idx.get(t)
    def up(self,i): return i>=self.n+5 and self.cont(i-self.n-5,i) and self.c[i]>self.ma[i]>self.ma[i-5]
    def rs(self,i):
        n=7*1440//self.tf
        return self.c[i]/self.c[i-n]-1 if self.cont(i-n,i) else None
    def liquid(self,i):
        n=1440//self.tf
        return self.cont(i-n+1,i) and sum(self.q[i-n+1:i+1])>=MIN_LIQ
    def rsi(self,i):
        if not self.cont(i-14,i): return None
        d=[self.c[j]-self.c[j-1] for j in range(i-13,i+1)]; u=sum(max(x,0) for x in d); v=sum(max(-x,0) for x in d)
        return 100*u/(u+v) if u+v else 50

def brk(s,i,box=False):
    n={5:24,15:16,60:12}[s.tf] if box else {60:48,240:20}[s.tf]
    if i<max(n,s.n)+5 or not s.cont(i-max(n,21),i) or not s.up(i): return None
    a=s.atr[i]; va=s.va[i]; bh=max(s.h[i-n:i]); bl=min(s.l[i-n:i])
    if not a or not va or s.c[i]<=bh or s.v[i]<va*(2 if box else 1.5): return None
    if box and (not 1.5<=(bh-bl)/a<=6 or sum(h>=bh*.999 for h in s.h[i-n:i])<2 or s.c[i]-bh>2*a or s.h[i]<=s.l[i] or (s.c[i]-s.o[i])/(s.h[i]-s.l[i])<.6): return None
    return bh

def signal(s,i,rule,flow='off'):
    if not s.up(i) or not s.liquid(i): return None
    a=s.atr[i]
    if not a: return None
    if rule=='BRK':
        bh=brk(s,i); return (bh,s.c[i]-3*a, None,'거래량 1.5배 이상 고점 돌파') if bh else None
    if rule=='DIP':
        rs=[s.rsi(j) for j in range(i-3,i)];
        if not s.cont(i-24,i) or any(x is None for x in rs) or min(rs)>32 or s.c[i]<=max(s.o[i],s.h[i-1]) or max(s.h[i-24:i])-min(s.l[i-3:i+1])<3*a: return None
        return (s.h[i-1],s.c[i]-3*a,None,'상승 EMA 위, RSI 32 이하 조정 후 직전 고점 돌파')
    box=rule=='BOX'; w=6 if box else 8
    for k in range(i-1,max(i-w-1,0),-1):
        bh=brk(s,k,box)
        if bh is None or not s.cont(k,i): continue
        ak=s.atr[k]; first=None
        for j in range(k+1,i+1):
            if s.c[j] < (bh*.998 if box else bh-.5*ak): break
            if s.l[j]<=(bh*1.003 if box else bh+.5*ak) and s.c[j]>bh and s.c[j]>s.o[j]: first=j; break
        if first!=i: continue
        if box and flow!='off':
            maxv,delta=(.7,.3) if flow=='strong' else (1.,0.)
            dr=lambda j:(2*s.c[j]-s.h[j]-s.l[j])/(s.h[j]-s.l[j]) if s.h[j]>s.l[j] else 0
            if statistics.mean(s.v[k+1:i+1])>maxv*s.v[k] or dr(i)<delta or sum(dr(j)*s.v[j] for j in range(k,i+1))<=0: continue
        stop=min(s.l[i],bh)-.5*ak if box else s.c[i]-3*a
        return (bh,stop,1.5 if box else None,'돌파 후 지지선 재확인 및 양봉 마감')
    return None

def pending_retest(s,i,box):
    """A breakout waiting for its first retest; no future candles consulted."""
    w=6 if box else 8
    for k in range(i,max(i-w,-1),-1):
        bh=brk(s,k,box)
        if bh is None or not s.cont(k,i): continue
        a=s.atr[k]; waiting=True
        for j in range(k+1,i+1):
            if s.c[j] < (bh*.998 if box else bh-.5*a) or (s.l[j]<=(bh*1.003 if box else bh+.5*a) and s.c[j]>bh and s.c[j]>s.o[j]):
                waiting=False;break
        if waiting and k+w>i: return bh,k+w-i
    return None

def market_states(b,group,expected=None):
    result={}; cur='mid'; pending=None; count=0; win=30*1440//b.tf
    for i,t in enumerate(b.t):
        valid=[]; rs=[]
        for m,s in group.items():
            j=s.at(t)
            if j is not None and s.cont(j-s.n-5,j):
                valid.append(s.c[j]>s.ma[j]); val=s.rs(j)
                if val is not None: rs.append((m,val))
        minimum=max(3,math.ceil((expected or len(group))*.8)); rank={}
        if len(rs)>=minimum:
            rs.sort(key=lambda x:(-x[1],x[0])); rank={m:(j+1)/len(rs) for j,(m,v) in enumerate(rs)}
        if i<max(b.n*4,win-1) or not b.cont(i-max(b.n*4,win-1),i) or len(valid)<minimum:
            cur='mid'; pending=None; count=0; result[t]=('unknown',None,rank); continue
        dd=b.c[i]/max(b.h[i-win+1:i+1])-1; breadth=sum(valid)/len(valid)
        sb=1 if b.up(i) and b.c[i]>b.slow[i] else -1 if b.c[i]<b.ma[i]<b.ma[i-5] and b.c[i]<b.slow[i] else 0
        score=sb+(1 if breadth>=.6 else -1 if breadth<=.4 else 0)+(1 if dd>=-.1 else -1 if dd<=-.2 else 0)
        raw='bull' if score>=2 else 'bear' if score<=-2 else 'mid'
        if raw==cur: pending=None;count=0
        else:
            count=count+1 if pending==raw else 1;pending=raw
            if count>=6: cur=raw;pending=None;count=0
        result[t]=(cur,{'score':score,'breadth':breadth,'dd':dd,'valid':len(valid)},rank)
    return result

def gate(rule,reg,rank):
    if reg not in ('bull','mid') or rank is None: return False
    return rank<=(.5 if reg=='bull' else .3) and (reg=='bull' or rule!='BRK')

def exit_trade(s,i,rule,plan):
    # decision at i close; execution at next open, no future-bar entry filters other than order risk check
    e=i+1; cap=48 if rule=='BOX' else 24; end=e+cap-1
    if end>=len(s.t) or not s.cont(i,end): return None,'미래 관측 부족(검증 제외)'
    entry=s.o[e]; bh,stop,rmult,_=plan
    if rule!='BOX': stop=entry-3*s.atr[i]
    risk=(entry-stop)/entry; limit=.04 if rule=='BOX' else .06
    if not 0<risk<=limit or stop<=0: return None,'진입가 위험 제한'
    target=entry*(1+1.5*risk) if rule=='BOX' else None
    if target and target/entry-1<.01: return None,'목표폭 부족'
    frac=1.; gross=0.; hh=entry; why='기간만료'
    for j in range(e,end+1):
        if s.l[j]<=stop:
            price=min(s.o[j],stop); why='손절/추적'; break
        if target and frac==1 and s.h[j]>=target:
            gross+=.5*(target/entry-1);frac=.5
        if rule=='BOX':
            if frac==1 and j-e+1>=8: price=s.c[j];why='시간청산';break
            if frac==.5: stop=max(stop,s.l[j])
        else:
            hh=max(hh,s.h[j]);stop=max(stop,hh-3*(s.atr[j] or s.atr[i]))
        price=s.c[j]
    gross+=frac*(price/entry-1);net=(1+gross)*(1-FEE)-(1+FEE)
    return {'entry':stamp(s.t[e]-s.step),'exit':stamp(s.t[j]),'exit_ms':s.t[j],'entry_price':entry,'net_pct':round(net*100,5),'reason':why},None

def run(mode='scan',tf=60,days=6,n=10,coins='',flow='off',progress=lambda x:None, now=None, acquire=candles, markets=universe):
    if tf not in (15,60,240) or days not in (6,13) or not 3<=n<=20 or flow not in ('off','weak','strong'): raise ValueError('설정 범위 오류')
    if coins.strip():
        ms=list(dict.fromkeys('KRW-'+x.strip().upper().removeprefix('KRW-') for x in coins.split(',') if x.strip()))
        if not 3<=len(ms)<=20 or any(not re.fullmatch(r'KRW-[A-Z0-9]{2,15}',m) or m[4:] in STABLES for m in ms): raise ValueError('원화 코인 3~20종 입력')
    else: ms=markets(n)
    if len(ms)<3: raise DataError('시장 폭 계산 대상 3개 미만')
    end=(int(time.time()*1000) if now is None else now)//60000*60000; start=end-days*DAY if mode=='backtest' else end
    warm=start-40*DAY; diag=[]; group={}; step=tf*60000
    for m in dict.fromkeys(['KRW-BTC']+ms):
        progress(f'{m} 완료봉 수집 ({len(diag)+1}/{len(set(ms+["KRW-BTC"]))})')
        try:
            rows=acquire(m,tf,warm,end); s=Series(rows,tf); group[m]=s
            diag.append({'coin':m,'bars':len(rows),'start':stamp(rows[0][0]),'end':stamp(s.t[-1]),'gaps':s.gaps[-1],'expected_bars':(end-warm)//step,'missing_estimate':max(0,(end-warm)//step-len(rows)),'status':'OK' if s.t[-1]>end-step else '최신봉 부족'})
        except DataError as e:
            diag.append({'coin':m,'status':str(e),'bars':0})
            if 'API 제한' in str(e): break
    if 'KRW-BTC' not in group: raise DataError('BTC 수집 실패: '+str(diag))
    cohort={m:group[m] for m in ms if m in group}; b=group['KRW-BTC']
    if len(cohort)<max(3,math.ceil(len(ms)*.8)): raise DataError('시장 폭 수집 부족: '+str(diag))
    progress('완료봉 시장 상태·상대강도 계산'); states=market_states(b,cohort,len(ms)); rules=['BOX'] if tf==15 else ['BRK','PB','DIP']; latest=b.t[-1]; reg,detail,ranks=states[latest]
    result={'version':VERSION,'mode':mode,'tf':tf,'requested':len(ms),'loaded':len(cohort),'until':stamp(latest),'market':reg,'market_detail':detail,'diag':diag,'cohort':ms,'rows':[],'summary':{},'warning':'현재 거래대금 순위/지정 종목 고정 표본. 역사적 전체 시장 재현 아님. 슬리피지 0, 모의 체결.'}
    if mode=='scan':
        for m,s in cohort.items():
            i=s.at(latest); rank=ranks.get(m)
            if i is None or latest<=end-step:
                result['rows'].append({'coin':m,'status':'데이터 부족','reason':'최신 완료봉 누락'});continue
            signals=[]
            for rule in rules:
                p=signal(s,i,rule,flow)
                if p:
                    bh,stop,r,why=p; risk=(s.c[i]-stop)/s.c[i]; limit=.04 if rule=='BOX' else .06
                    eligible=gate(rule,reg,rank) and 0<risk<=limit and stop>0 and (rule!='BOX' or 1.5*risk>=.01)
                    signals.append({'coin':m,'rule':RULES[rule],'status':'진입 후보' if eligible else '검토','price':s.c[i],'stop':stop,'risk_pct':round(risk*100,2),'target':s.c[i]*(1+1.5*risk) if rule=='BOX' else None,'rank_pct':round(rank*100,1) if rank is not None else None,'reason':why+(' · 추세/위험/상대강도 통과' if eligible else ' · 시장/상대강도/위험 조건 미통과')})
            if signals: result['rows']+=signals
            elif s.up(i) and s.liquid(i) and (pending:=pending_retest(s,i,tf==15)):
                bh,left=pending
                result['rows'].append({'coin':m,'rule':RULES['BOX' if tf==15 else 'PB'],'status':'검토','price':s.c[i],'rank_pct':round(rank*100,1) if rank is not None else None,'reason':f'돌파 확인. 지지선 {bh:.8g}원 근처 눌림 및 양봉 대기 ({left}봉 남음). 아직 진입 신호 아님'})
            else: result['rows'].append({'coin':m,'rule':' / '.join(RULES[r] for r in rules),'status':'관망','price':s.c[i],'rank_pct':round(rank*100,1) if rank is not None else None,'reason':'돌파·재테스트·반등 조건 미충족' if s.up(i) else 'EMA 상승추세 미충족'})
        result['rows'].sort(key=lambda r:({'진입 후보':0,'검토':1,'관망':2}.get(r['status'],3),r.get('rank_pct') or 101,r['coin']))
        return result
    progress('과거 신호 및 청산 모의검증'); candidates=[]; censored=Counter()
    for m,s in cohort.items():
        for i,t in enumerate(s.t):
            if t<start or t>=end: continue
            r,_,rank=states.get(t,('unknown',None,{})); rp=rank.get(m)
            for rule in rules:
                if not gate(rule,r,rp): continue
                p=signal(s,i,rule,flow)
                if p: candidates.append((t,rule,m,s,i,p,r,rp))
    count=Counter(); held={}; last={}; rows=[]
    for t,rule,m,s,i,p,r,rp in sorted(candidates,key=lambda x:(x[0],x[1],x[7],x[2])):
        day=stamp(t)[:10]
        if count[(rule,day)]>=4 or held.get((rule,m),0)>t or t-last.get((rule,m),-10**20)<180*60000: continue
        tr,why=exit_trade(s,i,rule,p)
        if tr is None: censored[why]+=1;continue
        count[(rule,day)]+=1;held[(rule,m)]=tr.pop('exit_ms');last[(rule,m)]=t
        rows.append({'coin':m,'rule':RULES[rule],'signal':stamp(t),'market':r,'rank_pct':round(rp*100,1),**tr})
    result['rows']=rows;result['censored']=dict(censored);result['from']=stamp(start)
    for rule in rules:
        a=[x['net_pct'] for x in rows if x['rule']==RULES[rule]];result['summary'][RULES[rule]]={'trades':len(a),'wins':sum(x>0 for x in a),'losses':sum(x<0 for x in a),'flat':sum(x==0 for x in a),'avg_net_pct':round(statistics.mean(a),4) if a else None,'sum_net_pct':round(sum(a),4)}
    result['daily']=[]
    d=datetime.fromtimestamp(start/1000,KST).date(); end_date=datetime.fromtimestamp(end/1000,KST).date()
    while d<=end_date:
        for rule in rules:
            a=[x['net_pct'] for x in rows if x['rule']==RULES[rule] and x['signal'].startswith(d.isoformat())]
            result['daily'].append({'date':d.isoformat(),'rule':RULES[rule],'trades':len(a),'wins':sum(x>0 for x in a),'losses':sum(x<0 for x in a),'net_sum_pct':round(sum(a),4),'avg_net_pct':round(statistics.mean(a),4) if a else None})
        d+=timedelta(days=1)
    return result

HTML='''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>V13 기반 독립 스캐너 V1.0</title><style>body{background:#101a2b;color:#eaf1fc;font:15px system-ui;margin:auto;padding:16px;max-width:1050px}section{background:#192a40;border:1px solid #354b65;border-radius:13px;margin:12px 0;padding:16px}h1{font-size:24px}h2{font-size:18px}label{display:block;margin:10px 0 4px}input,select,button{font:inherit;padding:10px;border:1px solid #56708f;border-radius:8px;background:#0e1c30;color:white;max-width:100%;box-sizing:border-box}input{width:100%}button{background:#286cb1;margin:8px 4px 4px 0;cursor:pointer}button:disabled{opacity:.5}.small{font-size:12px;color:#b5c6dc}.scroll{overflow:auto}table{border-collapse:collapse;font-size:13px;width:100%}td,th{padding:8px;border-bottom:1px solid #3a4e68;text-align:left;white-space:nowrap}.candidate{color:#8de5b0}.watch{color:#ffce86}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px}.coin-card{border:1px solid #48617e;border-radius:11px;padding:12px;background:#101d30}.coin-card p{margin:6px 0;color:#c5d5e9}.coin-card h3{margin:0 0 6px;font-size:17px}a{color:#a0d6ff}</style><h1>빗썸 V13 기반 독립 스캐너 V1.0</h1><p class="small">실제 주문 없음 · 완료봉 전용 · KRW 시장 · 연구 후보는 매수 권유가 아닙니다.</p><section><h2>1. 스캔 설정</h2><label>전략 / 봉</label><select id="tf"><option value="60">1시간 추세추종</option><option value="15">15분 박스 눌림목</option><option value="240">4시간 추세추종</option></select><label>거래대금 상위 종목 수</label><select id="n"><option>10</option><option>5</option><option>20</option></select><label>고정 종목 (선택, 3~20개 쉼표 구분)</label><input id="coins" placeholder="BTC,ETH,SOL,XRP,SUI"><label>백테스트 기간</label><select id="days"><option>6</option><option>13</option></select><label>눌림목 거래량/델타 근사 필터</label><select id="flow"><option value="off">끄기</option><option value="weak">약하게</option><option value="strong">강하게</option></select><p class="small">편도 0.25% · 약세/판정불가 진입 보류 · 하루 전략별 최대 4건(검증) · 최초 조회는 40일 지표 준비 구간으로 느릴 수 있음.</p><button id="scan">현재 스캔</button><button id="bt">백테스트</button><p id="state">대기</p></section><section><h2>2. 시장 상태</h2><div id="market">스캔 전</div></section><section><h2>3. 분석 결과</h2><div id="summary"></div><div class="scroll" id="rows"></div><details><summary>날짜별 성과 (백테스트)</summary><div class="scroll" id="daily"></div></details><p><a href="/api/csv">결과 CSV</a> · <a href="/api/diagnostics">진단 CSV</a></p></section><section><h2>4. 데이터 상태</h2><div id="diag" class="scroll"></div><p class="small">고정 종목 / 현재 거래대금 상위 종목의 과거 검증이므로 생존·선택 편향이 있습니다. 손절과 목표가가 같은 봉에 닿으면 손절 우선. 가상 체결과 실제 체결은 다를 수 있습니다.</p></section><script>const $=x=>document.getElementById(x);function table(el,rows){el.replaceChildren();if(!rows.length){el.textContent='결과 없음';return}let t=document.createElement('table'),keys=[...new Set(rows.flatMap(Object.keys))];let h=t.insertRow();for(let k of keys){let c=document.createElement('th');c.textContent=k;h.append(c)}for(let r of rows){let rr=t.insertRow();for(let k of keys){let c=rr.insertCell(),v=r[k];c.textContent=typeof v==='number'?Number(v.toPrecision(8)).toLocaleString('ko-KR'):v??'—';if(v==='진입 후보')c.className='candidate';if(v==='검토')c.className='watch'}}el.append(t)}function cards(el,rows){el.replaceChildren();let grid=document.createElement('div');grid.className='cards';for(let r of rows){let card=document.createElement('article');card.className='coin-card';let title=document.createElement('h3');title.textContent=r.coin+' · '+r.status;title.className=r.status==='진입 후보'?'candidate':r.status==='검토'?'watch':'';card.append(title);for(let v of [r.rule,r.reason,r.price!=null?'완료봉 종가 '+Number(r.price.toPrecision(8)).toLocaleString('ko-KR')+'원':null,r.rank_pct!=null?'상대강도 상위 '+r.rank_pct+'%':null,r.stop!=null?'참고 손절 '+Number(r.stop.toPrecision(8)).toLocaleString('ko-KR')+'원 · 위험 '+r.risk_pct+'%':null,r.target!=null?'1차 목표 '+Number(r.target.toPrecision(8)).toLocaleString('ko-KR')+'원 (50%)':null]){if(v){let p=document.createElement('p');p.textContent=v;card.append(p)}}grid.append(card)}el.append(grid);if(!rows.length)el.textContent='결과 없음'}let timer;async function poll(){try{let j=await(await fetch('/api/status',{cache:'no-store'})).json();$('state').textContent=j.message||j.state;if(j.state==='running'){timer=setTimeout(poll,1500);return}$('scan').disabled=$('bt').disabled=false;if(j.state==='done'){let r=j.result,d=r.market_detail;$('market').textContent=({bull:'강세',mid:'중립',bear:'약세',unknown:'판단 불가'}[r.market])+' / '+r.until+' KST / '+r.loaded+'/'+r.requested+' 종목'+(d?' / 추세선 위 '+(d.breadth*100).toFixed(1)+'% / BTC 낙폭 '+(d.dd*100).toFixed(1)+'%':'');table($('summary'),Object.entries(r.summary).map(([rule,v])=>({rule,...v})));if(r.mode==='scan')cards($('rows'),r.rows);else table($('rows'),r.rows.slice(0,200));table($('daily'),r.daily||[]);table($('diag'),r.diag);$('state').textContent='완료 · '+r.warning+(r.censored?' · 제외 '+JSON.stringify(r.censored):'')}}catch(e){$('state').textContent='연결 오류: '+e.message;$('scan').disabled=$('bt').disabled=false}}async function launch(mode){clearTimeout(timer);$('scan').disabled=$('bt').disabled=true;$('rows').replaceChildren();$('summary').replaceChildren();$('diag').replaceChildren();$('daily').replaceChildren();$('market').textContent='계산 중';try{let r=await(await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,tf:+$('tf').value,days:+$('days').value,n:+$('n').value,coins:$('coins').value,flow:$('flow').value})})).json();if(r.error)throw Error(r.error);poll()}catch(e){$('state').textContent=e.message;$('scan').disabled=$('bt').disabled=false}}$('scan').onclick=()=>launch('scan');$('bt').onclick=()=>launch('backtest');poll();</script></html>'''

def make_csv(rows):
    f=io.StringIO();keys=list(dict.fromkeys(k for r in rows for k in r)) or ['status'];w=csv.DictWriter(f,keys);w.writeheader();w.writerows(rows);return '\ufeff'+f.getvalue()
class Handler(BaseHTTPRequestHandler):
    def send(self,body,status=200,ctype='application/json',filename=None):
        if not isinstance(body,str): body=json.dumps(body,ensure_ascii=False,allow_nan=False)
        b=body.encode();self.send_response(status);self.send_header('Content-Type',ctype+'; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        if filename:self.send_header('Content-Disposition','attachment; filename="'+filename+'"')
        self.end_headers();self.wfile.write(b)
    def do_GET(self):
        p=urlsplit(self.path).path
        if p=='/':return self.send(HTML,ctype='text/html')
        if p=='/health':return self.send({'version':VERSION,'status':'ok'})
        if p=='/api/status':return self.send(dict(JOB))
        if p in ('/api/csv','/api/diagnostics'):
            r=RESULTS.get('last')
            if not r:return self.send({'error':'완료 결과 없음'},404)
            return self.send(make_csv(r['rows' if p=='/api/csv' else 'diag']),ctype='text/csv',filename='BITHUMB_V13_DERIVED_V1_0_'+('RESULT' if p=='/api/csv' else 'DIAGNOSTICS')+'.csv')
        self.send({'error':'not found'},404)
    def do_POST(self):
        if urlsplit(self.path).path!='/api/run':return self.send({'error':'not found'},404)
        try:
            k=int(self.headers.get('Content-Length','0'));assert 0<k<=4096
            d=json.loads(self.rfile.read(k));mode=d['mode'];assert mode in ('scan','backtest'); tf=int(d['tf']);days=int(d['days']);n=int(d['n']);coins=str(d.get('coins',''));flow=str(d.get('flow','off'))
            if not LOCK.acquire(False):return self.send({'error':'이미 연구 진행 중'},409)
        except Exception:return self.send({'error':'설정 오류'},400)
        JOB.clear();JOB.update(state='running',message='시작');RESULTS.clear()
        def work():
            try:
                r=run(mode,tf,days,n,coins,flow,lambda x:JOB.update(message=x));RESULTS['last']=r;JOB.update(state='done',message='완료',result=r)
            except Exception as e:JOB.update(state='error',message=str(e))
            finally:LOCK.release()
        threading.Thread(target=work,daemon=True).start();self.send({'ok':True})
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=int(os.environ.get('PORT',8080)));a=p.parse_args();print(f'V13 기반 V{VERSION}: http://{a.host}:{a.port}',flush=True);ThreadingHTTPServer((a.host,a.port),Handler).serve_forever()
