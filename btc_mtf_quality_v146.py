"""BTC MTF V1.4.6 — historical liquidity observations and source-gap evidence.

Historical liquidity uses ONLY completed 1h candles no later than the selected
snapshot. No current 24h ticker is used for historical quality estimates.
Historical 1m/5m checks AFTER the snapshot diagnose an UNKNOWN replay gap only;
they never change trade signals, OHLC, stop/take-profit or exit status.
"""
from datetime import timedelta
import math

RULE = "HISTORICAL_LIQUIDITY_V146"


def _ts(t):
    return t.strftime("%Y-%m-%d %H:%M") if t is not None else ""


def _valid_turnover(row):
    try:
        v = float(row["turnover"])
    except (KeyError, ValueError, TypeError, OverflowError):
        return None
    return v if math.isfinite(v) and v >= 0 else None


def snapshot_liquidity(h1, snapshot):
    """Read-only 24h historical turnover / hourly completeness from real bars.

    Note: missing hourly candles are NOT treated as zero-turnover evidence.
    A present hourly candle can still represent a thin market.
    """
    floor = snapshot.replace(minute=0, second=0, microsecond=0)
    start = floor - timedelta(hours=24)
    expected = [start + timedelta(hours=i) for i in range(24)]
    index = {}
    for r in h1:
        try:
            stamp = r["t"]
        except (KeyError, TypeError):
            continue
        if stamp not in expected or stamp in index:
            continue
        if stamp + timedelta(hours=1) > snapshot:
            continue
        amount = _valid_turnover(r)
        if amount is None:
            continue
        index[stamp] = amount

    present = len(index)
    recent6 = sum(t in index for t in expected[-6:])
    missing = [t for t in expected if t not in index]
    longest = stretch = 0
    for t in expected:
        stretch = 0 if t in index else stretch + 1
        longest = max(longest, stretch)

    observed_krw = sum(index.values())
    checks = {
        "과거 24시간 1시간봉 22개 이상 확인": present >= 22,
        "가장 최근 6시간에 1시간봉 5개 이상": recent6 >= 5,
        "완료된 24시간 관측 거래대금 1억원 이상": observed_krw >= 100_000_000,
        "직전 완료 1시간봉 확인": expected[-1] in index,
    }
    score = sum(checks.values())
    if present < 18 or recent6 < 4:
        kind = "DATA_GAP"; label = "과거 이력 공백 주의"
    elif present >= 22 and observed_krw < 100_000_000:
        kind = "THIN_OBSERVED"; label = "과거 거래대금 낮음"
    elif score == 4:
        kind = "OBSERVED_OK"; label = "과거 거래활동 관측 양호"
    else:
        kind = "CAUTION"; label = "과거 유동성 추가 확인"
    warnings = []
    if present < 24:
        warnings.append("24시간 중 확인되지 않은 1시간봉이 있어 거래대금 합계는 불완전한 관측값")
    if observed_krw < 100_000_000:
        warnings.append("관측 거래대금 1억원 미만: 자료가 완전하지 않다면 실제 거래대금 부족으로 단정 금지")
    if recent6 < 5:
        warnings.append("최근 6시간 1시간봉 연속성 부족")
    if expected[-1] not in index:
        warnings.append("직전 완료 1시간봉 없음")
    return {
        "rule": RULE,
        "kind": kind,
        "label": label,
        "checks": checks,
        "checks_passed": score,
        "checks_total": 4,
        "snapshot_kst": _ts(snapshot),
        "window_from_kst": _ts(start),
        "window_to_exclusive_kst": _ts(floor),
        "hourly_bars_observed": present,
        "hourly_bars_expected": 24,
        "recent_6h_bars_observed": recent6,
        "longest_missing_hours": longest,
        "missing_hour_samples_kst": [_ts(x) for x in missing[:8]],
        "observed_turnover_24h_krw": round(observed_krw, 2),
        "observed_turnover_is_complete": present == 24,
        "warnings": warnings,
        "changes_buy_watch_gate": False,
        "note": ("스냅샷 이전 실제 완료된 1시간봉만 사용. "
                 "현재 24시간 티커나 미래 청산 캔들을 반영하지 않음. "
                 "4개 점수는 경험적 자료 품질 지표이며 수익 확률이 아님."),
    }


def investigate_missing_hour(market, missing_hour, getter):
    """Diagnose only one future-replay missing hour with actual 5m/1m bars.

    No trade/tick query: the public historical tick endpoint documents only
    the recent 1–7 day window. A missing bar is NOT proof of no trading.
    """
    end = missing_hour + timedelta(hours=1)
    cursor = end + timedelta(minutes=1)
    observations = {}
    errors = {}
    samples = {}
    for tf, count in (("5m", 15), ("1m", 70)):
        try:
            items = getter(market, tf, cursor, count=count)
            interval = timedelta(minutes=5 if tf == "5m" else 1)
            # Exact time filter: no bars from the previous or following hour.
            verified = sorted({
                row["t"] for row in items
                if isinstance(row, dict)
                and "t" in row
                and missing_hour <= row["t"] < end
                and (row["t"] + interval) <= cursor
            })
            observations[tf] = len(verified)
            samples[tf] = [_ts(x) for x in verified[:6]]
        except Exception as ex:
            observations[tf] = None
            samples[tf] = []
            errors[tf] = str(ex)[:120]

    if errors:
        state = "LOWER_TIMEFRAME_API_ERROR"
        description = "1분/5분봉 중 일부 또는 전부 조회에 실패함"
    elif observations["1m"] or observations["5m"]:
        state = "LOWER_TIMEFRAME_BARS_EXIST"
        description = "누락된 1시간에 실제 1분/5분봉 일부 확인. 1시간봉 복구된 것은 아님"
    else:
        state = "NO_LOWER_TF_BARS_RETURNED"
        description = "조회된 1분/5분봉 없음. 실제 무거래인지 API 보존 한계인지 미확정"
    return {
        "rule": "LOWER_INTERVAL_GAP_V146",
        "missing_hour_kst": _ts(missing_hour),
        "status_code": state,
        "status_text": description,
        "five_min_bars_observed": observations["5m"],
        "one_min_bars_observed": observations["1m"],
        "five_min_samples_kst": samples["5m"],
        "one_min_samples_kst": samples["1m"],
        "errors": errors,
        "tick_history_limit_note": "공식 체결 조회 API daysAgo 범위 1–7일: 오래된 2026-10-31 시간대 체결 조회 대체 불가",
        "changes_exit_result": False,
        "note": "1분·5분봉은 원인 확인용. 1시간봉을 합성하거나 미래 정보를 매수 조건으로 사용하지 않음.",
    }
