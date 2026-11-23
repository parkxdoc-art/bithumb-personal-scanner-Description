"""V1.4.4: independent early rebound watch and confirmed-trend watch tracks.

- The original 10-point technical + quality candidate is ALWAYS evaluated first.
- Existing V1.4.3 BUY and strict trend watch rules are preserved exactly.
- EARLY_WATCH is observational, not a purchase signal or a way to bypass BUY.
- Do not use 120h hypothetical legacy WATCH exit as traded P&L.
"""
RULE_VERSION="WATCH_TRACKS_V144"

def classify_watch_tracks(legacy, strict, combined, evidence, m, ctx):
    scores=evidence.get("scores") or {}
    checks=m.get("checks") or {}
    macd=m.get("macd_h4") or {}
    rsi=float(m.get("rsi14") or 0)
    score=int(m.get("score") or 0)
    safety=float(scores.get("safety") or 0)
    timing=float(scores.get("timing") or 0)
    ratio=float(m.get("volume_ratio") or 0)
    risk=float(m.get("structure_risk_pct") or 1e9)
    data_gap=bool(m.get("data_gap_recent", True))
    severe_bear=(macd.get("state")=="DEAD"
                 and rsi<50
                 and float(scores.get("trend") or 0)<50)
    early_conditions={
        "기존 반등 점수 8/10 이상": score>=8,
        "기존 기술 5/7·진입품질 2/3 이상": (
            float(m.get("technical_score") or 0)>=5
            and float(m.get("quality_score") or 0)>=2),
        "RSI 45 이상":rsi>=45,
        "타이밍 점수 75 이상":timing>=75,
        "안전 점수 75 이상":safety>=75,
        "4시간 거래대금 0.7배 이상":ratio>=.7,
        "지지 위험거리 0~9%":0<=risk<=9,
        "최근 캔들 공백 없음":not data_gap,
        "MACD 데드·RSI 50미만·추세약세 동시 발생 제외":not severe_bear,
    }
    early_ready=legacy in ("BUY","WATCH") and all(early_conditions.values())
    # The existing V1.4.3 structure + trend + risk logic remains the
    # non-negotiable basis for the *confirmed* track, with no loosening.
    trend_conditions={
        "기존 V1.4.3 종합 WATCH 준비 통과":
            combined.get("watch_ready") is True,
        "일봉·4시간봉 상승 추세 필수조건": (
            strict.get("checks",{}).get("일봉 MA20 상향 + 22봉 연속") is True
            and strict.get("checks",{}).get("4시간봉 MA20 상회·상향 + 21봉 연속") is True),
        "4시간 MACD 골든 상태":macd.get("state")=="GOLDEN",
    }
    trend_ready=legacy in ("BUY","WATCH") and all(trend_conditions.values())
    # Never raise an early observer to BUY; BUY is precisely V1.4.3 BUY.
    if combined.get("signal")=="BUY":
        kind="BUY"; label="종합 BUY · 기존 엄격 기준 통과"
    elif trend_ready:
        kind="TREND_WATCH";label="추세 확인 WATCH · 상위 추세 확인"
    elif early_ready:
        kind="EARLY_WATCH";label="초기 반등 WATCH · 추세 확정 전"
    else:
        kind="RESEARCH";label="연구 보류 · 최종 관찰 기준 불합격"
    warnings=[]
    if kind=="EARLY_WATCH":
        warnings.append("초기 탐색만 허용: 일봉·4시간 추세는 아직 확정되지 않을 수 있음")
        warnings.append("BUY 아님: 실제 체결 진입·확정 돌파 신호 아님")
    if macd.get("state")!="GOLDEN":
        warnings.append("4시간 MACD 골든 상태 미확인")
    if ctx.get("long")=="down" or ctx.get("short")=="down":
        warnings.append("BTC 시장 추세 하락: 관찰 위험 증가")
    if data_gap:
        warnings.append("최근 캔들 누락: 신규 신호 보류")
    return {
        "rule":RULE_VERSION,
        "kind":kind,
        "label":label,
        "early_ready":early_ready,
        "trend_ready":trend_ready,
        "early_checks":early_conditions,
        "trend_checks":trend_conditions,
        "early_missing":[k for k,v in early_conditions.items() if not v],
        "trend_missing":[k for k,v in trend_conditions.items() if not v],
        "warnings":warnings,
        "old_combined_signal":combined.get("signal","RESEARCH"),
        "buy_unchanged":combined.get("signal")=="BUY" if kind=="BUY" else True,
        "note":"두 WATCH는 관찰용이며 BUY 진입/자동매매로 승격되지 않음. 120시간 가상 가격 경로는 실현 손익 아님.",
    }
