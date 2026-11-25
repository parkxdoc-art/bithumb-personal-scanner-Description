import os, json, csv, io, time, math, threading, statistics, traceback, zipfile
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "REVERSE SWING V0.8"
UTC = timezone.utc
KST = timezone(timedelta(hours=9))
DAY = 86400
BASE = "https://api.bithumb.com/v1/"
STABLE = {"USDT","USDC","DAI","TUSD","FDUSD","USDP","PYUSD","USDE","USD1","USDS","FRAX","BUSD","GUSD","EURC","RLUSD","USDG"}
rate_lock = threading.Lock()
next_request = 0.0
state_lock = threading.Lock()
state = {"status":"idle","phase":"대기","done":0,"total":0,"rank":[],"features":[],"summary":[],"errors":[],"coverage":[]}
def now(): return datetime.now(UTC)
def iso(t): return datetime.fromtimestamp(t,KST).strftime("%Y-%m-%d %H:%M")
def day_start(t,anchor=0): return ((t-anchor)//DAY)*DAY+anchor
def kststr(t): return datetime.fromtimestamp(t,KST).strftime("%Y-%m-%dT%H:%M:%S")
def api(path, **params):
    global next_request
    for attempt in range(3):
        with rate_lock:
            wait=max(0,next_request-time.monotonic())
            if wait: time.sleep(wait)
            next_request=time.monotonic()+0.16
        try:
            u=BASE+path+"?"+urlencode(params)
            with urlopen(Request(u,headers={"Accept":"application/json","User-Agent":"ReverseMini/0.1.3"}),timeout=12) as r:
                result=json.load(r)
            if not isinstance(result,list): raise ValueError(str(result)[:200])
            return result
        except Exception as e:
            if attempt==2: raise RuntimeError(path+" "+str(e)) from e
            time.sleep(0.8*(2**attempt))
def parse(row):
    t=int(datetime.fromisoformat(row["candle_date_time_utc"]).replace(tzinfo=UTC).timestamp())
    return (t,float(row["opening_price"]),float(row["high_price"]),float(row["low_price"]),float(row["trade_price"]),float(row["candle_acc_trade_volume"]),float(row["candle_acc_trade_price"]))
def candles(m, unit, end, count=200, pages=1, latest=False):
    out={}
    path="candles/days" if unit==1440 else "candles/minutes/"+str(unit)
    cursor=end
    for page in range(pages):
        params={"market":m,"count":min(200,count)}
        if not (latest and page==0): params["to"]=kststr(cursor)
        raw=api(path,**params)
        if not raw: break
        batch=[parse(r) for r in raw]
        for r in batch:
            if r[0]<end: out[r[0]]=r
        oldest=min(r[0] for r in batch)
        if oldest>=cursor: raise RuntimeError("페이지 시간 진행 오류")
        cursor=oldest
        if len(raw)<min(count,200): break
    return sorted(out.values())
def mean(a): return statistics.fmean(a)
def pct(a,b): return 100*(a/b-1) if b>0 else None
def ema(a,n):
    if len(a)<n: return None
    v=mean(a[:n]); k=2/(n+1)
    for x in a[n:]: v+=k*(x-v)
    return v
def rsi(a,n=14):
    if len(a)<n+1:return None
    diff=[b-a for a,b in zip(a,a[1:])]
    up=mean([max(0,x) for x in diff[:n]]); dn=mean([max(0,-x) for x in diff[:n]])
    for d in diff[n:]:
        up=(up*(n-1)+max(0,d))/n; dn=(dn*(n-1)+max(0,-d))/n
    return 100-100/(1+up/dn) if dn else (100 if up else 50)
def ratio(a,b): return a/b if b>0 else None
def macd(a):
    if len(a)<35:return None,None
    vals=[ema(a[:i],12)-ema(a[:i],26) for i in range(26,len(a)+1)]
    s=ema(vals,9)
    return vals[-1],vals[-1]-s
def features(rows,unit,cut,btc=None):
    rows=[r for r in rows if r[0]+unit*60<=cut]
    f={"bars":len(rows),"last_bar":iso(rows[-1][0]+unit*60) if rows else None}
    if rows:
        f["lag_minutes"]=(cut-rows[-1][0]-unit*60)/60
        f["gaps_last20"]=sum(max(0,(b[0]-a[0])//(unit*60)-1) for a,b in zip(rows[-21:],rows[-20:]))
    # Missing recent intervals are not treated as a flat or zero-volume series.
    if len(rows)<21 or cut-(rows[-1][0]+unit*60)>=unit*60 or any(b[0]-a[0]!=unit*60 for a,b in zip(rows[-21:],rows[-20:])):
        f["valid"]=False; return f
    f["valid"]=True
    c=[r[4] for r in rows]; vol=[r[5] for r in rows]; val=[r[6] for r in rows]
    r=rows[-1]; spread=r[2]-r[3]
    f.update(price=c[-1],ret_1=pct(c[-1],c[-2]),ret_5=pct(c[-1],c[-6]),ret_20=pct(c[-1],c[-21]),vol_ratio=ratio(vol[-1],mean(vol[-21:-1])),value_ratio=ratio(val[-1],mean(val[-21:-1])),vol_3_ratio=ratio(mean(vol[-3:]),mean(vol[-21:-3])),rsi=rsi(c),rsi_delta=rsi(c)-rsi(c[:-1]),body_pct=pct(r[4],r[1]),upper_wick_ratio=ratio(r[2]-max(r[1],r[4]),spread),lower_wick_ratio=ratio(min(r[1],r[4])-r[3],spread),range_20_pct=pct(max(x[2] for x in rows[-20:]),min(x[3] for x in rows[-20:])),value_20=sum(val[-20:]))
    for n in (5,10,20,60,120,200):
        f["ma"+str(n)+"_distance"]=pct(c[-1],mean(c[-n:])) if len(c)>=n else None
    m,h=macd(c); f["macd_pct"]=100*m/c[-1] if m is not None else None; f["macd_hist_pct"]=100*h/c[-1] if h is not None else None
    _,ph=macd(c[:-1])
    width=lambda a: 400*statistics.pstdev(a)/mean(a)
    bb=width(c[-20:]); previous=width(c[-40:-20]) if len(c)>=40 else None
    f["bb_width_pct"]=bb; f["bb_width_ratio"]=ratio(bb,previous) if previous is not None else None
    tr=[max(b[2]-b[3],abs(b[2]-a[4]),abs(b[3]-a[4])) for a,b in zip(rows,rows[1:])]
    f["atr_pct"]=100*mean(tr[-14:])/c[-1]
    f["higher_low"]=min(x[3] for x in rows[-3:])>min(x[3] for x in rows[-6:-3])
    f["breakout_20"]=c[-1]>max(x[2] for x in rows[-21:-1])
    f["volume_2x"]=f["vol_ratio"]>=2 if f["vol_ratio"] is not None else None
    f["value_2x"]=f["value_ratio"]>=2 if f["value_ratio"] is not None else None
    f["volume_3bar_15x"]=f["vol_3_ratio"]>=1.5 if f["vol_3_ratio"] is not None else None
    f["rsi_rising"]=f["rsi_delta"]>0
    f["rsi_above_50"]=f["rsi"]>=50
    f["ma5_above_ma20"]=mean(c[-5:])>mean(c[-20:])
    f["ma_cluster_1pct"]=max(mean(c[-n:]) for n in (5,10,20))/min(mean(c[-n:]) for n in (5,10,20))<=1.01
    f["bb_squeeze"]=f["bb_width_ratio"]<=0.75 if f["bb_width_ratio"] is not None else None
    f["macd_bull_cross"]=ph<=0<h if ph is not None and h is not None else None
    f["macd_positive"]=h>0 if h is not None else None
    f["positive_5bar"]=f["ret_5"]>0
    f["negative_5bar"]=f["ret_5"]<0
    f["negative_20bar"]=f["ret_20"]<0
    f["rsi_below_40"]=f["rsi"]<40
    f["rsi_below_30"]=f["rsi"]<30
    f["bull_candle"]=r[4]>r[1]
    f["lower_wick_40pct"]=f["lower_wick_ratio"]>=.4 if f["lower_wick_ratio"] is not None else None
    if btc:
        bm={x[0]:x[4] for x in btc if x[0]+unit*60<=cut}
        if rows[-1][0] in bm and rows[-6][0] in bm:
            f["btc_relative_5"]=f["ret_5"]-pct(bm[rows[-1][0]],bm[rows[-6][0]])
            f["btc_outperform"]=f["btc_relative_5"]>0
    return f
PATTERNS={"negative_5bar":"5봉 하락","negative_20bar":"20봉 하락","rsi_below_40":"RSI 40 미만","rsi_below_30":"RSI 30 미만","volume_2x":"거래량 2배","value_2x":"거래대금 2배","volume_3bar_15x":"최근 3봉 거래량 1.5배","higher_low":"저점 상승","breakout_20":"20봉 고점 돌파","rsi_rising":"RSI 상승","rsi_above_50":"RSI 50 이상","ma5_above_ma20":"MA5 > MA20","ma_cluster_1pct":"이평 응축 1%","bb_squeeze":"볼린저 폭 축소","macd_bull_cross":"MACD 히스토그램 양전환","macd_positive":"MACD 히스토그램 양수","positive_5bar":"5봉 상승","bull_candle":"양봉","lower_wick_40pct":"아랫꼬리 40%","btc_outperform":"BTC 상대강도 양수"}
WINDOWS=[("7일",7*DAY,1440),("3일",3*DAY,1440),("24시간",DAY,240),("4시간",14400,60),("1시간",3600,15),("30분",1800,5),("15분",900,5),("5분",300,1)]
def patch(**kw):
    with state_lock: state.update(kw)
def record_error(msg):
    with state_lock: state["errors"].append(str(msg))
def advance():
    with state_lock: state["done"]+=1
def parallel(items, fn):
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs={pool.submit(fn,x):x for x in items}
        for future in as_completed(jobs):
            item=jobs[future]
            try: yield item,future.result()
            except Exception as e: record_error(str(item)[:120]+": "+str(e)); yield item,None
            finally: advance()
def rank_days(data, starts, current, threshold):
    result=[]; coverage=[]
    for start in starts:
        candidates=[]
        for m,rows in data.items():
            by={r[0]:r for r in rows}
            r=by.get(start); prior=by.get(start-DAY)
            if not r or not prior or prior[4]<=0: continue
            gain=pct(r[4],prior[4])
            if gain<=0: continue
            hist=sum(x[0]<start for x in rows)
            candidates.append({"market":m,"day":iso(start)[:10],"start":start,"partial":start==current,"gain_pct":gain,"previous_close":prior[4],"day_high_pct":pct(r[2],prior[4]),"day_value":r[6],"short_history":hist<14,"threshold":threshold})
        candidates.sort(key=lambda r:(-r["gain_pct"],r["market"]))
        valid=sum(any(r[0]==start for r in rows) and any(r[0]==start-DAY for r in rows) for rows in data.values())
        coverage.append({"day":iso(start)[:10],"partial":start==current,"valid_markets":valid,"positive_markets":len(candidates),"selected":min(10,len(candidates))})
        for i,r in enumerate(candidates[:10],1): r["rank"]=i; result.append(r)
    return result,coverage
def first_event(rows,start,end,base,threshold):
    target=base*(1+threshold/100)
    for r in rows:
        if r[0]>=start and r[0]+300<=end and r[4]>=target: return r[0]+300
    return None
# Shared BTC history cache, keyed by timeframe and cut rounded down to a candle boundary.
btc_cache={}; btc_lock=threading.Lock()
def btc_history(unit,cut):
    key=(unit,cut)
    with btc_lock:
        if key in btc_cache:return btc_cache[key]
    data=candles("KRW-BTC",unit,cut,41)
    with btc_lock: btc_cache[key]=data
    return data
def analyze(case,all_daily,run_end):
    r=dict(case); m=r["market"]; start=r["start"]; end=min(start+DAY,run_end)
    pre=[]
    for label,delta in (("일 시작 전",0),("일 시작 3일 전",3*DAY),("일 시작 7일 전",7*DAY)):
        cut=start-delta
        item={"market":m,"day":r["day"],"rank":r["rank"],"partial":r["partial"],"short_history":r["short_history"],"event_status":"pre_day","offset":label,"unit_minutes":1440,"cut_kst":iso(cut)}
        item.update(features(all_daily[m],1440,cut,all_daily.get("KRW-BTC")))
        pre.append(item)
    try:
        five=candles(m,5,end,200,2)
        event=first_event(five,start,end,r["previous_close"],r["threshold"])
    except Exception as e:
        r["event_status"]="fetch_error"; record_error(m+" event: "+str(e));return r,pre
    if not event:
        r["event_status"]="no_confirmed_cross" if five else "missing_data"
        return r,pre
    # Check coverage from day start; if missing candles, the first observed crossing is not provably first.
    before=[x for x in five if start<=x[0]<event]
    r["event_expected_bars"]=(event-start)//300
    r["event_observed_bars"]=len(before)
    r["event_missing_bars"]=r["event_expected_bars"]-len(before)
    r["event_coverage_ok"]=r["event_missing_bars"]==0
    r["event_status"]="cross_confirmed" if r["event_coverage_ok"] else "first_observed_cross_gap"
    r["event_kst"]=iso(event)
    r["event_elapsed_min"]=(event-start)//60
    fs=[]
    for label,delta,unit in WINDOWS:
        cut=event-delta
        item={"market":m,"day":r["day"],"rank":r["rank"],"partial":r["partial"],"short_history":r["short_history"],"event_status":r["event_status"],"event_kst":r["event_kst"],"offset":label,"unit_minutes":unit,"cut_kst":iso(cut)}
        try:
            rows=candles(m,unit,cut,200) if unit!=1440 else [x for x in all_daily[m] if x[0]+DAY<=cut]
            if unit==1440 and rows and len(rows)<200:
                try: rows=candles(m,unit,cut,200)
                except Exception as e: record_error(m+" daily history: "+str(e))
            try: btc=btc_history(unit,cut)
            except Exception as e: btc=None; record_error("BTC "+label+": "+str(e))
            item.update(features(rows,unit,cut,btc))
        except Exception as e:
            item["valid"]=False;item["error"]=str(e);record_error(m+" "+label+": "+str(e))
        fs.append(item)
    return r,fs+pre
def summarize(fs):
    out=[]
    for cohort in ("regular","short_history","partial","gap_regular","gap_partial","pre_regular","pre_partial","pre_short_history"):
        windows=WINDOWS+[(label,0,1440) for label in ("일 시작 전","일 시작 3일 전","일 시작 7일 전")]
        for label,_,unit in windows:
            def cohort_of(x):
                base="partial" if x["partial"] else "short_history" if x["short_history"] else "regular"
                status=x.get("event_status")
                if status=="cross_confirmed": return base
                if status=="first_observed_cross_gap": return "gap_partial" if x["partial"] else "gap_regular"
                if status=="pre_day": return "pre_"+base
                return None
            group=[x for x in fs if x["offset"]==label and cohort_of(x)==cohort]
            if (label.startswith("일 시작")) != cohort.startswith("pre_"): continue
            for key,title in PATTERNS.items():
                vals=[x[key] for x in group if x.get("valid") and isinstance(x.get(key),bool)]
                n=len(vals); hit=sum(vals)
                out.append({"cohort":cohort,"offset":label,"unit_minutes":unit,"pattern":key,"name":title,"cases":len(group),"available":n,"hits":hit,"hit_pct":round(100*hit/n,2) if n else None})
            keys=list(PATTERNS)
            for a in range(len(keys)):
                for b in range(a+1,len(keys)):
                    k,l=keys[a],keys[b]
                    vals=[x[k] and x[l] for x in group if x.get("valid") and isinstance(x.get(k),bool) and isinstance(x.get(l),bool)]
                    if vals:
                        out.append({"cohort":cohort,"offset":label,"unit_minutes":unit,"pattern":k+"+"+l,"name":PATTERNS[k]+" + "+PATTERNS[l],"cases":len(group),"available":len(vals),"hits":sum(vals),"hit_pct":round(100*sum(vals)/len(vals),2)})
    return out
def run(days,threshold,include_today):
    try:
        run_end=int(now().timestamp())
        markets=api("market/all")
        universe=sorted({x["market"] for x in markets if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        patch(phase="일별 전체 시장 순위 수집",done=0,total=len(universe),universe=len(universe),run_kst=iso(run_end))
        if not universe: raise RuntimeError("원화 마켓 목록 0개: API 목록 응답 "+str(markets[:2]))
        probe=candles("KRW-BTC",1440,run_end,30,latest=True)
        patch(probe_count=len(probe),probe_latest=iso(probe[-1][0]) if probe else "없음")
        if len(probe)<2: raise RuntimeError("BTC 일봉 부족: "+str([iso(x[0]) for x in probe[-3:]]))
        anchor=probe[-1][0]%DAY
        if any(x[0]%DAY!=anchor for x in probe): raise RuntimeError("BTC 일봉 경계 불일치")
        current=day_start(run_end,anchor)
        if probe[-1][0]!=current: raise RuntimeError("BTC 최신 일봉 지연: "+str([iso(x[0]) for x in probe[-3:]]))
        starts=[current-i*DAY for i in range(days,0,-1)]+([current] if include_today else [])
        patch(day_boundary=iso(current)[11:],probe_latest=iso(probe[-1][0]))
        data={}
        for m,rows in parallel(universe,lambda m:candles(m,1440,run_end,200,latest=True)):
            if rows and all(x[0]%DAY==anchor for x in rows): data[m]=rows
            elif rows: record_error(m+" BTC와 일봉 경계 불일치: "+iso(rows[-1][0]))
            else: record_error(m+" 일봉 응답 없음")
        rank,coverage=rank_days(data,starts,current,threshold)
        for row in coverage: row.update(day_boundary_kst=iso(current)[11:],universe=len(universe),daily_success=len(data),btc_latest=iso(probe[-1][0]))
        patch(rank=rank,coverage=coverage,market_success=len(data),phase="급등 전 완료봉 역추적",done=0,total=len(rank))
        if not any(x["valid_markets"] for x in coverage):
            raise RuntimeError("일별 순위 유효 종목 0개. BTC 최신 일봉: "+str([iso(x[0]) for x in probe[-3:]]))
        results=[]; fs=[]
        for case,res in parallel(rank,lambda x:analyze(x,data,run_end)):
            if res is None:
                results.append(dict(case,event_status="analysis_error"));continue
            r,f=res;results.append(r);fs.extend(f)
        results.sort(key=lambda x:(x["start"],x["rank"]))
        fs.sort(key=lambda x:(x["day"],x["rank"],([a[0] for a in WINDOWS]+["일 시작 전","일 시작 3일 전","일 시작 7일 전"]).index(x["offset"])))
        patch(rank=results,features=fs,summary=summarize(fs),status="done",phase="완료" if any(x.get("valid") for x in fs) else "유효 상세 데이터 없음")
        if not any(x.get("valid") for x in fs): record_error("유효 상세 데이터 0건: 공통점 판단 불가")
    except Exception as e:
        record_error(str(e));patch(status="error",phase="실패")
def start_job(days,threshold,include_today):
    with state_lock:
        if state["status"]=="running":return False
        state.clear();state.update(status="running",phase="준비",done=0,total=0,rank=[],features=[],summary=[],errors=[],coverage=[])
    with btc_lock: btc_cache.clear()
    threading.Thread(target=run,args=(days,threshold,include_today),daemon=True).start()
    return True

# Stage 2: fixed discovery and earlier retrospective comparison; no intraday signals.
RULES2={"BASE":"전체 유효 종목","D20":"20일 하락","D5":"5일 하락","R40":"RSI < 40","D20_R40":"20일 하락 + RSI < 40","D20_D5":"20일·5일 하락","D20_VOLUP":"20일 하락 + 7일 전 대비 거래량 배수 상승","D20_VALUP":"20일 하락 + 7일 전 대비 거래대금 배수 상승","D20_VOL2":"20일 하락 + 거래량 2배","D20_RISE":"20일 하락 + RSI 상승","D20_BTC":"20일 하락 + BTC 상대강도 양수"}
def daily2(rows,start):
    h=[x for x in rows if x[0]+DAY<=start]
    if len(h)<28 or h[-1][0]+DAY!=start or any(b[0]-a[0]!=DAY for a,b in zip(h[-28:],h[-27:])):return None
    close=[x[4] for x in h]; v=[x[5] for x in h]; val=[x[6] for x in h]
    def div(a,b):return a/b if b>0 else None
    vr=div(v[-1],mean(v[-21:-1])); vr7=div(v[-8],mean(v[-28:-8]))
    tr=div(val[-1],mean(val[-21:-1])); tr7=div(val[-8],mean(val[-28:-8]))
    rs=rsi(close); prev=rsi(close[:-1])
    return {"ret20":pct(close[-1],close[-21]),"ret5":pct(close[-1],close[-6]),"rsi":rs,"rsi_delta":rs-prev,"vol_ratio":vr,"vol_ratio7":vr7,"value_ratio":tr,"value_ratio7":tr7,"prior_value":val[-1]}
def rules2(x,btc5):
    d=x["ret20"]<0; d5=x["ret5"]<0; rs=x["rsi"]<40
    vu=x["vol_ratio"] is not None and x["vol_ratio7"] is not None and x["vol_ratio"]>x["vol_ratio7"]
    tu=x["value_ratio"] is not None and x["value_ratio7"] is not None and x["value_ratio"]>x["value_ratio7"]
    return {"BASE":True,"D20":d,"D5":d5,"R40":rs,"D20_R40":d and rs,"D20_D5":d and d5,"D20_VOLUP":d and vu,"D20_VALUP":d and tu,"D20_VOL2":d and x["vol_ratio"] is not None and x["vol_ratio"]>=2,"D20_RISE":d and x["rsi_delta"]>0,"D20_BTC":d and btc5 is not None and x["ret5"]>btc5}
def eval2(data,starts,anchor):
    ranked,cov=rank_days(data,starts,-1,5)
    top={(x["market"],x["start"]) for x in ranked}
    allrows=[]; coverage=[]
    for t,cc in zip(starts,cov):
        label=iso(t)[:10]; group="discovery" if label>="2026-11-11" else "earlier_holdout"
        btc=daily2(data.get("KRW-BTC",[]),t); btc5=btc["ret5"] if btc else None
        batch=[]
        for m,rows in data.items():
            by={x[0]:x for x in rows}; cur=by.get(t); prior=by.get(t-DAY)
            if not cur or not prior or prior[4]<=0:continue
            x=daily2(rows,t)
            if not x or any(x[k] is None for k in ("vol_ratio","vol_ratio7","value_ratio","value_ratio7")) or btc5 is None:continue
            gain=pct(cur[4],prior[4])
            batch.append(dict(market=m,day=label,group=group,gain_pct=gain,top10=(m,t) in top,hit5=gain>=5,hit10=gain>=10,hit20=gain>=20,liquid=x["prior_value"]>=1e8,**x,**rules2(x,btc5)))
        coverage.append(dict(cc,group=group,feature_valid=len(batch),top10_valid=sum(x["top10"] for x in batch),liquid_valid=sum(x["liquid"] for x in batch)))
        allrows.extend(batch)
    summary=[]
    for group in ("discovery","earlier_holdout"):
        for scope in ("all","prior_value_100m"):
            sample=[x for x in allrows if x["group"]==group and (scope=="all" or x["liquid"])]
            base5=sum(x["hit5"] for x in sample)/len(sample) if sample else None
            base10=sum(x["hit10"] for x in sample)/len(sample) if sample else None
            total_top=sum(x["top10"] for x in sample)
            days=len({x["day"] for x in sample})
            for k,title in RULES2.items():
                chosen=[x for x in sample if x[k]]; n=len(chosen)
                h5=sum(x["hit5"] for x in chosen); h10=sum(x["hit10"] for x in chosen); h20=sum(x["hit20"] for x in chosen); ht=sum(x["top10"] for x in chosen)
                summary.append(dict(group=group,scope=scope,rule=k,name=title,valid=len(sample),candidates=n,days=days,per_day=round(n/days,2) if days else None,top10_hits=ht,top10_recall_pct=round(100*ht/total_top,2) if total_top else None,top10_precision_pct=round(100*ht/n,2) if n else None,hit5=h5,hit5_pct=round(100*h5/n,2) if n else None,hit10=h10,hit10_pct=round(100*h10/n,2) if n else None,hit20=h20,hit20_pct=round(100*h20/n,2) if n else None,lift5=round(h5/n/base5,3) if n and base5 else None,lift10=round(h10/n/base10,3) if n and base10 else None,median_daily_change=round(statistics.median(x["gain_pct"] for x in chosen),3) if n else None))
    return allrows,summary,coverage
def run2():
    try:
        end=int(now().timestamp())
        universe=sorted({x["market"] for x in api("market/all") if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        if not universe:raise RuntimeError("원화 마켓 0개")
        probe=candles("KRW-BTC",1440,end,30,latest=True)
        if len(probe)<2:raise RuntimeError("BTC 일봉 부족")
        anchor=probe[-1][0]%DAY
        start0=int(datetime(2026,10,28,12,tzinfo=KST).timestamp())
        starts=[day_start(start0,anchor)+i*DAY for i in range(28)]
        if starts[-1]+DAY>end:raise RuntimeError("완료되지 않은 일봉")
        patch(phase="전체 원화마켓 일봉 수집",done=0,total=len(universe),universe=len(universe))
        data={}
        for m,rows in parallel(universe,lambda m:candles(m,1440,end,200,latest=True)):
            if rows and all(x[0]%DAY==anchor for x in rows):data[m]=rows
            else:record_error(m+" 일봉 누락/경계 불일치")
        patch(phase="전체 종목 조건 비교",market_success=len(data),done=0,total=28)
        rows,summ,cov=eval2(data,starts,anchor)
        if not rows:raise RuntimeError("유효 비교 데이터 0건")
        patch(features=rows,summary=summ,coverage=cov,done=28,status="done",phase="완료")
    except Exception as e:record_error(str(e));patch(status="error",phase="실패")
def start2():
    with state_lock:
        if state["status"]=="running":return False
        state.clear();state.update(mode="stage2",status="running",phase="준비",done=0,total=0,rank=[],features=[],summary=[],errors=[],coverage=[])
    threading.Thread(target=run2,daemon=True).start()
    return True

# V0.3: swing outcomes from daily OHLC only, no intraday execution assumptions.
HORIZONS3=(1,2,3,5,7,10,14)
FEE3=0.0025
DATES3=(("earlier",datetime(2026,10,15,12,tzinfo=KST)),("discovery",datetime(2026,11,11,12,tzinfo=KST)))
daily_cache3={}
def outcome3(by,t,h,end):
    if t+h*DAY>end:return None
    seq=[by.get(t+i*DAY) for i in range(h)]
    if not all(seq):return None
    entry=seq[0][1]
    if entry<=0 or any(r[3]<=0 or r[4]<=0 for r in seq):return None
    high=max(r[2] for r in seq);low=min(r[3] for r in seq);close=seq[-1][4]
    peak=pct(high,entry); trough=pct(low,entry)
    out={"entry_open":entry,"peak_pct":peak,"worst_from_entry_pct":trough,"close_pct":pct(close,entry),"net_close_pct":100*(close/entry*(1-FEE3)/(1+FEE3)-1)}
    for target in (5,10,20):
        out["hit"+str(target)]=peak>=target-1e-10
        out["day_to_"+str(target)]=next((i+1 for i,r in enumerate(seq) if pct(r[2],entry)>=target-1e-10),None)
    out["down5"]=trough<=-5+1e-10
    return out
def eval3(data,dates,end,progress=None):
    indexed={m:{r[0]:r for r in rows} for m,rows in data.items()}
    out=[];coverage=[]
    for idx,(group,t) in enumerate(dates):
        btc=daily2(data.get("KRW-BTC",[]),t)
        counts={h:0 for h in HORIZONS3}; eligible=0; listed=0
        for m,rows in data.items():
            by=indexed[m]
            if t-DAY not in by:continue
            listed+=1
            x=daily2(rows,t)
            if not x or not btc or any(x[k] is None for k in ("vol_ratio","vol_ratio7","value_ratio","value_ratio7")):continue
            eligible+=1
            flags=rules2(x,btc["ret5"])
            for h in HORIZONS3:
                v=outcome3(by,t,h,end)
                if v is None:continue
                counts[h]+=1
                out.append(dict(group=group,day=iso(t)[:10],market=m,horizon=h,liquid=x["prior_value"]>=1e8,**x,**flags,**v))
        for h in HORIZONS3:coverage.append(dict(group=group,day=iso(t)[:10],horizon=h,listed=listed,eligible=eligible,mature=t+h*DAY<=end,valid=counts[h]))
        if progress:progress(idx+1)
    summ=[]
    def med(rows,key):return round(statistics.median(x[key] for x in rows),3) if rows else None
    def rate(rows,key):return round(100*sum(x[key] for x in rows)/len(rows),2) if rows else None
    for group in ("earlier","discovery"):
        for scope in ("all","prior_value_100m"):
            for h in HORIZONS3:
                sample=[x for x in out if x["group"]==group and x["horizon"]==h and (scope=="all" or x["liquid"])]
                days=len({x["day"] for x in sample})
                base=sum(x["hit10"] for x in sample)/len(sample) if sample else 0
                for k,title in RULES2.items():
                    chosen=[x for x in sample if x[k]];n=len(chosen)
                    hits=[x["day_to_10"] for x in chosen if x["day_to_10"] is not None]
                    # Each signal-day is an observation, not an independent trade.
                    summ.append(dict(group=group,scope=scope,horizon=h,rule=k,name=title,days=days,valid=len(sample),candidates=n,unique_markets=len({x["market"] for x in chosen}),per_day=round(n/days,2) if days else None,hit5_pct=rate(chosen,"hit5"),hit10_pct=rate(chosen,"hit10"),hit20_pct=rate(chosen,"hit20"),lift10=round(sum(x["hit10"] for x in chosen)/n/base,3) if n and base else None,median_peak=med(chosen,"peak_pct"),median_worst=med(chosen,"worst_from_entry_pct"),down5_pct=rate(chosen,"down5"),median_net_close=med(chosen,"net_close_pct"),net_positive_pct=round(100*sum(x["net_close_pct"]>0 for x in chosen)/n,2) if n else None,mean_day_to_10=round(statistics.fmean(hits),2) if hits else None))
    return out,summ,coverage
def run3():
    begin=time.monotonic()
    try:
        end=int(now().timestamp())
        universe=sorted({x["market"] for x in api("market/all") if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        if not universe:raise RuntimeError("원화 마켓 0개")
        probe=candles("KRW-BTC",1440,end,30,latest=True)
        if len(probe)<2:raise RuntimeError("BTC 일봉 부족")
        anchor=probe[-1][0]%DAY
        if probe[-1][0]!=day_start(end,anchor):raise RuntimeError("BTC 일봉 지연")
        dates=[(group,day_start(int(base.timestamp()),anchor)+i*DAY) for group,base in DATES3 for i in range(14)]
        patch(phase="전체 일봉 1회 수집",done=0,total=len(universe),universe=len(universe))
        data={}
        cached=daily_cache3.get("data")
        if cached and daily_cache3.get("boundary")==day_start(end,anchor) and daily_cache3.get("universe")==universe and time.monotonic()-daily_cache3["saved"]<900:
            data=cached;patch(done=len(data),phase="일봉 캐시 재사용",cache_used=True)
        else:
            for m,rows in parallel(universe,lambda m:candles(m,1440,end,200,latest=True)):
                if rows and all(x[0]%DAY==anchor for x in rows):data[m]=rows
                else:record_error(m+" 일봉 누락/경계 불일치")
            if len(data)==len(universe):daily_cache3.update(data=data,boundary=day_start(end,anchor),universe=universe,saved=time.monotonic())
        patch(phase="1·2·3·5·7·10·14일 스윙 결과 계산",market_success=len(data),done=0,total=len(dates),fetch_seconds=round(time.monotonic()-begin,1))
        rows,summ,cov=eval3(data,dates,end,lambda i:patch(done=i))
        if not rows:raise RuntimeError("유효 스윙 데이터 0건")
        patch(features=rows,summary=summ,coverage=cov,done=len(dates),status="done",phase="완료",elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:record_error(str(e));patch(status="error",phase="실패",elapsed_seconds=round(time.monotonic()-begin,1))
def start3():
    with state_lock:
        if state["status"]=="running":return False
        state.clear();state.update(mode="stage3",status="running",phase="준비",done=0,total=0,rank=[],features=[],summary=[],errors=[],coverage=[])
    threading.Thread(target=run3,daemon=True).start()
    return True
def detail3(rows,group,horizon,scope,rule,query,sort,page):
    filtered=[x for x in rows if x["group"]==group and x["horizon"]==horizon and (scope=="all" or x["liquid"]) and x.get(rule,False) and query in x["market"]]
    if sort=="peak":filtered.sort(key=lambda x:(-x["peak_pct"],x["day"],x["market"]))
    elif sort=="worst":filtered.sort(key=lambda x:(x["worst_from_entry_pct"],x["day"],x["market"]))
    elif sort=="net":filtered.sort(key=lambda x:(-x["net_close_pct"],x["day"],x["market"]))
    else:filtered.sort(key=lambda x:(x["day"],x["market"]))
    total=len(filtered); pages=max(1,(total+49)//50); page=min(max(page,1),pages)
    return dict(total=total,page=page,pages=pages,rows=filtered[(page-1)*50:page*50])

HTML3=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE SWING V0.3.3</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1100px;margin:auto;padding:16px}button,a,select{display:inline-block;color:white;background:#225b51;padding:10px;margin:4px;border:1px solid #476073;border-radius:7px;text-decoration:none}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}table{border-collapse:collapse;white-space:nowrap}td,th{padding:8px;border-bottom:1px solid #324355;text-align:right}td:first-child,th:first-child{text-align:left}progress{width:100%}.scroll{overflow:auto}pre{white-space:pre-wrap}small{color:#b4c4d1;line-height:1.7}</style><h1>REVERSE SWING V0.3.3</h1><a href="/reverse4">V0.4 급등 전 역추적</a><a href="/">1단계</a><a href="/stage2">2단계</a><section><h2>스윙 전용 전체 시장 백테스트</h2><p>발견 11/11~11/24 · 이전 비교 10/15~10/28 (2026). 일봉만 1회 수집. 1·2·3·5·7·10·14일 결과. 이전 기간은 14일 보유 종료가 발견 기간과 겹치지 않도록 조정. 최근 신호의 10·14일 결과는 기간 미완료 제외. 기간 비교는 이전 비교 구간을 우선 사용.</p><small>신호는 전일까지 완료된 일봉으로만 산출. 다음 일봉 시가 진입 가정. 종료 종가 수익률에 편도 0.25% 반영. 고가 도달은 익절 보장 아님. 저가는 진입가 대비 하락이며 고점 대비 낙폭이 아님. 기간 미완료·결측 제외, 중복 신호 포함. 자동 주문 없음.</small><p><button id="go">스윙 백테스트 시작</button></p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section id="out" hidden><a href="/csv/summary">조건 비교 CSV</a><a href="/csv/features.zip">상세 결과 ZIP (전체)</a><a id="detail" href="/csv/features?horizon=14&amp;group=earlier">선택 기간 상세 CSV</a><a href="/csv/coverage">수집률 CSV</a><a href="/csv/errors">오류 CSV</a><p><select id="group"><option value="earlier">이전 비교 기간</option><option value="discovery">발견 기간</option></select><select id="scope"><option value="all">전체 유효</option><option value="prior_value_100m">전일 거래대금 1억 이상</option></select><select id="horizon"><option>1</option><option>2</option><option>3</option><option>5</option><option>7</option><option>10</option><option selected>14</option></select>일</p><div class="scroll" id="tab"></div><div class="scroll" id="coverage"></div></section><section id="detailbox" hidden><h2>개별 코인 상세 보기</h2><small>위에서 선택한 비교 기간·보유 일수·거래대금 범위를 그대로 적용. 50건씩 표시. 행을 누르면 해당 신호의 전체 수치가 표시됩니다.</small><p><label>조건 <select id="drule"><option value="BASE">전체 유효</option><option value="D20_VOL2" selected>20일 하락 + 거래량 2배</option><option value="D20">20일 하락</option><option value="D5">5일 하락</option><option value="R40">RSI 40 미만</option><option value="D20_R40">20일 하락 + RSI 40 미만</option><option value="D20_D5">20일·5일 하락</option><option value="D20_VOLUP">20일 하락 + 거래량 배수 상승</option><option value="D20_VALUP">20일 하락 + 거래대금 배수 상승</option><option value="D20_RISE">20일 하락 + RSI 상승</option><option value="D20_BTC">20일 하락 + BTC 상대강도</option></select></label><label>정렬 <select id="dsort"><option value="date">날짜</option><option value="peak">최고 상승률 순</option><option value="net">순수익률 순</option><option value="worst">최대 하락 위험 순</option></select></label><label>코인 <input id="dquery" placeholder="ELF, BIGTIME 등" maxlength="40" style="padding:10px;width:140px"></label><button id="dsearch">조회</button></p><p id="dstatus"></p><div class="scroll" id="dtable"></div><button id="dprev">이전</button><button id="dnext">다음</button><div id="drow"></div></section><script>
const $=x=>document.getElementById(x);let result=null;
function table(id,heads,rows){const t=document.createElement('table'),h=t.insertRow();for(let v of heads){const th=document.createElement('th');th.textContent=v;h.append(th)}for(let r of rows){const tr=t.insertRow();for(let v of r)tr.insertCell().textContent=v??'—'}$(id).replaceChildren(t)}
function show(){if(!result)return;const h=Number($('horizon').value);$('detail').href='/csv/features?horizon='+h+'&group='+$('group').value;let rows=result.summary.filter(x=>x.group===$('group').value&&x.scope===$('scope').value&&x.horizon===h);table('tab',['조건','유효 일수','신호건수','일평균','5% 도달%','10% 도달%','20% 도달%','10% 배수','최고 중앙%','최저 중앙%','-5% 하락%','순종가 중앙%','10%까지 일'],rows.map(x=>[x.name,x.days,x.candidates,x.per_day,x.hit5_pct,x.hit10_pct,x.hit20_pct,x.lift10,x.median_peak,x.median_worst,x.down5_pct,x.median_net_close,x.mean_day_to_10]));table('coverage',['날짜','기간완료','조건 유효','결과 유효'],result.coverage.filter(x=>x.group===$('group').value&&x.horizon===h).map(x=>[x.day,x.mature?'완료':'미완료',x.eligible,x.valid]))}

let detailPage=1, detailPages=1, detailSeq=0;
function fmt(v){return v==null?'—':typeof v==='number'?Number(v.toFixed(3)).toString():String(v)}
async function loadDetails(page=1){
 if(!result)return;
 const token=++detailSeq;$('detailbox').hidden=false;$('dstatus').textContent='상세 조회 중';$('drow').replaceChildren();
 const q=new URLSearchParams({group:$('group').value,horizon:$('horizon').value,scope:$('scope').value,rule:$('drule').value,sort:$('dsort').value,query:$('dquery').value,page:String(page)});
 try{
   const r=await fetch('/stage3/details?'+q.toString(),{cache:'no-store'});if(!r.ok)throw Error(await r.text());const d=await r.json();if(token!==detailSeq)return;
   detailPage=d.page;detailPages=d.pages;$('dstatus').textContent=d.total+'건 · '+d.page+'/'+d.pages+' 페이지';
   table('dtable',['날짜','코인','진입가','최고%','최저%','순종가%','10% 도달일','거래량 배수'],d.rows.map(x=>[x.day,x.market,fmt(x.entry_open),fmt(x.peak_pct),fmt(x.worst_from_entry_pct),fmt(x.net_close_pct),fmt(x.day_to_10),fmt(x.vol_ratio)]));
   const trs=$('dtable').querySelectorAll('tr');d.rows.forEach((x,i)=>{trs[i+1].style.cursor='pointer';trs[i+1].onclick=()=>{table('drow',['항목','값'],[['코인',x.market],['신호 날짜',x.day],['보유 일수',x.horizon],['20일 수익률%',fmt(x.ret20)],['5일 수익률%',fmt(x.ret5)],['RSI',fmt(x.rsi)],['RSI 변화',fmt(x.rsi_delta)],['거래량 배수',fmt(x.vol_ratio)],['거래대금 배수',fmt(x.value_ratio)],['진입가',fmt(x.entry_open)],['최고 상승%',fmt(x.peak_pct)],['최대 하락%',fmt(x.worst_from_entry_pct)],['순종가 수익률%',fmt(x.net_close_pct)],['5% 도달 일수',fmt(x.day_to_5)],['10% 도달 일수',fmt(x.day_to_10)],['20% 도달 일수',fmt(x.day_to_20)]]);$('drow').scrollIntoView({block:'nearest'})}});
   $('dprev').disabled=d.page<=1;$('dnext').disabled=d.page>=d.pages;
 }catch(e){if(token===detailSeq){$('dstatus').textContent='조회 실패: '+e.message;$('dtable').replaceChildren()}}
}
$('dsearch').onclick=()=>loadDetails(1);$('drule').onchange=$('dsort').onchange=()=>loadDetails(1);
$('dprev').onclick=()=>loadDetails(detailPage-1);$('dnext').onclick=()=>loadDetails(detailPage+1);
$('dquery').onkeydown=e=>{if(e.key==='Enter')loadDetails(1)};

['group','scope','horizon'].forEach(id=>$(id).onchange=()=>{show();loadDetails(1)});
$('go').onclick=async()=>{try{$('go').disabled=true;let r=await fetch('/stage3/start',{method:'POST'});if(!r.ok)throw Error(await r.text());let v=await r.json();if(!v.started)throw Error('다른 연구 실행 중');result=null;detailSeq++;$('out').hidden=true;$('detailbox').hidden=true;await poll()}catch(e){$('phase').textContent=e.message;$('go').disabled=false}};
async function poll(){try{let s=await(await fetch('/status',{cache:'no-store'})).json();$('go').disabled=s.status==='running';if(s.mode!=='stage3'){$('out').hidden=true;$('detailbox').hidden=true;$('phase').textContent=s.status==='running'?'다른 연구 실행 중':'대기';return}$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 '+(s.market_success??0)+'/'+(s.universe??0)+' · 오류 '+s.error_count+' · 수집 '+(s.fetch_seconds??'—')+'초 · 전체 '+(s.elapsed_seconds??'—')+'초'+(s.cache_used?' · 캐시 사용':'');$('errors').textContent=s.errors.join('\n');if((s.status==='done'||s.status==='error')&&!result){result=await(await fetch('/results')).json();$('out').hidden=false;show();if(s.status==='done')loadDetails(1)}}catch(e){$('phase').textContent='연결 확인: '+e.message}}
poll();setInterval(poll,2000);
</script></html>"""


# V0.4: winner-only retrospective snapshots, no holding-period assumptions.
LOOKBACK4=(20,14,7,3,1)
NUM4={"ret_1":"1일 변화율","ret_5":"5일 변화율","ret_7":"7일 변화율","ret_14":"14일 변화율","ret_20":"20일 변화율","rsi":"RSI","rsi_delta":"RSI 변화","vol_ratio":"거래량 배수","value_ratio":"거래대금 배수","ma5_distance":"MA5 거리%","ma20_distance":"MA20 거리%","ma60_distance":"MA60 거리%","bb_width_pct":"볼린저 폭%","macd_hist_pct":"MACD 히스토그램%","atr_pct":"ATR%","btc_relative_5":"BTC 상대강도 5일","body_pct":"캔들 몸통%","lower_wick_ratio":"아랫꼬리 비율"}
def snapshot4(rows,btc,start,n):
    # D-n means the completed candle of the calendar date D-n, not D-n start.
    cut=start-(n-1)*DAY
    v=features(rows,1440,cut,btc)
    h=[r for r in rows if r[0]+DAY<=cut]
    for k in (7,14):
        v["ret_"+str(k)]=pct(h[-1][4],h[-1-k][4]) if len(h)>k and h[-1][0]==cut-DAY and h[-1-k][0]==cut-(k+1)*DAY else None
    v["negative_14bar"]=v["ret_14"]<0 if v["ret_14"] is not None else None
    return v
def reverse4(data,rank):
    fs=[];charts={}
    for case in rank:
        m=case["market"];t=case["start"]
        for n in LOOKBACK4:
            v=snapshot4(data[m],data.get("KRW-BTC"),t,n)
            fs.append(dict(market=m,day=case["day"],rank=case["rank"],gain_pct=case["gain_pct"],offset_days=n,snapshot_date=iso(t-n*DAY)[:10],cut_kst=iso(t-(n-1)*DAY),**v))
        charts[case["day"]+"|"+m]=[dict(day=iso(r[0])[:10],o=r[1],h=r[2],l=r[3],c=r[4],v=r[5]) for r in data[m] if t-20*DAY<=r[0]<=t]
    summ=[]
    for n in LOOKBACK4:
        subset=[x for x in fs if x["offset_days"]==n]
        valid=[x for x in subset if x.get("valid")]
        for k,label in NUM4.items():
            vals=[x[k] for x in valid if isinstance(x.get(k),(int,float)) and not isinstance(x[k],bool)]
            summ.append(dict(offset_days=n,kind="median",metric=k,name=label,cases=len(subset),available=len(vals),median=round(statistics.median(vals),4) if vals else None))
        for k,label in dict(PATTERNS,negative_14bar="14일 하락").items():
            vals=[x[k] for x in valid if isinstance(x.get(k),bool)]
            summ.append(dict(offset_days=n,kind="frequency",metric=k,name=label,cases=len(subset),available=len(vals),hits=sum(vals),hit_pct=round(100*sum(vals)/len(vals),2) if vals else None))
    return fs,summ,charts
def run4(days):
    began=time.monotonic()
    try:
        end=int(now().timestamp())
        universe=sorted({x["market"] for x in api("market/all") if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        if not universe:raise RuntimeError("원화마켓 0개")
        probe=candles("KRW-BTC",1440,end,30,latest=True)
        if len(probe)<2:raise RuntimeError("BTC 일봉 부족")
        anchor=probe[-1][0]%DAY;current=day_start(end,anchor)
        if probe[-1][0]!=current:raise RuntimeError("BTC 일봉 지연")
        starts=[current-i*DAY for i in range(days,0,-1)]
        patch(phase="전체 일봉 1회 수집",done=0,total=len(universe),universe=len(universe))
        data={}
        for m,rows in parallel(universe,lambda m:candles(m,1440,end,200,latest=True)):
            if rows and all(r[0]%DAY==anchor for r in rows):data[m]=rows
            else:record_error(m+" 일봉 누락/경계 불일치")
        rank,cov=rank_days(data,starts,current,5)
        if not rank:raise RuntimeError("유효 급등 순위 0건")
        patch(phase="급등 전 20·14·7·3·1일 비교",market_success=len(data),done=0,total=len(rank),fetch_seconds=round(time.monotonic()-began,1))
        fs,summ,charts=reverse4(data,rank)
        patch(rank=rank,features=fs,summary=summ,coverage=cov,charts=charts,done=len(rank),status="done",phase="완료",elapsed_seconds=round(time.monotonic()-began,1))
    except Exception as e:record_error(str(e));patch(status="error",phase="실패")
def start4(days):
    with state_lock:
        if state["status"]=="running":return False
        state.clear();state.update(mode="reverse4",status="running",phase="준비",done=0,total=0,rank=[],features=[],summary=[],errors=[],coverage=[],charts={})
    threading.Thread(target=run4,args=(days,),daemon=True).start()
    return True
HTML4=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE SWING V0.4</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1100px;margin:auto;padding:16px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}button,a,select{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:10px;margin:4px;border-radius:7px;text-decoration:none}table{border-collapse:collapse;white-space:nowrap}td,th{padding:8px;border-bottom:1px solid #324355;text-align:right}td:first-child,th:first-child{text-align:left}.scroll{overflow:auto}small{color:#b4c4d1;line-height:1.7}progress{width:100%}svg{width:100%;height:auto}pre{white-space:pre-wrap}</style><h1>REVERSE SWING V0.4</h1><a href="/reverse5">V0.5 세 조건 검증</a><section><h2>급등 전 역추적 연구</h2><p>완료된 일별 상승률 TOP10만 선정. 급등일 D 기준 D-20·D-14·D-7·D-3·D-1 일봉 마감 상태 비교.</p><small>보유 기간이나 매매 수익률을 계산하지 않습니다. D-14 시점과 D-1에서 관측한 최근 14일 하락률은 별개입니다. 오늘 미완료 일봉 제외. 현 상장 목록 기준. 급등 코인 공통점 발견용이며 예측 성능은 별도 검증 대상입니다.</small><p><select id="days"><option selected>14</option><option>28</option></select>일 <button id="go">급등 전 역추적 시작</button></p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section id="out" hidden><h2>CSV 저장</h2><a href="/csv/rank">급등 순위</a><a href="/csv/features">5개 시점 상세</a><a href="/csv/summary">공통점</a><a href="/csv/coverage">수집률</a><a href="/csv/errors">오류</a><h2>급등 전 지표 변화</h2><div class="scroll" id="med"></div><h2>공통점 발견 비율 (%)</h2><div class="scroll" id="freq"></div><h2>개별 코인 차트 및 지표</h2><select id="case"></select><p id="detailstatus"></p><div id="chart"></div><div class="scroll" id="detail"></div><h2>일별 TOP10</h2><div class="scroll" id="rank"></div></section><script>
const $=id=>document.getElementById(id);const offsets=[20,14,7,3,1];let result=null,seq=0;
function table(id,heads,rows){let t=document.createElement('table'),tr=t.insertRow();for(const h of heads){let th=document.createElement('th');th.textContent=h;tr.append(th)}for(const row of rows){let tr=t.insertRow();for(const v of row)tr.insertCell().textContent=v??'—'}$(id).replaceChildren(t)}
function summary(kind,id){const rows=result.summary.filter(x=>x.kind===kind);const metrics=[...new Set(rows.map(x=>x.metric))];table(id,['지표',...offsets.map(n=>'D-'+n)],metrics.map(k=>{let a=rows.filter(x=>x.metric===k);return [a[0].name,...offsets.map(n=>{let x=a.find(r=>r.offset_days===n);let v=kind==='median'?x?.median:x?.hit_pct;return v==null?'—':v+' ('+x.available+'/'+x.cases+')'})]}))}
function draw(rows){const ns='http://www.w3.org/2000/svg';const svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 720 310');if(!rows.length){$('chart').textContent='차트 데이터 없음';return}let hi=Math.max(...rows.map(r=>r.h)),lo=Math.min(...rows.map(r=>r.l)),span=hi-lo||hi*.01||1;const y=p=>20+(hi-p)/span*210;let maxv=Math.max(...rows.map(r=>r.v))||1;function el(tag,attrs,txt){let e=document.createElementNS(ns,tag);for(let [k,v] of Object.entries(attrs))e.setAttribute(k,v);if(txt!=null)e.textContent=txt;svg.append(e)}
const day0=Date.parse(rows[rows.length-1].day+'T00:00:00Z')-20*86400000;rows.forEach(r=>{const i=Math.round((Date.parse(r.day+'T00:00:00Z')-day0)/86400000),x=34+i*32,color=r.c>=r.o?'#55d7ae':'#f87171';el('line',{x1:x,x2:x,y1:y(r.h),y2:y(r.l),stroke:color});el('rect',{x:x-6,y:Math.min(y(r.o),y(r.c)),width:12,height:Math.max(1,Math.abs(y(r.o)-y(r.c))),fill:color});el('rect',{x:x-6,y:275-35*r.v/maxv,width:12,height:35*r.v/maxv,fill:color,opacity:.6});if([0,6,13,17,19,20].includes(i))el('text',{x,y:i===19?307:292,fill:'#b4c4d1','font-size':11,'text-anchor':'middle'},i===20?'D':'D-'+(20-i))});el('text',{x:3,y:13,fill:'#b4c4d1','font-size':11},'최고 '+hi);el('text',{x:3,y:236,fill:'#b4c4d1','font-size':11},'최저 '+lo);$('chart').replaceChildren(svg)}
async function details(){let r=result.rank[Number($('case').value)];if(!r)return;let token=++seq;$('detailstatus').textContent='조회 중';try{let res=await fetch('/reverse4/details?'+new URLSearchParams({day:r.day,market:r.market}),{cache:'no-store'});if(!res.ok)throw Error(await res.text());let d=await res.json();if(token!==seq)return;$('detailstatus').textContent=r.day+' '+r.market+' · 당일 상승률 '+r.gain_pct.toFixed(2)+'%';draw(d.chart);let keys=['price','ret_1','ret_5','ret_7','ret_14','ret_20','rsi','rsi_delta','vol_ratio','value_ratio','ma5_distance','ma20_distance','ma60_distance','bb_width_pct','macd_hist_pct','atr_pct','btc_relative_5'];table('detail',['시점','날짜','가격','1일%','5일%','7일%','14일%','20일%','RSI','RSI 변화','거래량 배수','거래대금 배수','MA5 거리%','MA20 거리%','MA60 거리%','볼린저 폭%','MACD 히스토그램%','ATR%','BTC 상대강도'],d.rows.map(x=>['D-'+x.offset_days,x.snapshot_date,...keys.map(k=>typeof x[k]==='number'?Number(x[k].toPrecision(6)):x[k])]))}catch(e){if(token===seq)$('detailstatus').textContent=e.message}}
function show(){summary('median','med');summary('frequency','freq');$('case').replaceChildren(...result.rank.map((r,i)=>{let o=document.createElement('option');o.value=i;o.textContent=r.day+' #'+r.rank+' '+r.market+' +'+r.gain_pct.toFixed(2)+'%';return o}));table('rank',['날짜','순위','코인','상승률%'],result.rank.map(r=>[r.day,r.rank,r.market,r.gain_pct.toFixed(2)]));details()}
$('case').onchange=details;$('go').onclick=async()=>{try{let r=await fetch('/reverse4/start?days='+$('days').value,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 연구 실행 중 또는 설정 오류');result=null;seq++;$('out').hidden=true;await poll()}catch(e){$('phase').textContent=e.message}};
async function poll(){try{let s=await(await fetch('/status',{cache:'no-store'})).json();$('go').disabled=s.status==='running';if(s.mode!=='reverse4'){$('out').hidden=true;result=null;$('phase').textContent=s.status==='running'?'다른 연구 실행 중':'대기';return}$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 '+(s.market_success??0)+'/'+(s.universe??0)+' · 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');if(s.status==='running'){result=null;$('out').hidden=true}if(s.status==='done'&&!result){result=await(await fetch('/results')).json();$('out').hidden=false;show()}}catch(e){$('phase').textContent='연결 확인: '+e.message}}
poll();setInterval(poll,2000);
</script></html>"""


# V0.5: only the three requested conditions; daily retrospective classification.
RULE5={"BASE":(),"A":("A",),"B":("B",),"C":("C",),"AB":("A","B"),"AC":("A","C"),"BC":("B","C"),"ABC":("A","B","C")}
def light5(rows,cut):
    h=[r for r in rows if r[0]+DAY<=cut]
    if len(h)<21 or h[-1][0]!=cut-DAY or any(b[0]-a[0]!=DAY for a,b in zip(h[-21:],h[-20:])):return None
    close=[r[4] for r in h];vol=[r[5] for r in h]
    ret=pct(close[-1],close[-21]);vr=ratio(vol[-1],mean(vol[-21:-1]))
    if ret is None or vr is None:return None
    return ret,rsi(close),vr
def eval5(data,starts):
    rank,cov=rank_days(data,starts,-1,5)
    top={(x["market"],x["start"]) for x in rank}
    indexed={m:{r[0]:r for r in rows} for m,rows in data.items()}
    out=[]; coverage=[]
    for t,cc in zip(starts,cov):
        day=iso(t)[:10];group="earlier" if day<"2026-11-11" else "recent"
        batch=[]
        for m,rows in data.items():
            by=indexed[m];cur=by.get(t);prior=by.get(t-DAY)
            if not cur or not prior or prior[4]<=0:continue
            d1=light5(rows,t);d3=light5(rows,t-2*DAY)
            if d1 is None or d3 is None:continue
            ret,rs,vr=d1;prevvr=d3[2];gain=pct(cur[4],prior[4])
            batch.append(dict(day=day,group=group,market=m,ret20=ret,rsi=rs,vol_ratio_d3=prevvr,vol_ratio_d1=vr,A=ret<0,B=vr>prevvr,C=rs<40,top10=(m,t) in top,gain_pct=gain))
        out.extend(batch)
        coverage.append(dict(cc,group=group,feature_valid=len(batch),top10_valid=sum(x["top10"] for x in batch)))
    summ=[]
    for group in ("all","earlier","recent"):
        sample=[x for x in out if group=="all" or x["group"]==group]
        total=len(sample);tops=sum(x["top10"] for x in sample);days=len({x["day"] for x in sample})
        base=tops/total if total else 0
        for name,keys in RULE5.items():
            selected=[x for x in sample if all(x[k] for k in keys)]
            n=len(selected);hits=sum(x["top10"] for x in selected)
            summ.append(dict(group=group,rule=name,days=days,valid=total,top10_valid=tops,candidates=n,per_day=round(n/days,2) if days else None,top10_hits=hits,top10_recall_pct=round(100*hits/tops,2) if tops else None,market_frequency_pct=round(100*n/total,2) if total else None,top10_precision_pct=round(100*hits/n,2) if n else None,lift=round(hits/n/base,3) if n and base else None))
    return rank,out,summ,coverage
def run5():
    begin=time.monotonic()
    try:
        end=int(now().timestamp())
        universe=sorted({x["market"] for x in api("market/all") if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        probe=candles("KRW-BTC",1440,end,30,latest=True)
        if not universe or len(probe)<2:raise RuntimeError("시장 목록 또는 BTC 일봉 부족")
        anchor=probe[-1][0]%DAY; current=day_start(end,anchor)
        if probe[-1][0]!=current:raise RuntimeError("BTC 일봉 지연")
        first=day_start(int(datetime(2026,10,28,12,tzinfo=KST).timestamp()),anchor)
        starts=[first+i*DAY for i in range(28)]
        if starts[-1]+DAY>end:raise RuntimeError("완료 일봉 부족")
        patch(phase="전체 일봉 1회 수집",done=0,total=len(universe),universe=len(universe))
        data={}
        for m,rows in parallel(universe,lambda m:candles(m,1440,end,200,latest=True)):
            if rows and all(r[0]%DAY==anchor for r in rows):data[m]=rows
            else:record_error(m+" 일봉 누락/경계 불일치")
        patch(phase="세 조건 비교",done=0,total=28,market_success=len(data))
        rank,rows,summ,cov=eval5(data,starts)
        if not rows:raise RuntimeError("유효 비교 데이터 없음")
        patch(rank=rank,features=rows,summary=summ,coverage=cov,done=28,status="done",phase="완료",elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:record_error(str(e));patch(status="error",phase="실패")
def start5():
    with state_lock:
        if state["status"]=="running":return False
        state.clear();state.update(mode="reverse5",status="running",phase="준비",done=0,total=0,rank=[],features=[],summary=[],errors=[],coverage=[])
    threading.Thread(target=run5,daemon=True).start()
    return True
HTML5=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE SWING V0.5</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1000px;margin:auto;padding:16px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}button,a,select{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:10px;margin:4px;border-radius:7px;text-decoration:none}table{border-collapse:collapse;white-space:nowrap}td,th{padding:8px;border-bottom:1px solid #324355;text-align:right}td:first-child,th:first-child{text-align:left}.scroll{overflow:auto}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}</style><h1>REVERSE SWING V0.5</h1><a href="/reverse6">V0.6.1 하락 추세 연구</a><a href="/reverse4">V0.4 급등 전 역추적</a><section><h2>세 가지 조건 전체 시장 비교</h2><p>A: 최근 20일 수익률 음수<br>B: D-3 대비 D-1 거래량 배수 증가 (직전 20일 평균 대비)<br>C: D-1 RSI 40 미만</p><small>2026년 10/28~11/24 동일 28일. D-1까지 마감된 일봉만 사용. 당일 상승률 TOP10과 전체 유효 시장 비교. 7개 조합 및 전체 기준. 이전·최근 14일은 이미 발견에 사용한 기간으로 독립 검증 아님. 매수·보유·손절 조건 없음.</small><p><button id="go">세 조건 검증 시작</button></p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section id="out" hidden><a href="/csv/summary">조건 비교 CSV</a><a href="/csv/features">상세 CSV</a><a href="/csv/rank">TOP10 CSV</a><a href="/csv/coverage">수집률 CSV</a><a href="/csv/errors">오류 CSV</a><p><select id="group"><option value="all">전체 28일</option><option value="earlier">이전 14일</option><option value="recent">최근 14일</option></select></p><small>포착률 = 조건 충족 TOP10 / 유효 TOP10. 적중률 = 조건 충족 TOP10 / 전체 조건 충족. 배수 = 전체 기준 대비 적중률.</small><div class="scroll" id="tab"></div><div class="scroll" id="cov"></div></section><script>
const $=id=>document.getElementById(id);let result=null;
function table(id,heads,rows){let t=document.createElement('table'),tr=t.insertRow();for(const h of heads){let th=document.createElement('th');th.textContent=h;tr.append(th)}for(const row of rows){let tr=t.insertRow();for(const v of row)tr.insertCell().textContent=v??'—'}$(id).replaceChildren(t)}
function show(){if(!result)return;let g=$('group').value;table('tab',['조건','후보','일평균','전체 발생%','TOP10 포착','포착률%','적중률%','기준 대비 배수'],result.summary.filter(x=>x.group===g).map(x=>[x.rule,x.candidates,x.per_day,x.market_frequency_pct,x.top10_hits,x.top10_recall_pct,x.top10_precision_pct,x.lift]));table('cov',['날짜','순위 유효','조건 유효','TOP10 유효'],result.coverage.filter(x=>g==='all'||x.group===g).map(x=>[x.day,x.valid_markets,x.feature_valid,x.top10_valid]))}
$('group').onchange=show;$('go').onclick=async()=>{try{$('go').disabled=true;let r=await fetch('/reverse5/start',{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 연구 실행 중');result=null;$('out').hidden=true;await poll()}catch(e){$('phase').textContent=e.message;$('go').disabled=false}};
async function poll(){try{let s=await(await fetch('/status',{cache:'no-store'})).json();$('go').disabled=s.status==='running';if(s.mode!=='reverse5'){$('out').hidden=true;result=null;$('phase').textContent=s.status==='running'?'다른 연구 실행 중':'대기';return}$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 '+(s.market_success??0)+'/'+(s.universe??0)+' · 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');if(s.status==='running'){result=null;$('out').hidden=true}if(s.status==='done'&&!result){result=await(await fetch('/results')).json();$('out').hidden=false;show()}}catch(e){$('phase').textContent='연결 확인: '+e.message}}
poll();setInterval(poll,2000);
</script></html>"""


# V0.6: chronological daily downtrend and pullback depth; winner-only research.
WINDOW6=(7,10,14,20)
def trend6(rows,t,n):
    # n daily close-to-close changes, n+1 consecutive completed candles.
    by={r[0]:r for r in rows}
    seq=[by.get(t-i*DAY) for i in range(n+1,0,-1)]
    if not all(seq):return {"valid":False}
    if any(min(r[1:5])<=0 for r in seq):return {"valid":False}
    close=[r[4] for r in seq]
    daily=[pct(b,a) for a,b in zip(close,close[1:])]
    down=sum(b<a for a,b in zip(close,close[1:]))
    streak=longest=0
    for a,b in zip(close,close[1:]):
        streak=streak+1 if b<a else 0;longest=max(longest,streak)
    half=len(seq)//2; first=seq[:half]; last=seq[-half:]
    lower_high=max(r[2] for r in last)<max(r[2] for r in first)
    lower_low=min(r[3] for r in last)<min(r[3] for r in first)
    logc=[math.log(v) for v in close]; mx=n/2; my=mean(logc)
    slope=sum((i-mx)*(v-my) for i,v in enumerate(logc))/sum((i-mx)**2 for i in range(n+1))
    # Ordered high->low on strictly different days; no same-day OHLC ordering assumption.
    peak=seq[0][2];peakidx=0; dd=0.0;ddpeak=0;ddlow=0
    for i,r in enumerate(seq[1:],1):
        drop=pct(r[3],peak)
        if drop<dd:dd=drop;ddpeak=peakidx;ddlow=i
        if r[2]>peak:peak=r[2];peakidx=i
    enddrop=pct(close[-1],max(r[2] for r in seq))
    ret=pct(close[-1],close[0])
    # A descriptive, transparent trend definition, not an entry rule.
    trend=ret<0 and slope<0 and lower_high and lower_low
    return dict(valid=True,start_date=iso(seq[0][0])[:10],end_date=iso(seq[-1][0])[:10],start_close=close[0],end_close=close[-1],net_pct=ret,down_days=down,down_pct=100*down/n,up_days=sum(v>0 for v in daily),flat_days=sum(v==0 for v in daily),longest_down_streak=longest,ending_down_streak=streak,all_days_down=down==n,lower_high=lower_high,lower_low=lower_low,slope_pct_per_day=100*math.expm1(slope),downtrend=trend,peak_to_later_low_pct=dd,drawdown_peak_date=iso(seq[ddpeak][0])[:10] if dd<0 else None,drawdown_low_date=iso(seq[ddlow][0])[:10] if dd<0 else None,peak_to_last_close_pct=enddrop,lower_high_steps=sum(b[2]<a[2] for a,b in zip(seq,seq[1:])),lower_low_steps=sum(b[3]<a[3] for a,b in zip(seq,seq[1:])))
def eval6(data,rank):
    fs=[]
    for case in rank:
        hist=data[case["market"]];t=case["start"]
        d1=light5(hist,t);d3=light5(hist,t-2*DAY)
        ready=d1 is not None and d3 is not None
        vr1=d1[2] if d1 else None;vr3=d3[2] if d3 else None
        rs=d1[1] if d1 else None
        for n in WINDOW6:
            v=trend6(hist,t,n)
            v.update(indicators_valid=ready,vol_ratio_d1=vr1,vol_ratio_d3=vr3,rsi_d1=rs,volume_increasing=vr1>vr3 if ready else None,rsi_below_40=rs<40 if rs is not None else None,combined=bool(v.get("downtrend") and vr1>vr3 and rs<40) if v["valid"] and ready else None)
            fs.append(dict(day=case["day"],market=case["market"],rank=case["rank"],gain_pct=case["gain_pct"],window=n,group="earlier" if case["day"]<"2026-11-11" else "recent",**v))
    summ=[]
    for group in ("all","earlier","recent"):
        for n in WINDOW6:
            rows=[x for x in fs if x["window"]==n and (group=="all" or x["group"]==group)]
            valid=[x for x in rows if x["valid"]];tr=[x for x in valid if x["downtrend"]]
            def med(a,k):return round(statistics.median(x[k] for x in a),3) if a else None
            def rate(a,k):return round(100*sum(x[k] for x in a)/len(a),2) if a else None
            combined_valid=[x for x in valid if x["indicators_valid"]]
            matched=[x for x in combined_valid if x["combined"]]
            for cohort,a in (("all_valid",valid),("downtrend",tr),("combined",matched)):
                bins=[sum(lo<=-x["peak_to_last_close_pct"]<hi for x in a) for lo,hi in ((0,10),(10,20),(20,30),(30,40),(40,50),(50,float("inf")))]
                summ.append(dict(group=group,window=n,cohort=cohort,cases=len(rows),valid=len(valid),count=len(a),share_pct=round(100*len(a)/len(combined_valid if cohort=="combined" else valid),2) if (combined_valid if cohort=="combined" else valid) else None,combined_valid=len(combined_valid),combined_count=len(matched),combined_share_pct=round(100*len(matched)/len(combined_valid),2) if combined_valid else None,median_vol_d3=med([x for x in a if x["vol_ratio_d3"] is not None],"vol_ratio_d3"),median_vol_d1=med([x for x in a if x["vol_ratio_d1"] is not None],"vol_ratio_d1"),median_rsi=med([x for x in a if x["rsi_d1"] is not None],"rsi_d1"),strict_down_count=sum(x["all_days_down"] for x in valid),median_down_days=med(a,"down_days"),median_longest_streak=med(a,"longest_down_streak"),median_net_pct=med(a,"net_pct"),median_peak_to_low_pct=med(a,"peak_to_later_low_pct"),median_peak_to_last_pct=med(a,"peak_to_last_close_pct"),median_surge_pct=med(a,"gain_pct"),depth_0_10=bins[0],depth_10_20=bins[1],depth_20_30=bins[2],depth_30_40=bins[3],depth_40_50=bins[4],depth_50_plus=bins[5]))
    return fs,summ
def run6():
    begin=time.monotonic()
    try:
        end=int(now().timestamp())
        universe=sorted({x["market"] for x in api("market/all") if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        probe=candles("KRW-BTC",1440,end,30,latest=True)
        if not universe or len(probe)<2:raise RuntimeError("시장 목록 또는 BTC 일봉 부족")
        anchor=probe[-1][0]%DAY;current=day_start(end,anchor)
        if probe[-1][0]!=current:raise RuntimeError("BTC 일봉 지연")
        first=day_start(int(datetime(2026,10,28,12,tzinfo=KST).timestamp()),anchor)
        starts=[first+i*DAY for i in range(28)]
        if starts[-1]+DAY>end:raise RuntimeError("완료 일봉 부족")
        patch(phase="전체 일봉 1회 수집",done=0,total=len(universe),universe=len(universe))
        data={}
        for m,rows in parallel(universe,lambda m:candles(m,1440,end,200,latest=True)):
            if rows and all(r[0]%DAY==anchor for r in rows):data[m]=rows
            else:record_error(m+" 일봉 누락/경계 불일치")
        rank,cov=rank_days(data,starts,current,5)
        if not rank:raise RuntimeError("급등 순위 없음")
        patch(phase="7·10·14·20일 하락 흐름 비교",done=0,total=len(rank),market_success=len(data))
        rows,summ=eval6(data,rank)
        if not any(x["valid"] for x in rows):raise RuntimeError("유효 추세 데이터 없음")
        patch(rank=rank,features=rows,summary=summ,coverage=cov,done=len(rank),status="done",phase="완료",elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:record_error(str(e));patch(status="error",phase="실패")
def start6():
    with state_lock:
        if state["status"]=="running":return False
        state.clear();state.update(mode="reverse6",status="running",phase="준비",done=0,total=0,rank=[],features=[],summary=[],errors=[],coverage=[])
    threading.Thread(target=run6,daemon=True).start()
    return True
HTML6=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE SWING V0.6.1</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1000px;margin:auto;padding:16px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:10px;margin:4px;border-radius:7px;text-decoration:none}table{border-collapse:collapse;white-space:nowrap}td,th{padding:8px;border-bottom:1px solid #324355;text-align:right}td:first-child,th:first-child{text-align:left}.scroll{overflow:auto;max-height:480px}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}</style><h1>REVERSE SWING V0.6.1</h1><a href="/reverse7">V0.7 전체 검증</a><a href="/reverse4">V0.4 기존 연구</a><section><h2>급등 전 실제 하락 추세 연구</h2><p>10/28~11/24 (2026) 일별 상승 TOP10. 급등 전 완료 일봉의 7·10·14·20일 가격 흐름과 하락폭 + 거래량 배수 증가 + RSI 40 미만.</p><small>하락 추세(탐색용 정의): 기간 종가 하락 + 로그 종가 기울기 음수 + 후반 절반의 최고가·최저가가 각각 전반 절반보다 낮음. 매일 하락한 경우와 구분. n일은 n개의 일간 변화(n+1개 종가). 고점→최저점은 서로 다른 날짜에서 고점이 먼저 발생한 경우만 비교. 고점→직전 종가는 해당 기간 최고가 대비 D-1 종가. 거래량 배수: D-3보다 D-1이 높음 (각 시점 직전 20일 평균 대비). RSI: D-1에서 40 미만. 세 조건 동시 충족을 기본으로 표시. 매수 조건이나 보유 기간이 아님. 기간들은 중첩되므로 중복 집계 가능.</small><p><button id="go">하락 추세 역추적 시작</button></p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section id="out" hidden><a href="/csv/summary">기간별 비교 CSV</a><a href="/csv/features">상세 결과 CSV</a><a href="/csv/rank">TOP10 CSV</a><a href="/csv/coverage">수집률 CSV</a><a href="/csv/errors">오류 CSV</a><p><select id="group"><option value="all">전체 28일</option><option value="earlier">이전 14일</option><option value="recent">최근 14일</option></select></p><h2>기간별 세 조건 동시 충족 결과</h2><div class="scroll" id="tab"></div><h2>세 조건 충족 사례의 고점 대비 급등 직전 하락폭 분포</h2><div class="scroll" id="bins"></div><h2>개별 코인 결과</h2><p><select id="win"><option>7</option><option>10</option><option>14</option><option selected>20</option></select>일 <select id="filter"><option value="combined" selected>세 조건 모두 충족</option><option value="trend">하락 추세만</option><option value="all">전체 유효</option><option value="strict">매일 하락</option></select><input id="query" placeholder="코인명" style="width:100px"></p><div id="count"></div><div class="scroll" id="detail"></div></section><script>
const $=id=>document.getElementById(id);let result=null,features=[];
function table(id,heads,rows){let t=document.createElement('table'),tr=t.insertRow();for(const h of heads){let th=document.createElement('th');th.textContent=h;tr.append(th)}for(const row of rows){let tr=t.insertRow();for(const v of row)tr.insertCell().textContent=v??'—'}$(id).replaceChildren(t)}
function fmt(x){return typeof x==='number'?Number(x.toFixed(2)):x}
function detail(){let g=$('group').value,n=Number($('win').value),q=$('query').value.trim().toUpperCase(),f=$('filter').value;let a=features.filter(x=>x.valid&&x.window===n&&(g==='all'||x.group===g)&&x.market.includes(q)&&(f==='all'||(f==='combined'?x.combined:f==='strict'?x.all_days_down:x.downtrend)));$('count').textContent=a.length+'건';table('detail',['급등일','코인','당일 상승%','하락일수','최장 연속 하락','종가변화%','고점→최저%','고점→직전%','D-3 거래량 배수','D-1 거래량 배수','RSI','세 조건 충족'],a.map(x=>[x.day,x.market,fmt(x.gain_pct),x.down_days,x.longest_down_streak,fmt(x.net_pct),fmt(x.peak_to_later_low_pct),fmt(x.peak_to_last_close_pct),fmt(x.vol_ratio_d3),fmt(x.vol_ratio_d1),fmt(x.rsi_d1),x.combined==null?'데이터 부족':x.combined?'충족':'미충족']))}
function show(){if(!result)return;let g=$('group').value;let a=result.summary.filter(x=>x.group===g&&x.cohort==='combined');table('tab',['기간','세 조건 유효','충족 건수','포착률%','하락일 중앙','고점→최저%','고점→직전%','D-3 거래량 배수 중앙','D-1 거래량 배수 중앙','RSI 중앙','급등% 중앙'],a.map(x=>[x.window,x.combined_valid,x.count,x.share_pct,x.median_down_days,x.median_peak_to_low_pct,x.median_peak_to_last_pct,x.median_vol_d3,x.median_vol_d1,x.median_rsi,x.median_surge_pct]));table('bins',['기간','0~10%','10~20%','20~30%','30~40%','40~50%','50% 이상'],a.map(x=>[x.window,x.depth_0_10,x.depth_10_20,x.depth_20_30,x.depth_30_40,x.depth_40_50,x.depth_50_plus]));detail()}
$('group').onchange=show;['win','filter'].forEach(id=>$(id).onchange=detail);$('query').oninput=detail;
$('go').onclick=async()=>{try{$('go').disabled=true;let r=await fetch('/reverse6/start',{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 연구 실행 중');result=null;features=[];$('out').hidden=true;await poll()}catch(e){$('phase').textContent=e.message;$('go').disabled=false}};
let loading=false;
async function poll(){try{let s=await(await fetch('/status',{cache:'no-store'})).json();$('go').disabled=s.status==='running';if(s.mode!=='reverse6'){$('out').hidden=true;result=null;$('phase').textContent=s.status==='running'?'다른 연구 실행 중':'대기';return}$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 '+(s.market_success??0)+'/'+(s.universe??0)+' · 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');if(s.status==='running'){result=null;$('out').hidden=true}if(s.status==='done'&&!result&&!loading){loading=true;try{let rr=await fetch('/results'),ff=await fetch('/reverse6/details');if(!rr.ok||!ff.ok)throw Error('결과 조회 실패');let a=await rr.json(),b=await ff.json();result=a;features=b;$('out').hidden=false;show()}finally{loading=false}}}catch(e){$('phase').textContent='연결 확인: '+e.message}}
poll();setInterval(poll,2000);
</script></html>"""


# V0.7: unchanged V0.6.1 conditions, all-market next-day open/close comparison.
def stats7(a):
    n=len(a);wins=sum(x["net_pct"]>1e-10 for x in a);loss=sum(x["net_pct"]< -1e-10 for x in a)
    return dict(candidates=n,wins=wins,losses=loss,flat=n-wins-loss,win_pct=round(100*wins/n,2) if n else None,avg_net_pct=round(mean([x["net_pct"] for x in a]),4) if n else None,median_net_pct=round(statistics.median(x["net_pct"] for x in a),4) if n else None,avg_gross_pct=round(mean([x["gross_pct"] for x in a]),4) if n else None,top10_hits=sum(x["top10"] for x in a))
def eval7(data,starts,progress=None):
    rank,_=rank_days(data,starts,-1,5)
    top={(x["market"],x["start"]) for x in rank}
    indexed={m:{r[0]:r for r in rows} for m,rows in data.items()}
    details=[]; daily=[]; bases=[]
    for i,t in enumerate(starts):
        label=iso(t)[:10];group="earlier" if label<"2026-11-11" else "recent"
        batches={n:[] for n in WINDOW6};base={n:[] for n in WINDOW6}
        for m,rows in data.items():
            by=indexed[m];cur=by.get(t);prior=by.get(t-DAY)
            if not cur or not prior or cur[1]<=0 or cur[4]<=0 or prior[4]<=0:continue
            d1=light5(rows,t);d3=light5(rows,t-2*DAY)
            if d1 is None or d3 is None:continue
            rs=d1[1];vr1=d1[2];vr3=d3[2]
            gross=pct(cur[4],cur[1]);net=100*(cur[4]/cur[1]*(1-FEE3)/(1+FEE3)-1)
            common=dict(day=label,group=group,market=m,buy_open=cur[1],sell_close=cur[4],gross_pct=gross,net_pct=net,top10=(m,t) in top,day_gain_pct=pct(cur[4],prior[4]))
            for n in WINDOW6:
                v=trend6(rows,t,n)
                if not v["valid"]:continue
                base[n].append(common)
                if not (v["downtrend"] and vr1>vr3 and rs<40):continue
                batches[n].append(dict(common,window=n,down_days=v["down_days"],longest_down_streak=v["longest_down_streak"],trend_net_pct=v["net_pct"],peak_to_low_pct=v["peak_to_later_low_pct"],peak_to_last_pct=v["peak_to_last_close_pct"],vol_ratio_d3=vr3,vol_ratio_d1=vr1,rsi=rs))
        for n in WINDOW6:
            a=batches[n];b=base[n];details.extend(a)
            bs=stats7(b);ss=stats7(a)
            daily.append(dict(day=label,group=group,window=n,valid=len(b),base_win_pct=bs["win_pct"],base_avg_net_pct=bs["avg_net_pct"],top10_valid=bs["top10_hits"],**ss))
            bases.extend(dict(x,window=n) for x in b)
        if progress:progress(i+1)
    summary=[]
    for group in ("all","earlier","recent"):
        for n in WINDOW6:
            a=[x for x in details if x["window"]==n and (group=="all" or x["group"]==group)]
            b=[x for x in bases if x["window"]==n and (group=="all" or x["group"]==group)]
            days=sum(1 for d in daily if d["window"]==n and d["valid"]>0 and (group=="all" or d["group"]==group))
            bs=stats7(b);ss=stats7(a)
            summary.append(dict(group=group,window=n,days=days,valid=len(b),per_day=round(len(a)/days,2) if days else None,base_win_pct=bs["win_pct"],base_avg_net_pct=bs["avg_net_pct"],top10_valid=bs["top10_hits"],**ss))
    return rank,details,summary,daily
def run7():
    begin=time.monotonic()
    try:
        end=int(now().timestamp())
        universe=sorted({x["market"] for x in api("market/all") if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        probe=candles("KRW-BTC",1440,end,30,latest=True)
        if not universe or len(probe)<2:raise RuntimeError("시장 목록 또는 BTC 일봉 부족")
        anchor=probe[-1][0]%DAY;current=day_start(end,anchor)
        if probe[-1][0]!=current:raise RuntimeError("BTC 일봉 지연")
        first=day_start(int(datetime(2026,10,28,12,tzinfo=KST).timestamp()),anchor)
        starts=[first+i*DAY for i in range(28)]
        if starts[-1]+DAY>end:raise RuntimeError("완료 일봉 부족")
        patch(phase="전체 일봉 1회 수집",done=0,total=len(universe),universe=len(universe))
        data={}
        for m,rows in parallel(universe,lambda m:candles(m,1440,end,200,latest=True)):
            if rows and all(r[0]%DAY==anchor for r in rows):data[m]=rows
            else:record_error(m+" 일봉 누락/경계 불일치")
        patch(phase="전체 코인 세 조건 비교",done=0,total=28,market_success=len(data))
        rank,rows,summ,daily=eval7(data,starts,lambda i:patch(done=i))
        if not any(x["valid"] for x in daily):raise RuntimeError("유효 비교 데이터 없음")
        patch(rank=rank,features=rows,summary=summ,coverage=daily,done=28,status="done",phase="완료",elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:record_error(str(e));patch(status="error",phase="실패")
def start7():
    with state_lock:
        if state["status"]=="running":return False
        state.clear();state.update(mode="reverse7",status="running",phase="준비",done=0,total=0,rank=[],features=[],summary=[],errors=[],coverage=[])
    threading.Thread(target=run7,daemon=True).start()
    return True
HTML7=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE SWING V0.7</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1050px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:12px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:9px;margin:4px;border-radius:7px;text-decoration:none}button:disabled{opacity:.5}table{border-collapse:collapse;white-space:nowrap}td,th{padding:8px;border-bottom:1px solid #324355;text-align:right}td:first-child,th:first-child{text-align:left}.scroll{overflow:auto;max-height:480px}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}</style><h1>REVERSE SWING V0.7</h1><a href="/reverse8">V0.8 7일 스윙</a><a href="/reverse6">V0.6.1 급등 역추적</a><section><h2>세 조건 전체 코인 검증</h2><p>7·10·14·20일 실제 하락 추세 + D-3 대비 D-1 거래량 배수 증가 + D-1 RSI 40 미만.</p><small>2026년 10/28~11/24. V0.6.1 조건 그대로. 신호는 전일 마감까지. 다음 일봉 시가 진입·당일 종가 청산 가정, 편도 0.25% 수수료, 슬리피지 제외. 당일 비교용이며 스윙 최종 승률이 아님. 동일 코인의 여러 날짜·기간 신호 중복 포함, 기간별 별도 집계. 현재 상장 목록 기준, 이미 연구한 기간.</small><p><button id="go">전체 코인 검증 시작</button></p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section id="out" hidden><a href="/csv/summary">승률 비교 CSV</a><a id="csv" href="/csv/features?window=20">선택 기간 진입 종목 CSV</a><a href="/csv/coverage">일별 결과 CSV</a><a href="/csv/errors">오류 CSV</a><h2>기간별 수익·손실 비교</h2><select id="group"><option value="all">전체 28일</option><option value="earlier">이전 14일</option><option value="recent">최근 14일</option></select><div class="scroll" id="tab"></div><h2>일별 결과 및 진입 종목</h2><select id="win"><option>7</option><option>10</option><option>14</option><option selected>20</option></select>일 <select id="day"><option value="">전체 날짜</option></select><input id="query" placeholder="코인 검색" style="width:100px"><button id="search">조회</button><div class="scroll" id="daily"></div><p id="count"></p><div class="scroll" id="detail"></div><button id="prev">이전</button><button id="next">다음</button></section><script>
const $=id=>document.getElementById(id);let result=null,page=1,pages=1,seq=0,loading=false;
function table(id,heads,rows){let t=document.createElement('table'),tr=t.insertRow();for(const h of heads){let th=document.createElement('th');th.textContent=h;tr.append(th)}for(const row of rows){let tr=t.insertRow();for(const v of row)tr.insertCell().textContent=v??'—'}$(id).replaceChildren(t)}
function fmt(x){return typeof x==='number'?Number(x.toPrecision(7)):x}
async function detail(p=1){if(!result)return;let token=++seq;try{let q=new URLSearchParams({group:$('group').value,window:$('win').value,day:$('day').value,query:$('query').value,page:String(p)});let r=await fetch('/reverse7/details?'+q,{cache:'no-store'});if(!r.ok)throw Error(await r.text());let d=await r.json();if(token!==seq)return;page=d.page;pages=d.pages;$('count').textContent=d.total+'건 · '+page+'/'+pages+' 페이지';table('detail',['진입일','코인','매수가','매도가','순수익%','판정','하락일수','고점 대비 하락%','거래량 D-3','거래량 D-1','RSI'],d.rows.map(x=>[x.day,x.market,fmt(x.buy_open),fmt(x.sell_close),fmt(x.net_pct),x.net_pct>1e-10?'수익':x.net_pct< -1e-10?'손실':'보합',x.down_days,fmt(x.peak_to_last_pct),fmt(x.vol_ratio_d3),fmt(x.vol_ratio_d1),fmt(x.rsi)]));$('prev').disabled=page<=1;$('next').disabled=page>=pages}catch(e){if(token===seq)$('count').textContent='조회 실패: '+e.message}}
function show(){if(!result)return;let g=$('group').value,n=Number($('win').value);table('tab',['하락기간','검색건수','일평균','수익','손실','보합','승률%','평균 순수익%','전체 기준 승률%','전체 기준 평균%','TOP10 포착'],result.summary.filter(x=>x.group===g).map(x=>[x.window,x.candidates,x.per_day,x.wins,x.losses,x.flat,x.win_pct,x.avg_net_pct,x.base_win_pct,x.base_avg_net_pct,x.top10_hits]));let d=result.coverage.filter(x=>x.window===n&&(g==='all'||x.group===g));let old=$('day').value;let o=document.createElement('option');o.value='';o.textContent='전체 날짜';$('day').replaceChildren(o,...d.map(x=>{let a=document.createElement('option');a.value=x.day;a.textContent=x.day;return a}));if(d.some(x=>x.day===old))$('day').value=old;table('daily',['날짜','검색','수익','손실','승률%','평균 순수익%'],d.map(x=>[x.day,x.candidates,x.wins,x.losses,x.win_pct,x.avg_net_pct]));$('csv').href='/csv/features?window='+n;detail()}
$('group').onchange=$('win').onchange=show;$('day').onchange=$('search').onclick=()=>detail();$('prev').onclick=()=>detail(page-1);$('next').onclick=()=>detail(page+1);
$('go').onclick=async()=>{try{$('go').disabled=true;let r=await fetch('/reverse7/start',{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 연구 실행 중');result=null;seq++;$('out').hidden=true;await poll()}catch(e){$('phase').textContent=e.message;$('go').disabled=false}};
async function poll(){try{let s=await(await fetch('/status',{cache:'no-store'})).json();$('go').disabled=s.status==='running';if(s.mode!=='reverse7'){$('out').hidden=true;result=null;$('phase').textContent=s.status==='running'?'다른 연구 실행 중':'대기';return}$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 '+(s.market_success??0)+'/'+(s.universe??0)+' · 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');if(s.status==='running'){result=null;$('out').hidden=true}if(s.status==='done'&&!result&&!loading){loading=true;try{let r=await fetch('/results');if(!r.ok)throw Error(r.status);result=await r.json();$('out').hidden=false;show()}finally{loading=false}}}catch(e){$('phase').textContent='연결 확인: '+e.message}}
poll();setInterval(poll,2000);
</script></html>"""


# V0.8: twenty-day trend and volume recovery only; maximum seven days.
def exit8(by,t,target):
    seq=[by.get(t+i*DAY) for i in range(7)]
    if not all(seq) or any(min(r[1:5])<=0 for r in seq):return None
    entry=seq[0][1]
    limit=entry*(1+target/100)*(1+FEE3)/(1-FEE3)
    hit=next((i for i,r in enumerate(seq) if r[2]>=limit),None)
    i=6 if hit is None else hit
    price=seq[i][4] if hit is None else limit
    net=100*(price/entry*(1-FEE3)/(1+FEE3)-1)
    return dict(buy_open=entry,sell_price=price,target_price=limit,exit_day=iso(t+i*DAY)[:10],exit_start=t+i*DAY,holding_days=i+1,target_hit=hit is not None,exit_reason="TARGET" if hit is not None else "DAY7",net_pct=target if hit is not None else net)
def stats8(a):
    n=len(a);w=sum(x["net_pct"]>1e-10 for x in a);l=sum(x["net_pct"]< -1e-10 for x in a)
    return dict(trades=n,wins=w,losses=l,flat=n-w-l,win_pct=round(100*w/n,2) if n else None,target_hits=sum(x["target_hit"] for x in a),avg_net_pct=round(mean([x["net_pct"] for x in a]),4) if n else None,median_net_pct=round(statistics.median(x["net_pct"] for x in a),4) if n else None,avg_holding_days=round(mean([x["holding_days"] for x in a]),2) if n else None,worst_net_pct=round(min(x["net_pct"] for x in a),4) if n else None)
def volume8(rows,cut):
    h=[r for r in rows if r[0]+DAY<=cut]
    if len(h)<21 or h[-1][0]!=cut-DAY or any(b[0]-a[0]!=DAY for a,b in zip(h[-21:],h[-20:])):return None
    return ratio(h[-1][5],mean([r[5] for r in h[-21:-1]]))
def eval8(data,starts,end,target,progress=None):
    indexed={m:{r[0]:r for r in rows} for m,rows in data.items()}
    trades=[];daily=[];busy={}
    for i,t in enumerate(starts):
        label=iso(t)[:10];mature=t+7*DAY<=end
        eligible=signals=overlap=missing=0;a=[]
        if mature:
            for m,rows in data.items():
                by=indexed[m]
                if t-DAY not in by:continue
                v=trend6(rows,t,20)
                if not v["valid"]:continue
                d1=volume8(rows,t);d3=volume8(rows,t-2*DAY)
                if d1 is None or d3 is None:continue
                eligible+=1
                if not (v["downtrend"] and d1>d3):continue
                signals+=1
                if busy.get(m,-1)>=t:overlap+=1;continue
                out=exit8(by,t,target)
                if out is None:missing+=1;continue
                busy[m]=out["exit_start"]
                a.append(dict(day=label,market=m,target_net_pct=target,down_days=v["down_days"],trend_net_pct=v["net_pct"],peak_to_last_pct=v["peak_to_last_close_pct"],vol_ratio_d3=d3,vol_ratio_d1=d1,**out))
        trades.extend(a)
        daily.append(dict(day=label,mature=mature,eligible=eligible,signals=signals,overlap_skipped=overlap,missing_future=missing,**stats8(a)))
        if progress:progress(i+1)
    good=[d for d in daily if d["mature"]]
    summary=[dict(target_net_pct=target,mature_days=len(good),pending_days=len(daily)-len(good),signals=sum(d["signals"] for d in good),overlap_skipped=sum(d["overlap_skipped"] for d in good),missing_future=sum(d["missing_future"] for d in good),**stats8(trades))]
    return trades,summary,daily
def run8(target):
    begin=time.monotonic()
    try:
        end=int(now().timestamp())
        universe=sorted({x["market"] for x in api("market/all") if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        probe=candles("KRW-BTC",1440,end,30,latest=True)
        if not universe or len(probe)<2:raise RuntimeError("시장 목록 또는 BTC 일봉 부족")
        anchor=probe[-1][0]%DAY;current=day_start(end,anchor)
        if probe[-1][0]!=current:raise RuntimeError("BTC 일봉 지연")
        first=day_start(int(datetime(2026,10,28,12,tzinfo=KST).timestamp()),anchor)
        starts=[first+i*DAY for i in range(28)]
        patch(phase="전체 일봉 1회 수집",done=0,total=len(universe),universe=len(universe))
        data={}
        for m,rows in parallel(universe,lambda m:candles(m,1440,end,200,latest=True)):
            if rows and all(r[0]%DAY==anchor for r in rows):data[m]=rows
            else:record_error(m+" 일봉 누락/경계 불일치")
        patch(phase="20일 하락 + 거래량 증가 / 7일 청산 검증",done=0,total=28,market_success=len(data))
        rows,summ,daily=eval8(data,starts,current,target,lambda i:patch(done=i))
        if not any(x["eligible"] for x in daily):raise RuntimeError("유효 비교 데이터 없음")
        patch(features=rows,summary=summ,coverage=daily,done=28,status="done",phase="완료",elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:record_error(str(e));patch(status="error",phase="실패")
def start8(target):
    with state_lock:
        if state["status"]=="running":return False
        state.clear();state.update(mode="reverse8",status="running",phase="준비",done=0,total=0,rank=[],features=[],summary=[],errors=[],coverage=[],target_net_pct=target)
    threading.Thread(target=run8,args=(target,),daemon=True).start()
    return True
HTML8=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE SWING V0.8</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1050px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:12px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:9px;margin:4px;border-radius:7px;text-decoration:none}button:disabled{opacity:.5}table{border-collapse:collapse;white-space:nowrap}td,th{padding:8px;border-bottom:1px solid #324355;text-align:right}td:first-child,th:first-child{text-align:left}.scroll{overflow:auto;max-height:480px}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}</style><h1>REVERSE SWING V0.8</h1><a href="/reverse7">V0.7 당일 비교</a><section><h2>20일 하락 추세 + 거래량 증가</h2><p>RSI 제외. D-3 대비 D-1 거래량 배수 증가. V0.7의 20일 가격 추세 정의 유지.</p><small>10/28~11/24 진입 후보 중 7일 모두 마감된 날짜만 검증. 다음 일봉 시가 진입, 7일 이내 일봉 고가가 목표가에 도달하면 목표가 청산 가정, 미도달 시 7일째 종가 청산. 편도 수수료 0.25%, 별도 손절 없음. 보유 중 같은 종목 재진입 금지, 청산 다음 날부터 재진입 가능. 호가 단위·체결·슬리피지 미반영. 현재 상장 목록 기준, 동일 기간 연구.</small><p>순수익 목표(%) <input id="target" type="number" min="0.01" max="50" step="0.01" value="0.5" style="width:80px"><button id="go">7일 스윙 검증 시작</button></p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section id="out" hidden><a href="/csv/summary">승률 CSV</a><a href="/csv/features">전체 매매 CSV</a><a href="/csv/coverage">일별 결과 CSV</a><a href="/csv/errors">오류 CSV</a><h2>7일 스윙 결과</h2><div id="headline"></div><div class="scroll" id="tab"></div><h2>일별 진입 결과</h2><div class="scroll" id="daily"></div><h2>개별 매매</h2><select id="day"><option value="">전체 날짜</option></select><select id="result"><option value="all">전체</option><option value="win">수익</option><option value="loss">손실</option></select><input id="query" placeholder="코인 검색" style="width:100px"><button id="search">조회</button><p id="count"></p><div class="scroll" id="detail"></div><button id="prev">이전</button><button id="next">다음</button></section><script>
const $=id=>document.getElementById(id);let result=null,page=1,pages=1,seq=0,loading=false;
function table(id,heads,rows){let t=document.createElement('table'),tr=t.insertRow();for(const h of heads){let th=document.createElement('th');th.textContent=h;tr.append(th)}for(const row of rows){let tr=t.insertRow();for(const v of row)tr.insertCell().textContent=v??'—'}$(id).replaceChildren(t)}
function fmt(x){return typeof x==='number'?Number(x.toPrecision(7)):x}
async function detail(p=1){if(!result)return;let token=++seq;try{let q=new URLSearchParams({day:$('day').value,result:$('result').value,query:$('query').value,page:String(p)});let r=await fetch('/reverse8/details?'+q,{cache:'no-store'});if(!r.ok)throw Error(r.status);let d=await r.json();if(token!==seq)return;page=d.page;pages=d.pages;$('count').textContent=d.total+'건 · '+page+'/'+pages+' 페이지';table('detail',['진입일','코인','매수가','청산일','매도가','보유일','순수익%','청산','고점 대비 하락%','거래량 D-3','거래량 D-1'],d.rows.map(x=>[x.day,x.market,fmt(x.buy_open),x.exit_day,fmt(x.sell_price),x.holding_days,fmt(x.net_pct),x.target_hit?'목표익절':'7일종가',fmt(x.peak_to_last_pct),fmt(x.vol_ratio_d3),fmt(x.vol_ratio_d1)]));$('prev').disabled=page<=1;$('next').disabled=page>=pages}catch(e){if(token===seq)$('count').textContent='조회 실패: '+e.message}}
function show(){let x=result.summary[0];$('headline').textContent='순수익 목표 '+x.target_net_pct+'% · 검증 '+x.mature_days+'일 · 7일 미경과 제외 '+x.pending_days+'일';table('tab',['매수','수익','손실','보합','승률%','목표익절','평균 순수익%','평균 보유일','최대 손실성 수익률%','중복 제외','일봉 누락 제외'],[[x.trades,x.wins,x.losses,x.flat,x.win_pct,x.target_hits,x.avg_net_pct,x.avg_holding_days,x.worst_net_pct,x.overlap_skipped,x.missing_future]]);let d=result.coverage.filter(x=>x.mature);let o=document.createElement('option');o.value='';o.textContent='전체 날짜';$('day').replaceChildren(o,...d.map(x=>{let a=document.createElement('option');a.value=x.day;a.textContent=x.day;return a}));table('daily',['진입일','신호','매수','수익','손실','승률%','평균 순수익%'],d.map(x=>[x.day,x.signals,x.trades,x.wins,x.losses,x.win_pct,x.avg_net_pct]));detail()}
$('day').onchange=$('result').onchange=$('search').onclick=()=>detail();$('prev').onclick=()=>detail(page-1);$('next').onclick=()=>detail(page+1);
$('go').onclick=async()=>{try{let t=Number($('target').value);if(!Number.isFinite(t)||t<.01||t>50)throw Error('목표 0.01~50%');$('go').disabled=true;let r=await fetch('/reverse8/start?target='+encodeURIComponent(t),{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 연구 실행 중 또는 설정 오류');result=null;seq++;$('out').hidden=true;await poll()}catch(e){$('phase').textContent=e.message;$('go').disabled=false}};
async function poll(){try{let s=await(await fetch('/status',{cache:'no-store'})).json();$('go').disabled=s.status==='running';if(s.mode!=='reverse8'){$('out').hidden=true;result=null;$('phase').textContent=s.status==='running'?'다른 연구 실행 중':'대기';return}$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 '+(s.market_success??0)+'/'+(s.universe??0)+' · 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');if(s.status==='running'){result=null;$('out').hidden=true}if(s.status==='done'&&!result&&!loading){loading=true;try{let r=await fetch('/results');if(!r.ok)throw Error(r.status);result=await r.json();$('out').hidden=false;show()}finally{loading=false}}}catch(e){$('phase').textContent='연결 확인: '+e.message}}
poll();setInterval(poll,2000);
</script></html>"""

HTML2=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE MINI V0.2</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1050px;margin:auto;padding:16px}button,a,select{display:inline-block;color:white;background:#225b51;padding:10px;margin:5px;border:1px solid #476073;border-radius:7px;text-decoration:none}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}table{border-collapse:collapse;white-space:nowrap}td,th{padding:8px;border-bottom:1px solid #324355;text-align:right}td:first-child,th:first-child{text-align:left}progress{width:100%}.scroll{overflow:auto}pre{white-space:pre-wrap}small{color:#b4c4d1}</style><h1>REVERSE MINI V0.2</h1><a href="/stage3">V0.3.3 스윙 연구</a><a href="/">1단계 급등 종목 연구</a><section><h2>2단계 전체 종목 백테스트</h2><p>발견 기간 11/11~11/24 · 이전 비교 기간 10/28~11/10 (2026). 오늘 제외. 일 시작 전 완료 일봉만 사용. 결과는 당일 종가 변화이며 매매 수익률 아님.</p><button id="go">전체 코인 검증 시작</button><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section id="out" hidden><a href="/csv/summary">조건 비교 CSV</a><a href="/csv/features">전체 종목 CSV</a><a href="/csv/coverage">수집률 CSV</a><a href="/csv/errors">오류 CSV</a><p><select id="group"><option value="discovery">발견 기간</option><option value="earlier_holdout">이전 비교 기간</option></select><select id="scope"><option value="all">전체 유효</option><option value="prior_value_100m">전일 거래대금 1억 이상</option></select></p><small>기준 대비 5% 상승률 배수 = 조건군 발생률 / 전체 발생률. TOP10 포착률은 해당 범위 내 유효 TOP10 기준.</small><div class="scroll" id="tab"></div><div class="scroll" id="coverage"></div></section><script>
const $=x=>document.getElementById(x);let result=null;
function table(id,heads,rows){const t=document.createElement('table'),h=t.insertRow();for(let v of heads){const th=document.createElement('th');th.textContent=v;h.append(th)}for(let r of rows){const tr=t.insertRow();for(let v of r)tr.insertCell().textContent=v??'—'}$(id).replaceChildren(t)}
function show(){if(!result)return;const rows=result.summary.filter(x=>x.group===$('group').value&&x.scope===$('scope').value);table('tab',['조건','대상','일평균','TOP10 포착','TOP10 적중%','5% 이상%','10% 이상%','5% 배수','10% 배수'],rows.map(x=>[x.name,x.candidates,x.per_day,x.top10_hits,x.top10_precision_pct,x.hit5_pct,x.hit10_pct,x.lift5,x.lift10]));table('coverage',['날짜','순위 유효','조건 유효','TOP10 유효'],result.coverage.filter(x=>x.group===$('group').value).map(x=>[x.day,x.valid_markets,x.feature_valid,x.top10_valid]))}
$('group').onchange=$('scope').onchange=show;
$('go').onclick=async()=>{try{$('go').disabled=true;let r=await fetch('/stage2/start',{method:'POST'});if(!r.ok)throw Error(await r.text());result=null;$('out').hidden=true;await poll()}catch(e){$('phase').textContent=e.message;$('go').disabled=false}};
async function poll(){try{let s=await(await fetch('/status',{cache:'no-store'})).json();$('go').disabled=s.status==='running';if(s.mode!=='stage2'){$('phase').textContent=s.status==='running'?'1단계 실행 중':'대기';return}$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 '+(s.market_success??0)+'/'+(s.universe??0)+' · 오류 '+s.error_count;$('errors').textContent=s.errors.join('\n');if((s.status==='done'||s.status==='error')&&!result){result=await(await fetch('/results')).json();$('out').hidden=false;show()}}catch(e){$('phase').textContent='연결 확인: '+e.message}}
poll();setInterval(poll,2000);
</script></html>"""

HTML=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE MINI V0.1.3</title><style>
*{box-sizing:border-box}body{margin:0 auto;max-width:1050px;padding:16px;background:#10151d;color:#edf2f7;font:14px system-ui,sans-serif}h1{font-size:23px;margin:8px 0}h2{font-size:17px;margin:18px 0 8px}.card{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}label{display:inline-block;margin:6px}input,select,button,a.btn{background:#263646;color:#fff;border:1px solid #476073;border-radius:7px;padding:9px;font:inherit}button,a.btn{cursor:pointer;background:#225b51;text-decoration:none;display:inline-block;margin:4px}button:disabled{opacity:.5}.muted{color:#b4c4d1;line-height:1.6}progress{width:100%;accent-color:#37b89d}.scroll{overflow:auto;max-height:470px}table{border-collapse:collapse;width:100%;white-space:nowrap;font-size:13px}th,td{padding:8px;text-align:left;border-bottom:1px solid #324355}th{position:sticky;top:0;background:#253443}.pos{color:#6be3b7}pre{white-space:pre-wrap;overflow-wrap:anywhere}small{color:#b4c4d1}</style>
<h1>REVERSE MINI V0.1.3</h1><a class="btn" href="/stage3">V0.3.3 스윙 연구</a><a class="btn" href="/stage2">2단계 전체 코인 검증</a><div class="muted">빗썸 원화마켓 · 일별 상승 TOP 10 · 급등 전 공통점 연구 전용</div>
<div class="card"><label>완료 일수 <select id="days"><option>3</option><option>7</option><option selected>14</option></select></label><label>급등 기준 <select id="threshold"><option>3</option><option selected>5</option><option>10</option></select>%</label><label><input id="today" type="checkbox" checked> 오늘 미완료 별도</label><button id="go">백테스트 시작</button><div class="muted">일봉 경계는 BTC 실제 수신 시간으로 자동 판별 (현재 관측 KST 00:00). 전일 종가 대비 당일 종가 상승률 순위. 최초 기준 도달 5분봉 종가 이전 완료봉만 분석. 현재 상장 목록 기준이며 스테이블코인 목록은 제한적. 짧은 이력(이전 일봉 14개 미만)은 별도. 매수 조건 없음.</div></div>
<div class="card"><strong id="phase">대기</strong> <span id="count"></span><progress id="progress" value="0" max="1"></progress><div id="stats" class="muted"></div></div>
<div id="output" hidden><div class="card"><h2>CSV 저장</h2><a class="btn" href="/csv/rank">일별 TOP10</a><a class="btn" href="/csv/features">상세 지표</a><a class="btn" href="/csv/summary">공통점</a><a class="btn" href="/csv/coverage">수집률</a><a class="btn" href="/csv/errors">오류</a><div class="muted">확정·분봉 공백·일 시작 전을 별도 집계. 공백 사례는 최초 관측 시점 기준으로 탐색 전용. 일 시작 전은 최초 급등 시점과 독립적. 분모는 해당 지표의 유효 데이터 수이며, 예측 성과나 수익률이 아님.</div></div>
<div class="card"><h2>공통점 발견율</h2><label>구분 <select id="cohort"><option value="regular">완료일 일반</option><option value="short_history">짧은 이력</option><option value="partial">오늘 미완료 (확정)</option><option value="gap_regular">완료일 분봉 공백 (탐색)</option><option value="gap_partial">오늘 분봉 공백 (탐색)</option><option value="pre_regular">완료일 일 시작 전</option><option value="pre_partial">오늘 일 시작 전</option><option value="pre_short_history">짧은 이력 일 시작 전</option></select></label><label>시점 <select id="offset"><option>5분</option><option>15분</option><option>30분</option><option>1시간</option><option>4시간</option><option>24시간</option><option>3일</option><option>7일</option><option>일 시작 전</option><option>일 시작 3일 전</option><option>일 시작 7일 전</option></select></label><label><input type="checkbox" id="pairs"> 2개 조건 조합</label><div class="scroll" id="summary"></div></div>
<div class="card"><h2>일별 상승 TOP10</h2><div class="scroll" id="rank"></div></div><div class="card"><h2>일별 수집 범위</h2><div class="scroll" id="coverage"></div></div></div><div class="card"><details><summary>수집 오류 <span id="errCount">0</span>건</summary><pre id="errors"></pre></details></div>
<script>
const $=id=>document.getElementById(id);let result=null;
function table(el,headers,rows){let t=document.createElement('table'),h=t.createTHead().insertRow();headers.forEach(v=>{let th=document.createElement('th');th.textContent=v;h.appendChild(th)});let b=t.createTBody();rows.forEach(row=>{let tr=b.insertRow();row.forEach(v=>{let td=tr.insertCell();td.textContent=v??'—'})});$(el).replaceChildren(t)}
function summary(){if(!result)return;let a=result.summary.filter(x=>x.cohort===$('cohort').value&&x.offset===$('offset').value&&x.pattern.includes('+')===$('pairs').checked);a.sort((a,b)=>(b.hit_pct??-1)-(a.hit_pct??-1)||b.available-a.available);table('summary',['공통점','발견','유효','대상','발견율'],a.map(x=>[x.name,x.hits,x.available,x.cases,x.hit_pct==null?'—':x.hit_pct.toFixed(1)+'%']))}
$('cohort').onchange=()=>{if($('cohort').value.startsWith('pre_'))$('offset').value='일 시작 전';else if($('offset').value.startsWith('일 시작'))$('offset').value='5분';summary()};['offset','pairs'].forEach(id=>$(id).onchange=summary);
$('go').onclick=async()=>{try{$('go').disabled=true;const r=await fetch('/start?days='+$('days').value+'&threshold='+$('threshold').value+'&today='+($('today').checked?1:0),{method:'POST'});if(!r.ok)throw Error(await r.text());$('output').hidden=true;result=null;await poll()}catch(e){$('phase').textContent='시작 실패: '+e.message;$('go').disabled=false}};
async function poll(){try{let r=await fetch('/status',{cache:'no-store'});if(!r.ok)throw Error(r.status);let s=await r.json();$('phase').textContent=s.phase;$('count').textContent=s.done+' / '+s.total;$('progress').max=Math.max(1,s.total);$('progress').value=s.done;$('go').disabled=s.status==='running';$('stats').textContent='시장 수집 '+(s.market_success??0)+' / '+(s.universe??0)+' · 순위 '+s.rank_count+'건 · 상세 '+s.feature_count+'건';$('errors').textContent=s.errors.join('\n');$('errCount').textContent=s.error_count;if(s.mode==='stage2'||s.mode==='stage3'||s.mode==='reverse4'||s.mode==='reverse5'||s.mode==='reverse6'||s.mode==='reverse7'){$('output').hidden=true;return}if((s.status==='done'||s.status==='error')&&!result){let x=await fetch('/results');if(!x.ok)throw Error(x.status);result=await x.json();$('output').hidden=false;summary();table('rank',['일봉 날짜','순위','코인','상승률','급등 확인','시점','구분'],result.rank.map(x=>[x.day,x.rank,x.market,x.gain_pct.toFixed(2)+'%',x.event_status,x.event_kst,x.partial?'미완료':x.short_history?'짧은 이력':'일반']));table('coverage',['일자','유효 시장','상승 시장','선정'],result.coverage.map(x=>[x.day+(x.partial?' (미완료)':''),x.valid_markets,x.positive_markets,x.selected]))}}catch(e){$('phase').textContent='연결 재확인 중: '+e.message} }
poll();setInterval(poll,2000);
</script></html>"""
class Handler(BaseHTTPRequestHandler):
    def send(self,body,ctype="application/json; charset=utf-8",status=200,filename=None):
        if not isinstance(body,bytes):body=body.encode()
        self.send_response(status);self.send_header("Content-Type",ctype);self.send_header("Content-Length",str(len(body)));self.send_header("Cache-Control","no-store")
        if filename:self.send_header("Content-Disposition",'attachment; filename="'+filename+'"')
        self.end_headers()
        try:self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):pass
    def js(self,data,status=200):self.send(json.dumps(data,ensure_ascii=False,allow_nan=False),status=status)
    def do_POST(self):
        u=urlparse(self.path)
        if u.path=="/reverse8/start":
            try:
                target=float(parse_qs(u.query).get("target",["0.5"])[0])
                if not math.isfinite(target) or not .01<=target<=50:raise ValueError()
            except ValueError:return self.js({"error":"invalid target"},400)
            return self.js({"started":start8(target)})
        if u.path=="/reverse7/start":return self.js({"started":start7()})
        if u.path=="/reverse6/start":return self.js({"started":start6()})
        if u.path=="/reverse5/start":return self.js({"started":start5()})
        if u.path=="/reverse4/start":
            try:
                days=int(parse_qs(u.query).get("days",["14"])[0])
                if days not in (14,28):raise ValueError()
            except ValueError:return self.js({"error":"invalid days"},400)
            return self.js({"started":start4(days)})
        if u.path=="/stage3/start":return self.js({"started":start3()})
        if u.path=="/stage2/start":return self.js({"started":start2()})
        if u.path!="/start":return self.js({"error":"not found"},404)
        q=parse_qs(u.query)
        try:
            days=int(q.get("days",["14"])[0]);threshold=int(q.get("threshold",["5"])[0]);today=q.get("today",["1"])[0]=="1"
            if days not in (3,7,14) or threshold not in (3,5,10):raise ValueError()
        except ValueError:return self.js({"error":"invalid settings"},400)
        return self.js({"started":start_job(days,threshold,today)})
    def do_GET(self):
        u=urlparse(self.path); p=u.path
        if p=="/":return self.send(HTML,"text/html; charset=utf-8")
        if p=="/stage2":return self.send(HTML2,"text/html; charset=utf-8")
        if p=="/stage3":return self.send(HTML3,"text/html; charset=utf-8")
        if p=="/reverse8":return self.send(HTML8,"text/html; charset=utf-8")
        if p=="/reverse8/details":
            q=parse_qs(u.query)
            try:
                page=int(q.get("page",["1"])[0]);kind=q.get("result",["all"])[0]
                day=q.get("day",[""])[0];query=q.get("query",[""])[0].strip().upper()[:40]
                if kind not in ("all","win","loss"):raise ValueError()
            except ValueError:return self.js({"error":"invalid filters"},400)
            with state_lock:
                if state.get("mode")!="reverse8" or state["status"]!="done":return self.js({"error":"not complete"},409)
                rows=state["features"]
            a=[x for x in rows if (not day or x["day"]==day) and query in x["market"] and (kind=="all" or (kind=="win" and x["net_pct"]>1e-10) or (kind=="loss" and x["net_pct"]< -1e-10))]
            a.sort(key=lambda x:(x["day"],x["market"]))
            pages=max(1,(len(a)+49)//50);page=min(max(1,page),pages)
            return self.js(dict(total=len(a),page=page,pages=pages,rows=a[(page-1)*50:page*50]))
        if p=="/reverse7":return self.send(HTML7,"text/html; charset=utf-8")
        if p=="/reverse7/details":
            q=parse_qs(u.query)
            try:
                n=int(q.get("window",["20"])[0]);page=int(q.get("page",["1"])[0])
                group=q.get("group",["all"])[0];day=q.get("day",[""])[0];query=q.get("query",[""])[0].strip().upper()[:40]
                if n not in WINDOW6 or group not in ("all","earlier","recent"):raise ValueError()
            except ValueError:return self.js({"error":"invalid filters"},400)
            with state_lock:
                if state.get("mode")!="reverse7" or state["status"]!="done":return self.js({"error":"not complete"},409)
                rows=state["features"]
            a=[x for x in rows if x["window"]==n and (group=="all" or x["group"]==group) and (not day or x["day"]==day) and query in x["market"]]
            a.sort(key=lambda x:(x["day"],x["market"]))
            pages=max(1,(len(a)+49)//50);page=min(max(1,page),pages)
            return self.js(dict(total=len(a),page=page,pages=pages,rows=a[(page-1)*50:page*50]))
        if p=="/reverse6":return self.send(HTML6,"text/html; charset=utf-8")
        if p=="/reverse6/details":
            with state_lock:
                if state.get("mode")!="reverse6" or state["status"]!="done":return self.js({"error":"not complete"},409)
                rows=state["features"]
            return self.js(rows)
        if p=="/reverse5":return self.send(HTML5,"text/html; charset=utf-8")
        if p=="/reverse4":return self.send(HTML4,"text/html; charset=utf-8")
        if p=="/reverse4/details":
            q=parse_qs(u.query);day=q.get("day",[""])[0];m=q.get("market",[""])[0]
            with state_lock:
                if state.get("mode")!="reverse4" or state["status"]!="done":return self.js({"error":"not complete"},409)
                rows=[x for x in state["features"] if x["day"]==day and x["market"]==m]
                chart=state["charts"].get(day+"|"+m,[])
            return self.js({"rows":rows,"chart":chart})
        if p=="/stage3/details":
            q=parse_qs(u.query)
            try:
                group=q.get("group",["earlier"])[0]; horizon=int(q.get("horizon",["14"])[0])
                scope=q.get("scope",["all"])[0]; rule=q.get("rule",["D20_VOL2"])[0]
                query=q.get("query",[""])[0].strip().upper()[:40]; sort=q.get("sort",["date"])[0];page=int(q.get("page",["1"])[0])
                if group not in ("earlier","discovery") or horizon not in HORIZONS3 or scope not in ("all","prior_value_100m") or rule not in RULES2 or sort not in ("date","peak","worst","net"):raise ValueError()
            except ValueError:return self.js({"error":"잘못된 조회 조건"},400)
            with state_lock:
                if state.get("mode")!="stage3" or state["status"]!="done":return self.js({"error":"스윙 백테스트 완료 후 조회 가능"},409)
                rows=state["features"]
            return self.js(detail3(rows,group,horizon,scope,rule,query,sort,page))
        if p=="/health":return self.js({"version":VERSION,"ok":True})
        with state_lock:
            if p=="/status":
                x={k:v for k,v in state.items() if k not in ("rank","features","summary","errors","coverage","charts")}
                x.update(rank_count=len(state["rank"]),feature_count=len(state["features"]),error_count=len(state["errors"]),errors=state["errors"][-20:])
            elif p=="/results":x={k:state[k] for k in ("rank","summary","coverage")}
            elif p.startswith("/csv/"):
                key=p.split("/")[-1]
                if key=="features.zip":key="features"
                if key not in ("rank","features","summary","coverage","errors"):return self.js({"error":"not found"},404)
                if state["status"] not in ("done","error"):return self.js({"error":"not complete"},409)
                x=list(state[key]); mode=state.get("mode")
            else:return self.js({"error":"not found"},404)
        if not p.startswith("/csv/"):return self.js(x)
        if key=="errors":x=[{"error":v} for v in x]
        if mode=="stage3" and key=="features":
            q=parse_qs(u.query)
            horizon=q.get("horizon",[""])[0];group=q.get("group",[""])[0]
            if horizon and horizon not in tuple(str(h) for h in HORIZONS3):return self.js({"error":"invalid horizon"},400)
            if group and group not in ("earlier","discovery"):return self.js({"error":"invalid group"},400)
            x=[r for r in x if (not horizon or str(r["horizon"])==horizon) and (not group or r["group"]==group)]
            if p.endswith(".zip"):
                archive=io.BytesIO()
                with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=5) as z:
                    for cohort in ("earlier","discovery"):
                        for h in HORIZONS3:
                            subset=[r for r in x if r["group"]==cohort and r["horizon"]==h]
                            if not subset:continue
                            buf=io.StringIO();w=csv.DictWriter(buf,fieldnames=list(subset[0]))
                            w.writeheader();w.writerows(subset)
                            z.writestr("REVERSE_SWING_V0_3_3_FEATURES_"+cohort+"_"+str(h)+"D.csv",("\ufeff"+buf.getvalue()).encode("utf-8"))
                return self.send(archive.getvalue(),"application/zip",filename="REVERSE_SWING_V0_3_3_FEATURES_"+now().astimezone(KST).strftime("%Y%m%d%H%M")+".zip")
        if mode=="reverse7" and key=="features":
            n=parse_qs(u.query).get("window",[""])[0]
            if n and n not in tuple(str(v) for v in WINDOW6):return self.js({"error":"invalid window"},400)
            if n:x=[r for r in x if str(r["window"])==n]
        fields=list(dict.fromkeys(k for row in x for k in row))
        stream=io.StringIO();w=csv.DictWriter(stream,fieldnames=fields)
        if fields:w.writeheader();w.writerows(x)
        return self.send(("\ufeff"+stream.getvalue()).encode("utf-8"),"text/csv; charset=utf-8",filename=("REVERSE_SWING_V0_8_" if mode=="reverse8" else "REVERSE_SWING_V0_7_" if mode=="reverse7" else "REVERSE_SWING_V0_6_1_" if mode=="reverse6" else "REVERSE_SWING_V0_5_" if mode=="reverse5" else "REVERSE_SWING_V0_4_" if mode=="reverse4" else "REVERSE_SWING_V0_3_3_" if mode=="stage3" else "REVERSE_MINI_V0_2_" if mode=="stage2" else "REVERSE_MINI_V0_1_3_")+key.upper()+("_"+n+"D" if mode=="reverse7" and key=="features" and n else "")+("_"+group+"_"+horizon+"D" if mode=="stage3" and key=="features" and horizon and group else "")+"_"+now().astimezone(KST).strftime("%Y%m%d%H%M")+".csv")
def selftest():
    start=20000*DAY
    assert kststr(start).endswith("09:00:00")
    midnight=start-9*3600
    assert iso(midnight).endswith("00:00")
    assert day_start(midnight+17*3600,midnight%DAY)==midnight
    assert day_start(start+17*3600,start%DAY)==start
    for boundary in (midnight,start):
        d=[(boundary-DAY,100,100,100,100,1,100),(boundary,100,130,100,120,1,120)]
        ranked,cov=rank_days({"KRW-A":d},[boundary],boundary+DAY,5)
        assert len(ranked)==1 and cov[0]["valid_markets"]==1
    rows=[(start+i*300,100,106,99,100+i,1,100) for i in range(10)]
    assert first_event(rows,start,start+3000,100,5)==start+1800
    assert first_event(rows,start,start+1500,100,5) is None
    r=[(start+i*300,100+i,102+i,99+i,101+i,10,1000) for i in range(50)]
    f=features(r,5,start+40*300)
    assert f["valid"] and f["price"]==140 and f["bars"]==40 and f["ma60_distance"] is None
    assert not features(r[:-1],5,start+50*300)["valid"]
    daily=[(start-DAY,100,100,100,100,1,100),(start,100,130,100,120,1,120)]
    rank,cov=rank_days({"KRW-A":daily},[start],start+DAY,5)
    assert rank[0]["market"]=="KRW-A" and abs(rank[0]["gain_pct"]-20)<1e-8 and rank[0]["short_history"]
    assert not summarize([])[0]["available"]
    assert f["gaps_last20"]==0
    assert isinstance(f["negative_20bar"],bool)
    sample=dict(f,offset="일 시작 전",partial=False,short_history=False,event_status="pre_day")
    assert any(x["cohort"]=="pre_regular" and x["pattern"]=="negative_20bar" and x["available"]==1 for x in summarize([sample]))
    hist=[(start+i*DAY,100,101,99,100-i,10,1000) for i in range(40)]
    x=daily2(hist,start+35*DAY)
    assert x and x["ret20"]<0 and rules2(x,0)["D20_R40"]
    rr,ss,cc=eval2({"KRW-BTC":hist,"KRW-A":hist},[start+35*DAY],0)
    assert len(rr)==2 and cc[0]["feature_valid"]==2
    future={start:(start,100,112,96,105,1,100),start+DAY:(start+DAY,105,121,90,110,1,100)}
    o=outcome3(future,start,2,start+2*DAY)
    assert o["hit10"] and o["day_to_10"]==1 and o["hit20"] and o["down5"] and abs(o["net_close_pct"]-100*(1.1*(1-FEE3)/(1+FEE3)-1))<1e-8
    assert outcome3(future,start,2,start+DAY) is None and outcome3(future,start,3,start+3*DAY) is None
    rr,ss,cc=eval3({"KRW-BTC":hist,"KRW-A":hist},[("earlier",start+35*DAY)],start+40*DAY)
    assert len(rr)==8 and len(cc)==7 and all(not x["mature"] for x in cc if x["horizon"] in (7,10,14))
    long_future={start+i*DAY:(start+i*DAY,100,110 if i<13 else 125,90,105,1,100) for i in range(14)}
    assert outcome3(long_future,start,10,start+14*DAY)["hit20"] is False
    assert outcome3(long_future,start,14,start+14*DAY)["day_to_20"]==14
    assert outcome3(long_future,start,14,start+13*DAY) is None
    sample=dict(group="earlier",horizon=14,liquid=True,D20_VOL2=True,BASE=True,market="KRW-ELF",day="2026-10-15",peak_pct=20,worst_from_entry_pct=-5,net_close_pct=10)
    assert detail3([sample]*51,"earlier",14,"all","D20_VOL2","ELF","peak",2)["total"]==51
    assert len(detail3([sample]*51,"earlier",14,"all","D20_VOL2","ELF","peak",2)["rows"])==1
    assert detail3([sample],"earlier",14,"all","BASE","BTC","date",1)["total"]==0
    hist4=[(start+i*DAY,100+i,102+i,99+i,101+i,10,1000) for i in range(80)]
    t=start+60*DAY
    for n in LOOKBACK4:
        q=snapshot4(hist4,hist4,t,n)
        assert q["valid"] and q["price"]==101+60-n
        assert abs(q["ret_14"]-pct(101+60-n,101+60-n-14))<1e-8
    case4=dict(market="KRW-A",start=t,day=iso(t)[:10],rank=1,gain_pct=10)
    rr,ss,cc=reverse4({"KRW-A":hist4,"KRW-BTC":hist4},[case4])
    assert len(rr)==5 and len(cc[case4["day"]+"|KRW-A"])==21
    assert light5(hist4,t)[0]==snapshot4(hist4,hist4,t,1)["ret_20"]
    assert light5(hist4,t-2*DAY)[2]==snapshot4(hist4,hist4,t,3)["vol_ratio"]
    rr5,ff5,ss5,cc5=eval5({"KRW-A":hist4,"KRW-BTC":hist4},[t])
    assert len(ff5)==2 and len(ss5)==24 and cc5[0]["top10_valid"]==2
    assert all(not x["A"] and not x["B"] and not x["C"] for x in ff5)
    down6=[(start+i*DAY,100-i,102-i,98-i,100-i,10,1000) for i in range(30)]
    x6=trend6(down6,start+25*DAY,20)
    assert x6["valid"] and x6["downtrend"] and x6["down_days"]==20 and x6["all_days_down"]
    assert x6["peak_to_later_low_pct"]<0 and x6["drawdown_peak_date"]<x6["drawdown_low_date"]
    assert not trend6(down6[:15]+down6[16:],start+25*DAY,20)["valid"]
    assert not trend6(hist4,t,20)["downtrend"]
    rr6,ss6=eval6({"KRW-A":down6},[dict(case4,start=start+25*DAY)])
    assert len(rr6)==4 and len(ss6)==36
    assert all(x["indicators_valid"] and x["combined"] is False for x in rr6)
    vol6=[tuple(list(r[:5])+[100 if i==24 else 10,r[6]]) for i,r in enumerate(down6)]
    ff6,summary6=eval6({"KRW-A":vol6},[dict(case4,start=start+25*DAY)])
    assert all(x["combined"] is True for x in ff6)
    assert all(x["count"]==1 for x in summary6 if x["group"]=="all" and x["cohort"]=="combined")
    rank7,rows7,summ7,daily7=eval7({"KRW-A":vol6},[start+25*DAY])
    assert len(rows7)==4 and len(summ7)==12 and len(daily7)==4
    assert all(x["net_pct"]<0 and x["buy_open"]==x["sell_close"] for x in rows7)
    assert all(x["losses"]==1 for x in daily7)
    assert all(x["candidates"]==1 for x in summ7 if x["group"]=="all")
    assert stats7([])["win_pct"] is None
    synthetic8={start+i*DAY:(start+i*DAY,100,102 if i==2 else 100,98,99,10,1000) for i in range(7)}
    a8=exit8(synthetic8,start,.5)
    assert a8["target_hit"] and a8["holding_days"]==3 and a8["net_pct"]==.5
    b8=exit8(synthetic8,start,5)
    assert not b8["target_hit"] and b8["holding_days"]==7 and b8["net_pct"]<0
    assert exit8({k:v for k,v in synthetic8.items() if k!=start+6*DAY},start,.5) is None
    vol8=vol6+[(start+i*DAY,70,71,69,70,10,1000) for i in range(30,36)]
    r8,s8,d8=eval8({"KRW-A":vol8},[start+25*DAY,start+30*DAY],start+32*DAY,.5)
    assert len(r8)==1 and s8[0]["mature_days"]==1 and s8[0]["pending_days"]==1
    assert s8[0]["trades"]==1 and d8[0]["signals"]==1
    print("REVERSE SWING V0.8 self-test PASS",flush=True)
if __name__=="__main__":
    import sys
    if "--selftest" in sys.argv:selftest()
    else:
        selftest()
        ThreadingHTTPServer(("0.0.0.0",int(os.environ.get("PORT","8000"))),Handler).serve_forever()
