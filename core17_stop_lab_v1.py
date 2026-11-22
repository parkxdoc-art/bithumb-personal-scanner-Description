"""CORE.17 SL V1.0 research extension: matched TP1 1%/3% x stop 0/1/2/structure.

Only completed five-minute candles up through signal_i define structural stop.
No exchange orders. No effect on original six CORE17 scenario outputs.
"""
from datetime import timedelta
from math import isfinite

VERSION = 'CORE.17 SL V1.0'
TP1 = (1, 3)
STOPS = ('NONE', 'SL1', 'SL2', 'STRUCT')
PARTS = (.30, .30, .40)
HOLD_BARS = 48


def prior_six_structure(rows5, signal_i):
    """Prior 6 *completed* five-minute candles, including the signal candle.

    No future candles, and no synthetic data across holes. Buffer -0.2%.
    """
    if signal_i is None or signal_i < 5 or signal_i >= len(rows5):
        return None
    chunk = rows5[signal_i - 5: signal_i + 1]
    if any(chunk[j]['t'] - chunk[j - 1]['t'] != timedelta(minutes=5)
           for j in range(1, len(chunk))):
        return None
    lows = [float(x['l']) for x in chunk]
    if not all(isfinite(v) and v > 0 for v in lows):
        return None
    return min(lows) * 0.998


def simulate(rows5, signal_i, cfg, first_target_net_pct, stop_kind):
    """Research-only 30/30/40; targets NET +first/+5/+10%, stop GROSS.

    For an OHLC candle that touches stop AND target, stop takes precedence.
    Fixed stop remains active after partial TP. Gaps fill at worse open.
    """
    if first_target_net_pct not in TP1 or stop_kind not in STOPS:
        raise ValueError('invalid experiment option')
    out = dict(status='UNKNOWN', outcome='UNKNOWN', net_pct=None, reason='',
               minutes=None, stop_price=None, stop_distance_pct=None,
               stop_triggered=False, ambiguous_stop_target=False,
               tp1_filled=False, tp5_filled=False, tp10_filled=False,
               tp1_min=None, tp5_min=None, tp10_min=None,
               remaining_fraction=1., exit_legs=[])

    def unknown(message):
        out['reason'] = message
        return out

    if signal_i is None or signal_i < 0 or signal_i + 1 >= len(rows5):
        return unknown('진입 다음 5분봉 부족')
    step = timedelta(minutes=5)
    if rows5[signal_i + 1]['t'] - rows5[signal_i]['t'] != step:
        return unknown('진입 직전 5분봉 공백')
    entry = float(rows5[signal_i + 1]['o'])
    if not isfinite(entry) or entry <= 0:
        return unknown('진입 시가 오류')
    fee = (float(cfg['fee']) + float(cfg['slip'])) / 100.
    if not 0 <= fee < .01:
        return unknown('수수료 설정 오류')
    targets = [entry * (1 + p / 100.) * (1 + fee) / (1 - fee)
               for p in (first_target_net_pct, 5., 10.)]
    if stop_kind == 'SL1':
        stop = entry * .99
    elif stop_kind == 'SL2':
        stop = entry * .98
    elif stop_kind == 'STRUCT':
        stop = prior_six_structure(rows5, signal_i)
    else:
        stop = None
    if stop_kind != 'NONE':
        if stop is None or not isfinite(stop) or not 0 < stop < entry:
            return unknown('구조적 손절가 무효 또는 진입가 이상')
        out['stop_price'] = round(stop, 12)
        out['stop_distance_pct'] = round(100. * (entry - stop) / entry, 6)
    remaining = 1.
    realized = 0.
    stage = 0
    last = signal_i

    def add_leg(kind, fraction, price, minute, candle):
        nonlocal remaining, realized
        net = (price * (1 - fee) / (entry * (1 + fee)) - 1) * 100
        realized += fraction * net
        remaining = max(0., round(remaining - fraction, 10))
        out['exit_legs'].append(dict(kind=kind, fraction=fraction,
                exit_price=round(price, 12), net_pct=round(net, 6),
                weighted_net_pct=round(net * fraction, 6), minute=minute,
                time_kst=candle.strftime('%Y-%m-%d %H:%M')))
        out['remaining_fraction'] = remaining
        if kind == 'TP1':
            out['tp1_filled'] = True
            out['tp1_min'] = minute
        elif kind == 'TP5':
            out['tp5_filled'] = True
            out['tp5_min'] = minute
        elif kind == 'TP10':
            out['tp10_filled'] = True
            out['tp10_min'] = minute
        elif kind == 'STOP':
            out['stop_triggered'] = True

    def finish(kind, minute):
        out.update(status='RESOLVED', outcome=kind, net_pct=round(realized, 6),
                   minutes=minute, reason='')
        return out

    for k in range(1, HOLD_BARS + 1):
        j = signal_i + k
        if j >= len(rows5):
            return unknown('청산 전 미래 5분봉 부족')
        bar = rows5[j]
        if bar['t'] - rows5[last]['t'] != step:
            return unknown('청산 전 미래 5분봉 공백')
        last = j
        high, low, op, close = (float(bar[x]) for x in ('h', 'l', 'o', 'c'))
        if not all(isfinite(v) and v > 0 for v in (high, low, op, close)):
            return unknown('미래 봉 가격 오류')
        minute = 5 * k
        tol = max(entry, 1) * 1e-12
        if stop is not None and low <= stop + tol:
            if stage < 3 and high + tol >= targets[stage]:
                out['ambiguous_stop_target'] = True
            add_leg('STOP', remaining, min(op, stop), minute, bar['t'])
            return finish('STOP', minute)
        if stage == 0 and high + tol >= targets[0]:
            add_leg('TP1', PARTS[0], targets[0], minute, bar['t'])
            stage = 1
        if stage == 1 and high + tol >= targets[1]:
            add_leg('TP5', PARTS[1], targets[1], minute, bar['t'])
            stage = 2
        if stage == 2 and high + tol >= targets[2]:
            add_leg('TP10', PARTS[2], targets[2], minute, bar['t'])
            stage = 3
            return finish('TP10', minute)
        if k == HOLD_BARS:
            if remaining > 0:
                add_leg('TIME', remaining, close, minute, bar['t'])
            return finish('TIME', minute)
    return unknown('판정 불가')


