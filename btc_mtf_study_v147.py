"""V1.4.7 — retrospective cohort table for existing BTC-MTF scan results.

The input is the already-computed historical snapshot candidates. No new
market-data lookups are made and no candidate BUY/WATCH rules are changed.
A WATCH is an observation, NOT an executed trade. UNKNOWN is never profit.
"""
from collections import defaultdict
from math import isfinite
from statistics import mean, median

VERSION = "RESEARCH_COHORT_V147"
KINDS = ("BUY", "EARLY_WATCH", "TREND_WATCH", "RESEARCH")
QUALITY = ("Q4", "Q23", "Q01", "Q_UNKNOWN")
COVERAGE = ("COMPLETE_24H", "INCOMPLETE_24H", "UNKNOWN_24H")


def _float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return n if isfinite(n) else None


def _quality(q):
    score = q.get("checks_passed")
    n = _float(score)
    if n is None or n < 0 or n > 4 or int(n) != n:
        return "Q_UNKNOWN"
    if n == 4:
        return "Q4"
    return "Q23" if n >= 2 else "Q01"


def _coverage(q):
    if q.get("observed_turnover_is_complete") is True and q.get("hourly_bars_observed") == 24:
        return "COMPLETE_24H"
    if q.get("hourly_bars_observed") is not None:
        return "INCOMPLETE_24H"
    return "UNKNOWN_24H"


def _group(rows, label):
    done = [x["net_pct"] for x in rows if x["status"] == "RESOLVED"]
    pos = sum(x > 0 for x in done)
    size = len(rows)
    return {
        "label": label,
        "candidates": size,
        "unique_markets": len({x["market"] for x in rows}),
        "unique_snapshots": len({x["at"] for x in rows}),
        "resolved": len(done),
        "unknown": size-len(done),
        "positive": pos,
        "nonpositive": len(done)-pos,
        "resolution_rate_pct": round(100*len(done)/size, 2) if size else None,
        "positive_fraction_resolved_pct": round(100*pos/len(done), 2) if done else None,
        "mean_hypothetical_net_pct": round(mean(done), 4) if done else None,
        "median_hypothetical_net_pct": round(median(done), 4) if done else None,
        "min_hypothetical_net_pct": round(min(done), 4) if done else None,
        "max_hypothetical_net_pct": round(max(done), 4) if done else None,
        "small_sample": len(done) < 5,
        "watch_is_not_trade": True,
    }


def compare_watch_cohorts(items, scanned=0, issues=0, fee_each=0.04):
    """Compare already stored per-snapshot rows without backtest lookahead.

    Never infer a virtual gain from missing hourly candles. A non-finite or
    missing P&L stays UNKNOWN even if the caller marks status RESOLVED.
    """
    if not isinstance(items, list) or len(items) > 1500:
        raise ValueError("연구 비교는 후보 1500건 이하만 지원")
    if fee_each not in (0.04, 0.25):
        raise ValueError("편도 수수료 0.04 또는 0.25만 지원")
    clean = []
    used = set()
    duplicate_count = 0
    invalid_count = 0
    for item in items:
        if not isinstance(item, dict):
            invalid_count += 1
            continue
        market = str(item.get("market") or "").strip().upper()
        at = str(item.get("at") or "").strip()
        kind = str(item.get("kind") or "")
        if not (market.startswith("KRW-") and market[4:].isalnum()
                and 4 <= len(market) <= 22 and len(at) >= 10
                and kind in KINDS):
            invalid_count += 1
            continue
        key = market, at
        if key in used:
            duplicate_count += 1
            continue
        used.add(key)
        q = item.get("liquidity_audit") or {}
        if not isinstance(q, dict):
            q = {}
        bt = item.get("backtest") or {}
        if not isinstance(bt, dict):
            bt = {}
        net = _float(bt.get("net_pct"))
        status = "RESOLVED" if bt.get("status") == "RESOLVED" and net is not None else "UNKNOWN"
        clean.append({
            "market": market, "at": at, "kind": kind,
            "quality": _quality(q), "coverage": _coverage(q),
            "quality_checks_passed": q.get("checks_passed"),
            "hourly_bars_observed": q.get("hourly_bars_observed"),
            "status": status,
            "net_pct": net if status == "RESOLVED" else None,
            "backtest_reason": str(bt.get("reason") or "")[:160],
            "historical_turnover_krw": _float(q.get("observed_turnover_24h_krw")),
            "legacy_signal": str(item.get("legacy_signal") or ""),
        })
    by_kind = [_group([x for x in clean if x["kind"] == k], k) for k in KINDS]
    by_quality = [_group([x for x in clean if x["quality"] == k], k) for k in QUALITY]
    by_coverage = [_group([x for x in clean if x["coverage"] == k], k) for k in COVERAGE]
    cross = [
        {**_group([x for x in clean if x["kind"] == k and x["quality"] == q],
                  k+" × "+q), "kind": k, "quality": q}
        for k in KINDS for q in QUALITY
    ]
    by_day = [_group([x for x in clean if x["at"][:10] == day], day)
              for day in sorted({x["at"][:10] for x in clean})]
    original_buy = _group([x for x in clean if x["legacy_signal"] == "BUY"], "ORIGINAL_BUY")
    total = _group(clean, "ALL")
    warnings = [
        "WATCH와 RESEARCH의 120시간 가격 경로는 실제 진입 거래가 아닌 사후 가정",
        "UNKNOWN은 미확정으로 별도 집계, 승률/평균 분모에서 제외",
        "확정 사례만으로 집계한 수익 비율은 누락 편향을 포함할 수 있음",
        "여러 날 같은 종목은 서로 독립된 표본이 아님 (고유 종목 수 병기)",
        "종목 목록·거래대금 검색 범위는 현재 티커 기반이라 과거 시점 생존/미래정보 편향 있음",
        "유동성 4/4는 후보의 선행 관측 조건일 뿐 수익확률이나 BUY 조건 아님",
        "특히 확정 5건 미만 그룹의 평균 및 양수 비율은 실전 성능 판단 근거 부족",
    ]
    return {
        "version": VERSION,
        "fee_each_pct": fee_each,
        "scanned": int(scanned) if isinstance(scanned, int) and 0 <= scanned <= 100000 else 0,
        "issues": int(issues) if isinstance(issues, int) and 0 <= issues <= 100000 else 0,
        "duplicates_skipped": duplicate_count,
        "invalid_skipped": invalid_count,
        "summary": total,
        "original_buy": original_buy,
        "by_kind": by_kind,
        "by_quality": by_quality,
        "by_coverage": by_coverage,
        "by_kind_quality": cross,
        "by_day": by_day,
        "rows": clean,
        "warnings": warnings,
    }
