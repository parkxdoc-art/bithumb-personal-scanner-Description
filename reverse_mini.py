import os, json, csv, io, time, math, threading, statistics, traceback
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = "REVERSE MINI V0.1.1"
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
            with urlopen(Request(u,headers={"Accept":"application/json","User-Agent":"ReverseMini/0.1.1"}),timeout=12) as r:
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
    f["bull_candle"]=r[4]>r[1]
    f["lower_wick_40pct"]=f["lower_wick_ratio"]>=.4 if f["lower_wick_ratio"] is not None else None
    if btc:
        bm={x[0]:x[4] for x in btc if x[0]+unit*60<=cut}
        if rows[-1][0] in bm and rows[-6][0] in bm:
            f["btc_relative_5"]=f["ret_5"]-pct(bm[rows[-1][0]],bm[rows[-6][0]])
            f["btc_outperform"]=f["btc_relative_5"]>0
    return f
PATTERNS={"volume_2x":"거래량 2배","value_2x":"거래대금 2배","volume_3bar_15x":"최근 3봉 거래량 1.5배","higher_low":"저점 상승","breakout_20":"20봉 고점 돌파","rsi_rising":"RSI 상승","rsi_above_50":"RSI 50 이상","ma5_above_ma20":"MA5 > MA20","ma_cluster_1pct":"이평 응축 1%","bb_squeeze":"볼린저 폭 축소","macd_bull_cross":"MACD 히스토그램 양전환","macd_positive":"MACD 히스토그램 양수","positive_5bar":"5봉 상승","bull_candle":"양봉","lower_wick_40pct":"아랫꼬리 40%","btc_outperform":"BTC 상대강도 양수"}
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
    key=(unit,cut//(unit*60))
    with btc_lock:
        if key in btc_cache:return btc_cache[key]
    data=candles("KRW-BTC",unit,cut//(unit*60)*(unit*60),40)
    with btc_lock: btc_cache[key]=data
    return data
def analyze(case,all_daily,run_end):
    r=dict(case); m=r["market"]; start=r["start"]; end=min(start+DAY,run_end)
    try:
        five=candles(m,5,end,200,2)
        event=first_event(five,start,end,r["previous_close"],r["threshold"])
    except Exception as e:
        r["event_status"]="fetch_error"; record_error(m+" event: "+str(e));return r,[]
    if not event:
        r["event_status"]="no_confirmed_cross" if five else "missing_data"
        return r,[]
    # Check coverage from day start; if missing candles, the first observed crossing is not provably first.
    before=[x for x in five if start<=x[0]<event]
    r["event_coverage_ok"]=len(before)==(event-start)//300
    r["event_status"]="cross_confirmed" if r["event_coverage_ok"] else "first_observed_cross_gap"
    r["event_kst"]=iso(event)
    r["event_elapsed_min"]=(event-start)//60
    fs=[]
    for label,delta,unit in WINDOWS:
        cut=event-delta
        item={"market":m,"day":r["day"],"rank":r["rank"],"partial":r["partial"],"short_history":r["short_history"],"event_status":r["event_status"],"event_kst":r["event_kst"],"offset":label,"unit_minutes":unit,"cut_kst":iso(cut)}
        try:
            rows=candles(m,unit,cut//(unit*60)*(unit*60),200) if unit!=1440 else [x for x in all_daily[m] if x[0]+DAY<=cut]
            if unit==1440 and rows and len(rows)<200:
                try: rows=candles(m,unit,cut//DAY*DAY,200)
                except Exception as e: record_error(m+" daily history: "+str(e))
            try: btc=btc_history(unit,cut)
            except Exception as e: btc=None; record_error("BTC "+label+": "+str(e))
            item.update(features(rows,unit,cut,btc))
        except Exception as e:
            item["valid"]=False;item["error"]=str(e);record_error(m+" "+label+": "+str(e))
        fs.append(item)
    return r,fs
def summarize(fs):
    out=[]
    for cohort in ("regular","short_history","partial"):
        for label,_,unit in WINDOWS:
            group=[x for x in fs if x["offset"]==label and x.get("event_status")=="cross_confirmed" and (("partial" if x["partial"] else "short_history" if x["short_history"] else "regular")==cohort)]
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
        run_end=int(now().timestamp()); current=run_end//DAY*DAY
        starts=[current-i*DAY for i in range(days,0,-1)]+([current] if include_today else [])
        markets=api("market/all")
        universe=sorted({x["market"] for x in markets if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE})
        patch(phase="일별 전체 시장 순위 수집",done=0,total=len(universe),universe=len(universe),run_kst=iso(run_end))
        if not universe: raise RuntimeError("원화 마켓 목록 0개: API 목록 응답 "+str(markets[:2]))
        probe=candles("KRW-BTC",1440,run_end,30,latest=True)
        patch(probe_count=len(probe),probe_latest=iso(probe[-1][0]) if probe else "없음")
        if not probe or probe[-1][0]<current-DAY: raise RuntimeError("BTC 일봉 최신 데이터 없음: "+str([iso(x[0]) for x in probe[-3:]]))
        data={}
        for m,rows in parallel(universe,lambda m:candles(m,1440,run_end,200,latest=True)):
            if rows: data[m]=rows
            else: record_error(m+" 일봉 응답 없음")
        rank,coverage=rank_days(data,starts,current,threshold)
        patch(rank=rank,coverage=coverage,market_success=len(data),phase="급등 전 완료봉 역추적",done=0,total=len(rank))
        if not any(x["valid_markets"] for x in coverage):
            raise RuntimeError("일별 순위 유효 종목 0개. BTC 최신 일봉: "+str([iso(x[0]) for x in probe[-3:]]))
        results=[]; fs=[]
        for case,res in parallel(rank,lambda x:analyze(x,data,run_end)):
            if res is None:
                results.append(dict(case,event_status="analysis_error"));continue
            r,f=res;results.append(r);fs.extend(f)
        results.sort(key=lambda x:(x["start"],x["rank"]))
        fs.sort(key=lambda x:(x["day"],x["rank"],[a[0] for a in WINDOWS].index(x["offset"])))
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
HTML=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>REVERSE MINI V0.1.1</title><style>
*{box-sizing:border-box}body{margin:0 auto;max-width:1050px;padding:16px;background:#10151d;color:#edf2f7;font:14px system-ui,sans-serif}h1{font-size:23px;margin:8px 0}h2{font-size:17px;margin:18px 0 8px}.card{background:#19232e;border:1px solid #324355;border-radius:12px;padding:14px;margin:12px 0}label{display:inline-block;margin:6px}input,select,button,a.btn{background:#263646;color:#fff;border:1px solid #476073;border-radius:7px;padding:9px;font:inherit}button,a.btn{cursor:pointer;background:#225b51;text-decoration:none;display:inline-block;margin:4px}button:disabled{opacity:.5}.muted{color:#b4c4d1;line-height:1.6}progress{width:100%;accent-color:#37b89d}.scroll{overflow:auto;max-height:470px}table{border-collapse:collapse;width:100%;white-space:nowrap;font-size:13px}th,td{padding:8px;text-align:left;border-bottom:1px solid #324355}th{position:sticky;top:0;background:#253443}.pos{color:#6be3b7}pre{white-space:pre-wrap;overflow-wrap:anywhere}small{color:#b4c4d1}</style>
<h1>REVERSE MINI V0.1.1</h1><div class="muted">빗썸 원화마켓 · 일별 상승 TOP 10 · 급등 전 공통점 연구 전용</div>
<div class="card"><label>완료 일수 <select id="days"><option>3</option><option>7</option><option selected>14</option></select></label><label>급등 기준 <select id="threshold"><option>3</option><option selected>5</option><option>10</option></select>%</label><label><input id="today" type="checkbox" checked> 오늘 미완료 별도</label><button id="go">백테스트 시작</button><div class="muted">일봉 KST 09:00~익일 09:00. 전일 종가 대비 당일 종가 상승률 순위. 최초 기준 도달 5분봉 종가 이전 완료봉만 분석. 현재 상장 목록 기준이며 스테이블코인 목록은 제한적. 짧은 이력(이전 일봉 14개 미만)은 별도. 매수 조건 없음.</div></div>
<div class="card"><strong id="phase">대기</strong> <span id="count"></span><progress id="progress" value="0" max="1"></progress><div id="stats" class="muted"></div></div>
<div id="output" hidden><div class="card"><h2>CSV 저장</h2><a class="btn" href="/csv/rank">일별 TOP10</a><a class="btn" href="/csv/features">상세 지표</a><a class="btn" href="/csv/summary">공통점</a><a class="btn" href="/csv/coverage">수집률</a><a class="btn" href="/csv/errors">오류</a><div class="muted">공통점은 완전 확인 사례만 집계. 분모는 해당 지표의 유효 데이터 수이며, 예측 성과나 수익률이 아님.</div></div>
<div class="card"><h2>공통점 발견율</h2><label>구분 <select id="cohort"><option value="regular">완료일 일반</option><option value="short_history">짧은 이력</option><option value="partial">오늘 미완료</option></select></label><label>시점 <select id="offset"><option>5분</option><option>15분</option><option>30분</option><option>1시간</option><option>4시간</option><option>24시간</option><option>3일</option><option>7일</option></select></label><label><input type="checkbox" id="pairs"> 2개 조건 조합</label><div class="scroll" id="summary"></div></div>
<div class="card"><h2>일별 상승 TOP10</h2><div class="scroll" id="rank"></div></div><div class="card"><h2>일별 수집 범위</h2><div class="scroll" id="coverage"></div></div></div><div class="card"><details><summary>수집 오류 <span id="errCount">0</span>건</summary><pre id="errors"></pre></details></div>
<script>
const $=id=>document.getElementById(id);let result=null;
function table(el,headers,rows){let t=document.createElement('table'),h=t.createTHead().insertRow();headers.forEach(v=>{let th=document.createElement('th');th.textContent=v;h.appendChild(th)});let b=t.createTBody();rows.forEach(row=>{let tr=b.insertRow();row.forEach(v=>{let td=tr.insertCell();td.textContent=v??'—'})});$(el).replaceChildren(t)}
function summary(){if(!result)return;let a=result.summary.filter(x=>x.cohort===$('cohort').value&&x.offset===$('offset').value&&x.pattern.includes('+')===$('pairs').checked);a.sort((a,b)=>(b.hit_pct??-1)-(a.hit_pct??-1)||b.available-a.available);table('summary',['공통점','발견','유효','대상','발견율'],a.map(x=>[x.name,x.hits,x.available,x.cases,x.hit_pct==null?'—':x.hit_pct.toFixed(1)+'%']))}
['cohort','offset','pairs'].forEach(id=>$(id).onchange=summary);
$('go').onclick=async()=>{try{$('go').disabled=true;const r=await fetch('/start?days='+$('days').value+'&threshold='+$('threshold').value+'&today='+($('today').checked?1:0),{method:'POST'});if(!r.ok)throw Error(await r.text());$('output').hidden=true;result=null;await poll()}catch(e){$('phase').textContent='시작 실패: '+e.message;$('go').disabled=false}};
async function poll(){try{let r=await fetch('/status',{cache:'no-store'});if(!r.ok)throw Error(r.status);let s=await r.json();$('phase').textContent=s.phase;$('count').textContent=s.done+' / '+s.total;$('progress').max=Math.max(1,s.total);$('progress').value=s.done;$('go').disabled=s.status==='running';$('stats').textContent='시장 수집 '+(s.market_success??0)+' / '+(s.universe??0)+' · 순위 '+s.rank_count+'건 · 상세 '+s.feature_count+'건';$('errors').textContent=s.errors.join('\n');$('errCount').textContent=s.error_count;if((s.status==='done'||s.status==='error')&&!result){let x=await fetch('/results');if(!x.ok)throw Error(x.status);result=await x.json();$('output').hidden=false;summary();table('rank',['일봉 날짜','순위','코인','상승률','급등 확인','시점','구분'],result.rank.map(x=>[x.day,x.rank,x.market,x.gain_pct.toFixed(2)+'%',x.event_status,x.event_kst,x.partial?'미완료':x.short_history?'짧은 이력':'일반']));table('coverage',['일자','유효 시장','상승 시장','선정'],result.coverage.map(x=>[x.day+(x.partial?' (미완료)':''),x.valid_markets,x.positive_markets,x.selected]))}}catch(e){$('phase').textContent='연결 재확인 중: '+e.message} }
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
        if u.path!="/start":return self.js({"error":"not found"},404)
        q=parse_qs(u.query)
        try:
            days=int(q.get("days",["14"])[0]);threshold=int(q.get("threshold",["5"])[0]);today=q.get("today",["1"])[0]=="1"
            if days not in (3,7,14) or threshold not in (3,5,10):raise ValueError()
        except ValueError:return self.js({"error":"invalid settings"},400)
        return self.js({"started":start_job(days,threshold,today)})
    def do_GET(self):
        p=urlparse(self.path).path
        if p=="/":return self.send(HTML,"text/html; charset=utf-8")
        if p=="/health":return self.js({"version":VERSION,"ok":True})
        with state_lock:
            if p=="/status":
                x={k:v for k,v in state.items() if k not in ("rank","features","summary","errors","coverage")}
                x.update(rank_count=len(state["rank"]),feature_count=len(state["features"]),error_count=len(state["errors"]),errors=state["errors"][-20:])
            elif p=="/results":x={k:state[k] for k in ("rank","summary","coverage")}
            elif p.startswith("/csv/"):
                key=p.split("/")[-1]
                if key not in ("rank","features","summary","coverage","errors"):return self.js({"error":"not found"},404)
                if state["status"] not in ("done","error"):return self.js({"error":"not complete"},409)
                x=list(state[key])
            else:return self.js({"error":"not found"},404)
        if not p.startswith("/csv/"):return self.js(x)
        if key=="errors":x=[{"error":v} for v in x]
        fields=list(dict.fromkeys(k for row in x for k in row))
        stream=io.StringIO();w=csv.DictWriter(stream,fieldnames=fields)
        if fields:w.writeheader();w.writerows(x)
        return self.send(("\ufeff"+stream.getvalue()).encode("utf-8"),"text/csv; charset=utf-8",filename="REVERSE_MINI_V0_1_1_"+key.upper()+"_"+now().astimezone(KST).strftime("%Y%m%d%H%M")+".csv")
def selftest():
    start=20000*DAY
    assert kststr(start).endswith("09:00:00")
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
    print("REVERSE MINI V0.1.1 self-test PASS",flush=True)
if __name__=="__main__":
    import sys
    if "--selftest" in sys.argv:selftest()
    else:
        selftest()
        ThreadingHTTPServer(("0.0.0.0",int(os.environ.get("PORT","8000"))),Handler).serve_forever()
