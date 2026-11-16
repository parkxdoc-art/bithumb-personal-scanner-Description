

# ---------------------------------------------------------------------------
# V2 CORE.18 -- RESEARCH ONLY: 6 same-entry exit rules in ONE candle fetch.
# SL0: unchanged CORE.16 E; SL1: unchanged CORE.17 F.
# Initial gross-price stop (-1/-1.5/-2/-2.5/-3%) stays active ONLY
# before the first 0.5% NET TP. Thereafter: 30/30/40 + next-bar BE.
# Existing CORE.17 / CORE.16 results are copied without recomputation.
# ---------------------------------------------------------------------------
CORE18_STOPS = (0.0, 1.0, 1.5, 2.0, 2.5, 3.0)
CORE18_RULE_BY_STOP = {
    0.0: 'SCALE_05_5_10_BE',
    1.0: 'SCALE_05_5_10_BE_SL1',
    1.5: 'SCALE_05_5_10_BE_SL15',
    2.0: 'SCALE_05_5_10_BE_SL2',
    2.5: 'SCALE_05_5_10_BE_SL25',
    3.0: 'SCALE_05_5_10_BE_SL3',
}
CORE18_RULES = tuple(CORE18_RULE_BY_STOP.values())

def evaluate_core18_focus(rows5, cfg, core17):
    focus = core17.get('events', [])
    index5 = {r['t'].strftime('%Y-%m-%d %H:%M'): i
              for i, r in enumerate(rows5)}
    events = []
    for old in focus:
        ev = dict(old)
        previous = old.get('core17_scenarios') or {}
        scenarios = {
            CORE18_RULE_BY_STOP[0.0]: previous['SCALE_05_5_10_BE'],
            CORE18_RULE_BY_STOP[1.0]: previous['SCALE_05_5_10_BE_SL1'],
        }
        i = index5.get(old.get('signal_time_kst'))
        for stop_pct in CORE18_STOPS[2:]:
            scenarios[CORE18_RULE_BY_STOP[stop_pct]] = (
                core17_simulate_stop_before_first_tp(rows5, i, cfg, stop_pct))
        full = bool(old.get('core17_full_path'))
        ev.update(core18_scenarios=scenarios,
                  core18_full_path=full,
                  core18_path_reason=old.get('core17_path_reason', ''),
                  core18_early_confirmed=(not full and any(
                      o.get('status') == 'RESOLVED' for o in scenarios.values())))
        if full and any(scenarios[r].get('status') != 'RESOLVED' for r in CORE18_RULES):
            raise AssertionError('CORE.18 complete path unresolved')
        # Preservation contracts for the previous two strategies.
        assert scenarios[CORE18_RULE_BY_STOP[0.0]] == previous['SCALE_05_5_10_BE']
        assert scenarios[CORE18_RULE_BY_STOP[1.0]] == previous['SCALE_05_5_10_BE_SL1']
        events.append(ev)
    return dict(strategy=CORE11_FOCUS, events=events, rules=list(CORE18_RULES),
                stop_gross_pcts=list(CORE18_STOPS),
                initial_stop_active_until='first net +0.5% TP',
                first_take_profit_net_pct=0.5,
                higher_take_profit_net_pcts=[5.0, 10.0],
                sell_fractions_original=[0.30, 0.30, 0.40],
                be_after_first_tp=True, be_effective_from='next 5m candle',
                maximum_hold_minutes=240, research_only=True,
                comparison_rule=('B_10M_MA03 same entries and cost assumptions; '
                   'SL0 = unchanged CORE16 E, SL1 = unchanged CORE17 F; '
                   'initial entry-price stop only before first TP; thereafter '
                   'cost-adjusted next-bar BE; 5m mixed intrabar SL beats TP; '
                   'open price used on stop gap; compare same full 240m paths, '
                   'track early resolved separately; no fabricated candles'))



# ---------------------------------------------------------------------------
# V2 CORE.20 — liquidity-aware post-entry rebound / downtrend REVIEW ONLY.
# Original CORE.19 results and live BUY are NEVER modified.
# Trigger: first completed 5m CLOSE <= entry price * 0.995, before
# the baseline BE strategy is fully closed. Detect only with past bars.
# Classification at that close uses COMPLETED bars exclusively.
# Follow-up 15/30/60m returns are outcomes, NEVER classification inputs.
# ---------------------------------------------------------------------------
CORE20_DIP_TRIGGER_PCT = 0.5
CORE20_HORIZONS = (15, 30, 60)
CORE20_LIQ_BARS = 12
CORE20_STATES = ('RECOVERY_CONFIRMED', 'REBOUND_WATCH',
                 'DOWNTREND', 'INDETERMINATE', 'INSUFFICIENT_DATA')


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


def core20_fetch_3m_window(api_get, market, asof, kst):
    """One bounded 3m request only for liquid symbols with a dip trigger.

    Request ends 65min after decision (for 15/30m rechecks) and includes
    135 historical 3m bars. The response may contain future bars, but each
    classifier always filters by candle END <= decision_time.
    """
    cursor = asof + timedelta(minutes=65)
    raw = api_get('/v1/candles/minutes/3', {
        'market':market, 'count':135,
        'to':cursor.strftime('%Y-%m-%d %H:%M:%S')})
    if not isinstance(raw,list):
        raise ValueError('3분봉 API 응답 오류')
    seen = {}
    for item in raw:
        if not isinstance(item,dict):
            continue
        try:
            ts = local_time(item['candle_date_time_kst'],kst)
            op,hi,lo,cl = (float(item[k]) for k in
                ('opening_price','high_price','low_price','trade_price'))
            vol = float(item['candle_acc_trade_volume'])
            if (ts >= cursor or ts.minute%3 != 0 or ts.second != 0 or
                not all(isfinite(x) and x>0 for x in (op,hi,lo,cl)) or
                not isfinite(vol) or vol<0 or lo>min(op,cl) or hi<max(op,cl)):
                continue
            seen[ts] = dict(t=ts,o=op,h=hi,l=lo,c=cl,v=vol)
        except (ValueError,TypeError,KeyError,OverflowError):
            pass
    return [seen[t] for t in sorted(seen)]


def core20_tf_trend(rows, tf, asof):
    """Trend at a single timestamp; no incomplete or nonconsecutive bars."""
    observed = [b for b in rows if b['t'] + timedelta(minutes=tf) <= asof]
    if len(observed)<7:
        return dict(state='UNKNOWN', reason='이평선 계산 데이터 부족')
    recent=observed[-7:]
    if any(recent[i]['t']-recent[i-1]['t'] != timedelta(minutes=tf)
           for i in range(1,len(recent))):
        return dict(state='UNKNOWN', reason='최근 분봉 공백')
    cs = [float(b['c']) for b in recent]
    ma3 = sum(cs[-3:])/3
    ma3_before = sum(cs[-4:-1])/3
    ma5 = sum(cs[-5:])/5
    close = cs[-1]
    slope_pct = 100*(ma3/ma3_before-1) if ma3_before>0 else 0
    last=recent[-1]
    prev=recent[-2]
    higher_lows = (last['l']>prev['l'] and prev['l']>=recent[-3]['l'])
    lower_low = last['l']<min(b['l'] for b in recent[-4:-1])
    prev_turnovers = [b['c']*b['v'] for b in recent[-5:-1]]
    denom=sum(prev_turnovers)/4
    vol_ratio = last['c']*last['v']/denom if denom>0 else None
    bullish = (higher_lows and close>ma3 and slope_pct>0)
    bearish = (close<ma5 and slope_pct<0 and (lower_low or close<prev['c']))
    state = 'UP' if bullish else 'DOWN' if bearish else 'MIXED'
    return dict(state=state, reason='', sma3_slope_pct=round(slope_pct,5),
                close_vs_ma5_pct=round(100*(close/ma5-1),5),
                volume_ratio4=round(vol_ratio,5) if vol_ratio is not None else None,
                higher_lows=bool(higher_lows), lower_low=bool(lower_low))


def core20_classify(data5, data3, data10, data15, liquidity, asof):
    primary=liquidity['primary_tf']; secondary=liquidity['secondary_tf']
    if primary is None:
        return dict(state='INSUFFICIENT_DATA', primary_state='UNKNOWN',
                    secondary_state='UNKNOWN', reason='거래대금 또는 유효 분봉 부족')
    bars={3:data3, 5:data5, 10:data10, 15:data15}
    if primary == 3 and data3 is None:
        return dict(state='INSUFFICIENT_DATA', primary_state='UNKNOWN',
                    secondary_state='UNKNOWN', reason='3분봉 조회 불가')
    first=core20_tf_trend(bars[primary],primary,asof)
    second=core20_tf_trend(bars[secondary],secondary,asof)
    a,b=first['state'],second['state']
    if 'UNKNOWN' in (a,b):
        label='INSUFFICIENT_DATA'
    elif a=='DOWN' and b=='DOWN':
        label='DOWNTREND'
    elif a=='UP' and b=='UP' and (first.get('volume_ratio4') or 0) >= 1.1:
        label='RECOVERY_CONFIRMED'
    elif a=='UP':
        label='REBOUND_WATCH'
    else:
        label='INDETERMINATE'
    return dict(state=label, primary_state=a, secondary_state=b,
                primary_sma3_slope_pct=first.get('sma3_slope_pct'),
                primary_volume_ratio4=first.get('volume_ratio4'),
                primary_higher_lows=first.get('higher_lows'),
                primary_lower_low=first.get('lower_low'),
                reason=first.get('reason') or second.get('reason') or '')


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


