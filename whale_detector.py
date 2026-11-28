"""WHALE DETECTOR V1.0 - live accumulation evidence only."""
import csv, io, json, math, re, statistics, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs

VERSION="WHALE DETECTOR V1.0"
STEP=14400
DAY=86400
KEYS=("volume_flat","persistent_flow","higher_low","sell_absorption","big_buy_repeat")
LABELS=("가격 횡보 + 거래량 증가","반복 거래대금 증가","저점 상승","매도 압력 흡수","반복 대량 매수")
LOCK=threading.Lock()
STATE=dict(status="idle",phase="대기",done=0,total=0,rows=[],errors=[],coverage=[])
def patch(**kw):
    with LOCK: STATE.update(kw)

def candidate(rows,cut,ctx):
    by={r[0]:r for r in rows}
    candidates=[]; valid=ctx["r131valid"]
    for d in (7,3):
        z,a=ctx["whale32candles"](rows,cut,d)
        if not z or not z["volume_flat"] or z["candle_score"]<(2 if d==7 else 3):continue
        n=d*6
        seq=[by.get(cut-i*STEP) for i in range(4*n,0,-1)]
        if not all(valid(r) for r in seq):continue
        base=statistics.fmean(r[6] for r in seq[:-n])*6
        daily=[sum(r[6] for r in seq[-n+i:-n+i+6] if r) for i in range(0,n-6,6)]+[sum(r[6] for r in seq[-6:])]
        count=sum(v>=base*1.2 for v in daily) if base>0 else 0
        z.update(persistent_flow=(count>=math.ceil(d*.6)) if base>0 else None,
                 elevated_days=count,baseline_daily_krw=base,window_start_price=by[cut-(n+1)*STEP][4],
                 sell_absorption=None,big_buy_repeat=None)
        candidates.append(z)
    return candidates[0] if candidates else None

def classify(r):
    score=sum(r.get(k) is True for k in KEYS)
    known=sum(r.get(k) is not None for k in KEYS)
    flow=any(r.get(k) is True for k in KEYS[3:])
    strong=r.get("volume_flat") is True and r.get("persistent_flow") is True and score>=4 and flow
    return score,known,"STRONG" if strong else "WATCH" if score>=3 else "NONE"

