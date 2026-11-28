"""WHALE DETECTOR V1.6: turnover sequence research after volatility/downturn watch."""
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
# Each feature uses completed candles strictly before the reference time.
def sequence(by,t):
    recent=[by[k] for k in range(t-18*STEP,t,STEP)]
    # Earliest maximum on ties; not necessarily the candle with most aggressive orders.
    i=max(range(18),key=lambda j:recent[j][6])
    peak=recent[i];o,h,l,cl,q=peak[1],peak[2],peak[3],peak[4],peak[6]
    body=pct(cl,o);loc=(cl-l)/(h-l) if h>l else None
    wick=(min(o,cl)-l)/(h-l) if h>l else None
    defense=(loc>=.65 and wick>=.35) if loc is not None else None
    before=[by[k] for k in range(peak[0]-3*STEP,peak[0],STEP)]
    after=recent[i+1:i+4]
    qb=statistics.fmean(x[6] for x in before)
    qa=statistics.fmean(x[6] for x in after) if len(after)==3 else None
    quiet=qa/qb if qa is not None and qb>0 else None
    held=(min(x[3] for x in after)>=l*.995) if len(after)==3 else None
    # Renewed flow before watch, strictly after the three contraction bars.
    renewed=None;renewed_at=None
    if quiet is not None and quiet<=.5 and len(recent[i+4:])>=1:
        renewed=False
        for j in range(i+4,18):
            x=recent[j];prior=statistics.fmean(y[6] for y in recent[j-3:j])
            if prior>0 and x[6]>=1.5*prior and x[4]>x[1] and x[4]>recent[j-1][4]:
                renewed=True;renewed_at=x[0]+STEP;break
    return dict(peak_bar_ts=peak[0],peak_bar_age_hours=(t-peak[0]-STEP)//3600,peak_bar_body_pct=body,
        peak_bar_turnover=q,peak_bar_close_location=loc,peak_bar_lower_wick=wick,
        peak_down=body<=-2,peak_up=body>=2,peak_defense=defense,peak_early=i<9,
        peak_share_pct=100*q/sum(x[6] for x in recent) if sum(x[6] for x in recent)>0 else None,
        after_before_turnover_ratio=quiet,post_peak_quiet=quiet<=.5 if quiet is not None else None,
        post_peak_low_held=held,prior_renewal=renewed,prior_renewal_ts=renewed_at)
def first_reflow(by,t,p):
    # No future OHLC used to decide a completed candle signal; next open is execution proxy.
    for j in range(1,WAIT+1):
        e=t+j*STEP;b=by[e-STEP];a=by[e-2*STEP];entry=by[e][1]
        q=statistics.fmean(by[k][6] for k in range(e-7*STEP,e-STEP,STEP))
        if q<=0 or b[6]<1.5*q or b[4]<=b[1] or b[4]<=a[4]:continue
        if pct(b[4],p)>8 or pct(entry,p)>8 or pct(entry,b[4])>3:continue
        return e
    return None
