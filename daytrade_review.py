"""V2 CORE.38 LIGHT — BB20 squeeze and early breakout research, split 30/30/40 + BE.

Standalone replacement for daytrade_review.py; integrates with the existing
Flask app via register_daytrade_review(app, deps).  Does not change app.py.
Research only: no order placement or automated execution.
"""
from datetime import date, datetime, time as dt_time, timedelta
from math import isfinite, sqrt
from threading import Lock
import re
import time
from flask import Response, jsonify, request

VERSION = 'V2.CORE.38-LIGHT-BB-SQUEEZE-BREAKOUT'
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
    """Fetch 4h prehistory + full day + 2h after, exclusive KST page cursor."""
    start = datetime.combine(day, dt_time.min, tzinfo=kst) - timedelta(hours=4)
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



def _sma(bars, i, n):
    if not _contiguous(bars, i, n):
        return None
    return sum(b['c'] for b in bars[i-n+1:i+1])/n


def _bb(bars, i, n=20, dev=2):
    if not _contiguous(bars, i, n):
        return None
    closes = [b['c'] for b in bars[i-n+1:i+1]]
    mid = sum(closes)/n
    sigma = sqrt(sum((x-mid)**2 for x in closes)/n)
    upper, lower = mid + dev*sigma, mid-dev*sigma
    return {'mid':mid, 'upper':upper, 'lower':lower, 'width_pct':(upper-lower)/mid*100}


def _empty_coverage(market, day, bars):
    return {'market':market,'date':day.isoformat(),'bar_count':len(bars),
            'contiguous_scan_bars':0,'squeeze_starts':0,'breakouts':0,
            'confirmed':0,'breakdown':0,'expired':0,'insufficient_coverage':0,
            'reason':''}


