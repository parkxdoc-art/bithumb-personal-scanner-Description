"""V2 CORE.40 LIGHT — causal Elliott-inspired wave-4 reversal research + CORE39 baseline.

Standalone replacement for daytrade_review.py; integrates with the existing
Flask app via register_daytrade_review(app, deps).  Does not change app.py.
Research only: no order placement or automated execution.
"""
from datetime import date, datetime, time as dt_time, timedelta
from math import isfinite
from threading import Lock
import re
import time
from flask import Response, jsonify, request

VERSION = 'V2.CORE.40-LIGHT-WAVE4-TO-WAVE5-RESEARCH'
MARKET_PATTERN = re.compile(r'^KRW-[A-Z0-9]+$')
FIVE = timedelta(minutes=5)
KST_FORMAT = '%Y-%m-%d %H:%M:%S'


def _price_pct(new, old):
    return (new / old - 1.0) * 100.0 if old > 0 else None


def _check_cfg(raw, kst):
    if not isinstance(raw, dict):
        raise ValueError('검사 요청 형식이 올바르지 않습니다')
    try:
        day = date.fromisoformat(str(raw['date']))
        fee, slip = float(raw.get('fee', .25)), float(raw.get('slip', 0))
    except (ValueError, KeyError, TypeError, OverflowError):
        raise ValueError('날짜 또는 비용 값이 올바르지 않습니다')
    now = datetime.now(kst).date()
    if day < now - timedelta(days=60) or day >= now:
        raise ValueError('종료된 최근 60일 안의 날짜만 조회 가능합니다')
    if any(not isfinite(v) for v in (fee, slip)) or not 0 <= fee <= .5 or not 0 <= slip <= .5:
        raise ValueError('편도 수수료·슬리피지는 각각 0~0.5% 범위여야 합니다')
    return {'day': day, 'fee': fee, 'slip': slip}


def _parse_candle(x, kst):
    t = datetime.fromisoformat(str(x['candle_date_time_kst']))
    t = t.replace(tzinfo=kst) if t.tzinfo is None else t.astimezone(kst)
    o, h, l, c = (float(x[k]) for k in ('opening_price', 'high_price', 'low_price', 'trade_price'))
    v = float(x['candle_acc_trade_volume'])
    cash = float(x.get('candle_acc_trade_price', c*v))
    if (any(not isfinite(z) for z in (o, h, l, c, v, cash)) or min(o, h, l, c) <= 0 or
            min(v, cash) < 0 or l > min(o, c) or h < max(o, c) or l > h):
        raise ValueError('잘못된 분봉 가격 또는 거래량')
    return {'t': t, 'o': o, 'h': h, 'l': l, 'c': c, 'v': v, 'cash': cash}


def fetch_5m(api_get, market, day, kst):
    """Fetch 2h prehistory + full day + 2h after, exclusive KST page cursor."""
    start = datetime.combine(day, dt_time.min, tzinfo=kst) - timedelta(hours=2)
    end = datetime.combine(day + timedelta(days=1), dt_time.min, tzinfo=kst) + timedelta(hours=2)
    cursor = end
    seen = {}
    pages = 0
    truncated = True
    bad = 0
    for _ in range(4):
        response = api_get('/v1/candles/minutes/5', {
            'market': market, 'to': cursor.strftime(KST_FORMAT), 'count': 200})
        if not isinstance(response, list):
            raise ValueError('5분봉 응답이 배열이 아닙니다')
        pages += 1
        oldest = None
        for x in response:
            try:
                c = _parse_candle(x, kst)
                t = c['t']
                oldest = t if oldest is None or t < oldest else oldest
                if start <= t < end:
                    seen[t] = c
            except (ValueError, KeyError, TypeError, OverflowError):
                bad += 1
        if oldest is None:
            truncated = False
            break
        if oldest <= start or len(response) < 200:
            truncated = False
            break
        if oldest >= cursor:
            raise ValueError('분봉 페이지 커서가 이동하지 않습니다')
        cursor = oldest
    return [seen[x] for x in sorted(seen)], {'pages': pages, 'bad_candles': bad,
                                             'range_truncated': truncated, 'bars': len(seen)}


def _contiguous(bars, i, n):
    if i - n + 1 < 0:
        return False
    window = bars[i-n+1:i+1]
    return all(window[j]['t'] - window[j-1]['t'] == FIVE for j in range(1, len(window)))


def _ma5(bars, i):
    if not _contiguous(bars, i, 5):
        return None
    return sum(x['c'] for x in bars[i-4:i+1]) / 5


def _net_pct(entry, exit_price, fee, slip):
    side = (fee + slip)/100
    return 100*(exit_price*(1-side)/(entry*(1+side))-1)


def _exit_sim(bars, entry_i, fee, slip, target_net, stop_gross=.7, hold_bars=12):
    """Open next 5m, 30/60-min exits; conservative stop-first OHLC ambiguity.

    Missing future bars => UNKNOWN, unless exit was already reached.
    """
    if entry_i is None or entry_i < 0 or entry_i >= len(bars):
        return {'status':'UNKNOWN', 'reason':'진입봉 없음'}
    entry = bars[entry_i]['o']
    side = (fee+slip)/100
    limit = entry*(1+side)*(1+target_net/100)/(1-side)
    stop = entry*(1-stop_gross/100)
    prev = None
    high = entry
    low = entry
    for k in range(entry_i, entry_i+hold_bars):
        if k >= len(bars):
            return {'status':'UNKNOWN', 'reason':'청산 기간 분봉 부족'}
        bar = bars[k]
        if prev is not None and bar['t']-prev != FIVE:
            return {'status':'UNKNOWN', 'reason':'진입 후 분봉 공백'}
        prev = bar['t']
        high = max(high, bar['h'])
        low = min(low, bar['l'])
        if bar['o'] <= stop:
            outcome = ('SL_GAP', bar['o'])
        elif bar['o'] >= limit:
            outcome = ('TP_GAP', bar['o'])
        elif bar['l'] <= stop:
            outcome = ('SL', stop)  # includes same-bar TP; conservative
        elif bar['h'] >= limit:
            outcome = ('TP', limit)
        else:
            outcome = None
        if outcome is not None:
            kind, px = outcome
            return {'status':'READY', 'exit':kind, 'entry_price':entry,
                    'exit_price':px, 'entry_at':bars[entry_i]['t'].isoformat(),
                    'exit_at':(bar['t']+FIVE).isoformat(),
                    'net_pct':round(_net_pct(entry, px, fee, slip),5),
                    'bars_held':k-entry_i+1,
                    'mfe_high_pct':round(_price_pct(high,entry),4),
                    'mae_low_pct':round(_price_pct(low,entry),4)}
    bar = bars[entry_i+hold_bars-1]
    return {'status':'READY', 'exit':'TIME60', 'entry_price':entry,
            'exit_price':bar['c'], 'entry_at':bars[entry_i]['t'].isoformat(),
            'exit_at':(bar['t']+FIVE).isoformat(),
            'net_pct':round(_net_pct(entry,bar['c'],fee,slip),5),
            'bars_held':hold_bars,
            'mfe_high_pct':round(_price_pct(high,entry),4),
            'mae_low_pct':round(_price_pct(low,entry),4)}



def _sma(bars, i, n):
    if not _contiguous(bars, i, n):
        return None
    return sum(x['c'] for x in bars[i-n+1:i+1]) / n


def _score(bars, i, peak, rise_pct, box_start):
    """Signal-time quality score (0..100), NOT a post-event best pick."""
    b = bars[i]
    sma20 = _sma(bars,i,20)
    old20 = _sma(bars,i-4,20)
    slope = _price_pct(sma20,old20) if sma20 and old20 else None
    depth = (peak-b['c'])/peak*100
    box_bars = bars[box_start:i+1]
    bh, bl = max(x['h'] for x in box_bars), min(x['l'] for x in box_bars)
    width = (bh/bl-1)*100 if bl>0 else 999
    # volume slowdown observable at i only, not a requirement
    recent=sum(x['v'] for x in bars[i-2:i+1])/3 if i>=2 else 0
    prev=sum(x['v'] for x in bars[i-8:i-2])/6 if i>=8 else 0
    volratio=recent/prev if prev else None
    sc=0
    if 5 <= rise_pct <= 12: sc+=20
    elif rise_pct<=18: sc+=12
    else: sc+=5
    if 0.3<=depth<=2.3: sc+=22
    elif depth<=3.5: sc+=10
    if slope is not None and slope>=0: sc+=18
    elif slope is not None and slope>=-.10: sc+=8
    if width <=1.0: sc+=20
    elif width<=2.0: sc+=12
    if volratio is not None and volratio<=.9: sc+=10
    elif volratio is not None and volratio<=1.4: sc+=5
    if b['h']>b['l'] and (b['c']-b['l'])/(b['h']-b['l'])>=.55: sc+=10
    return int(min(100,sc)), {'depth_pct':round(depth,4),'box_width_pct':round(width,4),
        'sma20_slope_20m_pct':round(slope,4) if slope is not None else None,
        'vol_ratio_3_to_6':round(volratio,4) if volratio is not None else None}


def _next_entry(rows, signal_i):
    if signal_i is None or signal_i+1>=len(rows): return None
    if rows[signal_i+1]['t']-rows[signal_i]['t']!=FIVE: return None
    return signal_i+1


