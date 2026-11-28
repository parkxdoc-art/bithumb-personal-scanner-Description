import os, json, csv, io, time, math, threading, statistics, traceback, zipfile
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "REVERSE SWING V1.4.3 LIVE + PAPER"
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
HTML8=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE SWING V0.8</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1050px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:12px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:9px;margin:4px;border-radius:7px;text-decoration:none}button:disabled{opacity:.5}table{border-collapse:collapse;white-space:nowrap}td,th{padding:8px;border-bottom:1px solid #324355;text-align:right}td:first-child,th:first-child{text-align:left}.scroll{overflow:auto;max-height:480px}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}</style><h1>REVERSE SWING V0.8</h1><a href="/reverse81">V0.8.1 최대 수익</a><a href="/reverse7">V0.7 당일 비교</a><section><h2>20일 하락 추세 + 거래량 증가</h2><p>RSI 제외. D-3 대비 D-1 거래량 배수 증가. V0.7의 20일 가격 추세 정의 유지.</p><small>10/28~11/24 진입 후보 중 7일 모두 마감된 날짜만 검증. 다음 일봉 시가 진입, 7일 이내 일봉 고가가 목표가에 도달하면 목표가 청산 가정, 미도달 시 7일째 종가 청산. 편도 수수료 0.25%, 별도 손절 없음. 보유 중 같은 종목 재진입 금지, 청산 다음 날부터 재진입 가능. 호가 단위·체결·슬리피지 미반영. 현재 상장 목록 기준, 동일 기간 연구.</small><p>순수익 목표(%) <input id="target" type="number" min="0.01" max="50" step="0.01" value="0.5" style="width:80px"><button id="go">7일 스윙 검증 시작</button></p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section id="out" hidden><a href="/csv/summary">승률 CSV</a><a href="/csv/features">전체 매매 CSV</a><a href="/csv/coverage">일별 결과 CSV</a><a href="/csv/errors">오류 CSV</a><h2>7일 스윙 결과</h2><div id="headline"></div><div class="scroll" id="tab"></div><h2>일별 진입 결과</h2><div class="scroll" id="daily"></div><h2>개별 매매</h2><select id="day"><option value="">전체 날짜</option></select><select id="result"><option value="all">전체</option><option value="win">수익</option><option value="loss">손실</option></select><input id="query" placeholder="코인 검색" style="width:100px"><button id="search">조회</button><p id="count"></p><div class="scroll" id="detail"></div><button id="prev">이전</button><button id="next">다음</button></section><script>
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


# V0.8.1: maximum attainable profit research, not an executable exit strategy.
FEE81=.0004
def outcome81(by,t):
    seq=[by.get(t+i*DAY) for i in range(7)]
    if not all(seq) or any(min(r[1:5])<=0 for r in seq):return None
    entry=seq[0][1]
    def net(p):return 100*(p/entry*(1-FEE81)/(1+FEE81)-1)
    peak_i=max(range(7),key=lambda i:seq[i][2])
    peak=seq[peak_i][2];potential=net(peak);close=seq[-1][4]
    profitable=potential>1e-10
    exit_i=peak_i if profitable else 6
    price=peak if profitable else close
    return dict(buy_open=entry,peak_price=peak,peak_day=iso(t+peak_i*DAY)[:10],peak_day_number=peak_i+1,peak_net_pct=potential,profit_opportunity=profitable,hypothetical_exit_day=iso(t+exit_i*DAY)[:10],hypothetical_exit_price=price,net_pct=net(price),day7_close=close,day7_net_pct=net(close),worst_from_entry_pct=pct(min(r[3] for r in seq),entry),holding_days=exit_i+1,exit_reason="PEAK_HINDSIGHT" if profitable else "DAY7")
def stats81(a):
    n=len(a);w=sum(x["net_pct"]>1e-10 for x in a);l=sum(x["net_pct"]< -1e-10 for x in a)
    cw=sum(x["day7_net_pct"]>1e-10 for x in a);cl=sum(x["day7_net_pct"]< -1e-10 for x in a)
    def avg(k):return round(mean([x[k] for x in a]),4) if n else None
    def med(k):return round(statistics.median(x[k] for x in a),4) if n else None
    return dict(trades=n,profit_opportunities=w,no_profit=l,flat=n-w-l,opportunity_pct=round(100*w/n,2) if n else None,avg_hypothetical_net_pct=avg("net_pct"),median_hypothetical_net_pct=med("net_pct"),avg_peak_net_pct=avg("peak_net_pct"),median_peak_net_pct=med("peak_net_pct"),highest_peak_net_pct=round(max(x["peak_net_pct"] for x in a),4) if n else None,worst_hypothetical_net_pct=round(min(x["net_pct"] for x in a),4) if n else None,day7_wins=cw,day7_losses=cl,day7_flat=n-cw-cl,day7_win_pct=round(100*cw/n,2) if n else None,avg_day7_net_pct=avg("day7_net_pct"),median_day7_net_pct=med("day7_net_pct"),avg_peak_day=avg("peak_day_number"),hit_net_1=sum(x["peak_net_pct"]>=1 for x in a),hit_net_3=sum(x["peak_net_pct"]>=3 for x in a),hit_net_5=sum(x["peak_net_pct"]>=5 for x in a),hit_net_10=sum(x["peak_net_pct"]>=10 for x in a),hit_net_20=sum(x["peak_net_pct"]>=20 for x in a))
def eval81(data,starts,end,progress=None):
    indexed={m:{r[0]:r for r in rows} for m,rows in data.items()}
    trades=[];daily=[];busy={}
    for i,t in enumerate(starts):
        mature=t+7*DAY<=end
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
                if busy.get(m,-1)>t:overlap+=1;continue
                out=outcome81(by,t)
                if out is None:missing+=1;continue
                # Same fixed seven-day signal spacing for BOTH columns;
                # a future-known peak must never determine a new entry.
                busy[m]=t+7*DAY
                a.append(dict(day=iso(t)[:10],market=m,down_days=v["down_days"],trend_net_pct=v["net_pct"],peak_to_last_pct=v["peak_to_last_close_pct"],vol_ratio_d3=d3,vol_ratio_d1=d1,fee_oneway_pct=.04,**out))
        trades.extend(a)
        daily.append(dict(day=iso(t)[:10],mature=mature,eligible=eligible,signals=signals,overlap_skipped=overlap,missing_future=missing,**stats81(a)))
        if progress:progress(i+1)
    good=[d for d in daily if d["mature"]]
    summary=[dict(fee_oneway_pct=.04,mature_days=len(good),pending_days=len(daily)-len(good),signals=sum(d["signals"] for d in good),overlap_skipped=sum(d["overlap_skipped"] for d in good),missing_future=sum(d["missing_future"] for d in good),**stats81(trades))]
    return trades,summary,daily
def run81():
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
        patch(phase="7일 최대 수익 가능성 비교",done=0,total=28,market_success=len(data))
        rows,summ,daily=eval81(data,starts,current,lambda i:patch(done=i))
        if not any(x["eligible"] for x in daily):raise RuntimeError("유효 비교 데이터 없음")
        patch(features=rows,summary=summ,coverage=daily,done=28,status="done",phase="완료",elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:record_error(str(e));patch(status="error",phase="실패")
def start81():
    with state_lock:
        if state["status"]=="running":return False
        state.clear();state.update(mode="reverse81",status="running",phase="준비",done=0,total=0,rank=[],features=[],summary=[],errors=[],coverage=[])
    threading.Thread(target=run81,daemon=True).start()
    return True
HTML81=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE SWING V0.8.1</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1050px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:12px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:9px;margin:4px;border-radius:7px;text-decoration:none}button:disabled{opacity:.5}table{border-collapse:collapse;white-space:nowrap}td,th{padding:8px;border-bottom:1px solid #324355;text-align:right}td:first-child,th:first-child{text-align:left}.scroll{overflow:auto;max-height:480px}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}</style><h1>REVERSE SWING V0.8.1</h1><a href="/live">V1.0 실전 검색</a><a href="/reverse8">V0.8 고정익절 비교</a><section><h2>20일 하락 + 거래량 증가 / 7일 최대 수익</h2><p>RSI 제외 · 편도 수수료 0.04% · 고정 익절·손절 없음.</p><small>10/28~11/24 진입일 중 7일 모두 완료된 날만. 전일 마감 신호로 다음 일봉 시가 진입. 7일 고가 중 최고가가 순수익이면 그 가격에 사후 청산했다고 가정, 아니면 7일 종가. 이는 미래 최고가를 알아야 하는 이론적 상한으로 실제 승률 아님. 동일 진입에서 7일 종가 청산 수익률 별도 표시. 최고가 날짜가 다음 진입에 영향을 주지 않도록 같은 종목 7일간 재진입 금지(두 비교군 동일). 슬리피지·체결·호가단위 미반영. 현재 상장 목록 기준.</small><p><button id="go">7일 최대 수익 검증 시작</button></p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section id="out" hidden><a href="/csv/summary">결과 CSV</a><a href="/csv/features">전체 종목 CSV</a><a href="/csv/coverage">일별 CSV</a><a href="/csv/errors">오류 CSV</a><h2>최대 수익 가능성과 7일 종가 비교</h2><p id="headline"></p><div class="scroll" id="tab"></div><h2>7일 내 순수익 도달 분포</h2><div class="scroll" id="hits"></div><h2>일별 결과</h2><div class="scroll" id="daily"></div><h2>개별 종목</h2><select id="day"><option value="">전체 날짜</option></select><select id="kind"><option value="all">전체</option><option value="win">수익 기회 있음</option><option value="loss">수익 기회 없음</option></select><input id="query" placeholder="코인 검색" style="width:100px"><button id="search">조회</button><p id="count"></p><div class="scroll" id="detail"></div><button id="prev">이전</button><button id="next">다음</button></section><script>
const $=id=>document.getElementById(id);let result=null,page=1,pages=1,seq=0,loading=false;
function table(id,heads,rows){let t=document.createElement('table'),tr=t.insertRow();for(const h of heads){let th=document.createElement('th');th.textContent=h;tr.append(th)}for(const row of rows){let tr=t.insertRow();for(const v of row)tr.insertCell().textContent=v??'—'}$(id).replaceChildren(t)}
function fmt(x){return typeof x==='number'?Number(x.toPrecision(7)):x}
async function detail(p=1){if(!result)return;let token=++seq;try{let q=new URLSearchParams({day:$('day').value,kind:$('kind').value,query:$('query').value,page:String(p)});let r=await fetch('/reverse81/details?'+q,{cache:'no-store'});if(!r.ok)throw Error(r.status);let d=await r.json();if(token!==seq)return;page=d.page;pages=d.pages;$('count').textContent=d.total+'건 · '+page+'/'+pages+' 페이지';table('detail',['진입일','코인','매수가','최고가일','최고가','최대 순수익%','가정 수익%','7일 종가 순수익%','최저 하락%','거래량 D-3','거래량 D-1','고점 대비 하락%'],d.rows.map(x=>[x.day,x.market,fmt(x.buy_open),x.peak_day,fmt(x.peak_price),fmt(x.peak_net_pct),fmt(x.net_pct),fmt(x.day7_net_pct),fmt(x.worst_from_entry_pct),fmt(x.vol_ratio_d3),fmt(x.vol_ratio_d1),fmt(x.peak_to_last_pct)]));$('prev').disabled=page<=1;$('next').disabled=page>=pages}catch(e){if(token===seq)$('count').textContent='조회 실패: '+e.message}}
function show(){let x=result.summary[0];$('headline').textContent='검증 '+x.mature_days+'일 · 7일 미경과 제외 '+x.pending_days+'일 · 중복 제외 '+x.overlap_skipped+'건 · 누락 '+x.missing_future+'건';table('tab',['구분','매수','수익','손실','보합','비율%','평균 순수익%','중앙값%'],[['최고가 사후 가정',x.trades,x.profit_opportunities,x.no_profit,x.flat,x.opportunity_pct,x.avg_hypothetical_net_pct,x.median_hypothetical_net_pct],['7일 종가 청산',x.trades,x.day7_wins,x.day7_losses,x.day7_flat,x.day7_win_pct,x.avg_day7_net_pct,x.median_day7_net_pct]]);table('hits',['순수익 목표','도달 건수','도달률%'],[1,3,5,10,20].map(k=>[k+'%',x['hit_net_'+k],x.trades?fmt(100*x['hit_net_'+k]/x.trades):null]));let d=result.coverage.filter(x=>x.mature);let o=document.createElement('option');o.value='';o.textContent='전체 날짜';$('day').replaceChildren(o,...d.map(x=>{let a=document.createElement('option');a.value=x.day;a.textContent=x.day;return a}));table('daily',['진입일','매수','수익 기회','기회율%','최고가 가정 평균%','7일 종가 승률%','7일 종가 평균%'],d.map(x=>[x.day,x.trades,x.profit_opportunities,x.opportunity_pct,x.avg_hypothetical_net_pct,x.day7_win_pct,x.avg_day7_net_pct]));detail()}
$('day').onchange=$('kind').onchange=$('search').onclick=()=>detail();$('prev').onclick=()=>detail(page-1);$('next').onclick=()=>detail(page+1);
$('go').onclick=async()=>{try{$('go').disabled=true;let r=await fetch('/reverse81/start',{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 연구 실행 중');result=null;seq++;$('out').hidden=true;await poll()}catch(e){$('phase').textContent=e.message;$('go').disabled=false}};
async function poll(){try{let s=await(await fetch('/status',{cache:'no-store'})).json();$('go').disabled=s.status==='running';if(s.mode!=='reverse81'){$('out').hidden=true;result=null;$('phase').textContent=s.status==='running'?'다른 연구 실행 중':'대기';return}$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 '+(s.market_success??0)+'/'+(s.universe??0)+' · 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');if(s.status==='running'){result=null;$('out').hidden=true}if(s.status==='done'&&!result&&!loading){loading=true;try{let r=await fetch('/results');if(!r.ok)throw Error(r.status);result=await r.json();$('out').hidden=false;show()}finally{loading=false}}}catch(e){$('phase').textContent='연결 확인: '+e.message}}
poll();setInterval(poll,2000);
</script></html>"""


# V1.0 live manual-entry candidate scanner. Same completed-candle rule as V0.8.1.
def candidate10(rows,cut,observed):
    v=trend6(rows,cut,20)
    if not v["valid"]:return None,"history"
    d1=volume8(rows,cut);d3=volume8(rows,cut-2*DAY)
    if d1 is None or d3 is None:return None,"history"
    if not(v["downtrend"] and d1>d3):return None,"no_signal"
    today=next((r for r in rows if r[0]==cut),None)
    if not today or today[1]<=0 or today[4]<=0:return None,"no_quote"
    p=today[4];opening=today[1]
    return dict(signal_day=iso(cut)[:10],signal_time=iso(cut),observed_at=iso(observed),reference_price=p,day_open=opening,change_from_open_pct=pct(p,opening),last_completed_close=v["end_close"],down_days=v["down_days"],trend_net_pct=v["net_pct"],peak_to_last_pct=v["peak_to_last_close_pct"],peak_to_low_pct=v["peak_to_later_low_pct"],vol_ratio_d3=d3,vol_ratio_d1=d1,volume_increase_pct=pct(d1,d3) if d3>0 else None,breakeven_price=p*(1+FEE81)/(1-FEE81),fee_oneway_pct=.04,planned_last_day=iso(cut+6*DAY)[:10],status="MATCH"),"match"
def run10():
    begin=time.monotonic()
    try:
        end=int(now().timestamp())
        universe=sorted({x["market"] for x in api("market/all") if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        probe=candles("KRW-BTC",1440,end,30,latest=True)
        if not universe or len(probe)<2:raise RuntimeError("시장 목록 또는 BTC 일봉 부족")
        anchor=probe[-1][0]%DAY;cut=day_start(end,anchor)
        if probe[-1][0]!=cut:raise RuntimeError("BTC 일봉 지연")
        patch(phase="전체 원화마켓 일봉 검색",done=0,total=len(universe),universe=len(universe),signal_time=iso(cut))
        out=[];coverage=[];success=0;valid=0
        def fetch(m):
            at=int(now().timestamp())
            rows=candles(m,1440,at,30,latest=True)
            return rows,int(now().timestamp())
        for m,pack in parallel(universe,fetch):
            if not pack:
                coverage.append(dict(market=m,status="fetch_error"));continue
            rows,observed=pack
            if not rows or any(r[0]%DAY!=anchor for r in rows):
                coverage.append(dict(market=m,status="invalid_candle"));record_error(m+" 일봉 누락/경계 불일치");continue
            success+=1
            item,why=candidate10(rows,cut,observed)
            if why not in ("history",):valid+=1
            coverage.append(dict(market=m,status=why))
            if item:out.append(dict(market=m,**item))
        if day_start(int(now().timestamp()),anchor)!=cut:raise RuntimeError("검색 중 일봉 경계 변경: 다시 검색해 주세요")
        if valid==0:raise RuntimeError("유효 분석 데이터 없음")
        out.sort(key=lambda x:x["market"])
        summary=[dict(signal_day=iso(cut)[:10],signal_time=iso(cut),finished_at=iso(int(now().timestamp())),universe=len(universe),market_success=success,valid=valid,matches=len(out),history_missing=sum(x["status"]=="history" for x in coverage),no_quote=sum(x["status"]=="no_quote" for x in coverage),fee_oneway_pct=.04)]
        patch(rank=out,features=out,summary=summary,coverage=coverage,done=len(universe),market_success=success,status="done",phase="완료",elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:record_error(str(e));patch(status="error",phase="실패")
def start10():
    with state_lock:
        if state["status"]=="running":return False
        state.clear();state.update(mode="live10",status="running",phase="준비",done=0,total=0,rank=[],features=[],summary=[],errors=[],coverage=[])
    threading.Thread(target=run10,daemon=True).start()
    return True
HTML10=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE SWING V1.1 LIVE + PAPER</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1000px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:10px;margin:4px;border-radius:7px;text-decoration:none}input{background:#10151d}button:disabled{opacity:.5}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:10px}.coin{background:#111923;border:1px solid #3b4d60;border-radius:12px;padding:12px}.coin h3{margin:0 0 8px}.line{display:flex;justify-content:space-between;gap:10px;margin:6px 0}.green{color:#72ddb0}.red{color:#ff9b9b}.muted{color:#a9bac9}h1{font-size:23px;margin-bottom:4px}</style><h1>REVERSE SWING V1.1 LIVE + PAPER</h1><a href="/reverse81">V0.8.1 백테스트</a><section><h2>실전 매수 후보 검색</h2><p>20일 하락 추세 + D-3 대비 D-1 거래량 배수 증가 · RSI 제외</p><small>V0.8.1과 동일한 마감 일봉 검색식. 신호는 현재 일봉 시작 시점에 확정되며 미완료 일봉은 참고 가격에만 사용. 검색 시점의 가격은 실제 체결가가 아니며 당일 시가 진입 백테스트와 다를 수 있음. 최고가 매도 가정 결과는 실제 승률이 아님. 자동 주문 없음. 별도 매도·손절 신호 미검증. 기존 보유 종목은 직접 제외.</small><p><button id="go">전체 코인 검색 시작</button></p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section><h2>모의매매 계좌</h2><small>가상 원금 100만원, 1회 투자금 변경 가능. 현재가 조회 후 전량 가상 체결(편도 0.04%). 직접 매수/매도, 7일 경과 표시만 제공. 실제 주문 없음. 기록은 이 브라우저에만 저장, 기기·브라우저 변경/데이터 삭제 시 유실될 수 있음. CSV 백업 권장.</small><p>1회 투자금(원) <input id="paperAmount" type="number" min="5000" step="1000" value="100000" style="width:105px"><button id="refreshPaper">보유 가격 갱신</button><button id="paperCSV">모의매매 CSV</button></p><p id="paperSummary"></p><p id="paperWin"></p><p id="paperMsg" class="muted"></p><h3>보유 종목</h3><div class="cards" id="paperOpen"></div><details><summary>청산 기록 (최근 100건)</summary><div class="cards" id="paperClosed"></div></details></section><section id="out" hidden><h2 id="headline"></h2><p id="audit" class="muted"></p><p id="notice" class="muted"></p><a href="/csv/features">매수 후보 CSV</a><a href="/csv/coverage">수집 상태 CSV</a><a href="/csv/errors">오류 CSV</a><p><input id="query" placeholder="코인 검색" style="width:105px"><select id="sort"><option value="market">종목명 순</option><option value="volume">거래량 증가율 순</option><option value="depth">고점 대비 하락폭 순</option><option value="gap">당일 시가 대비 변동률 순</option></select></p><p id="count"></p><div class="cards" id="cards"></div><button id="more">더 보기</button></section><script>
const $=id=>document.getElementById(id);let result=null,loading=false,limit=30;
function num(x){return typeof x==='number'?x.toLocaleString('ko-KR',{maximumSignificantDigits:8}):'—'}
function pct(x){return typeof x==='number'?(x>0?'+':'')+x.toFixed(2)+'%':'—'}
function node(tag,txt,cls){let e=document.createElement(tag);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
function line(card,k,v,cls){let r=node('div',null,'line');r.append(node('span',k,'muted'),node('strong',v,cls));card.append(r)}
function render(){if(!result)return;let q=$('query').value.trim().toUpperCase(),sort=$('sort').value;let a=result.rank.filter(x=>x.market.includes(q));let key={volume:'volume_increase_pct',depth:'peak_to_last_pct',gap:'change_from_open_pct'}[sort];if(key)a.sort((x,y)=>sort==='depth'?(x[key]??Infinity)-(y[key]??Infinity):(y[key]??-Infinity)-(x[key]??-Infinity));else a.sort((x,y)=>x.market.localeCompare(y.market));$('count').textContent=a.length+'종목 · '+Math.min(limit,a.length)+'개 표시';let cards=a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · 조건 충족','green'));line(c,'검색 시 참고가',num(x.reference_price)+'원');line(c,'당일 시가',num(x.day_open)+'원');line(c,'시가 대비',pct(x.change_from_open_pct),x.change_from_open_pct>=0?'green':'red');line(c,'20일 실제 하락일',x.down_days+'/20일');line(c,'20일 종가 변화',pct(x.trend_net_pct),'red');line(c,'고점 대비 전일 종가',pct(x.peak_to_last_pct),'red');line(c,'거래량 배수',num(x.vol_ratio_d3)+' → '+num(x.vol_ratio_d1)+'배','green');line(c,'거래량 배수 증가율',pct(x.volume_increase_pct));line(c,'참고가 기준 본전',num(x.breakeven_price)+'원');line(c,'신호 확정(KST)',x.signal_time);line(c,'가격 조회(KST)',x.observed_at);line(c,'7일째 일봉 날짜',x.planned_last_day);let b=node('button',paperStats().open.some(t=>t.market===x.market)?'모의 보유 중':'모의매수');b.disabled=paperBusy||!storageOK||paperStats().open.some(t=>t.market===x.market);b.onclick=()=>paperBuy(x);c.append(b);return c});$('cards').replaceChildren(...cards);$('more').hidden=a.length<=limit}
function show(){let x=result.summary[0];$('headline').textContent='매수 조건 충족 '+x.matches+'종목';$('audit').textContent='신호 확정 '+x.signal_time+' KST · 검색 완료 '+x.finished_at+' KST · 분석 '+x.valid+'/'+x.universe+' · 이력 부족 '+x.history_missing+' · 현재가 없음 '+x.no_quote;$('notice').textContent='수수료 편도 0.04% 적용. 본전가는 참고가 기준 이론값(호가단위·슬리피지 미반영). 직접 매매 전 현재가와 보유 여부 확인.';limit=30;render()}
$('query').oninput=$('sort').onchange=()=>{limit=30;render()};$('more').onclick=()=>{limit+=30;render()};
$('go').onclick=async()=>{try{$('go').disabled=true;let r=await fetch('/live10/start',{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 연구 실행 중');result=null;$('out').hidden=true;await poll()}catch(e){$('phase').textContent=e.message;$('go').disabled=false}};
async function poll(){try{let s=await(await fetch('/status',{cache:'no-store'})).json();$('go').disabled=s.status==='running';if(s.mode!=='live10'){$('out').hidden=true;result=null;$('phase').textContent=s.status==='running'?'다른 연구 실행 중':'대기';return}$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');if(s.status==='running'){result=null;$('out').hidden=true}if(s.status==='done'&&!result&&!loading){loading=true;try{let r=await fetch('/results',{cache:'no-store'});if(!r.ok)throw Error(r.status);result=await r.json();$('out').hidden=false;show()}finally{loading=false}}}catch(e){$('phase').textContent='연결 확인: '+e.message}}

const FEE=.0004,STORE='reverse-swing-paper-v1',INITIAL=1000000;
let account={trades:[]},storageOK=true,paperBusy=false,quotes={};
try{let raw=localStorage.getItem(STORE);if(raw){let a=JSON.parse(raw);if(!Array.isArray(a.trades))throw Error('invalid data');account=a}}catch(e){storageOK=false}
function savePaper(next){localStorage.setItem(STORE,JSON.stringify(next));account=next}
function paperStats(){let closed=account.trades.filter(x=>x.closedAt),open=account.trades.filter(x=>!x.closedAt),real=closed.reduce((s,x)=>s+x.pnl,0),spent=open.reduce((s,x)=>s+x.amount,0);return {closed,open,real,cash:INITIAL+real-spent,wins:closed.filter(x=>x.pnl>0).length,losses:closed.filter(x=>x.pnl<0).length}}
function paperMsg(s){$('paperMsg').textContent=s}
function paperTime(t){return new Date(t).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})}
async function quote(markets){let all={};for(let i=0;i<markets.length;i+=40){let r=await fetch('/paper/quotes?markets='+encodeURIComponent(markets.slice(i,i+40).join(',')),{cache:'no-store'});if(!r.ok)throw Error('현재가 조회 실패');let d=await r.json();Object.assign(all,d.quotes)}return all}
async function paperBuy(x){
 if(paperBusy||!storageOK)return;
 let a=Number($('paperAmount').value),s=paperStats();
 if(!Number.isFinite(a)||a<5000||a>s.cash){paperMsg('가상 잔액 내에서 5,000원 이상 입력해 주세요.');return}
 if(s.open.some(t=>t.market===x.market)){paperMsg('이미 모의 보유 중인 종목입니다.');return}
 if(s.closed.some(t=>t.market===x.market&&t.signalTime===x.signal_time)){paperMsg('같은 일봉 신호에서 이미 매매한 종목입니다.');return}
 paperBusy=true;renderPaper();paperMsg('새로운 현재가 조회 중...');
 try{let r=await fetch('/paper/entry?market='+encodeURIComponent(x.market)+'&signal='+encodeURIComponent(x.signal_time),{cache:'no-store'});if(!r.ok)throw Error('신호가 만료되었거나 가격 조회 실패. 다시 검색해 주세요.');let q=await r.json();let p=q.price;if(!Number.isFinite(p)||p<=0)throw Error('유효한 현재가 없음');
 let t={id:Date.now()+'-'+x.market,market:x.market,signalTime:x.signal_time,boughtAt:q.observed_ms,amount:a,buy:p,qty:a/(1+FEE)/p,deadline:q.observed_ms+7*86400000,signalDeadline:q.signal_deadline_ms};savePaper({trades:[...account.trades,t]});quotes[x.market]=q;paperMsg(x.market+' 모의매수 완료 (시장 참고가 가정).');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function paperSell(t){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('매도 참고가 조회 중...');
 try{let q=(await quote([t.market]))[t.market];if(!q)throw Error('가격 확인 불가');let proceeds=t.qty*q.price*(1-FEE),pnl=proceeds-t.amount;let closed={...t,closedAt:q.observed_ms,sell:q.price,proceeds,pnl,netPct:100*pnl/t.amount};savePaper({trades:account.trades.map(x=>x.id===t.id?closed:x)});quotes[t.market]=q;paperMsg(t.market+' 모의매도 완료. 순손익 '+num(pnl)+'원');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function refreshPaper(){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('보유 종목 가격 조회 중...');
 try{let m=paperStats().open.map(x=>x.market);if(m.length)quotes=await quote(m);paperMsg('가격 갱신 완료. 가격 확인 불가 종목은 — 표시.')}catch(e){quotes={};paperMsg(e.message)}finally{paperBusy=false;renderPaper()}
}
function renderPaper(){
 let s=paperStats();$('paperSummary').textContent='가상 원금 1,000,000원 · 잔액 '+num(s.cash)+'원 · 실현손익 '+num(s.real)+'원';$('paperWin').textContent='보유 '+s.open.length+' · 청산 '+s.closed.length+' · 수익 '+s.wins+' · 손실 '+s.losses+' · 승률 '+(s.closed.length?(100*s.wins/s.closed.length).toFixed(2)+'%':'—');
 $('refreshPaper').disabled=paperBusy||!storageOK;
 let cards=s.open.map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 모의 보유'));line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매수가',num(t.buy)+'원');line(c,'투자금(수수료 포함)',num(t.amount)+'원');let q=quotes[t.market];let val=q?t.qty*q.price*(1-FEE)-t.amount:null;line(c,'조회 가격',q?num(q.price)+'원':'—');line(c,'가격 조회',q?paperTime(q.observed_ms):'—');line(c,'평가 순손익',val===null?'—':num(val)+'원 / '+pct(100*val/t.amount),val===null?'muted':val>=0?'green':'red');line(c,'7일 보유 만료',paperTime(t.deadline),Date.now()>=t.deadline?'red':'muted');if(Date.now()>=t.deadline)c.append(node('p','7일 경과: 직접 모의매도 필요','red'));let b=node('button','모의매도');b.disabled=paperBusy||!storageOK;b.onclick=()=>paperSell(t);c.append(b);return c});
 $('paperOpen').replaceChildren(...cards);if(!cards.length)$('paperOpen').textContent='보유 종목 없음';
 let hist=s.closed.slice().reverse().slice(0,100).map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 청산 완료'));line(c,'매수 / 매도',num(t.buy)+' / '+num(t.sell));line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매도 시각',paperTime(t.closedAt));line(c,'순손익',num(t.pnl)+'원 / '+pct(t.netPct),t.pnl>=0?'green':'red');return c});$('paperClosed').replaceChildren(...hist);if(!hist.length)$('paperClosed').textContent='청산 기록 없음';if(!storageOK)paperMsg('브라우저 저장소 사용 불가 또는 기록 손상. 모의매매를 중단합니다.');
}
$('refreshPaper').onclick=refreshPaper;
$('paperCSV').onclick=()=>{let keys=['market','signalTime','boughtAt','amount','buy','qty','deadline','closedAt','sell','proceeds','pnl','netPct'];let esc=v=>'"'+String(v??'').replaceAll('"','""')+'"';let rows=[keys.join(','),...account.trades.map(x=>keys.map(k=>esc(x[k])).join(','))];let url=URL.createObjectURL(new Blob(['\ufeff'+rows.join('\r\n')],{type:'text/csv;charset=utf-8'}));let a=document.createElement('a');a.href=url;a.download='REVERSE_SWING_V1_1_PAPER_'+new Date().toISOString().slice(0,10)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
renderPaper();

poll();setInterval(poll,2000);
</script></html>"""


# V1.2: strengthened daily setup, confirmed hourly SMA5 crossing.
HOUR=3600
def setup12(rows,t):
    v=trend6(rows,t,20)
    if not v["valid"]:return None
    a=volume8(rows,t-2*DAY);b=volume8(rows,t)
    if a is None or b is None:return None
    if not(v["downtrend"] and v["peak_to_last_close_pct"]<=-30 and b>=2 and b>=2*a and b>a):return None
    return dict(down_days=v["down_days"],peak_to_last_pct=v["peak_to_last_close_pct"],vol_ratio_d3=a,vol_ratio_d1=b)
def cross12(by,t):
    # t is opening of the next candle, never use its close.
    seq=[by.get(t-i*HOUR) for i in range(6,0,-1)]
    if not all(seq) or any(r[4]<=0 for r in seq):return None
    z=[r[4] for r in seq];ma0=mean(z[:5]);ma1=mean(z[1:])
    return dict(cross=z[-2]<=ma0 and z[-1]>ma1,ma5=ma1,close=z[-1],signal_time=iso(t))
def spike121(opening,high):
    return opening>0 and high>=opening*1.30-1e-12*opening
def daily_spike121(rows,cut):
    r=next((r for r in rows if r[0]==cut),None)
    if not r or min(r[1:5])<=0:return None
    return dict(day_open=r[1],day_high=r[2],reference_price=r[4],day_high_pct=pct(r[2],r[1]),change_from_open_pct=pct(r[4],r[1]),breakeven_price=r[4]*(1+FEE81)/(1-FEE81),spike=spike121(r[1],max(r[2],r[4])))
LIVE14_PRESETS={"A":(30,15),"B":(35,14),"C":(40,15),"D":(35,13)}
def preset14_ok(s,preset):
    if not s:return False
    depth,days=LIVE14_PRESETS[preset]
    return s["peak_to_last_pct"]<=-depth and s["down_days"]>=days
def paper142_allowed(match,requested):
    status=match.get("status")
    if match.get("preset"):
        return status in ("BUY","WATCH") and requested==status
    return status in ("BUY","MATCH")
def spike141(rows,cut,price=None,lookback=20):
    # Inspect the 20 completed daily changes plus today's observed high.
    # Prior close is included as another base to catch opening gaps.
    by={r[0]:r for r in rows}
    best=None;hits=0
    for t in range(cut-lookback*DAY,cut+DAY,DAY):
        prev=by.get(t-DAY);r=by.get(t)
        if not prev or not r or not all(math.isfinite(v) and v>0 for v in (prev[4],r[1],r[2],r[4])):return None
        high=max(r[2],r[4],price if t==cut and price is not None else 0)
        base=min(r[1],prev[4])
        value=pct(high,base)
        hit=spike121(base,high)
        hits+=int(hit)
        if best is None or value>best["lookback_max_spike_pct"]:
            best=dict(lookback_max_spike_pct=value,lookback_max_spike_date=iso(t)[:10],lookback_spike_basis="day_open" if r[1]<=prev[4] else "previous_close")
    return dict(best,lookback_spike_days=hits,lookback_spike_excluded=hits>0,spike_lookback_days=lookback)
def run12(preset=None):
    begin=time.monotonic()
    try:
        end=int(now().timestamp());universe=sorted({x["market"] for x in api("market/all") if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        probe=candles("KRW-BTC",1440,end,30,latest=True)
        if not universe or len(probe)<2:raise RuntimeError("일봉 데이터 부족")
        anchor=probe[-1][0]%DAY;cut=day_start(end,anchor)
        if probe[-1][0]!=cut:raise RuntimeError("BTC 일봉 지연")
        patch(phase="일봉 강화 조건 검색",done=0,total=len(universe),universe=len(universe))
        out=[];coverage=[];success=0;valid=0
        for m,rows in parallel(universe,lambda m:candles(m,1440,int(now().timestamp()),30,latest=True)):
            if not rows or any(r[0]%DAY!=anchor for r in rows):
                coverage.append(dict(market=m,status="fetch_error"));continue
            success+=1
            x,why=candidate10(rows,cut,int(now().timestamp()))
            if why!="history":valid+=1
            s=setup12(rows,cut)
            if s and preset and not preset14_ok(s,preset):
                coverage.append(dict(market=m,status="preset_fail",down_days=s["down_days"],peak_to_last_pct=s["peak_to_last_pct"]));continue
            if x and s:
                ds=daily_spike121(rows,cut)
                audit=spike141(rows,cut) if preset else {}
                if ds is None:coverage.append(dict(market=m,status="no_quote"))
                elif audit is None:coverage.append(dict(market=m,status="spike_history_missing",day_high_pct=ds["day_high_pct"]))
                elif ds["spike"] or audit.get("lookback_spike_excluded"):coverage.append(dict(market=m,status="spike_excluded",day_high_pct=ds["day_high_pct"],**audit))
                else:
                    out.append(dict(market=m,**dict(x,**ds,**audit)))
                    coverage.append(dict(market=m,status="daily_match",day_high_pct=ds["day_high_pct"],**audit))
            else:coverage.append(dict(market=m,status=why if not x else "strength_fail"))
        if not valid:raise RuntimeError("유효 일봉 없음")
        # One fixed hour boundary for the entire second phase.
        hcut=(int(now().timestamp())//HOUR)*HOUR
        patch(phase="1시간봉 반등 확인",done=0,total=len(out),market_success=success)
        def hourfetch(x):
            return candles(x,60,int(now().timestamp()),12,latest=True),candles(x,1440,int(now().timestamp()),22 if preset else 2,latest=True)
        bymarket={x["market"]:x for x in out}
        covmap={x["market"]:x for x in coverage};kept=[]
        for m,pack in parallel(list(bymarket),hourfetch):
            x=bymarket[m];hr,dr=pack if pack else ([],[])
            ds=daily_spike121(dr,cut)
            if ds is None:
                covmap[m].update(status="no_quote");continue
            audit=spike141(dr,cut) if preset else {}
            if audit is None:
                covmap[m].update(status="spike_history_missing");continue
            if ds["spike"] or audit.get("lookback_spike_excluded"):
                covmap[m].update(status="spike_excluded",day_high_pct=ds["day_high_pct"],**audit);continue
            h=cross12({r[0]:r for r in hr},hcut)
            x.update(ds,**audit,observed_at=iso(int(now().timestamp())),status="BUY" if h and h["cross"] else "WATCH",hour_signal_time=iso(hcut),hour_ma5=h["ma5"] if h else None,hour_close=h["close"] if h else None,hour_valid=h is not None)
            covmap[m].update(status=x["status"],day_high_pct=ds["day_high_pct"],**audit);kept.append(x)
        out=kept
        nowts=int(now().timestamp())
        if day_start(nowts,anchor)!=cut or nowts//HOUR*HOUR!=hcut:raise RuntimeError("일봉/시간봉 경계 변경: 다시 검색")
        out.sort(key=lambda x:(x["status"]!="BUY",-x["vol_ratio_d1"],x["market"]))
        summary=[dict(signal_day=iso(cut)[:10],signal_time=iso(cut),finished_at=iso(nowts),universe=len(universe),market_success=success,valid=valid,matches=len(out),spike_excluded=sum(x["status"]=="spike_excluded" for x in coverage),buys=sum(x["status"]=="BUY" for x in out),watches=sum(x["status"]=="WATCH" for x in out),hour_missing=sum(not x["hour_valid"] for x in out),history_missing=sum(x["status"]=="history" for x in coverage),no_quote=sum(x["status"]=="no_quote" for x in coverage),fee_oneway_pct=.04)]
        if preset:
            depth,days=LIVE14_PRESETS[preset]
            summary[0].update(spike_lookback_days=20,spike_history_missing=sum(x["status"]=="spike_history_missing" for x in coverage))
            summary[0]["past_spike_excluded"]=sum(x["status"]=="spike_excluded" and x.get("lookback_max_spike_date","")<iso(cut)[:10] for x in coverage)
            with state_lock:scan_id=state["scan_id"]
            for row in out+coverage+summary:
                row.update(preset=preset,min_drawdown_pct=depth,min_down_days=days,scan_id=scan_id)
        patch(rank=out,features=out,summary=summary,coverage=coverage,done=len(bymarket),status="done",phase="완료",elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:record_error(str(e));patch(status="error",phase="실패")
def start12(mode):
    with state_lock:
        if state["status"]=="running":return False
        state.clear();state.update(mode=mode,status="running",phase="준비",done=0,total=0,rank=[],features=[],summary=[],errors=[],coverage=[])
    threading.Thread(target=run12 if mode=="live12" else back12,daemon=True).start()
    return True
def start14(preset):
    if preset not in LIVE14_PRESETS:return False
    with state_lock:
        if state["status"]=="running":return False
        state.clear();state.update(mode="live14",preset=preset,scan_id=str(time.time_ns()),status="running",phase="준비",done=0,total=0,rank=[],features=[],summary=[],errors=[],coverage=[])
    threading.Thread(target=run12,args=(preset,),daemon=True).start()
    return True
def result12(seq):
    if not seq or any(min(r[1:5])<=0 for r in seq):return None
    p=seq[0][1]
    def net(q):return 100*(q/p*(1-FEE81)/(1+FEE81)-1)
    peak=max(r[2] for r in seq)
    return dict(buy_price=p,peak_net_pct=net(peak),close_net_pct=net(seq[-1][4]))
def summarize12(rows):
    a=len(rows)
    def avg(k):return mean([r[k] for r in rows]) if a else None
    return dict(count=a,profit_opportunities=sum(r["peak_net_pct"]>0 for r in rows),opportunity_pct=100*sum(r["peak_net_pct"]>0 for r in rows)/a if a else None,mean_peak_pct=avg("peak_net_pct"),median_peak_pct=statistics.median(r["peak_net_pct"] for r in rows) if a else None,close_wins=sum(r["close_net_pct"]>0 for r in rows),close_win_pct=100*sum(r["close_net_pct"]>0 for r in rows)/a if a else None,mean_close_pct=avg("close_net_pct"))
def back12():
    begin=time.monotonic()
    try:
        end=int(now().timestamp());universe=sorted({x["market"] for x in api("market/all") if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        probe=candles("KRW-BTC",1440,end,30,latest=True)
        if len(probe)<2:raise RuntimeError("일봉 데이터 부족")
        anchor=probe[-1][0]%DAY;cut=day_start(end,anchor)
        if anchor%HOUR:raise RuntimeError("일봉 시간 경계 불일치")
        first=day_start(int(datetime(2026,10,28,12,tzinfo=KST).timestamp()),anchor)
        starts=[first+i*DAY for i in range(28) if first+i*DAY+8*DAY<=cut]
        if not starts:raise RuntimeError("완료 검증 기간 없음")
        patch(phase="일봉 조건 재검증",done=0,total=len(universe),universe=len(universe))
        candidates={};success=0
        for m,rows in parallel(universe,lambda m:candles(m,1440,end,200,latest=True)):
            if not rows or any(r[0]%DAY!=anchor for r in rows):continue
            success+=1;by={r[0]:r for r in rows}
            for t in starts:
                s=setup12(rows,t)
                if not s:continue
                seq=[by.get(t+i*DAY) for i in range(7)]
                base=result12(seq) if all(seq) else None
                if base:candidates.setdefault(m,[]).append((t,s,base))
        patch(phase="후보만 1시간봉 과거 수집",done=0,total=len(candidates),market_success=success)
        details=[];coverage=[]
        # fetch enough history ending at fixed historical end, not latest now.
        histend=starts[-1]+8*DAY
        pages=math.ceil((histend-(starts[0]-6*HOUR))/(200*HOUR))+1
        for m,rows in parallel(list(candidates),lambda m:candles(m,60,histend,200,pages=pages,latest=False)):
            by={r[0]:r for r in (rows or [])};valid=0;hits=0;excluded=0
            for t,s,base in candidates[m]:
                checks=[cross12(by,t+i*HOUR) for i in range(1,24)]
                # Require complete hourly data for both arms even when no crossing.
                full=all(t+i*HOUR in by for i in range(-5,24+168))
                if not full or any(h is None for h in checks):continue
                valid+=1;row=dict(market=m,day=iso(t)[:10],arm="DAILY",entry_time=iso(t),**s,**base);details.append(row)
                hit=next((i+1 for i,h in enumerate(checks) if h["cross"]),None)
                if hit is None:continue
                entry=t+hit*HOUR
                # Only prices known before the entry, plus its opening price.
                seen=max([by[t+i*HOUR][2] for i in range(hit)]+[by[entry][1]])
                if spike121(by[t][1],seen):
                    excluded+=1;continue
                seq=[by[entry+i*HOUR] for i in range(168)]
                r=result12(seq)
                if r:details.append(dict(market=m,day=iso(t)[:10],arm="HOUR",entry_time=iso(entry),**s,**r));hits+=1
            coverage.append(dict(market=m,daily_signals=len(candidates[m]),hour_valid=valid,hour_hits=hits,spike_excluded=excluded))
        a=[x for x in details if x["arm"]=="DAILY"];b=[x for x in details if x["arm"]=="HOUR"]
        paired={(x["market"],x["day"]) for x in b}
        p=[x for x in a if (x["market"],x["day"]) in paired]
        summary=[dict(arm=k,**summarize12(v)) for k,v in (("DAILY",a),("DAILY_PAIRED",p),("HOUR",b))]
        patch(features=details,summary=summary,coverage=coverage,done=len(candidates),daily_signals=sum(len(x) for x in candidates.values()),spike_excluded=sum(x["spike_excluded"] for x in coverage),mature_days=len(starts),status="done",phase="완료",elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:record_error(str(e));patch(status="error",phase="실패")
HTMLB12=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE SWING V1.2.1 BACKTEST</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:900px;margin:auto;padding:14px}button,a{display:inline-block;background:#225b51;color:white;padding:10px;margin:5px;border-radius:7px}table{border-collapse:collapse;white-space:nowrap}td,th{padding:8px;border-bottom:1px solid #324355;text-align:right}progress{width:100%}div{overflow:auto}</style><h1>REVERSE SWING V1.2.1 BACKTEST</h1><a href="/live12">실전·모의매매</a><p>20일 하락 + 고점 대비 30% 하락 + 전일 거래량 2배 + D-3 대비 2배. 일봉 시가 vs 당일 첫 마감 1시간봉 SMA5 상향 돌파 다음 시가. 1시간봉 진입 직전까지 당일 시가 대비 30% 이상 상승한 종목 제외(진입 후 가격은 필터에 사용하지 않음). 각 진입 후 7일. 편도 0.04%. 동일 코인 날짜별 독립 신호(중복 포함). 시간봉 완전 수집 사례만 비교. 최고가는 수익 기회, 7일 종료가는 실제 청산 가정. 이미 연구한 기간이며 독립 검증 아님.</p><button id="go">비교 검증 시작</button><p id="phase"></p><progress id="p"></progress><pre id="errors" style="white-space:pre-wrap"></pre><section id="out" hidden><a href="/csv/summary">요약 CSV</a><a href="/csv/features">매매 CSV</a><a href="/csv/coverage">수집 CSV</a><a href="/csv/errors">오류 CSV</a><p id="audit"></p><div id="tab"></div></section><script>
const $=x=>document.getElementById(x);let loaded=false,busy=false;
$('go').onclick=async()=>{try{let r=await fetch('/back12/start',{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 검색 실행 중');loaded=false;$('out').hidden=true;poll()}catch(e){$('phase').textContent=e.message}};
function render(a){let t=document.createElement('table');let tr=t.insertRow();for(let s of ['구분','매수','수익기회','기회율%','최고가 평균%','최고가 중앙%','7일청산 수익','7일청산 승률%','7일청산 평균%'])tr.insertCell().textContent=s;for(let x of a){let r=t.insertRow();for(let v of [{DAILY:'일봉 전체',DAILY_PAIRED:'동일 종목 일봉',HOUR:'1시간 반등'}[x.arm],x.count,x.profit_opportunities,x.opportunity_pct,x.mean_peak_pct,x.median_peak_pct,x.close_wins,x.close_win_pct,x.mean_close_pct])r.insertCell().textContent=typeof v==='number'?Number(v.toFixed(2)):v??'—'}$('tab').replaceChildren(t)}
async function poll(){try{let s=await(await fetch('/status',{cache:'no-store'})).json();$('go').disabled=s.status==='running';if(s.mode!=='back12'){$('out').hidden=true;loaded=false;$('phase').textContent=s.status==='running'?'다른 검색 실행 중':'대기';return}$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('errors').textContent=s.errors.join('\n');if(s.status==='running'){loaded=false;$('out').hidden=true}if(s.status==='done'&&!loaded&&!busy){busy=true;try{let d=await(await fetch('/results')).json();render(d.summary);$('audit').textContent='일봉 조건 신호 '+s.daily_signals+'건 · 급등 제외 '+s.spike_excluded+'건 · 진입일 '+s.mature_days+'일 · 시간봉 유효 '+d.summary[0].count+'건 · 오류 '+s.error_count+'건';$('out').hidden=false;loaded=true}finally{busy=false}}}catch(e){$('phase').textContent=e.message}}
poll();setInterval(poll,2000);
</script></html>"""

HTML12=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE SWING V1.2.1 LIVE + PAPER</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1000px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:10px;margin:4px;border-radius:7px;text-decoration:none}input{background:#10151d}button:disabled{opacity:.5}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:10px}.coin{background:#111923;border:1px solid #3b4d60;border-radius:12px;padding:12px}.coin h3{margin:0 0 8px}.line{display:flex;justify-content:space-between;gap:10px;margin:6px 0}.green{color:#72ddb0}.red{color:#ff9b9b}.muted{color:#a9bac9}h1{font-size:23px;margin-bottom:4px}</style><h1>REVERSE SWING V1.2.1 LIVE + PAPER</h1><a href="/reverse81">V0.8.1 백테스트</a><a href="/back12">V1.2.1 비교 검증</a><section><h2>실전 매수 후보 검색</h2><p>20일 하락 + 고점 대비 30% 이상 하락 + 전일 거래량 2배 이상 + D-3 대비 2배 이상 · RSI 제외</p><small>당일 시가 대비 고가 30% 이상 급등 시 제외. 강화 일봉 조건 충족 WATCH. 가장 최근 완료 1시간봉의 SMA5 상향 돌파만 BUY 후보. 미완료 봉 제외. BUY 신호는 해당 1시간 동안 유효. 검증 전 실전 매수 권장 아님. 검색 시점의 가격은 실제 체결가가 아니며 당일 시가 진입 백테스트와 다를 수 있음. 최고가 매도 가정 결과는 실제 승률이 아님. 자동 주문 없음. 별도 매도·손절 신호 미검증. 기존 보유 종목은 직접 제외.</small><p><button id="go">전체 코인 검색 시작</button></p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section><h2>모의매매 계좌</h2><small>가상 원금 100만원, 1회 투자금 변경 가능. 현재가 조회 후 전량 가상 체결(편도 0.04%). 직접 매수/매도, 7일 경과 표시만 제공. 실제 주문 없음. 기록은 이 브라우저에만 저장, 기기·브라우저 변경/데이터 삭제 시 유실될 수 있음. CSV 백업 권장.</small><p>1회 투자금(원) <input id="paperAmount" type="number" min="5000" step="1000" value="100000" style="width:105px"><button id="refreshPaper">보유 가격 갱신</button><button id="paperCSV">모의매매 CSV</button></p><p id="paperSummary"></p><p id="paperWin"></p><p id="paperMsg" class="muted"></p><h3>보유 종목</h3><div class="cards" id="paperOpen"></div><details><summary>청산 기록 (최근 100건)</summary><div class="cards" id="paperClosed"></div></details></section><section id="out" hidden><h2 id="headline"></h2><p id="audit" class="muted"></p><p id="notice" class="muted"></p><a href="/csv/features">매수 후보 CSV</a><a href="/csv/coverage">수집 상태 CSV</a><a href="/csv/errors">오류 CSV</a><p><input id="query" placeholder="코인 검색" style="width:105px"><select id="sort"><option value="market">종목명 순</option><option value="volume">거래량 증가율 순</option><option value="depth">고점 대비 하락폭 순</option><option value="gap">당일 시가 대비 변동률 순</option></select></p><p id="count"></p><div class="cards" id="cards"></div><button id="more">더 보기</button></section><script>
const $=id=>document.getElementById(id);let result=null,loading=false,limit=5;
function num(x){return typeof x==='number'?x.toLocaleString('ko-KR',{maximumSignificantDigits:8}):'—'}
function pct(x){return typeof x==='number'?(x>0?'+':'')+x.toFixed(2)+'%':'—'}
function node(tag,txt,cls){let e=document.createElement(tag);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
function line(card,k,v,cls){let r=node('div',null,'line');r.append(node('span',k,'muted'),node('strong',v,cls));card.append(r)}
function render(){if(!result)return;let q=$('query').value.trim().toUpperCase(),sort=$('sort').value;let a=result.rank.filter(x=>x.market.includes(q));let key={volume:'volume_increase_pct',depth:'peak_to_last_pct',gap:'change_from_open_pct'}[sort];if(key)a.sort((x,y)=>sort==='depth'?(x[key]??Infinity)-(y[key]??Infinity):(y[key]??-Infinity)-(x[key]??-Infinity));else a.sort((x,y)=>x.market.localeCompare(y.market));$('count').textContent=a.length+'종목 · '+Math.min(limit,a.length)+'개 표시';a.sort((x,y)=>Number(y.status==="BUY")-Number(x.status==="BUY"));let cards=a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+(x.status==='BUY'?'BUY 후보':'WATCH'),x.status==='BUY'?'green':'muted'));line(c,'검색 시 참고가',num(x.reference_price)+'원');line(c,'당일 시가',num(x.day_open)+'원');line(c,'시가 대비',pct(x.change_from_open_pct),x.change_from_open_pct>=0?'green':'red');line(c,'당일 최고 상승률',pct(x.day_high_pct));line(c,'20일 실제 하락일',x.down_days+'/20일');line(c,'20일 종가 변화',pct(x.trend_net_pct),'red');line(c,'고점 대비 전일 종가',pct(x.peak_to_last_pct),'red');line(c,'거래량 배수',num(x.vol_ratio_d3)+' → '+num(x.vol_ratio_d1)+'배','green');line(c,'거래량 배수 증가율',pct(x.volume_increase_pct));line(c,'참고가 기준 본전',num(x.breakeven_price)+'원');line(c,'신호 확정(KST)',x.signal_time);line(c,'가격 조회(KST)',x.observed_at);line(c,'1시간봉 5이평',num(x.hour_ma5));line(c,'1시간봉 확인',x.hour_valid?x.hour_signal_time:'데이터 부족');line(c,'7일째 일봉 날짜',x.planned_last_day);let b=node('button',paperStats().open.some(t=>t.market===x.market)?'모의 보유 중':'모의매수');b.disabled=x.status!=='BUY'||paperBusy||!storageOK||paperStats().open.some(t=>t.market===x.market);b.onclick=()=>paperBuy(x);c.append(b);return c});$('cards').replaceChildren(...cards);$('more').hidden=a.length<=limit}
function show(){let x=result.summary[0];$('headline').textContent='BUY '+x.buys+'개 · WATCH '+x.watches+'개 · 급등 제외 '+x.spike_excluded+'개';$('audit').textContent='신호 확정 '+x.signal_time+' KST · 검색 완료 '+x.finished_at+' KST · 분석 '+x.valid+'/'+x.universe+' · 이력 부족 '+x.history_missing+' · 현재가 없음 '+x.no_quote;$('notice').textContent='수수료 편도 0.04% 적용. 본전가는 참고가 기준 이론값(호가단위·슬리피지 미반영). 직접 매매 전 현재가와 보유 여부 확인.';limit=5;render()}
$('query').oninput=$('sort').onchange=()=>{limit=5;render()};$('more').onclick=()=>{limit+=5;render()};
$('go').onclick=async()=>{try{$('go').disabled=true;let r=await fetch('/live12/start',{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 연구 실행 중');result=null;$('out').hidden=true;await poll()}catch(e){$('phase').textContent=e.message;$('go').disabled=false}};
async function poll(){try{let s=await(await fetch('/status',{cache:'no-store'})).json();$('go').disabled=s.status==='running';if(s.mode!=='live12'){$('out').hidden=true;result=null;$('phase').textContent=s.status==='running'?'다른 연구 실행 중':'대기';return}$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');if(s.status==='running'){result=null;$('out').hidden=true}if(s.status==='done'&&!result&&!loading){loading=true;try{let r=await fetch('/results',{cache:'no-store'});if(!r.ok)throw Error(r.status);result=await r.json();$('out').hidden=false;show()}finally{loading=false}}}catch(e){$('phase').textContent='연결 확인: '+e.message}}

const FEE=.0004,STORE='reverse-swing-paper-v1',INITIAL=1000000;
let account={trades:[]},storageOK=true,paperBusy=false,quotes={};
try{let raw=localStorage.getItem(STORE);if(raw){let a=JSON.parse(raw);if(!Array.isArray(a.trades))throw Error('invalid data');account=a}}catch(e){storageOK=false}
function savePaper(next){localStorage.setItem(STORE,JSON.stringify(next));account=next}
function paperStats(){let closed=account.trades.filter(x=>x.closedAt),open=account.trades.filter(x=>!x.closedAt),real=closed.reduce((s,x)=>s+x.pnl,0),spent=open.reduce((s,x)=>s+x.amount,0);return {closed,open,real,cash:INITIAL+real-spent,wins:closed.filter(x=>x.pnl>0).length,losses:closed.filter(x=>x.pnl<0).length}}
function paperMsg(s){$('paperMsg').textContent=s}
function paperTime(t){return new Date(t).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})}
async function quote(markets){let all={};for(let i=0;i<markets.length;i+=40){let r=await fetch('/paper/quotes?markets='+encodeURIComponent(markets.slice(i,i+40).join(',')),{cache:'no-store'});if(!r.ok)throw Error('현재가 조회 실패');let d=await r.json();Object.assign(all,d.quotes)}return all}
async function paperBuy(x){
 if(paperBusy||!storageOK||x.status!=='BUY')return;
 let a=Number($('paperAmount').value),s=paperStats();
 if(!Number.isFinite(a)||a<5000||a>s.cash){paperMsg('가상 잔액 내에서 5,000원 이상 입력해 주세요.');return}
 if(s.open.some(t=>t.market===x.market)){paperMsg('이미 모의 보유 중인 종목입니다.');return}
 if(s.closed.some(t=>t.market===x.market&&t.signalTime===x.signal_time)){paperMsg('같은 일봉 신호에서 이미 매매한 종목입니다.');return}
 paperBusy=true;renderPaper();paperMsg('새로운 현재가 조회 중...');
 try{let r=await fetch('/paper/entry?market='+encodeURIComponent(x.market)+'&signal='+encodeURIComponent(x.signal_time)+'&hour='+encodeURIComponent(x.hour_signal_time),{cache:'no-store'});if(!r.ok)throw Error('신호가 만료되었거나 가격 조회 실패. 다시 검색해 주세요.');let q=await r.json();let p=q.price;if(!Number.isFinite(p)||p<=0)throw Error('유효한 현재가 없음');
 let t={id:Date.now()+'-'+x.market,market:x.market,signalTime:x.signal_time,boughtAt:q.observed_ms,amount:a,buy:p,qty:a/(1+FEE)/p,deadline:q.observed_ms+7*86400000,signalDeadline:q.signal_deadline_ms};savePaper({trades:[...account.trades,t]});quotes[x.market]=q;paperMsg(x.market+' 모의매수 완료 (시장 참고가 가정).');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function paperSell(t){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('매도 참고가 조회 중...');
 try{let q=(await quote([t.market]))[t.market];if(!q)throw Error('가격 확인 불가');let proceeds=t.qty*q.price*(1-FEE),pnl=proceeds-t.amount;let closed={...t,closedAt:q.observed_ms,sell:q.price,proceeds,pnl,netPct:100*pnl/t.amount};savePaper({trades:account.trades.map(x=>x.id===t.id?closed:x)});quotes[t.market]=q;paperMsg(t.market+' 모의매도 완료. 순손익 '+num(pnl)+'원');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function refreshPaper(){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('보유 종목 가격 조회 중...');
 try{let m=paperStats().open.map(x=>x.market);if(m.length)quotes=await quote(m);paperMsg('가격 갱신 완료. 가격 확인 불가 종목은 — 표시.')}catch(e){quotes={};paperMsg(e.message)}finally{paperBusy=false;renderPaper()}
}
function renderPaper(){
 let s=paperStats();$('paperSummary').textContent='가상 원금 1,000,000원 · 잔액 '+num(s.cash)+'원 · 실현손익 '+num(s.real)+'원';$('paperWin').textContent='보유 '+s.open.length+' · 청산 '+s.closed.length+' · 수익 '+s.wins+' · 손실 '+s.losses+' · 승률 '+(s.closed.length?(100*s.wins/s.closed.length).toFixed(2)+'%':'—');
 $('refreshPaper').disabled=paperBusy||!storageOK;
 let cards=s.open.map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 모의 보유'));line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매수가',num(t.buy)+'원');line(c,'투자금(수수료 포함)',num(t.amount)+'원');let q=quotes[t.market];let val=q?t.qty*q.price*(1-FEE)-t.amount:null;line(c,'조회 가격',q?num(q.price)+'원':'—');line(c,'가격 조회',q?paperTime(q.observed_ms):'—');line(c,'평가 순손익',val===null?'—':num(val)+'원 / '+pct(100*val/t.amount),val===null?'muted':val>=0?'green':'red');line(c,'7일 보유 만료',paperTime(t.deadline),Date.now()>=t.deadline?'red':'muted');if(Date.now()>=t.deadline)c.append(node('p','7일 경과: 직접 모의매도 필요','red'));let b=node('button','모의매도');b.disabled=paperBusy||!storageOK;b.onclick=()=>paperSell(t);c.append(b);return c});
 $('paperOpen').replaceChildren(...cards);if(!cards.length)$('paperOpen').textContent='보유 종목 없음';
 let hist=s.closed.slice().reverse().slice(0,100).map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 청산 완료'));line(c,'매수 / 매도',num(t.buy)+' / '+num(t.sell));line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매도 시각',paperTime(t.closedAt));line(c,'순손익',num(t.pnl)+'원 / '+pct(t.netPct),t.pnl>=0?'green':'red');return c});$('paperClosed').replaceChildren(...hist);if(!hist.length)$('paperClosed').textContent='청산 기록 없음';if(!storageOK)paperMsg('브라우저 저장소 사용 불가 또는 기록 손상. 모의매매를 중단합니다.');
}
$('refreshPaper').onclick=refreshPaper;
$('paperCSV').onclick=()=>{let keys=['market','signalTime','boughtAt','amount','buy','qty','deadline','closedAt','sell','proceeds','pnl','netPct'];let esc=v=>'"'+String(v??'').replaceAll('"','""')+'"';let rows=[keys.join(','),...account.trades.map(x=>keys.map(k=>esc(x[k])).join(','))];let url=URL.createObjectURL(new Blob(['\ufeff'+rows.join('\r\n')],{type:'text/csv;charset=utf-8'}));let a=document.createElement('a');a.href=url;a.download='REVERSE_SWING_V1_2_1_PAPER_'+new Date().toISOString().slice(0,10)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
renderPaper();

poll();setInterval(poll,2000);
</script></html>"""
HTML14=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE SWING V1.4.3 LIVE + PAPER</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1000px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:10px;margin:4px;border-radius:7px;text-decoration:none}input{background:#10151d}button:disabled{opacity:.5}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:10px}.coin{background:#111923;border:1px solid #3b4d60;border-radius:12px;padding:12px}.coin h3{margin:0 0 8px}.line{display:flex;justify-content:space-between;gap:10px;margin:6px 0}.green{color:#72ddb0}.red{color:#ff9b9b}.muted{color:#a9bac9}h1{font-size:23px;margin-bottom:4px}</style><h1>REVERSE SWING V1.4.3 LIVE + PAPER</h1><a href="/live12">기존 V1.2.1</a><a href="/research131">V1.3.1 비교 연구</a><section><h2>실전 매수 후보 검색</h2><p>20일 하락 추세 + 선택한 하락폭·하락일 수 + 전일 거래량 2배 이상 + D-3 대비 2배 이상 · RSI 제외</p><small>최근 20일과 오늘의 일별 고가가 시가 또는 전일 종가 대비 한 번이라도 30% 이상 상승하면 제외. 과거 급등 이력 부족 시 제외. 강화 일봉 조건 충족 WATCH. 가장 최근 완료 1시간봉의 SMA5 상향 돌파만 BUY 후보. 미완료 봉 제외. BUY/WATCH 모의매수 모두 검색된 1시간 동안 유효. WATCH는 5이평 상향 돌파 미확인 상태의 비교 실험이며 BUY로 변경하지 않음. 검증 전 실전 매수 권장 아님. 검색 시점의 가격은 실제 체결가가 아니며 당일 시가 진입 백테스트와 다를 수 있음. 최고가 매도 가정 결과는 실제 승률이 아님. 자동 주문 없음. 별도 매도·손절 신호 미검증. 기존 보유 종목은 직접 제외.</small><p><label for="preset">검색 조건</label><select id="preset"><option value="A">A · 30% / 15일</option><option value="B">B · 35% / 14일</option><option value="C">C · 40% / 15일</option><option value="D" selected>D · 35% / 13일 (완화 테스트)</option></select><button id="go">전체 코인 검색 시작</button></p><p id="applied" class="muted">조건을 선택하고 검색해 주세요.</p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section><h2>모의매매 계좌</h2><small>가상 원금 100만원, 1회 투자금 변경 가능. 현재가 조회 후 전량 가상 체결(편도 0.04%). 직접 매수/매도, 7일 경과 표시만 제공. 실제 주문 없음. 기록은 이 브라우저에만 저장, 기기·브라우저 변경/데이터 삭제 시 유실될 수 있음. CSV 백업 권장.</small><p>1회 투자금(원) <input id="paperAmount" type="number" min="5000" step="1000" value="100000" style="width:105px"><button id="refreshPaper">보유 가격 갱신</button><button id="paperCSV">모의매매 CSV</button></p><p id="paperSummary"></p><p id="paperWin"></p><p id="paperMsg" class="muted"></p><h3>보유 종목</h3><div class="cards" id="paperOpen"></div><details><summary>청산 기록 (최근 100건)</summary><div class="cards" id="paperClosed"></div></details></section><section id="out" hidden><h2 id="headline"></h2><p id="audit" class="muted"></p><p id="notice" class="muted"></p><a href="/csv/features?mode=live14">매수 후보 CSV</a><a href="/csv/summary?mode=live14">검색 요약 CSV</a><a href="/csv/coverage?mode=live14">수집 상태 CSV</a><a href="/csv/errors?mode=live14">오류 CSV</a><p><input id="query" placeholder="코인 검색" style="width:105px"><select id="sort"><option value="market">종목명 순</option><option value="volume">거래량 증가율 순</option><option value="depth">고점 대비 하락폭 순</option><option value="gap">당일 시가 대비 변동률 순</option></select></p><p id="count"></p><div class="cards" id="cards"></div><button id="more">더 보기</button></section><script>
const $=id=>document.getElementById(id);let result=null,loading=false,limit=5,loadedScan=null;const names={A:"A · 30% / 15일",B:"B · 35% / 14일",C:"C · 40% / 15일",D:"D · 35% / 13일"};
function num(x){return typeof x==='number'?x.toLocaleString('ko-KR',{maximumSignificantDigits:8}):'—'}
function pct(x){return typeof x==='number'?(x>0?'+':'')+x.toFixed(2)+'%':'—'}
function node(tag,txt,cls){let e=document.createElement(tag);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
function line(card,k,v,cls){let r=node('div',null,'line');r.append(node('span',k,'muted'),node('strong',v,cls));card.append(r)}
function render(){if(!result)return;let q=$('query').value.trim().toUpperCase(),sort=$('sort').value;let a=result.rank.filter(x=>x.market.includes(q));let key={volume:'volume_increase_pct',depth:'peak_to_last_pct',gap:'change_from_open_pct'}[sort];if(key)a.sort((x,y)=>sort==='depth'?(x[key]??Infinity)-(y[key]??Infinity):(y[key]??-Infinity)-(x[key]??-Infinity));else a.sort((x,y)=>x.market.localeCompare(y.market));$('count').textContent=a.length+'종목 · '+Math.min(limit,a.length)+'개 표시';a.sort((x,y)=>Number(y.status==="BUY")-Number(x.status==="BUY"));let cards=a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+(x.status==='BUY'?'BUY 후보':'WATCH'),x.status==='BUY'?'green':'muted'));line(c,'적용 검색식',names[x.preset]||x.preset);line(c,'검색 시 참고가',num(x.reference_price)+'원');line(c,'당일 시가',num(x.day_open)+'원');line(c,'시가 대비',pct(x.change_from_open_pct),x.change_from_open_pct>=0?'green':'red');line(c,'당일 최고 상승률',pct(x.day_high_pct));line(c,'20일+오늘 최대 일중 급등',pct(x.lookback_max_spike_pct));line(c,'최대 급등 날짜',x.lookback_max_spike_date||'—');line(c,'20일 실제 하락일',x.down_days+'/20일');line(c,'20일 종가 변화',pct(x.trend_net_pct),'red');line(c,'고점 대비 전일 종가',pct(x.peak_to_last_pct),'red');line(c,'거래량 배수',num(x.vol_ratio_d3)+' → '+num(x.vol_ratio_d1)+'배','green');line(c,'거래량 배수 증가율',pct(x.volume_increase_pct));line(c,'참고가 기준 본전',num(x.breakeven_price)+'원');line(c,'신호 확정(KST)',x.signal_time);line(c,'가격 조회(KST)',x.observed_at);line(c,'1시간봉 5이평',num(x.hour_ma5));line(c,'1시간봉 확인',x.hour_valid?x.hour_signal_time:'데이터 부족');line(c,'7일째 일봉 날짜',x.planned_last_day);let b=node('button',paperStats().open.some(t=>t.market===x.market)?'모의 보유 중':x.status==='WATCH'?'WATCH 모의매수':'BUY 모의매수');b.disabled=!['BUY','WATCH'].includes(x.status)||paperBusy||!storageOK||paperStats().open.some(t=>t.market===x.market);b.onclick=()=>paperBuy(x);c.append(b);return c});$('cards').replaceChildren(...cards);$('more').hidden=a.length<=limit}
function show(){let x=result.summary[0];$('applied').textContent='결과 적용 조건: '+names[x.preset];$('headline').textContent='BUY '+x.buys+'개 · WATCH '+x.watches+'개 · 20일+오늘 급등 제외 '+x.spike_excluded+'개';$('audit').textContent='신호 확정 '+x.signal_time+' KST · 검색 완료 '+x.finished_at+' KST · 분석 '+x.valid+'/'+x.universe+' · 이력 부족 '+x.history_missing+' · 현재가 없음 '+x.no_quote+' · 급등 이력 미확인 '+x.spike_history_missing;$('notice').textContent='수수료 편도 0.04% 적용. 본전가는 참고가 기준 이론값(호가단위·슬리피지 미반영). 직접 매매 전 현재가와 보유 여부 확인.';limit=5;render()}
$('preset').onchange=()=>{if(result)$('applied').textContent='현재 결과: '+names[result.summary[0].preset]+' · 다음 검색: '+names[$('preset').value]};$('query').oninput=$('sort').onchange=()=>{limit=5;render()};$('more').onclick=()=>{limit+=5;render()};
$('go').onclick=async()=>{try{$('go').disabled=true;let r=await fetch('/live14/start?preset='+encodeURIComponent($('preset').value),{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 연구 실행 중');result=null;loadedScan=null;$('out').hidden=true;await poll()}catch(e){$('phase').textContent=e.message;$('go').disabled=false}};
async function poll(){try{let s=await(await fetch('/status',{cache:'no-store'})).json();$('go').disabled=s.status==='running';$('preset').disabled=s.status==='running';if(s.mode!=='live14'){$('out').hidden=true;result=null;$('phase').textContent=s.status==='running'?'다른 연구 실행 중':'대기';return}$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');if(s.status==='running'){$('applied').textContent='검색 중: '+names[s.preset];result=null;loadedScan=null;$('out').hidden=true}if(s.status==='done'&&loadedScan!==s.scan_id&&!loading){loading=true;try{let r=await fetch('/results',{cache:'no-store'});if(!r.ok)throw Error(r.status);let d=await r.json();if(!d.summary[0]||d.summary[0].preset!==s.preset||d.summary[0].scan_id!==s.scan_id)return;result=d;loadedScan=s.scan_id;$('preset').value=s.preset;$('out').hidden=false;show()}finally{loading=false}}}catch(e){$('phase').textContent='연결 확인: '+e.message}}

const FEE=.0004,STORE='reverse-swing-paper-v1',INITIAL=1000000;
let account={trades:[]},storageOK=true,paperBusy=false,quotes={};
try{let raw=localStorage.getItem(STORE);if(raw){let a=JSON.parse(raw);if(!Array.isArray(a.trades))throw Error('invalid data');account=a}}catch(e){storageOK=false}
function savePaper(next){localStorage.setItem(STORE,JSON.stringify(next));account=next}
function paperStats(){let closed=account.trades.filter(x=>x.closedAt),open=account.trades.filter(x=>!x.closedAt),real=closed.reduce((s,x)=>s+x.pnl,0),spent=open.reduce((s,x)=>s+x.amount,0);return {closed,open,real,cash:INITIAL+real-spent,wins:closed.filter(x=>x.pnl>0).length,losses:closed.filter(x=>x.pnl<0).length}}
function paperMsg(s){$('paperMsg').textContent=s}
function paperTime(t){return new Date(t).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})}
async function quote(markets){let all={};for(let i=0;i<markets.length;i+=40){let r=await fetch('/paper/quotes?markets='+encodeURIComponent(markets.slice(i,i+40).join(',')),{cache:'no-store'});if(!r.ok)throw Error('현재가 조회 실패');let d=await r.json();Object.assign(all,d.quotes)}return all}
async function paperBuy(x){
 if(paperBusy||!storageOK||!result||!['BUY','WATCH'].includes(x.status))return;
 let a=Number($('paperAmount').value),s=paperStats();
 if(!Number.isFinite(a)||a<5000||a>s.cash){paperMsg('가상 잔액 내에서 5,000원 이상 입력해 주세요.');return}
 if(s.open.some(t=>t.market===x.market)){paperMsg('이미 모의 보유 중인 종목입니다.');return}
 if(s.closed.some(t=>t.market===x.market&&t.signalTime===x.signal_time)){paperMsg('같은 일봉 신호에서 이미 매매한 종목입니다.');return}
 paperBusy=true;renderPaper();paperMsg('새로운 현재가 조회 중...');
 try{let r=await fetch('/paper/entry?market='+encodeURIComponent(x.market)+'&signal='+encodeURIComponent(x.signal_time)+'&hour='+encodeURIComponent(x.hour_signal_time)+'&preset='+encodeURIComponent(x.preset)+'&scan_id='+encodeURIComponent(result.summary[0].scan_id)+'&entry_status='+encodeURIComponent(x.status),{cache:'no-store'});if(!r.ok)throw Error('신호 만료, 급등 제외 또는 가격 조회 실패. 다시 검색해 주세요.');let q=await r.json();if(q.entry_status!==x.status)throw Error('검색 판정 변경. 다시 검색해 주세요.');let p=q.price;if(!Number.isFinite(p)||p<=0)throw Error('유효한 현재가 없음');
 let t={id:Date.now()+'-'+x.market,market:x.market,signalTime:x.signal_time,boughtAt:q.observed_ms,amount:a,buy:p,qty:a/(1+FEE)/p,deadline:q.observed_ms+7*86400000,signalDeadline:q.signal_deadline_ms,preset:q.preset,minDrawdownPct:q.min_drawdown_pct,minDownDays:q.min_down_days,entryStatus:q.entry_status};savePaper({trades:[...account.trades,t]});quotes[x.market]=q;paperMsg(x.market+' '+q.entry_status+' 모의매수 완료 (시장 참고가 가정).');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function paperSell(t){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('매도 참고가 조회 중...');
 try{let q=(await quote([t.market]))[t.market];if(!q)throw Error('가격 확인 불가');let proceeds=t.qty*q.price*(1-FEE),pnl=proceeds-t.amount;let closed={...t,closedAt:q.observed_ms,sell:q.price,proceeds,pnl,netPct:100*pnl/t.amount};savePaper({trades:account.trades.map(x=>x.id===t.id?closed:x)});quotes[t.market]=q;paperMsg(t.market+' 모의매도 완료. 순손익 '+num(pnl)+'원');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function refreshPaper(){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('보유 종목 가격 조회 중...');
 try{let m=paperStats().open.map(x=>x.market);if(m.length)quotes=await quote(m);paperMsg('가격 갱신 완료. 가격 확인 불가 종목은 — 표시.')}catch(e){quotes={};paperMsg(e.message)}finally{paperBusy=false;renderPaper()}
}
function renderPaper(){
 let s=paperStats();$('paperSummary').textContent='가상 원금 1,000,000원 · 잔액 '+num(s.cash)+'원 · 실현손익 '+num(s.real)+'원';$('paperWin').textContent='보유 '+s.open.length+' · 청산 '+s.closed.length+' · 수익 '+s.wins+' · 손실 '+s.losses+' · 승률 '+(s.closed.length?(100*s.wins/s.closed.length).toFixed(2)+'%':'—');
 $('refreshPaper').disabled=paperBusy||!storageOK;
 let cards=s.open.map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 모의 보유'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매수가',num(t.buy)+'원');line(c,'투자금(수수료 포함)',num(t.amount)+'원');let q=quotes[t.market];let val=q?t.qty*q.price*(1-FEE)-t.amount:null;line(c,'조회 가격',q?num(q.price)+'원':'—');line(c,'가격 조회',q?paperTime(q.observed_ms):'—');line(c,'평가 순손익',val===null?'—':num(val)+'원 / '+pct(100*val/t.amount),val===null?'muted':val>=0?'green':'red');line(c,'7일 보유 만료',paperTime(t.deadline),Date.now()>=t.deadline?'red':'muted');if(Date.now()>=t.deadline)c.append(node('p','7일 경과: 직접 모의매도 필요','red'));let b=node('button','모의매도');b.disabled=paperBusy||!storageOK;b.onclick=()=>paperSell(t);c.append(b);return c});
 $('paperOpen').replaceChildren(...cards);if(!cards.length)$('paperOpen').textContent='보유 종목 없음';
 let hist=s.closed.slice().reverse().slice(0,100).map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 청산 완료'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 / 매도',num(t.buy)+' / '+num(t.sell));line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매도 시각',paperTime(t.closedAt));line(c,'순손익',num(t.pnl)+'원 / '+pct(t.netPct),t.pnl>=0?'green':'red');return c});$('paperClosed').replaceChildren(...hist);if(!hist.length)$('paperClosed').textContent='청산 기록 없음';if(!storageOK)paperMsg('브라우저 저장소 사용 불가 또는 기록 손상. 모의매매를 중단합니다.');
}
$('refreshPaper').onclick=refreshPaper;
$('paperCSV').onclick=()=>{let keys=['market','preset','entryStatus','minDrawdownPct','minDownDays','signalTime','boughtAt','amount','buy','qty','deadline','closedAt','sell','proceeds','pnl','netPct'];let esc=v=>'"'+String(v??'').replaceAll('"','""')+'"';let rows=[keys.join(','),...account.trades.map(x=>keys.map(k=>esc(x[k])).join(','))];let url=URL.createObjectURL(new Blob(['\ufeff'+rows.join('\r\n')],{type:'text/csv;charset=utf-8'}));let a=document.createElement('a');a.href=url;a.download='REVERSE_SWING_V1_4_3_PAPER_'+new Date().toISOString().slice(0,10)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
renderPaper();

poll();setInterval(poll,2000);
</script></html>"""
HTML20=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BITHUMB SWING V2.0 TEST</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1000px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:10px;margin:4px;border-radius:7px;text-decoration:none}input{background:#10151d}button:disabled{opacity:.5}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:10px}.coin{background:#111923;border:1px solid #3b4d60;border-radius:12px;padding:12px}.coin h3{margin:0 0 8px}.line{display:flex;justify-content:space-between;gap:10px;margin:6px 0}.green{color:#72ddb0}.red{color:#ff9b9b}.muted{color:#a9bac9}h1{font-size:23px;margin-bottom:4px}</style><h1>BITHUMB SWING V2.0 TEST</h1><a href="/live143">기존 V1.4.3 보존판</a><section><h2>매수 후보 검색</h2><p><label>전략 <select id="strategy"><option value="reverse">저점 반등형 (기존)</option><option value="trend">상승 눌림형 (실험)</option></select></label></p><div id="reverseRules"><p>20일 하락 추세 + 선택한 하락폭·하락일 수 + 전일 거래량 2배 이상 + D-3 대비 2배 이상 · RSI 제외</p><small>최근 20일과 오늘의 일별 고가가 시가 또는 전일 종가 대비 한 번이라도 30% 이상 상승하면 제외. 과거 급등 이력 부족 시 제외. 강화 일봉 조건 충족 WATCH. 가장 최근 완료 1시간봉의 SMA5 상향 돌파만 BUY 후보. 미완료 봉 제외. BUY/WATCH 모의매수 모두 검색된 1시간 동안 유효. WATCH는 5이평 상향 돌파 미확인 상태의 비교 실험이며 BUY로 변경하지 않음. 검증 전 실전 매수 권장 아님. 검색 시점의 가격은 실제 체결가가 아니며 당일 시가 진입 백테스트와 다를 수 있음. 최고가 매도 가정 결과는 실제 승률이 아님. 자동 주문 없음. 별도 매도·손절 신호 미검증. 기존 보유 종목은 직접 제외.</small></div><div id="trendRules" hidden><p>선택 시간봉: 눌림 전 20개 봉 상승폭 10% 이상 · 연속 음봉 2~4개 · 종가 눌림 3~10% (저가 최대 10%) · 눌림 평균 거래량 감소.</p><small>4시간봉은 KST 00/04/08/12/16/20시, 1시간봉으로 합성(누락 봉 제외). BUY: 완료 1시간봉 양봉·직전 고가 돌파·직전 3시간 평균 거래량 1.2배 이상, 이전 고점 미초과. WATCH는 반등 미확인. 일봉 추세 필터는 아직 적용하지 않음. 15일 백테스트는 같은 조건의 BUY만 다음 시간봉 시가 진입, 1·3일 수익률, 동일 코인 72시간 중복 제외. 현재 상장종목 기준, 누락은 제외. 실제 주문 없음.</small><p>시간봉 <select id="trendTf"><option value="T4">4시간봉</option><option value="T1">1시간봉</option></select><button id="back">15일 백테스트</button></p></div><p><label id="presetLabel" for="preset">검색 조건</label><select id="preset"><option value="A">A · 30% / 15일</option><option value="B">B · 35% / 14일</option><option value="C">C · 40% / 15일</option><option value="D" selected>D · 35% / 13일 (완화 테스트)</option></select><button id="go">전체 코인 검색 시작</button></p><p id="applied" class="muted">조건을 선택하고 검색해 주세요.</p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section><h2>모의매매 계좌</h2><small>가상 원금 100만원, 1회 투자금 변경 가능. 현재가 조회 후 전량 가상 체결(편도 0.04%). 직접 매수/매도, 7일 경과 표시만 제공. 실제 주문 없음. 기록은 이 브라우저에만 저장, 기기·브라우저 변경/데이터 삭제 시 유실될 수 있음. CSV 백업 권장.</small><p>1회 투자금(원) <input id="paperAmount" type="number" min="5000" step="1000" value="100000" style="width:105px"><button id="refreshPaper">보유 가격 갱신</button><button id="paperCSV">모의매매 CSV</button></p><p id="paperSummary"></p><p id="paperWin"></p><p id="paperMsg" class="muted"></p><h3>보유 종목</h3><div class="cards" id="paperOpen"></div><details><summary>청산 기록 (최근 100건)</summary><div class="cards" id="paperClosed"></div></details></section><section id="out" hidden><h2 id="headline"></h2><p id="audit" class="muted"></p><p id="notice" class="muted"></p><a id="featuresCSV" href="/csv/features?mode=live14">결과 CSV</a><a id="summaryCSV" href="/csv/summary?mode=live14">요약 CSV</a><a id="coverageCSV" href="/csv/coverage?mode=live14">수집 상태 CSV</a><a id="errorsCSV" href="/csv/errors?mode=live14">오류 CSV</a><p><input id="query" placeholder="코인 검색" style="width:105px"><select id="sort"><option value="market">종목명 순</option><option value="volume">거래량 증가율 순</option><option value="depth">고점 대비 하락폭 순</option><option value="gap">당일 시가 대비 변동률 순</option></select></p><p id="count"></p><div class="cards" id="cards"></div><button id="more">더 보기</button></section><script>
const $=id=>document.getElementById(id);let result=null,loading=false,limit=5,loadedScan=null;const names={A:"A · 30% / 15일",B:"B · 35% / 14일",C:"C · 40% / 15일",D:"D · 35% / 13일",T4:"상승 눌림 · 4시간봉",T1:"상승 눌림 · 1시간봉"};
function num(x){return typeof x==='number'?x.toLocaleString('ko-KR',{maximumSignificantDigits:8}):'—'}
function pct(x){return typeof x==='number'?(x>0?'+':'')+x.toFixed(2)+'%':'—'}
function node(tag,txt,cls){let e=document.createElement(tag);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
function line(card,k,v,cls){let r=node('div',null,'line');r.append(node('span',k,'muted'),node('strong',v,cls));card.append(r)}
function render(){if(!result)return;if(result.summary[0].strategy==="trend"){renderTrend();return;}let q=$('query').value.trim().toUpperCase(),sort=$('sort').value;let a=result.rank.filter(x=>x.market.includes(q));let key={volume:'volume_increase_pct',depth:'peak_to_last_pct',gap:'change_from_open_pct'}[sort];if(key)a.sort((x,y)=>sort==='depth'?(x[key]??Infinity)-(y[key]??Infinity):(y[key]??-Infinity)-(x[key]??-Infinity));else a.sort((x,y)=>x.market.localeCompare(y.market));$('count').textContent=a.length+'종목 · '+Math.min(limit,a.length)+'개 표시';a.sort((x,y)=>Number(y.status==="BUY")-Number(x.status==="BUY"));let cards=a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+(x.status==='BUY'?'BUY 후보':'WATCH'),x.status==='BUY'?'green':'muted'));line(c,'적용 검색식',names[x.preset]||x.preset);line(c,'검색 시 참고가',num(x.reference_price)+'원');line(c,'당일 시가',num(x.day_open)+'원');line(c,'시가 대비',pct(x.change_from_open_pct),x.change_from_open_pct>=0?'green':'red');line(c,'당일 최고 상승률',pct(x.day_high_pct));line(c,'20일+오늘 최대 일중 급등',pct(x.lookback_max_spike_pct));line(c,'최대 급등 날짜',x.lookback_max_spike_date||'—');line(c,'20일 실제 하락일',x.down_days+'/20일');line(c,'20일 종가 변화',pct(x.trend_net_pct),'red');line(c,'고점 대비 전일 종가',pct(x.peak_to_last_pct),'red');line(c,'거래량 배수',num(x.vol_ratio_d3)+' → '+num(x.vol_ratio_d1)+'배','green');line(c,'거래량 배수 증가율',pct(x.volume_increase_pct));line(c,'참고가 기준 본전',num(x.breakeven_price)+'원');line(c,'신호 확정(KST)',x.signal_time);line(c,'가격 조회(KST)',x.observed_at);line(c,'1시간봉 5이평',num(x.hour_ma5));line(c,'1시간봉 확인',x.hour_valid?x.hour_signal_time:'데이터 부족');line(c,'7일째 일봉 날짜',x.planned_last_day);let b=node('button',paperStats().open.some(t=>t.market===x.market)?'모의 보유 중':x.status==='WATCH'?'WATCH 모의매수':'BUY 모의매수');b.disabled=!['BUY','WATCH'].includes(x.status)||paperBusy||!storageOK||paperStats().open.some(t=>t.market===x.market);b.onclick=()=>paperBuy(x);c.append(b);return c});$('cards').replaceChildren(...cards);$('more').hidden=a.length<=limit}
function show(){let x=result.summary[0];if(x.strategy==="trend"){showTrend(x);return;}$('applied').textContent='결과 적용 조건: '+names[x.preset];$('headline').textContent='BUY '+x.buys+'개 · WATCH '+x.watches+'개 · 20일+오늘 급등 제외 '+x.spike_excluded+'개';$('audit').textContent='신호 확정 '+x.signal_time+' KST · 검색 완료 '+x.finished_at+' KST · 분석 '+x.valid+'/'+x.universe+' · 이력 부족 '+x.history_missing+' · 현재가 없음 '+x.no_quote+' · 급등 이력 미확인 '+x.spike_history_missing;$('notice').textContent='수수료 편도 0.04% 적용. 본전가는 참고가 기준 이론값(호가단위·슬리피지 미반영). 직접 매매 전 현재가와 보유 여부 확인.';limit=5;render()}

let resultMode=null;
function syncMode(){let trend=$('strategy').value==='trend';$('reverseRules').hidden=trend;$('trendRules').hidden=!trend;$('preset').hidden=trend;$('presetLabel').hidden=trend;}
function clearResult(){result=null;loadedScan=null;resultMode=null;$('out').hidden=true;}
$('strategy').onchange=()=>{syncMode();clearResult();$('applied').textContent='전략을 선택하고 새로 검색해 주세요.'};
$('trendTf').onchange=()=>{clearResult();$('applied').textContent='시간봉 변경: 다시 검색해 주세요.'};
$('preset').onchange=()=>{clearResult();$('applied').textContent='조건 변경: 다시 검색해 주세요.'};
$('query').oninput=$('sort').onchange=()=>{limit=5;render()};$('more').onclick=()=>{limit+=5;render()};
async function begin20(back){try{$('go').disabled=true;$('back').disabled=true;$('strategy').disabled=true;$('preset').disabled=true;$('trendTf').disabled=true;let trend=$('strategy').value==='trend';let url=trend?'/trend20/start?preset='+encodeURIComponent($('trendTf').value)+'&back='+(back?'1':'0'):'/live14/start?preset='+encodeURIComponent($('preset').value);let r=await fetch(url,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 검색 실행 중');clearResult();await poll()}catch(e){$('phase').textContent=e.message;for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=false}}
$('go').onclick=()=>begin20(false);$('back').onclick=()=>begin20(true);
function showTrend(x){
 $('applied').textContent='결과 적용 조건: '+names[x.preset];
 $('headline').textContent=resultMode==='trendback20'?'15일 과거 BUY 진입 '+x.matches+'건 · 3일 평균 '+pct(x.day3_mean_pct):'BUY '+x.buys+'개 · WATCH '+x.watches+'개';
 $('audit').textContent='분석 '+x.valid+'/'+x.universe+' · 이력 부족 종목 '+x.history_missing+' · 검색 완료 '+x.finished_at;
 $('notice').textContent=resultMode==='trendback20'?'1일 평균 '+pct(x.day1_mean_pct)+' · 3일 최고 순수익 5% 도달률 '+pct(x.day3_hit5_pct)+' · 평가 누락 '+x.outcome_missing+'건 · 누락 시간 '+x.missing_hours+' · 가격 범위 제외 '+x.price_skipped+' · 매수 후 72시간 중복 제외':'상승 눌림 실험 조건 · 편도 0.04% · BUY/WATCH 모의매수만 가능';
 limit=5;render();
}
function renderTrend(){
 let q=$('query').value.trim().toUpperCase();let a=result.rank.filter(x=>x.market.includes(q));
 if(resultMode!=='trendback20'){let sort=$('sort').value;if(sort==='volume')a.sort((x,y)=>y.confirm_volume_ratio-x.confirm_volume_ratio);else if(sort==='depth')a.sort((x,y)=>y.pullback_pct-x.pullback_pct);else if(sort==='market')a.sort((x,y)=>x.market.localeCompare(y.market));a.sort((x,y)=>Number(y.status==='BUY')-Number(x.status==='BUY'))}
 $('count').textContent=a.length+'건 · '+Math.min(limit,a.length)+'개 표시';$('more').hidden=a.length<=limit;
 $('cards').replaceChildren(...a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+x.status,x.status==='BUY'?'green':'muted'));line(c,'전략',names[x.preset]);line(c,'이전 20봉 상승',pct(x.prior_gain_pct));line(c,'연속 음봉',x.pullback_bars+'개');line(c,'눌림폭',pct(-x.pullback_pct));line(c,'눌림 거래량 / 이전 평균',num(x.pullback_volume_ratio)+'배');line(c,'반등 시간봉 거래량',num(x.confirm_volume_ratio)+'배');line(c,'신호 확정(KST)',x.signal_time);
 if(resultMode==='trendback20'){line(c,'진입가',num(x.buy_price)+'원');line(c,'1일 종료 / 최고',pct(x.day1_close_pct)+' / '+pct(x.day1_peak_pct));line(c,'3일 종료 / 최고',pct(x.day3_close_pct)+' / '+pct(x.day3_peak_pct));line(c,'3일 최저',pct(x.day3_low_pct));}
 else{line(c,'검색 참고가',num(x.reference_price)+'원');line(c,'이전 고점',num(x.prior_peak)+'원');line(c,'눌림 저점',num(x.pullback_low)+'원');let held=paperStats().open.some(t=>t.market===x.market),b=node('button',held?'모의 보유 중':x.status+' 모의매수');b.disabled=held||paperBusy||!storageOK;b.onclick=()=>paperBuy(x);c.append(b)}return c}));
}
async function poll(){try{
 let s=await(await fetch('/status',{cache:'no-store'})).json();let running=s.status==='running';
 for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=running;
 if(!['live14','trend20','trendback20'].includes(s.mode)){$('phase').textContent=running?'다른 연구 실행 중':'대기';clearResult();return}
 $('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');
 if(running){clearResult();$('applied').textContent='검색 중: '+names[s.preset];return}
 if(s.status==='done'&&loadedScan!==s.scan_id&&!loading){
  // Do not overwrite a user's deliberate strategy selection after a previous completed scan.
  if((s.mode==='live14')!==($('strategy').value==='reverse'))return;
  if(s.mode==='live14'&&$('preset').value!==s.preset)return;
  if(s.mode!=='live14'&&$('trendTf').value!==s.preset)return;
  loading=true;try{let d=await(await fetch('/results',{cache:'no-store'})).json();if(!d.summary[0]||d.summary[0].scan_id!==s.scan_id)return;result=d;loadedScan=s.scan_id;resultMode=s.mode;if(s.mode==='live14')$('preset').value=s.preset;
  for(let k of ['features','summary','coverage','errors'])$(k+'CSV').href='/csv/'+k+'?mode='+s.mode;
  $('out').hidden=false;show()}finally{loading=false}
 }
 }catch(e){$('phase').textContent='연결 확인: '+e.message}}
syncMode();

const FEE=.0004,STORE='reverse-swing-paper-v1',INITIAL=1000000;
let account={trades:[]},storageOK=true,paperBusy=false,quotes={};
try{let raw=localStorage.getItem(STORE);if(raw){let a=JSON.parse(raw);if(!Array.isArray(a.trades))throw Error('invalid data');account=a}}catch(e){storageOK=false}
function savePaper(next){localStorage.setItem(STORE,JSON.stringify(next));account=next}
function paperStats(){let closed=account.trades.filter(x=>x.closedAt),open=account.trades.filter(x=>!x.closedAt),real=closed.reduce((s,x)=>s+x.pnl,0),spent=open.reduce((s,x)=>s+x.amount,0);return {closed,open,real,cash:INITIAL+real-spent,wins:closed.filter(x=>x.pnl>0).length,losses:closed.filter(x=>x.pnl<0).length}}
function paperMsg(s){$('paperMsg').textContent=s}
function paperTime(t){return new Date(t).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})}
async function quote(markets){let all={};for(let i=0;i<markets.length;i+=40){let r=await fetch('/paper/quotes?markets='+encodeURIComponent(markets.slice(i,i+40).join(',')),{cache:'no-store'});if(!r.ok)throw Error('현재가 조회 실패');let d=await r.json();Object.assign(all,d.quotes)}return all}
async function paperBuy(x){
 if(paperBusy||!storageOK||!result||!['BUY','WATCH'].includes(x.status))return;
 let a=Number($('paperAmount').value),s=paperStats();
 if(!Number.isFinite(a)||a<5000||a>s.cash){paperMsg('가상 잔액 내에서 5,000원 이상 입력해 주세요.');return}
 if(s.open.some(t=>t.market===x.market)){paperMsg('이미 모의 보유 중인 종목입니다.');return}
 if(s.closed.some(t=>t.market===x.market&&t.signalTime===x.signal_time)){paperMsg('같은 신호에서 이미 매매한 종목입니다.');return}
 paperBusy=true;renderPaper();paperMsg('새로운 현재가 조회 중...');
 try{let r=await fetch((x.strategy==='trend'?'/trend20/entry':'/paper/entry')+'?market='+encodeURIComponent(x.market)+'&signal='+encodeURIComponent(x.signal_time)+'&hour='+encodeURIComponent(x.hour_signal_time)+'&preset='+encodeURIComponent(x.preset)+'&scan_id='+encodeURIComponent(result.summary[0].scan_id)+'&entry_status='+encodeURIComponent(x.status),{cache:'no-store'});if(!r.ok)throw Error('신호 만료, 진입 범위 제외 또는 가격 조회 실패. 다시 검색해 주세요.');let q=await r.json();if(q.entry_status!==x.status)throw Error('검색 판정 변경. 다시 검색해 주세요.');let p=q.price;if(!Number.isFinite(p)||p<=0)throw Error('유효한 현재가 없음');
 let t={id:Date.now()+'-'+x.market,market:x.market,signalTime:x.signal_time,boughtAt:q.observed_ms,amount:a,buy:p,qty:a/(1+FEE)/p,deadline:q.observed_ms+7*86400000,signalDeadline:q.signal_deadline_ms,preset:q.preset,minDrawdownPct:q.min_drawdown_pct,minDownDays:q.min_down_days,entryStatus:q.entry_status,strategy:x.strategy||'reverse',timeframe:x.strategy==='trend'?x.preset:null};savePaper({trades:[...account.trades,t]});quotes[x.market]=q;paperMsg(x.market+' '+q.entry_status+' 모의매수 완료 (시장 참고가 가정).');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function paperSell(t){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('매도 참고가 조회 중...');
 try{let q=(await quote([t.market]))[t.market];if(!q)throw Error('가격 확인 불가');let proceeds=t.qty*q.price*(1-FEE),pnl=proceeds-t.amount;let closed={...t,closedAt:q.observed_ms,sell:q.price,proceeds,pnl,netPct:100*pnl/t.amount};savePaper({trades:account.trades.map(x=>x.id===t.id?closed:x)});quotes[t.market]=q;paperMsg(t.market+' 모의매도 완료. 순손익 '+num(pnl)+'원');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function refreshPaper(){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('보유 종목 가격 조회 중...');
 try{let m=paperStats().open.map(x=>x.market);if(m.length)quotes=await quote(m);paperMsg('가격 갱신 완료. 가격 확인 불가 종목은 — 표시.')}catch(e){quotes={};paperMsg(e.message)}finally{paperBusy=false;renderPaper()}
}
function renderPaper(){
 let s=paperStats();$('paperSummary').textContent='가상 원금 1,000,000원 · 잔액 '+num(s.cash)+'원 · 실현손익 '+num(s.real)+'원';$('paperWin').textContent='보유 '+s.open.length+' · 청산 '+s.closed.length+' · 수익 '+s.wins+' · 손실 '+s.losses+' · 승률 '+(s.closed.length?(100*s.wins/s.closed.length).toFixed(2)+'%':'—');
 $('refreshPaper').disabled=paperBusy||!storageOK;
 let cards=s.open.map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 모의 보유'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매수가',num(t.buy)+'원');line(c,'투자금(수수료 포함)',num(t.amount)+'원');let q=quotes[t.market];let val=q?t.qty*q.price*(1-FEE)-t.amount:null;line(c,'조회 가격',q?num(q.price)+'원':'—');line(c,'가격 조회',q?paperTime(q.observed_ms):'—');line(c,'평가 순손익',val===null?'—':num(val)+'원 / '+pct(100*val/t.amount),val===null?'muted':val>=0?'green':'red');line(c,'7일 보유 만료',paperTime(t.deadline),Date.now()>=t.deadline?'red':'muted');if(Date.now()>=t.deadline)c.append(node('p','7일 경과: 직접 모의매도 필요','red'));let b=node('button','모의매도');b.disabled=paperBusy||!storageOK;b.onclick=()=>paperSell(t);c.append(b);return c});
 $('paperOpen').replaceChildren(...cards);if(!cards.length)$('paperOpen').textContent='보유 종목 없음';
 let hist=s.closed.slice().reverse().slice(0,100).map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 청산 완료'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 / 매도',num(t.buy)+' / '+num(t.sell));line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매도 시각',paperTime(t.closedAt));line(c,'순손익',num(t.pnl)+'원 / '+pct(t.netPct),t.pnl>=0?'green':'red');return c});$('paperClosed').replaceChildren(...hist);if(!hist.length)$('paperClosed').textContent='청산 기록 없음';if(!storageOK)paperMsg('브라우저 저장소 사용 불가 또는 기록 손상. 모의매매를 중단합니다.');
}
$('refreshPaper').onclick=refreshPaper;
$('paperCSV').onclick=()=>{let keys=['market','strategy','timeframe','preset','entryStatus','minDrawdownPct','minDownDays','signalTime','boughtAt','amount','buy','qty','deadline','closedAt','sell','proceeds','pnl','netPct'];let esc=v=>'"'+String(v??'').replaceAll('"','""')+'"';let rows=[keys.join(','),...account.trades.map(x=>keys.map(k=>esc(x[k])).join(','))];let url=URL.createObjectURL(new Blob(['\ufeff'+rows.join('\r\n')],{type:'text/csv;charset=utf-8'}));let a=document.createElement('a');a.href=url;a.download='BITHUMB_SWING_V2_0_TEST_PAPER_'+new Date().toISOString().slice(0,10)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
renderPaper();

poll();setInterval(poll,2000);
</script></html>"""
HTML21=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BITHUMB SWING V2.1 TEST</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1000px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:10px;margin:4px;border-radius:7px;text-decoration:none}input{background:#10151d}button:disabled{opacity:.5}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:10px}.coin{background:#111923;border:1px solid #3b4d60;border-radius:12px;padding:12px}.coin h3{margin:0 0 8px}.line{display:flex;justify-content:space-between;gap:10px;margin:6px 0}.green{color:#72ddb0}.red{color:#ff9b9b}.muted{color:#a9bac9}h1{font-size:23px;margin-bottom:4px}</style><h1>BITHUMB SWING V2.1 TEST</h1><a href="/live143">기존 V1.4.3 보존판</a><section><h2>매수 후보 검색</h2><p><label>전략 <select id="strategy"><option value="reverse">저점 반등형 (기존)</option><option value="trend">상승 눌림형 (실험)</option></select></label></p><div id="reverseRules"><p>20일 하락 추세 + 선택한 하락폭·하락일 수 + 전일 거래량 2배 이상 + D-3 대비 2배 이상 · RSI 제외</p><small>최근 20일과 오늘의 일별 고가가 시가 또는 전일 종가 대비 한 번이라도 30% 이상 상승하면 제외. 과거 급등 이력 부족 시 제외. 강화 일봉 조건 충족 WATCH. 가장 최근 완료 1시간봉의 SMA5 상향 돌파만 BUY 후보. 미완료 봉 제외. BUY/WATCH 모의매수 모두 검색된 1시간 동안 유효. WATCH는 5이평 상향 돌파 미확인 상태의 비교 실험이며 BUY로 변경하지 않음. 검증 전 실전 매수 권장 아님. 검색 시점의 가격은 실제 체결가가 아니며 당일 시가 진입 백테스트와 다를 수 있음. 최고가 매도 가정 결과는 실제 승률이 아님. 자동 주문 없음. 별도 매도·손절 신호 미검증. 기존 보유 종목은 직접 제외.</small></div><div id="trendRules" hidden><p id="newRules">신규 15/20: 완료된 240분봉 20개 중 종가 &lt; 시가인 봉 15개 이상. 연속 불필요. 상승폭·눌림폭·4시간 거래량 필터 없음. BUY는 기존 1시간봉 양봉·직전 고가 돌파·직전 3시간 평균 거래량 1.2배 이상. 실제 거래소 240분봉 경계 사용. 15일 진입 검증, 다음 시간봉 시가 진입 후 1·3일 평가. 누락 제외.</p><div id="oldTrendRules"><p>선택 시간봉: 눌림 전 20개 봉 상승폭 10% 이상 · 연속 음봉 2~4개 · 종가 눌림 3~10% (저가 최대 10%) · 눌림 평균 거래량 감소.</p><small>4시간봉은 KST 00/04/08/12/16/20시, 1시간봉으로 합성(누락 봉 제외). BUY: 완료 1시간봉 양봉·직전 고가 돌파·직전 3시간 평균 거래량 1.2배 이상, 이전 고점 미초과. WATCH는 반등 미확인. 일봉 추세 필터는 아직 적용하지 않음. 15일 백테스트는 같은 조건의 BUY만 다음 시간봉 시가 진입, 1·3일 수익률, 동일 코인 72시간 중복 제외. 현재 상장종목 기준, 누락은 제외. 실제 주문 없음.</small></div><p>시간봉 <select id="trendTf"><option value="T415" selected>4시간봉 · 20개 중 15개 이상 음봉 (신규)</option><option value="T4">4시간봉 (기존)</option><option value="T1">1시간봉</option></select><button id="back">15일 백테스트</button></p></div><p><label id="presetLabel" for="preset">검색 조건</label><select id="preset"><option value="A">A · 30% / 15일</option><option value="B">B · 35% / 14일</option><option value="C">C · 40% / 15일</option><option value="D" selected>D · 35% / 13일 (완화 테스트)</option></select><button id="go">전체 코인 검색 시작</button></p><p id="applied" class="muted">조건을 선택하고 검색해 주세요.</p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section><h2>모의매매 계좌</h2><small>가상 원금 100만원, 1회 투자금 변경 가능. 현재가 조회 후 전량 가상 체결(편도 0.04%). 직접 매수/매도, 7일 경과 표시만 제공. 실제 주문 없음. 기록은 이 브라우저에만 저장, 기기·브라우저 변경/데이터 삭제 시 유실될 수 있음. CSV 백업 권장.</small><p>1회 투자금(원) <input id="paperAmount" type="number" min="5000" step="1000" value="100000" style="width:105px"><button id="refreshPaper">보유 가격 갱신</button><button id="paperCSV">모의매매 CSV</button></p><p id="paperSummary"></p><p id="paperWin"></p><p id="paperMsg" class="muted"></p><h3>보유 종목</h3><div class="cards" id="paperOpen"></div><details><summary>청산 기록 (최근 100건)</summary><div class="cards" id="paperClosed"></div></details></section><section id="out" hidden><h2 id="headline"></h2><p id="audit" class="muted"></p><p id="notice" class="muted"></p><a id="featuresCSV" href="/csv/features?mode=live14">결과 CSV</a><a id="summaryCSV" href="/csv/summary?mode=live14">요약 CSV</a><a id="coverageCSV" href="/csv/coverage?mode=live14">수집 상태 CSV</a><a id="errorsCSV" href="/csv/errors?mode=live14">오류 CSV</a><p><input id="query" placeholder="코인 검색" style="width:105px"><select id="sort"><option value="market">종목명 순</option><option value="volume">거래량 증가율 순</option><option value="depth">고점 대비 하락폭 순</option><option value="gap">당일 시가 대비 변동률 순</option></select></p><p id="count"></p><div class="cards" id="cards"></div><button id="more">더 보기</button></section><script>
const $=id=>document.getElementById(id);let result=null,loading=false,limit=5,loadedScan=null;const names={A:"A · 30% / 15일",B:"B · 35% / 14일",C:"C · 40% / 15일",D:"D · 35% / 13일",T4:"상승 눌림 · 4시간봉",T1:"상승 눌림 · 1시간봉",T415:"4시간봉 음봉 15/20"};
function num(x){return typeof x==='number'?x.toLocaleString('ko-KR',{maximumSignificantDigits:8}):'—'}
function pct(x){return typeof x==='number'?(x>0?'+':'')+x.toFixed(2)+'%':'—'}
function node(tag,txt,cls){let e=document.createElement(tag);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
function line(card,k,v,cls){let r=node('div',null,'line');r.append(node('span',k,'muted'),node('strong',v,cls));card.append(r)}
function render(){if(!result)return;if(result.summary[0].strategy==="trend"){renderTrend();return;}let q=$('query').value.trim().toUpperCase(),sort=$('sort').value;let a=result.rank.filter(x=>x.market.includes(q));let key={volume:'volume_increase_pct',depth:'peak_to_last_pct',gap:'change_from_open_pct'}[sort];if(key)a.sort((x,y)=>sort==='depth'?(x[key]??Infinity)-(y[key]??Infinity):(y[key]??-Infinity)-(x[key]??-Infinity));else a.sort((x,y)=>x.market.localeCompare(y.market));$('count').textContent=a.length+'종목 · '+Math.min(limit,a.length)+'개 표시';a.sort((x,y)=>Number(y.status==="BUY")-Number(x.status==="BUY"));let cards=a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+(x.status==='BUY'?'BUY 후보':'WATCH'),x.status==='BUY'?'green':'muted'));line(c,'적용 검색식',names[x.preset]||x.preset);line(c,'검색 시 참고가',num(x.reference_price)+'원');line(c,'당일 시가',num(x.day_open)+'원');line(c,'시가 대비',pct(x.change_from_open_pct),x.change_from_open_pct>=0?'green':'red');line(c,'당일 최고 상승률',pct(x.day_high_pct));line(c,'20일+오늘 최대 일중 급등',pct(x.lookback_max_spike_pct));line(c,'최대 급등 날짜',x.lookback_max_spike_date||'—');line(c,'20일 실제 하락일',x.down_days+'/20일');line(c,'20일 종가 변화',pct(x.trend_net_pct),'red');line(c,'고점 대비 전일 종가',pct(x.peak_to_last_pct),'red');line(c,'거래량 배수',num(x.vol_ratio_d3)+' → '+num(x.vol_ratio_d1)+'배','green');line(c,'거래량 배수 증가율',pct(x.volume_increase_pct));line(c,'참고가 기준 본전',num(x.breakeven_price)+'원');line(c,'신호 확정(KST)',x.signal_time);line(c,'가격 조회(KST)',x.observed_at);line(c,'1시간봉 5이평',num(x.hour_ma5));line(c,'1시간봉 확인',x.hour_valid?x.hour_signal_time:'데이터 부족');line(c,'7일째 일봉 날짜',x.planned_last_day);let b=node('button',paperStats().open.some(t=>t.market===x.market)?'모의 보유 중':x.status==='WATCH'?'WATCH 모의매수':'BUY 모의매수');b.disabled=!['BUY','WATCH'].includes(x.status)||paperBusy||!storageOK||paperStats().open.some(t=>t.market===x.market);b.onclick=()=>paperBuy(x);c.append(b);return c});$('cards').replaceChildren(...cards);$('more').hidden=a.length<=limit}
function show(){let x=result.summary[0];if(x.strategy==="trend"){showTrend(x);return;}$('applied').textContent='결과 적용 조건: '+names[x.preset];$('headline').textContent='BUY '+x.buys+'개 · WATCH '+x.watches+'개 · 20일+오늘 급등 제외 '+x.spike_excluded+'개';$('audit').textContent='신호 확정 '+x.signal_time+' KST · 검색 완료 '+x.finished_at+' KST · 분석 '+x.valid+'/'+x.universe+' · 이력 부족 '+x.history_missing+' · 현재가 없음 '+x.no_quote+' · 급등 이력 미확인 '+x.spike_history_missing;$('notice').textContent='수수료 편도 0.04% 적용. 본전가는 참고가 기준 이론값(호가단위·슬리피지 미반영). 직접 매매 전 현재가와 보유 여부 확인.';limit=5;render()}

let resultMode=null;
function syncMode(){let trend=$('strategy').value==='trend';$('newRules').hidden=$('trendTf').value!=='T415';$('oldTrendRules').hidden=$('trendTf').value==='T415';$('reverseRules').hidden=trend;$('trendRules').hidden=!trend;$('preset').hidden=trend;$('presetLabel').hidden=trend;}
function clearResult(){result=null;loadedScan=null;resultMode=null;$('out').hidden=true;}
$('strategy').onchange=()=>{syncMode();clearResult();$('applied').textContent='전략을 선택하고 새로 검색해 주세요.'};
$('trendTf').onchange=()=>{syncMode();clearResult();$('applied').textContent='시간봉 변경: 다시 검색해 주세요.'};
$('preset').onchange=()=>{clearResult();$('applied').textContent='조건 변경: 다시 검색해 주세요.'};
$('query').oninput=$('sort').onchange=()=>{limit=5;render()};$('more').onclick=()=>{limit+=5;render()};
async function begin20(back){try{$('go').disabled=true;$('back').disabled=true;$('strategy').disabled=true;$('preset').disabled=true;$('trendTf').disabled=true;let trend=$('strategy').value==='trend';let url=trend?'/trend20/start?preset='+encodeURIComponent($('trendTf').value)+'&back='+(back?'1':'0'):'/live14/start?preset='+encodeURIComponent($('preset').value);let r=await fetch(url,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 검색 실행 중');clearResult();await poll()}catch(e){$('phase').textContent=e.message;for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=false}}
$('go').onclick=()=>begin20(false);$('back').onclick=()=>begin20(true);
function showTrend(x){
 $('applied').textContent='결과 적용 조건: '+names[x.preset];
 $('headline').textContent=resultMode==='trendback20'?'15일 과거 BUY 진입 '+x.matches+'건 · 3일 평균 '+pct(x.day3_mean_pct):'BUY '+x.buys+'개 · WATCH '+x.watches+'개';
 $('audit').textContent='분석 '+x.valid+'/'+x.universe+' · 이력 부족 종목 '+x.history_missing+' · 검색 완료 '+x.finished_at;
 $('notice').textContent=resultMode==='trendback20'?'1일 평균 '+pct(x.day1_mean_pct)+' · 3일 최고 순수익 5% 도달률 '+pct(x.day3_hit5_pct)+' · 평가 누락 '+x.outcome_missing+'건 · 누락 시간 '+x.missing_hours+' · 가격 범위 제외 '+x.price_skipped+' · 매수 후 72시간 중복 제외':'상승 눌림 실험 조건 · 편도 0.04% · BUY/WATCH 모의매수만 가능';
 limit=5;render();
}
function renderTrend(){
 let q=$('query').value.trim().toUpperCase();let a=result.rank.filter(x=>x.market.includes(q));
 if(resultMode!=='trendback20'){let sort=$('sort').value;if(sort==='volume')a.sort((x,y)=>y.confirm_volume_ratio-x.confirm_volume_ratio);else if(sort==='depth')a.sort((x,y)=>y.pullback_pct-x.pullback_pct);else if(sort==='market')a.sort((x,y)=>x.market.localeCompare(y.market));a.sort((x,y)=>Number(y.status==='BUY')-Number(x.status==='BUY'))}
 $('count').textContent=a.length+'건 · '+Math.min(limit,a.length)+'개 표시';$('more').hidden=a.length<=limit;
 $('cards').replaceChildren(...a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+x.status,x.status==='BUY'?'green':'muted'));line(c,'전략',names[x.preset]);if(x.preset==='T415'){line(c,'음봉 수',x.down_bars+'/20개');line(c,'20봉 변화',pct(x.window_change_pct));line(c,'고점 대비 변화',pct(-x.pullback_pct));line(c,'마지막 4h 거래량 / 이전 19봉',num(x.last_4h_volume_ratio)+'배');line(c,'4h 조건 확정',x.setup_end);}else{line(c,'이전 20봉 상승',pct(x.prior_gain_pct));line(c,'연속 음봉',x.pullback_bars+'개');line(c,'눌림폭',pct(-x.pullback_pct));line(c,'눌림 거래량 / 이전 평균',num(x.pullback_volume_ratio)+'배');}line(c,'반등 시간봉 거래량',num(x.confirm_volume_ratio)+'배');line(c,'신호 확정(KST)',x.signal_time);
 if(resultMode==='trendback20'){line(c,'진입가',num(x.buy_price)+'원');line(c,'1일 종료 / 최고',pct(x.day1_close_pct)+' / '+pct(x.day1_peak_pct));line(c,'3일 종료 / 최고',pct(x.day3_close_pct)+' / '+pct(x.day3_peak_pct));line(c,'3일 최저',pct(x.day3_low_pct));}
 else{line(c,'검색 참고가',num(x.reference_price)+'원');line(c,'이전 고점',num(x.prior_peak)+'원');line(c,'눌림 저점',num(x.pullback_low)+'원');let held=paperStats().open.some(t=>t.market===x.market),b=node('button',held?'모의 보유 중':x.status+' 모의매수');b.disabled=held||paperBusy||!storageOK;b.onclick=()=>paperBuy(x);c.append(b)}return c}));
}
async function poll(){try{
 let s=await(await fetch('/status',{cache:'no-store'})).json();let running=s.status==='running';
 for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=running;
 if(!['live14','trend20','trendback20'].includes(s.mode)){$('phase').textContent=running?'다른 연구 실행 중':'대기';clearResult();return}
 $('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');
 if(running){clearResult();$('applied').textContent='검색 중: '+names[s.preset];return}
 if(s.status==='done'&&loadedScan!==s.scan_id&&!loading){
  // Do not overwrite a user's deliberate strategy selection after a previous completed scan.
  if((s.mode==='live14')!==($('strategy').value==='reverse'))return;
  if(s.mode==='live14'&&$('preset').value!==s.preset)return;
  if(s.mode!=='live14'&&$('trendTf').value!==s.preset)return;
  loading=true;try{let d=await(await fetch('/results',{cache:'no-store'})).json();if(!d.summary[0]||d.summary[0].scan_id!==s.scan_id)return;result=d;loadedScan=s.scan_id;resultMode=s.mode;if(s.mode==='live14')$('preset').value=s.preset;
  for(let k of ['features','summary','coverage','errors'])$(k+'CSV').href='/csv/'+k+'?mode='+s.mode;
  $('out').hidden=false;show()}finally{loading=false}
 }
 }catch(e){$('phase').textContent='연결 확인: '+e.message}}
syncMode();

const FEE=.0004,STORE='reverse-swing-paper-v1',INITIAL=1000000;
let account={trades:[]},storageOK=true,paperBusy=false,quotes={};
try{let raw=localStorage.getItem(STORE);if(raw){let a=JSON.parse(raw);if(!Array.isArray(a.trades))throw Error('invalid data');account=a}}catch(e){storageOK=false}
function savePaper(next){localStorage.setItem(STORE,JSON.stringify(next));account=next}
function paperStats(){let closed=account.trades.filter(x=>x.closedAt),open=account.trades.filter(x=>!x.closedAt),real=closed.reduce((s,x)=>s+x.pnl,0),spent=open.reduce((s,x)=>s+x.amount,0);return {closed,open,real,cash:INITIAL+real-spent,wins:closed.filter(x=>x.pnl>0).length,losses:closed.filter(x=>x.pnl<0).length}}
function paperMsg(s){$('paperMsg').textContent=s}
function paperTime(t){return new Date(t).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})}
async function quote(markets){let all={};for(let i=0;i<markets.length;i+=40){let r=await fetch('/paper/quotes?markets='+encodeURIComponent(markets.slice(i,i+40).join(',')),{cache:'no-store'});if(!r.ok)throw Error('현재가 조회 실패');let d=await r.json();Object.assign(all,d.quotes)}return all}
async function paperBuy(x){
 if(paperBusy||!storageOK||!result||!['BUY','WATCH'].includes(x.status))return;
 let a=Number($('paperAmount').value),s=paperStats();
 if(!Number.isFinite(a)||a<5000||a>s.cash){paperMsg('가상 잔액 내에서 5,000원 이상 입력해 주세요.');return}
 if(s.open.some(t=>t.market===x.market)){paperMsg('이미 모의 보유 중인 종목입니다.');return}
 if(s.closed.some(t=>t.market===x.market&&t.signalTime===x.signal_time)){paperMsg('같은 신호에서 이미 매매한 종목입니다.');return}
 paperBusy=true;renderPaper();paperMsg('새로운 현재가 조회 중...');
 try{let r=await fetch((x.strategy==='trend'?'/trend20/entry':'/paper/entry')+'?market='+encodeURIComponent(x.market)+'&signal='+encodeURIComponent(x.signal_time)+'&hour='+encodeURIComponent(x.hour_signal_time)+'&preset='+encodeURIComponent(x.preset)+'&scan_id='+encodeURIComponent(result.summary[0].scan_id)+'&entry_status='+encodeURIComponent(x.status),{cache:'no-store'});if(!r.ok)throw Error('신호 만료, 진입 범위 제외 또는 가격 조회 실패. 다시 검색해 주세요.');let q=await r.json();if(q.entry_status!==x.status)throw Error('검색 판정 변경. 다시 검색해 주세요.');let p=q.price;if(!Number.isFinite(p)||p<=0)throw Error('유효한 현재가 없음');
 let t={id:Date.now()+'-'+x.market,market:x.market,signalTime:x.signal_time,boughtAt:q.observed_ms,amount:a,buy:p,qty:a/(1+FEE)/p,deadline:q.observed_ms+7*86400000,signalDeadline:q.signal_deadline_ms,preset:q.preset,minDrawdownPct:q.min_drawdown_pct,minDownDays:q.min_down_days,entryStatus:q.entry_status,strategy:x.strategy||'reverse',timeframe:x.strategy==='trend'?x.preset:null};savePaper({trades:[...account.trades,t]});quotes[x.market]=q;paperMsg(x.market+' '+q.entry_status+' 모의매수 완료 (시장 참고가 가정).');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function paperSell(t){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('매도 참고가 조회 중...');
 try{let q=(await quote([t.market]))[t.market];if(!q)throw Error('가격 확인 불가');let proceeds=t.qty*q.price*(1-FEE),pnl=proceeds-t.amount;let closed={...t,closedAt:q.observed_ms,sell:q.price,proceeds,pnl,netPct:100*pnl/t.amount};savePaper({trades:account.trades.map(x=>x.id===t.id?closed:x)});quotes[t.market]=q;paperMsg(t.market+' 모의매도 완료. 순손익 '+num(pnl)+'원');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function refreshPaper(){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('보유 종목 가격 조회 중...');
 try{let m=paperStats().open.map(x=>x.market);if(m.length)quotes=await quote(m);paperMsg('가격 갱신 완료. 가격 확인 불가 종목은 — 표시.')}catch(e){quotes={};paperMsg(e.message)}finally{paperBusy=false;renderPaper()}
}
function renderPaper(){
 let s=paperStats();$('paperSummary').textContent='가상 원금 1,000,000원 · 잔액 '+num(s.cash)+'원 · 실현손익 '+num(s.real)+'원';$('paperWin').textContent='보유 '+s.open.length+' · 청산 '+s.closed.length+' · 수익 '+s.wins+' · 손실 '+s.losses+' · 승률 '+(s.closed.length?(100*s.wins/s.closed.length).toFixed(2)+'%':'—');
 $('refreshPaper').disabled=paperBusy||!storageOK;
 let cards=s.open.map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 모의 보유'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매수가',num(t.buy)+'원');line(c,'투자금(수수료 포함)',num(t.amount)+'원');let q=quotes[t.market];let val=q?t.qty*q.price*(1-FEE)-t.amount:null;line(c,'조회 가격',q?num(q.price)+'원':'—');line(c,'가격 조회',q?paperTime(q.observed_ms):'—');line(c,'평가 순손익',val===null?'—':num(val)+'원 / '+pct(100*val/t.amount),val===null?'muted':val>=0?'green':'red');line(c,'7일 보유 만료',paperTime(t.deadline),Date.now()>=t.deadline?'red':'muted');if(Date.now()>=t.deadline)c.append(node('p','7일 경과: 직접 모의매도 필요','red'));let b=node('button','모의매도');b.disabled=paperBusy||!storageOK;b.onclick=()=>paperSell(t);c.append(b);return c});
 $('paperOpen').replaceChildren(...cards);if(!cards.length)$('paperOpen').textContent='보유 종목 없음';
 let hist=s.closed.slice().reverse().slice(0,100).map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 청산 완료'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 / 매도',num(t.buy)+' / '+num(t.sell));line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매도 시각',paperTime(t.closedAt));line(c,'순손익',num(t.pnl)+'원 / '+pct(t.netPct),t.pnl>=0?'green':'red');return c});$('paperClosed').replaceChildren(...hist);if(!hist.length)$('paperClosed').textContent='청산 기록 없음';if(!storageOK)paperMsg('브라우저 저장소 사용 불가 또는 기록 손상. 모의매매를 중단합니다.');
}
$('refreshPaper').onclick=refreshPaper;
$('paperCSV').onclick=()=>{let keys=['market','strategy','timeframe','preset','entryStatus','minDrawdownPct','minDownDays','signalTime','boughtAt','amount','buy','qty','deadline','closedAt','sell','proceeds','pnl','netPct'];let esc=v=>'"'+String(v??'').replaceAll('"','""')+'"';let rows=[keys.join(','),...account.trades.map(x=>keys.map(k=>esc(x[k])).join(','))];let url=URL.createObjectURL(new Blob(['\ufeff'+rows.join('\r\n')],{type:'text/csv;charset=utf-8'}));let a=document.createElement('a');a.href=url;a.download='BITHUMB_SWING_V2_1_TEST_PAPER_'+new Date().toISOString().slice(0,10)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
renderPaper();

poll();setInterval(poll,2000);
</script></html>"""
HTML22=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BITHUMB SWING V2.2 TEST</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1000px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:10px;margin:4px;border-radius:7px;text-decoration:none}input{background:#10151d}button:disabled{opacity:.5}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:10px}.coin{background:#111923;border:1px solid #3b4d60;border-radius:12px;padding:12px}.coin h3{margin:0 0 8px}.line{display:flex;justify-content:space-between;gap:10px;margin:6px 0}.green{color:#72ddb0}.red{color:#ff9b9b}.muted{color:#a9bac9}h1{font-size:23px;margin-bottom:4px}</style><h1>BITHUMB SWING V2.2 TEST</h1><a href="/live143">기존 V1.4.3 보존판</a><section><h2>매수 후보 검색</h2><p><label>전략 <select id="strategy"><option value="reverse">저점 반등형 (기존)</option><option value="trend">상승 눌림형 (실험)</option></select></label></p><div id="reverseRules"><p>20일 하락 추세 + 선택한 하락폭·하락일 수 + 전일 거래량 2배 이상 + D-3 대비 2배 이상 · RSI 제외</p><small>최근 20일과 오늘의 일별 고가가 시가 또는 전일 종가 대비 한 번이라도 30% 이상 상승하면 제외. 과거 급등 이력 부족 시 제외. 강화 일봉 조건 충족 WATCH. 가장 최근 완료 1시간봉의 SMA5 상향 돌파만 BUY 후보. 미완료 봉 제외. BUY/WATCH 모의매수 모두 검색된 1시간 동안 유효. WATCH는 5이평 상향 돌파 미확인 상태의 비교 실험이며 BUY로 변경하지 않음. 검증 전 실전 매수 권장 아님. 검색 시점의 가격은 실제 체결가가 아니며 당일 시가 진입 백테스트와 다를 수 있음. 최고가 매도 가정 결과는 실제 승률이 아님. 자동 주문 없음. 별도 매도·손절 신호 미검증. 기존 보유 종목은 직접 제외.</small></div><div id="trendRules" hidden><p id="newRules"><span id="directRuleText">신규 20/30: 완료된 240분봉 30개 중 종가 &lt; 시가인 봉 20개 이상.</span> 연속 불필요. 상승폭·눌림폭·4시간 거래량 필터 없음. BUY는 기존 1시간봉 양봉·직전 고가 돌파·직전 3시간 평균 거래량 1.2배 이상. 실제 거래소 240분봉 경계 사용. 15일 진입 검증, 다음 시간봉 시가 진입 후 1·3일 평가. 누락 제외.</p><div id="oldTrendRules"><p>선택 시간봉: 눌림 전 20개 봉 상승폭 10% 이상 · 연속 음봉 2~4개 · 종가 눌림 3~10% (저가 최대 10%) · 눌림 평균 거래량 감소.</p><small>4시간봉은 KST 00/04/08/12/16/20시, 1시간봉으로 합성(누락 봉 제외). BUY: 완료 1시간봉 양봉·직전 고가 돌파·직전 3시간 평균 거래량 1.2배 이상, 이전 고점 미초과. WATCH는 반등 미확인. 일봉 추세 필터는 아직 적용하지 않음. 15일 백테스트는 같은 조건의 BUY만 다음 시간봉 시가 진입, 1·3일 수익률, 동일 코인 72시간 중복 제외. 현재 상장종목 기준, 누락은 제외. 실제 주문 없음.</small></div><p>시간봉 <select id="trendTf"><option value="T430" selected>4시간봉 · 30개 중 20개 이상 음봉 (신규)</option><option value="T415">4시간봉 · 20개 중 15개 이상 음봉</option><option value="T4">4시간봉 (기존)</option><option value="T1">1시간봉</option></select><button id="back">15일 백테스트</button></p></div><p><label id="presetLabel" for="preset">검색 조건</label><select id="preset"><option value="A">A · 30% / 15일</option><option value="B">B · 35% / 14일</option><option value="C">C · 40% / 15일</option><option value="D" selected>D · 35% / 13일 (완화 테스트)</option></select><button id="go">전체 코인 검색 시작</button></p><p id="applied" class="muted">조건을 선택하고 검색해 주세요.</p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section><h2>모의매매 계좌</h2><small>가상 원금 100만원, 1회 투자금 변경 가능. 현재가 조회 후 전량 가상 체결(편도 0.04%). 직접 매수/매도, 7일 경과 표시만 제공. 실제 주문 없음. 기록은 이 브라우저에만 저장, 기기·브라우저 변경/데이터 삭제 시 유실될 수 있음. CSV 백업 권장.</small><p>1회 투자금(원) <input id="paperAmount" type="number" min="5000" step="1000" value="100000" style="width:105px"><button id="refreshPaper">보유 가격 갱신</button><button id="paperCSV">모의매매 CSV</button></p><p id="paperSummary"></p><p id="paperWin"></p><p id="paperMsg" class="muted"></p><h3>보유 종목</h3><div class="cards" id="paperOpen"></div><details><summary>청산 기록 (최근 100건)</summary><div class="cards" id="paperClosed"></div></details></section><section id="out" hidden><h2 id="headline"></h2><p id="audit" class="muted"></p><p id="notice" class="muted"></p><a id="featuresCSV" href="/csv/features?mode=live14">결과 CSV</a><a id="summaryCSV" href="/csv/summary?mode=live14">요약 CSV</a><a id="coverageCSV" href="/csv/coverage?mode=live14">수집 상태 CSV</a><a id="errorsCSV" href="/csv/errors?mode=live14">오류 CSV</a><p><input id="query" placeholder="코인 검색" style="width:105px"><select id="sort"><option value="market">종목명 순</option><option value="volume">거래량 증가율 순</option><option value="depth">고점 대비 하락폭 순</option><option value="gap">당일 시가 대비 변동률 순</option></select></p><p id="count"></p><div class="cards" id="cards"></div><button id="more">더 보기</button></section><script>
const $=id=>document.getElementById(id);let result=null,loading=false,limit=5,loadedScan=null;const names={A:"A · 30% / 15일",B:"B · 35% / 14일",C:"C · 40% / 15일",D:"D · 35% / 13일",T4:"상승 눌림 · 4시간봉",T1:"상승 눌림 · 1시간봉",T415:"4시간봉 음봉 15/20",T430:"4시간봉 음봉 20/30"};
function num(x){return typeof x==='number'?x.toLocaleString('ko-KR',{maximumSignificantDigits:8}):'—'}
function pct(x){return typeof x==='number'?(x>0?'+':'')+x.toFixed(2)+'%':'—'}
function node(tag,txt,cls){let e=document.createElement(tag);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
function line(card,k,v,cls){let r=node('div',null,'line');r.append(node('span',k,'muted'),node('strong',v,cls));card.append(r)}
function render(){if(!result)return;if(result.summary[0].strategy==="trend"){renderTrend();return;}let q=$('query').value.trim().toUpperCase(),sort=$('sort').value;let a=result.rank.filter(x=>x.market.includes(q));let key={volume:'volume_increase_pct',depth:'peak_to_last_pct',gap:'change_from_open_pct'}[sort];if(key)a.sort((x,y)=>sort==='depth'?(x[key]??Infinity)-(y[key]??Infinity):(y[key]??-Infinity)-(x[key]??-Infinity));else a.sort((x,y)=>x.market.localeCompare(y.market));$('count').textContent=a.length+'종목 · '+Math.min(limit,a.length)+'개 표시';a.sort((x,y)=>Number(y.status==="BUY")-Number(x.status==="BUY"));let cards=a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+(x.status==='BUY'?'BUY 후보':'WATCH'),x.status==='BUY'?'green':'muted'));line(c,'적용 검색식',names[x.preset]||x.preset);line(c,'검색 시 참고가',num(x.reference_price)+'원');line(c,'당일 시가',num(x.day_open)+'원');line(c,'시가 대비',pct(x.change_from_open_pct),x.change_from_open_pct>=0?'green':'red');line(c,'당일 최고 상승률',pct(x.day_high_pct));line(c,'20일+오늘 최대 일중 급등',pct(x.lookback_max_spike_pct));line(c,'최대 급등 날짜',x.lookback_max_spike_date||'—');line(c,'20일 실제 하락일',x.down_days+'/20일');line(c,'20일 종가 변화',pct(x.trend_net_pct),'red');line(c,'고점 대비 전일 종가',pct(x.peak_to_last_pct),'red');line(c,'거래량 배수',num(x.vol_ratio_d3)+' → '+num(x.vol_ratio_d1)+'배','green');line(c,'거래량 배수 증가율',pct(x.volume_increase_pct));line(c,'참고가 기준 본전',num(x.breakeven_price)+'원');line(c,'신호 확정(KST)',x.signal_time);line(c,'가격 조회(KST)',x.observed_at);line(c,'1시간봉 5이평',num(x.hour_ma5));line(c,'1시간봉 확인',x.hour_valid?x.hour_signal_time:'데이터 부족');line(c,'7일째 일봉 날짜',x.planned_last_day);let b=node('button',paperStats().open.some(t=>t.market===x.market)?'모의 보유 중':x.status==='WATCH'?'WATCH 모의매수':'BUY 모의매수');b.disabled=!['BUY','WATCH'].includes(x.status)||paperBusy||!storageOK||paperStats().open.some(t=>t.market===x.market);b.onclick=()=>paperBuy(x);c.append(b);return c});$('cards').replaceChildren(...cards);$('more').hidden=a.length<=limit}
function show(){let x=result.summary[0];if(x.strategy==="trend"){showTrend(x);return;}$('applied').textContent='결과 적용 조건: '+names[x.preset];$('headline').textContent='BUY '+x.buys+'개 · WATCH '+x.watches+'개 · 20일+오늘 급등 제외 '+x.spike_excluded+'개';$('audit').textContent='신호 확정 '+x.signal_time+' KST · 검색 완료 '+x.finished_at+' KST · 분석 '+x.valid+'/'+x.universe+' · 이력 부족 '+x.history_missing+' · 현재가 없음 '+x.no_quote+' · 급등 이력 미확인 '+x.spike_history_missing;$('notice').textContent='수수료 편도 0.04% 적용. 본전가는 참고가 기준 이론값(호가단위·슬리피지 미반영). 직접 매매 전 현재가와 보유 여부 확인.';limit=5;render()}

let resultMode=null;
function syncMode(){let trend=$('strategy').value==='trend';let tf=$('trendTf').value;let direct=['T415','T430'].includes(tf);$('newRules').hidden=!direct;$('oldTrendRules').hidden=direct;$('directRuleText').textContent=tf==='T430'?'신규 20/30: 완료된 240분봉 30개 중 종가 < 시가인 봉 20개 이상.':'15/20: 완료된 240분봉 20개 중 종가 < 시가인 봉 15개 이상.';$('reverseRules').hidden=trend;$('trendRules').hidden=!trend;$('preset').hidden=trend;$('presetLabel').hidden=trend;}
function clearResult(){result=null;loadedScan=null;resultMode=null;$('out').hidden=true;}
$('strategy').onchange=()=>{syncMode();clearResult();$('applied').textContent='전략을 선택하고 새로 검색해 주세요.'};
$('trendTf').onchange=()=>{syncMode();clearResult();$('applied').textContent='시간봉 변경: 다시 검색해 주세요.'};
$('preset').onchange=()=>{clearResult();$('applied').textContent='조건 변경: 다시 검색해 주세요.'};
$('query').oninput=$('sort').onchange=()=>{limit=5;render()};$('more').onclick=()=>{limit+=5;render()};
async function begin20(back){try{$('go').disabled=true;$('back').disabled=true;$('strategy').disabled=true;$('preset').disabled=true;$('trendTf').disabled=true;let trend=$('strategy').value==='trend';let url=trend?'/trend20/start?preset='+encodeURIComponent($('trendTf').value)+'&back='+(back?'1':'0'):'/live14/start?preset='+encodeURIComponent($('preset').value);let r=await fetch(url,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 검색 실행 중');clearResult();await poll()}catch(e){$('phase').textContent=e.message;for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=false}}
$('go').onclick=()=>begin20(false);$('back').onclick=()=>begin20(true);
function showTrend(x){
 $('applied').textContent='결과 적용 조건: '+names[x.preset];
 $('headline').textContent=resultMode==='trendback20'?'15일 과거 BUY 진입 '+x.matches+'건 · 3일 평균 '+pct(x.day3_mean_pct):'BUY '+x.buys+'개 · WATCH '+x.watches+'개';
 $('audit').textContent='분석 '+x.valid+'/'+x.universe+' · 이력 부족 종목 '+x.history_missing+' · 검색 완료 '+x.finished_at;
 $('notice').textContent=resultMode==='trendback20'?'1일 평균 '+pct(x.day1_mean_pct)+' · 3일 최고 순수익 5% 도달률 '+pct(x.day3_hit5_pct)+' · 평가 누락 '+x.outcome_missing+'건 · 누락 시간 '+x.missing_hours+' · 가격 범위 제외 '+x.price_skipped+' · 매수 후 72시간 중복 제외':'상승 눌림 실험 조건 · 편도 0.04% · BUY/WATCH 모의매수만 가능';
 limit=5;render();
}
function renderTrend(){
 let q=$('query').value.trim().toUpperCase();let a=result.rank.filter(x=>x.market.includes(q));
 if(resultMode!=='trendback20'){let sort=$('sort').value;if(sort==='volume')a.sort((x,y)=>y.confirm_volume_ratio-x.confirm_volume_ratio);else if(sort==='depth')a.sort((x,y)=>y.pullback_pct-x.pullback_pct);else if(sort==='market')a.sort((x,y)=>x.market.localeCompare(y.market));a.sort((x,y)=>Number(y.status==='BUY')-Number(x.status==='BUY'))}
 $('count').textContent=a.length+'건 · '+Math.min(limit,a.length)+'개 표시';$('more').hidden=a.length<=limit;
 $('cards').replaceChildren(...a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+x.status,x.status==='BUY'?'green':'muted'));line(c,'전략',names[x.preset]);if(['T415','T430'].includes(x.preset)){line(c,'음봉 수',x.down_bars+'/'+x.total_bars+'개');line(c,x.total_bars+'봉 변화',pct(x.window_change_pct));line(c,'고점 대비 변화',pct(-x.pullback_pct));line(c,'마지막 4h 거래량 / 이전 '+(x.total_bars-1)+'봉',num(x.last_4h_volume_ratio)+'배');line(c,'4h 조건 확정',x.setup_end);}else{line(c,'이전 20봉 상승',pct(x.prior_gain_pct));line(c,'연속 음봉',x.pullback_bars+'개');line(c,'눌림폭',pct(-x.pullback_pct));line(c,'눌림 거래량 / 이전 평균',num(x.pullback_volume_ratio)+'배');}line(c,'반등 시간봉 거래량',num(x.confirm_volume_ratio)+'배');line(c,'신호 확정(KST)',x.signal_time);
 if(resultMode==='trendback20'){line(c,'진입가',num(x.buy_price)+'원');line(c,'1일 종료 / 최고',pct(x.day1_close_pct)+' / '+pct(x.day1_peak_pct));line(c,'3일 종료 / 최고',pct(x.day3_close_pct)+' / '+pct(x.day3_peak_pct));line(c,'3일 최저',pct(x.day3_low_pct));}
 else{line(c,'검색 참고가',num(x.reference_price)+'원');line(c,'이전 고점',num(x.prior_peak)+'원');line(c,'눌림 저점',num(x.pullback_low)+'원');let held=paperStats().open.some(t=>t.market===x.market),b=node('button',held?'모의 보유 중':x.status+' 모의매수');b.disabled=held||paperBusy||!storageOK;b.onclick=()=>paperBuy(x);c.append(b)}return c}));
}
async function poll(){try{
 let s=await(await fetch('/status',{cache:'no-store'})).json();let running=s.status==='running';
 for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=running;
 if(!['live14','trend20','trendback20'].includes(s.mode)){$('phase').textContent=running?'다른 연구 실행 중':'대기';clearResult();return}
 $('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');
 if(running){clearResult();$('applied').textContent='검색 중: '+names[s.preset];return}
 if(s.status==='done'&&loadedScan!==s.scan_id&&!loading){
  // Do not overwrite a user's deliberate strategy selection after a previous completed scan.
  if((s.mode==='live14')!==($('strategy').value==='reverse'))return;
  if(s.mode==='live14'&&$('preset').value!==s.preset)return;
  if(s.mode!=='live14'&&$('trendTf').value!==s.preset)return;
  loading=true;try{let d=await(await fetch('/results',{cache:'no-store'})).json();if(!d.summary[0]||d.summary[0].scan_id!==s.scan_id)return;result=d;loadedScan=s.scan_id;resultMode=s.mode;if(s.mode==='live14')$('preset').value=s.preset;
  for(let k of ['features','summary','coverage','errors'])$(k+'CSV').href='/csv/'+k+'?mode='+s.mode;
  $('out').hidden=false;show()}finally{loading=false}
 }
 }catch(e){$('phase').textContent='연결 확인: '+e.message}}
syncMode();

const FEE=.0004,STORE='reverse-swing-paper-v1',INITIAL=1000000;
let account={trades:[]},storageOK=true,paperBusy=false,quotes={};
try{let raw=localStorage.getItem(STORE);if(raw){let a=JSON.parse(raw);if(!Array.isArray(a.trades))throw Error('invalid data');account=a}}catch(e){storageOK=false}
function savePaper(next){localStorage.setItem(STORE,JSON.stringify(next));account=next}
function paperStats(){let closed=account.trades.filter(x=>x.closedAt),open=account.trades.filter(x=>!x.closedAt),real=closed.reduce((s,x)=>s+x.pnl,0),spent=open.reduce((s,x)=>s+x.amount,0);return {closed,open,real,cash:INITIAL+real-spent,wins:closed.filter(x=>x.pnl>0).length,losses:closed.filter(x=>x.pnl<0).length}}
function paperMsg(s){$('paperMsg').textContent=s}
function paperTime(t){return new Date(t).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})}
async function quote(markets){let all={};for(let i=0;i<markets.length;i+=40){let r=await fetch('/paper/quotes?markets='+encodeURIComponent(markets.slice(i,i+40).join(',')),{cache:'no-store'});if(!r.ok)throw Error('현재가 조회 실패');let d=await r.json();Object.assign(all,d.quotes)}return all}
async function paperBuy(x){
 if(paperBusy||!storageOK||!result||!['BUY','WATCH'].includes(x.status))return;
 let a=Number($('paperAmount').value),s=paperStats();
 if(!Number.isFinite(a)||a<5000||a>s.cash){paperMsg('가상 잔액 내에서 5,000원 이상 입력해 주세요.');return}
 if(s.open.some(t=>t.market===x.market)){paperMsg('이미 모의 보유 중인 종목입니다.');return}
 if(s.closed.some(t=>t.market===x.market&&t.signalTime===x.signal_time)){paperMsg('같은 신호에서 이미 매매한 종목입니다.');return}
 paperBusy=true;renderPaper();paperMsg('새로운 현재가 조회 중...');
 try{let r=await fetch((x.strategy==='trend'?'/trend20/entry':'/paper/entry')+'?market='+encodeURIComponent(x.market)+'&signal='+encodeURIComponent(x.signal_time)+'&hour='+encodeURIComponent(x.hour_signal_time)+'&preset='+encodeURIComponent(x.preset)+'&scan_id='+encodeURIComponent(result.summary[0].scan_id)+'&entry_status='+encodeURIComponent(x.status),{cache:'no-store'});if(!r.ok)throw Error('신호 만료, 진입 범위 제외 또는 가격 조회 실패. 다시 검색해 주세요.');let q=await r.json();if(q.entry_status!==x.status)throw Error('검색 판정 변경. 다시 검색해 주세요.');let p=q.price;if(!Number.isFinite(p)||p<=0)throw Error('유효한 현재가 없음');
 let t={id:Date.now()+'-'+x.market,market:x.market,signalTime:x.signal_time,boughtAt:q.observed_ms,amount:a,buy:p,qty:a/(1+FEE)/p,deadline:q.observed_ms+7*86400000,signalDeadline:q.signal_deadline_ms,preset:q.preset,minDrawdownPct:q.min_drawdown_pct,minDownDays:q.min_down_days,entryStatus:q.entry_status,strategy:x.strategy||'reverse',timeframe:x.strategy==='trend'?x.preset:null};savePaper({trades:[...account.trades,t]});quotes[x.market]=q;paperMsg(x.market+' '+q.entry_status+' 모의매수 완료 (시장 참고가 가정).');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function paperSell(t){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('매도 참고가 조회 중...');
 try{let q=(await quote([t.market]))[t.market];if(!q)throw Error('가격 확인 불가');let proceeds=t.qty*q.price*(1-FEE),pnl=proceeds-t.amount;let closed={...t,closedAt:q.observed_ms,sell:q.price,proceeds,pnl,netPct:100*pnl/t.amount};savePaper({trades:account.trades.map(x=>x.id===t.id?closed:x)});quotes[t.market]=q;paperMsg(t.market+' 모의매도 완료. 순손익 '+num(pnl)+'원');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function refreshPaper(){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('보유 종목 가격 조회 중...');
 try{let m=paperStats().open.map(x=>x.market);if(m.length)quotes=await quote(m);paperMsg('가격 갱신 완료. 가격 확인 불가 종목은 — 표시.')}catch(e){quotes={};paperMsg(e.message)}finally{paperBusy=false;renderPaper()}
}
function renderPaper(){
 let s=paperStats();$('paperSummary').textContent='가상 원금 1,000,000원 · 잔액 '+num(s.cash)+'원 · 실현손익 '+num(s.real)+'원';$('paperWin').textContent='보유 '+s.open.length+' · 청산 '+s.closed.length+' · 수익 '+s.wins+' · 손실 '+s.losses+' · 승률 '+(s.closed.length?(100*s.wins/s.closed.length).toFixed(2)+'%':'—');
 $('refreshPaper').disabled=paperBusy||!storageOK;
 let cards=s.open.map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 모의 보유'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매수가',num(t.buy)+'원');line(c,'투자금(수수료 포함)',num(t.amount)+'원');let q=quotes[t.market];let val=q?t.qty*q.price*(1-FEE)-t.amount:null;line(c,'조회 가격',q?num(q.price)+'원':'—');line(c,'가격 조회',q?paperTime(q.observed_ms):'—');line(c,'평가 순손익',val===null?'—':num(val)+'원 / '+pct(100*val/t.amount),val===null?'muted':val>=0?'green':'red');line(c,'7일 보유 만료',paperTime(t.deadline),Date.now()>=t.deadline?'red':'muted');if(Date.now()>=t.deadline)c.append(node('p','7일 경과: 직접 모의매도 필요','red'));let b=node('button','모의매도');b.disabled=paperBusy||!storageOK;b.onclick=()=>paperSell(t);c.append(b);return c});
 $('paperOpen').replaceChildren(...cards);if(!cards.length)$('paperOpen').textContent='보유 종목 없음';
 let hist=s.closed.slice().reverse().slice(0,100).map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 청산 완료'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 / 매도',num(t.buy)+' / '+num(t.sell));line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매도 시각',paperTime(t.closedAt));line(c,'순손익',num(t.pnl)+'원 / '+pct(t.netPct),t.pnl>=0?'green':'red');return c});$('paperClosed').replaceChildren(...hist);if(!hist.length)$('paperClosed').textContent='청산 기록 없음';if(!storageOK)paperMsg('브라우저 저장소 사용 불가 또는 기록 손상. 모의매매를 중단합니다.');
}
$('refreshPaper').onclick=refreshPaper;
$('paperCSV').onclick=()=>{let keys=['market','strategy','timeframe','preset','entryStatus','minDrawdownPct','minDownDays','signalTime','boughtAt','amount','buy','qty','deadline','closedAt','sell','proceeds','pnl','netPct'];let esc=v=>'"'+String(v??'').replaceAll('"','""')+'"';let rows=[keys.join(','),...account.trades.map(x=>keys.map(k=>esc(x[k])).join(','))];let url=URL.createObjectURL(new Blob(['\ufeff'+rows.join('\r\n')],{type:'text/csv;charset=utf-8'}));let a=document.createElement('a');a.href=url;a.download='BITHUMB_SWING_V2_2_TEST_PAPER_'+new Date().toISOString().slice(0,10)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
renderPaper();

poll();setInterval(poll,2000);
</script></html>"""
HTML23=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BITHUMB SWING V2.3 TEST</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1000px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:10px;margin:4px;border-radius:7px;text-decoration:none}input{background:#10151d}button:disabled{opacity:.5}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:10px}.coin{background:#111923;border:1px solid #3b4d60;border-radius:12px;padding:12px}.coin h3{margin:0 0 8px}.line{display:flex;justify-content:space-between;gap:10px;margin:6px 0}.green{color:#72ddb0}.red{color:#ff9b9b}.muted{color:#a9bac9}h1{font-size:23px;margin-bottom:4px}</style><h1>BITHUMB SWING V2.3 TEST</h1><a href="/live143">기존 V1.4.3 보존판</a><section><h2>매수 후보 검색</h2><p><label>전략 <select id="strategy"><option value="reverse">저점 반등형 (기존)</option><option value="trend">상승 눌림형 (실험)</option></select></label></p><div id="reverseRules"><p>20일 하락 추세 + 선택한 하락폭·하락일 수 + 전일 거래량 2배 이상 + D-3 대비 2배 이상 · RSI 제외</p><small>최근 20일과 오늘의 일별 고가가 시가 또는 전일 종가 대비 한 번이라도 30% 이상 상승하면 제외. 과거 급등 이력 부족 시 제외. 강화 일봉 조건 충족 WATCH. 가장 최근 완료 1시간봉의 SMA5 상향 돌파만 BUY 후보. 미완료 봉 제외. BUY/WATCH 모의매수 모두 검색된 1시간 동안 유효. WATCH는 5이평 상향 돌파 미확인 상태의 비교 실험이며 BUY로 변경하지 않음. 검증 전 실전 매수 권장 아님. 검색 시점의 가격은 실제 체결가가 아니며 당일 시가 진입 백테스트와 다를 수 있음. 최고가 매도 가정 결과는 실제 승률이 아님. 자동 주문 없음. 별도 매도·손절 신호 미검증. 기존 보유 종목은 직접 제외.</small></div><div id="trendRules" hidden><p id="newRules"><span id="directRuleText">신규 20/30: 완료된 240분봉 30개 중 종가 &lt; 시가인 봉 20개 이상.</span> 연속 불필요. 상승폭·눌림폭·4시간 거래량 필터 없음. BUY는 기존 1시간봉 양봉·직전 고가 돌파·직전 3시간 평균 거래량 1.2배 이상. 실제 거래소 240분봉 경계 사용. 15일 진입 검증, 다음 시간봉 시가 진입 후 1·3일 평가. 누락 제외.</p><div id="oldTrendRules"><p>선택 시간봉: 눌림 전 20개 봉 상승폭 10% 이상 · 연속 음봉 2~4개 · 종가 눌림 3~10% (저가 최대 10%) · 눌림 평균 거래량 감소.</p><small>4시간봉은 KST 00/04/08/12/16/20시, 1시간봉으로 합성(누락 봉 제외). BUY: 완료 1시간봉 양봉·직전 고가 돌파·직전 3시간 평균 거래량 1.2배 이상, 이전 고점 미초과. WATCH는 반등 미확인. 일봉 추세 필터는 아직 적용하지 않음. 15일 백테스트는 같은 조건의 BUY만 다음 시간봉 시가 진입, 1·3일 수익률, 동일 코인 72시간 중복 제외. 현재 상장종목 기준, 누락은 제외. 실제 주문 없음.</small></div><p>시간봉 <select id="trendTf"><option value="T42ALL" selected>4시간봉 · 42개 중 30~36개 동시 비교 (백테스트)</option><option value="T430">4시간봉 · 30개 중 20개 이상 음봉 (신규)</option><option value="T415">4시간봉 · 20개 중 15개 이상 음봉</option><option value="T4">4시간봉 (기존)</option><option value="T1">1시간봉</option></select><button id="back">15일 백테스트</button></p></div><p><label id="presetLabel" for="preset">검색 조건</label><select id="preset"><option value="A">A · 30% / 15일</option><option value="B">B · 35% / 14일</option><option value="C">C · 40% / 15일</option><option value="D" selected>D · 35% / 13일 (완화 테스트)</option></select><button id="go">전체 코인 검색 시작</button></p><p id="applied" class="muted">조건을 선택하고 검색해 주세요.</p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section><h2>모의매매 계좌</h2><small>가상 원금 100만원, 1회 투자금 변경 가능. 현재가 조회 후 전량 가상 체결(편도 0.04%). 직접 매수/매도, 7일 경과 표시만 제공. 실제 주문 없음. 기록은 이 브라우저에만 저장, 기기·브라우저 변경/데이터 삭제 시 유실될 수 있음. CSV 백업 권장.</small><p>1회 투자금(원) <input id="paperAmount" type="number" min="5000" step="1000" value="100000" style="width:105px"><button id="refreshPaper">보유 가격 갱신</button><button id="paperCSV">모의매매 CSV</button></p><p id="paperSummary"></p><p id="paperWin"></p><p id="paperMsg" class="muted"></p><h3>보유 종목</h3><div class="cards" id="paperOpen"></div><details><summary>청산 기록 (최근 100건)</summary><div class="cards" id="paperClosed"></div></details></section><section id="out" hidden><h2 id="headline"></h2><p id="audit" class="muted"></p><p id="notice" class="muted"></p><div id="compare23" hidden></div><a id="featuresCSV" href="/csv/features?mode=live14">결과 CSV</a><a id="summaryCSV" href="/csv/summary?mode=live14">요약 CSV</a><a id="coverageCSV" href="/csv/coverage?mode=live14">수집 상태 CSV</a><a id="errorsCSV" href="/csv/errors?mode=live14">오류 CSV</a><p><input id="query" placeholder="코인 검색" style="width:105px"><select id="sort"><option value="market">종목명 순</option><option value="volume">거래량 증가율 순</option><option value="depth">고점 대비 하락폭 순</option><option value="gap">당일 시가 대비 변동률 순</option></select></p><p id="count"></p><div class="cards" id="cards"></div><button id="more">더 보기</button></section><script>
const $=id=>document.getElementById(id);let result=null,loading=false,limit=5,loadedScan=null;const names={A:"A · 30% / 15일",B:"B · 35% / 14일",C:"C · 40% / 15일",D:"D · 35% / 13일",T4:"상승 눌림 · 4시간봉",T1:"상승 눌림 · 1시간봉",T415:"4시간봉 음봉 15/20",T430:"4시간봉 음봉 20/30",T42ALL:"42봉 · 음봉 30~36개 비교"};
function num(x){return typeof x==='number'?x.toLocaleString('ko-KR',{maximumSignificantDigits:8}):'—'}
function pct(x){return typeof x==='number'?(x>0?'+':'')+x.toFixed(2)+'%':'—'}
function node(tag,txt,cls){let e=document.createElement(tag);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
function line(card,k,v,cls){let r=node('div',null,'line');r.append(node('span',k,'muted'),node('strong',v,cls));card.append(r)}
function render(){if(!result)return;if(result.summary[0].strategy==="trend"){renderTrend();return;}let q=$('query').value.trim().toUpperCase(),sort=$('sort').value;let a=result.rank.filter(x=>x.market.includes(q));let key={volume:'volume_increase_pct',depth:'peak_to_last_pct',gap:'change_from_open_pct'}[sort];if(key)a.sort((x,y)=>sort==='depth'?(x[key]??Infinity)-(y[key]??Infinity):(y[key]??-Infinity)-(x[key]??-Infinity));else a.sort((x,y)=>x.market.localeCompare(y.market));$('count').textContent=a.length+'종목 · '+Math.min(limit,a.length)+'개 표시';a.sort((x,y)=>Number(y.status==="BUY")-Number(x.status==="BUY"));let cards=a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+(x.status==='BUY'?'BUY 후보':'WATCH'),x.status==='BUY'?'green':'muted'));line(c,'적용 검색식',names[x.preset]||x.preset);line(c,'검색 시 참고가',num(x.reference_price)+'원');line(c,'당일 시가',num(x.day_open)+'원');line(c,'시가 대비',pct(x.change_from_open_pct),x.change_from_open_pct>=0?'green':'red');line(c,'당일 최고 상승률',pct(x.day_high_pct));line(c,'20일+오늘 최대 일중 급등',pct(x.lookback_max_spike_pct));line(c,'최대 급등 날짜',x.lookback_max_spike_date||'—');line(c,'20일 실제 하락일',x.down_days+'/20일');line(c,'20일 종가 변화',pct(x.trend_net_pct),'red');line(c,'고점 대비 전일 종가',pct(x.peak_to_last_pct),'red');line(c,'거래량 배수',num(x.vol_ratio_d3)+' → '+num(x.vol_ratio_d1)+'배','green');line(c,'거래량 배수 증가율',pct(x.volume_increase_pct));line(c,'참고가 기준 본전',num(x.breakeven_price)+'원');line(c,'신호 확정(KST)',x.signal_time);line(c,'가격 조회(KST)',x.observed_at);line(c,'1시간봉 5이평',num(x.hour_ma5));line(c,'1시간봉 확인',x.hour_valid?x.hour_signal_time:'데이터 부족');line(c,'7일째 일봉 날짜',x.planned_last_day);let b=node('button',paperStats().open.some(t=>t.market===x.market)?'모의 보유 중':x.status==='WATCH'?'WATCH 모의매수':'BUY 모의매수');b.disabled=!['BUY','WATCH'].includes(x.status)||paperBusy||!storageOK||paperStats().open.some(t=>t.market===x.market);b.onclick=()=>paperBuy(x);c.append(b);return c});$('cards').replaceChildren(...cards);$('more').hidden=a.length<=limit}
function show(){let x=result.summary[0];$('compare23').hidden=true;if(x.strategy==="trend"){showTrend(x);return;}$('applied').textContent='결과 적용 조건: '+names[x.preset];$('headline').textContent='BUY '+x.buys+'개 · WATCH '+x.watches+'개 · 20일+오늘 급등 제외 '+x.spike_excluded+'개';$('audit').textContent='신호 확정 '+x.signal_time+' KST · 검색 완료 '+x.finished_at+' KST · 분석 '+x.valid+'/'+x.universe+' · 이력 부족 '+x.history_missing+' · 현재가 없음 '+x.no_quote+' · 급등 이력 미확인 '+x.spike_history_missing;$('notice').textContent='수수료 편도 0.04% 적용. 본전가는 참고가 기준 이론값(호가단위·슬리피지 미반영). 직접 매매 전 현재가와 보유 여부 확인.';limit=5;render()}

let resultMode=null;
function syncMode(){let trend=$('strategy').value==='trend';let tf=$('trendTf').value;let direct=['T415','T430','T42ALL'].includes(tf);$('newRules').hidden=!direct;$('oldTrendRules').hidden=direct;$('directRuleText').textContent=tf==='T42ALL'?'42개 완료 4시간봉 중 음봉 최소 30,31,32,33,34,35,36개를 각각 비교 (백테스트 전용).':tf==='T430'?'신규 20/30: 완료된 240분봉 30개 중 종가 < 시가인 봉 20개 이상.':'15/20: 완료된 240분봉 20개 중 종가 < 시가인 봉 15개 이상.';$('reverseRules').hidden=trend;$('trendRules').hidden=!trend;$('preset').hidden=trend;$('presetLabel').hidden=trend;}
function clearResult(){result=null;loadedScan=null;resultMode=null;$('out').hidden=true;}
$('strategy').onchange=()=>{syncMode();clearResult();$('applied').textContent='전략을 선택하고 새로 검색해 주세요.'};
$('trendTf').onchange=()=>{syncMode();clearResult();$('applied').textContent='시간봉 변경: 다시 검색해 주세요.'};
$('preset').onchange=()=>{clearResult();$('applied').textContent='조건 변경: 다시 검색해 주세요.'};
$('query').oninput=$('sort').onchange=()=>{limit=5;render()};$('more').onclick=()=>{limit+=5;render()};
async function begin20(back){try{$('go').disabled=true;$('back').disabled=true;$('strategy').disabled=true;$('preset').disabled=true;$('trendTf').disabled=true;let trend=$('strategy').value==='trend';if(trend&&$('trendTf').value==='T42ALL')back=true;let url=trend?'/trend20/start?preset='+encodeURIComponent($('trendTf').value)+'&back='+(back?'1':'0'):'/live14/start?preset='+encodeURIComponent($('preset').value);let r=await fetch(url,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 검색 실행 중');clearResult();await poll()}catch(e){$('phase').textContent=e.message;for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=false}}
$('go').onclick=()=>begin20(false);$('back').onclick=()=>begin20(true);
function showTrend(x){
 if(x.preset==='T42ALL'){show23(x);return}
 $('applied').textContent='결과 적용 조건: '+names[x.preset];
 $('headline').textContent=resultMode==='trendback20'?'15일 과거 BUY 진입 '+x.matches+'건 · 3일 평균 '+pct(x.day3_mean_pct):'BUY '+x.buys+'개 · WATCH '+x.watches+'개';
 $('audit').textContent='분석 '+x.valid+'/'+x.universe+' · 이력 부족 종목 '+x.history_missing+' · 검색 완료 '+x.finished_at;
 $('notice').textContent=resultMode==='trendback20'?'1일 평균 '+pct(x.day1_mean_pct)+' · 3일 최고 순수익 5% 도달률 '+pct(x.day3_hit5_pct)+' · 평가 누락 '+x.outcome_missing+'건 · 누락 시간 '+x.missing_hours+' · 가격 범위 제외 '+x.price_skipped+' · 매수 후 72시간 중복 제외':'상승 눌림 실험 조건 · 편도 0.04% · BUY/WATCH 모의매수만 가능';
 limit=5;render();
}
function show23(x){
 $('applied').textContent=names[x.preset];$('headline').textContent='42봉 · 7개 기준 동시 비교';
 $('audit').textContent=x.first_entry+' ~ '+x.last_entry_exclusive+' (종료 시각 미포함) · 전체 '+x.universe+' · 수집 성공 '+x.market_success;
 $('notice').textContent='각 기준별 72시간 중복 제외 · 1/3일 종료 수익 및 최고가 기회 (실현 수익 아님) · 동일 표본이 여러 그룹에 포함 가능 · 수수료 왕복 0.08%';
 let panel=$('compare23');panel.hidden=false;panel.replaceChildren(...result.summary.map(r=>{let c=node('article',null,'coin');c.append(node('h3','최소 '+r.minimum_down_bars+'/42 · '+r.matches+'건'));line(c,'1일 / 3일 종료 평균',pct(r.day1_mean_pct)+' / '+pct(r.day3_mean_pct));line(c,'3일 최고 평균',pct(r.day3_peak_mean_pct));line(c,'5% / 10% 도달률',pct(r.day3_hit5_pct)+' / '+pct(r.day3_hit10_pct));line(c,'-10% 도달률',pct(r.day3_loss10_pct));line(c,'판정 가능 / 평가 누락',pct(r.valid_pct)+' / '+r.outcome_missing+'건');return c}));limit=5;render();
}
function renderTrend(){
 let q=$('query').value.trim().toUpperCase();let a=result.rank.filter(x=>x.market.includes(q));
 if(resultMode!=='trendback20'){let sort=$('sort').value;if(sort==='volume')a.sort((x,y)=>y.confirm_volume_ratio-x.confirm_volume_ratio);else if(sort==='depth')a.sort((x,y)=>y.pullback_pct-x.pullback_pct);else if(sort==='market')a.sort((x,y)=>x.market.localeCompare(y.market));a.sort((x,y)=>Number(y.status==='BUY')-Number(x.status==='BUY'))}
 $('count').textContent=a.length+'건 · '+Math.min(limit,a.length)+'개 표시';$('more').hidden=a.length<=limit;
 $('cards').replaceChildren(...a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+x.status,x.status==='BUY'?'green':'muted'));line(c,'전략',names[x.preset]);if(['T415','T430','T42ALL'].includes(x.preset)){if(x.preset==='T42ALL')line(c,'비교군 최소 음봉',x.minimum_down_bars+'/42');line(c,'음봉 수',x.down_bars+'/'+x.total_bars+'개');line(c,x.total_bars+'봉 변화',pct(x.window_change_pct));line(c,'고점 대비 변화',pct(-x.pullback_pct));line(c,'마지막 4h 거래량 / 이전 '+(x.total_bars-1)+'봉',num(x.last_4h_volume_ratio)+'배');line(c,'4h 조건 확정',x.setup_end);}else{line(c,'이전 20봉 상승',pct(x.prior_gain_pct));line(c,'연속 음봉',x.pullback_bars+'개');line(c,'눌림폭',pct(-x.pullback_pct));line(c,'눌림 거래량 / 이전 평균',num(x.pullback_volume_ratio)+'배');}line(c,'반등 시간봉 거래량',num(x.confirm_volume_ratio)+'배');line(c,'신호 확정(KST)',x.signal_time);
 if(resultMode==='trendback20'){line(c,'진입가',num(x.buy_price)+'원');line(c,'1일 종료 / 최고',pct(x.day1_close_pct)+' / '+pct(x.day1_peak_pct));line(c,'3일 종료 / 최고',pct(x.day3_close_pct)+' / '+pct(x.day3_peak_pct));line(c,'3일 최저',pct(x.day3_low_pct));}
 else{line(c,'검색 참고가',num(x.reference_price)+'원');line(c,'이전 고점',num(x.prior_peak)+'원');line(c,'눌림 저점',num(x.pullback_low)+'원');let held=paperStats().open.some(t=>t.market===x.market),b=node('button',held?'모의 보유 중':x.status+' 모의매수');b.disabled=held||paperBusy||!storageOK;b.onclick=()=>paperBuy(x);c.append(b)}return c}));
}
async function poll(){try{
 let s=await(await fetch('/status',{cache:'no-store'})).json();let running=s.status==='running';
 for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=running;
 if(!['live14','trend20','trendback20'].includes(s.mode)){$('phase').textContent=running?'다른 연구 실행 중':'대기';clearResult();return}
 $('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');
 if(running){clearResult();$('applied').textContent='검색 중: '+names[s.preset];return}
 if(s.status==='done'&&loadedScan!==s.scan_id&&!loading){
  // Do not overwrite a user's deliberate strategy selection after a previous completed scan.
  if((s.mode==='live14')!==($('strategy').value==='reverse'))return;
  if(s.mode==='live14'&&$('preset').value!==s.preset)return;
  if(s.mode!=='live14'&&$('trendTf').value!==s.preset)return;
  loading=true;try{let d=await(await fetch('/results',{cache:'no-store'})).json();if(!d.summary[0]||d.summary[0].scan_id!==s.scan_id)return;result=d;loadedScan=s.scan_id;resultMode=s.mode;if(s.mode==='live14')$('preset').value=s.preset;
  for(let k of ['features','summary','coverage','errors'])$(k+'CSV').href='/csv/'+k+'?mode='+s.mode;
  $('out').hidden=false;show()}finally{loading=false}
 }
 }catch(e){$('phase').textContent='연결 확인: '+e.message}}
syncMode();

const FEE=.0004,STORE='reverse-swing-paper-v1',INITIAL=1000000;
let account={trades:[]},storageOK=true,paperBusy=false,quotes={};
try{let raw=localStorage.getItem(STORE);if(raw){let a=JSON.parse(raw);if(!Array.isArray(a.trades))throw Error('invalid data');account=a}}catch(e){storageOK=false}
function savePaper(next){localStorage.setItem(STORE,JSON.stringify(next));account=next}
function paperStats(){let closed=account.trades.filter(x=>x.closedAt),open=account.trades.filter(x=>!x.closedAt),real=closed.reduce((s,x)=>s+x.pnl,0),spent=open.reduce((s,x)=>s+x.amount,0);return {closed,open,real,cash:INITIAL+real-spent,wins:closed.filter(x=>x.pnl>0).length,losses:closed.filter(x=>x.pnl<0).length}}
function paperMsg(s){$('paperMsg').textContent=s}
function paperTime(t){return new Date(t).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})}
async function quote(markets){let all={};for(let i=0;i<markets.length;i+=40){let r=await fetch('/paper/quotes?markets='+encodeURIComponent(markets.slice(i,i+40).join(',')),{cache:'no-store'});if(!r.ok)throw Error('현재가 조회 실패');let d=await r.json();Object.assign(all,d.quotes)}return all}
async function paperBuy(x){
 if(paperBusy||!storageOK||!result||!['BUY','WATCH'].includes(x.status))return;
 let a=Number($('paperAmount').value),s=paperStats();
 if(!Number.isFinite(a)||a<5000||a>s.cash){paperMsg('가상 잔액 내에서 5,000원 이상 입력해 주세요.');return}
 if(s.open.some(t=>t.market===x.market)){paperMsg('이미 모의 보유 중인 종목입니다.');return}
 if(s.closed.some(t=>t.market===x.market&&t.signalTime===x.signal_time)){paperMsg('같은 신호에서 이미 매매한 종목입니다.');return}
 paperBusy=true;renderPaper();paperMsg('새로운 현재가 조회 중...');
 try{let r=await fetch((x.strategy==='trend'?'/trend20/entry':'/paper/entry')+'?market='+encodeURIComponent(x.market)+'&signal='+encodeURIComponent(x.signal_time)+'&hour='+encodeURIComponent(x.hour_signal_time)+'&preset='+encodeURIComponent(x.preset)+'&scan_id='+encodeURIComponent(result.summary[0].scan_id)+'&entry_status='+encodeURIComponent(x.status),{cache:'no-store'});if(!r.ok)throw Error('신호 만료, 진입 범위 제외 또는 가격 조회 실패. 다시 검색해 주세요.');let q=await r.json();if(q.entry_status!==x.status)throw Error('검색 판정 변경. 다시 검색해 주세요.');let p=q.price;if(!Number.isFinite(p)||p<=0)throw Error('유효한 현재가 없음');
 let t={id:Date.now()+'-'+x.market,market:x.market,signalTime:x.signal_time,boughtAt:q.observed_ms,amount:a,buy:p,qty:a/(1+FEE)/p,deadline:q.observed_ms+7*86400000,signalDeadline:q.signal_deadline_ms,preset:q.preset,minDrawdownPct:q.min_drawdown_pct,minDownDays:q.min_down_days,entryStatus:q.entry_status,strategy:x.strategy||'reverse',timeframe:x.strategy==='trend'?x.preset:null};savePaper({trades:[...account.trades,t]});quotes[x.market]=q;paperMsg(x.market+' '+q.entry_status+' 모의매수 완료 (시장 참고가 가정).');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function paperSell(t){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('매도 참고가 조회 중...');
 try{let q=(await quote([t.market]))[t.market];if(!q)throw Error('가격 확인 불가');let proceeds=t.qty*q.price*(1-FEE),pnl=proceeds-t.amount;let closed={...t,closedAt:q.observed_ms,sell:q.price,proceeds,pnl,netPct:100*pnl/t.amount};savePaper({trades:account.trades.map(x=>x.id===t.id?closed:x)});quotes[t.market]=q;paperMsg(t.market+' 모의매도 완료. 순손익 '+num(pnl)+'원');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function refreshPaper(){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('보유 종목 가격 조회 중...');
 try{let m=paperStats().open.map(x=>x.market);if(m.length)quotes=await quote(m);paperMsg('가격 갱신 완료. 가격 확인 불가 종목은 — 표시.')}catch(e){quotes={};paperMsg(e.message)}finally{paperBusy=false;renderPaper()}
}
function renderPaper(){
 let s=paperStats();$('paperSummary').textContent='가상 원금 1,000,000원 · 잔액 '+num(s.cash)+'원 · 실현손익 '+num(s.real)+'원';$('paperWin').textContent='보유 '+s.open.length+' · 청산 '+s.closed.length+' · 수익 '+s.wins+' · 손실 '+s.losses+' · 승률 '+(s.closed.length?(100*s.wins/s.closed.length).toFixed(2)+'%':'—');
 $('refreshPaper').disabled=paperBusy||!storageOK;
 let cards=s.open.map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 모의 보유'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매수가',num(t.buy)+'원');line(c,'투자금(수수료 포함)',num(t.amount)+'원');let q=quotes[t.market];let val=q?t.qty*q.price*(1-FEE)-t.amount:null;line(c,'조회 가격',q?num(q.price)+'원':'—');line(c,'가격 조회',q?paperTime(q.observed_ms):'—');line(c,'평가 순손익',val===null?'—':num(val)+'원 / '+pct(100*val/t.amount),val===null?'muted':val>=0?'green':'red');line(c,'7일 보유 만료',paperTime(t.deadline),Date.now()>=t.deadline?'red':'muted');if(Date.now()>=t.deadline)c.append(node('p','7일 경과: 직접 모의매도 필요','red'));let b=node('button','모의매도');b.disabled=paperBusy||!storageOK;b.onclick=()=>paperSell(t);c.append(b);return c});
 $('paperOpen').replaceChildren(...cards);if(!cards.length)$('paperOpen').textContent='보유 종목 없음';
 let hist=s.closed.slice().reverse().slice(0,100).map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 청산 완료'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 / 매도',num(t.buy)+' / '+num(t.sell));line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매도 시각',paperTime(t.closedAt));line(c,'순손익',num(t.pnl)+'원 / '+pct(t.netPct),t.pnl>=0?'green':'red');return c});$('paperClosed').replaceChildren(...hist);if(!hist.length)$('paperClosed').textContent='청산 기록 없음';if(!storageOK)paperMsg('브라우저 저장소 사용 불가 또는 기록 손상. 모의매매를 중단합니다.');
}
$('refreshPaper').onclick=refreshPaper;
$('paperCSV').onclick=()=>{let keys=['market','strategy','timeframe','preset','entryStatus','minDrawdownPct','minDownDays','signalTime','boughtAt','amount','buy','qty','deadline','closedAt','sell','proceeds','pnl','netPct'];let esc=v=>'"'+String(v??'').replaceAll('"','""')+'"';let rows=[keys.join(','),...account.trades.map(x=>keys.map(k=>esc(x[k])).join(','))];let url=URL.createObjectURL(new Blob(['\ufeff'+rows.join('\r\n')],{type:'text/csv;charset=utf-8'}));let a=document.createElement('a');a.href=url;a.download='BITHUMB_SWING_V2_3_TEST_PAPER_'+new Date().toISOString().slice(0,10)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
renderPaper();

poll();setInterval(poll,2000);
</script></html>"""
HTML24=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BITHUMB SWING V2.4 TEST</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1000px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:10px;margin:4px;border-radius:7px;text-decoration:none}input{background:#10151d}button:disabled{opacity:.5}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:10px}.coin{background:#111923;border:1px solid #3b4d60;border-radius:12px;padding:12px}.coin h3{margin:0 0 8px}.line{display:flex;justify-content:space-between;gap:10px;margin:6px 0}.green{color:#72ddb0}.red{color:#ff9b9b}.muted{color:#a9bac9}h1{font-size:23px;margin-bottom:4px}</style><h1>BITHUMB SWING V2.4 TEST</h1><a href="/live143">기존 V1.4.3 보존판</a><section><h2>매수 후보 검색</h2><p><label>전략 <select id="strategy"><option value="reverse">저점 반등형 (기존)</option><option value="trend">상승 눌림형 (실험)</option></select></label></p><div id="reverseRules"><p>20일 하락 추세 + 선택한 하락폭·하락일 수 + 전일 거래량 2배 이상 + D-3 대비 2배 이상 · RSI 제외</p><small>최근 20일과 오늘의 일별 고가가 시가 또는 전일 종가 대비 한 번이라도 30% 이상 상승하면 제외. 과거 급등 이력 부족 시 제외. 강화 일봉 조건 충족 WATCH. 가장 최근 완료 1시간봉의 SMA5 상향 돌파만 BUY 후보. 미완료 봉 제외. BUY/WATCH 모의매수 모두 검색된 1시간 동안 유효. WATCH는 5이평 상향 돌파 미확인 상태의 비교 실험이며 BUY로 변경하지 않음. 검증 전 실전 매수 권장 아님. 검색 시점의 가격은 실제 체결가가 아니며 당일 시가 진입 백테스트와 다를 수 있음. 최고가 매도 가정 결과는 실제 승률이 아님. 자동 주문 없음. 별도 매도·손절 신호 미검증. 기존 보유 종목은 직접 제외.</small></div><div id="trendRules" hidden><p id="newRules"><span id="directRuleText">신규 20/30: 완료된 240분봉 30개 중 종가 &lt; 시가인 봉 20개 이상.</span> 연속 불필요. 상승폭·눌림폭·4시간 거래량 필터 없음. BUY는 기존 1시간봉 양봉·직전 고가 돌파·직전 3시간 평균 거래량 1.2배 이상. 실제 거래소 240분봉 경계 사용. 15일 진입 검증, 다음 시간봉 시가 진입 후 1·3일 평가. 누락 제외.</p><div id="oldTrendRules"><p>선택 시간봉: 눌림 전 20개 봉 상승폭 10% 이상 · 연속 음봉 2~4개 · 종가 눌림 3~10% (저가 최대 10%) · 눌림 평균 거래량 감소.</p><small>4시간봉은 KST 00/04/08/12/16/20시, 1시간봉으로 합성(누락 봉 제외). BUY: 완료 1시간봉 양봉·직전 고가 돌파·직전 3시간 평균 거래량 1.2배 이상, 이전 고점 미초과. WATCH는 반등 미확인. 일봉 추세 필터는 아직 적용하지 않음. 15일 백테스트는 같은 조건의 BUY만 다음 시간봉 시가 진입, 1·3일 수익률, 동일 코인 72시간 중복 제외. 현재 상장종목 기준, 누락은 제외. 실제 주문 없음.</small></div><p>시간봉 <select id="trendTf"><option value="U20ALL" selected>4시간봉 · 20개 중 양봉 10~20개 동시 비교 (백테스트)</option><option value="T42ALL">4시간봉 · 42개 중 30~36개 동시 비교 (백테스트)</option><option value="T430">4시간봉 · 30개 중 20개 이상 음봉 (신규)</option><option value="T415">4시간봉 · 20개 중 15개 이상 음봉</option><option value="T4">4시간봉 (기존)</option><option value="T1">1시간봉</option></select><button id="back">15일 백테스트</button></p></div><p><label id="presetLabel" for="preset">검색 조건</label><select id="preset"><option value="A">A · 30% / 15일</option><option value="B">B · 35% / 14일</option><option value="C">C · 40% / 15일</option><option value="D" selected>D · 35% / 13일 (완화 테스트)</option></select><button id="go">전체 코인 검색 시작</button></p><p id="applied" class="muted">조건을 선택하고 검색해 주세요.</p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section><h2>모의매매 계좌</h2><small>가상 원금 100만원, 1회 투자금 변경 가능. 현재가 조회 후 전량 가상 체결(편도 0.04%). 직접 매수/매도, 7일 경과 표시만 제공. 실제 주문 없음. 기록은 이 브라우저에만 저장, 기기·브라우저 변경/데이터 삭제 시 유실될 수 있음. CSV 백업 권장.</small><p>1회 투자금(원) <input id="paperAmount" type="number" min="5000" step="1000" value="100000" style="width:105px"><button id="refreshPaper">보유 가격 갱신</button><button id="paperCSV">모의매매 CSV</button></p><p id="paperSummary"></p><p id="paperWin"></p><p id="paperMsg" class="muted"></p><h3>보유 종목</h3><div class="cards" id="paperOpen"></div><details><summary>청산 기록 (최근 100건)</summary><div class="cards" id="paperClosed"></div></details></section><section id="out" hidden><h2 id="headline"></h2><p id="audit" class="muted"></p><p id="notice" class="muted"></p><div id="compare23" hidden></div><a id="featuresCSV" href="/csv/features?mode=live14">결과 CSV</a><a id="summaryCSV" href="/csv/summary?mode=live14">요약 CSV</a><a id="coverageCSV" href="/csv/coverage?mode=live14">수집 상태 CSV</a><a id="errorsCSV" href="/csv/errors?mode=live14">오류 CSV</a><p><input id="query" placeholder="코인 검색" style="width:105px"><select id="sort"><option value="market">종목명 순</option><option value="volume">거래량 증가율 순</option><option value="depth">고점 대비 하락폭 순</option><option value="gap">당일 시가 대비 변동률 순</option></select></p><p id="count"></p><div class="cards" id="cards"></div><button id="more">더 보기</button></section><script>
const $=id=>document.getElementById(id);let result=null,loading=false,limit=5,loadedScan=null;const names={A:"A · 30% / 15일",B:"B · 35% / 14일",C:"C · 40% / 15일",D:"D · 35% / 13일",T4:"상승 눌림 · 4시간봉",T1:"상승 눌림 · 1시간봉",T415:"4시간봉 음봉 15/20",T430:"4시간봉 음봉 20/30",T42ALL:"42봉 · 음봉 30~36개 비교",U20ALL:"20봉 · 양봉 10~20개 비교"};
function num(x){return typeof x==='number'?x.toLocaleString('ko-KR',{maximumSignificantDigits:8}):'—'}
function pct(x){return typeof x==='number'?(x>0?'+':'')+x.toFixed(2)+'%':'—'}
function node(tag,txt,cls){let e=document.createElement(tag);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
function line(card,k,v,cls){let r=node('div',null,'line');r.append(node('span',k,'muted'),node('strong',v,cls));card.append(r)}
function render(){if(!result)return;if(result.summary[0].strategy==="trend"){renderTrend();return;}let q=$('query').value.trim().toUpperCase(),sort=$('sort').value;let a=result.rank.filter(x=>x.market.includes(q));let key={volume:'volume_increase_pct',depth:'peak_to_last_pct',gap:'change_from_open_pct'}[sort];if(key)a.sort((x,y)=>sort==='depth'?(x[key]??Infinity)-(y[key]??Infinity):(y[key]??-Infinity)-(x[key]??-Infinity));else a.sort((x,y)=>x.market.localeCompare(y.market));$('count').textContent=a.length+'종목 · '+Math.min(limit,a.length)+'개 표시';a.sort((x,y)=>Number(y.status==="BUY")-Number(x.status==="BUY"));let cards=a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+(x.status==='BUY'?'BUY 후보':'WATCH'),x.status==='BUY'?'green':'muted'));line(c,'적용 검색식',names[x.preset]||x.preset);line(c,'검색 시 참고가',num(x.reference_price)+'원');line(c,'당일 시가',num(x.day_open)+'원');line(c,'시가 대비',pct(x.change_from_open_pct),x.change_from_open_pct>=0?'green':'red');line(c,'당일 최고 상승률',pct(x.day_high_pct));line(c,'20일+오늘 최대 일중 급등',pct(x.lookback_max_spike_pct));line(c,'최대 급등 날짜',x.lookback_max_spike_date||'—');line(c,'20일 실제 하락일',x.down_days+'/20일');line(c,'20일 종가 변화',pct(x.trend_net_pct),'red');line(c,'고점 대비 전일 종가',pct(x.peak_to_last_pct),'red');line(c,'거래량 배수',num(x.vol_ratio_d3)+' → '+num(x.vol_ratio_d1)+'배','green');line(c,'거래량 배수 증가율',pct(x.volume_increase_pct));line(c,'참고가 기준 본전',num(x.breakeven_price)+'원');line(c,'신호 확정(KST)',x.signal_time);line(c,'가격 조회(KST)',x.observed_at);line(c,'1시간봉 5이평',num(x.hour_ma5));line(c,'1시간봉 확인',x.hour_valid?x.hour_signal_time:'데이터 부족');line(c,'7일째 일봉 날짜',x.planned_last_day);let b=node('button',paperStats().open.some(t=>t.market===x.market)?'모의 보유 중':x.status==='WATCH'?'WATCH 모의매수':'BUY 모의매수');b.disabled=!['BUY','WATCH'].includes(x.status)||paperBusy||!storageOK||paperStats().open.some(t=>t.market===x.market);b.onclick=()=>paperBuy(x);c.append(b);return c});$('cards').replaceChildren(...cards);$('more').hidden=a.length<=limit}
function show(){let x=result.summary[0];$('compare23').hidden=true;if(x.strategy==="trend"){showTrend(x);return;}$('applied').textContent='결과 적용 조건: '+names[x.preset];$('headline').textContent='BUY '+x.buys+'개 · WATCH '+x.watches+'개 · 20일+오늘 급등 제외 '+x.spike_excluded+'개';$('audit').textContent='신호 확정 '+x.signal_time+' KST · 검색 완료 '+x.finished_at+' KST · 분석 '+x.valid+'/'+x.universe+' · 이력 부족 '+x.history_missing+' · 현재가 없음 '+x.no_quote+' · 급등 이력 미확인 '+x.spike_history_missing;$('notice').textContent='수수료 편도 0.04% 적용. 본전가는 참고가 기준 이론값(호가단위·슬리피지 미반영). 직접 매매 전 현재가와 보유 여부 확인.';limit=5;render()}

let resultMode=null;
function syncMode(){let trend=$('strategy').value==='trend';let tf=$('trendTf').value;let direct=['T415','T430','T42ALL','U20ALL'].includes(tf);$('newRules').hidden=!direct;$('oldTrendRules').hidden=direct;$('directRuleText').textContent=tf==='U20ALL'?'20개 완료 4시간봉 중 종가 > 시가인 양봉 최소 10~20개를 각각 비교 (백테스트 전용).':tf==='T42ALL'?'42개 완료 4시간봉 중 음봉 최소 30,31,32,33,34,35,36개를 각각 비교 (백테스트 전용).':tf==='T430'?'신규 20/30: 완료된 240분봉 30개 중 종가 < 시가인 봉 20개 이상.':'15/20: 완료된 240분봉 20개 중 종가 < 시가인 봉 15개 이상.';$('reverseRules').hidden=trend;$('trendRules').hidden=!trend;$('preset').hidden=trend;$('presetLabel').hidden=trend;}
function clearResult(){result=null;loadedScan=null;resultMode=null;$('out').hidden=true;}
$('strategy').onchange=()=>{syncMode();clearResult();$('applied').textContent='전략을 선택하고 새로 검색해 주세요.'};
$('trendTf').onchange=()=>{syncMode();clearResult();$('applied').textContent='시간봉 변경: 다시 검색해 주세요.'};
$('preset').onchange=()=>{clearResult();$('applied').textContent='조건 변경: 다시 검색해 주세요.'};
$('query').oninput=$('sort').onchange=()=>{limit=5;render()};$('more').onclick=()=>{limit+=5;render()};
async function begin20(back){try{$('go').disabled=true;$('back').disabled=true;$('strategy').disabled=true;$('preset').disabled=true;$('trendTf').disabled=true;let trend=$('strategy').value==='trend';if(trend&&['T42ALL','U20ALL'].includes($('trendTf').value))back=true;let url=trend?'/trend20/start?preset='+encodeURIComponent($('trendTf').value)+'&back='+(back?'1':'0'):'/live14/start?preset='+encodeURIComponent($('preset').value);let r=await fetch(url,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 검색 실행 중');clearResult();await poll()}catch(e){$('phase').textContent=e.message;for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=false}}
$('go').onclick=()=>begin20(false);$('back').onclick=()=>begin20(true);
function showTrend(x){
 if(['T42ALL','U20ALL'].includes(x.preset)){show23(x);return}
 $('applied').textContent='결과 적용 조건: '+names[x.preset];
 $('headline').textContent=resultMode==='trendback20'?'15일 과거 BUY 진입 '+x.matches+'건 · 3일 평균 '+pct(x.day3_mean_pct):'BUY '+x.buys+'개 · WATCH '+x.watches+'개';
 $('audit').textContent='분석 '+x.valid+'/'+x.universe+' · 이력 부족 종목 '+x.history_missing+' · 검색 완료 '+x.finished_at;
 $('notice').textContent=resultMode==='trendback20'?'1일 평균 '+pct(x.day1_mean_pct)+' · 3일 최고 순수익 5% 도달률 '+pct(x.day3_hit5_pct)+' · 평가 누락 '+x.outcome_missing+'건 · 누락 시간 '+x.missing_hours+' · 가격 범위 제외 '+x.price_skipped+' · 매수 후 72시간 중복 제외':'상승 눌림 실험 조건 · 편도 0.04% · BUY/WATCH 모의매수만 가능';
 limit=5;render();
}
function show23(x){
 $('applied').textContent=names[x.preset];$('headline').textContent=x.preset==='U20ALL'?'20봉 · 양봉 11개 기준 동시 비교':'42봉 · 7개 기준 동시 비교';
 $('audit').textContent=x.first_entry+' ~ '+x.last_entry_exclusive+' (종료 시각 미포함) · 전체 '+x.universe+' · 수집 성공 '+x.market_success;
 $('notice').textContent='각 기준별 72시간 중복 제외 · 1/3일 종료 수익 및 최고가 기회 (실현 수익 아님) · 동일 표본이 여러 그룹에 포함 가능 · 수수료 왕복 0.08%';
 let panel=$('compare23');panel.hidden=false;panel.replaceChildren(...result.summary.map(r=>{let c=node('article',null,'coin');c.append(node('h3','최소 '+(r.minimum_up_bars??r.minimum_down_bars)+'/'+r.total_bars+' · '+r.matches+'건'));line(c,'1일 / 3일 종료 평균',pct(r.day1_mean_pct)+' / '+pct(r.day3_mean_pct));line(c,'3일 최고 평균',pct(r.day3_peak_mean_pct));line(c,'5% / 10% 도달률',pct(r.day3_hit5_pct)+' / '+pct(r.day3_hit10_pct));line(c,'-10% 도달률',pct(r.day3_loss10_pct));line(c,'판정 가능 / 평가 누락',pct(r.valid_pct)+' / '+r.outcome_missing+'건');return c}));limit=5;render();
}
function renderTrend(){
 let q=$('query').value.trim().toUpperCase();let a=result.rank.filter(x=>x.market.includes(q));
 if(resultMode!=='trendback20'){let sort=$('sort').value;if(sort==='volume')a.sort((x,y)=>y.confirm_volume_ratio-x.confirm_volume_ratio);else if(sort==='depth')a.sort((x,y)=>y.pullback_pct-x.pullback_pct);else if(sort==='market')a.sort((x,y)=>x.market.localeCompare(y.market));a.sort((x,y)=>Number(y.status==='BUY')-Number(x.status==='BUY'))}
 $('count').textContent=a.length+'건 · '+Math.min(limit,a.length)+'개 표시';$('more').hidden=a.length<=limit;
 $('cards').replaceChildren(...a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+x.status,x.status==='BUY'?'green':'muted'));line(c,'전략',names[x.preset]);if(['T415','T430','T42ALL','U20ALL'].includes(x.preset)){if(x.preset==='T42ALL')line(c,'비교군 최소 음봉',x.minimum_down_bars+'/42');if(x.preset==='U20ALL'){line(c,'비교군 최소 양봉',x.minimum_up_bars+'/20');line(c,'실제 양봉 수',x.up_bars+'/20개');line(c,'보합 봉',x.flat_bars+'개');}line(c,'음봉 수',x.down_bars+'/'+x.total_bars+'개');line(c,x.total_bars+'봉 변화',pct(x.window_change_pct));line(c,'고점 대비 변화',pct(-x.pullback_pct));line(c,'마지막 4h 거래량 / 이전 '+(x.total_bars-1)+'봉',num(x.last_4h_volume_ratio)+'배');line(c,'4h 조건 확정',x.setup_end);}else{line(c,'이전 20봉 상승',pct(x.prior_gain_pct));line(c,'연속 음봉',x.pullback_bars+'개');line(c,'눌림폭',pct(-x.pullback_pct));line(c,'눌림 거래량 / 이전 평균',num(x.pullback_volume_ratio)+'배');}line(c,'반등 시간봉 거래량',num(x.confirm_volume_ratio)+'배');line(c,'신호 확정(KST)',x.signal_time);
 if(resultMode==='trendback20'){line(c,'진입가',num(x.buy_price)+'원');line(c,'1일 종료 / 최고',pct(x.day1_close_pct)+' / '+pct(x.day1_peak_pct));line(c,'3일 종료 / 최고',pct(x.day3_close_pct)+' / '+pct(x.day3_peak_pct));line(c,'3일 최저',pct(x.day3_low_pct));}
 else{line(c,'검색 참고가',num(x.reference_price)+'원');line(c,'이전 고점',num(x.prior_peak)+'원');line(c,'눌림 저점',num(x.pullback_low)+'원');let held=paperStats().open.some(t=>t.market===x.market),b=node('button',held?'모의 보유 중':x.status+' 모의매수');b.disabled=held||paperBusy||!storageOK;b.onclick=()=>paperBuy(x);c.append(b)}return c}));
}
async function poll(){try{
 let s=await(await fetch('/status',{cache:'no-store'})).json();let running=s.status==='running';
 for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=running;
 if(!['live14','trend20','trendback20'].includes(s.mode)){$('phase').textContent=running?'다른 연구 실행 중':'대기';clearResult();return}
 $('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');
 if(running){clearResult();$('applied').textContent='검색 중: '+names[s.preset];return}
 if(s.status==='done'&&loadedScan!==s.scan_id&&!loading){
  // Do not overwrite a user's deliberate strategy selection after a previous completed scan.
  if((s.mode==='live14')!==($('strategy').value==='reverse'))return;
  if(s.mode==='live14'&&$('preset').value!==s.preset)return;
  if(s.mode!=='live14'&&$('trendTf').value!==s.preset)return;
  loading=true;try{let d=await(await fetch('/results',{cache:'no-store'})).json();if(!d.summary[0]||d.summary[0].scan_id!==s.scan_id)return;result=d;loadedScan=s.scan_id;resultMode=s.mode;if(s.mode==='live14')$('preset').value=s.preset;
  for(let k of ['features','summary','coverage','errors'])$(k+'CSV').href='/csv/'+k+'?mode='+s.mode;
  $('out').hidden=false;show()}finally{loading=false}
 }
 }catch(e){$('phase').textContent='연결 확인: '+e.message}}
syncMode();

const FEE=.0004,STORE='reverse-swing-paper-v1',INITIAL=1000000;
let account={trades:[]},storageOK=true,paperBusy=false,quotes={};
try{let raw=localStorage.getItem(STORE);if(raw){let a=JSON.parse(raw);if(!Array.isArray(a.trades))throw Error('invalid data');account=a}}catch(e){storageOK=false}
function savePaper(next){localStorage.setItem(STORE,JSON.stringify(next));account=next}
function paperStats(){let closed=account.trades.filter(x=>x.closedAt),open=account.trades.filter(x=>!x.closedAt),real=closed.reduce((s,x)=>s+x.pnl,0),spent=open.reduce((s,x)=>s+x.amount,0);return {closed,open,real,cash:INITIAL+real-spent,wins:closed.filter(x=>x.pnl>0).length,losses:closed.filter(x=>x.pnl<0).length}}
function paperMsg(s){$('paperMsg').textContent=s}
function paperTime(t){return new Date(t).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})}
async function quote(markets){let all={};for(let i=0;i<markets.length;i+=40){let r=await fetch('/paper/quotes?markets='+encodeURIComponent(markets.slice(i,i+40).join(',')),{cache:'no-store'});if(!r.ok)throw Error('현재가 조회 실패');let d=await r.json();Object.assign(all,d.quotes)}return all}
async function paperBuy(x){
 if(paperBusy||!storageOK||!result||!['BUY','WATCH'].includes(x.status))return;
 let a=Number($('paperAmount').value),s=paperStats();
 if(!Number.isFinite(a)||a<5000||a>s.cash){paperMsg('가상 잔액 내에서 5,000원 이상 입력해 주세요.');return}
 if(s.open.some(t=>t.market===x.market)){paperMsg('이미 모의 보유 중인 종목입니다.');return}
 if(s.closed.some(t=>t.market===x.market&&t.signalTime===x.signal_time)){paperMsg('같은 신호에서 이미 매매한 종목입니다.');return}
 paperBusy=true;renderPaper();paperMsg('새로운 현재가 조회 중...');
 try{let r=await fetch((x.strategy==='trend'?'/trend20/entry':'/paper/entry')+'?market='+encodeURIComponent(x.market)+'&signal='+encodeURIComponent(x.signal_time)+'&hour='+encodeURIComponent(x.hour_signal_time)+'&preset='+encodeURIComponent(x.preset)+'&scan_id='+encodeURIComponent(result.summary[0].scan_id)+'&entry_status='+encodeURIComponent(x.status),{cache:'no-store'});if(!r.ok)throw Error('신호 만료, 진입 범위 제외 또는 가격 조회 실패. 다시 검색해 주세요.');let q=await r.json();if(q.entry_status!==x.status)throw Error('검색 판정 변경. 다시 검색해 주세요.');let p=q.price;if(!Number.isFinite(p)||p<=0)throw Error('유효한 현재가 없음');
 let t={id:Date.now()+'-'+x.market,market:x.market,signalTime:x.signal_time,boughtAt:q.observed_ms,amount:a,buy:p,qty:a/(1+FEE)/p,deadline:q.observed_ms+7*86400000,signalDeadline:q.signal_deadline_ms,preset:q.preset,minDrawdownPct:q.min_drawdown_pct,minDownDays:q.min_down_days,entryStatus:q.entry_status,strategy:x.strategy||'reverse',timeframe:x.strategy==='trend'?x.preset:null};savePaper({trades:[...account.trades,t]});quotes[x.market]=q;paperMsg(x.market+' '+q.entry_status+' 모의매수 완료 (시장 참고가 가정).');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function paperSell(t){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('매도 참고가 조회 중...');
 try{let q=(await quote([t.market]))[t.market];if(!q)throw Error('가격 확인 불가');let proceeds=t.qty*q.price*(1-FEE),pnl=proceeds-t.amount;let closed={...t,closedAt:q.observed_ms,sell:q.price,proceeds,pnl,netPct:100*pnl/t.amount};savePaper({trades:account.trades.map(x=>x.id===t.id?closed:x)});quotes[t.market]=q;paperMsg(t.market+' 모의매도 완료. 순손익 '+num(pnl)+'원');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function refreshPaper(){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('보유 종목 가격 조회 중...');
 try{let m=paperStats().open.map(x=>x.market);if(m.length)quotes=await quote(m);paperMsg('가격 갱신 완료. 가격 확인 불가 종목은 — 표시.')}catch(e){quotes={};paperMsg(e.message)}finally{paperBusy=false;renderPaper()}
}
function renderPaper(){
 let s=paperStats();$('paperSummary').textContent='가상 원금 1,000,000원 · 잔액 '+num(s.cash)+'원 · 실현손익 '+num(s.real)+'원';$('paperWin').textContent='보유 '+s.open.length+' · 청산 '+s.closed.length+' · 수익 '+s.wins+' · 손실 '+s.losses+' · 승률 '+(s.closed.length?(100*s.wins/s.closed.length).toFixed(2)+'%':'—');
 $('refreshPaper').disabled=paperBusy||!storageOK;
 let cards=s.open.map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 모의 보유'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매수가',num(t.buy)+'원');line(c,'투자금(수수료 포함)',num(t.amount)+'원');let q=quotes[t.market];let val=q?t.qty*q.price*(1-FEE)-t.amount:null;line(c,'조회 가격',q?num(q.price)+'원':'—');line(c,'가격 조회',q?paperTime(q.observed_ms):'—');line(c,'평가 순손익',val===null?'—':num(val)+'원 / '+pct(100*val/t.amount),val===null?'muted':val>=0?'green':'red');line(c,'7일 보유 만료',paperTime(t.deadline),Date.now()>=t.deadline?'red':'muted');if(Date.now()>=t.deadline)c.append(node('p','7일 경과: 직접 모의매도 필요','red'));let b=node('button','모의매도');b.disabled=paperBusy||!storageOK;b.onclick=()=>paperSell(t);c.append(b);return c});
 $('paperOpen').replaceChildren(...cards);if(!cards.length)$('paperOpen').textContent='보유 종목 없음';
 let hist=s.closed.slice().reverse().slice(0,100).map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 청산 완료'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 / 매도',num(t.buy)+' / '+num(t.sell));line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매도 시각',paperTime(t.closedAt));line(c,'순손익',num(t.pnl)+'원 / '+pct(t.netPct),t.pnl>=0?'green':'red');return c});$('paperClosed').replaceChildren(...hist);if(!hist.length)$('paperClosed').textContent='청산 기록 없음';if(!storageOK)paperMsg('브라우저 저장소 사용 불가 또는 기록 손상. 모의매매를 중단합니다.');
}
$('refreshPaper').onclick=refreshPaper;
$('paperCSV').onclick=()=>{let keys=['market','strategy','timeframe','preset','entryStatus','minDrawdownPct','minDownDays','signalTime','boughtAt','amount','buy','qty','deadline','closedAt','sell','proceeds','pnl','netPct'];let esc=v=>'"'+String(v??'').replaceAll('"','""')+'"';let rows=[keys.join(','),...account.trades.map(x=>keys.map(k=>esc(x[k])).join(','))];let url=URL.createObjectURL(new Blob(['\ufeff'+rows.join('\r\n')],{type:'text/csv;charset=utf-8'}));let a=document.createElement('a');a.href=url;a.download='BITHUMB_SWING_V2_4_TEST_PAPER_'+new Date().toISOString().slice(0,10)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
renderPaper();

poll();setInterval(poll,2000);
</script></html>"""
HTML25=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BITHUMB SWING V2.5 TEST</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1000px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:10px;margin:4px;border-radius:7px;text-decoration:none}input{background:#10151d}button:disabled{opacity:.5}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:10px}.coin{background:#111923;border:1px solid #3b4d60;border-radius:12px;padding:12px}.coin h3{margin:0 0 8px}.line{display:flex;justify-content:space-between;gap:10px;margin:6px 0}.green{color:#72ddb0}.red{color:#ff9b9b}.muted{color:#a9bac9}h1{font-size:23px;margin-bottom:4px}</style><h1>BITHUMB SWING V2.5 TEST</h1><a href="/live143">기존 V1.4.3 보존판</a><section><h2>매수 후보 검색</h2><p><label>전략 <select id="strategy"><option value="reverse">저점 반등형 (기존)</option><option value="trend">상승 눌림형 (실험)</option></select></label></p><div id="reverseRules"><p>20일 하락 추세 + 선택한 하락폭·하락일 수 + 전일 거래량 2배 이상 + D-3 대비 2배 이상 · RSI 제외</p><small>최근 20일과 오늘의 일별 고가가 시가 또는 전일 종가 대비 한 번이라도 30% 이상 상승하면 제외. 과거 급등 이력 부족 시 제외. 강화 일봉 조건 충족 WATCH. 가장 최근 완료 1시간봉의 SMA5 상향 돌파만 BUY 후보. 미완료 봉 제외. BUY/WATCH 모의매수 모두 검색된 1시간 동안 유효. WATCH는 5이평 상향 돌파 미확인 상태의 비교 실험이며 BUY로 변경하지 않음. 검증 전 실전 매수 권장 아님. 검색 시점의 가격은 실제 체결가가 아니며 당일 시가 진입 백테스트와 다를 수 있음. 최고가 매도 가정 결과는 실제 승률이 아님. 자동 주문 없음. 별도 매도·손절 신호 미검증. 기존 보유 종목은 직접 제외.</small></div><div id="trendRules" hidden><p id="newRules"><span id="directRuleText">신규 20/30: 완료된 240분봉 30개 중 종가 &lt; 시가인 봉 20개 이상.</span> 연속 불필요. 상승폭·눌림폭·4시간 거래량 필터 없음. BUY는 기존 1시간봉 양봉·직전 고가 돌파·직전 3시간 평균 거래량 1.2배 이상. 실제 거래소 240분봉 경계 사용. 15일 진입 검증, 다음 시간봉 시가 진입 후 1·3일 평가. 누락 제외.</p><div id="oldTrendRules"><p>선택 시간봉: 눌림 전 20개 봉 상승폭 10% 이상 · 연속 음봉 2~4개 · 종가 눌림 3~10% (저가 최대 10%) · 눌림 평균 거래량 감소.</p><small>4시간봉은 KST 00/04/08/12/16/20시, 1시간봉으로 합성(누락 봉 제외). BUY: 완료 1시간봉 양봉·직전 고가 돌파·직전 3시간 평균 거래량 1.2배 이상, 이전 고점 미초과. WATCH는 반등 미확인. 일봉 추세 필터는 아직 적용하지 않음. 15일 백테스트는 같은 조건의 BUY만 다음 시간봉 시가 진입, 1·3일 수익률, 동일 코인 72시간 중복 제외. 현재 상장종목 기준, 누락은 제외. 실제 주문 없음.</small></div><p>시간봉 <select id="trendTf"><option value="P20ALL" selected>4시간봉 · 상승폭 3/5/10% + 눌림 최종 비교 (백테스트)</option><option value="U20ALL">4시간봉 · 20개 중 양봉 10~20개 동시 비교 (백테스트)</option><option value="T42ALL">4시간봉 · 42개 중 30~36개 동시 비교 (백테스트)</option><option value="T430">4시간봉 · 30개 중 20개 이상 음봉 (신규)</option><option value="T415">4시간봉 · 20개 중 15개 이상 음봉</option><option value="T4">4시간봉 (기존)</option><option value="T1">1시간봉</option></select><button id="back">15일 백테스트</button></p></div><p><label id="presetLabel" for="preset">검색 조건</label><select id="preset"><option value="A">A · 30% / 15일</option><option value="B">B · 35% / 14일</option><option value="C">C · 40% / 15일</option><option value="D" selected>D · 35% / 13일 (완화 테스트)</option></select><button id="go">전체 코인 검색 시작</button></p><p id="applied" class="muted">조건을 선택하고 검색해 주세요.</p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section><h2>모의매매 계좌</h2><small>가상 원금 100만원, 1회 투자금 변경 가능. 현재가 조회 후 전량 가상 체결(편도 0.04%). 직접 매수/매도, 7일 경과 표시만 제공. 실제 주문 없음. 기록은 이 브라우저에만 저장, 기기·브라우저 변경/데이터 삭제 시 유실될 수 있음. CSV 백업 권장.</small><p>1회 투자금(원) <input id="paperAmount" type="number" min="5000" step="1000" value="100000" style="width:105px"><button id="refreshPaper">보유 가격 갱신</button><button id="paperCSV">모의매매 CSV</button></p><p id="paperSummary"></p><p id="paperWin"></p><p id="paperMsg" class="muted"></p><h3>보유 종목</h3><div class="cards" id="paperOpen"></div><details><summary>청산 기록 (최근 100건)</summary><div class="cards" id="paperClosed"></div></details></section><section id="out" hidden><h2 id="headline"></h2><p id="audit" class="muted"></p><p id="notice" class="muted"></p><div id="compare23" hidden></div><a id="featuresCSV" href="/csv/features?mode=live14">결과 CSV</a><a id="summaryCSV" href="/csv/summary?mode=live14">요약 CSV</a><a id="coverageCSV" href="/csv/coverage?mode=live14">수집 상태 CSV</a><a id="errorsCSV" href="/csv/errors?mode=live14">오류 CSV</a><p><input id="query" placeholder="코인 검색" style="width:105px"><select id="sort"><option value="market">종목명 순</option><option value="volume">거래량 증가율 순</option><option value="depth">고점 대비 하락폭 순</option><option value="gap">당일 시가 대비 변동률 순</option></select></p><p id="count"></p><div class="cards" id="cards"></div><button id="more">더 보기</button></section><script>
const $=id=>document.getElementById(id);let result=null,loading=false,limit=5,loadedScan=null;const names={A:"A · 30% / 15일",B:"B · 35% / 14일",C:"C · 40% / 15일",D:"D · 35% / 13일",T4:"상승 눌림 · 4시간봉",T1:"상승 눌림 · 1시간봉",T415:"4시간봉 음봉 15/20",T430:"4시간봉 음봉 20/30",T42ALL:"42봉 · 음봉 30~36개 비교",U20ALL:"20봉 · 양봉 10~20개 비교",P20ALL:"20봉 상승폭 + 눌림 최종 비교"};
function num(x){return typeof x==='number'?x.toLocaleString('ko-KR',{maximumSignificantDigits:8}):'—'}
function pct(x){return typeof x==='number'?(x>0?'+':'')+x.toFixed(2)+'%':'—'}
function node(tag,txt,cls){let e=document.createElement(tag);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
function line(card,k,v,cls){let r=node('div',null,'line');r.append(node('span',k,'muted'),node('strong',v,cls));card.append(r)}
function render(){if(!result)return;if(result.summary[0].strategy==="trend"){renderTrend();return;}let q=$('query').value.trim().toUpperCase(),sort=$('sort').value;let a=result.rank.filter(x=>x.market.includes(q));let key={volume:'volume_increase_pct',depth:'peak_to_last_pct',gap:'change_from_open_pct'}[sort];if(key)a.sort((x,y)=>sort==='depth'?(x[key]??Infinity)-(y[key]??Infinity):(y[key]??-Infinity)-(x[key]??-Infinity));else a.sort((x,y)=>x.market.localeCompare(y.market));$('count').textContent=a.length+'종목 · '+Math.min(limit,a.length)+'개 표시';a.sort((x,y)=>Number(y.status==="BUY")-Number(x.status==="BUY"));let cards=a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+(x.status==='BUY'?'BUY 후보':'WATCH'),x.status==='BUY'?'green':'muted'));line(c,'적용 검색식',names[x.preset]||x.preset);line(c,'검색 시 참고가',num(x.reference_price)+'원');line(c,'당일 시가',num(x.day_open)+'원');line(c,'시가 대비',pct(x.change_from_open_pct),x.change_from_open_pct>=0?'green':'red');line(c,'당일 최고 상승률',pct(x.day_high_pct));line(c,'20일+오늘 최대 일중 급등',pct(x.lookback_max_spike_pct));line(c,'최대 급등 날짜',x.lookback_max_spike_date||'—');line(c,'20일 실제 하락일',x.down_days+'/20일');line(c,'20일 종가 변화',pct(x.trend_net_pct),'red');line(c,'고점 대비 전일 종가',pct(x.peak_to_last_pct),'red');line(c,'거래량 배수',num(x.vol_ratio_d3)+' → '+num(x.vol_ratio_d1)+'배','green');line(c,'거래량 배수 증가율',pct(x.volume_increase_pct));line(c,'참고가 기준 본전',num(x.breakeven_price)+'원');line(c,'신호 확정(KST)',x.signal_time);line(c,'가격 조회(KST)',x.observed_at);line(c,'1시간봉 5이평',num(x.hour_ma5));line(c,'1시간봉 확인',x.hour_valid?x.hour_signal_time:'데이터 부족');line(c,'7일째 일봉 날짜',x.planned_last_day);let b=node('button',paperStats().open.some(t=>t.market===x.market)?'모의 보유 중':x.status==='WATCH'?'WATCH 모의매수':'BUY 모의매수');b.disabled=!['BUY','WATCH'].includes(x.status)||paperBusy||!storageOK||paperStats().open.some(t=>t.market===x.market);b.onclick=()=>paperBuy(x);c.append(b);return c});$('cards').replaceChildren(...cards);$('more').hidden=a.length<=limit}
function show(){let x=result.summary[0];$('compare23').hidden=true;if(x.strategy==="trend"){showTrend(x);return;}$('applied').textContent='결과 적용 조건: '+names[x.preset];$('headline').textContent='BUY '+x.buys+'개 · WATCH '+x.watches+'개 · 20일+오늘 급등 제외 '+x.spike_excluded+'개';$('audit').textContent='신호 확정 '+x.signal_time+' KST · 검색 완료 '+x.finished_at+' KST · 분석 '+x.valid+'/'+x.universe+' · 이력 부족 '+x.history_missing+' · 현재가 없음 '+x.no_quote+' · 급등 이력 미확인 '+x.spike_history_missing;$('notice').textContent='수수료 편도 0.04% 적용. 본전가는 참고가 기준 이론값(호가단위·슬리피지 미반영). 직접 매매 전 현재가와 보유 여부 확인.';limit=5;render()}

let resultMode=null;
function syncMode(){let trend=$('strategy').value==='trend';let tf=$('trendTf').value;let direct=['T415','T430','T42ALL','U20ALL','P20ALL'].includes(tf);$('newRules').hidden=!direct;$('oldTrendRules').hidden=direct;$('directRuleText').textContent=tf==='P20ALL'?'최종 비교: 완료 20봉 중 양봉 10개 이상, 순상승폭 최소 3/5/10%, 마지막 연속 음봉 1~3개, 음봉 직전까지의 고점 대비 종가 눌림 2~10%.':tf==='U20ALL'?'20개 완료 4시간봉 중 종가 > 시가인 양봉 최소 10~20개를 각각 비교 (백테스트 전용).':tf==='T42ALL'?'42개 완료 4시간봉 중 음봉 최소 30,31,32,33,34,35,36개를 각각 비교 (백테스트 전용).':tf==='T430'?'신규 20/30: 완료된 240분봉 30개 중 종가 < 시가인 봉 20개 이상.':'15/20: 완료된 240분봉 20개 중 종가 < 시가인 봉 15개 이상.';$('reverseRules').hidden=trend;$('trendRules').hidden=!trend;$('preset').hidden=trend;$('presetLabel').hidden=trend;}
function clearResult(){result=null;loadedScan=null;resultMode=null;$('out').hidden=true;}
$('strategy').onchange=()=>{syncMode();clearResult();$('applied').textContent='전략을 선택하고 새로 검색해 주세요.'};
$('trendTf').onchange=()=>{syncMode();clearResult();$('applied').textContent='시간봉 변경: 다시 검색해 주세요.'};
$('preset').onchange=()=>{clearResult();$('applied').textContent='조건 변경: 다시 검색해 주세요.'};
$('query').oninput=$('sort').onchange=()=>{limit=5;render()};$('more').onclick=()=>{limit+=5;render()};
async function begin20(back){try{$('go').disabled=true;$('back').disabled=true;$('strategy').disabled=true;$('preset').disabled=true;$('trendTf').disabled=true;let trend=$('strategy').value==='trend';if(trend&&['T42ALL','U20ALL','P20ALL'].includes($('trendTf').value))back=true;let url=trend?'/trend20/start?preset='+encodeURIComponent($('trendTf').value)+'&back='+(back?'1':'0'):'/live14/start?preset='+encodeURIComponent($('preset').value);let r=await fetch(url,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 검색 실행 중');clearResult();await poll()}catch(e){$('phase').textContent=e.message;for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=false}}
$('go').onclick=()=>begin20(false);$('back').onclick=()=>begin20(true);
function showTrend(x){
 if(['T42ALL','U20ALL','P20ALL'].includes(x.preset)){show23(x);return}
 $('applied').textContent='결과 적용 조건: '+names[x.preset];
 $('headline').textContent=resultMode==='trendback20'?'15일 과거 BUY 진입 '+x.matches+'건 · 3일 평균 '+pct(x.day3_mean_pct):'BUY '+x.buys+'개 · WATCH '+x.watches+'개';
 $('audit').textContent='분석 '+x.valid+'/'+x.universe+' · 이력 부족 종목 '+x.history_missing+' · 검색 완료 '+x.finished_at;
 $('notice').textContent=resultMode==='trendback20'?'1일 평균 '+pct(x.day1_mean_pct)+' · 3일 최고 순수익 5% 도달률 '+pct(x.day3_hit5_pct)+' · 평가 누락 '+x.outcome_missing+'건 · 누락 시간 '+x.missing_hours+' · 가격 범위 제외 '+x.price_skipped+' · 매수 후 72시간 중복 제외':'상승 눌림 실험 조건 · 편도 0.04% · BUY/WATCH 모의매수만 가능';
 limit=5;render();
}
function show23(x){
 $('applied').textContent=names[x.preset];$('headline').textContent=x.preset==='P20ALL'?'상승폭 3/5/10% + 눌림 최종 비교':x.preset==='U20ALL'?'20봉 · 양봉 11개 기준 동시 비교':'42봉 · 7개 기준 동시 비교';
 $('audit').textContent=x.first_entry+' ~ '+x.last_entry_exclusive+' (종료 시각 미포함) · 전체 '+x.universe+' · 수집 성공 '+x.market_success;
 $('notice').textContent='각 기준별 72시간 중복 제외 · 1/3일 종료 수익 및 최고가 기회 (실현 수익 아님) · 동일 표본이 여러 그룹에 포함 가능 · 수수료 왕복 0.08%';
 let panel=$('compare23');panel.hidden=false;panel.replaceChildren(...result.summary.map(r=>{let c=node('article',null,'coin');c.append(node('h3',(r.preset==='P20ALL'?'최소 상승 '+r.minimum_gain_pct+'%':'최소 '+(r.minimum_up_bars??r.minimum_down_bars)+'/'+r.total_bars)+' · '+r.matches+'건'));line(c,'1일 / 3일 종료 평균',pct(r.day1_mean_pct)+' / '+pct(r.day3_mean_pct));line(c,'1일 / 3일 중앙값',pct(r.day1_median_pct)+' / '+pct(r.day3_median_pct));line(c,'1일 / 3일 종료 승률',pct(r.day1_win_pct)+' / '+pct(r.day3_win_pct));line(c,'3일 최고 평균',pct(r.day3_peak_mean_pct));line(c,'5% / 10% 도달률',pct(r.day3_hit5_pct)+' / '+pct(r.day3_hit10_pct));line(c,'-10% 도달률',pct(r.day3_loss10_pct));line(c,'판정 가능 / 평가 누락',pct(r.valid_pct)+' / '+r.outcome_missing+'건');return c}));limit=5;render();
}
function renderTrend(){
 let q=$('query').value.trim().toUpperCase();let a=result.rank.filter(x=>x.market.includes(q));
 if(resultMode!=='trendback20'){let sort=$('sort').value;if(sort==='volume')a.sort((x,y)=>y.confirm_volume_ratio-x.confirm_volume_ratio);else if(sort==='depth')a.sort((x,y)=>y.pullback_pct-x.pullback_pct);else if(sort==='market')a.sort((x,y)=>x.market.localeCompare(y.market));a.sort((x,y)=>Number(y.status==='BUY')-Number(x.status==='BUY'))}
 $('count').textContent=a.length+'건 · '+Math.min(limit,a.length)+'개 표시';$('more').hidden=a.length<=limit;
 $('cards').replaceChildren(...a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+x.status,x.status==='BUY'?'green':'muted'));line(c,'전략',names[x.preset]);if(['T415','T430','T42ALL','U20ALL','P20ALL'].includes(x.preset)){if(x.preset==='T42ALL')line(c,'비교군 최소 음봉',x.minimum_down_bars+'/42');if(x.preset==='P20ALL'){line(c,'비교군 최소 상승폭',x.minimum_gain_pct+'%');line(c,'실제 양봉 수',x.up_bars+'/20개');line(c,'최근 연속 음봉',x.pullback_bars+'개');}if(x.preset==='U20ALL'){line(c,'비교군 최소 양봉',x.minimum_up_bars+'/20');line(c,'실제 양봉 수',x.up_bars+'/20개');line(c,'보합 봉',x.flat_bars+'개');}line(c,'음봉 수',x.down_bars+'/'+x.total_bars+'개');line(c,x.total_bars+'봉 변화',pct(x.window_change_pct));line(c,'고점 대비 변화',pct(-x.pullback_pct));line(c,'마지막 4h 거래량 / 이전 '+(x.total_bars-1)+'봉',num(x.last_4h_volume_ratio)+'배');line(c,'4h 조건 확정',x.setup_end);}else{line(c,'이전 20봉 상승',pct(x.prior_gain_pct));line(c,'연속 음봉',x.pullback_bars+'개');line(c,'눌림폭',pct(-x.pullback_pct));line(c,'눌림 거래량 / 이전 평균',num(x.pullback_volume_ratio)+'배');}line(c,'반등 시간봉 거래량',num(x.confirm_volume_ratio)+'배');line(c,'신호 확정(KST)',x.signal_time);
 if(resultMode==='trendback20'){line(c,'진입가',num(x.buy_price)+'원');line(c,'1일 종료 / 최고',pct(x.day1_close_pct)+' / '+pct(x.day1_peak_pct));line(c,'3일 종료 / 최고',pct(x.day3_close_pct)+' / '+pct(x.day3_peak_pct));line(c,'3일 최저',pct(x.day3_low_pct));}
 else{line(c,'검색 참고가',num(x.reference_price)+'원');line(c,'이전 고점',num(x.prior_peak)+'원');line(c,'눌림 저점',num(x.pullback_low)+'원');let held=paperStats().open.some(t=>t.market===x.market),b=node('button',held?'모의 보유 중':x.status+' 모의매수');b.disabled=held||paperBusy||!storageOK;b.onclick=()=>paperBuy(x);c.append(b)}return c}));
}
async function poll(){try{
 let s=await(await fetch('/status',{cache:'no-store'})).json();let running=s.status==='running';
 for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=running;
 if(!['live14','trend20','trendback20'].includes(s.mode)){$('phase').textContent=running?'다른 연구 실행 중':'대기';clearResult();return}
 $('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');
 if(running){clearResult();$('applied').textContent='검색 중: '+names[s.preset];return}
 if(s.status==='done'&&loadedScan!==s.scan_id&&!loading){
  // Do not overwrite a user's deliberate strategy selection after a previous completed scan.
  if((s.mode==='live14')!==($('strategy').value==='reverse'))return;
  if(s.mode==='live14'&&$('preset').value!==s.preset)return;
  if(s.mode!=='live14'&&$('trendTf').value!==s.preset)return;
  loading=true;try{let d=await(await fetch('/results',{cache:'no-store'})).json();if(!d.summary[0]||d.summary[0].scan_id!==s.scan_id)return;result=d;loadedScan=s.scan_id;resultMode=s.mode;if(s.mode==='live14')$('preset').value=s.preset;
  for(let k of ['features','summary','coverage','errors'])$(k+'CSV').href='/csv/'+k+'?mode='+s.mode;
  $('out').hidden=false;show()}finally{loading=false}
 }
 }catch(e){$('phase').textContent='연결 확인: '+e.message}}
syncMode();

const FEE=.0004,STORE='reverse-swing-paper-v1',INITIAL=1000000;
let account={trades:[]},storageOK=true,paperBusy=false,quotes={};
try{let raw=localStorage.getItem(STORE);if(raw){let a=JSON.parse(raw);if(!Array.isArray(a.trades))throw Error('invalid data');account=a}}catch(e){storageOK=false}
function savePaper(next){localStorage.setItem(STORE,JSON.stringify(next));account=next}
function paperStats(){let closed=account.trades.filter(x=>x.closedAt),open=account.trades.filter(x=>!x.closedAt),real=closed.reduce((s,x)=>s+x.pnl,0),spent=open.reduce((s,x)=>s+x.amount,0);return {closed,open,real,cash:INITIAL+real-spent,wins:closed.filter(x=>x.pnl>0).length,losses:closed.filter(x=>x.pnl<0).length}}
function paperMsg(s){$('paperMsg').textContent=s}
function paperTime(t){return new Date(t).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})}
async function quote(markets){let all={};for(let i=0;i<markets.length;i+=40){let r=await fetch('/paper/quotes?markets='+encodeURIComponent(markets.slice(i,i+40).join(',')),{cache:'no-store'});if(!r.ok)throw Error('현재가 조회 실패');let d=await r.json();Object.assign(all,d.quotes)}return all}
async function paperBuy(x){
 if(paperBusy||!storageOK||!result||!['BUY','WATCH'].includes(x.status))return;
 let a=Number($('paperAmount').value),s=paperStats();
 if(!Number.isFinite(a)||a<5000||a>s.cash){paperMsg('가상 잔액 내에서 5,000원 이상 입력해 주세요.');return}
 if(s.open.some(t=>t.market===x.market)){paperMsg('이미 모의 보유 중인 종목입니다.');return}
 if(s.closed.some(t=>t.market===x.market&&t.signalTime===x.signal_time)){paperMsg('같은 신호에서 이미 매매한 종목입니다.');return}
 paperBusy=true;renderPaper();paperMsg('새로운 현재가 조회 중...');
 try{let r=await fetch((x.strategy==='trend'?'/trend20/entry':'/paper/entry')+'?market='+encodeURIComponent(x.market)+'&signal='+encodeURIComponent(x.signal_time)+'&hour='+encodeURIComponent(x.hour_signal_time)+'&preset='+encodeURIComponent(x.preset)+'&scan_id='+encodeURIComponent(result.summary[0].scan_id)+'&entry_status='+encodeURIComponent(x.status),{cache:'no-store'});if(!r.ok)throw Error('신호 만료, 진입 범위 제외 또는 가격 조회 실패. 다시 검색해 주세요.');let q=await r.json();if(q.entry_status!==x.status)throw Error('검색 판정 변경. 다시 검색해 주세요.');let p=q.price;if(!Number.isFinite(p)||p<=0)throw Error('유효한 현재가 없음');
 let t={id:Date.now()+'-'+x.market,market:x.market,signalTime:x.signal_time,boughtAt:q.observed_ms,amount:a,buy:p,qty:a/(1+FEE)/p,deadline:q.observed_ms+7*86400000,signalDeadline:q.signal_deadline_ms,preset:q.preset,minDrawdownPct:q.min_drawdown_pct,minDownDays:q.min_down_days,entryStatus:q.entry_status,strategy:x.strategy||'reverse',timeframe:x.strategy==='trend'?x.preset:null};savePaper({trades:[...account.trades,t]});quotes[x.market]=q;paperMsg(x.market+' '+q.entry_status+' 모의매수 완료 (시장 참고가 가정).');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function paperSell(t){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('매도 참고가 조회 중...');
 try{let q=(await quote([t.market]))[t.market];if(!q)throw Error('가격 확인 불가');let proceeds=t.qty*q.price*(1-FEE),pnl=proceeds-t.amount;let closed={...t,closedAt:q.observed_ms,sell:q.price,proceeds,pnl,netPct:100*pnl/t.amount};savePaper({trades:account.trades.map(x=>x.id===t.id?closed:x)});quotes[t.market]=q;paperMsg(t.market+' 모의매도 완료. 순손익 '+num(pnl)+'원');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function refreshPaper(){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('보유 종목 가격 조회 중...');
 try{let m=paperStats().open.map(x=>x.market);if(m.length)quotes=await quote(m);paperMsg('가격 갱신 완료. 가격 확인 불가 종목은 — 표시.')}catch(e){quotes={};paperMsg(e.message)}finally{paperBusy=false;renderPaper()}
}
function renderPaper(){
 let s=paperStats();$('paperSummary').textContent='가상 원금 1,000,000원 · 잔액 '+num(s.cash)+'원 · 실현손익 '+num(s.real)+'원';$('paperWin').textContent='보유 '+s.open.length+' · 청산 '+s.closed.length+' · 수익 '+s.wins+' · 손실 '+s.losses+' · 승률 '+(s.closed.length?(100*s.wins/s.closed.length).toFixed(2)+'%':'—');
 $('refreshPaper').disabled=paperBusy||!storageOK;
 let cards=s.open.map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 모의 보유'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매수가',num(t.buy)+'원');line(c,'투자금(수수료 포함)',num(t.amount)+'원');let q=quotes[t.market];let val=q?t.qty*q.price*(1-FEE)-t.amount:null;line(c,'조회 가격',q?num(q.price)+'원':'—');line(c,'가격 조회',q?paperTime(q.observed_ms):'—');line(c,'평가 순손익',val===null?'—':num(val)+'원 / '+pct(100*val/t.amount),val===null?'muted':val>=0?'green':'red');line(c,'7일 보유 만료',paperTime(t.deadline),Date.now()>=t.deadline?'red':'muted');if(Date.now()>=t.deadline)c.append(node('p','7일 경과: 직접 모의매도 필요','red'));let b=node('button','모의매도');b.disabled=paperBusy||!storageOK;b.onclick=()=>paperSell(t);c.append(b);return c});
 $('paperOpen').replaceChildren(...cards);if(!cards.length)$('paperOpen').textContent='보유 종목 없음';
 let hist=s.closed.slice().reverse().slice(0,100).map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 청산 완료'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 / 매도',num(t.buy)+' / '+num(t.sell));line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매도 시각',paperTime(t.closedAt));line(c,'순손익',num(t.pnl)+'원 / '+pct(t.netPct),t.pnl>=0?'green':'red');return c});$('paperClosed').replaceChildren(...hist);if(!hist.length)$('paperClosed').textContent='청산 기록 없음';if(!storageOK)paperMsg('브라우저 저장소 사용 불가 또는 기록 손상. 모의매매를 중단합니다.');
}
$('refreshPaper').onclick=refreshPaper;
$('paperCSV').onclick=()=>{let keys=['market','strategy','timeframe','preset','entryStatus','minDrawdownPct','minDownDays','signalTime','boughtAt','amount','buy','qty','deadline','closedAt','sell','proceeds','pnl','netPct'];let esc=v=>'"'+String(v??'').replaceAll('"','""')+'"';let rows=[keys.join(','),...account.trades.map(x=>keys.map(k=>esc(x[k])).join(','))];let url=URL.createObjectURL(new Blob(['\ufeff'+rows.join('\r\n')],{type:'text/csv;charset=utf-8'}));let a=document.createElement('a');a.href=url;a.download='BITHUMB_SWING_V2_5_TEST_PAPER_'+new Date().toISOString().slice(0,10)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
renderPaper();

poll();setInterval(poll,2000);
</script></html>"""
HTML26=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BITHUMB SWING V2.6 TEST</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1000px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:10px;margin:4px;border-radius:7px;text-decoration:none}input{background:#10151d}button:disabled{opacity:.5}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:10px}.coin{background:#111923;border:1px solid #3b4d60;border-radius:12px;padding:12px}.coin h3{margin:0 0 8px}.line{display:flex;justify-content:space-between;gap:10px;margin:6px 0}.green{color:#72ddb0}.red{color:#ff9b9b}.muted{color:#a9bac9}h1{font-size:23px;margin-bottom:4px}</style><h1>BITHUMB SWING V2.6 TEST</h1><a href="/live143">기존 V1.4.3 보존판</a><section><h2>매수 후보 검색</h2><p><label>전략 <select id="strategy"><option value="reverse">저점 반등형 (기존)</option><option value="trend">상승 눌림형 (실험)</option></select></label></p><div id="reverseRules"><p>20일 하락 추세 + 선택한 하락폭·하락일 수 + 전일 거래량 2배 이상 + D-3 대비 2배 이상 · RSI 제외</p><small>최근 20일과 오늘의 일별 고가가 시가 또는 전일 종가 대비 한 번이라도 30% 이상 상승하면 제외. 과거 급등 이력 부족 시 제외. 강화 일봉 조건 충족 WATCH. 가장 최근 완료 1시간봉의 SMA5 상향 돌파만 BUY 후보. 미완료 봉 제외. BUY/WATCH 모의매수 모두 검색된 1시간 동안 유효. WATCH는 5이평 상향 돌파 미확인 상태의 비교 실험이며 BUY로 변경하지 않음. 검증 전 실전 매수 권장 아님. 검색 시점의 가격은 실제 체결가가 아니며 당일 시가 진입 백테스트와 다를 수 있음. 최고가 매도 가정 결과는 실제 승률이 아님. 자동 주문 없음. 별도 매도·손절 신호 미검증. 기존 보유 종목은 직접 제외.</small></div><div id="trendRules" hidden><p id="newRules"><span id="directRuleText">신규 20/30: 완료된 240분봉 30개 중 종가 &lt; 시가인 봉 20개 이상.</span> 연속 불필요. 상승폭·눌림폭·4시간 거래량 필터 없음. BUY는 기존 1시간봉 양봉·직전 고가 돌파·직전 3시간 평균 거래량 1.2배 이상. 실제 거래소 240분봉 경계 사용. 15일 진입 검증, 다음 시간봉 시가 진입 후 1·3일 평가. 누락 제외.</p><div id="oldTrendRules"><p>선택 시간봉: 눌림 전 20개 봉 상승폭 10% 이상 · 연속 음봉 2~4개 · 종가 눌림 3~10% (저가 최대 10%) · 눌림 평균 거래량 감소.</p><small>4시간봉은 KST 00/04/08/12/16/20시, 1시간봉으로 합성(누락 봉 제외). BUY: 완료 1시간봉 양봉·직전 고가 돌파·직전 3시간 평균 거래량 1.2배 이상, 이전 고점 미초과. WATCH는 반등 미확인. 일봉 추세 필터는 아직 적용하지 않음. 15일 백테스트는 같은 조건의 BUY만 다음 시간봉 시가 진입, 1·3일 수익률, 동일 코인 72시간 중복 제외. 현재 상장종목 기준, 누락은 제외. 실제 주문 없음.</small></div><p>시간봉 <select id="trendTf"><option value="G20ALL" selected>BTC 상승장 필터 전후 비교 (백테스트)</option><option value="P20ALL">4시간봉 · 상승폭 3/5/10% + 눌림 최종 비교 (백테스트)</option><option value="U20ALL">4시간봉 · 20개 중 양봉 10~20개 동시 비교 (백테스트)</option><option value="T42ALL">4시간봉 · 42개 중 30~36개 동시 비교 (백테스트)</option><option value="T430">4시간봉 · 30개 중 20개 이상 음봉 (신규)</option><option value="T415">4시간봉 · 20개 중 15개 이상 음봉</option><option value="T4">4시간봉 (기존)</option><option value="T1">1시간봉</option></select><button id="back">15일 백테스트</button></p></div><p><label id="presetLabel" for="preset">검색 조건</label><select id="preset"><option value="A">A · 30% / 15일</option><option value="B">B · 35% / 14일</option><option value="C">C · 40% / 15일</option><option value="D" selected>D · 35% / 13일 (완화 테스트)</option></select><button id="go">전체 코인 검색 시작</button></p><p id="applied" class="muted">조건을 선택하고 검색해 주세요.</p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section><h2>모의매매 계좌</h2><small>가상 원금 100만원, 1회 투자금 변경 가능. 현재가 조회 후 전량 가상 체결(편도 0.04%). 직접 매수/매도, 7일 경과 표시만 제공. 실제 주문 없음. 기록은 이 브라우저에만 저장, 기기·브라우저 변경/데이터 삭제 시 유실될 수 있음. CSV 백업 권장.</small><p>1회 투자금(원) <input id="paperAmount" type="number" min="5000" step="1000" value="100000" style="width:105px"><button id="refreshPaper">보유 가격 갱신</button><button id="paperCSV">모의매매 CSV</button></p><p id="paperSummary"></p><p id="paperWin"></p><p id="paperMsg" class="muted"></p><h3>보유 종목</h3><div class="cards" id="paperOpen"></div><details><summary>청산 기록 (최근 100건)</summary><div class="cards" id="paperClosed"></div></details></section><section id="out" hidden><h2 id="headline"></h2><p id="audit" class="muted"></p><p id="notice" class="muted"></p><div id="compare23" hidden></div><a id="featuresCSV" href="/csv/features?mode=live14">결과 CSV</a><a id="summaryCSV" href="/csv/summary?mode=live14">요약 CSV</a><a id="coverageCSV" href="/csv/coverage?mode=live14">수집 상태 CSV</a><a id="errorsCSV" href="/csv/errors?mode=live14">오류 CSV</a><p><input id="query" placeholder="코인 검색" style="width:105px"><select id="sort"><option value="market">종목명 순</option><option value="volume">거래량 증가율 순</option><option value="depth">고점 대비 하락폭 순</option><option value="gap">당일 시가 대비 변동률 순</option></select></p><p id="count"></p><div class="cards" id="cards"></div><button id="more">더 보기</button></section><script>
const $=id=>document.getElementById(id);let result=null,loading=false,limit=5,loadedScan=null;const names={A:"A · 30% / 15일",B:"B · 35% / 14일",C:"C · 40% / 15일",D:"D · 35% / 13일",T4:"상승 눌림 · 4시간봉",T1:"상승 눌림 · 1시간봉",T415:"4시간봉 음봉 15/20",T430:"4시간봉 음봉 20/30",T42ALL:"42봉 · 음봉 30~36개 비교",U20ALL:"20봉 · 양봉 10~20개 비교",P20ALL:"20봉 상승폭 + 눌림 최종 비교",G20ALL:"BTC 상승장 필터 적용 전후"};
function num(x){return typeof x==='number'?x.toLocaleString('ko-KR',{maximumSignificantDigits:8}):'—'}
function pct(x){return typeof x==='number'?(x>0?'+':'')+x.toFixed(2)+'%':'—'}
function node(tag,txt,cls){let e=document.createElement(tag);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
function line(card,k,v,cls){let r=node('div',null,'line');r.append(node('span',k,'muted'),node('strong',v,cls));card.append(r)}
function render(){if(!result)return;if(result.summary[0].strategy==="trend"){renderTrend();return;}let q=$('query').value.trim().toUpperCase(),sort=$('sort').value;let a=result.rank.filter(x=>x.market.includes(q));let key={volume:'volume_increase_pct',depth:'peak_to_last_pct',gap:'change_from_open_pct'}[sort];if(key)a.sort((x,y)=>sort==='depth'?(x[key]??Infinity)-(y[key]??Infinity):(y[key]??-Infinity)-(x[key]??-Infinity));else a.sort((x,y)=>x.market.localeCompare(y.market));$('count').textContent=a.length+'종목 · '+Math.min(limit,a.length)+'개 표시';a.sort((x,y)=>Number(y.status==="BUY")-Number(x.status==="BUY"));let cards=a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+(x.status==='BUY'?'BUY 후보':'WATCH'),x.status==='BUY'?'green':'muted'));line(c,'적용 검색식',names[x.preset]||x.preset);line(c,'검색 시 참고가',num(x.reference_price)+'원');line(c,'당일 시가',num(x.day_open)+'원');line(c,'시가 대비',pct(x.change_from_open_pct),x.change_from_open_pct>=0?'green':'red');line(c,'당일 최고 상승률',pct(x.day_high_pct));line(c,'20일+오늘 최대 일중 급등',pct(x.lookback_max_spike_pct));line(c,'최대 급등 날짜',x.lookback_max_spike_date||'—');line(c,'20일 실제 하락일',x.down_days+'/20일');line(c,'20일 종가 변화',pct(x.trend_net_pct),'red');line(c,'고점 대비 전일 종가',pct(x.peak_to_last_pct),'red');line(c,'거래량 배수',num(x.vol_ratio_d3)+' → '+num(x.vol_ratio_d1)+'배','green');line(c,'거래량 배수 증가율',pct(x.volume_increase_pct));line(c,'참고가 기준 본전',num(x.breakeven_price)+'원');line(c,'신호 확정(KST)',x.signal_time);line(c,'가격 조회(KST)',x.observed_at);line(c,'1시간봉 5이평',num(x.hour_ma5));line(c,'1시간봉 확인',x.hour_valid?x.hour_signal_time:'데이터 부족');line(c,'7일째 일봉 날짜',x.planned_last_day);let b=node('button',paperStats().open.some(t=>t.market===x.market)?'모의 보유 중':x.status==='WATCH'?'WATCH 모의매수':'BUY 모의매수');b.disabled=!['BUY','WATCH'].includes(x.status)||paperBusy||!storageOK||paperStats().open.some(t=>t.market===x.market);b.onclick=()=>paperBuy(x);c.append(b);return c});$('cards').replaceChildren(...cards);$('more').hidden=a.length<=limit}
function show(){let x=result.summary[0];$('compare23').hidden=true;if(x.strategy==="trend"){showTrend(x);return;}$('applied').textContent='결과 적용 조건: '+names[x.preset];$('headline').textContent='BUY '+x.buys+'개 · WATCH '+x.watches+'개 · 20일+오늘 급등 제외 '+x.spike_excluded+'개';$('audit').textContent='신호 확정 '+x.signal_time+' KST · 검색 완료 '+x.finished_at+' KST · 분석 '+x.valid+'/'+x.universe+' · 이력 부족 '+x.history_missing+' · 현재가 없음 '+x.no_quote+' · 급등 이력 미확인 '+x.spike_history_missing;$('notice').textContent='수수료 편도 0.04% 적용. 본전가는 참고가 기준 이론값(호가단위·슬리피지 미반영). 직접 매매 전 현재가와 보유 여부 확인.';limit=5;render()}

let resultMode=null;
function syncMode(){let trend=$('strategy').value==='trend';let tf=$('trendTf').value;let direct=['T415','T430','T42ALL','U20ALL','P20ALL','G20ALL'].includes(tf);$('newRules').hidden=!direct;$('oldTrendRules').hidden=direct;$('directRuleText').textContent=tf==='G20ALL'?'V2.5 조건 유지 (양봉 10/20 이상, 상승폭 3/5/10%, 연속 음봉 1~3개, 눌림 2~10%). BTC 완료 4시간봉 종가 > SMA20 및 SMA20 > 5봉 전 SMA20일 때만 진입하는 필터와 무필터를 각각 비교. BTC는 시장의 대리 지표.':tf==='P20ALL'?'최종 비교: 완료 20봉 중 양봉 10개 이상, 순상승폭 최소 3/5/10%, 마지막 연속 음봉 1~3개, 음봉 직전까지의 고점 대비 종가 눌림 2~10%.':tf==='U20ALL'?'20개 완료 4시간봉 중 종가 > 시가인 양봉 최소 10~20개를 각각 비교 (백테스트 전용).':tf==='T42ALL'?'42개 완료 4시간봉 중 음봉 최소 30,31,32,33,34,35,36개를 각각 비교 (백테스트 전용).':tf==='T430'?'신규 20/30: 완료된 240분봉 30개 중 종가 < 시가인 봉 20개 이상.':'15/20: 완료된 240분봉 20개 중 종가 < 시가인 봉 15개 이상.';$('reverseRules').hidden=trend;$('trendRules').hidden=!trend;$('preset').hidden=trend;$('presetLabel').hidden=trend;}
function clearResult(){result=null;loadedScan=null;resultMode=null;$('out').hidden=true;}
$('strategy').onchange=()=>{syncMode();clearResult();$('applied').textContent='전략을 선택하고 새로 검색해 주세요.'};
$('trendTf').onchange=()=>{syncMode();clearResult();$('applied').textContent='시간봉 변경: 다시 검색해 주세요.'};
$('preset').onchange=()=>{clearResult();$('applied').textContent='조건 변경: 다시 검색해 주세요.'};
$('query').oninput=$('sort').onchange=()=>{limit=5;render()};$('more').onclick=()=>{limit+=5;render()};
async function begin20(back){try{$('go').disabled=true;$('back').disabled=true;$('strategy').disabled=true;$('preset').disabled=true;$('trendTf').disabled=true;let trend=$('strategy').value==='trend';if(trend&&['T42ALL','U20ALL','P20ALL','G20ALL'].includes($('trendTf').value))back=true;let url=trend?'/trend20/start?preset='+encodeURIComponent($('trendTf').value)+'&back='+(back?'1':'0'):'/live14/start?preset='+encodeURIComponent($('preset').value);let r=await fetch(url,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 검색 실행 중');clearResult();await poll()}catch(e){$('phase').textContent=e.message;for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=false}}
$('go').onclick=()=>begin20(false);$('back').onclick=()=>begin20(true);
function showTrend(x){
 if(['T42ALL','U20ALL','P20ALL','G20ALL'].includes(x.preset)){show23(x);return}
 $('applied').textContent='결과 적용 조건: '+names[x.preset];
 $('headline').textContent=resultMode==='trendback20'?'15일 과거 BUY 진입 '+x.matches+'건 · 3일 평균 '+pct(x.day3_mean_pct):'BUY '+x.buys+'개 · WATCH '+x.watches+'개';
 $('audit').textContent='분석 '+x.valid+'/'+x.universe+' · 이력 부족 종목 '+x.history_missing+' · 검색 완료 '+x.finished_at;
 $('notice').textContent=resultMode==='trendback20'?'1일 평균 '+pct(x.day1_mean_pct)+' · 3일 최고 순수익 5% 도달률 '+pct(x.day3_hit5_pct)+' · 평가 누락 '+x.outcome_missing+'건 · 누락 시간 '+x.missing_hours+' · 가격 범위 제외 '+x.price_skipped+' · 매수 후 72시간 중복 제외':'상승 눌림 실험 조건 · 편도 0.04% · BUY/WATCH 모의매수만 가능';
 limit=5;render();
}
function show23(x){
 $('applied').textContent=names[x.preset];$('headline').textContent=x.preset==='G20ALL'?'BTC 상승장 필터 적용 전후 6개 비교군':x.preset==='P20ALL'?'상승폭 3/5/10% + 눌림 최종 비교':x.preset==='U20ALL'?'20봉 · 양봉 11개 기준 동시 비교':'42봉 · 7개 기준 동시 비교';
 $('audit').textContent=x.first_entry+' ~ '+x.last_entry_exclusive+' (종료 시각 미포함) · 전체 '+x.universe+' · 수집 성공 '+x.market_success+(x.preset==='G20ALL'?' · BTC 상승 '+x.btc_up_hours+'/360시간 · 판정불가 '+x.btc_unknown_hours+'시간':'');
 $('notice').textContent='각 기준별 72시간 중복 제외 · 1/3일 종료 수익 및 최고가 기회 (실현 수익 아님) · 동일 표본이 여러 그룹에 포함 가능 · 수수료 왕복 0.08%';
 let panel=$('compare23');panel.hidden=false;panel.replaceChildren(...result.summary.map(r=>{let c=node('article',null,'coin');c.append(node('h3',(r.preset==='G20ALL'?(r.market_filter==='btc_up'?'BTC 상승 필터':'필터 없음')+' · 최소 상승 '+r.minimum_gain_pct+'%':r.preset==='P20ALL'?'최소 상승 '+r.minimum_gain_pct+'%':'최소 '+(r.minimum_up_bars??r.minimum_down_bars)+'/'+r.total_bars)+' · '+r.matches+'건'));line(c,'1일 / 3일 종료 평균',pct(r.day1_mean_pct)+' / '+pct(r.day3_mean_pct));line(c,'1일 / 3일 중앙값',pct(r.day1_median_pct)+' / '+pct(r.day3_median_pct));line(c,'1일 / 3일 종료 승률',pct(r.day1_win_pct)+' / '+pct(r.day3_win_pct));line(c,'3일 최고 평균',pct(r.day3_peak_mean_pct));line(c,'5% / 10% 도달률',pct(r.day3_hit5_pct)+' / '+pct(r.day3_hit10_pct));line(c,'-10% 도달률',pct(r.day3_loss10_pct));line(c,'판정 가능 / 평가 누락',pct(r.valid_pct)+' / '+r.outcome_missing+'건');return c}));limit=5;render();
}
function renderTrend(){
 let q=$('query').value.trim().toUpperCase();let a=result.rank.filter(x=>x.market.includes(q));
 if(resultMode!=='trendback20'){let sort=$('sort').value;if(sort==='volume')a.sort((x,y)=>y.confirm_volume_ratio-x.confirm_volume_ratio);else if(sort==='depth')a.sort((x,y)=>y.pullback_pct-x.pullback_pct);else if(sort==='market')a.sort((x,y)=>x.market.localeCompare(y.market));a.sort((x,y)=>Number(y.status==='BUY')-Number(x.status==='BUY'))}
 $('count').textContent=a.length+'건 · '+Math.min(limit,a.length)+'개 표시';$('more').hidden=a.length<=limit;
 $('cards').replaceChildren(...a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+x.status,x.status==='BUY'?'green':'muted'));line(c,'전략',names[x.preset]);if(['T415','T430','T42ALL','U20ALL','P20ALL','G20ALL'].includes(x.preset)){if(x.preset==='T42ALL')line(c,'비교군 최소 음봉',x.minimum_down_bars+'/42');if(x.preset==='G20ALL'){line(c,'시장 필터',x.market_filter==='btc_up'?'BTC 상승만':'없음');line(c,'진입 당시 BTC',x.btc_regime);line(c,'BTC SMA20',num(x.btc_sma20));}if(['P20ALL','G20ALL'].includes(x.preset)){line(c,'비교군 최소 상승폭',x.minimum_gain_pct+'%');line(c,'실제 양봉 수',x.up_bars+'/20개');line(c,'최근 연속 음봉',x.pullback_bars+'개');}if(x.preset==='U20ALL'){line(c,'비교군 최소 양봉',x.minimum_up_bars+'/20');line(c,'실제 양봉 수',x.up_bars+'/20개');line(c,'보합 봉',x.flat_bars+'개');}line(c,'음봉 수',x.down_bars+'/'+x.total_bars+'개');line(c,x.total_bars+'봉 변화',pct(x.window_change_pct));line(c,'고점 대비 변화',pct(-x.pullback_pct));line(c,'마지막 4h 거래량 / 이전 '+(x.total_bars-1)+'봉',num(x.last_4h_volume_ratio)+'배');line(c,'4h 조건 확정',x.setup_end);}else{line(c,'이전 20봉 상승',pct(x.prior_gain_pct));line(c,'연속 음봉',x.pullback_bars+'개');line(c,'눌림폭',pct(-x.pullback_pct));line(c,'눌림 거래량 / 이전 평균',num(x.pullback_volume_ratio)+'배');}line(c,'반등 시간봉 거래량',num(x.confirm_volume_ratio)+'배');line(c,'신호 확정(KST)',x.signal_time);
 if(resultMode==='trendback20'){line(c,'진입가',num(x.buy_price)+'원');line(c,'1일 종료 / 최고',pct(x.day1_close_pct)+' / '+pct(x.day1_peak_pct));line(c,'3일 종료 / 최고',pct(x.day3_close_pct)+' / '+pct(x.day3_peak_pct));line(c,'3일 최저',pct(x.day3_low_pct));}
 else{line(c,'검색 참고가',num(x.reference_price)+'원');line(c,'이전 고점',num(x.prior_peak)+'원');line(c,'눌림 저점',num(x.pullback_low)+'원');let held=paperStats().open.some(t=>t.market===x.market),b=node('button',held?'모의 보유 중':x.status+' 모의매수');b.disabled=held||paperBusy||!storageOK;b.onclick=()=>paperBuy(x);c.append(b)}return c}));
}
async function poll(){try{
 let s=await(await fetch('/status',{cache:'no-store'})).json();let running=s.status==='running';
 for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=running;
 if(!['live14','trend20','trendback20'].includes(s.mode)){$('phase').textContent=running?'다른 연구 실행 중':'대기';clearResult();return}
 $('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');
 if(running){clearResult();$('applied').textContent='검색 중: '+names[s.preset];return}
 if(s.status==='done'&&loadedScan!==s.scan_id&&!loading){
  // Do not overwrite a user's deliberate strategy selection after a previous completed scan.
  if((s.mode==='live14')!==($('strategy').value==='reverse'))return;
  if(s.mode==='live14'&&$('preset').value!==s.preset)return;
  if(s.mode!=='live14'&&$('trendTf').value!==s.preset)return;
  loading=true;try{let d=await(await fetch('/results',{cache:'no-store'})).json();if(!d.summary[0]||d.summary[0].scan_id!==s.scan_id)return;result=d;loadedScan=s.scan_id;resultMode=s.mode;if(s.mode==='live14')$('preset').value=s.preset;
  for(let k of ['features','summary','coverage','errors'])$(k+'CSV').href='/csv/'+k+'?mode='+s.mode;
  $('out').hidden=false;show()}finally{loading=false}
 }
 }catch(e){$('phase').textContent='연결 확인: '+e.message}}
syncMode();

const FEE=.0004,STORE='reverse-swing-paper-v1',INITIAL=1000000;
let account={trades:[]},storageOK=true,paperBusy=false,quotes={};
try{let raw=localStorage.getItem(STORE);if(raw){let a=JSON.parse(raw);if(!Array.isArray(a.trades))throw Error('invalid data');account=a}}catch(e){storageOK=false}
function savePaper(next){localStorage.setItem(STORE,JSON.stringify(next));account=next}
function paperStats(){let closed=account.trades.filter(x=>x.closedAt),open=account.trades.filter(x=>!x.closedAt),real=closed.reduce((s,x)=>s+x.pnl,0),spent=open.reduce((s,x)=>s+x.amount,0);return {closed,open,real,cash:INITIAL+real-spent,wins:closed.filter(x=>x.pnl>0).length,losses:closed.filter(x=>x.pnl<0).length}}
function paperMsg(s){$('paperMsg').textContent=s}
function paperTime(t){return new Date(t).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})}
async function quote(markets){let all={};for(let i=0;i<markets.length;i+=40){let r=await fetch('/paper/quotes?markets='+encodeURIComponent(markets.slice(i,i+40).join(',')),{cache:'no-store'});if(!r.ok)throw Error('현재가 조회 실패');let d=await r.json();Object.assign(all,d.quotes)}return all}
async function paperBuy(x){
 if(paperBusy||!storageOK||!result||!['BUY','WATCH'].includes(x.status))return;
 let a=Number($('paperAmount').value),s=paperStats();
 if(!Number.isFinite(a)||a<5000||a>s.cash){paperMsg('가상 잔액 내에서 5,000원 이상 입력해 주세요.');return}
 if(s.open.some(t=>t.market===x.market)){paperMsg('이미 모의 보유 중인 종목입니다.');return}
 if(s.closed.some(t=>t.market===x.market&&t.signalTime===x.signal_time)){paperMsg('같은 신호에서 이미 매매한 종목입니다.');return}
 paperBusy=true;renderPaper();paperMsg('새로운 현재가 조회 중...');
 try{let r=await fetch((x.strategy==='trend'?'/trend20/entry':'/paper/entry')+'?market='+encodeURIComponent(x.market)+'&signal='+encodeURIComponent(x.signal_time)+'&hour='+encodeURIComponent(x.hour_signal_time)+'&preset='+encodeURIComponent(x.preset)+'&scan_id='+encodeURIComponent(result.summary[0].scan_id)+'&entry_status='+encodeURIComponent(x.status),{cache:'no-store'});if(!r.ok)throw Error('신호 만료, 진입 범위 제외 또는 가격 조회 실패. 다시 검색해 주세요.');let q=await r.json();if(q.entry_status!==x.status)throw Error('검색 판정 변경. 다시 검색해 주세요.');let p=q.price;if(!Number.isFinite(p)||p<=0)throw Error('유효한 현재가 없음');
 let t={id:Date.now()+'-'+x.market,market:x.market,signalTime:x.signal_time,boughtAt:q.observed_ms,amount:a,buy:p,qty:a/(1+FEE)/p,deadline:q.observed_ms+7*86400000,signalDeadline:q.signal_deadline_ms,preset:q.preset,minDrawdownPct:q.min_drawdown_pct,minDownDays:q.min_down_days,entryStatus:q.entry_status,strategy:x.strategy||'reverse',timeframe:x.strategy==='trend'?x.preset:null};savePaper({trades:[...account.trades,t]});quotes[x.market]=q;paperMsg(x.market+' '+q.entry_status+' 모의매수 완료 (시장 참고가 가정).');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function paperSell(t){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('매도 참고가 조회 중...');
 try{let q=(await quote([t.market]))[t.market];if(!q)throw Error('가격 확인 불가');let proceeds=t.qty*q.price*(1-FEE),pnl=proceeds-t.amount;let closed={...t,closedAt:q.observed_ms,sell:q.price,proceeds,pnl,netPct:100*pnl/t.amount};savePaper({trades:account.trades.map(x=>x.id===t.id?closed:x)});quotes[t.market]=q;paperMsg(t.market+' 모의매도 완료. 순손익 '+num(pnl)+'원');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function refreshPaper(){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('보유 종목 가격 조회 중...');
 try{let m=paperStats().open.map(x=>x.market);if(m.length)quotes=await quote(m);paperMsg('가격 갱신 완료. 가격 확인 불가 종목은 — 표시.')}catch(e){quotes={};paperMsg(e.message)}finally{paperBusy=false;renderPaper()}
}
function renderPaper(){
 let s=paperStats();$('paperSummary').textContent='가상 원금 1,000,000원 · 잔액 '+num(s.cash)+'원 · 실현손익 '+num(s.real)+'원';$('paperWin').textContent='보유 '+s.open.length+' · 청산 '+s.closed.length+' · 수익 '+s.wins+' · 손실 '+s.losses+' · 승률 '+(s.closed.length?(100*s.wins/s.closed.length).toFixed(2)+'%':'—');
 $('refreshPaper').disabled=paperBusy||!storageOK;
 let cards=s.open.map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 모의 보유'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매수가',num(t.buy)+'원');line(c,'투자금(수수료 포함)',num(t.amount)+'원');let q=quotes[t.market];let val=q?t.qty*q.price*(1-FEE)-t.amount:null;line(c,'조회 가격',q?num(q.price)+'원':'—');line(c,'가격 조회',q?paperTime(q.observed_ms):'—');line(c,'평가 순손익',val===null?'—':num(val)+'원 / '+pct(100*val/t.amount),val===null?'muted':val>=0?'green':'red');line(c,'7일 보유 만료',paperTime(t.deadline),Date.now()>=t.deadline?'red':'muted');if(Date.now()>=t.deadline)c.append(node('p','7일 경과: 직접 모의매도 필요','red'));let b=node('button','모의매도');b.disabled=paperBusy||!storageOK;b.onclick=()=>paperSell(t);c.append(b);return c});
 $('paperOpen').replaceChildren(...cards);if(!cards.length)$('paperOpen').textContent='보유 종목 없음';
 let hist=s.closed.slice().reverse().slice(0,100).map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 청산 완료'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 / 매도',num(t.buy)+' / '+num(t.sell));line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매도 시각',paperTime(t.closedAt));line(c,'순손익',num(t.pnl)+'원 / '+pct(t.netPct),t.pnl>=0?'green':'red');return c});$('paperClosed').replaceChildren(...hist);if(!hist.length)$('paperClosed').textContent='청산 기록 없음';if(!storageOK)paperMsg('브라우저 저장소 사용 불가 또는 기록 손상. 모의매매를 중단합니다.');
}
$('refreshPaper').onclick=refreshPaper;
$('paperCSV').onclick=()=>{let keys=['market','strategy','timeframe','preset','entryStatus','minDrawdownPct','minDownDays','signalTime','boughtAt','amount','buy','qty','deadline','closedAt','sell','proceeds','pnl','netPct'];let esc=v=>'"'+String(v??'').replaceAll('"','""')+'"';let rows=[keys.join(','),...account.trades.map(x=>keys.map(k=>esc(x[k])).join(','))];let url=URL.createObjectURL(new Blob(['\ufeff'+rows.join('\r\n')],{type:'text/csv;charset=utf-8'}));let a=document.createElement('a');a.href=url;a.download='BITHUMB_SWING_V2_6_TEST_PAPER_'+new Date().toISOString().slice(0,10)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
renderPaper();

poll();setInterval(poll,2000);
</script></html>"""
HTML27=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BITHUMB SWING V2.7 TEST</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1000px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}button,a,select,input{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:10px;margin:4px;border-radius:7px;text-decoration:none}input{background:#10151d}button:disabled{opacity:.5}small{color:#b4c4d1;line-height:1.7}progress{width:100%}pre{white-space:pre-wrap}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:10px}.coin{background:#111923;border:1px solid #3b4d60;border-radius:12px;padding:12px}.coin h3{margin:0 0 8px}.line{display:flex;justify-content:space-between;gap:10px;margin:6px 0}.green{color:#72ddb0}.red{color:#ff9b9b}.muted{color:#a9bac9}h1{font-size:23px;margin-bottom:4px}</style><h1>BITHUMB SWING V2.7 TEST</h1><a href="/live143">기존 V1.4.3 보존판</a><section><h2>매수 후보 검색</h2><p><label>전략 <select id="strategy"><option value="reverse">저점 반등형 (기존)</option><option value="trend">상승 눌림형 (실험)</option></select></label></p><div id="reverseRules"><p>20일 하락 추세 + 선택한 하락폭·하락일 수 + 전일 거래량 2배 이상 + D-3 대비 2배 이상 · RSI 제외</p><small>최근 20일과 오늘의 일별 고가가 시가 또는 전일 종가 대비 한 번이라도 30% 이상 상승하면 제외. 과거 급등 이력 부족 시 제외. 강화 일봉 조건 충족 WATCH. 가장 최근 완료 1시간봉의 SMA5 상향 돌파만 BUY 후보. 미완료 봉 제외. BUY/WATCH 모의매수 모두 검색된 1시간 동안 유효. WATCH는 5이평 상향 돌파 미확인 상태의 비교 실험이며 BUY로 변경하지 않음. 검증 전 실전 매수 권장 아님. 검색 시점의 가격은 실제 체결가가 아니며 당일 시가 진입 백테스트와 다를 수 있음. 최고가 매도 가정 결과는 실제 승률이 아님. 자동 주문 없음. 별도 매도·손절 신호 미검증. 기존 보유 종목은 직접 제외.</small></div><div id="trendRules" hidden><p id="newRules"><span id="directRuleText">신규 20/30: 완료된 240분봉 30개 중 종가 &lt; 시가인 봉 20개 이상.</span> 연속 불필요. 상승폭·눌림폭·4시간 거래량 필터 없음. BUY는 기존 1시간봉 양봉·직전 고가 돌파·직전 3시간 평균 거래량 1.2배 이상. 실제 거래소 240분봉 경계 사용. 15일 진입 검증, 다음 시간봉 시가 진입 후 1·3일 평가. 누락 제외.</p><div id="oldTrendRules"><p>선택 시간봉: 눌림 전 20개 봉 상승폭 10% 이상 · 연속 음봉 2~4개 · 종가 눌림 3~10% (저가 최대 10%) · 눌림 평균 거래량 감소.</p><small>4시간봉은 KST 00/04/08/12/16/20시, 1시간봉으로 합성(누락 봉 제외). BUY: 완료 1시간봉 양봉·직전 고가 돌파·직전 3시간 평균 거래량 1.2배 이상, 이전 고점 미초과. WATCH는 반등 미확인. 일봉 추세 필터는 아직 적용하지 않음. 15일 백테스트는 같은 조건의 BUY만 다음 시간봉 시가 진입, 1·3일 수익률, 동일 코인 72시간 중복 제외. 현재 상장종목 기준, 누락은 제외. 실제 주문 없음.</small></div><p>시간봉 <select id="trendTf"><option value="R415ALL" selected>4시간봉 15/20 · 하락폭·거래량 비교 (백테스트)</option><option value="G20ALL">BTC 상승장 필터 전후 비교 (백테스트)</option><option value="P20ALL">4시간봉 · 상승폭 3/5/10% + 눌림 최종 비교 (백테스트)</option><option value="U20ALL">4시간봉 · 20개 중 양봉 10~20개 동시 비교 (백테스트)</option><option value="T42ALL">4시간봉 · 42개 중 30~36개 동시 비교 (백테스트)</option><option value="T430">4시간봉 · 30개 중 20개 이상 음봉 (신규)</option><option value="T415">4시간봉 · 20개 중 15개 이상 음봉</option><option value="T4">4시간봉 (기존)</option><option value="T1">1시간봉</option></select><button id="back">15일 백테스트</button></p></div><p><label id="presetLabel" for="preset">검색 조건</label><select id="preset"><option value="A">A · 30% / 15일</option><option value="B">B · 35% / 14일</option><option value="C">C · 40% / 15일</option><option value="D" selected>D · 35% / 13일 (완화 테스트)</option></select><button id="go">전체 코인 검색 시작</button></p><p id="applied" class="muted">조건을 선택하고 검색해 주세요.</p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre></section><section><h2>모의매매 계좌</h2><small>가상 원금 100만원, 1회 투자금 변경 가능. 현재가 조회 후 전량 가상 체결(편도 0.04%). 직접 매수/매도, 7일 경과 표시만 제공. 실제 주문 없음. 기록은 이 브라우저에만 저장, 기기·브라우저 변경/데이터 삭제 시 유실될 수 있음. CSV 백업 권장.</small><p>1회 투자금(원) <input id="paperAmount" type="number" min="5000" step="1000" value="100000" style="width:105px"><button id="refreshPaper">보유 가격 갱신</button><button id="paperCSV">모의매매 CSV</button></p><p id="paperSummary"></p><p id="paperWin"></p><p id="paperMsg" class="muted"></p><h3>보유 종목</h3><div class="cards" id="paperOpen"></div><details><summary>청산 기록 (최근 100건)</summary><div class="cards" id="paperClosed"></div></details></section><section id="out" hidden><h2 id="headline"></h2><p id="audit" class="muted"></p><p id="notice" class="muted"></p><div id="compare23" hidden></div><a id="featuresCSV" href="/csv/features?mode=live14">결과 CSV</a><a id="summaryCSV" href="/csv/summary?mode=live14">요약 CSV</a><a id="coverageCSV" href="/csv/coverage?mode=live14">수집 상태 CSV</a><a id="errorsCSV" href="/csv/errors?mode=live14">오류 CSV</a><p><input id="query" placeholder="코인 검색" style="width:105px"><select id="sort"><option value="market">종목명 순</option><option value="volume">거래량 증가율 순</option><option value="depth">고점 대비 하락폭 순</option><option value="gap">당일 시가 대비 변동률 순</option></select></p><p id="count"></p><div class="cards" id="cards"></div><button id="more">더 보기</button></section><script>
const $=id=>document.getElementById(id);let result=null,loading=false,limit=5,loadedScan=null;const names={A:"A · 30% / 15일",B:"B · 35% / 14일",C:"C · 40% / 15일",D:"D · 35% / 13일",T4:"상승 눌림 · 4시간봉",T1:"상승 눌림 · 1시간봉",T415:"4시간봉 음봉 15/20",T430:"4시간봉 음봉 20/30",T42ALL:"42봉 · 음봉 30~36개 비교",U20ALL:"20봉 · 양봉 10~20개 비교",P20ALL:"20봉 상승폭 + 눌림 최종 비교",G20ALL:"BTC 상승장 필터 적용 전후",R415ALL:"15/20 음봉 하락폭·거래량 비교"};
function num(x){return typeof x==='number'?x.toLocaleString('ko-KR',{maximumSignificantDigits:8}):'—'}
function pct(x){return typeof x==='number'?(x>0?'+':'')+x.toFixed(2)+'%':'—'}
function node(tag,txt,cls){let e=document.createElement(tag);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
function line(card,k,v,cls){let r=node('div',null,'line');r.append(node('span',k,'muted'),node('strong',v,cls));card.append(r)}
function render(){if(!result)return;if(result.summary[0].strategy==="trend"){renderTrend();return;}let q=$('query').value.trim().toUpperCase(),sort=$('sort').value;let a=result.rank.filter(x=>x.market.includes(q));let key={volume:'volume_increase_pct',depth:'peak_to_last_pct',gap:'change_from_open_pct'}[sort];if(key)a.sort((x,y)=>sort==='depth'?(x[key]??Infinity)-(y[key]??Infinity):(y[key]??-Infinity)-(x[key]??-Infinity));else a.sort((x,y)=>x.market.localeCompare(y.market));$('count').textContent=a.length+'종목 · '+Math.min(limit,a.length)+'개 표시';a.sort((x,y)=>Number(y.status==="BUY")-Number(x.status==="BUY"));let cards=a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+(x.status==='BUY'?'BUY 후보':'WATCH'),x.status==='BUY'?'green':'muted'));line(c,'적용 검색식',names[x.preset]||x.preset);line(c,'검색 시 참고가',num(x.reference_price)+'원');line(c,'당일 시가',num(x.day_open)+'원');line(c,'시가 대비',pct(x.change_from_open_pct),x.change_from_open_pct>=0?'green':'red');line(c,'당일 최고 상승률',pct(x.day_high_pct));line(c,'20일+오늘 최대 일중 급등',pct(x.lookback_max_spike_pct));line(c,'최대 급등 날짜',x.lookback_max_spike_date||'—');line(c,'20일 실제 하락일',x.down_days+'/20일');line(c,'20일 종가 변화',pct(x.trend_net_pct),'red');line(c,'고점 대비 전일 종가',pct(x.peak_to_last_pct),'red');line(c,'거래량 배수',num(x.vol_ratio_d3)+' → '+num(x.vol_ratio_d1)+'배','green');line(c,'거래량 배수 증가율',pct(x.volume_increase_pct));line(c,'참고가 기준 본전',num(x.breakeven_price)+'원');line(c,'신호 확정(KST)',x.signal_time);line(c,'가격 조회(KST)',x.observed_at);line(c,'1시간봉 5이평',num(x.hour_ma5));line(c,'1시간봉 확인',x.hour_valid?x.hour_signal_time:'데이터 부족');line(c,'7일째 일봉 날짜',x.planned_last_day);let b=node('button',paperStats().open.some(t=>t.market===x.market)?'모의 보유 중':x.status==='WATCH'?'WATCH 모의매수':'BUY 모의매수');b.disabled=!['BUY','WATCH'].includes(x.status)||paperBusy||!storageOK||paperStats().open.some(t=>t.market===x.market);b.onclick=()=>paperBuy(x);c.append(b);return c});$('cards').replaceChildren(...cards);$('more').hidden=a.length<=limit}
function show(){let x=result.summary[0];$('compare23').hidden=true;if(x.strategy==="trend"){showTrend(x);return;}$('applied').textContent='결과 적용 조건: '+names[x.preset];$('headline').textContent='BUY '+x.buys+'개 · WATCH '+x.watches+'개 · 20일+오늘 급등 제외 '+x.spike_excluded+'개';$('audit').textContent='신호 확정 '+x.signal_time+' KST · 검색 완료 '+x.finished_at+' KST · 분석 '+x.valid+'/'+x.universe+' · 이력 부족 '+x.history_missing+' · 현재가 없음 '+x.no_quote+' · 급등 이력 미확인 '+x.spike_history_missing;$('notice').textContent='수수료 편도 0.04% 적용. 본전가는 참고가 기준 이론값(호가단위·슬리피지 미반영). 직접 매매 전 현재가와 보유 여부 확인.';limit=5;render()}

let resultMode=null;
function syncMode(){let trend=$('strategy').value==='trend';let tf=$('trendTf').value;let direct=['T415','T430','T42ALL','U20ALL','P20ALL','G20ALL','R415ALL'].includes(tf);$('newRules').hidden=!direct;$('oldTrendRules').hidden=direct;$('directRuleText').textContent=tf==='R415ALL'?'기본 15/20 음봉, 20봉 종가 변화 -20%~-10%, 마지막 4시간봉 거래량 / 이전 19봉 평균 0.5 이상 1.5 미만을 각각 및 함께 적용한 4개 그룹. BTC 필터 없음.':tf==='G20ALL'?'V2.5 조건 유지 (양봉 10/20 이상, 상승폭 3/5/10%, 연속 음봉 1~3개, 눌림 2~10%). BTC 완료 4시간봉 종가 > SMA20 및 SMA20 > 5봉 전 SMA20일 때만 진입하는 필터와 무필터를 각각 비교. BTC는 시장의 대리 지표.':tf==='P20ALL'?'최종 비교: 완료 20봉 중 양봉 10개 이상, 순상승폭 최소 3/5/10%, 마지막 연속 음봉 1~3개, 음봉 직전까지의 고점 대비 종가 눌림 2~10%.':tf==='U20ALL'?'20개 완료 4시간봉 중 종가 > 시가인 양봉 최소 10~20개를 각각 비교 (백테스트 전용).':tf==='T42ALL'?'42개 완료 4시간봉 중 음봉 최소 30,31,32,33,34,35,36개를 각각 비교 (백테스트 전용).':tf==='T430'?'신규 20/30: 완료된 240분봉 30개 중 종가 < 시가인 봉 20개 이상.':'15/20: 완료된 240분봉 20개 중 종가 < 시가인 봉 15개 이상.';$('reverseRules').hidden=trend;$('trendRules').hidden=!trend;$('preset').hidden=trend;$('presetLabel').hidden=trend;}
function clearResult(){result=null;loadedScan=null;resultMode=null;$('out').hidden=true;}
$('strategy').onchange=()=>{syncMode();clearResult();$('applied').textContent='전략을 선택하고 새로 검색해 주세요.'};
$('trendTf').onchange=()=>{syncMode();clearResult();$('applied').textContent='시간봉 변경: 다시 검색해 주세요.'};
$('preset').onchange=()=>{clearResult();$('applied').textContent='조건 변경: 다시 검색해 주세요.'};
$('query').oninput=$('sort').onchange=()=>{limit=5;render()};$('more').onclick=()=>{limit+=5;render()};
async function begin20(back){try{$('go').disabled=true;$('back').disabled=true;$('strategy').disabled=true;$('preset').disabled=true;$('trendTf').disabled=true;let trend=$('strategy').value==='trend';if(trend&&['T42ALL','U20ALL','P20ALL','G20ALL','R415ALL'].includes($('trendTf').value))back=true;let url=trend?'/trend20/start?preset='+encodeURIComponent($('trendTf').value)+'&back='+(back?'1':'0'):'/live14/start?preset='+encodeURIComponent($('preset').value);let r=await fetch(url,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 검색 실행 중');clearResult();await poll()}catch(e){$('phase').textContent=e.message;for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=false}}
$('go').onclick=()=>begin20(false);$('back').onclick=()=>begin20(true);
function showTrend(x){
 if(['T42ALL','U20ALL','P20ALL','G20ALL','R415ALL'].includes(x.preset)){show23(x);return}
 $('applied').textContent='결과 적용 조건: '+names[x.preset];
 $('headline').textContent=resultMode==='trendback20'?'15일 과거 BUY 진입 '+x.matches+'건 · 3일 평균 '+pct(x.day3_mean_pct):'BUY '+x.buys+'개 · WATCH '+x.watches+'개';
 $('audit').textContent='분석 '+x.valid+'/'+x.universe+' · 이력 부족 종목 '+x.history_missing+' · 검색 완료 '+x.finished_at;
 $('notice').textContent=resultMode==='trendback20'?'1일 평균 '+pct(x.day1_mean_pct)+' · 3일 최고 순수익 5% 도달률 '+pct(x.day3_hit5_pct)+' · 평가 누락 '+x.outcome_missing+'건 · 누락 시간 '+x.missing_hours+' · 가격 범위 제외 '+x.price_skipped+' · 매수 후 72시간 중복 제외':'상승 눌림 실험 조건 · 편도 0.04% · BUY/WATCH 모의매수만 가능';
 limit=5;render();
}
function show23(x){
 $('applied').textContent=names[x.preset];$('headline').textContent=x.preset==='R415ALL'?'15/20 음봉 · 4개 조건 비교':x.preset==='G20ALL'?'BTC 상승장 필터 적용 전후 6개 비교군':x.preset==='P20ALL'?'상승폭 3/5/10% + 눌림 최종 비교':x.preset==='U20ALL'?'20봉 · 양봉 11개 기준 동시 비교':'42봉 · 7개 기준 동시 비교';
 $('audit').textContent=x.first_entry+' ~ '+x.last_entry_exclusive+' (종료 시각 미포함) · 전체 '+x.universe+' · 수집 성공 '+x.market_success+(x.preset==='G20ALL'?' · BTC 상승 '+x.btc_up_hours+'/360시간 · 판정불가 '+x.btc_unknown_hours+'시간':'');
 $('notice').textContent='각 기준별 72시간 중복 제외 · 1/3일 종료 수익 및 최고가 기회 (실현 수익 아님) · 동일 표본이 여러 그룹에 포함 가능 · 수수료 왕복 0.08%';
 let panel=$('compare23');panel.hidden=false;panel.replaceChildren(...result.summary.map(r=>{let c=node('article',null,'coin');c.append(node('h3',(r.preset==='R415ALL'?({base:'A 기본',depth:'B 하락폭 10~20%',volume:'C 거래량 0.5~1.5배 미만',both:'D 하락폭+거래량'}[r.refinement]):r.preset==='G20ALL'?(r.market_filter==='btc_up'?'BTC 상승 필터':'필터 없음')+' · 최소 상승 '+r.minimum_gain_pct+'%':r.preset==='P20ALL'?'최소 상승 '+r.minimum_gain_pct+'%':'최소 '+(r.minimum_up_bars??r.minimum_down_bars)+'/'+r.total_bars)+' · '+r.matches+'건'));line(c,'1일 / 3일 종료 평균',pct(r.day1_mean_pct)+' / '+pct(r.day3_mean_pct));line(c,'1일 / 3일 중앙값',pct(r.day1_median_pct)+' / '+pct(r.day3_median_pct));line(c,'1일 / 3일 종료 승률',pct(r.day1_win_pct)+' / '+pct(r.day3_win_pct));line(c,'3일 최고 평균',pct(r.day3_peak_mean_pct));line(c,'5% / 10% 도달률',pct(r.day3_hit5_pct)+' / '+pct(r.day3_hit10_pct));line(c,'-10% 도달률',pct(r.day3_loss10_pct));line(c,'판정 가능 / 평가 누락',pct(r.valid_pct)+' / '+r.outcome_missing+'건');return c}));limit=5;render();
}
function renderTrend(){
 let q=$('query').value.trim().toUpperCase();let a=result.rank.filter(x=>x.market.includes(q));
 if(resultMode!=='trendback20'){let sort=$('sort').value;if(sort==='volume')a.sort((x,y)=>y.confirm_volume_ratio-x.confirm_volume_ratio);else if(sort==='depth')a.sort((x,y)=>y.pullback_pct-x.pullback_pct);else if(sort==='market')a.sort((x,y)=>x.market.localeCompare(y.market));a.sort((x,y)=>Number(y.status==='BUY')-Number(x.status==='BUY'))}
 $('count').textContent=a.length+'건 · '+Math.min(limit,a.length)+'개 표시';$('more').hidden=a.length<=limit;
 $('cards').replaceChildren(...a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+x.status,x.status==='BUY'?'green':'muted'));line(c,'전략',names[x.preset]);if(['T415','T430','T42ALL','U20ALL','P20ALL','G20ALL','R415ALL'].includes(x.preset)){if(x.preset==='T42ALL')line(c,'비교군 최소 음봉',x.minimum_down_bars+'/42');if(x.preset==='R415ALL')line(c,'비교군',x.refinement);if(x.preset==='G20ALL'){line(c,'시장 필터',x.market_filter==='btc_up'?'BTC 상승만':'없음');line(c,'진입 당시 BTC',x.btc_regime);line(c,'BTC SMA20',num(x.btc_sma20));}if(['P20ALL','G20ALL'].includes(x.preset)){line(c,'비교군 최소 상승폭',x.minimum_gain_pct+'%');line(c,'실제 양봉 수',x.up_bars+'/20개');line(c,'최근 연속 음봉',x.pullback_bars+'개');}if(x.preset==='U20ALL'){line(c,'비교군 최소 양봉',x.minimum_up_bars+'/20');line(c,'실제 양봉 수',x.up_bars+'/20개');line(c,'보합 봉',x.flat_bars+'개');}line(c,'음봉 수',x.down_bars+'/'+x.total_bars+'개');line(c,x.total_bars+'봉 변화',pct(x.window_change_pct));line(c,'고점 대비 변화',pct(-x.pullback_pct));line(c,'마지막 4h 거래량 / 이전 '+(x.total_bars-1)+'봉',num(x.last_4h_volume_ratio)+'배');line(c,'4h 조건 확정',x.setup_end);}else{line(c,'이전 20봉 상승',pct(x.prior_gain_pct));line(c,'연속 음봉',x.pullback_bars+'개');line(c,'눌림폭',pct(-x.pullback_pct));line(c,'눌림 거래량 / 이전 평균',num(x.pullback_volume_ratio)+'배');}line(c,'반등 시간봉 거래량',num(x.confirm_volume_ratio)+'배');line(c,'신호 확정(KST)',x.signal_time);
 if(resultMode==='trendback20'){line(c,'진입가',num(x.buy_price)+'원');line(c,'1일 종료 / 최고',pct(x.day1_close_pct)+' / '+pct(x.day1_peak_pct));line(c,'3일 종료 / 최고',pct(x.day3_close_pct)+' / '+pct(x.day3_peak_pct));line(c,'3일 최저',pct(x.day3_low_pct));}
 else{line(c,'검색 참고가',num(x.reference_price)+'원');line(c,'이전 고점',num(x.prior_peak)+'원');line(c,'눌림 저점',num(x.pullback_low)+'원');let held=paperStats().open.some(t=>t.market===x.market),b=node('button',held?'모의 보유 중':x.status+' 모의매수');b.disabled=held||paperBusy||!storageOK;b.onclick=()=>paperBuy(x);c.append(b)}return c}));
}
async function poll(){try{
 let s=await(await fetch('/status',{cache:'no-store'})).json();let running=s.status==='running';
 for(let id of ['go','back','strategy','preset','trendTf'])$(id).disabled=running;
 if(!['live14','trend20','trendback20'].includes(s.mode)){$('phase').textContent=running?'다른 연구 실행 중':'대기';clearResult();return}
 $('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');
 if(running){clearResult();$('applied').textContent='검색 중: '+names[s.preset];return}
 if(s.status==='done'&&loadedScan!==s.scan_id&&!loading){
  // Do not overwrite a user's deliberate strategy selection after a previous completed scan.
  if((s.mode==='live14')!==($('strategy').value==='reverse'))return;
  if(s.mode==='live14'&&$('preset').value!==s.preset)return;
  if(s.mode!=='live14'&&$('trendTf').value!==s.preset)return;
  loading=true;try{let d=await(await fetch('/results',{cache:'no-store'})).json();if(!d.summary[0]||d.summary[0].scan_id!==s.scan_id)return;result=d;loadedScan=s.scan_id;resultMode=s.mode;if(s.mode==='live14')$('preset').value=s.preset;
  for(let k of ['features','summary','coverage','errors'])$(k+'CSV').href='/csv/'+k+'?mode='+s.mode;
  $('out').hidden=false;show()}finally{loading=false}
 }
 }catch(e){$('phase').textContent='연결 확인: '+e.message}}
syncMode();

const FEE=.0004,STORE='reverse-swing-paper-v1',INITIAL=1000000;
let account={trades:[]},storageOK=true,paperBusy=false,quotes={};
try{let raw=localStorage.getItem(STORE);if(raw){let a=JSON.parse(raw);if(!Array.isArray(a.trades))throw Error('invalid data');account=a}}catch(e){storageOK=false}
function savePaper(next){localStorage.setItem(STORE,JSON.stringify(next));account=next}
function paperStats(){let closed=account.trades.filter(x=>x.closedAt),open=account.trades.filter(x=>!x.closedAt),real=closed.reduce((s,x)=>s+x.pnl,0),spent=open.reduce((s,x)=>s+x.amount,0);return {closed,open,real,cash:INITIAL+real-spent,wins:closed.filter(x=>x.pnl>0).length,losses:closed.filter(x=>x.pnl<0).length}}
function paperMsg(s){$('paperMsg').textContent=s}
function paperTime(t){return new Date(t).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})}
async function quote(markets){let all={};for(let i=0;i<markets.length;i+=40){let r=await fetch('/paper/quotes?markets='+encodeURIComponent(markets.slice(i,i+40).join(',')),{cache:'no-store'});if(!r.ok)throw Error('현재가 조회 실패');let d=await r.json();Object.assign(all,d.quotes)}return all}
async function paperBuy(x){
 if(paperBusy||!storageOK||!result||!['BUY','WATCH'].includes(x.status))return;
 let a=Number($('paperAmount').value),s=paperStats();
 if(!Number.isFinite(a)||a<5000||a>s.cash){paperMsg('가상 잔액 내에서 5,000원 이상 입력해 주세요.');return}
 if(s.open.some(t=>t.market===x.market)){paperMsg('이미 모의 보유 중인 종목입니다.');return}
 if(s.closed.some(t=>t.market===x.market&&t.signalTime===x.signal_time)){paperMsg('같은 신호에서 이미 매매한 종목입니다.');return}
 paperBusy=true;renderPaper();paperMsg('새로운 현재가 조회 중...');
 try{let r=await fetch((x.strategy==='trend'?'/trend20/entry':'/paper/entry')+'?market='+encodeURIComponent(x.market)+'&signal='+encodeURIComponent(x.signal_time)+'&hour='+encodeURIComponent(x.hour_signal_time)+'&preset='+encodeURIComponent(x.preset)+'&scan_id='+encodeURIComponent(result.summary[0].scan_id)+'&entry_status='+encodeURIComponent(x.status),{cache:'no-store'});if(!r.ok)throw Error('신호 만료, 진입 범위 제외 또는 가격 조회 실패. 다시 검색해 주세요.');let q=await r.json();if(q.entry_status!==x.status)throw Error('검색 판정 변경. 다시 검색해 주세요.');let p=q.price;if(!Number.isFinite(p)||p<=0)throw Error('유효한 현재가 없음');
 let t={id:Date.now()+'-'+x.market,market:x.market,signalTime:x.signal_time,boughtAt:q.observed_ms,amount:a,buy:p,qty:a/(1+FEE)/p,deadline:q.observed_ms+7*86400000,signalDeadline:q.signal_deadline_ms,preset:q.preset,minDrawdownPct:q.min_drawdown_pct,minDownDays:q.min_down_days,entryStatus:q.entry_status,strategy:x.strategy||'reverse',timeframe:x.strategy==='trend'?x.preset:null};savePaper({trades:[...account.trades,t]});quotes[x.market]=q;paperMsg(x.market+' '+q.entry_status+' 모의매수 완료 (시장 참고가 가정).');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function paperSell(t){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('매도 참고가 조회 중...');
 try{let q=(await quote([t.market]))[t.market];if(!q)throw Error('가격 확인 불가');let proceeds=t.qty*q.price*(1-FEE),pnl=proceeds-t.amount;let closed={...t,closedAt:q.observed_ms,sell:q.price,proceeds,pnl,netPct:100*pnl/t.amount};savePaper({trades:account.trades.map(x=>x.id===t.id?closed:x)});quotes[t.market]=q;paperMsg(t.market+' 모의매도 완료. 순손익 '+num(pnl)+'원');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function refreshPaper(){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('보유 종목 가격 조회 중...');
 try{let m=paperStats().open.map(x=>x.market);if(m.length)quotes=await quote(m);paperMsg('가격 갱신 완료. 가격 확인 불가 종목은 — 표시.')}catch(e){quotes={};paperMsg(e.message)}finally{paperBusy=false;renderPaper()}
}
function renderPaper(){
 let s=paperStats();$('paperSummary').textContent='가상 원금 1,000,000원 · 잔액 '+num(s.cash)+'원 · 실현손익 '+num(s.real)+'원';$('paperWin').textContent='보유 '+s.open.length+' · 청산 '+s.closed.length+' · 수익 '+s.wins+' · 손실 '+s.losses+' · 승률 '+(s.closed.length?(100*s.wins/s.closed.length).toFixed(2)+'%':'—');
 $('refreshPaper').disabled=paperBusy||!storageOK;
 let cards=s.open.map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 모의 보유'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매수가',num(t.buy)+'원');line(c,'투자금(수수료 포함)',num(t.amount)+'원');let q=quotes[t.market];let val=q?t.qty*q.price*(1-FEE)-t.amount:null;line(c,'조회 가격',q?num(q.price)+'원':'—');line(c,'가격 조회',q?paperTime(q.observed_ms):'—');line(c,'평가 순손익',val===null?'—':num(val)+'원 / '+pct(100*val/t.amount),val===null?'muted':val>=0?'green':'red');line(c,'7일 보유 만료',paperTime(t.deadline),Date.now()>=t.deadline?'red':'muted');if(Date.now()>=t.deadline)c.append(node('p','7일 경과: 직접 모의매도 필요','red'));let b=node('button','모의매도');b.disabled=paperBusy||!storageOK;b.onclick=()=>paperSell(t);c.append(b);return c});
 $('paperOpen').replaceChildren(...cards);if(!cards.length)$('paperOpen').textContent='보유 종목 없음';
 let hist=s.closed.slice().reverse().slice(0,100).map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 청산 완료'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'상승 눌림':'저점 반등');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 / 매도',num(t.buy)+' / '+num(t.sell));line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매도 시각',paperTime(t.closedAt));line(c,'순손익',num(t.pnl)+'원 / '+pct(t.netPct),t.pnl>=0?'green':'red');return c});$('paperClosed').replaceChildren(...hist);if(!hist.length)$('paperClosed').textContent='청산 기록 없음';if(!storageOK)paperMsg('브라우저 저장소 사용 불가 또는 기록 손상. 모의매매를 중단합니다.');
}
$('refreshPaper').onclick=refreshPaper;
$('paperCSV').onclick=()=>{let keys=['market','strategy','timeframe','preset','entryStatus','minDrawdownPct','minDownDays','signalTime','boughtAt','amount','buy','qty','deadline','closedAt','sell','proceeds','pnl','netPct'];let esc=v=>'"'+String(v??'').replaceAll('"','""')+'"';let rows=[keys.join(','),...account.trades.map(x=>keys.map(k=>esc(x[k])).join(','))];let url=URL.createObjectURL(new Blob(['\ufeff'+rows.join('\r\n')],{type:'text/csv;charset=utf-8'}));let a=document.createElement('a');a.href=url;a.download='BITHUMB_SWING_V2_7_TEST_PAPER_'+new Date().toISOString().slice(0,10)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
renderPaper();

poll();setInterval(poll,2000);
</script></html>"""

# V3.0: independent accumulation research. No order or paper-entry endpoint.
def whale30candles(rows,cut):
    by={r[0]:r for r in rows}
    seq=[by.get(cut-i*HOUR) for i in range(97,0,-1)]
    if not all(r131valid(r) for r in seq):return None
    recent=seq[-24:];base=seq[-96:-24];prev=seq[-48:-24]
    ret=pct(recent[-1][4],seq[-25][4]);span=pct(max(r[2] for r in recent),min(r[3] for r in recent))
    avg=mean([r[5] for r in base]);vol=ratio(mean([r[5] for r in recent]),avg)
    if vol is None:return None
    lo1=min(r[3] for r in prev);lo2=min(r[3] for r in recent)
    obv=sum((1 if b[4]>a[4] else -1 if b[4]<a[4] else 0)*b[5] for a,b in zip(seq[-25:-1],recent))
    total=sum(r[5] for r in recent)
    obv_ratio=ratio(obv,total)
    if obv_ratio is None:return None
    old7=by.get(cut-169*HOUR)
    full7=r131valid(old7)  # Exact endpoint is enough for close-to-close return; not for path-dependent metrics.
    signals=[bool(abs(ret)<=3 and span<=8 and vol>=1.5),bool(lo2>=lo1*1.005),bool(obv_ratio>=.10 and ret<=3)]
    return dict(reference_price=recent[-1][4],price_basis="completed_1h_close",signal_time=iso(cut),change_1d_pct=ret,change_3d_pct=pct(recent[-1][4],seq[-73][4]),change_7d_pct=pct(recent[-1][4],old7[4]) if full7 else None,range_1d_pct=span,volume_ratio=vol,turnover_1d=sum(r[6] for r in recent),low_change_pct=pct(lo2,lo1),obv_ratio_pct=100*obv_ratio,volume_flat=signals[0],higher_low=signals[1],obv_up=signals[2],candle_score=sum(signals))
def whale30trades(raw,at):
    seen=set();ticks=[]
    for r in raw:
        try:
            ident=str(r["sequential_id"]);ts=int(r["timestamp"]);p=float(r["trade_price"]);v=float(r["trade_volume"]);side=r["ask_bid"]
            if ident in seen or side not in ("ASK","BID") or not all(math.isfinite(x) and x>0 for x in (p,v,p*v)) or not at-1800000<=ts<=at:continue
            seen.add(ident);ticks.append((ts,p,p*v,side))
        except (KeyError,TypeError,ValueError,OverflowError):continue
    ticks.sort(key=lambda x:x[0]);n=len(ticks)
    z=dict(trade_count=n,trade_span_seconds=(ticks[-1][0]-ticks[0][0])/1000 if n else 0,trade_latest=iso(ticks[-1][0]/1000) if n else None,trade_start=iso(ticks[0][0]/1000) if n else None,trade_sample_complete=False,buy_share_pct=None,trade_delta_krw=None,big_threshold_krw=None,big_buy_count=None,big_sell_count=None,trade_change_pct=None,trade_range_pct=None,buy_flow=None,big_buy_repeat=None,sell_absorption=None)
    if n<30 or z["trade_span_seconds"]<30 or at-ticks[-1][0]>120000:return z
    buy=sum(x[2] for x in ticks if x[3]=="BID");sell=sum(x[2] for x in ticks if x[3]=="ASK");share=100*buy/(buy+sell)
    threshold=max(1000000,5*statistics.median([x[2] for x in ticks]))
    bigbuy=[x for x in ticks if x[3]=="BID" and x[2]>=threshold];bigsell=[x for x in ticks if x[3]=="ASK" and x[2]>=threshold]
    ret=pct(ticks[-1][1],ticks[0][1]);span=pct(max(x[1] for x in ticks),min(x[1] for x in ticks))
    z.update(trade_sample_complete=True,buy_share_pct=share,trade_delta_krw=buy-sell,big_threshold_krw=threshold,big_buy_count=len(bigbuy),big_sell_count=len(bigsell),trade_change_pct=ret,trade_range_pct=span,buy_flow=bool(share>=60),big_buy_repeat=bool(len(bigbuy)>=3 and len(bigbuy)>len(bigsell) and sum(x[2] for x in bigbuy)>sum(x[2] for x in bigsell)),sell_absorption=bool(share<=45 and ret>=-.3 and span<=2 and pct(ticks[-1][1],min(x[1] for x in ticks))>=.2))
    return z
def whale30book(raw,m,at):
    r=next((r for r in raw if r.get("market")==m),None)
    if not r:return {}
    ts=int(r["timestamp"])
    if not 0<=at-ts<=30000:return {}
    units=r["orderbook_units"][:5]
    if len(units)!=5:return {}
    values=[tuple(float(x[k]) for k in ("bid_price","bid_size","ask_price","ask_size")) for x in units]
    if any(not all(math.isfinite(v) and v>=0 for v in x) or x[0]<=0 or x[2]<=0 for x in values):return {}
    bid=sum(x[0]*x[1] for x in values);ask=sum(x[2]*x[3] for x in values)
    return dict(book_bid_krw=bid,book_ask_krw=ask,book_bid_ask_ratio=ratio(bid,ask),book_time=iso(ts/1000),book_refill=None)
def whale31qualified(r):
    return bool(r.get("volume_flat") is True and r.get("trade_checked") is True and r.get("score",0)>=4)
def whale31coverage(rows,cut):
    if rows is None:return dict(status="fetch_error",received_bars=0)
    by={r[0]:r for r in rows if r[0]+HOUR<=cut}
    seq=[by.get(cut-i*HOUR) for i in range(97,0,-1)]
    missing=sum(r is None for r in seq)
    invalid=sum(r is not None and not r131valid(r) for r in seq)
    return dict(status="missing_hours" if missing else "invalid_ohlcv" if invalid else "complete",received_bars=len(by),missing_hours_97=missing,invalid_hours_97=invalid,oldest_candle=iso(min(by)) if by else None,latest_candle=iso(max(by)) if by else None,endpoint_7d_available=bool(r131valid(by.get(cut-169*HOUR))))

# V3.1: native 4h candles, independently selected 1/3/7-day accumulation windows.
WHALE32_RULES={1:(3,8),3:(5,12),7:(8,18)}
def whale32candles(rows,cut,days):
    step=4*HOUR;n=days*6;change_limit,range_limit=WHALE32_RULES[days]
    by={r[0]:r for r in rows if r[0]+step<=cut}
    seq=[by.get(cut-i*step) for i in range(4*n+1,0,-1)]
    missing=sum(r is None for r in seq)
    invalid=sum(r is not None and not r131valid(r) for r in seq)
    audit=dict(received_bars=len(by),required_bars=4*n+1,missing_bars=missing,invalid_bars=invalid,period_days=days,candle_minutes=240,oldest_candle=iso(min(by)) if by else None,latest_candle=iso(max(by)) if by else None)
    if missing or invalid:return None,dict(audit,status="missing_4h" if missing else "invalid_ohlcv")
    recent=seq[-n:];base=seq[-4*n:-n];prev=seq[-2*n:-n]
    ret=pct(recent[-1][4],seq[-n-1][4])
    span=pct(max(r[2] for r in recent),min(r[3] for r in recent))
    vol=ratio(mean([r[5] for r in recent]),mean([r[5] for r in base]))
    lo1=min(r[3] for r in prev);lo2=min(r[3] for r in recent)
    obv=sum((1 if b[4]>a[4] else -1 if b[4]<a[4] else 0)*b[5] for a,b in zip(seq[-n-1:-1],recent))
    obv_ratio=ratio(obv,sum(r[5] for r in recent))
    if vol is None or obv_ratio is None:return None,dict(audit,status="zero_volume")
    signals=[bool(abs(ret)<=change_limit and span<=range_limit and vol>=1.5),bool(lo2>=lo1*1.005),bool(obv_ratio>=.10 and ret<=change_limit)]
    def close_return(d):
        old=by.get(cut-(d*6+1)*step)
        return pct(recent[-1][4],old[4]) if r131valid(old) else None
    data=dict(reference_price=recent[-1][4],price_basis="completed_4h_close",signal_time=iso(cut),period_days=days,candle_minutes=240,period_change_pct=ret,period_range_pct=span,change_limit_pct=change_limit,range_limit_pct=range_limit,change_1d_pct=close_return(1),change_3d_pct=close_return(3),change_7d_pct=close_return(7),volume_ratio=vol,turnover_period=sum(r[6] for r in recent),low_change_pct=pct(lo2,lo1),obv_ratio_pct=100*obv_ratio,volume_flat=signals[0],higher_low=signals[1],obv_up=signals[2],candle_score=sum(signals))
    return data,dict(audit,status="valid")

# V3.1.1: classify independently from display limits; missing cross-period data is unknown.
def whale33class(r):
    if whale31qualified(r):return "STRONG"
    if r.get("volume_flat") is True and (r.get("candle_score",0)==3 or (r.get("candle_score",0)>=2 and r.get("matched_period_count",0)>=2)):
        return "WATCH"
    return "NONE"
def runwhale30(target,days=1):
    begin=time.monotonic()
    try:
        end=int(now().timestamp());step=4*HOUR
        probe=candles("KRW-BTC",240,end,8,latest=True)
        if len(probe)<2:raise RuntimeError("BTC 4시간봉 경계 확인 실패")
        anchor=probe[-1][0]%step
        if any(r[0]%step!=anchor for r in probe):raise RuntimeError("4시간봉 경계 불일치")
        cut=((end-anchor)//step)*step+anchor
        if not any(r[0]==cut-step and r131valid(r) for r in probe):raise RuntimeError("완료 BTC 4시간봉 지연")
        universe=sorted({x["market"] for x in api("market/all") if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        if target and target not in universe:raise RuntimeError("해당 KRW 종목 없음")
        if target:universe=[target]
        if not universe:raise RuntimeError("시장 목록 없음")
        with state_lock:scan_id=state["scan_id"]
        patch(phase="1단계 · "+str(days)+"일 매집 4시간봉 검색",done=0,total=len(universe))
        allrows=[];cov=[]
        for m,rows in parallel(universe,lambda m:candles(m,240,cut,200,latest=False)):
            if rows is None:s,audit=None,dict(status="fetch_error",period_days=days,candle_minutes=240)
            else:s,audit=whale32candles(rows,cut,days)
            cov.append(dict(market=m,**audit))
            if s:
                periods={}
                for d in (1,3,7):
                    z=s if d==days else whale32candles(rows,cut,d)[0]
                    periods[d]=z
                matched=[d for d,z in periods.items() if z and z["volume_flat"] and z["candle_score"]>=2]
                s.update(matched_periods="/".join(str(d) for d in matched),matched_period_count=len(matched))
                for d,z in periods.items():
                    s["candle_score_"+str(d)+"d"]=z["candle_score"] if z else None
                    s["candidate_"+str(d)+"d"]=bool(z["volume_flat"] and z["candle_score"]>=2) if z else None
                allrows.append(dict(market=m,scan_id=scan_id,strategy="whale",**s))
        allrows.sort(key=lambda r:(-r["candle_score"],-r["volume_ratio"],r["market"]))
        eligible=[r for r in allrows if r["volume_flat"] and r["candle_score"]>=2]
        candidates=([r for r in allrows if r["market"]==target] if target else eligible)[:20]
        patch(phase="2단계 · 후보 실제 체결 및 호가",done=0,total=len(candidates))
        def enrich(r):
            m=r["market"];out={}
            if r.get("change_7d_pct") is None:
                try:
                    old=candles(m,240,cut-168*HOUR,3,latest=False)
                    exact=next((x for x in old if x[0]==cut-172*HOUR and r131valid(x)),None)
                    if exact:out["change_7d_pct"]=pct(r["reference_price"],exact[4])
                except Exception as e:record_error(m+" 7d endpoint: "+str(e))
            try:
                ticks=api("trades/ticks",market=m,count=500);out.update(whale30trades(ticks,int(now().timestamp()*1000)))
            except Exception as e:record_error(m+" trades: "+str(e))
            try:
                book=api("orderbook",markets=m);out.update(whale30book(book,m,int(now().timestamp()*1000)))
            except Exception as e:record_error(m+" book: "+str(e))
            return out
        by={r["market"]:r for r in candidates}
        for m,s in parallel(list(by),lambda m:enrich(by[m])):
            r=by[m];r.update(s or {})
            r["trade_checked"]=bool(r.get("trade_sample_complete"))
            r["score_max"]=6 if r["trade_checked"] else 3
            r["score"]=r["candle_score"]+sum(r.get(k) is True for k in ("buy_flow","big_buy_repeat","sell_absorption"))
            r["qualified"]=whale31qualified(r)
            r["trade_diagnosis"]="체결 확인" if r["trade_checked"] else "체결 자료 부족"
            r["observed_at"]=iso(int(now().timestamp()))
        candidates.sort(key=lambda r:(not r["trade_checked"],-r["score"],-r["candle_score"],-r["volume_ratio"]))
        for r in allrows:
            if "score" not in r:r.update(score=r["candle_score"],score_max=3,trade_checked=False,qualified=False,trade_diagnosis="정밀 분석 미실행")
            r["status"]=whale33class(r)
        qualified=[r for r in candidates if r["status"]=="STRONG"]
        watches=sorted([r for r in allrows if r["status"]=="WATCH"],key=lambda r:(-r["matched_period_count"],-r["candle_score"],-r["volume_ratio"],r["market"]))
        shown=candidates if target else qualified[:5]+watches[:5]
        for r in allrows:r["displayed"]=any(x["market"]==r["market"] for x in shown)
        summary=dict(strategy="whale",preset="WHALE30",period_days=days,candle_minutes=240,scan_id=scan_id,universe=len(universe),valid=len(allrows),history_missing=len(universe)-len(allrows),candle_candidates=len(eligible),deep_checked=len(candidates),trade_valid=sum(r["trade_checked"] for r in candidates),suspects=len(qualified),watch_count=len(watches),strong_displayed=sum(r["status"]=="STRONG" for r in shown),watch_displayed=sum(r["status"]=="WATCH" for r in shown),displayed=len(shown),target=target,seven_day_valid=sum(r.get("change_7d_pct") is not None for r in allrows),missing_4h_markets=sum(r["status"]=="missing_4h" for r in cov),fetch_errors=sum(r["status"]=="fetch_error" for r in cov),signal_time=iso(cut),finished_at=iso(int(now().timestamp())))
        patch(status="done",phase="완료",rank=shown,features=allrows,coverage=cov,summary=[summary],elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:record_error(str(e));patch(status="error",phase="실패")
def startwhale30(target,days=1):
    with state_lock:
        if state["status"]=="running":return False
        state.clear();state.update(mode="whale30",preset="WHALE30",target=target,period_days=days,scan_id=str(time.time_ns()),status="running",phase="준비",done=0,total=0,rank=[],features=[],summary=[],errors=[],coverage=[])
    threading.Thread(target=runwhale30,args=(target,days),daemon=True).start()
    return True

HTML29=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BITHUMB V3.1.1 LIVE</title><style>
:root{color-scheme:light;--ink:#192421;--muted:#64716b;--green:#16734e;--line:#e0e7e3;--soft:#f4f7f5}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0 auto;max-width:820px;padding:22px 14px 48px;background:#f6f8f7;color:var(--ink);font:15px/1.55 system-ui,-apple-system,sans-serif}
h1{font-size:25px;line-height:1.25;margin:0;letter-spacing:-.7px}h2{font-size:18px;margin:0 0 16px}h3{font-size:16px;margin:0 0 10px}p{margin:10px 0}small{display:block;color:var(--muted);font-size:12.5px;line-height:1.7}
header{padding:5px 3px 15px}header p{margin:5px 0 0;color:var(--muted);font-size:13px}
section{background:white;border:1px solid var(--line);border-radius:18px;padding:16px;margin:0 0 12px}
button,a,select,input{font:inherit;max-width:100%;border:1px solid var(--line);border-radius:10px;padding:11px 12px;color:var(--ink);background:white}
button,a{cursor:pointer;text-decoration:none}button:disabled,select:disabled{opacity:.5;cursor:default}button:focus-visible,a:focus-visible,select:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--green);outline-offset:3px}
.strategies{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:0 0 16px}
.strategy{min-height:116px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;padding:13px 6px;background:#fff;color:var(--ink);border:1px solid #dce4df;border-radius:13px}
.strategy svg{width:32px;height:32px;stroke:var(--green);fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.strategy strong{font-size:17px;letter-spacing:-.3px}.strategy span{color:var(--muted);font-size:12px}
.strategy[aria-pressed=true]{background:#eef8f2;border:2px solid var(--green);padding:12px 5px}.strategy[aria-pressed=true] strong{color:var(--green)}
.settings{padding:12px;background:var(--soft);border-radius:12px;margin-bottom:12px}.field{display:grid;gap:5px;margin:0 0 10px}.field:last-child{margin-bottom:0}.field label{font-size:13px;font-weight:600}.field select{width:100%;min-height:44px}
details{margin:10px 0;color:var(--muted)}summary{cursor:pointer;font-size:13px;font-weight:600;padding:7px 0}details p{font-size:13px;line-height:1.7}
#go{display:block;width:100%;background:var(--green);color:white;border:0;font-weight:700;font-size:16px;min-height:49px;margin:14px 0 10px}
#applied,#phase,#stats{font-size:12.5px;margin:5px 0;color:var(--muted)}progress{width:100%;height:7px;accent-color:var(--green);display:block;margin:9px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;color:#b33b39;margin:6px 0}pre:empty{display:none}
.quick{display:grid;gap:0;border-top:1px solid var(--line);margin-top:14px}.quick a{display:flex;align-items:center;gap:10px;border:0;border-bottom:1px solid var(--line);border-radius:0;padding:13px 1px;font-weight:600;font-size:14px}.quick a:last-child{border-bottom:0;padding-bottom:3px}.quick svg{width:20px;height:20px;stroke:var(--green);fill:none;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}.quick .arrow{margin-left:auto;color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:10px}.coin{background:white;border:1px solid var(--line);border-radius:13px;padding:14px;min-width:0}.coin h3{border-bottom:1px solid var(--line);padding-bottom:10px}.line{display:flex;justify-content:space-between;gap:10px;margin:6px 0;font-size:13px}.line strong{text-align:right;overflow-wrap:anywhere}.coin button{width:100%;margin:8px 0 0;background:#edf6f0;color:var(--green);font-weight:650}
.whale-watch{color:#9a6400}.green{color:var(--green)}.red{color:#b33b39}.muted{color:var(--muted)}
.actions{display:flex;flex-wrap:wrap;gap:7px;margin:12px 0}.actions button,.actions a{font-size:13px;background:#f4f7f5;font-weight:600}.actions input,.actions select{min-width:0}.results-tools{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:10px 0}.results-tools input,.results-tools select{width:100%;font-size:13px}
#more{width:100%;margin:10px 0}#paperSummary{font-weight:650;background:var(--soft);padding:12px;border-radius:10px;font-size:14px}#paperWin,#count,#audit,#notice{font-size:12.5px;color:var(--muted)}#paperMsg{font-size:13px}
@media(max-width:360px){section{padding:12px}body{padding:14px 10px}.strategy span{font-size:11px}}
</style><header><h1>BITHUMB V3.1.1 LIVE</h1><p>실시간 코인 스캐너 · 빗썸 KRW</p></header><section><h2>전략 선택</h2><select id="strategy" hidden aria-label="전략"><option value="reverse">저점형 (일봉)</option><option value="trend">돌파형 (4시간봉)</option><option value="whale">매집형</option></select><div class="strategies"><button type="button" class="strategy" id="pickReverse" aria-pressed="true"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 20v-4m5 4v-8m5 8v-5m5 5V9M3 11l6-6 4 4 8-7"/></svg><strong>저점형</strong><span>일봉 스윙</span></button><button type="button" class="strategy" id="pickTrend" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 17 7-7 4 4 7-9m-7 0h7v7"/></svg><strong>돌파형</strong><span>4시간봉 단타</span></button><button type="button" class="strategy" id="pickWhale" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="m16 16 5 5M8 11h6m-3-3v6"/></svg><strong>매집형</strong><span>매집 탐지</span></button></div><div id="whaleRules" hidden><div class="settings"><div class="field"><label for="whaleDays">매집 관찰 기간</label><select id="whaleDays"><option value="1">1일 · 단기</option><option value="3" selected>3일 · 지속</option><option value="7">7일 · 장기</option></select></div><div class="field"><label for="whaleTarget">검색 종목 (비워두면 전체 KRW)</label><input id="whaleTarget" placeholder="예: CFG" maxlength="36" autocomplete="off"></div><small>STRONG 최대 5개 · WATCH 최대 5개 · 개별 종목 진단</small></div><details><summary>매집 탐지 기준</summary><p>완료 4시간봉 기준 1일(6봉), 3일(18봉), 7일(42봉). 기간별 종가 변화 ±3/5/8% 이내 및 고저 범위 8/12/18% 이내, 직전 3배 기간의 봉당 평균 거래량 대비 1.5배 이상 (1점). 직전 동일 길이 기간 대비 저점 0.5% 이상 상승 (1점). OBV 순증가량이 선택 기간 거래량의 10% 이상이며 종가 상승률이 기간별 상한 이하 (1점). 필요 봉 누락 시 제외. 이 기준은 연구용이며 검증되지 않았습니다. 횡보 중 거래량 증가 필수 + 캔들 2점 이상 후보 최대 20개를 정밀 분석합니다. STRONG은 기존 기준(횡보·거래량 필수, 유효 체결 및 4/6 이상)을 유지합니다. WATCH는 선택 기간의 횡보·거래량 필수와 함께 캔들 3/3 또는 1·3·7일 중 2개 이상 기간에서 캔들 후보(2/3 이상)인 종목입니다. STRONG과 WATCH는 중복 없이 각각 최대 5개, WATCH는 공통 기간 수 우선으로 표시합니다. 다른 기간 데이터 부족은 미확인으로 구분합니다. 단일 종목은 조건 미충족이어도 진단합니다.</p><p>최근 최대 500체결 중 30분 이내 표본: 매수 체결대금 60% 이상, 큰 매수 체결 3건 이상 우세(100만원 또는 표본 중앙값 5배 중 큰 기준), 매도 우세인데 가격 유지 및 저점 회복을 각각 1점으로 평가. 30건 및 30초 이상, 마지막 체결 2분 이내인 경우에만 채점. 선택 기간 전체의 체결이 아닌 현재 시점의 관측 표본입니다. 전체 30분 CVD도 아닙니다. 호가 5단계 잔량 비율은 참고값. 호가 재충전 및 실제 고래 신원은 확인하지 않습니다. 임시 기준이며 BUY 신호나 상승 확률이 아닙니다.</p></details></div><details id="reverseRules"><summary>저점형 전략 설명</summary><p>20일 하락 추세 + 선택한 하락폭·하락일 수 + 전일 거래량 2배 이상 + D-3 대비 2배 이상 · RSI 제외</p><small>최근 20일과 오늘의 일별 고가가 시가 또는 전일 종가 대비 한 번이라도 30% 이상 상승하면 제외. 과거 급등 이력 부족 시 제외. 강화 일봉 조건 충족 WATCH. 가장 최근 완료 1시간봉의 SMA5 상향 돌파만 BUY 후보. 미완료 봉 제외. BUY/WATCH 모의매수 모두 검색된 1시간 동안 유효. WATCH는 5이평 상향 돌파 미확인 상태의 비교 실험이며 BUY로 변경하지 않음. 검증 전 실전 매수 권장 아님. 검색 시점의 가격은 실제 체결가가 아니며 당일 시가 진입 백테스트와 다를 수 있음. 최고가 매도 가정 결과는 실제 승률이 아님. 자동 주문 없음. 별도 매도·손절 신호 미검증. 기존 보유 종목은 직접 제외.</small></details><div id="trendRules" hidden><details><summary>돌파형 전략 설명</summary><p>완료 4시간봉 20개 중 음봉 15개 이상 (연속 불필요). BUY: 완료 1시간봉 양봉, 직전 고가 돌파, 거래량 직전 3봉 평균 1.2배 이상. WATCH는 반등 미확인. 별도 하락폭·4시간 거래량·BTC 필터 없음.</p></details><div class="settings"><div class="field"><label for="exitPlan">매도 방식</label><select id="exitPlan"><option value="B" selected>분할 +1% 70% / 추세 30%</option><option value="A1">전량 +1%</option><option value="A2">전량 +2%</option><option value="A3">전량 +3%</option><option value="C">전량 추세 추종</option></select></div><div class="field"><label for="stopPlan">순손절</label><select id="stopPlan"><option value="1">-1%</option><option value="2">-2%</option><option value="3" selected>-3%</option></select></div><small>최대 보유 72시간 · 왕복 수수료 0.08%</small></div><details><summary>매도 방식 및 주의사항</summary><small>돌파형 최대 72시간. 추세 이탈은 완료 1시간봉 종가가 SMA5 미만일 때 표시. 매도는 직접 실행, 자동 주문 없음. 매도 계획은 모의매수 시 저장. 실시간 수익성 미검증.</small></details></div><div class="settings" id="reverseSettings"><div class="field"><label id="presetLabel" for="preset">검색 조건</label><select id="preset"><option value="A">A · 30% / 15일</option><option value="B">B · 35% / 14일</option><option value="C">C · 40% / 15일</option><option value="D" selected>D · 35% / 13일 (완화 테스트)</option></select></div></div><button id="go">전체 코인 검색 시작</button><p id="applied" class="muted">조건을 선택하고 검색해 주세요.</p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="stats"></p><pre id="errors"></pre><nav class="quick" aria-label="바로가기"><a href="#paperSection"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="M3 9h18m-5 5h3"/></svg>모의매매 계좌<span class="arrow">›</span></a><a href="#resultAnchor"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18m-12 0v10m6-10v10"/></svg>검색 결과 및 CSV 저장<span class="arrow">›</span></a></nav></section><div id="resultAnchor"><section id="out" hidden><h2 id="headline"></h2><p id="audit" class="muted"></p><p id="notice" class="muted"></p><div id="compare23" hidden></div><p class="results-tools"><input id="query" placeholder="코인 검색" style="width:105px"><select id="sort"><option value="market">종목명 순</option><option value="volume">거래량 증가율 순</option><option value="depth">고점 대비 하락폭 순</option><option value="gap">당일 시가 대비 변동률 순</option></select></p><p id="count"></p><div class="cards" id="cards"></div><button id="more">더 보기</button><div class="actions"><a id="featuresCSV" href="/csv/features?mode=live14">결과 CSV</a><a id="summaryCSV" href="/csv/summary?mode=live14">요약 CSV</a><a id="coverageCSV" href="/csv/coverage?mode=live14">수집 상태 CSV</a><a id="errorsCSV" href="/csv/errors?mode=live14">오류 CSV</a></div></section><p id="resultEmpty" class="muted" style="padding:0 4px">검색 후 이곳에 결과가 표시됩니다.</p></div><section id="paperSection"><h2>모의매매 계좌</h2><details><summary>계좌 이용 안내</summary><small>가상 원금 100만원, 1회 투자금 변경 가능. 현재가 조회 후 전량 가상 체결(편도 0.04%). 직접 모의매수/매도. 저점형 기존 7일, 돌파형 72시간 청산 안내. 계획은 자동으로 체결되지 않음. 실제 주문 없음. 기록은 이 브라우저에만 저장, 기기·브라우저 변경/데이터 삭제 시 유실될 수 있음. CSV 백업 권장.</small></details><p class="actions"><label for="paperAmount">1회 투자금(원)</label><input id="paperAmount" type="number" min="5000" step="1000" value="100000" style="width:105px"><button id="refreshPaper">보유 가격 갱신</button><button id="paperCSV">모의매매 CSV</button></p><p id="paperSummary"></p><p id="paperWin"></p><p id="paperMsg" class="muted"></p><h3>보유 종목</h3><div class="cards" id="paperOpen"></div><details><summary>청산 기록 (최근 100건)</summary><div class="cards" id="paperClosed"></div></details></section><script>
const $=id=>document.getElementById(id);let result=null,loading=false,limit=5,loadedScan=null;const names={A:"A · 30% / 15일",B:"B · 35% / 14일",C:"C · 40% / 15일",D:"D · 35% / 13일",T415:"돌파형 · 4시간봉 음봉 15/20",WHALE30:"매집형"};
function num(x){return typeof x==='number'?x.toLocaleString('ko-KR',{maximumSignificantDigits:8}):'—'}
function pct(x){return typeof x==='number'?(x>0?'+':'')+x.toFixed(2)+'%':'—'}
function node(tag,txt,cls){let e=document.createElement(tag);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
function line(card,k,v,cls){let r=node('div',null,'line');r.append(node('span',k,'muted'),node('strong',v,cls));card.append(r)}
function render(){if(!result)return;if(result.summary[0].strategy==="whale"){renderWhale();return;}if(result.summary[0].strategy==="trend"){renderTrend();return;}let q=$('query').value.trim().toUpperCase(),sort=$('sort').value;let a=result.rank.filter(x=>x.market.includes(q));let key={volume:'volume_increase_pct',depth:'peak_to_last_pct',gap:'change_from_open_pct'}[sort];if(key)a.sort((x,y)=>sort==='depth'?(x[key]??Infinity)-(y[key]??Infinity):(y[key]??-Infinity)-(x[key]??-Infinity));else a.sort((x,y)=>x.market.localeCompare(y.market));$('count').textContent=a.length+'종목 · '+Math.min(limit,a.length)+'개 표시';a.sort((x,y)=>Number(y.status==="BUY")-Number(x.status==="BUY"));let cards=a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+(x.status==='BUY'?'BUY 후보':'WATCH'),x.status==='BUY'?'green':'muted'));line(c,'적용 검색식',names[x.preset]||x.preset);line(c,'검색 시 참고가',num(x.reference_price)+'원');line(c,'당일 시가',num(x.day_open)+'원');line(c,'시가 대비',pct(x.change_from_open_pct),x.change_from_open_pct>=0?'green':'red');line(c,'당일 최고 상승률',pct(x.day_high_pct));line(c,'20일+오늘 최대 일중 급등',pct(x.lookback_max_spike_pct));line(c,'최대 급등 날짜',x.lookback_max_spike_date||'—');line(c,'20일 실제 하락일',x.down_days+'/20일');line(c,'20일 종가 변화',pct(x.trend_net_pct),'red');line(c,'고점 대비 전일 종가',pct(x.peak_to_last_pct),'red');line(c,'거래량 배수',num(x.vol_ratio_d3)+' → '+num(x.vol_ratio_d1)+'배','green');line(c,'거래량 배수 증가율',pct(x.volume_increase_pct));line(c,'참고가 기준 본전',num(x.breakeven_price)+'원');line(c,'신호 확정(KST)',x.signal_time);line(c,'가격 조회(KST)',x.observed_at);line(c,'1시간봉 5이평',num(x.hour_ma5));line(c,'1시간봉 확인',x.hour_valid?x.hour_signal_time:'데이터 부족');line(c,'7일째 일봉 날짜',x.planned_last_day);let b=node('button',paperStats().open.some(t=>t.market===x.market)?'모의 보유 중':x.status==='WATCH'?'WATCH 모의매수':'BUY 모의매수');b.disabled=!['BUY','WATCH'].includes(x.status)||paperBusy||!storageOK||paperStats().open.some(t=>t.market===x.market);b.onclick=()=>paperBuy(x);c.append(b);return c});$('cards').replaceChildren(...cards);$('more').hidden=a.length<=limit}
function show(){let x=result.summary[0];$('compare23').hidden=true;if(x.strategy==="whale"){showWhale(x);return;}if(x.strategy==="trend"){showTrend(x);return;}$('applied').textContent='결과 적용 조건: '+names[x.preset];$('headline').textContent='BUY '+x.buys+'개 · WATCH '+x.watches+'개 · 20일+오늘 급등 제외 '+x.spike_excluded+'개';$('audit').textContent='신호 확정 '+x.signal_time+' KST · 검색 완료 '+x.finished_at+' KST · 분석 '+x.valid+'/'+x.universe+' · 이력 부족 '+x.history_missing+' · 현재가 없음 '+x.no_quote+' · 급등 이력 미확인 '+x.spike_history_missing;$('notice').textContent='수수료 편도 0.04% 적용. 본전가는 참고가 기준 이론값(호가단위·슬리피지 미반영). 직접 매매 전 현재가와 보유 여부 확인.';limit=5;render()}

let resultMode=null;
function syncMode(){let mode=$('strategy').value,trend=mode==='trend',whale=mode==='whale';$('reverseRules').hidden=mode!=='reverse';$('trendRules').hidden=!trend;$('reverseSettings').hidden=mode!=='reverse';$('whaleRules').hidden=!whale;$('pickReverse').setAttribute('aria-pressed',String(mode==='reverse'));$('pickTrend').setAttribute('aria-pressed',String(trend));$('pickWhale').setAttribute('aria-pressed',String(whale));}
function pickStrategy(v){if($('strategy').disabled||$('strategy').value===v)return;$('strategy').value=v;$('strategy').onchange();}
$('pickReverse').onclick=()=>pickStrategy('reverse');$('pickTrend').onclick=()=>pickStrategy('trend');$('pickWhale').onclick=()=>pickStrategy('whale');
function clearResult(){result=null;loadedScan=null;resultMode=null;$('out').hidden=true;$('resultEmpty').hidden=false;}
$('strategy').onchange=()=>{syncMode();clearResult();$('applied').textContent='전략을 선택하고 새로 검색해 주세요.'};
$('preset').onchange=()=>{clearResult();$('applied').textContent='조건 변경: 다시 검색해 주세요.'};$('whaleDays').onchange=$('whaleTarget').onchange=()=>{clearResult();$('applied').textContent='매집 설정 변경: 다시 검색해 주세요.'};
$('query').oninput=$('sort').onchange=()=>{limit=5;render()};$('more').onclick=()=>{limit+=5;render()};
async function begin29(){try{for(let id of ['go','strategy','preset','exitPlan','stopPlan','pickReverse','pickTrend','pickWhale','whaleTarget','whaleDays'])$(id).disabled=true;let trend=$('strategy').value==='trend';let url=$('strategy').value==='whale'?'/whale30/start?days='+encodeURIComponent($('whaleDays').value)+'&market='+encodeURIComponent($('whaleTarget').value.trim().toUpperCase()):trend?'/trend20/start?preset=T415&back=0':'/live14/start?preset='+encodeURIComponent($('preset').value);let r=await fetch(url,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 검색 실행 중');clearResult();await poll()}catch(e){$('phase').textContent=e.message;for(let id of ['go','strategy','preset','exitPlan','stopPlan','pickReverse','pickTrend','pickWhale','whaleTarget','whaleDays'])$(id).disabled=false}}
$('go').onclick=begin29;
function showWhale(x){
 $('applied').textContent='매집형 · '+x.period_days+'일 · 4시간봉 연구용';$('headline').textContent='STRONG '+x.strong_displayed+' · WATCH '+x.watch_displayed;$('audit').textContent='1차 유효 '+x.valid+'/'+x.universe+' · 이력 부족 '+x.history_missing+' · 전체 STRONG '+x.suspects+' / WATCH '+x.watch_count+' · 1차 후보 '+x.candle_candidates+' · 정밀 분석 '+x.deep_checked+' · 체결 유효 '+x.trade_valid+' · 7일 비교 가능 '+x.seven_day_valid+' · 완료 '+x.finished_at;$('notice').textContent='STRONG: 유효 체결 및 4/6 이상. WATCH: 캔들 3/3 또는 여러 기간 공통 후보. 각각 최대 5개. WATCH는 매수 확정 신호가 아니며 체결 미확인일 수 있습니다. CSV에 전체 1차 결과와 기간별 판정 포함.';limit=5;render();
}
function renderWhale(){
 let q=$('query').value.trim().toUpperCase(),a=result.rank.filter(x=>x.market.includes(q));let sort=$('sort').value;
 if(sort==='volume')a.sort((x,y)=>y.volume_ratio-x.volume_ratio);else if(sort==='gap')a.sort((x,y)=>y.period_change_pct-x.period_change_pct);else if(sort==='depth')a.sort((x,y)=>y.low_change_pct-x.low_change_pct);else a.sort((x,y)=>y.matched_period_count-x.matched_period_count||y.candle_score-x.candle_score||y.volume_ratio-x.volume_ratio);
 $('count').textContent=a.length+'개 표시';$('more').hidden=true;
 const makeCard=x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+(x.status==='NONE'?'조건 미충족':x.status),x.status==='STRONG'?'green':x.status==='WATCH'?'whale-watch':'muted'));line(c,'매집 징후',x.score+'/'+x.score_max+' · '+(x.trade_checked?'체결 포함':'캔들만'));line(c,'캔들 점수 (1·3·7일)',[1,3,7].map(d=>x['candle_score_'+d+'d']==null?'미확인':x['candle_score_'+d+'d']+'/3').join(' / '));line(c,'공통 후보 기간',x.matched_periods?x.matched_periods.split('/').map(d=>d+'일').join(' · '):'없음');line(c,'체결 판정',x.trade_diagnosis||'미확인');line(c,'관찰 기간',x.period_days+'일 · 4시간봉');line(c,'완료 4h 종가',num(x.reference_price)+'원');line(c,'1일 / 3일 / 7일 변화',[x.change_1d_pct,x.change_3d_pct,x.change_7d_pct].map(pct).join(' / '));line(c,'선택 기간 가격 변화',pct(x.period_change_pct));line(c,'선택 기간 고저 범위',pct(x.period_range_pct));line(c,'선택 기간 거래량 배수',num(x.volume_ratio)+'배');line(c,'직전 동일 기간 대비 저점',pct(x.low_change_pct));line(c,'OBV 순증가 비율',pct(x.obv_ratio_pct));let checks=[['횡보 중 거래량 증가','volume_flat'],['저점 상승','higher_low'],['OBV 상승','obv_up'],['매수 체결 우세','buy_flow'],['대량 매수 반복','big_buy_repeat'],['매도 흡수 의심','sell_absorption']];for(let [label,key] of checks)line(c,label,x[key]===true?'✓ 충족':x[key]===false?'미충족':'미확인',x[key]===true?'green':'muted');line(c,'체결 표본',x.trade_count==null?'미확인':x.trade_count+'건 / '+num(x.trade_span_seconds)+'초');line(c,'표본 매수 대금 비율',pct(x.buy_share_pct));line(c,'표본 순매수 대금',num(x.trade_delta_krw)+'원');line(c,'대량 매수 / 매도 건수',num(x.big_buy_count)+' / '+num(x.big_sell_count));line(c,'호가 5단계 매수/매도',num(x.book_bid_ask_ratio)+'배');line(c,'호가 재충전','미측정');line(c,'체결 표본 종료',x.trade_latest||'—');line(c,'호가 조회 시각',x.book_time||'—');return c};
 let items=[];for(let [status,label,cls] of [['STRONG','STRONG · 매집 강한 의심','green'],['WATCH','WATCH · 매집 관찰 후보','whale-watch'],['NONE','개별 진단 · 조건 미충족','muted']]){let group=a.filter(x=>x.status===status);if(status==='NONE'&&!group.length)continue;let head=node('h3',label+' ('+group.length+')',cls);head.style.gridColumn='1 / -1';head.style.margin='12px 0 0';items.push(head);if(group.length)items.push(...group.map(makeCard));else{let empty=node('p','해당 종목 없음','muted');empty.style.gridColumn='1 / -1';items.push(empty)}}$('cards').replaceChildren(...items);
}
function showTrend(x){$('applied').textContent='돌파형 · 4시간봉 15/20';$('headline').textContent='BUY '+x.buys+'개 · WATCH '+x.watches+'개';$('audit').textContent='분석 '+x.valid+'/'+x.universe+' · 이력 부족 '+x.history_missing+' · 검색 완료 '+x.finished_at;$('notice').textContent='BUY는 1시간봉 돌파 확인, WATCH는 관찰 후보. 돌파형 모의매수는 BUY만 가능. 최대 72시간. 편도 0.04%.';limit=5;render();}
function renderTrend(){
 let q=$('query').value.trim().toUpperCase(),a=result.rank.filter(x=>x.market.includes(q));let sort=$('sort').value;if(sort==='volume')a.sort((x,y)=>y.confirm_volume_ratio-x.confirm_volume_ratio);else if(sort==='depth')a.sort((x,y)=>y.pullback_pct-x.pullback_pct);else a.sort((x,y)=>x.market.localeCompare(y.market));a.sort((x,y)=>Number(y.status==='BUY')-Number(x.status==='BUY'));
 $('count').textContent=a.length+'종목 · '+Math.min(limit,a.length)+'개 표시';$('more').hidden=a.length<=limit;
 $('cards').replaceChildren(...a.slice(0,limit).map(x=>{let c=node('article',null,'coin');c.append(node('h3',x.market.replace('KRW-','')+' · '+x.status,x.status==='BUY'?'green':'muted'));line(c,'음봉 수',x.down_bars+'/20개');line(c,'20봉 변화',pct(x.window_change_pct));line(c,'고점 대비 변화',pct(-x.pullback_pct));line(c,'마지막 4h 거래량 / 이전 19봉',num(x.last_4h_volume_ratio)+'배');line(c,'반등 시간봉 거래량',num(x.confirm_volume_ratio)+'배');line(c,'신호 확정(KST)',x.signal_time);line(c,'검색 참고가',num(x.reference_price)+'원');line(c,'본전가',num(x.breakeven_price)+'원');let held=paperStats().open.some(t=>t.market===x.market),b=node('button',held?'모의 보유 중':x.status==='BUY'?'BUY 모의매수':'WATCH 관찰');b.disabled=held||paperBusy||!storageOK||x.status!=='BUY';b.onclick=()=>paperBuy(x);c.append(b);return c}));
}
async function poll(){try{
 let s=await(await fetch('/status',{cache:'no-store'})).json();let running=s.status==='running';
 for(let id of ['go','strategy','preset','exitPlan','stopPlan','pickReverse','pickTrend','pickWhale','whaleTarget','whaleDays'])$(id).disabled=running;
 if(!['live14','trend20','whale30'].includes(s.mode)){$('phase').textContent=running?'다른 연구 실행 중':'대기';clearResult();return}
 $('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('stats').textContent='수집 오류 '+s.error_count+' · 전체 '+(s.elapsed_seconds??'—')+'초';$('errors').textContent=s.errors.join('\n');
 if(running){clearResult();$('applied').textContent='검색 중: '+names[s.preset];return}
 if(s.status==='done'&&loadedScan!==s.scan_id&&!loading){
  // Do not overwrite a user's deliberate strategy selection after a previous completed scan.
  if(({live14:'reverse',trend20:'trend',whale30:'whale'})[s.mode]!==$('strategy').value)return;
  if(s.mode==='live14'&&$('preset').value!==s.preset)return;
  if(s.mode==='trend20'&&s.preset!=='T415')return;
  if(s.mode==='whale30'){let target=$('whaleTarget').value.trim().toUpperCase();if(target&&!target.startsWith('KRW-'))target='KRW-'+target;if(Number($('whaleDays').value)!==s.period_days||target!==(s.target||''))return;}
  loading=true;try{let d=await(await fetch('/results',{cache:'no-store'})).json();if(!d.summary[0]||d.summary[0].scan_id!==s.scan_id)return;result=d;loadedScan=s.scan_id;resultMode=s.mode;if(s.mode==='live14')$('preset').value=s.preset;
  for(let k of ['features','summary','coverage','errors'])$(k+'CSV').href='/csv/'+k+'?mode='+s.mode+(s.mode==='whale30'?'':'&live29=1')+'&scan_id='+encodeURIComponent(s.scan_id);
  $('out').hidden=false;$('resultEmpty').hidden=true;show()}finally{loading=false}
 }
 }catch(e){$('phase').textContent='연결 확인: '+e.message}}
syncMode();

const FEE=.0004,STORE='reverse-swing-paper-v1',INITIAL=1000000;
let account={trades:[]},storageOK=true,paperBusy=false,quotes={},trendQuotes={};const planNames={A1:'전량 +1%',A2:'전량 +2%',A3:'전량 +3%',B:'70% +1% / 30% 추세',C:'전량 추세'};
try{let raw=localStorage.getItem(STORE);if(raw){let a=JSON.parse(raw);if(!Array.isArray(a.trades))throw Error('invalid data');account=a}}catch(e){storageOK=false}
function savePaper(next){localStorage.setItem(STORE,JSON.stringify(next));account=next}
function paperStats(){let closed=account.trades.filter(x=>x.closedAt),open=account.trades.filter(x=>!x.closedAt),real=closed.reduce((s,x)=>s+x.pnl,0),spent=open.reduce((s,x)=>s+x.amount,0);return {closed,open,real,cash:INITIAL+real-spent,wins:closed.filter(x=>x.pnl>0).length,losses:closed.filter(x=>x.pnl<0).length}}
function paperMsg(s){$('paperMsg').textContent=s}
function paperTime(t){return new Date(t).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})}
async function quote(markets){let all={};for(let i=0;i<markets.length;i+=40){let r=await fetch('/paper/quotes?markets='+encodeURIComponent(markets.slice(i,i+40).join(',')),{cache:'no-store'});if(!r.ok)throw Error('현재가 조회 실패');let d=await r.json();Object.assign(all,d.quotes)}return all}
async function paperBuy(x){
 if(paperBusy||!storageOK||!result||x.strategy==='whale'||!['BUY','WATCH'].includes(x.status)||(x.strategy==='trend'&&x.status!=='BUY'))return;
 let a=Number($('paperAmount').value),s=paperStats();
 if(!Number.isFinite(a)||a<5000||a>s.cash){paperMsg('가상 잔액 내에서 5,000원 이상 입력해 주세요.');return}
 if(s.open.some(t=>t.market===x.market)){paperMsg('이미 모의 보유 중인 종목입니다.');return}
 if(s.closed.some(t=>t.market===x.market&&t.signalTime===x.signal_time)){paperMsg('같은 신호에서 이미 매매한 종목입니다.');return}
 paperBusy=true;renderPaper();paperMsg('새로운 현재가 조회 중...');
 try{let r=await fetch((x.strategy==='trend'?'/trend20/entry':'/paper/entry')+'?market='+encodeURIComponent(x.market)+'&signal='+encodeURIComponent(x.signal_time)+'&hour='+encodeURIComponent(x.hour_signal_time)+'&preset='+encodeURIComponent(x.preset)+'&scan_id='+encodeURIComponent(result.summary[0].scan_id)+'&entry_status='+encodeURIComponent(x.status),{cache:'no-store'});if(!r.ok)throw Error('신호 만료, 진입 범위 제외 또는 가격 조회 실패. 다시 검색해 주세요.');let q=await r.json();if(q.entry_status!==x.status)throw Error('검색 판정 변경. 다시 검색해 주세요.');let p=q.price;if(!Number.isFinite(p)||p<=0)throw Error('유효한 현재가 없음');
 let t={id:Date.now()+'-'+x.market,market:x.market,signalTime:x.signal_time,boughtAt:q.observed_ms,amount:a,buy:p,qty:a/(1+FEE)/p,deadline:q.observed_ms+(x.strategy==='trend'?3:7)*86400000,exitPlan:x.strategy==='trend'?$('exitPlan').value:null,stopPlan:x.strategy==='trend'?Number($('stopPlan').value):null,signalDeadline:q.signal_deadline_ms,preset:q.preset,minDrawdownPct:q.min_drawdown_pct,minDownDays:q.min_down_days,entryStatus:q.entry_status,strategy:x.strategy||'reverse',timeframe:x.strategy==='trend'?x.preset:null};savePaper({trades:[...account.trades,t]});quotes[x.market]=q;paperMsg(x.market+' '+q.entry_status+' 모의매수 완료 (시장 참고가 가정).');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function paperSell(t){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('매도 참고가 조회 중...');
 try{let q=(await quote([t.market]))[t.market];if(!q)throw Error('가격 확인 불가');let proceeds=t.qty*q.price*(1-FEE),pnl=proceeds-t.amount;let closed={...t,closedAt:q.observed_ms,sell:q.price,proceeds,pnl,netPct:100*pnl/t.amount};savePaper({trades:account.trades.map(x=>x.id===t.id?closed:x)});quotes[t.market]=q;paperMsg(t.market+' 모의매도 완료. 순손익 '+num(pnl)+'원');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function paperPartial29(t){
 if(paperBusy||!storageOK||t.exitPlan!=='B'||t.partialDone||t.closedAt)return;
 paperBusy=true;renderPaper();try{let q=(await quote([t.market]))[t.market];if(!q)throw Error('가격 확인 불가');let net=100*(t.qty*q.price*(1-FEE)/t.amount-1);if(net<1)throw Error('현재 순수익 +1% 미도달');let fraction=.7,amount=t.amount*fraction,qty=t.qty*fraction,proceeds=qty*q.price*(1-FEE),pnl=proceeds-amount;
 let closed={...t,id:t.id+'-partial',amount,qty,closedAt:q.observed_ms,sell:q.price,proceeds,pnl,netPct:100*pnl/amount,partialDone:true,exitLeg:'70%'};
 let rest={...t,amount:t.amount-amount,qty:t.qty-qty,partialDone:true,exitLeg:'30%'};
 savePaper({trades:account.trades.flatMap(x=>x.id===t.id?[rest,closed]:[x])});quotes[t.market]=q;paperMsg(t.market+' 70% 분할 모의매도 완료 · 30% 잔량 유지');
 }catch(e){paperMsg(e.message)}finally{paperBusy=false;renderPaper();render()}
}
async function refreshPaper(){
 if(paperBusy||!storageOK)return;paperBusy=true;renderPaper();paperMsg('보유 종목 가격 조회 중...');
 try{let m=paperStats().open.map(x=>x.market);if(m.length)quotes=await quote(m);trendQuotes={};let held=paperStats().open.filter(t=>t.exitPlan==='C'||(t.exitPlan==='B'&&t.partialDone));for(let i=0;i<held.length;i+=20){try{let r=await fetch('/live29/trend?markets='+encodeURIComponent(held.slice(i,i+20).map(t=>t.market).join(',')),{cache:'no-store'});if(r.ok)Object.assign(trendQuotes,(await r.json()).trends)}catch(e){}}paperMsg('가격 및 추세 확인 완료. 직접 매도 필요 여부를 확인하세요.')}catch(e){quotes={};paperMsg(e.message)}finally{paperBusy=false;renderPaper()}
}
function renderPaper(){
 let s=paperStats();$('paperSummary').textContent='가상 원금 1,000,000원 · 잔액 '+num(s.cash)+'원 · 실현손익 '+num(s.real)+'원';$('paperWin').textContent='보유 '+s.open.length+' · 청산 '+s.closed.length+' · 수익 '+s.wins+' · 손실 '+s.losses+' · 승률 '+(s.closed.length?(100*s.wins/s.closed.length).toFixed(2)+'%':'—');
 $('refreshPaper').disabled=paperBusy||!storageOK;
 let cards=s.open.map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 모의 보유'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'돌파형 / 기존 4시간봉':'저점형');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매수가',num(t.buy)+'원');line(c,'투자금(수수료 포함)',num(t.amount)+'원');let q=quotes[t.market];let val=q?t.qty*q.price*(1-FEE)-t.amount:null;line(c,'조회 가격',q?num(q.price)+'원':'—');line(c,'가격 조회',q?paperTime(q.observed_ms):'—');line(c,'평가 순손익',val===null?'—':num(val)+'원 / '+pct(100*val/t.amount),val===null?'muted':val>=0?'green':'red');let deadline=t.exitPlan?t.boughtAt+3*86400000:t.deadline;line(c,t.exitPlan?'72시간 보유 만료':'기존 보유 만료',paperTime(deadline),Date.now()>=deadline?'red':'muted');if(Date.now()>=deadline)c.append(node('p','보유 만료: 직접 모의매도 필요','red'));if(t.exitPlan){line(c,'매도 계획',planNames[t.exitPlan]+' / -'+t.stopPlan+'%');let net=val===null?null:100*val/t.amount;if(net!==null&&net<=-t.stopPlan)c.append(node('p','손절 기준 도달 · 직접 청산 확인','red'));if(net!==null&&t.exitPlan.startsWith('A')&&net>=Number(t.exitPlan[1]))c.append(node('p','목표 순수익 도달 · 직접 청산 확인','green'));if(t.exitPlan==='B'&&!t.partialDone&&net!==null&&net>=1){let pb=node('button','70% 분할 모의매도');pb.disabled=paperBusy||!storageOK;pb.onclick=()=>paperPartial29(t);c.append(pb)}if(t.exitPlan==='C'||(t.exitPlan==='B'&&t.partialDone)){let z=trendQuotes[t.market];line(c,'1시간봉 SMA5 확인',z?z.status==='below'?'이탈 · 직접 청산 확인':z.status==='above'?'유지':'이력 부족':'가격 갱신 시 확인',z?.status==='below'?'red':'muted');if(z?.end)line(c,'추세 확인 시각',z.end);}}let b=node('button','모의매도');b.disabled=paperBusy||!storageOK;b.onclick=()=>paperSell(t);c.append(b);return c});
 $('paperOpen').replaceChildren(...cards);if(!cards.length)$('paperOpen').textContent='보유 종목 없음';
 let hist=s.closed.slice().reverse().slice(0,100).map(t=>{let c=node('article',null,'coin');c.append(node('h3',t.market+' · 청산 완료'));line(c,'매수 조건',names[t.preset]||'기존 기록');line(c,'전략',t.strategy==='trend'?'돌파형 / 기존 4시간봉':'저점형');line(c,'매수 당시 판정',t.entryStatus||'기존 기록 (미기록)');line(c,'매수 / 매도',num(t.buy)+' / '+num(t.sell));line(c,'매수 시각',paperTime(t.boughtAt));line(c,'매도 시각',paperTime(t.closedAt));line(c,'순손익',num(t.pnl)+'원 / '+pct(t.netPct),t.pnl>=0?'green':'red');return c});$('paperClosed').replaceChildren(...hist);if(!hist.length)$('paperClosed').textContent='청산 기록 없음';if(!storageOK)paperMsg('브라우저 저장소 사용 불가 또는 기록 손상. 모의매매를 중단합니다.');
}
$('refreshPaper').onclick=refreshPaper;
$('paperCSV').onclick=()=>{let keys=['market','strategy','timeframe','preset','entryStatus','minDrawdownPct','minDownDays','signalTime','boughtAt','amount','buy','qty','deadline','exitPlan','stopPlan','partialDone','exitLeg','closedAt','sell','proceeds','pnl','netPct'];let esc=v=>'"'+String(v??'').replaceAll('"','""')+'"';let rows=[keys.join(','),...account.trades.map(x=>keys.map(k=>esc(x[k])).join(','))];let url=URL.createObjectURL(new Blob(['\ufeff'+rows.join('\r\n')],{type:'text/csv;charset=utf-8'}));let a=document.createElement('a');a.href=url;a.download='BITHUMB_V3_1_1_LIVE_PAPER_'+new Date().toISOString().slice(0,10)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
renderPaper();

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

# V1.3 research only: separate job state, no changes to live or paper account.
R13_LOCK=threading.Lock()
R13={"status":"idle","phase":"대기","done":0,"total":0,"summary":[],"features":[],"coverage":[],"errors":[]}
R13_ARMS={
    "A":"기존 V1.2.1","B":"고점 대비 40% 하락","C":"하락일 15일 이상",
    "D":"전일 거래량 3배","E":"전일 거래량 4배","F":"진입가 SMA5 위",
    "G":"SMA5 상승 전환","H":"RSI 과매도 반등","I":"MACD 히스토그램 증가",
    "J":"아래꼬리 매수세","K":"양봉+거래량 증가","L":"BTC 상대강도",
    "M":"40% 하락+SMA5 지지","N":"15일 하락+BTC 상대강도","O":"BTC 상대강도+MACD","P":"15일 하락+MACD","Q":"15일 하락+BTC 상대강도+MACD","R":"BTC 상대강도+진입가 SMA5 위"}
def r13patch(**kw):
    with R13_LOCK:R13.update(kw)
def r13error(s):
    with R13_LOCK:R13["errors"].append(str(s))
def r13parallel(items,fn):
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs={pool.submit(fn,x):x for x in items}
        for job in as_completed(jobs):
            m=jobs[job]
            try:yield m,job.result()
            except Exception as e:r13error(str(m)+": "+str(e));yield m,None
            finally:
                with R13_LOCK:R13["done"]+=1
def r13relative(by,btc,t):
    a=by.get(t-DAY);b=by.get(t-6*DAY);x=btc.get(t-DAY);y=btc.get(t-6*DAY)
    if not all((a,b,x,y)) or min(a[4],b[4],x[4],y[4])<=0:return None
    return pct(a[4],b[4])-pct(x[4],y[4])
def r13flags(s,seq,entry,rs):
    # 80 completed hourly bars, all indicators computed before entry.
    closes=[r[4] for r in seq];p=entry[1];a=seq[-1]
    ma=mean(closes[-5:]);prev=mean(closes[-6:-1]);old=mean(closes[-7:-2])
    r0=rsi(closes[:-1]);r1=rsi(closes);_,m0=macd(closes[:-1]);_,m1=macd(closes)
    span=a[2]-a[3];wick=(min(a[1],a[4])-a[3])/span if span>0 else 0
    vr=ratio(a[5],mean([r[5] for r in seq[-21:-1]]))
    flags=dict(A=True,B=s["peak_to_last_pct"]<=-40,C=s["down_days"]>=15,D=s["vol_ratio_d1"]>=3,E=s["vol_ratio_d1"]>=4,F=p>ma,G=ma>prev and prev<=old,H=r0<35 and r1>r0,I=m1>m0,J=wick>=.4 and a[4]>=a[3]+.6*span and span>0,K=a[4]>a[1] and vr is not None and vr>=1.5,L=rs>0)
    flags["M"]=flags["B"] and flags["F"]
    for arm,keys in (("N","CL"),("O","LI"),("P","CI"),("Q","CLI"),("R","LF")):flags[arm]=all(flags[k] for k in keys)
    return flags,dict(hour_sma5=ma,hour_sma5_prev=prev,hour_rsi_prev=r0,hour_rsi=r1,hour_macd_hist_prev=m0,hour_macd_hist=m1,lower_wick_ratio=wick,hour_volume_ratio=vr,relative_btc_5d_pp=rs,entry_vs_sma5_pct=pct(p,ma))
def r13summary(rows,days,period,arm):
    n=len(rows)
    def avg(k):return mean([r[k] for r in rows]) if n else None
    def rate(k):return 100*sum(r["peak_net_pct"]>=k for r in rows)/n if n else None
    return dict(period=period,arm=arm,condition=R13_ARMS[arm],count=n,signals_per_day=n/days if days else None,opportunity_pct=100*sum(r["peak_net_pct"]>0 for r in rows)/n if n else None,hit_3_pct=rate(3),hit_5_pct=rate(5),peak_median_pct=statistics.median(r["peak_net_pct"] for r in rows) if n else None,close_win_pct=100*sum(r["close_net_pct"]>0 for r in rows)/n if n else None,close_mean_pct=avg("close_net_pct"),worst_low_median_pct=statistics.median(r["worst_low_net_pct"] for r in rows) if n else None)
def r131ranges(dates):
    ranges=[]
    for t in sorted(dates):
        left=t-4*DAY;right=t+8*DAY
        if ranges and left<=ranges[-1][1]:ranges[-1][1]=max(right,ranges[-1][1])
        else:ranges.append([left,right])
    return ranges
def r131page(m,left,right,request=None):
    # Cursor progresses to the actual oldest returned candle, not an assumed page size.
    request=request or (lambda market,cursor:api("candles/minutes/60",market=market,count=200,to=kststr(cursor)))
    out={};cursor=right;pages=0;why="reached"
    limit=math.ceil((right-left)/HOUR)+2
    while cursor>left and pages<limit:
        raw=request(m,cursor);pages+=1
        if not raw:why="empty";break
        batch=[parse(r) for r in raw]
        older=[r for r in batch if r[0]<cursor]
        if not older:why="cursor_stalled";break
        for r in older:
            if left<=r[0]<right and r[0]%HOUR==0:out[r[0]]=r
        cursor=min(r[0] for r in older)
    if cursor>left and pages>=limit:why="page_limit"
    return out,pages,why
def r131valid(r):
    return bool(r) and all(math.isfinite(v) for v in r[1:]) and min(r[1:5])>0 and r[2]>=max(r[1],r[4],r[3]) and r[3]<=min(r[1],r[4]) and min(r[5:])>=0
def r131match(real,daily):
    # Certify a day using independently fetched daily OHLC, coin volume and KRW turnover.
    if not real or not r131valid(daily) or any(not r131valid(r) for r in real):return False
    vals=(real[0][1],max(r[2] for r in real),min(r[3] for r in real),real[-1][4],sum(r[5] for r in real),sum(r[6] for r in real))
    return all(math.isclose(a,b,rel_tol=1e-7,abs_tol=1e-9 if i<5 else .01) for i,(a,b) in enumerate(zip(vals,daily[1:])))
def r131fetch(m,dates,daily):
    raw={};filled={};cert=set();bad=set();pages=0;retries=0;reasons={};repairs={}
    ranges=r131ranges(dates)
    for left,right in ranges:
        a,n,why=r131page(m,left,right);pages+=n;raw.update(a);reasons[why]=reasons.get(why,0)+1
        # Retry each incomplete day which cannot be explained by matching daily aggregates.
        for day in range(left,right,DAY):
            real=[raw[day+i*HOUR] for i in range(24) if day+i*HOUR in raw]
            if len(real)<24 and not r131match(real,daily.get(day)):
                try:
                    a,n,why=r131page(m,day,day+DAY);pages+=n;retries+=1
                    raw.update(a);reasons[why]=reasons.get(why,0)+1
                except Exception as e:r13error(m+" "+iso(day)+" retry: "+str(e))
    # Only fill missing hours if independent daily totals match. Never use synthetic candles for entry/crossover.
    for left,right in ranges:
        for day in range(left,right,DAY):
            real=[raw[day+i*HOUR] for i in range(24) if day+i*HOUR in raw]
            if len(real)==24 and all(r131valid(r) for r in real):
                filled.update({r[0]:r for r in real});continue
            if not r131match(real,daily.get(day)):
                bad.add(day)
                filled.update({r[0]:r for r in real if r131valid(r)})
                continue
            cert.add(day)
            prev=daily.get(day-DAY);last=prev[4] if r131valid(prev) else None
            # Pre-first-trade gap requires a known previous daily close.
            for i in range(24):
                t=day+i*HOUR
                if t in raw:
                    r=raw[t];filled[t]=r;last=r[4]
                elif last is not None and last>0:
                    filled[t]=(t,last,last,last,last,0.0,0.0);repairs[t]=True
    return raw,filled,repairs,dict(hour_pages=pages,retry_days=retries,raw_hours=len(raw),certified_gap_days=len(cert),certified_gap_hours=len(repairs),unverified_days=len(bad),page_stops=";".join(k+":"+str(v) for k,v in sorted(reasons.items())))
def r131evaluate(t,s,rs,daily,raw,by,repairs):
    # Only inspect completed bars before the signal. An unknown earlier cross cannot be skipped.
    op=daily.get(t)
    if not r131valid(op):return "daily_missing",None,None
    hit=None
    for i in range(1,24):
        entry=t+i*HOUR
        need=[entry-j*HOUR for j in range(6,0,-1)]
        if any(k not in by for k in need):return "cross_history_missing",None,entry
        # Existing live cross12 requires six actual consecutive hourly candles.
        if any(k not in raw or not r131valid(raw[k]) for k in need):continue
        if cross12(raw,entry)["cross"]:
            hit=i;break
    if hit is None:return "no_cross",None,None
    entry=t+hit*HOUR
    if any(t+i*HOUR not in by for i in range(hit)):return "preentry_missing",None,entry
    if entry not in raw or not r131valid(raw[entry]):return "entry_no_trade" if entry in repairs else "entry_missing",None,entry
    seen=max([raw[t+i*HOUR][2] for i in range(hit) if t+i*HOUR in raw and r131valid(raw[t+i*HOUR])]+[raw[entry][1]])
    if spike121(op[1],seen):return "spike_excluded",None,entry
    # Need only the 80 hours before actual entry and 168 after, not a blanket 272-hour window.
    hk=[entry-i*HOUR for i in range(80,0,-1)];fk=[entry+i*HOUR for i in range(168)]
    if any(k not in by for k in hk):return "indicator_history_missing",None,entry
    if any(k not in by for k in fk):return "outcome_missing",None,entry
    if rs is None:return "relative_missing",None,entry
    history=[by[k] for k in hk];future=[by[k] for k in fk]
    result=result12(future)
    if not result:return "outcome_missing",None,entry
    flags,features=r13flags(s,history,raw[entry],rs)
    lo=min(r[3] for r in future);p=result["buy_price"]
    worst=100*(lo/p*(1-FEE81)/(1+FEE81)-1)
    return "baseline",dict(**s,**features,**result,worst_low_net_pct=worst,preentry_day_high_pct=pct(seen,op[1]),history_gap_hours=sum(k in repairs for k in hk),outcome_gap_hours=sum(k in repairs for k in fk),**{"pass_"+k:v for k,v in flags.items()}),entry
def run13(days):
    begin=time.monotonic()
    try:
        end=int(now().timestamp());universe=sorted({x["market"] for x in api("market/all") if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        btcrows=candles("KRW-BTC",1440,end,200,latest=True)
        if len(btcrows)<days+30:raise RuntimeError("BTC 일봉 부족")
        anchor=btcrows[-1][0]%DAY;cut=day_start(end,anchor)
        if anchor%HOUR or btcrows[-1][0]!=cut:raise RuntimeError("일봉 경계 불일치")
        btc={r[0]:r for r in btcrows}
        starts=[cut-(days+7-i)*DAY for i in range(days)]
        split=starts[days*2//3];early=[t for t in starts if t+8*DAY<=split];late=[t for t in starts if t>=split]
        r13patch(phase="전체 일봉 검색",done=0,total=len(universe),universe=len(universe),first=iso(starts[0]),last=iso(starts[-1]),split=iso(split),early_days=len(early),late_days=len(late))
        setups={};dailies={};cov=[];success=0
        for m,rows in r13parallel(universe,lambda m:candles(m,1440,end,200,latest=True)):
            if not rows or any(r[0]%DAY!=anchor for r in rows):
                cov.append(dict(market=m,daily_status="fetch_error"));continue
            success+=1;by={r[0]:r for r in rows};a=[]
            for t in starts:
                s=setup12(rows,t)
                if s:a.append((t,s,r13relative(by,btc,t)))
            if a:setups[m]=a;dailies[m]=by
            cov.append(dict(market=m,daily_status="ok",daily_signals=len(a)))
        if not success:raise RuntimeError("일봉 수집 실패")
        r13patch(phase="후보 시간봉 페이지 검증",done=0,total=len(setups),market_success=success,daily_signals=sum(len(a) for a in setups.values()))
        details=[];diagnostics=[];audit={x["market"]:x for x in cov};totals={}
        for m,pack in r13parallel(list(setups),lambda m:r131fetch(m,[t for t,s,rs in setups[m]],dailies[m])):
            counts={}
            if pack:raw,by,repairs,meta=pack;audit[m].update(meta)
            else:raw,by,repairs={},{},{}
            for t,s,rs in setups[m]:
                why,row,entry=r131evaluate(t,s,rs,dailies[m],raw,by,repairs) if pack else ("fetch_error",None,None)
                counts[why]=counts.get(why,0)+1;totals[why]=totals.get(why,0)+1
                diagnostics.append(dict(market=m,signal_day=iso(t)[:10],reason=why,entry_time=iso(entry) if entry else None,**s))
                if row is None:continue
                period="EARLY" if t in early else "LATE" if t in late else "GAP"
                details.append(dict(market=m,signal_day=iso(t)[:10],entry_time=iso(entry),entry_ts=entry,period=period,**row))
            audit[m].update({"reason_"+k:v for k,v in counts.items()})
            audit[m]["baseline_signals"]=counts.get("baseline",0)
        details.sort(key=lambda r:(r["entry_ts"],r["market"]))
        summary=[]
        for arm in R13_ARMS:
            last={};kept=[]
            for r in details:
                accept=r["pass_"+arm] and r["entry_ts"]>=last.get(r["market"],0)
                r["trade_"+arm]=accept
                if accept:kept.append(r);last[r["market"]]=r["entry_ts"]+7*DAY
            for name,n in (("ALL",days),("EARLY",len(early)),("LATE",len(late))):
                rows=kept if name=="ALL" else [r for r in kept if r["period"]==name]
                summary.append(r13summary(rows,n,name,arm))
        unknown=sum(v for k,v in totals.items() if k not in ("baseline","no_cross","spike_excluded","entry_no_trade"))
        r13patch(status="done",phase="완료",done=len(setups),summary=summary,features=details,coverage=cov,diagnostics=diagnostics,reason_counts=json.dumps(totals,ensure_ascii=False,sort_keys=True),unknown_signals=unknown,classified_signals=len(diagnostics)-unknown,elapsed=round(time.monotonic()-begin,1),baseline_raw=len(details))
    except Exception as e:r13error(e);r13patch(status="error",phase="실패")
def start13(days):
    with R13_LOCK:
        if R13["status"]=="running":return False
        R13.clear();R13.update(status="running",phase="준비",done=0,total=0,days=days,summary=[],features=[],coverage=[],errors=[],diagnostics=[])
    threading.Thread(target=run13,args=(days,),daemon=True).start()
    return True
def csv13(snapshot):
    # One rectangular CSV with named sections and complete union of columns.
    rows=[]
    excluded={"summary","features","coverage","errors","diagnostics"}
    rows.append(dict(section="settings",**{k:v for k,v in snapshot.items() if k not in excluded},fee_oneway_pct=.04,holding_hours=168))
    for k in ("summary","features","coverage","diagnostics"):
        rows.extend(dict(section=k,**r) for r in snapshot.get(k,[]))
    rows.extend(dict(section="errors",message=e) for e in snapshot["errors"])
    cols=list(dict.fromkeys(k for r in rows for k in r))
    out=io.StringIO();w=csv.DictWriter(out,fieldnames=cols);w.writeheader();w.writerows(rows)
    return out.getvalue().encode("utf-8-sig")
HTML13=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE SWING V1.3.1 RESEARCH</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:1100px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:12px;margin:12px 0}button,a,select{display:inline-block;background:#225b51;color:white;padding:10px;margin:4px;border:1px solid #476073;border-radius:7px;text-decoration:none}button:disabled{opacity:.5}table{border-collapse:collapse;white-space:nowrap;width:100%}td,th{padding:8px;border-bottom:1px solid #324355;text-align:right}td:first-child,th:first-child{text-align:left;position:sticky;left:0;background:#19232e}progress{width:100%}.scroll{overflow:auto}small{color:#b4c4d1;line-height:1.7}</style><h1>REVERSE SWING V1.3.1 RESEARCH</h1><a href="/live12">기존 V1.2.1 실시간·모의매매</a><section><h2>강화 조건 비교</h2><p>기존 일봉 조건 + 첫 1시간봉 SMA5 상향 돌파 + 당일 30% 급등 제외를 기준으로, 추가 조건 및 조합 17개 비교.</p><small>진입은 돌파 다음 시간봉 시가. 편도 0.04%, 각 군 동일 코인 7일 재진입 금지. 실제 페이지 커서 추적 및 누락 일자 재조회. 일봉 OHLC·거래량·거래대금 일치로 검증된 무거래 추정 시간만 지표 및 평가에 보완(진입·돌파에는 실제 시간봉 필수). 누락 원인은 CSV diagnostics에서 확인. 후반 구간은 시간순 별도 확인용이며 독립적인 미래 검증은 아님. 최고가 도달률은 수익 기회, 7일 종료가 승률은 청산 가정. 실제 체결/손절 없음. 현재 상장 종목 기준으로 상장폐지 편향 가능. 연구 결과는 기존 실시간 검색에 자동 반영하지 않음.</small><p>검증 진입일 <select id="days"><option value="30">30일</option><option value="60">60일</option><option value="90" selected>90일</option></select><button id="go">비교 백테스트 시작</button></p><p id="phase">대기</p><progress id="p" value="0" max="1"></progress><p id="audit"></p><pre id="diag" style="white-space:pre-wrap"></pre><pre id="err" style="white-space:pre-wrap"></pre></section><section id="out" hidden><h2>비교 성적</h2><a href="/research131/csv">전체 결과 CSV 받기</a><p><select id="period"><option value="ALL">전체</option><option value="EARLY">전반</option><option value="LATE">후반</option></select></p><div class="scroll" id="table"></div><small>전반/후반 경계 이전 7일 신호는 전반 성적에서 제외. 전체에는 포함. 표본 적은 조건은 성적이 높아도 추가 검증 필요. RSI: 직전 RSI14 35 미만 후 상승. MACD: 히스토그램 증가. 아래꼬리: 봉 길이 40% 이상 및 종가 범위 상위 40%. 양봉+거래량: 전 20시간 평균 대비 1.5배. BTC 상대강도: 완료 일봉 5일 수익률이 BTC보다 높음. F는 진입 시가 기준이며 이후 지지는 보장하지 않음.</small></section><script>
const $=x=>document.getElementById(x);let data=null;
function render(){if(!data)return;let rows=data.summary.filter(x=>x.period===$('period').value);let t=document.createElement('table'),h=t.insertRow();for(let s of ['조건','건수','하루 평균','기회율%','+3% 도달','+5% 도달','최고가 중앙%','7일 승률%','7일 평균%','최저가 중앙%']){let th=document.createElement('th');th.textContent=s;h.append(th)}for(let x of rows){let r=t.insertRow();for(let v of [x.arm+' '+x.condition,x.count,x.signals_per_day,x.opportunity_pct,x.hit_3_pct,x.hit_5_pct,x.peak_median_pct,x.close_win_pct,x.close_mean_pct,x.worst_low_median_pct]){let cell=r.insertCell();cell.textContent=typeof v==='number'?Number(v.toFixed(2)):v??'—'}if(x.count<20)r.cells[1].style.color='#ffc978'}$('table').replaceChildren(t)}
$('period').onchange=render;$('go').onclick=async()=>{try{$('go').disabled=true;let r=await fetch('/research131/start?days='+$('days').value,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('이미 실행 중');data=null;$('out').hidden=true;poll()}catch(e){$('phase').textContent=e.message;$('go').disabled=false}};
async function poll(){try{let r=await fetch('/research131/status',{cache:'no-store'}),s=await r.json();$('go').disabled=s.status==='running';$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('p').max=Math.max(1,s.total);$('p').value=s.done;$('audit').textContent='전체 '+(s.universe??'—')+'종목 · 일봉 성공 '+(s.market_success??'—')+' · 일봉 신호 '+(s.daily_signals??'—')+' · 시간봉 기본 신호 '+(s.baseline_raw??'—')+' · 오류 '+s.error_count+' · 기간 '+(s.first??'—')+' ~ '+(s.last??'—');$('err').textContent=s.errors.join('\n');$('diag').textContent=s.reason_counts?'판정 가능 '+s.classified_signals+' · 미확인 '+s.unknown_signals+'\n'+s.reason_counts:'';if(s.status==='done'){data=s;$('out').hidden=false;render()}else{$('out').hidden=true;data=null}}catch(e){$('phase').textContent='연결 확인: '+e.message}}
poll();setInterval(poll,2500);
</script></html>"""

APP_LINK_TEST=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>빗썸 앱 연결 테스트 T0.1</title><style>body{background:#10151d;color:#edf2f7;font:16px system-ui;max-width:620px;margin:auto;padding:18px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:16px;margin:14px 0}a,button{display:block;background:#225b51;color:white;border:1px solid #476073;padding:14px;margin:10px 0;border-radius:8px;text-decoration:none;font:inherit;text-align:center}button{width:100%}small{color:#b4c4d1;line-height:1.7}input{font:inherit;color:#edf2f7;background:#10151d;border:1px solid #476073;padding:9px;border-radius:7px;max-width:90%}#msg{color:#ffcf87;white-space:pre-wrap}</style><h1>빗썸 앱 연결 테스트 T0.1</h1><p>안드로이드 Chrome에서 실행해 주세요. 앱 실행만 시험합니다. 로그인 정보, API 키, 주문 요청은 전혀 수집하거나 전달하지 않습니다.</p><section><h2>1. 설치된 앱 실행</h2><a id="main" href="intent:#Intent;action=android.intent.action.MAIN;category=android.intent.category.LAUNCHER;package=com.btckorea.bithumb;end">① 빗썸 앱 실행 시도</a><small>앱의 런처를 지정하는 시험입니다. 브라우저에서 허용되지 않으면 열리지 않을 수 있습니다.</small><a id="scheme" href="intent://open/#Intent;scheme=bithumb;package=com.btckorea.bithumb;end">② 앱 전용 주소 실행 시도</a><small>bithumb 스킴은 공식 확인되지 않은 시험용 후보입니다. 설치된 빗썸 패키지로만 요청합니다.</small></section><section><h2>2. 종목 직접 연결</h2><p>테스트 종목 <input id="coin" value="MANTA" maxlength="20" aria-label="코인 심볼"></p><button id="copy">종목명 복사</button><a id="trade" href="intent://www.bithumb.com/trade/order/MANTA_KRW#Intent;scheme=https;package=com.btckorea.bithumb;end">③ 해당 종목 앱 화면 실행 시도</a><small>공식 웹 거래 주소를 설치된 앱으로 전달하는 시험입니다. 앱이 해당 주소를 지원하지 않으면 실행되지 않습니다.</small></section><p id="msg" role="status">버튼을 눌러 앱이 열리는지 확인해 주세요. 웹 거래 화면으로 자동 전환하지 않습니다.</p><section><a href="https://play.google.com/store/apps/details?id=com.btckorea.bithumb" target="_blank" rel="noopener noreferrer">④ 공식 Play 스토어에서 열기 (대안)</a><small>Play 스토어의 빗썸 페이지에서 직접 열기를 누르는 방법입니다.</small><a href="/live142">기존 V1.4.2 스캐너 돌아가기</a></section><script>
const $=id=>document.getElementById(id);
const fallback=encodeURIComponent(location.origin+location.pathname+"?failed=1");
function withFallback(a){a.href=a.getAttribute("href").replace(/;end$/,";S.browser_fallback_url="+fallback+";end")}
withFallback($("main"));withFallback($("scheme"));
function symbol(){return $("coin").value.trim().toUpperCase().replace(/[^A-Z0-9]/g,"").slice(0,20)||"MANTA"}
function trade(){ $("trade").setAttribute("href","intent://www.bithumb.com/trade/order/"+symbol()+"_KRW#Intent;scheme=https;package=com.btckorea.bithumb;end");withFallback($("trade")); }
$("coin").addEventListener("input",trade);trade();
$("copy").onclick=async()=>{try{await navigator.clipboard.writeText(symbol());$("msg").textContent=symbol()+" 복사 완료. 앱이 열리면 검색창에 붙여넣으세요."}catch(e){$("coin").select();$("msg").textContent="자동 복사가 차단되었습니다. 선택된 종목명을 직접 복사해 주세요."}};
if(new URLSearchParams(location.search).has("failed"))$("msg").textContent="앱 연결을 처리할 수 없어 테스트 페이지로 돌아왔습니다. 다른 번호를 시험해 주세요.";
</script></html>"""

# Independent fixed-entry research; V1.4.3 live state is never modified.
import base64, zlib
D15_SOURCE = "REVERSE_SWING_V1_3_1_RESEARCH_202611260705.csv / features / trade_A=True"
D15_ENTRIES = json.loads(zlib.decompress(base64.b64decode("eNptWUtyG7kSvMusKQRQ+C9bVIviEynykZQte8L3v8ZkVaEBNmUvrAhFGuj6ZWbB//77z/vl59Pz8z8bl0umlIq1G+cNhc2TM8XF7OufjaKuhy+FBRcCYJWMd5tgvPXVhQX1Pu1vpwWH0+wmFGvs5qka653zZQG+zPNZcdG5xNcWk2gTTUquhrTAzr8uiirRMyonU4ucFnBrXmBvrzeF1ZCJD7PGe8QQTK5Etd+6hFqjfJsjU8rGmhJtTT2G03n+EFhxNeqnOQ4hmFCTJ7/gtpNGUGIInLhk8fGbJ28iURohPO8Ph4aLchxZUyM+zhnCFYUW4OX0rKkryRdOsUM2OIpqPFGwcQH+r6W4ZJf5wETyfQkpxp8O+5i1YtVmy8ehDtahsviZa009KdtfV8URpxjXVo/jgrHJIVEd9TZv3xUXnOISdwCuLcW6NOKdPhZcIs6LByJvKBkXfQg9zdOPQ4PVyDWjYoj4UOttiPFb+hacy8bnTTIpovPyt+RVpJm0tilsvAk5e9dz/LHVRqmVtFG8qYFjBar6h8xV66q1S+boe+Ze5ue9nFedo8ql8DZFrgW6GYOBej+Gga6tOmZkKo4ktEstdy16UlSKMhUpynHZZJtDdD13L+cGq47kA9Fw3KGYJSRgge0UVEi6Hd0kI+FjoNjvu56n7ay46mwRHG7nYKONMfc7nw+fl4YjndhkuI0z6prJPfAEo6QO2RmPiS0GietksjtfG8hT0HmNBTX1gUASI2cXmVfc56WDUzXRCUHl4KmHcJx/NFgWkvCmUEZqkeAIHurH+YYqEiamkDhpDj9z9b2TjtPH7TKtoMUESxxDsCGN1pwOC0rZBNcWXB4LedtDvU0/phWMDE8hyu8QRE7j2l8Koyx3Igge1srlGvw6zacPhfkkNBzwUfg01CJZTEjnzbd2aQQVNd7MAgND2EHD88f11IBeWr0k4oZDQ8UaR8O9fWifUyKBoTG0L21OblBn73JKRSgMRJ1JuC6CI2wv7fyyazNGqZLohOVOR21RQzdI7PM6H+brdYE2AmWCQl9VKFY/8vjcrs6WWn1j8DwUtdo0Tvw5fZ1XwGjQmdwH5Pkjv/U7FZ0LlCxEbisLgq7he4Ebjgy5qPfihn7e6+nw0nBRaRvJ4f7jSoM2eiC/nlewElkuuLlQEhq9d2iFK0kogEJQnQ21DDJ+mW7TGia5e2Llyzi+N8LP0/o4LkTZoG4INg/+3J5O73ulC6rcgJIW63k4kOOhUVzgBSZhOCeNVYyzaLb+gZc1CpQhn+fRpyWWR40CLurnhayShyhotP1xflnBcGUUVIRY9Ip9TTpD3sba3EJMGwJfgMVHYQe7g9WlU1wQdg8G7V8Hhb59HqeGUwIi8JRYqMCWKo9eVpQjrT9anRMSmd36pM3ndqfzQds4QIaE2wsopH/d78u24aJMuBMxribjytzrdT5pQmAgJCGZm05UIFrKg8p2x3ar96SMl8lz5igGW7/Tj/dZPIBHN7lNZgtQQ+/g6bz3axjLOkKBNIfBtNfpdV7BnFYfDc8j9ujuGFY1cZEqDyLsRE09hv3xawVzyeulK8ZTCwhQ8SQEFZPjKwvoeMzg7W1ewXAU5wPflz2y3duXfjdYFYHybHLEsYXi45j96eu6wkGmg9JsxQV3bf66W+OI2xxh3M/gpSnFAoI6Mop9kfNpUMSlDUM/TGoPZ1J8hWProZ5+fjx8HZLhuZeiDX7E+ut1v743kkPVcCaurzjTPQoykEIRMs2iAo7dWa//r8vneQFmZfaqDQB8db0B5v1ubn0XnIwsRadsEvjefvH79dhgQdUxB9IDsT/cWcrdbX9sBQ5qFwv7wCcwQU4B9NFvvrYPhJfVwkVlWbQnNpIeye1rgUkjB54Ltg3sBfxYLBooqwNcYrAEffKjvK1sGH/pKTkLUhthCPIdE1+WCIoSbBUGgFX0kIrRBddLg9VQGiUW/F2xPwz6X9yHjzmrhaIs84/VIQ3f1hyPj1WJ0+QgjQLdo7vF6NT6CXuMUhgWHf4yh10s9C877VpNU5R8+KB7Ebgp41N7uY4L52T1nSTSBELELlb8X1g46x4DFuY2hlNBCDQY9nb5PLZKVKu7QrJNTqgi/vxNZAGUSLAQKv97l8d0f55XoFz1LEcQ8MHEl9NxofaqCxSBAxAIZjxigcjfyadWORB9wtsHCyhWsvyX+YFECE25KndjH0xYGO7aeNe8R7DqLkGguqckJvoyXP5uPnWk2FVPqhisQ2kUZff21WBFlwHuBbWr+U7NhoZiaWw7dyzyjdUUmJ5hMfcfDVdUMuBQotwL25fGlvx1PmiZA6RE1QAelPOdCGv8mI/p+rad2t1QMV0zRF9wtMM/Jnp4Zei4AMKS3ARIa3ncWAFL2oj4RK1LtazDQyUvpwZUpswtZNA9vnU0zuKqFxzoQ0cdrsbV8QiCfn2+qdzjs9XQcw/KkQRbPaZg+7ZvLQEZ5JaALWTqjSaE7IYHWgxVgJxJZyvzUjRiAsIwEO+3FbC0OoPowhCR1+2NGiyJ+BaZ0WxQubsMdtIKwamjAsJJChMC7gV5XYG8cUzO2F0q7wC9Yf6/Qlk2IZE/rWS2AHcNs8Jl0iuZP/CL8PA0wDg1VDJ1sA+hWuq1/dp+rFABgqGnWVjHu0XtbTlNXlVAlbIU8LIMvzoYZrdvMK+dB14mkd4S73aW46lVPwRhBFBgEu9osysj1O37c4NFVSvYt6RUBKvfTzscVyh+POD11oN+7VgdrsuVUQqK9MJWiYjbFIbW/56vS6RNFPTJApILSzX22/M8tXrhEtU1E6TyUNI4tL7TeIhOVk0wQAns+WLKpcPeF0ewwMB8WIRFPLwfnXR6bl+HMqamRR7ylwMUvZ92uh7bxGIjaJRbVP9KwR7WncN8WT6uapMEnWxQhbM0HiTPp+apApOZMLOMNoKGcMTxPHCczqcF2BrFJeL0oUu8G8p7eW9VS/quiqWhPTTBZ+bhvfvaGlJ/WyF+4kLaafD31PouWx3/xA8wmMYQ8X1+FO1j97kCIiPyJulZ3spALs8+Cw4Uys8XgQ+MY0G7k6zlZjGvT6IfHFwXwf20PlDfkWFvChvev70PPB6JZSjDltwV5dwVIeursy9SZ6FHdhMPvh5OTR4nwPREHA1cKbxPT+JthcKUNQ6lHEaJl91qgTHlkb6Ikx9SuWw5CyzKeggt4Ibuh/1chLyhvC4S/JwEr3nHP+d+mkxlIs1JNvywMKTvdX9Y4YpKkKz/MIpjLOfj0jFZJgTEJ3Tm4Urr4ysso2Q+kNesUgqBHCmxu4YqOhy5NDOCbwjDSE4/pv55JbeF3glVoQx2KC7M1Y8mLKVZumr1zR6rjB3h8uNe66vi9cUhiVKB/KCig66mZhFDCUVJMjvGRX7dL3cPIosuF33cr/wGK+1cIKPDjvTXHwAl0RgQ0kiwHA/LubItJanWO5kluDq29kOxlke+BRf4JUlfCrDzDjt+PTdGKNnqC6SP+vAJ0hmcdX1vua5Wlxg+R0Wm+HC3y+7buMG3aNuT7kSVvWD57jnBlkk+T56omPHxuZ4e/8OAgW25MyyC/BiAqg/38Hr4/FoB0fspMJnzU/LQ1P2pTW/NzfYtFBOQvlj+/PkPZR/nPQ==")))
D15_LOCK=threading.Lock()
D15={"status":"idle","done":0,"total":132,"rows":[],"errors":[]}
def d15row(entry,raw,cut):
    m,t,p,ref=entry
    out=dict(market=m,entry_time=iso(t),buy_price=p)
    for d in range(1,16):
        end=t+d*DAY;r=raw.get(end-HOUR)
        out["day"+str(d)]=round(100*(r[4]/p*(1-FEE81)/(1+FEE81)-1),6) if end<=cut and r131valid(r) else None
    actual=out["day7"]
    out["day7_check"]="missing" if actual is None else "ok" if abs(actual-ref)<.0001 else "mismatch"
    # Keep the original measured D7, rather than silently replacing it.
    out["day7"]=ref
    if out["day7_check"]=="mismatch":
        for d in range(1,16):
            if d!=7:out["day"+str(d)]=None
    out["mature_days"]=max(0,min(15,(cut-t)//DAY))
    out["missing_days"]=sum(out["day"+str(d)] is None for d in range(1,out["mature_days"]+1))
    return out
def d15market(item,cut):
    m,entries=item;raw={};ranges=[]
    for _,t,_,_ in sorted(entries,key=lambda r:r[1]):
        end=min(t+15*DAY,cut)
        if end<=t:continue
        if ranges and t<=ranges[-1][1]:ranges[-1][1]=max(end,ranges[-1][1])
        else:ranges.append([t,end])
    errors=[]
    for left,right in ranges:
        try:
            a,n,why=r131page(m,left,right);raw.update(a)
            if why!="reached":errors.append(m+" "+iso(left)+": "+why)
        except Exception as e:errors.append(m+" "+str(e))
    return [d15row(e,raw,cut) for e in entries],errors
def d15run():
    cut=int(now().timestamp())//HOUR*HOUR
    groups={}
    for r in D15_ENTRIES:groups.setdefault(r[0],[]).append(r)
    rows=[];errors=[]
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs={pool.submit(d15market,item,cut):item for item in groups.items()}
            for job in as_completed(jobs):
                m,entries=jobs[job]
                try:
                    a,b=job.result();rows.extend(a);errors.extend(b)
                except Exception as e:
                    errors.append(m+" "+str(e));rows.extend(d15row(r,{},cut) for r in entries)
                with D15_LOCK:D15.update(done=D15["done"]+1)
        rows.sort(key=lambda r:(r["entry_time"],r["market"]))
        with D15_LOCK:D15.update(status="done",rows=rows,errors=errors,cut=iso(cut))
    except Exception as e:
        with D15_LOCK:D15.update(status="error",errors=[str(e)])
def d15start():
    with D15_LOCK:
        if D15["status"]=="running":return False
        D15.clear();D15.update(status="running",done=0,total=len({r[0] for r in D15_ENTRIES}),rows=[],errors=[])
    threading.Thread(target=d15run,daemon=True).start()
    return True
HTML_D15=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE SWING 15일 가격 추적 T1</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;margin:12px}button,a{display:inline-block;color:white;background:#225b51;padding:11px;border:1px solid #476073;border-radius:7px;text-decoration:none;margin:4px}button:disabled{opacity:.5}progress{width:100%}table{border-collapse:collapse;white-space:nowrap}th,td{padding:8px;border-bottom:1px solid #324355;text-align:right}th:first-child,td:first-child{position:sticky;left:0;background:#19232e;text-align:left}.pos{color:#72ddb0}.neg{color:#ff9b9b}.scroll{overflow-x:auto}small{color:#b4c4d1}</style><h1>15일 가격 추적 T1</h1><p>기존 90일 V1.3.1 기본 진입 174건 · 최초 매수가 고정</p><small>기존 기본 진입 기록이며 현재 A/B/C/D 및 최근 20일 급등 제외를 새로 적용하지 않습니다. 매수 후 24시간 단위 종가의 누적 순수익률(편도 0.04%). 공란은 미경과 또는 미수집. 7일차는 원본 CSV 값 유지. 재조회 7일차 불일치 시 다른 일차 값은 표시하지 않음. 자동 주문 없음.</small><p><button id="go">1~15일 가격 조회</button><a href="/live143">기존 스캐너</a><a id="csv" href="/track15/csv" hidden>결과 CSV</a></p><progress id="p" max="132" value="0"></progress><p id="msg">대기</p><div class="scroll" id="out"></div><details><summary>수집 오류</summary><pre id="err" style="white-space:pre-wrap"></pre></details><script>
const $=x=>document.getElementById(x);let shown=false;
$('go').onclick=async()=>{try{$('go').disabled=true;let r=await fetch('/track15/start',{method:'POST'});if(!r.ok)throw Error(r.status);shown=false;await poll()}catch(e){$('msg').textContent=e.message;$('go').disabled=false}};
function render(s){let t=document.createElement('table'),h=t.insertRow();for(let label of ['종목','진입일시','진입가',...Array.from({length:15},(_,i)=>(i+1)+'일'),'7일 검증','누락']){let th=document.createElement('th');th.textContent=label;h.append(th)}for(let x of s.rows){let r=t.insertRow();for(let v of [x.market.replace('KRW-',''),x.entry_time,x.buy_price])r.insertCell().textContent=v;for(let i=1;i<=15;i++){let v=x['day'+i],cell=r.insertCell();cell.textContent=v==null?'—':(v>0?'+':'')+v.toFixed(2)+'%';if(v!=null)cell.className=v>0?'pos':v<0?'neg':''}r.insertCell().textContent=x.day7_check;r.insertCell().textContent=x.missing_days}$('out').replaceChildren(t)}
async function poll(){try{let s=await(await fetch('/track15/status',{cache:'no-store'})).json();$('go').disabled=s.status==='running';$('p').max=s.total||132;$('p').value=s.done;$('csv').hidden=s.status!=='done';$('msg').textContent=s.status==='done'?'완료 '+s.rows.length+'건 · 오류 '+s.errors.length+' · 15일 완료 '+s.rows.filter(x=>x.mature_days===15&&x.missing_days===0).length+'건':s.status==='running'?'조회 중 '+s.done+'/'+s.total:s.status==='error'?'조회 실패':'대기';$('err').textContent=s.errors.join('\n');if(s.status==='done'&&!shown){render(s);shown=true}if(s.status==='running'){shown=false;$('out').replaceChildren()}}catch(e){$('msg').textContent='연결 확인: '+e.message}}poll();setInterval(poll,2500);
</script></html>"""

# V2.0 experimental trend-pullback; independent strategy, same paper account.
TREND20_NAME={"T4":"4시간봉","T1":"1시간봉","T415":"4시간봉 15/20 음봉","T430":"4시간봉 20/30 음봉"}
TREND_DIRECT=("T415","T430","T42ALL")
TREND20_NAME["T42ALL"]="4시간봉 42개 · 30~36개 비교"
TREND20_NAME["U20ALL"]="4시간봉 양봉 10~20개 비교"
TREND20_NAME["P20ALL"]="20봉 상승 후 눌림 최종 비교"
TREND20_NAME["G20ALL"]="BTC 상승장 필터 적용 전후 비교"
TREND20_NAME["R415ALL"]="15/20 음봉 하락폭·거래량 비교"
def trend20bars(raw):
    # Explicit KST 00/04/08/12/16/20 alignment, no synthesized missing hours.
    anchor=-9*HOUR;step=4*HOUR;out={}
    for t in raw:
        b=(t-anchor)//step*step+anchor
        if b in out:continue
        rr=[raw.get(b+i*HOUR) for i in range(4)]
        if all(r131valid(r) for r in rr):
            out[b]=(b,rr[0][1],max(r[2] for r in rr),min(r[3] for r in rr),rr[-1][4],sum(r[5] for r in rr),sum(r[6] for r in rr))
    return out
def trend20setup(by,end,step):
    # end is an exclusive completed-bar boundary.
    rr=[by.get(end-i*step) for i in range(25,0,-1)]
    if not all(r131valid(r) for r in rr):return "history",None
    n=0
    for r in reversed(rr):
        if r[4]>=r[1]:break
        n+=1
    if not 2<=n<=4:return "no_setup",None
    prior=rr[-n-20:-n];pull=rr[-n:]
    gain=pct(prior[-1][4],prior[0][1]);peak=max(r[2] for r in prior)
    depth=-pct(min(r[3] for r in pull),peak)
    close_depth=-pct(pull[-1][4],peak)
    vol=mean([r[5] for r in prior]);pv=mean([r[5] for r in pull])
    if gain<10 or not 3<=close_depth<=10 or depth>10 or vol<=0 or pv>=vol:return "no_setup",None
    return "setup",dict(prior_gain_pct=gain,pullback_pct=close_depth,pullback_low_pct=depth,pullback_bars=n,pullback_volume_ratio=pv/vol,prior_peak=peak,pullback_low=min(r[3] for r in pull))
def trend21setup(by,end,step,total=20,minimum=15):
    rr=[by.get(end-i*step) for i in range(total,0,-1)]
    if not all(r131valid(r) for r in rr):return "history",None
    n=sum(r[4]<r[1] for r in rr)
    if n<minimum:return "no_setup",None
    peak=max(r[2] for r in rr);low=min(r[3] for r in rr)
    avg=mean([r[5] for r in rr[:-1]])
    return "setup",dict(down_bars=n,total_bars=total,window_change_pct=pct(rr[-1][4],rr[0][1]),pullback_pct=-pct(rr[-1][4],peak),prior_peak=peak,pullback_low=low,last_4h_volume_ratio=rr[-1][5]/avg if avg>0 else None,setup_end=iso(end))
def trend20signal(raw,bars,cut,preset,direct_anchor=None):
    step=4*HOUR if preset=="T4" or preset in TREND_DIRECT else HOUR
    anchor=direct_anchor if preset in TREND_DIRECT else -9*HOUR if preset=="T4" else 0
    setup=(lambda b,e,s:trend21setup(b,e,s,42,30)) if preset=="T42ALL" else (lambda b,e,s:trend21setup(b,e,s,30,20)) if preset=="T430" else trend21setup if preset=="T415" else trend20setup
    def boundary(t):return (t-anchor)//step*step+anchor
    # The preceding setup must be complete BEFORE the confirming hourly candle starts.
    before=boundary(cut-HOUR);cur=boundary(cut)
    why,s=setup(bars,before,step)
    hr=[raw.get(cut-i*HOUR) for i in (4,3,2,1)]
    if not all(r131valid(r) for r in hr):return "history",None
    avg=mean([r[5] for r in hr[:-1]])
    last=hr[-1];ratio=last[5]/avg if avg>0 else 0
    if s and last[4]>last[1] and last[4]>hr[-2][2] and ratio>=1.2:
        # No BUY if the confirming close has already exceeded the prior swing high.
        if preset in TREND_DIRECT or (last[4]<=s["prior_peak"] and last[3]>=s["prior_peak"]*.90):
            return "BUY",dict(s,confirm_volume_ratio=ratio,signal_close=last[4],status="BUY")
    why2,w=setup(bars,cur,step)
    if w:return "WATCH",dict(w,confirm_volume_ratio=ratio,signal_close=last[4],status="WATCH")
    # A 4h setup may still be actionable during the same forming 4h bar.
    if s and before==cur:return "WATCH",dict(s,confirm_volume_ratio=ratio,signal_close=last[4],status="WATCH")
    return ("history" if "history" in (why,why2) else "no_setup"),None
def trend20net(p,q):return 100*(q/p*(1-FEE81)/(1+FEE81)-1)
def trend20out(raw,t,hours):
    seq=[raw.get(t+i*HOUR) for i in range(hours)]
    if not all(r131valid(r) for r in seq):return None
    p=seq[0][1]
    return dict(close=trend20net(p,seq[-1][4]),peak=trend20net(p,max(r[2] for r in seq)),low=trend20net(p,min(r[3] for r in seq)))
def trend20eval(raw,cut,preset,back,direct=None,direct_anchor=None):
    bars=direct if preset in TREND_DIRECT else trend20bars(raw) if preset=="T4" else raw
    if not back:
        why,s=trend20signal(raw,bars,cut,preset,direct_anchor)
        return why,s
    rows=[];missing=0;signals=0;out_missing=0;skipped=0;price_skipped=0;last=0;valid=0
    # Fifteen entry days, all with a completed 72h outcome window.
    first=cut-18*DAY
    for t in range(first,cut-3*DAY,HOUR):
        why,s=trend20signal(raw,bars,t,preset,direct_anchor)
        if why=="history":missing+=1;continue
        valid+=1
        if why!="BUY":continue
        signals+=1
        if t<last:skipped+=1;continue
        # Match the live entry-price guard at the simulated next-hour open.
        if preset not in TREND_DIRECT and r131valid(raw.get(t)) and not s["pullback_low"]<=raw[t][1]<=s["prior_peak"]:price_skipped+=1;continue
        # Reserve the same 72h window even if the future data are missing.
        last=t+3*DAY
        a=trend20out(raw,t,24);b=trend20out(raw,t,72)
        if not a or not b:out_missing+=1;continue
        rows.append(dict(s,signal_time=iso(t),entry_ts=t,buy_price=raw[t][1],day1_close_pct=a["close"],day1_peak_pct=a["peak"],day3_close_pct=b["close"],day3_peak_pct=b["peak"],day3_low_pct=b["low"]))
    return rows,dict(valid_hours=valid,missing_hours=missing,buy_signals=signals,overlap_skipped=skipped,price_skipped=price_skipped,outcome_missing=out_missing)
def trend25setup(bars,end,step):
    why,s=trend21setup(bars,end,step,20,0)
    if not s:return why,None
    rr=[bars[end-i*step] for i in range(20,0,-1)]
    up=sum(r[4]>r[1] for r in rr)
    n=0
    for r in reversed(rr):
        if r[4]>=r[1]:break
        n+=1
    if up<10 or not 1<=n<=3:return "no_setup",None
    peak=max(r[2] for r in rr[:-n])
    depth=-pct(rr[-1][4],peak)
    if not 2<=depth<=10:return "no_setup",None
    return "setup",dict(s,up_bars=up,flat_bars=sum(r[4]==r[1] for r in rr),pullback_bars=n,prior_peak=peak,pullback_pct=depth)
def trend26regimes(bars,cut,anchor):
    step=4*HOUR;out={};cache={}
    for t in range(cut-18*DAY,cut-3*DAY,HOUR):
        end=(t-anchor)//step*step+anchor
        if end not in cache:
            rr=[bars.get(end-i*step) for i in range(25,0,-1)]
            if not all(r131valid(r) for r in rr):cache[end]=dict(btc_regime="unknown")
            else:
                closes=[r[4] for r in rr];ma=mean(closes[-20:]);prev=mean(closes[:20])
                cache[end]=dict(btc_regime="up" if closes[-1]>ma and ma>prev else "not_up",btc_close=closes[-1],btc_sma20=ma,btc_sma20_previous=prev,btc_regime_end=iso(end))
        out[t]=cache[end]
    return out
def trend26group(g):
    return dict(comparison_id=g,minimum_gain_pct=g%100,market_filter="btc_up" if g>=100 else "none")
def trend27group(g):
    return dict(comparison_id=g,minimum_down_bars=15,refinement=("base","depth","volume","both")[g])
def trend27match(s,g):
    if not s or s["down_bars"]<15:return False
    depth=-20<=s["window_change_pct"]<=-10
    vol=s["last_4h_volume_ratio"]
    volume=vol is not None and .5<=vol<1.5
    return (True,depth,volume,depth and volume)[g]
def trend23eval(raw,bars,cut,anchor,bull=False,pull=False,regimes=None,refine=False):
    # Common hourly BUY observations, independent 72h reservation for every minimum.
    groups=range(4) if refine else (3,5,10,103,105,110) if regimes is not None else (3,5,10) if pull else range(10,21) if bull else range(30,37)
    count_key="window_change_pct" if pull else "up_bars" if bull else "down_bars"
    min_key="comparison_id" if refine or regimes is not None else "minimum_gain_pct" if pull else "minimum_up_bars" if bull else "minimum_down_bars"
    trades={g:[] for g in groups}
    audit={g:dict(valid_hours=0,missing_hours=0,buy_signals=0,overlap_skipped=0,outcome_missing=0,price_skipped=0) for g in groups}
    last={g:0 for g in groups}
    step=4*HOUR
    cache={}
    def get_setup(end):
        if end not in cache:
            if refine:cache[end]=trend21setup(bars,end,step,20,15)
            elif pull:cache[end]=trend25setup(bars,end,step)
            elif bull:
                why,s=trend21setup(bars,end,step,20,0)
                if s:
                    rr=[bars[end-i*step] for i in range(20,0,-1)]
                    s.update(up_bars=sum(r[4]>r[1] for r in rr),flat_bars=sum(r[4]==r[1] for r in rr))
                cache[end]=(why,s)
            else:cache[end]=trend21setup(bars,end,step,42,0)
        return cache[end]
    for t in range(cut-18*DAY,cut-3*DAY,HOUR):
        before=(t-HOUR-anchor)//step*step+anchor
        cur=(t-anchor)//step*step+anchor
        w,s=get_setup(before);w2,s2=get_setup(cur)
        hr=[raw.get(t-i*HOUR) for i in (4,3,2,1)]
        ok=all(r131valid(r) for r in hr)
        ratio=0;confirm=False
        if ok:
            avg=mean([r[5] for r in hr[:-1]])
            ratio=hr[-1][5]/avg if avg>0 else 0
            confirm=hr[-1][4]>hr[-1][1] and hr[-1][4]>hr[-2][2] and ratio>=1.2
        outcomes=None;checked=False
        for g in groups:
            a=audit[g]
            threshold=g%100 if regimes is not None else g
            match=trend27match(s,g) if refine else bool(s and s[count_key]>=threshold)
            watch=trend27match(s2,g) if refine else bool(s2 and s2[count_key]>=threshold)
            buy=ok and confirm and match
            # Same history classification as the earlier single-preset signal function.
            if not ok or (not buy and not watch and not (match and before==cur) and "history" in (w,w2)):
                a["missing_hours"]+=1;continue
            regime=regimes.get(t,{"btc_regime":"unknown"}) if regimes is not None else {}
            if regimes is not None and g>=100 and regime["btc_regime"]=="unknown":
                a["missing_hours"]+=1;continue
            a["valid_hours"]+=1
            if not buy:continue
            if regimes is not None and g>=100 and regime["btc_regime"]!="up":continue
            a["buy_signals"]+=1
            if t<last[g]:a["overlap_skipped"]+=1;continue
            last[g]=t+3*DAY
            if not checked:
                one=trend20out(raw,t,24);three=trend20out(raw,t,72)
                outcomes=(one,three) if one and three else None
                checked=True
            if not outcomes:a["outcome_missing"]+=1;continue
            one,three=outcomes
            trades[g].append(dict(s,**(trend27group(g) if refine else trend26group(g) if regimes is not None else {min_key:g}),**regime,confirm_volume_ratio=ratio,signal_close=hr[-1][4],status="BUY",signal_time=iso(t),entry_ts=t,buy_price=raw[t][1],day1_close_pct=one["close"],day1_peak_pct=one["peak"],day3_close_pct=three["close"],day3_peak_pct=three["peak"],day3_low_pct=three["low"]))
    return trades,audit
def runtrend23(bull=False,pull=False,gate=False,refine=False):
    begin=time.monotonic()
    preset="R415ALL" if refine else "G20ALL" if gate else "P20ALL" if pull else "U20ALL" if bull else "T42ALL"
    min_key="comparison_id" if refine or gate else "minimum_gain_pct" if pull else "minimum_up_bars" if bull else "minimum_down_bars"
    total_bars=20 if refine or bull or pull else 42
    try:
        end=int(now().timestamp());cut=end//HOUR*HOUR;step=4*HOUR
        universe=sorted({x["market"] for x in api("market/all") if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        if not universe:raise RuntimeError("시장 목록 없음")
        probe=candles("KRW-BTC",240,end,8,latest=True)
        if len(probe)<2 or any(r[0]%step!=probe[-1][0]%step for r in probe) or probe[-1][0]+step<=cut-step:raise RuntimeError("BTC 240분봉 경계 확인 실패")
        anchor=probe[-1][0]%step
        with state_lock:scan_id=state["scan_id"]
        regimes=None
        if gate:
            btc=candles("KRW-BTC",240,end,180,latest=True)
            regimes=trend26regimes({r[0]:r for r in btc if r[0]%step==anchor and r[0]+step<=cut},cut,anchor)
            if not any(r["btc_regime"]!="unknown" for r in regimes.values()):raise RuntimeError("BTC 시장 판정 이력 부족")
        patch(phase="15/20 하락폭·거래량 4개 비교" if refine else "BTC 상승장 필터 전후 비교" if gate else "상승폭 3/5/10% + 눌림 최종 비교" if pull else "20봉 양봉 10~20개 동시 검증" if bull else "42봉 30~36개 동시 검증",done=0,total=len(universe))
        groups=range(4) if refine else (3,5,10,103,105,110) if gate else (3,5,10) if pull else range(10,21) if bull else range(30,37);out=[];cov=[];success=0
        def groupmeta(g):return trend27group(g) if refine else trend26group(g) if gate else {min_key:g}
        def fetch(m):
            rr4=candles(m,240,end,150 if refine else 180,latest=True)
            direct={r[0]:r for r in rr4 if r[0]%step==anchor and r[0]+step<=cut}
            if not direct:return None
            raw,n,why=r131page(m,cut-23*DAY,cut)
            return raw,direct,n,why
        for m,pack in parallel(universe,fetch):
            base=dict(market=m,preset=preset,strategy="trend",scan_id=scan_id)
            if not pack or not pack[0]:
                for g in groups:cov.append(dict(base,**groupmeta(g),status="fetch_error",valid_hours=0,missing_hours=360,buy_signals=0,overlap_skipped=0,outcome_missing=0,price_skipped=0))
                continue
            raw,direct,n,why=pack;success+=1
            trades,audit=trend23eval(raw,direct,cut,anchor,bull,pull,regimes,refine)
            for g in groups:
                a=audit[g]
                cov.append(dict(base,**groupmeta(g),status="ok" if a["valid_hours"] else "history",pages=n,page_stop=why,**a))
                out.extend(dict(base,**r) for r in trades[g])
        if not success or not any(r["valid_hours"] for r in cov):raise RuntimeError("유효한 시간봉 부족")
        out.sort(key=lambda r:(r[min_key],r["entry_ts"],r["market"]))
        summaries=[]
        for g in groups:
            a=[r for r in out if r[min_key]==g];b=[r for r in cov if r[min_key]==g];n=len(a)
            def avg(k):return mean([r[k] for r in a]) if n else None
            def rate(k,limit,low=False):return 100*sum((r[k]<=limit if low else r[k]>=limit) for r in a)/n if n else None
            valid_hours=sum(r["valid_hours"] for r in b)
            summaries.append(dict(preset=preset,strategy="trend",scan_id=scan_id,**groupmeta(g),total_bars=total_bars,signal_time=iso(cut),finished_at=iso(int(now().timestamp())),entry_days=15,first_entry=iso(cut-18*DAY),last_entry_exclusive=iso(cut-3*DAY),universe=len(universe),market_success=success,valid=sum(r["valid_hours"]>0 for r in b),history_missing=sum(r["status"]=="history" for r in b),matches=n,unique_markets=len({r["market"] for r in a}),buys=n,watches=0,fee_oneway_pct=.04,valid_hours=valid_hours,missing_hours=sum(r["missing_hours"] for r in b),valid_pct=100*valid_hours/(360*len(universe)),buy_signals=sum(r["buy_signals"] for r in b),overlap_skipped=sum(r["overlap_skipped"] for r in b),outcome_missing=sum(r["outcome_missing"] for r in b),day1_mean_pct=avg("day1_close_pct"),day3_mean_pct=avg("day3_close_pct"),day1_peak_mean_pct=avg("day1_peak_pct"),day3_peak_mean_pct=avg("day3_peak_pct"),day3_hit5_pct=rate("day3_peak_pct",5),day3_hit10_pct=rate("day3_peak_pct",10),day3_loss10_pct=rate("day3_low_pct",-10,True),day1_median_pct=statistics.median([r["day1_close_pct"] for r in a]) if n else None,day3_median_pct=statistics.median([r["day3_close_pct"] for r in a]) if n else None,day1_win_pct=100*sum(r["day1_close_pct"]>0 for r in a)/n if n else None,day3_win_pct=100*sum(r["day3_close_pct"]>0 for r in a)/n if n else None))
        if gate:
            for r in summaries:r.update(btc_up_hours=sum(v["btc_regime"]=="up" for v in regimes.values()),btc_not_up_hours=sum(v["btc_regime"]=="not_up" for v in regimes.values()),btc_unknown_hours=sum(v["btc_regime"]=="unknown" for v in regimes.values()))
        patch(status="done",phase="완료",rank=out,features=out,coverage=cov,summary=summaries,elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:record_error(str(e));patch(status="error",phase="실패")
EXIT28_METHODS=("A1","A2","A3","B","C")
def exit28simulate(raw,t,method,stop):
    seq=[raw.get(t+i*HOUR) for i in range(72)]
    if not all(r131valid(r) for r in seq):return None
    p=seq[0][1];fee_ratio=(1+FEE81)/(1-FEE81)
    sl=p*(1-stop/100)*fee_ratio
    tp=p*(1+(int(method[1]) if method.startswith("A") else 1)/100)*fee_ratio
    remaining=1.;net=0.;legs=[];ambiguous=0;pending=False
    def sell(q,price,at,why):
        nonlocal remaining,net
        remaining-=q;net+=q*trend20net(p,price)
        legs.append(dict(fraction=q,price=price,time=iso(at),reason=why))
    for i,r in enumerate(seq):
        at=t+i*HOUR;op,hi,lo,cl=r[1:5]
        if op<=sl:
            sell(remaining,op,at,"stop_gap");break
        if pending:
            sell(remaining,op,at,"sma5_exit");break
        target_active=method.startswith("A") or (method=="B" and remaining>.99)
        if target_active and op>=tp:
            sell(remaining if method.startswith("A") else .7,tp,at,"target")
            if remaining<1e-9:break
            target_active=False
        if lo<=sl:
            if target_active and hi>=tp:ambiguous+=1
            sell(remaining,sl,at+HOUR,"stop");break
        if target_active and hi>=tp:
            sell(remaining if method.startswith("A") else .7,tp,at+HOUR,"target")
            if remaining<1e-9:break
        if i==71:
            sell(remaining,cl,at+HOUR,"72h");break
        if method=="C" or (method=="B" and remaining<.99):
            rr=[raw.get(at-j*HOUR) for j in range(4,-1,-1)]
            if all(r131valid(x) for x in rr):
                pending=cl<mean([x[4] for x in rr])
    if remaining>1e-9:return None
    return dict(exit_method=method,stop_net_pct=-stop,realized_net_pct=net,exit_reason=legs[-1]["reason"],exit_time=legs[-1]["time"],holding_hours=(datetime.strptime(legs[-1]["time"],"%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp()-t)/HOUR,partial_taken=int(len(legs)>1),ambiguous_bars=ambiguous,exit_legs=json.dumps(legs,ensure_ascii=False))
def runexit28(entry,method,stop):
    begin=time.monotonic()
    try:
        end=int(now().timestamp());cut=end//HOUR*HOUR;step=4*HOUR
        universe=sorted({x["market"] for x in api("market/all") if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        if not universe:raise RuntimeError("시장 목록 없음")
        probe=candles("KRW-BTC",240,end,8,latest=True)
        if len(probe)<2 or any(r[0]%step!=probe[-1][0]%step for r in probe) or probe[-1][0]+step<=cut-step:raise RuntimeError("240분봉 경계 확인 실패")
        anchor=probe[-1][0]%step
        with state_lock:scan_id=state["scan_id"]
        methods=EXIT28_METHODS if method=="ALL" else (method,)
        patch(phase="15/20 진입 및 매도 방식 검증",done=0,total=len(universe))
        out=[];cov=[];success=0
        def fetch(m):
            rr=candles(m,240,end,150,latest=True)
            direct={r[0]:r for r in rr if r[0]%step==anchor and r[0]+step<=cut}
            if not direct:return None
            raw,n,why=r131page(m,cut-23*DAY,cut)
            return raw,direct,n,why
        for m,pack in parallel(universe,fetch):
            base=dict(market=m,preset="EXIT28",strategy="exit28",scan_id=scan_id,**trend27group(entry))
            if not pack or not pack[0]:
                cov.append(dict(base,status="fetch_error",valid_hours=0,missing_hours=360,buy_signals=0,overlap_skipped=0,outcome_missing=0));continue
            raw,direct,n,why=pack;success+=1
            trades,audit=trend23eval(raw,direct,cut,anchor,False,False,None,True)
            a=audit[entry]
            cov.append(dict(base,status="ok" if a["valid_hours"] else "history",pages=n,page_stop=why,**a))
            for row in trades[entry]:
                results=[exit28simulate(raw,row["entry_ts"],k,stop) for k in methods]
                if not all(x is not None for x in results):raise RuntimeError("매도 비교 자료 불일치")
                for x in results:out.append(dict(base,**{k:v for k,v in row.items() if k not in base},**x))
        if not success or not any(r["valid_hours"] for r in cov):raise RuntimeError("유효한 시간봉 부족")
        out.sort(key=lambda r:(r["exit_method"],r["entry_ts"],r["market"]))
        summaries=[]
        for k in methods:
            a=[r for r in out if r["exit_method"]==k];n=len(a)
            vals=[r["realized_net_pct"] for r in a]
            summaries.append(dict(preset="EXIT28",strategy="exit28",scan_id=scan_id,**trend27group(entry),exit_method=k,stop_net_pct=-stop,fee_oneway_pct=.04,max_hold_hours=72,entry_days=15,first_entry=iso(cut-18*DAY),last_entry_exclusive=iso(cut-3*DAY),universe=len(universe),market_success=success,matches=n,mean_net_pct=mean(vals) if n else None,median_net_pct=statistics.median(vals) if n else None,win_pct=100*sum(v>0 for v in vals)/n if n else None,mean_hold_hours=mean([r["holding_hours"] for r in a]) if n else None,stop_count=sum(r["exit_reason"].startswith("stop") for r in a),partial_count=sum(r["partial_taken"] for r in a),ambiguous_bars=sum(r["ambiguous_bars"] for r in a),outcome_missing=sum(r.get("outcome_missing",0) for r in cov),valid_hours=sum(r["valid_hours"] for r in cov),missing_hours=sum(r["missing_hours"] for r in cov),buy_signals=sum(r["buy_signals"] for r in cov),overlap_skipped=sum(r["overlap_skipped"] for r in cov)))
        patch(status="done",phase="완료",rank=out,features=out,coverage=cov,summary=summaries,elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:record_error(str(e));patch(status="error",phase="실패")
def startexit28(entry,method,stop):
    if entry not in range(4) or method not in ("ALL",)+EXIT28_METHODS or stop not in (1,2,3):return False
    with state_lock:
        if state["status"]=="running":return False
        state.clear();state.update(mode="exit28",preset="EXIT28",scan_id=str(time.time_ns()),status="running",phase="준비",done=0,total=0,rank=[],features=[],summary=[],errors=[],coverage=[])
    threading.Thread(target=runexit28,args=(entry,method,stop),daemon=True).start()
    return True
def runtrend20(preset,back):
    begin=time.monotonic()
    try:
        end=int(now().timestamp());cut=end//HOUR*HOUR
        universe=sorted({x["market"] for x in api("market/all") if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        if not universe:raise RuntimeError("시장 목록 없음")
        direct_anchor=None
        if preset in TREND_DIRECT:
            probe=candles("KRW-BTC",240,end,8,latest=True)
            if len(probe)<2 or any(r[0]%(4*HOUR)!=probe[-1][0]%(4*HOUR) for r in probe):raise RuntimeError("BTC 240분봉 시간 경계 확인 실패")
            direct_anchor=probe[-1][0]%(4*HOUR)
            if probe[-1][0]+4*HOUR<=cut-4*HOUR:raise RuntimeError("BTC 240분봉 지연")
        with state_lock:scan_id=state["scan_id"]
        patch(phase="15일 과거 진입 검증" if back else "상승 눌림 검색",done=0,total=len(universe))
        out=[];cov=[];valid=0;success=0
        def fetch(m):
            direct=None
            if preset in TREND_DIRECT:
                rr4=candles(m,240,end,150 if back else (35 if preset=="T430" else 25),latest=True)
                direct={r[0]:r for r in rr4 if r[0]%(4*HOUR)==direct_anchor and r[0]+4*HOUR<=cut}
                if not direct:return {},1,"no_240m",direct
            if back:
                raw,n,reason=r131page(m,cut-23*DAY,cut)
                return raw,n,reason,direct
            rr=candles(m,60,end,120 if preset not in TREND_DIRECT else 8,latest=True)
            return {r[0]:r for r in rr},1,"latest",direct
        for m,pack in parallel(universe,fetch):
            base=dict(market=m,preset=preset,strategy="trend",scan_id=scan_id)
            if not pack or not pack[0]:cov.append(dict(base,status="fetch_error"));continue
            raw,pages,reason,direct=pack;success+=1
            if back:
                rows,meta=trend20eval(raw,cut,preset,True,direct,direct_anchor)
                valid+=int(meta["valid_hours"]>0)
                cov.append(dict(base,status="ok" if meta["valid_hours"] else "history",pages=pages,page_stop=reason,**meta))
                out.extend(dict(base,**r) for r in rows)
            else:
                why,s=trend20eval(raw,cut,preset,False,direct,direct_anchor)
                valid+=int(why!="history")
                cov.append(dict(base,status=why,pages=pages))
                if s:out.append(dict(base,**s,signal_time=iso(cut),hour_signal_time=iso(cut)))
        if not success or not valid:raise RuntimeError("유효한 시간봉 부족")
        if not back:
            # Obtain fresh reference prices only for candidates, after scan.
            prices={};patch(phase="후보 현재가 조회",done=0,total=math.ceil(len(out)/40))
            for i in range(0,len(out),40):
                batch=[r["market"] for r in out[i:i+40]]
                try:
                    for q in api("ticker",markets=",".join(batch)):
                        p=float(q.get("trade_price",0))
                        if q.get("market") in batch and math.isfinite(p) and p>0:prices[q["market"]]=p
                except Exception as e:record_error(str(e))
                advance()
            kept=[];audit={r["market"]:r for r in cov}
            for r in out:
                p=prices.get(r["market"])
                if p is None:audit[r["market"]]["status"]="no_quote";continue
                r.update(reference_price=p,observed_at=iso(int(now().timestamp())),breakeven_price=p*(1+FEE81)/(1-FEE81))
                kept.append(r)
            out=kept
            if int(now().timestamp())//HOUR*HOUR!=cut:raise RuntimeError("시간봉 경계 변경: 다시 검색")
            out.sort(key=lambda r:(r["status"]!="BUY",-r["confirm_volume_ratio"],r["market"]))
        else:out.sort(key=lambda r:(r["entry_ts"],r["market"]))
        summary=dict(preset=preset,strategy="trend",scan_id=scan_id,signal_time=iso(cut),finished_at=iso(int(now().timestamp())),universe=len(universe),market_success=success,valid=valid,matches=len(out),buys=sum(r["status"]=="BUY" for r in out),watches=sum(r["status"]=="WATCH" for r in out),history_missing=sum(r["status"]=="history" for r in cov),no_quote=sum(r["status"]=="no_quote" for r in cov),fee_oneway_pct=.04)
        if back:
            summary.update(entry_days=15,first_entry=iso(cut-18*DAY),last_entry_exclusive=iso(cut-3*DAY),valid_hours=sum(r.get("valid_hours",0) for r in cov),missing_hours=sum(r.get("missing_hours",0) for r in cov),buy_signals=sum(r.get("buy_signals",0) for r in cov),outcome_missing=sum(r.get("outcome_missing",0) for r in cov),overlap_skipped=sum(r.get("overlap_skipped",0) for r in cov),price_skipped=sum(r.get("price_skipped",0) for r in cov),day1_mean_pct=mean([r["day1_close_pct"] for r in out]) if out else None,day3_mean_pct=mean([r["day3_close_pct"] for r in out]) if out else None,day3_hit5_pct=100*sum(r["day3_peak_pct"]>=5 for r in out)/len(out) if out else None)
        patch(status="done",phase="완료",rank=out,features=out,coverage=cov,summary=[summary],elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:record_error(str(e));patch(status="error",phase="실패")
def starttrend20(preset,back):
    if preset not in TREND20_NAME or (preset in ("T42ALL","U20ALL","P20ALL","G20ALL","R415ALL") and not back):return False
    with state_lock:
        if state["status"]=="running":return False
        state.clear();state.update(mode="trendback20" if back else "trend20",preset=preset,scan_id=str(time.time_ns()),status="running",phase="준비",done=0,total=0,rank=[],features=[],summary=[],errors=[],coverage=[])
    if preset in ("T42ALL","U20ALL","P20ALL","G20ALL","R415ALL"):threading.Thread(target=runtrend23,args=(preset=="U20ALL",preset in ("P20ALL","G20ALL"),preset=="G20ALL",preset=="R415ALL"),daemon=True).start()
    else:threading.Thread(target=runtrend20,args=(preset,back),daemon=True).start()
    return True
HTML28=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BITHUMB SWING V2.8 TEST</title><style>body{background:#10151d;color:#edf2f7;font:14px system-ui;max-width:800px;margin:auto;padding:14px}section{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}a,button,select{display:inline-block;background:#225b51;color:white;border:1px solid #476073;padding:10px;margin:4px;border-radius:7px;text-decoration:none}button:disabled,select:disabled{opacity:.5}progress{width:100%}small{color:#b4c4d1;line-height:1.7}pre{white-space:pre-wrap}.coin{background:#111923;border:1px solid #3b4d60;border-radius:10px;padding:12px;margin:8px 0}.line{display:flex;justify-content:space-between;gap:10px;margin:6px 0}h1{font-size:23px}h3{margin:0 0 8px}</style><h1>BITHUMB SWING V2.8 TEST</h1><a href="/live143">기존 V1.4.3 실시간</a><a href="/swing27">기존 V2.7 진입 비교</a><section><h2>15/20 단타 + 추세 보유 (백테스트)</h2><p>진입 조건 <select id="entry"><option value="0">A 기본 15/20</option><option value="1">B 하락폭 10~20%</option><option value="2">C 거래량 0.5~1.5배 미만</option><option value="3">D 하락폭 + 거래량</option></select></p><p>매도 방식 <select id="method"><option value="ALL">전체 비교 (A1/A2/A3/B/C)</option><option value="A1">A1 전량 +1%</option><option value="A2">A2 전량 +2%</option><option value="A3">A3 전량 +3%</option><option value="B">B +1% 70% / 추세 30%</option><option value="C">C 전량 추세 추종</option></select></p><p>순손절 <select id="stop"><option value="1">-1%</option><option value="2" selected>-2%</option><option value="3">-3%</option></select></p><small>기존 V2.7 진입 그대로. 15일 진입, 최대 72시간, 편도 0.04%. B/C 추세 청산: 완료 1시간봉 종가가 SMA5 미만이면 다음 시간봉 시가. B 잔량도 최초 손절가 유지. 같은 시간봉에서 손절/익절 순서 불명확하면 손절 우선, 갭 손절은 시가. 시간봉 내부 청산 시각은 봉 종료로 기록. 모든 방식 동일 진입, 72시간 중복 제외 유지. 실제 주문 없음.</small><p><button id="go">15일 백테스트 실행</button></p><p id="phase">대기</p><progress id="progress" value="0" max="1"></progress><pre id="errors"></pre></section><section id="out" hidden><h2>매도 방식 비교 결과</h2><p id="audit"></p><a href="/csv/features?mode=exit28">상세 CSV</a><a href="/csv/summary?mode=exit28">요약 CSV</a><a href="/csv/coverage?mode=exit28">수집 상태 CSV</a><a href="/csv/errors?mode=exit28">오류 CSV</a><div id="results"></div></section><script>
const $=id=>document.getElementById(id);let loaded=null,loading=false;const names={A1:'A1 전량 +1%',A2:'A2 전량 +2%',A3:'A3 전량 +3%',B:'B 70% 익절 + 30% 추세',C:'C 전량 추세'};
function fmt(x,unit='%'){return typeof x==='number'?x.toFixed(2)+unit:'—'}
function node(tag,s){let e=document.createElement(tag);e.textContent=s;return e}
function line(c,k,v){let r=node('div','');r.className='line';r.append(node('span',k),node('strong',v));c.append(r)}
function lock(b){for(let id of ['entry','method','stop','go'])$(id).disabled=b}
$('go').onclick=async()=>{lock(true);$('out').hidden=true;loaded=null;try{let q=new URLSearchParams({entry:$('entry').value,method:$('method').value,stop:$('stop').value});let r=await fetch('/exit28/start?'+q,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('다른 검색 실행 중 또는 조건 오류');await poll()}catch(e){$('phase').textContent=e.message;lock(false)}};
async function poll(){try{let s=await(await fetch('/status',{cache:'no-store'})).json();lock(s.status==='running');if(s.mode!=='exit28'){$('phase').textContent=s.status==='running'?'다른 연구 진행 중':'대기';$('out').hidden=true;return} $('phase').textContent=s.phase+' '+s.done+'/'+s.total+' · '+(s.elapsed_seconds??'—')+'초';$('progress').max=Math.max(1,s.total);$('progress').value=s.done;$('errors').textContent=(s.errors||[]).join('\n');if(s.status!=='done'||loaded===s.scan_id||loading)return;loading=true;try{let d=await(await fetch('/results',{cache:'no-store'})).json();let x=d.summary[0];if(!x||x.scan_id!==s.scan_id||x.preset!=='EXIT28')return;loaded=s.scan_id;$('audit').textContent='진입 '+['A 기본','B 하락폭','C 거래량','D 둘 다'][x.comparison_id]+' · 손절 '+x.stop_net_pct+'% · '+x.first_entry+' ~ '+x.last_entry_exclusive+' (미포함) · 수집 '+x.market_success+'/'+x.universe+' · 평가 누락 '+x.outcome_missing+'건';$('results').replaceChildren(...d.summary.map(r=>{let c=node('article','');c.className='coin';c.append(node('h3',names[r.exit_method]+' · '+r.matches+'건'));line(c,'평균 순수익 / 중앙값',fmt(r.mean_net_pct)+' / '+fmt(r.median_net_pct));line(c,'종료 승률',fmt(r.win_pct));line(c,'평균 보유',fmt(r.mean_hold_hours,'시간'));line(c,'손절 / 분할 실행',r.stop_count+' / '+r.partial_count+'건');line(c,'동일 봉 충돌',r.ambiguous_bars+'건');return c}));$('out').hidden=false}finally{loading=false}}catch(e){$('phase').textContent='연결 확인: '+e.message}}
poll();setInterval(poll,1500);
</script></html>"""


# WHALE RESEARCH V0.1 -- independent state and UI. Live strategy functions remain unchanged.
WBSTEP=4*HOUR
WB_DAYS=(1,3,7)
WB_ENTRY_DAYS=(30,90)
def wbpages(days):return math.ceil((days*6+169+41)/200)
WB_TARGETS=(3,5,10,20)
WB_GROUPS=("ALL","NONE","D1_2","D1_3","D3_2","D3_3","D7_2","D7_3","MULTI2","MULTI3","WATCH","VOL2","VOL3","VOL5")
WB_LABELS={"ALL":"전체 기준군","NONE":"캔들 후보 없음","D1_2":"1일 2점 이상","D1_3":"1일 3점","D3_2":"3일 2점 이상","D3_3":"3일 3점","D7_2":"7일 2점 이상","D7_3":"7일 3점","MULTI2":"2개 이상 기간 공통","MULTI3":"3개 기간 공통","WATCH":"V3.1.1 WATCH 캔들 기준","VOL2":"후보 중 거래량 2배 이상","VOL3":"후보 중 거래량 3배 이상","VOL5":"후보 중 거래량 5배 이상"}
WB_LOCK=threading.Lock()
WB={"status":"idle","phase":"대기","done":0,"total":0,"summary":[],"features":[],"coverage":[],"errors":[],"events":[]}
def wbpatch(**kw):
    with WB_LOCK:WB.update(kw)
def wbnet(p,q):return 100*(q/p*(1-FEE81)/(1+FEE81)-1)
def wboutcome(by,t):
    seq=[by.get(t+i*WBSTEP) for i in range(42)]
    if not all(r131valid(r) for r in seq):return None
    p=seq[0][1]
    out={"entry_price":p}
    for d in WB_DAYS:
        win=seq[:d*6];hi=max(r[2] for r in win);lo=min(r[3] for r in win)
        out.update({"peak_"+str(d)+"d":wbnet(p,hi),"low_"+str(d)+"d":wbnet(p,lo),"close_"+str(d)+"d":wbnet(p,win[-1][4])})
    for k in WB_TARGETS:
        hit=next((i+1 for i,r in enumerate(seq) if wbnet(p,r[2])>=k-1e-9),None)
        out["hit"+str(k)+"_within_hours"]=hit*4 if hit is not None else None
    return out
def wbgroups(periods):
    matched=[d for d in WB_DAYS if periods[d]["volume_flat"] and periods[d]["candle_score"]>=2]
    groups=["ALL"]
    if not matched:groups.append("NONE")
    for d in matched:
        groups.append("D"+str(d)+"_2")
        if periods[d]["candle_score"]==3:groups.append("D"+str(d)+"_3")
    if len(matched)>=2:groups.append("MULTI2")
    if len(matched)==3:groups.append("MULTI3")
    if any(periods[d]["candle_score"]==3 for d in matched) or len(matched)>=2:groups.append("WATCH")
    maxvol=max((periods[d]["volume_ratio"] for d in matched),default=0)
    for k in (2,3,5):
        if maxvol>=k:groups.append("VOL"+str(k))
    return groups,matched,maxvol
def wbnewstats():
    return {"signals":0,"outcome_missing":0,"evaluated":0,**{"raw_hit"+str(k)+"_"+str(d)+"d":0 for k in WB_TARGETS for d in WB_DAYS}}
def wbeval(m,rows,start,stop,scan):
    by={r[0]:r for r in rows}
    cov=dict(market=m,scan_id=scan,received_bars=len(by),expected_windows=(stop-start)//WBSTEP,valid_1d=0,valid_3d=0,valid_7d=0,valid_common=0,common_outcome_missing=0,common_evaluated=0)
    raw={g:wbnewstats() for g in WB_GROUPS};reserved={g:0 for g in WB_GROUPS};overlap={g:0 for g in WB_GROUPS};picked=[];events=[]
    for t in range(start,stop,WBSTEP):
        # Restrict input to the exact 169-bar historical window; same signals, less repeated work.
        historical=[by[k] for k in range(t-169*WBSTEP,t,WBSTEP) if k in by]
        periods={d:whale32candles(historical,t,d)[0] for d in WB_DAYS}
        for d,z in periods.items():cov["valid_"+str(d)+"d"]+=int(z is not None)
        if any(z is None for z in periods.values()):continue
        cov["valid_common"]+=1
        groups,matched,maxvol=wbgroups(periods)
        outcome=wboutcome(by,t)
        cov["common_evaluated" if outcome else "common_outcome_missing"]+=1
        detail=dict(market=m,scan_id=scan,signal_time=iso(t),entry_time=iso(t),entry_ts=t,matched_periods="/".join(map(str,matched)),matched_count=len(matched),max_candidate_volume_ratio=maxvol)
        for d,z in periods.items():
            for key in ("candle_score","volume_flat","higher_low","obv_up","volume_ratio","period_change_pct","period_range_pct","low_change_pct","obv_ratio_pct"):
                detail[key+"_"+str(d)+"d"]=z[key]
        for g in groups:
            s=raw[g];s["signals"]+=1
            if outcome is None:s["outcome_missing"]+=1
            else:
                s["evaluated"]+=1
                for d in WB_DAYS:
                    for k in WB_TARGETS:s["raw_hit"+str(k)+"_"+str(d)+"d"]+=int(outcome["peak_"+str(d)+"d"]>=k-1e-9)
            # Chronological, reserve even when later outcome missing. No overlapping positions per market/group.
            if t<reserved[g]:overlap[g]+=1;continue
            reserved[g]=t+7*DAY
            picked.append(dict(detail,group=g,evaluated=outcome is not None,**(outcome or {})))
        # Reverse view: all eligible 4h windows leading to >=20% within 7d, not distinct pumps.
        # Retain only best 3 examples per market (global UI/CSV capped separately).
        if outcome and outcome["peak_7d"]>=20-1e-9:
            events.append(dict(detail,groups="/".join(groups),**outcome))
            events.sort(key=lambda x:(-x["peak_7d"],x["entry_ts"]))
            del events[3:]
    cov["status"]="ok" if cov["valid_common"] else "common_history_missing"
    return cov,raw,overlap,picked,events
def wbsummary(raw,overlap,features):
    out=[]
    for g in WB_GROUPS:
        s=raw[g];subset=[r for r in features if r["group"]==g];valid=[r for r in subset if r["evaluated"]];n=len(valid)
        row=dict(group=g,label=WB_LABELS[g],raw_signals=s["signals"],raw_evaluated=s["evaluated"],raw_outcome_missing=s["outcome_missing"],overlap_skipped=overlap[g],dedup_signals=len(subset),evaluated=n,outcome_missing=len(subset)-n,distinct_markets=len({r["market"] for r in valid}))
        for d in WB_DAYS:
            for key in ("peak","close","low"):
                vals=[r[key+"_"+str(d)+"d"] for r in valid]
                row[key+"_mean_"+str(d)+"d"]=mean(vals) if vals else None
                row[key+"_median_"+str(d)+"d"]=statistics.median(vals) if vals else None
            for k in WB_TARGETS:
                key="hit"+str(k)+"_"+str(d)+"d";hits=sum(r["peak_"+str(d)+"d"]>=k-1e-9 for r in valid)
                rawhits=s["raw_"+key];den=raw["ALL"]["raw_"+key]
                row[key+"_count"]=hits;row[key+"_pct"]=100*hits/n if n else None
                row["raw_"+key+"_count"]=rawhits
                row["raw_"+key+"_pct"]=100*rawhits/s["evaluated"] if s["evaluated"] else None
                row["capture_"+key+"_pct"]=100*rawhits/den if den else None
        for k in WB_TARGETS:
            hours=[r["hit"+str(k)+"_within_hours"] for r in valid if r["hit"+str(k)+"_within_hours"] is not None]
            row["median_hit"+str(k)+"_hours_upper"]=statistics.median(hours) if hours else None
        out.append(row)
    return out
def wbrun(days):
    begin=time.monotonic()
    try:
        end=int(now().timestamp())
        probe=candles("KRW-BTC",240,end,8,latest=True)
        if len(probe)<2:raise RuntimeError("BTC 4시간봉 경계 확인 실패")
        anchor=probe[-1][0]%WBSTEP
        if any(r[0]%WBSTEP!=anchor for r in probe):raise RuntimeError("4시간봉 경계 불일치")
        cut=((end-anchor)//WBSTEP)*WBSTEP+anchor
        if not any(r[0]==cut-WBSTEP and r131valid(r) for r in probe):raise RuntimeError("완료 BTC 4시간봉 지연")
        stop=cut-7*DAY+WBSTEP;start=stop-days*DAY
        pages=wbpages(days)
        universe=sorted({r["market"] for r in api("market/all") if r["market"].startswith("KRW-") and r["market"][4:] not in STABLE})
        if not universe:raise RuntimeError("시장 목록 없음")
        with WB_LOCK:scan=WB["scan_id"]
        wbpatch(phase="4시간봉 수집 및 과거 시점 검증",total=len(universe),entry_days=days,expected_windows_per_market=days*6,requested_pages_per_market=pages,entry_start=iso(start),entry_end_exclusive=iso(stop),data_end=iso(cut),fee_roundtrip_pct=.08)
        raw={g:wbnewstats() for g in WB_GROUPS};overlap={g:0 for g in WB_GROUPS};features=[];coverage=[];events=[];errors=[]
        def fetch(m):
            rows=candles(m,240,cut,200,pages=pages,latest=False)
            return wbeval(m,rows,start,stop,scan)
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs={pool.submit(fetch,m):m for m in universe}
            for fut in as_completed(jobs):
                m=jobs[fut]
                try:
                    cov,stats,skips,rows,ev=fut.result()
                    coverage.append(cov);features.extend(rows);events.extend(ev)
                    for g in WB_GROUPS:
                        overlap[g]+=skips[g]
                        for k,v in stats[g].items():raw[g][k]+=v
                except Exception as e:
                    errors.append(dict(market=m,error=str(e)))
                    coverage.append(dict(market=m,scan_id=scan,status="fetch_or_eval_error",expected_windows=days*6))
                with WB_LOCK:WB["done"]+=1;WB["error_count"]=len(errors)
        wbpatch(phase="비교군 집계")
        summary=wbsummary(raw,overlap,features)
        for r in summary:r.update(scan_id=scan,entry_days=days,requested_pages_per_market=pages,entry_start=iso(start),entry_end_exclusive=iso(stop),data_end=iso(cut),fee_oneway_pct=.04,universe=len(universe),common_valid_windows=sum(x.get("valid_common",0) for x in coverage))
        for r in features:r["entry_days"]=days
        for r in coverage:r["entry_days"]=days
        for r in events:r["entry_days"]=days
        features.sort(key=lambda x:(x["group"],x["entry_ts"],x["market"]))
        coverage.sort(key=lambda x:x["market"])
        events.sort(key=lambda x:(-x["peak_7d"],x["market"],x["entry_ts"]))
        wbpatch(status="done",phase="완료",summary=summary,features=features,coverage=coverage,errors=errors,events=events[:100],elapsed_seconds=round(time.monotonic()-begin,1),valid_markets=sum(x.get("valid_common",0)>0 for x in coverage),common_valid_windows=sum(x.get("valid_common",0) for x in coverage),common_evaluated_windows=raw["ALL"]["evaluated"],error_count=len(errors))
    except Exception as e:wbpatch(status="error",phase="실패",errors=[dict(error=str(e))],error_count=1)
def wbstart(days=90):
    if days not in WB_ENTRY_DAYS:return False
    with WB_LOCK:
        if WB["status"]=="running":return False
        WB.clear();WB.update(status="running",phase="준비",entry_days=days,scan_id=str(time.time_ns()),done=0,total=0,summary=[],features=[],coverage=[],events=[],errors=[],error_count=0)
    threading.Thread(target=wbrun,args=(days,),daemon=True).start()
    return True
HTMLWB=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BITHUMB WHALE RESEARCH V0.2</title><style>
*{box-sizing:border-box}[hidden]{display:none!important}body{max-width:980px;margin:auto;padding:16px;background:#f6f8f7;color:#192421;font:14px/1.5 system-ui,sans-serif}section{padding:15px;background:#fff;border:1px solid #dce5df;border-radius:16px;margin:12px 0}h1{font-size:22px;margin:4px 0}h2{font-size:17px}p{margin:8px 0}small{color:#63716a;display:block;line-height:1.65}button,a,select{font:inherit;border:1px solid #dce5df;border-radius:9px;padding:10px;color:#16734e;background:white;text-decoration:none;display:inline-block;margin:3px}button{background:#16734e;color:white;width:100%;font-weight:bold;padding:14px}button:disabled{opacity:.5}progress{width:100%;accent-color:#16734e}table{border-collapse:collapse;white-space:nowrap;width:100%;font-size:12px}td,th{border-bottom:1px solid #dce5df;padding:8px;text-align:right}td:first-child,th:first-child{text-align:left}.scroll{overflow:auto}.muted{color:#63716a}.good{color:#16734e}.bad{color:#b33b39}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere}
</style><h1>BITHUMB WHALE RESEARCH V0.2</h1><small>매집형 과거 검증 · 실시간 스캐너와 별도</small><a href="/live311">기존 LIVE 스캐너</a><section><h2>매집 신호 장기 비교</h2><p><label for="entryDays">검증 기간</label> <select id="entryDays"><option value="30">30일</option><option value="90" selected>90일</option></select></p><small>90일은 30일 결과보다 표본 확대 목적이며 3일·7일 3점군을 중점 비교합니다. 전체 14개 비교군 유지.</small><small>전체 KRW(스테이블 제외) · 완료 4시간봉 · 1/3/7일 동일 구간 · 왕복 수수료 0.08% · 신호 확정 후 다음 4시간봉 시가 기준</small><details><summary>검증 방법 및 한계</summary><small>7일 결과가 성숙한 과거 선택 기간 동안 4시간마다 평가. 1/3/7일 캔들 지표가 모두 계산 가능한 시점만 공통 비교. 42개 후속 봉 누락 시 결과 제외. 각 비교군별 동일 종목 7일 중복 제거(결과 누락 신호도 예약). 거래량 필수인 2점 이상 후보와 3점, 복수 기간, 거래량 2/3/5배 비교. 모든 비교군은 중첩 가능. 전체 기준군 및 후보 없음 비교. 실제 체결과 호가는 미검증이며 STRONG 검증 아님. 최고·최저는 해당 기간 극값, 매도 성과나 실제 승률 아님. 고점 도달 시간은 4시간봉 종료 기준 상한. 현재 상장 종목 기준 생존편향, 누락 편향, 거래비용 외 슬리피지 미반영.</small></details><button id="go">90일 백테스트 시작</button><p id="phase">대기</p><progress id="bar" max="1" value="0"></progress><p id="audit" class="muted"></p><pre id="error"></pre></section><section id="out" hidden><h2>비교 결과</h2><p><label>신호 후 기간 <select id="horizon"><option value="1">1일</option><option value="3">3일</option><option value="7" selected>7일</option></select></label></p><small>7일 중복 제외 표본 · 수익률은 수수료 반영 · 도달률은 기간 중 최고가 기준</small><div class="scroll"><table id="tbl"></table></div><details><summary>급등 역추적 지표 읽는 법</summary><small>CSV capture_hit 열은 중복 제거 전 전체 평가 가능 4시간 시점 중 목표에 도달한 시점에서 각 신호가 포착한 비율. 실제 고유 급등 건수나 거래 승률이 아닙니다. 급등 사례 CSV는 7일 최고 +20% 이상 중 종목별 상위 3개, 전체 상위 100개 예시입니다. 중복 시점 포함.</small></details><div id="links"></div><p class="muted">CSV를 저장하면 이후 실시간 검색과 무관하게 보관할 수 있습니다.</p></section><script>
const $=x=>document.getElementById(x);let data=[],loaded=null,busy=false,starting=false;
function fmt(x){return typeof x==='number'?x.toFixed(2)+'%':'—'}
function render(){let d=$('horizon').value,t=$('tbl');t.replaceChildren();let head=t.createTHead().insertRow();for(let k of ['비교군','평가 건수','+3%','+5%','+10%','+20%','평균 최고','평균 종료','평균 최저']){let th=document.createElement('th');th.textContent=k;head.append(th)}let body=t.createTBody();for(let r of data){let tr=body.insertRow();if(['D3_3','D7_3'].includes(r.group)){tr.style.background='#eef8f2';tr.style.fontWeight='600'}for(let v of [r.label,r.evaluated,...[3,5,10,20].map(k=>fmt(r['hit'+k+'_'+d+'d_pct'])),fmt(r['peak_mean_'+d+'d']),fmt(r['close_mean_'+d+'d']),fmt(r['low_mean_'+d+'d'])])tr.insertCell().textContent=v}}
$('horizon').onchange=render;$('entryDays').onchange=()=>{$('go').textContent=$('entryDays').value+'일 백테스트 시작';};
async function poll(){if(busy||starting)return;busy=true;try{let s=await(await fetch('/whaleback/status',{cache:'no-store'})).json();$('go').disabled=s.status==='running';$('entryDays').disabled=s.status==='running';$('phase').textContent=(s.entry_days?s.entry_days+'일 · ':'')+s.phase+' · '+s.done+'/'+s.total;$('bar').max=Math.max(1,s.total);$('bar').value=s.done;$('audit').textContent=(s.entry_start?'신호 '+s.entry_start+' ~ '+s.entry_end_exclusive+' (미포함) · ':'')+'오류 '+(s.error_count||0)+(s.status==='done'?' · 공통 유효 종목 '+s.valid_markets+' · 공통 평가 시점 '+s.common_evaluated_windows+' · '+s.elapsed_seconds+'초':'');$('error').textContent=(s.error_preview||[]).map(x=>(x.market||'')+' '+x.error).join('\n');if(s.status==='running'){$('out').hidden=true;loaded=null}if(s.status==='done'&&loaded!==s.scan_id){let d=await(await fetch('/whaleback/results',{cache:'no-store'})).json();if(d.scan_id!==s.scan_id)return;data=d.summary;loaded=s.scan_id;render();$('links').replaceChildren(...['summary','features','coverage','errors','events'].map((k,i)=>{let a=document.createElement('a');a.href='/whaleback/csv?kind='+k+'&scan_id='+encodeURIComponent(loaded);a.textContent=['요약 CSV','상세 CSV','수집 상태 CSV','오류 CSV','급등 사례 CSV'][i];return a}));$('out').hidden=false}}catch(e){$('phase').textContent='연결 오류: '+e.message;$('go').disabled=false}finally{busy=false}}
$('go').onclick=async()=>{if(starting||busy)return;starting=true;try{$('go').disabled=true;$('entryDays').disabled=true;let r=await fetch('/whaleback/start?days='+encodeURIComponent($('entryDays').value),{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('이미 실행 중 또는 설정 오류');$('out').hidden=true;loaded=null;}catch(e){$('phase').textContent=e.message;$('go').disabled=false;$('entryDays').disabled=false}finally{starting=false;poll()}};poll();setInterval(poll,2000);
</script></html>"""

import whale_back03
import whale_detector
import whale_bt11
import whale_reverse12

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
        if whale_reverse12.handle(self,u,globals(),post=True):return
        if whale_bt11.handle(self,u,globals(),post=True):return
        if whale_detector.handle(self,u,globals(),post=True):return
        if whale_back03.handle(self,u,globals(),post=True):return
        if u.path=="/whaleback/start":
            try:
                days=int(parse_qs(u.query).get("days",["90"])[0])
                if days not in WB_ENTRY_DAYS:raise ValueError()
            except ValueError:return self.js({"error":"invalid days"},400)
            return self.js({"started":wbstart(days)})
        if u.path=="/whale30/start":
            target=parse_qs(u.query).get("market",[""])[0].strip().upper()
            if target and not target.startswith("KRW-"):target="KRW-"+target
            if target and (len(target)>40 or not target[4:].isascii() or not target[4:].isalnum()):return self.js({"error":"invalid market"},400)
            try:
                days=int(parse_qs(u.query).get("days",["1"])[0])
                if days not in WHALE32_RULES:raise ValueError()
            except ValueError:return self.js({"error":"invalid days"},400)
            return self.js({"started":startwhale30(target,days)})
        if u.path=="/exit28/start":
            try:
                q=parse_qs(u.query);entry=int(q.get("entry",["0"])[0]);method=q.get("method",["ALL"])[0];stop=int(q.get("stop",["2"])[0])
                if entry not in range(4) or method not in ("ALL",)+EXIT28_METHODS or stop not in (1,2,3):raise ValueError()
            except ValueError:return self.js({"error":"invalid settings"},400)
            return self.js({"started":startexit28(entry,method,stop)})
        if u.path=="/trend20/start":
            q=parse_qs(u.query);preset=q.get("preset",["T4"])[0];back=q.get("back",["0"])[0]
            if preset not in TREND20_NAME or back not in ("0","1"):return self.js({"error":"invalid mode"},400)
            return self.js({"started":starttrend20(preset,back=="1")})
        if u.path=="/track15/start":return self.js({"started":d15start()})
        if u.path in ("/research13/start","/research131/start"):
            try:
                days=int(parse_qs(u.query).get("days",["60"])[0])
                if days not in (30,60,90):raise ValueError()
            except ValueError:return self.js({"error":"invalid days"},400)
            return self.js({"started":start13(days)})
        if u.path=="/live14/start":
            preset=parse_qs(u.query).get("preset",["B"])[0]
            if preset not in LIVE14_PRESETS:return self.js({"error":"invalid preset"},400)
            return self.js({"started":start14(preset)})
        if u.path=="/live12/start":return self.js({"started":start12("live12")})
        if u.path=="/back12/start":return self.js({"started":start12("back12")})
        if u.path=="/live10/start":return self.js({"started":start10()})
        if u.path=="/reverse81/start":return self.js({"started":start81()})
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
        if whale_reverse12.handle(self,u,globals()):return
        if whale_bt11.handle(self,u,globals()):return
        if whale_detector.handle(self,u,globals()):return
        if whale_back03.handle(self,u,globals()):return
        if p in ("/whaleback","/whaleback02"):return self.send(HTMLWB,"text/html; charset=utf-8")
        if p=="/whaleback/status":
            with WB_LOCK:
                data={k:v for k,v in WB.items() if k not in ("summary","features","coverage","errors","events")}
                data["error_preview"]=WB["errors"][:10]
            return self.js(data)
        if p=="/whaleback/results":
            with WB_LOCK:data=dict(scan_id=WB.get("scan_id"),summary=WB["summary"])
            return self.js(data)
        if p=="/whaleback/csv":
            q=parse_qs(u.query);kind=q.get("kind",["summary"])[0];scan=q.get("scan_id",[""])[0]
            if kind not in ("summary","features","coverage","errors","events"):return self.js({"error":"invalid kind"},400)
            with WB_LOCK:
                if WB["status"]!="done" or scan!=WB.get("scan_id"):return self.js({"error":"scan again"},409)
                data=list(WB[kind]);entry_days=WB["entry_days"]
            fields=list(dict.fromkeys(k for r in data for k in r))
            buf=io.StringIO();writer=csv.DictWriter(buf,fieldnames=fields)
            if fields:writer.writeheader();writer.writerows(data)
            return self.send(("\ufeff"+buf.getvalue()).encode("utf-8"),"text/csv; charset=utf-8",filename="BITHUMB_WHALE_RESEARCH_V0_2_"+str(entry_days)+"D_"+kind.upper()+"_"+now().astimezone(KST).strftime("%Y%m%d%H%M")+".csv")
        if p in ("/","/live29","/live30","/live301","/live31","/live311","/swing","/swing20","/swing21","/swing22","/swing23","/swing24","/swing25","/swing26","/swing27","/swing28"):return self.send(HTML29,"text/html; charset=utf-8")
        if p=="/live29/trend":
            markets=list(dict.fromkeys(parse_qs(u.query).get("markets",[""])[0].split(",")))
            if not 1<=len(markets)<=20 or any(not m.startswith("KRW-") or not m[4:].isalnum() or len(m)>40 for m in markets):return self.js({"error":"invalid markets"},400)
            end=int(now().timestamp());cut=end//HOUR*HOUR;out={}
            for m in markets:
                try:
                    rr=candles(m,60,end,8,latest=True);by={r[0]:r for r in rr}
                    r=[by.get(cut-i*HOUR) for i in range(5,0,-1)]
                    if all(r131valid(x) for x in r):
                        ma=mean([x[4] for x in r]);out[m]=dict(status="below" if r[-1][4]<ma else "above",end=iso(cut),close=r[-1][4],sma5=ma)
                    else:out[m]=dict(status="unknown")
                except Exception:out[m]=dict(status="unknown")
            return self.js({"trends":out})
        if p=="/swing28":return self.send(HTML28,"text/html; charset=utf-8")
        if p=="/swing27":return self.send(HTML27,"text/html; charset=utf-8")
        if p=="/swing26":return self.send(HTML26,"text/html; charset=utf-8")
        if p=="/swing25":return self.send(HTML25,"text/html; charset=utf-8")
        if p=="/swing24":return self.send(HTML24,"text/html; charset=utf-8")
        if p=="/swing23":return self.send(HTML23,"text/html; charset=utf-8")
        if p=="/swing22":return self.send(HTML22,"text/html; charset=utf-8")
        if p=="/swing21":return self.send(HTML21,"text/html; charset=utf-8")
        if p=="/swing20":return self.send(HTML20,"text/html; charset=utf-8")
        if p=="/trend20/entry":
            q=parse_qs(u.query);market=q.get("market",[""])[0];scan=q.get("scan_id",[""])[0]
            with state_lock:
                match=next((dict(r) for r in state["rank"] if r["market"]==market and r["scan_id"]==scan),None) if state.get("mode")=="trend20" and state.get("status")=="done" else None
            if not match or match["preset"]!=q.get("preset",[""])[0] or match["status"]!=q.get("entry_status",[""])[0] or match["signal_time"]!=q.get("signal",[""])[0]:return self.js({"error":"scan changed"},409)
            try:
                cut=int(datetime.strptime(match["signal_time"],"%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp())
                if not cut<=int(now().timestamp())<cut+HOUR:return self.js({"error":"expired"},409)
                quotes=api("ticker",markets=market)
                price=next((float(r["trade_price"]) for r in quotes if r.get("market")==market),0)
                at=int(now().timestamp()*1000)
                if not math.isfinite(price) or price<=0 or at>=(cut+HOUR)*1000:return self.js({"error":"expired or missing price"},409)
                # Reject entries which have broken the recorded pullback low or already surpassed the swing high.
                if match["preset"] not in TREND_DIRECT and not match["pullback_low"]<=price<=match["prior_peak"]:return self.js({"error":"price outside setup"},409)
                with state_lock:
                    if state.get("mode")!="trend20" or state.get("status")!="done" or state.get("scan_id")!=scan:return self.js({"error":"scan changed"},409)
                return self.js(dict(price=price,observed_ms=at,signal_deadline_ms=(cut+7*DAY)*1000,preset=match["preset"],entry_status=match["status"]))
            except Exception:return self.js({"error":"quote unavailable"},502)
        if p=="/track15":return self.send(HTML_D15,"text/html; charset=utf-8")
        if p=="/track15/status":
            with D15_LOCK:data=dict(D15)
            return self.js(data)
        if p=="/track15/csv":
            with D15_LOCK:data=dict(D15)
            if data["status"]!="done":return self.js({"error":"not complete"},409)
            cols=["market","entry_time","buy_price"]+["day"+str(d) for d in range(1,16)]+["day7_check","mature_days","missing_days"]
            stream=io.StringIO();w=csv.DictWriter(stream,fieldnames=cols);w.writeheader();w.writerows(data["rows"])
            return self.send(stream.getvalue().encode("utf-8-sig"),"text/csv; charset=utf-8",filename="REVERSE_SWING_TRACK15_T1_"+now().astimezone(KST).strftime("%Y%m%d%H%M")+".csv")
        if p=="/app-test":return self.send(APP_LINK_TEST,"text/html; charset=utf-8")
        if p=="/":return self.send(HTML,"text/html; charset=utf-8")
        if p=="/stage2":return self.send(HTML2,"text/html; charset=utf-8")
        if p=="/stage3":return self.send(HTML3,"text/html; charset=utf-8")
        if p in ("/research13","/research131"):return self.send(HTML13,"text/html; charset=utf-8")
        if p in ("/research13/status","/research131/status"):
            with R13_LOCK:
                data={k:v for k,v in R13.items() if k not in ("features","coverage","errors","diagnostics")}
                data.update(errors=R13["errors"][-20:],error_count=len(R13["errors"]))
            return self.js(data)
        if p in ("/research13/csv","/research131/csv"):
            with R13_LOCK:
                snap=dict(R13)
            if snap["status"]!="done":return self.js({"error":"not complete"},409)
            return self.send(csv13(snap),"text/csv; charset=utf-8",filename="REVERSE_SWING_V1_3_1_RESEARCH_"+now().astimezone(KST).strftime("%Y%m%d%H%M")+".csv")
        if p in ("/live14","/live141","/live142","/live143"):return self.send(HTML14,"text/html; charset=utf-8")
        if p in ("/live","/live12"):return self.send(HTML12,"text/html; charset=utf-8")
        if p=="/back12":return self.send(HTMLB12,"text/html; charset=utf-8")
        if p in ("/live10","/live11"):return self.send(HTML10,"text/html; charset=utf-8")
        if p in ("/paper/quotes","/paper/entry"):
            q=parse_qs(u.query)
            markets=q.get("markets",[""])[0].split(",") if p=="/paper/quotes" else [q.get("market",[""])[0]]
            if not 1<=len(markets)<=40 or any(not m.startswith("KRW-") or not m[4:].isalnum() or len(m)>40 for m in markets):return self.js({"error":"invalid markets"},400)
            signal_deadline=None
            if p=="/paper/entry":
                sig=q.get("signal",[""])[0]
                with state_lock:
                    match=next((x for x in state["rank"] if x["market"]==markets[0] and x.get("signal_time")==sig),None) if state.get("mode") in ("live10","live12","live14") and state["status"]=="done" else None
                if not match:return self.js({"error":"scan again"},409)
                if match.get("preset") and (q.get("preset",[""])[0]!=match["preset"] or q.get("scan_id",[""])[0]!=match.get("scan_id")):return self.js({"error":"preset changed"},409)
                if not paper142_allowed(match,q.get("entry_status",[""])[0]):return self.js({"error":"entry status changed or not allowed"},409)
                if match.get("status")!="MATCH":
                    hour=q.get("hour",[""])[0]
                    if hour!=match.get("hour_signal_time"):return self.js({"error":"hour changed"},409)
                    hstart=int(datetime.strptime(hour,"%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp())
                    if not hstart<=int(now().timestamp())<hstart+HOUR:return self.js({"error":"hour expired"},409)
                cut=int(datetime.strptime(sig,"%Y-%m-%d %H:%M").replace(tzinfo=KST).timestamp())
                if not cut<=int(now().timestamp())<cut+DAY:return self.js({"error":"expired"},409)
                signal_deadline=(cut+7*DAY)*1000
            try:
                rows=api("ticker",markets=",".join(markets))
                at=int(now().timestamp()*1000);quotes={}
                for r in rows:
                    m=r.get("market");price=float(r.get("trade_price",0))
                    if m in markets and math.isfinite(price) and price>0:
                        quotes[m]=dict(price=price,observed_ms=at,trade_timestamp=r.get("trade_timestamp"))
                if p=="/paper/entry":
                    if markets[0] not in quotes or at>=(cut+DAY)*1000:return self.js({"error":"expired or quote missing"},409)
                    if match.get("status") in ("BUY","WATCH"):
                        dr=candles(markets[0],1440,int(now().timestamp()),22 if match.get("preset") else 2,latest=True)
                        ds=daily_spike121(dr,cut)
                        if not ds or spike121(ds["day_open"],max(ds["day_high"],quotes[markets[0]]["price"])):return self.js({"error":"30% spike or missing data"},409)
                        if match.get("preset"):
                            audit=spike141(dr,cut,quotes[markets[0]]["price"])
                            if audit is None or audit["lookback_spike_excluded"]:return self.js({"error":"historical 30% spike or missing data"},409)
                        if int(now().timestamp())>=min(cut+DAY,hstart+HOUR):return self.js({"error":"expired"},409)
                    if match.get("preset"):
                        with state_lock:
                            if state.get("mode")!="live14" or state.get("status")!="done" or state.get("scan_id")!=match.get("scan_id"):return self.js({"error":"scan changed"},409)
                    return self.js(dict(quotes[markets[0]],signal_deadline_ms=signal_deadline,preset=match.get("preset"),min_drawdown_pct=match.get("min_drawdown_pct"),min_down_days=match.get("min_down_days"),entry_status=match.get("status")))
                return self.js({"quotes":quotes})
            except Exception:return self.js({"error":"quote unavailable"},502)
        if p=="/reverse81":return self.send(HTML81,"text/html; charset=utf-8")
        if p=="/reverse81/details":
            q=parse_qs(u.query)
            try:
                page=int(q.get("page",["1"])[0]);kind=q.get("kind",["all"])[0]
                day=q.get("day",[""])[0];query=q.get("query",[""])[0].strip().upper()[:40]
                if kind not in ("all","win","loss"):raise ValueError()
            except ValueError:return self.js({"error":"invalid filters"},400)
            with state_lock:
                if state.get("mode")!="reverse81" or state["status"]!="done":return self.js({"error":"not complete"},409)
                rows=state["features"]
            a=[x for x in rows if (not day or x["day"]==day) and query in x["market"] and (kind=="all" or (kind=="win" and x["profit_opportunity"]) or (kind=="loss" and not x["profit_opportunity"]))]
            a.sort(key=lambda x:(x["day"],x["market"]))
            pages=max(1,(len(a)+49)//50);page=min(max(1,page),pages)
            return self.js(dict(total=len(a),page=page,pages=pages,rows=a[(page-1)*50:page*50]))
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
                x=list(state[key]); mode=state.get("mode");csv_preset=state.get("preset");csv_whale_days=state.get("period_days",1)
                expected_mode=parse_qs(u.query).get("mode",[""])[0]
                if expected_mode and expected_mode!=mode:return self.js({"error":"scan again"},409)
                csv_live29=parse_qs(u.query).get("live29",["0"])[0]=="1"
                expected_scan=parse_qs(u.query).get("scan_id",[""])[0]
                if expected_scan and expected_scan!=state.get("scan_id"):return self.js({"error":"scan again"},409)
                if csv_live29 and not (mode=="live14" or (mode=="trend20" and csv_preset=="T415")):return self.js({"error":"scan again"},409)
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
        return self.send(("\ufeff"+stream.getvalue()).encode("utf-8"),"text/csv; charset=utf-8",filename=("BITHUMB_V3_1_1_LIVE_"+("BREAKOUT_" if mode=="trend20" else "BOTTOM_")+str(csv_preset)+"_" if csv_live29 else ("BITHUMB_SWING_V2_2_TEST_" if csv_preset=="T430" else "BITHUMB_SWING_V2_7_TEST_" if csv_preset=="R415ALL" else "BITHUMB_SWING_V2_6_TEST_" if csv_preset=="G20ALL" else "BITHUMB_SWING_V2_5_TEST_" if csv_preset=="P20ALL" else "BITHUMB_SWING_V2_4_TEST_" if csv_preset=="U20ALL" else "BITHUMB_SWING_V2_3_TEST_" if csv_preset=="T42ALL" else "BITHUMB_SWING_V2_1_TEST_" if csv_preset=="T415" else "BITHUMB_SWING_V2_0_TEST_")+("BACK_" if mode=="trendback20" else "LIVE_")+str(csv_preset)+"_" if mode in ("trend20","trendback20") else "BITHUMB_V3_1_1_LIVE_WHALE_"+str(csv_whale_days)+"D_" if mode=="whale30" else "BITHUMB_SWING_V2_8_TEST_EXIT_" if mode=="exit28" else "REVERSE_SWING_V1_4_3_LIVE_"+str(csv_preset)+"_" if mode=="live14" else "REVERSE_SWING_V1_2_1_LIVE_" if mode=="live12" else "REVERSE_SWING_V1_2_1_BACKTEST_" if mode=="back12" else "REVERSE_SWING_V1_1_LIVE_" if mode=="live10" else "REVERSE_SWING_V0_8_1_" if mode=="reverse81" else "REVERSE_SWING_V0_8_" if mode=="reverse8" else "REVERSE_SWING_V0_7_" if mode=="reverse7" else "REVERSE_SWING_V0_6_1_" if mode=="reverse6" else "REVERSE_SWING_V0_5_" if mode=="reverse5" else "REVERSE_SWING_V0_4_" if mode=="reverse4" else "REVERSE_SWING_V0_3_3_" if mode=="stage3" else "REVERSE_MINI_V0_2_" if mode=="stage2" else "REVERSE_MINI_V0_1_3_")+key.upper()+("_"+n+"D" if mode=="reverse7" and key=="features" and n else "")+("_"+group+"_"+horizon+"D" if mode=="stage3" and key=="features" and horizon and group else "")+"_"+now().astimezone(KST).strftime("%Y%m%d%H%M")+".csv")
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
    a81=outcome81(synthetic8,start)
    assert a81["profit_opportunity"] and a81["peak_day_number"]==3
    assert abs(a81["peak_net_pct"]-100*(1.02*(1-.0004)/(1+.0004)-1))<1e-9
    assert a81["day7_net_pct"]<0 and a81["net_pct"]>0
    no81={start+i*DAY:(start+i*DAY,100,100,98,99,10,1000) for i in range(7)}
    b81=outcome81(no81,start)
    assert not b81["profit_opportunity"] and b81["net_pct"]==b81["day7_net_pct"]
    assert outcome81({k:v for k,v in synthetic8.items() if k!=start+6*DAY},start) is None
    r81,s81,d81=eval81({"KRW-A":vol8},[start+25*DAY,start+30*DAY],start+32*DAY)
    assert len(r81)==1 and s81[0]["mature_days"]==1 and s81[0]["pending_days"]==1
    c10=start+25*DAY
    a10,why10=candidate10(vol6,c10,c10+100)
    assert why10=="match" and a10["status"]=="MATCH" and a10["down_days"]==20
    assert abs(a10["breakeven_price"]/a10["reference_price"]-(1+.0004)/(1-.0004))<1e-10
    altered10=[tuple(list(r[:4])+[r[4]*2,10000,r[6]]) if r[0]==c10 else r for r in vol6]
    b10,why10=candidate10(altered10,c10,c10+100)
    assert why10=="match" and b10["vol_ratio_d1"]==a10["vol_ratio_d1"] and b10["trend_net_pct"]==a10["trend_net_pct"] and b10["reference_price"]==2*a10["reference_price"]
    assert candidate10(down6,c10,c10+100)[1]=="no_signal"
    assert candidate10(vol6[:25],c10,c10+100)[1]=="no_quote"
    hh={start+i*HOUR:(start+i*HOUR,p,p,p,p,10,100) for i,p in enumerate([10,9,8,7,6,10])}
    assert cross12(hh,start+6*HOUR)["cross"]
    assert cross12(hh,start+5*HOUR) is None
    assert setup12(vol6,c10) is None
    assert spike121(100,130) and not spike121(100,129.99)
    assert daily_spike121([(c10,100,150,90,99,10,1000)],c10)["spike"]
    assert daily_spike121([],c10) is None
    seq13=[(start+i*HOUR,100+i*.01,102+i*.01,98+i*.01,100+i*.01,10,1000) for i in range(80)]
    flags13,vals13=r13flags(dict(peak_to_last_pct=-42,down_days=15,vol_ratio_d1=3.5),seq13,(start+80*HOUR,102,103,101,102,10,1000),1)
    assert flags13["A"] and flags13["B"] and flags13["C"] and flags13["D"] and not flags13["E"] and flags13["F"] and flags13["M"]
    assert r13summary([],30,"ALL","A")["close_win_pct"] is None
    assert b"section" in csv13(dict(status="done",summary=[],features=[],coverage=[],errors=[]))
    assert flags13["N"] and flags13["R"]
    real131=[(start,100,105,99,104,5,510),(start+2*HOUR,104,106,103,105,3,315)]
    assert r131match(real131,(start,100,106,99,105,8,825))
    assert not r131match(real131,(start,100,106,99,105,9,825))
    def mock131(m,cur):
        return [dict(candle_date_time_utc=datetime.fromtimestamp(t,UTC).strftime("%Y-%m-%dT%H:%M:%S"),opening_price=100,high_price=101,low_price=99,trade_price=100,candle_acc_trade_volume=1,candle_acc_trade_price=100) for t in range(cur-HOUR,max(start-HOUR,cur-3*HOUR),-HOUR)]
    pp131,n131,w131=r131page("KRW-A",start,start+5*HOUR,mock131)
    assert len(pp131)==5 and n131==3 and w131=="reached"
    assert preset14_ok(dict(peak_to_last_pct=-30,down_days=15),"A")
    assert not preset14_ok(dict(peak_to_last_pct=-30,down_days=14),"A")
    assert preset14_ok(dict(peak_to_last_pct=-35,down_days=14),"B")
    assert not preset14_ok(dict(peak_to_last_pct=-34.99,down_days=20),"B")
    assert preset14_ok(dict(peak_to_last_pct=-40,down_days=15),"C")
    assert not preset14_ok(dict(peak_to_last_pct=-40,down_days=14),"C")
    cut141=start+21*DAY
    plain141=[(start+i*DAY,100,129.99,90,100,10,1000) for i in range(22)]
    assert not spike141(plain141,cut141)["lookback_spike_excluded"]
    hist141=list(plain141);hist141[3]=(start+3*DAY,100,130,90,100,10,1000)
    assert spike141(hist141,cut141)["lookback_spike_excluded"] and spike141(hist141,cut141)["lookback_max_spike_date"]==iso(start+3*DAY)[:10]
    gap141=list(plain141);gap141[3]=(start+3*DAY,125,130,100,110,10,1000)
    assert spike141(gap141,cut141)["lookback_spike_excluded"]
    assert spike141(plain141[1:],cut141) is None
    assert spike141(plain141,cut141,130)["lookback_spike_excluded"]
    assert paper142_allowed(dict(preset="B",status="WATCH"),"WATCH")
    assert paper142_allowed(dict(preset="A",status="BUY"),"BUY")
    assert not paper142_allowed(dict(preset="B",status="WATCH"),"BUY")
    assert not paper142_allowed(dict(preset="C",status="WATCH"),"")
    assert not paper142_allowed(dict(preset="C",status="spike_excluded"),"spike_excluded")
    assert not paper142_allowed(dict(status="WATCH"),"WATCH")
    assert paper142_allowed(dict(status="BUY"),"")
    assert paper142_allowed(dict(status="MATCH"),"")
    assert preset14_ok(dict(peak_to_last_pct=-35,down_days=13),"D")
    assert not preset14_ok(dict(peak_to_last_pct=-35,down_days=12),"D")
    assert not preset14_ok(dict(peak_to_last_pct=-34.99,down_days=20),"D")
    assert not preset14_ok(dict(peak_to_last_pct=-35,down_days=13),"B")
    assert paper142_allowed(dict(preset="D",status="WATCH"),"WATCH")
    assert len(D15_ENTRIES)==174 and len({r[0] for r in D15_ENTRIES})==132
    e=["KRW-A",start,100,round(100*((1-FEE81)/(1+FEE81)-1),6)]
    check=d15row(e,{start+7*DAY-HOUR:(start+7*DAY-HOUR,100,100,100,100,1,100)},start+8*DAY)
    assert check["day7_check"]=="ok" and check["day1"] is None and check["day15"] is None
    # Synthetic, contiguous 1h data: prior uptrend, 2 bearish low-volume bars, then breakout.
    t20=start+30*HOUR
    raw20={}
    for i in range(25):
        op=100+i*.8;cl=op+.6
        raw20[t20-(25-i)*HOUR]=(t20-(25-i)*HOUR,op,cl+.1,op-.1,cl,100,10000)
    raw20[t20-2*HOUR]=(t20-2*HOUR,118,118.1,114.9,115,30,3000)
    raw20[t20-HOUR]=(t20-HOUR,115,115.1,111.9,112,30,3000)
    assert trend20signal(raw20,raw20,t20,"T1")[0]=="WATCH"
    raw20[t20]=(t20,112,116.1,111.9,116,200,20000)
    assert trend20signal(raw20,raw20,t20+HOUR,"T1")[0]=="BUY"
    assert trend20signal(raw20,raw20,t20,"T1")[0]=="WATCH"
    assert trend20out(raw20,t20,24) is None
    test4={start+HOUR*i:(start+HOUR*i,100,101,99,100,1,100) for i in range(3,7)}
    assert len(trend20bars(test4))==1
    assert not trend20bars({k:v for k,v in test4.items() if k!=start+4*HOUR})
    step=4*HOUR
    test21={start+i*step:(start+i*step,100,101,98,99 if i<15 else 100,1,100) for i in range(20)}
    why21,s21=trend21setup(test21,start+20*step,step)
    assert why21=="setup" and s21["down_bars"]==15
    test21[start]=(start,100,101,98,100,1,100)
    assert trend21setup(test21,start+20*step,step)[0]=="no_setup"
    del test21[start]
    assert trend21setup(test21,start+20*step,step)[0]=="history"
    test22={start+i*step:(start+i*step,100,101,98,99 if i%3!=2 else 100,1,100) for i in range(30)}
    why22,s22=trend21setup(test22,start+30*step,step,30,20)
    assert why22=="setup" and s22["down_bars"]==20 and s22["total_bars"]==30
    test22[start]=(start,100,101,98,100,1,100)
    assert trend21setup(test22,start+30*step,step,30,20)[0]=="no_setup"
    del test22[start]
    assert trend21setup(test22,start+30*step,step,30,20)[0]=="history"
    test23={start+i*step:(start+i*step,100,101,98,99 if i<34 else 100,1,100) for i in range(42)}
    for g in range(30,37):
        assert (trend21setup(test23,start+42*step,step,42,g)[0]=="setup")== (g<=34)
    cut23=start+18*DAY
    empty23,audit23=trend23eval({}, {},cut23,0)
    assert all(not empty23[g] and audit23[g]["missing_hours"]==360 for g in range(30,37))
    empty24,audit24=trend23eval({}, {},cut23,0,True)
    assert set(empty24)==set(range(10,21)) and all(not empty24[g] and audit24[g]["missing_hours"]==360 for g in range(10,21))
    # 15 bullish, 4 bearish, 1 flat in the completed 20-bar window.
    t24=cut23-18*DAY
    b24={t24-i*step:(t24-i*step,100,102,98,101 if i<=15 else 99 if i<=19 else 100,1,100) for i in range(1,22)}
    # first signal at t24+HOUR; setup boundary is t24.
    h24={t24+i*HOUR:(t24+i*HOUR,100,103 if i==0 else 101,99,102 if i==0 else 100,20 if i==0 else 10,1000) for i in range(-3,73)}
    trades24,a24=trend23eval(h24,b24,cut23,0,True)
    assert all(len(trades24[g])==1 and trades24[g][0]["up_bars"]==15 and trades24[g][0]["flat_bars"]==1 for g in range(10,16))
    assert all(not trades24[g] for g in range(16,21))
    # 18 bullish bars followed by two bearish bars. 108/100 -1=8%, 108/114 -1=-5.26%.
    t25=start+20*step
    b25={start+i*step:(start+i*step,100+i*.7,100+i*.7+2,99+i*.7,100+i*.7+1,1,100) for i in range(18)}
    b25[t25-2*step]=(t25-2*step,114,114,110,111,1,100)
    b25[t25-step]=(t25-step,111,112,107,108,1,100)
    w25,s25=trend25setup(b25,t25,step)
    assert w25=="setup" and s25["up_bars"]==18 and s25["pullback_bars"]==2
    assert 5<=s25["window_change_pct"]<10 and 2<=s25["pullback_pct"]<=10
    assert trend25setup(b25,t25-step,step)[0]=="history"
    empty25,audit25=trend23eval({}, {},cut23,0,False,True)
    assert set(empty25)=={3,5,10} and all(audit25[g]["missing_hours"]==360 for g in (3,5,10))
    r26=trend26regimes({},cut23,0)
    assert len(r26)==360 and all(v["btc_regime"]=="unknown" for v in r26.values())
    t26=cut23-18*DAY
    btc26={t26-i*step:(t26-i*step,100-i,101-i,99-i,100-i,1,100) for i in range(1,26)}
    assert trend26regimes(btc26,cut23,0)[t26]["btc_regime"]=="up"
    assert trend26group(110)["minimum_gain_pct"]==10 and trend26group(110)["market_filter"]=="btc_up"
    empty26,audit26=trend23eval({}, {},cut23,0,False,True,r26)
    assert set(empty26)=={3,5,10,103,105,110} and all(audit26[g]["missing_hours"]==360 for g in empty26)
    s27=dict(down_bars=15,window_change_pct=-15,last_4h_volume_ratio=.8)
    assert all(trend27match(s27,g) for g in range(4))
    assert [trend27match(dict(s27,last_4h_volume_ratio=1.5),g) for g in range(4)]==[True,True,False,False]
    assert not any(trend27match(dict(s27,down_bars=14),g) for g in range(4))
    empty27,audit27=trend23eval({}, {},cut23,0,False,False,None,True)
    assert set(empty27)==set(range(4)) and all(audit27[g]["missing_hours"]==360 for g in empty27)
    t28=start
    raw28={t28+i*HOUR:(t28+i*HOUR,100,104,96,100,1,100) for i in range(-4,72)}
    z28=exit28simulate(raw28,t28,"A1",2)
    assert z28["exit_reason"]=="stop" and abs(z28["realized_net_pct"]+2)<1e-8 and z28["ambiguous_bars"]==1
    raw28={t28+i*HOUR:(t28+i*HOUR,100,102,99.9,101,1,100) for i in range(-4,72)}
    z28=exit28simulate(raw28,t28,"B",2)
    assert z28["partial_taken"]==1 and z28["exit_reason"]=="72h" and z28["holding_hours"]==72
    assert exit28simulate({},t28,"A1",2) is None
    assert "백테스트 실행" not in HTML29 and 'id="back"' not in HTML29 and "preset=T415&back=0" in HTML29
    wrows=[(start+i*HOUR,100,101,99,100,10,1000) for i in range(97)]
    w=whale30candles(wrows,start+97*HOUR)
    assert w and w["candle_score"]==0 and w["change_7d_pct"] is None
    assert whale30candles(wrows[:-1],start+97*HOUR) is None
    assert whale30trades([],start*1000)["buy_flow"] is None
    wt=[dict(sequential_id=i,timestamp=start*1000+i*2000,trade_price=100,trade_volume=100,ask_bid="BID") for i in range(40)]
    assert whale30trades(wt,start*1000+80000)["buy_flow"] is True
    assert "pickWhale" in HTML29 and "whale30/start" in HTML29
    assert whale31qualified(dict(volume_flat=True,trade_checked=True,score=4))
    assert not whale31qualified(dict(volume_flat=False,trade_checked=True,score=5))
    assert not whale31qualified(dict(volume_flat=True,trade_checked=False,score=4))
    assert not whale31qualified(dict(volume_flat=True,trade_checked=True,score=3))
    assert whale31coverage(wrows[:-1],start+97*HOUR)["missing_hours_97"]==1
    assert whale31coverage(None,start+97*HOUR)["status"]=="fetch_error"
    old7=(start-72*HOUR,95,96,94,95,10,950)
    assert abs(whale30candles([old7]+wrows,start+97*HOUR)["change_7d_pct"]-pct(100,95))<1e-9
    for days in (1,3,7):
        n=days*6;step=4*HOUR;end32=start+(4*n+1)*step
        rows32=[(start+i*step,100,101,99,100,10 if i<3*n+1 else 20,1000) for i in range(4*n+1)]
        z,audit=whale32candles(rows32,end32,days)
        assert z and z["period_days"]==days and abs(z["volume_ratio"]-2)<1e-9 and z["volume_flat"] and z["candle_score"]==1
        assert whale32candles(rows32[:-1],end32,days)[1]["missing_bars"]==1
        assert whale32candles(rows32,end32,days)[1]["required_bars"]==4*n+1
    assert whale33class(dict(volume_flat=True,trade_checked=True,score=4,candle_score=3))=="STRONG"
    assert whale33class(dict(volume_flat=True,candle_score=3))=="WATCH"
    assert whale33class(dict(volume_flat=True,candle_score=2,matched_period_count=2))=="WATCH"
    assert whale33class(dict(volume_flat=True,candle_score=2,matched_period_count=1))=="NONE"
    assert whale33class(dict(volume_flat=False,candle_score=3,matched_period_count=3))=="NONE"
    # Whale research synthetic regression: exact forward windows, missing data, overlap.
    testrows=[(start+i*WBSTEP,100,106,99,100,10,1000) for i in range(250)]
    testby={r[0]:r for r in testrows};t=start+170*WBSTEP
    wbo=wboutcome(testby,t)
    assert wbo and wbo["hit5_within_hours"]==4 and wbo["hit10_within_hours"] is None
    assert wboutcome({k:v for k,v in testby.items() if k!=t+WBSTEP},t) is None
    cv,rs,sk,rr,ev=wbeval("KRW-TEST",testrows,t,t+4*WBSTEP,"test")
    assert cv["valid_common"]==4 and rs["ALL"]["evaluated"]==4 and sk["ALL"]==3
    assert rs["NONE"]["signals"]==4 and len([r for r in rr if r["group"]=="ALL"])==1
    sm=wbsummary(rs,sk,rr)
    assert sm[0]["hit5_7d_pct"]==100 and sm[0]["capture_hit5_7d_pct"]==100
    assert "whaleback/start" in HTMLWB
    assert wbpages(30)==2 and wbpages(90)==4
    # Window trimming must give exactly the same signals as the live calculator.
    for d in WB_DAYS:
        local=[testby[k] for k in range(t-169*WBSTEP,t,WBSTEP) if k in testby]
        assert whale32candles(local,t,d)[0]==whale32candles(testrows,t,d)[0]
    assert "entryDays" in HTMLWB and "V0.2" in HTMLWB
    print("LIVE V3.1.1 preserved + WHALE RESEARCH V0.2 self-test PASS",flush=True)
if __name__=="__main__":
    import sys
    if "--selftest" in sys.argv:selftest()
    else:
        selftest()
        ThreadingHTTPServer(("0.0.0.0",int(os.environ.get("PORT","8000"))),Handler).serve_forever()
