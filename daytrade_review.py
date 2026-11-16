"""V2 CORE.25 LIGHT — research-only streamlined B 10m/SMA3 and CORE.24 comparison.

Full CORE.24 remains archived separately. No change to the app.py live BUY scanner.
This module replaces the original daytrade_review.py, retaining only necessary
signal discovery, CORE.23 PRIMARY rechecks, CORE.24 entry/exit diagnostics,
BTC market context and the same /v2-core-review /api/v2core/* endpoints.
All shared model functions are unchanged from CORE.24 unless marked CORE25.
No live trading or order submission.
"""
from datetime import datetime, timedelta, date as _date, time as _time
from math import isfinite
from threading import Lock
import time
import re
from flask import jsonify, render_template_string, request

VERSION = 'V2.CORE.25-LIGHT'

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
          'quality','fixed','trailing','path','failure_category')
    for row in report['events']:
        slim.append({k:row.get(k) for k in keep})
    return dict(events=slim,coverage=diagnostics,version=VERSION,
        unchanged_strategy='B_10M_MA03 + CORE23 PRIMARY + CORE24 same-entry exits',
        core24_math_unchanged=True,research_only=True)


CORE_PAGE = '<!doctype html>\n<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n<title>V2 CORE.25 LIGHT · 진입·청산 집중 검증</title>\n<style>\n:root{color-scheme:dark;--bg:#0b1020;--panel:#141d30;--line:#293750;--ink:#ecf2ff;--muted:#9aaccb;--blue:#779cff;--good:#4ade80;--bad:#ff8590}\n*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 system-ui,\'Noto Sans KR\',sans-serif}\nmain{max-width:980px;margin:auto;padding:20px 13px 58px}h1{font-size:23px;margin:0 0 3px}h2{font-size:17px;margin:0 0 12px}h3{font-size:14px;margin:0 0 10px}p{margin:5px 0 12px}\na{color:#b5c8ff}.muted{color:var(--muted);font-size:12px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:16px;margin:0 0 13px}\n.toolbar{display:flex;flex-wrap:wrap;gap:9px;align-items:flex-end}.field{display:flex;flex-direction:column;gap:5px;min-width:130px;flex:1}.field.small{min-width:95px;flex:.55}\nlabel{font-size:12px;color:var(--muted)}input,select,button{background:#0e172a;color:var(--ink);border:1px solid #465572;border-radius:8px;padding:10px;font:inherit;max-width:100%}button{background:#385fd4;border-color:#426ce9;font-weight:650;cursor:pointer}button.secondary{background:#233148;border-color:#43536b}button:disabled{opacity:.4;cursor:not-allowed}\nprogress{width:100%;height:11px;margin-top:15px}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.kpi{border:1px solid var(--line);border-radius:9px;padding:10px}.kpi strong{font-size:22px;display:block}.good{color:var(--good)}.bad{color:var(--bad)}.cols{display:flex;gap:12px;flex-wrap:wrap}.cols .field{max-width:205px}.table-scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:620px}th,td{padding:9px 8px;border-bottom:1px solid var(--line);white-space:nowrap;text-align:right;font-size:12px}th:first-child,td:first-child{text-align:left}th{color:#c2cff0;font-weight:650}tr:last-child td{border-bottom:0}\nsmall{font-size:11px}.note{background:#0e192d;padding:12px;border-radius:9px;color:var(--muted);font-size:12px}details{margin-top:10px}summary{cursor:pointer;font-weight:600;padding:8px 0}\n@media(max-width:620px){main{padding:14px 9px 42px}h1{font-size:20px}.panel{padding:12px}.kpis{grid-template-columns:repeat(2,1fr)}.field{min-width:45%}.field.small{min-width:45%}button{padding:10px 9px}}\n</style></head>\n<body><main>\n<h1>🧪 V2 CORE.25 LIGHT</h1>\n<p class="muted">B 10분봉 SMA3 · 10/20/30분 재검증 · 진입/청산 5방식 집중 비교</p>\n<p><a href="/">← 기존 실전 V2 스캐너로 돌아가기</a></p>\n<section class="panel"><h2>① 검사 설정</h2>\n<div class="toolbar">\n<div class="field"><label for="day">날짜 (KST · 완료된 하루)</label><input id="day" type="date"></div>\n<div class="field"><label for="scope">범위</label><select id="scope"><option value="20">거래대금 상위 20</option><option value="40" selected>상위 40</option><option value="80">상위 80</option><option value="all">전체 KRW 종목</option></select></div>\n<div class="field small"><label for="fee">편도 수수료 (%)</label><input id="fee" type="number" min="0" max="0.5" step="0.01" value="0.25"></div>\n<div class="field small"><label for="slip">편도 슬리피지 (%)</label><input id="slip" type="number" min="0" max="0.5" step="0.01" value="0"></div>\n<button id="run">검사 시작</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="csv" class="secondary" disabled>CORE.25 결과 CSV</button>\n</div>\n<progress id="progress" max="1" value="0"></progress>\n<div id="status" class="muted" aria-live="polite">검사 전 · BTC 맥락은 날짜당 한 번만 조회해.</div>\n<div class="muted">전체 종목은 실시간 API 응답 속도에 따라 오래 걸릴 수 있어. 거래대금 순위는 현재 조회 기준이므로 과거 시장 순위의 완전한 재현은 아니야.</div>\n</section>\n<section class="panel"><h2>② 핵심 결과</h2>\n<div class="kpis">\n<div class="kpi"><span class="muted">검사 완료</span><strong id="done">—</strong></div>\n<div class="kpi"><span class="muted">B형 원본 신호</span><strong id="signals">—</strong></div>\n<div class="kpi"><span class="muted">API 실패</span><strong id="failures">—</strong></div>\n<div class="kpi"><span class="muted">경과 시간</span><strong id="elapsed">—</strong></div>\n</div>\n<h3 style="margin-top:16px">10·20·30분 재검증 요약 · 전체 원본 신호 기준</h3>\n<div class="table-scroll"><table><thead><tr><th>대기</th><th>평가</th><th>기존 PASS</th><th>신규 PASS</th><th>BE 청산 확정</th><th>BE 평균 순수익</th></tr></thead><tbody id="delay-summary"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n</section>\n<section class="panel"><h2>③ 진입 품질·청산 방식 비교</h2>\n<div class="cols"><div class="field"><label for="delay">대기 시간</label><select id="delay"><option value="10" selected>10분</option><option value="20">20분</option><option value="30">30분</option></select></div>\n<div class="field"><label for="cohort">분석 대상</label><select id="cohort"><option value="ALL">전체 원본 신호</option><option value="A">기존 CORE.23 PASS</option><option value="B">신규 CORE.24 품질 PASS</option><option value="BOTH">두 필터 모두 PASS</option></select></div></div>\n<p class="muted" id="selected-note">진입품질 필터와 청산 규칙을 분리해 비교해.</p>\n<h3>진입 조건별 기존 BE 성과</h3>\n<div class="table-scroll"><table><thead><tr><th>필터</th><th>후보</th><th>청산 확정</th><th>수익 거래</th><th>평균 순수익</th></tr></thead><tbody id="quality-body"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<h3 style="margin-top:15px">같은 진입가에서 청산 방식 5가지 비교</h3>\n<div class="table-scroll"><table><thead><tr><th>청산 방식</th><th>확정 / 후보</th><th>수익 거래</th><th>평균 순수익</th><th>공통 표본 평균</th><th>공통 BE 대비</th></tr></thead><tbody id="exit-body"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<div class="note" id="matched-note">각 청산 방식의 확정 수가 다르면 평균을 직접 비교하지 말고, 다섯 방식이 모두 확정된 동일 표본 비교를 확인해야 해.</div>\n</section>\n<section class="panel"><h2>④ 실패 원인·종목 상세</h2>\n<div class="table-scroll"><table><thead><tr><th>BE 수익</th><th>수익 반납 가능성</th><th>플러스 기회 없음</th><th>작은 상승 기회</th><th>판정 불가</th></tr></thead><tbody id="diagnosis-body"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<p class="muted">MFE·MAE는 실제 5분봉 관측 고가·저가 기준의 사후 진단이야. 매매 시점에는 미래 가격을 쓰지 않아.</p>\n<details><summary>상세 종목 펼치기 (화면 최대 80건 · CSV는 전체)</summary>\n<div class="table-scroll"><table><thead><tr><th>검증 시각</th><th>코인</th><th>분류</th><th>기존 PASS</th><th>신규 PASS</th><th>BE</th><th>추적</th><th>30분 종가</th><th>MFE / MAE (60분)</th></tr></thead><tbody id="details-body"><tr><td colspan="9">검사 전</td></tr></tbody></table></div>\n</details></section>\n<div class="note">연구용 가상 체결 결과야. 기존 실전 BUY 로직과 주문 기능은 변경하지 않아. 갭·저유동성·분봉 내 고저가 순서 불명으로 실제 거래 결과와 다를 수 있어. 추적손절은 첫 익절 이전에 고정 손절이 없어 큰 손실 위험이 남아 있어.</div>\n</main>\n<script>\n\'use strict\';\nconst $=id=>document.getElementById(id);\nconst esc=s=>String(s??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nconst fmt=n=>n==null||!Number.isFinite(Number(n))?\'—\':(Number(n)>0?\'+\':\'\')+Number(n).toFixed(3)+\'%\';\nconst avg=a=>a.length?a.reduce((p,c)=>p+Number(c),0)/a.length:NaN;\nconst isReady=o=>o&&o.status===\'RESOLVED\'&&o.net_pct!=null&&Number.isFinite(Number(o.net_pct));\nconst be=e=>({status:e.postscan_be_status,net_pct:e.postscan_be_net_pct});\nconst rules=[[\'BE\',\'기존 30/30/40 BE\',be],[\'T15\',\'15분 종가\',e=>e.fixed?.[\'15\']],[\'T30\',\'30분 종가\',e=>e.fixed?.[\'30\']],[\'T60\',\'60분 종가\',e=>e.fixed?.[\'60\']],[\'TRAIL\',\'첫 익절 후 잔량 1% 추적\',e=>e.trailing]];\nlet state={running:false,pause:false,key:\'\',markets:[],cursor:0,done:0,results:[],errors:[],btc:null,btcError:\'\',start:0,date:\'\',fee:0.25,slip:0};\n$(\'day\').value=new Intl.DateTimeFormat(\'sv-SE\',{timeZone:\'Asia/Seoul\',year:\'numeric\',month:\'2-digit\',day:\'2-digit\'}).format(new Date(Date.now()-86400000));\nfunction btcOf(e){const stamp=e.search_time_kst||\'\';if(stamp.length<16||!state.btc)return null;\n const h=Number(stamp.slice(11,13)),m=Number(stamp.slice(14,16));let t=h*60+m-5;\n if(!Number.isInteger(t)||t<0)return null;\n const key=stamp.slice(0,10)+\' \'+String(Math.floor(t/60)).padStart(2,\'0\')+\':\'+String(t%60).padStart(2,\'0\');\n return state.btc[key]??null;}\nfunction gate23(e){const b=btcOf(e);if(e.local_guard===\'LOCAL_REJECT\'||b?.btc_regime===\'BTC_DOWN\')return \'REJECT\';\n if(e.local_guard===\'LOCAL_UNKNOWN\'||!b||b.btc_regime===\'UNKNOWN\')return \'UNKNOWN\';\n if(e.local_guard===\'LOCAL_STRONG\'&&[\'BTC_UP\',\'BTC_SIDE\'].includes(b.btc_regime))return \'PASS\';return \'WAIT\';}\nfunction gate24(e){const b=btcOf(e),local=e.quality?.local_gate;if(local===\'UNKNOWN\'||!b||b.btc_regime===\'UNKNOWN\')return \'UNKNOWN\';\n if(b.btc_regime===\'BTC_DOWN\')return \'REJECT\';if(local===\'PASS\'&&[\'BTC_UP\',\'BTC_SIDE\'].includes(b.btc_regime))return \'PASS\';return \'WAIT\';}\nfunction allRows(){return state.results.flatMap(r=>(r.core25_focus?.events||[]).map(e=>({...e,date:r.date,old_gate:gate23(e),new_gate:gate24(e)})));}\nfunction filtered(rows,cohort){return cohort===\'ALL\'?rows:rows.filter(e=>(cohort===\'A\'&&e.old_gate===\'PASS\')||(cohort===\'B\'&&e.new_gate===\'PASS\')||(cohort===\'BOTH\'&&e.old_gate===\'PASS\'&&e.new_gate===\'PASS\'));}\nfunction paint(){\n $(\'done\').textContent=`${state.done}/${state.markets.length||0}`;\n $(\'signals\').textContent=state.results.reduce((a,r)=>a+(r.core25_focus?.coverage?.signals||0),0);\n $(\'failures\').textContent=state.errors.length;\n $(\'elapsed\').textContent=state.start?((performance.now()-state.start)/1000).toFixed(1)+\'초\':\'—\';\n $(\'progress\').max=Math.max(1,state.markets.length);$(\'progress\').value=state.done;\n const rows=allRows();\n $(\'delay-summary\').innerHTML=[10,20,30].map(d=>{const g=rows.filter(e=>e.delay_minutes===d),done=g.filter(e=>isReady(be(e)));return `<tr><td>${d}분</td><td>${g.length}</td><td>${g.filter(e=>e.old_gate===\'PASS\').length}</td><td>${g.filter(e=>e.new_gate===\'PASS\').length}</td><td>${done.length}</td><td>${fmt(avg(done.map(e=>e.postscan_be_net_pct)))}</td></tr>`;}).join(\'\')||\'<tr><td colspan="6">신호 없음</td></tr>\';\n const delay=Number($(\'delay\').value),cohort=$(\'cohort\').value,group=rows.filter(e=>e.delay_minutes===delay),selected=filtered(group,cohort);\n $(\'quality-body\').innerHTML=[[\'전체\',()=>true],[\'기존 CORE.23\',e=>e.old_gate===\'PASS\'],[\'신규 CORE.24\',e=>e.new_gate===\'PASS\'],[\'두 필터\',e=>e.old_gate===\'PASS\'&&e.new_gate===\'PASS\']].map(([name,pred])=>{const x=group.filter(pred),r=x.filter(e=>isReady(be(e)));return `<tr><td>${name}</td><td>${x.length}</td><td>${r.length}</td><td>${r.filter(e=>Number(e.postscan_be_net_pct)>0).length}</td><td>${fmt(avg(r.map(e=>e.postscan_be_net_pct)))}</td></tr>`;}).join(\'\');\n const matched=selected.filter(e=>rules.every(([, ,ex])=>isReady(ex(e))));const base=avg(matched.map(e=>e.postscan_be_net_pct));\n $(\'selected-note\').textContent=`${delay}분 · 선택 ${selected.length}건 · 전체 원본 신호 ${group.length}건`;\n $(\'exit-body\').innerHTML=rules.map(([,label,ex])=>{const r=selected.map(ex).filter(isReady),shared=avg(matched.map(e=>ex(e).net_pct));return `<tr><td>${label}</td><td>${r.length}/${selected.length}</td><td>${r.filter(o=>Number(o.net_pct)>0).length}</td><td>${fmt(avg(r.map(o=>o.net_pct)))}</td><td>${fmt(shared)}</td><td>${fmt(Number.isFinite(base)&&Number.isFinite(shared)?shared-base:null)}</td></tr>`;}).join(\'\');\n $(\'matched-note\').textContent=`5가지 청산 방식 모두 결과가 확정된 동일 표본 ${matched.length}건. 공통 표본 기준에서만 전략 차이를 비교해. 미확정 거래를 수익이나 손실로 간주하지 않아.`;\n const cats=[\'BE_PROFIT\',\'GIVEBACK_OPPORTUNITY\',\'NO_POSITIVE_MFE\',\'SMALL_OPPORTUNITY\',\'UNDETERMINED\'];\n $(\'diagnosis-body\').innerHTML=\'<tr>\'+cats.map(k=>`<td>${selected.filter(e=>e.failure_category===k).length}</td>`).join(\'\')+\'</tr>\';\n $(\'details-body\').innerHTML=selected.slice(0,80).map(e=>{const m=e.path?.m60,tri=e.trailing,tm=e.fixed?.[\'30\'];return `<tr><td>${esc(e.search_time_kst)}</td><td>${esc(e.market)}</td><td>${esc(e.state)}</td><td>${e.old_gate}</td><td>${e.new_gate}</td><td>${isReady(be(e))?fmt(e.postscan_be_net_pct):\'—\'}</td><td>${isReady(tri)?fmt(tri.net_pct):\'—\'}</td><td>${isReady(tm)?fmt(tm.net_pct):\'—\'}</td><td>${m?.status===\'READY\'?fmt(m.mfe_gross_pct)+\' / \'+fmt(m.mae_gross_pct):\'—\'}</td></tr>`;}).join(\'\')||\'<tr><td colspan="9">종목 없음</td></tr>\';\n}\n$(\'delay\').onchange=paint;$(\'cohort\').onchange=paint;\nasync function api(url,payload){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),55000);try{const r=await fetch(url,{method:payload?\'POST\':\'GET\',headers:{\'Content-Type\':\'application/json\'},body:payload?JSON.stringify(payload):undefined,cache:\'no-store\',signal:controller.signal});const j=await r.json();if(!r.ok||!j.ok)throw Error(j.error||`HTTP ${r.status}`);return j;}finally{clearTimeout(timer)}}\n$(\'pause\').onclick=()=>{state.pause=true;$(\'pause\').disabled=true;};\n$(\'run\').onclick=async()=>{\n if(state.running)return;\n const date=$(\'day\').value,fee=Number($(\'fee\').value),slip=Number($(\'slip\').value),scope=$(\'scope\').value;\n if(!date||!Number.isFinite(fee)||fee<0||fee>0.5||!Number.isFinite(slip)||slip<0||slip>0.5){alert(\'날짜와 편도 비용을 확인해.\');return;}\n const key=[date,scope,fee,slip].join(\'|\');const resume=state.key===key&&state.markets.length&&state.cursor<state.markets.length;\n if(!resume)state={running:false,pause:false,key,markets:[],cursor:0,done:0,results:[],errors:[],btc:null,btcError:\'\',start:performance.now(),date,fee,slip};\n state.running=true;state.pause=false;$(\'run\').disabled=true;$(\'pause\').disabled=false;$(\'csv\').disabled=true;\n try{\n  if(!resume){$(\'status\').textContent=\'코인 목록 조회 중...\';const u=await api(\'/api/v2core/universe\');state.markets=u.items.slice(0,scope===\'all\'?u.items.length:Number(scope));\n   try{const b=await api(\'/api/v2core/btc-context\',{date});state.btc=b.context;}catch(e){state.btcError=String(e.message||e);state.btc=null;}\n  }\n  paint();const total=state.markets.length;\n  async function work(){while(!state.pause&&state.cursor<total){const market=state.markets[state.cursor++];try{const a=await api(\'/api/v2core/evaluate\',{market,date,fee,slip});state.results.push(a.result);}catch(e){state.errors.push({market,error:String(e.message||e)});}finally{state.done++;if(state.done%10===0||state.done===total){paint();$(\'status\').textContent=`${state.done}/${total} 검사 · 오류 ${state.errors.length} · BTC ${state.btc?\'확인\':\'미확인\'}`;}}}}\n  await Promise.all([work(),work()]);paint();const finished=state.cursor===total&&state.done===total;\n  $(\'status\').textContent=(state.pause?\'일시 중지 · 같은 조건으로 검사 시작을 다시 누르면 이어서 진행\':finished?\'검사 완료\':\'검사 중단\')+` · ${state.done}/${total} · API 오류 ${state.errors.length}`+(state.btcError?\' · BTC 미확인: \'+state.btcError:\'\');\n  $(\'csv\').disabled=state.results.length===0;\n }catch(e){$(\'status\').textContent=\'⚠️ \'+String(e.message||e);}finally{state.running=false;$(\'run\').disabled=false;$(\'pause\').disabled=true;paint();}\n};\nfunction csvCell(s){const text=String(s??\'\');return /[",\\n\\r]/.test(text)?\'"\'+text.replace(/"/g,\'""\')+\'"\':text;}\n$(\'csv\').onclick=()=>{\n const cols=[\'version\',\'date\',\'market\',\'base_id\',\'signal_time_kst\',\'search_time_kst\',\'delay_minutes\',\'entry_time_kst\',\'entry_price\',\n \'core23_final_gate\',\'core24_final_gate\',\'trend_state\',\'review_status\',\'liquidity_tier\',\'price_change_pct\',\'core24_local_gate\',\'core24_quality_reason\',\n \'original_be_status\',\'original_be_net_pct\',\'core24_be_status\',\'core24_be_net_pct\',\'core24_time15_status\',\'core24_time15_net_pct\',\n \'core24_time30_status\',\'core24_time30_net_pct\',\'core24_time60_status\',\'core24_time60_net_pct\',\'core24_trail_status\',\'core24_trail_net_pct\',\n \'core24_trail_tp1_filled\',\'core24_trail_tp1_min\',\'core24_trail_stop_min\',\'core24_trail_ambiguous_bar\',\'core24_mfe15_gross_pct\',\n \'core24_mae15_gross_pct\',\'core24_mfe30_gross_pct\',\'core24_mae30_gross_pct\',\'core24_mfe60_gross_pct\',\'core24_mae60_gross_pct\',\n \'core24_mfe60_net_upper_pct\',\'core24_mfe60_first_min\',\'core24_mae60_first_min\',\'failure_category\',\'btc_regime\',\'btc_ret60_pct\',\n \'quality_volume_ratio_4v12\',\'quality_price_structure\',\'fee_pct_side\',\'slip_pct_side\',\'scan_complete\',\'scan_errors\'];\n const total=state.markets.length,complete=state.done===total&&state.cursor===total,all=allRows();\n const records=all.map(e=>{const btc=btcOf(e);return {\n version:\'V2.CORE.25-LIGHT\',date:e.date,market:e.market,base_id:e.base_id,signal_time_kst:e.signal_time_kst,search_time_kst:e.search_time_kst,\n delay_minutes:e.delay_minutes,entry_time_kst:e.entry_time_kst,entry_price:e.entry_price,core23_final_gate:e.old_gate,core24_final_gate:e.new_gate,\n trend_state:e.state,review_status:e.review_status,liquidity_tier:e.liquidity_tier,price_change_pct:e.price_change_pct,\n core24_local_gate:e.quality?.local_gate,core24_quality_reason:e.quality?.reason,original_be_status:e.original_be_status,original_be_net_pct:e.original_be_net_pct,\n core24_be_status:e.postscan_be_status,core24_be_net_pct:e.postscan_be_net_pct,\n core24_time15_status:e.fixed?.[\'15\']?.status,core24_time15_net_pct:e.fixed?.[\'15\']?.net_pct,\n core24_time30_status:e.fixed?.[\'30\']?.status,core24_time30_net_pct:e.fixed?.[\'30\']?.net_pct,\n core24_time60_status:e.fixed?.[\'60\']?.status,core24_time60_net_pct:e.fixed?.[\'60\']?.net_pct,\n core24_trail_status:e.trailing?.status,core24_trail_net_pct:e.trailing?.net_pct,core24_trail_tp1_filled:e.trailing?.tp1_filled,\n core24_trail_tp1_min:e.trailing?.tp1_min,core24_trail_stop_min:e.trailing?.trail_min,core24_trail_ambiguous_bar:e.trailing?.ambiguous_same_bar,\n core24_mfe15_gross_pct:e.path?.m15?.mfe_gross_pct,core24_mae15_gross_pct:e.path?.m15?.mae_gross_pct,\n core24_mfe30_gross_pct:e.path?.m30?.mfe_gross_pct,core24_mae30_gross_pct:e.path?.m30?.mae_gross_pct,\n core24_mfe60_gross_pct:e.path?.m60?.mfe_gross_pct,core24_mae60_gross_pct:e.path?.m60?.mae_gross_pct,\n core24_mfe60_net_upper_pct:e.path?.m60?.mfe_net_upper_pct,core24_mfe60_first_min:e.path?.m60?.mfe_first_min,\n core24_mae60_first_min:e.path?.m60?.mae_first_min,failure_category:e.failure_category,btc_regime:btc?.btc_regime??\'UNKNOWN\',\n btc_ret60_pct:btc?.btc_ret60_pct,quality_volume_ratio_4v12:e.quality?.volume_ratio_4v12,\n quality_price_structure:e.quality?.has_price_structure,fee_pct_side:state.fee,slip_pct_side:state.slip,scan_complete:complete,scan_errors:state.errors.length};});\n const lines=[cols.join(\',\'),...records.map(e=>cols.map(k=>csvCell(e[k])).join(\',\'))];const blob=new Blob([\'\\ufeff\'+lines.join(\'\\n\')],{type:\'text/csv;charset=utf-8\'}),url=URL.createObjectURL(blob),a=document.createElement(\'a\');\n a.href=url;a.download=\'bithumb_V2_CORE25_LIGHT_ENTRY_EXIT_BE.csv\';a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);\n};\npaint();\n</script></body></html>\n'

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