def _add_simulations(e, rows, fee, slip, signals):
    for method, signal_i in signals.items():
        e[f'{method}_signal_at']=(rows[signal_i]['t']+FIVE).isoformat() if signal_i is not None else ''
        if signal_i is None:
            for target in (.3,.5):
                suf=f'{method}_tp{int(target*10):02}_'
                e[suf+'status']='NO_SIGNAL'
            continue
        ei=_next_entry(rows,signal_i)
        for target in (.3,.5):
            result=_exit_sim(rows,ei,fee,slip,target)
            for key,val in result.items():
                e[f'{method}_tp{int(target*10):02}_{key}']=val



def _sma(bars, i, n):
    if not _contiguous(bars, i, n):
        return None
    return sum(x['c'] for x in bars[i-n+1:i+1]) / n


def _score(bars, i, peak, rise_pct, box_start):
    """Signal-time quality score (0..100), NOT a post-event best pick."""
    b = bars[i]
    sma20 = _sma(bars,i,20)
    old20 = _sma(bars,i-4,20)
    slope = _price_pct(sma20,old20) if sma20 and old20 else None
    depth = (peak-b['c'])/peak*100
    box_bars = bars[box_start:i+1]
    bh, bl = max(x['h'] for x in box_bars), min(x['l'] for x in box_bars)
    width = (bh/bl-1)*100 if bl>0 else 999
    # volume slowdown observable at i only, not a requirement
    recent=sum(x['v'] for x in bars[i-2:i+1])/3 if i>=2 else 0
    prev=sum(x['v'] for x in bars[i-8:i-2])/6 if i>=8 else 0
    volratio=recent/prev if prev else None
    sc=0
    if 5 <= rise_pct <= 12: sc+=20
    elif rise_pct<=18: sc+=12
    else: sc+=5
    if 0.3<=depth<=2.3: sc+=22
    elif depth<=3.5: sc+=10
    if slope is not None and slope>=0: sc+=18
    elif slope is not None and slope>=-.10: sc+=8
    if width <=1.0: sc+=20
    elif width<=2.0: sc+=12
    if volratio is not None and volratio<=.9: sc+=10
    elif volratio is not None and volratio<=1.4: sc+=5
    if b['h']>b['l'] and (b['c']-b['l'])/(b['h']-b['l'])>=.55: sc+=10
    return int(min(100,sc)), {'depth_pct':round(depth,4),'box_width_pct':round(width,4),
        'sma20_slope_20m_pct':round(slope,4) if slope is not None else None,
        'vol_ratio_3_to_6':round(volratio,4) if volratio is not None else None}


def _next_entry(rows, signal_i):
    if signal_i is None or signal_i+1>=len(rows): return None
    if rows[signal_i+1]['t']-rows[signal_i]['t']!=FIVE: return None
    return signal_i+1


def _add_simulations(e, rows, fee, slip, signals):
    for method, signal_i in signals.items():
        e[f'{method}_signal_at']=(rows[signal_i]['t']+FIVE).isoformat() if signal_i is not None else ''
        if signal_i is None:
            for target in (.3,.5):
                suf=f'{method}_tp{int(target*10):02}_'
                e[suf+'status']='NO_SIGNAL'
            continue
        ei=_next_entry(rows,signal_i)
        for target in (.3,.5):
            result=_exit_sim(rows,ei,fee,slip,target)
            for key,val in result.items():
                e[f'{method}_tp{int(target*10):02}_{key}']=val



def _box_signal_diagnostics(bars, dip_i, box_i):
    """Only completed bars at/before the breakout; no forward-looking inputs.

    The PRE box excludes the breakout bar and starts with the dip bar.
    These fields are diagnostics ONLY; never used to pick or score trades.
    """
    pre=bars[dip_i:box_i]
    b=bars[box_i]
    if not pre:
        return {}
    upper=max(x['h'] for x in pre)
    lower=min(x['l'] for x in pre)
    crange=b['h']-b['l']
    prior=pre[-3:]  # 1-3 bars; record the denominator count
    avg_volume=sum(x['v'] for x in prior)/len(prior)
    avg_cash=sum(x['cash'] for x in prior)/len(prior)
    return {
        'box_pre_bars':len(pre),
        'box_pre_upper':round(upper,10),
        'box_pre_lower':round(lower,10),
        'box_pre_width_pct':round((upper/lower-1)*100,4),
        'box_breakout_over_pre_upper_pct':round(_price_pct(b['c'],upper),4),
        'box_breakout_open':round(b['o'],10),
        'box_breakout_high':round(b['h'],10),
        'box_breakout_low':round(b['l'],10),
        'box_breakout_close':round(b['c'],10),
        'box_breakout_body_change_pct':round(_price_pct(b['c'],b['o']),4),
        'box_breakout_body_share_pct':round((b['c']-b['o'])/crange*100,4) if crange else None,
        'box_breakout_upper_wick_pct':round((b['h']-max(b['o'],b['c']))/crange*100,4) if crange else None,
        'box_breakout_close_location_pct':round((b['c']-b['l'])/crange*100,4) if crange else None,
        'box_breakout_candle_range_pct':round((b['h']/b['l']-1)*100,4),
        'box_breakout_volume':round(b['v'],8),
        'box_breakout_volume_base_bars':len(prior),
        'box_breakout_volume_vs_pre3':round(b['v']/avg_volume,4) if avg_volume>0 else None,
        'box_breakout_cash_vs_pre3':round(b['cash']/avg_cash,4) if avg_cash>0 else None,
        'box_breakout_cash_krw':round(b['cash'],2),
    }


def _box_firstbar_diagnostics(bars, box_i, sim, fee, slip):
    """AFTER-ENTRY labels only: NEVER feed these into signal scoring/selection."""
    ei=_next_entry(bars,box_i)
    ret={'box_first5_data_ok':False,'box_first5_sl':False,'box_first5_tp':False,
         'box_first5_touch_both':False}
    if ei is None:
        return ret
    b=bars[ei]
    entry=b['o']
    side=(fee+slip)/100
    stop=entry*.993
    tp=entry*(1+side)*1.005/(1-side)
    exit_kind=sim.get('exit','')
    held=sim.get('bars_held')
    ret.update({
        'box_first5_data_ok':True,
        'box_first5_sl':bool(held==1 and exit_kind.startswith('SL')),
        'box_first5_tp':bool(held==1 and exit_kind.startswith('TP')),
        'box_first5_touch_both':bool(b['l']<=stop and b['h']>=tp),
        'box_first5_low_from_entry_pct':round(_price_pct(b['l'],entry),4),
        'box_first5_high_from_entry_pct':round(_price_pct(b['h'],entry),4),
    })
    return ret


