"""ELLIOTT WAVE MINI V0.4 | Public Bithumb data, research only.
Standalone: python elliott_wave_mini.py ; existing Flask: register_elliott(app).
"""
import os, time, json, math, csv, io, threading, statistics
from datetime import datetime, timezone, timedelta
import requests, sys
from urllib.parse import urlencode, parse_qs
from uuid import uuid4
from bisect import bisect_right
from wsgiref.simple_server import make_server, WSGIServer
from socketserver import ThreadingMixIn

VERSION='ELLIOTT WAVE MINI V0.4'
BASE='/elliott-mini'
KST=timezone(timedelta(hours=9))
API='https://api.bithumb.com'
LOCAL=threading.local()
LOCK=threading.Lock(); RATE=threading.Lock(); LAST=0.; CACHE={}; CACHE_LOCK=threading.Lock()
STATE={'running':False,'mode':'','done':0,'total':0,'results':[],'trades':[],'errors':[],'started':'','ended':'','fee':0.04,'message':'검색 대기','stats':{}}

def stamp(t=None): return datetime.fromtimestamp(time.time() if t is None else t,KST).strftime('%m-%d %H:%M')
def pct(a,b): return (b/a-1)*100 if a else 0.
def sma(rows,n): return statistics.fmean(x[4] for x in rows[-n:]) if len(rows)>=n else None
def atr(rows,n=14):
    return statistics.fmean(max(b[2]-b[3],abs(b[2]-a[4]),abs(b[3]-a[4])) for a,b in zip(rows[-n-1:-1],rows[-n:])) if len(rows)>n else 0.
def get(path, params=None):
    global LAST
    err=None
    for attempt in range(2):
        try:
            remaining=getattr(LOCAL,'deadline',float('inf'))-time.monotonic()
            if remaining<=0: raise TimeoutError('조회 제한시간 초과')
            with RATE:
                time.sleep(max(0,.13-(time.monotonic()-LAST))); LAST=time.monotonic()
            r=requests.get(API+path,params=params,timeout=(min(3,max(.2,remaining)),min(5,max(.2,remaining))),headers={'Accept':'application/json','User-Agent':'bithumb-btc-mtf-personal-v1/1.0'})
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            err=exc
            if attempt==0: time.sleep(.3)
    raise RuntimeError(f'{path}: {type(err).__name__} {str(err)[:130]}')

def markets(min_eok,limit,progress=None):
    # Same v1 endpoints as existing MTF scanner, not legacy ALL_KRW.
    data=get('/v1/market/all',{'isDetails':'false'})
    if not isinstance(data,list): raise ValueError('종목 목록 응답 오류')
    names=sorted({x['market'] for x in data if isinstance(x,dict) and str(x.get('market','')).startswith('KRW-')})
    if not names: raise ValueError('KRW 목록 없음')
    ticker={}; failed=[]
    for i in range(0,len(names),50):
        if progress: progress(f'거래대금 조회 {min(i+50,len(names))}/{len(names)}')
        try:
            batch=get('/v1/ticker',{'markets':','.join(names[i:i+50])})
            if not isinstance(batch,list): raise ValueError('거래대금 응답 오류')
            ticker.update({x['market']:x for x in batch if isinstance(x,dict) and x.get('market')})
        except Exception as e: failed.append(str(e))
    if failed: raise RuntimeError('거래대금 일부 조회 실패: '+failed[0])
    if any(m not in ticker for m in names): raise RuntimeError('거래대금 응답 누락')
    out=[]
    for m in names:
        try:
            turnover=float(ticker[m]['acc_trade_price_24h'])
            if math.isfinite(turnover) and turnover>=min_eok*1e8: out.append((m[4:],turnover))
        except (ValueError,KeyError,TypeError): continue
    return sorted(out,key=lambda x:(-x[1],x[0]))[:limit or None]

class DataGap(ValueError): pass

def parse_start(v):
    raw=v.get('candle_date_time_kst')
    zone=KST
    if not raw:
        raw=v['candle_date_time_utc']; zone=timezone.utc
    d=datetime.fromisoformat(str(raw).replace('Z','+00:00'))
    if d.tzinfo is None: d=d.replace(tzinfo=zone)
    return int(d.timestamp())