def evaluate_core20_focus(rows5, cfg, core19, market, kst, api_get=None):
    """Observe the first post-entry -0.5% completed-close dip per BE trade.

    Neither the base BE settlement nor any CORE19 scenario is edited.
    3m candles are optional, requested only on HIGH tier after a trigger.
    """
    idx={b['t'].strftime('%Y-%m-%d %H:%M'):i for i,b in enumerate(rows5)}
    rows10=core20_aggregate_5m(rows5,10)
    rows15=core20_aggregate_5m(rows5,15)
    events=[]
    for old in core19.get('events',[]):
        i=idx.get(old.get('signal_time_kst'))
        ev={k:old.get(k) for k in ('market','base_id','signal_time_kst','entry_time_kst')}
        ev.update(status='NO_TRIGGER', reason='완성 5분봉 -0.5% 하락 조건 미발생',
                  trigger_time_kst='',trigger_minutes=None,trigger_gross_pct=None,
                  liquidity=None, decision=None, recheck_15=None, recheck_30=None,
                  followups={}, baseline_be_net_pct=None, baseline_be_status='UNKNOWN',
                  baseline_be_outcome='UNKNOWN', first_trigger_before_be_exit=True,
                  source_strategy='B_10M_MA03', classification_only=True)
        base=old['core19_scenarios'][CORE18_RULE_BY_STOP[0.0]]
        ev['baseline_be_net_pct']=base.get('net_pct')
        ev['baseline_be_status']=base.get('status','UNKNOWN')
        ev['baseline_be_outcome']=base.get('outcome','UNKNOWN')
        if i is None or i+1>=len(rows5) or rows5[i+1]['t']-rows5[i]['t']!=timedelta(minutes=5):
            ev.update(status='UNKNOWN',reason='진입 시각 연속 5분봉 부족')
            events.append(ev);continue
        entry=float(rows5[i+1]['o'])
        if not isfinite(entry) or entry<=0:
            ev.update(status='UNKNOWN',reason='진입 가격 오류')
            events.append(ev);continue
        max_j=min(len(rows5)-1,i+CORE16_MAX_HOLD//5)
        closed_at = base.get('minutes') if base.get('status')=='RESOLVED' else None
        trigger=None
        prior=i
        for j in range(i+1,max_j+1):
            if rows5[j]['t']-rows5[prior]['t'] != timedelta(minutes=5):
                ev.update(status='UNKNOWN',reason='하락 관찰 구간에서 5분봉 공백')
                break
            prior=j
            # A decision at the close of bar j cannot affect an exit in bar j.
            if closed_at is not None and (j-i)*5 >= closed_at:
                ev['reason']='기존 BE 포지션 청산 전에 하락 트리거 없음'
                break
            if rows5[j]['c'] <= entry*(1-CORE20_DIP_TRIGGER_PCT/100):
                trigger=j;break
        if trigger is None:
            # No trigger cannot be established from an unfinished observation.
            if (ev['status']=='NO_TRIGGER' and closed_at is None and
                    max_j<i+CORE16_MAX_HOLD//5):
                ev.update(status='UNKNOWN',reason='240분까지 관찰할 미래 5분봉 부족')
            events.append(ev);continue
        t=rows5[trigger]['t']+timedelta(minutes=5)
        ev['trigger_time_kst']=t.strftime('%Y-%m-%d %H:%M')
        ev['trigger_minutes']=(trigger-i)*5
        ev['trigger_gross_pct']=round(100*(rows5[trigger]['c']/entry-1),6)
        liq=core20_liquidity(rows5,trigger)
        ev['liquidity']=liq
        rows3=None
        fetch_note=''
        if liq['primary_tf']==3:
            if api_get is None:
                fetch_note='3분봉 조회 함수 없음'
            else:
                try:
                    rows3=core20_fetch_3m_window(api_get,market,t,kst)
                except Exception as exc:
                    fetch_note=('3분봉 조회 실패: '+type(exc).__name__)[:100]
        ev['fetch_3m_note']=fetch_note
        decision=core20_classify(rows5,rows3,rows10,rows15,liq,t)
        if fetch_note and decision['state']=='INSUFFICIENT_DATA':
            decision['reason']=fetch_note
        ev['decision']=decision
        ev['status']='TRIGGERED'
        ev['reason']=''
        for mins in (15,30):
            f=core20_followup(rows5,trigger,mins)
            k='recheck_'+str(mins)
            if f['status']!='READY':
                ev[k]=dict(state='UNKNOWN',reason='해당 시점 연속 5분봉 부족')
            else:
                asof=t+timedelta(minutes=mins)
                ev[k]=core20_classify(rows5,rows3,rows10,rows15,liq,asof)
        for mins in CORE20_HORIZONS:
            ev['followups'][str(mins)]=core20_followup(rows5,trigger,mins)
        # Preservation contract: original nested results are never mutated.
        assert old['core19_scenarios'][CORE18_RULE_BY_STOP[0.0]] == base
        events.append(ev)
    return dict(strategy='CORE20_LIQUIDITY_RECHECK_B_10M_MA03',
                events=events, dip_trigger_gross_pct=CORE20_DIP_TRIGGER_PCT,
                future_horizon_minutes=list(CORE20_HORIZONS),
                liquidity_hypotheses={'high_min_avg_5m_krw':5_000_000,
                  'mid_min_avg_5m_krw':1_000_000,
                  'low_min_avg_5m_krw':250_000,
                  'high_min_valid_5m':11,'mid_min_valid_5m':10,
                  'low_min_valid_5m':8,'window_scheduled_5m':12},
                research_only=True,
                comparison_rule=('trigger on completed 5m close -0.5pct before BE exit; '
                  'dynamic 3+5/5+10/10+15 by trailing 60m observed volume and gaps; '
                  '3m separate API fetch only for high-liquidity triggered trades; '
                  'only completed bars at decision; 15/30/60m future outcome separately; '
                  'no changes to CORE19, no automatic exit or live-BUY changes'))

# ---------------------------------------------------------------------------
# V2 CORE.19 · ATR(14) of COMPLETED 10-minute bars, at signal close.
# Four fixed-at-entry ATR multipliers, bound to gross initial stop [1,3] pct.
# Same B_10M_MA03 signals, 30/30/40 TP and next-bar cost-aware BE.
# CORE.18 six existing outcomes are preserved without recomputation.
# ---------------------------------------------------------------------------
CORE19_ATR_MULTS = (1.0, 1.5, 2.0, 2.5)
CORE19_ATR_MIN_STOP_PCT = 1.0
CORE19_ATR_MAX_STOP_PCT = 3.0
CORE19_ATR_RULE_BY_MULT = {
    1.0: 'SCALE_05_5_10_BE_ATR1',
    1.5: 'SCALE_05_5_10_BE_ATR15',
    2.0: 'SCALE_05_5_10_BE_ATR2',
    2.5: 'SCALE_05_5_10_BE_ATR25',
}
CORE19_RULES = CORE18_RULES + tuple(CORE19_ATR_RULE_BY_MULT.values())

def evaluate_core19_focus(rows5, cfg, core18):
    index5 = {r['t'].strftime('%Y-%m-%d %H:%M'): i
              for i, r in enumerate(rows5)}
    events = []
    for old in core18.get('events', []):
        ev = dict(old)
        scenarios = dict(old['core18_scenarios'])
        i = index5.get(old.get('signal_time_kst'))
        atr_raw = old.get('core14_atr14_10m_abs')
        atr_status = old.get('core14_atr_data_status', 'UNKNOWN')
        entry = None
        if i is not None and i + 1 < len(rows5):
            entry = float(rows5[i + 1]['o'])
        ready = (atr_status == 'READY' and atr_raw is not None
                 and entry is not None and isfinite(entry) and entry > 0
                 and isfinite(float(atr_raw)) and float(atr_raw) > 0)
        atr_pct = 100.0 * float(atr_raw) / entry if ready else None
        stop_pcts = {}
        for multiple, rule in CORE19_ATR_RULE_BY_MULT.items():
            if ready:
                stop = min(CORE19_ATR_MAX_STOP_PCT,
                           max(CORE19_ATR_MIN_STOP_PCT, atr_pct * multiple))
                stop_pcts[rule] = round(stop, 8)
                o = core17_simulate_stop_before_first_tp(rows5, i, cfg, stop)
                o['atr_multiple'] = multiple
                o['atr_pct_at_entry'] = round(atr_pct, 8)
                o['atr_stop_gross_pct'] = round(stop, 8)
                o['atr_clamped_min'] = atr_pct * multiple < CORE19_ATR_MIN_STOP_PCT
                o['atr_clamped_max'] = atr_pct * multiple > CORE19_ATR_MAX_STOP_PCT
            else:
                o = dict(outcome='UNKNOWN', label='UNKNOWN', status='UNKNOWN',
                         net_pct=None, minutes=None, exit_time_kst='',
                         reason='진입 당시 완성된 10분봉 ATR(14) 산출 불가: ' + str(atr_status),
                         stop_gross_pct=None, stop_price=None, stop_triggered=False,
                         stop_min=None, stop_tp_same_bar=False, initial_stop=False,
                         tp1_filled=False, tp5_filled=False, tp10_filled=False,
                         be_triggered=False, ambiguous_same_bar=False,
                         tp1_min=None, tp5_min=None, tp10_min=None, be_min=None,
                         partial_realized_net_pct=0.0, remaining_fraction=1.0,
                         exit_legs=[], tp_prices=[], be_price=None,
                         atr_multiple=multiple, atr_pct_at_entry=None,
                         atr_stop_gross_pct=None, atr_clamped_min=False,
                         atr_clamped_max=False)
            scenarios[rule] = o
        full = bool(old.get('core18_full_path'))
        if ready and full and any(scenarios[rule]['status'] != 'RESOLVED'
                                  for rule in CORE19_RULES):
            raise AssertionError('CORE19 240m full path unexpectedly unresolved')
        for rule in CORE18_RULES:
            assert scenarios[rule] == old['core18_scenarios'][rule]
        ev.update(core19_scenarios=scenarios,
                  core19_full_path=full,
                  core19_path_reason=old.get('core18_path_reason', ''),
                  core19_atr_status=atr_status,
                  core19_atr_ready=bool(ready),
                  core19_atr14_10m_abs=float(atr_raw) if ready else None,
                  core19_atr_pct_entry=round(atr_pct, 8) if ready else None,
                  core19_stop_pcts=stop_pcts,
                  core19_early_confirmed=(not full and any(
                      scenarios[r]['status'] == 'RESOLVED' for r in CORE19_RULES)))
        events.append(ev)
    return dict(strategy=CORE11_FOCUS, events=events, rules=list(CORE19_RULES),
                atr_multipliers=list(CORE19_ATR_MULTS),
                atr_period=14, atr_timeframe_minutes=10,
                stop_gross_pct_min=CORE19_ATR_MIN_STOP_PCT,
                stop_gross_pct_max=CORE19_ATR_MAX_STOP_PCT,
                atr_fixed_at='B_10M_MA03 completed signal 10m candle',
                initial_stop_active_until='first net +0.5% TP',
                after_first_tp='cost-aware BE from next 5m candle',
                maximum_hold_minutes=240, research_only=True,
                comparison_rule=('6 existing CORE18 outcomes copied byte-for-byte; '
                                 '4 ATR14-10m x1/1.5/2/2.5 variants at entry; '
                                 'clamp gross-price stop to 1..3 pct; no future ATR; '
                                 'all 10 only compared on same ATR-ready full paths; '
                                 'missing ATR stays UNKNOWN; no auto orders'))

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
VERSION = 'V2.CORE.20'

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

def evaluate_core_rows(rows6,cfg,market,kst,meta=None,core20_api_get=None):
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
    result['core11_focus'] = evaluate_core11_focus(rows6, cfg, result['core10_timeframes'])
    result['core12_focus'] = evaluate_core12_focus(rows6, cfg, result['core11_focus'])
    result['core13_focus'] = evaluate_core13_focus(rows6, cfg, result['core12_focus'])
    result['core14_focus'] = evaluate_core14_focus(rows6, cfg, result['core13_focus'])
    result['core15_focus'] = evaluate_core15_focus(rows6, cfg, result['core14_focus'])
    result['core16_focus'] = evaluate_core16_focus(rows6, cfg, result['core15_focus'])
    result['core17_focus'] = evaluate_core17_focus(rows6, cfg, result['core16_focus'])
    result['core18_focus'] = evaluate_core18_focus(rows6, cfg, result['core17_focus'])
    result['core19_focus'] = evaluate_core19_focus(rows6, cfg, result['core18_focus'])
    result['core20_focus'] = evaluate_core20_focus(rows6, cfg, result['core19_focus'], market, kst, core20_api_get)
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



# ---------------------------------------------------------------------------
# V2 CORE.11: diagnostics for ONE preselected signal: Beggs B, 10m x SMA3.
# Original 12 CORE.10 strategies and all previous evaluations are untouched.
# Uses only same already-fetched coin candles. Historical BTC context is
# linked once per date in the browser from CORE.7's existing cached endpoint.
# ---------------------------------------------------------------------------
CORE11_FOCUS = 'B_10M_MA03'
CORE11_HOLDS = (15, 30, 60)


def evaluate_core11_focus(rows5, cfg, core10):
    """Attach descriptive diagnostics and paired exits to existing B10/SMA3.

    Historical volume is measured in completed 10m bars: signal bar volume
    divided by the average of TWENTY PRIOR consecutive 10m bars. The signal
    candle is not included in the denominator. Missing periods -> None.

    15/30/60m comparisons are reported only on the SAME full continuous 60m
    future 5m path. This is an evaluation sample rule, not an entry filter.
    All TP/SL paths retain existing next-5m open and conservative SL-first
    same-bar assumption. Returns do NOT mutate original outcome or net_pct.
    """
    focus = core10.get('groups', {}).get(CORE11_FOCUS, [])
    bars10 = core10_resample_10m(rows5)
    lookup10 = {r['last_5m_t'].strftime('%Y-%m-%d %H:%M'):i
                for i, r in enumerate(bars10)}
    lookup5 = {r['t'].strftime('%Y-%m-%d %H:%M'):i for i, r in enumerate(rows5)}
    for e in focus:
        e.update(core11_volume_ratio20_10m=None,
                 core11_atr14_10m_pct=None,
                 core11_path_status='UNKNOWN',
                 core11_path_reason='',
                 core11_reference='B 10m SMA3 research-only')
        for h in CORE11_HOLDS:
            for key, val in (('outcome','UNKNOWN'),('net_pct',None),('minutes',None)):
                e['core11_hold_%dm_%s' % (h,key)] = val
        stamp = e['signal_time_kst']
        pos10 = lookup10.get(stamp)
        if pos10 is not None and pos10 >= 20 and lab_contiguous_tf(bars10,pos10-20,pos10,10):
            prior = bars10[pos10-20:pos10]
            vol_avg = sum(b['v'] for b in prior)/20
            if vol_avg > 0:
                e['core11_volume_ratio20_10m'] = round(bars10[pos10]['v']/vol_avg, 6)
            # Completed 10-minute signal and 14 prior bars: no future access.
            trs=[]
            for j in range(pos10-13,pos10+1):
                b, p = bars10[j], bars10[j-1]
                trs.append(max(b['h']-b['l'], abs(b['h']-p['c']),abs(b['l']-p['c'])))
            if bars10[pos10]['c']>0:
                e['core11_atr14_10m_pct'] = round(100*sum(trs)/14/bars10[pos10]['c'], 6)
        pos5 = lookup5.get(stamp)
        if pos5 is None or pos5+12>=len(rows5):
            e['core11_path_reason']='60분 신호 후 5분봉 부족'
            continue
        if not lab_contiguous(rows5,pos5,pos5+12):
            e['core11_path_reason']='60분 신호 후 5분봉 공백'
            continue
        outputs={}
        for h in CORE11_HOLDS:
            alt=dict(cfg,bars=h//5,horizon=h)
            outputs[h]=lab_outcome_v11145(rows5,pos5,alt)
        if any(v.get('outcome') not in ('TP','SL','TIME') for v in outputs.values()):
            e['core11_path_reason']='청산 계산 판정 불가'
            continue
        e['core11_path_status']='FULL'
        e['core11_path_reason']='15·30·60분 동일 60분 연속봉 확보'
        for h, o in outputs.items():
            e['core11_hold_%dm_outcome' % h]=o['outcome']
            e['core11_hold_%dm_net_pct' % h]=o['net_pct']
            e['core11_hold_%dm_minutes' % h]=o['minutes']
    return {'strategy':CORE11_FOCUS, 'events':focus,
            'holds_minutes':list(CORE11_HOLDS),
            'comparison_rule':'same 60m full path; TP1.5 SL1.0; cost included'}


# ---------------------------------------------------------------------------
# V2 CORE.12: B 10-minute SMA3 fixed-horizon 0.5%-net label experiment.
# Does not change 1.5% TP / 1.0% SL execution, CORE.11 or V2 live BUY.
# A fixed-time CLOSE below +0.5% net is labeled '손절' by the requested
# research convention, even when its actual return is small positive.
# This label is NOT a real stop order or evidence of a -0.5% loss.
# Full contiguous 240-minute 5m paths only, for paired comparisons.
# ---------------------------------------------------------------------------
CORE12_HOLDS = (15, 30, 60, 120, 180, 240)
CORE12_NET_TARGET_PCT = 0.5


def evaluate_core12_focus(rows5, cfg, core11):
    focus = core11.get('events', [])
    index5 = {r['t'].strftime('%Y-%m-%d %H:%M'): i
              for i, r in enumerate(rows5)}
    output = []
    n = max(CORE12_HOLDS) // 5
    cost = (cfg['fee'] + cfg['slip']) / 100
    for base in focus:
        e = dict(base)  # do NOT mutate already-evaluated CORE.11 events
        e.update(core12_path_status='UNKNOWN', core12_path_reason='',
                 core12_entry=None, core12_net_target_pct=CORE12_NET_TARGET_PCT,
                 core12_full_window_minutes=max(CORE12_HOLDS))
        for hold in CORE12_HOLDS:
            prefix = f'core12_hold_{hold}m_'
            e.update({prefix+'net_pct': None, prefix+'label': 'UNKNOWN',
                      prefix+'target_reached': None,
                      prefix+'first_target_min': None})
        i = index5.get(base.get('signal_time_kst'))
        if i is None or i+1 >= len(rows5):
            e['core12_path_reason'] = '진입 다음 5분봉 없음'
            output.append(e)
            continue
        if rows5[i+1]['t']-rows5[i]['t'] != timedelta(minutes=5):
            e['core12_path_reason'] = '진입 전 5분봉 공백'
            output.append(e)
            continue
        if i+n >= len(rows5):
            e['core12_path_reason'] = '240분 미래 5분봉 부족'
            output.append(e)
            continue
        if not lab_contiguous(rows5, i, i+n):
            e['core12_path_reason'] = '240분 미래 5분봉 공백'
            output.append(e)
            continue
        entry = rows5[i+1]['o']
        e['core12_entry'] = round(entry, 10)
        e['core12_path_status'] = 'FULL'
        e['core12_path_reason'] = '240분 연속 5분봉 확보'
        # At each horizon, compute actual hypothetical CLOSE net return.
        # Also report whether the high at any earlier 5m slot COULD have
        # reached +0.5% net; this is a diagnostic, not an executed exit.
        first_touch = None
        for k in range(1,n+1):
            high = rows5[i+k]['h']
            possible_net = (high*(1-cost)/(entry*(1+cost))-1)*100
            if first_touch is None and possible_net + 1e-10 >= CORE12_NET_TARGET_PCT:
                first_touch = k*5
            hold = k*5
            if hold not in CORE12_HOLDS:
                continue
            close = rows5[i+k]['c']
            net = (close*(1-cost)/(entry*(1+cost))-1)*100
            prefix = f'core12_hold_{hold}m_'
            # Compare on UNROUNDED value at the inclusive 0.5% boundary.
            e[prefix+'net_pct'] = round(net, 5)
            e[prefix+'label'] = '수익' if net + 1e-10 >= CORE12_NET_TARGET_PCT else '손절'
            e[prefix+'target_reached'] = first_touch is not None
            e[prefix+'first_target_min'] = first_touch
        output.append(e)
    return {
        'strategy': CORE11_FOCUS,
        'events': output,
        'holds_minutes': list(CORE12_HOLDS),
        'net_target_pct': CORE12_NET_TARGET_PCT,
        'comparison_rule': ('same full 240m contiguous path, time-close return '
                            'including round trip cost; >=+0.5% is profit label; '
                            'below +0.5% is research stop label, NOT an order'),
    }


# ---------------------------------------------------------------------------
# V2 CORE.13: three competing 240-minute exit policies on FIXED B 10m/SMA3
# signals. A: net +0.5% TP, no SL; B: same TP + price -1.0% stop;
# C: hold to 240m close, no TP/SL. Both fees and slippage are per side.
# Original CORE.12 samples and events are never modified.
# Early resolved TP/SL remain valid even if LATER 5m candles are absent.
# Equal-sample comparison includes ONLY full contiguous 240m paths.
# No interpolation, no look-ahead, same-bar TP & SL => conservative SL.
# ---------------------------------------------------------------------------
CORE13_RULES = ('TP05_NO_SL', 'TP05_SL1', 'HOLD240')
CORE13_NET_TP_PCT = 0.5
CORE13_STOP_GROSS_PCT = 1.0
CORE13_MAX_HOLD = 240


def evaluate_core13_focus(rows5, cfg, core12):
    from datetime import timedelta as _td
    events = []
    index5 = {r['t'].strftime('%Y-%m-%d %H:%M'): i
              for i,r in enumerate(rows5)}
    n = CORE13_MAX_HOLD // 5
    cost = (cfg['fee'] + cfg['slip']) / 100.0
    step = _td(minutes=5)

    def fmt(t):
        return t.strftime('%Y-%m-%d %H:%M')

    def net_at(entry, price):
        return (price*(1-cost)/(entry*(1+cost))-1)*100

    def blank(reason):
        return dict(outcome='UNKNOWN', label='UNKNOWN', net_pct=None,
                    minutes=None, exit_price=None, exit_time_kst='',
                    status='UNKNOWN', reason=reason)

    def resolved(outcome, entry, exit_price, minutes, exit_time):
        net = net_at(entry, exit_price)
        return dict(outcome=outcome, label=('수익' if net+1e-10>=CORE13_NET_TP_PCT
                                            else '손절 판정'),
                    net_pct=round(net, 6), minutes=minutes,
                    exit_price=round(exit_price, 10),
                    exit_time_kst=fmt(exit_time),
                    status='RESOLVED', reason='')

    for prev in core12.get('events', []):
        ev = dict(prev)
        i = index5.get(ev.get('signal_time_kst'))
        ev.update(core13_full_path=False, core13_path_status='UNKNOWN',
                  core13_path_reason='', core13_entry=None,
                  core13_tp_price=None,core13_stop_price=None,
                  core13_target_net_pct=CORE13_NET_TP_PCT,
                  core13_stop_gross_pct=CORE13_STOP_GROSS_PCT,
                  core13_first_tp_min=None,core13_first_sl_min=None,
                  core13_first_touch='UNKNOWN',core13_early_confirmed=False)
        outcomes = {rule:blank('진입 다음 5분봉 없음') for rule in CORE13_RULES}
        if i is None or i+1>=len(rows5):
            ev['core13_path_reason']='진입 다음 5분봉 없음'
            ev['core13_scenarios']=outcomes
            events.append(ev)
            continue
        if rows5[i+1]['t']-rows5[i]['t']!=step:
            ev['core13_path_reason']='진입 전 5분봉 공백'
            ev['core13_scenarios']=outcomes
            events.append(ev)
            continue
        entry=rows5[i+1]['o']
        if not isfinite(entry) or entry<=0:
            ev['core13_path_reason']='진입 가격 오류'
            ev['core13_scenarios']=outcomes
            events.append(ev)
            continue
        tp_price=entry*(1+CORE13_NET_TP_PCT/100)*(1+cost)/(1-cost)
        sl_price=entry*(1-CORE13_STOP_GROSS_PCT/100)
        ev['core13_entry']=round(entry,10)
        ev['core13_tp_price']=round(tp_price,10)
        ev['core13_stop_price']=round(sl_price,10)
        outcomes={rule:blank('240분 이내 확정 불가') for rule in CORE13_RULES}
        first_tp=None;first_sl=None;both_first=False
        last=i;path_reason=''; path_full=True
        for k in range(1,n+1):
            j=i+k
            if j>=len(rows5):
                path_full=False;path_reason='240분 미래 5분봉 부족';break
            r=rows5[j]
            if r['t']-rows5[last]['t']!=step:
                path_full=False;path_reason='240분 미래 5분봉 공백';break
            last=j
            tp_hit=r['h']+max(abs(tp_price),1)*1e-12>=tp_price
            sl_hit=r['l']<=sl_price
            if tp_hit and first_tp is None: first_tp=k*5
            if sl_hit and first_sl is None: first_sl=k*5
            if tp_hit and sl_hit and first_tp==k*5 and first_sl==k*5:
                both_first=True
            if outcomes['TP05_NO_SL']['outcome']=='UNKNOWN' and tp_hit:
                outcomes['TP05_NO_SL']=resolved('TP',entry,tp_price,k*5,r['t'])
            if outcomes['TP05_SL1']['outcome']=='UNKNOWN':
                # Same-bar TP & SL: stop wins. Gap below SL: fill at open.
                if sl_hit:
                    outcomes['TP05_SL1']=resolved('SL',entry,min(sl_price,r['o']),k*5,r['t'])
                elif tp_hit:
                    outcomes['TP05_SL1']=resolved('TP',entry,tp_price,k*5,r['t'])
            if k==n:
                for rule in ('TP05_NO_SL','TP05_SL1'):
                    if outcomes[rule]['outcome']=='UNKNOWN':
                        outcomes[rule]=resolved('TIME',entry,r['c'],k*5,r['t'])
                outcomes['HOLD240']=resolved('TIME',entry,r['c'],k*5,r['t'])
        ev['core13_full_path']=path_full
        ev['core13_path_status']='FULL' if path_full else 'INCOMPLETE'
        ev['core13_path_reason']='240분 연속 5분봉 확보' if path_full else path_reason
        ev['core13_first_tp_min']=first_tp
        ev['core13_first_sl_min']=first_sl
        if first_tp is not None and first_sl is not None:
            ev['core13_first_touch']= ('SAME_BAR_SL_FIRST' if first_tp==first_sl
                                      else ('TP_FIRST' if first_tp<first_sl else 'SL_FIRST'))
        elif first_tp is not None:
            ev['core13_first_touch']='TP_ONLY_OBSERVED'
        elif first_sl is not None:
            ev['core13_first_touch']='SL_ONLY_OBSERVED'
        elif path_full:
            ev['core13_first_touch']='NEITHER'
        else:
            ev['core13_first_touch']='UNKNOWN_GAP'
        if not path_full:
            for r in CORE13_RULES:
                if outcomes[r]['outcome']=='UNKNOWN':outcomes[r]['reason']=path_reason
        ev['core13_early_confirmed']=(not path_full and
                  any(outcomes[r]['outcome'] in ('TP','SL') for r in CORE13_RULES))
        # Preserve CORE.12 240m fixed-horizon close return exactly (same rules).
        if path_full:
            old=ev.get('core12_hold_240m_net_pct')
            new=outcomes['HOLD240']['net_pct']
            if old is not None and abs(float(old)-new)>0.00012:
                raise AssertionError('CORE.12 240분 종가와 CORE.13 보유 수익률 불일치')
        ev['core13_scenarios']=outcomes
        events.append(ev)
    return dict(strategy=CORE11_FOCUS, events=events,
                rules=list(CORE13_RULES),
                target_net_pct=CORE13_NET_TP_PCT,
                stop_price_pct=CORE13_STOP_GROSS_PCT,
                max_hold_minutes=CORE13_MAX_HOLD,
                comparison_rule=('same 240m complete 5m paths for paired comparison; '
                    'early TP/SL also retained on incomplete later paths; '
                    'net +0.5% TP price includes round-trip fees and slippage; '
                    'same-bar SL-first; gap-down SL at opening price; '
                    'research only, never executed live orders'))


# ---------------------------------------------------------------------------
# V2 CORE.14: Research-only exit variations on the same B 10m/SMA3 entries.
# A: CORE.13 +0.5% net TP / hold up to 240m, no SL (exact baseline copy).
# B: same TP; if not yet hit, at 30m close exit when net <= -0.5%.
# C: same TP; if not yet hit, at 60m close exit when net <= -0.5%.
# D: same TP; intrabar stop at entry - 1.5 * ATR14 of fully closed
#    10-minute bars as known at the SIGNAL (not afterward).
# No prospective order placement, no change to CORE.13 or V2 live BUY.
# Conservative TP/SL same-bar => SL, gaps at stop use worse open.
# No candle filling: full path for paired comparisons; already-confirmed
# exits may survive missing LATER candles, with separate statistics.
# ---------------------------------------------------------------------------
CORE14_RULES = ('TP05_NO_SL', 'DIAG30_NEG05', 'DIAG60_NEG05', 'ATR14_10M_X15')
CORE14_DIAG_NET_PCT = -0.5
CORE14_ATR_PERIOD = 14
CORE14_ATR_MULT = 1.5


def evaluate_core14_focus(rows5, cfg, core13):
    focus = core13.get('events', [])
    lookup5 = {r['t'].strftime('%Y-%m-%d %H:%M'): i for i, r in enumerate(rows5)}
    bars10 = core10_resample_10m(rows5)
    lookup10 = {r['last_5m_t'].strftime('%Y-%m-%d %H:%M'): i
                for i, r in enumerate(bars10)}
    n = CORE13_MAX_HOLD // 5
    step = timedelta(minutes=5)
    cost = (cfg['fee'] + cfg['slip']) / 100.0
    events = []

    def fmt(t):
        return t.strftime('%Y-%m-%d %H:%M')

    def net_at(entry, price):
        return (price * (1-cost) / (entry * (1+cost)) - 1) * 100

    def unknown(reason):
        return dict(outcome='UNKNOWN', label='UNKNOWN', net_pct=None,
                    minutes=None, exit_price=None, exit_time_kst='',
                    status='UNKNOWN', reason=reason)

    def exit_result(reason, entry, price, elapsed, stamp):
        net = net_at(entry, price)
        return dict(outcome=reason,
                    label='수익' if net+1e-10 >= CORE13_NET_TP_PCT else '목표 미달',
                    net_pct=round(net, 6), minutes=elapsed,
                    exit_price=round(price, 10), exit_time_kst=fmt(stamp),
                    status='RESOLVED', reason='')

    for old in focus:
        ev = dict(old)  # Preserve exact CORE.13 output on the prior object.
        parent = old.get('core13_scenarios', {})
        baseline = dict(parent.get('TP05_NO_SL') or unknown('CORE.13 기준 누락'))
        outcomes = {name: unknown('진입 전/이후 경로 부족') for name in CORE14_RULES}
        outcomes['TP05_NO_SL'] = baseline
        ev.update(core14_atr14_10m_abs=None,
                  core14_atr14_10m_pct_entry=None,
                  core14_atr_stop_price=None,
                  core14_atr_data_status='UNKNOWN',
                  core14_early_confirmed=False,
                  core14_scenarios=outcomes,
                  core14_full_path=bool(old.get('core13_full_path')),
                  core14_path_reason=old.get('core13_path_reason', ''))
        i = lookup5.get(old.get('signal_time_kst'))
        entry = old.get('core13_entry')
        if i is None or i+1 >= len(rows5) or entry is None or entry <= 0:
            ev['core14_atr_data_status'] = 'ENTRY_UNKNOWN'
            events.append(ev)
            continue
        if rows5[i+1]['t'] - rows5[i]['t'] != step:
            ev['core14_atr_data_status'] = 'ENTRY_GAP'
            events.append(ev)
            continue
        # Use unrounded actual next 5m opening price for exact fee accounting.
        # CORE.13 stores a rounded display field, not the execution reference.
        entry = float(rows5[i+1]['o'])
        tp_price = entry*(1+CORE13_NET_TP_PCT/100)*(1+cost)/(1-cost)

        # Compute ATR(14) from 14 fully completed 10-minute bars only.
        # The signal bar is the final known 10m bar; exclude all future bars.
        pos10 = lookup10.get(old.get('signal_time_kst'))
        atr_stop = None
        if (pos10 is not None and pos10 >= CORE14_ATR_PERIOD
                and lab_contiguous_tf(bars10, pos10-CORE14_ATR_PERIOD, pos10, 10)):
            trs = []
            for j in range(pos10-CORE14_ATR_PERIOD+1, pos10+1):
                b, prev = bars10[j], bars10[j-1]
                trs.append(max(b['h']-b['l'], abs(b['h']-prev['c']),
                               abs(b['l']-prev['c'])))
            atr = sum(trs) / CORE14_ATR_PERIOD
            if isfinite(atr) and 0 < atr and entry - CORE14_ATR_MULT*atr > 0:
                atr_stop = entry - CORE14_ATR_MULT*atr
                ev['core14_atr14_10m_abs'] = round(atr, 10)
                ev['core14_atr14_10m_pct_entry'] = round(100*atr/entry, 6)
                ev['core14_atr_stop_price'] = round(atr_stop, 10)
                ev['core14_atr_data_status'] = 'READY'
            else:
                ev['core14_atr_data_status'] = 'ATR_INVALID'
        else:
            ev['core14_atr_data_status'] = 'ATR_HISTORY_MISSING'
        outcomes['DIAG30_NEG05'] = unknown('240분 이내 결과 미확정')
        outcomes['DIAG60_NEG05'] = unknown('240분 이내 결과 미확정')
        outcomes['ATR14_10M_X15'] = (unknown('240분 이내 결과 미확정') if atr_stop is not None
                                     else unknown('진입 당시 ATR14 산출 불가'))
        prev_i = i
        for k in range(1, n+1):
            j = i+k
            if j >= len(rows5):
                break
            bar = rows5[j]
            if bar['t'] - rows5[prev_i]['t'] != step:
                break
            prev_i = j
            tp_hit = bar['h'] + max(abs(tp_price), 1)*1e-12 >= tp_price
            # In diagnostic variants TP touch precedes closing-price diagnosis
            # within the same 5m bar. No intrabar stop is defined for B/C.
            for rule, diag_at in (('DIAG30_NEG05', 6), ('DIAG60_NEG05', 12)):
                if outcomes[rule]['outcome'] != 'UNKNOWN':
                    continue
                if tp_hit:
                    outcomes[rule] = exit_result('TP', entry, tp_price, k*5, bar['t'])
                elif k == diag_at and net_at(entry, bar['c']) <= CORE14_DIAG_NET_PCT+1e-10:
                    outcomes[rule] = exit_result('DIAG', entry, bar['c'], k*5, bar['t'])
                elif k == n:
                    outcomes[rule] = exit_result('TIME', entry, bar['c'], k*5, bar['t'])
            rule = 'ATR14_10M_X15'
            if atr_stop is not None and outcomes[rule]['outcome'] == 'UNKNOWN':
                if bar['l'] <= atr_stop:
                    # If TP and ATR stop occur in one bar, conservatively stop.
                    outcomes[rule] = exit_result('SL', entry, min(atr_stop,bar['o']),
                                                 k*5, bar['t'])
                elif tp_hit:
                    outcomes[rule] = exit_result('TP', entry, tp_price, k*5, bar['t'])
                elif k == n:
                    outcomes[rule] = exit_result('TIME', entry, bar['c'], k*5, bar['t'])
        if not ev['core14_full_path']:
            for rule in CORE14_RULES[1:]:
                if outcomes[rule]['outcome'] == 'UNKNOWN' and atr_stop is not None:
                    outcomes[rule]['reason'] = ev['core14_path_reason']
            ev['core14_early_confirmed'] = any(
                outcomes[rule]['outcome'] in ('TP','SL','DIAG') for rule in CORE14_RULES)
        # Invariants: exact A baseline, same C13 full-window status and entry.
        if ev['core14_full_path']:
            assert all(outcomes[r]['outcome'] in ('TP','SL','DIAG','TIME')
                       for r in CORE14_RULES[:3]), 'CORE.14 full path A/B/C unresolved'
            if atr_stop is not None:
                assert outcomes['ATR14_10M_X15']['outcome'] in ('TP','SL','TIME')
        events.append(ev)
    return dict(strategy=CORE11_FOCUS, events=events, rules=list(CORE14_RULES),
                atr_period=CORE14_ATR_PERIOD, atr_multiplier=CORE14_ATR_MULT,
                diag_net_pct=CORE14_DIAG_NET_PCT,
                target_net_pct=CORE13_NET_TP_PCT,
                max_hold_minutes=CORE13_MAX_HOLD,
                comparison_rule=('same B 10m/SMA3 entries, next 5m open; A copied exactly '
                    'from CORE.13; B diagnostic at 30m if net <= -0.5%; C at 60m '
                    'if net <= -0.5%; D ATR14 of completed signal-time 10m bars '
                    'times 1.5 as gross price stop; TP net +0.5%; full 240m '
                    'common sample required for paired comparison, ATR-ready-only '
                    'for D; no look-ahead or synthetic candles; research-only'))



# ---------------------------------------------------------------------------
# V2 CORE.15: TEST-ONLY 1% -> 5% -> 10% scale-out + optional breakeven.
# The 1/5/10 targets are NET return per sold slice (round-trip costs incl.).
# Slice weights are 50%, 25%, 25% of ORIGINAL position respectively.
# 'BE' stop is cost-adjusted round-trip breakeven for remaining units.
# No stop BEFORE first 1% TP. At 240m, exit the remaining position at close.
# Activate BE from the NEXT 5-minute candle after TP1 (intrabar order
# during the first TP candle is unknowable). Flag same-bar ambiguity.
# If BE already active at bar start, prioritize BE over later TP highs.
# Early completed exits are valid despite gaps afterward; incomplete
# remaining holdings are UNKNOWN, not fabricated completed returns.
# CORE.14 and production V2 BUY / app.py remain unchanged.
# ---------------------------------------------------------------------------
CORE15_RULES = ('TP05_NO_SL', 'SCALE_1_5_10', 'SCALE_1_5_10_BE')
CORE15_TP_NET_PCTS = (1.0, 5.0, 10.0)
CORE15_TP_PARTS = (0.5, 0.25, 0.25)
CORE15_MAX_HOLD = 240


def core15_simulate_scale(rows5, signal_i, cfg, use_be):
    """Run ONE observed 5m path; output weighted net return on original capital."""
    step = timedelta(minutes=5)
    n = CORE15_MAX_HOLD // 5
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
    prices=[entry*(1+p/100.0)*(1+cost)/(1-cost) for p in CORE15_TP_NET_PCTS]
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
            if hi+tol>=prices[stage] and stage<len(prices):
                res['ambiguous_same_bar']=True
            add_leg('BE',remaining,min(be_price,op),minute,bar['t'])
            return finish('BE',minute,bar['t'])
        # No fixed stop before TP1. Split fills in ascending order.
        if stage==0 and hi+tol>=prices[0]:
            add_leg('TP1',CORE15_TP_PARTS[0],prices[0],minute,bar['t'])
            stage=1
            # TP1 and BE same bar have unknown price ordering; BE is
            # armed from NEXT candle to avoid a fictitious same-bar stop
            # merely because the entry candle opened below net-BE.
            if use_be and lo<=be_price+tol:
                res['ambiguous_same_bar']=True
        if stage==1 and hi+tol>=prices[1]:
            add_leg('TP5',CORE15_TP_PARTS[1],prices[1],minute,bar['t'])
            stage=2
        if stage==2 and hi+tol>=prices[2]:
            add_leg('TP10',CORE15_TP_PARTS[2],prices[2],minute,bar['t'])
            stage=3
            return finish('TP10',minute,bar['t'])
        if k==n:
            if remaining>0:
                add_leg('TIME',remaining,cl,minute,bar['t'])
            return finish('TIME',minute,bar['t'])
    res['reason']='판정 불가'
    return res


def evaluate_core15_focus(rows5, cfg, core14):
    focus=core14.get('events', [])
    index5={r['t'].strftime('%Y-%m-%d %H:%M'):i for i,r in enumerate(rows5)}
    events=[]
    for old in focus:
        ev=dict(old)
        parent=(old.get('core13_scenarios') or {}).get('TP05_NO_SL')
        baseline=dict(parent) if parent else dict(outcome='UNKNOWN',status='UNKNOWN',
                                                net_pct=None,reason='기존 A 기준 누락')
        outcomes={'TP05_NO_SL':baseline}
        i=index5.get(old.get('signal_time_kst'))
        outcomes['SCALE_1_5_10']=core15_simulate_scale(rows5,i,cfg,False)
        outcomes['SCALE_1_5_10_BE']=core15_simulate_scale(rows5,i,cfg,True)
        ev.update(core15_scenarios=outcomes,
                  core15_full_path=bool(old.get('core13_full_path')),
                  core15_path_reason=old.get('core13_path_reason',''),
                  core15_early_confirmed=(not old.get('core13_full_path') and
                       any(x.get('status')=='RESOLVED' for x in outcomes.values())))
        if ev['core15_full_path']:
            if any(x.get('status')!='RESOLVED' for x in outcomes.values()):
                raise AssertionError('CORE.15 완전 240분 경로인데 청산 미확정')
        events.append(ev)
    return dict(strategy=CORE11_FOCUS,events=events,rules=list(CORE15_RULES),
                profit_targets_net_pct=list(CORE15_TP_NET_PCTS),
                sell_fractions_original=list(CORE15_TP_PARTS),
                be_reference='round-trip cost-adjusted net breakeven',
                first_tp_without_stop=True,
                max_hold_minutes=CORE15_MAX_HOLD,
                comparison_rule=('same exact B 10m/SMA3 entries, 240m complete '
                   '5m paths for paired comparison; +1/+5/+10% round-trip NET '
                   'per-slice targets, 50/25/25 percent of original position; '
                   'BE arms from next 5m candle after TP1, cost-adjusted; '
                   'conservative BE-first on later bars with both BE and higher TP; '
                   'TP1 and BE same 5m candle flagged ambiguous; '
                   'no stop before TP1, unknown partial exits not counted '
                   'as completed; no real orders, no future price interpolation'))

def lab_contiguous_tf(rows,first,last,tf):
    if first < 0 or last >= len(rows):
        return False
    step = timedelta(minutes=tf)
    return all(rows[j]['t']-rows[j-1]['t']==step for j in range(first+1,last+1))


# ---------------------------------------------------------------------------
# V2 CORE.16 -- research only. 0.5/5/10% NET targets, 30/30/40%
# of INITIAL position. Keep all CORE.15 scenarios byte-for-byte untouched.
# Breakeven activates on NEXT 5m candle after first TP; no initial stop.
# ---------------------------------------------------------------------------
CORE16_RULES = ('TP05_NO_SL', 'SCALE_1_5_10', 'SCALE_1_5_10_BE',
                'SCALE_05_5_10', 'SCALE_05_5_10_BE')
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



def evaluate_core16_focus(rows5, cfg, core15):
    focus=core15.get('events', [])
    index5={r['t'].strftime('%Y-%m-%d %H:%M'):i for i,r in enumerate(rows5)}
    events=[]
    for old in focus:
        ev=dict(old)
        scenarios=dict(old.get('core15_scenarios') or {})
        i=index5.get(old.get('signal_time_kst'))
        scenarios['SCALE_05_5_10']=core16_simulate_scale(rows5,i,cfg,False)
        scenarios['SCALE_05_5_10_BE']=core16_simulate_scale(rows5,i,cfg,True)
        full=bool(old.get('core15_full_path'))
        ev.update(core16_scenarios=scenarios,
                  core16_full_path=full,
                  core16_path_reason=old.get('core15_path_reason',''),
                  core16_early_confirmed=(not full and
                      any(o.get('status')=='RESOLVED' for o in scenarios.values())))
        if full and any(scenarios[r].get('status')!='RESOLVED' for r in CORE16_RULES):
            raise AssertionError('CORE.16 full path unresolved')
        # Hard preservation invariant: prior benchmark and full old split results identical.
        for r in CORE15_RULES:
            assert scenarios[r] == old['core15_scenarios'][r]
        events.append(ev)
    return dict(strategy=CORE11_FOCUS,events=events,rules=list(CORE16_RULES),
                profit_targets_net_pct=list(CORE16_TP_NET_PCTS),
                sell_fractions_original=list(CORE16_TP_PARTS),
                max_hold_minutes=CORE16_MAX_HOLD,
                be_reference='round-trip cost-adjusted net breakeven',
                comparison_rule=('same B 10m SMA3 entries and fees; old 1/5/10 '
                    '50/25/25 versus new 0.5/5/10 30/30/40; '+
                    'all fractions of INITIAL position; 240m fully continuous '+
                    'paths for 5-rule paired comparison; first TP + next-bar '+
                    'BE only; mixed intrabar cases flagged, no initial stop; '+
                    'unknown partial positions excluded from completed returns'))


# ---------------------------------------------------------------------------
# V2 CORE.17 -- RESEARCH ONLY. Add one F rule to existing CORE16 A-E.
# F: before first net +0.5% TP: -1.0% GROSS price fixed stop for 100%.
#    After first partial fill (30%), DISABLE fixed stop and arm cost-
#    adjusted BE on the next 5m candle for the remaining 70% (or 40%).
#    +5%/30%, +10%/40% unchanged. 240m maximum, no synthetic candles.
#    When an initial SL and a TP both touch the same OHLC candle, STOP
#    conservatively takes priority and ambiguity is explicitly flagged.
# ---------------------------------------------------------------------------
CORE17_RULES = CORE16_RULES + ('SCALE_05_5_10_BE_SL1',)
CORE17_STOP_GROSS_PCT = 1.0

def core17_simulate_stop_before_first_tp(rows5, signal_i, cfg, stop_gross_pct=CORE17_STOP_GROSS_PCT):
    """Run ONE observed 5m path; output weighted net return on original capital."""
    step = timedelta(minutes=5)
    n = CORE16_MAX_HOLD // 5
    use_be = True
    cost = (cfg['fee'] + cfg['slip']) / 100.0
    blank = dict(outcome='UNKNOWN', label='UNKNOWN', net_pct=None,
                 minutes=None, exit_time_kst='', status='UNKNOWN',
                 reason='진입/미래 가격 데이터 부족',
                 tp1_filled=False, tp5_filled=False, tp10_filled=False,
                 be_triggered=False, ambiguous_same_bar=False,
                 tp1_min=None, tp5_min=None, tp10_min=None, be_min=None,
                 partial_realized_net_pct=0.0, remaining_fraction=1.0,
                 exit_legs=[], tp_prices=[], be_price=None,
                 initial_stop=False, stop_triggered=False, stop_min=None,
                 stop_price=None, stop_gross_pct=stop_gross_pct,
                 stop_tp_same_bar=False)
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
    sl_price=entry*(1-stop_gross_pct/100.0)
    res=unknown('240분 이내 결과 미확정')
    res['tp_prices']=[round(x,10) for x in prices]
    res['be_price']=round(be_price,10)
    res['stop_price']=round(sl_price,10)
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
        # CORE.17: fixed initial stop ONLY BEFORE first partial +0.5% TP.
        # On an OHLC bar touching both initial SL and TP1, order cannot
        # be known. Prioritize the stop (pessimistic) and FLAG ambiguity.
        # A downward opening gap fills at open, not at the better stop price.
        if stage==0 and lo<=sl_price+tol:
            if hi+tol>=prices[0]:
                res['ambiguous_same_bar']=True
                res['stop_tp_same_bar']=True
            res['initial_stop']=True
            res['stop_triggered']=True
            res['stop_min']=minute
            add_leg('STOP1',remaining,min(sl_price,op),minute,bar['t'])
            return finish('STOP1',minute,bar['t'])
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




def evaluate_core17_focus(rows5, cfg, core16):
    focus=core16.get('events', [])
    index5={r['t'].strftime('%Y-%m-%d %H:%M'):i for i,r in enumerate(rows5)}
    events=[]
    for old in focus:
        ev=dict(old)
        scenarios=dict(old.get('core16_scenarios') or {})
        i=index5.get(old.get('signal_time_kst'))
        scenarios['SCALE_05_5_10_BE_SL1']=core17_simulate_stop_before_first_tp(rows5,i,cfg)
        full=bool(old.get('core16_full_path'))
        ev.update(core17_scenarios=scenarios,
                  core17_full_path=full,
                  core17_path_reason=old.get('core16_path_reason',''),
                  core17_early_confirmed=(not full and
                      any(o.get('status')=='RESOLVED' for o in scenarios.values())))
        if full and any(scenarios[r].get('status')!='RESOLVED' for r in CORE17_RULES):
            raise AssertionError('CORE.17 complete path unresolved')
        for r in CORE16_RULES:
            assert scenarios[r] == old['core16_scenarios'][r]
        events.append(ev)
    return dict(strategy=CORE11_FOCUS,events=events,rules=list(CORE17_RULES),
                target_net_pct=(0.5, 5.0, 10.0),
                initial_sell_fractions=(0.3,0.3,0.4),
                initial_stop_gross_pct=CORE17_STOP_GROSS_PCT,
                initial_stop_active_until='first net +0.5% TP',
                be_after_first_tp=True,
                be_effective_from='next 5m candle',
                maximum_hold_minutes=240,
                research_only=True,
                comparison_rule=('same B_10M_MA03 entries and same cfg costs; '
                   'existing A-E copied unchanged; new F = 30/30/40 with '
                   'gross-entry -1% stop before TP1 then next-bar net BE; '
                   'SL wins initial mixed TP/SL bar, gap-down open realistic; '
                   'full observed 240min path for six-rule paired comparison; '
                   'early fully resolved exits tracked separately'))

CORE_PAGE = '<!doctype html><html lang="ko"><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>V2 CORE.20 · 유동성별 반등·하락 재판정 연구</title>\n<style>\n:root{color-scheme:dark;--bg:#0b1020;--p:#141c2f;--l:#2a3951;--t:#edf3ff;--m:#9dabca;--g:#4ade80;--r:#ff788a}\n*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--t);font:14px/1.55 system-ui,\'Noto Sans KR\',sans-serif}\nmain{max-width:1080px;margin:auto;padding:20px 14px 50px}h1{font-size:23px;margin:0}.sub{color:var(--m);margin:5px 0 16px}\na{color:#accbff}.panel{background:var(--p);border:1px solid var(--l);border-radius:14px;padding:15px;margin-bottom:13px}\n.tools{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}.field{display:flex;flex-direction:column;gap:5px;min-width:135px}\nlabel{font-size:12px;color:var(--m)}input,select,button{border:1px solid #43536b;background:#10192e;border-radius:9px;color:var(--t);padding:10px;font:inherit}\nbutton{background:#3160dc;border-color:#3160dc;cursor:pointer;font-weight:700}button:disabled{opacity:.45;cursor:wait}\nbutton.secondary{background:#202d43;border-color:#43536b}.muted{color:var(--m);font-size:12px}\nprogress{width:100%;height:12px;margin:12px 0}table{border-collapse:collapse;width:100%;min-width:630px}th,td{padding:9px 7px;text-align:right;border-bottom:1px solid var(--l);font-size:12px;white-space:nowrap}\nth:first-child,td:first-child{text-align:left}.scroll{overflow-x:auto}.positive{color:var(--g)}.negative{color:var(--r)}\n.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.kpi{border:1px solid var(--l);border-radius:10px;padding:9px}.kpi b{display:block;font-size:21px}\n@media(max-width:540px){.kpis{grid-template-columns:1fr 1fr}h1{font-size:19px}}\n</style></head><body><main>\n<h1>🧪 V2 CORE.20 · 유동성별 분봉 · 반등 재판정 검증실</h1><div class="sub">A 볼먼 · B 벡스 필터 · K 코너스 — 기존 3전략 유지 · 기존 전략 보존 · CORE.2~6 유지 · 5분·10분봉 × 3·5·10이평 신규 비교 · 기존 연구 보존</div>\n<p><a href="/">← 기존 V2 실전 스캐너로 돌아가기</a></p>\n<section class="panel">\n<div class="tools">\n<div class="field"><label for="day">검사 날짜 (KST)</label><input type="date" id="day"></div>\n<div class="field"><label for="scope">검사 범위</label><select id="scope"><option value="20">거래대금 상위 20종목</option><option value="40" selected>상위 40종목</option><option value="80">상위 80종목</option><option value="all">전체 KRW 종목 (느릴 수 있음)</option></select></div>\n<div class="field"><label for="fee">편도 수수료 (%)</label><input type="number" min="0" max="0.5" step="0.01" value="0.25" id="fee"></div>\n<div class="field"><label for="slip">편도 슬리피지 (%)</label><input type="number" min="0" max="0.5" step="0.01" value="0" id="slip"></div>\n<button id="run">검사 시작</button><button class="secondary" id="csv" disabled>CSV 받기</button></div>\n<div class="muted">기본 상위 40종목만 검사해 빠른 비교에 집중. 전체 검사 선택 시 이전 연구처럼 시간이 오래 걸릴 수 있어.</div>\n<progress id="progress" value="0" max="100"></progress><div id="status">검사를 시작하세요. BTC 시장 맥락은 날짜당 한 번만 요청합니다.</div>\n</section>\n<section class="panel"><div class="kpis"><div class="kpi">검사 완료<b id="done">—</b></div><div class="kpi">API 오류<b id="errors">—</b></div><div class="kpi">경과 시간<b id="elapsed">—</b></div></div></section>\n<section class="panel"><h2>전략별 결과</h2><div class="scroll"><table><thead><tr><th>전략</th><th>진입</th><th>결과확인</th><th>수익</th><th>손실</th><th>미확인</th><th>수익거래율</th><th>평균 순수익률</th></tr></thead><tbody id="summary"><tr><td colspan="8">검사 전</td></tr></tbody></table></div></section>\n<section class="panel" id="core10-panel">\n<h2>CORE.10 · 분봉 × 단순이동평균 6조합 독립 비교</h2>\n<div class="muted"><b>5분봉 / 10분봉 × SMA3·5·10</b>의 직전 종가 아래→현재 종가 위 상향 교차를 검출하고, 볼먼 가격 압축·박스 상단 돌파 A와 강한 봉 선별 B(벡스)를 각각 나란히 비교해. <b>B는 A의 부분집합</b>이므로 거래 수를 합산하지 마.\n10분봉은 5분봉 2개가 모두 있는 경우만 결합하며, <b>10분봉 신호 완성 후 다음 5분봉 시가</b>에 진입해. 누락된 봉은 복원하지 않아.</div>\n<div class="muted"><b>압축 박스:</b> 5분봉은 직전 6봉(30분), 10분봉은 직전 3봉(30분). <b>EMA 추세 기준:</b> 5분봉 EMA20, 10분봉 EMA10(모두 약 100분에 해당하지만 가격 표본이 달라 완전히 동등하지 않아). <b>익절 +1.5%, 손절 -1%, 최대 60분</b>, 기존 비용과 동일. 조합별 신호 재발생은 최소 60분 간격으로 제한해.\n현재 시점 거래대금으로 종목을 선정하는 기존 편향과 날짜별 데이터 확보 차이가 있으니 <b>조합의 수익성이 입증된 것으로 보지 마.</b></div>\n<div class="scroll"><table><thead><tr><th>진입 유형</th><th>분봉</th><th>이평</th><th>이평 시간</th><th>신호</th><th>청산 완료</th><th>수익</th><th>손실</th><th>미확인</th><th>승률</th><th>평균 순수익률</th><th>플러스 날짜</th></tr></thead><tbody id="core10_summary"><tr><td colspan="12">검사 전</td></tr></tbody></table></div>\n<div class="muted" id="core10_coverage">검사 전 · 참고: 5분봉 5이평 A는 기존 볼먼 A와 동일한 신호·청산 결과가 나와야 해.</div>\n</section>\n<section class="panel"><h2>가상 필터: 거래량 1.2배</h2>\n<div class="muted">A_V / B_V / K_V는 기존 A / B / K 진입 중, 신호봉 거래량이 직전 20개 연속된 5분봉 평균의 1.2배 이상인 거래만 선택한 가상 비교야. 기존 BUY를 바꾸지 않아. 제외된 거래에서도 원래 결과를 유지하여 피한 손실/놓친 수익을 함께 계산해.</div>\n<div class="scroll"><table><thead><tr><th>가상 전략</th><th>통과</th><th>결과확인</th><th>수익</th><th>손실</th><th>평균 순수익률</th><th>피한 손실</th><th>놓친 수익</th><th>판정 불가</th></tr></thead><tbody id="shadow_summary"><tr><td colspan="9">검사 전</td></tr></tbody></table></div></section>\n<section class="panel"><h2>CORE.3 · EMA/ATR 가상 필터 실험</h2>\n<div class="muted">A_E · B_E: 신호 당시 EMA20의 3봉 기울기 &gt; 0%. K_ATR: 신호 당시 ATR(14) / 종가 &lt; 0.5%. 2026-11-05~13 자료를 살펴보고 선정한 조건이라 <b>그 기간 결과는 성능 검증으로 간주하지 않아.</b> CORE.3 검증에 10월 25~31일을 사용했고, CORE.4는 10월 17~23일과 10월 11~16일도 분석했어. 본래 A/B/K 진입·청산 결과는 변경되지 않아.</div>\n<div class="scroll"><table><thead><tr><th>가상 전략</th><th>통과</th><th>결과확인</th><th>수익</th><th>손실</th><th>수익거래율</th><th>평균 순수익률</th><th>피한 손실</th><th>놓친 수익</th><th>판정 불가</th></tr></thead><tbody id="trend_shadow_summary"><tr><td colspan="10">검사 전</td></tr></tbody></table></div>\n</section>\n<section class="panel"><h2>CORE.4 · 돌파봉 품질 (EMA 통과 후 추가 선별)</h2>\n<div class="muted">A_EQ: A_E 통과 + 양봉 몸통 ≥50%, 윗꼬리 ≤20%, 직전 6봉 박스 상단 대비 종가 돌파 폭 ≥0.05%. B_EQ: B_E 통과 + 몸통 ≥70%, 윗꼬리 ≤10%, 돌파 폭 ≥0.10%. 몸통·윗꼬리는 <b>돌파봉 전체 고저 범위 대비 비율</b>이며, 돌파 폭은 <b>박스 상단 대비 종가의 %</b>야. 수치는 단순 연구 가설이고 최적화된 값이 아니야.</div>\n<div class="scroll"><table><thead><tr><th>품질 필터</th><th>EMA 통과</th><th>Q 통과</th><th>청산 완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th><th>추가 피한 손실</th><th>추가 놓친 수익</th><th>판정 불가</th></tr></thead><tbody id="quality_summary"><tr><td colspan="11">검사 전</td></tr></tbody></table></div>\n<h3>돌파봉 세부 조건별 탐색 결과</h3>\n<div class="muted">각 조건 <b>한 가지씩</b> 적용할 때의 결과를 보여줘. 세 조건 전체 조합인 A_EQ/B_EQ와 다르며, 다른 두 조건과 독립적인 인과효과를 뜻하지는 않아. 분석 대상은 기존 EMA 필터를 통과한 완료 거래뿐이야.</div>\n<div class="scroll"><table><thead><tr><th>기준 전략</th><th>독립 조건</th><th>조건 통과 완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th><th>제외된 수익</th><th>제외된 손실</th></tr></thead><tbody id="quality_diagnostics"><tr><td colspan="9">검사 전</td></tr></tbody></table></div>\n<div class="muted">⚠️ CORE.4 연구에 10월 11~23일 결과가 이미 쓰였으므로, 그 기간은 신규 CORE.5 조건의 독립 검증에 재사용하지 않아. 화면 통계는 거래당 평균으로, 누적 수익률이나 실거래 성과가 아니야.</div>\n</section>\n<section class="panel"><h2>CORE.5 · 볼먼 돌파 폭 / 벡스 몸통 제한 분리 실험</h2>\n<div class="muted"><b>A_EM</b> = A_E 통과 + 돌파 폭 ≥0.05% <b>단독</b> 적용 (기존 A_EQ 몸통 50%·윗꼬리 20% 추가 제한 해제).<br>\n<b>B_ENB</b> = B_E 통과 + 윗꼬리 ≤10% + 돌파 폭 ≥0.10% (기존 B_EQ의 <b>추가 몸통 ≥70% 제한만 제거</b>).<br>\n<b>B_EM</b> = B_E 통과 + 돌파 폭 ≥0.10% <b>단독</b> 적용. B는 원래 벡스 몸통 ≥40% 등 기본 조건을 그대로 사용해.<br>\n검증 화면은 원래 EMA 통과 거래와 비교해 <b>추가로 피한 손실/놓친 수익</b>을 표시하고, 같은 가상 진입의 순수익률을 재사용해. 실전 BUY는 변경하지 않아.</div>\n<div class="scroll"><table><thead><tr><th>비교 필터</th><th>EMA 통과</th><th>추가 통과</th><th>청산 완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th><th>추가 피한 손실</th><th>추가 놓친 수익</th><th>판정 불가</th></tr></thead><tbody id="core5_summary"><tr><td colspan="11">검사 전</td></tr></tbody></table></div>\n<div class="muted">⚠️ 이번 조건은 10월 11~23일의 손익을 확인한 뒤 선정했어. <b>2026년 10월 3~9일</b>처럼 아직 조건 선택에 사용하지 않은 날짜로 따로 검증해. 표본이 적거나 평균이 마이너스면 실전 적용하지 말자.</div>\n</section>\n<section class="panel"><h2>CORE.6 · 볼먼 국면 분리 / 벡스 윗꼬리 단독 실험</h2>\n<div class="muted"><b>A_EQ_TR</b> = 기존 A_EQ 통과 + 해당 <b>종목의 5분봉 SMA50</b> 상승(3봉)·종가 SMA50 상회. <b>A_EQ_ATR</b> = 기존 A_EQ 통과 + ATR(14)/종가 &lt;0.5%. <b>A_EQ_R</b> = 두 조건 동시 통과. <b>B_EW</b> = 기존 B_E 통과 + 윗꼬리 비율 ≤10% <b>단독</b> 적용.<br>\n⚠️ SMA50은 <b>각 코인 자체의 추세</b>를 설명하며 전체 KRW 시장의 상승·하락을 뜻하지 않아. 이 분석을 위해 BTC 등 별도 종목의 API를 호출하지 않으므로 기존 속도에 영향이 작아. 기존 B_EM(돌파 폭만), B_ENB(윗꼬리+돌파 폭)와 B_EW를 나란히 비교해. 조건은 수익성 검증 전인 가설이고 실전 BUY에는 반영하지 않았어.</div>\n<div class="scroll"><table><thead><tr><th>신규 가상 전략</th><th>부모 필터 통과</th><th>추가 통과</th><th>청산 완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th><th>추가 피한 손실</th><th>추가 놓친 수익</th><th>판정 불가</th></tr></thead><tbody id="core6_summary"><tr><td colspan="11">검사 전</td></tr></tbody></table></div>\n<h3>볼먼 A_EQ · 종목별 추세와 변동성에 따른 결과</h3>\n<div class="muted">추세·변동성은 신호봉 시점의 완료된 과거 5분봉으로만 계산. 표에는 <b>A_EQ를 이미 통과한 완료 거래만</b> 표시하며, 상·하 조건을 가로질러 해석할 때 표본 수를 확인해. 추세 50봉이 없으면 UNKNOWN으로 남겨.</div>\n<div class="scroll"><table><thead><tr><th>종목별 조건</th><th>완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th></tr></thead><tbody id="core6_regime"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<div class="muted">⚠️ CORE.6의 신규 기준을 정할 때 10월 3~31일 데이터를 참고했어. <b>2026년 9월 26일~10월 2일</b> 등 아직 조건 선정에 쓰이지 않은 기간에서 검증하자. 가상 수익률은 동일 거래 중 필터 통과 거래의 평균이지 실제 투자 수익을 뜻하지 않아.</div>\n</section>\n<section class="panel"><h2>CORE.7 · BTC 1시간 국면과 진입 결과</h2>\n<div class="muted">각 코인 신호가 발생한 <b>같은 5분봉의 BTC 완료 종가</b>와 정확히 60분 전 BTC 종가를 비교해.\nBTC 1시간 변화율 <b>+0.30% 이상은 상승</b>, <b>-0.30% 이하는 하락</b>, 그 사이는 횡보로 분류해.\nBTC는 전체 KRW 시장의 대리 지표일 뿐이며, 본 분류는 아직 검증되지 않은 연구 가설이야.\n누락된 봉은 보간하지 않고 UNKNOWN으로 분리해. BTC 참조 데이터는 날짜당 한 번만 서버에서 조회하며 10분 재사용해.</div>\n<div id="btc_status" class="muted">BTC 데이터 조회 전</div>\n<h3>기준 전략의 BTC 국면별 성적</h3>\n<div class="scroll"><table><thead><tr><th>기준 전략</th><th>BTC 국면</th><th>완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th></tr></thead><tbody id="core7_regimes"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<h3>가상 BTC 상승 국면 필터 — 실전 BUY 미적용</h3>\n<div class="muted">A_EQ_BTC_UP / B_E_BTC_UP / B_EW_BTC_UP은 각 부모 전략을 통과한 신호 중 BTC 1시간 상승만 남긴 실험이야.\n제외 거래의 원래 손익을 보존하므로 피한 손실과 놓친 수익을 함께 계산할 수 있어. BTC 정보가 없으면 통과시키지 않고 판정 불가로 분리해.</div>\n<div class="scroll"><table><thead><tr><th>가상 전략</th><th>부모 통과</th><th>BTC상승 통과</th><th>완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th><th>피한 손실</th><th>놓친 수익</th><th>판정 불가</th></tr></thead><tbody id="core7_summary"><tr><td colspan="11">검사 전</td></tr></tbody></table></div>\n<div class="muted">주의: BTC 상승 상태가 수익의 원인이라고 단정할 수 없어. A와 B는 같은 볼먼 신호를 공유하며, 서로 독립 거래로 합산하면 안 돼. 검사 당시 상위 거래대금 종목은 과거 날짜의 상위 종목과 다를 수 있어.</div>\n</section>\n<section class="panel"><h2>CORE.8 · BTC ATR 변동성 × 코인 돌파 폭 교차 분석</h2>\n<div class="muted">기존 <b>A_EQ(볼먼)</b>와 <b>B_E(벡스)</b>에서 부모 필터를 통과한 신호만 비교해.\n<b>BTC ATR(14)/BTC 종가</b>는 해당 신호봉과 직전 연속 14개 5분봉에서 계산한 값이며,\n<b>돌파 폭</b>은 같은 코인 신호봉의 종가가 직전 6봉 박스 상단보다 높은 정도(%)야.\n둘 다 <b>매수 신호가 완성된 시점까지</b>의 값만 사용하고 미래 봉을 읽지 않아.</div>\n<div class="muted">BTC ATR 구간: <b>낮음 &lt;0.15%</b> / <b>중간 0.15~0.25%</b> / <b>높음 ≥0.25%</b>.\n돌파 폭 구간: <b>작음 &lt;0.10%</b> / <b>중간 0.10~0.30%</b> / <b>큼 ≥0.30%</b>.\n이 경계는 2026년 11월 데이터 탐색을 참고한 <b>사후 연구 기준</b>이므로 같은 기간의 좋은 결과를 독립적인 수익성 증거로 취급하지 않아.\n한 코인의 신호는 각 전략에서 한 칸에만 들어가지만, A와 B가 같은 원본 돌파 신호를 공유할 수 있으므로 합산해서 별도 거래로 해석하면 안 돼.</div>\n<h3>국면별 거래당 평균 순수익률 — 3×3 교차표</h3>\n<div class="scroll"><table><thead><tr><th>기준 전략</th><th>BTC ATR</th><th>돌파 폭</th><th>완료 거래</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th></tr></thead>\n<tbody id="core8_matrix"><tr><td colspan="8">검사 전</td></tr></tbody></table></div>\n<h3>연구용 가상 필터 — 수익 누락·손실 회피 함께 기록</h3>\n<div class="muted"><b>BTC_ATR25</b>: BTC ATR &lt;0.25%만 적용. <b>BTC_ATR25_M10</b>: BTC ATR &lt;0.25%이면서 코인 돌파 폭 0.10% 이상.\n이는 같은 기존 진입 시각을 사후에 분류한 <b>가상 비교</b>이며 매매 신호나 실전 BUY를 변경하지 않아.\n진단값이 없으면 제외(REJECT)로 처리하지 않고 <b>판정 불가</b>로 따로 기록해.</div>\n<div class="scroll"><table><thead><tr><th>신규 비교 필터</th><th>부모 통과</th><th>조건 통과</th><th>청산 완료</th><th>수익</th><th>손실</th><th>승률</th><th>평균 순수익률</th><th>피한 손실</th><th>놓친 수익</th><th>판정 불가</th><th>플러스 날짜</th></tr></thead>\n<tbody id="core8_summary"><tr><td colspan="12">검사 전</td></tr></tbody></table></div>\n<div class="muted">데이터 확인: 이번 연구는 이미 살펴본 11월 1~7일, 13~14일 BTC ATR 자료를 탐색에 사용했어.\n따라서 실제 개선 여부는 이후 새로운 신호나 다른 날짜에서 추가 검증해야 해. 검사 당시 거래대금 상위 목록은 현재 조회 시점 기준이라 과거 전체 시장의 완전한 재현은 아니야.</div>\n</section>\n\n<section class="panel" id="core20-panel">\n<h2>🔎 CORE.20 · 거래 활발도별 분봉 선택 + 하락 후 반등·추세 재판정</h2>\n<div class="muted"><b>분류 연구 전용 · 기존 CORE.19 BE/고정손절/ATR 결과와 실전 BUY는 변경 없음.</b> 진입 후 첫 <b>-0.5% 하락(완성 5분봉 종가 기준)</b>에서 재판정. 포지션이 이미 기존 BE 규칙으로 청산됐다면 추가 관찰을 시작하지 않아. 60분간 관측한 5분봉 개수와 추정 거래대금(종가 × 거래량)을 기준으로 활발=3+5분, 보통=5+10분, 적음=10+15분을 선택하고, 거래 부족·빈 봉·미완성 봉은 판정 보류해.</div>\n<div class="muted"><b>가설적 기준:</b> 최근 12개 예정 5분봉 중 11개 이상 + 평균 500만원/5분 → 3+5분, 10개 이상 + 평균 100만원/5분 → 5+10분, 8개 이상 + 평균 25만원/5분 → 10+15분. 기준을 충족하지 못하면 유동성 부족. <b>3분봉은 거래 활발 + 하락 트리거 발생 시에만 별도 조회</b>해 속도 부담을 줄였어. 미래 15·30·60분 가격은 오직 사후 평가에만 사용해.</div>\n<div class="scroll"><table><thead><tr><th>기존 진입 신호</th><th>-0.5% 하락 재판정</th><th>하락 조건 없음</th><th>관찰 불가</th><th>유동성 부족</th><th>3분봉 사용 대상</th></tr></thead><tbody id="core20_coverage"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<h3>① 재판정 시점의 분류와 실제 60분 후 결과</h3>\n<div class="muted">15·30·60분 가격 변화는 <b>트리거 시점 종가 대비 수수료 미포함 단순 가격 변화율</b>이야. 기존 BE 순수익률과 혼동하면 안 돼. 60분 연속 데이터가 확보된 표본만 결과 평균을 산출해.</div>\n<div class="scroll"><table><thead><tr><th>재판정 분류</th><th>전체</th><th>60분 결과 확인</th><th>60분 가격 상승</th><th>60분 +0.5%↑</th><th>60분 평균 변화</th><th>60분 최악 저점 평균</th></tr></thead><tbody id="core20_summary"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<h3>② 유동성별 선택 분봉 및 분류</h3>\n<div class="scroll"><table><thead><tr><th>거래활발도</th><th>선택 분봉</th><th>신호 수</th><th>반등 확인</th><th>반등 관찰</th><th>하락 지속</th><th>보류/불명</th></tr></thead><tbody id="core20_liquidity"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<h3>③ 실제 거래별 판정 · 재확인 · 미래 가격 변화</h3>\n<div class="scroll"><table><thead><tr><th>종목/진입 시각</th><th>하락 인식</th><th>하락폭</th><th>거래활발도</th><th>분봉</th><th>첫 판정</th><th>15분 재판정</th><th>30분 재판정</th><th>15분 가격</th><th>30분 가격</th><th>60분 가격</th><th>기존 BE 순수익률</th></tr></thead><tbody id="core20_details"><tr><td colspan="12">검사 전</td></tr></tbody></table></div>\n<div class="muted">주의: 3분봉/5분봉/10분봉/15분봉의 신호는 예측 확률이 아닌 규칙 기반 분류야. OHLC 봉 내 선후는 알 수 없고, 낮은 거래대금에서는 미끄러짐이 커질 수 있어. 데이터가 비면 임의로 봉을 생성하지 않아. 복수 테스트 날짜에서 오탐·미탐을 검증한 뒤에만 청산 실험으로 확장해.</div>\n</section>\n\n<section class="panel" id="core19-panel">\n<h2>🛡️ CORE.19 · ATR(14) 변동성 손절 × 4종 vs 기존 6종</h2>\n<div class="muted"><b>검증실 전용 · 기존 BUY / 주문 변경 없음.</b> 기존 CORE.18 결과 6종을 그대로 보존하면서 ATR(14) 10분봉 × 1·1.5·2·2.5배를 추가해 <b>총 10개 청산 방식</b>을 같은 B 10분 SMA3 신호로 비교해. 신호가 확정된 10분봉까지의 완성 데이터로 ATR을 계산하고, 진입 이후 ATR은 갱신하지 않아. 첫 익절 전에만 손절 적용 → +0.5% 순익 30%, +5% 30%, +10% 40% 익절, 그 다음 봉부터 비용 반영 본절 스탑.</div>\n<div class="muted">ATR 손절의 <b>가격 기준 폭 = min(3%, max(1%, ATR(14)÷진입가×배수×100))</b>. 이전에 정한 최대 -3% 범위를 유지하고 너무 짧은 손절은 -1%로 제한했어. 손절폭은 <b>왕복 수수료 제외 가격 기준</b>이므로 실제 순손실은 더 클 수 있고, 갭 시 더 불리할 수 있어. 봉에서 익절과 손절이 동시에 닿으면 손절 우선 처리하고 모호성을 기록해.</div>\n<div class="scroll"><table><thead><tr><th>전체 신호</th><th>ATR 계산 가능</th><th>ATR + 240분 완전 경로</th><th>10방식 공통 표본</th><th>ATR 계산 불가</th><th>조기 확정 존재</th></tr></thead><tbody id="core19_coverage"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<h3>① 동일 신호 · 동일 완전 경로 · 10방식 순수익률 비교</h3>\n<div class="scroll"><table><thead><tr><th>방식</th><th>비교 건</th><th>수익 거래</th><th>손절 건</th><th>첫 익절</th><th>본절 종료</th><th>평균 순수익률</th><th>최악 순수익률</th><th>손절 없음 대비 평균 차이</th><th>개선</th><th>악화</th><th>양수→음수 반전</th><th>실제 손절폭 범위</th><th>최대폭 상한 적용</th><th>봉 내 순서 불명</th></tr></thead><tbody id="core19_paired"><tr><td colspan="15">검사 전</td></tr></tbody></table></div>\n<h3>② 전체 신호 결과확정 / 미확인 현황 (표본 다름 · 직접 순위 비교 금지)</h3>\n<div class="scroll"><table><thead><tr><th>방식</th><th>전체</th><th>확정</th><th>미확인</th><th>손절 확정</th><th>확정분 평균</th></tr></thead><tbody id="core19_all"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<div class="muted">ATR 계산 불가면 다른 손절 방식으로 대신하지 않고 UNKNOWN 처리해. CORE.19은 기존 CORE.18의 6가지 결과를 건드리지 않아. 이미 살펴본 날짜에서 좋은 설정이 나오더라도 과최적화 가능성이 있으므로 다른 날짜의 독립 검증이 필요해. 실거래 수익 보장 아님.</div>\n</section>\n<section class="panel" id="core18-panel">\n<h2>🧪 CORE.18 · 손절 없음부터 -3%까지 6가지 폭 비교</h2>\n<div class="muted"><b>검증실 전용 · 실전 BUY / 주문 변경 없음.</b> 동일한 벡스 B 10분봉 × SMA3 신호, 다음 5분봉 시가, 최대 240분 관측. 익절은 매수 원물량 기준 순수익 +0.5%에 30%, +5%에 30%, +10%에 40%로 고정. <b>첫 익절 전</b>에만 진입가격 대비 가격 손절을 사용하고, 첫 익절 후에는 손절폭 대신 비용 반영 본절 스탑(다음 5분봉부터)을 사용해. 수수료·슬리피지는 상단 설정을 그대로 사용해.</div>\n<div class="muted">⚠️ -1%/-1.5%/-2%/-2.5%/-3%는 <b>가격 기준 손절폭</b>이며 수수료 포함 최종 순손실률과 달라. 같은 5분봉에서 손절 및 첫 익절가에 함께 닿으면 <b>손절을 먼저</b> 가정하고 모호성 건수에 기록해. 갭 발생 시 손절가보다 낮은 시가로 청산 가능. 없는 봉을 보충하지 않고, 본절·상위 익절이 같은 봉에서 닿을 때도 본절 우선 가정이야.</div>\n<div class="scroll"><table><thead><tr><th>전체 진입 신호</th><th>240분 연속 경로</th><th>6방식 같은 표본</th><th>경로 미확인</th><th>조기 확정 존재</th></tr></thead><tbody id="core18_coverage"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<h3>① 동일한 240분 경로 — 손절폭 6가지 공정 비교</h3>\n<div class="scroll"><table><thead><tr><th>초기 가격 손절</th><th>동일 거래</th><th>순이익</th><th>+0.5% 이상</th><th>손절 확정</th><th>첫 익절</th><th>본절 청산</th><th>시간 종료</th><th>평균 순수익률</th><th>최악 순수익률</th><th>무손절 대비 개선</th><th>무손절 대비 악화</th><th>SL·TP 동일 봉</th></tr></thead><tbody id="core18_paired"><tr><td colspan="13">검사 전</td></tr></tbody></table></div>\n<h3>② 240분 미완성 경로까지 포함한 확정 결과 (표본 차이 — 직접 성과 비교 금지)</h3>\n<div class="scroll"><table><thead><tr><th>초기 가격 손절</th><th>총 신호</th><th>결과 확정</th><th>미확인</th><th>손절 확정</th><th>평균 순수익률(확정분만)</th></tr></thead><tbody id="core18_all"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<div class="muted">핵심 판정은 위 <b>① 동일 표본</b>으로 해. 과거 연구일과 현재 거래대금으로 선정된 종목에 편향이 있으므로 승자 설정을 실전용으로 확정하지 말고 새로운 날짜에서도 재검증해야 해.</div>\n</section>\n<section class="panel" id="core17-panel">\n<h2>🛡️ CORE.17 · 새 F형: 첫 익절 전 -1% 손절 + 익절 후 본절</h2>\n<div class="muted"><b>검증실 전용 · 실전 주문 실행 없음.</b> 기존 A~E를 그대로 두고 <b>F형</b>만 추가했어. F형은 <b>+0.5% 순수익 목표 도달 전에만</b> 매수가 대비 가격 -1.0%에서 100% 손절하고, <b>첫 30% 익절 뒤에는 -1% 손절을 해제</b>하고 다음 5분봉부터 남은 물량에 비용 반영 본절 스탑을 적용해. 익절은 +0.5%에서 최초 물량 30%, +5%에서 30%, +10%에서 남은 40%야. 240분 지나면 잔량 청산.</div>\n<div class="muted">⚠️ 수수료가 편도 0.25%, 슬리피지가 0%라면 <b>가격 -1% 손절의 실제 순손실은 약 -1.49%</b>야. 첫 익절 전에 동일 5분봉에서 -1% 손절과 +0.5% 익절에 모두 닿으면 <b>보수적으로 손절 우선</b>, 순서 불명 표시. 갭 하락은 손절가보다 불리한 시가에 청산할 수 있어. 본절 스탑은 첫 익절 다음 봉부터 작동해. 목표 도달 및 체결은 가상 OHLC 기준이야.</div>\n<div class="scroll"><table><thead><tr><th>전체 신호</th><th>240분 연속 경로</th><th>미확인 경로</th><th>F형 조기 확정</th><th>F형 -1% 손절 전체 확정</th></tr></thead><tbody id="core17_coverage"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<h3>① 동일한 240분 연속 경로 · A~F 6방식 공정 비교</h3>\n<div class="scroll"><table><thead><tr><th>전략</th><th>비교 수</th><th>순이익 거래</th><th>+0.5% 이상</th><th>첫 익절</th><th>+5% 익절</th><th>+10% 익절</th><th>초기 -1% 손절</th><th>본절 스탑</th><th>시간 종료</th><th>평균 순수익률</th><th>최악 순수익률</th><th>봉 내 순서 불명</th></tr></thead><tbody id="core17_paired"><tr><td colspan="13">검사 전</td></tr></tbody></table></div>\n<h3>② F형 -1% 손절: 막은 손실 vs 놓친 반등 (E형 대비)</h3>\n<div class="scroll"><table><thead><tr><th>F형 손절</th><th>E형 대비 개선</th><th>E형 대비 악화</th><th>손절 없었으면 첫 익절</th><th>익절·손절 동일봉 불명</th></tr></thead><tbody id="core17_stop_impact"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<h3>③ 중간 데이터 공백 발생 전 F형 청산이 확정된 추가 거래 (다른 표본)</h3>\n<div class="scroll"><table><thead><tr><th>전체 신호</th><th>F형 결과 확정</th><th>미확인</th><th>F형 +수익</th><th>F형 -1% 손절</th><th>F형 평균 순수익률 (확정분만)</th></tr></thead><tbody id="core17_all"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<div class="muted">핵심 비교는 <b>F와 E (초기 손절만 차이)</b>야. 첫 익절 전 손절이 큰 손실을 방지하는지, 반대로 나중에 반등해 +0.5% 목표를 달성할 코인을 놓치는지 본다. 기존 11월 8~15일 자료는 이미 연구에 사용한 탐색 구간이므로 신규 기간에서도 검증해야 해.</div>\n</section>\n<section class="panel" id="core16-panel">\n<h2>🎯 CORE.16 · +0.5% 30% → +5% 30% → +10% 40% 전량 익절</h2>\n<div class="muted"><b>연구 전용 · 주문 실행 없음.</b> 동일한 벡스 10분봉 × SMA3 매수 신호에서 5가지 청산 규칙을 비교해. 새 전략의 <b>30%·30%·40%는 최초 매수 물량 기준</b>이야. 모든 목표는 <b>편도 수수료·슬리피지 왕복 반영 후 순수익률</b>이야.</div>\n<div class="muted"><b>A 기존</b> +0.5% 전량 익절 / <b>B 구 분할</b> +1% 50%·+5% 25%·+10% 25% / <b>C 구 분할 + 본절</b> / <b>D 새 분할</b> +0.5% 30%·+5% 30%·+10% 40% / <b>E 새 분할 + 본절</b>. 첫 익절 전 손절 없음, 이후 비용 반영 본절 스탑은 <b>다음 5분봉부터</b> 동작. 240분 이후 남은 물량은 종가 청산해.</div>\n<div class="muted">⚠️ 5분봉 안의 익절/본절 선후는 관측할 수 없어. 첫 익절 봉에서 본절까지 터치하면 순서 불명으로 표시해. 이후 봉에서 본절과 상위 익절이 동시에 닿으면 <b>보수적으로 본절 우선</b> 처리해. 본절 스탑은 첫 익절 이후 잔량만 청산하며, 실제 주문 체결은 보장되지 않아.</div>\n<div class="scroll"><table><thead><tr><th>전체 신호</th><th>240분 경로 완료</th><th>경로 미확인</th><th>공백 전 조기 전량 청산</th></tr></thead><tbody id="core16_coverage"><tr><td colspan="4">검사 전</td></tr></tbody></table></div>\n<h3>① 동일 240분 가격 경로 · 5가지 방식 공정 비교</h3>\n<div class="scroll"><table><thead><tr><th>전략</th><th>같은 거래</th><th>순이익</th><th>순수익 ≥+0.5%</th><th>첫 익절</th><th>+5% 익절</th><th>+10% 익절</th><th>본절 스탑</th><th>시간 종료</th><th>평균 순수익률</th><th>최악 순수익률</th><th>봉 내부 순서 불명</th></tr></thead><tbody id="core16_paired"><tr><td colspan="12">검사 전</td></tr></tbody></table></div>\n<h3>② 조기 전량 청산 포함 전체 확정 결과 · 표본 차이로 직접 비교 금지</h3>\n<div class="scroll"><table><thead><tr><th>전략</th><th>전체 신호</th><th>결과 확정</th><th>미확인</th><th>순이익 거래</th><th>평균 순수익률</th></tr></thead><tbody id="core16_all"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<div class="muted">기존 CORE.15·CORE.14·실전 V2 BUY는 보존. 과거 11월 8~15일은 이미 연구에 사용한 기간이므로 탐색용이며, 신규 기간에서 별도 검증해야 해. 특히 전체 240분 경로를 확보하지 못한 거래를 수익률 계산에 섞지 않아.</div>\n</section>\n\n<section class="panel" id="core15-panel">\n<h2>🎯 CORE.15 · +1% 절반 → +5% 남은 절반 → +10% 잔량 익절 · 본절 스탑</h2>\n<div class="muted"><b>연구 전용 · 실제 주문 없음.</b> 기존 벡스 10분봉 × SMA3 매수 신호를 그대로 쓰고, 같은 진입가·비용·240분 경로에서 세 청산 방식만 비교해.</div>\n<div class="muted"><b>A · 기존 기준</b>: 순수익 +0.5% 전량 즉시 익절(기존 CORE.13 A와 완전히 동일). <b>B · 분할 익절</b>: 매도 조각별 왕복 비용 차감 후 순수익 <b>+1%에 최초 물량 50%, +5%에 25%, +10%에 최종 25%</b> 익절. <b>C · 분할 + 본절 스탑</b>: B에 더해 첫 +1% 익절 뒤 남은 물량의 순손익이 본절에 닿으면 잔량 전량 청산. <b>첫 +1% 익절 전에는 손절 없음.</b> 240분이 되면 남은 물량을 종가에 청산해.</div>\n<div class="muted">본절은 <b>진입·청산 편도 비용까지 반영한 실질 순손익 0% 가격</b>이야. 5분봉에서 익절가와 본절가가 함께 터치하면 <b>보수적으로 본절 청산 우선</b>, 첫 +1% 익절 봉에서는 본절 스탑을 발동하지 않고 다음 5분봉부터 적용하며, 같은 봉에서 양쪽 가격이 닿으면 <b>봉 내부 순서 불확실</b>로 표시해. 실제 체결·대기 주문 성공을 보장하지 않아.</div>\n<div class="scroll"><table><thead><tr><th>전체 신호</th><th>240분 경로 완료</th><th>경로 미확인</th><th>미완성 경로의 조기 전량 청산</th></tr></thead><tbody id="core15_coverage"><tr><td colspan="4">검사 전</td></tr></tbody></table></div>\n<h3>① 동일한 240분 가격 경로 · A/B/C 공정 비교</h3>\n<div class="scroll"><table><thead><tr><th>매도 전략</th><th>같은 거래</th><th>순이익 건수</th><th>순수익 ≥ +0.5%</th><th>+1% 50% 익절</th><th>+5% 25% 익절</th><th>+10% 최종 익절</th><th>본절 스탑</th><th>240분 종료</th><th>평균 순수익률</th><th>최악 순수익률</th><th>동일 봉 순서 불명</th></tr></thead><tbody id="core15_paired"><tr><td colspan="12">검사 전</td></tr></tbody></table></div>\n<h3>② 240분 가격 경로 누락 이후의 청산 완료 결과 (표본이 달라 직접 성적 비교 금지)</h3>\n<div class="scroll"><table><thead><tr><th>매도 전략</th><th>전체 신호</th><th>결과 확정</th><th>미확인</th><th>순이익 건수</th><th>평균 순수익률</th></tr></thead><tbody id="core15_all"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<div class="muted">⚠️ 목표 +5/+10%는 240분 안에 드물 수 있고, <b>+1% 첫 익절 실패 거래에는 손절이 없어서 큰 손실 가능성</b>이 있어. 상승과 하락이 같은 5분봉에서 발생하면 체결 순서가 확정되지 않으므로 불확실 건수를 함께 보고 있어. 기존 CORE.14 및 V2 실전 BUY는 변경되지 않았어.</div>\n</section>\n\n<section class="panel" id="core14-panel">\n<h2>🔬 CORE.14 · 30·60분 초기 약세 진단 + ATR 손절 연구</h2>\n<div class="muted"><b>매수 신호는 기존 벡스 10분봉 × SMA3 그대로.</b> 전 방식 순수익 +0.5% 목표가 공통이며, 진입은 신호 다음 5분봉 시가. 기본 편도 수수료 0.04% + 슬리피지 0.05%를 양쪽에 반영한 연구용 가상 청산이야. <b>CORE.13 A형과 동일한 기준(A)을 그대로 보존해 비교</b>해.</div>\n<div class="muted"><b>A · 기준</b>: +0.5% 익절, 손절 없음, 최대 240분. <b>B · 30분 진단</b>: 목표 미도달 시 30분봉 종가의 순손익률이 -0.50% 이하라면 해당 종가에서 청산, 아니면 기준 규칙 유지. <b>C · 60분 진단</b>: 동일하게 60분 시점에만 진단. <b>D · ATR 손절</b>: 진입 전에 완료된 <b>10분봉 ATR14 ×1.5</b>를 진입가격에서 뺀 고정 손절가, 익절 +0.5%, 최대 240분. ATR 기록이 부족하면 D는 UNKNOWN. 이는 사전에 정한 탐색 가설이며 최적 기준이 아니야.</div>\n<div class="scroll"><table><thead><tr><th>전체 신호</th><th>240분 연속 경로</th><th>진입 시 ATR 산출</th><th>4방식 공통 비교 가능</th><th>경로 미확인</th></tr></thead><tbody id="core14_coverage"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<h3>① 동일한 240분 연속 경로: 기준·30분·60분 비교</h3>\n<div class="scroll"><table><thead><tr><th>청산 규칙</th><th>동일 거래</th><th>+0.5% 이상</th><th>실제 플러스</th><th>익절 TP</th><th>진단 청산</th><th>손절 SL</th><th>시간 종료</th><th>평균 순수익률</th><th>최악 수익률</th><th>조기 종료 후 놓친 TP</th></tr></thead><tbody id="core14_paired"><tr><td colspan="11">검사 전</td></tr></tbody></table></div>\n<h3>② 4가지 방식 공정 비교: ATR 산출 가능한 동일 경로만</h3>\n<div class="muted">ATR 자료가 없는 거래를 D형에서만 제외하고 A/B/C에는 넣으면 비교 표본이 달라져. 따라서 아래는 <b>전 방식 같은 신호</b>로만 비교하고, 위 표와 구분해.</div>\n<div class="scroll"><table><thead><tr><th>청산 규칙</th><th>동일 거래</th><th>+0.5% 이상</th><th>실제 플러스</th><th>익절 TP</th><th>진단 청산</th><th>손절 SL</th><th>시간 종료</th><th>평균 순수익률</th><th>최악 수익률</th><th>조기 종료 후 놓친 TP</th></tr></thead><tbody id="core14_paired_atr"><tr><td colspan="11">검사 전</td></tr></tbody></table></div>\n<h3>③ 연속 240분이 없어도 조기 확정된 청산 (통계 분리)</h3>\n<div class="scroll"><table><thead><tr><th>청산 규칙</th><th>전체 신호</th><th>결과 확인</th><th>미확인</th><th>+0.5% 이상</th><th>평균 순수익률</th></tr></thead><tbody id="core14_all"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<div class="muted">⚠️ 30·60분 진단은 <b>시점 직전까지 목표가에 터치하지 않은 거래</b>만 해당해. 동일 봉에서 TP 터치 후 종가가 악화되었으면 <b>TP 먼저</b> 처리해. ATR형에서 같은 5분봉 TP/SL 동시 터치는 <b>SL 먼저</b>, 손절 아래 갭 시 해당 봉 시가로 청산을 가정해. 5분봉 내부 순서는 알 수 없고, 실제 체결을 보장하지 않아. <b>현재까지 본 11월 8~15일은 탐색 자료</b>이며 실전 BUY·자동주문은 변경되지 않아.</div>\n</section>\n\n<section class="panel" id="core13-panel">\n<h2>🎯 CORE.13 · 순수익 +0.5% 즉시 익절 vs -1% 손절 vs 240분 보유</h2>\n<div class="muted"><b>진입 신호는 벡스 10분봉 × SMA3 고정.</b> 다음 5분봉 시가 진입, 수수료·슬리피지 편도 각 기본 0.04%·0.05% 적용. <b>익절 목표는 비용 차감 후 순수익 +0.50%에 해당하는 가격</b>이야. +0.50% 미만은 이전 연구에서처럼 <b>목표 미달(손절 판정)</b>으로 분류하지만 실제 손절 체결과 혼동하지 마.</div>\n<div class="scroll"><table><thead><tr><th>전체 신호</th><th>240분 전체 봉</th><th>미완성 경로</th><th>미완성인데 익절/손절 확정</th></tr></thead><tbody id="core13_coverage"><tr><td colspan="4">검사 전</td></tr></tbody></table></div>\n<h3>동일 240분 연속 경로로 3가지 청산 방식 공정 비교</h3>\n<div class="muted"><b>A</b>: 순수익 +0.5% 닿으면 즉시 익절, 손절 주문 없이 최대 240분. <b>B</b>: 동일 익절 및 진입가 대비 -1.0% 손절(갭 하락 시 시가 기준), 최대 240분. <b>C</b>: 중간 익절·손절 없이 240분 종가 청산. 동일 5분봉 안에서 목표가와 손절가 모두 터치하면 <b>B는 손절 우선</b>. 가격 안의 체결 순서를 알 수 없기 때문이야.</div>\n<div class="scroll"><table><thead><tr><th>청산 방식</th><th>동일 거래</th><th>순수익 ≥+0.5%</th><th>목표 미달</th><th>익절 TP</th><th>실제 손절 SL</th><th>시간 종료</th><th>수익 판정률</th><th>평균 순수익률</th></tr></thead><tbody id="core13_paired"><tr><td colspan="9">검사 전</td></tr></tbody></table></div>\n<h3>240분 봉이 부족해도 조기 확정된 TP/SL 건수</h3>\n<div class="muted">이 표는 위의 <b>동일 표본 비교와 분리</b>돼 있어. 예를 들어 30분에 TP가 확정되고 200분에 공백이 생기면 A/B는 확정할 수 있지만 C의 240분 종가 결과는 UNKNOWN이야. 없는 5분봉을 생성하지 않으므로 전략별 관측 수가 달라질 수 있어.</div>\n<div class="scroll"><table><thead><tr><th>청산 방식</th><th>전체 신호</th><th>조기·전체 결과 확인</th><th>미확인</th><th>수익 판정</th><th>손절 판정</th><th>평균 순수익률</th></tr></thead><tbody id="core13_all"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<div class="scroll"><table><thead><tr><th>동일 240분 경로의 선후관계</th><th>TP 먼저</th><th>SL 먼저</th><th>같은 봉 동시 터치</th><th>둘 다 미도달</th></tr></thead><tbody id="core13_order"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<div class="muted">⚠️ 현재 자료는 이전 연구에 사용한 날짜들이므로 수익성 확증이 아니라 <b>진입 이후 실제 청산 가능성을 비교하는 탐색</b>이야. 중간 봉의 가격 경로 순서는 정확히 알 수 없고 TP·SL 가격에서 체결된다는 보장은 없어. 어떤 전략도 기존 V2 실전 BUY·주문 기능을 변경하지 않아.</div>\n</section>\n\n<section class="panel" id="core12-panel">\n<h2>⏱ CORE.12 · 벡스 10분봉 × SMA3 — 240분까지 +0.5% 판정</h2>\n<div class="muted"><b>같은 매수 신호를 최대 240분까지 추적</b>하고, 각 시점(15·30·60·120·180·240분)에 바로 청산했다고 가정한 <b>왕복 비용 반영 순수익률</b>을 비교해.\n<b>순수익률 +0.50% 이상 → 수익</b>, <b>+0.50% 미만 → 손절 판정</b>이야. 사용자가 요청한 연구용 표기일 뿐, -0.5% 가격에 실제 손절했다는 뜻이 아니고 <b>+0.1%의 실제 플러스 수익도 여기서는 손절 판정으로 분류</b>돼.\n기존 +1.5% 익절 / -1.0% 손절 / 60분 결과는 CORE.11 아래 기존 표에 그대로 남아 있어.</div>\n<div class="scroll"><table><thead><tr><th>전체 신호</th><th>240분 경로 확보</th><th>미확인(부족/공백)</th><th>판정 기준</th></tr></thead><tbody id="core12_overview"><tr><td colspan="4">검사 전</td></tr></tbody></table></div>\n<h3>시간별 순수익 +0.5% 도달 여부 · 동일한 240분 연속봉 표본</h3>\n<div class="muted">시간별 값은 <b>해당 분의 5분봉 종가에 청산한다고 가정</b>한 순수익률이야. 실제로 TP/SL 주문을 걸어 자동 청산한 결과가 아니야. 마지막 열은 중간 고가가 +0.5% 순수익에 도달했는지만 따로 진단한 값이라, 그 시점의 실제 매도 체결을 뜻하지 않아.</div>\n<div class="scroll"><table><thead><tr><th>보유 시간</th><th>동일 비교 거래</th><th>수익 ≥+0.5%</th><th>손절 판정 &lt;+0.5%</th><th>수익 판정률</th><th>평균 순수익률</th><th>중간 +0.5% 터치</th></tr></thead><tbody id="core12_holds"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n<div class="muted">⚠️ <b>240분 연속봉이 부족한 거래는 모든 시간 비교에서 제외</b>하여 표본을 통일해. 파일의 UNKNOWN은 손절 판정으로 취급하지 않아. 신호가 나온 다음 봉 시가 진입·수수료 0.04% 및 슬리피지 0.05%(기본 편도) 조건을 유지해.</div>\n</section>\n<section class="panel" id="core11-panel">\n<h2>🎯 CORE.11 · 벡스 10분봉 × SMA3 단일 집중 연구</h2>\n<div class="muted"><b>매수 신호 고정:</b> CORE.10의 B_10M_MA03를 그대로 사용해. 신규 진입 필터는 없어. 돌파 폭·10분봉 거래량 배수·BTC 1시간 국면은 <b>신호봉이 모두 완성된 당시 값</b>으로만 구분해. BTC 맥락은 기존 날짜별 1회 조회를 재사용해. 아래 분류는 연구용 관찰 결과이며 다음 날 성공 여부를 예측하는 신호가 아니야.</div>\n<div class="scroll"><table><thead><tr><th>전체 신호</th><th>기존 청산 완료</th><th>기존 수익</th><th>기존 손실</th><th>기존 평균 순수익률</th><th>연속 60분 경로</th><th>경로 평가 불가</th><th>BTC 맥락 불가</th><th>거래량 비율 불가</th></tr></thead><tbody id="core11_overview"><tr><td colspan="9">검사 전</td></tr></tbody></table></div>\n<h3>진입 당시 조건별 비교 · 동일 60분 연속봉 표본</h3>\n<div class="muted">세 분류표는 <b>서로 다른 필터가 아니라 같은 매수 거래를 서로 다른 관점에서 나눈 것</b>이야. 미확인값은 UNKNOWN으로 분리해. 표본이 적거나 하루 성적이 좋은 구간을 바로 BUY 조건으로 채택하지 않아.</div>\n<div class="scroll"><table><thead><tr><th>진단</th><th>구간</th><th>완료 거래</th><th>수익 / 손실</th><th>승률</th><th>평균 순수익률</th></tr></thead><tbody id="core11_diagnostics"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<h3>보유 시간만 15 / 30 / 60분 비교</h3>\n<div class="muted">세 조합 모두 <b>동일한 신호·다음 5분봉 시가 진입·익절 +1.5%·손절 -1.0%·거래 비용</b>을 유지해. <b>신호 후 연속 60분 봉이 확보된 동일 거래</b>에서만 비교하므로 기간마다 거래수가 달라지는 표본 편향을 줄였어. 동일 5분봉에서 익절·손절 동시 도달은 기존처럼 손절 우선. 현실 주문 체결을 보장하는 수치는 아니야.</div>\n<div class="scroll"><table><thead><tr><th>최대 보유</th><th>비교 거래</th><th>수익 / 손실</th><th>승률</th><th>평균 순수익률</th><th>TP</th><th>SL</th><th>시간 종료</th></tr></thead><tbody id="core11_holds"><tr><td colspan="8">검사 전</td></tr></tbody></table></div>\n<div class="muted">⚠️ 지금까지 연구했던 11월 8~14일은 조건 탐색용이야. 가장 좋아 보이는 구간이나 보유 시간을 이 기간에 맞춰 채택하면 과적합 우려가 있어. 기준을 확정한 다음 새 날짜에서 검증해. 과거 종목 선정은 조회 시점의 최근 24시간 거래대금을 쓰는 한계도 유지돼.</div>\n</section>\n<section class="panel" id="core9-panel">\n<h2>CORE.9 · 진입 이후 60분 가격 경로 분석 (연구 전용)</h2>\n<div class="muted">대상은 <b>볼먼 A_EQ</b>와 <b>벡스 B_E</b> 필터를 통과한 기존 신호야. 기존 신호와 +1.5% 익절 / -1.0% 손절을 <b>절대 변경하지 않고</b> 별도 가격 경로를 관찰해.\n<b>MFE</b>는 다음 5분봉 시가 진입을 가정하고 이후 60분 동안의 최대 고가 기준 상승폭, <b>MAE</b>는 최저 저가 기준 하락폭이야.\n이 값은 <b>기존 익절·손절로 이미 청산된 이후의 움직임까지 포함</b>할 수 있으므로, 실제로 모두 누릴 수 있었던 수익을 뜻하지 않아.</div>\n<h3>가격 경로 진단</h3>\n<div class="scroll"><table><thead><tr><th>기준 전략</th><th>신호</th><th>60분 연속봉 확보</th><th>미확인</th><th>MFE 평균</th><th>MAE 평균</th><th>MFE ≥+0.5%</th><th>MFE ≥+1.0%</th><th>MFE ≥+1.5%</th><th>60분 종가 순수익 평균</th></tr></thead><tbody id="core9_paths"><tr><td colspan="10">검사 전</td></tr></tbody></table></div>\n<h3>익절·손절 조합 12개 비교 · 동일 신호·동일 60분 연속봉</h3>\n<div class="muted">익절 +0.50 / +0.75 / +1.00 / +1.50%, 손절 -0.50 / -0.75 / -1.00%를 교차 적용해. <b>진입 가격과 거래 비용은 CORE.8과 동일</b>하고, 60분 시간 종료도 유지해. 같은 5분봉에서 익절·손절이 동시에 닿으면 보수적으로 손절 우선, 손절가 아래로 갭 출발하면 그 시가로 계산해. <b>60분 경로가 완전히 확보된 거래만</b> 비교하므로 조합마다 표본이 달라지는 문제를 막아.</div>\n<div class="scroll"><table><thead><tr><th>기준 전략</th><th>익절</th><th>손절</th><th>완료</th><th>수익 / 손실</th><th>승률</th><th>평균 순수익률</th><th>TP / SL / 시간</th></tr></thead><tbody id="core9_grid"><tr><td colspan="8">검사 전</td></tr></tbody></table></div>\n<h3>진입 5분 지연 비교 · 기존 익절 +1.5% / 손절 -1.0%</h3>\n<div class="muted">기존 방식은 신호 직후 다음 봉 시가, 지연 방식은 <b>그보다 한 봉(5분) 뒤의 시가</b>에 가상 진입해. 지연된 진입도 새로 60분 보유하며, 추가 봉이 부족하면 판정 불가로 분리해. 지연 체결은 실전 주문 지연을 근사할 뿐 실제 체결가를 보장하지 않아.</div>\n<div class="scroll"><table><thead><tr><th>기준 전략</th><th>5분 지연 완료</th><th>지연 평가 불가</th><th>기존 평균(동일 표본)</th><th>지연 평균</th><th>변화</th></tr></thead><tbody id="core9_delay"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<div class="muted">⚠️ CORE.9의 여러 청산 조합은 <b>탐색용</b>이며 최고 수익률 조합을 골라 같은 날짜에서 재검증하면 과적합이 발생해. 5분봉은 봉 내부 가격 순서를 알 수 없으므로 실제 체결 결과와 다를 수 있어. 역사적 종목 선정은 현재 시점 24시간 거래대금 기준인 기존 한계도 유지돼.</div>\n</section>\n<section class="panel"><h2>거래 상세 (기존 전략, 최대 200건 표시)</h2><div class="scroll"><table><thead><tr><th>코인/전략</th><th>신호 시각</th><th>결과</th><th>순수익률</th></tr></thead><tbody id="details"><tr><td colspan="4">검사 전</td></tr></tbody></table></div></section>\n<section class="panel"><div class="muted">B는 A형 볼먼 신호를 걸러내는 부분집합이며 독립 전략이 아니야. K는 코너스의 일봉 원형이 아닌 5분봉 SMA50·RSI(2) 변형이야. A/B/K는 과거 완료봉에서만 신호를 판정하고 다음 실제 5분봉 시가에 가상 진입해. TP +1.5%, SL -1.0%, 최대 60분. 동일 봉 TP·SL 동시 도달 시 SL을 우선해. 없는 봉을 생성하지 않아. 실제 V2의 BUY·알림·기록·주문에는 영향을 주지 않아. CORE.4·CORE.5·CORE.6 필터는 기존 CORE.3 및 완료된 돌파봉의 진단값만 재사용해. 실전 BUY에는 적용하지 않았어. 가상 필터 수익률은 새로운 거래를 재진입한 결과가 아니라 같은 시점의 기존 거래에서 통과한 부분집합이야.</div></section>\n</main><script>\nconst $=id=>document.getElementById(id),esc=x=>String(x??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nlet responses=[],failures=[],started=0,btcContext=null,btcStatus=\'NOT_LOADED\';\nconst CORE7_DEFS=[[\'A_EQ_BTC_UP\',\'A_EQ\',\'shadow_breakout\',\'볼먼 A_EQ + BTC 상승\'],\n [\'B_E_BTC_UP\',\'B_E\',\'shadow_trend_atr\',\'벡스 B_E + BTC 상승\'],\n [\'B_EW_BTC_UP\',\'B_EW\',\'shadow_core6\',\'벡스 B_EW + BTC 상승\']];\nconst core7Done=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&e.net_pct!==undefined&&Number.isFinite(Number(e.net_pct));\nfunction core7Ctx(e){return btcContext?.[e.signal_time_kst]??null;}\nfunction core7Metrics(e){const c=core7Ctx(e);return {\n btc_regime:c?.btc_regime??\'UNKNOWN\',btc_ret60_pct:c?.btc_ret60_pct??null,\n btc_atr14_pct:c?.btc_atr14_pct??null,btc_regime_threshold_pct:0.30,\n btc_context_source:\'KRW-BTC 5m; current completed signal candle\'};}\nfunction deriveCore7(result){const out={};for(const [id,parent,key] of CORE7_DEFS){\n const rows=result[key]?.[parent]||[];\n out[id]=rows.map(old=>{const ctx=core7Metrics(old);const prev=old.filter_status;\n let status,reason;\n if(prev===\'REJECT\'||prev===\'BASE_REJECT\'){\n  status=\'BASE_REJECT\';reason=\'부모 필터에서 이미 제외\';\n }else if(prev!==\'PASS\'){\n  status=\'BASE_UNKNOWN\';reason=\'부모 필터 판정 불가\';\n }else if(ctx.btc_regime===\'UNKNOWN\'){\n  status=\'NOT_EVALUABLE\';reason=\'동시각 BTC 1시간 연속봉 부족\';\n }else if(ctx.btc_regime===\'BTC_UP\'){\n  status=\'PASS\';reason=\'BTC 1시간 상승 국면\';\n }else{status=\'REJECT\';reason=\'BTC 상승 국면 미충족\';}\n return {...old,...ctx,strategy:id,source_strategy:old.source_strategy||old.strategy,\n  comparison_base:parent,core7_rule_name:id,filter_status:status,\n  filter_pass:status===\'PASS\',filter_reason:reason};});\n }return out;}\n// CORE.8 reuses CORE.7\'s cached, same-time BTC candles and source outcomes.\n// Thresholds were chosen after November discovery; no claim of validation.\nconst CORE8_ATR_QUIET=.25, CORE8_ATR_MED=.35, CORE8_ATR_LOW=.15;\nconst CORE8_MARGIN_MID=.10, CORE8_MARGIN_HIGH=.30;\nconst CORE8_DEFS=[\n [\'A_EQ_BTC_ATR25\',\'A_EQ\',\'shadow_breakout\',\'볼먼 A_EQ · BTC ATR<0.25%\'],\n [\'A_EQ_BTC_ATR25_M10\',\'A_EQ\',\'shadow_breakout\',\'볼먼 A_EQ · BTC ATR<0.25% + 돌파≥0.10%\'],\n [\'B_E_BTC_ATR25\',\'B_E\',\'shadow_trend_atr\',\'벡스 B_E · BTC ATR<0.25%\'],\n [\'B_E_BTC_ATR25_M10\',\'B_E\',\'shadow_trend_atr\',\'벡스 B_E · BTC ATR<0.25% + 돌파≥0.10%\']\n];\nfunction core8Num(x){if(x===null||x===undefined||x===\'\')return null;\n const n=Number(x);return Number.isFinite(n)?n:null;}\nfunction core8AtrBand(v){if(v===null)return \'UNKNOWN\';\n return v<CORE8_ATR_LOW?\'LOW\':v<CORE8_ATR_QUIET?\'MID\':\'HIGH\';}\nfunction core8MarginBand(v){if(v===null)return \'UNKNOWN\';\n return v<CORE8_MARGIN_MID?\'SMALL\':v<CORE8_MARGIN_HIGH?\'MID\':\'LARGE\';}\nfunction core8Metrics(e){const btc=core7Metrics(e);\n const atr=core8Num(btc.btc_atr14_pct),margin=core8Num(e.break_margin_pct);\n return {...btc,core8_btc_atr_band:core8AtrBand(atr),\n core8_break_margin_band:core8MarginBand(margin),core8_btc_quiet_max_pct:CORE8_ATR_QUIET,\n core8_mid_margin_min_pct:CORE8_MARGIN_MID,core8_mid_margin_max_pct:CORE8_MARGIN_HIGH};}\nfunction deriveCore8(result){const out={};for(const [id,parent,key] of CORE8_DEFS){\n const joined=id.endsWith(\'_ATR25_M10\');\n out[id]=(result[key]?.[parent]||[]).map(old=>{\n const ctx=core8Metrics(old),prev=old.filter_status;\n const atr=core8Num(ctx.btc_atr14_pct),margin=core8Num(old.break_margin_pct);\n let status,reason;\n if(prev===\'REJECT\'||prev===\'BASE_REJECT\'){\n  status=\'BASE_REJECT\';reason=\'부모 필터에서 이미 제외됨\';\n }else if(prev!==\'PASS\'){\n  status=\'BASE_UNKNOWN\';reason=\'부모 필터 판정 불가\';\n }else if(atr===null||(joined&&margin===null)){\n  status=\'NOT_EVALUABLE\';reason=\'BTC 연속봉 또는 돌파 폭 진단값 없음\';\n }else if(atr<CORE8_ATR_QUIET&&(!joined||(margin>=CORE8_MARGIN_MID))){\n  status=\'PASS\';reason=\'BTC ATR<0.25%\'+(joined?\' + 중간 돌파 폭\':\'\');\n }else{status=\'REJECT\';reason=\'BTC 변동성 / 돌파 폭 조건 미충족\';}\n return {...old,...ctx,strategy:id,source_strategy:old.source_strategy||old.strategy,\n comparison_base:parent,core8_rule_name:id,filter_status:status,\n filter_pass:status===\'PASS\',filter_reason:reason};\n });}return out;}\nconst ymd=()=>{const parts=new Intl.DateTimeFormat(\'en-CA\',{timeZone:\'Asia/Seoul\',year:\'numeric\',month:\'2-digit\',day:\'2-digit\'}).formatToParts(new Date());const g=k=>parts.find(x=>x.type===k)?.value;return `${g(\'year\')}-${g(\'month\')}-${g(\'day\')}`};\nconst today=ymd(),dt=new Date(today+\'T00:00:00+09:00\');dt.setDate(dt.getDate()-1);const yesterday=new Intl.DateTimeFormat(\'en-CA\',{timeZone:\'Asia/Seoul\',year:\'numeric\',month:\'2-digit\',day:\'2-digit\'}).formatToParts(dt);const q=k=>yesterday.find(x=>x.type===k)?.value;\n$(\'day\').value=`${q(\'year\')}-${q(\'month\')}-${q(\'day\')}`;$(\'day\').max=$(\'day\').value;\nfunction stats(events){const v=events.filter(e=>e.entry_time_kst&&e.outcome!==\'NO_ENTRY\');const x=v.filter(e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&Number.isFinite(Number(e.net_pct)));const w=x.filter(e=>Number(e.net_pct)>0).length;return {entry:v.length,done:x.length,wins:w,loss:x.length-w,unknown:v.filter(e=>e.outcome===\'UNKNOWN\').length,avg:x.length?x.reduce((s,e)=>s+Number(e.net_pct),0)/x.length:NaN};}\nconst pct=x=>Number.isFinite(x)?x.toFixed(3)+\'%\':\'—\';\nfunction paint(){const arms=[[\'A\',\'볼먼 A\'],[\'B\',\'벡스 B (볼먼 필터)\'],[\'K\',\'코너스 K\']];const all=[];\n $(\'summary\').innerHTML=arms.map(([id,name])=>{const es=responses.flatMap(r=>r.events?.[id]||[]);all.push(...es.filter(e=>e.entry_time_kst&&e.outcome!==\'NO_ENTRY\'));const s=stats(es);return `<tr><td>${name}</td><td>${s.entry}</td><td>${s.done}</td><td class="positive">${s.wins}</td><td class="negative">${s.loss}</td><td>${s.unknown}</td><td>${s.done?(100*s.wins/s.done).toFixed(1)+\'%\':\'—\'}</td><td class="${s.avg>=0?\'positive\':\'negative\'}">${pct(s.avg)}</td></tr>`}).join(\'\');\n const sorted=all.sort((a,b)=>(a.signal_time_kst||\'\').localeCompare(b.signal_time_kst||\'\'));$(\'details\').innerHTML=sorted.slice(0,200).map(e=>`<tr><td>${esc(e.market)} / ${esc(e.strategy)}</td><td>${esc(e.signal_time_kst)}</td><td>${esc(e.outcome)}</td><td>${e.net_pct==null?\'—\':pct(Number(e.net_pct))}</td></tr>`).join(\'\')||\'<tr><td colspan="4">신호 없음</td></tr>\';\n $(\'done\').textContent=responses.length;$(\'errors\').textContent=failures.length;$(\'elapsed\').textContent=((performance.now()-started)/1000).toFixed(1)+\'초\';paintShadow();paintTrendShadow();paintBreakout();paintCore5();paintCore6();paintCore7();paintCore8();paintCore9();paintCore10();paintCore11();paintCore12();paintCore13();paintCore14();paintCore15();paintCore16();paintCore17();paintCore18();paintCore19();paintCore20();}\n\nfunction paintShadow(){\n const arms=[[\'A_V\',\'볼먼 A + 거래량\'],[\'B_V\',\'벡스 B + 거래량\'],[\'K_V\',\'코너스 K + 거래량\']];\n const complete=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&e.net_pct!==undefined&&Number.isFinite(Number(e.net_pct));\n $(\'shadow_summary\').innerHTML=arms.map(([id,name])=>{\n  const all=responses.flatMap(r=>r.shadow?.[id]||[]);\n  const passed=all.filter(e=>e.filter_status===\'PASS\');\n  const outcome=passed.filter(complete);\n  const wins=outcome.filter(e=>Number(e.net_pct)>0).length;\n  const dropped=all.filter(e=>e.filter_status===\'REJECT\'&&complete(e));\n  const avoided=dropped.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=dropped.filter(e=>Number(e.net_pct)>0).length;\n  const unknown=all.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  const avg=outcome.length?outcome.reduce((s,e)=>s+Number(e.net_pct),0)/outcome.length:NaN;\n  return `<tr><td>${name}</td><td>${passed.length}</td><td>${outcome.length}</td><td class="positive">${wins}</td><td class="negative">${outcome.length-wins}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td><td>${avoided}</td><td>${missed}</td><td>${unknown}</td></tr>`;\n }).join(\'\');\n}\nfunction paintTrendShadow(){\n const arms=[[\'A_E\',\'볼먼 + EMA 상승\'],[\'B_E\',\'벡스 + EMA 상승\'],[\'K_ATR\',\'코너스 + 저변동성 ATR\']];\n const complete=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&e.net_pct!==undefined&&Number.isFinite(Number(e.net_pct));\n $(\'trend_shadow_summary\').innerHTML=arms.map(([id,name])=>{\n  const all=responses.flatMap(r=>r.shadow_trend_atr?.[id]||[]);\n  const passed=all.filter(e=>e.filter_status===\'PASS\');\n  const finished=passed.filter(complete);\n  const wins=finished.filter(e=>Number(e.net_pct)>0).length;\n  const rejected=all.filter(e=>e.filter_status===\'REJECT\'&&complete(e));\n  const avoided=rejected.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=rejected.filter(e=>Number(e.net_pct)>0).length;\n  const unknown=all.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  const avg=finished.length?finished.reduce((s,e)=>s+Number(e.net_pct),0)/finished.length:NaN;\n  return `<tr><td>${name}</td><td>${passed.length}</td><td>${finished.length}</td><td class="positive">${wins}</td><td class="negative">${finished.length-wins}</td><td>${finished.length?(100*wins/finished.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td><td>${avoided}</td><td>${missed}</td><td>${unknown}</td></tr>`;\n }).join(\'\');\n}\n\nfunction paintBreakout(){\n const arms=[[\'A_EQ\',\'볼먼 A_E + 강한 돌파봉\',\'A_E\'],[\'B_EQ\',\'벡스 B_E + 매우 강한 돌파봉\',\'B_E\']];\n const complete=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&e.net_pct!==undefined&&Number.isFinite(Number(e.net_pct));\n const avg=a=>a.length?a.reduce((s,e)=>s+Number(e.net_pct),0)/a.length:NaN;\n $(\'quality_summary\').innerHTML=arms.map(([id,name,parent])=>{\n  const all=responses.flatMap(r=>r.shadow_breakout?.[id]||[]);\n  const base=all.filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status));\n  const eligible=all.filter(e=>e.filter_status!==\'BASE_REJECT\'&&e.filter_status!==\'BASE_UNKNOWN\');\n  const passed=eligible.filter(e=>e.filter_status===\'PASS\');\n  const done=passed.filter(complete);const wins=done.filter(e=>Number(e.net_pct)>0).length;\n  const drop=eligible.filter(e=>e.filter_status===\'REJECT\'&&complete(e));\n  const avoided=drop.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=drop.filter(e=>Number(e.net_pct)>0).length;\n  const unjudge=eligible.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  const emaPassed=all.filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status)).length;\n  return `<tr><td>${name}</td><td>${emaPassed}</td><td>${passed.length}</td><td>${done.length}</td><td class="positive">${wins}</td><td class="negative">${done.length-wins}</td><td>${done.length?(100*wins/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg(done)>=0?\'positive\':\'negative\'}">${pct(avg(done))}</td><td>${avoided}</td><td>${missed}</td><td>${unjudge}</td></tr>`;\n }).join(\'\');\n const specs=[[\'quality_body_ok\',\'몸통 비율\'],[\'quality_upper_ok\',\'윗꼬리 비율\'],[\'quality_margin_ok\',\'돌파 폭\']];\n $(\'quality_diagnostics\').innerHTML=arms.flatMap(([id,name])=>{\n  const all=responses.flatMap(r=>r.shadow_breakout?.[id]||[]).filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status)&&complete(e));\n  return specs.map(([key,metric])=>{\n   const passed=all.filter(e=>e[key]===true),excluded=all.filter(e=>e[key]===false);\n   const wins=passed.filter(e=>Number(e.net_pct)>0).length;\n   const lose=passed.length-wins;\n   const mw=excluded.filter(e=>Number(e.net_pct)>0).length;\n   const al=excluded.filter(e=>Number(e.net_pct)<=0).length;\n   return `<tr><td>${name}</td><td>${metric}</td><td>${passed.length}</td><td class="positive">${wins}</td><td class="negative">${lose}</td><td>${passed.length?(100*wins/passed.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg(passed)>=0?\'positive\':\'negative\'}">${pct(avg(passed))}</td><td>${mw}</td><td>${al}</td></tr>`;\n  });\n }).join(\'\');\n}\nfunction paintCore5(){\n const arms=[[\'A_EM\',\'볼먼 A_E + 돌파 폭만\'],[\'B_ENB\',\'벡스 B_E + 몸통 70% 제한 해제\'],[\'B_EM\',\'벡스 B_E + 돌파 폭만\']];\n const complete=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&e.net_pct!==undefined&&Number.isFinite(Number(e.net_pct));\n const avg=a=>a.length?a.reduce((s,e)=>s+Number(e.net_pct),0)/a.length:NaN;\n $(\'core5_summary\').innerHTML=arms.map(([id,name])=>{\n  const all=responses.flatMap(r=>r.shadow_core5?.[id]||[]);\n  const eligible=all.filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status));\n  const passed=eligible.filter(e=>e.filter_status===\'PASS\');\n  const done=passed.filter(complete), wins=done.filter(e=>Number(e.net_pct)>0).length;\n  const dropped=eligible.filter(e=>e.filter_status===\'REJECT\'&&complete(e));\n  const avoided=dropped.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=dropped.filter(e=>Number(e.net_pct)>0).length;\n  const unknown=eligible.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  return `<tr><td>${name}</td><td>${eligible.length}</td><td>${passed.length}</td><td>${done.length}</td><td class="positive">${wins}</td><td class="negative">${done.length-wins}</td><td>${done.length?(100*wins/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg(done)>=0?\'positive\':\'negative\'}">${pct(avg(done))}</td><td>${avoided}</td><td>${missed}</td><td>${unknown}</td></tr>`;\n }).join(\'\');\n}\nfunction paintCore6(){\n const variants=[[\'A_EQ_TR\',\'볼먼 A_EQ + 종목 SMA50 상승\'],[\'A_EQ_ATR\',\'볼먼 A_EQ + ATR<0.5%\'],[\'A_EQ_R\',\'볼먼 A_EQ + 두 조건\'],[\'B_EW\',\'벡스 B_E + 윗꼬리 ≤10%만\']];\n const complete=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&e.net_pct!==undefined&&Number.isFinite(Number(e.net_pct));\n const mean=a=>a.length?a.reduce((s,e)=>s+Number(e.net_pct),0)/a.length:NaN;\n $(\'core6_summary\').innerHTML=variants.map(([id,label])=>{\n  const all=responses.flatMap(r=>r.shadow_core6?.[id]||[]);\n  const eligible=all.filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status));\n  const passed=eligible.filter(e=>e.filter_status===\'PASS\');\n  const done=passed.filter(complete), wins=done.filter(e=>Number(e.net_pct)>0).length;\n  const dropped=eligible.filter(e=>e.filter_status===\'REJECT\'&&complete(e));\n  const avoided=dropped.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=dropped.filter(e=>Number(e.net_pct)>0).length;\n  const unknown=eligible.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  return `<tr><td>${label}</td><td>${eligible.length}</td><td>${passed.length}</td><td>${done.length}</td><td class="positive">${wins}</td><td class="negative">${done.length-wins}</td><td>${done.length?(100*wins/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${mean(done)>=0?\'positive\':\'negative\'}">${pct(mean(done))}</td><td>${avoided}</td><td>${missed}</td><td>${unknown}</td></tr>`;\n }).join(\'\');\n const all=responses.flatMap(r=>r.shadow_core6?.A_EQ_R||[])\n   .filter(e=>e.filter_status!==\'BASE_REJECT\'&&e.filter_status!==\'BASE_UNKNOWN\'&&complete(e));\n const groups=[[\'종목 SMA50 상승 + 종가 상회\',e=>e.local_uptrend===true],[\'그 외 종목 추세\',e=>e.local_uptrend===false],[\'ATR 0.5% 미만\',e=>e.local_low_volatility===true],[\'ATR 0.5% 이상\',e=>e.local_low_volatility===false]];\n $(\'core6_regime\').innerHTML=groups.map(([name,pred])=>{\n  const done=all.filter(e=>pred(e)),wins=done.filter(e=>Number(e.net_pct)>0).length;\n  return `<tr><td>${name}</td><td>${done.length}</td><td class="positive">${wins}</td><td class="negative">${done.length-wins}</td><td>${done.length?(100*wins/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${mean(done)>=0?\'positive\':\'negative\'}">${pct(mean(done))}</td></tr>`;\n }).join(\'\');\n}\nfunction paintCore7(){\n $(\'btc_status\').textContent=btcStatus===\'OK\'\n  ?\'BTC 과거 5분봉 조회 완료 · 동시간대의 완료 봉만 사용 · 10분 서버 캐시\'\n  :\'BTC 상태: \'+btcStatus+\' (조회 실패 시 기존 전략 결과는 계속 표시)\';\n const mean=a=>a.length?a.reduce((s,e)=>s+Number(e.net_pct),0)/a.length:NaN;\n const parents=[[\'볼먼 A_EQ\',\'shadow_breakout\',\'A_EQ\'],\n  [\'벡스 B_E\',\'shadow_trend_atr\',\'B_E\'],[\'벡스 B_EW\',\'shadow_core6\',\'B_EW\']];\n $(\'core7_regimes\').innerHTML=parents.flatMap(([label,key,id])=>{\n  const eligible=responses.flatMap(r=>r[key]?.[id]||[])\n   .filter(e=>e.filter_status===\'PASS\'&&core7Done(e));\n  return [[\'BTC_UP\',\'상승\'],[\'BTC_SIDE\',\'횡보\'],[\'BTC_DOWN\',\'하락\'],[\'UNKNOWN\',\'판정 불가\']].map(([regime,title])=>{\n   const done=eligible.filter(e=>core7Metrics(e).btc_regime===regime);\n   const wins=done.filter(e=>Number(e.net_pct)>0).length;\n   return `<tr><td>${label}</td><td>${title}</td><td>${done.length}</td><td>${wins}</td><td>${done.length-wins}</td><td>${done.length?(100*wins/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${mean(done)>=0?\'positive\':\'negative\'}">${pct(mean(done))}</td></tr>`;\n  });\n }).join(\'\');\n $(\'core7_summary\').innerHTML=CORE7_DEFS.map(([id,parent,key,label])=>{\n  const all=responses.flatMap(r=>r.shadow_core7?.[id]||[]);\n  const valid=all.filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status));\n  const passed=valid.filter(e=>e.filter_status===\'PASS\'),done=passed.filter(core7Done);\n  const wins=done.filter(e=>Number(e.net_pct)>0).length;\n  const rejected=valid.filter(e=>e.filter_status===\'REJECT\'&&core7Done(e));\n  const avoided=rejected.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=rejected.filter(e=>Number(e.net_pct)>0).length;\n  const unknown=valid.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  return `<tr><td>${label}</td><td>${valid.length}</td><td>${passed.length}</td><td>${done.length}</td><td>${wins}</td><td>${done.length-wins}</td><td>${done.length?(100*wins/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${mean(done)>=0?\'positive\':\'negative\'}">${pct(mean(done))}</td><td>${avoided}</td><td>${missed}</td><td>${unknown}</td></tr>`;\n }).join(\'\');\n}\nfunction paintCore10(){\n const arms=[[\'A\',\'볼먼 A\'],[\'B\',\'벡스 B (A의 부분집합)\']];\n const tf=[5,10], mas=[3,5,10];\n const key=(arm,tf,ma)=>arm+\'_\'+tf+\'M_MA\'+String(ma).padStart(2,\'0\');\n const completed=e=>[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!=null&&Number.isFinite(Number(e.net_pct));\n $(\'core10_summary\').innerHTML=arms.flatMap(([arm,name])=>tf.flatMap(t=>mas.map(m=>{\n  const id=key(arm,t,m),all=responses.flatMap(r=>r.core10_timeframes?.groups?.[id]||[]);\n  const done=all.filter(completed),wins=done.filter(e=>Number(e.net_pct)>0).length;\n  const net=done.length?done.reduce((sum,e)=>sum+Number(e.net_pct),0)/done.length:NaN;\n  const days=new Map();for(const e of done){const d=e.signal_time_kst.slice(0,10);if(!days.has(d))days.set(d,[]);days.get(d).push(e);}\n  let positiveDays=0;for(const xs of days.values()){if(xs.reduce((s,e)=>s+Number(e.net_pct),0)/xs.length>0)positiveDays++;}\n  return `<tr><td>${name}</td><td>${t}분</td><td>SMA${m}</td><td>${t*m}분</td><td>${all.length}</td><td>${done.length}</td><td>${wins}</td><td>${done.length-wins}</td><td>${all.length-done.length}</td><td>${done.length?(100*wins/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${net>=0?\'positive\':\'negative\'}">${pct(net)}</td><td>${positiveDays}/${days.size}</td></tr>`;\n }))).join(\'\');\n let five=0,ten=0,miss=0;for(const r of responses){const c=r.core10_timeframes?.coverage;if(!c)continue;five+=c.observed_5m||0;ten+=c.complete_10m||0;miss+=(c.possible_10m_slots||0)-(c.complete_10m||0);}\n $(\'core10_coverage\').textContent=`조회된 5분봉 ${five}개 · 완성된 10분봉 ${ten}개 · 10분봉 미완성/누락 ${miss}개 (모든 종목 합산). A 5분 SMA5는 원래 볼먼 A 신호와 정확히 같아야 해.`;\n}\n\nfunction core11FocusEvents(){return responses.flatMap(r=>r.core11_focus?.events||[]);}\nfunction core11PctMean(xs,field=\'net_pct\'){\n return xs.length?xs.reduce((a,b)=>a+Number(b[field]),0)/xs.length:NaN;\n}\nfunction core12FocusEvents(){return responses.flatMap(r=>r.core12_focus?.events||[]);}\n\nfunction paintCore18(){\n const evs=responses.flatMap(r=>r.core18_focus?.events||[]);\n const defs=[[\'SCALE_05_5_10_BE\',\'손절 없음\'],\n  [\'SCALE_05_5_10_BE_SL1\',\'-1.0%\'],\n  [\'SCALE_05_5_10_BE_SL15\',\'-1.5%\'],\n  [\'SCALE_05_5_10_BE_SL2\',\'-2.0%\'],\n  [\'SCALE_05_5_10_BE_SL25\',\'-2.5%\'],\n  [\'SCALE_05_5_10_BE_SL3\',\'-3.0%\']];\n const done=o=>o?.status===\'RESOLVED\'&&Number.isFinite(Number(o.net_pct))&&o.net_pct!==null;\n const full=evs.filter(e=>e.core18_full_path===true);\n const paired=full.filter(e=>defs.every(([id])=>done(e.core18_scenarios?.[id])));\n const early=evs.filter(e=>!e.core18_full_path&&defs.some(([id])=>done(e.core18_scenarios?.[id])));\n $(\'core18_coverage\').innerHTML=`<tr><td>${evs.length}</td><td>${full.length}</td><td>${paired.length}</td><td>${evs.length-full.length}</td><td>${early.length}</td></tr>`;\n const avg=v=>v.length?v.reduce((s,o)=>s+Number(o.net_pct),0)/v.length:NaN;\n const baselineId=defs[0][0];\n $(\'core18_paired\').innerHTML=defs.map(([id,name])=>{\n  const samples=paired.map(e=>({o:e.core18_scenarios[id],base:e.core18_scenarios[baselineId]}));\n  const vals=samples.map(x=>x.o),mean=avg(vals),worst=vals.length?Math.min(...vals.map(o=>Number(o.net_pct))):NaN;\n  const count=f=>vals.filter(f).length;\n  const imp=id===baselineId?\'—\':samples.filter(x=>Number(x.o.net_pct)>Number(x.base.net_pct)+1e-7).length;\n  const bad=id===baselineId?\'—\':samples.filter(x=>Number(x.o.net_pct)<Number(x.base.net_pct)-1e-7).length;\n  return `<tr><td>${name}</td><td>${vals.length}</td><td>${count(o=>o.net_pct>0)}</td><td>${count(o=>o.net_pct>=0.5-1e-10)}</td><td>${count(o=>o.stop_triggered===true)}</td><td>${count(o=>o.tp1_filled===true)}</td><td>${count(o=>o.be_triggered===true)}</td><td>${count(o=>o.outcome===\'TIME\')}</td><td class="${mean>=0?\'positive\':\'negative\'}">${pct(mean)}</td><td class="${worst>=0?\'positive\':\'negative\'}">${pct(worst)}</td><td>${imp}</td><td>${bad}</td><td>${count(o=>o.stop_tp_same_bar===true)}</td></tr>`;\n }).join(\'\');\n $(\'core18_all\').innerHTML=defs.map(([id,name])=>{\n  const v=evs.map(e=>e.core18_scenarios?.[id]).filter(done),mean=avg(v);\n  return `<tr><td>${name}</td><td>${evs.length}</td><td>${v.length}</td><td>${evs.length-v.length}</td><td>${v.filter(o=>o.stop_triggered===true).length}</td><td class="${mean>=0?\'positive\':\'negative\'}">${pct(mean)}</td></tr>`;\n }).join(\'\');\n}\n\n\n\nfunction paintCore20(){\n const evs=responses.flatMap(r=>r.core20_focus?.events||[]);\n const triggered=evs.filter(e=>e.status===\'TRIGGERED\');\n const noDip=evs.filter(e=>e.status===\'NO_TRIGGER\');\n const unknown=evs.filter(e=>e.status===\'UNKNOWN\');\n const thin=triggered.filter(e=>e.liquidity?.tier===\'THIN\');\n const high=triggered.filter(e=>e.liquidity?.tier===\'HIGH\');\n $(\'core20_coverage\').innerHTML=`<tr><td>${evs.length}</td><td>${triggered.length}</td><td>${noDip.length}</td><td>${unknown.length}</td><td>${thin.length}</td><td>${high.length}</td></tr>`;\n const names={\'RECOVERY_CONFIRMED\':\'반등 확인\',\'REBOUND_WATCH\':\'반등 관찰\',\'DOWNTREND\':\'하락 지속\',\'INDETERMINATE\':\'방향 불명\',\'INSUFFICIENT_DATA\':\'판정 보류\'};\n const states=[\'RECOVERY_CONFIRMED\',\'REBOUND_WATCH\',\'DOWNTREND\',\'INDETERMINATE\',\'INSUFFICIENT_DATA\'];\n const ready=e=>e.followups?.[\'60\']?.status===\'READY\';\n const mean=v=>v.length?v.reduce((a,x)=>a+Number(x),0)/v.length:NaN;\n $(\'core20_summary\').innerHTML=states.map(id=>{\n  const part=triggered.filter(e=>e.decision?.state===id);\n  const known=part.filter(ready);\n  const vals=known.map(e=>Number(e.followups[\'60\'].gross_pct));\n  return `<tr><td>${names[id]}</td><td>${part.length}</td><td>${known.length}</td><td>${vals.filter(v=>v>0).length}</td><td>${vals.filter(v=>v>=0.5).length}</td><td class="${mean(vals)>=0?\'positive\':\'negative\'}">${pct(mean(vals))}</td><td>${pct(mean(known.map(e=>Number(e.followups[\'60\'].worst_low_pct))))}</td></tr>`;\n }).join(\'\');\n const levels=[[\'HIGH\',\'활발\',\'3+5분\'],[\'MEDIUM\',\'보통\',\'5+10분\'],[\'LOW\',\'적음\',\'10+15분\'],[\'THIN\',\'유동성 부족\',\'판정보류\']];\n $(\'core20_liquidity\').innerHTML=levels.map(([id,name,tf])=>{\n  const part=triggered.filter(e=>e.liquidity?.tier===id);\n  const n=st=>part.filter(e=>e.decision?.state===st).length;\n  return `<tr><td>${name}</td><td>${tf}</td><td>${part.length}</td><td>${n(\'RECOVERY_CONFIRMED\')}</td><td>${n(\'REBOUND_WATCH\')}</td><td>${n(\'DOWNTREND\')}</td><td>${n(\'INDETERMINATE\')+n(\'INSUFFICIENT_DATA\')}</td></tr>`;\n }).join(\'\');\n $(\'core20_details\').innerHTML=triggered.slice(0,250).map(e=>{\n  const l=e.liquidity||{},a=e.decision||{};\n  const ret=n=>e.followups?.[String(n)]?.status===\'READY\'?pct(Number(e.followups[String(n)].gross_pct)):\'—\';\n  return `<tr><td>${esc(e.market)} · ${esc(e.entry_time_kst)}</td><td>${esc(e.trigger_time_kst)}</td><td>${pct(Number(e.trigger_gross_pct))}</td><td>${esc(l.tier)}</td><td>${l.primary_tf||\'—\'}+${l.secondary_tf||\'—\'}</td><td>${names[a.state]||esc(a.state)}</td><td>${names[e.recheck_15?.state]||\'—\'}</td><td>${names[e.recheck_30?.state]||\'—\'}</td><td>${ret(15)}</td><td>${ret(30)}</td><td>${ret(60)}</td><td>${e.baseline_be_net_pct==null?\'—\':pct(Number(e.baseline_be_net_pct))}</td></tr>`;\n }).join(\'\')||\'<tr><td colspan="12">하락 재판정 신호 없음</td></tr>\';\n}\n\nfunction paintCore19(){\n const evs=responses.flatMap(r=>r.core19_focus?.events||[]);\n const defs=[[\'SCALE_05_5_10_BE\',\'기존 BE · 손절 없음\'],\n  [\'SCALE_05_5_10_BE_SL1\',\'기존 -1.0%\'],\n  [\'SCALE_05_5_10_BE_SL15\',\'기존 -1.5%\'],\n  [\'SCALE_05_5_10_BE_SL2\',\'기존 -2.0%\'],\n  [\'SCALE_05_5_10_BE_SL25\',\'기존 -2.5%\'],\n  [\'SCALE_05_5_10_BE_SL3\',\'기존 -3.0%\'],\n  [\'SCALE_05_5_10_BE_ATR1\',\'ATR × 1.0\'],\n  [\'SCALE_05_5_10_BE_ATR15\',\'ATR × 1.5\'],\n  [\'SCALE_05_5_10_BE_ATR2\',\'ATR × 2.0\'],\n  [\'SCALE_05_5_10_BE_ATR25\',\'ATR × 2.5\']];\n const done=o=>o?.status===\'RESOLVED\'&&o.net_pct!==null&&Number.isFinite(Number(o.net_pct));\n const ready=evs.filter(e=>e.core19_atr_ready===true);\n const full=ready.filter(e=>e.core19_full_path===true);\n const paired=full.filter(e=>defs.every(([id])=>done(e.core19_scenarios?.[id])));\n const early=ready.filter(e=>!e.core19_full_path&&defs.some(([id])=>done(e.core19_scenarios?.[id])));\n $(\'core19_coverage\').innerHTML=`<tr><td>${evs.length}</td><td>${ready.length}</td><td>${full.length}</td><td>${paired.length}</td><td>${evs.length-ready.length}</td><td>${early.length}</td></tr>`;\n const average=xs=>xs.length?xs.reduce((a,b)=>a+Number(b.net_pct),0)/xs.length:NaN;\n const baseId=defs[0][0];\n $(\'core19_paired\').innerHTML=defs.map(([id,name])=>{\n  const ss=paired.map(e=>({o:e.core19_scenarios[id],b:e.core19_scenarios[baseId]}));\n  const vs=ss.map(x=>x.o),mean=average(vs),base=average(ss.map(x=>x.b));\n  const stops=vs.map(o=>Number(o.stop_gross_pct)).filter(Number.isFinite).filter(n=>n>0);\n  const range=stops.length?`${Math.min(...stops).toFixed(2)}~${Math.max(...stops).toFixed(2)}%`:\'—\';\n  const imp=ss.filter(x=>Number(x.o.net_pct)>Number(x.b.net_pct)+1e-7).length;\n  const bad=ss.filter(x=>Number(x.o.net_pct)<Number(x.b.net_pct)-1e-7).length;\n  const reversed=ss.filter(x=>Number(x.b.net_pct)>0&&Number(x.o.net_pct)<0).length;\n  const min=vs.length?Math.min(...vs.map(o=>Number(o.net_pct))):NaN;\n  const delta=mean-base;\n  return `<tr><td>${name}</td><td>${vs.length}</td><td>${vs.filter(o=>Number(o.net_pct)>0).length}</td><td>${vs.filter(o=>o.stop_triggered===true).length}</td><td>${vs.filter(o=>o.tp1_filled===true).length}</td><td>${vs.filter(o=>o.be_triggered===true).length}</td><td class="${mean>=0?\'positive\':\'negative\'}">${pct(mean)}</td><td>${pct(min)}</td><td class="${delta>=0?\'positive\':\'negative\'}">${id===baseId?\'기준\':pct(delta)}</td><td>${imp}</td><td>${bad}</td><td>${reversed}</td><td>${range}</td><td>${vs.filter(o=>o.atr_clamped_max===true).length}</td><td>${vs.filter(o=>o.stop_tp_same_bar===true).length}</td></tr>`;\n }).join(\'\');\n $(\'core19_all\').innerHTML=defs.map(([id,name])=>{\n  const ds=evs.map(e=>e.core19_scenarios?.[id]).filter(done);\n  const mean=average(ds);\n  return `<tr><td>${name}</td><td>${evs.length}</td><td>${ds.length}</td><td>${evs.length-ds.length}</td><td>${ds.filter(o=>o.stop_triggered===true).length}</td><td class="${mean>=0?\'positive\':\'negative\'}">${pct(mean)}</td></tr>`;\n }).join(\'\');\n}\n\nfunction paintCore17(){\n const evs=responses.flatMap(r=>r.core17_focus?.events||[]);\n const full=evs.filter(e=>e.core17_full_path===true);\n const fid=\'SCALE_05_5_10_BE_SL1\',eid=\'SCALE_05_5_10_BE\';\n const done=o=>o?.status===\'RESOLVED\'&&core8Num(o.net_pct)!==null;\n const allResolved=evs.map(e=>e.core17_scenarios?.[fid]).filter(done);\n const early=evs.filter(e=>!e.core17_full_path && done(e.core17_scenarios?.[fid]));\n $(\'core17_coverage\').innerHTML=`<tr><td>${evs.length}</td><td>${full.length}</td><td>${evs.length-full.length}</td><td>${early.length}</td><td>${allResolved.filter(o=>o.stop_triggered).length}</td></tr>`;\n const rules=[[\'TP05_NO_SL\',\'A · 기존 +0.5% 전량\'],[\'SCALE_1_5_10\',\'B · 50/25/25\'],\n [\'SCALE_1_5_10_BE\',\'C · 50/25/25+본절\'],[\'SCALE_05_5_10\',\'D · 30/30/40\'],\n [eid,\'E · 30/30/40+본절\'],[fid,\'F · E + 첫 익절 전 -1% 손절\']];\n const get=id=>full.map(e=>e.core17_scenarios?.[id]).filter(done);\n const pctAvg=v=>v.length?v.reduce((s,o)=>s+Number(o.net_pct),0)/v.length:NaN;\n const fmt=(val)=>pct(val);\n $(\'core17_paired\').innerHTML=rules.map(([id,label])=>{\n  const v=get(id),n=k=>v.filter(o=>o[k]===true).length,oc=k=>v.filter(o=>o.outcome===k).length;\n  const avg=pctAvg(v),worst=v.length?Math.min(...v.map(o=>Number(o.net_pct))):NaN;\n  return `<tr><td>${label}</td><td>${v.length}</td><td>${v.filter(o=>Number(o.net_pct)>0).length}</td><td>${v.filter(o=>Number(o.net_pct)>=0.5-1e-10).length}</td><td>${n(\'tp1_filled\')}</td><td>${n(\'tp5_filled\')}</td><td>${n(\'tp10_filled\')}</td><td>${oc(\'STOP1\')}</td><td>${oc(\'BE\')}</td><td>${oc(\'TIME\')}</td><td class="${avg>=0?\'positive\':\'negative\'}">${fmt(avg)}</td><td class="${worst>=0?\'positive\':\'negative\'}">${fmt(worst)}</td><td>${n(\'ambiguous_same_bar\')}</td></tr>`;\n }).join(\'\');\n const stopCases=full.filter(e=>e.core17_scenarios?.[fid]?.outcome===\'STOP1\');\n const diff=stopCases.map(e=>({newer:e.core17_scenarios[fid],earlier:e.core17_scenarios[eid]}));\n const improve=diff.filter(x=>done(x.earlier)&&x.newer.net_pct>x.earlier.net_pct+1e-7).length;\n const worsen=diff.filter(x=>done(x.earlier)&&x.newer.net_pct<x.earlier.net_pct-1e-7).length;\n const lost=diff.filter(x=>x.earlier?.tp1_filled===true).length;\n const ambiguous=diff.filter(x=>x.newer.stop_tp_same_bar===true).length;\n $(\'core17_stop_impact\').innerHTML=`<tr><td>${stopCases.length}</td><td>${improve}</td><td>${worsen}</td><td>${lost}</td><td>${ambiguous}</td></tr>`;\n const avg=pctAvg(allResolved);\n $(\'core17_all\').innerHTML=`<tr><td>${evs.length}</td><td>${allResolved.length}</td><td>${evs.length-allResolved.length}</td><td>${allResolved.filter(o=>Number(o.net_pct)>0).length}</td><td>${allResolved.filter(o=>o.stop_triggered).length}</td><td class="${avg>=0?\'positive\':\'negative\'}">${fmt(avg)}</td></tr>`;\n}\n\nfunction paintCore16(){\n const evs=responses.flatMap(r=>r.core16_focus?.events||[]);\n const full=evs.filter(e=>e.core16_full_path===true);\n const early=evs.filter(e=>e.core16_early_confirmed===true);\n $(\'core16_coverage\').innerHTML=`<tr><td>${evs.length}</td><td>${full.length}</td><td>${evs.length-full.length}</td><td>${early.length}</td></tr>`;\n const rules=[[\'TP05_NO_SL\',\'A · 기존 +0.5% 전량\'],\n  [\'SCALE_1_5_10\',\'B · 기존 50/25/25\'],\n  [\'SCALE_1_5_10_BE\',\'C · 기존 50/25/25 + 본절\'],\n  [\'SCALE_05_5_10\',\'D · 신규 30/30/40\'],\n  [\'SCALE_05_5_10_BE\',\'E · 신규 30/30/40 + 본절\']];\n const done=o=>o?.status===\'RESOLVED\'&&core8Num(o.net_pct)!==null;\n const stats=(id,name,source,allMode)=>{\n  const v=source.map(e=>e.core16_scenarios?.[id]).filter(done);\n  const profit=v.filter(o=>Number(o.net_pct)>0).length;\n  const goal=v.filter(o=>Number(o.net_pct)>=0.5-1e-10).length;\n  const avg=v.length?v.reduce((s,o)=>s+Number(o.net_pct),0)/v.length:NaN;\n  if(allMode)return `<tr><td>${name}</td><td>${evs.length}</td><td>${v.length}</td><td>${evs.length-v.length}</td><td>${profit}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td></tr>`;\n  const hit=k=>v.filter(o=>o[k]===true).length;\n  const count=k=>v.filter(o=>o.outcome===k).length;\n  const worst=v.length?Math.min(...v.map(o=>Number(o.net_pct))):NaN;\n  return `<tr><td>${name}</td><td>${v.length}</td><td>${profit}</td><td>${goal}</td><td>${hit(\'tp1_filled\')}</td><td>${hit(\'tp5_filled\')}</td><td>${hit(\'tp10_filled\')}</td><td>${count(\'BE\')}</td><td>${count(\'TIME\')}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td><td class="${worst>=0?\'positive\':\'negative\'}">${pct(worst)}</td><td>${hit(\'ambiguous_same_bar\')}</td></tr>`;\n };\n $(\'core16_paired\').innerHTML=rules.map(([id,name])=>stats(id,name,full,false)).join(\'\');\n $(\'core16_all\').innerHTML=rules.map(([id,name])=>stats(id,name,evs,true)).join(\'\');\n}\n\nfunction paintCore15(){\n const evs=responses.flatMap(r=>r.core15_focus?.events||[]);\n const full=evs.filter(e=>e.core15_full_path===true);\n const early=evs.filter(e=>e.core15_early_confirmed===true);\n $(\'core15_coverage\').innerHTML=`<tr><td>${evs.length}</td><td>${full.length}</td><td>${evs.length-full.length}</td><td>${early.length}</td></tr>`;\n const rules=[[\'TP05_NO_SL\',\'A · 기존 +0.5% 전량 익절\'],\n   [\'SCALE_1_5_10\',\'B · 1/5/10% 분할 익절\'],\n   [\'SCALE_1_5_10_BE\',\'C · 분할 익절 + 본절 스탑\']];\n const done=o=>o?.status===\'RESOLVED\' && core8Num(o.net_pct)!==null;\n const stats=(rule,name,source,allMode)=>{\n  const vals=source.map(e=>e.core15_scenarios?.[rule]).filter(done);\n  const positive=vals.filter(o=>Number(o.net_pct)>0).length;\n  const target=vals.filter(o=>Number(o.net_pct)+1e-10>=0.5).length;\n  const avg=vals.length?vals.reduce((s,o)=>s+Number(o.net_pct),0)/vals.length:NaN;\n  if(allMode)return `<tr><td>${name}</td><td>${evs.length}</td><td>${vals.length}</td><td>${evs.length-vals.length}</td><td>${positive}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td></tr>`;\n  const fill=(field)=>vals.filter(o=>o[field]===true).length;\n  const worst=vals.length?Math.min(...vals.map(o=>Number(o.net_pct))):NaN;\n  const exits=(kind)=>vals.filter(o=>o.outcome===kind).length;\n  return `<tr><td>${name}</td><td>${vals.length}</td><td>${positive}</td><td>${target}</td><td>${fill(\'tp1_filled\')}</td><td>${fill(\'tp5_filled\')}</td><td>${fill(\'tp10_filled\')}</td><td>${exits(\'BE\')}</td><td>${exits(\'TIME\')}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td><td class="${worst>=0?\'positive\':\'negative\'}">${pct(worst)}</td><td>${fill(\'ambiguous_same_bar\')}</td></tr>`;\n };\n $(\'core15_paired\').innerHTML=rules.map(([id,name])=>stats(id,name,full,false)).join(\'\');\n $(\'core15_all\').innerHTML=rules.map(([id,name])=>stats(id,name,evs,true)).join(\'\');\n}\n\nfunction paintCore14(){\n const evs=responses.flatMap(r=>r.core14_focus?.events||[]);\n const full=evs.filter(e=>e.core14_full_path===true);\n const atrReady=evs.filter(e=>e.core14_atr_data_status===\'READY\');\n const pairedATR=full.filter(e=>e.core14_atr_data_status===\'READY\');\n $(\'core14_coverage\').innerHTML=`<tr><td>${evs.length}</td><td>${full.length}</td><td>${atrReady.length}</td><td>${pairedATR.length}</td><td>${evs.length-full.length}</td></tr>`;\n const ruleNames=[[\'TP05_NO_SL\',\'A · 기준 +0.5% 익절\'],\n   [\'DIAG30_NEG05\',\'B · 30분 -0.5% 진단\'],\n   [\'DIAG60_NEG05\',\'C · 60분 -0.5% 진단\'],\n   [\'ATR14_10M_X15\',\'D · ATR14(10분) ×1.5 손절\']];\n const resolved=o=>o && [\'TP\',\'SL\',\'DIAG\',\'TIME\'].includes(o.outcome) && core8Num(o.net_pct)!==null;\n const stats=(rule,name,subset,allMode)=>{\n   const rows=subset.map(e=>({e,o:e.core14_scenarios?.[rule]})).filter(x=>resolved(x.o));\n   const o=rows.map(x=>x.o),wins=o.filter(x=>Number(x.net_pct)+1e-10>=0.5).length;\n   const avg=o.length?o.reduce((sum,x)=>sum+Number(x.net_pct),0)/o.length:NaN;\n   if(allMode)return `<tr><td>${name}</td><td>${evs.length}</td><td>${o.length}</td><td>${evs.length-o.length}</td><td>${wins}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td></tr>`;\n   const pos=o.filter(x=>Number(x.net_pct)>0).length;\n   const count=type=>o.filter(x=>x.outcome===type).length;\n   const worst=o.length?Math.min(...o.map(x=>Number(x.net_pct))):NaN;\n   const missed=rows.filter(({e,o})=>o.outcome===\'DIAG\'&&e.core14_scenarios?.TP05_NO_SL?.outcome===\'TP\').length;\n   return `<tr><td>${name}</td><td>${o.length}</td><td class="positive">${wins}</td><td>${pos}</td><td>${count(\'TP\')}</td><td>${count(\'DIAG\')}</td><td>${count(\'SL\')}</td><td>${count(\'TIME\')}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td><td class="${worst>=0?\'positive\':\'negative\'}">${pct(worst)}</td><td>${missed}</td></tr>`;\n };\n $(\'core14_paired\').innerHTML=ruleNames.slice(0,3).map(([id,name])=>stats(id,name,full,false)).join(\'\');\n $(\'core14_paired_atr\').innerHTML=ruleNames.map(([id,name])=>stats(id,name,pairedATR,false)).join(\'\');\n $(\'core14_all\').innerHTML=ruleNames.map(([id,name])=>stats(id,name,evs,true)).join(\'\');\n}\n\nfunction paintCore13(){\n const evs=responses.flatMap(r=>r.core13_focus?.events||[]);\n const full=evs.filter(e=>e.core13_full_path===true);\n const early=evs.filter(e=>e.core13_early_confirmed===true);\n $(\'core13_coverage\').innerHTML=`<tr><td>${evs.length}</td><td>${full.length}</td><td>${evs.length-full.length}</td><td>${early.length}</td></tr>`;\n const rules=[[\'TP05_NO_SL\',\'A · +0.5% 익절 / 손절 없음\'],[\'TP05_SL1\',\'B · +0.5% 익절 / -1% 손절\'],[\'HOLD240\',\'C · 240분 종가 보유\']];\n const isDone=o=>o && [\'TP\',\'SL\',\'TIME\'].includes(o.outcome) && core8Num(o.net_pct)!==null;\n const line=(rule,name,source,paired)=>{\n   const os=source.map(e=>e.core13_scenarios?.[rule]).filter(isDone);\n   const tp=os.filter(o=>o.outcome===\'TP\').length, sl=os.filter(o=>o.outcome===\'SL\').length;\n   const time=os.filter(o=>o.outcome===\'TIME\').length, wins=os.filter(o=>Number(o.net_pct)+1e-10>=0.5).length;\n   const avg=os.length?os.reduce((s,o)=>s+Number(o.net_pct),0)/os.length:NaN;\n   if(paired)return `<tr><td>${name}</td><td>${os.length}</td><td class="positive">${wins}</td><td>${os.length-wins}</td><td>${tp}</td><td>${sl}</td><td>${time}</td><td>${os.length?(100*wins/os.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td></tr>`;\n   return `<tr><td>${name}</td><td>${evs.length}</td><td>${os.length}</td><td>${evs.length-os.length}</td><td>${wins}</td><td>${os.length-wins}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td></tr>`;\n };\n $(\'core13_paired\').innerHTML=rules.map(([id,name])=>line(id,name,full,true)).join(\'\');\n $(\'core13_all\').innerHTML=rules.map(([id,name])=>line(id,name,evs,false)).join(\'\');\n const tpFirst=full.filter(e=>[\'TP_FIRST\',\'TP_ONLY_OBSERVED\'].includes(e.core13_first_touch)).length;\n const slFirst=full.filter(e=>[\'SL_FIRST\',\'SL_ONLY_OBSERVED\'].includes(e.core13_first_touch)).length;\n const same=full.filter(e=>e.core13_first_touch===\'SAME_BAR_SL_FIRST\').length;\n const none=full.filter(e=>e.core13_first_touch===\'NEITHER\').length;\n $(\'core13_order\').innerHTML=`<tr><td>${full.length}건</td><td>${tpFirst}</td><td>${slFirst}</td><td>${same}</td><td>${none}</td></tr>`;\n}\n\nfunction paintCore12(){\n const es=core12FocusEvents(), full=es.filter(e=>e.core12_path_status===\'FULL\');\n $(\'core12_overview\').innerHTML=`<tr><td>${es.length}</td><td>${full.length}</td><td>${es.length-full.length}</td><td>순수익률 +0.50% 이상</td></tr>`;\n $(\'core12_holds\').innerHTML=[15,30,60,120,180,240].map(h=>{\n  const pre=`core12_hold_${h}m_`;\n  const valid=full.filter(e=>[\'수익\',\'손절\'].includes(e[pre+\'label\'])&&core8Num(e[pre+\'net_pct\'])!==null);\n  const profit=valid.filter(e=>e[pre+\'label\']===\'수익\').length;\n  const loss=valid.length-profit;\n  const avg=valid.length?valid.reduce((sum,e)=>sum+Number(e[pre+\'net_pct\']),0)/valid.length:NaN;\n  const touch=valid.filter(e=>e[pre+\'target_reached\']===true).length;\n  return `<tr><td>${h}분</td><td>${valid.length}</td><td class="positive">${profit}</td><td class="negative">${loss}</td><td>${valid.length?(100*profit/valid.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td><td>${touch}</td></tr>`;\n }).join(\'\');\n}\n\nfunction paintCore11(){\n const es=core11FocusEvents(),completed=es.filter(core7Done);\n const full=es.filter(e=>e.core11_path_status===\'FULL\');\n const unknownBTC=es.filter(e=>core7Metrics(e).btc_regime===\'UNKNOWN\').length;\n const unknownVol=es.filter(e=>core8Num(e.core11_volume_ratio20_10m)===null).length;\n const wins=completed.filter(e=>Number(e.net_pct)>0).length;\n $(\'core11_overview\').innerHTML=`<tr><td>${es.length}</td><td>${completed.length}</td><td>${wins}</td><td>${completed.length-wins}</td><td class="${core11PctMean(completed)>=0?\'positive\':\'negative\'}">${pct(core11PctMean(completed))}</td><td>${full.length}</td><td>${es.length-full.length}</td><td>${unknownBTC}</td><td>${unknownVol}</td></tr>`;\n const ranges=[\n  [\'돌파 폭\',[\n   [\'0.10% 미만\',e=>core8Num(e.break_margin_pct)!==null&&Number(e.break_margin_pct)<.10],\n   [\'0.10~0.30%\',e=>core8Num(e.break_margin_pct)!==null&&Number(e.break_margin_pct)>=.10&&Number(e.break_margin_pct)<.30],\n   [\'0.30% 이상\',e=>core8Num(e.break_margin_pct)!==null&&Number(e.break_margin_pct)>=.30],\n   [\'UNKNOWN\',e=>core8Num(e.break_margin_pct)===null]]],\n  [\'10분 거래량 / 직전 20봉\',[\n   [\'1.0배 미만\',e=>core8Num(e.core11_volume_ratio20_10m)!==null&&Number(e.core11_volume_ratio20_10m)<1],\n   [\'1.0~1.5배\',e=>core8Num(e.core11_volume_ratio20_10m)!==null&&Number(e.core11_volume_ratio20_10m)>=1&&Number(e.core11_volume_ratio20_10m)<1.5],\n   [\'1.5배 이상\',e=>core8Num(e.core11_volume_ratio20_10m)!==null&&Number(e.core11_volume_ratio20_10m)>=1.5],\n   [\'UNKNOWN\',e=>core8Num(e.core11_volume_ratio20_10m)===null]]],\n  [\'BTC 최근 1시간\',[\n   [\'상승 ≥+0.30%\',e=>core7Metrics(e).btc_regime===\'BTC_UP\'],\n   [\'횡보\',e=>core7Metrics(e).btc_regime===\'BTC_SIDE\'],\n   [\'하락 ≤-0.30%\',e=>core7Metrics(e).btc_regime===\'BTC_DOWN\'],\n   [\'UNKNOWN\',e=>core7Metrics(e).btc_regime===\'UNKNOWN\']]]];\n $(\'core11_diagnostics\').innerHTML=ranges.flatMap(([label,bins])=>bins.map(([title,select])=>{\n   const v=full.filter(select),w=v.filter(e=>Number(e.net_pct)>0).length,avg=core11PctMean(v);\n   return `<tr><td>${esc(label)}</td><td>${esc(title)}</td><td>${v.length}</td><td>${w} / ${v.length-w}</td><td>${v.length?(100*w/v.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td></tr>`;\n })).join(\'\');\n $(\'core11_holds\').innerHTML=[15,30,60].map(h=>{\n  const key=`core11_hold_${h}m_`,valid=full.filter(e=>[\'TP\',\'SL\',\'TIME\'].includes(e[key+\'outcome\'])&&core8Num(e[key+\'net_pct\'])!==null),wins=valid.filter(e=>Number(e[key+\'net_pct\'])>0).length;\n  const avg=valid.length?valid.reduce((s,e)=>s+Number(e[key+\'net_pct\']),0)/valid.length:NaN;\n  const count=x=>valid.filter(e=>e[key+\'outcome\']===x).length;\n  return `<tr><td>${h}분</td><td>${valid.length}</td><td>${wins} / ${valid.length-wins}</td><td>${valid.length?(100*wins/valid.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td><td>${count(\'TP\')}</td><td>${count(\'SL\')}</td><td>${count(\'TIME\')}</td></tr>`;\n }).join(\'\');\n}\n\nfunction paintCore9(){\n const defs=[[\'A_EQ\',\'볼먼 A_EQ\'],[\'B_E\',\'벡스 B_E\']];\n const ok=e=>e&&e.path_status===\'FULL\';\n const completed=e=>e&&[\'TP\',\'SL\',\'TIME\'].includes(e.outcome)&&e.net_pct!==null&&Number.isFinite(Number(e.net_pct));\n const avg=(a,key)=>a.length?a.reduce((s,e)=>s+Number(e[key]),0)/a.length:NaN;\n const doneList=(arm)=>responses.flatMap(r=>r.core9_paths?.[arm]||[]);\n $(\'core9_paths\').innerHTML=defs.map(([arm,label])=>{\n  const rows=doneList(arm),full=rows.filter(ok),missing=rows.length-full.length;\n  const count=v=>full.filter(e=>e.mfe_pct>=v).length;\n  return `<tr><td>${label}</td><td>${rows.length}</td><td>${full.length}</td><td>${missing}</td><td>${pct(avg(full,\'mfe_pct\'))}</td><td>${pct(avg(full,\'mae_pct\'))}</td><td>${count(.5)}</td><td>${count(1)}</td><td>${count(1.5)}</td><td>${pct(avg(full,\'net_60m_pct\'))}</td></tr>`;\n }).join(\'\');\n const tps=[.5,.75,1,1.5],sls=[.5,.75,1];\n const key=(tp,sl)=>\'tp\'+String(Math.round(tp*100)).padStart(3,\'0\')+\'_sl\'+String(Math.round(sl*100)).padStart(3,\'0\');\n $(\'core9_grid\').innerHTML=defs.flatMap(([arm,label])=>{\n  const full=doneList(arm).filter(ok);\n  return tps.flatMap(tp=>sls.map(sl=>{\n   const x=full.map(e=>e.grid?.[key(tp,sl)]).filter(completed);\n   const wins=x.filter(e=>Number(e.net_pct)>0).length;\n   const n=type=>x.filter(e=>e.outcome===type).length;\n   return `<tr><td>${label}</td><td>+${tp.toFixed(2)}%</td><td>-${sl.toFixed(2)}%</td><td>${x.length}</td><td>${wins} / ${x.length-wins}</td><td>${x.length?(100*wins/x.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg(x,\'net_pct\')>=0?\'positive\':\'negative\'}">${pct(avg(x,\'net_pct\'))}</td><td>${n(\'TP\')} / ${n(\'SL\')} / ${n(\'TIME\')}</td></tr>`;\n  }));\n }).join(\'\');\n $(\'core9_delay\').innerHTML=defs.map(([arm,label])=>{\n  const all=doneList(arm).filter(ok),matched=all.filter(e=>e.base_net_pct!=null&&Number.isFinite(Number(e.base_net_pct))&&[\'TP\',\'SL\',\'TIME\'].includes(e.delay5_outcome)&&e.delay5_net_pct!=null&&Number.isFinite(Number(e.delay5_net_pct)));\n  const base=avg(matched,\'base_net_pct\'),late=avg(matched,\'delay5_net_pct\');\n  return `<tr><td>${label}</td><td>${matched.length}</td><td>${all.length-matched.length}</td><td>${pct(base)}</td><td>${pct(late)}</td><td>${pct(late-base)}</td></tr>`;\n }).join(\'\');\n}\nfunction paintCore8(){\n const base=[[\'볼먼 A_EQ\',\'shadow_breakout\',\'A_EQ\'],[\'벡스 B_E\',\'shadow_trend_atr\',\'B_E\']];\n const atrs=[[\'LOW\',\'낮음 <0.15%\'],[\'MED\',\'중간 0.15~0.25%\'],[\'HIGH\',\'높음 ≥0.25%\']];\n const widths=[[\'SMALL\',\'작음 <0.10%\'],[\'MID\',\'중간 0.10~0.30%\'],[\'LARGE\',\'큼 ≥0.30%\']];\n const mean=x=>x.length?x.reduce((s,e)=>s+Number(e.net_pct),0)/x.length:NaN;\n $(\'core8_matrix\').innerHTML=base.flatMap(([label,key,arm])=>{\n  const completed=responses.flatMap(r=>r[key]?.[arm]||[])\n   .filter(e=>e.filter_status===\'PASS\'&&core7Done(e));\n  const cells=atrs.flatMap(([a,aLabel])=>widths.map(([m,mLabel])=>{\n   const rows=completed.filter(e=>{const c=core8Metrics(e);\n    return c.core8_btc_atr_band===a&&c.core8_break_margin_band===m;});\n   const win=rows.filter(e=>Number(e.net_pct)>0).length,avg=mean(rows);\n   return `<tr><td>${label}</td><td>${aLabel}</td><td>${mLabel}</td><td>${rows.length}</td><td>${win}</td><td>${rows.length-win}</td><td>${rows.length?(100*win/rows.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td></tr>`;\n  }));\n  const missing=completed.filter(e=>{const c=core8Metrics(e);\n   return c.core8_btc_atr_band===\'UNKNOWN\'||c.core8_break_margin_band===\'UNKNOWN\';});\n  const win=missing.filter(e=>Number(e.net_pct)>0).length,avg=mean(missing);\n  cells.push(`<tr><td>${label}</td><td colspan="2">판정 불가(누락 데이터)</td><td>${missing.length}</td><td>${win}</td><td>${missing.length-win}</td><td>${missing.length?(100*win/missing.length).toFixed(1)+\'%\':\'—\'}</td><td>${pct(avg)}</td></tr>`);\n  return cells;\n }).join(\'\');\n $(\'core8_summary\').innerHTML=CORE8_DEFS.map(([id,parent,key,label])=>{\n  const all=responses.flatMap(r=>r.shadow_core8?.[id]||[]);\n  const eligible=all.filter(e=>![\'BASE_REJECT\',\'BASE_UNKNOWN\'].includes(e.filter_status));\n  const passed=eligible.filter(e=>e.filter_status===\'PASS\'),done=passed.filter(core7Done);\n  const win=done.filter(e=>Number(e.net_pct)>0).length,avg=mean(done);\n  const reject=eligible.filter(e=>e.filter_status===\'REJECT\'&&core7Done(e));\n  const avoided=reject.filter(e=>Number(e.net_pct)<=0).length;\n  const missed=reject.filter(e=>Number(e.net_pct)>0).length;\n  const unknown=eligible.filter(e=>e.filter_status===\'NOT_EVALUABLE\').length;\n  const groups=new Map();for(const e of done){const day=String(e.signal_time_kst||\'\').slice(0,10);\n   if(!groups.has(day))groups.set(day,[]);groups.get(day).push(e);}\n  let positives=0;for(const dayRows of groups.values())if(mean(dayRows)>0)positives++;\n  return `<tr><td>${label}</td><td>${eligible.length}</td><td>${passed.length}</td><td>${done.length}</td><td>${win}</td><td>${done.length-win}</td><td>${done.length?(100*win/done.length).toFixed(1)+\'%\':\'—\'}</td><td class="${avg>=0?\'positive\':\'negative\'}">${pct(avg)}</td><td>${avoided}</td><td>${missed}</td><td>${unknown}</td><td>${positives}/${groups.size}</td></tr>`;\n }).join(\'\');\n}\nasync function api(url,data){const ctl=new AbortController(),to=setTimeout(()=>ctl.abort(),55000);try{const r=await fetch(url,{method:data?\'POST\':\'GET\',headers:{\'Content-Type\':\'application/json\'},body:data?JSON.stringify(data):undefined,cache:\'no-store\',signal:ctl.signal});const obj=await r.json();if(!r.ok||!obj.ok)throw Error(obj.error||`HTTP ${r.status}`);return obj;}finally{clearTimeout(to)}}\n$(\'run\').onclick=async()=>{if($(\'run\').disabled)return;const day=$(\'day\').value,scope=$(\'scope\').value,fee=Number($(\'fee\').value),slip=Number($(\'slip\').value);\n if(!day||!Number.isFinite(fee)||!Number.isFinite(slip)||fee<0||fee>0.5||slip<0||slip>0.5)return alert(\'날짜·비용 설정을 확인하세요.\');\n $(\'run\').disabled=true;$(\'csv\').disabled=true;responses=[];failures=[];btcContext=null;btcStatus=\'LOADING\';started=performance.now();paint();$(\'status\').textContent=\'종목 목록 준비 중\';\n try{const info=await api(\'/api/v2core/universe\');const count=scope===\'all\'?info.items.length:Number(scope);const markets=info.items.slice(0,count);\n try{const marketInfo=await api(\'/api/v2core/btc-context\',{date:day});\n  btcContext=marketInfo.context;btcStatus=\'OK\';\n }catch(e){btcStatus=\'조회 실패: \'+String(e.message||e).slice(0,90);btcContext=null;}\n let cursor=0,done=0;const total=markets.length;$(\'progress\').max=total;$(\'progress\').value=0;\n async function work(){while(cursor<total){const market=markets[cursor++];try{const out=await api(\'/api/v2core/evaluate\',{market,date:day,fee,slip});out.result.shadow_core7=deriveCore7(out.result);out.result.shadow_core8=deriveCore8(out.result);responses.push(out.result)}catch(e){failures.push({market,error:String(e.message||e)})}\n done++;$(\'progress\').value=done;$(\'status\').textContent=`${done}/${total} 검사 · 성공 ${responses.length} · 오류 ${failures.length}`;if(done%5===0||done===total)paint();}}\n await Promise.all(Array.from({length:Math.min(4,total)},work));paint();$(\'csv\').disabled=responses.length===0;$(\'status\').textContent=failures.length?`일부 오류 ${failures.length}건. CSV에는 성공한 종목만 포함됩니다. 누락을 확인하세요.`:`✅ 완료 ${responses.length}종목, 오류 0건`;\n }catch(e){$(\'status\').textContent=\'검사 중단: \'+e.message;}finally{$(\'run\').disabled=false;}};\nconst csvCell=x=>\'"\'+String(x??\'\').replace(/"/g,\'""\')+\'"\';\n$(\'csv\').onclick=()=>{const cols=[\'version\',\'date\',\'market\',\'strategy\',\'base_id\',\'signal_time_kst\',\'entry_time_kst\',\'outcome\',\'entry\',\'exit\',\'net_pct\',\'minutes\',\'reason\',\'close_position\',\'body_ratio\',\'upper_wick_ratio\',\'break_margin_pct\',\'connors_rsi2_before\',\'connors_rsi2_signal\',\'connors_sma50\',\'connors_sma50_slope3_pct\',\'pattern_volume_ratio\',\'source_strategy\',\'filter_pass\',\'filter_status\',\'filter_reason\',\'shadow_threshold\',\'volume_ratio20\',\'ema20_slope3_pct\',\'atr14_pct\',\'filter_metric\',\'filter_operator\',\'filter_threshold\',\'filter_value\',\'comparison_base\',\'quality_body_min\',\'quality_upper_wick_max\',\'quality_break_margin_min_pct\',\'quality_body_ok\',\'quality_upper_ok\',\'quality_margin_ok\',\'core5_rule_name\',\'core5_active_rules\',\'core5_margin_ok\',\'core5_wick_ok\',\'local_sma50\',\'local_sma50_slope3_pct\',\'local_close_vs_sma50_pct\',\'local_uptrend\',\'local_regime\',\'local_low_volatility\',\'core6_atr_max_pct\',\'core6_wick_max\',\'core6_wick_ok\',\'core6_rule_name\',\'btc_regime\',\'btc_ret60_pct\',\'btc_atr14_pct\',\'btc_regime_threshold_pct\',\'btc_context_source\',\'core7_rule_name\',\'core8_btc_atr_band\',\'core8_break_margin_band\',\'core8_btc_quiet_max_pct\',\'core8_mid_margin_min_pct\',\'core8_mid_margin_max_pct\',\'core8_rule_name\',\'core9_path_status\',\'core9_path_reason\',\'core9_mfe_pct\',\'core9_mae_pct\',\'core9_mfe_first_min\',\'core9_mae_first_min\',\'core9_net_15m_pct\',\'core9_net_30m_pct\',\'core9_net_60m_pct\',\'core9_tp_pct\',\'core9_sl_pct\',\'core9_delay_minutes\',\'core9_base_outcome\',\'core9_base_net_pct\',\'core10_timeframe_minutes\',\'core10_ma_periods\',\'core10_ma_window_minutes\',\'core10_compression_bars\',\'core10_compression_window_minutes\',\'core10_ema_periods\',\'core10_ema_window_minutes\',\'core10_research_only\',\'core11_volume_ratio20_10m\',\'core11_atr14_10m_pct\',\'core11_path_status\',\'core11_path_reason\',\'core11_reference\',\'core11_hold_max_minutes\',\'core11_hold_outcome\',\'core11_hold_net_pct\',\'core11_hold_minutes\',\'core11_hold_15m_outcome\',\'core11_hold_15m_net_pct\',\'core11_hold_15m_minutes\',\'core11_hold_30m_outcome\',\'core11_hold_30m_net_pct\',\'core11_hold_30m_minutes\',\'core11_hold_60m_outcome\',\'core11_hold_60m_net_pct\',\'core11_hold_60m_minutes\',\'core12_path_status\',\'core12_path_reason\',\'core12_entry\',\'core12_net_target_pct\',\'core12_full_window_minutes\',\'core12_hold_max_minutes\',\'core12_hold_net_pct\',\'core12_hold_label\',\'core12_hold_target_reached\',\'core12_hold_first_target_min\',\'core12_hold_15m_net_pct\',\'core12_hold_30m_net_pct\',\'core12_hold_60m_net_pct\',\'core12_hold_120m_net_pct\',\'core12_hold_180m_net_pct\',\'core12_hold_240m_net_pct\',\'core13_rule\',\'core13_path_status\',\'core13_path_reason\',\'core13_full_path\',\'core13_entry\',\'core13_tp_price\',\'core13_stop_price\',\'core13_target_net_pct\',\'core13_stop_gross_pct\',\'core13_first_tp_min\',\'core13_first_sl_min\',\'core13_first_touch\',\'core13_early_confirmed\',\'core13_outcome\',\'core13_label\',\'core13_net_pct\',\'core13_minutes\',\'core13_exit_price\',\'core13_exit_time_kst\',\'core13_status\',\'core13_reason\',\n\'core14_rule\',\'core14_full_path\',\'core14_path_reason\',\'core14_atr14_10m_abs\',\'core14_atr14_10m_pct_entry\',\'core14_atr_stop_price\',\'core14_atr_data_status\',\'core14_early_confirmed\',\'core14_outcome\',\'core14_label\',\'core14_net_pct\',\'core14_minutes\',\'core14_exit_price\',\'core14_exit_time_kst\',\'core14_status\',\'core14_reason\',\n\'core15_rule\',\'core15_full_path\',\'core15_path_reason\',\'core15_early_confirmed\',\'core15_tp1_price\',\'core15_tp5_price\',\'core15_tp10_price\',\'core15_be_price\',\'core15_outcome\',\'core15_label\',\'core15_net_pct\',\'core15_minutes\',\'core15_status\',\'core15_reason\',\'core15_tp1_filled\',\'core15_tp5_filled\',\'core15_tp10_filled\',\'core15_be_triggered\',\'core15_ambiguous_same_bar\',\'core15_tp1_min\',\'core15_tp5_min\',\'core15_tp10_min\',\'core15_be_min\',\'core15_partial_realized_net_pct\',\'core15_remaining_fraction\',\'core15_exit_legs\',\n\'core16_rule\',\'core16_full_path\',\'core16_path_reason\',\'core16_early_confirmed\',\'core16_tp1_price\',\'core16_tp5_price\',\'core16_tp10_price\',\'core16_be_price\',\'core16_outcome\',\'core16_label\',\'core16_net_pct\',\'core16_minutes\',\'core16_status\',\'core16_reason\',\'core16_tp1_filled\',\'core16_tp5_filled\',\'core16_tp10_filled\',\'core16_be_triggered\',\'core16_ambiguous_same_bar\',\'core16_tp1_min\',\'core16_tp5_min\',\'core16_tp10_min\',\'core16_be_min\',\'core16_partial_realized_net_pct\',\'core16_remaining_fraction\',\'core16_exit_legs\',\n\'core17_rule\',\'core17_full_path\',\'core17_path_reason\',\'core17_early_confirmed\',\n\'core17_tp1_price\',\'core17_tp5_price\',\'core17_tp10_price\',\'core17_be_price\',\n\'core17_stop_price\',\'core17_stop_gross_pct\',\'core17_stop_triggered\',\n\'core17_stop_min\',\'core17_stop_tp_same_bar\',\'core17_initial_stop\',\n\'core17_outcome\',\'core17_label\',\'core17_net_pct\',\'core17_minutes\',\'core17_status\',\'core17_reason\',\n\'core17_tp1_filled\',\'core17_tp5_filled\',\'core17_tp10_filled\',\n\'core17_be_triggered\',\'core17_ambiguous_same_bar\',\n\'core17_tp1_min\',\'core17_tp5_min\',\'core17_tp10_min\',\'core17_be_min\',\n\'core17_partial_realized_net_pct\',\'core17_remaining_fraction\',\'core17_exit_legs\',\n\'core18_rule\',\'core18_full_path\',\'core18_path_reason\',\'core18_early_confirmed\',\n\'core18_stop_gross_pct\',\'core18_stop_price\',\'core18_stop_triggered\',\'core18_stop_min\',\'core18_stop_tp_same_bar\',\n\'core18_outcome\',\'core18_label\',\'core18_net_pct\',\'core18_minutes\',\'core18_status\',\'core18_reason\',\n\'core18_tp1_filled\',\'core18_tp5_filled\',\'core18_tp10_filled\',\'core18_be_triggered\',\'core18_ambiguous_same_bar\',\n\'core18_tp1_min\',\'core18_tp5_min\',\'core18_tp10_min\',\'core18_be_min\',\n\'core18_partial_realized_net_pct\',\'core18_remaining_fraction\',\'core18_exit_legs\',\n\'core19_rule\',\'core19_atr_data_status\',\'core19_atr14_10m_abs\',\'core19_atr_pct_entry\',\'core19_atr_multiple\',\n\'core19_atr_ready\',\'core19_full_path\',\'core19_path_reason\',\'core19_early_confirmed\',\n\'core19_stop_gross_pct\',\'core19_stop_price\',\'core19_stop_triggered\',\'core19_stop_min\',\'core19_stop_tp_same_bar\',\n\'core19_clamped_min\',\'core19_clamped_max\',\'core19_outcome\',\'core19_label\',\'core19_net_pct\',\n\'core19_minutes\',\'core19_status\',\'core19_reason\',\'core19_tp1_filled\',\'core19_tp5_filled\',\'core19_tp10_filled\',\n\'core19_be_triggered\',\'core19_ambiguous_same_bar\',\'core19_tp1_min\',\'core19_tp5_min\',\n\'core19_tp10_min\',\'core19_be_min\',\'core19_partial_realized_net_pct\',\n\'core19_remaining_fraction\',\'core19_exit_legs\',\n\'core20_status\',\'core20_reason\',\'core20_dip_trigger_pct\',\'core20_trigger_time_kst\',\'core20_trigger_minutes\',\'core20_trigger_gross_pct\',\'core20_liquidity_tier\',\'core20_primary_tf\',\'core20_secondary_tf\',\'core20_5m_slots\',\'core20_5m_coverage\',\'core20_avg_5m_turnover_krw\',\'core20_decision\',\'core20_primary_state\',\'core20_secondary_state\',\'core20_sma3_slope_pct\',\'core20_volume_ratio4\',\'core20_decision_reason\',\'core20_recheck_15\',\'core20_recheck_30\',\'core20_gross_15m_pct\',\'core20_gross_30m_pct\',\'core20_gross_60m_pct\',\'core20_worst_low_15m_pct\',\'core20_worst_low_30m_pct\',\'core20_worst_low_60m_pct\',\'core20_followup_15_status\',\'core20_followup_30_status\',\'core20_followup_60_status\',\'core20_be_status\',\'core20_be_outcome\',\'core20_be_net_pct\',\'core20_3m_fetch_note\'];const lines=[cols.join(\',\')];\n for(const r of responses){for(const id of [\'A\',\'B\',\'K\'])for(const e of r.events?.[id]||[]){const row={version:\'V2.CORE.20\',date:r.date,...e,...core8Metrics(e),strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const id of [\'A_V\',\'B_V\',\'K_V\'])for(const e of r.shadow?.[id]||[]){const row={version:\'V2.CORE.20\',date:r.date,...e,...core8Metrics(e),strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const id of [\'A_E\',\'B_E\',\'K_ATR\'])for(const e of r.shadow_trend_atr?.[id]||[]){const row={version:\'V2.CORE.20\',date:r.date,...e,...core8Metrics(e),strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const id of [\'A_EQ\',\'B_EQ\'])for(const e of r.shadow_breakout?.[id]||[]){const row={version:\'V2.CORE.20\',date:r.date,...e,...core8Metrics(e),strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const id of [\'A_EM\',\'B_ENB\',\'B_EM\'])for(const e of r.shadow_core5?.[id]||[]){const row={version:\'V2.CORE.20\',date:r.date,...e,...core8Metrics(e),strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const id of [\'A_EQ_TR\',\'A_EQ_ATR\',\'A_EQ_R\',\'B_EW\'])for(const e of r.shadow_core6?.[id]||[]){const row={version:\'V2.CORE.20\',date:r.date,...e,...core8Metrics(e),strategy:id};lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const [id] of CORE7_DEFS)for(const e of r.shadow_core7?.[id]||[]){\n   const row={version:\'V2.CORE.20\',date:r.date,...e,...core8Metrics(e),strategy:id};\n   lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const [id] of CORE8_DEFS)for(const e of r.shadow_core8?.[id]||[]){\n   const row={version:\'V2.CORE.20\',date:r.date,...e,...core8Metrics(e),strategy:id};\n   lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));}}\n for(const r of responses){for(const id of [\'A_EQ\',\'B_E\'])for(const e of r.core9_paths?.[id]||[]){\n   const shared={version:\'V2.CORE.20\',date:r.date,market:e.market,base_id:e.base_id,signal_time_kst:e.signal_time_kst,entry_time_kst:e.entry_time_kst,\n    core9_path_status:e.path_status,core9_path_reason:e.path_reason,core9_mfe_pct:e.mfe_pct,core9_mae_pct:e.mae_pct,\n    core9_mfe_first_min:e.mfe_first_min,core9_mae_first_min:e.mae_first_min,core9_net_15m_pct:e.net_15m_pct,\n    core9_net_30m_pct:e.net_30m_pct,core9_net_60m_pct:e.net_60m_pct,core9_base_outcome:e.base_outcome,\n    core9_base_net_pct:e.base_net_pct};\n   lines.push(cols.map(k=>csvCell(({...shared,strategy:\'CORE9_PATH_\'+id})[k])).join(\',\'));\n   for(const tp of [.5,.75,1,1.5])for(const sl of [.5,.75,1]){\n    const key=\'tp\'+String(Math.round(tp*100)).padStart(3,\'0\')+\'_sl\'+String(Math.round(sl*100)).padStart(3,\'0\');\n    const v=e.grid?.[key];if(!v)continue;\n    const gridRow={...shared,strategy:\'CORE9_EXIT_\'+id,core9_tp_pct:tp,core9_sl_pct:sl,\n      outcome:v.outcome,net_pct:v.net_pct,minutes:v.minutes};\n    lines.push(cols.map(k=>csvCell(gridRow[k])).join(\',\'));\n   }\n   const delayed={...shared,strategy:\'CORE9_DELAY5_\'+id,core9_tp_pct:1.5,core9_sl_pct:1,\n    core9_delay_minutes:5,entry_time_kst:e.delay5_entry_time_kst,\n    outcome:e.delay5_outcome,net_pct:e.delay5_net_pct};\n   lines.push(cols.map(k=>csvCell(delayed[k])).join(\',\'));\n }}\n for(const r of responses){const groups=r.core10_timeframes?.groups||{};\n  for(const [strategy,items] of Object.entries(groups))for(const e of items){\n   const row={version:\'V2.CORE.20\',date:r.date,...e,strategy,\n    core10_timeframe_minutes:e.timeframe_minutes,core10_ma_periods:e.ma_periods,\n    core10_ma_window_minutes:e.ma_window_minutes,core10_compression_bars:e.compression_bars,\n    core10_compression_window_minutes:e.compression_window_minutes,\n    core10_ema_periods:e.ema_periods,core10_ema_window_minutes:e.ema_window_minutes,\n    core10_research_only:e.research_only,\n    ...(strategy===\'B_10M_MA03\'?core7Metrics(e):{})};\n   lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));\n  }\n }\n\n for(const r of responses){for(const e of r.core11_focus?.events||[]){\n   const btc=core7Metrics(e);\n   for(const h of [15,30,60]){\n    const row={version:\'V2.CORE.20\',date:r.date,...e,...btc,\n      strategy:\'CORE11_HOLD_B_10M_MA03\',source_strategy:\'B_10M_MA03\',\n      core11_hold_max_minutes:h,\n      core11_hold_outcome:e[`core11_hold_${h}m_outcome`],\n      core11_hold_net_pct:e[`core11_hold_${h}m_net_pct`],\n      core11_hold_minutes:e[`core11_hold_${h}m_minutes`],\n      outcome:e[`core11_hold_${h}m_outcome`],\n      net_pct:e[`core11_hold_${h}m_net_pct`],\n      minutes:e[`core11_hold_${h}m_minutes`]};\n    lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));\n   }\n }}\n for(const r of responses){for(const e of r.core12_focus?.events||[]){\n  const btc=core7Metrics(e);\n  for(const h of [15,30,60,120,180,240]){\n   const prefix=`core12_hold_${h}m_`;\n   const row={version:\'V2.CORE.20\',date:r.date,...e,...btc,\n     strategy:\'CORE12_HOLD_B_10M_MA03\',source_strategy:\'B_10M_MA03\',\n     core12_hold_max_minutes:h,\n     core12_hold_net_pct:e[prefix+\'net_pct\'],\n     core12_hold_label:e[prefix+\'label\'],\n     core12_hold_target_reached:e[prefix+\'target_reached\'],\n     core12_hold_first_target_min:e[prefix+\'first_target_min\'],\n     outcome:e[prefix+\'label\'],net_pct:e[prefix+\'net_pct\'],minutes:h};\n   lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));\n  }\n }}\n for(const r of responses){for(const e of r.core13_focus?.events||[]){\n  const btc=core7Metrics(e);\n  for(const rule of [\'TP05_NO_SL\',\'TP05_SL1\',\'HOLD240\']){\n   const o=e.core13_scenarios?.[rule]||{};\n   const row={version:\'V2.CORE.20\',date:r.date,market:e.market,base_id:e.base_id,\n    signal_time_kst:e.signal_time_kst,entry_time_kst:e.entry_time_kst,...btc,\n    strategy:\'CORE13_EXIT_B_10M_MA03\',source_strategy:\'B_10M_MA03\',\n    core13_rule:rule,core13_path_status:e.core13_path_status,\n    core13_path_reason:e.core13_path_reason,core13_full_path:e.core13_full_path,\n    core13_entry:e.core13_entry,core13_tp_price:e.core13_tp_price,\n    core13_stop_price:e.core13_stop_price,core13_target_net_pct:e.core13_target_net_pct,\n    core13_stop_gross_pct:e.core13_stop_gross_pct,core13_first_tp_min:e.core13_first_tp_min,\n    core13_first_sl_min:e.core13_first_sl_min,core13_first_touch:e.core13_first_touch,\n    core13_early_confirmed:e.core13_early_confirmed,\n    core13_outcome:o.outcome,core13_label:o.label,core13_net_pct:o.net_pct,\n    core13_minutes:o.minutes,core13_exit_price:o.exit_price,\n    core13_exit_time_kst:o.exit_time_kst,core13_status:o.status,core13_reason:o.reason,\n    outcome:o.outcome,net_pct:o.net_pct,entry:e.core13_entry,exit:o.exit_price,\n    minutes:o.minutes,reason:o.reason};\n   lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));\n  }\n }}\n for(const r of responses){for(const e of r.core14_focus?.events||[]){\n  const btc=core7Metrics(e);\n  for(const rule of [\'TP05_NO_SL\',\'DIAG30_NEG05\',\'DIAG60_NEG05\',\'ATR14_10M_X15\']){\n   const o=e.core14_scenarios?.[rule]||{};\n   const row={version:\'V2.CORE.20\',date:r.date,market:e.market,base_id:e.base_id,\n    signal_time_kst:e.signal_time_kst,entry_time_kst:e.entry_time_kst,...btc,\n    strategy:\'CORE14_EXIT_B_10M_MA03\',source_strategy:\'B_10M_MA03\',\n    core14_rule:rule,core14_full_path:e.core14_full_path,\n    core14_path_reason:e.core14_path_reason,\n    core14_atr14_10m_abs:e.core14_atr14_10m_abs,\n    core14_atr14_10m_pct_entry:e.core14_atr14_10m_pct_entry,\n    core14_atr_stop_price:e.core14_atr_stop_price,\n    core14_atr_data_status:e.core14_atr_data_status,\n    core14_early_confirmed:e.core14_early_confirmed,\n    core14_outcome:o.outcome,core14_label:o.label,core14_net_pct:o.net_pct,\n    core14_minutes:o.minutes,core14_exit_price:o.exit_price,\n    core14_exit_time_kst:o.exit_time_kst,core14_status:o.status,core14_reason:o.reason,\n    outcome:o.outcome,net_pct:o.net_pct,entry:e.core13_entry,exit:o.exit_price,\n    minutes:o.minutes,reason:o.reason};\n   lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));\n  }\n }}\n for(const r of responses){for(const e of r.core15_focus?.events||[]){\n  const btc=core7Metrics(e);\n  for(const rule of [\'TP05_NO_SL\',\'SCALE_1_5_10\',\'SCALE_1_5_10_BE\']){\n   const o=e.core15_scenarios?.[rule]||{};\n   const tp=o.tp_prices||[];\n   const row={version:\'V2.CORE.20\',date:r.date,market:e.market,base_id:e.base_id,\n    signal_time_kst:e.signal_time_kst,entry_time_kst:e.entry_time_kst,...btc,\n    strategy:\'CORE15_EXIT_B_10M_MA03\',source_strategy:\'B_10M_MA03\',\n    core15_rule:rule,core15_full_path:e.core15_full_path,\n    core15_path_reason:e.core15_path_reason,\n    core15_early_confirmed:e.core15_early_confirmed,\n    core15_tp1_price:tp[0],core15_tp5_price:tp[1],core15_tp10_price:tp[2],\n    core15_be_price:o.be_price,core15_outcome:o.outcome,\n    core15_label:o.label,core15_net_pct:o.net_pct,\n    core15_minutes:o.minutes,core15_status:o.status,core15_reason:o.reason,\n    core15_tp1_filled:o.tp1_filled,core15_tp5_filled:o.tp5_filled,\n    core15_tp10_filled:o.tp10_filled,core15_be_triggered:o.be_triggered,\n    core15_ambiguous_same_bar:o.ambiguous_same_bar,\n    core15_tp1_min:o.tp1_min,core15_tp5_min:o.tp5_min,\n    core15_tp10_min:o.tp10_min,core15_be_min:o.be_min,\n    core15_partial_realized_net_pct:o.partial_realized_net_pct,\n    core15_remaining_fraction:o.remaining_fraction,\n    core15_exit_legs:o.exit_legs?JSON.stringify(o.exit_legs):\'\',\n    outcome:o.outcome,net_pct:o.net_pct,entry:e.core13_entry,\n    minutes:o.minutes,reason:o.reason};\n   lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));\n  }\n }}\n for(const r of responses){for(const e of r.core16_focus?.events||[]){\n  const btc=core7Metrics(e);\n  for(const rule of [\'TP05_NO_SL\',\'SCALE_1_5_10\',\'SCALE_1_5_10_BE\',\'SCALE_05_5_10\',\'SCALE_05_5_10_BE\']){\n   const o=e.core16_scenarios?.[rule]||{},tp=o.tp_prices||[];\n   const row={version:\'V2.CORE.20\',date:r.date,market:e.market,base_id:e.base_id,\n    signal_time_kst:e.signal_time_kst,entry_time_kst:e.entry_time_kst,...btc,\n    strategy:\'CORE16_EXIT_B_10M_MA03\',source_strategy:\'B_10M_MA03\',\n    core16_rule:rule,core16_full_path:e.core16_full_path,\n    core16_path_reason:e.core16_path_reason,\n    core16_early_confirmed:e.core16_early_confirmed,\n    core16_tp1_price:tp[0],core16_tp5_price:tp[1],core16_tp10_price:tp[2],\n    core16_be_price:o.be_price,core16_outcome:o.outcome,\n    core16_label:o.label,core16_net_pct:o.net_pct,\n    core16_minutes:o.minutes,core16_status:o.status,core16_reason:o.reason,\n    core16_tp1_filled:o.tp1_filled,core16_tp5_filled:o.tp5_filled,\n    core16_tp10_filled:o.tp10_filled,core16_be_triggered:o.be_triggered,\n    core16_ambiguous_same_bar:o.ambiguous_same_bar,\n    core16_tp1_min:o.tp1_min,core16_tp5_min:o.tp5_min,\n    core16_tp10_min:o.tp10_min,core16_be_min:o.be_min,\n    core16_partial_realized_net_pct:o.partial_realized_net_pct,\n    core16_remaining_fraction:o.remaining_fraction,\n    core16_exit_legs:o.exit_legs?JSON.stringify(o.exit_legs):\'\',\n    outcome:o.outcome,net_pct:o.net_pct,entry:e.core13_entry,\n    minutes:o.minutes,reason:o.reason};\n   lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));\n  }\n }}\n for(const r of responses){for(const e of r.core17_focus?.events||[]){\n  const btc=core7Metrics(e);\n  for(const rule of [\'TP05_NO_SL\',\'SCALE_1_5_10\',\'SCALE_1_5_10_BE\',\n    \'SCALE_05_5_10\',\'SCALE_05_5_10_BE\',\'SCALE_05_5_10_BE_SL1\']){\n   const o=e.core17_scenarios?.[rule]||{},tp=o.tp_prices||[];\n   const row={version:\'V2.CORE.20\',date:r.date,market:e.market,base_id:e.base_id,\n    signal_time_kst:e.signal_time_kst,entry_time_kst:e.entry_time_kst,...btc,\n    strategy:\'CORE17_EXIT_B_10M_MA03\',source_strategy:\'B_10M_MA03\',\n    core17_rule:rule,core17_full_path:e.core17_full_path,\n    core17_path_reason:e.core17_path_reason,\n    core17_early_confirmed:e.core17_early_confirmed,\n    core17_tp1_price:tp[0],core17_tp5_price:tp[1],core17_tp10_price:tp[2],\n    core17_be_price:o.be_price,core17_stop_price:o.stop_price,\n    core17_stop_gross_pct:o.stop_gross_pct,\n    core17_stop_triggered:o.stop_triggered,core17_stop_min:o.stop_min,\n    core17_stop_tp_same_bar:o.stop_tp_same_bar,\n    core17_initial_stop:o.initial_stop,\n    core17_outcome:o.outcome,core17_label:o.label,core17_net_pct:o.net_pct,\n    core17_minutes:o.minutes,core17_status:o.status,core17_reason:o.reason,\n    core17_tp1_filled:o.tp1_filled,core17_tp5_filled:o.tp5_filled,\n    core17_tp10_filled:o.tp10_filled,core17_be_triggered:o.be_triggered,\n    core17_ambiguous_same_bar:o.ambiguous_same_bar,\n    core17_tp1_min:o.tp1_min,core17_tp5_min:o.tp5_min,\n    core17_tp10_min:o.tp10_min,core17_be_min:o.be_min,\n    core17_partial_realized_net_pct:o.partial_realized_net_pct,\n    core17_remaining_fraction:o.remaining_fraction,\n    core17_exit_legs:o.exit_legs?JSON.stringify(o.exit_legs):\'\',\n    outcome:o.outcome,net_pct:o.net_pct,entry:e.core13_entry,\n    minutes:o.minutes,reason:o.reason};\n   lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));\n  }\n }}\n for(const r of responses){for(const e of r.core18_focus?.events||[]){\n  const btc=core7Metrics(e);\n  const defs=[[\'SCALE_05_5_10_BE\',0],[\'SCALE_05_5_10_BE_SL1\',1],\n   [\'SCALE_05_5_10_BE_SL15\',1.5],[\'SCALE_05_5_10_BE_SL2\',2],\n   [\'SCALE_05_5_10_BE_SL25\',2.5],[\'SCALE_05_5_10_BE_SL3\',3]];\n  for(const [rule,stopPct] of defs){\n   const o=e.core18_scenarios?.[rule]||{};\n   const row={version:\'V2.CORE.20\',date:r.date,market:e.market,base_id:e.base_id,\n    signal_time_kst:e.signal_time_kst,entry_time_kst:e.entry_time_kst,...btc,\n    strategy:\'CORE18_EXIT_B_10M_MA03\',source_strategy:\'B_10M_MA03\',\n    core18_rule:rule,core18_full_path:e.core18_full_path,\n    core18_path_reason:e.core18_path_reason,core18_early_confirmed:e.core18_early_confirmed,\n    core18_stop_gross_pct:stopPct,core18_stop_price:o.stop_price,\n    core18_stop_triggered:o.stop_triggered||false,core18_stop_min:o.stop_min,\n    core18_stop_tp_same_bar:o.stop_tp_same_bar||false,\n    core18_outcome:o.outcome,core18_label:o.label,core18_net_pct:o.net_pct,\n    core18_minutes:o.minutes,core18_status:o.status,core18_reason:o.reason,\n    core18_tp1_filled:o.tp1_filled,core18_tp5_filled:o.tp5_filled,\n    core18_tp10_filled:o.tp10_filled,core18_be_triggered:o.be_triggered,\n    core18_ambiguous_same_bar:o.ambiguous_same_bar,\n    core18_tp1_min:o.tp1_min,core18_tp5_min:o.tp5_min,\n    core18_tp10_min:o.tp10_min,core18_be_min:o.be_min,\n    core18_partial_realized_net_pct:o.partial_realized_net_pct,\n    core18_remaining_fraction:o.remaining_fraction,\n    core18_exit_legs:o.exit_legs?JSON.stringify(o.exit_legs):\'\',\n    outcome:o.outcome,net_pct:o.net_pct,entry:e.core13_entry,\n    minutes:o.minutes,reason:o.reason};\n   lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));\n  }\n }}\n for(const r of responses){for(const e of r.core19_focus?.events||[]){\n  const btc=core7Metrics(e);\n  const defs=[[\'SCALE_05_5_10_BE\',0],[\'SCALE_05_5_10_BE_SL1\',1],\n   [\'SCALE_05_5_10_BE_SL15\',1.5],[\'SCALE_05_5_10_BE_SL2\',2],\n   [\'SCALE_05_5_10_BE_SL25\',2.5],[\'SCALE_05_5_10_BE_SL3\',3],\n   [\'SCALE_05_5_10_BE_ATR1\',null],[\'SCALE_05_5_10_BE_ATR15\',null],\n   [\'SCALE_05_5_10_BE_ATR2\',null],[\'SCALE_05_5_10_BE_ATR25\',null]];\n  for(const [rule,fixed] of defs){\n   const o=e.core19_scenarios?.[rule]||{};\n   const row={version:\'V2.CORE.20\',date:r.date,market:e.market,base_id:e.base_id,\n    signal_time_kst:e.signal_time_kst,entry_time_kst:e.entry_time_kst,...btc,\n    strategy:\'CORE19_EXIT_B_10M_MA03\',source_strategy:\'B_10M_MA03\',\n    core19_rule:rule,core19_atr_data_status:e.core19_atr_status,\n    core19_atr14_10m_abs:e.core19_atr14_10m_abs,\n    core19_atr_pct_entry:e.core19_atr_pct_entry,\n    core19_atr_multiple:o.atr_multiple??\'\',core19_atr_ready:e.core19_atr_ready,\n    core19_full_path:e.core19_full_path,core19_path_reason:e.core19_path_reason,\n    core19_early_confirmed:e.core19_early_confirmed,\n    core19_stop_gross_pct:fixed===0?0:(o.stop_gross_pct??\'\'),\n    core19_stop_price:o.stop_price,core19_stop_triggered:o.stop_triggered||false,\n    core19_stop_min:o.stop_min,core19_stop_tp_same_bar:o.stop_tp_same_bar||false,\n    core19_clamped_min:o.atr_clamped_min??\'\',core19_clamped_max:o.atr_clamped_max??\'\',\n    core19_outcome:o.outcome,core19_label:o.label,core19_net_pct:o.net_pct,\n    core19_minutes:o.minutes,core19_status:o.status,core19_reason:o.reason,\n    core19_tp1_filled:o.tp1_filled,core19_tp5_filled:o.tp5_filled,\n    core19_tp10_filled:o.tp10_filled,core19_be_triggered:o.be_triggered,\n    core19_ambiguous_same_bar:o.ambiguous_same_bar,\n    core19_tp1_min:o.tp1_min,core19_tp5_min:o.tp5_min,\n    core19_tp10_min:o.tp10_min,core19_be_min:o.be_min,\n    core19_partial_realized_net_pct:o.partial_realized_net_pct,\n    core19_remaining_fraction:o.remaining_fraction,\n    core19_exit_legs:o.exit_legs?JSON.stringify(o.exit_legs):\'\',\n    outcome:o.outcome,net_pct:o.net_pct,entry:e.core13_entry,\n    minutes:o.minutes,reason:o.reason};\n   lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));\n  }\n }}\n for(const r of responses){for(const e of r.core20_focus?.events||[]){\n  const q=e.liquidity||{},d=e.decision||{},f=e.followups||{};\n  const row={version:\'V2.CORE.20\',date:r.date,market:e.market,base_id:e.base_id,\n   signal_time_kst:e.signal_time_kst,entry_time_kst:e.entry_time_kst,\n   strategy:\'CORE20_RECHECK_B_10M_MA03\',source_strategy:\'B_10M_MA03\',\n   core20_status:e.status,core20_reason:e.reason,core20_dip_trigger_pct:0.5,\n   core20_trigger_time_kst:e.trigger_time_kst,core20_trigger_minutes:e.trigger_minutes,\n   core20_trigger_gross_pct:e.trigger_gross_pct,\n   core20_liquidity_tier:q.tier,core20_primary_tf:q.primary_tf,core20_secondary_tf:q.secondary_tf,\n   core20_5m_slots:q.last_60m_slots,core20_5m_coverage:q.coverage_ratio,\n   core20_avg_5m_turnover_krw:q.avg_5m_turnover_krw,\n   core20_decision:d.state,core20_primary_state:d.primary_state,\n   core20_secondary_state:d.secondary_state,core20_sma3_slope_pct:d.primary_sma3_slope_pct,\n   core20_volume_ratio4:d.primary_volume_ratio4,core20_decision_reason:d.reason,\n   core20_recheck_15:e.recheck_15?.state,core20_recheck_30:e.recheck_30?.state,\n   core20_gross_15m_pct:f[\'15\']?.gross_pct,core20_gross_30m_pct:f[\'30\']?.gross_pct,\n   core20_gross_60m_pct:f[\'60\']?.gross_pct,\n   core20_worst_low_15m_pct:f[\'15\']?.worst_low_pct,\n   core20_worst_low_30m_pct:f[\'30\']?.worst_low_pct,\n   core20_worst_low_60m_pct:f[\'60\']?.worst_low_pct,\n   core20_followup_15_status:f[\'15\']?.status,core20_followup_30_status:f[\'30\']?.status,\n   core20_followup_60_status:f[\'60\']?.status,\n   core20_be_status:e.baseline_be_status,core20_be_outcome:e.baseline_be_outcome,\n   core20_be_net_pct:e.baseline_be_net_pct,core20_3m_fetch_note:e.fetch_3m_note};\n  lines.push(cols.map(k=>csvCell(row[k])).join(\',\'));\n }}\n for(const e of failures)lines.push(cols.map(k=>csvCell(({version:\'V2.CORE.20\',date:$(\'day\').value,market:e.market,strategy:\'ERROR\',reason:e.error})[k])).join(\',\'));\n const blob=new Blob([\'\\ufeff\'+lines.join(\'\\n\')],{type:\'text/csv;charset=utf-8\'}),url=URL.createObjectURL(blob),a=document.createElement(\'a\');a.href=url;a.download=\'bithumb_V2_CORE20_LIQUIDITY_RECHECK_3_5_10_15M.csv\';a.click();setTimeout(()=>URL.revokeObjectURL(url),3000);};\n</script></body></html>'

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
            data=evaluate_core_rows(rows,cfg,market,kst,meta,api_get)
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
