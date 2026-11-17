"""V2 CORE.31 MULTIDAY REVERSAL — CORE.27 LIGHT signals unchanged + independent expanded reverse EMA/Fibonacci research.

Full CORE.24 remains archived separately. No change to the app.py live BUY scanner.
This module replaces the original daytrade_review.py, retaining only necessary
signal discovery, CORE.23 PRIMARY rechecks, CORE.24 entry/exit diagnostics,
BTC market context and the same /v2-core-review /api/v2core/* endpoints.
All shared model functions are unchanged from CORE.24 unless marked CORE25.
No live trading or order submission.
"""
from datetime import datetime, timedelta, date as _date, time as _time
from math import isfinite
import math
from threading import Lock
import time
import re
from flask import jsonify, render_template_string, request

VERSION = 'V2.CORE.31-MULTIDAY-REVERSAL'

def core21_ema(prices, periods):
    k=2.0/(periods+1)
    value=float(prices[0]); out=[]
    for price in prices:
        value=value+k*(float(price)-value)
        out.append(value)
    return out

def core21_tf_ema_trend(rows, tf, asof):
    """EMA5/10, recent highs/lows, only contiguous COMPLETED data at asof."""
    complete=[b for b in rows if b['t']+timedelta(minutes=tf)<=asof]
    if len(complete)<16:
        return dict(state='UNKNOWN',reason='EMA 추세 데이터 부족')
    tail=complete[-16:]
    delta=timedelta(minutes=tf)
    if any(tail[i]['t']-tail[i-1]['t']!=delta for i in range(1,len(tail))):
        return dict(state='UNKNOWN',reason='최근 EMA 분봉 공백')
    age=asof-(tail[-1]['t']+delta)
    if age<timedelta(0) or age>=delta:
        return dict(state='UNKNOWN',reason='최신 확정 분봉 시간 오류')
    cs=[float(b['c']) for b in tail]
    ema5=core21_ema(cs,5)
    ema10=core21_ema(cs,10)
    close=cs[-1]
    highlow_up=(tail[-1]['l']>tail[-2]['l'] and
                tail[-2]['l']>=tail[-3]['l'])
    highlow_down=(tail[-1]['l']<min(b['l'] for b in tail[-4:-1]))
    breakout=close>tail[-2]['h']
    up=close>ema5[-1]>ema10[-1] and ema5[-1]>ema5[-2] and (highlow_up or breakout)
    down=close<ema10[-1] and ema5[-1]<ema10[-1] and ema5[-1]<ema5[-2] and (highlow_down or close<cs[-2])
    state='UP' if up else ('DOWN' if down else 'MIXED')
    return dict(state=state,reason='',ema5=round(ema5[-1],8),
                ema10=round(ema10[-1],8),
                ema5_slope_pct=round(100*(ema5[-1]/ema5[-2]-1),6),
                close_vs_ema10_pct=round(100*(close/ema10[-1]-1),6),
                higher_lows=bool(highlow_up),lower_low=bool(highlow_down),
                recent_high_break=bool(breakout))

CORE20_LIQ_BARS = 12

def core20_aggregate_5m(rows5, tf):
    """Use ONLY complete, aligned pairs/triples (never invent missing candles)."""
    if tf not in (10, 15):
        raise ValueError('Unsupported aggregated timeframe')
    by_t = {r['t']: r for r in rows5}
    n = tf // 5
    output = []
    for row in rows5:
        t = row['t']
        if (t.minute % tf != 0 or t.second != 0 or t.microsecond != 0):
            continue
        group = [by_t.get(t + timedelta(minutes=5*k)) for k in range(n)]
        if any(bar is None for bar in group):
            continue
        output.append({'t':t, 'o':group[0]['o'],
                       'h':max(b['h'] for b in group),
                       'l':min(b['l'] for b in group),
                       'c':group[-1]['c'],
                       'v':sum(b['v'] for b in group)})
    return output

def core20_liquidity(rows5, at_index):
    """12 last scheduled 5m slots, volume x close is approximate KRW turnover."""
    t = rows5[at_index]['t']
    lookup = {b['t']:b for b in rows5[max(0,at_index-100):at_index+1]}
    window = [lookup.get(t-timedelta(minutes=5*k)) for k in range(11,-1,-1)]
    valid = [b for b in window if b is not None]
    coverage = len(valid) / CORE20_LIQ_BARS
    avg_krw = sum(float(b['v'])*float(b['c']) for b in valid) / len(valid) if valid else 0.
    # Ex-ante hypotheses: do not confuse these thresholds with proven cutoffs.
    if len(valid) >= 11 and avg_krw >= 5_000_000:
        level, primary, secondary = 'HIGH', 3, 5
    elif len(valid) >= 10 and avg_krw >= 1_000_000:
        level, primary, secondary = 'MEDIUM', 5, 10
    elif len(valid) >= 8 and avg_krw >= 250_000:
        level, primary, secondary = 'LOW', 10, 15
    else:
        level, primary, secondary = 'THIN', None, None
    return dict(tier=level, primary_tf=primary, secondary_tf=secondary,
                last_60m_slots=len(valid), coverage_ratio=round(coverage,4),
                avg_5m_turnover_krw=round(avg_krw,2))

def core20_followup(rows5, trigger_index, horizon):
    """Gross close-to-close change and worst LOW, separately from BE net P&L."""
    bars = horizon // 5
    end=trigger_index+bars
    if end >= len(rows5):
        return dict(status='UNKNOWN', gross_pct=None, worst_low_pct=None)
    group=rows5[trigger_index:end+1]
    if any(group[i]['t']-group[i-1]['t'] != timedelta(minutes=5)
           for i in range(1,len(group))):
        return dict(status='UNKNOWN', gross_pct=None, worst_low_pct=None)
    px=group[0]['c']
    if not isfinite(px) or px<=0:
        return dict(status='UNKNOWN', gross_pct=None, worst_low_pct=None)
    return dict(status='READY',
                gross_pct=round(100*(group[-1]['c']/px-1),6),
                worst_low_pct=round(100*(min(b['l'] for b in group[1:])/px-1),6))

MARKET_RE = re.compile(r'^KRW-[A-Z0-9]+$')

LAB_MAX_PAGES = 5

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

def c5_h2_v111434_fetch_extended(api_get,market,cfg,kst):
    """Fetch same raw candle endpoint with 6h rather than 4h prehistory.

    Same page size, exclusive KST cursor, validation, and missing-candle policy
    as lab_fetch_5m. No candle interpolation or price forward-fill. Returns
    meta for transparent audit; all prices are exchange observations.
    """
    from datetime import datetime as _dt,time as _tm
    begin=_dt.combine(cfg['day'],_tm.min,tzinfo=kst)-timedelta(hours=6)
    end=_dt.combine(cfg['day']+timedelta(days=1),_tm.min,tzinfo=kst)+timedelta(hours=4,minutes=5)
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

def lab_contiguous_tf(rows,first,last,tf):
    if first < 0 or last >= len(rows):
        return False
    step = timedelta(minutes=tf)
    return all(rows[j]['t']-rows[j-1]['t']==step for j in range(first+1,last+1))

CORE16_TP_NET_PCTS = (0.5, 5.0, 10.0)

CORE16_TP_PARTS = (0.30, 0.30, 0.40)

CORE16_MAX_HOLD = 240

def core16_simulate_scale(rows5, signal_i, cfg, use_be):
    """Run ONE observed 5m path; output weighted net return on original capital."""
    step = timedelta(minutes=5)
    n = CORE16_MAX_HOLD // 5
    cost = (cfg['fee'] + cfg['slip']) / 100.0
    blank = dict(outcome='UNKNOWN', label='UNKNOWN', net_pct=None,
                 minutes=None, exit_time_kst='', status='UNKNOWN',
                 reason='진입/미래 가격 데이터 부족',
                 tp1_filled=False, tp5_filled=False, tp10_filled=False,
                 be_triggered=False, ambiguous_same_bar=False,
                 tp1_min=None, tp5_min=None, tp10_min=None, be_min=None,
                 partial_realized_net_pct=0.0, remaining_fraction=1.0,
                 exit_legs=[], tp_prices=[], be_price=None,
                 initial_stop=False)
    def unknown(reason):
        res = dict(blank)
        res['reason'] = reason
        res['exit_legs'] = []
        res['tp_prices'] = []
        return res

    if signal_i is None or signal_i<0 or signal_i+1>=len(rows5):
        return unknown('진입 다음 5분봉 없음')
    if rows5[signal_i+1]['t']-rows5[signal_i]['t'] != step:
        return unknown('진입 직전 5분봉 공백')
    entry = float(rows5[signal_i+1]['o'])
    if not isfinite(entry) or entry<=0:
        return unknown('진입 가격 오류')
    prices=[entry*(1+p/100.0)*(1+cost)/(1-cost) for p in CORE16_TP_NET_PCTS]
    be_price=entry*(1+cost)/(1-cost)
    res=unknown('240분 이내 결과 미확정')
    res['tp_prices']=[round(x,10) for x in prices]
    res['be_price']=round(be_price,10)
    stage=0
    remaining=1.0
    realized=0.0
    last=signal_i

    def add_leg(kind, fraction, px, minute, stamp):
        nonlocal remaining, realized
        net=(px*(1-cost)/(entry*(1+cost))-1)*100.0
        realized+=fraction*net
        remaining=max(0.0, round(remaining-fraction,10))
        res['exit_legs'].append(dict(kind=kind, fraction=fraction,
                     exit_price=round(px,10), net_pct=round(net,6),
                     weighted_net_pct=round(net*fraction,6),
                     minute=minute, time_kst=stamp.strftime('%Y-%m-%d %H:%M')))
        res['remaining_fraction']=remaining
        res['partial_realized_net_pct']=round(realized,6)
        if kind=='TP1':
            res['tp1_filled']=True; res['tp1_min']=minute
        if kind=='TP5':
            res['tp5_filled']=True; res['tp5_min']=minute
        if kind=='TP10':
            res['tp10_filled']=True; res['tp10_min']=minute
        if kind=='BE':
            res['be_triggered']=True; res['be_min']=minute

    def finish(kind, minute, stamp):
        res.update(outcome=kind, status='RESOLVED', reason='',
                   net_pct=round(realized,6), minutes=minute,
                   exit_time_kst=stamp.strftime('%Y-%m-%d %H:%M'),
                   label='순이익' if realized>1e-10 else ('순손실' if realized<-1e-10 else '본절'))
        return res

    for k in range(1,n+1):
        j=signal_i+k
        if j>=len(rows5):
            res['reason']='청산 전 미래 5분봉 부족'
            return res
        bar=rows5[j]
        if bar['t']-rows5[last]['t']!=step:
            res['reason']='청산 전 미래 5분봉 공백'
            return res
        last=j
        hi=float(bar['h']); lo=float(bar['l']); op=float(bar['o']); cl=float(bar['c'])
        if not all(isfinite(x) and x>0 for x in (hi,lo,op,cl)):
            res['reason']='가격 데이터 오류'
            return res
        minute=k*5
        tol=max(entry,1)*1e-12
        # BE was armed by a prior 5-minute candle. Stop precedes higher
        # take-profit fills on a mixed high/low 5m candle (cautious).
        if use_be and stage>=1 and lo<=be_price+tol:
            if stage < len(prices) and hi+tol>=prices[stage]:
                res['ambiguous_same_bar']=True
            add_leg('BE',remaining,min(be_price,op),minute,bar['t'])
            return finish('BE',minute,bar['t'])
        # No fixed stop before TP1. Split fills in ascending order.
        if stage==0 and hi+tol>=prices[0]:
            add_leg('TP1',CORE16_TP_PARTS[0],prices[0],minute,bar['t'])
            stage=1
            # TP1 and BE same bar have unknown price ordering; BE is
            # armed from NEXT candle to avoid a fictitious same-bar stop
            # merely because the entry candle opened below net-BE.
            if use_be and lo<=be_price+tol:
                res['ambiguous_same_bar']=True
        if stage==1 and hi+tol>=prices[1]:
            add_leg('TP5',CORE16_TP_PARTS[1],prices[1],minute,bar['t'])
            stage=2
        if stage==2 and hi+tol>=prices[2]:
            add_leg('TP10',CORE16_TP_PARTS[2],prices[2],minute,bar['t'])
            stage=3
            return finish('TP10',minute,bar['t'])
        if k==n:
            if remaining>0:
                add_leg('TIME',remaining,cl,minute,bar['t'])
            return finish('TIME',minute,bar['t'])
    res['reason']='판정 불가'
    return res

CORE23_DELAYS=(10,20,30)

CORE23_DECEL_RATIO=0.70

CORE23_CHASE_PCT=3.0

def core23_snapshot(rows5,rows10,rows15,by_t,pos_by_t,cfg,old,complete,search_time,delay,phase):
    step=timedelta(minutes=5)
    cutoff=search_time-timedelta(minutes=delay)
    assert cutoff-timedelta(minutes=30) <= complete <= cutoff
    stamp=lambda d:d.strftime('%Y-%m-%d %H:%M')
    ev=dict(market=old.get('market'),base_id=old.get('base_id'),
        signal_time_kst=old.get('signal_time_kst'),
        original_entry_time_kst=old.get('entry_time_kst'),
        search_time_kst=stamp(search_time), first_cutoff_kst=stamp(cutoff),
        signal_complete_kst=stamp(complete),
        delay_minutes=delay, scan_phase=phase, scan_hour_kst=search_time.hour,
        review_status='UNKNOWN', state='INSUFFICIENT_DATA',
        local_guard='LOCAL_UNKNOWN',local_reason='분봉 데이터 확인 전',
        trend_label='UNKNOWN',liquidity_tier=None,primary_tf=None,
        secondary_tf=None,primary_slope_now=None,primary_slope_past=None,
        secondary_slope_now=None,secondary_slope_past=None,
        primary_higher_lows=None,primary_high_break=None,
        slowdown_primary=None,slowdown_secondary=None,
        price_change_pct=None,chase_warning=False,
        postscan_be_status='UNKNOWN',postscan_be_net_pct=None,
        postscan_be_outcome='UNKNOWN',postscan_entry_time_kst='',
        original_be_status=old.get('_baseline_status','UNKNOWN'),
        original_be_net_pct=old.get('_baseline_net_pct'),
        original_be_outcome=old.get('_baseline_outcome','UNKNOWN'),
        forward15_status='UNKNOWN',forward15_gross_pct=None,
        forward30_status='UNKNOWN',forward30_gross_pct=None,
        forward60_status='UNKNOWN',forward60_gross_pct=None)
    recent=by_t.get(search_time-step)
    n=delay//5
    ref=by_t.get(cutoff-step)
    bars=[by_t.get(cutoff+i*step) for i in range(n)]
    if recent is None or ref is None or any(b is None for b in bars):
        ev['local_reason']='검증구간 5분봉 또는 기준봉 누락'
        return ev
    if any(not all(isfinite(float(b[k])) and float(b[k])>0 for k in ('o','h','l','c')) for b in bars+[ref]):
        ev['local_reason']='검증구간 가격값 이상'; return ev
    pos=pos_by_t.get(search_time-step)
    if pos is None:
        ev['local_reason']='시간축 대응 오류'; return ev
    ev['price_change_pct']=round(100*(float(bars[-1]['c'])/float(ref['c'])-1),6)
    ev['chase_warning']=ev['price_change_pct']>=CORE23_CHASE_PCT
    # Outcomes are deliberately calculated independently of the local decision.
    for horizon in (15,30,60):
        f=core20_followup(rows5,pos,horizon)
        ev['forward'+str(horizon)+'_status']=f.get('status','UNKNOWN')
        ev['forward'+str(horizon)+'_gross_pct']=f.get('gross_pct')
    entry_index=pos+1
    if entry_index<len(rows5) and rows5[entry_index]['t']==search_time:
        result=core16_simulate_scale(rows5,entry_index,cfg,True)
        ev['postscan_entry_time_kst']=stamp(search_time+step)
        ev['postscan_be_status']=result.get('status','UNKNOWN')
        ev['postscan_be_outcome']=result.get('outcome','UNKNOWN')
        ev['postscan_be_net_pct']=result.get('net_pct')
    liq=core20_liquidity(rows5,pos)
    ev['liquidity_tier']=liq.get('tier')
    if liq.get('tier')=='THIN':
        ev['local_reason']='유동성 부족 — 판정 보류';return ev
    tf1,tf2=(10,15) if liq.get('tier')=='LOW' else (5,10)
    source={5:rows5,10:rows10,15:rows15}
    cur1=core21_tf_ema_trend(source[tf1],tf1,search_time)
    old1=core21_tf_ema_trend(source[tf1],tf1,search_time-timedelta(minutes=tf1*2))
    cur2=core21_tf_ema_trend(source[tf2],tf2,search_time)
    old2=core21_tf_ema_trend(source[tf2],tf2,search_time-timedelta(minutes=tf2*2))
    ev['primary_tf'],ev['secondary_tf']=tf1,tf2
    if any(x.get('state')=='UNKNOWN' for x in (cur1,old1,cur2,old2)):
        ev['local_reason']='현재·과거 EMA 연속봉 부족';return ev
    p,pp=cur1['ema5_slope_pct'],old1['ema5_slope_pct']
    s,sp=cur2['ema5_slope_pct'],old2['ema5_slope_pct']
    ev.update(primary_slope_now=p,primary_slope_past=pp,
              secondary_slope_now=s,secondary_slope_past=sp,
              primary_higher_lows=cur1['higher_lows'],
              primary_high_break=cur1['recent_high_break'])
    ev['slowdown_primary']=bool(pp>0 and p<pp*CORE23_DECEL_RATIO)
    ev['slowdown_secondary']=bool(sp>0 and s<sp*CORE23_DECEL_RATIO)
    ev['review_status']='REVIEWED'
    a,b=cur1['state'],cur2['state']
    if a=='DOWN' and b=='DOWN':
        ev.update(state='DOWNTREND',trend_label='DOWN',local_guard='LOCAL_REJECT',
                  local_reason='두 분봉 모두 하락 지속')
    elif a=='UP' and b=='UP':
        if ev['slowdown_primary'] or ev['slowdown_secondary']:
            ev.update(state='UP_DECELERATING',trend_label='SLOWDOWN',
                      local_guard='LOCAL_WAIT',local_reason='EMA 기울기 이전 대비 30% 이상 둔화')
        elif ev['chase_warning']:
            ev.update(state='UP_OVEREXTENDED',trend_label='CHASE',
                      local_guard='LOCAL_WAIT',local_reason='검증구간 상승폭 3% 이상 추격 주의')
        elif cur1['higher_lows'] and cur1['recent_high_break'] and p>0 and s>0:
            recovering=(pp<=0 or sp<=0)
            ev.update(state='RECOVERY_CONFIRMED' if recovering else 'UP_ACCELERATING',
                      trend_label='RECOVERY' if recovering else 'UP',
                      local_guard='LOCAL_STRONG',
                      local_reason='상승 기울기 유지/개선 + 저점 상승 + 최근 고점 돌파')
        else:
            ev.update(state='UP_UNCONFIRMED',trend_label='WAIT',local_guard='LOCAL_WAIT',
                      local_reason='이평 상승 중이나 가격 구조 확인 부족')
    elif a=='DOWN' and b!='UP':
        ev.update(state='DOWNTREND_RISK',trend_label='DOWN_RISK',
                  local_guard='LOCAL_REJECT',local_reason='단기 하락 + 중기 상승 확인 없음')
    elif a=='UP' and b!='DOWN':
        ev.update(state='REBOUND_WATCH',trend_label='WATCH',
                  local_guard='LOCAL_WAIT',local_reason='단기 반등, 상위 분봉 미확인')
    else:
        ev.update(state='INDETERMINATE',trend_label='MIXED',
                  local_guard='LOCAL_WAIT',local_reason='분봉 방향 불일치 또는 추세 불명확')
    return ev