def analyze_pullbacks(rows, day, market, fee=.25, slip=0):
    """CORE36: online rolling low -> completed +5% impulse -> first SMA5 dip -> box/reclaim.

    'Pump' is first seen using a completed candle and a prior (strictly historical)
    24-bar rolling low, from this KST day only. No future swing-low pivot label.
    All event features are frozen at their respective signal time. The next bar
    open is the earliest hypothetical fill. Exit OHLC is conservatively stop-first.
    """
    coverage={'market':market,'date':day.isoformat(),'bar_count':len(rows),
              'contiguous_scan_bars':0,'pump_setups':0,'dip_setups':0,
              'reclaim_setups':0,'box_break_setups':0,'reason':''}
    result={'market':market,'date':day.isoformat(),'coverage':coverage,'events':[]}
    if not rows:
        coverage['reason']='분봉 없음';return result
    tz=rows[0]['t'].tzinfo
    day_start=datetime.combine(day, dt_time.min,tzinfo=tz)
    day_end=day_start+timedelta(days=1)
    # No exact midnight open required; only completed bars at/after 00:00 KST.
    first=next((i for i,b in enumerate(rows) if b['t']>=day_start),len(rows))
    last=next((i for i,b in enumerate(rows) if b['t']>=day_end),len(rows))
    if last-first<12:
        coverage['reason']='당일 5분봉 부족'; return result
    event_last=-9999
    armed=None
    in_box=None
    events=[]
    day_cash=0.0
    for i in range(first,last):
        b=rows[i]
        if b['t'].time() >= dt_time(22, 15):
            break
        day_cash+=b['cash']
        if not _contiguous(rows,i,25):
            # Do not extend a setup across missing 5m candles.
            armed=None;in_box=None
            continue
        coverage['contiguous_scan_bars']+=1
        ma5=_sma(rows,i,5)
        ma20=_sma(rows,i,20)
        prev5=_sma(rows,i-1,5)
        prev20=_sma(rows,i-4,20)
        if None in (ma5,ma20,prev5,prev20):continue

        if in_box is not None:
            st=in_box
            # only information at this bar can affect current setup
            boxlen=i-st['dip_i']+1
            prev_high=max(x['h'] for x in rows[st['dip_i']:i]) if i>st['dip_i'] else st['peak']
            st['last_i']=i
            st['box_high']=max(st['box_high'],b['h'])
            st['box_low']=min(st['box_low'],b['l'])
            depth=(st['peak']-b['c'])/st['peak']*100
            bad=(depth>3.5 or b['c']<ma20*.995 or ma20 < prev20*.995)
            if bad:
                st['invalid_reason']='20이평·눌림 깊이 이탈'
                in_box=None;armed=None
                continue
            if boxlen>=2:
                if st['reclaim_i'] is None and b['c']>ma5 and b['c']>rows[i-1]['c']:
                    st['reclaim_i']=i
                    st['reclaim_cash']=day_cash
                    st['reclaim_score'],st['reclaim_diag']=_score(rows,i,st['peak'],st['rise'],st['dip_i'])
                # Previous box high only; current bar cannot set its own breakout threshold.
                if (st['box_i'] is None and b['c']>prev_high and
                    b['c']>ma5 and b['c']>ma20 and b['c']>rows[i-1]['c']):
                    st['box_i']=i
                    st['box_cash']=day_cash
                    st['box_score'],st['box_diag']=_score(rows,i,st['peak'],st['rise'],st['dip_i'])
                    st['box_signal_diagnostics']=_box_signal_diagnostics(rows,st['dip_i'],i)
            if st['reclaim_i'] is not None and st['box_i'] is not None:
                in_box=None; armed=None
            elif boxlen>=16:
                in_box=None;armed=None
            continue

        if i-event_last<24:  # 2h cooldown after a dip on the same coin
            continue
        if armed:
            if i-armed['pump_i']>36 or b['c']<armed['peak']*.965:
                armed=None
            else:
                armed['peak']=max(armed['peak'],b['h'])
                # the first below-SMA5 close after at least 2 above-SMA5 closes
                mprev2=_sma(rows,i-2,5)
                if (mprev2 and rows[i-2]['c']>mprev2 and rows[i-1]['c']>prev5
                    and b['c']<ma5 and b['c']>=ma20*.995 and ma20>=prev20*.995):
                    dip_depth=(armed['peak']-b['c'])/armed['peak']*100
                    if .2<=dip_depth<=3.5:
                        sc,diag=_score(rows,i,armed['peak'],armed['rise'],i)
                        st=dict(pump_i=armed['pump_i'],anchor_i=armed['anchor_i'],
                            anchor_low=armed['anchor_low'],peak=armed['peak'],rise=armed['rise'],
                            dip_i=i,box_high=b['h'],box_low=b['l'],reclaim_i=None,box_i=None,
                            dip_score=sc,dip_diag=diag,reclaim_score=None,reclaim_diag={},reclaim_cash=None,
                            box_score=None,box_diag={},box_cash=None,invalid_reason='')
                        event={'market':market,'date':day.isoformat(),
                          'pump_signal_at':(rows[armed['pump_i']]['t']+FIVE).isoformat(),
                          'swing_low_at':rows[armed['anchor_i']]['t'].isoformat(),
                          'swing_low_price':round(armed['anchor_low'],10),
                          'pump_gain_pct':round(armed['rise'],4),
                          'pump_peak_before_dip':round(armed['peak'],10),
                          'dip_bar_at':b['t'].isoformat(),
                          'dip_signal_at':(b['t']+FIVE).isoformat(),
                          'dip_close_price':b['c'], 'dip_sma5':round(ma5,10),
                          'dip_sma20':round(ma20,10),
                          'dip_below_sma5_pct':round(_price_pct(b['c'],ma5),4),
                          'dip_depth_pct':round(dip_depth,4),
                          'day_trade_krw_at_dip':round(day_cash,2),
                          'prior_60m_trade_krw':round(sum(x['cash'] for x in rows[i-11:i+1]),2),
                          'score_dip':sc,**{'dip_'+k:v for k,v in diag.items()}}
                        events.append(event)
                        st['event']=event
                        st['last_i']=i
                        event['_pending_state']=st
                        # store a pending setup, to be populated with future signals
                        in_box=st;armed=None;event_last=i
                        coverage['dip_setups']+=1
                        continue

        # Set up a new impulse only if NOT armed; rising completed closes above SMA5.
        # Anchor is the minimum LOW of at most 24 prior completed bars since 00:00.
        if armed is None and i-first>=6:
            prior=rows[max(first,i-24):i]
            anchor=min(prior,key=lambda x:x['l'])
            anchor_i=next((j for j in range(max(first,i-24),i) if rows[j] is anchor),None)
            gain=_price_pct(b['c'],anchor['l'])
            prev_gain=_price_pct(rows[i-1]['c'],anchor['l'])
            if (anchor_i is not None and gain is not None and gain>=5 and
                 b['c']>ma5 and rows[i-1]['c']>prev5 and
                 ma5>=prev5 and b['c']>=ma20 and
                 (prev_gain is None or prev_gain>=3.0)):
                armed={'pump_i':i,'anchor_i':anchor_i,'anchor_low':anchor['l'],
                       'rise':gain,'peak':b['h']}
                coverage['pump_setups']+=1

    # All future information is used ONLY to label later signals and outcomes.
    # Reconstruct box signals in their event rows without changing older event fields.
    # in_box has been collected as a reference in each event via closure-style st object.
    # (populated in _box_state keyed by event identity in the loop; see below)
    for e in events:
        st=e.pop('_pending_state',None)
        if st is None:
            # Explicit unknown result rather than silently claiming zero signals.
            _add_simulations(e,rows,fee,slip,{'dip':None,'reclaim':None,'box':None})
            continue
        e['box_bars_observed']=min(16, max(1,st['last_i']-st['dip_i']+1))
        e['box_width_pct']=round((st['box_high']/st['box_low']-1)*100,4)
        e['reclaim_score']=st['reclaim_score'];e['box_score']=st['box_score']
        e['reclaim_day_trade_krw_at_signal']=round(st['reclaim_cash'],2) if st['reclaim_cash'] is not None else None
        e['box_day_trade_krw_at_signal']=round(st['box_cash'],2) if st['box_cash'] is not None else None
        e['reclaim_minutes']=(st['reclaim_i']-st['dip_i'])*5 if st['reclaim_i'] is not None else None
        e['box_break_minutes']=(st['box_i']-st['dip_i'])*5 if st['box_i'] is not None else None
        e['box_invalid_reason']=st['invalid_reason']
        if st['box_i'] is not None:
            e.update(st.get('box_signal_diagnostics',{}))
        for stage in ('reclaim','box'):
            for k,v in st[stage+'_diag'].items():e[stage+'_'+k]=v
        _add_simulations(e,rows,fee,slip,{'dip':st['dip_i'],'reclaim':st['reclaim_i'],'box':st['box_i']})
        if st['box_i'] is not None:
            sim={'exit':e.get('box_tp05_exit',''), 'bars_held':e.get('box_tp05_bars_held')}
            e.update(_box_firstbar_diagnostics(rows,st['box_i'],sim,fee,slip))
        if st['reclaim_i'] is not None: coverage['reclaim_setups']+=1
        if st['box_i'] is not None: coverage['box_break_setups']+=1
    coverage['events']=len(events)
    if coverage['contiguous_scan_bars']==0:coverage['reason']='연속 5분봉 부족'
    result['events']=events
    return result

