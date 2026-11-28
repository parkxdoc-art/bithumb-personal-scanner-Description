"""WHALE DETECTOR V1.5: causal first-rebound replay after volatility/downturn watch."""
import csv,io,math,statistics,threading,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from urllib.parse import parse_qs
from whale_reverse13 import DISCOVERY
SECOND=set("KRW-1INCH,KRW-ACE,KRW-AEON,KRW-AIA,KRW-ALLO,KRW-APE,KRW-ARK,KRW-AT,KRW-AVAIL,KRW-AXS,KRW-BASED,KRW-BERA,KRW-BIOT,KRW-BNB,KRW-BOUNTY,KRW-BTC,KRW-CAKE,KRW-CC,KRW-CHIP,KRW-CORE,KRW-CRO,KRW-CTC,KRW-CYS,KRW-DEXE,KRW-DOT,KRW-EDU,KRW-ENA,KRW-ESP,KRW-FCT2,KRW-FLR,KRW-GAME2,KRW-GMX,KRW-GRT,KRW-HEMI,KRW-HP,KRW-ICNT,KRW-INIT,KRW-IQ,KRW-KAIA,KRW-KITE,KRW-LAYER,KRW-LIT,KRW-MAGMA,KRW-MAV,KRW-MEGA,KRW-MEV,KRW-MNT,KRW-MOVE,KRW-NEAR,KRW-NIL,KRW-OGN,KRW-OPEN,KRW-PARTI,KRW-PEPE,KRW-POL,KRW-PRL,KRW-PUMP,KRW-Q,KRW-RE,KRW-REZ,KRW-RSR,KRW-SAND,KRW-SENT,KRW-SKL,KRW-SOLV,KRW-SPX,KRW-STX,KRW-SWELL,KRW-TAVA,KRW-TIA,KRW-TREE,KRW-UMA,KRW-VANA,KRW-W,KRW-WET,KRW-XAI,KRW-XLM,KRW-XTZ,KRW-ZAMA,KRW-ZKP".split(","))
STEP=14400;DAY=86400;WAIT=12;HORIZON=42
TARGETS=(30,50,100)
LABELS={"BASE":"전체 기준군","WATCH":"저점 후보 (즉시 기준)","A":"A 직전 고가 돌파","B":"B 2봉 상승·저점 상승","C":"C 고가 돌파 + 거래대금 1.5배"}
LOCK=threading.Lock()
STATE=dict(status="idle",phase="대기",done=0,total=0,summary=[],details=[],coverage=[],errors=[])
def patch(**kw):
    with LOCK:STATE.update(kw)
def pct(a,b):return 100*(a/b-1)
def features(by,t):
    seq=[by[k] for k in range(t-43*STEP,t,STEP)]
    recent=seq[-18:];prev=seq[-36:-18];week=seq[-42:]
    lo=min(r[3] for r in recent);hi=max(r[2] for r in recent);close=recent[-1][4]
    span=pct(hi,lo);change=pct(close,seq[-19][4])
    span7=pct(max(r[2] for r in week),min(r[3] for r in week))
    v0=sum(r[5] for r in prev);v=sum(r[5] for r in recent);q=sum(r[6] for r in recent)
    volume=v/v0 if v0>0 else None
    loc=100*(close-lo)/(hi-lo) if hi>lo else None
    conc=100*sum(sorted((r[6] for r in recent),reverse=True)[:3])/q if q>0 else None
    extras=(span7>=25,volume<1 if volume is not None else None,loc<=25 if loc is not None else None,conc>=40 if conc is not None else None)
    return dict(span3=span,change3=change,span7=span7,volume3_vs_prev3=volume,close_location3=loc,concentration3=conc,
                extra_count=sum(x is True for x in extras),extra_known=sum(x is not None for x in extras),watch=span>=12 and change<0)
