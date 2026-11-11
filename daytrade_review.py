"""V11.1 live chart-first candidate board for the Bithumb V2 scanner.

All listed KRW markets are checked regardless of 24h trading value or warnings.
V2's completed 5m-candle BUY/WATCH decisions are unchanged. Its 24h turnover
score bonus is removed in THIS board using the existing calculate_score helper.
Only BUY/WATCH candidates receive an additional completed 24h-candle context.
No historical backtest dates, future returns, or auto orders are used.
The existing V2 dashboard and browser snapshots remain untouched.
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

PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>빗썸 · 실시간 24시간 BUY / 관심 후보</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#141c2f;--border:#2b3855;--text:#eef3ff;--sub:#a9b8d4;--buy:#4ade80;--watch:#fbbf24}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1020px;margin:auto;padding:20px 14px 60px}h1{font-size:22px;margin:0 0 6px}h2{font-size:18px;margin:0 0 14px}
p{margin:4px 0 12px}.muted{font-size:13px;color:var(--sub)}.panel{border:1px solid var(--border);border-radius:15px;background:var(--panel);padding:17px;margin-top:16px}
button{padding:12px 18px;border-radius:9px;background:#315de7;border:0;color:white;font-size:15px;font-weight:750;cursor:pointer}
button:disabled{opacity:.55;cursor:default}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.metric{border:1px solid var(--border);border-radius:10px;padding:12px}.metric b{display:block;font-size:23px}.buy{color:var(--buy)}.watch{color:var(--watch)}
.progress{width:100%;height:11px;margin-top:10px}#status{margin-top:10px;white-space:pre-wrap;overflow-wrap:anywhere}
.list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.item{border:1px solid var(--border);border-radius:12px;padding:14px;background:#10182a}
.item.buycard{border-color:#28664c}.item.watchcard{border-color:#665527}.item h3{display:flex;justify-content:space-between;gap:10px;align-items:center;font-size:17px;margin:0 0 8px}.pill{border-radius:7px;padding:3px 7px;font-size:12px;background:#1e3351}
.item .details{font-size:13px;white-space:pre-line}.item .context{margin-top:10px;padding-top:9px;border-top:1px solid var(--border);font-size:12px;white-space:pre-line;color:var(--sub)}
section{margin-top:18px}.empty{font-size:13px;color:var(--sub)}.foot{font-size:12px;color:var(--sub);margin-top:18px}
a{color:#a9caff}@media(max-width:650px){.list{grid-template-columns:1fr}.metric b{font-size:20px}h1{font-size:20px}}
</style></head><body><main>
<h1>⚡ 빗썸 실시간 BUY · 관심 후보</h1>
<p class="muted">V11.1 · 검사 시점 기준 · KRW 전체 코인 · 거래대금 제한 없음 · V2 확정 5분봉 신호 · 후보의 직전 24시간 확인</p>
<div class="panel"><button id="run" type="button">지금 전체 검색</button> <a href="/">V2 메인 화면</a>
<p class="muted">기존 V2 BUY/WATCH 차트 조건을 유지합니다. 거래대금으로 제외하거나 점수에 가산하지 않습니다. 거래유의종목도 자동 제외하지 않습니다.</p>
<div class="metrics">
  <div class="metric"><span class="muted">전체 / 검사 대상</span><b id="all">—</b></div>
  <div class="metric"><span class="muted">BUY</span><b id="buys" class="buy">—</b></div>
  <div class="metric"><span class="muted">관심 WATCH</span><b id="watches" class="watch">—</b></div>
</div>
<progress class="progress" id="progress" max="1" value="0"></progress>
<div class="muted" id="status" role="status" aria-live="polite">검색 버튼을 누르면 거래대금과 관계없이 전체 KRW 코인의 차트 신호를 확인합니다.</div>
<div class="muted" id="timing"></div>
</div>
<section class="panel"><h2 class="buy">BUY · 진입 조건 충족</h2><div class="list" id="buyList"><div class="empty">아직 검사하지 않았습니다.</div></div></section>
<section class="panel"><h2 class="watch">WATCH · 추가 확인 필요</h2><div class="list" id="watchList"><div class="empty">아직 검사하지 않았습니다.</div></div></section>
<p class="foot">매매 권유나 주문 기능이 아닙니다. BUY는 V2 기술 조건 충족을 뜻하며 수익을 보장하지 않습니다. 거래대금 제한이 없으므로 유동성이 낮은 종목은 호가 차이·체결 지연에 특히 주의하세요. 직전 24시간 캔들 분석은 보조 정보로, BUY 판단을 소급 변경하지 않습니다. 24시간 자료는 완료된 5분봉만 사용합니다.</p>
</main><script>
const $ = id => document.getElementById(id);
const quote = x => String(x ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const won = v => Number(v).toLocaleString('ko-KR',{maximumFractionDigits:6})+'원';
let working=false;
const ctxByMarket=new Map();
const signalByMarket=new Map();
function sorted(kind){return [...signalByMarket.values()].filter(x=>x.signal===kind).sort((a,b)=>Number(b.score||0)-Number(a.score||0)||Number(b.vol_ratio||0)-Number(a.vol_ratio||0)||a.market.localeCompare(b.market));}
function paint(){
  const buy=sorted('BUY'),watch=sorted('WATCH');
  $('buys').textContent=buy.length;$('watches').textContent=watch.length;
  function card(x){
    const c=ctxByMarket.get(x.market),p=Number(x.price),r=Number(x.vol_ratio);
    const context=!c?'⏳ 직전 24시간 5분봉 확인 대기':c.error?'⚠️ 24시간 조회 실패: '+quote(c.error):
      `직전 24시간 완성 5분봉 ${c.bars}/288개 (${c.coverage_pct}%)`+
      `\n24시간 고가 ${won(c.high)} · 저가 ${won(c.low)}`+
      `\n24시간 시가→종가 ${c.change_pct>0?'+':''}${c.change_pct}%`+
      `\n마지막 봉 ${quote(c.last_candle)} · ${c.fresh?'정상':'⚠️ 최근 봉 지연/누락'}`+
      (c.complete?'':'\n⚠️ 24시간 연속 봉 부족 — 보조 분석만 표시');
    return `<article class="item ${x.signal==='BUY'?'buycard':'watchcard'}"><h3><span>${quote(x.market)}</span><span class="pill ${x.signal==='BUY'?'buy':'watch'}">${x.signal}</span></h3>`+
      `<div class="details">확정 5분봉 종가 ${won(p)}`+
      `\n거래량 ${Number.isFinite(r)?r.toFixed(2)+'배':'—'} · 점수 ${Number(x.score||0)}`+
      `\n확정 봉 ${quote(x.candle_time)} · ${quote(x.resistance)}`+
      `\n${quote(x.note)}</div><div class="context">${context}</div></article>`;
  }
  $('buyList').innerHTML=buy.length?buy.map(card).join(''):'<div class="empty">현재 BUY 없음</div>';
  $('watchList').innerHTML=watch.length?watch.map(card).join(''):'<div class="empty">현재 WATCH 없음</div>';
}
async function post(path,payload,ms=70000){
  const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),ms);
  try{
    const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:controller.signal,cache:'no-store'});
    const j=await r.json();if(!r.ok||!j.ok)throw new Error(j.error||`HTTP ${r.status}`);return j;
  }finally{clearTimeout(timer);}
}
async function pool(batches,concurrency,fn){
  let next=0,errors=[];
  async function worker(){while(next<batches.length){const i=next++;try{await fn(batches[i],i);}catch(e){errors.push(e);}}}
  await Promise.all(Array.from({length:Math.min(concurrency,batches.length)},worker));return errors;
}
async function scan(){
 if(working)return;working=true;$('run').disabled=true;
 signalByMarket.clear();ctxByMarket.clear();paint();$('all').textContent='—';$('timing').textContent='';
 $('status').textContent='전체 KRW 코인 목록 조회 중…';$('progress').value=0;$('progress').max=1;
 const started=performance.now();let failed=0,contextFailures=0;
 try{
  const res=await fetch('/api/live24/universe',{cache:'no-store'});
  const u=await res.json();if(!res.ok||!u.ok)throw new Error(u.error||'전체 코인 목록 조회 실패');
  const items=u.eligible;$('all').textContent=`${u.total} / ${items.length}`;
  const packs=[];for(let i=0;i<items.length;i+=40)packs.push(items.slice(i,i+40));
  let scanned=0,evaluated=0,skipped=0;$('progress').max=Math.max(items.length,1);
  const errs=await pool(packs,3,async(batch)=>{
    const r=await post('/api/live24/scan',{items:batch});
    scanned+=r.scanned;evaluated+=r.evaluated;skipped+=r.skipped;failed+=r.errors;
    for(const x of r.candidates)signalByMarket.set(x.market,x);
    $('progress').value=scanned;
    $('status').textContent=`V2 차트 검색 ${scanned}/${items.length}개 · 차트 유효 ${evaluated} · 봉 부족 ${skipped} · BUY ${sorted('BUY').length} · WATCH ${sorted('WATCH').length}`;
    paint();
  });
  const secs=((performance.now()-started)/1000).toFixed(1);
  $('timing').textContent=`전체 차트 1차 검색 ${secs}초 · 검사 ${scanned}/${items.length}개 · 차트 유효 ${evaluated}개 · 봉 부족 ${skipped}개 · 검사 시작 ${quote(u.snapshot_at)} · 서버 오류 ${failed}건 · 묶음 오류 ${errs.length}건`;
  if(errs.length)$('status').textContent=`⚠️ 일부 묶음 검색 실패: ${errs[0].message} · 남은 결과만 표시 중`;
  else $('status').textContent=`✅ 1차 후보 검색 완료 · BUY ${sorted('BUY').length}개 · WATCH ${sorted('WATCH').length}개 · 직전 24시간 캔들 확인 중…`;
  const picks=[...sorted('BUY'),...sorted('WATCH')];
  const blocks=[];for(let i=0;i<picks.length;i+=5)blocks.push(picks.slice(i,i+5).map(x=>x.market));
  let checked=0;
  const ctxErrs=await pool(blocks,2,async markets=>{
    const r=await post('/api/live24/context',{markets},70000);
    for(const x of r.results){ctxByMarket.set(x.market,x);if(x.error)contextFailures++;}
    checked+=markets.length;paint();
    $('status').textContent=`✅ BUY/WATCH 검색 완료 · 24시간 캔들 확인 ${checked}/${picks.length}개`;
  });
  const totalSec=((performance.now()-started)/1000).toFixed(1);
  $('status').textContent=ctxErrs.length||contextFailures||failed||errs.length?
    `⚠️ 일부 자료 확인 실패 · BUY ${sorted('BUY').length}개 · WATCH ${sorted('WATCH').length}개 · 원본 오류 ${failed}, 묶음 오류 ${errs.length}, 24시간 오류 ${ctxErrs.length+contextFailures}`:
    `✅ 완료 · 전체 ${scanned}/${items.length}개 검사 · 유효 ${evaluated}개 · 봉 부족 ${skipped}개 · BUY ${sorted('BUY').length}개 · WATCH ${sorted('WATCH').length}개 · 24시간 캔들 확인 ${checked}개`;
  $('timing').textContent+=` · 24시간 확인 포함 전체 ${totalSec}초`;
 }catch(e){$('status').textContent='⚠️ 검색 중단: '+e.message+' · 이미 확인된 후보는 화면에 유지됩니다.';}
 finally{working=false;$('run').disabled=false;}
}
$('run').addEventListener('click',scan);
</script></body></html>'''


def register_daytrade_review(app, deps):
    """Drop-in replacement: app.py still imports register_daytrade_review."""
    api_get = deps["api_get"]
    get_markets = deps["get_markets"]
    scan_market = deps["scan_market"]
    calculate_score = deps["calculate_score"]  # Board score without turnover bonus.
    kst = deps["KST"]
    ticker_cache = {"at": 0.0, "markets": None, "eligible": None, "total": 0, "snapshot_at": ""}
    ticker_lock = Lock()

    @app.get("/backtest-daytrade")
    def live24_review_page():
        return render_template_string(PAGE)

    @app.get("/api/live24/universe")
    def live24_universe():
        try:
            # The full ticker snapshot is refreshed once, then frozen for the run.
            with ticker_lock:
                if ticker_cache["eligible"] is not None and time.time() - ticker_cache["at"] < 30:
                    return jsonify({"ok": True, "total": ticker_cache["total"],
                                    "eligible": ticker_cache["eligible"],
                                    "snapshot_at": ticker_cache["snapshot_at"]})
                markets = sorted({m for m in get_markets() if MARKET_RE.fullmatch(m)})
                if not markets:
                    raise ValueError("KRW 거래 목록이 비어 있습니다")
                tickers = {}
                for start in range(0, len(markets), 50):
                    raw = api_get("/v1/ticker", {"markets": ",".join(markets[start:start+50])})
                    if not isinstance(raw, list):
                        raise ValueError("24시간 티커 데이터가 올바르지 않습니다")
                    for t in raw:
                        if isinstance(t, dict) and t.get("market") in markets:
                            tickers[t["market"]] = t
                if len(tickers) != len(markets):
                    raise ValueError(f"티커 누락 {len(markets)-len(tickers)}개: 전체 검색을 시작하지 않았습니다")
                eligible = []
                for market in markets:
                    t = tickers[market]
                    try:
                        value = float(t.get("acc_trade_price_24h"))
                        change = float(t.get("signed_change_rate", 0))
                    except (TypeError, ValueError):
                        raise ValueError(f"{market} 티커 숫자 데이터 오류")
                    if not isfinite(value) or value < 0 or not isfinite(change):
                        raise ValueError(f"{market} 티커 숫자 데이터 오류")
                    eligible.append({"market": market,
                                     "ticker": {"market": market, "acc_trade_price_24h": value,
                                                "signed_change_rate": change}})
                # Do not rank scan batches by trading value; check every market.
                eligible.sort(key=lambda x: x["market"])
                snapshot_at = datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S KST")
                ticker_cache.update(at=time.time(), markets=markets, eligible=eligible,
                                    total=len(markets), snapshot_at=snapshot_at)
                return jsonify({"ok": True, "total": len(markets),
                                "eligible": eligible, "snapshot_at": snapshot_at})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)[:160]}), 502

    @app.post("/api/live24/scan")
    def live24_scan_batch():
        try:
            payload = request.get_json(silent=True)
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list) or not 1 <= len(items) <= PAGE_SIZE:
                raise ValueError("한 번에 1~40개씩 요청하세요")
            names = []
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("ticker"), dict):
                    raise ValueError("티커 목록 형식 오류")
                market = item.get("market")
                t = item["ticker"]
                if (not isinstance(market, str) or not MARKET_RE.fullmatch(market)
                        or t.get("market") != market):
                    raise ValueError("코인 이름 오류")
                try:
                    val = float(t.get("acc_trade_price_24h", 0))
                    chg = float(t.get("signed_change_rate", 0))
                except (TypeError, ValueError):
                    raise ValueError("티커 수치 오류")
                if not isfinite(val) or val < 0 or not isfinite(chg):
                    raise ValueError("티커 수치 오류")
                names.append(market)
            if len(set(names)) != len(names):
                raise ValueError("중복 코인")

            candidates = []
            errors = 0
            evaluated = 0
            skipped = 0
            with ThreadPoolExecutor(max_workers=5) as executor:
                tasks = [executor.submit(scan_market, x["market"], 5, 1.5, 2.0, x["ticker"])
                         for x in items]
                for task in as_completed(tasks):
                    try:
                        result = task.result()
                        if result is None:
                            # A low-trade coin can have fewer than 35 completed
                            # candles. Do not misreport that as a chart pass.
                            skipped += 1
                            continue
                        evaluated += 1
                        if result.get("signal") in ("BUY", "WATCH"):
                            # V2's score rewards turnover; this board ranks on
                            # price/MA5/chart volume/signal ONLY. Signal itself
                            # stays exactly as returned by the V2 scanner.
                            result["score"] = calculate_score(
                                result.get("change", 0), result.get("ma5_info"),
                                result.get("vol_ratio"), None,
                                result.get("signal"), 2.0)
                            candidates.append(result)
                    except Exception:
                        errors += 1
            return jsonify({"ok": True, "scanned": len(items),
                            "evaluated": evaluated, "skipped": skipped,
                            "errors": errors, "candidates": candidates})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)[:160]}), 400

    def context_one(market, anchor):
        # Anchor to last *completed* 5-minute candle, never today's forming candle.
        begin = anchor - timedelta(hours=24)
        raw_by_time = {}
        cursor = None
        for page in range(2):
            params = {"market": market, "count": 200 if page == 0 else 120}
            if cursor:
                params["to"] = cursor
            raw = api_get("/v1/candles/minutes/5", params)
            if not isinstance(raw, list):
                raise ValueError("5분봉 응답 형식 오류")
            if not raw:
                break
            earliest = None
            for row in raw:
                if not isinstance(row, dict):
                    continue
                stamp = datetime.fromisoformat(row["candle_date_time_kst"])
                stamp = stamp.replace(tzinfo=kst) if stamp.tzinfo is None else stamp.astimezone(kst)
                earliest = stamp if earliest is None or stamp < earliest else earliest
                if begin <= stamp < anchor:
                    raw_by_time[stamp] = row
            if earliest is None or earliest < begin:
                break
            cursor = earliest.strftime("%Y-%m-%dT%H:%M:%S")
        times = sorted(raw_by_time)
        if not times:
            return {"market": market, "error": "직전 24시간 완성된 5분봉 없음"}
        bars = [raw_by_time[t] for t in times]
        high = max(float(r["high_price"]) for r in bars)
        low = min(float(r["low_price"]) for r in bars)
        opening = float(bars[0]["opening_price"])
        closing = float(bars[-1]["trade_price"])
        if not all(isfinite(v) and v > 0 for v in (high, low, opening, closing)):
            raise ValueError("캔들 가격 오류")
        last_end = times[-1] + timedelta(minutes=5)
        complete = len(times) == 288 and times[0] == begin and last_end == anchor
        fresh = (anchor-last_end) <= timedelta(minutes=10)
        return {"market": market, "bars": len(times),
                "coverage_pct": round(100*len(times)/288, 1),
                "high": high, "low": low, "change_pct": round(100*(closing/opening-1), 2),
                "last_candle": times[-1].strftime("%m-%d %H:%M"),
                "complete": complete, "fresh": fresh}

    @app.post("/api/live24/context")
    def live24_context():
        try:
            payload = request.get_json(silent=True)
            markets = payload.get("markets") if isinstance(payload, dict) else None
            if (not isinstance(markets, list) or not 1 <= len(markets) <= 5
                    or any(not isinstance(x, str) or not MARKET_RE.fullmatch(x) for x in markets)
                    or len(set(markets)) != len(markets)):
                raise ValueError("1~5개 코인을 지정하세요")
            now = datetime.now(kst)
            anchor = now.replace(minute=(now.minute//5)*5, second=0, microsecond=0)
            results = []
            with ThreadPoolExecutor(max_workers=4) as pool:
                tasks = {pool.submit(context_one, m, anchor): m for m in markets}
                for future in as_completed(tasks):
                    market = tasks[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append({"market": market, "error": type(exc).__name__})
            return jsonify({"ok": True, "results": results})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)[:160]}), 400
