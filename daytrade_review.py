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
VERSION = 'V2.CORE.10'

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
    # CORE.5: read-only independent margin/body rule ablations.
    result['shadow_core5'] = evaluate_core5_separated_shadow(result['shadow_trend_atr'])
    # CORE.6: isolated local market-regime proxy and Beggs wick ablation.
    result['shadow_core6'] = evaluate_core6_regime_wick_shadow(
        rows6, result['shadow_breakout'], result['shadow_trend_atr'])
    # CORE.9 is observational only: parent events/shadows are unchanged.
    result['core9_paths'] = evaluate_core9_paths(rows6, result, cfg)
    # CORE.10 uses same raw candle data and leaves CORE.9 intact.
    result['core10_timeframes'] = evaluate_core10_timeframes(rows6, cfg, market, kst)
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



# ---------------------------------------------------------------------------
# V2 CORE.5: isolate breakout margin vs redundant body-strength restrictions.
# Compare directly to the CORE.3 EMA-pass parent, never CORE.4 outcome status.
# IMPORTANT: B already includes the original Beggs body>=40% and close-position
# filter. "No body" means remove ONLY CORE.4's EXTRA body>=70% threshold.
# Frozen research thresholds are hypotheses, not trading recommendations.
# All checks depend only on the completed original signal candle.
# ---------------------------------------------------------------------------
CORE5_SEPARATED_RULES = {
    # 1 factor: does minimum breakout amplitude alone help Volman?
    'A_EM': ('A_E', 'A', (('break_margin_pct', '>=', 0.05),)),
    # 2 factors: does removing CORE.4's new body>=70% help Beggs?
    'B_ENB': ('B_E', 'B', (('upper_wick_ratio', '<=', 0.10),
                            ('break_margin_pct', '>=', 0.10))),
    # 1 factor: isolate breakout amplitude on the same Beggs parent.
    'B_EM': ('B_E', 'B', (('break_margin_pct', '>=', 0.10),)),
}


def evaluate_core5_separated_shadow(trend_shadows):
    """Reclassify *only* the original EMA-filtered entries; never alter PnL.

    BASE_REJECT/BASE_UNKNOWN means the old EMA base didn't pass, and must
    NEVER be counted as a newly avoided loss or missed profit. Missing metrics
    are NOT_EVALUABLE rather than silently treated as rejected trades.
    """
    result = {name: [] for name in CORE5_SEPARATED_RULES}
    for name, (parent, original, checks) in CORE5_SEPARATED_RULES.items():
        for old in trend_shadows.get(parent, []):
            prior_status = old.get('filter_status')
            check_results = {}
            for metric, comparator, boundary in checks:
                value = old.get(metric)
                if (not isinstance(value, (int,float)) or
                        isinstance(value, bool) or not isfinite(value)):
                    check_results[metric] = None
                else:
                    check_results[metric] = (value >= boundary if comparator == '>='
                                             else value <= boundary)
            if prior_status == 'REJECT':
                status = 'BASE_REJECT'
                reason = 'EMA20 기본 필터 제외 (추가 회피 손실에 미포함)'
            elif prior_status != 'PASS':
                status = 'BASE_UNKNOWN'
                reason = 'EMA20 기본 필터 판정 불가'
            elif any(v is None for v in check_results.values()):
                status = 'NOT_EVALUABLE'
                reason = '필요한 돌파봉 진단값 부족'
            elif all(check_results.values()):
                status = 'PASS'
                reason = '사전 고정한 CORE.5 분리 필터 조건 충족'
            else:
                status = 'REJECT'
                reason = '분리 필터 미충족: ' + ','.join(
                    key for key,val in check_results.items() if val is False)
            row = {key: value for key,value in old.items()
                   if key not in ('filter_metric','filter_operator','filter_threshold',
                                  'filter_value','filter_status','filter_pass',
                                  'filter_reason')}
            row.update(strategy=name, source_strategy=original,
                       comparison_base=parent,
                       core5_rule_name=name,
                       core5_margin_ok=check_results.get('break_margin_pct'),
                       core5_wick_ok=check_results.get('upper_wick_ratio'),
                       core5_active_rules=' & '.join(
                           f'{key}{comp}{cut:g}' for key,comp,cut in checks),
                       filter_status=status, filter_pass=(status=='PASS'),
                       filter_reason=reason)
            result[name].append(row)
    return result


# ---------------------------------------------------------------------------
# V2 CORE.6: no extra exchange API calls. Split local trend/ATR regimes for
# Volman A_EQ and wick-only for Beggs B_E. Not an overall BTC/market regime.
# Comparison is always to its DIRECT parent (A_EQ or B_E): original A/B/K,
# CORE.2/3/4/5 trade outcomes and entry times are never modified.
# Previously selected thresholds are unvalidated research hypotheses.
# ---------------------------------------------------------------------------
CORE6_ATR_CUTOFF_PCT = 0.50
CORE6_WICK_MAX = 0.10
CORE6_RULES = {
    'A_EQ_TR': ('A_EQ', 'A', ('local_uptrend',)),
    'A_EQ_ATR': ('A_EQ', 'A', ('local_low_volatility',)),
    'A_EQ_R': ('A_EQ', 'A', ('local_uptrend', 'local_low_volatility')),
    'B_EW': ('B_E', 'B', ('wick_only',)),
}


def core6_local_context(rows, signal_i):
    """SMA50 + its 3-bar slope, based solely on continuous completed 5m bars.

    Uses same candle at signal_i, no entry or exit data. For missing historical
    segments, returns None instead of making a bullish/bearish guess.
    """
    info = {'local_sma50': None, 'local_sma50_slope3_pct': None,
            'local_close_vs_sma50_pct': None, 'local_uptrend': None,
            'local_regime': 'UNKNOWN'}
    if signal_i is None or signal_i < 54 or signal_i >= len(rows):
        return info
    if not lab_contiguous(rows, signal_i - 54, signal_i):
        return info
    last = rows[signal_i - 54:signal_i + 1]
    closes = [bar['c'] for bar in last]
    ma_now = sum(closes[-50:]) / 50
    ma_prev = sum(closes[-53:-3]) / 50
    close = closes[-1]
    if not (ma_now > 0 and ma_prev > 0 and close > 0):
        return info
    slope = 100 * (ma_now / ma_prev - 1)
    distance = 100 * (close / ma_now - 1)
    is_up = slope > 0 and distance > 0
    info.update(local_sma50=round(ma_now, 10),
                local_sma50_slope3_pct=round(slope, 6),
                local_close_vs_sma50_pct=round(distance, 6),
                local_uptrend=is_up,
                local_regime='UP' if is_up else 'OTHER')
    return info


def evaluate_core6_regime_wick_shadow(rows, breakout_shadows, trend_shadows):
    """Same trades, parent filter precedence, no new candle fetches.

    Pass/fail metrics depend only on information available at the completed
    original trigger. CONTEXT groups are descriptive, not true market-wide.
    """
    positions = {r['t'].strftime('%Y-%m-%d %H:%M'): i for i, r in enumerate(rows)}
    context_cache = {}
    result = {name: [] for name in CORE6_RULES}
    for name, (parent, source, checks) in CORE6_RULES.items():
        parent_rows = (breakout_shadows if parent == 'A_EQ' else trend_shadows).get(parent, [])
        for old in parent_rows:
            stamp = old.get('signal_time_kst')
            if stamp not in context_cache:
                context_cache[stamp] = core6_local_context(rows, positions.get(stamp))
            ctx = context_cache[stamp]
            atr = old.get('atr14_pct')
            atr_ok = (isinstance(atr, (float, int)) and not isinstance(atr, bool)
                      and isfinite(atr))
            vol_status = (atr < CORE6_ATR_CUTOFF_PCT) if atr_ok else None
            wick = old.get('upper_wick_ratio')
            wick_ok = (isinstance(wick, (float, int)) and not isinstance(wick, bool)
                       and isfinite(wick))
            wick_status = (wick <= CORE6_WICK_MAX) if wick_ok else None
            values = {'local_uptrend': ctx['local_uptrend'],
                      'local_low_volatility': vol_status,
                      'wick_only': wick_status}
            previous = old.get('filter_status')
            if previous in ('BASE_REJECT', 'REJECT'):
                status, reason = 'BASE_REJECT', '기존 부모 필터에서 이미 제외됨'
            elif previous != 'PASS':
                status, reason = 'BASE_UNKNOWN', '기존 부모 필터 판정 불가'
            elif any(values[k] is None for k in checks):
                status, reason = 'NOT_EVALUABLE', '연속 5분봉 또는 진단값 부족'
            elif all(values[k] for k in checks):
                status, reason = 'PASS', 'CORE.6 가상 조건 통과'
            else:
                status = 'REJECT'
                reason = 'CORE.6 미충족: ' + ','.join(k for k in checks if values[k] is False)
            row = {key: val for key, val in old.items()
                   if key not in ('filter_status', 'filter_pass', 'filter_reason')}
            row.update(strategy=name, source_strategy=source,
                       comparison_base=parent,
                       local_sma50=ctx['local_sma50'],
                       local_sma50_slope3_pct=ctx['local_sma50_slope3_pct'],
                       local_close_vs_sma50_pct=ctx['local_close_vs_sma50_pct'],
                       local_uptrend=ctx['local_uptrend'],
                       local_regime=ctx['local_regime'],
                       local_low_volatility=vol_status,
                       core6_atr_max_pct=CORE6_ATR_CUTOFF_PCT,
                       core6_wick_max=CORE6_WICK_MAX,
                       core6_wick_ok=wick_status,
                       core6_rule_name=name,
                       filter_status=status, filter_pass=(status == 'PASS'),
                       filter_reason=reason)
            result[name].append(row)
    return result


# ---------------------------------------------------------------------------
# CORE.7: independent BTC intraday market context (read-only research).
# This is a BTC proxy, NOT full KRW market breadth. 5m candle timestamps
# represent the start of the candle: context uses the BTC candle that closes
# at the SAME instant as each coin's signal candle, before the next-bar entry.
# No future BTC bars, no interpolation, no outcome-based optimization.
# ---------------------------------------------------------------------------
CORE7_BTC_RETURN_THRESHOLD_PCT = 0.30

def build_btc_hour_context(rows, cfg, kst):
    """Timestamp-keyed BTC 1h return/ATR for historical signal timestamps.

    Uses 13+1 consecutive completed candles including the signal candle,
    exactly 12 prior 5-minute intervals for return. Missing bars => UNKNOWN.
    The 0.30% classification is a predeclared exploratory threshold.
    """
    first = datetime.combine(cfg['day'], _time.min, tzinfo=kst)
    end = first + timedelta(days=1)
    result = {}
    for i, bar in enumerate(rows):
        ts = bar['t']
        if ts < first or ts >= end:
            continue
        stamp = ts.strftime('%Y-%m-%d %H:%M')
        value = {'btc_regime': 'UNKNOWN', 'btc_ret60_pct': None,
                 'btc_atr14_pct': None}
        if i >= 14 and lab_contiguous(rows, i-14, i):
            past = rows[i-12]['c']
            close = bar['c']
            if past > 0 and close > 0:
                move = (close / past - 1) * 100
                true_ranges = []
                for j in range(i-13, i+1):
                    cur, prev = rows[j], rows[j-1]
                    true_ranges.append(max(cur['h']-cur['l'],
                        abs(cur['h']-prev['c']), abs(cur['l']-prev['c'])))
                atr_pct = sum(true_ranges) / len(true_ranges) / close * 100
                if isfinite(move) and isfinite(atr_pct):
                    regime = ('BTC_UP' if move >= CORE7_BTC_RETURN_THRESHOLD_PCT
                              else 'BTC_DOWN' if move <= -CORE7_BTC_RETURN_THRESHOLD_PCT
                              else 'BTC_SIDE')
                    value.update(btc_regime=regime,
                                 btc_ret60_pct=round(move, 6),
                                 btc_atr14_pct=round(atr_pct, 6))
        result[stamp] = value
    return result


