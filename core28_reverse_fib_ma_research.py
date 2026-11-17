#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CORE.28 independent, read-only reverse study: Bithumb historical 5m candles.

Not part of the scanner. No trades, no private API keys, no changes to BUY rules.
Execute on a machine/CI runner with internet access:
    python core28_reverse_fib_ma_research.py --out core28_results
Or verify calculation without network:
    python core28_reverse_fib_ma_research.py --self-test

The 14 case timestamps and 60m outcomes below are transcribed from
CORE27_reverse_downtrend_cases_8days.csv, based on user-supplied CORE25/27 CSVs.
Time is KST throughout. ASOF features are built ONLY from completed candles.
POSTHOC columns describe what happened later and MUST NOT be used as entry features.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = 'https://api.bithumb.com/v1/candles/minutes/5'
FIB_LEVELS = (0.236, 0.382, 0.5, 0.618, 0.786)
FEE_ROUNDTRIP_PCT = 0.50
FIVE = timedelta(minutes=5)

# date, market, 10min recheck (KST), 60-minute TIME EXIT net % or blank
CASES = '''2026-11-10,KRW-MERL,2026-11-10 06:20,0.677973
2026-11-10,KRW-HBAR,2026-11-10 15:00,
2026-11-10,KRW-Q,2026-11-10 22:20,1.147132
2026-11-12,KRW-ETH,2026-11-12 07:30,-0.889372
2026-11-12,KRW-AVAX,2026-11-12 21:30,-1.371571
2026-11-12,KRW-BTC,2026-11-12 21:30,-0.797811
2026-11-12,KRW-HBAR,2026-11-12 23:50,-1.219694
2026-11-13,KRW-AVAX,2026-11-13 17:20,0.478491
2026-11-14,KRW-IRYS,2026-11-14 06:30,
2026-11-14,KRW-PENGU,2026-11-14 17:30,0.024806
2026-11-15,KRW-NEAR,2026-11-15 17:40,1.517225
2026-11-16,KRW-DOGE,2026-11-16 01:40,-0.325965
2026-11-16,KRW-Q,2026-11-16 09:10,-2.460667
2026-11-16,KRW-BIRB,2026-11-16 21:50,-4.099262'''


def dt(s):
    return datetime.strptime(s[:16], '%Y-%m-%d %H:%M')


def stamp(d):
    return d.strftime('%Y-%m-%d %H:%M')


def load_cases(path=None):
    if path:
        with open(path, 'r', encoding='utf-8-sig', newline='') as f:
            source = list(csv.DictReader(f))
        cases = []
        for r in source:
            cases.append(dict(date=r['날짜'], market=r['코인'], asof=dt(r['10분 재검증 시각(KST)']),
                              legacy_net=float(r['60분 시간청산 순수익률(%)']) if r['60분 시간청산 순수익률(%)'] else None))
    else:
        cases = []
        for line in CASES.splitlines():
            d, m, t, v = line.split(',')
            cases.append(dict(date=d, market=m, asof=dt(t), legacy_net=float(v) if v else None))
    return sorted(cases, key=lambda x: (x['asof'], x['market']))


