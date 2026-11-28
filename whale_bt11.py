"""WHALE DETECTOR V1.1 - historical candle-only explosion detection."""
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
    stats={key:newstat() for key in keyset()}
    reserve={g:0 for g in GROUPS}
    details=[];cov=dict(market=m,received_bars=len(by),expected_windows=(stop-start)//STEP,valid_history=0,valid_outcome=0,missing_outcome=0,raw_candidates=0,raw_c3=0)
    for t in range(start,stop,STEP):
        hist=[by.get(t-i*STEP) for i in range(169,0,-1)]
        if not all(valid(r) for r in hist):continue
        cov["valid_history"]+=1
        future=[by.get(t+i*STEP) for i in range(180)]
        if not all(valid(r) for r in future):
            cov["missing_outcome"]+=1;continue
        cov["valid_outcome"]+=1
        p=future[0][1]
        # Same candle prefilter, same no-chase bounds; next open is a historical proxy for live quote.
        z=live.candidate(hist,t,ctx)
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
        for i,r in enumerate(future,1):
            low=100*(r[3]/p-1);high=100*(r[2]/p-1)
            lowest=min(lowest,low);peak=max(peak,high)
            for k in TARGETS:
                if k not in first and high>=k-1e-9:first[k]=(i*4,lowest)
            if i in (42,84,180):snap[i//6]=(peak,lowest)
        for g in groups:
            for d in HORIZONS:
                for k in TARGETS:
                    s=stats[g,d,k];s["raw"]+=1;s["markets"].add(m)
                    hit=k in first and first[k][0]<=d*24
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
def run(ctx):
    begin=time.monotonic()
    try:
        days=90;now=int(ctx["now"]().timestamp());candles=ctx["candles"];iso=ctx["iso"]
        probe=candles("KRW-BTC",240,now,8,latest=True)
        if len(probe)<2:raise RuntimeError("BTC 봉 경계 확인 실패")
        anchor=probe[-1][0]%STEP;cut=(now-anchor)//STEP*STEP+anchor
        if any(r[0]%STEP!=anchor for r in probe) or not any(r[0]==cut-STEP and ctx["r131valid"](r) for r in probe):raise RuntimeError("완료 봉 확인 실패")
        stop=cut-30*DAY+STEP;start=stop-days*DAY
        pages=math.ceil((days*6+169+179)/200)
        universe=sorted({r["market"] for r in ctx["api"]("market/all") if r["market"].startswith("KRW-") and r["market"][4:] not in ctx["STABLE"]})
        if not universe:raise RuntimeError("시장 목록 없음")
        patch(phase="4시간봉 급등 역추적",total=len(universe),universe=len(universe),signal_start=iso(start),signal_end_exclusive=iso(stop),data_end=iso(cut),pages=pages)
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
        details.sort(key=lambda r:(r["signal_ts"],r["market"]))
        coverage.sort(key=lambda r:r["market"])
        patch(status="done",phase="완료",summary=summary,details=details,coverage=coverage,errors=errors,
              valid_markets=sum(c.get("valid_outcome",0)>0 for c in coverage),candidate_windows=len(details),
              common_windows=sum(c.get("valid_outcome",0) for c in coverage),error_count=len(errors),elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:patch(status="error",phase="실패",errors=[dict(error=str(e))],error_count=1)
def start(ctx):
    with LOCK:
        if STATE["status"]=="running":return False
        STATE.clear();STATE.update(status="running",phase="준비",done=0,total=0,scan_id=str(time.time_ns()),summary=[],details=[],coverage=[],errors=[],error_count=0)
    threading.Thread(target=run,args=(ctx,),daemon=True).start();return True
HTML=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WHALE DETECTOR V1.1 BACKTEST</title><style>
*{box-sizing:border-box}[hidden]{display:none!important}body{max-width:840px;margin:auto;padding:14px;background:#f5f8f7;color:#172a23;font:14px/1.5 system-ui}section{background:white;border:1px solid #d7e3dd;border-radius:14px;padding:14px;margin:12px 0}h1{font-size:23px}button,a,select{font:inherit;padding:10px;border-radius:8px;border:1px solid #abc1b5;background:white;color:#176345;display:inline-block;text-decoration:none;margin:3px}button{background:#176345;color:white;width:97%;font-weight:bold}button:disabled{opacity:.5}progress{width:100%}small{color:#53645d;display:block}table{border-collapse:collapse;white-space:nowrap;font-size:12px;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:right}td:first-child,th:first-child{text-align:left}.scroll{overflow:auto}pre{white-space:pre-wrap}</style>
<h1>WHALE DETECTOR V1.1</h1><a href="/whaledetector">실시간 고래 매집 검색</a><section><h2>90일 급등 역추적 백테스트</h2><small>기존 실시간 매집 예비 검색식 재사용. 30일 후속 결과가 성숙한 과거 90일의 완료 4시간봉. 전체 KRW(스테이블 제외), 동일 169개 선행 봉 + 180개 후속 봉 유효 시점. 다음 봉 시가 기준, 기존 추격/이탈 제외 적용. 과거 체결·호가는 미확인으로 처리하며 STRONG 재현이 아님. 수수료 제외 가격 상승률.</small><button id="go">90일 백테스트 시작</button><p id="phase">대기</p><progress id="bar" max="1" value="0"></progress><p id="audit"></p><pre id="err"></pre></section>
<section id="out" hidden><h2>대폭발 상승 포착 결과</h2><label>관찰 기간 <select id="days"><option>7</option><option>14</option><option selected>30</option></select>일</label><div class="scroll"><table id="tbl"></table></div><small>도달률: 동일 종목·그룹별 최초 신호부터 30일 중복 제외한 표본 기준. 전체 기준군은 동일하게 중복 제거하되 후보 시점과 다를 수 있음. 급등 종목 포착률: 중복 제거 전 유효 시점에서 해당 목표 상승을 보인 전체 종목 중 매집 후보 시점에서도 목표에 도달한 종목 비율. 고유 급등 사건 포착률이나 매매 승률 아님. 도달시간은 봉 종료 상한, 최저는 도달 봉 포함(봉 내부 순서 미확인). CSV에 원시 시점 도달률 및 전체 후보 포함. 현재 상장 종목 편향과 누락 편향 존재.</small><p id="links"></p></section>
<script>
const $=id=>document.getElementById(id);let data=[],loaded=null,busy=false,starting=false;
function f(x){return typeof x==='number'?x.toFixed(2):'—'}
function render(){let d=Number($('days').value),t=$('tbl');t.replaceChildren();let h=t.createTHead().insertRow();for(let k of ['구분','목표','포착 건수','도달 건수','도달률','급등 종목 포착률','중앙 도달시간','평균 최저']){let th=document.createElement('th');th.textContent=k;h.append(th)}let b=t.createTBody();for(let r of data.filter(x=>x.horizon_days===d)){let tr=b.insertRow();for(let v of [r.label,'+'+r.target_pct+'%',r.episodes,r.episode_hits,f(r.episode_hit_pct)+'%',f(r.market_capture_pct)+'%',f(r.median_hit_hours_upper)+'h',f(r.mean_low_including_hit_bar_pct)+'%'])tr.insertCell().textContent=v}}
$('days').onchange=render;
async function poll(){if(busy||starting)return;busy=true;try{let res=await fetch('/whalebt11/status',{cache:'no-store'});if(!res.ok)throw Error('HTTP '+res.status);let s=await res.json();$('go').disabled=s.status==='running';$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('bar').max=Math.max(1,s.total);$('bar').value=s.done;$('audit').textContent=(s.signal_start?'신호 '+s.signal_start+' ~ '+s.signal_end_exclusive+' (미포함) · ':'')+'오류 '+(s.error_count||0)+(s.status==='done'?' · 유효 종목 '+s.valid_markets+' · 공통 시점 '+s.common_windows+' · 매집 시점 '+s.candidate_windows+' · '+s.elapsed_seconds+'초':'');$('err').textContent=(s.error_preview||[]).map(e=>(e.market||'')+' '+e.error).join('\n');if(s.status==='running'){$('out').hidden=true;loaded=null}if(s.status==='done'&&loaded!==s.scan_id){let x=await(await fetch('/whalebt11/results',{cache:'no-store'})).json();if(x.scan_id!==s.scan_id)return;data=x.summary;loaded=s.scan_id;render();let a=document.createElement('a');a.href='/whalebt11/csv?scan_id='+encodeURIComponent(loaded);a.textContent='전체 결과 CSV 받기';$('links').replaceChildren(a);$('out').hidden=false}}catch(e){$('phase').textContent='연결 오류: '+e.message;$('go').disabled=false}finally{busy=false}}
$('go').onclick=async()=>{if(busy||starting)return;starting=true;$('go').disabled=true;try{let r=await fetch('/whalebt11/start',{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('이미 실행 중');loaded=null;$('out').hidden=true}catch(e){$('phase').textContent=e.message;$('go').disabled=false}finally{starting=false;poll()}};poll();setInterval(poll,2000);
</script></html>"""
def handle(h,u,ctx,post=False):
    p=u.path
    if not (p=="/whalebt11" or p.startswith("/whalebt11/")):return False
    if post and p=="/whalebt11/start":h.js({"started":start(ctx)})
    elif not post and p=="/whalebt11":h.send(HTML,"text/html; charset=utf-8")
    elif not post and p=="/whalebt11/status":
        with LOCK:
            d={k:v for k,v in STATE.items() if k not in ("summary","details","coverage","errors")};d["error_preview"]=STATE["errors"][:10]
        h.js(d)
    elif not post and p=="/whalebt11/results":
        with LOCK:d=dict(scan_id=STATE.get("scan_id"),summary=STATE["summary"])
        h.js(d)
    elif not post and p=="/whalebt11/csv":
        q=parse_qs(u.query)
        with LOCK:
            ok=STATE["status"]=="done" and q.get("scan_id",[""])[0]==STATE.get("scan_id")
            rows=[dict(r,record_type=kind) for kind in ("summary","details","coverage","errors") for r in STATE[kind]] if ok else []
        if not ok:h.js({"error":"scan again"},409);return True
        fields=["record_type"]+list(dict.fromkeys(k for r in rows for k in r if k!="record_type"))
        b=io.StringIO();w=csv.DictWriter(b,fieldnames=fields);w.writeheader();w.writerows(rows)
        h.send(("\ufeff"+b.getvalue()).encode(),"text/csv; charset=utf-8",filename="WHALE_DETECTOR_V1_1_90D_"+ctx["now"]().astimezone(ctx["KST"]).strftime("%Y%m%d%H%M")+".csv")
    else:h.js({"error":"not found"},404)
    return True