# ---------------------------------------------------------------------------
# CORE.9: research-only price-path diagnostics and paired exit simulations.
# The ENTRY signals and existing CORE.8 data are NOT changed.  Uses the SAME
# historical per-coin candles; no new market or BTC calls.  Unlike signal
# detection, a backtest legitimately observes bars AFTER entry to score it.
# Full-60m path MFE/MAE is unconditional (even after hypothetical stop/target).
# High-vs-low intrabar order is unknown; all simulations prefer SL in ties.
# ---------------------------------------------------------------------------
CORE9_TPS = (0.50, 0.75, 1.00, 1.50)
CORE9_SLS = (0.50, 0.75, 1.00)
CORE9_PARENTS = (('A_EQ', 'shadow_breakout'),
                 ('B_E', 'shadow_trend_atr'))


def core9_grid_id(tp, sl):
    return f'tp{int(round(tp*100)):03d}_sl{int(round(sl*100)):03d}'


def core9_analyze_event(rows, index, cfg, original):
    """Follow one historical signal's next 12+ observed 5m candles.

    MFE/MAE require a complete, contiguous 60m window and assume entry at
    the NEXT candle open (same as CORE.8).  End-of-15/30/60m returns use
    the last CLOSE of that horizon; all returns subtract the same costs.
    Alternate TP/SL simulations use the identical SL-first stop-gap model
    as existing `lab_outcome_v11145` and are scored only on FULL paths in UI.
    Delayed entry opens at the following 5m bar and needs 60m more history.
    """
    record = {'market': original.get('market'),
              'base_id': original.get('base_id'),
              'signal_time_kst': original.get('signal_time_kst'),
              'entry_time_kst': original.get('entry_time_kst'),
              'base_outcome': original.get('outcome'),
              'base_net_pct': original.get('net_pct'),
              'path_status': 'UNKNOWN', 'path_reason': '',
              'entry': None, 'mfe_pct': None, 'mae_pct': None,
              'mfe_first_min': None, 'mae_first_min': None,
              'net_15m_pct': None, 'net_30m_pct': None,
              'net_60m_pct': None, 'grid': {},
              'delay5_outcome': 'UNKNOWN', 'delay5_net_pct': None,
              'delay5_entry_time_kst': None}
    n = cfg['bars']
    if index is None or index < 0 or index + 1 >= len(rows):
        record['path_reason'] = '진입 다음 봉 없음'
        return record
    step = timedelta(minutes=5)
    if rows[index+1]['t'] - rows[index]['t'] != step:
        record['path_reason'] = '진입 전 봉 공백'
        return record
    entry = rows[index+1]['o']
    record['entry'] = round(entry, 10)
    if index + n >= len(rows):
        record['path_reason'] = '60분 가격 경로 봉 부족'
        return record
    if not lab_contiguous(rows, index, index+n):
        record['path_reason'] = '60분 가격 경로 봉 공백'
        return record
    after = rows[index+1:index+n+1]
    highs = [r['h'] for r in after]
    lows = [r['l'] for r in after]
    mfe = (max(highs)/entry-1)*100
    mae = (min(lows)/entry-1)*100
    record.update(path_status='FULL',path_reason='연속 60분 가격 경로 확보',
                  mfe_pct=round(mfe, 5),mae_pct=round(mae, 5),
                  mfe_first_min=(highs.index(max(highs))+1)*5,
                  mae_first_min=(lows.index(min(lows))+1)*5)
    cost = (cfg['fee']+cfg['slip'])/100
    for minute in (15,30,60):
        if minute > cfg['horizon']:
            continue
        bar = after[minute//5-1]
        net = ((bar['c']*(1-cost)) / (entry*(1+cost)) - 1)*100
        record[f'net_{minute}m_pct'] = round(net, 5)
    # Always preserve the original exit simulation at TP=1.5, SL=1.0.
    for tp in CORE9_TPS:
        for sl in CORE9_SLS:
            alt = dict(cfg, tp=tp, sl=sl)
            out = lab_outcome_v11145(rows, index, alt)
            key = core9_grid_id(tp, sl)
            record['grid'][key] = {'outcome':out.get('outcome'),
                                   'net_pct':out.get('net_pct'),
                                   'minutes':out.get('minutes')}
    # 5m execution delay is an ALTERNATIVE execution assumption, not a
    # confirmed better fill. It enters at bar i+2 open, holds 60m from there.
    if index+2 < len(rows) and rows[index+2]['t']-rows[index+1]['t'] == step:
        record['delay5_entry_time_kst'] = rows[index+2]['t'].strftime('%Y-%m-%d %H:%M')
    delayed = lab_outcome_v11145(rows, index+1, cfg)
    record['delay5_outcome'] = delayed.get('outcome')
    record['delay5_net_pct'] = delayed.get('net_pct')
    return record


def evaluate_core9_paths(rows, result, cfg):
    """Only EMA/quality-passed A_EQ/B_E, using their original signal time.

    The same A base_id can appear in B, hence never pool A and B trade counts.
    """
    lookup = {r['t'].strftime('%Y-%m-%d %H:%M'):i for i,r in enumerate(rows)}
    groups = {}
    for arm, category in CORE9_PARENTS:
        events = result.get(category, {}).get(arm, [])
        groups[arm] = [core9_analyze_event(rows,lookup.get(e.get('signal_time_kst')),
                                           cfg,e)
                       for e in events if e.get('filter_status') == 'PASS'
                       and e.get('entry_time_kst')]
    return groups



# ---------------------------------------------------------------------------
# V2 CORE.10: 3/5/10-period simple moving-average crosses on 5m/10m candles.
# No extra API calls: 10-minute candles are aggregated only from TWO verified
# consecutive completed 5-minute observations aligned to KST :00/:10 slots.
# Parent A is Volman-style price compression; B is its Beggs candle-quality
# subset. Signal timestamps represent the LAST 5-minute candle start within
# the completed signal period. Entry is the NEXT 5-minute candle opening.
# No live-BUY changes, no mixing of unlike candlestick futures for entry.
# ---------------------------------------------------------------------------
CORE10_MAS = (3, 5, 10)
CORE10_TIMEFRAMES = (5, 10)


def core10_strategy_id(arm, tf, ma):
    return '%s_%dM_MA%02d' % (arm, tf, ma)


def core10_resample_10m(rows5):
    """Build 10-minute OHLCV bars; drop incomplete/missing pairs.

    `t` is the first 5m candle's start, `last_5m_t` is the second one's
    start. All comparisons use COMPLETED bars. No padding or interpolation.
    """
    bytime = {r['t']: r for r in rows5}
    from datetime import timedelta as _td
    out = []
    for r in rows5:
        ts = r['t']
        if ts.minute % 10 != 0 or ts.second != 0 or ts.microsecond != 0:
            continue
        later = bytime.get(ts + _td(minutes=5))
        if later is None:
            continue
        if (ts + _td(minutes=5)).minute % 10 != 5:
            continue
        out.append({'t': ts, 'last_5m_t': later['t'],
                    'o': r['o'], 'h': max(r['h'], later['h']),
                    'l': min(r['l'], later['l']), 'c': later['c'],
                    'v': r['v'] + later['v']})
    return out


def core10_ma_cross(rows, i, ma):
    """Equivalent to the original five-period rule, generalized to ma."""
    if i < ma:
        return False
    prev = sum(x['c'] for x in rows[i-ma:i]) / ma
    now = sum(x['c'] for x in rows[i-ma+1:i+1]) / ma
    return bool(rows[i-1]['c'] < prev and rows[i]['c'] > rows[i]['o']
                and rows[i]['c'] > now and now > prev)


def core10_volman_gate(rows, i, tf):
    """Time-equivalent to original 5m box6/EMA20: 30m box/100m EMA.

    With 10-minute samples EMA10 is only approximately equivalent to 5m
    EMA20. We explicitly treat it as a different research hypothesis.
    """
    if tf == 5:
        return c5_expert_v111422_gates(rows, i)['volman'] is True
    box_bars = 3
    ema_bars = 10
    start = i
    step = timedelta(minutes=tf)
    while start > 0 and i-start < 199 and rows[start]['t']-rows[start-1]['t'] == step:
        start -= 1
    win = rows[start:i+1]
    if len(win) < ema_bars+1 or len(win) < box_bars+1:
        return False
    closes = [r['c'] for r in win]
    ema = sum(closes[:ema_bars]) / ema_bars
    k = 2 / (ema_bars+1)
    for j in range(ema_bars, len(win)-1):
        ema += k*(closes[j]-ema)
    prior = win[-(box_bars+1):-1]
    top = max(r['h'] for r in prior)
    bottom = min(r['l'] for r in prior)
    reference = sum(r['c'] for r in prior) / box_bars
    if reference <= 0:
        return False
    compressed = ((top-bottom)/reference*100 <= 1.2)
    ema_near = bottom*.997 <= ema <= top*1.003
    return bool(compressed and ema_near and closes[-1] > top
                and closes[-1] > win[-1]['o'])


def evaluate_core10_timeframes(rows6, cfg, market, kst):
    """Six SMA/timeframe configurations, A base and B quality subset.

    All six enter at the next *5m* open and exit under the unchanged
    1.5% TP/1% SL/60min rule including fee and slippage. Signals separated
    by 60 minutes per configuration. Historical signal detection sees no
    subsequent 5m bar. B does not create an independent trading signal.
    """
    from datetime import datetime as _dt, time as _tm, timedelta as _td
    begin = _dt.combine(cfg['day'], _tm.min, tzinfo=kst) - _td(hours=4)
    stop = _dt.combine(cfg['day'] + _td(days=1), _tm.min, tzinfo=kst)
    start_day = _dt.combine(cfg['day'], _tm.min, tzinfo=kst)
    rows5 = [r for r in rows6 if r['t'] >= begin]
    rows10 = core10_resample_10m(rows5)
    index5 = {r['t']:i for i,r in enumerate(rows6)}
    groups = {core10_strategy_id(arm,tf,ma):[]
              for tf in CORE10_TIMEFRAMES for ma in CORE10_MAS
              for arm in ('A','B')}
    coverage = {'observed_5m':len(rows5), 'complete_10m':len(rows10),
                'possible_10m_slots':sum(r['t'].minute%10==0 for r in rows5),
                'evaluated_positions':{}, 'base_crosses':{}}
    for tf, bars in ((5,rows5),(10,rows10)):
        box = 6 if tf == 5 else 3
        min_idx = 26 if tf == 5 else 13
        for ma in CORE10_MAS:
            a_id = core10_strategy_id('A',tf,ma)
            b_id = core10_strategy_id('B',tf,ma)
            last_cross_t = None
            eligible = 0
            crossings = 0
            # Preserve original 5m baseline's len(rows)-1 scan limit.
            last_allowed = len(bars)-1 if tf==5 else len(bars)
            last_cross_i = -1000000
            for i in range(max(min_idx,ma),last_allowed):
                bar = bars[i]
                signal_ts = bar['t'] if tf==5 else bar['last_5m_t']
                if not start_day <= signal_ts < stop:
                    continue
                if not lab_contiguous_tf(bars,i-min_idx,i,tf):
                    continue
                eligible += 1
                if tf == 5 and i-last_cross_i < cfg['bars']:
                    continue
                if (tf == 10 and last_cross_t is not None and
                    signal_ts-last_cross_t < _td(minutes=cfg['horizon'])):
                    continue
                if not core10_ma_cross(bars,i,ma):
                    continue
                last_cross_t = signal_ts
                last_cross_i = i
                crossings += 1
                if not core10_volman_gate(bars,i,tf):
                    continue
                # Use original NEXT 5-minute start as entry, not a late bar.
                signal_i = index5.get(signal_ts)
                if signal_i is None:
                    continue
                top = max(b['h'] for b in bars[i-box:i])
                q = c5_beggs_v111430_quality(bar,top)
                stamp = signal_ts.strftime('%Y-%m-%d %H:%M')
                common = {'market':market,'base_id':market+'|'+stamp,
                          'signal_time_kst':stamp,
                          'entry_time_kst':(signal_ts+_td(minutes=5)).strftime('%Y-%m-%d %H:%M'),
                          'timeframe_minutes':tf,'ma_periods':ma,
                          'ma_window_minutes':tf*ma,
                          'compression_bars':box,
                          'compression_window_minutes':tf*box,
                          'ema_periods':(20 if tf==5 else 10),
                          'ema_window_minutes':100,
                          'box_high':round(top,10),
                          'close_position':q['close_position'],
                          'body_ratio':q['body_ratio'],
                          'upper_wick_ratio':q['upper_wick_ratio'],
                          'break_margin_pct':q['break_margin_pct'],
                          'research_only':True}
                outcome = lab_outcome_v11145(rows6,signal_i,cfg)
                groups[a_id].append({**common,'strategy':a_id,**outcome})
                if q['accepted']:
                    groups[b_id].append({**common,'strategy':b_id,**outcome})
            coverage['evaluated_positions'][str(tf)+'m_ma'+str(ma)] = eligible
            coverage['base_crosses'][str(tf)+'m_ma'+str(ma)] = crossings
    return {'groups':groups,'coverage':coverage}


def lab_contiguous_tf(rows,first,last,tf):
    if first < 0 or last >= len(rows):
        return False
    step = timedelta(minutes=tf)
    return all(rows[j]['t']-rows[j-1]['t']==step for j in range(first+1,last+1))

CORE_PAGE = '<!doctype html><html lang="ko"><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>V2 CORE.10 · 5분·10분봉 × 3·5·10이평 비교</title>\n<style>\n:root{color-scheme:dark;--bg:#0b1020;--p:#141c2f;--l:#2a3951;--t:#edf3ff;--m:#9dabca;--g:#4ade80;--r:#ff788a}\n*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--t);font:14px/1.55 system-ui,\'Noto Sans KR\',sans-serif}\nmain{max-width:1080px;margin:auto;padding:20px 14px 50px}h1{font-size:23px;margin:0}.sub{color:var(--m);margin:5px 0 16px}\na{color:#accbff}.panel{background:var(--p);border:1px solid var(--l);border-radius:14px;padding:15px;margin-bottom:13px}\n.tools{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}.field{display:flex;flex-direction:column;gap:5px;min-width:135px}\nlabel{font-size:12px;color:var(--m)}input,select,button{border:1px solid #43536b;background:#10192e;border-radius:9px;color:var(--t);padding:10px;font:inherit}\nbutton{background:#3160dc;border-color:#3160dc;cursor:pointer;font-weight:700}button:disabled{opacity:.45;cursor:wait}\nbutton.secondary{background:#202d43;border-color:#43536b}.muted{color:var(--m);font-size:12px}\nprogress{width:100%;height:12px;margin:12px 0}table{border-collapse:collapse;width:100%;min-width:630px}th,td{padding:9px 7px;text-align:right;border-bottom:1px solid var(--l);font-size:12px;white-space:nowrap}\nth:first-child,td:first-child{text-align:left}.scroll{overflow-x:auto}.positive{color:var(--g)}.negative{color:var(--r)}\n.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.kpi{border:1px solid var(--l);border-radius:10px;padding:9px}.kpi b{display:block;font-size:21px}\n@media(max-width:540px){.kpis{grid-template-columns:1fr 1fr}h1{font-size:19px}}\n</style></head><body><main>\n<h1>🧪 V2 CORE.10 · 5·10분봉 × 3·5·10이평 매수 타이밍 비교</h1><div class="sub">A 볼먼 · B 벡스 필터 · K 코너스 — 기존 3전략 유지 · 기존 전략 보존 · CORE.2~6 유지 · 5분·10분봉 × 3·5·10이평 신규 비교 · 기존 연구 보존</div>\n<p><a href="/">← 기존 V2 실전 스캐너로 돌아가기</a></p>\n<section class="panel">\n<div class="tools">\n<div class="field"><label for="day">검사 날짜 (KST)</label><input type="date" id="day"></div>\n<div class="field"><label for="scope">검사 범위</label><select id="scope"><option value="20">거래대금 상위 20종목</option><option value="40" selected>상위 40종목</option><option value="80">상위 80종목</option><option value="all">전체 KRW 종목 (느릴 수 있음)</option></select></div>\n<div class="field"><label for="fee">편도 수수료 (%)</label><input type="number" min="0" max="0.5" step="0.01" value="0.04" id="fee"></div>\n<div class="field"><label for="slip">편도 슬리피지 (%)</label><input type="number" min="0" max="0.5" step="0.01" value="0.05" id="slip"></div>\n<button id="run">검사 시작</button><button class="secondary" id="csv" disabled>CSV 받기</button></div>\n<div class="muted">기본 상위 40종목만 검사해 빠른 비교에 집중. 전체 검사 선택 시 이전 연구처럼 시간이 오래 걸릴 수 있어.</div>\n<progress id="progress" value="0" max="100"></progress><div id="status">검사를 시작하세요. BTC 시장 맥락은 날짜당 한 번만 요청합니다.</div>\n</section>\n<section class="panel"><div class="kpis"><div class="kpi">검사 완료<b id="done">—</b></div><div class="kpi">API 오류<b id="errors">—</b></div><div class="kpi">경과 시간<b id="elapsed">—</b></div></div></section>\n<section class="panel"><h2>전략별 결과</h2><div class="scroll"><table><thead><tr><th>전략</th><th>진입</th><th>결과확인</th><th>수익</th><th>손실</th><th>미확인</th><th>수익거래율</th><th>평균 순수익률</th></tr></thead><tbody id="summary"><tr><td colspan="8">검사 전</td></tr></tbody></table></div></section>\n<section class="panel" id="core10-panel">\n<h2>CORE.10 · 분봉 × 단순이동평균 6조합 독립 비교</h2>\n<div class="muted"><b>5분봉 / 10분봉 × SMA3·5·10</b>의 직전 종가 아래→현재 종가 위 상향 교차를 검출하고, 볼먼 가격 압축·박스 상단 돌파 A와 강한 봉 선별 B(벡스)를 각각 나란히 비교해. <b>B는 A의 부분집합</b>이므로 거래 수를 합산하지 마.\n10분봉은 5분봉 2개가 모두 있는 경우만 결합하며, <b>10분봉 신호 완성 후 다음 5분봉 시가</b>에 진입해. 누락된 봉은 복원하지 않아.</div>\n<div class="muted"><b>압축 박스:</b> 5분봉은 직전 6봉(30분), 10분봉은 직전 3봉(30분). <b>EMA 추세 기준:</b> 5분봉 EMA20, 10분봉 EMA10(모두 약 100분에 해당하지만 가격 표본이 달라 완전히 동등하지 않아). <b>익절 +1.5%, 손절 -1%, 최대 60분</b>, 기존 비용과 동일. 조합별 신호 재발생은 최소 60분 간격으로 제한해.\n현재 시점 거래대금으로 종목을 선정하는 기존 편향과 날짜별 데이터 확보 차이가 있으니 <b>조합의 수익성이 입증된 것으로 보지 마.</b></div>\n<div class="scroll"><table><thead><tr><th>진입 유형</th><th>분봉</th><th>이평</th><th>이평 시간</th><th>신호</th><th>청산 완료</th><th>수익</th><th>손실</th><th>미확인</th><th>승률</th><th>평균 순수익률</th><th>플러스 날짜</th></tr></thead><tbody id="core10_summary"><tr><td colspan="12">검사 전</td></tr></tbody></table></div>\n<div class="muted" id="core10_coverage">검사 전 · 참고: 5분봉 5이평 A는 기존 볼먼 A와 동일한 신호·청산 결과가 나와야 해.</div>\n</section>\n<section class="panel"><h2>가상 필터: 거래량 1.2배</h2>\n<div class="muted">A_V / B_V / K_V는 기존 A / B / K 진입 중, 신호봉 거래량이 직전 20개 연속된 5분봉 평균의 1.2배 이상인 거래만 선택한 가상 비교야. 기존 BUY를 바꾸지 않아. 제외된 거래에서도 원래 결과를 유지하여 피한 손실/놓친 수익을 함께 계산해.</div>\n<div class="scroll"><table><thead><tr><th>가상 전략</th><th>통과</th><th>결과확인</th><th>수익</th><th>손실</th><th>평균 순수익률</th><th>피한 손실</th><th>놓친 수익</th><th>판정 불가</th></tr></thead><tbody id="shadow_summary"><tr><td colspan="9">검사 전</td></tr></tbody></table></div></section>\n<section class="panel"><h2>CORE.3 · EMA/ATR 가상 필터 실험</h2>\n<div class="muted">A_E · B_E: 신호 당시 EMA20의 3봉 기울기 &gt; 0%. K_ATR: 신호 당시 ATR(14) / 종가 &lt; 0.5%. 2026-11-05~13 자료를 살펴보고 선정한 조건이라 <b>그 기간 결과는 성능 검증으로 간주하지 않아.</b> CORE.3 검증에 10월 25~31일을 사용했고, CORE.4는 10월 17~23일과 10월 11~16일도 분석했어. 본래 A/B/K 진입·청산 결과는 변경되지 않아.</div>\n<div class="scroll"><table><thead><tr><th>가상 전략</th><th>통과</th><th>결과확인</th><th>수익</th><th>손실</th><th>수익거래율</th><th>평균 순수익률</th><th>피한 손실</th><th>놓친 수익</th><th>판정 불가</th></tr></thead><tbody id="trend_shadow_summary"><tr><td colspan="10">검사 전</td></tr></tbody></table></div>\n</section>\n<section class="panel"><h2>CORE.4 · 돌파봉 품질 (EMA 통과 후 추가 선별)</h2>\n<div class="muted">A_EQ: A_E 통과 + 양봉 몸통 ≥50%, 윗꼬리 ≤20%, 직전 6봉 박스 상단 대비 종가 돌파 폭 ≥0.05%. B_EQ: B_E 통과 + 몸통 ≥70%, 윗꼬리 ≤10%, 돌파 폭 ≥0.10%. 몸통·윗꼬리는 <b>돌파봉 전체 고저 범위 대비 비율</b>이며, 돌파 폭은 <b>박스 상단 대비 종가의 %</b>야. 수치는 단순 연구 가설이고 최적화된 값이 아니야.</div>\n<div class="scroll"><table><thead><tr><th>품질 필터</th><th>EMA 통과</th><th>Q 통과</th><th>청산 완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th><th>추가 피한 손실</th><th>추가 놓친 수익</th><th>판정 불가</th></tr></thead><tbody id="quality_summary"><tr><td colspan="11">검사 전</td></tr></tbody></table></div>\n<h3>돌파봉 세부 조건별 탐색 결과</h3>\n<div class="muted">각 조건 <b>한 가지씩</b> 적용할 때의 결과를 보여줘. 세 조건 전체 조합인 A_EQ/B_EQ와 다르며, 다른 두 조건과 독립적인 인과효과를 뜻하지는 않아. 분석 대상은 기존 EMA 필터를 통과한 완료 거래뿐이야.</div>\n<div class="scroll"><table><thead><tr><th>기준 전략</th><th>독립 조건</th><th>조건 통과 완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th><th>제외된 수익</th><th>제외된 손실</th></tr></thead><tbody id="quality_diagnostics"><tr><td colspan="9">검사 전</td></tr></tbody></table></div>\n<div class="muted">⚠️ CORE.4 연구에 10월 11~23일 결과가 이미 쓰였으므로, 그 기간은 신규 CORE.5 조건의 독립 검증에 재사용하지 않아. 화면 통계는 거래당 평균으로, 누적 수익률이나 실거래 성과가 아니야.</div>\n</section>\n<section class="panel"><h2>CORE.5 · 볼먼 돌파 폭 / 벡스 몸통 제한 분리 실험</h2>\n<div class="muted"><b>A_EM</b> = A_E 통과 + 돌파 폭 ≥0.05% <b>단독</b> 적용 (기존 A_EQ 몸통 50%·윗꼬리 20% 추가 제한 해제).<br>\n<b>B_ENB</b> = B_E 통과 + 윗꼬리 ≤10% + 돌파 폭 ≥0.10% (기존 B_EQ의 <b>추가 몸통 ≥70% 제한만 제거</b>).<br>\n<b>B_EM</b> = B_E 통과 + 돌파 폭 ≥0.10% <b>단독</b> 적용. B는 원래 벡스 몸통 ≥40% 등 기본 조건을 그대로 사용해.<br>\n검증 화면은 원래 EMA 통과 거래와 비교해 <b>추가로 피한 손실/놓친 수익</b>을 표시하고, 같은 가상 진입의 순수익률을 재사용해. 실전 BUY는 변경하지 않아.</div>\n<div class="scroll"><table><thead><tr><th>비교 필터</th><th>EMA 통과</th><th>추가 통과</th><th>청산 완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th><th>추가 피한 손실</th><th>추가 놓친 수익</th><th>판정 불가</th></tr></thead><tbody id="core5_summary"><tr><td colspan="11">검사 전</td></tr></tbody></table></div>\n<div class="muted">⚠️ 이번 조건은 10월 11~23일의 손익을 확인한 뒤 선정했어. <b>2026년 10월 3~9일</b>처럼 아직 조건 선택에 사용하지 않은 날짜로 따로 검증해. 표본이 적거나 평균이 마이너스면 실전 적용하지 말자.</div>\n</section>\n<section class="panel"><h2>CORE.6 · 볼먼 국면 분리 / 벡스 윗꼬리 단독 실험</h2>\n<div class="muted"><b>A_EQ_TR</b> = 기존 A_EQ 통과 + 해당 <b>종목의 5분봉 SMA50</b> 상승(3봉)·종가 SMA50 상회. <b>A_EQ_ATR</b> = 기존 A_EQ 통과 + ATR(14)/종가 &lt;0.5%. <b>A_EQ_R</b> = 두 조건 동시 통과. <b>B_EW</b> = 기존 B_E 통과 + 윗꼬리 비율 ≤10% <b>단독</b> 적용.<br>\n⚠️ SMA50은 <b>각 코인 자체의 추세</b>를 설명하며 전체 KRW 시장의 상승·하락을 뜻하지 않아. 이 분석을 위해 BTC 등 별도 종목의 API를 호출하지 않으므로 기존 속도에 영향이 작아. 기존 B_EM(돌파 폭만), B_ENB(윗꼬리+돌파 폭)와 B_EW를 나란히 비교해. 조건은 수익성 검증 전인 가설이고 실전 BUY에는 반영하지 않았어.</div>\n<div class="scroll"><table><thead><tr><th>신규 가상 전략</th><th>부모 필터 통과</th><th>추가 통과</th><th>청산 완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th><th>추가 피한 손실</th><th>추가 놓친 수익</th><th>판정 불가</th></tr></thead><tbody id="core6_summary"><tr><td colspan="11">검사 전</td></tr></tbody></table></div>\n<h3>볼먼 A_EQ · 종목별 추세와 변동성에 따른 결과</h3>\n<div class="muted">추세·변동성은 신호봉 시점의 완료된 과거 5분봉으로만 계산. 표에는 <b>A_EQ를 이미 통과한 완료 거래만</b> 표시하며, 상·하 조건을 가로질러 해석할 때 표본 수를 확인해. 추세 50봉이 없으면 UNKNOWN으로 남겨.</div>\n<div class="scroll"><table><thead><tr><th>종목별 조건</th><th>완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th></tr></thead><tbody id="core6_regime"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<div class="muted">⚠️ CORE.6의 신규 기준을 정할 때 10월 3~31일 데이터를 참고했어. <b>2026년 9월 26일~10월 2일</b> 등 아직 조건 선정에 쓰이지 않은 기간에서 검증하자. 가상 수익률은 동일 거래 중 필터 통과 거래의 평균이지 실제 투자 수익을 뜻하지 않아.</div>\n</section>\n<section class="panel"><h2>CORE.7 · BTC 1시간 국면과 진입 결과</h2>\n<div class="muted">각 코인 신호가 발생한 <b>같은 5분봉의 BTC 완료 종가</b>와 정확히 60분 전 BTC 종가를 비교해.\nBTC 1시간 변화율 <b>+0.30% 이상은 상승</b>, <b>-0.30% 이하는 하락</b>, 그 사이는 횡보로 분류해.\nBTC는 전체 KRW 시장의 대리 지표일 뿐이며, 본 분류는 아직 검증되지 않은 연구 가설이야.\n누락된 봉은 보간하지 않고 UNKNOWN으로 분리해. BTC 참조 데이터는 날짜당 한 번만 서버에서 조회하며 10분 재사용해.</div>\n<div id="btc_status" class="muted">BTC 데이터 조회 전</div>\n<h3>기준 전략의 BTC 국면별 성적</h3>\n<div class="scroll"><table><thead><tr><th>기준 전략</th><th>BTC 국면</th><th>완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th></tr></thead><tbody id="core7_regimes"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<h3>가상 BTC 상승 국면 필터 — 실전 BUY 미적용</h3>\n<div class="muted">A_EQ_BTC_UP / B_E_BTC_UP / B_EW_BTC_UP은 각 부모 전략을 통과한 신호 중 BTC 1시간 상승만 남긴 실험이야.\n제외 거래의 원래 손익을 보존하므로 피한 손실과 놓친 수익을 함께 계산할 수 있어. BTC 정보가 없으면 통과시키지 않고 판정 불가로 분리해.</div>\n<div class="scroll"><table><thead><tr><th>가상 전략</th><th>부모 통과</th><th>BTC상승 통과</th><th>완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th><th>피한 손실</th><th>놓친 수익</th><th>판정 불가</th></tr></thead><tbody id="core7_summary"><tr><td colspan="11">검사 전</td></tr></tbody></table></div>\n<div class="muted">주의: BTC 상승 상태가 수익의 원인이라고 단정할 수 없어. A와 B는 같은 볼먼 신호를 공유하며, 서로 독립 거래로 합산하면 안 돼. 검사 당시 상위 거래대금 종목은 과거 날짜의 상위 종목과 다를 수 있어.</div>\n</section>\n<section class="panel"><h2>CORE.8 · BTC ATR 변동성 × 코인 돌파 폭 교차 분석</h2>\n<div class="muted">기존 <b>A_EQ(볼먼)</b>와 <b>B_E(벡스)</b>에서 부모 필터를 통과한 신호만 비교해.\n<b>BTC ATR(14)/BTC 종가</b>는 해당 신호봉과 직전 연속 14개 5분봉에서 계산한 값이며,\n<b>돌파 폭</b>은 같은 코인 신호봉의 종가가 직전 6봉 박스 상단보다 높은 정도(%)야.\n둘 다 <b>매수 신호가 완성된 시점까지</b>의 값만 사용하고 미래 봉을 읽지 않아.</div>\n<div class="muted">BTC ATR 구간: <b>낮음 &lt;0.15%</b> / <b>중간 0.15~0.25%</b> / <b>높음 ≥0.25%</b>.\n돌파 폭 구간: <b>작음 &lt;0.10%</b> / <b>중간 0.10~0.30%</b> / <b>큼 ≥0.30%</b>.\n이 경계는 2026년 11월 데이터 탐색을 참고한 <b>사후 연구 기준</b>이므로 같은 기간의 좋은 결과를 독립적인 수익성 증거로 취급하지 않아.\n한 코인의 신호는 각 전략에서 한 칸에만 들어가지만, A와 B가 같은 원본 돌파 신호를 공유할 수 있으므로 합산해서 별도 거래로 해석하면 안 돼.</div>\n<h3>국면별 거래당 평균 순수익률 — 3×3 교차표</h3>\n<div class="scroll"><table><thead><tr><th>기준 전략</th><th>BTC ATR</th><th>돌파 폭</th><th>완료 거래</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th></tr></thead>\n<tbody id="core8_matrix"><tr><td colspan="8">검사 전</td></tr></tbody></table></div>\n<h3>연구용 가상 필터 — 수익 누락·손실 회피 함께 기록</h3>\n<div class="muted"><b>BTC_ATR25</b>: BTC ATR &lt;0.25%만 적용. <b>BTC_ATR25_M10</b>: BTC ATR &lt;0.25%이면서 코인 돌파 폭 0.10% 이상.\n이는 같은 기존 진입 시각을 사후에 분류한 <b>가상 비교</b>이며 매매 신호나 실전 BUY를 변경하지 않아.\n진단값이 없으면 제외(REJECT)로 처리하지 않고 <b>판정 불가</b>로 따로 기록해.</div>\n<div class="scroll"><table><thead><tr><th>신규 비교 필터</th><th>부모 통과</th><th>조건 통과</th><th>청산 완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th><th>피한 손실</th><th>놓친 수익</th><th>판정 불가</th><th>플러스 날짜</th></tr></thead>\n<tbody id="core8_summary"><tr><td colspan="12">검사 전</td></tr></tbody></table></div>\n<div class="muted">데이터 확인: 이번 연구는 이미 살펴본 11월 1~7일, 13~14일 BTC ATR 자료를 탐색에 사용했어.\n따라서 실제 개선 여부는 이후 새로운 신호나 다른 날짜에서 추가 검증해야 해. 검사 당시 거래대금 상위 목록은 현재 조회 시점 기준이라 과거 전체 시장의 완전한 재현은 아니야.</div>\n</section>\n<section class="panel" id="core9-panel">\n<h2>CORE.9 · 진입 이후 60분 가격 경로 분석 (연구 전용)</h2>\n<div class="muted">대상은 <b>볼먼 A_EQ</b>와 <b>벡스 B_E</b> 필터를 통과한 기존 신호야. 기존 신호와 +1.5% 익절 / -1.0% 손절을 <b>절대 변경하지 않고</b> 별도 가격 경로를 관찰해.\n<b>MFE</b>는 다음 5분봉 시가 진입을 가정하고 이후 60분 동안의 최대 고가 기준 상승폭, <b>MAE</b>는 최저 저가 기준 하락폭이야.\n이 값은 <b>기존 익절·손절로 이미 청산된 이후의 움직임까지 포함</b>할 수 있으므로, 실제로 모두 누릴 수 있었던 수익을 뜻하지 않아.</div>\n<h3>가격 경로 진단</h3>\n<div class="scroll"><table><thead><tr><th>기준 전략</th><th>신호</th><th>60분 연속봉 확보</th><th>미확인</th><th>MFE 평균</th><th>MAE 평균</th><th>MFE ≥+0.5%</th><th>MFE ≥+1.0%</th><th>MFE ≥+1.5%</th><th>60분 종가 순수익 평균</th></tr></thead><tbody id="core9_paths"><tr><td colspan="10">검사 전</td></tr></tbody></table></div>\n<h3>익절·손절 조합 12개 비교 · 동일 신호·동일 60분 연속봉</h3>\n<div class="muted">익절 +0.50 / +0.75 / +1.00 / +1.50%, 손절 -0.50 / -0.75 / -1.00%를 교차 적용해. <b>진입 가격과 거래 비용은 CORE.8과 동일</b>하고, 60분 시간 종료도 유지해. 같은 5분봉에서 익절·손절이 동시에 닿으면 보수적으로 손절 우선, 손절가 아래로 갭 출발하면 그 시가로 계산해. <b>60분 경로가 완전히 확보된 거래만</b> 비교하므로 조합마다 표본이 달라지는 문제를 막아.</div>\n<div class="scroll"><table><thead><tr><th>기준 전략</th><th>익절</th><th>손절</th><th>완료</th><th>수익 / 손실</th><th>승률</th><th>평균 순수익률</th><th>TP / SL / 시간</th></tr></thead><tbody id="core9_grid"><tr><td colspan="8">검사 전</td></tr></tbody></table></div>\n<h3>진입 5분 지연 비교 · 기존 익절 +1.5% / 손절 -1.0%</h3>\n<div class="muted">기존 방식은 신호 직후 다음 봉 시가, 지연 방식은 <b>그보다 한 봉(5분) 뒤의 시가</b>에 가상 진입해. 지연된 진입도 새로 60분 보유하며, 추가 봉이 부족하면 판정 불가로 분리해. 지연 체결은 실전 주문 지연을 근사할 뿐 실제 체결가를 보장하지 않아.</div>\n<div class="scroll"><table><thead><tr><th>기준 전략</th><th>5분 지연 완료</th><th>지연 평가 불가</th><th>기존 평균(동일 표본)</th><th>지연 평균</th><th>변화</th></tr></thead><tbody id="core9_delay"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<div class="muted">⚠️ CORE.9의 여러 청산 조합은 <b>탐색용</b>이며 최고 수익률 조합을 골라 같은 날짜에서 재검증하면 과적합이 발생해. 5분봉은 봉 내부 가격 순서를 알 수 없으므로 실제 체결 결과와 다를 수 있어. 역사적 종목 선정은 현재 시점 24시간 거래대금 기준인 기존 한계도 유지돼.</div>\n</section>\n<section class="panel"><h2>거래 상세 (기존 전략, 최대 200건 표시)</h2><div class="scroll"><table><thead><tr><th>코인/전략</th><th>신호 시각</th><th>결과</th><th>순수익률</th></tr></thead><tbody id="details"><tr><td colspan="4">검사 전</td></tr></tbody></table></div></section>\n<section class="panel"><div class="muted">B는 A형 볼먼 신호를 걸러내는 부분집합이며 독립 전략이 아니야. K는 코너스의 일봉 원형이 아닌 5분봉 SMA50·RSI(2) 변형이야. A/B/K는 과거 완료봉에서만 신호를 판정하고 다음 실제 5분봉 시가에 가상 진입해. TP +1.5%, SL -1.0%, 최대 60분. 동일 봉 TP·SL 동시 도달 시 SL을 우선해. 없는 봉을 생성하지 않아. 실제 V2의 BUY·알림·기록·주문에는 영향을 주지 않아. CORE.4·CORE.5·CORE.6 필터는 기존 CORE.3 및 완료된 돌파봉의 진단값만 재사용해. 실전 BUY에는 적용하지 않았어. 가상 필터 수익률은 새로운 거래를 재진입한 결과가 아니라 같은 시점의 기존 거래에서 통과한 부분집합이야.</div></section>\n</main><script>\nconst $=id=>document.getElementById(id),esc=x=>String(x??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nlet responses=[],failures=[],started=0,btcContext=null,btcStatus=\'NOT_LOADED\';\nconst CORE7_DEFS=[[\'A_EQ_BTC_UP\',\'A_EQ\',\'shadow_breakout\',\'볼먼 A_EQ + BTC 상승\'],\n [\'B_E_BTC_UP\',\'B_E\',\'shadow_trend_atr\',\'벡스 B_E + BTC 상승\'],\n [\'B_EW_BTC_UP\',\'B_EW\',\'shadow_core6\',\'벡스 B_EW + BTC 상승\']];\nconst core7Done=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&e.net_pct!==undefined&&Number.isFinite(Number(e.net_pct));\nfunction core7Ctx(e){return btcContext?.[e.signal_time_kst]??null;}\nfunction core7Metrics(e){const c=core7Ctx(e);return {\n btc_regime:c?.btc_regime??\'UNKNOWN\',btc_ret60_pct:c?.btc_ret60_pct??null,\n btc_atr14_pct:c?.btc_atr14_pct??null,btc_regime_threshold_pct:0.30,\n btc_context_source:\'KRW-BTC 5m; current completed signal candle\'};}\nfunction deriveCore7(result){const out={};for(const [id,parent,key] of CORE7_DEFS){\n const rows=result[key]?.[parent]||[];\n out[id]=rows.map(old=>{const ctx=core7Metrics(old);const prev=old.filter_status;\n let status,reason;\n if(prev===\'REJECT\'||prev===\'BASE_REJECT\'){\n  status=\'BASE_REJECT\';reason=\'부모 필터에서 이미 제외\';\n }else if(prev!==\'PASS\'){\n  status=\'BASE_UNKNOWN\';reason=\'부모 필터 판정 불가\';\n }else if(ctx.btc_regime===\'UNKNOWN\'){\n  status=\'NOT_EVALUABLE\';reason=\'동시각 BTC 1시간 연속봉 부족\';\n }else if(ctx.btc_regime===\'BTC_UP\'){\n  status=\'PASS\';reason=\'BTC 1시간 상승 국면\';\n }else{status=\'REJECT\';reason=\'BTC 상승 국면 미충족\';}\n return {...old,...ctx,strategy:id,source_strategy:old.source_strategy||old.strategy,\n  comparison_base:parent,core7_rule_name:id,filter_status:status,\n  filter_pass:status===\'PASS\',filter_reason:reason};});\n }return out;}\n// CORE.8 reuses CORE.7\'s cached, same-time BTC candles and source outcomes.\n// Thresholds were chosen after November discovery; no claim of validation.\nconst CORE8_ATR_QUIET=.25, CORE8_ATR_MED=.35, CORE8_ATR_LOW=.15;\nconst CORE8_MARGIN_MID=.10, CORE8_MARGIN_HIGH=.30;\nconst CORE8_DEFS=[\n [\'A_EQ_BTC_ATR25\',\'A_EQ\',\'shadow_breakout\',\'볼먼 A_EQ · BTC ATR<0.25%\'],\n [\'A_EQ_BTC_ATR25_M10\',\'A_EQ\',\'shadow_breakout\',\'볼먼 A_EQ · BTC ATR<0.25% + 돌파≥0.10%\'],\n [\'B_E_BTC_ATR25\',\'B_E\',\'shadow_trend_atr\',\'벡스 B_E · BTC ATR<0.25%\'],\n [\'B_E_BTC_ATR25_M10\',\'B_E\',\'shadow_trend_atr\',\'벡스 B_E · BTC ATR<0.25% + 돌파≥0.10%\']\n];\nfunction core8Num(x){if(x===null||x===undefined||x===\'\')return null;\n const n=Number(x);return Number.isFinite(n)?n:null;}\nfunction core8AtrBand(v){if(v===null)return \'UNKNOWN\';\n return v<CORE8_ATR_LOW?\'LOW\':v<CORE8_ATR_QUIET?\'MID\':\'HIGH\';}\nfunction core8MarginBand(v){if(v===null)return \'UNKNOWN\';\n return v<CORE8_MARGIN_MID?\'SMALL\':v<CORE8_MARGIN_HIGH?\'MID\':\'LARGE\';}\nfunction core8Metrics(e){const btc=core7Metrics(e);\n const atr=core8Num(btc.btc_atr14_pct),margin=core8Num(e.break_margin_pct);\n return {...btc,core8_btc_atr_band:core8AtrBand(atr),\n core8_break_margin_band:core8MarginBand(margin),core8_btc_quiet_max_pct:CORE8_ATR_QUIET,\n core8_mid_margin_min_pct:CORE8_MARGIN_MID,core8_mid_margin_max_pct:CORE8_MARGIN_HIGH};}\nfunction deriveCore8(result){const out={};for(const [id,parent,key] of CORE8_DEFS){\n const joined=id.endsWith(\'_ATR25_M10\');\n out[id]=(result[key]?.[parent]||[]).map(old=>{\n const ctx=core8Metrics(old),prev=old.filter_status;\n const atr=core8Num(ctx.btc_atr14_pct),margin=core8Num(old.break_margin_pct);\n let status,reason;\n if(prev===\'REJECT\'||prev===\'BASE_REJECT\'){\n  status=\'BASE_REJECT\';reason=\'부모 필터에서 이미 제외됨\';\n }else if(prev!==\'PASS\'){\n  status=\'BASE_UNKNOWN\';reason=\'부모 필터 판정 불가\';\n }else if(atr===null||(joined&&margin===null)){\n  status=\'NOT_EVALUABLE\';reason=\'BTC 연속봉 또는 돌파 폭 진단값 없음\';\n }else if(atr<CORE8_ATR_QUIET&&(!joined||(margin>=CORE8_MARGIN_MID))){\n  status=\'PASS\';reason=\'BTC ATR<0.25%\'+(joined?\' + 중간 돌파 폭\':\'\');\n }else{status=\'REJECT\';reason=\'BTC 변동성 / 돌파 폭 조건 미충족\';}\n return {...old,...ctx,strategy:id,source_strategy:old.source_strategy||old.strategy,\n comparison_base:parent,core8_rule_name:id,filter_status:status,\n filter_pass:status===\'PASS\',filter_reason:reason};\n });}return out;}\nconst ymd=()=>{const parts=new Intl.DateTimeFormat(\'en-CA\',{timeZone:\'Asia/Seoul\',year:\'numeric\',month:\'2-digit\',day:\'2-digit\'}).formatToParts(new Date());const g=k=>parts.find(x=>x.type===k)?.value;return `${g(\'year\')}-${g(\'month\')}-${g(\'day\')}`};\nconst today=ymd(),dt=new Date(today+\'T00:00:00+09:00\');dt.setDate(dt.getDate()-1);const yesterday=new Intl.DateTimeFormat(\'en-CA\',{timeZone:\'Asia/Seoul\',year:\'numeric\',month:\'2-digit\',day:\'2-digit\'}).formatToParts(dt);const q=k=>yesterday.find(x=>x.type===k)?.value;\n$(\'day\').value=`${q(\'year\')}-${q(\'month\')}-${q(\'day\')}`;$(\'day\').max=$(\'day\').value;\nfunction stats(events){const v=events.filter(e=>e.entry_time_kst&&e.outcome!==\'NO_ENTRY\');const x=v.filter(e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&Number.isFinite(Number(e.net_pct)));const w=x.filter(e=>Number(e.net_pct)>0).length;return {entry:v.length,done:x.length,wins:w,loss:x.length-w,unknown:v.filter(e=>e.outcome===\'UNKNOWN\').length,avg:x.length?x.reduce((s,e)=>s+Number(e.net_pct),0)/x.length:NaN};}\nconst pct=x=>Number.isFinite(x)?x.toFixed(3)+\'%\':\'—\';\nfunction paint(){const arms=[[\'A\',\'볼먼 A\'],[\'B\',\'벡스 B (볼먼 필터)\'],[\'K\',\'코너스 K\']];const all=[];\n $(\'summary\').innerHTML=arms.map(([id,name])=>{const es=responses.flatMap(r=>r.events?.[id]||[]);all.push(...es.filter(e=>e.entry_time_kst&&e.outcome!==\'NO_ENTRY\'));const s=stats(es);return `<tr><td>${name}</td><td>${s.entry}</td><td>${s.done}</td><td class="positive">${s.wins}</td><td class="negative">${s.loss}</td><td>${s.unknown}</td><td>${s.done?(100*s.wins/s.done).toFixed(1)+\'%\':\'—\'}</td><td class="${s.avg>=0?\'positive\':\'negative\'}">${pct(s.avg)}</td></tr>`}).join(\'\');\n const sorted=all.sort((a,b)=>(a.signal_time_kst||\'\').localeCompare(b.signal_time_kst||\'\'));$(\'details\').innerHTML=sorted.slice(0,200).map(e=>`<tr><td>${esc(e.market)} / ${esc(e.strategy)}</td><td>${esc(e.signal_time_kst)}</td><td>${esc(e.outcome)}</td><td>${e.net_pct==null?\'—\':pct(Number(e.net_pct))}</td></tr>`).join(\'\')||\'<tr><td colspan="4">신호 없음</td></tr>\';\n $(\'done\').textContent=responses.length;$(\'errors\').textContent=failures.length;$(\'elapsed\').textContent=((performance.now()-started)/1000).toFixed(1)+\'초\';paintShadow();paintTrendShadow();paintBreakout();paintCore5();paintCore6();paintCore7();paintCore8();paintCore9();paintCore10();}\n\nfunction paintShadow(){\n const arms=[[\'A_V\',\'볼먼 A + 거래량\'],[\'B_V\',\'벡스 B + 거래량\'],[\'K_V\',\'코너스 K + 거래량\']];\n const complete=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&e.net_pct!==undefined&&Number.isFinite(Number(e.net_pct));\n $(\'shadow_summary\').innerHTML=arms.map(([id,name])=>{\n  const all=responses.flatMap(r=>r.shadow?.[id]||[]);\n  const passed=all.filter(e=>e.filter_status===\'PASS\');\n  const outcome=passed.filter(complete);\n  const wins=outcome.filter(e=>Number(e.net_pct)>0).length;\n  const dropped=all.filter(e=>e.filter_status===\'REJECT\'&&complete(e));\n  const avoided=dropped.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=dropped.filter(e=>Number(e.net_pct)>0).length;\n  const unknown=all.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  const avg=outcome.length?outcome.reduce((s,e)=>s+Number(e.net_pct),0)/outcome.length:NaN;\n  return `<tr><td>${name}</td><td>${passed.length}</td><td>${outcome.length}</td><td class="positive">${wins}</td><td class="negative">${outcome.length-wins}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td><td>${avoided}</td><td>${missed}</td><td>${unknown}</td></tr>`;\n }).join(\'\');\n}\nfunction paintTrendShadow(){\n const arms=[[\'A_E\',\'볼먼 + EMA 상승\'],[\'B_E\',\'벡스 + EMA 상승\'],[\'K_ATR\',\'코너스 + 저변동성 ATR\']];\n const complete=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&e.net_pct!==undefined&&Number.isFinite(Number(e.net_pct));\n $(\'trend_shadow_summary\').innerHTML=arms.map(([id,name])=>{\n  const all=responses.flatMap(r=>r.shadow_trend_atr?.[id]||[]);\n  const passed=all.filter(e=>e.filter_status===\'PASS\');\n  const finished=passed.filter(complete);\n  const wins=finished.filter(e=>Number(e.net_pct)>0).length;\n  const rejected=all.filter(e=>e.filter_status===\'REJECT\'&&complete(e));\n  const avoided=rejected.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=rejected.filter(e=>Number(e.net_pct)>0).length;\n  const unknown=all.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  const avg=finished.length?finished.reduce((s,e)=>s+Number(e.net_pct),0)/finished.length:NaN;\n  return `<tr><td>${name}</td><td>${passed.length}</td><td>${finished.length}</td><td class="positive">${wins}</td><td class="negative">${finished.length-wins}</td><td>${finished.length?(100*wins/finished.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td><td>${avoided}</td><td>${missed}</td><td>${unknown}</td></tr>`;\n }).join(\'\');\n}\n\nfunction paintBreakout(){\n const arms=[[\'A_EQ\',\'볼먼 A_E + 강한 돌파봉\',\'A_E\'],[\'B_EQ\',\'벡스 B_E + 매우 강한 돌파봉\',\'B_E\']];\n const complete=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&e.net_pct!==undefined&&Number.isFinite(Number(e.net_pct));\n const avg=a=>a.length?a.reduce((s,e)=>s+Number(e.net_pct),0)/a.length:NaN;\n $(\'quality_summary\').innerHTML=arms.map(([id,name,parent])=>{\n  const all=responses.flatMap(r=>r.shadow_breakout?.[id]||[]);\n  const base=all.filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status));\n  const eligible=all.filter(e=>e.filter_status!==\'BASE_REJECT\'&&e.filter_status!==\'BASE_UNKNOWN\');\n  const passed=eligible.filter(e=>e.filter_status===\'PASS\');\n  const done=passed.filter(complete);const wins=done.filter(e=>Number(e.net_pct)>0).length;\n  const drop=eligible.filter(e=>e.filter_status===\'REJECT\'&&complete(e));\n  const avoided=drop.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=drop.filter(e=>Number(e.net_pct)>0).length;\n  const unjudge=eligible.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  const emaPassed=all.filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status)).length;\n  return `<tr><td>${name}</td><td>${emaPassed}</td><td>${passed.length}</td><td>${done.length}</td><td class="positive">${wins}</td><td class="negative">${done.length-wins}</td><td>${done.length?(100*wins/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg(done)>=0?\'positive\':\'negative\'}">${pct(avg(done))}</td><td>${avoided}</td><td>${missed}</td><td>${unjudge}</td></tr>`;\n }).join(\'\');\n const specs=[[\'quality_body_ok\',\'몸통 비율\'],[\'quality_upper_ok\',\'윗꼬리 비율\'],[\'quality_margin_ok\',\'돌파 폭\']];\n $(\'quality_diagnostics\').innerHTML=arms.flatMap(([id,name])=>{\n  const all=responses.flatMap(r=>r.shadow_breakout?.[id]||[]).filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status)&&complete(e));\n  return specs.map(([key,metric])=>{\n   const passed=all.filter(e=>e[key]===true),excluded=all.filter(e=>e[key]===false);\n   const wins=passed.filter(e=>Number(e.net_pct)>0).length;\n   const lose=passed.length-wins;\n   const mw=excluded.filter(e=>Number(e.net_pct)>0).length;\n   const al=excluded.filter(e=>Number(e.net_pct)<=0).length;\n   return `<tr><td>${name}</td><td>${metric}</td><td>${passed.length}</td><td class="positive">${wins}</td><td class="negative">${lose}</td><td>${passed.length?(100*wins/passed.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg(passed)>=0?\'positive\':\'negative\'}">${pct(avg(passed))}</td><td>${mw}</td><td>${al}</td></tr>`;\n  });\n }).join(\'\');\n}\nfunction paintCore5(){\n const arms=[[\'A_EM\',\'볼먼 A_E + 돌파 폭만\'],[\'B_ENB\',\'벡스 B_E + 몸통 70% 제한 해제\'],[\'B_EM\',\'벡스 B_E + 돌파 폭만\']];\n const complete=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&e.net_pct!==undefined&&Number.isFinite(Number(e.net_pct));\n const avg=a=>a.length?a.reduce((s,e)=>s+Number(e.net_pct),0)/a.length:NaN;\n $(\'core5_summary\').innerHTML=arms.map(([id,name])=>{\n  const all=responses.flatMap(r=>r.shadow_core5?.[id]||[]);\n  const eligible=all.filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status));\n  const passed=eligible.filter(e=>e.filter_status===\'PASS\');\n  const done=passed.filter(complete), wins=done.filter(e=>Number(e.net_pct)>0).length;\n  const dropped=eligible.filter(e=>e.filter_status===\'REJECT\'&&complete(e));\n  const avoided=dropped.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=dropped.filter(e=>Number(e.net_pct)>0).length;\n  const unknown=eligible.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  return `<tr><td>${name}</td><td>${eligible.length}</td><td>${passed.length}</td><td>${done.length}</td><td class="positive">${wins}</td><td class="negative">${done.length-wins}</td><td>${done.length?(100*wins/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg(done)>=0?\'positive\':\'negative\'}">${pct(avg(done))}</td><td>${avoided}</td><td>${missed}</td><td>${unknown}</td></tr>`;\n }).join(\'\');\n}\nfunction paintCore6(){\n const variants=[[\'A_EQ_TR\',\'볼먼 A_EQ + 종목 SMA50 상승\'],[\'A_EQ_ATR\',\'볼먼 A_EQ + ATR<0.5%\'],[\'A_EQ_R\',\'볼먼 A_EQ + 두 조건\'],[\'B_EW\',\'벡스 B_E + 윗꼬리 ≤10%만\']];\n const complete=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&e.net_pct!==undefined&&Number.isFinite(Number(e.net_pct));\n const mean=a=>a.length?a.reduce((s,e)=>s+Number(e.net_pct),0)/a.length:NaN;\n $(\'core6_summary\').innerHTML=variants.map(([id,label])=>{\n  const all=responses.flatMap(r=>r.shadow_core6?.[id]||[]);\n  const eligible=all.filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status));\n  const passed=eligible.filter(e=>e.filter_status===\'PASS\');\n  const done=passed.filter(complete), wins=done.filter(e=>Number(e.net_pct)>0).length;\n  const dropped=eligible.filter(e=>e.filter_status===\'REJECT\'&&complete(e));\n  const avoided=dropped.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=dropped.filter(e=>Number(e.net_pct)>0).length;\n  const unknown=eligible.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  return `<tr><td>${label}</td><td>${eligible.length}</td><td>${passed.length}</td><td>${done.length}</td><td class="positive">${wins}</td><td class="negative">${done.length-wins}</td><td>${done.length?(100*wins/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${mean(done)>=0?\'positive\':\'negative\'}">${pct(mean(done))}</td><td>${avoided}</td><td>${missed}</td><td>${unknown}</td></tr>`;\n }).join(\'\');\n const all=responses.flatMap(r=>r.shadow_core6?.A_EQ_R||[])\n   .filter(e=>e.filter_status!==\'BASE_REJECT\'&&e.filter_status!==\'BASE_UNKNOWN\'&&complete(e));\n const groups=[[\'종목 SMA50 상승 + 종가 상회\',e=>e.local_uptrend===true],[\'그 외 종목 추세\',e=>e.local_uptrend===false],[\'ATR 0.5% 미만\',e=>e.local_low_volatility===true],[\'ATR 0.5% 이상\',e=>e.local_low_volatility===false]];\n $(\'core6_regime\').innerHTML=groups.map(([name,pred])=>{\n  const done=all.filter(e=>pred(e)),wins=done.filter(e=>Number(e.net_pct)>0).length;\n  return `<tr><td>${name}</td><td>${done.length}</td><td class="positive">${wins}</td><td class="negative">${done.length-wins}</td><td>${done.length?(100*wins/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${mean(done)>=0?\'positive\':\'negative\'}">${pct(mean(done))}</td></tr>`;\n }).join(\'\');\n}\nfunction paintCore7(){\n $(\'btc_status\').textContent=btcStatus===\'OK\'\n  ?\'BTC 과거 5분봉 조회 완료 · 동시간대의 완료 봉만 사용 · 10분 서버 캐시\'\n  :\'BTC 상태: \'+btcStatus+\' (조회 실패 시 기존 전략 결과는 계속 표시)\';\n const mean=a=>a.length?a.reduce((s,e)=>s+Number(e.net_pct),0)/a.length:NaN;\n const parents=[[\'볼먼 A_EQ\',\'shadow_breakout\',\'A_EQ\'],\n  [\'벡스 B_E\',\'shadow_trend_atr\',\'B_E\'],[\'벡스 B_EW\',\'shadow_core6\',\'B_EW\']];\n $(\'core7_regimes\').innerHTML=parents.flatMap(([label,key,id])=>{\n  const eligible=responses.flatMap(r=>r[key]?.[id]||[])\n   .filter(e=>e.filter_status===\'PASS\'&&core7Done(e));\n  return [[\'BTC_UP\',\'상승\'],[\'BTC_SIDE\',\'횡보\'],[\'BTC_DOWN\',\'하락\'],[\'UNKNOWN\',\'판정 불가\']].map(([regime,title])=>{\n   const done=eligible.filter(e=>core7Metrics(e).btc_regime===regime);\n   const wins=done.filter(e=>Number(e.net_pct)>0).length;\n   return `<tr><td>${label}</td><td>${title}</td><td>${done.length}</td><td>${wins}</td><td>${done.length-wins}</td><td>${done.length?(100*wins/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${mean(done)>=0?\'positive\':\'negative\'}">${pct(mean(done))}</td></tr>`;\n  });\n }).join(\'\');\n $(\'core7_summary\').innerHTML=CORE7_DEFS.map(([id,parent,key,label])=>{\n  const all=responses.flatMap(r=>r.shadow_core7?.[id]||[]);\n  const valid=all.filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status));\n  const passed=valid.filter(e=>e.filter_status===\'PASS\'),done=passed.filter(core7Done);\n  const wins=done.filter(e=>Number(e.net_pct)>0).length;\n  const rejected=valid.filter(e=>e.filter_status===\'REJECT\'&&core7Done(e));\n  const avoided=rejected.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=rejected.filter(e=>Number(e.net_pct)>0).length;\n  const unknown=valid.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  return `<tr><td>${label}</td><td>${valid.length}</td><td>${passed.length}</td><td>${done.length}</td><td>${wins}</td><td>${done.length-wins}</td><td>${done.length?(100*wins/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${mean(done)>=0?\'positive\':\'negative\'}">${pct(mean(done))}</td><td>${avoided}</td><td>${missed}</td><td>${unknown}</td></tr>`;\n }).join(\'\');\n}\nfunction paintCore10(){\n const arms=[[\'A\',\'볼먼 A\'],[\'B\',\'벡스 B (A의 부분집합)\']];\n const tf=[5,10], mas=[3,5,10];\n const key=(arm,tf,ma)=>arm+\'_\'+tf+\'M_MA\'+String(ma).padStart(2,\'0\');\n const completed=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!=null&&Number.isFinite(Number(e.net_pct));\n $(\'core10_summary\').innerHTML=arms.flatMap(([arm,name])=>tf.flatMap(t=>mas.map(m=>{\n  const id=key(arm,t,m),all=responses.flatMap(r=>r.core10_timeframes?.groups?.[id]||[]);\n  const done=all.filter(completed),wins=done.filter(e=>Number(e.net_pct)>0).length;\n  const net=done.length?done.reduce((sum,e)=>sum+Number(e.net_pct),0)/done.length:NaN;\n  const days=new Map();for(const e of done){const d=e.signal_time_kst.slice(0,10);if(!days.has(d))days.set(d,[]);days.get(d).push(e);}\n  let positiveDays=0;for(const xs of days.values()){if(xs.reduce((s,e)=>s+Number(e.net_pct),0)/xs.length>0)positiveDays++;}\n  return `<tr><td>${name}</td><td>${t}분</td><td>SMA${m}</td><td>${t*m}분</td><td>${all.length}</td><td>${done.length}</td><td>${wins}</td><td>${done.length-wins}</td><td>${all.length-done.length}</td><td>${done.length?(100*wins/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${net>=0?\'positive\':\'negative\'}">${pct(net)}</td><td>${positiveDays}/${days.size}</td></tr>`;\n }))).join(\'\');\n let five=0,ten=0,miss=0;for(const r of responses){const c=r.core10_timeframes?.coverage;if(!c)continue;five+=c.observed_5m||0;ten+=c.complete_10m||0;miss+=(c.possible_10m_slots||0)-(c.complete_10m||0);}\n $(\'core10_coverage\').textContent=`조회된 5분봉 ${five}개 · 완성된 10분봉 ${ten}개 · 10분봉 미완성/누락 ${miss}개 (모든 종목 합산). A 5분 SMA5는 원래 볼먼 A 신호와 정확히 같아야 해.`;\n}\nfunction paintCore9(){\n const defs=[[\'A_EQ\',\'볼먼 A_EQ\'],[\'B_E\',\'벡스 B_E\']];\n const ok=e=>e&&e.path_status===\'FULL\';\n const completed=e=>e&&[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&Number.isFinite(Number(e.net_pct));\n const avg=(a,key)=>a.length?a.reduce((s,e)=>s+Number(e[key]),0)/a.length:NaN;\n const doneList=(arm)=>responses.flatMap(r=>r.core9_paths?.[arm]||[]);\n $(\'core9_paths\').innerHTML=defs.map(([arm,label])=>{\n  const rows=doneList(arm),full=rows.filter(ok),missing=rows.length-full.length;\n  const count=v=>full.filter(e=>e.mfe_pct>=v).length;\n  return `<tr><td>${label}</td><td>${rows.length}</td><td>${full.length}</td><td>${missing}</td><td>${pct(avg(full,\'mfe_pct\'))}</td><td>${pct(avg(full,\'mae_pct\'))}</td><td>${count(.5)}</td><td>${count(1)}</td><td>${count(1.5)}</td><td>${pct(avg(full,\'net_60m_pct\'))}</td></tr>`;\n }).join(\'\');\n const tps=[.5,.75,1,1.5],sls=[.5,.75,1];\n const key=(tp,sl)=>\'tp\'+String(Math.round(tp*100)).padStart(3,\'0\')+\'_sl\'+String(Math.round(sl*100)).padStart(3,\'0\');\n $(\'core9_grid\').innerHTML=defs.flatMap(([arm,label])=>{\n  const full=doneList(arm).filter(ok);\n  return tps.flatMap(tp=>sls.map(sl=>{\n   const x=full.map(e=>e.grid?.[key(tp,sl)]).filter(completed);\n   const wins=x.filter(e=>Number(e.net_pct)>0).length;\n   const n=type=>x.filter(e=>e.outcome===type).length;\n   return `<tr><td>${label}</td><td>+${tp.toFixed(2)}%</td><td>-${sl.toFixed(2)}%</td><td>${x.length}</td><td>${wins} / ${x.length-wins}</td><td>${x.length?(100*wins/x.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg(x,\'net_pct\')>=0?\'positive\':\'negative\'}">${pct(avg(x,\'net_pct\'))}</td><td>${n(\'TP\')} / ${n(\'SL\')} / ${n(\'TIME\')}</td></tr>`;\n  }));\n }).join(\'\');\n $(\'core9_delay\').innerHTML=defs.map(([arm,label])=>{\n  const all=doneList(arm).filter(ok),matched=all.filter(e=>e.base_net_pct!=null&&Number.isFinite(Number(e.base_net_pct))&&[\'TP\',\'SL\',\'TIME\'].includes(e.delay5_outcome)&&e.delay5_net_pct!=null&&Number.isFinite(Number(e.delay5_net_pct)));\n  const base=avg(matched,\'base_net_pct\'),late=avg(matched,\'delay5_net_pct\');\n  return `<tr><td>${label}</td><td>${matched.length}</td><td>${all.length-matched.length}</td><td>${pct(base)}</td><td>${pct(late)}</td><td>${pct(late-base)}</td></tr>`;\n }).join(\'\');\n}\nfunction paintCore8(){\n const base=[[\'볼먼 A_EQ\',\'shadow_breakout\',\'A_EQ\'],[\'벡스 B_E\',\'shadow_trend_atr\',\'B_E\']];\n const atrs=[[\'LOW\',\'낮음 <0.15%\'],[\'MED\',\'중간 0.15~0.25%\'],[\'HIGH\',\'높음 ≥0.25%\']];\n const widths=[[\'SMALL\',\'작음 <0.10%\'],[\'MID\',\'중간 0.10~0.30%\'],[\'LARGE\',\'큼 ≥0.30%\']];\n const mean=x=>x.length?x.reduce((s,e)=>s+Number(e.net_pct),0)/x.length:NaN;\n $(\'core8_matrix\').innerHTML=base.flatMap(([label,key,arm])=>{\n  const completed=responses.flatMap(r=>r[key]?.[arm]||[])\n   .filter(e=>e.filter_status===\'PASS\'&&core7Done(e));\n  const cells=atrs.flatMap(([a,aLabel])=>widths.map(([m,mLabel])=>{\n   const rows=completed.filter(e=>{const c=core8Metrics(e);\n    return c.core8_btc_atr_band===a&&c.core8_break_margin_band===m;});\n   const win=rows.filter(e=>Number(e.net_pct)>0).length,avg=mean(rows);\n   return `<tr><td>${label}</td><td>${aLabel}</td><td>${mLabel}</td><td>${rows.length}</td><td>${win}</td><td>${rows.length-win}</td><td>${rows.length?(100*win/rows.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td></tr>`;\n  }));\n  const missing=completed.filter(e=>{const c=core8Metrics(e);\n   return c.core8_btc_atr_band===\'UNKNOWN\'||c.core8_break_margin_band===\'UNKNOWN\';});\n  const win=missing.filter(e=>Number(e.net_pct)>0).length,avg=mean(missing);\n  cells.push(`<tr><td>${label}</td><td colspan="2">판정 불가(누락 데이터)</td><td>${missing.length}</td><td>${win}</td><td>${missing.length-win}</td><td>${missing.length?(100*win/missing.length).toFixed(1)+\'%\':\'—\'}</td><td>${pct(avg)}</td></tr>`);\n  return cells;\n }).join(\'\');\n $(\'core8_summary\').innerHTML=CORE8_DEFS.map(([id,parent,key,label])=>{\n  const all=responses.flatMap(r=>r.shadow_core8?.[id]||[]);\n  const eligible=all.filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status));\n  const passed=eligible.filter(e=>e.filter_status===\'PASS\'),done=passed.filter(core7Done);\n  const win=done.filter(e=>Number(e.net_pct)>0).length,avg=mean(done);\n  const reject=eligible.filter(e=>e.filter_status===\'REJECT\'&&core7Done(e));\n  const avoided=reject.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=reject.filter(e=>Number(e.net_pct)>0).length;\n  const unknown=eligible.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  const groups=new Map();for(const e of done){const day=String(e.signal_time_kst||\'\').slice(0,10);\n   if(!groups.has(day))groups.set(day,[]);groups.get(day).push(e);}\n  let positives=0;for(const dayRows of groups.values())if(mean(dayRows)>0)positives++;\n  return `<tr><td>${label}</td><td>${eligible.length}</td><td>${passed.length}</td><td>${done.length}</td><td>${win}</td><td>${done.length-win}</td><td>${done.length?(100*win/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td><td>${avoided}</td><td>${missed}</td><td>${unknown}</td><td>${positives}/${groups.size}</td></tr>`;\n }).join(\'\');\n}\nasync function api(url,data){const ctl=new AbortController(),to=setTimeout(()=>ctl.abort(),55000);try{const r=await fetch(url,{method:data?\'POST\':\'GET\',headers:{\'Content-Type\':\'application/json\'},body:data?JSON.stringify(data):undefined,cache:\'no-store\',signal:ctl.signal});const obj=await r.json();if(!r.ok||!obj.ok)throw Error(obj.error||`HTTP ${r.status}`);return obj;}finally{clearTimeout(to)}}\n$(\'run\').onclick=async()=>{if($(\'run\').disabled)return;const day=$(\'day\').value,scope=$(\'scope\').value,fee=Number($(\'fee\').value),slip=Number($(\'slip\').value);\n if(!day||!Number.isFinite(fee)||!Number.isFinite(slip)||fee<0||fee>0.5||slip<0||slip>0.5)return alert(\'날짜·비용 설정을 확인하세요.\');\n $(\'run\').disabled=true;$(\'csv\').disabled=true;responses=[];failures=[];btcContext=null;btcStatus=\'LOADING\';started=performance.now();paint();$(\'status\').textContent=\'종목 목록 준비 중\';\n try{const info=await api(\'/api/v2core/universe\');const count=scope===\'all\'?info.items.length:Number(scope);const markets=info.items.slice(0,count);\n try{const marketInfo=await api(\'/api/v2core/btc-context\',{date:day});\n  btcContext=marketInfo.context;btcStatus=\'OK\';\n }catch(e){btcStatus=\'조회 실패: \'+String(e.message||e).slice(0,90);btcContext=null;}\n let cursor=0,done=0;const total=markets.length;$(\'progress\').max=total;$(\'progress\').value=0;\n async function work(){while(cursor<total){const market=markets[cursor++];try{const out=await api(\'/api/v2core/evaluate\',{market,date:day,fee,slip});out.result.shadow_core7=deriveCore7(out.result);out.result.shadow_core8=deriveCore8(out.result);responses.push(out.result)}catch(e){failures.push({market,error:String(e.message||e)})}\n done++;$(\'progress\').value=done;$(\'status\').textContent=`${done}/${total} 검사 · 성공 ${responses.length} · 오류 ${failures.length}`;if(done%5===0||done===total)paint();}}\n await Promise.all(Array.from({length:Math.min(4,total)},work));paint();$(\'csv\').disabled=responses.length===0;$(\'status\').textContent=failures.length?`일부 오류 ${failures.length}건. CSV에는 성공한 종목만 포함됩니다. 누락을 확인하세요.`:`✅ 완료 ${responses.length}종목, 오류 0건`;\n }catch(e){$(\'status\').textContent=\'검사 중단: \'+e.message;}finally{$(\'run\').disabled=false;}};\nconst csvCell=x=>\'"\'+String(x??\'\').replace(/"/g,\'""\')+\'"\';\n$(\'csv\').onclick=()=>{const cols=[\'version\',\'date\',\'market\',\'strategy\',\'base_id\',\'signal_time_kst\',\'entry_time_kst\',\'outcome\',\'entry\',\'exit\',\'net_pct\',\'minutes\',\'reason\',\'close_position\',\'body_ratio\',\'upper_wick_ratio\',\'break_margin_pct\',\'connors_rsi2_before\',\'connors_rsi2_signal\',\'connors_sma50\',\'connors_sma50_slope3_pct\',\'pattern_volume_ratio\',\'source_strategy\',\'filter_pass\',\'filter_status\',\'filter_reason\',\'shadow_threshold\',\'volume_ratio20\',\'ema20_slope3_pct\',\'atr14_pct\',\'filter_metric\',\'filter_operator\',\'filter_threshold\',\'filter_value\',\'comparison_base\',\'quality_body_min\',\'quality_upper_wick_max\',\'quality_break_margin_min_pct\',\'quality_body_ok\',\'quality_upper_ok\',\'quality_margin_ok\',\'core5_rule_name\',\'core5_active_rules\',\'core5_margin_ok\',\'core5_wick_ok\',\'local_sma50\',\'local_sma50_slope3_pct\',\'local_close_vs_sma50_pct\',\'local_uptrend\',\'local_regime\',\'local_low_volatility\',\'core6_atr_max_pct\',\'core6_wick_max\',\'core6_wick_ok\',\'core6_rule_name\',\'btc_regime\',\'btc_ret60_pct\',\'btc_atr14_pct\',\'btc_regime_threshold_pct\',\'btc_context_source\',\'core7_rule_name\',\'core8_btc_atr_band\',\'core8_break_margin_band\',\'core8_btc_quiet_max_pct\',\'core8_mid_margin_min_pct\',\'core8_mid_margin_max_pct\',\'core8_rule_name\',\'core9_path_status\',\'core9_path_reason\',\'core9_mfe_pct\',\'core9_mae_pct\',\'core9_mfe_first_min\',\'core9_mae_first_min\',\'core9_net_15m_pct\',\'core9_net_30m_pct\',\'core9_net_60m_pct\',\'core9_tp_pct\',\'core9_sl_pct\',\'core9_delay_minutes\',\'core9_base_outcome\',\'core9_base_net_pct\',\'core10_timeframe_minutes\',\'core10_ma_periods\',\'core10_ma_window_minutes\',\'core10_compression_bars\',\'core10_compression_window_minutes\',\'core10_ema_periods\',\'core10_ema_window_minutes\',\'core10_research_only\'];const lines=[cols.join(\',\')];\n for(const r of responses){for(const id of [\'A\',\'B\',\'K\'])for(const e of r.events?.[id]||[]){const row={version:\'V2.CORE.10\',date:r.date,...e,...core8Metrics(e),strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const id of [\'A_V\',\'B_V\',\'K_V\'])for(const e of r.shadow?.[id]||[]){const row={version:\'V2.CORE.10\',date:r.date,...e,...core8Metrics(e),strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const id of [\'A_E\',\'B_E\',\'K_ATR\'])for(const e of r.shadow_trend_atr?.[id]||[]){const row={version:\'V2.CORE.10\',date:r.date,...e,...core8Metrics(e),strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const id of [\'A_EQ\',\'B_EQ\'])for(const e of r.shadow_breakout?.[id]||[]){const row={version:\'V2.CORE.10\',date:r.date,...e,...core8Metrics(e),strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const id of [\'A_EM\',\'B_ENB\',\'B_EM\'])for(const e of r.shadow_core5?.[id]||[]){const row={version:\'V2.CORE.10\',date:r.date,...e,...core8Metrics(e),strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const id of [\'A_EQ_TR\',\'A_EQ_ATR\',\'A_EQ_R\',\'B_EW\'])for(const e of r.shadow_core6?.[id]||[]){const row={version:\'V2.CORE.10\',date:r.date,...e,...core8Metrics(e),strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const [id] of CORE7_DEFS)for(const e of r.shadow_core7?.[id]||[]){\n   const row={version:\'V2.CORE.10\',date:r.date,...e,...core8Metrics(e),strategy:id};\n   lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const [id] of CORE8_DEFS)for(const e of r.shadow_core8?.[id]||[]){\n   const row={version:\'V2.CORE.10\',date:r.date,...e,...core8Metrics(e),strategy:id};\n   lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const id of [\'A_EQ\',\'B_E\'])for(const e of r.core9_paths?.[id]||[]){\n   const shared={version:\'V2.CORE.10\',date:r.date,market:e.market,base_id:e.base_id,signal_time_kst:e.signal_time_kst,entry_time_kst:e.entry_time_kst,\n    core9_path_status:e.path_status,core9_path_reason:e.path_reason,core9_mfe_pct:e.mfe_pct,core9_mae_pct:e.mae_pct,\n    core9_mfe_first_min:e.mfe_first_min,core9_mae_first_min:e.mae_first_min,core9_net_15m_pct:e.net_15m_pct,\n    core9_net_30m_pct:e.net_30m_pct,core9_net_60m_pct:e.net_60m_pct,core9_base_outcome:e.base_outcome,\n    core9_base_net_pct:e.base_net_pct};\n   lines.push(cols.map(k=>csvCell(({...shared,strategy:\'CORE9_PATH_\'+id})[k])).join(\',\'));\n   for(const tp of [.5,.75,1,1.5])for(const sl of [.5,.75,1]){\n    const key=\'tp\'+String(Math.round(tp*100)).padStart(3,\'0\')+\'_sl\'+String(Math.round(sl*100)).padStart(3,\'0\');\n    const v=e.grid?.[key];if(!v)continue;\n    const gridRow={...shared,strategy:\'CORE9_EXIT_\'+id,core9_tp_pct:tp,core9_sl_pct:sl,\n      outcome:v.outcome,net_pct:v.net_pct,minutes:v.minutes};\n    lines.push(cols.map(k=>csvCell(gridRow[k])).join(\',\'));\n   }\n   const delayed={...shared,strategy:\'CORE9_DELAY5_\'+id,core9_tp_pct:1.5,core9_sl_pct:1,\n    core9_delay_minutes:5,entry_time_kst:e.delay5_entry_time_kst,\n    outcome:e.delay5_outcome,net_pct:e.delay5_net_pct};\n   lines.push(cols.map(k=>csvCell(delayed[k])).join(\',\'));\n }}\n for(const r of responses){const groups=r.core10_timeframes?.groups||{};\n  for(const [strategy,items] of Object.entries(groups))for(const e of items){\n   const row={version:\'V2.CORE.10\',date:r.date,...e,strategy,\n    core10_timeframe_minutes:e.timeframe_minutes,core10_ma_periods:e.ma_periods,\n    core10_ma_window_minutes:e.ma_window_minutes,core10_compression_bars:e.compression_bars,\n    core10_compression_window_minutes:e.compression_window_minutes,\n    core10_ema_periods:e.ema_periods,core10_ema_window_minutes:e.ema_window_minutes,\n    core10_research_only:e.research_only};\n   lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));\n  }\n }\n for(const e of failures)lines.push(cols.map(k=>csvCell(({version:\'V2.CORE.10\',date:$(\'day\').value,market:e.market,strategy:\'ERROR\',reason:e.error})[k])).join(\',\'));\n const blob=new Blob([\'\\ufeff\'+lines.join(\'\\n\')],{type:\'text/csv;charset=utf-8\'}),url=URL.createObjectURL(blob),a=document.createElement(\'a\');a.href=url;a.download=\'bithumb_V2_CORE10_TIMEFRAME_MA_MATRIX.csv\';a.click();setTimeout(()=>URL.revokeObjectURL(url),3000);};\n</script></body></html>'
def register_daytrade_review(app, deps):
    """ONE lab, ONE registration point. Main V2 scanner is untouched."""
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache = {'at': 0., 'markets': [], 'results': {}, 'ticker_at': 0., 'sorted': [], 'btc': {}}
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

    @app.post('/api/v2core/btc-context')
    def v2_core_btc_context():
        """One date-scoped BTC reference for whole scan, cached 10 minutes.

        BTC is fetched ONCE per date request via paginated candle API, not
        for every coin. Failure never changes original A/B/K outcomes.
        """
        try:
            raw = request.get_json(silent=True)
            cfg = lab_valid_request({'date':raw.get('date') if isinstance(raw,dict) else None,
                                     'tp':1.5,'sl':1.,'horizon':60,
                                     'fee':.04,'slip':.05},kst)
            day = cfg['day']
            with lock:
                old = cache['btc'].get(day)
            if old is not None and time.monotonic()-old[0] < 600:
                return jsonify(ok=True,version=VERSION,date=day.isoformat(),
                               context=old[1],cached=True)
            rows,truncated,meta = c5_h2_v111434_fetch_extended(
                api_get,'KRW-BTC',cfg,kst)
            context = build_btc_hour_context(rows,cfg,kst)
            if not context:
                raise ValueError('BTC 역사적 5분봉을 불러오지 못했습니다')
            with lock:
                if len(cache['btc']) > 60: cache['btc'].clear()
                cache['btc'][day] = (time.monotonic(),context)
            return jsonify(ok=True,version=VERSION,date=day.isoformat(),
                           context=context,cached=False,
                           observed_candles=len(rows),page_limit_reached=truncated)
        except Exception as exc:
            return jsonify(ok=False,error=(type(exc).__name__+': '+str(exc))[:180]),400

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
