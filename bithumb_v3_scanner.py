"""BITHUMB V3.16 | 시장 판단 + 분리된 진단용 후보 미리보기.

BTC 월/주/일/4시간/1시간과 30/15/5분의 마감된 캔들만 분석한다.
큰 흐름/시장 상태 판정은 V3.11과 동일, 초단기 흐름 및 정제 거래대금은 관찰만 한다.
실제 관찰 후보는 시장 탐색이 허용될 때만 선정한다. 관망 중 진단 버튼은 매수 신호가 아닌 비교용 별도 목록만 제공한다.
BUY 신호, 자동 주문, 수익률 백테스트 없음. 가격 조건 및 후보 선별은 연구 가설이며 수익성 검증 전.

pip install requests
python bithumb_v3_scanner.py
Render: python bithumb_v3_scanner.py  (PORT 환경 변수 자동 사용)
"""
from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import requests

VERSION = 'V3.16'
API = 'https://api.bithumb.com/v1'
AGE_DAYS = 14
AGE_WORKERS = 12
CANDIDATE_WORKERS = 6
CANDIDATE_POOL = 20
MAX_CANDIDATES = 2
MIN_TURNOVER_KRW = 500_000_000
MAX_RISE_24H_PCT = 10.0
# Price-pegged assets: explicit, inspectable list; never use ticker-name heuristics.
STABLE_CODES = frozenset({'USDT','USDC','DAI','FDUSD','TUSD','PYUSD','USDE','USDS','USDD','RLUSD','USD1','EURC','FRAX','GHO','LUSD','SUSD'})
AGE_LOCK = threading.Lock()
AGE_CACHE = {}
KST = timezone(timedelta(hours=9))
CACHE_SECONDS = 60
LOCK = threading.Lock()
PREVIEW_LOCK = threading.Lock()
CACHE = {'time': 0.0, 'result': None}
TIMEFRAMES = (
    ('month', '월봉', '/candles/months'),
    ('week', '주봉', '/candles/weeks'),
    ('day', '일봉', '/candles/days'),
    ('4h', '4시간봉', '/candles/minutes/240'),
    ('1h', '1시간봉', '/candles/minutes/60'),
    ('30m', '30분봉', '/candles/minutes/30'),
    ('15m', '15분봉', '/candles/minutes/15'),
    ('5m', '5분봉', '/candles/minutes/5'),
)


def now_kst():
    return datetime.now(KST)


def finite_number(value, default=None):
    try:
        x = float(value)
        return x if x == x and abs(x) != float('inf') else default
    except (ValueError, TypeError):
        return default


def get_api(path, params=None, max_attempts=3, timeout=(4, 12)):
    """Read-only API; retry a few transient failures, never touch private routes."""
    last = None
    for attempt in range(max_attempts):
        try:
            response = requests.get(API + path, params=params, timeout=timeout,
                                    headers={'Accept': 'application/json', 'User-Agent': 'v316-market-gate/1.0'})
            if response.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f'HTTP {response.status_code} 요청 제한/일시 오류')
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                raise ValueError('API 배열 응답이 아닙니다')
            return data
        except (requests.RequestException, ValueError, RuntimeError) as e:
            last = str(e)
            if attempt + 1 < max_attempts:
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f'{path} 조회 실패: {last}')


def candle_start_kst(obj):
    raw = obj.get('candle_date_time_kst')
    if not isinstance(raw, str) or not raw:
        raise ValueError('캔들 시작 시각 없음')
    dt = datetime.fromisoformat(raw)
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)


def end_of_candle(start, tf):
    if tf == 'month':
        year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
        day = min(start.day, monthrange(year, month)[1])
        return start.replace(year=year, month=month, day=day)
    if tf == 'week':
        return start + timedelta(days=7)
    if tf == 'day':
        return start + timedelta(days=1)
    if tf == '4h':
        return start + timedelta(hours=4)
    if tf == '1h':
        return start + timedelta(hours=1)
    if tf in ('30m', '15m', '5m'):
        return start + timedelta(minutes=int(tf[:-1]))
    raise ValueError(f'지원하지 않는 시간봉 {tf}')


def completed_candles(raw, tf, now=None):
    """Drop the currently forming candle; reject duplicated/invalid items."""
    now = now or now_kst()
    dedup = {}
    for item in raw:
        start = candle_start_kst(item)
        close = finite_number(item.get('trade_price'))
        if close is None or close <= 0:
            continue
        if end_of_candle(start, tf) <= now - timedelta(seconds=2):
            dedup[start] = {'start': start, 'close': close}
    return sorted(dedup.values(), key=lambda x: x['start'])


def contiguous_last(bars, tf, n=10):
    if len(bars) < n:
        return False
    last = bars[-n:]
    return all(end_of_candle(last[i-1]['start'], tf) == last[i]['start'] for i in range(1, n))


def classify_trend(bars, tf):
    """Predeclared simple rule: close vs MA5, MA5 vs MA10 (completed bars only)."""
    if not contiguous_last(bars, tf, 10):
        return {'state': 'unknown', 'reason': '마감봉 10개 연속 이력 부족', 'bars': len(bars)}
    closes = [r['close'] for r in bars[-10:]]
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes) / 10
    last = closes[-1]
    if last > ma5 and ma5 > ma10:
        state = 'up'
    elif last < ma5 and ma5 < ma10:
        state = 'down'
    else:
        state = 'side'
    return {
        'state': state, 'close': last, 'ma5': ma5, 'ma10': ma10,
        'last_closed_start': bars[-1]['start'].isoformat(),
        'last_closed_end': end_of_candle(bars[-1]['start'], tf).isoformat(),
        'bars': len(bars),
        'return_4h_pct': round((closes[-1] / closes[-5] - 1) * 100, 4) if tf == '1h' else None,
    }


def combine(a, b):
    """Conflicting signals never become UP/DOWN by averaging."""
    if 'unknown' in (a, b):
        return 'unknown'
    if a == b:
        return a
    if a == 'side':
        return b
    if b == 'side':
        return a
    return 'mixed'


def stablecoin(market):
    return market.rsplit('-', 1)[-1] in STABLE_CODES


def age_history_cutoff(now=None):
    # A day candle beginning before this KST date proves a KRW trading history
    # around 14 calendar days earlier. This is NOT an official listing timestamp.
    now = now or now_kst()
    day = (now - timedelta(days=AGE_DAYS)).date()
    return datetime(day.year, day.month, day.day, tzinfo=KST)


def check_prior_krw_history(market, cutoff, reader=None):
    """Market must have a KRW daily candle before the cutoff to join turnover."""
    reader = reader or get_api
    try:
        # Bithumb 'to' is KST; returns the most recent candle STRICTLY before it.
        data = reader('/candles/days',
                      {'market': market, 'to': cutoff.strftime('%Y-%m-%d %H:%M:%S'), 'count': 1},
                      max_attempts=2, timeout=(3, 7))
        if not data:
            return {'status': 'recent_history', 'evidence': '',
                    'reason': f'{AGE_DAYS}일 이전 KRW 일봉 기록 없음(신규 또는 과거 이력 부족)'}
        ts = candle_start_kst(data[0])
        if ts < cutoff:
            return {'status': 'established', 'evidence': ts.isoformat(), 'reason': ''}
        return {'status': 'recent_history', 'evidence': ts.isoformat(),
                'reason': f'{AGE_DAYS}일 이전 KRW 일봉 기록 없음'}
    except Exception as exc:
        return {'status': 'unverified', 'evidence': '', 'reason': 'KRW 이력 확인 실패: ' + str(exc)[:110]}


def check_market_ages(markets, now=None, reader=None):
    """Cache age status and fetch missing checks concurrently; one public day candle per market.

    It is intentionally a *history* gate, not a claim of exact listing date.
    A positive prior candle is cached for 24h; short history for 4h; failures 5m.
    """
    now = now or now_kst()
    cutoff = age_history_cutoff(now)
    targets = sorted(set(m for m in markets if m.startswith('KRW-') and m != 'KRW-BTC' and not stablecoin(m)))
    result = {}
    missing = []
    current = time.time()
    with AGE_LOCK:
        for m in targets:
            cached = AGE_CACHE.get(m)
            if cached and cached[0] == cutoff.date().isoformat() and current < cached[2]:
                result[m] = dict(cached[1])
            else:
                missing.append(m)
    if missing:
        def worker(m):
            return m, check_prior_krw_history(m, cutoff, reader)
        with ThreadPoolExecutor(max_workers=AGE_WORKERS) as pool:
            pending = {pool.submit(worker, m): m for m in missing}
            for future in as_completed(pending):
                m = pending[future]
                try:
                    _, item = future.result()
                except Exception as exc:
                    item = {'status':'unverified','evidence':'','reason':f'이력 검사 실패: {str(exc)[:100]}'}
                result[m] = item
                ttl = 86400 if item['status'] == 'established' else (14400 if item['status'] == 'recent_history' else 300)
                with AGE_LOCK:
                    AGE_CACHE[m] = (cutoff.date().isoformat(), dict(item), time.time() + ttl)
    return result, cutoff


