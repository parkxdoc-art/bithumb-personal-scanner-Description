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
VERSION = 'V2.CORE.4'

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
    result = {'market':market,'events':{'A':ab['events']['A'],
                                     'B':ab['events']['B'],'K':kk},
            'coverage':{'observed_5m':len(rows6),
                        'legacy_observed_5m':len(rows4),
                        'eligible_windows':ab['eligible_windows'],
                        'baseline_signals':ab['baseline_signals'],
                        'connors_k_eligible_positions':eligible,
                        **(meta or {})}}
    # Shadow decisions use ONLY the same already-fetched market candles.
    # All original events/metrics remain byte-for-byte equivalent to CORE.1.
    result['shadow'] = evaluate_volume_shadow(rows6, result['events'])
    # CORE.3: read ONLY diagnostics already computed by CORE.2; no data calls.
    result['shadow_trend_atr'] = evaluate_trend_atr_shadow(result['shadow'])
    # CORE.4: historical candle quality from parent A/B; no candle re-fetch.
    result['shadow_breakout'] = evaluate_breakout_quality_shadow(result['shadow_trend_atr'])
    return result



# ---------------------------------------------------------------------------
# V2 CORE.2: observational shadow filters (NOT used by live V2 BUY).
# Fixed predeclared threshold: current completed candle volume >= 1.2x
# average of 20 PREVIOUS completed 5-minute candle volumes.
# Decisions reuse original A/B/K outcome rather than entering late.
# No exchange calls; no interpolation; no future data.
# ---------------------------------------------------------------------------
V2_SHADOW_MIN_VOLUME_RATIO = 1.20
V2_SHADOW_ARM_MAP = {'A_V': 'A', 'B_V': 'B', 'K_V': 'K'}


def v2_core_bar_diagnostics(rows, i):
    """Observed completed 5m candle at i, with only preceding observations.

    Missing prehistory is NOT implicitly treated as a failed volume signal.
    EMA20 slope and ATR14 are descriptive diagnostics, not entry filters.
    """
    data = {'volume_ratio20': None, 'ema20_slope3_pct': None,
            'atr14_pct': None, 'close_position': None}
    if not (0 <= i < len(rows)):
        return data
    bar = rows[i]
    if bar['h'] > bar['l']:
        data['close_position'] = round((bar['c']-bar['l'])/(bar['h']-bar['l']), 5)
    if i < 20 or not lab_contiguous(rows, i-20, i):
        return data
    volume_avg = sum(b['v'] for b in rows[i-20:i])/20
    if volume_avg > 0:
        data['volume_ratio20'] = round(bar['v']/volume_avg, 6)
    if i >= 25 and lab_contiguous(rows, i-25, i):
        candles=rows[i-25:i+1]
        closes=[r['c'] for r in candles]
        ema = sum(closes[:20])/20
        alpha=2/21
        previous_ema=None
        for pos in range(20,26):
            ema = alpha*closes[pos]+(1-alpha)*ema
            if pos==22:
                previous_ema=ema
        if previous_ema and previous_ema>0:
            data['ema20_slope3_pct']=round(100*(ema/previous_ema-1), 5)
    trs=[]
    for j in range(i-13, i+1):
        r=rows[j]
        prior_close=rows[j-1]['c']
        trs.append(max(r['h']-r['l'], abs(r['h']-prior_close), abs(r['l']-prior_close)))
    if bar['c']>0:
        data['atr14_pct']=round(100*sum(trs)/14/bar['c'], 5)
    return data


