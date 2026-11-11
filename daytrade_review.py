"""V11.6 precision 4H trendline -> 5M entry experiment.

Only the /backtest-daytrade experiment page is replaced. app.py and the V2
main-page BUY logic are untouched. All KRW markets are considered, without
turnover or warning-list exclusions. This is an experimental signal, not an order.

Pass 1: fetch 80 completed 4-hour candles for *all* markets, confirm two
  pivot lows/highs, project each line by actual timestamps to the scan time.
Pass 2: fetch 80 completed 5-minute candles only when a valid 4H trendline
  is within +/-2% of the ticker snapshot; check bounce / confirmed breakout.
No look-ahead candles, future returns, or synthetic gap-fill candles are used.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from math import isfinite
from threading import Lock
import re
import time

from flask import jsonify, render_template_string, request

PAGE_SIZE = 40
MARKET_RE = re.compile(r"^KRW-[A-Z0-9]+$")
NEAR_4H_PCT = 2.0  # 4H first-pass filter, previously +/-6%.
WATCH_SUPPORT_PCT = 1.0  # Watch only close ABOVE an ascending 4H support.
WATCH_RESISTANCE_PCT = 1.2  # Watch near a descending 4H resistance.
SUPPORT_RECLAIM_PCT = 0.8  # Below support: separate caution, never BUY/WATCH.
MAX_BUY_SUPPORT_PCT = 1.5  # Avoid buying well above the 4H support.
MAX_BUY_BREAKOUT_PCT = 1.8  # Avoid chasing already extended resistance breaks.
FRESH_5M_MINUTES = 10.0
FRESH_4H_MINUTES = 480.0
MIN_4H_BARS = 35
MIN_5M_BARS = 35


def local_time(s, kst):
    t = datetime.fromisoformat(str(s))
    return t.replace(tzinfo=kst) if t.tzinfo is None else t.astimezone(kst)


def completed_candles(api_get, market, unit, count, now, kst):
    raw = api_get(f"/v1/candles/minutes/{unit}", {"market": market, "count": count})
    if not isinstance(raw, list):
        raise ValueError("캔들 응답 형식 오류")
    found = {}
    step = timedelta(minutes=unit)
    for c in raw:
        if not isinstance(c, dict):
            continue
        try:
            t = local_time(c["candle_date_time_kst"], kst)
            prices = [float(c[key]) for key in ("opening_price", "high_price", "low_price", "trade_price")]
            volume = float(c["candle_acc_trade_volume"])
            if not all(isfinite(v) and v > 0 for v in prices):
                continue
            if not isfinite(volume) or volume < 0:
                continue
            if prices[2] > min(prices[0], prices[3]) or prices[1] < max(prices[0], prices[3]):
                continue
            if t + step <= now:
                found[t] = c
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return sorted(found.items())


def pivots(ordered, field, kind):
    values = [float(r[field]) for _, r in ordered]
    if kind == "support":
        return [i for i in range(2, len(values) - 2)
                if values[i] <= min(values[i-2:i]) and values[i] < min(values[i+1:i+3])]
    return [i for i in range(2, len(values) - 2)
            if values[i] >= max(values[i-2:i]) and values[i] > max(values[i+1:i+3])]


def make_line(ordered, field, kind):
    ids = pivots(ordered, field, kind)
    if len(ids) < 2:
        return None
    i, j = ids[-2], ids[-1]
    a, b = ordered[i], ordered[j]
    p1, p2 = float(a[1][field]), float(b[1][field])
    t1, t2 = a[0].timestamp(), b[0].timestamp()
    if t2 <= t1:
        return None
    # Slopes are per elapsed second, NOT per candle index. Gaps cannot shift the line.
    return {"p1": p1, "p2": p2, "t1": t1, "t2": t2,
            "kind": kind, "direction": "상승" if p2 > p1 else "하락" if p2 < p1 else "수평",
            "time1": a[0].strftime("%m-%d %H:%M"),
            "time2": b[0].strftime("%m-%d %H:%M")}


def line_value(line, at):
    if not line:
        return None
    t1, t2 = float(line["t1"]), float(line["t2"])
    p1, p2 = float(line["p1"]), float(line["p2"])
    if t2 <= t1 or p1 <= 0 or p2 <= 0:
        return None
    value = p2 + ((p2-p1)/(t2-t1)) * (at.timestamp()-t2)
    return value if isfinite(value) and value > 0 else None


def analyze_4h(api_get, market, price, now, kst):
    ordered = completed_candles(api_get, market, 240, 80, now, kst)
    out = {"market": market, "price": price, "bars4h": len(ordered),
           "near": False, "support": None, "resistance": None, "reason": "", "last4h": None}
    if len(ordered) < MIN_4H_BARS:
        out["reason"] = "4시간봉 데이터 부족"
        return out
    out["last4h"] = ordered[-1][0].strftime("%m-%d %H:%M")
    out["age4h"] = round((now - (ordered[-1][0] + timedelta(hours=4))).total_seconds()/60, 1)
    if out["age4h"] > FRESH_4H_MINUTES:
        out["reason"] = "4시간봉 최근 데이터 지연"
        return out
    for kind, field in (("support", "low_price"), ("resistance", "high_price")):
        line = make_line(ordered, field, kind)
        v = line_value(line, now)
        if line and v:
            # Strategy only considers higher-low support or lower-high resistance.
            # Lines with the opposite slope are not actionable trendlines here.
            direction_ok = (line["direction"] in ("상승", "수평") if kind == "support"
                            else line["direction"] in ("하락", "수평"))
            if not direction_ok:
                continue
            line["now"] = round(v, 10)
            line["distance_pct"] = round((price / v - 1)*100, 3)
            out[kind] = line
    if not out["support"] and not out["resistance"]:
        out["reason"] = "유효한 상승 지지선·하락 저항선 없음"
    else:
        out["near"] = any(abs(x["distance_pct"]) <= NEAR_4H_PCT
                          for x in (out["support"], out["resistance"]) if x)
        out["reason"] = "5분봉 확인 대상" if out["near"] else "4시간 추세선과 거리 멂"
    return out


def five_minute_entry(api_get, htf, now, kst):
    market = htf["market"]
    rows = completed_candles(api_get, market, 5, 80, now, kst)
    out = {"market": market, "status": "NONE", "setup": "", "note": "",
           "price": None, "volume_ratio": None, "age_minutes": None,
           "bars5m": len(rows), "htf": htf}
    if len(rows) < MIN_5M_BARS:
        out.update(status="SKIP", note="5분봉 데이터 부족")
        return out
    t = [x[0] for x in rows]
    c = [float(x[1]["trade_price"]) for x in rows]
    o = [float(x[1]["opening_price"]) for x in rows]
    l = [float(x[1]["low_price"]) for x in rows]
    v = [float(x[1]["candle_acc_trade_volume"]) for x in rows]
    out["price"] = c[-1]
    out["signal_time"] = t[-1].strftime("%Y-%m-%d %H:%M")
    age = (now - (t[-1] + timedelta(minutes=5))).total_seconds()/60
    out["age_minutes"] = round(age, 1)
    baseline = sum(v[-21:-1])/20
    out["volume_ratio"] = round(v[-1]/baseline, 2) if baseline > 0 else None
    recent_continuous = all(b-a == timedelta(minutes=5) for a, b in zip(t[-21:-1], t[-20:]))
    if age > FRESH_5M_MINUTES or age < -0.5:
        freshness = "STALE"
    elif not recent_continuous:
        freshness = "GAP"
    else:
        freshness = "CURRENT"

    options = []
    for kind, label in (("support", "4시간 지지선"), ("resistance", "4시간 저항선")):
        line = htf.get(kind)
        if not line:
            continue
        # Re-evaluate the ORIGINAL 4H line at exact 5-minute candle timestamps.
        # Never build a fresh 5M trendline for entry decisions.
        levels = [line_value(line, ts) for ts in t[-5:]]
        if any(level is None for level in levels):
            continue
        closes = c[-5:]
        opens = o[-5:]
        lows = l[-5:]
        distance = (closes[-1] / levels[-1] - 1) * 100
        if kind == "support":
            # A recent near-touch must happen on either of the last two bars,
            # with no deep break below support. The most recent closed bar
            # confirms a bullish bounce, not a mere price proximity.
            touched = any(levels[k] * 0.990 <= lows[k] <= levels[k] * 1.004
                          for k in (-2, -1))
            holds = closes[-2] >= levels[-2] * 0.995
            bounce = (touched and holds and
                      closes[-1] >= levels[-1] * 1.001 and
                      closes[-1] > opens[-1] and closes[-1] > closes[-2] and
                      0.10 <= distance <= MAX_BUY_SUPPORT_PCT)
            if bounce:
                options.append(("BUY", "지지 반등 확인", distance, line, label))
            elif 0 <= distance <= WATCH_SUPPORT_PCT:
                options.append(("WATCH", "지지선 접근 · 반등 대기", distance, line, label))
            elif -SUPPORT_RECLAIM_PCT <= distance < 0:
                # A broken support is not a good WATCH. Preserve it separately
                # for reclaim observation; do not mistake proximity for a BUY.
                options.append(("RECLAIM", "지지선 아래 · 회복 대기", distance, line, label))
        else:
            # Recently crossed a 4H declining resistance and stayed above it
            # for two completed 5M closes. Do not buy a far-extended move.
            crossed = any(closes[k-1] <= levels[k-1] * 1.001 and
                          closes[k] > levels[k] * 1.003 for k in (-3, -2))
            held_above = (closes[-2] > levels[-2] * 1.002 and
                          closes[-1] > levels[-1] * 1.002)
            breakout = crossed and held_above and 0.20 <= distance <= MAX_BUY_BREAKOUT_PCT
            if breakout:
                options.append(("BUY", "저항 돌파 · 2봉 확인", distance, line, label))
            elif -WATCH_RESISTANCE_PCT <= distance <= 0:
                options.append(("WATCH", "저항선 접근 · 돌파 대기", distance, line, label))
            elif 0 < distance <= WATCH_RESISTANCE_PCT:
                options.append(("WATCH", "저항선 위 · 돌파 재확인 대기", distance, line, label))
    if not options:
        return out
    priority = {"BUY": 0, "WATCH": 1, "RECLAIM": 2}
    options.sort(key=lambda y: (priority[y[0]], abs(y[2])))
    status, setup, dist, line, label = options[0]
    out.update(status=status if freshness == "CURRENT" else freshness,
               original_signal=status, setup=setup,
               note=f"{label} · 5분봉 {setup}",
               distance_pct=round(dist, 2), line_price=round(line_value(line, t[-1]), 10),
               line_direction=line["direction"], source_kind=line["kind"],
               pivot1=f"{line['time1']} / {line['p1']:.8g}",
               pivot2=f"{line['time2']} / {line['p2']:.8g}")
    if freshness == "STALE":
        out["note"] += " · 최근 확정봉 오래됨"
    elif freshness == "GAP":
        out["note"] += " · 최근 21개 5분봉 공백"
    return out


PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.6 · 4시간 추세선 정밀 진입 실험</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#141c2f;--border:#30405d;--text:#edf3ff;--sub:#a8b9d3;--green:#4ade80;--yellow:#fbbf24}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1050px;margin:auto;padding:20px 14px 60px}h1{font-size:22px;margin:0 0 8px}h2{font-size:18px;margin:0 0 12px}
p{margin:5px 0 12px}.muted{font-size:13px;color:var(--sub)}.panel{background:var(--panel);border:1px solid var(--border);border-radius:15px;padding:17px;margin-top:14px}
button{background:#315de7;color:white;border:0;border-radius:9px;padding:12px 18px;font:inherit;font-weight:750;cursor:pointer}button:disabled{opacity:.5}a{color:#aecaff}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}.metric{border:1px solid var(--border);border-radius:10px;padding:10px;min-width:0}.metric b{display:block;font-size:23px}.buy{color:var(--green)}.watch{color:var(--yellow)}.subtle{color:#d8b4fe}
progress{width:100%;height:12px;margin-top:12px}#status{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:8px}.list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.item{background:#10182a;border:1px solid var(--border);padding:14px;border-radius:12px;overflow-wrap:anywhere}.item.buycard{border-color:#2a7954}.item.watchcard{border-color:#756027}.item h3{display:flex;justify-content:space-between;gap:8px;font-size:16px;margin:0 0 8px}.pill{font-size:12px;border-radius:7px;background:#1d314e;padding:2px 7px}.details{white-space:pre-line;font-size:13px}details summary{cursor:pointer;font-weight:750}#timing{margin-top:8px}
@media(max-width:680px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.list{grid-template-columns:1fr}}
</style></head><body><main>
<h1>📈 4시간 추세선 → 5분봉 진입 후보</h1>
<p class="muted">V11.6 정밀 진입 실험판 · KRW 전체 · 거래대금 제한 없음 · 유의종목 포함 · V2 메인 변경 없음</p>
<div class="panel"><button id="run">지금 전체 검색</button> <a href="/">기존 V2 메인</a>
<p class="muted">4시간봉의 상승 지지선(저점 두 개)·하락 저항선(고점 두 개)을 5분봉에 그대로 투영합니다. ±2% 이내 종목만 정밀 확인하며, 지지 반등·2봉 저항 돌파를 실험합니다. 거래량은 참고용입니다.</p>
<div class="metrics">
<div class="metric"><span class="muted">전체 / 4시간봉 검사</span><b id="all">—</b></div>
<div class="metric"><span class="muted">5분봉 정밀 확인</span><b id="near">—</b></div>
<div class="metric"><span class="muted">BUY</span><b class="buy" id="buys">—</b></div>
<div class="metric"><span class="muted">WATCH</span><b class="watch" id="watches">—</b></div>
<div class="metric"><span class="muted">지지 이탈 주의</span><b class="subtle" id="reclaims">—</b></div>
</div>
<progress id="progress" value="0" max="1"></progress>
<div id="status" class="muted">검색 버튼을 누르면 4시간봉 전체 검사부터 진행합니다.</div>
<div id="timing" class="muted"></div>
</div>
<section class="panel"><h2 class="buy">실험 BUY 후보 · 4시간 추세선 기준</h2><div class="list" id="buyList"><p class="muted">검색 전</p></div></section>
<section class="panel"><h2 class="watch">WATCH · 추세선 접근 후보</h2><div class="list" id="watchList"><p class="muted">검색 전</p></div></section>
<details class="panel" id="reclaimPanel"><summary class="subtle">⚠️ 지지선 이탈 · 회복 대기 <span id="reclaimCount"></span></summary><p class="muted">4시간 지지선 아래에 있어 지지가 아직 확인되지 않은 종목이야. 지지선을 다시 회복하기 전에는 BUY나 일반 WATCH로 취급하지 않아.</p><div id="reclaimList" class="list"></div></details>
<details class="panel" id="holdPanel"><summary class="subtle">⚠️ 신호 시간/캔들 공백 주의 <span id="holdCount"></span></summary><p class="muted">마지막 5분봉이 오래됐거나 최근 21개 봉이 연속되지 않은 후보야. 현재 BUY로 표시하지 않아.</p><div id="holdList" class="list"></div></details>
<p class="muted">실험용 기술 신호로 매수 권유나 수익 보장이 아니야. 고점·저점은 양옆 2개 확정 4시간봉으로 확인하고 미래 봉을 사용하지 않아. 4시간선 아래 지지 이탈과 미확인 돌파는 BUY가 아니며, 먼 가격은 추격 방지 목적으로 후보에서 제외해.</p>
</main><script>
const $=id=>document.getElementById(id);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const won=v=>Number(v).toLocaleString('ko-KR',{maximumFractionDigits:8})+'원';
const resultMap=new Map();let running=false;let showMore=false;
function entries(status){return [...resultMap.values()].filter(x=>status==='HOLD'?['STALE','GAP'].includes(x.status):x.status===status)
 .sort((a,b)=>Math.abs(Number(a.distance_pct||99))-Math.abs(Number(b.distance_pct||99))||a.market.localeCompare(b.market));}
function card(x){const h=x.htf||{},l=x.source_kind==='support'?h.support:h.resistance;
const lineText=l?`${l.kind==='support'?'4시간 저점→저점':'4시간 고점→고점'} (${esc(l.direction)})\n고점/저점 1: ${esc(x.pivot1)}\n고점/저점 2: ${esc(x.pivot2)}`:'';
const r=x.volume_ratio==null?'확인 불가':Number(x.volume_ratio).toFixed(2)+'배';
return `<article class="item ${x.status==='BUY'?'buycard':'watchcard'}"><h3><span>${esc(x.market)}</span><span class="pill ${x.status==='BUY'?'buy':'watch'}">${esc(x.status==='BUY'?'BUY':x.status==='WATCH'?'WATCH':x.status==='RECLAIM'?'회복 대기':'확인 보류')}</span></h3>`+
`<div class="details">${esc(x.setup)} · ${esc(x.note)}\n확정 5분봉 ${esc(x.signal_time)} · 종료 후 ${esc(x.age_minutes)}분\n5분봉 종가 ${won(x.price)} · 추세선 ${won(x.line_price)} · 거리 ${x.distance_pct>0?'+':''}${esc(x.distance_pct)}%\n거래량 ${esc(r)} (참고용)\n${lineText}</div></article>`;}
function paint(){const b=entries('BUY'),w=entries('WATCH'),r=entries('RECLAIM'),h=entries('HOLD');$('buys').textContent=b.length;$('watches').textContent=w.length;$('reclaims').textContent=r.length;$('holdCount').textContent=`(${h.length}개)`;$('reclaimCount').textContent=`(${r.length}개)`;
$('buyList').innerHTML=b.length?b.map(card).join(''):'<p class="muted">현재 확정 BUY 없음</p>';
$('watchList').innerHTML=w.length?(showMore?w:w.slice(0,20)).map(card).join('')+(w.length>20?`<button type="button" id="moreBtn">${showMore?'접기':`나머지 ${w.length-20}개 더 보기`}</button>`:''):'<p class="muted">현재 WATCH 없음</p>';
const more=$('moreBtn');if(more)more.addEventListener('click',()=>{showMore=!showMore;paint();});
$('reclaimList').innerHTML=$('reclaimPanel').open?(r.length?r.map(card).join(''):'<p class="muted">지지선 회복 대기 후보 없음</p>'):'';
$('holdList').innerHTML=$('holdPanel').open?(h.length?h.map(card).join(''):'<p class="muted">확인 보류 없음</p>'):'';}
async function post(path,items){const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),75000);
try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items}),signal:controller.signal,cache:'no-store'});
const j=await r.json();if(!r.ok||!j.ok)throw Error(j.error||`HTTP ${r.status}`);return j;}finally{clearTimeout(timer);}}
async function pool(packs,limit,fn){let idx=0;const errs=[];async function worker(){while(idx<packs.length){const i=idx++;try{await fn(packs[i]);}catch(e){errs.push(e.message||String(e));}}}
await Promise.all(Array.from({length:Math.min(limit,packs.length)},worker));return errs;}
async function scan(){if(running)return;running=true;$('run').disabled=true;resultMap.clear();showMore=false;paint();$('all').textContent='—';$('near').textContent='—';$('timing').textContent='';$('progress').value=0;
const started=performance.now();let lineErrors=0,entryErrors=0;try{
const resp=await fetch('/api/line45/universe',{cache:'no-store'});const u=await resp.json();if(!resp.ok||!u.ok)throw Error(u.error||'시장 목록 조회 실패');
const items=u.items;const packs=[];for(let i=0;i<items.length;i+=40)packs.push(items.slice(i,i+40));
$('progress').max=items.length;$('all').textContent=`0 / ${items.length}`;const near=[];let scanned=0,valid=0,noLines=0;
$('status').textContent='1단계 · 전체 4시간봉 고점·저점 추세선 분석 중…';
const errors1=await pool(packs,3,async batch=>{const r=await post('/api/line45/lines',batch);
scanned+=r.scanned;lineErrors+=r.errors;for(const x of r.results){if(x.near)near.push(x);if(x.support||x.resistance)valid++;else noLines++;}
$('all').textContent=`${scanned} / ${items.length}`;$('progress').value=scanned;
$('status').textContent=`1단계 · 4시간봉 ${scanned}/${items.length}개 · 추세선 있는 종목 ${valid}개 · 5분봉 확인 대상 ${near.length}개`;});
const t1=((performance.now()-started)/1000).toFixed(1);if(errors1.length||lineErrors)throw Error(`4시간봉 일부 조회 실패: ${lineErrors}개 / 요청 ${errors1.length}건. 불완전한 검사이므로 결과 확정하지 않음`);
$('near').textContent=near.length;$('status').textContent=`2단계 · 4시간선 근처 ${near.length}개 종목의 5분봉 반등·돌파 확인 중…`;
const packs2=[];for(let i=0;i<near.length;i+=40)packs2.push(near.slice(i,i+40));let checked=0,skipped=0;
const errors2=await pool(packs2,3,async batch=>{const r=await post('/api/line45/entries',batch);checked+=r.scanned;entryErrors+=r.errors;skipped+=r.skipped;
for(const x of r.results){if(['BUY','WATCH','RECLAIM','GAP','STALE'].includes(x.status))resultMap.set(x.market,x);}paint();
$('status').textContent=`2단계 · 5분봉 ${checked}/${near.length}개 · BUY ${entries('BUY').length} · WATCH ${entries('WATCH').length} · 지지 이탈 ${entries('RECLAIM').length}`;});
paint();const total=((performance.now()-started)/1000).toFixed(1);
$('timing').textContent=`4시간봉 전체 ${t1}초 · 5분봉 포함 전체 ${total}초 · 시작 ${esc(u.snapshot_at)} · 추세선 없음 ${noLines}개 · 5분봉 부족 ${skipped}개`;
$('status').textContent=(errors2.length||entryErrors?'⚠️ 일부 실패 · ':'✅ 완료 · ')+`전체 ${scanned}/${items.length} · 5분봉 확인 ${checked}/${near.length} · BUY ${entries('BUY').length} · WATCH ${entries('WATCH').length} · 지지 이탈 ${entries('RECLAIM').length} · 시간/공백 보류 ${entries('HOLD').length} · 오류 ${lineErrors+entryErrors+errors2.length}건`;
}catch(e){$('status').textContent='⚠️ 검색 중단: '+e.message+' · 표시된 내용은 불완전할 수 있어.';}
finally{running=false;$('run').disabled=false;}}
$('run').addEventListener('click',scan);$('holdPanel').addEventListener('toggle',paint);$('reclaimPanel').addEventListener('toggle',paint);
</script></body></html>'''


def register_daytrade_review(app, deps):
    api_get, get_markets, kst = deps["api_get"], deps["get_markets"], deps["KST"]
    universe_cache = {"at": 0, "data": None}
    universe_lock = Lock()

    @app.get("/backtest-daytrade")
    def line45_page():
        return render_template_string(PAGE)

    @app.get("/api/line45/universe")
    def line45_universe():
        try:
            with universe_lock:
                if universe_cache["data"] and time.time() - universe_cache["at"] < 30:
                    return jsonify(universe_cache["data"])
                markets = sorted({x for x in get_markets()
                                  if isinstance(x, str) and MARKET_RE.fullmatch(x)})
                if not markets:
                    raise ValueError("KRW 코인 목록이 비었습니다")
                ticker_data = {}
                market_set = set(markets)
                for i in range(0, len(markets), 50):
                    raw = api_get("/v1/ticker", {"markets": ",".join(markets[i:i+50])})
                    if not isinstance(raw, list):
                        raise ValueError("시세 데이터 형식 오류")
                    for x in raw:
                        if isinstance(x, dict) and x.get("market") in market_set:
                            ticker_data[x["market"]] = x
                if len(ticker_data) != len(markets):
                    raise ValueError("전체 KRW 시세가 누락되어 검사를 시작하지 않았습니다")
                items = []
                for market in markets:
                    try:
                        price = float(ticker_data[market]["trade_price"])
                    except (KeyError, ValueError, TypeError):
                        raise ValueError(market+" 현재가 확인 불가")
                    if not isfinite(price) or price <= 0:
                        raise ValueError(market+" 현재가 오류")
                    items.append({"market": market, "price": price})
                data = {"ok": True, "items": items, "total": len(items),
                        "snapshot_at": datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S KST")}
                universe_cache.update(at=time.time(), data=data)
                return jsonify(data)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:160]}), 502

    @app.post("/api/line45/lines")
    def line45_lines():
        payload = request.get_json(silent=True)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not 1 <= len(items) <= PAGE_SIZE:
            return jsonify({"ok": False, "error": "요청 코인 수는 1~40개여야 합니다"}), 400
        try:
            names = [x.get("market") for x in items if isinstance(x, dict)]
            if len(names) != len(items) or len(set(names)) != len(names) or any(
                    not isinstance(m, str) or not MARKET_RE.fullmatch(m) for m in names):
                raise ValueError("코인 목록 형식 오류")
            results = []
            errors = 0
            now = datetime.now(kst)
            with ThreadPoolExecutor(max_workers=5) as pool:
                tasks = {pool.submit(analyze_4h, api_get, x["market"], float(x["price"]), now, kst): x["market"]
                         for x in items}
                for f in as_completed(tasks):
                    market = tasks[f]
                    try:
                        results.append(f.result())
                    except Exception as e:
                        errors += 1
                        results.append({"market": market, "near": False, "error": type(e).__name__})
            return jsonify({"ok": True, "scanned": len(items), "errors": errors, "results": results})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:160]}), 400

    @app.post("/api/line45/entries")
    def line45_entries():
        payload = request.get_json(silent=True)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not 1 <= len(items) <= PAGE_SIZE:
            return jsonify({"ok": False, "error": "요청 코인 수는 1~40개여야 합니다"}), 400
        try:
            names = [x.get("market") for x in items if isinstance(x, dict)]
            if len(names) != len(items) or len(set(names)) != len(names) or any(
                    not isinstance(m, str) or not MARKET_RE.fullmatch(m) for m in names):
                raise ValueError("코인 목록 형식 오류")
            for x in items:
                if not x.get("near") or not isinstance(x.get("price"), (float, int)):
                    raise ValueError("4시간 추세선 자료가 없습니다")
                for kind in ("support", "resistance"):
                    z = x.get(kind)
                    if z is not None and not all(key in z for key in ("t1", "t2", "p1", "p2", "kind")):
                        raise ValueError("추세선 정보 형식 오류")
            now = datetime.now(kst)
            results = []
            errors = 0
            skipped = 0
            with ThreadPoolExecutor(max_workers=5) as pool:
                tasks = {pool.submit(five_minute_entry, api_get, x, now, kst): x["market"] for x in items}
                for f in as_completed(tasks):
                    market = tasks[f]
                    try:
                        x = f.result()
                        results.append(x)
                        skipped += int(x["status"] == "SKIP")
                    except Exception as e:
                        errors += 1
                        results.append({"market": market, "status": "ERROR", "error": type(e).__name__})
            return jsonify({"ok": True, "scanned": len(items), "skipped": skipped,
                            "errors": errors, "results": results})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:160]}), 400
