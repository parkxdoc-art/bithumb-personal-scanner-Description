"""WHALE DETECTOR V1.2: reverse research of large price increases."""
import csv,io,math,statistics,threading,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from urllib.parse import parse_qs
import whale_detector as live
STEP=14400
DAY=86400
TARGETS=(30,50,100)
LOCK=threading.Lock()
STATE=dict(status="idle",phase="대기",done=0,total=0,summary=[],details=[],coverage=[],errors=[])
def patch(**kw):
    with LOCK:STATE.update(kw)
def evaluate(m,rows,start,stop,ctx):
    by={r[0]:r for r in rows};valid=ctx["r131valid"];iso=ctx["iso"]
    good={t for t,r in by.items() if valid(r)}
    windows=[];cov=dict(market=m,received_bars=len(by),expected_windows=(stop-start)//STEP,valid_windows=0,missing_history=0,missing_future=0)
    for t in range(start,stop,STEP):
        if any(k not in good for k in range(t-169*STEP,t,STEP)):
            cov["missing_history"]+=1;continue
        if any(k not in good for k in range(t,t+42*STEP,STEP)):
            cov["missing_future"]+=1;continue
        p=by[t][1];future=[by[k] for k in range(t,t+42*STEP,STEP)]
        peak=100*(max(r[2] for r in future)/p-1)
        windows.append((t,peak))
    cov["valid_windows"]=len(windows)
    events=[];until=0
    for t,peak in windows:
        if peak>=30-1e-9 and t>=until:
            events.append((t,peak));until=t+7*DAY
    # Controls have a full non-rising target window and do not overlap selected event outcome windows.
    controls=[];until=0
    for t,peak in windows:
        if peak>=30-1e-9 or t<until or any(abs(t-e)<7*DAY for e,_ in events):continue
        controls.append((t,peak));until=t+7*DAY
    out=[]
    for kind,items in (("PUMP",events),("CONTROL",controls)):
        for t,peak in items:
            hist=[by[k] for k in range(t-169*STEP,t,STEP)];future=[by[k] for k in range(t,t+42*STEP,STEP)]
            p=future[0][1];r=dict(record_type="detail",market=m,kind=kind,reference_time=iso(t),reference_ts=t,reference_price=p,peak_7d_pct=peak,
            low_7d_pct=100*(min(b[3] for b in future)/p-1),extreme_peak_flag=peak>300)
            for target in TARGETS:
                hit=next((i for i,b in enumerate(future,1) if 100*(b[2]/p-1)>=target-1e-9),None)
                r["hit"+str(target)+"_hours_upper"]=hit*4 if hit else None
                r["hit"+str(target)+"_low_pct"]=100*(min(b[3] for b in future[:hit])/p-1) if hit else None
            for d in (1,3,7):
                z,_=ctx["whale32candles"](hist,t,d);n=d*6;recent=hist[-n:];base=hist[-4*n:-n]
                suffix="_"+str(d)+"d";v=statistics.fmean(b[6] for b in base)*6
                daily=[sum(b[6] for b in recent[i:i+6]) for i in range(0,n,6)]
                r["turnover_ratio"+suffix]=statistics.fmean(daily)/v if v>0 else None
                r["elevated_day_share"+suffix]=100*sum(q>=1.2*v for q in daily)/d if v>0 else None
                for k in ("period_change_pct","period_range_pct","volume_ratio","low_change_pct","obv_ratio_pct","volume_flat","higher_low","obv_up","candle_score"):
                    r[k+suffix]=z[k] if z else None
                r["old_prefilter"+suffix]=bool(z and z["volume_flat"] and z["candle_score"]>=(2 if d==7 else 3)) if d in (3,7) else None
            z=live.candidate(hist,t,ctx)
            r["old_pre"]=False;r["old_c3"]=False
            if z:
                move=100*(p/z["reference_price"]-1);window=100*(p/z["window_start_price"]-1)
                r["old_pre"]=abs(move)<=3 and abs(window)<=z["change_limit_pct"]
                r["old_c3"]=r["old_pre"] and all(z.get(k) is True for k in live.KEYS[:3])
                r["old_reason"]="포착" if r["old_pre"] else "가격 이탈 또는 추격"
            elif not r["volume_flat_3d"] and not r["volume_flat_7d"]:r["old_reason"]="3/7일 횡보·거래량 필수조건 미충족"
            else:r["old_reason"]="3/7일 매집 점수 부족 또는 자료 부족"
            out.append(r)
    cov.update(events=len(events),controls=len(controls),status="ok" if windows else "missing")
    return out,cov
def summarize(rows):
    result=[]
    cohorts={"CONTROL":[r for r in rows if r["kind"]=="CONTROL"]}
    for k in TARGETS:cohorts["PUMP"+str(k)]=[r for r in rows if r["kind"]=="PUMP" and r["peak_7d_pct"]>=k-1e-9]
    for name,rs in cohorts.items():
        n=len(rs)
        out=dict(record_type="summary",cohort=name,count=n,markets=len({r["market"] for r in rs}),old_pre_count=sum(r["old_pre"] for r in rs),old_c3_count=sum(r["old_c3"] for r in rs),old_pre_pct=100*sum(r["old_pre"] for r in rs)/n if n else None,extreme_peak_count=sum(r["extreme_peak_flag"] for r in rs))
        for d in (1,3,7):
            s="_"+str(d)+"d"
            for k in ("period_change_pct","period_range_pct","volume_ratio","turnover_ratio","elevated_day_share","low_change_pct","obv_ratio_pct"):
                vals=[r[k+s] for r in rs if r.get(k+s) is not None]
                out["median_"+k+s]=statistics.median(vals) if vals else None
            for k in ("volume_flat","higher_low","obv_up"):
                vals=[r[k+s] for r in rs if r.get(k+s) is not None]
                out[k+s+"_pct"]=100*sum(vals)/len(vals) if vals else None
        result.append(out)
    return result
def run(ctx,limit):
    begin=time.monotonic()
    try:
        candles=ctx["candles"];iso=ctx["iso"];end=int(ctx["now"]().timestamp())
        probe=candles("KRW-BTC",240,end,8,latest=True)
        if len(probe)<2:raise RuntimeError("BTC 봉 경계 확인 실패")
        anchor=probe[-1][0]%STEP;cut=(end-anchor)//STEP*STEP+anchor
        if any(r[0]%STEP!=anchor for r in probe) or not any(r[0]==cut-STEP and ctx["r131valid"](r) for r in probe):raise RuntimeError("완료 봉 확인 실패")
        stop=cut-7*DAY+STEP;start=stop-30*DAY
        universe=sorted({r["market"] for r in ctx["api"]("market/all") if r["market"].startswith("KRW-") and r["market"][4:] not in ctx["STABLE"]})
        if not universe:raise RuntimeError("시장 목록 없음")
        total=len(universe)
        if limit and limit<total:universe=[universe[i*total//limit] for i in range(limit)]
        pages=math.ceil((180+169+41)/200)
        patch(phase="급등 및 대조 구간 추출",total=len(universe),universe=len(universe),total_universe=total,limit=limit,signal_start=iso(start),signal_end_exclusive=iso(stop),data_end=iso(cut))
        rows=[];cov=[];errors=[]
        def fetch(m):return evaluate(m,candles(m,240,cut,200,pages=pages,latest=False),start,stop,ctx)
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs={pool.submit(fetch,m):m for m in universe}
            for f in as_completed(jobs):
                m=jobs[f]
                try:
                    a,c=f.result();rows.extend(a);cov.append(c)
                except Exception as e:errors.append(dict(market=m,error=str(e)));cov.append(dict(market=m,status="error"))
                with LOCK:STATE["done"]+=1;STATE["error_count"]=len(errors)
        rows.sort(key=lambda r:(r["kind"]!="PUMP",-r["peak_7d_pct"],r["market"],r["reference_ts"]))
        summary=summarize(rows)
        for r in summary:r.update(signal_start=iso(start),signal_end_exclusive=iso(stop),data_end=iso(cut),universe=len(universe),total_universe=total,valid_markets=sum(c.get("valid_windows",0)>0 for c in cov),sample_limit=limit,sample_method="alphabetically_spaced" if limit else "all")
        allrows=summary+rows+[dict(r,record_type="coverage") for r in cov]+[dict(r,record_type="error") for r in errors]
        fields=list(dict.fromkeys(k for r in allrows for k in r))
        b=io.StringIO();w=csv.DictWriter(b,fieldnames=fields);w.writeheader();w.writerows(allrows)
        name="WHALE_DETECTOR_V1_2_REVERSE_30D_"+(str(limit) if limit else "ALL")+"_"+ctx["now"]().astimezone(ctx["KST"]).strftime("%Y%m%d%H%M")+".csv"
        patch(status="done",phase="완료",summary=summary,details=rows,coverage=cov,errors=errors,csv_bytes=("\ufeff"+b.getvalue()).encode("utf-8"),csv_name=name,valid_markets=sum(c.get("valid_windows",0)>0 for c in cov),error_count=len(errors),elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:patch(status="error",phase="실패",errors=[dict(error=str(e))],error_count=1)
def start(ctx,limit):
    with LOCK:
        if STATE["status"]=="running":return False
        STATE.clear();STATE.update(status="running",phase="준비",done=0,total=0,scan_id=str(time.time_ns()),summary=[],details=[],coverage=[],errors=[],error_count=0)
    threading.Thread(target=run,args=(ctx,limit),daemon=True).start();return True
HTML=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WHALE DETECTOR V1.2</title><style>
*{box-sizing:border-box}[hidden]{display:none!important}body{font:14px/1.55 system-ui;max-width:820px;margin:auto;padding:14px;background:#f5f8f7;color:#172a23}section{background:white;border:1px solid #d7e3dd;border-radius:12px;padding:14px;margin:12px 0}h1{font-size:23px}button,a,select{display:inline-block;padding:10px;margin:3px;border-radius:8px;border:1px solid #a5bbb0;background:white;color:#176345;text-decoration:none;font:inherit}button{background:#176345;color:white;width:97%}button:disabled{opacity:.5}progress{width:100%}small{display:block;color:#53645d}.scroll{overflow:auto}table{border-collapse:collapse;white-space:nowrap;font-size:12px;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:right}td:first-child,th:first-child{text-align:left}pre{white-space:pre-wrap}</style>
<h1>WHALE DETECTOR V1.2</h1><small>실제 급등 종목 역추적 · 매집 공통점 확인</small><a href="/whaledetector">기존 실시간</a><a href="/whalebt11">기존 백테스트</a><section><label>30일 검증 <select id="limit"><option value="80">80종목 빠른 확인</option><option value="0">전체 KRW</option></select></label><small>7일 결과가 성숙한 최근 30일의 4시간 시점. 다음 봉 시가 대비 7일 내 +30% 이상을 기록한 최초 기준 시점부터 같은 종목 7일 중복 제거. +50/+100%는 그 급등 사례의 부분집합. 실제 상승 개시 시점으로 단정하지 않음. 비급등 대조군은 +30% 미만이며 선정 급등 구간과 후속 7일이 겹치지 않도록 선택. 1/3/7일 전 완료 봉 특징 비교. 과거 체결·호가 미확인. 현재 상장 종목 기준, 80개는 알파벳 순 균등 표본.</small><button id="go">급등 역추적 시작</button><p id="phase">대기</p><progress id="bar" max="1" value="0"></progress><p id="audit"></p><pre id="err"></pre></section>
<section id="out" hidden><h2>급등군 vs 비급등군</h2><label>선행 기간 <select id="days"><option>1</option><option selected>3</option><option>7</option></select>일</label><div class="scroll"><table id="tbl"></table></div><p><small>급등군과 대조군의 중앙값 비교. 사후 선택된 표본의 특징이며 예측 성능이나 고래 매집 입증이 아님. +50/+100%군은 중복 포함. 기존 포착률은 선정 기준 시점에서의 캔들 예비 검색식 통과 비율.</small></p><h2>급등 사례 상위 20개</h2><div class="scroll"><table id="events"></table></div><p id="links"></p></section>
<script>
const $=id=>document.getElementById(id);let data=[],events=[],loaded=null,busy=false,starting=false;const cacheKey='whale_reverse12_summary';
const f=x=>typeof x==='number'?x.toFixed(2):'—';
function table(t,heads,rows){t.replaceChildren();let h=t.createTHead().insertRow();for(let x of heads){let th=document.createElement('th');th.textContent=x;h.append(th)}let b=t.createTBody();for(let row of rows){let tr=b.insertRow();for(let v of row)tr.insertCell().textContent=v}}
function render(){let s='_'+$('days').value+'d';table($('tbl'),['구분','사례','기존 포착','가격변화%','범위%','거래량배','거래대금배','유입일%','저점변화%','OBV%'],data.map(r=>[r.cohort==='CONTROL'?'비급등':r.cohort.replace('PUMP','+'),r.count,f(r.old_pre_pct)+'%',...['period_change_pct','period_range_pct','volume_ratio','turnover_ratio','elevated_day_share','low_change_pct','obv_ratio_pct'].map(k=>f(r['median_'+k+s]))]));table($('events'),['종목','기준 시각','7일 최고','기존 포착','미포착 이유'],events.map(r=>[r.market,r.reference_time,f(r.peak_7d_pct)+'%',r.old_pre?'포착':'미포착',r.old_reason]))}
function download(blob,name){let a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;document.body.append(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},30000)}
function links(){let a=document.createElement('a');a.href='#';a.textContent='전체 결과 CSV';a.onclick=async e=>{e.preventDefault();try{let r=await fetch('/whalereverse12/csv?scan_id='+encodeURIComponent(loaded));if(!r.ok)throw Error('HTTP '+r.status);let n=(r.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/);download(await r.blob(),n?n[1]:'WHALE_DETECTOR_V1_2_FULL.csv')}catch(e){$('err').textContent='전체 CSV 실패: '+e.message+' · 요약 CSV 저장 가능'}};let b=document.createElement('a');b.href='#';b.textContent='요약 CSV (비상 저장)';b.onclick=e=>{e.preventDefault();let k=[...new Set(data.flatMap(Object.keys))],esc=x=>'"'+String(x??'').replaceAll('"','""')+'"';download(new Blob(['\ufeff'+[k.map(esc).join(','),...data.map(r=>k.map(x=>esc(r[x])).join(','))].join('\r\n')],{type:'text/csv;charset=utf-8'}),'WHALE_DETECTOR_V1_2_SUMMARY.csv')};$('links').replaceChildren(a,b)}
$('days').onchange=render;
async function poll(){if(busy||starting)return;busy=true;try{let r=await fetch('/whalereverse12/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);let s=await r.json();$('go').disabled=$('limit').disabled=s.status==='running';$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('bar').max=Math.max(1,s.total);$('bar').value=s.done;$('audit').textContent=(s.signal_start?s.signal_start+' ~ '+s.signal_end_exclusive+' (미포함) · ':'')+'오류 '+(s.error_count||0)+(s.status==='done'?' · 유효 '+s.valid_markets+'종목 · '+s.elapsed_seconds+'초':'');$('err').textContent=(s.error_preview||[]).map(e=>(e.market||'')+' '+e.error).join('\n');if(s.status==='running'){$('out').hidden=true;loaded=null}if(s.status==='done'&&loaded!==s.scan_id){let x=await(await fetch('/whalereverse12/results',{cache:'no-store'})).json();if(x.scan_id!==s.scan_id)return;data=x.summary;events=x.events;loaded=s.scan_id;try{localStorage.setItem(cacheKey,JSON.stringify({data,events,loaded}))}catch(e){}render();links();$('out').hidden=false}}catch(e){$('phase').textContent='연결 오류: '+e.message;$('go').disabled=false}finally{busy=false}}
$('go').onclick=async()=>{if(busy||starting)return;starting=true;$('go').disabled=true;try{let r=await fetch('/whalereverse12/start?limit='+$('limit').value,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('이미 실행 중');loaded=null;$('out').hidden=true}catch(e){$('phase').textContent=e.message;$('go').disabled=false}finally{starting=false;poll()}};try{let x=JSON.parse(localStorage.getItem(cacheKey)||'null');if(x&&Array.isArray(x.data)&&x.data.length){data=x.data;events=x.events||[];loaded=x.loaded;render();links();$('out').hidden=false}}catch(e){}poll();setInterval(poll,2000);
</script></html>"""
def handle(h,u,ctx,post=False):
    p=u.path
    if not(p=="/whalereverse12" or p.startswith("/whalereverse12/")):return False
    q=parse_qs(u.query)
    if post and p=="/whalereverse12/start":
        try:
            limit=int(q.get("limit",["80"])[0])
            if limit not in (0,80):raise ValueError()
        except ValueError:h.js({"error":"invalid limit"},400);return True
        h.js({"started":start(ctx,limit)})
    elif not post and p=="/whalereverse12":h.send(HTML,"text/html; charset=utf-8")
    elif not post and p=="/whalereverse12/status":
        with LOCK:
            d={k:v for k,v in STATE.items() if k not in ("summary","details","coverage","errors","csv_bytes")};d["error_preview"]=STATE["errors"][:10]
        h.js(d)
    elif not post and p=="/whalereverse12/results":
        with LOCK:d=dict(scan_id=STATE.get("scan_id"),summary=STATE["summary"],events=[r for r in STATE["details"] if r["kind"]=="PUMP"][:20])
        h.js(d)
    elif not post and p=="/whalereverse12/csv":
        with LOCK:
            ok=STATE["status"]=="done" and q.get("scan_id",[""])[0]==STATE.get("scan_id");payload=STATE.get("csv_bytes") if ok else None;name=STATE.get("csv_name")
        if payload is None:h.js({"error":"scan expired; use summary CSV"},409);return True
        h.send(payload,"text/csv; charset=utf-8",filename=name)
    else:h.js({"error":"not found"},404)
    return True