def outcome(by,t,n,p):
    rs=[by[k] for k in range(t,t+n*STEP,STEP)]
    peak=pct(max(r[2] for r in rs),p);low=pct(min(r[3] for r in rs),p)
    out=dict(peak_pct=peak,low_pct=min(0.,low))
    for k in TARGETS:
        hit=next((i for i,r in enumerate(rs,1) if pct(r[2],p)>=k-1e-9),None)
        out["hit"+str(k)]=hit is not None
        out["hit"+str(k)+"_hours_upper"]=hit*4 if hit else None
        out["low_to_hit"+str(k)]=min(0.,pct(min(r[3] for r in rs[:hit]),p)) if hit else None
    return out
def triggers(by,t,watch_price):
    found={}
    for j in range(1,WAIT+1):
        e=t+j*STEP;b=by[e-STEP];a=by[e-2*STEP];prev=by[e-3*STEP];entry=by[e][1]
        # Signal is known only when b completes. The next open proxies an executable price.
        if pct(b[4],watch_price)>8 or pct(entry,watch_price)>8 or pct(entry,b[4])>3:continue
        a_test=b[4]>a[2] and b[4]>b[1]
        b_test=b[4]>a[4]>prev[4] and b[3]>a[3]
        q=statistics.fmean(by[k][6] for k in range(e-7*STEP,e-STEP,STEP))
        c_test=a_test and q>0 and b[6]>=1.5*q
        for key,ok in (("A",a_test),("B",b_test),("C",c_test)):
            if ok and key not in found:found[key]=e
    return found