def breadth_from_tickers(markets, tickers, age_status=None, cutoff=None):
    """Market breadth is V3.14-compatible. Only turnover statistics use history-filtered alts.

    A ticker with no verified prior history is omitted from adjusted turnover;
    API verification failure remains separate from a probable recent listing.
    """
    age_status = age_status or {}
    eligible = {m for m in markets if m.startswith('KRW-') and m != 'KRW-BTC'}
    mapped = {}
    for obj in tickers:
        name = obj.get('market')
        if name not in eligible or name in mapped:
            continue
        rate = finite_number(obj.get('signed_change_rate'))
        turnover = finite_number(obj.get('acc_trade_price_24h'))
        if rate is None or turnover is None or turnover < 0:
            continue
        mapped[name] = {
            'market': name, 'change_rate_pct': round(rate * 100, 6),
            'turnover_24h_krw': round(turnover), 'rising': rate > 0,
        }
    rows = sorted(mapped.values(), key=lambda x: x['market'])
    total = len(eligible)
    valid = len(rows)
    up = sum(x['rising'] for x in rows)
    down = sum(x['change_rate_pct'] < 0 for x in rows)
    flat = valid - up - down
    ratio = (up * 100 / valid) if valid else None
    coverage = (valid * 100 / total) if total else 0
    if valid < 20 or coverage < 80:
        state = 'unknown'
    elif ratio >= 55:
        state = 'strong'
    elif ratio >= 40:
        state = 'mixed'
    else:
        state = 'weak'

    raw_turnover = sum(x['turnover_24h_krw'] for x in rows)
    included = []
    excluded = []
    totals = {'stablecoin': 0, 'recent_history': 0, 'unverified': 0}
    for row in rows:
        market = row['market']
        if stablecoin(market):
            category = 'stablecoin'
            detail = {'status':'stablecoin','evidence':'', 'reason':'가격연동 스테이블코인'}
        else:
            detail = age_status.get(market, {'status':'unverified','evidence':'','reason':'거래 이력 확인 결과 없음'})
            category = '' if detail['status'] == 'established' else detail['status']
            if category not in ('', 'recent_history', 'unverified'):
                category = 'unverified'
        row['age_status'] = detail['status']
        row['age_evidence'] = detail.get('evidence', '')
        row['turnover_included'] = not bool(category)
        row['exclusion_reason'] = detail.get('reason', '') if category else ''
        if category:
            totals[category] += row['turnover_24h_krw']
            excluded.append(row)
        else:
            included.append(row)
    adjusted = sum(x['turnover_24h_krw'] for x in included)
    top = sorted(included, key=lambda x: (-x['turnover_24h_krw'], x['market']))
    top1 = top[0] if top else None
    top1_value = top1['turnover_24h_krw'] if top1 else 0
    top10_value = sum(x['turnover_24h_krw'] for x in top[:10])
    nonstable = max(0, raw_turnover - totals['stablecoin'])
    age_verified_pct = (100 * (nonstable - totals['unverified']) / nonstable) if nonstable else 0
    # Do not confidently label concentration when material turnover is unverified.
    enough = (valid >= 20 and coverage >= 80 and len(included) >= 20
              and adjusted > 0 and age_verified_pct >= 95)
    top1_pct = round(top1_value * 100 / adjusted, 2) if enough else None
    top10_pct = round(top10_value * 100 / adjusted, 2) if enough else None
    concentration = 'unknown' if not enough else (
        'high' if top1_pct >= 30 or top10_pct >= 75 else 'normal'
    )
    turnover_detail = {
        'state': concentration, 'top_market': top1['market'] if top1 else None,
        'top1_krw': top1_value if enough else None,
        'top10_krw': top10_value if enough else None,
        'top1_pct': top1_pct, 'top10_pct': top10_pct,
        'excluding_top1_krw': (adjusted - top1_value) if enough else None,
        'thresholds': {'top1_pct': 30, 'top10_pct': 75},
        'age_checked_share_pct': round(age_verified_pct,2),
        'eligible_count': len(included),
    }
    return {
        'total_markets': total, 'valid_markets': valid, 'coverage_pct': round(coverage, 2),
        'rising': up, 'falling': down, 'flat': flat, 'rising_pct': round(ratio, 2) if ratio is not None else None,
        'turnover_24h_krw': adjusted, 'raw_turnover_24h_krw': raw_turnover,
        'excluded_turnover_24h_krw': sum(totals.values()),
        'excluded_turnover_by_reason': totals,
        'excluded_counts': {key:sum(row['age_status']==key for row in excluded) for key in totals},
        'excluded_markets': excluded,
        'turnover_cutoff_kst': cutoff.isoformat() if cutoff else '',
        'concentration': turnover_detail,
        'state': state, 'rows': rows,
    }


def decide(tf, breadth):
    m, w, d, h4, h1 = (tf[k]['state'] for k in ('month', 'week', 'day', '4h', '1h'))
    long = combine(m, w)
    short = combine(h4, h1)
    market = breadth.get('state', 'unknown')
    if 'unknown' in (long, short) or market == 'unknown':
        return {'key': 'unknown', 'label': '자료 확인 필요', 'action': '진입 판단 보류',
                'detail': 'BTC 마감봉 또는 원화마켓 데이터가 부족해 판단하지 않았어.',
                'long': long, 'short': short, 'can_search': False}
    if long == 'up' and short == 'down':
        return {'key': 'swing', 'label': '스윙 대기', 'action': '눌림 후 반등 확인',
                'detail': '장기는 상승이지만 4시간·1시간 흐름이 약해. 지금 바로 매수하지 않고 전환을 기다려.',
                'long': long, 'short': short, 'can_search': False}
    if long == 'down' and short == 'up':
        if market == 'strong':
            return {'key': 'scalp', 'label': '단타 후보', 'action': '짧은 반등만 탐색',
                    'detail': '장기 하락 속 단기 반등이야. 알트 상승 참여율이 높지만 추격은 피해야 해.',
                    'long': long, 'short': short, 'can_search': True}
        return {'key': 'wait', 'label': '관망', 'action': '단타 후보 보류',
                'detail': 'BTC는 반등하지만 빗썸 알트 상승 참여가 충분하지 않아.',
                'long': long, 'short': short, 'can_search': False}
    if long == 'up' and short == 'up':
        if market == 'strong':
            return {'key': 'trend', 'label': '추세 탐색', 'action': '단타·스윙 후보 탐색',
                    'detail': '장기·단기 방향과 알트 상승 참여가 같이 강해. 그래도 진입 위치는 별도 검토해야 해.',
                    'long': long, 'short': short, 'can_search': True}
        return {'key': 'wait', 'label': '관망', 'action': '종목 탐색 보류',
                'detail': 'BTC 방향은 좋지만 알트 상승 참여율이 기준에 미달해.',
                'long': long, 'short': short, 'can_search': False}
    return {'key': 'wait', 'label': '관망', 'action': '시장 확인',
            'detail': '추세가 엇갈리거나 단기 하락이 이어지고 있어. 매수 신호로 쓰지 않아.',
            'long': long, 'short': short, 'can_search': False}