def analyze_squeezes(bars, day, market, fee=.25, slip=0):
    """Historical, time-causal BB20(2sigma) squeeze -> box breakout.

    At time t, all squeeze features use candles already completed at t.
    Breakout is evaluated only after the squeeze exists and is frozen; its
    upper reference is the original setup's high, not any future high.
    A is next 5m OPEN after completed breakout bar.  B is next 5m OPEN
    after one additional completed hold-confirmation bar.
    """
    coverage = _empty_coverage(market, day, bars)
    result = {'date':day.isoformat(), 'market':market,'coverage':coverage,'events':[]}
    if not bars:
        coverage['reason']='분봉 없음'; return result
    tz=bars[0]['t'].tzinfo
    st=datetime.combine(day,dt_time.min,tzinfo=tz)
    en=st+timedelta(days=1)
    start=next((j for j,b in enumerate(bars) if b['t']>=st),len(bars))
    stop=next((j for j,b in enumerate(bars) if b['t']>=en),len(bars))
    if stop-start<20:
        coverage['reason']='당일 5분봉 부족';return result

    active = None
    events=[]
    day_cash=0.0
    next_start_allowed=start
    # need 2h prehistory (40 bars) to compare baseline BB width, no lookahead
    for i in range(start,stop):
        b=bars[i]
        if b['t'].time()>=dt_time(22,15): break
        day_cash += b['cash']
        if not _contiguous(bars,i,44):
            coverage['insufficient_coverage']+=1
            if active:
                active['status']='INCOMPLETE_GAP'
                active['incomplete_reason']='5분봉 누락'
                active=None
            continue
        coverage['contiguous_scan_bars']+=1
        bb=_bb(bars,i)
        bb_old=_bb(bars,i-24)
        ma5=_sma(bars,i,5)
        ma20_old=_sma(bars,i-4,20)
        if not bb or not bb_old or not ma5 or not ma20_old: continue

        if active:
            e=active
            age=i-e['_setup_i']
            hi=e['box_high_price']; lo=e['box_low_price']
            if b['c'] < lo*.997:
                e['status']='BREAKDOWN'
                e['end_at']=(b['t']+FIVE).isoformat()
                coverage['breakdown']+=1
                active=None
                next_start_allowed=i+8
                continue
            vbase = sum(x['v'] for x in bars[i-12:i])/12
            vratio = b['v']/vbase if vbase else None
            cashbase=sum(x['cash'] for x in bars[i-12:i])/12
            cashratio=b['cash']/cashbase if cashbase else None
            threshold=hi*1.001
            top_of_candle=(b['c']-b['l'])/(b['h']-b['l']) if b['h']>b['l'] else 0
            body_ratio=abs(b['c']-b['o'])/(b['h']-b['l']) if b['h']>b['l'] else 0
            # Favor a real close above prior consolidation; not high-only wick.
            break_price=(b['c']>=threshold and b['c']>bb['mid'] and
                         b['c']>ma5 and b['c']>b['o'] and top_of_candle>=.65)
            if break_price:
                e.update(status='BREAKOUT',breakout_bar_at=b['t'].isoformat(),
                    breakout_signal_at=(b['t']+FIVE).isoformat(),
                    breakout_close_price=round(b['c'],10),
                    breakout_above_box_pct=round(_price_pct(b['c'],hi),4),
                    breakout_volume_ratio=round(vratio,4) if vratio is not None else None,
                    breakout_cash_ratio=round(cashratio,4) if cashratio is not None else None,
                    breakout_body_ratio=round(body_ratio,4),
                    breakout_close_location=round(top_of_candle,4),
                    breakout_bbwidth_pct=round(bb['width_pct'],4),
                    day_trade_krw_at_break=round(day_cash,2),
                    prior_60m_trade_krw=round(sum(x['cash'] for x in bars[i-11:i+1]),2))
                # Volume confirmation is a score feature, NOT a fixed eligibility requirement.
                score=0
                score+=25 if e['squeeze_bbwidth_pct']<=2.0 else 16
                score+=15 if e['squeeze_vs_2h_ratio']<=.70 else 8
                score+=20 if vratio is not None and vratio>=1.7 else (10 if vratio is not None and vratio>=1.1 else 0)
                score+=15 if e['box_width_pct']<=1.5 else (8 if e['box_width_pct']<=2.5 else 0)
                score+=15 if top_of_candle>=.85 else 8
                score+=10 if max(day_cash,sum(x['cash'] for x in bars[i-11:i+1]))>=5e8 else 0
                e['quality_score']=min(score,100)
                e['_break_i']=i
                # B: next COMPLETED 5m candle holds above original box high.
                k=i+1
                if (k<stop and k<len(bars) and bars[k]['t']-bars[i]['t']==FIVE):
                    confirm=bars[k]
                    if (confirm['c']>=hi and confirm['c']>=confirm['o']*.999 and
                        confirm['l']>=lo and confirm['c']>=_sma(bars,k,5)*.995):
                        e['confirm_signal_at']=(confirm['t']+FIVE).isoformat()
                        e['_confirm_i']=k
                        coverage['confirmed']+=1
                a_entry=i+1 if i+1<len(bars) and bars[i+1]['t']-bars[i]['t']==FIVE else None
                b_entry=(e.get('_confirm_i',-99)+1 if e.get('_confirm_i') is not None
                         and e['_confirm_i']+1<len(bars) and
                         bars[e['_confirm_i']+1]['t']-bars[e['_confirm_i']]['t']==FIVE else None)
                for method, ent in [('break',a_entry),('confirm',b_entry)]:
                    if method=='confirm' and e.get('_confirm_i') is None:
                        e['confirm_split_status']='NO_SIGNAL';e['confirm_full_status']='NO_SIGNAL'
                        continue
                    sr=_exit_split_be_sim(bars,ent,fee,slip)
                    fr=_exit_sim(bars,ent,fee,slip,.5)
                    for fld,val in sr.items():e[method+'_split_'+fld]=val
                    for fld,val in fr.items():e[method+'_full_'+fld]=val
                coverage['breakouts']+=1
                next_start_allowed=i+24  # avoid counting the same breakout multiple times
                active=None
                continue
            if age>=12:
                e['status']='EXPIRED_NO_BREAK'
                e['end_at']=(b['t']+FIVE).isoformat()
                coverage['expired']+=1
                active=None
                next_start_allowed=i+8
            continue

        if i<next_start_allowed:continue
        prev12=bars[i-11:i+1]
        boxhi=max(x['h'] for x in prev12)
        boxlo=min(x['l'] for x in prev12)
        width=(boxhi/boxlo-1)*100
        baseline_ratio=bb['width_pct']/bb_old['width_pct'] if bb_old['width_pct']>0 else None
        slope=abs(_price_pct(bb['mid'],ma20_old))
        # The adjustable thresholds are RESEARCH defaults, not established buy rules.
        squeeze=(width<=3.0 and slope is not None and slope<=.65 and
                 (bb['width_pct']<=1.8 or
                  (bb['width_pct']<=4.0 and baseline_ratio is not None and baseline_ratio<=.85)))
        if squeeze:
            e={'market':market,'date':day.isoformat(),'status':'SQUEEZE',
               'squeeze_signal_at':(b['t']+FIVE).isoformat(),
               'squeeze_bar_at':b['t'].isoformat(),
               'squeeze_bbwidth_pct':round(bb['width_pct'],4),
               'squeeze_vs_2h_ratio':round(baseline_ratio,4),
               'box_high_price':round(boxhi,10),
               'box_low_price':round(boxlo,10),
               'box_width_pct':round(width,4),
               'sma20_slope_20m_pct':round(_price_pct(bb['mid'],ma20_old),4),
               'sma5_sma20_gap_pct':round(_price_pct(ma5,bb['mid']),4),
               'day_trade_krw_at_squeeze':round(day_cash,2),
               '_setup_i':i}
            events.append(e);active=e;coverage['squeeze_starts']+=1
    if active:
        active['status']='END_OF_DAY'
    for e in events:
        e.pop('_setup_i',None);e.pop('_break_i',None);e.pop('_confirm_i',None)
    result['events']=events
    if coverage['contiguous_scan_bars']==0 and not coverage['reason']:
        coverage['reason']='연속 5분봉 부족(최소 44개)'
    return result


