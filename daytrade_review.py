"""V11.13 experiment: 5-minute activity -> 1-hour flow + 4-hour structure.

Standalone, read-only scanner. Only /backtest-daytrade and /api/flow513/*
are registered; app.py and the V2 scanner are untouched.
- Scan all Bithumb KRW markets' 80 most recent completed 5-minute candles.
- Recent activity/pattern screening uses the last 3 consecutive candles and a
  real-time 5-minute close, NOT 21 consecutive candles. Older missing intervals
  remain visible in diagnostics; absent candles are NEVER synthesized.
- Verify only the best 80 screen-positive markets, keeping the cap and unverified
  count explicit. Use completed 1-hour and 4-hour bars as of the signal close.
- No buy when 1-hour is high risk, 4-hour data unavailable, overhead resistance
  unverified, or volume/pattern/risk/reward criteria fail.
- No order placement, no trade execution, no demonstrated win rate.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from math import isfinite
from statistics import median
from threading import Lock
import re
import time

from flask import jsonify, render_template_string, request

PAGE_SIZE = 40
MARKET_RE = re.compile(r"^KRW-[A-Z0-9]+$")
NEAR_4H_PCT = 2.0          # Horizontal 4H zone proximity.
NEAR_DIAG_ONLY_PCT = 1.0   # Tighter for diagonal-only markets to protect speed.
CONFLUENCE_PCT = 0.75        # Same-side horizontal and diagonal levels nearby.
H1_MIN_BARS = 30
H1_MAX_DELAY_MINUTES = 150.0
ZONE_CLUSTER_PCT = 0.8     # Two confirmed 4H pivots within 0.8% form a zone.
ZONE_MIN_PIVOT_GAP = 3     # Reactions must occur at different times (>=12h).
ZONE_MIN_HALF_PCT = 0.3    # Make a small price band, not a mathematical line.
ZONE_MAX_HALF_PCT = 0.65
WATCH_SUPPORT_PCT = 1.2
WATCH_RESISTANCE_PCT = 1.2
SUPPORT_RECLAIM_PCT = 0.8
MAX_BUY_SUPPORT_PCT = 1.5
MAX_BUY_BREAKOUT_PCT = 1.8
FRESH_5M_MINUTES = 10.0
FRESH_4H_MINUTES = 480.0
MIN_4H_BARS = 35
MIN_5M_BARS = 35
# Experimental quality gates (change only this experiment, never V2 main).
MIN_BUY_VOL_RATIO = 1.5            # Last 5M candle or breakout bar.
MIN_CONFIRM_VOL_RATIO = 0.8        # Confirmation candle must not go silent.
MIN_BULLISH_BODY_SHARE = 0.40      # Body / complete 5M high-low range.
MAX_UPPER_WICK_SHARE = 0.40
MIN_GROSS_ROOM_PCT = 3.0           # Gross 3% room to the next known 4H resistance.
MIN_GROSS_RR = 2.0                # At least 2:1 measured gross room / stop risk.
MIN_STOP_RISK_PCT = 0.25
MAX_STOP_RISK_PCT = 1.25
STOP_PAD_PCT = 0.25                # Buffer under 4H zone boundary.


def local_time(s, kst):
    t = datetime.fromisoformat(str(s))
    return t.replace(tzinfo=kst) if t.tzinfo is None else t.astimezone(kst)


def completed_candles(api_get, market, unit, count, now, kst):
    raw = api_get(f"/v1/candles/minutes/{unit}", {"market": market, "count": count})
    if not isinstance(raw, list):
        raise ValueError("캔들 응답 형식 오류")
    found = {}
    step = timedelta(minutes=unit)
    for c in raw:
        if not isinstance(c, dict):
            continue
        try:
            t = local_time(c["candle_date_time_kst"], kst)
            prices = [float(c[key]) for key in ("opening_price", "high_price", "low_price", "trade_price")]
            volume = float(c["candle_acc_trade_volume"])
            if not all(isfinite(v) and v > 0 for v in prices):
                continue
            if not isfinite(volume) or volume < 0:
                continue
            if prices[2] > min(prices[0], prices[3]) or prices[1] < max(prices[0], prices[3]):
                continue
            if t + step <= now:
                found[t] = c
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return sorted(found.items())


def pivots(ordered, field, kind):
    values = [float(r[field]) for _, r in ordered]
    if kind == "support":
        return [i for i in range(2, len(values) - 2)
                if values[i] <= min(values[i-2:i]) and values[i] < min(values[i+1:i+3])]
    return [i for i in range(2, len(values) - 2)
            if values[i] >= max(values[i-2:i]) and values[i] > max(values[i+1:i+3])]


def zone_candidates(ordered, field, kind, price):
    """Find a horizontal reaction *area* confirmed by >=2 separate 4H pivots.

    Pivot confirmation looks only at completed 4H candles. Candidate groups
    are ranked by distance to today's ticker price, then reactions/recency.
    This is a deterministic research heuristic, NOT an objective price truth.
    """
    ids = pivots(ordered, field, kind)
    if len(ids) < 2:
        return []
    values = [(i, float(ordered[i][1][field])) for i in ids]
    candidates = []
    for seed_i, seed_price in values:
        nearby = [(i, v) for i, v in values
                  if abs(v / seed_price - 1) * 100 <= ZONE_CLUSTER_PCT]
        if len(nearby) < 2:
            continue
        # Ignore multiple closely clustered pivots of the same local movement.
        selected = []
        for i, v in nearby:
            if not selected or i - selected[-1][0] >= ZONE_MIN_PIVOT_GAP:
                selected.append((i, v))
        if len(selected) < 2:
            continue
        zone_mid = float(median(v for _, v in selected))
        if zone_mid <= 0:
            continue
        lo_p, hi_p = min(v for _, v in selected), max(v for _, v in selected)
        half_pct = max(ZONE_MIN_HALF_PCT, (hi_p-lo_p) / (2*zone_mid)*100 + 0.1)
        half_pct = min(ZONE_MAX_HALF_PCT, half_pct)
        low = zone_mid * (1 - half_pct / 100)
        high = zone_mid * (1 + half_pct / 100)
        chosen = selected[-2:]
        candidates.append({
            "kind": kind, "mid": round(zone_mid, 10),
            "low": round(low, 10), "high": round(high, 10),
            "distance_pct": round((price/zone_mid-1)*100, 3),
            "touches": len(selected), "last_pivot_index": selected[-1][0],
            "time1": ordered[chosen[0][0]][0].strftime("%m-%d %H:%M"),
            "time2": ordered[chosen[1][0]][0].strftime("%m-%d %H:%M"),
            "p1": round(chosen[0][1], 10), "p2": round(chosen[1][1], 10),
        })
    if not candidates:
        return []
    candidates.sort(key=lambda z: (abs(z["distance_pct"]), -z["touches"], -z["last_pivot_index"]))
    distinct = []
    for item in candidates:
        # Several seed pivots can describe the same band. Keep one record.
        if any(abs(item["mid"] / x["mid"] - 1) < 0.002 for x in distinct):
            continue
        item = dict(item)
        del item["last_pivot_index"]
        distinct.append(item)
    return distinct


def make_zone(ordered, field, kind, price):
    zones = zone_candidates(ordered, field, kind, price)
    return zones[0] if zones else None


def trendline_4h(ordered, kind):
    """Most recent two confirmed 4H pivots; project by real elapsed time.

    A rising support uses swing LOWs; declining resistance uses swing HIGHs.
    2 candles on either side confirm a pivot; the current 4H bar is excluded.
    """
    field = "low_price" if kind == "support" else "high_price"
    ids = pivots(ordered, field, kind)
    if len(ids) < 2:
        return None
    i, j = ids[-2:]
    t1, t2 = ordered[i][0].timestamp(), ordered[j][0].timestamp()
    p1, p2 = float(ordered[i][1][field]), float(ordered[j][1][field])
    if t2 <= t1 or p1 <= 0 or p2 <= 0:
        return None
    if kind == "support" and p2 <= p1:
        return None  # falling lows are not upward support
    if kind == "resistance" and p2 >= p1:
        return None  # rising highs are not downward resistance
    return {"kind": kind, "t1": t1, "t2": t2, "p1": p1, "p2": p2,
            "direction": "상승" if p2 > p1 else "하락" if p2 < p1 else "수평",
            "time1": ordered[i][0].strftime("%m-%d %H:%M"),
            "time2": ordered[j][0].strftime("%m-%d %H:%M")}


def trendline_value(line, at):
    if not isinstance(line, dict):
        return None
    t1, t2 = float(line["t1"]), float(line["t2"])
    p1, p2 = float(line["p1"]), float(line["p2"])
    if t2 <= t1 or p1 <= 0 or p2 <= 0:
        return None
    value = p2 + (p2-p1) * ((at.timestamp()-t2)/(t2-t1))
    return value if isfinite(value) and value > 0 else None


def trendline_as_zone(line, at):
    """Adapt a projected diagonal level to V11.9's display/quality fields."""
    v = trendline_value(line, at)
    if v is None:
        return None
    return {"kind": line["kind"], "low": v, "high": v, "mid": v,
            "touches": 2, "p1": line["p1"], "p2": line["p2"],
            "time1": line["time1"], "time2": line["time2"]}