def evaluate(m,rows,start,stop,ctx):
    by={r[0]:r for r in rows};good={k for k,r in by.items() if ctx["r131valid"](r)};iso=ctx["iso"]
    cov=dict(record_type="coverage",market=m,received_bars=len(by),valid_windows=0,missing_history=0,missing_future=0,watch_raw=0,watch_episodes=0,baseline_episodes=0)
    details=[];next_base=next_watch=0;raw=dict(raw=0,pump30=0,pump50=0,pump100=0,watch30=0,watch50=0,watch100=0)
    for t in range(start,stop,STEP):
        if any(k not in good for k in range(t-43*STEP,t,STEP)):cov["missing_history"]+=1;continue
        if any(k not in good for k in range(t,t+(WAIT+HORIZON)*STEP,STEP)):cov["missing_future"]+=1;continue
        cov["valid_windows"]+=1;p=by[t][1];f=features(by,t);raw["raw"]+=1
        future_high=max(by[k][2] for k in range(t,t+HORIZON*STEP,STEP));peak=pct(future_high,p)
        for target in TARGETS:
            if peak>=target-1e-9:
                raw["pump"+str(target)]+=1
                if f["watch"]:raw["watch"+str(target)]+=1
        if f["watch"]:cov["watch_raw"]+=1
        base=t>=next_base;watch=f["watch"] and t>=next_watch
        if not(base or watch):continue
        # Every group uses the same 9-day cool-down, covering the full 48h + 7d outcome.
        original=outcome(by,t,HORIZON,p)
        if base:
            next_base=t+(WAIT+HORIZON)*STEP;cov["baseline_episodes"]+=1
            details.append(dict(record_type="detail",market=m,method="BASE",watch_ts=t,watch_time=iso(t),entry_ts=t,entry_time=iso(t),entry_price=p,delay_hours=0,**f,**original))
        if not watch:continue
        next_watch=t+(WAIT+HORIZON)*STEP;cov["watch_episodes"]+=1
        found=triggers(by,t,p)
        common=dict(record_type="detail",market=m,watch_ts=t,watch_time=iso(t),watch_price=p,watch_peak_pct=original["peak_pct"],watch_low_pct=original["low_pct"],**f)
        details.append(dict(common,method="WATCH",entry_ts=t,entry_time=iso(t),entry_price=p,delay_hours=0,**original))
        for method,e in found.items():
            entry=by[e][1];res=outcome(by,e,HORIZON,entry)
            fixed=outcome(by,e,HORIZON-(e-t)//STEP,entry)
            row=dict(common,method=method,entry_ts=e,entry_time=iso(e),entry_price=entry,delay_hours=(e-t)//3600,
                     entry_change_pct=pct(entry,p),**res)
            for k in TARGETS:row["same_watch_end_hit"+str(k)]=fixed["hit"+str(k)]
            row["same_watch_end_peak_pct"]=fixed["peak_pct"]
            details.append(row)
    cov.update(raw,status="ok" if cov["valid_windows"] else "missing")
    return details,cov
def summarize(rows,cov):
    out=[];watch=[r for r in rows if r["method"]=="WATCH"];watch_by={(r["market"],r["watch_ts"]):r for r in watch}
    for scope,minimum in (("all",0),("extra2",2)):
        for method,label in LABELS.items():
            if method=="BASE" and minimum:continue
            eligible=[r for r in watch if r["extra_count"]>=minimum]
            rs=[r for r in rows if r["method"]==method and r["extra_count"]>=minimum]
            n=len(rs);denom=len(eligible) if method!="BASE" else n
            paired=[watch_by[(r["market"],r["watch_ts"])] for r in rs] if method in ("A","B","C") else rs
            outrow=dict(record_type="summary",scope=scope,method=method,label=label,signals=n,watch_episodes=denom,
                no_trigger=denom-n,trigger_pct=100*n/denom if denom else None,markets=len({r["market"] for r in rs}),
                median_peak=statistics.median(r["peak_pct"] for r in rs) if n else None,median_low=statistics.median(r["low_pct"] for r in rs) if n else None,
                median_delay_hours=statistics.median(r["delay_hours"] for r in rs) if n else None,
                extreme_peak_count=sum(r["peak_pct"]>300 for r in rs))
            for k in TARGETS:
                hits=[r for r in rs if r["hit"+str(k)]];h=len(hits);ph=sum(r["hit"+str(k)] for r in paired)
                same=sum(r.get("same_watch_end_hit"+str(k),r["hit"+str(k)]) for r in rs)
                outrow.update({"hit"+str(k):h,"hit"+str(k)+"_pct":100*h/n if n else None,
                    "hit"+str(k)+"_per_watch_pct":100*h/denom if denom else None,
                    "paired_watch_hit"+str(k):ph,"same_watch_end_hit"+str(k):same,
                    "median_hours_to"+str(k):statistics.median(r["hit"+str(k)+"_hours_upper"] for r in hits) if hits else None,
                    "median_low_to"+str(k):statistics.median(r["low_to_hit"+str(k)] for r in hits) if hits else None})
            out.append(outrow)
    r=dict(record_type="reverse_summary",scope="raw_4h_windows",windows=sum(c.get("raw",0) for c in cov))
    for k in TARGETS:
        n=sum(c.get("pump"+str(k),0) for c in cov);h=sum(c.get("watch"+str(k),0) for c in cov)
        r.update({"pump"+str(k):n,"watch"+str(k):h,"capture"+str(k)+"_pct":100*h/n if n else None})
    return out+[r]
def run(ctx,mode,days):
    begin=time.monotonic()
    try:
        candles=ctx["candles"];iso=ctx["iso"];end=int(ctx["now"]().timestamp())
        probe=candles("KRW-BTC",240,end,8,latest=True)
        if len(probe)<2:raise RuntimeError("BTC 봉 경계 확인 실패")
        anchor=probe[-1][0]%STEP;cut=(end-anchor)//STEP*STEP+anchor
        if not any(r[0]==cut-STEP and ctx["r131valid"](r) for r in probe):raise RuntimeError("완료 봉 확인 실패")
        stop=cut-(WAIT+HORIZON-1)*STEP;start=stop-days*DAY
        universe=sorted({r["market"] for r in ctx["api"]("market/all") if r["market"].startswith("KRW-") and r["market"][4:] not in ctx["STABLE"]})
        total=len(universe)
        if mode=="new80":
            universe=[m for m in universe if m not in DISCOVERY and m not in SECOND]
            if len(universe)>80:universe=[universe[i*len(universe)//80] for i in range(80)]
        elif mode=="previous80":universe=[m for m in universe if m in SECOND]
        if not universe:raise RuntimeError("시장 목록 없음")
        pages=math.ceil((days*6+43+WAIT+HORIZON-1)/200)
        patch(phase="저점 후보 및 최초 반등 검증",total=len(universe),universe=len(universe),total_universe=total,mode=mode,days=days,signal_start=iso(start),signal_end_exclusive=iso(stop),data_end=iso(cut))
        rows=[];cov=[];errors=[]
        def fetch(m):return evaluate(m,candles(m,240,cut,200,pages=pages,latest=False),start,stop,ctx)
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs={pool.submit(fetch,m):m for m in universe}
            for f in as_completed(jobs):
                m=jobs[f]
                try:a,c=f.result();rows.extend(a);cov.append(c)
                except Exception as e:errors.append(dict(record_type="error",market=m,error=str(e)));cov.append(dict(record_type="coverage",market=m,status="error"))
                with LOCK:STATE["done"]+=1;STATE["error_count"]=len(errors)
        summary=summarize(rows,cov)
        for r in summary:r.update(mode=mode,days=days,universe=len(universe),total_universe=total,signal_start=iso(start),signal_end_exclusive=iso(stop),data_end=iso(cut),gross_price_return=True)
        rows.sort(key=lambda r:(r["watch_ts"],r["market"],r["method"]))
        allrows=summary+rows+cov+errors;fields=list(dict.fromkeys(k for r in allrows for k in r))
        b=io.StringIO();w=csv.DictWriter(b,fieldnames=fields);w.writeheader();w.writerows(allrows)
        name="WHALE_DETECTOR_V1_5_"+str(days)+"D_"+mode.upper()+"_"+ctx["now"]().astimezone(ctx["KST"]).strftime("%Y%m%d%H%M")+".csv"
        patch(status="done",phase="완료",summary=summary,details=rows,coverage=cov,errors=errors,csv_bytes=("\ufeff"+b.getvalue()).encode("utf-8"),csv_name=name,
              valid_markets=sum(c.get("valid_windows",0)>0 for c in cov),error_count=len(errors),elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:patch(status="error",phase="실패",errors=[dict(error=str(e))],error_count=1)
def start(ctx,mode,days):
    with LOCK:
        if STATE["status"]=="running":return False
        STATE.clear();STATE.update(status="running",phase="준비",done=0,total=0,scan_id=str(time.time_ns()),summary=[],details=[],coverage=[],errors=[],error_count=0)
    threading.Thread(target=run,args=(ctx,mode,days),daemon=True).start();return True
HTML=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WHALE DETECTOR V1.5</title><style>
*{box-sizing:border-box}[hidden]{display:none!important}body{font:14px/1.55 system-ui;max-width:820px;margin:auto;padding:14px;background:#f5f8f7;color:#172a23}section{background:white;border:1px solid #d7e3dd;border-radius:12px;padding:14px;margin:12px 0}h1{font-size:23px}button,a,select{display:inline-block;padding:10px;margin:3px;border-radius:8px;border:1px solid #a5bbb0;background:white;color:#176345;text-decoration:none;font:inherit}button{background:#176345;color:white;width:97%}button:disabled{opacity:.5}progress{width:100%}small{display:block;color:#53645d}.scroll{overflow:auto}table{border-collapse:collapse;white-space:nowrap;font-size:12px;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:right}td:first-child,th:first-child{text-align:left}pre{white-space:pre-wrap}</style>
<h1>WHALE DETECTOR V1.5</h1><small>저점 후보 → 최초 반등 검증 · 매매 주문 없음</small><a href="/whalereverse14">V1.4 역추적</a><a href="/whaledetector">기존 실시간</a><section><label>표본 <select id="mode"><option value="new80">새로운 80종목</option><option value="previous80">V1.4 동일 80종목</option><option value="all">전체 KRW</option></select></label><label>기간 <select id="days"><option value="30">30일</option><option value="90">90일</option></select></label><small>WATCH: 완료 4시간봉 3일 변동폭 12% 이상 + 3일 하락. 추가 4조건: 7일 변동폭 25% 이상, 직전 동기간 대비 3일 거래량 감소, 3일 종가 위치 하위 25%, 상위 3봉 거래대금 40% 이상. 48시간 내 첫 A: 양봉 종가 직전 고가 돌파, B: 2봉 종가 상승 및 저점 상승, C: A + 직전 6봉 평균 대비 거래대금 1.5배. 확인 종가·다음 시가가 후보 기준보다 8% 넘게 상승하거나 다음 시가가 확인 종가보다 3% 넘게 갭 상승하면 제외. 진입은 다음 봉 시가 대용. 모든 그룹 9일 중복 제거. 이후 7일 최고/최저 가격 기준, 수수료 제외, 손익률 아님. 현재 상장 종목 및 연속 봉 검증 편향. 80종목은 V1.2 및 V1.4 표본 제외 후 균등 추출.</small><button id="go">최초 반등 검증 시작</button><p id="phase">대기</p><progress id="bar" max="1" value="0"></progress><p id="audit"></p><pre id="err"></pre></section>
<section id="out" hidden><h2>첫 반등 이후 상승 결과</h2><label>후보 조건 <select id="scope"><option value="all">기본 WATCH</option><option value="extra2">추가 2/4 이상</option></select></label><label>상승 목표 <select id="target"><option value="30">+30%</option><option value="50">+50%</option><option value="100">+100%</option></select></label><div class="scroll"><table id="tbl"></table></div><small>도달률은 실제 해당 방식이 발동한 건수 기준. WATCH 대비 도달률은 반등이 없던 후보도 분모에 포함. 짝지은 WATCH 도달 건수와 동일 종료 시점 도달 건수를 함께 비교. 도달 시각은 4시간봉 종료 상한, 저점은 도달 봉 포함. 조건은 탐색 가설이며 고래 매집 입증 아님.</small><h2>급등 역추적 보조 결과</h2><p id="reverse"></p><small>모든 유효 4시간 시점 중 7일 목표에 도달한 시점에서 WATCH가 성립한 비율. 중복 포함하며 고유 급등 사건 포착률 아님.</small><h2>반등 확인 사례 (상위 20개)</h2><div class="scroll"><table id="events"></table></div><p id="links"></p></section>
<script>
const $=id=>document.getElementById(id);let data=[],events=[],loaded=null,busy=false,starting=false;const cacheKey='whale_reverse15_summary';
const f=x=>typeof x==='number'?x.toFixed(2):'—';
function table(t,heads,rows){t.replaceChildren();let h=t.createTHead().insertRow();for(let x of heads){let th=document.createElement('th');th.textContent=x;h.append(th)}let b=t.createTBody();for(let row of rows){let tr=b.insertRow();for(let v of row)tr.insertCell().textContent=v}}
function render(){let k=$('target').value;table($('tbl'),['방식','신호','미발동','도달','도달률','WATCH 대비','짝 WATCH 도달','동일 종료 도달','중앙 지연 h','중앙 최저%'],data.filter(r=>r.record_type==='summary'&&r.scope===$('scope').value).map(r=>[r.label,r.signals,r.no_trigger,r['hit'+k],f(r['hit'+k+'_pct'])+'%',f(r['hit'+k+'_per_watch_pct'])+'%',r['paired_watch_hit'+k],r['same_watch_end_hit'+k],f(r.median_delay_hours),f(r.median_low)]));let x=data.find(r=>r.record_type==='reverse_summary');$('reverse').textContent=x?[30,50,100].map(k=>'+'+k+'%: '+x['watch'+k]+'/'+x['pump'+k]+' ('+f(x['capture'+k+'_pct'])+'%)').join(' · '):'—';table($('events'),['종목','방식','반등 기준 시각','기준가','후속 최고%','후속 최저%'],events.map(r=>[r.market,r.method,r.entry_time,r.entry_price,f(r.peak_pct),f(r.low_pct)]))}
function download(blob,name){let a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;document.body.append(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},30000)}
function links(){let a=document.createElement('a');a.href='#';a.textContent='전체 결과 CSV';a.onclick=async e=>{e.preventDefault();try{let r=await fetch('/whalereverse15/csv?scan_id='+encodeURIComponent(loaded));if(!r.ok)throw Error('HTTP '+r.status);let n=(r.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/);download(await r.blob(),n?n[1]:'WHALE_DETECTOR_V1_5_FULL.csv')}catch(e){$('err').textContent='전체 CSV 실패: '+e.message+' · 요약 CSV 저장 가능'}};let b=document.createElement('a');b.href='#';b.textContent='요약 CSV (비상 저장)';b.onclick=e=>{e.preventDefault();let k=[...new Set(data.flatMap(Object.keys))],esc=x=>'"'+String(x??'').replaceAll('"','""')+'"';download(new Blob(['\ufeff'+[k.map(esc).join(','),...data.map(r=>k.map(x=>esc(r[x])).join(','))].join('\r\n')],{type:'text/csv;charset=utf-8'}),'WHALE_DETECTOR_V1_5_SUMMARY.csv')};$('links').replaceChildren(a,b)}
$('scope').onchange=render;$('target').onchange=render;
async function poll(){if(busy||starting)return;busy=true;try{let r=await fetch('/whalereverse15/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);let s=await r.json();$('go').disabled=$('mode').disabled=$('days').disabled=s.status==='running';$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('bar').max=Math.max(1,s.total);$('bar').value=s.done;$('audit').textContent=(s.signal_start?s.signal_start+' ~ '+s.signal_end_exclusive+' (미포함) · ':'')+'오류 '+(s.error_count||0)+(s.status==='done'?' · 유효 '+s.valid_markets+'종목 · '+s.elapsed_seconds+'초':'');$('err').textContent=(s.error_preview||[]).map(e=>(e.market||'')+' '+e.error).join('\n');if(s.status==='running'){$('out').hidden=true;loaded=null}if(s.status==='done'&&loaded!==s.scan_id){let x=await(await fetch('/whalereverse15/results',{cache:'no-store'})).json();if(x.scan_id!==s.scan_id)return;data=x.summary;events=x.events;loaded=s.scan_id;try{localStorage.setItem(cacheKey,JSON.stringify({data,events,loaded}))}catch(e){}render();links();$('out').hidden=false}}catch(e){$('phase').textContent='연결 오류: '+e.message;$('go').disabled=false}finally{busy=false}}
$('go').onclick=async()=>{if(busy||starting)return;starting=true;$('go').disabled=true;try{let r=await fetch('/whalereverse15/start?mode='+$('mode').value+'&days='+$('days').value,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('이미 실행 중');loaded=null;$('out').hidden=true}catch(e){$('phase').textContent=e.message;$('go').disabled=false}finally{starting=false;poll()}};try{let x=JSON.parse(localStorage.getItem(cacheKey)||'null');if(x&&Array.isArray(x.data)&&x.data.length){data=x.data;events=x.events||[];loaded=x.loaded;render();links();$('out').hidden=false}}catch(e){}poll();setInterval(poll,2000);
</script></html>"""
def handle(h,u,ctx,post=False):
    p=u.path
    if not(p=="/whalereverse15" or p.startswith("/whalereverse15/")):return False
    q=parse_qs(u.query)
    if post and p=="/whalereverse15/start":
        try:
            mode=q.get("mode",["new80"])[0];days=int(q.get("days",["30"])[0])
            if mode not in ("new80","previous80","all") or days not in (30,90):raise ValueError()
        except ValueError:h.js({"error":"invalid settings"},400);return True
        h.js({"started":start(ctx,mode,days)})
    elif not post and p=="/whalereverse15":h.send(HTML,"text/html; charset=utf-8")
    elif not post and p=="/whalereverse15/status":
        with LOCK:
            d={k:v for k,v in STATE.items() if k not in ("summary","details","coverage","errors","csv_bytes")};d["error_preview"]=STATE["errors"][:10]
        h.js(d)
    elif not post and p=="/whalereverse15/results":
        with LOCK:d=dict(scan_id=STATE.get("scan_id"),summary=STATE["summary"],events=sorted((r for r in STATE["details"] if r["method"] in ("A","B","C")),key=lambda r:-r["peak_pct"])[:20])
        h.js(d)
    elif not post and p=="/whalereverse15/csv":
        with LOCK:
            ok=STATE["status"]=="done" and q.get("scan_id",[""])[0]==STATE.get("scan_id");payload=STATE.get("csv_bytes") if ok else None;name=STATE.get("csv_name")
        if payload is None:h.js({"error":"scan expired; use summary CSV"},409);return True
        h.send(payload,"text/csv; charset=utf-8",filename=name)
    else:h.js({"error":"not found"},404)
    return True