CORE24_VOLUME_RATIO_MIN = 1.20

CORE24_TRAIL_GROSS_PCT = 1.00

CORE24_HORIZONS = (15, 30, 60)

CORE24_LAB = 'CORE24_ENTRY_VS_EXIT_DIAG_B_10M_MA03'

def core24_net(entry, exit_price, cfg):
    cost = (float(cfg['fee']) + float(cfg['slip'])) / 100.0
    if entry <= 0 or exit_price <= 0 or cost >= 1:
        raise ValueError('CORE24 invalid prices/cost')
    return 100.0 * (exit_price * (1.0-cost) / (entry * (1.0+cost)) - 1.0)

def core24_setup(rows5, signal_i):
    """signal_i points to the hypothetical 5m candle at search time T.

    Actual simulated buy is at the next candle open (T+5 minutes), exactly
    matching CORE.23 postscan BE. Return None on missing/invalid entry.
    """
    if signal_i is None or signal_i < 0 or signal_i+1 >= len(rows5):
        return None
    i = signal_i + 1
    if rows5[i]['t'] - rows5[signal_i]['t'] != timedelta(minutes=5):
        return None
    try:
        entry = float(rows5[i]['o'])
    except (TypeError, ValueError, KeyError):
        return None
    if not isfinite(entry) or entry <= 0:
        return None
    return i, entry

def core24_bar_path(rows5, entry_i, minutes):
    """Return contiguous observed completed 5m bars after entry, inclusive.

    A bar with start at T+5 completes at T+10. Thus 15m exit is the close
    of the third bar, 15 minutes after the hypothetical entry at T+5.
    """
    if entry_i is None or minutes % 5 or minutes < 5:
        return None
    end = entry_i + minutes//5
    if end > len(rows5):
        return None
    bars = rows5[entry_i:end]
    for j, b in enumerate(bars):
        if j and b['t']-bars[j-1]['t'] != timedelta(minutes=5):
            return None
        try:
            nums = [float(b[k]) for k in ('o','h','l','c')]
        except (KeyError,TypeError,ValueError):
            return None
        if any(not isfinite(v) or v <= 0 for v in nums):
            return None
        if nums[1] < max(nums[0],nums[3]) or nums[2] > min(nums[0],nums[3]):
            return None
    return bars

def core24_diagnostics(rows5, entry_i, entry, cfg):
    result = {}
    for minutes in CORE24_HORIZONS:
        bars = core24_bar_path(rows5, entry_i, minutes)
        prefix = 'm'+str(minutes)
        if bars is None:
            result[prefix] = dict(status='UNKNOWN',mfe_gross_pct=None,
                mae_gross_pct=None,mfe_net_upper_pct=None,
                mfe_first_min=None,mae_first_min=None)
            continue
        peak = max(float(b['h']) for b in bars)
        bottom = min(float(b['l']) for b in bars)
        pidx = next(i for i,b in enumerate(bars) if float(b['h'])==peak)
        bidx = next(i for i,b in enumerate(bars) if float(b['l'])==bottom)
        result[prefix] = dict(status='READY',
           mfe_gross_pct=round(100*(peak/entry-1),6),
           mae_gross_pct=round(100*(bottom/entry-1),6),
           mfe_net_upper_pct=round(core24_net(entry,peak,cfg),6),
           mfe_first_min=5*(pidx+1),mae_first_min=5*(bidx+1))
    return result

