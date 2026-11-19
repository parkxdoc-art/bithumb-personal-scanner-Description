"""V2 CORE.56 LIGHT — Binance/Bithumb momentum B vs B2 and structural stop research; no lookahead.

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

VERSION = 'V2.CORE.56-LIGHT-B2-STRUCTURAL-PB-TREND'
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


# --- CORE.53 full-day Binance Spot USDT -> Bithumb KRW lag research (research only) ---
CORE53_STOP_PCT = 3.0
CORE53_TPS = ((3.0, .30), (5.0, .30), (10.0, .20), (30.0, .20))
CORE53_BURST_GROUP_MIN = 15  # grouping label only; NEVER drops a qualifying event
CORE53_STABLE = {'USDT','USDC','FDUSD','TUSD','USDD','DAI','USDP','BUSD','EUR','EURC','TRY','KRW','USD','PAX','PYUSD'}


# CORE.53.1: local process pacing and server-side IP cooldown. Binance applies
# REQUEST_WEIGHT limits per public IP; other Render processes or tenants may
# still trigger a 429/418. NEVER try alternate IPs or ignore Retry-After.
_CORE531_HTTP_LOCK = Lock()
_CORE531_LIMIT = {'next_at': 0.0, 'blocked_until': 0.0, 'reason': ''}
_CORE531_MIN_REQUEST_GAP_SEC = 1.0
_CORE531_BARS_LOCK = Lock()
_CORE531_BARS_CACHE = {}  # one completed 1m history per spot symbol/date


def _core531_retry_after(headers):
    """Parse RFC Retry-After seconds or HTTP-date; None means no usable hint."""
    from datetime import timezone as _tz
    from email.utils import parsedate_to_datetime
    value = (headers.get('Retry-After') if headers else None)
    if not value:
        return None
    try:
        seconds = float(str(value).strip())
        if isfinite(seconds) and seconds >= 0:
            return max(1, int(seconds + .9999))
    except (ValueError, TypeError, OverflowError):
        pass
    try:
        end = parsedate_to_datetime(str(value))
        if end.tzinfo is None:
            end = end.replace(tzinfo=_tz.utc)
        return max(1, int((end - datetime.now(_tz.utc)).total_seconds() + .9999))
    except (ValueError, TypeError, OverflowError, IndexError):
        return None


def _core53_http(path, args=None):
    """Public Binance market data, paced, with mandatory 429/418 backoff.

    Bounded automatic retry only if Binance asks for <=6 seconds of waiting;
    longer bans/limits return a useful error instead of hanging Flask workers.
    """
    from urllib.request import urlopen, Request
    from urllib.parse import urlencode
    from urllib.error import HTTPError, URLError
    import json
    if not path.startswith('/api/v3/'):
        raise ValueError('Binance public market-data paths only')
    url = 'https://data-api.binance.vision' + path + ('?' + urlencode(args) if args else '')
    req = Request(url, headers={'User-Agent': 'PersonalResearchScanner/54.0',
                                'Accept': 'application/json'})
    for attempt in range(2):
        retry = False
        # Serialize outgoing Binance calls across concurrently submitted probes
        # within this process, and retain a cooldown after an IP rate limit.
        with _CORE531_HTTP_LOCK:
            now = time.monotonic()
            remaining = _CORE531_LIMIT['blocked_until'] - now
            if remaining > 0:
                # One controlled in-request retry for short Retry-After only.
                if attempt and remaining <= 6:
                    time.sleep(remaining)
                else:
                    raise ValueError('Binance 요청 제한 대기 중: 약 '
                                     + str(int(remaining + .999)) + '초 후 재시도'
                                     + ' (' + _CORE531_LIMIT['reason'] + ')')
            delay = _CORE531_LIMIT['next_at'] - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            _CORE531_LIMIT['next_at'] = time.monotonic() + _CORE531_MIN_REQUEST_GAP_SEC
            try:
                with urlopen(req, timeout=12) as response:
                    value = json.load(response)
            except HTTPError as exc:
                if exc.code in (418, 429):
                    hint = _core531_retry_after(exc.headers)
                    # No header: back off conservatively. Explicit larger
                    # Retry-After values are never shortened or bypassed.
                    seconds = hint if hint is not None else (120 if exc.code == 418 else 60)
                    _CORE531_LIMIT['blocked_until'] = time.monotonic() + seconds
                    _CORE531_LIMIT['reason'] = 'HTTP ' + str(exc.code)
                    if exc.code == 429 and hint is not None and hint <= 6 and attempt == 0:
                        retry = True
                    else:
                        kind = 'Binance IP 차단' if exc.code == 418 else 'Binance API 요청 제한'
                        raise ValueError(f'{kind} (HTTP {exc.code}): '
                                         f'약 {seconds}초 뒤 재시도. 현재 공유 IP 제한일 수 있습니다') from exc
                if exc.code == 403:
                    raise ValueError('Binance HTTP 403: 서버 IP/지역 또는 보안정책 차단 (속도 문제와 별개)') from exc
                if exc.code not in (418,429):
                    raise ValueError(f'Binance HTTP {exc.code}: 지역/접근 제한 또는 심볼 오류') from exc
            except URLError as exc:
                raise ValueError(f'Binance 연결 실패: {exc.reason}') from exc
        if retry:
            continue
        if isinstance(value, dict) and value.get('code') and value.get('msg'):
            raise ValueError('Binance 응답 오류: ' + str(value['msg'])[:110])
        return value
    raise ValueError('Binance 요청 제한으로 재시도 실패. 요청 간격을 넓혀 주세요')


def _core53_universe(market_names, http_get=_core53_http):
    raw=http_get('/api/v3/exchangeInfo',{'permissions':'SPOT'})
    if not isinstance(raw,dict) or not isinstance(raw.get('symbols'),list):
        raise ValueError('Binance 현물 심볼 응답 오류')
    eligible=set(market_names)
    out=[]; seen=set()
    for x in raw['symbols']:
        if not isinstance(x,dict):continue
        base=x.get('baseAsset','');market='KRW-'+base;symbol=x.get('symbol','')
        if (base in CORE53_STABLE or x.get('quoteAsset')!='USDT' or
            x.get('status')!='TRADING' or market not in eligible or not re.fullmatch(r'[A-Z0-9]{2,25}USDT',symbol)):
            continue
        permitted=x.get('permissions',[])
        sets=x.get('permissionSets',[])
        if permitted and 'SPOT' not in permitted:continue
        if not permitted and sets and not any('SPOT' in sub for sub in sets if isinstance(sub,list)):continue
        if base in seen:continue
        seen.add(base);out.append({'market':market,'binance_symbol':symbol})
    return sorted(out,key=lambda x:x['market'])


def _core53_fetch_binance_1m(symbol,day,kst,http_get=_core53_http):
    """Get complete KST day with 110m PRE warmup and 11m POST comparison.

    A preceding 20 x 5-minute baseline requires 105 contiguous 1-minute bars.
    Last-minute detections need the next day's 10 minutes for post-signal labels.
    """
    # Cache successfully fetched historical candles independent of fees/slippage.
    # Injected test/client fetchers bypass this runtime cache by design.
    key = (symbol, day.isoformat())
    if http_get is _core53_http:
        with _CORE531_BARS_LOCK:
            cached = _CORE531_BARS_CACHE.get(key)
            if cached and time.monotonic() - cached[0] < 3600:
                return [dict(v) for v in cached[1]], dict(cached[2], binance_cached=True)
    from datetime import timezone
    if not re.fullmatch(r'[A-Z0-9]{2,25}USDT',symbol):raise ValueError('Binance 심볼 코드 오류')
    start=datetime.combine(day,dt_time.min,tzinfo=kst)-timedelta(minutes=110)
    end=datetime.combine(day+timedelta(days=1),dt_time.min,tzinfo=kst)+timedelta(minutes=11)
    beg_ms=int(start.timestamp()*1000);end_ms=int(end.timestamp()*1000)
    cursor=beg_ms;found={};pages=0
    while cursor<end_ms and pages<5:
        raw=http_get('/api/v3/klines',{'symbol':symbol,'interval':'1m','startTime':cursor,
                                       'endTime':end_ms-1,'limit':1000})
        if not isinstance(raw,list):raise ValueError('Binance 분봉 응답 배열 오류')
        pages+=1;last=None
        for a in raw:
            try:
                tms=int(a[0]);o,h,l,c=[float(a[j]) for j in (1,2,3,4)]
                q=float(a[7]);take=float(a[10])
                if (tms<cursor or tms>=end_ms or min(o,h,l,c)<=0 or q<0 or
                        not 0<=take<=q*1.002 or any(not isfinite(v) for v in (o,h,l,c,q,take))):
                    continue
                t=datetime.fromtimestamp(tms/1000,timezone.utc).astimezone(kst)
                found[t]={'t':t,'o':o,'h':h,'l':l,'c':c,'q':q,'taker':take}
                last=tms if last is None else max(tms,last)
            except (ValueError,IndexError,TypeError,OverflowError):continue
        if not raw:break
        if last is None:raise ValueError('Binance 분봉에 정상 시각 자료 없음')
        if last+60000<=cursor:raise ValueError('Binance 분봉 페이지 이동 실패')
        cursor=last+60000
        if len(raw)<1000:break
    if cursor<end_ms and pages>=5:raise ValueError('Binance 분봉 페이지 범위 초과')
    rows = [found[k] for k in sorted(found)]
    meta = {'binance_pages': pages, 'binance_1m_bars': len(found),
            'warmup_min': 110, 'post_min': 11, 'binance_cached': False}
    if http_get is _core53_http and rows:
        with _CORE531_BARS_LOCK:
            # Keep only up to 24 symbol/day cached histories to bound RAM.
            if len(_CORE531_BARS_CACHE) >= 24:
                oldest = min(_CORE531_BARS_CACHE, key=lambda k: _CORE531_BARS_CACHE[k][0])
                _CORE531_BARS_CACHE.pop(oldest, None)
            _CORE531_BARS_CACHE[key] = (time.monotonic(), rows, meta)
    return rows, meta


def _core53_signals(bars,day):
    """Record EVERY qualifying completed 1m/5m signal in the KST calendar day.

    Episode grouping (15m) is metadata only. Do not discard later bursts, early
    hours or 19:45+ signals. Never use post-signal prices to decide detection.
    """
    if not bars:return []
    start=datetime.combine(day,dt_time.min,tzinfo=bars[0]['t'].tzinfo)
    end=start+timedelta(days=1)
    out=[];last=None;episode=0;one=timedelta(minutes=1)
    for i,b in enumerate(bars):
        if b['t']<start or b['t']>=end or i<104:continue
        if any(bars[j]['t']-bars[j-1]['t']!=one for j in range(i-103,i+1)):
            continue
        # Reference: 20 PRIOR non-overlapping completed 5m windows (100 bars).
        # The most recent five 1m bars are the CURRENT trailing 5m window.
        reference=bars[i-104:i-4]
        q1=sum(x['q'] for x in bars[i-20:i])/20
        base5=sum(x['q'] for x in reference)/20
        current5=sum(x['q'] for x in bars[i-4:i+1]);taker5=sum(x['taker'] for x in bars[i-4:i+1])
        r1=b['q']/q1 if q1>0 else 0;r5=current5/base5 if base5>0 else 0
        buy1=b['taker']/b['q'] if b['q']>0 else 0
        buy5=taker5/current5 if current5>0 else 0
        up1=_price_pct(b['c'],b['o']);up5=_price_pct(b['c'],bars[i-5]['c'])
        sig1=(r1>=3 and b['q']>=25000 and buy1>=.55 and up1>=.25 and up5>=.30)
        # 5m signal only at completed calendar-aligned 5m candle close.
        five_close=(b['t'].minute+1)%5==0
        sig5=(five_close and r5>=2 and current5>=70000 and buy5>=.55 and up5>=.40)
        if not (sig1 or sig5):continue
        if last is None or (b['t']-last).total_seconds()>CORE53_BURST_GROUP_MIN*60:
            episode+=1;episode_first=True
        else:episode_first=False
        ts=b['t']+one
        out.append({'date':day.isoformat(),'symbol':'','signal_at':ts.isoformat(),
                    'signal_type':'1M_AND_5M' if sig1 and sig5 else '1M' if sig1 else '5M',
                    'burst_group':episode,'episode_first':episode_first,
                    'binance_price':b['c'],'binance_quote_usdt_1m':round(b['q'],3),
                    'binance_quote_usdt_5m':round(current5,3),
                    'binance_quote_ratio_1m':round(r1,4),'binance_quote_ratio_5m':round(r5,4),
                    'binance_taker_buy_1m':round(buy1*100,3),
                    'binance_taker_buy_5m':round(buy5*100,3),
                    'binance_return_5m_pct':round(up5,5)})
        last=b['t']
    return out


def _core53_fetch_bithumb_day(api_get, market, day, kst, hold=240, with_meta=False):
    """Historical 1m candles, sparse-safe cursor pagination. NEVER backfill.

    A short (<200) nonempty page is NOT proof that older dates are unavailable.
    Continue until the required start boundary, empty response or hard cap.
    with_meta=False retains CORE.53.1 two-value API for regression tests.
    """
    start = datetime.combine(day,dt_time.min,tzinfo=kst)-timedelta(minutes=7)
    stop = datetime.combine(day+timedelta(days=1),dt_time.min,tzinfo=kst)+timedelta(minutes=max(hold+35,48))
    cursor=stop; seen={}; pages=0; bad=0; short_pages=0; raw_count=0
    reached=False; reason=''; oldest=None; newest=None; last_cursor=None
    # Sparse 1m candles may require > (window_minutes / 200) calls, but cap
    # cost and never disguise an incomplete history as full coverage.
    for _ in range(24):
        raw=api_get('/v1/candles/minutes/1', {'market':market,'to':cursor.strftime(KST_FORMAT),'count':200})
        if not isinstance(raw,list):raise ValueError('빗썸 1분봉 응답 형식 오류')
        pages+=1;raw_count+=len(raw)
        if len(raw)<200: short_pages+=1
        if not raw:
            reason='EMPTY_BEFORE_START' if not reached else 'START_REACHED';break
        page_oldest=None;parse_good=0
        for b in raw:
            try:
                c=_parse_candle(b,kst);t=c['t'];parse_good+=1
                if page_oldest is None or t<page_oldest:page_oldest=t
                if oldest is None or t<oldest:oldest=t
                if newest is None or t>newest:newest=t
                if start<=t<stop:seen[t]=c
            except (ValueError,KeyError,TypeError,OverflowError):bad+=1
        if page_oldest is None:
            reason='ALL_CANDLES_INVALID';break
        if page_oldest<=start:
            reached=True;reason='START_REACHED';break
        if page_oldest>=cursor or page_oldest==last_cursor:
            reason='CURSOR_STALLED';break
        last_cursor=cursor;cursor=page_oldest
    else:reason='PAGE_CAP_REACHED'
    rows=[seen[t] for t in sorted(seen)]
    daystart=datetime.combine(day,dt_time.min,tzinfo=kst)
    in_day=sum(daystart<=r['t']<daystart+timedelta(days=1) for r in rows)
    meta={'bithumb_1m_pages':pages,'bithumb_1m_bars':len(rows),
          'bithumb_1m_day_bars':in_day, 'bithumb_1m_day_missing_slots':1440-in_day,
          'bithumb_1m_raw_count':raw_count,'bithumb_1m_invalid_rows':bad,
          'bithumb_1m_short_pages':short_pages,
          'bithumb_1m_oldest':oldest.isoformat() if oldest else '',
          'bithumb_1m_newest':newest.isoformat() if newest else '',
          'bithumb_1m_cursor_status':reason,
          'bithumb_1m_history_complete':reached,
          'bithumb_1m_coverage_note':'빈 분봉은 자동 생성하거나 보간하지 않음'}
    return (rows,pages,meta) if with_meta else (rows,pages)


def _core532_fetch_5m_audit(api_get,market,day,kst):
    """INDEPENDENT completed 5m Bithumb OHLCV: retrospective audit only.

    Never use these bars to create 1m entry prices or to pass a BUY gate. A
    5m bar closing after a signal contains unknowable future-at-signal data.
    """
    start=datetime.combine(day,dt_time.min,tzinfo=kst)-timedelta(minutes=15)
    end=datetime.combine(day+timedelta(days=1),dt_time.min,tzinfo=kst)+timedelta(minutes=20)
    cursor=end;seen={};pages=0;oldest=None;reached=False;reason=''
    for _ in range(8):
        raw=api_get('/v1/candles/minutes/5',{'market':market,'to':cursor.strftime(KST_FORMAT),'count':200})
        if not isinstance(raw,list):raise ValueError('빗썸 5분봉 응답 형식 오류')
        pages+=1
        if not raw:reason='EMPTY_BEFORE_START';break
        page=[]
        for b in raw:
            try:
                bar=_parse_candle(b,kst);page.append(bar)
                if start<=bar['t']<end:seen[bar['t']]=bar
            except (ValueError,KeyError,TypeError,OverflowError):continue
        if not page:reason='INVALID_5M_PAGE';break
        oldest=min(r['t'] for r in page)
        if oldest<=start:reached=True;reason='START_REACHED';break
        if oldest>=cursor:reason='CURSOR_STALLED';break
        cursor=oldest
    else:reason='PAGE_CAP_REACHED'
    return seen,{'bithumb_5m_pages':pages,'bithumb_5m_bars':len(seen),
                 'bithumb_5m_history_complete':reached,'bithumb_5m_cursor_status':reason}


def _core532_bucket_start(t):
    return t.replace(minute=(t.minute//5)*5,second=0,microsecond=0)


def _core532_5m_check(bucket, minute_by_t, five_by_t):
    """Audit if a sparse bucket's 1m OHLCV reconciles to 5m independently.

    Checks use the COMPLETE 5m bar, so ALL such labels are retrospective only,
    forbidden for trading decisions at the earlier 1m timestamp.
    """
    one=timedelta(minutes=1)
    bar=five_by_t.get(bucket)
    ms=[minute_by_t.get(bucket+i*one) for i in range(5)]
    actual=[x for x in ms if x is not None]
    missing=sum(x is None for x in ms)
    if not bar:return 'NO_5M_BAR_AUDIT',missing
    if not actual:return 'NO_1M_BAR_5M_EXISTS_AUDIT',missing
    # Aggregated turnover & OHLC: independent data agreement gives strong
    # retrospective evidence that absent 1m slots contained no trades.
    cash=sum(x['cash'] for x in actual)
    volume=sum(x['v'] for x in actual)
    def near(x,y):return abs(x-y)<=max(1e-8,max(abs(x),abs(y))*0.005)
    price_ok=(near(actual[0]['o'],bar['o']) and near(actual[-1]['c'],bar['c'])
              and near(max(x['h'] for x in actual),bar['h'])
              and near(min(x['l'] for x in actual),bar['l']))
    if not near(cash,bar['cash']) or not near(volume,bar['v']) or not price_ok:
        return 'SOURCE_MISMATCH_AUDIT',missing
    return ('NO_TRADE_SLOTS_SUPPORTED_AUDIT' if missing else 'FULLY_MATCHED_AUDIT'),missing


def _core532_audit_event(e, minute_by_t, five_by_t, history_complete):
    """Label missing 1m before signal using INDEPENDENT 5m hindsight.

    Never turn these labels into a trade. The five-minute bar may close AFTER
    the signal, therefore it must NEVER be used as a pre-signal BUY filter.
    """
    signal=datetime.fromisoformat(e['signal_at']);one=timedelta(minutes=1)
    missing=[signal-k*one for k in range(6,0,-1) if signal-k*one not in minute_by_t]
    result={'pre_missing_1m_count':len(missing),
            'pre_missing_1m_times':','.join(x.strftime('%H:%M') for x in missing),
            'pre_audit_use':'POST_HOC_ONLY_NEVER_ENTRY'}
    if not missing:
        return dict(result,pre_5m_audit='NO_GAP')
    if not history_complete:
        return dict(result,pre_5m_audit='FETCH_INCOMPLETE_UNRESOLVED')
    audits={}
    for t in missing:
        b=_core532_bucket_start(t)
        if b not in audits:audits[b]=_core532_5m_check(b,minute_by_t,five_by_t)[0]
    labels=set(audits.values())
    if labels=={'NO_TRADE_SLOTS_SUPPORTED_AUDIT'}:
        classification='LIKELY_NO_TRADE_AUDIT_ONLY'
    elif 'SOURCE_MISMATCH_AUDIT' in labels or 'NO_1M_BAR_5M_EXISTS_AUDIT' in labels:
        classification='SOURCE_MISMATCH_AUDIT_ONLY'
    elif 'NO_5M_BAR_AUDIT' in labels:
        classification='5M_DATA_ABSENT_UNRESOLVED'
    else:classification='MIXED_OR_UNRESOLVED'
    result['pre_5m_audit']=classification
    result['pre_5m_bucket_details']='|'.join(b.strftime('%H:%M')+':'+v for b,v in sorted(audits.items()))
    return result


def _core532_sparse_quote_diag(e, minute_by_t):
    """Past-only stale last-trade snapshot; OBSERVATION ONLY, never BUY.

    If a one-minute candle was not produced, last known traded close is stale;
    use it only for an audit diagnostic with age, never a fillable live quote.
    """
    signal=datetime.fromisoformat(e['signal_at']);one=timedelta(minutes=1)
    newest=next((minute_by_t[signal-k*one] for k in range(1,16)
                 if signal-k*one in minute_by_t),None)
    baseline=next((minute_by_t[signal-k*one] for k in range(6,22)
                   if signal-k*one in minute_by_t),None)
    result={'sparse_quote_use':'PAST_ONLY_DIAGNOSTIC_NOT_EXECUTABLE',
            'sparse_last_trade_at':newest['t'].isoformat() if newest else '',
            'sparse_reference_at':baseline['t'].isoformat() if baseline else '',
            'sparse_latest_stale_min':int((signal-one-newest['t']).total_seconds()/60) if newest else None,
            'sparse_reference_stale_min':int((signal-6*one-baseline['t']).total_seconds()/60) if baseline else None}
    if newest is not None and baseline is not None and baseline['t']<newest['t']:
        ret=_price_pct(newest['c'],baseline['c'])
        result.update(sparse_bithumb_5m_ret_pct=round(ret,5),
                      sparse_binance_gap_pct=round(e['binance_return_5m_pct']-ret,5))
    else:result.update(sparse_bithumb_5m_ret_pct=None,sparse_binance_gap_pct=None)
    return result


def _core53_post_reaction(e,bn_by_t,bt_by_t):
    """AFTER-signal labels for research ONLY: NEVER filter trade entries with these.

    All return percentages are relative to the LAST COMPLETED 1m CLOSE at
    signal time. A missing intervening minute makes that horizon UNKNOWN.
    """
    signal=datetime.fromisoformat(e['signal_at']);one=timedelta(minutes=1)
    pre=bt_by_t.get(signal-one)
    result={'post_label_use':'AFTER_SIGNAL_DIAGNOSTIC_ONLY'}
    if pre is None:
        for k in range(1,11):result[f'post_{k}m_status']='UNKNOWN_PRE_SIGNAL_BITHUMB'
        return result
    base_bn=e['binance_price'];base_bt=pre['c']
    first_bn=None;first_bt=None
    ok_bn=True;ok_bt=True
    for k in range(1,11):
        b=bn_by_t.get(signal+(k-1)*one)
        t=bt_by_t.get(signal+(k-1)*one)
        ok_bn=ok_bn and b is not None
        ok_bt=ok_bt and t is not None
        if ok_bn and first_bn is None and _price_pct(b['c'],base_bn)>=.5:first_bn=k
        if ok_bt and first_bt is None and _price_pct(t['c'],base_bt)>=.5:first_bt=k
        result[f'post_{k}m_status']='OK' if ok_bn and ok_bt else 'UNKNOWN_1M_GAP'
        result[f'post_{k}m_binance_pct']=round(_price_pct(b['c'],base_bn),5) if ok_bn else None
        result[f'post_{k}m_bithumb_pct']=round(_price_pct(t['c'],base_bt),5) if ok_bt else None
        result[f'post_{k}m_gap_pct']=round(result[f'post_{k}m_binance_pct']-result[f'post_{k}m_bithumb_pct'],5) if ok_bn and ok_bt else None
    result.update(binance_post_0p5m_first_min=first_bn,bithumb_post_0p5m_first_min=first_bt,
                  reaction_10m_complete=ok_bn and ok_bt)
    return result


def _core53_net_be(entry,fee,slip):
    s=(fee+slip)/100
    return entry*(1+s)/(1-s)


def _core53_exit_4tp(bars, entry_at,fee=.25,slip=.15,hold=240,by_t=None, *, stop_price=None):
    """Gross TP 3/5/10/30%, sell 30/30/20/20%; conservative stop-first.

    +3% first fill triggers FEE-ADJUSTED net BE for all remaining units starting
    with the NEXT minute, not retroactively in the same ambiguous candle.
    """
    if by_t is None:by_t={b['t']:b for b in bars}
    one=timedelta(minutes=1)
    first=by_t.get(entry_at)
    if first is None:return {'status':'UNKNOWN','reason':'ENTRY_1M_MISSING'}
    entry=first['o'];stop=entry*.97 if stop_price is None else float(stop_price);be=_core53_net_be(entry,fee,slip)
    remain=1.;realized=0.;tp_count=0;fills=[];active_be=False
    cost=(fee+slip)/100
    def sale(frac,px,reason,t):
        nonlocal remain,realized
        remain=round(remain-frac,10)
        realized+=frac*px*(1-cost)/(entry*(1+cost))
        fills.append({'at':t.isoformat(),'reason':reason,'fraction':round(frac,5),'price':round(px,12)})
    for n in range(hold):
        t=entry_at+n*one;b=by_t.get(t)
        if b is None:return {'status':'UNKNOWN','reason':'EXIT_1M_MISSING','partial_fills':fills}
        floor=be if active_be else stop
        # stop-first if stop and target touch the same bar; opening gaps take OPEN.
        if b['o']<=floor or b['l']<=floor:
            px=b['o'] if b['o']<=floor else floor
            sale(remain,px,'NET_BE_STOP' if active_be else 'HARD_STOP',t)
            return {'status':'READY','exit':fills[-1]['reason'],
                    'entry_at':entry_at.isoformat(),'exit_at':(t+one).isoformat(),
                    'entry_price':entry,'net_pct':round((realized-1)*100,5),
                    'tp_count':tp_count,'fills':fills,'bars_held':n+1,'be_price':be}
        was_active=active_be
        while tp_count<len(CORE53_TPS):
            target,fraction=CORE53_TPS[tp_count];px=entry*(1+target/100)
            if b['h']+1e-12<px:break
            sale(fraction,max(px,b['o']),'TP_'+str(int(target)),t)
            tp_count+=1
        if tp_count==len(CORE53_TPS):
            return {'status':'READY','exit':'TP30_ALL','entry_at':entry_at.isoformat(),
                    'exit_at':(t+one).isoformat(),'entry_price':entry,
                    'net_pct':round((realized-1)*100,5),'tp_count':tp_count,
                    'fills':fills,'bars_held':n+1,'be_price':be}
        if tp_count>=1:active_be=True
        # BE is never checked in same candle as first TP (unknown intra-bar order).
    final_at=entry_at+(hold-1)*one
    bar=by_t[final_at]
    sale(remain,bar['c'],'TIME_'+str(hold)+'M',final_at)
    return {'status':'READY','exit':'TIME_'+str(hold)+'M','entry_at':entry_at.isoformat(),
            'exit_at':(final_at+one).isoformat(),'entry_price':entry,
            'net_pct':round((realized-1)*100,5),'tp_count':tp_count,
            'fills':fills,'bars_held':hold,'be_price':be}


def _core53_align_event(e,bt_rows,fee=.25,slip=.15,hold=240,by_t=None):
    from datetime import datetime
    s=datetime.fromisoformat(e['signal_at']);t1=timedelta(minutes=1)
    if by_t is None:by_t={r['t']:r for r in bt_rows}
    prev=by_t.get(s-t1);back=by_t.get(s-timedelta(minutes=6))
    if any(by_t.get(s-k*t1) is None for k in range(1,7)):
        return dict(e,comparison='UNKNOWN',reason='BITHUMB_PRE_SIGNAL_5M_GAP',status='UNKNOWN')
    bret=_price_pct(prev['c'],back['c']);gap=e['binance_return_5m_pct']-bret
    e=dict(e,bithumb_return_5m_pct=round(bret,5),lead_gap_pct=round(gap,5))
    if bret>1.0:
        return dict(e,comparison='BITHUMB_ALREADY_MOVED',status='NO_ENTRY')
    if gap<.50:
        return dict(e,comparison='NO_VERIFIED_LEAD',status='NO_ENTRY')
    # Entry is delayed 1 minute after the first NEXT 1m open to reduce idealized
    # instantaneous cross-exchange execution assumptions.
    entry_at=s+t1
    result=_core53_exit_4tp(bt_rows,entry_at,fee,slip,hold,by_t=by_t)
    return dict(e,comparison='BINANCE_LEAD',**result)


# --- CORE.54 simultaneous global momentum -> Bithumb pullback/reacceleration ---
# All these thresholds are research hypotheses, NEVER orders or live buy signals.
CORE54_QUIET_MIN = 30
CORE54_SCAN_MIN = 20
CORE54_PULLBACK_MIN = .8
CORE54_PULLBACK_MAX = 3.5
CORE54_REBOUND_MIN = .6


def _core54_bithumb_retest(e, bt_by_t, bn_by_t, fee=.25, slip=.15, hold=240,
                           previous_spike_at=None, history_complete=True):
    """B-type paper setup: completed Binance spike -> completed Bithumb pullback
    -> reacceleration close -> entry at NEXT 1m open.  No post-entry leaks.

    All conditions are evaluated sequentially on one-minute candle closes.
    A missing 1m candle is UNKNOWN, never assumed to be a flat bar.
    """
    minute = timedelta(minutes=1)
    signal = datetime.fromisoformat(e['signal_at'])
    out = {'strategy':'B_RETEST', 'date':e['date'], 'market':e['market'],
           'symbol':e['symbol'], 'signal_at':e['signal_at'],
           'burst_group':e['burst_group'], 'signal_type':e['signal_type'],
           'stage':'UNASSESSED', 'status':'NO_ENTRY', 'reason':'',
           'entry_at':None, 'net_pct':None, 'signal_first':bool(e['episode_first']),
           'signal_1m_quote_ratio':e['binance_quote_ratio_1m'],
           'signal_5m_quote_ratio':e['binance_quote_ratio_5m']}
    def finish(stage, reason, status='NO_ENTRY'):
        out.update(stage=stage,reason=reason,status=status)
        return out
    if not e['episode_first']:
        return finish('NOT_FIRST_EPISODE', '같은 급증 구간의 반복 신호')
    # The first 30 minutes of the day cannot prove there were no prior-day spikes.
    midnight=signal.replace(hour=0,minute=0,second=0,microsecond=0)
    if signal-midnight < timedelta(minutes=CORE54_QUIET_MIN) and previous_spike_at is None:
        return finish('INITIAL_HISTORY_UNKNOWN','당일 00시 이전 30분 급증 여부 미검사','UNKNOWN')
    if previous_spike_at is not None and signal-previous_spike_at < timedelta(minutes=CORE54_QUIET_MIN):
        return finish('REPEATED_SPIKE','직전 급증 신호 이후 30분 미경과')
    pre_times = [signal-i*minute for i in range(6,0,-1)]
    pre=[bt_by_t.get(t) for t in pre_times]
    if not history_complete or any(x is None for x in pre):
        return finish('PRE_SIGNAL_DATA_UNKNOWN','빗썸 신호 직전 6개 1분봉 불연속','UNKNOWN')
    pre5=_price_pct(pre[-1]['c'],pre[0]['c'])
    out['bithumb_pre_5m_pct']=round(pre5,5)
    if not .5 <= pre5 <= 8.0:
        return finish('INITIAL_MOMENTUM_OUT_OF_RANGE','동시 가속 연구 기준 5분 +0.5~+8% 이외')
    bn_base=bn_by_t.get(signal-timedelta(minutes=61))
    bn_last=bn_by_t.get(signal-minute)
    if bn_base is None or bn_last is None:
        return finish('BINANCE_60M_UNKNOWN','바이낸스 60분 이전 가격 정보 없음','UNKNOWN')
    bn60=_price_pct(bn_last['c'],bn_base['c'])
    out['binance_pre_60m_pct']=round(bn60,5)
    if bn60 > 20:
        return finish('LATE_GLOBAL_RUNUP','바이낸스 직전 60분 +20% 초과 · 추격 위험')
    impulse=pre[-1]
    initial_low=min(x['l'] for x in pre)
    support_floor=initial_low*.99
    peak=impulse['h'];peak_cash=max(impulse['cash'],1e-9)
    trough=None;trough_at=None;dip_at=None
    state='IMPULSE'
    previous=[]
    for step in range(CORE54_SCAN_MIN):
        t=signal+step*minute
        c=bt_by_t.get(t)
        if c is None:
            out['observed_minutes']=step
            return finish('POST_SIGNAL_DATA_UNKNOWN','빗썸 회복 확인 중 1분봉 누락','UNKNOWN')
        previous.append(c)
        # Strong impulse is allowed for only first three CLOSED post-signal bars.
        if state=='IMPULSE' and step<=2:
            peak=max(peak,c['h']);peak_cash=max(peak_cash,c['cash'])
        if c['l'] < support_floor:
            return finish('SUPPORT_BROKEN','급등 직전 5분 저점 아래로 이탈')
        if _price_pct(peak,impulse['c'])>8:
            return finish('ALREADY_EXTENDED','초기 기준가 대비 고점 +8% 초과')
        drawdown=100*(1-c['c']/peak)  # positive when price pulls back
        if drawdown>CORE54_PULLBACK_MAX:
            out['pullback_pct']=round(drawdown,4)
            return finish('PULLBACK_TOO_DEEP','고점 대비 종가 눌림 3.5% 초과')
        if state=='IMPULSE':
            # At least two completed post-signal minutes before a support test.
            if step < 1 or drawdown<CORE54_PULLBACK_MIN:
                continue
            # The pullback must have lower turnover than the original impulse;
            # compare only bars that had already closed at this step.
            recent=previous[-2:]
            ratio=(sum(x['cash'] for x in recent)/len(recent))/peak_cash
            out['pullback_turnover_ratio']=round(ratio,4)
            if ratio > .85:
                return finish('NO_VOLUME_FADE','눌림 구간 거래대금 감소가 충분하지 않음')
            state='PULLBACK';dip_at=t;trough=c['l'];trough_at=t
            out['pullback_at']=t.isoformat()
            out['pullback_pct']=round(drawdown,4)
            continue
        # Price can consolidate, but is not allowed to re-break the support.
        if c['l']<trough:
            trough=c['l'];trough_at=t
        out['pullback_pct']=round(max(out.get('pullback_pct',0),drawdown),4)
        if t<=dip_at:
            continue
        # Recovery candle: bullish, higher close vs previous two CLOSED bars,
        # > 0.6% from the lowest observed support, SMA5 reclaim, solid volume.
        lookback=[bt_by_t.get(t-i*minute) for i in range(4,-1,-1)]
        if any(x is None for x in lookback):
            return finish('RECOVERY_HISTORY_UNKNOWN','회복 시 5개 연속 분봉 부족','UNKNOWN')
        prev_c=bt_by_t.get(t-minute)['c']
        prior2=bt_by_t.get(t-2*minute)
        if prior2 is None:
            return finish('RECOVERY_HISTORY_UNKNOWN','회복 직전 연속 봉 부족','UNKNOWN')
        sma5=sum(x['c'] for x in lookback)/5
        recovered=(c['c']>c['o'] and c['c']>max(prev_c,prior2['c'])
                   and c['c']>sma5 and _price_pct(c['c'],trough)>=CORE54_REBOUND_MIN
                   and c['cash']>=.75*peak_cash)
        if not recovered:
            continue
        # No chases at recovery: only accept near the pre-recovery impulse high.
        if c['c']>peak*1.015:
            return finish('RECOVERY_CHASE','회복봉이 관찰 고점 대비 +1.5% 넘게 돌파')
        entry_at=t+minute
        out.update(stage='ENTRY_CONFIRM', reason='눌림 저거래량·지지·재상승 확인',
                   recovery_at=(t+minute).isoformat(),
                   recovery_close=c['c'], entry_at=entry_at.isoformat(),
                   pullback_low=round(trough,12),
                   rebound_from_low_pct=round(_price_pct(c['c'],trough),5),
                   recovery_turnover_ratio=round(c['cash']/peak_cash,4),
                   observed_minutes=step+1)
        # Backtest entry pricing starts only at the NEXT candle open.
        result=_core53_exit_4tp([],entry_at,fee,slip,hold,by_t=bt_by_t)
        out.update(result)
        if result['status']=='UNKNOWN':
            out['stage']='EXIT_DATA_UNKNOWN'
        else:
            out['stage']='PAPER_ENTRY'
        return out
    if state=='IMPULSE':
        return finish('NO_VALID_PULLBACK','20분 안에 충분한 거래대금 감소 눌림 없음')
    return finish('NO_RECOVERY','20분 안에 지지 후 재상승 확정 없음')


def _core54_scan_b_retests(events, bt_rows, bn_rows, fee,slip,hold,
                            history_complete=True):
    bt_by_t={x['t']:x for x in bt_rows};bn_by_t={x['t']:x for x in bn_rows}
    out=[];previous=None
    for e in events:
        if e.get('episode_first'):
            out.append(_core54_bithumb_retest(e,bt_by_t,bn_by_t,fee,slip,hold,
                                               previous_spike_at=previous,
                                               history_complete=history_complete))
        previous=datetime.fromisoformat(e['signal_at'])
    return out



# --- CORE.55: paired B vs one-bar confirmation (B2), and structural stop. ---
# B is left untouched. B2 waits for the WHOLE next one-minute candle to close.
# Structural stop is 0.20% BELOW the observed pullback low, only for risk
# distances from 0.8% to 3.0%; larger/smaller risks are explicitly ineligible.
# All variants use identical 4TP/NET_BE exit mechanics and exchange candles.
CORE55_STRUCT_BUFFER_PCT = 0.20
CORE55_STRUCT_MIN_RISK_PCT = 0.80
CORE55_STRUCT_MAX_RISK_PCT = 3.00
CORE55_B2_MAX_CONFIRM_CHASE_PCT = 2.0


def _core55_make_variant(base, variant, bt_by_t, fee, slip, hold):
    minute = timedelta(minutes=1)
    # The baseline recovery is a necessary prerequisite, NOT a selected trade.
    if not base.get('entry_at'):
        return None
    result = {k:base.get(k) for k in (
        'date','market','symbol','signal_at','burst_group','pullback_at',
        'pullback_pct','pullback_low','rebound_from_low_pct',
        'pullback_turnover_ratio','recovery_turnover_ratio','recovery_close')}
    result.update(strategy=variant, variant=variant, entry_at=None,
                  status='NO_ENTRY',stage='UNASSESSED',reason='',net_pct=None)
    base_entry = datetime.fromisoformat(base['entry_at'])
    entry_at = base_entry
    if variant.startswith('B2_'):
        # First bar AFTER recovery: baseline B enters at its open, B2 waits for
        # its close (never knowing the result at the original B entry moment).
        confirm = bt_by_t.get(base_entry)
        recovery = bt_by_t.get(base_entry-minute)
        if confirm is None or recovery is None:
            result.update(stage='B2_DATA_UNKNOWN',status='UNKNOWN',
                          reason='추가 확인봉 또는 기존 회복봉 자료 없음')
            return result
        lookback=[bt_by_t.get(base_entry-timedelta(minutes=i)) for i in range(4,-1,-1)]
        if any(x is None for x in lookback):
            result.update(stage='B2_DATA_UNKNOWN',status='UNKNOWN',
                          reason='추가 확인 시점 5개 연속 1분봉 부족')
            return result
        floor = float(base.get('pullback_low') or 0)
        if floor<=0:
            result.update(stage='B2_DATA_UNKNOWN',status='UNKNOWN',
                          reason='기존 눌림 저점 정보 없음')
            return result
        ma=sum(x['c'] for x in lookback)/len(lookback)
        if (confirm['l'] < floor*.998 or confirm['c'] < recovery['c']
                or confirm['c'] <= ma):
            result.update(stage='B2_CONFIRM_FAILED',reason='추가 1분봉에서 저점·회복 종가·SMA5 유지 실패')
            return result
        if _price_pct(confirm['c'],recovery['c'])>CORE55_B2_MAX_CONFIRM_CHASE_PCT:
            result.update(stage='B2_CHASE',reason='추가 확인봉이 이미 2% 초과 상승')
            return result
        entry_at = base_entry+minute
        result['confirmation_at'] = (base_entry+minute).isoformat()
        result['confirmation_close'] = confirm['c']
    entrybar=bt_by_t.get(entry_at)
    if entrybar is None:
        result.update(stage='ENTRY_DATA_UNKNOWN',status='UNKNOWN',reason='다음 1분봉 시가 자료 없음')
        return result
    entry=entrybar['o']
    if not entry or entry<=0:
        result.update(stage='ENTRY_DATA_UNKNOWN',status='UNKNOWN',reason='진입가격 오류')
        return result
    stop = entry*.97
    if variant.endswith('_STRUCT'):
        low=float(base.get('pullback_low') or 0)
        if low<=0:
            result.update(stage='STRUCT_NO_LOW',status='UNKNOWN',reason='눌림 저점 자료 없음')
            return result
        stop=low*(1-CORE55_STRUCT_BUFFER_PCT/100)
        risk_pct=100*(entry-stop)/entry  # structural risk as percentage of entry
        if risk_pct<=0:
            result.update(stage='STRUCT_ALREADY_BROKEN',reason='진입 시 구조적 손절선 아래, 진입 제외',
                          structure_risk_pct=round(risk_pct,5))
            return result
        if not CORE55_STRUCT_MIN_RISK_PCT<=risk_pct<=CORE55_STRUCT_MAX_RISK_PCT:
            result.update(stage='STRUCT_RISK_OUT_OF_RANGE',
                          reason='눌림저점 손절 거리 0.8~3.0% 밖, 진입 제외',
                          structure_risk_pct=round(risk_pct,5))
            return result
        result['structure_risk_pct']=round(risk_pct,5)
    result.update(entry_at=entry_at.isoformat(),entry_price=entry,
                  stop_price=round(stop,12),stop_gross_pct=round(100*(1-stop/entry),5))
    exit_result=_core53_exit_4tp([],entry_at,fee,slip,hold,by_t=bt_by_t,
                                  stop_price=(stop if variant.endswith('_STRUCT') else None))
    result.update(exit_result)
    result['stage']='PAPER_ENTRY' if exit_result['status']=='READY' else 'EXIT_DATA_UNKNOWN'
    if result['stage']=='EXIT_DATA_UNKNOWN':
        result['reason']=exit_result.get('reason','청산 자료 부족')
    else:
        result['reason']='기존 B 회복 후 추가 확인' if variant.startswith('B2') else '기존 B 진입·구조적 손절 비교'
    return result


def _core55_compare_variants(base_events, bt_rows, fee, slip, hold):
    bt_by_t={x['t']:x for x in bt_rows}
    result=[]
    for case in base_events:
        if not case.get('entry_at'):
            continue
        for name in ('B_FIXED','B_STRUCT','B2_FIXED','B2_STRUCT'):
            variant=_core55_make_variant(case,name,bt_by_t,fee,slip,hold)
            if variant is not None:
                result.append(variant)
    return result

def _core53_analyze_market(api_get,market,symbol,day,kst,fee,slip,hold,http_get=_core53_http):
    b,meta=_core53_fetch_binance_1m(symbol,day,kst,http_get)
    events=_core53_signals(b,day)
    for e in events:e['market']=market;e['symbol']=symbol
    pages=0;bt_rows=[];fetch_error=''; audit_error='';five_by_t={};audit_meta={}
    bt_meta={'bithumb_1m_history_complete':False,'bithumb_1m_cursor_status':'NOT_FETCHED',
             'bithumb_1m_pages':0,'bithumb_1m_bars':0}
    if events:
        try:
            bt_rows,pages,bt_meta=_core53_fetch_bithumb_day(api_get,market,day,kst,hold,with_meta=True)
        except Exception as exc:
            fetch_error=f'{type(exc).__name__}: {exc}'[:140]
        # ALWAYS keep the original exchange candles unchanged, including gaps.
        if bt_rows:
            try:five_by_t,audit_meta=_core532_fetch_5m_audit(api_get,market,day,kst)
            except Exception as exc:audit_error=f'{type(exc).__name__}: {exc}'[:140]
        bn_by_t={x['t']:x for x in b};bt_by_t={x['t']:x for x in bt_rows}
        for e in events:
            e.update(_core532_audit_event(e,bt_by_t,five_by_t,
                                          bt_meta.get('bithumb_1m_history_complete') and not bool(audit_error)))
            e['bithumb_audit_error']=audit_error
            e.update(_core532_sparse_quote_diag(e,bt_by_t))
            if fetch_error:
                e.update(comparison='UNKNOWN',status='UNKNOWN',reason='BITHUMB_FETCH_ERROR: '+fetch_error)
                e.update(_core53_post_reaction(e,bn_by_t,bt_by_t))
                continue
            try:
                # This alignment intentionally uses 1m ORIGINAL data ONLY.
                diag=_core53_post_reaction(e,bn_by_t,bt_by_t)
                result=_core53_align_event(e,bt_rows,fee,slip,hold,by_t=bt_by_t)
                e.update(result);e.update(diag)
                if not bt_meta.get('bithumb_1m_history_complete') and e['comparison']=='BINANCE_LEAD':
                    # Incomplete history should NEVER be allowed to produce a BUY.
                    e.update(comparison='UNKNOWN',status='UNKNOWN',
                             reason='BITHUMB_1M_HISTORY_INCOMPLETE',net_pct=None)
            except Exception as exc:
                e.update(comparison='UNKNOWN',status='UNKNOWN',reason=f'{type(exc).__name__}: {exc}'[:140])
    # CORE.54: independent B-type research; A-type original events remain intact.
    # Incomplete exchange history must never fabricate a BUY or a retest.
    retests=_core54_scan_b_retests(events,bt_rows,b,fee,slip,hold,
                     history_complete=(bt_meta.get('bithumb_1m_history_complete',False)
                                       and not bool(fetch_error)))
    variants=_core55_compare_variants(retests,bt_rows,fee,slip,hold)
    start=datetime.combine(day,dt_time.min,tzinfo=kst)
    coverage=dict(meta,**bt_meta,**audit_meta,
                  detected=len(events),episodes=sum(bool(e.get('episode_first')) for e in events),
                  first_signal_at=events[0]['signal_at'] if events else '',
                  last_signal_at=events[-1]['signal_at'] if events else '',
                  compared=sum(e.get('comparison') not in (None,'UNKNOWN') for e in events),
                  sim_ready=sum(e.get('status')=='READY' for e in events),
                  bithumb_error=fetch_error,bithumb_5m_audit_error=audit_error,
                  pre_gap_events=sum(e.get('pre_missing_1m_count',0)>0 for e in events),
                  pre_gap_no_trade_supported=sum(e.get('pre_5m_audit')=='LIKELY_NO_TRADE_AUDIT_ONLY' for e in events),
                  pre_gap_source_mismatch=sum(e.get('pre_5m_audit')=='SOURCE_MISMATCH_AUDIT_ONLY' for e in events),
                  binance_day_bars=sum(start<=x['t']<start+timedelta(days=1) for x in b),
                  binance_day_expected=1440,
                  reaction_10m_complete=sum(e.get('reaction_10m_complete') is True for e in events),
                  full_day_detection=True,raw_spikes_not_capped=True,
                  gap_audit_diagnostic_only=True,
                  core54_first_episode_candidates=len(retests),
                  core54_b_confirmed=sum(x.get('entry_at') is not None for x in retests),
                  core54_b_ready=sum(x.get('status')=='READY' for x in retests),
                  core54_b_unknown=sum(x.get('status')=='UNKNOWN' for x in retests),
                  core55_comparison_records=len(variants),
                  core55_b2_ready=sum(x['status']=='READY' and x['strategy']=='B2_FIXED' for x in variants))
    return {'date':day.isoformat(),'market':market,'symbol':symbol,
            'events':events,'momentum_retests':retests,'momentum_variants':variants,'coverage':coverage}


CORE_PAGE = '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n<title>V2 CORE.56 LIGHT · 바이낸스 모멘텀 · B2 확인봉 / 구조적 손절 비교 연구</title>\n<style>\n:root{color-scheme:dark;--bg:#09111f;--panel:#162139;--edge:#33445e;--text:#eaf1ff;--sub:#b6c8e2;--ok:#68dfa3;--bad:#ff9aa9;--accent:#7fa8ff}\n*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,-apple-system,sans-serif}\nmain{max-width:1160px;margin:0 auto;padding:14px 12px 65px}h1{font-size:21px;margin:0 0 4px}h2{font-size:17px;margin:0 0 9px}h3{font-size:14px;margin:14px 0 7px}p{margin:7px 0}\n.panel{background:var(--panel);border:1px solid var(--edge);border-radius:13px;padding:14px;margin:12px 0}\n.muted{color:var(--sub);font-size:12px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}\n.field{display:flex;flex-direction:column;gap:4px}label{font-size:12px;color:var(--sub)}select,input,button{width:100%;border:1px solid #50637e;border-radius:8px;background:#0d182c;color:var(--text);padding:9px;font:inherit}\nbutton{background:#426cbd;font-weight:700;cursor:pointer}button.alt{background:#30405f}button:disabled{opacity:.45;cursor:default}.buttons{display:flex;flex-wrap:wrap;gap:8px;margin:13px 0}.buttons button{flex:1;min-width:125px}\n.kpi{background:#101d31;border:1px solid #364b67;border-radius:10px;padding:11px}.kpi strong{font-size:22px;display:block;font-variant-numeric:tabular-nums}.note{border-radius:9px;background:#0e1a2e;color:#c7d6ed;padding:12px;margin:9px 0;font-size:12px}.good{color:var(--ok)}.bad{color:var(--bad)}.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;min-width:780px}th,td{padding:9px 8px;border-bottom:1px solid #33445e;text-align:right;font-size:12px;white-space:nowrap}th:first-child,td:first-child{text-align:left}a{color:#a5c1ff}progress{width:100%;height:12px}\n@media(max-width:660px){main{padding:9px 8px 45px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.panel{padding:11px}}\n</style></head><body><main>\n<header><h1>🧪 V2 CORE.56 LIGHT · 모멘텀 B2 확인·구조적 손절 비교</h1><p class="muted">바이낸스 현물 급증은 감지기, 빗썸 분봉은 진입 확인용 · A/B 기존 유지 + B2·구조적 손절 별도 연구 · 자동 주문 없음</p><a href="/">← 기존 스캐너로 이동</a></header>\n<section class="panel"><h2>① 이번 연구의 두 가지 방식</h2>\n<div class="grid"><div class="note"><b>A · 바이낸스 선행형 (기존)</b><p>바이낸스 직전 5분 상승률이 빗썸보다 0.5%p 이상 앞서고 빗썸 5분 상승률이 +1% 이내인 경우, 1분 지연 진입</p></div>\n<div class="note"><b>B · 동시 가속 후 눌림·재상승 (신규)</b><p>급증 30분 공백 후 첫 신호 → 빗썸 5분 +0.5~8% → 20분 내 0.8~3.5% 눌림·거래대금 감소 → 지지·SMA5 위 마감봉 확인 → 다음 1분봉 시가 진입</p></div></div>\n<div class="grid"><div class="note"><b>손절 · 기존 유지</b><p>가격 기준 -3%, 손절 체결이 보장되지는 않음</p></div><div class="note"><b>분할 익절 · 기존 유지</b><p>+3% 30% / +5% 30% / +10% 20% / +30% 20%</p></div><div class="note"><b>1차 익절 후</b><p>잔여 70%는 수수료·슬리피지 포함 순본절 스톱, 다음 1분봉부터 활성화</p></div></div>\n<p class="muted">CORE.55 기준을 유지: 기존 B 진입과 B2(회복 다음 1분봉이 마감될 때까지 추가 확인), 구조적 손절(눌림 저점 -0.2%, 진입 손절폭 0.8~3.0%만 유효)을 각각 비교해. B2는 확인봉 마감 다음 1분봉 시가에 진입. 모든 방식은 각각 독립 가상 계좌, 원래 4단계 익절·순본절 스톱 유지. </p><p class="muted">B 기준의 수치들은 이익이 검증된 값이 아닌 시작 가설이야. 신호 이후 미래 봉을 사용해 과거 시점의 BUY를 만들지 않고, 모든 가상 진입은 회복봉 마감 이후에만 가능해.</p></section>\n<section class="panel"><h2>② 테스트 설정</h2><div class="grid">\n<div class="field"><label for="day">마지막 완료 날짜 (KST)</label><input type="date" id="day"></div>\n<div class="field"><label for="days">날짜 수</label><select id="days"><option value="1" selected>1일</option><option value="3">3일</option><option value="7">7일</option><option value="14">14일 (API 요청 많음)</option></select></div>\n<div class="field"><label for="scope">검사 범위</label><select id="scope"><option value="examples" selected>MMT · PIEVERSE 우선</option><option value="custom">코인 직접 선택</option><option value="all">전체 현물 동시상장 (시간 오래 걸림)</option></select></div>\n<div class="field"><label for="custom">선택 코인(쉼표 구분)</label><input id="custom" value="MMT,PIEVERSE" placeholder="MMT,PIEVERSE"></div>\n<div class="field"><label for="fee">편도 수수료 %</label><input id="fee" type="number" value="0.25" step="0.01" min="0" max="0.5"></div>\n<div class="field"><label for="slip">편도 슬리피지 %</label><select id="slip"><option value="0.10">0.10</option><option value="0.15" selected>0.15</option><option value="0.20">0.20</option></select></div>\n<div class="field"><label for="hold">최대 관찰 시간</label><select id="hold"><option value="120">120분</option><option value="240" selected>240분</option><option value="480">480분</option></select></div>\n<div class="field"><label for="risk">거래당 계좌 위험</label><select id="risk"><option value="0.5" selected>0.5%</option><option value="1">1.0%</option></select></div>\n<div class="field"><label for="dailyLoss">일 손실 중단</label><select id="dailyLoss"><option value="1">1.0%</option><option value="1.5" selected>1.5%</option><option value="2">2.0%</option></select></div>\n</div><div class="buttons"><button id="run">CORE.56 기존 비교군 스캔</button><button id="pause" disabled class="alt">중지 요청</button><button id="csv" disabled class="alt">CORE.56 기존 비교군 CSV 저장</button></div>\n<progress id="bar" value="0" max="1"></progress><p id="status" class="muted">검사 전</p>\n<div class="note">바이낸스 거래대금·공격적 매수 1분/5분 급증 기준은 CORE.53.2 그대로 유지해. 빗썸 1분봉이 없으면 임의 보간하지 않고 UNKNOWN으로 분류해. 5분봉 일치 검사는 사후 자료 품질 진단용이며 진입 판단에는 사용하지 않아. 과거 호가 스프레드 및 급변 시 슬리피지는 정확히 재현할 수 없어.</div></section>\n<section class="panel"><h2>③ 검사 요약</h2><div class="grid">\n<div class="kpi"><span class="muted">검사 완료</span><strong id="done">—</strong></div>\n<div class="kpi"><span class="muted">바이낸스 원신호</span><strong id="spikes">—</strong></div>\n<div class="kpi"><span class="muted">B 첫 급증 후보</span><strong id="starts">—</strong></div>\n<div class="kpi"><span class="muted">B 재상승 확정</span><strong id="bConfirm">—</strong></div>\n<div class="kpi"><span class="muted">데이터 오류/UNKNOWN</span><strong id="error">—</strong></div></div>\n<h3>기존 A/B 및 B2·구조적 손절 비교 · 각 전략마다 하루 4회 제한 적용</h3>\n<div class="tablewrap"><table><thead><tr><th>전략</th><th>선정</th><th>수익/손실</th><th>결과 확정</th><th>평균 순손익</th><th>수익 기대값</th><th>가상 계좌 변화</th><th>검증</th></tr></thead><tbody id="summary"><tr><td colspan="8">검사 전</td></tr></tbody></table></div>\n<p class="muted">확정 거래 평균 순손익 = 승률×평균 수익 − 패율×평균 손실과 같아. 두 전략은 독립 가상 계좌로 비교해. 미확정 매매는 통계에서 제외하되, 위험 선정 과정은 보수적으로 중단해. 거래가 적거나 자료가 불완전하면 우열을 판단할 수 없어.</p></section>\n<section class="panel"><h2>④ B형 · 왜 진입하거나 제외됐을까?</h2>\n<div class="tablewrap"><table><thead><tr><th>신호 시각 / 코인</th><th>진단 단계</th><th>빗썸 직전 5분</th><th>바이낸스 직전 60분</th><th>눌림 깊이</th><th>거래대금 감소 비</th><th>회복 뒤 반등</th><th>가상 진입</th><th>사유</th></tr></thead><tbody id="cases"><tr><td colspan="9">검사 전</td></tr></tbody></table></div>\n<p class="muted">새 변형도 기존 B 진입이 발견된 경우에만 비교하며, 확인 실패와 손절폭 부적격을 CSV에 남겨. B형은 모든 바이낸스 반복 신호를 매수 후보로 세지 않아. 급증 구간의 첫 신호만 평가하며 30분 내 직전 급증이 있으면 제외해. 자료가 없을 때는 잘못된 진입을 만들지 않고 UNKNOWN 처리해.</p></section>\n<section class="panel"><h2>⑤ A/B 및 B2·구조적 손절 가상 선정 거래</h2><div class="tablewrap"><table><thead><tr><th>방식 / 코인</th><th>진입 KST</th><th>청산</th><th>TP 달성</th><th>순손익</th><th>계좌 반영</th><th>연구 사유</th></tr></thead><tbody id="trades"><tr><td colspan="7">검사 전</td></tr></tbody></table></div></section>\n<section class="panel"><h2>⑥ B·B2와 고정·구조 손절 비교 상세</h2><div class="tablewrap"><table><thead><tr><th>유형 / 종목</th><th>원래 B 신호</th><th>진입 시각</th><th>진입가</th><th>손절폭</th><th>상태</th><th>순손익</th><th>탈락·청산 이유</th></tr></thead><tbody id="variants"><tr><td colspan="8">검사 전</td></tr></tbody></table></div><p class="muted">구조 손절은 눌림 저점의 0.2% 아래. 기준 진입가 대비 구조적 손절까지 0.8~3%인 신호만 유효하며, 불리한 순간 갭·슬리피지는 실제 손실을 키울 수 있어. B2는 다음 완료봉을 확인한 뒤 별도 가격에 진입해.</p></section>\n<section class="panel"><h2>⑦ 거래대금 급증 원신호·데이터 품질</h2><div class="tablewrap"><table><thead><tr><th>시각 / 종목</th><th>급증 유형</th><th>1분 배수</th><th>5분 배수</th><th>빗썸 선행 판정</th><th>빗썸 사후 5분</th><th>빗썸 사후 10분</th></tr></thead><tbody id="rawEvents"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<h3>종목별 수집 상태</h3><div class="tablewrap"><table><thead><tr><th>날짜 / 종목</th><th>바이낸스 1분봉</th><th>빗썸 1분봉</th><th>B 후보</th><th>B 진입</th><th>빗썸 조회</th><th>오류</th></tr></thead><tbody id="coverage"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<p class="muted">과거 현재 상장 목록을 사용하는 데 따른 생존 편향이 남아 있어. 실제 Binance IP 제한(429/418)이 발생하면 Retry-After를 준수하며, 검사가 실패한 종목은 결과 없음과 구분해 기록해. 표는 일부만 표시하고 CSV는 전체를 저장해.</p></section>\n<script>\n\'use strict\';\nconst $=id=>document.getElementById(id);\nconst esc=x=>String(x??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nconst num=(x,n=3)=>x===undefined||x===null||!Number.isFinite(Number(x))?\'—\':Number(x).toFixed(n);\nconst today=()=>new Date(Date.now()+9*3600000).toISOString().slice(0,10);\nconst yesterday=()=>new Date(Date.now()+9*3600000-86400000).toISOString().slice(0,10);\n$(\'day\').value=yesterday();$(\'day\').max=yesterday();\nlet state=null,busy=false,paused=false;\nfunction setBusy(v){busy=v;$(\'run\').disabled=v;$(\'pause\').disabled=!v;}\nfunction dates(end,n){let ans=[];for(let j=n-1;j>=0;j--){const d=new Date(end+\'T00:00:00Z\');d.setUTCDate(d.getUTCDate()-j);ans.push(d.toISOString().slice(0,10));}return ans;}\nfunction config(){const day=$(\'day\').value,count=Number($(\'days\').value),hold=Number($(\'hold\').value),fee=Number($(\'fee\').value),slip=Number($(\'slip\').value);\n if(!day||day>=today()||![1,3,7,14].includes(count)||![120,240,480].includes(hold)||[fee,slip].some(x=>!Number.isFinite(x)||x<0||x>.5))throw Error(\'날짜·비용을 확인해줘\');\n return {days:dates(day,count),fee,slip,hold,scope:$(\'scope\').value,risk:Number($(\'risk\').value),dailyLoss:Number($(\'dailyLoss\').value)};}\nasync function call(path,body){const resp=await fetch(path,{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(body)});const data=await resp.json().catch(()=>({ok:false,error:\'JSON 응답 오류\'}));if(!resp.ok||!data.ok)throw Error(data.error||\'API \'+resp.status);return data;}\nfunction events(){return state?state.results.flatMap(x=>x.events||[]):[];}\nfunction cases(){return state?state.results.flatMap(x=>x.momentum_retests||[]):[];}\nfunction variants(){return state?state.results.flatMap(x=>x.momentum_variants||[]):[];}\nfunction selectTrades(strategy){if(!state)return {selected:[],days:[]};\n const candidates=(strategy===\'A\'?events().filter(x=>x.comparison===\'BINANCE_LEAD\'):(strategy===\'B\'?cases():variants().filter(x=>x.strategy===strategy))).filter(x=>x.entry_at)\n  .map(x=>({...x,strategy})).sort((a,b)=>a.entry_at.localeCompare(b.entry_at)||a.market.localeCompare(b.market));\n const selected=[],days=[];let globalBusy=\'\';\n const dateSet=[...new Set([...state.days,...candidates.map(x=>x.entry_at.slice(0,10))])].sort();\n for(const day of dateSet){let count=0,streak=0,equity=100,halt=\'\',busyUntil=globalBusy;\n  const daily=candidates.filter(x=>x.entry_at.slice(0,10)===day);\n  for(const e of daily){if(halt||count>=4)break;if(busyUntil&&e.entry_at<busyUntil)continue;\n   const cost=(state.fee+state.slip)/100;const stopGross=(Number(e.stop_price)>0&&Number(e.entry_price)>0?Number(e.stop_price)/Number(e.entry_price):.97);const stopNet=(stopGross*(1-cost)/(1+cost)-1)*100;\n   const pos=Math.min(100,100*state.risk/Math.abs(stopNet));\n   const net=e.status===\'READY\'&&Number.isFinite(Number(e.net_pct))?Number(e.net_pct):null;\n   const account=net===null?null:pos*net/100;\n   const item={...e,position_pct:pos,account_pct:account};selected.push(item);count++;\n   if(net===null){halt=\'UNKNOWN_OUTCOME\';break;}\n   equity*=1+account/100;streak=account<0?streak+1:0;\n   busyUntil=e.exit_at||\'9999\';if(streak>=3)halt=\'THREE_LOSSES\';if(equity<=100-state.dailyLoss)halt=\'DAILY_LOSS_LIMIT\';\n  }\n  globalBusy=busyUntil;days.push({date:day,strategy,count,halt,equity_pct:equity-100});\n }\n return {selected,days};}\nfunction stats(strategy){const r=selectTrades(strategy),ready=r.selected.filter(x=>x.status===\'READY\'),win=ready.filter(x=>x.net_pct>0),loss=ready.filter(x=>x.net_pct<=0);\n const avg=ready.length?ready.reduce((s,x)=>s+x.net_pct,0)/ready.length:null;\n const wavg=win.length?win.reduce((s,x)=>s+x.net_pct,0)/win.length:null;\n const lavg=loss.length?-loss.reduce((s,x)=>s+x.net_pct,0)/loss.length:null;\n const ev=ready.length?(win.length/ready.length)*(wavg||0)-(loss.length/ready.length)*(lavg||0):null;\n const portfolio=(r.days.reduce((s,d)=>s*(1+d.equity_pct/100),1)-1)*100;\n return {...r,n:ready.length,win:win.length,loss:loss.length,avg,ev,portfolio};}\nfunction render(){if(!state)return;\n const a=stats(\'A\'),b=stats(\'B\'),bf=stats(\'B_FIXED\'),bs=stats(\'B_STRUCT\'),b2=stats(\'B2_FIXED\'),b2s=stats(\'B2_STRUCT\'),ev=events(),bc=cases(),vc=variants(),total=state.tasks.length;\n $(\'done\').textContent=state.done+\'/\'+total;$(\'spikes\').textContent=ev.length;\n $(\'starts\').textContent=bc.filter(x=>x.stage!==\'NOT_FIRST_EPISODE\').length;\n $(\'bConfirm\').textContent=bc.filter(x=>x.stage===\'PAPER_ENTRY\'||x.stage===\'EXIT_DATA_UNKNOWN\').length;\n $(\'error\').textContent=state.errors.length+bc.filter(x=>x.status===\'UNKNOWN\').length;\n $(\'summary\').innerHTML=[[\'A · 기존 선행형\',a],[\'B · 기존 눌림(-3%)\',b],[\'B · 기존 고정손절 검산\',bf],[\'B · 구조손절\',bs],[\'B2 · 한 봉 추가확인(-3%)\',b2],[\'B2 · 한 봉 확인+구조손절\',b2s]].map(([name,x])=>`<tr><td>${esc(name)}</td><td>${x.selected.length}</td><td>${x.win}/${x.loss}</td><td>${x.n}</td><td class="${x.avg>=0?\'good\':\'bad\'}">${num(x.avg)}%</td><td class="${x.ev>=0?\'good\':\'bad\'}">${num(x.ev)}%</td><td class="${x.portfolio>=0?\'good\':\'bad\'}">${num(x.portfolio)}%</td><td>${x.n>=100?\'표본 100건 확보 · 추가 검증 필요\':\'100건 미달\'}</td></tr>`).join(\'\');\n $(\'cases\').innerHTML=bc.slice(0,160).map(x=>`<tr><td>${esc(x.signal_at+\' \'+x.market)}</td><td>${esc(x.stage)}</td><td>${num(x.bithumb_pre_5m_pct)}%</td><td>${num(x.binance_pre_60m_pct)}%</td><td>${num(x.pullback_pct)}%</td><td>${num(x.pullback_turnover_ratio,2)}</td><td>${num(x.rebound_from_low_pct)}%</td><td>${esc(x.entry_at||\'—\')}</td><td>${esc(x.reason||\'—\')}</td></tr>`).join(\'\')||\'<tr><td colspan="9">B형 후보 없음</td></tr>\';\n $(\'variants\').innerHTML=vc.slice(0,500).map(x=>`<tr><td>${esc(x.strategy+\' / \'+x.market)}</td><td>${esc(x.signal_at)}</td><td>${esc(x.entry_at||\'—\')}</td><td>${num(x.entry_price,3)}</td><td>${num(x.stop_gross_pct,2)}%</td><td>${esc(x.stage)}</td><td class="${x.net_pct>=0?\'good\':\'bad\'}">${num(x.net_pct)}%</td><td>${esc(x.reason||x.exit||\'—\')}</td></tr>`).join(\'\')||\'<tr><td colspan="8">비교할 B 진입 없음</td></tr>\';\n const all=[...a.selected,...b.selected,...bs.selected,...b2.selected,...b2s.selected].sort((x,y)=>x.entry_at.localeCompare(y.entry_at));\n $(\'trades\').innerHTML=all.slice(0,200).map(x=>`<tr><td>${esc(x.strategy+\' / \'+x.market)}</td><td>${esc(x.entry_at)}</td><td>${esc(x.exit||x.reason||\'미확정\')}</td><td>${x.tp_count||0}/4</td><td class="${x.net_pct>=0?\'good\':\'bad\'}">${num(x.net_pct)}%</td><td>${num(x.account_pct)}%</td><td>${esc(x.strategy===\'B\'?x.stage:x.comparison)}</td></tr>`).join(\'\')||\'<tr><td colspan="7">선정 없음</td></tr>\';\n $(\'rawEvents\').innerHTML=ev.slice(0,160).map(x=>`<tr><td>${esc(x.signal_at+\' \'+x.market)}</td><td>${esc(x.signal_type)}</td><td>${num(x.binance_quote_ratio_1m,2)}x</td><td>${num(x.binance_quote_ratio_5m,2)}x</td><td>${esc(x.comparison||\'미분류\')}</td><td>${num(x.post_5m_bithumb_pct)}%</td><td>${num(x.post_10m_bithumb_pct)}%</td></tr>`).join(\'\')||\'<tr><td colspan="7">급증 신호 없음</td></tr>\';\n $(\'coverage\').innerHTML=[...state.results.map(x=>`<tr><td>${esc(x.date+\' \'+x.market)}</td><td>${x.coverage.binance_day_bars||0}/1440</td><td>${x.coverage.bithumb_1m_day_bars??\'—\'}</td><td>${x.momentum_retests?.length||0}</td><td>${x.momentum_retests?.filter(z=>z.entry_at).length||0}</td><td>${esc(x.coverage.bithumb_1m_cursor_status||\'—\')}</td><td>${esc(x.coverage.bithumb_error||x.coverage.bithumb_5m_audit_error||\'—\')}</td></tr>`),...(state.missing||[]).map(n=>`<tr><td>${esc(n)}</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>현재 바이낸스 현물·빗썸 동시 상장 아님</td></tr>`),...state.errors.map(x=>`<tr><td>${esc(x.date+\' \'+x.market)}</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>${esc(x.error)}</td></tr>`)].slice(0,250).join(\'\')||\'<tr><td colspan="7">결과 없음</td></tr>\';\n $(\'bar\').max=Math.max(1,total);$(\'bar\').value=state.done;\n $(\'status\').textContent=`${state.done}/${total} 검사 · 급증 원신호 ${ev.length}건 · B형 연구후보 ${bc.length}건 · API 오류 ${state.errors.length}건 · 미상장 ${(state.missing||[]).join(\', \')||\'없음\'}`+(state.complete?\' · 완료\':state.errors.length?\' · 일부 미완료\':\' · 진행 중\');\n $(\'csv\').disabled=!state.results.length&&!state.errors.length;\n}\n$(\'pause\').onclick=()=>{paused=true;$(\'status\').textContent=\'현재 요청 완료 후 정지\';};\n$(\'run\').onclick=async()=>{if(busy)return;try{const c=config();paused=false;setBusy(true);$(\'status\').textContent=\'동시상장 코인 조회 중…\';\n const uni=await fetch(\'/api/v2core53/universe\').then(x=>x.json());if(!uni.ok)throw Error(uni.error||\'동시 상장 목록 오류\');\n let coins=uni.items,missing=[];\n if(c.scope!==\'all\'){const names=(c.scope===\'examples\'?[\'MMT\',\'PIEVERSE\']:$(\'custom\').value.toUpperCase().split(/[\\s,;]+/)).filter(Boolean);\n  coins=coins.filter(x=>names.includes(x.market.replace(\'KRW-\',\'\')));missing=names.filter(n=>!coins.some(x=>x.market===\'KRW-\'+n));\n  if(!coins.length)throw Error(\'선택 코인의 바이낸스 현물 USDT·빗썸 KRW 동시 상장 거래쌍이 없어\');}\n state={...c,results:[],errors:[],missing,done:0,complete:false,tasks:c.days.flatMap(date=>coins.map(x=>({date,...x})))};render();let cursor=0;\n async function worker(){while(!paused&&cursor<state.tasks.length){const task=state.tasks[cursor++];try{const r=await call(\'/api/v2core53/probe\',{market:task.market,symbol:task.binance_symbol,date:task.date,fee:c.fee,slip:c.slip,hold:c.hold});state.results.push(r.report);}catch(e){state.errors.push({...task,error:String(e.message||e)});}state.done++;render();if(!paused&&cursor<state.tasks.length)await new Promise(resolve=>setTimeout(resolve,900));}}\n await Promise.all(Array.from({length:Math.min(1,state.tasks.length)},()=>worker()));state.complete=!paused&&state.done===state.tasks.length&&state.errors.length===0;render();\n }catch(e){$(\'status\').textContent=\'⚠️ \'+String(e.message||e);}finally{setBusy(false);if(state)render();}};\nfunction csvCell(v){let x=v===undefined||v===null?\'\':typeof v===\'object\'?JSON.stringify(v):String(v);if(/^[=+@-]/.test(x)&&!Number.isFinite(Number(x)))x="\'"+x;return \'"\'+x.replaceAll(\'"\',\'""\')+\'"\';}\n$(\'csv\').onclick=()=>{if(!state)return;const a=selectTrades(\'A\'),b=selectTrades(\'B\'),bf=selectTrades(\'B_FIXED\'),bs=selectTrades(\'B_STRUCT\'),b2=selectTrades(\'B2_FIXED\'),b2s=selectTrades(\'B2_STRUCT\');\n const rows=[...state.results.flatMap(x=>[\n  {row_type:\'MARKET_COVERAGE\',date:x.date,market:x.market,...x.coverage},\n  ...(x.events||[]).map(e=>({row_type:\'BINANCE_RAW_SPIKE\',...e})),\n  ...(x.momentum_retests||[]).map(e=>({row_type:\'B_RETEST_RESEARCH\',...e})),\n  ...(x.momentum_variants||[]).map(e=>({row_type:\'B_VARIANT_RESEARCH\',...e}))]),\n  ...a.selected.map(e=>({row_type:\'SELECTED_A_PAPER_TRADE\',...e})),\n  ...b.selected.map(e=>({row_type:\'SELECTED_B_PAPER_TRADE\',...e})),\n  ...bf.selected.map(e=>({row_type:\'SELECTED_B_FIXED_PAPER_TRADE\',...e})),\n  ...bs.selected.map(e=>({row_type:\'SELECTED_B_STRUCT_PAPER_TRADE\',...e})),\n  ...b2.selected.map(e=>({row_type:\'SELECTED_B2_FIXED_PAPER_TRADE\',...e})),\n  ...b2s.selected.map(e=>({row_type:\'SELECTED_B2_STRUCT_PAPER_TRADE\',...e})),\n  ...a.days.map(x=>({row_type:\'DAILY_A_RISK_SUMMARY\',...x})),\n  ...b.days.map(x=>({row_type:\'DAILY_B_RISK_SUMMARY\',...x})),\n  ...[...bf.days,...bs.days,...b2.days,...b2s.days].map(x=>({row_type:\'DAILY_VARIANT_RISK_SUMMARY\',...x})),\n  ...(state.missing||[]).map(market=>({row_type:\'NOT_IN_SPOT_UNIVERSE\',market:\'KRW-\'+market})),\n  ...state.errors.map(e=>({row_type:\'API_ERROR\',...e}))];\n rows.push({row_type:\'SCAN_STATUS\',version:\'CORE.56\',paired_b2_structural_stops:true,full_day_detection:true,original_binance_signals_preserved:true,forward_only_b_entry:true,bithumb_gaps_not_backfilled:true,\n  completed:state.complete,scan_done:state.done,scan_total:state.tasks.length,fee:state.fee,slip:state.slip,hold:state.hold,scope:state.scope,\n  b_initial_spike_quiet_min:30,b_post_window_min:20,b_pullback_min_pct:.8,b_pullback_max_pct:3.5,b_min_rebound_pct:.6,\n  a_selected:a.selected.length,b_selected:b.selected.length,\n  b_struct_selected:bs.selected.length,b2_fixed_selected:b2.selected.length,b2_struct_selected:b2s.selected.length});\n const cols=[...new Set([\'row_type\',\'date\',\'market\',\'signal_at\',\'strategy\',\'stage\',\'status\',\'entry_at\',\'net_pct\',...rows.flatMap(x=>Object.keys(x))])];\n const contents=\'\\ufeff\'+[cols.join(\',\'),...rows.map(x=>cols.map(k=>csvCell(x[k])).join(\',\'))].join(\'\\r\\n\');\n const url=URL.createObjectURL(new Blob([contents],{type:\'text/csv;charset=utf-8\'}));const el=document.createElement(\'a\');el.href=url;el.download=`CORE56_BINANCE_B2_STRUCT_RETEST_3_5_10_30_${state.days[0]}_${state.days.at(-1)}_${state.scope}_KRW.csv`;\n document.body.appendChild(el);el.click();el.remove();setTimeout(()=>URL.revokeObjectURL(url),4000);\n};\n</script>\n<section class="panel" id="core56-panel">\n<h2>⑧ CORE.56 · 독립 눌림목 / 추세추종 연구</h2>\n<p class="muted">새로 받은 PULLBACK.1 전략을 기존 A/B/B2와 <b>분리된 빗썸 전용 비교군</b>으로 테스트해. 기존 CORE.55 결과와 청산 규칙은 변경하지 않아. 선택한 코인의 완료된 봉만 사용하며 미래 가격을 진입 조건에 사용하지 않아.</p>\n<div class="grid">\n<div class="field"><label for="pb56mode">연구 방식</label><select id="pb56mode"><option value="pullback">박스 돌파→눌림 재확인</option><option value="trend">추세추종 돌파 + ATR 트레일링</option></select></div>\n<div class="field"><label for="pb56tf">분봉</label><select id="pb56tf"><option value="5">5분 (박스24 / EMA240)</option><option value="15" selected>15분 (박스16 / EMA96)</option><option value="60">60분 (박스12 / EMA48)</option></select></div>\n<div class="field"><label for="pb56n">현재 거래대금 상위 코인</label><select id="pb56n"><option value="10">10개</option><option value="20" selected>20개</option><option value="40">40개</option></select></div>\n<div class="field"><label for="pb56of">캔들 기반 오더플로우 비교</label><select id="pb56of"><option value="strong">필터 없음 vs 강함</option><option value="weak">필터 없음 vs 약함</option><option value="off">필터 없음만</option></select></div>\n</div>\n<div class="buttons"><button id="pb56run">CORE.56 신규 전략 백테스트</button><button id="pb56pause" disabled class="alt">중지 요청</button><button id="pb56csv" disabled class="alt">CORE.56 CSV 저장</button></div>\n<progress id="pb56bar" value="0" max="1"></progress><p class="muted" id="pb56status">신규 전략 검사 전 · 위 ②의 마지막 완료 날짜, 날짜 수, 수수료, 슬리피지 설정을 공유해.</p>\n<div class="tablewrap"><table><thead><tr><th>전략</th><th>원 후보</th><th>선정 (하루 최대 4회)</th><th>승 / 패</th><th>미확정</th><th>평균 순손익</th><th>순손익 단순합</th></tr></thead><tbody id="pb56summary"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<h3>후보별 검사 기록</h3><div class="tablewrap"><table><thead><tr><th>전략 / 코인</th><th>진입 시각</th><th>진입가</th><th>손절</th><th>1차 익절</th><th>상태</th><th>비용 반영 순손익</th></tr></thead><tbody id="pb56trades"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<p class="muted">주의: 현재 거래대금 상위 코인으로 과거를 검사하므로 생존·선정 편향이 있어. 추세추종은 60분 최대 24봉, 4시간 최대 18봉까지만 관찰하고 끝에서 미완료면 UNKNOWN 처리해. 기존 CORE.55 A/B/B2와 <b>검색 대상과 청산 방식이 다르므로</b> 같은 숫자만 보고 우열을 단정할 수 없어. 캔들 델타는 실제 체결 오더플로우가 아니야. 슬리피지와 실제 체결은 다를 수 있어.</p>\n</section>\n<script>\n(()=>{\nconst el=id=>document.getElementById(id), fmt=x=>x==null?\'—\':Number(x).toFixed(3), esc=x=>String(x??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nconst result={trades:[],errors:[],done:0,total:0,complete:false};let active=false,stopping=false;\nconst mode=()=>el(\'pb56mode\').value;\nfunction updateTF(){const trend=mode()===\'trend\',s=el(\'pb56tf\');s.innerHTML=trend?\'<option value="60">60분 (고점48 / EMA200)</option><option value="240">4시간 (고점20 / EMA50)</option>\':\'<option value="5">5분 (박스24 / EMA240)</option><option value="15" selected>15분 (박스16 / EMA96)</option><option value="60">60분 (박스12 / EMA48)</option>\';el(\'pb56of\').disabled=trend;}\nel(\'pb56mode\').onchange=updateTF;\nfunction picks(variant){const candidates=result.trades.filter(t=>t.filter?.[variant]&&t.time).sort((a,b)=>a.time.localeCompare(b.time)||a.market.localeCompare(b.market));\n const out=[],perDay={};let busy=\'\';for(const t of candidates){const d=t.time.slice(0,10),v=perDay[d]||(perDay[d]={count:0,streak:0});if(v.count>=4||v.streak>=3||busy&&t.time<busy)continue;\n out.push(t);v.count++;if(t.status!==\'READY\'){busy=\'9999\';break;}v.streak=t.net_pct<=0?v.streak+1:0;busy=t.exit_at||\'9999\';}\n return {candidates,out};}\nfunction paint(){el(\'pb56bar\').max=Math.max(1,result.total);el(\'pb56bar\').value=result.done;\n const variants=mode()===\'trend\'?[\'off\']:[\'off\',...(el(\'pb56of\').value===\'off\'?[]:[el(\'pb56of\').value])];\n el(\'pb56summary\').innerHTML=variants.map(v=>{const {candidates,out}=picks(v),ready=out.filter(x=>x.status===\'READY\'),wins=ready.filter(x=>x.net_pct>0).length,unknown=out.length-ready.length,total=ready.reduce((s,x)=>s+x.net_pct,0),avg=ready.length?total/ready.length:null;\n return `<tr><td>${esc(mode()===\'trend\'?\'추세추종\':v===\'off\'?\'눌림 · 필터 없음\':\'눌림 · 필터 \'+(v===\'strong\'?\'강함\':\'약함\'))}</td><td>${candidates.length}</td><td>${out.length}</td><td>${wins}/${ready.length-wins}</td><td>${unknown}</td><td>${fmt(avg)}%</td><td>${fmt(total)}%</td></tr>`;}).join(\'\');\n el(\'pb56trades\').innerHTML=result.trades.slice(0,300).map(t=>`<tr><td>${esc(t.strategy+\' / \'+t.market)}</td><td>${esc(t.time?.slice(0,16))}</td><td>${fmt(t.price)}</td><td>${fmt(t.stop)}</td><td>${t.tp1?fmt(t.tp1):\'트레일링\'}</td><td>${esc(t.reason||t.status)}</td><td>${t.net_pct==null?\'UNKNOWN\':fmt(t.net_pct)+\'%\'}</td></tr>`).join(\'\')||\'<tr><td colspan="7">후보 없음</td></tr>\';\n el(\'pb56status\').textContent=`${result.done}/${result.total} 완료 · 원 후보 ${result.trades.length}건 · 오류 ${result.errors.length}건`+(result.complete?\' · 전체 완료\':\' · 미완료/진행 중\');el(\'pb56csv\').disabled=!result.trades.length&&!result.errors.length;}\nfunction dates(last,n){const x=new Date(last+\'T00:00:00Z\');return Array.from({length:n},(_,i)=>new Date(x.getTime()-(n-1-i)*86400000).toISOString().slice(0,10));}\nel(\'pb56pause\').onclick=()=>{stopping=true;el(\'pb56status\').textContent=\'진행 중 요청 완료 후 중지\';};\nel(\'pb56run\').onclick=async()=>{if(active)return;active=true;stopping=false;el(\'pb56run\').disabled=true;el(\'pb56pause\').disabled=false;\n try{const d=el(\'day\').value,ds=Number(el(\'days\').value),tf=Number(el(\'pb56tf\').value),st=mode(),of=el(\'pb56of\').value,fee=Number(el(\'fee\').value),slip=Number(el(\'slip\').value);if(!d||!ds)throw Error(\'위 ②에서 마지막 완료 날짜를 선택해\');\n el(\'pb56status\').textContent=\'빗썸 KRW 상위 종목 확인 중…\';const uni=await fetch(\'/api/v2core/universe\').then(r=>r.json());if(!uni.ok)throw Error(uni.error||\'KRW 종목 조회 실패\');\n const markets=uni.items.slice(0,Number(el(\'pb56n\').value));const task=dates(d,ds).flatMap(date=>markets.map(market=>({date,market})));result.trades=[];result.errors=[];result.done=0;result.total=task.length;result.complete=false;paint();\n if(task.length>120)el(\'pb56status\').textContent=\'검사량이 많아. 필요하면 1일·10개부터 확인해.\';\n let cursor=0;async function work(){while(!stopping&&cursor<task.length){const t=task[cursor++];try{const res=await fetch(\'/api/v2core56/pb-probe\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({...t,tf,mode:st,fee,slip,of})}).then(r=>r.json());if(!res.ok)throw Error(res.error||\'검사 실패\');result.trades.push(...res.report.trades);}catch(e){result.errors.push({...t,error:String(e.message||e)});}result.done++;paint();}}\n await Promise.all(Array.from({length:Math.min(2,task.length)},()=>work()));result.complete=!stopping&&result.done===task.length&&!result.errors.length;paint();\n }catch(e){el(\'pb56status\').textContent=\'⚠️ \'+String(e.message||e);}finally{active=false;el(\'pb56run\').disabled=false;el(\'pb56pause\').disabled=true;}}\nfunction csvCell(x){const s=String(x??\'\');return \'"\'+(/^[=+@-]/.test(s)&&!Number.isFinite(Number(s))?"\'"+s:s).replaceAll(\'"\',\'""\')+\'"\';}\nel(\'pb56csv\').onclick=()=>{const rows=[...result.trades.map(x=>({row_type:\'PULLBACK_TREND_RESEARCH\',...x,filter:JSON.stringify(x.filter)})),...result.errors.map(x=>({row_type:\'API_ERROR\',...x})),{row_type:\'SCAN_STATUS\',version:\'CORE.56\',complete:result.complete,done:result.done,total:result.total}];const cols=[...new Set([\'row_type\',\'market\',\'strategy\',\'time\',\'signal_at\',\'price\',\'stop\',\'tp1\',\'status\',\'reason\',\'net_pct\',...rows.flatMap(x=>Object.keys(x))])];\n const text=\'\\ufeff\'+[cols.join(\',\'),...rows.map(x=>cols.map(k=>csvCell(x[k])).join(\',\'))].join(\'\\r\\n\');const url=URL.createObjectURL(new Blob([text],{type:\'text/csv;charset=utf-8\'}));const a=document.createElement(\'a\');a.href=url;a.download=`CORE56_PULLBACK_TREND_${el(\'day\').value}_${mode()}_${el(\'pb56tf\').value}m.csv`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000);};\n})();\n</script>\n</main></body></html>\n'

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

# CORE.56 — Bithumb-only independent pullback / trend research.
# Standalone research extension; existing CORE.55 engines remain unchanged.
CORE56_PB_PRESETS = {5: (24, 240, 8, 48), 15: (16, 96, 8, 48), 60: (12, 48, 6, 24)}
CORE56_TREND_PRESETS = {60: (48, 200), 240: (20, 50)}
CORE56_MAX_PAGES = 24


def _core56_fetch(api_get, market, day, kst, tf, warmup, after):
    span = timedelta(minutes=tf)
    start = datetime.combine(day, dt_time.min, tzinfo=kst) - span * warmup
    end = datetime.combine(day + timedelta(days=1), dt_time.min, tzinfo=kst) + span * after
    cursor = end
    seen = {}
    pages = 0
    reached = False
    reason = 'PAGE_CAP'
    bad = 0
    for _ in range(CORE56_MAX_PAGES):
        raw = api_get(f'/v1/candles/minutes/{tf}', {
            'market': market, 'count': 200, 'to': cursor.strftime(KST_FORMAT)})
        if not isinstance(raw, list):
            raise ValueError('빗썸 분봉 응답이 배열이 아닙니다')
        pages += 1
        if not raw:
            reason = 'NO_OLDER_BARS'
            break
        parsed = []
        for item in raw:
            try:
                bar = _parse_candle(item, kst)
                parsed.append(bar)
                if start <= bar['t'] < end:
                    seen[bar['t']] = bar
            except (KeyError, ValueError, TypeError, OverflowError):
                bad += 1
        if not parsed:
            reason = 'ALL_INVALID'
            break
        oldest = min(z['t'] for z in parsed)
        if oldest <= start:
            reached = True
            reason = 'START_REACHED'
            break
        if oldest >= cursor:
            reason = 'CURSOR_STALLED'
            break
        cursor = oldest
        # Short pages in sparse markets are not proof of full history.
    rows = [seen[t] for t in sorted(seen)]
    return rows, {'pages':pages, 'bars':len(rows), 'invalid_bars':bad,
                  'history_reached':reached, 'history_status':reason,
                  'first_bar':rows[0]['t'].isoformat() if rows else '',
                  'last_bar':rows[-1]['t'].isoformat() if rows else ''}


def _core56_contiguous(rows, end, size, span):
    if end - size + 1 < 0:
        return False
    return all(rows[j]['t'] - rows[j-1]['t'] == span
               for j in range(end-size+2, end+1))


def _core56_factors(rows, tf, ema_len):
    span = timedelta(minutes=tf)
    emas = []
    atrs = []
    avg_vol = []
    alpha = 2.0/(ema_len+1)
    ema = None
    for i, row in enumerate(rows):
        ema = row['c'] if ema is None else alpha*row['c']+(1-alpha)*ema
        emas.append(ema)
        if i >= 13 and _core56_contiguous(rows,i,14,span):
            tr=[]
            for j in range(i-13,i+1):
                prev = rows[j-1]['c'] if j else row['o']
                tr.append(max(rows[j]['h']-rows[j]['l'],abs(rows[j]['h']-prev),abs(rows[j]['l']-prev)))
            atrs.append(sum(tr)/14)
        else:
            atrs.append(None)
        if i >= 20 and _core56_contiguous(rows,i,21,span):
            avg_vol.append(sum(rows[j]['v'] for j in range(i-20,i))/20)
        else:
            avg_vol.append(None)
    return emas,atrs,avg_vol


def _core56_after_cost(entry, exit_price, fee, slip):
    side=(fee+slip)/100
    if entry<=0 or side>=1:
        raise ValueError('잘못된 진입가 또는 비용')
    return 100*(exit_price*(1-side)/(entry*(1+side))-1)


def _core56_exit_pullback(rows, ei, tf, entry, stop, tp1, fee, slip, time_stop, max_hold):
    span=timedelta(minutes=tf)
    half=False;first=None;prev=None
    for j in range(ei,ei+max_hold):
        if j>=len(rows) or (j>ei and rows[j]['t']-rows[j-1]['t']!=span):
            return {'status':'UNKNOWN','reason':'FUTURE_CANDLE_MISSING'}
        bar=rows[j]
        if not half:
            if bar['o']<=stop or bar['l']<=stop:
                px=min(stop,bar['o'])
                return {'status':'READY','reason':'SL','exit_at':(bar['t']+span).isoformat(),
                        'net_pct':round(_core56_after_cost(entry,px,fee,slip),5)}
            if bar['h']>=tp1:
                first=0.5*_core56_after_cost(entry,max(tp1,bar['o']),fee,slip)
                half=True
                prev=bar['l']
                continue
            if j-ei+1>=time_stop:
                return {'status':'READY','reason':'TIME_STOP','exit_at':(bar['t']+span).isoformat(),
                        'net_pct':round(_core56_after_cost(entry,bar['c'],fee,slip),5)}
        else:
            if bar['o']<prev or bar['l']<prev:
                px=min(prev,bar['o'])
                return {'status':'READY','reason':'TRAIL','exit_at':(bar['t']+span).isoformat(),
                        'net_pct':round(first+0.5*_core56_after_cost(entry,px,fee,slip),5)}
            prev=bar['l']
    bar=rows[ei+max_hold-1]
    net=(first+0.5*_core56_after_cost(entry,bar['c'],fee,slip)) if half else _core56_after_cost(entry,bar['c'],fee,slip)
    return {'status':'READY','reason':'HOLD_END','exit_at':(bar['t']+span).isoformat(),
            'net_pct':round(net,5)}


def _core56_exit_trend(rows, ei, tf, entry, stop, atrs, fee, slip, hold_bars):
    span=timedelta(minutes=tf)
    peak=entry
    initial=stop
    for j in range(ei,ei+hold_bars):
        if j>=len(rows) or (j>ei and rows[j]['t']-rows[j-1]['t']!=span):
            return {'status':'UNKNOWN','reason':'FUTURE_CANDLE_MISSING'}
        bar=rows[j]
        if bar['o']<=stop or bar['l']<=stop:
            px=min(stop,bar['o'])
            return {'status':'READY','reason':'TRAIL' if stop>initial else 'SL',
                    'exit_at':(bar['t']+span).isoformat(),
                    'net_pct':round(_core56_after_cost(entry,px,fee,slip),5)}
        # Stop moves only AFTER this bar closes. No same-bar lookahead.
        peak=max(peak,bar['h'])
        if atrs[j] is not None:
            stop=max(stop,peak-3*atrs[j])
    bar=rows[ei+hold_bars-1]
    return {'status':'READY','reason':'HOLD_END','exit_at':(bar['t']+span).isoformat(),
            'net_pct':round(_core56_after_cost(entry,bar['c'],fee,slip),5)}


def _core56_flow_ok(rows, k, j, mode):
    if mode=='off':return True
    max_ratio, minimum = {'weak':(1.0,0.0),'strong':(0.7,0.3)}[mode]
    if rows[k]['v']<=0:return False
    mean=sum(rows[z]['v'] for z in range(k+1,j+1))/(j-k)
    if mean>max_ratio*rows[k]['v']:return False
    def delta(bar):
        size=bar['h']-bar['l']
        return ((2*bar['c']-bar['h']-bar['l'])/size if size>0 else 0)
    if delta(rows[j])<minimum:return False
    return sum(delta(rows[z])*rows[z]['v'] for z in range(k,j+1))>0


def _core56_research(rows, day, market, tf, mode, fee, slip, of='off'):
    if mode=='pullback':
        box_len, ema_len, time_stop,max_hold=CORE56_PB_PRESETS[tf]
        warm=max(box_len,ema_len,20,14)
    else:
        box_len,ema_len=CORE56_TREND_PRESETS[tf]
        max_hold=24 if tf==60 else 18   # bounded follow-up research; explicitly capped
        warm=max(box_len,ema_len,20,14)
    span=timedelta(minutes=tf)
    start=datetime.combine(day,dt_time.min,tzinfo=rows[0]['t'].tzinfo) if rows else None
    end=start+timedelta(days=1) if start else None
    if len(rows)<warm+30:return [], {'insufficient':True,'reason':'INSUFFICIENT_WARMUP'}
    emas,atrs,avgs=_core56_factors(rows,tf,ema_len)
    selected=[]
    next_allowed=0
    for k in range(max(warm+2,21),len(rows)-1):
        if k<next_allowed or rows[k]['t']+span<start-span*7 or rows[k]['t']>=end:
            continue
        if not _core56_contiguous(rows,k,max(ema_len+6,box_len+1,21),span):
            continue
        row=rows[k]
        atr=atrs[k];avg=avgs[k]
        if atr is None or atr<=0 or avg is None or avg<=0:
            continue
        if mode=='pullback':
            box=rows[k-box_len:k]
            bh=max(x['h'] for x in box)
            bl=min(x['l'] for x in box)
            if not 1.5 <= (bh-bl)/atr <=6.0:continue
            if sum(x['h']>=bh*.999 for x in box)<2:continue
            if row['c']<=bh or row['c']-bh>2*atr:continue
            if row['v']<2*avg:continue
            rng=row['h']-row['l']
            if rng<=0 or (row['c']-row['o'])/rng<.6:continue
            if row['c']<=emas[k]:continue
            for j in range(k+1,min(k+6,len(rows)-2)+1):
                if rows[j]['t']-rows[j-1]['t']!=span:break
                ret=rows[j]
                if ret['c']<bh*.998:break
                if ret['l']>bh*1.003 or ret['c']<=bh or ret['c']<=ret['o']:
                    continue
                ei=j+1
                if rows[ei]['t']-rows[j]['t']!=span:break
                if not start <= rows[ei]['t'] < end:break
                entry=rows[ei]['o']
                stop=min(ret['l'],bh)-.5*atr
                risk=(entry-stop)/entry
                tp=entry+1.5*(entry-stop)
                if not (stop>0 and 0<risk<=.04 and tp/entry-1>=.01):break
                flow={m:_core56_flow_ok(rows,k,j,m) for m in ('off','weak','strong')}
                ex=_core56_exit_pullback(rows,ei,tf,entry,stop,tp,fee,slip,time_stop,max_hold)
                out={'market':market,'strategy':'PB'+str(tf),'time':rows[ei]['t'].isoformat(),
                    'signal_at':(rows[j]['t']+span).isoformat(),
                    'price':round(entry,10),'stop':round(stop,10),'tp1':round(tp,10),
                    'risk_pct':round(risk*100,4),'filter':flow,**ex}
                selected.append(out)
                next_allowed=ei+1
                break
        else:
            hh=max(x['h'] for x in rows[k-box_len:k])
            if row['c']<=hh or row['v']<1.5*avg:continue
            if row['c']<=emas[k] or emas[k]<=emas[k-5]:continue
            ei=k+1
            if rows[ei]['t']-rows[k]['t']!=span or not start<=rows[ei]['t']<end:continue
            entry=rows[ei]['o']
            stop=entry-3*atr
            risk=(entry-stop)/entry
            if not (stop>0 and 0<risk<=.15):continue
            ex=_core56_exit_trend(rows,ei,tf,entry,stop,atrs,fee,slip,max_hold)
            selected.append({'market':market,'strategy':'TREND'+str(tf),'time':rows[ei]['t'].isoformat(),
                             'signal_at':(row['t']+span).isoformat(),
                             'price':round(entry,10),'stop':round(stop,10),'tp1':None,
                             'risk_pct':round(risk*100,4),'filter':{'off':True,'weak':True,'strong':True},**ex})
            next_allowed=ei+1
    return selected, {'insufficient':False}


def register_daytrade_review(app,deps):
    """Only research routes; existing / and live app.py behavior stay unchanged."""
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache={'at':0.,'sorted':[],'markets':[],'results':{}}
    lock=Lock()
    core56_cache = {}

    @app.post('/api/v2core56/pb-probe')
    def core56_pb_probe():
        try:
            raw=request.get_json(silent=True)
            cfg=_check_cfg(raw,kst)
            market=raw.get('market')
            st=raw.get('mode','pullback')
            tf=int(raw.get('tf',15))
            of=raw.get('of','off')
            if not isinstance(market,str) or not MARKET_PATTERN.fullmatch(market):
                raise ValueError('KRW 마켓 형식 오류')
            if market not in set(get_markets()):
                raise ValueError('현재 빗썸 KRW 마켓에 존재하지 않습니다')
            if st not in ('pullback','trend'):
                raise ValueError('지원하지 않는 전략')
            if st=='pullback' and tf not in CORE56_PB_PRESETS:
                raise ValueError('눌림목 지원 분봉: 5, 15, 60')
            if st=='trend' and tf not in CORE56_TREND_PRESETS:
                raise ValueError('추세추종 지원 분봉: 60, 240')
            if of not in ('off','weak','strong'):
                raise ValueError('필터 방식 오류')
            key=('CORE56_PB',cfg['day'],market,st,tf,cfg['fee'],cfg['slip'])
            with lock:
                saved=core56_cache.get(key)
            if saved and time.monotonic()-saved[0]<1800:
                return jsonify(ok=True,version=VERSION,cached=True,report=saved[1])
            if st=='pullback':
                box,ema,time_stop,max_hold=CORE56_PB_PRESETS[tf]
            else:
                box,ema=CORE56_TREND_PRESETS[tf]
                max_hold=24 if tf==60 else 18
            bars,meta=_core56_fetch(api_get,market,cfg['day'],kst,tf,
                                    warmup=max(box,ema)+60,after=max_hold+1)
            # The API may return the currently FORMING candle; never use it.
            now=datetime.now(kst)
            span=timedelta(minutes=tf)
            bars=[b for b in bars if b['t']+span<=now]
            trades,diagnostic=_core56_research(bars,cfg['day'],market,tf,st,cfg['fee'],cfg['slip'],of)
            report={'market':market,'date':cfg['day'].isoformat(),'mode':st,'tf':tf,
                    'fee':cfg['fee'],'slip':cfg['slip'],'trades':trades,
                    'coverage':meta,'diagnostic':diagnostic,
                    'note':'기존 CORE.55 엔진과 독립. 수익률/체결 검증 전 연구용'}
            with lock:
                if len(core56_cache)>700:core56_cache.clear()
                core56_cache[key]=(time.monotonic(),report)
            return jsonify(ok=True,version=VERSION,cached=False,report=report)
        except Exception as exc:
            return jsonify(ok=False,error=f'{type(exc).__name__}: {exc}'[:200]),400

    core53_universe_cache={'at':0.,'items':[]}

    @app.get('/api/v2core53/universe')
    def core53_universe_route():
        try:
            with lock:
                cached=core53_universe_cache['items'] if core53_universe_cache['items'] and time.monotonic()-core53_universe_cache['at']<3600 else None
            if cached is not None:
                return jsonify(ok=True,version=VERSION,items=cached,cached=True)
            markets=set(get_markets())
            items=_core53_universe(markets)
            if not items:raise ValueError('현재 공통 상장 바이낸스 현물 거래쌍을 확인할 수 없습니다')
            with lock:
                core53_universe_cache.update(at=time.monotonic(),items=items)
            return jsonify(ok=True,version=VERSION,items=items,cached=False,
                           note='현재 동시상장 목록: 과거 상장 폐지/등록 시점의 생존편향 존재')
        except Exception as exc:
            return jsonify(ok=False,error=f'{type(exc).__name__}: {exc}'[:190]),502

    @app.post('/api/v2core53/probe')
    def core53_probe_route():
        try:
            raw=request.get_json(silent=True)
            cfg=_check_cfg(raw,kst)
            market=raw.get('market');symbol=raw.get('symbol');hold=int(raw.get('hold',240))
            if not isinstance(market,str) or not MARKET_PATTERN.fullmatch(market):raise ValueError('마켓 코드 오류')
            if not isinstance(symbol,str) or not re.fullmatch(r'[A-Z0-9]{2,25}USDT',symbol):raise ValueError('바이낸스 심볼 오류')
            if hold not in (120,240,480):raise ValueError('보유시간 설정 오류')
            with lock:
                pairs=core53_universe_cache['items'] if core53_universe_cache['items'] and time.monotonic()-core53_universe_cache['at']<3600 else None
            if pairs is None:
                pairs=_core53_universe(set(get_markets()))
                with lock:core53_universe_cache.update(at=time.monotonic(),items=pairs)
            if not any(x['market']==market and x['binance_symbol']==symbol for x in pairs):
                raise ValueError('현재 동시상장 바이낸스 현물/빗썸 마켓에 없는 심볼')
            key=('CORE55_B2_STRUCT_RETEST',cfg['day'],market,symbol,cfg['fee'],cfg['slip'],hold)
            with lock:cached=cache['results'].get(key)
            if cached is not None and time.monotonic()-cached[0]<1800:
                return jsonify(ok=True,version=VERSION,cached=True,report=cached[1])
            report=_core53_analyze_market(api_get,market,symbol,cfg['day'],kst,
                                          cfg['fee'],cfg['slip'],hold)
            with lock:
                if len(cache['results'])>1500:cache['results'].clear()
                cache['results'][key]=(time.monotonic(),report)
            return jsonify(ok=True,version=VERSION,cached=False,report=report)
        except Exception as exc:
            return jsonify(ok=False,error=f'{type(exc).__name__}: {exc}'[:200]),400

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
