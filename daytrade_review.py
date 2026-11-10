"""Add-on day-trading A/B backtest dashboard. No changes to live BUY signals."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from math import isfinite
from threading import Lock
import re
import time

from flask import jsonify, render_template_string, request

DAYS = ("2026-11-01", "2026-11-04", "2026-11-07", "2026-11-08")
HOURS = (1, 3, 6, 12, 24)
LABELS = tuple(f"{h}h" for h in HOURS)
EMPTY = {k: 0 for k in LABELS}
LIQUIDITY_MIN_KRW = 500_000_000
MARKET_PATTERN = re.compile(r"^KRW-[A-Z0-9]+$")

PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>단타 A/B 백테스트 | 빗썸 스캐너</title>
<style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,sans-serif}
*{box-sizing:border-box}body{margin:0;background:#0b1020;color:#edf3ff;padding:16px}
main{max-width:1040px;margin:auto}h1{font-size:23px}h2{font-size:17px;margin:0 0 12px}
p,small{color:#a9b7d2;line-height:1.6}a{color:#92b7ff}
section{background:#141c2f;border:1px solid #30415f;border-radius:14px;padding:15px;margin:14px 0}
.controls{display:flex;flex-wrap:wrap;gap:12px;align-items:end}
.field{display:flex;flex-direction:column;gap:5px;min-width:110px;flex:1}
label{font-size:13px;color:#a9b7d2}select,button{font-size:15px;padding:10px;border-radius:8px;border:1px solid #384d71}
select{background:#0b1426;color:white}button{background:#315de7;color:white;cursor:pointer;font-weight:bold;min-width:120px}
button:disabled{opacity:.55;cursor:default}
.summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.tile{padding:12px;border:1px solid #344562;border-radius:10px;background:#111b31}
.tile b{font-size:22px}.tile small{display:block;font-size:12px}
.tablewrap{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:620px}
tr{border-bottom:1px solid #293750}td,th{text-align:left;padding:9px;font-size:12px;white-space:nowrap}th{color:#afbdd8}
strong.green{color:#5ee0aa}strong.yellow{color:#ffd77d}
.warn{color:#ffd77d}.error{color:#ff9aa6}#status{min-height:30px}
progress{width:100%;height:14px;accent-color:#54b89c}ul{padding-left:20px}
@media(min-width:750px){.summary{grid-template-columns:repeat(4,minmax(0,1fr))}}
</style></head><body><main>
<a href="/">← 빗썸 실시간 스캐너로 돌아가기</a>
<h1>📊 단타 A/B 검증판</h1>
<p>기존 BUY(A)와 피보나치(B)를 같은 과거 5분봉에서 비교합니다. 실시간 주문 기능은 없습니다.</p>
<section><h2>백테스트 설정</h2>
<div class="controls">
<div class="field"><label for="day">검사일 (KST)</label><select id="day">
<option>2026-11-08</option><option>2026-11-07</option><option>2026-11-04</option><option>2026-11-01</option>
</select></div>
<div class="field"><label for="limit">코인 수</label><select id="limit">
<option value="10">10개</option><option value="30" selected>30개</option><option value="50">50개</option>
</select></div>
<div class="field"><label for="fee">매수·매도 각각 수수료</label><select id="fee">
<option value="0.25" selected>0.25%</option><option value="0.04">0.04%</option><option value="0">0%</option>
</select></div>
<button id="start" type="button">A/B 분석 시작</button>
</div>
<small>현재 상장된 KRW 코인을 현재 24시간 거래대금 순으로 선택합니다. 과거 시점의 유니버스 순위가 아니므로 선택 편향이 있습니다. 과거 거래대금 필터는 신호 시점 직전 24시간 5억 원 이상입니다.</small>
<p id="status" role="status">설정을 고르고 분석 시작을 누르세요.</p>
<progress id="progress" max="1" value="0"></progress>
</section>
<section><h2>전략 비교</h2>
<div class="summary">
<div class="tile"><small>조회 완료 코인</small><b id="count">0</b></div>
<div class="tile"><small>유효 검증 구간</small><b id="windows">0</b></div>
<div class="tile"><small>A 기존 BUY</small><b id="aCount">0</b></div>
<div class="tile"><small>B 피보나치 BUY</small><b id="bCount">0</b></div>
</div>
<div class="tablewrap"><table><thead><tr><th>전략</th><th>시간</th><th>표본 수</th><th>평균 순수익률</th><th>수익 비율</th></tr></thead><tbody id="metrics"></tbody></table></div>
<small>신호 직후 다음 봉 시가 매수 → 지정 시간 뒤 종가 매도. 설정 수수료 반영, 슬리피지·세금·실제 체결 영향 제외. 목표/손절을 적용한 실거래 성과는 아닙니다.</small>
</section>
<section><h2>진단: BUY가 없는 이유 찾기</h2>
<div id="diag" class="warn">결과 대기 중</div>
</section>
<section><h2>코인별 결과</h2><div class="tablewrap"><table><thead><tr><th>코인</th><th>유효 구간</th><th>A BUY</th><th>B BUY</th><th>상태</th></tr></thead><tbody id="coinRows"></tbody></table></div></section>
<section><h2>발생 신호 예시</h2><div id="events">결과 대기 중</div></section>
<section><h2>검증 범위에 대한 주의</h2><p>
하루 중 00:00~12:00에 확인된 신호를 최대 24시간까지 추적합니다. 과거 24시간 연속 거래대금과 이후 24시간 연속 5분봉이 확보된 경우만 평가합니다. 거래가 드문 코인은 유효 구간 0개로 표시됩니다. 1일/30개 결과만으로 수익성을 증명할 수 없으며, A/B 신호가 0개면 성공률도 계산하지 않습니다.
</p></section>
</main><script>
const $ = id => document.getElementById(id);
const horizons = ['1h','3h','6h','12h','24h'];
let running = false;
const fmt = x => x == null ? '—' : Number(x).toLocaleString('ko-KR');
const pct = x => x == null ? '—' : Number(x).toFixed(2) + '%';
function paint(all, done, total, rankInfo) {
  const complete = all.filter(x => !x.error);
  const count = complete.length;
  const windows = complete.reduce((s,x)=>s + (x.windows||0),0);
  $('count').textContent = fmt(count) + ' / ' + fmt(total) + ' (유효 ' + fmt(complete.filter(x=>x.windows>0).length) + ')';
  $('windows').textContent = fmt(windows);
  const rows = [];
  const agg = {};
  for(const strategy of ['A','B']) {
    let signals=0;
    let totals={};
    for(const key of horizons) totals[key]={n:0,sum:0,wins:0};
    for(const row of complete) {
      const x = row.strategies[strategy];
      signals += x.signals;
      for(const key of horizons){
        totals[key].n += x.samples[key];
        totals[key].sum += x.return_sums[key];
        totals[key].wins += x.positive[key];
      }
    }
    agg[strategy] = signals;
    horizons.forEach((key,i) => {
      const v=totals[key];
      rows.push(`<tr><td>${i?'':strategy==='A'?'A 기존 BUY':'B 피보나치'}</td><td>${key}</td><td>${fmt(v.n)}</td><td>${pct(v.n?v.sum/v.n:null)}</td><td>${pct(v.n?100*v.wins/v.n:null)}</td></tr>`);
    });
  }
  $('aCount').textContent=fmt(agg.A);
  $('bCount').textContent=fmt(agg.B);
  $('metrics').innerHTML=rows.join('');
  const diag={aBreakout:0,aSupport:0,bTrend:0,bTouch:0,bBounce:0,bVolume:0,bAllPrice:0,bSwing25:0,bEvaluated:0,liquidity:0,excluded:0};
  for(const x of complete){for(const k in diag) diag[k] += x.diagnostics[k] || 0;}
  $('diag').textContent = `과거 24h 5억 통과 구간 ${fmt(diag.liquidity)}개 / 전체 ${fmt(windows)}개 · A 돌파 확인 ${fmt(diag.aBreakout)}개 / 지지 유지 ${fmt(diag.aSupport)}개 · B 선행 상승폭 2.5% 통과 ${fmt(diag.bSwing25)}개 / B 상세 조건까지 도달 ${fmt(diag.bEvaluated)}개 · 상세 진단: 추세 ${fmt(diag.bTrend)}개 / 피보나치 접촉 ${fmt(diag.bTouch)}개 / 반등 ${fmt(diag.bBounce)}개 / 거래량 ${fmt(diag.bVolume)}개 / 4조건 동시 충족 ${fmt(diag.bAllPrice)}개. (각 조건은 독립 집계입니다.)`;
  $('coinRows').innerHTML = all.map(x => `<tr><td>${x.market}</td><td>${fmt(x.windows||0)}</td><td>${x.error?'—':fmt(x.strategies.A.signals)}</td><td>${x.error?'—':fmt(x.strategies.B.signals)}</td><td>${x.error?'오류: '+String(x.error).slice(0,65):(x.windows?'정상':(x.coverage_reason||'검증 구간 없음'))}</td></tr>`).join('');
  const ev=[];
  for(const x of complete) for(const strategy of ['A','B']) for(const e of x.strategies[strategy].examples){
    ev.push(`${strategy} · ${e.market} · ${e.time} · 1h ${pct(e.net['1h'])} / 24h ${pct(e.net['24h'])}`);
  }
  $('events').textContent=ev.length?ev.slice(0,30).join('\n'):'아직 BUY 신호 예시가 없습니다.';
  $('events').style.whiteSpace='pre-line';
  $('progress').max=total||1;$('progress').value=done;
}
async function startBacktest(){
  if(running)return;
  running=true;$('start').disabled=true;
  $('coinRows').innerHTML='';
  const all=[];
  const date=$('day').value, limit=Number($('limit').value), fee=$('fee').value;
  try {
    $('status').textContent='KRW 코인 거래대금 순위 조회 중…';
    const url='/api/daytrade-universe?limit='+limit;
    const uResp=await fetch(url,{cache:'no-store'});
    const u=await uResp.json();
    if(!uResp.ok || !u.ok)throw new Error(u.error||'마켓 목록 오류');
    const markets=u.markets;
    $('status').textContent=`현재 KRW ${u.available_markets}개 중 상위 ${markets.length}개 검사 예정`;
    for(let i=0;i<markets.length;i+=5){
      const batch=markets.slice(i,i+5);
      $('status').textContent=`검사 중 · ${i}/${markets.length}개 완료 (${batch.join(', ')})`;
      const qs=new URLSearchParams({date,markets:batch.join(','),fee});
      const resp=await fetch('/api/daytrade-batch?'+qs.toString(),{cache:'no-store'});
      const r=await resp.json();
      if(!resp.ok||!r.ok)throw new Error(r.error||'분석 요청 실패');
      all.push(...r.results);
      paint(all,all.length,markets.length,u);
    }
    const errors=all.filter(x=>x.error).length;
    const valid=all.filter(x=>!x.error && x.windows>0).length;
    $('status').textContent=`완료 · ${all.length}개 조회, 유효 코인 ${valid}개, ${errors}개 조회/계산 오류. A와 B를 비교하세요.`;
  } catch(e) {
    $('status').textContent='⚠️ 중단: '+e.message+' · 이미 받은 결과는 아래에 남겨두었습니다.';
  } finally {
    running=false;$('start').disabled=false;
  }
}
$('start').addEventListener('click',startBacktest);
paint([],0,1,{});
</script></body></html>"""