CORE_PAGE = '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>V2 CORE.40 LIGHT · 4파 눌림 → 5파 상승 연구</title><style>\n:root{color-scheme:dark;--bg:#0b1020;--panel:#151e31;--edge:#2b3954;--fg:#ecf2ff;--sub:#aab8d0;--blue:#87a4ff;--good:#4ade80;--bad:#ff8797}\n*{box-sizing:border-box}body{background:var(--bg);color:var(--fg);font:14px/1.55 system-ui,"Noto Sans KR",sans-serif;margin:0}main{max-width:1030px;margin:auto;padding:18px 13px 70px}\nh1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:0 0 11px}h3{font-size:14px;margin:14px 0 6px}p{margin:6px 0 12px}a{color:#b3c8ff}.muted{color:var(--sub);font-size:12px}.panel{border:1px solid var(--edge);background:var(--panel);border-radius:14px;padding:16px;margin:13px 0}\n.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:9px}.field{display:flex;flex-direction:column;gap:4px}label{font-size:12px;color:var(--sub)}input,select,button{width:100%;padding:11px;background:#111a2d;border:1px solid #465775;border-radius:8px;color:var(--fg);font:inherit}\nbutton{background:#3765df;font-weight:650;cursor:pointer}button.secondary{background:#273852;border-color:#415470}button:disabled{opacity:.4;cursor:default}.controls{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.controls button{width:auto;flex:1;min-width:105px}\n.kpis{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.kpi{border:1px solid var(--edge);border-radius:9px;padding:13px}.kpi .n{font-size:22px;font-weight:750}.good{color:var(--good)}.bad{color:var(--bad)}.warning{color:#ffd483}\n.tablewrap{overflow:auto;max-width:100%}table{border-collapse:collapse;width:100%;min-width:620px}th,td{font-size:12px;padding:9px;border-bottom:1px solid var(--edge);white-space:nowrap;text-align:right}th:first-child,td:first-child{text-align:left}th{color:#c3d1ec}tr:last-child td{border:0}.note{background:#0c172c;color:#b8c6dd;border-radius:9px;padding:12px;font-size:12px}.pill{display:inline-block;background:#283958;border:1px solid #4c6288;padding:2px 6px;border-radius:5px;font-size:11px}\nprogress{width:100%;height:12px}details{margin:10px 0}summary{cursor:pointer;padding:10px 0;font-weight:650}\n@media(max-width:640px){main{padding:13px 9px 44px}.panel{padding:12px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.field{min-width:0}h1{font-size:20px}button{padding:10px 8px}}\n</style></head><body><main>\n<header><h1>🧪 V2 CORE.40 LIGHT · 4파 눌림 → 5파 상승 연구</h1><p class="muted">자정 기준가격 제거 · 00시 이후 실제 상승파동 +5% · 5분봉 SMA5/SMA20 · 연구 전용 (자동 주문 없음)</p><a href="/">← 실전 스캐너로 돌아가기</a></header>\n<section class="panel"><h2>① 4파 눌림에서 진입해 5파 상승 노리기</h2>\n<p><b>1파 상승 → 2파 눌림 → 3파 상승 → 4파 눌림</b>이 확인될 때 4파에서 먼저 진입하고, <b>5파 상승 여부</b>를 추적합니다. 5파는 반드시 발생하는 게 아닙니다. 기존 C형은 같은 기간 비교 기준으로만 유지합니다.</p>\n<p>진입 방식 비교: <b>W4 조기 반등</b> / <b>W4 저점 확인</b> / 기존 <b>C · 눌림 박스 돌파</b>. 모두 완성된 분봉 이후 다음 시가 진입을 가정합니다.</p>\n<div class="note"><b>기존 C형 비교 기준:</b> +5%는 자정 대비가 아닙니다. 00시부터 검사하되 각 시점 직전 최대 24개 5분봉의 실제 저점을 기준으로 계산합니다. 5이평은 SMA5, 추세 지지 참고는 SMA20입니다. 이탈 후 20이평을 크게 밑돌거나 눌림 깊이가 3.5%를 초과하면 회복 신호가 무효화됩니다. <b>승률이 검증된 진입 규칙이 아닙니다.</b></div></section>\n<section class="panel"><h2>🔎 CORE.40 파동 연구 (CORE.40 기존 신호 보존)</h2>\n<p><b>W0 저점 → 1파 상승 → 2파 눌림 → 3파 고점 → 4파 눌림</b>을 완료된 5분봉만으로 판별합니다. 4파 진입은 두 방식:</p>\n<ul><li><b>W4 조기 반등:</b> 3파 고점 확인 후 4파 되돌림이 18~70%이고, 1파 고점보다 위에서 SMA5 부근으로 반등할 때 다음 봉 시가 진입 (4파 저점은 잠정)</li>\n<li><b>W4 저점 확인:</b> 4파 저점을 좌우 2개 5분봉으로 확인한 후 반등 진입 (확정은 10분 지연)</li></ul>\n<p>0·1·2·3·4파 라벨은 임시 판별입니다. 5파는 <b>성공한다고 가정하지 않습니다.</b> 1차 비교는 +0.5% 전량 익절이며, 30/30/40 + BE 결과도 같은 CSV에 기록합니다. <b>진입 후 60분 안에 3파 고점을 재돌파했는지</b>는 별도 사후 라벨로 분석합니다.</p><p>기존 CORE.40의 C형 결과는 유지하고 별도 파동 진입과 비교하며 수익 거래와 즉시 손절 거래를 비교합니다.</p><div class="note">돌파 전 박스 폭 = 첫 눌림봉부터 돌파 바로 전봉까지의 고가/저가 범위(돌파봉 제외). 돌파봉 몸통 상승률 · 윗꼬리 비율 · 종가 위치 · 상단 초과폭 · 최근 박스 최대 3봉 대비 거래량을 저장합니다. 진입 후 첫 봉 손절·익절과 동봉 TP/SL 양쪽 터치 여부는 <b>사후 결과 라벨</b>로만 표시하며 BUY 필터에 넣지 않습니다.</div></section>\n<section class="panel"><h2>② 검사 설정</h2><div class="grid">\n<div class="field"><label for="endDay">마지막 날짜 (KST·지난 날짜)</label><input id="endDay" type="date"></div>\n<div class="field"><label for="days">날짜 수</label><select id="days"><option value="1" selected>1일</option><option value="3">3일</option><option value="5">5일</option></select></div>\n<div class="field"><label for="scope">검사 범위</label><select id="scope"><option value="all" selected>전체 KRW</option><option value="40">현재 거래대금 상위 40</option><option value="80">현재 거래대금 상위 80</option></select></div>\n<div class="field"><label for="fee">편도 수수료 (%)</label><input id="fee" type="number" min="0" max="0.5" step="0.01" value="0.25"></div>\n<div class="field"><label for="slip">편도 슬리피지 (%)</label><input id="slip" type="number" min="0" max="0.5" step="0.01" value="0"></div>\n<div class="field"><label for="method">진입 비교 방식</label><select id="method"><option value="wave40_early" selected>W4 · 조기 반등 (잠정 4파)</option><option value="wave40_confirm">W4 · 저점 확인 뒤 반등</option><option value="box">기존 C · 눌림 박스 상단 돌파</option><option value="reclaim">B · SMA5 재회복</option><option value="dip">A · SMA5 첫 이탈 (비교용)</option></select></div>\n<div class="field"><label for="target">익절 순수익</label><select id="target"><option value="05" selected>+0.5%</option><option value="03">+0.3%</option></select></div>\n<div class="field"><label for="cap">하루 최대 선정</label><select id="cap"><option value="3">3건</option><option value="4" selected>4건</option></select></div>\n<div class="field"><label for="minScore">신호 품질 점수 (시점 기준)</label><select id="minScore"><option value="0">전체 (비교용)</option><option value="50">50점 이상</option><option value="65" selected>65점 이상</option><option value="80">80점 이상</option></select></div>\n<div class="field"><label for="minCash">신호 당시 당일 거래대금</label><select id="minCash"><option value="0">제한 없음</option><option value="500000000" selected>5억 원 이상</option></select></div>\n<div class="field"><label for="selectMode">하루 최대 4건 분산 선정</label><select id="selectMode"><option value="slot" selected>6시간대별 최대 1건</option><option value="first">전체 하루 시간순 최대 4건</option></select></div>\n</div><div class="controls"><button id="run">CORE.40 4파 연구 검사</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="csv" class="secondary" disabled>CORE.40 파동 CSV 다운로드</button></div>\n<progress id="bar" value="0" max="1"></progress><p id="status" class="muted" aria-live="polite">검사 전</p>\n<div class="note">상위 40·80종목은 현재 24시간 거래대금 기준이며, 당시 순위로 소급한 것은 아닙니다. 4개 작업을 동시에 조회합니다. 자동 주문은 하지 않습니다. 전체 검사 완료 전에도 부분 CSV를 저장할 수 있습니다. 검사 종료 후 파일을 보내주면 결과를 비교합니다. 시간대 분산은 사전 규칙이며 나중에 오른 코인만 고르는 방식이 아닙니다.</div></section>\n<section class="panel"><h2>③ 하루 0~4건 검증 · W4 조기/확인 vs C</h2><div class="kpis">\n<div class="kpi"><span class="muted">완료 종목·날짜</span><div id="kDone" class="n">—</div></div>\n<div class="kpi"><span class="muted">기존 눌림 사건</span><div id="kDips" class="n">—</div></div>\n<div class="kpi"><span class="muted">4파 조기 / 4파 확인 신호</span><div id="kBox" class="n">—</div></div>\n<div class="kpi"><span class="muted">API·자료 오류</span><div id="kErrors" class="n">—</div></div>\n<div class="kpi"><span class="muted">선정 거래</span><div id="kSelected" class="n">—</div></div>\n<div class="kpi"><span class="muted">확정 거래 평균 순익</span><div id="kNet" class="n">—</div></div>\n<div class="kpi"><span class="muted">선정 신호 · 첫 5분봉 손절</span><div id="kSL5" class="n bad">—</div></div>\n<div class="kpi"><span class="muted">선정 신호 · 첫 봉 TP·SL 동시 터치</span><div id="kBoth" class="n warning">—</div></div>\n</div>\n<h3>선정 거래 · 4파의 눌림 깊이·청산·첫 5분봉 확인</h3><div class="tablewrap"><table><thead><tr><th>날짜·종목</th><th>진입 신호</th><th>1~3파 상승</th><th>품질</th><th>4파 되돌림</th><th>4파 저점 거리</th><th>2파 되돌림</th><th>3파 돌파폭</th><th>파동 유형</th><th>첫 5분봉</th><th>청산</th><th>순수익</th></tr></thead><tbody id="selectedTable"><tr><td colspan="12">검사 전</td></tr></tbody></table></div>\n<h3>날짜별 성과</h3><div class="tablewrap"><table><thead><tr><th>날짜</th><th>적격 후보</th><th>선정</th><th>확정</th><th>수익</th><th>손실</th><th>평균 순익</th></tr></thead><tbody id="dailyTable"><tr><td colspan="7">검사 전</td></tr></tbody></table></div></section>\n<section class="panel"><h2>④ 신규 4파 진입 vs 기존 C형 비교 · 전체 발생 신호</h2>\n<div class="tablewrap"><table><thead><tr><th>진입 방식</th><th>신호 수</th><th>청산 확정</th><th>순이익</th><th>평균 순수익</th></tr></thead><tbody id="compareTable"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<details><summary>전체 눌림 자세히 보기 (화면 100건, CSV 전체)</summary><div class="tablewrap"><table><thead><tr><th>날짜·종목</th><th>눌림 시각</th><th>상승파동</th><th>눌림 깊이</th><th>SMA5 회복</th><th>박스 돌파</th><th>C 품질점수</th><th>C 순익</th></tr></thead><tbody id="allTable"><tr><td colspan="8">검사 전</td></tr></tbody></table></div></details>\n<p class="muted">비교표는 전체 신호 기준(선정 정책 미적용), ③은 하루 최대 4건 제한 기준입니다. <b>4파 가설은 엘리엇 파동을 사후 확정한 것이 아니라, 과거 봉만으로 만든 객관적 시험 규칙</b>입니다. 4파 확정형은 저점 확인 때문에 2봉 지연됩니다. 원본 BTC 흐름·시장 변동성과 상관없이 이번에는 가격 구조 하나를 먼저 검증합니다.</p></section>\n<section class="panel"><h2>⑤ 검증상 주의</h2><p>이 전략은 과거 데이터로 검증하는 가설입니다. 5이평 이탈이 건강한 눌림인지 추세 종료인지는 사전 확정할 수 없습니다. 종가 신호는 완성 후에만 확인하고 다음 분봉 시가에 체결을 가정합니다.</p><p class="muted">30/30/40 + 본절로스 결과는 CSV에 보존합니다. 수수료 편도 0.25%·슬리피지 0% 기본, 순익 목표 +0.3/+0.5%, 고정 손절 -0.7% (수수료 포함 순손실 약 -1.2%), 60분 시간청산. 동일 봉 TP/SL 동시 충족은 보수적으로 손절 우선. 결측봉 청산 미확정은 무리하게 보충하지 않습니다. 실제 슬리피지·호가·주문 체결은 반영되지 않습니다.</p></section>\n<script>\n \'use strict\';\nconst $=id=>document.getElementById(id);\nconst CODE_VERSION=\'V2.CORE.40-LIGHT-WAVE4-TO-WAVE5-RESEARCH\';\nconst esc=s=>String(s??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nconst fmt=(v,d=3)=>v===null||v===undefined||!Number.isFinite(Number(v))?\'—\':Number(v).toFixed(d)+\'%\';\nconst num=v=>Number(v||0).toLocaleString(\'ko-KR\');\nconst todayKST=()=>new Date(Date.now()+9*3600000).toISOString().slice(0,10);\nfunction daysEnding(end,n){const a=[];for(let i=n-1;i>=0;i--){const d=new Date(end+\'T00:00:00Z\');d.setUTCDate(d.getUTCDate()-i);a.push(d.toISOString().slice(0,10));}return a;}\n$(\'endDay\').value=new Date(Date.now()+9*3600000-86400000).toISOString().slice(0,10);\n$(\'endDay\').max=$(\'endDay\').value;\nfunction cfg(){const end=$(\'endDay\').value,n=Number($(\'days\').value),scope=$(\'scope\').value,fee=Number($(\'fee\').value),slip=Number($(\'slip\').value);\n if(!end||end>=todayKST())throw Error(\'종료된 날짜를 선택해 줘\');\n if(![1,3,5].includes(n)||![\'all\',\'40\',\'80\'].includes(scope))throw Error(\'범위 설정 오류\');\n if([fee,slip].some(x=>!Number.isFinite(x)||x<0||x>.5))throw Error(\'비용 입력은 0~0.5%\');\n return {days:daysEnding(end,n),scope,fee,slip};}\nfunction policy(){return {method:$(\'method\').value,target:$(\'target\').value,cap:Number($(\'cap\').value),minScore:Number($(\'minScore\').value),minCash:Number($(\'minCash\').value),selectMode:$(\'selectMode\').value};}\nlet state=null,running=false,paused=false;\nconst KEYSET=[\'endDay\',\'days\',\'scope\',\'fee\',\'slip\'];\nfunction api(url,payload){return fetch(url,{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(payload)}).then(async r=>{const j=await r.json().catch(()=>({error:\'응답을 읽을 수 없음\'}));if(!r.ok||!j.ok)throw Error(j.error||\'HTTP \'+r.status);return j;});}\nfunction allEvents(){return state?state.results.flatMap(r=>r.events||[]):[];}\nfunction allWaveEvents(){return state?state.results.flatMap(r=>r.wave_events||[]):[];}\nfunction methodEvents(m){return m.startsWith(\'wave40_\')?allWaveEvents():allEvents();}\nfunction methodKey(m,target){return m.startsWith(\'wave40_\')?m+\'_tp\'+target:m+\'_tp\'+target;}\nfunction scoreKey(m){return m===\'dip\'?\'score_dip\':m+\'_score\';}\nfunction cashKey(m){return m===\'dip\'?\'day_trade_krw_at_dip\':m+\'_day_trade_krw_at_signal\';}\nfunction candidateId(e){return e.wave40_event_id||[e.date,e.market,e.dip_signal_at].join(\'|\');}\nfunction first5sl(e,m){return m.startsWith(\'wave40_\')?e[m+\'_first5_sl\']:e.box_first5_sl;}\nfunction first5both(e,m){return m.startsWith(\'wave40_\')?e[m+\'_first5_touch_both\']:e.box_first5_touch_both;}\nfunction pickRows(){\n if(!state)return {selected:[],byDay:[],nCandidates:0};\n const p=policy(),key=methodKey(p.method,p.target);\n const qual=methodEvents(p.method).filter(e=>e[p.method+\'_signal_at\'] && Number(e[cashKey(p.method)])>=p.minCash && Number(e[scoreKey(p.method)])>=p.minScore)\n .map(e=>({e,at:e[p.method+\'_signal_at\'],rkey:key,method:p.method,score:Number(e[scoreKey(p.method)])}))\n .sort((a,b)=>a.at.localeCompare(b.at)||b.score-a.score||a.e.market.localeCompare(b.e.market));\n const selected=[],byDay=[];\n for(const day of state.days){const pool=qual.filter(x=>x.e.date===day),chosen=[],usedSlots=new Set(),usedCoins=new Set();let busyUntil=\'\';\n  for(const item of pool){if(chosen.length>=p.cap)break;\n   const slot=Math.floor(Number(item.at.slice(11,13))/6);\n   if(p.selectMode===\'slot\'&&usedSlots.has(slot))continue;\n   if(usedCoins.has(item.e.market))continue;\n   if(busyUntil&&item.at<busyUntil)continue;\n   chosen.push(item);selected.push(item);usedSlots.add(slot);usedCoins.add(item.e.market);\n   const st=item.e[key+\'_status\'];busyUntil=st===\'READY\'?(item.e[key+\'_exit_at\']||item.at):day+\'T23:59:59+09:00\';\n  }\n  const ready=chosen.filter(x=>x.e[key+\'_status\']===\'READY\');const wins=ready.filter(x=>Number(x.e[key+\'_net_pct\'])>0);\n  byDay.push({date:day,candidates:pool.length,selected:chosen.length,ready:ready.length,wins:wins.length,loss:ready.length-wins.length,avg:ready.length?ready.reduce((s,x)=>s+Number(x.e[key+\'_net_pct\']),0)/ready.length:null});\n }\n return {selected,byDay,nCandidates:qual.length};\n}\nfunction rows(id,html,n){$(id).innerHTML=html||`<tr><td colspan="${n}">해당 결과 없음</td></tr>`;}\nfunction render(){if(!state)return;const ev=allEvents(),wv=allWaveEvents(),p=policy(),pick=pickRows(),key=methodKey(p.method,p.target);\n const ready=pick.selected.filter(x=>x.e[key+\'_status\']===\'READY\'),avg=ready.length?ready.reduce((s,x)=>s+Number(x.e[key+\'_net_pct\']),0)/ready.length:null;\n $(\'kDone\').textContent=num(state.done)+\' / \'+num(state.tasks.length);$(\'kDips\').textContent=num(ev.length);\n $(\'kBox\').textContent=wv.filter(e=>e.wave40_early_signal_at).length+\' / \'+wv.filter(e=>e.wave40_confirm_signal_at).length;\n $(\'kErrors\').textContent=num(state.errors.length+state.results.filter(r=>r.coverage.reason).length);\n $(\'kSelected\').textContent=num(pick.selected.length);$(\'kNet\').textContent=fmt(avg);$(\'kNet\').className=\'n \'+(avg!==null?(avg>=0?\'good\':\'bad\'):\'\');\n $(\'kSL5\').textContent=num(pick.selected.filter(x=>first5sl(x.e,x.method)).length);$(\'kBoth\').textContent=num(pick.selected.filter(x=>first5both(x.e,x.method)).length);\n $(\'bar\').max=Math.max(1,state.tasks.length);$(\'bar\').value=state.done;\n rows(\'selectedTable\',pick.selected.map(x=>{const e=x.e,m=x.method,v=e[key+\'_net_pct\'];const wave=m.startsWith(\'wave40_\');const sl=first5sl(e,m);const both=first5both(e,m);const f5=sl?\'첫 봉 SL\':e[m+\'_first5_data_ok\']?\'첫 봉 유지\':wave?\'진단 기록\':e.box_first5_tp?\'첫 봉 TP\':e.box_first5_data_ok?\'첫 봉 유지\':\'미확인\';return `<tr><td>${esc(e.date)} ${esc(e.market)}</td><td>${esc(x.at.slice(11,16))}</td><td>${fmt(wave?e.total_w0_w3_pct:e.pump_gain_pct)}</td><td>${x.score}</td><td>${fmt(wave?e[m+\'_w4_retrace_pct\']:e.box_pre_width_pct)}</td><td>${fmt(wave?e[m+\'_w4_stop_dist_pct\']:e.box_breakout_body_change_pct)}</td><td>${fmt(wave?e.w2_retrace_pct:e.box_breakout_upper_wick_pct)}</td><td>${fmt(wave?e.w3_beyond_w1_pct:e.box_breakout_over_pre_upper_pct)}</td><td>${wave?m===\'wave40_early\'?\'조기\':\'확인\':\'기존 C\'}</td><td class="${sl?\'bad\':\'\'}">${f5}${both?\' · 양방향\':\'\'}</td><td>${esc(e[key+\'_exit\']||e[key+\'_status\']||\'—\')}</td><td class="${v===undefined?\'\':v>=0?\'good\':\'bad\'}">${fmt(v)}</td></tr>`;}).join(\'\'),12);\n rows(\'dailyTable\',pick.byDay.map(d=>`<tr><td>${esc(d.date)}</td><td>${d.candidates}</td><td>${d.selected}</td><td>${d.ready}</td><td class="good">${d.wins}</td><td class="bad">${d.loss}</td><td>${fmt(d.avg)}</td></tr>`).join(\'\'),7);\n rows(\'compareTable\',[\'wave40_early\',\'wave40_confirm\',\'box\'].map(m=>{const k=methodKey(m,p.target);const list=methodEvents(m).filter(e=>e[m+\'_signal_at\']);const rd=list.filter(e=>e[k+\'_status\']===\'READY\');const win=rd.filter(e=>Number(e[k+\'_net_pct\'])>0).length;const mean=rd.length?rd.reduce((a,e)=>a+Number(e[k+\'_net_pct\']),0)/rd.length:null;return `<tr><td>${({wave40_early:\'W4 · 조기 반등\',wave40_confirm:\'W4 · 저점 확인\',box:\'기존 C · 박스 돌파\'})[m]}</td><td>${list.length}</td><td>${rd.length}</td><td>${win}</td><td>${fmt(mean)}</td></tr>`}).join(\'\'),5);\n rows(\'allTable\',ev.slice(0,100).map(e=>`<tr><td>${esc(e.date)} ${esc(e.market)}</td><td>${esc(e.dip_signal_at?.slice(11,16))}</td><td>${fmt(e.pump_gain_pct)}</td><td>${fmt(e.dip_depth_pct)}</td><td>${esc(e.reclaim_signal_at?.slice(11,16)||\'미확인\')}</td><td>${esc(e.box_signal_at?.slice(11,16)||\'미확인\')}</td><td>${e.box_score??\'—\'}</td><td>${fmt(e[\'box_tp\'+p.target+\'_net_pct\'])}</td></tr>`).join(\'\'),8);\n}\nfunction status(t){$(\'status\').textContent=t;}\nfunction setRunning(x){running=x;$(\'run\').disabled=x;$(\'pause\').disabled=!x;KEYSET.forEach(id=>$(id).disabled=x);}\n$(\'pause\').onclick=()=>{paused=true;status(\'일시 중지 요청 · 진행 중인 작업만 완료합니다\');};\n[\'method\',\'target\',\'cap\',\'minScore\',\'minCash\',\'selectMode\'].forEach(id=>$(id).addEventListener(\'change\',render));\n$(\'run\').onclick=async()=>{if(running)return;try{const c=cfg(),key=JSON.stringify(c);\n if(!state||state.key!==key||state.cursor===state.tasks.length){const r=await fetch(\'/api/v2core/universe\').then(x=>x.json());if(!r.ok||!Array.isArray(r.items))throw Error(r.error||\'KRW 목록 조회 실패\');const markets=c.scope===\'all\'?r.items:r.items.slice(0,Number(c.scope));const tasks=c.days.flatMap(date=>markets.map(market=>({date,market})));state={key,...c,tasks,cursor:0,done:0,results:[],errors:[]};}\n paused=false;setRunning(true);render();$(\'csv\').disabled=false;let last=0;\n async function worker(){while(!paused&&state.cursor<state.tasks.length){const task=state.tasks[state.cursor++];try{const out=await api(\'/api/v2core/wave40/analyze\',{...task,fee:state.fee,slip:state.slip});state.results.push(out.result);}catch(e){state.errors.push({...task,error:String(e.message||e)});}finally{state.done++;if(state.done-last>=8||state.done===state.tasks.length){last=state.done;render();status(`${state.done}/${state.tasks.length} 검사 중 · 오류 ${state.errors.length}`);}}}}\n await Promise.all(Array.from({length:Math.min(4,state.tasks.length)},()=>worker()));render();status(paused?\'일시 중지 · 다시 검사하면 같은 설정부터 이어서 진행\':`완료 ${state.done}/${state.tasks.length} · 눌림 ${allEvents().length}건 · CSV 저장 가능`);\n }catch(e){status(\'⚠️ \'+String(e.message||e));}finally{setRunning(false);$(\'csv\').disabled=!state||(!state.results.length&&!state.errors.length);}};\nfunction csvVal(v){let s=(v!==null&&typeof v===\'object\')?JSON.stringify(v):String(v??\'\');if(/^[=+@-]/.test(s)&&isNaN(Number(s)))s="\'"+s;return \'"\'+s.replace(/"/g,\'""\')+\'"\';}\n$(\'csv\').onclick=()=>{if(!state)return;\n const pick=pickRows(),chosen=new Set(pick.selected.map(x=>candidateId(x.e)));\n const p=policy();const base={version:CODE_VERSION,date_from:state.days[0],date_to:state.days.at(-1),scope:state.scope,fee_pct_side:state.fee,slippage_pct_side:state.slip,scan_complete:state.done===state.tasks.length,scan_done:state.done,scan_tasks:state.tasks.length,scan_errors:state.errors.length,selection_method:p.method,selection_mode:p.selectMode,min_score:p.minScore,daily_max:p.cap,target_net_pct:p.target===\'05\'?.5:.3,stop_gross_pct:.7,horizon_min:60,wave_pivot:\'LEFT2_RIGHT2_CAUSALLY_CONFIRMED\',wave_model:\'W0L_W1H_W2L_W3H_W4L_NON_OVERLAP\',simulation:\'NEXT_OPEN_OHLC_STOP_FIRST\',baseline:\'CORE39_UNCHANGED\'};\n const out=[];for(const d of pick.byDay)out.push({...base,row_type:\'DAY_SUMMARY\',date:d.date,...d});\n for(const r of state.results){out.push({...base,row_type:\'MARKET_COVERAGE\',date:r.date,market:r.market,...r.coverage});\n  for(const e of r.events||[])out.push({...base,row_type:\'PULLBACK_EVENT\',selected_for_daily_limit:p.method.startsWith(\'wave40_\')?false:chosen.has(candidateId(e)),selected_entry_method:p.method.startsWith(\'wave40_\')?\'\':p.method,...e});\n  for(const e of r.wave_events||[])out.push({...base,row_type:\'WAVE40_EVENT\',selected_for_daily_limit:p.method.startsWith(\'wave40_\')&&chosen.has(candidateId(e)),selected_entry_method:p.method.startsWith(\'wave40_\')?p.method:\'\',...e});}\n for(const e of state.errors)out.push({...base,row_type:\'API_ERROR\',...e});\n const cols=[...new Set([\'row_type\',\'version\',\'date\',\'market\',\'selected_for_daily_limit\',\'selected_entry_method\',\'wave40_event_id\',\'wave40_early_signal_at\',\'wave40_confirm_signal_at\',\'wave40_early_tp05_net_pct\',\'wave40_confirm_tp05_net_pct\',\'wave40_early_split_net_pct\',\'wave40_confirm_split_net_pct\',\'wave40_early_first5_sl\',\'wave40_confirm_first5_sl\',\'wave40_early_wave5_w3_high_reached_60m\',\'wave40_confirm_wave5_w3_high_reached_60m\',\'w0_at\',\'w1_at\',\'w2_at\',\'w3_at\',\'w1_gain_pct\',\'w2_retrace_pct\',\'w3_gain_pct\',\'w3_beyond_w1_pct\',\'total_w0_w3_pct\',...out.flatMap(x=>Object.keys(x))])];\n const csv=\'\\ufeff\'+[cols.join(\',\'),...out.map(r=>cols.map(c=>csvVal(r[c])).join(\',\'))].join(\'\\r\\n\');const u=URL.createObjectURL(new Blob([csv],{type:\'text/csv;charset=utf-8\'}));const a=document.createElement(\'a\');a.href=u;a.download=`CORE40_WAVE4_TO_WAVE5_${state.days[0]}_${state.days.at(-1)}_${state.scope}_KRW.csv`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),3000);};\n</script></main></body></html>'
def _exit_split_be_sim(bars, entry_i, fee, slip, stop_gross=.7, hold_bars=12):
    """30/30/40 at NET +0.5/+1.5/+3.0%, cost-aware BE after TP1.

    All checks use completed 5-min OHLC; within a candle an uncertain stop
    is assumed BEFORE a favorable take-profit. TP1 and BE can both be filled
    in the same candle conservatively. A downward opening gap through a stop
    fills at that opening, not the stop level. No implied execution guarantee.
    """
    if entry_i is None or entry_i < 0 or entry_i >= len(bars):
        return {'status':'UNKNOWN','reason':'진입봉 없음','net_pct':None}
    entry=bars[entry_i]['o']
    side=(fee+slip)/100.0
    be_price=entry*(1+side)/(1-side)
    targets=[be_price*(1+x/100) for x in (.5,1.5,3.0)]
    weights=(.30,.30,.40)
    stop0=entry*(1-stop_gross/100.0)
    sold=0
    weighted=0.0
    realized=[]
    previous_time=None
    high=entry
    low=entry
    summary={'status':'UNKNOWN','entry_price':entry,'entry_at':bars[entry_i]['t'].isoformat(),
             'be_price':round(be_price,10),'stop0_price':round(stop0,10),
             'target1_price':round(targets[0],10),'target2_price':round(targets[1],10),
             'target3_price':round(targets[2],10),'tp1_hit':False,'tp2_hit':False,
             'tp3_hit':False,'be_triggered':False,'net_pct':None}

    def fill(fraction, price, reason, t):
        nonlocal sold, weighted
        sold+=fraction
        pnl=_net_pct(entry,price,fee,slip)
        weighted+=fraction*pnl
        realized.append({'weight_pct':round(fraction*100,4),'exit':reason,'at':t,
                         'price':round(price,10),'net_pct':round(pnl,5)})

    def finish(reason, at, i):
        summary.update(status='READY',exit=reason,exit_at=at,
                       bars_held=i-entry_i+1,net_pct=round(weighted,5),
                       mfe_high_pct=round(_price_pct(high,entry),4),
                       mae_low_pct=round(_price_pct(low,entry),4),
                       fills=realized,weight_sold_pct=round(sold*100,4))
        return summary

    for k in range(entry_i,entry_i+hold_bars):
        if k>=len(bars):
            summary.update(reason='청산 기간 분봉 부족',fills=realized,weight_sold_pct=round(sold*100,4))
            return summary
        bar=bars[k]
        if previous_time is not None and bar['t']-previous_time!=FIVE:
            summary.update(reason='진입 후 분봉 공백',fills=realized,weight_sold_pct=round(sold*100,4))
            return summary
        previous_time=bar['t']
        high=max(high,bar['h'])
        low=min(low,bar['l'])
        at=(bar['t']+FIVE).isoformat()

        if sold < 1.0-1e-8:
            active_stop=stop0 if sold==0 else be_price
            # Stop first, including same-bar limit/stop collision.
            if bar['o']<=active_stop or bar['l']<=active_stop:
                gap=bar['o']<=active_stop
                px=bar['o'] if gap else active_stop
                kind=('SL_GAP' if gap else 'SL') if sold==0 else ('BE_GAP' if gap else 'BE')
                fill(1.0-sold,px,kind,at)
                if sold>0 and kind.startswith('BE'):summary['be_triggered']=True
                return finish(kind,at,k)

            if sold==0 and bar['h']>=targets[0]:
                fill(weights[0],targets[0],'TP1',at)
                summary['tp1_hit']=True
                summary['tp1_at']=at
                # The low may have come before or after TP1; pessimistically
                # assume the remaining fraction was stopped at BE after TP1.
                if bar['l']<=be_price:
                    fill(1.0-sold,be_price,'BE_SAME_BAR',at)
                    summary['be_triggered']=True
                    return finish('BE_SAME_BAR',at,k)
            # If we were already in the trade with TP1 in an earlier candle,
            # the stop above would have been checked first.
            if sold>.0 and sold < 1.0-1e-8:
                if not summary['tp2_hit'] and bar['h']>=targets[1]:
                    fill(weights[1],targets[1],'TP2',at)
                    summary['tp2_hit']=True
                    summary['tp2_at']=at
                if bar['h']>=targets[2] and not summary['tp3_hit']:
                    # TP3 implies TP2 was also crossed if not yet filled.
                    if not summary['tp2_hit']:
                        fill(weights[1],targets[1],'TP2',at)
                        summary['tp2_hit']=True
                        summary['tp2_at']=at
                    fill(weights[2],targets[2],'TP3',at)
                    summary['tp3_hit']=True
                    summary['tp3_at']=at
                    return finish('TP3',at,k)
        if k==entry_i+hold_bars-1 and sold<1.0-1e-8:
            fill(1.0-sold,bar['c'],'TIME60',at)
            return finish('TIME60',at,k)
    return summary


