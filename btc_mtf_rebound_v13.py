"""BTC MTF V1.3 | quality-weighted early rebound classification.
Keeps V1.2's 7 completed-candle technical checks and adds three independent
quality checks. A score is a research heuristic, not a probability of profit.
"""
from btc_mtf_rebound_v12 import bottom_rebound_metrics as _metrics
from btc_mtf_rebound_v12 import rebound_signal as _legacy_signal

RULE_VERSION = "REBOUND_V1.3_SCORE10"
MAX_SCORE = 10

def bottom_rebound_metrics(*args, **kwargs):
    m, issue = _metrics(*args, **kwargs)
    if m is None:
        return m, issue
    quality = {
        "btc_strength": m["relative_pct"] >= 0.5,
        "entry_zone": 1.2 <= m["rebound_from_low_pct"] <= 11.0 and m["rise24_pct"] <= 12,
        "risk_control": 0 <= m["structure_risk_pct"] <= 11,
    }
    m["technical_score"] = m["score"]
    m["quality_checks"] = quality
    m["quality_score"] = sum(quality.values())
    m["score"] = m["technical_score"] + m["quality_score"]
    m["score_total"] = MAX_SCORE
    m["score_rule"] = RULE_VERSION
    return m, None

def rebound_signal(m, ctx, fee):
    if not m:
        return None
    c = m["checks"]
    q = m["quality_checks"]
    from_low = m["rebound_from_low_pct"]
    risk = m["structure_risk_pct"]
    rel = m["relative_pct"]
    bearish = ctx["long"] == "down" or ctx["short"] == "down"
    relative_floor = 0.8 if bearish else 0.5

    # Mandatory prefilters: scores never excuse bad entry location or
    # relative weakness. Already-exploded coins are excluded, not WATCH.
    watch_eligible = (
        m["score"] >= 7
        and m["technical_score"] >= 4
        and rel >= relative_floor
        and 1.2 <= from_low <= 11
        and 0 <= risk <= 11
        and m["rise24_pct"] <= 12
        and 0.15 <= m["bb_position"] <= 1.0
        and m["volume_ratio"] >= 0.35
        and m["rsi14"] <= 73
    )
    if not watch_eligible:
        return None

    trigger = (
        c["h1_confirm"] and c["ma_recover"]
        and c["rsi_recover"] and c["macd_recover"]
        and (c["bb_recover"] or c["ichimoku_recover"])
    )
    buy = (
        m["score"] >= 8 and trigger and rel >= relative_floor
        and m["volume_ratio"] >= 0.7
        and from_low <= 9.0 and risk <= 9.0
        and m["bb_position"] <= 0.95
        and m.get("timing_ok", True)
        and not m.get("data_gap_recent", False)  # avoid BUY on interrupted 1h/4h history
    )
    kind = "BUY" if buy else "WATCH"
    stage = ("반등 확인 BUY" if buy else
             "점검 공백 · BUY 보류" if m.get("data_gap_recent", False) else
             "반등 직전 WATCH" if m["score"] >= 8 else "반등 준비 WATCH")
    legacy = dict(m)
    legacy["score"] = m["technical_score"]  # preserve legacy /7 explanation and plan
    raw = _legacy_signal(legacy, ctx, fee)
    if raw is None:
        return None

    names = {
        "bb_recover": "볼린저밴드 회복", "ma_recover": "MA5 기울기 회복",
        "ichimoku_recover": "일목 전환선", "rsi_recover": "RSI 상승",
        "macd_recover": "MACD 모멘텀", "volume_recover": "4시간 거래대금",
        "h1_confirm": "1시간봉 반등"
    }
    quality_names = {
        "btc_strength": "BTC 대비 +0.5%p 이상",
        "entry_zone": "저점에서 +1.2~11% 초기 구간",
        "risk_control": "저점 지지까지 11% 이내"
    }
    tech_details = " · ".join(("✓ " if passed else "× ") + names[k] for k, passed in c.items())
    qual_details = " · ".join(("✓ " if passed else "× ") + quality_names[k] for k, passed in q.items())
    why = [
        f"반등 점수 {m['score']}/10 = 지표 {m['technical_score']}/7 + 진입 품질 {m['quality_score']}/3",
        f"판정 {stage} · 10점이어도 반등 확정이 아니며 BUY 필수 확인 조건 적용",
        "기술 지표: " + tech_details,
        "진입 품질: " + qual_details,
        ("⚠ 최근 1시간·4시간 마감봉에 공백이 있어 BUY를 자동 보류함. 실거래 캔들만 이용하며 누락봉 보간 없음."
         if m.get("data_gap_recent") else "최근 1시간·4시간 마감봉 연속성 확인"),
        *raw["explanation"][1:7],
        f"저점 대비 +{from_low:.2f}% · 저점 기준 위험 거리 {risk:.2f}% · 24시간 등락 {m['rise24_pct']:+.2f}%",
    ]
    if not buy:
        missing = [names[k] for k in ("h1_confirm", "ma_recover", "rsi_recover", "macd_recover") if not c[k]]
        if not (c["bb_recover"] or c["ichimoku_recover"]):
            missing.append("볼밴 또는 일목 회복")
        if m["score"] < 8:
            missing.append("BUY 점수 8/10")
        if m["volume_ratio"] < 0.7:
            missing.append("거래대금 0.7배")
        if from_low > 9 or risk > 9:
            missing.append("BUY 진입 위치 안전 기준")
        if not m.get("timing_ok", True):
            missing.append("선택한 검색 시간봉 확인")
        if m.get("data_gap_recent"):
            missing.append("점검 후 최근 마감봉 공백(연속봉 회복까지 BUY 보류)")
        why.append("BUY 보류 이유: " + (", ".join(missing) if missing else "반등 확인 추가 대기"))
    else:
        why.append("완료봉 기준 반등 확인. 실제 주문 신호나 수익 보장은 아님")

    return {"signal": kind, "stage": stage, "metrics": m, "explanation": why, "plan": raw["plan"]}
