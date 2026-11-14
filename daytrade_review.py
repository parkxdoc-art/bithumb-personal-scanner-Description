"""V2 CORE CLEAN · research-only A/B/K validation, replace old append-only labs.

V2 live `/` and `/api/scan` stay in app.py unchanged.
`register_daytrade_review(app, globals())` remains the public integration point.
A is the original Volman C5 signal, B filters A by Beggs strength,
K is Connors RSI(2) 5m hypothesis. These are research adaptations.
Only observed 5-minute candles are used; no orders are placed.
Old experiments were deliberately removed from the runtime, not from Git history.
"""
from datetime import datetime, timedelta, date as _date, time as _time
from math import isfinite
from threading import Lock
import time
import re
from concurrent.futures import ThreadPoolExecutor
from flask import jsonify, render_template_string, request

MARKET_RE = re.compile(r'^KRW-[A-Z0-9]+$')
LAB_WARMUP_BARS = 26
LAB_MAX_PAGES = 5
VERSION = 'V2.CORE.1'

def local_time(s, kst):
    t = datetime.fromisoformat(str(s))
    return t.replace(tzinfo=kst) if t.tzinfo is None else t.astimezone(kst)

def lab_valid_request(raw, kst):
    if not isinstance(raw, dict):
        raise ValueError("검사 설정 형식 오류")
    try:
        day = _date.fromisoformat(raw["date"])
        tp = float(raw.get("tp", 1.5))
        sl = float(raw.get("sl", 1.0))
        horizon = int(raw.get("horizon", 60))
        fee = float(raw.get("fee", 0.04))
        slip = float(raw.get("slip", 0.05))
    except (KeyError, ValueError, TypeError, OverflowError):
        raise ValueError("날짜/손익 설정 값이 올바르지 않습니다")
    today = datetime.now(kst).date()
    if not (today - timedelta(days=60) <= day < today):
        raise ValueError("완전히 종료된 최근 60일 중 하루만 선택하세요")
    if not (0.3 <= tp <= 10 and 0.2 <= sl <= 8 and
            horizon in (30, 60, 120) and 0 <= fee <= 0.5 and 0 <= slip <= 0.5 and
            all(isfinite(x) for x in (tp, sl, fee, slip))):
        raise ValueError("익절·손절·시간·비용 설정 범위 오류")
    return {"day": day, "tp": tp, "sl": sl, "horizon": horizon,
            "fee": fee, "slip": slip, "bars": horizon // 5}

def lab_contiguous(rows, first, last):
    """All observed bars in the requested [first,last] range are 5m apart."""
    if first < 0 or last >= len(rows):
        return False
    return all(rows[j]["t"] - rows[j-1]["t"] == timedelta(minutes=5)
               for j in range(first+1, last+1))

def lab_outcome_v11145(rows, index, cfg):
    """Determine TP / SL as soon as hit, in time order, without future leakage.

    Only the observed bars up to the earliest decided exit need to be
    continuous. Gaps BEFORE the exit => UNKNOWN; gaps AFTER an exit do not
    retroactively invalidate it. When neither barrier is hit, a TIME exit
    needs all `n` consecutive future bars. Same-bar TP+SL => SL first.

    Retains the V11.14.x NEXT 5-minute candle opening entry, assumptions for
    target / stop price, fees / slippage and minute counting; this changes
    ONLY the ordering of the historical completeness check.
    """
    n = cfg['bars']
    if index < 0 or index >= len(rows) or n <= 0:
        return {'outcome': 'UNKNOWN', 'reason': '입력 봉 범위 오류'}
    if index + 1 >= len(rows):
        return {'outcome': 'UNKNOWN', 'reason': '진입 다음 봉 부족'}
    step = timedelta(minutes=5)
    # Enter only if the NEXT observed bar is the actual next 5-minute slot.
    if rows[index+1]['t'] - rows[index]['t'] != step:
        return {'outcome': 'UNKNOWN', 'reason': '진입 전 봉 공백'}

    entry = rows[index+1]['o']
    target = entry*(1+cfg['tp']/100)
    stop = entry*(1-cfg['sl']/100)
    outcome = None
    exit_price = None
    elapsed = None
    last = index

    for j in range(index+1, index+n+1):
        if j >= len(rows):
            return {'outcome': 'UNKNOWN', 'reason': '청산 전 미래 봉 부족'}
        # Gaps are unknown even when the observed next bar later reaches TP/SL.
        if rows[j]['t'] - rows[last]['t'] != step:
            return {'outcome': 'UNKNOWN', 'reason': '청산 전 봉 공백'}
        last = j
        r = rows[j]
        # Conservative intrabar convention: if TP & SL both touched, SL wins.
        if r['l'] <= stop:
            outcome = 'SL'
            # A bar that opens below stop cannot assume an ideal stop fill.
            exit_price = min(stop, r['o'])
        elif r['h'] >= target:
            outcome = 'TP'
            exit_price = target
        elif j == index + n:
            outcome = 'TIME'
            exit_price = r['c']
        if outcome:
            elapsed = (j-index)*5
            break

    if outcome is None:
        return {'outcome': 'UNKNOWN', 'reason': '판정 불가'}
    cost_in = entry*(1+(cfg['fee']+cfg['slip'])/100)
    revenue_out = exit_price*(1-(cfg['fee']+cfg['slip'])/100)
    net = (revenue_out/cost_in-1)*100
    return {'outcome': outcome, 'entry': round(entry,10),
            'exit': round(exit_price,10), 'net_pct': round(net,4),
            'minutes': elapsed}

def c5_v111417_signal(rows, i):
    """Only completed 5-minute bars through i; no future prices.

    Prior close is strictly below its contemporary 5-close SMA.
    Current candle is bullish and closes strictly above its own 5-close SMA.
    SMA(5) must rise vs the preceding bar. Thus only FIRST above-close
    after a below-close qualifies; consecutive above candles do not retrigger.
    """
    if i < 5:
        return False
    prev_ma5 = sum(r['c'] for r in rows[i-5:i]) / 5
    now_ma5 = sum(r['c'] for r in rows[i-4:i+1]) / 5
    return (rows[i-1]['c'] < prev_ma5
            and rows[i]['c'] > rows[i]['o']
            and rows[i]['c'] > now_ma5
            and now_ma5 > prev_ma5)

def c5_beggs_v111430_quality(bar, box_top):
    """Completed breakout candle only; no future bar reference.

    These cutoffs are *experimental hypotheses*, not thresholds proposed by
    Lance Beggs.  Genuine 0-range candles are rejected without division.
    """
    width = bar['h']-bar['l']
    if width <= 0 or box_top <= 0:
        return {'accepted':False,'close_position':None,'body_ratio':None,
                'upper_wick_ratio':None,'break_margin_pct':None}
    close_pos = (bar['c']-bar['l'])/width
    body = (bar['c']-bar['o'])/width
    upper = (bar['h']-max(bar['c'],bar['o']))/width
    break_margin = 100*(bar['c']/box_top-1)
    eligible = (close_pos >= .70 and body >= .40 and upper <= .30
                and break_margin >= .02)
    return {'accepted':eligible,'close_position':round(close_pos,5),
            'body_ratio':round(body,5),'upper_wick_ratio':round(upper,5),
            'break_margin_pct':round(break_margin,5)}

def c5_beggs_v111430_evaluate_rows(rows, cfg, market, kst):
    """Evaluate same historical Volman parent events as V11.14.29.

    B is a filter applied to the ORIGINAL A entry time/price. C is a
    one-candle delayed confirmation: signal at i+1, buy open at i+2.
    In particular C does not secretly 'filter' an entry at time i+1 using
    information from i+1; this avoids look-ahead bias.
    """
    from datetime import datetime as _dt, time as _tm
    day_start = _dt.combine(cfg['day'], _tm.min, tzinfo=kst)
    day_end = day_start+timedelta(days=1)
    step = timedelta(minutes=5)
    events = {'A':[], 'B':[], 'C':[]}
    eligible = 0
    c5_count = 0
    last_c5 = -1000000

    for i in range(LAB_WARMUP_BARS, len(rows)-1):
        if not day_start <= rows[i]['t'] < day_end:
            continue
        if not lab_contiguous(rows, i-25, i):
            continue
        eligible += 1
        if i-last_c5 < cfg['bars'] or not c5_v111417_signal(rows,i):
            continue
        last_c5 = i
        c5_count += 1
        if c5_expert_v111422_gates(rows,i)['volman'] is not True:
            continue
        box_top = max(r['h'] for r in rows[i-6:i])
        base_time = rows[i]['t'].strftime('%Y-%m-%d %H:%M')
        base_id = market+'|'+base_time
        quality = c5_beggs_v111430_quality(rows[i],box_top)
        shared = {
            'market':market,'base_id':base_id,'base_signal_time_kst':base_time,
            'box_high':round(box_top,10),
            'close_position':quality['close_position'],
            'body_ratio':quality['body_ratio'],
            'upper_wick_ratio':quality['upper_wick_ratio'],
            'break_margin_pct':quality['break_margin_pct'],
        }

        def make(arm, signal_i=None, reason='', outcome='NO_ENTRY'):
            ev = dict(shared)
            ev.update(strategy=arm,
                      signal_time_kst=(rows[signal_i]['t'].strftime('%Y-%m-%d %H:%M')
                                       if signal_i is not None else ''),
                      entry_time_kst=((rows[signal_i]['t']+step).strftime('%Y-%m-%d %H:%M')
                                      if signal_i is not None else ''),
                      entry_delay_minutes=((signal_i-i)*5 if signal_i is not None else None),
                      reason=reason)
            if signal_i is None:
                ev.update(outcome=outcome,net_pct=None,entry=None,exit=None,minutes=None)
            else:
                ev.update(lab_outcome_v11145(rows,signal_i,cfg))
            return ev

        baseline=make('A',i)
        events['A'].append(baseline)
        if quality['accepted']:
            # This is the identical entry and outcome as A; filter only.
            events['B'].append(make('B',i))
        else:
            events['B'].append(make('B',reason='돌파봉 힘 조건 미충족'))

        if i+1 >= len(rows) or rows[i+1]['t']-rows[i]['t'] != step:
            events['C'].append(make('C',reason='다음 완료 5분봉이 없어 확인 불가',
                                    outcome='UNKNOWN'))
        else:
            next_bar=rows[i+1]
            # 0.25% wick tolerance near the original six-bar box ceiling;
            # closing above it is required to establish price acceptance.
            accepted=(next_bar['c'] > box_top and
                      next_bar['l'] >= box_top*(1-.0025))
            if accepted:
                events['C'].append(make('C',i+1))
            else:
                events['C'].append(make('C',reason='다음 봉에서 돌파 가격 안착 실패'))

    return {'market':market,'observed_5m':len(rows),
            'eligible_windows':eligible,'c5_watches':c5_count,
            'baseline_signals':len(events['A']), 'events':events}

def c5_v111437_rsi2_wilder(closes):
    """Return last two Wilder RSI(2) values from a *continuous* close list.

    Seed gains/losses from first two deltas, then recursively smooth.
    No future closes and no prior segment through a missing candle.
    """
    if len(closes) < 4:
        return (None,None)
    changes=[closes[j]-closes[j-1] for j in range(1,len(closes))]
    gain=(max(changes[0],0)+max(changes[1],0))/2
    loss=(max(-changes[0],0)+max(-changes[1],0))/2
    def value():
        if gain==0 and loss==0:return 50.0
        if loss==0:return 100.0
        if gain==0:return 0.0
        return 100.0-100.0/(1.0+gain/loss)
    prior=value()
    current=prior
    for delta in changes[2:]:
        gain=(gain+max(delta,0))/2
        loss=(loss+max(-delta,0))/2
        prior,current=current,value()
    return prior,current

def c5_v111437_connors_signal(rows,i):
    """5m hypothesis: SMA50 rising, RSI2 crosses UP through 10 after oversold.

    Requires >=55 consecutive observed 5m bars to calculate SMA50 at i-3.
    Uses the actual *5-minute* SMA50, not Connors's *daily* SMA200.
    """
    if i<54 or not lab_contiguous(rows,i-54,i):
        return None
    recent=rows[i-54:i+1]
    closes=[x['c'] for x in recent]
    ma50=sum(closes[-50:])/50
    prev_ma50=sum(closes[-53:-3])/50
    if ma50<=0 or prev_ma50<=0:
        return None
    b=rows[i]
    if not (ma50>prev_ma50 and b['c']>ma50 and b['c']>b['o']
            and b['c']>rows[i-1]['c']):
        return None
    rsi_prev,rsi_now=c5_v111437_rsi2_wilder(closes)
    if (rsi_prev is None or rsi_now is None
            or not (rsi_prev<=10.0 and 10.0<rsi_now<=75.0)):
        return None
    vavg=sum(x['v'] for x in rows[i-20:i])/20
    if vavg<=0:return None
    vr=b['v']/vavg
    if vr<0.75:return None
    return {'pattern_name':'CONNORS_RSI2_5M_SMA50_ADAPTATION',
            'connors_rsi2_before':round(rsi_prev,4),
            'connors_rsi2_signal':round(rsi_now,4),
            'connors_sma50':round(ma50,10),
            'connors_sma50_slope3_pct':round(100*(ma50/prev_ma50-1),5),
            'connors_close_vs_sma50_pct':round(100*(b['c']/ma50-1),5),
            'pattern_volume_ratio':round(vr,4)}

def c5_expert_v111422_gates(rows, signal_i):
    """The original Volman boolean only, equivalent to V11.14.42 gate['volman'].

    Dropped the unrelated Raschke/ADX calculations; preserve the original
    contiguous-window seed, EMA20 seed and Volman cutoff exactly.
    """
    start=signal_i
    while (start>0 and signal_i-start<199 and
           rows[start]['t']-rows[start-1]['t']==timedelta(minutes=5)):
        start-=1
    win=rows[start:signal_i+1]
    if len(win)<21:
        return {'volman':None}
    closes=[r['c'] for r in win]
    ema=sum(closes[:20])/20
    k=2/21
    # Original EMA for prior bar (win[-2]); do not use signal bar here.
    for j in range(20,len(win)-1):
        ema=ema+k*(closes[j]-ema)
    prior=win[-7:-1]
    squeeze_top=max(b['h'] for b in prior)
    squeeze_bottom=min(b['l'] for b in prior)
    reference=sum(b['c'] for b in prior)/6
    if reference<=0:return {'volman':False}
    squeeze_range_pct=(squeeze_top-squeeze_bottom)/reference*100
    ema_near_box=bool(squeeze_bottom*0.997<=ema<=squeeze_top*1.003)
    volman=bool(squeeze_range_pct<=1.2 and ema_near_box
                and closes[-1]>squeeze_top and closes[-1]>win[-1]['o'])
    return {'volman':volman}

def c5_h2_v111434_fetch_extended(api_get,market,cfg,kst):
    """Fetch same raw candle endpoint with 6h rather than 4h prehistory.

    Same page size, exclusive KST cursor, validation, and missing-candle policy
    as lab_fetch_5m. No candle interpolation or price forward-fill. Returns
    meta for transparent audit; all prices are exchange observations.
    """
    from datetime import datetime as _dt,time as _tm
    begin=_dt.combine(cfg['day'],_tm.min,tzinfo=kst)-timedelta(hours=6)
    end=_dt.combine(cfg['day']+timedelta(days=1),_tm.min,tzinfo=kst)+timedelta(hours=2,minutes=5)
    cursor=end
    observed={}
    truncated=True
    pages_used=0
    raw_count=0
    skipped_count=0
    duplicate_count=0
    reason='PAGE_LIMIT'
    for _ in range(LAB_MAX_PAGES):
        raw=api_get('/v1/candles/minutes/5',{
            'market':market,'count':200,
            'to':cursor.strftime('%Y-%m-%d %H:%M:%S')})
        if not isinstance(raw,list):
            raise ValueError('6시간 사전 데이터 API 응답 형식 오류')
        pages_used+=1
        raw_count+=len(raw)
        oldest=None
        for bar in raw:
            if not isinstance(bar,dict):
                skipped_count+=1
                continue
            try:
                ts=local_time(bar['candle_date_time_kst'],kst)
                op,hi,lo,cl=(float(bar[k]) for k in
                    ('opening_price','high_price','low_price','trade_price'))
                vol=float(bar['candle_acc_trade_volume'])
                if not (all(isfinite(v) and v>0 for v in (op,hi,lo,cl))
                        and isfinite(vol) and vol>=0
                        and lo<=min(op,cl) and hi>=max(op,cl) and lo<=hi):
                    skipped_count+=1
                    continue
                if oldest is None or ts<oldest:
                    oldest=ts
                if begin<=ts<end:
                    if ts in observed:duplicate_count+=1
                    observed[ts]={'t':ts,'o':op,'h':hi,'l':lo,'c':cl,'v':vol}
            except (TypeError,ValueError,KeyError,OverflowError):
                skipped_count+=1
                continue
        if oldest is None:
            truncated=False;reason='NO_VALID_BARS'
            break
        if oldest<=begin:
            truncated=False;reason='BEGIN_REACHED'
            break
        if len(raw)<200:
            truncated=False;reason='SHORT_PAGE'
            break
        if oldest>=cursor:
            raise ValueError('6시간 사전 봉 조회 페이지가 전진하지 않습니다')
        cursor=oldest
    info={'fetch_5m_pages_used':pages_used,
          'fetch_5m_raw_records':raw_count,
          'fetch_5m_invalid_records':skipped_count,
          'fetch_5m_duplicates':duplicate_count,
          'fetch_5m_stop_reason':reason,
          'fetch_5m_page_limit_reached':truncated}
    return [observed[t] for t in sorted(observed)],truncated,info

def evaluate_core_rows(rows6,cfg,market,kst,meta=None):
    """A/B use the historical four-hour window; K uses six-hour window.

    This mirrors V11.14.42's A/B legacy-subset behavior without running any
    unrelated H2, O, trend, diagnosis or comparison sub-labs.
    """
    begin=datetime.combine(cfg['day'],_time.min,tzinfo=kst)-timedelta(hours=4)
    rows4=[r for r in rows6 if r['t']>=begin]
    ab=c5_beggs_v111430_evaluate_rows(rows4,cfg,market,kst)
    first=datetime.combine(cfg['day'],_time.min,tzinfo=kst)
    end=first+timedelta(days=1)
    kk=[];eligible=0;last=-1000000
    for i in range(54,len(rows6)-1):
        bar=rows6[i]
        if not (first<=bar['t']<end):continue
        if not lab_contiguous(rows6,i-54,i):continue
        eligible+=1
        if i-last<cfg['bars']:continue
        info=c5_v111437_connors_signal(rows6,i)
        if info is None:continue
        last=i
        stamp=bar['t'].strftime('%Y-%m-%d %H:%M')
        ev={'market':market,'strategy':'K',
            'base_id':market+'|K|'+stamp,
            'base_signal_time_kst':stamp,'signal_time_kst':stamp,
            'entry_time_kst':(bar['t']+timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M'),
            'entry_delay_minutes':0,'hypothetical_only':True,
            'comparison_group':'V111437_NEW_TRADER_INDEPENDENT',
            'reason':'코너스 응용: 5분봉 상승추세 RSI(2) 과매도 회복',**info}
        ev.update(lab_outcome_v11145(rows6,i,cfg))
        kk.append(ev)
    return {'market':market,'events':{'A':ab['events']['A'],
                                     'B':ab['events']['B'],'K':kk},
            'coverage':{'observed_5m':len(rows6),
                        'legacy_observed_5m':len(rows4),
                        'eligible_windows':ab['eligible_windows'],
                        'baseline_signals':ab['baseline_signals'],
                        'connors_k_eligible_positions':eligible,
                        **(meta or {})}}


CORE_PAGE = '<!doctype html><html lang="ko"><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>V2 · 핵심 3전략 경량 검증실</title>\n<style>\n:root{color-scheme:dark;--bg:#0b1020;--p:#141c2f;--l:#2a3951;--t:#edf3ff;--m:#9dabca;--g:#4ade80;--r:#ff788a}\n*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--t);font:14px/1.55 system-ui,\'Noto Sans KR\',sans-serif}\nmain{max-width:1080px;margin:auto;padding:20px 14px 50px}h1{font-size:23px;margin:0}.sub{color:var(--m);margin:5px 0 16px}\na{color:#accbff}.panel{background:var(--p);border:1px solid var(--l);border-radius:14px;padding:15px;margin-bottom:13px}\n.tools{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}.field{display:flex;flex-direction:column;gap:5px;min-width:135px}\nlabel{font-size:12px;color:var(--m)}input,select,button{border:1px solid #43536b;background:#10192e;border-radius:9px;color:var(--t);padding:10px;font:inherit}\nbutton{background:#3160dc;border-color:#3160dc;cursor:pointer;font-weight:700}button:disabled{opacity:.45;cursor:wait}\nbutton.secondary{background:#202d43;border-color:#43536b}.muted{color:var(--m);font-size:12px}\nprogress{width:100%;height:12px;margin:12px 0}table{border-collapse:collapse;width:100%;min-width:630px}th,td{padding:9px 7px;text-align:right;border-bottom:1px solid var(--l);font-size:12px;white-space:nowrap}\nth:first-child,td:first-child{text-align:left}.scroll{overflow-x:auto}.positive{color:var(--g)}.negative{color:var(--r)}\n.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.kpi{border:1px solid var(--l);border-radius:10px;padding:9px}.kpi b{display:block;font-size:21px}\n@media(max-width:540px){.kpis{grid-template-columns:1fr 1fr}h1{font-size:19px}}\n</style></head><body><main>\n<h1>🧪 V2 핵심 전략 검증실</h1><div class="sub">A 볼먼 · B 벡스 필터 · K 코너스 — 이전 40여 개 연구실 제거, 세 전략만 비교</div>\n<p><a href="/">← 기존 V2 실전 스캐너로 돌아가기</a></p>\n<section class="panel">\n<div class="tools">\n<div class="field"><label for="day">검사 날짜 (KST)</label><input type="date" id="day"></div>\n<div class="field"><label for="scope">검사 범위</label><select id="scope"><option value="20">거래대금 상위 20종목</option><option value="40" selected>상위 40종목</option><option value="80">상위 80종목</option><option value="all">전체 KRW 종목 (느릴 수 있음)</option></select></div>\n<div class="field"><label for="fee">편도 수수료 (%)</label><input type="number" min="0" max="0.5" step="0.01" value="0.04" id="fee"></div>\n<div class="field"><label for="slip">편도 슬리피지 (%)</label><input type="number" min="0" max="0.5" step="0.01" value="0.05" id="slip"></div>\n<button id="run">검사 시작</button><button class="secondary" id="csv" disabled>CSV 받기</button></div>\n<div class="muted">기본 상위 40종목만 검사해 빠른 비교에 집중. 전체 검사 선택 시 이전 연구처럼 시간이 오래 걸릴 수 있어.</div>\n<progress id="progress" value="0" max="100"></progress><div id="status">검사를 시작하세요.</div>\n</section>\n<section class="panel"><div class="kpis"><div class="kpi">검사 완료<b id="done">—</b></div><div class="kpi">API 오류<b id="errors">—</b></div><div class="kpi">경과 시간<b id="elapsed">—</b></div></div></section>\n<section class="panel"><h2>전략별 결과</h2><div class="scroll"><table><thead><tr><th>전략</th><th>진입</th><th>결과확인</th><th>수익</th><th>손실</th><th>미확인</th><th>수익거래율</th><th>평균 순수익률</th></tr></thead><tbody id="summary"><tr><td colspan="8">검사 전</td></tr></tbody></table></div></section>\n<section class="panel"><h2>거래 상세 (최대 200건 표시)</h2><div class="scroll"><table><thead><tr><th>코인/전략</th><th>신호 시각</th><th>결과</th><th>순수익률</th></tr></thead><tbody id="details"><tr><td colspan="4">검사 전</td></tr></tbody></table></div></section>\n<section class="panel"><div class="muted">B는 A형 볼먼 신호를 걸러내는 부분집합이며 독립 전략이 아니야. K는 코너스의 일봉 원형이 아닌 5분봉 SMA50·RSI(2) 변형이야. A/B/K는 과거 완료봉에서만 신호를 판정하고 다음 실제 5분봉 시가에 가상 진입해. TP +1.5%, SL -1.0%, 최대 60분. 동일 봉 TP·SL 동시 도달 시 SL을 우선해. 없는 봉을 생성하지 않아. 실제 V2의 BUY·알림·기록·주문에는 영향을 주지 않아.</div></section>\n</main><script>\nconst $=id=>document.getElementById(id),esc=x=>String(x??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nlet responses=[],failures=[],started=0;\nconst ymd=()=>{const parts=new Intl.DateTimeFormat(\'en-CA\',{timeZone:\'Asia/Seoul\',year:\'numeric\',month:\'2-digit\',day:\'2-digit\'}).formatToParts(new Date());const g=k=>parts.find(x=>x.type===k)?.value;return `${g(\'year\')}-${g(\'month\')}-${g(\'day\')}`};\nconst today=ymd(),dt=new Date(today+\'T00:00:00+09:00\');dt.setDate(dt.getDate()-1);const yesterday=new Intl.DateTimeFormat(\'en-CA\',{timeZone:\'Asia/Seoul\',year:\'numeric\',month:\'2-digit\',day:\'2-digit\'}).formatToParts(dt);const q=k=>yesterday.find(x=>x.type===k)?.value;\n$(\'day\').value=`${q(\'year\')}-${q(\'month\')}-${q(\'day\')}`;$(\'day\').max=$(\'day\').value;\nfunction stats(events){const v=events.filter(e=>e.entry_time_kst&&e.outcome!==\'NO_ENTRY\');const x=v.filter(e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&Number.isFinite(Number(e.net_pct)));const w=x.filter(e=>Number(e.net_pct)>0).length;return {entry:v.length,done:x.length,wins:w,loss:x.length-w,unknown:v.filter(e=>e.outcome===\'UNKNOWN\').length,avg:x.length?x.reduce((s,e)=>s+Number(e.net_pct),0)/x.length:NaN};}\nconst pct=x=>Number.isFinite(x)?x.toFixed(3)+\'%\':\'—\';\nfunction paint(){const arms=[[\'A\',\'볼먼 A\'],[\'B\',\'벡스 B (볼먼 필터)\'],[\'K\',\'코너스 K\']];const all=[];\n $(\'summary\').innerHTML=arms.map(([id,name])=>{const es=responses.flatMap(r=>r.events?.[id]||[]);all.push(...es.filter(e=>e.entry_time_kst&&e.outcome!==\'NO_ENTRY\'));const s=stats(es);return `<tr><td>${name}</td><td>${s.entry}</td><td>${s.done}</td><td class="positive">${s.wins}</td><td class="negative">${s.loss}</td><td>${s.unknown}</td><td>${s.done?(100*s.wins/s.done).toFixed(1)+\'%\':\'—\'}</td><td class="${s.avg>=0?\'positive\':\'negative\'}">${pct(s.avg)}</td></tr>`}).join(\'\');\n const sorted=all.sort((a,b)=>(a.signal_time_kst||\'\').localeCompare(b.signal_time_kst||\'\'));$(\'details\').innerHTML=sorted.slice(0,200).map(e=>`<tr><td>${esc(e.market)} / ${esc(e.strategy)}</td><td>${esc(e.signal_time_kst)}</td><td>${esc(e.outcome)}</td><td>${pct(Number(e.net_pct))}</td></tr>`).join(\'\')||\'<tr><td colspan="4">신호 없음</td></tr>\';\n $(\'done\').textContent=responses.length;$(\'errors\').textContent=failures.length;$(\'elapsed\').textContent=((performance.now()-started)/1000).toFixed(1)+\'초\';}\nasync function api(url,data){const ctl=new AbortController(),to=setTimeout(()=>ctl.abort(),55000);try{const r=await fetch(url,{method:data?\'POST\':\'GET\',headers:{\'Content-Type\':\'application/json\'},body:data?JSON.stringify(data):undefined,cache:\'no-store\',signal:ctl.signal});const obj=await r.json();if(!r.ok||!obj.ok)throw Error(obj.error||`HTTP ${r.status}`);return obj;}finally{clearTimeout(to)}}\n$(\'run\').onclick=async()=>{if($(\'run\').disabled)return;const day=$(\'day\').value,scope=$(\'scope\').value,fee=Number($(\'fee\').value),slip=Number($(\'slip\').value);\n if(!day||!Number.isFinite(fee)||!Number.isFinite(slip)||fee<0||fee>0.5||slip<0||slip>0.5)return alert(\'날짜·비용 설정을 확인하세요.\');\n $(\'run\').disabled=true;$(\'csv\').disabled=true;responses=[];failures=[];started=performance.now();paint();$(\'status\').textContent=\'종목 목록 준비 중\';\n try{const info=await api(\'/api/v2core/universe\');const count=scope===\'all\'?info.items.length:Number(scope);const markets=info.items.slice(0,count);let cursor=0,done=0;const total=markets.length;$(\'progress\').max=total;$(\'progress\').value=0;\n async function work(){while(cursor<total){const market=markets[cursor++];try{const out=await api(\'/api/v2core/evaluate\',{market,date:day,fee,slip});responses.push(out.result)}catch(e){failures.push({market,error:String(e.message||e)})}\n done++;$(\'progress\').value=done;$(\'status\').textContent=`${done}/${total} 검사 · 성공 ${responses.length} · 오류 ${failures.length}`;if(done%5===0||done===total)paint();}}\n await Promise.all(Array.from({length:Math.min(4,total)},work));paint();$(\'csv\').disabled=responses.length===0;$(\'status\').textContent=failures.length?`일부 오류 ${failures.length}건. CSV에는 성공한 종목만 포함됩니다. 누락을 확인하세요.`:`✅ 완료 ${responses.length}종목, 오류 0건`;\n }catch(e){$(\'status\').textContent=\'검사 중단: \'+e.message;}finally{$(\'run\').disabled=false;}};\nconst csvCell=x=>\'"\'+String(x??\'\').replace(/"/g,\'""\')+\'"\';\n$(\'csv\').onclick=()=>{const cols=[\'version\',\'date\',\'market\',\'strategy\',\'base_id\',\'signal_time_kst\',\'entry_time_kst\',\'outcome\',\'entry\',\'exit\',\'net_pct\',\'minutes\',\'reason\',\'close_position\',\'body_ratio\',\'upper_wick_ratio\',\'break_margin_pct\',\'connors_rsi2_before\',\'connors_rsi2_signal\',\'connors_sma50\',\'connors_sma50_slope3_pct\',\'pattern_volume_ratio\'];const lines=[cols.join(\',\')];\n for(const r of responses){for(const id of [\'A\',\'B\',\'K\'])for(const e of r.events?.[id]||[]){const row={version:\'V2.CORE.1\',date:r.date,...e,strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const e of failures)lines.push(cols.map(k=>csvCell(({version:\'V2.CORE.1\',date:$(\'day\').value,market:e.market,strategy:\'ERROR\',reason:e.error})[k])).join(\',\'));\n const blob=new Blob([\'\\ufeff\'+lines.join(\'\\n\')],{type:\'text/csv;charset=utf-8\'}),url=URL.createObjectURL(blob),a=document.createElement(\'a\');a.href=url;a.download=\'bithumb_V2_CORE_ABK.csv\';a.click();setTimeout(()=>URL.revokeObjectURL(url),3000);};\n</script></body></html>'

def register_daytrade_review(app, deps):
    """ONE lab, ONE registration point. Main V2 scanner is untouched."""
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache = {'at': 0., 'markets': [], 'results': {}, 'ticker_at': 0., 'sorted': []}
    lock = Lock()

    @app.get('/backtest-daytrade')
    @app.get('/v2-core-review')
    def v2_core_page():
        return render_template_string(CORE_PAGE)

    @app.get('/api/v2core/universe')
    def v2_core_universe():
        try:
            now=time.monotonic()
            with lock:
                if now-cache['at']<600 and cache['sorted']:
                    return jsonify(ok=True,version=VERSION,items=cache['sorted'])
            markets=sorted({m for m in get_markets() if isinstance(m,str) and MARKET_RE.fullmatch(m)})
            if not markets:raise ValueError('KRW 마켓 정보 없음')
            # Reuse V2 ticker cache when possible; otherwise fetch once per 10 minutes.
            tick={}
            main_cache=deps.get('CACHE',{})
            if (isinstance(main_cache,dict) and time.time()-main_cache.get('tickers_at',0)<300):
                tick=dict(main_cache.get('tickers') or {})
            if len(tick)<len(markets):
                for i in range(0,len(markets),50):
                    try:
                        raw=api_get('/v1/ticker',{'markets':','.join(markets[i:i+50])})
                        if isinstance(raw,list):
                            tick.update({t['market']:t for t in raw if isinstance(t,dict) and t.get('market')})
                    except Exception:
                        pass
            ranked=sorted(markets,key=lambda m:(-float((tick.get(m) or {}).get('acc_trade_price_24h') or 0),m))
            with lock:
                cache.update(at=now,sorted=ranked,markets=markets)
            return jsonify(ok=True,version=VERSION,items=ranked)
        except Exception as exc:
            return jsonify(ok=False,error=(type(exc).__name__+': '+str(exc))[:160]),400

    @app.post('/api/v2core/evaluate')
    def v2_core_evaluate():
        try:
            raw=request.get_json(silent=True)
            if not isinstance(raw,dict):raise ValueError('요청 형식 오류')
            market=raw.get('market')
            if not isinstance(market,str) or not MARKET_RE.fullmatch(market):
                raise ValueError('KRW 마켓 형식 오류')
            cfg=lab_valid_request({'date':raw.get('date'),'tp':1.5,'sl':1.0,
                     'horizon':60,'fee':raw.get('fee',0.04),'slip':raw.get('slip',0.05)},kst)
            with lock:
                old=cache['results'].get((market,cfg['day'],cfg['fee'],cfg['slip']))
                valid=market in cache['markets'] if cache['markets'] and time.monotonic()-cache['at']<600 else None
            if valid is False:raise ValueError('현재 KRW 마켓 목록에 없음')
            if valid is None and market not in get_markets():raise ValueError('현재 KRW 마켓 목록에 없음')
            if old is not None and time.monotonic()-old[0]<600:
                return jsonify(ok=True,date=cfg['day'].isoformat(),cached=True,result=old[1])
            rows,truncated,meta=c5_h2_v111434_fetch_extended(api_get,market,cfg,kst)
            data=evaluate_core_rows(rows,cfg,market,kst,meta)
            data['truncated']=truncated
            data['date']=cfg['day'].isoformat()
            key=(market,cfg['day'],cfg['fee'],cfg['slip'])
            with lock:
                if len(cache['results'])>1200:
                    cache['results'].clear()
                cache['results'][key]=(time.monotonic(),data)
            return jsonify(ok=True,date=cfg['day'].isoformat(),result=data,cached=False)
        except Exception as exc:
            return jsonify(ok=False,error=(type(exc).__name__+': '+str(exc))[:180]),400
