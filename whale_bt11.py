"""WHALE DETECTOR V1.1.1 - historical candle-only explosion detection."""
import csv, io, math, statistics, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs
import whale_detector as live
STEP=14400
DAY=86400
HORIZONS=(7,14,30)
TARGETS=(30,50,100)
GROUPS=("ALL","PRE","C3")
LABELS={"ALL":"전체 기준군","PRE":"기존 매집 예비 후보","C3":"캔들 정황 3/3"}
LOCK=threading.Lock()
STATE=dict(status="idle",phase="대기",done=0,total=0,summary=[],details=[],coverage=[],errors=[])
def patch(**kw):
    with LOCK:STATE.update(kw)
def newstat():
    return {"raw":0,"dedup":0,"raw_hit":0,"dedup_hit":0,"hours":[],"lows":[],"markets":set(),"hit_markets":set()}
def keyset():
    return [(g,d,k) for g in GROUPS for d in HORIZONS for k in TARGETS]
def market_eval(m,rows,start,stop,ctx):
    by={r[0]:r for r in rows};valid=ctx["r131valid"];iso=ctx["iso"]
    # Check OHLCV validity once per candle rather than hundreds of times.
    good={t for t,r in by.items() if valid(r)}
    stats={key:newstat() for key in keyset()}
    reserve={g:0 for g in GROUPS}
    details=[];cov=dict(market=m,received_bars=len(by),expected_windows=(stop-start)//STEP,valid_history=0,valid_outcome=0,missing_outcome=0,raw_candidates=0,raw_c3=0)
    for t in range(start,stop,STEP):
        if any(k not in good for k in range(t-169*STEP,t,STEP)):continue
        cov["valid_history"]+=1
        if any(k not in good for k in range(t,t+180*STEP,STEP)):
            cov["missing_outcome"]+=1;continue
        cov["valid_outcome"]+=1
        p=by[t][1]
        hist=[by[k] for k in range(t-169*STEP,t,STEP)]
        # Exact necessary volume_flat checks from whale32candles; only qualifying windows need full scoring.
        plausible=False
        for d,change,span in ((7,8,18),(3,5,12)):
            n=d*6;recent=hist[-n:];base=hist[-4*n:-n]
            v=statistics.fmean(r[5] for r in base)
            if v>0 and statistics.fmean(r[5] for r in recent)/v>=1.5 and abs(100*(recent[-1][4]/hist[-n-1][4]-1))<=change and 100*(max(r[2] for r in recent)/min(r[3] for r in recent)-1)<=span:
                plausible=True;break
        z=live.candidate(hist,t,ctx) if plausible else None
        future=[by[k] for k in range(t,t+180*STEP,STEP)]
        groups=["ALL"];score=None
        if z:
            move=100*(p/z["reference_price"]-1)
            window=100*(p/z["window_start_price"]-1)
            if abs(move)<=3 and abs(window)<=z["change_limit_pct"]:
                groups.append("PRE");cov["raw_candidates"]+=1
                score=sum(z.get(k) is True for k in live.KEYS[:3])
                if score==3:groups.append("C3");cov["raw_c3"]+=1
        selected={g:t>=reserve[g] for g in groups}
        for g in groups:
            if selected[g]:reserve[g]=t+30*DAY
        first={};lowest=0.;peak=0.;snap={}
        # Raw counts need only the horizon high; detailed paths only for deduplicated or candidate windows.
        detailed=any(selected.values()) or "PRE" in groups
        if not detailed:
            for d in HORIZONS:
                win=future[:d*6]
                snap[d]=(100*(max(r[2] for r in win)/p-1),None)
        for i,r in enumerate(future if detailed else (),1):
            low=100*(r[3]/p-1);high=100*(r[2]/p-1)
            lowest=min(lowest,low);peak=max(peak,high)
            for k in TARGETS:
                if k not in first and high>=k-1e-9:first[k]=(i*4,lowest)
            if i in (42,84,180):snap[i//6]=(peak,lowest)
        for g in groups:
            for d in HORIZONS:
                for k in TARGETS:
                    s=stats[g,d,k];s["raw"]+=1;s["markets"].add(m)
                    hit=snap[d][0]>=k-1e-9
                    if hit:s["raw_hit"]+=1;s["hit_markets"].add(m)
                    if selected[g]:
                        s["dedup"]+=1
                        if hit:
                            s["dedup_hit"]+=1;s["hours"].append(first[k][0]);s["lows"].append(first[k][1])
        if "PRE" in groups:
            detail=dict(market=m,signal_time=iso(t),signal_ts=t,entry_price_proxy=p,period_days=z["period_days"],
                        candle_score=score,original_candle_score=z["candle_score"],volume_ratio=z["volume_ratio"],
                        elevated_days=z["elevated_days"],persistent_flow=z["persistent_flow"],higher_low=z["higher_low"],
                        obv_ratio_pct=z["obv_ratio_pct"],period_change_pct=z["period_change_pct"],
                        period_range_pct=z["period_range_pct"],selected_pre=selected["PRE"],
                        selected_c3=selected.get("C3",False),trade_evidence="unknown",orderbook_evidence="unknown")
            for d,(hi,lo) in snap.items():
                detail["peak_"+str(d)+"d"]=hi;detail["low_"+str(d)+"d"]=lo
                for k in TARGETS:detail["hit_"+str(k)+"_"+str(d)+"d"]=k in first and first[k][0]<=d*24
            for k in TARGETS:
                detail["hit"+str(k)+"_hours_upper"]=first[k][0] if k in first else None
                detail["hit"+str(k)+"_low_including_hit_bar_pct"]=first[k][1] if k in first else None
            detail["extreme_peak_flag"]=int(snap[30][0]>300)
            details.append(detail)
    cov["status"]="ok" if cov["valid_outcome"] else "insufficient_history_or_outcome"
    return stats,details,cov
def summarize(stats,days,start,stop,cut,ctx,universe):
    out=[]
    for g,d,k in keyset():
        s=stats[g,d,k];base=stats["ALL",d,k]
        out.append(dict(group=g,label=LABELS[g],horizon_days=d,target_pct=k,raw_windows=s["raw"],
            raw_hits=s["raw_hit"],raw_hit_pct=100*s["raw_hit"]/s["raw"] if s["raw"] else None,
            episodes=s["dedup"],episode_hits=s["dedup_hit"],episode_hit_pct=100*s["dedup_hit"]/s["dedup"] if s["dedup"] else None,
            markets=len(s["markets"]),hit_markets=len(s["hit_markets"]),all_hit_markets=len(base["hit_markets"]),
            market_capture_pct=100*len(s["hit_markets"])/len(base["hit_markets"]) if base["hit_markets"] else None,
            raw_window_capture_pct=100*s["raw_hit"]/base["raw_hit"] if base["raw_hit"] else None,
            median_hit_hours_upper=statistics.median(s["hours"]) if s["hours"] else None,
            mean_low_including_hit_bar_pct=statistics.fmean(s["lows"]) if s["lows"] else None,
            median_low_including_hit_bar_pct=statistics.median(s["lows"]) if s["lows"] else None,
            signal_days=days,signal_start=ctx["iso"](start),signal_end_exclusive=ctx["iso"](stop),data_end=ctx["iso"](cut),
            universe=universe,price_return_gross=True,trade_evidence="unknown",orderbook_evidence="unknown"))
    return out
def run(ctx,days,limit):
    begin=time.monotonic()
    try:
        now=int(ctx["now"]().timestamp());candles=ctx["candles"];iso=ctx["iso"]
        probe=candles("KRW-BTC",240,now,8,latest=True)
        if len(probe)<2:raise RuntimeError("BTC 봉 경계 확인 실패")
        anchor=probe[-1][0]%STEP;cut=(now-anchor)//STEP*STEP+anchor
        if any(r[0]%STEP!=anchor for r in probe) or not any(r[0]==cut-STEP and ctx["r131valid"](r) for r in probe):raise RuntimeError("완료 봉 확인 실패")
        stop=cut-30*DAY+STEP;start=stop-days*DAY
        pages=math.ceil((days*6+169+179)/200)
        universe=sorted({r["market"] for r in ctx["api"]("market/all") if r["market"].startswith("KRW-") and r["market"][4:] not in ctx["STABLE"]})
        if not universe:raise RuntimeError("시장 목록 없음")
        total_universe=len(universe)
        if limit and limit<len(universe):
            # Deterministic evenly spaced sample of alphabetically sorted KRW symbols.
            universe=[universe[i*len(universe)//limit] for i in range(limit)]
        patch(phase="4시간봉 급등 역추적",total=len(universe),universe=len(universe),total_universe=total_universe,days=days,sample_limit=limit,signal_start=iso(start),signal_end_exclusive=iso(stop),data_end=iso(cut),pages=pages)
        stats={key:newstat() for key in keyset()};details=[];coverage=[];errors=[]
        def fetch(m):return market_eval(m,candles(m,240,cut,200,pages=pages,latest=False),start,stop,ctx)
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs={pool.submit(fetch,m):m for m in universe}
            for f in as_completed(jobs):
                m=jobs[f]
                try:
                    st,rows,c=f.result();details.extend(rows);coverage.append(c)
                    for key,s in st.items():
                        a=stats[key]
                        for k in ("raw","dedup","raw_hit","dedup_hit"):a[k]+=s[k]
                        for k in ("hours","lows"):a[k].extend(s[k])
                        for k in ("markets","hit_markets"):a[k].update(s[k])
                except Exception as e:
                    errors.append(dict(market=m,error=str(e)));coverage.append(dict(market=m,status="fetch_or_eval_error"))
                with LOCK:STATE["done"]+=1;STATE["error_count"]=len(errors)
        summary=summarize(stats,days,start,stop,cut,ctx,len(universe))
        for r in summary:r.update(total_universe=total_universe,sample_limit=limit,sample_method="alphabetically_spaced" if limit else "all")
        details.sort(key=lambda r:(r["signal_ts"],r["market"]))
        coverage.sort(key=lambda r:r["market"])
        patch(phase="CSV 생성 및 저장")
        rows=[dict(r,record_type=kind) for kind,data in (("summary",summary),("details",details),("coverage",coverage),("errors",errors)) for r in data]
        fields=["record_type"]+list(dict.fromkeys(k for r in rows for k in r if k!="record_type"))
        b=io.StringIO();w=csv.DictWriter(b,fieldnames=fields);w.writeheader();w.writerows(rows)
        payload=("\ufeff"+b.getvalue()).encode("utf-8")
        filename="WHALE_DETECTOR_V1_1_1_"+str(days)+"D_"+("ALL" if not limit else str(limit))+"_"+ctx["now"]().astimezone(ctx["KST"]).strftime("%Y%m%d%H%M")+".csv"
        patch(status="done",phase="완료",summary=summary,details=details,coverage=coverage,errors=errors,csv_bytes=payload,csv_name=filename,
              valid_markets=sum(c.get("valid_outcome",0)>0 for c in coverage),candidate_windows=len(details),
              common_windows=sum(c.get("valid_outcome",0) for c in coverage),error_count=len(errors),elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:patch(status="error",phase="실패",errors=[dict(error=str(e))],error_count=1)
def start(ctx,days=30,limit=80):
    with LOCK:
        if STATE["status"]=="running":return False
        STATE.clear();STATE.update(status="running",phase="준비",done=0,total=0,scan_id=str(time.time_ns()),summary=[],details=[],coverage=[],errors=[],error_count=0)
    threading.Thread(target=run,args=(ctx,days,limit),daemon=True).start();return True
HTML=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WHALE DETECTOR V1.1.1 BACKTEST</title><style>
*{box-sizing:border-box}[hidden]{display:none!important}body{max-width:840px;margin:auto;padding:14px;background:#f5f8f7;color:#172a23;font:14px/1.5 system-ui}section{background:white;border:1px solid #d7e3dd;border-radius:14px;padding:14px;margin:12px 0}h1{font-size:23px}button,a,select{font:inherit;padding:10px;border-radius:8px;border:1px solid #abc1b5;background:white;color:#176345;display:inline-block;text-decoration:none;margin:3px}button{background:#176345;color:white;width:97%;font-weight:bold}button:disabled{opacity:.5}progress{width:100%}small{color:#53645d;display:block}table{border-collapse:collapse;white-space:nowrap;font-size:12px;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:right}td:first-child,th:first-child{text-align:left}.scroll{overflow:auto}pre{white-space:pre-wrap}</style>
<h1>WHALE DETECTOR V1.1.1</h1><a href="/whaledetector">실시간 고래 매집 검색</a><section><h2>급등 역추적 백테스트</h2><label>검증 <select id="mode"><option value="30,80" selected>빠른 확인 · 30일 / 80종목</option><option value="30,0">30일 / 전체</option><option value="90,0">90일 / 전체</option></select></label><small>80종목은 가나다/알파벳 순 균등 표본이며 전체 시장 결과가 아닙니다. 전체 검증은 API 호출 제한으로 오래 걸릴 수 있습니다.</small><small>기존 실시간 매집 예비 검색식 재사용. 30일 후속 결과가 성숙한 선택 기간의 완료 4시간봉. 전체 KRW(스테이블 제외), 동일 169개 선행 봉 + 180개 후속 봉 유효 시점. 다음 봉 시가 기준, 기존 추격/이탈 제외 적용. 과거 체결·호가는 미확인으로 처리하며 STRONG 재현이 아님. 수수료 제외 가격 상승률.</small><button id="go">백테스트 시작</button><p id="phase">대기</p><progress id="bar" max="1" value="0"></progress><p id="audit"></p><pre id="err"></pre></section>
<section id="out" hidden><h2>대폭발 상승 포착 결과</h2><label>관찰 기간 <select id="days"><option>7</option><option>14</option><option selected>30</option></select>일</label><div class="scroll"><table id="tbl"></table></div><small>도달률: 동일 종목·그룹별 최초 신호부터 30일 중복 제외한 표본 기준. 전체 기준군은 동일하게 중복 제거하되 후보 시점과 다를 수 있음. 급등 종목 포착률: 중복 제거 전 유효 시점에서 해당 목표 상승을 보인 전체 종목 중 매집 후보 시점에서도 목표에 도달한 종목 비율. 고유 급등 사건 포착률이나 매매 승률 아님. 도달시간은 봉 종료 상한, 최저는 도달 봉 포함(봉 내부 순서 미확인). CSV에 원시 시점 도달률 및 전체 후보 포함. 현재 상장 종목 편향과 누락 편향 존재.</small><p id="links"></p></section>
<script>
const $=id=>document.getElementById(id);let data=[],loaded=null,busy=false,starting=false;const cacheKey='whalebt111_summary';
function download(blob,name){let a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;document.body.append(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},30000)}
function summaryCSV(){let keys=[...new Set(data.flatMap(Object.keys))],esc=v=>'"'+String(v??'').replaceAll('"','""')+'"';download(new Blob(['\ufeff'+[keys.map(esc).join(','),...data.map(r=>keys.map(k=>esc(r[k])).join(','))].join('\r\n')],{type:'text/csv;charset=utf-8'}),'WHALE_DETECTOR_V1_1_1_SUMMARY.csv')}
function links(){let a=document.createElement('a');a.href='#';a.textContent='전체 CSV 받기';a.onclick=async e=>{e.preventDefault();try{let res=await fetch('/whalebt11/csv?scan_id='+encodeURIComponent(loaded));if(!res.ok)throw Error('HTTP '+res.status);let name=(res.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/);download(await res.blob(),name?name[1]:'WHALE_DETECTOR_V1_1_1_FULL.csv')}catch(e){$('err').textContent='전체 CSV 실패: '+e.message+' · 요약 CSV는 아래에서 저장 가능'}};let b=document.createElement('a');b.href='#';b.textContent='요약 CSV (비상 저장)';b.onclick=e=>{e.preventDefault();summaryCSV()};$('links').replaceChildren(a,b)}
function f(x){return typeof x==='number'?x.toFixed(2):'—'}
function render(){let d=Number($('days').value),t=$('tbl');t.replaceChildren();let h=t.createTHead().insertRow();for(let k of ['구분','목표','포착 건수','도달 건수','도달률','급등 종목 포착률','중앙 도달시간','평균 최저']){let th=document.createElement('th');th.textContent=k;h.append(th)}let b=t.createTBody();for(let r of data.filter(x=>x.horizon_days===d)){let tr=b.insertRow();for(let v of [r.label,'+'+r.target_pct+'%',r.episodes,r.episode_hits,f(r.episode_hit_pct)+'%',f(r.market_capture_pct)+'%',f(r.median_hit_hours_upper)+'h',f(r.mean_low_including_hit_bar_pct)+'%'])tr.insertCell().textContent=v}}
$('days').onchange=render;
async function poll(){if(busy||starting)return;busy=true;try{let res=await fetch('/whalebt11/status',{cache:'no-store'});if(!res.ok)throw Error('HTTP '+res.status);let s=await res.json();$('go').disabled=s.status==='running';$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('bar').max=Math.max(1,s.total);$('bar').value=s.done;$('audit').textContent=(s.signal_start?'신호 '+s.signal_start+' ~ '+s.signal_end_exclusive+' (미포함) · ':'')+'오류 '+(s.error_count||0)+(s.status==='done'?' · 유효 종목 '+s.valid_markets+' · 공통 시점 '+s.common_windows+' · 매집 시점 '+s.candidate_windows+' · '+s.elapsed_seconds+'초':'');$('err').textContent=(s.error_preview||[]).map(e=>(e.market||'')+' '+e.error).join('\n');if(s.status==='running'){$('out').hidden=true;loaded=null}if(s.status==='done'&&loaded!==s.scan_id){let x=await(await fetch('/whalebt11/results',{cache:'no-store'})).json();if(x.scan_id!==s.scan_id)return;data=x.summary;loaded=s.scan_id;try{localStorage.setItem(cacheKey,JSON.stringify({data,loaded}))}catch(e){}render();links();$('out').hidden=false}}catch(e){$('phase').textContent='연결 오류: '+e.message;$('go').disabled=false}finally{busy=false}}
$('go').onclick=async()=>{if(busy||starting)return;starting=true;$('go').disabled=true;try{let r=await fetch('/whalebt11/start?days='+$('mode').value.split(',')[0]+'&limit='+$('mode').value.split(',')[1],{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('이미 실행 중');loaded=null;$('out').hidden=true}catch(e){$('phase').textContent=e.message;$('go').disabled=false}finally{starting=false;poll()}};try{let old=JSON.parse(localStorage.getItem(cacheKey)||'null');if(old&&Array.isArray(old.data)&&old.data.length){data=old.data;loaded=old.loaded;render();links();$('out').hidden=false}}catch(e){}poll();setInterval(poll,2000);
</script></html>"""
def handle(h,u,ctx,post=False):
    p=u.path
    if not (p=="/whalebt11" or p.startswith("/whalebt11/")):return False
    if post and p=="/whalebt11/start":
        try:
            q=parse_qs(u.query);days=int(q.get("days",["30"])[0]);limit=int(q.get("limit",["80"])[0])
            if (days,limit) not in ((30,80),(30,0),(90,0)):raise ValueError()
        except ValueError:h.js({"error":"invalid mode"},400);return True
        h.js({"started":start(ctx,days,limit)})
    elif not post and p=="/whalebt11":h.send(HTML,"text/html; charset=utf-8")
    elif not post and p=="/whalebt11/status":
        with LOCK:
            d={k:v for k,v in STATE.items() if k not in ("summary","details","coverage","errors","csv_bytes")};d["error_preview"]=STATE["errors"][:10]
        h.js(d)
    elif not post and p=="/whalebt11/results":
        with LOCK:d=dict(scan_id=STATE.get("scan_id"),summary=STATE["summary"])
        h.js(d)
    elif not post and p=="/whalebt11/csv":
        q=parse_qs(u.query)
        with LOCK:
            ok=STATE["status"]=="done" and q.get("scan_id",[""])[0]==STATE.get("scan_id")
            payload=STATE.get("csv_bytes") if ok else None;name=STATE.get("csv_name")
        if payload is None:h.js({"error":"scan result expired; use browser summary CSV"},409);return True
        h.send(payload,"text/csv; charset=utf-8",filename=name)
    else:h.js({"error":"not found"},404)
    return True
