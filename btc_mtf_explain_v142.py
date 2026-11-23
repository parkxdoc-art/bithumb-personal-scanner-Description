"""V1.4.2: explainable OHLC-based swing snapshot, not order-book advice.

All values derive solely from day/4h/1h candles completed by the scan
snapshot. No later data, public orderbook snapshots, or fictional trade
executions are used. Scores are documented 0..100 research checklists,
NOT calibrated success probabilities or broker signals.
"""
from __future__ import annotations

from statistics import mean

EXPLAIN_VERSION = "EXPLAIN_V142"
CHECK_TIMEFRAME = "4시간봉"
REQUIRE_FUTURE_CANDLE = True


def _score(conditions):
    return int(round(100 * sum(bool(v) for v in conditions) / len(conditions)))


def _round_price(x):
    return round(float(x), 8)


def build_explanation(m, ctx, day, h4, h1, fee_each_pct):
    """Return audit-friendly factors and a conditional future-bar watch plan.

    Caller has already checked minimum OHLC sample size. Required snapshot:
    day>=24, h4>=80, h1>=24, completed/snapshot-aligned.
    """
    if len(day)<24 or len(h4)<30 or len(h1)<16:
        return {"status": "INSUFFICIENT", "reason": "완료봉 수 부족",
                "scores": {}, "plan": None, "version": EXPLAIN_VERSION}
    close = float(m["close"])
    ma20 = mean(x["c"] for x in h4[-20:])
    ma20_prev = mean(x["c"] for x in h4[-21:-1])
    day_ma20 = mean(x["c"] for x in day[-20:])
    recent_high = max(x["h"] for x in h4[-3:])
    recent_low = min(x["l"] for x in h4[-6:])
    atr_abs = max(float(m.get("atr_pct", 0)), 0) * close / 100
    vratio = max(float(m.get("volume_ratio", 0)), 0)
    c = m["checks"]
    macd_state = m.get("macd_h4", {}).get("state", "UNKNOWN")
    recent_gap = bool(m.get("data_gap_recent", True))
    btc_nonbear = ctx.get("long") in ("up", "side") and ctx.get("short") in ("up", "side")
    h4_up = bool(m["h4_up"])
    day_up = bool(m["day_up"])

    factors = {
        "structure": [
            ("4시간 저점 상승", bool(m["h4_higher_low"])),
            ("4시간 고점 상승", bool(m["h4_higher_high"])),
            ("4시간 종가 MA20 위", close > ma20),
            ("4시간 MA20 상향", ma20 > ma20_prev),
        ],
        "timing": [
            ("1시간 반등 확인", bool(c["h1_confirm"])),
            ("MA5 회복", bool(c["ma_recover"])),
            ("RSI 상승 전환", bool(c["rsi_recover"])),
            ("MACD 모멘텀 개선", bool(c["macd_recover"])),
        ],
        "trend": [
            ("일봉 MA20 상승 확인", day_up),
            ("4시간 MA7·MA14 상승", h4_up),
            ("RSI 50 이상", float(m["rsi14"]) >= 50),
            ("4시간 MACD 골든 상태", macd_state == "GOLDEN"),
        ],
        "setup": [
            ("최근 4시간 거래대금 1.1배", vratio >= 1.1),
            ("저점 대비 +1.2~9%", 1.2 <= float(m["rebound_from_low_pct"]) <= 9),
            ("구조적 지지 위험거리 9% 내", 0 <= float(m["structure_risk_pct"]) <= 9),
            ("볼린저밴드 과열·과매도 회피", 0.15 <= float(m["bb_position"]) <= .95),
        ],
        "entry": [
            ("선택한 검색봉 추세 확인", bool(m.get("timing_ok", False))),
            ("직전 4시간 종가보다 상승", h4[-1]["c"] > h4[-2]["c"]),
            ("4시간 봉 몸통 비율 50% 이상", float(m.get("body_ratio",0)) >= .5),
            ("4시간 거래대금 2.0배 이상", vratio >= 2.0),
        ],
        "safety": [
            ("지지 거리 5% 내", 0 <= float(m["structure_risk_pct"]) <= 5),
            ("RSI 과열 아님(<=70)", float(m["rsi14"]) <= 70),
            ("최근 1시간·4시간 봉 연속", not recent_gap),
            ("BTC 장·단기 하락 아님", btc_nonbear),
        ],
    }
    scores = {k: _score([ok for _, ok in checks]) for k, checks in factors.items()}
    # A red candle's turnover is NOT a direct measure of taker sell orders.
    last4 = h4[-4:]
    total_turnover = sum(max(0, float(x.get("turnover", 0))) for x in last4)
    down_turnover = sum(max(0, float(x.get("turnover", 0)))
                        for x in last4 if x["c"] < x["o"])
    red_turnover_pct = round(100*down_turnover/total_turnover, 1) if total_turnover else None

    # Future-bar reference only. No confirmation can be known at this snapshot.
    trigger = max(recent_high * 1.001, close * 1.001)
    stop = min(recent_low - .15*atr_abs, trigger * .999)
    risk_pct = 100*(trigger-stop)/trigger
    risk_unit = trigger-stop
    # Round-trip fee breakeven reference at the hypothetical breakout entry.
    fee = max(0, float(fee_each_pct))/100
    breakeven = trigger*(1+fee)/(1-fee) if fee < 1 else None
    targets_valid = .8 <= risk_pct <= 5
    plan = {
        "trigger_ref": _round_price(trigger),
        "stop_ref": _round_price(stop),
        "breakeven_ref": _round_price(breakeven) if breakeven is not None else None,
        "target1_ref": _round_price(trigger+1.5*risk_unit) if targets_valid else None,
        "target2_ref": _round_price(trigger+3.0*risk_unit) if targets_valid else None,
        "target1_rr": 1.5 if targets_valid else None,
        "target2_rr": 3.0 if targets_valid else None,
        "risk_pct": round(risk_pct, 3),
        "plan_valid": targets_valid and not recent_gap,
        "requires_next_completed_4h_body_close_above_trigger": True,
        "requires_future_4h_turnover_ratio": 2.0,
        "note": "향후 완료될 4시간봉의 몸통 종가·거래대금 2.0배 확인이 필요. 아직 신호 아님. 가격은 참고치이며 호가 단위·실제 체결/슬리피지 미검증.",
    }
    warnings = []
    if not day_up or not h4_up: warnings.append("상위 추세 미확인(일봉 또는 4시간봉)")
    if macd_state != "GOLDEN": warnings.append("4시간 MACD 골든 상태 미확인")
    if float(m["rsi14"]) < 50: warnings.append("RSI 50 미만")
    if vratio < 1.1: warnings.append("4시간 거래대금 증가 부족")
    if recent_gap: warnings.append("최근 완료봉 공백")
    if not btc_nonbear: warnings.append("BTC 추세 하락/불명")
    if not targets_valid: warnings.append("구조적 손절 거리 과대/과소: 목표가 미산정")
    if float(m.get("h4_distance_ma7_pct",0)) > 7.5:
        warnings.append("4시간 MA7 과이격: 추격 금지")

    if day_up and h4_up and macd_state=="GOLDEN" and float(m["rsi14"])>=50:
        summary = "상위 상승 추세 후보지만 미래 돌파봉은 아직 확인되지 않았어."
    elif h4_up or bool(c["h1_confirm"]):
        summary = "단기 반등 징후는 있으나 상위 추세 확정 전이므로 진입을 보류해."
    else:
        summary = "상승 전환 근거 부족. 반등 점수만으로는 진입하지 않아."

    return {
        "version": EXPLAIN_VERSION,
        "status": "RESEARCH_ONLY",
        "closed_kst": m.get("closed_kst"),
        "scores": scores,
        "factors": {k: [{"label": label, "passed":bool(ok)} for label,ok in checks]
                    for k,checks in factors.items()},
        "sell_pressure_proxy": {
            "red_candle_turnover_pct": red_turnover_pct,
            "label": "최근 4시간봉 4개 중 음봉의 거래대금 비중(실제 체결 매도량 아님)",
            "orderbook_sell_wall": None,
            "taker_sell_strength": None,
            "note": "호가벽·체결강도 원본을 조회하지 않았으며, 과거 호가를 복원할 수 없음.",
        },
        "context": {
            "close_ref": _round_price(close),
            "h4_rvol_turnover": round(vratio, 3),
            "h4_atr_pct": round(float(m.get("atr_pct",0)),3),
            "h4_rsi": round(float(m["rsi14"]),2),
            "h4_ma7_distance_pct": round(float(m.get("h4_distance_ma7_pct",0)),3),
            "h4_ma20_ref": _round_price(ma20),
            "day_ma20_ref": _round_price(day_ma20),
            "last_24h_closed_turnover_krw": round(
                sum(max(0,float(x.get("turnover",0))) for x in h4[-6:]),0),
            "btc_relative_pct": round(float(m.get("relative_pct",0)),3),
        },
        "summary": summary, "warnings": warnings, "plan": plan,
        "snapshot_notice": "모든 수치는 스캔 시각까지 완료된 OHLC에서 계산. 점수는 성공 확률이 아닌 4개 근거씩의 체크리스트. 실시간 호가·체결강도는 미수집.",
    }