def assess_micro(tf):
    """30/15/5m status only. Must NEVER change the V3.11 market gate.

    1h is used only to label a lower-timeframe rebound accurately.
    Unknown/missing candles are not interpreted as a new trend.
    """
    keys = ('30m', '15m', '5m')
    vals = [tf[k]['state'] for k in keys]
    h1 = tf['1h']['state']
    if 'unknown' in vals:
        return {'key': 'unknown', 'label': '분봉 자료 확인 필요',
                'detail': '30·15·5분 마감봉이 부족하거나 조회에 실패했어. 타이밍 판단 보류.'}
    if vals == ['up', 'up', 'up']:
        if h1 == 'down':
            return {'key': 'rebound', 'label': '1시간 하락 속 반등 관찰',
                    'detail': '30·15·5분은 상승이지만 1시간 추세는 하락. 추세 전환 확정이 아니야.'}
        return {'key': 'up', 'label': '단기 상승 정렬',
                'detail': '30·15·5분 마감봉이 모두 상승. 시장 진입 허용과는 별개야.'}
    if vals[1:] == ['up', 'up']:
        return {'key': 'rebound', 'label': '초기 반등 관찰',
                'detail': '15·5분은 상승하지만 30분 추세는 아직 정렬되지 않았어.'}
    if vals == ['down', 'down', 'down']:
        return {'key': 'down', 'label': '단기 하락 지속',
                'detail': '30·15·5분이 모두 하락. 반등 신호로 해석하지 않아.'}
    if vals[0] == 'up' and vals[1:] == ['down', 'down']:
        return {'key': 'pullback', 'label': '단기 조정 관찰',
                'detail': '30분 상승 흐름 안에서 15·5분 하락이 나타나고 있어.'}
    return {'key': 'mixed', 'label': '분봉 방향 혼조',
            'detail': '30·15·5분 방향이 서로 달라. 진입 신호로 쓰지 않아.'}




def analyze_candidate_market(market, ticker, btc_4h_change, reader=None, now=None):
    """Read-only candidate review. Uses completed candles only; never places orders."""
    reader = reader or get_api
    now = now or now_kst()
    tf = {}
    issues = []
    for key, path in (('day', '/candles/days'), ('4h', '/candles/minutes/240'), ('1h', '/candles/minutes/60')):
        try:
            raw = reader(path, {'market': market, 'count': 20})
            tf[key] = classify_trend(completed_candles(raw, key, now), key)
            # A continuous but old candle series is not a current-entry candidate.
            latest_end = tf[key].get('last_closed_end')
            if latest_end and now - datetime.fromisoformat(latest_end) > {'day': timedelta(hours=48), '4h': timedelta(hours=8), '1h': timedelta(hours=2)}[key]:
                issues.append(f'{key}: 최신 마감봉이 오래됨')
        except Exception as exc:
            tf[key] = {'state': 'unknown', 'reason': '캔들 API 조회 실패'}
            issues.append(f'{key}: {str(exc)[:80]}')
    states = {k: tf[k]['state'] for k in ('day', '4h', '1h')}
    strength = tf['1h'].get('return_4h_pct')
    relative = round(strength - btc_4h_change, 4) if strength is not None and btc_4h_change is not None else None
    h4 = tf['4h']
    deviation = (round((h4['close'] / h4['ma5'] - 1) * 100, 4)
                 if h4.get('ma5') and h4.get('close') else None)
    common = {
        'market': market, 'turnover_24h_krw': ticker['turnover_24h_krw'],
        'change_rate_pct': ticker['change_rate_pct'],
        'daily_state': states['day'], 'h4_state': states['4h'], 'h1_state': states['1h'],
        'return_4h_pct': strength, 'btc_return_4h_pct': btc_4h_change,
        'relative_4h_pct': relative, 'h4_distance_ma5_pct': deviation,
        'last_price': tf['1h'].get('close'),
        'last_closed_end': tf['1h'].get('last_closed_end'),
        'issues': issues,
    }
    if issues or any(state == 'unknown' for state in states.values()) or relative is None or deviation is None:
        return {**common, 'scalp_ok': False, 'swing_ok': False,
                'state': 'DATA_INCOMPLETE', 'reason': '마감봉 연속 이력 또는 API 자료 부족', 'selected_mode': ''}
    if not (0 <= ticker['change_rate_pct'] <= MAX_RISE_24H_PCT):
        return {**common, 'scalp_ok': False, 'swing_ok': False,
                'state': 'REJECT', 'reason': '24시간 상승률 0~10% 범위 밖(급등 추격 방지)', 'selected_mode': ''}
    scalp_ok = states['1h'] == 'up' and states['4h'] in ('up', 'side') and relative > 0
    swing_ok = (states['day'] == 'up' and states['4h'] in ('up', 'side')
                and states['1h'] in ('up', 'side') and -2 <= deviation <= 2)
    reason = ('단타·스윙 모두 관찰 가능' if scalp_ok and swing_ok else
              '단타 관찰 기준 충족' if scalp_ok else
              '스윙 관찰 기준 충족' if swing_ok else '기준 미충족 · 매수 신호 없음')
    return {**common, 'scalp_ok': scalp_ok, 'swing_ok': swing_ok,
            'state': 'ELIGIBLE' if scalp_ok or swing_ok else 'REJECT',
            'reason': reason, 'selected_mode': ''}


def select_candidates(decision, breadth, btc, reader=None, now=None):
    """Gate first. No candidate candle requests when market is WAIT/SWING_WAIT/UNKNOWN.

    Each mode has separate predeclared review rules; at most two unique symbols total.
    'swing' gate is a WAIT state, so swing candidates do not appear until gate permits.
    """
    output = {'state': 'BLOCKED', 'label': '시장 관망 · 후보 탐색 안 함',
              'gate': decision.get('key'), 'pool_size': 0, 'checked': 0,
              'candidates': [], 'reviews': [], 'warnings': [],
              'rules': {
                  'universe': f'14일 이전 거래 확인된 일반 알트 중 거래대금 >=5억원, 24시간 상승률 0~10% 상위 {CANDIDATE_POOL}개',
                  'exclude': '신규·이력 미확인·스테이블·BTC 제외. 24시간 +10% 초과 추격 제외',
                  'scalp': '1시간 상승·4시간 비하락·BTC 대비 최근 4시간 상대강도 양수',
                  'swing': '일봉 상승·4시간 비하락·1시간 비하락·4시간 종가가 SMA5 ±2% 이내',
                  'limit': '단타 시장은 단타 최대 2개, 추세 시장은 단타 1개+스윙 1개 우선, 총 2개',
                  'limitations': '관찰 후보만 표시. 미래 수익·체결 검증 없음. 실전 BUY/자동주문 없음',
              }}
    if not decision.get('can_search') or decision.get('key') not in ('scalp', 'trend'):
        return output
    btc_4h = btc.get('1h', {}).get('return_4h_pct')
    if btc_4h is None:
        output.update(state='DATA_INCOMPLETE', label='BTC 4시간 상대강도 자료 부족')
        return output
    pool = sorted((r for r in breadth.get('rows', []) if r.get('turnover_included')
                   and r['turnover_24h_krw'] >= MIN_TURNOVER_KRW
                   and 0 <= r['change_rate_pct'] <= MAX_RISE_24H_PCT),
                  key=lambda r: (-r['turnover_24h_krw'], r['market']))[:CANDIDATE_POOL]
    output['pool_size'] = len(pool)
    if not pool:
        output.update(state='NO_CANDIDATE', label='조건에 맞는 거래대금 종목 없음')
        return output
    now = now or now_kst()
    with ThreadPoolExecutor(max_workers=CANDIDATE_WORKERS) as executor:
        tasks = {executor.submit(analyze_candidate_market, row['market'], row, btc_4h, reader, now): row for row in pool}
        for future in as_completed(tasks):
            row = tasks[future]
            try:
                data = future.result()
            except Exception as exc:
                data = {'market': row['market'], 'turnover_24h_krw': row['turnover_24h_krw'],
                        'change_rate_pct': row['change_rate_pct'], 'state': 'DATA_ERROR',
                        'reason': '종목 검사 실패', 'issues': [str(exc)[:120]],
                        'scalp_ok': False, 'swing_ok': False, 'selected_mode': ''}
            output['reviews'].append(data)
            if data.get('issues'):
                output['warnings'].append(row['market'] + ': ' + '; '.join(data['issues'])[:100])
    output['reviews'].sort(key=lambda r: (-r['turnover_24h_krw'], r['market']))
    output['checked'] = len(output['reviews'])
    scalps = sorted((r for r in output['reviews'] if r.get('scalp_ok')),
                    key=lambda r: (-r['relative_4h_pct'], -r['turnover_24h_krw'], r['market']))
    swings = sorted((r for r in output['reviews'] if r.get('swing_ok')),
                    key=lambda r: (abs(r['h4_distance_ma5_pct']), -r['turnover_24h_krw'], r['market']))
    chosen = []
    used = set()
    def add(rows, mode):
        for r in rows:
            if r['market'] not in used:
                used.add(r['market'])
                r['selected_mode'] = mode
                chosen.append(r)
                return
    if decision['key'] == 'scalp':
        for r in scalps[:MAX_CANDIDATES]:
            r['selected_mode'] = '단타 관찰'
            chosen.append(r)
    else:
        add(scalps, '단타 관찰')
        add(swings, '스윙 관찰')
        while len(chosen) < MAX_CANDIDATES:
            before = len(chosen)
            add(scalps, '단타 관찰')
            if len(chosen) < MAX_CANDIDATES:
                add(swings, '스윙 관찰')
            if len(chosen) == before:
                break
    output.update(state='READY' if chosen else 'NO_CANDIDATE',
                  label=f'관찰 후보 {len(chosen)}개' if chosen else '기준을 통과한 관찰 후보 없음',
                  candidates=chosen)
    return output



