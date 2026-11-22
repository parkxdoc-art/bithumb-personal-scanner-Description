"""CORE.17 SPLIT 70/30 V1.0: reconstruct two-stage exits from audited paired OHLC replays.

First 70% at NET +1%, remaining 30% at NET +3/+5/+10%.
Runs on already-simulated 1% and 3% 30/30/40 exit legs with identical
entry, continuous 5m candles, stop rule and time limit.
When neither baseline path is fully resolved, result remains UNKNOWN.
No orders; no mutation of original scan/exit scenarios.
"""

VERSION = "CORE.17 SPLIT70/30 V1.0"
SECOND_TARGETS = (3, 5, 10)
STOPS = ("NONE", "SL1", "SL2", "STRUCT")


def _done(x):
    return bool(isinstance(x, dict) and x.get("status") == "RESOLVED"
                and isinstance(x.get("net_pct"), (int, float)))


def _leg(x, kind):
    return next((leg for leg in (x.get("exit_legs") or [])
                 if leg.get("kind") == kind), None)


def _unknown(reason):
    return {"status": "UNKNOWN", "outcome": "UNKNOWN", "net_pct": None,
            "reason": reason, "first_filled": False, "second_filled": False,
            "stop_triggered": False, "first_min": None, "second_min": None,
            "minutes": None, "exit_legs": []}


def reconstruct(first, third, target):
    """Exact matching-case reconstruction under stop-priority fill rules.

    +3% derived from 3%-first arm; +5%/+10% from 1%-first arm.
    If target not met, its unmatched remainder uses STOP/TIME leg.
    """
    if target not in SECOND_TARGETS:
        raise ValueError("Second target must be 3, 5 or 10")
    if not (_done(first) and _done(third)):
        return _unknown("1% 및 3% 기준 비교군의 완결된 경로가 필요")
    l1 = _leg(first, "TP1")
    l3 = _leg(third, "TP1")
    if l3 and not l1:
        raise AssertionError("3% 도달 후 1% 미도달 모순")
    if not l1:
        # No first target: whole position exits at the same STOP/TIME.
        final = _leg(first, "STOP") or _leg(first, "TIME")
        if final is None:
            raise AssertionError("Missing terminal leg without TP1")
        return {"status": "RESOLVED", "outcome": first.get("outcome"),
                "net_pct": round(float(first["net_pct"]), 6),
                "reason": "", "first_filled": False, "second_filled": False,
                "stop_triggered": final["kind"] == "STOP", "first_min": None,
                "second_min": None, "minutes": first.get("minutes"),
                "exit_legs": [dict(final, fraction=1.0,
                          weighted_net_pct=round(float(final["net_pct"]), 6))]}
    candidate = third if target == 3 else first
    target_kind = "TP1" if target == 3 else "TP5" if target == 5 else "TP10"
    second = _leg(candidate, target_kind)
    terminal = second or _leg(candidate, "STOP") or _leg(candidate, "TIME")
    if terminal is None:
        raise AssertionError("No target or final STOP/TIME leg")
    first_net = float(l1["net_pct"])
    second_net = float(terminal["net_pct"])
    net = round(.70 * first_net + .30 * second_net, 6)
    first_min = l1.get("minute")
    second_min = second.get("minute") if second else None
    return {
        "status": "RESOLVED",
        "outcome": "TP2" if second else ("STOP" if terminal["kind"] == "STOP" else "TIME"),
        "net_pct": net, "reason": "",
        "first_filled": True, "second_filled": bool(second),
        "stop_triggered": terminal["kind"] == "STOP",
        "first_min": first_min, "second_min": second_min,
        "minutes": terminal.get("minute"),
        "exit_legs": [
            dict(l1, fraction=.70, weighted_net_pct=round(.70 * first_net, 6)),
            dict(terminal, kind="TP2" if second else terminal["kind"],
                 fraction=.30, weighted_net_pct=round(.30 * second_net, 6))
        ]
    }


def compare(stop_compare):
    result = {}
    for stop in STOPS:
        v1 = (stop_compare or {}).get("1", {}).get(stop)
        v3 = (stop_compare or {}).get("3", {}).get(stop)
        result[stop] = {str(t): reconstruct(v1, v3, t)
                        for t in SECOND_TARGETS}
    return result


PANEL = """<section class="panel" id="split7030-panel">
<h2>CORE.17 SPLIT70/30 V1.0 · 수익 +1% 70% 선확정</h2>
<div class="muted">원본 <b>B_10M_MA03 진입</b>과 기존 스캔 신호 유지. 매도 1차: <b>수수료 차감 순익 +1%에 70%</b>, 2차: <b>순익 +3%, +5%, +10% 중 하나에 남은 30% 전량</b>. 240분 만료 잔량 청산. 4가지 손절(없음/-1%/-2%/신호 당시 6봉 저점-0.2%)은 진입부터 잔량 청산까지 적용. 가격 기준 -1%/-2%는 수수료 차감 전이며 실제 순손익에는 수수료 반영. 본절 보호 및 자동매매 없음.</div>
<div class="muted">모든 12개 방식이 완료된 <b>동일 진입 신호</b>만 집계. 손절과 익절이 동일 5분봉에 닿으면 보수적으로 손절 우선, 가격 갭은 불리한 시가 체결. 기존 1%·3% 청산 경로의 실제 기록을 활용하므로 가상의 캔들을 만들지 않음. 수수료 기본 0.04%, 슬리피지 기본 0%, 하루 최대 4회 제한 미적용.</div>
<p id="split7030-coverage">검사 전</p>
<div class="scroll"><table><thead><tr><th>2차 익절</th><th>손절</th><th>동일 거래</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익</th><th>최대 손실</th><th>1차 도달</th><th>2차 도달</th></tr></thead><tbody id="split7030-rows"><tr><td colspan="10">검사 전</td></tr></tbody></table></div>
<button id="split7030-csv" disabled>70·30 비교 CSV 받기</button>
</section>"""

