"""WHALE DETECTOR V2.0 - live accumulation evidence only."""
import csv, io, json, math, re, statistics, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs

VERSION="WHALE DETECTOR V2.0"
STEP=14400
DAY=86400
LOCK=threading.Lock()
STATE=dict(status="idle",phase="대기",done=0,total=0,rows=[],errors=[],coverage=[])
def patch(**kw):
    with LOCK: STATE.update(kw)

from whale_reverse15 import features
from whale_reverse16 import sequence

KEYS=("range3_ok","down3_ok","range7_ok","half_volume_ok","low_location_ok","concentration_ok")
LABELS=("3일 변동폭 12% 이상","3일 가격 하락","7일 변동폭 25% 이상","3일 거래량 0.5배 이하","3일 종가 위치 하위 25%","거래대금 상위 3봉 집중도 40% 이상")
def candidate(rows,cut,ctx):
    by={r[0]:r for r in rows}
    if any(not ctx["r131valid"](by.get(t)) for t in range(cut-43*STEP,cut,STEP)):
        return None,"missing_history"
    f=features(by,cut)
    v=f["volume3_vs_prev3"];loc=f["close_location3"];conc=f["concentration3"]
    flags=(f["span3"]>=12,f["change3"]<0,f["span7"]>=25,v<=.5 if v is not None else None,loc<=25 if loc is not None else None,conc>=40 if conc is not None else None)
    z=dict(f,**dict(zip(KEYS,flags)),score=sum(x is True for x in flags),known=sum(x is not None for x in flags),reference_close=by[cut-STEP][4])
    if not all(x is True for x in flags):return z,"not_strong"
    z.update(sequence(by,cut))
    return z,"strong"
def quote(m,ctx):
    q=next(q for q in ctx["api"]("ticker",markets=m) if q["market"]==m)
    p=float(q["trade_price"]);ts=int(q.get("trade_timestamp") or q["timestamp"])//1000;at=int(ctx["now"]().timestamp())
    if not math.isfinite(p) or p<=0 or not 0<=at-ts<=120:raise ValueError("현재가 지연 또는 오류")
    return p,ts,at