def diagnostic_preview(snapshot, reader=None, now=None):
    """Explicit, separate, read-only lookahead-free diagnostic on the same market snapshot.

    Never touches decision.can_search or the official V3.15 candidate list. Two
    comparison examples (scalp/swing), NOT recommendations. A nonqualifying
    example is labelled as such rather than being silently promoted to a BUY.
    """
    now = now or now_kst()
    reader = reader or get_api
    br = snapshot['breadth']
    tf = snapshot['btc']
    diag = {
        'state': 'NO_DATA', 'label': '진단 대상 없음', 'snapshot_kst': snapshot['checked_kst'],
        'checked_kst': now.isoformat(timespec='seconds'),
        'market_gate': snapshot['decision']['key'],
        'market_gate_label': snapshot['decision']['label'],
        'official_gate_unchanged': True, 'pool_size': 0, 'checked': 0,
        'candidates': [], 'reviews': [], 'warnings': [],
        'rule': '시장 상태와 무관하게 정제 거래대금 상위 20종목(일간 -5~+10%)만 점검. 단타·스윙 비교용 각 최대 1종목. BUY 신호/추천 아님.'
    }
    btc_4h = tf.get('1h', {}).get('return_4h_pct')
    if btc_4h is None:
        diag.update(state='DATA_INCOMPLETE', label='BTC 최근 4시간 기준값 부족')
        return diag
    pool = sorted((row for row in br.get('rows', [])
                   if row.get('turnover_included')
                   and row['turnover_24h_krw'] >= MIN_TURNOVER_KRW
                   and -5 <= row['change_rate_pct'] <= MAX_RISE_24H_PCT),
                  key=lambda row: (-row['turnover_24h_krw'], row['market']))[:CANDIDATE_POOL]
    diag['pool_size'] = len(pool)
    if not pool:
        return diag
    with ThreadPoolExecutor(max_workers=CANDIDATE_WORKERS) as executor:
        tasks = {executor.submit(analyze_candidate_market, row['market'], row, btc_4h, reader, now): row for row in pool}
        for future in as_completed(tasks):
            row = tasks[future]
            try:
                item = future.result()
            except Exception as exc:
                item = {'market': row['market'], 'turnover_24h_krw':row['turnover_24h_krw'],
                        'change_rate_pct': row['change_rate_pct'], 'state':'DATA_ERROR',
                        'reason':'종목 데이터 조회 오류', 'issues':[str(exc)[:110]],
                        'scalp_ok': False, 'swing_ok': False}
            diag['reviews'].append(item)
            if item.get('issues'):
                diag['warnings'].append(row['market'] + ': ' + '; '.join(item['issues'])[:100])
    diag['reviews'].sort(key=lambda row: (-row['turnover_24h_krw'], row['market']))
    diag['checked'] = len(diag['reviews'])
    usable = [r for r in diag['reviews'] if not r.get('issues')
              and r.get('relative_4h_pct') is not None
              and r.get('h4_distance_ma5_pct') is not None
              and r.get('last_closed_end')]
    if not usable:
        diag.update(state='DATA_INCOMPLETE', label='연속된 종목 캔들 부족')
        return diag
    # Fixed, simple comparison ordering: 1h strength for scalp and daily/4h
    # plus proximity to MA5 for swing. Display strict condition compliance.
    scalp_rank = sorted(usable, key=lambda r:(
        -int(bool(r.get('scalp_ok'))),
        -int(r.get('h1_state')=='up'),
        -int(r.get('h4_state') in ('up','side')),
        -r['relative_4h_pct'], -r['turnover_24h_krw'], r['market']))
    swing_rank = sorted(usable, key=lambda r:(
        -int(bool(r.get('swing_ok'))),
        -int(r.get('daily_state')=='up'),
        -int(r.get('h4_state') in ('up','side')),
        abs(r['h4_distance_ma5_pct']), -r['turnover_24h_krw'], r['market']))
    picked = set()
    for mode, ranked, flag in [('단타 진단',scalp_rank,'scalp_ok'), ('스윙 진단',swing_rank,'swing_ok')]:
        for item in ranked:
            if item['market'] in picked:
                continue
            picked.add(item['market'])
            copy = dict(item)
            copy['diagnostic_mode'] = mode
            copy['diagnostic_qualified'] = bool(item.get(flag))
            copy['diagnostic_note'] = (mode + ' 관찰 조건 충족 · 그래도 현재 시장 매수 허용과 무관' if copy['diagnostic_qualified']
                                       else mode + ' 관찰 조건 미충족 · 비교 참고만 가능')
            diag['candidates'].append(copy)
            break
    if diag['candidates']:
        diag.update(state='READY', label='진단 비교 ' + str(len(diag['candidates'])) + '개 · 매수 추천 아님')
    return diag


def preview_cached():
    """One explicit preview call per market snapshot. Never overwrite a refreshed snapshot."""
    with PREVIEW_LOCK:
        with LOCK:
            snapshot = CACHE['result']
            if snapshot is None:
                raise ValueError('먼저 시장 분석하기를 실행해줘.')
            existing = snapshot.get('diagnostic')
            if existing is not None:
                return existing
        preview = diagnostic_preview(snapshot)
        with LOCK:
            if CACHE['result'] is not snapshot:
                raise RuntimeError('시장 분석 결과가 갱신됐어. 진단 버튼을 다시 눌러줘.')
            snapshot['diagnostic'] = preview
        return preview

def compute_status():
    warnings = []
    tf = {}
    for key, label, path in TIMEFRAMES:
        try:
            data = get_api(path, {'market': 'KRW-BTC', 'count': 20})
            tf[key] = {'label': label, **classify_trend(completed_candles(data, key), key)}
        except Exception as exc:
            tf[key] = {'label': label, 'state': 'unknown', 'reason': 'BTC 캔들 조회 실패'}
            warnings.append(f'{label}: {str(exc)[:140]}')
    try:
        meta = get_api('/market/all', {'isDetails': 'false'})
        markets = sorted(set(str(x.get('market', '')) for x in meta if str(x.get('market', '')).startswith('KRW-')))
        tickers = []
        for i in range(0, len(markets), 100):
            try:
                tickers.extend(get_api('/ticker', {'markets': ','.join(markets[i:i+100])}))
            except Exception as exc:
                warnings.append(f'현재가 {i//100 + 1}번째 묶음 조회 실패: {str(exc)[:100]}')
        # One 14-day KRW candle-history check per active nonstable ticker.
        # Results are cached, so repeat scans do not refetch all symbols.
        checked_names = [str(t.get('market','')) for t in tickers if t.get('market') in markets]
        age_info, cutoff = check_market_ages(checked_names)
        breadth = breadth_from_tickers(markets, tickers, age_info, cutoff)
        excluded = breadth['excluded_counts']
        if excluded['unverified']:
            warnings.append(f"거래 이력 확인 실패 {excluded['unverified']}개 · 정제 거래대금은 부분 집계")

    except Exception as exc:
        breadth = breadth_from_tickers([], [])
        warnings.append(f'원화마켓 조회 실패: {str(exc)[:140]}')
    decision = decide(tf, breadth)
    timing = assess_micro(tf)
    candidate_data = select_candidates(decision, breadth, tf)
    return {
        'version': VERSION, 'checked_kst': now_kst().isoformat(timespec='seconds'),
        'btc': tf, 'breadth': breadth, 'decision': decision, 'timing': timing, 'candidates': candidate_data, 'warnings': warnings,
        'rules': {
            'trend': '마감봉 종가 > SMA5 > SMA10 = 상승; 종가 < SMA5 < SMA10 = 하락; 나머지 혼조',
            'long': '월봉·주봉 합의', 'short': '4시간봉·1시간봉 합의', 'day': '일봉은 참고 표시',
            'micro': '30분·15분·5분은 단기 움직임 관찰 전용. 매수 허용 여부와 기존 시장 판단을 변경하지 않음',
            'breadth': '원화 알트코인 전일 대비 상승 참여율 >=55%이면 상승 참여 우세(임시 연구 기준)',
            'coverage': '전체 현존 KRW 알트의 80% 이상·20종목 이상 확인해야 판정',
            'concentration': 'BTC·스테이블코인·14일 이전 KRW 일봉이 없는 코인은 거래대금/집중도에서 제외. 정확한 상장일 추정 아님. 임시 집중 기준 1위 >=30% 또는 상위 10개 >=75%. 매수 허용 판정 불변',
            'age_filter': '기준 시각(KST)보다 14일 이전 일봉이 확인된 KRW 코인만 정제 거래대금에 포함. 조회 실패는 별도 제외·경고. 최초 검사 때 추가 API 조회 발생',
            'candidate': '시장 탐색 허용 시 정제 거래대금 상위 20개에서 단타 또는 스윙 관찰 후보 최대 2개. 조건은 후보 선택 연구 가설이며 BUY 신호 아님',
            'diagnostic': '진단 버튼 누를 때만 시장 판정과 분리해 정제 종목 20개 이내에서 단타·스윙 참고 각 1개. 조건 미충족도 명시하며 실제 추천/BUY 아님',
            'caveat': '수익성 미검증·현재 상장 코인 기준·자동 주문/매수 신호 아님',
        },
    }


