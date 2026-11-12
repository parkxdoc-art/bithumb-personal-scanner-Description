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
