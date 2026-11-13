"""V11.13 live scanner + independent V11.14.5 historical C-strategy research lab.

The V11.13 read-only scanner is preserved and separate historical research
endpoints (V11.14.x) are registered alongside it. app.py and the V2 main
scanner are untouched. The V11.14.5 corrected outcome logic runs ONLY on
/backtest-winrate-c-validated and does not rewrite older experiment results.
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
# V11.14.2: unchanged historical strategy lab plus coverage/overlap audits.
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


def lab_longest_consecutive_5m(rows):
    """Diagnostic only: number of consecutive *observed* five-minute bars."""
    best = run = 0
    previous = None
    for row in rows:
        run = run + 1 if (previous is not None and
                           row["t"] - previous == timedelta(minutes=5)) else 1
        best = max(best, run)
        previous = row["t"]
    return best


def lab_diagnostic(market, candles, cfg, truncated, observed_day,
                   valid_windows, signals, missing, trades, day_start, day_end):
    """Why a market could/could not contribute to historical statistics.

    Does NOT change trading signals, simulated fills, or inclusion rules.
    """
    day_rows = [r for r in candles if day_start <= r["t"] < day_end]
    longest = lab_longest_consecutive_5m(day_rows)
    history_longest = lab_longest_consecutive_5m(candles)
    if observed_day == 0:
        code = 'NO_DAY_BARS'
    elif len(candles) < LAB_WARMUP_BARS + cfg["bars"]:
        code = 'TOO_FEW_FETCHED'
    elif valid_windows == 0 and history_longest < 25:
        code = 'NO_25_CONSECUTIVE'
    elif valid_windows == 0:
        code = 'NO_VALID_WINDOW'
    elif signals == 0:
        code = 'NO_A_B_C_SIGNAL'
    elif not trades:
        code = 'OUTCOMES_UNKNOWN'
    else:
        code = 'TRADE_EVALUATED'
    return {'market': market, 'bars': observed_day, 'fetched_bars': len(candles),
            'missing_5m_slots': max(0, 288-observed_day),
            'longest_day_run': longest, 'longest_history_run': history_longest,
            'truncated': truncated, 'valid_windows': valid_windows,
            'signals': signals, 'missing_outcome': missing, 'decided_trades': len(trades),
            'diag_code': code}


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
        return {**lab_diagnostic(market,candles,cfg,truncated,observed_day,
                                 0,0,0,[],day_start,day_end),
                'candidates': 0, 'trades': []}
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
    return {**lab_diagnostic(market,candles,cfg,truncated,observed_day,
                             contiguous_windows,count_by["signal"],
                             count_by["missing"],detail,day_start,day_end),
            "candidates": count_by["signal"], "trades": detail}


def lab_audit_gap_with_minutes(api_get, market, cfg, kst):
    """Spot-check ONE 5M gap using independently queried 1M candles.

    Never manufacture OHLC. 1M found within a missing 5M interval is an
    inconsistency needing manual investigation, not proof of the root cause.
    Absence of 1M isn't proof the exchange experienced no trades.
    This function is only called by the optional three-coin audit endpoint.
    """
    candles, truncated = lab_fetch_5m(api_get, market, cfg, kst)
    day_start = datetime.combine(cfg["day"], _time.min, tzinfo=kst)
    day_end = day_start + timedelta(days=1)
    day_rows = [x for x in candles if day_start <= x["t"] < day_end]
    gaps = []
    for left, right in zip(day_rows[:-1], day_rows[1:]):
        elapsed = int((right["t"]-left["t"]).total_seconds()//60)
        if elapsed > 5 and elapsed % 5 == 0:
            gaps.append((elapsed//5-1, left["t"], right["t"]))
    result = {"market": market, "bars": len(day_rows),
              "missing_slots": max(0, 288-len(day_rows)),
              "longest_run": lab_longest_consecutive_5m(day_rows),
              "gap_pairs": len(gaps), "page_limit": truncated,
              "gap_start": None, "gap_end": None,
              "gap_5m_slots": 0, "one_min_bars": None,
              "one_min_range_verified": False, "assessment": "검사할 내부 공백 없음"}
    if not gaps:
        return result
    # Prefer the shortest fully internal gap. Bound the separate 1M request.
    gaps.sort(key=lambda g:(g[0], g[1]))
    missing, left, right = gaps[0]
    first_missing = left+timedelta(minutes=5)
    result.update(gap_start=first_missing.strftime("%m-%d %H:%M"),
                  gap_end=right.strftime("%m-%d %H:%M"),gap_5m_slots=missing)
    if missing > 6:
        result["assessment"] = "표본 공백이 30분 초과 · 추가 자동조회 생략"
        return result
    raw = api_get("/v1/candles/minutes/1", {
        "market": market, "count": 200,
        "to": right.strftime("%Y-%m-%d %H:%M:%S")})
    if not isinstance(raw, list):
        raise ValueError("1분봉 교차확인 API 형식 오류")
    minutes = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            t = local_time(row["candle_date_time_kst"], kst)
            if t < right:
                minutes.append(t)
        except (KeyError, TypeError, ValueError):
            continue
    if not minutes:
        result["assessment"] = "1분봉 응답 없음 · 검증 불가"
        return result
    observed = sorted(set(minutes))
    in_gap = [t for t in observed if first_missing <= t < right]
    covered = min(observed) <= first_missing
    result.update(one_min_bars=len(in_gap), one_min_range_verified=covered)
    if in_gap:
        result["assessment"] = "⚠️ 누락된 5분 구간 안에 1분봉 존재 · 데이터 재확인 필요"
    elif covered:
        result["assessment"] = "표본 구간에 1분봉 미관측 · 무거래 가능성 (확정 아님)"
    else:
        result["assessment"] = "1분봉 조회범위가 공백 시작까지 닿지 않아 미판정"
    return result


WINRATE_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.2 · 단타 승률 검증 데이터 점검</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#171d34;--card:#11182b;--border:#33405e;--fg:#eff4ff;--muted:#aabbd7;--blue:#3564ec;--green:#4ade80}
*{box-sizing:border-box}body{background:var(--bg);color:var(--fg);margin:0;font:15px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1000px;margin:auto;padding:20px 14px 55px}h1{font-size:23px;line-height:1.4}h2{font-size:18px;margin:0 0 10px}.panel{border:1px solid var(--border);border-radius:15px;background:var(--panel);margin-top:14px;padding:17px}.muted,.hint{color:var(--muted);font-size:13px}.danger{color:#fca5a5}.good{color:var(--green)}
.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.field{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--muted)}
input,select,button{font:inherit}input,select{background:#10182a;border:1px solid var(--border);border-radius:8px;color:var(--fg);padding:10px;width:100%;min-width:0}
button{background:var(--blue);border:0;border-radius:9px;padding:12px 15px;color:#fff;font-weight:800;cursor:pointer}button:disabled{opacity:.45}a{color:#b5cfff}progress{width:100%;height:13px;margin-top:12px}.statgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}.stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px}.stat strong{display:block;font-size:20px}.stat small{color:var(--muted)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--border);padding:8px 7px;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}.scroll{overflow:auto}.note{background:#11192d;border-radius:10px;padding:12px;font-size:13px;line-height:1.65}.flex{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.detail{border:1px solid var(--border);padding:12px;border-radius:10px;margin:8px 0;background:var(--card);font-size:13px}.detail b{display:block;margin-bottom:5px}.status{white-space:pre-wrap;font-size:13px;color:var(--muted)}
@media(max-width:640px){.controls{grid-template-columns:repeat(2,minmax(0,1fr))}th,td{padding:8px 5px}}
</style></head><body><main>
<h1>📊 V11.14.2 · 승률 데이터 신뢰도 검증</h1><p class="muted">V11.13 실시간 스캐너와 V2 메인은 그대로 둡니다. <b>이 페이지의 A/B/C는 신규 실험 전략</b>이며 V11.13 BUY 실적이 아닙니다.</p>
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
<section class="panel"><h2>🔎 80개 중 왜 일부만 검증됐을까?</h2><p class="hint">종목별 수집된 5분봉 개수와 최장 연속 구간을 구분합니다. <b>승률 계산·신호·표본 구성은 V11.14와 동일</b>합니다. 거래가 없어서 봉이 비었는지, 해당 날짜에 아직 상장되지 않았는지는 이 자료만으로 확정할 수 없습니다.</p><div id="diagSummary" class="statgrid">검사 후 원인 표시</div><details style="margin-top:12px"><summary>종목별 검증 가능 여부 자세히 보기</summary><div id="diagRows" class="scroll">검사 후 표시</div></details><p class="hint">하루 288개 = 24시간×12개 5분 구간. 빈 구간에는 미상장·거래 없음·수집 데이터 누락 등 여러 원인이 있을 수 있습니다. 거래가 없던 구간의 봉을 인위적으로 만들지 않습니다.</p><button id="diagCsv" type="button" disabled>종목별 진단 CSV 저장</button></section>

<section class="panel"><h2>📊 관측 밀도별 승률과 반복신호 편중</h2>
<p class="hint">원래 고정 표본을 바꾸지 않고 <b>해당 날짜의 관측 5분봉 수</b>로 그룹을 나눕니다. 당일 봉 수는 거래대금이나 실거래 유동성과 동일하지 않습니다. 분류는 당일 데이터가 끝난 뒤 만든 <b>사후 분석</b>이므로 BUY 조건으로 사용하지 않습니다.</p>
<div id="densityResults" class="scroll">검사 완료 후 그룹별 결과 표시</div>
<h3 style="font-size:15px;margin-top:16px">종목·전략별 첫 거래만 따로 집계</h3>
<p class="hint">같은 종목에서 같은 전략이 한 날짜에 여러 번 발생해도 첫 판정 거래 하나만 계산합니다. 기존 A/B/C 전체 통계는 변경하지 않습니다. 서로 다른 코인의 동시 신호는 여전히 독립이라고 보장할 수 없습니다.</p>
<div id="firstOnlyResults" class="scroll">검사 완료 후 편중 비교 표시</div></section>
<section class="panel"><h2>🧪 ONDO · AEON · MEGA 1분봉 교차확인</h2>
<p class="hint">선택 날짜의 세 코인에서 <b>5분봉 내부 공백 한 곳만 표본으로 뽑아</b> 별도 1분봉 API로 확인합니다. 5분봉 전체를 재검사하거나 기존 승률을 바꾸지 않습니다. 1분봉이 안 보인다고 무거래로 확정할 수 없으며, 1분봉이 관측되면 수집 불일치를 추가 조사해야 합니다.</p>
<button id="audit3" type="button">세 종목 공백 교차확인</button>
<p id="auditStatus" class="status">진단 실행 전 · 선택한 날짜 기준</p>
<div id="auditResults" class="scroll"></div></section>
<section class="panel"><h2>다른 날짜와 누적 비교</h2><p class="hint">같은 코인 선정 규칙·손익 조건의 서로 다른 날짜만 합칩니다. 같은 날짜를 다시 돌리면 교체합니다. 기록은 현재 브라우저에만 저장됩니다.</p><div id="history" class="scroll">검사 후 표시</div><div class="flex"><button id="csv" type="button">검사 기록 CSV 저장</button><button id="clear" type="button">누적 기록 삭제</button></div></section>
<details class="panel"><summary>최근 실험 거래 사례</summary><div id="examples">검사 후 표시</div></details>
<section class="panel"><h2>이 결과가 뜻하지 않는 것</h2><div class="note">동일 날짜의 과거 OHLCV를 사용한 모의 성적이며 <b>실제 주문 승률이 아닙니다.</b> 평가 종목은 현재 상장된 KRW 코인을 일정 간격으로 선정하므로 과거 상장폐지 종목이 빠지는 생존자 편향이 있습니다. 신호는 미래 봉을 보지 않지만 과거 날짜를 반복해 조건을 바꾸면 과적합될 수 있습니다. 호가 잔량·지연·실제 체결·정확한 호가단위는 미반영입니다. 한 날짜 또는 소수 거래만으로 실전 승률을 주장할 수 없습니다. 같은 종목·날짜의 여러 신호는 서로 독립적인 거래 기회가 아닐 수 있습니다.</div></section>
</main><script>
const $=id=>document.getElementById(id),esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const metric=(a,b)=>`<div class="stat"><small>${esc(a)}</small><strong>${esc(b)}</strong></div>`;
const strategies={A:'A 지지 반등',B:'B 돌파·재확인',C:'C 1H 흐름·눌림'};
const storageKey='bithumb_v1114_winrate_history';let allTrades=[],diagRows=[],busy=false,latest=null;
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

function renderCoverageSplits(){
 const groups=[
  ['매우 조밀 (240~288봉)',d=>Number(d.bars)>=240],
  ['중간 (120~239봉)',d=>Number(d.bars)>=120&&Number(d.bars)<240],
  ['희소 (0~119봉)',d=>Number(d.bars)<120]
 ];
 let html='<table><thead><tr><th>당일 관측 밀도</th><th>코인</th><th>검사 가능</th><th>A 거래/순승률/평균손익</th><th>B 거래/순승률/평균손익</th><th>C 거래/순승률/평균손익</th></tr></thead><tbody>';
 for(const [label,test] of groups){
  const ds=diagRows.filter(test),set=new Set(ds.map(d=>d.market));
  const s=statsOf(allTrades.filter(t=>set.has(t.market)));
  const fmt=k=>{const v=s[k];return v.trades?`${v.trades}건 / ${(100*v.wins/v.trades).toFixed(1)}% / ${(v.net/v.trades).toFixed(2)}%`:'0건 / — / —'};
  html+=`<tr><td>${esc(label)}</td><td>${ds.length}</td><td>${ds.filter(d=>d.valid_windows>0).length}</td><td>${fmt('A')}</td><td>${fmt('B')}</td><td>${fmt('C')}</td></tr>`;
 }
 $('densityResults').innerHTML=html+'</tbody></table><p class="hint">※ 그룹 분류는 해당 날짜 사후 정보이며, 실제 매매 시점에 미래의 하루 봉 수를 알고 있다고 가정하지 않습니다.</p>';
 const sorted=allTrades.slice().sort((a,b)=>String(a.time).localeCompare(String(b.time)));
 const seen=new Set(),first=[];
 for(const t of sorted){const key=t.market+'|'+t.strategy;if(!seen.has(key)){seen.add(key);first.push(t)}}
 const all=statsOf(allTrades),firsts=statsOf(first);
 let h='<table><thead><tr><th>전략</th><th>전체 거래</th><th>첫 거래만</th><th>첫 거래 순승률</th><th>첫 거래 평균 순손익</th></tr></thead><tbody>';
 for(const [k,name] of Object.entries(strategies)){const s=firsts[k];h+=`<tr><td>${esc(name)}</td><td>${all[k].trades}</td><td>${s.trades}</td><td>${s.trades?(100*s.wins/s.trades).toFixed(1)+'%':'—'}</td><td>${s.trades?(s.net/s.trades).toFixed(2)+'%':'—'}</td></tr>`}
 $('firstOnlyResults').innerHTML=h+'</tbody></table><p class="hint">※ 동일 코인·동일 전략·동일 날짜 기준 첫 번째 판정 거래만 사용한 진단. 30건 미만은 특히 불안정합니다.</p>';
}
async function auditThree(){
 if(busy)return;
 $('audit3').disabled=true;
 $('auditStatus').textContent='1분봉·5분봉 공백 교차확인 중…';
 $('auditResults').textContent='';
 try{
  const cfg=currentCfg();
  const v=await getJson('/api/winrate14/audit-three',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  if(v.errors||!Array.isArray(v.results)||v.results.length!==3)throw Error('일부 종목 확인 실패 · 결과 확정 보류');
  let html='<table><thead><tr><th>종목</th><th>5분봉</th><th>최장 연속</th><th>공백 사례</th><th>누락 구간의 1분봉</th><th>판단</th></tr></thead><tbody>';
  for(const x of v.results){html+=`<tr><td>${esc(x.market)}</td><td>${x.bars}/288</td><td>${x.longest_run}봉</td><td>${x.gap_start?esc(x.gap_start)+'~'+esc(x.gap_end)+' ('+x.gap_5m_slots+'칸)':'없음'}</td><td>${x.one_min_bars==null?'미검사':x.one_min_bars+'개'}</td><td>${esc(x.assessment)}</td></tr>`}
  $('auditResults').innerHTML=html+'</tbody></table>';
  $('auditStatus').textContent='✅ '+cfg.date+' · 세 종목 표본 교차확인 완료. 공백 전체를 증명하는 검사는 아닙니다.';
 }catch(e){$('auditStatus').textContent='⚠️ 교차확인 실패: '+String(e.message||e)}
 finally{$('audit3').disabled=false}
}
$('audit3').addEventListener('click',auditThree);

function currentCfg(){const config={date:$('day').value,count:Number($('count').value),tp:Number($('tp').value),sl:Number($('sl').value),horizon:Number($('horizon').value),fee:Number($('fee').value),slip:Number($('slip').value)};if(!config.date||config.date>yesterday())throw Error('완료된 과거 날짜를 선택하세요');if(!Number.isFinite(config.tp)||!Number.isFinite(config.sl)||!Number.isFinite(config.fee)||!Number.isFinite(config.slip)||config.tp<0.3||config.tp>10||config.sl<0.2||config.sl>8||config.fee<0||config.fee>0.5||config.slip<0||config.slip>0.5)throw Error('손익/비용 입력 범위를 확인하세요');return config}
const keyOf=r=>JSON.stringify([r.count,r.tp,r.sl,r.horizon,r.fee,r.slip,r.sample||'']);
function displayHistory(){const hist=readHist(),n=Number($('count').value);let selected=[];try{selected=JSON.parse(localStorage.getItem(universeKey(n))||'[]')}catch{};const cfg={count:n,tp:Number($('tp').value),sl:Number($('sl').value),horizon:Number($('horizon').value),fee:Number($('fee').value),slip:Number($('slip').value),sample:Array.isArray(selected)?selected.join('|'):''};const matching=hist.filter(x=>keyOf(x)===keyOf(cfg));let total={A:emptyStat(),B:emptyStat(),C:emptyStat()};for(const item of matching)total=addStats(total,item.stats);$('history').innerHTML=renderTable(total,`동일 설정 ${matching.length}개 날짜 누적 · 거래가 0이면 승률 표시 불가`)+(matching.length?`<p class="hint">누적 날짜: ${matching.map(x=>esc(x.date)).join(', ')}</p>`:'')}
async function getJson(url,opts){const c=new AbortController(),timer=setTimeout(()=>c.abort(),120000);try{const r=await fetch(url,{...opts,signal:c.signal,cache:'no-store'}),v=await r.json();if(!r.ok||!v.ok)throw Error(v.error||`HTTP ${r.status}`);return v}finally{clearTimeout(timer)}}
const universeKey=n=>`bithumb_v1114_fixed_sample_${n}`;
async function frozenSample(n){let stored=null;try{stored=JSON.parse(localStorage.getItem(universeKey(n))||'null')}catch{};if(Array.isArray(stored)&&stored.length===n&&stored.every(x=>/^KRW-[A-Z0-9]+$/.test(x)))return stored;
const u=await getJson('/api/winrate14/universe?count='+n);try{localStorage.setItem(universeKey(n),JSON.stringify(u.items))}catch{throw Error('비교 종목 고정 저장 실패. 브라우저 저장 기능을 확인하세요')};return u.items;}
async function workerPool(groups,workers,fn){let i=0;let errors=[];async function work(){while(i<groups.length){const batch=groups[i++];try{await fn(batch)}catch(e){errors.push(String(e))}}}await Promise.all(Array.from({length:Math.min(workers,groups.length)},work));return errors}
async function run(){if(busy)return;busy=true;$('run').disabled=true;$('diagCsv').disabled=true;diagRows=[];$('densityResults').textContent='조회 중';$('firstOnlyResults').textContent='조회 중';$('diagSummary').textContent='조회 중';$('diagRows').textContent='조회 중';let config,failed=0,processed=0,totalBars=0,signals=0,unresolved=0,truncated=0,eligible=0,returned=[];const start=performance.now();try{config=currentCfg();const names=await frozenSample(config.count);config.sample=names.join('|');const batches=[];for(let i=0;i<names.length;i+=2)batches.push(names.slice(i,i+2));$('progress').max=names.length;$('progress').value=0;$('status').textContent=`과거 ${config.date} · 표본 ${names.length}개 캔들 조회 중 (일반 실시간 스캔보다 오래 걸릴 수 있습니다)`;
const errors=await workerPool(batches,2,async namesBatch=>{const r=await getJson('/api/winrate14/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...config,markets:namesBatch})});processed+=r.scanned;failed+=r.errors;for(const item of r.results){totalBars+=item.bars||0;signals+=item.candidates||0;unresolved+=item.missing_outcome||0;eligible+=item.valid_windows>0?1:0;truncated+=item.truncated?1:0;diagRows.push(item);returned.push(...(item.trades||[]))}$('progress').value=processed;$('status').textContent=`조회 ${processed}/${names.length}개 · 조건 발생 ${signals}건 · 판정 ${returned.length}건 · API 오류 ${failed}건`;});if(errors.length||failed||processed!==names.length)throw Error(`일부 API 실패: ${errors.length}개 배치 / ${failed}개 코인. 불완전한 데이터는 승률로 저장하지 않습니다.`);
allTrades=returned;latest=config;renderCoverageSplits();const s=statsOf(allTrades);$('table').innerHTML=renderTable(s,`${esc(config.date)} · 전체 ${names.length}개 표본 · 총 ${allTrades.length}건 판정 가능 거래`);
$('quality').innerHTML=[['검사 대상',names.length+'개'],['검사 가능 종목',eligible+'개'],['해당 날짜 5분봉',totalBars+'개'],['실험 신호',signals+'건'],['결과 미판정(봉 공백 등)',unresolved+'건'],['조회 5페이지 상한 도달',truncated+'개']].map(x=>metric(...x)).join('');
const labels={NO_DAY_BARS:'당일 5분봉 0개',TOO_FEW_FETCHED:'과거 준비·미래 판정 봉 부족',NO_25_CONSECUTIVE:'연속 25봉 없음',NO_VALID_WINDOW:'연속 구간은 있으나 유효 분석창 없음',NO_A_B_C_SIGNAL:'연속봉 확보 · 진입 모양 없음',OUTCOMES_UNKNOWN:'신호 있음 · 판정 불가',TRADE_EVALUATED:'모의 거래 평가 가능'};
const reasons={};for(const x of diagRows){const k=x.diag_code||'OTHER';reasons[k]=(reasons[k]||0)+1;}
$('diagSummary').innerHTML=Object.entries(labels).map(([code,name])=>metric(name,(reasons[code]||0)+'개')).join('')+metric('기타/분류 불가',(reasons.OTHER||0)+'개');
$('diagRows').innerHTML='<table><thead><tr><th>종목</th><th>구분</th><th>당일 봉</th><th>최장 연속</th><th>유효창</th><th>판정 거래</th><th>페이지한계</th></tr></thead><tbody>'+diagRows.slice().sort((a,b)=>(a.valid_windows>0?1:0)-(b.valid_windows>0?1:0)||a.market.localeCompare(b.market)).map(x=>`<tr><td>${esc(x.market)}</td><td>${esc(labels[x.diag_code]||'기타')}</td><td>${esc(x.bars||0)}/288</td><td>${esc(x.longest_day_run||0)}봉</td><td>${esc(x.valid_windows||0)}</td><td>${esc(x.decided_trades||0)}</td><td>${x.truncated?'예':'아니오'}</td></tr>`).join('')+'</tbody></table>';
$('diagCsv').disabled=false;
$('examples').innerHTML=allTrades.length?allTrades.slice(0,40).map(x=>`<div class="detail"><b>${esc(x.market)} · ${esc(strategies[x.strategy])} · ${esc(x.outcome)}</b>신호 확정 ${esc(x.time)} · 진입 ${esc(x.entry)}원 · 청산 ${esc(x.exit)}원 · 순손익 ${Number(x.net_pct).toFixed(2)}% · 보유 ${x.minutes}분</div>`).join(''):'<p class="muted">검증된 거래가 없습니다. 0% 승률로 취급하지 마세요.</p>';
const hist=readHist().filter(x=>!(x.date===config.date&&keyOf(x)===keyOf(config)));hist.push({...config,stats:s,recorded:new Date().toISOString()});const stored=writeHist(hist);displayHistory();const sec=((performance.now()-start)/1000).toFixed(1);$('status').textContent=`✅ ${config.date} · ${processed}개 표본 · ${sec}초 · 판정 가능 ${allTrades.length}거래 · 미판정 ${unresolved}건${stored?'':' · 브라우저 기록 저장 실패'}. 실전 승률로 오해하지 마세요.`;
}catch(e){$('status').textContent='⚠️ 검증 실패 · '+String(e.message||e)+' · 일부 결과로 승률을 계산하지 않습니다.';$('table').textContent='미완료 · 신뢰 가능한 승률 없음'}finally{busy=false;$('run').disabled=false}}
$('run').addEventListener('click',run);$('csv').onclick=()=>{const rows=readHist(),head=['date','count','tp','sl','horizon','fee','slip','strategy','trades','tp_count','sl_count','time_count','wins','net_pct_sum'];const lines=[head.join(',')];for(const r of rows)for(const id of Object.keys(strategies)){const s=r.stats[id]||emptyStat();lines.push([r.date,r.count,r.tp,r.sl,r.horizon,r.fee,r.slip,id,s.trades,s.tp,s.sl,s.time,s.wins,s.net].join(','))}const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='bithumb_winrate_lab_v1114.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
$('diagCsv').onclick=()=>{if(!diagRows.length)return;const head=['market','reason_code','observed_day_5m','missing_day_5m_slots','longest_day_run','longest_fetched_run','valid_windows','signals','unknown_outcomes','decided_trades','pagination_limit'];const data=[head.join(',')];for(const x of diagRows.slice().sort((a,b)=>a.market.localeCompare(b.market))){data.push([x.market,x.diag_code,x.bars,x.missing_5m_slots,x.longest_day_run,x.longest_history_run,x.valid_windows,x.signals,x.missing_outcome,x.decided_trades,x.truncated?1:0].join(','))}const blob=new Blob(['\ufeff'+data.join('\n')],{type:'text/csv;charset=utf-8'}),u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download='bithumb_winrate_data_diagnosis_v11141.csv';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000)};
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

    @app.post("/api/winrate14/audit-three")
    def winrate_lab_audit_three():
        try:
            payload = request.get_json(silent=True)
            cfg = lab_valid_request(payload, kst)
            targets = ("KRW-ONDO", "KRW-AEON", "KRW-MEGA")
            with sample_lock:
                if sample_cache["markets"] is None:
                    sample_cache.update(markets=sorted({m for m in get_markets()
                        if isinstance(m, str) and MARKET_RE.fullmatch(m)}), at=time.time())
                allowed = set(sample_cache["markets"] or [])
            missing = [m for m in targets if m not in allowed]
            if missing:
                raise ValueError("현재 KRW 마켓에 없는 교차확인 종목: "+", ".join(missing))
            results, errors = [], 0
            with ThreadPoolExecutor(max_workers=3) as pool:
                tasks = {pool.submit(lab_audit_gap_with_minutes,api_get,m,cfg,kst):m
                         for m in targets}
                for f in as_completed(tasks):
                    try:
                        results.append(f.result())
                    except Exception:
                        errors += 1
            return jsonify({"ok": errors == 0, "results": sorted(results,key=lambda x:x["market"]),
                            "scanned":len(results),"errors":errors})
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


# ---------------------------------------------------------------------------
# V11.14.3: FULL current-listed Bithumb KRW universe, historical C ONLY.
# Does not change any existing V11.13/V11.14.2 routes, algorithms, or page.
# The new route evaluates C by calling EXACTLY the existing lab_signals,
# lab_outcome, lab_fetch_5m and lab_contiguous logic.
# ---------------------------------------------------------------------------

def lab_fullc_evaluate_symbol(api_get, market, cfg, kst):
    rows, truncated = lab_fetch_5m(api_get, market, cfg, kst)
    day_start = datetime.combine(cfg["day"], _time.min, tzinfo=kst)
    day_end = day_start + timedelta(days=1)
    day_rows = [r for r in rows if day_start <= r["t"] < day_end]
    n = cfg["bars"]
    valid = 0
    signals = 0
    unknown = 0
    cooldown = -1
    trades = []
    first_signal = None  # The ACTUAL earliest signal; never replace an UNKNOWN with a later winner.
    if len(rows) >= LAB_WARMUP_BARS + n:
        for i in range(LAB_WARMUP_BARS, len(rows)-1):
            t = rows[i]["t"]
            if not day_start <= t < day_end:
                continue
            if not lab_contiguous(rows, i-24, i):
                continue
            valid += 1
            if i < cooldown:
                continue
            if "C" not in lab_signals(rows, i):
                continue
            cooldown = i + n  # Same original C cooldown; unrelated A/B signals do not affect it.
            signals += 1
            outcome = lab_outcome(rows, i, cfg)
            entry_time = (t + timedelta(minutes=5)).strftime("%m-%d %H:%M")
            event = {"time": entry_time, "outcome": outcome["outcome"],
                     "net_pct": outcome.get("net_pct"),
                     "entry": outcome.get("entry"),
                     "minutes": outcome.get("minutes")}
            if first_signal is None:
                first_signal = event
            if outcome["outcome"] == "UNKNOWN":
                unknown += 1
            else:
                trades.append(event)
    return {"market": market, "observed_5m": len(day_rows),
            "longest_day_run": lab_longest_consecutive_5m(day_rows),
            "valid_windows": valid, "signals": signals, "unknown": unknown,
            "truncated": truncated, "first_signal": first_signal,
            "trades": trades}


FULL_C_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.3 · 전체 KRW C전략 검증</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#1a2038;--card:#121a2e;--border:#35425f;--fg:#f2f5ff;--muted:#afc0db;--blue:#3867ee;--green:#4ade80;--warn:#ffc46c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1050px;margin:auto;padding:18px 14px 60px}h1{font-size:23px;margin:8px 0}h2{font-size:18px;margin:0 0 8px}
.panel{border:1px solid var(--border);background:var(--panel);border-radius:16px;padding:16px;margin-top:15px}
.muted,.hint{font-size:13px;color:var(--muted)}.note{padding:12px;background:#101a30;border-radius:10px;font-size:13px;line-height:1.75}
.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.field{display:flex;flex-direction:column;font-size:13px;color:var(--muted);gap:5px}
input,button,select{font:inherit}input,select{padding:10px;background:#10182d;border:1px solid var(--border);color:var(--fg);border-radius:8px;width:100%;min-width:0}
button{background:var(--blue);border:0;border-radius:9px;color:#fff;padding:12px 14px;font-weight:800;cursor:pointer}button:disabled{opacity:.48}.secondary{background:#364660}
.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px;align-items:center}a{color:#bcd0ff}progress{width:100%;height:15px;margin-top:14px}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.stat{border:1px solid var(--border);background:var(--card);border-radius:10px;padding:11px}
.stat small{font-size:12px;color:var(--muted);display:block}.stat strong{font-size:22px;display:block}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--border);padding:9px 7px;text-align:right;white-space:nowrap}td:first-child,th:first-child{text-align:left}
.status{font-size:13px;white-space:pre-wrap;color:var(--muted)}.good{color:var(--green)}.warn{color:var(--warn)}
@media(max-width:650px){.controls{grid-template-columns:repeat(2,minmax(0,1fr))}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><main>
<h1>📊 V11.14.3 · 전체 KRW C전략 승률 검증</h1>
<p class="muted">전체 <b>현재 상장된</b> 빗썸 KRW 종목의 과거 하루를 확인합니다. V11.13 실시간 BUY 전략이 아니라 V11.14의 <b>실험 C</b> 신호입니다. 기존 V11.14.2와 V2는 변경되지 않습니다.</p>
<section class="panel"><h2>검증 조건</h2>
<div class="controls">
<label class="field">완료된 과거 날짜 (KST)<input id="date" type="date"></label>
<label class="field">익절 목표 (%)<input id="tp" type="number" min="0.3" max="10" step="0.1" value="1.5"></label>
<label class="field">손절 기준 (%)<input id="sl" type="number" min="0.2" max="8" step="0.1" value="1.0"></label>
<label class="field">최대 보유<select id="horizon"><option value="30">30분</option><option value="60" selected>60분</option><option value="120">120분</option></select></label>
<label class="field">매수·매도 각각 수수료 (%)<input id="fee" type="number" step="0.01" value="0.04" min="0" max="0.5"></label>
<label class="field">매수·매도 각각 슬리피지 (%)<input id="slip" type="number" step="0.01" value="0.05" min="0" max="0.5"></label>
</div>
<p class="hint">기존 C 조건과 익절·손절·수수료 산식을 그대로 사용합니다. 다음 5분봉 시가에 모의 진입하고 같은 봉 TP·SL 동시 접촉 시 SL 우선, 봉 공백은 미판정입니다.</p>
<div class="actions"><button id="run">전체 KRW 검사 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">현재 진행 기록 초기화</button></div>
<progress id="progress" value="0" max="1"></progress>
<p id="status" class="status">검사 전 · 실제 전체 시장 승률 없음</p>
<p class="hint">전체 코인은 다수의 API 요청이 필요해 시간이 걸릴 수 있습니다. 이 화면을 열어 두고 검사하세요. 배치별 결과를 브라우저에 저장하므로 중간에 끊기면 같은 설정으로 이어할 수 있습니다. 오류가 나면 원인을 해결한 뒤 이어할 수 있으며, 완료 전 승률은 공개하지 않습니다.</p>
</section>
<section class="panel"><h2>전체 시장 C 신호: 검증 완료 시 공개</h2>
<div class="stats" id="summary"><div class="stat"><small>상태</small><strong>검사 전</strong></div></div>
<p class="hint">하루의 C 신호 수와 <b>실제로 판정 가능한 수</b>를 구별합니다. 같은 코인에서 여러 C 신호가 반복될 수 있으므로 <b>코인당 첫 신호</b>와 <b>전체 C 거래</b>를 함께 보여줍니다. 최초 신호가 미판정이면 뒤의 수익 신호로 대체하지 않습니다.</p></section>
<section class="panel"><h2>전 종목의 데이터 상태</h2><div id="coverage" class="stats"><div class="stat">검사 전</div></div>
<p class="hint">5분봉 240개 이상 / 120~239개 / 0~119개를 구분합니다. 검증 기준은 바꾸지 않고 관측 밀도만 구분합니다.</p></section>
<section class="panel"><h2>종목별 C 신호·손익</h2>
<p class="hint">정확한 결과는 전체 종목 검사가 끝나야 표시됩니다. 고정된 코인 목록과 검사 설정은 해당 진행 기록에 함께 보관합니다.</p>
<div id="byMarket" class="scroll">검사 전</div>
<div class="actions"><button id="csv" disabled>전체 C 전략 거래 CSV 저장</button></div></section>
<section class="panel"><h2>주의사항</h2>
<div class="note">현재 상장 중인 종목 전체에 과거 날짜를 적용합니다. 과거에 상장 폐지된 종목은 제외되어 <b>생존자 편향</b>이 있습니다. C의 1시간 흐름은 실제 1시간봉 데이터가 아니라 <b>과거 5분봉 24개로 근사한 흐름</b>입니다. 모든 결과는 과거 모의 평가이며 실제 체결, 호가 스프레드, 가격 단위, 미래 시장 상황이 보장되지 않습니다. 한 종목의 반복 신호는 서로 독립적이지 않을 수 있습니다.</div>
<p><a href="/backtest-winrate">V11.14.2 기존 비교·진단판</a> · <a href="/backtest-daytrade">V11.13 실시간 스캐너</a> · <a href="/">V2 메인</a></p></section>
</main><script>
const $=id=>document.getElementById(id);
const escapeHtml=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const KEY='bithumb_v11143_full_c_checkpoint_1';
let state=null, running=false, stopRequested=false, hardFailure=false;
function kstDate(dt){let p=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(dt);let v=k=>p.find(x=>x.type===k).value;return `${v('year')}-${v('month')}-${v('day')}`;}
const yesterday=()=>kstDate(new Date(Date.now()-86400000));
$('date').max=yesterday();$('date').value=yesterday();
function cfgOf(){let c={date:$('date').value,tp:+$('tp').value,sl:+$('sl').value,horizon:+$('horizon').value,fee:+$('fee').value,slip:+$('slip').value};
 if(!/^\d{4}-\d{2}-\d{2}$/.test(c.date)||c.date>yesterday())throw Error('완료된 과거 날짜만 선택하세요');
 if(![c.tp,c.sl,c.horizon,c.fee,c.slip].every(Number.isFinite)||c.tp<0.3||c.tp>10||c.sl<0.2||c.sl>8||![30,60,120].includes(c.horizon)||c.fee<0||c.fee>0.5||c.slip<0||c.slip>0.5)throw Error('검증 설정 범위를 확인하세요');
 return c;
}
const signature=c=>JSON.stringify([c.date,c.tp,c.sl,c.horizon,c.fee,c.slip]);
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function load(){try{const x=JSON.parse(localStorage.getItem(KEY)||'null');if(!x||x.version!==1||!Array.isArray(x.markets)||!x.config||!x.results||typeof x.results!=='object')return null;
 if(x.markets.length<1||x.markets.some(m=>!/^KRW-[A-Z0-9]+$/.test(m))||new Set(x.markets).size!==x.markets.length)return null;
 return x;}catch{return null}}
function setConfig(c){for(const id of ['date','tp','sl','horizon','fee','slip'])$(id).value=c[id];}
function numPct(n){return Number.isFinite(n)?n.toFixed(2)+'%':'—';}
function card(name,val){return `<div class="stat"><small>${escapeHtml(name)}</small><strong>${escapeHtml(val)}</strong></div>`;}
function sampleResults(){return state?state.markets.filter(m=>Object.prototype.hasOwnProperty.call(state.results,m)).map(m=>state.results[m]):[];}
function aggregate(events){const n=events.length;let tp=0,sl=0,time=0,wins=0,net=0;
 for(const x of events){if(x.outcome==='TP')tp++;else if(x.outcome==='SL')sl++;else if(x.outcome==='TIME')time++;
 let v=Number(x.net_pct);if(Number.isFinite(v)){net+=v;if(v>0)wins++;}}
 return {n,tp,sl,time,wins,net,winPct:n?100*wins/n:null,avg:n?net/n:null};}
function paint(){let a=sampleResults(),n=a.length,total=state?.markets.length||0,complete=total>0&&n===total&&state?.failed===0;
 $('progress').max=total||1;$('progress').value=n;
 let signals=a.reduce((s,x)=>s+x.signals,0),unknown=a.reduce((s,x)=>s+x.unknown,0),trades=a.flatMap(x=>x.trades||[]),first=a.map(x=>x.first_signal).filter(Boolean),firstDecided=first.filter(x=>x.outcome!=='UNKNOWN');
 let eligible=a.filter(x=>x.valid_windows>0).length;
 const covered=`${n}/${total}`;
 $('coverage').innerHTML=card('조회된 종목 / 전체',covered)+card('분석 가능한 코인',eligible+'개')+card('확인된 C 신호',signals+'건')+card('승패 미판정 C 신호',unknown+'건')+
  card('해당 날짜 5분봉 240개 이상',a.filter(x=>x.observed_5m>=240).length+'개')+card('120~239개',a.filter(x=>x.observed_5m>=120&&x.observed_5m<240).length+'개')+
  card('0~119개',a.filter(x=>x.observed_5m<120).length+'개')+card('5페이지 상한 도달',a.filter(x=>x.truncated).length+'개');
 if(!complete){$('summary').innerHTML=card('전체 시장 결과','검사 미완료')+card('진행',covered)+card('최종 순승률','표시 보류')+card('최종 평균 순손익','표시 보류');$('byMarket').textContent='전체 코인 검사 완료 후 결과가 공개됩니다.';$('csv').disabled=true;}
 else {let s=aggregate(trades),f=aggregate(firstDecided);
   $('summary').innerHTML=card('전체 KRW 검사',total+'개 완료')+card('C 첫 신호 발생 코인',first.length+'개')+
    card('첫 신호 중 판정 가능',f.n+'개')+card('첫 신호 순승률',f.n?numPct(f.winPct):'—')+
    card('첫 신호 평균 순손익',f.n?numPct(f.avg):'—')+card('첫 신호 TP / SL / 시간종료',`${f.tp} / ${f.sl} / ${f.time}`)+
    card('전체 C 판정 거래',s.n+'건')+card('전체 C 순승률',s.n?numPct(s.winPct):'—')+
    card('전체 C 평균 순손익',s.n?numPct(s.avg):'—')+card('전체 C TP / SL / 시간종료',`${s.tp} / ${s.sl} / ${s.time}`)+
    card('최초 신호 미판정',first.length-f.n+'개 코인')+card('C 신호 없는 종목',total-first.length+'개');
   let rows=a.filter(x=>x.signals>0).sort((x,y)=>x.market.localeCompare(y.market));
   $('byMarket').innerHTML=rows.length?`<table><thead><tr><th>코인</th><th>당일 5분봉</th><th>C 신호</th><th>판정</th><th>미판정</th><th>첫 신호</th><th>첫 신호 순손익</th><th>전체 평균 순손익</th></tr></thead><tbody>`+
      rows.map(x=>{let f=x.first_signal,t=aggregate(x.trades||[]);return `<tr><td>${escapeHtml(x.market)}</td><td>${x.observed_5m}/288</td><td>${x.signals}</td><td>${x.trades.length}</td><td>${x.unknown}</td><td>${escapeHtml(f?.outcome||'—')}</td><td>${f?.net_pct==null?'—':numPct(+f.net_pct)}</td><td>${t.n?numPct(t.avg):'—'}</td></tr>`}).join('')+'</tbody></table>':'C 신호 없음';
   $('csv').disabled=false;
 }
 if(state){let status=(complete?'✅ 전체 시장 검사 완료':'⏳ 전체 시장 미완료')+` · ${covered}개 · C 신호 ${signals}건 · 미판정 ${unknown}건`;
  if(state.failed)status+=' · 실패 배치가 있어 재개 필요';
  $('status').textContent=status+(complete?'\n이 결과는 과거 모의 성적이며 실전 승률이 아닙니다.':'\n검사가 끝나기 전에는 일부 종목 승률을 전체 시장 승률처럼 표시하지 않습니다.');
 }else $('status').textContent='검사 전 · 전체 시장 승률 없음';
}
async function req(url,opts){const ctl=new AbortController(),tid=setTimeout(()=>ctl.abort(),115000);
 try{const r=await fetch(url,{...opts,cache:'no-store',signal:ctl.signal});const v=await r.json();if(!r.ok||!v.ok)throw Error(v.error||'HTTP '+r.status);return v;}finally{clearTimeout(tid)}}
async function initSession(cfg){let v=await req('/api/winrate14c/universe');if(!Array.isArray(v.items)||!v.items.length)throw Error('현재 KRW 목록이 비었습니다');
 state={version:1,config:cfg,markets:v.items,results:{},failed:0};save();}
async function run(){if(running)return;
 let cfg;try{cfg=cfgOf()}catch(e){$('status').textContent='⚠️ '+e.message;return;}
 if(state&&signature(state.config)!==signature(cfg)){
   if(!confirm('설정/날짜가 달라졌습니다. 기존 전체 시장 검사 진행 기록을 지우고 새로 시작할까요?'))return;
   state=null;localStorage.removeItem(KEY);
 }
 running=true;stopRequested=false;hardFailure=false;
 $('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;
 try{
   if(!state)await initSession(cfg);
   state.failed=0;save();paint();
   let pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m));
   if(!pending.length){paint();return;}
   const batches=[];for(let i=0;i<pending.length;i+=2)batches.push(pending.slice(i,i+2));
   let pos=0,fail=0;async function worker(){while(!stopRequested&&!hardFailure){const b=batches[pos++];if(!b)break;
      let response=null;for(let attempt=0;attempt<3&&!stopRequested;attempt++){
       try{const v=await req('/api/winrate14c/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:b})});
          if(v.errors||v.scanned!==b.length||!Array.isArray(v.results)||v.results.length!==b.length||v.results.some(x=>!b.includes(x.market)))throw Error('불완전한 배치 응답');
          response=v;break;
       }catch(e){if(attempt===2){fail++;$('status').textContent=`⚠️ ${b.join(', ')} 조회 실패: ${String(e.message||e)}. 중단 후 이어하기로 재검사 가능합니다.`;}
       else await new Promise(ok=>setTimeout(ok,1000*(attempt+1)));}
      }
      if(response){for(const x of response.results)state.results[x.market]=x;
        try{save();}catch(e){hardFailure=true;$('status').textContent='⚠️ 브라우저 저장공간 부족: 진행 기록을 보존할 수 없습니다.';break;}
        paint();
      }
      if(fail>=4){hardFailure=true;break;}
   }}
   await Promise.all([worker(),worker()]);
   state.failed=fail;try{save();}catch{}paint();
   if(stopRequested&&!hardFailure)$('status').textContent+='\n사용자가 일시 중지했습니다. 다시 버튼을 누르면 이어서 검사합니다.';
   if(fail)$('status').textContent+=`\n실패 배치 ${fail}개 · 완료되지 않은 종목은 이어하기 시 다시 검사합니다.`;
 }catch(e){$('status').textContent='⚠️ '+String(e.message||e)+' · 미완료 결과는 승률로 집계하지 않습니다.';}
 finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}
}
$('run').onclick=run;
$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;$('status').textContent+='\n진행 중인 배치만 마친 뒤 중지합니다.'};
$('reset').onclick=()=>{if(running)return;if(!confirm('현재 전체 KRW C 검사 진행 기록을 삭제할까요? 기존 V11.14.2 누적 기록에는 영향을 주지 않습니다.'))return;
 state=null;localStorage.removeItem(KEY);paint();};
function csvCell(v){const s=String(v??'');return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!state||sampleResults().length!==state.markets.length||state.failed)return;
 let lines=[['date','market','observed_day_5m','c_signals','unknown_c','signal_time','outcome','entry','minutes','net_pct','is_first_signal'].join(',')];
 for(const x of sampleResults()){
  let first=x.first_signal;
  if(!x.signals){lines.push([state.config.date,x.market,x.observed_5m,0,0,'','NO_SIGNAL','','','',''].map(csvCell).join(','));continue;}
  const events=x.trades||[];
  for(const ev of events)lines.push([state.config.date,x.market,x.observed_5m,x.signals,x.unknown,ev.time,ev.outcome,ev.entry,ev.minutes,ev.net_pct,first?.time===ev.time?1:0].map(csvCell).join(','));
  if(first?.outcome==='UNKNOWN')lines.push([state.config.date,x.market,x.observed_5m,x.signals,x.unknown,first.time,'UNKNOWN','','','','1'].map(csvCell).join(','));
 }
 const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download=`bithumb_full_krw_C_${state.config.date}_v11143.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);
};
state=load();if(state)setConfig(state.config);paint();
</script></body></html>'''


def register_full_c_lab(app, deps):
    api_get, get_markets, kst = deps["api_get"], deps["get_markets"], deps["KST"]
    current = {"at": 0, "names": None}
    mu = Lock()

    def current_names():
        with mu:
            if current["names"] is None or time.time() - current["at"] > 600:
                names = sorted({m for m in get_markets() if isinstance(m, str) and MARKET_RE.fullmatch(m)})
                if not names:
                    raise ValueError("빗썸 KRW 종목 목록이 비었습니다")
                current.update(names=names, at=time.time())
            return current["names"]

    @app.get("/backtest-winrate-full-c")
    def full_c_page_11143():
        return render_template_string(FULL_C_PAGE)

    @app.get("/api/winrate14c/universe")
    def full_c_universe_11143():
        try:
            names = current_names()
            return jsonify({"ok": True, "items": names, "count": len(names),
                            "universe_type": "current_KRW_listings"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:160]}), 400

    @app.post("/api/winrate14c/evaluate")
    def full_c_evaluate_11143():
        try:
            payload = request.get_json(silent=True)
            cfg = lab_valid_request(payload, kst)
            markets = payload.get("markets") if isinstance(payload, dict) else None
            if (not isinstance(markets, list) or not 1 <= len(markets) <= 2 or
                len(markets) != len(set(markets)) or
                any(not isinstance(m, str) or not MARKET_RE.fullmatch(m) for m in markets)):
                raise ValueError("한 번에 1~2개 KRW 종목만 조회할 수 있습니다")
            names = set(current_names())
            if not all(m in names for m in markets):
                raise ValueError("현재 빗썸 KRW 목록에서 찾을 수 없는 종목입니다")
            results, errors = [], []
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {pool.submit(lab_fullc_evaluate_symbol, api_get, m, cfg, kst): m for m in markets}
                for fut in as_completed(futures):
                    try:
                        results.append(fut.result())
                    except Exception as e:
                        errors.append({"market": futures[fut], "error": str(e)[:120]})
            return jsonify({"ok": len(errors) == 0, "scanned": len(markets),
                            "results": results, "errors": len(errors),
                            "error_markets": errors})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:160]}), 400


_v11142_register_daytrade_review = register_daytrade_review

def register_daytrade_review(app, deps):
    _v11142_register_daytrade_review(app, deps)
    register_full_c_lab(app, deps)


# ---------------------------------------------------------------------------
# V11.14.4: separate C-strategy loss-diagnostics lab (zero rule modifications).
# V11.14.3 existing endpoints, page, and browser checkpoint remain intact.
# ---------------------------------------------------------------------------

def lab_c_signal_features(rows, i):
    """Feature snapshot using only 5m bars available at C signal close."""
    c = rows[i]
    prev20 = rows[i-20:i]
    avgvol = sum(x['v'] for x in prev20) / len(prev20)
    ma_old = sum(x['c'] for x in rows[i-24:i-12]) / 12
    ma_recent = sum(x['c'] for x in rows[i-12:i]) / 12
    ma10 = sum(x['c'] for x in rows[i-10:i]) / 10
    span = max(1e-12, c['h'] - c['l'])
    def r(x):
        return round(x, 4) if isfinite(x) else None
    return {
        'volume_ratio': r(c['v'] / avgvol) if avgvol > 0 else None,
        'body_pct': r(100 * (c['c'] / c['o'] - 1)),
        'body_share_pct': r(100 * (c['c'] - c['o']) / span),
        'flow_1h_proxy_pct': r(100 * (ma_recent / ma_old - 1)) if ma_old > 0 else None,
        'pullback_depth_pct': r(100 * (ma10 - c['l']) / ma10) if ma10 > 0 else None,
        'reclaim_ma10_pct': r(100 * (c['c'] / ma10 - 1)) if ma10 > 0 else None,
        'prior_15m_pct': r(100 * (c['c'] / rows[i-3]['c'] - 1)),
        'prior_30m_pct': r(100 * (c['c'] / rows[i-6]['c'] - 1)),
    }


def lab_cdiag_evaluate_symbol(api_get, market, cfg, kst, outcome_fn=lab_outcome, require_full_horizon=True):
    """Keep EXACT C signal, cooldown, and outcome rules of V11.14.3.

    Store all signal events, including *every* UNKNOWN (not only first).
    Extra features are descriptive, not additional C qualification gates.
    """
    rows, truncated = lab_fetch_5m(api_get, market, cfg, kst)
    day_start = datetime.combine(cfg['day'], _time.min, tzinfo=kst)
    day_end = day_start + timedelta(days=1)
    day_rows = [r for r in rows if day_start <= r['t'] < day_end]
    n, valid, signals, unknown, cooldown = cfg['bars'], 0, 0, 0, -1
    events, trades, first_signal = [], [], None
    if len(rows) >= LAB_WARMUP_BARS + (n if require_full_horizon else 2):
        for i in range(LAB_WARMUP_BARS, len(rows)-1):
            t = rows[i]['t']
            if not day_start <= t < day_end:
                continue
            if not lab_contiguous(rows, i-24, i):
                continue
            valid += 1
            if i < cooldown:
                continue
            if 'C' not in lab_signals(rows, i):
                continue
            cooldown = i + n
            signals += 1
            outcome = outcome_fn(rows, i, cfg)
            entry_time = t + timedelta(minutes=5)
            event = {
                'time': entry_time.strftime('%m-%d %H:%M'),
                'signal_kst': entry_time.strftime('%Y-%m-%d %H:%M'),
                'outcome': outcome['outcome'],
                'net_pct': outcome.get('net_pct'),
                'entry': outcome.get('entry'),
                'exit': outcome.get('exit'),
                'minutes': outcome.get('minutes'),
                'reason': outcome.get('reason'),
                'is_first_signal': first_signal is None,
                'features': lab_c_signal_features(rows, i),
            }
            if first_signal is None:
                # The true first signal is kept even when its result is UNKNOWN.
                first_signal = {k: event[k] for k in ('time','outcome','net_pct','entry','minutes')}
            events.append(event)
            if outcome['outcome'] == 'UNKNOWN':
                unknown += 1
            else:
                trades.append(event)
    assert len(events) == signals and len(trades) + unknown == signals
    return {
        'market': market, 'observed_5m': len(day_rows),
        'longest_day_run': lab_longest_consecutive_5m(day_rows),
        'valid_windows': valid, 'signals': signals, 'unknown': unknown,
        'truncated': truncated, 'first_signal': first_signal,
        'trades': trades, 'events': events,
    }


C_DIAG_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.4 · 전체 KRW C 전략 손절 원인 진단</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#1a2038;--card:#121a2e;--border:#35425f;--fg:#f2f5ff;--muted:#afc0db;--blue:#3867ee;--green:#4ade80;--warn:#ffc46c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1050px;margin:auto;padding:18px 14px 60px}h1{font-size:23px;margin:8px 0}h2{font-size:18px;margin:0 0 8px}
.panel{border:1px solid var(--border);background:var(--panel);border-radius:16px;padding:16px;margin-top:15px}
.muted,.hint{font-size:13px;color:var(--muted)}.note{padding:12px;background:#101a30;border-radius:10px;font-size:13px;line-height:1.75}
.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.field{display:flex;flex-direction:column;font-size:13px;color:var(--muted);gap:5px}
input,button,select{font:inherit}input,select{padding:10px;background:#10182d;border:1px solid var(--border);color:var(--fg);border-radius:8px;width:100%;min-width:0}
button{background:var(--blue);border:0;border-radius:9px;color:#fff;padding:12px 14px;font-weight:800;cursor:pointer}button:disabled{opacity:.48}.secondary{background:#364660}
.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px;align-items:center}a{color:#bcd0ff}progress{width:100%;height:15px;margin-top:14px}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.stat{border:1px solid var(--border);background:var(--card);border-radius:10px;padding:11px}
.stat small{font-size:12px;color:var(--muted);display:block}.stat strong{font-size:22px;display:block}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--border);padding:9px 7px;text-align:right;white-space:nowrap}td:first-child,th:first-child{text-align:left}
.status{font-size:13px;white-space:pre-wrap;color:var(--muted)}.good{color:var(--green)}.warn{color:var(--warn)}
@media(max-width:650px){.controls{grid-template-columns:repeat(2,minmax(0,1fr))}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><main>
<h1>📊 V11.14.4 · 전체 KRW C 전략 손절 원인 진단</h1>
<p class="muted">전체 <b>현재 상장된</b> 빗썸 KRW 종목에서 발생한 실험 C 신호의 <b>진입 당시 특성</b>과 나중에 확인된 모의 손익을 비교합니다. A/B/C 진입식, TP/SL, V11.13 실시간 BUY와 기존 V11.14.3 화면은 바꾸지 않습니다. 신호/손익/진단은 실험 목적입니다.</p>
<section class="panel"><h2>검증 조건</h2>
<div class="controls">
<label class="field">완료된 과거 날짜 (KST)<input id="date" type="date"></label>
<label class="field">익절 목표 (%)<input id="tp" type="number" min="0.3" max="10" step="0.1" value="1.5"></label>
<label class="field">손절 기준 (%)<input id="sl" type="number" min="0.2" max="8" step="0.1" value="1.0"></label>
<label class="field">최대 보유<select id="horizon"><option value="30">30분</option><option value="60" selected>60분</option><option value="120">120분</option></select></label>
<label class="field">매수·매도 각각 수수료 (%)<input id="fee" type="number" step="0.01" value="0.04" min="0" max="0.5"></label>
<label class="field">매수·매도 각각 슬리피지 (%)<input id="slip" type="number" step="0.01" value="0.05" min="0" max="0.5"></label>
</div>
<p class="hint">기존 C 조건과 익절·손절·수수료 산식은 그대로 유지합니다. 신호 발생 시점까지의 <b>완료된 봉</b>으로만 거래량·양봉강도·상승 흐름·눌림 깊이를 기록합니다. 진입은 다음 5분봉 시가로 모의 처리하며 봉 공백은 미판정입니다.</p>
<div class="actions"><button id="run">전체 KRW 검사 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">현재 진행 기록 초기화</button></div>
<progress id="progress" value="0" max="1"></progress>
<p id="status" class="status">검사 전 · 실제 전체 시장 승률 없음</p>
<p class="hint">기존 V11.14.3의 489개 검사 결과에는 진입 당시 지표가 없으므로 <b>이 진단 화면에서는 데이터를 새로 조회</b>해야 합니다. 이 화면은 별도 브라우저 진행 기록을 사용하며, 중단 시 이어할 수 있습니다. 전체가 끝나기 전에는 최종 성적을 표시하지 않습니다.</p>
</section>
<section class="panel"><h2>전체 시장 C 신호: 검증 완료 시 공개</h2>
<div class="stats" id="summary"><div class="stat"><small>상태</small><strong>검사 전</strong></div></div>
<p class="hint">하루의 C 신호 수와 <b>실제로 판정 가능한 수</b>를 구별합니다. 같은 코인에서 여러 C 신호가 반복될 수 있으므로 <b>코인당 첫 신호</b>와 <b>전체 C 거래</b>를 함께 보여줍니다. 최초 신호가 미판정이면 뒤의 수익 신호로 대체하지 않습니다.</p></section>
<section class="panel"><h2>전 종목의 데이터 상태</h2><div id="coverage" class="stats"><div class="stat">검사 전</div></div>
<p class="hint">5분봉 240개 이상 / 120~239개 / 0~119개를 구분합니다. 검증 기준은 바꾸지 않고 관측 밀도만 구분합니다.</p></section>
<section class="panel"><h2>종목별 C 신호·손익</h2>
<p class="hint">정확한 결과는 전체 종목 검사가 끝나야 표시됩니다. 고정된 코인 목록과 검사 설정은 해당 진행 기록에 함께 보관합니다.</p>
<div id="byMarket" class="scroll">검사 전</div>
<div class="actions"><button id="csv" disabled>전체 C 신호·진단 CSV 저장 (미판정 포함)</button></div></section>
<section class="panel"><h2>진입 당시 지표 비교 (익절 / 손절 / 시간 종료)</h2>
<p class="hint">비교에 사용되는 모든 지표는 신호가 확정된 시점까지의 실제 5분봉만으로 계산합니다. <b>이후 청산 결과를 신호 조건에 섞지 않습니다.</b> 표본이 적거나 특정 코인에 편중되어 있으면 차이가 우연일 수 있습니다.</p>
<div id="riskSummary" class="stats">전체 시장 검사 후 공개</div>
<div id="featureGroups" class="scroll">전체 시장 검사 후 공개</div>
<p class="hint">숫자는 각 그룹의 <b>중앙값</b>입니다. 그룹의 차이는 상관관계일 뿐 매매 필터로 확정한 기준이 아닙니다. C 흐름 강도는 실제 1시간봉이 아니라 과거 5분봉 24개에서 계산한 근사치입니다.</p>
</section>
<section class="panel"><h2>빠른 손절과 반복 진입 분석</h2>
<div id="lossSummary">전체 시장 검사 후 공개</div>
</section>
<section class="panel"><h2>개별 손절 사례 · 신호 당시 조건</h2>
<p class="hint">손절에 빨리 도달한 사례부터 보여줍니다. 전체 신호와 UNKNOWN은 CSV에 모두 저장됩니다. 손절된 사례의 공통점은 단지 관찰 자료이며 미래 승률을 보장하지 않습니다.</p>
<div id="lossCases" class="scroll">전체 시장 검사 후 공개</div>
</section>
<section class="panel"><h2>주의사항</h2>
<div class="note">현재 상장 중인 종목 전체에 과거 날짜를 적용합니다. 과거에 상장 폐지된 종목은 제외되어 <b>생존자 편향</b>이 있습니다. C의 1시간 흐름은 실제 1시간봉 데이터가 아니라 <b>과거 5분봉 24개로 근사한 흐름</b>입니다. 모든 결과는 과거 모의 평가이며 실제 체결, 호가 스프레드, 가격 단위, 미래 시장 상황이 보장되지 않습니다. 한 종목의 반복 신호는 서로 독립적이지 않을 수 있습니다.</div>
<p><a href="/backtest-winrate-full-c">V11.14.3 기존 전체 시장 결과</a> · <a href="/backtest-winrate">V11.14.2 비교·진단판</a> · <a href="/backtest-daytrade">V11.13 실시간 스캐너</a> · <a href="/">V2 메인</a></p></section>
</main><script>
const $=id=>document.getElementById(id);
const escapeHtml=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const KEY='bithumb_v11144_c_diagnostics_checkpoint_1';
let state=null, running=false, stopRequested=false, hardFailure=false;
function kstDate(dt){let p=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(dt);let v=k=>p.find(x=>x.type===k).value;return `${v('year')}-${v('month')}-${v('day')}`;}
const yesterday=()=>kstDate(new Date(Date.now()-86400000));
$('date').max=yesterday();$('date').value=yesterday();
function cfgOf(){let c={date:$('date').value,tp:+$('tp').value,sl:+$('sl').value,horizon:+$('horizon').value,fee:+$('fee').value,slip:+$('slip').value};
 if(!/^\d{4}-\d{2}-\d{2}$/.test(c.date)||c.date>yesterday())throw Error('완료된 과거 날짜만 선택하세요');
 if(![c.tp,c.sl,c.horizon,c.fee,c.slip].every(Number.isFinite)||c.tp<0.3||c.tp>10||c.sl<0.2||c.sl>8||![30,60,120].includes(c.horizon)||c.fee<0||c.fee>0.5||c.slip<0||c.slip>0.5)throw Error('검증 설정 범위를 확인하세요');
 return c;
}
const signature=c=>JSON.stringify([c.date,c.tp,c.sl,c.horizon,c.fee,c.slip]);
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function load(){try{const x=JSON.parse(localStorage.getItem(KEY)||'null');if(!x||x.version!==2||!Array.isArray(x.markets)||!x.config||!x.results||typeof x.results!=='object')return null;
 if(x.markets.length<1||x.markets.some(m=>!/^KRW-[A-Z0-9]+$/.test(m))||new Set(x.markets).size!==x.markets.length)return null;
 return x;}catch{return null}}
function setConfig(c){for(const id of ['date','tp','sl','horizon','fee','slip'])$(id).value=c[id];}
function numPct(n){return Number.isFinite(n)?n.toFixed(2)+'%':'—';}
function card(name,val){return `<div class="stat"><small>${escapeHtml(name)}</small><strong>${escapeHtml(val)}</strong></div>`;}
function sampleResults(){return state?state.markets.filter(m=>Object.prototype.hasOwnProperty.call(state.results,m)).map(m=>state.results[m]):[];}
function aggregate(events){const n=events.length;let tp=0,sl=0,time=0,wins=0,net=0;
 for(const x of events){if(x.outcome==='TP')tp++;else if(x.outcome==='SL')sl++;else if(x.outcome==='TIME')time++;
 let v=Number(x.net_pct);if(Number.isFinite(v)){net+=v;if(v>0)wins++;}}
 return {n,tp,sl,time,wins,net,winPct:n?100*wins/n:null,avg:n?net/n:null};}

function med(events,key){const vals=events.map(e=>e.features?.[key]).filter(v=>typeof v==='number'&&Number.isFinite(v)).sort((a,b)=>a-b);if(!vals.length)return null;const n=vals.length,mid=n>>1;return n%2?vals[mid]:(vals[mid-1]+vals[mid])/2;}
function metricValue(n,suffix){return n==null?'—':n.toFixed(2)+(suffix||'');}
function renderRisk(results){
 const events=results.flatMap(x=>(x.events||[]).map(e=>({...e,market:x.market})));
 const decided=events.filter(e=>e.outcome!=='UNKNOWN'&&typeof e.net_pct==='number');
 const tp=decided.filter(e=>e.outcome==='TP'),sl=decided.filter(e=>e.outcome==='SL'),timed=decided.filter(e=>e.outcome==='TIME');
 const first=decided.filter(e=>e.is_first_signal),repeat=decided.filter(e=>!e.is_first_signal);
 const fast=sl.filter(e=>typeof e.minutes==='number'&&e.minutes<=10);
 const missing=events.filter(e=>e.outcome==='UNKNOWN');
 $('riskSummary').innerHTML=card('기록된 C 신호',events.length+'건')+card('판정 / 미판정',decided.length+' / '+missing.length+'건')+card('익절 / 손절 / 시간종료',`${tp.length} / ${sl.length} / ${timed.length}`)+card('손절 중 10분 이내',sl.length?`${fast.length}/${sl.length} (${(100*fast.length/sl.length).toFixed(1)}%)`:'—');
 const features=[['volume_ratio','신호봉 거래량 / 직전 20봉 평균','배'],['body_pct','신호봉 몸통 상승률','%'],['body_share_pct','양봉 몸통 / 전체 봉 범위','%'],['flow_1h_proxy_pct','1H 근사 흐름 강도','%'],['pullback_depth_pct','MA10 대비 저가 눌림','%'],['reclaim_ma10_pct','MA10 대비 회복 종가','%'],['prior_15m_pct','직전 15분 대비 신호봉 종가','%'],['prior_30m_pct','직전 30분 대비 신호봉 종가','%']];
 const groups=[['익절 TP',tp],['손절 SL',sl],['시간 종료',timed],['순수익 양수',decided.filter(e=>e.net_pct>0)],['순수익 0 이하',decided.filter(e=>e.net_pct<=0)]];
 $('featureGroups').innerHTML='<table><thead><tr><th>기준(신호 확정 시점)</th>'+groups.map(([n,g])=>`<th>${escapeHtml(n)} (${g.length})</th>`).join('')+'</tr></thead><tbody>'+features.map(([k,label,unit])=>'<tr><td>'+escapeHtml(label)+'</td>'+groups.map(([,g])=>'<td>'+metricValue(med(g,k),unit)+'</td>').join('')+'</tr>').join('')+'</tbody></table>';
 function firststats(label,g){const s=aggregate(g);return `<tr><td>${escapeHtml(label)}</td><td>${s.n}</td><td>${s.tp}/${s.sl}/${s.time}</td><td>${s.n?metricValue(s.winPct,'%'):'—'}</td><td>${s.n?metricValue(s.avg,'%'):'—'}</td></tr>`;}
 const block=(list,label)=>{const count=list.length,slN=list.filter(x=>x.outcome==='SL').length;return `${label}: 판정 ${count}건 · 손절 ${slN}건`;};
 $('lossSummary').innerHTML='<p class="hint">첫 번째 신호가 미판정이면 이후 판정 거래를 첫 거래로 대체하지 않습니다. 서로 다른 종목도 동시간대에는 상관될 수 있습니다.</p><div class="scroll"><table><thead><tr><th>분류</th><th>판정</th><th>TP/SL/TIME</th><th>순승률</th><th>평균 순손익</th></tr></thead><tbody>'+firststats('코인당 최초 신호',first)+firststats('두 번째 이후 신호',repeat)+'</tbody></table></div>'+
 '<p class="hint">'+escapeHtml(block(sl,'손절군'))+' · 그중 10분 이내 손절 '+fast.length+'건. 진입 특징이 원인이라는 결론은 내릴 수 없습니다.</p>';
 const cases=sl.slice().sort((a,b)=>(a.minutes??999)-(b.minutes??999)||(a.signal_kst||'').localeCompare(b.signal_kst||'')).slice(0,30);
 $('lossCases').innerHTML=cases.length?'<table><thead><tr><th>종목</th><th>신호(KST)</th><th>최초?</th><th>보유</th><th>순손익</th><th>거래량배수</th><th>몸통비중</th><th>1H근사강도</th><th>눌림</th><th>회복</th></tr></thead><tbody>'+cases.map(e=>{let f=e.features||{};return `<tr><td>${escapeHtml(e.market)}</td><td>${escapeHtml(e.signal_kst)}</td><td>${e.is_first_signal?'첫 신호':'반복'}</td><td>${e.minutes}분</td><td>${metricValue(e.net_pct,'%')}</td><td>${metricValue(f.volume_ratio,'배')}</td><td>${metricValue(f.body_share_pct,'%')}</td><td>${metricValue(f.flow_1h_proxy_pct,'%')}</td><td>${metricValue(f.pullback_depth_pct,'%')}</td><td>${metricValue(f.reclaim_ma10_pct,'%')}</td></tr>`}).join('')+'</tbody></table>':'해당 날짜 손절 거래가 없습니다.';
}

function paint(){let a=sampleResults(),n=a.length,total=state?.markets.length||0,complete=total>0&&n===total&&state?.failed===0;
 $('progress').max=total||1;$('progress').value=n;
 let signals=a.reduce((s,x)=>s+x.signals,0),unknown=a.reduce((s,x)=>s+x.unknown,0),trades=a.flatMap(x=>x.trades||[]),first=a.map(x=>x.first_signal).filter(Boolean),firstDecided=first.filter(x=>x.outcome!=='UNKNOWN');
 let eligible=a.filter(x=>x.valid_windows>0).length;
 const covered=`${n}/${total}`;
 $('coverage').innerHTML=card('조회된 종목 / 전체',covered)+card('분석 가능한 코인',eligible+'개')+card('확인된 C 신호',signals+'건')+card('승패 미판정 C 신호',unknown+'건')+
  card('해당 날짜 5분봉 240개 이상',a.filter(x=>x.observed_5m>=240).length+'개')+card('120~239개',a.filter(x=>x.observed_5m>=120&&x.observed_5m<240).length+'개')+
  card('0~119개',a.filter(x=>x.observed_5m<120).length+'개')+card('5페이지 상한 도달',a.filter(x=>x.truncated).length+'개');
 if(!complete){$('summary').innerHTML=card('전체 시장 결과','검사 미완료')+card('진행',covered)+card('최종 순승률','표시 보류')+card('최종 평균 순손익','표시 보류');$('byMarket').textContent='전체 코인 검사 완료 후 결과가 공개됩니다.';$('csv').disabled=true;}
 else {let s=aggregate(trades),f=aggregate(firstDecided);
   $('summary').innerHTML=card('전체 KRW 검사',total+'개 완료')+card('C 첫 신호 발생 코인',first.length+'개')+
    card('첫 신호 중 판정 가능',f.n+'개')+card('첫 신호 순승률',f.n?numPct(f.winPct):'—')+
    card('첫 신호 평균 순손익',f.n?numPct(f.avg):'—')+card('첫 신호 TP / SL / 시간종료',`${f.tp} / ${f.sl} / ${f.time}`)+
    card('전체 C 판정 거래',s.n+'건')+card('전체 C 순승률',s.n?numPct(s.winPct):'—')+
    card('전체 C 평균 순손익',s.n?numPct(s.avg):'—')+card('전체 C TP / SL / 시간종료',`${s.tp} / ${s.sl} / ${s.time}`)+
    card('최초 신호 미판정',first.length-f.n+'개 코인')+card('C 신호 없는 종목',total-first.length+'개');
   let rows=a.filter(x=>x.signals>0).sort((x,y)=>x.market.localeCompare(y.market));
   $('byMarket').innerHTML=rows.length?`<table><thead><tr><th>코인</th><th>당일 5분봉</th><th>C 신호</th><th>판정</th><th>미판정</th><th>첫 신호</th><th>첫 신호 순손익</th><th>전체 평균 순손익</th></tr></thead><tbody>`+
      rows.map(x=>{let f=x.first_signal,t=aggregate(x.trades||[]);return `<tr><td>${escapeHtml(x.market)}</td><td>${x.observed_5m}/288</td><td>${x.signals}</td><td>${x.trades.length}</td><td>${x.unknown}</td><td>${escapeHtml(f?.outcome||'—')}</td><td>${f?.net_pct==null?'—':numPct(+f.net_pct)}</td><td>${t.n?numPct(t.avg):'—'}</td></tr>`}).join('')+'</tbody></table>':'C 신호 없음';
   $('csv').disabled=false;
 }
 if(complete)renderRisk(a);else for(const id of ['riskSummary','featureGroups','lossSummary','lossCases'])$(id).textContent='전체 시장 검사 완료 후 공개';
 if(state){let status=(complete?'✅ 전체 시장 검사 완료':'⏳ 전체 시장 미완료')+` · ${covered}개 · C 신호 ${signals}건 · 미판정 ${unknown}건`;
  if(state.failed)status+=' · 실패 배치가 있어 재개 필요';
  $('status').textContent=status+(complete?'\n이 결과는 과거 모의 성적이며 실전 승률이 아닙니다.':'\n검사가 끝나기 전에는 일부 종목 승률을 전체 시장 승률처럼 표시하지 않습니다.');
 }else $('status').textContent='검사 전 · 전체 시장 승률 없음';
}
async function req(url,opts){const ctl=new AbortController(),tid=setTimeout(()=>ctl.abort(),115000);
 try{const r=await fetch(url,{...opts,cache:'no-store',signal:ctl.signal});const v=await r.json();if(!r.ok||!v.ok)throw Error(v.error||'HTTP '+r.status);return v;}finally{clearTimeout(tid)}}
async function initSession(cfg){let v=await req('/api/winrate14cdiag/universe');if(!Array.isArray(v.items)||!v.items.length)throw Error('현재 KRW 목록이 비었습니다');
 state={version:2,config:cfg,markets:v.items,results:{},failed:0};save();}
async function run(){if(running)return;
 let cfg;try{cfg=cfgOf()}catch(e){$('status').textContent='⚠️ '+e.message;return;}
 if(state&&signature(state.config)!==signature(cfg)){
   if(!confirm('설정/날짜가 달라졌습니다. 기존 전체 시장 검사 진행 기록을 지우고 새로 시작할까요?'))return;
   state=null;localStorage.removeItem(KEY);
 }
 running=true;stopRequested=false;hardFailure=false;
 $('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;
 try{
   if(!state)await initSession(cfg);
   state.failed=0;save();paint();
   let pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m));
   if(!pending.length){paint();return;}
   const batches=[];for(let i=0;i<pending.length;i+=2)batches.push(pending.slice(i,i+2));
   let pos=0,fail=0;async function worker(){while(!stopRequested&&!hardFailure){const b=batches[pos++];if(!b)break;
      let response=null;for(let attempt=0;attempt<3&&!stopRequested;attempt++){
       try{const v=await req('/api/winrate14cdiag/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:b})});
          if(v.errors||v.scanned!==b.length||!Array.isArray(v.results)||v.results.length!==b.length||v.results.some(x=>!b.includes(x.market)||!Array.isArray(x.events)||x.events.length!==x.signals||x.events.filter(e=>e.outcome==='UNKNOWN').length!==x.unknown))throw Error('불완전한 배치 응답');
          response=v;break;
       }catch(e){if(attempt===2){fail++;$('status').textContent=`⚠️ ${b.join(', ')} 조회 실패: ${String(e.message||e)}. 중단 후 이어하기로 재검사 가능합니다.`;}
       else await new Promise(ok=>setTimeout(ok,1000*(attempt+1)));}
      }
      if(response){for(const x of response.results)state.results[x.market]=x;
        try{save();}catch(e){hardFailure=true;$('status').textContent='⚠️ 브라우저 저장공간 부족: 진행 기록을 보존할 수 없습니다.';break;}
        paint();
      }
      if(fail>=4){hardFailure=true;break;}
   }}
   await Promise.all([worker(),worker()]);
   state.failed=fail;try{save();}catch{}paint();
   if(stopRequested&&!hardFailure)$('status').textContent+='\n사용자가 일시 중지했습니다. 다시 버튼을 누르면 이어서 검사합니다.';
   if(fail)$('status').textContent+=`\n실패 배치 ${fail}개 · 완료되지 않은 종목은 이어하기 시 다시 검사합니다.`;
 }catch(e){$('status').textContent='⚠️ '+String(e.message||e)+' · 미완료 결과는 승률로 집계하지 않습니다.';}
 finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}
}
$('run').onclick=run;
$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;$('status').textContent+='\n진행 중인 배치만 마친 뒤 중지합니다.'};
$('reset').onclick=()=>{if(running)return;if(!confirm('현재 V11.14.4 진단용 진행 기록만 삭제할까요? 기존 V11.14.3과 V11.14.2 기록은 보존됩니다.'))return;
 state=null;localStorage.removeItem(KEY);paint();};
function csvCell(v){const s=String(v??'');return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{
 if(!state||sampleResults().length!==state.markets.length||state.failed)return;
 const headers=['date','market','observed_day_5m','c_signals','unknown_c','signal_time_kst','outcome','reason','entry','exit','minutes','net_pct','is_first_signal','volume_ratio','body_pct','body_share_pct','flow_1h_proxy_pct','pullback_depth_pct','reclaim_ma10_pct','prior_15m_pct','prior_30m_pct'];
 const lines=[headers.map(csvCell).join(',')];
 for(const x of sampleResults()){
  if(!x.signals){lines.push([state.config.date,x.market,x.observed_5m,0,0,'','NO_SIGNAL','','','','','','','','','','','','','',''].map(csvCell).join(','));continue;}
  if(!Array.isArray(x.events)||x.events.length!==x.signals){$('status').textContent='⚠️ CSV 신호 보존 개수가 일치하지 않습니다. 데이터를 다시 검사하세요.';return;}
  for(const e of x.events){
   const d=e.features||{};
   lines.push([state.config.date,x.market,x.observed_5m,x.signals,x.unknown,e.signal_kst,e.outcome,e.reason,e.entry,e.exit,e.minutes,e.net_pct,e.is_first_signal?1:0,d.volume_ratio,d.body_pct,d.body_share_pct,d.flow_1h_proxy_pct,d.pullback_depth_pct,d.reclaim_ma10_pct,d.prior_15m_pct,d.prior_30m_pct].map(csvCell).join(','));
  }
 }
 const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download=`bithumb_full_krw_C_diagnostics_${state.config.date}_v11144.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);
};
state=load();if(state)setConfig(state.config);paint();
</script></body></html>'''

def register_cdiag_lab(app, deps):
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    current = {'at': 0, 'names': None}
    mu = Lock()

    def current_names():
        with mu:
            if current['names'] is None or time.time() - current['at'] > 600:
                names = sorted({m for m in get_markets()
                                if isinstance(m, str) and MARKET_RE.fullmatch(m)})
                if not names:
                    raise ValueError('빗썸 KRW 종목 목록이 비었습니다')
                current.update(names=names, at=time.time())
            return current['names']

    @app.get('/backtest-winrate-c-diagnostics')
    def cdiag_page_11144():
        return render_template_string(C_DIAG_PAGE)

    @app.get('/api/winrate14cdiag/universe')
    def cdiag_universe_11144():
        try:
            names = current_names()
            return jsonify({'ok': True, 'items': names, 'count': len(names),
                            'universe_type': 'current_KRW_listings'})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)[:160]}), 400

    @app.post('/api/winrate14cdiag/evaluate')
    def cdiag_evaluate_11144():
        try:
            payload = request.get_json(silent=True)
            cfg = lab_valid_request(payload, kst)
            markets = payload.get('markets') if isinstance(payload, dict) else None
            if (not isinstance(markets, list) or not 1 <= len(markets) <= 2
                or len(markets) != len(set(markets))
                or any(not isinstance(m, str) or not MARKET_RE.fullmatch(m) for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 조회할 수 있습니다')
            names = set(current_names())
            if not all(m in names for m in markets):
                raise ValueError('현재 빗썸 KRW 목록에서 찾을 수 없는 종목입니다')
            results, errors = [], []
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {pool.submit(lab_cdiag_evaluate_symbol, api_get, m, cfg, kst): m
                           for m in markets}
                for fut in as_completed(futures):
                    try:
                        results.append(fut.result())
                    except Exception as e:
                        errors.append({'market': futures[fut], 'error': str(e)[:120]})
            return jsonify({'ok': len(errors) == 0, 'scanned': len(markets),
                            'results': results, 'errors': len(errors),
                            'error_markets': errors})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)[:160]}), 400


_v11143_register_daytrade_review = register_daytrade_review

def register_daytrade_review(app, deps):
    _v11143_register_daytrade_review(app, deps)
    register_cdiag_lab(app, deps)


# ---------------------------------------------------------------------------
# V11.14.5: FIX SETTLEMENT-ORDER VALIDATION, WITHOUT CHANGING C SIGNALS.
# This is a NEW experimental endpoint and a NEW browser checkpoint. The
# V11.14.2 / .3 / .4 endpoints and historical results remain unchanged.
# ---------------------------------------------------------------------------

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


def lab_cdiag_v11145_evaluate_symbol(api_get, market, cfg, kst):
    # All C signal gates, feature snapshots and cooldown remain V11.14.4.
    return lab_cdiag_evaluate_symbol(api_get, market, cfg, kst,
                                     outcome_fn=lab_outcome_v11145,
                                     require_full_horizon=False)


def make_c_v11145_page():
    """Reuse the V11.14.4 presentation, but isolate data and mark new rules."""
    p = C_DIAG_PAGE
    p = p.replace('V11.14.4', 'V11.14.5')
    p = p.replace('bithumb_v11144_c_diagnostics_checkpoint_1',
                  'bithumb_v11145_c_outcome_fix_checkpoint_1')
    p = p.replace('/api/winrate14cdiag/', '/api/winrate14cfix/')
    p = p.replace('x.version!==2', 'x.version!==3')
    p = p.replace('state={version:2,', 'state={version:3,')
    # Add a distinct, unambiguous explanation of the fix and its scope.
    intro = '<section class="panel"><h2>검증 조건</h2>'
    note = ("""<section class="panel"><h2>V11.14.5 · 판정 순서 수정</h2>
<div class="note">이번 화면은 과거 C 진입 조건은 변경하지 않고, <b>TP/SL 도달까지의 5분봉만 연속성을 확인</b>합니다.
먼저 익절·손절했다면 그 뒤의 봉 공백으로 결과를 미판정으로 되돌리지 않습니다.
신호를 판단할 과거 25봉과 다음 진입봉이 있으면, 미래 보유 기간 전체의 봉이 없더라도 신호는 기록하고 판정을 시도합니다.
반대로 <b>청산 전에 봉이 빠지면 여전히 UNKNOWN</b>입니다. 이 화면은 V11.14.4의 기존 검사 기록과 저장소를 공유하지 않습니다.
검증 비교는 <b>2026-11-11 / 60분 / TP 1.5% / SL 1.0% / 수수료 0.04% / 슬리피지 0.05%</b>를 권장합니다.
단일 날짜의 승률은 실전 성과나 장래 승률을 보장하지 않습니다.</div></section>
<section class="panel"><h2>검증 조건</h2>""")
    assert p.count(intro) == 1
    p = p.replace(intro,note,1)
    # New data MUST not accidentally mix with any old cached outcome labels.
    assert "bithumb_v11145_c_outcome_fix_checkpoint_1" in p
    assert "x.version!==3" in p and "state={version:3," in p
    assert '/api/winrate14cdiag/' not in p
    # V11.14.3 / 14.4 links remain accessible separately.
    previous = '<a href="/backtest-winrate-full-c">'
    assert previous in p
    p = p.replace(previous, '<a href="/backtest-winrate-c-diagnostics">V11.14.4 기존 손절 분석판</a> · '+previous,1)
    start = p.index("$('csv').onclick=()=>{")
    end = p.index('\nstate=load();', start)
    new_csv = r"""$('csv').onclick=()=>{
 if(!state||sampleResults().length!==state.markets.length||state.failed)return;
 const cfgCols=['rule_version','date','tp_pct','sl_pct','max_hold_minutes','fee_per_side_pct','slippage_per_side_pct'];
 const tradeCols=['market','observed_day_5m','c_signals','unknown_c','signal_time_kst','outcome','reason','entry','exit','minutes','net_pct','is_first_signal','volume_ratio','body_pct','body_share_pct','flow_1h_proxy_pct','pullback_depth_pct','reclaim_ma10_pct','prior_15m_pct','prior_30m_pct'];
 const lines=[[...cfgCols,...tradeCols].map(csvCell).join(',')];
 const prefix=['V11.14.5',state.config.date,state.config.tp,state.config.sl,state.config.horizon,state.config.fee,state.config.slip];
 function append(row){if(row.length!==tradeCols.length)throw Error('CSV 열 개수 불일치');lines.push([...prefix,...row].map(csvCell).join(','));}
 for(const x of sampleResults()){
  if(!x.signals){const row=Array(tradeCols.length).fill('');row[0]=x.market;row[1]=x.observed_5m;row[2]=0;row[3]=0;row[5]='NO_SIGNAL';append(row);continue;}
  if(!Array.isArray(x.events)||x.events.length!==x.signals){$('status').textContent='⚠️ CSV 신호 보존 개수가 일치하지 않습니다. 데이터를 다시 검사하세요.';return;}
  for(const e of x.events){const d=e.features||{};
   append([x.market,x.observed_5m,x.signals,x.unknown,e.signal_kst,e.outcome,e.reason,e.entry,e.exit,e.minutes,e.net_pct,e.is_first_signal?1:0,d.volume_ratio,d.body_pct,d.body_share_pct,d.flow_1h_proxy_pct,d.pullback_depth_pct,d.reclaim_ma10_pct,d.prior_15m_pct,d.prior_30m_pct]);
  }
 }
 const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),u=URL.createObjectURL(blob),a=document.createElement('a');
 a.href=u;a.download=`bithumb_full_krw_C_validated_${state.config.date}_v11145.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);
};"""
    p = p[:start]+new_csv+p[end:]
    assert 'rule_version' in p and 'max_hold_minutes' in p
    assert 'bithumb_full_krw_C_validated_' in p
    return p


C_VALID_PAGE = make_c_v11145_page()


def register_c_valid_lab(app, deps):
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cached = {'names': None, 'at': 0}
    mu = Lock()

    def current_names():
        with mu:
            if cached['names'] is None or time.time()-cached['at']>600:
                names = sorted({m for m in get_markets()
                                if isinstance(m,str) and MARKET_RE.fullmatch(m)})
                if not names:
                    raise ValueError('현재 빗썸 KRW 종목 목록이 비었습니다')
                cached.update(names=names,at=time.time())
            return cached['names']

    @app.get('/backtest-winrate-c-validated')
    def cvalid_page_11145():
        return render_template_string(C_VALID_PAGE)

    @app.get('/api/winrate14cfix/universe')
    def cvalid_universe_11145():
        try:
            names=current_names()
            return jsonify({'ok': True, 'items': names,'count': len(names),
                            'universe_type': 'current_KRW_listings',
                            'rule_version': 'V11.14.5'})
        except Exception as e:
            return jsonify({'ok':False,'error':str(e)[:160]}),400

    @app.post('/api/winrate14cfix/evaluate')
    def cvalid_evaluate_11145():
        try:
            payload = request.get_json(silent=True)
            cfg = lab_valid_request(payload,kst)
            markets = payload.get('markets') if isinstance(payload,dict) else None
            if (not isinstance(markets,list) or not 1<=len(markets)<=2
                or len(markets)!=len(set(markets))
                or any(not isinstance(m,str) or not MARKET_RE.fullmatch(m) for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 조회할 수 있습니다')
            allowed = set(current_names())
            if any(m not in allowed for m in markets):
                raise ValueError('현재 빗썸 KRW 목록에서 찾을 수 없는 종목입니다')
            results, errors = [], []
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures={pool.submit(lab_cdiag_v11145_evaluate_symbol,
                                     api_get,m,cfg,kst):m for m in markets}
                for fut in as_completed(futures):
                    try:
                        results.append(fut.result())
                    except Exception as e:
                        errors.append({'market':futures[fut],'error':str(e)[:120]})
            return jsonify({'ok':not errors, 'scanned':len(markets),
                            'results':results,'errors':len(errors),
                            'error_markets':errors,'rule_version':'V11.14.5'})
        except Exception as e:
            return jsonify({'ok':False,'error':str(e)[:160]}),400


_v11144_register_daytrade_review = register_daytrade_review

def register_daytrade_review(app, deps):
    _v11144_register_daytrade_review(app, deps)
    register_c_valid_lab(app, deps)


# ---------------------------------------------------------------------------
# V11.14.6: RSI(14) and MACD(12,26,9) research on ORIGINAL C signals.
# Comparison-only. No changes to V11.13 BUY or any V11.14.5 endpoints.
# RSI/Wilder and MACD/SMA-seeded EMAs computed on 70 CONTIGUOUS completed
# five-minute candles ending at the C signal candle, never future prices.
# Hypotheses are fixed BEFORE examining results:
#   C+RSI  : 40 <= RSI14 <= 70 and RSI14 > RSI14_previous
#   C+MACD : histogram > 0 and histogram > preceding histogram
#   C+both : both conditions
# Every subset is compared to ORIGINAL C on the SAME indicator-eligible cohort.
# ---------------------------------------------------------------------------

C_IND_LOOKBACK = 70


def cind_fetch_extended(api_get, market, cfg, kst):
    """Fetch 12h prehistory for indicator warm-up; keep prior 4h baseline intact.

    Uses the same Bithumb 5m KST-exclusive pagination and OHLCV validity as
    lab_fetch_5m. No synthetic candles and no modifications to old fetching.
    """
    begin = datetime.combine(cfg['day'], _time.min, tzinfo=kst) - timedelta(hours=12)
    end = datetime.combine(cfg['day']+timedelta(days=1), _time.min, tzinfo=kst)+timedelta(hours=2,minutes=5)
    cursor, seen, truncated = end, {}, True
    for _ in range(LAB_MAX_PAGES):
        raw = api_get('/v1/candles/minutes/5', {
            'market': market, 'count': 200,
            'to': cursor.strftime('%Y-%m-%d %H:%M:%S')})
        if not isinstance(raw, list):
            raise ValueError('5분봉 응답 형식 오류')
        oldest = None
        for bar in raw:
            if not isinstance(bar, dict):
                continue
            try:
                ts = local_time(bar['candle_date_time_kst'], kst)
                op,hi,lo,cl = (float(bar[k]) for k in
                        ('opening_price','high_price','low_price','trade_price'))
                vol = float(bar['candle_acc_trade_volume'])
                if not (all(isfinite(v) and v>0 for v in (op,hi,lo,cl))
                        and isfinite(vol) and vol>=0
                        and lo<=min(op,cl) and hi>=max(op,cl) and lo<=hi):
                    continue
                oldest = ts if oldest is None else min(oldest,ts)
                if begin <= ts < end:
                    seen[ts]={'t':ts,'o':op,'h':hi,'l':lo,'c':cl,'v':vol}
            except (KeyError,TypeError,ValueError,OverflowError):
                continue
        if oldest is None:
            truncated = False
            break
        if oldest<=begin or len(raw)<200:
            truncated = False
            break
        if oldest >= cursor:
            raise ValueError('분봉 페이지 진행 오류')
        cursor=oldest
    return [seen[k] for k in sorted(seen)], truncated


def cind_rsi_series(closes, period=14):
    """Wilder RSI; return last and preceding RSI. For flat prices, RSI=50."""
    if len(closes)<period+2:
        return None
    diffs=[closes[j]-closes[j-1] for j in range(1,len(closes))]
    gain=sum(max(d,0.0) for d in diffs[:period])/period
    loss=sum(max(-d,0.0) for d in diffs[:period])/period
    def value(g,l):
        if l==0 and g==0: return 50.0
        if l==0: return 100.0
        return 100-100/(1+g/l)
    r=value(gain,loss)
    prev=None
    for d in diffs[period:]:
        prev=r
        gain=(gain*(period-1)+max(d,0))/period
        loss=(loss*(period-1)+max(-d,0))/period
        r=value(gain,loss)
    return r,prev


def cind_macd_series(closes, fast=12, slow=26, signal=9):
    """SMA-seeded EMA 12/26; MACD, signal and histogram current/previous."""
    if len(closes)<slow+signal+1:
        return None
    a=sum(closes[:fast])/fast
    b=sum(closes[:slow])/slow
    ma=[]
    af=2/(fast+1)
    bs=2/(slow+1)
    for idx,price in enumerate(closes):
        if idx>=fast:
            a=af*price+(1-af)*a
        if idx>=slow:
            b=bs*price+(1-bs)*b
        if idx>=slow-1:
            ma.append(a-b)
    if len(ma)<signal+1:
        return None
    sg=sum(ma[:signal])/signal
    k=2/(signal+1)
    last_h=None
    prev_h=None
    for idx,macd in enumerate(ma):
        if idx>=signal:
            sg=k*macd+(1-k)*sg
        if idx>=signal-1:
            prev_h,last_h=last_h,macd-sg
    return ma[-1],sg,last_h,prev_h


def cind_indicators(extended, idx):
    """Only already completed 5m candles through signal idx are used."""
    if idx < C_IND_LOOKBACK-1:
        return {'eligible':False,'reason':'70개 과거 5분봉 부족'}
    if not lab_contiguous(extended, idx-(C_IND_LOOKBACK-1), idx):
        return {'eligible':False,'reason':'최근 70개 5분봉 연속성 부족'}
    prices=[r['c'] for r in extended[idx-C_IND_LOOKBACK+1:idx+1]]
    rsi=cind_rsi_series(prices)
    macd=cind_macd_series(prices)
    if not rsi or not macd or rsi[1] is None or macd[3] is None:
        return {'eligible':False,'reason':'지표 계산 불가'}
    rv,rprev=rsi
    m,s,h,hprev=macd
    denom=prices[-1]
    return {
        'eligible':True,'reason':None,
        'rsi14':round(rv,5), 'rsi_prev':round(rprev,5),
        'macd_pct':round(100*m/denom,6),
        'macd_signal_pct':round(100*s/denom,6),
        'hist_pct':round(100*h/denom,6),
        'hist_prev_pct':round(100*hprev/denom,6),
        'rsi_pass':bool(40<=rv<=70 and rv>rprev),
        'macd_pass':bool(h>0 and h>hprev),
    }


def cind_evaluate_symbol(api_get,market,cfg,kst):
    """Preserve V11.14.5 C events by evaluating ONLY its original 4h slice.

    Additional fetched history is used ONLY for RSI/MACD. It cannot create
    extra C signals or change the original next-bar outcome calculation.
    """
    extended,truncated=cind_fetch_extended(api_get,market,cfg,kst)
    day_start=datetime.combine(cfg['day'],_time.min,tzinfo=kst)
    day_end=day_start+timedelta(days=1)
    base_begin=day_start-timedelta(hours=4)
    base=[r for r in extended if r['t']>=base_begin]
    offset=len(extended)-len(base)
    day_bars=sum(day_start<=r['t']<day_end for r in base)
    n,valid,signals,unknown,cooldown=cfg['bars'],0,0,0,-1
    events,trades,first=None,[],None
    events=[]
    if len(base)>=LAB_WARMUP_BARS+2:
        for i in range(LAB_WARMUP_BARS,len(base)-1):
            t=base[i]['t']
            if not day_start<=t<day_end:
                continue
            if not lab_contiguous(base,i-24,i):
                continue
            valid+=1
            if i<cooldown or 'C' not in lab_signals(base,i):
                continue
            cooldown=i+n
            signals+=1
            outcome=lab_outcome_v11145(base,i,cfg)
            entry_time=t+timedelta(minutes=5)
            event={
                'time':entry_time.strftime('%m-%d %H:%M'),
                'signal_kst':entry_time.strftime('%Y-%m-%d %H:%M'),
                'outcome':outcome['outcome'],
                'net_pct':outcome.get('net_pct'),
                'entry':outcome.get('entry'),
                'exit':outcome.get('exit'),
                'minutes':outcome.get('minutes'),
                'reason':outcome.get('reason'),
                'is_first_signal':first is None,
                'features':lab_c_signal_features(base,i),
                'indicators':cind_indicators(extended,offset+i),
            }
            if first is None:
                first={k:event[k] for k in ('time','outcome','net_pct','entry','minutes')}
            events.append(event)
            if event['outcome']=='UNKNOWN':
                unknown+=1
            else:
                trades.append(event)
    assert len(events)==signals and len(trades)+unknown==signals
    return {'market':market,'observed_5m':day_bars,
        'longest_day_run':lab_longest_consecutive_5m([r for r in base if day_start<=r['t']<day_end]),
        'valid_windows':valid,'signals':signals,'unknown':unknown,
        'truncated':truncated,'first_signal':first,'trades':trades,'events':events}


def make_c_indicator_page():
    """Reuse the tested V11.14.5 mobile workflow, add paired-cohort table."""
    p=C_VALID_PAGE.replace('V11.14.5','V11.14.6')
    p=p.replace('bithumb_v11145_c_outcome_fix_checkpoint_1',
                'bithumb_v11146_rsi_macd_checkpoint_1')
    p=p.replace('/api/winrate14cfix/','/api/winrate14cind/')
    p=p.replace('x.version!==3','x.version!==4')
    p=p.replace('state={version:3,','state={version:4,')
    p=p.replace('현재 V11.14.4 진단용 진행 기록만 삭제할까요?',
                '현재 V11.14.6 지표 비교 진행 기록만 삭제할까요?')
    assert '/api/winrate14cfix/' not in p
    assert 'x.version!==4' in p and 'state={version:4,' in p
    assert 'bithumb_v11146_rsi_macd_checkpoint_1' in p
    intro='<section class="panel"><h2>검증 조건</h2>'
    info="""<section class="panel"><h2>V11.14.6 · RSI·MACD 가설 사전 고정</h2>
<div class="note"><b>기존 C 신호는 동일</b>하며, RSI·MACD는 신규 BUY 신호가 아니라 C의 <b>모의 거래 필터 후보</b>입니다.
RSI(14)는 Wilder 방식으로 <b>40~70이면서 직전 확정봉보다 상승</b>하는 경우,
MACD(12,26,9)는 <b>히스토그램이 양수이고 직전 확정봉보다 증가</b>하는 경우로 가설을 고정했습니다.
지표는 신호 시점까지 <b>연속 70개의 확정 5분봉</b>이 있는 경우에만 계산합니다.
<strong>공정 비교:</strong> 지표 계산이 가능한 동일 C 신호 집단에서 C 원본 / C+RSI / C+MACD / C+두 지표를 비교합니다.
기존 C 전체 신호에 대한 성적은 위의 종합 화면에 그대로 따로 나타납니다. 제외된 수익 기회도 표에 보입니다.
과거 6일에 대해 이미 알고 있는 결과로 규칙을 변경하지 마세요. 아직 평가하지 않은 다른 날짜에서 확인해야 합니다.</div></section>
<section class="panel"><h2>검증 조건</h2>"""
    assert p.count(intro)==1
    p=p.replace(intro,info,1)
    insertion='<section class="panel"><h2>주의사항</h2>'
    extra="""<section class="panel"><h2>RSI·MACD 비교 실험 (지표 계산 가능한 C 신호만)</h2>
<div id="indicatorSummary" class="stats">전체 시장 검사 후 표시</div>
<div id="indicatorResults" class="scroll">검사 완료 후 표시</div>
<p class="hint">수익/손실 여부는 비용 차감 후 순손익 기준입니다. TP/SL은 개별 봉 내 보수적인 체결 가정이며 실제 체결은 보장되지 않습니다. 필터 통과 거래 수가 적으면 승률이 높아도 신뢰할 수 없습니다. 현재 전체 시장 목록을 과거에 적용하므로 상장폐지 종목이 빠지는 생존자 편향이 있습니다.</p>
<div class="scroll" id="indicatorFirst">첫 신호만 비교: 검사 완료 후 표시</div>
</section>
<section class="panel"><h2>주의사항</h2>"""
    assert p.count(insertion)==1
    p=p.replace(insertion,extra,1)
    foot='<a href="/backtest-winrate-c-diagnostics">V11.14.4 기존 손절 분석판</a>'
    assert foot in p
    p=p.replace(foot,'<a href="/backtest-winrate-c-validated">V11.14.5 기준 검사</a> · '+foot,1)
    # Add compare calculations BEFORE paint(), and invoke after complete status.
    anchor='function paint(){let a=sampleResults()'
    assert p.count(anchor)==1
    compare_js=r"""
function renderIndicatorComparison(results){
 const events=results.flatMap(x=>(x.events||[]).map(e=>({...e,market:x.market})));
 const withIndicators=events.filter(x=>x.indicators?.eligible===true);
 const decided=withIndicators.filter(x=>x.outcome!=='UNKNOWN'&&typeof x.net_pct==='number');
 const first=decided.filter(x=>x.is_first_signal);
 const cases=[
  ['C 원본 (지표 공통표본)',x=>true],
  ['C + RSI(14)',x=>x.indicators.rsi_pass===true],
  ['C + MACD(12·26·9)',x=>x.indicators.macd_pass===true],
  ['C + RSI + MACD',x=>x.indicators.rsi_pass===true&&x.indicators.macd_pass===true]
 ];
 const unavailable=events.length-withIndicators.length;
 const missing=withIndicators.filter(x=>x.outcome==='UNKNOWN').length;
 $('indicatorSummary').innerHTML=card('전체 C 신호',events.length+'건')+
  card('지표 계산 가능 신호',withIndicators.length+'건')+
  card('지표 불가능 · 비교 제외',unavailable+'건')+
  card('지표 가능하지만 승패 미판정',missing+'건')+
  card('동일 표본 · 판정 거래',decided.length+'건');
 const baseline=aggregate(decided);
 function table(data){return '<table><thead><tr><th>비교 방식</th><th>판정 거래</th><th>익절</th><th>손절</th><th>시간 종료</th><th>순승률</th><th>평균 순손익</th><th>기존 대비 기회 제외</th><th>제외한 수익 거래</th></tr></thead><tbody>'+cases.map(([label,check])=>{
  const kept=data.filter(check),omitted=data.filter(x=>!check(x)),s=aggregate(kept);
  return `<tr><td>${escapeHtml(label)}</td><td>${s.n}</td><td>${s.tp}</td><td>${s.sl}</td><td>${s.time}</td><td>${s.n?numPct(s.winPct):'—'}</td><td>${s.n?numPct(s.avg):'—'}</td><td>${omitted.length}</td><td>${omitted.filter(x=>x.net_pct>0).length}</td></tr>`;
 }).join('')+'</tbody></table>';}
 $('indicatorResults').innerHTML='<p class="hint">같은 데이터로 계산 가능한 판정 C 거래 '+decided.length+'건이 공통 모집단입니다. 전체 C '+events.length+'건을 기준으로 한 직접 비교가 아닙니다.</p>'+table(decided);
 $('indicatorFirst').innerHTML='<p class="hint">코인별 실제 최초 C 신호 중 지표 계산 가능하며 판정된 거래만 별도 비교합니다. 최초 신호가 미판정이면 뒤의 거래로 대체하지 않습니다.</p>'+table(first);
}

"""
    p=p.replace(anchor,compare_js+anchor,1)
    trigger="if(complete)renderRisk(a);else for(const id of ['riskSummary','featureGroups','lossSummary','lossCases'])$(id).textContent='전체 시장 검사 완료 후 공개';"
    assert p.count(trigger)==1
    p=p.replace(trigger,trigger+"\n if(complete)renderIndicatorComparison(a);else { $('indicatorSummary').textContent='전체 시장 검사 완료 후 공개'; $('indicatorResults').textContent='전체 시장 검사 완료 후 공개'; $('indicatorFirst').textContent='전체 시장 검사 완료 후 공개'; }",1)
    # Replace only V11.14.5 CSV handler with V11.14.6 expanded CSV schema.
    start=p.index("$('csv').onclick=()=>{")
    end=p.index('\nstate=load();',start)
    new_csv=r"""$('csv').onclick=()=>{
 if(!state||sampleResults().length!==state.markets.length||state.failed)return;
 const configCols=['rule_version','date','tp_pct','sl_pct','max_hold_minutes','fee_per_side_pct','slippage_per_side_pct'];
 const detailCols=['market','observed_day_5m','c_signals','unknown_c','signal_time_kst','outcome','reason','entry','exit','minutes','net_pct','is_first_signal','volume_ratio','body_pct','body_share_pct','flow_1h_proxy_pct','pullback_depth_pct','reclaim_ma10_pct','prior_15m_pct','prior_30m_pct',
  'ind_eligible','ind_reason','rsi14','rsi_prev','macd_pct','macd_signal_pct','hist_pct','hist_prev_pct','rsi_pass','macd_pass'];
 const lines=[[...configCols,...detailCols].map(csvCell).join(',')];
 const prefix=['V11.14.6',state.config.date,state.config.tp,state.config.sl,state.config.horizon,state.config.fee,state.config.slip];
 function push(row){if(row.length!==detailCols.length)throw Error('CSV 열 개수 불일치');lines.push([...prefix,...row].map(csvCell).join(','));}
 for(const x of sampleResults()){
  if(!x.signals){const row=Array(detailCols.length).fill('');row[0]=x.market;row[1]=x.observed_5m;row[2]=0;row[3]=0;row[5]='NO_SIGNAL';push(row);continue;}
  if(!Array.isArray(x.events)||x.events.length!==x.signals){$('status').textContent='⚠️ 모든 C 신호를 보존하지 못했습니다. 재검사하세요.';return;}
  for(const e of x.events){const d=e.features||{},i=e.indicators||{};
   push([x.market,x.observed_5m,x.signals,x.unknown,e.signal_kst,e.outcome,e.reason,e.entry,e.exit,e.minutes,e.net_pct,e.is_first_signal?1:0,d.volume_ratio,d.body_pct,d.body_share_pct,d.flow_1h_proxy_pct,d.pullback_depth_pct,d.reclaim_ma10_pct,d.prior_15m_pct,d.prior_30m_pct,
    i.eligible?1:0,i.reason,i.rsi14,i.rsi_prev,i.macd_pct,i.macd_signal_pct,i.hist_pct,i.hist_prev_pct,i.eligible?(i.rsi_pass?1:0):'',i.eligible?(i.macd_pass?1:0):'']);
  }
 }
 const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),u=URL.createObjectURL(blob),a=document.createElement('a');
 a.href=u;a.download=`bithumb_full_krw_C_rsi_macd_${state.config.date}_v11146.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);
};"""
    p=p[:start]+new_csv+p[end:]
    assert 'renderIndicatorComparison(a)' in p and "ind_eligible" in p
    assert '/api/winrate14cfix/' not in p
    return p


C_IND_PAGE=make_c_indicator_page()


def register_c_indicator_lab(app,deps):
    api_get,get_markets,kst=deps['api_get'],deps['get_markets'],deps['KST']
    cache={'at':0,'names':None}
    mutex=Lock()

    def listed():
        with mutex:
            if cache['names'] is None or time.time()-cache['at']>600:
                markets=sorted({m for m in get_markets()
                                if isinstance(m,str) and MARKET_RE.fullmatch(m)})
                if not markets:
                    raise ValueError('현재 KRW 상장 목록이 없습니다')
                cache.update(names=markets,at=time.time())
            return cache['names']

    @app.get('/backtest-winrate-c-indicators')
    def c_ind_page_11146():
        return render_template_string(C_IND_PAGE)

    @app.get('/api/winrate14cind/universe')
    def c_ind_universe_11146():
        try:
            names=listed()
            return jsonify({'ok':True,'items':names,'count':len(names),
                            'universe_type':'current_KRW_listings',
                            'rule_version':'V11.14.6'})
        except Exception as e:
            return jsonify({'ok':False,'error':str(e)[:160]}),400

    @app.post('/api/winrate14cind/evaluate')
    def c_ind_eval_11146():
        try:
            payload=request.get_json(silent=True)
            cfg=lab_valid_request(payload,kst)
            markets=payload.get('markets') if isinstance(payload,dict) else None
            if (not isinstance(markets,list) or not 1<=len(markets)<=2
                    or len(set(markets))!=len(markets)
                    or any(not isinstance(m,str) or not MARKET_RE.fullmatch(m) for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 검사합니다')
            allowed=set(listed())
            if any(m not in allowed for m in markets):
                raise ValueError('현재 빗썸 KRW 목록에 없는 종목입니다')
            results,errors=[],[]
            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks={pool.submit(cind_evaluate_symbol,api_get,m,cfg,kst):m for m in markets}
                for future in as_completed(tasks):
                    try:
                        results.append(future.result())
                    except Exception as e:
                        errors.append({'market':tasks[future],'error':str(e)[:120]})
            return jsonify({'ok':not errors,'scanned':len(markets),
                            'results':results,'errors':len(errors),
                            'error_markets':errors,'rule_version':'V11.14.6'})
        except Exception as e:
            return jsonify({'ok':False,'error':str(e)[:160]}),400


_v11145_register_daytrade_review=register_daytrade_review

def register_daytrade_review(app,deps):
    _v11145_register_daytrade_review(app,deps)
    register_c_indicator_lab(app,deps)


# ---------------------------------------------------------------------------
# V11.14.7: C vs. C2 delayed-confirmation experiment (NEW read-only endpoint).
# Historical C, V11.14.5, RSI/MACD study, V11.13 and V2 are NOT modified.
# C2 is evaluated ON THE EXACT preexisting C signal universe and cooldown.
# C2 decisions use one additional completed 5M bar and enter one bar later.
# ---------------------------------------------------------------------------


def c2_confirmation(rows, i):
    """After C closes at i, check next COMPLETED 5-minute candle (i+1).

    Ex-ante C2 hypothesis; no target/stop/outcome or further prices involved.
    The C signal low is an *experimental* short-term support proxy, not a
    verified horizontal support level. A 0.2% undercut is permitted.
    """
    if i+1 >= len(rows):
        return {'status':'UNKNOWN', 'reason':'다음 확인 봉 없음'}
    if rows[i+1]['t']-rows[i]['t'] != timedelta(minutes=5):
        return {'status':'UNKNOWN', 'reason':'확인 봉 사이 5분 공백'}
    signal, confirm=rows[i],rows[i+1]
    holds=confirm['l'] >= signal['l']*0.998
    rises=confirm['c'] > signal['c']
    bull=confirm['c'] > confirm['o']
    if not holds:
        return {'status':'SKIP','reason':'C 신호봉 저가 지지 유지 실패'}
    if not rises:
        return {'status':'SKIP','reason':'직전 C 종가 위로 재상승 실패'}
    if not bull:
        return {'status':'SKIP','reason':'확인 봉이 양봉 아님'}
    return {'status':'PASS','reason':'지지 유지 + 양봉 + 종가 재상승'}


def c2_evaluate_symbol(api_get,market,cfg,kst):
    """Single 5m fetch, identical original C decisions and cooldown.

    Baseline C enters at opening of i+1, C2 may enter at opening of i+2.
    Both get their OWN stop/target/horizon from OWN entry opening.
    C2 SKIP denotes no position, not a profit or loss; UNKNOWN is unscored.
    """
    rows,truncated=lab_fetch_5m(api_get,market,cfg,kst)
    day_start=datetime.combine(cfg['day'],_time.min,tzinfo=kst)
    day_end=day_start+timedelta(days=1)
    day_bars=[r for r in rows if day_start<=r['t']<day_end]
    n,valid,signals,unknown,cooldown=cfg['bars'],0,0,0,-1
    events=[]
    if len(rows)>=LAB_WARMUP_BARS+2:
        for i in range(LAB_WARMUP_BARS,len(rows)-1):
            if not day_start<=rows[i]['t']<day_end:
                continue
            if not lab_contiguous(rows,i-24,i):
                continue
            valid+=1
            if i<cooldown or 'C' not in lab_signals(rows,i):
                continue
            cooldown=i+n  # IDENTICAL C cooldown. C2 does not replace it.
            signals+=1
            baseline=lab_outcome_v11145(rows,i,cfg)
            check=c2_confirmation(rows,i)
            if check['status']=='PASS':
                c2=lab_outcome_v11145(rows,i+1,cfg)
            else:
                c2={'outcome':check['status'],'reason':check['reason']}
            if baseline['outcome']=='UNKNOWN':unknown+=1
            entry_t=rows[i]['t']+timedelta(minutes=5)
            confirm_t=rows[i]['t']+timedelta(minutes=5)
            c2_entry_t=rows[i]['t']+timedelta(minutes=10)
            events.append({
                'signal_time_kst':rows[i]['t'].strftime('%Y-%m-%d %H:%M'),
                'c_entry_time_kst':entry_t.strftime('%Y-%m-%d %H:%M'),
                'c2_confirm_time_kst':confirm_t.strftime('%Y-%m-%d %H:%M'),
                'c2_entry_time_kst':c2_entry_t.strftime('%Y-%m-%d %H:%M'),
                'is_first_signal':signals==1,
                'c':baseline,
                'c2':c2,
                'c2_check':check,
                'features':lab_c_signal_features(rows,i),
            })
    return {
        'market':market,'observed_5m':len(day_bars),
        'longest_day_run':lab_longest_consecutive_5m(day_bars),
        'valid_windows':valid,'signals':signals,'unknown':unknown,
        'truncated':truncated,'events':events,
    }


C2_PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.7 · 기존 C vs C2 지지 확인 실험</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#1a2038;--card:#121a2e;--border:#35425f;--fg:#f2f5ff;--muted:#afc0db;--blue:#3867ee;--green:#4ade80;--warn:#ffc46c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1050px;margin:auto;padding:18px 14px 70px}h1{font-size:23px;margin:8px 0}h2{font-size:18px;margin:0 0 8px}
.panel{border:1px solid var(--border);background:var(--panel);border-radius:16px;padding:16px;margin-top:15px}
.muted,.hint{font-size:13px;color:var(--muted)}.note{padding:12px;background:#101a30;border-radius:10px;font-size:13px;line-height:1.75}
.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.field{display:flex;flex-direction:column;font-size:13px;color:var(--muted);gap:5px}
input,button,select{font:inherit}input,select{padding:10px;background:#10182d;border:1px solid var(--border);color:var(--fg);border-radius:8px;width:100%;min-width:0}
button{background:var(--blue);border:0;border-radius:9px;color:#fff;padding:12px 14px;font-weight:800;cursor:pointer}button:disabled{opacity:.48}.secondary{background:#364660}
.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px;align-items:center}a{color:#bcd0ff}progress{width:100%;height:15px;margin-top:14px}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.stat{border:1px solid var(--border);background:var(--card);border-radius:10px;padding:11px}
.stat small{font-size:12px;color:var(--muted);display:block}.stat strong{font-size:22px;display:block}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--border);padding:9px 7px;text-align:right;white-space:nowrap}td:first-child,th:first-child{text-align:left}
.status{font-size:13px;white-space:pre-wrap;color:var(--muted)}.good{color:var(--green)}.warn{color:var(--warn)}
@media(max-width:650px){.controls{grid-template-columns:repeat(2,minmax(0,1fr))}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><main>
<h1>🧪 V11.14.7 · C vs C2 진입 구조 비교</h1>
<p class="muted">별도 과거 모의 검증실 · C2는 실전 BUY 신호가 아닙니다. V11.13, V11.14.5, V11.14.6 및 V2 메인은 변경하지 않습니다.</p>
<section class="panel"><h2>먼저 비교 방법부터 확인</h2>
<div class="note"><b>기존 C:</b> 상승 흐름 → 눌림 → 첫 반등 5분봉 확정 → <b>다음 5분봉 시가 진입</b>.<br>
<b>실험 C2:</b> 같은 첫 반등 신호 이후 <b>다음 확정 5분봉</b>에서 (① 저가가 첫 신호봉 저가보다 0.2% 넘게 낮아지지 않음, ② 양봉, ③ 종가가 첫 신호봉 종가보다 높음)을 모두 확인 → <b>그다음 5분봉 시가 진입</b>.<br>
C2의 지지는 <b>신호봉 저가를 이용한 잠정 기준</b>이지 진짜 수평 지지선 인증이 아닙니다. C2는 조건을 못 맞추면 진입하지 않습니다.
C와 C2는 <b>각자의 실제 모의 진입 시가</b>를 기준으로 새로 익절·손절·시간 종료를 계산합니다.
모든 C 신호의 발생 순서와 종목별 재진입 대기 시간은 이전과 동일합니다. C2 때문에 신호를 과거로 되돌려 재선택하지 않습니다.</div>
</section>
<section class="panel"><h2>검증 조건</h2>
<div class="controls">
<label class="field">과거 날짜 (KST)<input id="date" type="date"></label>
<label class="field">익절 목표 (%)<input id="tp" type="number" min="0.3" max="10" step="0.1" value="1.5"></label>
<label class="field">손절 기준 (%)<input id="sl" type="number" min="0.2" max="8" step="0.1" value="1.0"></label>
<label class="field">최대 보유<select id="horizon"><option value="30">30분</option><option value="60" selected>60분</option><option value="120">120분</option></select></label>
<label class="field">편도 수수료 (%)<input id="fee" type="number" step="0.01" value="0.04" min="0" max="0.5"></label>
<label class="field">편도 슬리피지 (%)<input id="slip" type="number" step="0.01" value="0.05" min="0" max="0.5"></label>
</div>
<div class="actions"><button id="run">전체 KRW 검사 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">이번 C2 진행 기록 초기화</button></div>
<progress id="progress" value="0" max="1"></progress><p id="status" class="status">검사 전 · 전체 시장 승률 없음</p>
<p class="hint">지금 현재 상장 중인 빗썸 KRW 종목을 과거 날짜에 적용하므로 생존자 편향이 있습니다. 전체 검사 완료 전에는 최종 승률을 공개하지 않습니다. 중단 시 같은 브라우저에서 이어할 수 있습니다.</p>
</section>
<section class="panel"><h2>전 종목 데이터 현황</h2><div id="coverage" class="stats">검사 전</div></section>
<section class="panel"><h2>C vs C2 · 전체 결과</h2><div id="summary" class="stats">검사 완료 후 공개</div>
<p class="hint">전체 C 거래 평균과 C2 통과 거래 평균은 거래 대상이 다르므로 이 둘만 보고 C2가 우수하다고 결론 내릴 수 없습니다.</p>
<div id="allTable" class="scroll">검사 완료 후 공개</div></section>
<section class="panel"><h2>공정 비교 A · 실제 C2 진입한 신호만 짝짓기</h2>
<div class="note">C2가 통과했고 <b>C와 C2 모두 손익이 판정된 동일한 C 신호</b>를 골라서 두 진입 가격·손익을 비교합니다. 이 표는 한 봉 늦은 진입 효과를 확인하지만, C2가 제외한 거래의 효과는 따로 보아야 합니다.</div>
<div id="pairTable" class="scroll">검사 완료 후 공개</div></section>
<section class="panel"><h2>공정 비교 B · 원래 C 신호 한 건당 평균 손익</h2>
<div class="note">같은 원래 C 신호에서 <b>C가 진입하면 실제 모의 손익</b>, <b>C2가 조건 미충족으로 진입하지 않으면 0%</b>로 계산합니다. 두 전략이 판정 가능한 신호만 사용하고, 결과 미판정은 공정 비교에서 제외합니다. 기회당 평균이며 실제 계좌 수익률·동시 보유 포트폴리오 수익률이 아닙니다.</div>
<div id="opportunityTable" class="scroll">검사 완료 후 공개</div></section>
<section class="panel"><h2>코인별 첫 C 신호 기준</h2><p class="hint">첫 신호가 미판정이면 두 번째 승리 거래로 대체하지 않습니다.</p>
<div id="firstTable" class="scroll">검사 완료 후 공개</div></section>
<section class="panel"><h2>C2 진입 보류 원인</h2><div id="reasons" class="stats">검사 완료 후 공개</div></section>
<section class="panel"><h2>종목별 신호 비교</h2><div id="byMarket" class="scroll">검사 완료 후 공개</div>
<div class="actions"><button id="csv" disabled>전체 C/C2 비교 CSV 저장</button></div></section>
<section class="panel"><h2>해석 주의</h2><div class="note">완성된 5분봉 OHLC에 기반한 모의 체결입니다. 봉 내부 익절/손절이 동시에 발생하면 손절 우선, 청산 전 봉 공백은 미판정입니다. 실제 호가 스프레드, 가격 단위, 체결 가능성 및 상장폐지 종목은 반영하지 못합니다. C의 1시간 흐름은 실제 1시간봉이 아니라 과거 24개의 5분봉을 이용한 근사치입니다. 반복 신호는 독립 사례가 아닐 수 있습니다. 이미 살펴본 11월 4~11일은 C2 규칙 설정에 영향을 준 과거 기간이므로 <b>검증용 독립 표본으로 취급하지 않습니다.</b> 다른 날짜에서 평가하세요.</div>
<p><a href="/backtest-winrate-c-indicators">V11.14.6 RSI·MACD</a> · <a href="/backtest-winrate-c-validated">V11.14.5 기존 C</a> · <a href="/backtest-daytrade">V11.13 실시간</a> · <a href="/">V2 메인</a></p></section>
</main><script>
const $=id=>document.getElementById(id);
const escapeHtml=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const KEY='bithumb_v11147_c2_checkpoint_1';
let state=null,running=false,stopRequested=false,hardFailure=false;
function kstDate(dt){let p=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(dt);let v=k=>p.find(x=>x.type===k).value;return `${v('year')}-${v('month')}-${v('day')}`;}
const yesterday=()=>kstDate(new Date(Date.now()-86400000));
$('date').max=yesterday();$('date').value=yesterday();
function cfgOf(){let c={date:$('date').value,tp:+$('tp').value,sl:+$('sl').value,horizon:+$('horizon').value,fee:+$('fee').value,slip:+$('slip').value};
 if(!/^\d{4}-\d{2}-\d{2}$/.test(c.date)||c.date>yesterday())throw Error('완료된 과거 날짜를 선택하세요');
 if(![c.tp,c.sl,c.horizon,c.fee,c.slip].every(Number.isFinite)||c.tp<0.3||c.tp>10||c.sl<0.2||c.sl>8||![30,60,120].includes(c.horizon)||c.fee<0||c.fee>0.5||c.slip<0||c.slip>0.5)throw Error('검증 설정 범위를 확인하세요');
 return c;
}
const signature=c=>JSON.stringify([c.date,c.tp,c.sl,c.horizon,c.fee,c.slip]);
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function load(){try{const x=JSON.parse(localStorage.getItem(KEY)||'null');if(!x||x.version!==7||!Array.isArray(x.markets)||!x.config||!x.results||typeof x.results!=='object')return null;
 if(x.markets.length<1||x.markets.some(m=>!/^KRW-[A-Z0-9]+$/.test(m))||new Set(x.markets).size!==x.markets.length)return null;return x;}catch{return null}}
function setConfig(c){for(const id of ['date','tp','sl','horizon','fee','slip'])$(id).value=c[id];}
function numPct(n){return Number.isFinite(n)?n.toFixed(3)+'%':'—';}
function card(name,val){return `<div class="stat"><small>${escapeHtml(name)}</small><strong>${escapeHtml(val)}</strong></div>`;}
function sampleResults(){return state?state.markets.filter(m=>Object.prototype.hasOwnProperty.call(state.results,m)).map(m=>state.results[m]):[];}
const decided=e=>e&&['TP','SL','TIME'].includes(e.outcome)&&typeof e.net_pct==='number'&&Number.isFinite(e.net_pct);
function aggregate(events){let n=0,tp=0,sl=0,tm=0,wins=0,net=0;for(const x of events){if(!decided(x))continue;n++;net+=x.net_pct;if(x.net_pct>0)wins++;if(x.outcome==='TP')tp++;if(x.outcome==='SL')sl++;if(x.outcome==='TIME')tm++;}
 return {n,tp,sl,tm,wins,net,avg:n?net/n:null,win:n?100*wins/n:null};}
function table(rows){return '<table><thead><tr><th>구분</th><th>판정 거래</th><th>익절/손절/시간종료</th><th>순승률</th><th>평균 순손익</th></tr></thead><tbody>'+rows.map(([label,e])=>{let s=aggregate(e);return `<tr><td>${escapeHtml(label)}</td><td>${s.n}</td><td>${s.tp}/${s.sl}/${s.tm}</td><td>${numPct(s.win)}</td><td>${numPct(s.avg)}</td></tr>`;}).join('')+'</tbody></table>';}
function flatten(results){return results.flatMap(r=>(r.events||[]).map(e=>({...e,market:r.market})));}
function renderComparisons(results){
 const es=flatten(results),cs=es.map(e=>e.c).filter(decided),c2=es.map(e=>e.c2).filter(decided);
 const pair=es.filter(e=>decided(e.c)&&decided(e.c2));
 const opportunity=es.filter(e=>decided(e.c)&&(decided(e.c2)||e.c2?.outcome==='SKIP'));
 const first=es.filter(e=>e.is_first_signal),pairFirst=first.filter(e=>decided(e.c)&&decided(e.c2));
 const opportunityFirst=first.filter(e=>decided(e.c)&&(decided(e.c2)||e.c2?.outcome==='SKIP'));
 const cTotal=aggregate(cs),c2Total=aggregate(c2);
 const pass=es.filter(e=>e.c2_check?.status==='PASS').length;
 const skipped=es.filter(e=>e.c2?.outcome==='SKIP').length;
 const unknownC2=es.filter(e=>e.c2?.outcome==='UNKNOWN').length;
 $('summary').innerHTML=card('원본 C 신호',es.length+'건')+card('기존 C 판정 거래',cTotal.n+'건')+card('C2 확인봉 통과',pass+'건')+card('C2 판정 거래',c2Total.n+'건')+card('C2 진입 안 함',skipped+'건')+card('C2 미판정',unknownC2+'건')+card('C2 순승률',numPct(c2Total.win))+card('C2 거래당 평균 순손익',numPct(c2Total.avg));
 $('allTable').innerHTML=table([['기존 C · 전체 판정',cs],['C2 · 실제 판정된 진입',c2]]);
 $('pairTable').innerHTML='<p class="hint">같은 C 신호 '+pair.length+'건: 두 전략 모두 손익 판정이 끝난 거래만 비교합니다.</p>'+table([['기존 C · 동일 신호',pair.map(e=>e.c)],['C2 · 동일 신호',pair.map(e=>e.c2)]]);
 function opportunityMetrics(data){const n=data.length,c=aggregate(data.map(e=>e.c)),c2Dec=aggregate(data.map(e=>e.c2));const skips=data.filter(e=>e.c2?.outcome==='SKIP').length;const c2Net=data.reduce((s,e)=>s+(decided(e.c2)?e.c2.net_pct:0),0);
  return {n,skips,cAvg:n?c.net/n:null,c2Avg:n?c2Net/n:null,c2Entered:c2Dec.n};}
 const o=opportunityMetrics(opportunity),of=opportunityMetrics(opportunityFirst);
 function opportunityHTML(title,s){return `<p class="hint">${escapeHtml(title)} · 공통 판정 가능 원본 C 신호 ${s.n}건 · C2 실제 진입 ${s.c2Entered}건 · C2 미진입 ${s.skips}건</p><table><thead><tr><th>방식</th><th>원본 신호 수</th><th>원본 신호당 평균 순손익</th></tr></thead><tbody><tr><td>C · 모두 진입</td><td>${s.n}</td><td>${numPct(s.cAvg)}</td></tr><tr><td>C2 · 보류는 0%</td><td>${s.n}</td><td>${numPct(s.c2Avg)}</td></tr></tbody></table>`;}
 $('opportunityTable').innerHTML=opportunityHTML('모든 신호',o);
 $('firstTable').innerHTML='<p class="hint">코인별 진짜 첫 신호 '+first.length+'건. 이 가운데 모두 판정된 C2 진입 '+pairFirst.length+'건.</p>'+table([['C · C2 진입한 첫 신호만',pairFirst.map(e=>e.c)],['C2 · 같은 첫 신호만',pairFirst.map(e=>e.c2)]])+opportunityHTML('첫 신호 · 보류 포함',of);
 const reasonMap=new Map();for(const e of es){const k=e.c2_check?.reason||'확인 실패';reasonMap.set(k,(reasonMap.get(k)||0)+1);}const parts=[...reasonMap.entries()].sort((a,b)=>b[1]-a[1]);
 $('reasons').innerHTML=parts.map(([name,n])=>card(name,n+'건')).join('')||'신호 없음';
 const markets=results.filter(x=>x.signals>0).sort((a,b)=>a.market.localeCompare(b.market));
 $('byMarket').innerHTML=markets.length?'<table><thead><tr><th>코인</th><th>C 신호</th><th>C 판정</th><th>C2 통과</th><th>C2 판정</th><th>C2 보류</th></tr></thead><tbody>'+markets.map(x=>{const c=x.events.filter(e=>decided(e.c)).length,c2=x.events.filter(e=>decided(e.c2)).length,ok=x.events.filter(e=>e.c2_check?.status==='PASS').length;return `<tr><td>${escapeHtml(x.market)}</td><td>${x.signals}</td><td>${c}</td><td>${ok}</td><td>${c2}</td><td>${x.events.filter(e=>e.c2?.outcome==='SKIP').length}</td></tr>`;}).join('')+'</tbody></table>':'C 신호 없음';
}
function paint(){const a=sampleResults(),n=a.length,total=state?.markets.length||0,complete=total>0&&n===total&&state?.failed===0;
 $('progress').max=total||1;$('progress').value=n;
 const signals=a.reduce((s,x)=>s+x.signals,0);
 $('coverage').innerHTML=card('조회된 코인',`${n}/${total}`)+card('분석 가능 코인',a.filter(x=>x.valid_windows>0).length+'개')+card('C 신호',signals+'건')+card('240봉 이상',a.filter(x=>x.observed_5m>=240).length+'개')+card('120~239봉',a.filter(x=>x.observed_5m>=120&&x.observed_5m<240).length+'개')+card('0~119봉',a.filter(x=>x.observed_5m<120).length+'개')+card('과거 페이지 상한',a.filter(x=>x.truncated).length+'개');
 const outputs=['summary','allTable','pairTable','opportunityTable','firstTable','reasons','byMarket'];
 if(complete){renderComparisons(a);$('csv').disabled=false;}else{for(const id of outputs)$(id).textContent='전체 KRW 검사가 완료되면 공개됩니다.';$('csv').disabled=true;}
 if(state){$('status').textContent=(complete?'✅ 전체 검사 완료':'⏳ 검사 진행 중')+` · ${n}/${total}종목 · C 신호 ${signals}건`+(state.failed?' · 일부 배치 실패, 이어하기 필요':'')+(complete?'\nC2는 연구용 과거 시뮬레이션이며 실전 승률이 아닙니다.':'\n검사 완료 전 부분적인 승률을 표시하지 않습니다.');}else $('status').textContent='검사 전 · 전체 시장 승률 없음';
}
async function req(url,opts){const ctl=new AbortController(),tid=setTimeout(()=>ctl.abort(),115000);try{const r=await fetch(url,{...opts,cache:'no-store',signal:ctl.signal});const v=await r.json();if(!r.ok||!v.ok)throw Error(v.error||'HTTP '+r.status);return v;}finally{clearTimeout(tid)}}
async function initSession(cfg){const v=await req('/api/winrate14c2/universe');if(!Array.isArray(v.items)||!v.items.length)throw Error('KRW 목록이 비었습니다');state={version:7,config:cfg,markets:v.items,results:{},failed:0};save();}
async function run(){if(running)return;let cfg;try{cfg=cfgOf()}catch(e){$('status').textContent='⚠️ '+e.message;return;}
 if(state&&signature(state.config)!==signature(cfg)){if(!confirm('설정/날짜가 달라졌습니다. 이전 C2 진행 기록을 지우고 새로 시작할까요?'))return;state=null;localStorage.removeItem(KEY);}
 running=true;stopRequested=false;hardFailure=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;
 try{if(!state)await initSession(cfg);state.failed=0;save();paint();let pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m));
  const batches=[];for(let i=0;i<pending.length;i+=2)batches.push(pending.slice(i,i+2));let pos=0,fail=0;
  async function worker(){while(!stopRequested&&!hardFailure){const b=batches[pos++];if(!b)break;let response=null;
   for(let attempt=0;attempt<3&&!stopRequested;attempt++){try{const v=await req('/api/winrate14c2/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:b})});
     if(v.errors||v.scanned!==b.length||!Array.isArray(v.results)||v.results.length!==b.length||v.results.some(x=>!b.includes(x.market)||!Array.isArray(x.events)||x.events.length!==x.signals||x.events.some(e=>!e.c||!e.c2||!e.c2_check)))throw Error('불완전한 배치 응답');
     response=v;break;}catch(e){if(attempt===2){fail++;$('status').textContent=`⚠️ ${b.join(', ')} 조회 실패: ${String(e.message||e)}. 다시 이어하기를 눌러 재검사할 수 있습니다.`;}else await new Promise(ok=>setTimeout(ok,1000*(attempt+1)));}}
   if(response){for(const x of response.results)state.results[x.market]=x;try{save()}catch{hardFailure=true;$('status').textContent='⚠️ 브라우저 저장공간 부족 · 진행 기록을 저장할 수 없습니다.';break;}paint();}
   if(fail>=4){hardFailure=true;break;}
  }}
  await Promise.all([worker(),worker()]);state.failed=fail;try{save()}catch{}paint();
  if(stopRequested&&!hardFailure)$('status').textContent+='\n일시 중지했습니다. 다시 누르면 남은 코인을 검사합니다.';
  if(fail)$('status').textContent+=`\n실패 배치 ${fail}개 · 다음 실행에서 누락 코인을 재조회합니다.`;
 }catch(e){$('status').textContent='⚠️ '+String(e.message||e)+' · 일부 성적을 전체 결과로 표시하지 않습니다.';}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}
}
$('run').onclick=run;
$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;$('status').textContent+='\n진행 중인 요청이 끝나면 일시 중지합니다.';};
$('reset').onclick=()=>{if(running)return;if(!confirm('현재 V11.14.7 C2 진행 기록만 삭제할까요?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(v){const s=String(v??'');return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!state||sampleResults().length!==state.markets.length||state.failed)return;
 const cols=['version','date','tp_pct','sl_pct','horizon_minutes','fee_pct_side','slip_pct_side','market','day_5m','c_signals','original_signal_time_kst','original_c_entry_time','c2_confirmation_time','c2_entry_time','first_c_signal','c_outcome','c_reason','c_entry','c_exit','c_minutes','c_net_pct','c2_check_status','c2_check_reason','c2_outcome','c2_reason','c2_entry','c2_exit','c2_minutes','c2_net_pct','original_volume_ratio','original_body_pct','original_pullback_pct'];
 const lines=[cols.join(',')];const prefix=['V11.14.7',state.config.date,state.config.tp,state.config.sl,state.config.horizon,state.config.fee,state.config.slip];
 function append(row){if(row.length!==cols.length-prefix.length)throw Error('CSV 열 개수 오류');lines.push([...prefix,...row].map(csvCell).join(','));}
 for(const x of sampleResults()){if(!x.signals){const r=Array(cols.length-prefix.length).fill('');r[0]=x.market;r[1]=x.observed_5m;r[2]=0;append(r);continue;}
  if(!Array.isArray(x.events)||x.events.length!==x.signals){$('status').textContent='⚠️ C 신호 내역이 일부 누락되었습니다. 재검사하세요.';return;}
  for(const e of x.events){const c=e.c||{},c2=e.c2||{},ck=e.c2_check||{},f=e.features||{};
   append([x.market,x.observed_5m,x.signals,e.signal_time_kst,e.c_entry_time_kst,e.c2_confirm_time_kst,e.c2_entry_time_kst,e.is_first_signal?1:0,c.outcome,c.reason,c.entry,c.exit,c.minutes,c.net_pct,ck.status,ck.reason,c2.outcome,c2.reason,c2.entry,c2.exit,c2.minutes,c2.net_pct,f.volume_ratio,f.body_pct,f.pullback_depth_pct]);}
 }
 const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`bithumb_full_krw_C_vs_C2_${state.config.date}_v11147.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
state=load();if(state)setConfig(state.config);paint();
</script></body></html>"""


def register_c2_lab(app,deps):
    api_get,get_markets,kst=deps['api_get'],deps['get_markets'],deps['KST']
    cache={'at':0,'names':None}
    mutex=Lock()

    def listed():
        with mutex:
            if cache['names'] is None or time.time()-cache['at']>600:
                markets=sorted({m for m in get_markets() if isinstance(m,str) and MARKET_RE.fullmatch(m)})
                if not markets:
                    raise ValueError('현재 빗썸 KRW 목록이 없습니다')
                cache.update(names=markets,at=time.time())
            return cache['names']

    @app.get('/backtest-winrate-c2')
    def c2_page_v11147():
        return render_template_string(C2_PAGE)

    @app.get('/api/winrate14c2/universe')
    def c2_universe_v11147():
        try:
            names=listed()
            return jsonify({'ok':True,'items':names,'count':len(names),
                            'universe_type':'current_KRW_listings','rule_version':'V11.14.7'})
        except Exception as e:
            return jsonify({'ok':False,'error':str(e)[:160]}),400

    @app.post('/api/winrate14c2/evaluate')
    def c2_evaluate_v11147():
        try:
            payload=request.get_json(silent=True)
            cfg=lab_valid_request(payload,kst)
            markets=payload.get('markets') if isinstance(payload,dict) else None
            if (not isinstance(markets,list) or not 1<=len(markets)<=2
                    or len(set(markets))!=len(markets)
                    or any(not isinstance(m,str) or not MARKET_RE.fullmatch(m) for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 검사합니다')
            allowed=set(listed())
            if any(m not in allowed for m in markets):
                raise ValueError('현재 빗썸 KRW 목록에 없는 종목입니다')
            results,errors=[],[]
            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks={pool.submit(c2_evaluate_symbol,api_get,m,cfg,kst):m for m in markets}
                for future in as_completed(tasks):
                    try:results.append(future.result())
                    except Exception as e:errors.append({'market':tasks[future],'error':str(e)[:120]})
            return jsonify({'ok':not errors,'scanned':len(markets),
                            'results':results,'errors':len(errors),
                            'error_markets':errors,'rule_version':'V11.14.7'})
        except Exception as e:
            return jsonify({'ok':False,'error':str(e)[:160]}),400


_v11146_register_daytrade_review=register_daytrade_review

def register_daytrade_review(app,deps):
    _v11146_register_daytrade_review(app,deps)
    register_c2_lab(app,deps)


# ---------------------------------------------------------------------------
# V11.14.8: Independent Bollinger(20,2) entry-time-only C filter study.
# Leaves ALL existing endpoints, C signals, cooldown, and execution untouched.
# Hypotheses are locked BEFORE results; no after-the-fact threshold tuning.
# ---------------------------------------------------------------------------


def bb_v11148_snapshot(rows, i):
    """20 closes ending at confirmed C signal i + 20 preceding closes.

    Uses only 21 consecutive COMPLETED 5M candles <= i. Population SD,
    SMA(20), upper/lower = SMA +/- 2*SD. No extra confirmation bar.
    """
    if i < 20 or not lab_contiguous(rows, i-20, i):
        return {'eligible':False, 'reason':'연속 21개 확정 5분봉 부족'}
    closes=[float(r['c']) for r in rows[i-20:i+1]]
    prev_mid=sum(closes[:20])/20
    mid=sum(closes[1:])/20
    variance=sum((x-mid)**2 for x in closes[1:])/20
    stdev=variance**0.5
    upper=mid+2*stdev
    lower=mid-2*stdev
    price=closes[-1]
    headroom=(upper/price-1)*100
    bandwidth=(upper-lower)/mid*100 if mid>0 else 0.0
    pct_b=(price-lower)/(upper-lower) if upper>lower else None
    above_mid=price>mid
    rising_mid=mid>prev_mid
    # Frozen hypotheses: A = trend, B = no upper-band chase.
    a=bool(above_mid and rising_mid)
    b=bool(above_mid and headroom>=0.50)
    return {'eligible':True, 'reason':None, 'middle':round(mid,10),
            'upper':round(upper,10), 'lower':round(lower,10),
            'middle_prev':round(prev_mid,10), 'headroom_pct':round(headroom,5),
            'bandwidth_pct':round(bandwidth,5),
            'percent_b':round(pct_b,5) if pct_b is not None else None,
            'trend_pass':a, 'headroom_pass':b, 'both_pass':a and b}


def bb_v11148_evaluate_symbol(api_get,market,cfg,kst):
    """Same fetch, C universe, cooldown, and C outcomes as V11.14.7."""
    rows,truncated=lab_fetch_5m(api_get,market,cfg,kst)
    day_start=datetime.combine(cfg['day'],_time.min,tzinfo=kst)
    day_end=day_start+timedelta(days=1)
    day_bars=[r for r in rows if day_start<=r['t']<day_end]
    n,valid,signals,unknown,cooldown=cfg['bars'],0,0,0,-1
    events=[]
    if len(rows)>=LAB_WARMUP_BARS+2:
        for i in range(LAB_WARMUP_BARS,len(rows)-1):
            if not day_start<=rows[i]['t']<day_end:
                continue
            if not lab_contiguous(rows,i-24,i):
                continue
            valid+=1
            if i<cooldown or 'C' not in lab_signals(rows,i):
                continue
            cooldown=i+n
            signals+=1
            c=lab_outcome_v11145(rows,i,cfg)
            if c['outcome']=='UNKNOWN':unknown+=1
            events.append({
                'signal_time_kst':rows[i]['t'].strftime('%Y-%m-%d %H:%M'),
                'c_entry_time_kst':(rows[i]['t']+timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M'),
                'is_first_signal':signals==1,
                'c':c,
                'bb':bb_v11148_snapshot(rows,i),
                'features':lab_c_signal_features(rows,i),
            })
    assert len(events)==signals
    return {'market':market,'observed_5m':len(day_bars),
            'longest_day_run':lab_longest_consecutive_5m(day_bars),
            'valid_windows':valid,'signals':signals,'unknown':unknown,
            'truncated':truncated,'events':events}


BB_V11148_PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.8 · 볼린저밴드 C 필터 연구실</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#1a2038;--card:#121a2e;--border:#35425f;--fg:#f2f5ff;--muted:#afc0db;--blue:#3867ee;--green:#4ade80;--warn:#ffc46c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1050px;margin:auto;padding:18px 14px 70px}h1{font-size:23px;margin:8px 0}h2{font-size:18px;margin:0 0 8px}
.panel{border:1px solid var(--border);background:var(--panel);border-radius:16px;padding:16px;margin-top:15px}
.muted,.hint{font-size:13px;color:var(--muted)}.note{padding:12px;background:#101a30;border-radius:10px;font-size:13px;line-height:1.75}
.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.field{display:flex;flex-direction:column;font-size:13px;color:var(--muted);gap:5px}
input,button,select{font:inherit}input,select{padding:10px;background:#10182d;border:1px solid var(--border);color:var(--fg);border-radius:8px;width:100%;min-width:0}
button{background:var(--blue);border:0;border-radius:9px;color:#fff;padding:12px 14px;font-weight:800;cursor:pointer}button:disabled{opacity:.48}.secondary{background:#364660}
.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px;align-items:center}a{color:#bcd0ff}progress{width:100%;height:15px;margin-top:14px}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.stat{border:1px solid var(--border);background:var(--card);border-radius:10px;padding:11px}
.stat small{font-size:12px;color:var(--muted);display:block}.stat strong{font-size:22px;display:block}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--border);padding:9px 7px;text-align:right;white-space:nowrap}td:first-child,th:first-child{text-align:left}
.status{font-size:13px;white-space:pre-wrap;color:var(--muted)}
@media(max-width:650px){.controls{grid-template-columns:repeat(2,minmax(0,1fr))}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><main>
<h1>🧪 V11.14.8 · C + 볼린저밴드 비교</h1>
<p class="muted">과거 모의 연구 전용 · V2 실전 BUY / V11.13 / C / C2 / RSI·MACD 검증실은 변경하지 않습니다.</p>
<section class="panel"><h2>미리 고정한 비교 조건</h2>
<div class="note">기존 C 신호, 재진입 대기 시간, <b>다음 5분봉 시가 진입</b>, 익절·손절·비용은 모두 동일합니다. 필터가 거르는 신호는 진입하지 않고 <b>0% 손익</b>으로 계산합니다.<br>
<b>볼린저밴드:</b> 확정된 5분봉 종가 20개, SMA20 ± 2×표준편차(모집단), 신호봉까지의 자료만 사용.<br>
<b>A · 추세:</b> 신호 종가가 중심선 위이고, 중심선이 직전 봉보다 상승.<br>
<b>B · 상단 여유:</b> 신호 종가가 중심선 위이고, 상단 밴드까지 상승 여유가 신호 종가 기준 <b>0.50% 이상</b>.<br>
<b>A+B:</b> 두 조건 모두 충족.<br>
이 수치는 실전 최적값이 아닌 <b>사전 가설</b>입니다. 신호 발생 이후 가격을 필터에 사용하지 않습니다.</div></section>
<section class="panel"><h2>검증 조건</h2>
<div class="controls">
<label class="field">과거 날짜 (KST)<input id="date" type="date" value="2026-11-01"></label>
<label class="field">익절 목표 (%)<input id="tp" type="number" min="0.3" max="10" step="0.1" value="1.5"></label>
<label class="field">손절 기준 (%)<input id="sl" type="number" min="0.2" max="8" step="0.1" value="1.0"></label>
<label class="field">최대 보유<select id="horizon"><option value="30">30분</option><option value="60" selected>60분</option><option value="120">120분</option></select></label>
<label class="field">편도 수수료 (%)<input id="fee" type="number" step="0.01" value="0.04" min="0" max="0.5"></label>
<label class="field">편도 슬리피지 (%)<input id="slip" type="number" step="0.01" value="0.05" min="0" max="0.5"></label>
</div>
<div class="actions"><button id="run">전체 KRW 검사 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">V11.14.8 기록 초기화</button><button id="csv" class="secondary" disabled>전체 비교 CSV 저장</button></div>
<progress id="progress" value="0" max="1"></progress><p id="status" class="status">검사 전</p>
<p class="hint">전체 검사 완료 전에는 비교 결과를 공개하지 않습니다. 같은 브라우저에서 중단 후 이어 할 수 있습니다. 검증에 현재 상장 중인 KRW 종목 목록을 사용하므로 생존자 편향이 있습니다.</p>
</section>
<section class="panel"><h2>데이터 완성도</h2><div id="coverage" class="stats">검사 전</div></section>
<section class="panel"><h2>전체 신호 · 공정 비교</h2><div id="summary" class="stats">검사 완료 후 표시</div>
<p class="hint">원본 C 전체 판정 결과와 지표 계산 가능한 공통 표본을 구분합니다. 같은 신호에서 필터가 제외한 거래를 0%로 처리한 원본 신호당 평균 손익도 보여줍니다.</p><div id="results" class="scroll">검사 완료 후 표시</div></section>
<section class="panel"><h2>코인별 첫 신호만</h2><div id="first" class="scroll">검사 완료 후 표시</div>
<p class="hint">한 코인에서 여러 번 발생한 신호의 영향도 확인합니다. 평균 수익률은 실제 포트폴리오 성적이 아닙니다.</p></section>
<section class="panel"><h2>주의사항</h2><p class="hint">수수료·슬리피지 설정을 차감합니다. 동일 봉에서 익절/손절 모두 닿으면 손절 우선, 손절 갭은 불리한 체결을 반영합니다. 11월 1~3일 자료로 규칙을 고르는 경우 이 날짜의 결과를 독립 검증으로 사용하면 안 됩니다. 실제 주문 기능은 없습니다.</p>
<a href="/backtest-winrate-c2">V11.14.7 C vs C2 검증실</a> · <a href="/backtest-winrate-c-validated">V11.14.5 기존 C 검증실</a></section>
</main><script>
'use strict';
const $=id=>document.getElementById(id),KEY='bithumb_v11148_bb_checkpoint_1';
let state=null,running=false,stopRequested=false,hardFailure=false;
const fields=['date','tp','sl','horizon','fee','slip'];
function cfgOf(){const c={};for(const k of fields)c[k]=$(k).value;if(!/^20\d{2}-\d{2}-\d{2}$/.test(c.date))throw Error('날짜를 선택하세요');for(const k of fields.slice(1)){if(!Number.isFinite(Number(c[k])))throw Error('설정 값 오류: '+k);}return c;}
function signature(c){return fields.map(k=>String(c[k])).join('|')}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function load(){try{const x=JSON.parse(localStorage.getItem(KEY)||'null');return x?.version===8&&Array.isArray(x.markets)&&x.config&&x.results?x:null;}catch{return null;}}
function setConfig(c){for(const k of fields)$(k).value=c[k];}
function sampleResults(){return state?state.markets.filter(x=>Object.prototype.hasOwnProperty.call(state.results,x)).map(x=>state.results[x]):[];}
function card(k,v){return `<div class="stat"><small>${esc(k)}</small><strong>${esc(v)}</strong></div>`;}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function fmt(x){return Number.isFinite(x)?x.toFixed(3)+'%':'—';}
function decided(e){return e?.c&&['TP','SL','TIME'].includes(e.c.outcome)&&Number.isFinite(e.c.net_pct)}
function stat(es){const n=es.length,sum=es.reduce((s,e)=>s+e.c.net_pct,0);return{n,wins:es.filter(e=>e.c.net_pct>0).length,tp:es.filter(e=>e.c.outcome==='TP').length,sl:es.filter(e=>e.c.outcome==='SL').length,tm:es.filter(e=>e.c.outcome==='TIME').length,sum,avg:n?sum/n:NaN,win:n?100*es.filter(e=>e.c.net_pct>0).length/n:NaN};}
function table(es){const cols=[['기존 C · 공통 표본',()=>true],['C + A · 중심선 상승',e=>e.bb.trend_pass===true],['C + B · 상단 여유',e=>e.bb.headroom_pass===true],['C + A+B',e=>e.bb.both_pass===true]];
 const header='<table><thead><tr><th>방식</th><th>진입</th><th>익절/손절/시간</th><th>순승률</th><th>거래당 평균</th><th>원본 신호당 평균 (보류=0%)</th><th>보류 건수</th><th>보류한 수익거래</th></tr></thead><tbody>';
 return header+cols.map(([name,test])=>{const kept=es.filter(test),removed=es.filter(e=>!test(e)),s=stat(kept);return `<tr><td>${esc(name)}</td><td>${s.n}</td><td>${s.tp}/${s.sl}/${s.tm}</td><td>${fmt(s.win)}</td><td>${fmt(s.avg)}</td><td>${fmt(es.length?s.sum/es.length:NaN)}</td><td>${removed.length}</td><td>${removed.filter(e=>e.c.net_pct>0).length}</td></tr>`;}).join('')+'</tbody></table>';
}
function renderResults(a){const events=a.flatMap(r=>(r.events||[]).map(e=>({...e,market:r.market}))),base=events.filter(decided),eligible=base.filter(e=>e.bb?.eligible===true),first=eligible.filter(e=>e.is_first_signal),all=stat(base),common=stat(eligible);
 $('summary').innerHTML=card('전체 C 발생',events.length+'건')+card('전체 C 판정',all.n+'건')+card('전체 C 평균 순손익',fmt(all.avg))+card('볼린저 계산 가능 · 판정',eligible.length+'건')+card('볼린저 계산 불가 · 판정',base.length-eligible.length+'건')+card('볼린저 공통표본 평균',fmt(common.avg));
 $('results').innerHTML='<p class="hint">같은 원본 C 신호 '+eligible.length+'건을 대상으로 비교. 계산 불가 및 미판정은 이 표에서 제외.</p>'+table(eligible);
 $('first').innerHTML='<p class="hint">코인별 첫 원본 C 신호 중 지표 계산 가능 + 손익 판정된 '+first.length+'건</p>'+table(first);
}
function paint(){const a=sampleResults(),n=a.length,total=state?.markets.length||0,complete=total>0&&n===total&&state?.failed===0,signals=a.reduce((s,r)=>s+r.signals,0);
 $('progress').max=total||1;$('progress').value=n;
 $('coverage').innerHTML=card('조회 종목',`${n}/${total}`)+card('분석 가능 종목',a.filter(x=>x.valid_windows>0).length+'개')+card('C 신호',signals+'건')+card('데이터 240봉 이상',a.filter(x=>x.observed_5m>=240).length+'개')+card('과거 페이지 상한',a.filter(x=>x.truncated).length+'개');
 if(complete){renderResults(a);$('csv').disabled=false;}else{$('summary').textContent='전체 KRW 검사 완료 후 표시';$('results').textContent='전체 KRW 검사 완료 후 표시';$('first').textContent='전체 KRW 검사 완료 후 표시';$('csv').disabled=true;}
 $('status').textContent=state?(complete?'✅ 전체 검사 완료':'⏳ 진행/중단 상태')+` · ${n}/${total}종목 · C 신호 ${signals}건`+(state.failed?' · 일부 요청 실패, 이어하기 필요':''):'검사 전 · 전체 시장 결과 없음';
}
async function req(url,opts){const ctl=new AbortController(),tid=setTimeout(()=>ctl.abort(),115000);try{const r=await fetch(url,{...opts,cache:'no-store',signal:ctl.signal}),v=await r.json();if(!r.ok||!v.ok)throw Error(v.error||'HTTP '+r.status);return v;}finally{clearTimeout(tid)}}
async function initSession(c){const v=await req('/api/winrate14bb/universe');if(!Array.isArray(v.items)||!v.items.length)throw Error('KRW 종목 목록이 없습니다');state={version:8,config:c,markets:v.items,results:{},failed:0};save();}
async function run(){if(running)return;let c;try{c=cfgOf()}catch(e){$('status').textContent='⚠️ '+e.message;return;}
 if(state&&signature(state.config)!==signature(c)){if(!confirm('날짜나 설정이 바뀌었습니다. V11.14.8의 이전 진행 기록만 지우고 시작할까요?'))return;state=null;localStorage.removeItem(KEY);}
 running=true;stopRequested=false;hardFailure=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;
 try{if(!state)await initSession(c);state.failed=0;save();paint();let pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m));const batches=[];for(let i=0;i<pending.length;i+=2)batches.push(pending.slice(i,i+2));let pos=0,fail=0;
  async function worker(){while(!stopRequested&&!hardFailure){const batch=batches[pos++];if(!batch)break;let response=null;
   for(let attempt=0;attempt<3&&!stopRequested;attempt++){try{let r=await req('/api/winrate14bb/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:batch})});
    if(r.errors||r.scanned!==batch.length||!Array.isArray(r.results)||r.results.length!==batch.length||r.results.some(x=>!batch.includes(x.market)||!Array.isArray(x.events)||x.events.length!==x.signals||x.events.some(e=>!e.c||!e.bb)))throw Error('불완전한 검사 결과');
    response=r;break;}catch(e){if(attempt===2){fail++;$('status').textContent='⚠️ '+batch.join(', ')+' 실패: '+String(e.message||e);}else await new Promise(ok=>setTimeout(ok,1000*(attempt+1)));}}
   if(response){for(const x of response.results)state.results[x.market]=x;try{save()}catch(e){hardFailure=true;$('status').textContent='⚠️ 브라우저 저장 공간 부족';break;}paint();}
   if(fail>=4){hardFailure=true;break;}
  }}
  await Promise.all([worker(),worker()]);state.failed=fail;try{save()}catch{}paint();
  if(stopRequested)$('status').textContent+='\n일시 중지했습니다. 다시 누르면 남은 종목부터 검사합니다.';
  if(fail)$('status').textContent+=`\n실패한 묶음 ${fail}개 · 다시 실행하면 누락된 종목을 재검사합니다.`;
 }catch(e){$('status').textContent='⚠️ '+String(e.message||e)+' · 부분 결과를 승률로 공개하지 않습니다.';}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}
}
$('run').onclick=run;
$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;$('status').textContent+='\n진행 중 요청이 끝나면 중지합니다.';};
$('reset').onclick=()=>{if(running)return;if(!confirm('V11.14.8 볼린저 검증 기록만 초기화할까요?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(v){const s=String(v??'');return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!state||sampleResults().length!==state.markets.length||state.failed)return;
 const cols=['version','date','tp_pct','sl_pct','horizon_minutes','fee_pct_side','slip_pct_side','market','observed_5m','c_signals','original_signal_time_kst','c_entry_time_kst','is_first_signal','c_outcome','c_reason','c_entry','c_exit','c_minutes','c_net_pct','bb_eligible','bb_reason','bb_middle','bb_upper','bb_lower','bb_middle_prev','bb_headroom_pct','bb_bandwidth_pct','bb_percent_b','bb_trend_pass','bb_headroom_pass','bb_both_pass','original_volume_ratio','original_body_pct','original_pullback_pct'];
 const rows=[cols.map(csvCell).join(',')];const prefix={version:'V11.14.8',date:state.config.date,tp_pct:state.config.tp,sl_pct:state.config.sl,horizon_minutes:state.config.horizon,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip};
 function append(obj){rows.push(cols.map(k=>csvCell(obj[k])).join(','));}
 for(const r of sampleResults()){if(!r.signals){append({...prefix,market:r.market,observed_5m:r.observed_5m,c_signals:0});continue;}
  if(!Array.isArray(r.events)||r.events.length!==r.signals){$('status').textContent='⚠️ CSV에 필요한 이벤트 누락';return;}
  for(const e of r.events){const c=e.c||{},b=e.bb||{},f=e.features||{};append({...prefix,market:r.market,observed_5m:r.observed_5m,c_signals:r.signals,original_signal_time_kst:e.signal_time_kst,c_entry_time_kst:e.c_entry_time_kst,is_first_signal:e.is_first_signal?1:0,c_outcome:c.outcome,c_reason:c.reason,c_entry:c.entry,c_exit:c.exit,c_minutes:c.minutes,c_net_pct:c.net_pct,bb_eligible:b.eligible?1:0,bb_reason:b.reason,bb_middle:b.middle,bb_upper:b.upper,bb_lower:b.lower,bb_middle_prev:b.middle_prev,bb_headroom_pct:b.headroom_pct,bb_bandwidth_pct:b.bandwidth_pct,bb_percent_b:b.percent_b,bb_trend_pass:b.eligible?(b.trend_pass?1:0):'',bb_headroom_pass:b.eligible?(b.headroom_pass?1:0):'',bb_both_pass:b.eligible?(b.both_pass?1:0):'',original_volume_ratio:f.volume_ratio,original_body_pct:f.body_pct,original_pullback_pct:f.pullback_depth_pct});}
 }
 const blob=new Blob(['\ufeff'+rows.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`bithumb_full_krw_C_BB_${state.config.date}_v11148.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
state=load();if(state)setConfig(state.config);paint();
</script></body></html>"""


def register_bb_v11148_lab(app,deps):
    api_get,get_markets,kst=deps['api_get'],deps['get_markets'],deps['KST']
    cache={'at':0,'names':None}
    mutex=Lock()

    def listed():
        with mutex:
            if cache['names'] is None or time.time()-cache['at']>600:
                markets=sorted({m for m in get_markets() if isinstance(m,str) and MARKET_RE.fullmatch(m)})
                if not markets:
                    raise ValueError('현재 빗썸 KRW 종목 목록이 없습니다')
                cache.update(names=markets,at=time.time())
            return cache['names']

    @app.get('/backtest-winrate-bb')
    def bb_page_v11148():
        return render_template_string(BB_V11148_PAGE)

    @app.get('/api/winrate14bb/universe')
    def bb_universe_v11148():
        try:
            names=listed()
            return jsonify({'ok':True,'items':names,'count':len(names),
                            'universe_type':'current_KRW_listings','rule_version':'V11.14.8'})
        except Exception as e:
            return jsonify({'ok':False,'error':str(e)[:160]}),400

    @app.post('/api/winrate14bb/evaluate')
    def bb_evaluate_v11148():
        try:
            payload=request.get_json(silent=True)
            cfg=lab_valid_request(payload,kst)
            markets=payload.get('markets') if isinstance(payload,dict) else None
            if (not isinstance(markets,list) or not 1<=len(markets)<=2
                    or len(set(markets))!=len(markets)
                    or any(not isinstance(m,str) or not MARKET_RE.fullmatch(m) for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 검사합니다')
            if any(m not in set(listed()) for m in markets):
                raise ValueError('현재 KRW 목록에 없는 종목입니다')
            results,errors=[],[]
            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks={pool.submit(bb_v11148_evaluate_symbol,api_get,m,cfg,kst):m for m in markets}
                for future in as_completed(tasks):
                    try:results.append(future.result())
                    except Exception as e:errors.append({'market':tasks[future],'error':str(e)[:120]})
            return jsonify({'ok':not errors,'scanned':len(markets),
                            'results':results,'errors':len(errors),
                            'error_markets':errors,'rule_version':'V11.14.8'})
        except Exception as e:
            return jsonify({'ok':False,'error':str(e)[:160]}),400


_v11147_register_daytrade_review=register_daytrade_review

def register_daytrade_review(app,deps):
    _v11147_register_daytrade_review(app,deps)
    register_bb_v11148_lab(app,deps)


# ---------------------------------------------------------------------------
# V11.14.9: C + Ichimoku 9/26/52 filter study (READ-ONLY separate route).
# Existing V2, C, C2, Bollinger and older endpoints are not changed.
# Signals, C cooldown, entry opening, costs & exits remain identical.
# Future-leakage prevention: cloud plotted AT i uses spans COMPUTED at i-26.
# ---------------------------------------------------------------------------

def ich_v11149_fetch_5m(api_get, market, cfg, kst):
    """Only for V11.14.9: fetch >= 8h history for unshifted 78-bar Ichimoku.

    Same data and outcome semantics as the historic C labs, but extended prehistory.
    No fabricated bars; no lookahead. Older labs keep their original fetch logic.
    """
    begin = datetime.combine(cfg["day"], _time.min, tzinfo=kst) - timedelta(hours=8)
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



def ich_v11149_midpoint(rows, end, length):
    """Donchian midpoint of observed HIGH/LOW over length bars ending at end."""
    subset=rows[end-length+1:end+1]
    return (max(r['h'] for r in subset)+min(r['l'] for r in subset))/2.0


def ich_v11149_snapshot(rows, i):
    """All values are known when C signal candle i CLOSES.

    Tenkan 9, Kijun 26 (at i).
    Senkou A/B *appearing* under the price at i are the values computed at
    i-26, 26 bars BEFORE signal candle i. 52-bar Span B at i-26 requires
    earliest i-77; require all 78 bars contiguous. Never read after i.
    This is the historically plotted cloud, not tomorrow's forward cloud.
    """
    if i < 77 or not lab_contiguous(rows, i-77, i):
        return {'eligible':False, 'reason':'일목 78개 연속 확정 5분봉 부족'}
    tenkan=ich_v11149_midpoint(rows,i,9)
    kijun=ich_v11149_midpoint(rows,i,26)
    anchor=i-26
    tenkan_anchor=ich_v11149_midpoint(rows,anchor,9)
    kijun_anchor=ich_v11149_midpoint(rows,anchor,26)
    senkou_a=(tenkan_anchor+kijun_anchor)/2
    senkou_b=ich_v11149_midpoint(rows,anchor,52)
    top=max(senkou_a,senkou_b)
    bottom=min(senkou_a,senkou_b)
    price=rows[i]['c']
    above_cloud=price>top
    line_pass=tenkan>kijun
    thickness_pct=(top-bottom)/bottom*100 if bottom>0 else None
    gap_pct=(price/top-1)*100 if top>0 else None
    return {'eligible':True,'reason':None,'tenkan':round(tenkan,10),
            'kijun':round(kijun,10),'senkou_a_now':round(senkou_a,10),
            'senkou_b_now':round(senkou_b,10),'cloud_top':round(top,10),
            'cloud_bottom':round(bottom,10),'price_gap_pct':round(gap_pct,5) if gap_pct is not None else None,
            'cloud_thickness_pct':round(thickness_pct,5) if thickness_pct is not None else None,
            'cloud_pass':bool(above_cloud),'line_pass':bool(line_pass),
            'both_pass':bool(above_cloud and line_pass)}


def ich_v11149_evaluate_symbol(api_get,market,cfg,kst):
    """Replays exactly the same C events as V11.14.8; never delays entry."""
    rows,truncated=ich_v11149_fetch_5m(api_get,market,cfg,kst)
    day_start=datetime.combine(cfg['day'],_time.min,tzinfo=kst)
    day_end=day_start+timedelta(days=1)
    day_bars=[r for r in rows if day_start<=r['t']<day_end]
    n,valid,signals,unknown,cooldown=cfg['bars'],0,0,0,-1
    events=[]
    if len(rows)>=LAB_WARMUP_BARS+2:
        for i in range(LAB_WARMUP_BARS,len(rows)-1):
            if not day_start<=rows[i]['t']<day_end:
                continue
            if not lab_contiguous(rows,i-24,i):
                continue
            valid+=1
            if i<cooldown or 'C' not in lab_signals(rows,i):
                continue
            cooldown=i+n
            signals+=1
            c=lab_outcome_v11145(rows,i,cfg)
            if c['outcome']=='UNKNOWN':unknown+=1
            events.append({
                'signal_time_kst':rows[i]['t'].strftime('%Y-%m-%d %H:%M'),
                'c_entry_time_kst':(rows[i]['t']+timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M'),
                'is_first_signal':signals==1,
                'c':c,
                'ich':ich_v11149_snapshot(rows,i),
                'features':lab_c_signal_features(rows,i),
            })
    assert len(events)==signals
    return {'market':market,'observed_5m':len(day_bars),
            'longest_day_run':lab_longest_consecutive_5m(day_bars),
            'valid_windows':valid,'signals':signals,'unknown':unknown,
            'truncated':truncated,'events':events}


ICH_V11149_PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.9 · 일목균형표 C 필터 연구실</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#1a2038;--card:#121a2e;--border:#35425f;--fg:#f2f5ff;--muted:#afc0db;--blue:#3867ee;--green:#4ade80;--warn:#ffc46c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1050px;margin:auto;padding:18px 14px 70px}h1{font-size:23px;margin:8px 0}h2{font-size:18px;margin:0 0 8px}
.panel{border:1px solid var(--border);background:var(--panel);border-radius:16px;padding:16px;margin-top:15px}
.muted,.hint{font-size:13px;color:var(--muted)}.note{padding:12px;background:#101a30;border-radius:10px;font-size:13px;line-height:1.75}
.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.field{display:flex;flex-direction:column;font-size:13px;color:var(--muted);gap:5px}
input,button,select{font:inherit}input,select{padding:10px;background:#10182d;border:1px solid var(--border);color:var(--fg);border-radius:8px;width:100%;min-width:0}
button{background:var(--blue);border:0;border-radius:9px;color:#fff;padding:12px 14px;font-weight:800;cursor:pointer}button:disabled{opacity:.48}.secondary{background:#364660}
.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px;align-items:center}a{color:#bcd0ff}progress{width:100%;height:15px;margin-top:14px}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.stat{border:1px solid var(--border);background:var(--card);border-radius:10px;padding:11px}
.stat small{font-size:12px;color:var(--muted);display:block}.stat strong{font-size:22px;display:block}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--border);padding:9px 7px;text-align:right;white-space:nowrap}td:first-child,th:first-child{text-align:left}
.status{font-size:13px;white-space:pre-wrap;color:var(--muted)}
@media(max-width:650px){.controls{grid-template-columns:repeat(2,minmax(0,1fr))}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><main>
<h1>🧪 V11.14.9 · C + 일목균형표 비교</h1>
<p class="muted">과거 모의 연구 전용 · V2 실전 BUY / V11.13 / C / C2 / RSI·MACD 검증실은 변경하지 않습니다.</p>
<section class="panel"><h2>미리 고정한 비교 조건</h2>
<div class="note">기존 C 신호, 재진입 대기, <b>다음 5분봉 시가 진입</b>, 익절·손절·비용은 동일합니다. 필터가 제외한 신호는 진입하지 않아 <b>0% 손익</b>으로 비교합니다.<br>
<b>일목균형표:</b> 5분봉 기준 전환선(9), 기준선(26), 선행스팬(9·26·52, 26봉 이동)을 사용합니다. 지표 계산에는 신호 확정 시점까지의 5분봉만 사용합니다.<br>
<b>A · 구름대 위:</b> C 신호봉 <b>종가 &gt; 현재 시점에 표시되는 구름대 상단</b>.<br>
<b>B · 전환선 우위:</b> C 신호봉 마감 시점의 <b>전환선(9) &gt; 기준선(26)</b>.<br>
<b>A+B:</b> 두 조건 모두 충족.<br>
<b>중요:</b> 시점 i의 구름대는 i-26봉에 계산된 선행스팬을 사용합니다. 미래 26봉을 읽지 않습니다. 후행스팬은 이번 가설에서 사용하지 않습니다.<br>
이는 실전 최적값이 아닌 사전 정의한 가설입니다. 이 연구실은 <b>주문을 실행하지 않습니다.</b></div></section>
<section class="panel"><h2>검증 조건</h2>
<div class="controls">
<label class="field">과거 날짜 (KST)<input id="date" type="date" value="2026-11-01"></label>
<label class="field">익절 목표 (%)<input id="tp" type="number" min="0.3" max="10" step="0.1" value="1.5"></label>
<label class="field">손절 기준 (%)<input id="sl" type="number" min="0.2" max="8" step="0.1" value="1.0"></label>
<label class="field">최대 보유<select id="horizon"><option value="30">30분</option><option value="60" selected>60분</option><option value="120">120분</option></select></label>
<label class="field">편도 수수료 (%)<input id="fee" type="number" step="0.01" value="0.04" min="0" max="0.5"></label>
<label class="field">편도 슬리피지 (%)<input id="slip" type="number" step="0.01" value="0.05" min="0" max="0.5"></label>
</div>
<div class="actions"><button id="run">전체 KRW 검사 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">V11.14.9 기록 초기화</button><button id="csv" class="secondary" disabled>전체 비교 CSV 저장</button></div>
<progress id="progress" value="0" max="1"></progress><p id="status" class="status">검사 전</p>
<p class="hint">전체 검사 완료 전에는 비교 결과를 공개하지 않습니다. 같은 브라우저에서 중단 후 이어 할 수 있습니다. 검증에 현재 상장 중인 KRW 종목 목록을 사용하므로 생존자 편향이 있습니다. 데이터 공백 시 일목 계산 불가 신호는 별도 집계합니다.</p>
</section>
<section class="panel"><h2>데이터 완성도</h2><div id="coverage" class="stats">검사 전</div></section>
<section class="panel"><h2>전체 신호 · 공정 비교</h2><div id="summary" class="stats">검사 완료 후 표시</div>
<p class="hint">원본 C 전체 판정 결과와 지표 계산 가능한 공통 표본을 구분합니다. 같은 신호에서 필터가 제외한 거래를 0%로 처리한 원본 신호당 평균 손익도 보여줍니다.</p><div id="results" class="scroll">검사 완료 후 표시</div></section>
<section class="panel"><h2>코인별 첫 신호만</h2><div id="first" class="scroll">검사 완료 후 표시</div>
<p class="hint">한 코인에서 여러 번 발생한 신호의 영향도 확인합니다. 평균 수익률은 실제 포트폴리오 성적이 아닙니다.</p></section>
<section class="panel"><h2>주의사항</h2><p class="hint">수수료·슬리피지 설정을 차감합니다. 동일 봉에서 익절/손절 모두 닿으면 손절 우선, 손절 갭은 불리한 체결을 반영합니다. 11월 1~3일 자료로 규칙을 고르는 경우 이 날짜의 결과를 독립 검증으로 사용하면 안 됩니다. 실제 주문 기능은 없습니다.</p>
<a href="/backtest-winrate-c2">V11.14.7 C vs C2 검증실</a> · <a href="/backtest-winrate-c-validated">V11.14.5 기존 C 검증실</a></section>
</main><script>

'use strict';
const $=id=>document.getElementById(id),KEY='bithumb_v11149_ich_checkpoint_1';
let state=null,running=false,stopRequested=false,hardFailure=false;
const fields=['date','tp','sl','horizon','fee','slip'];
function cfgOf(){const c={};for(const k of fields)c[k]=$(k).value;if(!/^20\d{2}-\d{2}-\d{2}$/.test(c.date))throw Error('날짜를 선택하세요');for(const k of fields.slice(1)){if(!Number.isFinite(Number(c[k])))throw Error('설정 값 오류: '+k);}return c;}
function signature(c){return fields.map(k=>String(c[k])).join('|')}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function load(){try{const x=JSON.parse(localStorage.getItem(KEY)||'null');return x?.version===9&&Array.isArray(x.markets)&&x.config&&x.results?x:null;}catch{return null;}}
function setConfig(c){for(const k of fields)$(k).value=c[k];}
function sampleResults(){return state?state.markets.filter(x=>Object.prototype.hasOwnProperty.call(state.results,x)).map(x=>state.results[x]):[];}
function card(k,v){return `<div class="stat"><small>${esc(k)}</small><strong>${esc(v)}</strong></div>`;}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function fmt(x){return Number.isFinite(x)?x.toFixed(3)+'%':'—';}
function decided(e){return e?.c&&['TP','SL','TIME'].includes(e.c.outcome)&&Number.isFinite(e.c.net_pct)}
function stat(es){const n=es.length,sum=es.reduce((s,e)=>s+e.c.net_pct,0);return{n,wins:es.filter(e=>e.c.net_pct>0).length,tp:es.filter(e=>e.c.outcome==='TP').length,sl:es.filter(e=>e.c.outcome==='SL').length,tm:es.filter(e=>e.c.outcome==='TIME').length,sum,avg:n?sum/n:NaN,win:n?100*es.filter(e=>e.c.net_pct>0).length/n:NaN};}
function table(es){const cols=[['기존 C · 공통 표본',()=>true],['C + A · 구름대 위',e=>e.ich.cloud_pass===true],['C + B · 전환선 우위',e=>e.ich.line_pass===true],['C + A+B',e=>e.ich.both_pass===true]];
 const header='<table><thead><tr><th>방식</th><th>진입</th><th>익절/손절/시간</th><th>순승률</th><th>거래당 평균</th><th>원본 신호당 평균 (보류=0%)</th><th>보류 건수</th><th>보류한 수익거래</th></tr></thead><tbody>';
 return header+cols.map(([name,test])=>{const kept=es.filter(test),removed=es.filter(e=>!test(e)),s=stat(kept);return `<tr><td>${esc(name)}</td><td>${s.n}</td><td>${s.tp}/${s.sl}/${s.tm}</td><td>${fmt(s.win)}</td><td>${fmt(s.avg)}</td><td>${fmt(es.length?s.sum/es.length:NaN)}</td><td>${removed.length}</td><td>${removed.filter(e=>e.c.net_pct>0).length}</td></tr>`;}).join('')+'</tbody></table>';
}
function renderResults(a){const events=a.flatMap(r=>(r.events||[]).map(e=>({...e,market:r.market}))),base=events.filter(decided),eligible=base.filter(e=>e.ich?.eligible===true),first=eligible.filter(e=>e.is_first_signal),all=stat(base),common=stat(eligible);
 $('summary').innerHTML=card('전체 C 발생',events.length+'건')+card('전체 C 판정',all.n+'건')+card('전체 C 평균 순손익',fmt(all.avg))+card('일목 계산 가능 · 판정',eligible.length+'건')+card('일목 계산 불가 · 판정',base.length-eligible.length+'건')+card('일목 공통표본 평균',fmt(common.avg));
 $('results').innerHTML='<p class="hint">같은 원본 C 신호 '+eligible.length+'건을 대상으로 비교. 계산 불가 및 미판정은 이 표에서 제외.</p>'+table(eligible);
 $('first').innerHTML='<p class="hint">코인별 첫 원본 C 신호 중 지표 계산 가능 + 손익 판정된 '+first.length+'건</p>'+table(first);
}
function paint(){const a=sampleResults(),n=a.length,total=state?.markets.length||0,complete=total>0&&n===total&&state?.failed===0,signals=a.reduce((s,r)=>s+r.signals,0);
 $('progress').max=total||1;$('progress').value=n;
 $('coverage').innerHTML=card('조회 종목',`${n}/${total}`)+card('분석 가능 종목',a.filter(x=>x.valid_windows>0).length+'개')+card('C 신호',signals+'건')+card('데이터 240봉 이상',a.filter(x=>x.observed_5m>=240).length+'개')+card('과거 페이지 상한',a.filter(x=>x.truncated).length+'개');
 if(complete){renderResults(a);$('csv').disabled=false;}else{$('summary').textContent='전체 KRW 검사 완료 후 표시';$('results').textContent='전체 KRW 검사 완료 후 표시';$('first').textContent='전체 KRW 검사 완료 후 표시';$('csv').disabled=true;}
 $('status').textContent=state?(complete?'✅ 전체 검사 완료':'⏳ 진행/중단 상태')+` · ${n}/${total}종목 · C 신호 ${signals}건`+(state.failed?' · 일부 요청 실패, 이어하기 필요':''):'검사 전 · 전체 시장 결과 없음';
}
async function req(url,opts){const ctl=new AbortController(),tid=setTimeout(()=>ctl.abort(),115000);try{const r=await fetch(url,{...opts,cache:'no-store',signal:ctl.signal}),v=await r.json();if(!r.ok||!v.ok)throw Error(v.error||'HTTP '+r.status);return v;}finally{clearTimeout(tid)}}
async function initSession(c){const v=await req('/api/winrate14ich/universe');if(!Array.isArray(v.items)||!v.items.length)throw Error('KRW 종목 목록이 없습니다');state={version:9,config:c,markets:v.items,results:{},failed:0};save();}
async function run(){if(running)return;let c;try{c=cfgOf()}catch(e){$('status').textContent='⚠️ '+e.message;return;}
 if(state&&signature(state.config)!==signature(c)){if(!confirm('날짜나 설정이 바뀌었습니다. V11.14.9의 이전 진행 기록만 지우고 시작할까요?'))return;state=null;localStorage.removeItem(KEY);}
 running=true;stopRequested=false;hardFailure=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;
 try{if(!state)await initSession(c);state.failed=0;save();paint();let pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m));const batches=[];for(let i=0;i<pending.length;i+=2)batches.push(pending.slice(i,i+2));let pos=0,fail=0;
  async function worker(){while(!stopRequested&&!hardFailure){const batch=batches[pos++];if(!batch)break;let response=null;
   for(let attempt=0;attempt<3&&!stopRequested;attempt++){try{let r=await req('/api/winrate14ich/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:batch})});
    if(r.errors||r.scanned!==batch.length||!Array.isArray(r.results)||r.results.length!==batch.length||r.results.some(x=>!batch.includes(x.market)||!Array.isArray(x.events)||x.events.length!==x.signals||x.events.some(e=>!e.c||!e.ich)))throw Error('불완전한 검사 결과');
    response=r;break;}catch(e){if(attempt===2){fail++;$('status').textContent='⚠️ '+batch.join(', ')+' 실패: '+String(e.message||e);}else await new Promise(ok=>setTimeout(ok,1000*(attempt+1)));}}
   if(response){for(const x of response.results)state.results[x.market]=x;try{save()}catch(e){hardFailure=true;$('status').textContent='⚠️ 브라우저 저장 공간 부족';break;}paint();}
   if(fail>=4){hardFailure=true;break;}
  }}
  await Promise.all([worker(),worker()]);state.failed=fail;try{save()}catch{}paint();
  if(stopRequested)$('status').textContent+='\n일시 중지했습니다. 다시 누르면 남은 종목부터 검사합니다.';
  if(fail)$('status').textContent+=`\n실패한 묶음 ${fail}개 · 다시 실행하면 누락된 종목을 재검사합니다.`;
 }catch(e){$('status').textContent='⚠️ '+String(e.message||e)+' · 부분 결과를 승률로 공개하지 않습니다.';}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}
}
$('run').onclick=run;
$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;$('status').textContent+='\n진행 중 요청이 끝나면 중지합니다.';};
$('reset').onclick=()=>{if(running)return;if(!confirm('V11.14.9 일목 검증 기록만 초기화할까요?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(v){const s=String(v??'');return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!state||sampleResults().length!==state.markets.length||state.failed)return;
 const cols=['version','date','tp_pct','sl_pct','horizon_minutes','fee_pct_side','slip_pct_side','market','observed_5m','c_signals','original_signal_time_kst','c_entry_time_kst','is_first_signal','c_outcome','c_reason','c_entry','c_exit','c_minutes','c_net_pct','ich_eligible','ich_reason','ich_tenkan','ich_kijun','ich_senkou_a_now','ich_senkou_b_now','ich_cloud_top','ich_cloud_bottom','ich_price_gap_pct','ich_cloud_thickness_pct','ich_cloud_pass','ich_line_pass','ich_both_pass','original_volume_ratio','original_body_pct','original_pullback_pct'];
 const rows=[cols.map(csvCell).join(',')];const prefix={version:'V11.14.9',date:state.config.date,tp_pct:state.config.tp,sl_pct:state.config.sl,horizon_minutes:state.config.horizon,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip};
 function append(obj){rows.push(cols.map(k=>csvCell(obj[k])).join(','));}
 for(const r of sampleResults()){if(!r.signals){append({...prefix,market:r.market,observed_5m:r.observed_5m,c_signals:0});continue;}
  if(!Array.isArray(r.events)||r.events.length!==r.signals){$('status').textContent='⚠️ CSV에 필요한 이벤트 누락';return;}
  for(const e of r.events){const c=e.c||{},s=e.ich||{},f=e.features||{};append({...prefix,market:r.market,observed_5m:r.observed_5m,c_signals:r.signals,original_signal_time_kst:e.signal_time_kst,c_entry_time_kst:e.c_entry_time_kst,is_first_signal:e.is_first_signal?1:0,c_outcome:c.outcome,c_reason:c.reason,c_entry:c.entry,c_exit:c.exit,c_minutes:c.minutes,c_net_pct:c.net_pct,ich_eligible:s.eligible?1:0,ich_reason:s.reason,ich_tenkan:s.tenkan,ich_kijun:s.kijun,ich_senkou_a_now:s.senkou_a_now,ich_senkou_b_now:s.senkou_b_now,ich_cloud_top:s.cloud_top,ich_cloud_bottom:s.cloud_bottom,ich_price_gap_pct:s.price_gap_pct,ich_cloud_thickness_pct:s.cloud_thickness_pct,ich_cloud_pass:s.eligible?(s.cloud_pass?1:0):'',ich_line_pass:s.eligible?(s.line_pass?1:0):'',ich_both_pass:s.eligible?(s.both_pass?1:0):'',original_volume_ratio:f.volume_ratio,original_body_pct:f.body_pct,original_pullback_pct:f.pullback_depth_pct});}
 }
 const blob=new Blob(['\ufeff'+rows.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`bithumb_full_krw_C_ICH_${state.config.date}_v11149.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
state=load();if(state)setConfig(state.config);paint();

</script></body></html>"""


def register_ich_v11149_lab(app,deps):
    api_get,get_markets,kst=deps['api_get'],deps['get_markets'],deps['KST']
    cache={'at':0,'names':None}
    mutex=Lock()

    def listed():
        with mutex:
            if cache['names'] is None or time.time()-cache['at']>600:
                markets=sorted({m for m in get_markets() if isinstance(m,str) and MARKET_RE.fullmatch(m)})
                if not markets:
                    raise ValueError('현재 빗썸 KRW 종목 목록이 없습니다')
                cache.update(names=markets,at=time.time())
            return cache['names']

    @app.get('/backtest-winrate-ich')
    def ich_page_v11149():
        return render_template_string(ICH_V11149_PAGE)

    @app.get('/api/winrate14ich/universe')
    def ich_universe_v11149():
        try:
            names=listed()
            return jsonify({'ok':True,'items':names,'count':len(names),
                            'universe_type':'current_KRW_listings','rule_version':'V11.14.9'})
        except Exception as e:
            return jsonify({'ok':False,'error':str(e)[:160]}),400

    @app.post('/api/winrate14ich/evaluate')
    def ich_evaluate_v11149():
        try:
            payload=request.get_json(silent=True)
            cfg=lab_valid_request(payload,kst)
            markets=payload.get('markets') if isinstance(payload,dict) else None
            if (not isinstance(markets,list) or not 1<=len(markets)<=2
                    or len(set(markets))!=len(markets)
                    or any(not isinstance(m,str) or not MARKET_RE.fullmatch(m) for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 검사합니다')
            allowed=set(listed())
            if any(m not in allowed for m in markets):
                raise ValueError('현재 KRW 목록에 없는 종목입니다')
            results,errors=[],[]
            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks={pool.submit(ich_v11149_evaluate_symbol,api_get,m,cfg,kst):m for m in markets}
                for future in as_completed(tasks):
                    try:results.append(future.result())
                    except Exception as e:errors.append({'market':tasks[future],'error':str(e)[:120]})
            return jsonify({'ok':not errors,'scanned':len(markets),
                            'results':results,'errors':len(errors),
                            'error_markets':errors,'rule_version':'V11.14.9'})
        except Exception as e:
            return jsonify({'ok':False,'error':str(e)[:160]}),400


_v11148_register_daytrade_review=register_daytrade_review

def register_daytrade_review(app,deps):
    _v11148_register_daytrade_review(app,deps)
    register_ich_v11149_lab(app,deps)


# ---------------------------------------------------------------------------
# V11.14.10: Independent higher-timeframe MACD(12,26,9) C filter experiment.
# Backward-only daily/weekly completed candles. No future candles, no edits to
# V2, V11.13 BUY, C signal definitions, outcomes, or previous research labs.
# All threshold choices are preregistered hypotheses, NOT tuned performance.
# ---------------------------------------------------------------------------

HTF_V111410_MIN_BARS = 60
HTF_V111410_RULES = {
    'day': {'period': timedelta(days=1), 'cross_window':3, 'near_gap_pct':0.12,
            'api_path':'/v1/candles/days'},
    'week': {'period': timedelta(days=7), 'cross_window':2, 'near_gap_pct':0.25,
             'api_path':'/v1/candles/weeks'},
}


def htf_v111410_fetch(api_get, market, cfg, kst, timeframe):
    """Fetch OHLC candle starts before target date end; later partial candles
    may be returned but htf_v111410_snapshot excludes them at EVERY C event.
    Bithumb's `to` is KST exclusive, 200 candles max. No pagination necessary
    for 130-session MACD warmup in this experiment.
    """
    rule=HTF_V111410_RULES[timeframe]
    day_end=datetime.combine(cfg['day']+timedelta(days=1),_time.min,tzinfo=kst)
    raw=api_get(rule['api_path'], {'market':market,'count':130,
             'to':day_end.strftime('%Y-%m-%d %H:%M:%S')})
    if not isinstance(raw,list):
        raise ValueError('상위 시간봉 API 응답 형식 오류: '+timeframe)
    found={}
    for bar in raw:
        if not isinstance(bar,dict):continue
        try:
            ts=local_time(bar['candle_date_time_kst'],kst)
            op,hi,lo,cl=(float(bar[k]) for k in
                           ('opening_price','high_price','low_price','trade_price'))
            if not (all(isfinite(x) and x>0 for x in (op,hi,lo,cl))
                    and lo<=min(op,cl) and hi>=max(op,cl) and lo<=hi):
                continue
            if ts<day_end:
                found[ts]={'t':ts,'c':cl}
        except (KeyError,TypeError,ValueError,OverflowError):
            continue
    return [found[t] for t in sorted(found)]


def htf_v111410_macd_histogram(closes):
    """Proper SMA-seeded 12/26 EMA and 9 EMA signal; returns last 4
    completed MACD/signal/hist values, or None when history is short.
    """
    if len(closes)<HTF_V111410_MIN_BARS:return None
    alpha_f,alpha_s,alpha_g=2/13,2/27,2/10
    ema_f=sum(closes[:12])/12
    ema_s=sum(closes[:26])/26
    diffs=[]
    for idx,price in enumerate(closes):
        if idx>=12:ema_f=alpha_f*price+(1-alpha_f)*ema_f
        if idx>=26:ema_s=alpha_s*price+(1-alpha_s)*ema_s
        if idx>=25:diffs.append(ema_f-ema_s)
    if len(diffs)<12:return None
    ema_g=sum(diffs[:9])/9
    out=[]
    for idx,macd in enumerate(diffs):
        if idx>=9:ema_g=alpha_g*macd+(1-alpha_g)*ema_g
        if idx>=8:out.append({'macd':macd,'signal':ema_g,'hist':macd-ema_g})
    return out


def htf_v111410_snapshot(raw, signal_close, timeframe):
    """Never use an uncompleted day/week candle, even if API returns it.
    signal_close is time the original 5-minute C candle is CONFIRMED.
    Candles after that instant cannot enter the EMA or cross calculation.
    """
    rule=HTF_V111410_RULES[timeframe]
    period=rule['period']
    available=[x for x in raw if x['t']+period<=signal_close]
    if not available:
        return {'eligible':False,'reason':'확정된 '+('일봉' if timeframe=='day' else '주봉')+' 없음'}
    # A missing most-recent completed candle is not a fresh crossover signal.
    if signal_close-(available[-1]['t']+period)>=period:
        return {'eligible':False,'reason':'최근 확정 봉 누락'}
    # Require 60 most recent COMPLETED uninterrupted periods; never blend over
    # missing daily/weekly bars as if the missing bar was a flat candle.
    start=0
    for j in range(len(available)-1,0,-1):
        if available[j]['t']-available[j-1]['t']!=period:
            start=j
            break
    stable=available[start:]
    if len(stable)<HTF_V111410_MIN_BARS:
        return {'eligible':False,'reason':'연속 확정 '+('일봉' if timeframe=='day' else '주봉')+' 60개 미만',
                'available_bars':len(stable)}
    arr=htf_v111410_macd_histogram([x['c'] for x in stable])
    if arr is None or len(arr)<4:return {'eligible':False,'reason':'MACD 계산 실패'}
    curr,prev,prev2=arr[-1],arr[-2],arr[-3]
    cross_age=None
    window=rule['cross_window']
    for age in range(window):
        j=len(arr)-1-age
        if j>0 and arr[j-1]['hist']<=0<arr[j]['hist'] and curr['hist']>0:
            cross_age=age
            break
    close=stable[-1]['c']
    hist_pct=curr['hist']/close*100
    prior_pct=prev['hist']/close*100
    before_pct=prev2['hist']/close*100
    near=(curr['hist']<0 and -rule['near_gap_pct']<=hist_pct
          and hist_pct>prior_pct>before_pct)
    crossed=(cross_age is not None)
    return {'eligible':True,'reason':None,
            'latest_closed_bar_end_kst':(stable[-1]['t']+period).strftime('%Y-%m-%d %H:%M'),
            'available_bars':len(stable),'macd_pct':round(curr['macd']/close*100,6),
            'signal_pct':round(curr['signal']/close*100,6),
            'hist_pct':round(hist_pct,6),'hist_prev_pct':round(prior_pct,6),
            'hist_prev2_pct':round(before_pct,6),
            'cross_age_bars':cross_age,'cross_pass':crossed,
            'near_pass':bool(near),'either_pass':bool(crossed or near)}


def htf_v111410_evaluate_symbol(api_get,market,cfg,kst):
    """Preserve the EXACT original C 5m signal universe and C outcomes.
    Daily and weekly lookback data are only explanatory filters at C close.
    """
    rows,truncated=lab_fetch_5m(api_get,market,cfg,kst)
    day_start=datetime.combine(cfg['day'],_time.min,tzinfo=kst)
    day_end=day_start+timedelta(days=1)
    day_rows=[r for r in rows if day_start<=r['t']<day_end]
    n,valid,signals,unknown,cooldown=cfg['bars'],0,0,0,-1
    events=[]
    if len(rows)>=LAB_WARMUP_BARS+2:
        for i in range(LAB_WARMUP_BARS,len(rows)-1):
            if not day_start<=rows[i]['t']<day_end:continue
            if not lab_contiguous(rows,i-24,i):continue
            valid+=1
            if i<cooldown or 'C' not in lab_signals(rows,i):continue
            cooldown=i+n
            signals+=1
            out=lab_outcome_v11145(rows,i,cfg)
            if out['outcome']=='UNKNOWN':unknown+=1
            events.append({'signal_time_kst':rows[i]['t'].strftime('%Y-%m-%d %H:%M'),
                   'c_entry_time_kst':(rows[i]['t']+timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M'),
                   'is_first_signal':signals==1,'c':out,
                   'features':lab_c_signal_features(rows,i),
                   '_signal_close':rows[i]['t']+timedelta(minutes=5)})
    if events:
        daily=htf_v111410_fetch(api_get,market,cfg,kst,'day')
        weekly=htf_v111410_fetch(api_get,market,cfg,kst,'week')
        # Many C events per symbol are possible; run same causal snapshot each time.
        for e in events:
            e['htf']={'day':htf_v111410_snapshot(daily,e['_signal_close'],'day'),
                      'week':htf_v111410_snapshot(weekly,e['_signal_close'],'week')}
            del e['_signal_close']
    assert len(events)==signals
    return {'market':market,'observed_5m':len(day_rows),
          'longest_day_run':lab_longest_consecutive_5m(day_rows),
          'valid_windows':valid,'signals':signals,'unknown':unknown,
          'truncated':truncated,'events':events}

HTF_V111410_PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.10 · 일봉·주봉 MACD 비교 검증실</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#1a2038;--card:#121a2e;--border:#35425f;--fg:#f2f5ff;--muted:#afc0db;--blue:#3867ee;--green:#4ade80;--warn:#ffc46c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1050px;margin:auto;padding:18px 14px 70px}h1{font-size:23px;margin:8px 0}h2{font-size:18px;margin:0 0 8px}
.panel{border:1px solid var(--border);background:var(--panel);border-radius:16px;padding:16px;margin-top:15px}
.muted,.hint{font-size:13px;color:var(--muted)}.note{padding:12px;background:#101a30;border-radius:10px;font-size:13px;line-height:1.75}
.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.field{display:flex;flex-direction:column;font-size:13px;color:var(--muted);gap:5px}
input,button,select{font:inherit}input,select{padding:10px;background:#10182d;border:1px solid var(--border);color:var(--fg);border-radius:8px;width:100%;min-width:0}
button{background:var(--blue);border:0;border-radius:9px;color:#fff;padding:12px 14px;font-weight:800;cursor:pointer}button:disabled{opacity:.48}.secondary{background:#364660}
.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px;align-items:center}a{color:#bcd0ff}progress{width:100%;height:15px;margin-top:14px}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.stat{border:1px solid var(--border);background:var(--card);border-radius:10px;padding:11px}
.stat small{font-size:12px;color:var(--muted);display:block}.stat strong{font-size:22px;display:block}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--border);padding:9px 7px;text-align:right;white-space:nowrap}td:first-child,th:first-child{text-align:left}
.status{font-size:13px;white-space:pre-wrap;color:var(--muted)}
@media(max-width:650px){.controls{grid-template-columns:repeat(2,minmax(0,1fr))}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><main>
<h1>🧪 V11.14.10 · C + 일봉·주봉 MACD 비교</h1>
<p class="muted">별도 과거 모의검증 · 기존 V2 BUY / C / C2 / 볼린저 / 일목 및 예전 검증실은 변경하지 않습니다.</p>
<section class="panel"><h2>이번 연구에서 고정한 조건</h2>
<div class="note">
<b>기본:</b> 기존 C 신호 그대로, 다음 5분봉 시가 진입과 익절·손절·비용 동일. MACD는 C 신호가 확정되는 시점에 <b>완전히 종료된 일봉·주봉만</b> 사용합니다. 진행 중인 일/주봉은 제외합니다.<br>
<b>일봉 D 확정:</b> MACD(12,26,9) 히스토그램이 0을 상향 돌파한 지 <b>최근 3개 확정 일봉 이내</b>이고 지금도 0 초과.<br>
<b>일봉 D 직전:</b> 아직 MACD &lt; 시그널(히스토그램 음수)이나, 최근 2번 연속으로 차이가 줄고 히스토그램이 종가 대비 <b>-0.12% 이상 ~ 0% 미만</b>.<br>
<b>주봉 W 확정:</b> 골든크로스가 <b>최근 2개 확정 주봉 이내</b> 발생했고 지금도 0 초과.<br>
<b>주봉 W 직전:</b> 2주 연속 히스토그램 개선, 음수이면서 종가 대비 <b>-0.25% 이상 ~ 0% 미만</b>.<br>
<b>각 시간대 OR:</b> 확정 또는 직전. <b>일봉 OR 주봉:</b> 각 시간대의 확정/직전 중 하나라도 통과(적어도 한쪽 계산 가능하면 평가).<br>
<b>계산 불가:</b> 연속 확정 60개 미만이면 실패가 아닌 별도 집계. 지표 계산 불가와 손익 미판정은 공정 비교에서 제외합니다.<br>
<i>교차 직전 기준은 사전에 정한 실험용 수치이며 미래의 교차 성공을 알고 적용한 게 아닙니다. 지난 기간으로 조정하면 새 날짜로 별도 검증해야 합니다.</i>
</div></section>
<section class="panel"><h2>검증 조건</h2><div class="controls">
<label class="field">검사할 과거 날짜 (KST)<input id="date" type="date" value="2026-11-01"></label>
<label class="field">익절 (%)<input id="tp" type="number" min="0.3" max="10" step="0.1" value="1.5"></label>
<label class="field">손절 (%)<input id="sl" type="number" min="0.2" max="8" step="0.1" value="1.0"></label>
<label class="field">최대 보유<select id="horizon"><option value="30">30분</option><option value="60" selected>60분</option><option value="120">120분</option></select></label>
<label class="field">편도 수수료 (%)<input id="fee" type="number" min="0" max="0.5" step="0.01" value="0.04"></label>
<label class="field">편도 슬리피지 (%)<input id="slip" type="number" min="0" max="0.5" step="0.01" value="0.05"></label>
</div><div class="actions"><button id="run">전체 KRW 검사 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">V11.14.10 기록 초기화</button><button id="csv" class="secondary" disabled>전체 MACD 비교 CSV 저장</button></div>
<progress id="progress" value="0" max="1"></progress><p id="status" class="status">검사 전</p>
<p class="hint">검사가 끝나기 전에는 결과를 공개하지 않습니다. 브라우저 저장으로 이어하기 가능. 일/주봉이 추가 조회되므로 기존 C 검사보다 느릴 수 있습니다.</p></section>
<section class="panel"><h2>전체 검사 상태</h2><div id="coverage" class="stats">검사 전</div></section>
<section class="panel"><h2>공정 비교 · 모든 신호</h2><div id="summary" class="stats">검사 후 공개</div>
<p class="hint">각 필터는 해당 시간봉이 계산 가능한 원본 C 신호의 성적과 비교합니다. 계산 불가 종목은 자동으로 손실회피 성공으로 취급하지 않습니다. 보류=0%는 신호당 수치이지 포트폴리오 수익률이 아닙니다.</p><div id="results" class="scroll">검사 후 공개</div></section>
<section class="panel"><h2>코인별 첫 C 신호만 따로 확인</h2><div id="first" class="scroll">검사 후 공개</div></section>
<section class="panel"><h2>검증 한계</h2><p class="hint">현재 상장 중인 KRW 목록을 과거 날짜에 조회하므로 생존자 편향이 있습니다. 동일 코인 내 여러 신호는 독립 표본이 아닙니다. 고가·저가 기반 결과는 실제 체결을 보장하지 않습니다. 11/1~3 자료로 지표를 설계했다면 별도 미사용 날짜로 재검증해야 합니다. 실전 주문 기능은 없습니다.</p>
<a href="/backtest-winrate-ich">V11.14.9 일목</a> · <a href="/backtest-winrate-bb">V11.14.8 볼린저</a> · <a href="/backtest-winrate-c2">V11.14.7 C2</a> · <a href="/">V2 메인</a></section>
</main><script>
'use strict';
const $=id=>document.getElementById(id),KEY='bithumb_v111410_htfmacd_checkpoint_1';
let state=null,running=false,stopRequested=false,hardFailure=false;
const fields=['date','tp','sl','horizon','fee','slip'];
function cfgOf(){const c={};for(const k of fields)c[k]=$(k).value;if(!/^20\d{2}-\d{2}-\d{2}$/.test(c.date))throw Error('날짜를 선택해');for(const k of fields.slice(1))if(!Number.isFinite(Number(c[k])))throw Error('입력 확인: '+k);return c;}
function signature(c){return fields.map(k=>String(c[k])).join('|')}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function load(){try{const x=JSON.parse(localStorage.getItem(KEY)||'null');return x?.version===10&&Array.isArray(x.markets)&&x.config&&x.results?x:null}catch{return null}}
function setConfig(c){for(const k of fields)$(k).value=c[k];}
function sampleResults(){return state?state.markets.filter(x=>Object.prototype.hasOwnProperty.call(state.results,x)).map(x=>state.results[x]):[];}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function card(k,v){return `<div class="stat"><small>${esc(k)}</small><strong>${esc(v)}</strong></div>`;}
function fmt(x){return Number.isFinite(x)?x.toFixed(3)+'%':'—';}
function decided(e){return e?.c&&['TP','SL','TIME'].includes(e.c.outcome)&&Number.isFinite(e.c.net_pct);}
function stat(es){const n=es.length,sum=es.reduce((s,e)=>s+e.c.net_pct,0);return {n,sum,avg:n?sum/n:NaN,wins:es.filter(e=>e.c.net_pct>0).length,win:n?100*es.filter(e=>e.c.net_pct>0).length/n:NaN,tp:es.filter(e=>e.c.outcome==='TP').length,sl:es.filter(e=>e.c.outcome==='SL').length,tm:es.filter(e=>e.c.outcome==='TIME').length};}
function eligible(e,k){if(k==='any')return e.htf?.day?.eligible===true||e.htf?.week?.eligible===true;return e.htf?.[k]?.eligible===true;}
const plans=[
  {cohort:'day',name:'일봉 · 기준 C',test:e=>true},
  {cohort:'day',name:'일봉 · 골든크로스 확정',test:e=>e.htf.day.cross_pass===true},
  {cohort:'day',name:'일봉 · 교차 직전',test:e=>e.htf.day.near_pass===true},
  {cohort:'day',name:'일봉 · 확정 또는 직전',test:e=>e.htf.day.either_pass===true},
  {cohort:'week',name:'주봉 · 기준 C',test:e=>true},
  {cohort:'week',name:'주봉 · 골든크로스 확정',test:e=>e.htf.week.cross_pass===true},
  {cohort:'week',name:'주봉 · 교차 직전',test:e=>e.htf.week.near_pass===true},
  {cohort:'week',name:'주봉 · 확정 또는 직전',test:e=>e.htf.week.either_pass===true},
  {cohort:'any',name:'일봉 또는 주봉 · 기준 C',test:e=>true},
  {cohort:'any',name:'일봉 또는 주봉 충족',test:e=>(e.htf.day.eligible&&e.htf.day.either_pass)||(e.htf.week.eligible&&e.htf.week.either_pass)}
];
function table(base){let html='<table><thead><tr><th>방식</th><th>공통 C</th><th>진입</th><th>TP/SL/시간</th><th>순승률</th><th>진입당 평균</th><th>보류=0% 신호당 평균</th><th>제외 거래</th><th>제외한 수익거래</th></tr></thead><tbody>';
 for(const p of plans){const es=base.filter(e=>eligible(e,p.cohort)),kept=es.filter(p.test),removed=es.filter(e=>!p.test(e)),s=stat(kept);html+=`<tr><td>${esc(p.name)}</td><td>${es.length}</td><td>${s.n}</td><td>${s.tp}/${s.sl}/${s.tm}</td><td>${fmt(s.win)}</td><td>${fmt(s.avg)}</td><td>${fmt(es.length?s.sum/es.length:NaN)}</td><td>${removed.length}</td><td>${removed.filter(e=>e.c.net_pct>0).length}</td></tr>`;}
 return html+'</tbody></table>';}
function renderResults(a){const events=a.flatMap(r=>(r.events||[]).map(e=>({...e,market:r.market}))),base=events.filter(decided),day=base.filter(e=>eligible(e,'day')),week=base.filter(e=>eligible(e,'week')),both=base.filter(e=>eligible(e,'any')),all=stat(base),first=base.filter(e=>e.is_first_signal);
 $('summary').innerHTML=card('원본 C 전체 신호',events.length+'건')+card('C 손익 판정',all.n+'건')+card('원본 C 평균',fmt(all.avg))+card('일봉 계산 가능',day.length+'건')+card('주봉 계산 가능',week.length+'건')+card('일/주 중 적어도 하나 가능',both.length+'건')+card('일봉 불가 · 판정',base.length-day.length+'건')+card('주봉 불가 · 판정',base.length-week.length+'건');
 $('results').innerHTML=table(base);$('first').innerHTML=table(first);
}
function paint(){const a=sampleResults(),n=a.length,total=state?.markets.length||0,complete=total>0&&n===total&&state?.failed===0,signals=a.reduce((s,r)=>s+r.signals,0);
 $('progress').max=total||1;$('progress').value=n;
 $('coverage').innerHTML=card('검사 진행',`${n}/${total}`)+card('C 신호',signals+'건')+card('유효 C 검색 윈도우',a.reduce((s,r)=>s+(r.valid_windows||0),0)+'개')+card('5분봉 240개 이상',a.filter(x=>x.observed_5m>=240).length+'코인')+card('과거 5분봉 상한 도달',a.filter(x=>x.truncated).length+'코인');
 if(complete){renderResults(a);$('csv').disabled=false;}else{$('summary').textContent='전체 KRW 검사 후 공개';$('results').textContent='검사 후 공개';$('first').textContent='검사 후 공개';$('csv').disabled=true;}
 $('status').textContent=state?(complete?'✅ 전체 검사 완료':'⏳ 진행 중 또는 중단')+` · ${n}/${total}종목 · C 신호 ${signals}건`+(state.failed?' · 실패 묶음 재검사 필요':''):'검사 전';}
async function req(url,opts){const ctl=new AbortController(),tid=setTimeout(()=>ctl.abort(),115000);try{const r=await fetch(url,{...opts,cache:'no-store',signal:ctl.signal}),v=await r.json();if(!r.ok||!v.ok)throw Error(v.error||'HTTP '+r.status);return v;}finally{clearTimeout(tid);}}
async function initSession(c){const v=await req('/api/winrate14htfmacd/universe');if(!Array.isArray(v.items)||!v.items.length)throw Error('KRW 목록이 없습니다');state={version:10,config:c,markets:v.items,results:{},failed:0};save();}
async function run(){if(running)return;let c;try{c=cfgOf()}catch(e){$('status').textContent='⚠️ '+e.message;return;}
 if(state&&signature(state.config)!==signature(c)){if(!confirm('날짜·설정이 변경됐어. 이 버전의 이전 검사 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);}
 running=true;stopRequested=false;hardFailure=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;
 try{if(!state)await initSession(c);state.failed=0;save();paint();const pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m)),batches=[];for(let i=0;i<pending.length;i+=2)batches.push(pending.slice(i,i+2));let pos=0,fail=0;
 async function worker(){while(!stopRequested&&!hardFailure){const batch=batches[pos++];if(!batch)break;let response=null;
 for(let attempt=0;attempt<3&&!stopRequested;attempt++){try{const v=await req('/api/winrate14htfmacd/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:batch})});
 if(v.errors||v.scanned!==batch.length||!Array.isArray(v.results)||v.results.length!==batch.length||v.results.some(r=>!batch.includes(r.market)||!Array.isArray(r.events)||r.events.length!==r.signals||r.events.some(e=>!e.c||!e.htf?.day||!e.htf?.week)))throw Error('불완전한 응답');
 response=v;break;}catch(e){if(attempt===2){fail++;$('status').textContent='⚠️ '+batch.join(', ')+' 실패 '+String(e.message||e);}else await new Promise(ok=>setTimeout(ok,1000*(attempt+1)));}}
 if(response){for(const x of response.results)state.results[x.market]=x;try{save()}catch(e){hardFailure=true;$('status').textContent='⚠️ 저장 공간 부족';break;}paint();}
 if(fail>=4){hardFailure=true;break;}}}
 await Promise.all([worker(),worker()]);state.failed=fail;try{save()}catch{}paint();if(stopRequested)$('status').textContent+='\n일시 중지. 다시 시작하면 이어서 검사합니다.';if(fail)$('status').textContent+=`\n누락 묶음 ${fail}개 · 다시 실행해 재검사하세요.`;
 }catch(e){$('status').textContent='⚠️ '+String(e.message||e)+' · 전체 검사 전 성과 미공개';}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}}
$('run').onclick=run;
$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;};
$('reset').onclick=()=>{if(running)return;if(!confirm('V11.14.10 진행 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(v){const s=String(v??'');return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!state||sampleResults().length!==state.markets.length||state.failed)return;
 const cols=['version','date','tp_pct','sl_pct','horizon_minutes','fee_pct_side','slip_pct_side','market','observed_5m','c_signals','original_signal_time_kst','c_entry_time_kst','is_first_signal','c_outcome','c_reason','c_entry','c_exit','c_minutes','c_net_pct','day_eligible','day_reason','day_latest_closed_kst','day_macd_pct','day_signal_pct','day_hist_pct','day_hist_prev_pct','day_hist_prev2_pct','day_cross_age','day_cross_pass','day_near_pass','day_either_pass','week_eligible','week_reason','week_latest_closed_kst','week_macd_pct','week_signal_pct','week_hist_pct','week_hist_prev_pct','week_hist_prev2_pct','week_cross_age','week_cross_pass','week_near_pass','week_either_pass','original_volume_ratio','original_body_pct','original_pullback_pct'];
 const rows=[cols.map(csvCell).join(',')],prefix={version:'V11.14.10',date:state.config.date,tp_pct:state.config.tp,sl_pct:state.config.sl,horizon_minutes:state.config.horizon,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip};
 function append(obj){rows.push(cols.map(k=>csvCell(obj[k])).join(','));}
 for(const r of sampleResults()){if(!r.signals){append({...prefix,market:r.market,observed_5m:r.observed_5m,c_signals:0});continue;}
 if(r.events.length!==r.signals){$('status').textContent='⚠️ CSV 이벤트 수 불일치';return;}
 for(const e of r.events){const c=e.c||{},d=e.htf?.day||{},w=e.htf?.week||{},f=e.features||{};
 const obj={...prefix,market:r.market,observed_5m:r.observed_5m,c_signals:r.signals,original_signal_time_kst:e.signal_time_kst,c_entry_time_kst:e.c_entry_time_kst,is_first_signal:e.is_first_signal?1:0,c_outcome:c.outcome,c_reason:c.reason,c_entry:c.entry,c_exit:c.exit,c_minutes:c.minutes,c_net_pct:c.net_pct,original_volume_ratio:f.volume_ratio,original_body_pct:f.body_pct,original_pullback_pct:f.pullback_depth_pct};
 for(const [pre,s] of [['day',d],['week',w]]){obj[pre+'_eligible']=s.eligible?1:0;obj[pre+'_reason']=s.reason;obj[pre+'_latest_closed_kst']=s.latest_closed_bar_end_kst;for(const k of ['macd_pct','signal_pct','hist_pct','hist_prev_pct','hist_prev2_pct'])obj[pre+'_'+k]=s[k];obj[pre+'_cross_age']=s.cross_age_bars;for(const k of ['cross_pass','near_pass','either_pass'])obj[pre+'_'+k]=s.eligible?(s[k]?1:0):'';}
 append(obj);}}
 const blob=new Blob(['\ufeff'+rows.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`bithumb_full_krw_C_HTF_MACD_${state.config.date}_v111410.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
state=load();if(state)setConfig(state.config);paint();
</script></body></html>"""


def register_htf_macd_v111410_lab(app,deps):
    api_get,get_markets,kst=deps['api_get'],deps['get_markets'],deps['KST']
    cache={'at':0,'names':None}
    mutex=Lock()

    def listed():
        with mutex:
            if cache['names'] is None or time.time()-cache['at']>600:
                markets=sorted({m for m in get_markets()
                                if isinstance(m,str) and MARKET_RE.fullmatch(m)})
                if not markets:raise ValueError('현재 빗썸 KRW 목록이 없습니다')
                cache.update(names=markets,at=time.time())
            return cache['names']

    @app.get('/backtest-winrate-htf-macd')
    def page_htf_macd_v111410():
        return render_template_string(HTF_V111410_PAGE)

    @app.get('/api/winrate14htfmacd/universe')
    def universe_htf_macd_v111410():
        try:
            names=listed()
            return jsonify({'ok':True,'items':names,'count':len(names),
                            'universe_type':'current_KRW_listings',
                            'rule_version':'V11.14.10'})
        except Exception as e:return jsonify({'ok':False,'error':str(e)[:160]}),400

    @app.post('/api/winrate14htfmacd/evaluate')
    def evaluate_htf_macd_v111410():
        try:
            payload=request.get_json(silent=True)
            cfg=lab_valid_request(payload,kst)
            markets=payload.get('markets') if isinstance(payload,dict) else None
            if (not isinstance(markets,list) or not 1<=len(markets)<=2
                    or len(set(markets))!=len(markets)
                    or any(not isinstance(m,str) or not MARKET_RE.fullmatch(m) for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 검사합니다')
            if any(m not in set(listed()) for m in markets):
                raise ValueError('현재 KRW 목록에 없는 종목입니다')
            results,errors=[],[]
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs={pool.submit(htf_v111410_evaluate_symbol,api_get,m,cfg,kst):m
                      for m in markets}
                for job in as_completed(jobs):
                    try:results.append(job.result())
                    except Exception as exc:errors.append({'market':jobs[job],
                                                         'error':str(exc)[:120]})
            return jsonify({'ok':not errors,'scanned':len(markets),
                            'results':results,'errors':len(errors),
                            'error_markets':errors,'rule_version':'V11.14.10'})
        except Exception as e:return jsonify({'ok':False,'error':str(e)[:160]}),400

_v11149_register_daytrade_review=register_daytrade_review

def register_daytrade_review(app,deps):
    _v11149_register_daytrade_review(app,deps)
    register_htf_macd_v111410_lab(app,deps)


# ---------------------------------------------------------------------------
# V11.14.11: Independent C + session VWAP and BTC context filter lab.
# All previous routes, C entry signals and historical exit rules remain intact.
# Only completed 5-minute candles up to original C signal close are used.
# The source of VWAP is Bithumb cumulative quote turnover per candle, not an
# approximate OHLC-derived price. No BUY signals or trading/orders are added.
# ---------------------------------------------------------------------------

VWAP_BTC_V111411_BTC_MIN_1H_PCT = -0.30  # prior 60m BTC return must not crash
VWAP_BTC_V111411_REL_MIN_1H_PP = 0.50   # coin beats BTC by at least 0.5%p
VWAP_BTC_V111411_SESSION_HOUR = 9       # KST 09:00, matches exchange daily bars


def vwb_v111411_fetch_5m(api_get,market,cfg,kst):
    """Same OHLCV and paging as original C history, plus optional quote turnover.

    Extends the history window to previous day's 09:00 for complete KST-session
    VWAP evaluation at the earliest signal. Missing quote turnover does not
    discard 5m price rows, so the exact C signal universe stays unchanged.
    """
    day_start=datetime.combine(cfg['day'],_time.min,tzinfo=kst)
    begin=day_start-timedelta(hours=15)
    end=day_start+timedelta(days=1,hours=2,minutes=5)
    cursor=end; observed={}; truncated=True
    for _ in range(LAB_MAX_PAGES):
        raw=api_get('/v1/candles/minutes/5',{'market':market,'count':200,
            'to':cursor.strftime('%Y-%m-%d %H:%M:%S')})
        if not isinstance(raw,list):raise ValueError('과거 5분봉 API 응답 오류')
        oldest=None
        for bar in raw:
            if not isinstance(bar,dict):continue
            try:
                ts=local_time(bar['candle_date_time_kst'],kst)
                op,hi,lo,cl=(float(bar[k]) for k in ('opening_price','high_price','low_price','trade_price'))
                vol=float(bar['candle_acc_trade_volume'])
                if not (all(isfinite(x) and x>0 for x in (op,hi,lo,cl))
                        and isfinite(vol) and vol>=0 and
                        lo<=min(op,cl) and hi>=max(op,cl) and lo<=hi):continue
                if oldest is None or ts<oldest:oldest=ts
                if begin<=ts<end:
                    quote=None
                    try:
                        v=float(bar['candle_acc_trade_price'])
                        if isfinite(v) and v>=0 and (vol==0 or v>0):quote=v
                    except (TypeError,ValueError,KeyError,OverflowError):pass
                    observed[ts]={'t':ts,'o':op,'h':hi,'l':lo,'c':cl,'v':vol,'q':quote}
            except (TypeError,ValueError,KeyError,OverflowError):continue
        if oldest is None:
            truncated=False;break
        if oldest<=begin or len(raw)<200:
            truncated=False;break
        if oldest>=cursor:raise ValueError('과거 봉 페이지가 전진하지 않습니다')
        cursor=oldest
    return [observed[t] for t in sorted(observed)],truncated


def vwb_v111411_vwap_snapshot(rows,i):
    """Anchored session VWAP at C signal close using observed actual trades.

    Sessions are KST 09:00 -> next day 08:55. We require at least 6 observed
    session bars and quote turnover for each included bar. Missing periods are
    not filled; coverage is exposed for audit. The last 15m slope only uses
    completed bar snapshots from this same session.
    """
    signal=rows[i]
    anchor=signal['t'].replace(hour=VWAP_BTC_V111411_SESSION_HOUR,minute=0,second=0,microsecond=0)
    if signal['t']<anchor:anchor-=timedelta(days=1)
    part=[r for r in rows[:i+1] if anchor<=r['t']<=signal['t']]
    expected=int((signal['t']-anchor)/timedelta(minutes=5))+1
    coverage=len(part)/expected if expected>0 else 0
    if len(part)<6:return {'eligible':False,'reason':'당일 관측 5분봉 6개 미만','bars':len(part),'coverage_pct':round(coverage*100,2)}
    if any(r['q'] is None for r in part):return {'eligible':False,'reason':'봉 거래대금 누락','bars':len(part),'coverage_pct':round(coverage*100,2)}
    volumes=[];prices=[];cum_v=cum_q=0.0
    for r in part:
        cum_v+=r['v'];cum_q+=r['q']
        volumes.append(cum_v);prices.append(cum_q/cum_v if cum_v>0 else None)
    if cum_v<=0 or prices[-1] is None:return {'eligible':False,'reason':'당일 거래량 없음','bars':len(part),'coverage_pct':round(coverage*100,2)}
    last=prices[-1]
    above=signal['c']>last
    # Compare against an anchored VWAP exactly 15 minutes earlier.
    recent=part[-4:]
    slope_eligible=(len(recent)==4 and all(recent[k]['t']-recent[k-1]['t']==timedelta(minutes=5) for k in range(1,4))
                    and prices[-4] is not None)
    # When 15-minute slope cannot be computed, the VWAP-A condition is still
    # available, while VWAP-B and A+B require the slope data.
    rising=(last>prices[-4]) if slope_eligible else None
    return {'eligible':True,'reason':None,'bars':len(part),'coverage_pct':round(coverage*100,2),
            'vwap':round(last,10),'distance_pct':round((signal['c']/last-1)*100,5),
            'above_pass':bool(above),'slope_eligible':bool(slope_eligible),
            'rising_pass':bool(rising) if slope_eligible else None,
            'both_pass':bool(above and rising) if slope_eligible else None}


def vwb_v111411_btc_snapshot(rows,i,btc_by_time):
    """Use 13 contiguous completed BTC 5m candles ending at signal bar time.

    BTC 1h and the coin's 1h return are both close-to-close on exactly the
    same time window. Missing BTC candles are UNKNOWN, never false filter hits.
    """
    stamp=rows[i]['t']
    times=[stamp-timedelta(minutes=5*k) for k in range(12,-1,-1)]
    btc=[btc_by_time.get(t) for t in times]
    if any(r is None for r in btc):return {'eligible':False,'reason':'BTC 최근 1시간 확정봉 공백'}
    if i<12 or rows[i-12]['t']!=times[0] or not lab_contiguous(rows,i-12,i):
        return {'eligible':False,'reason':'코인 최근 1시간 확정봉 공백'}
    old_b=btc[0]['c'];now_b=btc[-1]['c'];old_coin=rows[i-12]['c']
    if min(old_b,now_b,old_coin)<=0:return {'eligible':False,'reason':'수익률 계산 불가'}
    bret=(now_b/old_b-1)*100
    cret=(rows[i]['c']/old_coin-1)*100
    relative=cret-bret
    return {'eligible':True,'reason':None,'btc_1h_pct':round(bret,5),
            'coin_1h_pct':round(cret,5),'relative_1h_pp':round(relative,5),
            'stable_pass':bool(bret>=VWAP_BTC_V111411_BTC_MIN_1H_PCT),
            'relative_pass':bool(relative>=VWAP_BTC_V111411_REL_MIN_1H_PP)}


def vwb_v111411_evaluate_symbol(api_get,market,cfg,kst,btc_by_time):
    """Original C event and exit, with simultaneous VWAP and BTC snapshots."""
    rows,truncated=vwb_v111411_fetch_5m(api_get,market,cfg,kst)
    day_start=datetime.combine(cfg['day'],_time.min,tzinfo=kst)
    day_end=day_start+timedelta(days=1)
    day_rows=[r for r in rows if day_start<=r['t']<day_end]
    n,valid,signals,unknown,cooldown=cfg['bars'],0,0,0,-1
    events=[]
    if len(rows)>=LAB_WARMUP_BARS+2:
        for i in range(LAB_WARMUP_BARS,len(rows)-1):
            if not day_start<=rows[i]['t']<day_end:continue
            if not lab_contiguous(rows,i-24,i):continue
            valid+=1
            if i<cooldown or 'C' not in lab_signals(rows,i):continue
            cooldown=i+n
            signals+=1
            out=lab_outcome_v11145(rows,i,cfg)
            if out['outcome']=='UNKNOWN':unknown+=1
            e={'signal_time_kst':rows[i]['t'].strftime('%Y-%m-%d %H:%M'),
               'c_entry_time_kst':(rows[i]['t']+timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M'),
               'is_first_signal':signals==1,'c':out,
               'features':lab_c_signal_features(rows,i),
               'vwap':vwb_v111411_vwap_snapshot(rows,i),
               'btc':vwb_v111411_btc_snapshot(rows,i,btc_by_time)}
            events.append(e)
    assert len(events)==signals
    return {'market':market,'observed_5m':len(day_rows),
          'longest_day_run':lab_longest_consecutive_5m(day_rows),
          'valid_windows':valid,'signals':signals,'unknown':unknown,
          'truncated':truncated,'events':events}

VWAP_BTC_V111411_PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.11 · C + VWAP · BTC 영향 비교 검증실</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#1a2038;--card:#121a2e;--border:#35425f;--fg:#f2f5ff;--muted:#afc0db;--blue:#3867ee;--green:#4ade80;--warn:#ffc46c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1050px;margin:auto;padding:18px 14px 70px}h1{font-size:23px;margin:8px 0}h2{font-size:18px;margin:0 0 8px}
.panel{border:1px solid var(--border);background:var(--panel);border-radius:16px;padding:16px;margin-top:15px}
.muted,.hint{font-size:13px;color:var(--muted)}.note{padding:12px;background:#101a30;border-radius:10px;font-size:13px;line-height:1.75}
.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.field{display:flex;flex-direction:column;font-size:13px;color:var(--muted);gap:5px}
input,button,select{font:inherit}input,select{padding:10px;background:#10182d;border:1px solid var(--border);color:var(--fg);border-radius:8px;width:100%;min-width:0}
button{background:var(--blue);border:0;border-radius:9px;color:#fff;padding:12px 14px;font-weight:800;cursor:pointer}button:disabled{opacity:.48}.secondary{background:#364660}
.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px;align-items:center}a{color:#bcd0ff}progress{width:100%;height:15px;margin-top:14px}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.stat{border:1px solid var(--border);background:var(--card);border-radius:10px;padding:11px}
.stat small{font-size:12px;color:var(--muted);display:block}.stat strong{font-size:22px;display:block}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--border);padding:9px 7px;text-align:right;white-space:nowrap}td:first-child,th:first-child{text-align:left}
.status{font-size:13px;white-space:pre-wrap;color:var(--muted)}
@media(max-width:650px){.controls{grid-template-columns:repeat(2,minmax(0,1fr))}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><main>
<h1>🧪 V11.14.11 · C + VWAP · BTC 영향 검증실</h1>
<p class="muted">별도 과거 모의검증 · V2 실전 BUY, C/C2, 볼린저, 일목, MACD 검증실은 변경하지 않습니다.</p>
<section class="panel"><h2>사전에 정한 비교 조건</h2>
<div class="note">
<b>원본 C:</b> 신호가 확정된 다음 5분봉 시가 진입·동일 익절/손절/비용. 필터를 통과해도 진입 시점을 늦추지 않습니다.<br>
<b>VWAP A:</b> 신호봉 종가가 <b>KST 09:00 시작 세션 VWAP 위</b>. 빗썸 5분봉 실제 거래대금 ÷ 거래량을 사용합니다.<br>
<b>VWAP B:</b> 세션 VWAP가 <b>15분 전보다 상승</b>. A+B는 두 조건 모두 충족합니다.<br>
<b>BTC 안정:</b> 동일한 60분 구간의 BTC 종가 수익률이 <b>-0.30% 이상</b>.<br>
<b>BTC 상대강도:</b> 같은 60분간 코인의 상승률이 BTC 상승률보다 <b>0.50%p 이상 높음</b>.<br>
<b>조합:</b> VWAP A+B와 BTC 안정 조건 동시 충족.<br>
<i>BTC 데이터·VWAP 거래대금·VWAP 기울기 계산이 불가한 신호는 해당 비교에서 별도로 제외합니다. 계산 불가 신호를 성공적인 손실 회피로 취급하지 않습니다. 위 기준은 검증 전 설정한 연구용 가설로, 수익성은 아직 입증되지 않았습니다.</i>
</div></section>
<section class="panel"><h2>검증 조건</h2><div class="controls">
<label class="field">과거 날짜 (KST)<input id="date" type="date" value="2026-11-01"></label>
<label class="field">익절 (%)<input id="tp" type="number" min="0.3" max="10" step="0.1" value="1.5"></label>
<label class="field">손절 (%)<input id="sl" type="number" min="0.2" max="8" step="0.1" value="1.0"></label>
<label class="field">최대 보유<select id="horizon"><option value="30">30분</option><option value="60" selected>60분</option><option value="120">120분</option></select></label>
<label class="field">편도 수수료 (%)<input id="fee" type="number" min="0" max="0.5" step="0.01" value="0.04"></label>
<label class="field">편도 슬리피지 (%)<input id="slip" type="number" min="0" max="0.5" step="0.01" value="0.05"></label>
</div><div class="actions"><button id="run">전체 KRW 검사 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">V11.14.11 기록 초기화</button><button id="csv" class="secondary" disabled>전체 VWAP/BTC 비교 CSV 저장</button></div>
<progress id="progress" value="0" max="1"></progress><p id="status" class="status">검사 전</p>
<p class="hint">전체 검사가 끝나기 전에는 최종 성과를 공개하지 않습니다. 브라우저 저장 후 이어하기 가능. 기록은 이 버전만 별도로 저장됩니다.</p></section>
<section class="panel"><h2>전 종목 검사 상태</h2><div id="coverage" class="stats">검사 전</div></section>
<section class="panel"><h2>동일 신호 기준 공정 비교</h2><div id="summary" class="stats">검사 완료 후 공개</div>
<p class="hint">공통 계산 가능 신호의 기존 C 성적과 필터 통과 신호를 비교합니다. 보류를 0%로 처리한 신호당 평균은 실제 계좌 수익률이 아닙니다. 반복 신호는 독립 표본이 아닙니다.</p><div id="results" class="scroll">검사 완료 후 공개</div></section>
<section class="panel"><h2>종목별 첫 C 신호만 별도 비교</h2><div id="first" class="scroll">검사 완료 후 공개</div></section>
<section class="panel"><h2>검증의 한계</h2><p class="hint">현재 상장 중인 빗썸 KRW 종목을 과거 날짜로 검사하므로 생존자 편향이 있습니다. 과거 거래대금 및 BTC 봉 공백은 계산 불가로 표시합니다. 11/1~3은 규칙 탐색 자료이므로 이후 새로운 날짜에서 독립 검증이 필요합니다. 실제 체결·수익을 보장하지 않으며 주문 기능은 없습니다.</p>
<a href="/backtest-winrate-htf-macd">V11.14.10 MACD</a> · <a href="/backtest-winrate-ich">V11.14.9 일목</a> · <a href="/backtest-winrate-bb">V11.14.8 볼린저</a> · <a href="/backtest-winrate-c2">V11.14.7 C2</a> · <a href="/">V2 메인</a></section>
</main><script>
'use strict';
const $=id=>document.getElementById(id),KEY='bithumb_v111411_vwapbtc_checkpoint_1';
let state=null,running=false,stopRequested=false,hardFailure=false;
const fields=['date','tp','sl','horizon','fee','slip'];
function cfgOf(){const c={};for(const k of fields)c[k]=$(k).value;if(!/^20\d{2}-\d{2}-\d{2}$/.test(c.date))throw Error('날짜를 선택해');for(const k of fields.slice(1))if(!Number.isFinite(Number(c[k])))throw Error('입력 확인: '+k);return c;}
function signature(c){return fields.map(k=>String(c[k])).join('|');}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function load(){try{const x=JSON.parse(localStorage.getItem(KEY)||'null');return x?.version===11&&Array.isArray(x.markets)&&x.config&&x.results?x:null;}catch{return null;}}
function setConfig(c){for(const k of fields)$(k).value=c[k];}
function sampleResults(){return state?state.markets.filter(x=>Object.prototype.hasOwnProperty.call(state.results,x)).map(x=>state.results[x]):[];}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function card(k,v){return `<div class="stat"><small>${esc(k)}</small><strong>${esc(v)}</strong></div>`;}
function fmt(x){return Number.isFinite(x)?x.toFixed(3)+'%':'—';}
function decided(e){return e?.c&&['TP','SL','TIME'].includes(e.c.outcome)&&Number.isFinite(e.c.net_pct);}
function stat(es){const n=es.length,sum=es.reduce((s,e)=>s+e.c.net_pct,0),wins=es.filter(e=>e.c.net_pct>0).length;return {n,sum,avg:n?sum/n:NaN,wins,win:n?100*wins/n:NaN,tp:es.filter(e=>e.c.outcome==='TP').length,sl:es.filter(e=>e.c.outcome==='SL').length,tm:es.filter(e=>e.c.outcome==='TIME').length};}
function cohort(e,k){const v=e.vwap||{},b=e.btc||{};if(k==='all')return true;if(k==='va')return v.eligible===true;if(k==='vb')return v.eligible===true&&v.slope_eligible===true;if(k==='btc')return b.eligible===true;if(k==='both')return v.eligible===true&&v.slope_eligible===true&&b.eligible===true;return false;}
const plans=[
  {co:'all',name:'기존 C 전체 (기준)',pass:e=>true},
  {co:'va',name:'VWAP A 비교 · 기준 C',pass:e=>true},
  {co:'va',name:'VWAP A · 종가 위',pass:e=>e.vwap.above_pass===true},
  {co:'vb',name:'VWAP B 비교 · 기준 C',pass:e=>true},
  {co:'vb',name:'VWAP B · 15분 상승',pass:e=>e.vwap.rising_pass===true},
  {co:'vb',name:'VWAP A+B',pass:e=>e.vwap.both_pass===true},
  {co:'btc',name:'BTC 비교 · 기준 C',pass:e=>true},
  {co:'btc',name:'BTC 1시간 급락 제외',pass:e=>e.btc.stable_pass===true},
  {co:'btc',name:'BTC 대비 상대강도',pass:e=>e.btc.relative_pass===true},
  {co:'both',name:'조합 비교 · 기준 C',pass:e=>true},
  {co:'both',name:'VWAP A+B + BTC 안정',pass:e=>e.vwap.both_pass===true&&e.btc.stable_pass===true}
];
function table(base){let html='<table><thead><tr><th>비교 방식</th><th>공통 계산 가능 C</th><th>진입</th><th>TP/SL/시간</th><th>승률</th><th>진입당 순평균</th><th>미진입=0% 신호당 순평균</th><th>제외</th><th>제외한 수익거래</th></tr></thead><tbody>';
 for(const p of plans){const es=base.filter(e=>cohort(e,p.co)),kept=es.filter(p.pass),removed=es.filter(e=>!p.pass(e)),s=stat(kept);html+=`<tr><td>${esc(p.name)}</td><td>${es.length}</td><td>${s.n}</td><td>${s.tp}/${s.sl}/${s.tm}</td><td>${fmt(s.win)}</td><td>${fmt(s.avg)}</td><td>${fmt(es.length?s.sum/es.length:NaN)}</td><td>${removed.length}</td><td>${removed.filter(e=>e.c.net_pct>0).length}</td></tr>`;}
 return html+'</tbody></table>';}
function renderResults(arr){const events=arr.flatMap(r=>(r.events||[]).map(e=>({...e,market:r.market}))),base=events.filter(decided),all=stat(base),a=base.filter(e=>cohort(e,'va')),b=base.filter(e=>cohort(e,'vb')),btc=base.filter(e=>cohort(e,'btc')),both=base.filter(e=>cohort(e,'both'));
 $('summary').innerHTML=card('원본 C 신호',events.length+'건')+card('원본 C 손익 판정',base.length+'건')+card('원본 C 평균',fmt(all.avg))+card('VWAP 값 계산 가능',a.length+'건')+card('VWAP 15분 기울기 가능',b.length+'건')+card('BTC 최근 1시간 가능',btc.length+'건')+card('VWAP+BTC 모두 가능',both.length+'건')+card('VWAP 계산 불가 · 판정',base.length-a.length+'건')+card('BTC 계산 불가 · 판정',base.length-btc.length+'건');
 $('results').innerHTML=table(base);$('first').innerHTML=table(base.filter(e=>e.is_first_signal));}
function paint(){const a=sampleResults(),n=a.length,total=state?.markets.length||0,complete=total>0&&n===total&&state?.failed===0,signals=a.reduce((s,r)=>s+r.signals,0);
 $('progress').max=total||1;$('progress').value=n;
 $('coverage').innerHTML=card('검사 진행',`${n}/${total}`)+card('C 신호',signals+'건')+card('유효 C 검색 구간',a.reduce((s,r)=>s+(r.valid_windows||0),0)+'개')+card('5분봉 240개 이상',a.filter(x=>x.observed_5m>=240).length+'코인')+card('5분봉 조회 상한 도달',a.filter(x=>x.truncated).length+'코인');
 if(complete){renderResults(a);$('csv').disabled=false;}else{$('summary').textContent='전체 KRW 검사 완료 후 공개';$('results').textContent='전체 검사 후 공개';$('first').textContent='전체 검사 후 공개';$('csv').disabled=true;}
 $('status').textContent=state?(complete?'✅ 전체 검사 완료':'⏳ 검사 중 또는 중단')+` · ${n}/${total}종목 · C 신호 ${signals}건`+(state.failed?' · 실패 묶음 재검사 필요':''):'검사 전';}
async function req(url,opts){const ctl=new AbortController(),tid=setTimeout(()=>ctl.abort(),115000);try{const r=await fetch(url,{...opts,cache:'no-store',signal:ctl.signal}),v=await r.json();if(!r.ok||!v.ok)throw Error(v.error||'HTTP '+r.status);return v;}finally{clearTimeout(tid);}}
async function initSession(c){const v=await req('/api/winrate14vwapbtc/universe');if(!Array.isArray(v.items)||!v.items.length)throw Error('KRW 목록이 없습니다');state={version:11,config:c,markets:v.items,results:{},failed:0};save();}
async function run(){if(running)return;let c;try{c=cfgOf();}catch(e){$('status').textContent='⚠️ '+e.message;return;}
 if(state&&signature(state.config)!==signature(c)){if(!confirm('날짜·설정이 변경됐어. 이 버전의 이전 검사 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);}
 running=true;stopRequested=false;hardFailure=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;
 try{if(!state)await initSession(c);state.failed=0;save();paint();const pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m)),batches=[];for(let i=0;i<pending.length;i+=2)batches.push(pending.slice(i,i+2));let pos=0,fail=0;
 async function worker(){while(!stopRequested&&!hardFailure){const batch=batches[pos++];if(!batch)break;let response=null;
 for(let attempt=0;attempt<3&&!stopRequested;attempt++){try{const v=await req('/api/winrate14vwapbtc/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:batch})});
 if(v.errors||v.scanned!==batch.length||!Array.isArray(v.results)||v.results.length!==batch.length||v.results.some(r=>!batch.includes(r.market)||!Array.isArray(r.events)||r.events.length!==r.signals||r.events.some(e=>!e.c||!e.vwap||!e.btc)))throw Error('불완전한 응답');
 response=v;break;}catch(e){if(attempt===2){fail++;$('status').textContent='⚠️ '+batch.join(', ')+' 실패 '+String(e.message||e);}else await new Promise(ok=>setTimeout(ok,1000*(attempt+1)));}}
 if(response){for(const x of response.results)state.results[x.market]=x;try{save();}catch(e){hardFailure=true;$('status').textContent='⚠️ 저장 공간 부족';break;}paint();}
 if(fail>=4){hardFailure=true;break;}}}
 await Promise.all([worker(),worker()]);state.failed=fail;try{save();}catch{}paint();if(stopRequested)$('status').textContent+='\n일시 중지. 다시 시작하면 이어서 검사합니다.';if(fail)$('status').textContent+=`\n누락 묶음 ${fail}개 · 다시 실행해서 재검사하세요.`;
 }catch(e){$('status').textContent='⚠️ '+String(e.message||e)+' · 전체 검사 전 성과 미공개';}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}}
$('run').onclick=run;
$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;};
$('reset').onclick=()=>{if(running)return;if(!confirm('V11.14.11 진행 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(v){const s=String(v??'');return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!state||sampleResults().length!==state.markets.length||state.failed)return;
 const cols=['version','date','tp_pct','sl_pct','horizon_minutes','fee_pct_side','slip_pct_side','market','observed_5m','c_signals','original_signal_time_kst','c_entry_time_kst','is_first_signal','c_outcome','c_reason','c_entry','c_exit','c_minutes','c_net_pct','vwap_eligible','vwap_reason','vwap_bars','vwap_coverage_pct','vwap_value','vwap_distance_pct','vwap_above_pass','vwap_slope_eligible','vwap_rising_pass','vwap_both_pass','btc_eligible','btc_reason','btc_1h_pct','coin_1h_pct','relative_1h_pp','btc_stable_pass','btc_relative_pass','combo_pass','original_volume_ratio','original_body_pct','original_pullback_pct'];
 const rows=[cols.map(csvCell).join(',')],prefix={version:'V11.14.11',date:state.config.date,tp_pct:state.config.tp,sl_pct:state.config.sl,horizon_minutes:state.config.horizon,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip};
 function append(obj){rows.push(cols.map(k=>csvCell(obj[k])).join(','));}
 for(const r of sampleResults()){if(!r.signals){append({...prefix,market:r.market,observed_5m:r.observed_5m,c_signals:0});continue;}
 if(r.events.length!==r.signals){$('status').textContent='⚠️ CSV 이벤트 수 불일치';return;}
 for(const e of r.events){const c=e.c||{},v=e.vwap||{},b=e.btc||{},f=e.features||{};
 append({...prefix,market:r.market,observed_5m:r.observed_5m,c_signals:r.signals,original_signal_time_kst:e.signal_time_kst,c_entry_time_kst:e.c_entry_time_kst,is_first_signal:e.is_first_signal?1:0,c_outcome:c.outcome,c_reason:c.reason,c_entry:c.entry,c_exit:c.exit,c_minutes:c.minutes,c_net_pct:c.net_pct,
 vwap_eligible:v.eligible?1:0,vwap_reason:v.reason,vwap_bars:v.bars,vwap_coverage_pct:v.coverage_pct,vwap_value:v.vwap,vwap_distance_pct:v.distance_pct,vwap_above_pass:v.eligible?(v.above_pass?1:0):'',vwap_slope_eligible:v.eligible?(v.slope_eligible?1:0):'',vwap_rising_pass:v.slope_eligible?(v.rising_pass?1:0):'',vwap_both_pass:v.slope_eligible?(v.both_pass?1:0):'',
 btc_eligible:b.eligible?1:0,btc_reason:b.reason,btc_1h_pct:b.btc_1h_pct,coin_1h_pct:b.coin_1h_pct,relative_1h_pp:b.relative_1h_pp,btc_stable_pass:b.eligible?(b.stable_pass?1:0):'',btc_relative_pass:b.eligible?(b.relative_pass?1:0):'',combo_pass:v.slope_eligible&&b.eligible?(v.both_pass&&b.stable_pass?1:0):'',
 original_volume_ratio:f.volume_ratio,original_body_pct:f.body_pct,original_pullback_pct:f.pullback_depth_pct});}}
 const blob=new Blob(['\ufeff'+rows.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`bithumb_full_krw_C_VWAP_BTC_${state.config.date}_v111411.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
state=load();if(state)setConfig(state.config);paint();

</script></body></html>"""


def register_vwap_btc_v111411_lab(app,deps):
    api_get,get_markets,kst=deps['api_get'],deps['get_markets'],deps['KST']
    cache={'names':None,'names_at':0,'btc':{},'btc_at':{}}
    mutex=Lock()

    def listed():
        with mutex:
            if cache['names'] is None or time.time()-cache['names_at']>600:
                names=sorted({m for m in get_markets() if isinstance(m,str) and MARKET_RE.fullmatch(m)})
                if not names:raise ValueError('현재 빗썸 KRW 목록이 없습니다')
                cache['names']=names;cache['names_at']=time.time()
            return cache['names']

    def btc_for_day(cfg):
        key=cfg['day'].isoformat()
        with mutex:
            if key not in cache['btc'] or time.time()-cache['btc_at'].get(key,0)>1800:
                # A single shared historical BTC download per date per worker,
                # rather than one BTC API request for EVERY altcoin.
                bars,_=lab_fetch_5m(api_get,'KRW-BTC',cfg,kst)
                if not bars:raise ValueError('BTC 5분봉 데이터가 비어 있습니다')
                cache['btc']={key:{r['t']:r for r in bars}}
                cache['btc_at']={key:time.time()}
            return cache['btc'][key]

    @app.get('/backtest-winrate-vwap-btc')
    def page_vwap_btc_v111411():
        return render_template_string(VWAP_BTC_V111411_PAGE)

    @app.get('/api/winrate14vwapbtc/universe')
    def universe_vwap_btc_v111411():
        try:
            names=listed()
            return jsonify({'ok':True,'items':names,'count':len(names),
                            'universe_type':'current_KRW_listings',
                            'rule_version':'V11.14.11'})
        except Exception as e:return jsonify({'ok':False,'error':str(e)[:160]}),400

    @app.post('/api/winrate14vwapbtc/evaluate')
    def evaluate_vwap_btc_v111411():
        try:
            payload=request.get_json(silent=True)
            cfg=lab_valid_request(payload,kst)
            markets=payload.get('markets') if isinstance(payload,dict) else None
            if (not isinstance(markets,list) or not 1<=len(markets)<=2
                    or len(set(markets))!=len(markets)
                    or any(not isinstance(m,str) or not MARKET_RE.fullmatch(m) for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 검사합니다')
            known=set(listed())
            if any(m not in known for m in markets):
                raise ValueError('현재 KRW 목록에 없는 종목입니다')
            btc_by_time=btc_for_day(cfg)
            results,errors=[],[]
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs={pool.submit(vwb_v111411_evaluate_symbol,api_get,m,cfg,kst,btc_by_time):m for m in markets}
                for job in as_completed(jobs):
                    try:results.append(job.result())
                    except Exception as exc:errors.append({'market':jobs[job],'error':str(exc)[:120]})
            return jsonify({'ok':not errors,'scanned':len(markets),
                            'results':results,'errors':len(errors),
                            'error_markets':errors,'rule_version':'V11.14.11'})
        except Exception as e:return jsonify({'ok':False,'error':str(e)[:160]}),400

_v111410_register_daytrade_review=register_daytrade_review

def register_daytrade_review(app,deps):
    _v111410_register_daytrade_review(app,deps)
    register_vwap_btc_v111411_lab(app,deps)


# ---------------------------------------------------------------------------
# V11.14.12: Separate historical C + objective Fibonacci/BTC-relative lab.
# All older endpoints and C entries/outcomes are kept unchanged.
# Pivots, pullback and BTC returns use only completed bars at C signal close.
# No order placement, BUY signal modification, or live scanner changes.
# ---------------------------------------------------------------------------
FIB_V111412_LOOKBACK = 48              # Prior 4h, completed 5m bars only
FIB_V111412_MAX_PEAK_AGE = 24          # Most recent confirmed high <=2h old
FIB_V111412_MIN_IMPULSE_PCT = 1.5      # Rise from pre-peak low to peak
FIB_V111412_RELATIVE_MIN_PP = 0.50     # 1h coin - BTC in percentage points


def fib_v111412_snapshot(rows,i):
    """Deterministic prior-low -> prior-high Fibonacci retracement study.

    Select highest HIGH among 48 immediately preceding, contiguous closed 5M
    bars; use lowest LOW *before* that high. Require at least two bars both
    between low and high and after high for a usable impulse/pullback. The C
    signal itself is completed and can test whether its low retraced to a
    Fibonacci area. Neither entry/exit prices nor any future candle are used.

    This is a rolling-window proxy for an impulse, NOT an Elliott wave parser.
    """
    if i < FIB_V111412_LOOKBACK:
        return {'eligible':False,'reason':'직전 4시간 5분봉 부족'}
    start=i-FIB_V111412_LOOKBACK
    if not lab_contiguous(rows,start,i):
        return {'eligible':False,'reason':'최근 4시간 5분봉 공백'}
    peak_idx=max(range(start,i),key=lambda k:(rows[k]['h'],k))
    age=i-peak_idx
    if age<3 or age>FIB_V111412_MAX_PEAK_AGE:
        return {'eligible':True,'swing_valid':False,'narrow_pass':False,'broad_pass':False,'reason':'최근 확정된 상승 고점 부재'}
    if peak_idx-start<3:
        return {'eligible':True,'swing_valid':False,'narrow_pass':False,'broad_pass':False,'reason':'상승 고점 이전 저점 구간 부족'}
    low_idx=min(range(start,peak_idx),key=lambda k:(rows[k]['l'],k))
    if peak_idx-low_idx<2:
        return {'eligible':True,'swing_valid':False,'narrow_pass':False,'broad_pass':False,'reason':'상승 저점·고점 간격 부족'}
    low=rows[low_idx]['l'];high=rows[peak_idx]['h']
    if low<=0 or high<=low:
        return {'eligible':True,'swing_valid':False,'narrow_pass':False,'broad_pass':False,'reason':'상승 파동 계산 불가'}
    impulse=(high/low-1)*100
    if impulse<FIB_V111412_MIN_IMPULSE_PCT:
        return {'eligible':True,'swing_valid':False,'narrow_pass':False,'broad_pass':False,'reason':'최근 상승 폭 1.5% 미만','impulse_pct':round(impulse,4)}
    span=high-low
    current=rows[i]
    depth=(high-current['l'])/span
    # Support levels for an upward impulse: High - depth * (High-Low).
    support_382=high-0.382*span
    support_500=high-0.500*span
    support_618=high-0.618*span
    support_786=high-0.786*span
    narrow=0.382<=depth<=0.618 and current['c']>=support_618
    broad=0.236<=depth<=0.786 and current['c']>=support_786
    return {'eligible':True,'swing_valid':True,'reason':None,'impulse_low':round(low,10),
            'impulse_high':round(high,10),
            'low_time_kst':rows[low_idx]['t'].strftime('%Y-%m-%d %H:%M'),
            'high_time_kst':rows[peak_idx]['t'].strftime('%Y-%m-%d %H:%M'),
            'peak_age_bars':age,'impulse_pct':round(impulse,5),
            'signal_low':round(current['l'],10),'signal_close':round(current['c'],10),
            'retracement_pct':round(depth*100,5),
            'level_382':round(support_382,10),'level_500':round(support_500,10),
            'level_618':round(support_618,10),'level_786':round(support_786,10),
            'narrow_pass':bool(narrow),'broad_pass':bool(broad)}


def fib_v111412_evaluate_symbol(api_get,market,cfg,kst,btc_by_time):
    """Unchanged baseline C search/exit, adding independent at-signal filters."""
    rows,truncated=vwb_v111411_fetch_5m(api_get,market,cfg,kst)
    day_start=datetime.combine(cfg['day'],_time.min,tzinfo=kst)
    day_end=day_start+timedelta(days=1)
    day_rows=[r for r in rows if day_start<=r['t']<day_end]
    n,valid,signals,unknown,cooldown=cfg['bars'],0,0,0,-1
    events=[]
    if len(rows)>=LAB_WARMUP_BARS+2:
        for i in range(LAB_WARMUP_BARS,len(rows)-1):
            if not day_start<=rows[i]['t']<day_end:continue
            if not lab_contiguous(rows,i-24,i):continue
            valid+=1
            if i<cooldown or 'C' not in lab_signals(rows,i):continue
            cooldown=i+n
            signals+=1
            out=lab_outcome_v11145(rows,i,cfg)
            if out['outcome']=='UNKNOWN':unknown+=1
            e={'signal_time_kst':rows[i]['t'].strftime('%Y-%m-%d %H:%M'),
               'c_entry_time_kst':(rows[i]['t']+timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M'),
               'is_first_signal':signals==1,'c':out,
               'features':lab_c_signal_features(rows,i),
               'fib':fib_v111412_snapshot(rows,i),
               'btc':vwb_v111411_btc_snapshot(rows,i,btc_by_time)}
            events.append(e)
    assert len(events)==signals
    return {'market':market,'observed_5m':len(day_rows),
          'longest_day_run':lab_longest_consecutive_5m(day_rows),
          'valid_windows':valid,'signals':signals,'unknown':unknown,
          'truncated':truncated,'events':events}

FIB_BTC_V111412_PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.12 · C + 피보나치 · BTC 상대강도 검증실</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#1a2038;--card:#121a2e;--border:#35425f;--fg:#f2f5ff;--muted:#afc0db;--blue:#3867ee;--green:#4ade80;--warn:#ffc46c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1050px;margin:auto;padding:18px 14px 70px}h1{font-size:23px;margin:8px 0}h2{font-size:18px;margin:0 0 8px}
.panel{border:1px solid var(--border);background:var(--panel);border-radius:16px;padding:16px;margin-top:15px}
.muted,.hint{font-size:13px;color:var(--muted)}.note{padding:12px;background:#101a30;border-radius:10px;font-size:13px;line-height:1.75}
.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.field{display:flex;flex-direction:column;font-size:13px;color:var(--muted);gap:5px}
input,button,select{font:inherit}input,select{padding:10px;background:#10182d;border:1px solid var(--border);color:var(--fg);border-radius:8px;width:100%;min-width:0}
button{background:var(--blue);border:0;border-radius:9px;color:#fff;padding:12px 14px;font-weight:800;cursor:pointer}button:disabled{opacity:.48}.secondary{background:#364660}
.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px;align-items:center}a{color:#bcd0ff}progress{width:100%;height:15px;margin-top:14px}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.stat{border:1px solid var(--border);background:var(--card);border-radius:10px;padding:11px}
.stat small{font-size:12px;color:var(--muted);display:block}.stat strong{font-size:22px;display:block}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--border);padding:9px 7px;text-align:right;white-space:nowrap}td:first-child,th:first-child{text-align:left}
.status{font-size:13px;white-space:pre-wrap;color:var(--muted)}
@media(max-width:650px){.controls{grid-template-columns:repeat(2,minmax(0,1fr))}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><main>
<h1>🧪 V11.14.12 · C + 피보나치 · BTC 상대강도</h1>
<p class="muted">별도 과거 모의검증 · 기존 V2 실전 BUY, C/C2, 볼린저, 일목, MACD, VWAP/BTC 검증실은 변경하지 않습니다.</p>
<section class="panel"><h2>이번 실험 규칙 (결과 확인 전 고정)</h2><div class="note">
<b>기존 C:</b> 원래 신호봉 확정 → 다음 5분봉 시가 진입. 익절·손절·보유·비용은 동일하며 추가 확인봉을 기다리지 않습니다.<br>
<b>피보나치 고점·저점:</b> C 신호 직전 <b>연속 48개 확정 5분봉(4시간)</b>에서 가장 높은 고점과 그 이전 최저 저점을 선택합니다. 고점은 신호보다 3~24봉 전에 발생했고 저점은 고점보다 최소 2봉 앞서 있어야 합니다. 상승 폭이 <b>1.5% 이상</b>인 경우만 피보나치 계산 가능으로 분류합니다.<br>
<b>피보나치 A:</b> C 신호봉 저가가 상승 파동의 <b>38.2~61.8% 되돌림</b> 구간이며 신호봉 종가는 61.8% 지지 가격 위에 마감.<br>
<b>피보나치 B:</b> 신호봉 저가가 <b>23.6~78.6% 되돌림</b> 구간이며 종가는 78.6% 지지 가격 위에 마감 (넓은 탐색 조건).<br>
<b>BTC 상대강도:</b> 동일한 최근 60분 확정봉에서 <b>코인 등락률 - BTC 등락률 ≥ +0.50%p</b>.<br>
<b>조합:</b> 각각 피보나치 A 또는 B + BTC 상대강도 동시 충족.<br>
<i>4시간 봉이 부족해서 계산할 수 없는 신호는 통과 실패로 세지 않고 별도 집계합니다. 상승 파동 기준 미충족은 계산 가능한 신호 중 필터 탈락으로 집계합니다. 과거 봉의 가장 높은/낮은 가격을 이용한 규칙적 근사이며 진짜 추세 전환이나 지지선을 보증하지 않습니다. 수익성이 아직 검증된 조건이 아닙니다.</i>
</div></section>
<section class="panel"><h2>검증 설정</h2><div class="controls">
<label class="field">과거 날짜 (KST)<input id="date" type="date" value="2026-11-01"></label>
<label class="field">익절 (%)<input id="tp" type="number" min="0.3" max="10" step="0.1" value="1.5"></label>
<label class="field">손절 (%)<input id="sl" type="number" min="0.2" max="8" step="0.1" value="1.0"></label>
<label class="field">최대 보유<select id="horizon"><option value="30">30분</option><option value="60" selected>60분</option><option value="120">120분</option></select></label>
<label class="field">편도 수수료 (%)<input id="fee" type="number" min="0" max="0.5" step="0.01" value="0.04"></label>
<label class="field">편도 슬리피지 (%)<input id="slip" type="number" min="0" max="0.5" step="0.01" value="0.05"></label>
</div><div class="actions"><button id="run">전체 KRW 검사 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">V11.14.12 기록 초기화</button><button id="csv" class="secondary" disabled>전체 FIB/BTC 비교 CSV 저장</button></div>
<progress id="progress" value="0" max="1"></progress><p id="status" class="status">검사 전</p>
<p class="hint">전체 검사가 완료되기 전에는 최종 성과를 공개하지 않습니다. 이 버전의 기록만 브라우저에 저장되어 이어서 실행할 수 있습니다.</p></section>
<section class="panel"><h2>전 종목 검사 현황</h2><div id="coverage" class="stats">검사 전</div></section>
<section class="panel"><h2>공정 비교 · 같은 C 신호 기준</h2><div id="summary" class="stats">전체 검사 후 공개</div>
<p class="hint">각 필터별 동일한 계산 가능 집단에서 기존 C와 필터 통과 종목을 함께 표시합니다. ‘미진입 0%’ 평균은 실제 계좌 수익률이 아닙니다. 동일 종목 반복 C 신호는 독립적인 표본이 아닙니다.</p><div id="results" class="scroll">전체 검사 후 공개</div></section>
<section class="panel"><h2>종목별 첫 C 신호만</h2><div id="first" class="scroll">전체 검사 후 공개</div></section>
<section class="panel"><h2>검증 한계 및 기존 검증실</h2><p class="hint">현재 상장 종목을 과거 날짜로 검사하므로 생존자 편향이 있습니다. 11월 1~3일은 기존 전략 설계에 참고한 날짜라 독립 검증으로 사용할 수 없습니다. 과거 BTC 봉 누락이나 코인 5분봉 공백은 별도 계산 불가로 처리합니다. 수익을 보장하지 않으며 주문 기능은 없습니다.</p>
<a href="/backtest-winrate-vwap-btc">V11.14.11 VWAP/BTC</a> · <a href="/backtest-winrate-htf-macd">V11.14.10 MACD</a> · <a href="/backtest-winrate-ich">V11.14.9 일목</a> · <a href="/backtest-winrate-bb">V11.14.8 볼린저</a> · <a href="/backtest-winrate-c2">V11.14.7 C2</a> · <a href="/">V2 메인</a></section>
</main><script>
'use strict';
const $=id=>document.getElementById(id),KEY='bithumb_v111412_fibbtc_checkpoint_1';
let state=null,running=false,stopRequested=false,hardFailure=false;
const fields=['date','tp','sl','horizon','fee','slip'];
function cfgOf(){const c={};for(const k of fields)c[k]=$(k).value;if(!/^20\d{2}-\d{2}-\d{2}$/.test(c.date))throw Error('날짜를 선택해');for(const k of fields.slice(1))if(!Number.isFinite(Number(c[k])))throw Error('입력 확인: '+k);return c;}
function signature(c){return fields.map(k=>String(c[k])).join('|');}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function load(){try{const x=JSON.parse(localStorage.getItem(KEY)||'null');return x?.version===12&&Array.isArray(x.markets)&&x.config&&x.results?x:null;}catch{return null;}}
function setConfig(c){for(const k of fields)$(k).value=c[k];}
function sampleResults(){return state?state.markets.filter(x=>Object.prototype.hasOwnProperty.call(state.results,x)).map(x=>state.results[x]):[];}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function card(k,v){return `<div class="stat"><small>${esc(k)}</small><strong>${esc(v)}</strong></div>`;}
function fmt(x){return Number.isFinite(x)?x.toFixed(3)+'%':'—';}
function decided(e){return e?.c&&['TP','SL','TIME'].includes(e.c.outcome)&&Number.isFinite(e.c.net_pct);}
function stat(es){const n=es.length,sum=es.reduce((s,e)=>s+e.c.net_pct,0),wins=es.filter(e=>e.c.net_pct>0).length;return {n,sum,avg:n?sum/n:NaN,wins,win:n?100*wins/n:NaN,tp:es.filter(e=>e.c.outcome==='TP').length,sl:es.filter(e=>e.c.outcome==='SL').length,tm:es.filter(e=>e.c.outcome==='TIME').length};}
function cohort(e,k){const f=e.fib||{},b=e.btc||{};if(k==='all')return true;if(k==='fib')return f.eligible===true;if(k==='btc')return b.eligible===true;if(k==='both')return f.eligible===true&&b.eligible===true;return false;}
const plans=[
 {co:'all',name:'기존 C 전체 (기준)',pass:e=>true},
 {co:'fib',name:'피보나치 비교 · 기존 C',pass:e=>true},
 {co:'fib',name:'피보나치 A · 38.2~61.8%',pass:e=>e.fib.narrow_pass===true},
 {co:'fib',name:'피보나치 B · 23.6~78.6%',pass:e=>e.fib.broad_pass===true},
 {co:'btc',name:'BTC 비교 · 기존 C',pass:e=>true},
 {co:'btc',name:'BTC 대비 상대강도 +0.5%p',pass:e=>e.btc.relative_pass===true},
 {co:'both',name:'공통 계산 가능 · 기존 C',pass:e=>true},
 {co:'both',name:'피보나치 A + BTC 상대강도',pass:e=>e.fib.narrow_pass===true&&e.btc.relative_pass===true},
 {co:'both',name:'피보나치 B + BTC 상대강도',pass:e=>e.fib.broad_pass===true&&e.btc.relative_pass===true}
];
function table(base){let html='<table><thead><tr><th>비교 방식</th><th>공통 계산 가능 C</th><th>진입</th><th>TP/SL/시간</th><th>승률</th><th>진입당 순평균</th><th>미진입=0% 신호당 순평균</th><th>제외</th><th>제외한 수익거래</th></tr></thead><tbody>';
 for(const p of plans){const es=base.filter(e=>cohort(e,p.co)),kept=es.filter(p.pass),removed=es.filter(e=>!p.pass(e)),s=stat(kept);html+=`<tr><td>${esc(p.name)}</td><td>${es.length}</td><td>${s.n}</td><td>${s.tp}/${s.sl}/${s.tm}</td><td>${fmt(s.win)}</td><td>${fmt(s.avg)}</td><td>${fmt(es.length?s.sum/es.length:NaN)}</td><td>${removed.length}</td><td>${removed.filter(e=>e.c.net_pct>0).length}</td></tr>`;}
 return html+'</tbody></table>';}
function renderResults(arr){const events=arr.flatMap(r=>(r.events||[]).map(e=>({...e,market:r.market}))),base=events.filter(decided),all=stat(base),fib=base.filter(e=>cohort(e,'fib')),btc=base.filter(e=>cohort(e,'btc')),both=base.filter(e=>cohort(e,'both'));
 $('summary').innerHTML=card('원본 C 신호',events.length+'건')+card('원본 C 손익 판정',base.length+'건')+card('원본 C 평균',fmt(all.avg))+card('피보나치 계산 가능',fib.length+'건')+card('피보나치 상승 파동 충족',fib.filter(e=>e.fib.swing_valid===true).length+'건')+card('BTC 1시간 상대강도 계산 가능',btc.length+'건')+card('피보나치·BTC 모두 계산 가능',both.length+'건')+card('피보나치 5분봉 부족',base.length-fib.length+'건')+card('BTC 1시간 봉 부족',base.length-btc.length+'건');
 $('results').innerHTML=table(base);$('first').innerHTML=table(base.filter(e=>e.is_first_signal));}
function paint(){const a=sampleResults(),n=a.length,total=state?.markets.length||0,complete=total>0&&n===total&&state?.failed===0,signals=a.reduce((s,r)=>s+r.signals,0);
 $('progress').max=total||1;$('progress').value=n;
 $('coverage').innerHTML=card('검사 진행',`${n}/${total}`)+card('C 신호',signals+'건')+card('유효 C 검색 구간',a.reduce((s,r)=>s+(r.valid_windows||0),0)+'개')+card('5분봉 240개 이상',a.filter(x=>x.observed_5m>=240).length+'코인')+card('5분봉 조회 상한 도달',a.filter(x=>x.truncated).length+'코인');
 if(complete){renderResults(a);$('csv').disabled=false;}else{$('summary').textContent='전체 KRW 검사 완료 후 공개';$('results').textContent='전체 검사 후 공개';$('first').textContent='전체 검사 후 공개';$('csv').disabled=true;}
 $('status').textContent=state?(complete?'✅ 전체 검사 완료':'⏳ 검사 중 또는 중단')+` · ${n}/${total}종목 · C 신호 ${signals}건`+(state.failed?' · 실패 묶음 재검사 필요':''):'검사 전';}
async function req(url,opts){const ctl=new AbortController(),tid=setTimeout(()=>ctl.abort(),115000);try{const r=await fetch(url,{...opts,cache:'no-store',signal:ctl.signal}),v=await r.json();if(!r.ok||!v.ok)throw Error(v.error||'HTTP '+r.status);return v;}finally{clearTimeout(tid);}}
async function initSession(c){const v=await req('/api/winrate14fibbtc/universe');if(!Array.isArray(v.items)||!v.items.length)throw Error('KRW 목록이 없습니다');state={version:12,config:c,markets:v.items,results:{},failed:0};save();}
async function run(){if(running)return;let c;try{c=cfgOf();}catch(e){$('status').textContent='⚠️ '+e.message;return;}
 if(state&&signature(state.config)!==signature(c)){if(!confirm('날짜·설정이 변경됐어. 이 버전의 이전 검사 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);}
 running=true;stopRequested=false;hardFailure=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;
 try{if(!state)await initSession(c);state.failed=0;save();paint();const pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m)),batches=[];for(let i=0;i<pending.length;i+=2)batches.push(pending.slice(i,i+2));let pos=0,fail=0;
 async function worker(){while(!stopRequested&&!hardFailure){const batch=batches[pos++];if(!batch)break;let response=null;
 for(let attempt=0;attempt<3&&!stopRequested;attempt++){try{const v=await req('/api/winrate14fibbtc/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:batch})});
 if(v.errors||v.scanned!==batch.length||!Array.isArray(v.results)||v.results.length!==batch.length||v.results.some(r=>!batch.includes(r.market)||!Array.isArray(r.events)||r.events.length!==r.signals||r.events.some(e=>!e.c||!e.fib||!e.btc)))throw Error('불완전한 응답');
 response=v;break;}catch(e){if(attempt===2){fail++;$('status').textContent='⚠️ '+batch.join(', ')+' 실패 '+String(e.message||e);}else await new Promise(ok=>setTimeout(ok,1000*(attempt+1)));}}
 if(response){for(const x of response.results)state.results[x.market]=x;try{save();}catch(e){hardFailure=true;$('status').textContent='⚠️ 저장 공간 부족';break;}paint();}
 if(fail>=4){hardFailure=true;break;}}}
 await Promise.all([worker(),worker()]);state.failed=fail;try{save();}catch{}paint();if(stopRequested)$('status').textContent+='\n일시 중지. 다시 시작하면 이어서 검사합니다.';if(fail)$('status').textContent+=`\n누락 묶음 ${fail}개 · 다시 실행해서 재검사하세요.`;
 }catch(e){$('status').textContent='⚠️ '+String(e.message||e)+' · 전체 검사 전 성과 미공개';}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}}
$('run').onclick=run;
$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;};
$('reset').onclick=()=>{if(running)return;if(!confirm('V11.14.12 진행 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(v){const s=String(v??'');return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!state||sampleResults().length!==state.markets.length||state.failed)return;
 const cols=['version','date','tp_pct','sl_pct','horizon_minutes','fee_pct_side','slip_pct_side','market','observed_5m','c_signals','original_signal_time_kst','c_entry_time_kst','is_first_signal','c_outcome','c_reason','c_entry','c_exit','c_minutes','c_net_pct','fib_eligible','fib_reason','fib_swing_valid','fib_low_time_kst','fib_high_time_kst','fib_low','fib_high','fib_impulse_pct','fib_peak_age_bars','fib_signal_low','fib_retracement_pct','fib_level_382','fib_level_500','fib_level_618','fib_level_786','fib_narrow_pass','fib_broad_pass','btc_eligible','btc_reason','btc_1h_pct','coin_1h_pct','relative_1h_pp','btc_relative_pass','combo_narrow_pass','combo_broad_pass','original_volume_ratio','original_body_pct','original_pullback_pct'];
 const rows=[cols.map(csvCell).join(',')],prefix={version:'V11.14.12',date:state.config.date,tp_pct:state.config.tp,sl_pct:state.config.sl,horizon_minutes:state.config.horizon,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip};
 function append(obj){rows.push(cols.map(k=>csvCell(obj[k])).join(','));}
 for(const r of sampleResults()){if(!r.signals){append({...prefix,market:r.market,observed_5m:r.observed_5m,c_signals:0});continue;}
 if(r.events.length!==r.signals){$('status').textContent='⚠️ CSV 이벤트 수 불일치';return;}
 for(const e of r.events){const c=e.c||{},f=e.fib||{},b=e.btc||{},ft=e.features||{};
 append({...prefix,market:r.market,observed_5m:r.observed_5m,c_signals:r.signals,original_signal_time_kst:e.signal_time_kst,c_entry_time_kst:e.c_entry_time_kst,is_first_signal:e.is_first_signal?1:0,c_outcome:c.outcome,c_reason:c.reason,c_entry:c.entry,c_exit:c.exit,c_minutes:c.minutes,c_net_pct:c.net_pct,
 fib_eligible:f.eligible?1:0,fib_reason:f.reason,fib_swing_valid:f.eligible?(f.swing_valid?1:0):'',fib_low_time_kst:f.low_time_kst,fib_high_time_kst:f.high_time_kst,fib_low:f.impulse_low,fib_high:f.impulse_high,fib_impulse_pct:f.impulse_pct,fib_peak_age_bars:f.peak_age_bars,fib_signal_low:f.signal_low,fib_retracement_pct:f.retracement_pct,fib_level_382:f.level_382,fib_level_500:f.level_500,fib_level_618:f.level_618,fib_level_786:f.level_786,fib_narrow_pass:f.eligible?(f.narrow_pass?1:0):'',fib_broad_pass:f.eligible?(f.broad_pass?1:0):'',
 btc_eligible:b.eligible?1:0,btc_reason:b.reason,btc_1h_pct:b.btc_1h_pct,coin_1h_pct:b.coin_1h_pct,relative_1h_pp:b.relative_1h_pp,btc_relative_pass:b.eligible?(b.relative_pass?1:0):'',combo_narrow_pass:f.eligible&&b.eligible?(f.narrow_pass&&b.relative_pass?1:0):'',combo_broad_pass:f.eligible&&b.eligible?(f.broad_pass&&b.relative_pass?1:0):'',
 original_volume_ratio:ft.volume_ratio,original_body_pct:ft.body_pct,original_pullback_pct:ft.pullback_depth_pct});}}
 const blob=new Blob(['\ufeff'+rows.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`bithumb_full_krw_C_FIB_BTC_${state.config.date}_v111412.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
state=load();if(state)setConfig(state.config);paint();

</script></body></html>"""


def register_fib_btc_v111412_lab(app,deps):
    api_get,get_markets,kst=deps['api_get'],deps['get_markets'],deps['KST']
    cache={'names':None,'names_at':0,'btc':{},'btc_at':{}}
    mutex=Lock()

    def listed():
        with mutex:
            if cache['names'] is None or time.time()-cache['names_at']>600:
                names=sorted({m for m in get_markets() if isinstance(m,str) and MARKET_RE.fullmatch(m)})
                if not names:raise ValueError('현재 빗썸 KRW 목록이 없습니다')
                cache['names']=names;cache['names_at']=time.time()
            return cache['names']

    def btc_for_day(cfg):
        key=cfg['day'].isoformat()
        with mutex:
            if key not in cache['btc'] or time.time()-cache['btc_at'].get(key,0)>1800:
                bars,_=lab_fetch_5m(api_get,'KRW-BTC',cfg,kst)
                if not bars:raise ValueError('BTC 5분봉 데이터가 비어 있습니다')
                cache['btc']={key:{r['t']:r for r in bars}}
                cache['btc_at']={key:time.time()}
            return cache['btc'][key]

    @app.get('/backtest-winrate-fib-btc')
    def page_fib_btc_v111412():
        return render_template_string(FIB_BTC_V111412_PAGE)

    @app.get('/api/winrate14fibbtc/universe')
    def universe_fib_btc_v111412():
        try:
            names=listed()
            return jsonify({'ok':True,'items':names,'count':len(names),
                            'universe_type':'current_KRW_listings',
                            'rule_version':'V11.14.12'})
        except Exception as e:return jsonify({'ok':False,'error':str(e)[:160]}),400

    @app.post('/api/winrate14fibbtc/evaluate')
    def evaluate_fib_btc_v111412():
        try:
            payload=request.get_json(silent=True)
            cfg=lab_valid_request(payload,kst)
            markets=payload.get('markets') if isinstance(payload,dict) else None
            if (not isinstance(markets,list) or not 1<=len(markets)<=2
                    or len(set(markets))!=len(markets)
                    or any(not isinstance(m,str) or not MARKET_RE.fullmatch(m) for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 검사합니다')
            known=set(listed())
            if any(m not in known for m in markets):
                raise ValueError('현재 KRW 목록에 없는 종목입니다')
            btc_by_time=btc_for_day(cfg)
            results,errors=[],[]
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs={pool.submit(fib_v111412_evaluate_symbol,api_get,m,cfg,kst,btc_by_time):m for m in markets}
                for job in as_completed(jobs):
                    try:results.append(job.result())
                    except Exception as exc:errors.append({'market':jobs[job],'error':str(exc)[:120]})
            return jsonify({'ok':not errors,'scanned':len(markets),
                            'results':results,'errors':len(errors),
                            'error_markets':errors,'rule_version':'V11.14.12'})
        except Exception as e:return jsonify({'ok':False,'error':str(e)[:160]}),400

_v111411_register_daytrade_review=register_daytrade_review

def register_daytrade_review(app,deps):
    _v111411_register_daytrade_review(app,deps)
    register_fib_btc_v111412_lab(app,deps)


# ---------------------------------------------------------------------------
# V11.14.13: Independent C signal loss attribution / regime diagnostic lab.
# NO new trading conditions, no changes to C entry/exit or older endpoints.
# BTC 1h return uses the already completed, matched 5m bars at signal close.
# Historical observed-candle gaps are diagnostic: do NOT synthesize candles
# and do NOT assert the cause of missing API candles without trade-level proof.
# ---------------------------------------------------------------------------
LOSSDIAG_V111413_BTC_BAND_PCT = 0.30  # Fixed, pre-specified 1h BTC regimes
LOSSDIAG_V111413_4H_BARS = 48


def lossdiag_v111413_history(rows, i, truncated):
    """Explain 4h lookback coverage using only bars at or before signal.

    A 49-slot grid includes the C signal bar and the preceding 48 five-minute
    slots. Beginning-of-data shortage and holes *within the observed range*
    have different labels. Neither label proves zero trading vs API omission.
    """
    at = rows[i]['t']
    start = at - timedelta(minutes=5*LOSSDIAG_V111413_4H_BARS)
    expected = LOSSDIAG_V111413_4H_BARS + 1
    observed = {r['t'] for r in rows[:i+1] if start <= r['t'] <= at}
    missing = expected - len(observed)
    earliest = rows[0]['t']
    start_missing = max(0, min(expected, int((earliest-start).total_seconds()//300)))
    internal_missing = max(0, missing-start_missing)
    if internal_missing:
        status, reason = 'observed_gap', '4시간 내 관측 5분봉 누락 (거래 부재/API 원인 미확정)'
    elif start_missing and truncated:
        status, reason = 'page_limit', '초기 기록 부족 + 조회 상한 도달 가능성'
    elif start_missing:
        status, reason = 'history_short', '해당 범위의 첫 관측 기록 부족 (원인 미확정)'
    else:
        status, reason = 'complete', '4시간 연속 관측'
    return {'status':status, 'reason':reason,
            'observed_slots':len(observed), 'expected_slots':expected,
            'missing_slots':missing,'internal_missing':internal_missing,
            'start_missing':start_missing,
            'oldest_observed_kst':earliest.strftime('%Y-%m-%d %H:%M')}


def lossdiag_v111413_evaluate_symbol(api_get, market, cfg, kst, btc_by_time):
    """Exact V11.14.12 baseline C signal/cooldown/outcome. Diagnosis only."""
    rows, truncated = vwb_v111411_fetch_5m(api_get,market,cfg,kst)
    start = datetime.combine(cfg['day'],_time.min,tzinfo=kst)
    end = start + timedelta(days=1)
    day_rows = [r for r in rows if start <= r['t'] < end]
    n, valid, signals, unknown, cooldown = cfg['bars'],0,0,0,-1
    events=[]
    if len(rows)>=LAB_WARMUP_BARS+2:
        for i in range(LAB_WARMUP_BARS,len(rows)-1):
            if not start <= rows[i]['t'] < end: continue
            if not lab_contiguous(rows,i-24,i): continue
            valid += 1
            if i < cooldown or 'C' not in lab_signals(rows,i): continue
            cooldown = i+n
            signals += 1
            out = lab_outcome_v11145(rows,i,cfg)
            if out['outcome']=='UNKNOWN':unknown+=1
            events.append({
                'signal_time_kst':rows[i]['t'].strftime('%Y-%m-%d %H:%M'),
                'c_entry_time_kst':(rows[i]['t']+timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M'),
                'signal_number':signals,
                'is_first_signal':signals==1,
                'c':out,
                'features':lab_c_signal_features(rows,i),
                'btc':vwb_v111411_btc_snapshot(rows,i,btc_by_time),
                'history':lossdiag_v111413_history(rows,i,truncated)})
    assert len(events)==signals
    return {'market':market,'observed_5m':len(day_rows),
            'day_missing_5m':max(0,288-len(day_rows)),
            'longest_day_run':lab_longest_consecutive_5m(day_rows),
            'valid_windows':valid,'signals':signals,'unknown':unknown,
            'truncated':truncated,'events':events}

LOSSDIAG_V111413_PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.13 · C 신호 손실 원인 분석실</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#1a2038;--card:#121a2e;--border:#35425f;--fg:#f2f5ff;--muted:#afc0db;--blue:#3867ee;--green:#4ade80;--warn:#ffc46c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1050px;margin:auto;padding:18px 14px 70px}h1{font-size:23px;margin:8px 0}h2{font-size:18px;margin:0 0 8px}
.panel{border:1px solid var(--border);background:var(--panel);border-radius:16px;padding:16px;margin-top:15px}
.muted,.hint{font-size:13px;color:var(--muted)}.note{padding:12px;background:#101a30;border-radius:10px;font-size:13px;line-height:1.75}
.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.field{display:flex;flex-direction:column;font-size:13px;color:var(--muted);gap:5px}
input,button,select{font:inherit}input,select{padding:10px;background:#10182d;border:1px solid var(--border);color:var(--fg);border-radius:8px;width:100%;min-width:0}
button{background:var(--blue);border:0;border-radius:9px;color:#fff;padding:12px 14px;font-weight:800;cursor:pointer}button:disabled{opacity:.48}.secondary{background:#364660}
.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px;align-items:center}a{color:#bcd0ff}progress{width:100%;height:15px;margin-top:14px}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.stat{border:1px solid var(--border);background:var(--card);border-radius:10px;padding:11px}
.stat small{font-size:12px;color:var(--muted);display:block}.stat strong{font-size:22px;display:block}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--border);padding:9px 7px;text-align:right;white-space:nowrap}td:first-child,th:first-child{text-align:left}
.status{font-size:13px;white-space:pre-wrap;color:var(--muted)}
@media(max-width:650px){.controls{grid-template-columns:repeat(2,minmax(0,1fr))}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}

h3{font-size:15px;margin:18px 0 6px}.bad{color:#ffc1c1}.good{color:#78eca1}.small{font-size:12px;color:var(--muted)}input[type=file]{max-width:100%}
</style></head><body><main>
<h1>🔎 V11.14.13 · C 신호 손실 원인 분석실</h1>
<p class="muted">기존 BUY와 C 신호의 진입·익절·손절 규칙은 그대로. 신규 매매 필터가 아닌 독립 진단용 연구실입니다.</p>
<section class="panel"><h2>이번 분석의 고정 기준</h2><div class="note">
<b>기존 C:</b> 5분봉 확정 C 신호 → 다음 5분봉 시가 모의 진입, 기존 비용·익절·손절·보유 조건 동일.<br>
<b>BTC 상승:</b> 신호 시각까지 확정된 BTC 최근 1시간 수익률 ≥ +0.30%.<br>
<b>BTC 횡보:</b> -0.30% 초과, +0.30% 미만. <b>BTC 하락:</b> ≤ -0.30%. 정확히 같은 시각의 BTC·코인 5분봉을 사용합니다.<br>
<b>첫 신호:</b> 한 종목의 검증 날짜 내 첫 C 신호. <b>반복 신호:</b> 그날 같은 종목에서 두 번째 이후 C 신호.<br>
<b>5분봉 연속성:</b> 신호봉 포함 직전 4시간(49개 시간 슬롯) 중 실제 관측된 봉을 검사합니다. 최초 기록 부족, 조회 상한 의심, 내부 공백을 따로 분류하되 원인은 확정하지 않습니다.<br>
<b>해석:</b> 승률보다 평균 순손익, 판정 완료 수, 서로 다른 종목 수를 함께 확인하세요. 미판정은 성과 분모에서 제외합니다. 거래 수가 적거나 반복 신호가 많은 그룹은 과신하지 마세요.
</div></section>
<section class="panel"><h2>기존 CSV 7일치 즉석 분석 (서버 전송 없음)</h2>
<p class="hint">이미 받은 <b>V11.14.12 피보나치/BTC CSV를 여러 개 동시 선택</b>하면 재스캔 없이 BTC 장세·첫/반복 신호를 비교할 수 있어요. 옛 CSV로는 공백의 정확한 유형을 구분하지 못합니다. 이 기능은 별도 연구용이며 실전 스캐너를 변경하지 않습니다.</p>
<input id="importFiles" type="file" accept=".csv,text/csv" multiple aria-label="V11.14.12 CSV 여러 개 선택">
<p id="importStatus" class="status">파일을 선택하면 즉시 분석합니다. 같은 날짜·종목·시각의 중복 신호는 1건으로 처리합니다.</p>
<div id="importStats">CSV 불러오기 전</div></section>
<section class="panel"><h2>새 날짜 전체 KRW 검사</h2><div class="controls">
<label class="field">검증 날짜 (KST)<input id="date" type="date" value="2026-11-08"></label>
<label class="field">익절 (%)<input id="tp" type="number" min="0.3" max="10" step="0.1" value="1.5"></label>
<label class="field">손절 (%)<input id="sl" type="number" min="0.2" max="8" step="0.1" value="1.0"></label>
<label class="field">최대 보유<select id="horizon"><option value="30">30분</option><option value="60" selected>60분</option><option value="120">120분</option></select></label>
<label class="field">편도 수수료 (%)<input id="fee" type="number" min="0" max="0.5" step="0.01" value="0.04"></label>
<label class="field">편도 슬리피지 (%)<input id="slip" type="number" min="0" max="0.5" step="0.01" value="0.05"></label>
</div><div class="actions"><button id="run">전체 KRW 검사 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">V11.14.13 진행 기록 초기화</button><button id="csv" class="secondary" disabled>전체 손실 진단 CSV 저장</button></div>
<progress id="progress" value="0" max="1"></progress><p id="status" class="status">검사 전</p>
<p class="hint">완료 전에는 새 전체 검사 통계를 공개하지 않습니다. 진행 기록은 이 브라우저에만 저장됩니다. 날짜·설정을 바꾸면 이 버전의 진행 기록만 초기화합니다.</p></section>
<section class="panel"><h2>전체 종목 관측 현황</h2><div class="stats" id="coverage">검사 전</div></section>
<section class="panel"><h2>신호 시점 4시간 캔들 공백 진단</h2><div class="stats" id="gapStats">전체 검사 완료 후 공개</div>
<p class="hint">'관측 공백'은 API에서 유효한 5분봉이 관측되지 않은 구간이며, 거래가 없어서인지 API/파싱 문제인지를 증명하지 않습니다. '최초 기록 부족'도 원인 미확정입니다. 생존자 편향과 과거 API 지원 범위의 제한이 있습니다.</p></section>
<section class="panel"><h2>새 날짜 손실 원인 통계</h2><div id="liveStats">전체 검사 완료 후 공개</div></section>
<section class="panel"><h2>기존 검증실 / 실전 스캐너</h2><p class="hint">수익성이 검증된 BUY 신호가 아니며 주문 기능이 없습니다. 이전 연구 날짜를 계속 선택해 조건을 조정하면 과적합 위험이 있습니다. 다른 날짜·시장 상황에서 새로 검증하세요.</p>
<a href="/backtest-winrate-fib-btc">V11.14.12 피보나치/BTC</a> · <a href="/backtest-winrate-vwap-btc">V11.14.11 VWAP/BTC</a> · <a href="/backtest-winrate-htf-macd">V11.14.10 MACD</a> · <a href="/backtest-winrate-ich">V11.14.9 일목</a> · <a href="/backtest-winrate-bb">V11.14.8 볼린저</a> · <a href="/backtest-winrate-c2">V11.14.7 C2</a> · <a href="/">V2 메인</a></section>
</main><script>
'use strict';
const $=id=>document.getElementById(id);
const KEY='bithumb_c_lossdiag_v111413_checkpoint';
const API='/api/winrate14lossdiag';
let state=null,running=false,stopRequested=false,hardFailure=false,imports=[];
function escapeHtml(v){return String(v??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));}
function number(v){return Number.isFinite(Number(v))?Number(v):NaN;}
function pct(v){return Number.isFinite(v)?v.toFixed(3)+'%':'—';}
function mean(a){return a.length?a.reduce((s,x)=>s+x,0)/a.length:NaN;}
function config(){const o={date:$('date').value,tp:number($('tp').value),sl:number($('sl').value),horizon:number($('horizon').value),fee:number($('fee').value),slip:number($('slip').value)};
 if(!/^\d{4}-\d{2}-\d{2}$/.test(o.date)||!(o.tp>=0.3&&o.tp<=10&&o.sl>=0.2&&o.sl<=8&&[30,60,120].includes(o.horizon)&&o.fee>=0&&o.fee<=0.5&&o.slip>=0&&o.slip<=0.5))throw Error('날짜 또는 검사 설정을 확인하세요');return o;}
function fingerprint(c){return JSON.stringify(c);}
function restore(){try{const x=JSON.parse(localStorage.getItem(KEY)||'null');if(x&&x.version===13&&Array.isArray(x.markets)&&x.results&&x.config)return x;}catch{}return null;}
function persist(){localStorage.setItem(KEY,JSON.stringify(state));}
function configToForm(c){for(const k of ['date','tp','sl','horizon','fee','slip'])if(c[k]!=null)$(k).value=c[k];}
function allSymbols(){return !state?[]:state.markets.filter(m=>Object.prototype.hasOwnProperty.call(state.results,m)).map(m=>state.results[m]);}
function finished(){return !!(state&&state.failed===0&&allSymbols().length===state.markets.length);}
function eventsOf(results,day){let a=[];for(const r of results)for(const e of r.events||[]){a.push({date:day,market:r.market,signal:e.signal_time_kst,seq:e.signal_number||0,first:!!e.is_first_signal,outcome:e.c?.outcome||'UNKNOWN',net:e.c?.outcome==='UNKNOWN'?NaN:number(e.c?.net_pct),btc_1h:e.btc?.eligible?number(e.btc.btc_1h_pct):NaN,coin_1h:e.btc?.eligible?number(e.btc.coin_1h_pct):NaN,relative:e.btc?.eligible?number(e.btc.relative_1h_pp):NaN,gap:e.history?.status||'미판정',missing:e.history?.missing_slots??null,gapDetail:e.history?.reason||'',c_entry:e.c?.entry??null,c_exit:e.c?.exit??null,minutes:e.c?.minutes??null,volume:e.features?.volume_ratio??null});}return a;}
function regime(e){if(!Number.isFinite(e.btc_1h))return 'BTC 계산 불가';if(e.btc_1h>=0.3)return 'BTC 상승';if(e.btc_1h<=-0.3)return 'BTC 하락';return 'BTC 횡보';}
function stats(rows){let known=rows.filter(x=>Number.isFinite(x.net)),n=known.length, wins=known.filter(x=>x.net>0).length;return {signals:rows.length,n,missing:rows.length-n,wins,wr:n?wins/n*100:NaN,avg:mean(known.map(x=>x.net)),symbols:new Set(known.map(x=>x.market)).size,tp:known.filter(x=>x.outcome==='TP').length,sl:known.filter(x=>x.outcome==='SL').length,time:known.filter(x=>x.outcome==='TIME').length};}
function card(label,value){return `<div class="stat"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong></div>`;}
function summaryRow(name,arr){const s=stats(arr);return `<tr><td>${escapeHtml(name)}</td><td>${s.signals}</td><td>${s.n}</td><td>${s.symbols}</td><td>${s.wins}</td><td>${pct(s.wr)}</td><td class="${s.avg<0?'bad':'good'}">${pct(s.avg)}</td><td>${s.tp}/${s.sl}/${s.time}</td><td>${s.missing}</td></tr>`;}
function table(title,groups){return `<h3>${escapeHtml(title)}</h3><div class="scroll"><table><thead><tr><th>구분</th><th>신호</th><th>판정</th><th>종목</th><th>수익</th><th>수익비율</th><th>평균 순손익</th><th>TP/SL/TIME</th><th>미판정</th></tr></thead><tbody>${groups.map(([name,rows])=>summaryRow(name,rows)).join('')}</tbody></table></div>`;}
function renderAnalytics(ev,target){if(!ev.length){$(target).textContent='분석할 C 신호가 없습니다.';return;}
 const group=(pred)=>ev.filter(pred);
 const chunks=[table('전체 및 BTC 시장 방향',[
 ['기존 C 전체',ev],['BTC 상승 (1h ≥ +0.3%)',group(x=>regime(x)==='BTC 상승')],['BTC 횡보 (-0.3% 초과 ~ +0.3% 미만)',group(x=>regime(x)==='BTC 횡보')],['BTC 하락 (1h ≤ -0.3%)',group(x=>regime(x)==='BTC 하락')],['BTC 계산 불가',group(x=>regime(x)==='BTC 계산 불가')]]),
 table('첫 신호 / 반복 신호', [['종목별 당일 첫 C',group(x=>x.first)],['같은 종목 당일 반복 C',group(x=>!x.first)]]),
 table('BTC 장세 × 첫/반복', ['BTC 상승','BTC 횡보','BTC 하락','BTC 계산 불가'].flatMap(r=>[['첫 신호',true],['반복 신호',false]].map(([label,f])=>[r+' · '+label,group(x=>regime(x)===r&&x.first===f)]))),
 table('4시간 5분봉 연속성 / 기록 부족',[['4시간 연속 관측',group(x=>x.gap==='complete')],['구간 내 관측 공백',group(x=>x.gap==='observed_gap')],['시작 기록 부족',group(x=>x.gap==='history_short')],['조회 상한 의심',group(x=>x.gap==='page_limit')],['진단 정보 없음',group(x=>x.gap==='미판정'||!x.gap)]]),
 table('날짜별 전체 C 손익', [...new Set(ev.map(x=>x.date))].sort().map(d=>[d,group(x=>x.date===d)]))];
 $(target).innerHTML=chunks.join('');
}
function paint(){let done=allSymbols(),complete=finished(),total=state?.markets.length||0,signalCount=done.reduce((s,r)=>s+(r.signals||0),0);
 $('progress').max=total||1;$('progress').value=done.length;
 $('coverage').innerHTML=card('검사 완료 종목',`${done.length}/${total}`)+card('기존 C 신호',signalCount+'건')+card('당일 5분봉 288개 관측',done.filter(r=>r.observed_5m===288).length+'종목')+card('조회 페이지 상한',done.filter(r=>r.truncated).length+'종목');
 $('status').textContent=state?(complete?'✅ 전체 시장 검사 완료':(running?'⏳ 진행 중':'검사 중단 또는 미완료'))+` · ${done.length}/${total}종목 · C 신호 ${signalCount}건`+(state.failed?' · 재시도 필요한 묶음 '+state.failed:''):'검사 전';
 if(complete){const ev=eventsOf(done,state.config.date);renderAnalytics(ev,'liveStats');const gaps=ev.filter(x=>x.gap==='observed_gap'),short=ev.filter(x=>x.gap==='history_short'),limit=ev.filter(x=>x.gap==='page_limit');$('gapStats').innerHTML=card('4시간 관측 공백 신호',gaps.length+'건')+card('최초 기록 부족 신호',short.length+'건')+card('조회 상한 의심',limit.length+'건')+card('손익 미판정',ev.filter(x=>!Number.isFinite(x.net)).length+'건');$('csv').disabled=false;}else{$('liveStats').textContent='전체 KRW 검사 완료 후에만 결과를 공개합니다.';$('gapStats').textContent='전체 검사 완료 후 공개';$('csv').disabled=true;}}
async function call(url,opts){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),115000);try{const r=await fetch(url,{...opts,signal:controller.signal,cache:'no-store'}),data=await r.json();if(!r.ok||!data.ok)throw Error(data.error||'HTTP '+r.status);return data;}finally{clearTimeout(timer);}}
async function init(c){const u=await call(API+'/universe');if(!Array.isArray(u.items)||!u.items.length)throw Error('KRW 종목이 없습니다');state={version:13,config:c,markets:u.items,results:{},failed:0};persist();}
async function run(){if(running)return;let c;try{c=config();}catch(e){$('status').textContent='⚠️ '+e.message;return;}
 if(state&&fingerprint(state.config)!==fingerprint(c)){if(!confirm('검사 설정이 변경됐어. V11.14.13 이전 검사만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);}
 running=true;stopRequested=false;hardFailure=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;
 try{if(!state)await init(c);state.failed=0;persist();paint();const pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m)),batches=[];for(let i=0;i<pending.length;i+=2)batches.push(pending.slice(i,i+2));let pos=0,failed=0;
 async function worker(){while(!stopRequested&&!hardFailure){const batch=batches[pos++];if(!batch)break;let response=null;
 for(let attempt=0;attempt<3&&!stopRequested;attempt++){try{const out=await call(API+'/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:batch})});if(out.errors||out.scanned!==batch.length||!Array.isArray(out.results)||out.results.length!==batch.length||out.results.some(r=>!batch.includes(r.market)||!Array.isArray(r.events)||r.events.length!==r.signals||r.events.some(e=>!e.c||!e.history||!e.btc)))throw Error('불완전한 응답');response=out;break;}catch(e){if(attempt===2){failed++;$('status').textContent='⚠️ '+batch.join(', ')+' 실패: '+String(e.message||e);}else await new Promise(ok=>setTimeout(ok,1000*(attempt+1)));}}
 if(response){for(const r of response.results)state.results[r.market]=r;try{persist();}catch(e){hardFailure=true;$('status').textContent='⚠️ 브라우저 저장 공간 부족';break;}paint();}if(failed>=4){hardFailure=true;break;}}}
 await Promise.all([worker(),worker()]);state.failed=failed;try{persist();}catch{}paint();if(stopRequested)$('status').textContent+='\n검사가 일시 중지됨. 다시 누르면 이어서 검사합니다.';if(failed)$('status').textContent+='\n누락 묶음 '+failed+'개 · 다시 실행해 재검사하세요.';
 }catch(e){$('status').textContent='⚠️ '+String(e.message||e);}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}}
$('run').onclick=run;
$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;};
$('reset').onclick=()=>{if(running)return;if(!confirm('V11.14.13 진행 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(v){const s=String(v??'');return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!finished())return;const cols=['version','date','tp_pct','sl_pct','horizon_minutes','fee_pct_side','slip_pct_side','market','observed_5m','day_missing_5m','history_truncated','c_signals','signal_time_kst','c_entry_time_kst','signal_number','is_first_signal','c_outcome','c_reason','c_entry','c_exit','c_minutes','c_net_pct','btc_eligible','btc_reason','btc_regime','btc_1h_pct','coin_1h_pct','relative_1h_pp','history_status','history_reason','history_observed_slots','history_missing_slots','history_internal_missing','history_start_missing','original_volume_ratio','original_body_pct','original_pullback_pct'];
 const lines=[cols.join(',')],prefix={version:'V11.14.13',date:state.config.date,tp_pct:state.config.tp,sl_pct:state.config.sl,horizon_minutes:state.config.horizon,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip};
 function append(o){lines.push(cols.map(k=>csvCell(o[k])).join(','));}
 for(const r of allSymbols()){if(!r.signals){append({...prefix,market:r.market,observed_5m:r.observed_5m,day_missing_5m:r.day_missing_5m,history_truncated:r.truncated?1:0,c_signals:0});continue;}
 if(!Array.isArray(r.events)||r.events.length!==r.signals){$('status').textContent='⚠️ CSV 이벤트 수 불일치';return;}
 for(const e of r.events){const b=e.btc||{},c=e.c||{},h=e.history||{},f=e.features||{};const rr=b.eligible?regime({btc_1h:number(b.btc_1h_pct)}):'BTC 계산 불가';append({...prefix,market:r.market,observed_5m:r.observed_5m,day_missing_5m:r.day_missing_5m,history_truncated:r.truncated?1:0,c_signals:r.signals,signal_time_kst:e.signal_time_kst,c_entry_time_kst:e.c_entry_time_kst,signal_number:e.signal_number,is_first_signal:e.is_first_signal?1:0,c_outcome:c.outcome,c_reason:c.reason,c_entry:c.entry,c_exit:c.exit,c_minutes:c.minutes,c_net_pct:c.net_pct,btc_eligible:b.eligible?1:0,btc_reason:b.reason,btc_regime:rr,btc_1h_pct:b.btc_1h_pct,coin_1h_pct:b.coin_1h_pct,relative_1h_pp:b.relative_1h_pp,history_status:h.status,history_reason:h.reason,history_observed_slots:h.observed_slots,history_missing_slots:h.missing_slots,history_internal_missing:h.internal_missing,history_start_missing:h.start_missing,original_volume_ratio:f.volume_ratio,original_body_pct:f.body_pct,original_pullback_pct:f.pullback_depth_pct});}}
 const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`bithumb_full_krw_C_LOSS_DIAG_${state.config.date}_v111413.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);};
// Import previously exported V11.14.12 CSV(s) locally. No network/upload.
function parseCsv(src){src=src.replace(/^\ufeff/,'');let rows=[],row=[],field='',quoted=false;for(let i=0;i<src.length;i++){const ch=src[i];if(quoted){if(ch==='"'&&src[i+1]==='"'){field+='"';i++;}else if(ch==='"')quoted=false;else field+=ch;}else{if(ch==='"')quoted=true;else if(ch===','){row.push(field);field='';}else if(ch==='\n'){row.push(field.replace(/\r$/,''));rows.push(row);row=[];field='';}else field+=ch;}}if(field.length||row.length){row.push(field.replace(/\r$/,''));rows.push(row);}if(quoted)throw Error('CSV 따옴표가 닫히지 않았습니다');return rows;}
function parseOld(text){const raw=parseCsv(text);if(!raw.length)throw Error('빈 CSV 파일');const cols=raw.shift();for(const required of ['version','date','market','original_signal_time_kst','is_first_signal','c_outcome','c_net_pct','btc_1h_pct','fib_reason'])if(!cols.includes(required))throw Error('V11.14.12 CSV 형식이 아니야: '+required+' 누락');
 const events=[];for(const cells of raw){if(!cells.some(Boolean))continue;const r={};for(let j=0;j<cols.length;j++)r[cols[j]]=cells[j]??'';if(!r.original_signal_time_kst)continue;if(r.version!=='V11.14.12')throw Error('V11.14.12 CSV만 가져올 수 있습니다');let g='complete';if(r.fib_eligible==='0'){g=r.fib_reason==='최근 4시간 5분봉 공백'?'observed_gap':'history_short';}
 const validBtc=r.btc_eligible==='1'&&r.btc_1h_pct!=='';
 const outcome=r.c_outcome||'UNKNOWN';events.push({date:r.date,market:r.market,signal:r.original_signal_time_kst,seq:0,first:r.is_first_signal==='1',outcome,net:outcome==='UNKNOWN'?NaN:number(r.c_net_pct),btc_1h:validBtc?number(r.btc_1h_pct):NaN,coin_1h:validBtc?number(r.coin_1h_pct):NaN,relative:validBtc?number(r.relative_1h_pp):NaN,gap:g,missing:null,gapDetail:r.fib_reason||''});}return events;}
$('importFiles').onchange=async()=>{const files=Array.from($('importFiles').files||[]);if(!files.length)return;try{const all=[];for(const f of files){const data=parseOld(await f.text());all.push(...data);}const seen=new Set(),unique=[];for(const e of all){const key=[e.date,e.market,e.signal].join('|');if(!seen.has(key)){seen.add(key);unique.push(e);}}
 imports=unique;const days=new Set(imports.map(e=>e.date));$('importStatus').textContent=`✅ ${files.length}개 파일에서 ${days.size}일 · C 신호 ${imports.length}건 불러옴 (중복 제거). 파일은 서버로 전송되지 않아.`;renderAnalytics(imports,'importStats');}catch(e){$('importStatus').textContent='⚠️ '+String(e.message||e);$('importStats').textContent='CSV 형식을 다시 확인해 줘.';}};
state=restore();if(state)configToForm(state.config);paint();

</script></body></html>"""



def register_lossdiag_v111413_lab(app, deps):
    api_get, get_markets, kst = deps['api_get'],deps['get_markets'],deps['KST']
    cache={'names':None,'names_at':0,'btc':{},'btc_at':{}}
    mutex=Lock()

    def listed():
        with mutex:
            if cache['names'] is None or time.time()-cache['names_at']>600:
                names=sorted({m for m in get_markets() if isinstance(m,str) and MARKET_RE.fullmatch(m)})
                if not names:raise ValueError('현재 빗썸 KRW 목록이 없습니다')
                cache['names'],cache['names_at']=names,time.time()
            return cache['names']

    def btc_for_day(cfg):
        key=cfg['day'].isoformat()
        with mutex:
            if key not in cache['btc'] or time.time()-cache['btc_at'].get(key,0)>1800:
                bars,_=lab_fetch_5m(api_get,'KRW-BTC',cfg,kst)
                if not bars:raise ValueError('BTC 5분봉 데이터가 비어 있습니다')
                cache['btc']={key:{r['t']:r for r in bars}}
                cache['btc_at']={key:time.time()}
            return cache['btc'][key]

    @app.get('/backtest-winrate-lossdiag')
    def page_lossdiag_v111413():
        return render_template_string(LOSSDIAG_V111413_PAGE)

    @app.get('/api/winrate14lossdiag/universe')
    def universe_lossdiag_v111413():
        try:
            items=listed()
            return jsonify({'ok':True,'items':items,'count':len(items),
                            'universe_type':'current_KRW_listings',
                            'rule_version':'V11.14.13'})
        except Exception as e:
            return jsonify({'ok':False,'error':str(e)[:160]}),400

    @app.post('/api/winrate14lossdiag/evaluate')
    def evaluate_lossdiag_v111413():
        try:
            payload=request.get_json(silent=True)
            cfg=lab_valid_request(payload,kst)
            markets=payload.get('markets') if isinstance(payload,dict) else None
            if (not isinstance(markets,list) or not 1<=len(markets)<=2
                    or len(set(markets))!=len(markets)
                    or any(not isinstance(m,str) or not MARKET_RE.fullmatch(m) for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 검사합니다')
            if any(m not in set(listed()) for m in markets):
                raise ValueError('현재 KRW 목록에 없는 종목입니다')
            btc_map=btc_for_day(cfg)
            results,errors=[],[]
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures={pool.submit(lossdiag_v111413_evaluate_symbol,
                                     api_get,m,cfg,kst,btc_map):m for m in markets}
                for f in as_completed(futures):
                    try:results.append(f.result())
                    except Exception as e:errors.append({'market':futures[f],'error':str(e)[:120]})
            return jsonify({'ok':not errors,'scanned':len(markets),
                            'results':results,'errors':len(errors),
                            'error_markets':errors,'rule_version':'V11.14.13'})
        except Exception as e:
            return jsonify({'ok':False,'error':str(e)[:160]}),400

_v111412_register_daytrade_review=register_daytrade_review

def register_daytrade_review(app,deps):
    _v111412_register_daytrade_review(app,deps)
    register_lossdiag_v111413_lab(app,deps)


# ---------------------------------------------------------------------------
# V11.14.14 — Independent 3-minute versus existing 5-minute C research lab.
# Original C / V2 / all older research endpoints are registered unchanged.
# Both variants use C's same price/volume thresholds, equal wall-clock windows,
# 60-minute example cooldown, TP/SL/cost convention, and next-bar entry.
# The 5-minute arm reuses the EXACT V11.14.5 C evaluator as its baseline.
# This is NOT an actual buy signal and cannot place any order.
# ---------------------------------------------------------------------------

TF_V111414_CONFIG = {
    3: {'volume': 34, 'flow_hour': 20, 'ma_reclaim': 17, 'fast': 10, 'warmup': 40},
    5: {'volume': 20, 'flow_hour': 12, 'ma_reclaim': 10, 'fast': 6, 'warmup': 26},
}


def tf_v111414_fetch_3m(api_get, market, cfg, kst):
    """KST-exclusive Bithumb 3m pagination; only observed OHLCV bars.

    Up to six pages cover the normal 4h prehistory, full selected date and
    2h forward settlement window; sparse/short history is reported, not filled.
    """
    day_start = datetime.combine(cfg['day'], _time.min, tzinfo=kst)
    begin = day_start - timedelta(hours=4)
    end = day_start + timedelta(days=1, hours=2, minutes=3)
    cursor, observed, truncated = end, {}, True
    for _ in range(6):
        raw = api_get('/v1/candles/minutes/3', {
            'market': market, 'count': 200,
            'to': cursor.strftime('%Y-%m-%d %H:%M:%S'),
        })
        if not isinstance(raw, list):
            raise ValueError('3분봉 API 응답 형식 오류')
        oldest = None
        for bar in raw:
            if not isinstance(bar, dict):
                continue
            try:
                t = local_time(bar['candle_date_time_kst'], kst)
                op, hi, lo, close = (float(bar[k]) for k in
                                     ('opening_price', 'high_price', 'low_price', 'trade_price'))
                volume = float(bar['candle_acc_trade_volume'])
                if not (all(isfinite(x) and x > 0 for x in (op, hi, lo, close))
                        and isfinite(volume) and volume >= 0
                        and lo <= min(op, close) and hi >= max(op, close) and lo <= hi):
                    continue
                # Time boundaries for 3m candles are KST-aligned.
                if t.minute % 3 != 0 or t.second != 0 or t.microsecond != 0:
                    continue
                oldest = t if oldest is None else min(oldest, t)
                if begin <= t < end:
                    observed[t] = {'t': t, 'o': op, 'h': hi, 'l': lo, 'c': close, 'v': volume}
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        if oldest is None:
            truncated = False
            break
        if oldest <= begin or len(raw) < 200:
            truncated = False
            break
        if oldest >= cursor:
            raise ValueError('3분봉 API 조회 페이지가 전진하지 않습니다')
        cursor = oldest
    return [observed[t] for t in sorted(observed)], truncated


def tf_v111414_contiguous(rows, a, b, minutes):
    if a < 0 or b >= len(rows) or a > b:
        return False
    delta = timedelta(minutes=minutes)
    return all(rows[j]['t'] - rows[j-1]['t'] == delta for j in range(a+1, b+1))


def tf_v111414_signal_3m(rows, i):
    """Wall-clock scaled C formula; NO future or current unclosed bars.

    Old+recent 1-hour averages: 20x3m instead of 12x5m.
    Short trend: 10x3m instead of 6x5m. Reclaim: ~51m instead of 50m.
    Previous volume average: ~102m instead of 100m.
    """
    n = TF_V111414_CONFIG[3]
    if i < n['warmup']:
        return False
    c = rows[i]
    vol = sum(x['v'] for x in rows[i-n['volume']:i]) / n['volume']
    if vol <= 0:
        return False
    span = max(1e-12, c['h'] - c['l'])
    bull = c['c'] > c['o'] and (c['c'] - c['o']) / span >= .30
    hour = n['flow_hour']
    ma_old = sum(x['c'] for x in rows[i-2*hour:i-hour]) / hour
    ma_recent = sum(x['c'] for x in rows[i-hour:i]) / hour
    short = n['fast']
    fast = sum(x['c'] for x in rows[i-short:i]) / short
    fast_old = sum(x['c'] for x in rows[i-short-1:i-1]) / short
    ma_reclaim = sum(x['c'] for x in rows[i-n['ma_reclaim']:i]) / n['ma_reclaim']
    return bool(bull and c['v']/vol >= .90 and ma_recent >= ma_old*1.003
                and fast >= fast_old and c['l'] <= ma_reclaim*1.005
                and c['c'] >= ma_reclaim*1.001 and c['c'] >= rows[i-1]['c'])


def tf_v111414_outcome_3m(rows, i, cfg):
    """3m equivalent of V11.14.5 conservative earliest-exit model.

    Next actual 3m candle open; SL first on a same-bar TP/SL tie. Never
    settle beyond a missing observed bar. Fees and slip are charged per side.
    """
    n = cfg['horizon']//3
    if not (0 <= i < len(rows)) or n < 1 or i+1 >= len(rows):
        return {'outcome': 'UNKNOWN', 'reason': '진입 다음 3분봉 부족'}
    delta = timedelta(minutes=3)
    if rows[i+1]['t']-rows[i]['t'] != delta:
        return {'outcome': 'UNKNOWN', 'reason': '진입 전 3분봉 공백'}
    entry = rows[i+1]['o']
    target, stop = entry*(1+cfg['tp']/100), entry*(1-cfg['sl']/100)
    last = i
    for j in range(i+1, i+n+1):
        if j >= len(rows):
            return {'outcome': 'UNKNOWN', 'reason': '청산 전 3분봉 부족'}
        if rows[j]['t']-rows[last]['t'] != delta:
            return {'outcome': 'UNKNOWN', 'reason': '청산 전 3분봉 공백'}
        last = j
        candle = rows[j]
        if candle['l'] <= stop:
            result, exit_price = 'SL', min(stop, candle['o'])
        elif candle['h'] >= target:
            result, exit_price = 'TP', target
        elif j == i+n:
            result, exit_price = 'TIME', candle['c']
        else:
            continue
        cost = (cfg['fee']+cfg['slip'])/100
        net = (exit_price*(1-cost)/(entry*(1+cost))-1)*100
        return {'outcome': result, 'entry': round(entry, 10),
                'exit': round(exit_price, 10), 'net_pct': round(net, 4),
                'minutes': (j-i)*3}
    return {'outcome': 'UNKNOWN', 'reason': '3분봉 미판정'}


def tf_v111414_evaluate_3m(api_get, market, cfg, kst):
    rows, truncated = tf_v111414_fetch_3m(api_get, market, cfg, kst)
    start = datetime.combine(cfg['day'], _time.min, tzinfo=kst)
    end = start + timedelta(days=1)
    observed = sum(start <= r['t'] < end for r in rows)
    warmup = TF_V111414_CONFIG[3]['warmup']
    n = cfg['horizon']//3
    events = []
    cooldown, valid = -1, 0
    if len(rows) >= warmup+2:
        for i in range(warmup, len(rows)-1):
            t = rows[i]['t']
            if not start <= t < end:
                continue
            if not tf_v111414_contiguous(rows, i-warmup, i, 3):
                continue
            valid += 1
            if i < cooldown or not tf_v111414_signal_3m(rows, i):
                continue
            cooldown = i+n
            outcome = tf_v111414_outcome_3m(rows, i, cfg)
            events.append({'signal_time_kst': t.strftime('%Y-%m-%d %H:%M'),
                           'entry_time_kst': (t+timedelta(minutes=3)).strftime('%Y-%m-%d %H:%M'),
                           'is_first_signal': len(events)==0,
                           **outcome})
    return {'observed': observed, 'valid_windows': valid,
            'truncated': truncated, 'signals': len(events),
            'unknown': sum(e['outcome']=='UNKNOWN' for e in events),
            'events': events}


def tf_v111414_evaluate_symbol(api_get, market, cfg, kst):
    """Existing C 5m evaluator called WITHOUT altering its logic or history."""
    old = lab_cdiag_v11145_evaluate_symbol(api_get, market, cfg, kst)
    arm5 = {
        'observed': old['observed_5m'],
        'valid_windows': old['valid_windows'],
        'truncated': old['truncated'],
        'signals': old['signals'], 'unknown': old['unknown'],
        'events': [{
            'signal_time_kst': (
                datetime.strptime(e['signal_kst'], '%Y-%m-%d %H:%M')
                - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M'),
            'entry_time_kst': e['signal_kst'],
            'is_first_signal': e['is_first_signal'],
            'outcome': e['outcome'], 'net_pct': e.get('net_pct'),
            'entry': e.get('entry'), 'exit': e.get('exit'),
            'minutes': e.get('minutes'), 'reason': e.get('reason'),
        } for e in old['events']]
    }
    arm3 = tf_v111414_evaluate_3m(api_get, market, cfg, kst)
    return {'market': market, 'tf5': arm5, 'tf3': arm3}


TF_V111414_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>V11.14.14 · C 전략 3분봉 vs 5분봉 독립 검증</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#1a2038;--card:#121a2e;--border:#35425f;--fg:#f2f5ff;--muted:#afc0db;--blue:#3867ee;--good:#4ade80;--bad:#ffb4b4}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1050px;margin:auto;padding:18px 14px 60px}h1{font-size:23px;margin:9px 0}h2{font-size:18px;margin:0 0 8px}
.panel{border:1px solid var(--border);background:var(--panel);border-radius:16px;padding:16px;margin-top:14px}
.note{border-radius:10px;background:#10182d;padding:12px;font-size:13px;line-height:1.75}
.muted,.hint{font-size:13px;color:var(--muted)}.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.field{display:flex;flex-direction:column;gap:5px;color:var(--muted);font-size:13px}
input,select,button{font:inherit}input,select{min-width:0;width:100%;padding:10px;border:1px solid var(--border);border-radius:8px;background:#10182d;color:var(--fg)}
button{padding:11px 13px;border:0;border-radius:9px;background:var(--blue);color:white;font-weight:750;cursor:pointer}button:disabled{opacity:.48}.secondary{background:#364660}
.actions{display:flex;flex-wrap:wrap;align-items:center;gap:9px;margin:14px 0 10px}progress{width:100%;height:17px}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.stat{padding:12px;border-radius:10px;background:var(--card);border:1px solid var(--border)}
.stat small{display:block;color:var(--muted);font-size:12px}.stat strong{display:block;font-size:21px}.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--border);padding:10px 8px;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}
.good{color:var(--good)}.bad{color:var(--bad)}a{color:#bcd0ff}.status{white-space:pre-wrap;color:var(--muted);font-size:13px}
@media(max-width:650px){.controls{grid-template-columns:repeat(2,minmax(0,1fr))}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><main>
<h1>🧪 V11.14.14 · 3분봉 vs 5분봉 C 전략 검증실</h1>
<p class="muted">실전 BUY와 기존 5분봉 C 검증실은 변경하지 않습니다. 두 전략을 <b>같은 날짜·현재 KRW 종목군</b>에서 독립적으로 비교하는 과거 모의 실험입니다.</p>
<section class="panel"><h2>비교 기준</h2><div class="note">
<b>기존 5분봉 C:</b> 원래 V11.14.5 C 신호·손익 판정 함수를 그대로 사용합니다.<br>
<b>실험 3분봉 C:</b> 흐름 1시간(20×3분), 빠른 평균 30분(10×3분), 눌림 평균 약 51분(17×3분), 이전 거래량 약 102분(34×3분)으로 시간 기준을 맞춥니다. 신호 강도 임계값은 같습니다.<br>
<b>진입:</b> 각 신호봉이 확정된 후 다음 해당 분봉 시가에서 모의 진입합니다. <b>청산:</b> 익절·손절·보유 시간과 수수료·슬리피지를 동일하게 적용합니다. 같은 봉에 익절·손절 동시 도달 시 손절 우선입니다.<br>
<b>주의:</b> 3분봉의 발생 신호는 5분봉 신호와 서로 다릅니다. 단순히 거래당 승률만 비교하면 안 되고 신호 개수, 서로 다른 종목 수, 첫 신호, 미판정, 평균 순손익을 함께 보아야 합니다. 거래소 3분봉과 5분봉 간 가격 경로 재현 차이도 있습니다. 결과는 실제 체결이나 실전 수익률을 보장하지 않습니다.
</div></section>
<section class="panel"><h2>검증 조건</h2><div class="controls">
<label class="field">검증 날짜 (KST)<input type="date" id="date"></label>
<label class="field">익절 목표 (%)<input type="number" id="tp" step="0.1" min="0.3" max="10" value="1.5"></label>
<label class="field">손절 기준 (%)<input type="number" id="sl" step="0.1" min="0.2" max="8" value="1.0"></label>
<label class="field">최대 보유<select id="horizon"><option value="30">30분</option><option value="60" selected>60분</option><option value="120">120분</option></select></label>
<label class="field">편도 수수료 (%)<input type="number" id="fee" step="0.01" min="0" max="0.5" value="0.04"></label>
<label class="field">편도 슬리피지 (%)<input type="number" id="slip" step="0.01" min="0" max="0.5" value="0.05"></label>
</div>
<div class="actions"><button id="run">전체 KRW 비교 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">진행 기록 초기화</button><button id="csv" class="secondary" disabled>3분봉·5분봉 비교 CSV 저장</button></div>
<progress id="progress" value="0" max="1"></progress><p id="status" class="status">검사 전</p></section>
<section class="panel"><h2>검사 현황</h2><div id="coverage" class="stats">검사 전</div></section>
<section class="panel"><h2>전체 C 신호 비교</h2><p class="hint">전체 종목 검사가 끝나야 최종 성적을 공개합니다. 미판정은 승률/평균 손익 분모에서 제외합니다.</p><div class="scroll" id="mainStats">검사 완료 후 공개</div></section>
<section class="panel"><h2>종목당 첫 C 신호만 비교</h2><p class="hint">동일 종목·동일 날짜의 반복 신호에 따른 중복 영향을 줄이기 위한 추가 지표입니다. 서로 다른 3분봉·5분봉 신호를 1:1 매칭한 것은 아닙니다.</p><div class="scroll" id="firstStats">검사 완료 후 공개</div></section>
<section class="panel"><h2>코인별 결과</h2><div id="byMarket" class="scroll">검사 완료 후 공개</div></section>
<section class="panel"><h2>기존 연구실</h2><p><a href="/backtest-winrate-lossdiag">V11.14.13 손실 원인 분석</a> · <a href="/backtest-winrate-fib-btc">피보나치/BTC</a> · <a href="/backtest-winrate-vwap-btc">VWAP/BTC</a> · <a href="/backtest-winrate-c-validated">기존 C 검증</a> · <a href="/">V2 메인</a></p><div class="note">완료된 과거 한 날짜의 현재 상장된 KRW 종목만 검사합니다. 현재 상장 목록을 과거 시점의 상장 종목 목록으로 간주할 수 없으며, 거래정지·상장폐지·과거 거래 비용을 완벽히 재현하지 않습니다.</div></section>
</main><script>
const $=id=>document.getElementById(id);
const KEY='bithumb_v111414_3m5m_checkpoint_1', API='/api/winrate14tf';
let state=null, running=false, stopRequested=false, hardFailure=false;
function dateKST(d){const p=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(d);const x=k=>p.find(a=>a.type===k).value;return `${x('year')}-${x('month')}-${x('day')}`;}
const yesterday=()=>dateKST(new Date(Date.now()-86400000));
$('date').max=yesterday();$('date').value=('2026-11-10'<=yesterday()&&'2026-11-10'>=dateKST(new Date(Date.now()-59*86400000)))?'2026-11-10':yesterday();
function config(){const c={date:$('date').value,tp:+$('tp').value,sl:+$('sl').value,horizon:+$('horizon').value,fee:+$('fee').value,slip:+$('slip').value};if(!/^\d{4}-\d{2}-\d{2}$/.test(c.date)||c.date>yesterday()||!Object.values(c).slice(1).every(Number.isFinite)||c.tp<.3||c.tp>10||c.sl<.2||c.sl>8||![30,60,120].includes(c.horizon)||c.fee<0||c.fee>.5||c.slip<0||c.slip>.5)throw Error('검증 날짜·손익·비용 설정을 확인하세요');return c;}
const sig=c=>JSON.stringify([c.date,c.tp,c.sl,c.horizon,c.fee,c.slip]);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=n=>Number.isFinite(n)?n.toFixed(3)+'%':'—';
const card=(title,val)=>`<div class="stat"><small>${esc(title)}</small><strong>${esc(val)}</strong></div>`;
function load(){try{const s=JSON.parse(localStorage.getItem(KEY)||'null');if(!s||s.version!==1||!s.config||!Array.isArray(s.markets)||!s.markets.length||!s.results||typeof s.results!=='object'||s.markets.some(m=>!/^KRW-[A-Z0-9]+$/.test(m))||new Set(s.markets).size!==s.markets.length)return null;return s;}catch{return null;}}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function setConfig(c){for(const name of ['date','tp','sl','horizon','fee','slip'])$(name).value=c[name];}
function symbols(){return state?state.markets.filter(m=>Object.prototype.hasOwnProperty.call(state.results,m)).map(m=>state.results[m]):[];}
function finished(){return !!state&&symbols().length===state.markets.length&&state.failed===0;}
function sum(group,tf,firstOnly){let signals=0,unknown=0,n=0,wins=0,net=0,tp=0,sl=0,timed=0,markets=0,valid=0,observed=0;for(const s of group){const a=s[tf];valid+=a.valid_windows;observed+=a.observed;let events=a.events;if(firstOnly)events=events.filter(e=>e.is_first_signal);signals+=firstOnly?events.length:a.signals;unknown+=firstOnly?events.filter(e=>e.outcome==='UNKNOWN').length:a.unknown;if(events.length)markets++;for(const e of events){if(e.outcome==='UNKNOWN')continue;const v=Number(e.net_pct);if(!Number.isFinite(v))continue;n++;net+=v;if(v>0)wins++;if(e.outcome==='TP')tp++;if(e.outcome==='SL')sl++;if(e.outcome==='TIME')timed++;}}return {signals,unknown,n,wins,tp,sl,timed,markets,valid,observed,avg:n?net/n:NaN,win:n?100*wins/n:NaN};}
function tableStats(group,firstOnly){const a=sum(group,'tf5',firstOnly),b=sum(group,'tf3',firstOnly);const rows=[['신호 건수',a.signals,b.signals],['신호 발생 종목',a.markets,b.markets],['판정 완료 거래',a.n,b.n],['미판정 신호',a.unknown,b.unknown],['수익 거래',a.wins,b.wins],['수익 거래 비율',pct(a.win),pct(b.win)],['거래당 평균 순손익',pct(a.avg),pct(b.avg)],['익절/손절/시간종료',`${a.tp}/${a.sl}/${a.timed}`,`${b.tp}/${b.sl}/${b.timed}`]];return `<table><thead><tr><th>비교 항목</th><th>기존 5분봉</th><th>실험 3분봉</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td><td>${esc(r[2])}</td></tr>`).join('')}</tbody></table>`;}
function paint(){const all=symbols(),n=all.length,total=state?.markets.length||0,complete=finished();$('progress').max=total||1;$('progress').value=n;const v5=sum(all,'tf5',false),v3=sum(all,'tf3',false);
$('coverage').innerHTML=card('조회 완료 종목',`${n}/${total}`)+card('5분봉 C 신호',v5.signals+'건')+card('3분봉 C 신호',v3.signals+'건')+card('5분봉·3분봉 미판정',`${v5.unknown} / ${v3.unknown}`)+card('5분봉 유효 평가 구간',v5.valid.toLocaleString())+card('3분봉 유효 평가 구간',v3.valid.toLocaleString());
$('csv').disabled=!complete;if(!complete){$('mainStats').textContent='전체 검사 완료 후 최종 결과를 공개합니다.';$('firstStats').textContent='전체 검사 완료 후 공개';$('byMarket').textContent='전체 검사 완료 후 공개';}else{$('mainStats').innerHTML=tableStats(all,false);$('firstStats').innerHTML=tableStats(all,true);const rows=all.filter(s=>s.tf5.signals||s.tf3.signals).sort((a,b)=>a.market.localeCompare(b.market));$('byMarket').innerHTML=rows.length?`<table><thead><tr><th>종목</th><th>5m 신호</th><th>3m 신호</th><th>5m 판정</th><th>3m 판정</th><th>5m 평균</th><th>3m 평균</th></tr></thead><tbody>${rows.map(s=>{const a=sum([s],'tf5',false),b=sum([s],'tf3',false);return `<tr><td>${esc(s.market)}</td><td>${a.signals}</td><td>${b.signals}</td><td>${a.n}</td><td>${b.n}</td><td>${pct(a.avg)}</td><td>${pct(b.avg)}</td></tr>`;}).join('')}</tbody></table>`:'C 신호 없음';}
$('status').textContent=(complete?'✅ 전체 KRW 종목 3분봉·5분봉 비교 완료':'⏳ 전체 시장 검사 미완료')+` · ${n}/${total}종목`+(state?.failed?' · ⚠️ 실패 배치 '+state.failed+'개':'')+(complete?'\n거래당 평균 순손익은 실제 계좌 수익률이 아닙니다. 실전 적용 전 독립 검증이 필요합니다.':'\n최종 승률과 손익은 모든 종목 검사 완료 뒤에만 표시합니다.');}
async function call(url,opts){const ctl=new AbortController(),timer=setTimeout(()=>ctl.abort(),115000);try{const resp=await fetch(url,{...opts,cache:'no-store',signal:ctl.signal}),data=await resp.json();if(!resp.ok||!data.ok)throw Error(data.error||'HTTP '+resp.status);return data;}finally{clearTimeout(timer);}}
async function init(c){const v=await call(API+'/universe');if(!Array.isArray(v.items)||!v.items.length)throw Error('현재 KRW 종목 목록 없음');state={version:1,config:c,markets:v.items,results:{},failed:0};save();}
async function run(){if(running)return;let c;try{c=config();}catch(e){$('status').textContent='⚠️ '+e.message;return;}if(state&&sig(state.config)!==sig(c)){if(!confirm('날짜나 설정이 바뀌었어. 이전 진행 기록을 지우고 새 검사를 시작할까?'))return;state=null;localStorage.removeItem(KEY);}running=true;stopRequested=false;hardFailure=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;try{if(!state)await init(c);state.failed=0;save();paint();const todo=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m)),jobs=todo.map(m=>[m]);let pos=0,failed=0;async function worker(){while(!stopRequested&&!hardFailure){const batch=jobs[pos++];if(!batch)return;let response=null;for(let attempt=0;attempt<3&&!stopRequested;attempt++){try{const x=await call(API+'/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:batch})});if(x.errors||x.scanned!==batch.length||!Array.isArray(x.results)||x.results.length!==batch.length||x.results.some(r=>!batch.includes(r.market)||!r.tf3||!r.tf5||!Array.isArray(r.tf3.events)||!Array.isArray(r.tf5.events)||r.tf3.events.length!==r.tf3.signals||r.tf5.events.length!==r.tf5.signals))throw Error('불완전한 종목 결과');response=x;break;}catch(e){if(attempt===2){failed++;$('status').textContent='⚠️ '+batch.join(', ')+' 검사 실패: '+String(e.message||e);}else await new Promise(resolve=>setTimeout(resolve,1000*(attempt+1)));}}if(response){for(const r of response.results)state.results[r.market]=r;try{save();}catch(e){hardFailure=true;$('status').textContent='⚠️ 브라우저 저장 공간 부족';break;}paint();}if(failed>=4){hardFailure=true;break;}}}await Promise.all([worker(),worker()]);state.failed=failed;try{save();}catch{}paint();if(stopRequested)$('status').textContent+='\n일시 중지했어. 같은 날짜/설정으로 누르면 이어서 검사해.';if(failed)$('status').textContent+='\n누락된 종목은 다시 실행할 때 재검사해.';}catch(e){$('status').textContent='⚠️ '+String(e.message||e);}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}}
$('run').onclick=run;$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;};$('reset').onclick=()=>{if(running)return;if(!confirm('이 3분봉·5분봉 검증의 진행 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(s){s=String(s??'');return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!finished())return;const cols=['version','date','tp_pct','sl_pct','horizon_minutes','fee_pct_side','slip_pct_side','market','timeframe_minutes','observed_day_bars','valid_windows','c_signals','c_unknown','signal_time_kst','entry_time_kst','is_first_signal','outcome','net_pct','entry','exit','minutes','reason'];const lines=[cols.join(',')];for(const r of symbols()){for(const [tf,unit] of [['tf5',5],['tf3',3]]){const arm=r[tf];const prefix={version:'V11.14.14',date:state.config.date,tp_pct:state.config.tp,sl_pct:state.config.sl,horizon_minutes:state.config.horizon,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip,market:r.market,timeframe_minutes:unit,observed_day_bars:arm.observed,valid_windows:arm.valid_windows,c_signals:arm.signals,c_unknown:arm.unknown};if(!arm.events.length){lines.push(cols.map(k=>csvCell(prefix[k])).join(','));continue;}for(const ev of arm.events)lines.push(cols.map(k=>csvCell(({...prefix,...ev})[k])).join(','));}}const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`bithumb_C_3m_vs_5m_${state.config.date}_v111414.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);};
state=load();if(state)setConfig(state.config);paint();
</script></body></html>
'''


def register_tf_v111414_lab(app, deps):
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache = {'names': None, 'time': 0}
    mutex = Lock()

    def listed():
        with mutex:
            if cache['names'] is None or time.time() - cache['time'] > 600:
                names = sorted({m for m in get_markets()
                                if isinstance(m, str) and MARKET_RE.fullmatch(m)})
                if not names:
                    raise ValueError('현재 KRW 종목 목록이 비었습니다')
                cache['names'], cache['time'] = names, time.time()
            return cache['names']

    @app.get('/backtest-winrate-3m5m')
    def page_tf_v111414():
        return render_template_string(TF_V111414_PAGE)

    @app.get('/api/winrate14tf/universe')
    def universe_tf_v111414():
        try:
            names = listed()
            return jsonify({'ok': True, 'items': names, 'count': len(names),
                            'rule_version': 'V11.14.14',
                            'universe_type': 'currently_listed_KRW'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400

    @app.post('/api/winrate14tf/evaluate')
    def evaluate_tf_v111414():
        try:
            body = request.get_json(silent=True)
            cfg = lab_valid_request(body, kst)
            markets = body.get('markets') if isinstance(body, dict) else None
            if (not isinstance(markets, list) or not 1 <= len(markets) <= 2
                    or len(set(markets)) != len(markets)
                    or any(not isinstance(m, str) or not MARKET_RE.fullmatch(m)
                           for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 검사합니다')
            names = set(listed())
            if any(m not in names for m in markets):
                raise ValueError('현재 KRW 종목에 없는 코인입니다')
            results, errors = [], []
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {pool.submit(tf_v111414_evaluate_symbol,
                                       api_get, market, cfg, kst): market
                           for market in markets}
                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        errors.append({'market': futures[future],
                                       'error': str(exc)[:120]})
            return jsonify({'ok': not errors, 'scanned': len(markets),
                            'results': results, 'errors': len(errors),
                            'error_markets': errors, 'rule_version': 'V11.14.14'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400


_v111413_register_daytrade_review = register_daytrade_review


def register_daytrade_review(app, deps):
    _v111413_register_daytrade_review(app, deps)
    register_tf_v111414_lab(app, deps)

# ---------------------------------------------------------------------------
# V11.14.15 — independent 5m / 10m / 15m C timeframe comparison lab.
# Append-only: V2 live and previous research endpoints/functions unchanged.
# V11.14.5 C is the unchanged baseline. 10m and 15m signal conditions are
# scaled in wall-clock time and settled with observed 5m candles, NOT invented.
# Same-side fees / slippage and earliest-bar conservative barrier rules apply.
# ---------------------------------------------------------------------------

TF_V111415_SCALES = {
    10: {'hour': 6, 'fast': 3, 'ma': 5, 'volume': 10, 'warmup': 14},
    15: {'hour': 4, 'fast': 2, 'ma': 3, 'volume': 7, 'warmup': 10},
}


def tf_v111415_fetch(api_get, market, cfg, kst, unit):
    """KST-exclusive pagination; observed candles only (up to 4×200)."""
    if unit not in TF_V111415_SCALES:
        raise ValueError('지원하지 않는 비교 분봉입니다')
    day_start = datetime.combine(cfg['day'], _time.min, tzinfo=kst)
    begin = day_start - timedelta(hours=4)
    end = day_start + timedelta(days=1, hours=2, minutes=unit)
    cursor, bars, truncated = end, {}, True
    for _ in range(4):
        raw = api_get(f'/v1/candles/minutes/{unit}', {
            'market': market, 'count': 200,
            'to': cursor.strftime('%Y-%m-%d %H:%M:%S'),
        })
        if not isinstance(raw, list):
            raise ValueError(f'{unit}분봉 API 응답 형식 오류')
        oldest = None
        for bar in raw:
            if not isinstance(bar, dict):
                continue
            try:
                t = local_time(bar['candle_date_time_kst'], kst)
                op, hi, lo, close = (float(bar[k]) for k in (
                    'opening_price', 'high_price', 'low_price', 'trade_price'))
                volume = float(bar['candle_acc_trade_volume'])
                if not (all(isfinite(x) and x > 0 for x in (op, hi, lo, close))
                        and isfinite(volume) and volume >= 0
                        and lo <= min(op, close) and hi >= max(op, close) and lo <= hi):
                    continue
                if t.minute % unit or t.second or t.microsecond:
                    continue
                oldest = t if oldest is None else min(oldest, t)
                if begin <= t < end:
                    bars[t] = {'t': t, 'o': op, 'h': hi,
                               'l': lo, 'c': close, 'v': volume}
            except (KeyError, ValueError, TypeError, OverflowError):
                continue
        if oldest is None or oldest <= begin or len(raw) < 200:
            truncated = False
            break
        if oldest >= cursor:
            raise ValueError(f'{unit}분봉 과거 조회 페이지가 전진하지 않습니다')
        cursor = oldest
    return [bars[t] for t in sorted(bars)], truncated


def tf_v111415_contiguous(rows, first, last, unit):
    if not (0 <= first <= last < len(rows)):
        return False
    delta = timedelta(minutes=unit)
    return all(rows[j]['t']-rows[j-1]['t']==delta
               for j in range(first+1, last+1))


def tf_v111415_signal(rows, i, unit):
    """Only completed signal candle and bars PRIOR to it are ever referenced."""
    p = TF_V111415_SCALES[unit]
    if i < p['warmup']:
        return False
    c = rows[i]
    volume = sum(r['v'] for r in rows[i-p['volume']:i])/p['volume']
    if volume <= 0:
        return False
    span = max(1e-12, c['h']-c['l'])
    bull = c['c']>c['o'] and (c['c']-c['o'])/span >= 0.30
    h, fast_n = p['hour'],p['fast']
    old_hour = sum(r['c'] for r in rows[i-2*h:i-h])/h
    recent_hour = sum(r['c'] for r in rows[i-h:i])/h
    fast = sum(r['c'] for r in rows[i-fast_n:i])/fast_n
    fast_old = sum(r['c'] for r in rows[i-fast_n-1:i-1])/fast_n
    ma = sum(r['c'] for r in rows[i-p['ma']:i])/p['ma']
    return bool(bull and c['v']/volume>=0.90
                and recent_hour>=old_hour*1.003
                and fast>=fast_old and c['l']<=ma*1.005
                and c['c']>=ma*1.001 and c['c']>=rows[i-1]['c'])


def tf_v111415_settle_5m(complete_signal_t, unit, rows_5_by_time, cfg):
    """Enter at next higher-timeframe candle start; settle using actual 5m.

    The exit checks the next 12 observed 5m candles for a 60m hold, never
    synthesizes absent candles, and stops at the earliest settled TP or SL.
    A missing bar BEFORE an exit => UNKNOWN; missing AFTER exit is irrelevant.
    """
    entry_t = complete_signal_t + timedelta(minutes=unit)
    n = cfg['horizon']//5
    entry_bar = rows_5_by_time.get(entry_t)
    if entry_bar is None:
        return {'outcome': 'UNKNOWN', 'reason': '진입 시점 5분봉 없음'}
    entry = entry_bar['o']
    target, stop = entry*(1+cfg['tp']/100), entry*(1-cfg['sl']/100)
    for j in range(n):
        t = entry_t + timedelta(minutes=5*j)
        bar = rows_5_by_time.get(t)
        if bar is None:
            return {'outcome': 'UNKNOWN', 'reason': '청산 전 5분봉 공백/부족'}
        if bar['l'] <= stop:
            outcome, exit_price = 'SL', min(stop, bar['o'])
        elif bar['h'] >= target:
            outcome, exit_price = 'TP', target
        elif j==n-1:
            outcome, exit_price = 'TIME', bar['c']
        else:
            continue
        cost = (cfg['fee']+cfg['slip'])/100
        net = (exit_price*(1-cost)/(entry*(1+cost))-1)*100
        return {'outcome': outcome, 'entry': round(entry,10),
                'exit': round(exit_price,10), 'net_pct': round(net,4),
                'minutes': (j+1)*5}
    return {'outcome': 'UNKNOWN', 'reason': '청산 미판정'}


def tf_v111415_evaluate_arm(api_get, market, cfg, kst, unit, rows5):
    rows, truncated = tf_v111415_fetch(api_get, market, cfg, kst, unit)
    start = datetime.combine(cfg['day'], _time.min, tzinfo=kst)
    end = start + timedelta(days=1)
    observed = sum(start <= r['t'] < end for r in rows)
    scale = TF_V111415_SCALES[unit]
    warmup, cooldown_bars = scale['warmup'], cfg['horizon']//unit
    events, cooldown, valid = [], -1, 0
    day_rows5 = {r['t']:r for r in rows5}
    for i in range(warmup, max(warmup,len(rows)-1)):
        t = rows[i]['t']
        if not start <= t < end:
            continue
        if not tf_v111415_contiguous(rows, i-2*scale['hour'], i, unit):
            continue
        valid += 1
        if i < cooldown or not tf_v111415_signal(rows, i, unit):
            continue
        cooldown = i + cooldown_bars
        next_t = t + timedelta(minutes=unit)
        # Need the REAL next longer bar; do not assume trades occurred.
        if i+1 >= len(rows) or rows[i+1]['t'] != next_t:
            outcome = {'outcome':'UNKNOWN','reason':'다음 상위분봉 미관측'}
        else:
            outcome = tf_v111415_settle_5m(t, unit, day_rows5, cfg)
        events.append({
            'signal_time_kst':t.strftime('%Y-%m-%d %H:%M'),
            'entry_time_kst':next_t.strftime('%Y-%m-%d %H:%M'),
            'is_first_signal':len(events)==0,
            **outcome,
        })
    return {'observed':observed, 'valid_windows':valid,
            'truncated':truncated, 'signals':len(events),
            'unknown':sum(e['outcome']=='UNKNOWN' for e in events),
            'events':events}


def tf_v111415_evaluate_symbol(api_get, market, cfg, kst):
    """Preserve original 5m C exactly and capture same raw 5m calls for exits.

    This prevents a redundant fifth-minute history request and guarantees the
    same baseline source, data boundaries, cooldown and PnL as V11.14.5.
    """
    captured = {}
    def capture5(path, params):
        raw = api_get(path, params)
        if path == '/v1/candles/minutes/5' and isinstance(raw, list):
            for b in raw:
                if not isinstance(b, dict):
                    continue
                try:
                    t = local_time(b['candle_date_time_kst'], kst)
                    op, hi, lo, cl = (float(b[k]) for k in (
                        'opening_price','high_price','low_price','trade_price'))
                    vol = float(b['candle_acc_trade_volume'])
                    if not (all(isfinite(v) and v>0 for v in (op,hi,lo,cl))
                            and isfinite(vol) and vol>=0
                            and lo<=min(op,cl) and hi>=max(op,cl) and lo<=hi):
                        continue
                    captured[t]={'t':t,'o':op,'h':hi,'l':lo,'c':cl,'v':vol}
                except (KeyError, ValueError, TypeError, OverflowError):
                    continue
        return raw
    base = lab_cdiag_v11145_evaluate_symbol(capture5, market, cfg, kst)
    arm5 = {
        'observed':base['observed_5m'],
        'valid_windows':base['valid_windows'],
        'truncated':base['truncated'],
        'signals':base['signals'],
        'unknown':base['unknown'],
        'events':[{
            'signal_time_kst':(
                datetime.strptime(e['signal_kst'],'%Y-%m-%d %H:%M')
                -timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M'),
            'entry_time_kst':e['signal_kst'],
            'is_first_signal':e['is_first_signal'],
            'outcome':e['outcome'],
            'net_pct':e.get('net_pct'), 'entry':e.get('entry'),
            'exit':e.get('exit'),'minutes':e.get('minutes'),
            'reason':e.get('reason'),
        } for e in base['events']]
    }
    rows5 = [captured[t] for t in sorted(captured)]
    arm10 = tf_v111415_evaluate_arm(api_get,market,cfg,kst,10,rows5)
    arm15 = tf_v111415_evaluate_arm(api_get,market,cfg,kst,15,rows5)
    return {'market':market, 'tf5':arm5, 'tf10':arm10, 'tf15':arm15}

TF_V111415_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.15 | 5·10·15분봉 C 전략 검증실</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#19213b;--card:#111a30;--line:#334160;--fg:#f2f5ff;--muted:#b4c2dc;--accent:#416ef4;--good:#62e9a0;--warn:#ffc783}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,-apple-system,"Noto Sans KR",sans-serif}main{max-width:1080px;margin:auto;padding:20px 14px 70px}
h1{font-size:24px;line-height:1.3;margin:8px 0}h2{font-size:18px;margin:0 0 10px}p{margin:8px 0}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px;margin:14px 0}.note{font-size:13px;line-height:1.75;background:#0f1930;padding:12px;border-radius:10px}.muted,.hint{font-size:13px;color:var(--muted)}
.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.field{display:flex;flex-direction:column;gap:5px;color:var(--muted);font-size:13px}
input,select,button{font:inherit}input,select{padding:10px;background:#10182c;color:var(--fg);border:1px solid var(--line);border-radius:9px;min-width:0;width:100%}
button{background:var(--accent);color:white;border:0;border-radius:9px;padding:11px 13px;font-weight:750;cursor:pointer}button.secondary{background:#364868}button:disabled{opacity:.45}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.stat{background:var(--card);padding:11px;border:1px solid var(--line);border-radius:10px}
.stat small{display:block;font-size:12px;color:var(--muted)}.stat strong{font-size:20px}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:10px 8px;border-bottom:1px solid var(--line);white-space:nowrap;text-align:right}th:first-child,td:first-child{text-align:left}th{color:var(--muted)}
.good{color:var(--good)}a{color:#a9c5ff}.status{white-space:pre-wrap;font-size:13px;color:var(--muted)}progress{width:100%;height:17px}
@media(max-width:680px){.controls{grid-template-columns:repeat(2,minmax(0,1fr))}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><main>
<h1>🧪 V11.14.15 · 5분봉 vs 10분봉 vs 15분봉</h1>
<p class="muted">빗썸 KRW 시장 · C 신호 과거 비교 연구. 실전 BUY와 기존 검증실은 변경하지 않습니다.</p>
<section class="panel"><h2>검증 설계</h2><div class="note">
<b>5분봉 C:</b> V11.14.5 기존 신호·손익 함수를 그대로 사용합니다.<br>
<b>10분봉 C:</b> 상승 흐름 1시간×2, 거래량 평균 100분, 빠른 평균 30분, 눌림 평균 50분으로 시간 정렬합니다.<br>
<b>15분봉 C:</b> 상승 흐름 1시간×2, 거래량 평균 약 105분, 빠른 평균 30분, 눌림 평균 약 45분으로 조정합니다.<br>
<b>진입:</b> 신호봉 확정 후 다음 해당 분봉 시작 시점의 실제 관측된 <b>5분봉 시가</b>로 모의 진입합니다. 다음 상위분봉이 존재해야 합니다.<br>
<b>청산:</b> 10분봉·15분봉 모두 실제 <b>5분봉 OHLC</b>로 동일한 익절/손절/보유 시간 판정. 같은 5분봉에서 익절·손절 동시 도달 시 손절 우선, 중간 봉 누락 시 미판정.<br>
<b>주의:</b> 5·10·15분봉은 서로 다른 시점에서 신호가 발생합니다. 종목·날짜는 같지만 거래를 1:1 짝지은 결과는 아닙니다. 분봉 기간별 지표 길이는 시간상 근사치입니다. 과거 모의 결과는 실제 체결 성과를 보장하지 않습니다.
</div></section>
<section class="panel"><h2>검증 설정</h2><div class="controls">
<label class="field">검증 날짜 (KST)<input id="date" type="date"></label>
<label class="field">익절 목표 (%)<input id="tp" type="number" value="1.5" min="0.3" max="10" step="0.1"></label>
<label class="field">손절 기준 (%)<input id="sl" type="number" value="1.0" min="0.2" max="8" step="0.1"></label>
<label class="field">최대 보유<select id="horizon"><option value="30">30분</option><option value="60" selected>60분</option><option value="120">120분</option></select></label>
<label class="field">편도 수수료 (%)<input id="fee" type="number" value="0.04" min="0" max="0.5" step="0.01"></label>
<label class="field">편도 슬리피지 (%)<input id="slip" type="number" value="0.05" min="0" max="0.5" step="0.01"></label>
</div>
<div class="actions"><button id="run">전체 KRW 비교 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">기록 초기화</button><button id="csv" class="secondary" disabled>5·10·15분봉 비교 CSV 저장</button></div>
<progress id="progress" value="0" max="1"></progress><div id="status" class="status">검사 전</div></section>
<section class="panel"><h2>분석 가능 여부</h2><div id="coverage" class="stats">검사 전</div><p class="hint">과거 봉이 연속되어 분석 가능한 종목 수와 미판정 건수를 각각 표시합니다. 계산 불가 거래는 승률을 올리는 성공 사례로 간주하지 않습니다.</p></section>
<section class="panel"><h2>전체 C 신호 비교</h2><p class="hint">전체 시장 검사 완료 시 공개 · 수익 거래 비율은 순손익이 0% 초과인 거래 기준</p><div class="scroll" id="summary">전체 검사 후 표시</div></section>
<section class="panel"><h2>3개 분봉 모두 분석 가능한 종목만 비교</h2><p class="hint">신호 발생 여부와 무관하게 세 분봉에서 각각 유효 평가 구간이 있는 동일 종목들입니다. 신호 시각은 서로 다릅니다.</p><div class="scroll" id="common">전체 검사 후 표시</div></section>
<section class="panel"><h2>종목별 첫 C 신호만 비교</h2><p class="hint">같은 날 같은 코인의 반복 진입 영향을 일부 줄인 비교입니다.</p><div class="scroll" id="first">전체 검사 후 표시</div></section>
<section class="panel"><h2>코인별 결과</h2><div class="scroll" id="markets">전체 검사 후 표시</div></section>
<section class="panel"><h2>기존 검증실</h2><p><a href="/backtest-winrate-3m5m">3분봉 vs 5분봉</a> · <a href="/backtest-winrate-lossdiag">손실 원인 분석</a> · <a href="/backtest-winrate-fib-btc">피보나치/BTC</a> · <a href="/backtest-winrate-c-validated">원래 C 검증</a> · <a href="/">V2 메인</a></p><p class="muted">현재 상장 종목을 과거 일자의 종목군으로 조회합니다. 상장 폐지·과거 거래비용·체결 충격 등을 완전히 재현하지 못합니다. 실전 주문 기능이 아닙니다.</p></section>
</main><script>
const $=id=>document.getElementById(id), KEY='bithumb_v111415_5m10m15m_checkpoint_1', API='/api/winrate14tf1015';
let state=null,running=false,stop=false,storeFailure=false;
const timeframes=[['tf5',5],['tf10',10],['tf15',15]];
function dateKST(d){const p=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(d);const x=k=>p.find(a=>a.type===k)?.value||'';return `${x('year')}-${x('month')}-${x('day')}`;}
const yesterday=()=>dateKST(new Date(Date.now()-86400000));$('date').max=yesterday();$('date').value=('2026-11-10'<=yesterday()&&'2026-11-10'>=dateKST(new Date(Date.now()-59*86400000)))?'2026-11-10':yesterday();
function config(){const c={date:$('date').value,tp:+$('tp').value,sl:+$('sl').value,horizon:+$('horizon').value,fee:+$('fee').value,slip:+$('slip').value};if(!/^\d{4}-\d{2}-\d{2}$/.test(c.date)||c.date>yesterday()||!Object.values(c).slice(1).every(Number.isFinite)||c.tp<.3||c.tp>10||c.sl<.2||c.sl>8||![30,60,120].includes(c.horizon)||c.fee<0||c.fee>.5||c.slip<0||c.slip>.5)throw Error('날짜/익절/손절/비용 입력을 확인하세요');return c;}
const sig=c=>JSON.stringify([c.date,c.tp,c.sl,c.horizon,c.fee,c.slip]);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=x=>Number.isFinite(x)?x.toFixed(3)+'%':'—';const box=(t,v)=>`<div class="stat"><small>${esc(t)}</small><strong>${esc(v)}</strong></div>`;
function load(){try{const s=JSON.parse(localStorage.getItem(KEY)||'null');if(!s||s.version!==1||!s.config||!Array.isArray(s.markets)||!s.markets.length||new Set(s.markets).size!==s.markets.length||!s.markets.every(m=>/^KRW-[A-Z0-9]+$/.test(m))||!s.results||typeof s.results!=='object')return null;return s;}catch{return null;}}
const save=()=>localStorage.setItem(KEY,JSON.stringify(state));
function records(){return state?state.markets.filter(m=>Object.prototype.hasOwnProperty.call(state.results,m)).map(m=>state.results[m]):[];}
function finished(){return !!state&&state.failed===0&&records().length===state.markets.length;}
function stats(rs,key,firstOnly=false){let signals=0,unknown=0,n=0,win=0,tp=0,sl=0,held=0,net=0,markets=0,valid=0,observed=0;for(const r of rs){const a=r[key];valid+=a.valid_windows;observed+=a.observed;let es=firstOnly?a.events.filter(e=>e.is_first_signal):a.events;signals+=firstOnly?es.length:a.signals;unknown+=firstOnly?es.filter(e=>e.outcome==='UNKNOWN').length:a.unknown;if(es.length)markets++;for(const e of es){if(e.outcome==='UNKNOWN'||!Number.isFinite(Number(e.net_pct)))continue;const p=Number(e.net_pct);n++;net+=p;if(p>0)win++;if(e.outcome==='TP')tp++;if(e.outcome==='SL')sl++;if(e.outcome==='TIME')held++;}}return {signals,unknown,n,win,tp,sl,held,markets,valid,observed,winRate:n?100*win/n:NaN,avg:n?net/n:NaN};}
function comparison(rs,firstOnly=false){const a=timeframes.map(([key])=>stats(rs,key,firstOnly));const rows=[['신호 수',...a.map(v=>v.signals)],['신호 발생 종목',...a.map(v=>v.markets)],['손익 판정',...a.map(v=>v.n)],['미판정 신호',...a.map(v=>v.unknown)],['수익 거래',...a.map(v=>v.win)],['수익 거래 비율',...a.map(v=>pct(v.winRate))],['평균 순손익',...a.map(v=>pct(v.avg))],['익절/손절/시간종료',...a.map(v=>`${v.tp}/${v.sl}/${v.held}`)]];return '<table><thead><tr><th>항목</th><th>기존 5분</th><th>실험 10분</th><th>실험 15분</th></tr></thead><tbody>'+rows.map(v=>'<tr>'+v.map(x=>`<td>${esc(x)}</td>`).join('')+'</tr>').join('')+'</tbody></table>';}
function paint(){const rs=records(),n=rs.length,total=state?.markets.length||0,full=finished();$('progress').max=total||1;$('progress').value=n;const a=timeframes.map(([k])=>stats(rs,k));$('coverage').innerHTML=box('진행',`${n}/${total}`)+timeframes.map(([k,u],i)=>box(`${u}분봉 유효 종목`,rs.filter(r=>r[k].valid_windows>0).length+'개')).join('')+timeframes.map(([k,u],i)=>box(`${u}분봉 신호 / 미판정`,`${a[i].signals} / ${a[i].unknown}`)).join('');$('csv').disabled=!full;if(!full){for(const id of ['summary','common','first','markets'])$(id).textContent='전체 검사 완료 후 공개';}else{$('summary').innerHTML=comparison(rs);const common=rs.filter(r=>timeframes.every(([key])=>r[key].valid_windows>0));$('common').innerHTML=`<p class="hint">공통 유효 종목 ${common.length}개 / ${rs.length}개</p>`+comparison(common);$('first').innerHTML=comparison(rs,true);const coins=rs.filter(r=>timeframes.some(([k])=>r[k].signals>0));coins.sort((x,y)=>x.market.localeCompare(y.market));$('markets').innerHTML=coins.length?'<table><thead><tr><th>종목</th><th>5분 신호</th><th>10분 신호</th><th>15분 신호</th><th>5분 평균</th><th>10분 평균</th><th>15분 평균</th></tr></thead><tbody>'+coins.map(r=>{const s=timeframes.map(([k])=>stats([r],k));return `<tr><td>${esc(r.market)}</td>${s.map(v=>`<td>${v.signals}</td>`).join('')}${s.map(v=>`<td>${pct(v.avg)}</td>`).join('')}</tr>`;}).join('')+'</tbody></table>':'C 신호 없음';}$('status').textContent=(full?'✅ 5·10·15분봉 전체 KRW 비교 완료':'⏳ 전체 시장 검사 진행 중')+` · ${n}/${total}종목`+(state?.failed?' · 실패 검사 있음: 이어하기로 재검사':'')+(full?'\n주의: 과거 모의 성적은 실전 수익률이 아닙니다.':'\n검사 미완료 시 최종 승률·손익을 공개하지 않습니다.');}
async function request(url,opts){const ctrl=new AbortController(),timeout=setTimeout(()=>ctrl.abort(),115000);try{const resp=await fetch(url,{...opts,cache:'no-store',signal:ctrl.signal});const data=await resp.json();if(!resp.ok||!data.ok)throw Error(data.error||'HTTP '+resp.status);return data;}finally{clearTimeout(timeout);}}
async function init(c){const x=await request(API+'/universe');if(!Array.isArray(x.items)||!x.items.length)throw Error('현재 KRW 종목 목록이 없습니다');state={version:1,config:c,markets:x.items,results:{},failed:0};save();}
function validResult(resp,market){if(resp.errors||resp.scanned!==1||!Array.isArray(resp.results)||resp.results.length!==1)return false;const r=resp.results[0];return r.market===market&&timeframes.every(([k])=>r[k]&&Array.isArray(r[k].events)&&r[k].signals===r[k].events.length&&r[k].unknown===r[k].events.filter(e=>e.outcome==='UNKNOWN').length);}
async function run(){if(running)return;let c;try{c=config();}catch(e){$('status').textContent='⚠️ '+e.message;return;}if(state&&sig(c)!==sig(state.config)){if(!confirm('날짜/설정이 바뀌었어. 이 검증의 이전 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);}running=true;stop=false;storeFailure=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;try{if(!state)await init(c);state.failed=0;save();paint();const pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m));let index=0,fail=0;async function worker(){while(!stop&&!storeFailure){const market=pending[index++];if(!market)return;let response=null;for(let attempt=0;attempt<3&&!stop;attempt++){try{const x=await request(API+'/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:[market]})});if(!validResult(x,market))throw Error('응답 검사 실패');response=x;break;}catch(e){if(attempt===2){fail++;$('status').textContent='⚠️ '+market+' 실패: '+String(e.message||e);}else await new Promise(resolve=>setTimeout(resolve,(attempt+1)*1200));}}if(response){state.results[market]=response.results[0];try{save();}catch(e){storeFailure=true;$('status').textContent='⚠️ 저장 공간 부족 · 검사 중지';break;}paint();}if(fail>=4){stop=true;break;}}}await Promise.all([worker(),worker()]);state.failed=fail;try{save();}catch{}paint();if(stop)$('status').textContent+='\n중지했어. 같은 설정에서 다시 누르면 이어서 검사할 수 있어.';if(fail)$('status').textContent+='\n실패한 종목은 다음 실행에서 다시 검사돼.';}catch(e){$('status').textContent='⚠️ '+String(e.message||e);}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}}
$('run').onclick=run;$('pause').onclick=()=>{stop=true;$('pause').disabled=true;};$('reset').onclick=()=>{if(running)return;if(!confirm('이 5·10·15분봉 검증 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(s){const v=String(s??'');return /[",\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;}
$('csv').onclick=()=>{if(!finished())return;const cols=['version','date','tp_pct','sl_pct','horizon_minutes','fee_pct_side','slip_pct_side','market','timeframe_minutes','observed_day_bars','valid_windows','truncated','c_signals','c_unknown','signal_time_kst','entry_time_kst','is_first_signal','outcome','net_pct','entry','exit','minutes','reason'];const lines=[cols.join(',')];for(const r of records()){for(const [k,u] of timeframes){const a=r[k],base={version:'V11.14.15',date:state.config.date,tp_pct:state.config.tp,sl_pct:state.config.sl,horizon_minutes:state.config.horizon,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip,market:r.market,timeframe_minutes:u,observed_day_bars:a.observed,valid_windows:a.valid_windows,truncated:a.truncated,c_signals:a.signals,c_unknown:a.unknown};const es=a.events.length?a.events:[{}];for(const e of es){const record={...base,...e};lines.push(cols.map(col=>csvCell(record[col])).join(','));}}}const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`bithumb_C_5m_10m_15m_${state.config.date}_v111415.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);};
state=load();if(state){for(const k of ['date','tp','sl','horizon','fee','slip'])$(k).value=state.config[k];}paint();
</script></body></html>
'''


def register_tf_v111415_lab(app, deps):
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache = {'names': None, 'at': 0}
    mutex = Lock()

    def listed():
        with mutex:
            if cache['names'] is None or time.time()-cache['at']>600:
                names = sorted({m for m in get_markets()
                                if isinstance(m,str) and MARKET_RE.fullmatch(m)})
                if not names:
                    raise ValueError('현재 KRW 종목 목록이 없습니다')
                cache.update(names=names,at=time.time())
            return cache['names']

    @app.get('/backtest-winrate-5m10m15m')
    def page_tf_v111415():
        return render_template_string(TF_V111415_PAGE)

    @app.get('/api/winrate14tf1015/universe')
    def universe_tf_v111415():
        try:
            names = listed()
            return jsonify({'ok':True,'items':names,'count':len(names),
                            'rule_version':'V11.14.15',
                            'universe_type':'currently_listed_KRW'})
        except Exception as exc:
            return jsonify({'ok':False,'error':str(exc)[:160]}),400

    @app.post('/api/winrate14tf1015/evaluate')
    def evaluate_tf_v111415():
        try:
            body = request.get_json(silent=True)
            cfg = lab_valid_request(body,kst)
            markets = body.get('markets') if isinstance(body,dict) else None
            if (not isinstance(markets,list) or not 1 <= len(markets) <= 2
                    or len(set(markets)) != len(markets)
                    or any(not isinstance(m,str) or not MARKET_RE.fullmatch(m)
                           for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 검사합니다')
            names = set(listed())
            if any(m not in names for m in markets):
                raise ValueError('현재 KRW 종목 목록에 없는 코인입니다')
            results,errors = [],[]
            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks = {pool.submit(tf_v111415_evaluate_symbol,
                                     api_get,m,cfg,kst):m for m in markets}
                for future in as_completed(tasks):
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        errors.append({'market':tasks[future],'error':str(exc)[:150]})
            return jsonify({'ok':not errors,'scanned':len(markets),
                            'results':results,'errors':len(errors),
                            'error_markets':errors,'rule_version':'V11.14.15'})
        except Exception as exc:
            return jsonify({'ok':False,'error':str(exc)[:160]}),400


_v111414_register_daytrade_review = register_daytrade_review


def register_daytrade_review(app,deps):
    _v111414_register_daytrade_review(app,deps)
    register_tf_v111415_lab(app,deps)


# ---------------------------------------------------------------------------
# V11.14.16: C strategy post-entry exit-study lab.
# KEEP the existing C signal universe, cooldown and entry completely unchanged.
# Only TP, SL and hold length differ across exit arms. No live V2 changes.
# All exits are 5M OHLC simulations with SL priority for same-bar ambiguity.
# ---------------------------------------------------------------------------

V111416_PLANS = (
    ('base', '기존 C · +1.5/-1.0 · 60분', 1.5, 1.0, 60),
    ('a', 'A · +1.0/-1.0 · 60분', 1.0, 1.0, 60),
    ('b', 'B · +1.2/-0.8 · 60분', 1.2, 0.8, 60),
    ('c', 'C · +1.5/-1.0 · 30분', 1.5, 1.0, 30),
    ('d', 'D · +1.0/-0.8 · 30분', 1.0, 0.8, 30),
)


def exitdiag_v111416_config(raw, kst):
    """Validate user-controlled date and costs; keep all study exits fixed."""
    if not isinstance(raw, dict):
        raise ValueError('설정 데이터 형식이 올바르지 않습니다')
    return lab_valid_request({
        'date': raw.get('date'), 'tp': 1.5, 'sl': 1.0, 'horizon': 60,
        'fee': raw.get('fee', 0.04), 'slip': raw.get('slip', 0.05),
    }, kst)


def exitdiag_v111416_path(rows, signal_idx, cfg):
    """Descriptive future path only, NOT used for signal selection or exit.

    Require all 12 future 5M bars to compare the SAME 60-minute period.
    Max/min are retrospective, not executable profits or intrabar sequencing.
    The close at +5/+10/+20/+30/+60 minutes is a hypothetical marked exit
    net of both-side fees/slippage; intermediate actual exits are ignored.
    """
    n = 12
    if signal_idx + n >= len(rows):
        return {'complete': False, 'reason': '60분 관측 봉 부족'}
    if not lab_contiguous(rows, signal_idx, signal_idx + n):
        return {'complete': False, 'reason': '60분 관측 봉 공백'}
    entry = rows[signal_idx + 1]['o']
    look = rows[signal_idx + 1: signal_idx + n + 1]
    fee = (cfg['fee'] + cfg['slip']) / 100
    cp = {}
    for mins in (5, 10, 20, 30, 60):
        cl = rows[signal_idx + mins // 5]['c']
        cp[str(mins)] = round((cl * (1 - fee) / (entry * (1 + fee)) - 1) * 100, 4)
    return {
        'complete': True,
        'mfe_pct': round(100 * (max(b['h'] for b in look) / entry - 1), 4),
        'mae_pct': round(100 * (min(b['l'] for b in look) / entry - 1), 4),
        'checkpoints_net_pct': cp,
    }


def exitdiag_v111416_evaluate_symbol(api_get, market, cfg, kst):
    """Exact historical 5M C signals from V11.14.5, then 4 exit replays.

    One paginated raw 5M request per market, captured from the original C
    evaluator, ensures the baseline is unmodified and every arm sees the same
    underlying OHLC and signal timestamps. Cooldown stays fixed at 60min.
    """
    captured = {}

    def capture5(path, params):
        raw = api_get(path, params)
        if path == '/v1/candles/minutes/5' and isinstance(raw, list):
            for bar in raw:
                if not isinstance(bar, dict):
                    continue
                try:
                    t = local_time(bar['candle_date_time_kst'], kst)
                    o, h, l, c = [float(bar[k]) for k in (
                        'opening_price', 'high_price', 'low_price', 'trade_price')]
                    v = float(bar['candle_acc_trade_volume'])
                    if (all(isfinite(q) and q > 0 for q in (o, h, l, c))
                            and isfinite(v) and v >= 0
                            and l <= min(o, c) and h >= max(o, c) and l <= h):
                        captured[t] = {'t': t, 'o': o, 'h': h,
                                       'l': l, 'c': c, 'v': v}
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
        return raw

    original = lab_cdiag_v11145_evaluate_symbol(capture5, market, cfg, kst)
    rows = [captured[t] for t in sorted(captured)]
    by_time = {r['t']: i for i, r in enumerate(rows)}
    events = []
    for original_event in original['events']:
        # Existing 'signal_kst' is the entry time (signal + 5 minutes).
        entry_t = local_time(original_event['signal_kst'], kst)
        signal_t = entry_t - timedelta(minutes=5)
        i = by_time.get(signal_t)
        if i is None:
            raise ValueError('기존 C 신호와 5분봉 기준시각 불일치')
        arms = {}
        for key, label, tp, sl, horizon in V111416_PLANS:
            if key == 'base':
                # Copy the original result, do not recalculate the benchmark.
                outcome = {k: original_event.get(k) for k in (
                    'outcome', 'reason', 'net_pct', 'entry', 'exit', 'minutes')}
            else:
                varied = {**cfg, 'tp': tp, 'sl': sl,
                          'horizon': horizon, 'bars': horizon // 5}
                outcome = lab_outcome_v11145(rows, i, varied)
            arms[key] = outcome
        events.append({
            'signal_time_kst': signal_t.strftime('%Y-%m-%d %H:%M'),
            'entry_time_kst': entry_t.strftime('%Y-%m-%d %H:%M'),
            'is_first_signal': original_event['is_first_signal'],
            'arms': arms,
            'path': exitdiag_v111416_path(rows, i, cfg),
        })
    assert len(events) == original['signals']
    return {
        'market': market, 'observed_5m': original['observed_5m'],
        'valid_windows': original['valid_windows'],
        'truncated': original['truncated'],
        'signals': original['signals'],
        'unknown_base': original['unknown'],
        'events': events,
    }


EXITDIAG_V111416_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.16 · C 진입 후 익절/손절/보유시간 비교</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#1a223b;--card:#121a30;--line:#35435e;--fg:#f3f6ff;--muted:#b5c4de;--blue:#416cf3;--good:#61e2a3;--bad:#ff9a93}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,-apple-system,"Noto Sans KR",sans-serif}main{max-width:1080px;margin:auto;padding:18px 14px 65px}h1{font-size:23px;margin:6px 0}h2{font-size:18px;margin:0 0 9px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px;margin:14px 0}.muted,.hint{font-size:13px;color:var(--muted)}.note{font-size:13px;line-height:1.75;border-radius:10px;padding:12px;background:#10182d}
.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.field{font-size:13px;color:var(--muted);display:flex;flex-direction:column;gap:5px}input,button{font:inherit}input{padding:10px;background:#111a2e;border:1px solid var(--line);color:var(--fg);border-radius:8px;width:100%;min-width:0}button{background:var(--blue);border:0;color:white;border-radius:9px;padding:12px 14px;font-weight:750;cursor:pointer}button.secondary{background:#364762}button:disabled{opacity:.45}.actions{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px}.stat{border:1px solid var(--line);background:var(--card);border-radius:10px;padding:11px}.stat small{display:block;color:var(--muted);font-size:12px}.stat strong{display:block;font-size:21px}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--line);padding:10px 8px;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{color:var(--muted)}.status{font-size:13px;color:var(--muted);white-space:pre-wrap}progress{width:100%;height:16px}a{color:#bcd1ff}
@media(max-width:650px){.controls,.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><main>
<h1>🧪 V11.14.16 · C 진입 후 청산 전략 비교</h1>
<p class="muted">빗썸 KRW 과거 모의 검증실 · 기존 5분봉 C 신호·진입 시점·60분 신호 쿨다운은 전혀 변경하지 않습니다.</p>
<section class="panel"><h2>비교할 5개 청산 규칙</h2>
<div class="scroll"><table><thead><tr><th>전략</th><th>익절</th><th>손절</th><th>보유</th></tr></thead><tbody>
<tr><td>기존 C</td><td>+1.5%</td><td>-1.0%</td><td>60분</td></tr>
<tr><td>실험 A</td><td>+1.0%</td><td>-1.0%</td><td>60분</td></tr>
<tr><td>실험 B</td><td>+1.2%</td><td>-0.8%</td><td>60분</td></tr>
<tr><td>실험 C</td><td>+1.5%</td><td>-1.0%</td><td>30분</td></tr>
<tr><td>실험 D</td><td>+1.0%</td><td>-0.8%</td><td>30분</td></tr>
</tbody></table></div>
<div class="note">같은 C 신호의 <b>다음 5분봉 시가</b>에 진입하고, 이후 관측된 5분봉 고가·저가로 청산을 재계산합니다. 손절/익절이 같은 봉에 동시에 닿으면 <b>손절 우선</b>, 청산 전 봉 공백은 <b>미판정</b>입니다. 모든 전략에서 진입 신호·60분 쿨다운은 동일합니다.<br>
<b>주의:</b> 미판정 비율이 다를 수 있으므로 전체 판정 표 외에 다섯 규칙 모두 판정된 <b>동일 신호 공통 표</b>를 별도로 제시합니다. 이는 실전 체결·호가·시장충격을 재현하지 않습니다.</div></section>
<section class="panel"><h2>검증 설정</h2><div class="controls">
<label class="field">검증 날짜 (한국시간)<input id="date" type="date"></label>
<label class="field">편도 수수료 (%)<input id="fee" type="number" value="0.04" min="0" max="0.5" step="0.01"></label>
<label class="field">편도 슬리피지 (%)<input id="slip" type="number" value="0.05" min="0" max="0.5" step="0.01"></label>
</div>
<div class="actions"><button id="run">전체 KRW 비교 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">이 검증 기록 초기화</button><button id="csv" class="secondary" disabled>청산 전략 비교 CSV 저장</button></div>
<progress id="progress" max="1" value="0"></progress><div id="status" class="status">검사 전</div></section>
<section class="panel"><h2>검사 진행 및 데이터</h2><div id="coverage" class="stats">검사 전</div></section>
<section class="panel"><h2>청산 규칙별 전체 결과</h2><p class="hint">전체 KRW 시장 검사가 끝난 뒤 공개. 각 규칙에서 손익 판정이 가능한 거래만 계산합니다.</p><div class="scroll" id="summary">검사 완료 후 공개</div></section>
<section class="panel"><h2>공정 비교 · 동일 신호 5종 모두 판정</h2><div class="scroll" id="matched">검사 완료 후 공개</div><p class="hint">다섯 규칙 중 하나라도 미판정인 신호는 공통 표에서만 제외합니다. 결과가 미판정인 신호를 수익 또는 무손실로 취급하지 않습니다.</p></section>
<section class="panel"><h2>첫 신호 / 반복 신호 비교</h2><div class="scroll" id="first">검사 완료 후 공개</div></section>
<section class="panel"><h2>진입 후 60분 가격 움직임</h2><div id="path">검사 완료 후 공개</div><p class="hint">최대 상승폭(MFE)·최대 하락폭(MAE)은 60분 전체를 뒤돌아본 관측치입니다. 같은 봉 안의 가격 경로는 알 수 없으며, MFE는 실제로 실현 가능한 수익을 뜻하지 않습니다. 시간별 수익률은 매번 해당 시점에 강제로 청산했다고 가정한 참고 수치입니다.</p></section>
<section class="panel"><h2>종목별 결과</h2><div class="scroll" id="markets">검사 완료 후 공개</div></section>
<script>
'use strict';
const API='/api/winrate14exit', KEY='bithumb_v111416_exitdiag_checkpoint_v1';
const plans=[['base','기존 C','1.5 / 1.0 / 60'],['a','실험 A','1.0 / 1.0 / 60'],['b','실험 B','1.2 / 0.8 / 60'],['c','실험 C','1.5 / 1.0 / 30'],['d','실험 D','1.0 / 0.8 / 30']];
const $=id=>document.getElementById(id); let state=null,running=false,stop=false,storeFailed=false;
function escapeHtml(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function asNumber(x){if(x===null||x===undefined||x==='')return NaN;return Number(x);}
function pct(n){return Number.isFinite(n)?n.toFixed(3)+'%':'—';}
function formatDate(d){const y=d.getFullYear(),m=String(d.getMonth()+1).padStart(2,'0'),day=String(d.getDate()).padStart(2,'0');return `${y}-${m}-${day}`;}
(function defaultDate(){const d=new Date();d.setDate(d.getDate()-1);$('date').value=formatDate(d);})();
function config(){const date=$('date').value,fee=Number($('fee').value),slip=Number($('slip').value);if(!/^\d{4}-\d\d-\d\d$/.test(date))throw Error('날짜를 선택해줘');if(!Number.isFinite(fee)||!Number.isFinite(slip)||fee<0||fee>0.5||slip<0||slip>0.5)throw Error('비용은 0~0.5%에서 설정해줘');return {date,fee,slip};}
const sig=c=>JSON.stringify(c);
function load(){try{const s=JSON.parse(localStorage.getItem(KEY));return s&&s.version===1&&Array.isArray(s.markets)&&s.results&&typeof s.results==='object'?s:null;}catch{return null;}}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function records(){if(!state)return [];return state.markets.filter(m=>Object.prototype.hasOwnProperty.call(state.results,m)).map(m=>state.results[m]);}
function finished(){return !!(state&&state.markets.length&&records().length===state.markets.length&&!state.failed);}
function items(rs){const out=[];for(const r of rs){for(const e of r.events){out.push({...e,market:r.market});}}return out;}
function summarize(evs,key){let win=0,n=0,net=0,tp=0,sl=0,tm=0,unknown=0,minutes=0;for(const e of evs){const a=e.arms[key];if(a.outcome==='UNKNOWN'){unknown++;continue;}const p=asNumber(a.net_pct);if(!Number.isFinite(p))continue;n++;net+=p;minutes+=asNumber(a.minutes);if(p>0)win++;if(a.outcome==='TP')tp++;if(a.outcome==='SL')sl++;if(a.outcome==='TIME')tm++;}return {signals:evs.length,n,unknown,win,tp,sl,tm,avg:n?net/n:NaN,rate:n?100*win/n:NaN,minutes:n?minutes/n:NaN};}
function summaryTable(evs){const head='<table><thead><tr><th>전략</th><th>신호</th><th>판정</th><th>미판정</th><th>수익 비율</th><th>평균 순손익</th><th>익절/손절/시간</th><th>평균 청산분</th></tr></thead><tbody>';return head+plans.map(([k,label])=>{const s=summarize(evs,k);return `<tr><td>${escapeHtml(label)}</td><td>${s.signals}</td><td>${s.n}</td><td>${s.unknown}</td><td>${pct(s.rate)}</td><td>${pct(s.avg)}</td><td>${s.tp}/${s.sl}/${s.tm}</td><td>${Number.isFinite(s.minutes)?s.minutes.toFixed(1):'—'}</td></tr>`;}).join('')+'</tbody></table>';}
function card(title,value){return `<div class="stat"><small>${escapeHtml(title)}</small><strong>${escapeHtml(value)}</strong></div>`;}
function paint(){const rs=records(),count=rs.length,total=state?.markets.length||0,full=finished(),all=items(rs),decided=all.filter(e=>plans.every(([k])=>e.arms[k].outcome!=='UNKNOWN'&&Number.isFinite(asNumber(e.arms[k].net_pct))));$('progress').max=total||1;$('progress').value=count;$('csv').disabled=!full;$('coverage').innerHTML=card('검사 종목',`${count} / ${total}`)+card('기존 C 신호',String(all.length))+card('기존 C 미판정',String(all.filter(e=>e.arms.base.outcome==='UNKNOWN').length))+card('60분 가격 경로 완전',String(all.filter(e=>e.path.complete).length))+card('전체 5분봉 유효 종목',String(rs.filter(r=>r.valid_windows>0).length))+card('5개 규칙 공통 판정',String(decided.length));
if(!full){for(const id of ['summary','matched','first','path','markets'])$(id).textContent='전체 검사 완료 후 공개';}else{
$('summary').innerHTML=summaryTable(all);$('matched').innerHTML=`<p class="hint">원래 C 신호 ${all.length}건 중 공통 판정 ${decided.length}건</p>`+summaryTable(decided);
$('first').innerHTML='<h3>당일 종목별 첫 C 신호</h3>'+summaryTable(all.filter(e=>e.is_first_signal))+'<h3>같은 종목의 반복 C 신호</h3>'+summaryTable(all.filter(e=>!e.is_first_signal));
const paths=all.filter(e=>e.path.complete),mfe=paths.map(e=>asNumber(e.path.mfe_pct)),mae=paths.map(e=>asNumber(e.path.mae_pct));const mean=a=>a.length?a.reduce((s,v)=>s+v,0)/a.length:NaN;const cps=[5,10,20,30,60].map(x=>[x,mean(paths.map(e=>asNumber(e.path.checkpoints_net_pct[String(x)])))]);$('path').innerHTML=`<p class="hint">60분 구간 연속 관측 가능 신호 ${paths.length}건 / 전체 ${all.length}건</p><div class="stats">`+card('평균 최대 상승폭(MFE)',pct(mean(mfe)))+card('평균 최대 하락폭(MAE)',pct(mean(mae)))+card('60분 내 고가 +1.5% 이상',paths.length?`${mfe.filter(x=>x>=1.5).length} / ${paths.length}`:'—')+'</div><div class="scroll"><table><thead><tr><th>가상 청산 시점</th><th>평균 순손익(비용 반영)</th></tr></thead><tbody>'+cps.map(([x,v])=>`<tr><td>${x}분</td><td>${pct(v)}</td></tr>`).join('')+'</tbody></table></div>';
const coins=rs.filter(r=>r.signals>0).sort((a,b)=>a.market.localeCompare(b.market));$('markets').innerHTML=coins.length?'<table><thead><tr><th>종목</th><th>C 신호</th><th>기존 평균</th><th>A 평균</th><th>B 평균</th><th>C 평균</th><th>D 평균</th></tr></thead><tbody>'+coins.map(r=>{const a=plans.map(([k])=>summarize(r.events,k));return `<tr><td>${escapeHtml(r.market)}</td><td>${r.signals}</td>${a.map(x=>`<td>${pct(x.avg)}</td>`).join('')}</tr>`;}).join('')+'</tbody></table>':'C 신호 없음';}
$('status').textContent=(full?'✅ 전체 KRW 비교 완료':running?'⏳ 전체 KRW 검사 진행 중':'검사 대기 또는 일시 중지')+` · ${count}/${total}종목`+(state?.failed?' · 오류 종목 발생 (다시 실행하여 재검사 가능)':'')+(full?'\n주의: 과거 모의 성적이며 실전 거래 성과가 아닙니다.':'\n검사 완료 전에는 최종 수익률을 공개하지 않습니다.');}
async function requestData(url,options){const ac=new AbortController(),timer=setTimeout(()=>ac.abort(),115000);try{const resp=await fetch(url,{...options,cache:'no-store',signal:ac.signal});const j=await resp.json();if(!resp.ok||!j.ok)throw Error(j.error||'HTTP '+resp.status);return j;}finally{clearTimeout(timer);}}
async function init(c){const x=await requestData(API+'/universe');if(!Array.isArray(x.items)||!x.items.length)throw Error('현재 KRW 종목 목록이 없습니다');state={version:1,config:c,markets:x.items,results:{},failed:0};save();}
function validResponse(x,m){if(x.scanned!==1||x.errors!==0||!Array.isArray(x.results)||x.results.length!==1)return false;const r=x.results[0];return r.market===m&&Array.isArray(r.events)&&r.events.length===r.signals&&r.events.every(e=>e.arms&&plans.every(([k])=>e.arms[k]&&typeof e.arms[k].outcome==='string')&&e.path&&typeof e.path.complete==='boolean');}
async function run(){if(running)return;let c;try{c=config();}catch(e){$('status').textContent='⚠️ '+e.message;return;}if(state&&sig(c)!==sig(state.config)){if(!confirm('검사 날짜나 비용이 달라졌어. 이 검증실의 이전 결과만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);}running=true;stop=false;storeFailed=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;try{if(!state)await init(c);state.failed=0;save();paint();const pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m));let next=0,fail=0;async function worker(){while(!stop&&!storeFailed){const market=pending[next++];if(!market)return;let good=false;for(let attempt=0;attempt<3&&!stop;attempt++){try{const x=await requestData(API+'/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:[market]})});if(!validResponse(x,market))throw Error('응답 검증 실패');state.results[market]=x.results[0];try{save();}catch{storeFailed=true;$('status').textContent='⚠️ 휴대폰 저장공간 부족으로 검사 중지';break;}good=true;paint();break;}catch(e){if(attempt===2){fail++;$('status').textContent='⚠️ '+market+' 검사 실패: '+String(e.message||e);}else await new Promise(ok=>setTimeout(ok,(attempt+1)*1200));}}if(!good&&fail>=4){stop=true;break;}}}await Promise.all([worker(),worker()]);state.failed=fail;try{save();}catch{}paint();if(stop)$('status').textContent+='\n중지됐어. 다시 누르면 미검사 코인부터 이어서 검사해.';}catch(e){$('status').textContent='⚠️ '+String(e.message||e);}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}}
$('run').onclick=run;$('pause').onclick=()=>{stop=true;$('pause').disabled=true;};$('reset').onclick=()=>{if(running)return;if(!confirm('이 청산 비교 검증실의 기록만 지울까?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(s){let v=String(s??'');if(/^[\s]*[=+@]/.test(v)||(/^[\s]*-/.test(v)&&!/^\s*-\d+(?:\.\d+)?\s*$/.test(v)))v="'"+v;return /[",\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;}
$('csv').onclick=()=>{if(!finished())return;const cols=['version','date','market','signal_time_kst','entry_time_kst','is_first_signal','fee_pct_side','slip_pct_side','strategy','tp_pct','sl_pct','horizon_minutes','outcome','net_pct','entry','exit','minutes','reason','path_complete','mfe_pct','mae_pct','close_5m_net_pct','close_10m_net_pct','close_20m_net_pct','close_30m_net_pct','close_60m_net_pct'];const lines=[cols.join(',')];for(const r of records()){if(!r.events.length){const v={version:'V11.14.16',date:state.config.date,market:r.market,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip};lines.push(cols.map(c=>csvCell(v[c])).join(','));continue;}for(const e of r.events){for(const [k,label,tp,sl,horizon] of [['base','기존 C',1.5,1,60],['a','A',1,1,60],['b','B',1.2,0.8,60],['c','C',1.5,1,30],['d','D',1,0.8,30]]){const a=e.arms[k],cp=e.path.checkpoints_net_pct||{};const v={version:'V11.14.16',date:state.config.date,market:r.market,signal_time_kst:e.signal_time_kst,entry_time_kst:e.entry_time_kst,is_first_signal:e.is_first_signal,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip,strategy:k,tp_pct:tp,sl_pct:sl,horizon_minutes:horizon,outcome:a.outcome,net_pct:a.net_pct,entry:a.entry,exit:a.exit,minutes:a.minutes,reason:a.reason,path_complete:e.path.complete,mfe_pct:e.path.mfe_pct,mae_pct:e.path.mae_pct,close_5m_net_pct:cp['5'],close_10m_net_pct:cp['10'],close_20m_net_pct:cp['20'],close_30m_net_pct:cp['30'],close_60m_net_pct:cp['60']};lines.push(cols.map(c=>csvCell(v[c])).join(','));}}}const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`bithumb_C_exit_comparison_${state.config.date}_v111416.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);};
state=load();if(state){for(const key of ['date','fee','slip'])$(key).value=state.config[key];}paint();
</script></body></html>'''


def register_exitdiag_v111416_lab(app, deps):
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache = {'markets': None, 'at': 0}
    mutex = Lock()

    def listed():
        with mutex:
            if cache['markets'] is None or time.time() - cache['at'] > 600:
                markets = sorted({m for m in get_markets()
                                  if isinstance(m, str) and MARKET_RE.fullmatch(m)})
                if not markets:
                    raise ValueError('현재 KRW 종목 목록이 없습니다')
                cache.update(markets=markets, at=time.time())
            return cache['markets']

    @app.get('/backtest-winrate-exit-compare')
    def page_exit_v111416():
        return render_template_string(EXITDIAG_V111416_PAGE)

    @app.get('/api/winrate14exit/universe')
    def universe_exit_v111416():
        try:
            names = listed()
            return jsonify({'ok': True, 'items': names, 'count': len(names),
                            'rule_version': 'V11.14.16',
                            'universe_type': 'currently_listed_KRW'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400

    @app.post('/api/winrate14exit/evaluate')
    def evaluate_exit_v111416():
        try:
            body = request.get_json(silent=True)
            cfg = exitdiag_v111416_config(body, kst)
            markets = body.get('markets') if isinstance(body, dict) else None
            if (not isinstance(markets, list) or not 1 <= len(markets) <= 2
                    or len(set(markets)) != len(markets)
                    or any(not isinstance(m, str) or not MARKET_RE.fullmatch(m)
                           for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 검사합니다')
            allowed = set(listed())
            if any(m not in allowed for m in markets):
                raise ValueError('현재 KRW 시장에 없는 종목입니다')
            results, errors = [], []
            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks = {pool.submit(exitdiag_v111416_evaluate_symbol,
                                     api_get, m, cfg, kst): m for m in markets}
                for future in as_completed(tasks):
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        errors.append({'market': tasks[future], 'error': str(exc)[:150]})
            return jsonify({'ok': not errors, 'scanned': len(markets),
                            'results': results, 'errors': len(errors),
                            'error_markets': errors, 'rule_version': 'V11.14.16'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400


_v111415_register_daytrade_review = register_daytrade_review


def register_daytrade_review(app, deps):
    _v111415_register_daytrade_review(app, deps)
    register_exitdiag_v111416_lab(app, deps)


# ---------------------------------------------------------------------------
# V11.14.17: independent original C vs 5M SMA(5) first-reclaim C5 lab.
# V11.14.16 and all live BUY routes remain UNCHANGED (append-only).
# The original historical C is calculated through its original evaluator.
# C5 uses an independent 60min cooldown and the SAME 5M coverage / exits.
# ---------------------------------------------------------------------------

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


def c5_v111417_features(rows, i):
    prev_ma5 = sum(r['c'] for r in rows[i-5:i]) / 5
    now_ma5 = sum(r['c'] for r in rows[i-4:i+1]) / 5
    return {
        'ma5_prev': round(prev_ma5, 10),
        'ma5_signal': round(now_ma5, 10),
        'ma5_slope_pct': round((now_ma5 / prev_ma5 - 1) * 100, 4),
        'prev_close_vs_ma5_pct': round((rows[i-1]['c'] / prev_ma5 - 1) * 100, 4),
        'reclaim_vs_ma5_pct': round((rows[i]['c'] / now_ma5 - 1) * 100, 4),
    }


def c5_v111417_config(raw, kst):
    """Keep TP/SL/hold fixed. Only selected date and costs may vary."""
    if not isinstance(raw, dict):
        raise ValueError('설정 형식 오류')
    return lab_valid_request({
        'date': raw.get('date'), 'tp': 1.5, 'sl': 1.0, 'horizon': 60,
        'fee': raw.get('fee', 0.04), 'slip': raw.get('slip', 0.05),
    }, kst)


def c5_v111417_evaluate_symbol(api_get, market, cfg, kst):
    """Reuse original C evaluator and its 5m response pages for C5.

    IMPORTANT: original C signal timestamps, 60m cooldown, and outcomes
    are copied unmodified from V11.14.5. C5 independently tests SMA5
    within the same 25-contiguous-bar eligible windows (coverage matched).
    """
    seen = {}

    def capture5(path, params):
        raw = api_get(path, params)
        if path == '/v1/candles/minutes/5' and isinstance(raw, list):
            for bar in raw:
                if not isinstance(bar, dict):
                    continue
                try:
                    t = local_time(bar['candle_date_time_kst'], kst)
                    o, h, l, c = (float(bar[key]) for key in (
                        'opening_price','high_price','low_price','trade_price'))
                    v = float(bar['candle_acc_trade_volume'])
                    if (all(isfinite(x) and x > 0 for x in (o,h,l,c))
                            and isfinite(v) and v >= 0
                            and l <= min(o,c) and h >= max(o,c) and l <= h):
                        seen[t] = {'t':t,'o':o,'h':h,'l':l,'c':c,'v':v}
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
        return raw

    old = lab_cdiag_v11145_evaluate_symbol(capture5, market, cfg, kst)
    day_start = datetime.combine(cfg['day'], _time.min, tzinfo=kst)
    day_end = day_start + timedelta(days=1)
    begin = day_start - timedelta(hours=4)
    end = day_end + timedelta(hours=2, minutes=5)
    rows = [seen[t] for t in sorted(seen) if begin <= t < end]
    time_index = {r['t']: i for i,r in enumerate(rows)}

    old_events = []
    for ev in old['events']:
        entry_t = local_time(ev['signal_kst'], kst)
        signal_t = entry_t - timedelta(minutes=5)
        if signal_t not in time_index:
            raise ValueError('기존 C 신호 시각과 원본 봉이 일치하지 않습니다')
        old_events.append({
            'signal_time_kst': signal_t.strftime('%Y-%m-%d %H:%M'),
            'entry_time_kst': entry_t.strftime('%Y-%m-%d %H:%M'),
            'is_first_signal': ev['is_first_signal'],
            'outcome': ev['outcome'], 'reason': ev.get('reason'),
            'net_pct': ev.get('net_pct'), 'entry': ev.get('entry'),
            'exit': ev.get('exit'), 'minutes': ev.get('minutes'),
        })

    eligible = 0
    cooldown = -1
    c5_events = []
    n = cfg['bars']
    if len(rows) >= LAB_WARMUP_BARS + 2:
        for i in range(LAB_WARMUP_BARS, len(rows)-1):
            t = rows[i]['t']
            if not (day_start <= t < day_end):
                continue
            if not lab_contiguous(rows, i-24, i):
                continue
            eligible += 1
            if i < cooldown or not c5_v111417_signal(rows, i):
                continue
            cooldown = i + n  # independent, like original C: 60m signal cooldown
            outcome = lab_outcome_v11145(rows, i, cfg)
            entry_t = t + timedelta(minutes=5)
            c5_events.append({
                'signal_time_kst': t.strftime('%Y-%m-%d %H:%M'),
                'entry_time_kst': entry_t.strftime('%Y-%m-%d %H:%M'),
                'is_first_signal': not c5_events,
                **outcome,
                'features': c5_v111417_features(rows, i),
            })
    if eligible != old['valid_windows']:
        raise ValueError('C와 C5의 비교 가능 5분봉 구간이 다릅니다')
    c5_unknown = sum(e['outcome'] == 'UNKNOWN' for e in c5_events)
    return {
        'market': market, 'observed_5m': old['observed_5m'],
        'valid_windows': old['valid_windows'], 'truncated': old['truncated'],
        'c': {'signals': old['signals'], 'unknown': old['unknown'],
              'events': old_events},
        'c5': {'signals': len(c5_events), 'unknown': c5_unknown,
               'events': c5_events},
    }


C5_V111417_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.17 · 기존 C vs 5이평 회복 C5 검증</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#1a223b;--card:#121a30;--line:#35435e;--fg:#f3f6ff;--muted:#b5c4de;--blue:#416cf3;--good:#61e2a3;--bad:#ff9a93}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,-apple-system,"Noto Sans KR",sans-serif}main{max-width:1080px;margin:auto;padding:18px 14px 65px}h1{font-size:23px;margin:6px 0}h2{font-size:18px;margin:0 0 9px}h3{font-size:15px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px;margin:14px 0}.muted,.hint{font-size:13px;color:var(--muted)}.note{font-size:13px;line-height:1.75;border-radius:10px;padding:12px;background:#10182d}.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.field{font-size:13px;color:var(--muted);display:flex;flex-direction:column;gap:5px}input,button{font:inherit}input{padding:10px;background:#111a2e;border:1px solid var(--line);color:var(--fg);border-radius:8px;width:100%;min-width:0}button{background:var(--blue);border:0;color:white;border-radius:9px;padding:12px 14px;font-weight:750;cursor:pointer}button.secondary{background:#364762}button:disabled{opacity:.45}.actions{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px}.stat{border:1px solid var(--line);background:var(--card);border-radius:10px;padding:11px}.stat small{display:block;color:var(--muted);font-size:12px}.stat strong{display:block;font-size:21px}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--line);padding:10px 8px;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{color:var(--muted)}.status{font-size:13px;color:var(--muted);white-space:pre-wrap}progress{width:100%;height:16px}a{color:#bcd1ff}
@media(max-width:650px){.controls,.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><main>
<h1>🧪 V11.14.17 · 기존 C vs 5이평 회복 C5</h1>
<p class="muted">빗썸 KRW 전체 종목 · 5분봉 역사적 모의 비교 · 실전 BUY 스캐너 무변경</p>
<section class="panel"><h2>새 실험: 5이평 반등 C5</h2>
<div class="note"><b>기존 C:</b> 현재까지 사용한 상승 흐름→눌림→반등 조건(6·10·12봉 및 거래량·봉 모양 등)을 그대로 평가합니다.<br>
<b>새 C5:</b> ① 직전 봉 종가가 직전 5이평 <b>아래</b> ② 현재 봉이 <b>양봉</b>이고 종가가 현재 5이평 <b>위로 첫 회복</b> ③ 현재 5이평이 직전보다 <b>상승</b>하면 신호 확정.<br>
두 전략 모두 <b>다음 5분봉 시가</b> 진입 · <b>익절 +1.5% / 손절 -1.0% / 최대 60분</b> · 60분 신호 쿨다운(각 전략 독립) · 양방향 비용 반영. 손절/익절 동시 터치는 손절 우선, 청산 전 데이터 공백은 미판정입니다.</div>
<p class="hint">비교 가능성을 위해 두 전략 모두 동일한 <b>연속 25개 5분봉</b> 평가 구간을 사용합니다. 단, 서로 다른 시점에 신호가 발생하므로 <b>동일 거래의 전후 비교가 아닌 독립 전략 비교</b>입니다. 단일 날짜의 소폭 우위는 성능 향상을 의미하지 않습니다.</p></section>
<section class="panel"><h2>검증 설정</h2><div class="controls">
<label class="field">검증 날짜 (한국시간)<input id="date" type="date"></label>
<label class="field">편도 수수료 (%)<input id="fee" type="number" value="0.04" min="0" max="0.5" step="0.01"></label>
<label class="field">편도 슬리피지 (%)<input id="slip" type="number" value="0.05" min="0" max="0.5" step="0.01"></label>
</div>
<div class="actions"><button id="run">전체 KRW 비교 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">이 검증만 초기화</button><button id="csv" class="secondary" disabled>C vs C5 비교 CSV 저장</button></div>
<progress id="progress" max="1" value="0"></progress><div id="status" class="status">검사 전</div></section>
<section class="panel"><h2>검사 진행 상황</h2><div id="coverage" class="stats">검사 전</div></section>
<section class="panel"><h2>전체 결과 비교</h2><p class="hint">모든 KRW 종목 검사가 끝난 뒤 최종 승률·평균 순손익을 공개합니다.</p><div id="summary" class="scroll">전체 검사 완료 후 공개</div></section>
<section class="panel"><h2>종목별 첫 신호 vs 반복 신호</h2><div id="first" class="scroll">전체 검사 완료 후 공개</div></section>
<section class="panel"><h2>신호 시점 중복·차이</h2><div id="overlap">전체 검사 완료 후 공개</div></section>
<section class="panel"><h2>신호 발생 종목별 비교</h2><div id="markets" class="scroll">전체 검사 완료 후 공개</div></section>
<p class="hint">현재 상장된 KRW 종목만 대상으로 한 과거 검증입니다. 상장 폐지 종목은 포함되지 않을 수 있으며, 과거 실시간 체결 가능성을 입증하지 않습니다. 거래 권유 또는 수익 보장이 아닙니다.</p>
</main>
<script>
'use strict';
const API='/api/winrate14c5', KEY='bithumb_v111417_c_vs_c5_checkpoint_v1';
const $=id=>document.getElementById(id);let state=null,running=false,stopRequested=false,storeFailed=false;
const escapeHtml=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const asNumber=v=>(v===null||v===undefined||v==='')?NaN:Number(v);
const pct=v=>Number.isFinite(v)?v.toFixed(3)+'%':'—';
function kstDate(d){const v=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(d);const get=x=>v.find(p=>p.type===x).value;return `${get('year')}-${get('month')}-${get('day')}`;}
const yesterday=()=>kstDate(new Date(Date.now()-86400000));
$('date').max=yesterday();$('date').value=yesterday();
function config(){const date=$('date').value,fee=asNumber($('fee').value),slip=asNumber($('slip').value);if(!/^\d{4}-\d{2}-\d{2}$/.test(date)||date>yesterday())throw Error('종료된 과거 날짜를 선택해줘');if(!Number.isFinite(fee)||!Number.isFinite(slip)||fee<0||fee>0.5||slip<0||slip>0.5)throw Error('비용은 0~0.5% 범위에서 설정해줘');return {date,fee,slip};}
const signature=c=>JSON.stringify([c.date,c.fee,c.slip]);
function load(){try{const s=JSON.parse(localStorage.getItem(KEY)||'null');if(!s||s.version!==1||!s.config||!Array.isArray(s.markets)||!s.markets.length||!s.results||typeof s.results!=='object'||s.markets.some(m=>!/^KRW-[A-Z0-9]+$/.test(m))||new Set(s.markets).size!==s.markets.length)return null;return s;}catch{return null;}}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function records(){return state?state.markets.filter(m=>Object.prototype.hasOwnProperty.call(state.results,m)).map(m=>state.results[m]):[];}
function finished(){return !!(state&&state.markets.length&&records().length===state.markets.length&&state.failed===0);}
function events(rs,key){const list=[];for(const r of rs)for(const e of r[key].events)list.push({...e,market:r.market});return list;}
function stat(evs){let unknown=0,judged=0,wins=0,tp=0,sl=0,tm=0,totalNet=0;for(const e of evs){if(e.outcome==='UNKNOWN'){unknown++;continue;}const net=asNumber(e.net_pct);if(!Number.isFinite(net))continue;judged++;totalNet+=net;if(net>0)wins++;if(e.outcome==='TP')tp++;if(e.outcome==='SL')sl++;if(e.outcome==='TIME')tm++;}return {signals:evs.length,unknown,judged,wins,tp,sl,tm,rate:judged?100*wins/judged:NaN,avg:judged?totalNet/judged:NaN};}
function table(ce,c5e){const a=stat(ce),b=stat(c5e);const rows=[['신호 건수',a.signals,b.signals],['판정 완료',a.judged,b.judged],['미판정',a.unknown,b.unknown],['수익 거래',a.wins,b.wins],['승률',pct(a.rate),pct(b.rate)],['평균 순손익',pct(a.avg),pct(b.avg)],['익절 / 손절 / 시간종료',`${a.tp} / ${a.sl} / ${a.tm}`,`${b.tp} / ${b.sl} / ${b.tm}`]];return '<table><thead><tr><th>항목</th><th>기존 C</th><th>신규 C5</th></tr></thead><tbody>'+rows.map(a=>`<tr><td>${escapeHtml(a[0])}</td><td>${escapeHtml(a[1])}</td><td>${escapeHtml(a[2])}</td></tr>`).join('')+'</tbody></table>';}
function card(k,v){return `<div class="stat"><small>${escapeHtml(k)}</small><strong>${escapeHtml(v)}</strong></div>`;}
function paint(){const rs=records(),count=rs.length,total=state?.markets.length||0,full=finished(),ce=events(rs,'c'),c5e=events(rs,'c5');$('progress').max=total||1;$('progress').value=count;$('csv').disabled=!full;$('coverage').innerHTML=card('검사 종목',`${count} / ${total}`)+card('기존 C 신호',String(ce.length))+card('신규 C5 신호',String(c5e.length))+card('기존 C 미판정',String(stat(ce).unknown))+card('C5 미판정',String(stat(c5e).unknown))+card('평가 가능 종목',String(rs.filter(r=>r.valid_windows>0).length));
if(!full){for(const id of ['summary','first','overlap','markets'])$(id).textContent='전체 검사 완료 후 공개';}else{
$('summary').innerHTML=table(ce,c5e);
$('first').innerHTML='<h3>각 종목 첫 신호</h3>'+table(ce.filter(e=>e.is_first_signal),c5e.filter(e=>e.is_first_signal))+'<h3>같은 종목 반복 신호</h3>'+table(ce.filter(e=>!e.is_first_signal),c5e.filter(e=>!e.is_first_signal));
const co=new Set(ce.map(e=>e.market+'|'+e.signal_time_kst)),c5o=new Set(c5e.map(e=>e.market+'|'+e.signal_time_kst)),overlap=[...co].filter(x=>c5o.has(x)).length;$('overlap').innerHTML='<div class="stats">'+card('동일 종목·동일 시각 신호',String(overlap))+card('기존 C에만 있는 신호',String(co.size-overlap))+card('C5에만 있는 신호',String(c5o.size-overlap))+'</div><p class="hint">신호 발생 시각 비교이며, 두 전략이 같은 거래를 실행했다는 뜻은 아닙니다.</p>';
const coins=rs.filter(r=>r.c.signals||r.c5.signals).sort((a,b)=>a.market.localeCompare(b.market));$('markets').innerHTML=coins.length?'<table><thead><tr><th>종목</th><th>C 신호</th><th>C5 신호</th><th>C 판정</th><th>C5 판정</th><th>C 평균</th><th>C5 평균</th></tr></thead><tbody>'+coins.map(r=>{const a=stat(r.c.events),b=stat(r.c5.events);return `<tr><td>${escapeHtml(r.market)}</td><td>${a.signals}</td><td>${b.signals}</td><td>${a.judged}</td><td>${b.judged}</td><td>${pct(a.avg)}</td><td>${pct(b.avg)}</td></tr>`;}).join('')+'</tbody></table>':'신호 발생 종목 없음';}
$('status').textContent=(full?'✅ 전체 KRW 비교 완료':running?'⏳ 검사 진행 중':'검사 대기 또는 일시 중지')+` · ${count}/${total}종목`+(state?.failed?' · ⚠️ 오류 발생, 누락 종목은 이어하기로 재검사':'')+(full?'\n주의: 과거 모의 성적이며 실전 수익률이 아닙니다.':'\n최종 승률·손익은 전체 검사 완료 후에만 공개합니다.');}
async function fetchJson(url,options){const ac=new AbortController(),tm=setTimeout(()=>ac.abort(),115000);try{const res=await fetch(url,{...options,cache:'no-store',signal:ac.signal}),data=await res.json();if(!res.ok||!data.ok)throw Error(data.error||'HTTP '+res.status);return data;}finally{clearTimeout(tm);}}
async function init(c){const u=await fetchJson(API+'/universe');if(!Array.isArray(u.items)||!u.items.length)throw Error('빗썸 KRW 종목 목록 오류');state={version:1,config:c,markets:u.items,results:{},failed:0};save();}
function validResponse(data,market){if(data.scanned!==1||data.errors!==0||!Array.isArray(data.results)||data.results.length!==1)return false;const r=data.results[0];if(r.market!==market||!r.c||!r.c5||!Number.isInteger(r.valid_windows)||r.valid_windows<0)return false;for(const arm of [r.c,r.c5]){if(!Number.isInteger(arm.signals)||arm.signals<0||!Number.isInteger(arm.unknown)||arm.unknown<0||!Array.isArray(arm.events)||arm.events.length!==arm.signals||arm.events.filter(e=>e.outcome==='UNKNOWN').length!==arm.unknown||arm.events.some(e=>!['TP','SL','TIME','UNKNOWN'].includes(e.outcome)||typeof e.signal_time_kst!=='string'||typeof e.entry_time_kst!=='string'))return false;}return true;}
async function run(){if(running)return;let cfg;try{cfg=config();}catch(e){$('status').textContent='⚠️ '+e.message;return;}if(state&&signature(cfg)!==signature(state.config)){if(!confirm('설정이 달라졌어. 이 C5 검증 기록만 초기화하고 다시 시작할까?'))return;state=null;localStorage.removeItem(KEY);}running=true;stopRequested=false;storeFailed=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;try{if(!state)await init(cfg);state.failed=0;save();paint();const pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m));let next=0,failed=0;async function worker(){while(!stopRequested&&!storeFailed){const market=pending[next++];if(!market)return;let passed=false;for(let tries=0;tries<3&&!stopRequested;tries++){try{const res=await fetchJson(API+'/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:[market]})});if(!validResponse(res,market))throw Error('불완전한 결과');state.results[market]=res.results[0];try{save();}catch(e){storeFailed=true;$('status').textContent='⚠️ 휴대폰 저장 공간 부족 · 진행 중단';break;}passed=true;paint();break;}catch(e){if(tries===2){failed++;$('status').textContent='⚠️ '+market+': '+String(e.message||e);}else await new Promise(ok=>setTimeout(ok,1000*(tries+1)));}}if(!passed&&failed>=4){stopRequested=true;break;}}}await Promise.all([worker(),worker()]);state.failed=failed;try{save();}catch{}paint();if(stopRequested)$('status').textContent+='\n일시 중지됨 · 다시 실행하면 남은 종목부터 검사합니다.';}catch(e){$('status').textContent='⚠️ '+String(e.message||e);}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}}
$('run').onclick=run;$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;};$('reset').onclick=()=>{if(running)return;if(!confirm('C5 비교 검증실의 저장 기록만 지울까?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(x){let s=String(x??'');if(/^[\s]*[=+@]/.test(s)||(/^[\s]*-/.test(s)&&!/^[\s]*-\d+(?:\.\d+)?\s*$/.test(s)))s="'"+s;return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!finished())return;const cols=['version','date','market','strategy','observed_5m','valid_windows','truncated','strategy_signals','strategy_unknown','signal_time_kst','entry_time_kst','is_first_signal','outcome','reason','net_pct','entry','exit','minutes','tp_pct','sl_pct','horizon_minutes','fee_pct_side','slip_pct_side','ma5_prev','ma5_signal','ma5_slope_pct','prev_close_vs_ma5_pct','reclaim_vs_ma5_pct'];const lines=[cols.join(',')];for(const r of records())for(const [key,strategy] of [['c','C'],['c5','C5']]){const arm=r[key],prefix={version:'V11.14.17',date:state.config.date,market:r.market,strategy,observed_5m:r.observed_5m,valid_windows:r.valid_windows,truncated:r.truncated?1:0,strategy_signals:arm.signals,strategy_unknown:arm.unknown,tp_pct:1.5,sl_pct:1,horizon_minutes:60,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip};for(const ev of (arm.events.length?arm.events:[{}])){const o={...prefix,...ev,...(ev.features||{})};lines.push(cols.map(c=>csvCell(o[c])).join(','));}}const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`bithumb_C_vs_C5_${state.config.date}_v111417.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);};
state=load();if(state){for(const k of ['date','fee','slip'])$(k).value=state.config[k];}paint();
</script></body></html>
'''


def register_c5_v111417_lab(app, deps):
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache = {'markets': None, 'at': 0}
    mutex = Lock()

    def listed():
        with mutex:
            if cache['markets'] is None or time.time() - cache['at'] > 600:
                markets = sorted({m for m in get_markets()
                                  if isinstance(m, str) and MARKET_RE.fullmatch(m)})
                if not markets:
                    raise ValueError('현재 빗썸 KRW 종목이 없습니다')
                cache.update(markets=markets, at=time.time())
            return cache['markets']

    @app.get('/backtest-winrate-c-vs-c5')
    def page_c5_v111417():
        return render_template_string(C5_V111417_PAGE)

    @app.get('/api/winrate14c5/universe')
    def universe_c5_v111417():
        try:
            names = listed()
            return jsonify({'ok': True, 'items': names, 'count': len(names),
                            'rule_version': 'V11.14.17',
                            'universe_type': 'currently_listed_KRW'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400

    @app.post('/api/winrate14c5/evaluate')
    def evaluate_c5_v111417():
        try:
            body = request.get_json(silent=True)
            cfg = c5_v111417_config(body, kst)
            markets = body.get('markets') if isinstance(body, dict) else None
            if (not isinstance(markets, list) or not 1 <= len(markets) <= 2
                    or len(set(markets)) != len(markets)
                    or any(not isinstance(m, str) or not MARKET_RE.fullmatch(m)
                           for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 검사할 수 있습니다')
            names = set(listed())
            if any(m not in names for m in markets):
                raise ValueError('현재 KRW 마켓 목록에 없는 종목입니다')
            results, errors = [], []
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {pool.submit(c5_v111417_evaluate_symbol,
                                       api_get, m, cfg, kst):m for m in markets}
                for f in as_completed(futures):
                    try:
                        results.append(f.result())
                    except Exception as exc:
                        errors.append({'market': futures[f], 'error': str(exc)[:150]})
            return jsonify({'ok': not errors, 'scanned': len(markets),
                            'results': results, 'errors': len(errors),
                            'error_markets': errors, 'rule_version': 'V11.14.17'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400


_v111416_register_daytrade_review = register_daytrade_review


def register_daytrade_review(app, deps):
    _v111416_register_daytrade_review(app, deps)
    register_c5_v111417_lab(app, deps)


# ---------------------------------------------------------------------------
# V11.14.18: C5 one-per-trend BUY lock + post-stop exclusion study.
# Append only: no changes to live BUY or V11.14.17 and earlier endpoints.
# Legacy C5 remains the exact, unchanged V11.14.17 baseline.
# Experimental arms: TP 1.5/3/4%, SL 1%, 60m hold, 120m after SL.
# ---------------------------------------------------------------------------

C5LOCK_V111418_PLANS = (
    ('a', 'A · 1.5% 익절 + 잠금', 1.5),
    ('b', 'B · 3.0% 익절 + 잠금', 3.0),
    ('c', 'C · 4.0% 익절 + 잠금', 4.0),
)
C5LOCK_V111418_STOP_EXCLUSION_MINUTES = 120


def c5lock_v111418_config(raw, kst):
    """Only day and costs are editable; all experiment rules are fixed."""
    return c5_v111417_config(raw, kst)


def c5lock_v111418_evaluate_symbol(api_get, market, cfg, kst):
    """Replay raw C5 first-reclaim candidates with independent arm locks.

    Eligibility is identical to V11.14.17. Signal decisions see bars <= i.
    After taking a signal, outcomes are calculated with the unchanged historical
    5-minute settlement routine. Their EXIT TIME only affects LATER decisions;
    this is chronological, rather than a future-aware signal filter.

    Trend reset definition, fixed before testing: 3 CONSECUTIVE DECLINING
    SMA(5) updates on contiguous 5m bars. A wave id increments on the third.
    One BUY per wave, no overlapping entries, SL => additional 120m lock from
    the conservatively assumed end of the exit candle. An UNKNOWN means we
    cannot prove the position closed; conservatively block remaining day.
    """
    captured = {}

    def capture5(path, params):
        raw = api_get(path, params)
        if path == '/v1/candles/minutes/5' and isinstance(raw, list):
            for candle in raw:
                if not isinstance(candle, dict):
                    continue
                try:
                    t = local_time(candle['candle_date_time_kst'], kst)
                    o, h, l, c = (float(candle[k]) for k in (
                        'opening_price', 'high_price', 'low_price', 'trade_price'))
                    v = float(candle['candle_acc_trade_volume'])
                    if (all(isfinite(x) and x > 0 for x in (o, h, l, c))
                            and isfinite(v) and v >= 0
                            and l <= min(o, c) and h >= max(o, c) and l <= h):
                        captured[t] = {'t': t, 'o': o, 'h': h, 'l': l, 'c': c, 'v': v}
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
        return raw

    old = c5_v111417_evaluate_symbol(capture5, market, cfg, kst)
    day_start = datetime.combine(cfg['day'], _time.min, tzinfo=kst)
    day_end = day_start + timedelta(days=1)
    start = day_start - timedelta(hours=4)
    end = day_end + timedelta(hours=2, minutes=5)
    rows = [captured[t] for t in sorted(captured) if start <= t < end]
    bars = cfg['bars']
    step = timedelta(minutes=5)
    states = {}
    for key, label, tp in C5LOCK_V111418_PLANS:
        states[key] = {
            'label': label, 'tp': tp, 'traded_wave': None,
            'busy_until': None, 'cooldown_until': None, 'unknown_lock': False,
            'events': [], 'blocked_events': [],
            'blocked_reasons': {'SAME_WAVE': 0, 'POSITION_OPEN': 0,
                                'POST_SL_120M': 0, 'UNKNOWN_EXIT': 0},
        }

    eligible = 0
    raw_candidates = 0
    wave_id = 0
    down_run = 0
    for i in range(5, len(rows) - 1):
        # This trend-state update sees only closes observed through i.
        if lab_contiguous(rows, i - 5, i):
            now_ma5 = sum(r['c'] for r in rows[i-4:i+1]) / 5
            prev_ma5 = sum(r['c'] for r in rows[i-5:i]) / 5
            down_run = down_run + 1 if now_ma5 < prev_ma5 else 0
            if down_run == 3:
                wave_id += 1
        else:
            down_run = 0

        if i < LAB_WARMUP_BARS:
            continue
        t = rows[i]['t']
        if not day_start <= t < day_end:
            continue
        if not lab_contiguous(rows, i - 24, i):
            continue
        eligible += 1
        if not c5_v111417_signal(rows, i):
            continue
        raw_candidates += 1
        signal_close = t + step
        signal_at = t.strftime('%Y-%m-%d %H:%M')
        entry_at = signal_close.strftime('%Y-%m-%d %H:%M')
        features = c5_v111417_features(rows, i)

        for key, label, tp in C5LOCK_V111418_PLANS:
            state = states[key]
            # UNKNOWN / active position and SL cooldown take priority.
            if state['unknown_lock']:
                block = 'UNKNOWN_EXIT'
            elif state['busy_until'] and signal_close < state['busy_until']:
                block = 'POSITION_OPEN'
            elif (state['cooldown_until']
                  and signal_close < state['cooldown_until']):
                block = 'POST_SL_120M'
            elif state['traded_wave'] == wave_id:
                block = 'SAME_WAVE'
            else:
                block = None

            if block:
                state['blocked_reasons'][block] += 1
                # Opportunity cost is purely descriptive, never fed into
                # the block/entry decision. Each arm uses its OWN TP level.
                alt_cfg = {**cfg, 'tp': tp}
                hypothetical = lab_outcome_v11145(rows, i, alt_cfg)
                state['blocked_events'].append({
                    'signal_time_kst': signal_at, 'entry_time_kst': entry_at,
                    'wave_id': wave_id, 'block_reason': block,
                    'hypothetical_outcome': hypothetical['outcome'],
                    'hypothetical_net_pct': hypothetical.get('net_pct'),
                })
                continue

            arm_cfg = {**cfg, 'tp': tp}
            result = lab_outcome_v11145(rows, i, arm_cfg)
            event = {
                'signal_time_kst': signal_at, 'entry_time_kst': entry_at,
                'wave_id': wave_id, 'is_first_signal': not state['events'],
                **result, 'features': features,
            }
            state['events'].append(event)
            state['traded_wave'] = wave_id
            if result['outcome'] == 'UNKNOWN':
                state['unknown_lock'] = True
            else:
                # lab_outcome.minutes counts 5m bars from the NEXT entry
                # candle to the exit candle. Use END of exit candle, not
                # its opening, to avoid approving an overlapping BUY.
                exit_close = signal_close + timedelta(minutes=result['minutes'])
                state['busy_until'] = exit_close
                if result['outcome'] == 'SL':
                    state['cooldown_until'] = exit_close + timedelta(
                        minutes=C5LOCK_V111418_STOP_EXCLUSION_MINUTES)

    if eligible != old['valid_windows']:
        raise ValueError('기존 C5와 실험 C5의 평가 가능 구간 불일치')

    arms = {'base': old['c5']}
    for key, label, tp in C5LOCK_V111418_PLANS:
        state = states[key]
        blocked = state['blocked_events']
        arms[key] = {
            'signals': len(state['events']),
            'unknown': sum(e['outcome'] == 'UNKNOWN' for e in state['events']),
            'events': state['events'], 'blocked_count': len(blocked),
            'blocked_reasons': state['blocked_reasons'],
            'blocked_judged': sum(x['hypothetical_net_pct'] is not None for x in blocked),
            'blocked_profitable': sum(x['hypothetical_net_pct'] is not None and
                                      x['hypothetical_net_pct'] > 0 for x in blocked),
            'blocked_events': blocked,
        }
        if arms[key]['signals'] + arms[key]['blocked_count'] != raw_candidates:
            raise ValueError('원시 C5 신호 보존 수 불일치')
    return {
        'market': market, 'observed_5m': old['observed_5m'],
        'valid_windows': old['valid_windows'], 'truncated': old['truncated'],
        'raw_candidates': raw_candidates, 'arms': arms,
    }


C5LOCK_V111418_PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.18 · C5 중복 차단 + 익절 3·4% 검증</title>
<style>/*INSERT_PREVIOUS_STYLE*/
</style></head><body><main>
<h1>🧪 V11.14.18 · C5 BUY 잠금 실험</h1>
<p class="muted">5분봉 5이평 첫 회복 · 종목별 가상 거래 · 실전 BUY 스캐너 무변경</p>
<section class="panel"><h2>이번 실험에서 바꾸는 것</h2>
<div class="note"><b>기존 C5:</b> +1.5% 익절 · -1% 손절 · 최대 60분 · 종전 60분 신호 간격(변경 없음).<br>
<b>실험 A:</b> 익절 +1.5% · <b>실험 B:</b> 익절 +3% · <b>실험 C:</b> 익절 +4%. 세 실험 모두 손절 -1%, 최대 60분.<br>
<b>세 실험 공통 BUY 차단:</b> 같은 상승 흐름에선 첫 BUY만 허용. 상승 흐름은 <b>5이평이 연속 3개 봉 하락</b>하면 종료한 것으로 보고, 이후 새 상승 흐름의 첫 회복을 허용합니다. 포지션이 열려 있는 동안 중복 BUY 차단, <b>가상 손절 시 청산 후 120분 차단</b>. 미판정 거래는 안전하게 그날 남은 재진입 차단.<br>
익절·손절은 수수료·슬리피지를 포함한 <b>순손익 목표가 아니라 가격 변동 기준</b>입니다. 당일 신호의 미래 5분봉으로 결과를 판정하며 같은 봉 익절·손절 동시 터치는 손절 우선입니다.</div>
<p class="hint">주의: 익절 목표를 높이는 것 자체는 중복 신호를 줄이지 않습니다. 이번 실험은 <b>목표가 변경과 잠금 규칙이 결합</b>되어 있어 두 효과를 분리할 수 없습니다. A와 기존 C5의 비교는 잠금 효과를 보여주지만 신호 수가 달라 거래당 평균만으로 성능을 단정할 수 없습니다.</p></section>
<section class="panel"><h2>검증 설정</h2><div class="controls">
<label class="field">검증 날짜 (한국시간)<input id="date" type="date"></label>
<label class="field">편도 수수료 (%)<input id="fee" type="number" value="0.04" min="0" max="0.5" step="0.01"></label>
<label class="field">편도 슬리피지 (%)<input id="slip" type="number" value="0.05" min="0" max="0.5" step="0.01"></label>
</div>
<div class="actions"><button id="run">전체 KRW 비교 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">이 검증만 초기화</button><button id="csv" class="secondary" disabled>V11.14.18 상세 CSV 저장</button></div>
<progress id="progress" max="1" value="0"></progress><div id="status" class="status">검사 전</div></section>
<section class="panel"><h2>검사 진행 상황</h2><div id="coverage" class="stats">검사 전</div></section>
<section class="panel"><h2>4개 전략 비교</h2><p class="hint">전체 검사 후 공개합니다. 승률은 실제 판정 거래 중 순손익이 플러스인 비율입니다.</p><div id="summary" class="scroll">전체 검사 완료 후 공개</div></section>
<section class="panel"><h2>중복 BUY 차단 원인</h2><p class="hint">차단된 후보 수는 실제 거래가 아니며, 가상 손익은 참고용입니다. 차단한 수익 후보가 얼마나 있는지도 기록합니다.</p><div id="blockers" class="scroll">전체 검사 완료 후 공개</div></section>
<section class="panel"><h2>종목별 성적</h2><div id="markets" class="scroll">전체 검사 완료 후 공개</div></section>
<p class="hint">현재 KRW 상장 종목 기준의 과거 모의실험입니다. 거래 체결 가능성, 상장폐지 종목, API 봉 누락을 완벽히 반영할 수 없습니다. UNKNOWN 거래는 수익/손실에 넣지 않습니다. 청산 후 120분이 다음 날짜로 넘어가는 상태는 연계하지 않는 <b>하루 단위 독립 실험</b>입니다. 실전 전용 로직은 변경하지 않았습니다.</p>
</main>
<script>
'use strict';
const API='/api/winrate14c5lock',KEY='bithumb_v111418_c5_lock_checkpoint_v1';
const $=id=>document.getElementById(id);
const STRATS=[['base','기존 C5 · 1.5%'],['a','A · 1.5% + 잠금'],['b','B · 3.0% + 잠금'],['c','C · 4.0% + 잠금']];
let state=null,running=false,stopRequested=false,storeFailed=false;
const escapeHtml=s=>String(s??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const asNumber=v=>(v===null||v===undefined||v==='')?NaN:Number(v);
const pct=v=>Number.isFinite(v)?v.toFixed(3)+'%':'—';
function kstDate(d){const p=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(d);const g=x=>p.find(t=>t.type===x).value;return `${g('year')}-${g('month')}-${g('day')}`;}
const yesterday=()=>kstDate(new Date(Date.now()-86400000));
$('date').max=yesterday();$('date').value=yesterday();
function config(){const date=$('date').value,fee=asNumber($('fee').value),slip=asNumber($('slip').value);if(!/^\d{4}-\d{2}-\d{2}$/.test(date)||date>yesterday())throw Error('완료된 과거 날짜만 선택해줘');if(!Number.isFinite(fee)||!Number.isFinite(slip)||fee<0||fee>0.5||slip<0||slip>0.5)throw Error('편도 비용은 0~0.5% 범위만 가능');return {date,fee,slip};}
const signature=c=>JSON.stringify([c.date,c.fee,c.slip]);
function load(){try{const s=JSON.parse(localStorage.getItem(KEY)||'null');if(!s||s.version!==1||!s.config||!Array.isArray(s.markets)||!s.markets.length||!s.results||typeof s.results!=='object'||s.markets.some(m=>!/^KRW-[A-Z0-9]+$/.test(m))||new Set(s.markets).size!==s.markets.length)return null;return s;}catch{return null;}}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function records(){return state?state.markets.filter(m=>Object.prototype.hasOwnProperty.call(state.results,m)).map(m=>state.results[m]):[];}
function finished(){return !!(state&&state.markets.length&&records().length===state.markets.length&&state.failed===0);}
function events(rs,key){return rs.flatMap(r=>r.arms[key].events.map(e=>({...e,market:r.market})));}
function stat(evs){const r={n:evs.length,unknown:0,judged:0,wins:0,tp:0,sl:0,tm:0,total:0,avg:NaN,rate:NaN};for(const e of evs){if(e.outcome==='UNKNOWN'){r.unknown++;continue;}const net=asNumber(e.net_pct);if(!Number.isFinite(net))continue;r.judged++;r.total+=net;if(net>0)r.wins++;if(e.outcome==='TP')r.tp++;else if(e.outcome==='SL')r.sl++;else if(e.outcome==='TIME')r.tm++;}if(r.judged){r.avg=r.total/r.judged;r.rate=100*r.wins/r.judged;}return r;}
function table(rs){let body='';for(const [key,label] of STRATS){const s=stat(events(rs,key));body+=`<tr><td>${escapeHtml(label)}</td><td>${s.n}</td><td>${s.judged}</td><td>${s.unknown}</td><td>${s.wins}</td><td>${pct(s.rate)}</td><td>${pct(s.avg)}</td><td>${s.tp} / ${s.sl} / ${s.tm}</td></tr>`;}return '<table><thead><tr><th>전략</th><th>BUY</th><th>판정</th><th>미판정</th><th>수익 거래</th><th>승률</th><th>거래당 순손익</th><th>TP / SL / TIME</th></tr></thead><tbody>'+body+'</tbody></table>';}
function card(k,v){return `<div class="stat"><small>${escapeHtml(k)}</small><strong>${escapeHtml(v)}</strong></div>`;}
function blockers(rs){let html='<table><thead><tr><th>실험</th><th>차단 후보</th><th>포지션 중</th><th>손절 후 120분</th><th>동일 상승 흐름</th><th>미판정 잠금</th><th>차단 후보 중 수익 가능*</th><th>가상 판정 가능</th></tr></thead><tbody>';for(const [key,label] of STRATS.slice(1)){const r={SAME_WAVE:0,POSITION_OPEN:0,POST_SL_120M:0,UNKNOWN_EXIT:0};let total=0,positive=0,judged=0;for(const m of rs){const a=m.arms[key];total+=a.blocked_count;positive+=a.blocked_profitable;judged+=a.blocked_judged;for(const reason of Object.keys(r))r[reason]+=(a.blocked_reasons[reason]||0);}html+=`<tr><td>${escapeHtml(label)}</td><td>${total}</td><td>${r.POSITION_OPEN}</td><td>${r.POST_SL_120M}</td><td>${r.SAME_WAVE}</td><td>${r.UNKNOWN_EXIT}</td><td>${positive}</td><td>${judged}</td></tr>`;}return html+'</tbody></table><p class="hint">*차단 당시 강제 진입했다고 가정한 해당 실험의 가상 플러스 손익 건수. 실제 놓친 확정 이익이 아닙니다. 잠금의 효과는 수익 가능성만으로 판단하지 않습니다.</p>';}
function markets(rs){const selected=rs.filter(r=>STRATS.some(([k])=>r.arms[k].signals>0));return selected.length?'<table><thead><tr><th>종목</th><th>원시 C5 후보</th><th>기존 C5</th><th>A BUY / 평균</th><th>B BUY / 평균</th><th>C BUY / 평균</th></tr></thead><tbody>'+selected.sort((a,b)=>a.market.localeCompare(b.market)).map(r=>{const val=k=>{const s=stat(r.arms[k].events);return s.n+' / '+pct(s.avg);};return `<tr><td>${escapeHtml(r.market)}</td><td>${r.raw_candidates}</td><td>${val('base')}</td><td>${val('a')}</td><td>${val('b')}</td><td>${val('c')}</td></tr>`;}).join('')+'</tbody></table>':'신호 없음';}
function paint(){const rs=records(),n=rs.length,total=state?.markets.length||0,full=finished();$('progress').max=total||1;$('progress').value=n;$('csv').disabled=!full;$('coverage').innerHTML=card('검사 종목',`${n} / ${total}`)+card('연속봉 분석 가능 종목',String(rs.filter(r=>r.valid_windows>0).length))+card('원시 C5 후보',String(rs.reduce((s,r)=>s+r.raw_candidates,0)))+card('기존 C5 BUY',String(rs.reduce((s,r)=>s+r.arms.base.signals,0)));if(full){$('summary').innerHTML=table(rs);$('blockers').innerHTML=blockers(rs);$('markets').innerHTML=markets(rs);}else{for(const id of ['summary','blockers','markets'])$(id).textContent='전체 검사 완료 후 공개';}$('status').textContent=(full?'✅ 전체 KRW 검사 완료':running?'⏳ 검사 진행 중':'검사 대기 또는 일시 중지')+` · ${n}/${total}`+(state?.failed?' · ⚠️ 일부 검사 실패 (이어하기로 재시도)':'')+(full?'\n이 결과는 과거 모의 성적이며 실전 수익 보장이 아닙니다.':'\n최종 성적은 검사 완료 후 공개합니다.');}
async function fetchJson(url,options){const ac=new AbortController(),t=setTimeout(()=>ac.abort(),115000);try{const response=await fetch(url,{...options,cache:'no-store',signal:ac.signal});const data=await response.json();if(!response.ok||!data.ok)throw Error(data.error||'HTTP '+response.status);return data;}finally{clearTimeout(t);}}
async function init(cfg){const u=await fetchJson(API+'/universe');if(!Array.isArray(u.items)||!u.items.length)throw Error('빗썸 KRW 종목 목록을 불러올 수 없음');state={version:1,config:cfg,markets:u.items,results:{},failed:0};save();}
function validResponse(data,market){if(data.scanned!==1||data.errors!==0||!Array.isArray(data.results)||data.results.length!==1)return false;const r=data.results[0];if(r.market!==market||!Number.isInteger(r.raw_candidates)||r.raw_candidates<0||!Number.isInteger(r.valid_windows)||r.valid_windows<0||!r.arms)return false;for(const [k] of STRATS){const a=r.arms[k];if(!a||!Number.isInteger(a.signals)||a.signals<0||!Number.isInteger(a.unknown)||a.unknown<0||!Array.isArray(a.events)||a.events.length!==a.signals||a.events.filter(e=>e.outcome==='UNKNOWN').length!==a.unknown||a.events.some(e=>!['TP','SL','TIME','UNKNOWN'].includes(e.outcome)||typeof e.signal_time_kst!=='string'||typeof e.entry_time_kst!=='string'))return false;if(k!=='base'&&(!Number.isInteger(a.blocked_count)||!Array.isArray(a.blocked_events)||a.blocked_events.length!==a.blocked_count||a.signals+a.blocked_count!==r.raw_candidates))return false;}return true;}
async function run(){if(running)return;let cfg;try{cfg=config();}catch(e){$('status').textContent='⚠️ '+e.message;return;}if(state&&signature(cfg)!==signature(state.config)){if(!confirm('검사 설정이 달라졌어. 이 실험 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);}running=true;stopRequested=false;storeFailed=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;try{if(!state)await init(cfg);state.failed=0;save();paint();const pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m));let cursor=0,failed=0;async function worker(){while(!stopRequested&&!storeFailed){const market=pending[cursor++];if(!market)return;let ok=false;for(let tries=0;tries<3&&!stopRequested;tries++){try{const data=await fetchJson(API+'/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:[market]})});if(!validResponse(data,market))throw Error('응답 데이터 검증 실패');state.results[market]=data.results[0];try{save();}catch(e){storeFailed=true;$('status').textContent='⚠️ 기기 저장공간 부족 · 검사를 중단합니다.';break;}ok=true;paint();break;}catch(e){if(tries===2){failed++;$('status').textContent='⚠️ '+market+': '+String(e.message||e);}else await new Promise(resolve=>setTimeout(resolve,1000*(tries+1)));}}if(!ok&&failed>=4){stopRequested=true;break;}}}await Promise.all([worker(),worker()]);state.failed=failed;save();paint();if(stopRequested)$('status').textContent+='\n일시 중지됨 · 다시 시작하면 남은 종목만 검사합니다.';}catch(e){$('status').textContent='⚠️ '+String(e.message||e);}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}}
$('run').onclick=run;$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;};$('reset').onclick=()=>{if(running)return;if(!confirm('V11.14.18 검사 기록만 삭제할까?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(x){let s=String(x??'');if(/^[\s]*[=+@]/.test(s)||(/^[\s]*-/.test(s)&&!/^[\s]*-\d+(?:\.\d+)?\s*$/.test(s)))s="'"+s;return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!finished())return;const cols=['version','date','market','strategy','tp_pct','sl_pct','horizon_minutes','post_sl_exclusion_minutes','fee_pct_side','slip_pct_side','observed_5m','valid_windows','truncated','raw_candidates','strategy_signals','strategy_unknown','strategy_blocked','status','block_reason','signal_time_kst','entry_time_kst','wave_id','is_first_signal','outcome','reason','net_pct','entry','exit','minutes','hypothetical_outcome','hypothetical_net_pct','ma5_prev','ma5_signal','ma5_slope_pct','prev_close_vs_ma5_pct','reclaim_vs_ma5_pct'];const lines=[cols.join(',')];for(const r of records())for(const [key,label] of STRATS){const a=r.arms[key],tp=key==='base'||key==='a'?1.5:key==='b'?3:4,pre={version:'V11.14.18',date:state.config.date,market:r.market,strategy:key.toUpperCase(),tp_pct:tp,sl_pct:1,horizon_minutes:60,post_sl_exclusion_minutes:key==='base'?0:120,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip,observed_5m:r.observed_5m,valid_windows:r.valid_windows,truncated:r.truncated?1:0,raw_candidates:r.raw_candidates,strategy_signals:a.signals,strategy_unknown:a.unknown,strategy_blocked:a.blocked_count||0};const seq=[...a.events.map(e=>({...e,status:'TAKEN'})),...(a.blocked_events||[]).map(e=>({...e,status:'BLOCKED'}))];for(const ev of (seq.length?seq:[{status:'NO_SIGNAL'}])){const item={...pre,...ev,...(ev.features||{})};lines.push(cols.map(c=>csvCell(item[c])).join(','));}}const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download=`bithumb_C5_lock_exit_${state.config.date}_v111418.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(u),2000);};
state=load();if(state){for(const key of ['date','fee','slip'])$(key).value=state.config[key];}paint();
</script></body></html>
""".replace('/*INSERT_PREVIOUS_STYLE*/', C5_V111417_PAGE.split('<style>', 1)[1].split('</style>', 1)[0])


def register_c5lock_v111418_lab(app, deps):
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache = {'markets': None, 'at': 0}
    mutex = Lock()

    def listed():
        with mutex:
            if cache['markets'] is None or time.time() - cache['at'] > 600:
                names = sorted({m for m in get_markets()
                                if isinstance(m, str) and MARKET_RE.fullmatch(m)})
                if not names:
                    raise ValueError('현재 KRW 종목 목록이 없습니다')
                cache.update(markets=names, at=time.time())
            return cache['markets']

    @app.get('/backtest-winrate-c5-lock')
    def page_c5lock_v111418():
        return render_template_string(C5LOCK_V111418_PAGE)

    @app.get('/api/winrate14c5lock/universe')
    def universe_c5lock_v111418():
        try:
            names = listed()
            return jsonify({'ok': True, 'items': names, 'count': len(names),
                            'rule_version': 'V11.14.18',
                            'universe_type': 'currently_listed_KRW'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400

    @app.post('/api/winrate14c5lock/evaluate')
    def evaluate_c5lock_v111418():
        try:
            body = request.get_json(silent=True)
            cfg = c5lock_v111418_config(body, kst)
            markets = body.get('markets') if isinstance(body, dict) else None
            if (not isinstance(markets, list) or not 1 <= len(markets) <= 2
                    or len(set(markets)) != len(markets)
                    or any(not isinstance(m, str) or not MARKET_RE.fullmatch(m)
                           for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 검사할 수 있습니다')
            names = set(listed())
            if any(m not in names for m in markets):
                raise ValueError('현재 KRW 마켓 목록에 없는 종목입니다')
            results, errors = [], []
            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks = {pool.submit(c5lock_v111418_evaluate_symbol,
                                     api_get, m, cfg, kst): m for m in markets}
                for future in as_completed(tasks):
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        errors.append({'market': tasks[future], 'error': str(exc)[:150]})
            return jsonify({'ok': not errors, 'scanned': len(markets),
                            'results': results, 'errors': len(errors),
                            'error_markets': errors, 'rule_version': 'V11.14.18'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400


_v111417_register_daytrade_review = register_daytrade_review


def register_daytrade_review(app, deps):
    _v111417_register_daytrade_review(app, deps)
    register_c5lock_v111418_lab(app, deps)

# ---------------------------------------------------------------------------
# V11.14.19 - Historical C / C5 pre-entry pattern explorer.
# APPEND-ONLY: retains live BUY, all previous labs and exact C/C5 signals.
# All explanatory features are calculated from bars at/before the SIGNAL.
# The NEXT candle and later prices are used ONLY for the original outcome.
# ---------------------------------------------------------------------------

PATTERNS_V111419_FEATURES = (
    ('pre_30m_return_pct', 'BUY 신호 전 30분 종가 변화 (%)'),
    ('pre_60m_return_pct', 'BUY 신호 전 60분 종가 변화 (%)'),
    ('pre_60m_range_pct', '신호 전 60분 고저폭 (%)'),
    ('pre_peak_distance_pct', '신호봉 종가의 직전 60분 고점 대비 (%)'),
    ('pre_6_bull_count', '직전 6봉 양봉 개수'),
    ('pre_volume_6v6_ratio', '직전 6봉 거래량 / 앞선 6봉 거래량'),
    ('ma5_slope_pct', '신호 확정 시 5이평 1봉 기울기 (%)'),
    ('ma20_slope_30m_pct', '신호 확정 시 20이평 30분 기울기 (%)'),
    ('ma5_vs_ma20_pct', '신호 확정 시 5이평-20이평 간격 (%)'),
    ('close_vs_ma20_pct', '신호봉 종가의 20이평 대비 위치 (%)'),
    ('signal_body_pct', '신호봉 양봉 몸통 크기 (%)'),
    ('signal_volume_ratio', '신호봉 거래량 / 이전 20봉 평균'),
    ('signal_upper_wick_pct', '신호봉 윗꼬리 비중 (%)'),
    ('signal_range_pct', '신호봉 전체 고저폭 (%)'),
    ('prior_close_vs_ma5_pct', '직전 봉 종가의 직전 5이평 대비 (%)'),
)


def patterns_v111419_features(rows, i):
    """Snapshot uses observed candles at or BEFORE confirmed signal i.

    Pre-30/60m features EXCLUDE the signal candle. No rows after i are read.
    Missing/non-consecutive lookbacks produce null, never invented candles.
    """
    if i < 25 or not lab_contiguous(rows, i - 24, i):
        raise ValueError('과거 5분봉 연속성이 충분하지 않습니다')
    current = rows[i]
    pre = rows[i - 12:i]  # 12 completed candles strictly BEFORE signal
    close_before = rows[i - 1]['c']
    prev_ma5 = sum(r['c'] for r in rows[i - 5:i]) / 5
    ma5 = sum(r['c'] for r in rows[i - 4:i + 1]) / 5
    ma20 = sum(r['c'] for r in rows[i - 19:i + 1]) / 20
    v20 = sum(r['v'] for r in rows[i - 20:i]) / 20
    old_6v = sum(r['v'] for r in rows[i - 12:i - 6])
    new_6v = sum(r['v'] for r in rows[i - 6:i])
    hi, lo = max(r['h'] for r in pre), min(r['l'] for r in pre)
    span = current['h'] - current['l']

    def r(v):
        return round(v, 5) if isinstance(v, (float, int)) and isfinite(v) else None

    # 20MA 30-minute slope needs one extra bar before the standard 25-bar
    # eligible window. Do NOT treat a gap as a valid 30-minute trend.
    old_ma20 = None
    if lab_contiguous(rows, i - 25, i):
        old_ma20 = sum(row['c'] for row in rows[i - 25:i - 5]) / 20

    return {
        'pre_30m_return_pct': r((close_before / rows[i - 7]['c'] - 1) * 100),
        'pre_60m_return_pct': r((close_before / rows[i - 13]['c'] - 1) * 100),
        'pre_60m_range_pct': r((hi / lo - 1) * 100),
        'pre_peak_distance_pct': r((current['c'] / hi - 1) * 100),
        'pre_6_bull_count': sum(bar['c'] > bar['o'] for bar in rows[i - 6:i]),
        'pre_volume_6v6_ratio': r(new_6v / old_6v) if old_6v > 0 else None,
        'ma5_slope_pct': r((ma5 / prev_ma5 - 1) * 100),
        'ma20_slope_30m_pct': r((ma20 / old_ma20 - 1) * 100) if old_ma20 else None,
        'ma5_vs_ma20_pct': r((ma5 / ma20 - 1) * 100),
        'close_vs_ma20_pct': r((current['c'] / ma20 - 1) * 100),
        'signal_body_pct': r((current['c'] / current['o'] - 1) * 100),
        'signal_volume_ratio': r(current['v'] / v20) if v20 > 0 else None,
        'signal_upper_wick_pct': r((current['h'] - max(current['c'], current['o'])) / span * 100) if span > 0 else None,
        'signal_range_pct': r((current['h'] / current['l'] - 1) * 100),
        'prior_close_vs_ma5_pct': r((close_before / prev_ma5 - 1) * 100),
    }


def patterns_v111419_evaluate_symbol(api_get, market, cfg, kst):
    """Augment exact V11.14.17 C/C5 signals without changing their results."""
    captured = {}

    def capture5(path, params):
        raw = api_get(path, params)
        if path == '/v1/candles/minutes/5' and isinstance(raw, list):
            for bar in raw:
                if not isinstance(bar, dict):
                    continue
                try:
                    t = local_time(bar['candle_date_time_kst'], kst)
                    o, hi, lo, c = [float(bar[k]) for k in (
                        'opening_price', 'high_price', 'low_price', 'trade_price')]
                    vol = float(bar['candle_acc_trade_volume'])
                    if (all(isfinite(x) and x > 0 for x in (o, hi, lo, c))
                            and isfinite(vol) and vol >= 0
                            and lo <= min(o, c) and hi >= max(o, c)):
                        captured[t] = {'t': t, 'o': o, 'h': hi, 'l': lo, 'c': c, 'v': vol}
                except (KeyError, ValueError, TypeError, OverflowError):
                    continue
        return raw

    baseline = c5_v111417_evaluate_symbol(capture5, market, cfg, kst)
    ordered = [captured[t] for t in sorted(captured)]
    indexes = {row['t']: i for i, row in enumerate(ordered)}
    arms = {}
    for strategy in ('c', 'c5'):
        original = baseline[strategy]
        feature_events = []
        for original_event in original['events']:
            signal_time = local_time(original_event['signal_time_kst'], kst)
            if signal_time not in indexes:
                raise ValueError('C/C5 신호와 과거 봉 연결에 실패했습니다')
            i = indexes[signal_time]
            # Important: features never inspect rows[i+1:] (next candle).
            feat = patterns_v111419_features(ordered, i)
            outcome = original_event['outcome']
            net = original_event.get('net_pct')
            if outcome == 'TP':
                group = 'TP'
            elif outcome == 'SL':
                group = 'SL'
            elif outcome == 'TIME':
                group = 'TIME_PROFIT' if net is not None and net > 0 else 'TIME_LOSS'
            else:
                group = 'UNKNOWN'
            feature_events.append({
                **{k: v for k, v in original_event.items() if k != 'features'},
                'exit_group': group, 'features': feat,
            })
        if len(feature_events) != original['signals'] or sum(
            e['exit_group'] == 'UNKNOWN' for e in feature_events) != original['unknown']:
            raise ValueError('기존 C/C5 신호 수 또는 미판정 수가 변경되었습니다')
        arms[strategy] = {
            'signals': original['signals'], 'unknown': original['unknown'],
            'events': feature_events,
        }
    return {
        'market': market, 'observed_5m': baseline['observed_5m'],
        'valid_windows': baseline['valid_windows'], 'truncated': baseline['truncated'],
        'c': arms['c'], 'c5': arms['c5'],
    }


PATTERNS_V111419_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.19 · C·C5 익절·손절 직전 차트 공통 패턴 분석실</title>
<style>/*INSERT_PREVIOUS_STYLE*/
select{font:inherit;color:var(--fg);background:#111a2e;border:1px solid var(--line);border-radius:8px;padding:9px;max-width:100%}
.legend{font-size:12px;color:var(--muted);margin:4px 0 10px}.good{color:var(--good)}.bad{color:var(--bad)}.em{font-weight:750}.wide{width:100%}.small{font-size:12px}.sectionhead{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.sectionhead h2{margin-right:auto}.risk{border-left:3px solid #ff9a93;padding:8px 10px;background:#1d2135;margin-top:10px}.compact td{padding:7px 6px}
</style></head><body><main>
<h1>🔍 V11.14.19 · BUY 이전 공통 패턴 분석실</h1>
<p class="muted">빗썸 KRW 과거 5분봉 · 기존 C/C5 신호 보존 · 실전 BUY 스캐너 무변경</p>
<section class="panel"><h2>무엇을 분석하나요?</h2>
<div class="note">기존 <b>C와 C5의 동일한 신호·익절·손절 결과</b>를 재사용합니다. BUY 직전의 30·60분 상승률, 고점과의 거리, 5·20이평, 반등 양봉, 거래량·변동폭을 기록하고 <b>익절(TP) / 손절(SL) / 시간 종료 수익 / 시간 종료 손실</b>로 비교합니다.<br>
<b>BUY 이후 가격은 청산 결과 판정에만 사용</b>합니다. 조건 탐색에는 이용하지 않습니다. 새로운 BUY 필터를 적용하거나 실전 신호를 바꾸지 않습니다.</div>
<p class="hint">기본 익절 +1.5%, 손절 -1.0%, 최대 60분, 종목당 전략별 60분 신호 간격. 표의 '시간 종료 수익'은 TP 목표 미달이지만 거래비용 이후 순수익이 플러스인 경우입니다.</p>
</section>
<section class="panel"><h2>검증 설정</h2><div class="controls">
<label class="field">검증 날짜 · 한국시간<input id="date" type="date"></label>
<label class="field">편도 수수료 (%)<input id="fee" type="number" value="0.04" min="0" max="0.5" step="0.01"></label>
<label class="field">편도 슬리피지 (%)<input id="slip" type="number" value="0.05" min="0" max="0.5" step="0.01"></label></div>
<div class="actions"><button id="run">전체 KRW 패턴 분석 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">이 검증만 초기화</button><button id="csv" class="secondary" disabled>전체 신호·특징 CSV 저장</button></div>
<progress id="progress" max="1" value="0"></progress><div id="status" class="status">검사 전</div></section>
<section class="panel"><h2>분석 범위 · 데이터 품질</h2><div id="coverage" class="stats">검사 전</div><p class="hint">연속된 캔들이 부족한 종목과 미판정 거래는 구분합니다. 현재 상장된 KRW 종목으로 과거를 검사하므로 상장폐지 종목은 빠질 수 있습니다.</p></section>
<section class="panel"><h2>1. 기존 C · C5 성적 확인</h2><p class="hint">전체 종목 검사가 끝나면 공개합니다. 이전 V11.14.17의 동일 날짜·동일 비용 결과와 숫자가 같아야 합니다.</p><div class="scroll" id="summary">전체 검사 완료 후 공개</div></section>
<section class="panel"><div class="sectionhead"><h2>2. 익절·손절 거래의 특징 비교</h2><label class="field">전략 선택<select id="strategy"><option value="c">기존 C</option><option value="c5">5이평 C5</option></select></label></div>
<p class="hint">각 분류의 <b>중앙값</b>을 비교합니다. 중앙값이 비슷하면 해당 특징 하나로는 익절과 손절을 구별하기 어렵다는 뜻입니다. '미판정'은 특징 비교에서 제외합니다.</p>
<div id="groups" class="stats">전체 검사 완료 후 공개</div><div id="medians" class="scroll">전체 검사 완료 후 공개</div></section>
<section class="panel"><h2>3. 수익·손실과 관련된 특징 후보</h2><p class="hint">분석 전에 고정한 가격·거래량 구간을 사용합니다. <b>표본 20건 이상</b>인 구간만 표시하며, 각 구간을 제외한 <b>나머지 같은 전략 신호</b>와 비교합니다. 평균 순손익 차이가 커도 여러 날짜에서 반복 검증되지 않으면 실전 필터로 사용하면 안 됩니다.</p>
<h3>상대적으로 유리했던 구간 (탐색용)</h3><div id="top" class="scroll">전체 검사 완료 후 공개</div>
<h3>상대적으로 불리했던 구간 (탐색용)</h3><div id="bottom" class="scroll">전체 검사 완료 후 공개</div>
<p class="risk small">⚠️ 가장 좋아 보이는 구간을 과거 자료에서 골라낸 결과라 우연·과최적화 가능성이 있습니다. 다른 날짜에서도 방향이 같은지 확인하고, 그 이후 새 날짜를 독립 검증해야 합니다.</p></section>
<section class="panel"><h2>4. 익절·손절 신호 사례</h2><p class="hint">실제 가격 대신 주요 특징을 나란히 보기 위한 표입니다. 특정 종목을 BUY 추천하는 기능이 아닙니다.</p><div id="examples" class="scroll">전체 검사 완료 후 공개</div></section>
<p class="hint">미래 가격을 알 수 없는 실제 체결과는 다른 5분봉 OHLC 백테스트입니다. 같은 봉에서 TP/SL 동시 도달 시 손절 우선, 진입/청산 전 가격 공백은 미판정입니다. 수수료·슬리피지 가정 포함, 실전 수익 보장 아님.</p>
</main><script>
'use strict';
const API='/api/winrate14patterns', KEY='bithumb_v111419_preentry_patterns_v1';
const $=id=>document.getElementById(id);
let state=null,running=false,stopRequested=false,storeFailed=false;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=v=>(v===null||v===undefined||v==='')?NaN:Number(v);
const pct=v=>Number.isFinite(v)?v.toFixed(3)+'%':'—';
const val=v=>Number.isFinite(v)?v.toFixed(3):'—';
const FEATURES=[
 ['pre_30m_return_pct','직전 30분 변화','%'],
 ['pre_60m_return_pct','직전 60분 변화','%'],
 ['pre_60m_range_pct','직전 60분 변동폭','%'],
 ['pre_peak_distance_pct','직전 60분 고점 대비','%'],
 ['pre_6_bull_count','직전 6봉 양봉 수','개'],
 ['pre_volume_6v6_ratio','직전 6봉/앞선 6봉 거래량','배'],
 ['ma5_slope_pct','5이평 1봉 기울기','%'],
 ['ma20_slope_30m_pct','20이평 30분 기울기','%'],
 ['ma5_vs_ma20_pct','5이평-20이평 간격','%'],
 ['close_vs_ma20_pct','신호 종가/20이평','%'],
 ['signal_body_pct','신호봉 몸통','%'],
 ['signal_volume_ratio','신호봉 거래량 배율','배'],
 ['signal_upper_wick_pct','신호봉 윗꼬리 비중','%'],
 ['signal_range_pct','신호봉 고저폭','%'],
 ['prior_close_vs_ma5_pct','직전 봉/5이평','%'],
];
// All exploratory bin cutoffs are pre-specified; no automatic tuning.
const BINS=[
 ['pre_60m_return_pct','진입 전 60분 변화',[-1,1],['-1% 미만','-1%~+1%','+1% 이상']],
 ['pre_30m_return_pct','진입 전 30분 변화',[-0.5,0.5],['-0.5% 미만','-0.5%~+0.5%','+0.5% 이상']],
 ['pre_peak_distance_pct','직전 고점 대비',[-1,-0.3],['-1% 미만','-1%~-0.3%','-0.3% 이상']],
 ['pre_60m_range_pct','직전 60분 변동폭',[1,3],['1% 미만','1%~3%','3% 이상']],
 ['ma20_slope_30m_pct','20이평 30분 변화',[0,0.3],['하락','0~+0.3%','+0.3% 이상']],
 ['ma5_vs_ma20_pct','5이평 대 20이평',[0,0.4],['5이평이 아래','근접 0~0.4%','5이평이 위 +0.4% 이상']],
 ['close_vs_ma20_pct','종가 대 20이평',[0,0.5],['20이평 아래','0~+0.5%','+0.5% 이상']],
 ['signal_body_pct','신호봉 몸통',[0.25,1],['0.25% 미만','0.25%~1%','1% 이상']],
 ['signal_volume_ratio','신호봉 거래량 배율',[1,2],['1배 미만','1~2배','2배 이상']],
 ['pre_6_bull_count','직전 6봉 양봉',[3,5],['0~2개','3~4개','5~6개']],
 ['prior_close_vs_ma5_pct','직전 봉/5이평',[-0.35,-0.06],['-0.35% 미만','-0.35%~-0.06%','-0.06% 이상']],
];
function kstDate(d){const v=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(d),get=x=>v.find(p=>p.type===x).value;return `${get('year')}-${get('month')}-${get('day')}`;}
const yesterday=()=>kstDate(new Date(Date.now()-86400000));
$('date').max=yesterday();$('date').value=yesterday();
function config(){const date=$('date').value,fee=num($('fee').value),slip=num($('slip').value);if(!/^\d{4}-\d{2}-\d{2}$/.test(date)||date>yesterday())throw Error('종료된 과거 날짜를 선택해줘');if(!Number.isFinite(fee)||!Number.isFinite(slip)||fee<0||fee>0.5||slip<0||slip>0.5)throw Error('비용은 0~0.5%여야 해');return {date,fee,slip};}
const signature=c=>JSON.stringify([c.date,c.fee,c.slip]);
function load(){try{const s=JSON.parse(localStorage.getItem(KEY)||'null');if(!s||s.version!==1||!s.config||!Array.isArray(s.markets)||!s.markets.length||!s.results||typeof s.results!=='object'||new Set(s.markets).size!==s.markets.length||s.markets.some(m=>!/^KRW-[A-Z0-9]+$/.test(m)))return null;return s;}catch{return null;}}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function records(){return state?state.markets.filter(m=>Object.prototype.hasOwnProperty.call(state.results,m)).map(m=>state.results[m]):[];}
function finished(){return !!(state&&state.markets.length&&records().length===state.markets.length&&state.failed===0);}
function events(rs,k){const out=[];for(const r of rs)for(const e of r[k].events)out.push({...e,market:r.market});return out;}
function stat(evs){let signals=evs.length,unknown=0,judged=0,wins=0,tp=0,sl=0,timeProfit=0,timeLoss=0,total=0;for(const e of evs){if(e.outcome==='UNKNOWN'){unknown++;continue;}const x=num(e.net_pct);if(!Number.isFinite(x))continue;judged++;total+=x;if(x>0)wins++;if(e.outcome==='TP')tp++;if(e.outcome==='SL')sl++;if(e.outcome==='TIME'){if(x>0)timeProfit++;else timeLoss++;}}return {signals,unknown,judged,wins,tp,sl,timeProfit,timeLoss,avg:judged?total/judged:NaN,winrate:judged?100*wins/judged:NaN,tprate:judged?100*tp/judged:NaN,slrate:judged?100*sl/judged:NaN};}
function card(k,v){return `<div class="stat"><small>${esc(k)}</small><strong>${esc(v)}</strong></div>`;}
function median(values){const s=values.map(num).filter(Number.isFinite).sort((a,b)=>a-b);if(!s.length)return NaN;const m=Math.floor(s.length/2);return s.length%2?s[m]:(s[m-1]+s[m])/2;}
function row(labels){return '<tr>'+labels.map(v=>`<td>${esc(v)}</td>`).join('')+'</tr>';}
function makeTable(headers,body){return '<table class="compact"><thead><tr>'+headers.map((h,i)=>`<th>${esc(h)}</th>`).join('')+'</tr></thead><tbody>'+body+'</tbody></table>';}
function summaryTable(ce,c5e){const a=stat(ce),b=stat(c5e);const rows=[['발생 신호',a.signals,b.signals],['판정 완료',a.judged,b.judged],['미판정',a.unknown,b.unknown],['익절(TP)',a.tp,b.tp],['손절(SL)',a.sl,b.sl],['시간 종료 수익',a.timeProfit,b.timeProfit],['시간 종료 손실',a.timeLoss,b.timeLoss],['전체 수익 거래',a.wins,b.wins],['수익 거래 비율',pct(a.winrate),pct(b.winrate)],['평균 순손익',pct(a.avg),pct(b.avg)]];return makeTable(['항목','기존 C','5이평 C5'],rows.map(row).join(''));}
function classifiedGroup(e){return e.exit_group;}
function renderGroups(evs){const keys=[['TP','익절(TP)'],['SL','손절(SL)'],['TIME_PROFIT','시간종료 수익'],['TIME_LOSS','시간종료 손실']];return keys.map(([key,label])=>card(label,evs.filter(e=>classifiedGroup(e)===key).length+'건')).join('');}
function renderMedians(evs){const groups=['TP','SL','TIME_PROFIT','TIME_LOSS'];const rows=FEATURES.map(([k,label,unit])=>row([label,...groups.map(g=>{const e=evs.filter(x=>x.exit_group===g),values=e.map(x=>x.features?.[k]).filter(x=>Number.isFinite(num(x)));const mid=median(values);return `${val(mid)}${Number.isFinite(mid)?unit:''} (${values.length}건)`;})])).join('');return makeTable(['BUY 이전 특징','익절 중앙값','손절 중앙값','시간수익 중앙값','시간손실 중앙값'],rows);}
function patternGroups(evs){const judged=evs.filter(e=>e.outcome!=='UNKNOWN'&&Number.isFinite(num(e.net_pct)));const results=[];for(const [field,label,cuts,names] of BINS){for(let band=0;band<3;band++){const inside=judged.filter(e=>{const x=num(e.features?.[field]);return Number.isFinite(x)&&(band===0?x<cuts[0]:band===1?x>=cuts[0]&&x<cuts[1]:x>=cuts[1]);});const outside=judged.filter(e=>{const x=num(e.features?.[field]);return Number.isFinite(x)&&!(band===0?x<cuts[0]:band===1?x>=cuts[0]&&x<cuts[1]:x>=cuts[1]);});if(inside.length<20||outside.length<20)continue;const s=stat(inside),o=stat(outside);results.push({feature:label,condition:names[band],n:s.judged,other:o.judged,avg:s.avg,otherAvg:o.avg,delta:s.avg-o.avg,tp:s.tprate,sl:s.slrate,otherTP:o.tprate,otherSL:o.slrate});}}return results;}
function renderPatterns(items){if(!items.length)return '<p class="hint">표본 20건 이상인 비교 구간이 아직 없습니다.</p>';return makeTable(['특징','조건','조건/나머지 건수','조건 평균','나머지 평균','차이(%p)','익절률 조건/나머지','손절률 조건/나머지'],items.map(p=>row([p.feature,p.condition,p.n+' / '+p.other,pct(p.avg),pct(p.otherAvg),pct(p.delta),pct(p.tp)+' / '+pct(p.otherTP),pct(p.sl)+' / '+pct(p.otherSL)])).join(''));}
function renderExamples(evs){const tp=evs.filter(e=>e.outcome==='TP').slice(0,8),sl=evs.filter(e=>e.outcome==='SL').slice(0,8);const group=(name,es)=>'<h3>'+esc(name)+'</h3>'+(!es.length?'<p class="hint">사례 없음</p>':makeTable(['종목','진입시각','순손익','직전 60분','고점 대비','신호봉 몸통','20이평 30분'],es.map(e=>row([e.market,e.entry_time_kst,pct(num(e.net_pct)),pct(num(e.features?.pre_60m_return_pct)),pct(num(e.features?.pre_peak_distance_pct)),pct(num(e.features?.signal_body_pct)),pct(num(e.features?.ma20_slope_30m_pct))])).join('')));return group('익절 사례 (앞선 8건)',tp)+group('손절 사례 (앞선 8건)',sl);}
function paint(){const rs=records(),count=rs.length,total=state?.markets.length||0,full=finished();$('progress').max=total||1;$('progress').value=count;$('csv').disabled=!full;const ce=events(rs,'c'),c5e=events(rs,'c5');$('coverage').innerHTML=card('검사 완료 종목',`${count} / ${total}`)+card('연속 봉 분석 가능',`${rs.filter(r=>r.valid_windows>0).length}개`)+card('기존 C 신호',`${ce.length}건`)+card('신규 C5 신호',`${c5e.length}건`)+card('미판정 (C / C5)',`${stat(ce).unknown} / ${stat(c5e).unknown}`)+card('원본 봉 페이지 제한',`${rs.filter(r=>r.truncated).length}종목`);
if(!full){for(const id of ['summary','groups','medians','top','bottom','examples'])$(id).textContent='전체 검사 완료 후 공개';}
else{$('summary').innerHTML=summaryTable(ce,c5e);paintAnalysis();}
$('status').textContent=(full?'✅ 전체 KRW 패턴 분석 완료':running?'⏳ 과거 5분봉 패턴 분석 중':'검사 대기 또는 일시 중지')+` · ${count}/${total}종목`+(state?.failed?' · ⚠️ 요청 오류: 이어하기로 누락 종목 재검사':'')+(full?'\n결과는 탐색적 연관성일 뿐 실전 승률 검증이 아닙니다.':'\n최종 패턴·승률·평균 손익은 전체 검사가 끝나면 공개합니다.');}
function paintAnalysis(){if(!finished())return;const rs=records(),evs=events(rs,$('strategy').value);$('groups').innerHTML=renderGroups(evs);$('medians').innerHTML=renderMedians(evs);const sorted=patternGroups(evs).sort((a,b)=>b.delta-a.delta);$('top').innerHTML=renderPatterns(sorted.slice(0,10));$('bottom').innerHTML=renderPatterns([...sorted].reverse().slice(0,10));$('examples').innerHTML=renderExamples(evs);}
$('strategy').onchange=paintAnalysis;
async function fetchJson(url,options){const ac=new AbortController(),timer=setTimeout(()=>ac.abort(),115000);try{const response=await fetch(url,{...options,cache:'no-store',signal:ac.signal}),data=await response.json();if(!response.ok||!data.ok)throw Error(data.error||'HTTP '+response.status);return data;}finally{clearTimeout(timer);}}
async function init(c){const u=await fetchJson(API+'/universe');if(!Array.isArray(u.items)||!u.items.length)throw Error('KRW 종목 목록 조회 실패');state={version:1,config:c,markets:u.items,results:{},failed:0};save();}
function validResponse(data,market){if(data.scanned!==1||data.errors!==0||!Array.isArray(data.results)||data.results.length!==1)return false;const r=data.results[0];if(r.market!==market||!r.c||!r.c5||!Number.isInteger(r.valid_windows)||r.valid_windows<0)return false;for(const arm of [r.c,r.c5]){if(!Number.isInteger(arm.signals)||arm.signals<0||!Number.isInteger(arm.unknown)||arm.unknown<0||!Array.isArray(arm.events)||arm.events.length!==arm.signals||arm.events.filter(e=>e.outcome==='UNKNOWN').length!==arm.unknown||arm.events.some(e=>!['TP','SL','TIME','UNKNOWN'].includes(e.outcome)||typeof e.signal_time_kst!=='string'||typeof e.entry_time_kst!=='string'||!e.features||typeof e.features!=='object'))return false;}return true;}
async function run(){if(running)return;let cfg;try{cfg=config();}catch(e){$('status').textContent='⚠️ '+e.message;return;}if(state&&signature(cfg)!==signature(state.config)){if(!confirm('검증 설정이 달라졌어. V11.14.19 기록만 초기화하고 다시 시작할까?'))return;state=null;localStorage.removeItem(KEY);}running=true;stopRequested=false;storeFailed=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;
try{if(!state)await init(cfg);state.failed=0;save();paint();const pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m));let next=0,failed=0;async function worker(){while(!stopRequested&&!storeFailed){const market=pending[next++];if(!market)return;let passed=false;for(let tries=0;tries<3&&!stopRequested;tries++){try{const data=await fetchJson(API+'/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:[market]})});if(!validResponse(data,market))throw Error('검사 응답 형식 오류');state.results[market]=data.results[0];try{save();}catch(e){storeFailed=true;$('status').textContent='⚠️ 브라우저 저장공간 부족 · 검사 중지';break;}passed=true;paint();break;}catch(e){if(tries===2){failed++;$('status').textContent='⚠️ '+market+': '+String(e.message||e);}else await new Promise(resolve=>setTimeout(resolve,1000*(tries+1)));}}if(!passed&&failed>=4){stopRequested=true;break;}}}await Promise.all([worker(),worker()]);state.failed=failed;try{save();}catch{}paint();if(stopRequested)$('status').textContent+='\n일시 중지됨 · 시작 버튼으로 남은 종목 이어서 검사 가능';}catch(e){$('status').textContent='⚠️ '+String(e.message||e);}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}}
$('run').onclick=run;$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;};$('reset').onclick=()=>{if(running)return;if(!confirm('V11.14.19 검사 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(x){let s=String(x??'');if(/^[\s]*[=+@]/.test(s)||(/^[\s]*-/.test(s)&&!/^[\s]*-\d+(?:\.\d+)?\s*$/.test(s)))s="'"+s;return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!finished())return;const featureNames=FEATURES.map(x=>x[0]);const columns=['version','date','market','strategy','tp_pct','sl_pct','horizon_minutes','fee_pct_side','slip_pct_side','observed_5m','valid_windows','truncated','strategy_signals','strategy_unknown','signal_time_kst','entry_time_kst','is_first_signal','outcome','exit_group','reason','net_pct','entry','exit','minutes',...featureNames];const lines=[columns.join(',')];for(const r of records())for(const key of ['c','c5']){const a=r[key],pre={version:'V11.14.19',date:state.config.date,market:r.market,strategy:key.toUpperCase(),tp_pct:1.5,sl_pct:1,horizon_minutes:60,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip,observed_5m:r.observed_5m,valid_windows:r.valid_windows,truncated:r.truncated?1:0,strategy_signals:a.signals,strategy_unknown:a.unknown};for(const ev of a.events.length?a.events:[{}]){const item={...pre,...ev,...(ev.features||{})};lines.push(columns.map(c=>csvCell(item[c])).join(','));}}const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`bithumb_C_C5_preentry_patterns_${state.config.date}_v111419.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);};
state=load();if(state)for(const key of ['date','fee','slip'])$(key).value=state.config[key];paint();
</script></body></html>
'''.replace('/*INSERT_PREVIOUS_STYLE*/', C5_V111417_PAGE.split('<style>', 1)[1].split('</style>', 1)[0])


def register_patterns_v111419_lab(app, deps):
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache = {'markets': None, 'at': 0}
    mutex = Lock()

    def listed():
        with mutex:
            if cache['markets'] is None or time.time() - cache['at'] > 600:
                names = sorted({m for m in get_markets()
                                if isinstance(m, str) and MARKET_RE.fullmatch(m)})
                if not names:
                    raise ValueError('현재 KRW 종목 목록이 없습니다')
                cache.update(markets=names, at=time.time())
            return cache['markets']

    @app.get('/backtest-winrate-patterns')
    def patterns_v111419_page():
        return render_template_string(PATTERNS_V111419_PAGE)

    @app.get('/api/winrate14patterns/universe')
    def patterns_v111419_universe():
        try:
            names = listed()
            return jsonify({'ok': True, 'items': names, 'count': len(names),
                            'rule_version': 'V11.14.19',
                            'universe_type': 'currently_listed_KRW'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400

    @app.post('/api/winrate14patterns/evaluate')
    def patterns_v111419_evaluate():
        try:
            raw = request.get_json(silent=True)
            cfg = c5_v111417_config(raw, kst)
            markets = raw.get('markets') if isinstance(raw, dict) else None
            if (not isinstance(markets, list) or not 1 <= len(markets) <= 2
                    or len(set(markets)) != len(markets)
                    or any(not isinstance(m, str) or not MARKET_RE.fullmatch(m)
                           for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 검사할 수 있습니다')
            names = set(listed())
            if any(m not in names for m in markets):
                raise ValueError('현재 KRW 종목 목록에 없는 종목입니다')
            results, errors = [], []
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = {pool.submit(patterns_v111419_evaluate_symbol,
                                    api_get, m, cfg, kst): m for m in markets}
                for f in as_completed(jobs):
                    try:
                        results.append(f.result())
                    except Exception as exc:
                        errors.append({'market': jobs[f], 'error': str(exc)[:150]})
            return jsonify({'ok': not errors, 'scanned': len(markets),
                            'results': results, 'errors': len(errors),
                            'error_markets': errors, 'rule_version': 'V11.14.19'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400


_v111418_register_daytrade_review = register_daytrade_review


def register_daytrade_review(app, deps):
    _v111418_register_daytrade_review(app, deps)
    register_patterns_v111419_lab(app, deps)


# ---------------------------------------------------------------------------
# V11.14.20: C5 pre-entry RISK FILTER OUT-OF-SAMPLE comparison lab.
# Append-only: all previous live and research routes remain unchanged.
# Preselected research-only thresholds from 2026-11-10/11 exploration:
#   - prior close <= prior SMA5 * (1 - 0.0037)  (deep pullback)
#   - signal close > signal SMA20 * (1 + 0.015) (chasing extended price)
# These numbers were discovered on Nov 10/11; those days are IN-SAMPLE.
# Filter only the EXISTING V11.14.17 C5 signal calendar. Do not change
# its 60min signal cooldown, entry time, outcomes, or future opportunity set.
# Every rejected signal retains its ORIGINAL hypothetical outcome so
# forgone TP / gains as well as avoided SL can be audited explicitly.
# ---------------------------------------------------------------------------

C5RISK_V111420_MA5_MIN_PCT = -0.37
C5RISK_V111420_MA20_MAX_PCT = 1.50
C5RISK_V111420_RULES = (
    ('base', '기존 C5 · 필터 없음'),
    ('deep', '5이평 깊은 이탈만 제외'),
    ('chase', '20이평 과도한 이격만 제외'),
    ('both', '두 조건 모두 제외'),
)


def c5risk_v111420_rejections(features):
    """Only pre-entry features, never outcome or a future candle."""
    if not isinstance(features, dict):
        return None
    d = features.get('prior_close_vs_ma5_pct')
    m = features.get('close_vs_ma20_pct')
    if (not isinstance(d, (int, float)) or not isfinite(d)
            or not isinstance(m, (int, float)) or not isfinite(m)):
        return None
    return (
        d <= C5RISK_V111420_MA5_MIN_PCT,
        m > C5RISK_V111420_MA20_MAX_PCT,
    )


def c5risk_v111420_filter_events(original):
    """Partition the UNCHANGED original C5 events; no rerun/cooldown edits.

    Each arm preserves original event timestamps and original outcomes exactly.
    A blocked event remains available for 'missed TP / avoided SL' audits,
    but never contributes to the accepted trades' P&L.
    """
    if not isinstance(original, dict) or not isinstance(original.get('events'), list):
        raise ValueError('C5 원본 거래 기록 형식 오류')
    base = original['events']
    arms = {}
    for key, label in C5RISK_V111420_RULES:
        accepted, blocked = [], []
        for event in base:
            feature_flags = c5risk_v111420_rejections(event.get('features'))
            if key == 'base':
                block_reason = ''
            elif feature_flags is None:
                block_reason = 'FEATURE_UNAVAILABLE'
            else:
                deep, chase = feature_flags
                reasons = []
                if key in ('deep', 'both') and deep:
                    reasons.append('DEEP_MA5')
                if key in ('chase', 'both') and chase:
                    reasons.append('EXTENDED_MA20')
                block_reason = '+'.join(reasons)
            ev = {**event, 'filter_reason': block_reason,
                  'status': 'BLOCKED' if block_reason else 'ACCEPTED'}
            if block_reason:
                blocked.append(ev)
            else:
                accepted.append(ev)
        if len(accepted) + len(blocked) != len(base):
            raise ValueError('기존 C5 원본 BUY 후보 수가 변경되었습니다')
        arms[key] = {
            'label': label, 'signals': len(accepted),
            'unknown': sum(e['outcome'] == 'UNKNOWN' for e in accepted),
            'blocked_count': len(blocked),
            'blocked_unknown': sum(e['outcome'] == 'UNKNOWN' for e in blocked),
            'feature_missing': sum(e['filter_reason'] == 'FEATURE_UNAVAILABLE' for e in blocked),
            'events': accepted, 'blocked_events': blocked,
        }
    if (arms['base']['signals'] != original['signals']
            or arms['base']['unknown'] != original['unknown']
            or len(arms['base']['events']) != len(base)):
        raise ValueError('기존 C5 신호 수/미판정 수가 바뀌었습니다')
    return arms


def c5risk_v111420_evaluate_symbol(api_get, market, cfg, kst):
    # Calls exact V11.14.19 evaluator: retains its original C and C5 decisions,
    # and its strictly pre-entry measurements. One round of historical API calls.
    original = patterns_v111419_evaluate_symbol(api_get, market, cfg, kst)
    return {
        'market': original['market'],
        'observed_5m': original['observed_5m'],
        'valid_windows': original['valid_windows'],
        'truncated': original['truncated'],
        'c_reference_signals': original['c']['signals'],
        'c_reference_unknown': original['c']['unknown'],
        'original_c5_signals': original['c5']['signals'],
        'arms': c5risk_v111420_filter_events(original['c5']),
    }


C5RISK_V111420_PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.20 · C5 위험 패턴 제거 독립 검증</title>
<style>/*BASE_STYLE*/
.notice{background:#10182d;border:1px solid #415471;padding:13px;border-radius:10px;font-size:13px;line-height:1.8}
.warn{border-left:3px solid #ff9a93;padding:10px 12px;background:#212239;font-size:13px;line-height:1.7}
.pill{display:inline-block;border:1px solid #52617d;padding:2px 8px;border-radius:12px;font-size:12px;margin:4px 5px 0 0}
</style></head><body><main>
<h1>🔎 V11.14.20 · C5 위험 패턴 제거 검증실</h1>
<p class="muted">빗썸 KRW · 5분봉 · 과거 실험 전용 · 실전 BUY 스캐너 무변경</p>
<section class="panel"><h2>어제 발견한 위험 패턴을 검증해 보자</h2>
<div class="notice">
<b>기준: 기존 C5의 BUY 시간은 절대 바꾸지 않아.</b><br>
① <b>깊은 이탈</b>: 신호 직전 종가가 직전 5이평 대비 <b>-0.37% 이하</b>인 신호 제외<br>
② <b>과도한 이격</b>: 신호봉 종가가 당시 20이평 대비 <b>+1.50% 초과</b>인 신호 제외<br>
4가지 방식으로 <b>필터 없음 / ①만 / ②만 / ①+②</b> 비교.<br>
<b>익절 +1.5%, 손절 -1.0%, 최대 60분</b> 및 비용·진입 시점 모두 동일.</div>
<p class="hint">중요: 11월 10~11일은 조건을 발견한 <b>학습용 날짜</b>야. 결과가 좋더라도 증거가 아니고, 처음 보는 <b>11월 12일 이후</b>의 성능을 더 중요하게 봐야 해. 필터로 거절한 신호가 실제로는 익절했을 수도 있으므로 누락된 익절도 같이 세어.</p></section>
<section class="panel"><h2>검증 설정</h2><div class="controls">
<label class="field">검사 날짜 · 한국시간<input id="date" type="date"></label>
<label class="field">편도 수수료 (%)<input id="fee" type="number" value="0.04" min="0" max="0.5" step="0.01"></label>
<label class="field">편도 슬리피지 (%)<input id="slip" type="number" value="0.05" min="0" max="0.5" step="0.01"></label></div>
<div class="actions"><button id="run">전체 KRW 검사 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">이 검증만 초기화</button><button id="csv" class="secondary" disabled>전체 신호·차단 사유 CSV 저장</button></div>
<progress id="progress" max="1" value="0"></progress><div id="status" class="status">검사 전</div></section>
<section class="panel"><h2>1. 검사 범위와 데이터 품질</h2><div id="coverage" class="stats">검사 전</div>
<p class="hint">현재 상장된 KRW 종목의 과거 캔들을 사용하므로 상장폐지 종목은 누락될 수 있어. 충분한 연속 봉이 없는 종목, 과거 데이터 페이지 부족, 진입·청산 미판정은 따로 기록해.</p></section>
<section class="panel"><h2>2. 네 전략의 성적 비교</h2>
<div id="summary" class="scroll">전체 검사 완료 후 공개</div>
<p class="hint">각 전략은 <b>동일한 기존 C5 후보 목록</b>에서 신호를 필터링해. 차단 후 새로운 BUY를 채워 넣지 않아. 판정 완료된 거래만 평균 순손익에 포함하므로 미판정 수도 같이 확인해야 해.</p></section>
<section class="panel"><h2>3. 차단된 익절과 손절 — 놓친 기회까지 확인</h2>
<div id="blocked" class="scroll">전체 검사 완료 후 공개</div>
<p class="hint">차단된 결과는 기존 C5 신호를 그대로 가상 추적한 값이야. 차단된 익절·시간종료 수익은 기회 손실, 차단된 손절·시간종료 손실은 피한 손실 후보에 해당해. 서로 중복된 가상 거래도 있으니 실제 계좌 총수익으로 해석하면 안 돼.</p></section>
<section class="panel"><h2>4. 실제로 제외한 신호의 사례</h2><div id="examples" class="scroll">전체 검사 완료 후 공개</div>
<p class="hint">CSV에는 <b>ACCEPTED / BLOCKED</b>와 차단 사유, 원래의 TP·SL·TIME·UNKNOWN, 손익 및 BUY 이전 15가지 특징이 모두 들어 있어. 분석·검증 전용이며 실전 진입 추천이 아니야.</p></section>
<p class="warn">⚠️ 현재 테스트는 과거 OHLC 5분봉 시뮬레이션이야. 같은 봉에 익절과 손절이 동시에 닿으면 손절 우선 처리하고, 데이터 부족은 미판정으로 남겨. 과거 날짜에서 확인된 효과가 미래 수익을 보장하지 않아.</p>
</main><script>
'use strict';
const API='/api/winrate14c5risk',KEY='bithumb_v111420_c5_risk_filters_v1';
const $=id=>document.getElementById(id),esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=v=>(v===null||v===undefined||v==='')?NaN:Number(v), pct=v=>Number.isFinite(v)?v.toFixed(3)+'%':'—';
const STRATS=[['base','기존 C5'],['deep','5이평 깊은 이탈 제외'],['chase','20이평 이격 제외'],['both','두 조건 모두 제외']];
const FEATURES=['pre_30m_return_pct','pre_60m_return_pct','pre_60m_range_pct','pre_peak_distance_pct','pre_6_bull_count','pre_volume_6v6_ratio','ma5_slope_pct','ma20_slope_30m_pct','ma5_vs_ma20_pct','close_vs_ma20_pct','signal_body_pct','signal_volume_ratio','signal_upper_wick_pct','signal_range_pct','prior_close_vs_ma5_pct'];
let state=null,running=false,stopRequested=false,storeFailed=false;
function kstDate(d){const v=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(d),get=x=>v.find(p=>p.type===x).value;return `${get('year')}-${get('month')}-${get('day')}`;}
const yesterday=()=>kstDate(new Date(Date.now()-86400000));
$('date').max=yesterday();$('date').value=yesterday();
function config(){const date=$('date').value,fee=num($('fee').value),slip=num($('slip').value);if(!/^\d{4}-\d{2}-\d{2}$/.test(date)||date>yesterday())throw Error('완전히 종료된 과거 날짜만 선택해줘');if(!Number.isFinite(fee)||!Number.isFinite(slip)||fee<0||fee>0.5||slip<0||slip>0.5)throw Error('편도 비용은 각각 0~0.5%로 입력해줘');return {date,fee,slip};}
const signature=c=>JSON.stringify([c.date,c.fee,c.slip]);
function load(){try{const s=JSON.parse(localStorage.getItem(KEY)||'null');if(!s||s.version!==1||!s.config||!Array.isArray(s.markets)||!s.markets.length||!s.results||typeof s.results!=='object'||new Set(s.markets).size!==s.markets.length||s.markets.some(m=>!/^KRW-[A-Z0-9]+$/.test(m)))return null;return s;}catch{return null;}}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function records(){return state?state.markets.filter(m=>Object.prototype.hasOwnProperty.call(state.results,m)).map(m=>state.results[m]):[];}
function finished(){return !!(state&&state.markets.length&&records().length===state.markets.length&&state.failed===0);}
function events(rs,key,field='events'){const out=[];for(const r of rs){const a=r.arms[key];for(const ev of (a?.[field]||[]))out.push({...ev,market:r.market});}return out;}
function stats(evs){const judged=evs.filter(e=>e.outcome!=='UNKNOWN'&&Number.isFinite(num(e.net_pct))),n=judged.length,profit=judged.filter(e=>num(e.net_pct)>0).length;return {signals:evs.length,judged:n,unknown:evs.length-n,tp:judged.filter(e=>e.outcome==='TP').length,sl:judged.filter(e=>e.outcome==='SL').length,time:judged.filter(e=>e.outcome==='TIME').length,profit,loss:n-profit,winrate:n?100*profit/n:NaN,avg:n?judged.reduce((a,b)=>a+num(b.net_pct),0)/n:NaN};}
function row(cells){return '<tr>'+cells.map(v=>`<td>${esc(v)}</td>`).join('')+'</tr>';}
function makeTable(headers,body){return '<table><thead><tr>'+headers.map(h=>`<th>${esc(h)}</th>`).join('')+'</tr></thead><tbody>'+body+'</tbody></table>';}
function card(name,value){return `<div class="stat"><small>${esc(name)}</small><strong>${esc(value)}</strong></div>`;}
function paintSummary(rs){const baseline=stats(events(rs,'base'));let body='';for(const [key,name] of STRATS){const evs=events(rs,key),s=stats(evs),blocked=events(rs,key,'blocked_events');const d=Number.isFinite(s.avg)&&Number.isFinite(baseline.avg)?s.avg-baseline.avg:NaN;body+=row([name,s.signals,s.judged,s.unknown,s.profit,pct(s.winrate),pct(s.avg),pct(d),s.tp,s.sl,s.time,blocked.length]);}return makeTable(['전략','BUY','판정','미판정','수익 거래','수익 비율','평균 순손익','기존 대비(%p)','익절','손절','시간 종료','제외'],body);}
function paintBlocked(rs){let body='';for(const [key,name] of STRATS.slice(1)){const es=events(rs,key,'blocked_events'),s=stats(es),reason={DEEP_MA5:0,EXTENDED_MA20:0,FEATURE_UNAVAILABLE:0};for(const e of es)for(const k of Object.keys(reason))if(String(e.filter_reason||'').includes(k))reason[k]++;body+=row([name,es.length,s.judged,s.unknown,s.tp,s.sl,s.time,s.profit,pct(s.avg),reason.DEEP_MA5,reason.EXTENDED_MA20,reason.FEATURE_UNAVAILABLE]);}return makeTable(['전략','제외 신호','판정 가능','미판정','놓친 익절','피한 손절','시간종료','놓친 수익 거래','제외 신호 가상 평균','5이평 이유','20이평 이유','특징 부족'],body);}
function paintExamples(rs){const all=events(rs,'both','blocked_events'),examples=[...all.filter(e=>e.outcome==='TP').slice(0,6),...all.filter(e=>e.outcome==='SL').slice(0,6)];if(!examples.length)return '<p class="hint">제외된 익절·손절 사례 없음</p>';return makeTable(['종목','BUY 시각','원래 결과','원래 순손익','차단 이유','5이평 이탈','20이평 이격'],examples.map(e=>row([e.market,e.entry_time_kst,e.outcome,pct(num(e.net_pct)),e.filter_reason,pct(num(e.features?.prior_close_vs_ma5_pct)),pct(num(e.features?.close_vs_ma20_pct))])).join(''));}
function paint(){const rs=records(),total=state?.markets.length||0,full=finished();$('progress').max=total||1;$('progress').value=rs.length;$('csv').disabled=!full;const base=events(rs,'base');$('coverage').innerHTML=card('검사 종목',`${rs.length} / ${total}`)+card('5분봉 연속 구간 있음',`${rs.filter(r=>r.valid_windows>0).length}개`)+card('기존 C5 신호',`${base.length}건`)+card('C5 미판정',`${stats(base).unknown}건`)+card('과거 원본 봉 제한',`${rs.filter(r=>r.truncated).length}개`)+card('기존 C 참고 신호',`${rs.reduce((v,r)=>v+r.c_reference_signals,0)}건`);
if(full){$('summary').innerHTML=paintSummary(rs);$('blocked').innerHTML=paintBlocked(rs);$('examples').innerHTML=paintExamples(rs);}else for(const id of ['summary','blocked','examples'])$(id).textContent='전체 검사 완료 후 공개';
$('status').textContent=(full?'✅ 전체 검사 완료':running?'⏳ 과거 5분봉 조회 중':'검사 대기 또는 중단')+` · ${rs.length}/${total}종목`+(state?.failed?' · ⚠️ 일부 요청 실패: 이어하기로 재검사 가능':'')+(full?'\n이 자료는 가상 과거 성적이며, 기준 발견 날짜(11월 10~11일)와 별도로 해석해야 해.':'\n최종 성적은 전체 종목 검사 후 공개');}
async function fetchJson(url,options){const ac=new AbortController(),t=setTimeout(()=>ac.abort(),115000);try{const res=await fetch(url,{...options,cache:'no-store',signal:ac.signal}),data=await res.json();if(!res.ok||!data.ok)throw Error(data.error||'HTTP '+res.status);return data;}finally{clearTimeout(t);}}
async function init(c){const u=await fetchJson(API+'/universe');if(!Array.isArray(u.items)||!u.items.length)throw Error('KRW 종목 목록을 가져오지 못했습니다');state={version:1,config:c,markets:u.items,results:{},failed:0};save();}
function validResponse(data,market){if(data.scanned!==1||data.errors!==0||!Array.isArray(data.results)||data.results.length!==1)return false;const r=data.results[0];if(r.market!==market||!r.arms||!Number.isInteger(r.valid_windows)||r.valid_windows<0||!Number.isInteger(r.original_c5_signals))return false;const base=r.arms.base;if(!base||base.signals!==r.original_c5_signals)return false;for(const [key] of STRATS){const a=r.arms[key];if(!a||!Number.isInteger(a.signals)||!Number.isInteger(a.unknown)||!Number.isInteger(a.blocked_count)||!Array.isArray(a.events)||!Array.isArray(a.blocked_events)||a.events.length!==a.signals||a.blocked_events.length!==a.blocked_count||a.signals+a.blocked_count!==r.original_c5_signals||a.unknown!==a.events.filter(e=>e.outcome==='UNKNOWN').length)return false;if([...a.events,...a.blocked_events].some(e=>!['TP','SL','TIME','UNKNOWN'].includes(e.outcome)||!e.features||typeof e.features!=='object'||typeof e.signal_time_kst!=='string'||typeof e.entry_time_kst!=='string'))return false;}return true;}
async function run(){if(running)return;let cfg;try{cfg=config();}catch(e){$('status').textContent='⚠️ '+e.message;return;}if(state&&signature(cfg)!==signature(state.config)){if(!confirm('설정이 달라졌어. V11.14.20 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);}running=true;stopRequested=false;storeFailed=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;
try{if(!state)await init(cfg);state.failed=0;save();paint();const pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m));let next=0,failed=0;async function worker(){while(!stopRequested&&!storeFailed){const market=pending[next++];if(!market)return;let pass=false;for(let tries=0;tries<3&&!stopRequested;tries++){try{const d=await fetchJson(API+'/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:[market]})});if(!validResponse(d,market))throw Error('검사 결과 형식 오류');state.results[market]=d.results[0];try{save();}catch(e){storeFailed=true;delete state.results[market];$('status').textContent='⚠️ 브라우저 저장공간 부족 · 검사가 중지됐어';break;}pass=true;paint();break;}catch(e){if(tries===2){failed++;$('status').textContent='⚠️ '+market+': '+String(e.message||e);}else await new Promise(resolve=>setTimeout(resolve,1000*(tries+1)));}}if(!pass&&failed>=4){stopRequested=true;break;}}}await Promise.all([worker(),worker()]);state.failed=failed;try{save();}catch{}paint();if(stopRequested)$('status').textContent+='\n일시 중지됨 · 시작 버튼으로 남은 종목 이어서 검사 가능';}catch(e){$('status').textContent='⚠️ '+String(e.message||e);}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}}
$('run').onclick=run;$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;};$('reset').onclick=()=>{if(running)return;if(!confirm('V11.14.20 검사 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(x){let s=String(x??'');if(/^[\s]*[=+@]/.test(s)||(/^[\s]*-/.test(s)&&!/^[\s]*-\d+(?:\.\d+)?\s*$/.test(s)))s="'"+s;return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!finished())return;const cols=['version','date','market','strategy','status','filter_reason','tp_pct','sl_pct','horizon_minutes','fee_pct_side','slip_pct_side','observed_5m','valid_windows','truncated','original_c5_signals','strategy_signals','strategy_unknown','strategy_blocked','signal_time_kst','entry_time_kst','is_first_signal','outcome','exit_group','reason','net_pct','entry','exit','minutes',...FEATURES];const lines=[cols.join(',')];for(const r of records())for(const [key] of STRATS){const arm=r.arms[key],pre={version:'V11.14.20',date:state.config.date,market:r.market,strategy:key.toUpperCase(),tp_pct:1.5,sl_pct:1,horizon_minutes:60,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip,observed_5m:r.observed_5m,valid_windows:r.valid_windows,truncated:r.truncated?1:0,original_c5_signals:r.original_c5_signals,strategy_signals:arm.signals,strategy_unknown:arm.unknown,strategy_blocked:arm.blocked_count};const all=[...arm.events,...arm.blocked_events];for(const ev of (all.length?all:[{status:'NO_SIGNAL'}])){const item={...pre,...ev,...(ev.features||{})};lines.push(cols.map(c=>csvCell(item[c])).join(','));}}const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`bithumb_C5_risk_filters_${state.config.date}_v111420.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);};
state=load();if(state)for(const key of ['date','fee','slip'])$(key).value=state.config[key];paint();
</script></body></html>
""".replace('/*BASE_STYLE*/', C5_V111417_PAGE.split('<style>', 1)[1].split('</style>', 1)[0])


def register_c5risk_v111420_lab(app, deps):
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cache = {'markets': None, 'at': 0}
    mutex = Lock()

    def listed():
        with mutex:
            if cache['markets'] is None or time.time() - cache['at'] > 600:
                names = sorted({m for m in get_markets()
                                if isinstance(m, str) and MARKET_RE.fullmatch(m)})
                if not names:
                    raise ValueError('현재 KRW 종목 목록이 없습니다')
                cache.update(markets=names, at=time.time())
            return cache['markets']

    @app.get('/backtest-winrate-c5-risk')
    def c5risk_v111420_page():
        return render_template_string(C5RISK_V111420_PAGE)

    @app.get('/api/winrate14c5risk/universe')
    def c5risk_v111420_universe():
        try:
            names = listed()
            return jsonify({'ok': True, 'items': names, 'count': len(names),
                            'rule_version': 'V11.14.20',
                            'universe_type': 'currently_listed_KRW'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400

    @app.post('/api/winrate14c5risk/evaluate')
    def c5risk_v111420_evaluate():
        try:
            raw = request.get_json(silent=True)
            cfg = c5_v111417_config(raw, kst)
            markets = raw.get('markets') if isinstance(raw, dict) else None
            if (not isinstance(markets, list) or not 1 <= len(markets) <= 2
                    or len(set(markets)) != len(markets)
                    or any(not isinstance(m, str) or not MARKET_RE.fullmatch(m)
                           for m in markets)):
                raise ValueError('한 번에 1~2개 KRW 종목만 검사할 수 있습니다')
            names = set(listed())
            if any(m not in names for m in markets):
                raise ValueError('현재 KRW 종목 목록에 없는 종목입니다')
            results, errors = [], []
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = {pool.submit(c5risk_v111420_evaluate_symbol,
                                    api_get, m, cfg, kst): m for m in markets}
                for f in as_completed(jobs):
                    try:
                        results.append(f.result())
                    except Exception as exc:
                        errors.append({'market': jobs[f], 'error': str(exc)[:150]})
            return jsonify({'ok': not errors, 'scanned': len(markets),
                            'results': results, 'errors': len(errors),
                            'error_markets': errors, 'rule_version': 'V11.14.20'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400


_v111419_register_daytrade_review = register_daytrade_review


def register_daytrade_review(app, deps):
    _v111419_register_daytrade_review(app, deps)
    register_c5risk_v111420_lab(app, deps)


# ---------------------------------------------------------------------------
# V11.14.21 - C5 multi-timeframe trend + higher-low / breakout research lab.
# APPEND-ONLY. All previous routes and original C5 results remain untouched.
# Historical C5 entries, cooldown, exits and fee assumptions are inherited
# from V11.14.17 through the V11.14.19 pattern evaluator. This lab only
# applies 3 research filters to those SAME entry opportunities:
#     base / trend_15m / structure / both.
# Data used to classify an entry: completed 5m bars at or before its signal,
# never later bars; 15m bars require three contiguous completed 5m bars.
# ---------------------------------------------------------------------------

C5_TREND_V111421_RULES = (
    ('base', '기존 C5'),
    ('trend', 'C5 + 15분 상승'),
    ('structure', 'C5 + 상승 저점·고점 돌파'),
    ('both', 'C5 + 두 조건 모두'),
)


def c5_trend_v111421_gates(rows, i):
    """Compute fully pre-entry HTF and structure features for signal bar i.

    All calculations use rows[:i+1]. Each 15m candle is assembled ONLY from
    exact 00/05/10-minute starting 5m bars of its own 15m bucket and its
    timestamp plus 15 minutes must be <= the signal 5m candle CLOSE.
    Missing bars result in unknown, not inferred or filled history.
    """
    from collections import defaultdict

    if not 0 <= i < len(rows):
        raise ValueError('신호 인덱스 오류')
    upto = rows[:i + 1]
    signal_end = rows[i]['t'] + timedelta(minutes=5)
    buckets = defaultdict(dict)
    for bar in upto:
        t = bar['t']
        bucket = t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)
        offset = int((t - bucket).total_seconds() / 60)
        if offset in (0, 5, 10):
            buckets[bucket][offset] = bar

    fifteen = []
    for bucket, slots in sorted(buckets.items()):
        if bucket + timedelta(minutes=15) <= signal_end and set(slots) == {0, 5, 10}:
            fifteen.append((bucket, slots[10]['c']))

    trend_ok = None
    trend_close, trend_sma5, trend_slope_pct = None, None, None
    if len(fifteen) >= 6:
        recent = fifteen[-6:]
        latest_expected_close = signal_end.replace(
            minute=(signal_end.minute // 15) * 15, second=0, microsecond=0)
        if (recent[-1][0] + timedelta(minutes=15) == latest_expected_close
                and all(recent[j + 1][0] - recent[j][0] == timedelta(minutes=15)
                        for j in range(5))):
            prev_sma = sum(x[1] for x in recent[:-1]) / 5
            now_sma = sum(x[1] for x in recent[1:]) / 5
            trend_close = recent[-1][1]
            trend_sma5 = now_sma
            trend_slope_pct = (now_sma / prev_sma - 1) * 100
            trend_ok = bool(trend_close > now_sma and now_sma > prev_sma)

    structure_ok = None
    low_before, low_after, breakout_high = None, None, None
    if i >= 8 and lab_contiguous(rows, i - 8, i):
        low_before = min(x['l'] for x in rows[i - 8:i - 4])
        low_after = min(x['l'] for x in rows[i - 4:i])
        breakout_high = max(x['h'] for x in rows[i - 3:i])
        structure_ok = bool(low_after > low_before and rows[i]['c'] > breakout_high)

    def r(v):
        return round(v, 6) if isinstance(v, (int, float)) and isfinite(v) else None

    return {
        'trend_15m': trend_ok, 'structure': structure_ok,
        'trend_15m_close': r(trend_close), 'trend_15m_sma5': r(trend_sma5),
        'trend_15m_sma5_slope_pct': r(trend_slope_pct),
        'older_4_low': r(low_before), 'recent_4_low': r(low_after),
        'previous_3_high': r(breakout_high),
        'signal_close': r(rows[i]['c']),
    }


def c5_trend_v111421_evaluate_symbol(api_get, market, cfg, kst):
    """Reuse unchanged C5 candidates / outcomes; add only pre-entry gates."""
    captured = {}

    def capture5(path, params):
        raw = api_get(path, params)
        if path == '/v1/candles/minutes/5' and isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                try:
                    t = local_time(item['candle_date_time_kst'], kst)
                    op, hi, lo, close = [float(item[k]) for k in
                                         ('opening_price', 'high_price', 'low_price', 'trade_price')]
                    vol = float(item['candle_acc_trade_volume'])
                    if (all(isfinite(x) and x > 0 for x in (op, hi, lo, close))
                            and isfinite(vol) and vol >= 0
                            and lo <= min(op, close) and hi >= max(op, close)):
                        captured[t] = {'t': t, 'o': op, 'h': hi, 'l': lo, 'c': close, 'v': vol}
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
        return raw

    original = patterns_v111419_evaluate_symbol(capture5, market, cfg, kst)
    ordered = [captured[t] for t in sorted(captured)]
    positions = {row['t']: i for i, row in enumerate(ordered)}
    baseline = original['c5']
    events = []
    for event in baseline['events']:
        ts = local_time(event['signal_time_kst'], kst)
        if ts not in positions:
            raise ValueError('C5 신호봉을 과거 원본 데이터에 연결할 수 없습니다')
        gates = c5_trend_v111421_gates(ordered, positions[ts])
        events.append({
            'signal_time_kst': event['signal_time_kst'],
            'entry_time_kst': event['entry_time_kst'],
            'is_first_signal': event['is_first_signal'],
            'outcome': event['outcome'],
            'net_pct': event.get('net_pct'),
            'entry': event.get('entry'), 'exit': event.get('exit'),
            'minutes': event.get('minutes'), 'reason': event.get('reason'),
            'gates': gates,
        })
    if len(events) != baseline['signals'] or sum(
            e['outcome'] == 'UNKNOWN' for e in events) != baseline['unknown']:
        raise ValueError('C5 원본 신호 또는 결과가 변경되었습니다')
    return {
        'market': market, 'observed_5m': original['observed_5m'],
        'valid_windows': original['valid_windows'],
        'truncated': original['truncated'],
        'original_c5_signals': baseline['signals'],
        'original_c5_unknown': baseline['unknown'],
        'events': events,
    }


C5_TREND_V111421_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.21 · C5 + 15분 추세 / 상승 저점 검증실</title>
<style>/*REPLACE_STYLE*/
.note{border-radius:10px;padding:12px;background:#10182d;line-height:1.75;font-size:13px}
.notice{border-left:3px solid #ffca7a;padding:11px 12px;background:#252339;font-size:13px;line-height:1.75}
fieldset{border:0;padding:0;margin:0}.smaller{font-size:12px}
select{padding:10px;background:#111a2e;border:1px solid #35435e;color:#f3f6ff;border-radius:8px;width:100%;min-width:0;font:inherit}
</style></head><body><main>
<h1>🧪 V11.14.21 · C5 전문가 기법 비교</h1>
<p class="muted">빗썸 KRW 과거 5분봉 · 15분 추세 · 상승 저점 / 고점 돌파 · 기존 V2 스캐너 변경 없음</p>
<section class="panel"><h2>이번 실험의 기준</h2>
<div class="note"><b>① 기존 C5:</b> 직전 종가 5이평 아래 → 현재 양봉이 5이평 위 첫 회복 → 5이평 상승.<br>
<b>② C5 + 15분 상승:</b> 신호 확정 시점까지 <b>완성된 15분봉</b> 6개에서, 최근 종가가 15분봉 5이평 위이고 그 5이평도 상승 중.<br>
<b>③ C5 + 상승 저점·고점 돌파:</b> 신호 전 4개 봉의 최저가가 그 앞 4개 봉의 최저가보다 높고, 신호 종가가 이전 3개 봉의 최고가 위.<br>
<b>④ C5 + 두 조건 모두:</b> ②와 ③이 동시에 참인 C5만 통과.<br>
<b>모든 방식 공통:</b> 동일 C5 신호 목록과 60분 신호 제한·다음 봉 시가 진입·익절 +1.5% / 손절 -1.0% / 최대 60분 종료·편도 거래 비용.</div>
<p class="hint">신호와 청산 결과는 기존 V11.14.17/19 검증 코드에서 가져옵니다. 15분봉은 5분봉을 3개씩 묶되, 미완성 15분봉이나 누락된 5분봉이 있으면 사용하지 않습니다. <b>필터가 제외한 신호도 원래 손익을 유지</b>해 놓친 수익 기회도 함께 확인합니다.</p>
<div class="notice">⚠️ 이 실험은 필터 적용 전후의 <b>거래당 성과</b> 비교입니다. 신호를 제거하고 다른 BUY로 자동 대체하지 않습니다. 과거 OHLC의 동일 봉 익절·손절 동시 터치는 기존 코드대로 손절 우선입니다. 실전 수익이 보장되지는 않습니다.</div></section>
<section class="panel"><h2>검증 설정</h2>
<div class="controls">
<label class="field">검증 날짜 · 한국시간<input id="date" type="date"></label>
<label class="field">검사 범위<select id="scope"><option value="40">빠른 예비검증 · 40종목</option><option value="all">전체 KRW 종목</option></select></label>
<label class="field">편도 수수료 (%)<input id="fee" type="number" value="0.04" min="0" max="0.5" step="0.01"></label>
<label class="field">편도 슬리피지 (%)<input id="slip" type="number" value="0.05" min="0" max="0.5" step="0.01"></label>
</div>
<p class="hint">40종목 예비검증은 이름순 전체 목록에서 일정 간격으로 고른 <b>기능 점검용 부분 표본</b>이며 전체 시장의 대표 표본이 아닙니다. 전 종목 결과는 반드시 '전체 KRW 종목'으로 따로 검사해 주세요. 검사 날짜나 범위를 바꾸면 이 실험의 저장 기록이 초기화됩니다.</p>
<div class="actions"><button id="run">검사 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">이 실험만 초기화</button><button id="csv" class="secondary" disabled>전체 신호·필터 결과 CSV 저장</button></div>
<progress id="progress" max="1" value="0"></progress><div id="status" class="status">검사 전</div></section>
<section class="panel"><h2>1. 데이터 범위</h2><div id="coverage" class="stats">검사 전</div><p class="hint">현재 상장된 종목의 과거 데이터만 조회합니다. 상장폐지 종목, 충분한 과거 봉이 없는 종목은 누락될 수 있으며 미래 데이터는 신호 판정에 사용하지 않습니다.</p></section>
<section class="panel"><h2>2. C5 대비 전략별 성과</h2><div id="summary" class="scroll">검사 전</div><p class="hint">수익률은 수수료·슬리피지를 반영한 <b>거래당 평균 순손익</b>입니다. 판정 완료 신호만 수익 계산에 포함하고 미판정 건수는 별도로 표시합니다.</p></section>
<section class="panel"><h2>3. 필터가 거른 신호 결과</h2><div id="blocked" class="scroll">검사 전</div><p class="hint">원래 C5에서 수익이 났던 신호를 제외하면 기회도 함께 놓친 것입니다. 필터를 통과한 신호와 거른 신호의 결과를 모두 기록합니다.</p></section>
<section class="panel"><h2>4. 종목별 상세 비교</h2><div id="examples" class="scroll">검사 전</div><p class="hint">날짜가 달라지면 결과를 별도 CSV로 저장한 뒤 다음 날짜를 검사하세요. 11월 10~11일처럼 조건 탐색에 썼던 기간과 그 뒤의 새로운 기간을 구분하세요.</p></section>
</main><script>
'use strict';
const API='/api/winrate14c5trend',KEY='bithumb_v111421_c5_trend_structure_v1';
const $=id=>document.getElementById(id);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const asNum=v=>(v===null||v===undefined||v==='')?NaN:Number(v);
const pct=v=>Number.isFinite(v)?v.toFixed(3)+'%':'—';
const ARMS=[['base','기존 C5'],['trend','C5 + 15분 상승'],['structure','C5 + 상승 저점·고점 돌파'],['both','C5 + 두 조건 모두']];
let state=null,running=false,stopRequested=false,storeFailed=false;
function kstDate(d){const a=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(d),g=k=>a.find(x=>x.type===k).value;return `${g('year')}-${g('month')}-${g('day')}`;}
const yesterday=()=>kstDate(new Date(Date.now()-86400000));
$('date').max=yesterday();$('date').value=yesterday();
function config(){const date=$('date').value,fee=asNum($('fee').value),slip=asNum($('slip').value),scope=$('scope').value;if(!/^\d{4}-\d{2}-\d{2}$/.test(date)||date>yesterday())throw Error('완전히 종료된 과거 날짜를 선택해줘');if(!Number.isFinite(fee)||!Number.isFinite(slip)||fee<0||fee>0.5||slip<0||slip>0.5)throw Error('편도 비용은 0~0.5% 범위로 입력해줘');if(!['40','all'].includes(scope))throw Error('검사 범위 오류');return {date,fee,slip,scope};}
const signature=c=>JSON.stringify([c.date,c.fee,c.slip,c.scope]);
function load(){try{const s=JSON.parse(localStorage.getItem(KEY)||'null');if(!s||s.version!==1||!s.config||!Array.isArray(s.markets)||!s.markets.length||!s.results||typeof s.results!=='object'||new Set(s.markets).size!==s.markets.length||s.markets.some(m=>!/^KRW-[A-Z0-9]+$/.test(m)))return null;return s;}catch{return null;}}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function records(){return state?state.markets.filter(m=>Object.prototype.hasOwnProperty.call(state.results,m)).map(m=>state.results[m]):[];}
function finished(){return !!(state&&state.markets.length&&records().length===state.markets.length&&state.failed===0);}
function accepted(e,k){if(k==='base')return true;if(k==='trend')return e.gates.trend_15m===true;if(k==='structure')return e.gates.structure===true;return e.gates.trend_15m===true&&e.gates.structure===true;}
function events(rs,key,allow=true){const out=[];for(const r of rs)for(const ev of r.events)if(accepted(ev,key)===allow)out.push({...ev,market:r.market});return out;}
function stat(es){const judged=es.filter(e=>e.outcome!=='UNKNOWN'&&Number.isFinite(asNum(e.net_pct)));const n=judged.length,profits=judged.filter(e=>asNum(e.net_pct)>0).length;return {signals:es.length,judged:n,unknown:es.length-n,profit:profits,tp:judged.filter(e=>e.outcome==='TP').length,sl:judged.filter(e=>e.outcome==='SL').length,tm:judged.filter(e=>e.outcome==='TIME').length,rate:n?profits/n*100:NaN,avg:n?judged.reduce((s,e)=>s+asNum(e.net_pct),0)/n:NaN};}
function row(cells){return '<tr>'+cells.map(x=>`<td>${esc(x)}</td>`).join('')+'</tr>';}
function table(headers,body){return '<table><thead><tr>'+headers.map(x=>`<th>${esc(x)}</th>`).join('')+'</tr></thead><tbody>'+body+'</tbody></table>';}
function card(label,v){return `<div class="stat"><small>${esc(label)}</small><strong>${esc(v)}</strong></div>`;}
function paintSummary(rs){let body='';const b=stat(events(rs,'base'));for(const [k,name] of ARMS){const s=stat(events(rs,k)),delta=Number.isFinite(s.avg)&&Number.isFinite(b.avg)?s.avg-b.avg:NaN;body+=row([name,s.signals,s.judged,s.unknown,s.profit,pct(s.rate),pct(s.avg),pct(delta),`${s.tp} / ${s.sl} / ${s.tm}`,k==='base'?0:events(rs,k,false).length]);}return table(['전략','신호','판정','미판정','수익','수익 거래 비율','평균 순손익','기존 대비 %p','TP / SL / TIME','제외'],body);}
function paintBlocked(rs){let body='';for(const [k,name] of ARMS.slice(1)){const blocked=events(rs,k,false),s=stat(blocked),unknown=blocked.filter(e=>e.gates.trend_15m===null||e.gates.structure===null).length;body+=row([name,blocked.length,s.judged,s.unknown,s.tp,s.sl,s.profit,pct(s.avg),unknown]);}return table(['전략','제외 건수','제외 신호 판정','미판정','놓친 TP','제외한 SL','놓친 수익 거래','제외 신호 평균','조건 결측 포함'],body);}
function paintExamples(rs){const items=events(rs,'base').filter(e=>e.gates).sort((a,b)=>b.entry_time_kst.localeCompare(a.entry_time_kst)).slice(0,70);return items.length?table(['종목','C5 진입 시각','결과','순손익','15분 상승','상승저점·돌파','두 조건 통과'],items.map(e=>row([e.market,e.entry_time_kst,e.outcome,pct(asNum(e.net_pct)),e.gates.trend_15m===null?'자료부족':e.gates.trend_15m?'O':'X',e.gates.structure===null?'자료부족':e.gates.structure?'O':'X',accepted(e,'both')?'O':'X'])).join('')):'신호 없음';}
function paint(){const rs=records(),total=state?.markets.length||0,full=finished(),base=events(rs,'base');$('progress').max=total||1;$('progress').value=rs.length;$('csv').disabled=!full;$('coverage').innerHTML=card('검사 완료',`${rs.length} / ${total}`)+card('기존 C5 신호',String(base.length))+card('C5 판정 / 미판정',`${stat(base).judged} / ${stat(base).unknown}`)+card('15분 상승 통과',String(base.filter(e=>e.gates.trend_15m===true).length))+card('구조 통과',String(base.filter(e=>e.gates.structure===true).length))+card('원본 과거 봉 제한',String(rs.filter(r=>r.truncated).length));if(full){$('summary').innerHTML=paintSummary(rs);$('blocked').innerHTML=paintBlocked(rs);$('examples').innerHTML=paintExamples(rs);}else for(const id of ['summary','blocked','examples'])$(id).textContent='선택한 검사 범위 완료 후 공개';$('status').textContent=(full?'✅ 검증 완료':running?'⏳ 과거 기록 분석 중':'검사 대기 / 일시 중지')+` · ${rs.length}/${total}종목`+(state?.failed?' · 일부 API 오류, 이어하기 가능':'')+(full?`\n${state.config.scope==='40'?'⚠️ 40종목 예비 표본 결과이며 전체 시장 결론이 아닙니다.':'전 KRW 종목 검사 완료. 과거 모의 성적입니다.'}`:'\n전체 범위가 끝나면 전략별 결과를 공개합니다.');}
async function fetchJson(url,opts){const ac=new AbortController(),timer=setTimeout(()=>ac.abort(),115000);try{const res=await fetch(url,{...opts,cache:'no-store',signal:ac.signal});const data=await res.json();if(!res.ok||!data.ok)throw Error(data.error||'HTTP '+res.status);return data;}finally{clearTimeout(timer);}}
async function init(cfg){const d=await fetchJson(API+'/universe');if(!Array.isArray(d.items)||!d.items.length)throw Error('빗썸 KRW 목록을 불러오지 못했어');const all=d.items,markets=cfg.scope==='40'&&all.length>40?Array.from({length:40},(_,i)=>all[Math.floor((i+.5)*all.length/40)]):all;state={version:1,config:cfg,markets,results:{},failed:0};save();}
function validResponse(d,market){if(d.scanned!==1||d.errors!==0||!Array.isArray(d.results)||d.results.length!==1)return false;const r=d.results[0];return r.market===market&&Number.isInteger(r.original_c5_signals)&&Number.isInteger(r.original_c5_unknown)&&Array.isArray(r.events)&&r.events.length===r.original_c5_signals&&r.events.filter(e=>e.outcome==='UNKNOWN').length===r.original_c5_unknown&&r.events.every(e=>e.gates&&[true,false,null].includes(e.gates.trend_15m)&&[true,false,null].includes(e.gates.structure)&&['TP','SL','TIME','UNKNOWN'].includes(e.outcome)&&typeof e.signal_time_kst==='string'&&typeof e.entry_time_kst==='string');}
async function run(){if(running)return;let cfg;try{cfg=config();}catch(e){$('status').textContent='⚠️ '+e.message;return;}if(state&&signature(cfg)!==signature(state.config)){if(!confirm('검증 날짜/비용/범위가 달라졌어. 이 실험의 저장 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);}running=true;stopRequested=false;storeFailed=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;try{if(!state)await init(cfg);state.failed=0;save();paint();const pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m));let cursor=0,failed=0;async function worker(){while(!stopRequested&&!storeFailed){const market=pending[cursor++];if(!market)return;let pass=false;for(let tryN=0;tryN<3&&!stopRequested;tryN++){try{const data=await fetchJson(API+'/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:[market]})});if(!validResponse(data,market))throw Error('응답 구조 검증 실패');state.results[market]=data.results[0];try{save();}catch(e){delete state.results[market];storeFailed=true;$('status').textContent='⚠️ 휴대폰 저장공간 부족, 검사를 멈췄어';break;}pass=true;paint();break;}catch(e){if(tryN===2){failed++;$('status').textContent='⚠️ '+market+': '+String(e.message||e);}else await new Promise(resolve=>setTimeout(resolve,1000*(tryN+1)));}}if(!pass&&failed>=4){stopRequested=true;break;}}}await Promise.all([worker(),worker()]);state.failed=failed;try{save();}catch{}paint();if(stopRequested)$('status').textContent+='\n일시 중지: 다시 누르면 미완료 종목부터 계속 검사';}catch(e){$('status').textContent='⚠️ '+String(e.message||e);}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}}
$('run').onclick=run;$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;};$('reset').onclick=()=>{if(running)return;if(!confirm('V11.14.21 실험 진행 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(v){let s=String(v??'');if(/^[\s]*[=+@]/.test(s)||(/^[\s]*-/.test(s)&&!/^[\s]*-\d+(?:\.\d+)?\s*$/.test(s)))s="'"+s;return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!finished())return;const columns=['version','date','market','strategy','status','filter_reason','outcome','net_pct','signal_time_kst','entry_time_kst','is_first_signal','entry','exit','minutes','reason','trend_15m','structure','trend_15m_close','trend_15m_sma5','trend_15m_sma5_slope_pct','older_4_low','recent_4_low','previous_3_high','signal_close','fee_pct_side','slip_pct_side','tp_pct','sl_pct','horizon_minutes'];const lines=[columns.join(',')];for(const r of records())for(const [key] of ARMS){const pre={version:'V11.14.21',date:state.config.date,market:r.market,strategy:key,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip,tp_pct:1.5,sl_pct:1,horizon_minutes:60};for(const ev of r.events.length?r.events:[{status:'NO_SIGNAL'}]){const ok=ev.gates?accepted(ev,key):false,missing=ev.gates&&(key==='trend'&&ev.gates.trend_15m===null||key==='structure'&&ev.gates.structure===null||key==='both'&&(ev.gates.trend_15m===null||ev.gates.structure===null));const reason=ok?'':missing?'FEATURE_UNAVAILABLE':ev.gates?key==='both'?'TREND_OR_STRUCTURE_FAIL':key==='trend'?'TREND_FAIL':key==='structure'?'STRUCTURE_FAIL':'':'NO_SIGNAL';const item={...pre,...ev,...(ev.gates||{}),status:ev.status|| (ok?'ACCEPTED':'BLOCKED'),filter_reason:reason};lines.push(columns.map(k=>csvCell(item[k])).join(','));}}const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`bithumb_C5_trend_structure_${state.config.date}_v111421.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),2500);};
state=load();if(state){for(const key of ['date','fee','slip','scope'])$(key).value=state.config[key];}paint();
</script></body></html>
'''.replace('/*REPLACE_STYLE*/', C5_V111417_PAGE.split('<style>',1)[1].split('</style>',1)[0])


def register_c5_trend_v111421_lab(app, deps):
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    market_cache = {'markets': None, 'at': 0}
    mutex = Lock()

    def listed():
        with mutex:
            if market_cache['markets'] is None or time.time() - market_cache['at'] > 600:
                names = sorted({m for m in get_markets()
                                if isinstance(m, str) and MARKET_RE.fullmatch(m)})
                if not names:
                    raise ValueError('현재 KRW 종목 목록이 없습니다')
                market_cache.update(markets=names, at=time.time())
            return market_cache['markets']

    @app.get('/backtest-winrate-c5-trend')
    def c5_trend_v111421_page():
        return render_template_string(C5_TREND_V111421_PAGE)

    @app.get('/api/winrate14c5trend/universe')
    def c5_trend_v111421_universe():
        try:
            names = listed()
            return jsonify({'ok': True, 'items': names, 'count': len(names),
                            'rule_version': 'V11.14.21',
                            'universe_type': 'currently_listed_KRW'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400

    @app.post('/api/winrate14c5trend/evaluate')
    def c5_trend_v111421_evaluate():
        try:
            raw = request.get_json(silent=True)
            cfg = c5_v111417_config(raw, kst)
            markets = raw.get('markets') if isinstance(raw, dict) else None
            if (not isinstance(markets, list) or len(markets) != 1
                    or any(not isinstance(m, str) or not MARKET_RE.fullmatch(m)
                           for m in markets)):
                raise ValueError('한 번에 한 개 KRW 종목만 검증할 수 있습니다')
            if any(m not in set(listed()) for m in markets):
                raise ValueError('현재 KRW 목록에 없는 종목입니다')
            results, errors = [], []
            for m in markets:
                try:
                    results.append(c5_trend_v111421_evaluate_symbol(api_get, m, cfg, kst))
                except Exception as exc:
                    errors.append({'market': m, 'error': str(exc)[:150]})
            return jsonify({'ok': not errors, 'scanned': len(markets),
                            'results': results, 'errors': len(errors),
                            'error_markets': errors, 'rule_version': 'V11.14.21'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400


_v111420_register_daytrade_review = register_daytrade_review


def register_daytrade_review(app, deps):
    _v111420_register_daytrade_review(app, deps)
    register_c5_trend_v111421_lab(app, deps)

# ---------------------------------------------------------------------------
# V11.14.22 - C5 × two classic scalping concepts (RESEARCH ONLY).
# APPEND-ONLY: all V11.14.21 and older endpoints remain unchanged.
#  - Raschke-inspired: Wilder ADX(14) > 30 in +DI uptrend, ADX expansion
#    recently seen, EMA(20) pullback contact, then bar-close confirmation.
#  - Volman-inspired: six preceding 5m bars compress into <= 1.2% range,
#    EMA(20) near the box, then signal CLOSE breaks the range high.
# These are explicit mechanical filters on SAME existing C5 entry/exit events,
# NOT independent reproductions of the discretionary original strategies.
# Prices/indicators stop at the completed C5 signal bar; never use future bars.
# ---------------------------------------------------------------------------

C5_EXPERT_V111422_ARMS = (
    ('base', '기존 C5'),
    ('trend', 'C5 + 15분 상승 (기존)'),
    ('structure', 'C5 + 가격 구조 (기존)'),
    ('both', 'C5 + 기존 두 조건'),
    ('raschke', 'C5 + ADX/EMA20 눌림'),
    ('volman', 'C5 + 압축 후 돌파'),
)


def c5_expert_v111422_gates(rows, signal_i):
    """Mechanical retrospective classifications using no bars after signal_i.

    Unknown if there is not enough contiguous 5-minute history. For Wilder
    ADX(14), the first valid ADX corresponds to bar 27 (0-based), and the
    first rise comparison needs bar 28. EMA20 seeds with the first SMA20.
    """
    n_all = len(rows)
    if not 0 <= signal_i < n_all:
        raise ValueError('C5 신호 위치 오류')

    start = signal_i
    # Start from contiguous history only. Truncation keeps calculations fast;
    # EMA warmup remains fixed and deterministic, with a generous 200 bars.
    while (start > 0 and signal_i - start < 199 and
           rows[start]['t'] - rows[start-1]['t'] == timedelta(minutes=5)):
        start -= 1
    win = rows[start:signal_i+1]
    size = len(win)
    close = [b['c'] for b in win]
    high = [b['h'] for b in win]
    low = [b['l'] for b in win]
    ema = [None] * size
    if size >= 20:
        ema[19] = sum(close[:20]) / 20
        k = 2 / 21
        for j in range(20, size):
            ema[j] = ema[j-1] + k * (close[j] - ema[j-1])

    adx = [None] * size
    pdi = [None] * size
    mdi = [None] * size
    if size >= 28:
        tr, pdm, mdm = [0.] * size, [0.] * size, [0.] * size
        for j in range(1, size):
            up = high[j] - high[j-1]
            down = low[j-1] - low[j]
            pdm[j] = up if up > down and up > 0 else 0.
            mdm[j] = down if down > up and down > 0 else 0.
            tr[j] = max(high[j]-low[j], abs(high[j]-close[j-1]),
                        abs(low[j]-close[j-1]))
        # Wilder's 14-bar smoothed sums of TR, +DM, -DM.
        atr_sum, pos_sum, neg_sum = sum(tr[1:15]), sum(pdm[1:15]), sum(mdm[1:15])
        dx = [None] * size
        for j in range(14, size):
            if j > 14:
                atr_sum = atr_sum - atr_sum/14 + tr[j]
                pos_sum = pos_sum - pos_sum/14 + pdm[j]
                neg_sum = neg_sum - neg_sum/14 + mdm[j]
            pos = (100*pos_sum/atr_sum) if atr_sum > 0 else 0.
            neg = (100*neg_sum/atr_sum) if atr_sum > 0 else 0.
            pdi[j], mdi[j] = pos, neg
            dx[j] = 100*abs(pos-neg)/(pos+neg) if pos+neg > 0 else 0.
            if j == 27:
                adx[j] = sum(dx[14:28])/14
            elif j > 27:
                adx[j] = (adx[j-1]*13 + dx[j])/14

    raschke = None
    recent_adx_rise = None
    ema_touch_4 = None
    if size >= 30 and ema[-2] is not None and adx[-1] is not None:
        recent_adx_rise = any(
            adx[j] is not None and adx[j-1] is not None and
            adx[j] > 30 and adx[j] > adx[j-1]
            for j in range(max(28, size-7), size)
        )
        ema_touch_4 = any(
            ema[j] is not None and low[j] <= ema[j]*1.002 and
            high[j] >= ema[j]*0.998
            for j in range(max(0, size-5), size-1)
        )
        raschke = bool(
            adx[-1] > 30 and pdi[-1] > mdi[-1] and recent_adx_rise
            and ema_touch_4 and ema[-1] > ema[-2]
            and close[-1] > ema[-1] and close[-1] > high[-2]
        )

    volman = None
    squeeze_range_pct, squeeze_top, squeeze_bottom, ema_near_box = None, None, None, None
    if size >= 21 and ema[-2] is not None:
        prior = win[-7:-1]  # six completely closed bars BEFORE C5 signal
        squeeze_top = max(b['h'] for b in prior)
        squeeze_bottom = min(b['l'] for b in prior)
        reference = sum(b['c'] for b in prior)/6
        squeeze_range_pct = (squeeze_top-squeeze_bottom)/reference*100
        ema_near_box = bool(squeeze_bottom*0.997 <= ema[-2] <= squeeze_top*1.003)
        volman = bool(squeeze_range_pct <= 1.2 and ema_near_box
                      and close[-1] > squeeze_top and close[-1] > win[-1]['o'])

    def num(v):
        return round(v, 6) if v is not None and isfinite(v) else None

    return {
        'raschke': raschke, 'volman': volman,
        'adx14': num(adx[-1]), 'plus_di14': num(pdi[-1]),
        'minus_di14': num(mdi[-1]), 'ema20': num(ema[-1]),
        'adx_rise_recent': recent_adx_rise, 'ema_touch_4': ema_touch_4,
        'squeeze_range_pct': num(squeeze_range_pct),
        'squeeze_top': num(squeeze_top), 'squeeze_bottom': num(squeeze_bottom),
        'ema_near_box': ema_near_box,
    }


def c5_expert_v111422_evaluate_symbol(api_get, market, cfg, kst):
    """Reuse exact V11.14.21 C5 entries and exits; capture existing 5m data.

    This wrapper adds no extra upstream requests: the underlying evaluator
    makes its usual candle queries, whose responses are reused for new gates.
    """
    captured = {}

    def intercept(path, params):
        raw = api_get(path, params)
        if path == '/v1/candles/minutes/5' and isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                try:
                    t = local_time(item['candle_date_time_kst'], kst)
                    op, hi, lo, c = [float(item[k]) for k in
                                     ('opening_price', 'high_price', 'low_price', 'trade_price')]
                    v = float(item['candle_acc_trade_volume'])
                    if (all(isfinite(x) and x > 0 for x in (op, hi, lo, c))
                            and isfinite(v) and v >= 0
                            and lo <= min(op, c) and hi >= max(op, c)):
                        captured[t] = {'t': t, 'o': op, 'h': hi, 'l': lo, 'c': c, 'v': v}
                except (KeyError, ValueError, TypeError, OverflowError):
                    continue
        return raw

    baseline = c5_trend_v111421_evaluate_symbol(intercept, market, cfg, kst)
    ordered = [captured[t] for t in sorted(captured)]
    positions = {b['t']: i for i, b in enumerate(ordered)}
    for event in baseline['events']:
        ts = local_time(event['signal_time_kst'], kst)
        if ts not in positions:
            raise ValueError('전문가 비교: 원본 C5 신호 위치를 찾을 수 없습니다')
        event['gates'].update(c5_expert_v111422_gates(ordered, positions[ts]))
    if any('raschke' not in e['gates'] or 'volman' not in e['gates']
           for e in baseline['events']):
        raise ValueError('전문가 필터 계산 오류')
    return baseline



C5_EXPERT_V111422_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.22 · C5 + ADX/EMA20 / 가격 압축 검증실</title>
<style>/*REPLACE_STYLE*/
.note{border-radius:10px;padding:12px;background:#10182d;line-height:1.75;font-size:13px}
.notice{border-left:3px solid #ffca7a;padding:11px 12px;background:#252339;font-size:13px;line-height:1.75}
fieldset{border:0;padding:0;margin:0}.smaller{font-size:12px}
select{padding:10px;background:#111a2e;border:1px solid #35435e;color:#f3f6ff;border-radius:8px;width:100%;min-width:0;font:inherit}
</style></head><body><main>
<h1>🧪 V11.14.22 · C5 전문가 기법 2종 추가</h1>
<p class="muted">빗썸 KRW 과거 5분봉 · 15분 추세 / 캔들 구조 / ADX+EMA20 / 가격 압축 · V2 스캐너 변경 없음</p>
<section class="panel"><h2>이번 실험의 기준</h2>
<div class="note"><b>① 기존 C5:</b> 직전 종가 5이평 아래 → 현재 양봉이 5이평 위 첫 회복 → 5이평 상승.<br>
<b>② C5 + 15분 상승:</b> 신호 확정 시점까지 <b>완성된 15분봉</b> 6개에서, 최근 종가가 15분봉 5이평 위이고 그 5이평도 상승 중.<br>
<b>③ C5 + 상승 저점·고점 돌파:</b> 신호 전 4개 봉의 최저가가 그 앞 4개 봉의 최저가보다 높고, 신호 종가가 이전 3개 봉의 최고가 위.<br>
<b>④ C5 + 두 조건 모두:</b> ②와 ③이 동시에 참인 C5만 통과.<br>
<b>⑤ C5 + ADX/EMA20 (라슈키 원칙 응용):</b> Wilder ADX(14) > 30, +DI > -DI, 최근 ADX 상승 경험, 지난 4봉 중 EMA20 접촉, EMA20 상승 및 신호 종가가 EMA20과 직전 봉 고가 위.<br>
<b>⑥ C5 + 압축 돌파 (볼먼 원칙 응용):</b> 신호 이전 6개 5분봉 전체 가격 폭 1.2% 이내, EMA20이 가격 박스 부근, 신호 양봉 종가가 이전 6봉의 최고가를 돌파.<br>
<b>모든 방식 공통:</b> 동일 C5 신호 목록과 60분 신호 제한·다음 봉 시가 진입·익절 +1.5% / 손절 -1.0% / 최대 60분 종료·편도 거래 비용.</div>
<p class="hint">신호와 청산 결과는 기존 V11.14.17/19 검증 코드에서 가져옵니다. 15분봉은 5분봉을 3개씩 묶습니다. ADX/EMA20/압축 필터에도 누락 봉을 임의로 채우지 않습니다. 데이터가 부족하면 미확인으로 표시합니다. <b>필터가 제외한 신호도 원래 손익을 유지</b>해 놓친 수익 기회도 함께 확인합니다.</p>
<div class="notice">⚠️ 이 실험은 트레이더의 원전 전략 자체가 아니라 C5에 적용한 <b>기계식 필터</b>의 거래당 성과 비교입니다. 신호를 제거하고 다른 BUY로 자동 대체하지 않습니다. 과거 OHLC의 동일 봉 익절·손절 동시 터치는 기존 코드대로 손절 우선입니다. 실전 수익이 보장되지는 않습니다.</div></section>
<section class="panel"><h2>검증 설정</h2>
<div class="controls">
<label class="field">검증 날짜 · 한국시간<input id="date" type="date"></label>
<label class="field">검사 범위<select id="scope"><option value="40">빠른 예비검증 · 40종목</option><option value="all">전체 KRW 종목</option></select></label>
<label class="field">편도 수수료 (%)<input id="fee" type="number" value="0.04" min="0" max="0.5" step="0.01"></label>
<label class="field">편도 슬리피지 (%)<input id="slip" type="number" value="0.05" min="0" max="0.5" step="0.01"></label>
</div>
<p class="hint">40종목 예비검증은 이름순 전체 목록에서 일정 간격으로 고른 <b>기능 점검용 부분 표본</b>이며 전체 시장의 대표 표본이 아닙니다. 전 종목 결과는 반드시 '전체 KRW 종목'으로 따로 검사해 주세요. 검사 날짜나 범위를 바꾸면 이 V11.14.22 실험의 저장 기록만 초기화됩니다.</p>
<div class="actions"><button id="run">검사 시작 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">이 실험만 초기화</button><button id="csv" class="secondary" disabled>전체 신호·필터 결과 CSV 저장</button></div>
<progress id="progress" max="1" value="0"></progress><div id="status" class="status">검사 전</div></section>
<section class="panel"><h2>1. 데이터 범위</h2><div id="coverage" class="stats">검사 전</div><p class="hint">현재 상장된 종목의 과거 데이터만 조회합니다. 상장폐지 종목, 충분한 과거 봉이 없는 종목은 누락될 수 있으며 미래 데이터는 신호 판정에 사용하지 않습니다.</p></section>
<section class="panel"><h2>2. C5 대비 전략별 성과</h2><div id="summary" class="scroll">검사 전</div><p class="hint">수익률은 수수료·슬리피지를 반영한 <b>거래당 평균 순손익</b>입니다. 판정 완료 신호만 수익 계산에 포함하고 미판정 건수는 별도로 표시합니다.</p></section>
<section class="panel"><h2>3. 필터가 거른 신호 결과</h2><div id="blocked" class="scroll">검사 전</div><p class="hint">원래 C5에서 수익이 났던 신호를 제외하면 기회도 함께 놓친 것입니다. 필터를 통과한 신호와 거른 신호의 결과를 모두 기록합니다.</p></section>
<section class="panel"><h2>4. 종목별 상세 비교</h2><div id="examples" class="scroll">검사 전</div><p class="hint">날짜가 달라지면 결과를 CSV로 저장한 뒤 다음 날짜를 검사하세요. 과거 조건을 맞춘 구간과 별도 검증 구간을 나눠 비교하세요.</p></section>
</main><script>
'use strict';
const API='/api/winrate14c5experts',KEY='bithumb_v111422_c5_experts_v1';
const $=id=>document.getElementById(id);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const asNum=v=>(v===null||v===undefined||v==='')?NaN:Number(v);
const pct=v=>Number.isFinite(v)?v.toFixed(3)+'%':'—';
const ARMS=[['base','기존 C5'],['trend','C5 + 15분 상승'],['structure','C5 + 저점·고점 구조'],['both','C5 + 기존 두 조건'],['raschke','C5 + ADX/EMA20 눌림'],['volman','C5 + 압축 후 돌파']];
let state=null,running=false,stopRequested=false,storeFailed=false;
function kstDate(d){const a=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(d),g=k=>a.find(x=>x.type===k).value;return `${g('year')}-${g('month')}-${g('day')}`;}
const yesterday=()=>kstDate(new Date(Date.now()-86400000));
$('date').max=yesterday();$('date').value=yesterday();
function config(){const date=$('date').value,fee=asNum($('fee').value),slip=asNum($('slip').value),scope=$('scope').value;if(!/^\d{4}-\d{2}-\d{2}$/.test(date)||date>yesterday())throw Error('완전히 종료된 과거 날짜를 선택해줘');if(!Number.isFinite(fee)||!Number.isFinite(slip)||fee<0||fee>0.5||slip<0||slip>0.5)throw Error('편도 비용은 0~0.5% 범위로 입력해줘');if(!['40','all'].includes(scope))throw Error('검사 범위 오류');return {date,fee,slip,scope};}
const signature=c=>JSON.stringify([c.date,c.fee,c.slip,c.scope]);
function load(){try{const s=JSON.parse(localStorage.getItem(KEY)||'null');if(!s||s.version!==1||!s.config||!Array.isArray(s.markets)||!s.markets.length||!s.results||typeof s.results!=='object'||new Set(s.markets).size!==s.markets.length||s.markets.some(m=>!/^KRW-[A-Z0-9]+$/.test(m)))return null;return s;}catch{return null;}}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function records(){return state?state.markets.filter(m=>Object.prototype.hasOwnProperty.call(state.results,m)).map(m=>state.results[m]):[];}
function finished(){return !!(state&&state.markets.length&&records().length===state.markets.length&&state.failed===0);}
function accepted(e,k){if(k==='base')return true;if(k==='trend')return e.gates.trend_15m===true;if(k==='structure')return e.gates.structure===true;if(k==='both')return e.gates.trend_15m===true&&e.gates.structure===true;if(k==='raschke')return e.gates.raschke===true;if(k==='volman')return e.gates.volman===true;return false;}
function missingFeature(e,k){const g=e.gates;if(!g)return true;if(k==='trend')return g.trend_15m===null;if(k==='structure')return g.structure===null;if(k==='both')return g.trend_15m===null||g.structure===null;if(k==='raschke')return g.raschke===null;if(k==='volman')return g.volman===null;return false;}
function events(rs,key,allow=true){const out=[];for(const r of rs)for(const ev of r.events)if(accepted(ev,key)===allow)out.push({...ev,market:r.market});return out;}
function stat(es){const judged=es.filter(e=>e.outcome!=='UNKNOWN'&&Number.isFinite(asNum(e.net_pct)));const n=judged.length,profits=judged.filter(e=>asNum(e.net_pct)>0).length;return {signals:es.length,judged:n,unknown:es.length-n,profit:profits,tp:judged.filter(e=>e.outcome==='TP').length,sl:judged.filter(e=>e.outcome==='SL').length,tm:judged.filter(e=>e.outcome==='TIME').length,rate:n?profits/n*100:NaN,avg:n?judged.reduce((s,e)=>s+asNum(e.net_pct),0)/n:NaN};}
function row(cells){return '<tr>'+cells.map(x=>`<td>${esc(x)}</td>`).join('')+'</tr>';}
function table(headers,body){return '<table><thead><tr>'+headers.map(x=>`<th>${esc(x)}</th>`).join('')+'</tr></thead><tbody>'+body+'</tbody></table>';}
function card(label,v){return `<div class="stat"><small>${esc(label)}</small><strong>${esc(v)}</strong></div>`;}
function paintSummary(rs){let body='';const b=stat(events(rs,'base'));for(const [k,name] of ARMS){const s=stat(events(rs,k)),delta=Number.isFinite(s.avg)&&Number.isFinite(b.avg)?s.avg-b.avg:NaN;body+=row([name,s.signals,s.judged,s.unknown,s.profit,pct(s.rate),pct(s.avg),pct(delta),`${s.tp} / ${s.sl} / ${s.tm}`,k==='base'?0:events(rs,k,false).length]);}return table(['전략','신호','판정','미판정','수익','수익 거래 비율','평균 순손익','기존 대비 %p','TP / SL / TIME','제외'],body);}
function paintBlocked(rs){let body='';for(const [k,name] of ARMS.slice(1)){const blocked=events(rs,k,false),s=stat(blocked),unknown=blocked.filter(e=>missingFeature(e,k)).length;body+=row([name,blocked.length,s.judged,s.unknown,s.tp,s.sl,s.profit,pct(s.avg),unknown]);}return table(['전략','제외 건수','제외 신호 판정','미판정','놓친 TP','제외한 SL','놓친 수익 거래','제외 신호 평균','조건 결측 포함'],body);}
function paintExamples(rs){const items=events(rs,'base').filter(e=>e.gates).sort((a,b)=>b.entry_time_kst.localeCompare(a.entry_time_kst)).slice(0,70);const flag=v=>v===null?'자료부족':v?'O':'X';return items.length?table(['종목','C5 진입 시각','결과','순손익','15분 상승','캔들 구조','ADX/EMA20','압축 돌파','ADX14','압축폭 %'],items.map(e=>row([e.market,e.entry_time_kst,e.outcome,pct(asNum(e.net_pct)),flag(e.gates.trend_15m),flag(e.gates.structure),flag(e.gates.raschke),flag(e.gates.volman),Number.isFinite(asNum(e.gates.adx14))?asNum(e.gates.adx14).toFixed(2):'—',pct(asNum(e.gates.squeeze_range_pct))])).join('')):'신호 없음';}
function paint(){const rs=records(),total=state?.markets.length||0,full=finished(),base=events(rs,'base');$('progress').max=total||1;$('progress').value=rs.length;$('csv').disabled=!full;$('coverage').innerHTML=card('검사 완료',`${rs.length} / ${total}`)+card('기존 C5 신호',String(base.length))+card('C5 판정 / 미판정',`${stat(base).judged} / ${stat(base).unknown}`)+card('15분 상승 통과',String(base.filter(e=>e.gates.trend_15m===true).length))+card('구조 통과',String(base.filter(e=>e.gates.structure===true).length))+card('ADX/EMA20 통과',String(base.filter(e=>e.gates.raschke===true).length))+card('압축 돌파 통과',String(base.filter(e=>e.gates.volman===true).length))+card('원본 과거 봉 제한',String(rs.filter(r=>r.truncated).length));if(full){$('summary').innerHTML=paintSummary(rs);$('blocked').innerHTML=paintBlocked(rs);$('examples').innerHTML=paintExamples(rs);}else for(const id of ['summary','blocked','examples'])$(id).textContent='선택한 검사 범위 완료 후 공개';$('status').textContent=(full?'✅ 검증 완료':running?'⏳ 과거 기록 분석 중':'검사 대기 / 일시 중지')+` · ${rs.length}/${total}종목`+(state?.failed?' · 일부 API 오류, 이어하기 가능':'')+(full?`\n${state.config.scope==='40'?'⚠️ 40종목 예비 표본 결과이며 전체 시장 결론이 아닙니다.':'전 KRW 종목 검사 완료. 과거 모의 성적입니다.'}`:'\n전체 범위가 끝나면 전략별 결과를 공개합니다.');}
async function fetchJson(url,opts){const ac=new AbortController(),timer=setTimeout(()=>ac.abort(),115000);try{const res=await fetch(url,{...opts,cache:'no-store',signal:ac.signal});const data=await res.json();if(!res.ok||!data.ok)throw Error(data.error||'HTTP '+res.status);return data;}finally{clearTimeout(timer);}}
async function init(cfg){const d=await fetchJson(API+'/universe');if(!Array.isArray(d.items)||!d.items.length)throw Error('빗썸 KRW 목록을 불러오지 못했어');const all=d.items,markets=cfg.scope==='40'&&all.length>40?Array.from({length:40},(_,i)=>all[Math.floor((i+.5)*all.length/40)]):all;state={version:1,config:cfg,markets,results:{},failed:0};save();}
function validResponse(d,market){if(d.scanned!==1||d.errors!==0||!Array.isArray(d.results)||d.results.length!==1)return false;const r=d.results[0];return r.market===market&&Number.isInteger(r.original_c5_signals)&&Number.isInteger(r.original_c5_unknown)&&Array.isArray(r.events)&&r.events.length===r.original_c5_signals&&r.events.filter(e=>e.outcome==='UNKNOWN').length===r.original_c5_unknown&&r.events.every(e=>e.gates&&[true,false,null].includes(e.gates.trend_15m)&&[true,false,null].includes(e.gates.structure)&&[true,false,null].includes(e.gates.raschke)&&[true,false,null].includes(e.gates.volman)&&['TP','SL','TIME','UNKNOWN'].includes(e.outcome)&&typeof e.signal_time_kst==='string'&&typeof e.entry_time_kst==='string');}
async function run(){if(running)return;let cfg;try{cfg=config();}catch(e){$('status').textContent='⚠️ '+e.message;return;}if(state&&signature(cfg)!==signature(state.config)){if(!confirm('검증 날짜/비용/범위가 달라졌어. 이 실험의 저장 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);}running=true;stopRequested=false;storeFailed=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;try{if(!state)await init(cfg);state.failed=0;save();paint();const pending=state.markets.filter(m=>!Object.prototype.hasOwnProperty.call(state.results,m));let cursor=0,failed=0;async function worker(){while(!stopRequested&&!storeFailed){const market=pending[cursor++];if(!market)return;let pass=false;for(let tryN=0;tryN<3&&!stopRequested;tryN++){try{const data=await fetchJson(API+'/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...state.config,markets:[market]})});if(!validResponse(data,market))throw Error('응답 구조 검증 실패');state.results[market]=data.results[0];try{save();}catch(e){delete state.results[market];storeFailed=true;$('status').textContent='⚠️ 휴대폰 저장공간 부족, 검사를 멈췄어';break;}pass=true;paint();break;}catch(e){if(tryN===2){failed++;$('status').textContent='⚠️ '+market+': '+String(e.message||e);}else await new Promise(resolve=>setTimeout(resolve,1000*(tryN+1)));}}if(!pass&&failed>=4){stopRequested=true;break;}}}await Promise.all([worker(),worker()]);state.failed=failed;try{save();}catch{}paint();if(stopRequested)$('status').textContent+='\n일시 중지: 다시 누르면 미완료 종목부터 계속 검사';}catch(e){$('status').textContent='⚠️ '+String(e.message||e);}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;}}
$('run').onclick=run;$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;};$('reset').onclick=()=>{if(running)return;if(!confirm('V11.14.22 실험 진행 기록만 초기화할까?'))return;state=null;localStorage.removeItem(KEY);paint();};
function csvCell(v){let s=String(v??'');if(/^[\s]*[=+@]/.test(s)||(/^[\s]*-/.test(s)&&!/^[\s]*-\d+(?:\.\d+)?\s*$/.test(s)))s="'"+s;return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!finished())return;const columns=['version','date','market','strategy','status','filter_reason','outcome','net_pct','signal_time_kst','entry_time_kst','is_first_signal','entry','exit','minutes','reason','trend_15m','structure','raschke','volman','adx14','plus_di14','minus_di14','ema20','adx_rise_recent','ema_touch_4','squeeze_range_pct','squeeze_top','squeeze_bottom','ema_near_box','trend_15m_close','trend_15m_sma5','trend_15m_sma5_slope_pct','older_4_low','recent_4_low','previous_3_high','signal_close','fee_pct_side','slip_pct_side','tp_pct','sl_pct','horizon_minutes'];const lines=[columns.join(',')];for(const r of records())for(const [key] of ARMS){const pre={version:'V11.14.22',date:state.config.date,market:r.market,strategy:key,fee_pct_side:state.config.fee,slip_pct_side:state.config.slip,tp_pct:1.5,sl_pct:1,horizon_minutes:60};for(const ev of r.events.length?r.events:[{status:'NO_SIGNAL'}]){const ok=ev.gates?accepted(ev,key):false,missing=ev.gates&&missingFeature(ev,key);const reason=ok?'':missing?'FEATURE_UNAVAILABLE':ev.gates?'FILTER_FAIL':'NO_SIGNAL';const item={...pre,...ev,...(ev.gates||{}),status:ev.status|| (ok?'ACCEPTED':'BLOCKED'),filter_reason:reason};lines.push(columns.map(k=>csvCell(item[k])).join(','));}}const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`bithumb_C5_experts_${state.config.date}_v111422.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),2500);};
state=load();if(state){for(const key of ['date','fee','slip','scope'])$(key).value=state.config[key];}paint();
</script></body></html>
'''.replace('/*REPLACE_STYLE*/', C5_V111417_PAGE.split('<style>',1)[1].split('</style>',1)[0])


def register_c5_expert_v111422_lab(app, deps):
    api_get, get_markets, kst = deps['api_get'], deps['get_markets'], deps['KST']
    cached = {'markets': None, 'at': 0}
    lock = Lock()

    def listed():
        with lock:
            if cached['markets'] is None or time.time() - cached['at'] > 600:
                items = sorted({m for m in get_markets()
                                if isinstance(m, str) and MARKET_RE.fullmatch(m)})
                if not items:
                    raise ValueError('KRW 종목을 조회하지 못했습니다')
                cached.update(markets=items, at=time.time())
            return cached['markets']

    @app.get('/backtest-winrate-c5-experts')
    def c5_expert_v111422_page():
        return render_template_string(C5_EXPERT_V111422_PAGE)

    @app.get('/api/winrate14c5experts/universe')
    def c5_expert_v111422_universe():
        try:
            items = listed()
            return jsonify({'ok': True, 'items': items, 'count': len(items),
                            'rule_version': 'V11.14.22',
                            'universe_type': 'currently_listed_KRW'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400

    @app.post('/api/winrate14c5experts/evaluate')
    def c5_expert_v111422_evaluate():
        try:
            raw = request.get_json(silent=True)
            cfg = c5_v111417_config(raw, kst)
            markets = raw.get('markets') if isinstance(raw, dict) else None
            if (not isinstance(markets, list) or len(markets) != 1
                    or not isinstance(markets[0], str)
                    or not MARKET_RE.fullmatch(markets[0])):
                raise ValueError('한 번에 KRW 종목 1개만 검증할 수 있습니다')
            if markets[0] not in set(listed()):
                raise ValueError('현재 KRW 목록에 없는 종목입니다')
            result = c5_expert_v111422_evaluate_symbol(
                api_get, markets[0], cfg, kst)
            return jsonify({'ok': True, 'scanned': 1, 'results': [result],
                            'errors': 0, 'error_markets': [],
                            'rule_version': 'V11.14.22'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:160]}), 400


_v111421_register_daytrade_review = register_daytrade_review


def register_daytrade_review(app, deps):
    _v111421_register_daytrade_review(app, deps)
    register_c5_expert_v111422_lab(app, deps)


# ---------------------------------------------------------------------------
# V11.14.23 - Research ONLY: C5 + Volman-inspired + SMA12/SMA200 filters.
# Uses the 74 already-observed Volman-ACCEPTED events from the three
# user-supplied V11.14.22 CSV files: 2026-11-08, 2026-11-09, 2026-11-11.
# Baseline results are preserved; only 12/200 SMA features are requested.
# No changes to V2 app.py, existing labs, or existing scan/BUY functions.
# ---------------------------------------------------------------------------

import json as _ma12200_json

C5_MA_V111423_SEEDS = _ma12200_json.loads(r'''[{"id":0,"date":"2026-11-08","market":"KRW-AKT","signal_time_kst":"2026-11-08 11:25","outcome":"TIME","net_pct":0.1733,"entry":848.0,"exit":851.0,"minutes":60},{"id":1,"date":"2026-11-08","market":"KRW-BLEND","signal_time_kst":"2026-11-08 18:00","outcome":"TP","net_pct":1.3175,"entry":76.77,"exit":77.92155,"minutes":10},{"id":2,"date":"2026-11-08","market":"KRW-BTC","signal_time_kst":"2026-11-08 20:00","outcome":"TIME","net_pct":-0.1933,"entry":103556000.0,"exit":103542000.0,"minutes":60},{"id":3,"date":"2026-11-08","market":"KRW-BTC","signal_time_kst":"2026-11-08 21:25","outcome":"TIME","net_pct":-0.0902,"entry":103615000.0,"exit":103708000.0,"minutes":60},{"id":4,"date":"2026-11-08","market":"KRW-BTR","signal_time_kst":"2026-11-08 08:20","outcome":"SL","net_pct":-1.178,"entry":56.08,"exit":55.5192,"minutes":10},{"id":5,"date":"2026-11-08","market":"KRW-ETH","signal_time_kst":"2026-11-08 07:15","outcome":"TIME","net_pct":-0.1798,"entry":3051000.0,"exit":3051000.0,"minutes":60},{"id":6,"date":"2026-11-08","market":"KRW-FLOCK","signal_time_kst":"2026-11-08 12:45","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":7,"date":"2026-11-08","market":"KRW-HUMA","signal_time_kst":"2026-11-08 23:45","outcome":"TIME","net_pct":-0.2438,"entry":15.61,"exit":15.6,"minutes":60},{"id":8,"date":"2026-11-08","market":"KRW-IN","signal_time_kst":"2026-11-08 19:05","outcome":"SL","net_pct":-1.178,"entry":35.46,"exit":35.1054,"minutes":60},{"id":9,"date":"2026-11-08","market":"KRW-ONDO","signal_time_kst":"2026-11-08 08:40","outcome":"TIME","net_pct":-0.3807,"entry":497.0,"exit":496.0,"minutes":60},{"id":10,"date":"2026-11-08","market":"KRW-Q","signal_time_kst":"2026-11-08 08:20","outcome":"TIME","net_pct":-0.447,"entry":37.36,"exit":37.26,"minutes":60},{"id":11,"date":"2026-11-08","market":"KRW-Q","signal_time_kst":"2026-11-08 16:15","outcome":"TP","net_pct":1.3175,"entry":36.78,"exit":37.3317,"minutes":40},{"id":12,"date":"2026-11-08","market":"KRW-SIGN","signal_time_kst":"2026-11-08 10:00","outcome":"TP","net_pct":1.3175,"entry":12.56,"exit":12.7484,"minutes":5},{"id":13,"date":"2026-11-08","market":"KRW-SUI","signal_time_kst":"2026-11-08 13:00","outcome":"TIME","net_pct":-0.2656,"entry":1164.0,"exit":1163.0,"minutes":60},{"id":14,"date":"2026-11-08","market":"KRW-TRUMP","signal_time_kst":"2026-11-08 01:50","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":15,"date":"2026-11-08","market":"KRW-US","signal_time_kst":"2026-11-08 20:00","outcome":"TP","net_pct":1.3175,"entry":5.894,"exit":5.98241,"minutes":60},{"id":16,"date":"2026-11-08","market":"KRW-USDT","signal_time_kst":"2026-11-08 07:45","outcome":"TIME","net_pct":-0.1798,"entry":1366.0,"exit":1366.0,"minutes":60},{"id":17,"date":"2026-11-09","market":"KRW-BTC","signal_time_kst":"2026-11-09 20:15","outcome":"TIME","net_pct":-0.1789,"entry":101266000.0,"exit":101267000.0,"minutes":60},{"id":18,"date":"2026-11-09","market":"KRW-BTC","signal_time_kst":"2026-11-09 23:40","outcome":"TIME","net_pct":0.6585,"entry":101450000.0,"exit":102302000.0,"minutes":60},{"id":19,"date":"2026-11-09","market":"KRW-DOGE","signal_time_kst":"2026-11-09 13:05","outcome":"TIME","net_pct":-0.6213,"entry":94.97,"exit":94.55,"minutes":60},{"id":20,"date":"2026-11-09","market":"KRW-DOGE","signal_time_kst":"2026-11-09 16:35","outcome":"TIME","net_pct":0.4741,"entry":93.11,"exit":93.72,"minutes":60},{"id":21,"date":"2026-11-09","market":"KRW-DOGE","signal_time_kst":"2026-11-09 20:15","outcome":"TIME","net_pct":-0.0099,"entry":93.98,"exit":94.14,"minutes":60},{"id":22,"date":"2026-11-09","market":"KRW-ELSA","signal_time_kst":"2026-11-09 19:45","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":23,"date":"2026-11-09","market":"KRW-ENA","signal_time_kst":"2026-11-09 20:15","outcome":"SL","net_pct":-1.178,"entry":207.0,"exit":204.93,"minutes":35},{"id":24,"date":"2026-11-09","market":"KRW-ENJ","signal_time_kst":"2026-11-09 20:00","outcome":"TP","net_pct":1.3175,"entry":31.45,"exit":31.92175,"minutes":5},{"id":25,"date":"2026-11-09","market":"KRW-ETH","signal_time_kst":"2026-11-09 20:15","outcome":"TIME","net_pct":-0.3873,"entry":2887000.0,"exit":2881000.0,"minutes":60},{"id":26,"date":"2026-11-09","market":"KRW-HPP","signal_time_kst":"2026-11-09 11:55","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":27,"date":"2026-11-09","market":"KRW-HUMA","signal_time_kst":"2026-11-09 23:40","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":28,"date":"2026-11-09","market":"KRW-ICNT","signal_time_kst":"2026-11-09 03:50","outcome":"SL","net_pct":-1.178,"entry":491.0,"exit":486.09,"minutes":40},{"id":29,"date":"2026-11-09","market":"KRW-KAIA","signal_time_kst":"2026-11-09 11:05","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":30,"date":"2026-11-09","market":"KRW-LINK","signal_time_kst":"2026-11-09 11:30","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":31,"date":"2026-11-09","market":"KRW-MPLX","signal_time_kst":"2026-11-09 14:05","outcome":"TP","net_pct":1.3175,"entry":93.47,"exit":94.87205,"minutes":20},{"id":32,"date":"2026-11-09","market":"KRW-NEAR","signal_time_kst":"2026-11-09 10:40","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":33,"date":"2026-11-09","market":"KRW-ONDO","signal_time_kst":"2026-11-09 13:05","outcome":"TIME","net_pct":-0.998,"entry":488.0,"exit":484.0,"minutes":60},{"id":34,"date":"2026-11-09","market":"KRW-SIGN","signal_time_kst":"2026-11-09 01:30","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":35,"date":"2026-11-09","market":"KRW-SOL","signal_time_kst":"2026-11-09 21:45","outcome":"TIME","net_pct":-0.5182,"entry":118000.0,"exit":117600.0,"minutes":60},{"id":36,"date":"2026-11-09","market":"KRW-SUI","signal_time_kst":"2026-11-09 12:40","outcome":"TIME","net_pct":-0.7912,"entry":1143.0,"exit":1136.0,"minutes":60},{"id":37,"date":"2026-11-09","market":"KRW-SUI","signal_time_kst":"2026-11-09 20:15","outcome":"TIME","net_pct":0.1744,"entry":1127.0,"exit":1131.0,"minutes":60},{"id":38,"date":"2026-11-09","market":"KRW-TRUMP","signal_time_kst":"2026-11-09 01:25","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":39,"date":"2026-11-09","market":"KRW-US","signal_time_kst":"2026-11-09 20:05","outcome":"TIME","net_pct":0.9077,"entry":5.507,"exit":5.567,"minutes":60},{"id":40,"date":"2026-11-09","market":"KRW-US","signal_time_kst":"2026-11-09 21:05","outcome":"TIME","net_pct":0.3229,"entry":5.559,"exit":5.587,"minutes":60},{"id":41,"date":"2026-11-09","market":"KRW-USDT","signal_time_kst":"2026-11-09 06:05","outcome":"TIME","net_pct":0.4783,"entry":1365.0,"exit":1374.0,"minutes":60},{"id":42,"date":"2026-11-09","market":"KRW-WAVES","signal_time_kst":"2026-11-09 19:20","outcome":"SL","net_pct":-1.178,"entry":305.0,"exit":301.95,"minutes":5},{"id":43,"date":"2026-11-09","market":"KRW-XLM","signal_time_kst":"2026-11-09 18:00","outcome":"TIME","net_pct":-0.1798,"entry":199.0,"exit":199.0,"minutes":60},{"id":44,"date":"2026-11-09","market":"KRW-XRP","signal_time_kst":"2026-11-09 16:35","outcome":"TIME","net_pct":-0.0452,"entry":1483.0,"exit":1485.0,"minutes":60},{"id":45,"date":"2026-11-09","market":"KRW-XRP","signal_time_kst":"2026-11-09 20:15","outcome":"TIME","net_pct":0.2254,"entry":1478.0,"exit":1484.0,"minutes":60},{"id":46,"date":"2026-11-09","market":"KRW-ZAMA","signal_time_kst":"2026-11-09 15:45","outcome":"SL","net_pct":-1.178,"entry":88.39,"exit":87.5061,"minutes":25},{"id":47,"date":"2026-11-09","market":"KRW-ZAMA","signal_time_kst":"2026-11-09 20:15","outcome":"TP","net_pct":1.3175,"entry":92.0,"exit":93.38,"minutes":50},{"id":48,"date":"2026-11-11","market":"KRW-BTC","signal_time_kst":"2026-11-11 22:50","outcome":"TIME","net_pct":-0.6019,"entry":100051000.0,"exit":99628000.0,"minutes":60},{"id":49,"date":"2026-11-11","market":"KRW-CHIP","signal_time_kst":"2026-11-11 10:25","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":50,"date":"2026-11-11","market":"KRW-DOGE","signal_time_kst":"2026-11-11 05:10","outcome":"TIME","net_pct":0.2872,"entry":91.91,"exit":92.34,"minutes":60},{"id":51,"date":"2026-11-11","market":"KRW-DOGE","signal_time_kst":"2026-11-11 07:05","outcome":"TIME","net_pct":-0.8486,"entry":92.54,"exit":91.92,"minutes":60},{"id":52,"date":"2026-11-11","market":"KRW-DOGE","signal_time_kst":"2026-11-11 23:35","outcome":"TP","net_pct":1.3175,"entry":92.45,"exit":93.83675,"minutes":60},{"id":53,"date":"2026-11-11","market":"KRW-ENA","signal_time_kst":"2026-11-11 09:00","outcome":"TP","net_pct":1.3175,"entry":205.0,"exit":208.075,"minutes":35},{"id":54,"date":"2026-11-11","market":"KRW-ETH","signal_time_kst":"2026-11-11 04:45","outcome":"TIME","net_pct":0.2503,"entry":2785000.0,"exit":2797000.0,"minutes":60},{"id":55,"date":"2026-11-11","market":"KRW-ETH","signal_time_kst":"2026-11-11 20:10","outcome":"TIME","net_pct":-0.3946,"entry":2789000.0,"exit":2783000.0,"minutes":60},{"id":56,"date":"2026-11-11","market":"KRW-ETH","signal_time_kst":"2026-11-11 23:35","outcome":"TIME","net_pct":0.5718,"entry":2789000.0,"exit":2810000.0,"minutes":60},{"id":57,"date":"2026-11-11","market":"KRW-HAEDAL","signal_time_kst":"2026-11-11 10:25","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":58,"date":"2026-11-11","market":"KRW-IRYS","signal_time_kst":"2026-11-11 20:15","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":59,"date":"2026-11-11","market":"KRW-KGEN","signal_time_kst":"2026-11-11 20:45","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":60,"date":"2026-11-11","market":"KRW-ONDO","signal_time_kst":"2026-11-11 05:10","outcome":"TIME","net_pct":0.2219,"entry":497.0,"exit":499.0,"minutes":60},{"id":61,"date":"2026-11-11","market":"KRW-ONDO","signal_time_kst":"2026-11-11 07:05","outcome":"SL","net_pct":-1.178,"entry":502.0,"exit":496.98,"minutes":55},{"id":62,"date":"2026-11-11","market":"KRW-ONDO","signal_time_kst":"2026-11-11 09:35","outcome":"TIME","net_pct":0.4167,"entry":502.0,"exit":505.0,"minutes":60},{"id":63,"date":"2026-11-11","market":"KRW-ONDO","signal_time_kst":"2026-11-11 16:20","outcome":"TIME","net_pct":0.4215,"entry":498.0,"exit":501.0,"minutes":60},{"id":64,"date":"2026-11-11","market":"KRW-PENGU","signal_time_kst":"2026-11-11 21:10","outcome":"TIME","net_pct":0.2279,"entry":8.568,"exit":8.603,"minutes":60},{"id":65,"date":"2026-11-11","market":"KRW-SOL","signal_time_kst":"2026-11-11 04:45","outcome":"TIME","net_pct":0.0852,"entry":113000.0,"exit":113300.0,"minutes":60},{"id":66,"date":"2026-11-11","market":"KRW-TRUMP","signal_time_kst":"2026-11-11 17:10","outcome":"SL","net_pct":-1.178,"entry":1513.0,"exit":1497.87,"minutes":25},{"id":67,"date":"2026-11-11","market":"KRW-US","signal_time_kst":"2026-11-11 06:05","outcome":"TIME","net_pct":-0.2353,"entry":5.398,"exit":5.395,"minutes":60},{"id":68,"date":"2026-11-11","market":"KRW-US","signal_time_kst":"2026-11-11 15:50","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":69,"date":"2026-11-11","market":"KRW-US","signal_time_kst":"2026-11-11 19:20","outcome":"UNKNOWN","net_pct":null,"entry":null,"exit":null,"minutes":null},{"id":70,"date":"2026-11-11","market":"KRW-USDT","signal_time_kst":"2026-11-11 01:30","outcome":"TIME","net_pct":-0.1798,"entry":1361.0,"exit":1361.0,"minutes":60},{"id":71,"date":"2026-11-11","market":"KRW-XPL","signal_time_kst":"2026-11-11 12:40","outcome":"TIME","net_pct":0.1862,"entry":70.9,"exit":71.16,"minutes":60},{"id":72,"date":"2026-11-11","market":"KRW-XRP","signal_time_kst":"2026-11-11 03:15","outcome":"TIME","net_pct":1.1372,"entry":1440.0,"exit":1459.0,"minutes":60},{"id":73,"date":"2026-11-11","market":"KRW-XRP","signal_time_kst":"2026-11-11 21:35","outcome":"TIME","net_pct":-0.389,"entry":1432.0,"exit":1429.0,"minutes":60}]''')
C5_MA_V111423_SEED_SHA = "dc2811bff23545d46430cbbe5710b7da53e936497d2da3fdca73de4f72c0c191"
C5_MA_V111423_BY_ID = {e['id']: e for e in C5_MA_V111423_SEEDS}
C5_MA_V111423_DATES = ('2026-11-08', '2026-11-09', '2026-11-11')


def c5_ma_v111423_features(api_get, event, kst):
    """SMA12/SMA200 of completed 5m candle closes at the original signal.

    The Bithumb `to` parameter is exclusive (in KST); signal+5min includes
    the completed signal candle, with nothing from the future. Two 200-bar
    pages are enough for 206 observed bars. Require true 5m continuity:
      200 bars for current crossover state; 206 for past-six-bar crossing
      and the slope of SMA200 over 30 minutes.
    Missing candles are never forward-filled or replaced. No decision is
    made when its prerequisite history is incomplete.
    """
    market = event['market']
    signal = local_time(event['signal_time_kst'], kst)
    if signal.second != 0 or signal.microsecond != 0 or signal.minute % 5:
        raise ValueError('5분봉 신호 시각 형식 오류')
    end_exclusive = signal + timedelta(minutes=5)
    cursor = end_exclusive
    found = {}
    for page in range(2):
        raw = api_get('/v1/candles/minutes/5', {
            'market': market, 'count': 200,
            'to': cursor.strftime('%Y-%m-%d %H:%M:%S'),
        })
        if not isinstance(raw, list):
            raise ValueError('200이평 캔들 응답 형식 오류')
        oldest = None
        for b in raw:
            if not isinstance(b, dict):
                continue
            try:
                t = local_time(b['candle_date_time_kst'], kst)
                close = float(b['trade_price'])
                if not isfinite(close) or close <= 0:
                    continue
                if t < end_exclusive:
                    found[t] = close
                    oldest = t if oldest is None or t < oldest else oldest
            except (KeyError, ValueError, TypeError, OverflowError):
                continue
        if oldest is None or len(raw) < 200 or len(found) >= 206:
            break
        if oldest >= cursor:
            raise ValueError('과거 봉 페이지네이션이 전진하지 않습니다')
        cursor = oldest

    times = sorted(t for t in found if t <= signal)
    rows = [(t, found[t]) for t in times]
    n = len(rows)
    result = {
        'id': event['id'], 'signal_time_kst': event['signal_time_kst'],
        'history_status': 'SHORT', 'observed_bars': n,
        'sma12': None, 'sma200': None,
        'sma200_slope6_pct': None, 'distance12_200_pct': None,
        'ma_above': None, 'ma_cross6': None, 'ma_slope6': None,
        'signal_close': None, 'missing_intervals_206': None,
    }
    if not rows or rows[-1][0] != signal:
        result['history_status'] = 'SIGNAL_CANDLE_NOT_FOUND'
        return result
    result['signal_close'] = rows[-1][1]
    if n < 200:
        return result
    step = timedelta(minutes=5)
    last200 = rows[-200:]
    if any(last200[j][0] - last200[j-1][0] != step for j in range(1, 200)):
        result['history_status'] = 'GAP_200'
        return result
    closes = [p for t, p in rows]
    sma12 = sum(closes[-12:]) / 12
    sma200 = sum(closes[-200:]) / 200
    result.update(sma12=round(sma12, 10), sma200=round(sma200, 10),
                  distance12_200_pct=round((sma12 / sma200 - 1)*100, 6),
                  ma_above=bool(sma12 > sma200),
                  history_status='PARTIAL_200')
    if n < 206:
        return result
    last206 = rows[-206:]
    gaps = sum(last206[j][0] - last206[j-1][0] != step for j in range(1, 206))
    result['missing_intervals_206'] = gaps
    if gaps:
        result['history_status'] = 'GAP_206'
        return result
    prev_sma200 = sum(closes[-206:-6])/200
    slope = (sma200 / prev_sma200 - 1) * 100
    crossed = False
    for end in range(n-5, n+1):
        now12 = sum(closes[end-12:end])/12
        now200 = sum(closes[end-200:end])/200
        prior12 = sum(closes[end-13:end-1])/12
        prior200 = sum(closes[end-201:end-1])/200
        if prior12 <= prior200 and now12 > now200:
            crossed = True
            break
    result.update(history_status='OK',
                  sma200_slope6_pct=round(slope, 6),
                  ma_cross6=bool(sma12 > sma200 and crossed),
                  ma_slope6=bool(sma12 > sma200 and sma200 > prev_sma200))
    return result


C5_MA_V111423_PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.23 · C5 볼먼 + 12/200이평 검증실</title>
<style>/*REPLACE_STYLE*/
.notice{border-left:3px solid #ffca7a;padding:11px 12px;background:#252339;font-size:13px;line-height:1.75}
.note{border-radius:10px;padding:12px;background:#10182d;line-height:1.8;font-size:13px}
select{padding:10px;background:#111a2e;border:1px solid #35435e;color:#f3f6ff;border-radius:8px}
</style></head><body><main>
<h1>🧪 V11.14.23 · 볼먼 + 12/200이평 비교</h1>
<p class="muted">기존 C5·볼먼 압축돌파 기록에 12이평·200이평 조건만 덧붙이는 검증실 · 실전 V2 변경 없음</p>
<section class="panel"><h2>이번 실험의 세 날짜</h2>
<div class="note"><b>11월 8일:</b> 볼먼 응용 +0.074% (소폭 플러스)<br>
<b>11월 11일:</b> 볼먼 응용 +0.072% (소폭 플러스)<br>
<b>11월 9일:</b> 볼먼 응용 -0.054% (7일 중 가장 작은 평균 손실)<br>
<b>기준:</b> 이미 저장한 V11.14.22 CSV의 볼먼 ACCEPTED 신호 74건 (11/8: 17 · 11/9: 31 · 11/11: 26). 그중 손익 확정 58건. 다른 날짜나 다른 신호는 섞지 않음.</div>
<div class="notice">⚠️ 날짜와 필터는 이미 확인한 수익 결과를 참고해 고른 것이므로 <b>독립 검증이 아닌 사후 탐색</b>입니다. SMA는 신호가 완전히 끝난 시점의 과거 봉만 사용합니다. 거래량이 없는 5분봉을 임의 생성하지 않으며, 200봉 연속 데이터가 없으면 미확인으로 둡니다. 유명 트레이더의 원전 매매법과 동일하다고 주장하지 않습니다.</div>
</section>
<section class="panel"><h2>비교할 네 가지</h2>
<div class="note"><b>① 볼먼 기본:</b> 기존 C5 + 볼먼 압축돌파 (기존 손익은 그대로)<br>
<b>② 볼먼 + 12이평 > 200이평:</b> C5 신호 확정봉 종가 기준 SMA12가 SMA200보다 위<br>
<b>③ 볼먼 + 최근 6봉 내 12/200 상향 교차:</b> 최근 30분 이내 SMA12가 SMA200을 아래에서 위로 돌파하고 현재도 위<br>
<b>④ 볼먼 + 12>200 + 상승하는 200이평:</b> SMA12 > SMA200이고 SMA200이 6봉 전보다 높음</div>
<p class="hint">전부 5분봉 단순이동평균(SMA)이며 진입가/청산 결과는 V11.14.22 CSV와 동일해. 조건이 성립하지 않으면 해당 BUY만 제외하며 다음 신호를 새로 생성하지 않아. 미확인 데이터는 제외/승패로 임의 처리하지 않아.</p>
<div class="actions"><button id="run">74건 일괄 검사 / 이어하기</button><button id="pause" class="secondary" disabled>일시 중지</button><button id="reset" class="secondary">이 실험만 초기화</button><button id="csv" class="secondary" disabled>비교 결과 CSV 저장</button></div>
<progress id="progress" max="74" value="0"></progress><div id="status" class="status">기존 결과 불러오는 중</div></section>
<section class="panel"><h2>1. 데이터 및 진행률</h2><div id="coverage" class="stats">불러오는 중</div></section>
<section class="panel"><h2>2. 3일 통합 비교</h2><div id="overall" class="scroll">검사 전</div></section>
<section class="panel"><h2>3. 날짜별 전략 비교</h2><div id="byday">검사 전</div></section>
<section class="panel"><h2>4. 종목별 원본 신호와 이동평균 판정</h2><div id="examples" class="scroll">검사 전</div><p class="hint">미확인 사유(GAP_200 / GAP_206 / SHORT 등)도 CSV에 따로 남겨. 평균 순손익은 기존 CSV에 실제 값이 있는 신호만 집계해. 이 연구의 결론을 실전에 적용하려면 선택하지 않은 별도 날짜로 재검증해야 해.</p></section>
</main><script>
'use strict';
const API='/api/winrate14c5ma12200',KEY='bithumb_v111423_volman_ma12_ma200_v1';
const $=id=>document.getElementById(id);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=v=>(v===null||v===undefined||v==='')?NaN:Number(v);
const pct=x=>Number.isFinite(x)?x.toFixed(3)+'%':'—';
const ARMS=[['base','볼먼 기본'],['ma_above','볼먼 + 12>200'],['ma_cross6','볼먼 + 6봉내 상향교차'],['ma_slope6','볼먼 + 12>200·200상승']];
const DAYS=['2026-11-08','2026-11-09','2026-11-11'];
let state=null,seeds=[],running=false,stopRequested=false;
function row(vals){return '<tr>'+vals.map(x=>'<td>'+esc(x)+'</td>').join('')+'</tr>';}
function table(head,rs){return '<table><thead><tr>'+head.map(x=>'<th>'+esc(x)+'</th>').join('')+'</tr></thead><tbody>'+rs.join('')+'</tbody></table>';}
function card(name,value){return `<div class="stat"><small>${esc(name)}</small><strong>${esc(value)}</strong></div>`;}
function load(){try{const x=JSON.parse(localStorage.getItem(KEY)||'null');return x&&x.version===1&&x.results&&typeof x.results==='object'?x:null;}catch{return null;}}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function ready(){return seeds.length===74;}
function done(){return ready()&&seeds.every(x=>Object.prototype.hasOwnProperty.call(state.results,x.id));}
function flag(e,k){if(k==='base')return true;const r=state.results[e.id];return r?r[k]:null;}
function metrics(items,k){const taken=items.filter(e=>flag(e,k)===true),ok=taken.filter(e=>e.outcome!=='UNKNOWN'&&Number.isFinite(num(e.net_pct))),n=ok.length;const sum=ok.reduce((a,e)=>a+num(e.net_pct),0);return {signals:taken.length,judged:n,unknown:taken.length-n,profit:ok.filter(e=>num(e.net_pct)>0).length,tp:ok.filter(e=>e.outcome==='TP').length,sl:ok.filter(e=>e.outcome==='SL').length,time:ok.filter(e=>e.outcome==='TIME').length,avg:n?sum/n:NaN,winrate:n?ok.filter(e=>num(e.net_pct)>0).length/n*100:NaN,filterMissing:k==='base'?0:items.filter(e=>flag(e,k)===null).length};}
function oneTable(items){const base=metrics(items,'base');return table(['전략','통과','결과확정','미확정 손익','수익 거래 비율','평균 순손익','볼먼 대비 %p','TP/SL/TIME','이평 자료부족'],ARMS.map(([key,name])=>{const s=metrics(items,key);return row([name,s.signals,s.judged,s.unknown,pct(s.winrate),pct(s.avg),pct(Number.isFinite(s.avg)?s.avg-base.avg:NaN),`${s.tp}/${s.sl}/${s.time}`,s.filterMissing]);}));}
function paint(){if(!ready())return;const checked=seeds.filter(x=>Object.prototype.hasOwnProperty.call(state.results,x.id)).length;const allok=done();$('progress').max=seeds.length;$('progress').value=checked;$('csv').disabled=!allok;$('coverage').innerHTML=card('원래 볼먼 신호',seeds.length+'건')+card('12/200 검사 완료',checked+' / '+seeds.length)+card('200봉 연속 확보',seeds.filter(e=>['OK','PARTIAL_200','GAP_206'].includes(state.results[e.id]?.history_status)).length+'건')+card('200봉 연속 미확보',seeds.filter(e=>['GAP_200','SHORT','SIGNAL_CANDLE_NOT_FOUND'].includes(state.results[e.id]?.history_status)).length+'건');
$('status').textContent=(allok?'✅ 세 날짜의 모든 신호 검사 완료':running?'⏳ 과거 5분봉 확인 중':'검사 대기 / 이어하기 가능')+` · ${checked}/${seeds.length}건`+(allok?'\n기존 손익값은 변경하지 않았어. 이평 결과를 확인하고 CSV를 저장해줘.':'\n원본 볼먼 성적은 바로 보이고, 추가 조건은 검사 완료 후에 비교해줘.');
$('overall').innerHTML=oneTable(seeds)+(allok?'':'<p class="hint">※ 추가 필터 성적은 아직 일부만 검사한 임시 집계야.</p>');
$('byday').innerHTML=DAYS.map(d=>`<h3>${d} · 볼먼 ${seeds.filter(e=>e.date===d).length}건</h3>`+oneTable(seeds.filter(e=>e.date===d))).join('');
const checkedRows=seeds.filter(e=>state.results[e.id]);$('examples').innerHTML=checkedRows.length?table(['날짜','종목','신호 시각','볼먼 순손익','12>200','최근 상향교차','200상승','12이평','200이평','데이터 상태'],checkedRows.map(e=>{const r=state.results[e.id],f=k=>r[k]===null?'자료부족':r[k]?'O':'X';return row([e.date,e.market,e.signal_time_kst,pct(num(e.net_pct)),f('ma_above'),f('ma_cross6'),f('ma_slope6'),Number.isFinite(num(r.sma12))?num(r.sma12).toFixed(6):'—',Number.isFinite(num(r.sma200))?num(r.sma200).toFixed(6):'—',r.history_status]);})):'검사 시작 전';}
async function fetchJson(url,opts){const ac=new AbortController(),t=setTimeout(()=>ac.abort(),115000);try{const res=await fetch(url,{...opts,cache:'no-store',signal:ac.signal});const d=await res.json();if(!res.ok||!d.ok)throw Error(d.error||'HTTP '+res.status);return d;}finally{clearTimeout(t);}}
async function init(){const d=await fetchJson(API+'/seed');if(!Array.isArray(d.items)||d.items.length!==74)throw Error('기존 볼먼 CSV 신호 목록 오류');seeds=d.items;state=load();if(!state||state.source_sha!==d.source_sha)state={version:1,source_sha:d.source_sha,results:{}};save();paint();}
async function run(){if(running||!ready())return;running=true;stopRequested=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;const pending=seeds.filter(e=>!Object.prototype.hasOwnProperty.call(state.results,e.id));let cursor=0,failures=0;try{async function worker(){while(!stopRequested){const e=pending[cursor++];if(!e)return;for(let t=0;t<3&&!stopRequested;t++){try{const d=await fetchJson(API+'/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:e.id})});const r=d.result;if(r.id!==e.id||![true,false,null].includes(r.ma_above)||![true,false,null].includes(r.ma_cross6)||![true,false,null].includes(r.ma_slope6))throw Error('이평 판정 구조 불일치');state.results[e.id]=r;save();paint();break;}catch(err){if(t===2){failures++;$('status').textContent=`⚠️ ${e.market} ${e.signal_time_kst}: ${String(err.message||err)} · 다시 누르면 이어하기`;if(failures>=5){stopRequested=true;break;}}else await new Promise(resolve=>setTimeout(resolve,650*(t+1)));}}}}await Promise.all([worker(),worker()]);}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;paint();if(failures)$('status').textContent+=`
API 실패 ${failures}건 · 검사 시작을 다시 눌러 재시도`;}}
$('run').onclick=run;$('pause').onclick=()=>{stopRequested=true;$('pause').disabled=true;};$('reset').onclick=()=>{if(running)return;if(!confirm('12/200이평 검사 기록만 지울까? 기존 CSV 기초 데이터는 유지돼.'))return;state={version:1,source_sha:state.source_sha,results:{}};save();paint();};
function csvCell(v){let s=String(v??'');if(/^[\s]*[=+@]/.test(s)||(/^[\s]*-/.test(s)&&!/^[\s]*-\d+(?:\.\d+)?\s*$/.test(s)))s="'"+s;return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!done())return;const cols=['version','date','market','signal_time_kst','strategy','status','outcome','net_pct','ma_above','ma_cross6','ma_slope6','sma12','sma200','sma200_slope6_pct','distance12_200_pct','observed_bars','history_status','missing_intervals_206','entry','exit','minutes'];const lines=[cols.join(',')];for(const e of seeds){const r=state.results[e.id];for(const [key] of ARMS){const f=flag(e,key),o={version:'V11.14.23',...e,...r,strategy:key,status:f===null?'FEATURE_UNAVAILABLE':f?'ACCEPTED':'BLOCKED'};lines.push(cols.map(k=>csvCell(o[k])).join(','));}}const blob=new Blob(['﻿'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='bithumb_C5_volman_MA12_200_2026-11-08_09_11_v111423.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),2500);};
init().catch(e=>{$('status').textContent='⚠️ '+String(e.message||e);});
</script></body></html>
'''.replace('/*REPLACE_STYLE*/', C5_V111417_PAGE.split('<style>',1)[1].split('</style>',1)[0])


def register_c5_ma_v111423_lab(app, deps):
    api_get, kst = deps['api_get'], deps['KST']

    @app.get('/backtest-winrate-c5-ma12200')
    def c5_ma_v111423_page():
        return render_template_string(C5_MA_V111423_PAGE)

    @app.get('/api/winrate14c5ma12200/seed')
    def c5_ma_v111423_seed():
        return jsonify({'ok': True, 'rule_version': 'V11.14.23',
                        'source_sha': C5_MA_V111423_SEED_SHA,
                        'items': C5_MA_V111423_SEEDS})

    @app.post('/api/winrate14c5ma12200/evaluate')
    def c5_ma_v111423_evaluate():
        try:
            raw = request.get_json(silent=True)
            event_id = raw.get('id') if isinstance(raw, dict) else None
            if type(event_id) is not int or event_id not in C5_MA_V111423_BY_ID:
                raise ValueError('허용된 74개 신호 중 하나를 선택해 주세요')
            result = c5_ma_v111423_features(
                api_get, C5_MA_V111423_BY_ID[event_id], kst)
            return jsonify({'ok': True, 'result': result,
                            'rule_version': 'V11.14.23'})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:180]}), 400


_v111422_register_daytrade_review = register_daytrade_review


def register_daytrade_review(app, deps):
    _v111422_register_daytrade_review(app, deps)
    register_c5_ma_v111423_lab(app, deps)


# ---------------------------------------------------------------------------
# V11.14.24 - Research only: 5-minute SMA12/200 history continuity recheck.
# Root cause of V11.14.23 CSV GAP_200: 400 observed candles were returned,
# but absent time slots broke the strictly continuous 200/206-candle window.
# No attempt is made to fabricate observed exchange candles. This lab has
# two visibly distinguished data classes: raw-continuous, and cautiously
# reconstructed no-trade assumptions (last observed close; zero-volume slot).
# Reconstructed values are a sensitivity analysis, NOT verified exchange OHLC.
# Existing V2 scanner and every prior lab are preserved byte-for-byte.
# ---------------------------------------------------------------------------

C5_MA_V111424_MAX_MISSING = 20        # <=9.7% of 206 five-minute slots
C5_MA_V111424_MAX_GAP_RUN = 3        # <=15 minutes consecutive unobserved


def c5_ma_v111424_features(api_get, event, kst):
    """Compute SMA12/200 on 206 historical 5m slots, never after signal time.

    A missing 5m slot may indicate no trades OR an unavailable API candle.
    For transparency, only short/sparse gaps use previous-close assumption.
    Longer gaps are 'UNVERIFIED_GAP' and no MA signal is assigned.
    """
    market = event['market']
    signal = local_time(event['signal_time_kst'], kst)
    step = timedelta(minutes=5)
    if signal.second or signal.microsecond or signal.minute % 5:
        raise ValueError('신호 시각이 5분봉 기준에 맞지 않습니다')
    start = signal - 205*step
    exclusive = signal + step
    cursor = exclusive
    found = {}
    pages = 0
    earliest = None
    # Two pages normally suffice. Extra pages only when 206-slot window
    # and a preceding close have not been reached because of sparse trading.
    for _ in range(4):
        raw = api_get('/v1/candles/minutes/5', {
            'market': market, 'count': 200,
            'to': cursor.strftime('%Y-%m-%d %H:%M:%S'),
        })
        pages += 1
        if not isinstance(raw, list):
            raise ValueError('빗썸 캔들 응답이 목록이 아닙니다')
        page_oldest = None
        for b in raw:
            if not isinstance(b, dict):
                continue
            try:
                t = local_time(b['candle_date_time_kst'], kst)
                price = float(b['trade_price'])
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
            if not isfinite(price) or price <= 0 or t >= exclusive:
                continue
            if t not in found:
                found[t] = price
            if page_oldest is None or t < page_oldest:
                page_oldest = t
        if page_oldest is None:
            break
        if page_oldest >= cursor:
            raise ValueError('캔들 페이지 진행이 멈췄습니다')
        earliest = page_oldest if earliest is None or page_oldest < earliest else earliest
        if earliest <= start or len(raw) < 200:
            break
        cursor = page_oldest

    result = {
        'id': event['id'], 'signal_time_kst': event['signal_time_kst'],
        'history_status': 'SOURCE_SHORT', 'data_quality': 'UNAVAILABLE',
        'pages': pages, 'observed_bars': len(found),
        'actual_slots_206': None, 'filled_slots_206': None,
        'max_gap_run': None, 'strict200_ok': False, 'strict206_ok': False,
        'sma12': None, 'sma200': None,
        'sma200_slope6_pct': None, 'distance12_200_pct': None,
        'ma_above': None, 'ma_cross6': None, 'ma_slope6': None,
        'signal_close': found.get(signal),
    }
    if signal not in found:
        result['history_status'] = 'SIGNAL_NOT_OBSERVED'
        return result
    if earliest is None or earliest > start:
        return result

    # Need an observed candle at or before window start to avoid guessing
    # its initial close when the first slot is empty.
    prior_times = [t for t in found if t <= start]
    if not prior_times:
        return result
    prior_time = max(prior_times)
    last_close = found[prior_time]
    closes = []
    missing = []
    longest = run = 0
    for j in range(206):
        t = start + j * step
        if t in found:
            last_close = found[t]
            run = 0
        else:
            missing.append(j)
            run += 1
            longest = max(longest, run)
        closes.append(last_close)
    n_missing = len(missing)
    raw200_ok = not any(j >= 6 for j in missing)
    raw206_ok = not missing
    result.update(actual_slots_206=206-n_missing,
                  filled_slots_206=n_missing,
                  max_gap_run=longest,
                  strict200_ok=raw200_ok,
                  strict206_ok=raw206_ok)

    if n_missing > C5_MA_V111424_MAX_MISSING or longest > C5_MA_V111424_MAX_GAP_RUN:
        result['history_status'] = 'UNVERIFIED_GAP'
        return result

    ma12 = sum(closes[-12:]) / 12
    ma200 = sum(closes[-200:]) / 200
    earlier200 = sum(closes[:200]) / 200
    crossed = False
    for end in range(201, 207):
        prev12 = sum(closes[end-13:end-1]) / 12
        prev200 = sum(closes[end-201:end-1]) / 200
        now12 = sum(closes[end-12:end]) / 12
        now200 = sum(closes[end-200:end]) / 200
        if prev12 <= prev200 and now12 > now200:
            crossed = True
            break
    result.update(
        history_status='RAW_OK' if raw206_ok else 'REBUILT_5M',
        data_quality='RAW' if raw206_ok else 'CARRY_FORWARD_ASSUMPTION',
        sma12=round(ma12, 10), sma200=round(ma200, 10),
        sma200_slope6_pct=round((ma200 / earlier200 - 1) * 100, 6),
        distance12_200_pct=round((ma12 / ma200 - 1) * 100, 6),
        ma_above=bool(ma12 > ma200),
        ma_cross6=bool(ma12 > ma200 and crossed),
        ma_slope6=bool(ma12 > ma200 and ma200 > earlier200),
    )
    return result


C5_MA_V111424_PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.24 · C5 볼먼 12/200이평 공백 재검증</title>
<style>/*REPLACE_STYLE*/</style></head><body>
<main class="wrap"><header><div><small>연구 검증실 · 기존 V2 변경 없음</small>
<h1>🔬 V11.14.24 · 볼먼 + 12/200이평 재검증</h1>
<p>11월 8·9·11일 기존 볼먼 신호 74건 · 과거 5분봉 공백 진단 및 보수적 보완</p></div>
<a class="btn secondary" href="/">V2 스캐너</a></header>
<section class="panel"><h2>검증 방식</h2>
<p>기존 V11.14.23에서 74건 모두 400개의 거래 캔들을 조회했지만, 5분 간격이 끊긴 사례가 많았어.
이번에는 <b>실제 연속 5분봉(RAW)</b>과 <b>중간 5분 구간에 직전 종가를 대입한 보완 계산(가정)</b>을 분리해.</p>
<p class="hint">⚠️ 보완 값은 거래소가 제공한 실제 봉이 아니야. 거래 미발생과 데이터 장애를 확실히 구별할 수 없어서, 빠진 구간이 206봉 중 20개를 넘거나 3봉 연속 넘으면 판정하지 않아.
신호 당일 이후 봉은 사용하지 않아. 이 연구는 거래 추천이 아니야.</p>
<div class="actions"><button id="run">74건 재검사 / 이어하기</button><button class="secondary" id="pause" disabled>일시 중지</button><button class="secondary" id="reset">이 검증 초기화</button><button class="secondary" id="csv" disabled>결과 CSV 저장</button></div>
<progress id="progress" max="74" value="0"></progress><div class="status" id="status">원본 신호 불러오는 중</div></section>
<section class="panel"><h2>1. 자료 확보 현황</h2><div id="coverage" class="stats">검사 전</div></section>
<section class="panel"><h2>2. 3일 합산 성적 (비교 가능한 표본 표시)</h2><div id="overall" class="scroll">검사 전</div></section>
<section class="panel"><h2>3. 날짜별 결과</h2><div id="byday" class="scroll">검사 전</div></section>
<section class="panel"><h2>4. 신호별 이평 판정과 공백</h2><div id="examples" class="scroll">검사 전</div></section>
</main><script>
'use strict';
const API='/api/winrate14c5ma12200v2',KEY='bithumb_v111424_c5_ma_history_v1',DAYS=['2026-11-08','2026-11-09','2026-11-11'];
const $=id=>document.getElementById(id),num=v=>v==null||v===''?NaN:Number(v);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=x=>Number.isFinite(x)?x.toFixed(3)+'%':'—';
const ARMS=[['base','원본 볼먼 전체'],['comparable','볼먼 (동일 비교표본)'],['ma_above','볼먼 + 12>200'],['ma_cross6','볼먼 + 최근 6봉 상향교차'],['ma_slope6','볼먼 + 12>200·200상승']];
let seeds=[],state=null,running=false,stopping=false;
function card(a,b){return `<div class="stat"><small>${esc(a)}</small><strong>${esc(b)}</strong></div>`;}
function row(xs){return '<tr>'+xs.map(x=>'<td>'+esc(x)+'</td>').join('')+'</tr>';}
function table(h,rs){return '<table><thead><tr>'+h.map(x=>'<th>'+esc(x)+'</th>').join('')+'</tr></thead><tbody>'+rs.join('')+'</tbody></table>';}
function load(){try{const x=JSON.parse(localStorage.getItem(KEY)||'null');return x&&x.version===1&&x.results&&typeof x.results==='object'?x:null;}catch{return null;}}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function done(){return seeds.length===74&&seeds.every(e=>Object.prototype.hasOwnProperty.call(state.results,e.id));}
function flag(e,k){if(k==='base')return true;const r=state.results[e.id];if(!r)return null;if(k==='comparable')return r.data_quality!=='UNAVAILABLE';return r[k];}
function metrics(items,k){const taken=items.filter(e=>flag(e,k)===true),judged=taken.filter(e=>e.outcome!=='UNKNOWN'&&Number.isFinite(num(e.net_pct))),n=judged.length,sum=judged.reduce((s,e)=>s+num(e.net_pct),0);return {total:taken.length,judged:n,unknown:taken.length-n,win:judged.filter(e=>num(e.net_pct)>0).length,avg:n?sum/n:NaN,tp:judged.filter(e=>e.outcome==='TP').length,sl:judged.filter(e=>e.outcome==='SL').length,time:judged.filter(e=>e.outcome==='TIME').length,unavailable:k==='base'?0:items.filter(e=>flag(e,k)===null).length};}
function report(items){const baseline=metrics(items,'comparable');return table(['전략','통과','손익확정','수익비율','평균순손익','동일표본 볼먼 대비','TP/SL/TIME','이평 미확인'],ARMS.map(([key,title])=>{const m=metrics(items,key);return row([title,m.total,m.judged,pct(m.judged?m.win/m.judged*100:NaN),pct(m.avg),pct(Number.isFinite(m.avg)&&Number.isFinite(baseline.avg)?m.avg-baseline.avg:NaN),`${m.tp}/${m.sl}/${m.time}`,m.unavailable]);}));}
function paint(){if(!seeds.length||!state)return;const checked=seeds.filter(e=>state.results[e.id]).length,finished=done();$('csv').disabled=!finished;$('progress').max=74;$('progress').value=checked;const r=seeds.map(e=>state.results[e.id]).filter(Boolean);const raw=r.filter(x=>x.data_quality==='RAW').length,filled=r.filter(x=>x.data_quality==='CARRY_FORWARD_ASSUMPTION').length,missing=r.filter(x=>x.data_quality==='UNAVAILABLE').length;const totalGap=r.reduce((a,x)=>a+(x.filled_slots_206||0),0);
$('coverage').innerHTML=card('원본 볼먼 신호',seeds.length+'건')+card('재검사',checked+'/74')+card('원본 연속 206봉',raw+'건')+card('단기 공백 가정보완',filled+'건')+card('판정 불가',missing+'건')+card('누락 슬롯 합계',totalGap+'개');
$('status').textContent=(finished?'✅ 74건 검사 완료':running?'⏳ 당시 5분봉 확인 중':'검사 준비 / 이어하기 가능')+` · ${checked}/74건`+(finished?'\n원본과 가정 보완 결과를 구분해서 해석하고 CSV를 저장해줘.':'\n신호 성과는 이전 CSV의 원본 값을 그대로 사용해.');
$('overall').innerHTML=report(seeds)+(finished?'':'<p class="hint">추가 전략 성적은 검사 중인 일부 자료만 반영했어.</p>')+'<p class="hint">동일표본 대비는 같은 자료 확인 가능한 볼먼 신호들과 비교한 값이야. 서로 다른 필터 통과 집합이므로 성과 향상을 단정할 수 없어.</p>';
$('byday').innerHTML=DAYS.map(d=>`<h3>${d}</h3>`+report(seeds.filter(e=>e.date===d))).join('');
$('examples').innerHTML=r.length?table(['날짜','종목','원본 순손익','12>200','6봉 교차','200상승','실제 5분봉','빈 구간','최대 연속 공백','자료 판정'],seeds.filter(e=>state.results[e.id]).map(e=>{const x=state.results[e.id],f=k=>x[k]===null?'미확인':x[k]?'O':'X';return row([e.date,e.market,pct(num(e.net_pct)),f('ma_above'),f('ma_cross6'),f('ma_slope6'),x.actual_slots_206??'—',x.filled_slots_206??'—',x.max_gap_run??'—',x.history_status]);})):'검사 전';}
async function fetchJson(url,opts={}){const c=new AbortController(),t=setTimeout(()=>c.abort(),110000);try{const resp=await fetch(url,{...opts,cache:'no-store',signal:c.signal});const d=await resp.json();if(!resp.ok||!d.ok)throw Error(d.error||`HTTP ${resp.status}`);return d;}finally{clearTimeout(t);}}
async function init(){const d=await fetchJson(API+'/seed');if(!Array.isArray(d.items)||d.items.length!==74)throw Error('원본 74건을 불러올 수 없어');seeds=d.items;state=load();if(!state||state.source_sha!==d.source_sha)state={version:1,source_sha:d.source_sha,results:{}};save();paint();}
async function run(){if(running||!seeds.length)return;running=true;stopping=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;const pending=seeds.filter(e=>!state.results[e.id]);let index=0,errors=0;try{async function worker(){while(!stopping){const e=pending[index++];if(!e)return;for(let attempt=0;attempt<3&&!stopping;attempt++){try{const d=await fetchJson(API+'/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:e.id})});const r=d.result;if(r.id!==e.id||![true,false,null].includes(r.ma_above)||![true,false,null].includes(r.ma_cross6)||![true,false,null].includes(r.ma_slope6))throw Error('판정 형식 오류');state.results[e.id]=r;save();paint();break;}catch(err){if(attempt===2){errors++;$('status').textContent=`⚠️ ${e.market}: ${String(err.message||err)} · 다시 검사하면 이어서 진행`;if(errors>=5)stopping=true;}else await new Promise(resolve=>setTimeout(resolve,700*(attempt+1)));}}}}await Promise.all([worker(),worker()]);}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;paint();if(errors)$('status').textContent+=`\n실패 ${errors}건 · 다시 눌러 이어하기`;}}
$('run').onclick=run;$('pause').onclick=()=>{stopping=true;$('pause').disabled=true;};$('reset').onclick=()=>{if(running||!state)return;if(!confirm('이번 12/200 공백 재검사 기록만 초기화할까?'))return;state={version:1,source_sha:state.source_sha,results:{}};save();paint();};
function csvCell(v){let s=String(v??'');if(/^[\s]*[=+@]/.test(s)||(/^[\s]*-/.test(s)&&!/^[\s]*-\d+(?:\.\d+)?\s*$/.test(s)))s="'"+s;return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!done())return;const cols=['version','date','market','signal_time_kst','strategy','status','outcome','net_pct','data_quality','history_status','actual_slots_206','filled_slots_206','max_gap_run','strict200_ok','strict206_ok','ma_above','ma_cross6','ma_slope6','sma12','sma200','sma200_slope6_pct','distance12_200_pct','observed_bars','pages','entry','exit','minutes'];const lines=[cols.join(',')];for(const e of seeds){const r=state.results[e.id];for(const [key] of ARMS){const f=flag(e,key),o={...e,...r,version:'V11.14.24',strategy:key,status:f===null?'FEATURE_UNAVAILABLE':f?'ACCEPTED':'BLOCKED'};lines.push(cols.map(k=>csvCell(o[k])).join(','));}}const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download='bithumb_C5_volman_MA12_200_HISTORY_v111424.csv';a.click();setTimeout(()=>URL.revokeObjectURL(u),2000);};
init().catch(e=>{$('status').textContent='⚠️ '+String(e.message||e);});
</script></body></html>""".replace('/*REPLACE_STYLE*/', C5_V111417_PAGE.split('<style>',1)[1].split('</style>',1)[0])


def register_c5_ma_v111424_lab(app, deps):
    api_get, kst = deps['api_get'], deps['KST']

    @app.get('/backtest-winrate-c5-ma12200-history')
    def c5_ma_v111424_page():
        return render_template_string(C5_MA_V111424_PAGE)

    @app.get('/api/winrate14c5ma12200v2/seed')
    def c5_ma_v111424_seed():
        return jsonify({'ok': True, 'rule_version': 'V11.14.24',
                        'source_sha': C5_MA_V111423_SEED_SHA,
                        'items': C5_MA_V111423_SEEDS})

    @app.post('/api/winrate14c5ma12200v2/evaluate')
    def c5_ma_v111424_evaluate():
        try:
            body = request.get_json(silent=True)
            event_id = body.get('id') if isinstance(body, dict) else None
            if type(event_id) is not int or event_id not in C5_MA_V111423_BY_ID:
                raise ValueError('74개 기존 신호 중 하나를 지정해 주세요')
            r = c5_ma_v111424_features(api_get, C5_MA_V111423_BY_ID[event_id], kst)
            return jsonify({'ok': True, 'rule_version': 'V11.14.24', 'result': r})
        except Exception as exc:
            return jsonify({'ok': False, 'error': str(exc)[:180]}), 400


_v111423_register_daytrade_review = register_daytrade_review


def register_daytrade_review(app, deps):
    _v111423_register_daytrade_review(app, deps)
    register_c5_ma_v111424_lab(app, deps)


# ---------------------------------------------------------------------------
# V11.14.25 FINAL, ONE-OFF TEST ONLY:
# C5 + Volman compression breakout, with SMA12/SMA200 crossover,
# 200-SMA retest support and renewed price breakout.
# Reuses identical 74 base signal IDs and *unchanged* historical outcomes
# (Nov 8, 9, 11) from V11.14.22; never alters live V2 app.py.
# Pre-registered research rules; not a profitable-strategy claim.
# Distinguish 241-slot fully observed RAW from carry-forward assumptions.
# Recent 31 bars must be real observed OHLC; no lookahead after signal.
# ---------------------------------------------------------------------------

C5_MA_V111425_SLOTS = 241             # 20 hours and five minutes of 5m slots
C5_MA_V111425_MAX_MISSING = 24        # <= ~10% of all slots
C5_MA_V111425_MAX_GAP_RUN = 3        # <=3 adjacent missing slots
C5_MA_V111425_RECENT_RAW = 31        # prior 30 and signal candle must be real
C5_MA_V111425_CROSS_LOOKBACK = 30    # <=150 minutes before the C5 signal
C5_MA_V111425_RETEST_LOOKBACK = 12   # <=60 minutes before the C5 signal
C5_MA_V111425_TOUCH_ABOVE = 1.005   # prior wick <= SMA200 +0.5%
C5_MA_V111425_CLOSE_FLOOR = 0.997   # prior close >= SMA200 -0.3%


def c5_ma_v111425_classify(slots):
    """Pure causal classifier for chronological 241 fully indexed 5m slots.

    A slot is {'open','high','low','close','raw'}. Older slots may be
    reconstructed, but last 31 must be genuine OHLC. No future bars.
    """
    n = len(slots)
    if n != C5_MA_V111425_SLOTS:
        raise ValueError('241개 5분 구간이 필요합니다')
    prices = [float(s['close']) for s in slots]
    prefix = [0.0]
    for p in prices:
        prefix.append(prefix[-1] + p)
    def ma(i, period):
        if i + 1 < period:
            return None
        return (prefix[i+1] - prefix[i+1-period]) / period
    m12 = [ma(i, 12) for i in range(n)]
    m200 = [ma(i, 200) for i in range(n)]
    last = n-1
    above = m12[last] > m200[last]
    previous = slots[last-3:last]
    candle = slots[last]
    # A deliberately explicit restart, separate from the earlier Volman test.
    restart = (float(candle['close']) > float(candle['open']) and
               float(candle['close']) > m12[last] and
               float(candle['close']) > m200[last] and
               float(candle['close']) > max(float(b['high']) for b in previous))
    # Search most recent *observed* crossing, where an MA200 is defined
    # on both sides of the crossover. The signal candle is NOT the retest.
    crossing = []
    for i in range(max(200, last-C5_MA_V111425_CROSS_LOOKBACK), last):
        if m12[i-1] <= m200[i-1] and m12[i] > m200[i]:
            crossing.append(i)
    recent_cross = crossing[-1] if crossing else None
    def touching(i):
        return (float(slots[i]['low']) <= m200[i]*C5_MA_V111425_TOUCH_ABOVE
                and float(slots[i]['close']) >= m200[i]*C5_MA_V111425_CLOSE_FLOOR
                and m12[i] >= m200[i])
    recent_start = last-C5_MA_V111425_RETEST_LOOKBACK
    any_retest = [i for i in range(recent_start, last) if touching(i)]
    post_cross = [i for i in any_retest if recent_cross is not None and i > recent_cross]
    simple = bool(above and restart and any_retest)
    strict = bool(above and restart and post_cross)
    return {
        'sma12':round(m12[last], 10), 'sma200':round(m200[last], 10),
        'ma_above':bool(above), 'relaunch_pass':bool(restart),
        'retest_relaunch':simple, 'cross_retest_relaunch':strict,
        'cross_bars_ago':last-recent_cross if recent_cross is not None else None,
        'retest_bars_ago':last-any_retest[-1] if any_retest else None,
        'strict_retest_bars_ago':last-post_cross[-1] if post_cross else None,
        'signal_close':float(candle['close']),
    }


def c5_ma_v111425_features(api_get, event, kst):
    """Fetch only as-of C5 signal; maintain API candles' 5m timestamps.

    Missing old slots: optional carry-forward sensitivity only. A missing
    recent pattern bar returns UNAVAILABLE, never a false negative.
    """
    signal=local_time(event['signal_time_kst'],kst)
    step=timedelta(minutes=5)
    if signal.minute%5 or signal.second or signal.microsecond:
        raise ValueError('5분봉에 맞지 않는 신호 시각입니다')
    start=signal-(C5_MA_V111425_SLOTS-1)*step
    exclusive=signal+step
    cursor=exclusive
    found={}
    pages=0
    oldest=None
    for _ in range(5):
        raw=api_get('/v1/candles/minutes/5', {
            'market':event['market'], 'count':200,
            'to':cursor.strftime('%Y-%m-%d %H:%M:%S'),
        })
        pages+=1
        if not isinstance(raw,list):
            raise ValueError('빗썸 5분봉 응답 형식 오류')
        page_oldest=None
        for b in raw:
            if not isinstance(b,dict):
                continue
            try:
                t=local_time(b['candle_date_time_kst'],kst)
                o=float(b['opening_price'])
                h=float(b['high_price'])
                l=float(b['low_price'])
                c=float(b['trade_price'])
            except (KeyError,ValueError,TypeError,OverflowError):
                continue
            if not (all(isfinite(v) and v>0 for v in (o,h,l,c)) and
                    l<=min(o,c) and h>=max(o,c) and t<exclusive):
                continue
            if t not in found:
                found[t]={'open':o,'high':h,'low':l,'close':c,'raw':True}
            if page_oldest is None or t<page_oldest:
                page_oldest=t
        if page_oldest is None:
            break
        if page_oldest>=cursor:
            raise ValueError('캔들 페이지 진행이 멈췄습니다')
        oldest=page_oldest if oldest is None or page_oldest<oldest else oldest
        if oldest <= start or len(raw)<200:
            break
        cursor=page_oldest

    result={
        'id':event['id'], 'signal_time_kst':event['signal_time_kst'],
        'history_status':'SOURCE_SHORT','data_quality':'UNAVAILABLE',
        'pages':pages,'observed_bars':len(found),
        'actual_slots_241':None,'filled_slots_241':None,'max_gap_run':None,
        'recent_31_raw':False,
        'sma12':None,'sma200':None,'ma_above':None,
        'relaunch_pass':None,'retest_relaunch':None,
        'cross_retest_relaunch':None,'cross_bars_ago':None,
        'retest_bars_ago':None,'strict_retest_bars_ago':None,
        'signal_close':None,
    }
    if signal not in found:
        result['history_status']='SIGNAL_NOT_OBSERVED'
        return result
    if oldest is None or oldest > start:
        return result
    prior=[t for t in found if t<=start]
    if not prior:
        return result
    last_close=found[max(prior)]['close']
    slots=[]
    missing=[]
    longest=run=0
    for i in range(C5_MA_V111425_SLOTS):
        t=start+i*step
        if t in found:
            candle=found[t]
            last_close=candle['close']
            run=0
        else:
            # Never pretend this was an observed candle.
            candle={'open':last_close,'high':last_close,'low':last_close,
                    'close':last_close,'raw':False}
            missing.append(i)
            run+=1
            longest=max(longest,run)
        slots.append(candle)
    result.update(actual_slots_241=C5_MA_V111425_SLOTS-len(missing),
                  filled_slots_241=len(missing),max_gap_run=longest,
                  recent_31_raw=all(s['raw'] for s in slots[-C5_MA_V111425_RECENT_RAW:]))
    if len(missing)>C5_MA_V111425_MAX_MISSING or longest>C5_MA_V111425_MAX_GAP_RUN:
        result['history_status']='UNVERIFIED_GAP'
        return result
    if not result['recent_31_raw']:
        result['history_status']='RECENT_PATTERN_GAP'
        return result
    computed=c5_ma_v111425_classify(slots)
    result.update(computed)
    if not missing:
        result.update(history_status='RAW_OK',data_quality='RAW')
    else:
        result.update(history_status='OLDER_GAP_ASSUMPTION',
                      data_quality='CARRY_FORWARD_ASSUMPTION')
    return result


C5_MA_V111425_PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.14.25 마지막 12/200 재지지·재출발 검증</title>
<style>/*REPLACE_STYLE*/</style></head><body>
<main class="wrap"><header><div><small>7일 연구 후 마지막 일회성 검증 · V2 실전 스캐너 변경 없음</small>
<h1>🔬 V11.14.25 · 볼먼 + 12/200 돌파·재지지·재출발</h1>
<p>기존 볼먼 통과 신호 74건 · 2026년 11월 8·9·11일</p></div>
<a class="btn secondary" href="/">V2 스캐너</a></header>
<section class="panel"><h2>이번 한 번의 검증 기준 (미리 고정)</h2>
<p><b>① 기본 볼먼</b> · 기존 C5+압축 돌파에서 이미 발생한 동일한 74건<br>
<b>② 12 &gt; 200</b> · 신호 당시 SMA12가 SMA200 위<br>
<b>③ 재지지→재출발</b> · 위 조건 + 최근 12봉 이내 저가가 200이평의 +0.5% 안쪽, 해당 봉 종가는 200이평 -0.3% 이상 + 신호봉 양봉·SMA12 위·직전 3개 봉 고가 돌파<br>
<b>④ 최근 교차→재지지→재출발</b> · ③ 조건 + 신호 전 최근 30봉 안에 SMA12가 SMA200 상향 교차, 교차 <em>이후</em> 지지 확인</p>
<p class="hint">⚠️ 과거 신호를 새 기준으로 <b>분류</b>할 뿐, 새로운 매수가격이나 진입 시각을 가정하지 않아. 기존 V11.14.22 순손익을 그대로 사용해. 신호 이후 캔들은 지표 계산에 사용하지 않아.</p>
<p class="hint">⚠️ 최근 31개 패턴 봉은 실제 관측된 5분봉만 사용. 앞선 장기 이평 구간에 빈 봉이 있으면 직전 종가로 가정한 계산을 <b>별도 표</b>에 표시. 미확인과 손실을 혼동하지 않아.</p>
<div class="actions"><button id="run" disabled>74건 최종 검사 / 이어하기</button><button class="secondary" id="pause" disabled>일시 중지</button><button class="secondary" id="reset" disabled>이 검증 초기화</button><button class="secondary" id="csv" disabled>CSV 다운로드</button></div>
<progress id="progress" max="74" value="0"></progress><div class="status" id="status">신호 불러오는 중</div></section>
<section class="panel"><h2>1. 자료 확보 현황</h2><div id="coverage" class="stats">검사 전</div></section>
<section class="panel"><h2>2. 실제 연속 봉만 사용한 RAW 결과 — 최우선</h2><div id="raw_report" class="scroll">검사 전</div></section>
<section class="panel"><h2>3. 과거 봉 공백 보완 결과 — 참고용</h2><div id="assumed_report" class="scroll">검사 전</div></section>
<section class="panel"><h2>4. 날짜별 RAW 결과</h2><div id="daily" class="scroll">검사 전</div></section>
<section class="panel"><h2>5. 신호별 판정</h2><div id="details" class="scroll">검사 전</div></section>
</main><script>
'use strict';
const API='/api/winrate14c5ma12200final',KEY='bithumb_v111425_c5_ma_final_v1';
const DAYS=['2026-11-08','2026-11-09','2026-11-11'];
const ARMS=[['base','볼먼 원래 74건 (참고)'],['comparable','볼먼 (동일 자료)'],['ma_above','볼먼 + 12>200'],['retest_relaunch','볼먼 + 재지지·재출발'],['cross_retest_relaunch','볼먼 + 최근교차·재지지·재출발']];
const $=id=>document.getElementById(id), num=x=>x==null||x===''?NaN:Number(x);
const fmt=x=>Number.isFinite(x)?x.toFixed(3)+'%':'—';
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const row=xs=>'<tr>'+xs.map(x=>'<td>'+esc(x)+'</td>').join('')+'</tr>';
const table=(h,rs)=>'<table><thead><tr>'+h.map(x=>'<th>'+esc(x)+'</th>').join('')+'</tr></thead><tbody>'+rs.join('')+'</tbody></table>';
const card=(a,b)=>`<div class="stat"><small>${esc(a)}</small><strong>${esc(b)}</strong></div>`;
let seeds=[],state=null,running=false,stopping=false;
function load(){try{let x=JSON.parse(localStorage.getItem(KEY)||'null');return x&&x.version===1&&x.results&&typeof x.results==='object'?x:null;}catch{return null;}}
function save(){localStorage.setItem(KEY,JSON.stringify(state));}
function ready(){return seeds.length===74 && seeds.every(e=>Object.prototype.hasOwnProperty.call(state.results,e.id));}
function flag(e,k){if(k==='base')return true;let r=state.results[e.id];if(!r)return null;if(k==='comparable')return r.data_quality!=='UNAVAILABLE';if(r.data_quality==='UNAVAILABLE')return null;return r[k];}
function stats(items,k){let eligible=items.filter(e=>flag(e,k)===true);let done=eligible.filter(e=>e.outcome!=='UNKNOWN'&&Number.isFinite(num(e.net_pct)));let sum=done.reduce((s,e)=>s+num(e.net_pct),0);let win=done.filter(e=>num(e.net_pct)>0).length;return {passed:eligible.length,judged:done.length,win,avg:done.length?sum/done.length:NaN, tp:done.filter(e=>e.outcome==='TP').length,sl:done.filter(e=>e.outcome==='SL').length,time:done.filter(e=>e.outcome==='TIME').length};}
function report(items){let base=stats(items,'comparable');let rows=ARMS.map(([key,title])=>{let x=stats(items,key);return row([title,x.passed,x.judged,x.judged?((x.win/x.judged)*100).toFixed(1)+'%':'—',fmt(x.avg),key==='base'?'—':(Number.isFinite(x.avg)&&Number.isFinite(base.avg)?fmt(x.avg-base.avg):'—'),`${x.tp}/${x.sl}/${x.time}`]);});return table(['전략','통과','손익 확정','수익비율','평균 순손익','동일자료 볼먼 대비','TP/SL/TIME'],rows);}
function filtered(quality){return seeds.filter(e=>state.results[e.id]?.data_quality===quality);}
function paint(){if(!seeds.length||!state)return;let checked=seeds.filter(e=>state.results[e.id]).length;let fin=ready();$('progress').value=checked;$('csv').disabled=!fin;let raws=filtered('RAW'), assumed=filtered('CARRY_FORWARD_ASSUMPTION'), unknown=seeds.length-raws.length-assumed.length;
$('coverage').innerHTML=card('원본 볼먼 신호','74건')+card('검사 완료',checked+'/74')+card('RAW 완전 연속봉',raws.length+'건')+card('장기봉 가정보완',assumed.length+'건')+card('판정 불가',unknown+'건');
let rawStrict=stats(raws,'cross_retest_relaunch');let rawRelaxed=stats(raws,'retest_relaunch');let base=stats(raws,'comparable');
let advice=fin? (rawStrict.judged<20&&rawRelaxed.judged<20?'⚠️ 새 조건의 RAW 확정 거래가 20건 미만이야. 좋은 수익률이 나와도 개선 입증 불가. 12/200 필터는 실전 도입 보류.':(rawStrict.avg>0&&rawStrict.avg>base.avg?'조건부 개선 신호가 있지만, 선택한 3일 데이터라 새 날짜의 독립 검증 전에는 실전 적용하지 않아.':'RAW 기준 개선이 확인되지 않았어. 12/200 추가 도입 보류.')):'검사가 끝나기 전 숫자는 중간 결과야.';
$('status').textContent=(fin?'✅ 74건 검사 완료':running?'⏳ 과거 5분봉 조회 중':'검사 대기 / 이어하기 가능')+` · ${checked}/74건`+'\n'+advice;
$('raw_report').innerHTML=report(raws)+'<p class="hint">RAW는 최근 241개 5분 구간 모두 원본 관측. 선택된 결과를 보고 고른 세 날짜이므로 독립적인 수익성 검증은 아님.</p>';
$('assumed_report').innerHTML=report(assumed)+'<p class="hint">과거 일부 5분봉을 직전 종가로 채운 가정 계산. 실제 거래소 봉과 동일하게 취급하면 안 돼.</p>';
$('daily').innerHTML=DAYS.map(d=>`<h3>${esc(d)}</h3>`+report(raws.filter(e=>e.date===d))).join('');
let observed=seeds.filter(e=>state.results[e.id]);$('details').innerHTML=observed.length?table(['날짜','종목','손익','자료 분류','12>200','재지지·재출발','교차→재지지→재출발','교차 몇 봉 전','재지지 몇 봉 전','구간 누락','판정 이유'],observed.map(e=>{let r=state.results[e.id];let p=k=>r[k]==null?'미확인':r[k]?'YES':'NO';return row([e.date,e.market,Number.isFinite(num(e.net_pct))?fmt(num(e.net_pct)):'UNKNOWN',r.data_quality,p('ma_above'),p('retest_relaunch'),p('cross_retest_relaunch'),r.cross_bars_ago??'—',r.retest_bars_ago??'—',r.filled_slots_241??'—',r.history_status]);})):'검사 전';
}
async function fetchJson(url,opts={}){const ctrl=new AbortController(),t=setTimeout(()=>ctrl.abort(),110000);try{const x=await fetch(url,{...opts,cache:'no-store',signal:ctrl.signal});const d=await x.json();if(!x.ok||!d.ok)throw Error(d.error||'API 요청 오류');return d;}finally{clearTimeout(t);}}
async function init(){let d=await fetchJson(API+'/seed');if(!Array.isArray(d.items)||d.items.length!==74)throw Error('기존 74건 불러오기 실패');seeds=d.items;state=load();if(!state||state.source_sha!==d.source_sha)state={version:1,source_sha:d.source_sha,results:{}};$('run').disabled=false;$('reset').disabled=false;paint();}
async function run(){if(running||!seeds.length)return;running=true;stopping=false;$('run').disabled=true;$('pause').disabled=false;$('reset').disabled=true;let pending=seeds.filter(e=>!state.results[e.id]),idx=0,failed=0;paint();async function worker(){while(!stopping&&idx<pending.length){let e=pending[idx++];try{let d=await fetchJson(API+'/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:e.id})});state.results[e.id]=d.result;save();}catch(err){failed++;if(failed<=3)$('status').textContent='⚠️ '+String(err.message||err);if(failed>=5){stopping=true;break;}}paint();}}try{await Promise.all([worker(),worker()]);}finally{running=false;$('run').disabled=false;$('pause').disabled=true;$('reset').disabled=false;paint();if(failed)$('status').textContent+=`
실패 ${failed}건: 다시 누르면 실패한 신호만 재검사`;}}
$('run').onclick=run;$('pause').onclick=()=>{stopping=true;$('pause').disabled=true;};$('reset').onclick=()=>{if(running||!state)return;if(!confirm('이번 마지막 검증 기록만 초기화할까?'))return;state={version:1,source_sha:state.source_sha,results:{}};save();paint();};
function csvCell(v){let s=String(v??'');if(/^[\s]*[=+@]/.test(s)||(/^[\s]*-/.test(s)&&!/^[\s]*-\d+(?:\.\d+)?\s*$/.test(s)))s="'"+s;return /[",\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
$('csv').onclick=()=>{if(!ready())return;const cols=['version','date','market','signal_time_kst','strategy','status','outcome','net_pct','data_quality','history_status','actual_slots_241','filled_slots_241','max_gap_run','recent_31_raw','ma_above','relaunch_pass','retest_relaunch','cross_retest_relaunch','cross_bars_ago','retest_bars_ago','strict_retest_bars_ago','sma12','sma200','signal_close','pages','entry','exit','minutes'];let lines=[cols.join(',')];for(let e of seeds){let r=state.results[e.id];for(let [key] of ARMS){let f=flag(e,key),o={...e,...r,version:'V11.14.25',strategy:key,status:f===null?'FEATURE_UNAVAILABLE':f?'ACCEPTED':'BLOCKED'};lines.push(cols.map(k=>csvCell(o[k])).join(','));}}let blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download='bithumb_C5_volman_MA12_200_FINAL_v111425.csv';a.click();setTimeout(()=>URL.revokeObjectURL(u),2000);};
init().catch(e=>{$('status').textContent='⚠️ '+String(e.message||e);});
</script></body></html>""".replace('/*REPLACE_STYLE*/', C5_V111417_PAGE.split('<style>',1)[1].split('</style>',1)[0])


def register_c5_ma_v111425_lab(app, deps):
    api_get,kst=deps['api_get'],deps['KST']

    @app.get('/backtest-winrate-c5-ma12200-final')
    def c5_ma_v111425_page():
        return render_template_string(C5_MA_V111425_PAGE)

    @app.get('/api/winrate14c5ma12200final/seed')
    def c5_ma_v111425_seed():
        return jsonify({'ok':True,'rule_version':'V11.14.25',
                        'source_sha':C5_MA_V111423_SEED_SHA,
                        'items':C5_MA_V111423_SEEDS})

    @app.post('/api/winrate14c5ma12200final/evaluate')
    def c5_ma_v111425_evaluate():
        try:
            body=request.get_json(silent=True)
            ident=body.get('id') if isinstance(body,dict) else None
            if type(ident) is not int or ident not in C5_MA_V111423_BY_ID:
                raise ValueError('74개 기존 신호 중 하나를 선택해야 합니다')
            r=c5_ma_v111425_features(api_get,C5_MA_V111423_BY_ID[ident],kst)
            return jsonify({'ok':True,'rule_version':'V11.14.25','result':r})
        except Exception as exc:
            return jsonify({'ok':False,'error':str(exc)[:180]}),400


_v111424_register_daytrade_review = register_daytrade_review


def register_daytrade_review(app, deps):
    _v111424_register_daytrade_review(app,deps)
    register_c5_ma_v111425_lab(app,deps)