def status_cached(force=False):
    with LOCK:
        if CACHE['result'] is not None and not force and time.time() - CACHE['time'] < CACHE_SECONDS:
            return CACHE['result']
        result = compute_status()
        CACHE.update(time=time.time(), result=result)
        return result


def csv_safe(value):
    """Prevent formula injection when a CSV is opened in spreadsheet software."""
    if value is None:
        return ''
    s = str(value)
    if s and s[0] in ('=', '+', '@', '\t', '\r', '\n'):
        return "'" + s
    if s.startswith('-'):
        try:
            float(s)
        except ValueError:
            return "'" + s
    return s


def export_csv(result):
    """One UTF-8 BOM CSV: gate + timing + eight TF + breadth + ticker snapshots."""
    output = io.StringIO(newline='')
    fields = ['row_type', 'version', 'checked_kst', 'market', 'timeframe', 'state', 'value',
              'close', 'ma5', 'ma10', 'last_closed_start', 'last_closed_end', 'rising_pct', 'change_rate_pct',
              'turnover_24h_krw', 'valid_markets', 'total_markets', 'decision', 'timing', 'reason',
              'top_market', 'top1_turnover_24h_krw', 'top1_share_pct', 'top10_share_pct',
              'turnover_ex_top1_24h_krw', 'turnover_rank', 'turnover_share_pct',
              'raw_turnover_24h_krw', 'adjusted_turnover_24h_krw', 'stable_excluded_krw',
              'recent_excluded_krw', 'unverified_excluded_krw', 'age_status',
              'age_evidence', 'turnover_included', 'exclusion_reason', 'turnover_cutoff_kst', 'age_checked_share_pct',
              'candidate_mode', 'candidate_gate', 'pool_size', 'candidate_checked',
              'candidate_rank', 'diagnostic_mode', 'diagnostic_qualified', 'diagnostic_note', 'snapshot_kst', 'diagnostic_checked_kst', 'daily_state', 'h4_state', 'h1_state', 'return_4h_pct', 'btc_return_4h_pct',
              'relative_4h_pct', 'h4_distance_ma5_pct', 'last_price', 'selected', 'can_search', 'candidate_rule']
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    stamp = result['checked_kst']
    base = {'version': VERSION, 'checked_kst': stamp}

    def write(kind, **kwargs):
        writer.writerow({k: csv_safe(v) for k, v in {**base, 'row_type': kind, **kwargs}.items()})

    dec = result['decision']
    br = result['breadth']
    timing = result['timing']
    write('SUMMARY', market='KRW-BTC', state=dec['key'], decision=dec['label'],
          timing=timing['label'], reason=dec['detail'],
          rising_pct=br['rising_pct'], valid_markets=br['valid_markets'], total_markets=br['total_markets'])
    write('MICRO_TIMING', market='KRW-BTC', state=timing['key'], timing=timing['label'], reason=timing['detail'])
    for name, val in result['rules'].items():
        write('RULE', state=name, reason=val)
    for tf_key, obj in result['btc'].items():
        write('BTC_TIMEFRAME', market='KRW-BTC', timeframe=tf_key, state=obj['state'],
              close=obj.get('close'), ma5=obj.get('ma5'), ma10=obj.get('ma10'),
              last_closed_start=obj.get('last_closed_start'),
              last_closed_end=obj.get('last_closed_end'), reason=obj.get('reason', ''))
    write('MARKET_BREADTH', market='KRW-ALTS', state=br['state'], rising_pct=br['rising_pct'],
          value=f"상승 {br['rising']} / 하락 {br['falling']} / 보합 {br['flat']}",
          turnover_24h_krw=br['raw_turnover_24h_krw'], valid_markets=br['valid_markets'],
          total_markets=br['total_markets'])
    conc = br['concentration']
    write('TURNOVER_CONCENTRATION', market='KRW-ALTS', state=conc['state'],
          reason='정제 거래대금 내 쏠림 · 신규/스테이블 제외 · 진입 판정에 영향 없음',
          turnover_24h_krw=br['turnover_24h_krw'],
          top_market=conc['top_market'], top1_turnover_24h_krw=conc['top1_krw'],
          top1_share_pct=conc['top1_pct'], top10_share_pct=conc['top10_pct'],
          turnover_ex_top1_24h_krw=conc['excluding_top1_krw'],
          valid_markets=br['valid_markets'], total_markets=br['total_markets'],
          raw_turnover_24h_krw=br['raw_turnover_24h_krw'],
          adjusted_turnover_24h_krw=br['turnover_24h_krw'],
          age_checked_share_pct=conc['age_checked_share_pct'],
          turnover_cutoff_kst=br['turnover_cutoff_kst'],
          stable_excluded_krw=br['excluded_turnover_by_reason']['stablecoin'],
          recent_excluded_krw=br['excluded_turnover_by_reason']['recent_history'],
          unverified_excluded_krw=br['excluded_turnover_by_reason']['unverified'])
    for category, amount in br['excluded_turnover_by_reason'].items():
        write('TURNOVER_EXCLUSION_SUMMARY', state=category, value=br['excluded_counts'][category],
              turnover_24h_krw=amount, reason='정제 거래대금 제외')
    ranked = sorted((r for r in br['rows'] if r['turnover_included']), key=lambda x: (-x['turnover_24h_krw'], x['market']))
    order = {row['market']: i for i, row in enumerate(ranked, 1)}
    total_value = br['turnover_24h_krw']
    for row in br['rows']:
        write('MARKET_TICKER', market=row['market'], state='up' if row['rising'] else 'not_up',
              change_rate_pct=row['change_rate_pct'], turnover_24h_krw=row['turnover_24h_krw'],
              turnover_rank=order.get(row['market']),
              turnover_share_pct=round(row['turnover_24h_krw'] * 100 / total_value, 4) if total_value and row['turnover_included'] else None,
              age_status=row['age_status'], age_evidence=row['age_evidence'],
              turnover_included=row['turnover_included'], exclusion_reason=row['exclusion_reason'])
    for row in br['excluded_markets']:
        write('EXCLUDED_MARKET', market=row['market'], state=row['age_status'],
              turnover_24h_krw=row['turnover_24h_krw'], reason=row['exclusion_reason'],
              age_evidence=row['age_evidence'], turnover_cutoff_kst=br['turnover_cutoff_kst'])
    candidate_data = result.get('candidates', {})
    write('CANDIDATE_GATE', state=candidate_data.get('state'), candidate_gate=dec.get('key'),
          decision=candidate_data.get('label'), can_search=dec.get('can_search'),
          pool_size=candidate_data.get('pool_size'), candidate_checked=candidate_data.get('checked'),
          value=len(candidate_data.get('candidates', [])))
    for key, rule in candidate_data.get('rules', {}).items():
        write('CANDIDATE_RULE', state=key, candidate_rule=rule)
    ranked_candidates = candidate_data.get('candidates', [])
    ranks = {r['market']: i for i, r in enumerate(ranked_candidates, 1)}
    for r in candidate_data.get('reviews', []):
        common = dict(market=r['market'], state=r['state'], reason=r.get('reason'),
                      change_rate_pct=r.get('change_rate_pct'), turnover_24h_krw=r.get('turnover_24h_krw'),
                      candidate_mode=r.get('selected_mode'), candidate_rank=ranks.get(r['market']),
                      selected=r['market'] in ranks,
                      daily_state=r.get('daily_state'), h4_state=r.get('h4_state'), h1_state=r.get('h1_state'),
                      return_4h_pct=r.get('return_4h_pct'), btc_return_4h_pct=r.get('btc_return_4h_pct'),
                      relative_4h_pct=r.get('relative_4h_pct'), h4_distance_ma5_pct=r.get('h4_distance_ma5_pct'),
                      last_price=r.get('last_price'), last_closed_end=r.get('last_closed_end'))
        write('CANDIDATE_REVIEW', **common)
        if r['market'] in ranks:
            write('CANDIDATE_SELECTED', **common)
    for w in candidate_data.get('warnings', []):
        write('CANDIDATE_WARNING', reason=w)
    # Diagnostic review is completely separate from official market-gated selections.
    diag = result.get('diagnostic')
    if diag:
        write('DIAGNOSTIC_GATE', state=diag['state'], decision=diag['market_gate_label'],
              value=len(diag['candidates']), pool_size=diag['pool_size'], candidate_checked=diag['checked'],
              snapshot_kst=diag['snapshot_kst'], diagnostic_checked_kst=diag['checked_kst'],
              reason='기존 시장 허용 판정 불변 · 진단 버튼 수동 실행 · 실전 BUY 아님')
        write('DIAGNOSTIC_RULE', reason=diag['rule'])
        diag_ranks = {r['market']:i for i,r in enumerate(diag['candidates'],1)}
        for r in diag['reviews']:
            common = dict(market=r['market'], state=r['state'], reason=r.get('reason'),
                          change_rate_pct=r.get('change_rate_pct'), turnover_24h_krw=r.get('turnover_24h_krw'),
                          diagnostic_mode=r.get('diagnostic_mode'), candidate_rank=diag_ranks.get(r['market']),
                          selected=r['market'] in diag_ranks,
                          daily_state=r.get('daily_state'), h4_state=r.get('h4_state'), h1_state=r.get('h1_state'),
                          relative_4h_pct=r.get('relative_4h_pct'), h4_distance_ma5_pct=r.get('h4_distance_ma5_pct'),
                          last_closed_end=r.get('last_closed_end'))
            write('DIAGNOSTIC_REVIEW', **common)
        for r in diag['candidates']:
            write('DIAGNOSTIC_PICK', market=r['market'], state='REFERENCE_ONLY',
                  reason=r.get('reason'), candidate_rank=diag_ranks[r['market']],
                  diagnostic_mode=r['diagnostic_mode'], diagnostic_qualified=r['diagnostic_qualified'],
                  diagnostic_note=r['diagnostic_note'],
                  snapshot_kst=diag['snapshot_kst'], diagnostic_checked_kst=diag['checked_kst'],
                  change_rate_pct=r['change_rate_pct'], turnover_24h_krw=r['turnover_24h_krw'],
                  daily_state=r.get('daily_state'), h4_state=r.get('h4_state'), h1_state=r.get('h1_state'),
                  relative_4h_pct=r.get('relative_4h_pct'), h4_distance_ma5_pct=r.get('h4_distance_ma5_pct'),
                  last_closed_end=r.get('last_closed_end'))
        for warning in diag['warnings']:
            write('DIAGNOSTIC_WARNING', reason=warning)
    for w in result['warnings']:
        write('DATA_WARNING', reason=w)
    return ('\ufeff' + output.getvalue()).encode('utf-8')


PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>BITHUMB V3.16 · 시장 판단 + 진단 미리보기</title>
<style>
:root{color-scheme:dark;--bg:#0b1324;--panel:#142038;--border:#2a3a55;--muted:#91a3c2;--white:#edf2fe;--green:#52d5a0;--red:#f28e91;--amber:#ffcf77}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(170deg,#12223d 0,#0b1324 45%);color:var(--white);font-family:system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:760px;margin:0 auto;padding:22px 15px 56px}.eyebrow{font-size:12px;letter-spacing:.14em;color:var(--green);font-weight:700}
h1{font-size:25px;margin:6px 0 7px;letter-spacing:-.04em}p{color:var(--muted);line-height:1.5;font-size:13px}header{margin-bottom:20px}
.panel{background:#142038;border:1px solid var(--border);border-radius:18px;padding:18px;margin:13px 0;box-shadow:0 10px 24px #050b1822}
.hero{border-top:3px solid #637d9c}.microline{font-size:13px;color:#d7e4f7;padding-top:11px;margin-top:10px;border-top:1px solid #2a3a55}.microline b{font-weight:800}.microline small{color:var(--muted);display:block;margin-top:5px;font-size:11px;line-height:1.4}.hero.scalp,.hero.trend{border-top-color:var(--green)}.hero.swing{border-top-color:var(--amber)}.hero.wait,.hero.unknown{border-top-color:var(--red)}
.kicker{font-size:12px;color:var(--muted);font-weight:600;margin-bottom:6px}.headline{font-size:29px;font-weight:800;letter-spacing:-.04em;margin:1px 0 7px}.subline{font-size:14px;color:#cddaf0;line-height:1.5}.notice{font-size:13px;color:var(--muted);line-height:1.55;margin-top:9px}
.sectiontitle{font-weight:700;font-size:16px;margin:0 0 13px}.chips{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:7px}.microchips{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}.microcaption{font-size:11px;color:var(--muted);margin:12px 0 7px}
.chip{background:#0e192e;border:1px solid #253754;border-radius:11px;text-align:center;padding:12px 3px;min-width:0}
.chip .name{font-size:11px;color:var(--muted);white-space:nowrap}.chip .state{font-size:15px;font-weight:750;margin-top:6px;white-space:nowrap}
.up{color:var(--green)}.down{color:var(--red)}.side{color:var(--amber)}.unknown{color:#93a2bd}
.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.stat{background:#0e192e;border-radius:11px;padding:13px 7px;text-align:center}
.stat small{font-size:11px;color:var(--muted);display:block;margin-bottom:6px}.stat b{font-size:20px;font-variant-numeric:tabular-nums}
.meter{height:9px;border-radius:9px;background:#273750;overflow:hidden;margin:15px 0 6px}.meter>div{height:100%;width:0;background:var(--green);transition:width .4s}
.concentration{border-top:1px solid #2a3a55;margin-top:12px;padding-top:11px}.conctitle{display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:13px;font-weight:750;margin-bottom:3px}.conctitle b{color:var(--muted)}.conctitle b.high{color:var(--amber)}.conctitle b.normal{color:var(--green)}
.actions{display:flex;gap:9px;margin:19px 0 6px}button{font:inherit;border:0;border-radius:11px;padding:13px 16px;font-weight:750;cursor:pointer}
#go{background:#54dfac;color:#052219;flex:1}#csv{background:#263a57;color:var(--white);border:1px solid #405676;flex:1}button:disabled{opacity:.45;cursor:wait}
#preview{background:#324560;color:#edf2fe;width:100%;border:1px solid #556b89;margin:8px 0} .previewpanel{border-color:#53627c}.diagnostic-tag{font-size:11px;font-weight:750;color:var(--amber)}.candidate .risk{color:var(--amber)}.candidates{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.candidate{background:#0e192e;border:1px solid #334968;border-radius:12px;padding:12px;min-width:0}.candidate strong{display:block;font-size:18px;margin:5px 0}.candidate .mode{font-size:11px;color:var(--green)}.candidate p{font-size:12px;margin:5px 0;overflow-wrap:anywhere}.emptybox{font-size:13px;color:var(--muted);line-height:1.65}.statusline{font-size:12px;color:var(--muted);min-height:17px;margin-top:8px}.small{font-size:12px;color:var(--muted);line-height:1.6}
.info{display:flex;justify-content:space-between;gap:9px;border-bottom:1px solid #273650;padding:8px 0;font-size:12px}
.info:last-child{border-bottom:0}.info span{color:var(--muted)}.info b{text-align:right;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
footer{font-size:12px;color:#7d90af;line-height:1.6;margin-top:20px}
@media(max-width:390px){main{padding:17px 11px}.panel{padding:14px}.chip .name{font-size:10px}.chip .state{font-size:13px}.stats{gap:5px}.stat b{font-size:17px}.actions{flex-direction:column}.candidates{grid-template-columns:1fr}h1{font-size:22px}}
</style></head><body><main>
<header><div class="eyebrow">BITHUMB · MARKET GATE</div><h1>V3.16 시장 + 코인 진단</h1><p>① 시장 진입 가능한가? → ② 산다면 어떤 코인인가?<br>시장 관망 시에도 별도 진단 버튼으로 코인 2개 비교 · BUY 신호 아님</p></header>
<section id="hero" class="panel hero unknown"><div class="kicker">BTC + 빗썸 원화시장</div><div id="decision" class="headline">검사 전</div><div id="action" class="subline">버튼을 눌러 시장 상태를 확인해.</div><div id="detail" class="notice">매수 신호가 아닌 시장 상태 연구용 표시야.</div><div class="microline">단기 움직임: <b id="timing">검사 전</b><small id="timingdetail">30·15·5분 마감봉으로만 관찰</small></div></section>
<section class="panel"><h2 class="sectiontitle">BTC 8개 시간봉 · 마감봉만</h2><div class="chips" id="chips">
<div class="chip"><div class="name">월봉</div><div class="state unknown">—</div></div><div class="chip"><div class="name">주봉</div><div class="state unknown">—</div></div><div class="chip"><div class="name">일봉</div><div class="state unknown">—</div></div><div class="chip"><div class="name">4시간</div><div class="state unknown">—</div></div><div class="chip"><div class="name">1시간</div><div class="state unknown">—</div></div></div><div class="microcaption">작은 움직임 · 관찰 전용</div><div id="microchips" class="microchips"><div class="chip"><div class="name">30분</div><div class="state unknown">—</div></div><div class="chip"><div class="name">15분</div><div class="state unknown">—</div></div><div class="chip"><div class="name">5분</div><div class="state unknown">—</div></div></div><p class="small">마감봉 종가 &gt; 5이평 &gt; 10이평 = 상승, 역순 = 하락. 월·주 = 장기, 4시간·1시간 = 시장 방향, 30·15·5분 = 타이밍 관찰. 분봉 상승만으로 매수 허용하지 않아.</p></section>
<section class="panel"><h2 class="sectiontitle">알트코인 상승 참여</h2><div class="stats"><div class="stat"><small>상승 참여율</small><b id="breadth">—</b></div><div class="stat"><small>상승 / 확인</small><b id="rising">—</b></div><div class="stat"><small>정제 24시간 거래대금</small><b id="money">—</b></div></div><div class="meter"><div id="meterfill"></div></div><p id="mnote" class="small">현재 상장된 빗썸 원화 알트코인의 전일 대비 상승 비율. 55% 이상은 임시 연구 기준.</p><div class="concentration"><div class="conctitle"><span>거래대금 집중</span><b id="concstate">분석 전</b></div><div id="concinfo" class="small">신규·스테이블 제외 후 거래대금 쏠림 확인</div><div id="concrest" class="small"></div><div id="exclusions" class="small"></div></div></section>
<section class="panel"><h2 class="sectiontitle">② 공식 종목 관찰 후보 · 시장 허용 시만</h2><div id="candidatebox" class="emptybox">시장 분석 전 · 시장이 허용할 때만 종목 탐색</div><p id="candidatenote" class="small">기존 일반 알트 중 거래대금 상위 20개만 검사. 시장 관망이면 종목 캔들을 조회하지 않아.</p></section>
<section class="panel previewpanel"><h2 class="sectiontitle">③ 진단용 코인 미리보기 · 매수 추천 아님</h2>
<p class="small">시장 <b>관망</b>이어도 버튼을 누르면 기존 일반 알트 상위 20개를 따로 확인해. 공식 매수 후보는 여전히 0개야.</p>
<button id="preview" disabled>진단용 후보 미리보기</button>
<div id="previewbox" class="emptybox">시장 분석 후 별도 버튼을 눌러줘.</div>
<p id="previewnote" class="small">단타 1개 · 스윙 1개를 비교용으로만 표시. 기준 미충족·시장 관망 상태에서도 BUY로 해석하면 안 돼.</p></section>
<section class="panel"><h2 class="sectiontitle">판정 요약</h2><div class="info"><span>BTC 장기 / 단기</span><b id="direction">— / —</b></div><div class="info"><span>시장 판단</span><b id="state">—</b></div><div class="info"><span>단기 움직임</span><b id="microstate">—</b></div><div class="info"><span>자료 확인 비율</span><b id="coverage">—</b></div><div class="info"><span>검사 시각 (KST)</span><b id="checked">—</b></div><div class="statusline" id="warning"></div></section>
<div class="actions"><button id="go">시장 + 후보 분석하기</button><button id="csv" disabled>결과 CSV 저장</button></div><div id="msg" class="statusline">첫 번째 연구판 · 자동 주문 기능 없음</div>
<footer>단타는 짧은 가격 움직임, 스윙은 더 큰 가격 움직임을 관찰한다는 의미야. 투자금을 크게 늘리라는 뜻은 아니야. 이 규칙은 아직 수익성 검증 전이며 진입 결정은 별도 확인해야 해.</footer>
</main><script>
'use strict';
const $ = id => document.getElementById(id);
const labels={up:'상승',down:'하락',side:'혼조',unknown:'자료부족',mixed:'혼조',strong:'상승 참여 우세',weak:'상승 참여 약세',mixedMarket:'혼조'};
const fmt = v => v===null||v===undefined?'—':Number(v).toLocaleString('ko-KR');
const f = v => Number.isFinite(v)?Number(v).toFixed(1)+'%':'—';
function showPreview(pre){
 const root=$('previewbox'); root.replaceChildren();
 if(!pre){root.className='emptybox';root.textContent='아직 진단을 실행하지 않았어. 시장 판정과 관계없이 별도 버튼으로 검사할 수 있어.';
  $('previewnote').textContent='진단 결과는 공식 후보·매수 허용 판단을 변경하지 않아.';return;}
 const entries=Array.isArray(pre.candidates)?pre.candidates:[];
 if(!entries.length){root.className='emptybox';root.textContent=pre.label||'비교할 데이터 없음';}
 else{
  root.className='candidates';
  for(const item of entries){
   const card=document.createElement('div');card.className='candidate';
   const tag=document.createElement('div');tag.className='diagnostic-tag';tag.textContent=item.diagnostic_mode+' · '+(item.diagnostic_qualified?'관찰 기준 충족':'기준 미충족');
   const name=document.createElement('strong');name.textContent=item.market||'—';
   const stats=document.createElement('p');stats.textContent='24시간 '+f(item.change_rate_pct)+' · 상대강도 '+f(item.relative_4h_pct);
   const desc=document.createElement('p');desc.textContent='일봉 '+(labels[item.daily_state]||'?')+' / 4시간 '+(labels[item.h4_state]||'?')+' / 1시간 '+(labels[item.h1_state]||'?');
   const risk=document.createElement('p');risk.className='risk';risk.textContent='시장과 무관한 참고용 · 매수 신호 아님';
   card.append(tag,name,stats,desc,risk);root.append(card);
  }
 }
 $('previewnote').textContent='시장 판정 '+(pre.market_gate_label||'—')+' 유지 · '+fmt(pre.checked)+' / '+fmt(pre.pool_size)+'개 조사 · 진단 '+fmt(entries.length)+'개 · 데이터 '+String(pre.checked_kst||'').replace('T',' ').slice(0,19)+' KST';
}
function show(d){
 const dec=d.decision, br=d.breadth, micro=d.timing||{label:'자료 확인 필요',detail:'분봉 자료 없음'};
 const cand=d.candidates||{state:'BLOCKED',label:'시장 관망',candidates:[],checked:0,pool_size:0};
 $('hero').className='panel hero '+dec.key;
 $('decision').textContent=dec.label; $('action').textContent=dec.action; $('detail').textContent=dec.detail;
 $('timing').textContent=micro.label;$('timingdetail').textContent=micro.detail;$('microstate').textContent=micro.label;
 $('chips').innerHTML='';
 for(const [key,label] of [['month','월봉'],['week','주봉'],['day','일봉'],['4h','4시간'],['1h','1시간']]){
  const value=d.btc[key]||{state:'unknown'};
  const cell=document.createElement('div');cell.className='chip';
  const name=document.createElement('div');name.className='name';name.textContent=label;
  const state=document.createElement('div');state.className='state '+value.state;state.textContent=labels[value.state]||'자료부족';
  cell.append(name,state);$('chips').append(cell);
 }
 $('microchips').replaceChildren();
 for(const [key,label] of [['30m','30분'],['15m','15분'],['5m','5분']]){
  const value=d.btc[key]||{state:'unknown'};
  const cell=document.createElement('div');cell.className='chip';
  const name=document.createElement('div');name.className='name';name.textContent=label;
  const state=document.createElement('div');state.className='state '+value.state;state.textContent=labels[value.state]||'자료부족';
  cell.append(name,state);$('microchips').append(cell);
 }
 $('breadth').textContent=f(br.rising_pct);$('rising').textContent=fmt(br.rising)+' / '+fmt(br.valid_markets);
 $('money').textContent=br.turnover_24h_krw>0?(br.turnover_24h_krw/1e8).toLocaleString('ko-KR',{maximumFractionDigits:0})+'억':'—';
 $('meterfill').style.width=Number.isFinite(br.rising_pct)?Math.max(0,Math.min(100,br.rising_pct))+'%':'0%';
 $('mnote').textContent='상승 참여율은 이전과 동일 · 거래대금만 신규 이력/스테이블 제외 · 55%는 임시 기준';
 const c=br.concentration||{};
 $('concstate').textContent=c.state==='high'?'높음 · 주의':c.state==='normal'?'기준 미만':'자료 확인 필요';
 $('concstate').className=c.state||'unknown';
 if(Number.isFinite(c.top1_pct)&&Number.isFinite(c.top10_pct)){
  $('concinfo').textContent='1위 '+String(c.top_market||'—')+' '+f(c.top1_pct)+' · 상위 10개 '+f(c.top10_pct);
  $('concrest').textContent='1위 제외 '+(c.excluding_top1_krw/1e8).toLocaleString('ko-KR',{maximumFractionDigits:0})+'억 · 집중 기준은 임시값';
 }else{
  $('concinfo').textContent='유효 종목 또는 거래대금 자료 부족'; $('concrest').textContent='시장 판정과 분리된 참고 정보야.';
 }
 const ex=br.excluded_turnover_by_reason||{}; const ec=br.excluded_counts||{};
 $('exclusions').textContent='거래대금 제외: 신규·이력부족 '+fmt(ec.recent_history||0)+'개 / '+(Number(ex.recent_history||0)/1e8).toLocaleString('ko-KR',{maximumFractionDigits:0})+'억 · 스테이블 '+fmt(ec.stablecoin||0)+'개 / '+(Number(ex.stablecoin||0)/1e8).toLocaleString('ko-KR',{maximumFractionDigits:0})+'억'+(Number(ec.unverified||0)>0?' · 이력 조회 실패 '+fmt(ec.unverified)+'개':'');
 $('direction').textContent=(labels[dec.long]||'혼조')+' / '+(labels[dec.short]||'혼조');
 $('state').textContent=br.state==='strong'?'상승 참여 우세':br.state==='mixed'?'혼조':br.state==='weak'?'상승 참여 약세':'자료 부족';
 $('coverage').textContent=f(br.coverage_pct)+' ('+fmt(br.valid_markets)+' / '+fmt(br.total_markets)+')';
 $('checked').textContent=d.checked_kst.replace('T',' ').slice(0,19);
 $('warning').textContent=(d.warnings||[]).length?'자료 경고: '+d.warnings.join(' / '):'마감된 BTC 캔들만 사용 · 거래 신호 아님';
 const root=$('candidatebox'); root.replaceChildren();
 if(!Array.isArray(cand.candidates)||cand.candidates.length===0){
  root.className='emptybox';root.textContent=cand.label||'관찰 후보 없음';
 }else{
  root.className='candidates';
  for(const item of cand.candidates.slice(0,2)){
   const card=document.createElement('div');card.className='candidate';
   const mode=document.createElement('div');mode.className='mode';mode.textContent=item.selected_mode||'관찰 후보';
   const title=document.createElement('strong');title.textContent=item.market||'—';
   const summary=document.createElement('p');summary.textContent='24시간 '+f(item.change_rate_pct)+' · 거래대금 '+fmt(Math.round((item.turnover_24h_krw||0)/1e8))+'억';
   const detail=document.createElement('p');detail.textContent=item.selected_mode==='단타 관찰'?'BTC 대비 4시간 '+f(item.relative_4h_pct):'4시간 MA5 거리 '+f(item.h4_distance_ma5_pct);
   card.append(mode,title,summary,detail);root.append(card);
  }
 }
 $('candidatenote').textContent='시장 '+dec.label+' · 검사 '+fmt(cand.checked)+' / '+fmt(cand.pool_size)+'개 · 후보는 매수 신호가 아니며 진입 위치와 위험은 아직 미검증';
 showPreview(d.diagnostic||null);$('preview').disabled=false;
 $('csv').disabled=false;
}
async function go(){
 $('go').disabled=true;$('go').textContent='시장 + 후보 분석 중…';$('msg').textContent='BTC 8개 시간봉 → 시장 상태 → 허용 시 후보 20개를 확인하고 있어.';
 try{
  const r=await fetch('/api/status?refresh=1',{cache:'no-store'});const d=await r.json();
  if(!r.ok)throw Error(d.error||'조회 실패');show(d);$('msg').textContent='분석 완료 · 데이터 상태를 확인해줘.';
 }catch(e){$('msg').textContent='조회 오류: '+String(e.message||e).slice(0,150)}
 finally{$('go').disabled=false;$('go').textContent='다시 분석하기'}
}
async function previewOnly(){
 $('preview').disabled=true;$('preview').textContent='별도 진단 중…';
 $('previewnote').textContent='이 분석은 시장 판정과 분리되어 있어. 기존 알트 20개 이하만 검사해.';
 try{
  const r=await fetch('/api/preview',{cache:'no-store'});const d=await r.json();
  if(!r.ok)throw Error(d.error||'진단 조회 실패');
  showPreview(d.diagnostic);$('msg').textContent='진단 미리보기 완료 · 공식 시장 판정은 변경되지 않았어.';
 }catch(e){$('previewnote').textContent='진단 오류: '+String(e.message||e).slice(0,140);}
 finally{$('preview').disabled=false;$('preview').textContent='진단용 후보 미리보기';}
}
async function download(){
 $('csv').disabled=true;
 try{
  const r=await fetch('/api/csv',{cache:'no-store'});
  if(!r.ok)throw Error('CSV를 가져오지 못했어');
  const blob=await r.blob(),a=document.createElement('a'),url=URL.createObjectURL(blob);
  const cd=r.headers.get('Content-Disposition')||'';const name=cd.match(/filename="([^"]+)"/);
  a.href=url;a.download=name?name[1]:'BITHUMB_V3_16_MARKET_GATE.csv';
  document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000);
 }catch(e){$('msg').textContent=String(e.message||e)}finally{$('csv').disabled=false}
}
$('go').addEventListener('click',go);$('csv').addEventListener('click',download);$('preview').addEventListener('click',previewOnly);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print('[MARKET GATE]', fmt % args)

    def respond(self, code, payload, content_type='application/json', filename=None):
        if isinstance(payload, bytes):
            raw = payload
        elif content_type.startswith('application/json'):
            raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        else:
            raw = payload.encode('utf-8')
        try:
            self.send_response(code)
            self.send_header('Content-Type', content_type+'; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if filename:
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/':
            return self.respond(200, PAGE, 'text/html')
        if path in ('/health', '/api/ping'):
            return self.respond(200, {'ok': True, 'version': VERSION, 'trading': False})
        if path == '/api/status':
            try:
                return self.respond(200, status_cached(force='refresh=1' in parsed.query))
            except Exception as exc:
                return self.respond(502, {'error': str(exc)[:300]})
        if path == '/api/preview':
            try:
                return self.respond(200, {'diagnostic': preview_cached()})
            except ValueError as exc:
                return self.respond(409, {'error': str(exc)[:200]})
            except Exception as exc:
                return self.respond(502, {'error': str(exc)[:200]})
        if path == '/api/csv':
            with LOCK:
                result = CACHE['result']
            if result is None:
                return self.respond(409, {'error': '먼저 시장 분석을 실행해줘.'})
            stamp = result['checked_kst'].replace('-', '').replace(':', '').replace('T', '_')[:15]
            return self.respond(200, export_csv(result), 'text/csv', f'BITHUMB_V3_16_MARKET_GATE_{stamp}.csv')
        return self.respond(404, {'error': '없는 주소야.'})


def main():
    port = int(os.getenv('PORT', '8080'))
    host = '0.0.0.0' if os.getenv('PORT') else '127.0.0.1'
    server = ThreadingHTTPServer((host, port), Handler)
    print(f'BITHUMB {VERSION} MARKET GATE: http://127.0.0.1:{port}', flush=True)
    print('공개시세 연구용 · 매수/주문 기능 없음', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