def fetch_bars(market, start, end, pause=0.22, retries=4):
    """End exclusive, KST string, fetch 200 candles per page; no forward-fill."""
    found = {}
    cursor = end
    reasons = []
    for page in range(4):
        params = urlencode({'market': market, 'count': 200,
                            'to': cursor.strftime('%Y-%m-%d %H:%M:%S')})
        req = Request(API + '?' + params, headers={'Accept': 'application/json',
                                                    'User-Agent': 'CORE28-Bithumb-Historical-Research/1.0'})
        last_error = None
        payload = None
        for attempt in range(retries):
            try:
                with urlopen(req, timeout=15) as r:
                    payload = json.loads(r.read().decode('utf-8'))
                if not isinstance(payload, list):
                    raise ValueError(f'bad Bithumb response ({type(payload).__name__})')
                last_error = None
                break
            except (HTTPError, URLError, TimeoutError, ValueError) as e:
                last_error = e
                if isinstance(e, HTTPError) and e.code not in (429, 500, 502, 503, 504):
                    break
                time.sleep(min(2.0, 0.6 * (attempt + 1)))
        if last_error is not None:
            raise RuntimeError(f'{market} page {page+1}: {last_error}')
        oldest = None
        for b in payload:
            try:
                t = dt(b['candle_date_time_kst'])
                o, h, l, c = (float(b[k]) for k in
                              ('opening_price', 'high_price', 'low_price', 'trade_price'))
                vol = float(b['candle_acc_trade_volume'])
                val = float(b.get('candle_acc_trade_price') or 0.0)
                if not (0 < l <= min(o, c) <= h and h >= max(o, c) and
                        all(map(math.isfinite, (o, h, l, c, vol, val))) and vol >= 0 and val >= 0):
                    continue
                oldest = min(oldest, t) if oldest else t
                if start <= t < end:
                    found[t] = dict(market=market, t=t, o=o, h=h, l=l, c=c, volume=vol, turnover_krw=val)
            except (KeyError, ValueError, TypeError):
                continue
        reasons.append(f'page={page+1},returned={len(payload)},oldest={stamp(oldest) if oldest else "none"}')
        if oldest is None or oldest <= start or len(payload) < 200:
            break
        if oldest >= cursor:
            raise RuntimeError('Bithumb pagination did not advance')
        cursor = oldest
        time.sleep(pause)
    return [found[k] for k in sorted(found)], '; '.join(reasons)


def contiguous(bars):
    return bool(bars) and all(bars[i]['t'] - bars[i-1]['t'] == FIVE for i in range(1, len(bars)))


def ema(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1)
    x = float(values[0])
    out = []
    for v in values:
        x += k * (float(v) - x)
        out.append(x)
    return out


def atr14(bars):
    if len(bars) < 15:
        return None
    tail = bars[-15:]
    if not contiguous(tail):
        return None
    tr = [max(b['h']-b['l'], abs(b['h']-prev['c']), abs(b['l']-prev['c']))
          for prev, b in zip(tail[:-1], tail[1:])]
    return sum(tr) / 14


def classify_nearest(ratio):
    if ratio is None or not math.isfinite(ratio) or ratio < 0 or ratio > 1.15:
        return ('', '')
    nearest = min(FIB_LEVELS, key=lambda x: abs(x-ratio))
    return (round(nearest * 100, 1), round(abs(ratio-nearest)*100, 2))


def swing_anchors(past):
    """Anchors use only as-of history, and require meaningful ordering.

    UP SWING: low before a past high, followed by pullback.
    DOWN SWING: high before a past low, followed by recovery.
    These are hypotheses, not automatic trading supports/resistances.
    """
    tail = past[-48:]
    if len(tail) < 48 or not contiguous(tail):
        return None
    recent_high_pos = max(range(12, 44), key=lambda i: tail[i]['h'])
    low_pos = min(range(max(0, recent_high_pos-24), recent_high_pos-2),
                  key=lambda i: tail[i]['l'])
    up_low, up_high = tail[low_pos]['l'], tail[recent_high_pos]['h']
    # Down leg must start before trough and have a finite distance.
    low_down_pos = min(range(18, 48), key=lambda i: tail[i]['l'])
    high_candidates = range(max(0, low_down_pos-30), low_down_pos-2)
    high_down_pos = max(high_candidates, key=lambda i: tail[i]['h'])
    down_high, down_low = tail[high_down_pos]['h'], tail[low_down_pos]['l']
    atr = atr14(tail)
    # Minimum 0.8% span avoids reporting arbitrary fib levels for flat micro-swings.
    up_ok = up_high > up_low * 1.008 and (recent_high_pos-low_pos) >= 3
    down_ok = down_high > down_low * 1.008 and (low_down_pos-high_down_pos) >= 3
    return dict(up_low=up_low if up_ok else None,
                up_high=up_high if up_ok else None,
                up_low_time=stamp(tail[low_pos]['t']) if up_ok else '',
                up_high_time=stamp(tail[recent_high_pos]['t']) if up_ok else '',
                down_high=down_high if down_ok else None,
                down_low=down_low if down_ok else None,
                down_high_time=stamp(tail[high_down_pos]['t']) if down_ok else '',
                down_low_time=stamp(tail[low_down_pos]['t']) if down_ok else '',
                atr=atr)


def pct(a, b):
    return 100.0 * (a / b - 1.0) if b and b > 0 else None


