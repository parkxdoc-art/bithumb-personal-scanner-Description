import os, json, csv, io, time, math, threading, statistics, traceback, zipfile
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "REVERSE SWING V0.3.2"
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
HTML3=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE SWING V0.3.2</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1100px;margin:auto;padding:16px}button,a,select{display:inline-block;color:white;background:#225b51;padding:10px;margin:4px;border:1px solid #476073;border-radius:7px;text-decoration:none}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}table{border-collapse:collapse;white-space:nowrap}td,th{padding:8px;border-bottom:1px solid #324355;text-align:right}td:first-child,th:first-child{text-align:left}progress{width:100%}.scroll{overflow:auto}pre{white-space:pre-wrap}small{color:#b4c4d1;line-height:1.7}</style><h1>REVERSE SWING V0.3.2</h1><a href="/">1단계</a><a href="/stage2">2단계</a><section><h2>스윙 전용 전체 시장 백테스트</h2><p>발견 11/11~11/24 · 이전 비교 10/15~10/28 (2026). 일봉만 1회 수집. 1·2·3·5·7·10·14일 결과. 이전 기간은 14일 보유 종료가 발견 기간과 겹치지 않도록 조정. 최근 신호의 10·14일 결과는 기간 미완료 제외. 기간 비교는 이전 비교 구간을 우선 사용.</p><small>신호는 전일까지 완료된 일봉으로만 산출. 다음 일봉 시가 진입 가정. 종료 종가 수익률에 편도 0.25% 반영. 고가 도달은 익절 보장 아님. 저가는 진입가 대비 하락이며 고점 대비 낙폭이 아님. 기간 미완료·결측 제외, 중복 신호 포함. 자동 주문 없음.</small><p><button id="go">스윙 백테스트 시작</button></p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section id="out" hidden><a href="/csv/summary">조건 비교 CSV</a><a href="/csv/features.zip">상세 결과 ZIP (전체)</a><a id="detail" href="/csv/features?horizon=14&amp;group=earlier">선택 기간 상세 CSV</a><a href="/csv/coverage">수집률 CSV</a><a href="/csv/errors">오류 CSV</a><p><select id="group"><option value="earlier">이전 비교 기간</option><option value="discovery">발견 기간</option></select><select id="scope"><option value="all">전체 유효</option><option value="prior_value_100m">전일 거래대금 1억 이상</option></select><select id="horizon"><option>1</option><option>2</option><option>3</option><option>5</option><option>7</option><option>10</option><option selected>14</option></select>일</p><div class="scroll" id="tab"></div><div class="scroll" id="coverage"></div></section><script>
const $=x=>document.getElementById(x);let result=null;
function table(id,heads,rows){const t=document.createElement('table'),h=t.insertRow();for(let v of heads){const th=document.createElement('th');th.textContent=v;h.append(th)}for(let r of rows){const tr=t.insertRow();for(let v of r)tr.insertCell().textContent=v??'—'}$(id).replaceChildren(t)}
function show(){if(!result)return;const h=Number($('horizon').value);$('detail').href='/csv/features?horizon='+h+'&group='+$('group').value;let rows=result.summary.filter(x=>x.group===$('group').value&&x.scope===$('scope').value&&x.horizon===h);table('tab',['조건','유효 일수','신호건수','일평균','5% 도달%','10% 도달%','20% 도달%','10% 배수','최고 중앙%','최저 중앙%','-5% 하락%','순종가 중앙%','10%까지 일'],rows.map(x=>[x.name,x.days,x.candidates,x.per_day,x.hit5_pct,x.hit10_pct,x.hit20_pct,x.lift10,x.median_peak,x.median_worst,x.down5_pct,x.median_net_close,x.mean_day_to_10]));table('coverage',['날짜','기간완료','조건 유효','결과 유효'],result.coverage.filter(x=>x.group===$('group').value&&x.horizon===h).map(x=>[x.day,x.mature?'완료':'미완료',x.eligible,x.valid]))}
['group','scope','horizon'].forEach(id=>$(id).onchange=show);
$('go').onclick=async()=>{try{$('go').disabled=true;let r=await fetch('/stage3/start',{method:'POST'});if(!r.ok)throw Error(await r.text());let v=await r.json();if(!v.started)throw Error('다른 연구 실행 중');result=null;$('out').hidden=true;await poll()}catch(e){$('phase').textContent=e.message;$('go').disabled=false}};
async function poll(){try{let s=await(await fetch('/status',{cache:'no-store'})).json();$('go').disabled=s.status==='running';if(s.mode!=='stage3'){$('out').hidden=true;$('phase').textContent=s.status==='running'?'다른 연구 실행 중':'대기';return}$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 '+(s.market_success??0)+'/'+(s.universe??0)+' · 오류 '+s.error_count+' · 수집 '+(s.fetch_seconds??'—')+'초 · 전체 '+(s.elapsed_seconds??'—')+'초'+(s.cache_used?' · 캐시 사용':'');$('errors').textContent=s.errors.join('\n');if((s.status==='done'||s.status==='error')&&!result){result=await(await fetch('/results')).json();$('out').hidden=false;show()}}catch(e){$('phase').textContent='연결 확인: '+e.message}}
poll();setInterval(poll,2000);
</script></html>"""

HTML2=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE MINI V0.2</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1050px;margin:auto;padding:16px}button,a,select{display:inline-block;color:white;background:#225b51;padding:10px;margin:5px;border:1px solid #476073;border-radius:7px;text-decoration:none}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}table{border-collapse:collapse;white-space:nowrap}td,th{padding:8px;border-bottom:1px solid #324355;text-align:right}td:first-child,th:first-child{text-align:left}progress{width:100%}.scroll{overflow:auto}pre{white-space:pre-wrap}small{color:#b4c4d1}</style><h1>REVERSE MINI V0.2</h1><a href="/stage3">V0.3.2 스윙 연구</a><a href="/">1단계 급등 종목 연구</a><section><h2>2단계 전체 종목 백테스트</h2><p>발견 기간 11/11~11/24 · 이전 비교 기간 10/28~11/10 (2026). 오늘 제외. 일 시작 전 완료 일봉만 사용. 결과는 당일 종가 변화이며 매매 수익률 아님.</p><button id="go">전체 코인 검증 시작</button><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section id="out" hidden><a href="/csv/summary">조건 비교 CSV</a><a href="/csv/features">전체 종목 CSV</a><a href="/csv/coverage">수집률 CSV</a><a href="/csv/errors">오류 CSV</a><p><select id="group"><option value="discovery">발견 기간</option><option value="earlier_holdout">이전 비교 기간</option></select><select id="scope"><option value="all">전체 유효</option><option value="prior_value_100m">전일 거래대금 1억 이상</option></select></p><small>기준 대비 5% 상승률 배수 = 조건군 발생률 / 전체 발생률. TOP10 포착률은 해당 범위 내 유효 TOP10 기준.</small><div class="scroll" id="tab"></div><div class="scroll" id="coverage"></div></section><script>
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
<h1>REVERSE MINI V0.1.3</h1><a class="btn" href="/stage3">V0.3.2 스윙 연구</a><a class="btn" href="/stage2">2단계 전체 코인 검증</a><div class="muted">빗썸 원화마켓 · 일별 상승 TOP 10 · 급등 전 공통점 연구 전용</div>
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
async function poll(){try{let r=await fetch('/status',{cache:'no-store'});if(!r.ok)throw Error(r.status);let s=await r.json();$('phase').textContent=s.phase;$('count').textContent=s.done+' / '+s.total;$('progress').max=Math.max(1,s.total);$('progress').value=s.done;$('go').disabled=s.status==='running';$('stats').textContent='시장 수집 '+(s.market_success??0)+' / '+(s.universe??0)+' · 순위 '+s.rank_count+'건 · 상세 '+s.feature_count+'건';$('errors').textContent=s.errors.join('\n');$('errCount').textContent=s.error_count;if(s.mode==='stage2'||s.mode==='stage3'){$('output').hidden=true;return}if((s.status==='done'||s.status==='error')&&!result){let x=await fetch('/results');if(!x.ok)throw Error(x.status);result=await x.json();$('output').hidden=false;summary();table('rank',['일봉 날짜','순위','코인','상승률','급등 확인','시점','구분'],result.rank.map(x=>[x.day,x.rank,x.market,x.gain_pct.toFixed(2)+'%',x.event_status,x.event_kst,x.partial?'미완료':x.short_history?'짧은 이력':'일반']));table('coverage',['일자','유효 시장','상승 시장','선정'],result.coverage.map(x=>[x.day+(x.partial?' (미완료)':''),x.valid_markets,x.positive_markets,x.selected]))}}catch(e){$('phase').textContent='연결 재확인 중: '+e.message} }
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
        if p=="/health":return self.js({"version":VERSION,"ok":True})
        with state_lock:
            if p=="/status":
                x={k:v for k,v in state.items() if k not in ("rank","features","summary","errors","coverage")}
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
                            z.writestr("REVERSE_SWING_V0_3_2_FEATURES_"+cohort+"_"+str(h)+"D.csv",("\ufeff"+buf.getvalue()).encode("utf-8"))
                return self.send(archive.getvalue(),"application/zip",filename="REVERSE_SWING_V0_3_2_FEATURES_"+now().astimezone(KST).strftime("%Y%m%d%H%M")+".zip")
        fields=list(dict.fromkeys(k for row in x for k in row))
        stream=io.StringIO();w=csv.DictWriter(stream,fieldnames=fields)
        if fields:w.writeheader();w.writerows(x)
        return self.send(("\ufeff"+stream.getvalue()).encode("utf-8"),"text/csv; charset=utf-8",filename=("REVERSE_SWING_V0_3_2_" if mode=="stage3" else "REVERSE_MINI_V0_2_" if mode=="stage2" else "REVERSE_MINI_V0_1_3_")+key.upper()+("_"+group+"_"+horizon+"D" if mode=="stage3" and key=="features" and horizon and group else "")+"_"+now().astimezone(KST).strftime("%Y%m%d%H%M")+".csv")
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
    print("REVERSE SWING V0.3.2 self-test PASS",flush=True)
if __name__=="__main__":
    import sys
    if "--selftest" in sys.argv:selftest()
    else:
        selftest()
        ThreadingHTTPServer(("0.0.0.0",int(os.environ.get("PORT","8000"))),Handler).serve_forever()