def one_hour_flow(api_get, market, now, kst):
    """Trend context only. Neutral/early reversal does NOT veto a 5M entry."""
    ordered = completed_candles(api_get, market, 60, 80, now, kst)
    result = {"state": "확인 불가", "risk": True, "reason": "1시간봉 데이터 부족",
              "last": None, "ma5": None, "ma20": None, "change_3h_pct": None}
    if len(ordered) < H1_MIN_BARS:
        return result
    closed = [float(x[1]["trade_price"]) for x in ordered]
    age = (now-(ordered[-1][0]+timedelta(hours=1))).total_seconds()/60
    result["last"] = ordered[-1][0].strftime("%m-%d %H:%M")
    if age > H1_MAX_DELAY_MINUTES or age < -0.5:
        result["reason"] = "1시간봉 최근 데이터 지연"
        return result
    if any(b-a != timedelta(hours=1) for a,b in
           zip([x[0] for x in ordered[-5:-1]], [x[0] for x in ordered[-4:]])):
        result["reason"] = "최근 1시간봉 시간 공백"
        return result
    ma5 = sum(closed[-5:])/5
    ma20 = sum(closed[-20:])/20
    last3 = (closed[-1] / closed[-4] - 1)*100
    deviation = (closed[-1] / ma20 - 1)*100
    result.update(ma5=round(ma5,10), ma20=round(ma20,10),
                  change_3h_pct=round(last3,2), age_minutes=round(age,1),
                  risk=False, reason="")
    if deviation < -1.5 and ma5 < ma20 and last3 < -0.8:
        result.update(state="강한 하락", risk=True, reason="1시간봉 강한 하락")
    elif deviation >= 0.3 and ma5 >= ma20 and last3 > 0:
        result["state"] = "상승 흐름"
    elif last3 >= 0.2 and closed[-1] >= ma5:
        result["state"] = "상승 전환 시도"
    else:
        result["state"] = "중립·약세 관찰"
    return result


def analyze_4h(api_get, market, price, now, kst):
    ordered = completed_candles(api_get, market, 240, 80, now, kst)
    out = {"market": market, "price": price, "bars4h": len(ordered),
           "near": False, "support": None, "resistance": None,
           "diag_support": None, "diag_resistance": None,
           "confluence_kinds": [], "reason": "", "last4h": None}
    if len(ordered) < MIN_4H_BARS:
        out["reason"] = "4시간봉 데이터 부족"
        return out
    out["last4h"] = ordered[-1][0].strftime("%m-%d %H:%M")
    out["age4h"] = round((now-(ordered[-1][0]+timedelta(hours=4))).total_seconds()/60, 1)
    if out["age4h"] > FRESH_4H_MINUTES:
        out["reason"] = "4시간봉 최근 데이터 지연"
        return out
    out["support"] = make_zone(ordered, "low_price", "support", price)
    all_resistances = zone_candidates(ordered, "high_price", "resistance", price)
    out["resistance"] = all_resistances[0] if all_resistances else None
    # Additional confirmed 4H resistance areas above market are used to
    # estimate headroom. All are derived from the SAME 80 fetched candles.
    # The nearest selected resistance is not automatically the next overhead.
    out["overhead"] = sorted(
        ({"low": z["low"], "high": z["high"], "touches": z["touches"]}
         for z in all_resistances if z["low"] > price * 0.99),
        key=lambda z: z["low"]
    )[:10]
    # Reuse the SAME completed 4H candles for diagonal trendlines.
    for kind in ("support", "resistance"):
        line = trendline_4h(ordered, kind)
        projected = trendline_value(line, now) if line else None
        if projected:
            line["now"] = round(projected, 10)
            line["distance_pct"] = round((price / projected - 1)*100, 3)
            out["diag_"+kind] = line
            horizontal = out.get(kind)
            if horizontal and abs((horizontal["mid"] / projected - 1)*100) <= CONFLUENCE_PCT:
                out["confluence_kinds"].append(kind)
    all_levels = [out.get(z) for z in
                  ("support", "resistance", "diag_support", "diag_resistance")]
    active = [x for x in all_levels if x]
    if not active:
        out["reason"] = "유효한 수평 가격대 또는 추세선 없음"
    else:
        out["near"] = (any(abs(x["distance_pct"]) <= NEAR_4H_PCT
                           for x in (out["support"],out["resistance"]) if x) or
                       any(abs(x["distance_pct"]) <= NEAR_DIAG_ONLY_PCT
                           for x in (out["diag_support"],out["diag_resistance"]) if x))
        out["reason"] = "5분봉 확인 대상" if out["near"] else "4시간 가격대·추세선과 거리 멂"
    return out


def evaluate_entry_quality(entry, stop, overhead, strong_candle,
                           volume_confirmed, retest_confirmed):
    """Conservative research gates for a long entry; amounts are GROSS.

    Missing overhead level does not mean unlimited upside. Without an observed
    4H resistance above price, keep the setup on WATCH for verification.
    """
    reasons = []
    reason_codes = []  # Stable, distinct categories for V11.11 diagnostics.
    if not strong_candle:
        reasons.append("5분봉 양봉 몸통/윗꼬리 확인 부족")
        reason_codes.append("candle")
    if not volume_confirmed:
        reasons.append("매수 거래량 확인 부족")
        reason_codes.append("volume")
    if not retest_confirmed:
        reasons.append("돌파 후 지지 재확인 부족")
        reason_codes.append("retest")
    if not (isfinite(entry) and isfinite(stop) and 0 < stop < entry):
        return {"passed": False, "reasons": reasons+["유효한 손절 기준 없음"],
                "reason_codes": reason_codes+["stop"],
                "stop": None, "risk_pct": None, "room_pct": None,
                "rr": None, "next_resistance": None}
    risk_pct = (entry - stop) / entry * 100
    if not MIN_STOP_RISK_PCT <= risk_pct <= MAX_STOP_RISK_PCT:
        reasons.append(f"손절 거리 {risk_pct:.2f}% (허용 {MIN_STOP_RISK_PCT}~{MAX_STOP_RISK_PCT}%)")
        reason_codes.append("stop")
    levels = []
    for z in overhead or []:
        try:
            upper_floor = float(z["low"])
            if isfinite(upper_floor) and upper_floor > entry * 1.0001:
                levels.append(upper_floor)
        except (TypeError, ValueError, KeyError, OverflowError):
            continue
    next_resistance = min(levels) if levels else None
    room_pct = (next_resistance / entry - 1)*100 if next_resistance else None
    rr = room_pct / risk_pct if room_pct is not None and risk_pct > 0 else None
    if next_resistance is None:
        reasons.append("위쪽 4시간봉 저항 확인 불가")
        reason_codes.append("overhead_unknown")
    else:
        if room_pct < MIN_GROSS_ROOM_PCT:
            reasons.append(f"위쪽 저항 여유 {room_pct:.2f}% 부족")
            reason_codes.append("room")
        if rr < MIN_GROSS_RR:
            reasons.append(f"참고 손익비 {rr:.2f} 미달")
            reason_codes.append("rr")
    return {"passed": not reasons, "reasons": reasons,
            "reason_codes": reason_codes,
            "stop": round(stop, 10), "risk_pct": round(risk_pct, 2),
            "room_pct": round(room_pct, 2) if room_pct is not None else None,
            "rr": round(rr, 2) if rr is not None else None,
            "next_resistance": round(next_resistance, 10) if next_resistance else None}



# The 5M-first flow uses no minimum KRW turnover and never auto excludes warnings.
SHORTLIST_LIMIT = 80
MIN_SCREEN_5M_BARS = 25
MIN_ACTIVE_CANDLES_30M = 5
MAX_SIGNAL_AGE_MIN = 10.0
MAX_FINAL_AGE_MIN = 10.0
MOTION_LOOKBACK_BARS = 18


