"""FLOW PRICE MINI V0.4: read-only snapshot research; no orders."""
import csv
import io
import math
import re
import threading
import time
from datetime import datetime, timezone, timedelta
import requests
from flask import Blueprint, jsonify, request, Response

VERSION = "FLOW PRICE MINI V0.4"
API = "https://api.bithumb.com"
KST = timezone(timedelta(hours=9))
STABLE = {"USDT","USDC","DAI","TUSD","FDUSD","USDE","USDD","USD1","PYUSD","RLUSD","USDS"}
LOCK = threading.Lock()
NEXT = 0.0
CACHE = (0, [])
bp = Blueprint("flow_price_mini", __name__, url_prefix="/flow-price-mini")

def get(path, **params):
    global NEXT
    with LOCK:
        now = time.monotonic()
        delay = max(0, NEXT-now)
        if delay > 2:
            raise RuntimeError("요청 혼잡: 잠시 후 재검색")
        NEXT = max(now, NEXT) + .16
    if delay:
        time.sleep(delay)
    r = requests.get(API + path, params=params, timeout=(3,5))
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError("API 응답 형식 오류")
    return data

def markets():
    global CACHE
    if time.monotonic()-CACHE[0]<45 and CACHE[1]:
        return CACHE[1]
    m = {x["market"]:x.get("korean_name",x["market"]) for x in get("/v1/market/all")
         if x["market"].startswith("KRW-") and x["market"][4:] not in STABLE}
    keys = list(m)
    out = []
    for i in range(0,len(keys),50):
        for x in get("/v1/ticker",markets=",".join(keys[i:i+50])):
            if x["market"] in m:
                out.append(dict(market=x["market"], name=m[x["market"]],
                                turnover=float(x["acc_trade_price_24h"])))
    CACHE=(time.monotonic(),sorted(out,key=lambda x:x["turnover"],reverse=True))
    return CACHE[1]

def candle_rows(data, now):
    rows={}
    for x in data:
        ts=datetime.fromisoformat(x["candle_date_time_utc"]).replace(tzinfo=timezone.utc).timestamp()
        if ts+60>now:
            continue
        row=dict(t=ts,o=float(x["opening_price"]),h=float(x["high_price"]),
                 l=float(x["low_price"]),c=float(x["trade_price"]),
                 v=float(x["candle_acc_trade_volume"]),a=float(x["candle_acc_trade_price"]))
        if min(row["o"],row["h"],row["l"],row["c"])<=0 or min(row["v"],row["a"])<0:
            raise ValueError("캔들 값 오류")
        rows[ts]=row
    return sorted(rows.values(),key=lambda x:x["t"])

def flow_stats(data, now):
    ticks={}
    for x in data:
        t=float(x["timestamp"])/1000
        p=float(x["trade_price"])
        v=float(x["trade_volume"])
        side=x["ask_bid"].upper()
        if p<=0 or v<=0 or side not in ("BID","ASK") or t>now+1:
            continue
        ticks[str(x["sequential_id"])]=(t,p,v,side)
    all_ticks=list(ticks.values())
    span=now-min((x[0] for x in all_ticks),default=now)
    def summarize(seconds, min_ticks, max_age):
        recent=[x for x in all_ticks if now-seconds<=x[0]<=now]
        b=sum(p*v for t,p,v,s in recent if s=="BID")
        a=sum(p*v for t,p,v,s in recent if s=="ASK")
        last=max(recent,key=lambda x:x[0]) if recent else None
        age=now-last[0] if last else None
        covered=span>=seconds and len(recent)>=min_ticks and age is not None and age<=max_age and b+a>0
        return dict(coverage=covered,ticks=len(recent),last_trade_age=round(age,1) if last else None,
                    bid_pct=round(b/(b+a)*100,2) if b+a else None,
                    delta_krw=round(b-a),last_price=last[1] if last else None)
    one=summarize(60,8,12)
    five=summarize(300,15,30)
    chosen=five if five["coverage"] else one if one["coverage"] else five
    return dict(**chosen,coverage_seconds=round(span,1),
                flow_window_sec=300 if chosen is five else 60,
                ticks60=one["ticks"],bid_pct60=one["bid_pct"],coverage60=one["coverage"],
                ticks300=five["ticks"],bid_pct300=five["bid_pct"],coverage300=five["coverage"])


