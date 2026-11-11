"""V11.9 selective day-trade scanner (max three *manual* trades/day).

Only the /backtest-daytrade experiment page is replaced. app.py and the V2
main-page BUY logic are untouched. All KRW markets are considered, without
turnover or warning-list exclusions. This is an experimental signal, not an order.

Pass 1: fetch 80 completed 4-hour candles for *all* markets, find
  horizontal support/resistance *zones* with at least two distinct confirmed
  swing lows/highs near the same price (not diagonal trendlines).
Pass 2: fetch 80 completed 5-minute candles only when a valid 4H zone
  is within +/-2% of the ticker snapshot; check bounce / confirmed breakout.
Before displaying BUY, require 5M price action/volume and a minimum gross
price buffer to the next confirmed 4H resistance versus a defined stop.
BUY requires at least 3% measured gross space below the nearest identified 4H resistance.
Only the highest-ranked 3 confirmed BUY / one-condition-away WATCH setups are
spotlighted; users manually log up to 3 trades per Seoul day in local storage.
The scanner cannot place orders or enforce real exchange trading limits.
This is a strict observational gate, not a validated profitability model.
No look-ahead candles, future returns, or synthetic gap-fill candles are used.
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
NEAR_4H_PCT = 2.0          # Same first-pass width as V11.6, for comparison.
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


def analyze_4h(api_get, market, price, now, kst):
    ordered = completed_candles(api_get, market, 240, 80, now, kst)
    out = {"market": market, "price": price, "bars4h": len(ordered),
           "near": False, "support": None, "resistance": None,
           "reason": "", "last4h": None}
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
    if not out["support"] and not out["resistance"]:
        out["reason"] = "2회 이상 확인된 수평 지지·저항 가격대 없음"
    else:
        out["near"] = any(abs(z["distance_pct"]) <= NEAR_4H_PCT
                          for z in (out["support"], out["resistance"]) if z)
        out["reason"] = "5분봉 확인 대상" if out["near"] else "수평 가격대와 거리 멂"
    return out


def evaluate_entry_quality(entry, stop, overhead, strong_candle,
                           volume_confirmed, retest_confirmed):
    """Conservative research gates for a long entry; amounts are GROSS.

    Missing overhead level does not mean unlimited upside. Without an observed
    4H resistance above price, keep the setup on WATCH for verification.
    """
    reasons = []
    if not strong_candle:
        reasons.append("5분봉 양봉 몸통/윗꼬리 확인 부족")
    if not volume_confirmed:
        reasons.append("매수 거래량 확인 부족")
    if not retest_confirmed:
        reasons.append("돌파 후 지지 재확인 부족")
    if not (isfinite(entry) and isfinite(stop) and 0 < stop < entry):
        return {"passed": False, "reasons": reasons+["유효한 손절 기준 없음"],
                "stop": None, "risk_pct": None, "room_pct": None,
                "rr": None, "next_resistance": None}
    risk_pct = (entry - stop) / entry * 100
    if not MIN_STOP_RISK_PCT <= risk_pct <= MAX_STOP_RISK_PCT:
        reasons.append(f"손절 거리 {risk_pct:.2f}% (허용 {MIN_STOP_RISK_PCT}~{MAX_STOP_RISK_PCT}%)")
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
    else:
        if room_pct < MIN_GROSS_ROOM_PCT:
            reasons.append(f"위쪽 저항 여유 {room_pct:.2f}% 부족")
        if rr < MIN_GROSS_RR:
            reasons.append(f"참고 손익비 {rr:.2f} 미달")
    return {"passed": not reasons, "reasons": reasons,
            "stop": round(stop, 10), "risk_pct": round(risk_pct, 2),
            "room_pct": round(room_pct, 2) if room_pct is not None else None,
            "rr": round(rr, 2) if rr is not None else None,
            "next_resistance": round(next_resistance, 10) if next_resistance else None}


def five_minute_entry(api_get, htf, now, kst):
    market = htf["market"]
    rows = completed_candles(api_get, market, 5, 80, now, kst)
    out = {"market": market, "status": "NONE", "setup": "", "note": "",
           "price": None, "volume_ratio": None, "age_minutes": None,
           "bars5m": len(rows), "htf": htf, "quality": None}
    if len(rows) < MIN_5M_BARS:
        out.update(status="SKIP", note="5분봉 데이터 부족")
        return out
    ts = [x[0] for x in rows]
    close = [float(x[1]["trade_price"]) for x in rows]
    opening = [float(x[1]["opening_price"]) for x in rows]
    highs = [float(x[1]["high_price"]) for x in rows]
    lows = [float(x[1]["low_price"]) for x in rows]
    volumes = [float(x[1]["candle_acc_trade_volume"]) for x in rows]
    out["price"] = close[-1]
    out["signal_time"] = ts[-1].strftime("%Y-%m-%d %H:%M")
    age = (now - (ts[-1] + timedelta(minutes=5))).total_seconds()/60
    out["age_minutes"] = round(age, 1)
    baseline = sum(volumes[-21:-1])/20
    vol_now = volumes[-1]/baseline if baseline > 0 else None
    vol_prior = volumes[-2]/baseline if baseline > 0 else None
    out["volume_ratio"] = round(vol_now, 2) if vol_now is not None else None
    recent_continuous = all(b-a == timedelta(minutes=5)
                            for a,b in zip(ts[-21:-1],ts[-20:]))
    freshness = ("STALE" if age > FRESH_5M_MINUTES or age < -0.5 else
                 "GAP" if not recent_continuous else "CURRENT")
    candle_range = highs[-1] - lows[-1]
    body_share = ((close[-1] - opening[-1])/candle_range
                  if candle_range > 0 else 0)
    upper_wick_share = ((highs[-1] - close[-1])/candle_range
                        if candle_range > 0 else 1)
    strong_green = (close[-1] > opening[-1] and
                    body_share >= MIN_BULLISH_BODY_SHARE and
                    upper_wick_share <= MAX_UPPER_WICK_SHARE)

    options = []
    for kind, label in (("support", "4시간 수평 지지대"),
                        ("resistance", "4시간 수평 저항대")):
        zone = htf.get(kind)
        if not zone:
            continue
        lo, hi = float(zone["low"]), float(zone["high"])
        # Never compute a new zone from five-minute candles.
        distance = (close[-1] / hi - 1)*100
        if kind == "support":
            touched = any(lo*0.998 <= lows[k] <= hi*1.002 for k in (-3,-2,-1))
            held = all(close[k] >= lo*0.996 for k in (-3,-2))
            bounce = (touched and held and close[-1] >= hi*1.001 and
                      close[-1] > opening[-1] and close[-1] > close[-2] and
                      0.10 <= distance <= MAX_BUY_SUPPORT_PCT)
            if bounce:
                stop = lo * (1 - STOP_PAD_PCT / 100)
                quality = evaluate_entry_quality(
                    close[-1], stop, htf.get("overhead"), strong_green,
                    vol_now is not None and vol_now >= MIN_BUY_VOL_RATIO,
                    True  # Touch and close-over-zone are already verified.
                )
                if quality["passed"]:
                    options.append(("BUY", "수평 지지 반등 · 매수세 확인", distance, zone, label, quality, "BUY"))
                else:
                    options.append(("WATCH", "지지 반등 모양 · 진입 조건 보류", distance, zone, label, quality, "BUY"))
            elif lo*0.998 <= close[-1] <= hi*(1+WATCH_SUPPORT_PCT/100):
                options.append(("WATCH", "수평 지지대 접근 · 반등 대기", distance, zone, label, None, "WATCH"))
            elif lo*(1-SUPPORT_RECLAIM_PCT/100) <= close[-1] < lo*0.998:
                options.append(("RECLAIM", "수평 지지대 이탈 · 재진입 대기", distance, zone, label, None, "RECLAIM"))
        else:
            crossed = any(close[k-1] <= hi*1.001 and close[k] >= hi*1.002
                          for k in (-3,-2))
            confirmed = close[-2] >= hi*1.001 and close[-1] >= hi*1.001
            breakout = crossed and confirmed and 0.10 <= distance <= MAX_BUY_BREAKOUT_PCT
            if breakout:
                # Breakout should be followed by a candle testing former
                # resistance as support; two closes above alone aren't enough.
                retested = any(hi*0.995 <= lows[k] <= hi*1.004 and
                               close[k] >= hi*1.001 for k in (-2,-1))
                volume_ok = (vol_now is not None and vol_prior is not None and
                             max(vol_now, vol_prior) >= MIN_BUY_VOL_RATIO and
                             vol_now >= MIN_CONFIRM_VOL_RATIO)
                stop = hi * (1 - STOP_PAD_PCT / 100)
                quality = evaluate_entry_quality(
                    close[-1], stop, htf.get("overhead"), strong_green,
                    volume_ok, retested
                )
                if quality["passed"]:
                    options.append(("BUY", "수평 저항 돌파 · 재확인 완료", distance, zone, label, quality, "BUY"))
                else:
                    options.append(("WATCH", "저항 돌파 모양 · 진입 조건 보류", distance, zone, label, quality, "BUY"))
            elif lo*(1-WATCH_RESISTANCE_PCT/100) <= close[-1] <= hi*1.006:
                options.append(("WATCH", "수평 저항대 접근 · 돌파 관찰", distance, zone, label, None, "WATCH"))
    if not options:
        return out
    # Prefer confirmed BUY, then the best near-entry setup; preserve broken
    # support as a separate RECLAIM, even when only one candidate exists.
    priority = {"BUY":0, "WATCH":1, "RECLAIM":2}
    options.sort(key=lambda y:(priority[y[0]], 0 if y[6] == "BUY" else 1, abs(y[2])))
    status, setup, dist, zone, label, quality, pattern_signal = options[0]
    out.update(status=status if freshness == "CURRENT" else freshness,
               original_signal=status, pattern_signal=pattern_signal,
               setup=setup, quality=quality,
               note=f"{label} · 5분봉 {setup}",
               distance_pct=round(dist,2), zone_center=zone["mid"],
               zone_low=zone["low"],zone_high=zone["high"],
               zone_touches=zone["touches"],source_kind=zone["kind"],
               pivot1=f"{zone['time1']} / {zone['p1']:.8g}",
               pivot2=f"{zone['time2']} / {zone['p2']:.8g}")
    if freshness == "STALE":
        out["note"] += " · 최신 확정봉 오래됨"
    elif freshness == "GAP":
        out["note"] += " · 최근 21개 5분봉 공백"
    return out


PAGE = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V11.9 · 하루 최대 3회 단타 후보 실험</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#141c2f;--border:#30405d;--text:#edf3ff;--sub:#a8b9d3;--green:#4ade80;--yellow:#fbbf24}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1050px;margin:auto;padding:20px 14px 60px}h1{font-size:22px;margin:0 0 8px}h2{font-size:18px;margin:0 0 12px}
p{margin:5px 0 12px}.muted{font-size:13px;color:var(--sub)}.panel{background:var(--panel);border:1px solid var(--border);border-radius:15px;padding:17px;margin-top:14px}
button{background:#315de7;color:white;border:0;border-radius:9px;padding:12px 18px;font:inherit;font-weight:750;cursor:pointer}button:disabled{opacity:.5}a{color:#aecaff}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}.metric{border:1px solid var(--border);border-radius:10px;padding:10px;min-width:0}.metric b{display:block;font-size:23px}.buy{color:var(--green)}.watch{color:var(--yellow)}.subtle{color:#d8b4fe}
progress{width:100%;height:12px;margin-top:12px}#status{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:8px}.list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.item{background:#10182a;border:1px solid var(--border);padding:14px;border-radius:12px;overflow-wrap:anywhere}.item.buycard{border-color:#2a7954}.item.watchcard{border-color:#756027}.item h3{display:flex;justify-content:space-between;gap:8px;font-size:16px;margin:0 0 8px}.pill{font-size:12px;border-radius:7px;background:#1d314e;padding:2px 7px}.details{white-space:pre-line;font-size:13px}details summary{cursor:pointer;font-weight:750}#timing{margin-top:8px}.emphasis{font-weight:800}.recordnote{font-size:12px;color:var(--sub)}.focuscard{border-color:#486d9a}.focuscard.good{border-color:#2a7954}.focuscard h3{margin:0 0 6px;font-size:16px}.manualBar{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.manualBar button{font-size:13px;padding:8px 12px;background:#44526f}.manualBar button:disabled{opacity:.4}.dailycount{font-size:24px;font-weight:800}.smallhint{font-size:12px;color:var(--sub)}
@media(max-width:680px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.list{grid-template-columns:1fr}}
</style></head><body><main>
<h1>🎯 3~5% 움직임 여력 · 하루 3회 이하 단타 후보</h1>
<p class="muted">V11.9 집중 단타 실험판 · KRW 전체 · 거래대금 제한 없음 · 유의종목 포함 · V2 메인 변경 없음</p>
<div class="panel"><button id="run">지금 전체 검색</button> <a href="/">기존 V2 메인</a>
<p class="muted">4시간봉 수평 가격대와 5분봉 반등·돌파를 찾습니다. <b>위쪽 4시간 저항까지 최소 3%의 이론적 공간</b>, 짧은 구조적 손절, 거래량과 재확인을 통과한 BUY를 우선 표시합니다. 조건이 부족하면 WATCH로 남깁니다. 3~5% 상승이나 이익은 보장되지 않습니다.</p>
<div class="metrics">
<div class="metric"><span class="muted">전체 / 4시간봉 검사</span><b id="all">—</b></div>
<div class="metric"><span class="muted">5분봉 정밀 확인</span><b id="near">—</b></div>
<div class="metric"><span class="muted">BUY</span><b class="buy" id="buys">—</b></div>
<div class="metric"><span class="muted">WATCH</span><b class="watch" id="watches">—</b></div>
<div class="metric"><span class="muted">지지 이탈 주의</span><b class="subtle" id="reclaims">—</b></div>
</div>
<progress id="progress" value="0" max="1"></progress>
<div id="status" class="muted">검색 버튼을 누르면 4시간봉 전체 검사부터 진행합니다.</div>
<div id="timing" class="muted"></div>
</div>
<section class="panel"><h2>⭐ 오늘 집중할 최대 3개</h2><p class="muted">완료된 검색에서 BUY를 먼저, 그다음 단 한 조건만 남은 WATCH를 보여줍니다. 개수가 부족하면 억지로 세 개를 채우지 않습니다.</p><div class="list" id="focusList"><p class="muted">검색 완료 후 표시됩니다.</p></div></section>
<section class="panel"><h2>📝 오늘 수동 거래 기록</h2><div class="manualBar"><span class="dailycount" id="tradeCount">0 / 3회</span><button type="button" id="manualOne">거래 1회 직접 기록</button><button type="button" id="undoTrade">마지막 기록 취소</button></div><p class="recordnote" id="tradeHistory">실제 거래를 했다면 BUY 후보의 기록 버튼을 직접 눌러 주세요.</p><p class="smallhint">기록은 현재 브라우저에만 저장됩니다. 앱을 닫아도 같은 브라우저에 남지만, 다른 기기·브라우저와 공유되지 않습니다. 하루 3회는 사용자 원칙으로, 빗썸 주문을 막는 기능이 아닙니다.</p></section>
<section class="panel"><h2 class="buy">실험 BUY 후보 · 5분봉 정밀 검증 통과</h2><div class="list" id="buyList"><p class="muted">검색 전</p></div></section>
<section class="panel"><h2 class="watch">WATCH · 수평 가격대 접근 후보</h2><div class="list" id="watchList"><p class="muted">검색 전</p></div></section>
<details class="panel" id="reclaimPanel"><summary class="subtle">⚠️ 지지선 이탈 · 회복 대기 <span id="reclaimCount"></span></summary><p class="muted">4시간 수평 지지대 아래에 있어 아직 지지가 확인되지 않았습니다. 가격대를 회복하기 전에는 BUY로 취급하지 않습니다.</p><div id="reclaimList" class="list"></div></details>
<details class="panel" id="holdPanel"><summary class="subtle">⚠️ 신호 시간/캔들 공백 주의 <span id="holdCount"></span></summary><p class="muted">마지막 5분봉이 오래됐거나 최근 21개 봉이 연속되지 않은 후보야. 현재 BUY로 표시하지 않아.</p><div id="holdList" class="list"></div></details>
<p class="muted">실험용 신호이며 매수 권유나 수익 보장이 아닙니다. 진입 참고가는 최신 확정 5분봉 종가이며 실제 체결가가 아닙니다. 3~5%는 관찰 목표 움직임으로, 예측·확정 익절가가 아닙니다. 표시하는 손절 기준·위쪽 저항까지의 거리는 수수료·호가 차이·슬리피지를 뺀 순수익을 뜻하지 않습니다. 위쪽 저항이 확인되지 않으면 BUY를 확정하지 않습니다. 미래 봉은 사용하지 않습니다.</p>
</main><script>
const $=id=>document.getElementById(id);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const won=v=>Number(v).toLocaleString('ko-KR',{maximumFractionDigits:8})+'원';
const resultMap=new Map();let running=false;let showMore=false;let scanFinished=false;
const DAILY_LIMIT=3;
function kstToday(){
  const parts=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());
  const v=k=>parts.find(x=>x.type===k)?.value||'';
  return `${v('year')}-${v('month')}-${v('day')}`;
}
function tradeKey(){return 'bithumb_v119_manual_'+kstToday();}
function readTrades(){try{const x=JSON.parse(localStorage.getItem(tradeKey())||'[]');return Array.isArray(x)?x.slice(0,DAILY_LIMIT):[]}catch(e){return []}}
function saveTrades(rows){try{localStorage.setItem(tradeKey(),JSON.stringify(rows));return true}catch(e){$('tradeHistory').textContent='⚠️ 브라우저 기록 저장이 차단되어 수동 기록을 저장하지 못했습니다.';return false}}
function paintTrades(){
  const records=readTrades();$('tradeCount').textContent=`${records.length} / ${DAILY_LIMIT}회`;
  $('undoTrade').disabled=records.length===0;$('manualOne').disabled=records.length>=DAILY_LIMIT;
  $('tradeHistory').textContent=records.length?records.map((x,i)=>`${i+1}회 · ${x.market} · ${x.when}${x.price==null?'':' · 기록 가격 '+won(x.price)}`).join(' / '):'실제 거래가 있었다면 BUY 후보의 수동 기록 버튼을 눌러 주세요.';
}
function signalStillFresh(x){
  const ts=Date.parse(String(x.signal_time||'').replace(' ','T')+'+09:00');
  const age=Date.now()-(ts+5*60000);
  return Number.isFinite(ts)&&age>=-30000&&age<=10*60000;
}
function spotlight(){
  const arr=[...resultMap.values()].filter(x=>{
    if(x.status==='BUY')return true;
    const q=x.quality;
    return x.status==='WATCH'&&x.pattern_signal==='BUY'&&q&&
      Array.isArray(q.reasons)&&q.reasons.length===1&&
      Number(q.room_pct)>=MIN_ROOM_FOR_UI&&
      Number(q.risk_pct)>=0.25&&Number(q.risk_pct)<=1.25&&
      Number(q.rr)>=2.0;
  });
  return arr.sort((a,b)=>{
    const sa=a.status==='BUY'?0:1,sb=b.status==='BUY'?0:1;
    if(sa!==sb)return sa-sb;
    const qa=a.quality||{},qb=b.quality||{};
    return Number(qb.rr||0)-Number(qa.rr||0)||Number(qb.room_pct||0)-Number(qa.room_pct||0)||Number(a.distance_pct||99)-Number(b.distance_pct||99)||a.market.localeCompare(b.market);
  }).slice(0,DAILY_LIMIT);
}
const MIN_ROOM_FOR_UI=3.0;
function paintSpotlight(){
  if(!scanFinished){$('focusList').innerHTML='<p class="muted">검색 완료 후 최종 상위 후보를 표시합니다.</p>';return;}
  const picks=spotlight(),records=readTrades();
  if(!picks.length){$('focusList').innerHTML='<p class="muted">현재 기준에 맞는 집중 후보가 없습니다. 하루 3번을 채우기 위해 무리할 필요는 없습니다.</p>';return;}
  $('focusList').innerHTML=picks.map((x,i)=>{
    const buy=x.status==='BUY',q=x.quality||{};
    const exists=records.some(r=>r.market===x.market&&r.signal_time===x.signal_time);
    const allowed=buy&&records.length<DAILY_LIMIT&&!exists&&signalStillFresh(x);
    const text=buy?'확정 BUY 조건 통과':'진입 임박 · 한 조건 미충족';
    return `<article class="item focuscard ${buy?'good':''}"><h3>${i+1}. ${esc(x.market)} · ${text}</h3>`+
      `<div class="details">확정 종가 ${won(x.price)} · 참고 손절 ${q.stop==null?'—':won(q.stop)} / 위험 ${q.risk_pct==null?'—':q.risk_pct+'%'}
`+
      `위쪽 4시간봉 저항까지 이론적 여유 ${q.room_pct==null?'미확인':q.room_pct+'%'} · 참고 손익비 ${q.rr==null?'—':q.rr+':1'}
`+
      `${buy?'✅ 기존 엄격 조건 통과':'⚠️ 마지막 확인 필요: '+esc((q.reasons||[]).join(' / '))}</div>`+
      (buy?`<button type="button" class="manualTrade" data-market="${esc(x.market)}" ${allowed?'':'disabled'}>${exists?'기록 완료':records.length>=DAILY_LIMIT?'오늘 3회 기록 완료':signalStillFresh(x)?'실제 진입했다면 1회 기록':'신호 만료 · 재검색'}</button>`:'')+
      '</article>';
  }).join('');
}
function entries(status){return [...resultMap.values()].filter(x=>status==='HOLD'?['STALE','GAP'].includes(x.status):x.status===status)
 .sort((a,b)=>Math.abs(Number(a.distance_pct||99))-Math.abs(Number(b.distance_pct||99))||a.market.localeCompare(b.market));}
function card(x){const h=x.htf||{},z=x.source_kind==='support'?h.support:h.resistance;
const zoneText=z?`${z.kind==='support'?'4시간 수평 지지대':'4시간 수평 저항대'} · 확인된 반응 ${esc(z.touches)}회
가격대 ${won(z.low)} ~ ${won(z.high)} (중심 ${won(z.mid)})
반응 1: ${esc(x.pivot1)} · 반응 2: ${esc(x.pivot2)}`:'';
const r=x.volume_ratio==null?'확인 불가':Number(x.volume_ratio).toFixed(2)+'배';
const q=x.quality;const quality=q?`
진입 참고 손절 ${q.stop==null?'—':won(q.stop)} · 손절 거리 ${q.risk_pct==null?'—':q.risk_pct+'%'}
위쪽 4시간 저항 ${q.next_resistance==null?'확인 불가':won(q.next_resistance)} · 가격 여유 ${q.room_pct==null?'—':q.room_pct+'%'} · 참고 손익비 ${q.rr==null?'—':q.rr+':1'}
${q.passed?'✅ 진입 품질 조건 통과':'⚠️ BUY 보류: '+esc(q.reasons.join(' / '))}`:'';
return `<article class="item ${x.status==='BUY'?'buycard':'watchcard'}"><h3><span>${esc(x.market)}</span><span class="pill ${x.status==='BUY'?'buy':'watch'}">${esc(x.status==='BUY'?'BUY':x.status==='WATCH'?'WATCH':x.status==='RECLAIM'?'회복 대기':'확인 보류')}</span></h3>`+
`<div class="details">${esc(x.setup)} · ${esc(x.note)}
확정 5분봉 ${esc(x.signal_time)} · 종료 후 ${esc(x.age_minutes)}분
5분봉 종가 ${won(x.price)} · 가격대 상단 ${won(x.zone_high)} · 거리 ${x.distance_pct>0?'+':''}${esc(x.distance_pct)}%
거래량 ${esc(r)}${quality}
${zoneText}</div></article>`;}

function paint(){const b=entries('BUY'),w=entries('WATCH'),r=entries('RECLAIM'),h=entries('HOLD');$('buys').textContent=b.length;$('watches').textContent=w.length;$('reclaims').textContent=r.length;$('holdCount').textContent=`(${h.length}개)`;$('reclaimCount').textContent=`(${r.length}개)`;
$('buyList').innerHTML=b.length?b.map(card).join(''):'<p class="muted">현재 확정 BUY 없음</p>';
$('watchList').innerHTML=w.length?(showMore?w:w.slice(0,20)).map(card).join('')+(w.length>20?`<button type="button" id="moreBtn">${showMore?'접기':`나머지 ${w.length-20}개 더 보기`}</button>`:''):'<p class="muted">현재 WATCH 없음</p>';
const more=$('moreBtn');if(more)more.addEventListener('click',()=>{showMore=!showMore;paint();});
$('reclaimList').innerHTML=$('reclaimPanel').open?(r.length?r.map(card).join(''):'<p class="muted">지지선 회복 대기 후보 없음</p>'):'';
$('holdList').innerHTML=$('holdPanel').open?(h.length?h.map(card).join(''):'<p class="muted">확인 보류 없음</p>'):'';paintTrades();paintSpotlight();}
async function post(path,items){const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),75000);
try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items}),signal:controller.signal,cache:'no-store'});
const j=await r.json();if(!r.ok||!j.ok)throw Error(j.error||`HTTP ${r.status}`);return j;}finally{clearTimeout(timer);}}
async function pool(packs,limit,fn){let idx=0;const errs=[];async function worker(){while(idx<packs.length){const i=idx++;try{await fn(packs[i]);}catch(e){errs.push(e.message||String(e));}}}
await Promise.all(Array.from({length:Math.min(limit,packs.length)},worker));return errs;}
async function scan(){if(running)return;scanFinished=false;running=true;$('run').disabled=true;resultMap.clear();showMore=false;paint();$('all').textContent='—';$('near').textContent='—';$('timing').textContent='';$('progress').value=0;
const started=performance.now();let lineErrors=0,entryErrors=0;try{
const resp=await fetch('/api/zone45/universe',{cache:'no-store'});const u=await resp.json();if(!resp.ok||!u.ok)throw Error(u.error||'시장 목록 조회 실패');
const items=u.items;const packs=[];for(let i=0;i<items.length;i+=40)packs.push(items.slice(i,i+40));
$('progress').max=items.length;$('all').textContent=`0 / ${items.length}`;const near=[];let scanned=0,valid=0,noLines=0;
$('status').textContent='1단계 · 전체 4시간봉 수평 지지·저항 가격대 분석 중…';
const errors1=await pool(packs,3,async batch=>{const r=await post('/api/zone45/lines',batch);
scanned+=r.scanned;lineErrors+=r.errors;for(const x of r.results){if(x.near)near.push(x);if(x.support||x.resistance)valid++;else noLines++;}
$('all').textContent=`${scanned} / ${items.length}`;$('progress').value=scanned;
$('status').textContent=`1단계 · 4시간봉 ${scanned}/${items.length}개 · 수평 가격대 있는 종목 ${valid}개 · 5분봉 확인 대상 ${near.length}개`;});
const t1=((performance.now()-started)/1000).toFixed(1);if(errors1.length||lineErrors)throw Error(`4시간봉 일부 조회 실패: ${lineErrors}개 / 요청 ${errors1.length}건. 불완전한 검사이므로 결과 확정하지 않음`);
$('near').textContent=near.length;$('status').textContent=`2단계 · 4시간 수평 가격대 근처 ${near.length}개 종목의 5분봉 반등·돌파 확인 중…`;
const packs2=[];for(let i=0;i<near.length;i+=40)packs2.push(near.slice(i,i+40));let checked=0,skipped=0;
const errors2=await pool(packs2,3,async batch=>{const r=await post('/api/zone45/entries',batch);checked+=r.scanned;entryErrors+=r.errors;skipped+=r.skipped;
for(const x of r.results){if(['BUY','WATCH','RECLAIM','GAP','STALE'].includes(x.status))resultMap.set(x.market,x);}paint();
$('status').textContent=`2단계 · 5분봉 ${checked}/${near.length}개 · BUY ${entries('BUY').length} · WATCH ${entries('WATCH').length} · 지지 이탈 ${entries('RECLAIM').length}`;});
scanFinished=!(errors2.length||entryErrors||checked!==near.length);paint();const total=((performance.now()-started)/1000).toFixed(1);
$('timing').textContent=`4시간봉 전체 ${t1}초 · 5분봉 포함 전체 ${total}초 · 시작 ${esc(u.snapshot_at)} · 가격대 없음 ${noLines}개 · 5분봉 부족 ${skipped}개`;
$('status').textContent=(errors2.length||entryErrors?'⚠️ 일부 실패 · ':'✅ 완료 · ')+`전체 ${scanned}/${items.length} · 5분봉 확인 ${checked}/${near.length} · BUY ${entries('BUY').length} · WATCH ${entries('WATCH').length} · 지지 이탈 ${entries('RECLAIM').length} · 시간/공백 보류 ${entries('HOLD').length} · 오류 ${lineErrors+entryErrors+errors2.length}건`;
}catch(e){$('status').textContent='⚠️ 검색 중단: '+e.message+' · 표시된 내용은 불완전할 수 있어.';}
finally{running=false;$('run').disabled=false;}}
$('run').addEventListener('click',scan);$('holdPanel').addEventListener('toggle',paint);$('reclaimPanel').addEventListener('toggle',paint);
$('focusList').addEventListener('click',event=>{
  const btn=event.target.closest('button.manualTrade');if(!btn)return;
  const x=resultMap.get(btn.dataset.market);if(!scanFinished||!x||x.status!=='BUY'||!signalStillFresh(x))return;
  const records=readTrades();if(records.length>=DAILY_LIMIT||records.some(r=>r.market===x.market&&r.signal_time===x.signal_time))return;
  records.push({market:x.market,signal_time:x.signal_time,price:x.price,when:new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',hour:'2-digit',minute:'2-digit'}).format(new Date())});
  if(saveTrades(records))paint();
});
$('manualOne').addEventListener('click',()=>{const a=readTrades();if(a.length>=DAILY_LIMIT)return;a.push({market:'수동 거래',price:null,when:new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',hour:'2-digit',minute:'2-digit'}).format(new Date())});if(saveTrades(a))paint();});
$('undoTrade').addEventListener('click',()=>{const a=readTrades();if(!a.length)return;a.pop();if(saveTrades(a))paint();});
paintTrades();
</script></body></html>'''


def register_daytrade_review(app, deps):
    api_get, get_markets, kst = deps["api_get"], deps["get_markets"], deps["KST"]
    universe_cache = {"at": 0, "data": None}
    universe_lock = Lock()

    @app.get("/backtest-daytrade")
    def zone45_page():
        return render_template_string(PAGE)

    @app.get("/api/zone45/universe")
    def zone45_universe():
        try:
            with universe_lock:
                if universe_cache["data"] and time.time() - universe_cache["at"] < 30:
                    return jsonify(universe_cache["data"])
                markets = sorted({x for x in get_markets()
                                  if isinstance(x, str) and MARKET_RE.fullmatch(x)})
                if not markets:
                    raise ValueError("KRW 코인 목록이 비었습니다")
                ticker_data = {}
                market_set = set(markets)
                for i in range(0, len(markets), 50):
                    raw = api_get("/v1/ticker", {"markets": ",".join(markets[i:i+50])})
                    if not isinstance(raw, list):
                        raise ValueError("시세 데이터 형식 오류")
                    for x in raw:
                        if isinstance(x, dict) and x.get("market") in market_set:
                            ticker_data[x["market"]] = x
                if len(ticker_data) != len(markets):
                    raise ValueError("전체 KRW 시세가 누락되어 검사를 시작하지 않았습니다")
                items = []
                for market in markets:
                    try:
                        price = float(ticker_data[market]["trade_price"])
                    except (KeyError, ValueError, TypeError):
                        raise ValueError(market+" 현재가 확인 불가")
                    if not isfinite(price) or price <= 0:
                        raise ValueError(market+" 현재가 오류")
                    items.append({"market": market, "price": price})
                data = {"ok": True, "items": items, "total": len(items),
                        "snapshot_at": datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S KST")}
                universe_cache.update(at=time.time(), data=data)
                return jsonify(data)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:160]}), 502

    @app.post("/api/zone45/lines")
    def zone45_lines():
        payload = request.get_json(silent=True)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not 1 <= len(items) <= PAGE_SIZE:
            return jsonify({"ok": False, "error": "요청 코인 수는 1~40개여야 합니다"}), 400
        try:
            names = [x.get("market") for x in items if isinstance(x, dict)]
            if len(names) != len(items) or len(set(names)) != len(names) or any(
                    not isinstance(m, str) or not MARKET_RE.fullmatch(m) for m in names):
                raise ValueError("코인 목록 형식 오류")
            results = []
            errors = 0
            now = datetime.now(kst)
            with ThreadPoolExecutor(max_workers=5) as pool:
                tasks = {pool.submit(analyze_4h, api_get, x["market"], float(x["price"]), now, kst): x["market"]
                         for x in items}
                for f in as_completed(tasks):
                    market = tasks[f]
                    try:
                        results.append(f.result())
                    except Exception as e:
                        errors += 1
                        results.append({"market": market, "near": False, "error": type(e).__name__})
            return jsonify({"ok": True, "scanned": len(items), "errors": errors, "results": results})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:160]}), 400

    @app.post("/api/zone45/entries")
    def zone45_entries():
        payload = request.get_json(silent=True)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not 1 <= len(items) <= PAGE_SIZE:
            return jsonify({"ok": False, "error": "요청 코인 수는 1~40개여야 합니다"}), 400
        try:
            names = [x.get("market") for x in items if isinstance(x, dict)]
            if len(names) != len(items) or len(set(names)) != len(names) or any(
                    not isinstance(m, str) or not MARKET_RE.fullmatch(m) for m in names):
                raise ValueError("코인 목록 형식 오류")
            for x in items:
                if not x.get("near") or not isinstance(x.get("price"), (float, int)):
                    raise ValueError("4시간 수평 가격대 자료가 없습니다")
                for kind in ("support", "resistance"):
                    z = x.get(kind)
                    if z is not None:
                        if not isinstance(z, dict) or z.get("kind") != kind or not all(
                                key in z for key in ("low", "high", "mid", "touches", "p1", "p2", "time1", "time2")):
                            raise ValueError("수평 가격대 정보 형식 오류")
                        try:
                            lo, mid, hi = float(z["low"]), float(z["mid"]), float(z["high"])
                            touches = int(z["touches"])
                        except (TypeError, ValueError, OverflowError):
                            raise ValueError("수평 가격대 값 형식 오류")
                        if not (all(isfinite(v) for v in (lo, mid, hi)) and
                                0 < lo <= mid <= hi and touches >= 2):
                            raise ValueError("수평 가격대 가격/반응 오류")
            now = datetime.now(kst)
            results = []
            errors = 0
            skipped = 0
            with ThreadPoolExecutor(max_workers=5) as pool:
                tasks = {pool.submit(five_minute_entry, api_get, x, now, kst): x["market"] for x in items}
                for f in as_completed(tasks):
                    market = tasks[f]
                    try:
                        x = f.result()
                        results.append(x)
                        skipped += int(x["status"] == "SKIP")
                    except Exception as e:
                        errors += 1
                        results.append({"market": market, "status": "ERROR", "error": type(e).__name__})
            return jsonify({"ok": True, "scanned": len(items), "skipped": skipped,
                            "errors": errors, "results": results})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:160]}), 400