def evaluate(m,rows,start,stop,ctx):
    by={r[0]:r for r in rows};good={k for k,r in by.items() if ctx["r131valid"](r)};iso=ctx["iso"]
    cov=dict(record_type="coverage",market=m,received_bars=len(by),valid_windows=0,missing_history=0,missing_future=0,watch_raw=0,watch_episodes=0)
    details=[];next_watch=0
    for t in range(start,stop,STEP):
        if any(k not in good for k in range(t-43*STEP,t,STEP)):cov["missing_history"]+=1;continue
        if any(k not in good for k in range(t,t+(WAIT+HORIZON)*STEP,STEP)):cov["missing_future"]+=1;continue
        cov["valid_windows"]+=1;f=features(by,t)
        if not f["watch"]:continue
        cov["watch_raw"]+=1
        if t<next_watch:continue
        next_watch=t+(WAIT+HORIZON)*STEP;cov["watch_episodes"]+=1
        p=by[t][1];original=outcome(by,t,HORIZON,p);seq=sequence(by,t)
        common=dict(record_type="detail",market=m,watch_ts=t,watch_time=iso(t),watch_price=p,**f,**seq)
        details.append(dict(common,method="WATCH",entry_ts=t,entry_time=iso(t),entry_price=p,delay_hours=0,**original))
        if f["extra_count"]!=4:continue
        e=first_reflow(by,t,p)
        if e is None:continue
        entry=by[e][1];res=outcome(by,e,HORIZON,entry);fixed=outcome(by,e,HORIZON-(e-t)//STEP,entry)
        row=dict(common,method="REFLOW",entry_ts=e,entry_time=iso(e),entry_price=entry,delay_hours=(e-t)//3600,entry_change_pct=pct(entry,p),**res)
        for k in TARGETS:row["same_watch_end_hit"+str(k)]=fixed["hit"+str(k)]
        row["same_watch_end_peak_pct"]=fixed["peak_pct"];details.append(row)
    cov["status"]="ok" if cov["valid_windows"] else "missing"
    return details,cov
PATTERNS={
    "ALL":("기본 WATCH",lambda r:True),
    "FOUR":("기존 4/4",lambda r:r["extra_count"]==4),
    "HALF":("4/4 + 거래량 0.5배 이하",lambda r:r["extra_count"]==4 and r["volume3_vs_prev3"]<=.5),
    "DOWN":("최대 거래대금 봉 -2% 이하",lambda r:r["peak_down"]),
    "UP":("최대 거래대금 봉 +2% 이상",lambda r:r["peak_up"]),
    "DEF":("최대 거래대금 봉 아랫꼬리 방어",lambda r:r["peak_defense"]),
    "EARLY":("최대 거래대금 봉 전반 36시간",lambda r:r["peak_early"]),
    "QUIET":("집중 후 3봉 거래대금 절반 이하",lambda r:r["post_peak_quiet"]),
    "HELD":("집중 후 3봉 저점 방어",lambda r:r["post_peak_low_held"]),
    "DOWNQUIET":("하락 집중 → 거래대금 감소",lambda r:None if r["post_peak_quiet"] is None else r["peak_down"] and r["post_peak_quiet"]),
    "DEFQUIET":("방어 집중 → 거래대금 감소",lambda r:None if r["post_peak_quiet"] is None or r["peak_defense"] is None else r["peak_defense"] and r["post_peak_quiet"]),
    "QUIETHELD":("집중 → 감소 + 저점 방어",lambda r:None if r["post_peak_quiet"] is None or r["post_peak_low_held"] is None else r["post_peak_quiet"] and r["post_peak_low_held"]),
    "RENEW":("집중 → 감소 → 사전 재유입",lambda r:r["prior_renewal"])
}
def summarize(rows,cov):
    watches=[r for r in rows if r["method"]=="WATCH"]
    lookup={(r["market"],r["watch_ts"]):r for r in watches}
    reflows=[r for r in rows if r["method"]=="REFLOW"]
    out=[]
    def stat(scope,method,label,rs,denom,unknown=0,paired=None):
        n=len(rs);paired=rs if paired is None else paired
        z=dict(record_type="summary",scope=scope,method=method,label=label,signals=n,watch_episodes=denom,unknown=unknown,
            markets=len({r["market"] for r in rs}),median_peak=statistics.median(r["peak_pct"] for r in rs) if n else None,
            median_low=statistics.median(r["low_pct"] for r in rs) if n else None,
            median_delay_hours=statistics.median(r["delay_hours"] for r in rs) if n else None,
            loss20=sum(r["low_pct"]<=-20 for r in rs),loss20_pct=100*sum(r["low_pct"]<=-20 for r in rs)/n if n else None,
            extreme_peak_count=sum(r["peak_pct"]>300 for r in rs))
        for k in TARGETS:
            hits=[r for r in rs if r["hit"+str(k)]];h=len(hits)
            z.update({"hit"+str(k):h,"hit"+str(k)+"_pct":100*h/n if n else None,
                "hit"+str(k)+"_per_watch_pct":100*h/denom if denom else None,
                "paired_watch_hit"+str(k):sum(r["hit"+str(k)] for r in paired),
                "same_watch_end_hit"+str(k):sum(r.get("same_watch_end_hit"+str(k),r["hit"+str(k)]) for r in rs),
                "median_hours_to"+str(k):statistics.median(r["hit"+str(k)+"_hours_upper"] for r in hits) if hits else None,
                "median_low_to"+str(k):statistics.median(r["low_to_hit"+str(k)] for r in hits) if hits else None})
        out.append(z)
    for scope,base in (("all",watches),("four",[r for r in watches if r["extra_count"]==4]),("half",[r for r in watches if r["extra_count"]==4 and r["volume3_vs_prev3"]<=.5])):
        for key,(label,fn) in PATTERNS.items():
            values=[(r,fn(r)) for r in base]
            stat(scope,key,label,[r for r,v in values if v is True],len(base),sum(v is None for r,v in values))
        if scope!="all":
            keys={(r["market"],r["watch_ts"]) for r in base}
            rs=[r for r in reflows if (r["market"],r["watch_ts"]) in keys]
            stat(scope,"REFLOW","WATCH 후 48h 내 거래대금 재유입",rs,len(base),0,[lookup[(r["market"],r["watch_ts"])] for r in rs])
    return out
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
        patch(phase="거래대금 집중·감소·재유입 순서 검증",total=len(universe),universe=len(universe),total_universe=total,mode=mode,days=days,signal_start=iso(start),signal_end_exclusive=iso(stop),data_end=iso(cut))
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
        name="WHALE_DETECTOR_V1_6_"+str(days)+"D_"+mode.upper()+"_"+ctx["now"]().astimezone(ctx["KST"]).strftime("%Y%m%d%H%M")+".csv"
        patch(status="done",phase="완료",summary=summary,details=rows,coverage=cov,errors=errors,csv_bytes=("\ufeff"+b.getvalue()).encode("utf-8"),csv_name=name,
              valid_markets=sum(c.get("valid_windows",0)>0 for c in cov),error_count=len(errors),elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:patch(status="error",phase="실패",errors=[dict(error=str(e))],error_count=1)
def start(ctx,mode,days):
    with LOCK:
        if STATE["status"]=="running":return False
        STATE.clear();STATE.update(status="running",phase="준비",done=0,total=0,scan_id=str(time.time_ns()),summary=[],details=[],coverage=[],errors=[],error_count=0)
    threading.Thread(target=run,args=(ctx,mode,days),daemon=True).start();return True
HTML=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WHALE DETECTOR V1.6</title><style>
*{box-sizing:border-box}[hidden]{display:none!important}body{font:14px/1.55 system-ui;max-width:820px;margin:auto;padding:14px;background:#f5f8f7;color:#172a23}section{background:white;border:1px solid #d7e3dd;border-radius:12px;padding:14px;margin:12px 0}h1{font-size:23px}button,a,select{display:inline-block;padding:10px;margin:3px;border-radius:8px;border:1px solid #a5bbb0;background:white;color:#176345;text-decoration:none;font:inherit}button{background:#176345;color:white;width:97%}button:disabled{opacity:.5}progress{width:100%}small{display:block;color:#53645d}.scroll{overflow:auto}table{border-collapse:collapse;white-space:nowrap;font-size:12px;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:right}td:first-child,th:first-child{text-align:left}pre{white-space:pre-wrap}</style>
<h1>WHALE DETECTOR V1.6</h1><small>거래대금 집중 → 감소 → 재유입 · 매매 주문 없음</small><a href="/whalereverse15">V1.5 반등 검증</a><a href="/whaledetector">기존 실시간</a><section><label>표본 <select id="mode"><option value="new80">80종목 표본</option><option value="previous80">이전 연구 표본</option><option value="all">전체 KRW</option></select></label><label>기간 <select id="days"><option value="30">30일</option><option value="90">90일</option></select></label><small>V1.5 기본 WATCH와 4/4 조건, 9일 중복 제거 유지. 최근 3일 중 거래대금 최대 4시간봉의 하락(-2% 이하), 상승(+2% 이상), 아랫꼬리 방어(꼬리 35% 이상·종가 위치 65% 이상) 및 발생 시점 비교. 집중 봉 직전 3봉 대비 이후 3봉 평균 거래대금 절반 이하를 감소로 판단. 이후 3봉이 완료되지 않았으면 미확인. 저점 방어는 이후 3봉 저점이 집중 봉 저점의 99.5% 이상. 사전 재유입은 감소 3봉 이후 WATCH 이전에 양봉·종가 상승·직전 3봉 대비 거래대금 1.5배. 별도로 WATCH 이후 48시간 내 양봉·종가 상승·직전 6봉 대비 1.5배 재유입을 비교하며 추격 제한 8% 및 시가 갭 3% 유지. 이후 7일 최고/최저 가격, 수수료 제외. 실제 매수·매도 체결 방향 및 고래 신원 미확인. 현재 상장 종목 표본.</small><button id="go">거래대금 순서 검증 시작</button><p id="phase">대기</p><progress id="bar" max="1" value="0"></progress><p id="audit"></p><pre id="err"></pre></section>
<section id="out" hidden><h2>거래대금 순서별 급등 도달률</h2><label>후보 조건 <select id="scope"><option value="four">기존 4/4</option><option value="half">4/4 + 거래량 0.5배 이하</option><option value="all">기본 WATCH</option></select></label><label>상승 목표 <select id="target"><option value="30">+30%</option><option value="50">+50%</option><option value="100">+100%</option></select></label><div class="scroll"><table id="tbl"></table></div><small>도달률은 해당 조건 충족 사례 중 비율. 미확인은 조건 미충족과 구분. 조건별 중복 포함. 재유입은 다음 봉 시가 기준이며 짝지은 WATCH와 동일 종료 시점 결과도 비교. 저점은 도달 봉 포함, 봉 내부 순서 미확인. 사후 탐색 결과이며 매매 승률이나 실제 매집 입증 아님.</small><h2>4/4 사례 (최고 상승 상위 20개)</h2><div class="scroll"><table id="events"></table></div><p id="links"></p></section>
<script>
const $=id=>document.getElementById(id);let data=[],events=[],loaded=null,busy=false,starting=false;const cacheKey='whale_reverse16_summary';
const f=x=>typeof x==='number'?x.toFixed(2):'—';
function table(t,heads,rows){t.replaceChildren();let h=t.createTHead().insertRow();for(let x of heads){let th=document.createElement('th');th.textContent=x;h.append(th)}let b=t.createTBody();for(let row of rows){let tr=b.insertRow();for(let v of row)tr.insertCell().textContent=v}}
function render(){let k=$('target').value;table($('tbl'),['조건','사례','미확인','도달','도달률','전체 후보 대비','-20% 하락','동일 종료 도달','짝 WATCH 도달'],data.filter(r=>r.scope===$('scope').value).map(r=>[r.label,r.signals,r.unknown,r['hit'+k],f(r['hit'+k+'_pct'])+'%',f(r['hit'+k+'_per_watch_pct'])+'%',f(r.loss20_pct)+'%',r['same_watch_end_hit'+k],r['paired_watch_hit'+k]]));table($('events'),['종목','시각','최대 거래대금 봉 변화%','집중 후 거래대금 배수','7일 최고%','7일 최저%'],events.map(r=>[r.market,r.entry_time,f(r.peak_bar_body_pct),f(r.after_before_turnover_ratio),f(r.peak_pct),f(r.low_pct)]))}
function download(blob,name){let a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;document.body.append(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},30000)}
function links(){let a=document.createElement('a');a.href='#';a.textContent='전체 결과 CSV';a.onclick=async e=>{e.preventDefault();try{let r=await fetch('/whalereverse16/csv?scan_id='+encodeURIComponent(loaded));if(!r.ok)throw Error('HTTP '+r.status);let n=(r.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/);download(await r.blob(),n?n[1]:'WHALE_DETECTOR_V1_6_FULL.csv')}catch(e){$('err').textContent='전체 CSV 실패: '+e.message+' · 요약 CSV 저장 가능'}};let b=document.createElement('a');b.href='#';b.textContent='요약 CSV (비상 저장)';b.onclick=e=>{e.preventDefault();let k=[...new Set(data.flatMap(Object.keys))],esc=x=>'"'+String(x??'').replaceAll('"','""')+'"';download(new Blob(['\ufeff'+[k.map(esc).join(','),...data.map(r=>k.map(x=>esc(r[x])).join(','))].join('\r\n')],{type:'text/csv;charset=utf-8'}),'WHALE_DETECTOR_V1_6_SUMMARY.csv')};$('links').replaceChildren(a,b)}
$('scope').onchange=render;$('target').onchange=render;
async function poll(){if(busy||starting)return;busy=true;try{let r=await fetch('/whalereverse16/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);let s=await r.json();$('go').disabled=$('mode').disabled=$('days').disabled=s.status==='running';$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('bar').max=Math.max(1,s.total);$('bar').value=s.done;$('audit').textContent=(s.signal_start?s.signal_start+' ~ '+s.signal_end_exclusive+' (미포함) · ':'')+'오류 '+(s.error_count||0)+(s.status==='done'?' · 유효 '+s.valid_markets+'종목 · '+s.elapsed_seconds+'초':'');$('err').textContent=(s.error_preview||[]).map(e=>(e.market||'')+' '+e.error).join('\n');if(s.status==='running'){$('out').hidden=true;loaded=null}if(s.status==='done'&&loaded!==s.scan_id){let x=await(await fetch('/whalereverse16/results',{cache:'no-store'})).json();if(x.scan_id!==s.scan_id)return;data=x.summary;events=x.events;loaded=s.scan_id;try{localStorage.setItem(cacheKey,JSON.stringify({data,events,loaded}))}catch(e){}render();links();$('out').hidden=false}}catch(e){$('phase').textContent='연결 오류: '+e.message;$('go').disabled=false}finally{busy=false}}
$('go').onclick=async()=>{if(busy||starting)return;starting=true;$('go').disabled=true;try{let r=await fetch('/whalereverse16/start?mode='+$('mode').value+'&days='+$('days').value,{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('이미 실행 중');loaded=null;$('out').hidden=true}catch(e){$('phase').textContent=e.message;$('go').disabled=false}finally{starting=false;poll()}};try{let x=JSON.parse(localStorage.getItem(cacheKey)||'null');if(x&&Array.isArray(x.data)&&x.data.length){data=x.data;events=x.events||[];loaded=x.loaded;render();links();$('out').hidden=false}}catch(e){}poll();setInterval(poll,2000);
</script></html>"""
def handle(h,u,ctx,post=False):
    p=u.path
    if not(p=="/whalereverse16" or p.startswith("/whalereverse16/")):return False
    q=parse_qs(u.query)
    if post and p=="/whalereverse16/start":
        try:
            mode=q.get("mode",["new80"])[0];days=int(q.get("days",["30"])[0])
            if mode not in ("new80","previous80","all") or days not in (30,90):raise ValueError()
        except ValueError:h.js({"error":"invalid settings"},400);return True
        h.js({"started":start(ctx,mode,days)})
    elif not post and p=="/whalereverse16":h.send(HTML,"text/html; charset=utf-8")
    elif not post and p=="/whalereverse16/status":
        with LOCK:
            d={k:v for k,v in STATE.items() if k not in ("summary","details","coverage","errors","csv_bytes")};d["error_preview"]=STATE["errors"][:10]
        h.js(d)
    elif not post and p=="/whalereverse16/results":
        with LOCK:d=dict(scan_id=STATE.get("scan_id"),summary=STATE["summary"],events=sorted((r for r in STATE["details"] if r["method"]=="WATCH" and r["extra_count"]==4),key=lambda r:-r["peak_pct"])[:20])
        h.js(d)
    elif not post and p=="/whalereverse16/csv":
        with LOCK:
            ok=STATE["status"]=="done" and q.get("scan_id",[""])[0]==STATE.get("scan_id");payload=STATE.get("csv_bytes") if ok else None;name=STATE.get("csv_name")
        if payload is None:h.js({"error":"scan expired; use summary CSV"},409);return True
        h.send(payload,"text/csv; charset=utf-8",filename=name)
    else:h.js({"error":"not found"},404)
    return True