def completed_tf_ema(past_all, at, tf_minutes):
    """Aggregate past 5m candles only; incomplete 10/15m bars are excluded."""
    n = tf_minutes//5
    by_t = {b['t']: b for b in past_all}
    res = []
    for first in past_all:
        start=first['t']
        if start.minute % tf_minutes or start.second or start+timedelta(minutes=tf_minutes)>at:
            continue
        group=[by_t.get(start+FIVE*i) for i in range(n)]
        if any(b is None for b in group):
            continue
        res.append(dict(t=start,c=group[-1]['c']))
    res.sort(key=lambda x:x['t'])
    tail=res[-20:]
    d=timedelta(minutes=tf_minutes)
    if len(tail)<20 or any(tail[i]['t']-tail[i-1]['t']!=d for i in range(1,len(tail))):
        return {f'tf{tf_minutes}_status':'INSUFFICIENT_OR_GAPPED'}
    # At most one current aggregate period may be unfinished.
    if not timedelta(0) <= (at-(tail[-1]['t']+d)) < d:
        return {f'tf{tf_minutes}_status':'STALE'}
    e5=ema([b['c'] for b in tail],5)
    e10=ema([b['c'] for b in tail],10)
    return {f'tf{tf_minutes}_status':'READY',
            f'tf{tf_minutes}_ema5_slope_pct':pct(e5[-1],e5[-2]),
            f'tf{tf_minutes}_ema10_slope_pct':pct(e10[-1],e10[-2]),
            f'tf{tf_minutes}_ema5_above_ema10':int(e5[-1]>e10[-1]),
            f'tf{tf_minutes}_close_above_ema5':int(tail[-1]['c']>e5[-1])}


def features_at(bars, at):
    past_all = [b for b in bars if b['t'] + FIVE <= at][-100:]
    past = past_all[-48:]
    if len(past) < 48 or not contiguous(past[-48:]) or past[-1]['t']+FIVE != at:
        return dict(feature_status='INSUFFICIENT_OR_MISSING_5M', asof_kst=stamp(at))
    closes = [b['c'] for b in past]
    e5, e10, e20 = (ema(closes, n) for n in (5, 10, 20))
    arr = past[-16:]
    v_prev = sum(b['volume'] for b in arr[:-4]) / 12
    v_last = sum(b['volume'] for b in arr[-4:]) / 4
    anchors = swing_anchors(past)
    atr = atr14(past)
    latest = past[-1]
    res = dict(feature_status='READY', asof_kst=stamp(at), close=latest['c'],
               ema5=e5[-1], ema10=e10[-1], ema20=e20[-1],
               ema5_slope_15m_pct=pct(e5[-1], e5[-4]),
               ema10_slope_15m_pct=pct(e10[-1], e10[-4]),
               ema5_minus_ema10_pct=pct(e5[-1], e10[-1]),
               close_vs_ema20_pct=pct(latest['c'], e20[-1]),
               ema5_above_ema10=int(e5[-1] > e10[-1]),
               ema5_rising=int(e5[-1] > e5[-2]),
               price_above_ema5=int(latest['c'] > e5[-1]),
               low_rising_3=int(past[-3]['l'] <= past[-2]['l'] < past[-1]['l']),
               high_break=int(latest['c'] > max(b['h'] for b in past[-4:-1])),
               volume_ratio_4v12=(v_last/v_prev if v_prev > 0 else None),
               turnover_recent_4_krw=sum(b['turnover_krw'] for b in past[-4:]),
               atr14_pct=(100*atr/latest['c']) if atr else None)
    res.update(completed_tf_ema(past_all, at, 10))
    res.update(completed_tf_ema(past_all, at, 15))
    if anchors:
        res.update(anchors)
        lo, hi = anchors['up_low'], anchors['up_high']
        if lo is not None and hi is not None:
            recent_low = min(b['l'] for b in past[-12:])
            r = (hi - recent_low)/(hi-lo)
            near, err = classify_nearest(r)
            res.update(up_retrace_recent_low_pct=100*r,
                       up_retrace_nearest_fib_pct=near,
                       up_retrace_distance_points=err)
            for level in FIB_LEVELS:
                res[f'up_support_{level*100:g}_price'] = hi-(hi-lo)*level
        dlo, dhi = anchors['down_low'], anchors['down_high']
        if dlo is not None and dhi is not None:
            r = (latest['c']-dlo)/(dhi-dlo)
            near, err = classify_nearest(r)
            res.update(down_recover_close_pct=100*r,
                       down_recover_nearest_fib_pct=near,
                       down_recover_distance_points=err)
            for level in FIB_LEVELS:
                res[f'down_recovery_{level*100:g}_price'] = dlo+(dhi-dlo)*level
    return res


