"""FLOW PRICE MINI V0.1: read-only snapshot research; no orders."""
import csv
import io
import math
import re
import threading
import time
from datetime import datetime, timezone, timedelta
import requests
from flask import Blueprint, jsonify, request, Response

VERSION = "FLOW PRICE MINI V0.1"
API = "https://api.bithumb.com"
KST = timezone(timedelta(hours=9))
STABLE = {"USDT","USDC","DAI","TUSD","FDUSD","USDE","USDD","USD1","PYUSD","RLUSD"}
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
    recent=[x for x in all_ticks if now-60<=x[0]<=now]
    b=sum(p*v for t,p,v,s in recent if s=="BID")
    a=sum(p*v for t,p,v,s in recent if s=="ASK")
    span=now-min((x[0] for x in all_ticks),default=now)
    age=now-max((x[0] for x in recent),default=0)
    covered=span>=60 and len(recent)>=8 and age<=12 and b+a>0
    return dict(coverage=covered,coverage_seconds=round(span,1),ticks=len(recent),
                last_trade_age=round(age,1) if recent else None,
                bid_pct=round(b/(b+a)*100,2) if b+a else None,
                delta_krw=round(b-a),last_price=max(recent,key=lambda x:x[0])[1] if recent else None)

def evaluate(rows, flow, now, fee=.25):
    if len(rows)<45 or now-rows[-1]["t"]>125:
        raise ValueError("최근 마감봉 부족 또는 지연")
    # No invented candles; structural checks require consecutive recent minutes.
    if any(b["t"]-a["t"]!=60 for a,b in zip(rows[-15:-1],rows[-14:])):
        raise ValueError("최근 15분 마감봉 결측")
    c=rows[-1]["c"]
    p=flow["last_price"]
    short=rows[-15:]
    prior=short[-11:-6]
    recent=short[-6:-1]
    breakout=max(x["h"] for x in short[-4:-1])
    pa=min(x["l"] for x in recent)>min(x["l"] for x in prior) and c>breakout and c>short[-1]["o"]
    window=rows[-60:]
    v=sum(x["v"] for x in window)
    if v<=0:
        raise ValueError("거래량 부족")
    vwap=sum(x["a"] for x in window)/v
    vw=bool(p and c>=vwap and p>=vwap and p/vwap<=1.025)
    # Price-volume proxy: allocate each completed candle's volume to its own mean execution price.
    step=.0025
    bins={}
    for x in window:
        if x["v"]<=0:
            continue
        k=math.floor(math.log(x["a"]/x["v"])/math.log1p(step)) if x["a"]>0 else None
        if k is not None:
            bins[k]=bins.get(k,0)+x["v"]
    maxvol=max(bins.values(),default=0)
    if not maxvol:
        raise ValueError("매물대 데이터 부족")
    levels=[math.exp(k*math.log1p(step)) for k,vol in bins.items() if vol>=maxvol*.45]
    target_factor=(1+fee/100)*1.005/(1-fee/100)
    resistance=min((x for x in levels if p and x>p),default=None)
    # A nearby heavy bin is a blocker, not a claimed guaranteed resistance.
    vp=bool(p and (resistance is None or resistance>=p*target_factor))
    of=bool(flow["coverage"] and flow["bid_pct"]>=58)
    flags=[bool(pa),vw,of,vp]
    score=sum(flags)
    fresh=bool(p and abs(p/c-1)<=.008)
    ready=score==4 and fresh and flow["coverage"]
    status="BUY 후보" if ready else "관심" if score>=2 and flow["coverage"] else "관찰" if flow["coverage"] else "체결 부족"
    return dict(version=VERSION,time_kst=datetime.fromtimestamp(now,KST).strftime("%Y-%m-%d %H:%M:%S"),
                status=status,score=score,pa=flags[0],vwap_ok=flags[1],flow_ok=flags[2],vp_ok=flags[3],
                price=p,closed_price=c,vwap=vwap,resistance=resistance,breakout=breakout,
                fresh=fresh,fee_each=fee,target_net_pct=.5,
                target_ref=p*target_factor if p else None,stop_ref=min(x["l"] for x in recent),
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

def register_flow_price(app):
    app.register_blueprint(bp)

HTML = r"""<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>FLOW PRICE MINI V0.1</title><style>
*{box-sizing:border-box}body{margin:0;background:#111722;color:#e6ebf3;font:15px system-ui,sans-serif}main{max-width:760px;margin:auto;padding:16px}h1{font-size:23px;margin:8px 0}p,small{color:#aab8ca;line-height:1.55}button,select{font:inherit;color:#e6ebf3;background:#25334a;border:1px solid #43526b;border-radius:9px;padding:10px}button{cursor:pointer}button:disabled{opacity:.5}#go{background:#18734f;width:100%;margin:12px 0;font-weight:700}label{display:inline-flex;flex-direction:column;gap:5px;margin:5px 5px 5px 0}article{background:#1b2534;border:1px solid #34435b;border-radius:12px;padding:12px;margin:9px 0}article.buy{border-color:#39c986}h3{margin:0 0 7px}.good{color:#68e3a3}.bad{color:#9aa9bc}.warn{color:#ffb96a}.line{display:flex;justify-content:space-between;gap:8px}.tags{display:flex;flex-wrap:wrap;gap:7px;margin:8px 0}.tag{font-size:13px;border:1px solid #40506a;padding:5px 7px;border-radius:7px}progress{width:100%;accent-color:#36bd8b}details{margin:12px 0}#err{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#88b9ff}
</style><main><h1>FLOW PRICE MINI V0.1</h1><p>가격 구조 · VWAP · 실제 체결 · 근사 매물대<br>실시간 연구 전용 / 자동매매 없음</p>
<label>검색 범위<select id="limit"><option>10</option><option>20</option><option selected>30</option><option>60</option></select></label>
<label>24시간 거래대금<select id="minimum"><option value="0">제한 없음</option><option value="50000000">5천만원</option><option value="100000000" selected>1억원</option><option value="500000000">5억원</option></select></label>
<label>편도 수수료<select id="fee"><option value=".25">0.25%</option><option value=".04">0.04%</option></select></label>
<button id="go">검색 시작</button><progress id="progress" value="0" max="1"></progress><p id="status">검색 대기</p>
<button id="save" disabled>이번 검색 CSV</button> <button id="all" disabled>누적 CSV</button>
<label><select id="filter"><option value="all">전체 표시</option><option value="watch">관심 이상</option><option value="buy">BUY 후보만</option></select></label>
<div id="results"></div><details><summary>오류 내역</summary><p id="err"></p></details>
<details><summary>판정 기준 및 한계</summary><p>가격 구조: 최근 1분 마감봉의 저점 상승과 최근 3봉 고점 돌파. VWAP: 최근 60개 마감봉의 실제 거래대금/거래량, 과도한 이격 제외. 체결: 최근 60초 매수 체결대금 비중 58% 이상, 8건 이상, 60초 범위 확인 및 마지막 체결 12초 이내. 매물대: 최근 60개 마감봉 평균 체결가별 거래량을 0.25% 구간에 집계한 근사치. 가까운 상단 대량 거래 구간을 회피. 4/4와 가격 신선도 확인 시 BUY 후보. 순수익 0.5% 참고 목표가는 수수료만 반영하며 슬리피지 미반영. 누적 CSV는 브라우저에 저장되는 관측 기록이며 매매 성과가 아님. 실제 수익 검증은 별도 필요.</p></details></main>
<script>
const $=id=>document.getElementById(id);let rows=[],errors=[],busy=false,history=[];
try{history=JSON.parse(localStorage.getItem('flow_price_mini_v01')||'[]');if(!Array.isArray(history))history=[]}catch(e){}
const fmt=n=>n==null?'—':Number(n).toLocaleString('ko-KR',{maximumFractionDigits:8});
const node=(tag,text,cls)=>{let e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e};
function render(){const root=$('results');root.replaceChildren();let f=$('filter').value;let sorted=[...rows].sort((a,b)=>(b.status==='BUY 후보')-(a.status==='BUY 후보')||b.score-a.score);
 for(const r of sorted){if(f==='buy'&&r.status!=='BUY 후보'||f==='watch'&&!['BUY 후보','관심'].includes(r.status))continue;let el=document.createElement('article');if(r.status==='BUY 후보')el.className='buy';el.append(node('h3',r.name+' ('+r.market+') · '+r.score+'/4'));let line=document.createElement('div');line.className='line';line.append(node('strong',r.status,r.status==='BUY 후보'?'good':'warn'),node('span',fmt(r.price)+'원'));el.append(line);let tags=document.createElement('div');tags.className='tags';for(const [k,n] of [['pa','가격 구조'],['vwap_ok','VWAP'],['flow_ok','실제 체결'],['vp_ok','근사 매물대']])tags.append(node('span',(r[k]?'✓ ':'× ')+n,'tag '+(r[k]?'good':'bad')));el.append(tags);el.append(node('small','매수 체결 '+fmt(r.bid_pct)+'% · 60초 '+r.ticks+'건 · 범위 '+(r.coverage?'충분':'부족')));el.append(document.createElement('br'));el.append(node('small','VWAP '+fmt(r.vwap)+' · 상단 매물 '+fmt(r.resistance)+' · '+r.time_kst));if(r.status==='BUY 후보'){el.append(document.createElement('br'));el.append(node('small','참고 목표 '+fmt(r.target_ref)+' · 구조 저점 '+fmt(r.stop_ref)+' (자동 주문 없음)'))}root.append(el);}
 $('err').textContent=errors.join('\n');$('save').disabled=!rows.length&&!errors.length;$('all').disabled=!history.length;
}
async function fetchj(url,ms){const ac=new AbortController(),timer=setTimeout(()=>ac.abort(),ms);try{let r=await fetch(url,{signal:ac.signal,cache:'no-store'}),d=await r.json();if(!r.ok||d.error)throw Error(d.error||r.status);return d}finally{clearTimeout(timer)}}
$('filter').onchange=render;
$('go').onclick=async()=>{if(busy)return;busy=true;$('go').disabled=true;rows=[];errors=[];$('progress').value=0;render();$('status').textContent='종목 목록 조회 중';let done=0;
 try{const fee=$('fee').value,limit=$('limit').value,minimum=$('minimum').value;
 const d=await fetchj('/flow-price-mini/markets?limit='+limit+'&minimum='+minimum,55000),items=d.items;$('progress').max=items.length||1;$('status').textContent='0 / '+items.length;let i=0;
 async function worker(){while(i<items.length){let x=items[i++];try{let r=await fetchj('/flow-price-mini/coin?market='+encodeURIComponent(x.market)+'&fee='+fee,22000);rows.push({...x,...r})}catch(e){errors.push(x.market+': '+e.message);history.push({...x,version:'FLOW PRICE MINI V0.1',time_kst:new Date().toISOString(),status:'오류',error:e.message})}finally{done++;$('progress').value=done;$('status').textContent=done+' / '+items.length+' · BUY '+rows.filter(x=>x.status==='BUY 후보').length+' · 오류 '+errors.length;render()}}}
 await Promise.all([worker(),worker()]);history.push(...rows);history=history.slice(-5000);try{localStorage.setItem('flow_price_mini_v01',JSON.stringify(history))}catch(e){errors.push('브라우저 저장 한도: CSV로 저장해 주세요.')}$('status').textContent='검색 완료 · '+done+'종목 · BUY '+rows.filter(x=>x.status==='BUY 후보').length+' · 오류 '+errors.length+' · 전체 KRW '+d.total;
 }catch(e){errors.push('목록 조회: '+e.message);$('status').textContent='검색 실패: '+e.message}finally{busy=false;$('go').disabled=false;render()}};
function download(data,suffix){let cols=['version','time_kst','market','name','turnover','status','score','pa','vwap_ok','flow_ok','vp_ok','fresh','price','closed_price','vwap','resistance','breakout','coverage','coverage_seconds','ticks','last_trade_age','bid_pct','delta_krw','fee_each','target_net_pct','target_ref','stop_ref','error'];let q=v=>'"'+String(v??'').replace(/"/g,'""')+'"';let csv=[cols.join(','),...data.map(r=>cols.map(k=>q(r[k])).join(','))].join('\r\n');let url=URL.createObjectURL(new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8'}));let a=document.createElement('a');a.href=url;a.download='FLOW_PRICE_MINI_V0_1_'+suffix+'_'+new Date().toLocaleString('sv-SE',{timeZone:'Asia/Seoul'}).replace(/\D/g,'').slice(0,12)+'.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),3000)}
$('save').onclick=()=>download([...rows,...errors.map(error=>({version:'FLOW PRICE MINI V0.1',status:'오류',error}))],'SCAN');$('all').onclick=()=>download(history,'HISTORY');render();
</script></html>"""