def evaluate(rows, flow, now, fee=.25):
    end = math.floor(now/60)*60
    short = [x for x in rows if end-900 <= x["t"] < end]
    window = [x for x in rows if end-3600 <= x["t"] < end]
    # Aggregate actual observed 1m bars into fixed, closed 5m time buckets.
    # Missing minutes are not filled. Missing 5m buckets remain unknown.
    end5=math.floor(now/300)*300
    groups=[]
    for t in range(end5-1500,end5,300):
        part=[x for x in rows if t<=x["t"]<t+300]
        groups.append(dict(t=t,o=part[0]["o"],h=max(x["h"] for x in part),
                           l=min(x["l"] for x in part),c=part[-1]["c"],n=len(part)) if part else None)
    observed5=sum(x is not None for x in groups)
    latest=groups[-1]
    c=latest["c"] if latest else None
    p=flow["last_price"]
    pa_data=observed5==5 and latest["n"]>=2 and sum(x["n"] for x in groups)>=8
    breakout=max(x["h"] for x in groups[-3:-1] if x is not None) if any(x is not None for x in groups[-3:-1]) else None
    stop=min((x["l"] for x in groups[-3:-1] if x is not None),default=None)
    pa=(stop>min(x["l"] for x in groups[:2]) and c>breakout and c>latest["o"]) if pa_data else None
    v = sum(x["v"] for x in window)
    vwap = sum(x["a"] for x in window)/v if v>0 else None
    long_data = len(window)>=24 and vwap is not None and vwap>0
    vw = bool(p>=vwap and p/vwap<=1.025) if long_data and p and flow["last_trade_age"] is not None and flow["last_trade_age"]<=30 else None
    step=.0025
    bins={}
    for x in window:
        if x["v"]>0 and x["a"]>0:
            k=math.floor(math.log(x["a"]/x["v"])/math.log1p(step))
            bins[k]=bins.get(k,0)+x["v"]
    maxvol=max(bins.values(),default=0)
    # Use upper edge of heavy bins; current occupied heavy bin is not silently ignored.
    levels=[(math.exp(k*math.log1p(step)),math.exp((k+1)*math.log1p(step)))
            for k,vol in bins.items() if vol>=maxvol*.45]
    target_factor=(1+fee/100)*1.005/(1-fee/100)
    resistance=min((max(lo,p) for lo,hi in levels if p and hi>p),default=None)
    vp=bool(resistance is None or resistance>=p*target_factor) if long_data and p and maxvol and flow["last_trade_age"] is not None and flow["last_trade_age"]<=30 else None
    of=bool(flow["bid_pct"]>=58) if flow["coverage"] else None
    flags=[pa,vw,of,vp]
    score=sum(x is True for x in flags)
    unknown=sum(x is None for x in flags)
    fresh=bool(p and c and breakout and stop and abs(p/c-1)<=.008 and p>=breakout and p>stop)
    ready=score==4 and fresh and flow["coverage"]
    status="BUY 후보" if ready else "데이터 부족" if unknown else "관심" if score>=2 else "관찰"
    return dict(version=VERSION,time_kst=datetime.fromtimestamp(now,KST).strftime("%Y-%m-%d %H:%M:%S"),
                status=status,score=score,pa=pa,vwap_ok=vw,flow_ok=of,vp_ok=vp,
                price=p,closed_price=c,vwap=vwap,resistance=resistance,breakout=breakout,
                fresh=fresh,fee_each=fee,target_net_pct=.5,
                target_ref=p*target_factor if p else None,stop_ref=stop,
                bars15=len(short),bars60=len(window),bars5_observed=observed5,unknown=unknown,
                **{k:v for k,v in flow.items() if k!="last_price"})

@bp.get("/")
def home():
    return Response(HTML,mimetype="text/html")

@bp.get("/markets")
def market_api():
    try:
        limit=int(request.args.get("limit",30))
        minimum=float(request.args.get("minimum",100000000))
        if limit not in (10,20,30,60) or minimum not in (0,50000000,100000000,500000000):
            return jsonify(error="설정 범위 오류"),400
        all_m=markets()
        return jsonify(items=[x for x in all_m if x["turnover"]>=minimum][:limit],total=len(all_m))
    except Exception as e:
        return jsonify(error=str(e)),502

