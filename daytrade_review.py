"""V2 CORE.48 LIGHT — recovery quality diagnostics + confirmed hidden RSI research.

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

VERSION = 'V2.CORE.49-LIGHT-PRETRADE-RANK80-DIAG'
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
    """Fetch 2h prehistory + full day + 3h15m after, exclusive KST page cursor."""
    start = datetime.combine(day, dt_time.min, tzinfo=kst) - timedelta(hours=2)
    end = datetime.combine(day + timedelta(days=1), dt_time.min, tzinfo=kst) + timedelta(hours=3, minutes=15)
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




# -------- CORE.43 DIAGNOSTIC ONLY: completed breakout and first 5m bar --------
# Pre/breakout fields are known at the completed BOX signal. Next5 fields
# become known only FIVE minutes LATER and MUST NOT select or score instant BUY.
def _core43_breakout_diagnostics(bars, box_i, event):
    """Signal-time pre-volume/shape + post-signal first-5m observations.

    No effect on the CORE36 score, baseline selection, WATCH41, or exit simulator.
    The fixed thresholds are *hypotheses* used to group historical signals.
    """
    b = bars[box_i]
    out = {
        'core43_pre_volume_status': 'UNKNOWN',
        'core43_pre_volume_ratio_3_vs_prior6': None,
        'core43_pre_cash_ratio_3_vs_prior6': None,
        'core43_pre_volume_recent3': None,
        'core43_pre_volume_prior6': None,
        'core43_pre_volume_bars': 0,
        'core43_signal_hypothesis': 'UNKNOWN',
        'core43_signal_hypothesis_rule': 'PRE_VOL_3_TO_PREV6_GE1_AND_BREAK_VOL_GE1_AND_BODY_GE60_AND_UPPER_WICK_LE20',
        'core43_next5_status': 'UNKNOWN',
        'core43_next5_continuation': 'UNKNOWN',
        'core43_next5_observed_at': '',
        'core43_next5_close_vs_breakout_pct': None,
        'core43_next5_close_vs_pre_upper_pct': None,
        'core43_next5_close_vs_entry_pct': None,
        'core43_next5_low_vs_pre_upper_pct': None,
        'core43_next5_high_vs_breakout_pct': None,
        'core43_next5_volume_vs_breakout': None,
        'core43_next5_cash_vs_breakout': None,
        'core43_next5_holds_pre_upper': None,
        'core43_next5_closes_above_breakout': None,
    }
    # Exactly 9 consecutive completed bars, strictly before breakout.
    # In particular, the current breakout bar is NOT in either volume mean.
    if box_i >= 9 and _contiguous(bars, box_i-1, 9):
        prior6 = bars[box_i-9:box_i-3]
        recent3 = bars[box_i-3:box_i]
        old_v = sum(x['v'] for x in prior6)/6
        new_v = sum(x['v'] for x in recent3)/3
        old_cash = sum(x['cash'] for x in prior6)/6
        new_cash = sum(x['cash'] for x in recent3)/3
        out['core43_pre_volume_bars'] = 9
        out['core43_pre_volume_recent3'] = round(new_v, 8)
        out['core43_pre_volume_prior6'] = round(old_v, 8)
        if old_v > 0 and old_cash > 0:
            vr = new_v/old_v
            out['core43_pre_volume_status'] = 'READY'
            out['core43_pre_volume_ratio_3_vs_prior6'] = round(vr, 4)
            out['core43_pre_cash_ratio_3_vs_prior6'] = round(new_cash/old_cash, 4)
        else:
            out['core43_pre_volume_status'] = 'ZERO_DENOMINATOR'
    else:
        out['core43_pre_volume_status'] = 'MISSING_9_CONTIGUOUS_PRE_BARS'

    pre_v = out['core43_pre_volume_ratio_3_vs_prior6']
    br_v = event.get('box_breakout_volume_vs_pre3')
    body = event.get('box_breakout_body_share_pct')
    wick = event.get('box_breakout_upper_wick_pct')
    if all(isinstance(z, (int,float)) and isfinite(z) for z in (pre_v,br_v,body,wick)):
        out['core43_signal_hypothesis'] = ('MATCH' if
            pre_v >= 1 and br_v >= 1 and body >= 60 and wick <= 20 else 'OTHER')

    # This is the NEXT bar after the instant-buy signal, NOT signal-time data.
    ni = _next_entry(bars, box_i)
    if ni is None:
        out['core43_next5_status'] = 'MISSING_NEXT_5M_BAR'
        return out
    nxt = bars[ni]
    upper = event.get('box_pre_upper')
    out['core43_next5_status'] = 'READY'
    out['core43_next5_observed_at'] = (nxt['t']+FIVE).isoformat()
    out['core43_next5_close_vs_breakout_pct'] = round(_price_pct(nxt['c'], b['c']),4)
    out['core43_next5_close_vs_entry_pct'] = round(_price_pct(nxt['c'], nxt['o']),4)
    out['core43_next5_high_vs_breakout_pct'] = round(_price_pct(nxt['h'], b['h']),4)
    if isinstance(upper, (int,float)) and upper > 0:
        out['core43_next5_close_vs_pre_upper_pct'] = round(_price_pct(nxt['c'],upper),4)
        out['core43_next5_low_vs_pre_upper_pct'] = round(_price_pct(nxt['l'],upper),4)
        out['core43_next5_holds_pre_upper'] = bool(nxt['c']>upper)
        out['core43_next5_closes_above_breakout'] = bool(nxt['c']>b['c'])
        out['core43_next5_continuation'] = (
            'CONTINUE' if nxt['c']>upper and nxt['c']>b['c'] else 'RETRACE')
    out['core43_next5_volume_vs_breakout'] = round(nxt['v']/b['v'],4) if b['v']>0 else None
    out['core43_next5_cash_vs_breakout'] = round(nxt['cash']/b['cash'],4) if b['cash']>0 else None
    return out


# -------- CORE.44 RESEARCH ONLY: delayed entry, re-priced AFTER one completed 5m bar --------
def _delay44_after_box(bars, box_i, fee, slip, next5_continuation):
    """C: always wait one FULL bar; D: wait and require its close to hold the break.

    All decisions are made at the end of bars[box_i+1]; earliest entry is
    bars[box_i+2] OPEN. Exits are simulated for a new 60-minute holding window.
    No future exit observation may influence C/D eligibility.
    """
    result = {
        'delay44_rule': 'WAIT_ONE_COMPLETED_5M_ALWAYS_NEXT_OPEN',
        'cont44_rule': 'WAIT_ONE_COMPLETED_5M_NEXT_CLOSE_GT_BOX_CLOSE_AND_PRE_UPPER',
        'delay44_confirm_status': 'UNKNOWN',
        'cont44_confirm_status': 'UNKNOWN',
        'delay44_confirm_reason': '',
        'cont44_confirm_reason': '',
        'delay44_signal_at': '',
        'cont44_signal_at': '',
        'delay44_entry_delay_min': 5,
        'cont44_entry_delay_min': 5,
        'delay44_tp03_status': 'UNKNOWN',
        'delay44_tp05_status': 'UNKNOWN',
        'cont44_tp03_status': 'UNKNOWN',
        'cont44_tp05_status': 'UNKNOWN',
    }
    ni = _next_entry(bars, box_i)
    if ni is None:
        result['delay44_confirm_reason'] = '돌파 직후 연속된 첫 5분봉 없음'
        result['cont44_confirm_reason'] = result['delay44_confirm_reason']
        return result
    signal_at = (bars[ni]['t'] + FIVE).isoformat()
    result['delay44_confirm_status'] = 'CONFIRMED'
    result['delay44_signal_at'] = signal_at
    eligible_cont = next5_continuation == 'CONTINUE'
    result['cont44_confirm_status'] = ('CONFIRMED' if eligible_cont else
                                       'UNKNOWN' if next5_continuation == 'UNKNOWN' else 'REJECTED')
    if eligible_cont:
        result['cont44_signal_at'] = signal_at
    else:
        result['cont44_confirm_reason'] = ('첫 5분봉 종가 상승 유지 아님' if next5_continuation != 'UNKNOWN'
                                           else '첫 5분봉 상승 유지 판단 불가')
        for target in (.3, .5):
            result[f'cont44_tp{int(target*10):02}_status'] = ('REJECTED' if
                next5_continuation != 'UNKNOWN' else 'UNKNOWN')
    entry_i = _next_entry(bars, ni)
    if entry_i is None:
        result['delay44_confirm_reason'] = '확인 후 연속 진입봉 없음'
        if eligible_cont: result['cont44_confirm_reason'] = '확인 후 연속 진입봉 없음'
        return result
    for target in (.3, .5):
        simulation = _exit_sim(bars, entry_i, fee, slip, target)
        suffix = f'_tp{int(target*10):02}_'
        for key, value in simulation.items():
            result['delay44' + suffix + key] = value
            if eligible_cont:
                result['cont44' + suffix + key] = value
    for method in ('delay44', 'cont44'):
        if method == 'cont44' and not eligible_cont:
            continue
        first_diag = _box_firstbar_diagnostics(
            bars, ni,
            {'exit': result.get(method + '_tp05_exit', ''),
             'bars_held': result.get(method + '_tp05_bars_held')}, fee, slip)
        for key, value in first_diag.items():
            if key.startswith('box_'):
                result[method + '_' + key[4:]] = value
    return result

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


# ----- CORE.45 exit diagnostics ONLY; CORE36 entry / selection is unchanged -----
# A is the reference method: same breakout, next 5-minute open, same entry price.
# Percent targets always mean AFTER both sides' fees and slippage.
# Trailing activation: first bar reaches net +3.0%; previous completed-bar
# highest HIGH determines next bar's trailing stop (avoid intrabar lookahead).

def _core45_split_exit(bars, entry_i, fee, slip, *, sizes=(.5,.3,.2),
                       targets=(.5,1.2,3.), horizon=60, trail_gap=None):
    result={'status':'UNKNOWN','net_pct':None,'exit':'UNKNOWN','entry_price':None,
            'fills':[], 'reason':'', 'hold_minutes':horizon,
            'trail_gap_pct':trail_gap, 'tp1':False, 'tp2':False,
            'tp3':False, 'trail_activated':False, 'be_used':False}
    if entry_i is None or not 0 <= entry_i < len(bars):
        result['reason']='진입봉 없음';return result
    if any(x<=0 for x in sizes) or abs(sum(sizes)-1)>1e-9 or not targets[0]<targets[1]<targets[2]:
        raise ValueError('분할 익절 비중 또는 목표가 오류')
    if trail_gap is not None and not 0 < trail_gap <= 5:
        raise ValueError('추적 손절 폭은 0~5%')
    entry=bars[entry_i]['o'];result['entry_price']=entry
    side=(fee+slip)/100
    net_to_price=lambda n:entry*(1+side)*(1+n/100)/(1-side)
    levels=[net_to_price(x) for x in targets]
    be=net_to_price(0)
    initial_sl=entry*(1-.7/100)
    remain=1.;stage=0;weighted=0.;highest=None;trailing_floor=None
    last_t=None;last_c=None

    def fill(frac,px,kind,bar):
        nonlocal weighted,remain
        frac=min(remain,frac)
        if frac <= 1e-8:return
        pct=_net_pct(entry,px,fee,slip)
        weighted+=frac*pct;remain-=frac
        result['fills'].append({'share':round(frac,5),'kind':kind,
            'price':round(px,10),'net_pct':round(pct,5),
            'at':(bar['t']+FIVE).isoformat()})
        if 'BE' in kind:result['be_used']=True

    for off in range(horizon//5):
        i=entry_i+off
        if i>=len(bars):
            result['reason']='청산 기간 미래 분봉 부족';return result
        bar=bars[i]
        if last_t is not None and bar['t']-last_t != FIVE:
            result['reason']='진입 후 5분봉 불연속';return result
        last_t=bar['t'];last_c=bar['c']
        # Stops are active at bar OPEN, based only on prior completed bars.
        protective=initial_sl if stage==0 else max(be,trailing_floor or be)
        if bar['o']<=protective:
            fill(remain,bar['o'],'STOP_GAP' if stage==0 else 'BE_OR_TRAIL_GAP',bar)
        elif bar['l']<=protective:
            # High and low ordering unknown; stop first.
            fill(remain,protective,'SL' if stage==0 else 'BE_OR_TRAIL',bar)
        else:
            # TP1, TP2 and TP3/activation cannot use any future bar.
            if stage==0 and bar['h']>=levels[0]:
                fill(sizes[0],max(levels[0],bar['o']),'TP1',bar)
                stage=1;result['tp1']=True
                # When new BE and the bar's low overlap, pessimistically BE all remaining.
                if bar['l']<=be:
                    fill(remain,be,'SAME_BAR_BE',bar)
            if remain>1e-8 and stage==1 and bar['h']>=levels[1]:
                fill(sizes[1],max(levels[1],bar['o']),'TP2',bar)
                stage=2;result['tp2']=True
            if remain>1e-8 and stage==2:
                if trail_gap is None:
                    if bar['h']>=levels[2]:
                        fill(remain,max(levels[2],bar['o']),'TP3',bar)
                        result['tp3']=True
                elif highest is None and bar['h']>=levels[2]:
                    # Activation known at this candle's END; stop updates NEXT candle.
                    result['trail_activated']=True
                    highest=bar['h']
                elif highest is not None:
                    highest=max(highest,bar['h'])
            if trail_gap is not None and highest is not None and remain>1e-8:
                trailing_floor=max(be,highest*(1-trail_gap/100))
        if remain<=1e-8:
            result.update(status='READY',exit='COMPLETE',net_pct=round(weighted,5),
                exit_at=(bar['t']+FIVE).isoformat(),bars_held=off+1)
            return result
    # At the end of horizon exit ALL residual units at the last completed close.
    fill(remain,last_c,'TIME_EXIT',bars[entry_i+horizon//5-1])
    result.update(status='READY',exit='TIME_EXIT',net_pct=round(weighted,5),
                  exit_at=(bars[entry_i+horizon//5-1]['t']+FIVE).isoformat(),
                  bars_held=horizon//5)
    return result

_CORE45_VARIANTS=(
    ('old303040_60',(.3,.3,.4),(.5,1.5,3.),60,None),
    *tuple((f'fixed503020_{h}',(.5,.3,.2),(.5,1.2,3.),h,None)
           for h in (60,120,180)),
    *tuple((f'trail503020_{h}_gap{int(round(g*10)):02}',(.5,.3,.2),
            (.5,1.2,3.),h,g) for h in (60,120,180) for g in (.6,.8,1.0)),
)

def _add_core45_exit_research(e, bars, box_i, fee, slip):
    # The simulated fill is the OPEN of box_i+1, identical to the baseline A.
    entry_i=_next_entry(bars,box_i)
    e['core45_design']='RESEARCH_ONLY_BASELINE_A_SAME_ENTRY'
    for name,sizes,targets,h,gap in _CORE45_VARIANTS:
        sim=_core45_split_exit(bars,entry_i,fee,slip,sizes=sizes,
                               targets=targets,horizon=h,trail_gap=gap)
        pref='core45_'+name+'_'
        for k,v in sim.items():e[pref+k]=v



# -------- CORE.46 RESEARCH ONLY: CONFIRMED hidden bullish divergence --------
# Same 5m bars as existing C pattern. Price makes a HIGHER low, RSI(14) a
# LOWER low across the latest two confirmed price swing lows (2 left / 2 right).
# Right-side confirmation is available ONLY after those two bars have CLOSED.
# Neither RSI nor the hidden-divergence result changes any existing BUY signal.

_CORE46_LEFT = 2
_CORE46_RIGHT = 2
_CORE46_MIN_PIVOT_SPACING = 4      # at least 20 minutes
_CORE46_MAX_PIVOT_SPACING = 24     # at most 120 minutes
_CORE46_MAX_AGE_BARS = 12         # pattern last pivot <= 60 minutes old
_CORE46_PRICE_RISE_MIN_PCT = .10   # at least +0.10% higher price low
_CORE46_RSI_LOWER_MIN_POINTS = 2.0 # RSI at newer price pivot at least 2 lower


def _core46_rsi_wilder(bars, length=14):
    """Causal Wilder RSI: no forward values, reset at missing 5m candles."""
    out=[None]*len(bars)
    if not bars:return out
    avg_up=avg_down=None
    n=0;sum_up=sum_down=0.0
    for i in range(1,len(bars)):
        if bars[i]['t']-bars[i-1]['t']!=FIVE:
            n=0;sum_up=sum_down=0.0;avg_up=avg_down=None
            continue
        delta=bars[i]['c']-bars[i-1]['c']
        up=max(0.0,delta);down=max(0.0,-delta)
        if avg_up is None:
            n+=1;sum_up+=up;sum_down+=down
            if n<length:continue
            avg_up=sum_up/length;avg_down=sum_down/length
        else:
            avg_up=(avg_up*(length-1)+up)/length
            avg_down=(avg_down*(length-1)+down)/length
        if avg_down==0:
            out[i]=50.0 if avg_up==0 else 100.0
        elif avg_up==0:
            out[i]=0.0
        else:
            out[i]=100.0-100.0/(1.0+avg_up/avg_down)
    return out


def _core46_hidden_bull(bars, rsi, signal_i):
    """Evaluate ONLY at a completed signal bar, never use future to confirm pivots.

    A lower RSI is measured at the two corresponding PRICE swing-low candles,
    not chosen opportunistically from later RSI minima. Only the last TWO
    confirmed price pivots are compared (no cherry-picked successful pairs).
    """
    fields={'core46_hidden_state':'UNKNOWN','core46_hidden_reason':'',
            'core46_timeframe':'5m','core46_rsi_period':14,
            'core46_pivot_left':_CORE46_LEFT,'core46_pivot_right':_CORE46_RIGHT,
            'core46_price_low_older':None,'core46_price_low_newer':None,
            'core46_price_higher_low_pct':None,'core46_rsi_older':None,
            'core46_rsi_newer':None,'core46_rsi_delta_points':None,
            'core46_pivot_older_at':'','core46_pivot_newer_at':'',
            'core46_pivot_confirmed_at':'','core46_age_minutes':None,
            'core46_signal_checked_at':'',
            'core46_sma20_up_at_signal':None}
    if signal_i is None or signal_i<0 or signal_i>=len(bars):
        fields['core46_hidden_reason']='NO_SIGNAL';return fields
    fields['core46_signal_checked_at']=(bars[signal_i]['t']+FIVE).isoformat()
    ma=_sma(bars,signal_i,20);old=_sma(bars,signal_i-4,20)
    if ma is not None and old is not None:
        fields['core46_sma20_up_at_signal']=ma>old
    # Current index is the latest COMPLETED 5-minute bar. Only pivots with
    # k+RIGHT<=signal_i can be confirmed; therefore no pivot-time hindsight.
    latest=signal_i-_CORE46_RIGHT
    if latest<14+_CORE46_LEFT or rsi[signal_i] is None:
        fields['core46_hidden_reason']='INSUFFICIENT_RSI_OR_RIGHT_BARS';return fields
    # At least 48 bars of eligible pivot history; search in a bounded window.
    first=max(_CORE46_LEFT,signal_i-48)
    pivots=[]
    for k in range(first,latest+1):
        if rsi[k] is None or not _contiguous(bars,k+_CORE46_RIGHT,
                                           _CORE46_LEFT+_CORE46_RIGHT+1):
            continue
        low=bars[k]['l']
        left=bars[k-_CORE46_LEFT:k]
        right=bars[k+1:k+_CORE46_RIGHT+1]
        # Strict left ensures equal-price plateaus do not generate many pivots;
        # non-strict right makes tie resolution deterministic.
        if all(low<x['l'] for x in left) and all(low<=x['l'] for x in right):
            pivots.append(k)
    if len(pivots)<2:
        fields.update(core46_hidden_state='NO_MATCH',
                      core46_hidden_reason='LESS_THAN_TWO_CONFIRMED_PRICE_PIVOTS')
        return fields
    older,newer=pivots[-2:]
    price_old,price_new=bars[older]['l'],bars[newer]['l']
    rsi_old,rsi_new=rsi[older],rsi[newer]
    space=newer-older;age=signal_i-newer
    fields.update(core46_pivot_older_at=bars[older]['t'].isoformat(),
                  core46_pivot_newer_at=bars[newer]['t'].isoformat(),
                  core46_pivot_confirmed_at=(bars[newer+_CORE46_RIGHT]['t']+FIVE).isoformat(),
                  core46_price_low_older=round(price_old,10),
                  core46_price_low_newer=round(price_new,10),
                  core46_price_higher_low_pct=round(_price_pct(price_new,price_old),4),
                  core46_rsi_older=round(rsi_old,4),core46_rsi_newer=round(rsi_new,4),
                  core46_rsi_delta_points=round(rsi_new-rsi_old,4),
                  core46_age_minutes=age*5)
    if not _CORE46_MIN_PIVOT_SPACING<=space<=_CORE46_MAX_PIVOT_SPACING:
        fields.update(core46_hidden_state='NO_MATCH',core46_hidden_reason='PIVOT_SPACING_OUT_OF_RANGE')
    elif age>_CORE46_MAX_AGE_BARS:
        fields.update(core46_hidden_state='NO_MATCH',core46_hidden_reason='CONFIRMED_PIVOT_TOO_OLD')
    elif _price_pct(price_new,price_old)<_CORE46_PRICE_RISE_MIN_PCT:
        fields.update(core46_hidden_state='NO_MATCH',core46_hidden_reason='PRICE_NOT_HIGHER_LOW')
    elif rsi_new>rsi_old-_CORE46_RSI_LOWER_MIN_POINTS:
        fields.update(core46_hidden_state='NO_MATCH',core46_hidden_reason='RSI_NOT_LOWER_LOW')
    else:
        fields.update(core46_hidden_state='MATCH',core46_hidden_reason='CONFIRMED_HIDDEN_BULL')
    return fields


def _core46_attach(e, bars, rsi, dip_i, box_i):
    for label,index in (('dip',dip_i),('box',box_i)):
        if index is None:
            e[f'core46_{label}_hidden_state']='NO_SIGNAL'
            continue
        diag=_core46_hidden_bull(bars,rsi,index)
        for k,v in diag.items():
            e[f'core46_{label}_'+k.removeprefix('core46_')]=v
    e['core46_design']='DIAGNOSTIC_ONLY_NO_BUY_OR_SELECTION_FILTER'


# ----- CORE.47.1 RESEARCH ONLY: hidden bullish RSI -> actual SMA5 recovery entry -----
# Explicitly NOT a change to CORE.36 BUY, daily selection, or CORE.45 exits.
# At the completed DIP bar we freeze the causal confirmed-pivot hidden RSI
# state; AFTER the DIP the existing SMA5 reclaim candle signals a new setup.
# Earliest simulated fill is the NEXT 5m candle's OPEN, not the reclaim close.

def _core47_recovery_research(event, bars, dip_i, reclaim_i, fee, slip):
    hidden = event.get('core46_dip_hidden_state', 'UNKNOWN')
    result = {
        'core47_design':'RESEARCH_ONLY_NO_CHANGE_TO_EXISTING_BUY',
        'core47_rule':'DIP_CONFIRMED_HIDDEN_AT_DIP_PLUS_FIRST_CLOSE_ABOVE_SMA5_AND_PREV_CLOSE',
        'core47_hidden_at_dip':hidden,
        'core47_recovery_status':'NO_RECLAIM',
        'core47_recovery_reason':'',
        'core47_signal_at':'',
        'core47_entry_at':'',
        'core47_entry_price':None,
        'core47_recovery_from_dip_min':None,
        'core47_reclaim_close_vs_dip_pct':None,
        'core47_reclaim_close_vs_sma5_pct':None,
        'core47_recovery_tp03_status':'NO_SIGNAL',
        'core47_recovery_tp05_status':'NO_SIGNAL',
    }
    if dip_i is None or not 0 <= dip_i < len(bars):
        result['core47_recovery_status']='UNKNOWN'
        result['core47_recovery_reason']='DIP_BAR_MISSING'
        return result
    # Reclaim is an existing CORE.36 recorded signal, not a retrospective RSI minimum.
    if reclaim_i is None:
        result['core47_recovery_reason']='NO_SMA5_RECOVERY_BEFORE_SETUP_END'
        return result
    if not dip_i < reclaim_i < len(bars):
        result['core47_recovery_status']='UNKNOWN'
        result['core47_recovery_reason']='INVALID_RECLAIM_INDEX'
        return result
    # Require one contiguous path all the way from the dip to the confirm.
    if any(bars[j]['t']-bars[j-1]['t'] != FIVE for j in range(dip_i+1,reclaim_i+1)):
        result['core47_recovery_status']='UNKNOWN'
        result['core47_recovery_reason']='DIP_TO_RECLAIM_GAP'
        return result
    sma5=_sma(bars,reclaim_i,5)
    if sma5 is None or not (bars[reclaim_i]['c']>sma5 and bars[reclaim_i]['c']>bars[reclaim_i-1]['c']):
        result['core47_recovery_status']='UNKNOWN'
        result['core47_recovery_reason']='INCONSISTENT_RECLAIM_SMA5'
        return result
    signal_at=bars[reclaim_i]['t']+FIVE
    result.update(core47_recovery_status='CONFIRMED',
                  core47_signal_at=signal_at.isoformat(),
                  core47_recovery_from_dip_min=(reclaim_i-dip_i)*5,
                  core47_reclaim_close_vs_dip_pct=round(_price_pct(
                      bars[reclaim_i]['c'], bars[dip_i]['c']),4),
                  core47_reclaim_close_vs_sma5_pct=round(_price_pct(
                      bars[reclaim_i]['c'], sma5),4))
    entry_i=_next_entry(bars,reclaim_i)
    if entry_i is None:
        result['core47_recovery_reason']='NEXT_ENTRY_OPEN_MISSING'
    else:
        result['core47_entry_at']=bars[entry_i]['t'].isoformat()
        result['core47_entry_price']=bars[entry_i]['o']
    # Save SAME fresh entry with 0.5% full exit and 50/30/20 alternatives.
    # Also evaluate no-hidden recoveries, so the filter can be compared honestly.
    for target in (.3,.5):
        sim=_exit_sim(bars,entry_i,fee,slip,target)
        pref=f'core47_recovery_tp{int(target*10):02}_'
        for k,v in sim.items():result[pref+k]=v
    for name,sizes,targets,h,gap in _CORE45_VARIANTS:
        sim=_core45_split_exit(bars,entry_i,fee,slip,sizes=sizes,
                              targets=targets,horizon=h,trail_gap=gap)
        pref='core47_'+name+'_'
        for k,v in sim.items():result[pref+k]=v
    return result



# ---- CORE.48: recovery quality diagnostics (all signal-time, research-only) ----
# All reported predictive features use bars <= reclaim_i. Exit and P/L fields
# from CORE.47 are outcome labels only, NEVER fed into any signal calculation.
def _core48_recovery_quality(event, bars, dip_i, reclaim_i):
    out = {
        'core48_quality_rule': 'RECLAIM_CLOSE_ONLY_DIAGNOSTIC_NO_BUY_FILTER',
        'core48_quality_status': 'NO_RECLAIM',
        'core48_signal_at': '',
        'core48_candle_bullish': None,
        'core48_body_pct_range': None,
        'core48_close_location_pct': None,
        'core48_upper_wick_pct_range': None,
        'core48_candle_range_pct_open': None,
        'core48_reclaim_vs_prev_close_pct': None,
        'core48_close_vs_dip_close_pct': None,
        'core48_close_vs_dip_to_reclaim_low_pct': None,
        'core48_pre3_vs_prior6_volume_ratio': None,
        'core48_pre3_vs_prior6_cash_ratio': None,
        'core48_last3_close_change_pct': None,
        'core48_dip_to_reclaim_minutes': None,
        'core48_missing_reason': ''
    }
    state=event.get('core47_recovery_status')
    if state != 'CONFIRMED':
        out['core48_quality_status'] = 'UNKNOWN' if state == 'UNKNOWN' else 'NO_RECLAIM'
        out['core48_missing_reason'] = event.get('core47_recovery_reason','')
        return out
    if (reclaim_i is None or dip_i is None or not
            0 <= dip_i < reclaim_i < len(bars)):
        out['core48_quality_status']='UNKNOWN'
        out['core48_missing_reason']='INVALID_EVENT_INDICES'
        return out
    if not _contiguous(bars, reclaim_i, reclaim_i-dip_i+1):
        out['core48_quality_status']='UNKNOWN'
        out['core48_missing_reason']='NONCONTIGUOUS_DIP_TO_RECLAIM'
        return out
    b=bars[reclaim_i]
    out['core48_quality_status']='READY'
    out['core48_signal_at']=(b['t']+FIVE).isoformat()
    out['core48_dip_to_reclaim_minutes']=(reclaim_i-dip_i)*5
    out['core48_candle_bullish']=b['c']>b['o']
    ran=b['h']-b['l']
    if ran>0:
        out['core48_body_pct_range']=round(abs(b['c']-b['o'])/ran*100,4)
        out['core48_close_location_pct']=round((b['c']-b['l'])/ran*100,4)
        out['core48_upper_wick_pct_range']=round((b['h']-max(b['o'],b['c']))/ran*100,4)
    else:
        out['core48_missing_reason']='ZERO_RANGE_CANDLE_ONLY_PARTIAL_METRICS'
    out['core48_candle_range_pct_open']=round((b['h']-b['l'])/b['o']*100,4)
    out['core48_reclaim_vs_prev_close_pct']=round(_price_pct(b['c'],bars[reclaim_i-1]['c']),4)
    out['core48_close_vs_dip_close_pct']=round(_price_pct(b['c'],bars[dip_i]['c']),4)
    earlier_low=min(x['l'] for x in bars[dip_i:reclaim_i+1])
    out['core48_close_vs_dip_to_reclaim_low_pct']=round(_price_pct(b['c'],earlier_low),4)
    if _contiguous(bars,reclaim_i,9):
        last3=bars[reclaim_i-2:reclaim_i+1]
        prev6=bars[reclaim_i-8:reclaim_i-2]
        av3=sum(x['v'] for x in last3)/3
        av6=sum(x['v'] for x in prev6)/6
        cash3=sum(x['cash'] for x in last3)/3
        cash6=sum(x['cash'] for x in prev6)/6
        out['core48_pre3_vs_prior6_volume_ratio']=round(av3/av6,5) if av6>0 else None
        out['core48_pre3_vs_prior6_cash_ratio']=round(cash3/cash6,5) if cash6>0 else None
    if _contiguous(bars,reclaim_i,4):
        out['core48_last3_close_change_pct']=round(
            _price_pct(b['c'],bars[reclaim_i-3]['c']),4)
    return out


# ----- CORE.49: PRE-TRADE QUALITY & RANKED HISTORICAL LIQUIDITY (DIAGNOSTIC ONLY) -----
# Historical rank is based on the most recently completed 09:00-to-09:00
# 24-hour DAILY candle available at the selected KST day 00:00.  The live
# /v1/ticker/acc_trade_price_24h is NEVER used for historical candidate ranks.

def _core49_historical_cash(api_get, market, days, kst):
    """Per-market prior completed Bithumb 09:00 daily cash for requested KST dates.

    A 09:00 daily candle is not eligible until the FOLLOWING 09:00.
    A 00:00 snapshot therefore uses the previous 09:00 close: ~15h old.
    Missing candles are explicit nulls, not zero-valued ranks.
    """
    dates = sorted(set(days))
    to_time = datetime.combine(dates[-1], dt_time.min, tzinfo=kst)
    raw = api_get('/v1/candles/days', {
        'market': market, 'to': to_time.strftime(KST_FORMAT),
        'count': min(50, 10 + (dates[-1]-dates[0]).days)})
    if not isinstance(raw, list):
        raise ValueError('과거 일봉 거래대금 응답이 목록이 아닙니다')
    daily = []
    for x in raw:
        try:
            b = _parse_candle(x, kst)
            daily.append(b)
        except (ValueError, KeyError, TypeError, OverflowError):
            continue
    vals = {}
    stamps = {}
    for d in dates:
        cutoff = datetime.combine(d, dt_time.min, tzinfo=kst)
        valid = [b for b in daily if b['t'] + timedelta(days=1) <= cutoff
                 and timedelta(0) <= cutoff - (b['t'] + timedelta(days=1)) <= timedelta(hours=16)]
        if valid:
            latest = max(valid, key=lambda b: b['t'])
            vals[d.isoformat()] = round(latest['cash'], 2)
            stamps[d.isoformat()] = (latest['t'] + timedelta(days=1)).isoformat()
        else:
            vals[d.isoformat()] = None
            stamps[d.isoformat()] = ''
    return vals, stamps


def _core49_rsi_single(complete):
    """Wilder RSI14 using only completed contiguous candles."""
    if len(complete) < 30:
        return None
    step = complete[-1]['t']-complete[-2]['t']
    if step <= timedelta(0) or any(complete[j]['t']-complete[j-1]['t'] != step
       for j in range(len(complete)-29, len(complete))):
        return None
    sequence = complete[-30:]
    diffs = [sequence[j]['c']-sequence[j-1]['c'] for j in range(1,len(sequence))]
    gain = sum(max(d,0) for d in diffs[:14])/14
    loss = sum(max(-d,0) for d in diffs[:14])/14
    for d in diffs[14:]:
        gain=(gain*13+max(d,0))/14
        loss=(loss*13+max(-d,0))/14
    if loss == 0:
        return 100. if gain else 50.
    return round(100-100/(1+gain/loss),3)


def _core49_signal5(bars, i, dip_i, rsi):
    """Freeze 5m information at the *completed signal* bar i."""
    out={'status':'UNKNOWN','rsi14':None,'sma5':None,
         'sma20':None,'sma60':None,'ma_alignment':'UNKNOWN',
         'close_location_pct':None,'close_vs_sma5_pct':None,
         'risk_to_recent_low_pct':None,'risk_to_sma5_pct':None,
         'pattern_tag':'NONE','signal_at':''}
    if i is None or i<0 or i>=len(bars):return out
    b=bars[i]
    out['signal_at']=(b['t']+FIVE).isoformat()
    out['status']='READY'
    out['rsi14']=round(rsi[i],3) if i<len(rsi) and rsi[i] is not None else None
    for n in (5,20,60):
        ma=_sma(bars,i,n)
        out[f'sma{n}']=round(ma,10) if ma is not None else None
    if all(out[f'sma{n}'] is not None for n in (5,20,60)):
        a,c,d=out['sma5'],out['sma20'],out['sma60']
        out['ma_alignment']='UP' if a>c>d else 'DOWN' if a<c<d else 'MIXED'
    if b['h']>b['l']:
        out['close_location_pct']=round(100*(b['c']-b['l'])/(b['h']-b['l']),3)
    if out['sma5']:
        out['close_vs_sma5_pct']=round(_price_pct(b['c'],out['sma5']),4)
        out['risk_to_sma5_pct']=round(_price_pct(b['c'],out['sma5']),4)
    if _contiguous(bars,i,6):
        stop=min(x['l'] for x in bars[i-5:i+1])
        out['risk_to_recent_low_pct']=round(_price_pct(b['c'],stop),4)
    out['pattern_tag']='SMA5_RECLAIM' if i>dip_i else 'PULLBACK_START'
    return out


def _core49_mtf(complete_bars, signal_at, span):
    """Ichimoku cloud at signal time: spans computed 26 CLOSED bars earlier.

    SenkouA=(Tenkan9+Kijun26)/2, SenkouB=(high52+low52)/2;
    current cloud plotted at t comes from t-26. Requires 78 contiguous bars.
    """
    valid=[b for b in complete_bars if b['t']+span <= signal_at]
    out={'status':'UNKNOWN','reason':'','closed_count':len(valid),
         'rsi14':None,'ma_alignment':'UNKNOWN','cloud_state':'UNKNOWN',
         'cloud_positive':None,'close_vs_cloud_top_pct':None}
    if len(valid)<78:
        out['reason']='NEEDS_78_COMPLETED_BARS';return out
    seq=valid[-78:]
    if any(seq[j]['t']-seq[j-1]['t']!=span for j in range(1,len(seq))):
        out['reason']='DISCONTINUOUS';return out
    last=seq[-1]['t']+span
    if signal_at-last >= span+FIVE:
        out['reason']='STALE_HISTORY';return out
    def median_range(arr):
        return (max(b['h'] for b in arr)+min(b['l'] for b in arr))/2
    base_i = len(seq)-1-26
    tenkan=median_range(seq[base_i-8:base_i+1])
    kijun=median_range(seq[base_i-25:base_i+1])
    cloud_a=(tenkan+kijun)/2
    cloud_b=median_range(seq[base_i-51:base_i+1])
    top, bottom=max(cloud_a,cloud_b),min(cloud_a,cloud_b)
    close=seq[-1]['c']
    ma5=sum(b['c'] for b in seq[-5:])/5
    ma20=sum(b['c'] for b in seq[-20:])/20
    ma60=sum(b['c'] for b in seq[-60:])/60
    out.update(status='READY',reason='OK',rsi14=_core49_rsi_single(seq),
               ma_alignment=('UP' if ma5>ma20>ma60 else 'DOWN' if ma5<ma20<ma60 else 'MIXED'),
               cloud_state=('ABOVE' if close>top else 'BELOW' if close<bottom else 'INSIDE'),
               cloud_positive=cloud_a>cloud_b,
               close_vs_cloud_top_pct=round(_price_pct(close,top),4))
    return out


def _core49_attach_event(e, rows, st, rsi):
    for prefix,idx in (('box',st['box_i']),('reclaim',st['reclaim_i'])):
        feats=_core49_signal5(rows,idx,st['dip_i'],rsi)
        for k,v in feats.items():e[f'core49_{prefix}_{k}']=v
    tag=[]
    if st['box_i'] is not None:tag.append('BOX_BREAK')
    if st['reclaim_i'] is not None:tag.append('SMA5_RECLAIM')
    if e.get('core46_dip_hidden_state')=='MATCH':tag.append('CONFIRMED_HIDDEN_BULL')
    e['core49_pattern_tags']='|'.join(tag) or 'NO_CONFIRMED_PATTERN'
    # No subjective wedge/V-pattern claim without a separately validated rule.


def _core49_attach_mtf(report, histories):
    for e in report['events']:
        for kind, at_key in (('box','box_signal_at'),('reclaim','core47_signal_at')):
            if not e.get(at_key):continue
            when=datetime.fromisoformat(e[at_key])
            for tf,(_,span,_) in _TFS.items():
                hs=histories.get(tf,{'bars':[],'error':'UNFETCHED'})
                feat=({'status':'UNKNOWN','reason':'API_ERROR','error':hs['error']}
                      if hs.get('error') else _core49_mtf(hs['bars'],when,span))
                for k,v in feat.items():e[f'core49_{kind}_{tf}_{k}']=v
    report['coverage']['core49_quality_mtf_events']=sum(
        bool(e.get('core49_box_h1_status')=='READY' or
             e.get('core49_reclaim_h1_status')=='READY') for e in report['events'])
    return report

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
    core46_rsi=_core46_rsi_wilder(rows)
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
        _core46_attach(e,rows,core46_rsi,st['dip_i'],st['box_i'])
        e.update(_core47_recovery_research(e,rows,st['dip_i'],st['reclaim_i'],fee,slip))
        e.update(_core48_recovery_quality(e,rows,st['dip_i'],st['reclaim_i']))
        if st['box_i'] is not None:
            sim={'exit':e.get('box_tp05_exit',''), 'bars_held':e.get('box_tp05_bars_held')}
            e.update(_box_firstbar_diagnostics(rows,st['box_i'],sim,fee,slip))
            e.update(_core43_breakout_diagnostics(rows,st['box_i'],e))
            e.update(_watch41_after_box(rows,st['box_i'],
                     e['box_pre_upper'],fee,slip))
            e.update(_delay44_after_box(rows,st['box_i'],fee,slip,
                     e.get('core43_next5_continuation','UNKNOWN')))
            _add_core45_exit_research(e,rows,st['box_i'],fee,slip)
        _core49_attach_event(e,rows,st,core46_rsi)
        if st['reclaim_i'] is not None: coverage['reclaim_setups']+=1
        if st['box_i'] is not None: coverage['box_break_setups']+=1
    coverage['core48_quality_ready']=sum(e.get('core48_quality_status')=='READY' for e in events)
    coverage['core47_recovery_events']=sum(e.get('core47_recovery_status')=='CONFIRMED' for e in events)
    coverage['core47_hidden_recovery_events']=sum(e.get('core47_recovery_status')=='CONFIRMED' and e.get('core47_hidden_at_dip')=='MATCH' for e in events)
    coverage['core46_hidden_box_matches']=sum(e.get('core46_box_hidden_state')=='MATCH' for e in events)
    coverage['core46_hidden_box_unknown']=sum(e.get('core46_box_hidden_state')=='UNKNOWN' for e in events)
    coverage['delay44_signal_events'] = sum(bool(e.get('delay44_signal_at')) for e in events)
    coverage['cont44_confirmed_events'] = sum(e.get('cont44_confirm_status') == 'CONFIRMED' for e in events)
    coverage['events']=len(events)
    if coverage['contiguous_scan_bars']==0:coverage['reason']='연속 5분봉 부족'
    result['events']=events
    return result

CORE_PAGE = '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>V2 CORE.49 LIGHT · 거래대금 순위 × 매수 전 점검 연구</title><style>\n:root{color-scheme:dark;--bg:#0b1020;--panel:#151e31;--edge:#2b3954;--fg:#ecf2ff;--sub:#aab8d0;--blue:#87a4ff;--good:#4ade80;--bad:#ff8797}\n*{box-sizing:border-box}body{background:var(--bg);color:var(--fg);font:14px/1.55 system-ui,"Noto Sans KR",sans-serif;margin:0}main{max-width:1030px;margin:auto;padding:18px 13px 70px}\nh1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:0 0 11px}h3{font-size:14px;margin:14px 0 6px}p{margin:6px 0 12px}a{color:#b3c8ff}.muted{color:var(--sub);font-size:12px}.panel{border:1px solid var(--edge);background:var(--panel);border-radius:14px;padding:16px;margin:13px 0}\n.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:9px}.field{display:flex;flex-direction:column;gap:4px}label{font-size:12px;color:var(--sub)}input,select,button{width:100%;padding:11px;background:#111a2d;border:1px solid #465775;border-radius:8px;color:var(--fg);font:inherit}\nbutton{background:#3765df;font-weight:650;cursor:pointer}button.secondary{background:#273852;border-color:#415470}button:disabled{opacity:.4;cursor:default}.controls{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.controls button{width:auto;flex:1;min-width:105px}\n.kpis{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.kpi{border:1px solid var(--edge);border-radius:9px;padding:13px}.kpi .n{font-size:22px;font-weight:750}.good{color:var(--good)}.bad{color:var(--bad)}.warning{color:#ffd483}\n.tablewrap{overflow:auto;max-width:100%}table{border-collapse:collapse;width:100%;min-width:620px}th,td{font-size:12px;padding:9px;border-bottom:1px solid var(--edge);white-space:nowrap;text-align:right}th:first-child,td:first-child{text-align:left}th{color:#c3d1ec}tr:last-child td{border:0}.note{background:#0c172c;color:#b8c6dd;border-radius:9px;padding:12px;font-size:12px}.pill{display:inline-block;background:#283958;border:1px solid #4c6288;padding:2px 6px;border-radius:5px;font-size:11px}\nprogress{width:100%;height:12px}details{margin:10px 0}summary{cursor:pointer;padding:10px 0;font-weight:650}\n@media(max-width:640px){main{padding:13px 9px 44px}.panel{padding:12px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.field{min-width:0}h1{font-size:20px}button{padding:10px 8px}}\n\n.same{background:#16283a;border-left:3px solid #7ca1f8;padding:11px;border-radius:7px;margin-top:9px}\n.grid.comp{grid-template-columns:repeat(2,minmax(0,1fr))}\n.tablewrap{margin-top:8px}\n</style></head><body><main>\n<header><h1>🧪 V2 CORE.49 LIGHT · 거래대금 50/80위 × 매수 전 종합진단</h1><p class="muted">CORE.48 기능·50·30·20 유지 · 역사적 거래대금 1~50/51~80위 비교 · 일목구름·RSI·정배열·손절·쿨다운 연구 · 자동주문 없음</p><a href="/">← 실전 스캐너로 돌아가기</a></header>\n<section class="panel"><h2>① 연구 목표 · 기존 신호 보존 + 청산 비교</h2>\n<p>기존 <b>+5% 상승 → SMA5 첫 눌림 → 박스 상단 돌파(C형)</b> BUY·청산·선정 로직을 그대로 보존합니다. CORE.42의 <b>일봉·4시간봉·1시간봉 완료봉 추세</b>와 CORE.41의 즉시 BUY·WATCH 비교 위에, 돌파 전 거래량 변화·돌파봉 모양·다음 5분봉의 움직임을 진단 전용으로 추가합니다.</p>\n<div class="grid comp"><div class="note"><b>A. 기존 즉시 BUY</b><p>돌파 5분봉 마감 시 신호. 바로 <b>다음 5분봉 시가</b>에 진입한 것으로 계산.</p></div><div class="note"><b>B. WATCH → 확인 BUY</b><p>돌파봉 마감 시 WATCH. 다음 5분봉의 <b>종가가 돌파 전 박스 상단과 SMA5보다 모두 높으면</b> 확인, 그 <b>다음 5분봉 시가</b>에 진입. 미충족이면 매수하지 않음.</p></div></div>\n<p class="same">확인 대기 중인 후보는 선정 슬롯을 차지하지 않습니다. 확인이 끝난 후보만 시간순으로 재선정합니다. <b>미래에 수익 난 코인을 사후에 고르지 않습니다.</b> 두 방식은 진입 시각이 달라 수익·손절 결과도 새 진입가로 다시 계산합니다.</p>\n<p class="muted">두 방식 모두 C형 품질 점수는 기존 값 그대로 사용합니다. WATCH 확인은 새로운 수익 확률 점수가 아닙니다. 진입 시뮬레이션은 수수료와 5분봉 OHLC를 이용하며 동일 봉 TP/SL 동시 충족은 손절 우선입니다.</p></section>\n<section class="panel"><h2>② 검사 설정</h2><div class="grid">\n<div class="field"><label for="endDay">마지막 날짜 (KST·지난 날짜)</label><input id="endDay" type="date"></div>\n<div class="field"><label for="days">날짜 수</label><select id="days"><option value="1" selected>1일</option><option value="3">3일</option><option value="5">5일</option></select></div>\n<div class="field"><label for="scope">검사 범위</label><select id="scope"><option value="hist80" selected>과거 확정 거래대금 상위 1~80위 (추천)</option><option value="hist50">과거 확정 거래대금 상위 1~50위</option><option value="hist51to80">과거 확정 거래대금 51~80위만</option><option value="histall">과거 확정 순위를 조회한 전체 KRW</option><option value="all">전체 KRW (순위 계산 없음 · 기존 기준)</option></select></div>\n<div class="field"><label for="fee">편도 수수료 (%)</label><input id="fee" type="number" min="0" max="0.5" step="0.01" value="0.25"></div>\n<div class="field"><label for="slip">편도 슬리피지 (%)</label><input id="slip" type="number" min="0" max="0.5" step="0.01" value="0"></div>\n<div class="field"><label for="target">익절 순수익</label><select id="target"><option value="05" selected>+0.5%</option><option value="03">+0.3%</option></select></div>\n<div class="field"><label for="cap">하루 최대 선정</label><select id="cap"><option value="3">3건</option><option value="4" selected>4건</option></select></div>\n<div class="field"><label for="minScore">C형 신호 품질 점수</label><select id="minScore"><option value="0">전체</option><option value="50">50점 이상</option><option value="65" selected>65점 이상</option><option value="80">80점 이상</option></select></div>\n<div class="field"><label for="minCash">신호 당시 당일 거래대금</label><select id="minCash"><option value="0">제한 없음</option><option value="500000000" selected>5억 원 이상</option></select></div>\n<div class="field"><label for="selectMode">하루 선정 정책 (네 방식에 동일 적용)</label><select id="selectMode"><option value="slot" selected>6시간대별 최대 1건</option><option value="first">전체 하루 시간순 최대 4건</option></select></div>\n</div><div class="controls"><button id="run">CORE.49 순위·매수 전 종합진단 시작</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="csv" class="secondary" disabled>CORE.49 진단 CSV 다운로드</button></div>\n<progress id="bar" value="0" max="1"></progress><p id="status" class="muted" aria-live="polite">검사 전</p>\n<div class="note">5분봉 연속 데이터가 없는 종목은 포함되지 않을 수 있습니다. 하단의 검사 완료 수·자료 부족 수를 반드시 확인하세요. 역사적 순위 모드는 검사 당일 0시 이전에 이미 확정된 전일 09시~09시 거래대금으로 당시 현재 상장 중인 전체 KRW 마켓 순위를 다시 계산합니다. 실시간 24시간 이동 거래대금과는 다른 값입니다. <b>순위 조회에는 전체 종목 일봉 API 호출이 추가되므로 시간이 더 걸릴 수 있습니다.</b> 누락·오류 종목은 순위를 0으로 채우지 않고 제외/기록합니다. 현재 상장 종목을 기준으로 하므로 상장폐지 종목이 빠지는 생존 편향은 남습니다. 4개 병렬 작업이며 주문은 실행하지 않습니다. 검사 중 중단하면 부분 CSV는 미완료로 표시됩니다.</div></section>\n<section class="panel"><h2>③ 하루 최대 4건 · A/B/C/D 독립 비교</h2>\n<div class="kpis">\n<div class="kpi"><span class="muted">완료 종목·날짜</span><div id="kDone" class="n">—</div></div>\n<div class="kpi"><span class="muted">자료 부족 / API 오류</span><div id="kErrors" class="n warning">—</div></div>\n<div class="kpi"><span class="muted">전체 C 돌파 신호</span><div id="kSignals" class="n">—</div></div>\n<div class="kpi"><span class="muted">WATCH 확인 / 거절 / 미확인</span><div id="kWatch" class="n">—</div></div>\n<div class="kpi"><span class="muted">A · 즉시 BUY 선정 / 첫 봉 손절</span><div id="kABuy" class="n">—</div></div>\n<div class="kpi"><span class="muted">A · 확정 평균 순손익</span><div id="kANet" class="n">—</div></div>\n<div class="kpi"><span class="muted">B · WATCH BUY 선정 / 첫 봉 손절</span><div id="kBBuy" class="n">—</div></div>\n<div class="kpi"><span class="muted">B · 확정 평균 순손익</span><div id="kBNet" class="n">—</div></div>\n<div class="kpi"><span class="muted">C · 5분 무조건 지연 선정 / 첫 봉 손절</span><div id="kCBuy" class="n">—</div></div>\n<div class="kpi"><span class="muted">C · 확정 평균 순손익</span><div id="kCNet" class="n">—</div></div>\n<div class="kpi"><span class="muted">D · 상승 유지 확인 선정 / 첫 봉 손절</span><div id="kDBuy" class="n">—</div></div>\n<div class="kpi"><span class="muted">D · 확정 평균 순손익</span><div id="kDNet" class="n">—</div></div>\n</div>\n<h3>같은 정책으로 매일 독립 선정한 결과</h3><div class="tablewrap"><table><thead><tr><th>진입 정책</th><th>선정</th><th>확정</th><th>수익</th><th>손실</th><th>첫 봉 손절</th><th>동봉 TP/SL</th><th>평균 순익</th></tr></thead><tbody id="compareTable"><tr><td colspan="8">검사 전</td></tr></tbody></table></div>\n<h3>날짜별 A/B/C/D 비교</h3><div class="tablewrap"><table><thead><tr><th>날짜</th><th>A 선정/확정</th><th>A 승/패</th><th>A 평균</th><th>B 선정/확정</th><th>B 승/패</th><th>B 평균</th><th>C 선정/확정</th><th>C 승/패</th><th>C 평균</th><th>D 선정/확정</th><th>D 승/패</th><th>D 평균</th></tr></thead><tbody id="dailyTable"><tr><td colspan="13">검사 전</td></tr></tbody></table></div>\n<h3>선정된 거래 목록 (A/B/C/D 모두 포함)</h3><div class="tablewrap"><table><thead><tr><th>정책</th><th>날짜·종목</th><th>실제 가정 진입시각</th><th>점수</th><th>첫 5분봉</th><th>청산</th><th>순손익</th><th>돌파 당시 시각</th></tr></thead><tbody id="selectedTable"><tr><td colspan="8">검사 전</td></tr></tbody></table></div></section>\n<section class="panel"><h2>④ 전체 C 돌파 신호 진단 · 선정 무관</h2>\n<div class="tablewrap"><table><thead><tr><th>분류</th><th>신호 수</th><th>확정 청산</th><th>수익</th><th>평균 순손익</th></tr></thead><tbody id="signalTable"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<p class="muted">A/B/C/D는 진입 시점과 확인 조건이 다릅니다. 선정 결과와 전체 신호를 반드시 구분해 주세요.</p>\n<details><summary>WATCH 거절·확인 세부 신호 (100건 표시, CSV 전체)</summary><div class="tablewrap"><table><thead><tr><th>종목</th><th>돌파 완료</th><th>WATCH 확인</th><th>상태</th><th>기존 C 순익</th><th>WATCH 진입 순익</th></tr></thead><tbody id="allTable"><tr><td colspan="6">검사 전</td></tr></tbody></table></div></details></section>\n<section class="panel"><h2>⑤ 핵심 연구 · 일봉 / 4시간 / 1시간 추세</h2>\n<p class="same">이 진단은 <b>기존 BUY를 한 건도 거르지 않습니다.</b> 점수, 거래 선택, 익절·손절을 바꾸지 않고, 과거 매수 신호 당시 이미 <b>완전히 마감된</b> 상위 시간봉으로만 추세 상태를 분류합니다. 미완성 봉과 데이터 공백은 <b>UNKNOWN</b>으로 표시합니다.</p>\n<p class="muted">상승(UP): 종가 &gt; SMA20, SMA5 &gt; SMA20, SMA20 상승. 하락(DOWN): 세 조건 반대. 나머지는 중립(SIDE). SMA20 기울기는 4개 봉 전과 비교. 24개 연속 완료봉이 필요하며 이 기준은 연구 가설로서 검증된 매수 규칙이 아닙니다.</p>\n<h3>기존 C형 일일 선정 거래의 상위 추세별 결과</h3>\n<div class="tablewrap"><table><thead><tr><th>상위 봉</th><th>추세</th><th>선정</th><th>청산 확정</th><th>익절</th><th>첫 5분봉 손절</th><th>평균 순손익</th></tr></thead><tbody id="trendTable"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<h3>선정 거래마다 큰 추세가 어땠는가?</h3>\n<div class="tablewrap"><table><thead><tr><th>날짜·종목</th><th>신호</th><th>일봉</th><th>4시간봉</th><th>1시간봉</th><th>기존 결과</th><th>첫 봉 SL</th></tr></thead><tbody id="trendCases"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<p class="muted">아래 CSV에는 <code>trend_d1_*</code>, <code>trend_h4_*</code>, <code>trend_h1_*</code> 필드가 전체 C형 신호에 대해 저장됩니다. 각 시간봉 마지막 마감시각과 자료 부족 사유도 남깁니다. 비교 시 성공한 신호만 골라 보지 않습니다.</p></section>\n<section class="panel"><h2>⑥ CORE.43 핵심 · 돌파 성공/실패 신호 분해</h2>\n<p class="same">이번 연구는 <b>BUY 점수·매매 선정·손절·익절을 바꾸지 않습니다.</b> 시점이 다른 정보를 엄격히 분리합니다. 돌파봉 마감 전까지 알 수 있던 정보와 그 후에 알게 된 첫 5분봉 결과를 같은 순간의 필터처럼 사용하지 않습니다.</p>\n<p class="muted"><b>돌파 전 거래량</b>: 돌파봉 직전 3봉의 평균 거래량 ÷ 그 이전 6봉 평균(연속 9봉 필요). <b>돌파봉 거래량</b>: 돌파봉 거래량 ÷ 직전 최대 3봉 평균. <b>몸통 양호</b>: 상승 몸통이 전체 고저폭의 60% 이상이며 윗꼬리가 20% 이하. 수치는 검증 전의 비교 가설이며 BUY 조건이 아닙니다.</p>\n<h3>신호 당시 이미 알 수 있는 조건 · 전체 적격 BOX 신호</h3>\n<div class="tablewrap"><table><thead><tr><th>그룹</th><th>신호 수</th><th>확정</th><th>수익</th><th>첫 봉 손절</th><th>평균 순손익</th></tr></thead><tbody id="core43SignalTable"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<p class="muted">‘네 조건 동시 만족’은 돌파 전 거래량 ≥1, 돌파봉 거래량 ≥1, 몸통 ≥60%, 윗꼬리 ≤20%의 연구 그룹입니다. 모든 비교는 동일 날짜·동일 수수료·동일 C 신호를 사용하며 하루 선정 정책과 구분됩니다.</p>\n<h3>돌파 후 첫 5분 관찰 · 기존 즉시 BUY 판단에 사용 금지</h3>\n<div class="tablewrap"><table><thead><tr><th>첫 5분 종가</th><th>신호 수</th><th>확정</th><th>수익</th><th>첫 봉 손절</th><th>평균 순손익</th></tr></thead><tbody id="core43AfterTable"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<p class="muted">‘상승 유지’는 다음 5분봉 종가가 이전 박스 상단과 돌파봉 종가 <b>둘 다 초과</b>한 상태입니다. 이 데이터는 기존 즉시 BUY를 한 뒤에만 확인 가능하므로 진입 시점 예측 정확도로 해석할 수 없습니다. WATCH41 방식은 위 기존 비교표에서 별도 확인합니다.</p>\n<h3>실제 일일 선정된 기존 A 거래 · 진단 값</h3>\n<div class="tablewrap"><table><thead><tr><th>날짜·종목</th><th>돌파전 3/6</th><th>돌파량 배수</th><th>몸통 %</th><th>윗꼬리 %</th><th>첫5분 종가</th><th>기존 순손익</th></tr></thead><tbody id="core43PickedTable"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<p class="muted">CSV에는 <code>core43_pre_*</code>, <code>core43_signal_*</code>, <code>core43_next5_*</code> 진단 필드를 추가합니다. 분모가 0이거나 5분봉이 빠진 경우 수치를 임의로 0으로 만들지 않고 UNKNOWN/누락 사유를 남깁니다.</p></section>\n<section class="panel"><h2>⑦ CORE.44 연구 · 실제 가능한 5분 지연 진입</h2>\n<p class="same"><b>기존 A 즉시 BUY, B WATCH, C형 점수와 매매 조건은 변경하지 않습니다.</b> 가상 비교만 추가합니다. A는 돌파봉 마감 즉시 다음 봉 시가, C는 첫 5분봉이 마감된 다음 봉 시가, D는 그 첫 5분봉의 종가가 돌파봉 종가와 기존 박스 상단을 모두 초과한 경우에만 C와 같은 새 시가로 진입합니다. B는 기존 박스 상단·SMA5 확인 방식입니다.</p>\n<p class="muted">네 방식 모두 선택 정렬과 하루 최대 4건·6시간대별 1건 설정을 각각 독립 적용합니다. C와 D의 익절·손절·60분 보유 기간은 <b>새 진입가격 및 새 진입시각</b>에서 다시 계산합니다. 첫 5분봉은 확인에만 쓰며 C/D 거래 후 미래 OHLC로 조건을 만들지 않습니다.</p>\n<h3>같은 돌파 신호의 짝맞춤 비교 · 기존 즉시 A vs C 지연</h3>\n<div class="tablewrap"><table><thead><tr><th>첫 5분 흐름</th><th>양쪽 청산 확정</th><th>A 평균 순익</th><th>C 평균 순익</th><th>차이(C−A)</th></tr></thead><tbody id="core44PairTable"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<p class="muted">같은 종목·같은 BOX 돌파의 A/C 청산 결과가 모두 확정된 경우만 포함한 비교입니다. C/D에 필요한 연속 분봉이 없으면 UNKNOWN입니다. 수익률은 왕복 수수료와 지정된 슬리피지 반영. 실제 체결가/유동성, 상장폐지·상장 전 데이터, 미래 시점에서 수집한 시장 목록의 편향까지 보장하지는 않습니다.</p>\n</section>\n<section class="panel"><h2>⑧ 주의 및 분석 원칙</h2><p>확인봉을 한 번 더 기다리는 동안 상승을 놓칠 수도 있습니다. 따라서 확인 성공률만이 아니라 수수료 반영 실제 가정 진입가·순손익·첫 봉 손절을 비교해야 합니다. 진입 체결, 슬리피지, 호가 잔량은 실제와 다를 수 있습니다.</p>\n<p class="muted">기본: 편도 수수료 0.25%, 슬리피지 0%, 순익 목표 +0.5% 전량, 매수가 대비 손절 -0.7%, 진입 후 60분 시간 청산. 같은 봉 익절/손절을 모두 터치하면 보수적으로 손절 처리. <b>이것은 연구용 가상 거래이며 매매 권유나 자동 주문이 아닙니다.</b></p></section>\n\n<section class="panel" id="split45Research"><h2>⑨ CORE.45 연구 · 50·30·20 분할 익절 비교</h2>\n<p>같은 <b>A(즉시 BUY) 선정 거래 · 같은 진입 시각 · 같은 진입 가격</b>에서 청산 방식만 바꿔 비교합니다. <b>1차 50% / 순익 +0.5%, 2차 30% / +1.2%, 3차 20% / +3.0%</b>입니다. 추적형에서는 +3%에 도달하면 마지막 20%를 즉시 매도하지 않고 최고가 대비 지정 폭으로 추적합니다.</p>\n<div class="note">모든 목표는 왕복 수수료와 입력 슬리피지를 차감한 순수익률입니다. 최초 손절은 매수가 대비 -0.7%, 1차 익절 후 잔량은 비용 포함 본절로 보호합니다. 추적 손절은 완료 봉 고가를 기준으로 <b>다음 봉부터</b> 유효하게 적용합니다. 5분봉 안의 순서는 알 수 없어 손절 우선으로 보수적 계산합니다. 실전 주문 기능은 없습니다.</div>\n<h3>같은 최종 선정 거래의 청산 방식별 성적</h3>\n<div class="tablewrap"><table><thead><tr><th>청산 방식</th><th>최종 선정</th><th>청산 확정</th><th>순이익 거래</th><th>평균 순익</th><th>기존 전량 +0.5%와 차이</th><th>미확정</th></tr></thead><tbody id="core45Table"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<h3>일자별 50·30·20 요약</h3><div class="tablewrap"><table><thead><tr><th>날짜</th><th>기존 전량 +0.5%</th><th>새 고정 60분</th><th>새 고정 120분</th><th>새 고정 180분</th><th>추적형 120분 / 0.8%폭</th></tr></thead><tbody id="core45Daily"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<p class="muted">120·180분 보유 시 기존 60분 청산보다 보유 구간이 늘어, 후속 거래와 실제로 겹칠 수 있습니다. 이 화면은 동일한 선택 거래의 <b>개별 청산 가정 비교</b>이며 새 전략의 실행 가능한 일일 포트폴리오 수익률은 아닙니다. 분봉이 부족한 건 미확정으로 제외하며, 중단된 검사는 잠정 결과입니다.</p></section>\n<section class="panel" id="hidden46Research"><h2>⑩ CORE.46 연구 · RSI 히든 상승 다이버전스</h2>\n<p>5분봉 가격의 <b>높아진 저점(Higher Low)</b>과 같은 두 가격 저점에서 측정한 RSI(14)의 <b>낮아진 저점(Lower Low)</b>을 비교합니다. 저점은 양옆 2개 봉씩으로 확인하고, 오른쪽 2개 봉이 실제로 마감된 다음에만 확정 처리합니다.</p>\n<div class="note">조건: 가격 저점이 +0.10% 이상 높음 · RSI 2포인트 이상 하락 · 두 저점 간 20~120분 · 최신 저점에서 신호까지 최대 60분. <b>BOX 신호 발생 시점에 이미 확정된 값만</b> 진단합니다. MATCH는 성공 보장이 아니며 매수 조건/점수/선정에 사용하지 않습니다. 일봉 추세나 C형 기존 필터는 그대로입니다.</div>\n<h3>전체 BOX 신호 · RSI 히든 다이버전스 유무</h3>\n<div class="tablewrap"><table><thead><tr><th>구분</th><th>BOX 신호</th><th>청산 확정</th><th>순익 거래</th><th>승률</th><th>기존 평균 순익</th></tr></thead><tbody id="core46All"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<h3>기존 A 최종 선정 · 동일 거래의 분할익절 결과</h3>\n<div class="tablewrap"><table><thead><tr><th>구분</th><th>선정</th><th>기존/새 쌍확정</th><th>기존 평균</th><th>새 50·30·20 평균</th><th>변화</th></tr></thead><tbody id="core46Picked"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<h3>최종 선정 거래 · 진단 상세</h3>\n<div class="tablewrap"><table><thead><tr><th>날짜·종목</th><th>판정</th><th>저점 상승%</th><th>RSI 차이</th><th>확정 시각</th><th>기존 순익</th><th>새 고정120분</th></tr></thead><tbody id="core46Cases"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<p class="muted">CSV에는 <code>core46_dip_*</code>, <code>core46_box_*</code> 컬럼과 MATCH/NO_MATCH/UNKNOWN 및 이유·두 저점·RSI 수치·확정 시각을 기록합니다. 분봉이 부족하면 UNKNOWN. 실전 BUY와 기존 CORE.45 청산 가정은 변경하지 않습니다.</p></section>\n\n\n<section class="panel" id="recovery47Research"><h2>⑪ CORE.47.1 연구 · 히든 상승 RSI 후 실제 회복 진입</h2>\n<p><b>진단 전용 E 방식</b>: 기존 눌림(DIP) 마감 시 이미 확정된 히든 상승 다이버전스(MATCH)가 있는 코인에서, 그 이후 첫 <b>SMA5 상향 회복 + 직전 봉보다 높은 종가</b>가 마감된 경우에만 새 진입 후보를 만듭니다. 확인된 회복봉의 <b>다음 5분봉 시가</b>에서 진입한 것으로 계산해 비용과 손익을 다시 산출합니다. 기존 A/B/C/D 매수 신호는 바뀌지 않습니다.</p>\n<div class="note">RSI 피벗은 좌우 2봉이 이미 마감되어 확정된 값만 사용합니다. 실패한 회복 또는 자료 부족을 수익거래에서 제외하고 별도로 표기합니다. 같은 날짜·종목에서 회복 후 BOX 돌파와 진입 시점이 서로 다를 수 있으며, E는 신규 연구 시나리오일 뿐 실제 주문 신호가 아닙니다.</div>\n<h3>회복 확인 거래 · 당시 눌림 시점 히든 상승 일치 여부</h3>\n<div class="tablewrap"><table><thead><tr><th>구분</th><th>회복 후보</th><th>청산 확정</th><th>익절성 거래</th><th>승률</th><th>전량 +0.5% 평균</th><th>새 50·30·20 고정 120분</th></tr></thead><tbody id="core47Groups"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<h3>히든 상승 MATCH · 하루 최대 4건 독립 선정 (기존과 혼합하지 않음)</h3>\n<div class="tablewrap"><table><thead><tr><th>조건</th><th>후보/선정</th><th>확정 거래</th><th>수익 거래</th><th>평균 순손익</th><th>미확정</th></tr></thead><tbody id="core47Selected"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<h3>독립 선정 상세 · 동일 재진입가 비교</h3>\n<div class="tablewrap"><table><thead><tr><th>날짜/코인</th><th>회복 확정</th><th>새 가상 진입가</th><th>전량 +0.5% (60분)</th><th>50·30·20 고정120분</th><th>50·30·20 추적120분/0.8%</th></tr></thead><tbody id="core47Cases"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<p class="muted">분할 전략은 50% +0.5% / 30% +1.2% / 20% +3.0% 순수익 목표(비용 반영)이며 1차 이후 본전 보호, 추적형은 +3.0%에서 활성화합니다. 60/120/180분·추적 폭 0.6/0.8/1.0%는 CSV에 모두 저장됩니다. 각 보유시간의 결과는 <b>같은 60분 기준으로 선택한 거래들의 개별 결과</b>이며 실행 가능한 포트폴리오 성적이나 미래 성과 보장이 아닙니다.</p></section>\n\n\n<section class="panel" id="recovery48Research"><h2>⑫ CORE.48 연구 · 회복봉의 품질과 진입 전 과열 비교</h2>\n<p><b>기존 CORE.47.1 진입·선정 및 50·30·20 청산 방식은 그대로 유지</b>합니다. 회복봉이 마감되는 순간 확인할 수 있었던 <b>종가 위치·윗꼬리·몸통·직전 3봉 대비 과거 6봉의 평균 거래량 비율·눌림 대비 반등폭</b>을 새로 기록합니다. 수익 결과는 기존 회복 진입의 <b>다음 봉 시가</b>에서 다시 계산한 값을 사용합니다.</p>\n<div class="note">표의 범위는 탐색용 가설(고정 경계값)이며 최적화된 매수 필터가 아닙니다. 회복봉 이후의 고저가를 지표에 사용하지 않습니다. 확정 거래 수가 매우 적거나 분봉이 누락된 경우 수익 확률을 의미 있게 추정할 수 없습니다. 손익은 수수료 반영 가상값이며 실제 체결과 다를 수 있습니다.</div>\n<h3>회복 당시 RSI 일치 여부와 기존 선정 기준별 결과</h3>\n<div class="tablewrap"><table><thead><tr><th>대상</th><th>회복 신호</th><th>청산 확정</th><th>이익 거래</th><th>승률</th><th>+0.5% 전량 평균</th><th>50·30·20 120분 평균</th></tr></thead><tbody id="core48Groups"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<h3>회복봉 지표 구간별 사후 결과 비교 · 전체 회복 신호</h3>\n<div class="tablewrap"><table><thead><tr><th>사전에 알 수 있는 지표</th><th>범위</th><th>신호</th><th>확정</th><th>이익</th><th>승률</th><th>평균 순손익</th></tr></thead><tbody id="core48Bins"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<h3>선정 기준 통과 회복 신호 사례 (최대 60건)</h3>\n<div class="tablewrap"><table><thead><tr><th>날짜·코인</th><th>RSI 히든</th><th>회복봉 종가 위치</th><th>윗꼬리</th><th>거래량 배수</th><th>눌림 대비 반등</th><th>전량 +0.5% 순손익</th><th>E 선정</th></tr></thead><tbody id="core48Cases"><tr><td colspan="8">검사 전</td></tr></tbody></table></div>\n<p class="muted">승리는 최종 순손익이 양수인 거래로 정의합니다. 종가 위치는 (종가−저가)/(고가−저가), 윗꼬리는 (고가−몸통 상단)/(고가−저가), 거래량 배수는 회복봉 포함 최근 3봉의 평균 거래량 / 그 이전 6봉의 평균 거래량입니다. 거래량이 부족한 경우 \'자료 부족\'으로 분리합니다. 선정 방식은 CORE.47.1 정책을 변경하지 않습니다.</p>\n</section>\n\n<section class="panel" id="core49Research"><h2>⑬ CORE.49 · 과거 거래대금 순위 × 매수 전 체크리스트 (진단 전용)</h2>\n<p class="same"><b>상위 80위 검사 → 상위 1~50위·51~80위 독립 선정 결과를 비교</b>합니다. 순위는 검사 당일 00:00에 이미 끝난 마지막 09:00~09:00 일봉 거래대금 기준입니다. 조회 실패는 UNKNOWN으로 남기며 <b>현재 24시간 거래대금 순위를 과거에 사용하지 않습니다.</b></p>\n<div class="kpis"><div class="kpi"><span class="muted">과거 순위 조회 진행</span><div id="k49Rank" class="n">검사 전</div></div><div class="kpi"><span class="muted">순위 API 조회 실패</span><div id="k49Missing" class="n warning">—</div></div></div>\n<h3>날짜별 순위 조회 범위</h3><div class="tablewrap"><table><thead><tr><th>날짜</th><th>순위 산정</th><th>미확인</th><th>선택된 검사 종목</th><th>자료 마감</th></tr></thead><tbody id="core49Coverage"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<h3>선정 방식별 기존 A 즉시진입 성적 (독립 일일 선정)</h3><div class="tablewrap"><table><thead><tr><th>과거 순위 범위</th><th>후보</th><th>최종 선정</th><th>확정</th><th>수익</th><th>평균 순익</th></tr></thead><tbody id="core49RankGroups"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<h3>진입 전 완료봉 지표 · 일봉/4H/1H/5분</h3><div class="note">일봉·4시간·1시간: <b>완료봉만</b> 사용. 일목구름은 과거 26봉에 계산한 선행스팬을 현재 시점에 대조하며 <b>연속 78개 완료봉</b>이 없으면 UNKNOWN. RSI는 Wilder(14), 정배열은 SMA5＞20＞60. 5분봉 가격과 RSI/손절 거리는 신호 확정까지의 자료만 사용. 일목·이평·RSI는 기존 BUY에 영향을 주지 않습니다.</div>\n<div class="tablewrap"><table><thead><tr><th>날짜·종목</th><th>과거 순위</th><th>진단 점수 (확인분)</th><th>5m RSI</th><th>1H RSI/구름/MA</th><th>4H RSI/구름/MA</th><th>일 RSI/구름/MA</th><th>최근 저점 손절거리</th><th>기존 A 손익</th></tr></thead><tbody id="core49Signals"><tr><td colspan="9">검사 전</td></tr></tbody></table></div>\n<h3>패턴·손절 기준·매매 휴식 연구</h3><p class="muted">패턴은 이미 확인된 박스 돌파·SMA5 회복·RSI 히든 상승만 객관적 태그로 기록합니다. V자·더블바닥·쐐기 등은 이번 버전에서 임의 판정하지 않습니다. 손절 기준은 기존 -0.7%를 변경하지 않고 5분봉 SMA5·최근 6봉 저점까지 거리를 진단합니다. 3연속 손절 후 2시간·4시간 휴식은 <b>기존 선정 거래만 차단하는 사후 시뮬레이션</b>이며 차단된 거래 대신 새 후보를 뽑지 않으므로 실제 포트폴리오 수익이 아닙니다.</p>\n<div class="tablewrap"><table><thead><tr><th>3연속 손절 이후</th><th>기존 선정</th><th>차단 표시</th><th>남은 확정 거래</th><th>남은 평균 순익</th></tr></thead><tbody id="core49Cooldown"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<p class="muted">새 연구 점수는 연구용 가설일 뿐 기존 C형 65점 선정에 반영하지 않습니다. CSV에는 순위별 날짜 요약, 누락 정보, 프레임별 RSI·구름·정배열, 패턴 태그, 손절거리 및 쿨다운 표시를 기록합니다.</p></section>\n<script>\n\'use strict\';\nconst $=id=>document.getElementById(id);\nconst CODE_VERSION=\'V2.CORE.49-LIGHT-PRETRADE-RANK80-DIAG\';\nconst esc=s=>String(s??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nconst fmt=(v,d=3)=>v===null||v===undefined||!Number.isFinite(Number(v))?\'—\':Number(v).toFixed(d)+\'%\';\nconst num=v=>Number(v||0).toLocaleString(\'ko-KR\');\nconst todayKST=()=>new Date(Date.now()+9*3600000).toISOString().slice(0,10);\nfunction daysEnding(end,n){const a=[];for(let i=n-1;i>=0;i--){const d=new Date(end+\'T00:00:00Z\');d.setUTCDate(d.getUTCDate()-i);a.push(d.toISOString().slice(0,10));}return a;}\n$(\'endDay\').value=new Date(Date.now()+9*3600000-86400000).toISOString().slice(0,10);\n$(\'endDay\').max=$(\'endDay\').value;\nfunction cfg(){const end=$(\'endDay\').value,n=Number($(\'days\').value),scope=$(\'scope\').value,fee=Number($(\'fee\').value),slip=Number($(\'slip\').value);\n if(!end||end>=todayKST())throw Error(\'종료된 날짜를 선택해 줘\');\n if(![1,3,5].includes(n)||![\'all\',\'hist80\',\'hist50\',\'hist51to80\',\'histall\'].includes(scope))throw Error(\'범위 설정 오류\');\n if([fee,slip].some(x=>!Number.isFinite(x)||x<0||x>.5))throw Error(\'비용 입력은 0~0.5%\');\n return {days:daysEnding(end,n),scope,fee,slip};}\nfunction policy(){return {target:$(\'target\').value,cap:Number($(\'cap\').value),minScore:Number($(\'minScore\').value),minCash:Number($(\'minCash\').value),selectMode:$(\'selectMode\').value};}\nlet state=null,running=false,paused=false;\nconst KEYSET=[\'endDay\',\'days\',\'scope\',\'fee\',\'slip\'];\nfunction api(url,payload){return fetch(url,{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(payload)}).then(async r=>{const j=await r.json().catch(()=>({error:\'응답을 읽을 수 없음\'}));if(!r.ok||!j.ok)throw Error(j.error||\'HTTP \'+r.status);return j;});}\nfunction allEvents(){return state?state.results.flatMap(r=>r.events||[]):[];}\nfunction qualifiedEvents(){const p=policy();return allEvents().filter(e=>!!e.box_signal_at&&Number(e.box_day_trade_krw_at_signal)>=p.minCash&&Number(e.box_score)>=p.minScore);}\nfunction candidateId(e){return [e.date,e.market,e.dip_signal_at].join(\'|\');}\nfunction selectForMethod(method,rankRange=null){\n if(!state)return {method,selected:[],byDay:[],nCandidates:0};\n const p=policy(),key=method+\'_tp\'+p.target;\n const allowed=e=>{\n  if(method===\'box\')return !!e.box_signal_at;\n  if(method===\'watch41\')return e.watch41_confirm_status===\'CONFIRMED\'&&!!e.watch41_signal_at;\n  if(method===\'delay44\')return e.delay44_confirm_status===\'CONFIRMED\'&&!!e.delay44_signal_at;\n  if(method===\'cont44\')return e.cont44_confirm_status===\'CONFIRMED\'&&!!e.cont44_signal_at;\n  return false;\n };\n const pool=qualifiedEvents().filter(allowed).filter(e=>!rankRange||(Number(e.core49_rank)>=rankRange[0]&&Number(e.core49_rank)<=rankRange[1]))\n .map(e=>({e,at:e[method+\'_signal_at\'],method,key,score:Number(e.box_score)}))\n .sort((a,b)=>a.at.localeCompare(b.at)||b.score-a.score||a.e.market.localeCompare(b.e.market));\n const selected=[],byDay=[];\n for(const day of state.days){const chosen=[],usedSlots=new Set(),usedCoins=new Set();let busyUntil=\'\';const dayPool=pool.filter(x=>x.e.date===day);\n  for(const item of dayPool){if(chosen.length>=p.cap)break;\n   const slot=Math.floor(Number(item.at.slice(11,13))/6);\n   if(p.selectMode===\'slot\'&&usedSlots.has(slot))continue;\n   if(usedCoins.has(item.e.market))continue;\n   if(busyUntil&&item.at<busyUntil)continue;\n   chosen.push(item);selected.push(item);usedSlots.add(slot);usedCoins.add(item.e.market);\n   const st=item.e[key+\'_status\'];busyUntil=st===\'READY\'?(item.e[key+\'_exit_at\']||item.at):day+\'T23:59:59+09:00\';\n  }\n  const ready=chosen.filter(x=>x.e[key+\'_status\']===\'READY\');const wins=ready.filter(x=>Number(x.e[key+\'_net_pct\'])>0);\n  byDay.push({date:day,candidates:dayPool.length,selected:chosen.length,ready:ready.length,wins:wins.length,loss:ready.length-wins.length,avg:ready.length?ready.reduce((s,x)=>s+Number(x.e[key+\'_net_pct\']),0)/ready.length:null});\n }\n return {method,selected,byDay,nCandidates:pool.length};\n}\nfunction stats(pick){const ready=pick.selected.filter(x=>x.e[x.key+\'_status\']===\'READY\'),wins=ready.filter(x=>Number(x.e[x.key+\'_net_pct\'])>0);return {picked:pick.selected.length,ready:ready.length,wins:wins.length,loss:ready.length-wins.length,firstSL:pick.selected.filter(x=>x.e[pick.method+\'_first5_sl\']).length,both:pick.selected.filter(x=>x.e[pick.method+\'_first5_touch_both\']).length,avg:ready.length?ready.reduce((s,x)=>s+Number(x.e[x.key+\'_net_pct\']),0)/ready.length:null};}\nfunction rows(id,html,n){$(id).innerHTML=html||`<tr><td colspan="${n}">해당 결과 없음</td></tr>`;}\n\nfunction metric43(v){if(v===null||v===undefined||v===\'\')return null;const n=Number(v);return Number.isFinite(n)?n:null;}\nfunction group43(list,label){const p=policy(),k=\'box_tp\'+p.target,done=list.filter(e=>e[k+\'_status\']===\'READY\'),wins=done.filter(e=>metric43(e[k+\'_net_pct\'])>0),sl=list.filter(e=>e.box_first5_sl).length,avg=done.length?done.reduce((s,e)=>s+Number(e[k+\'_net_pct\']),0)/done.length:null;return `<tr><td>${esc(label)}</td><td>${list.length}</td><td>${done.length}</td><td class="good">${wins.length}</td><td class="bad">${sl}</td><td>${fmt(avg)}</td></tr>`;}\nfunction render43(cands,selected){\n const pre=e=>metric43(e.core43_pre_volume_ratio_3_vs_prior6);\n const bv=e=>metric43(e.box_breakout_volume_vs_pre3);\n const body=e=>metric43(e.box_breakout_body_share_pct);\n const wick=e=>metric43(e.box_breakout_upper_wick_pct);\n const pairs=[\n  [\'돌파 전 거래량 3/6 ≥1\',e=>pre(e)!==null&&pre(e)>=1],\n  [\'돌파봉 거래량 배수 ≥1\',e=>bv(e)!==null&&bv(e)>=1],\n  [\'상승 몸통 ≥60% + 윗꼬리 ≤20%\',e=>body(e)!==null&&wick(e)!==null&&body(e)>=60&&wick(e)<=20],\n  [\'네 조건 동시 만족\',e=>e.core43_signal_hypothesis===\'MATCH\'],\n ];\n const sig=[];\n for(const [label,pred] of pairs){const known=cands.filter(e=>label===\'네 조건 동시 만족\'?e.core43_signal_hypothesis!==\'UNKNOWN\':label.startsWith(\'돌파 전\')?pre(e)!==null:label.startsWith(\'돌파봉\')?bv(e)!==null:body(e)!==null&&wick(e)!==null);\n  sig.push(group43(known.filter(pred),label),group43(known.filter(e=>!pred(e)),label+\' · 미충족\'));\n  const unknown=cands.filter(e=>!known.includes(e));if(unknown.length)sig.push(group43(unknown,label+\' · 자료 부족\'));\n }\n rows(\'core43SignalTable\',sig.join(\'\'),6);\n rows(\'core43AfterTable\',[\n  group43(cands.filter(e=>e.core43_next5_continuation===\'CONTINUE\'),\'상승 유지 · 박스와 돌파 종가 초과\'),\n  group43(cands.filter(e=>e.core43_next5_continuation===\'RETRACE\'),\'상승 유지 실패 · 하나 이상 미달\'),\n  group43(cands.filter(e=>e.core43_next5_continuation===\'UNKNOWN\'),\'미확인 · 다음 봉 부족\'),\n ].join(\'\'),6);\n rows(\'core43PickedTable\',selected.map(x=>{const e=x.e,p=policy(),v=e[\'box_tp\'+p.target+\'_net_pct\'],c=e.core43_next5_close_vs_breakout_pct;return `<tr><td>${esc(e.date)} ${esc(e.market)}</td><td>${metric43(e.core43_pre_volume_ratio_3_vs_prior6)===null?\'—\':Number(e.core43_pre_volume_ratio_3_vs_prior6).toFixed(2)}</td><td>${metric43(e.box_breakout_volume_vs_pre3)===null?\'—\':Number(e.box_breakout_volume_vs_pre3).toFixed(2)}</td><td>${metric43(e.box_breakout_body_share_pct)===null?\'—\':Number(e.box_breakout_body_share_pct).toFixed(1)}</td><td>${metric43(e.box_breakout_upper_wick_pct)===null?\'—\':Number(e.box_breakout_upper_wick_pct).toFixed(1)}</td><td>${metric43(c)===null?\'—\':fmt(c)}</td><td class="${Number(v)<0?\'bad\':\'good\'}">${fmt(v)}</td></tr>`;}).join(\'\'),7);\n}\n\n\nfunction renderCore45(){\n if(!state)return;\n const a=selectForMethod(\'box\'),chosen=a.selected.map(x=>x.e),all=chosen.length;\n const variants=[[\'기존 전량 +0.5% (60분)\',\'baseline\',60,0],[\'예전 30·30·40 (60분)\',\'old303040_60\',60,0]];\n for(const h of [60,120,180])variants.push([`새 50·30·20 고정 (${h}분)`,`fixed503020_${h}`,h,0]);\n for(const h of [60,120,180])for(const g of [.6,.8,1.0])variants.push([`새 50·30·20 추적 (${h}분 / ${g.toFixed(1)}%)`,`trail503020_${h}_gap${String(Math.round(g*10)).padStart(2,\'0\')}`,h,g]);\n const k=\'box_tp05_\';const oldReady=chosen.filter(e=>e[k+\'status\']===\'READY\');\n const avg=arr=>arr.length?arr.reduce((s,n)=>s+n,0)/arr.length:null;\n const baseline=avg(oldReady.map(e=>Number(e[k+\'net_pct\'])));\n const extract=(name,list)=>name===\'baseline\' ? list.filter(e=>e[k+\'status\']===\'READY\').map(e=>Number(e[k+\'net_pct\'])) : list.filter(e=>e[`core45_${name}_status`]===\'READY\').map(e=>Number(e[`core45_${name}_net_pct`]));\n rows(\'core45Table\',variants.map(([label,name])=>{const values=extract(name,chosen),m=avg(values),wins=values.filter(v=>v>0).length;\n  return `<tr><td>${esc(label)}</td><td>${all}</td><td>${values.length}</td><td class="good">${wins}</td><td class="${m===null?\'\':m>=0?\'good\':\'bad\'}">${fmt(m)}</td><td>${m===null||baseline===null?\'—\':fmt(m-baseline)}</td><td>${all-values.length}</td></tr>`;\n }).join(\'\'),7);\n rows(\'core45Daily\',state.days.map(day=>{const list=chosen.filter(e=>e.date===day),ks=[\'baseline\',\'fixed503020_60\',\'fixed503020_120\',\'fixed503020_180\',\'trail503020_120_gap08\'];\n  return \'<tr><td>\'+esc(day)+\'</td>\'+ks.map(name=>{const vals=extract(name,list);return `<td>${fmt(avg(vals))} (${vals.length}/${list.length})</td>`;}).join(\'\')+\'</tr>\';\n }).join(\'\'),6);\n}\n\n\nfunction renderCore46(cands,chosen){\n const states=[[\'MATCH\',\'히든 상승 확인\'],[\'NO_MATCH\',\'조건 미충족\'],[\'UNKNOWN\',\'자료 부족\']];\n const key=\'box_tp05_\',newKey=\'core45_fixed503020_120_\';\n const mean=a=>a.length?a.reduce((s,x)=>s+x,0)/a.length:null;\n const group=st=>cands.filter(e=>(e.core46_box_hidden_state||\'UNKNOWN\')===st);\n rows(\'core46All\',states.map(([st,label])=>{\n  const list=group(st),ready=list.filter(e=>e[key+\'status\']===\'READY\');\n  const wins=ready.filter(e=>Number(e[key+\'net_pct\'])>0).length;\n  const values=ready.map(e=>Number(e[key+\'net_pct\']));\n  return `<tr><td>${label}</td><td>${list.length}</td><td>${ready.length}</td><td>${wins}</td><td>${ready.length?(wins/ready.length*100).toFixed(1)+\'%\':\'—\'}</td><td>${fmt(mean(values))}</td></tr>`;\n }).join(\'\'),6);\n rows(\'core46Picked\',states.map(([st,label])=>{\n  const list=chosen.filter(e=>(e.core46_box_hidden_state||\'UNKNOWN\')===st);\n  const pair=list.filter(e=>e[key+\'status\']===\'READY\'&&e[newKey+\'status\']===\'READY\');\n  const old=mean(pair.map(e=>Number(e[key+\'net_pct\'])));\n  const latest=mean(pair.map(e=>Number(e[newKey+\'net_pct\'])));\n  return `<tr><td>${label}</td><td>${list.length}</td><td>${pair.length}</td><td>${fmt(old)}</td><td>${fmt(latest)}</td><td>${old===null||latest===null?\'—\':fmt(latest-old)}</td></tr>`;\n }).join(\'\'),6);\n rows(\'core46Cases\',chosen.map(e=>{\n  const v=e.core46_box_hidden_state||\'UNKNOWN\';\n  const fmtVal=x=>x===null||x===undefined?\'—\':Number(x).toFixed(2);\n  return `<tr><td>${esc(e.date)} ${esc(e.market)}</td><td>${esc(v)}</td><td>${fmtVal(e.core46_box_price_higher_low_pct)}</td><td>${fmtVal(e.core46_box_rsi_delta_points)}</td><td>${esc(e.core46_box_pivot_confirmed_at?.slice(11,16)||\'—\')}</td><td>${fmt(e[key+\'net_pct\'])}</td><td>${fmt(e[newKey+\'net_pct\'])}</td></tr>`;\n }).join(\'\'),7);\n}\n\n\n// CORE.47.1 independent research-only recovery selection. All features available at signal close.\nfunction eligibleRecovery47(){\n const p=policy();return allEvents().filter(e=>e.core47_recovery_status===\'CONFIRMED\'&&\n  e.core47_signal_at&&Number(e.reclaim_score)>=p.minScore&&\n  Number(e.reclaim_day_trade_krw_at_signal)>=p.minCash);\n}\nfunction selectRecovery47(){\n if(!state)return {selected:[],candidates:0};\n const p=policy(),pool=eligibleRecovery47().filter(e=>e.core47_hidden_at_dip===\'MATCH\')\n  .map(e=>({e,at:e.core47_signal_at,score:Number(e.reclaim_score)}))\n  .sort((a,b)=>a.at.localeCompare(b.at)||b.score-a.score||a.e.market.localeCompare(b.e.market));\n const selected=[];\n for(const day of state.days){\n  const usedSlots=new Set(),usedCoins=new Set();let busyUntil=\'\',count=0;\n  for(const x of pool.filter(v=>v.e.date===day)){\n   if(count>=p.cap)break;\n   const slot=Math.floor(Number(x.at.slice(11,13))/6);\n   if(p.selectMode===\'slot\'&&usedSlots.has(slot))continue;\n   if(usedCoins.has(x.e.market)||(busyUntil&&x.at<busyUntil))continue;\n   selected.push(x);count++;usedSlots.add(slot);usedCoins.add(x.e.market);\n   busyUntil=x.e.core47_recovery_tp05_status===\'READY\'?(\n    x.e.core47_recovery_tp05_exit_at||x.at):day+\'T23:59:59+09:00\';\n  }\n }\n return {selected,candidates:pool.length};\n}\nfunction renderCore47(){\n if(!state)return;\n const pool=eligibleRecovery47(),picked=selectRecovery47();\n const avg=v=>v.length?v.reduce((s,x)=>s+x,0)/v.length:null;\n const variants=[[\'전량 +0.5% / 60분\',\'core47_recovery_tp05_\'],\n   [\'50·30·20 고정 / 60분\',\'core47_fixed503020_60_\'],\n   [\'50·30·20 고정 / 120분\',\'core47_fixed503020_120_\'],\n   [\'50·30·20 고정 / 180분\',\'core47_fixed503020_180_\'],\n   [\'50·30·20 추적 / 120분 0.8%\',\'core47_trail503020_120_gap08_\']];\n const stateGroup=(list,label)=>{\n  const a=list.filter(e=>e.core47_recovery_tp05_status===\'READY\');\n  const b=list.filter(e=>e.core47_fixed503020_120_status===\'READY\');\n  const v=a.map(e=>Number(e.core47_recovery_tp05_net_pct));\n  return `<tr><td>${esc(label)}</td><td>${list.length}</td><td>${a.length}</td><td>${v.filter(x=>x>0).length}</td><td>${a.length?(v.filter(x=>x>0).length/a.length*100).toFixed(1)+\'%\':\'—\'}</td><td>${fmt(avg(v))}</td><td>${fmt(avg(b.map(e=>Number(e.core47_fixed503020_120_net_pct))))}</td></tr>`;\n };\n rows(\'core47Groups\',[\n  stateGroup(pool.filter(e=>e.core47_hidden_at_dip===\'MATCH\'),\'히든 상승 확인\'),\n  stateGroup(pool.filter(e=>e.core47_hidden_at_dip===\'NO_MATCH\'),\'조건 미충족\'),\n  stateGroup(pool.filter(e=>![\'MATCH\',\'NO_MATCH\'].includes(e.core47_hidden_at_dip)),\'판단 불가\'),\n ].join(\'\'),7);\n const selected=picked.selected.map(x=>x.e);\n rows(\'core47Selected\',variants.map(([label,key])=>{\n  const d=selected.filter(e=>e[key+\'status\']===\'READY\');\n  const vals=d.map(e=>Number(e[key+\'net_pct\']));\n  return `<tr><td>${esc(label)}</td><td>${picked.candidates} / ${selected.length}</td><td>${d.length}</td><td>${vals.filter(v=>v>0).length}</td><td>${fmt(avg(vals))}</td><td>${selected.length-d.length}</td></tr>`;\n }).join(\'\'),6);\n rows(\'core47Cases\',picked.selected.map(({e})=>{\n  const v=e.core47_entry_price;\n  return `<tr><td>${esc(e.date)} ${esc(e.market)}</td><td>${esc(e.core47_signal_at?.slice(11,16)||\'—\')}</td><td>${v==null?\'—\':num(v)}</td><td>${fmt(e.core47_recovery_tp05_net_pct)}</td><td>${fmt(e.core47_fixed503020_120_net_pct)}</td><td>${fmt(e.core47_trail503020_120_gap08_net_pct)}</td></tr>`;\n }).join(\'\'),6);\n}\n\n\n// CORE.48 quality research groups and exploratory fixed bins (NEVER buy filters).\nfunction renderCore48(){\n if(!state)return;\n const all=allEvents().filter(e=>e.core48_quality_status===\'READY\');\n const eligible=eligibleRecovery47().filter(e=>e.core48_quality_status===\'READY\');\n const chosen=selectRecovery47().selected.map(x=>x.e);\n const avg=nums=>nums.length?nums.reduce((a,b)=>a+b,0)/nums.length:null;\n const summarize=(label,arr)=>{\n  const full=arr.filter(e=>e.core47_recovery_tp05_status===\'READY\');\n  const split=arr.filter(e=>e.core47_fixed503020_120_status===\'READY\');\n  const vals=full.map(e=>Number(e.core47_recovery_tp05_net_pct)).filter(Number.isFinite);\n  const extra=split.map(e=>Number(e.core47_fixed503020_120_net_pct)).filter(Number.isFinite);\n  const wins=vals.filter(x=>x>0).length;\n  return `<tr><td>${esc(label)}</td><td>${arr.length}</td><td>${vals.length}</td><td>${wins}</td><td>${vals.length?(wins/vals.length*100).toFixed(1)+\'%\':\'—\'}</td><td>${fmt(avg(vals))}</td><td>${fmt(avg(extra))}</td></tr>`;\n };\n rows(\'core48Groups\',[\n  summarize(\'전체 회복 확인\',all),\n  summarize(\'RSI 히든 MATCH\',all.filter(e=>e.core47_hidden_at_dip===\'MATCH\')),\n  summarize(\'RSI 히든 NO_MATCH\',all.filter(e=>e.core47_hidden_at_dip===\'NO_MATCH\')),\n  summarize(\'현재 선정 기준 통과\',eligible),\n  summarize(\'E 방식 최종 선정\',chosen)\n ].join(\'\'),7);\n const dim=[\n  {label:\'회복봉 종가 위치\',key:\'core48_close_location_pct\',bands:[[\'상단 70% 이상\',x=>x>=70],[\'45~70%\',x=>x>=45&&x<70],[\'45% 미만\',x=>x<45]]},\n  {label:\'회복봉 윗꼬리\',key:\'core48_upper_wick_pct_range\',bands:[[\'20% 이하\',x=>x<=20],[\'20~40%\',x=>x>20&&x<=40],[\'40% 초과\',x=>x>40]]},\n  {label:\'회복봉 포함 최근 거래량\',key:\'core48_pre3_vs_prior6_volume_ratio\',bands:[[\'1배 미만\',x=>x<1],[\'1~2배\',x=>x>=1&&x<2],[\'2배 이상\',x=>x>=2]]},\n  {label:\'눌림봉 대비 회복 종가\',key:\'core48_close_vs_dip_close_pct\',bands:[[\'+0.5% 미만\',x=>x<.5],[\'+0.5~1.5%\',x=>x>=.5&&x<1.5],[\'+1.5% 이상\',x=>x>=1.5]]}\n ];\n const bin=(label,bname,items)=>{\n  const done=items.filter(e=>e.core47_recovery_tp05_status===\'READY\');\n  const nets=done.map(e=>Number(e.core47_recovery_tp05_net_pct)).filter(Number.isFinite);\n  const wins=nets.filter(v=>v>0).length;\n  return `<tr><td>${esc(label)}</td><td>${esc(bname)}</td><td>${items.length}</td><td>${nets.length}</td><td>${wins}</td><td>${nets.length?(100*wins/nets.length).toFixed(1)+\'%\':\'—\'}</td><td>${fmt(avg(nets))}</td></tr>`;\n };\n rows(\'core48Bins\',dim.flatMap(d=>{\n   const valid=all.filter(e=>e[d.key]!==null&&e[d.key]!==undefined&&Number.isFinite(Number(e[d.key])));\n   const missing=all.filter(e=>e[d.key]===null||e[d.key]===undefined||!Number.isFinite(Number(e[d.key])));\n   return [...d.bands.map(([name,f])=>bin(d.label,name,valid.filter(e=>f(Number(e[d.key]))))),bin(d.label,\'자료 부족\',missing)];\n }).join(\'\'),7);\n const picked=new Set(chosen.map(candidateId));\n const strPercent=x=>x===null||x===undefined||!Number.isFinite(Number(x))?\'—\':Number(x).toFixed(2)+\'%\';\n rows(\'core48Cases\',eligible.slice().sort((a,b)=>String(a.core47_signal_at).localeCompare(String(b.core47_signal_at))||String(a.market).localeCompare(String(b.market))).slice(0,60).map(e=>\n  `<tr><td>${esc(e.date)} ${esc(e.market)}</td><td>${esc(e.core47_hidden_at_dip||\'UNKNOWN\')}</td><td>${strPercent(e.core48_close_location_pct)}</td><td>${strPercent(e.core48_upper_wick_pct_range)}</td><td>${e.core48_pre3_vs_prior6_volume_ratio==null?\'—\':Number(e.core48_pre3_vs_prior6_volume_ratio).toFixed(2)+\'배\'}</td><td>${strPercent(e.core48_close_vs_dip_close_pct)}</td><td>${fmt(e.core47_recovery_tp05_net_pct)}</td><td>${picked.has(candidateId(e))?\'선정\':\'—\'}</td></tr>`\n ).join(\'\'),8);\n}\n\n\nfunction buildCore49Ranks(){\n if(!state||!state.rankResults)return;\n const ranges={};const ranked={};\n for(const day of state.days){\n  const a=state.rankResults.flatMap(x=>{\n   const cash=x.cash_by_date?.[day];\n   return Number.isFinite(cash)&&cash>=0?[{market:x.market,cash,closed:x.closed_at_by_date?.[day]||\'\'}]:[];\n  }).sort((a,b)=>b.cash-a.cash||a.market.localeCompare(b.market));\n  const maps={};a.forEach((x,i)=>{maps[x.market]={rank:i+1,cash:x.cash,closed:x.closed};});\n  ranked[day]=maps;ranges[day]={known:a.length,missing:state.markets.length-a.length,\n     tasks:(state.scope===\'hist50\'?50:state.scope===\'hist51to80\'?30:state.scope===\'histall\'?a.length:80),\n     completed_at:\'KST 00:00 시점 직전 확정 09시~09시 일봉\'};\n }\n state.rankByDay=ranked;state.rankSummary=ranges;\n const tasks=[];\n for(const day of state.days){\n  const rows=Object.entries(ranked[day]).map(([market,v])=>({date:day,market,...v}))\n      .filter(x=>state.scope===\'histall\'||state.scope===\'hist50\'&&x.rank<=50||state.scope===\'hist80\'&&x.rank<=80||state.scope===\'hist51to80\'&&x.rank>=51&&x.rank<=80)\n      .sort((a,b)=>a.rank-b.rank);\n  if(!rows.length)throw Error(day+\' 과거 거래대금 조회 성공 종목이 없습니다. 순위로 테스트할 수 없습니다\');\n  if(state.scope===\'hist50\'&&rows.length<50||state.scope===\'hist80\'&&rows.length<80||state.scope===\'hist51to80\'&&rows.length<30)\n    state.rankWarnings.push(day+\' 순위 가능 종목이 목표보다 적습니다 (\'+rows.length+\'개)\');\n  ranges[day].tasks=rows.length;tasks.push(...rows);\n }\n state.tasks=tasks;\n}\nfunction core49Cooldown(pick,hours){\n const list=pick.selected.slice().sort((a,b)=>a.at.localeCompare(b.at));\n let streak=0,until=0,lastDay=null;const blocked=new Set();\n const exits=[];\n for(const x of list){\n   const at=Date.parse(x.at);if(lastDay!==null&&lastDay!==x.e.date)streak=0;lastDay=x.e.date;\n   const ready=exits.filter(t=>t.time<=at).sort((a,b)=>a.time-b.time);\n   for(const e of ready){\n     const ix=exits.indexOf(e);if(ix>=0)exits.splice(ix,1);\n     if(e.loss)streak++;else streak=0;\n     if(streak>=3){until=Math.max(until,e.time+hours*3600000);streak=0;}\n   }\n   if(at<until){blocked.add(candidateId(x.e));continue;}\n   const readyResult=x.e[x.key+\'_status\']===\'READY\';\n   const exit=Date.parse(x.e[x.key+\'_exit_at\']||\'\');\n   if(readyResult&&Number.isFinite(exit))exits.push({time:exit,loss:String(x.e[x.key+\'_exit\']||\'\').startsWith(\'SL\')});\n }\n const remaining=list.filter(x=>!blocked.has(candidateId(x.e))&&x.e[x.key+\'_status\']===\'READY\');\n const avg=remaining.length?remaining.reduce((s,x)=>s+Number(x.e[x.key+\'_net_pct\']),0)/remaining.length:null;\n return {blocked,ready:remaining.length,avg,total:list.length};\n}\n\nfunction core49Score(e){\n let score=0,known=0,missing=0;\n function add(weight,v,positive){if(v===null||v===undefined||v==="UNKNOWN"){missing++;return;}known+=weight;if(positive(v))score+=weight;}\n add(20,e.core49_rank,v=>Number(v)<=50);\n add(10,e.core49_box_ma_alignment,v=>v===\'UP\');\n add(10,e.core49_box_rsi14,v=>Number(v)>=42&&Number(v)<=65);\n for(const tf of [\'h1\',\'h4\',\'d1\']){\n   add(8,e[\'trend_\'+tf+\'_state\'],v=>v===\'UP\');\n   const cloud=e[\'core49_box_\'+tf+\'_cloud_state\'];\n   const pos=e[\'core49_box_\'+tf+\'_cloud_positive\'];\n   add(8,cloud===\'UNKNOWN\'||pos==null?null:cloud,v=>v===\'ABOVE\'&&pos===true);\n   add(4,e[\'core49_box_\'+tf+\'_ma_alignment\'],v=>v===\'UP\');\n }\n return {score,known,missing,status:known===100?\'COMPLETE\':\'PARTIAL\'};\n}\n\nfunction renderCore49(){\n if(!state)return;\n const enabled=!!state.rankTasks;const done=state.rankDone||0,rankN=state.rankTasks?.length||0;\n $(\'k49Rank\').textContent=enabled?`${done}/${rankN}`:\'순위 미사용\';\n $(\'k49Missing\').textContent=enabled?`${state.rankErrors?.length||0}건 API 실패`: \'—\';\n const rows=(id,body,n)=>$(id).innerHTML=body||`<tr><td colspan="${n}">검사 결과 없음</td></tr>`;\n rows(\'core49Coverage\',state.days.map(day=>{\n  const s=state.rankSummary?.[day];return `<tr><td>${day}</td><td>${s?.known??\'—\'}</td><td>${s?.missing??\'—\'}</td><td>${s?.tasks??\'—\'}</td><td>${s?esc(s.completed_at):\'—\'}</td></tr>`;\n }).join(\'\'),5);\n const eligible=qualifiedEvents();\n const variants=enabled?(state.scope===\'hist50\'?[[\'상위 1~50위\',[1,50]]]:state.scope===\'hist51to80\'?[[\'상위 51~80위\',[51,80]]]:[[\'상위 1~50위\',[1,50]],[\'상위 51~80위\',[51,80]],[\'상위 1~80위\',[1,80]]]):[];\n rows(\'core49RankGroups\',variants.map(([label,band])=>{\n  const p=selectForMethod(\'box\',band),s=stats(p);\n  return `<tr><td>${label}</td><td>${p.nCandidates}</td><td>${s.picked}</td><td>${s.ready}</td><td>${s.wins}</td><td>${fmt(s.avg)}</td></tr>`;\n }).join(\'\'),6);\n const pick=selectForMethod(\'box\'), formatTF=(e,tf)=>{\n  const path=`core49_box_${tf}_`,r=e[path+\'rsi14\'],cl=e[path+\'cloud_state\'],a=e[path+\'ma_alignment\'];\n  return `${r==null?\'—\':Number(r).toFixed(1)} / ${cl||\'?\'} / ${a||\'?\'}`;\n };\n rows(\'core49Signals\',pick.selected.map(x=>{\n  const e=x.e;const risk=e.core49_box_risk_to_recent_low_pct;\n  return `<tr><td>${esc(e.date+\' \'+e.market)}</td><td>${e.core49_rank??\'—\'}</td><td>${e.core49_score??\'—\'}/${e.core49_score_known??\'—\'}</td><td>${e.core49_box_rsi14==null?\'—\':Number(e.core49_box_rsi14).toFixed(1)}</td><td>${formatTF(e,\'h1\')}</td><td>${formatTF(e,\'h4\')}</td><td>${formatTF(e,\'d1\')}</td><td>${fmt(risk)}</td><td>${fmt(e[x.key+\'_net_pct\'])}</td></tr>`;\n }).join(\'\'),9);\n const cooldowns=[[\'기존 그대로\',null],[\'손절 3회 뒤 2시간\',2],[\'손절 3회 뒤 4시간\',4]];\n rows(\'core49Cooldown\',cooldowns.map(([label,h])=>{const s=h?core49Cooldown(pick,h):stats(pick);return `<tr><td>${label}</td><td>${s.total??s.picked}</td><td>${h?s.blocked.size:0}</td><td>${s.ready}</td><td>${fmt(s.avg)}</td></tr>`;}).join(\'\'),5);\n}\n\nfunction render(){if(!state)return;const ev=allEvents(),cands=qualifiedEvents(),a=selectForMethod(\'box\'),b=selectForMethod(\'watch41\'),c=selectForMethod(\'delay44\'),d=selectForMethod(\'cont44\'),sa=stats(a),sb=stats(b),sc=stats(c),sd=stats(d),p=policy();\n const ok=cands.filter(e=>e.watch41_confirm_status===\'CONFIRMED\').length,reject=cands.filter(e=>e.watch41_confirm_status===\'REJECTED\').length,unknown=cands.filter(e=>e.watch41_confirm_status===\'UNKNOWN\').length;\n $(\'kDone\').textContent=num(state.done)+\' / \'+num(state.tasks.length);\n $(\'kErrors\').textContent=num(state.results.filter(r=>r.coverage.reason).length)+\' / \'+num(state.errors.length);\n $(\'kSignals\').textContent=num(cands.length);$(\'kWatch\').textContent=`${ok} / ${reject} / ${unknown}`;\n $(\'kABuy\').textContent=sa.picked+\' / \'+sa.firstSL;$(\'kANet\').textContent=fmt(sa.avg);$(\'kANet\').className=\'n \'+(sa.avg!==null?(sa.avg>=0?\'good\':\'bad\'):\'\');\n $(\'kBBuy\').textContent=sb.picked+\' / \'+sb.firstSL;$(\'kBNet\').textContent=fmt(sb.avg);$(\'kBNet\').className=\'n \'+(sb.avg!==null?(sb.avg>=0?\'good\':\'bad\'):\'\');\n $(\'kCBuy\').textContent=sc.picked+\' / \'+sc.firstSL;$(\'kCNet\').textContent=fmt(sc.avg);$(\'kCNet\').className=\'n \'+(sc.avg!==null?(sc.avg>=0?\'good\':\'bad\'):\'\');\n $(\'kDBuy\').textContent=sd.picked+\' / \'+sd.firstSL;$(\'kDNet\').textContent=fmt(sd.avg);$(\'kDNet\').className=\'n \'+(sd.avg!==null?(sd.avg>=0?\'good\':\'bad\'):\'\');\n $(\'bar\').max=Math.max(1,state.tasks.length);$(\'bar\').value=state.done;\n rows(\'compareTable\',[{label:\'A · 즉시 BUY\',s:sa},{label:\'B · WATCH 후 BUY\',s:sb},{label:\'C · 5분 대기 후 BUY\',s:sc},{label:\'D · 5분 상승 유지 후 BUY\',s:sd}].map(x=>`<tr><td>${x.label}</td><td>${x.s.picked}</td><td>${x.s.ready}</td><td class="good">${x.s.wins}</td><td class="bad">${x.s.loss}</td><td class="bad">${x.s.firstSL}</td><td>${x.s.both}</td><td>${fmt(x.s.avg)}</td></tr>`).join(\'\'),8);\n rows(\'dailyTable\',state.days.map((day,i)=>{const cells=[a,b,c,d].flatMap(pick=>{const x=pick.byDay[i];return [`${x.selected}/${x.ready}`,`${x.wins}/${x.loss}`,fmt(x.avg)];});return \'<tr><td>\'+day+\'</td>\'+cells.map(v=>\'<td>\'+v+\'</td>\').join(\'\')+\'</tr>\';}).join(\'\'),13);\n const chosen=[...a.selected,...b.selected,...c.selected,...d.selected].sort((x,y)=>x.at.localeCompare(y.at)||x.method.localeCompare(y.method));\n const methodLabel={box:\'A · 즉시\',watch41:\'B · WATCH\',delay44:\'C · 5분 대기\',cont44:\'D · 상승 유지\'};\n rows(\'selectedTable\',chosen.map(x=>{const e=x.e,m=x.method,sl=e[m+\'_first5_sl\'],both=e[m+\'_first5_touch_both\'],v=e[x.key+\'_net_pct\'];const f5=sl?\'첫 봉 SL\':e[m+\'_first5_tp\']?\'첫 봉 TP\':e[m+\'_first5_data_ok\']?\'첫 봉 유지\':\'미확인\';return `<tr><td>${methodLabel[m]}</td><td>${esc(e.date)} ${esc(e.market)}</td><td>${esc(x.at.slice(11,16))}</td><td>${x.score}</td><td class="${sl?\'bad\':\'\'}">${f5}${both?\' · 양방향\':\'\'}</td><td>${esc(e[x.key+\'_exit\']||e[x.key+\'_status\']||\'—\')}</td><td class="${Number(v)<0?\'bad\':Number(v)>=0&&v!=null?\'good\':\'\'}">${fmt(v)}</td><td>${esc(e.box_signal_at.slice(11,16))}</td></tr>`;}).join(\'\'),8);\n const totalSets=[{label:\'A · 기존 C 전체 신호\',list:cands,key:\'box_tp\'+p.target},{label:\'B · WATCH 확인 후 진입\',list:cands.filter(e=>e.watch41_confirm_status===\'CONFIRMED\'),key:\'watch41_tp\'+p.target},{label:\'C · 5분 무조건 지연\',list:cands.filter(e=>e.delay44_confirm_status===\'CONFIRMED\'),key:\'delay44_tp\'+p.target},{label:\'D · 5분 상승 유지 확인\',list:cands.filter(e=>e.cont44_confirm_status===\'CONFIRMED\'),key:\'cont44_tp\'+p.target}];\n rows(\'signalTable\',totalSets.map(x=>{const rd=x.list.filter(e=>e[x.key+\'_status\']===\'READY\'),won=rd.filter(e=>Number(e[x.key+\'_net_pct\'])>0).length,avg=rd.length?rd.reduce((s,e)=>s+Number(e[x.key+\'_net_pct\']),0)/rd.length:null;return `<tr><td>${x.label}</td><td>${x.list.length}</td><td>${rd.length}</td><td>${won}</td><td>${fmt(avg)}</td></tr>`;}).join(\'\'),5);\n const aKey=\'box_tp\'+p.target,cKey=\'delay44_tp\'+p.target;\n const paired=cands.filter(e=>e[aKey+\'_status\']===\'READY\'&&e[cKey+\'_status\']===\'READY\');\n const pairGroups=[[\'전체 짝맞춤\',paired],[\'상승 유지\',paired.filter(e=>e.core43_next5_continuation===\'CONTINUE\')],[\'되밀림\',paired.filter(e=>e.core43_next5_continuation===\'RETRACE\')]];\n rows(\'core44PairTable\',pairGroups.map(([name,items])=>{const av=items.length?items.reduce((s,e)=>s+Number(e[aKey+\'_net_pct\']),0)/items.length:null;const cv=items.length?items.reduce((s,e)=>s+Number(e[cKey+\'_net_pct\']),0)/items.length:null;return `<tr><td>${name}</td><td>${items.length}</td><td>${fmt(av)}</td><td>${fmt(cv)}</td><td>${items.length?fmt(cv-av):\'—\'}</td></tr>`;}).join(\'\'),5);\n render43(cands,a.selected);\n renderCore45();\n renderCore46(cands,a.selected.map(x=>x.e));\n renderCore47();\n renderCore48();\n renderCore49();\n const states=[\'UP\',\'SIDE\',\'DOWN\',\'UNKNOWN\'],timeframes=[[\'d1\',\'일봉\'],[\'h4\',\'4시간\'],[\'h1\',\'1시간\']],sel=a.selected.map(x=>x.e);\n rows(\'trendTable\',timeframes.flatMap(([tf,label])=>states.map(st=>{\n  const grp=sel.filter(e=>(e[\'trend_\'+tf+\'_state\']||\'UNKNOWN\')===st);\n  const done=grp.filter(e=>e[\'box_tp\'+p.target+\'_status\']===\'READY\');\n  const wins=done.filter(e=>Number(e[\'box_tp\'+p.target+\'_net_pct\'])>0);\n  const avg=done.length?done.reduce((s,e)=>s+Number(e[\'box_tp\'+p.target+\'_net_pct\']),0)/done.length:null;\n  return `<tr><td>${label}</td><td>${st===\'UP\'?\'상승\':st===\'DOWN\'?\'하락\':st===\'SIDE\'?\'중립\':\'미확인\'}</td><td>${grp.length}</td><td>${done.length}</td><td class="good">${wins.length}</td><td class="bad">${grp.filter(e=>e.box_first5_sl).length}</td><td>${fmt(avg)}</td></tr>`;\n })).join(\'\'),7);\n rows(\'trendCases\',a.selected.map(x=>{const e=x.e,v=e[x.key+\'_net_pct\'];const st=tf=>{const s=e[\'trend_\'+tf+\'_state\']||\'UNKNOWN\';const r=e[\'trend_\'+tf+\'_reason\'];return esc(s===\'UP\'?\'상승\':s===\'DOWN\'?\'하락\':s===\'SIDE\'?\'중립\':\'미확인\')+(s===\'UNKNOWN\'&&r?\' (\'+esc(r)+\')\':\'\');};\n  return `<tr><td>${esc(e.date)} ${esc(e.market)}</td><td>${esc(e.box_signal_at.slice(11,16))}</td><td>${st(\'d1\')}</td><td>${st(\'h4\')}</td><td>${st(\'h1\')}</td><td class="${Number(v)<0?\'bad\':Number(v)>=0&&v!=null?\'good\':\'\'}">${fmt(v)}</td><td>${e.box_first5_sl?\'예\':\'아니오\'}</td></tr>`;\n }).join(\'\'),7);\n rows(\'allTable\',cands.slice(0,100).map(e=>`<tr><td>${esc(e.market)}</td><td>${esc(e.box_signal_at.slice(11,16))}</td><td>${esc(e.watch41_confirm_at?.slice(11,16)||\'—\')}</td><td>${esc(e.watch41_confirm_status)}</td><td>${fmt(e[\'box_tp\'+p.target+\'_net_pct\'])}</td><td>${fmt(e[\'watch41_tp\'+p.target+\'_net_pct\'])}</td></tr>`).join(\'\'),6);\n}\nfunction status(t){$(\'status\').textContent=t;}\nfunction setRunning(x){running=x;$(\'run\').disabled=x;$(\'pause\').disabled=!x;KEYSET.forEach(id=>$(id).disabled=x);}\n$(\'pause\').onclick=()=>{paused=true;status(\'일시 중지 요청 · 진행 중인 작업만 완료합니다\');};\n[\'target\',\'cap\',\'minScore\',\'minCash\',\'selectMode\'].forEach(id=>$(id).addEventListener(\'change\',render));\n\n$(\'run\').onclick=async()=>{if(running)return;\n try{\n  const c=cfg(),key=JSON.stringify(c),isHist=c.scope!==\'all\';\n  if(!state||state.key!==key||(state.tasks.length>0&&state.cursor===state.tasks.length)){\n   const r=await fetch(\'/api/v2core/universe\').then(x=>x.json());\n   if(!r.ok||!Array.isArray(r.items))throw Error(r.error||\'KRW 종목목록 조회 실패\');\n   const markets=[...new Set(r.items)];\n   const rankTasks=isHist?markets.map(market=>({market})):[];\n   state={key,...c,markets,rankTasks,rankCursor:0,rankDone:0,rankResults:[],rankErrors:[],rankWarnings:[],\n      rankByDay:{},rankSummary:{},tasks:isHist?[]:c.days.flatMap(date=>markets.map(market=>({date,market}))),\n      cursor:0,done:0,results:[],errors:[]};\n  }\n  paused=false;setRunning(true);render();$(\'csv\').disabled=false;\n  if(isHist&&state.rankCursor<state.rankTasks.length){\n   status(`과거 일봉 거래대금 조회 ${state.rankDone}/${state.rankTasks.length} · 순위가 확정된 뒤 5분봉 검사를 시작합니다`);\n   async function rankWorker(){while(!paused&&state.rankCursor<state.rankTasks.length){\n    const task=state.rankTasks[state.rankCursor++];\n    try{const out=await api(\'/api/v2core/core49/historical-cash\',{market:task.market,days:state.days});state.rankResults.push(out);}\n    catch(e){state.rankErrors.push({market:task.market,dates:state.days,error:String(e.message||e)});}\n    finally{state.rankDone++;if(state.rankDone%8===0||state.rankDone===state.rankTasks.length){render();status(`과거 순위 조회 ${state.rankDone}/${state.rankTasks.length} · 오류 ${state.rankErrors.length}`);}}\n   }}\n   await Promise.all(Array.from({length:Math.min(4,state.rankTasks.length)},()=>rankWorker()));\n   if(paused){status(\'과거 순위 조회 일시 중지 · 같은 설정으로 이어서 가능\');return;}\n  }\n  if(isHist&&state.tasks.length===0)buildCore49Ranks();\n  let last=0;\n  async function scanWorker(){while(!paused&&state.cursor<state.tasks.length){\n   const task=state.tasks[state.cursor++];\n   try{const out=await api(\'/api/v2core/mtf42/analyze\',{...task,fee:state.fee,slip:state.slip});\n    const r=out.result;\n    if(isHist){r.core49_rank=task.rank;r.core49_prior_day_cash=task.cash;r.core49_prior_close_at=task.closed;\n      for(const e of r.events||[]){e.core49_rank=task.rank;e.core49_prior_day_cash=task.cash;\n       e.core49_rank_band=task.rank<=50?\'RANK_1_50\':task.rank<=80?\'RANK_51_80\':\'RANK_81_PLUS\';\n       e.core49_rank_reference_closed_at=task.closed;const s=core49Score(e);e.core49_score=s.score;e.core49_score_known=s.known;e.core49_score_status=s.status;}}\n    state.results.push(r);\n   }catch(e){state.errors.push({...task,error:String(e.message||e)});}\n   finally{state.done++;if(state.done-last>=8||state.done===state.tasks.length){last=state.done;render();status(`${state.done}/${state.tasks.length} 검사 중 · 오류 ${state.errors.length}`);}}\n  }}\n  await Promise.all(Array.from({length:Math.min(4,state.tasks.length)},()=>scanWorker()));\n  render();status(paused?\'일시 중지 · 같은 설정으로 이어서 검사 가능\':`완료 ${state.done}/${state.tasks.length} · 기존 C 신호 ${qualifiedEvents().length}건 · CSV 저장 가능`+(state.rankWarnings.length?\' · 순위 누락 경고 확인\':\'\'));\n }catch(e){status(\'⚠️ \'+String(e.message||e));}\n finally{setRunning(false);$(\'csv\').disabled=!state||(!state.results.length&&!state.errors.length);}\n};\nfunction csvVal(v){let s=(v!==null&&typeof v===\'object\')?JSON.stringify(v):String(v??\'\');if(/^[=+@-]/.test(s)&&isNaN(Number(s)))s="\'"+s;return \'"\'+s.replace(/"/g,\'""\')+\'"\';}\n$(\'csv\').onclick=()=>{if(!state)return;const p=policy(),a=selectForMethod(\'box\'),b=selectForMethod(\'watch41\'),c=selectForMethod(\'delay44\'),d=selectForMethod(\'cont44\'),aIds=new Set(a.selected.map(x=>candidateId(x.e))),bIds=new Set(b.selected.map(x=>candidateId(x.e))),cIds=new Set(c.selected.map(x=>candidateId(x.e))),dIds=new Set(d.selected.map(x=>candidateId(x.e))),rIds=new Set(selectRecovery47().selected.map(x=>candidateId(x.e)));\n const base={version:CODE_VERSION,date_from:state.days[0],date_to:state.days.at(-1),scope:state.scope,fee_pct_side:state.fee,slippage_pct_side:state.slip,scan_complete:state.done===state.tasks.length,scan_done:state.done,scan_tasks:state.tasks.length,scan_errors:state.errors.length,selection_comparison:\'CORE36_BOX_MTF42_BREAKOUT43_WATCH41_DELAY44_CONT44\',selection_mode:p.selectMode,min_score:p.minScore,min_trade_krw:p.minCash,daily_max:p.cap,target_net_pct:p.target===\'05\'?.5:.3,stop_gross_pct:.7,horizon_min:60,pump_rule:\'PRIOR_24_COMPLETED_5M_LOW_PLUS5\',sma:\'SMA5_SMA20_5MIN\',simulation:\'NEXT_OPEN_OHLC_STOP_FIRST\',watch_confirm_rule:\'NEXT_5M_CLOSE_ABOVE_PRE_BOX_UPPER_AND_SMA5\',baseline:\'CORE36_C_SIGNAL_UNCHANGED\',mtf_rule:\'D1_H4_H1_SMA5_SMA20_SLOPE4_COMPLETED_ONLY_NO_TRADE_FILTER\',core43_rule:\'PRE3_VS_PRIOR6_AND_BREAKOUT_SHAPE_SIGNAL_TIME_NEXT5_OBSERVATION_ONLY_NO_TRADE_FILTER\',core44_rule:\'DELAY5_NEXT_OPEN_C_ALWAYS_OR_D_NEXT5_CLOSE_ABOVE_BREAKOUT_AND_PRE_UPPER_REPRICE_NEW_60M\',core45_rule:\'UNCHANGED_A_SAME_ENTRY_503020_NET_05_12_30_BE_TRAIL_ACTIVATE_AT_30\',core45_horizons:\'60/120/180\',core45_trail_gaps:\'0.6/0.8/1.0\',core46_rule:\'WILDER_RSI14_CONFIRMED_PRICE_PIVOT_L2_R2_HL_GE_0.10_RSI_LL_GE_2_MAX_AGE_60_MIN_DIAG_ONLY\',core47_rule:\'DIP_HIDDEN_CONFIRMED_AT_DIP_AND_FIRST_SUBSEQUENT_SMA5_RECLAIM_NEXT_OPEN_DIAG_ONLY\',core47_entry:\'SEPARATE_INDEPENDENT_SELECTION_NOT_LIVE_BUY\',core47_splits:\'FIXED503020_OR_TRAIL_AT_NET3_60_120_180_MIN\',core48_rule:\'RECLAIM_CLOSE_ONLY_SHAPE_VOL3_VS6_CHASE_PRICE_DIAG_NO_BUY_FILTER\',core49_rule:\'PREVIOUS_COMPLETED_09_TO_09_DAY_CASH_AT_KST_MIDNIGHT_RANK_DIAG_ONLY_NO_BUY_CHANGE\',core49_rank_lookup_total:state.rankTasks?.length||0,core49_rank_lookup_done:state.rankDone||0,core49_rank_api_errors:state.rankErrors?.length||0};\n const out=[];for(const day of state.days){const s=state.rankSummary?.[day];if(s)out.push({...base,row_type:\'CORE49_RANK_SUMMARY\',date:day,core49_ranks_known:s.known,core49_ranks_missing:s.missing,core49_tested_markets:s.tasks,core49_rank_rule:s.completed_at});}\n for(const re of state.rankErrors||[])out.push({...base,row_type:\'CORE49_RANK_API_ERROR\',market:re.market,dates:re.dates,error:re.error});\n for(const [method,pick] of [[\'box\',a],[\'watch41\',b],[\'delay44\',c],[\'cont44\',d]])for(const d of pick.byDay)out.push({...base,row_type:\'DAY_SUMMARY\',comparison_method:method,...d});\n const cool2=core49Cooldown(a,2).blocked,cool4=core49Cooldown(a,4).blocked;\n for(const r of state.results){out.push({...base,row_type:\'MARKET_COVERAGE\',date:r.date,market:r.market,core49_rank:r.core49_rank,core49_prior_day_cash:r.core49_prior_day_cash,core49_prior_close_at:r.core49_prior_close_at,...r.coverage});for(const e of r.events||[]){const id=candidateId(e);out.push({...base,row_type:\'PULLBACK_EVENT\',core49_cooldown2h_blocked:cool2.has(id),core49_cooldown4h_blocked:cool4.has(id),selected_for_daily_limit:aIds.has(id),selected_baseline:aIds.has(id),selected_watch41:bIds.has(id),selected_delay44:cIds.has(id),selected_cont44:dIds.has(id),selected_recovery47:rIds.has(id),...e});}}\n for(const e of state.errors)out.push({...base,row_type:\'API_ERROR\',...e});\n const cols=[...new Set([\'row_type\',\'version\',\'date\',\'market\',\'core49_rank\',\'core49_rank_band\',\'core49_prior_day_cash\',\'core49_rank_reference_closed_at\',\'core49_box_rsi14\',\'core49_box_ma_alignment\',\'core49_box_risk_to_recent_low_pct\',\'core49_box_h1_cloud_state\',\'core49_box_h4_cloud_state\',\'core49_box_d1_cloud_state\',\'core49_cooldown2h_blocked\',\'core49_cooldown4h_blocked\',\'core43_pre_volume_ratio_3_vs_prior6\',\'core43_signal_hypothesis\',\'core43_next5_continuation\',\'core43_next5_close_vs_breakout_pct\',\'core46_box_hidden_state\',\'core46_box_hidden_reason\',\'core46_box_pivot_confirmed_at\',\'core46_box_price_higher_low_pct\',\'core46_box_rsi_delta_points\',\'core48_quality_status\',\'core48_close_location_pct\',\'core48_upper_wick_pct_range\',\'core48_body_pct_range\',\'core48_pre3_vs_prior6_volume_ratio\',\'core48_close_vs_dip_close_pct\',\'core48_reclaim_vs_prev_close_pct\',\'core47_hidden_at_dip\',\'core47_recovery_status\',\'core47_signal_at\',\'core47_entry_at\',\'core47_recovery_tp05_net_pct\',\'core47_fixed503020_120_net_pct\',\'core47_trail503020_120_gap08_net_pct\',\'selected_recovery47\',\'trend_d1_state\',\'trend_h4_state\',\'trend_h1_state\',\'trend_d1_reason\',\'trend_h4_reason\',\'trend_h1_reason\',\'selected_baseline\',\'selected_watch41\',\'selected_delay44\',\'selected_cont44\',\'comparison_method\',\'box_signal_at\',\'watch41_confirm_at\',\'watch41_signal_at\',\'watch41_confirm_status\',\'watch41_confirm_reason\',\'box_score\',\'box_day_trade_krw_at_signal\',\'box_tp05_status\',\'box_tp05_net_pct\',\'box_first5_sl\',\'watch41_tp05_status\',\'watch41_tp05_net_pct\',\'watch41_first5_sl\',\'watch41_first5_touch_both\',\'delay44_signal_at\',\'delay44_confirm_status\',\'delay44_tp05_entry_price\',\'delay44_tp05_net_pct\',\'cont44_signal_at\',\'cont44_confirm_status\',\'cont44_tp05_entry_price\',\'cont44_tp05_net_pct\',...out.flatMap(x=>Object.keys(x))])];\n const csv=\'\ufeff\'+[cols.join(\',\'),...out.map(r=>cols.map(c=>csvVal(r[c])).join(\',\'))].join(\'\\r\\n\');const u=URL.createObjectURL(new Blob([csv],{type:\'text/csv;charset=utf-8\'}));const link=document.createElement(\'a\');link.href=u;link.download=`CORE49_PRETRADE_RANK80_${state.days[0]}_${state.days.at(-1)}_${state.scope}_KRW.csv`;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(u),3000);\n};\n</script></main></body></html>'

# CORE.42 higher-timeframe specification: restored from CORE.46.
# Used only for completed-candle D1/H4/H1 diagnostics, NOT BUY filtering.
_TFS = {
    'h1': ('/v1/candles/minutes/60', timedelta(hours=1), 100),
    'h4': ('/v1/candles/minutes/240', timedelta(hours=4), 90),
    'd1': ('/v1/candles/days', timedelta(days=1), 90),
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


    @app.post('/api/v2core/core49/historical-cash')
    def core49_historical_cash_route():
        try:
            raw=request.get_json(silent=True)
            if not isinstance(raw,dict):raise ValueError('잘못된 검사 요청')
            market=raw.get('market')
            dates=raw.get('days')
            if not isinstance(market,str) or not MARKET_PATTERN.fullmatch(market):
                raise ValueError('KRW 종목 형식 오류')
            if not isinstance(dates,list) or not 1<=len(dates)<=5:
                raise ValueError('날짜는 1~5개까지 허용')
            unique_dates=[]
            for d in dates:
                checked=_check_cfg({'date':d,'fee':0,'slip':0},kst)['day']
                unique_dates.append(checked)
            if (max(unique_dates)-min(unique_dates)).days > 15:
                raise ValueError('날짜 간격이 너무 큽니다')
            if market not in get_markets():
                raise ValueError('현재 거래가능 KRW 종목이 아닙니다')
            key=('CORE49_CASH',market,tuple(sorted(set(unique_dates))))
            with lock:
                prev=cache['results'].get(key)
            if prev is not None and time.monotonic()-prev[0]<1200:
                return jsonify(ok=True,version=VERSION,**prev[1],cached=True)
            vals,stamps=_core49_historical_cash(api_get,market,unique_dates,kst)
            ans={'market':market,'cash_by_date':vals,'closed_at_by_date':stamps,
                 'rule':'LAST_24H_09_TO_09_COMPLETED_BEFORE_KST_DATE_00'}
            with lock:
                if len(cache['results'])>3000:cache['results'].clear()
                cache['results'][key]=(time.monotonic(),ans)
            return jsonify(ok=True,version=VERSION,**ans,cached=False)
        except Exception as e:
            return jsonify(ok=False,error=f'{type(e).__name__}: {e}'[:220]),400

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
            if any(e.get('box_signal_at') or e.get('core47_signal_at') for e in report['events']):
                # Only C-signal-bearing markets need three extra timeframes.
                histories = fetch_mtf_history(api_get, market, cfg['day'], kst)
                attach_mtf_diagnostics(report, histories)
                _core49_attach_mtf(report, histories)
            else:
                report['coverage']['core49_quality_mtf_events']=0
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