def screen_5m(api_get, market, now, kst):
    """Price movement first. No synthetic candles, no future candles.

    The older 21-candle continuity constraint is deliberately NOT a BUY gate.
    Freshness, last 3 consecutive bars, and >=5 traded 5M intervals in the
    past 30min are mandatory for *screen-positive* candidates. Repeated bars
    with no actual trades cannot pass.
    """
    rows = completed_candles(api_get, market, 5, 80, now, kst)
    info = {"market": market, "screen": "NONE", "pattern": "",
            "price": None, "volume_ratio": None, "score": 0,
            "age_minutes": None, "last3_continuous": False,
            "gap21": False, "missing21": 0, "active30": 0,
            "bars5m": len(rows), "signal_time": None,
            "signal_end": None, "stage": "NONE", "reason": ""}
    if len(rows) < MIN_SCREEN_5M_BARS:
        info.update(screen="DATA", reason="5분봉 최소 데이터 부족")
        return info
    times = [t for t, _ in rows]
    p = [float(r["trade_price"]) for _, r in rows]
    o = [float(r["opening_price"]) for _, r in rows]
    hi = [float(r["high_price"]) for _, r in rows]
    lo = [float(r["low_price"]) for _, r in rows]
    volumes = [float(r["candle_acc_trade_volume"]) for _, r in rows]
    last = times[-1]
    end = last + timedelta(minutes=5)
    age = (now - end).total_seconds() / 60
    recent3 = all(times[i] - times[i-1] == timedelta(minutes=5) for i in (-2, -1))
    active30 = sum(end - timedelta(minutes=30) <= t <= last and v>0 for t,v in zip(times,volumes))
    gaps21 = [(times[i] - times[i-1]).total_seconds()/300 - 1
              for i in range(-20, 0) if times[i] - times[i-1] != timedelta(minutes=5)]
    baseline = sum(volumes[-21:-1]) / 20
    vr = volumes[-1] / baseline if baseline > 0 else 0.0
    bar_range = hi[-1] - lo[-1]
    bullish = p[-1] > o[-1]
    body_share = (p[-1]-o[-1])/bar_range if bar_range > 0 else 0.0
    wick_share = (hi[-1]-p[-1])/bar_range if bar_range > 0 else 1.0
    strong = bullish and body_share >= MIN_BULLISH_BODY_SHARE and wick_share <= MAX_UPPER_WICK_SHARE
    info.update(price=p[-1], volume_ratio=round(vr, 3), signal_time=last.strftime('%Y-%m-%d %H:%M'),
                signal_end=end.strftime('%Y-%m-%d %H:%M'), age_minutes=round(age, 2),
                last3_continuous=recent3, gap21=bool(gaps21), missing21=sum(max(0,round(n)) for n in gaps21),
                active30=active30, strong_candle=strong, body_share=round(body_share, 3),
                wick_share=round(wick_share, 3), stage="NONE")
    if age < -0.5 or age > MAX_SIGNAL_AGE_MIN:
        info.update(screen="STALE", reason="최종 확정 5분봉 지연")
        return info
    if not recent3 or any(v <= 0 for v in volumes[-3:]):
        info.update(screen="GAP3", reason="가장 최근 3개 5분봉의 거래·시간 불연속")
        return info
    if active30 < MIN_ACTIVE_CANDLES_30M:
        info.update(screen="QUIET", reason="최근 30분 내 실제 5분봉 부족")
        return info

    # Price levels below are formed using ONLY past completed 5M candles.
    last_high = max(hi[-MOTION_LOOKBACK_BARS:-3])
    last_low = min(lo[-MOTION_LOOKBACK_BARS:-3])
    if last_high <= 0 or last_low <= 0:
        return info
    bounced = (min(lo[-3:]) <= last_low * 1.006 and
               p[-1] > p[-2] and bullish and p[-1] >= last_low * 1.002)
    broke = (p[-1] >= last_high * 1.002 and
             (p[-2] <= last_high * 1.003 or p[-3] <= last_high * 1.003))
    recheck = (p[-1] >= last_high * 1.001 and
               p[-2] >= last_high * 1.001 and
               min(lo[-2:]) <= last_high * 1.005)
    touch_support = min(lo[-3:]) <= last_low * 1.009
    near_breakout = p[-1] >= last_high * 0.992 and p[-1] <= last_high * 1.022
    # Scan strong initial 5M movement even before an exact local-level test.
    momentum = (p[-1] / p[-4] - 1) * 100
    active_momentum = bullish and momentum >= 0.35 and vr >= 1.1
    stop_rebound = min(lo[-3:]) * (1 - STOP_PAD_PCT/100)
    stop_breakout = min(min(lo[-2:]), last_high) * (1 - STOP_PAD_PCT/100)
    if bounced:
        pattern, stage, stop = "5분봉 지지 반등", "CONFIRMED", stop_rebound
        retested = True
    elif broke and recheck and bullish:
        pattern, stage, stop = "5분봉 저항 돌파·재확인", "CONFIRMED", stop_breakout
        retested = True
    elif broke or (near_breakout and bullish and p[-1] >= p[-2]*1.001 and vr >= 1.0):
        pattern, stage, stop = "5분봉 저항 접근·돌파 시도", "READY", stop_breakout
        retested = bool(recheck)
    elif touch_support and bullish and p[-1] > p[-2]:
        pattern, stage, stop = "5분봉 지지 터치·반등 대기", "READY", stop_rebound
        retested = False
    elif active_momentum:
        pattern, stage, stop = "5분봉 상승 움직임 관찰", "READY", stop_rebound
        retested = False
    else:
        info.update(screen="NONE", reason="최근 5분봉 진입 준비 모양 없음")
        return info
    score = (100 if stage == "CONFIRMED" else 55)
    score += min(20, vr*5) + min(10,max(0,momentum)*2)
    score += (4 if not info['gap21'] else 0)
    info.update(screen="CANDIDATE", stage=stage, pattern=pattern,
                stop=round(stop, 10), retested=retested,
                momentum_pct=round(momentum, 3), score=round(score, 2),
                reason="5분봉 후보 · 1H/4H 미검증")
    return info


def confirm_candidate(api_get, item, kst):
    """Verify 1H and 4H as of 5M signal close; no look-ahead from 4H."""
    market = item['market']
    signal_end = local_time(item['signal_end'], kst)
    # Executed serially to avoid overloading public API; parallel across coins.
    oneh = one_hour_flow(api_get, market, signal_end, kst)
    htf = analyze_4h(api_get, market, item['price'], signal_end, kst)
    safety_reasons = []
    if oneh.get('risk'):
        safety_reasons.append('1시간봉 위험·자료 부족: '+oneh.get('reason',''))
    if htf.get('bars4h',0) < MIN_4H_BARS or not htf.get('last4h') or htf.get('age4h',9999)>FRESH_4H_MINUTES:
        safety_reasons.append('4시간봉 유효 데이터 부족')
    if not any(htf.get(k) for k in ('support','resistance','diag_support','diag_resistance')):
        safety_reasons.append('4시간봉 지지·저항 구조 확인 불가')
    price = float(item['price'])
    overhead = list(htf.get('overhead') or [])
    line = htf.get('diag_resistance')
    lvl = trendline_value(line, signal_end) if line else None
    if lvl is not None and lvl > price*1.0001:
        overhead.append({'low':lvl,'high':lvl,'touches':2})
    quality = evaluate_entry_quality(price, float(item['stop']), overhead,
                bool(item.get('strong_candle')),
                float(item.get('volume_ratio') or 0) >= MIN_BUY_VOL_RATIO,
                bool(item.get('retested')))
    reasons = list(quality['reasons'])+safety_reasons
    if item['stage']!='CONFIRMED':
        reasons.insert(0,'5분봉 반등·돌파 미확정')
    if not item['last3_continuous'] or item['active30']<MIN_ACTIVE_CANDLES_30M:
        reasons.insert(0,'가장 최근 거래 연속성 부족')
    status = 'BUY' if not reasons else 'WATCH'
    if status == 'BUY':
        stage = 'BUY'
    elif item['stage'] == 'CONFIRMED':
        stage = 'QUALITY_HOLD'
    else:
        stage = 'READY'
    bonus = 6 if htf.get('confluence_kinds') else 0
    oneh_score = {'상승 흐름':10,'상승 전환 시도':7,'중립·약세 관찰':0}.get(oneh.get('state'),-15)
    result = dict(item)
    result.update(status=status,stage=stage,hour1=oneh,htf=htf,quality=quality,
                  reasons=reasons,score=round(item['score']+bonus+oneh_score,2),
                  confluence=bool(htf.get('confluence_kinds')))
    return result


PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.13 · 5분봉 우선 단타 후보 스캐너</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#171d34;--card:#101728;--border:#33405e;--muted:#acb9d3;--fg:#f0f4ff;--blue:#315de7;--green:#4ade80;--orange:#f9c05a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1000px;margin:auto;padding:20px 14px 55px}h1{font-size:23px;line-height:1.35;margin:0 0 9px}h2{font-size:18px;margin:0 0 10px}h3{font-size:16px;margin:0 0 7px;overflow-wrap:anywhere}
p{margin:7px 0}.muted,small{color:var(--muted);font-size:13px}.panel{background:var(--panel);border:1px solid var(--border);padding:16px;border-radius:16px;margin-top:15px}
button{background:var(--blue);border:0;border-radius:10px;color:white;padding:12px 18px;font-weight:800;font-size:15px;cursor:pointer}button:disabled{opacity:.45}a{color:#b3c7ff}
.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.metric{border:1px solid var(--border);padding:10px;border-radius:10px}.metric b{display:block;font-size:21px}.metric small{display:block;font-size:11px}
.list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.item{padding:13px;border:1px solid var(--border);background:var(--card);border-radius:12px;overflow-wrap:anywhere}.item.buy{border-color:#328f67}.item.watch{border-color:#866f38}
.cardtext{font-size:13px;white-space:pre-line}.buylabel{color:var(--green)}.watchlabel{color:var(--orange)}.status{white-space:pre-wrap;font-size:13px;color:var(--muted)}.sub{color:var(--muted);font-size:12px}
progress{width:100%;height:13px;margin:14px 0 6px}.line{display:flex;align-items:center;gap:8px;flex-wrap:wrap}details summary{cursor:pointer;font-weight:700}.bar{margin-top:9px;display:flex;flex-wrap:wrap;gap:9px;align-items:center}.bar button{padding:8px 12px;background:#3d4d70;font-size:13px}
@media(max-width:680px){.list{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><main>
<h1>🎯 5분봉 움직임 → 1시간 흐름 → 4시간 구조</h1>
<p class="muted">V11.13 실험판 · 빗썸 KRW 전체 · 거래대금 하한 없음 · 유의종목 포함 · V2 메인 변경 없음</p>
<section class="panel"><button id="run">지금 전체 검색</button>　<a href="/">기존 V2 메인</a>　<a href="/backtest-winrate">📊 단타 승률 검증실</a>
<p class="muted">최근 거래가 있는 5분봉부터 후보를 찾습니다. 가장 최근 3개 5분봉 연속과 지난 30분 실제 봉 5개 이상을 확인하며, 더 오래된 봉 공백은 경고로만 표시합니다. 5분봉 후보 중 점수 상위 최대 80개만 1시간봉·4시간봉 추가 검증하므로 <b>검증하지 못한 후보 개수</b>를 반드시 함께 표시합니다. BUY에는 기존 거래량·손절 위험·위쪽 저항 3%·참고 손익비 조건을 적용합니다. 3~5% 상승을 보장하지 않습니다.</p>
<div class="metrics">
<div class="metric"><small>전체 / 5분봉 검사</small><b id="all">—</b></div>
<div class="metric"><small>최근 거래·5분봉 유효</small><b id="active">—</b></div>
<div class="metric"><small>1H·4H 확인</small><b id="verify">—</b></div>
<div class="metric"><small>확정 BUY</small><b id="buyCount" class="buylabel">—</b></div>
<div class="metric"><small>WATCH</small><b id="watchCount" class="watchlabel">—</b></div>
<div class="metric"><small>추가 검증 제외</small><b id="notChecked">—</b></div>
</div>
<progress id="progress" value="0" max="100"></progress><div id="status" class="status">검색 버튼을 눌러 시작하세요.</div><div id="timing" class="status"></div>
</section>
<section class="panel"><h2>⭐ 집중 관찰 최대 3개</h2><p class="muted">BUY → 5분봉 진입 모양 확인·품질 보류 → 터치·돌파 준비 순서. BUY가 아닌 후보를 매수 신호처럼 표시하지 않습니다.</p><div class="list" id="focus">완료 후 표시</div></section>
<section class="panel"><h2>🧪 진입 단계·검증 결과</h2><div class="metrics" id="stages">완료 후 표시</div></section>
<section class="panel"><h2>📉 5분봉 상태 진단</h2><div class="metrics" id="health">완료 후 표시</div><p class="muted">오래된 5분봉 공백은 합성하지 않습니다. 가장 최근 3개 봉이 연결되고 최근 30분 실제 거래가 확인되면, 그보다 오래된 21개 구간의 공백은 BUY를 무조건 차단하지 않습니다.</p></section>
<section class="panel"><h2>📝 오늘 수동 거래 기록</h2><div class="bar"><strong id="tradeN">0 / 3회</strong><button id="record">거래 1회 직접 기록</button><button id="undo">마지막 기록 취소</button></div><p class="muted">브라우저에만 저장됩니다. 주문을 실행하거나 거래소 주문 횟수를 차단하지 않습니다.</p></section>
<section class="panel"><h2 class="buylabel">BUY · 1H·4H 검증 통과</h2><div class="list" id="buys">검색 전</div></section>
<section class="panel"><h2 class="watchlabel">WATCH · 진입 준비·보류</h2><div class="list" id="watches">검색 전</div></section>
<details class="panel"><summary>⚠️ 5분봉 단계에서 제외·보류된 종목 현황</summary><div id="diagnosis">검사 전</div></details>
<p class="muted">실험용 기술적 신호이며 투자 권유나 자동 주문 기능이 아닙니다. 확인된 4시간봉 저항이 없다고 무제한 상승 가능하다고 가정하지 않습니다. 수수료·슬리피지를 제외한 실제 손익과 다릅니다.</p>
</main><script>
const $=x=>document.getElementById(x),esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const won=x=>x==null?'—':Number(x).toLocaleString('ko-KR',{maximumFractionDigits:8})+'원';
const LIMIT=3;let running=false,complete=false;let rows=[],stats={};
const day=()=>{const p=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());const v=k=>p.find(x=>x.type===k)?.value||'';return `${v('year')}-${v('month')}-${v('day')}`;};
const key=()=>`bithumb_v119_manual_${day()}`; // Retain V11.9-V11.12 daily logs
function logs(){try{const r=JSON.parse(localStorage.getItem(key())||'[]');return Array.isArray(r)?r:[]}catch{return []}}
function paintLogs(){const a=logs();$('tradeN').textContent=`${a.length} / 3회`;$('record').disabled=a.length>=LIMIT;$('undo').disabled=!a.length;}
$('record').onclick=()=>{const a=logs();if(a.length<LIMIT){a.push({market:'수동 거래',price:null,when:new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',hour:'2-digit',minute:'2-digit'}).format(new Date())});try{localStorage.setItem(key(),JSON.stringify(a));paintLogs();}catch(e){alert('수동 거래 기록 저장 실패: 브라우저 저장 공간을 확인하세요.');}}};
$('undo').onclick=()=>{const a=logs();if(a.length){a.pop();try{localStorage.setItem(key(),JSON.stringify(a));paintLogs();}catch(e){alert('기록 취소 저장 실패');}}};
const metric=(n,v)=>`<div class="metric"><small>${esc(n)}</small><b>${esc(v)}</b></div>`;
function textCard(r){const q=r.quality||{},h=r.htf||{},one=r.hour1||{};
const why=(r.reasons||[]).join(' / '),oldGap=r.gap21?' · ⚠️ 이전 구간 봉 공백':'',known=q.room_pct==null?'미확인':q.room_pct+'%';
return `<article class="item ${r.status==='BUY'?'buy':'watch'}"><h3>${esc(r.market)} · ${r.status==='BUY'?'✅ BUY':'⚠️ WATCH'}</h3><div class="cardtext">${esc(r.pattern)} · ${esc(r.stage)}
확정 5분봉 ${esc(r.signal_time)} · 종가 ${won(r.price)} · 거래량 ${esc(r.volume_ratio)}배${esc(oldGap)}
1시간봉 ${esc(one.state||'확인 전')} · 4시간봉 ${h.near?'가격대 근처':'구조 확인'}${r.confluence?' · 복합선 겹침':''}
진입 참고 손절 ${won(q.stop)} · 위험 ${q.risk_pct==null?'미확인':q.risk_pct+'%'}
다음 확인된 4시간 저항 ${won(q.next_resistance)} · 위쪽 공간 ${esc(known)} · 참고 손익비 ${q.rr==null?'—':q.rr+':1'}
${r.status==='BUY'?'✅ 모든 실험 기준 통과':'매수 보류: '+esc(why)}</div></article>`}
function paint(){const b=rows.filter(x=>x.status==='BUY').sort((a,b)=>b.score-a.score),w=rows.filter(x=>x.status==='WATCH').sort((a,b)=>b.score-a.score);$('buyCount').textContent=b.length;$('watchCount').textContent=w.length;
$('buys').innerHTML=b.length?b.map(textCard).join(''):'<p class="muted">확정 BUY 없음</p>';
$('watches').innerHTML=w.length?w.slice(0,18).map(textCard).join('')+(w.length>18?`<p class="muted">나머지 WATCH ${w.length-18}개는 상위 후보만 간결히 표시합니다.</p>`:''):'<p class="muted">WATCH 없음</p>';
let focus=rows.slice().sort((a,b)=>(a.status==='BUY'?0:a.stage==='QUALITY_HOLD'?1:2)-(b.status==='BUY'?0:b.stage==='QUALITY_HOLD'?1:2)||b.score-a.score).slice(0,3);
$('focus').innerHTML=focus.length?focus.map(textCard).join(''):'<p class="muted">현재 집중 후보가 없습니다. 억지로 거래할 필요는 없습니다.</p>';
$('stages').innerHTML=[['확정 BUY',b.length],['진입 모양·품질 보류',w.filter(x=>x.stage==='QUALITY_HOLD').length],['진입 준비',w.filter(x=>x.stage==='READY').length],['1H 위험 보류',w.filter(x=>x.hour1?.risk).length]].map(([a,b])=>metric(a,b+'개')).join('');}
async function post(url,items){const ctl=new AbortController(),tm=setTimeout(()=>ctl.abort(),75000);try{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items}),cache:'no-store',signal:ctl.signal});const v=await r.json();if(!r.ok||!v.ok)throw Error(v.error||`HTTP ${r.status}`);return v}finally{clearTimeout(tm)}}
async function pool(packs,workers,callback){let i=0;const err=[];async function work(){while(i<packs.length){const p=packs[i++];try{await callback(p)}catch(e){err.push(String(e))}}}await Promise.all(Array.from({length:Math.min(workers,packs.length)},work));return err}
function batches(items){const a=[];for(let i=0;i<items.length;i+=40)a.push(items.slice(i,i+40));return a}
async function scan(){if(running)return;running=true;complete=false;rows=[];$('run').disabled=true;let errors=0;
$('buyCount').textContent='—';$('watchCount').textContent='—';$('stages').textContent='분석 중';$('health').textContent='분석 중';$('focus').textContent='분석 중';$('buys').textContent='분석 중';$('watches').textContent='분석 중';
const begun=performance.now();try{const uRes=await fetch('/api/flow513/universe',{cache:'no-store'}),u=await uRes.json();if(!uRes.ok||!u.ok)throw Error(u.error||'코인 목록 불러오기 실패');
const markets=u.items,summary={STALE:0,GAP3:0,QUIET:0,DATA:0,NONE:0,CANDIDATE:0,older_gap:0,active:0},candidates=[];
let done=0;$('progress').max=markets.length;$('progress').value=0;
const err1=await pool(batches(markets),3,async pack=>{const r=await post('/api/flow513/screen',pack);errors+=r.errors;done+=r.scanned;for(const x of r.results){summary[x.screen]=(summary[x.screen]||0)+1;if(x.gap21)summary.older_gap++;if(x.screen==='CANDIDATE')candidates.push(x);if(x.screen!=='STALE'&&x.screen!=='DATA'&&x.screen!=='GAP3'&&x.screen!=='QUIET')summary.active++} $('all').textContent=`${done}/${markets.length}`;$('active').textContent=summary.active;$('progress').value=done;$('status').textContent=`1단계 · 5분봉 ${done}/${markets.length}개 · 진입 준비 후보 ${candidates.length}개`;});
if(err1.length||errors||done!==markets.length)throw Error(`5분봉 1차 스캔 실패 ${errors}건, 요청 실패 ${err1.length}건`);
const t1=(performance.now()-begun)/1000;candidates.sort((a,b)=>b.score-a.score||a.market.localeCompare(b.market));
const selected=candidates.slice(0,80),discarded=candidates.length-selected.length;$('notChecked').textContent=discarded;$('verify').textContent=`0/${selected.length}`;
$('health').innerHTML=[['최근 봉 지연',summary.STALE],['최근 3봉 공백',summary.GAP3],['최근 30분 거래 부족',summary.QUIET],['구간 내 오래된 공백',summary.older_gap],['5분봉 패턴 없음',summary.NONE],['진입 준비 후보',summary.CANDIDATE]].map(([k,v])=>metric(k,v+'개')).join('');
$('diagnosis').textContent=`5분봉 후보 ${candidates.length}개 중 ${selected.length}개만 1H/4H 추가 검증. 점수 상한 때문에 보류된 후보 ${discarded}개 (BUY 아님). 최근 봉이 오래되거나 마지막 3봉이 불연속인 종목은 진입 후보로 올리지 않음.`;
let verified=0;const err2=await pool(batches(selected),3,async pack=>{const r=await post('/api/flow513/verify',pack);errors+=r.errors;verified+=r.scanned;rows.push(...r.results.filter(x=>x.status==='WATCH'||x.status==='BUY'));$('verify').textContent=`${verified}/${selected.length}`;$('status').textContent=`2단계 · 1H/4H ${verified}/${selected.length}개 · BUY ${rows.filter(x=>x.status==='BUY').length}개`;});
complete=!err2.length&&!errors&&verified===selected.length;if(!complete)throw Error(`1H/4H 검증 누락: ${errors}건, 요청 실패 ${err2.length}건`);
paint();const elapsed=(performance.now()-begun)/1000;$('timing').textContent=`전체 ${elapsed.toFixed(1)}초 · 5분봉 1차 ${t1.toFixed(1)}초 · 후보 ${candidates.length}개 중 1H/4H 검증 ${verified}개 · 검증 상한 때문에 제외 ${discarded}개`;
$('status').textContent=`✅ 완료 · 전체 ${markets.length}개 · 5분봉 후보 ${candidates.length}개 · 검증 ${verified}개 · BUY ${rows.filter(x=>x.status==='BUY').length}개 · WATCH ${rows.filter(x=>x.status==='WATCH').length}개 · 오류 0건`;
}catch(e){$('status').textContent='⚠️ 완료되지 않음: '+e.message+' · 일부 결과로 BUY 판단하지 마세요.';$('focus').textContent='불완전한 스캔으로 집중 후보 표시 중단';$('buys').textContent='검색 미완료';$('watches').textContent='검색 미완료';}
finally{running=false;$('run').disabled=false}}
$('run').onclick=scan;paintLogs();
</script></body></html>"""


def register_daytrade_review(app, deps):
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    universe_cache = {'at': 0.0, 'data': None}
    universe_lock = Lock()

    @app.get('/backtest-daytrade')
    def page_513():
        return render_template_string(PAGE)

    @app.get('/api/flow513/universe')
    def universe_513():
        try:
            with universe_lock:
                if universe_cache['data'] is not None and time.time()-universe_cache['at']<30:
                    return jsonify(universe_cache['data'])
                markets = sorted({m for m in get_markets()
                                  if isinstance(m,str) and MARKET_RE.fullmatch(m)})
                if not markets:
                    raise ValueError('빗썸 KRW 코인 목록이 비어 있습니다')
                data={'ok':True,'items':markets,'total':len(markets),'start_time':datetime.now(kst).isoformat()}
                universe_cache.update(at=time.time(),data=data)
                return jsonify(data)
        except Exception as e:
            return jsonify({'ok':False,'error':str(e)[:140]}),502

    def valid_markets(payload):
        items=payload.get('items') if isinstance(payload,dict) else None
        if not isinstance(items,list) or not 1<=len(items)<=PAGE_SIZE:
            raise ValueError('한 번에 1~40개 종목만 요청할 수 있습니다')
        names=[x if isinstance(x,str) else x.get('market') if isinstance(x,dict) else None for x in items]
        if any(not isinstance(m,str) or not MARKET_RE.fullmatch(m) for m in names) or len(set(names))!=len(names):
            raise ValueError('코인 목록 형식 오류')
        return items,names

    @app.post('/api/flow513/screen')
    def screen_513():
        try:
            _,names=valid_markets(request.get_json(silent=True))
            now=datetime.now(kst)
            results=[];errors=0
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures={pool.submit(screen_5m,api_get,m,now,kst):m for m in names}
                for f in as_completed(futures):
                    try:results.append(f.result())
                    except Exception: errors+=1
            return jsonify({'ok':True,'scanned':len(names),'errors':errors,'results':results})
        except Exception as e:
            return jsonify({'ok':False,'error':str(e)[:140]}),400

    @app.post('/api/flow513/verify')
    def verify_513():
        try:
            items,names=valid_markets(request.get_json(silent=True))
            if any(not isinstance(x,dict) or x.get('market')!=m or
                   x.get('screen')!='CANDIDATE' or x.get('stage') not in ('READY','CONFIRMED') or
                   not isinstance(x.get('price'),(int,float)) or
                   not isinstance(x.get('stop'),(int,float)) or
                   not isinstance(x.get('score'),(int,float)) or
                   not isinstance(x.get('signal_end'),str) for x,m in zip(items,names)):
                raise ValueError('5분봉 후보 검증 입력 형식 오류')
            # First-stage output is always generated on server; reject stale/malformed inputs.
            now=datetime.now(kst)
            for x in items:
                signal_end=local_time(x['signal_end'],kst)
                if not 0 <= (now-signal_end).total_seconds()/60 <= 15:
                    raise ValueError('오래된 5분봉 후보 · 다시 검색하세요')
                if not all(isfinite(float(x[v])) and float(x[v])>0 for v in ('price','stop')):
                    raise ValueError('유효하지 않은 가격 데이터')
            results=[];errors=0
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures={pool.submit(confirm_candidate,api_get,x,kst):x['market'] for x in items}
                for f in as_completed(futures):
                    try:results.append(f.result())
                    except Exception:errors+=1
            return jsonify({'ok':True,'scanned':len(items),'errors':errors,'results':results})
        except Exception as e:
            return jsonify({'ok':False,'error':str(e)[:140]}),400

# ---------------------------------------------------------------------------
# V11.14: historical short-trade win-rate research lab.
# The V11.13 live scanner above is deliberately unchanged. Results produced
# below are for THREE NEW experimental 5M strategies, NOT the exact V11.13 BUY.
# ---------------------------------------------------------------------------
from datetime import date as _date, time as _time
from collections import defaultdict as _defaultdict

LAB_SIGNAL_NAMES = {"A": "A · 5분 지지 반등", "B": "B · 저항 돌파·재확인", "C": "C · 1시간 상승 흐름·눌림"}
LAB_WARMUP_BARS = 26
LAB_MAX_PAGES = 5
LAB_MIN_CONTIGUOUS = 25
LAB_MAX_HORIZON = 24   # 120 minutes / five-minute candle


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


def lab_fetch_5m(api_get, market, cfg, kst):
    """Page backwards with KST-exclusive `to`; never fabricate missing bars.

    For date D, include 4h prehistory and at least 2h following D to evaluate
    trades whose last signal is at D 23:55. Return only observed 5M OHLCV.
    """
    begin = datetime.combine(cfg["day"], _time.min, tzinfo=kst) - timedelta(hours=4)
    end = datetime.combine(cfg["day"] + timedelta(days=1), _time.min, tzinfo=kst) + timedelta(hours=2, minutes=5)
    cursor = end
    observed = {}
    truncated = True
    for _ in range(LAB_MAX_PAGES):
        raw = api_get("/v1/candles/minutes/5", {
            "market": market, "count": 200,
            "to": cursor.strftime("%Y-%m-%d %H:%M:%S")})
        if not isinstance(raw, list):
            raise ValueError("과거 5분봉 API 응답 오류")
        oldest = None
        for bar in raw:
            if not isinstance(bar, dict):
                continue
            try:
                ts = local_time(bar["candle_date_time_kst"], kst)
                op, hi, lo, cl = (float(bar[k]) for k in
                                  ("opening_price", "high_price", "low_price", "trade_price"))
                vol = float(bar["candle_acc_trade_volume"])
                if not (all(isfinite(x) and x > 0 for x in (op, hi, lo, cl))
                        and isfinite(vol) and vol >= 0
                        and lo <= min(op, cl) and hi >= max(op, cl) and lo <= hi):
                    continue
                if oldest is None or ts < oldest:
                    oldest = ts
                if begin <= ts < end:
                    observed[ts] = {"t": ts, "o": op, "h": hi, "l": lo, "c": cl, "v": vol}
            except (TypeError, ValueError, KeyError, OverflowError):
                continue
        if oldest is None:
            truncated = False
            break
        if oldest <= begin or len(raw) < 200:
            truncated = False
            break
        # Exclusive pagination; do not skip the oldest bar or repeat a page.
        if oldest >= cursor:
            raise ValueError("과거 봉 페이지가 전진하지 않습니다")
        cursor = oldest
    return [observed[t] for t in sorted(observed)], truncated


def lab_contiguous(rows, first, last):
    """All observed bars in the requested [first,last] range are 5m apart."""
    if first < 0 or last >= len(rows):
        return False
    return all(rows[j]["t"] - rows[j-1]["t"] == timedelta(minutes=5)
               for j in range(first+1, last+1))


def lab_signals(rows, i):
    """Signal definitions use data <= index i ONLY; no entry/outcome prices."""
    c = rows[i]
    past = rows[i-20:i]
    mean_volume = sum(v["v"] for v in past) / 20
    if mean_volume <= 0:
        return []
    vr = c["v"] / mean_volume
    span = max(1e-12, c["h"]-c["l"])
    bull = c["c"] > c["o"] and (c["c"]-c["o"]) / span >= 0.30
    prior = rows[i-12:i]
    support = min(x["l"] for x in prior)
    # A: same-candle support touch + bullish reclaim. 5M confirmed at close.
    a = (bull and vr >= 0.95 and c["l"] <= support*1.004 and
         c["c"] >= support*1.003 and c["c"] > rows[i-1]["c"])

    # B: previous candle was a new 12-bar breakout, current bar retests the
    # breakout level and closes back above it. Current and prev are completed.
    threshold = max(x["h"] for x in rows[i-14:i-1])
    prev = rows[i-1]
    pv = prev["v"] / mean_volume
    b = (bull and prev["c"] >= threshold*1.002 and
         rows[i-2]["c"] <= threshold*1.002 and
         threshold*0.996 <= c["l"] <= threshold*1.006 and c["c"] >= threshold*1.003 and
         max(vr, pv) >= 1.20 and vr >= 0.70)

    # C: approximate the 1-hour flow solely from 24 previously COMPLETED 5M
    # bars (two consecutive one-hour blocks). Reclaim of the prior 10-bar MA.
    ma_old = sum(x["c"] for x in rows[i-24:i-12])/12
    ma_recent = sum(x["c"] for x in rows[i-12:i])/12
    ma10 = sum(x["c"] for x in rows[i-10:i])/10
    fast = sum(x["c"] for x in rows[i-6:i])/6
    fast_old = sum(x["c"] for x in rows[i-7:i-1])/6
    csetup = (bull and vr >= 0.9 and ma_recent >= ma_old*1.003 and
              fast >= fast_old and c["l"] <= ma10*1.005 and
              c["c"] >= ma10*1.001 and c["c"] >= rows[i-1]["c"])
    return [s for s, flag in (("A", a), ("B", b), ("C", csetup)) if flag]


def lab_outcome(rows, index, cfg):
    """Market entry at NEXT observed 5M opening; conservative OHLC priority.

    Missing future candle => UNKNOWN, not TIME/SL/TP. If both levels are hit
    in one five-minute bar, SL is assumed first. Fee and slippage charged on
    both entry and exit. No fill certainty or tick rounding is claimed.
    """
    n = cfg["bars"]
    if index + n >= len(rows):
        return {"outcome": "UNKNOWN", "reason": "이후 봉 부족"}
    if not lab_contiguous(rows, index, index+n):
        return {"outcome": "UNKNOWN", "reason": "진입 후 봉 공백"}
    entry = rows[index+1]["o"]
    target = entry*(1+cfg["tp"]/100)
    stop = entry*(1-cfg["sl"]/100)
    outcome, exit_price, elapsed = "TIME", rows[index+n]["c"], n*5
    for j in range(index+1, index+n+1):
        r = rows[j]
        stop_hit = r["l"] <= stop
        target_hit = r["h"] >= target
        if stop_hit:  # worst case for same-candle ambiguity
            # If a 5M bar opens already below stop, assume the WORSE open fill.
            realized_stop = min(stop, r["o"])
            outcome, exit_price, elapsed = "SL", realized_stop, (j-index)*5
            break
        if target_hit:
            outcome, exit_price, elapsed = "TP", target, (j-index)*5
            break
    cost_in = entry*(1 + (cfg["fee"]+cfg["slip"])/100)
    revenue_out = exit_price*(1-(cfg["fee"]+cfg["slip"])/100)
    net = (revenue_out/cost_in - 1)*100
    return {"outcome": outcome, "entry": round(entry, 10),
            "exit": round(exit_price, 10), "net_pct": round(net, 4),
            "minutes": elapsed}


def lab_evaluate_symbol(api_get, market, cfg, kst):
    candles, truncated = lab_fetch_5m(api_get, market, cfg, kst)
    day_start = datetime.combine(cfg["day"], _time.min, tzinfo=kst)
    day_end = day_start + timedelta(days=1)
    count_by = _defaultdict(int)
    detail = []
    cooldown = {name: -1 for name in LAB_SIGNAL_NAMES}
    contiguous_windows = 0
    observed_day = sum(day_start <= x["t"] < day_end for x in candles)
    if len(candles) < LAB_WARMUP_BARS + cfg["bars"]:
        return {"market": market, "bars": observed_day, "truncated": truncated,
                "candidates": 0, "valid_windows": 0, "missing_outcome": 0, "trades": []}
    for i in range(LAB_WARMUP_BARS, len(candles)-1):
        t = candles[i]["t"]
        if not day_start <= t < day_end:
            continue
        if not lab_contiguous(candles, i-24, i):
            continue
        contiguous_windows += 1
        sigs = lab_signals(candles, i)
        for name in sigs:
            if i < cooldown[name]:
                continue
            cooldown[name] = i + cfg["bars"]
            count_by["signal"] += 1
            outcome = lab_outcome(candles, i, cfg)
            if outcome["outcome"] == "UNKNOWN":
                count_by["missing"] += 1
                continue
            detail.append({"market": market, "strategy": name,
                           "time": (t+timedelta(minutes=5)).strftime("%m-%d %H:%M"),
                           "signal_close": round(candles[i]["c"], 10), **outcome})
    return {"market": market, "bars": observed_day, "truncated": truncated,
            "candidates": count_by["signal"], "valid_windows": contiguous_windows,
            "missing_outcome": count_by["missing"], "trades": detail}


WINRATE_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14 · 단타 실험 신호 승률 검증실</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#171d34;--card:#11182b;--border:#33405e;--fg:#eff4ff;--muted:#aabbd7;--blue:#3564ec;--green:#4ade80}
*{box-sizing:border-box}body{background:var(--bg);color:var(--fg);margin:0;font:15px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1000px;margin:auto;padding:20px 14px 55px}h1{font-size:23px;line-height:1.4}h2{font-size:18px;margin:0 0 10px}.panel{border:1px solid var(--border);border-radius:15px;background:var(--panel);margin-top:14px;padding:17px}.muted,.hint{color:var(--muted);font-size:13px}.danger{color:#fca5a5}.good{color:var(--green)}
.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.field{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--muted)}
input,select,button{font:inherit}input,select{background:#10182a;border:1px solid var(--border);border-radius:8px;color:var(--fg);padding:10px;width:100%;min-width:0}
button{background:var(--blue);border:0;border-radius:9px;padding:12px 15px;color:#fff;font-weight:800;cursor:pointer}button:disabled{opacity:.45}a{color:#b5cfff}progress{width:100%;height:13px;margin-top:12px}.statgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}.stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px}.stat strong{display:block;font-size:20px}.stat small{color:var(--muted)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--border);padding:8px 7px;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}.scroll{overflow:auto}.note{background:#11192d;border-radius:10px;padding:12px;font-size:13px;line-height:1.65}.flex{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.detail{border:1px solid var(--border);padding:12px;border-radius:10px;margin:8px 0;background:var(--card);font-size:13px}.detail b{display:block;margin-bottom:5px}.status{white-space:pre-wrap;font-size:13px;color:var(--muted)}
@media(max-width:640px){.controls{grid-template-columns:repeat(2,minmax(0,1fr))}th,td{padding:8px 5px}}
</style></head><body><main>
<h1>📊 V11.14 · 단타 승률 검증실</h1><p class="muted">V11.13 실시간 스캐너와 V2 메인은 그대로 둡니다. <b>이 페이지의 A/B/C는 신규 실험 전략</b>이며 V11.13 BUY 실적이 아닙니다.</p>
<section class="panel"><h2>검증 조건</h2><div class="controls">
<label class="field">검증 날짜 (KST)<input id="day" type="date" required></label>
<label class="field">동일 규칙 표본 코인<select id="count"><option value="20">20개 · 빠른 예비검사</option><option value="40" selected>40개 · 비교검사</option><option value="80">80개 · 확대검사</option></select></label>
<label class="field">익절 목표 (%)<input id="tp" type="number" min="0.3" max="10" step="0.1" value="1.5"></label>
<label class="field">손절 기준 (%)<input id="sl" type="number" min="0.2" max="8" step="0.1" value="1.0"></label>
<label class="field">종료 시한<select id="horizon"><option value="30">30분</option><option value="60" selected>60분</option><option value="120">120분</option></select></label>
<label class="field">매수·매도 각각 수수료 (%)<input id="fee" type="number" min="0" max="0.5" step="0.01" value="0.04"></label>
<label class="field">매수·매도 각각 슬리피지 가정 (%)<input id="slip" type="number" min="0" max="0.5" step="0.01" value="0.05"></label>
</div><p class="hint">수수료 0.04%는 입력 예시이며 실제 계정 수수료를 확인해 바꿀 수 있습니다. 슬리피지는 모의 가정입니다. 과거 완료된 날짜 하루씩 검사합니다.</p>
<div class="flex"><button id="run">선택 날짜 승률 검사</button><a href="/backtest-daytrade">V11.13 실시간 스캐너</a><a href="/">V2 메인</a></div>
<p class="hint">처음 실행한 종목 표본을 이 브라우저에 고정합니다. 다른 날짜도 동일 코인으로 검사합니다. <button id="resetUniverse" type="button" style="background:#34425f;padding:6px 10px;font-size:12px">표본 고정 해제</button></p>
<progress id="progress" value="0" max="100"></progress><p class="status" id="status">실행 전 · 실제 승률 데이터 없음</p></section>
<section class="panel"><h2>세 가지 실험 신호 비교</h2><p class="hint">A: 5분 지지 터치·양봉 회복 | B: 이전 봉 저항 돌파 후 다음 봉 재확인 | C: 1시간 상당 상승 흐름에서 5분 눌림 후 반등. 세 신호 모두 <b>확정된 과거 5분봉만</b> 사용합니다.</p><div class="scroll" id="table"><p class="muted">검사 후 결과 표시</p></div><p class="hint">익절 선도달률 = TP / 판정 가능 거래. 순승률 = 수수료·슬리피지 차감 후 수익 거래 비율. 만기청산(TIME)도 순승률·평균손익에 포함합니다.</p></section>
<section class="panel"><h2>데이터 신뢰도</h2><div class="statgrid" id="quality"><div class="stat">조회 전</div></div><p class="hint">동일 봉에 익절·손절 모두 닿으면 <b>손절 우선</b> 처리. 중간 봉이 빠져 결과 순서를 알 수 없으면 <b>미판정 제외</b>. 시장가 진입은 다음 봉의 시가를 가정하고 체결 보장은 없습니다.</p></section>
<section class="panel"><h2>다른 날짜와 누적 비교</h2><p class="hint">같은 코인 선정 규칙·손익 조건의 서로 다른 날짜만 합칩니다. 같은 날짜를 다시 돌리면 교체합니다. 기록은 현재 브라우저에만 저장됩니다.</p><div id="history" class="scroll">검사 후 표시</div><div class="flex"><button id="csv" type="button">검사 기록 CSV 저장</button><button id="clear" type="button">누적 기록 삭제</button></div></section>
<details class="panel"><summary>최근 실험 거래 사례</summary><div id="examples">검사 후 표시</div></details>
<section class="panel"><h2>이 결과가 뜻하지 않는 것</h2><div class="note">동일 날짜의 과거 OHLCV를 사용한 모의 성적이며 <b>실제 주문 승률이 아닙니다.</b> 평가 종목은 현재 상장된 KRW 코인을 일정 간격으로 선정하므로 과거 상장폐지 종목이 빠지는 생존자 편향이 있습니다. 신호는 미래 봉을 보지 않지만 과거 날짜를 반복해 조건을 바꾸면 과적합될 수 있습니다. 호가 잔량·지연·실제 체결·정확한 호가단위는 미반영입니다. 한 날짜 또는 소수 거래만으로 실전 승률을 주장할 수 없습니다. 같은 종목·날짜의 여러 신호는 서로 독립적인 거래 기회가 아닐 수 있습니다.</div></section>
</main><script>
const $=id=>document.getElementById(id),esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const metric=(a,b)=>`<div class="stat"><small>${esc(a)}</small><strong>${esc(b)}</strong></div>`;
const strategies={A:'A 지지 반등',B:'B 돌파·재확인',C:'C 1H 흐름·눌림'};
const storageKey='bithumb_v1114_winrate_history';let allTrades=[],busy=false,latest=null;
function readHist(){try{const x=JSON.parse(localStorage.getItem(storageKey)||'[]');return Array.isArray(x)?x:[]}catch{return []}}
function writeHist(a){try{localStorage.setItem(storageKey,JSON.stringify(a.slice(-120)));return true}catch{return false}}
const dateInSeoul=(dt)=>{const ps=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(dt);const g=k=>ps.find(p=>p.type===k)?.value;return `${g('year')}-${g('month')}-${g('day')}`};
const yesterday=()=>dateInSeoul(new Date(Date.now()-86400000));$('day').value=yesterday();$('day').max=yesterday();
const cols=['전략','거래','익절','손절','시간종료','익절 선도달','순승률','평균 순손익'];
const emptyStat=()=>({trades:0,tp:0,sl:0,time:0,wins:0,net:0});
function statsOf(trades){const s={A:emptyStat(),B:emptyStat(),C:emptyStat()};for(const x of trades){if(!s[x.strategy])continue;const t=s[x.strategy];t.trades++;t.net+=x.net_pct;t.wins+=x.net_pct>0?1:0;if(x.outcome==='TP')t.tp++;else if(x.outcome==='SL')t.sl++;else t.time++}return s}
function addStats(a,b){const r={};for(const k of Object.keys(strategies)){r[k]={};for(const field of Object.keys(emptyStat()))r[k][field]=(a[k]?.[field]||0)+(b[k]?.[field]||0)}return r}
function renderTable(stats,caption){let html=`<p class="muted">${esc(caption)}</p><table><thead><tr>${cols.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>`;
for(const [key,label] of Object.entries(strategies)){const s=stats[key]||emptyStat(),n=s.trades;html+=`<tr><td>${esc(label)}</td><td>${n}</td><td>${s.tp}</td><td>${s.sl}</td><td>${s.time}</td><td>${n?(100*s.tp/n).toFixed(1)+'%':'—'}</td><td>${n?(100*s.wins/n).toFixed(1)+'%':'—'}</td><td>${n?(s.net/n).toFixed(2)+'%':'—'}</td></tr>`}return html+'</tbody></table><p class="hint">※ 전략별 판정 거래가 30건 미만이면 작은 표본입니다. 과거 날짜를 바꾸며 얻은 수치를 실전 보장 승률로 해석하지 마세요.</p>'}
function currentCfg(){const config={date:$('day').value,count:Number($('count').value),tp:Number($('tp').value),sl:Number($('sl').value),horizon:Number($('horizon').value),fee:Number($('fee').value),slip:Number($('slip').value)};if(!config.date||config.date>yesterday())throw Error('완료된 과거 날짜를 선택하세요');if(!Number.isFinite(config.tp)||!Number.isFinite(config.sl)||!Number.isFinite(config.fee)||!Number.isFinite(config.slip)||config.tp<0.3||config.tp>10||config.sl<0.2||config.sl>8||config.fee<0||config.fee>0.5||config.slip<0||config.slip>0.5)throw Error('손익/비용 입력 범위를 확인하세요');return config}
const keyOf=r=>JSON.stringify([r.count,r.tp,r.sl,r.horizon,r.fee,r.slip,r.sample||'']);
function displayHistory(){const hist=readHist(),n=Number($('count').value);let selected=[];try{selected=JSON.parse(localStorage.getItem(universeKey(n))||'[]')}catch{};const cfg={count:n,tp:Number($('tp').value),sl:Number($('sl').value),horizon:Number($('horizon').value),fee:Number($('fee').value),slip:Number($('slip').value),sample:Array.isArray(selected)?selected.join('|'):''};const matching=hist.filter(x=>keyOf(x)===keyOf(cfg));let total={A:emptyStat(),B:emptyStat(),C:emptyStat()};for(const item of matching)total=addStats(total,item.stats);$('history').innerHTML=renderTable(total,`동일 설정 ${matching.length}개 날짜 누적 · 거래가 0이면 승률 표시 불가`)+(matching.length?`<p class="hint">누적 날짜: ${matching.map(x=>esc(x.date)).join(', ')}</p>`:'')}
async function getJson(url,opts){const c=new AbortController(),timer=setTimeout(()=>c.abort(),120000);try{const r=await fetch(url,{...opts,signal:c.signal,cache:'no-store'}),v=await r.json();if(!r.ok||!v.ok)throw Error(v.error||`HTTP ${r.status}`);return v}finally{clearTimeout(timer)}}
const universeKey=n=>`bithumb_v1114_fixed_sample_${n}`;
async function frozenSample(n){let stored=null;try{stored=JSON.parse(localStorage.getItem(universeKey(n))||'null')}catch{};if(Array.isArray(stored)&&stored.length===n&&stored.every(x=>/^KRW-[A-Z0-9]+$/.test(x)))return stored;
const u=await getJson('/api/winrate14/universe?count='+n);try{localStorage.setItem(universeKey(n),JSON.stringify(u.items))}catch{throw Error('비교 종목 고정 저장 실패. 브라우저 저장 기능을 확인하세요')};return u.items;}
async function workerPool(groups,workers,fn){let i=0;let errors=[];async function work(){while(i<groups.length){const batch=groups[i++];try{await fn(batch)}catch(e){errors.push(String(e))}}}await Promise.all(Array.from({length:Math.min(workers,groups.length)},work));return errors}
async function run(){if(busy)return;busy=true;$('run').disabled=true;let config,failed=0,processed=0,totalBars=0,signals=0,unresolved=0,truncated=0,eligible=0,returned=[];const start=performance.now();try{config=currentCfg();const names=await frozenSample(config.count);config.sample=names.join('|');const batches=[];for(let i=0;i<names.length;i+=2)batches.push(names.slice(i,i+2));$('progress').max=names.length;$('progress').value=0;$('status').textContent=`과거 ${config.date} · 표본 ${names.length}개 캔들 조회 중 (일반 실시간 스캔보다 오래 걸릴 수 있습니다)`;
const errors=await workerPool(batches,2,async namesBatch=>{const r=await getJson('/api/winrate14/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...config,markets:namesBatch})});processed+=r.scanned;failed+=r.errors;for(const item of r.results){totalBars+=item.bars||0;signals+=item.candidates||0;unresolved+=item.missing_outcome||0;eligible+=item.valid_windows>0?1:0;truncated+=item.truncated?1:0;returned.push(...(item.trades||[]))}$('progress').value=processed;$('status').textContent=`조회 ${processed}/${names.length}개 · 조건 발생 ${signals}건 · 판정 ${returned.length}건 · API 오류 ${failed}건`;});if(errors.length||failed||processed!==names.length)throw Error(`일부 API 실패: ${errors.length}개 배치 / ${failed}개 코인. 불완전한 데이터는 승률로 저장하지 않습니다.`);
allTrades=returned;latest=config;const s=statsOf(allTrades);$('table').innerHTML=renderTable(s,`${esc(config.date)} · 전체 ${names.length}개 표본 · 총 ${allTrades.length}건 판정 가능 거래`);
$('quality').innerHTML=[['검사 대상',names.length+'개'],['검사 가능 종목',eligible+'개'],['해당 날짜 5분봉',totalBars+'개'],['실험 신호',signals+'건'],['결과 미판정(봉 공백 등)',unresolved+'건'],['조회 5페이지 상한 도달',truncated+'개']].map(x=>metric(...x)).join('');
$('examples').innerHTML=allTrades.length?allTrades.slice(0,40).map(x=>`<div class="detail"><b>${esc(x.market)} · ${esc(strategies[x.strategy])} · ${esc(x.outcome)}</b>신호 확정 ${esc(x.time)} · 진입 ${esc(x.entry)}원 · 청산 ${esc(x.exit)}원 · 순손익 ${Number(x.net_pct).toFixed(2)}% · 보유 ${x.minutes}분</div>`).join(''):'<p class="muted">검증된 거래가 없습니다. 0% 승률로 취급하지 마세요.</p>';
const hist=readHist().filter(x=>!(x.date===config.date&&keyOf(x)===keyOf(config)));hist.push({...config,stats:s,recorded:new Date().toISOString()});const stored=writeHist(hist);displayHistory();const sec=((performance.now()-start)/1000).toFixed(1);$('status').textContent=`✅ ${config.date} · ${processed}개 표본 · ${sec}초 · 판정 가능 ${allTrades.length}거래 · 미판정 ${unresolved}건${stored?'':' · 브라우저 기록 저장 실패'}. 실전 승률로 오해하지 마세요.`;
}catch(e){$('status').textContent='⚠️ 검증 실패 · '+String(e.message||e)+' · 일부 결과로 승률을 계산하지 않습니다.';$('table').textContent='미완료 · 신뢰 가능한 승률 없음'}finally{busy=false;$('run').disabled=false}}
$('run').addEventListener('click',run);$('csv').onclick=()=>{const rows=readHist(),head=['date','count','tp','sl','horizon','fee','slip','strategy','trades','tp_count','sl_count','time_count','wins','net_pct_sum'];const lines=[head.join(',')];for(const r of rows)for(const id of Object.keys(strategies)){const s=r.stats[id]||emptyStat();lines.push([r.date,r.count,r.tp,r.sl,r.horizon,r.fee,r.slip,id,s.trades,s.tp,s.sl,s.time,s.wins,s.net].join(','))}const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='bithumb_winrate_lab_v1114.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
$('resetUniverse').onclick=()=>{if(confirm('현재 코인 표본 고정을 해제할까요? 이후 날짜 비교는 새 표본으로 따로 집계됩니다.')){for(const n of [20,40,80])localStorage.removeItem(universeKey(n));latest=null;displayHistory()}};
$('clear').onclick=()=>{if(confirm('이 브라우저의 V11.14 누적 통계만 삭제할까요?')){try{localStorage.removeItem(storageKey);displayHistory()}catch{alert('브라우저 저장소 삭제 실패')}}};for(const id of ['count','tp','sl','horizon','fee','slip'])$(id).addEventListener('change',displayHistory);displayHistory();
</script></body></html>'''


def register_winrate_lab(app, deps):
    api_get, get_markets, kst = deps["api_get"], deps["get_markets"], deps["KST"]
    sample_cache = {"at": 0, "markets": None}
    sample_lock = Lock()

    @app.get("/backtest-winrate")
    def winrate_lab_page_1114():
        return render_template_string(WINRATE_PAGE)

    @app.get("/api/winrate14/universe")
    def winrate_lab_universe_1114():
        try:
            n = int(request.args.get("count", "40"))
            if n not in (20, 40, 80):
                raise ValueError("표본은 20/40/80개만 선택할 수 있습니다")
            with sample_lock:
                if sample_cache["markets"] is None or time.time()-sample_cache["at"]>600:
                    names = sorted({m for m in get_markets() if isinstance(m, str) and MARKET_RE.fullmatch(m)})
                    if not names:
                        raise ValueError("KRW 종목 목록이 비었습니다")
                    sample_cache.update(markets=names, at=time.time())
                names = sample_cache["markets"]
            if len(names) <= n:
                chosen = names
            else:
                # Deterministic equal-rank sampling across all CURRENT listings.
                chosen = [names[round(j*(len(names)-1)/(n-1))] for j in range(n)]
            return jsonify({"ok": True, "items": chosen, "sampled": len(chosen),
                            "all_current_markets": len(names)})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:150]}), 400

    @app.post("/api/winrate14/evaluate")
    def winrate_lab_evaluate_1114():
        try:
            payload = request.get_json(silent=True)
            cfg = lab_valid_request(payload, kst)
            markets = payload.get("markets") if isinstance(payload, dict) else None
            if (not isinstance(markets, list) or not 1 <= len(markets) <= 2 or
                    len(set(markets)) != len(markets) or
                    any(not isinstance(m, str) or not MARKET_RE.fullmatch(m) for m in markets)):
                raise ValueError("종목은 한 번에 최대 2개만 검증합니다")
            # Universe was retrieved through /universe first; reuse the cache
            # instead of querying all listings for EVERY two-market batch.
            with sample_lock:
                if sample_cache["markets"] is None:
                    sample_cache.update(markets=sorted({m for m in get_markets()
                        if isinstance(m, str) and MARKET_RE.fullmatch(m)}), at=time.time())
                allowed = set(sample_cache["markets"] or [])
            if not allowed:
                raise ValueError("KRW 종목 목록을 확인할 수 없습니다")
            if any(m not in allowed for m in markets):
                raise ValueError("현재 빗썸 KRW 목록에 없는 종목입니다")
            results = []
            errors = 0
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {pool.submit(lab_evaluate_symbol, api_get, market, cfg, kst): market
                           for market in markets}
                for f in as_completed(futures):
                    try:
                        results.append(f.result())
                    except Exception:
                        errors += 1
            return jsonify({"ok": True, "scanned": len(markets), "results": results, "errors": errors})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:150]}), 400


# Preserve the entire existing V11.13 registration and add independent routes.
_v1113_register_daytrade_review = register_daytrade_review

def register_daytrade_review(app, deps):
    _v1113_register_daytrade_review(app, deps)
    register_winrate_lab(app, deps)