# ------------ CORE40: incremental wave-4 diagnostics, no future pivots ------------

def _wave40_pivot(rows, i):
    """A pivot at i-2 is *first known* at the close of i (2 right bars).

    Use 2 left/2 right *consecutive* completed bars, never forward bars after i.
    Ignore a bar that qualifies as both high and low to avoid intrabar-order bias.
    """
    j=i-2
    if j-2 < 0 or not _contiguous(rows,i,5):
        return None
    window=rows[j-2:j+3]
    hi=rows[j]['h']; lo=rows[j]['l']
    is_high=all(hi>=x['h'] for k,x in enumerate(window) if k!=2) and any(hi>x['h'] for k,x in enumerate(window) if k!=2)
    is_low=all(lo<=x['l'] for k,x in enumerate(window) if k!=2) and any(lo<x['l'] for k,x in enumerate(window) if k!=2)
    if is_high==is_low: return None
    return {'kind':'H' if is_high else 'L','i':j,'price':hi if is_high else lo, 'known_i':i}


def _wave40_update_pivots(pivots, point):
    if point is None: return False
    if pivots and point['kind']==pivots[-1]['kind']:
        old=pivots[-1]
        better=point['price']>old['price'] if point['kind']=='H' else point['price']<old['price']
        if better:
            pivots[-1]=point
            return True
        return False
    pivots.append(point)
    if len(pivots)>48: del pivots[:len(pivots)-48]
    return True


