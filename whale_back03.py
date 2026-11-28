"""WHALE RESEARCH V0.3: independent reversal-entry research, no orders."""
import csv, io, math, statistics, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs

VERSION="BITHUMB WHALE RESEARCH V0.3"
STEP=14400
DAY=86400
METHODS={"BASE":"즉시 진입","MA5":"MA5 상향 돌파","MA20":"MA20 상향 돌파","HIGH":"직전 6봉 고점 돌파","VOL":"양봉 + 거래량 1.5배","RS":"BTC 하락 중 상대 강세"}
GROUPS=("D3_3","D7_2","D7_3")
TARGETS=(3,5,10)
STOP=-3
LOCK=threading.Lock()
STATE={"status":"idle","phase":"대기","done":0,"total":0,"summary":[],"features":[],"signals":[],"coverage":[],"errors":[]}

def patch(**kw):
    with LOCK: STATE.update(kw)

def seq(by,t,n):
    return [by.get(t-i*STEP) for i in range(n,0,-1)]

def flags(by,btc,t,valid):
    a=seq(by,t,21)
    if not all(valid(r) for r in a):
        return {k:None for k in METHODS if k!="BASE"}
    c=[r[4] for r in a]
    last=a[-1]
    avg=statistics.fmean(r[5] for r in a[:-1])
    b0,b1=btc.get(t-7*STEP),btc.get(t-STEP)
    rs=None
    if valid(b0) and valid(b1):
        cr=c[-1]/c[-7]-1
        br=b1[4]/b0[4]-1
        rs=br<0 and cr>0 and cr>br
    return {"MA5":c[-2]<=statistics.fmean(c[-6:-1]) and c[-1]>statistics.fmean(c[-5:]),
            "MA20":c[-2]<=statistics.fmean(c[-21:-1]) and c[-1]>statistics.fmean(c[-20:]),
            "HIGH":c[-1]>max(r[2] for r in a[-7:-1]),
            "VOL":last[4]>last[1] and c[-1]>c[-2] and last[5]>=1.5*avg if avg>0 else None,
            "RS":rs}

def outcome(by,t,valid,net):
    a=[by.get(t+i*STEP) for i in range(42)]
    if not all(valid(r) for r in a): return None
    p=a[0][1]
    out={"entry_price":p,"peak_7d":net(p,max(r[2] for r in a)),
         "low_7d":net(p,min(r[3] for r in a)),"close_7d":net(p,a[-1][4])}
    out["extreme_peak_flag"]=int(out["peak_7d"]>100)
    for target in TARGETS:
        status="TIME"; pnl=out["close_7d"]; hours=168; ambiguous=0
        for i,r in enumerate(a):
            op,hi,lo=(net(p,r[j]) for j in (1,2,3))
            if op<=STOP: status="SL";pnl=op
            elif op>=target: status="TP";pnl=target
            elif lo<=STOP and hi>=target: status="SL";pnl=STOP;ambiguous=1
            elif lo<=STOP: status="SL";pnl=STOP
            elif hi>=target: status="TP";pnl=target
            else: continue
            hours=(i+1)*4;break
        k=str(target)
        out.update({"status_"+k:status,"net_"+k:pnl,"hours_upper_"+k:hours,"ambiguous_"+k:ambiguous})
    return out

