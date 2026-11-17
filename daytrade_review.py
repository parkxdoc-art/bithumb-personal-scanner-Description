"""V2 CORE.39 LIGHT — CORE.36 unchanged BUY selection with box-break diagnostics.

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

VERSION = 'V2.CORE.39-LIGHT-BOX-STRUCTURE-DIAG'
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

CORE_PAGE = '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>V2 CORE.39 LIGHT · 눌림 박스 구조 진단</title><style>\n:root{color-scheme:dark;--bg:#0b1020;--panel:#151e31;--edge:#2b3954;--fg:#ecf2ff;--sub:#aab8d0;--blue:#87a4ff;--good:#4ade80;--bad:#ff8797}\n*{box-sizing:border-box}body{background:var(--bg);color:var(--fg);font:14px/1.55 system-ui,"Noto Sans KR",sans-serif;margin:0}main{max-width:1030px;margin:auto;padding:18px 13px 70px}\nh1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:0 0 11px}h3{font-size:14px;margin:14px 0 6px}p{margin:6px 0 12px}a{color:#b3c8ff}.muted{color:var(--sub);font-size:12px}.panel{border:1px solid var(--edge);background:var(--panel);border-radius:14px;padding:16px;margin:13px 0}\n.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:9px}.field{display:flex;flex-direction:column;gap:4px}label{font-size:12px;color:var(--sub)}input,select,button{width:100%;padding:11px;background:#111a2d;border:1px solid #465775;border-radius:8px;color:var(--fg);font:inherit}\nbutton{background:#3765df;font-weight:650;cursor:pointer}button.secondary{background:#273852;border-color:#415470}button:disabled{opacity:.4;cursor:default}.controls{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.controls button{width:auto;flex:1;min-width:105px}\n.kpis{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.kpi{border:1px solid var(--edge);border-radius:9px;padding:13px}.kpi .n{font-size:22px;font-weight:750}.good{color:var(--good)}.bad{color:var(--bad)}.warning{color:#ffd483}\n.tablewrap{overflow:auto;max-width:100%}table{border-collapse:collapse;width:100%;min-width:620px}th,td{font-size:12px;padding:9px;border-bottom:1px solid var(--edge);white-space:nowrap;text-align:right}th:first-child,td:first-child{text-align:left}th{color:#c3d1ec}tr:last-child td{border:0}.note{background:#0c172c;color:#b8c6dd;border-radius:9px;padding:12px;font-size:12px}.pill{display:inline-block;background:#283958;border:1px solid #4c6288;padding:2px 6px;border-radius:5px;font-size:11px}\nprogress{width:100%;height:12px}details{margin:10px 0}summary{cursor:pointer;padding:10px 0;font-weight:650}\n@media(max-width:640px){main{padding:13px 9px 44px}.panel{padding:12px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.field{min-width:0}h1{font-size:20px}button{padding:10px 8px}}\n</style></head><body><main>\n<header><h1>🧪 V2 CORE.39 LIGHT · 눌림 박스 구조 진단</h1><p class="muted">자정 기준가격 제거 · 00시 이후 실제 상승파동 +5% · 5분봉 SMA5/SMA20 · 연구 전용 (자동 주문 없음)</p><a href="/">← 실전 스캐너로 돌아가기</a></header>\n<section class="panel"><h2>① 이번에 찾는 눌림 자리</h2>\n<p><b>최근 2시간 저점 대비 +5% 상승</b>하며 5이평 위에 머문 코인이, 5이평 아래로 잠깐 밀린 뒤 <b>20이평 부근에서 2~16개 5분봉 동안 버티는 눌림·횡보</b>를 찾습니다.</p>\n<p>진입 방식 비교: <b>A · 첫 5이평 이탈</b> / <b>B · 5이평 재회복</b> / <b>C · 눌림 박스 상단 돌파</b>. 모두 완성된 분봉 이후 다음 시가 진입을 가정합니다.</p>\n<div class="note">+5%는 자정 대비가 아닙니다. 00시부터 검사하되 각 시점 직전 최대 24개 5분봉의 실제 저점을 기준으로 계산합니다. 5이평은 SMA5, 추세 지지 참고는 SMA20입니다. 이탈 후 20이평을 크게 밑돌거나 눌림 깊이가 3.5%를 초과하면 회복 신호가 무효화됩니다. <b>승률이 검증된 진입 규칙이 아닙니다.</b></div></section>\n<section class="panel"><h2>🔎 CORE.39 진단 기록 (매수조건 변경 없음)</h2><p>기존 CORE.36의 진입, 품질 점수, 선정 순서, 순익 +0.5% 및 손절 -0.7%를 <b>한 글자도 변경하지 않고</b> 수익 거래와 즉시 손절 거래를 비교합니다.</p><div class="note">돌파 전 박스 폭 = 첫 눌림봉부터 돌파 바로 전봉까지의 고가/저가 범위(돌파봉 제외). 돌파봉 몸통 상승률 · 윗꼬리 비율 · 종가 위치 · 상단 초과폭 · 최근 박스 최대 3봉 대비 거래량을 저장합니다. 진입 후 첫 봉 손절·익절과 동봉 TP/SL 양쪽 터치 여부는 <b>사후 결과 라벨</b>로만 표시하며 BUY 필터에 넣지 않습니다.</div></section>\n<section class="panel"><h2>② 검사 설정</h2><div class="grid">\n<div class="field"><label for="endDay">마지막 날짜 (KST·지난 날짜)</label><input id="endDay" type="date"></div>\n<div class="field"><label for="days">날짜 수</label><select id="days"><option value="1" selected>1일</option><option value="3">3일</option><option value="5">5일</option></select></div>\n<div class="field"><label for="scope">검사 범위</label><select id="scope"><option value="all" selected>전체 KRW</option><option value="40">현재 거래대금 상위 40</option><option value="80">현재 거래대금 상위 80</option></select></div>\n<div class="field"><label for="fee">편도 수수료 (%)</label><input id="fee" type="number" min="0" max="0.5" step="0.01" value="0.25"></div>\n<div class="field"><label for="slip">편도 슬리피지 (%)</label><input id="slip" type="number" min="0" max="0.5" step="0.01" value="0"></div>\n<div class="field"><label for="method">진입 비교 방식</label><select id="method"><option value="box" selected>C · 눌림 박스 상단 돌파</option><option value="reclaim">B · SMA5 재회복</option><option value="dip">A · SMA5 첫 이탈 (비교용)</option></select></div>\n<div class="field"><label for="target">익절 순수익</label><select id="target"><option value="05" selected>+0.5%</option><option value="03">+0.3%</option></select></div>\n<div class="field"><label for="cap">하루 최대 선정</label><select id="cap"><option value="3">3건</option><option value="4" selected>4건</option></select></div>\n<div class="field"><label for="minScore">신호 품질 점수 (시점 기준)</label><select id="minScore"><option value="0">전체 (비교용)</option><option value="50">50점 이상</option><option value="65" selected>65점 이상</option><option value="80">80점 이상</option></select></div>\n<div class="field"><label for="minCash">신호 당시 당일 거래대금</label><select id="minCash"><option value="0">제한 없음</option><option value="500000000" selected>5억 원 이상</option></select></div>\n<div class="field"><label for="selectMode">하루 4건 분산 선정</label><select id="selectMode"><option value="slot" selected>6시간대별 최대 1건</option><option value="first">전체 하루 시간순 최대 4건</option></select></div>\n</div><div class="controls"><button id="run">CORE.39 구조 진단 검사</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="csv" class="secondary" disabled>CORE.39 진단 CSV 다운로드</button></div>\n<progress id="bar" value="0" max="1"></progress><p id="status" class="muted" aria-live="polite">검사 전</p>\n<div class="note">상위 40·80종목은 현재 24시간 거래대금 기준이며, 당시 순위로 소급한 것은 아닙니다. 4개 작업을 동시에 조회합니다. 자동 주문은 하지 않습니다. 전체 검사 완료 전에도 부분 CSV를 저장할 수 있습니다. 검사 종료 후 파일을 보내주면 결과를 비교합니다. 시간대 분산은 사전 규칙이며 나중에 오른 코인만 고르는 방식이 아닙니다.</div></section>\n<section class="panel"><h2>③ 하루 0~4건 검증</h2><div class="kpis">\n<div class="kpi"><span class="muted">완료 종목·날짜</span><div id="kDone" class="n">—</div></div>\n<div class="kpi"><span class="muted">5이평 첫 눌림</span><div id="kDips" class="n">—</div></div>\n<div class="kpi"><span class="muted">SMA5 회복 / 박스 상단 돌파</span><div id="kBox" class="n">—</div></div>\n<div class="kpi"><span class="muted">API·자료 오류</span><div id="kErrors" class="n">—</div></div>\n<div class="kpi"><span class="muted">선정 거래</span><div id="kSelected" class="n">—</div></div>\n<div class="kpi"><span class="muted">확정 거래 평균 순익</span><div id="kNet" class="n">—</div></div>\n<div class="kpi"><span class="muted">선정 C형 · 첫 5분봉 손절</span><div id="kSL5" class="n bad">—</div></div>\n<div class="kpi"><span class="muted">선정 C형 · 첫 봉 TP·SL 동시 터치</span><div id="kBoth" class="n warning">—</div></div>\n</div>\n<h3>선정된 거래 (손절이 크면 작은 익절 여러 번을 상쇄합니다)</h3><div class="tablewrap"><table><thead><tr><th>날짜·종목</th><th>진입 신호</th><th>상승파동</th><th>품질</th><th>돌파 전 박스폭</th><th>돌파봉 몸통</th><th>돌파봉 윗꼬리</th><th>상단 초과폭</th><th>상대 거래량</th><th>첫 5분봉</th><th>청산</th><th>순수익</th></tr></thead><tbody id="selectedTable"><tr><td colspan="12">검사 전</td></tr></tbody></table></div>\n<h3>날짜별 성과</h3><div class="tablewrap"><table><thead><tr><th>날짜</th><th>적격 후보</th><th>선정</th><th>확정</th><th>수익</th><th>손실</th><th>평균 순익</th></tr></thead><tbody id="dailyTable"><tr><td colspan="7">검사 전</td></tr></tbody></table></div></section>\n<section class="panel"><h2>④ A · 이탈 / B · 재회복 / C · 박스 상단 돌파</h2>\n<div class="tablewrap"><table><thead><tr><th>진입 방식</th><th>신호 수</th><th>청산 확정</th><th>순이익</th><th>평균 순수익</th></tr></thead><tbody id="compareTable"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<details><summary>전체 눌림 자세히 보기 (화면 100건, CSV 전체)</summary><div class="tablewrap"><table><thead><tr><th>날짜·종목</th><th>눌림 시각</th><th>상승파동</th><th>눌림 깊이</th><th>SMA5 회복</th><th>박스 돌파</th><th>C 품질점수</th><th>C 순익</th></tr></thead><tbody id="allTable"><tr><td colspan="8">검사 전</td></tr></tbody></table></div></details>\n<p class="muted">비교표는 전체 신호 기준, ③은 하루 최대 4건 제한 기준입니다. 원본 BTC 흐름·시장 변동성과 상관없이 이번에는 가격 구조 하나를 먼저 검증합니다.</p></section>\n<section class="panel"><h2>⑤ 검증상 주의</h2><p>이 전략은 과거 데이터로 검증하는 가설입니다. 5이평 이탈이 건강한 눌림인지 추세 종료인지는 사전 확정할 수 없습니다. 종가 신호는 완성 후에만 확인하고 다음 분봉 시가에 체결을 가정합니다.</p><p class="muted">수수료 편도 0.25%·슬리피지 0% 기본, 순익 목표 +0.3/+0.5%, 고정 손절 -0.7% (수수료 포함 순손실 약 -1.2%), 60분 시간청산. 동일 봉 TP/SL 동시 충족은 보수적으로 손절 우선. 결측봉 청산 미확정은 무리하게 보충하지 않습니다. 실제 슬리피지·호가·주문 체결은 반영되지 않습니다.</p></section>\n<script>\n \'use strict\';\nconst $=id=>document.getElementById(id);\nconst CODE_VERSION=\'V2.CORE.39-LIGHT-BOX-STRUCTURE-DIAG\';\nconst esc=s=>String(s??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nconst fmt=(v,d=3)=>v===null||v===undefined||!Number.isFinite(Number(v))?\'—\':Number(v).toFixed(d)+\'%\';\nconst num=v=>Number(v||0).toLocaleString(\'ko-KR\');\nconst todayKST=()=>new Date(Date.now()+9*3600000).toISOString().slice(0,10);\nfunction daysEnding(end,n){const a=[];for(let i=n-1;i>=0;i--){const d=new Date(end+\'T00:00:00Z\');d.setUTCDate(d.getUTCDate()-i);a.push(d.toISOString().slice(0,10));}return a;}\n$(\'endDay\').value=new Date(Date.now()+9*3600000-86400000).toISOString().slice(0,10);\n$(\'endDay\').max=$(\'endDay\').value;\nfunction cfg(){const end=$(\'endDay\').value,n=Number($(\'days\').value),scope=$(\'scope\').value,fee=Number($(\'fee\').value),slip=Number($(\'slip\').value);\n if(!end||end>=todayKST())throw Error(\'종료된 날짜를 선택해 줘\');\n if(![1,3,5].includes(n)||![\'all\',\'40\',\'80\'].includes(scope))throw Error(\'범위 설정 오류\');\n if([fee,slip].some(x=>!Number.isFinite(x)||x<0||x>.5))throw Error(\'비용 입력은 0~0.5%\');\n return {days:daysEnding(end,n),scope,fee,slip};}\nfunction policy(){return {method:$(\'method\').value,target:$(\'target\').value,cap:Number($(\'cap\').value),minScore:Number($(\'minScore\').value),minCash:Number($(\'minCash\').value),selectMode:$(\'selectMode\').value};}\nlet state=null,running=false,paused=false;\nconst KEYSET=[\'endDay\',\'days\',\'scope\',\'fee\',\'slip\'];\nfunction api(url,payload){return fetch(url,{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(payload)}).then(async r=>{const j=await r.json().catch(()=>({error:\'응답을 읽을 수 없음\'}));if(!r.ok||!j.ok)throw Error(j.error||\'HTTP \'+r.status);return j;});}\nfunction allEvents(){return state?state.results.flatMap(r=>r.events||[]):[];}\nfunction pickRows(){\n if(!state)return {selected:[],byDay:[],nCandidates:0};\n const p=policy(),key=p.method+\'_tp\'+p.target;\n const qual=allEvents().filter(e=>e[p.method+\'_signal_at\'] && Number(e[p.method===\'dip\'?\'day_trade_krw_at_dip\':p.method+\'_day_trade_krw_at_signal\'])>=p.minCash && Number(e[p.method===\'dip\'?\'score_dip\':p.method+\'_score\'])>=p.minScore)\n .map(e=>({e,at:e[p.method+\'_signal_at\'],rkey:key,method:p.method,score:Number(e[p.method===\'dip\'?\'score_dip\':p.method+\'_score\'])}))\n .sort((a,b)=>a.at.localeCompare(b.at)||b.score-a.score||a.e.market.localeCompare(b.e.market));\n const selected=[],byDay=[];\n for(const day of state.days){const pool=qual.filter(x=>x.e.date===day),chosen=[],usedSlots=new Set(),usedCoins=new Set();let busyUntil=\'\';\n  for(const item of pool){if(chosen.length>=p.cap)break;\n   const slot=Math.floor(Number(item.at.slice(11,13))/6);\n   if(p.selectMode===\'slot\'&&usedSlots.has(slot))continue;\n   if(usedCoins.has(item.e.market))continue;\n   if(busyUntil&&item.at<busyUntil)continue;\n   chosen.push(item);selected.push(item);usedSlots.add(slot);usedCoins.add(item.e.market);\n   const st=item.e[key+\'_status\'];busyUntil=st===\'READY\'?(item.e[key+\'_exit_at\']||item.at):day+\'T23:59:59+09:00\';\n  }\n  const ready=chosen.filter(x=>x.e[key+\'_status\']===\'READY\');const wins=ready.filter(x=>Number(x.e[key+\'_net_pct\'])>0);\n  byDay.push({date:day,candidates:pool.length,selected:chosen.length,ready:ready.length,wins:wins.length,loss:ready.length-wins.length,avg:ready.length?ready.reduce((s,x)=>s+Number(x.e[key+\'_net_pct\']),0)/ready.length:null});\n }\n return {selected,byDay,nCandidates:qual.length};\n}\nfunction rows(id,html,n){$(id).innerHTML=html||`<tr><td colspan="${n}">해당 결과 없음</td></tr>`;}\nfunction render(){if(!state)return;const ev=allEvents(),p=policy(),pick=pickRows(),key=p.method+\'_tp\'+p.target;\n const ready=pick.selected.filter(x=>x.e[key+\'_status\']===\'READY\'),avg=ready.length?ready.reduce((s,x)=>s+Number(x.e[key+\'_net_pct\']),0)/ready.length:null;\n $(\'kDone\').textContent=num(state.done)+\' / \'+num(state.tasks.length);$(\'kDips\').textContent=num(ev.length);\n $(\'kBox\').textContent=ev.filter(e=>e.reclaim_signal_at).length+\' / \'+ev.filter(e=>e.box_signal_at).length;\n $(\'kErrors\').textContent=num(state.errors.length+state.results.filter(r=>r.coverage.reason).length);\n $(\'kSelected\').textContent=num(pick.selected.length);$(\'kNet\').textContent=fmt(avg);$(\'kNet\').className=\'n \'+(avg!==null?(avg>=0?\'good\':\'bad\'):\'\');\n $(\'kSL5\').textContent=num(pick.selected.filter(x=>x.method===\'box\'&&x.e.box_first5_sl).length);$(\'kBoth\').textContent=num(pick.selected.filter(x=>x.method===\'box\'&&x.e.box_first5_touch_both).length);\n $(\'bar\').max=Math.max(1,state.tasks.length);$(\'bar\').value=state.done;\n rows(\'selectedTable\',pick.selected.map(x=>{const e=x.e,v=e[key+\'_net_pct\'];const f5=x.method===\'box\'?(e.box_first5_sl?\'첫 봉 SL\':e.box_first5_tp?\'첫 봉 TP\':e.box_first5_data_ok?\'첫 봉 유지\':\'미확인\'):\'C형 전용\';return `<tr><td>${esc(e.date)} ${esc(e.market)}</td><td>${esc(x.at.slice(11,16))}</td><td>${fmt(e.pump_gain_pct)}</td><td>${x.score}</td><td>${fmt(e.box_pre_width_pct)}</td><td>${fmt(e.box_breakout_body_change_pct)}</td><td>${fmt(e.box_breakout_upper_wick_pct)}</td><td>${fmt(e.box_breakout_over_pre_upper_pct)}</td><td>${e.box_breakout_volume_vs_pre3==null?\'—\':Number(e.box_breakout_volume_vs_pre3).toFixed(2)+\'배\'}</td><td class="${e.box_first5_sl?\'bad\':\'\'}">${f5}${e.box_first5_touch_both?\' · 양방향\':\'\'}</td><td>${esc(e[key+\'_exit\']||e[key+\'_status\']||\'—\')}</td><td class="${v===undefined?\'\':v>=0?\'good\':\'bad\'}">${fmt(v)}</td></tr>`;}).join(\'\'),12);\n rows(\'dailyTable\',pick.byDay.map(d=>`<tr><td>${esc(d.date)}</td><td>${d.candidates}</td><td>${d.selected}</td><td>${d.ready}</td><td class="good">${d.wins}</td><td class="bad">${d.loss}</td><td>${fmt(d.avg)}</td></tr>`).join(\'\'),7);\n rows(\'compareTable\',[\'dip\',\'reclaim\',\'box\'].map(m=>{const k=m+\'_tp\'+p.target;const list=ev.filter(e=>e[m+\'_signal_at\']);const rd=list.filter(e=>e[k+\'_status\']===\'READY\');const win=rd.filter(e=>Number(e[k+\'_net_pct\'])>0).length;const mean=rd.length?rd.reduce((a,e)=>a+Number(e[k+\'_net_pct\']),0)/rd.length:null;return `<tr><td>${({dip:\'A · 첫 5이평 이탈\',reclaim:\'B · 5이평 회복\',box:\'C · 박스 상단 돌파\'})[m]}</td><td>${list.length}</td><td>${rd.length}</td><td>${win}</td><td>${fmt(mean)}</td></tr>`}).join(\'\'),5);\n rows(\'allTable\',ev.slice(0,100).map(e=>`<tr><td>${esc(e.date)} ${esc(e.market)}</td><td>${esc(e.dip_signal_at?.slice(11,16))}</td><td>${fmt(e.pump_gain_pct)}</td><td>${fmt(e.dip_depth_pct)}</td><td>${esc(e.reclaim_signal_at?.slice(11,16)||\'미확인\')}</td><td>${esc(e.box_signal_at?.slice(11,16)||\'미확인\')}</td><td>${e.box_score??\'—\'}</td><td>${fmt(e[\'box_tp\'+p.target+\'_net_pct\'])}</td></tr>`).join(\'\'),8);\n}\nfunction status(t){$(\'status\').textContent=t;}\nfunction setRunning(x){running=x;$(\'run\').disabled=x;$(\'pause\').disabled=!x;KEYSET.forEach(id=>$(id).disabled=x);}\n$(\'pause\').onclick=()=>{paused=true;status(\'일시 중지 요청 · 진행 중인 작업만 완료합니다\');};\n[\'method\',\'target\',\'cap\',\'minScore\',\'minCash\',\'selectMode\'].forEach(id=>$(id).addEventListener(\'change\',render));\n$(\'run\').onclick=async()=>{if(running)return;try{const c=cfg(),key=JSON.stringify(c);\n if(!state||state.key!==key||state.cursor===state.tasks.length){const r=await fetch(\'/api/v2core/universe\').then(x=>x.json());if(!r.ok||!Array.isArray(r.items))throw Error(r.error||\'KRW 목록 조회 실패\');const markets=c.scope===\'all\'?r.items:r.items.slice(0,Number(c.scope));const tasks=c.days.flatMap(date=>markets.map(market=>({date,market})));state={key,...c,tasks,cursor:0,done:0,results:[],errors:[]};}\n paused=false;setRunning(true);render();$(\'csv\').disabled=false;let last=0;\n async function worker(){while(!paused&&state.cursor<state.tasks.length){const task=state.tasks[state.cursor++];try{const out=await api(\'/api/v2core/pullback39/analyze\',{...task,fee:state.fee,slip:state.slip});state.results.push(out.result);}catch(e){state.errors.push({...task,error:String(e.message||e)});}finally{state.done++;if(state.done-last>=8||state.done===state.tasks.length){last=state.done;render();status(`${state.done}/${state.tasks.length} 검사 중 · 오류 ${state.errors.length}`);}}}}\n await Promise.all(Array.from({length:Math.min(4,state.tasks.length)},()=>worker()));render();status(paused?\'일시 중지 · 다시 검사하면 같은 설정부터 이어서 진행\':`완료 ${state.done}/${state.tasks.length} · 눌림 ${allEvents().length}건 · CSV 저장 가능`);\n }catch(e){status(\'⚠️ \'+String(e.message||e));}finally{setRunning(false);$(\'csv\').disabled=!state||(!state.results.length&&!state.errors.length);}};\nfunction csvVal(v){let s=String(v??\'\');if(/^[=+@-]/.test(s)&&isNaN(Number(s)))s="\'"+s;return \'"\'+s.replace(/"/g,\'""\')+\'"\';}\n$(\'csv\').onclick=()=>{if(!state)return;const pick=pickRows(),chosen=new Map(pick.selected.map(x=>[x.e.date+\'|\'+x.e.market+\'|\'+x.e.dip_signal_at,x]));\n const p=policy();const base={version:CODE_VERSION,date_from:state.days[0],date_to:state.days.at(-1),scope:state.scope,fee_pct_side:state.fee,slippage_pct_side:state.slip,scan_complete:state.done===state.tasks.length,scan_done:state.done,scan_tasks:state.tasks.length,scan_errors:state.errors.length,selection_method:p.method,selection_mode:p.selectMode,min_score:p.minScore,daily_max:p.cap,target_net_pct:p.target===\'05\'?.5:.3,stop_gross_pct:.7,horizon_min:60,pump_rule:\'PRIOR_24_COMPLETED_5M_LOW_PLUS5_NO_MIDNIGHT_BASE\',sma:\'SMA5_SMA20_5MIN\',simulation:\'NEXT_OPEN_OHLC_STOP_FIRST\',diagnostic:\'PRE_BOX_EXCLUDES_BREAKOUT_POST_ENTRY_FIRST5_LABELS_ONLY\'};\n const out=[];for(const d of pick.byDay)out.push({...base,row_type:\'DAY_SUMMARY\',date:d.date,...d});\n for(const r of state.results){out.push({...base,row_type:\'MARKET_COVERAGE\',date:r.date,market:r.market,...r.coverage});for(const e of r.events){const c=chosen.get(e.date+\'|\'+e.market+\'|\'+e.dip_signal_at);out.push({...base,row_type:\'PULLBACK_EVENT\',date:e.date,market:e.market,selected_for_daily_limit:!!c,selected_entry_method:c?.method||\'\',selected_signal_at:c?.at||\'\',...e});}}\n for(const e of state.errors)out.push({...base,row_type:\'API_ERROR\',...e});\n const cols=[...new Set([\'row_type\',\'version\',\'date\',\'market\',\'selected_for_daily_limit\',\'pump_signal_at\',\'dip_signal_at\',\'reclaim_signal_at\',\'box_signal_at\',\'pump_gain_pct\',\'dip_depth_pct\',\'score_dip\',\'reclaim_score\',\'box_score\',\'dip_tp05_net_pct\',\'reclaim_tp05_net_pct\',\'box_tp05_net_pct\',\'box_pre_width_pct\',\'box_breakout_body_change_pct\',\'box_breakout_upper_wick_pct\',\'box_breakout_over_pre_upper_pct\',\'box_breakout_volume_vs_pre3\',\'box_first5_sl\',\'box_first5_touch_both\',...out.flatMap(x=>Object.keys(x))])];\n const csv=\'\\ufeff\'+[cols.join(\',\'),...out.map(r=>cols.map(c=>csvVal(r[c])).join(\',\'))].join(\'\\r\\n\');const u=URL.createObjectURL(new Blob([csv],{type:\'text/csv;charset=utf-8\'}));const a=document.createElement(\'a\');a.href=u;a.download=`CORE39_BOX_STRUCTURE_DIAG_${state.days[0]}_${state.days.at(-1)}_${state.scope}_KRW.csv`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),3000);};\n\n</script></main></body></html>'


def register_daytrade_review(app,deps):
    """Only research routes; existing / and live app.py behavior stay unchanged."""
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache={'at':0.,'sorted':[],'markets':[],'results':{}}
    lock=Lock()

    @app.get('/v2-core-review')
    @app.get('/backtest-daytrade')
    def core39_page():
        return Response(CORE_PAGE,mimetype='text/html; charset=utf-8')

    @app.get('/api/v2core/universe')
    def core39_universe():
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

    @app.post('/api/v2core/pullback39/analyze')
    def core39_analyze():
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
            report['coverage'].update(fetch_info)
            with lock:
                if len(cache['results'])>1000:
                    cache['results'].clear()
                cache['results'][key]=(time.monotonic(),report)
            return jsonify(ok=True,version=VERSION,cached=False,result=report)
        except Exception as e:
            return jsonify(ok=False,error=f'{type(e).__name__}: {e}'[:200]),400