def _wave40_first_three(p):
    """Validate only W0->W1->W2->W3, which are known before W4 begins.

    Price percentages are exploratory defaults, NOT Elliott laws.
    """
    if len(p)<4:return None
    a,b,c,d=p[-4:]
    if [x['kind'] for x in (a,b,c,d)]!=['L','H','L','H']:return None
    w1=b['price']-a['price']; w3=d['price']-c['price']
    if w1<=0 or w3<=0 or a['price']<=0:return None
    r2=(b['price']-c['price'])/w1
    if not (.20<=r2<=.80 and c['price']>a['price'] and d['price']>b['price']*1.003):return None
    if w1/a['price']*100<1.5 or w3/c['price']*100<1.5:return None
    if (d['price']/a['price']-1)*100<5.0:return None
    # The third leg's length is not declared "never-shortest" before wave 5 exists.
    return {'w0':dict(a),'w1':dict(b),'w2':dict(c),'w3':dict(d),
            'w1_gain_pct':round(_price_pct(b['price'],a['price']),4),
            'w2_retrace_pct':round(r2*100,4),
            'w3_gain_pct':round(_price_pct(d['price'],c['price']),4),
            'w3_beyond_w1_pct':round(_price_pct(d['price'],b['price']),4),
            'total_w0_w3_pct':round(_price_pct(d['price'],a['price']),4)}