def evaluate(m,rows,btc,start,stop,ctx):
    by={r[0]:r for r in rows}; valid=ctx["r131valid"]; iso=ctx["iso"]; net=ctx["wbnet"]
    reserved={g:0 for g in GROUPS}; features=[]; signals=[]
    cov={"market":m,"received_bars":len(by),"expected_windows":(stop-start)//STEP,"valid_common":0,"raw_candidates":0,"selected_episodes":0}
    for t in range(start,stop,STEP):
        hist=seq(by,t,169)
        if not all(valid(r) for r in hist): continue
        periods={d:ctx["whale32candles"](hist,t,d)[0] for d in (1,3,7)}
        if any(x is None for x in periods.values()):continue
        cov["valid_common"]+=1
        gs,_,_=ctx["wbgroups"](periods)
        chosen=[g for g in GROUPS if g in gs]
        if not chosen:continue
        cov["raw_candidates"]+=1
        detail={"market":m,"signal_time":iso(t),"signal_ts":t}
        for d in (3,7):
            for k in ("candle_score","volume_ratio","volume_flat","higher_low","obv_up","period_change_pct","period_range_pct"):
                detail[k+"_"+str(d)+"d"]=periods[d][k]
        for g in chosen:
            selected=t>=reserved[g]
            signals.append(dict(detail,group=g,selected=selected))
            if not selected:continue
            # Equal 8-day candidate reservation across arms: 24h confirmation + 7d outcome.
            reserved[g]=t+8*DAY;cov["selected_episodes"]+=1
            base=outcome(by,t,valid,net)
            triggers={"BASE":t}; unknown=set()
            for k in range(7):
                at=t+k*STEP
                f=flags(by,btc,at,valid)
                for method,v in f.items():
                    if method in triggers or method in unknown:continue
                    if v is None:unknown.add(method)
                    elif v:triggers[method]=at
            for method in METHODS:
                at=triggers.get(method)
                o=base if method=="BASE" else outcome(by,at,valid,net) if at is not None else None
                status=("evaluated" if o else "outcome_missing") if at is not None else ("trigger_unknown" if method in unknown else "no_trigger")
                row=dict(detail,group=g,method=method,entry_status=status,entry_time=iso(at) if at is not None else "",delay_hours=(at-t)//3600 if at is not None else None,**(o or {}))
                for target in TARGETS:
                    row["paired_base_net_"+str(target)]=base["net_"+str(target)] if base and o else None
                features.append(row)
    cov["status"]="ok" if cov["valid_common"] else "common_history_missing"
    return cov,features,signals

def summarize(features):
    out=[]
    for group in GROUPS:
        for method,label in METHODS.items():
            rr=[r for r in features if r["group"]==group and r["method"]==method]
            vv=[r for r in rr if r["entry_status"]=="evaluated"]
            n=len(vv)
            row={"group":group,"method":method,"label":label,"episodes":len(rr),"evaluated":n,
                 "no_trigger":sum(r["entry_status"]=="no_trigger" for r in rr),
                 "trigger_unknown":sum(r["entry_status"]=="trigger_unknown" for r in rr),
                 "outcome_missing":sum(r["entry_status"]=="outcome_missing" for r in rr),
                 "distinct_markets":len({r["market"] for r in vv}),
                 "mean_delay_hours":statistics.fmean(r["delay_hours"] for r in vv) if n else None,
                 "peak10_pct":100*sum(r["peak_7d"]>=10 for r in vv)/n if n else None,
                 "extreme_peak_count":sum(r["extreme_peak_flag"] for r in vv)}
            for target in TARGETS:
                k=str(target); a=[r["net_"+k] for r in vv]
                paired=[r["net_"+k]-r["paired_base_net_"+k] for r in vv if r["paired_base_net_"+k] is not None]
                row.update({"tp_"+k:sum(r["status_"+k]=="TP" for r in vv),
                            "sl_"+k:sum(r["status_"+k]=="SL" for r in vv),
                            "time_"+k:sum(r["status_"+k]=="TIME" for r in vv),
                            "ambiguous_"+k:sum(r["ambiguous_"+k] for r in vv),
                            "win_pct_"+k:100*sum(v>0 for v in a)/n if n else None,
                            "mean_net_"+k:statistics.fmean(a) if n else None,
                            "median_net_"+k:statistics.median(a) if n else None,
                            "paired_n_"+k:len(paired),
                            "paired_delta_"+k:statistics.fmean(paired) if paired else None,
                            "net_per_episode_"+k:sum(a)/len(rr) if rr and not row["trigger_unknown"] and not row["outcome_missing"] else None})
            out.append(row)
    return out

def run(days,ctx):
    begin=time.monotonic()
    try:
        now=ctx["now"]; candles=ctx["candles"]; iso=ctx["iso"]; valid=ctx["r131valid"]
        end=int(now().timestamp()); probe=candles("KRW-BTC",240,end,8,latest=True)
        if len(probe)<2:raise RuntimeError("BTC 봉 경계 확인 실패")
        anchor=probe[-1][0]%STEP; cut=(end-anchor)//STEP*STEP+anchor
        if any(r[0]%STEP!=anchor for r in probe) or not any(r[0]==cut-STEP and valid(r) for r in probe):
            raise RuntimeError("BTC 완료 봉 확인 실패")
        stop=cut-8*DAY+STEP;start=stop-days*DAY
        pages=math.ceil((days*6+169+47)/200)
        btc={r[0]:r for r in candles("KRW-BTC",240,cut,200,pages=pages)}
        universe=sorted({r["market"] for r in ctx["api"]("market/all") if r["market"].startswith("KRW-") and r["market"][4:] not in ctx["STABLE"]})
        if not universe:raise RuntimeError("시장 목록 없음")
        patch(phase="4시간봉 수집 및 진입 비교",total=len(universe),entry_start=iso(start),entry_end_exclusive=iso(stop),data_end=iso(cut),fee_roundtrip_pct=.08,pages=pages)
        cov=[];features=[];signals=[];errors=[]
        def fetch(m):
            return evaluate(m,candles(m,240,cut,200,pages=pages),btc,start,stop,ctx)
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs={pool.submit(fetch,m):m for m in universe}
            for f in as_completed(jobs):
                m=jobs[f]
                try:
                    c,rows,s=f.result();cov.append(c);features.extend(rows);signals.extend(s)
                except Exception as e:
                    errors.append({"market":m,"error":str(e)});cov.append({"market":m,"status":"fetch_or_eval_error"})
                with LOCK:STATE["done"]+=1;STATE["error_count"]=len(errors)
        summary=summarize(features)
        for r in summary:r.update(entry_days=days,entry_start=iso(start),entry_end_exclusive=iso(stop),fee_roundtrip_pct=.08,stop_net_pct=STOP,universe=len(universe),valid_common=sum(c.get("valid_common",0) for c in cov))
        features.sort(key=lambda r:(r["group"],r["signal_ts"],r["market"],r["method"]))
        signals.sort(key=lambda r:(r["group"],r["signal_ts"],r["market"]))
        patch(status="done",phase="완료",summary=summary,features=features,signals=signals,coverage=cov,errors=errors,error_count=len(errors),elapsed_seconds=round(time.monotonic()-begin,1),valid_markets=sum(c.get("valid_common",0)>0 for c in cov))
    except Exception as e:patch(status="error",phase="실패",errors=[{"error":str(e)}],error_count=1)

def start(days,ctx):
    if days not in (30,90):return False
    with LOCK:
        if STATE["status"]=="running":return False
        STATE.clear();STATE.update(status="running",phase="준비",done=0,total=0,days=days,scan_id=str(time.time_ns()),summary=[],features=[],signals=[],coverage=[],errors=[],error_count=0)
    threading.Thread(target=run,args=(days,ctx),daemon=True).start()
    return True

HTML="""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BITHUMB WHALE RESEARCH V0.3</title><style>
body{font:14px/1.5 system-ui;background:#f3f6f5;color:#172a23;max-width:1000px;margin:auto;padding:16px}section{background:white;border:1px solid #d6e2dc;padding:14px;border-radius:12px;margin:12px 0}h1{font-size:22px}button,a,select{font:inherit;display:inline-block;padding:10px;margin:4px;border-radius:8px;border:1px solid #a5bbb0;background:white;color:#176345;text-decoration:none}button{background:#176345;color:white;width:95%}button:disabled{opacity:.5}progress{width:100%}table{border-collapse:collapse;white-space:nowrap;font-size:12px;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:right}td:first-child,th:first-child{text-align:left}.scroll{overflow:auto}pre{white-space:pre-wrap}small{color:#53645d}[hidden]{display:none!important}</style>
<h1>BITHUMB WHALE RESEARCH V0.3</h1><a href="/whaleback02">V0.2 기존 검증</a><a href="/live311">기존 실시간</a>
<section><h2>매집 후 상승 전환 진입 비교</h2><label>기간 <select id="days"><option value="30">30일</option><option value="90" selected>90일</option></select></label><p>3일 3점 / 7일 2점 이상 / 7일 3점 · 완료 4시간봉 · 왕복 0.08%</p><small>매집 확정 직후 또는 24시간 내 최초 조건 충족 뒤 다음 봉 시가. 각 군별 8일 중복 제외. 진입 뒤 최대 7일, 순손절 -3%, 순익절 +3/5/10% 각각 독립 비교. 같은 봉 손절 우선, 갭 손절 시가. 거래 없음은 미진입, 자료 부족은 미확인. 고점 및 거래량 기준은 연구용 잠정값. 현재 상장 종목 기준이며 실제 체결/슬리피지는 미검증.</small><button id="go">백테스트 시작</button><p id="phase">대기</p><progress id="bar" max="1" value="0"></progress><pre id="err"></pre></section>
<section id="out" hidden><h2>비교 결과</h2><label>매집군 <select id="group"><option>D3_3</option><option>D7_2</option><option selected>D7_3</option></select></label><label>순익절 <select id="target"><option>3</option><option selected>5</option><option>10</option></select>%</label><p><small>평균 순수익과 동일 후보의 즉시 진입 대비 차이(pp). 미진입·미확인 건수도 표시. 전체 신호 CSV에는 중복 제외 전 후보까지 포함.</small></p><div class="scroll"><table id="tbl"></table></div><div id="links"></div></section>
<script>
const $=id=>document.getElementById(id);let data=[],loaded=null,busy=false,starting=false;
const fmt=x=>typeof x==='number'?x.toFixed(2)+'%':'—';
function render(){let t=$('tbl'),k=$('target').value;t.replaceChildren();let h=t.createTHead().insertRow();for(let x of ['방법','후보','평가','미진입','미확인','익절','손절','시간종료','승률','평균 순익','중앙값','동일후보 차이']){let th=document.createElement('th');th.textContent=x;h.append(th)}let b=t.createTBody();for(let r of data.filter(r=>r.group===$('group').value)){let tr=b.insertRow();for(let x of [r.label,r.episodes,r.evaluated,r.no_trigger,r.trigger_unknown+r.outcome_missing,r['tp_'+k],r['sl_'+k],r['time_'+k],fmt(r['win_pct_'+k]),fmt(r['mean_net_'+k]),fmt(r['median_net_'+k]),fmt(r['paired_delta_'+k])])tr.insertCell().textContent=x}}
$('group').onchange=render;$('target').onchange=render;
async function poll(){if(busy||starting)return;busy=true;try{let r=await fetch('/whaleback03/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);let s=await r.json();$('go').disabled=$('days').disabled=s.status==='running';$('phase').textContent=s.phase+' '+s.done+'/'+s.total+' · 오류 '+(s.error_count||0)+(s.elapsed_seconds?' · '+s.elapsed_seconds+'초':'');$('bar').max=Math.max(1,s.total);$('bar').value=s.done;$('err').textContent=(s.error_preview||[]).map(e=>(e.market||'')+' '+e.error).join('\\n');if(s.status==='running'){$('out').hidden=true;loaded=null}if(s.status==='done'&&loaded!==s.scan_id){let d=await(await fetch('/whaleback03/results',{cache:'no-store'})).json();if(d.scan_id!==s.scan_id)return;data=d.summary;loaded=s.scan_id;render();$('links').replaceChildren(...['summary','features','signals','coverage','errors'].map((k,i)=>{let a=document.createElement('a');a.href='/whaleback03/csv?kind='+k+'&scan_id='+encodeURIComponent(loaded);a.textContent=['요약 CSV','진입 상세 CSV','전체 매집 신호 CSV','수집 상태 CSV','오류 CSV'][i];return a}));$('out').hidden=false}}catch(e){$('phase').textContent='연결 오류: '+e.message;$('go').disabled=false;$('days').disabled=false}finally{busy=false}}
$('go').onclick=async()=>{if(busy||starting)return;starting=true;$('go').disabled=$('days').disabled=true;try{let r=await fetch('/whaleback03/start?days='+$('days').value,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('이미 실행 중 또는 설정 오류');loaded=null;$('out').hidden=true}catch(e){$('phase').textContent=e.message;$('go').disabled=$('days').disabled=false}finally{starting=false;poll()}};poll();setInterval(poll,2000);
</script></html>"""

def handle(h,u,ctx,post=False):
    p=u.path
    if not p.startswith("/whaleback03"):return False
    q=parse_qs(u.query)
    if post and p=="/whaleback03/start":
        try: days=int(q.get("days",["90"])[0])
        except ValueError: h.js({"error":"invalid days"},400);return True
        h.js({"started":start(days,ctx)})
    elif not post and p=="/whaleback03":h.send(HTML,"text/html; charset=utf-8")
    elif not post and p=="/whaleback03/status":
        with LOCK:
            data={k:v for k,v in STATE.items() if k not in ("summary","features","signals","coverage","errors")}
            data["error_preview"]=STATE["errors"][:10]
        h.js(data)
    elif not post and p=="/whaleback03/results":
        with LOCK:data=dict(scan_id=STATE.get("scan_id"),summary=STATE["summary"])
        h.js(data)
    elif not post and p=="/whaleback03/csv":
        kind=q.get("kind",["summary"])[0]
        if kind not in ("summary","features","signals","coverage","errors"):h.js({"error":"invalid kind"},400);return True
        with LOCK:
            if STATE["status"]!="done" or q.get("scan_id",[""])[0]!=STATE.get("scan_id"):
                data=None;days=0
            else:data=list(STATE[kind]);days=STATE["days"]
        if data is None:h.js({"error":"scan again"},409);return True
        fields=list(dict.fromkeys(k for r in data for k in r))
        if not fields:fields=["market","error"] if kind=="errors" else ["market"]
        buf=io.StringIO();w=csv.DictWriter(buf,fieldnames=fields);w.writeheader();w.writerows(data)
        h.send(("\ufeff"+buf.getvalue()).encode("utf-8"),"text/csv; charset=utf-8",filename="BITHUMB_WHALE_RESEARCH_V0_3_"+str(days)+"D_"+kind.upper()+"_"+ctx["now"]().astimezone(ctx["KST"]).strftime("%Y%m%d%H%M")+".csv")
    else:h.js({"error":"not found"},404)
    return True