def evaluate_volume_shadow(rows, original_events):
    """A/B/K parent outcomes copied, never recomputed or modified.

    Each row is audited, including rejected profit signals and UNKNOWNs.
    Original A/B/K events and original live BUY remain unchanged.
    """
    positions={r['t'].strftime('%Y-%m-%d %H:%M'):i for i,r in enumerate(rows)}
    by_time={}
    result={arm: [] for arm in V2_SHADOW_ARM_MAP}
    for arm, source in V2_SHADOW_ARM_MAP.items():
        for base in original_events.get(source, []):
            # Beggs rejected A-candidates have no actual B entry. Do not
            # miscount them as B shadow rejects, wins, or avoided losses.
            if not base.get('entry_time_kst') or base.get('outcome')=='NO_ENTRY':
                continue
            stamp=base.get('signal_time_kst')
            if stamp not in by_time:
                pos=positions.get(stamp)
                by_time[stamp]=v2_core_bar_diagnostics(rows,pos) if pos is not None else {
                    'volume_ratio20':None,'ema20_slope3_pct':None,
                    'atr14_pct':None,'close_position':None}
            diagnostics=by_time[stamp]
            ratio=diagnostics['volume_ratio20']
            if ratio is None:
                decision='NOT_EVALUABLE'
                reason='직전 20개 연속 5분봉 또는 유효 거래량 부족'
            elif ratio>=V2_SHADOW_MIN_VOLUME_RATIO:
                decision='PASS'
                reason='직전 20봉 평균 거래량의 1.2배 이상'
            else:
                decision='REJECT'
                reason='거래량 1.2배 미만'
            # Keep raw parent outcome as a counterfactual baseline even when
            # REJECT, so the UI can count missed wins AND avoided losses.
            item={**base,
                  'strategy':arm,
                  'source_strategy':source,
                  'shadow_threshold':V2_SHADOW_MIN_VOLUME_RATIO,
                  'filter_status':decision,
                  'filter_pass':decision=='PASS',
                  'filter_reason':reason,
                  'volume_ratio20':diagnostics['volume_ratio20'],
                  'ema20_slope3_pct':diagnostics['ema20_slope3_pct'],
                  'atr14_pct':diagnostics['atr14_pct']}
            result[arm].append(item)
    return result


# ---------------------------------------------------------------------------
# V2 CORE.3: frozen observational hypotheses from Nov 5-13 discovery data.
# A_E, B_E: completed signal-bar EMA20 3-bar slope > 0%.
# K_ATR: completed signal-bar ATR14 / close < 0.5%.
# The *identical* metrics and entries from CORE.2 are reused. In particular,
# these are not new entry signals and must not change the main V2 BUY.
# Missing metrics are UNKNOWN filter decisions, never forced REJECT.
# ---------------------------------------------------------------------------
CORE3_RULES = {
    'A_E': ('A_V', 'A', 'ema20_slope3_pct', 'gt', 0.0),
    'B_E': ('B_V', 'B', 'ema20_slope3_pct', 'gt', 0.0),
    'K_ATR': ('K_V', 'K', 'atr14_pct', 'lt', 0.5),
}


def evaluate_trend_atr_shadow(volume_shadows):
    """Counterfactual filter with shared CORE.2 diagnostics and parent PnL.

    Each incoming row represents a *real* A/B/K parent entry including its
    historical outcome. Only CORE.2's volume PASS/REJECT/NOT_EVALUABLE
    decision is replaced. No overlap selection, re-entry or future bar lookup.
    """
    output={name: [] for name in CORE3_RULES}
    for name,(volume_arm,parent,metric,operator,threshold) in CORE3_RULES.items():
        for old in volume_shadows.get(volume_arm, []):
            value=old.get(metric)
            if value is None or not isfinite(value):
                decision='NOT_EVALUABLE'
                reason='필요한 연속 5분봉 또는 진단 수치 없음'
            elif operator=='gt':
                decision='PASS' if value>threshold else 'REJECT'
                reason='EMA20 3봉 기울기 양수' if decision=='PASS' else 'EMA20 3봉 기울기 0% 이하'
            else:
                decision='PASS' if value<threshold else 'REJECT'
                reason='ATR(14) / 종가 0.5% 미만' if decision=='PASS' else 'ATR(14) / 종가 0.5% 이상'
            # Remove ONLY the old filter's metadata; keep untouched trades,
            # entry time, net PnL and diagnostic values for auditing.
            row={k:v for k,v in old.items()
                 if k not in ('shadow_threshold','filter_status','filter_pass','filter_reason')}
            row.update(strategy=name,source_strategy=parent,
                       filter_metric=metric,filter_operator=('>' if operator=='gt' else '<'),
                       filter_threshold=threshold,filter_value=value,
                       filter_status=decision,filter_pass=(decision=='PASS'),
                       filter_reason=reason)
            output[name].append(row)
    return output


# ---------------------------------------------------------------------------
# V2 CORE.4: breakout candle quality counterfactuals, on top of A_E / B_E.
# Frozen thresholds are simple *research hypotheses*, not fitted to returns.
# A_EQ: among EMA-passed A, body>=50%, upper wick<=20%, margin>=0.05%.
# B_EQ: among EMA-passed B, body>=70%, upper wick<=10%, margin>=0.10%.
# Metrics come from the SAME completed A/B trigger candle and its preceding
# six-bar box high; never from an entry or later candle.  NO extra API calls.
# BASE_REJECT/BASE_UNKNOWN are excluded from Q incremental avoided/missed
# counts, so we compare Q vs A_E/B_E, not Q vs unfiltered A/B by accident.
# ---------------------------------------------------------------------------
CORE4_QUALITY_RULES = {
    'A_EQ': ('A_E', 'A', 0.50, 0.20, 0.05),
    'B_EQ': ('B_E', 'B', 0.70, 0.10, 0.10),
}