def _wave40_outcome(rows, si, fee, slip, w3price):
    ei=_next_entry(rows,si)
    normal=_exit_sim(rows,ei,fee,slip,.5)
    normal03=_exit_sim(rows,ei,fee,slip,.3)
    split=_exit_split_be_sim(rows,ei,fee,slip)
    ret={}
    for k,v in normal.items():ret['tp05_'+k]=v
    for k,v in normal03.items():ret['tp03_'+k]=v
    for k,v in split.items():ret['split_'+k]=v
    if ei is not None:
        b=rows[ei]; entry=b['o'];stop=entry*.993
        side=(fee+slip)/100
        tp=entry*(1+side)*1.005/(1-side)
        ret.update(first5_data_ok=True,first5_low_pct=round(_price_pct(b['l'],entry),4),
                   first5_high_pct=round(_price_pct(b['h'],entry),4),
                   first5_sl=bool(normal.get('bars_held')==1 and str(normal.get('exit','')).startswith('SL')),
                   first5_touch_both=bool(b['l']<=stop and b['h']>=tp),
                   future_high_touched_w3_before_exit=(normal['mfe_high_pct']>=_price_pct(w3price,entry) if normal.get('status')=='READY' else None))
    # Independent post-entry wave-5 diagnostic, even if a trading simulation
    # has already exited. This is NOT a realizable PnL or a BUY-time input.
    if ei is None:
        ret['wave5_60m_data_ok']=False
        ret['wave5_w3_high_reached_60m']=None
    else:
        section=rows[ei:ei+12]
        ok=(len(section)==12 and all(section[k]['t']-section[k-1]['t']==FIVE for k in range(1,len(section))))
        ret['wave5_60m_data_ok']=ok
        ret['wave5_w3_high_reached_60m']=(max(z['h'] for z in section)>=w3price) if ok else None
        ret['wave5_max_high_pct_60m']=round(_price_pct(max(z['h'] for z in section),entry),4) if ok else None
        ret['wave5_w3_high_price']=round(w3price,10)
        ret['wave5_net_if_w3_reached']=round(_net_pct(entry,w3price,fee,slip),4)
    return ret