def candles(symbol, unit, count, asof):
    step=unit*60
    boundary=int(asof//step)*step
    key=(symbol,unit,count,boundary)
    with CACHE_LOCK:
        if key in CACHE: return CACHE[key]
    rows={}; cursor=boundary
    for _ in range(math.ceil(count/200)+3):
        q={'market':'KRW-'+symbol,'count':min(200,max(1,count-len(rows))), 'to':datetime.fromtimestamp(cursor,KST).strftime('%Y-%m-%d %H:%M:%S')}
        data=get('/v1/candles/minutes/'+str(unit),q)
        if not isinstance(data,list): raise RuntimeError(f'{unit}분봉 API 응답 오류: {str(data)[:100]}')
        if not data: break
        oldest=None
        for v in data:
            try:
                t=parse_start(v)
                oldest=t if oldest is None else min(oldest,t)
                vals=tuple(float(v[k]) for k in ('opening_price','high_price','low_price','trade_price'))
                vol=float(v.get('candle_acc_trade_volume') or 0)
                if t<cursor and t+step<=asof and min(vals)>0 and all(map(math.isfinite,vals+(vol,))) and vals[1]>=max(vals[0],vals[3]) and vals[2]<=min(vals[0],vals[3]):
                    rows[t]=(t,*vals,vol)
            except (ValueError,TypeError,KeyError,OverflowError): continue
        if len(rows)>=count: break
        if oldest is None: raise RuntimeError(f'{unit}분봉 시각 파싱 실패')
        if oldest>=cursor: raise RuntimeError(f'{unit}분봉 페이지 시각 역행 실패')
        cursor=oldest
    result=sorted(rows.values())[-count:]
    with CACHE_LOCK:
        if len(CACHE)>1200: CACHE.clear()
        CACHE[key]=result
    return result

def aggregate(rows, src, dst):
    step=dst*60; groups={}
    for r in sorted({x[0]:x for x in rows}.values()):
        t=((r[0]+9*3600)//step)*step-9*3600
        groups.setdefault(t,[]).append(r)
    out=[]
    for t,g in sorted(groups.items()):
        if [r[0] for r in g]!=list(range(t,t+step,src*60)): continue
        out.append((t,g[0][1],max(r[2] for r in g),min(r[3] for r in g),g[-1][4],sum(r[5] for r in g)))
    return out

def contiguous(rows,unit,n):
    if len(rows)<n: return False
    tail=rows[-n:]
    return all(b[0]-a[0]==unit*60 for a,b in zip(tail,tail[1:]))

def history_ok(rows,unit,n,recent):
    return len(rows)>=n and contiguous(rows,unit,recent) and rows[-1][0]-rows[-n][0] <= (n-1)*unit*60*1.25

def fresh(rows,unit,asof):
    if not rows: return False
    step=unit*60
    return 0<=asof-(rows[-1][0]+step)<step

def coverage(rows,unit,asof):
    latest=stamp(rows[-1][0]+unit*60) if rows else '없음'
    return f'{unit}분봉 {len(rows)}개 / 마지막 마감 {latest} / 기준 {stamp(asof)}'

def get120(symbol,asof):
    reasons=[]
    for unit,count in ((60,200),(30,400)):
        try:
            raw=candles(symbol,unit,count,asof); rows=aggregate(raw,unit,120)
            if history_ok(rows,120,63,5) and fresh(rows,120,asof): return rows
            reasons.append(f'{coverage(raw,unit,asof)} → {coverage(rows,120,asof)}')
            if len(rows)>=63 and fresh(rows,120,asof): break
        except Exception as exc: reasons.append(f'{unit}분: {type(exc).__name__} {str(exc)[:120]}')
    if reasons and all('→' not in x for x in reasons): raise RuntimeError('120분 수집 실패: '+' / '.join(reasons))
    raise DataGap('120분봉 자료 부족: '+' / '.join(reasons))

def pivots(rows,wing=2):
    # A pivot is usable only after wing completed candles on the right.
    out=[]
    for i in range(wing,len(rows)-wing):
        r=rows[i]; near=rows[i-wing:i]+rows[i+1:i+wing+1]
        hi=all(r[2]>x[2] for x in near); lo=all(r[3]<x[3] for x in near)
        if hi==lo: continue
        p=('H' if hi else 'L',i,r[2] if hi else r[3])
        if out and out[-1][0]==p[0]:
            if (hi and p[2]>out[-1][2]) or (lo and p[2]<out[-1][2]): out[-1]=p
        else: out.append(p)
    return out

def rsi(rows,n=9):
    if len(rows)<=n: return 50.
    dif=[b[4]-a[4] for a,b in zip(rows,rows[1:])][-n:]
    up=sum(max(0,x) for x in dif); dn=sum(max(0,-x) for x in dif)
    return 100*up/(up+dn) if up+dn else 50.
def ema(vals,n):
    a=2/(n+1); y=vals[0]; out=[]
    for x in vals: y+=a*(x-y); out.append(y)
    return out
def macd(rows):
    v=[r[4] for r in rows]; m=[a-b for a,b in zip(ema(v,12),ema(v,26))]; s=ema(m,9)
    return m[-1]-s[-1],m[-2]-s[-2]
def bullish(r): return r[4]>r[1]
def support120(rows):
    if not history_ok(rows,120,63,5): return False
    ma=sma(rows,60); old=sma(rows[:-3],60); a=atr(rows)
    if not a or not ma: return False
    recent=rows[-5:]; x=rows[-1]
    touch=any(r[3]<=ma+.45*a and r[2]>=ma-.45*a for r in recent)
    star=len(rows)>3 and rows[-3][4]<rows[-3][1] and abs(rows[-2][4]-rows[-2][1])<=abs(rows[-3][4]-rows[-3][1])*.65 and bullish(x) and x[4]>=(rows[-3][1]+rows[-3][4])/2
    reversal=(bullish(x) and x[4]>rows[-2][4]) or star
    return ma>=old and touch and x[4]>=ma and reversal

def structure15(rows):
    if not history_ok(rows,15,243,55): return None
    ma=sma(rows,240); old=sma(rows[:-3],240); ma20=sma(rows,20); a=atr(rows)
    if not a or ma<old or rows[-1][4]<ma-.3*a: return None
    recent=rows[-55:]; p=pivots(recent); hit=None
    for i in range(3,len(p)):
        h,l,b,c=p[i-3:i+1]
        if [q[0] for q in (h,l,b,c)]!=['H','L','H','L']: continue
        if b[2]>=h[2] or c[2]>l[2]+.35*a or h[2]-c[2]<1.2*a: continue
        if len(recent)-1-c[1]>20: continue
        if min(r[3] for r in recent[c[1]+1:])<c[2]: continue
        kind='ABC'
        if i>=5:
            six=p[i-5:i+1]
            if [q[0] for q in six]==['H','L','H','L','H','L'] and six[0][2]>six[2][2]>six[4][2] and six[1][2]>six[3][2]>six[5][2]: kind='하락 5파'
        hit={'type':kind,'low':c[2],'time':recent[c[1]][0],'ma20':ma20}
    return hit

def signals(rows, setup):
    if not setup or not contiguous(rows,1,60): return []
    a=atr(rows); last=rows[-1]; prev=rows[-2]; price=last[4]; ma20=sma(rows,20)
    if not a or not bullish(last) or price<ma20 or price<=prev[4]: return []
    rr=rows[-100:]; p=pivots(rr); out=[]
    # Anchor to a recent confirmed low following or within the 15m C candle.
    for i,origin in enumerate(p):
        if origin[0]!='L' or rr[origin[1]][0]<setup['time'] or price<=origin[2]: continue
        if min(x[3] for x in rr[origin[1]+1:])<origin[2]: continue
        seq=p[i:]; typ=None; level=0.; stop=0.; label=''
        # A: two lows (second higher or near equal), breakout of middle high now.
        if len(seq)==3 and [q[0] for q in seq]==['L','H','L']:
            l,h,d=seq
            if d[2]>=l[2] and d[2]-l[2]<=1.2*a and h[2]-l[2]>=.8*a and prev[4]<=h[2]<price:
                typ='A'; level=h[2]; stop=min(l[2],d[2]); label='쌍바닥 고점 돌파'
        # B: first rise, correction makes higher low, reclaim 20MA and local high.
        if len(seq)==3 and [q[0] for q in seq]==['L','H','L']:
            l,h,d=seq; span=h[2]-l[2]; retr=(h[2]-d[2])/span if span>0 else 0
            trigger=max(x[2] for x in rr[max(d[1]+1,len(rr)-4):-1]) if d[1]+1<len(rr)-1 else prev[2]
            if span>=1.5*a and .25<=retr<=.75 and d[2]>l[2] and prev[4]<=trigger<price and price<=h[2]+.25*a:
                out.append(make_signal('B',price,d[2],trigger,'2파 눌림 후 재상승',rows,setup))
        # C: higher highs/lows, fourth wave must stay above first-wave high (normal impulse).
        if len(seq)==5 and [q[0] for q in seq]==['L','H','L','H','L']:
            l,h,d,h3,d4=seq; span=h3[2]-d[2]; retr=(h3[2]-d4[2])/span if span>0 else 0
            trigger=max(x[2] for x in rr[max(d4[1]+1,len(rr)-4):-1]) if d4[1]+1<len(rr)-1 else prev[2]
            if h3[2]>h[2] and d[2]>l[2] and d4[2]>h[2] and .2<=retr<=.65 and span>=1.5*a and prev[4]<=trigger<price and price<=h3[2]+.25*a:
                typ='C'; level=trigger; stop=d4[2]; label='4파 눌림 후 재상승'
        if typ: out.append(make_signal(typ,price,stop,level,label,rows,setup))
        break  # use earliest surviving C-linked low; do not relabel wave 4 as wave 2
    # Select one result per pattern, preferring latest anchor / tightest stop.
    good={}
    for x in out:
        if x and (x['type'] not in good or x['stop']>good[x['type']]['stop']): good[x['type']]=x
    return list(good.values())

def make_signal(typ,price,low,level,label,rows,setup):
    a=atr(rows); stop=low-.1*a; risk=-pct(price,stop)
    if not .25<=risk<=3 or price>level+.8*a: return None
    hist,prev=macd(rows); rv=rsi(rows); vol=rows[-1][5]/max(1e-20,statistics.fmean(x[5] for x in rows[-21:-1]))
    aux=[rv>=45 and rv<=75,hist>=prev,vol>=1.2]
    return {'type':typ,'price':price,'stop':stop,'risk':risk,'time':rows[-1][0]+60,'reason':label,'setup':setup['type'],'aux':sum(aux),'rsi':rv,'macd_up':aux[1],'volume':vol}

def exit_warning(rows):
    if len(rows)<30: return False
    x=rows[-1]; p=rows[-2]; body=abs(x[4]-x[1]); wick=x[2]-max(x[1],x[4]); span=x[2]-x[3]; a=atr(rows)
    engulf=p[4]>p[1] and x[4]<x[1] and x[1]>=p[4] and x[4]<=p[1]
    rejection=span>0 and wick>=max(2*body,.45*span) and x[4]<x[1] and span>=.8*a
    pp=[q for q in pivots(rows[-45:]) if q[0]=='H']; div=False
    if len(pp)>=2:
        h1,h2=pp[-2:]; base=max(0,len(rows)-45)
        div=h2[2]>h1[2] and rsi(rows[:base+h2[1]+1])<rsi(rows[:base+h1[1]+1])-4
    vol=x[5]>=1.5*statistics.fmean(r[5] for r in rows[-21:-1]); near=x[2]>=max(r[2] for r in rows[-12:])-.3*a
    return (engulf or rejection) and (near or div or vol)

def simulate(rows,i,sig,fee,hold=60):
    # Entry only at next minute open. Conservative stop-first on ambiguous candles.
    if i+1>=len(rows) or rows[i+1][0]!=rows[i][0]+60: return None
    entry=rows[i+1][1]; stop=sig['stop']; risk=-pct(entry,stop)
    if not .25<=risk<=3 or abs(pct(sig['price'],entry))>.5: return None
    target=entry+2*(entry-stop); lastidx=min(len(rows)-1,i+hold); exitp=None; why=''; end=None
    for j in range(i+1,lastidx+1):
        x=rows[j]
        if x[0]!=rows[j-1][0]+60: return None
        if x[1]<=stop: exitp=x[1]; why='손절 갭'
        elif x[3]<=stop: exitp=stop; why='손절'
        elif x[1]>=target: exitp=x[1]; why='익절 갭'
        elif x[2]>=target: exitp=target; why='2R 익절'
        elif j<lastidx and j+1<len(rows):
            r1=rows[max(0,j-100):j+1]; r5=aggregate(r1,1,5)
            if exit_warning(r1) and r5 and r5[-1][0]+300<=x[0]+60 and r5[-1][4]<r5[-1][1]:
                if rows[j+1][0]!=x[0]+60: return None
                exitp=rows[j+1][1]; why='반전 경고'; j+=1; x=rows[j]
        if exitp is not None: end=x[0] if why in ('손절 갭','익절 갭','반전 경고') else x[0]+60; break
    if exitp is None:
        if i+hold>=len(rows): return None
        end=rows[lastidx][0]+60; exitp=rows[lastidx][4]; why='60분 종료'
    net=(exitp*(1-fee/100)/(entry*(1+fee/100))-1)*100
    return dict(sig,entry=entry,exit=exitp,net=net,why=why,end=end)

def analyze(symbol,turnover,mode,asof,fee,hours):
    r120=get120(symbol,asof)
    if mode=='live' and not support120(r120): return [],[],''
    r15=candles(symbol,15,300,asof)
    if not history_ok(r15,15,243,55) or not fresh(r15,15,asof): raise DataGap('15분봉 자료 부족: '+coverage(r15,15,asof))
    setup=structure15(r15)
    if mode=='live' and not setup: return [],[],''
    r1=candles(symbol,1,200 if mode=='live' else hours*60+180,asof)
    if not contiguous(r1,1,60) or not fresh(r1,1,asof): raise DataGap('1분봉 자료 부족: '+coverage(r1,1,asof))
    if mode=='live':
        items=signals(r1,setup); return [dict(x,symbol=symbol,turnover=turnover) for x in items],[],''
    ts120=[r[0]+7200 for r in r120]; ts15=[r[0]+900 for r in r15]
    trades=[]; cooldown={}; context={}; cutoff=asof-hours*3600
    for i in range(60,len(r1)):
        t=r1[i][0]+60
        if t<cutoff: continue
        k=bisect_right(ts120,t); l=bisect_right(ts15,t)
        if k==0 or l==0 or t-ts120[k-1]>=7200 or t-ts15[l-1]>=900: continue
        key=(k,l)
        if key not in context: context[key]=structure15(r15[:l]) if support120(r120[:k]) else None
        if not context[key]: continue
        for sig in signals(r1[max(0,i-119):i+1],context[key]):
            typ=sig['type']
            if t<=cooldown.get(typ,0): continue
            result=simulate(r1,i,sig,fee)
            if result:
                trades.append(dict(result,symbol=symbol)); cooldown[typ]=result['end']+60
            elif i+60>=len(r1): cooldown[typ]=t+3600
    return [],trades,''

def summarize(trades):
    out={}
    for k in 'ABC':
        vals=[x['net'] for x in trades if x['type']==k]; n=len(vals)
        out[k]={'n':n,'wins':sum(x>0 for x in vals),'losses':sum(x<0 for x in vals),'avg':sum(vals)/n if n else None}
    return out

JOB={}
def complete_locked():
    STATE['results'].sort(key=lambda x:(x['type']!='B',-x['aux'],-x['turnover']))
    STATE['trades'].sort(key=lambda x:x['time'],reverse=True)
    STATE.update(running=False,busy=False,ended=stamp(),message=('완료' if STATE['total']>len(STATE['errors']) else ('대상 없음' if not STATE['total'] else '판정 가능한 데이터 없음')))
    print(f"[ELLIOTT V0.4] done={STATE['done']} total={STATE['total']} errors={len(STATE['errors'])}",flush=True)

def start(args):
    try:
        mode=args.get('mode','live'); limit=int(args.get('limit',30)); min_eok=float(args.get('min_eok',1)); fee=float(args.get('fee',.04)); hours=int(args.get('hours',3))
        if mode not in ('live','test') or limit not in (10,30,50,100,0) or not 0<=min_eok<=10000 or not 0<=fee<=2 or hours not in (3,6) or (mode=='test' and limit not in (10,30)): raise ValueError()
    except (TypeError,ValueError): return {'ok':False,'message':'설정값 확인'},400
    with LOCK:
        if STATE['running']: return {'ok':False,'message':'이미 검색 중'},409
        token=uuid4().hex
        STATE.update(running=True,busy=True,job_id=token,mode=mode,done=0,total=0,results=[],trades=[],errors=[],started=stamp(),ended='',fee=fee,message='KRW 종목 목록 조회',stats={})
        JOB.clear()
    try:
        LOCAL.deadline=time.monotonic()+35
        print('[ELLIOTT V0.4] request market list start',flush=True)
        def progress(msg):
            with LOCK: STATE['message']=msg
        symbols=markets(min_eok,limit,progress)
        with LOCK:
            JOB.update(symbols=symbols,asof=time.time()-2,mode=mode,fee=fee,hours=hours)
            STATE.update(total=len(symbols),busy=False,message='차트 검색 중')
            if not symbols: complete_locked()
        print(f'[ELLIOTT V0.4] request market list OK {len(symbols)}',flush=True)
        return {'ok':True},200
    except Exception as e:
        with LOCK: STATE.update(running=False,busy=False,ended=stamp(),message='검색 실패: '+str(e))
        print(f'[ELLIOTT V0.4] market list failed: {e}',flush=True)
        return {'ok':False,'message':str(e)},400
    finally:
        LOCAL.deadline=float('inf')

def step(args):
    # One symbol per actual HTTP request; no detached worker, no queued futures.
    with LOCK:
        if not STATE.get('running') or STATE.get('busy') or args.get('job_id')!=STATE.get('job_id') or not JOB:
            return {'ok':True},200
        idx=STATE['done']
        if idx>=STATE['total']:
            complete_locked(); return {'ok':True},200
        sym,turnover=JOB['symbols'][idx]
        mode,asof,fee,hours=(JOB[k] for k in ('mode','asof','fee','hours'))
        STATE.update(busy=True,message=f'{sym} 차트 조회')
    results=[]; trades=[]; err=None
    try:
        LOCAL.deadline=time.monotonic()+22
        results,trades,_=analyze(sym,turnover,mode,asof,fee,hours)
    except Exception as e:
        err={'symbol':sym,'kind':'자료부족' if isinstance(e,DataGap) else 'API오류','error':str(e)}
    finally:
        LOCAL.deadline=float('inf')
        with LOCK:
            STATE['done']+=1; STATE['results'].extend(results); STATE['trades'].extend(trades)
            if err:
                STATE['errors'].append(err)
                if len(STATE['errors'])<=5: print(f'[ELLIOTT V0.4] {err}',flush=True)
            STATE['stats']=summarize(STATE['trades'])
            STATE.update(busy=False,message='차트 검색 중')
            if STATE['done']>=STATE['total']: complete_locked()
    return {'ok':True},200

def state():
    with LOCK: return json.loads(json.dumps(STATE,ensure_ascii=False))

def export():
    s=state(); rows=s['trades'] if s['mode']=='test' else s['results']; fields=['symbol','type','time','setup','reason','price','stop','risk','aux','rsi','macd_up','volume','entry','exit','net','why','end']
    f=io.StringIO(); w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore'); w.writeheader()
    for r in rows: w.writerow(dict(r,time=stamp(r['time']),end=stamp(r['end']) if 'end' in r else ''))
    return '\ufeff'+f.getvalue(), 'ELLIOTT_WAVE_MINI_V0_4_'+datetime.now(KST).strftime('%Y%m%d%H%M')+'.csv'

HTML=r'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>ELLIOTT WAVE MINI V0.4</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f5f8;color:#182637;font:15px system-ui,-apple-system,sans-serif}main{max-width:760px;margin:auto;padding:17px}h1{font-size:23px;margin:0 0 6px}p{margin:6px 0;line-height:1.5}.muted,small{color:#637183;font-size:12px}.panel{background:white;border:1px solid #e0e5ec;border-radius:15px;padding:15px;margin:13px 0}.controls{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0}label{font-size:12px;color:#536175}select,input,button{font:inherit;border:1px solid #d5dce5;border-radius:8px;padding:10px;width:100%;background:white;color:#182637}label select,label input{display:block;margin-top:5px}button{cursor:pointer;font-weight:700}button.primary{background:#1b5e52;color:white;border-color:#1b5e52}button:disabled{opacity:.5;cursor:default}.two{display:grid;grid-template-columns:1fr 1fr;gap:8px}.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}.tile{padding:10px;background:#f3f5f8;border-radius:9px}.big{font-size:20px;font-weight:750}.good{color:#087350}.bad{color:#b52d42}h2{font-size:17px;margin:4px 0 12px}.candidate{border-top:1px solid #e6eaf0;padding:11px 0}.line{display:flex;justify-content:space-between;align-items:center;gap:8px}.pill{background:#e5f1eb;color:#126247;border-radius:8px;padding:4px 8px;font-size:12px;font-weight:700}.pricegrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:10px 0}.pricegrid strong{display:block;font-size:17px}.progress{height:6px;background:#e3e8ef;border-radius:9px;margin:10px 0;overflow:hidden}.progress>div{height:100%;background:#1b5e52;width:0%}a{color:#236959;text-decoration:none}details{margin-top:10px}summary{cursor:pointer;color:#617184;font-size:13px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;padding:9px 5px;border-bottom:1px solid #e6eaf0}td:last-child,th:last-child{text-align:right}.scroll{overflow-x:auto}.empty{padding:18px 3px;color:#657386;text-align:center}footer{padding:6px 0 20px}.warn{color:#94621b;font-size:12px}@media(max-width:390px){main{padding:12px}.controls{gap:5px}select,input{padding:8px 4px}h1{font-size:21px}.tile{padding:8px}}
</style></head><body><main><h1>ELLIOTT WAVE MINI V0.4</h1><p class="muted">빗썸 KRW · 파동 매매 연구 전용 · 자동매매 없음</p><section class="panel"><div class="controls"><label>검색 범위<select id="limit"><option value="10">상위 10</option><option value="30" selected>상위 30</option><option value="50">상위 50</option><option value="100">상위 100</option><option value="0">전체 KRW</option></select></label><label>거래대금(억)<input id="min" type="number" value="1" min="0" step="0.1"></label><label>편도 수수료 %<input id="fee" type="number" value="0.04" min="0" max="2" step="0.01"></label></div><div class="two"><button class="primary" id="live" onclick="start('live')">실시간 검색</button><button id="test" onclick="start('test')">최근 검증</button></div><details><summary>검증 설정 / 검색 기준</summary><label>검증 기간<select id="hours"><option value="3">최근 3시간</option><option value="6">최근 6시간</option></select></label><p class="muted">검증은 상위 10 또는 30개만 지원. 마감봉 기준, 다음 1분봉 시가 진입, 구조적 손절 / 2R 익절 / 반전 경고 / 최대 60분. 미완료 거래 제외, 실제 체결 및 슬리피지 미반영. 현재 거래대금 순위로 과거 검증하므로 표본 편향이 있음.</p><p class="muted">120분봉 60선 지지 및 반전 → 15분봉 ABC/하락 5파 추정 및 240선 상승 (이평선은 실제 관측봉, 최근 구조는 연속봉 기준) → 1분봉 20선 회복 및 A/B/C 돌파. RSI·MACD·거래량은 보조 표시만.</p></details><p id="note" class="warn"></p><div class="progress"><div id="bar"></div></div><div class="line muted"><span id="status">검색 대기</span><span id="time"></span></div></section><section class="panel" id="results"><div class="line"><h2 id="heading">매수 후보</h2><a href="__BASE__/api/csv" id="download" hidden>CSV 저장</a></div><div id="body" class="empty">검색을 시작해 주세요.</div><details id="errs" hidden><summary id="errlabel">제외 / 오류</summary><div id="errbody" class="muted"></div></details></section><footer class="muted">A 첫 돌파 · B 2파 눌림 · C 4파 눌림<br>검색 화면을 열어 두세요. 화면이 닫히면 일시 중지되고 다시 열면 이어집니다.<br>파동은 기계적 추정이며 매수·수익을 보장하지 않습니다. 검색 결과는 가장 최근 마감봉의 신호만 표시합니다.</footer></main><script>
const base='__BASE__';const $=id=>document.getElementById(id);const esc=x=>String(x??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));const num=x=>Number(x).toLocaleString('ko-KR',{maximumSignificantDigits:8});const pp=x=>x==null?'—':`${x>=0?'+':''}${Number(x).toFixed(2)}%`;const tm=t=>new Date(t*1000).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});const labels={A:'첫 돌파',B:'2파 눌림',C:'4파 눌림'};let starting=false,advancing=false,refreshing=false,lastBodyKey="",lastErrorKey="";
async function start(mode){if(starting)return;starting=true;$('live').disabled=$('test').disabled=true;$('status').textContent='KRW 종목 목록 조회';$('note').textContent='';let lim=+$('limit').value;if(mode==='test'&&![10,30].includes(lim)){$('limit').value='30';lim=30;$('note').textContent='경량 검증을 위해 상위 30개로 설정했습니다.'}try{const r=await fetch(base+'/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,limit:lim,min_eok:+$('min').value,fee:+$('fee').value,hours:+$('hours').value})});const j=await r.json();if(!r.ok)$('note').textContent=j.message;await refresh()}catch(e){$('note').textContent='서버 연결 오류'}finally{starting=false;refresh()}}
function candidate(x){return `<article class="candidate"><div class="line"><strong>${esc(x.symbol)}</strong><span class="pill">${esc(x.type)}형 · ${labels[x.type]}</span></div><div class="pricegrid"><div><small>신호 가격</small><strong>${num(x.price)}</strong></div><div><small>구조적 손절</small><strong class="bad">${num(x.stop)}</strong></div></div><p>${esc(x.setup)} · ${esc(x.reason)}</p><small>보조 ${x.aux}/3 · 손절폭 ${x.risk.toFixed(2)}% · ${tm(x.time)}</small><details><summary>RSI / MACD / 거래량</summary><p class="muted">RSI ${x.rsi.toFixed(1)} · MACD ${x.macd_up?'개선':'둔화'} · 거래량 ${x.volume.toFixed(2)}배</p></details></article>`}
function backtest(s){let a=s.stats||{};let h='<div class="summary">'+['A','B','C'].map(k=>{let x=a[k]||{n:0,wins:0,losses:0,avg:null};return `<div class="tile"><small>${k}형 ${labels[k]}</small><div class="big">${x.n}건</div><small>${x.wins}승 ${x.losses}패</small><div class="${x.avg>=0?'good':'bad'}">${pp(x.avg)}</div><small>평균 순손익</small></div>`}).join('')+'</div>';if(!s.trades.length)return h+'<p class="empty">완료된 검증 거래 없음</p>';h+='<details><summary>최근 거래 내역</summary><div class="scroll"><table><tr><th>종목</th><th>형</th><th>진입</th><th>청산</th><th>순손익</th></tr>'+s.trades.slice(0,30).map(x=>`<tr><td>${esc(x.symbol)}</td><td>${esc(x.type)}</td><td>${tm(x.time)}</td><td>${esc(x.why)}</td><td class="${x.net>=0?'good':'bad'}">${pp(x.net)}</td></tr>`).join('')+'</table></div><p class="muted">최근 30건만 표시 · CSV에는 전체 완료 거래 저장</p></details>';return h}
async function refresh(){if(refreshing)return;refreshing=true;try{let r=await fetch(base+'/api/state',{cache:'no-store'});if(!r.ok)throw Error();let s=await r.json();$('live').disabled=$('test').disabled=s.running;$('status').textContent=`${s.message} ${s.total?s.done+'/'+s.total:''}${s.total?' · 정상 '+(s.done-s.errors.length):''}${s.errors.length?' · 부족 '+s.errors.filter(x=>x.kind==='자료부족').length+' / 오류 '+s.errors.filter(x=>x.kind!=='자료부족').length:''}`;$('time').textContent=s.ended||s.started;$('bar').style.width=(s.total?100*s.done/s.total:0)+'%';$('download').hidden=!s.mode||s.running;$('heading').textContent=s.mode==='test'?'검증 결과':'매수 후보 '+(s.results.length?s.results.length+'건':'');const bodyKey=JSON.stringify([s.mode,s.running,s.results,s.trades,s.stats,s.done,s.errors.length,s.message]);if(bodyKey!==lastBodyKey){lastBodyKey=bodyKey;$('body').className='';$('body').innerHTML=s.mode==='test'?backtest(s):s.results.length?s.results.map(candidate).join(''):`<div class="empty">${s.running?esc(s.message)+'…':s.message.startsWith('검색 실패')?esc(s.message):s.mode?(s.done>0&&s.done===s.errors.length?'데이터 부족/오류로 판정 불가':'조건을 충족한 후보 없음'):'검색을 시작해 주세요.'}</div>`;}$('errs').hidden=!s.errors.length;$('errlabel').textContent='제외 / 오류 '+s.errors.length+'건';if(s.running&&!s.busy&&s.job_id&&!advancing)advance(s.job_id);const ek=JSON.stringify(s.errors);if(ek!==lastErrorKey){lastErrorKey=ek;$('errbody').innerHTML=s.errors.map(x=>`<p>${esc(x.symbol)} · ${esc(x.kind||'오류')} · ${esc(x.error)}</p>`).join('')}}catch(e){$('status').textContent='서버 연결 확인 필요'}finally{refreshing=false}}
async function advance(job){if(advancing)return;advancing=true;try{const r=await fetch(base+'/api/step',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:job})});if(!r.ok)throw Error();}catch(e){$('note').textContent='연결 지연 · 자동 재시도'}finally{advancing=false;refresh()}}
refresh();setInterval(refresh,1000);
</script></body></html>'''

def page(): return HTML.replace('__BASE__',BASE)
def startup_probe():
    asof=time.time()-2
    try:
        LOCAL.deadline=time.monotonic()+35
        symbols=markets(1,10)
        print(f'[ELLIOTT V0.4 PROBE] market list {len(symbols)} OK',flush=True)
        LOCAL.deadline=time.monotonic()+22
        r=get120('BTC',asof)
        print('[ELLIOTT V0.4 PROBE] BTC '+coverage(r,120,asof),flush=True)
        for unit,n in ((15,300),(1,200)):
            rr=candles('BTC',unit,n,asof)
            print('[ELLIOTT V0.4 PROBE] BTC '+coverage(rr,unit,asof)+f' fresh={fresh(rr,unit,asof)}',flush=True)
    except Exception as e: print(f'[ELLIOTT V0.4 PROBE] {type(e).__name__}: {e}',flush=True)

def register_elliott(app):
    """Add isolated routes to an existing Flask app. Call before app.run()."""
    from flask import Response, request, jsonify
    @app.get(BASE,endpoint='elliott_mini_page')
    def elliott_page(): return Response(page(),mimetype='text/html')
    @app.get(BASE+'/api/state',endpoint='elliott_mini_state')
    def elliott_state(): return jsonify(state())
    @app.post(BASE+'/api/start',endpoint='elliott_mini_start')
    def elliott_start():
        body=request.get_json(silent=True); result,code=start(body if isinstance(body,dict) else {}); return jsonify(result),code
    @app.post(BASE+'/api/step',endpoint='elliott_mini_step')
    def elliott_step():
        body=request.get_json(silent=True); result,code=step(body if isinstance(body,dict) else {}); return jsonify(result),code
    @app.get(BASE+'/api/csv',endpoint='elliott_mini_csv')
    def elliott_csv():
        data,name=export(); return Response(data,mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename="{name}"'})
    return app

def application(env, respond):
    path=env.get('PATH_INFO',''); method=env.get('REQUEST_METHOD','GET'); code=200; headers=[]
    if path in ('/',BASE,BASE+'/') and method=='GET': body=page(); kind='text/html'
    elif path==BASE+'/api/state' and method=='GET': body=json.dumps(state(),ensure_ascii=False); kind='application/json'
    elif path in (BASE+'/api/start',BASE+'/api/step') and method=='POST':
        try:
            n=int(env.get('CONTENT_LENGTH','0') or 0)
            if not 0<n<=8192: raise ValueError()
            args=json.loads(env['wsgi.input'].read(n));
            if not isinstance(args,dict): raise ValueError()
            result,code=step(args) if path.endswith('/step') else start(args)
        except Exception: result,code={'ok':False,'message':'잘못된 요청'},400
        body=json.dumps(result,ensure_ascii=False); kind='application/json'
    elif path==BASE+'/api/csv' and method=='GET':
        body,name=export(); kind='text/csv'; headers=[('Content-Disposition',f'attachment; filename="{name}"')]
    else: body='Not found'; code=404; kind='text/plain'
    data=body.encode('utf-8'); respond(f'{code} '+{200:'OK',400:'Bad Request',404:'Not Found',409:'Conflict'}[code],[('Content-Type',kind+'; charset=utf-8'),('Content-Length',str(len(data))),('Cache-Control','no-store')]+headers); return [data]

class ThreadedServer(ThreadingMixIn, WSGIServer): daemon_threads=True
if __name__=='__main__':
    port=int(os.environ.get('PORT','10000'))
    with make_server('0.0.0.0',port,application,server_class=ThreadedServer) as server:
        print(f'{VERSION} running on {port}',flush=True); server.serve_forever()