JS = r"""<script>
(()=>{
 const stops=['NONE','SL1','SL2','STRUCT'],targets=['3','5','10'];
 const stable=new Set('USDT USDC DAI TUSD FDUSD USD1 PYUSD USDE USDD RLUSD EURC EURI'.split(' '));
 const done=o=>o?.status==='RESOLVED' && o.net_pct!==null && o.net_pct!==undefined && Number.isFinite(Number(o.net_pct));
 const all=()=>responses.flatMap(r=>(r.core17_focus?.events||[]).map(e=>({date:r.date,e}))).filter(x=>!stable.has(String(x.e.market||'').replace(/^KRW-/,'')));
 const paired=a=>a.filter(({e})=>stops.every(st=>targets.every(t=>done(e.split7030_compare?.[st]?.[t]))));
 const fmt=x=>Number.isFinite(x)?x.toFixed(3)+'%':'—';
 const base=paintCore17;
 paintCore17=function(){
  base();
  const a=all(),common=paired(a);
  document.getElementById('split7030-coverage').textContent='전체 '+a.length+'건 / 12가지 동일 검증 '+common.length+'건 / 제외 '+(a.length-common.length)+'건';
  document.getElementById('split7030-rows').innerHTML=stops.flatMap(st=>targets.map(t=>{
    const v=common.map(({e})=>e.split7030_compare[st][t]),n=v.length,net=v.map(o=>Number(o.net_pct)),wins=net.filter(x=>x>0).length,losses=net.filter(x=>x<0).length;
    const cols=[t+'%',({NONE:'손절 없음',SL1:'-1%',SL2:'-2%',STRUCT:'구조적'})[st],n,wins,losses,fmt(n?wins/n*100:NaN),fmt(n?net.reduce((s,x)=>s+x,0)/n:NaN),fmt(n?Math.min(...net):NaN),v.filter(o=>o.first_filled).length,v.filter(o=>o.second_filled).length];
    return '<tr>'+cols.map(x=>'<td>'+x+'</td>').join('')+'</tr>';
  })).join('');
  document.getElementById('split7030-csv').disabled=!a.length;
 };
 const quote=x=>'"'+String(x??'').replace(/"/g,'""')+'"';
 document.getElementById('split7030-csv').onclick=()=>{
  const a=all(),cols=['version','date','market','base_id','signal_time_kst','entry_time_kst','fee_pct','slippage_pct','first_target_net_pct','first_fraction','second_target_net_pct','second_fraction','stop_type','common_12_resolved','status','outcome','net_pct','reason','minutes','first_filled','second_filled','stop_triggered','exit_legs'],rows=[cols.join(',')];
  for(const {date,e} of a){const common=stops.every(st=>targets.every(t=>done(e.split7030_compare?.[st]?.[t])));for(const st of stops)for(const t of targets){
   const o=e.split7030_compare?.[st]?.[t]||{},values=['CORE17 SPLIT70/30 V1.0',date,e.market,e.base_id,e.signal_time_kst,e.entry_time_kst,e.tp_compare_fee,e.tp_compare_slip,1,.7,t,.3,st,common,o.status,o.outcome,o.net_pct,o.reason,o.minutes,o.first_filled,o.second_filled,o.stop_triggered,JSON.stringify(o.exit_legs||[])];
   rows.push(values.map(quote).join(','));
  }}
  const blob=new Blob(['\ufeff'+rows.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),downloadLink=document.createElement('a');downloadLink.href=url;downloadLink.download='BITHUMB_CORE17_SPLIT7030_V1_0_COMPARE.csv';downloadLink.click();setTimeout(()=>URL.revokeObjectURL(url),3000);
 };
})();
</script>"""


def make_page(stop_page):
    page = stop_page.replace("<title>CORE.17 SL V1.0 · 손절 비교</title>",
                             "<title>CORE.17 SPLIT70/30 V1.0 · 2분할 비교</title>",1)
    page = page.replace("<h1>🧪 CORE.17 SL V1.0 · 익절 1%/3% × 손절 4종</h1>",
                        "<h1>🧪 CORE.17 SPLIT70/30 V1.0 · 1% 70% · 잔량 30% 연구</h1>",1)
    marker='<section class="panel" id="slcompare-panel">'
    if marker not in page:
        raise AssertionError("SL panel missing")
    page=page.replace(marker,PANEL+marker,1)
    if "</body></html>" not in page:
        raise AssertionError("Closing body missing")
    return page.replace("</body></html>",JS+"</body></html>",1)