def run(ctx):
    begin=time.monotonic();now=ctx["now"];candles=ctx["candles"];api=ctx["api"];iso=ctx["iso"]
    try:
        end=int(now().timestamp());probe=candles("KRW-BTC",240,end,8,latest=True)
        if len(probe)<2:raise RuntimeError("BTC 4시간봉 경계 확인 실패")
        anchor=probe[-1][0]%STEP;cut=(end-anchor)//STEP*STEP+anchor
        if any(r[0]%STEP!=anchor for r in probe) or not any(r[0]==cut-STEP and ctx["r131valid"](r) for r in probe):raise RuntimeError("완료 봉 지연")
        markets=sorted({r["market"] for r in api("market/all") if r["market"].startswith("KRW-") and r["market"][4:] not in ctx["STABLE"]})
        if not markets:raise RuntimeError("시장 목록 없음")
        patch(phase="1/2 전체 KRW 조건 검색",total=len(markets),universe=len(markets),signal_time=iso(cut),anchor=anchor)
        hits=[];cov=[];errors=[]
        def fetch(m):
            rows=candles(m,240,cut,50,latest=False)
            z,status=candidate(rows,cut,ctx)
            return rows,z,status
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs={pool.submit(fetch,m):m for m in markets}
            for f in as_completed(jobs):
                m=jobs[f]
                try:
                    rows,z,status=f.result()
                    cov.append(dict(market=m,bars=len(rows),status=status,**(z or {})))
                    if status=="strong":hits.append(dict(z,market=m))
                except Exception as e:errors.append(dict(market=m,error=str(e)));cov.append(dict(market=m,status="error"))
                with LOCK:STATE["done"]+=1;STATE["error_count"]=len(errors)
        patch(phase="2/2 후보 현재가 확인",done=0,total=len(hits),candidate_count=len(hits),valid_markets=sum(r["status"] in ("strong","not_strong") for r in cov))
        results=[]
        for r in hits:
            a=dict(r,anchor=anchor,signal_ts=cut,signal_time=iso(cut))
            try:
                p,ts,at=quote(r["market"],ctx)
                a.update(price=p,observed_ts=at,observed_at=iso(at),quote_ts=ts,quote_age_seconds=at-ts,
                         drift_pct=100*(p/r["reference_close"]-1),status="STRONG WATCH")
            except Exception as e:
                a.update(status="UNCONFIRMED",quote_error=str(e));errors.append(dict(market=r["market"],error="현재가: "+str(e)))
            results.append(a)
            with LOCK:STATE["done"]+=1;STATE["error_count"]=len(errors)
        if (int(now().timestamp())-anchor)//STEP*STEP+anchor!=cut:raise RuntimeError("검색 중 4시간봉 경계 변경: 재검색")
        shown=[r for r in results if r["status"]=="STRONG WATCH"]
        shown.sort(key=lambda r:(r["volume3_vs_prev3"],-r["concentration3"],r["market"]))
        patch(status="done",phase="완료",rows=shown,details=results,coverage=cov,errors=errors,error_count=len(errors),
              strong_count=len(shown),unconfirmed_count=len(results)-len(shown),elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:patch(status="error",phase="실패",errors=[dict(error=str(e))],error_count=1)

def start(ctx):
    with LOCK:
        if STATE["status"]=="running":return False
        STATE.clear();STATE.update(status="running",phase="준비",scan_id=str(time.time_ns()),done=0,total=0,rows=[],errors=[],coverage=[],error_count=0)
    threading.Thread(target=run,args=(ctx,),daemon=True).start();return True

def track(m,observed,anchor,ctx):
    now=int(ctx["now"]().timestamp());deadline=observed+7*DAY
    step=3600;a=anchor%step;end=min(now,deadline)
    first=((observed-a+step-1)//step)*step+a;cut=(end-a)//step*step+a
    expected=max(0,(cut-first)//step)
    rows=ctx["candles"](m,60,cut,200,latest=False) if expected else []
    by={r[0]:r for r in rows};valid=[by[t] for t in range(first,cut,step) if ctx["r131valid"](by.get(t))]
    out=dict(market=m,expected=expected,valid=len(valid),high=max((r[2] for r in valid),default=None),
             low=min((r[3] for r in valid),default=None),checked_ts=now,complete=now>=deadline,partial_windows_excluded=True,
             current_price=None,current_ts=None,deadline_ts=deadline,deadline_at=ctx["iso"](deadline))
    if now<deadline:
        try:
            p,ts,at=quote(m,ctx)
            if observed<=ts<=at<deadline:out.update(current_price=p,current_ts=ts)
        except Exception as e:out["quote_error"]=str(e)
    return out

HTML=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WHALE DETECTOR V2.0 LIVE</title><style>
*{box-sizing:border-box}[hidden]{display:none!important}body{max-width:760px;margin:auto;padding:14px;background:#f5f8f7;color:#172a23;font:15px/1.55 system-ui}section,article{background:#fff;border:1px solid #d7e3dd;border-radius:14px;padding:14px;margin:10px 0}h1{font-size:25px;margin:6px 0}h2{font-size:18px}h3{margin:0;font-size:18px}button,a{font:inherit;padding:12px;border-radius:9px;border:1px solid #a8bfb3;color:#176345;background:white;display:inline-block;margin:4px 0;text-decoration:none}button.main{width:100%;background:#176345;color:white;font-weight:bold}button:disabled{opacity:.5}small{color:#53645d;display:block}progress{width:100%;accent-color:#176345}pre{white-space:pre-wrap}strong{color:#176345}.watch{color:#90631c}.pill{display:inline-block;padding:3px 8px;border-radius:6px;background:#e8f2ed}.line{display:flex;justify-content:space-between;gap:8px}.bars{display:flex;gap:4px;margin:10px 0}.bars span{height:8px;flex:1;border-radius:4px;background:#d8dfdc}.bars .yes{background:#176345}.bars .unknown{background:#eee;border:1px dashed #999}.muted{color:#66756e}</style>
<h1>WHALE DETECTOR V2.0 LIVE</h1><small>고래 매집 의심 정황 · STRONG WATCH 전용 · 매매 주문 없음</small><a href="/whalereverse16">V1.6 과거 연구</a><section><button id="go" class="main">전체 KRW STRONG WATCH 검색</button><p id="phase">대기</p><progress id="bar" max="1" value="0"></progress><p id="audit"></p><small>완료 4시간봉 43개 연속 확인. 연구와 동일한 6개 조건 전부 충족. 스테이블코인 제외. 신규 상장 별도 14일 필터는 적용하지 않음(역사 검증 조건 유지). 체결·호가 미검증. 실제 고래 매집 확정 아님. 현재가 지연 후보는 기록하지 않음.</small><pre id="err"></pre></section>
<section><h2>STRONG WATCH 포착 종목</h2><div id="results">검색 전</div></section><section><h2>7일 누적 검증</h2><small>같은 브라우저에 최초 포착가 저장. 동일 종목 9일 이내 중복 등록 제외(과거 연구와 동일 간격). 추적 갱신은 버튼으로 실행. 완료 1시간봉과 조회 시점 현재가만 반영, 첫·마지막 불완전 봉 제외. 미확인 구간이 있으므로 관측 최고·최저는 실제 극값과 다를 수 있음. 기록은 브라우저 저장소 삭제 시 사라지므로 CSV 보관 권장. 7일 이후 수익률이 아닌 관측 가격 변화율.</small><button id="refresh">추적 결과 갱신</button> <button id="csv">누적 CSV 받기</button> <button id="scanCsv">검색 상세 CSV</button><p id="trackmsg"></p><div id="totals"></div><div id="history"></div></section>
<script>
const $=x=>document.getElementById(x),key='whale_detector_v2_0_history',ks=['range3_ok','down3_ok','range7_ok','half_volume_ok','low_location_ok','concentration_ok'],labels=['3일 변동폭 12% 이상','3일 가격 하락','7일 변동폭 25% 이상','3일 거래량 0.5배 이하','3일 종가 위치 하위 25%','거래대금 집중도 40% 이상'];let hist=[],storage=true,loaded=null,busy=false,starting=false,tracking=false,latest=[];
try{hist=JSON.parse(localStorage.getItem(key)||'[]');if(!Array.isArray(hist))throw Error('기록 형식 오류')}catch(e){storage=false;$('trackmsg').textContent='저장소 읽기 실패: '+e.message}
function save(){if(!storage)return;try{localStorage.setItem(key,JSON.stringify(hist))}catch(e){storage=false;$('trackmsg').textContent='저장 실패: CSV를 보관하세요'}}
const node=(tag,s,cl)=>{let n=document.createElement(tag);if(s!==undefined)n.textContent=s;if(cl)n.className=cl;return n};
const fmt=(v,d=2)=>typeof v==='number'&&Number.isFinite(v)?v.toLocaleString('ko-KR',{maximumFractionDigits:d}):'미확인';
function line(c,k,v){let r=node('div',undefined,'line');r.append(node('span',k),node('b',String(v)));c.append(r)}
function render(rows){$('results').replaceChildren();if(!rows.length){$('results').textContent='조건 충족 및 현재가 확인 종목 없음';return}for(let r of rows){let c=node('article');c.append(node('h3',r.market+' · STRONG WATCH'),node('p','매집 의심 정황 '+r.score+'/6'));let b=node('div',undefined,'bars');for(let k of ks)b.append(node('span',undefined,r[k]===true?'yes':'unknown'));c.append(b);line(c,'이번 조회가',fmt(r.price,8)+'원');line(c,'조회 시각',r.observed_at);line(c,'완료봉 종가 대비',fmt(r.drift_pct)+'%');if(Math.abs(r.drift_pct)>3)c.append(node('small','완료봉 종가와 조회가 차이가 큼: 추격·급락 주의. 연구 조건 자체는 그대로 기록.'));let d=node('details');d.append(node('summary','포착 이유 및 추가 정황'));ks.forEach((k,i)=>line(d,labels[i],r[k]===true?'충족':'미확인'));line(d,'3일 변동폭',fmt(r.span3)+'%');line(d,'3일 가격 변화',fmt(r.change3)+'%');line(d,'7일 변동폭',fmt(r.span7)+'%');line(d,'3일 거래량 배수',fmt(r.volume3_vs_prev3)+'배');line(d,'3일 종가 위치',fmt(r.close_location3)+'%');line(d,'거래대금 집중도',fmt(r.concentration3)+'%');for(let [k,label] of [['peak_defense','집중 봉 아랫꼬리 방어'],['post_peak_quiet','집중 이후 거래대금 감소'],['post_peak_low_held','집중 이후 저점 방어'],['prior_renewal','사전 거래대금 재유입']])line(d,label,r[k]===true?'충족':r[k]===false?'미충족':'미확인');d.append(node('small','추가 정황은 참고용. 매수 신호 및 실제 고래 매집 증거가 아님.'));c.append(d);$('results').append(c)}}
function add(rows){if(!storage)return;for(let r of rows){if(!hist.some(x=>x.market===r.market&&r.observed_ts-x.observed_ts<9*86400&&r.observed_ts>=x.observed_ts)&&!hist.some(x=>x.market===r.market&&x.observed_ts===r.observed_ts)){hist.push({...r,high:r.price,low:r.price,checked_ts:r.observed_ts,expected:0,valid:0})}}save();draw()}
function draw(){let h=$('history');h.replaceChildren();let complete=hist.filter(r=>r.complete),covered=complete.filter(r=>r.expected===r.valid);$('totals').textContent='누적 '+hist.length+'건 · 7일 경과 '+complete.length+'건 · 내부 완료봉 확보 '+covered.length+'건 · 진행 '+(hist.length-complete.length)+'건';if(complete.length){let z=node('p','7일 경과 사례 관측 도달: '+[30,50,100].map(k=>'+'+k+'% '+complete.filter(r=>100*(r.high/r.price-1)+1e-8>=k).length+'/'+complete.length).join(' · ')+' / -20% '+complete.filter(r=>100*(r.low/r.price-1)<=-20+1e-8).length+'/'+complete.length+' (미관측 구간 포함, 확정 확률 아님)');$('totals').append(z)}if(!hist.length){h.textContent='기록 없음';return}for(let r of hist.slice().reverse()){let c=node('article');let gain=(r.high/r.price-1)*100;let low=(r.low/r.price-1)*100;c.append(node('h3',r.market+' · '+(r.complete?'7일 경과':'추적 중')));line(c,'최초 포착',r.observed_at+' / '+fmt(r.price,8)+'원');line(c,'관측 최고 / 최저',fmt(gain)+'% / '+fmt(low)+'%');line(c,'상승 도달',[10,20,30,50,100].filter(x=>gain+1e-8>=x).map(x=>'+'+x+'%').join(' · ')||'아직 관측 없음');line(c,'완료 1시간봉',r.valid+'/'+r.expected+(r.complete?' · 종료':''));if(r.deadline_at)line(c,'추적 종료',r.deadline_at);if(r.current_price!=null)line(c,'최근 갱신가',fmt(r.current_price,8)+'원');if(r.track_error)c.append(node('small',r.track_error));h.append(c)}}
async function poll(){if(busy||starting)return;busy=true;try{let res=await fetch('/whalelive20/status',{cache:'no-store'});if(!res.ok)throw Error('HTTP '+res.status);let s=await res.json();$('go').disabled=s.status==='running'||tracking;$('refresh').disabled=s.status==='running'||tracking;$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('bar').max=Math.max(1,s.total);$('bar').value=s.done;$('audit').textContent=s.universe?'전체 '+s.universe+' · 후보 '+(s.candidate_count??'—')+' · 유효 '+(s.valid_markets??'—')+' · STRONG WATCH '+(s.strong_count??'—')+' · 현재가 미확인 '+(s.unconfirmed_count??'—')+' · 오류 '+(s.error_count||0):'';$('err').textContent=(s.error_preview||[]).map(e=>(e.market||'')+' '+e.error).join('\n');if(s.status==='done'&&loaded!==s.scan_id){let d=await(await fetch('/whalelive20/results',{cache:'no-store'})).json();if(d.scan_id!==s.scan_id)return;loaded=s.scan_id;latest=d.rows;render(latest);add(latest)}}catch(e){$('phase').textContent='연결 오류: '+e.message;$('go').disabled=tracking}finally{busy=false}}
$('go').onclick=async()=>{if(busy||starting||tracking)return;starting=true;$('go').disabled=true;try{let r=await fetch('/whalelive20/start',{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('이미 검색 중');loaded=null;$('results').textContent='검색 중'}catch(e){$('phase').textContent=e.message}finally{starting=false;poll()}};
$('refresh').onclick=async()=>{if(tracking||starting||busy)return;tracking=true;$('refresh').disabled=$('go').disabled=true;let n=0;try{for(let r of hist){if(r.complete&&r.expected===r.valid)continue;try{let q=new URLSearchParams({market:r.market,observed:r.observed_ts,anchor:r.anchor});let res=await fetch('/whalelive20/track?'+q);let d=await res.json();if(!res.ok)throw Error(d.error||res.status);r.high=Math.max(r.high,d.high??r.price,d.current_price??r.price);r.low=Math.min(r.low,d.low??r.price,d.current_price??r.price);Object.assign(r,{expected:d.expected,valid:d.valid,complete:d.complete,checked_ts:d.checked_ts,deadline_at:d.deadline_at,current_price:d.current_price??r.current_price,track_error:d.quote_error||''})}catch(e){r.track_error=e.message}n++;$('trackmsg').textContent='갱신 '+n+'건';save();draw()}}finally{tracking=false;$('refresh').disabled=$('go').disabled=false;poll()}};
function csv(rows,name){if(!rows.length){$('trackmsg').textContent='저장할 기록 없음';return}let keys=[...new Set(rows.flatMap(Object.keys))],esc=v=>'"'+String(v??'').replaceAll('"','""')+'"';let data='\ufeff'+[keys.map(esc).join(','),...rows.map(r=>keys.map(k=>esc(r[k])).join(','))].join('\r\n');let a=document.createElement('a');a.href=URL.createObjectURL(new Blob([data],{type:'text/csv;charset=utf-8'}));a.download='WHALE_DETECTOR_V2_0_'+name+'_'+new Date().toISOString().replace(/[-:]/g,'').slice(0,15)+'.csv';document.body.append(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},30000)}
$('csv').onclick=()=>csv(hist.map(r=>({...r,peak_pct:100*(r.high/r.price-1),low_pct:100*(r.low/r.price-1),...Object.fromEntries([10,20,30,50,100].map(x=>['hit_'+x,100*(r.high/r.price-1)>=x]))})),'TRACK');
$('scanCsv').onclick=()=>{if(loaded)location.href='/whalelive20/csv?scan_id='+encodeURIComponent(loaded)};draw();poll();setInterval(poll,2000);
</script></html>"""

def handle(h,u,ctx,post=False):
    p=u.path
    if not (p=="/whalelive20" or p.startswith("/whalelive20/")):return False
    q=parse_qs(u.query)
    if post and p=="/whalelive20/start":h.js({"started":start(ctx)})
    elif not post and p=="/whalelive20":h.send(HTML,"text/html; charset=utf-8")
    elif not post and p=="/whalelive20/status":
        with LOCK:
            d={k:v for k,v in STATE.items() if k not in ("rows","details","coverage","errors")};d["error_preview"]=STATE["errors"][:8]
        h.js(d)
    elif not post and p=="/whalelive20/results":
        with LOCK:d=dict(scan_id=STATE.get("scan_id"),rows=STATE["rows"])
        h.js(d)
    elif not post and p=="/whalelive20/csv":
        with LOCK:
            ok=STATE["status"]=="done" and q.get("scan_id",[""])[0]==STATE.get("scan_id")
            rows=[dict(r,record_type="candidate") for r in STATE.get("details",[])] + [dict(r,record_type="coverage") for r in STATE["coverage"]]+[dict(r,record_type="error") for r in STATE["errors"]] if ok else []
        if not ok:h.js({"error":"scan again"},409);return True
        fields=list(dict.fromkeys(k for r in rows for k in r)) or ["market"]
        b=io.StringIO();w=csv.DictWriter(b,fieldnames=fields);w.writeheader();w.writerows(rows)
        h.send(("\ufeff"+b.getvalue()).encode(),"text/csv; charset=utf-8",filename="WHALE_DETECTOR_V2_0_SCAN_"+ctx["now"]().astimezone(ctx["KST"]).strftime("%Y%m%d%H%M")+".csv")
    elif not post and p=="/whalelive20/track":
        try:
            m=q.get("market",[""])[0];t=int(q.get("observed",["0"])[0]);a=int(q.get("anchor",["-1"])[0]);now=int(ctx["now"]().timestamp())
            if not re.fullmatch(r"KRW-[A-Z0-9]{1,30}",m) or not 0<=a<STEP or not 0<t<=now:raise ValueError("invalid tracking request")
            h.js(track(m,t,a,ctx))
        except Exception as e:h.js({"error":str(e)},400)
    else:h.js({"error":"not found"},404)
    return True
