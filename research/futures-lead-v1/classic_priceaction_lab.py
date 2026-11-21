#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bithumb Classic Price Action Research V1.0 — independent, read-only.

Compare deterministic adaptations inspired by Bob Volman's second-break,
Al Brooks H2, and Linda Raschke's Holy Grail. No trader's returns are asserted.
Python >= 3.9 standard library only. Not an order-execution program.
"""
from __future__ import annotations
import argparse
import bisect
import csv
import io
import json
import math
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

VERSION = "1.0"
FIVE = 300_000
HOUR = 3_600_000
DAY = 24 * HOUR
KST = timezone(timedelta(hours=9))
BITHUMB = "https://api.bithumb.com"
DEFAULT_COINS = "BTC,ETH,XRP,SOL,DOGE,ADA,LINK,AVAX,SUI,PEPE"
MAX_COINS, MAX_DAYS = 20, 13
STRATEGIES = {"VOLMAN":"볼먼형 2차 돌파", "BROOKS":"브룩스형 H2 재진입", "RASCHKE":"라쉬키형 EMA20·ADX"}
FEE_PCT = 0.5
TAKE_NET_PCT = 0.5
STOP_GROSS_PCT = -1.0
HOLD_BARS = 12
COOLDOWN_MS = 180 * 60_000
DAY_CAP = 4
MIN_24H_NOTIONAL_KRW = 500_000_000
# Keep the server and client from creating back-to-back bursts.
RATE_LOCK = threading.Lock()
NEXT_REQUEST_AT = 0.0

class ResearchError(Exception):
    pass


def kst(ms):
    return datetime.fromtimestamp(ms / 1000, KST).strftime("%Y-%m-%d %H:%M")


def coin_list(value):
    cs = list(dict.fromkeys(s.strip().upper().replace('KRW-', '') for s in str(value).split(',') if s.strip()))
    if not 1 <= len(cs) <= MAX_COINS or any(not re.fullmatch(r'[A-Z0-9]{2,16}', x) for x in cs):
        raise ValueError('코인은 영문·숫자 1~20종목만 입력해 주세요')
    return cs


def fetch_json(path, params):
    global NEXT_REQUEST_AT
    if path != '/v1/candles/minutes/5':
        raise ResearchError('허용되지 않은 공개 API 경로')
    with RATE_LOCK:
        now = time.monotonic()
        pause = max(0.0, NEXT_REQUEST_AT - now)
        if pause:
            time.sleep(pause)
        NEXT_REQUEST_AT = time.monotonic() + 0.40
    url = BITHUMB + path + '?' + urlencode(params)
    req = Request(url, headers={'User-Agent': 'BithumbPriceActionResearch/1.0', 'Accept': 'application/json'})
    try:
        with urlopen(req, timeout=18) as response:
            return json.loads(response.read(4_000_000).decode('utf-8'))
    except HTTPError as exc:
        if exc.code in (418, 429):
            raise ResearchError('빗썸 API 요청 제한(HTTP %s): 추가 요청 없이 중단' % exc.code)
        raise ResearchError('빗썸 API HTTP %s' % exc.code)
    except (URLError, TimeoutError, ValueError, OSError) as exc:
        raise ResearchError('빗썸 API 통신 실패: %s' % str(exc)[:120])


def candles(coin, start_ms, end_ms, get_json=fetch_json):
    """Only completed 5m OHLCV, oldest timestamp=interval START.

    Official 'to' is KST and excludes the candle at that timestamp. Bithumb
    may omit candles without trading; never invent missing intervals.
    """
    result = {}
    cursor = end_ms
    for _ in range(38):
        to_kst = datetime.fromtimestamp(cursor/1000, KST).strftime('%Y-%m-%dT%H:%M:%S')
        resp = get_json('/v1/candles/minutes/5', {'market':'KRW-'+coin, 'to':to_kst, 'count':200})
        if not isinstance(resp, list):
            raise ResearchError(coin + ': 캔들 응답 형식이 잘못됐습니다')
        if not resp:
            break
        oldest = None
        for row in resp:
            try:
                t = int(datetime.fromisoformat(row['candle_date_time_utc']).replace(tzinfo=timezone.utc).timestamp()*1000)
                oldest = t if oldest is None else min(oldest, t)
                if start_ms <= t < end_ms and t + FIVE <= end_ms:
                    o = float(row['opening_price']); h=float(row['high_price']); l=float(row['low_price'])
                    c = float(row['trade_price']); v=float(row.get('candle_acc_trade_price',0))
                    if not (0 < l <= min(o,c) <= max(o,c) <= h and math.isfinite(v) and v>=0):
                        continue
                    result[t] = {'t':t,'o':o,'h':h,'l':l,'c':c,'v':v}
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
        if oldest is None or oldest >= cursor:
            raise ResearchError(coin + ': 분봉 페이징 시각이 전진하지 않았습니다')
        cursor = oldest
        if cursor <= start_ms:
            break
    if not result:
        raise ResearchError(coin + ': 정상적인 5분봉이 없습니다')
    return [result[t] for t in sorted(result)]


def ema(values, n):
    alpha = 2/(n+1)
    ans = []
    current = None
    for x in values:
        current = x if current is None else current + alpha*(x-current)
        ans.append(current)
    return ans


def dmi_adx(candles5, n=14):
    """Wilder(14) smoothing, ADX available only after initial 2*n-1 bars."""
    count=len(candles5)
    adx=[None]*count; plus_di=[None]*count; minus_di=[None]*count
    if count<2*n:
        return adx,plus_di,minus_di
    trs=[]; pds=[]; mds=[]
    for i in range(1,count):
        curr,prev=candles5[i],candles5[i-1]
        tr=max(curr['h']-curr['l'],abs(curr['h']-prev['c']),abs(curr['l']-prev['c']))
        up=curr['h']-prev['h'];down=prev['l']-curr['l']
        trs.append(tr)
        pds.append(up if up>down and up>0 else 0.0)
        mds.append(down if down>up and down>0 else 0.0)
    st=sum(trs[:n]);sp=sum(pds[:n]);sm=sum(mds[:n]);dx=[]
    for i in range(n,count):
        if i>n:
            st=st-st/n+trs[i-1];sp=sp-sp/n+pds[i-1];sm=sm-sm/n+mds[i-1]
        p=100*sp/st if st>0 else 0.0
        m=100*sm/st if st>0 else 0.0
        plus_di[i]=p;minus_di[i]=m
        dx_i=100*abs(p-m)/(p+m) if p+m>0 else 0.0
        dx.append(dx_i)
        if len(dx)==n:
            adx[i]=sum(dx)/n
        elif len(dx)>n:
            adx[i]=(adx[i-1]*(n-1)+dx_i)/n
    return adx,plus_di,minus_di


def hourly_trend(bars):
    """1h trend ONLY from complete 12-candle UTC-hour buckets."""
    buckets=defaultdict(list)
    for bar in bars:
        buckets[bar['t']//HOUR*HOUR].append(bar)
    endtimes=[]; closes=[]; hourly=[]
    for start in sorted(buckets):
        group=buckets[start]
        if len(group)!=12 or any(x['t'] != start+i*FIVE for i,x in enumerate(group)):
            continue
        endtimes.append(start+HOUR)
        closes.append(group[-1]['c'])
    a=ema(closes,20);b=ema(closes,50)
    for i, end in enumerate(endtimes):
        allowed=(i>=55 and endtimes[i]-endtimes[i-55]==55*HOUR
                 and closes[i]>a[i]>b[i] and a[i]>a[i-3])
        hourly.append({'end':end,'up':allowed,'close':closes[i], 'ema20':a[i], 'ema50':b[i]})
    return endtimes, hourly


def consecutive(bars, start, end):
    if start < 0 or end >= len(bars):
        return False
    return all(bars[j]['t']-bars[j-1]['t']==FIVE for j in range(start+1,end+1))


def is_volman(b, ma, i):
    """Operational two-break approximation, NOT discretionary Bob Volman trades."""
    if not (b[i]['c']>b[i]['o'] and b[i]['c']>b[i-1]['h'] and b[i]['c']>ma[i]):
        return False
    for j in range(i-9,i-3):
        if j < 9:
            continue
        first = b[j]['c']>max(z['h'] for z in b[j-8:j]) and b[j]['c']>b[j]['o']
        if not first or b[i-1]['c']>b[j]['h'] or b[i]['c']<=b[j]['h']:
            continue
        middle=b[j+1:i]
        if len(middle)<3:
            continue
        trough=min(z['l'] for z in middle)
        if trough <=0 or trough<min(ma[j+1:i])*0.993:
            continue
        if any(z['c']<b[j]['c'] for z in middle) and any(z['c']<ma[k]*1.01 for k,z in enumerate(middle,j+1)):
            return True
    return False


def is_brooks(b, ma, i):
    """Approximate second bullish attempt after failed first rebound near EMA20."""
    if not (b[i]['c']>b[i]['o'] and b[i]['c']>b[i-1]['h'] and b[i]['c']>ma[i]):
        return False
    for j in range(i-10,i-3):
        if j<1 or not (b[j]['c']>b[j]['o'] and b[j]['c']>b[j-1]['h']):
            continue
        failed = min(z['l'] for z in b[j+1:i]) < b[j]['l']
        touch = any(b[k]['l']<=ma[k]*1.002 and b[k]['l']>=ma[k]*0.975 for k in range(j-1,i))
        if failed and touch and b[i-1]['c']<b[j]['h']:
            return True
    return False


def is_raschke(b, ma, adx, pdi, mdi, i):
    """Holy Grail-inspired: rising ADX above 30, EMA20 pullback, buy rebound."""
    if not (b[i]['c']>b[i]['o'] and b[i]['c']>ma[i] and b[i]['c']>b[i-1]['h']):
        return False
    if adx[i] is None or not (adx[i]>=30 and pdi[i]>mdi[i]):
        return False
    for j in range(i-4,i):
        if j<2 or adx[j] is None or adx[j-1] is None:
            continue
        if not (adx[j]>=30 and adx[j]>adx[j-1] and pdi[j]>mdi[j]):
            continue
        if b[j]['l']<=ma[j]*1.004 and b[j]['c']>=ma[j]*0.987 and b[i]['c']>b[j]['h']:
            return True
    return False


def exit_trade(bars, signal_i):
    """Signal closes at i; enter NEXT completed 5m close i+1.

    Exit on first subsequent completed 5m close. No wick/tick claims. 60m max.
    """
    ent=bars[signal_i+1]
    entry=ent['c']
    for k in range(signal_i+2,signal_i+HOLD_BARS+2):
        bar=bars[k]
        gross=(bar['c']/entry-1)*100
        net=gross-FEE_PCT
        reason='익절' if net>=TAKE_NET_PCT else '손절' if gross<=STOP_GROSS_PCT else '시간만료' if k==signal_i+HOLD_BARS+1 else ''
        if reason:
            return {'entry_time_ms':ent['t']+FIVE,'entry_price':entry,'exit_time_ms':bar['t']+FIVE,
                    'exit_price':bar['c'],'hold_min':(k-signal_i-1)*5,
                    'gross_pct':round(gross,5),'net_pct':round(net,5),'exit_reason':reason}
    raise AssertionError('exit window not covered')


def candidates_for_coin(coin, bars, since_ms, end_ms):
    """Signals use only information with observation time <= signal bar close."""
    if len(bars)<1000:
        return [], {'bars':len(bars),'reason':'캔들 1,000개 미만, 추세·거래대금 검증 불가'}
    closes=[r['c'] for r in bars]
    ma=ema(closes,20)
    adx,pdi,mdi=dmi_adx(bars)
    ends, hour=hourly_trend(bars)
    volume=[0]
    for r in bars: volume.append(volume[-1]+r['v'])
    candidates=[]; signal_tested=0;trend_seen=0;liq_seen=0
    latest_end = end_ms
    for i in range(288,len(bars)-HOLD_BARS-1):
        signal_close=bars[i]['t']+FIVE
        if not(since_ms<=signal_close and signal_close + (HOLD_BARS+1)*FIVE <= latest_end):
            continue
        # Need 30 complete 5m candles before signal and 65 minutes after it.
        if not consecutive(bars,i-30,i+HOLD_BARS+1):
            continue
        j=bisect.bisect_right(ends,signal_close)-1
        if j<0 or not hour[j]['up']:
            continue
        trend_seen+=1
        if volume[i+1]-volume[i+1-288] < MIN_24H_NOTIONAL_KRW:
            continue
        liq_seen+=1
        signal_tested+=1
        tests={'VOLMAN':is_volman(bars,ma,i), 'BROOKS':is_brooks(bars,ma,i),
               'RASCHKE':is_raschke(bars,ma,adx,pdi,mdi,i)}
        for rule, hit in tests.items():
            if hit:
                trade=exit_trade(bars,i)
                candidates.append({'coin':coin,'strategy':rule,'signal_ms':signal_close,
                                   'signal_kst':kst(signal_close),
                                   'entry_kst':kst(trade['entry_time_ms']),
                                   'exit_kst':kst(trade['exit_time_ms']),
                                   'signal_close':bars[i]['c'],
                                   'adx14':round(adx[i],2) if adx[i] is not None else '',
                                   'liquidity_24h_krw':int(volume[i+1]-volume[i+1-288]),
                                   **trade})
    gaps=sum(1 for i in range(1,len(bars)) if bars[i]['t']-bars[i-1]['t']!=FIVE)
    return candidates,{'bars':len(bars),'gaps':gaps,'trend_bars':trend_seen,
                       'liquid_trend_bars':liq_seen,'evaluated_bars':signal_tested,'raw_signals':len(candidates)}


def cap_and_cooldown(candidates):
    """Enforce max 4/day/strategy across all coins and 3h per coin/strategy.
    Earliest signal wins; never select based on future return.
    """
    ordered=sorted(candidates,key=lambda x:(x['signal_ms'],x['strategy'],x['coin']))
    chosen=[];last={};day_count=defaultdict(int);dropped={'cooldown':0,'daily_cap':0}
    for row in ordered:
        rule=row['strategy'];coin=row['coin']
        if row['signal_ms']-last.get((rule,coin),-10**18)<COOLDOWN_MS:
            dropped['cooldown']+=1;continue
        day=datetime.fromtimestamp(row['signal_ms']/1000,KST).strftime('%Y-%m-%d')
        if day_count[(rule,day)]>=DAY_CAP:
            dropped['daily_cap']+=1;continue
        last[(rule,coin)]=row['signal_ms'];day_count[(rule,day)]+=1;chosen.append(row)
    return chosen,dropped


def stats(rows):
    nets=[r['net_pct'] for r in rows];n=len(nets)
    return {'n':n,'wins':sum(x>0 for x in nets),'losses':sum(x<=0 for x in nets),
            'win_rate':round(sum(x>0 for x in nets)/n*100,2) if n else None,
            'avg_net_pct':round(sum(nets)/n,4) if n else None,
            'total_net_pct_points':round(sum(nets),4),
            'best':round(max(nets),4) if n else None,
            'worst':round(min(nets),4) if n else None,
            'tp':sum(r['exit_reason']=='익절' for r in rows),
            'sl':sum(r['exit_reason']=='손절' for r in rows),
            'timeout':sum(r['exit_reason']=='시간만료' for r in rows)}


def summarize(rows,since_ms,end_ms):
    by_rule={key:stats([r for r in rows if r['strategy']==key]) for key in STRATEGIES}
    by_day=defaultdict(list)
    for r in rows:
        day=datetime.fromtimestamp(r['signal_ms']/1000,KST).strftime('%Y-%m-%d')
        by_day[(day,r['strategy'])].append(r)
    daily=[]
    start_date=datetime.fromtimestamp(since_ms/1000,KST).date()
    end_date=datetime.fromtimestamp(end_ms/1000,KST).date()
    day=start_date
    while day<=end_date:
        key=day.isoformat()
        for rule in STRATEGIES:
            daily.append({'day':key,'strategy':rule,**stats(by_day[(key,rule)])})
        day+=timedelta(days=1)
    return by_rule,daily


def run_research(coins=DEFAULT_COINS,days=6,progress=lambda text,pct:None, now_ms=None,
                 acquire=candles):
    if isinstance(coins,str): coins=coin_list(coins)
    else: coins=coin_list(','.join(coins))
    if not 1<=int(days)<=MAX_DAYS: raise ValueError('1~13일 연구만 가능합니다')
    days=int(days)
    now_ms=int(time.time()*1000) if now_ms is None else now_ms
    end_ms=now_ms//FIVE*FIVE
    since_ms=end_ms-days*DAY
    warmup_ms=since_ms-72*HOUR
    rows=[];diagnostics=[];errors=[]
    for idx,coin in enumerate(coins,1):
        progress('빗썸 5분봉 %s 수집 (%d/%d)'%(coin,idx,len(coins)),int((idx-1)/len(coins)*90))
        try:
            bars=acquire(coin,warmup_ms,end_ms)
            signals,diag=candidates_for_coin(coin,bars,since_ms,end_ms)
            diag['coin']=coin;diagnostics.append(diag)
            rows.extend(signals)
        except ResearchError as exc:
            errors.append(coin+': '+str(exc))
            if '요청 제한' in str(exc):
                errors.append('거래소 요청 제한이므로 남은 종목은 조회하지 않았습니다')
                break
        except (ValueError,KeyError,TypeError,ZeroDivisionError) as exc:
            errors.append(coin+': 데이터 처리 오류: '+str(exc)[:120])
    if not diagnostics:
        raise ResearchError('모든 종목 수집에 실패했습니다: '+(' / '.join(errors)[:450] if errors else '데이터 없음'))
    progress('전략별 동일 기준 집계',95)
    chosen,dropped=cap_and_cooldown(rows)
    comparison,daily=summarize(chosen,since_ms,end_ms)
    progress('연구 종료',100)
    return {'version':VERSION,'since':kst(since_ms),'until':kst(end_ms),
            'days':days,'requested':len(coins),'covered':sum(1 for d in diagnostics if d.get('evaluated_bars',0)>0),
            'coin_diagnostics':diagnostics,'errors':errors,'raw_signal_count':len(rows),
            'excluded_signals':dropped,'signals':chosen,'summary':comparison,'daily':daily,
            'assumptions':{'fee_round_trip_pct':FEE_PCT,'take_net_pct':TAKE_NET_PCT,
                           'stop_gross_pct':STOP_GROSS_PCT,'max_hold_min':HOLD_BARS*5,
                           'cooldown_min':COOLDOWN_MS//60_000,'daily_cap_per_strategy':DAY_CAP,
                           'min_24h_trading_krw':MIN_24H_NOTIONAL_KRW}}


CSV_FIELDS=['coin','strategy','signal_kst','entry_kst','exit_kst','entry_price',
            'exit_price','hold_min','gross_pct','net_pct','exit_reason','adx14',
            'liquidity_24h_krw','signal_ms','entry_time_ms','exit_time_ms']

def csv_text(rows):
    stream=io.StringIO();writer=csv.writer(stream);writer.writerow(CSV_FIELDS)
    for r in rows:writer.writerow([r.get(k,'') for k in CSV_FIELDS])
    return '\ufeff'+stream.getvalue()


HTML = '''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>빗썸 클래식 가격행동 연구 V1.0</title>
<style>*{box-sizing:border-box}body{background:#0c1526;color:#eaf0f9;font:15px system-ui,sans-serif;margin:0;line-height:1.6}main{margin:auto;max-width:1100px;padding:22px 15px 90px}h1{font-size:26px}h2{font-size:19px}p,small{color:#b4c7d9}.card{background:#17263d;border:1px solid #314967;border-radius:15px;padding:18px;margin:18px 0}input,select{background:#0b1a2c;color:#f8fcff;border:1px solid #506984;border-radius:9px;padding:11px;font:inherit;max-width:100%}input{width:100%}button{background:#247ed1;color:#fff;border:0;border-radius:9px;padding:12px 18px;font-weight:700;font:inherit;cursor:pointer}button:disabled{opacity:.5}table{border-collapse:collapse;width:100%;min-width:690px;font-size:12px}th,td{padding:10px 8px;text-align:left;border-bottom:1px solid #334b66;white-space:nowrap}th{color:#b2d1ef}.scroll{overflow-x:auto}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}label{color:#bcd1e3;font-size:13px;display:block;margin:12px 0 5px}a{color:#8fd0ff}#error{white-space:pre-wrap;color:#ffb7af}.muted{font-size:12px;color:#acbfd6}.tag{display:inline-block;background:#253e5b;color:#cde4ff;padding:4px 10px;border-radius:100px;font-size:12px}.positive{color:#7ee1ae}.negative{color:#ffc0ac}</style></head><body><main>
<span class="tag">빗썸 KRW · 유명 트레이더 원칙 기반 연구 · V1.0</span><h1>가격행동 3전략 비교 연구실</h1><p>볼먼형 2차 돌파 / 브룩스형 두 번째 진입 / 라쉬키형 ADX·EMA20을 같은 조건으로 검증합니다. 원전 매매를 복제하지 않고 정량화한 연구 규칙이며, 실제 주문·BUY 알림은 없습니다.</p>
<section class="card"><h2>1. 연구 실행</h2><label for="coins">빗썸 KRW 코인 (최대 20개)</label><input id="coins" value="BTC,ETH,XRP,SOL,DOGE,ADA,LINK,AVAX,SUI,PEPE" autocomplete="off"><label for="days">최근 연구 일수</label><select id="days"><option value="6">6일 (기본)</option><option value="13">13일 (확대)</option></select><p class="muted">1시간봉 상승 추세 · 5분봉 진입 · 24시간 거래대금 5억 이상 · 전략별 하루 최대 4회 · 3시간 코인별 중복 방지 · 동일 설정 10분 이내 재실행은 결과 재사용</p><button id="run">연구 실행</button> <b id="state">대기</b><p id="progress" class="muted"></p><p id="error"></p></section>
<section class="card"><h2>2. 공통 모의청산 조건</h2><div class="grid"><div>왕복 수수료<br><b>0.50%</b></div><div>목표 순수익<br><b>+0.50%</b></div><div>가격 손절<br><b>-1.00%</b></div><div>최대 보유<br><b>60분</b></div></div><p class="muted">신호는 5분봉이 완성된 뒤에만 생성하며 다음 5분봉 종가에 가상 진입합니다. 청산도 이후 완료된 5분봉 종가로만 판정합니다. 슬리피지 0% 가정. 1시간봉은 해당 시점까지 완성된 봉만 사용합니다.</p></section>
<section class="card" id="results" hidden><h2>3. 3전략 성과 비교</h2><p id="coverage"></p><div class="scroll"><table><thead><tr><th>전략</th><th>진입</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익</th><th>익절/손절/만료</th><th>최대 손실</th></tr></thead><tbody id="summary"></tbody></table></div><p><a id="csv" href="/api/classic/csv">전체 신호 CSV 받기</a></p><h2>날짜별 결과</h2><div class="scroll"><table><thead><tr><th>날짜(KST)</th><th>전략</th><th>거래</th><th>수익</th><th>손실</th><th>순수익 평균</th></tr></thead><tbody id="daily"></tbody></table></div><h2>분석 범위와 누락</h2><p id="diag" class="muted"></p></section>
<section class="card"><h2>4. 해석 주의</h2><p class="muted">세 트레이더 개인의 실전 매매 기록이나 검증된 개인 승률이 아닙니다. 빗썸 현물 가격으로 만든 세 개의 자체 연구 신호입니다. 시장가 수수료·실제 슬리피지·호가 공백은 반영하지 않았고 5분봉의 고가/저가를 체결로 가정하지 않습니다. 표본이 작거나 오류·누락이 있으면 성과를 확정할 수 없습니다.</p><a href="/">기존 G형 연구실</a> · <a href="/traders/offline">파일 연구실 V1.6.2</a></section>
</main><script>
const $=id=>document.getElementById(id), rules={VOLMAN:'볼먼형',BROOKS:'브룩스형',RASCHKE:'라쉬키형'};
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const val=(v,d=2)=>typeof v==='number'?v.toFixed(d)+'%':'—';
async function refresh(){try{let r=await fetch('/api/classic/status',{cache:'no-store'});let j=await r.json();$('state').textContent=j.state==='running'?'연구 진행 중':j.state==='done'?'완료':j.state==='error'?'오류':'대기';$('progress').textContent=(j.progress||'')+' '+(j.pct||0)+'%';$('run').disabled=j.state==='running';$('error').textContent=j.state==='error'?j.error||'오류':'';if(j.state==='done'&&j.result){draw(j.result);return false;}return j.state==='running';}catch(e){$('error').textContent='서버 상태 확인 오류: '+e.message;return false}}
function draw(j){$('results').hidden=false;$('coverage').textContent=`${j.since} ~ ${j.until} KST · 요청 ${j.requested}코인, 분석 ${j.covered}코인 · 신호 ${j.raw_signal_count}건, 시간·일일제한 제외 ${(j.excluded_signals.cooldown||0)+(j.excluded_signals.daily_cap||0)}건`;
$('summary').innerHTML=Object.entries(rules).map(([id,name])=>{let s=j.summary[id];return `<tr><td>${name}</td><td>${s.n}</td><td>${s.wins}</td><td>${s.losses}</td><td>${val(s.win_rate)}</td><td>${val(s.avg_net_pct,3)}</td><td>${s.tp}/${s.sl}/${s.timeout}</td><td>${val(s.worst,3)}</td></tr>`}).join('');
$('daily').innerHTML=j.daily.map(r=>`<tr><td>${esc(r.day)}</td><td>${esc(rules[r.strategy])}</td><td>${r.n}</td><td>${r.wins}</td><td>${r.losses}</td><td>${val(r.avg_net_pct,3)}</td></tr>`).join('')||'<tr><td colspan="6">조건을 만족한 신호가 없습니다</td></tr>';
$('diag').textContent=(j.coin_diagnostics||[]).map(x=>`${x.coin}: ${x.bars}봉, 결측구간 ${x.gaps??'—'}, 신호 ${x.raw_signals??0}`).join(' / ')+(j.errors.length?' | 제외·오류: '+j.errors.join(' / '):'');}
let pollTimer=null;async function poll(){const running=await refresh();if(running)pollTimer=setTimeout(poll,1700)};
$('run').addEventListener('click',async()=>{try{$('error').textContent='';const r=await fetch('/api/classic/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({coins:$('coins').value,days:Number($('days').value)})});const j=await r.json();if(!r.ok)throw Error(j.error||'연구 실행 실패');if(pollTimer)clearTimeout(pollTimer);poll()}catch(e){$('error').textContent=e.message;}});poll();
</script></body></html>'''

JOB={'state':'idle','pct':0,'progress':'','result':None,'error':None,'cache_key':None,'finished_ms':0}
JOB_LOCK=threading.Lock()


def _job_worker(coins,days):
    def progress(txt,pct):
        with JOB_LOCK: JOB.update(progress=txt,pct=pct)
    try:
        result=run_research(coins=coins,days=days,progress=progress)
        with JOB_LOCK:JOB.update(state='done',pct=100,progress='분석 및 CSV 생성 완료',result=result,finished_ms=int(time.time()*1000))
    except Exception as exc:
        with JOB_LOCK:JOB.update(state='error',error=str(exc),pct=0)


def handle_get(handler,path):
    if path=='/classic':
        return handler.respond(HTML,ctype='text/html; charset=utf-8')
    if path=='/api/classic/status':
        with JOB_LOCK:r=dict(JOB)
        return handler.respond(json.dumps(r,ensure_ascii=False))
    if path=='/api/classic/csv':
        with JOB_LOCK:r=JOB.get('result')
        if not r:return handler.respond(json.dumps({'error':'연구 결과 없음'},ensure_ascii=False),404)
        return handler.respond(csv_text(r['signals']),ctype='text/csv; charset=utf-8',
                    extra={'Content-Disposition':'attachment; filename="BITHUMB_CLASSIC_PRICEACTION_V1_0.csv"'})
    return handler.respond(json.dumps({'error':'not found'}),404)


def handle_post(handler,path):
    if path!='/api/classic/run':return handler.respond('{"error":"not found"}',404)
    try:
        size=int(handler.headers.get('Content-Length','0'))
        if not 0<size<=2048:raise ValueError('요청 크기 오류')
        values=json.loads(handler.rfile.read(size))
        if not isinstance(values,dict):raise ValueError('잘못된 요청')
        coins=coin_list(values.get('coins',DEFAULT_COINS));days=int(values.get('days',6))
        if not 1<=days<=MAX_DAYS:raise ValueError('1~13일 기간만 지원')
    except (TypeError,ValueError,json.JSONDecodeError) as e:
        return handler.respond(json.dumps({'error':str(e)},ensure_ascii=False),400)
    with JOB_LOCK:
        if JOB['state']=='running':
            return handler.respond(json.dumps({'error':'다른 연구가 이미 진행 중입니다'},ensure_ascii=False),409)
        key=(','.join(coins),days)
        if (JOB['state']=='done' and JOB['cache_key']==key and JOB['result'] is not None
                and int(time.time()*1000)-JOB['finished_ms'] < 10*60_000):
            return handler.respond(json.dumps({'ok':True,'cached':True},ensure_ascii=False))
        JOB.update(state='running',pct=0,progress='연구 시작',result=None,error=None,cache_key=key)
    threading.Thread(target=_job_worker,args=(coins,days),daemon=True).start()
    return handler.respond(json.dumps({'ok':True},ensure_ascii=False))


class StandaloneHandler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):pass
    def respond(self,body,status=200,ctype='application/json; charset=utf-8',extra=None):
        if isinstance(body,str):body=body.encode('utf-8')
        self.send_response(status);self.send_header('Content-Type',ctype)
        self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)))
        self.send_header('X-Content-Type-Options','nosniff')
        for k,v in (extra or {}).items():self.send_header(k,v)
        self.end_headers();self.wfile.write(body)
    def do_GET(self):handle_get(self,urlsplit(self.path).path)
    def do_POST(self):handle_post(self,urlsplit(self.path).path)


def selftest():
    assert coin_list('BTC,BTC,eth')==['BTC','ETH']
    try:coin_list('BTC,BAD!')
    except ValueError:pass
    else:raise AssertionError('invalid coin accepted')
    assert ema([100]*60,20)[-1]==100
    t=1_700_000_000_000//FIVE*FIVE
    bars=[{'t':t+i*FIVE,'o':100,'h':101,'l':99,'c':100,'v':1e7} for i in range(370)]
    assert consecutive(bars,1,30)
    bars[9]['t']+=FIVE*2
    assert not consecutive(bars,0,20)
    bars[9]['t']-=FIVE*2
    a,p,m=dmi_adx(bars)
    assert a[26] is None and a[27] is not None
    ends,h=hourly_trend(bars)
    # If bars don't align with UTC hour boundaries, complete buckets still align.
    assert all(x['end']%HOUR==0 for x in h)
    assert not any(h[i]['up'] for i in range(min(55,len(h))))
    b=[dict(x) for x in bars]
    b[101]['c']=101;b[102]['c']=102.2
    ex=exit_trade(b,100)
    assert ex['entry_price']==101 and ex['exit_reason']=='익절' and ex['net_pct']>0.5
    b[102]['c']=99.8
    ex=exit_trade(b,100)
    assert ex['exit_reason']=='손절' and ex['net_pct'] < -1.5
    for k in range(102,114):b[k]['c']=101
    ex=exit_trade(b,100)
    assert ex['exit_reason']=='시간만료' and ex['hold_min']==60
    base={'coin':'BTC','strategy':'BROOKS','signal_ms':t,'net_pct':-1.5,'exit_reason':'손절'}
    dup=[dict(base),dict(base,signal_ms=t+FIVE),dict(base,signal_ms=t+COOLDOWN_MS),
         dict(base,coin='ETH',signal_ms=t+FIVE)]
    capped,dropped=cap_and_cooldown(dup)
    assert len(capped)==3 and dropped['cooldown']==1
    for trade in capped:trade.update(net_pct=1.0,exit_reason='익절')
    st=stats(capped)
    assert st['n']==3 and st['wins']==3 and st['avg_net_pct']==1
    assert csv_text(capped).startswith('\ufeffcoin,')
    # A signal must not use a 1h candle that closes after its 5m signal.
    s=t+9*HOUR
    assert bisect.bisect_right([s+HOUR],s+30*60_000)==0
    # No live HTTP required for self-test.
    fake=lambda coin,since,end:bars
    x=run_research('BTC,ETH',6,acquire=fake,now_ms=t+370*FIVE)
    assert len(x['coin_diagnostics'])==2 and not x['errors'] and x['version']==VERSION
    print('PASS: classic_priceaction_lab selftest (offline, no exchange requests)')


def main():
    p=argparse.ArgumentParser();p.add_argument('--selftest',action='store_true')
    p.add_argument('--web',action='store_true');p.add_argument('--port',type=int,default=10020)
    p.add_argument('--days',type=int,default=6);p.add_argument('--coins',default=DEFAULT_COINS)
    a=p.parse_args()
    if a.selftest:return selftest()
    if a.web:
        srv=ThreadingHTTPServer(('0.0.0.0',a.port),StandaloneHandler)
        print('Classic strategy research V%s listening on %s'%(VERSION,a.port),flush=True)
        return srv.serve_forever()
    result=run_research(a.coins,a.days)
    print(json.dumps({k:v for k,v in result.items() if k!='signals'},ensure_ascii=False,indent=2))

if __name__=='__main__':main()