def evaluate_breakout_quality_shadow(trend_shadows):
    """Read-only incremental shadow; original A/B/K and CORE.2/3 untouched."""
    arms={name:[] for name in CORE4_QUALITY_RULES}
    for name,(parent_arm,source,body_cut,wick_cut,margin_cut) in CORE4_QUALITY_RULES.items():
        for base in trend_shadows.get(parent_arm, []):
            quality={}
            diagnostics={}
            for key, operator, cut in (
                ('body_ratio', '>=', body_cut),
                ('upper_wick_ratio', '<=', wick_cut),
                ('break_margin_pct', '>=', margin_cut)):
                raw=base.get(key)
                valid=isinstance(raw,(int,float)) and not isinstance(raw,bool) and isfinite(raw)
                val=raw if valid else None
                diagnostics[key]=val
                quality[key]=(None if val is None else val>=cut if operator=='>=' else val<=cut)
            if base.get('filter_status')=='REJECT':
                status='BASE_REJECT'
                reason='EMA20 기존 필터에서 제외: Q의 추가 손실 회피 집계 제외'
            elif base.get('filter_status')!='PASS':
                status='BASE_UNKNOWN'
                reason='EMA20 기존 필터 판정 불가'
            elif any(val is None for val in quality.values()):
                status='NOT_EVALUABLE'
                reason='돌파봉 몸통/윗꼬리/돌파 폭 진단값 부족'
            elif all(quality.values()):
                status='PASS'
                reason='EMA20 통과 + 돌파봉 강도 3조건 통과'
            else:
                status='REJECT'
                reason='돌파봉 추가 조건 미충족: '+','.join(k for k,v in quality.items() if v is False)
            # Remove inherited EMA-specific decision metadata but retain actual
            # same-signal historical outcome, market, entry price, costs, etc.
            row={k:v for k,v in base.items() if k not in
                 ('filter_metric','filter_operator','filter_threshold','filter_value',
                  'filter_status','filter_pass','filter_reason')}
            row.update(strategy=name,source_strategy=source,
                       comparison_base=parent_arm,
                       quality_body_min=body_cut,quality_upper_wick_max=wick_cut,
                       quality_break_margin_min_pct=margin_cut,
                       quality_body_ok=quality['body_ratio'],
                       quality_upper_ok=quality['upper_wick_ratio'],
                       quality_margin_ok=quality['break_margin_pct'],
                       filter_status=status,filter_pass=(status=='PASS'),
                       filter_reason=reason)
            arms[name].append(row)
    return arms