@bp.get("/coin")
def coin_api():
    market=request.args.get("market","")
    if not re.fullmatch(r"KRW-[A-Z0-9]{1,20}",market):
        return jsonify(error="종목 코드 오류"),400
    try:
        fee=float(request.args.get("fee",.25))
        if fee not in (.25,.04):
            return jsonify(error="수수료 설정 오류"),400
        candles=get("/v1/candles/minutes/1",market=market,count=90)
        ticks=get("/v1/trades/ticks",market=market,count=500)
        now=time.time()
        data=evaluate(candle_rows(candles,now),flow_stats(ticks,now),now,fee)
        return jsonify(data)
    except Exception as e:
        return jsonify(error=str(e)),502

def replay_rows(rows, start, end, fee):
    by={x["t"]:x for x in rows if start<=x["t"]<end}
    # Require next full minute's observed open. No filling or assumed executions.
    if start not in by:
        return dict(result="ENTRY_MISSING",observed_bars=len(by))
    entry=by[start]["o"]
    target=entry*(1+fee/100)*1.005/(1-fee/100)
    stop=entry*.99
    base=dict(entry=entry,target=target,stop=stop,observed_bars=len(by))
    def done(outcome, price, t):
        net=(price*(1-fee/100)/(entry*(1+fee/100))-1)*100
        return dict(**base,result=outcome,exit=price,net_pct=round(net,4),minutes=int((t-start)/60)+1)
    for t in range(start,end,60):
        x=by.get(t)
        if x is None:
            return dict(**base,result="GAP_UNVERIFIED")
        if x["o"]<=stop:
            return done("STOP",x["o"],t)
        if x["l"]<=stop:
            return done("STOP_BOTH" if x["h"]>=target else "STOP",stop,t)
        if x["h"]>=target:
            return done("TARGET",target,t)
    return done("TIME",by[end-60]["c"],end-60)

