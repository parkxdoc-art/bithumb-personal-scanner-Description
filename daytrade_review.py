"""V2 CORE.51 LIGHT — strict breakout A/B research; CORE.50 diagnostics retained.

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

VERSION = 'V2.CORE.51-LIGHT-STRICT-BOX-AB-RISK-RESEARCH'
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



# ---- CORE.50 INDEPENDENT DIAGNOSTIC: possible accumulation, not proof of intent ----
# Based DIRECTLY on the working CORE.47.1.  CORE.48/49 research features
# (recovery-quality score, historical TOP80 ranking, Ichimoku/cooldown) are NOT
# included. No existing BUY, E entry, score, selection or exit policy is changed.
# At spike close only pre/current candles are used. Pullback and reclaim are
# assessed LATER, using completed candles; virtual fills occur NEXT candle open.
CORE50_VOL_MULT = 2.5       # exploratory hypotheses, NOT calibrated thresholds
CORE50_CASH_MULT = 2.0
CORE50_MIN_BAR_CASH = 30_000_000.0  # 5-min, distinct from day turnover gate
CORE50_PRE_BARS = 20
CORE50_MAX_FOLLOW = 12    # 60 min from observed spike close
CORE50_SPIKE_GAP = 6      # separate spike events by 30 min on same coin


def _core50_accumulation_scan(bars, day, market, fee=.25, slip=0):
    """Tag 5m *suspected* accumulation and simulate subsequent tradable rebound.

    Each spike is observable when its own bar CLOSES. Confirmation requires
    a separate completed pullback + SMA5-recovery candle, and entry is the
    following candle's OPEN. Missing/broken bars => UNKNOWN, never a win.
    A high-volume candle may be *distribution*, hence quality labels are
    descriptive, not proof of accumulation or a live trading gate.
    """
    if not bars:
        return []
    tz = bars[0]['t'].tzinfo
    start = datetime.combine(day, dt_time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    first = next((k for k,b in enumerate(bars) if b['t'] >= start), len(bars))
    last = next((k for k,b in enumerate(bars) if b['t'] >= end), len(bars))
    result = []
    prev_spike = -9999
    day_trade_so_far = 0.
    for i in range(first,last):
        b = bars[i]
        if b['t'].time() >= dt_time(21,30):
            break  # leave enough after-day data for 180-minute exit variants
        day_trade_so_far += b['cash']
        if i - prev_spike < CORE50_SPIKE_GAP or not _contiguous(bars,i,CORE50_PRE_BARS+1):
            continue
        old = bars[i-CORE50_PRE_BARS:i]
        avgv = sum(x['v'] for x in old)/CORE50_PRE_BARS
        avgcash = sum(x['cash'] for x in old)/CORE50_PRE_BARS
        if avgv <= 0 or avgcash <= 0:
            continue
        volume_ratio = b['v']/avgv
        cash_ratio = b['cash']/avgcash
        if (volume_ratio < CORE50_VOL_MULT or cash_ratio < CORE50_CASH_MULT or
                b['cash'] < CORE50_MIN_BAR_CASH):
            continue
        span = b['h']-b['l']
        if span <= 0:
            continue
        close_location = (b['c']-b['l'])/span
        upper_wick = (b['h']-max(b['o'],b['c']))/span
        lower_wick = (min(b['o'],b['c'])-b['l'])/span
        quality = ('SUPPORTIVE' if close_location >= .60 and upper_wick <= .30 else
                   'UPPER_WICK_RISK' if upper_wick >= .40 else 'MIXED')
        prev_spike = i
        event = {
            'market':market,'date':day.isoformat(),
            'acc50_version':'V2.CORE.50-LIGHT-ACCUMULATION-CANDLE-RESEARCH',
            'acc50_rule':'5M_CLOSE_VOLUME_GE2.5X_CASH_GE2X_BAR_CASH_GE30M',
            'acc50_spike_at':(b['t']+FIVE).isoformat(),
            'acc50_spike_index':i,
            'acc50_spike_open':round(b['o'],10),
            'acc50_spike_high':round(b['h'],10),
            'acc50_spike_low':round(b['l'],10),
            'acc50_spike_close':round(b['c'],10),
            'acc50_spike_cash_krw':round(b['cash'],2),
            'acc50_day_cash_at_spike':round(day_trade_so_far,2),
            'acc50_pre20_volume_mean':round(avgv,8),
            'acc50_pre20_cash_mean':round(avgcash,2),
            'acc50_volume_ratio':round(volume_ratio,4),
            'acc50_cash_ratio':round(cash_ratio,4),
            'acc50_close_location_pct':round(close_location*100,3),
            'acc50_upper_wick_pct':round(upper_wick*100,3),
            'acc50_lower_wick_pct':round(lower_wick*100,3),
            'acc50_body_pct_of_range':round(abs(b['c']-b['o'])/span*100,3),
            'acc50_candle_quality':quality,
            'acc50_status':'UNKNOWN',
            'acc50_reason':'',
            'acc50_pullback_at':'',
            'acc50_pullback_min_low':None,
            'acc50_pullback_vol_vs_spike':None,
            'acc50_confirm_at':'',
            'acc50_entry_at':'',
            'acc50_entry_price':None,
            'acc50_day_trade_krw_at_signal':None,
            'acc50_tp05_status':'NO_SIGNAL',
        }
        # These are purely *future* outcome labels, never spike-time inputs.
        for steps in (3,6,12):
            key=f'acc50_future_{steps*5}m_close_pct'
            if i+steps < len(bars) and _contiguous(bars,i+steps,steps+1):
                event[key]=round(_price_pct(bars[i+steps]['c'],b['c']),4)
            else:
                event[key]=None

        pulled = False
        pullback_i = None
        min_low = b['l']
        status = None
        for j in range(i+1,i+CORE50_MAX_FOLLOW+1):
            if j >= len(bars) or bars[j]['t']-bars[j-1]['t'] != FIVE:
                status = ('UNKNOWN','FUTURE_5M_CANDLE_MISSING_OR_GAP')
                break
            candle = bars[j]
            min_low = min(min_low,candle['l'])
            # A 0.5% break below spike LOW invalidates support hypothesis.
            if candle['l'] < b['l']*.995:
                status = ('SUPPORT_BROKEN','SPIKE_LOW_BREAK_MORE_THAN_0.5PCT')
                break
            # Retest must occur while the first eight candles are completing.
            if j-i <= 8 and candle['l'] <= b['c']*.998 and candle['c'] < b['c']:
                pulled = True
                if pullback_i is None:
                    pullback_i = j
            if not pulled or j-i < 2 or j <= pullback_i:
                continue
            sma5 = _sma(bars,j,5)
            if sma5 is None:
                continue
            # Calm volume is defined by completed bars STRICTLY BEFORE reclaim.
            calm = bars[max(i+1,j-3):j]
            calm_ratio = (sum(x['v'] for x in calm)/len(calm)/b['v']) if calm and b['v']>0 else None
            rng = candle['h']-candle['l']
            rel = ((candle['c']-candle['l'])/rng) if rng>0 else 0
            if (calm_ratio is not None and calm_ratio <= .80 and
                    candle['c'] > sma5 and candle['c'] > bars[j-1]['c'] and
                    candle['c'] >= b['c']*.995 and rel>=.55):
                event.update(
                    acc50_status='CONFIRMED',
                    acc50_reason='PULLBACK_SUPPORT_VOLUME_COOL_AND_SMA5_RECLAIM',
                    acc50_pullback_at=(bars[pullback_i]['t']+FIVE).isoformat(),
                    acc50_pullback_min_low=round(min_low,10),
                    acc50_pullback_vol_vs_spike=round(calm_ratio,4),
                    acc50_confirm_at=(candle['t']+FIVE).isoformat(),
                    acc50_day_trade_krw_at_signal=round(sum(x['cash'] for x in bars[first:j+1]),2),
                )
                entry_i = _next_entry(bars,j)
                if entry_i is None:
                    event['acc50_reason']='NEXT_ENTRY_OPEN_MISSING'
                else:
                    event['acc50_entry_at']=bars[entry_i]['t'].isoformat()
                    event['acc50_entry_price']=bars[entry_i]['o']
                full = _exit_sim(bars,entry_i,fee,slip,.5)
                for k,v in full.items():event[f'acc50_tp05_{k}']=v
                for name,sizes,targets,horizon,gap in _CORE45_VARIANTS:
                    sim=_core45_split_exit(bars,entry_i,fee,slip,sizes=sizes,
                                           targets=targets,horizon=horizon,trail_gap=gap)
                    for k,v in sim.items():event[f'acc50_{name}_{k}']=v
                status = ('CONFIRMED',event['acc50_reason'])
                break
        if status is None:
            status = ('NO_RECOVERY' if pulled else 'NO_PULLBACK',
                      'NO_SMA5_RECLAIM_WITHIN_60M' if pulled else 'NO_SUPPORT_RETEST_WITHIN_60M')
        if event['acc50_status'] != 'CONFIRMED':
            event.update(acc50_status=status[0],acc50_reason=status[1])
            if pullback_i is not None:
                event['acc50_pullback_at']=(bars[pullback_i]['t']+FIVE).isoformat()
                event['acc50_pullback_min_low']=round(min_low,10)
        result.append(event)
    return result


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
    result={'market':market,'date':day.isoformat(),'coverage':coverage,'events':[], 'accumulation_events':[]}
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
        if st['box_i'] is not None:
            sim={'exit':e.get('box_tp05_exit',''), 'bars_held':e.get('box_tp05_bars_held')}
            e.update(_box_firstbar_diagnostics(rows,st['box_i'],sim,fee,slip))
            e.update(_core43_breakout_diagnostics(rows,st['box_i'],e))
            e.update(_watch41_after_box(rows,st['box_i'],
                     e['box_pre_upper'],fee,slip))
            e.update(_delay44_after_box(rows,st['box_i'],fee,slip,
                     e.get('core43_next5_continuation','UNKNOWN')))
            _add_core45_exit_research(e,rows,st['box_i'],fee,slip)
        if st['reclaim_i'] is not None: coverage['reclaim_setups']+=1
        if st['box_i'] is not None: coverage['box_break_setups']+=1
    coverage['core47_recovery_events']=sum(e.get('core47_recovery_status')=='CONFIRMED' for e in events)
    coverage['core47_hidden_recovery_events']=sum(e.get('core47_recovery_status')=='CONFIRMED' and e.get('core47_hidden_at_dip')=='MATCH' for e in events)
    coverage['core46_hidden_box_matches']=sum(e.get('core46_box_hidden_state')=='MATCH' for e in events)
    coverage['core46_hidden_box_unknown']=sum(e.get('core46_box_hidden_state')=='UNKNOWN' for e in events)
    coverage['delay44_signal_events'] = sum(bool(e.get('delay44_signal_at')) for e in events)
    coverage['cont44_confirmed_events'] = sum(e.get('cont44_confirm_status') == 'CONFIRMED' for e in events)
    coverage['events']=len(events)
    if coverage['contiguous_scan_bars']==0:coverage['reason']='연속 5분봉 부족'
    result['events']=events
    # Diagnostics are separate; core C events, scores and daily selection unchanged.
    result['accumulation_events']=_core50_accumulation_scan(rows,day,market,fee,slip)
    coverage['acc50_spike_events']=len(result['accumulation_events'])
    coverage['acc50_confirmed_events']=sum(x.get('acc50_status')=='CONFIRMED'
        for x in result['accumulation_events'])
    return result


# CORE.51: independent strict 5-minute box-breakout experiment.
# Existing CORE.47.1 BUY and CORE.50 accumulation computations are not changed.
B51_MIN_BOX=12
B51_MAX_BOX=24
B51_MIN_ATR_WIDTH=1.5
B51_MAX_ATR_WIDTH=4.0
B51_VOL_RATIO=2.0
B51_MIN_BODY=0.60
B51_MAX_WICK=0.20
B51_MAX_BREAK_PCT=0.80
B51_HARD_STOP_PCT=1.0
B51_TARGET_R=1.5
B51_HOLD_BARS=8


def _core51_atr(bars, i, n=14):
    # Only the last n COMPLETED bars ending i-1; no breakout-bar information.
    if i < n+1 or not _contiguous(bars, i-1, n+1): return None
    t=[]
    for j in range(i-n,i):
        b,prev=bars[j],bars[j-1]
        t.append(max(b['h']-b['l'],abs(b['h']-prev['c']),abs(b['l']-prev['c'])))
    return sum(t)/n


def _core51_box(bars,i,atr):
    # Longest eligible past box is chosen first, deterministically.
    if atr is None or atr<=0: return None
    for n in range(B51_MAX_BOX,B51_MIN_BOX-1,-1):
        if not _contiguous(bars,i-1,n):continue
        window=bars[i-n:i]
        upper=max(x['h'] for x in window)
        lower=min(x['l'] for x in window)
        aw=(upper-lower)/atr
        if not B51_MIN_ATR_WIDTH<=aw<=B51_MAX_ATR_WIDTH:continue
        tol=max(atr*.20,upper*.0005)
        touches=[j for j,b in enumerate(window) if b['h']>=upper-tol]
        if not touches or not any(touches[k]-touches[j]>=2 for j in range(len(touches)) for k in range(j+1,len(touches))):continue
        return {'upper':upper,'lower':lower,'bars':n,'atr_width':aw,'touches':len(touches)}
    return None


def _core51_scan(rows,day,market,fee=.25,slip=.15):
    begin=datetime.combine(day,dt_time.min,tzinfo=rows[0]['t'].tzinfo) if rows else None
    end=begin+timedelta(days=1) if begin else None
    out=[];last=-100
    for i,b in enumerate(rows):
        if b['t']<begin or b['t']>=end:continue
        if b['t'].time()>=dt_time(22,30):continue
        if i-last<12 or not _contiguous(rows,i,26):continue
        atr=_core51_atr(rows,i)
        box=_core51_box(rows,i,atr)
        if box is None:continue
        upper=box['upper']
        if not (b['c']>upper*1.0003 and rows[i-1]['c']<=upper and b['c']>b['o']):continue
        last=i
        span=b['h']-b['l']
        body=(b['c']-b['o'])/span if span>0 else 0
        wick=(b['h']-b['c'])/span if span>0 else 1
        vavg=sum(x['v'] for x in rows[i-20:i])/20
        volx=b['v']/vavg if vavg>0 else 0
        excess=_price_pct(b['c'],upper)
        checks={'vol_2x':volx>=B51_VOL_RATIO,
                'body_60':body>=B51_MIN_BODY,
                'wick_20':wick<=B51_MAX_WICK,
                'chase_08':excess<=B51_MAX_BREAK_PCT}
        # All shape/volume observations are available at the breakout CLOSE.
        event={'date':day.isoformat(),'market':market,
               'b51_breakout_at':(b['t']+FIVE).isoformat(),
               'b51_box_top':round(upper,10),'b51_box_low':round(box['lower'],10),
               'b51_box_bars':box['bars'],'b51_box_touches':box['touches'],
               'b51_box_width_atr':round(box['atr_width'],5),
               'b51_volume_vs_pre20':round(volx,5),
               'b51_body_ratio':round(body,5),'b51_upper_wick_ratio':round(wick,5),
               'b51_excess_pct':round(excess,5),
               'b51_preliminary_pass':all(checks.values()),
               'b51_fail_reasons':','.join(k for k,v in checks.items() if not v),
               'b51_h1_state':'UNKNOWN','b51_h1_reason':'UNFETCHED',
               'b51_full_pass':False}
        # A = first completed 5m candle after breakout holds above top, next open.
        a_confirm=None
        if i+1<len(rows) and rows[i+1]['t']-b['t']==FIVE:
            if rows[i+1]['c']>upper:a_confirm=i+1
        # B = first of next 6 completed candles retesting top with bullish close.
        b_confirm=None
        for j in range(i+1,min(i+7,len(rows))):
            if rows[j]['t']-rows[j-1]['t']!=FIVE:break
            c=rows[j]
            if c['c']<upper*.9975:break  # retest failed; cannot look beyond breakdown
            if (upper*.9975<=c['l']<=upper*1.0015 and c['c']>upper
                    and c['c']>c['o']):
                b_confirm=j;break
        for tag,ci in (('a',a_confirm),('b',b_confirm)):
            event[f'b51_{tag}_confirm_at']=(rows[ci]['t']+FIVE).isoformat() if ci is not None else ''
            ei=_next_entry(rows,ci)
            event[f'b51_{tag}_entry_at']=rows[ei]['t'].isoformat() if ei is not None else ''
            event[f'b51_{tag}_entry_price']=rows[ei]['o'] if ei is not None else None
            # No benefit is inferred from unknown future entry bars.
            sim=_core51_exit(rows,ei,upper,fee,slip) if ei is not None else {'status':'NO_ENTRY'}
            for k,v in sim.items():event[f'b51_{tag}_{k}']=v
        out.append(event)
    return out


def _core51_exit(rows,ei,box_upper,fee,slip,hold_bars=B51_HOLD_BARS):
    if ei is None or ei>=len(rows):return {'status':'UNKNOWN','reason':'NEXT_OPEN_MISSING'}
    entry=rows[ei]['o'];hard=entry*(1-B51_HARD_STOP_PCT/100)
    tp1=entry*(1+B51_HARD_STOP_PCT/100*B51_TARGET_R)
    first=False;first_net=None;prev=None;last_price=None
    # Half at 1.5R gross, rest trails previous completed candle low after first TP.
    for step in range(hold_bars):
        j=ei+step
        if j>=len(rows) or (j>ei and rows[j]['t']-rows[j-1]['t']!=FIVE):
            return {'status':'UNKNOWN','reason':'FUTURE_5M_MISSING'}
        bar=rows[j]
        if not first:
            if bar['o']<=hard:kind='HARD_STOP_GAP';px=bar['o']
            elif bar['l']<=hard:kind='HARD_STOP';px=hard
            elif bar['o']>=tp1 and bar['c']>=box_upper:kind=None; first=True;first_net=_net_pct(entry,bar['o'],fee,slip)
            elif bar['h']>=tp1 and bar['c']>=box_upper:kind=None;first=True;first_net=_net_pct(entry,tp1,fee,slip)
            else:kind=None
            if kind is not None:
                return {'status':'READY','exit':kind,'net_pct':round(_net_pct(entry,px,fee,slip),5),
                        'entry_at':rows[ei]['t'].isoformat(),'exit_at':bar['t'].isoformat() if kind.endswith('GAP') else (bar['t']+FIVE).isoformat(),
                        'entry_price':entry,'exit_price':px,'tp1_hit':False,'bars_held':step+1}
            # If target 1 is touched after price CLOSES below upper on this bar,
            # sequence ambiguous: conservative BOX close before target.
            if not first and bar['c']<box_upper and step+1<hold_bars:
                jj=j+1
                if jj>=len(rows) or rows[jj]['t']-bar['t']!=FIVE:return {'status':'UNKNOWN','reason':'BOX_CLOSE_NEXT_OPEN_MISSING'}
                px=rows[jj]['o'];return {'status':'READY','exit':'BOX_CLOSE_NEXT_OPEN','net_pct':round(_net_pct(entry,px,fee,slip),5),
                    'entry_at':rows[ei]['t'].isoformat(),'exit_at':rows[jj]['t'].isoformat(),
                    'entry_price':entry,'exit_price':px,'tp1_hit':False,'bars_held':step+1}
        else:
            # Trailing stop active from NEXT bar after first profit fill;
            # reference is the LOW of the just-completed bar (not current bar).
            if prev is not None:
                trail=max(hard,prev['l'])
                if bar['o']<=trail:kind='TRAIL_GAP';px=bar['o']
                elif bar['l']<=trail:kind='TRAIL';px=trail
                else:kind=None
                if kind is not None:
                    net=(first_net+_net_pct(entry,px,fee,slip))/2
                    return {'status':'READY','exit':kind,'net_pct':round(net,5),
                            'entry_at':rows[ei]['t'].isoformat(),'exit_at':bar['t'].isoformat() if kind.endswith('GAP') else (bar['t']+FIVE).isoformat(),
                            'entry_price':entry,'exit_price':px,'tp1_hit':True,'bars_held':step+1}
        # BOX-close exit on next OPEN, if candle close under the top.
        # A hard stop above takes precedence; on TP + breakdown same candle,
        # do not assume favourable intrabar sequence (use breakout failure).
        if bar['c']<box_upper and step+1<hold_bars:
            if not first:
                pxj=j+1
                if pxj>=len(rows) or rows[pxj]['t']-bar['t']!=FIVE:return {'status':'UNKNOWN','reason':'BOX_CLOSE_NEXT_OPEN_MISSING'}
                px=rows[pxj]['o']
                return {'status':'READY','exit':'BOX_CLOSE_NEXT_OPEN','net_pct':round(_net_pct(entry,px,fee,slip),5),
                        'entry_at':rows[ei]['t'].isoformat(),'exit_at':rows[pxj]['t'].isoformat(),
                        'entry_price':entry,'exit_price':px,'tp1_hit':False,'bars_held':step+1}
            else:
                pxj=j+1
                if pxj>=len(rows) or rows[pxj]['t']-bar['t']!=FIVE:return {'status':'UNKNOWN','reason':'BOX_CLOSE_NEXT_OPEN_MISSING'}
                px=rows[pxj]['o'];net=(first_net+_net_pct(entry,px,fee,slip))/2
                return {'status':'READY','exit':'BOX_CLOSE_NEXT_OPEN','net_pct':round(net,5),
                        'entry_at':rows[ei]['t'].isoformat(),'exit_at':rows[pxj]['t'].isoformat(),
                        'entry_price':entry,'exit_price':px,'tp1_hit':True,'bars_held':step+1}
        prev=bar
        last_price=bar['c']
    net=_net_pct(entry,last_price,fee,slip) if not first else (first_net+_net_pct(entry,last_price,fee,slip))/2
    return {'status':'READY','exit':'TIME_8_BARS','net_pct':round(net,5),'entry_at':rows[ei]['t'].isoformat(),
            'exit_at':(rows[ei+hold_bars-1]['t']+FIVE).isoformat(),'entry_price':entry,
            'exit_price':last_price,'tp1_hit':first,'bars_held':hold_bars}


def _core51_h1_attach(report,h1):
    for e in report['breakout51_events']:
        at=datetime.fromisoformat(e['b51_breakout_at'])
        if h1['error']:
            d={'state':'UNKNOWN','reason':'API_ERROR'}
        else:d=_trend_completed_only(h1['bars'],at,timedelta(hours=1))
        e['b51_h1_state']=d['state']
        e['b51_h1_reason']=d['reason']
        # SMA20 slope and location available only on already CLOSED H1 candles.
        e['b51_full_pass']=bool(e['b51_preliminary_pass'] and d['state']=='UP')
    report['coverage']['b51_all_breakouts']=len(report['breakout51_events'])
    report['coverage']['b51_full_pass']=sum(e['b51_full_pass'] for e in report['breakout51_events'])
    report['coverage']['b51_h1_error']=bool(h1['error'])


def _core51_historical_cash(api_get,market,days,kst):
    # Completed Bithumb day candles are 09:00→09:00 KST; select the last
    # daily close known at 00:00 on each requested study day, not today's ticker.
    to_time=datetime.combine(max(days),dt_time.min,tzinfo=kst)
    raw=api_get('/v1/candles/days', {'market':market,'to':to_time.strftime(KST_FORMAT),
                                    'count':min(50,10+(max(days)-min(days)).days)})
    if not isinstance(raw,list):raise ValueError('일봉 거래대금 응답이 목록이 아닙니다')
    daily=[]
    for x in raw:
        try:daily.append(_parse_candle(x,kst))
        except (KeyError,ValueError,TypeError,OverflowError):pass
    ans={}
    for d in days:
        cutoff=datetime.combine(d,dt_time.min,tzinfo=kst)
        closed=[x for x in daily if x['t']+timedelta(days=1)<=cutoff
                and cutoff-(x['t']+timedelta(days=1))<=timedelta(hours=16)]
        ans[d.isoformat()]=round(max(closed,key=lambda x:x['t'])['cash'],2) if closed else None
    return ans


CORE_PAGE = '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n<title>V2 CORE.51 LIGHT · 엄격 박스 A/B × 순기대값 연구</title>\n<style>\n:root{color-scheme:dark;--bg:#0b1121;--panel:#17213a;--edge:#314260;--fg:#eaf1ff;--sub:#b0c0de;--accent:#90b4ff;--ok:#59e6a2;--bad:#ff98a8}\n*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 system-ui,\'Noto Sans KR\',sans-serif}main{max-width:1050px;margin:auto;padding:16px 12px 65px}\nh1{font-size:21px;margin:0 0 6px}h2{font-size:17px;margin:0 0 9px}h3{font-size:14px;margin:12px 0 7px}.muted{color:var(--sub);font-size:12px}.panel{border:1px solid var(--edge);background:var(--panel);border-radius:14px;padding:15px;margin:12px 0}\n.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:9px}.field{display:flex;flex-direction:column;gap:3px}label{font-size:12px;color:var(--sub)}input,select,button{width:100%;padding:10px;background:#111a2e;border:1px solid #526583;border-radius:8px;color:var(--fg);font:inherit}\nbutton{background:#4169ca;font-weight:700;cursor:pointer}button.alt{background:#293c58}button:disabled{opacity:.45;cursor:not-allowed}a{color:var(--accent)}.buttons{display:flex;flex-wrap:wrap;gap:8px;margin:13px 0}.buttons button{flex:1;min-width:130px}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:9px}.stat{border:1px solid #344968;padding:12px;border-radius:10px}.stat b{font-size:22px;display:block}.good{color:var(--ok)}.bad{color:var(--bad)}.tablewrap{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:650px}td,th{padding:9px;border-bottom:1px solid var(--edge);font-size:12px;text-align:right;white-space:nowrap}td:first-child,th:first-child{text-align:left}th{color:#c2d1ef}.note{padding:12px;background:#0e1b33;border-radius:8px;font-size:12px;color:#c2d1ec}.chip{display:inline-block;background:#314466;border-radius:5px;padding:2px 6px;font-size:11px}progress{width:100%;height:12px;margin-top:9px}details{margin:10px 0}summary{cursor:pointer}\n@media(max-width:650px){main{padding:12px 8px 45px}.panel{padding:11px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}\n</style></head><body><main>\n<header><h1>🧪 V2 CORE.51 LIGHT · 엄격 박스 돌파 A/B × 위험 관리 연구</h1><p class="muted">과거 거래대금 상위 20·30위 비교 · 12~24봉 박스·거래량/몸통/윗꼬리·완료 1시간봉 상승 · 수수료/슬리피지 반영 · 자동 주문 없음</p><a href="/">← 기존 스캐너</a></header>\n<section class="panel"><h2>① 새로운 독립 연구 전략</h2>\n<p>직전 12~24개 5분봉 박스(상단 2회 접촉, 폭 1.5~4 ATR) → 돌파봉 거래량 이전 20봉의 ≥2배, 양봉 몸통 ≥60%, 윗꼬리 ≤20%, 상단 초과 ≤0.8%, <b>이전에 마감된 1시간봉 상승 상태</b>를 충족하면 연구 후보로 기록합니다.</p>\n<div class="grid"><div class="note"><b>A · 첫 봉 안착</b><p>돌파 다음 봉이 박스 위에 마감하면 그 다음 봉 시가에 가상 진입</p></div><div class="note"><b>B · 박스 재테스트</b><p>돌파 후 6봉 안에 상단 부근을 재방문하고 양봉으로 지지하면 다음 봉 시가 진입</p></div></div>\n<p class="muted">두 방법은 독립 비교. 매수 후보 수보다 수수료·슬리피지 차감 순기대값을 우선합니다. 기존 CORE.50 매집 봉/CORE.47.1 신호는 코드에 보존하되 실전 BUY에는 적용하지 않습니다.</p>\n</section>\n<section class="panel"><h2>② 백테스트 설정</h2><div class="grid">\n<div class="field"><label>마지막 날짜(KST·완료된 날짜)</label><input type="date" id="endDay"></div>\n<div class="field"><label>검사 날짜</label><select id="days"><option value="1" selected>1일</option><option value="3">3일</option><option value="5">5일</option><option value="14">14일 (시간 오래 걸림)</option></select></div>\n<div class="field"><label>연구 감시 범위</label><select id="scope"><option value="30" selected>과거 거래대금 상위30 · 내부 20/30 동시 비교</option><option value="20">과거 거래대금 상위20만</option></select></div>\n<div class="field"><label>편도 수수료 (%)</label><input type="number" id="fee" value="0.25" min="0" max="0.5" step="0.01"></div>\n<div class="field"><label>편도 슬리피지 (%) · 스프레드 비용 대리값 포함</label><select id="slip"><option value="0.1" selected>0.10%</option><option value="0.15">0.15%</option><option value="0.2">0.20%</option></select></div>\n<div class="field"><label>거래 1회 자산 위험 상한 (%)</label><select id="risk"><option value="0.5" selected>0.5%</option><option value="1">1.0%</option></select></div>\n<div class="field"><label>일일 손실 중단 (%)</label><select id="loss"><option value="1">1.0%</option><option value="1.5" selected>1.5%</option><option value="2">2.0%</option></select></div>\n</div><div class="buttons"><button id="run">CORE.51 연구 스캔 시작</button><button id="pause" class="alt" disabled>중지 요청</button><button id="csv" class="alt" disabled>CORE.51 CSV 저장</button></div>\n<progress id="progress" value="0" max="1"></progress><p id="status" class="muted">검사 전</p>\n<div class="note">과거 순위는 <b>검사일 00:00 전에 확정된 일봉 거래대금</b>을 전체 현재상장 KRW 마켓에서 조회한 후 계산합니다. 현재 순위로 과거 성적을 고르지 않습니다. 다만 상장폐지 종목이 누락되는 생존 편향은 남습니다. 과거 호가 스프레드 자료가 없어 정확한 호가 필터는 백테스트에 적용하지 않으며, 슬리피지 입력으로 비용 민감도를 검사합니다. 동일 봉 TP/SL 충돌은 손절을 우선합니다. 실제 급격한 갭 하락에는 위험 한도를 초과할 수 있습니다.</div>\n</section>\n<section class="panel"><h2>③ A/B 및 상위 20/30 비교</h2>\n<div class="stats"><div class="stat"><span class="muted">검사 완료</span><b id="kDone">—</b></div><div class="stat"><span class="muted">박스 돌파 탐지</span><b id="kBreak">—</b></div><div class="stat"><span class="muted">엄격 조건 최종 통과</span><b id="kPass">—</b></div><div class="stat"><span class="muted">API / 순위 누락</span><b id="kErrors">—</b></div></div>\n<div class="tablewrap"><table><thead><tr><th>연구 정책</th><th>통과 후보</th><th>실제 가상 선정</th><th>청산확정</th><th>승 / 패</th><th>평균 순익</th><th>기대값</th><th>계좌 누적변화</th><th>100건 충족</th></tr></thead><tbody id="compare"><tr><td colspan="9">스캔 전</td></tr></tbody></table></div>\n<p class="muted">하루 최대 4거래, 한 번에 한 포지션, 3연속 손실 중단, 당일 자산손실 한도 중단. 거래당 위험 한도에 맞춰 가상 투자비중을 계산합니다. 승률·기대값은 청산확정 거래만으로 계산하고, 미확정이 있으면 검증불가로 표시합니다.</p>\n<h3>선정 가상 거래</h3><div class="tablewrap"><table><thead><tr><th>전략</th><th>날짜/종목</th><th>과거순위</th><th>가상 진입</th><th>청산</th><th>청산 이유</th><th>순수익률</th><th>계좌 영향</th><th>1차 TP</th></tr></thead><tbody id="trades"><tr><td colspan="9">선정된 거래 없음</td></tr></tbody></table></div>\n</section>\n<section class="panel"><h2>④ 필터 작동 분석 · 왜 신호가 걸러졌는가?</h2>\n<div class="tablewrap"><table><thead><tr><th>필터 상태</th><th>건수</th><th>A 진입 수</th><th>B 진입 수</th></tr></thead><tbody id="filters"><tr><td colspan="4">스캔 전</td></tr></tbody></table></div>\n<p class="muted">엄격 박스가 만들어진 뒤 돌파한 사건만 필터 비교에 포함됩니다. 상위 20위에선 전혀 신호가 없을 수도 있습니다. 이는 오류가 아니라 관측 결과입니다.</p>\n</section>\n<section class="panel"><h2>⑤ 현재 호가 스프레드 별도 확인 (과거 검증과 무관)</h2>\n<div class="buttons"><button id="spread" class="alt" disabled>이번에 감시한 코인의 현재 호가 확인</button></div><p class="muted">현재 스프레드만 조회합니다. 과거 진입 시점 스프레드로 사용하거나 과거 필터에 적용하지 않습니다. 호가가 넓으면 실제 주문 전 제외해야 합니다.</p>\n<div class="tablewrap"><table><thead><tr><th>종목</th><th>현재 매수 1호가</th><th>매도 1호가</th><th>현재 스프레드</th><th>현재 상태</th></tr></thead><tbody id="spreadTable"><tr><td colspan="5">검사 후 현재 호가 조회 가능</td></tr></tbody></table></div>\n</section>\n<section class="panel"><h2>⑥ 검증 원칙</h2><p>기존 41건 표본은 예비 필터 비교에만 사용합니다. 독립 신규 데이터 <b>최소 2주·확정 100건 이상</b>을 우선 확보하고, 그 후 2~4주 모의 운영을 통해 슬리피지와 실제 호가를 관찰합니다. 기대값이 양수라도 표본이 작으면 성공 전략으로 확정하지 않습니다.</p></section>\n<script>\n\'use strict\';\nconst $=id=>document.getElementById(id),VERSION=\'V2.CORE.51-LIGHT-STRICT-BOX-AB-RISK-RESEARCH\';\nconst esc=v=>String(v??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nconst num=(v,d=2)=>v===null||v===undefined||!Number.isFinite(Number(v))?\'—\':Number(v).toFixed(d);\nconst kstDate=()=>new Date(Date.now()+9*3600000).toISOString().slice(0,10);\nconst yesterday=()=>new Date(Date.now()+9*3600000-86400000).toISOString().slice(0,10);\n$(\'endDay\').value=yesterday();$(\'endDay\').max=yesterday();\nfunction daysEnding(end,n){let out=[];for(let k=n-1;k>=0;k--){let d=new Date(end+\'T00:00:00Z\');d.setUTCDate(d.getUTCDate()-k);out.push(d.toISOString().slice(0,10));}return out;}\nfunction cfg(){const d=$(\'endDay\').value,n=Number($(\'days\').value),fee=Number($(\'fee\').value),slip=Number($(\'slip\').value),scope=Number($(\'scope\').value);if(!d||d>=kstDate()||![1,3,5,14].includes(n)||![20,30].includes(scope))throw Error(\'날짜·범위를 확인해 줘\');if([fee,slip].some(x=>!Number.isFinite(x)||x<0||x>.5))throw Error(\'비용 범위 오류\');return {days:daysEnding(d,n),fee,slip,scope};}\nasync function api(url,payload){let resp=await fetch(url,{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(payload)}),j=await resp.json();if(!resp.ok||!j.ok)throw Error(j.error||\'HTTP \'+resp.status);return j;}\nlet state=null,running=false,paused=false;const IDs=[\'endDay\',\'days\',\'scope\',\'fee\',\'slip\'];\nfunction setBusy(x){running=x;$(\'run\').disabled=x;$(\'pause\').disabled=!x;for(const id of IDs)$(id).disabled=x;}\n$(\'risk\').onchange=render;$(\'loss\').onchange=render;\n$(\'pause\').onclick=()=>{paused=true;$(\'status\').textContent=\'중지 요청 · 진행 중인 조회는 종료 후 멈춰\';};\nfunction events(){return state?state.results.flatMap(x=>(x.report.breakout51_events||[]).map(e=>({...e,b51_rank:x.rank}))):[];}\nconst full=e=>e.b51_full_pass===true;\nfunction simulate(method,top){if(!state)return {pool:[],picked:[],summary:[]};let ev=events().filter(e=>e.b51_rank<=top&&full(e)&&!!e[\'b51_\'+method+\'_entry_at\']).sort((a,b)=>a[\'b51_\'+method+\'_entry_at\'].localeCompare(b[\'b51_\'+method+\'_entry_at\'])||a.b51_rank-b.b51_rank||a.market.localeCompare(b.market));let picked=[],summary=[];\nfor(const day of state.days){let pool=ev.filter(e=>e.date===day), equity=100, count=0,lossStreak=0, busy=\'\',halt=\'\',dayTrades=[];\n for(const e of pool){const at=e[\'b51_\'+method+\'_entry_at\'];if(halt||count>=4)break;if(busy&&at<busy)continue;\n  const status=e[\'b51_\'+method+\'_status\'];const per=Number($(\'risk\').value),cost=1+2*(state.fee+state.slip),position=Math.min(100,per/cost*100); // no leverage\n  const net=status===\'READY\'?Number(e[\'b51_\'+method+\'_net_pct\']):null;\n  const pnl=net===null?null:position*net/100;\n  const item={e,method,top,position,net,pnl,status,equityBefore:equity};picked.push(item);dayTrades.push(item);count++;\n  if(status!==\'READY\'){halt=\'UNKNOWN_OUTCOME\';break;}\n  equity+=equity*pnl/100;item.equityAfter=equity;\n  lossStreak=pnl<0?lossStreak+1:0;\n  if(lossStreak>=3)halt=\'THREE_LOSSES\';\n  if(equity<=100-Number($(\'loss\').value))halt=\'DAILY_LOSS_LIMIT\';\n  busy=e[\'b51_\'+method+\'_exit_at\']||\'9999\';\n }\n summary.push({day,available:pool.length,count,ready:dayTrades.filter(x=>x.status===\'READY\').length,equity:equity-100,halt});\n}\nreturn {pool:ev,picked,summary};}\nfunction stats(p){const x=p.picked.filter(a=>a.status===\'READY\'),win=x.filter(a=>a.net>0),loss=x.filter(a=>a.net<=0),avg=x.length?x.reduce((s,a)=>s+a.net,0)/x.length:null,aw=win.length?win.reduce((s,a)=>s+a.net,0)/win.length:0,al=loss.length?Math.abs(loss.reduce((s,a)=>s+a.net,0)/loss.length):0,expect=x.length?(win.length/x.length*aw-loss.length/x.length*al):null;return {ready:x.length,win:win.length,loss:loss.length,avg,expect};}\nfunction render(){if(!state)return;const ev=events();$(\'kDone\').textContent=state.done+\'/\'+state.tasks.length;$(\'kBreak\').textContent=ev.length;$(\'kPass\').textContent=ev.filter(full).length;$(\'kErrors\').textContent=state.errors.length+\' / \'+(state.rankErrors.length+Object.values(state.rankMissing||{}).reduce((a,b)=>a+b,0))+\' (날짜별 미확인 합산)\';\nlet methods=[[\'a\',20],[\'a\',30],[\'b\',20],[\'b\',30]].filter(([,n])=>n<=state.scope);let picks=methods.map(([m,n])=>({m,n,p:simulate(m,n)}));$(\'compare\').innerHTML=picks.map(({m,n,p})=>{const s=stats(p),totalEquity=(p.summary.reduce((x,d)=>x*(1+d.equity/100),1)-1)*100;return `<tr><td>${m.toUpperCase()} · 상위${n}</td><td>${p.pool.length}</td><td>${p.picked.length}</td><td>${s.ready}</td><td>${s.win}/${s.loss}</td><td>${num(s.avg,3)}%</td><td class="${s.expect>=0?\'good\':\'bad\'}">${num(s.expect,3)}%</td><td>${num(totalEquity,3)}%</td><td>${s.ready>=100?\'충족\':\'미달\'}</td></tr>`;}).join(\'\')||\'<tr><td colspan="9">해당 없음</td></tr>\';\n$(\'trades\').innerHTML=picks.flatMap(({m,n,p})=>p.picked.map(x=>{const e=x.e,s=x.status===\'READY\'?num(x.net,3)+\'%\':\'미확정\',netclass=x.net===null?\'\':x.net>=0?\'good\':\'bad\';return `<tr><td>${m.toUpperCase()}·${n}</td><td>${esc(e.date)} ${esc(e.market)}</td><td>${e.b51_rank}</td><td>${esc(e[\'b51_\'+m+\'_entry_at\'].slice(11,16))}</td><td>${esc((e[\'b51_\'+m+\'_exit_at\']||\'\').slice(11,16))}</td><td>${esc(e[\'b51_\'+m+\'_exit\']||e[\'b51_\'+m+\'_reason\']||\'—\')}</td><td class="${netclass}">${s}</td><td>${x.pnl===null?\'—\':num(x.pnl,3)+\'%\'}</td><td>${e[\'b51_\'+m+\'_tp1_hit\']?\'예\':\'아니오\'}</td></tr>`;})).slice(0,150).join(\'\')||\'<tr><td colspan="9">선정 거래 없음</td></tr>\';\nlet filters=[[\'엄격 조건 통과\',x=>x.b51_preliminary_pass],[\'거래량 <2배\',x=>x.b51_fail_reasons.includes(\'vol_2x\')],[\'몸통 <60%\',x=>x.b51_fail_reasons.includes(\'body_60\')],[\'윗꼬리 >20%\',x=>x.b51_fail_reasons.includes(\'wick_20\')],[\'추격 돌파 >0.8%\',x=>x.b51_fail_reasons.includes(\'chase_08\')],[\'1시간봉 상승 아님 / 자료부족\',x=>x.b51_preliminary_pass&&!x.b51_full_pass]];\n$(\'filters\').innerHTML=filters.map(([k,f])=>{let a=ev.filter(f);return `<tr><td>${k}</td><td>${a.length}</td><td>${a.filter(x=>x.b51_a_entry_at).length}</td><td>${a.filter(x=>x.b51_b_entry_at).length}</td></tr>`}).join(\'\');\n$(\'status\').textContent=(state.rankReady?\'과거 순위 계산 완료 · \':\'과거 순위 계산 중 · \')+state.done+\'/\'+state.tasks.length+\' 검사 · 데이터 오류 \'+state.errors.length+\' · 순위 자료없음 \'+state.rankErrors.length+(state.complete?\' · 완료\':\'\');$(\'progress\').max=Math.max(1,state.tasks.length);$(\'progress\').value=state.done;$(\'csv\').disabled=!state.results.length;$(\'spread\').disabled=!state.results.length;\n}\nasync function phaseRank(c){let m=await fetch(\'/api/v2core/universe\').then(r=>r.json());if(!m.ok||!Array.isArray(m.items))throw Error(m.error||\'시장 조회 실패\');state={...c,rankReady:false,rankErrors:[],rankMissing:{},errors:[],results:[],ranks:{},tasks:[],done:0,complete:false};const mk=[...new Set(m.items)],cache={};let cursor=0,done=0;\nasync function worker(){while(!paused&&cursor<mk.length){let coin=mk[cursor++];try{const r=await api(\'/api/v2core51/historical-cash\',{market:coin,days:c.days});cache[coin]=r.cash_by_date;}catch(e){state.rankErrors.push({market:coin,error:String(e)});}done++;if(done%15===0)$(\'status\').textContent=\'과거 거래대금 순위 \'+done+\'/\'+mk.length+\'개 확인 · 누락 \'+state.rankErrors.length;}}\nawait Promise.all(Array.from({length:Math.min(4,mk.length)},()=>worker()));if(paused)throw Error(\'순위 조회 중지됨\');\nfor(const day of c.days){const sorted=mk.filter(coin=>Number.isFinite(cache[coin]?.[day])).sort((a,b)=>cache[b][day]-cache[a][day]||a.localeCompare(b));state.rankMissing[day]=mk.length-sorted.length;state.ranks[day]=Object.fromEntries(sorted.map((coin,i)=>[coin,i+1]));const scope=sorted.slice(0,c.scope);for(const coin of scope)state.tasks.push({date:day,market:coin,rank:state.ranks[day][coin]});}\nstate.rankReady=true;return state;\n}\nasync function phaseScan(){let cursor=state.done;\nasync function worker(){while(!paused&&cursor<state.tasks.length){let task=state.tasks[cursor++];try{let r=await api(\'/api/v2core/mtf42/analyze\',{date:task.date,market:task.market,fee:state.fee,slip:state.slip});state.results.push({report:r.result,rank:task.rank});}catch(e){state.errors.push({...task,error:String(e)});}state.done++;if(state.done%5===0||state.done===state.tasks.length)render();}}\nawait Promise.all(Array.from({length:Math.min(4,state.tasks.length)},()=>worker()));state.complete=!paused&&state.done===state.tasks.length;render();}\n$(\'run\').onclick=async()=>{if(running)return;try{const c=cfg();paused=false;setBusy(true);$(\'csv\').disabled=true;$(\'spread\').disabled=true;await phaseRank(c);render();await phaseScan();}catch(e){$(\'status\').textContent=\'⚠️ \'+String(e.message||e);}finally{setBusy(false);if(state)render();}};\nfunction csvVal(v){let s=v===null||v===undefined?\'\':typeof v===\'object\'?JSON.stringify(v):String(v);if(/^[=+@-]/.test(s)&&!Number.isFinite(Number(s)))s="\'"+s;return \'"\'+s.replaceAll(\'"\',\'""\')+\'"\';}\n$(\'csv\').onclick=()=>{if(!state)return;let picks=[[\'a\',20],[\'a\',30],[\'b\',20],[\'b\',30]].filter(([,n])=>n<=state.scope).map(([m,n])=>({m,n,p:simulate(m,n)}));let rows=[];for(const d of state.days){rows.push({row_type:\'RANK_COVERAGE\',date:d,ranked_count:Object.keys(state.ranks[d]||{}).length,rank_missing:state.rankMissing[d]||0,scope:state.scope,complete:state.complete,scan_done:state.done,scan_total:state.tasks.length,fee:state.fee,slip:state.slip});}\nfor(const r of state.results){rows.push({row_type:\'MARKET_COVERAGE\',date:r.report.date,market:r.report.market,historical_rank:r.rank,...r.report.coverage});for(const e of r.report.breakout51_events||[])rows.push({row_type:\'BOX51_EVENT\',historical_rank:r.rank,...e});for(const e of r.report.events||[])if(e.box_signal_at)rows.push({row_type:\'BASELINE_BOX_EVENT\',historical_rank:r.rank,...e});}\nfor(const {m,n,p} of picks){for(const x of p.picked){rows.push({row_type:\'SELECTED_TRADE\',policy:m.toUpperCase()+\'_\'+n,historical_rank:x.e.b51_rank,position_equity_pct:x.position,account_pnl_pct:x.pnl,status:x.status,net_pct:x.net,...x.e});}for(const x of p.summary)rows.push({row_type:\'DAY_SUMMARY\',date:x.day,policy:m.toUpperCase()+\'_\'+n,selected:x.count,ready:x.ready,account_pnl_pct:x.equity,halt:x.halt});}\nfor(const e of state.errors)rows.push({row_type:\'API_ERROR\',...e});for(const e of state.rankErrors)rows.push({row_type:\'RANK_API_ERROR\',...e});\nlet cols=[\'row_type\',\'date\',\'market\',\'policy\',\'historical_rank\',\'status\',\'net_pct\',\'account_pnl_pct\',...new Set(rows.flatMap(x=>Object.keys(x)))];cols=[...new Set(cols)];let csv=\'\\ufeff\'+[cols.join(\',\'),...rows.map(r=>cols.map(k=>csvVal(r[k])).join(\',\'))].join(\'\\r\\n\');let url=URL.createObjectURL(new Blob([csv],{type:\'text/csv;charset=utf-8\'}));let link=document.createElement(\'a\');link.href=url;link.download=`CORE51_STRICT_BOX_AB_${state.days[0]}_${state.days.at(-1)}_hist${state.scope}_KRW.csv`;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),4000);};\n$(\'spread\').onclick=async()=>{if(!state)return;try{const end=state.days.at(-1),markets=[...new Set(state.results.filter(x=>x.report.date===end).map(x=>x.report.market))].slice(0,30);let r=await api(\'/api/v2core51/spread-now\',{markets});$(\'spreadTable\').innerHTML=r.items.map(x=>`<tr><td>${esc(x.market)}</td><td>${num(x.bid,9)}</td><td>${num(x.ask,9)}</td><td>${num(x.spread_pct,3)}%</td><td>${x.spread_pct===null?\'자료없음\':x.spread_pct>.2?\'넓음\':\'참고용\'}</td></tr>`).join(\'\')||\'<tr><td colspan="5">자료없음</td></tr>\';}catch(e){$(\'spreadTable\').innerHTML=`<tr><td colspan="5">${esc(String(e.message||e))}</td></tr>`;}};\n</script></main></body></html>\n'

# CORE.42 higher-timeframe specification: restored from CORE.46.
# Used only for completed-candle D1/H4/H1 diagnostics, NOT BUY filtering.
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

    @app.post('/api/v2core51/historical-cash')
    def core51_historical_cash_route():
        try:
            raw=request.get_json(silent=True)
            if not isinstance(raw,dict):raise ValueError('요청 형식 오류')
            market=raw.get('market');ds=raw.get('days')
            if not isinstance(market,str) or not MARKET_PATTERN.fullmatch(market):raise ValueError('종목 코드 오류')
            if not isinstance(ds,list) or not 1<=len(ds)<=14:raise ValueError('날짜는 1~14개')
            dates=sorted(set(_check_cfg({'date':d,'fee':0,'slip':0},kst)['day'] for d in ds))
            if (dates[-1]-dates[0]).days>14:raise ValueError('검사 기간 초과')
            # Strict market validation; request only currently-listed KRW markets.
            if market not in get_markets():raise ValueError('현재 KRW 마켓에 없는 종목')
            key=('B51_HIST',market,tuple(dates))
            with lock:prev=cache['results'].get(key)
            if prev is not None and time.monotonic()-prev[0]<1200:
                return jsonify(ok=True,version=VERSION,market=market,cash_by_date=prev[1],cached=True)
            answer=_core51_historical_cash(api_get,market,dates,kst)
            with lock:
                if len(cache['results'])>3000:cache['results'].clear()
                cache['results'][key]=(time.monotonic(),answer)
            return jsonify(ok=True,version=VERSION,market=market,cash_by_date=answer,cached=False)
        except Exception as exc:return jsonify(ok=False,error=f'{type(exc).__name__}:{exc}'[:160]),400

    @app.post('/api/v2core51/spread-now')
    def core51_spread_now():
        try:
            raw=request.get_json(silent=True)
            markets=raw.get('markets',[]) if isinstance(raw,dict) else []
            if (not isinstance(markets,list) or len(markets)>35 or
                any(not isinstance(x,str) or not MARKET_PATTERN.fullmatch(x) for x in markets)):
                raise ValueError('현재 호가 조회 종목 오류')
            allowed=set(get_markets())
            markets=sorted(set(x for x in markets if x in allowed))
            if not markets:return jsonify(ok=True,version=VERSION,items=[])
            book=api_get('/v1/orderbook',{'markets':','.join(markets)})
            if not isinstance(book,list):raise ValueError('호가 응답 배열 오류')
            out=[]
            for item in book:
                if not isinstance(item,dict) or item.get('market') not in markets:continue
                units=item.get('orderbook_units') or []
                try:
                    ask=float(units[0]['ask_price']);bid=float(units[0]['bid_price'])
                    spread=(ask-bid)/((ask+bid)/2)*100 if ask>0 and bid>0 and ask>=bid else None
                except (ValueError,KeyError,TypeError,IndexError,ZeroDivisionError):ask=bid=None;spread=None
                out.append({'market':item['market'],'ask':ask,'bid':bid,
                            'spread_pct':round(spread,5) if spread is not None else None,
                            'snapshot_timestamp_ms':item.get('timestamp')})
            return jsonify(ok=True,version=VERSION,items=out,note='CURRENT_ONLY_NOT_HISTORICAL')
        except Exception as exc:return jsonify(ok=False,error=f'{type(exc).__name__}:{exc}'[:160]),400

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
            report['breakout51_events']=_core51_scan(rows,cfg['day'],market,cfg['fee'],cfg['slip'])
            report['coverage'].update(fetch_info)
            histories=None
            if any(e.get('box_signal_at') for e in report['events']):
                # Only C-signal-bearing markets need three extra timeframes.
                histories = fetch_mtf_history(api_get, market, cfg['day'], kst)
                attach_mtf_diagnostics(report, histories)
            else:
                report['coverage'].update(trend_diagnostic_events=0,
                                          trend_higher_tf_request_count=0,
                                          trend_tf_api_error_count=0)
            if report['breakout51_events']:
                if histories is not None:
                    h1=histories.get('h1',{'bars':[],'error':'H1_MISSING'})
                else:
                    tf_path,span,count=_TFS['h1']
                    try:
                        until=datetime.combine(cfg['day']+timedelta(days=1),dt_time.min,tzinfo=kst)
                        raw_h1=api_get(tf_path,{'market':market,'to':until.strftime(KST_FORMAT),'count':count})
                        if not isinstance(raw_h1,list):raise ValueError('1시간봉 형식 오류')
                        uniq={}
                        for c in raw_h1:
                            try:
                                z=_parse_candle(c,kst)
                                if z['t']<until:uniq[z['t']]=z
                            except (ValueError,KeyError,TypeError,OverflowError):pass
                        h1={'bars':[uniq[t] for t in sorted(uniq)],'error':''}
                    except Exception as exc:
                        h1={'bars':[],'error':f'{type(exc).__name__}:{exc}'[:140]}
                _core51_h1_attach(report,h1)
            else:
                report['coverage'].update(b51_all_breakouts=0,b51_full_pass=0,b51_h1_error=False)
            with lock:
                if len(cache['results'])>1000:
                    cache['results'].clear()
                cache['results'][key]=(time.monotonic(),report)
            return jsonify(ok=True,version=VERSION,cached=False,result=report)
        except Exception as e:
            return jsonify(ok=False,error=f'{type(e).__name__}: {e}'[:200]),400