CORE_PAGE = '<!doctype html><html lang="ko"><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>V2 CORE.4 · 핵심 3전략 + 돌파봉 품질 실험</title>\n<style>\n:root{color-scheme:dark;--bg:#0b1020;--p:#141c2f;--l:#2a3951;--t:#edf3ff;--m:#9dabca;--g:#4ade80;--r:#ff788a}\n*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--t);font:14px/1.55 system-ui,\'Noto Sans KR\',sans-serif}\nmain{max-width:1080px;margin:auto;padding:20px 14px 50px}h1{font-size:23px;margin:0}.sub{color:var(--m);margin:5px 0 16px}\na{color:#accbff}.panel{background:var(--p);border:1px solid var(--l);border-radius:14px;padding:15px;margin-bottom:13px}\n.tools{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}.field{display:flex;flex-direction:column;gap:5px;min-width:135px}\nlabel{font-size:12px;color:var(--m)}input,select,button{border:1px solid #43536b;background:#10192e;border-radius:9px;color:var(--t);padding:10px;font:inherit}\nbutton{background:#3160dc;border-color:#3160dc;cursor:pointer;font-weight:700}button:disabled{opacity:.45;cursor:wait}\nbutton.secondary{background:#202d43;border-color:#43536b}.muted{color:var(--m);font-size:12px}\nprogress{width:100%;height:12px;margin:12px 0}table{border-collapse:collapse;width:100%;min-width:630px}th,td{padding:9px 7px;text-align:right;border-bottom:1px solid var(--l);font-size:12px;white-space:nowrap}\nth:first-child,td:first-child{text-align:left}.scroll{overflow-x:auto}.positive{color:var(--g)}.negative{color:var(--r)}\n.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.kpi{border:1px solid var(--l);border-radius:10px;padding:9px}.kpi b{display:block;font-size:21px}\n@media(max-width:540px){.kpis{grid-template-columns:1fr 1fr}h1{font-size:19px}}\n</style></head><body><main>\n<h1>🧪 V2 CORE.4 · 3전략 손실 회피 연구</h1><div class="sub">A 볼먼 · B 벡스 필터 · K 코너스 — 기존 3전략 유지 · 거래량 1.2배 + EMA20·ATR + 돌파봉 몸통·윗꼬리·돌파 폭 비교</div>\n<p><a href="/">← 기존 V2 실전 스캐너로 돌아가기</a></p>\n<section class="panel">\n<div class="tools">\n<div class="field"><label for="day">검사 날짜 (KST)</label><input type="date" id="day"></div>\n<div class="field"><label for="scope">검사 범위</label><select id="scope"><option value="20">거래대금 상위 20종목</option><option value="40" selected>상위 40종목</option><option value="80">상위 80종목</option><option value="all">전체 KRW 종목 (느릴 수 있음)</option></select></div>\n<div class="field"><label for="fee">편도 수수료 (%)</label><input type="number" min="0" max="0.5" step="0.01" value="0.04" id="fee"></div>\n<div class="field"><label for="slip">편도 슬리피지 (%)</label><input type="number" min="0" max="0.5" step="0.01" value="0.05" id="slip"></div>\n<button id="run">검사 시작</button><button class="secondary" id="csv" disabled>CSV 받기</button></div>\n<div class="muted">기본 상위 40종목만 검사해 빠른 비교에 집중. 전체 검사 선택 시 이전 연구처럼 시간이 오래 걸릴 수 있어.</div>\n<progress id="progress" value="0" max="100"></progress><div id="status">검사를 시작하세요.</div>\n</section>\n<section class="panel"><div class="kpis"><div class="kpi">검사 완료<b id="done">—</b></div><div class="kpi">API 오류<b id="errors">—</b></div><div class="kpi">경과 시간<b id="elapsed">—</b></div></div></section>\n<section class="panel"><h2>전략별 결과</h2><div class="scroll"><table><thead><tr><th>전략</th><th>진입</th><th>결과확인</th><th>수익</th><th>손실</th><th>미확인</th><th>수익거래율</th><th>평균 순수익률</th></tr></thead><tbody id="summary"><tr><td colspan="8">검사 전</td></tr></tbody></table></div></section>\n<section class="panel"><h2>가상 필터: 거래량 1.2배</h2>\n<div class="muted">A_V / B_V / K_V는 기존 A / B / K 진입 중, 신호봉 거래량이 직전 20개 연속된 5분봉 평균의 1.2배 이상인 거래만 선택한 가상 비교야. 기존 BUY를 바꾸지 않아. 제외된 거래에서도 원래 결과를 유지하여 피한 손실/놓친 수익을 함께 계산해.</div>\n<div class="scroll"><table><thead><tr><th>가상 전략</th><th>통과</th><th>결과확인</th><th>수익</th><th>손실</th><th>평균 순수익률</th><th>피한 손실</th><th>놓친 수익</th><th>판정 불가</th></tr></thead><tbody id="shadow_summary"><tr><td colspan="9">검사 전</td></tr></tbody></table></div></section>\n<section class="panel"><h2>CORE.3 · EMA/ATR 가상 필터 실험</h2>\n<div class="muted">A_E · B_E: 신호 당시 EMA20의 3봉 기울기 &gt; 0%. K_ATR: 신호 당시 ATR(14) / 종가 &lt; 0.5%. 2026-11-05~13 자료를 살펴보고 선정한 조건이라 <b>그 기간 결과는 성능 검증으로 간주하지 않아.</b> CORE.3 검증에 10월 25~31일을 사용했고, CORE.4는 10월 17~23일 같은 미사용 날짜에서 시험해. 본래 A/B/K 진입·청산 결과는 변경되지 않아.</div>\n<div class="scroll"><table><thead><tr><th>가상 전략</th><th>통과</th><th>결과확인</th><th>수익</th><th>손실</th><th>수익거래율</th><th>평균 순수익률</th><th>피한 손실</th><th>놓친 수익</th><th>판정 불가</th></tr></thead><tbody id="trend_shadow_summary"><tr><td colspan="10">검사 전</td></tr></tbody></table></div>\n</section>\n<section class="panel"><h2>CORE.4 · 돌파봉 품질 (EMA 통과 후 추가 선별)</h2>\n<div class="muted">A_EQ: A_E 통과 + 양봉 몸통 ≥50%, 윗꼬리 ≤20%, 직전 6봉 박스 상단 대비 종가 돌파 폭 ≥0.05%. B_EQ: B_E 통과 + 몸통 ≥70%, 윗꼬리 ≤10%, 돌파 폭 ≥0.10%. 몸통·윗꼬리는 <b>돌파봉 전체 고저 범위 대비 비율</b>이며, 돌파 폭은 <b>박스 상단 대비 종가의 %</b>야. 수치는 단순 연구 가설이고 최적화된 값이 아니야.</div>\n<div class="scroll"><table><thead><tr><th>품질 필터</th><th>EMA 통과</th><th>Q 통과</th><th>청산 완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th><th>추가 피한 손실</th><th>추가 놓친 수익</th><th>판정 불가</th></tr></thead><tbody id="quality_summary"><tr><td colspan="11">검사 전</td></tr></tbody></table></div>\n<h3>돌파봉 세부 조건별 탐색 결과</h3>\n<div class="muted">각 조건 <b>한 가지씩</b> 적용할 때의 결과를 보여줘. 세 조건 전체 조합인 A_EQ/B_EQ와 다르며, 다른 두 조건과 독립적인 인과효과를 뜻하지는 않아. 분석 대상은 기존 EMA 필터를 통과한 완료 거래뿐이야.</div>\n<div class="scroll"><table><thead><tr><th>기준 전략</th><th>독립 조건</th><th>조건 통과 완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th><th>제외된 수익</th><th>제외된 손실</th></tr></thead><tbody id="quality_diagnostics"><tr><td colspan="9">검사 전</td></tr></tbody></table></div>\n<div class="muted">⚠️ 이번 품질 조건은 기존 10월·11월 데이터에서 아이디어를 얻은 만큼, <b>10월 17~23일과 같이 아직 연구에 쓰지 않은 날짜를 먼저 검사</b>해야 해. 화면 통계는 거래당 평균으로, 누적 수익률이나 실거래 성과가 아니야.</div>\n</section>\n<section class="panel"><h2>거래 상세 (기존 전략, 최대 200건 표시)</h2><div class="scroll"><table><thead><tr><th>코인/전략</th><th>신호 시각</th><th>결과</th><th>순수익률</th></tr></thead><tbody id="details"><tr><td colspan="4">검사 전</td></tr></tbody></table></div></section>\n<section class="panel"><div class="muted">B는 A형 볼먼 신호를 걸러내는 부분집합이며 독립 전략이 아니야. K는 코너스의 일봉 원형이 아닌 5분봉 SMA50·RSI(2) 변형이야. A/B/K는 과거 완료봉에서만 신호를 판정하고 다음 실제 5분봉 시가에 가상 진입해. TP +1.5%, SL -1.0%, 최대 60분. 동일 봉 TP·SL 동시 도달 시 SL을 우선해. 없는 봉을 생성하지 않아. 실제 V2의 BUY·알림·기록·주문에는 영향을 주지 않아. CORE.4 필터는 기존 CORE.3과 완료된 돌파봉의 진단값만 재사용해. 실전 BUY에는 적용하지 않았어. 가상 필터 수익률은 새로운 거래를 재진입한 결과가 아니라 같은 시점의 기존 거래에서 통과한 부분집합이야.</div></section>\n</main><script>\nconst $=id=>document.getElementById(id),esc=x=>String(x??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nlet responses=[],failures=[],started=0;\nconst ymd=()=>{const parts=new Intl.DateTimeFormat(\'en-CA\',{timeZone:\'Asia/Seoul\',year:\'numeric\',month:\'2-digit\',day:\'2-digit\'}).formatToParts(new Date());const g=k=>parts.find(x=>x.type===k)?.value;return `${g(\'year\')}-${g(\'month\')}-${g(\'day\')}`};\nconst today=ymd(),dt=new Date(today+\'T00:00:00+09:00\');dt.setDate(dt.getDate()-1);const yesterday=new Intl.DateTimeFormat(\'en-CA\',{timeZone:\'Asia/Seoul\',year:\'numeric\',month:\'2-digit\',day:\'2-digit\'}).formatToParts(dt);const q=k=>yesterday.find(x=>x.type===k)?.value;\n$(\'day\').value=`${q(\'year\')}-${q(\'month\')}-${q(\'day\')}`;$(\'day\').max=$(\'day\').value;\nfunction stats(events){const v=events.filter(e=>e.entry_time_kst&&e.outcome!==\'NO_ENTRY\');const x=v.filter(e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&Number.isFinite(Number(e.net_pct)));const w=x.filter(e=>Number(e.net_pct)>0).length;return {entry:v.length,done:x.length,wins:w,loss:x.length-w,unknown:v.filter(e=>e.outcome===\'UNKNOWN\').length,avg:x.length?x.reduce((s,e)=>s+Number(e.net_pct),0)/x.length:NaN};}\nconst pct=x=>Number.isFinite(x)?x.toFixed(3)+\'%\':\'—\';\nfunction paint(){const arms=[[\'A\',\'볼먼 A\'],[\'B\',\'벡스 B (볼먼 필터)\'],[\'K\',\'코너스 K\']];const all=[];\n $(\'summary\').innerHTML=arms.map(([id,name])=>{const es=responses.flatMap(r=>r.events?.[id]||[]);all.push(...es.filter(e=>e.entry_time_kst&&e.outcome!==\'NO_ENTRY\'));const s=stats(es);return `<tr><td>${name}</td><td>${s.entry}</td><td>${s.done}</td><td class="positive">${s.wins}</td><td class="negative">${s.loss}</td><td>${s.unknown}</td><td>${s.done?(100*s.wins/s.done).toFixed(1)+\'%\':\'—\'}</td><td class="${s.avg>=0?\'positive\':\'negative\'}">${pct(s.avg)}</td></tr>`}).join(\'\');\n const sorted=all.sort((a,b)=>(a.signal_time_kst||\'\').localeCompare(b.signal_time_kst||\'\'));$(\'details\').innerHTML=sorted.slice(0,200).map(e=>`<tr><td>${esc(e.market)} / ${esc(e.strategy)}</td><td>${esc(e.signal_time_kst)}</td><td>${esc(e.outcome)}</td><td>${e.net_pct==null?\'—\':pct(Number(e.net_pct))}</td></tr>`).join(\'\')||\'<tr><td colspan="4">신호 없음</td></tr>\';\n $(\'done\').textContent=responses.length;$(\'errors\').textContent=failures.length;$(\'elapsed\').textContent=((performance.now()-started)/1000).toFixed(1)+\'초\';paintShadow();paintTrendShadow();paintBreakout();}\n\nfunction paintShadow(){\n const arms=[[\'A_V\',\'볼먼 A + 거래량\'],[\'B_V\',\'벡스 B + 거래량\'],[\'K_V\',\'코너스 K + 거래량\']];\n const complete=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&e.net_pct!==undefined&&Number.isFinite(Number(e.net_pct));\n $(\'shadow_summary\').innerHTML=arms.map(([id,name])=>{\n  const all=responses.flatMap(r=>r.shadow?.[id]||[]);\n  const passed=all.filter(e=>e.filter_status===\'PASS\');\n  const outcome=passed.filter(complete);\n  const wins=outcome.filter(e=>Number(e.net_pct)>0).length;\n  const dropped=all.filter(e=>e.filter_status===\'REJECT\'&&complete(e));\n  const avoided=dropped.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=dropped.filter(e=>Number(e.net_pct)>0).length;\n  const unknown=all.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  const avg=outcome.length?outcome.reduce((s,e)=>s+Number(e.net_pct),0)/outcome.length:NaN;\n  return `<tr><td>${name}</td><td>${passed.length}</td><td>${outcome.length}</td><td class="positive">${wins}</td><td class="negative">${outcome.length-wins}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td><td>${avoided}</td><td>${missed}</td><td>${unknown}</td></tr>`;\n }).join(\'\');\n}\nfunction paintTrendShadow(){\n const arms=[[\'A_E\',\'볼먼 + EMA 상승\'],[\'B_E\',\'벡스 + EMA 상승\'],[\'K_ATR\',\'코너스 + 저변동성 ATR\']];\n const complete=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&e.net_pct!==undefined&&Number.isFinite(Number(e.net_pct));\n $(\'trend_shadow_summary\').innerHTML=arms.map(([id,name])=>{\n  const all=responses.flatMap(r=>r.shadow_trend_atr?.[id]||[]);\n  const passed=all.filter(e=>e.filter_status===\'PASS\');\n  const finished=passed.filter(complete);\n  const wins=finished.filter(e=>Number(e.net_pct)>0).length;\n  const rejected=all.filter(e=>e.filter_status===\'REJECT\'&&complete(e));\n  const avoided=rejected.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=rejected.filter(e=>Number(e.net_pct)>0).length;\n  const unknown=all.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  const avg=finished.length?finished.reduce((s,e)=>s+Number(e.net_pct),0)/finished.length:NaN;\n  return `<tr><td>${name}</td><td>${passed.length}</td><td>${finished.length}</td><td class="positive">${wins}</td><td class="negative">${finished.length-wins}</td><td>${finished.length?(100*wins/finished.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td><td>${avoided}</td><td>${missed}</td><td>${unknown}</td></tr>`;\n }).join(\'\');\n}\n\nfunction paintBreakout(){\n const arms=[[\'A_EQ\',\'볼먼 A_E + 강한 돌파봉\',\'A_E\'],[\'B_EQ\',\'벡스 B_E + 매우 강한 돌파봉\',\'B_E\']];\n const complete=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&e.net_pct!==undefined&&Number.isFinite(Number(e.net_pct));\n const avg=a=>a.length?a.reduce((s,e)=>s+Number(e.net_pct),0)/a.length:NaN;\n $(\'quality_summary\').innerHTML=arms.map(([id,name,parent])=>{\n  const all=responses.flatMap(r=>r.shadow_breakout?.[id]||[]);\n  const base=all.filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status));\n  const eligible=all.filter(e=>e.filter_status!==\'BASE_REJECT\'&&e.filter_status!==\'BASE_UNKNOWN\');\n  const passed=eligible.filter(e=>e.filter_status===\'PASS\');\n  const done=passed.filter(complete);const wins=done.filter(e=>Number(e.net_pct)>0).length;\n  const drop=eligible.filter(e=>e.filter_status===\'REJECT\'&&complete(e));\n  const avoided=drop.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=drop.filter(e=>Number(e.net_pct)>0).length;\n  const unjudge=eligible.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  const emaPassed=all.filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status)).length;\n  return `<tr><td>${name}</td><td>${emaPassed}</td><td>${passed.length}</td><td>${done.length}</td><td class="positive">${wins}</td><td class="negative">${done.length-wins}</td><td>${done.length?(100*wins/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg(done)>=0?\'positive\':\'negative\'}">${pct(avg(done))}</td><td>${avoided}</td><td>${missed}</td><td>${unjudge}</td></tr>`;\n }).join(\'\');\n const specs=[[\'quality_body_ok\',\'몸통 비율\'],[\'quality_upper_ok\',\'윗꼬리 비율\'],[\'quality_margin_ok\',\'돌파 폭\']];\n $(\'quality_diagnostics\').innerHTML=arms.flatMap(([id,name])=>{\n  const all=responses.flatMap(r=>r.shadow_breakout?.[id]||[]).filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status)&&complete(e));\n  return specs.map(([key,metric])=>{\n   const passed=all.filter(e=>e[key]===true),excluded=all.filter(e=>e[key]===false);\n   const wins=passed.filter(e=>Number(e.net_pct)>0).length;\n   const lose=passed.length-wins;\n   const mw=excluded.filter(e=>Number(e.net_pct)>0).length;\n   const al=excluded.filter(e=>Number(e.net_pct)<=0).length;\n   return `<tr><td>${name}</td><td>${metric}</td><td>${passed.length}</td><td class="positive">${wins}</td><td class="negative">${lose}</td><td>${passed.length?(100*wins/passed.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg(passed)>=0?\'positive\':\'negative\'}">${pct(avg(passed))}</td><td>${mw}</td><td>${al}</td></tr>`;\n  });\n }).join(\'\');\n}\nasync function api(url,data){const ctl=new AbortController(),to=setTimeout(()=>ctl.abort(),55000);try{const r=await fetch(url,{method:data?\'POST\':\'GET\',headers:{\'Content-Type\':\'application/json\'},body:data?JSON.stringify(data):undefined,cache:\'no-store\',signal:ctl.signal});const obj=await r.json();if(!r.ok||!obj.ok)throw Error(obj.error||`HTTP ${r.status}`);return obj;}finally{clearTimeout(to)}}\n$(\'run\').onclick=async()=>{if($(\'run\').disabled)return;const day=$(\'day\').value,scope=$(\'scope\').value,fee=Number($(\'fee\').value),slip=Number($(\'slip\').value);\n if(!day||!Number.isFinite(fee)||!Number.isFinite(slip)||fee<0||fee>0.5||slip<0||slip>0.5)return alert(\'날짜·비용 설정을 확인하세요.\');\n $(\'run\').disabled=true;$(\'csv\').disabled=true;responses=[];failures=[];started=performance.now();paint();$(\'status\').textContent=\'종목 목록 준비 중\';\n try{const info=await api(\'/api/v2core/universe\');const count=scope===\'all\'?info.items.length:Number(scope);const markets=info.items.slice(0,count);let cursor=0,done=0;const total=markets.length;$(\'progress\').max=total;$(\'progress\').value=0;\n async function work(){while(cursor<total){const market=markets[cursor++];try{const out=await api(\'/api/v2core/evaluate\',{market,date:day,fee,slip});responses.push(out.result)}catch(e){failures.push({market,error:String(e.message||e)})}\n done++;$(\'progress\').value=done;$(\'status\').textContent=`${done}/${total} 검사 · 성공 ${responses.length} · 오류 ${failures.length}`;if(done%5===0||done===total)paint();}}\n await Promise.all(Array.from({length:Math.min(4,total)},work));paint();$(\'csv\').disabled=responses.length===0;$(\'status\').textContent=failures.length?`일부 오류 ${failures.length}건. CSV에는 성공한 종목만 포함됩니다. 누락을 확인하세요.`:`✅ 완료 ${responses.length}종목, 오류 0건`;\n }catch(e){$(\'status\').textContent=\'검사 중단: \'+e.message;}finally{$(\'run\').disabled=false;}};\nconst csvCell=x=>\'"\'+String(x??\'\').replace(/"/g,\'""\')+\'"\';\n$(\'csv\').onclick=()=>{const cols=[\'version\',\'date\',\'market\',\'strategy\',\'base_id\',\'signal_time_kst\',\'entry_time_kst\',\'outcome\',\'entry\',\'exit\',\'net_pct\',\'minutes\',\'reason\',\'close_position\',\'body_ratio\',\'upper_wick_ratio\',\'break_margin_pct\',\'connors_rsi2_before\',\'connors_rsi2_signal\',\'connors_sma50\',\'connors_sma50_slope3_pct\',\'pattern_volume_ratio\',\'source_strategy\',\'filter_pass\',\'filter_status\',\'filter_reason\',\'shadow_threshold\',\'volume_ratio20\',\'ema20_slope3_pct\',\'atr14_pct\',\'filter_metric\',\'filter_operator\',\'filter_threshold\',\'filter_value\',\'comparison_base\',\'quality_body_min\',\'quality_upper_wick_max\',\'quality_break_margin_min_pct\',\'quality_body_ok\',\'quality_upper_ok\',\'quality_margin_ok\'];const lines=[cols.join(\',\')];\n for(const r of responses){for(const id of [\'A\',\'B\',\'K\'])for(const e of r.events?.[id]||[]){const row={version:\'V2.CORE.4\',date:r.date,...e,strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const id of [\'A_V\',\'B_V\',\'K_V\'])for(const e of r.shadow?.[id]||[]){const row={version:\'V2.CORE.4\',date:r.date,...e,strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const id of [\'A_E\',\'B_E\',\'K_ATR\'])for(const e of r.shadow_trend_atr?.[id]||[]){const row={version:\'V2.CORE.4\',date:r.date,...e,strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const id of [\'A_EQ\',\'B_EQ\'])for(const e of r.shadow_breakout?.[id]||[]){const row={version:\'V2.CORE.4\',date:r.date,...e,strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const e of failures)lines.push(cols.map(k=>csvCell(({version:\'V2.CORE.4\',date:$(\'day\').value,market:e.market,strategy:\'ERROR\',reason:e.error})[k])).join(\',\'));\n const blob=new Blob([\'\\ufeff\'+lines.join(\'\\n\')],{type:\'text/csv;charset=utf-8\'}),url=URL.createObjectURL(blob),a=document.createElement(\'a\');a.href=url;a.download=\'bithumb_V2_CORE4_BREAKOUT_QUALITY.csv\';a.click();setTimeout(()=>URL.revokeObjectURL(url),3000);};\n</script></body></html>'

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
