"""V2 CORE.35 LIGHT — Momentum +5% / SMA5 pullback research.

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

VERSION = 'V2.CORE.35-LIGHT-PLUS5-SMA5-PULLBACK'
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


def analyze_pullbacks(rows, day, market, fee=.25, slip=0):
    """Ex-ante detection: only bars closed by each signal timestamp.

    Stages: +5% since 00:00 first 5m open -> 2 closes above SMA5 ->
    first close below SMA5 -> optional SMA5 reclaim in <=4 following bars.
    """
    start = datetime.combine(day, dt_time.min, tzinfo=rows[0]['t'].tzinfo) if rows else None
    end = start+timedelta(days=1) if start else None
    coverage = {'market':market, 'date':day.isoformat(), 'bar_count':len(rows),
                'midnight_open_found':False, 'complete_day_bars':0, 'events':0, 'reason':''}
    if not rows:
        coverage['reason']='분봉 없음'
        return {'market':market,'date':day.isoformat(),'coverage':coverage,'events':[]}
    indices = {b['t']: i for i,b in enumerate(rows)}
    midnight_i = indices.get(start)
    if midnight_i is None:
        coverage['reason']='00:00 시작 봉이 없어 당일 상승률 계산 불가'
        return {'market':market,'date':day.isoformat(),'coverage':coverage,'events':[]}
    base = rows[midnight_i]['o']
    coverage['midnight_open_found']=True
    coverage['midnight_open']=base
    events=[]
    reached=False
    last_event_t=None
    session_peak=base
    session_cash=0.0
    ma_cache={}
    for i in range(midnight_i, len(rows)):
        b=rows[i]
        if b['t'].time() >= dt_time(22, 15):
            break  # reserve enough time for reclaim + complete 60m exit within same day
        if b['t']+FIVE > end:
            break
        session_cash += b['cash']
        if not _contiguous(rows,i,7):
            continue
        coverage['complete_day_bars']+=1
        session_peak=max(session_peak,b['h']) # diagnostics at this bar only
        ma = ma_cache.setdefault(i,_ma5(rows,i))
        prev_ma=ma_cache.setdefault(i-1,_ma5(rows,i-1))
        prev2_ma=ma_cache.setdefault(i-2,_ma5(rows,i-2))
        if None in (ma,prev_ma,prev2_ma):
            continue
        prev1,prev2 = rows[i-1],rows[i-2]
        # Activation only after a PRIOR completed close >= +5%, not at future peak.
        active_before=reached
        if _price_pct(b['c'], base)>=5:
            reached=True
        if not active_before:
            continue
        if last_event_t and b['t'] - last_event_t < timedelta(minutes=90):
            continue
        # A formerly +5% coin that has already lost momentum is NOT eligible.
        if _price_pct(prev1['c'], base) < 5:
            continue
        if not (prev2['c'] > prev2_ma and prev1['c'] > prev_ma and b['c'] < ma):
            continue
        # First actual DOWN-CROSS after 2 candles ABOVE SMA5, no hidden future.
        span=rows[max(midnight_i, i-11):i+1]
        cash12=sum(x['cash'] for x in span)
        last_event_t=b['t']
        reclaim_i=None
        for j in range(i+1,min(len(rows),i+5)):
            if rows[j]['t']+FIVE > end or not _contiguous(rows,j,5):
                break
            if rows[j]['t']-rows[j-1]['t'] != FIVE:
                break
            r_ma = _ma5(rows,j)
            if r_ma is not None and rows[j]['c'] > r_ma and rows[j]['c'] > rows[j-1]['c']:
                reclaim_i=j
                break
        e={'market':market, 'date':day.isoformat(),
           'dip_bar_at':b['t'].isoformat(), 'dip_signal_at':(b['t']+FIVE).isoformat(),
           'reclaim_bar_at':rows[reclaim_i]['t'].isoformat() if reclaim_i is not None else '',
           'reclaim_signal_at':(rows[reclaim_i]['t']+FIVE).isoformat() if reclaim_i is not None else '',
           'minutes_to_reclaim':(reclaim_i-i)*5 if reclaim_i is not None else None,
           'day_open':round(base,10),'day_gain_before_dip_pct':round(_price_pct(prev1['c'],base),4),
           'day_gain_at_dip_pct':round(_price_pct(b['c'],base),4),
           'day_high_to_dip_pct':round(_price_pct(b['c'],session_peak),4),
           'dip_below_sma5_pct':round(_price_pct(b['c'],ma),4),
           'dip_body_pct':round(_price_pct(b['c'],b['o']),4),
           'dip_close_position_pct':round(100*(b['c']-b['l'])/(b['h']-b['l']),2) if b['h']>b['l'] else None,
           'prior_60m_trade_krw':round(cash12,2),
           'day_trade_krw_at_dip':round(session_cash,2),
           'dip_sma5':round(ma,10),'reclaim_sma5':round(_ma5(rows,reclaim_i),10) if reclaim_i is not None else None}
        for label, entry_i, signal_i in [('under',i+1,i),('reclaim',reclaim_i+1 if reclaim_i is not None else None,reclaim_i)]:
            if entry_i is not None and (entry_i>=len(rows) or rows[entry_i]['t']-rows[signal_i]['t'] != FIVE):
                entry_i=None  # never enter later than the next scheduled 5m bar
            for target in (.3,.5):
                r=_exit_sim(rows,entry_i,fee,slip,target)
                for key,val in r.items():
                    e[f'{label}_tp{int(target*10):02}_{key}']=val
        if reclaim_i is None:
            e['reclaim_tp03_status']='NO_RECLAIM'
            e['reclaim_tp05_status']='NO_RECLAIM'
            e['reclaim_tp03_reason']='4개 봉 안에 5이평 회복 없음'
            e['reclaim_tp05_reason']='4개 봉 안에 5이평 회복 없음'
        events.append(e)
    coverage['events']=len(events)
    coverage['activated_5pct']=reached
    if not coverage['complete_day_bars']:
        coverage['reason']='연속 5분봉 부족'
    return {'market':market,'date':day.isoformat(),'coverage':coverage,'events':events}

# PAGE injected by packager; no external HTML file required at Render.
CORE_PAGE = '<!doctype html>\n<html lang="ko">\n<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">\n<title>V2 CORE.35 LIGHT · +5% 상승 코인 5이평 눌림 연구</title>\n<style>\n:root{color-scheme:dark;--bg:#0b1020;--panel:#151e31;--edge:#2b3954;--fg:#ecf2ff;--sub:#aab8d0;--blue:#87a4ff;--good:#4ade80;--bad:#ff8797}\n*{box-sizing:border-box}body{background:var(--bg);color:var(--fg);font:14px/1.55 system-ui,"Noto Sans KR",sans-serif;margin:0}main{max-width:1030px;margin:auto;padding:18px 13px 70px}\nh1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:0 0 11px}h3{font-size:14px;margin:14px 0 6px}p{margin:6px 0 12px}a{color:#b3c8ff}.muted{color:var(--sub);font-size:12px}.panel{border:1px solid var(--edge);background:var(--panel);border-radius:14px;padding:16px;margin:13px 0}\n.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:9px}.field{display:flex;flex-direction:column;gap:4px}label{font-size:12px;color:var(--sub)}input,select,button{width:100%;padding:11px;background:#111a2d;border:1px solid #465775;border-radius:8px;color:var(--fg);font:inherit}\nbutton{background:#3765df;font-weight:650;cursor:pointer}button.secondary{background:#273852;border-color:#415470}button:disabled{opacity:.4;cursor:default}.controls{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.controls button{width:auto;flex:1;min-width:105px}\n.kpis{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.kpi{border:1px solid var(--edge);border-radius:9px;padding:13px}.kpi .n{font-size:22px;font-weight:750}.good{color:var(--good)}.bad{color:var(--bad)}.warning{color:#ffd483}\n.tablewrap{overflow:auto;max-width:100%}table{border-collapse:collapse;width:100%;min-width:620px}th,td{font-size:12px;padding:9px;border-bottom:1px solid var(--edge);white-space:nowrap;text-align:right}th:first-child,td:first-child{text-align:left}th{color:#c3d1ec}tr:last-child td{border:0}.note{background:#0c172c;color:#b8c6dd;border-radius:9px;padding:12px;font-size:12px}.pill{display:inline-block;background:#283958;border:1px solid #4c6288;padding:2px 6px;border-radius:5px;font-size:11px}\nprogress{width:100%;height:12px}details{margin:10px 0}summary{cursor:pointer;padding:10px 0;font-weight:650}\n@media(max-width:640px){main{padding:13px 9px 44px}.panel{padding:12px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.field{min-width:0}h1{font-size:20px}button{padding:10px 8px}}\n</style></head>\n<body><main>\n<header><h1>🧪 V2 CORE.35 LIGHT · +5% 상승 코인 5이평 눌림</h1>\n<p class="muted">완전 신규 연구 로직 · KST 00시 시가 대비 +5% · 5분봉 SMA5 · 기존 실전 스캐너/주문 기능 미변경</p>\n<a href="/">← 기존 실전 V2 스캐너로 돌아가기</a></header>\n<section class="panel"><h2>① 새로운 매매 가설</h2>\n<p><span class="pill">01</span> 00:00 이후 해당 코인이 <b>당일 +5% 이상</b>을 달성하고, 눌림 직전 봉도 +5% 이상 유지</p>\n<p><span class="pill">02</span> 5분봉 종가가 <b>5이평(SMA5) 위에 연속 2봉</b> 있었음</p>\n<p><span class="pill">03</span> 이후 종가가 <b>5이평 아래로 내려온 첫 눌림</b>을 포착</p>\n<p><span class="pill">04</span> <b>A: 눌림 다음 봉 시가</b> / <b>B: 4봉 이내 5이평 재회복 다음 봉 시가</b>를 비교</p>\n<div class="note">5이평은 EMA가 아닌 <b>5분봉 단순이동평균(SMA5)</b>이야. 당일 기준가는 00:00 KST 첫 5분봉의 시가이며, +5%는 <b>눌림 발생 이전에 확정</b>되어야 해. 이 단계는 과거 데이터 연구로 자동 매수·실시간 알림이 아니야.</div></section>\n<section class="panel"><h2>② 검사 설정</h2>\n<div class="grid">\n<div class="field"><label for="endDay">마지막 날짜 (KST, 끝난 하루)</label><input id="endDay" type="date"></div>\n<div class="field"><label for="days">연속 날짜 수</label><select id="days"><option value="1" selected>1일 (첫 테스트)</option><option value="3">3일</option><option value="5">5일</option></select></div>\n<div class="field"><label for="scope">분석 코인</label><select id="scope"><option value="all" selected>전체 KRW (권장)</option><option value="40">현재 거래대금 상위 40</option><option value="80">현재 거래대금 상위 80</option></select></div>\n<div class="field"><label for="fee">편도 수수료 (%)</label><input id="fee" type="number" value="0.25" step="0.01" min="0" max="0.5"></div>\n<div class="field"><label for="slip">편도 슬리피지 (%)</label><input id="slip" type="number" value="0" step="0.01" min="0" max="0.5"></div>\n<div class="field"><label for="method">일일 선정 방식</label><select id="method"><option value="reclaim" selected>B · 5이평 회복 확인</option><option value="under">A · 5이평 하향 이탈 직후</option></select></div>\n<div class="field"><label for="target">익절 목표 (순수익)</label><select id="target"><option value="05" selected>+0.5%</option><option value="03">+0.3%</option></select></div>\n<div class="field"><label for="cap">하루 최대 진입</label><select id="cap"><option value="3">3건</option><option value="4" selected>4건</option></select></div>\n<div class="field"><label for="minCash">진입 당시 당일 누적 거래대금</label><select id="minCash"><option value="0">제한 없음 (연구)</option><option value="500000000" selected>5억 원 이상</option></select></div>\n<div class="field"><label for="maxDrop">직전 당일 고점 대비 최대 눌림</label><select id="maxDrop"><option value="999" selected>제한 없음 (먼저 비교)</option><option value="3">3% 이내</option><option value="2">2% 이내</option></select></div>\n</div>\n<div class="controls"><button id="run">+5% 상승→눌림 검사 시작</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="csv" class="secondary" disabled>CORE.35 통합 CSV 다운로드</button></div>\n<progress id="bar" value="0" max="1"></progress>\n<p id="status" class="muted" aria-live="polite">검사 전 · 이 화면을 열린 상태로 유지해 줘. 중지 후 동일한 설정으로 다시 실행하면 이어서 진행해.</p>\n<div class="note">과거 데이터 조회는 현재 상장 코인 목록 기준이므로 상장폐지 코인 등은 포함되지 않을 수 있어. 00:00 분봉이 없으면 상승률을 추측하지 않고 제외해. 밤 22:15 이후 신규 신호는 60분 청산을 위해 제외하며, 같은 종목의 중복 눌림은 90분 간격으로 제한해.</div>\n</section>\n<section class="panel"><h2>③ 전체 결과 및 하루 최대 4건 선정</h2>\n<div class="kpis">\n<div class="kpi"><span class="muted">완료 종목·날짜 / 전체</span><div id="kDone" class="n">—</div></div>\n<div class="kpi"><span class="muted">+5% 후 눌림 후보</span><div id="kCandidates" class="n">—</div></div>\n<div class="kpi"><span class="muted">5이평 회복 후보</span><div id="kReclaims" class="n">—</div></div>\n<div class="kpi"><span class="muted">API/자료 오류</span><div id="kErrors" class="n">—</div></div>\n<div class="kpi"><span class="muted">선정 거래 (최대 4건/일)</span><div id="kSelected" class="n">—</div></div>\n<div class="kpi"><span class="muted">확정 거래 평균 순익</span><div id="kNet" class="n">—</div></div>\n</div>\n<h3>선정된 거래 — 순수익 +0.3% / +0.5% 선택 비교</h3>\n<div class="tablewrap"><table><thead><tr><th>날짜·코인</th><th>신호 시간</th><th>당일 상승</th><th>선택 방식</th><th>청산</th><th>순수익</th></tr></thead><tbody id="selectedTable"><tr><td colspan="6">검사 전</td></tr></tbody></table></div>\n<h3>일별 요약 — 미확정 거래는 손익 계산에서 제외하되 선정 건수에 포함</h3>\n<div class="tablewrap"><table><thead><tr><th>날짜</th><th>후보</th><th>선정</th><th>확정</th><th>승</th><th>손</th><th>평균 순익</th></tr></thead><tbody id="dailyTable"><tr><td colspan="7">검사 전</td></tr></tbody></table></div>\n</section>\n<section class="panel"><h2>④ 눌림 직후 vs 5이평 재회복 비교</h2>\n<div class="tablewrap"><table><thead><tr><th>진입 방식</th><th>후보</th><th>청산 확정</th><th>수익 거래</th><th>평균 순익</th></tr></thead><tbody id="compareTable"><tr><td colspan="5">검사 전</td></tr></tbody></table></div>\n<div class="note">전체 신호 분석과 하루 최대 4건 선정 성과는 서로 다른 통계야. 전체 신호가 많이 발생해도 실제 매매에서는 발생 시각과 동시 보유 제한을 지켜서 최대 4건만 선택해. 당일 첫 +5%를 나중에 달성한 거래를 과거로 소급하지 않아.</div>\n<details><summary>눌림 후보 전체 펼치기 (화면 100건까지, CSV는 전체)</summary>\n<div class="tablewrap"><table><thead><tr><th>날짜·코인</th><th>눌림 시간</th><th>당일 상승</th><th>5이평 이탈</th><th>고점 대비</th><th>재회복</th><th>직후 순익</th><th>재회복 순익</th></tr></thead><tbody id="allTable"><tr><td colspan="8">검사 전</td></tr></tbody></table></div></details>\n</section>\n<section class="panel"><h2>⑤ 해석할 때 꼭 주의</h2>\n<p>작은 눌림은 상승 중 잠깐 쉬는 구간일 수도 있지만, 추세가 꺾이기 시작하는 신호일 수도 있어. 특히 <b>5이평 아래에서 즉시 매수</b>하는 방법은 추가 하락 위험이 있어 비교 실험으로만 다뤄.</p>\n<p class="muted">백테스트의 고가·저가에 익절과 손절이 동시에 닿으면 손절 우선으로 처리해. 기본 손절은 매수가 대비 -0.7%이며 편도 수수료 0.25%씩을 반영하면 실제 순손실은 약 -1.2%야. 60분 이내 익절·손절이 없다면 시간청산. 슬리피지 0%는 실제 체결 결과를 보장하지 않아.</p>\n<p class="muted">실시간 매수 신호·자동 주문 기능 없음. 이 연구가 다른 날짜에서도 충분히 검증될 때만 별도 실시간 알림 기능을 고려하자.</p>\n</section>\n<script>\n\'use strict\';\nconst $=id=>document.getElementById(id);\nconst CODE_VERSION=\'V2.CORE.35-LIGHT-PLUS5-SMA5-PULLBACK\';\nconst esc=s=>String(s??\'\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nconst fmt=(v,d=3)=>v===null||v===undefined||!Number.isFinite(Number(v))?\'—\':Number(v).toFixed(d)+\'%\';\nconst num=(v)=>Number(v||0).toLocaleString(\'ko-KR\');\nconst todayKST=()=>new Date(Date.now()+9*3600000).toISOString().slice(0,10);\nfunction daysEnding(end,n){let arr=[];for(let i=n-1;i>=0;i--){let x=new Date(end+\'T00:00:00Z\');x.setUTCDate(x.getUTCDate()-i);arr.push(x.toISOString().slice(0,10));}return arr;}\nfunction formConfig(){\n const d=$(\'endDay\').value, n=Number($(\'days\').value), fee=Number($(\'fee\').value), slip=Number($(\'slip\').value),scope=$(\'scope\').value;\n if(!d||d>=todayKST())throw Error(\'완전히 종료된 과거 날짜를 선택해 줘\');\n if(![1,3,5].includes(n)||![0,40,80].includes(scope===\'all\'?0:Number(scope)))throw Error(\'범위 설정 오류\');\n if(![fee,slip].every(Number.isFinite)||Math.min(fee,slip)<0||Math.max(fee,slip)>.5)throw Error(\'편도 수수료·슬리피지는 0~0.5%\');\n return {days:daysEnding(d,n),scope,fee,slip};\n}\nfunction policy(){return {method:$(\'method\').value,target:$(\'target\').value,cap:Number($(\'cap\').value),minCash:Number($(\'minCash\').value),maxDrop:Number($(\'maxDrop\').value)};}\nlet state=null;\nlet running=false;\nlet paused=false;\nconst KEYSET=[\'endDay\',\'days\',\'scope\',\'fee\',\'slip\'];\n$(\'endDay\').value=new Date(Date.now()+9*3600000-86400000).toISOString().slice(0,10);\n$(\'endDay\').max=$(\'endDay\').value;\nfunction api(url,body){return fetch(url,{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(body)}).then(async r=>{let j=await r.json().catch(()=>({error:\'응답을 읽을 수 없음\'}));if(!r.ok||!j.ok)throw Error(j.error||\'API HTTP \'+r.status);return j;});}\nfunction allEvents(){return state?state.results.flatMap(r=>r.events||[]):[];}\nfunction pickRows(){\n if(!state)return {selected:[],byDay:[],nCandidates:0};\n const p=policy(), events=allEvents();\n const qual=events.filter(e=>Number(e.day_trade_krw_at_dip)>=p.minCash && Math.abs(Number(e.day_high_to_dip_pct))<=p.maxDrop);\n const candidates=qual.filter(e=>p.method===\'under\'||e.reclaim_signal_at).map(e=>({e,at:e[p.method===\'under\'?\'dip_signal_at\':\'reclaim_signal_at\'],rkey:p.method+\'_tp\'+p.target,method:p.method}));\n const sorted=candidates.sort((a,b)=>a.at.localeCompare(b.at)||Number(b.e.prior_60m_trade_krw)-Number(a.e.prior_60m_trade_krw)||a.e.market.localeCompare(b.e.market));\n const selected=[],byDay=[];\n for(const day of state.days){\n  const pool=sorted.filter(c=>c.e.date===day), chosen=[];let busyUntil=\'\';\n  for(const item of pool){\n    if(chosen.length>=p.cap)break;\n    if(busyUntil && item.at<busyUntil)continue; // only one open paper trade\n    const key=item.rkey, status=item.e[key+\'_status\'];\n    // Unknown exits still occupy a day\'s slot; never cherry-pick based on outcomes\n    chosen.push(item); selected.push(item);\n    busyUntil=status===\'READY\'?(item.e[key+\'_exit_at\']||item.at):day+\'T23:59:59+09:00\';\n  }\n  const ready=chosen.filter(x=>x.e[x.rkey+\'_status\']===\'READY\');\n  const wins=ready.filter(x=>Number(x.e[x.rkey+\'_net_pct\'])>0);\n  const avg=ready.length?ready.reduce((a,x)=>a+Number(x.e[x.rkey+\'_net_pct\']),0)/ready.length:null;\n  byDay.push({date:day,candidates:pool.length,selected:chosen.length,ready:ready.length,wins:wins.length,loss:ready.length-wins.length,avg});\n }\n return {selected,byDay,nCandidates:candidates.length};\n}\nfunction textRows(id, html, count){$(id).innerHTML=html||`<tr><td colspan="${count}">해당 결과 없음</td></tr>`;}\nfunction render(){\n if(!state)return;\n const ev=allEvents(),p=policy(),pick=pickRows(),ready=pick.selected.filter(x=>x.e[x.rkey+\'_status\']===\'READY\');\n const avg=ready.length?ready.reduce((a,x)=>a+Number(x.e[x.rkey+\'_net_pct\']),0)/ready.length:null;\n $(\'kDone\').textContent=num(state.done)+\' / \'+num(state.tasks.length);\n $(\'kCandidates\').textContent=num(ev.length);\n $(\'kReclaims\').textContent=num(ev.filter(e=>e.reclaim_signal_at).length);\n $(\'kErrors\').textContent=num(state.errors.length + state.results.filter(r=>r.coverage.reason).length);\n $(\'kSelected\').textContent=num(pick.selected.length);\n $(\'kNet\').textContent=fmt(avg);$(\'kNet\').className=\'n \'+(avg!==null?(avg>=0?\'good\':\'bad\'):\'\');\n $(\'bar\').max=Math.max(1,state.tasks.length);$(\'bar\').value=state.done;\n textRows(\'selectedTable\',pick.selected.map(x=>{let e=x.e,k=x.rkey, v=e[k+\'_net_pct\'];return `<tr><td>${esc(e.date)} ${esc(e.market)}</td><td>${esc(x.at.slice(11,16))}</td><td>${fmt(e.day_gain_before_dip_pct)}</td><td>${p.method===\'reclaim\'?\'재회복\':\'이탈 직후\'}</td><td>${esc(e[k+\'_exit\']||e[k+\'_status\'])}</td><td class="${Number(v)>=0&&v!==undefined?\'good\':\'bad\'}">${fmt(v)}</td></tr>`;}).join(\'\'),6);\n textRows(\'dailyTable\',pick.byDay.map(d=>`<tr><td>${esc(d.date)}</td><td>${d.candidates}</td><td>${d.selected}</td><td>${d.ready}</td><td class="good">${d.wins}</td><td class="bad">${d.loss}</td><td>${fmt(d.avg)}</td></tr>`).join(\'\'),7);\n textRows(\'compareTable\',[\'under\',\'reclaim\'].map(method=>{let list=ev.filter(e=>method===\'under\'||e.reclaim_signal_at), key=method+\'_tp\'+p.target,ready=list.filter(e=>e[key+\'_status\']===\'READY\'),win=ready.filter(e=>Number(e[key+\'_net_pct\'])>0).length, mean=ready.length?ready.reduce((a,e)=>a+Number(e[key+\'_net_pct\']),0)/ready.length:null;return `<tr><td>${method===\'under\'?\'A · 첫 이탈 후\':\'B · SMA5 재회복 후\'}</td><td>${list.length}</td><td>${ready.length}</td><td>${win}</td><td>${fmt(mean)}</td></tr>`;}).join(\'\'),5);\n textRows(\'allTable\',ev.slice(0,100).map(e=>`<tr><td>${esc(e.date)} ${esc(e.market)}</td><td>${esc(e.dip_signal_at.slice(11,16))}</td><td>${fmt(e.day_gain_before_dip_pct)}</td><td>${fmt(e.dip_below_sma5_pct)}</td><td>${fmt(e.day_high_to_dip_pct)}</td><td>${e.reclaim_signal_at?esc(e.reclaim_signal_at.slice(11,16)):\'미회복\'}</td><td>${fmt(e[\'under_tp\'+p.target+\'_net_pct\'])}</td><td>${fmt(e[\'reclaim_tp\'+p.target+\'_net_pct\'])}</td></tr>`).join(\'\'),8);\n}\nfunction setStatus(t){$(\'status\').textContent=t;}\nfunction setRunning(on){running=on;$(\'run\').disabled=on;$(\'pause\').disabled=!on;KEYSET.forEach(k=>$(k).disabled=on);}\n$(\'pause\').onclick=()=>{paused=true;setStatus(\'일시 중지 요청 · 현재 실행 중인 코인만 마무리해\');};\n[\'method\',\'target\',\'cap\',\'minCash\',\'maxDrop\'].forEach(id=>$(id).addEventListener(\'change\',render));\n$(\'run\').onclick=async()=>{\n if(running)return;\n try{\n  const cfg=formConfig(),key=JSON.stringify(cfg);\n  if(!state || state.key!==key || state.cursor===state.tasks.length){\n   const r=await fetch(\'/api/v2core/universe\').then(x=>x.json());\n   if(!r.ok||!Array.isArray(r.items))throw Error(r.error||\'KRW 목록 조회 실패\');\n   const markets=cfg.scope===\'all\'?r.items:r.items.slice(0,Number(cfg.scope));\n   const tasks=cfg.days.flatMap(day=>markets.map(market=>({date:day,market})));\n   state={key,...cfg,tasks,cursor:0,done:0,results:[],errors:[]};\n  }\n  paused=false;setRunning(true);render();$(\'csv\').disabled=false;\n  let lastPaint=0;\n  async function worker(){while(!paused&&state.cursor<state.tasks.length){\n   const task=state.tasks[state.cursor++];\n   try{const out=await api(\'/api/v2core/pullback35/analyze\',{...task,fee:state.fee,slip:state.slip});state.results.push(out.result);}\n   catch(e){state.errors.push({...task,error:String(e.message||e)});}\n   finally{state.done++;if(state.done-lastPaint>=8||state.done===state.tasks.length){lastPaint=state.done;render();setStatus(`${state.done}/${state.tasks.length} 코인·날짜 검사 중 · 오류 ${state.errors.length} · 화면을 유지해 줘`);}}\n  }}\n  await Promise.all(Array.from({length:Math.min(4,state.tasks.length)},()=>worker()));\n  render();setStatus(paused?\'일시 중지됨 · 같은 설정으로 다시 누르면 이어서 검사\':`완료 ${state.done}/${state.tasks.length} · +5% 후 눌림 ${allEvents().length}건 · CSV 다운로드 가능`);\n }catch(e){setStatus(\'⚠️ \'+String(e.message||e));}\n finally{setRunning(false);$(\'csv\').disabled=!state||(!state.results.length&&!state.errors.length);}\n};\nfunction csvVal(v){let s=String(v??\'\');if(/^[=+@-]/.test(s)&&isNaN(Number(s)))s="\'"+s;return \'"\'+s.replace(/"/g,\'""\')+\'"\';}\n$(\'csv\').onclick=()=>{\n if(!state)return;\n const pick=pickRows(),chosen=new Map(pick.selected.map(x=>[x.e.date+\'|\'+x.e.market+\'|\'+x.e.dip_signal_at,x]));\n const base={version:CODE_VERSION,date_from:state.days[0],date_to:state.days[state.days.length-1],scope:state.scope,fee_pct_side:state.fee,slippage_pct_side:state.slip,scan_complete:state.done===state.tasks.length,scan_tasks:state.tasks.length,scan_done:state.done,scan_errors:state.errors.length,selection_method:policy().method,daily_max:policy().cap,target_net_pct:policy().target===\'05\'?.5:.3,stop_gross_pct:.7,horizon_min:60,midnight_base:\'KST_00_00_FIRST_5M_OPEN\',sma:\'SMA5_ON_5M\',simulation:\'NEXT_OPEN_EXIT_OHLC_STOP_FIRST\'};\n const rec=[];for(const d of pick.byDay)rec.push({...base,row_type:\'DAY_SUMMARY\',date:d.date,...d});\n for(const r of state.results){rec.push({...base,row_type:\'MARKET_COVERAGE\',date:r.date,market:r.market,...r.coverage});\n  for(const e of r.events){const selected=chosen.get(e.date+\'|\'+e.market+\'|\'+e.dip_signal_at);rec.push({...base,row_type:\'PULLBACK_EVENT\',date:e.date,market:e.market,selected_for_daily_limit:!!selected,selected_entry_method:selected?.method||\'\',selected_signal_at:selected?.at||\'\',...e});}}\n for(const er of state.errors)rec.push({...base,row_type:\'API_ERROR\',...er});\n const cols=[...new Set([\'row_type\',\'version\',\'date\',\'market\',\'dip_signal_at\',\'reclaim_signal_at\',\'selected_for_daily_limit\',\'selected_entry_method\',\'day_gain_before_dip_pct\',\'day_trade_krw_at_dip\',\'under_tp05_net_pct\',\'reclaim_tp05_net_pct\',...rec.flatMap(x=>Object.keys(x))])];\n const data=\'\\ufeff\'+[cols.join(\',\'),...rec.map(r=>cols.map(c=>csvVal(r[c])).join(\',\'))].join(\'\\r\\n\');\n const url=URL.createObjectURL(new Blob([data],{type:\'text/csv;charset=utf-8\'}));const a=document.createElement(\'a\');a.href=url;a.download=`CORE35_PLUS5_SMA5_PULLBACK_${state.days[0]}_${state.days.at(-1)}_${state.scope}_KRW.csv`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000);\n};\n</script></main></body></html>\n'


def register_daytrade_review(app,deps):
    """Only research routes; existing / and live app.py behavior stay unchanged."""
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache={'at':0.,'sorted':[],'markets':[],'results':{}}
    lock=Lock()

    @app.get('/v2-core-review')
    @app.get('/backtest-daytrade')
    def core35_page():
        return Response(CORE_PAGE,mimetype='text/html; charset=utf-8')

    @app.get('/api/v2core/universe')
    def core35_universe():
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

    @app.post('/api/v2core/pullback35/analyze')
    def core35_analyze():
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