def register_daytrade_review(app, deps):
    """Register three additive routes using existing app.py helpers."""
    api_get = deps["api_get"]
    get_markets = deps["get_markets"]
    completed_rows = deps["backtest_completed_rows"]
    make_windows = deps["make_backtest_windows"]
    replay_a = deps["backtest_replay_signal"]
    replay_b = deps["backtest_fibonacci_signal"]
    kst = deps["KST"]
    cache = {"at": 0, "ranked": []}
    cache_lock = Lock()

    def get_ranked():
        with cache_lock:
            if cache["ranked"] and time.time() - cache["at"] < 300:
                return cache["ranked"][:]
            markets = sorted(set(get_markets()))
            tickers = {}
            for pos in range(0, len(markets), 50):
                raw = api_get("/v1/ticker", {"markets": ",".join(markets[pos:pos + 50])})
                if not isinstance(raw, list):
                    raise ValueError("ticker 응답 형식이 올바르지 않습니다")
                for x in raw:
                    market = x.get("market")
                    if market in markets:
                        try:
                            amount = float(x.get("acc_trade_price_24h", 0))
                            if isfinite(amount) and amount >= 0:
                                tickers[market] = amount
                        except (TypeError, ValueError):
                            pass
            ranked = sorted(tickers, key=lambda m: (-tickers[m], m))
            if not ranked:
                raise ValueError("현재 거래대금 순위를 가져오지 못했습니다")
            cache.update(at=time.time(), ranked=ranked)
            return ranked[:]

    def fetch_history(market, cutoff):
        by_time = {}
        cursor = cutoff
        for _ in range(4):
            page = completed_rows(market, 5, cursor, count=200)
            if not page:
                break
            for row in page:
                by_time[row["candle_date_time_kst"]] = row
            oldest = datetime.fromisoformat(page[0]["candle_date_time_kst"])
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=kst)
            else:
                oldest = oldest.astimezone(kst)
            if oldest >= cursor:
                raise ValueError("캔들 페이지 시간순서 오류")
            cursor = oldest
        return [by_time[t] for t in sorted(by_time)]

    def empty_strategy():
        return {"signals": 0, "samples": EMPTY.copy(),
                "return_sums": EMPTY.copy(), "positive": EMPTY.copy(),
                "examples": []}

    def evaluate_market(market, date, fee):
        output = {
            "market": market, "windows": 0, "coverage_reason": "", "history_rows": 0,
            "strategies": {"A": empty_strategy(), "B": empty_strategy()},
            "diagnostics": {name: 0 for name in (
                "aBreakout", "aSupport", "bTrend", "bTouch", "bBounce",
                "bVolume", "bAllPrice", "bSwing25", "bEvaluated", "liquidity", "excluded")},
        }
        try:
            day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=kst)
            start = day
            end = day + timedelta(hours=12)
            cutoff = day + timedelta(days=1, hours=12)
            rows = fetch_history(market, cutoff)
            output["history_rows"] = len(rows)
            # Diagnose historical coverage without altering any BUY criteria.
            run = longest_run = 0
            previous = None
            for candle in rows:
                stamp = datetime.fromisoformat(candle["candle_date_time_kst"])
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=kst)
                else:
                    stamp = stamp.astimezone(kst)
                run = run + 1 if previous is not None and stamp - previous == timedelta(minutes=5) else 1
                longest_run = max(longest_run, run)
                previous = stamp
            if len(rows) < 576:
                output["coverage_reason"] = f"원본 {len(rows)}봉 · 최장 연속 {longest_run}봉 (최소 576봉 필요)"
                return output
            # 288 historical 5-minute candles + 288 strictly future candles.
            # Independent strategy replays only see the LAST 80 historical candles.
            windows = make_windows(rows, minute=5, lookback=288, forward_bars=288)
            last_event = {"A": None, "B": None}
            for w in windows:
                at = w["cutoff_kst"].astimezone(kst)
                if not (start <= at < end):
                    continue
                history = w["signal_rows"]
                future = w["evaluation_rows"]
                if len(history) != 288 or len(future) != 288:
                    continue
                output["windows"] += 1
                value24 = sum(float(c["candle_acc_trade_price"]) for c in history)
                if not isfinite(value24) or value24 < 0:
                    raise ValueError("거래대금 데이터 오류")
                liquid = value24 >= LIQUIDITY_MIN_KRW
                output["diagnostics"]["liquidity"] += int(liquid)
                # Match B's existing 2.5% swing precondition on its last 80 bars.
                # This is diagnostic only: no change to A or B signal decisions.
                h80 = [float(c["high_price"]) for c in history[-80:]]
                l80 = [float(c["low_price"]) for c in history[-80:]]
                peak_idx = max(range(45, 77), key=lambda i: h80[i])
                low_idx = min(range(peak_idx - 30, peak_idx), key=lambda i: l80[i])
                swing_pass = l80[low_idx] > 0 and h80[peak_idx] / l80[low_idx] - 1 >= 0.025
                output["diagnostics"]["bSwing25"] += int(swing_pass)
                input_window = {
                    "cutoff_kst": w["cutoff_kst"],
                    "signal_rows": history[-80:],
                    "evaluation_rows": [],
                }
                a = replay_a(input_window)
                b = replay_b(input_window)
                if not isinstance(a, dict) or not isinstance(b, dict):
                    continue
                diagnostics = output["diagnostics"]
                diagnostics["bEvaluated"] += int("trend_ok" in b)
                diagnostics["aBreakout"] += int(a.get("breakout_seen") is True)
                diagnostics["aSupport"] += int(a.get("support_held") is True)
                labels = ("trend_ok", "touch_ok", "bounce_ok", "volume_ok")
                for source, name in zip(labels, ("bTrend", "bTouch", "bBounce", "bVolume")):
                    diagnostics[name] += int(b.get(source) is True)
                b_core = all(b.get(field) is True for field in labels)
                diagnostics["bAllPrice"] += int(b_core)
                # Previous B implementation used an incorrect 80-bar liquidity
                # criterion. Apply the same verified 288-bar criterion to A and B.
                signals = {"A": a.get("signal") == "BUY" and liquid,
                           "B": b_core and liquid}
                if not liquid and (a.get("signal") == "BUY" or b_core):
                    diagnostics["excluded"] += 1
                for strategy, matched in signals.items():
                    if not matched:
                        continue
                    prev = last_event[strategy]
                    if prev is not None and at - prev < timedelta(minutes=60):
                        continue
                    entry = float(future[0]["opening_price"])
                    if entry <= 0 or not isfinite(entry):
                        continue
                    result = output["strategies"][strategy]
                    net = {}
                    for h in HOURS:
                        exit_price = float(future[12 * h - 1]["trade_price"])
                        if exit_price <= 0 or not isfinite(exit_price):
                            raise ValueError("평가 가격 오류")
                        # Buy fee is paid in addition to acquired principal.
                        # Sell fee reduces proceeds. Fee is one-side %.
                        ret = ((exit_price / entry) * (1 - fee) / (1 + fee) - 1) * 100
                        key = f"{h}h"
                        result["samples"][key] += 1
                        result["return_sums"][key] += ret
                        result["positive"][key] += int(ret > 0)
                        net[key] = round(ret, 3)
                    result["signals"] += 1
                    last_event[strategy] = at
                    if len(result["examples"]) < 3:
                        result["examples"].append({
                            "market": market, "time": at.strftime("%m-%d %H:%M"),
                            "entry": entry, "net": net,
                        })
            if output["windows"] == 0:
                output["coverage_reason"] = (f"원본 {len(rows)}봉 · 최장 연속 {longest_run}봉, "
                                             f"선택일 00~12시의 유효 구간 없음")
            return output
        except Exception as exc:
            output["error"] = f"{type(exc).__name__}: {exc}"[:180]
            output["windows"] = 0
            output["strategies"] = {"A": empty_strategy(), "B": empty_strategy()}
            return output

    @app.get("/backtest-daytrade")
    def daytrade_review_page():
        return render_template_string(PAGE)

    @app.get("/api/daytrade-universe")
    def daytrade_review_universe():
        try:
            limit = int(request.args.get("limit", "30"))
            if limit not in (10, 30, 50):
                return jsonify({"ok": False, "error": "코인 수가 잘못되었습니다"}), 400
            markets = get_ranked()
            return jsonify({"ok": True, "markets": markets[:limit],
                            "available_markets": len(markets),
                            "selection": "현재 시점 24시간 거래대금 순위 (과거 순위 아님)"})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)[:160]}), 502

    @app.get("/api/daytrade-batch")
    def daytrade_review_batch():
        date = request.args.get("date", "2026-11-08")
        if date not in DAYS:
            return jsonify({"ok": False, "error": "지원하지 않는 날짜입니다"}), 400
        names = request.args.get("markets", "").split(",")
        if not 1 <= len(names) <= 5 or len(set(names)) != len(names) or not all(
                MARKET_PATTERN.fullmatch(name) for name in names):
            return jsonify({"ok": False, "error": "코인은 1~5개만 선택할 수 있습니다"}), 400
        try:
            fee_percent = float(request.args.get("fee", "0.25"))
            if fee_percent not in (0.0, 0.04, 0.25):
                raise ValueError("지원하지 않는 수수료입니다")
            fee = fee_percent / 100
            # Validate against the exchange's listed KRW universe.
            allowed = set(get_markets())
            if not set(names).issubset(allowed):
                raise ValueError("현재 상장되지 않은 KRW 마켓이 포함되었습니다")
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(evaluate_market, name, date, fee): name
                           for name in names}
                result_by_market = {}
                for future in as_completed(futures):
                    result_by_market[futures[future]] = future.result()
            return jsonify({"ok": True, "date": date,
                            "results": [result_by_market[name] for name in names],
                            "caveats": ["당일 00~12시 진입, 최대 24시간 추적",
                                        "과거 시점 24h 거래대금 5억 이상",
                                        "현재 시점 거래대금으로 선정한 코인들"]})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)[:160]}), 502