@bp.get("/replay")
def replay_api():
    market=request.args.get("market","")
    if not re.fullmatch(r"KRW-[A-Z0-9]{1,20}",market):
        return jsonify(error="종목 코드 오류"),400
    try:
        fee=float(request.args.get("fee",.25))
        if fee not in (.25,.04):
            return jsonify(error="수수료 설정 오류"),400
        stamp=datetime.strptime(request.args["at"],"%Y-%m-%d %H:%M:%S").replace(tzinfo=KST).timestamp()
        start=int(stamp//60+1)*60
        end=start+3600
        now=time.time()
        if stamp>now+5 or stamp<now-7*86400:
            return jsonify(error="최근 7일 관측만 검증 가능"),400
        if now<end:
            return jsonify(result="PENDING",remaining_minutes=math.ceil((end-now)/60))
        to=datetime.fromtimestamp(end,timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        raw=get("/v1/candles/minutes/1",market=market,count=65,to=to)
        return jsonify(replay_rows(candle_rows(raw,now),start,end,fee))
    except Exception as e:
        return jsonify(error=str(e)),502

def register_flow_price(app):
    app.register_blueprint(bp)

HTML = r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>FLOW PRICE MINI V0.4</title><style>
*{box-sizing:border-box}body{margin:0;background:#111722;color:#e6ebf3;font:15px system-ui,sans-serif}main{max-width:760px;margin:auto;padding:16px}h1{font-size:23px;margin:8px 0}p,small{color:#aab8ca;line-height:1.55}button,select{font:inherit;color:#e6ebf3;background:#25334a;border:1px solid #43526b;border-radius:9px;padding:10px}button{cursor:pointer}button:disabled{opacity:.5}#go{background:#18734f;width:100%;margin:12px 0;font-weight:700}label{display:inline-flex;flex-direction:column;gap:5px;margin:5px 5px 5px 0}article{background:#1b2534;border:1px solid #34435b;border-radius:12px;padding:12px;margin:9px 0}article.buy{border-color:#39c986}h3{margin:0 0 7px}.good{color:#68e3a3}.bad{color:#9aa9bc}.warn{color:#ffb96a}.line{display:flex;justify-content:space-between;gap:8px}.tags{display:flex;flex-wrap:wrap;gap:7px;margin:8px 0}.tag{font-size:13px;border:1px solid #40506a;padding:5px 7px;border-radius:7px}progress{width:100%;accent-color:#36bd8b}details{margin:12px 0}#err{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#88b9ff}
</style><main><h1>FLOW PRICE MINI V0.4</h1><p>가격 구조 · VWAP · 실제 체결 · 근사 매물대<br>실시간 연구 전용 / 자동매매 없음</p>
<label>검색 범위<select id="limit"><option>10</option><option>20</option><option selected>30</option><option>60</option></select></label>
<label>24시간 거래대금<select id="minimum"><option value="0">제한 없음</option><option value="50000000">5천만원</option><option value="100000000" selected>1억원</option><option value="500000000">5억원</option></select></label>
<label>편도 수수료<select id="fee"><option value=".25">0.25%</option><option value=".04">0.04%</option></select></label>
<button id="go">검색 시작</button><progress id="progress" value="0" max="1"></progress><p id="status">검색 대기</p>
<button id="save" disabled>이번 검색 CSV</button> <button id="all" disabled>누적 CSV</button>
<label><select id="filter"><option value="all">전체 표시</option><option value="watch">관심 이상</option><option value="buy">BUY 후보만</option></select></label>
<details open><summary>60분 가상 매매 검증</summary><p>이전 V0.3/V0.4 검색 기록의 모든 판정 완료 종목을 점수별 비교. 다음 1분봉 시가 진입, 순 +0.5% 목표, 가격 -1% 손절, 60분 청산. 봉 결측 시 미검증, 동일 봉 양쪽 도달 시 손절 우선. 실제 체결 및 슬리피지는 반영하지 않음.</p><label>이전 검색 CSV 가져오기<input id="importcsv" type="file" accept=".csv"></label><button id="replay">누적 기록 검증</button> <button id="replaycsv" disabled>검증 CSV</button><p id="replaystatus"></p><div id="replaytable"></div></details><div id="results"></div><details><summary>오류 내역</summary><p id="err"></p></details>
<details><summary>판정 기준 및 한계</summary><p>가격 구조: 최근 25분의 마감 5분 구간 5개 (실제 1분 관측봉 합계 8개 이상, 마지막 구간 2개 이상), 저점 상승 및 직전 2구간 고점 돌파. 관측봉 집계이며 빈 구간을 채우지 않음. VWAP: 최근 60분 관측봉 24개 이상 실제 거래대금/거래량, 과도한 이격 제외. 체결: 최근 300초 매수 체결대금 비중 58% 이상, 15건 이상, 마지막 체결 30초 이내, 전체 범위 확인. 300초 판정 불가 시 검증 가능한 60초(8건, 12초 이내)만 대체 사용하며 CSV에 두 구간 기록. 매물대: 최근 60분 관측 마감봉 평균 체결가별 거래량을 0.25% 구간에 집계한 근사치. 가까운 상단 대량 거래 구간을 회피. 4/4와 가격 신선도, 돌파 및 구조 저점 유지 확인 시 BUY 후보. 데이터 부족은 오류와 구분하며 해당 지표는 ?로 표시. 순수익 0.5% 참고 목표가는 수수료만 반영하며 슬리피지 미반영. 누적 CSV는 브라우저에 저장되는 관측 기록이며 매매 성과가 아님. 실제 수익 검증은 별도 필요.</p></details></main>
<script>
const $=id=>document.getElementById(id);let rows=[],errors=[],busy=false,history=[];
try{history=JSON.parse(localStorage.getItem('flow_price_mini_v04')||localStorage.getItem('flow_price_mini_v03')||'[]');if(!Array.isArray(history))history=[]}catch(e){}
const fmt=n=>n==null?'—':Number(n).toLocaleString('ko-KR',{maximumFractionDigits:8});
const node=(tag,text,cls)=>{let e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e};
function render(){const root=$('results');root.replaceChildren();let f=$('filter').value;let sorted=[...rows].sort((a,b)=>(b.status==='BUY 후보')-(a.status==='BUY 후보')||b.score-a.score);
 for(const r of sorted){if(f==='buy'&&r.status!=='BUY 후보'||f==='watch'&&!['BUY 후보','관심'].includes(r.status))continue;let el=document.createElement('article');if(r.status==='BUY 후보')el.className='buy';el.append(node('h3',r.name+' ('+r.market+') · '+r.score+'/4'+(r.unknown?' · 미판정 '+r.unknown:'')));let line=document.createElement('div');line.className='line';line.append(node('strong',r.status,r.status==='BUY 후보'?'good':'warn'),node('span',fmt(r.price)+'원'));el.append(line);let tags=document.createElement('div');tags.className='tags';for(const [k,n] of [['pa','가격 구조'],['vwap_ok','VWAP'],['flow_ok','실제 체결'],['vp_ok','근사 매물대']])tags.append(node('span',(r[k]===null?'? ':r[k]?'✓ ':'× ')+n,'tag '+(r[k]?'good':'bad')));el.append(tags);el.append(node('small','매수 체결 '+fmt(r.bid_pct)+'% · '+r.flow_window_sec+'초 '+r.ticks+'건 · 범위 '+(r.coverage?'충분':'부족')));el.append(document.createElement('br'));el.append(node('small','VWAP '+fmt(r.vwap)+' · 상단 매물 '+fmt(r.resistance)+' · '+r.time_kst));if(r.status==='BUY 후보'){el.append(document.createElement('br'));el.append(node('small','참고 목표 '+fmt(r.target_ref)+' · 구조 저점 '+fmt(r.stop_ref)+' (자동 주문 없음)'))}root.append(el);}
 $('err').textContent=errors.join('\n');$('save').disabled=!rows.length&&!errors.length;$('all').disabled=!history.length;
}
async function fetchj(url,ms){const ac=new AbortController(),timer=setTimeout(()=>ac.abort(),ms);try{let r=await fetch(url,{signal:ac.signal,cache:'no-store'}),d=await r.json();if(!r.ok||d.error)throw Error(d.error||r.status);return d}finally{clearTimeout(timer)}}
$('filter').onchange=render;
$('go').onclick=async()=>{if(busy)return;busy=true;$('go').disabled=true;rows=[];errors=[];$('progress').value=0;render();$('status').textContent='종목 목록 조회 중';let done=0;
 try{const fee=$('fee').value,limit=$('limit').value,minimum=$('minimum').value;
 const d=await fetchj('/flow-price-mini/markets?limit='+limit+'&minimum='+minimum,55000),items=d.items;$('progress').max=items.length||1;$('status').textContent='0 / '+items.length;let i=0;
 async function worker(){while(i<items.length){let x=items[i++];try{let r=await fetchj('/flow-price-mini/coin?market='+encodeURIComponent(x.market)+'&fee='+fee,22000);rows.push({...x,...r})}catch(e){errors.push(x.market+': '+e.message);history.push({...x,version:'FLOW PRICE MINI V0.4',time_kst:new Date().toLocaleString('sv-SE',{timeZone:'Asia/Seoul'}),status:'오류',error:e.message})}finally{done++;$('progress').value=done;$('status').textContent=done+' / '+items.length+' · BUY '+rows.filter(x=>x.status==='BUY 후보').length+' · 오류 '+errors.length;render()}}}
 await Promise.all([worker(),worker()]);history.push(...rows);history=history.slice(-5000);try{localStorage.setItem('flow_price_mini_v04',JSON.stringify(history))}catch(e){errors.push('브라우저 저장 한도: CSV로 저장해 주세요.')}$('status').textContent='검색 완료 · '+done+'종목 · BUY '+rows.filter(x=>x.status==='BUY 후보').length+' · 오류 '+errors.length+' · 전체 KRW '+d.total;
 }catch(e){errors.push('목록 조회: '+e.message);$('status').textContent='검색 실패: '+e.message}finally{busy=false;$('go').disabled=false;render()}};
function download(data,suffix){let cols=['version','time_kst','market','name','turnover','status','score','pa','vwap_ok','flow_ok','vp_ok','fresh','price','closed_price','vwap','resistance','breakout','coverage','coverage_seconds','ticks','last_trade_age','bid_pct','delta_krw','fee_each','target_net_pct','target_ref','stop_ref','bars15','bars60','bars5_observed','flow_window_sec','ticks60','bid_pct60','coverage60','ticks300','bid_pct300','coverage300','unknown','error'];let q=v=>'"'+String(v??'').replace(/"/g,'""')+'"';let csv=[cols.join(','),...data.map(r=>cols.map(k=>q(r[k])).join(','))].join('\r\n');let url=URL.createObjectURL(new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8'}));let a=document.createElement('a');a.href=url;a.download='FLOW_PRICE_MINI_V0_4_'+suffix+'_'+new Date().toLocaleString('sv-SE',{timeZone:'Asia/Seoul'}).replace(/\D/g,'').slice(0,12)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),3000)}
let replayResults=[],replayBusy=false,imported=[];
function parseCSV(text){
 let rows=[],row=[],cell='',quoted=false;
 text=text.replace(/^\ufeff/,'');
 for(let i=0;i<text.length;i++){
  let c=text[i];
  if(c==='"'){if(quoted&&text[i+1]==='"'){cell+='"';i++}else quoted=!quoted}
  else if(c===','&&!quoted){row.push(cell);cell=''}
  else if(c==='\n'&&!quoted){row.push(cell.replace(/\r$/,''));rows.push(row);row=[];cell=''}
  else cell+=c;
 }
 if(cell||row.length){row.push(cell);rows.push(row)}
 let head=rows.shift()||[];
 if(!['market','time_kst','score','unknown','version','fee_each'].every(k=>head.includes(k)))throw Error('검색 CSV 형식 확인');
 return rows.filter(r=>r.length===head.length).map(r=>Object.fromEntries(head.map((k,i)=>[k,r[i]])));
}
$('importcsv').onchange=async()=>{try{let f=$('importcsv').files[0];if(!f)return;if(f.size>3000000)throw Error('CSV 3MB 제한');imported=parseCSV(await f.text());$('replaystatus').textContent='가져온 관측 '+imported.length+'건'}catch(e){$('replaystatus').textContent=e.message}};
function renderReplay(){
 const root=$('replaytable');root.replaceChildren();
 for(let score=0;score<=4;score++){
  let a=replayResults.filter(x=>Number(x.score)===score),done=a.filter(x=>['TARGET','STOP','STOP_BOTH','TIME'].includes(x.result));
  if(!a.length)continue;
  let net=done.reduce((n,x)=>n+Number(x.net_pct),0);
  root.append(node('p',score+'/4 · '+a.length+'건 · 검증 '+done.length+' · 목표 '+done.filter(x=>x.result==='TARGET').length+' · 손절 '+done.filter(x=>x.result.startsWith('STOP')).length+' · 시간 '+done.filter(x=>x.result==='TIME').length+' · 평균 '+(done.length?(net/done.length).toFixed(3)+'%':'—')+' · 미확정 '+(a.length-done.length)));
 }
 $('replaycsv').disabled=!replayResults.length;
}
$('replay').onclick=async()=>{if(replayBusy)return;replayBusy=true;$('replay').disabled=true;replayResults=[];renderReplay();
 try{
 let unique=new Map();
 for(let x of [...history,...imported]){if(!['FLOW PRICE MINI V0.3','FLOW PRICE MINI V0.4'].includes(x.version)||x.unknown==null||x.unknown===''||Number(x.unknown)!==0||!x.market||!x.time_kst)continue;
 unique.set(x.market+'|'+x.time_kst+'|'+x.version,x)}
 let all=[...unique.values()].sort((a,b)=>String(b.time_kst).localeCompare(String(a.time_kst))),items=all.slice(0,120),i=0;
 $('replaystatus').textContent='검증 대상 '+items.length+'건 (최근 최대 120건)';
 async function work(){while(i<items.length){let x=items[i++];try{let r=await fetchj('/flow-price-mini/replay?market='+encodeURIComponent(x.market)+'&at='+encodeURIComponent(x.time_kst)+'&fee='+encodeURIComponent(x.fee_each),16000);replayResults.push({...x,...r})}catch(e){replayResults.push({...x,result:'ERROR',replay_error:e.message})}finally{$('replaystatus').textContent=replayResults.length+'/'+items.length+' · 검증 대상 전체 '+all.length+'건';renderReplay()}}}
 await Promise.all([work(),work()]);
 $('replaystatus').textContent='완료 '+replayResults.length+'건 · 미성숙/결측/오류는 수익률 집계 제외';
 }catch(e){$('replaystatus').textContent=e.message}finally{replayBusy=false;$('replay').disabled=false;renderReplay()}};
$('replaycsv').onclick=()=>{let cols=['version','time_kst','market','name','score','pa','vwap_ok','flow_ok','vp_ok','fee_each','result','entry','target','stop','exit','net_pct','minutes','observed_bars','remaining_minutes','replay_error'];let q=v=>'"'+String(v??'').replace(/"/g,'""')+'"';let url=URL.createObjectURL(new Blob(['\ufeff'+[cols.join(','),...replayResults.map(x=>cols.map(k=>q(x[k])).join(','))].join('\r\n')],{type:'text/csv;charset=utf-8'}));let a=document.createElement('a');a.href=url;a.download='FLOW_PRICE_MINI_V0_4_REPLAY_'+new Date().toLocaleString('sv-SE',{timeZone:'Asia/Seoul'}).replace(/\D/g,'').slice(0,12)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),3000)};
$('save').onclick=()=>download([...rows,...errors.map(error=>({version:'FLOW PRICE MINI V0.4',status:'오류',error}))],'SCAN');$('all').onclick=()=>download(history,'HISTORY');render();
</script></html>"""