CORE_PAGE = '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n<title>V2 CORE.38 LIGHT · 볼밴 응축 초기 돌파 연구</title>\n<style>\n:root{color-scheme:dark;font-family:system-ui,-apple-system,\'Noto Sans KR\',sans-serif;background:#0d1324;color:#edf2ff}\n*{box-sizing:border-box}body{margin:0;background:#0d1324;color:#edf2ff}main{max-width:1120px;margin:auto;padding:18px 12px 54px}h1{font-size:clamp(21px,4vw,30px);margin:8px 0 6px}h2{font-size:19px;margin:0 0 16px}h3{font-size:15px;margin:16px 0 8px}p{line-height:1.7}.muted,small{color:#aebbd3}a{color:#9dc3ff}.panel{background:#171f33;border:1px solid #303e59;border-radius:18px;padding:18px;margin:16px 0}.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}label{font-size:13px;color:#b7c4df;display:block}input,select{margin-top:8px;width:100%;min-height:45px;padding:10px;color:#fff;background:#10182b;border:1px solid #405170;border-radius:10px;font-size:15px}button{padding:12px 16px;border-radius:12px;border:1px solid #43516a;background:#24304a;color:#fff;font-size:15px;cursor:pointer;font-weight:650}button.primary{background:#396ce3;border-color:#638cfa}button:disabled{opacity:.43;cursor:not-allowed}.buttons{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:10px}.kpi{border:1px solid #35445d;border-radius:12px;padding:13px;background:#121a2b}.kpi span{display:block;font-size:12px;color:#b3bfd6}.kpi strong{display:block;font-size:24px;margin:6px 0}.good{color:#5bdfa7}.bad{color:#ff8491}.warn{color:#ffc87d}.scroll{overflow:auto;max-width:100%}table{border-collapse:collapse;min-width:710px;width:100%;font-size:13px}td,th{padding:10px 8px;text-align:right;border-bottom:1px solid #374259;white-space:nowrap}td:first-child,th:first-child{text-align:left}th{color:#aabbdb;font-weight:600}progress{width:100%;margin-top:16px;height:12px}#status{font-size:13px;color:#c8d5f1;line-height:1.6;white-space:pre-line}.note{background:#0e192d;border-radius:12px;padding:13px;font-size:13px;color:#c2d0eb;line-height:1.7}.pill{border-radius:20px;background:#243a57;color:#b8d7ff;padding:4px 10px;font-size:12px;font-weight:bold}footer{font-size:12px;color:#9eacc4;line-height:1.7}\n</style></head><body><main>\n<header><h1>🧪 V2 CORE.38 LIGHT · 볼밴 응축 초기 돌파</h1><p class="muted">5분봉 BB20(2σ) · 횡보 박스 수축 → 첫 상승 돌파 → 다음 봉 유지 비교 · 연구용(자동매매 없음)</p><a href="/">← 기존 실전 V2 스캐너로 돌아가기</a></header>\n<section class="panel"><h2>① 검사 설정</h2><div class="fields">\n<label>마지막 검사 날짜 (KST · 완료된 하루)<input type="date" id="date"></label>\n<label>기간<select id="days"><option value="1">1일</option><option value="3">3일</option><option value="5">5일</option></select></label>\n<label>KRW 검사 범위<select id="scope"><option value="all">전체 KRW</option><option value="100">현재 거래대금 상위 100개</option><option value="40">현재 거래대금 상위 40개</option></select></label>\n<label>편도 수수료 (%)<input type="number" id="fee" min="0" max="0.5" step="0.01" value="0.25"></label>\n<label>편도 슬리피지 (%)<input type="number" id="slip" min="0" max="0.5" step="0.01" value="0"></label>\n</div><div class="buttons"><button class="primary" id="run">응축→돌파 연구 검사</button><button id="pause" disabled>일시 중지</button><button id="csv" disabled>CORE.38 통합 CSV 다운로드</button></div><progress id="prog" max="100" value="0"></progress><div id="status">검사 전 · 데이터 수집 버튼을 누르면 시작합니다.</div>\n<p class="note">현재 시점의 24시간 거래대금 순위는 과거 시장 순위가 아닙니다. 전체 KRW 검사를 권장하며, 과거 5분봉이 불연속이면 해당 종목은 억지로 보간하지 않습니다.</p></section>\n<section class="panel"><h2>② 후보 선정·청산 비교</h2><div class="fields">\n<label>진입 방식<select id="method"><option value="break">A · 첫 돌파 확정 후 다음 봉 진입</option><option value="confirm">B · 다음 완성봉 유지 확인 후 진입</option></select></label>\n<label>청산 방식<select id="target"><option value="split">30/30/40 + 수수료 포함 본절로스</option><option value="full">기존 +0.5% 순익 전량 익절</option></select></label>\n<label>최소 품질 점수<select id="score"><option value="0">0점 · 전체 돌파 비교</option><option value="50">50점 이상</option><option value="60" selected>60점 이상</option><option value="70">70점 이상</option><option value="80">80점 이상</option></select></label>\n<label>당일 누적 또는 최근 60분 거래대금<select id="cash"><option value="500000000">5억 원 이상</option><option value="1000000000">10억 원 이상</option><option value="0">제한 없이 연구</option></select></label>\n<label>일일 거래 상한<select id="cap"><option value="3">최대 3건</option><option value="4" selected>최대 4건</option></select></label>\n<label>선정 방식<select id="mode"><option value="slot" selected>6시간대별 최대 1건</option><option value="day">당일 시간순 최대 4건</option></select></label>\n</div><p class="note">신호가 마감된 다음 5분봉 시가로 가상 진입합니다. 후보는 미래 수익을 보고 고르지 않으며 같은 코인은 하루 1건만 선정합니다. 기존 CORE.37의 분할 목표(순익 +0.5/+1.5/+3.0%, 30/30/40), 1차 후 BE, 1차 전 -0.7% 손절, 60분 보유를 유지합니다.</p>\n<div class="kpis"><div class="kpi"><span>검사 완료</span><strong id="kdone">0/0</strong></div><div class="kpi"><span>응축 시작</span><strong id="ksqueeze">0</strong></div><div class="kpi"><span>상단 돌파</span><strong id="kbreak">0</strong></div><div class="kpi"><span>다음 봉 확인</span><strong id="kconfirm">0</strong></div><div class="kpi"><span>실제 선정</span><strong id="kselected">0</strong></div><div class="kpi"><span>선정 평균 순익</span><strong id="kpnl">—</strong></div></div>\n<h3>선정된 거래 · 실제로 알 수 있었던 시점 기준</h3><div class="scroll"><table><thead><tr><th>종목·날짜</th><th>신호 시각</th><th>볼밴 폭%</th><th>돌파 거래량</th><th>점수</th><th>1/2/3차</th><th>첫 5분 손절</th><th>청산 결과</th><th>순익%</th></tr></thead><tbody id="selectedRows"><tr><td colspan="9">검사 전</td></tr></tbody></table></div>\n<h3>날짜별 성과</h3><div class="scroll"><table><thead><tr><th>날짜</th><th>돌파 후보</th><th>선정</th><th>확정</th><th>수익</th><th>손실</th><th>첫 5분 손절</th><th>평균 순익%</th></tr></thead><tbody id="dayRows"></tbody></table></div></section>\n<section class="panel"><h2>③ 연구 검증 · 실패한 응축도 기록</h2><p class="muted">응축이 나타나도 돌파하지 못한 사례와, 돌파 후 바로 손절된 사례를 분리해서 분석합니다. 결과가 확인되지 않은 거래는 수익/손실로 계산하지 않습니다.</p>\n<div class="scroll"><table><thead><tr><th>집단</th><th>신호 수</th><th>청산 확정</th><th>수익</th><th>첫 5분 손절</th><th>평균 순익%</th></tr></thead><tbody id="compareRows"></tbody></table></div>\n<h3>응축 및 돌파 사례 (최근 80개 표시, CSV는 전체)</h3><div class="scroll"><table><thead><tr><th>종목·날짜</th><th>응축 시각</th><th>상태</th><th>응축 BB폭%</th><th>박스폭%</th><th>돌파 시각</th><th>점수</th><th>A 분할%</th><th>B 분할%</th></tr></thead><tbody id="eventRows"></tbody></table></div>\n</section><footer>V2 CORE.38 · 연구 목적으로만 사용하세요. 5분봉 OHLC는 봉 안의 가격 이동 순서를 보장하지 않으며, 수수료 외 미끄러짐·유동성·호가 차이로 실제 수익은 달라질 수 있습니다. 기존 실전 주문 실행과 연결되지 않습니다.</footer>\n</main><script>\n\'use strict\';\nconst V=\'V2.CORE.38-LIGHT-BB-SQUEEZE-BREAKOUT\';\nconst $=id=>document.getElementById(id);\nconst esc=x=>String(x??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nconst num=(v,d=3)=>v===null||v===undefined||v===\'\'||!Number.isFinite(Number(v))?\'—\':Number(v).toFixed(d);\nlet state=null, running=false,paused=false;\nfunction kstToday(){return new Intl.DateTimeFormat(\'sv-SE\',{timeZone:\'Asia/Seoul\',year:\'numeric\',month:\'2-digit\',day:\'2-digit\'}).format(new Date())}\nfunction addDays(iso,delta){const [y,m,d]=iso.split(\'-\').map(Number);return new Date(Date.UTC(y,m-1,d+delta)).toISOString().slice(0,10)}\n$(\'date\').value=addDays(kstToday(),-1);\nfunction cfg(){const end=$(\'date\').value, n=Number($(\'days\').value);if(!/^\\d{4}-\\d\\d-\\d\\d$/.test(end))throw Error(\'날짜를 선택해 줘\');if(end>=kstToday())throw Error(\'완료된 날짜만 조회할 수 있어\');return {days:Array.from({length:n},(_,i)=>addDays(end,i-n+1)),scope:$(\'scope\').value,fee:Number($(\'fee\').value),slip:Number($(\'slip\').value)};}\nfunction policy(){return {method:$(\'method\').value,target:$(\'target\').value,score:Number($(\'score\').value),cash:Number($(\'cash\').value),cap:Number($(\'cap\').value),mode:$(\'mode\').value}}\nconst allEvents=()=>state?state.results.flatMap(r=>r.events||[]):[];\nconst exitKey=p=>p.method+\'_\'+(p.target===\'split\'?\'split\':\'full\');\nfunction picks(){const p=policy(),ek=exitKey(p),ev=allEvents();const ds=state?.days||[];const chosen=[],byDay=[];for(const day of ds){const candidates=ev.filter(x=>x.date===day&&x.status===\'BREAKOUT\'&&x.quality_score>=p.score&&Math.max(x.day_trade_krw_at_break||0,x.prior_60m_trade_krw||0)>=p.cash&&x[p.method===\'break\'?\'breakout_signal_at\':\'confirm_signal_at\']);candidates.sort((a,b)=>String(a[p.method===\'break\'?\'breakout_signal_at\':\'confirm_signal_at\']).localeCompare(String(b[p.method===\'break\'?\'breakout_signal_at\':\'confirm_signal_at\']))||b.quality_score-a.quality_score||a.market.localeCompare(b.market));const selected=[],slots=new Set(),markets=new Set();for(const e of candidates){const at=e[p.method===\'break\'?\'breakout_signal_at\':\'confirm_signal_at\'];const slot=Math.floor(Number(at.slice(11,13))/6);if(selected.length>=p.cap)break;if(markets.has(e.market)||(p.mode===\'slot\'&&slots.has(slot)))continue;selected.push({...e,selected_at:at,_exit_key:ek});markets.add(e.market);slots.add(slot);}const rd=selected.filter(e=>e[ek+\'_status\']===\'READY\');const wins=rd.filter(e=>Number(e[ek+\'_net_pct\'])>0).length;const early=rd.filter(e=>e[ek+\'_exit\']?.startsWith(\'SL\')&&e[ek+\'_bars_held\']===1).length;byDay.push({date:day,candidates:candidates.length,selected:selected.length,ready:rd.length,wins,loss:rd.length-wins,early,avg:rd.length?rd.reduce((s,e)=>s+Number(e[ek+\'_net_pct\']),0)/rd.length:null});chosen.push(...selected);}return {selected:chosen,byDay};}\nfunction tr(body,html,cols){$(body).innerHTML=html||`<tr><td colspan="${cols}" style="text-align:left;color:#91a2bd">결과 없음</td></tr>`;}\nfunction render(){if(!state){return}const ev=allEvents(),p=policy(),pick=picks(),ek=exitKey(p),c=state.done;\n$(\'kdone\').textContent=`${c}/${state.tasks.length}`;$(\'prog\').value=state.tasks.length?c/state.tasks.length*100:0;\n$(\'ksqueeze\').textContent=ev.length;$(\'kbreak\').textContent=ev.filter(e=>e.status===\'BREAKOUT\').length;$(\'kconfirm\').textContent=ev.filter(e=>e.confirm_signal_at).length;$(\'kselected\').textContent=pick.selected.length;\nconst rd=pick.selected.filter(e=>e[ek+\'_status\']===\'READY\');const avg=rd.length?rd.reduce((s,e)=>s+Number(e[ek+\'_net_pct\']),0)/rd.length:null;$(\'kpnl\').textContent=num(avg)+\'%\';$(\'kpnl\').className=avg===null?\'\':avg>=0?\'good\':\'bad\';\ntr(\'selectedRows\',pick.selected.map(e=>{const v=e[ek+\'_net_pct\'],early=e[ek+\'_exit\']?.startsWith(\'SL\')&&e[ek+\'_bars_held\']===1;return `<tr><td>${esc(e.date)} ${esc(e.market)}</td><td>${esc(e.selected_at.slice(11,16))}</td><td>${num(e.squeeze_bbwidth_pct,2)}</td><td>${num(e.breakout_volume_ratio,2)}×</td><td>${e.quality_score}</td><td>${[1,2,3].map(n=>e[p.method+\'_split_tp\'+n+\'_hit\']?\'✓\':\'—\').join(\' / \')}</td><td class="${early?\'bad\':\'\'}">${early?\'YES\':\'—\'}</td><td>${esc(e[ek+\'_exit\']||e[ek+\'_status\']||\'—\')}</td><td class="${v===null||v===undefined?\'\':Number(v)>=0?\'good\':\'bad\'}">${num(v)}</td></tr>`}).join(\'\'),9);\ntr(\'dayRows\',pick.byDay.map(x=>`<tr><td>${esc(x.date)}</td><td>${x.candidates}</td><td>${x.selected}</td><td>${x.ready}</td><td class="good">${x.wins}</td><td class="bad">${x.loss}</td><td class="bad">${x.early}</td><td>${num(x.avg)}</td></tr>`).join(\'\'),8);\nconst compared=[\'break\',\'confirm\'].map(m=>{const k=m+\'_\'+(p.target===\'split\'?\'split\':\'full\'),li=ev.filter(e=>e[m===\'break\'?\'breakout_signal_at\':\'confirm_signal_at\']);const rd=li.filter(e=>e[k+\'_status\']===\'READY\');const wins=rd.filter(e=>Number(e[k+\'_net_pct\'])>0).length;const early=rd.filter(e=>e[k+\'_exit\']?.startsWith(\'SL\')&&e[k+\'_bars_held\']===1).length;const avg=rd.length?rd.reduce((s,e)=>s+Number(e[k+\'_net_pct\']),0)/rd.length:null;return `<tr><td>${m===\'break\'?\'A · 돌파 직후\':\'B · 다음 봉 유지\'}</td><td>${li.length}</td><td>${rd.length}</td><td>${wins}</td><td>${early}</td><td>${num(avg)}</td></tr>`});\ntr(\'compareRows\',compared.join(\'\'),6);\ntr(\'eventRows\',ev.slice(0,80).map(e=>`<tr><td>${esc(e.date)} ${esc(e.market)}</td><td>${esc((e.squeeze_signal_at||\'\').slice(11,16))}</td><td>${esc(e.status)}</td><td>${num(e.squeeze_bbwidth_pct,2)}</td><td>${num(e.box_width_pct,2)}</td><td>${esc((e.breakout_signal_at||\'\').slice(11,16)||\'—\')}</td><td>${e.quality_score??\'—\'}</td><td>${num(e.break_split_net_pct)}</td><td>${num(e.confirm_split_net_pct)}</td></tr>`).join(\'\'),9);\n}\nfunction status(s){$(\'status\').textContent=s}\nfunction setRunning(x){running=x;$(\'run\').disabled=x;$(\'pause\').disabled=!x;[\'date\',\'days\',\'scope\',\'fee\',\'slip\'].forEach(id=>$(id).disabled=x)}\nfunction api(url,obj){return fetch(url,{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(obj)}).then(async x=>{let j=await x.json();if(!x.ok||!j.ok)throw Error(j.error||`HTTP ${x.status}`);return j})}\n$(\'pause\').onclick=()=>{paused=true;status(\'일시 중지 요청 · 진행 중인 작업만 마치고 중단합니다\')};\n[\'method\',\'target\',\'score\',\'cash\',\'cap\',\'mode\'].forEach(id=>$(id).addEventListener(\'change\',render));\n$(\'run\').onclick=async()=>{if(running)return;try{const c=cfg(),key=JSON.stringify(c);if(!state||state.key!==key||state.cursor>=state.tasks.length){const r=await fetch(\'/api/v2core/universe\').then(x=>x.json());if(!r.ok||!Array.isArray(r.items))throw Error(r.error||\'KRW 종목 목록 조회 실패\');const markets=c.scope===\'all\'?r.items:r.items.slice(0,Number(c.scope));const tasks=c.days.flatMap(date=>markets.map(market=>({date,market})));state={key,...c,tasks,cursor:0,done:0,results:[],errors:[]};}paused=false;setRunning(true);render();$(\'csv\').disabled=false;let last=0;\nasync function worker(){while(!paused&&state.cursor<state.tasks.length){const task=state.tasks[state.cursor++];try{const r=await api(\'/api/v2core/squeeze38/analyze\',{...task,fee:state.fee,slip:state.slip});state.results.push(r.result);}catch(ex){state.errors.push({...task,error:String(ex.message||ex)})}finally{state.done++;if(state.done-last>=8||state.done===state.tasks.length){last=state.done;render();status(`${state.done}/${state.tasks.length} 검사 중 · API 오류 ${state.errors.length}`)}}}}\nawait Promise.all(Array.from({length:Math.min(4,state.tasks.length)},()=>worker()));render();status(paused?\'일시 중지됨 · 같은 설정으로 다시 검사하면 이어서 실행합니다\':`완료 ${state.done}/${state.tasks.length} · 응축 ${allEvents().length}건 · CSV 저장 가능`);}catch(ex){status(\'⚠️ \'+String(ex.message||ex))}finally{setRunning(false);$(\'csv\').disabled=!state||(!state.results.length&&!state.errors.length)}};\nfunction cv(x){let s=x!==null&&typeof x===\'object\'?JSON.stringify(x):String(x??\'\');if(/^[=+@-]/.test(s)&&!Number.isFinite(Number(s)))s="\'"+s;return \'"\'+s.replace(/"/g,\'""\')+\'"\'}\n$(\'csv\').onclick=()=>{if(!state)return;const p=policy(),picked=picks(),selected=new Map(picked.selected.map(x=>[`${x.date}|${x.market}|${x.squeeze_signal_at}`,x]));const base={version:V,date_from:state.days[0],date_to:state.days.at(-1),scope:state.scope,fee_pct_side:state.fee,slippage_pct_side:state.slip,scan_complete:state.done===state.tasks.length,scan_done:state.done,scan_tasks:state.tasks.length,scan_errors:state.errors.length,selection_method:p.method,exit_policy:p.target===\'split\'?\'SPLIT303040_BE\':\'FULL05\',score_min:p.score,cash_min_krw:p.cash,cash_basis:\'MAX(DAY_CUMULATIVE,PRIOR_60MIN)\',daily_cap:p.cap,selection_mode:p.mode,indicator:\'BB20_STD2_SMA5_SMA20\',squeeze_rule:\'BOX_12BAR<=3%,SMA20_SLOPE<=0.65%,AND(BBWIDTH<=1.8%_OR_BBWIDTH<=4%_AND_RATIO_TO_2H<=0.85)\',breakout_rule:\'CLOSE>=BOXHIGH*1.001,STRONG_CLOSE\',stop_gross_pct:.7,horizon_min:60,fill:\'NEXT_5M_OPEN_STOP_FIRST\'};const data=[];picked.byDay.forEach(d=>data.push({...base,row_type:\'DAY_SUMMARY\',date:d.date,...d}));state.results.forEach(r=>{data.push({...base,row_type:\'MARKET_COVERAGE\',date:r.date,market:r.market,...r.coverage});r.events.forEach(e=>{const sel=selected.get(`${e.date}|${e.market}|${e.squeeze_signal_at}`);data.push({...base,row_type:\'SQUEEZE_EVENT\',selected_for_daily_limit:!!sel,selected_entry_method:sel?p.method:\'\',selected_signal_at:sel?sel.selected_at:\'\',selected_exit_status:sel?sel[exitKey(p)+\'_status\']:\'\',selected_net_pct:sel?sel[exitKey(p)+\'_net_pct\']:null,...e})})});state.errors.forEach(e=>data.push({...base,row_type:\'API_ERROR\',...e}));const cols=[...new Set([\'row_type\',\'version\',\'date\',\'market\',\'status\',\'selected_for_daily_limit\',\'squeeze_signal_at\',\'breakout_signal_at\',\'confirm_signal_at\',\'squeeze_bbwidth_pct\',\'box_width_pct\',\'quality_score\',\'break_split_net_pct\',\'confirm_split_net_pct\',\'selected_net_pct\',...data.flatMap(x=>Object.keys(x))])];const csv=\'\\ufeff\'+[cols.join(\',\'),...data.map(x=>cols.map(k=>cv(x[k])).join(\',\'))].join(\'\\r\\n\');const url=URL.createObjectURL(new Blob([csv],{type:\'text/csv;charset=utf-8\'}));const a=document.createElement(\'a\');a.href=url;a.download=`CORE38_BB_SQUEEZE_BREAKOUT_${state.days[0]}_${state.days.at(-1)}_${state.scope}_KRW.csv`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),5000)};\n</script></body></html>\n'