def core24_fixed_exit(rows5, signal_i, cfg, minutes):
    setup = core24_setup(rows5,signal_i)
    if setup is None:
        return dict(status='UNKNOWN',net_pct=None,reason='진입 5분봉 누락')
    entry_i,entry = setup
    bars = core24_bar_path(rows5,entry_i,minutes)
    if bars is None:
        return dict(status='UNKNOWN',net_pct=None,reason=f'{minutes}분 경로 불완전')
    px=float(bars[-1]['c'])
    return dict(status='RESOLVED',net_pct=round(core24_net(entry,px,cfg),6),
        minutes=minutes,outcome='TIME',exit_time_kst=(bars[-1]['t']+timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M'),reason='')

def core24_trailing_exit(rows5, signal_i, cfg, trail_pct=CORE24_TRAIL_GROSS_PCT):
    """After TP1 (+0.5% NET) sells 30%, remaining 70% trails peak high.

    Stop is armed NEXT 5m candle. Stop for current candle is computed only
    from highs of PRIOR completed candles (no same-bar high/low ordering).
    Minimum stop is gross price covering both fees (= cost-aware BE), but
    downward gaps execute at open if worse. No fixed stop before TP1.
    If TP1 never arrives, exit 100% at 240m CLOSE if fully observed.
    """
    blank = dict(status='UNKNOWN',outcome='UNKNOWN',net_pct=None,
        reason='경로 미확인',tp1_min=None,trail_min=None,
        tp1_filled=False,exit_time_kst='',ambiguous_same_bar=False,
        partial_realized_net_pct=0.0,remaining_fraction=1.0)
    setup = core24_setup(rows5,signal_i)
    if setup is None:
        return blank
    entry_i,entry=setup
    cost=(float(cfg['fee'])+float(cfg['slip']))/100.0
    tp1=entry * (1+.5/100.) * (1+cost)/(1-cost)
    be=entry * (1+cost)/(1-cost)
    stage=0; peak=None; result=dict(blank); realized=0.0
    last_time=None
    def finish(tag, minutes, bar, gain):
        result.update(status='RESOLVED',outcome=tag,
            net_pct=round(gain,6),reason='',remaining_fraction=0.0,
            partial_realized_net_pct=round(gain,6),
            exit_time_kst=(bar['t']+timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M'))
        return result
    for k in range(1, CORE16_MAX_HOLD//5+1):
        j=signal_i+k
        if j>=len(rows5):
            result['reason']='240분 이전 미래봉 부족';return result
        bar=rows5[j]
        if last_time is not None:
            if bar['t']-last_time!=timedelta(minutes=5):
                result['reason']='청산 전 5분봉 공백';return result
        elif bar['t']-rows5[signal_i]['t']!=timedelta(minutes=5):
            result['reason']='진입 직전 5분봉 공백';return result
        last_time=bar['t']
        try: op,hi,lo,cl=[float(bar[v]) for v in ('o','h','l','c')]
        except (KeyError,TypeError,ValueError):
            result['reason']='가격 이상';return result
        if (not all(isfinite(x) and x>0 for x in (op,hi,lo,cl))
            or hi<max(op,cl) or lo>min(op,cl)):
            result['reason']='가격 이상';return result
        minute=k*5
        if stage==0:
            if hi>=tp1:
                realized=.3*core24_net(entry,tp1,cfg)
                stage=1; peak=hi
                result.update(tp1_filled=True,tp1_min=minute,
                    remaining_fraction=.7,partial_realized_net_pct=round(realized,6))
                if lo<=be:
                    result['ambiguous_same_bar']=True
        else:
            stop=max(be,peak*(1-trail_pct/100.))
            if lo<=stop:
                # OHLC order ambiguous when this very bar makes a new high.
                if hi>peak and lo<=max(be,hi*(1-trail_pct/100.)):
                    result['ambiguous_same_bar']=True
                result['trail_min']=minute
                gain=realized+.7*core24_net(entry,min(op,stop),cfg)
                return finish('TRAIL',minute,bar,gain)
            peak=max(peak,hi)
        if minute==CORE16_MAX_HOLD:
            remaining=.7 if stage else 1.0
            gain=realized+remaining*core24_net(entry,cl,cfg)
            return finish('TIME',minute,bar,gain)
    return result

def core24_quality_at_search(rows5, signal_i, record):
    """Entry quality hypothesis from bars completed on/before search time.

    No post-entry bars are used for decision. Reuse CORE.23's completed-bar
    EMA slope and high/low assessments; add non-overlapping 4/12 volume test.
    CORE.23 slowdown is reported, but does not auto-reject this test group.
    """
    result=dict(local_gate='UNKNOWN',reason='데이터 부족',
                volume_ratio_4v12=None,has_price_structure=False,
                setup_class='UNKNOWN')
    if signal_i is None or signal_i < 16:
        return result
    history=rows5[signal_i-16:signal_i]  # last complete bar at T-5, exclude unclosed T
    if len(history)!=16 or any(history[i]['t']-history[i-1]['t']!=timedelta(minutes=5) for i in range(1,16)):
        result['reason']='5분봉 16개 연속성 부족';return result
    try: vols=[float(b['v']) for b in history]
    except (KeyError,TypeError,ValueError):
        result['reason']='거래량 값 누락';return result
    if any(not isfinite(v) or v<0 for v in vols):
        result['reason']='거래량 값 이상';return result
    mean_older=sum(vols[:12])/12
    if mean_older<=0:
        result['reason']='과거 거래량이 거의 없음';return result
    ratio=(sum(vols[12:])/4)/mean_older
    result['volume_ratio_4v12']=round(ratio,6)
    price_structure=bool(record.get('primary_high_break') and
            record.get('primary_higher_lows'))
    result['has_price_structure']=price_structure
    state=record.get('state')
    result['setup_class']=('RECOVERY' if state=='RECOVERY_CONFIRMED'
            else 'DECELERATING' if state=='UP_DECELERATING'
            else 'CONTINUATION' if state=='UP_ACCELERATING'
            else 'OTHER')
    # No future, no BTC/market decision here; BTC gate is added in JS.
    if record.get('review_status')!='REVIEWED':
        result.update(local_gate='UNKNOWN',reason='CORE.23 추세 판정 데이터 부족')
    elif (record.get('chase_warning') or
          (record.get('price_change_pct') is not None and
           record['price_change_pct'] >= CORE23_CHASE_PCT)):
        result.update(local_gate='WAIT',reason='검증구간 상승폭 3% 이상')
    elif (price_structure and record.get('primary_slope_now') is not None
          and record.get('secondary_slope_now') is not None
          and record['primary_slope_now']>0 and record['secondary_slope_now']>0
          and ratio>=CORE24_VOLUME_RATIO_MIN):
        result.update(local_gate='PASS',reason='저점상승+고점돌파+양 분봉 EMA 상승+거래량 회복')
    else:
        result.update(local_gate='WAIT',reason='가격 구조/EMA/거래량 동시 확인 안 됨')
    return result

def evaluate_core24_focus(rows5,cfg,core23):
    """Diagnostic replay on CORE.23 PRIMARY only, no extra exchange calls."""
    idx={r['t'].strftime('%Y-%m-%d %H:%M'):i for i,r in enumerate(rows5)}
    events=[]
    for old in core23.get('events',[]):
        if old.get('scan_phase')!='PRIMARY':
            continue
        rec={k:old.get(k) for k in (
            'market','base_id','signal_time_kst','search_time_kst',
            'delay_minutes','scan_phase','state','review_status',
            'local_guard','local_reason','postscan_entry_time_kst',
            'original_be_status','original_be_net_pct',
            'postscan_be_status','postscan_be_net_pct',
            'primary_high_break','primary_higher_lows','primary_slope_now',
            'secondary_slope_now','price_change_pct','chase_warning',
            'liquidity_tier','slowdown_primary','slowdown_secondary')}
        i=idx.get(old.get('search_time_kst'))
        # CORE.23 signal_i is bar starting at T, simulated entry bar T+5.
        setup=core24_setup(rows5,i)
        rec['entry_price']=round(setup[1],10) if setup else None
        rec['entry_time_kst']=(rows5[setup[0]]['t'].strftime('%Y-%m-%d %H:%M') if setup else '')
        rec['quality']=core24_quality_at_search(rows5,i,rec)
        rec['core27']=core27_structure_at_search(rows5,i)
        rec['fixed']={str(h):core24_fixed_exit(rows5,i,cfg,h) for h in CORE24_HORIZONS}
        rec['trailing']=core24_trailing_exit(rows5,i,cfg)
        rec['path']=core24_diagnostics(rows5,setup[0],setup[1],cfg) if setup else {
            'm'+str(h):dict(status='UNKNOWN',mfe_gross_pct=None,
              mae_gross_pct=None,mfe_net_upper_pct=None,
              mfe_first_min=None,mae_first_min=None) for h in CORE24_HORIZONS}
        # Explicit post-hoc label, NEVER a signal filter, only if complete.
        path=rec['path']['m60']
        be=rec.get('postscan_be_net_pct')
        if (path['status']!='READY' or rec.get('postscan_be_status')!='RESOLVED'
                or be is None):
            rec['failure_category']='UNDETERMINED'
        elif be>0:
            rec['failure_category']='BE_PROFIT'
        elif path['mfe_net_upper_pct']>=.5:
            rec['failure_category']='GIVEBACK_OPPORTUNITY'
        elif path['mfe_net_upper_pct']<=0:
            rec['failure_category']='NO_POSITIVE_MFE'
        else:
            rec['failure_category']='SMALL_OPPORTUNITY'
        # The original CORE.23 outputs are strictly read-only. Assertion
        # compares against the re-used (not substituted) baseline BE.
        assert rec['postscan_be_net_pct']==old.get('postscan_be_net_pct')
        assert rec['original_be_net_pct']==old.get('original_be_net_pct')
        events.append(rec)
    return dict(strategy=CORE24_LAB,events=events,
        phases=['PRIMARY'],delays_minutes=list(CORE23_DELAYS),
        source='CORE23 PRIMARY original B signal, exact same T+5m entry',
        quality_volume_threshold_4v12=CORE24_VOLUME_RATIO_MIN,
        time_exit_minutes=list(CORE24_HORIZONS),
        trail_gross_pct=CORE24_TRAIL_GROSS_PCT,
        trail_after_tp1='30% initial at +0.5% NET, 70% from next bar trailing peak with BE floor, max 240m',
        no_initial_fixed_stop=True,btc_gate='client-side BTC as-of T-5 only',
        diagnosis_horizons=list(CORE24_HORIZONS),
        no_future_in_quality=True,research_only=True,
        warnings=[
          'MFE uses OHLC HIGH, a theoretical upper bound not assured fill',
          'OHLC bars cannot order high/low; same-bar ambiguity is flagged',
          'fixed 15/30/60 exits use close, same next-open entry, no intrabar TP',
          'partial realized profit with unknown remainder is NOT resolved',
          'all tests are retrospective, not proven or approved live BUY',
          'no per-date hour selection from fitted sample; hold out later dates'])


# CORE27: independent, ex-ante 5-minute price-structure diagnostics.
# These are predeclared research hypotheses, NOT an approved live BUY gate.
def core27_structure_at_search(rows5, signal_i):
    """Assess ONLY 16 consecutive completed 5m candles before search time.

    The last observed bar is T-5m; the T bar (and the T+5m entry)
    are NOT used. A missing/invalid candle must be UNKNOWN, not False.
    The three-bar high breakout and rising-lows rules are tested
    separately and jointly to measure opportunity costs of strict gates.
    """
    out = dict(status='UNKNOWN', reason='데이터 부족',
               last_complete_kst='', higher_lows=None, high_break=None,
               previous_3_high=None, breakout_margin_pct=None,
               higher_low_step_pct=None)
    if signal_i is None or signal_i < 16 or signal_i >= len(rows5):
        return out
    hist = rows5[signal_i-16:signal_i]
    step = timedelta(minutes=5)
    if (len(hist) != 16 or
            hist[-1]['t'] + step != rows5[signal_i]['t'] or
            any(hist[j]['t']-hist[j-1]['t'] != step for j in range(1,16))):
        out['reason']='진입 직전 확정 5분봉 16개 연속성 부족'
        return out
    try:
        vals=[tuple(float(b[k]) for k in ('o','h','l','c')) for b in hist]
    except (ValueError,TypeError,KeyError,OverflowError):
        out['reason']='가격값 누락 또는 형식 오류'
        return out
    if any(not all(isfinite(v) and v>0 for v in bar) or
           bar[1]<max(bar[0],bar[3]) or bar[2]>min(bar[0],bar[3]) or
           bar[1]<bar[2] for bar in vals):
        out['reason']='비정상 OHLC 가격'
        return out
    highs=[v[1] for v in vals]; lows=[v[2] for v in vals]
    previous_high=max(highs[-4:-1])   # previous THREE completed 5m bars
    last_close=vals[-1][3]
    higher_lows=(lows[-1] > lows[-2] >= lows[-3])
    high_break=(last_close > previous_high)
    out.update(status='READY',reason='',
        last_complete_kst=hist[-1]['t'].strftime('%Y-%m-%d %H:%M'),
        higher_lows=bool(higher_lows), high_break=bool(high_break),
        previous_3_high=round(previous_high,10),
        breakout_margin_pct=round(100*(last_close/previous_high-1),6),
        higher_low_step_pct=round(100*(lows[-1]/lows[-2]-1),6))
    return out


# CORE25: direct B10M/SMA3 discovery instead of evaluating 12 MA arms and CORE2~19.
def core25_base_signals(rows6, cfg, market, kst):
    """Exactly the B_10M_MA03 subset of legacy CORE.10, no other MA arms.

    Gate precedence and 60-minute cross cooldown are preserved: cooldown
    starts at an MA cross even if the later Volman/quality gate rejects it.
    At no point are future candles inspected for signal detection.
    """
    first = datetime.combine(cfg['day'], _time.min, tzinfo=kst)
    end = first + timedelta(days=1)
    rows4 = [r for r in rows6 if r['t'] >= first-timedelta(hours=4)]
    bars10 = core10_resample_10m(rows4)
    pos5 = {r['t']:i for i,r in enumerate(rows6)}
    events=[]
    crossings=0
    last_cross_t=None
    for i in range(13,len(bars10)):
        bar=bars10[i]
        signal_t=bar['last_5m_t']
        if not first <= signal_t < end:
            continue
        if not lab_contiguous_tf(bars10,i-13,i,10):
            continue
        if last_cross_t is not None and signal_t-last_cross_t < timedelta(minutes=cfg['horizon']):
            continue
        if not core10_ma_cross(bars10,i,3):
            continue
        last_cross_t=signal_t
        crossings+=1
        if not core10_volman_gate(bars10,i,10):
            continue
        signal_i=pos5.get(signal_t)
        if signal_i is None:
            continue
        box_top=max(b['h'] for b in bars10[i-3:i])
        if not c5_beggs_v111430_quality(bar,box_top)['accepted']:
            continue
        stamp=signal_t.strftime('%Y-%m-%d %H:%M')
        baseline=core16_simulate_scale(rows6,signal_i,cfg,True)
        events.append(dict(market=market,base_id=market+'|'+stamp,
            signal_time_kst=stamp,
            entry_time_kst=(signal_t+timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M'),
            baseline=baseline))
    return events,dict(complete_10m=len(bars10),ma_crosses=crossings,signals=len(events))


def core25_rechecks(rows5,cfg,base_events,market,kst):
    """CORE.23 PRIMARY 10/20/30 only. Legacy hourly and other labs skipped."""
    by_t={r['t']:r for r in rows5}
    pos_by_t={r['t']:i for i,r in enumerate(rows5)}
    rows10=core20_aggregate_5m(rows5,10)
    rows15=core20_aggregate_5m(rows5,15)
    events=[]
    for base in base_events:
        stamp=base['signal_time_kst']
        signal_t=datetime.strptime(stamp,'%Y-%m-%d %H:%M').replace(tzinfo=kst)
        complete=signal_t+timedelta(minutes=5)
        b=base['baseline']
        old=dict(base, _baseline_status=b.get('status','UNKNOWN'),
            _baseline_net_pct=b.get('net_pct'),_baseline_outcome=b.get('outcome','UNKNOWN'))
        for delay in CORE23_DELAYS:
            search=complete+timedelta(minutes=delay)
            if search.date()!=cfg['day']:
                continue
            events.append(core23_snapshot(rows5,rows10,rows15,by_t,pos_by_t,
                cfg,old,complete,search,delay,'PRIMARY'))
    return dict(events=events)


def core25_focus(rows5,cfg,market,kst):
    """Core25 does not execute the old CORE.2~22 tree; original CORE24 math reused."""
    base,diagnostics=core25_base_signals(rows5,cfg,market,kst)
    rechecks=core25_rechecks(rows5,cfg,base,market,kst)
    report=evaluate_core24_focus(rows5,cfg,rechecks)
    # Trim network response: no historical tables, no duplicate raw candle arrays.
    slim=[]
    keep=('market','base_id','signal_time_kst','search_time_kst','delay_minutes',
          'state','review_status','local_guard','local_reason','liquidity_tier',
          'original_be_status','original_be_net_pct','postscan_be_status',
          'postscan_be_net_pct','entry_time_kst','entry_price','price_change_pct',
          'quality','core27','fixed','trailing','path','failure_category')
    for row in report['events']:
        slim.append({k:row.get(k) for k in keep})
    return dict(events=slim,coverage=diagnostics,version=VERSION,
        unchanged_strategy='B_10M_MA03 + CORE23 PRIMARY + CORE24 same-entry exits',
        core24_math_unchanged=True,research_only=True)



# CORE.28 reverse study — independent of CORE.27 signal/exits.
# Dates, benchmark outcomes, and snapshots are historic, research-only.
# The initial fixed 14 cases were selected after seeing their outcomes:
# never treat in-sample separation as out-of-sample predictive accuracy.
r28_FIVE = timedelta(minutes=5)
r28_FIB_LEVELS = (0.236,0.382,0.5,0.618,0.786)
r28_FEE_ROUNDTRIP_PCT = 0.50
R28_CASES_TEXT = '2026-11-10,KRW-MERL,2026-11-10 06:20,0.677973\n2026-11-10,KRW-HBAR,2026-11-10 15:00,\n2026-11-10,KRW-Q,2026-11-10 22:20,1.147132\n2026-11-12,KRW-ETH,2026-11-12 07:30,-0.889372\n2026-11-12,KRW-AVAX,2026-11-12 21:30,-1.371571\n2026-11-12,KRW-BTC,2026-11-12 21:30,-0.797811\n2026-11-12,KRW-HBAR,2026-11-12 23:50,-1.219694\n2026-11-13,KRW-AVAX,2026-11-13 17:20,0.478491\n2026-11-14,KRW-IRYS,2026-11-14 06:30,\n2026-11-14,KRW-PENGU,2026-11-14 17:30,0.024806\n2026-11-15,KRW-NEAR,2026-11-15 17:40,1.517225\n2026-11-16,KRW-DOGE,2026-11-16 01:40,-0.325965\n2026-11-16,KRW-Q,2026-11-16 09:10,-2.460667\n2026-11-16,KRW-BIRB,2026-11-16 21:50,-4.099262'
R28_CASES = []
for _i, _line in enumerate(R28_CASES_TEXT.splitlines()):
    _day, _market, _asof, _outcome = _line.split(',')
    R28_CASES.append(dict(index=_i, date=_day, market=_market, asof_kst=_asof,
                      historical_60m_net_pct=float(_outcome) if _outcome else None))


def r28_dt(s):
    return datetime.strptime(str(s)[:16].replace('T',' '), '%Y-%m-%d %H:%M')

def r28_stamp(d):
    return d.strftime('%Y-%m-%d %H:%M')

def r28_contiguous(bars):
    return bool(bars) and all(bars[i]['t'] - bars[i-1]['t'] == r28_FIVE for i in range(1, len(bars)))

def r28_ema(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1)
    x = float(values[0])
    out = []
    for v in values:
        x += k * (float(v) - x)
        out.append(x)
    return out

def r28_atr14(bars):
    if len(bars) < 15:
        return None
    tail = bars[-15:]
    if not r28_contiguous(tail):
        return None
    tr = [max(b['h']-b['l'], abs(b['h']-prev['c']), abs(b['l']-prev['c']))
          for prev, b in zip(tail[:-1], tail[1:])]
    return sum(tr) / 14

def r28_classify_nearest(ratio):
    if ratio is None or not math.isfinite(ratio) or ratio < 0 or ratio > 1.15:
        return ('', '')
    nearest = min(r28_FIB_LEVELS, key=lambda x: abs(x-ratio))
    return (round(nearest * 100, 1), round(abs(ratio-nearest)*100, 2))

def r28_swing_anchors(past):
    """Anchors use only as-of history, and require meaningful ordering.

    UP SWING: low before a past high, followed by pullback.
    DOWN SWING: high before a past low, followed by recovery.
    These are hypotheses, not automatic trading supports/resistances.
    """
    tail = past[-48:]
    if len(tail) < 48 or not r28_contiguous(tail):
        return None
    recent_high_pos = max(range(12, 44), key=lambda i: tail[i]['h'])
    low_pos = min(range(max(0, recent_high_pos-24), recent_high_pos-2),
                  key=lambda i: tail[i]['l'])
    up_low, up_high = tail[low_pos]['l'], tail[recent_high_pos]['h']
    # Down leg must start before trough and have a finite distance.
    low_down_pos = min(range(18, 48), key=lambda i: tail[i]['l'])
    high_candidates = range(max(0, low_down_pos-30), low_down_pos-2)
    high_down_pos = max(high_candidates, key=lambda i: tail[i]['h'])
    down_high, down_low = tail[high_down_pos]['h'], tail[low_down_pos]['l']
    atr = r28_atr14(tail)
    # Minimum 0.8% span avoids reporting arbitrary fib levels for flat micro-swings.
    up_ok = up_high > up_low * 1.008 and (recent_high_pos-low_pos) >= 3
    down_ok = down_high > down_low * 1.008 and (low_down_pos-high_down_pos) >= 3
    return dict(up_low=up_low if up_ok else None,
                up_high=up_high if up_ok else None,
                up_low_time=r28_stamp(tail[low_pos]['t']) if up_ok else '',
                up_high_time=r28_stamp(tail[recent_high_pos]['t']) if up_ok else '',
                down_high=down_high if down_ok else None,
                down_low=down_low if down_ok else None,
                down_high_time=r28_stamp(tail[high_down_pos]['t']) if down_ok else '',
                down_low_time=r28_stamp(tail[low_down_pos]['t']) if down_ok else '',
                atr=atr)

def r28_pct(a, b):
    return 100.0 * (a / b - 1.0) if b and b > 0 else None

def r28_completed_tf_ema(past_all, at, tf_minutes):
    """Aggregate past 5m candles only; incomplete 10/15m bars are excluded."""
    n = tf_minutes//5
    by_t = {b['t']: b for b in past_all}
    res = []
    for first in past_all:
        start=first['t']
        if start.minute % tf_minutes or start.second or start+timedelta(minutes=tf_minutes)>at:
            continue
        group=[by_t.get(start+r28_FIVE*i) for i in range(n)]
        if any(b is None for b in group):
            continue
        res.append(dict(t=start,c=group[-1]['c']))
    res.sort(key=lambda x:x['t'])
    tail=res[-20:]
    d=timedelta(minutes=tf_minutes)
    if len(tail)<20 or any(tail[i]['t']-tail[i-1]['t']!=d for i in range(1,len(tail))):
        return {f'tf{tf_minutes}_status':'INSUFFICIENT_OR_GAPPED'}
    # At most one current aggregate period may be unfinished.
    if not timedelta(0) <= (at-(tail[-1]['t']+d)) < d:
        return {f'tf{tf_minutes}_status':'STALE'}
    e5=r28_ema([b['c'] for b in tail],5)
    e10=r28_ema([b['c'] for b in tail],10)
    return {f'tf{tf_minutes}_status':'READY',
            f'tf{tf_minutes}_ema5_slope_pct':r28_pct(e5[-1],e5[-2]),
            f'tf{tf_minutes}_ema10_slope_pct':r28_pct(e10[-1],e10[-2]),
            f'tf{tf_minutes}_ema5_above_ema10':int(e5[-1]>e10[-1]),
            f'tf{tf_minutes}_close_above_ema5':int(tail[-1]['c']>e5[-1])}

def r28_features_at(bars, at):
    past_all = [b for b in bars if b['t'] + r28_FIVE <= at][-100:]
    past = past_all[-48:]
    if len(past) < 48 or not r28_contiguous(past[-48:]) or past[-1]['t']+r28_FIVE != at:
        return dict(feature_status='INSUFFICIENT_OR_MISSING_5M', asof_kst=r28_stamp(at))
    closes = [b['c'] for b in past]
    e5, e10, e20 = (r28_ema(closes, n) for n in (5, 10, 20))
    arr = past[-16:]
    v_prev = sum(b['volume'] for b in arr[:-4]) / 12
    v_last = sum(b['volume'] for b in arr[-4:]) / 4
    anchors = r28_swing_anchors(past)
    atr = r28_atr14(past)
    latest = past[-1]
    res = dict(feature_status='READY', asof_kst=r28_stamp(at), close=latest['c'],
               ema5=e5[-1], ema10=e10[-1], ema20=e20[-1],
               ema5_slope_15m_pct=r28_pct(e5[-1], e5[-4]),
               ema10_slope_15m_pct=r28_pct(e10[-1], e10[-4]),
               ema5_minus_ema10_pct=r28_pct(e5[-1], e10[-1]),
               close_vs_ema20_pct=r28_pct(latest['c'], e20[-1]),
               ema5_above_ema10=int(e5[-1] > e10[-1]),
               ema5_rising=int(e5[-1] > e5[-2]),
               price_above_ema5=int(latest['c'] > e5[-1]),
               low_rising_3=int(past[-3]['l'] <= past[-2]['l'] < past[-1]['l']),
               high_break=int(latest['c'] > max(b['h'] for b in past[-4:-1])),
               volume_ratio_4v12=(v_last/v_prev if v_prev > 0 else None),
               turnover_recent_4_krw=sum(b['turnover_krw'] for b in past[-4:]),
               atr14_pct=(100*atr/latest['c']) if atr else None)
    res.update(r28_completed_tf_ema(past_all, at, 10))
    res.update(r28_completed_tf_ema(past_all, at, 15))
    if anchors:
        res.update(anchors)
        lo, hi = anchors['up_low'], anchors['up_high']
        if lo is not None and hi is not None:
            recent_low = min(b['l'] for b in past[-12:])
            r = (hi - recent_low)/(hi-lo)
            near, err = r28_classify_nearest(r)
            res.update(up_retrace_recent_low_pct=100*r,
                       up_retrace_nearest_fib_pct=near,
                       up_retrace_distance_points=err)
            for level in r28_FIB_LEVELS:
                res[f'up_support_{level*100:g}_price'] = hi-(hi-lo)*level
        dlo, dhi = anchors['down_low'], anchors['down_high']
        if dlo is not None and dhi is not None:
            r = (latest['c']-dlo)/(dhi-dlo)
            near, err = r28_classify_nearest(r)
            res.update(down_recover_close_pct=100*r,
                       down_recover_nearest_fib_pct=near,
                       down_recover_distance_points=err)
            for level in r28_FIB_LEVELS:
                res[f'down_recovery_{level*100:g}_price'] = dlo+(dhi-dlo)*level
    return res

def r28_posthoc(bars, at, asof_features):
    """Future-only diagnosis, never a feature. UTC-free KST dates."""
    future = [b for b in bars if at <= b['t'] < at + timedelta(minutes=60)]
    if len(future) != 12 or not r28_contiguous(future) or future[0]['t'] != at:
        return dict(post_status='UNKNOWN_MISSING_60M')
    bottom = min(future, key=lambda x: x['l'])
    peak = max(future, key=lambda x: x['h'])
    last = future[-1]
    ref = asof_features.get('close')
    result = dict(post_status='READY', post_min_low=bottom['l'],
                  post_min_time_kst=r28_stamp(bottom['t']),
                  post_max_high=peak['h'], post_max_time_kst=r28_stamp(peak['t']),
                  post_close60=last['c'], post_close60_gross_pct=r28_pct(last['c'], ref),
                  post_close60_net_approx_pct=(r28_pct(last['c'], ref)-r28_FEE_ROUNDTRIP_PCT) if ref else None)
    lo, hi = asof_features.get('up_low'), asof_features.get('up_high')
    if lo and hi and hi > lo:
        ratio = (hi-bottom['l'])/(hi-lo)
        near, err = r28_classify_nearest(ratio)
        result.update(posthoc_pullback_low_pct=100*ratio,
                      posthoc_nearest_fib_pct=near,
                      posthoc_fib_distance_points=err)
    # First closing cross of EMA5 after bottom; a confirmative observation only.
    at_after_low = bottom['t'] + r28_FIVE
    for t in (at_after_low + r28_FIVE*i for i in range(12)):
        if t > at + timedelta(minutes=60):
            break
        f = r28_features_at(bars, t)
        if f.get('feature_status') == 'READY' and f['close'] > f['ema5'] and f['ema5_rising']:
            result['posthoc_first_ema5_reclaim_kst'] = r28_stamp(t)
            break
    return result


def r28_fetch_case_bars(api_get, market, asof):
    """Fetch past 6h + following 65m, using same established KST cursor API as CORE.27.

    Candles are unmodified historical Bithumb observations. Future candles are
    returned ONLY to posthoc diagnostics and never to as-of indicators.
    """
    begin=asof-timedelta(hours=6)
    end=asof+timedelta(minutes=65)
    cursor=end
    found={}
    pages=[]
    for page in range(4):
        raw=api_get('/v1/candles/minutes/5',{'market':market,'count':200,
                       'to':cursor.strftime('%Y-%m-%d %H:%M:%S')})
        if not isinstance(raw,list):
            raise ValueError('과거 5분봉 API 응답 형식 오류')
        oldest=None
        valid=0
        for item in raw:
            if not isinstance(item,dict):continue
            try:
                stamp=datetime.fromisoformat(item['candle_date_time_kst']).replace(tzinfo=None)
                op,hi,lo,cl=(float(item[k]) for k in ('opening_price','high_price','low_price','trade_price'))
                vol=float(item['candle_acc_trade_volume'])
                val=float(item.get('candle_acc_trade_price') or 0.)
                if not (0 < lo <= min(op,cl) <= hi and hi>=max(op,cl)
                    and all(isfinite(x) for x in (op,hi,lo,cl,vol,val)) and vol>=0 and val>=0):
                    continue
                valid+=1
                if oldest is None or stamp<oldest:oldest=stamp
                if begin<=stamp<end:
                    found[stamp]=dict(market=market,t=stamp,o=op,h=hi,l=lo,c=cl,
                                      volume=vol,turnover_krw=val)
            except (KeyError,ValueError,TypeError,OverflowError):
                continue
        pages.append(dict(page=page+1, returned=len(raw),valid=valid,
                          oldest_kst=r28_stamp(oldest) if oldest else ''))
        if oldest is None or oldest<=begin or len(raw)<200:break
        if oldest>=cursor:raise ValueError('과거 5분봉 페이지 조회 진행 오류')
        cursor=oldest
    rows=[found[t] for t in sorted(found)]
    return rows, pages

def r28_one_case(api_get, row):
    market=row['market']
    asof=r28_dt(row['asof_kst'])
    bars,pages=r28_fetch_case_bars(api_get,market,asof)
    features=r28_features_at(bars,asof)
    post=(r28_posthoc(bars,asof,features) if features.get('feature_status')=='READY'
          else {'post_status':'SKIPPED_NO_FEATURES'})
    snaps=[]
    for delta in (0,5,10,15,20,30,45,60):
        at=asof+timedelta(minutes=delta)
        snaps.append(dict(elapsed_min=delta,**r28_features_at(bars,at)))
    raw=[]
    for b in bars:
        raw.append({k:(r28_stamp(v) if k=='t' else v) for k,v in b.items()})
    return {'case':row,'features':features,'posthoc':post,
            'snapshots':snaps,'candles':raw,'fetch_pages':pages,'candles_count':len(bars),
            'research_only':True}


# CORE.29 — deterministic 8-day, 128-case stratified sample drawn only from
# as-of classification (not success or failure). Based on previously uploaded
# CORE.25/CORE.27 10-minute CSVs; 16 cases/day, up to 4/class, evenly spaced.
# A label at T+5m is attached for POST-HOC comparison only; never for selection.
R29_CASE_FIELDS = ('date','market','asof_kst','state','group','signal_time_kst',
                   'legacy_60m_status','legacy_60m_net_pct','legacy_benchmark_entry_kst')
R29_CASES_TEXT = '2026-11-09|KRW-Q|2026-11-09 06:00|INDETERMINATE|UNCERTAIN|2026-11-09 05:45|RESOLVED|-3.599211|2026-11-09 06:05\n2026-11-09|KRW-HBAR|2026-11-09 10:00|RECOVERY_CONFIRMED|UP|2026-11-09 09:45|RESOLVED|-0.629475|2026-11-09 10:05\n2026-11-09|KRW-PENGU|2026-11-09 10:00|RECOVERY_CONFIRMED|UP|2026-11-09 09:45|RESOLVED|-0.813409|2026-11-09 10:05\n2026-11-09|KRW-DEBIT|2026-11-09 11:00|UP_DECELERATING|UP|2026-11-09 10:45|UNKNOWN||2026-11-09 11:05\n2026-11-09|KRW-ONDO|2026-11-09 13:20|INDETERMINATE|UNCERTAIN|2026-11-09 13:05|RESOLVED|-1.112958|2026-11-09 13:25\n2026-11-09|KRW-XRP|2026-11-09 16:50|INDETERMINATE|UNCERTAIN|2026-11-09 16:35|RESOLVED|0.51381|2026-11-09 16:55\n2026-11-09|KRW-ETC|2026-11-09 17:30|REBOUND_WATCH|REBOUND|2026-11-09 17:15|RESOLVED|0.382764|2026-11-09 17:35\n2026-11-09|KRW-ETH|2026-11-09 17:30|REBOUND_WATCH|REBOUND|2026-11-09 17:15|RESOLVED|-0.463999|2026-11-09 17:35\n2026-11-09|KRW-US|2026-11-09 20:20|UP_UNCONFIRMED|UP|2026-11-09 20:05|RESOLVED|-0.156763|2026-11-09 20:25\n2026-11-09|KRW-ARB|2026-11-09 20:30|INDETERMINATE|UNCERTAIN|2026-11-09 20:15|RESOLVED|2.403367|2026-11-09 20:35\n2026-11-09|KRW-ENA|2026-11-09 20:30|INDETERMINATE|UNCERTAIN|2026-11-09 20:15|RESOLVED|0.467278|2026-11-09 20:35\n2026-11-09|KRW-SOL|2026-11-09 20:30|INDETERMINATE|UNCERTAIN|2026-11-09 20:15|RESOLVED|-0.329389|2026-11-09 20:35\n2026-11-09|KRW-XRP|2026-11-09 20:30|INDETERMINATE|UNCERTAIN|2026-11-09 20:15|RESOLVED|0.445026|2026-11-09 20:35\n2026-11-09|KRW-ZAMA|2026-11-09 20:30|UP_UNCONFIRMED|UP|2026-11-09 20:15|RESOLVED|0.268887|2026-11-09 20:35\n2026-11-09|KRW-WLD|2026-11-09 23:00|INDETERMINATE|UNCERTAIN|2026-11-09 22:45|RESOLVED|-0.272099|2026-11-09 23:05\n2026-11-09|KRW-MAGMA|2026-11-09 23:10|RECOVERY_CONFIRMED|UP|2026-11-09 22:55|UNKNOWN||2026-11-09 23:15\n2026-11-10|KRW-ONDO|2026-11-10 00:30|UP_UNCONFIRMED|UP|2026-11-10 00:15|RESOLVED|-1.07613|2026-11-10 00:35\n2026-11-10|KRW-SOL|2026-11-10 00:30|INDETERMINATE|UNCERTAIN|2026-11-10 00:15|RESOLVED|-1.251284|2026-11-10 00:35\n2026-11-10|KRW-SUI|2026-11-10 00:50|UP_UNCONFIRMED|UP|2026-11-10 00:35|RESOLVED|-2.10908|2026-11-10 00:55\n2026-11-10|KRW-ONDO|2026-11-10 03:10|REBOUND_WATCH|REBOUND|2026-11-10 02:55|RESOLVED|-1.468551|2026-11-10 03:15\n2026-11-10|KRW-ETH|2026-11-10 04:50|INDETERMINATE|UNCERTAIN|2026-11-10 04:35|RESOLVED|-0.395286|2026-11-10 04:55\n2026-11-10|KRW-MERL|2026-11-10 06:20|DOWNTREND|DOWN|2026-11-10 06:05|RESOLVED|0.677973|2026-11-10 06:25\n2026-11-10|KRW-BTC|2026-11-10 06:40|INDETERMINATE|UNCERTAIN|2026-11-10 06:25|RESOLVED|0.201684|2026-11-10 06:45\n2026-11-10|KRW-MERL|2026-11-10 10:50|REBOUND_WATCH|REBOUND|2026-11-10 10:35|RESOLVED|-1.202664|2026-11-10 10:55\n2026-11-10|KRW-Q|2026-11-10 14:10|RECOVERY_CONFIRMED|UP|2026-11-10 13:55|RESOLVED|-2.341911|2026-11-10 14:15\n2026-11-10|KRW-HBAR|2026-11-10 15:00|DOWNTREND|DOWN|2026-11-10 14:45|UNKNOWN||2026-11-10 15:05\n2026-11-10|KRW-ONDO|2026-11-10 17:50|INDETERMINATE|UNCERTAIN|2026-11-10 17:35|RESOLVED|-0.498753|2026-11-10 17:55\n2026-11-10|KRW-CHIP|2026-11-10 21:20|RECOVERY_CONFIRMED|UP|2026-11-10 21:05|RESOLVED|-1.063844|2026-11-10 21:25\n2026-11-10|KRW-Q|2026-11-10 22:20|DOWNTREND|DOWN|2026-11-10 22:05|RESOLVED|1.147132|2026-11-10 22:25\n2026-11-10|KRW-MAGMA|2026-11-10 22:40|UP_DECELERATING|UP|2026-11-10 22:25|UNKNOWN||2026-11-10 22:45\n2026-11-10|KRW-ETH|2026-11-10 23:50|INDETERMINATE|UNCERTAIN|2026-11-10 23:35|RESOLVED|-3.069936|2026-11-10 23:55\n2026-11-10|KRW-ONDO|2026-11-10 23:50|REBOUND_WATCH|REBOUND|2026-11-10 23:35|RESOLVED|-3.78965|2026-11-10 23:55\n2026-11-11|KRW-HBAR|2026-11-11 02:30|INDETERMINATE|UNCERTAIN|2026-11-11 02:15|UNKNOWN||2026-11-11 02:35\n2026-11-11|KRW-ZAMA|2026-11-11 03:50|INDETERMINATE|UNCERTAIN|2026-11-11 03:35|RESOLVED|-1.193769|2026-11-11 03:55\n2026-11-11|KRW-ONDO|2026-11-11 05:00|INDETERMINATE|UNCERTAIN|2026-11-11 04:45|RESOLVED|0.714677|2026-11-11 05:05\n2026-11-11|KRW-HYPE|2026-11-11 05:30|INDETERMINATE|UNCERTAIN|2026-11-11 05:15|RESOLVED|0.117036|2026-11-11 05:35\n2026-11-11|KRW-BTC|2026-11-11 06:00|INDETERMINATE|UNCERTAIN|2026-11-11 05:45|RESOLVED|-0.557209|2026-11-11 06:05\n2026-11-11|KRW-Q|2026-11-11 09:10|INDETERMINATE|UNCERTAIN|2026-11-11 08:55|RESOLVED|-0.887781|2026-11-11 09:15\n2026-11-11|KRW-ONDO|2026-11-11 09:50|INDETERMINATE|UNCERTAIN|2026-11-11 09:35|RESOLVED|0.500255|2026-11-11 09:55\n2026-11-11|KRW-XRP|2026-11-11 10:40|RECOVERY_CONFIRMED|UP|2026-11-11 10:25|RESOLVED|-0.290301|2026-11-11 10:45\n2026-11-11|KRW-HYPE|2026-11-11 15:00|UP_UNCONFIRMED|UP|2026-11-11 14:45|RESOLVED|-1.373376|2026-11-11 15:05\n2026-11-11|KRW-WLD|2026-11-11 15:00|UP_DECELERATING|UP|2026-11-11 14:45|RESOLVED|-1.444133|2026-11-11 15:05\n2026-11-11|KRW-SUI|2026-11-11 17:10|UP_UNCONFIRMED|UP|2026-11-11 16:55|RESOLVED|-1.208209|2026-11-11 17:15\n2026-11-11|KRW-ZRO|2026-11-11 17:10|INDETERMINATE|UNCERTAIN|2026-11-11 16:55|RESOLVED|-2.19721|2026-11-11 17:15\n2026-11-11|KRW-NEAR|2026-11-11 19:00|REBOUND_WATCH|REBOUND|2026-11-11 18:45|RESOLVED|0.234671|2026-11-11 19:05\n2026-11-11|KRW-SUI|2026-11-11 19:40|UP_ACCELERATING|UP|2026-11-11 19:25|RESOLVED|-0.587199|2026-11-11 19:45\n2026-11-11|KRW-RECALL|2026-11-11 20:10|INDETERMINATE|UNCERTAIN|2026-11-11 19:55|RESOLVED|-1.209169|2026-11-11 20:15\n2026-11-11|KRW-IRYS|2026-11-11 20:30|INDETERMINATE|UNCERTAIN|2026-11-11 20:15|UNKNOWN||2026-11-11 20:35\n2026-11-12|KRW-MAGMA|2026-11-12 03:20|INDETERMINATE|UNCERTAIN|2026-11-12 03:05|UNKNOWN||\n2026-11-12|KRW-HYPE|2026-11-12 05:10|INDETERMINATE|UNCERTAIN|2026-11-12 04:55|RESOLVED|-0.599924|2026-11-12 05:15\n2026-11-12|KRW-XRP|2026-11-12 06:00|UP_DECELERATING|UP|2026-11-12 05:45|RESOLVED|-0.77438|2026-11-12 06:05\n2026-11-12|KRW-ETH|2026-11-12 07:30|DOWNTREND|DOWN|2026-11-12 07:15|RESOLVED|-0.889372|2026-11-12 07:35\n2026-11-12|KRW-ETH|2026-11-12 15:20|REBOUND_WATCH|REBOUND|2026-11-12 15:05|RESOLVED|-0.606672|2026-11-12 15:25\n2026-11-12|KRW-AVAX|2026-11-12 15:40|INDETERMINATE|UNCERTAIN|2026-11-12 15:25|RESOLVED|-0.155645|2026-11-12 15:45\n2026-11-12|KRW-XCN|2026-11-12 17:20|INDETERMINATE|UNCERTAIN|2026-11-12 17:05|RESOLVED|0.527723|2026-11-12 17:25\n2026-11-12|KRW-CYS|2026-11-12 17:50|UP_DECELERATING|UP|2026-11-12 17:35|RESOLVED|-0.930233|2026-11-12 17:55\n2026-11-12|KRW-ETH|2026-11-12 17:50|INDETERMINATE|UNCERTAIN|2026-11-12 17:35|RESOLVED|-0.858353|2026-11-12 17:55\n2026-11-12|KRW-POKT|2026-11-12 18:10|UP_DECELERATING|UP|2026-11-12 17:55|RESOLVED|2.910065|2026-11-12 18:15\n2026-11-12|KRW-AVAX|2026-11-12 21:30|DOWNTREND|DOWN|2026-11-12 21:15|RESOLVED|-1.371571|2026-11-12 21:35\n2026-11-12|KRW-BTC|2026-11-12 21:30|DOWNTREND|DOWN|2026-11-12 21:15|RESOLVED|-0.797811|2026-11-12 21:35\n2026-11-12|KRW-PYBOBO|2026-11-12 23:30|UP_DECELERATING|UP|2026-11-12 23:15|RESOLVED|-3.69296|2026-11-12 23:35\n2026-11-12|KRW-APT|2026-11-12 23:50|INDETERMINATE|UNCERTAIN|2026-11-12 23:35|RESOLVED|-0.635056|2026-11-12 23:55\n2026-11-12|KRW-HBAR|2026-11-12 23:50|DOWNTREND|DOWN|2026-11-12 23:35|RESOLVED|-1.219694|2026-11-12 23:55\n2026-11-12|KRW-HYPE|2026-11-12 23:50|INDETERMINATE|UNCERTAIN|2026-11-12 23:35|RESOLVED|0.605064|2026-11-12 23:55\n2026-11-13|KRW-BTC|2026-11-13 01:10|REBOUND_WATCH|REBOUND|2026-11-13 00:55|RESOLVED|-0.852821|2026-11-13 01:15\n2026-11-13|KRW-XRP|2026-11-13 02:40|INDETERMINATE|UNCERTAIN|2026-11-13 02:25|RESOLVED|-0.140319|2026-11-13 02:45\n2026-11-13|KRW-HYPE|2026-11-13 03:10|INDETERMINATE|UNCERTAIN|2026-11-13 02:55|RESOLVED|-0.446658|2026-11-13 03:15\n2026-11-13|KRW-HYPE|2026-11-13 06:00|UP_DECELERATING|UP|2026-11-13 05:45|RESOLVED|-0.136366|2026-11-13 06:05\n2026-11-13|KRW-XRP|2026-11-13 09:00|INDETERMINATE|UNCERTAIN|2026-11-13 08:45|RESOLVED|-1.416792|2026-11-13 09:05\n2026-11-13|KRW-ZRO|2026-11-13 11:10|INDETERMINATE|UNCERTAIN|2026-11-13 10:55|RESOLVED|-1.864364|2026-11-13 11:15\n2026-11-13|KRW-AVAX|2026-11-13 14:30|INDETERMINATE|UNCERTAIN|2026-11-13 14:15|RESOLVED|1.135391|2026-11-13 14:35\n2026-11-13|KRW-APT|2026-11-13 15:30|UP_ACCELERATING|UP|2026-11-13 15:15|UNKNOWN||2026-11-13 15:35\n2026-11-13|KRW-CYS|2026-11-13 15:30|RECOVERY_CONFIRMED|UP|2026-11-13 15:15|RESOLVED|0.115191|2026-11-13 15:35\n2026-11-13|KRW-XRP|2026-11-13 15:30|UP_DECELERATING|UP|2026-11-13 15:15|RESOLVED|0.507036|2026-11-13 15:35\n2026-11-13|KRW-ETH|2026-11-13 16:10|UP_UNCONFIRMED|UP|2026-11-13 15:55|RESOLVED|-0.875795|2026-11-13 16:15\n2026-11-13|KRW-AVAX|2026-11-13 17:20|DOWNTREND_RISK|DOWN|2026-11-13 17:05|RESOLVED|0.478491|2026-11-13 17:25\n2026-11-13|KRW-BTC|2026-11-13 19:10|UP_ACCELERATING|UP|2026-11-13 18:55|RESOLVED|-0.57575|2026-11-13 19:15\n2026-11-13|KRW-STRK|2026-11-13 19:20|RECOVERY_CONFIRMED|UP|2026-11-13 19:05|RESOLVED|-0.001744|2026-11-13 19:25\n2026-11-13|KRW-ZRO|2026-11-13 19:30|REBOUND_WATCH|REBOUND|2026-11-13 19:15|RESOLVED|1.397412|2026-11-13 19:35\n2026-11-13|KRW-DOGE|2026-11-13 20:30|INDETERMINATE|UNCERTAIN|2026-11-13 20:15|RESOLVED|0.175832|2026-11-13 20:35\n2026-11-14|KRW-USDT|2026-11-14 00:30|UP_UNCONFIRMED|UP|2026-11-14 00:15|RESOLVED|-0.498753|2026-11-14 00:35\n2026-11-14|KRW-VIRTUAL|2026-11-14 02:40|REBOUND_WATCH|REBOUND|2026-11-14 02:25|UNKNOWN||2026-11-14 02:45\n2026-11-14|KRW-IRYS|2026-11-14 06:30|DOWNTREND|DOWN|2026-11-14 06:15|UNKNOWN||2026-11-14 06:35\n2026-11-14|KRW-CYS|2026-11-14 07:30|INDETERMINATE|UNCERTAIN|2026-11-14 07:15|RESOLVED|-2.394015|2026-11-14 07:35\n2026-11-14|KRW-BTC|2026-11-14 09:10|UP_ACCELERATING|UP|2026-11-14 08:55|RESOLVED|-0.793268|2026-11-14 09:15\n2026-11-14|KRW-KGEN|2026-11-14 09:10|INDETERMINATE|UNCERTAIN|2026-11-14 08:55|UNKNOWN||2026-11-14 09:15\n2026-11-14|KRW-DNA|2026-11-14 09:20|UP_UNCONFIRMED|UP|2026-11-14 09:05|RESOLVED|-1.855588|2026-11-14 09:25\n2026-11-14|KRW-MAGMA|2026-11-14 11:00|UP_ACCELERATING|UP|2026-11-14 10:45|RESOLVED|-1.619263|2026-11-14 11:05\n2026-11-14|KRW-TRUMP|2026-11-14 13:40|REBOUND_WATCH|REBOUND|2026-11-14 13:25|UNKNOWN||\n2026-11-14|KRW-ENA|2026-11-14 14:30|INDETERMINATE|UNCERTAIN|2026-11-14 14:15|UNKNOWN||2026-11-14 14:35\n2026-11-14|KRW-SUI|2026-11-14 14:30|INDETERMINATE|UNCERTAIN|2026-11-14 14:15|RESOLVED|-0.031172|2026-11-14 14:35\n2026-11-14|KRW-KAIA|2026-11-14 15:30|INDETERMINATE|UNCERTAIN|2026-11-14 15:15|RESOLVED|-1.572508|2026-11-14 15:35\n2026-11-14|KRW-PENGU|2026-11-14 17:30|DOWNTREND|DOWN|2026-11-14 17:15|RESOLVED|0.024806|2026-11-14 17:35\n2026-11-14|KRW-US|2026-11-14 19:00|INDETERMINATE|UNCERTAIN|2026-11-14 18:45|UNKNOWN||2026-11-14 19:05\n2026-11-14|KRW-USDT|2026-11-14 19:10|INDETERMINATE|UNCERTAIN|2026-11-14 18:55|RESOLVED|-0.498753|2026-11-14 19:15\n2026-11-14|KRW-Q|2026-11-14 19:20|REBOUND_WATCH|REBOUND|2026-11-14 19:05|RESOLVED|-0.069174|2026-11-14 19:25\n2026-11-15|KRW-ONDO|2026-11-15 02:40|REBOUND_WATCH|REBOUND|2026-11-15 02:25|UNKNOWN||2026-11-15 02:45\n2026-11-15|KRW-ADA|2026-11-15 09:00|REBOUND_WATCH|REBOUND|2026-11-15 08:45|RESOLVED|-2.440241|2026-11-15 09:05\n2026-11-15|KRW-DOGE|2026-11-15 09:00|RECOVERY_CONFIRMED|UP|2026-11-15 08:45|RESOLVED|-1.817066|2026-11-15 09:05\n2026-11-15|KRW-ETH|2026-11-15 09:00|UP_DECELERATING|UP|2026-11-15 08:45|RESOLVED|-2.408332|2026-11-15 09:05\n2026-11-15|KRW-HBAR|2026-11-15 09:00|RECOVERY_CONFIRMED|UP|2026-11-15 08:45|RESOLVED|-1.086384|2026-11-15 09:05\n2026-11-15|KRW-BTC|2026-11-15 09:30|INDETERMINATE|UNCERTAIN|2026-11-15 09:15|RESOLVED|-2.32373|2026-11-15 09:35\n2026-11-15|KRW-PUMPBTC|2026-11-15 11:00|REBOUND_WATCH|REBOUND|2026-11-15 10:45|RESOLVED|7.283643|2026-11-15 11:05\n2026-11-15|KRW-TRUMP|2026-11-15 12:20|UP_UNCONFIRMED|UP|2026-11-15 12:05|RESOLVED|-0.80073|2026-11-15 12:25\n2026-11-15|KRW-AVAX|2026-11-15 14:30|INDETERMINATE|UNCERTAIN|2026-11-15 14:15|RESOLVED|-0.847531|2026-11-15 14:35\n2026-11-15|KRW-DNA|2026-11-15 14:50|RECOVERY_CONFIRMED|UP|2026-11-15 14:35|RESOLVED|-1.747723|2026-11-15 14:55\n2026-11-15|KRW-DOGE|2026-11-15 17:40|REBOUND_WATCH|REBOUND|2026-11-15 17:25|RESOLVED|0.933547|2026-11-15 17:45\n2026-11-15|KRW-NEAR|2026-11-15 17:40|DOWNTREND|DOWN|2026-11-15 17:25|RESOLVED|1.517225|2026-11-15 17:45\n2026-11-15|KRW-XRP|2026-11-15 17:40|INDETERMINATE|UNCERTAIN|2026-11-15 17:25|RESOLVED|0.789534|2026-11-15 17:45\n2026-11-15|KRW-HUMA|2026-11-15 18:10|RECOVERY_CONFIRMED|UP|2026-11-15 17:55|RESOLVED|-0.152511|2026-11-15 18:15\n2026-11-15|KRW-LINK|2026-11-15 20:30|INDETERMINATE|UNCERTAIN|2026-11-15 20:15|UNKNOWN||2026-11-15 20:35\n2026-11-15|KRW-SOL|2026-11-15 20:30|INDETERMINATE|UNCERTAIN|2026-11-15 20:15|RESOLVED|0.000751|2026-11-15 20:35\n2026-11-16|KRW-DOGE|2026-11-16 01:40|DOWNTREND|DOWN|2026-11-16 01:25|RESOLVED|-0.325965|2026-11-16 01:45\n2026-11-16|KRW-DOGE|2026-11-16 03:10|UP_DECELERATING|UP|2026-11-16 02:55|RESOLVED|0.190945|2026-11-16 03:15\n2026-11-16|KRW-SOL|2026-11-16 03:10|INDETERMINATE|UNCERTAIN|2026-11-16 02:55|RESOLVED|-0.043022|2026-11-16 03:15\n2026-11-16|KRW-Q|2026-11-16 09:10|DOWNTREND|DOWN|2026-11-16 08:55|RESOLVED|-2.460667|2026-11-16 09:15\n2026-11-16|KRW-USDT|2026-11-16 09:20|INDETERMINATE|UNCERTAIN|2026-11-16 09:05|RESOLVED|-0.643272|2026-11-16 09:25\n2026-11-16|KRW-DOGE|2026-11-16 11:20|REBOUND_WATCH|REBOUND|2026-11-16 11:05|RESOLVED|-0.474285|2026-11-16 11:25\n2026-11-16|KRW-WLD|2026-11-16 11:20|REBOUND_WATCH|REBOUND|2026-11-16 11:05|RESOLVED|-0.228369|2026-11-16 11:25\n2026-11-16|KRW-XRP|2026-11-16 11:20|REBOUND_WATCH|REBOUND|2026-11-16 11:05|RESOLVED|-0.648492|2026-11-16 11:25\n2026-11-16|KRW-NEAR|2026-11-16 16:00|REBOUND_WATCH|REBOUND|2026-11-16 15:45|RESOLVED|-0.36113|2026-11-16 16:05\n2026-11-16|KRW-NCT|2026-11-16 16:40|INDETERMINATE|UNCERTAIN|2026-11-16 16:25|RESOLVED|-2.686639|2026-11-16 16:45\n2026-11-16|KRW-USDT|2026-11-16 18:20|UP_UNCONFIRMED|UP|2026-11-16 18:05|RESOLVED|-0.716639|2026-11-16 18:25\n2026-11-16|KRW-BIRB|2026-11-16 21:50|DOWNTREND_RISK|DOWN|2026-11-16 21:35|RESOLVED|-4.099262|2026-11-16 21:55\n2026-11-16|KRW-DOGE|2026-11-16 23:30|INDETERMINATE|UNCERTAIN|2026-11-16 23:15|RESOLVED|-2.130524|2026-11-16 23:35\n2026-11-16|KRW-ETH|2026-11-16 23:30|UP_UNCONFIRMED|UP|2026-11-16 23:15|RESOLVED|-2.356004|2026-11-16 23:35\n2026-11-16|KRW-LINK|2026-11-16 23:30|INDETERMINATE|UNCERTAIN|2026-11-16 23:15|RESOLVED|-2.701152|2026-11-16 23:35\n2026-11-16|KRW-SUI|2026-11-16 23:30|INDETERMINATE|UNCERTAIN|2026-11-16 23:15|RESOLVED|-2.603009|2026-11-16 23:35'
R29_CASES = []
for _idx, _rawline in enumerate(R29_CASES_TEXT.splitlines()):
    _parts=_rawline.split('|')
    if len(_parts)!=len(R29_CASE_FIELDS):
        raise ValueError('CORE29 case manifest malformed')
    _r=dict(zip(R29_CASE_FIELDS,_parts))
    _r['index']=_idx
    _r['historical_60m_net_pct']=(float(_r['legacy_60m_net_pct'])
                   if _r['legacy_60m_status']=='RESOLVED' and _r['legacy_60m_net_pct'] else None)
    R29_CASES.append(_r)


def r29_one_case(api_get, row):
    # Keep original CORE.28 OHLCV, Fibonacci and EMA as-of math unchanged.
    result=r28_one_case(api_get,row)
    bars=[dict(b,t=r28_dt(b['t'])) for b in result['candles']]
    asof=r28_dt(row['asof_kst'])
    series=[]
    for minutes in range(0,61,5):
        f=r28_features_at(bars,asof+timedelta(minutes=minutes))
        series.append(dict(elapsed_min=minutes,**f))
    # All after-T observations are POSTHOC. They can never influence an entry
    # decision at T. A confirmed cross is observable at candle close, not earlier.
    ready=[s for s in series if s.get('feature_status')=='READY']
    def first_min(predicate):
        for x in ready:
            if predicate(x):return x['elapsed_min']
        return None
    base=ready[0] if ready and ready[0]['elapsed_min']==0 else None
    cross_first=first_min(lambda x: x['elapsed_min']>0 and x['ema5_above_ema10']==1)
    first_up=first_min(lambda x: x['elapsed_min']>0 and x['ema5_rising']==1)
    confirmed2=confirmed3=None
    for n in (2,3):
        for i in range(n-1,len(ready)):
            w=ready[i-n+1:i+1]
            if w[-1]['elapsed_min']==0:continue
            if (all(x['ema5_above_ema10']==1 for x in w) and
                all(w[j]['elapsed_min']-w[j-1]['elapsed_min']==5 for j in range(1,n))):
                if n==2: confirmed2=w[-1]['elapsed_min']
                else:confirmed3=w[-1]['elapsed_min']
                break
    if base is None: phase='UNKNOWN'
    elif base['ema5_above_ema10']==1:phase='ALREADY_ABOVE_AT_T'
    elif confirmed2 is None:phase='NOT_CONFIRMED_WITHIN_60M'
    elif confirmed2<=20:phase='EARLY_5_20M'
    elif confirmed2<=40:phase='MID_25_40M'
    else:phase='LATE_45_60M'
    status=row.get('legacy_60m_status')
    net=row.get('historical_60m_net_pct')
    if status!='RESOLVED' or net is None: outcome='UNKNOWN'
    elif net >= .5:outcome='GAIN_0_5_PLUS'
    elif net>0:outcome='SMALL_GAIN'
    else:outcome='LOSS_OR_FLAT'
    post=dict(result.get('posthoc') or {})
    post.update(r29_ema_cross_first_min=cross_first,
                r29_ema5_turn_up_first_min=first_up,
                r29_ema_above_2bars_confirm_min=confirmed2,
                r29_ema_above_3bars_confirm_min=confirmed3,
                r29_ema_phase=phase,
                r29_outcome_label=outcome,
                r29_early_volume_ratio=next((s.get('volume_ratio_4v12') for s in ready
                               if s['elapsed_min']==15),None),
                r29_ema5_slope_15m_at_20=next((s.get('ema5_slope_15m_pct') for s in ready
                               if s['elapsed_min']==20),None),
                r29_fib_at_t_pct=(base or {}).get('up_retrace_recent_low_pct'),
                r29_fib_nearest_at_t_pct=(base or {}).get('up_retrace_nearest_fib_pct'),
                r29_fib_distance_points_at_t=(base or {}).get('up_retrace_distance_points'),
                r29_label_basis='CORE25/27 10m recheck T+5 entry / 60m close / net 0.5% fee',
                r29_sample_basis='16 per day as-of trend stratified, no outcome sorting')
    result['posthoc']=post
    result['snapshots']=series
    result['case']['dataset']='CORE29_STRATIFIED_8DAY'
    return result


# CORE.30/31 market-wide reverse research: snapshots are selected BEFORE future
# outcomes using only fully closed 5-minute candles. No scanner BUY gating.
# A market contributes <=1 observation per 60 minutes; overlapping outcomes
# within a market are minimized, but observations are not independent.
C30_FIVE=timedelta(minutes=5)
C30_COST_DEFAULT=.25

def c30_return_pct(a, b):
    return 100*(a/b-1) if b and b>0 else None

def c30_net_pct(entry, exit_price, fee_pct, slip_pct):
    cost=(float(fee_pct)+float(slip_pct))/100
    return 100*(exit_price*(1-cost)/(entry*(1+cost))-1)

def c30_bar_shape(bar, atr):
    span=bar['h']-bar['l'];body=abs(bar['c']-bar['o'])
    upper=bar['h']-max(bar['o'],bar['c'])
    lower=min(bar['o'],bar['c'])-bar['l']
    return dict(body_range_ratio=body/span if span else None,
                lower_wick_ratio=lower/span if span else None,
                upper_wick_ratio=upper/span if span else None,
                close_position=(bar['c']-bar['l'])/span if span else None,
                range_atr_ratio=span/atr if atr and atr>0 else None,
                body_atr_ratio=body/atr if atr and atr>0 else None,
                bullish=int(bar['c']>bar['o']))

def c30_rsi14(values):
    if len(values)<15:return None
    diffs=[values[i]-values[i-1] for i in range(len(values)-14,len(values))]
    gain=sum(max(0.,v) for v in diffs)/14
    loss=sum(max(0.,-v) for v in diffs)/14
    if not gain and not loss:return 50.
    if not loss:return 100.
    return 100-100/(1+gain/loss)

def c30_features_at(rows,i,e5,e10,e20):
    """As-of features rely only on rows[:i+1]. Return None for gapped data."""
    if i<60 or not lab_contiguous(rows,i-59,i):return None
    tail=rows[i-59:i+1];last=tail[-1]
    atr=r28_atr14(tail)
    if atr is None or atr<=0:return None
    first,second,third=tail[-3:]
    shapes=[c30_bar_shape(b,atr) for b in (first,second,third)]
    close=last['c'];cs=[b['c'] for b in tail]
    vol4=sum(b['v'] for b in tail[-4:])/4
    vol12=sum(b['v'] for b in tail[-16:-4])/12
    turnover4=sum(float(b['v'])*float(b['c']) for b in tail[-4:])
    prevgap=e5[i-3]-e10[i-3];gap=e5[i]-e10[i]
    # Completed lookback with ordering validated as-of only
    past28=[{'t':b['t'].replace(tzinfo=None),'o':b['o'],'h':b['h'],
             'l':b['l'],'c':b['c'],'volume':b['v'],
             'turnover_krw':float(b['v'])*float(b['c'])} for b in tail[-48:]]
    anchors=r28_swing_anchors(past28)
    out={
      'observation_kst':r28_stamp((last['t']+C30_FIVE).replace(tzinfo=None)),
      'close':close,
      'prior_30m_return_pct':c30_return_pct(close,rows[i-6]['c']),
      'prior_60m_return_pct':c30_return_pct(close,rows[i-12]['c']),
      'prior_2h_return_pct':c30_return_pct(close,rows[i-24]['c']),
      'ema5_vs_10_pct':c30_return_pct(e5[i],e10[i]),
      'ema10_vs_20_pct':c30_return_pct(e10[i],e20[i]),
      'ema_gap_narrow_15m_pct':100*(abs(prevgap)-abs(gap))/close,
      'ema5_slope_15m_pct':c30_return_pct(e5[i],e5[i-3]),
      'ema10_slope_15m_pct':c30_return_pct(e10[i],e10[i-3]),
      'ema20_slope_15m_pct':c30_return_pct(e20[i],e20[i-3]),
      'ema5_slope_acceleration_pct':c30_return_pct(e5[i],e5[i-3])-c30_return_pct(e5[i-3],e5[i-6]),
      'close_vs_ema20_pct':c30_return_pct(close,e20[i]),
      'rsi14':c30_rsi14(cs),
      'atr14_pct':100*atr/close,
      'volume_ratio_4_vs_12':vol4/vol12 if vol12>0 else None,
      'turnover_last20m_krw':turnover4,
      'last_candle_bullish':shapes[-1]['bullish'],
      'last3_bullish_count':sum(x['bullish'] for x in shapes),
      'last3_body_pattern':''.join('U' if b['c']>b['o'] else ('D' if b['c']<b['o'] else 'F') for b in (first,second,third)),
      'last_candle_body_ratio':shapes[-1]['body_range_ratio'],
      'last_candle_lower_wick_ratio':shapes[-1]['lower_wick_ratio'],
      'last_candle_upper_wick_ratio':shapes[-1]['upper_wick_ratio'],
      'last_candle_close_position':shapes[-1]['close_position'],
      'last_candle_range_atr':shapes[-1]['range_atr_ratio'],
      'last_candle_body_atr':shapes[-1]['body_atr_ratio'],
      'prior_candle_body_atr':shapes[-2]['body_atr_ratio'],
      'prior2_candle_body_atr':shapes[0]['body_atr_ratio'],
      'last3_lower_wick_avg':sum(x['lower_wick_ratio'] or 0 for x in shapes)/3,
      'last3_body_atr_avg':sum(x['body_atr_ratio'] or 0 for x in shapes)/3,
      'last3_range_atr_avg':sum(x['range_atr_ratio'] or 0 for x in shapes)/3,
      'higher_low_3':int(first['l']<=second['l']<third['l']),
      'new_low_3':int(third['l']<min(first['l'],second['l'])),
      'close_above_prev_high':int(last['c']>rows[i-1]['h']),
      'previous20m_low_distance_pct':c30_return_pct(close,min(b['l'] for b in tail[-4:])),
      'candle1_o':first['o'],'candle1_h':first['h'],'candle1_l':first['l'],'candle1_c':first['c'],
      'candle2_o':second['o'],'candle2_h':second['h'],'candle2_l':second['l'],'candle2_c':second['c'],
      'candle3_o':third['o'],'candle3_h':third['h'],'candle3_l':third['l'],'candle3_c':third['c']}
    if anchors:
        out['fib_up_anchor_low_kst']=anchors.get('up_low_time','')
        out['fib_up_anchor_high_kst']=anchors.get('up_high_time','')
        out['fib_down_anchor_high_kst']=anchors.get('down_high_time','')
        out['fib_down_anchor_low_kst']=anchors.get('down_low_time','')
        lo,hi=anchors.get('up_low'),anchors.get('up_high')
        if lo is not None and hi is not None and hi>lo:
            r=(hi-min(b['l'] for b in tail[-12:]))/(hi-lo)
            near,err=r28_classify_nearest(r)
            out.update(fib_up_retrace_pct=100*r,fib_up_nearest_pct=near,
                       fib_up_distance_points=err)
        dlo,dhi=anchors.get('down_low'),anchors.get('down_high')
        if dlo is not None and dhi is not None and dhi>dlo:
            r=(close-dlo)/(dhi-dlo)
            near,err=r28_classify_nearest(r)
            out.update(fib_down_recover_pct=100*r,fib_down_nearest_pct=near,
                       fib_down_distance_points=err)
    return out

def c30_is_down(rows,i,e5,e10,e20):
    """Predetermined decline cohort, independent of any future bounce."""
    if i<60 or not lab_contiguous(rows,i-59,i):return False
    return bool(e5[i]<e10[i]<e20[i] and rows[i]['c']<e10[i] and
                e10[i]<e10[i-3] and rows[i]['c']<rows[i-6]['c'])

def c30_future(rows,i,e5,e10,fee,slip):
    """Outcomes are separately labeled POSTHOC. Entry at NEXT 5m opening."""
    results={'future_status':'UNKNOWN','post_label':'UNKNOWN'}
    if i+1>=len(rows):return results
    entry=rows[i+1]['o']
    if entry<=0:return results
    results['post_entry_time_kst']=r28_stamp(rows[i+1]['t'].replace(tzinfo=None))
    results['post_entry_price']=entry
    for minutes in (15,30,60,120):
        end=i+minutes//5
        key=f'post_{minutes}m'
        if end>=len(rows) or not lab_contiguous(rows,i+1,end):
            results[f'{key}_status']='MISSING_OR_GAP';continue
        price=rows[end]['c']
        results[f'{key}_status']='READY'
        results[f'{key}_net_pct']=round(c30_net_pct(entry,price,fee,slip),6)
    if results.get('post_60m_status')!='READY':return results
    window=rows[i+1:i+13]
    results['future_status']='READY'
    x=results['post_60m_net_pct']
    results['post_label']='STRONG' if x>=.5 else ('SMALL_GAIN' if x>0 else 'LOSS')
    results['post_mfe60_gross_pct']=round(c30_return_pct(max(x['h'] for x in window),entry),6)
    results['post_mae60_gross_pct']=round(c30_return_pct(min(x['l'] for x in window),entry),6)
    results['post_net_high_upper_bound_pct']=round(c30_net_pct(entry,max(x['h'] for x in window),fee,slip),6)
    # First 2 consecutive completed 5m candles with EMA5 > EMA10.
    # This is a POST-HOC diagnostic, not an input to candidate selection.
    for j in range(i+1,min(i+13,len(rows)-1)):
        if e5[j]>e10[j] and e5[j+1]>e10[j+1] and lab_contiguous(rows,i+1,j+1):
            results['post_ema2_confirm_min']=(j+2-(i+1))*5
            break
    return results

def c30_btc_context(rows,day):
    """BTC as-of 60m return, hourly/quarter-hour lookup only."""
    by={b['t']:b for b in rows}
    start=datetime.combine(day,_time.min,tzinfo=rows[0]['t'].tzinfo)
    context={}
    for j in range(96):
        at=start+timedelta(minutes=15*j)
        last=by.get(at-C30_FIVE);prev=by.get(at-timedelta(minutes=65))
        if last and prev and last['c']>0 and prev['c']>0:
            context[r28_stamp(at.replace(tzinfo=None))]=round(c30_return_pct(last['c'],prev['c']),5)
    return context

def c30_market_events(rows,day,market,fee=.25,slip=0.,btc_by_time=None,truncated=False):
    """Scan all 15m scheduled instants and retain first DOWN snapshot per hour.

    No ranking by realized return and no dependency on CORE.29 BUY events.
    """
    closes=[b['c'] for b in rows]
    e5=r28_ema(closes,5);e10=r28_ema(closes,10);e20=r28_ema(closes,20)
    events=[];stale=None;eligible=0;down_hits=0;unknown_past=0
    for i,b in enumerate(rows):
        at=b['t']+C30_FIVE
        if at.date()!=day or at.minute%15!=0:continue
        eligible+=1
        if i<60 or not lab_contiguous(rows,i-59,i):
            unknown_past+=1;continue
        if not c30_is_down(rows,i,e5,e10,e20):continue
        down_hits+=1
        if stale is not None and at-stale<timedelta(minutes=60):continue
        f=c30_features_at(rows,i,e5,e10,e20)
        if not f:unknown_past+=1;continue
        key=r28_stamp(at.replace(tzinfo=None))
        f['btc_60m_return_pct']=(btc_by_time or {}).get(key)
        f['btc_status']='READY' if f['btc_60m_return_pct'] is not None else 'UNKNOWN'
        outcome=c30_future(rows,i,e5,e10,fee,slip)
        events.append({'market':market,'date':day.isoformat(),'asof_kst':key,
                       'sample_basis':'EXANTE_DOWN_15M_GRID_60M_COOLDOWN',
                       'feature_status':'READY','features':f,'posthoc':outcome})
        stale=at
    return {'market':market,'date':day.isoformat(),
            'coverage':{'observed_5m':len(rows),'grid_checked':eligible,
                        'grid_down_hits':down_hits,'grid_missing_prehistory':unknown_past,
                        'down_snapshots':len(events),'page_limit_reached':bool(truncated)},
            'events':events,'research_only':True}

CORE_PAGE = '<!doctype html>\n<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n<title>V2 CORE.31 LIGHT · 시장 전체 반등 원인 연구</title>\n<style>\n:root{color-scheme:dark;--bg:#0b1020;--panel:#141d30;--line:#293750;--ink:#ecf2ff;--muted:#9aaccb;--blue:#779cff;--good:#4ade80;--bad:#ff8590}\n*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 system-ui,\'Noto Sans KR\',sans-serif}\nmain{max-width:980px;margin:auto;padding:20px 13px 58px}h1{font-size:23px;margin:0 0 3px}h2{font-size:17px;margin:0 0 12px}h3{font-size:14px;margin:0 0 10px}p{margin:5px 0 12px}\na{color:#b5c8ff}.muted{color:var(--muted);font-size:12px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:16px;margin:0 0 13px}\n.toolbar{display:flex;flex-wrap:wrap;gap:9px;align-items:flex-end}.field{display:flex;flex-direction:column;gap:5px;min-width:130px;flex:1}.field.small{min-width:95px;flex:.55}\nlabel{font-size:12px;color:var(--muted)}input,select,button{background:#0e172a;color:var(--ink);border:1px solid #465572;border-radius:8px;padding:10px;font:inherit;max-width:100%}button{background:#385fd4;border-color:#426ce9;font-weight:650;cursor:pointer}button.secondary{background:#233148;border-color:#43536b}button:disabled{opacity:.4;cursor:not-allowed}\nprogress{width:100%;height:11px;margin-top:15px}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.kpi{border:1px solid var(--line);border-radius:9px;padding:10px}.kpi strong{font-size:22px;display:block}.good{color:var(--good)}.bad{color:var(--bad)}.cols{display:flex;gap:12px;flex-wrap:wrap}.cols .field{max-width:205px}.table-scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:620px}th,td{padding:9px 8px;border-bottom:1px solid var(--line);white-space:nowrap;text-align:right;font-size:12px}th:first-child,td:first-child{text-align:left}th{color:#c2cff0;font-weight:650}tr:last-child td{border-bottom:0}\nsmall{font-size:11px}.note{background:#0e192d;padding:12px;border-radius:9px;color:var(--muted);font-size:12px}details{margin-top:10px}summary{cursor:pointer;font-weight:600;padding:8px 0}\n@media(max-width:620px){main{padding:14px 9px 42px}h1{font-size:20px}.panel{padding:12px}.kpis{grid-template-columns:repeat(2,1fr)}.field{min-width:45%}.field.small{min-width:45%}button{padding:10px 9px}}\n</style></head>\n<body><main>\n<h1>🧪 V2 CORE.31 LIGHT · 하락→상승 공통점 연구</h1>\n<p class="muted">기존 CORE.27 BUY·청산 계산 유지 · CORE.31 전체 하락 코인 역추적 (별도 실행)</p>\n<p><a href="/">← 기존 실전 V2 스캐너로 돌아가기</a></p>\n<section class="panel"><h2>① 검사 설정</h2>\n<div class="toolbar">\n<div class="field"><label for="day">날짜 (KST · 완료된 하루)</label><input id="day" type="date"></div>\n<div class="field"><label for="scope">범위</label><select id="scope"><option value="20">거래대금 상위 20</option><option value="40" selected>상위 40</option><option value="80">상위 80</option><option value="all">전체 KRW 종목</option></select></div>\n<div class="field small"><label for="fee">편도 수수료 (%)</label><input id="fee" type="number" min="0" max="0.5" step="0.01" value="0.25"></div>\n<div class="field small"><label for="slip">편도 슬리피지 (%)</label><input id="slip" type="number" min="0" max="0.5" step="0.01" value="0"></div>\n<button id="run">검사 시작</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="csv" class="secondary" disabled>CORE.31 일반검사 CSV</button>\n</div>\n<progress id="progress" max="1" value="0"></progress>\n<div id="status" class="muted" aria-live="polite">검사 전 · BTC 맥락은 날짜당 한 번만 조회해.</div>\n<div class="muted">전체 종목은 실시간 API 응답 속도에 따라 오래 걸릴 수 있어. 거래대금 순위는 현재 조회 기준이므로 과거 시장 순위의 완전한 재현은 아니야.</div>\n</section>\n<section class="panel"><h2>② 핵심 결과</h2>\n<div class="kpis">\n<div class="kpi"><span class="muted">검사 완료</span><strong id="done">—</strong></div>\n<div class="kpi"><span class="muted">B형 원본 신호</span><strong id="signals">—</strong></div>\n<div class="kpi"><span class="muted">API 실패</span><strong id="failures">—</strong></div>\n<div class="kpi"><span class="muted">경과 시간</span><strong id="elapsed">—</strong></div>\n</div>\n<h3 style="margin-top:16px">10·20·30분 재검증 요약 · 전체 원본 신호 기준</h3>\n<div class="table-scroll"><table><thead><tr><th>대기</th><th>평가</th><th>기존 PASS</th><th>신규 PASS</th><th>BE 청산 확정</th><th>BE 평균 순수익</th></tr></thead><tbody id="delay-summary"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n</section>\n<section class="panel"><h2>③ 진입 품질·청산 방식 비교</h2>\n<div class="cols"><div class="field"><label for="delay">대기 시간</label><select id="delay"><option value="10" selected>10분</option><option value="20">20분</option><option value="30">30분</option></select></div>\n<div class="field"><label for="cohort">분석 대상</label><select id="cohort"><option value="ALL">전체 원본 신호</option><option value="A">기존 CORE.23 PASS</option><option value="B">신규 CORE.24 품질 PASS</option><option value="BOTH">두 필터 모두 PASS</option><option value="C">CORE.26 · 3중 확인</option>\n<option value="HL">CORE.27 · 저점 상승</option><option value="BR">CORE.27 · 고점 돌파</option>\n<option value="HBR">CORE.27 · 저점+고점</option>\n<option value="CHL">3중 확인 + 저점 상승</option><option value="CBR">3중 확인 + 고점 돌파</option>\n<option value="CBOTH">3중 확인 + 저점+고점</option></select></div></div>\n<p class="muted" id="selected-note">진입품질 필터와 청산 규칙을 분리해 비교해.</p>\n<p class="muted">CORE.26 3중 확인: BTC 최근 60분 +0.3% 이상 + 해당 코인 검증구간 상승 + 최근 4개 5분봉 평균 거래량이 앞선 12개 이상. CORE.27은 고점 돌파·저점 상승을 각각 추가 검증. <b>실전 BUY 조건 미변경</b>, 추가 API 호출 없음.</p>\n<h3>진입 조건별 기존 BE 성과</h3>\n<div class="table-scroll"><table><thead><tr><th>필터</th><th>후보</th><th>청산 확정</th><th>수익 거래</th><th>평균 순수익</th></tr></thead><tbody id="quality-body"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<h3 style="margin-top:15px">CORE.27 · 10분 후 가격 구조 비교 (독립 연구)</h3>\n<p class="muted">진입 전 완료된 5분봉 16개만 사용. <b>저점 상승</b>: 최근 세 봉의 저점이 순차적으로 높아짐(마지막은 엄격 상승). <b>고점 돌파</b>: 마지막 확정봉 종가가 직전 세 봉 최고가 돌파. 개별 조건과 3중 확인 조합을 분리해서 비교해. 아직 실전 BUY에 적용하지 않아.</p>\n<div class="table-scroll"><table><thead><tr><th>실험 조건 (10분)</th><th>후보</th><th>청산 확정</th><th>수익 거래</th><th>평균 순수익</th><th>제외된 수익*</th><th>판정 미확인</th></tr></thead><tbody id="structure27-body"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<p class="muted">* 제외된 수익은 같은 10분 표본에서 BE 청산이 확정되고 수익이었으나 해당 필터가 PASS하지 않은 건수(데이터 부족 포함). 기존 BE 순손익 기준. 필터가 인과적으로 수익을 개선했다는 뜻은 아니야.</p>\n<h3 style="margin-top:15px">같은 진입가에서 청산 방식 5가지 비교</h3>\n<div class="table-scroll"><table><thead><tr><th>청산 방식</th><th>확정 / 후보</th><th>수익 거래</th><th>평균 순수익</th><th>공통 표본 평균</th><th>공통 BE 대비</th></tr></thead><tbody id="exit-body"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<div class="note" id="matched-note">각 청산 방식의 확정 수가 다르면 평균을 직접 비교하지 말고, 다섯 방식이 모두 확정된 동일 표본 비교를 확인해야 해.</div>\n</section>\n<section class="panel"><h2>④ 실패 원인·종목 상세</h2>\n<div class="table-scroll"><table><thead><tr><th>BE 수익</th><th>수익 반납 가능성</th><th>플러스 기회 없음</th><th>작은 상승 기회</th><th>판정 불가</th></tr></thead><tbody id="diagnosis-body"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<p class="muted">MFE·MAE는 실제 5분봉 관측 고가·저가 기준의 사후 진단이야. 매매 시점에는 미래 가격을 쓰지 않아.</p>\n<details><summary>상세 종목 펼치기 (화면 최대 80건 · CSV는 전체)</summary>\n<div class="table-scroll"><table><thead><tr><th>검증 시각</th><th>코인</th><th>분류</th><th>기존 PASS</th><th>신규 PASS</th><th>BE</th><th>추적</th><th>30분 종가</th><th>MFE / MAE (60분)</th></tr></thead><tbody id="details-body"><tr><td colspan="9">검사 전</td></tr></tbody></table></div>\n</details></section>\n<div class="note">연구용 가상 체결 결과야. 기존 실전 BUY 로직과 주문 기능은 변경하지 않아. 갭·저유동성·분봉 내 고저가 순서 불명으로 실제 거래 결과와 다를 수 있어. 추적손절은 첫 익절 이전에 고정 손절이 없어 큰 손실 위험이 남아 있어.</div>\n\n<section class="panel" id="core31-research">\n<h2>⑤ CORE.31 · 전체 KRW 하락→상승 다중 날짜 검증</h2>\n<p class="muted">기존 BUY 후보와 관계없이 <b>하락하던 모든 분석 가능 종목</b>을 조사해. 하락 당시의 캔들 크기·꼬리·종가 위치·EMA·피보나치·거래량을 기록하고, 이후 A(상승 유지) / B(반등 후 반납) / C(반등 부족)를 비교해. 매수 조건은 바꾸지 않아.</p>\n<div class="toolbar">\n<div class="field"><label for="end31">검사 마지막 날짜 (KST · 종료된 하루)</label><input id="end31" type="date"></div>\n<div class="field small"><label for="days31">날짜 수</label><select id="days31"><option value="1">1일 점검</option><option value="3" selected>3일 검증</option><option value="5">5일 비교</option><option value="7">7일 비교</option></select></div>\n<div class="field"><label for="scope31">대상</label><select id="scope31"><option value="40">현재 거래대금 상위 40</option><option value="80">상위 80</option><option value="all" selected>전체 KRW 종목</option></select></div>\n<div class="field small"><label for="fee31">편도 수수료 (%)</label><input id="fee31" type="number" min="0" max="0.5" step="0.01" value="0.25"></div>\n<div class="field small"><label for="slip31">편도 슬리피지 (%)</label><input id="slip31" type="number" min="0" max="0.5" step="0.01" value="0"></div>\n<button id="run31">다중 날짜 하락→상승 검사</button><button id="pause31" class="secondary" disabled>일시 중지</button><button id="csv31" class="secondary" disabled>CORE.31 통합 CSV 다운로드</button>\n</div>\n<progress id="progress31" max="1" value="0"></progress>\n<p id="status31" class="muted" aria-live="polite">검사 전 · 날짜별로 조회하며, 도중에도 현재까지의 CSV를 저장할 수 있어.</p>\n<div class="kpis">\n<div class="kpi"><span class="muted">코인×날짜 검사</span><strong id="k31done">—</strong></div>\n<div class="kpi"><span class="muted">하락 관찰점 (전체)</span><strong id="k31cases">—</strong></div>\n<div class="kpi"><span class="muted">연속봉 분석 가능 종목·날짜</span><strong id="k31eligible">—</strong></div>\n<div class="kpi"><span class="muted">API 실패</span><strong id="k31errs">—</strong></div>\n</div>\n<h3 style="margin-top:16px">비교 단위 선택</h3>\n<div class="toolbar"><div class="field"><select id="unit31"><option value="FIRST" selected>코인·날짜별 첫 하락 관찰 (기본·중복 완화)</option><option value="ALL">하루 중 전체 하락 관찰점 (참고용)</option></select></div></div>\n<p class="muted">같은 코인의 여러 관찰점은 독립 표본이 아니야. 날짜별 분리 결과와 <b>코인·날짜별 첫 관찰</b>을 우선 판단해. 다른 날짜에 등장하는 같은 코인 역시 완전히 독립은 아니야.</p>\n<h3 style="margin-top:16px">날짜별 A / B / C · 60분 후 결과</h3>\n<div class="table-scroll"><table><thead><tr><th>날짜</th><th>검사 완료</th><th>연속봉 가능</th><th>하락 종목</th><th>A 상승 유지</th><th>B 반등 후 반납</th><th>C 반등 부족</th><th>미확정</th><th>API 실패</th></tr></thead><tbody id="daily31"><tr><td colspan="9">검사 전</td></tr></tbody></table></div>\n<div class="note">A: 60분 종가 기준 순수익 +0.5% 이상. B: 60분 종가 기준 A 미달이지만 중간 고가에서 이론적 순수익 +0.5% 이상 관측. C: 중간 고가에서도 +0.5% 미달. <b>B의 고가는 체결 가능한 확정 수익이 아냐.</b> 결과 불명은 A/B/C에서 제외해.</div>\n<h3 style="margin-top:16px">A / B / C 하락 당시 특징 중앙값</h3>\n<div class="table-scroll"><table><thead><tr><th>진입 전 특징</th><th>A 상승 유지</th><th>B 반등 반납</th><th>C 반등 부족</th><th>유효 A/B/C</th><th>A&gt;C 날짜*</th></tr></thead><tbody id="factors31"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<p class="muted">* 하루에 A 2건 이상·C 4건 이상이면서 지표값이 있는 날짜만 비교한 방향성 요약. <b>통계적 유의성이나 매수 신호를 의미하지 않아.</b> 원자료는 CSV에서 확인할 수 있어.</p>\n<h3 style="margin-top:16px">최근 3개 캔들 조합 (빈도순)</h3>\n<div class="table-scroll"><table><thead><tr><th>3봉 모양</th><th>전체</th><th>A</th><th>B</th><th>C</th><th>A 비율</th><th>발생 날짜</th></tr></thead><tbody id="patterns31"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<p class="muted">U=양봉, D=음봉, F=보합. 성공률만으로 정렬하지 않고 <b>발생 빈도순</b>으로 보여줘. 3봉 패턴이 같아도 캔들 몸통·꼬리·크기는 다를 수 있어.</p>\n<details><summary>대표 사례 펼치기 (A·B·C 각 최대 10건)</summary><div class="table-scroll"><table><thead><tr><th>날짜·코인·시각</th><th>그룹</th><th>60분 순익</th><th>몸통 %</th><th>종가 위치 %</th><th>3봉 모양</th></tr></thead><tbody id="details31"><tr><td colspan="6">검사 전</td></tr></tbody></table></div></details>\n<p class="muted"><b>고정된 하락 모집 기준:</b> 15분 간격 완료봉에서 EMA5&lt;EMA10&lt;EMA20, EMA10 하락, 종가&lt;EMA10, 직전 30분 하락. 같은 코인에서 관찰점은 최소 60분 간격. CORE.31과 비교할 수 있도록 정의를 그대로 유지해.</p>\n<p class="muted"><b>데이터 주의:</b> 연속 5분봉 60개가 확보되지 않으면 하락 판정을 내리지 않아. API 실패가 0이어도 희소 종목은 분석 불가할 수 있어. 현재 상장된 KRW 코인만 포함하고, 현재 거래대금 순위는 과거 순위가 아니야. 수익률은 다음 봉 시가 진입, 60분 종가 청산, 편도 수수료 0.25%, 슬리피지 0% 기본 가정이야. 코인 상관관계·시차 중복·표본 선택 편향을 고려해야 해.</p>\n</section>\n</main>\n<script>\n\'use strict\';\nconst $=id=>document.getElementById(id);\nconst esc=s=>String(s??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nconst fmt=n=>n==null||!Number.isFinite(Number(n))?\'—\':(Number(n)>0?\'+\':\'\')+Number(n).toFixed(3)+\'%\';\nconst avg=a=>a.length?a.reduce((p,c)=>p+Number(c),0)/a.length:NaN;\nconst isReady=o=>o&&o.status===\'RESOLVED\'&&o.net_pct!=null&&Number.isFinite(Number(o.net_pct));\nconst be=e=>({status:e.postscan_be_status,net_pct:e.postscan_be_net_pct});\nconst rules=[[\'BE\',\'기존 30/30/40 BE\',be],[\'T15\',\'15분 종가\',e=>e.fixed?.[\'15\']],[\'T30\',\'30분 종가\',e=>e.fixed?.[\'30\']],[\'T60\',\'60분 종가\',e=>e.fixed?.[\'60\']],[\'TRAIL\',\'첫 익절 후 잔량 1% 추적\',e=>e.trailing]];\nlet state={running:false,pause:false,key:\'\',markets:[],cursor:0,done:0,results:[],errors:[],btc:null,btcError:\'\',start:0,date:\'\',fee:0.25,slip:0};\n$(\'day\').value=new Intl.DateTimeFormat(\'sv-SE\',{timeZone:\'Asia/Seoul\',year:\'numeric\',month:\'2-digit\',day:\'2-digit\'}).format(new Date(Date.now()-86400000));\nfunction btcOf(e){const stamp=e.search_time_kst||\'\';if(stamp.length<16||!state.btc)return null;\n const h=Number(stamp.slice(11,13)),m=Number(stamp.slice(14,16));let t=h*60+m-5;\n if(!Number.isInteger(t)||t<0)return null;\n const key=stamp.slice(0,10)+\' \'+String(Math.floor(t/60)).padStart(2,\'0\')+\':\'+String(t%60).padStart(2,\'0\');\n return state.btc[key]??null;}\nfunction gate23(e){const b=btcOf(e);if(e.local_guard===\'LOCAL_REJECT\'||b?.btc_regime===\'BTC_DOWN\')return \'REJECT\';\n if(e.local_guard===\'LOCAL_UNKNOWN\'||!b||b.btc_regime===\'UNKNOWN\')return \'UNKNOWN\';\n if(e.local_guard===\'LOCAL_STRONG\'&&[\'BTC_UP\',\'BTC_SIDE\'].includes(b.btc_regime))return \'PASS\';return \'WAIT\';}\nfunction gate24(e){const b=btcOf(e),local=e.quality?.local_gate;if(local===\'UNKNOWN\'||!b||b.btc_regime===\'UNKNOWN\')return \'UNKNOWN\';\n if(b.btc_regime===\'BTC_DOWN\')return \'REJECT\';if(local===\'PASS\'&&[\'BTC_UP\',\'BTC_SIDE\'].includes(b.btc_regime))return \'PASS\';return \'WAIT\';}\nfunction gate26(e){\n // Research-only, only 10-minute PRIMARY snapshot. Completed data at search T.\n if(e.delay_minutes!==10)return \'NOT_TESTED\';\n const btc=btcOf(e),vol=e.quality?.volume_ratio_4v12,move=e.price_change_pct;\n if(e.review_status!==\'REVIEWED\'||!btc||btc.btc_regime===\'UNKNOWN\'||\n    vol==null||move==null||!Number.isFinite(Number(vol))||!Number.isFinite(Number(move)))return \'UNKNOWN\';\n return btc.btc_regime===\'BTC_UP\' && Number(move)>0 && Number(vol)>=1.0 ? \'PASS\':\'WAIT\';\n}\nfunction gate27(e,variant){\n if(e.delay_minutes!==10)return \'NOT_TESTED\';\n const z=e.core27;\n const combo=variant.startsWith(\'C\');\n if(combo){const triple=gate26(e);if(triple===\'UNKNOWN\')return \'UNKNOWN\';if(triple!==\'PASS\')return \'WAIT\';}\n if(!z||z.status!==\'READY\')return \'UNKNOWN\';\n const hl=z.higher_lows===true,br=z.high_break===true;\n const id=combo?variant.slice(1):variant;\n return (id===\'HL\'?hl:id===\'BR\'?br:id===\'HBR\'||id===\'BOTH\'?hl&&br:false)?\'PASS\':\'WAIT\';\n}\nconst variants27=[[\'HL\',\'저점 상승 단독\'],[\'BR\',\'고점 돌파 단독\'],[\'HBR\',\'저점+고점 모두\'],\n [\'CHL\',\'3중 확인 + 저점 상승\'],[\'CBR\',\'3중 확인 + 고점 돌파\'],[\'CBOTH\',\'3중 확인 + 두 조건\']];\nfunction allRows(){return state.results.flatMap(r=>(r.core25_focus?.events||[]).map(e=>{\n const row={...e,date:r.date,old_gate:gate23(e),new_gate:gate24(e),shadow26:gate26(e)};\n for(const [k] of variants27)row[\'s27_\'+k]=gate27(e,k);\n return row;\n }));}\nfunction filtered(rows,cohort){return cohort===\'ALL\'?rows:rows.filter(e=>(cohort===\'A\'&&e.old_gate===\'PASS\')||(cohort===\'B\'&&e.new_gate===\'PASS\')||(cohort===\'BOTH\'&&e.old_gate===\'PASS\'&&e.new_gate===\'PASS\')||(cohort===\'C\'&&e.shadow26===\'PASS\')||variants27.some(([k])=>cohort===k&&e[\'s27_\'+k]===\'PASS\'));}\nfunction paint(){\n $(\'done\').textContent=`${state.done}/${state.markets.length||0}`;\n $(\'signals\').textContent=state.results.reduce((a,r)=>a+(r.core25_focus?.coverage?.signals||0),0);\n $(\'failures\').textContent=state.errors.length;\n $(\'elapsed\').textContent=state.start?((performance.now()-state.start)/1000).toFixed(1)+\'초\':\'—\';\n $(\'progress\').max=Math.max(1,state.markets.length);$(\'progress\').value=state.done;\n const rows=allRows();\n $(\'delay-summary\').innerHTML=[10,20,30].map(d=>{const g=rows.filter(e=>e.delay_minutes===d),done=g.filter(e=>isReady(be(e)));return `<tr><td>${d}분</td><td>${g.length}</td><td>${g.filter(e=>e.old_gate===\'PASS\').length}</td><td>${g.filter(e=>e.new_gate===\'PASS\').length}</td><td>${done.length}</td><td>${fmt(avg(done.map(e=>e.postscan_be_net_pct)))}</td></tr>`;}).join(\'\')||\'<tr><td colspan="6">신호 없음</td></tr>\';\n const delay=Number($(\'delay\').value),cohort=$(\'cohort\').value,group=rows.filter(e=>e.delay_minutes===delay),selected=filtered(group,cohort);\n $(\'quality-body\').innerHTML=[[\'전체\',()=>true],[\'기존 CORE.23\',e=>e.old_gate===\'PASS\'],[\'신규 CORE.24\',e=>e.new_gate===\'PASS\'],[\'두 필터\',e=>e.old_gate===\'PASS\'&&e.new_gate===\'PASS\'],[\'CORE.26 3중 확인 (10분 전용)\',e=>e.shadow26===\'PASS\']].map(([name,pred])=>{const x=group.filter(pred),r=x.filter(e=>isReady(be(e)));return `<tr><td>${name}</td><td>${x.length}</td><td>${r.length}</td><td>${r.filter(e=>Number(e.postscan_be_net_pct)>0).length}</td><td>${fmt(avg(r.map(e=>e.postscan_be_net_pct)))}</td></tr>`;}).join(\'\');\n // The 10-minute reference population is fixed for every CORE.27 comparison.\n const ten=rows.filter(e=>e.delay_minutes===10),readyProfits=ten.filter(e=>isReady(be(e))&&Number(e.postscan_be_net_pct)>0);\n const stats27=[[\'ALL\',\'10분 전체 원본\',e=>true],[\'C\',\'기존 3중 확인\',e=>e.shadow26===\'PASS\'],\n ...variants27.map(([k,label])=>[k,label,e=>e[\'s27_\'+k]===\'PASS\'])];\n $(\'structure27-body\').innerHTML=stats27.map(([key,label,pred])=>{\n const passed=ten.filter(pred),settled=passed.filter(e=>isReady(be(e))),wins=settled.filter(e=>Number(e.postscan_be_net_pct)>0);\n const missed=readyProfits.filter(e=>!pred(e)).length;\n const missing=key===\'ALL\'?ten.filter(e=>e.core27?.status!==\'READY\').length:key===\'C\'?ten.filter(e=>e.shadow26===\'UNKNOWN\').length:ten.filter(e=>e[\'s27_\'+key]===\'UNKNOWN\').length;\n return `<tr><td>${esc(label)}</td><td>${passed.length}</td><td>${settled.length}</td><td>${wins.length}</td><td>${fmt(avg(settled.map(e=>e.postscan_be_net_pct)))}</td><td>${missed}</td><td>${missing}</td></tr>`;\n }).join(\'\')||\'<tr><td colspan="7">신호 없음</td></tr>\';\n const matched=selected.filter(e=>rules.every(([, ,ex])=>isReady(ex(e))));const base=avg(matched.map(e=>e.postscan_be_net_pct));\n $(\'selected-note\').textContent=`${delay}분 · 선택 ${selected.length}건 · 전체 원본 신호 ${group.length}건`;\n $(\'exit-body\').innerHTML=rules.map(([,label,ex])=>{const r=selected.map(ex).filter(isReady),shared=avg(matched.map(e=>ex(e).net_pct));return `<tr><td>${label}</td><td>${r.length}/${selected.length}</td><td>${r.filter(o=>Number(o.net_pct)>0).length}</td><td>${fmt(avg(r.map(o=>o.net_pct)))}</td><td>${fmt(shared)}</td><td>${fmt(Number.isFinite(base)&&Number.isFinite(shared)?shared-base:null)}</td></tr>`;}).join(\'\');\n $(\'matched-note\').textContent=`5가지 청산 방식 모두 결과가 확정된 동일 표본 ${matched.length}건. 공통 표본 기준에서만 전략 차이를 비교해. 미확정 거래를 수익이나 손실로 간주하지 않아.`;\n const cats=[\'BE_PROFIT\',\'GIVEBACK_OPPORTUNITY\',\'NO_POSITIVE_MFE\',\'SMALL_OPPORTUNITY\',\'UNDETERMINED\'];\n $(\'diagnosis-body\').innerHTML=\'<tr>\'+cats.map(k=>`<td>${selected.filter(e=>e.failure_category===k).length}</td>`).join(\'\')+\'</tr>\';\n $(\'details-body\').innerHTML=selected.slice(0,80).map(e=>{const m=e.path?.m60,tri=e.trailing,tm=e.fixed?.[\'30\'];return `<tr><td>${esc(e.search_time_kst)}</td><td>${esc(e.market)}</td><td>${esc(e.state)}</td><td>${e.old_gate}</td><td>${e.new_gate}</td><td>${isReady(be(e))?fmt(e.postscan_be_net_pct):\'—\'}</td><td>${isReady(tri)?fmt(tri.net_pct):\'—\'}</td><td>${isReady(tm)?fmt(tm.net_pct):\'—\'}</td><td>${m?.status===\'READY\'?fmt(m.mfe_gross_pct)+\' / \'+fmt(m.mae_gross_pct):\'—\'}</td></tr>`;}).join(\'\')||\'<tr><td colspan="9">종목 없음</td></tr>\';\n}\n$(\'delay\').onchange=()=>{if([\'C\',...variants27.map(([k])=>k)].includes($(\'cohort\').value)&&Number($(\'delay\').value)!==10)$(\'cohort\').value=\'ALL\';paint();};\n$(\'cohort\').onchange=()=>{if([\'C\',...variants27.map(([k])=>k)].includes($(\'cohort\').value))$(\'delay\').value=\'10\';paint();};\nasync function api(url,payload){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),55000);try{const r=await fetch(url,{method:payload?\'POST\':\'GET\',headers:{\'Content-Type\':\'application/json\'},body:payload?JSON.stringify(payload):undefined,cache:\'no-store\',signal:controller.signal});const j=await r.json();if(!r.ok||!j.ok)throw Error(j.error||`HTTP ${r.status}`);return j;}finally{clearTimeout(timer)}}\n$(\'pause\').onclick=()=>{state.pause=true;$(\'pause\').disabled=true;};\n$(\'run\').onclick=async()=>{\n if(state.running)return;\n const date=$(\'day\').value,fee=Number($(\'fee\').value),slip=Number($(\'slip\').value),scope=$(\'scope\').value;\n if(!date||!Number.isFinite(fee)||fee<0||fee>0.5||!Number.isFinite(slip)||slip<0||slip>0.5){alert(\'날짜와 편도 비용을 확인해.\');return;}\n const key=[date,scope,fee,slip].join(\'|\');const resume=state.key===key&&state.markets.length&&state.cursor<state.markets.length;\n if(!resume)state={running:false,pause:false,key,markets:[],cursor:0,done:0,results:[],errors:[],btc:null,btcError:\'\',start:performance.now(),date,fee,slip};\n state.running=true;state.pause=false;$(\'run\').disabled=true;$(\'pause\').disabled=false;$(\'csv\').disabled=true;\n try{\n  if(!resume){$(\'status\').textContent=\'코인 목록 조회 중...\';const u=await api(\'/api/v2core/universe\');state.markets=u.items.slice(0,scope===\'all\'?u.items.length:Number(scope));\n   try{const b=await api(\'/api/v2core/btc-context\',{date});state.btc=b.context;}catch(e){state.btcError=String(e.message||e);state.btc=null;}\n  }\n  paint();const total=state.markets.length;\n  async function work(){while(!state.pause&&state.cursor<total){const market=state.markets[state.cursor++];try{const a=await api(\'/api/v2core/evaluate\',{market,date,fee,slip});state.results.push(a.result);}catch(e){state.errors.push({market,error:String(e.message||e)});}finally{state.done++;if(state.done%10===0||state.done===total){paint();$(\'status\').textContent=`${state.done}/${total} 검사 · 오류 ${state.errors.length} · BTC ${state.btc?\'확인\':\'미확인\'}`;}}}}\n  await Promise.all(Array.from({length:Math.min(4,total)},()=>work()));paint();const finished=state.cursor===total&&state.done===total;\n  $(\'status\').textContent=(state.pause?\'일시 중지 · 같은 조건으로 검사 시작을 다시 누르면 이어서 진행\':finished?\'검사 완료\':\'검사 중단\')+` · ${state.done}/${total} · API 오류 ${state.errors.length}`+(state.btcError?\' · BTC 미확인: \'+state.btcError:\'\');\n  $(\'csv\').disabled=state.results.length===0;\n }catch(e){$(\'status\').textContent=\'⚠️ \'+String(e.message||e);}finally{state.running=false;$(\'run\').disabled=false;$(\'pause\').disabled=true;paint();}\n};\nfunction csvCell(s){const text=String(s??\'\');return /[",\\n\\r]/.test(text)?\'"\'+text.replace(/"/g,\'""\')+\'"\':text;}\n$(\'csv\').onclick=()=>{\n const cols=[\'version\',\'date\',\'market\',\'base_id\',\'signal_time_kst\',\'search_time_kst\',\'delay_minutes\',\'entry_time_kst\',\'entry_price\',\n \'core23_final_gate\',\'core24_final_gate\',\'trend_state\',\'review_status\',\'liquidity_tier\',\'price_change_pct\',\'core24_local_gate\',\'core24_quality_reason\',\n \'original_be_status\',\'original_be_net_pct\',\'core24_be_status\',\'core24_be_net_pct\',\'core24_time15_status\',\'core24_time15_net_pct\',\n \'core24_time30_status\',\'core24_time30_net_pct\',\'core24_time60_status\',\'core24_time60_net_pct\',\'core24_trail_status\',\'core24_trail_net_pct\',\n \'core24_trail_tp1_filled\',\'core24_trail_tp1_min\',\'core24_trail_stop_min\',\'core24_trail_ambiguous_bar\',\'core24_mfe15_gross_pct\',\n \'core24_mae15_gross_pct\',\'core24_mfe30_gross_pct\',\'core24_mae30_gross_pct\',\'core24_mfe60_gross_pct\',\'core24_mae60_gross_pct\',\n \'core24_mfe60_net_upper_pct\',\'core24_mfe60_first_min\',\'core24_mae60_first_min\',\'failure_category\',\'btc_regime\',\'btc_ret60_pct\',\n \'quality_volume_ratio_4v12\',\'quality_price_structure\',\'fee_pct_side\',\'slip_pct_side\',\'scan_complete\',\'scan_errors\',\'core26_shadow_gate\',\n \'core27_structure_status\',\'core27_structure_reason\',\'core27_last_complete_kst\',\'core27_higher_lows\',\'core27_high_break\',\'core27_previous_3_high\',\n \'core27_breakout_margin_pct\',\'core27_higher_low_step_pct\',\n \'core27_gate_HL\',\'core27_gate_BR\',\'core27_gate_HBR\',\'core27_gate_CHL\',\'core27_gate_CBR\',\'core27_gate_CBOTH\'];\n const total=state.markets.length,complete=state.done===total&&state.cursor===total,all=allRows();\n const records=all.map(e=>{const btc=btcOf(e);return {\n version:\'V2.CORE.31-MARKET-REVERSAL\',date:e.date,market:e.market,base_id:e.base_id,signal_time_kst:e.signal_time_kst,search_time_kst:e.search_time_kst,\n delay_minutes:e.delay_minutes,entry_time_kst:e.entry_time_kst,entry_price:e.entry_price,core23_final_gate:e.old_gate,core24_final_gate:e.new_gate,\n trend_state:e.state,review_status:e.review_status,liquidity_tier:e.liquidity_tier,price_change_pct:e.price_change_pct,\n core24_local_gate:e.quality?.local_gate,core24_quality_reason:e.quality?.reason,original_be_status:e.original_be_status,original_be_net_pct:e.original_be_net_pct,\n core24_be_status:e.postscan_be_status,core24_be_net_pct:e.postscan_be_net_pct,\n core24_time15_status:e.fixed?.[\'15\']?.status,core24_time15_net_pct:e.fixed?.[\'15\']?.net_pct,\n core24_time30_status:e.fixed?.[\'30\']?.status,core24_time30_net_pct:e.fixed?.[\'30\']?.net_pct,\n core24_time60_status:e.fixed?.[\'60\']?.status,core24_time60_net_pct:e.fixed?.[\'60\']?.net_pct,\n core24_trail_status:e.trailing?.status,core24_trail_net_pct:e.trailing?.net_pct,core24_trail_tp1_filled:e.trailing?.tp1_filled,\n core24_trail_tp1_min:e.trailing?.tp1_min,core24_trail_stop_min:e.trailing?.trail_min,core24_trail_ambiguous_bar:e.trailing?.ambiguous_same_bar,\n core24_mfe15_gross_pct:e.path?.m15?.mfe_gross_pct,core24_mae15_gross_pct:e.path?.m15?.mae_gross_pct,\n core24_mfe30_gross_pct:e.path?.m30?.mfe_gross_pct,core24_mae30_gross_pct:e.path?.m30?.mae_gross_pct,\n core24_mfe60_gross_pct:e.path?.m60?.mfe_gross_pct,core24_mae60_gross_pct:e.path?.m60?.mae_gross_pct,\n core24_mfe60_net_upper_pct:e.path?.m60?.mfe_net_upper_pct,core24_mfe60_first_min:e.path?.m60?.mfe_first_min,\n core24_mae60_first_min:e.path?.m60?.mae_first_min,failure_category:e.failure_category,btc_regime:btc?.btc_regime??\'UNKNOWN\',\n btc_ret60_pct:btc?.btc_ret60_pct,quality_volume_ratio_4v12:e.quality?.volume_ratio_4v12,\n quality_price_structure:e.quality?.has_price_structure,fee_pct_side:state.fee,slip_pct_side:state.slip,scan_complete:complete,scan_errors:state.errors.length,core26_shadow_gate:e.shadow26,\n core27_structure_status:e.core27?.status,core27_structure_reason:e.core27?.reason,core27_last_complete_kst:e.core27?.last_complete_kst,\n core27_higher_lows:e.core27?.higher_lows,core27_high_break:e.core27?.high_break,\n core27_previous_3_high:e.core27?.previous_3_high,core27_breakout_margin_pct:e.core27?.breakout_margin_pct,\n core27_higher_low_step_pct:e.core27?.higher_low_step_pct,\n core27_gate_HL:e.s27_HL,core27_gate_BR:e.s27_BR,core27_gate_HBR:e.s27_HBR,\n core27_gate_CHL:e.s27_CHL,core27_gate_CBR:e.s27_CBR,core27_gate_CBOTH:e.s27_CBOTH};});\n const lines=[cols.join(\',\'),...records.map(e=>cols.map(k=>csvCell(e[k])).join(\',\'))];const blob=new Blob([\'\\ufeff\'+lines.join(\'\\n\')],{type:\'text/csv;charset=utf-8\'}),url=URL.createObjectURL(blob),a=document.createElement(\'a\');\n a.href=url;a.download=\'bithumb_V2_CORE31_STANDARD_SCAN_10M_BE.csv\';a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);\n};\n\n// CORE.31: multi-date cross-sectional reverse study, one-file Render workflow.\nlet c31={running:false,pause:false,key:\'\',days:[],markets:[],tasks:[],cursor:0,done:0,\n results:[],errors:[],btcErrors:{},btcPrepared:{},scope:\'all\',fee:.25,slip:0};\nconst cf31=(x,d=2)=>x==null||!Number.isFinite(Number(x))?\'—\':Number(x).toFixed(d);\nfunction shift31(s,n){const d=new Date(s+\'T00:00:00Z\');d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10);}\nfunction todayK31(){return new Intl.DateTimeFormat(\'sv-SE\',{timeZone:\'Asia/Seoul\',year:\'numeric\',month:\'2-digit\',day:\'2-digit\'}).format(new Date());}\nfunction cutoff31(){return shift31(todayK31(),-1);}\n$(\'end31\').value=shift31(todayK31(),-2); // one day before discovery date by default\n$(\'end31\').max=cutoff31();\nfunction group31(e){\n const p=e.posthoc||{};\n if(p.future_status!==\'READY\'||p.post_60m_net_pct==null)return \'U\';\n if(Number(p.post_60m_net_pct)>=.5)return \'A\';\n if(p.post_net_high_upper_bound_pct!=null&&Number(p.post_net_high_upper_bound_pct)>=.5)return \'B\';\n return \'C\';\n}\nfunction all31(){\n const out=[];\n for(const r of c31.results){for(let i=0;i<(r.events||[]).length;i++){\n   const e=r.events[i];out.push({...e,first_observation_of_market_day:i===0,group_abc:group31(e)});\n }}\n return out;\n}\nfunction selected31(){const all=all31();return $(\'unit31\').value===\'FIRST\'?all.filter(e=>e.first_observation_of_market_day):all;}\nfunction med31(list){const a=list.map(Number).filter(Number.isFinite).sort((a,b)=>a-b);return {count:a.length,v:a.length?(a.length%2?a[(a.length-1)/2]:(a[a.length/2-1]+a[a.length/2])/2):null};}\nfunction daily31(events){\n return c31.days.map(day=>{\n  const checked=c31.results.filter(r=>r.date===day),bad=c31.errors.filter(r=>r.date===day),dayAll=all31().filter(e=>e.date===day),dayEv=events.filter(e=>e.date===day);\n  const n=g=>dayEv.filter(e=>e.group_abc===g).length;\n  return {date:day,checked:checked.length+bad.length,eligible_markets:checked.filter(r=>Number(r.coverage?.grid_checked||0)>Number(r.coverage?.grid_missing_prehistory||0)).length,unique_down_markets:new Set(dayAll.map(e=>e.market)).size,\n          all_snapshots:dayAll.length,first_snapshots:dayEv.filter(e=>e.first_observation_of_market_day).length,\n          A:n(\'A\'),B:n(\'B\'),C:n(\'C\'),U:n(\'U\'),error_count:bad.length};\n });\n}\nconst fields31=[\n [\'마지막 봉 몸통 / 폭\',\'last_candle_body_ratio\',100,\'%\'],\n [\'마지막 봉 아래꼬리 / 폭\',\'last_candle_lower_wick_ratio\',100,\'%\'],\n [\'마지막 봉 위꼬리 / 폭\',\'last_candle_upper_wick_ratio\',100,\'%\'],\n [\'마지막 봉 종가 위치\',\'last_candle_close_position\',100,\'%\'],\n [\'마지막 봉 크기 / ATR\',\'last_candle_range_atr\',1,\'배\'],\n [\'최근 3개 봉 몸통 / ATR\',\'last3_body_atr_avg\',1,\'배\'],\n [\'최근 3개 봉 아래꼬리 비율\',\'last3_lower_wick_avg\',100,\'%\'],\n [\'최근 3봉 양봉 개수\',\'last3_bullish_count\',1,\'개\'],\n [\'직전 30분 하락률\',\'prior_30m_return_pct\',1,\'%\'],\n [\'직전 60분 하락률\',\'prior_60m_return_pct\',1,\'%\'],\n [\'EMA5 기울기 (15분)\',\'ema5_slope_15m_pct\',1,\'%\'],\n [\'EMA5-10 간격 축소\',\'ema_gap_narrow_15m_pct\',1,\'%p\'],\n [\'거래량 최근4 / 이전12\',\'volume_ratio_4_vs_12\',1,\'배\'],\n [\'RSI14\',\'rsi14\',1,\'\'],\n [\'ATR14 / 가격\',\'atr14_pct\',1,\'%\'],\n [\'이전 상승파동 되돌림\',\'fib_up_retrace_pct\',1,\'%\'],\n [\'하락파동 종가 회복률\',\'fib_down_recover_pct\',1,\'%\'],\n [\'BTC 60분 변화율\',\'btc_60m_return_pct\',1,\'%\']\n];\nfunction render31(){\n const all=all31(), ev=selected31(), eligible=ev.filter(e=>e.group_abc!==\'U\');\n const n=g=>ev.filter(e=>e.group_abc===g).length;\n $(\'k31done\').textContent=c31.done+\'/\'+(c31.tasks.length||0);\n $(\'k31cases\').textContent=all.length;\n $(\'k31eligible\').textContent=c31.results.filter(r=>Number(r.coverage?.grid_checked||0)>Number(r.coverage?.grid_missing_prehistory||0)).length;\n $(\'k31errs\').textContent=c31.errors.length;\n $(\'progress31\').max=c31.tasks.length||1;$(\'progress31\').value=c31.done;\n $(\'daily31\').innerHTML=daily31(ev).map(d=>`<tr><td>${esc(d.date)}</td><td>${d.checked}/${c31.markets.length}</td><td>${d.eligible_markets}</td><td>${d.unique_down_markets}</td><td class="good">${d.A}</td><td>${d.B}</td><td class="bad">${d.C}</td><td>${d.U}</td><td>${d.error_count}</td></tr>`).join(\'\')||\'<tr><td colspan="9">검사 전</td></tr>\';\n $(\'factors31\').innerHTML=fields31.map(([label,key,scale,unit])=>{\n  const z={};for(const g of [\'A\',\'B\',\'C\'])z[g]=med31(eligible.filter(e=>e.group_abc===g).map(e=>e.features?.[key]).filter(v=>v!=null));\n  let compared=0,above=0;\n  for(const day of c31.days){\n   const a=med31(eligible.filter(e=>e.date===day&&e.group_abc===\'A\').map(e=>e.features?.[key]).filter(v=>v!=null));\n   const c=med31(eligible.filter(e=>e.date===day&&e.group_abc===\'C\').map(e=>e.features?.[key]).filter(v=>v!=null));\n   if(a.count>=2&&c.count>=4){compared++;if(a.v>c.v)above++;}\n  }\n  return `<tr><td>${label}</td><td>${cf31(z.A.v==null?null:z.A.v*scale)}${unit}</td><td>${cf31(z.B.v==null?null:z.B.v*scale)}${unit}</td><td>${cf31(z.C.v==null?null:z.C.v*scale)}${unit}</td><td>${z.A.count}/${z.B.count}/${z.C.count}</td><td>${compared?above+\'/\'+compared:\'—\'}</td></tr>`;\n }).join(\'\');\n const pats={};for(const e of eligible){const key=e.features?.last3_body_pattern||\'UNKNOWN\';\n  if(!pats[key])pats[key]={A:0,B:0,C:0,dates:new Set()};pats[key][e.group_abc]++;pats[key].dates.add(e.date);\n }\n $(\'patterns31\').innerHTML=Object.entries(pats).sort((a,b)=>(b[1].A+b[1].B+b[1].C)-(a[1].A+a[1].B+a[1].C)||a[0].localeCompare(b[0])).slice(0,12).map(([k,p])=>{\n   const total=p.A+p.B+p.C;return `<tr><td>${esc(k)}</td><td>${total}</td><td>${p.A}</td><td>${p.B}</td><td>${p.C}</td><td>${cf31(100*p.A/total,1)}%</td><td>${p.dates.size}</td></tr>`;\n }).join(\'\')||\'<tr><td colspan="7">확정된 사례 없음</td></tr>\';\n const picks=[\'A\',\'B\',\'C\'].flatMap(g=>eligible.filter(e=>e.group_abc===g).sort((a,b)=>a.date.localeCompare(b.date)||a.market.localeCompare(b.market)||a.asof_kst.localeCompare(b.asof_kst)).slice(0,10));\n $(\'details31\').innerHTML=picks.map(e=>`<tr><td>${esc(e.date.slice(5)+\' \'+e.market+\' \'+e.asof_kst.slice(11))}</td><td>${e.group_abc}</td><td>${cf31(e.posthoc?.post_60m_net_pct,3)}%</td><td>${cf31(e.features?.last_candle_body_ratio==null?null:e.features.last_candle_body_ratio*100)}%</td><td>${cf31(e.features?.last_candle_close_position==null?null:e.features.last_candle_close_position*100)}%</td><td>${esc(e.features?.last3_body_pattern)}</td></tr>`).join(\'\')||\'<tr><td colspan="6">확정 사례 없음</td></tr>\';\n}\nfunction status31(prefix=\'\'){\n const btc=Object.keys(c31.btcErrors).length;\n $(\'status31\').textContent=prefix+`${c31.done}/${c31.tasks.length} 코인×날짜 · 전체 하락 관찰 ${all31().length}건 · API 실패 ${c31.errors.length} · BTC 미확인 ${btc}일`;\n}\n$(\'unit31\').onchange=render31;\n$(\'pause31\').onclick=()=>{c31.pause=true;$(\'pause31\').disabled=true;};\n$(\'run31\').onclick=async()=>{\n if(c31.running)return;\n const end=$(\'end31\').value,count=Number($(\'days31\').value),scope=$(\'scope31\').value,fee=Number($(\'fee31\').value),slip=Number($(\'slip31\').value);\n if(!/^\\d{4}-\\d{2}-\\d{2}$/.test(end)||!end||end>cutoff31()||![1,3,5,7].includes(count)||![\'40\',\'80\',\'all\'].includes(scope)||\n  !Number.isFinite(fee)||fee<0||fee>.5||!Number.isFinite(slip)||slip<0||slip>.5){alert(\'연구 날짜·범위·비용을 확인해.\');return;}\n const days=Array.from({length:count},(_,i)=>shift31(end,i-count+1));\n if(days[0]<shift31(todayK31(),-60)){alert(\'최근 60일 내 종료된 날짜만 검사할 수 있어.\');return;}\n const key=[end,count,scope,fee,slip].join(\'|\');\n const resume=c31.key===key&&c31.tasks.length>0&&c31.cursor<c31.tasks.length;\n if(!resume)c31={running:false,pause:false,key,days,markets:[],tasks:[],cursor:0,done:0,results:[],errors:[],btcErrors:{},btcPrepared:{},scope,fee,slip};\n c31.running=true;c31.pause=false;$(\'run31\').disabled=true;$(\'pause31\').disabled=false;$(\'csv31\').disabled=true;\n for(const id of [\'end31\',\'days31\',\'scope31\',\'fee31\',\'slip31\'])$(id).disabled=true;\n try{\n  if(!resume){status31(\'전체 KRW 종목 목록 조회 중 · \');const u=await api(\'/api/v2core/universe\');\n   c31.markets=u.items.slice(0,scope===\'all\'?u.items.length:Number(scope));\n   if(!c31.markets.length)throw Error(\'조회할 KRW 종목이 없어.\');\n   c31.tasks=days.flatMap(date=>c31.markets.map(market=>({date,market})));\n  }\n  // Prefetch date-scoped BTC reference ONCE per date (not per market).\n  for(const date of c31.days){if(c31.pause)break;if(c31.btcPrepared[date])continue;\n    status31(date+\' BTC 5분봉 기준 확인 중 · \');\n    try{await api(\'/api/v2core/reverse31/btc-context\',{date});}\n    catch(e){c31.btcErrors[date]=String(e.message||e);}\n    c31.btcPrepared[date]=true;\n  }\n  const total=c31.tasks.length;\n  async function worker(){while(!c31.pause&&c31.cursor<total){const job=c31.tasks[c31.cursor++];\n    try{const r=await api(\'/api/v2core/reverse31/analyze\',{market:job.market,date:job.date,fee,slip});c31.results.push(r.result);}\n    catch(e){c31.errors.push({...job,error:String(e.message||e)});}\n    finally{c31.done++;if(c31.done%12===0||c31.done===total){render31();status31(job.date+\' 조회 중 · \');$(\'csv31\').disabled=false;}}\n  }}\n  if(!c31.pause)await Promise.all(Array.from({length:Math.min(4,total)},()=>worker()));\n  status31(c31.pause?\'일시 중지 · 같은 조건으로 다시 실행하면 재개 · \':c31.done===total?\'전체 검사 완료 · \':\'검사 미완료 · \');\n }catch(e){status31(\'⚠️ \'+String(e.message||e)+\' · \');}\n finally{c31.running=false;$(\'run31\').disabled=false;$(\'pause31\').disabled=true;\n  for(const id of [\'end31\',\'days31\',\'scope31\',\'fee31\',\'slip31\'])$(id).disabled=false;\n  $(\'csv31\').disabled=c31.results.length===0&&c31.errors.length===0;render31();}\n};\n$(\'csv31\').onclick=()=>{\n const ev=all31(),stats=daily31(ev.filter(e=>e.first_observation_of_market_day));\n const complete=c31.tasks.length>0&&c31.done===c31.tasks.length&&c31.cursor===c31.tasks.length;\n const base={version:\'V2.CORE.31-MULTIDAY-REVERSAL\',dataset:\'CORE31_EXANTE_FULL_KRW_DOWN_MULTIDAY\',\n  date_from:c31.days[0],date_to:c31.days[c31.days.length-1],requested_days:c31.days.length,\n  scope:c31.scope,fee_pct_side:c31.fee,slip_pct_side:c31.slip,\n  scan_total_tasks:c31.tasks.length,scan_done_tasks:c31.done,scan_api_errors:c31.errors.length,scan_complete:complete,\n  feature_policy:\'CLOSED_5M_ONLY\',outcome_policy:\'NEXT_OPEN_TO_60M_CLOSE_FEE_ADJUSTED\',selection_policy:\'EMA5_LT_EMA10_LT_EMA20_15M_GRID_60M_COOLDOWN\'};\n const records=[];\n for(const s of stats)records.push({...base,row_type:\'DAY_SUMMARY\',date:s.date,...s,btc_error:c31.btcErrors[s.date]||\'\'});\n for(const r of c31.results){records.push({...base,row_type:\'MARKET\',date:r.date,market:r.market,...r.coverage});\n  (r.events||[]).forEach((e,i)=>records.push({...base,row_type:\'CASE\',date:r.date,market:r.market,\n    asof_kst:e.asof_kst,sample_basis:e.sample_basis,first_observation_of_market_day:i===0,\n    group_abc:group31(e),...e.features,...e.posthoc}));\n }\n for(const err of c31.errors)records.push({...base,row_type:\'ERROR\',...err});\n if(!records.length)return;\n const cols=[...new Set([\'row_type\',\'version\',\'dataset\',\'date_from\',\'date_to\',\'date\',\'market\',\'asof_kst\',\'first_observation_of_market_day\',\'group_abc\',\'post_label\',\'post_60m_net_pct\',\'post_net_high_upper_bound_pct\',\'future_status\',\'scope\',\'scan_complete\',...records.flatMap(r=>Object.keys(r))])];\n const lines=[cols.join(\',\'),...records.map(r=>cols.map(k=>csvCell(r[k])).join(\',\'))];\n const blob=new Blob([\'\\ufeff\'+lines.join(\'\\n\')],{type:\'text/csv;charset=utf-8\'}),url=URL.createObjectURL(blob),a=document.createElement(\'a\');\n a.href=url;a.download=`CORE31_REVERSAL_COMPARE_${c31.days[0]}_${c31.days[c31.days.length-1]}_${c31.scope}_KRW.csv`;\n a.click();setTimeout(()=>URL.revokeObjectURL(url),2500);\n};\nrender31();\n\npaint();\n</script></body></html>\n'

def register_daytrade_review(app, deps):
    """ONE lab, ONE registration point. Main V2 scanner is untouched."""
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache = {'at': 0., 'markets': [], 'results': {}, 'ticker_at': 0., 'sorted': [], 'btc': {}, 'reverse31': {}, 'reverse31_btc': {}}
    lock = Lock()

    @app.get('/backtest-daytrade')
    @app.get('/v2-core-review')
    def v2_core_page():
        return render_template_string(CORE_PAGE)


    @app.get('/api/v2core/reverse28/cases')
    def v2_core_reverse28_cases():
        return jsonify(ok=True, version='V2.CORE.28-REVERSE',
                       items=R28_CASES, research_only=True)

    @app.post('/api/v2core/reverse28/analyze')
    def v2_core_reverse28_analyze():
        try:
            raw=request.get_json(silent=True)
            idx=raw.get('index') if isinstance(raw,dict) else None
            if isinstance(idx,bool) or not isinstance(idx,int) or not (0<=idx<len(R28_CASES)):
                raise ValueError('역추적 대상 번호 오류')
            case=R28_CASES[idx]
            if r28_dt(case['asof_kst']).date()>=datetime.now(kst).date():
                raise ValueError('완료되지 않은 날짜 조회 불가')
            with lock:
                previous=cache.setdefault('reverse28',{}).get(idx)
            if previous is not None and time.monotonic()-previous[0]<900:
                return jsonify(ok=True,result=previous[1],cached=True)
            result=r28_one_case(api_get,case)
            with lock:
                cache['reverse28'][idx]=(time.monotonic(),result)
            return jsonify(ok=True,result=result,cached=False)
        except Exception as exc:
            return jsonify(ok=False,error=(type(exc).__name__+': '+str(exc))[:180]),400

    @app.get('/api/v2core/reverse29/cases')
    def v2_core_reverse29_cases():
        return jsonify(ok=True,version=VERSION,items=R29_CASES,
                       selection='8 dates x 16/day; trend-stratified as-of selection only',
                       outcome_sorting=False,research_only=True)

    @app.post('/api/v2core/reverse29/analyze')
    def v2_core_reverse29_analyze():
        try:
            raw=request.get_json(silent=True)
            idx=raw.get('index') if isinstance(raw,dict) else None
            if isinstance(idx,bool) or not isinstance(idx,int) or not (0<=idx<len(R29_CASES)):
                raise ValueError('역추적 대상 번호 오류')
            case=R29_CASES[idx]
            if r28_dt(case['asof_kst']).date()>=datetime.now(kst).date():
                raise ValueError('완료되지 않은 날짜 조회 불가')
            with lock:
                previous=cache.setdefault('reverse29',{}).get(idx)
            if previous is not None and time.monotonic()-previous[0]<900:
                return jsonify(ok=True,result=previous[1],cached=True)
            result=r29_one_case(api_get,case)
            with lock:
                cache['reverse29'][idx]=(time.monotonic(),result)
            return jsonify(ok=True,result=result,cached=False)
        except Exception as exc:
            return jsonify(ok=False,error=(type(exc).__name__+': '+str(exc))[:180]),400

    @app.post('/api/v2core/reverse31/btc-context')
    def v2_core_reverse31_btc():
        try:
            raw=request.get_json(silent=True)
            cfg=lab_valid_request({'date':raw.get('date') if isinstance(raw,dict) else None,
                    'tp':1.5,'sl':1.,'horizon':60,'fee':.25,'slip':0},kst)
            day=cfg['day']
            with lock: item=cache['reverse31_btc'].get(day)
            if item and time.monotonic()-item[0]<900:
                return jsonify(ok=True,version=VERSION,points=len(item[1]),cached=True)
            rows,truncated,meta=c5_h2_v111434_fetch_extended(api_get,'KRW-BTC',cfg,kst)
            context=c30_btc_context(rows,day)
            if not context:raise ValueError('BTC 과거 5분봉 없음')
            with lock:cache['reverse31_btc'][day]=(time.monotonic(),context)
            return jsonify(ok=True,version=VERSION,points=len(context),cached=False)
        except Exception as exc:
            return jsonify(ok=False,error=(type(exc).__name__+': '+str(exc))[:180]),400

    @app.post('/api/v2core/reverse31/analyze')
    def v2_core_reverse31_analyze():
        try:
            raw=request.get_json(silent=True)
            if not isinstance(raw,dict):raise ValueError('설정 형식 오류')
            market=raw.get('market')
            if not isinstance(market,str) or not MARKET_RE.fullmatch(market):
                raise ValueError('KRW 종목 형식 오류')
            cfg=lab_valid_request({'date':raw.get('date'),'tp':1.5,'sl':1.,'horizon':60,
                                    'fee':raw.get('fee',.25),'slip':raw.get('slip',0)},kst)
            with lock:
                listed=(market in cache['markets'] if cache['markets'] and time.monotonic()-cache['at']<600 else None)
            if listed is False:raise ValueError('현재 KRW 목록에 없는 종목')
            if listed is None and market not in get_markets():raise ValueError('현재 KRW 목록에 없는 종목')
            key=(market,cfg['day'],cfg['fee'],cfg['slip'])
            with lock:
                old=cache['reverse31'].get(key)
                btc=cache['reverse31_btc'].get(cfg['day'])
            if old and time.monotonic()-old[0]<900:
                return jsonify(ok=True,version=VERSION,result=old[1],cached=True)
            rows,truncated,meta=c5_h2_v111434_fetch_extended(api_get,market,cfg,kst)
            context=btc[1] if btc and time.monotonic()-btc[0]<900 else {}
            data=c30_market_events(rows,cfg['day'],market,cfg['fee'],cfg['slip'],context,truncated)
            data['coverage']['fetch_pages']=meta.get('fetch_5m_pages_used',0)
            data['coverage']['btc_reference']='READY' if context else 'UNKNOWN'
            with lock:
                if len(cache['reverse31'])>550:cache['reverse31'].clear()
                cache['reverse31'][key]=(time.monotonic(),data)
            return jsonify(ok=True,version=VERSION,result=data,cached=False)
        except Exception as exc:
            return jsonify(ok=False,error=(type(exc).__name__+': '+str(exc))[:180]),400

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
            focus=core25_focus(rows,cfg,market,kst)
            data={'market':market,'core25_focus':focus,'coverage':{
                'observed_5m':len(rows),'fetch':meta,'B_signals':focus['coverage']['signals']}}
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