def compare(rows5, signal_i, cfg, tp_compare):
    """Reuse already-computed no-stop baseline to preserve CORE.17 TP results."""
    return {str(t): {
        'NONE': tp_compare.get(str(t), dict(status='UNKNOWN', net_pct=None,
                                            reason='기존 TP 기준 없음')),
        'SL1': simulate(rows5, signal_i, cfg, t, 'SL1'),
        'SL2': simulate(rows5, signal_i, cfg, t, 'SL2'),
        'STRUCT': simulate(rows5, signal_i, cfg, t, 'STRUCT')
    } for t in TP1}

PANEL = '''<section class="panel" id="slcompare-panel">
<h2>CORE.17 SL V1.0 · 1% vs 3% 익절 × 손절 4종</h2>
<div class="muted">원본 B_10M_MA03 진입은 그대로. 1차 순익 +1% 또는 +3%(30% 매도), 2차 순익 +5%(30%), 3차 순익 +10%(40%). 매도 목표는 수수료 반영 순익이고, -1%/-2% 손절은 수수료 반영 전 진입가 기준 가격 하락률. 구조적 손절은 신호 시점까지 완료된 <b>최근 5분봉 6개의 최저가 -0.2%</b>. 구조적 손절가가 진입가 이상이거나 최근 6개 봉이 불연속이면 판정 불가.</div>
<div class="muted"><b>손절은 최초 진입부터 잔량이 모두 청산될 때까지 유지</b>하고 본절 보호를 적용하지 않음. 손절과 익절이 한 봉에 모두 닿으면 손절 우선, 갭 하락은 더 불리한 시가 적용. 240분 만료 청산. 모든 8개 방식에 판정 완료된 <b>동일 신호</b>만 표에 사용. 하루 4회 제한이나 실제 슬리피지는 반영하지 않은 연구용 시뮬레이션.</div>
<p id="slcompare_coverage">검사 전</p>
<div class="scroll"><table><thead><tr><th>첫 익절</th><th>손절</th><th>동일 건수</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익</th><th>최대 손실</th><th>손절 발생</th><th>1차 도달</th><th>평균 손절폭</th></tr></thead><tbody id="slcompare_rows"><tr><td colspan="11">검사 전</td></tr></tbody></table></div>
<button id="slcompare_csv" disabled>SL 비교 CSV 받기</button></section>'''