def register_daytrade_review(app, deps):
    """Research only; existing main app and order handling are not modified."""
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache={'at':0.,'sorted':[],'markets':[],'results':{}}
    lock=Lock()

    @app.get('/v2-core-review')
    @app.get('/backtest-daytrade')
    def core38_page():
        return Response(CORE_PAGE,mimetype='text/html; charset=utf-8')

    @app.get('/api/v2core/universe')
    def core38_universe():
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
                    pass
            ranked=sorted(markets,key=lambda m:(-ranking.get(m,0),m))
            with lock:
                cache.update(at=time.monotonic(),sorted=ranked,markets=markets)
            return jsonify(ok=True,version=VERSION,items=ranked,listed=len(markets),cached=False,
                           warning='현재 24시간 거래대금 순위이며 과거 순위가 아닙니다')
        except Exception as e:
            return jsonify(ok=False,error=f'{type(e).__name__}: {e}'[:180]),400

    @app.post('/api/v2core/squeeze38/analyze')
    def core38_analyze():
        try:
            raw=request.get_json(silent=True)
            cfg=_check_cfg(raw,kst)
            market=raw.get('market')
            if not isinstance(market,str) or not MARKET_PATTERN.fullmatch(market):
                raise ValueError('KRW 거래종목 코드가 잘못됐습니다')
            with lock:
                is_valid=market in cache['markets'] if cache['markets'] and time.monotonic()-cache['at']<600 else None
            if is_valid is False or (is_valid is None and market not in get_markets()):
                raise ValueError('현재 KRW 거래종목 목록에 없는 코인')
            key=(market,cfg['day'],cfg['fee'],cfg['slip'])
            with lock:
                prev=cache['results'].get(key)
            if prev is not None and time.monotonic()-prev[0]<600:
                return jsonify(ok=True,version=VERSION,cached=True,result=prev[1])
            rows, fetch_info=fetch_5m(api_get,market,cfg['day'],kst)
            report=analyze_squeezes(rows,cfg['day'],market,cfg['fee'],cfg['slip'])
            report['coverage'].update(fetch_info)
            with lock:
                if len(cache['results'])>1000:
                    cache['results'].clear()
                cache['results'][key]=(time.monotonic(),report)
            return jsonify(ok=True,version=VERSION,cached=False,result=report)
        except Exception as e:
            return jsonify(ok=False,error=f'{type(e).__name__}: {e}'[:200]),400
