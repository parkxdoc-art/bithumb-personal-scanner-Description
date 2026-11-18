"""V2 CORE.42 LIGHT — completed higher timeframe trend diagnostics on CORE36 C signals.

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

VERSION = 'V2.CORE.42-LIGHT-MTF-TREND-DIAG'
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



def _watch41_after_box(bars, box_i, pre_upper, fee, slip):
    """Observational WATCH -> one completed 5-minute confirmation -> next open.

    C signal is known on breakout bar close (box_i end). At that time set WATCH.
    The one following complete bar (box_i+1) may validate the breakout, using
    only its close and SMA5. The next bar (box_i+2) open is earliest BUY fill.
    The rule is deliberately minimal, fixed before observing outcomes:
       confirm close > PRE-breakout box upper and close > SMA5.
    No post-entry outcomes enter the decision.
    """
    fields={
        'watch41_watch_at': (bars[box_i]['t']+FIVE).isoformat(),
        'watch41_confirm_rule':'NEXT_5M_CLOSE_ABOVE_PRE_BOX_UPPER_AND_SMA5',
        'watch41_pre_box_upper': round(pre_upper,10),
        'watch41_confirm_status': 'UNKNOWN',
        'watch41_confirm_reason': '',
        'watch41_confirm_at': '',
        'watch41_signal_at': '',
        'watch41_confirm_close': None,
        'watch41_confirm_sma5': None,
        'watch41_confirm_low': None,
        'watch41_entry_delay_min': 5,
        'watch41_first5_data_ok':False,
        'watch41_first5_sl':False,
        'watch41_first5_tp':False,
        'watch41_first5_touch_both':False,
        'watch41_tp03_status':'UNKNOWN',
        'watch41_tp05_status':'UNKNOWN',
    }
    confirm_i=box_i+1
    if confirm_i>=len(bars) or bars[confirm_i]['t']-bars[box_i]['t']!=FIVE:
        fields['watch41_confirm_reason']='확인봉 누락 또는 5분봉 불연속'
        return fields
    cbar=bars[confirm_i]
    ma5=_sma(bars,confirm_i,5)
    fields.update(watch41_confirm_at=(cbar['t']+FIVE).isoformat(),
                  watch41_confirm_close=round(cbar['c'],10),
                  watch41_confirm_low=round(cbar['l'],10),
                  watch41_confirm_sma5=round(ma5,10) if ma5 is not None else None)
    if ma5 is None:
        fields['watch41_confirm_reason']='확인봉 SMA5 자료 부족'
        return fields
    if not (cbar['c']>pre_upper and cbar['c']>ma5):
        fields.update(watch41_confirm_status='REJECTED',
                      watch41_confirm_reason='박스 상단 또는 SMA5 종가 이탈',
                      watch41_tp03_status='REJECTED',watch41_tp05_status='REJECTED')
        return fields
    fields['watch41_confirm_status']='CONFIRMED'
    fields['watch41_signal_at']=(cbar['t']+FIVE).isoformat()
    # +2 is necessary: confirmation close is not tradable before the next bar.
    ei=_next_entry(bars,confirm_i)
    if ei is None:
        fields['watch41_confirm_reason']='확인 후 진입봉 누락'
        return fields
    for tgt in (.3,.5):
        sim=_exit_sim(bars,ei,fee,slip,tgt)
        prefix='watch41_tp'+str(int(tgt*10)).zfill(2)+'_'
        for k,v in sim.items():fields[prefix+k]=v
    sim={'exit':fields.get('watch41_tp05_exit',''),
         'bars_held':fields.get('watch41_tp05_bars_held')}
    diags=_box_firstbar_diagnostics(bars,confirm_i,sim,fee,slip)
    for k,v in diags.items():
        if k.startswith('box_'):fields['watch41_'+k[4:]]=v
    return fields

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
            e.update(_watch41_after_box(rows,st['box_i'],
                     e['box_pre_upper'],fee,slip))
        if st['reclaim_i'] is not None: coverage['reclaim_setups']+=1
        if st['box_i'] is not None: coverage['box_break_setups']+=1
    coverage['events']=len(events)
    if coverage['contiguous_scan_bars']==0:coverage['reason']='연속 5분봉 부족'
    result['events']=events
    return result

CORE_PAGE = '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>V2 CORE.42 LIGHT · 상위 시간봉 추세 진단</title><style>\n:root{color-scheme:dark;--bg:#0b1020;--panel:#151e31;--edge:#2b3954;--fg:#ecf2ff;--sub:#aab8d0;--blue:#87a4ff;--good:#4ade80;--bad:#ff8797}\n*{box-sizing:border-box}body{background:var(--bg);color:var(--fg);font:14px/1.55 system-ui,"Noto Sans KR",sans-serif;margin:0}main{max-width:1030px;margin:auto;padding:18px 13px 70px}\nh1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:0 0 11px}h3{font-size:14px;margin:14px 0 6px}p{margin:6px 0 12px}a{color:#b3c8ff}.muted{color:var(--sub);font-size:12px}.panel{border:1px solid var(--edge);background:var(--panel);border-radius:14px;padding:16px;margin:13px 0}\n.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:9px}.field{display:flex;flex-direction:column;gap:4px}label{font-size:12px;color:var(--sub)}input,select,button{width:100%;padding:11px;background:#111a2d;border:1px solid #465775;border-radius:8px;color:var(--fg);font:inherit}\nbutton{background:#3765df;font-weight:650;cursor:pointer}button.secondary{background:#273852;border-color:#415470}button:disabled{opacity:.4;cursor:default}.controls{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.controls button{width:auto;flex:1;min-width:105px}\n.kpis{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.kpi{border:1px solid var(--edge);border-radius:9px;padding:13px}.kpi .n{font-size:22px;font-weight:750}.good{color:var(--good)}.bad{color:var(--bad)}.warning{color:#ffd483}\n.tablewrap{overflow:auto;max-width:100%}table{border-collapse:collapse;width:100%;min-width:620px}th,td{font-size:12px;padding:9px;border-bottom:1px solid var(--edge);white-space:nowrap;text-align:right}th:first-child,td:first-child{text-align:left}th{color:#c3d1ec}tr:last-child td{border:0}.note{background:#0c172c;color:#b8c6dd;border-radius:9px;padding:12px;font-size:12px}.pill{display:inline-block;background:#283958;border:1px solid #4c6288;padding:2px 6px;border-radius:5px;font-size:11px}\nprogress{width:100%;height:12px}details{margin:10px 0}summary{cursor:pointer;padding:10px 0;font-weight:650}\n@media(max-width:640px){main{padding:13px 9px 44px}.panel{padding:12px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.field{min-width:0}h1{font-size:20px}button{padding:10px 8px}}\n\n.same{background:#16283a;border-left:3px solid #7ca1f8;padding:11px;border-radius:7px;margin-top:9px}\n.grid.comp{grid-template-columns:repeat(2,minmax(0,1fr))}\n.tablewrap{margin-top:8px}\n</style></head><body><main>\n<header><h1>🧪 V2 CORE.42 LIGHT · 큰 추세 × 눌림 진단 연구</h1><p class="muted">CORE.36 C형 BUY 신호 유지 · 일봉/4시간/1시간 완료봉 분석 · 자동주문 없음</p><a href="/">← 실전 스캐너로 돌아가기</a></header>\n<section class="panel"><h2>① 큰 추세 진단 목표</h2>\n<p>기존 <b>+5% 상승 → SMA5 첫 눌림 → 박스 상단 돌파(C형)</b> 매수 신호와 청산을 보존하면서 <b>일봉·4시간봉·1시간봉의 추세</b>를 당시 마감된 봉만으로 추가 진단합니다. 기존 즉시 BUY와 WATCH 비교도 그대로 남겼습니다.</p>\n<div class="grid comp"><div class="note"><b>A. 기존 즉시 BUY</b><p>돌파 5분봉 마감 시 신호. 바로 <b>다음 5분봉 시가</b>에 진입한 것으로 계산.</p></div><div class="note"><b>B. WATCH → 확인 BUY</b><p>돌파봉 마감 시 WATCH. 다음 5분봉의 <b>종가가 돌파 전 박스 상단과 SMA5보다 모두 높으면</b> 확인, 그 <b>다음 5분봉 시가</b>에 진입. 미충족이면 매수하지 않음.</p></div></div>\n<p class="same">확인 대기 중인 후보는 선정 슬롯을 차지하지 않습니다. 확인이 끝난 후보만 시간순으로 재선정합니다. <b>미래에 수익 난 코인을 사후에 고르지 않습니다.</b> 두 방식은 진입 시각이 달라 수익·손절 결과도 새 진입가로 다시 계산합니다.</p>\n<p class="muted">두 방식 모두 C형 품질 점수는 기존 값 그대로 사용합니다. WATCH 확인은 새로운 수익 확률 점수가 아닙니다. 진입 시뮬레이션은 수수료와 5분봉 OHLC를 이용하며 동일 봉 TP/SL 동시 충족은 손절 우선입니다.</p></section>\n<section class="panel"><h2>② 검사 설정</h2><div class="grid">\n<div class="field"><label for="endDay">마지막 날짜 (KST·지난 날짜)</label><input id="endDay" type="date"></div>\n<div class="field"><label for="days">날짜 수</label><select id="days"><option value="1" selected>1일</option><option value="3">3일</option><option value="5">5일</option></select></div>\n<div class="field"><label for="scope">검사 범위</label><select id="scope"><option value="all" selected>전체 KRW</option><option value="40">현재 거래대금 상위 40</option><option value="80">현재 거래대금 상위 80</option></select></div>\n<div class="field"><label for="fee">편도 수수료 (%)</label><input id="fee" type="number" min="0" max="0.5" step="0.01" value="0.25"></div>\n<div class="field"><label for="slip">편도 슬리피지 (%)</label><input id="slip" type="number" min="0" max="0.5" step="0.01" value="0"></div>\n<div class="field"><label for="target">익절 순수익</label><select id="target"><option value="05" selected>+0.5%</option><option value="03">+0.3%</option></select></div>\n<div class="field"><label for="cap">하루 최대 선정</label><select id="cap"><option value="3">3건</option><option value="4" selected>4건</option></select></div>\n<div class="field"><label for="minScore">C형 신호 품질 점수</label><select id="minScore"><option value="0">전체</option><option value="50">50점 이상</option><option value="65" selected>65점 이상</option><option value="80">80점 이상</option></select></div>\n<div class="field"><label for="minCash">신호 당시 당일 거래대금</label><select id="minCash"><option value="0">제한 없음</option><option value="500000000" selected>5억 원 이상</option></select></div>\n<div class="field"><label for="selectMode">하루 선정 정책 (두 방식에 동일 적용)</label><select id="selectMode"><option value="slot" selected>6시간대별 최대 1건</option><option value="first">전체 하루 시간순 최대 4건</option></select></div>\n</div><div class="controls"><button id="run">CORE.42 상위 추세 진단 시작</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="csv" class="secondary" disabled>CORE.42 추세 CSV 다운로드</button></div>\n<progress id="bar" value="0" max="1"></progress><p id="status" class="muted" aria-live="polite">검사 전</p>\n<div class="note">5분봉 연속 데이터가 없는 종목은 포함되지 않을 수 있습니다. 하단의 검사 완료 수·자료 부족 수를 반드시 확인하세요. 상위 40·80코인은 현재 거래대금 순위로, 과거 순위가 아닙니다. 데이터 조회는 4개 병렬 작업이며 주문을 실행하지 않습니다. 검사 중 중단하면 부분 CSV는 미완료로 표시됩니다.</div></section>\n<section class="panel"><h2>③ 하루 최대 4건 · 정책별 성과 비교</h2>\n<div class="kpis">\n<div class="kpi"><span class="muted">완료 종목·날짜</span><div id="kDone" class="n">—</div></div>\n<div class="kpi"><span class="muted">자료 부족 / API 오류</span><div id="kErrors" class="n warning">—</div></div>\n<div class="kpi"><span class="muted">전체 C 돌파 신호</span><div id="kSignals" class="n">—</div></div>\n<div class="kpi"><span class="muted">WATCH 확인 / 거절 / 미확인</span><div id="kWatch" class="n">—</div></div>\n<div class="kpi"><span class="muted">A · 즉시 BUY 선정 / 첫 봉 손절</span><div id="kABuy" class="n">—</div></div>\n<div class="kpi"><span class="muted">A · 확정 평균 순손익</span><div id="kANet" class="n">—</div></div>\n<div class="kpi"><span class="muted">B · WATCH BUY 선정 / 첫 봉 손절</span><div id="kBBuy" class="n">—</div></div>\n<div class="kpi"><span class="muted">B · 확정 평균 순손익</span><div id="kBNet" class="n">—</div></div>\n</div>\n<h3>같은 정책으로 매일 독립 선정한 결과</h3><div class="tablewrap"><table><thead><tr><th>진입 정책</th><th>선정</th><th>확정</th><th>수익</th><th>손실</th><th>첫 봉 손절</th><th>동봉 TP/SL</th><th>평균 순익</th></tr></thead><tbody id="compareTable"><tr><td colspan="8">검사 전</td></tr></tbody></table></div>\n<h3>날짜별 A/B 비교</h3><div class="tablewrap"><table><thead><tr><th>날짜</th><th>A 선정/확정</th><th>A 수익/손실</th><th>A 평균</th><th>B 선정/확정</th><th>B 수익/손실</th><th>B 평균</th></tr></thead><tbody id="dailyTable"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<h3>선정된 거래 목록 (A · B 모두 포함)</h3><div class="tablewrap"><table><thead><tr><th>정책</th><th>날짜·종목</th><th>실제 가정 진입시각</th><th>점수</th><th>첫 5분봉</th><th>청산</th><th>순손익</th><th>돌파 당시 시각</th></tr></thead><tbody id="selectedTable"><tr><td colspan="8">검사 전</td></tr></tbody></table></div></section>\n<section class="panel"><h2>④ 전체 C 돌파 신호 진단 · 선정 무관</h2>\n<div class="tablewrap"><table><thead><tr><th>분류</th><th>신호 수</th><th>확정 청산</th><th>수익</th><th>평균 순손익</th></tr></thead><tbody id="signalTable"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<p class="muted">A 전체 신호와 B 확인 신호는 거래 집합·진입 시점이 다릅니다. 정책별 선정 결과와 구분해 해석해 주세요.</p>\n<details><summary>WATCH 거절·확인 세부 신호 (100건 표시, CSV 전체)</summary><div class="tablewrap"><table><thead><tr><th>종목</th><th>돌파 완료</th><th>WATCH 확인</th><th>상태</th><th>기존 C 순익</th><th>WATCH 진입 순익</th></tr></thead><tbody id="allTable"><tr><td colspan="6">검사 전</td></tr></tbody></table></div></details></section>\n<section class="panel"><h2>⑤ 핵심 연구 · 일봉 / 4시간 / 1시간 추세</h2>\n<p class="same">이 진단은 <b>기존 BUY를 한 건도 거르지 않습니다.</b> 점수, 거래 선택, 익절·손절을 바꾸지 않고, 과거 매수 신호 당시 이미 <b>완전히 마감된</b> 상위 시간봉으로만 추세 상태를 분류합니다. 미완성 봉과 데이터 공백은 <b>UNKNOWN</b>으로 표시합니다.</p>\n<p class="muted">상승(UP): 종가 &gt; SMA20, SMA5 &gt; SMA20, SMA20 상승. 하락(DOWN): 세 조건 반대. 나머지는 중립(SIDE). SMA20 기울기는 4개 봉 전과 비교. 24개 연속 완료봉이 필요하며 이 기준은 연구 가설로서 검증된 매수 규칙이 아닙니다.</p>\n<h3>기존 C형 일일 선정 거래의 상위 추세별 결과</h3>\n<div class="tablewrap"><table><thead><tr><th>상위 봉</th><th>추세</th><th>선정</th><th>청산 확정</th><th>익절</th><th>첫 5분봉 손절</th><th>평균 순손익</th></tr></thead><tbody id="trendTable"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<h3>선정 거래마다 큰 추세가 어땠는가?</h3>\n<div class="tablewrap"><table><thead><tr><th>날짜·종목</th><th>신호</th><th>일봉</th><th>4시간봉</th><th>1시간봉</th><th>기존 결과</th><th>첫 봉 SL</th></tr></thead><tbody id="trendCases"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<p class="muted">아래 CSV에는 <code>trend_d1_*</code>, <code>trend_h4_*</code>, <code>trend_h1_*</code> 필드가 전체 C형 신호에 대해 저장됩니다. 각 시간봉 마지막 마감시각과 자료 부족 사유도 남깁니다. 비교 시 성공한 신호만 골라 보지 않습니다.</p></section>\n<section class="panel"><h2>⑥ 주의 및 분석 원칙</h2><p>확인봉을 한 번 더 기다리는 동안 상승을 놓칠 수도 있습니다. 따라서 확인 성공률만이 아니라 수수료 반영 실제 가정 진입가·순손익·첫 봉 손절을 비교해야 합니다. 진입 체결, 슬리피지, 호가 잔량은 실제와 다를 수 있습니다.</p>\n<p class="muted">기본: 편도 수수료 0.25%, 슬리피지 0%, 순익 목표 +0.5% 전량, 매수가 대비 손절 -0.7%, 진입 후 60분 시간 청산. 같은 봉 익절/손절을 모두 터치하면 보수적으로 손절 처리. <b>이것은 연구용 가상 거래이며 매매 권유나 자동 주문이 아닙니다.</b></p></section>\n<script>\n\'use strict\';\nconst $=id=>document.getElementById(id);\nconst CODE_VERSION=\'V2.CORE.42-LIGHT-MTF-TREND-DIAG\';\nconst esc=s=>String(s??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nconst fmt=(v,d=3)=>v===null||v===undefined||!Number.isFinite(Number(v))?\'—\':Number(v).toFixed(d)+\'%\';\nconst num=v=>Number(v||0).toLocaleString(\'ko-KR\');\nconst todayKST=()=>new Date(Date.now()+9*3600000).toISOString().slice(0,10);\nfunction daysEnding(end,n){const a=[];for(let i=n-1;i>=0;i--){const d=new Date(end+\'T00:00:00Z\');d.setUTCDate(d.getUTCDate()-i);a.push(d.toISOString().slice(0,10));}return a;}\n$(\'endDay\').value=new Date(Date.now()+9*3600000-86400000).toISOString().slice(0,10);\n$(\'endDay\').max=$(\'endDay\').value;\nfunction cfg(){const end=$(\'endDay\').value,n=Number($(\'days\').value),scope=$(\'scope\').value,fee=Number($(\'fee\').value),slip=Number($(\'slip\').value);\n if(!end||end>=todayKST())throw Error(\'종료된 날짜를 선택해 줘\');\n if(![1,3,5].includes(n)||![\'all\',\'40\',\'80\'].includes(scope))throw Error(\'범위 설정 오류\');\n if([fee,slip].some(x=>!Number.isFinite(x)||x<0||x>.5))throw Error(\'비용 입력은 0~0.5%\');\n return {days:daysEnding(end,n),scope,fee,slip};}\nfunction policy(){return {target:$(\'target\').value,cap:Number($(\'cap\').value),minScore:Number($(\'minScore\').value),minCash:Number($(\'minCash\').value),selectMode:$(\'selectMode\').value};}\nlet state=null,running=false,paused=false;\nconst KEYSET=[\'endDay\',\'days\',\'scope\',\'fee\',\'slip\'];\nfunction api(url,payload){return fetch(url,{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(payload)}).then(async r=>{const j=await r.json().catch(()=>({error:\'응답을 읽을 수 없음\'}));if(!r.ok||!j.ok)throw Error(j.error||\'HTTP \'+r.status);return j;});}\nfunction allEvents(){return state?state.results.flatMap(r=>r.events||[]):[];}\nfunction qualifiedEvents(){const p=policy();return allEvents().filter(e=>!!e.box_signal_at&&Number(e.box_day_trade_krw_at_signal)>=p.minCash&&Number(e.box_score)>=p.minScore);}\nfunction candidateId(e){return [e.date,e.market,e.dip_signal_at].join(\'|\');}\nfunction selectForMethod(method){\n if(!state)return {method,selected:[],byDay:[],nCandidates:0};\n const p=policy(),key=(method===\'box\'?\'box\':\'watch41\')+\'_tp\'+p.target;\n const pool=qualifiedEvents().filter(e=>method===\'box\'||e.watch41_confirm_status===\'CONFIRMED\'&&e.watch41_signal_at)\n .map(e=>({e,at:method===\'box\'?e.box_signal_at:e.watch41_signal_at,method,key,score:Number(e.box_score)}))\n .sort((a,b)=>a.at.localeCompare(b.at)||b.score-a.score||a.e.market.localeCompare(b.e.market));\n const selected=[],byDay=[];\n for(const day of state.days){const chosen=[],usedSlots=new Set(),usedCoins=new Set();let busyUntil=\'\';const dayPool=pool.filter(x=>x.e.date===day);\n  for(const item of dayPool){if(chosen.length>=p.cap)break;\n   const slot=Math.floor(Number(item.at.slice(11,13))/6);\n   if(p.selectMode===\'slot\'&&usedSlots.has(slot))continue;\n   if(usedCoins.has(item.e.market))continue;\n   if(busyUntil&&item.at<busyUntil)continue;\n   chosen.push(item);selected.push(item);usedSlots.add(slot);usedCoins.add(item.e.market);\n   const st=item.e[key+\'_status\'];busyUntil=st===\'READY\'?(item.e[key+\'_exit_at\']||item.at):day+\'T23:59:59+09:00\';\n  }\n  const ready=chosen.filter(x=>x.e[key+\'_status\']===\'READY\');const wins=ready.filter(x=>Number(x.e[key+\'_net_pct\'])>0);\n  byDay.push({date:day,candidates:dayPool.length,selected:chosen.length,ready:ready.length,wins:wins.length,loss:ready.length-wins.length,avg:ready.length?ready.reduce((s,x)=>s+Number(x.e[key+\'_net_pct\']),0)/ready.length:null});\n }\n return {method,selected,byDay,nCandidates:pool.length};\n}\nfunction stats(pick){const ready=pick.selected.filter(x=>x.e[x.key+\'_status\']===\'READY\'),wins=ready.filter(x=>Number(x.e[x.key+\'_net_pct\'])>0);return {picked:pick.selected.length,ready:ready.length,wins:wins.length,loss:ready.length-wins.length,firstSL:pick.selected.filter(x=>x.e[(pick.method===\'box\'?\'box\':\'watch41\')+\'_first5_sl\']).length,both:pick.selected.filter(x=>x.e[(pick.method===\'box\'?\'box\':\'watch41\')+\'_first5_touch_both\']).length,avg:ready.length?ready.reduce((s,x)=>s+Number(x.e[x.key+\'_net_pct\']),0)/ready.length:null};}\nfunction rows(id,html,n){$(id).innerHTML=html||`<tr><td colspan="${n}">해당 결과 없음</td></tr>`;}\nfunction render(){if(!state)return;const ev=allEvents(),cands=qualifiedEvents(),a=selectForMethod(\'box\'),b=selectForMethod(\'watch41\'),sa=stats(a),sb=stats(b),p=policy();\n const ok=cands.filter(e=>e.watch41_confirm_status===\'CONFIRMED\').length,reject=cands.filter(e=>e.watch41_confirm_status===\'REJECTED\').length,unknown=cands.filter(e=>e.watch41_confirm_status===\'UNKNOWN\').length;\n $(\'kDone\').textContent=num(state.done)+\' / \'+num(state.tasks.length);\n $(\'kErrors\').textContent=num(state.results.filter(r=>r.coverage.reason).length)+\' / \'+num(state.errors.length);\n $(\'kSignals\').textContent=num(cands.length);$(\'kWatch\').textContent=`${ok} / ${reject} / ${unknown}`;\n $(\'kABuy\').textContent=sa.picked+\' / \'+sa.firstSL;$(\'kANet\').textContent=fmt(sa.avg);$(\'kANet\').className=\'n \'+(sa.avg!==null?(sa.avg>=0?\'good\':\'bad\'):\'\');\n $(\'kBBuy\').textContent=sb.picked+\' / \'+sb.firstSL;$(\'kBNet\').textContent=fmt(sb.avg);$(\'kBNet\').className=\'n \'+(sb.avg!==null?(sb.avg>=0?\'good\':\'bad\'):\'\');\n $(\'bar\').max=Math.max(1,state.tasks.length);$(\'bar\').value=state.done;\n rows(\'compareTable\',[{label:\'A · 즉시 BUY\',s:sa},{label:\'B · WATCH 후 BUY\',s:sb}].map(x=>`<tr><td>${x.label}</td><td>${x.s.picked}</td><td>${x.s.ready}</td><td class="good">${x.s.wins}</td><td class="bad">${x.s.loss}</td><td class="bad">${x.s.firstSL}</td><td>${x.s.both}</td><td>${fmt(x.s.avg)}</td></tr>`).join(\'\'),8);\n rows(\'dailyTable\',state.days.map((day,i)=>{const x=a.byDay[i],y=b.byDay[i];return `<tr><td>${day}</td><td>${x.selected}/${x.ready}</td><td>${x.wins}/${x.loss}</td><td>${fmt(x.avg)}</td><td>${y.selected}/${y.ready}</td><td>${y.wins}/${y.loss}</td><td>${fmt(y.avg)}</td></tr>`}).join(\'\'),7);\n const chosen=[...a.selected,...b.selected].sort((x,y)=>x.at.localeCompare(y.at)||x.method.localeCompare(y.method));\n rows(\'selectedTable\',chosen.map(x=>{const e=x.e,m=x.method===\'box\'?\'box\':\'watch41\',sl=e[m+\'_first5_sl\'],both=e[m+\'_first5_touch_both\'],v=e[x.key+\'_net_pct\'];let f5=sl?\'첫 봉 SL\':e[m+\'_first5_tp\']?\'첫 봉 TP\':e[m+\'_first5_data_ok\']?\'첫 봉 유지\':\'미확인\';return `<tr><td>${m===\'box\'?\'A · 즉시\':\'B · WATCH\'}</td><td>${esc(e.date)} ${esc(e.market)}</td><td>${esc(x.at.slice(11,16))}</td><td>${x.score}</td><td class="${sl?\'bad\':\'\'}">${f5}${both?\' · 양방향\':\'\'}</td><td>${esc(e[x.key+\'_exit\']||e[x.key+\'_status\']||\'—\')}</td><td class="${Number(v)<0?\'bad\':Number(v)>=0&&v!=null?\'good\':\'\'}">${fmt(v)}</td><td>${esc(e.box_signal_at.slice(11,16))}</td></tr>`;}).join(\'\'),8);\n const totalSets=[{label:\'기존 C · 전체 신호\',list:cands,key:\'box_tp\'+p.target},{label:\'WATCH 확인 후 진입\',list:cands.filter(e=>e.watch41_confirm_status===\'CONFIRMED\'),key:\'watch41_tp\'+p.target}];\n rows(\'signalTable\',totalSets.map(x=>{const rd=x.list.filter(e=>e[x.key+\'_status\']===\'READY\'),won=rd.filter(e=>Number(e[x.key+\'_net_pct\'])>0).length,avg=rd.length?rd.reduce((s,e)=>s+Number(e[x.key+\'_net_pct\']),0)/rd.length:null;return `<tr><td>${x.label}</td><td>${x.list.length}</td><td>${rd.length}</td><td>${won}</td><td>${fmt(avg)}</td></tr>`}).join(\'\'),5);\n const states=[\'UP\',\'SIDE\',\'DOWN\',\'UNKNOWN\'],timeframes=[[\'d1\',\'일봉\'],[\'h4\',\'4시간\'],[\'h1\',\'1시간\']],sel=a.selected.map(x=>x.e);\n rows(\'trendTable\',timeframes.flatMap(([tf,label])=>states.map(st=>{\n  const grp=sel.filter(e=>(e[\'trend_\'+tf+\'_state\']||\'UNKNOWN\')===st);\n  const done=grp.filter(e=>e[\'box_tp\'+p.target+\'_status\']===\'READY\');\n  const wins=done.filter(e=>Number(e[\'box_tp\'+p.target+\'_net_pct\'])>0);\n  const avg=done.length?done.reduce((s,e)=>s+Number(e[\'box_tp\'+p.target+\'_net_pct\']),0)/done.length:null;\n  return `<tr><td>${label}</td><td>${st===\'UP\'?\'상승\':st===\'DOWN\'?\'하락\':st===\'SIDE\'?\'중립\':\'미확인\'}</td><td>${grp.length}</td><td>${done.length}</td><td class="good">${wins.length}</td><td class="bad">${grp.filter(e=>e.box_first5_sl).length}</td><td>${fmt(avg)}</td></tr>`;\n })).join(\'\'),7);\n rows(\'trendCases\',a.selected.map(x=>{const e=x.e,v=e[x.key+\'_net_pct\'];const st=tf=>{const s=e[\'trend_\'+tf+\'_state\']||\'UNKNOWN\';const r=e[\'trend_\'+tf+\'_reason\'];return esc(s===\'UP\'?\'상승\':s===\'DOWN\'?\'하락\':s===\'SIDE\'?\'중립\':\'미확인\')+(s===\'UNKNOWN\'&&r?\' (\'+esc(r)+\')\':\'\');};\n  return `<tr><td>${esc(e.date)} ${esc(e.market)}</td><td>${esc(e.box_signal_at.slice(11,16))}</td><td>${st(\'d1\')}</td><td>${st(\'h4\')}</td><td>${st(\'h1\')}</td><td class="${Number(v)<0?\'bad\':Number(v)>=0&&v!=null?\'good\':\'\'}">${fmt(v)}</td><td>${e.box_first5_sl?\'예\':\'아니오\'}</td></tr>`;\n }).join(\'\'),7);\n rows(\'allTable\',cands.slice(0,100).map(e=>`<tr><td>${esc(e.market)}</td><td>${esc(e.box_signal_at.slice(11,16))}</td><td>${esc(e.watch41_confirm_at?.slice(11,16)||\'—\')}</td><td>${esc(e.watch41_confirm_status)}</td><td>${fmt(e[\'box_tp\'+p.target+\'_net_pct\'])}</td><td>${fmt(e[\'watch41_tp\'+p.target+\'_net_pct\'])}</td></tr>`).join(\'\'),6);\n}\nfunction status(t){$(\'status\').textContent=t;}\nfunction setRunning(x){running=x;$(\'run\').disabled=x;$(\'pause\').disabled=!x;KEYSET.forEach(id=>$(id).disabled=x);}\n$(\'pause\').onclick=()=>{paused=true;status(\'일시 중지 요청 · 진행 중인 작업만 완료합니다\');};\n[\'target\',\'cap\',\'minScore\',\'minCash\',\'selectMode\'].forEach(id=>$(id).addEventListener(\'change\',render));\n$(\'run\').onclick=async()=>{if(running)return;try{const c=cfg(),key=JSON.stringify(c);\n if(!state||state.key!==key||state.cursor===state.tasks.length){const r=await fetch(\'/api/v2core/universe\').then(x=>x.json());if(!r.ok||!Array.isArray(r.items))throw Error(r.error||\'KRW 목록 조회 실패\');const markets=[...new Set(c.scope===\'all\'?r.items:r.items.slice(0,Number(c.scope)))];const tasks=c.days.flatMap(date=>markets.map(market=>({date,market})));state={key,...c,tasks,cursor:0,done:0,results:[],errors:[]};}\n paused=false;setRunning(true);render();$(\'csv\').disabled=false;let last=0;\n async function worker(){while(!paused&&state.cursor<state.tasks.length){const task=state.tasks[state.cursor++];try{const out=await api(\'/api/v2core/mtf42/analyze\',{...task,fee:state.fee,slip:state.slip});state.results.push(out.result);}catch(e){state.errors.push({...task,error:String(e.message||e)});}finally{state.done++;if(state.done-last>=8||state.done===state.tasks.length){last=state.done;render();status(`${state.done}/${state.tasks.length} 검사 중 · API 오류 ${state.errors.length}`);}}}}\n await Promise.all(Array.from({length:Math.min(4,state.tasks.length)},()=>worker()));render();status(paused?\'일시 중지 · 같은 설정으로 이어서 재검사 가능\':`완료 ${state.done}/${state.tasks.length} · C 신호 ${qualifiedEvents().length}건 · CSV 저장 가능`);\n }catch(e){status(\'⚠️ \'+String(e.message||e));}finally{setRunning(false);$(\'csv\').disabled=!state||(!state.results.length&&!state.errors.length);}};\nfunction csvVal(v){let s=(v!==null&&typeof v===\'object\')?JSON.stringify(v):String(v??\'\');if(/^[=+@-]/.test(s)&&isNaN(Number(s)))s="\'"+s;return \'"\'+s.replace(/"/g,\'""\')+\'"\';}\n$(\'csv\').onclick=()=>{if(!state)return;const p=policy(),a=selectForMethod(\'box\'),b=selectForMethod(\'watch41\'),aIds=new Set(a.selected.map(x=>candidateId(x.e))),bIds=new Set(b.selected.map(x=>candidateId(x.e)));\n const base={version:CODE_VERSION,date_from:state.days[0],date_to:state.days.at(-1),scope:state.scope,fee_pct_side:state.fee,slippage_pct_side:state.slip,scan_complete:state.done===state.tasks.length,scan_done:state.done,scan_tasks:state.tasks.length,scan_errors:state.errors.length,selection_comparison:\'CORE36_BOX_MTF_DIAG_AND_WATCH41_LEGACY\',selection_mode:p.selectMode,min_score:p.minScore,min_trade_krw:p.minCash,daily_max:p.cap,target_net_pct:p.target===\'05\'?.5:.3,stop_gross_pct:.7,horizon_min:60,pump_rule:\'PRIOR_24_COMPLETED_5M_LOW_PLUS5\',sma:\'SMA5_SMA20_5MIN\',simulation:\'NEXT_OPEN_OHLC_STOP_FIRST\',watch_confirm_rule:\'NEXT_5M_CLOSE_ABOVE_PRE_BOX_UPPER_AND_SMA5\',baseline:\'CORE36_C_SIGNAL_UNCHANGED\',mtf_rule:\'D1_H4_H1_SMA5_SMA20_SLOPE4_COMPLETED_ONLY_NO_TRADE_FILTER\'};\n const out=[];for(const [method,pick] of [[\'box\',a],[\'watch41\',b]])for(const d of pick.byDay)out.push({...base,row_type:\'DAY_SUMMARY\',comparison_method:method,...d});\n for(const r of state.results){out.push({...base,row_type:\'MARKET_COVERAGE\',date:r.date,market:r.market,...r.coverage});for(const e of r.events||[]){const id=candidateId(e);out.push({...base,row_type:\'PULLBACK_EVENT\',selected_for_daily_limit:aIds.has(id),selected_baseline:aIds.has(id),selected_watch41:bIds.has(id),...e});}}\n for(const e of state.errors)out.push({...base,row_type:\'API_ERROR\',...e});\n const cols=[...new Set([\'row_type\',\'version\',\'date\',\'market\',\'trend_d1_state\',\'trend_h4_state\',\'trend_h1_state\',\'trend_d1_reason\',\'trend_h4_reason\',\'trend_h1_reason\',\'selected_baseline\',\'selected_watch41\',\'comparison_method\',\'box_signal_at\',\'watch41_confirm_at\',\'watch41_signal_at\',\'watch41_confirm_status\',\'watch41_confirm_reason\',\'box_score\',\'box_day_trade_krw_at_signal\',\'box_tp05_status\',\'box_tp05_net_pct\',\'box_first5_sl\',\'watch41_tp05_status\',\'watch41_tp05_net_pct\',\'watch41_first5_sl\',\'watch41_first5_touch_both\',...out.flatMap(x=>Object.keys(x))])];\n const csv=\'\ufeff\'+[cols.join(\',\'),...out.map(r=>cols.map(c=>csvVal(r[c])).join(\',\'))].join(\'\\r\\n\');const u=URL.createObjectURL(new Blob([csv],{type:\'text/csv;charset=utf-8\'}));const link=document.createElement(\'a\');link.href=u;link.download=`CORE42_MTF_TREND_DIAG_${state.days[0]}_${state.days.at(-1)}_${state.scope}_KRW.csv`;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(u),3000);\n};\n</script></main></body></html>'



# -------- CORE.42 DIAGNOSTIC ONLY: higher timeframes from completed candles --------
# The labels below DO NOT participate in the CORE36 signal, score, selection,
# WATCH confirmation, entry, exit or any price simulation.
_TFS = {
    'h1': ('/v1/candles/minutes/60', timedelta(hours=1), 100),
    'h4': ('/v1/candles/minutes/240', timedelta(hours=4), 90),
    'd1': ('/v1/candles/days', timedelta(days=1), 75),
}


def fetch_mtf_history(api_get, market, day, kst):
    """Three bounded public API requests, only when a C box breakout exists.

    Fetch up to the *end of the research day*. A later diagnostic function
    independently excludes any candle not fully CLOSED at the signal time.
    An unavailable timeframe never aborts the existing CORE36 backtest.
    """
    to_time = datetime.combine(day + timedelta(days=1), dt_time.min, tzinfo=kst)
    result = {}
    for name, (route, span, count) in _TFS.items():
        try:
            raw = api_get(route, {'market': market, 'to': to_time.strftime(KST_FORMAT), 'count': count})
            if not isinstance(raw, list):
                raise ValueError('상위 시간봉 응답이 배열이 아닙니다')
            unique = {}
            for candle in raw:
                try:
                    parsed = _parse_candle(candle, kst)
                    if parsed['t'] < to_time:
                        unique[parsed['t']] = parsed
                except (ValueError, KeyError, TypeError, OverflowError):
                    continue
            result[name] = {'bars': [unique[t] for t in sorted(unique)], 'error': ''}
        except Exception as exc:
            result[name] = {'bars': [], 'error': f'{type(exc).__name__}: {exc}'[:140]}
    return result


def _trend_completed_only(all_bars, signal_at, span):
    """Compute SMA5/SMA20 and 4-bar SMA20 slope from only 24 COMPLETED bars.

    Candle timestamps denote their OPEN. For a signal at 11:35, an H1 candle
    opened at 11:00 is INCOMPLETE and is not eligible. Likewise for H4/D1.
    No interpolation across gaps or forward filling of missing candles.
    """
    valid = [b for b in all_bars if b['t'] + span <= signal_at]
    blank = {'state': 'UNKNOWN', 'reason': '', 'completed_bars': len(valid),
             'last_closed_at': valid[-1]['t'].isoformat() if valid else ''}
    if len(valid) < 24:
        blank['reason'] = 'INSUFFICIENT_24_BARS'
        return blank
    selected = valid[-24:]
    if any(selected[i]['t'] - selected[i-1]['t'] != span for i in range(1, 24)):
        blank['reason'] = 'DISCONTINUOUS_CANDLES'
        return blank
    last_close = selected[-1]['t'] + span
    if signal_at - last_close >= span + FIVE:
        blank['reason'] = 'STALE_LAST_CANDLE'
        return blank
    closes = [b['c'] for b in selected]
    m5 = sum(closes[-5:])/5
    m20 = sum(closes[-20:])/20
    m20_prev4 = sum(closes[-24:-4])/20
    c = closes[-1]
    if c > m20 and m5 > m20 and m20 > m20_prev4:
        state = 'UP'
    elif c < m20 and m5 < m20 and m20 < m20_prev4:
        state = 'DOWN'
    else:
        state = 'SIDE'
    return {
        'state': state, 'reason': 'OK', 'completed_bars': len(valid),
        'last_closed_at': last_close.isoformat(),
        'close': round(c, 10),
        'sma5': round(m5, 10), 'sma20': round(m20, 10),
        'price_vs_sma20_pct': round(_price_pct(c, m20), 4),
        'sma5_vs_sma20_pct': round(_price_pct(m5, m20), 4),
        'sma20_slope_4bars_pct': round(_price_pct(m20, m20_prev4), 4),
    }


def attach_mtf_diagnostics(report, histories):
    """Annotate pre-existing C signals without changing any baseline outcome."""
    n = 0
    for event in report['events']:
        if not event.get('box_signal_at'):
            continue
        at = datetime.fromisoformat(event['box_signal_at'])
        for tf, (_, span, _) in _TFS.items():
            hs = histories[tf]
            if hs['error']:
                diag = {'state':'UNKNOWN','reason':'API_ERROR','api_error':hs['error']}
            else:
                diag = _trend_completed_only(hs['bars'], at, span)
            for k, v in diag.items():
                event[f'trend_{tf}_{k}'] = v
        event['trend_all_up'] = all(event.get(f'trend_{tf}_state')=='UP' for tf in ('d1','h4','h1'))
        event['trend_h4_h1_up'] = all(event.get(f'trend_{tf}_state')=='UP' for tf in ('h4','h1'))
        n += 1
    report['coverage']['trend_diagnostic_events'] = n
    report['coverage']['trend_higher_tf_request_count'] = 3
    report['coverage']['trend_tf_api_error_count'] = sum(bool(histories[tf]['error']) for tf in _TFS)
    return report

def register_daytrade_review(app,deps):
    """Only research routes; existing / and live app.py behavior stay unchanged."""
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache={'at':0.,'sorted':[],'markets':[],'results':{}}
    lock=Lock()

    @app.get('/v2-core-review')
    @app.get('/backtest-daytrade')
    def core42_page():
        return Response(CORE_PAGE,mimetype='text/html; charset=utf-8')

    @app.get('/api/v2core/universe')
    def core42_universe():
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

    @app.post('/api/v2core/mtf42/analyze')
    def core42_analyze():
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
            if any(e.get('box_signal_at') for e in report['events']):
                # Only C-signal-bearing markets need three extra timeframes.
                histories = fetch_mtf_history(api_get, market, cfg['day'], kst)
                attach_mtf_diagnostics(report, histories)
            else:
                report['coverage'].update(trend_diagnostic_events=0,
                                          trend_higher_tf_request_count=0,
                                          trend_tf_api_error_count=0)
            with lock:
                if len(cache['results'])>1000:
                    cache['results'].clear()
                cache['results'][key]=(time.monotonic(),report)
            return jsonify(ok=True,version=VERSION,cached=False,result=report)
        except Exception as e:
            return jsonify(ok=False,error=f'{type(e).__name__}: {e}'[:200]),400