def run(ctx):
    begin=time.monotonic(); now=ctx["now"]; candles=ctx["candles"]; api=ctx["api"]; iso=ctx["iso"]
    try:
        end=int(now().timestamp()); probe=candles("KRW-BTC",240,end,8,latest=True)
        if len(probe)<2:raise RuntimeError("BTC 4시간봉 경계 확인 실패")
        anchor=probe[-1][0]%STEP;cut=(end-anchor)//STEP*STEP+anchor
        if any(r[0]%STEP!=anchor for r in probe) or not any(r[0]==cut-STEP and ctx["r131valid"](r) for r in probe):raise RuntimeError("완료 봉 지연")
        markets=sorted({r["market"] for r in api("market/all") if r["market"].startswith("KRW-") and r["market"][4:] not in ctx["STABLE"]})
        if not markets:raise RuntimeError("시장 목록 없음")
        patch(phase="1/2 전체 KRW 매집 후보 검색",total=len(markets),universe=len(markets),signal_time=iso(cut),anchor=anchor)
        hits=[];cov=[];errors=[]
        def fetch(m):
            rows=candles(m,240,cut,200,latest=False)
            return rows,candidate(rows,cut,ctx)
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs={pool.submit(fetch,m):m for m in markets}
            for f in as_completed(jobs):
                m=jobs[f]
                try:
                    rows,z=f.result()
                    cov.append(dict(market=m,bars=len(rows),status="candidate" if z else "no_candidate_or_missing"))
                    if z:hits.append(dict(z,market=m))
                except Exception as e:errors.append(dict(market=m,error=str(e)));cov.append(dict(market=m,status="error"))
                with LOCK:STATE["done"]+=1;STATE["error_count"]=len(errors)
        hits.sort(key=lambda r:(-int(r.get("persistent_flow") is True),-int(r["period_days"]==7),-r["candle_score"],-r["obv_ratio_pct"],r["market"]))
        deep=hits[:20];patch(phase="2/2 상위 후보 체결·호가 확인",done=0,total=len(deep),candidate_count=len(hits),deep_limit=20)
        def enrich(r):
            m=r["market"];a=dict(r);errs=[]
            try:
                raw=api("trades/ticks",market=m,count=500)
                a.update(ctx["whale30trades"](raw,int(now().timestamp()*1000)))
            except Exception as e:errs.append(dict(market=m,error="체결: "+str(e)))
            try:
                raw=api("orderbook",markets=m)
                a.update(ctx["whale30book"](raw,m,int(now().timestamp()*1000)))
            except Exception as e:errs.append(dict(market=m,error="호가: "+str(e)))
            try:
                q=next(q for q in api("ticker",markets=m) if q["market"]==m)
                p=float(q["trade_price"]);ts=int(q["timestamp"])//1000;at=int(now().timestamp())
                if not math.isfinite(p) or p<=0 or not 0<=at-ts<=120:raise ValueError("현재가 지연 또는 오류")
                a.update(price=p,observed_ts=at,observed_at=iso(at),quote_ts=ts)
                # Do not chase a coin already outside its consolidation window.
                a["chase_pct"]=100*(p/a["reference_price"]-1)
                a["window_move_pct"]=100*(p/a["window_start_price"]-1)
                a["chase"]=a["chase_pct"]>3 or a["window_move_pct"]>a["change_limit_pct"]
                a["breakdown"]=a["chase_pct"]<-3 or a["window_move_pct"] < -a["change_limit_pct"]
            except Exception as e:errs.append(dict(market=m,error="현재가: "+str(e)))
            a["score"],a["known"],a["status"]=classify(a)
            if "price" not in a:a["status"]="UNCONFIRMED"
            elif a["chase"] or a["breakdown"]:a["status"]="SKIP"
            a["anchor"]=anchor
            return a,errs
        results=[]
        with ThreadPoolExecutor(max_workers=4) as pool:
            for f in as_completed([pool.submit(enrich,r) for r in deep]):
                try:
                    r,errs=f.result();results.append(r);errors.extend(errs)
                except Exception as e:errors.append(dict(error=str(e)))
                with LOCK:STATE["done"]+=1;STATE["error_count"]=len(errors)
        if (int(now().timestamp())-anchor)//STEP*STEP+anchor!=cut:raise RuntimeError("검색 중 봉 경계 변경: 재검색")
        results.sort(key=lambda r:(r["status"]!="STRONG",r["status"]!="WATCH",-r["score"],-r["period_days"],r["market"]))
        shown=[r for r in results if r["status"] in ("STRONG","WATCH")]
        patch(status="done",phase="완료",rows=shown,details=results,coverage=cov,errors=errors,error_count=len(errors),
              deep_checked=len(results),strong_count=sum(r["status"]=="STRONG" for r in shown),watch_count=sum(r["status"]=="WATCH" for r in shown),
              elapsed_seconds=round(time.monotonic()-begin,1))
    except Exception as e:patch(status="error",phase="실패",errors=[dict(error=str(e))],error_count=1)

def start(ctx):
    with LOCK:
        if STATE["status"]=="running":return False
        STATE.clear();STATE.update(status="running",phase="준비",scan_id=str(time.time_ns()),done=0,total=0,rows=[],errors=[],coverage=[],error_count=0)
    threading.Thread(target=run,args=(ctx,),daemon=True).start();return True