def analyze_wave40(rows,day,market,fee=.25,slip=0):
    """Real-time-observable W4 early and W4 confirmed entry hypotheses.

    A W3 anchor is a completed 2-left/2-right pivot; a W4 low is tentative in
    early mode and right-2-bar confirmed in confirmed mode.  No price at/after
    entry is used to decide signal eligibility; only outcomes see future bars.
    """
    if not rows:return []
    tz=rows[0]['t'].tzinfo
    start=datetime.combine(day,dt_time.min,tzinfo=tz)
    end=start+timedelta(days=1)
    first=next((i for i,x in enumerate(rows) if x['t']>=start),len(rows))
    last=next((i for i,x in enumerate(rows) if x['t']>=end),len(rows))
    pivots=[];active=None;events=[];seen_w3=set();cash=0.0

    def field_signal(evt,method,i,low_i,low_price,r4,nowcash):
        if evt.get(method+'_signal_at'):return
        evt[method+'_signal_at']=(rows[i]['t']+FIVE).isoformat()
        evt[method+'_signal_bar_at']=rows[i]['t'].isoformat()
        evt[method+'_score']=min(100,max(0,int(65+min(15,evt['total_w0_w3_pct']*1.2)
            + (8 if .25<=r4<=.5 else 0)
            + (5 if _sma(rows,i,20) and rows[i]['c']>_sma(rows,i,20) else 0))))
        evt[method+'_w4_low_at']=rows[low_i]['t'].isoformat()
        evt[method+'_w4_low_price']=round(low_price,10)
        evt[method+'_w4_retrace_pct']=round(r4*100,4)
        evt[method+'_w4_bars']=low_i-evt['_w3i']
        evt[method+'_w4_stop_dist_pct']=round(_price_pct(rows[i]['c'],low_price),4)
        evt[method+'_day_trade_krw_at_signal']=round(nowcash,2)
        evt[method+'_signal_close']=rows[i]['c']
        details=_wave40_outcome(rows,i,fee,slip,evt['w3_price'])
        evt.update({method+'_'+k:v for k,v in details.items()})

    for i in range(len(rows)):
        b=rows[i]
        if b['t']>=end:break
        if b['t']>=start:cash+=b['cash']
        p=_wave40_pivot(rows,i)
        if p:
            _wave40_update_pivots(pivots,p)
            if p['kind']=='H':
                pattern=_wave40_first_three(pivots)
                if pattern and pattern['w3']['i'] not in seen_w3:
                    seen_w3.add(pattern['w3']['i'])
                    w0,w1,w2,w3=(pattern[x] for x in ('w0','w1','w2','w3'))
                    # No non-contiguous entire wave permitted (except 2h prehistory
                    # won't change the KST clock); no forward selection.
                    if all(rows[k]['t']-rows[k-1]['t']==FIVE for k in range(w0['i']+1,i+1)):
                        evt={'market':market,'date':day.isoformat(),
                             'wave40_event_id':f"{market}|{rows[w3['i']]['t'].isoformat()}",
                             '_w3i':w3['i'],'w0_at':rows[w0['i']]['t'].isoformat(),'w0_price':round(w0['price'],10),
                             'w1_at':rows[w1['i']]['t'].isoformat(),'w1_price':round(w1['price'],10),
                             'w2_at':rows[w2['i']]['t'].isoformat(),'w2_price':round(w2['price'],10),
                             'w3_at':rows[w3['i']]['t'].isoformat(),'w3_price':round(w3['price'],10),
                             'w3_confirmed_at':(rows[i]['t']+FIVE).isoformat(),
                             **{k:v for k,v in pattern.items() if k not in ('w0','w1','w2','w3')},
                             'wave40_early_signal_at':'','wave40_confirm_signal_at':'',
                             'wave40_early_tp05_status':'NO_SIGNAL','wave40_confirm_tp05_status':'NO_SIGNAL'}
                        active={'e':evt,'w3_i':w3['i'],'low_i':None,'low':None,
                                'confirmed_i':None,'confirmed_price':None,'invalid':False}
                        events.append(evt)
            elif active and p['kind']=='L' and p['i']>active['w3_i']:
                if active['confirmed_i'] is None:
                    active['confirmed_i']=p['i']; active['confirmed_price']=p['price']

        if active is None or b['t']<start:continue
        a=active;evt=a['e'];w3i=a['w3_i']
        if i<=w3i:continue
        # Candles after W3 must be contiguous; missing bars discard active setup.
        if i>0 and rows[i]['t']-rows[i-1]['t']!=FIVE:
            active=None;continue
        since=i-w3i
        if since>18 or b['t'].time()>=dt_time(22,15):
            active=None;continue
        if a['low'] is None or b['l']<a['low']:
            a['low']=b['l'];a['low_i']=i
        w3price=evt['w3_price'];w1price=evt['w1_price'];w2price=evt['w2_price']
        leg=w3price-w2price
        if leg<=0:active=None;continue
        retr=(w3price-a['low'])/leg
        if a['low']<=w1price or retr>.70 or b['c']<=w1price:
            # May already have had an early signal; never retroactively remove it.
            active=None;continue
        if a['confirmed_i'] is not None and a['low_i']>a['confirmed_i']:
            # A new lower low invalidates the *confirmed* W4 pivot.
            a['confirmed_i']=None;a['confirmed_price']=None
        ma5=_sma(rows,i,5)
        is_recover= (ma5 is not None and i>=1 and b['c']>rows[i-1]['c'] and
                     b['c']>=ma5*.998 and b['c']<w3price*.997 and
                     i>a['low_i'] and .18<=retr<=.70)
        if b['t']>=start and is_recover and 2<=since<=14 and not evt.get('wave40_early_signal_at'):
            field_signal(evt,'wave40_early',i,a['low_i'],a['low'],retr,cash)
        if (b['t']>=start and is_recover and a['confirmed_i'] is not None and
            a['confirmed_i']==a['low_i'] and i>=a['confirmed_i']+2 and
            i<=a['confirmed_i']+8 and not evt.get('wave40_confirm_signal_at')):
            field_signal(evt,'wave40_confirm',i,a['confirmed_i'],a['confirmed_price'],retr,cash)
        # If W3 high has already been decisively reclaimed, the 4th-wave entry
        # window is over; do not label it retroactively as a wave-4 buy.
        if b['c']>=w3price:active=None
    for evt in events:evt.pop('_w3i',None)
    return events



def register_daytrade_review(app,deps):
    """Only research routes; existing / and live app.py behavior stay unchanged."""
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache={'at':0.,'sorted':[],'markets':[],'results':{}}
    lock=Lock()

    @app.get('/v2-core-review')
    @app.get('/backtest-daytrade')
    def core40_page():
        return Response(CORE_PAGE,mimetype='text/html; charset=utf-8')

    @app.get('/api/v2core/universe')
    def core40_universe():
        try:
            with lock:
                if time.monotonic()-cache['at']<600 and cache['sorted']:
                    return jsonify(ok=True,version=VERSION,items=cache['sorted'],cached=True)
            markets=sorted(set(x for x in get_markets() if isinstance(x,str) and MARKET_PATTERN.fullmatch(x)))
            if not markets:
                raise ValueError('현재 KRW 거래종목을 불러올 수 없습니다')
            ranking={}
            for off in range(0,len(markets),100):
                try:
                    raw=api_get('/v1/ticker',{'markets':','.join(markets[off:off+100])})
                    if isinstance(raw,list):
                        for item in raw:
                            if isinstance(item,dict) and item.get('market') in markets:
                                ranking[item['market']]=float(item.get('acc_trade_price_24h') or 0)
                except Exception:
                    pass # fallback alphabetical, do not silently drop markets
            ranked=sorted(markets,key=lambda m:(-ranking.get(m,0),m))
            with lock:
                cache.update(at=time.monotonic(),sorted=ranked,markets=markets)
            return jsonify(ok=True,version=VERSION,items=ranked,listed=len(markets),cached=False,
                           warning='상위 순위는 현재 24시간 거래대금이며 과거 순위가 아님')
        except Exception as e:
            return jsonify(ok=False,error=f'{type(e).__name__}: {e}'[:180]),400

    @app.post('/api/v2core/wave40/analyze')
    def core40_analyze():
        try:
            raw=request.get_json(silent=True)
            cfg=_check_cfg(raw,kst)
            market=raw.get('market')
            if not isinstance(market,str) or not MARKET_PATTERN.fullmatch(market):
                raise ValueError('KRW 거래종목 코드가 잘못됐습니다')
            with lock:
                is_valid = market in cache['markets'] if cache['markets'] and time.monotonic()-cache['at']<600 else None
            if is_valid is False or (is_valid is None and market not in get_markets()):
                raise ValueError('현재 KRW 거래종목 목록에 없는 코인')
            key=(market,cfg['day'],cfg['fee'],cfg['slip'])
            with lock:
                prev=cache['results'].get(key)
            if prev is not None and time.monotonic()-prev[0]<600:
                return jsonify(ok=True,version=VERSION,cached=True,result=prev[1])
            rows, fetch_info=fetch_5m(api_get,market,cfg['day'],kst)
            report=analyze_pullbacks(rows,cfg['day'],market,cfg['fee'],cfg['slip'])
            report['wave_events']=analyze_wave40(rows,cfg['day'],market,cfg['fee'],cfg['slip'])
            report['coverage']['wave3_patterns']=len(report['wave_events'])
            report['coverage']['wave4_early_signals']=sum(bool(e['wave40_early_signal_at']) for e in report['wave_events'])
            report['coverage']['wave4_confirm_signals']=sum(bool(e['wave40_confirm_signal_at']) for e in report['wave_events'])
            report['coverage'].update(fetch_info)
            with lock:
                if len(cache['results'])>1000:
                    cache['results'].clear()
                cache['results'][key]=(time.monotonic(),report)
            return jsonify(ok=True,version=VERSION,cached=False,result=report)
        except Exception as e:
            return jsonify(ok=False,error=f'{type(e).__name__}: {e}'[:200]),400