def posthoc(bars, at, asof_features):
    """Future-only diagnosis, never a feature. UTC-free KST dates."""
    future = [b for b in bars if at <= b['t'] < at + timedelta(minutes=60)]
    if len(future) != 12 or not contiguous(future) or future[0]['t'] != at:
        return dict(post_status='UNKNOWN_MISSING_60M')
    bottom = min(future, key=lambda x: x['l'])
    peak = max(future, key=lambda x: x['h'])
    last = future[-1]
    ref = asof_features.get('close')
    result = dict(post_status='READY', post_min_low=bottom['l'],
                  post_min_time_kst=stamp(bottom['t']),
                  post_max_high=peak['h'], post_max_time_kst=stamp(peak['t']),
                  post_close60=last['c'], post_close60_gross_pct=pct(last['c'], ref),
                  post_close60_net_approx_pct=(pct(last['c'], ref)-FEE_ROUNDTRIP_PCT) if ref else None)
    lo, hi = asof_features.get('up_low'), asof_features.get('up_high')
    if lo and hi and hi > lo:
        ratio = (hi-bottom['l'])/(hi-lo)
        near, err = classify_nearest(ratio)
        result.update(posthoc_pullback_low_pct=100*ratio,
                      posthoc_nearest_fib_pct=near,
                      posthoc_fib_distance_points=err)
    # First closing cross of EMA5 after bottom; a confirmative observation only.
    at_after_low = bottom['t'] + FIVE
    for t in (at_after_low + FIVE*i for i in range(12)):
        if t > at + timedelta(minutes=60):
            break
        f = features_at(bars, t)
        if f.get('feature_status') == 'READY' and f['close'] > f['ema5'] and f['ema5_rising']:
            result['posthoc_first_ema5_reclaim_kst'] = stamp(t)
            break
    return result


def dump_csv(path, records):
    if not records:
        return
    fields = sorted(set().union(*(r.keys() for r in records)))
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for row in records:
            writer.writerow({k: (round(v, 7) if isinstance(v, float) and math.isfinite(v) else v)
                             for k, v in row.items()})


