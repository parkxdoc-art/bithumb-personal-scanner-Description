"""BTC MTF V1.4.3 — combine original rebound strategy with evidence scores.

The research thresholds are deliberately conservative hypotheses, not a
validated success model. No orders, no tick/quote reconstruction. Uses only
candles completed at (or before) the historical scan snapshot.
"""
from __future__ import annotations

COMBINED_RULE = "COMBINED_V143"

REQUIRED_TREND_CHECKS = (
    "일봉 MA20 상향 + 22봉 연속",
    "4시간봉 MA20 상회·상향 + 21봉 연속",
    "4시간봉 MA7/14 상승 배열",
    "RSI(14) 50 이상",
    "MACD 히스토그램 양수",
    "4시간 MACD 골든 상태 (실제 교차와 구분)",
    "4시간 거래대금 1.1배 이상",
    "BTC 장·단기 비하락",
    "최근 봉 공백 없음",
)


def evaluate_combined(original, legacy_strict, evidence, m, h4, is_contiguous):
    """Return old and new evidence side by side, without dropping old rows.

    WATCH: high-score old candidate + 4h/day trend + evidence thresholds and
    bounded stop/risk/selling-pressure proxies. BUY: additionally requires
    old BUY, all V1.4 strict conditions and a newly completed 4h candle that
    actually closes above the *prior* 3 completed highs on >=2x turnover.

    The forward-looking trigger reference in evidence.plan is NOT used to
    claim that a future candle confirmed at the present snapshot.
    """
    scores = evidence.get("scores") or {}
    checks_old = legacy_strict.get("checks") or {}
    proxy = evidence.get("sell_pressure_proxy") or {}
    plan = evidence.get("plan") or {}
    sell_red = proxy.get("red_candle_turnover_pct")
    complete_h4 = len(h4) >= 4 and is_contiguous(h4, "4h", 4)
    if complete_h4:
        # Excludes the current completed candle from resistance construction.
        prior_resistance = max(float(x["h"]) for x in h4[-4:-1])
        breakout_level = prior_resistance * 1.001
        last = h4[-1]
        confirmed_breakout = (
            last["c"] > breakout_level
            and last["c"] > last["o"]
            and float(m.get("body_ratio", 0)) >= .5
            and float(m.get("volume_ratio", 0)) >= 2.0
        )
    else:
        breakout_level = None
        confirmed_breakout = False

    watch_checks = {
        "기존 10점 반등 점수 9점 이상": m.get("score", 0) >= 9,
        "기존 기술 6/7 · 진입 품질 3/3": (
            m.get("technical_score", 0) >= 6 and m.get("quality_score", 0) >= 3),
        "일봉/4시간 상승·MACD·BTC 필수조건": all(
            checks_old.get(key) is True for key in REQUIRED_TREND_CHECKS),
        "구조 점수 75 이상": scores.get("structure", 0) >= 75,
        "타이밍 점수 50 이상": scores.get("timing", 0) >= 50,
        "추세 점수 75 이상": scores.get("trend", 0) >= 75,
        "셋업 점수 75 이상": scores.get("setup", 0) >= 75,
        "진입 준비 점수 50 이상": scores.get("entry", 0) >= 50,
        "안전 점수 75 이상": scores.get("safety", 0) >= 75,
        "음봉 거래대금 비중 60% 이하(매도호가 아님)": (
            sell_red is not None and sell_red <= 60),
        "가상 손절 거리 0.8~5%·최근 봉 정상": plan.get("plan_valid") is True,
        "4시간봉 4개 연속": complete_h4,
    }
    watch_ready = bool(original) and all(watch_checks.values())
    buy_checks = {
        "기존 V1.3.3 BUY": original == "BUY",
        "V1.4.1 강화 BUY 조건": legacy_strict.get("passed") is True,
        "종합 WATCH 준비 기준 모두 충족": watch_ready,
        "진입 준비 점수 75 이상": scores.get("entry", 0) >= 75,
        "완료된 최신 4시간봉이 이전 3봉 고점 돌파": confirmed_breakout,
        "최신 4시간봉 거래대금 2.0배 이상": m.get("volume_ratio", 0) >= 2.0,
    }
    buy_ready = all(buy_checks.values())

    if buy_ready:
        label = "BUY"
        stage = "종합 추세·설명 점수 + 완료봉 돌파 확인"
    elif watch_ready:
        label = "WATCH"
        stage = "종합 후보 · 마지막 돌파/거래대금 확인 대기"
    else:
        label = "RESEARCH"
        stage = "원본 반등 기록 · 종합 기준 보류"

    return {
        "rule": COMBINED_RULE,
        "signal": label,
        "stage": stage,
        "watch_ready": watch_ready,
        "buy_ready": buy_ready,
        "watch_checks": watch_checks,
        "buy_checks": buy_checks,
        "missing_watch": [k for k, ok in watch_checks.items() if not ok],
        "missing_buy": [k for k, ok in buy_checks.items() if not ok],
        "prior_3bar_breakout_level": round(breakout_level, 8) if breakout_level else None,
        "latest_completed_4h_breakout": confirmed_breakout,
        "red_candle_turnover_pct": sell_red,
        "notes": [
            "10점 반등 + 일봉/4시간/BTC/MACD + 설명 점수/진입 위험 + 이미 완료된 4시간 돌파를 결합",
            "음봉 거래대금 비중은 매도 호가잔량이나 체결강도가 아닌 가격 봉 기반 추정 지표",
            "통합 기준은 사용자 연구용 가설이며 실제 승률 검증 전, 기대 수익을 보장하지 않음",
            "RESEARCH 항목도 CSV에 남고 기본 화면에서는 숨김",
        ],
    }
