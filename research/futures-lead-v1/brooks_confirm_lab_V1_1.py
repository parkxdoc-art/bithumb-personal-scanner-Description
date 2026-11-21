#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BITHUMB BROOKS H2 paired confirmation research V1.1 (READ-ONLY).

Same causal Brooks signal; compare entry at next completed 5m close to
entry one 5m candle later if first candle confirmed bullish. No orders.
Requires adjacent classic_priceaction_lab.py V1.0, stdlib only, Python>=3.9.
"""
from __future__ import annotations
import argparse
import bisect
import csv
import io
import json
import math
import threading
import time
from datetime import datetime, timedelta
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import classic_priceaction_lab as core

VERSION = '1.1'
STUDY = '브룩스형 H2 · 기존 진입 vs 한 봉 확인 진입'
FIVE, DAY, HOUR, KST = core.FIVE, core.DAY, core.HOUR, core.KST
DEFAULT_COINS = core.DEFAULT_COINS
CSV_NAME = 'BITHUMB_BROOKS_CONFIRM_V1_1.csv'
DIAG_NAME = 'BITHUMB_BROOKS_CONFIRM_V1_1_DIAGNOSTICS.csv'


def exit_after_entry(bars, entry_i):
    """Observe close-only TP/SL for 12 completed 5m candles AFTER entry.

    For confirmed entry, entry_i = signal_i + 2. Future candles cannot decide
    eligibility or entry; each of 12 later CLOSES independently decides exits.
    """
    if entry_i + core.HOLD_BARS >= len(bars):
        raise ValueError('완결되지 않은 청산 관측 구간')
    entry = bars[entry_i]['c']
    for k in range(entry_i + 1, entry_i + core.HOLD_BARS + 1):
        close = bars[k]['c']
        gross = (close / entry - 1) * 100
        net = gross - core.FEE_PCT
        reason = ('익절' if net >= core.TAKE_NET_PCT else
                  '손절' if gross <= core.STOP_GROSS_PCT else
                  '시간만료' if k == entry_i + core.HOLD_BARS else None)
        if reason:
            return {'entry_time_ms': bars[entry_i]['t'] + FIVE,
                    'entry_price': entry,
                    'exit_time_ms': bars[k]['t'] + FIVE,
                    'exit_price': close,
                    'hold_min': (k - entry_i) * 5,
                    'net_pct': round(net, 5),
                    'gross_pct': round(gross, 5),
                    'exit_reason': reason}
    raise AssertionError('Exit was not classified')


def bullish_confirmation(bars, i):
    """Completed next 5m candle bullish AND closes above signal close.

    Only uses the close of bar i+1, known BEFORE delayed entry at i+2 close.
    """
    confirm = bars[i+1]
    signal = bars[i]
    return confirm['c'] > confirm['o'] and confirm['c'] > signal['c']


def create_pair(coin, bars, i, liquidity):
    base = core.exit_trade(bars, i)  # V1.0 baseline semantics unchanged.
    approved = bullish_confirmation(bars, i)
    alternative = exit_after_entry(bars, i+2) if approved else None
    signal_ms = bars[i]['t'] + FIVE
    return {
        'coin':coin,'strategy':'BROOKS','signal_ms':signal_ms,
        'signal_kst':core.kst(signal_ms),'signal_close':bars[i]['c'],
        'confirm_bar_kst':core.kst(bars[i+1]['t']+FIVE),
        'confirm_bar_change_pct':round((bars[i+1]['c']/bars[i]['c']-1)*100,5),
        'confirmed':approved,'confirm_status':'진입' if approved else '미확인(거래 생략)',
        'liquidity_24h_krw':int(liquidity),
        'base_entry_kst':core.kst(base['entry_time_ms']),
        'base_exit_kst':core.kst(base['exit_time_ms']),
        'base_entry_price':base['entry_price'],
        'base_net_pct':base['net_pct'],
        'base_reason':base['exit_reason'],
        'base_hold_min':base['hold_min'],
        'confirmed_entry_kst':core.kst(alternative['entry_time_ms']) if approved else '',
        'confirmed_exit_kst':core.kst(alternative['exit_time_ms']) if approved else '',
        'confirmed_entry_price':alternative['entry_price'] if approved else '',
        'confirmed_net_pct':alternative['net_pct'] if approved else '',
        'confirmed_reason':alternative['exit_reason'] if approved else '',
        'confirmed_hold_min':alternative['hold_min'] if approved else '',
        'paired_delta_pct':round(alternative['net_pct']-base['net_pct'],5) if approved else '',
        'policy_net_pct':alternative['net_pct'] if approved else 0.0,
        'base':base,'alternative':alternative,
    }


def coverage_report(coin, bars, since_ms, end_ms):
    """Entire requested interval, not only the profitable signal subset."""
    expected=(end_ms-since_ms)//FIVE
    in_period=[r for r in bars if since_ms <= r['t'] < end_ms]
    have=len({r['t'] for r in in_period})
    return {'coin':coin,'status':'OK','requested_from_kst':core.kst(since_ms),
            'requested_until_kst':core.kst(end_ms),'bars_total':len(bars),
            'first_available_kst':core.kst(bars[0]['t']+FIVE) if bars else '',
            'last_available_kst':core.kst(bars[-1]['t']+FIVE) if bars else '',
            'expected_bars':expected,'observed_bars':have,
            'missing_bars':max(0,expected-have),
            'coverage_pct':round(100*have/expected,2) if expected else 0,
            'gap_count':sum(bars[i]['t']-bars[i-1]['t'] != FIVE for i in range(1,len(bars))),
            'trend_slots':0,'liquidity_slots':0,'evaluated_slots':0,
            'raw_signals':0,'selected_signals':0,'error':''}


def scan_brooks(coin, bars, since_ms, end_ms):
    diag=coverage_report(coin,bars,since_ms,end_ms)
    if len(bars) < 1000:
        diag.update(status='INSUFFICIENT',error='1,000개 미만의 5분봉; 추세와 24시간 거래대금 확인 불가')
        return [],diag
    # Reuse V1.0 causal hourly gate, 5m EMA20 and Brooks detection exactly.
    closes=[b['c'] for b in bars]
    ema20=core.ema(closes,20)
    hour_ends,hourly=core.hourly_trend(bars)
    turnover=[0]
    for b in bars:turnover.append(turnover[-1]+b['v'])
    # A 24h turnover window must be ACTUAL 288 contiguous 5m candles.
    gap=[0]
    for j in range(1,len(bars)):
        gap.append(gap[-1]+int(bars[j]['t']-bars[j-1]['t'] != FIVE))
    out=[]
    for i in range(288,len(bars)-core.HOLD_BARS-2):
        signal_ms=bars[i]['t']+FIVE
        if signal_ms < since_ms or signal_ms + (core.HOLD_BARS+2)*FIVE > end_ms:
            continue
        if gap[i]-gap[i-287]:
            continue
        # Both baseline and confirmation paths need complete later candle closes.
        if gap[i+core.HOLD_BARS+2]-gap[i-30]:
            continue
        hi=bisect.bisect_right(hour_ends,signal_ms)-1
        if hi<0 or not hourly[hi]['up']:
            continue
        diag['trend_slots']+=1
        liq=turnover[i+1]-turnover[i-287]
        if liq < core.MIN_24H_NOTIONAL_KRW:
            continue
        diag['liquidity_slots']+=1
        diag['evaluated_slots']+=1
        if core.is_brooks(bars,ema20,i):
            out.append(create_pair(coin,bars,i,liq))
    diag['raw_signals']=len(out)
    return out,diag


def select_pairs(raw):
    """Apply SAME signal-time cap/cooldown for both policies BEFORE results."""
    return core.cap_and_cooldown(raw)


def stats(rows,field):
    vals=[float(r[field]) for r in rows]
    return {'n':len(vals),'wins':sum(v>0 for v in vals),
            'losses':sum(v<=0 for v in vals),
            'win_rate':round(100*sum(v>0 for v in vals)/len(vals),2) if vals else None,
            'avg_net_pct':round(sum(vals)/len(vals),5) if vals else None,
            'sum_net_pct_points':round(sum(vals),5),
            'worst':round(min(vals),5) if vals else None}


def summarise(pairs,since_ms,end_ms):
    traded=[x for x in pairs if x['confirmed']]
    skipped=[x for x in pairs if not x['confirmed']]
    base_all=stats(pairs,'base_net_pct')
    confirmed=stats(traded,'confirmed_net_pct')
    matched_base=stats(traded,'base_net_pct')
    rejected_base=stats(skipped,'base_net_pct')
    policy=stats(pairs,'policy_net_pct')
    daily=[]
    d=datetime.fromtimestamp(since_ms/1000,KST).date()
    stop=datetime.fromtimestamp(end_ms/1000,KST).date()
    while d<=stop:
        key=d.isoformat()
        subset=[x for x in pairs if datetime.fromtimestamp(x['signal_ms']/1000,KST).date()==d]
        entered=[x for x in subset if x['confirmed']]
        daily.append({'day':key,'signals':len(subset),'confirmed_trades':len(entered),
                      'skipped':len(subset)-len(entered),
                      'base_avg':stats(subset,'base_net_pct')['avg_net_pct'],
                      'policy_avg':stats(subset,'policy_net_pct')['avg_net_pct'],
                      'confirmed_avg':stats(entered,'confirmed_net_pct')['avg_net_pct']})
        d+=timedelta(days=1)
    return {'base_all':base_all,'confirmed_only':confirmed,'base_matched':matched_base,
            'skipped_base':rejected_base,'confirmed_policy':policy,
            'matched_delta_pp':round(confirmed['avg_net_pct']-matched_base['avg_net_pct'],5)
                               if traded else None,
            'opportunity_delta_pp':round(policy['avg_net_pct']-base_all['avg_net_pct'],5)
                                    if pairs else None,
            'confirmed_tp':sum(x['confirmed_reason']=='익절' for x in traded),
            'confirmed_sl':sum(x['confirmed_reason']=='손절' for x in traded),
            'confirmed_timeout':sum(x['confirmed_reason']=='시간만료' for x in traded),
            'base_tp':sum(x['base_reason']=='익절' for x in pairs),
            'base_sl':sum(x['base_reason']=='손절' for x in pairs),
            'base_timeout':sum(x['base_reason']=='시간만료' for x in pairs)},daily


def run_research(coins=DEFAULT_COINS,days=6,progress=lambda msg,pct:None,
                 now_ms=None,acquire=None):
    coins=core.coin_list(coins if isinstance(coins,str) else ','.join(coins))
    days=int(days)
    if not 1<=days<=core.MAX_DAYS:raise ValueError('연구기간은 1~13일')
    end_ms=((int(time.time()*1000) if now_ms is None else now_ms)//FIVE)*FIVE
    since_ms=end_ms-days*DAY
    fetch_since=since_ms-72*HOUR
    acquire=acquire or core.candles
    all_raw=[];diags=[];errors=[]
    for ci,coin in enumerate(coins,1):
        progress(f'빗썸 5분봉 {coin} 확인 ({ci}/{len(coins)})',round((ci-1)*88/len(coins)))
        try:
            bars=acquire(coin,fetch_since,end_ms)
            raw,diag=scan_brooks(coin,bars,since_ms,end_ms)
            all_raw.extend(raw);diags.append(diag)
        except core.ResearchError as exc:
            text=str(exc)
            errors.append(f'{coin}: {text}')
            diags.append({'coin':coin,'status':'ERROR','error':text,'expected_bars':days*288,
                          'observed_bars':0,'missing_bars':'','coverage_pct':'',
                          'bars_total':0,'gap_count':'','raw_signals':0,'selected_signals':0})
            if '요청 제한' in text:
                errors.append('거래소 요청 제한 확인: 남은 종목 수집 중단')
                for unasked in coins[ci:]:
                    diags.append({'coin':unasked,'status':'NOT_ATTEMPTED','error':'앞선 코인 API 요청 제한으로 미조회',
                                  'expected_bars':days*288,'observed_bars':0,'missing_bars':'',
                                  'coverage_pct':'','bars_total':0,'gap_count':'','raw_signals':0,'selected_signals':0})
                break
        except (ValueError,TypeError,KeyError,ZeroDivisionError,OverflowError) as exc:
            text=f'데이터 오류: {str(exc)[:140]}'
            errors.append(f'{coin}: {text}')
            diags.append({'coin':coin,'status':'ERROR','error':text,'expected_bars':days*288,
                          'observed_bars':0,'missing_bars':'','coverage_pct':'',
                          'bars_total':0,'gap_count':'','raw_signals':0,'selected_signals':0})
    if not any(d['status']=='OK' for d in diags):
        raise core.ResearchError('정상 수집된 코인이 없습니다: '+(' / '.join(errors)[:350] if errors else '자료 부족'))
    pairs,dropped=select_pairs(all_raw)
    counts=defaultdict(int)
    for x in pairs:counts[x['coin']]+=1
    for d in diags:d['selected_signals']=counts[d['coin']]
    summary,daily=summarise(pairs,since_ms,end_ms)
    progress('동일 신호쌍 진입·누락 집계 완료',100)
    return {'version':VERSION,'study':STUDY,'since_kst':core.kst(since_ms),
            'until_kst':core.kst(end_ms),'days':days,'requested_coins':coins,
            'diagnostics':diags,'errors':errors,'raw_signals':len(all_raw),
            'pairs':pairs,'excluded_signals':dropped,'summary':summary,'daily':daily,
            'assumptions':{'fee_round_trip_pct':core.FEE_PCT,
              'take_net_pct':core.TAKE_NET_PCT,
              'stop_gross_pct':core.STOP_GROSS_PCT,
              'max_hold_minutes':core.HOLD_BARS*5,
              'confirmed_rule':'signal+1 close>open AND signal+1 close>signal close',
              'baseline_entry':'signal+1 completed 5m close',
              'confirmed_entry':'signal+2 completed 5m close only if confirmed',
              'no_trade_policy_value_pct':0.0,
              'liquidity_24h_krw':core.MIN_24H_NOTIONAL_KRW,
              'daily_cap':core.DAY_CAP,
              'cooldown_minutes':core.COOLDOWN_MS//60000}}


PAIR_FIELDS=['coin','signal_kst','signal_ms','liquidity_24h_krw','signal_close',
  'confirm_bar_kst','confirm_bar_change_pct','confirmed','confirm_status',
  'base_entry_kst','base_exit_kst','base_entry_price','base_net_pct','base_reason','base_hold_min',
  'confirmed_entry_kst','confirmed_exit_kst','confirmed_entry_price','confirmed_net_pct',
  'confirmed_reason','confirmed_hold_min','paired_delta_pct','policy_net_pct']
DIAG_FIELDS=['coin','status','requested_from_kst','requested_until_kst','bars_total',
  'first_available_kst','last_available_kst','expected_bars','observed_bars',
  'missing_bars','coverage_pct','gap_count','trend_slots','liquidity_slots',
  'evaluated_slots','raw_signals','selected_signals','error']


def csv_text(rows,fields):
    output=io.StringIO();writer=csv.writer(output);writer.writerow(fields)
    for r in rows:writer.writerow([r.get(k,'') for k in fields])
    return '\ufeff'+output.getvalue()


HTML = '''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>빗썸 브룩스 확인 진입 연구 V1.1</title><style>
*{box-sizing:border-box}body{background:#0c1526;color:#eef5ff;font:15px system-ui,sans-serif;line-height:1.62;margin:0}main{max-width:1050px;margin:auto;padding:20px 14px 80px}.card{background:#17263d;border:1px solid #344966;border-radius:15px;margin:18px 0;padding:17px}h1{font-size:26px;margin:9px 0}h2{font-size:19px}p,.hint{color:#b7cadf}.muted{font-size:12px;color:#a8c1da}input,select{display:block;background:#0e1d31;color:#f3f7fc;border:1px solid #547291;border-radius:9px;font:inherit;padding:12px;width:100%;max-width:100%}label{font-size:13px;display:block;margin:12px 0 6px;color:#c3d8ed}button{font:inherit;border:0;background:#2681d8;color:white;padding:12px 17px;border-radius:9px;cursor:pointer;font-weight:700}button:disabled{opacity:.55}a{color:#93d2ff}.scroll{overflow:auto}table{min-width:680px;border-collapse:collapse;font-size:12px;width:100%}td,th{white-space:nowrap;text-align:left;padding:9px;border-bottom:1px solid #354d68}th{color:#b4d4ef}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:10px}.kpi{background:#0e1d30;border:1px solid #2f4761;border-radius:12px;padding:13px}.num{font-size:23px;font-weight:800}#error{white-space:pre-wrap;color:#ffbfb7}.tag{color:#a9d2f9;font-size:12px}.good{color:#85dfad}.bad{color:#ffb3b3}</style></head><body><main>
<div class="tag">BITHUMB KRW · 독립 연구 · 거래 주문/BUY 없음 · V1.1</div><h1>브룩스형 H2 진입 시점 비교</h1><p>V1.0에서 관찰된 브룩스 신호에 대해 <b>기존 다음 5분봉 종가 진입</b>과 <b>그 봉이 상승 완료된 경우 한 봉 더 늦은 종가 진입</b>을 같은 신호별로 비교합니다. 다른 유명 트레이더의 실제 거래는 아닙니다.</p>
<section class="card"><h2>1. 연구 실행</h2><label for="coins">빗썸 KRW 코인 (최대 20개)</label><input id="coins" value="BTC,ETH,XRP,SOL,DOGE,ADA,LINK,AVAX,SUI,PEPE" autocomplete="off"><label for="days">연구 기간</label><select id="days"><option value="6">최근 6일</option><option value="13">최근 13일</option></select><p class="muted">기본 10종목·6일 · 1시간 상승추세 · 24시간 연속 5분봉 거래대금 5억 이상 · 하루 최대 4신호 · 같은 코인 3시간 쿨다운</p><button id="run">연구 실행</button> <b id="status">대기</b><p id="progress" class="muted"></p><p id="error"></p></section>
<section class="card"><h2>2. 두 가지 진입 방법</h2><p><b>기존:</b> 브룩스 신호 봉 완료 → 다음 5분봉 종가 매수 가정.</p><p><b>확인:</b> 다음 봉의 종가가 시가 및 브룩스 신호 종가보다 모두 높으면 승인 → 그 <b>다음 봉 종가</b>에서 매수 가정. 확인 실패 시 거래하지 않음(0%).</p><p class="muted">동일 신호만 사용 · 왕복 수수료 0.5% · 순수익 +0.5% 익절 · 가격 -1% 손절 · 최대 60분 · 완료봉 종가만 청산 판정 · 슬리피지 0%</p></section>
<section id="results" class="card" hidden><h2>3. 진입 효과 비교</h2><p id="scope" class="muted"></p><div class="kpis" id="kpis"></div><div class="scroll"><table><thead><tr><th>비교 대상</th><th>건수</th><th>수익</th><th>실거래 손실</th><th>미거래</th><th>평균 순수익</th><th>최대 손실</th></tr></thead><tbody id="summary"></tbody></table></div><p class="muted" id="compareNote"></p><p><a href="/api/brooks-v11/csv">동일 신호 비교 CSV 받기</a> · <a href="/api/brooks-v11/diagnostics.csv">수집 진단 CSV 받기</a></p><h2>날짜별 성과</h2><div class="scroll"><table><thead><tr><th>날짜(KST)</th><th>전체 신호</th><th>확인 진입</th><th>건너뜀</th><th>기존 평균</th><th>확인 정책 평균</th></tr></thead><tbody id="daily"></tbody></table></div><h2>수집 범위/오류</h2><div class="scroll"><table><thead><tr><th>코인</th><th>상태</th><th>수집 봉/기대 봉</th><th>누락 봉</th><th>원본 신호</th><th>선별 신호</th><th>비고</th></tr></thead><tbody id="diagnostics"></tbody></table></div></section>
<section class="card"><h2>4. 연구 해석 주의</h2><p>확인 진입의 개별 평균만 높아져도, 건너뛴 신호의 놓친 이익까지 고려해야 실효성을 판단할 수 있습니다. <b>동일 신호 중 확인된 부분집합끼리</b> 비교하며, 전체 신호당 성과에는 미거래를 0%로 계산한 보조 지표를 함께 표시합니다.</p><p class="muted">V1.0과 달리 24시간 연속 봉 확인을 추가했으므로 V1.0 CSV의 거래 건수와 같을 필요는 없습니다. 파일 수집 오류나 누락봉이 있으면 별도 진단에 표시합니다. 모든 수익률은 체결 확인이 아닌 모의 계산이며 슬리피지·호가·시장충격은 제외됩니다.</p><a href="/classic">기존 V1.0 연구실</a> · <a href="/">기존 G형 연구</a></section>
</main><script>
const $=id=>document.getElementById(id), esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=n=>typeof n==='number'?n.toFixed(3)+'%':'—';
let timer=null;async function poll(){try{const j=await(await fetch('/api/brooks-v11/status',{cache:'no-store'})).json();$('status').textContent=j.state==='running'?'연구 진행 중':j.state==='done'?'완료':j.state==='error'?'오류':'대기';$('run').disabled=j.state==='running';$('progress').textContent=(j.progress||'')+' '+(j.pct||0)+'%';$('error').textContent=j.state==='error'?(j.error||'오류'):'';if(j.state==='done'&&j.result)draw(j.result);if(j.state==='running')timer=setTimeout(poll,1700);}catch(e){$('error').textContent='상태 확인 오류: '+e.message;}}
function draw(j){$('results').hidden=false;const s=j.summary,b=s.base_all,c=s.confirmed_only,m=s.base_matched,k=s.skipped_base,p=s.confirmed_policy;$('scope').textContent=j.since_kst+' ~ '+j.until_kst+' KST · '+j.days+'일 · 원본 신호 '+j.raw_signals+'건 · 비교 신호 '+j.pairs.length+'건';const cards=[['원래 브룩스 신호',b.n],['상승 확인 후 진입',c.n],['진입하지 않은 신호',k.n],['같은 신호 개선폭',pct(s.matched_delta_pp)]];$('kpis').innerHTML=cards.map(([a,b])=>'<div class="kpi"><div class="muted">'+esc(a)+'</div><div class="num">'+esc(b)+'</div></div>').join('');const rec=[['기존: 전체 진입',b,0],['기존: 확인된 신호만',m,0],['확인: 확인된 신호만',c,0],['기존: 미확인 신호만',k,0],['확인 정책: 전체 신호당',p,k.n]];$('summary').innerHTML=rec.map(([title,x,skipped])=>'<tr><td>'+esc(title)+'</td><td>'+x.n+'</td><td>'+x.wins+'</td><td>'+Math.max(0,x.losses-skipped)+'</td><td>'+skipped+'</td><td>'+pct(x.avg_net_pct)+'</td><td>'+pct(x.worst)+'</td></tr>').join('');$('compareNote').textContent='같은 확인 신호에서 진입 시점만 바꾼 평균 차이: '+pct(s.matched_delta_pp)+'p. 전체 원래 신호당 0% 미거래 포함 정책 차이: '+pct(s.opportunity_delta_pp)+'p. 확인 정책 0%는 수익도 손실도 아닌 미거래입니다.';$('daily').innerHTML=j.daily.map(r=>'<tr><td>'+esc(r.day)+'</td><td>'+r.signals+'</td><td>'+r.confirmed_trades+'</td><td>'+r.skipped+'</td><td>'+pct(r.base_avg)+'</td><td>'+pct(r.policy_avg)+'</td></tr>').join('');$('diagnostics').innerHTML=j.diagnostics.map(r=>'<tr><td>'+esc(r.coin)+'</td><td>'+esc(r.status)+'</td><td>'+esc(r.observed_bars)+' / '+esc(r.expected_bars)+'</td><td>'+esc(r.missing_bars)+'</td><td>'+esc(r.raw_signals)+'</td><td>'+esc(r.selected_signals)+'</td><td>'+esc(r.error)+'</td></tr>').join('');if(j.errors.length)$('error').textContent='일부 종목 오류/미조회: '+j.errors.join(' / ');}
$('run').addEventListener('click',async()=>{try{$('error').textContent='';const r=await fetch('/api/brooks-v11/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({coins:$('coins').value,days:Number($('days').value)})});const j=await r.json();if(!r.ok)throw Error(j.error||'연구 실행 실패');if(timer)clearTimeout(timer);poll();}catch(e){$('error').textContent=e.message;}});poll();
</script></body></html>'''

JOB={'state':'idle','progress':'','pct':0,'error':None,'result':None,'cache_key':None,'completed_at':0}
JOB_LOCK=threading.Lock()


def worker(coins,days):
    def progress(message,pct):
        with JOB_LOCK:JOB.update(progress=message,pct=pct)
    try:
        result=run_research(coins,days,progress)
        with JOB_LOCK:JOB.update(state='done',progress='연구 및 CSV 완료',pct=100,result=result,completed_at=int(time.time()*1000))
    except Exception as exc:
        with JOB_LOCK:JOB.update(state='error',error=str(exc),pct=0)


def handle_get(handler,path):
    if path=='/classic/brooks':return handler.respond(HTML,ctype='text/html; charset=utf-8')
    if path=='/api/brooks-v11/status':
        with JOB_LOCK:data=dict(JOB)
        return handler.respond(json.dumps(data,ensure_ascii=False))
    if path in ('/api/brooks-v11/csv','/api/brooks-v11/diagnostics.csv'):
        with JOB_LOCK:data=JOB['result']
        if not data:return handler.respond(json.dumps({'error':'연구 완료 결과 없음'},ensure_ascii=False),404)
        diagnostic=path.endswith('diagnostics.csv')
        contents=csv_text(data['diagnostics'] if diagnostic else data['pairs'],DIAG_FIELDS if diagnostic else PAIR_FIELDS)
        filename=DIAG_NAME if diagnostic else CSV_NAME
        return handler.respond(contents,ctype='text/csv; charset=utf-8',extra={'Content-Disposition':f'attachment; filename="{filename}"'})
    return handler.respond(json.dumps({'error':'not found'}),404)


def handle_post(handler,path):
    if path!='/api/brooks-v11/run':return handler.respond('{"error":"not found"}',404)
    try:
        size=int(handler.headers.get('Content-Length','0'))
        if not 0<size<=2048:raise ValueError('요청 크기 오류')
        data=json.loads(handler.rfile.read(size))
        if not isinstance(data,dict):raise ValueError('잘못된 요청 형식')
        coins=core.coin_list(data.get('coins',DEFAULT_COINS))
        days=int(data.get('days',6))
        if not 1<=days<=core.MAX_DAYS:raise ValueError('1~13일만 지원')
    except (TypeError,ValueError,KeyError,json.JSONDecodeError) as exc:
        return handler.respond(json.dumps({'error':str(exc)},ensure_ascii=False),400)
    with core.JOB_LOCK:
        if core.JOB['state']=='running':
            return handler.respond(json.dumps({'error':'기존 클래식 연구 실행 중입니다. 완료 후 진행해 주세요'},ensure_ascii=False),409)
    with JOB_LOCK:
        if JOB['state']=='running':return handler.respond(json.dumps({'error':'이미 연구 진행 중'},ensure_ascii=False),409)
        key=(','.join(coins),days)
        if (JOB['state']=='done' and JOB['result'] is not None and JOB['cache_key']==key
                and int(time.time()*1000)-JOB['completed_at']<10*60_000):
            return handler.respond(json.dumps({'ok':True,'cached':True},ensure_ascii=False))
        JOB.update(state='running',progress='수집 시작',pct=0,error=None,result=None,cache_key=key)
    threading.Thread(target=worker,args=(coins,days),daemon=True).start()
    return handler.respond(json.dumps({'ok':True},ensure_ascii=False))


class StandaloneHandler(BaseHTTPRequestHandler):
    def log_message(self,format,*args):pass
    def respond(self,body,status=200,ctype='application/json; charset=utf-8',extra=None):
        if isinstance(body,str):body=body.encode('utf-8')
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        for key,value in (extra or {}).items():self.send_header(key,value)
        self.end_headers();self.wfile.write(body)
    def do_GET(self):return handle_get(self,urlsplit(self.path).path)
    def do_POST(self):return handle_post(self,urlsplit(self.path).path)


def main():
    p=argparse.ArgumentParser(description=STUDY)
    p.add_argument('--selftest',action='store_true')
    p.add_argument('--web',action='store_true');p.add_argument('--port',type=int,default=10021)
    p.add_argument('--coins',default=DEFAULT_COINS);p.add_argument('--days',type=int,default=6)
    args=p.parse_args()
    if args.selftest:
        from test_brooks_confirm_V1_1 import run_tests
        return run_tests()
    if args.web:
        server=ThreadingHTTPServer(('0.0.0.0',args.port),StandaloneHandler)
        print('BROOKS V1.1 web on',args.port,flush=True)
        return server.serve_forever()
    result=run_research(args.coins,args.days)
    print(json.dumps({k:v for k,v in result.items() if k!='pairs'},ensure_ascii=False,indent=2))


if __name__=='__main__':main()