def track(m,observed,anchor,ctx):
    now=int(ctx["now"]().timestamp());deadline=observed+30*DAY
    end=min(now,deadline)
    first=((observed-anchor+STEP-1)//STEP)*STEP+anchor
    cut=(end-anchor)//STEP*STEP+anchor
    expected=max(0,(cut-first)//STEP)
    rows=ctx["candles"](m,240,cut,200,latest=False) if expected else []
    by={r[0]:r for r in rows}
    valid=[by[t] for t in range(first,cut,STEP) if ctx["r131valid"](by.get(t))]
    out=dict(market=m,expected=expected,valid=len(valid),high=max((r[2] for r in valid),default=None),
             low=min((r[3] for r in valid),default=None),checked_ts=now,complete=now>=deadline,partial_windows_excluded=True,
             current_price=None,current_ts=None)
    if now<deadline:
        try:
            q=next(q for q in ctx["api"]("ticker",markets=m) if q["market"]==m)
            p=float(q["trade_price"]);ts=int(q["timestamp"])//1000
            if math.isfinite(p) and p>0 and observed<=ts<=now and now-ts<=120:
                out.update(current_price=p,current_ts=ts)
        except Exception as e:out["quote_error"]=str(e)
    return out

HTML=r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WHALE DETECTOR V1.0</title><style>
*{box-sizing:border-box}[hidden]{display:none!important}body{max-width:760px;margin:auto;padding:14px;background:#f5f8f7;color:#172a23;font:15px/1.55 system-ui}section,article{background:#fff;border:1px solid #d7e3dd;border-radius:14px;padding:14px;margin:10px 0}h1{font-size:25px;margin:6px 0}h2{font-size:18px}h3{margin:0;font-size:18px}button,a{font:inherit;padding:12px;border-radius:9px;border:1px solid #a8bfb3;color:#176345;background:white;display:inline-block;margin:4px 0;text-decoration:none}button.main{width:100%;background:#176345;color:white;font-weight:bold}button:disabled{opacity:.5}small{color:#53645d;display:block}progress{width:100%;accent-color:#176345}pre{white-space:pre-wrap}strong{color:#176345}.watch{color:#90631c}.pill{display:inline-block;padding:3px 8px;border-radius:6px;background:#e8f2ed}.line{display:flex;justify-content:space-between;gap:8px}.bars{display:flex;gap:4px;margin:10px 0}.bars span{height:8px;flex:1;border-radius:4px;background:#d8dfdc}.bars .yes{background:#176345}.bars .unknown{background:#eee;border:1px dashed #999}.muted{color:#66756e}</style>
<h1>WHALE DETECTOR V1.0</h1><small>대폭발 상승 전 매집 의심 정황 포착 · 실제 주문 없음</small><a href="/whalebt11">V1.1.1 급등 역추적 백테스트</a><section><button id="go" class="main">전체 KRW 고래 매집 검색</button><p id="phase">대기</p><progress id="bar" max="1" value="0"></progress><p id="audit"></p><small>7일 매집 우선, 3일 3점 보조. 상위 20종목만 체결·호가 정밀 확인. STRONG: 4/5 이상 + 반복 유입 + 체결 정황. WATCH: 3/5 이상. 급등 추격 및 급락 이탈 제외. 실제 고래의 신원·매집은 확인 불가.</small><pre id="err"></pre></section>
<section><h2>포착 종목</h2><div id="results">검색 전</div></section><section><h2>상승 추적 기록</h2><small>같은 브라우저에 종목별 최초 포착을 30일간 저장. 결과 갱신 버튼으로 완료 4시간봉 최고·최저 및 조회가 확인. 최초·마지막 불완전 봉 제외, 누락 구간 미확인. 10/20/30/50/100%는 수수료 제외 가격 상승률이며 매매 수익률이 아님.</small><button id="refresh">추적 결과 갱신</button> <button id="csv">누적 CSV 받기</button> <button id="scanCsv">검색 상세 CSV</button><p id="trackmsg"></p><div id="history"></div></section>
<script>
const $=x=>document.getElementById(x),key='whale_detector_v1_0_history',ks=['volume_flat','persistent_flow','higher_low','sell_absorption','big_buy_repeat'],labels=['가격 횡보 + 거래량 증가','반복 거래대금 증가','저점 상승','매도 압력 흡수','반복 대량 매수'];let hist=[],storage=true,loaded=null,busy=false,starting=false,tracking=false,latest=[];
try{hist=JSON.parse(localStorage.getItem(key)||'[]');if(!Array.isArray(hist))throw Error('기록 형식 오류')}catch(e){storage=false;$('trackmsg').textContent='저장소 읽기 실패: '+e.message}
function save(){if(!storage)return;try{localStorage.setItem(key,JSON.stringify(hist))}catch(e){storage=false;$('trackmsg').textContent='저장 실패: CSV를 보관하세요'}}
const node=(tag,s,cl)=>{let n=document.createElement(tag);if(s!==undefined)n.textContent=s;if(cl)n.className=cl;return n};
const fmt=(v,d=2)=>typeof v==='number'&&Number.isFinite(v)?v.toLocaleString('ko-KR',{maximumFractionDigits:d}):'미확인';
function line(c,k,v){let r=node('div',undefined,'line');r.append(node('span',k),node('b',String(v)));c.append(r)}
function render(rows){$('results').replaceChildren();if(!rows.length){$('results').textContent='조건 충족 종목 없음';return}for(let r of rows){let c=node('article');c.append(node('h3',r.market+' · '+r.status,r.status==='WATCH'?'watch':''),node('p','매집 정황 '+r.score+'/5 · 확인 '+r.known+'/5 · '+r.period_days+'일 매집'));let b=node('div',undefined,'bars');for(let k of ks)b.append(node('span',undefined,r[k]===true?'yes':r[k]===null||r[k]===undefined?'unknown':''));c.append(b);line(c,'포착 기준가',fmt(r.price,8)+'원');line(c,'포착 시각',r.observed_at);let d=node('details'),s=node('summary','매집 정황 자세히');d.append(s);ks.forEach((k,i)=>line(d,labels[i],r[k]===true?'충족':r[k]===false?'미충족':'미확인'));line(d,'기간 거래량 배수',fmt(r.volume_ratio)+'배');line(d,'반복 거래대금 증가일',r.elevated_days+'/'+r.period_days);line(d,'OBV 순증가 비율',fmt(r.obv_ratio_pct)+'%');line(d,'매수 체결대금 비율',fmt(r.buy_share_pct)+'%');line(d,'대량 매수 / 매도',fmt(r.big_buy_count,0)+' / '+fmt(r.big_sell_count,0));line(d,'상위 5호가 매수/매도',fmt(r.book_bid_ask_ratio)+'배');d.append(node('small','호가는 스냅샷 참고용이며 재충전이나 실제 매집 증거로 점수화하지 않음. 체결은 최근 최대 500건, 최대 30분 표본.'));c.append(d);$('results').append(c)}}
function add(rows){if(!storage)return;for(let r of rows){if(!hist.some(x=>x.market===r.market&&r.observed_ts-x.observed_ts<30*86400&&r.observed_ts>=x.observed_ts)&&!hist.some(x=>x.market===r.market&&x.observed_ts===r.observed_ts)){hist.push({...r,high:r.price,low:r.price,checked_ts:r.observed_ts,expected:0,valid:0})}}save();draw()}
function draw(){let h=$('history');h.replaceChildren();if(!hist.length){h.textContent='기록 없음';return}for(let r of hist.slice().reverse()){let c=node('article');let gain=(r.high/r.price-1)*100;let low=(r.low/r.price-1)*100;c.append(node('h3',r.market+' · '+r.status));line(c,'포착',r.observed_at+' / '+fmt(r.price,8)+'원');line(c,'관측 최고 / 최저',fmt(gain)+'% / '+fmt(low)+'%');line(c,'상승 도달',[10,20,30,50,100].filter(x=>gain+1e-8>=x).map(x=>'+'+x+'%').join(' · ')||'아직 관측 없음');line(c,'봉 확인',r.valid+'/'+r.expected+(r.complete?' · 30일 종료':''));if(r.track_error)c.append(node('small',r.track_error));h.append(c)}}
async function poll(){if(busy||starting)return;busy=true;try{let res=await fetch('/whaledetector/status',{cache:'no-store'});if(!res.ok)throw Error('HTTP '+res.status);let s=await res.json();$('go').disabled=s.status==='running'||tracking;$('refresh').disabled=s.status==='running'||tracking;$('phase').textContent=s.phase+' '+s.done+'/'+s.total;$('bar').max=Math.max(1,s.total);$('bar').value=s.done;$('audit').textContent=s.universe?'전체 '+s.universe+' · 후보 '+(s.candidate_count??'—')+' · STRONG '+(s.strong_count??'—')+' · WATCH '+(s.watch_count??'—')+' · 오류 '+(s.error_count||0):'';$('err').textContent=(s.error_preview||[]).map(e=>(e.market||'')+' '+e.error).join('\n');if(s.status==='done'&&loaded!==s.scan_id){let d=await(await fetch('/whaledetector/results',{cache:'no-store'})).json();if(d.scan_id!==s.scan_id)return;loaded=s.scan_id;latest=d.rows;render(latest);add(latest)}}catch(e){$('phase').textContent='연결 오류: '+e.message;$('go').disabled=tracking}finally{busy=false}}
$('go').onclick=async()=>{if(busy||starting||tracking)return;starting=true;$('go').disabled=true;try{let r=await fetch('/whaledetector/start',{method:'POST'});if(!r.ok||!(await r.json()).started)throw Error('이미 검색 중');loaded=null;$('results').textContent='검색 중'}catch(e){$('phase').textContent=e.message}finally{starting=false;poll()}};
$('refresh').onclick=async()=>{if(tracking||starting||busy)return;tracking=true;$('refresh').disabled=$('go').disabled=true;let n=0;try{for(let r of hist){if(r.complete&&r.expected===r.valid)continue;try{let q=new URLSearchParams({market:r.market,observed:r.observed_ts,anchor:r.anchor});let res=await fetch('/whaledetector/track?'+q);let d=await res.json();if(!res.ok)throw Error(d.error||res.status);r.high=Math.max(r.high,d.high??r.price,d.current_price??r.price);r.low=Math.min(r.low,d.low??r.price,d.current_price??r.price);Object.assign(r,{expected:d.expected,valid:d.valid,complete:d.complete,checked_ts:d.checked_ts,track_error:d.quote_error||''})}catch(e){r.track_error=e.message}n++;$('trackmsg').textContent='갱신 '+n+'건';save();draw()}}finally{tracking=false;$('refresh').disabled=$('go').disabled=false;poll()}};
function csv(rows,name){if(!rows.length){$('trackmsg').textContent='저장할 기록 없음';return}let keys=[...new Set(rows.flatMap(Object.keys))],esc=v=>'"'+String(v??'').replaceAll('"','""')+'"';let data='\ufeff'+[keys.map(esc).join(','),...rows.map(r=>keys.map(k=>esc(r[k])).join(','))].join('\r\n');let a=document.createElement('a');a.href=URL.createObjectURL(new Blob([data],{type:'text/csv;charset=utf-8'}));a.download='WHALE_DETECTOR_V1_0_'+name+'_'+new Date().toISOString().replace(/[-:]/g,'').slice(0,15)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}
$('csv').onclick=()=>csv(hist.map(r=>({...r,peak_pct:100*(r.high/r.price-1),low_pct:100*(r.low/r.price-1),...Object.fromEntries([10,20,30,50,100].map(x=>['hit_'+x,100*(r.high/r.price-1)>=x]))})),'TRACK');
$('scanCsv').onclick=()=>{if(loaded)location.href='/whaledetector/csv?scan_id='+encodeURIComponent(loaded)};draw();poll();setInterval(poll,2000);
</script></html>"""

def handle(h,u,ctx,post=False):
    p=u.path
    if not (p=="/whaledetector" or p.startswith("/whaledetector/")):return False
    q=parse_qs(u.query)
    if post and p=="/whaledetector/start":h.js({"started":start(ctx)})
    elif not post and p=="/whaledetector":h.send(HTML,"text/html; charset=utf-8")
    elif not post and p=="/whaledetector/status":
        with LOCK:
            d={k:v for k,v in STATE.items() if k not in ("rows","details","coverage","errors")};d["error_preview"]=STATE["errors"][:8]
        h.js(d)
    elif not post and p=="/whaledetector/results":
        with LOCK:d=dict(scan_id=STATE.get("scan_id"),rows=STATE["rows"])
        h.js(d)
    elif not post and p=="/whaledetector/csv":
        with LOCK:
            ok=STATE["status"]=="done" and q.get("scan_id",[""])[0]==STATE.get("scan_id")
            rows=[dict(r,record_type="candidate") for r in STATE.get("details",[])] + [dict(r,record_type="coverage") for r in STATE["coverage"]]+[dict(r,record_type="error") for r in STATE["errors"]] if ok else []
        if not ok:h.js({"error":"scan again"},409);return True
        fields=list(dict.fromkeys(k for r in rows for k in r)) or ["market"]
        b=io.StringIO();w=csv.DictWriter(b,fieldnames=fields);w.writeheader();w.writerows(rows)
        h.send(("\ufeff"+b.getvalue()).encode(),"text/csv; charset=utf-8",filename="WHALE_DETECTOR_V1_0_SCAN_"+ctx["now"]().astimezone(ctx["KST"]).strftime("%Y%m%d%H%M")+".csv")
    elif not post and p=="/whaledetector/track":
        try:
            m=q.get("market",[""])[0];t=int(q.get("observed",["0"])[0]);a=int(q.get("anchor",["-1"])[0]);now=int(ctx["now"]().timestamp())
            if not re.fullmatch(r"KRW-[A-Z0-9]{1,30}",m) or not 0<=a<STEP or not 0<t<=now:raise ValueError("invalid tracking request")
            h.js(track(m,t,a,ctx))
        except Exception as e:h.js({"error":str(e)},400)
    else:h.js({"error":"not found"},404)
    return True