def run(cases, out, pause=0.22):
    out.mkdir(parents=True, exist_ok=True)
    rows, checks, raw_all, problems = [], [], {}, []
    for idx, case in enumerate(cases, 1):
        asof = case['asof']
        market = case['market']
        key = f'{market}|{stamp(asof)}'
        base = dict(case_key=key, market=market, asof_kst=stamp(asof),
                    original_60m_time_exit_net_pct=case['legacy_net'],
                    original_60m_outcome='UNKNOWN' if case['legacy_net'] is None else
                    ('PROFIT' if case['legacy_net'] > 0 else 'LOSS'))
        try:
            bars, meta = fetch_bars(market, asof-timedelta(hours=6),
                                    asof+timedelta(minutes=65), pause=pause)
            for b in bars:
                raw_all[(market, b['t'])] = b
            f = features_at(bars, asof)
            p = posthoc(bars, asof, f) if f.get('feature_status') == 'READY' else {'post_status':'SKIPPED_NO_FEATURES'}
            rows.append({**base, **f, **p, 'source':'Bithumb v1 historical 5m API',
                         'raw_bars_collected':len(bars), 'fetch_detail':meta})
            for minutes in (0,5,10,15,20,30,45,60):
                t = asof + timedelta(minutes=minutes)
                snapshot = features_at(bars, t)
                checks.append({**base, 'elapsed_after_recheck_min':minutes, **snapshot})
            print(f'[{idx:02}/{len(cases)}] {market} {stamp(asof)}: {len(bars)} candles, {f["feature_status"]}',flush=True)
        except Exception as exc:
            msg = f'{type(exc).__name__}: {exc}'
            rows.append({**base,'feature_status':'API_ERROR', 'error':msg})
            problems.append(f'{key}: {msg}')
            print(f'[{idx:02}/{len(cases)}] {key} ERROR {msg}',flush=True)
        time.sleep(pause)
    dump_csv(out/'CORE28_case_fib_ema.csv', rows)
    dump_csv(out/'CORE28_recheck_5m_snapshots.csv', checks)
    dump_csv(out/'CORE28_raw_5m_candles.csv',
             [{**b, 't_kst':stamp(b['t'])} for b in raw_all.values()])
    n_good=sum(r.get('feature_status')=='READY' for r in rows)
    lines = [
        '# CORE.28 — Historical 5-minute reversal research (read-only)',
        '', f'- Cases selected from prior CORE25/27 CSV: {len(cases)}',
        f'- Cases with clean completed-5m history: {n_good}',
        f'- API failures: {len(problems)}',
        '- Transaction costs in prior outcome label: 0.25% per side, 0% slippage',
        '- As-of features use ONLY completed candles, with contiguous 48-bar checks',
        '- Fib previous-upswing retracement: (swing high - subsequent observed low) / (swing high - swing low)',
        '- Fib previous-downleg recovery: (as-of close - downleg low) / (downleg high - downleg low)',
        '- Pivot anchors: 48 contiguous 5m historical bars; minimum 0.8% swing amplitude',
        '- POSTHOC columns deliberately use future 60-minute candles and are NEVER eligible as buy filters',
        '- Fib 50% is a widely used convention, not a Fibonacci ratio; levels are hypotheses',
        '- Nearest level by itself is not statistical support; compare successful versus failed cases and non-Fib controls',
        '- A candle with missing 5-minute slots is NOT forward-filled',
        '- Original labels reflect entry at the next candle, and therefore differ from new as-of close-to-close calculations',
        '- The outcomes of these 14 cases were used to select them; do NOT treat in-sample patterns as verified predictive power',
        '', '## Files',
        '- CORE28_raw_5m_candles.csv: Bithumb observations; can be independently rechecked',
        '- CORE28_case_fib_ema.csv: as-of EMA/Fibonacci and separately marked posthoc lows',
        '- CORE28_recheck_5m_snapshots.csv: snapshots at t+0/5/10/15/20/30/45/60 (at each time, only completed bars)',
        '', '## Fetch issues',
    ] + (['- '+x for x in problems] if problems else ['- None'])
    (out/'README_CORE28_RESULTS.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return n_good, problems


def self_test():
    t = dt('2026-11-10 00:00')
    bars = []
    for i in range(110):
        close = 100 + 0.07*i + math.sin(i/4)*1.4
        bars.append(dict(market='KRW-TEST', t=t+FIVE*i,
                         o=close-0.1, c=close, h=close+0.3, l=close-0.3,
                         volume=100 + (i%9)*10, turnover_krw=1_000_000))
    at = t+FIVE*73
    first = features_at(bars,at)
    assert first['feature_status']=='READY'
    mutated = [dict(x) for x in bars]
    for b in mutated:
        if b['t']+FIVE>at:
            b['h'] *= 2; b['l'] *= 0.5; b['c'] *= 1.9
    later = features_at(mutated,at)
    assert later == first, 'look-ahead detected in as-of features'
    missing = [b for b in bars if b['t']!=t+FIVE*60]
    assert features_at(missing, at)['feature_status']=='INSUFFICIENT_OR_MISSING_5M'
    assert abs((120 - (120-100)*0.618)-107.64)<1e-9
    assert classify_nearest(.6181)[0]==61.8
    assert len(load_cases())==14
    assert posthoc(bars, at, first)['post_status']=='READY'
    print('PASS: no future leakage, missing bars, Fibonacci formula, 14 cases, 60m posthoc')


if __name__ == '__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--cases',type=Path,default=None,help='Optional Korean-header reverse-case CSV')
    ap.add_argument('--out',type=Path,default=Path('core28_results'))
    ap.add_argument('--self-test',action='store_true')
    ap.add_argument('--pause',type=float,default=0.22)
    ns=ap.parse_args()
    if ns.self_test:
        self_test()
    else:
        clean, issues=run(load_cases(ns.cases), ns.out, ns.pause)
        print(f'DONE: {clean} usable cases; {len(issues)} API issues; results at {ns.out}',flush=True)