JS = r'''<script>
(()=>{
 const ids=['1','3'], stops=['NONE','SL1','SL2','STRUCT'];
 const stable=new Set('USDT USDC DAI TUSD FDUSD USD1 PYUSD USDE USDD RLUSD EURC EURI'.split(' '));
 const done=o=>o?.status==='RESOLVED'&&o.net_pct!==null&&o.net_pct!==undefined&&Number.isFinite(Number(o.net_pct));
 const all=()=>responses.flatMap(r=>(r.core17_focus?.events||[]).map(e=>({date:r.date,e})))
   .filter(({e})=>!stable.has(String(e.market||'').replace(/^KRW-/,'')));
 const paired=arr=>arr.filter(({e})=>ids.every(t=>stops.every(stop=>done(e.stop_compare?.[t]?.[stop]))));
 const pct=x=>Number.isFinite(x)?x.toFixed(3)+'%':'—';
 const basePaint=paintCore17;
 paintCore17=function(){basePaint(); const a=all(), common=paired(a);
  document.getElementById('slcompare_coverage').textContent='진입 '+a.length+'건 / 8개 모두 판정 '+common.length+'건 / 제외 '+(a.length-common.length)+'건 (누락봉·구조손절 무효 등)';
  document.getElementById('slcompare_rows').innerHTML=ids.flatMap(t=>stops.map(st=>{
   const v=common.map(({e})=>e.stop_compare[t][st]), ns=v.map(x=>Number(x.net_pct)),n=ns.length;
   const wins=ns.filter(x=>x>0).length,losses=ns.filter(x=>x<0).length;
   const risks=v.map(x=>Number(x.stop_distance_pct)).filter(x=>Number.isFinite(x)&&x>0);
   const fields=[t+'%',{NONE:'없음',SL1:'-1%',SL2:'-2%',STRUCT:'6봉 저점 -0.2%'}[st],n,wins,losses,pct(n?100*wins/n:NaN),pct(n?ns.reduce((s,x)=>s+x,0)/n:NaN),pct(n?Math.min(...ns):NaN),v.filter(x=>x.stop_triggered).length,v.filter(x=>x.tp1_filled).length,risks.length?pct(risks.reduce((s,x)=>s+x,0)/risks.length):'—'];
   return '<tr>'+fields.map(x=>'<td>'+x+'</td>').join('')+'</tr>';
  })).join('');
  document.getElementById('slcompare_csv').disabled=!a.length;
 };
 const csv=x=>'"'+String(x??'').replace(/"/g,'""')+'"';
 document.getElementById('slcompare_csv').onclick=()=>{
  const a=all(), cols=['version','date','market','base_id','signal_time_kst','entry_time_kst','fee_pct','slippage_pct','first_tp_net_pct','second_tp_net_pct','third_tp_net_pct','parts','stop_type','common_8_resolved','status','outcome','net_pct','reason','minutes','stop_price','stop_distance_pct','stop_triggered','ambiguous_stop_target','tp1_filled','tp5_filled','tp10_filled','exit_legs'],lines=[cols.join(',')];
  for(const {date,e} of a){const complete=ids.every(t=>stops.every(st=>done(e.stop_compare?.[t]?.[st])));
   for(const t of ids)for(const st of stops){const o=e.stop_compare?.[t]?.[st]||{};
    const row=['CORE.17 SL V1.0',date,e.market,e.base_id,e.signal_time_kst,e.entry_time_kst,e.tp_compare_fee,e.tp_compare_slip,t,5,10,'30/30/40',st,complete,o.status,o.outcome,o.net_pct,o.reason,o.minutes,o.stop_price,o.stop_distance_pct,o.stop_triggered,o.ambiguous_stop_target,o.tp1_filled,o.tp5_filled,o.tp10_filled,JSON.stringify(o.exit_legs||[])];
    lines.push(row.map(csv).join(','));
   }
  }
  const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),el=document.createElement('a');el.href=url;el.download='BITHUMB_CORE17_SL_V1_0_COMPARE.csv';el.click();setTimeout(()=>URL.revokeObjectURL(url),3000);
 };
})();
</script>'''


def make_page(tp_page):
    """Dedicated route with both the stop panel and historic TP comparison."""
    page=tp_page.replace('<title>CORE.17 TP V1.0 · 첫 익절 비교</title>',
                         '<title>CORE.17 SL V1.0 · 손절 비교</title>',1)
    page=page.replace('<h1>🧪 CORE.17 TP V1.0 · 첫 익절 목표 비교</h1>',
                      '<h1>🧪 CORE.17 SL V1.0 · 익절 1%/3% × 손절 4종</h1>',1)
    marker='<section class="panel" id="tpcompare-panel">'
    if marker not in page:
        raise AssertionError('TP V1.0 panel insertion point missing')
    page=page.replace(marker,PANEL+marker,1)
    if '</body></html>' not in page:
        raise AssertionError('script insertion point missing')
    return page.replace('</body></html>',JS+'</body></html>',1)
