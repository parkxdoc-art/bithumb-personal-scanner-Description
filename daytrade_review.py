"""V10: scan the full active Bithumb KRW universe in resumable 5-market batches.

All V9 strategy logic is unchanged. Existing 10/30/50 snapshots remain untouched.
The full-universe option stores original raw candles in separate IndexedDB chunks.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from math import isfinite
from threading import Lock
import re
import time

from flask import jsonify, render_template_string, request

DAYS = ("2026-11-01", "2026-11-04", "2026-11-07", "2026-11-08")
HOURS = (1, 3, 6, 12, 24)
LABELS = tuple(f"{h}h" for h in HOURS)
EMPTY = {k: 0 for k in LABELS}
LIQUIDITY_MIN_KRW = 500_000_000
MAX_GAP_FILL_BARS = 6  # up to 30 minutes without trades
MAX_SYNTHETIC_BARS_PER_WINDOW = 24  # max 4.2% of a 48h sample
MARKET_PATTERN = re.compile(r"^KRW-[A-Z0-9]+$")

# Analytical thresholds on NET proceeds after entry and exit fees.
# These are evaluation markers, NOT live order instructions or new BUY gates.
TP_NET_PCT = 2.0
SL_NET_PCT = -1.0
TOUCH_HOURS = 6
TOUCH_KEYS = ("tp_first", "sl_first", "same_bar", "gap_unknown", "none")

# Independent V5 hypothetical exit scenarios. TP and SL percentages below
# are RAW price moves from next-bar OPEN, not net returns after commissions.
# All are measured over six hours; they NEVER change entry signals.
EXIT_SCENARIOS = (
    ("p2_s1", 2.0, 1.0),
    ("p2_s15", 2.0, 1.5),
    ("p3_s15", 3.0, 1.5),
    ("p3_s2", 3.0, 2.0),
)
EXIT_OUTCOMES = ("tp_first", "sl_first", "same_bar", "gap_unknown", "time_exit")


def _net_pct(price, entry, fee):
    return ((price / entry) * (1 - fee) / (1 + fee) - 1) * 100


def analyze_trade_quality(history, future, entry, fee):
    """Explain an already-produced BUY signal using strictly preceding/future data.

    The signal is determined elsewhere. Do not use the future diagnostics
    for BUY decisions. Filled candles cannot establish a target/stop touch.
    """
    close = float(history[-1]["trade_price"])
    before = {}
    for label, n in (("15m", 3), ("30m", 6), ("60m", 12)):
        past = float(history[-1-n]["trade_price"])
        before[label] = round(100 * (close / past - 1), 3) if past > 0 else None
    volumes = [float(c["candle_acc_trade_volume"]) for c in history[-21:-1]]
    vbase = sum(volumes) / len(volumes)
    vlast = float(history[-1]["candle_acc_trade_volume"])
    vr = round(vlast / vbase, 2) if vbase > 0 else None
    gap_before = sum(bool(c.get("_gap_fill")) for c in history[-80:])

    excursions = {}
    for h in (1, 3, 6):
        sample = future[:12*h]
        observed = [c for c in sample if not c.get("_gap_fill")]
        if observed:
            max_high = max(float(c["high_price"]) for c in observed)
            min_low = min(float(c["low_price"]) for c in observed)
            excursions[f"{h}h"] = {
                "max_up_pct": round((max_high / entry - 1) * 100, 3),
                "max_down_pct": round((min_low / entry - 1) * 100, 3),
                "gap_bars": len(sample) - len(observed),
            }
        else:
            excursions[f"{h}h"] = {
                "max_up_pct": None, "max_down_pct": None,
                "gap_bars": len(sample),
            }

    # Convert NET target/stop into raw intrabar price barriers.
    tp_price = entry * (1 + TP_NET_PCT / 100) * (1 + fee) / (1 - fee)
    sl_price = entry * (1 + SL_NET_PCT / 100) * (1 + fee) / (1 - fee)
    first_tp_min = first_sl_min = None
    outcome = "none"
    first_touch_min = None
    passed_unknown_gap = False
    for idx, candle in enumerate(future[:12*TOUCH_HOURS], 1):
        if candle.get("_gap_fill"):
            passed_unknown_gap = True
            continue
        hit_tp = float(candle["high_price"]) >= tp_price
        hit_sl = float(candle["low_price"]) <= sl_price
        if hit_tp and first_tp_min is None:
            first_tp_min = idx * 5
        if hit_sl and first_sl_min is None:
            first_sl_min = idx * 5
        if outcome == "none" and (hit_tp or hit_sl):
            first_touch_min = idx * 5
            if passed_unknown_gap:
                outcome = "gap_unknown"
            elif hit_tp and hit_sl:
                outcome = "same_bar"
            else:
                outcome = "tp_first" if hit_tp else "sl_first"
    if outcome == "none" and passed_unknown_gap:
        outcome = "gap_unknown"
    return {
        "before_pct": before, "volume_ratio_20": vr,
        "gap_before_80": gap_before,
        "excursions": excursions, "first_touch": outcome,
        "first_touch_minutes": first_touch_min,
        "tp_first_minutes": first_tp_min, "sl_first_minutes": first_sl_min,
    }



def analyze_exit_scenarios(future, entry, fee):
    """Check four hypothetical RAW-price targets/stops on observed OHLC only.

    The next bar opens at the assumed entry price. A missing bar prior to
    any observed hit makes the first-hit order unknowable. A bar hitting both
    bounds is ambiguous. Unknown cases NEVER receive an invented P&L.
    When no level is hit in 72 observed 5m bars, exit at bar 72 close.
    This is a backtest diagnostic, not an order execution simulator.
    """
    if len(future) < 12 * TOUCH_HOURS:
        raise ValueError("6시간 미래 캔들이 부족합니다")
    if not isfinite(entry) or entry <= 0:
        raise ValueError("진입가 오류")

    results = {}
    for key, take_pct, stop_pct in EXIT_SCENARIOS:
        tp_price = entry * (1 + take_pct / 100)
        sl_price = entry * (1 - stop_pct / 100)
        had_gap = False
        status = None
        first_min = None
        exit_price = None
        for idx, candle in enumerate(future[:12 * TOUCH_HOURS], start=1):
            if candle.get("_gap_fill"):
                had_gap = True
                continue
            high = float(candle["high_price"])
            low = float(candle["low_price"])
            if not isfinite(high) or not isfinite(low) or high < low or low <= 0:
                raise ValueError("고가·저가 데이터 오류")
            hit_tp, hit_sl = high >= tp_price, low <= sl_price
            if not (hit_tp or hit_sl):
                continue
            first_min = idx * 5
            if had_gap:
                status = "gap_unknown"
            elif hit_tp and hit_sl:
                status = "same_bar"
            elif hit_tp:
                status, exit_price = "tp_first", tp_price
            else:
                status, exit_price = "sl_first", sl_price
            break
        if status is None:
            if had_gap:
                status = "gap_unknown"
            else:
                status = "time_exit"
                exit_price = float(future[12 * TOUCH_HOURS - 1]["trade_price"])
                if not isfinite(exit_price) or exit_price <= 0:
                    raise ValueError("6시간 종가 데이터 오류")
                first_min = 12 * TOUCH_HOURS * 5

        net_return = _net_pct(exit_price, entry, fee) if exit_price is not None else None
        results[key] = {
            "status": status, "minutes": first_min,
            "net_pct": round(net_return, 3) if net_return is not None else None,
        }
    return results

# Fixed, exploratory entry-time gates; no future candle is used in these gates.
# These gates DO NOT change the underlying A/B BUY decisions or any V5 result.
V6_GATE_LABELS = (
    ("all", "B 전체 BUY (기준)"),
    ("raw80", "신호 전 80봉 모두 원본"),
    ("momentum", "15분 ≥ 0% · 30분 ≥ +0.5%"),
    ("momentum_volume", "방향 + 거래량 ≥ 1.5배"),
    ("raw_momentum_volume", "원본80 + 방향 + 거래량"),
)
V6_STATUSES = ("tp_first", "sl_first", "same_bar", "gap_unknown", "time_exit")


def v6_gate_flags(quality):
    """Pre-signal features only: close returns, prior bar volumes and past gaps.

    Outcome and future-gap flags deliberately do NOT enter this function.
    """
    before = quality["before_pct"]
    m15, m30 = before.get("15m"), before.get("30m")
    raw80 = quality["gap_before_80"] == 0
    momentum = (m15 is not None and m30 is not None
                and m15 >= 0.0 and m30 >= 0.5)
    vr = quality["volume_ratio_20"]
    volume = vr is not None and vr >= 1.5
    return {
        "all": True,
        "raw80": raw80,
        "momentum": momentum,
        "momentum_volume": momentum and volume,
        "raw_momentum_volume": raw80 and momentum and volume,
    }


def new_v6_summary():
    return {key: {
        "signals": 0, "resolvable": 0, "net_sum": 0.0, "positive": 0,
        "one_hour_sum": 0.0, "three_hour_sum": 0.0,
        "statuses": {status: 0 for status in V6_STATUSES},
        "future_6h_raw": 0,
    } for key, _ in V6_GATE_LABELS}


def add_v6_observation(summary, flags, scenario, quality, net):
    """Summarize an already-issued B BUY without changing it."""
    state, pnl = scenario["status"], scenario["net_pct"]
    if state not in V6_STATUSES:
        raise ValueError("V6 손익비 결과 종류 오류")
    for key, allowed in flags.items():
        if not allowed:
            continue
        row = summary[key]
        row["signals"] += 1
        row["statuses"][state] += 1
        row["one_hour_sum"] += net["1h"]
        row["three_hour_sum"] += net["3h"]
        if quality["excursions"]["6h"]["gap_bars"] == 0:
            row["future_6h_raw"] += 1
        if pnl is not None:
            row["resolvable"] += 1
            row["net_sum"] += pnl
            row["positive"] += int(pnl > 0)




# V7: pullback/rebound entry study on the *same already-issued B BUY signals*.
# No signals or V5/V6 result logic are changed. The gate only observes bars
# AFTER the completed BUY signal, and enters at the OPEN of the bar following
# a completed rebound-confirmation candle. A rally without a dip is SKIPPED.
V7_WAIT_BARS = 12                 # 12 closed 5m bars = maximum 60m to confirm
V7_INVALID_DROP_PCT = 3.0        # reject a breakdown before confirmation
V7_VARIANTS = (
    ("dip05", 0.5, "0.5% 눌림 + 반등 확인"),
    ("dip10", 1.0, "1.0% 눌림 + 반등 확인"),
)
V7_NO_ENTRIES = ("no_pullback", "no_rebound", "breakdown", "missed_price", "gap_unknown")


def evaluate_v7_pullback(history, future, fee, dip_pct):
    """Test a *missed-or-filled* delayed entry without knowing later candles.

    At signal close we know only history. For each new completed candle we:
    (1) require observed raw candles, (2) cancel on a >=3% breakdown,
    (3) remember the first raw low touching the dip level, and (4) only on a
    LATER completed candle confirm a bullish close over the previous close.
    A hypothetical trade enters at the next raw bar's OPEN only when that
    OPEN is no higher than the original signal close. Otherwise NO ENTRY.
    No same-bar OHLC ordering assumptions are used for entry. Exits are the
    unchanged V5 raw-price +3%/-2% first-touch simulation for 6h AFTER entry.
    """
    if len(history) < 1 or len(future) < V7_WAIT_BARS + 1 + 72:
        raise ValueError("V7 진입 및 이후 6시간 데이터가 부족합니다")
    reference = float(history[-1]["trade_price"])
    if not isfinite(reference) or reference <= 0:
        raise ValueError("V7 신호 종가 오류")
    if dip_pct not in (0.5, 1.0):
        raise ValueError("지원하지 않는 눌림 기준")
    dip_price = reference * (1 - dip_pct / 100)
    breakdown_price = reference * (1 - V7_INVALID_DROP_PCT / 100)
    pullback_at = None

    for idx in range(V7_WAIT_BARS):
        candle = future[idx]
        # Missing candles are not observable historical trading opportunities.
        if candle.get("_gap_fill"):
            return {"status": "gap_unknown", "minutes": None, "scenario": None,
                    "dip_seen": pullback_at is not None, "entry": None}
        high = float(candle["high_price"])
        low = float(candle["low_price"])
        open_price = float(candle["opening_price"])
        close = float(candle["trade_price"])
        if (not all(isfinite(x) for x in (high, low, open_price, close))
                or min(low, open_price, close) <= 0
                or not low <= min(open_price, close) <= max(open_price, close) <= high):
            raise ValueError("V7 진입 전 OHLC 가격 오류")
        # Use a complete observed candle to reject a sharp breakdown.
        # If the candle also contains a bounce, intrabar order is irrelevant:
        # any observed breakdown invalidates this exploratory setup.
        if low <= breakdown_price:
            return {"status": "breakdown", "minutes": None, "scenario": None,
                    "dip_seen": True, "entry": None}
        if pullback_at is None:
            if low <= dip_price:
                pullback_at = idx
            continue
        # The rebound must be confirmed on a *later* closed raw candle.
        prior = future[idx-1]
        if prior.get("_gap_fill"):
            return {"status": "gap_unknown", "minutes": None, "scenario": None,
                    "dip_seen": True, "entry": None}
        prior_close = float(prior["trade_price"])
        if close <= open_price or close <= prior_close:
            continue
        entry_index = idx + 1
        entry_candle = future[entry_index]
        if entry_candle.get("_gap_fill"):
            return {"status": "gap_unknown", "minutes": None, "scenario": None,
                    "dip_seen": True, "entry": None}
        entry = float(entry_candle["opening_price"])
        if not isfinite(entry) or entry <= 0:
            raise ValueError("V7 다음 봉 시가 오류")
        # The user's rule: if it already ran away, it is not our trade.
        if entry > reference:
            return {"status": "missed_price", "minutes": None, "scenario": None,
                    "dip_seen": True, "entry": None}
        if entry <= breakdown_price:
            return {"status": "breakdown", "minutes": None, "scenario": None,
                    "dip_seen": True, "entry": None}
        exits = analyze_exit_scenarios(future[entry_index:entry_index+72], entry, fee)
        scenario = exits["p3_s2"]
        return {"status": "entered", "minutes": entry_index * 5,
                "entry": round(entry, 10), "entry_vs_signal_pct": round(100 * (entry / reference - 1), 3),
                "scenario": scenario, "dip_seen": True}

    return {"status": "no_rebound" if pullback_at is not None else "no_pullback",
            "minutes": None, "scenario": None,
            "dip_seen": pullback_at is not None, "entry": None}


def new_v7_summary():
    return {key: {
        "signals": 0, "entered": 0,
        "not_entered": {k: 0 for k in V7_NO_ENTRIES},
        "outcomes": {k: 0 for k in EXIT_OUTCOMES},
        "resolvable": 0, "net_sum": 0.0, "positive": 0,
        "same_signal_immediate_n": 0, "same_signal_immediate_sum": 0.0,
        "skipped_immediate_tp": 0, "skipped_immediate_sl": 0,
        "wait_minutes_sum": 0, "entry_vs_signal_sum": 0.0,
    } for key, _, _ in V7_VARIANTS}


def add_v7_observation(summary, key, decision, immediate_scenario):
    """Only classify already-issued B BUY; no change to original signal."""
    row = summary[key]
    row["signals"] += 1
    if decision["status"] != "entered":
        status = decision["status"]
        if status not in row["not_entered"]:
            raise ValueError("V7 미진입 사유 오류")
        row["not_entered"][status] += 1
        if immediate_scenario["status"] == "tp_first":
            row["skipped_immediate_tp"] += 1
        if immediate_scenario["status"] == "sl_first":
            row["skipped_immediate_sl"] += 1
        return
    row["entered"] += 1
    row["wait_minutes_sum"] += decision["minutes"]
    row["entry_vs_signal_sum"] += decision["entry_vs_signal_pct"]
    exit_result = decision["scenario"]
    state = exit_result["status"]
    if state not in row["outcomes"]:
        raise ValueError("V7 익절·손절 결과 오류")
    row["outcomes"][state] += 1
    if exit_result["net_pct"] is not None:
        row["resolvable"] += 1
        row["net_sum"] += exit_result["net_pct"]
        row["positive"] += int(exit_result["net_pct"] > 0)
    # Paired reference: the hypothetical immediate-entry outcome on THESE
    # SAME B BUY events, not the larger, unmatched unconditional population.
    if immediate_scenario["net_pct"] is not None:
        row["same_signal_immediate_n"] += 1
        row["same_signal_immediate_sum"] += immediate_scenario["net_pct"]


# V9: Pre-entry, test-only quality flags for the V7 -1% pullback entry.
# "Support" is a PROVISIONAL low of the first observed dip candle, not a
# confirmed higher-timeframe support zone or a validated double bottom.
# Only closed candles available BEFORE the V7 entry open are inspected.
# Neither these flags nor V7 itself can alter the original A/B BUY signals.
V9_SUPPORT_BREAK_TOLERANCE = 0.002   # at most 0.2% under first dip low
V9_MIN_BODY_PCT = 0.25              # confirmation candle bullish body
V9_MIN_VOLUME_RATIO = 1.30          # completed confirmation / previous 20 raw bars
V9_VARIANTS = (("support", "눌림 저점 지지"),
               ("strong", "지지 + 강한 반등·거래량"))
V9_REJECTIONS = ("v7_skip", "support_broken", "weak_rebound",
                 "weak_volume", "volume_unknown", "data_unknown")


def evaluate_v9_confirmation(history, future, v7_entry):
    """Observe a V7 entry, never choose a later entry based on performance.

    The V7 decision supplies its PRE-ESTABLISHED entry_index. We only read
    future[:entry_index], i.e. data known at that entry's candle opening.
    Outcome/exit information contained in v7_entry is intentionally ignored.
    """
    if v7_entry["status"] != "entered":
        return {"support": False, "strong": False, "reason": "v7_skip",
                "dip_low": None, "body_pct": None, "volume_ratio": None}
    entry_index = int(v7_entry["minutes"] // 5)
    if (entry_index < 2 or entry_index > V7_WAIT_BARS
            or len(future) <= entry_index or not history):
        raise ValueError("V9 진입 시각이 V7 범위를 벗어났습니다")
    reference = float(history[-1]["trade_price"])
    dip_limit = reference * 0.99
    confirmation_idx = entry_index - 1
    dip_idx = None
    for i in range(confirmation_idx):
        bar = future[i]
        if bar.get("_gap_fill"):
            return {"support": False, "strong": False, "reason": "data_unknown",
                    "dip_low": None, "body_pct": None, "volume_ratio": None}
        if float(bar["low_price"]) <= dip_limit:
            dip_idx = i
            break
    if dip_idx is None:
        raise ValueError("V9 V7 눌림 시각을 찾지 못했습니다")
    first_dip = future[dip_idx]
    dip_low = float(first_dip["low_price"])
    dip_close = float(first_dip["trade_price"])
    confirm = future[confirmation_idx]
    if confirm.get("_gap_fill"):
        return {"support": False, "strong": False, "reason": "data_unknown",
                "dip_low": None, "body_pct": None, "volume_ratio": None}
    # A rebound is NOT a held floor if a later raw bar breaks the first low
    # by >0.2%, or the closed confirming candle cannot regain the dip close.
    held = (float(confirm["trade_price"]) > dip_close and
            all(not future[i].get("_gap_fill") and
                float(future[i]["low_price"]) >= dip_low * (1 - V9_SUPPORT_BREAK_TOLERANCE)
                for i in range(dip_idx + 1, confirmation_idx + 1)))
    opening = float(confirm["opening_price"])
    closing = float(confirm["trade_price"])
    if not isfinite(opening) or opening <= 0 or not isfinite(closing):
        raise ValueError("V9 확인봉 가격 오류")
    body_pct = 100 * (closing / opening - 1)
    # Average exactly the 20 known bars preceding the confirmation bar.
    # Synthetic zero-volume candles would distort the activity test; mark
    # those cases unverified instead of treating them as a pass/fail.
    known_prior = list(history) + list(future[:confirmation_idx])
    prior20 = known_prior[-20:]
    volume_ratio = None
    if (len(prior20) == 20 and
            all(not c.get("_gap_fill") for c in prior20)):
        amounts = [float(c["candle_acc_trade_volume"]) for c in prior20]
        curr_vol = float(confirm["candle_acc_trade_volume"])
        if (all(isfinite(v) and v >= 0 for v in amounts + [curr_vol])
                and sum(amounts) > 0):
            volume_ratio = curr_vol / (sum(amounts) / 20)
    reason = ("support_broken" if not held else
              "weak_rebound" if body_pct < V9_MIN_BODY_PCT else
              "volume_unknown" if volume_ratio is None else
              "weak_volume" if volume_ratio < V9_MIN_VOLUME_RATIO else "entered")
    return {"support": held, "strong": reason == "entered", "reason": reason,
            "dip_low": round(dip_low, 10), "body_pct": round(body_pct, 3),
            "volume_ratio": round(volume_ratio, 3) if volume_ratio is not None else None}


def new_v9_summary():
    return {key: {
        "signals": 0, "v7_entered": 0, "entered": 0,
        "rejected": {name: 0 for name in V9_REJECTIONS},
        "outcomes": {name: 0 for name in EXIT_OUTCOMES},
        "resolvable": 0, "net_sum": 0.0, "positive": 0,
        "wait_minutes_sum": 0, "rejected_v7_tp": 0, "rejected_v7_sl": 0,
        "paired_v7_n": 0, "paired_v7_net_sum": 0.0,
    } for key, _ in V9_VARIANTS}


def add_v9_observation(summary, flags, v7_entry):
    """Count each already-produced B signal once per fixed V9 hypothesis."""
    for key, _label in V9_VARIANTS:
        row = summary[key]
        row["signals"] += 1
        if v7_entry["status"] != "entered":
            row["rejected"]["v7_skip"] += 1
            continue
        row["v7_entered"] += 1
        admitted = flags[key]
        if not admitted:
            reason = "support_broken" if key == "support" else flags["reason"]
            if flags["reason"] == "data_unknown":
                reason = "data_unknown"
            if reason not in row["rejected"]:
                raise ValueError("V9 제외 사유 오류")
            row["rejected"][reason] += 1
            if v7_entry["scenario"]["status"] == "tp_first":
                row["rejected_v7_tp"] += 1
            elif v7_entry["scenario"]["status"] == "sl_first":
                row["rejected_v7_sl"] += 1
            continue
        row["entered"] += 1
        row["wait_minutes_sum"] += v7_entry["minutes"]
        outcome = v7_entry["scenario"]
        status = outcome["status"]
        row["outcomes"][status] += 1
        if outcome["net_pct"] is not None:
            row["resolvable"] += 1
            row["net_sum"] += outcome["net_pct"]
            row["positive"] += int(outcome["net_pct"] > 0)
            # Same entry and identical exit as V7 -1%: a paired identity test.
            row["paired_v7_n"] += 1
            row["paired_v7_net_sum"] += outcome["net_pct"]

PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>단타 A/B 백테스트 V9 | 빗썸 스캐너</title>
<style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,sans-serif}
*{box-sizing:border-box}body{margin:0;background:#0b1020;color:#edf3ff;padding:16px}
main{max-width:1040px;margin:auto}h1{font-size:23px}h2{font-size:17px;margin:0 0 12px}
p,small{color:#a9b7d2;line-height:1.6}a{color:#92b7ff}
section{background:#141c2f;border:1px solid #30415f;border-radius:14px;padding:15px;margin:14px 0}
.controls{display:flex;flex-wrap:wrap;gap:12px;align-items:end}
.field{display:flex;flex-direction:column;gap:5px;min-width:110px;flex:1}
label{font-size:13px;color:#a9b7d2}select,button{font-size:15px;padding:10px;border-radius:8px;border:1px solid #384d71}
select{background:#0b1426;color:white}button{background:#315de7;color:white;cursor:pointer;font-weight:bold;min-width:120px}
button:disabled{opacity:.55;cursor:default}
.summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.tile{padding:12px;border:1px solid #344562;border-radius:10px;background:#111b31}
.tile b{font-size:22px}.tile small{display:block;font-size:12px}
.tablewrap{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:620px}
tr{border-bottom:1px solid #293750}td,th{text-align:left;padding:9px;font-size:12px;white-space:nowrap}th{color:#afbdd8}
strong.green{color:#5ee0aa}strong.yellow{color:#ffd77d}
.warn{color:#ffd77d}.error{color:#ff9aa6}#status{min-height:30px}
progress{width:100%;height:14px;accent-color:#54b89c}ul{padding-left:20px}
.eventcard{margin:10px 0;padding:12px;background:#111b31;border:1px solid #344562;border-radius:10px}
.eventcard>div{margin-top:6px;font-size:13px;line-height:1.55}
.eventcard strong{color:#cfe0ff}.muted{color:#a9b7d2}.tiny{font-size:12px;color:#a9b7d2}
#touchSummary{line-height:1.8;white-space:pre-wrap}
.snapshot-tools{margin:12px 0;padding:10px;border:1px solid #344562;border-radius:8px}
.snapshot-tools small{display:block;margin-bottom:8px}
button.secondary{background:#203150;color:#d9e7ff;min-width:unset;font-size:12px;padding:8px}

@media(min-width:750px){.summary{grid-template-columns:repeat(4,minmax(0,1fr))}}
</style></head><body><main>
<a href="/">← 빗썸 실시간 스캐너로 돌아가기</a>
<h1>📊 단타 A/B 검증판 · V10</h1>
<p>기존 BUY(A)와 피보나치(B)를 같은 과거 5분봉에서 비교합니다. 실시간 주문 기능은 없습니다.</p>
<section><h2>백테스트 설정</h2>
<div class="controls">
<div class="field"><label for="day">검사일 (KST)</label><select id="day">
<option>2026-11-08</option><option>2026-11-07</option><option>2026-11-04</option><option>2026-11-01</option>
</select></div>
<div class="field"><label for="limit">코인 수</label><select id="limit">
<option value="10">10개</option><option value="30" selected>30개</option><option value="50">50개</option><option value="all">전체 KRW (5개씩 검사)</option>
</select></div>
<div class="field"><label for="fee">매수·매도 각각 수수료</label><select id="fee">
<option value="0.25" selected>0.25%</option><option value="0.04">0.04%</option><option value="0">0%</option>
</select></div>
<button id="start" type="button">A/B 분석 시작</button>
</div>
<small>10·30·50개는 현재 거래대금 상위 코인, <b>전체 KRW</b>는 현재 상장된 모든 KRW 코인을 선택합니다. 각 코인 수의 목록은 모든 날짜에 공통 고정됩니다. 전체 모드는 <b>5개씩 검사하고 자동 저장</b>하여 중단돼도 이어서 검사할 수 있습니다. 과거 당일 상장 목록이 아닌 현재 상장 목록이므로 생존·선정 편향은 남습니다.</small>
<div class="snapshot-tools">
  <small id="snapshotInfo">저장된 원본 데이터 확인 중…</small>
  <div class="controls">
    <button class="secondary" id="snapshotExport" type="button">📥 고정 원본 백업</button>
    <button class="secondary" id="snapshotImport" type="button">📂 백업 불러오기</button>
    <button class="secondary" id="snapshotReset" type="button">↻ 이 날짜 원본만 해제</button>
    <button class="secondary" id="cohortReset" type="button">코인 목록·모든 날짜 초기화</button>
    <input id="snapshotFile" type="file" accept=".json,.gz,application/json,application/gzip" hidden>
  </div>
</div>
<p id="status" role="status">설정을 고르고 분석 시작을 누르세요.</p>
<progress id="progress" max="1" value="0"></progress>
</section>
<section><h2>전략 비교</h2>
<div class="summary">
<div class="tile"><small>조회 완료 코인</small><b id="count">0</b></div>
<div class="tile"><small>유효 검증 구간</small><b id="windows">0</b></div>
<div class="tile"><small>A 기존 BUY</small><b id="aCount">0</b></div>
<div class="tile"><small>B 피보나치 BUY</small><b id="bCount">0</b></div>
</div>
<div class="tablewrap"><table><thead><tr><th>전략</th><th>시간</th><th>표본 수</th><th>평균 순수익률</th><th>수익 비율</th></tr></thead><tbody id="metrics"></tbody></table></div>
<small>신호 직후 다음 봉 시가 매수 → 지정 시간 뒤 종가 매도. 설정 수수료 반영, 슬리피지·세금·실제 체결 영향 제외. 목표/손절을 적용한 실거래 성과는 아닙니다.</small>
</section>
<section><h2>진단: BUY가 없는 이유 찾기</h2>
<div id="diag" class="warn">결과 대기 중</div>
</section>
<section><h2>코인별 결과</h2><div class="tablewrap"><table><thead><tr><th>코인</th><th>유효 구간</th><th>A BUY</th><th>B BUY</th><th>상태</th></tr></thead><tbody id="coinRows"></tbody></table></div></section>
<section><h2>6시간 익절·손절 선도달 분석</h2>
<p>순수익 +2% 목표와 -1% 손절 기준(선택 수수료 양방향 포함). 다음 봉 시가 진입 가정입니다. 두 가격을 같은 5분봉에서 모두 터치하면 순서를 알 수 없으므로 '동일봉 불명'으로, 도달 전 누락 캔들이 있으면 '공백 불명'으로 분리합니다. 실제 주문 체결과 같지 않습니다.</p>
<div id="touchSummary">결과 대기 중</div>
</section>
<section><h2>V5 · 6시간 가격 기준 손익비 4종 비교</h2>
<p>기존 V4의 <b>순수익 목표 +2% / 손절 -1%</b>와 달리, 아래 4개는 <b>매수가 대비 실제 가격 변동률</b> 기준입니다. 동일한 A/B BUY에서 가정한 다음 5분봉 시가로 진입하여, 6시간 내 먼저 닿은 목표·손절 가격에 청산한 것으로 계산합니다. 어느 쪽도 닿지 않으면 6시간 종가 청산을 가정합니다.</p>
<p>표의 평균 순수익률은 선택한 양방향 수수료를 반영하며, <b>동일봉 동시 터치·먼저 지나간 누락봉</b>은 수익률과 성공률 계산에서 제외합니다. 실제 지정가·시장가 체결, 갭 슬리피지, 호가잔량은 반영하지 못합니다.</p>
<div class="tablewrap"><table><thead><tr><th>전략</th><th>가격 익절/손절</th><th>익절 먼저</th><th>손절 먼저</th><th>동일봉 불명</th><th>공백 불명</th><th>6h 종가청산</th><th>판정가능</th><th>평균 순수익률</th><th>수익 비율</th></tr></thead><tbody id="scenarioRows"></tbody></table></div>
<small id="scenarioNotice">결과 대기 중</small>
</section>
<section><h2>V6 · B전략 진입 시점 조건 비교 (실험)</h2>
<p>기존 B BUY를 유지한 채, <b>신호 발생 시점 이전</b>에 이미 확인 가능한 정보로만 분류합니다. 조건은 기준 / 원본 80봉 / 직전 15분 ≥ 0%와 30분 ≥ +0.5% / 여기에 직전 거래량 ≥ 20봉 평균 1.5배 / 원본+방향+거래량입니다. 미래 고가·저가는 진입 판정에 쓰지 않습니다.</p>
<p><b>비교 손익비:</b> V5 가격 기준 +3% 익절 / -2% 손절, 최대 6시간. 수수료 반영. 순수익 평균·수익 비율은 익절·손절·6시간 종가 청산으로 판정할 수 있는 사례만 사용하고, 공백·동일봉 불명은 제외합니다. 원본 80봉 조건은 과거 데이터 품질로만 선별합니다. <b>미래 6시간 원본봉 수는 품질 진단일 뿐, 필터에 쓰지 않습니다.</b></p>
<div class="tablewrap"><table><thead><tr><th>진입 전 조건</th><th>원래 B BUY 중</th><th>익절 먼저</th><th>손절 먼저</th><th>불명</th><th>판정가능</th><th>평균 6h 가정 순수익</th><th>수익 비율</th><th>1h 종가 평균</th><th>3h 종가 평균</th><th>미래 6h 원본봉만</th></tr></thead><tbody id="v6Rows"></tbody></table></div>
<small id="v6Notice">결과 대기 중</small>
</section>

<section><h2>V7 · 눌림 확인 후 진입 (추격매수 안 함)</h2>
<p><b>규칙:</b> 기존 B BUY 후 최대 60분 동안 <b>신호 종가보다 -0.5% 또는 -1.0% 이상 눌림</b>을 기다립니다. 눌림을 본 <b>다음 이후의 완료된 5분봉</b>에서 양봉이면서 직전 봉 종가보다 높게 마감할 때만 반등을 확인합니다. 진입은 <b>그다음 실제 5분봉 시가</b>로 가정합니다.</p>
<p>진입 시가가 <b>원래 신호 종가보다 높아지면 추격하지 않습니다.</b> 기다리는 중 -3% 이상 급락, 미반등, 미눌림, 누락 캔들은 각각 <b>미진입·판정 불명</b>으로 기록합니다. 눌림이 없이 바로 상승한 기회는 수익으로 계산하지 않고 놓친 신호로 남깁니다. 진입 후에만 가격 기준 익절 +3% / 손절 -2%, 최대 6시간을 적용합니다.</p>
<div class="tablewrap"><table><thead><tr><th>진입 방식</th><th>B BUY</th><th>실제 가정 진입</th><th>미진입</th><th>미진입: 미눌림</th><th>미진입: 미반등</th><th>급락 취소</th><th>추격 방지</th><th>진입 전 공백</th><th>익절 먼저</th><th>손절 먼저</th><th>미래 판정 불명</th><th>6h 청산</th><th>판정 가능</th><th>평균 순수익률</th><th>수익 비율</th><th>평균 진입 대기</th><th>같은 신호 즉시 진입 평균</th><th>포기한 즉시진입 익절</th></tr></thead><tbody id="v7Rows"></tbody></table></div>
<small id="v7Notice">결과 대기 중</small>
</section>
<section><h2>V9 · 눌림 저점 지지 + 반등 강도 (실험)</h2>
<p><b>기존 -1% 눌림(V7) 진입과 동일한 진입 시점</b>을 사용합니다. 신호가 난 다음 보이는 첫 눌림봉의 저점을 <b>임시 지지선</b>으로 잡고, 반등 확인 전까지 저점을 0.2% 넘게 깨지 않았으며 확인봉 종가가 첫 눌림봉 종가보다 높아야 지지 통과입니다. <b>강한 반등</b>은 확인봉의 양봉 몸통이 0.25% 이상이고, 거래량이 그전 원본 20봉 평균의 1.3배 이상인 경우입니다.</p>
<p>두 조건은 <b>실험용 가설</b>이며 확인된 추세 지지선이나 수익성 인증이 아닙니다. V7의 최초 진입 시점을 바꾸지 않고, 통과하지 못한 신호는 가상 미진입으로 처리합니다. 누락봉이 포함되면 거래량을 추정해 통과시키지 않습니다.</p>
<div id="v9Cards">결과 대기 중</div>
<small>V9는 수수료 반영 가격 기준 익절 +3% / 손절 -2%, 진입 후 최대 6시간으로 비교합니다. 평균은 익절·손절·6시간 종가청산 등 판정 가능한 건만 계산합니다. 같은 종목의 반복 신호와 표본이 적은 날짜는 성능을 입증하지 못합니다.</small>
</section>
<section><h2>BUY별 진입 품질 · 진입 후 움직임</h2>
<small>진입 전 15·30·60분 수익률, 직전 봉 거래량/이전 20봉 평균, 6시간 중 실제 봉의 고가·저가 기준 최대 상승/하락을 비교합니다. 미래 최대치와 선도달 정보는 신호 시점에 알 수 없으므로 검증에만 사용합니다.</small>
<div id="qualityCards">결과 대기 중</div>
</section>
<section><h2>날짜별 검증 비교</h2>
<small>같은 브라우저의 고정 원본 5분봉을 재사용합니다(브라우저 저장 공간 삭제 시 사라질 수 있으므로 JSON 백업 권장). 여기 보이는 날짜별 결과 표 자체는 새로고침하면 초기화됩니다. 같은 코인 수/전체 KRW 고정 목록을 재사용하지만, 다른 코인 수의 결과와 수수료 설정이 다른 결과는 단순 합산하지 마세요.</small>
<div class="tablewrap"><table><thead><tr><th>날짜</th><th>코인/수수료</th><th>유효 코인</th><th>A BUY</th><th>B BUY</th><th>B 1h 평균</th><th>B 3h 평균</th><th>B 24h 평균</th></tr></thead><tbody id="dateRows"></tbody></table></div>
</section>
<section><h2>발생 신호 예시</h2><div id="events">결과 대기 중</div></section>
<section><h2>V8·V9·V10 고정 데이터 원칙</h2><p>처음 선정한 코인 목록을 같은 검사 코인 수의 <b>모든 날짜에 공통 사용</b>하고, 각 날짜의 <b>실거래 원본 캔들(보정 전)</b>을 휴대폰 브라우저 IndexedDB에 저장합니다. 저장된 날짜는 이후 빗썸 원본 캔들을 다시 요청하지 않습니다. 브라우저 데이터를 지우거나 기기를 바꾸면 별도의 JSON 백업을 불러와야 동일한 자료를 사용할 수 있습니다. 고정 자료도 캔들 누락과 현재 시점 선정 편향은 남습니다. 백업의 내용이 진짜 거래소 데이터인지는 별도로 확인해야 합니다.</p></section>
<section><h2>검증 범위에 대한 주의</h2><p>
하루 중 00:00~12:00에 확인된 신호를 최대 24시간까지 추적합니다. V7은 신호 이후 최대 60분 눌림·반등 확인 뒤 다음 봉 시가의 가상 진입만 비교하며, V9는 같은 -1% 눌림 진입에 임시 저점·반등 강도 검증을 추가합니다. 미진입 신호는 수익률 계산에 넣지 않습니다. V5/V6 손익비 비교는 6시간의 가격 기준 목표/손절이며, 수수료를 제외한 실제 체결 수익을 보장하지 않습니다. 익절/손절은 최대 6시간의 고가·저가 첫 터치 분석이며, 동일봉 안의 선후는 판별하지 못합니다. 5분봉 사이의 누락 체결과 슬리피지를 재현하지 못하므로 실전 주문 시뮬레이션이 아닙니다. 누락된 5분봉 중 30분 이하 공백만 직전 종가와 거래량 0으로 표시하여 시간축을 보정합니다(실제 체결 데이터가 아님). 48시간 구간에서 보정 봉이 24개를 넘거나 신호봉/매수봉이 보정 봉이면 제외합니다. 하루 또는 소수의 BUY 결과만으로 수익성을 증명할 수 없으며, 가상 가격은 실제 매매 체결을 보장하지 않습니다.
</p></section>
</main><script>
const $ = id => document.getElementById(id);
const horizons = ['1h','3h','6h','12h','24h'];
let running = false;
const dateHistory = new Map();
const fmt = x => x == null ? '—' : Number(x).toLocaleString('ko-KR');
const pct = x => x == null ? '—' : Number(x).toFixed(2) + '%';
function paint(all, done, total, rankInfo) {
  const complete = all.filter(x => !x.error);
  const count = complete.length;
  const windows = complete.reduce((s,x)=>s + (x.windows||0),0);
  $('count').textContent = fmt(count) + ' / ' + fmt(total) + ' (유효 ' + fmt(complete.filter(x=>x.windows>0).length) + ')';
  $('windows').textContent = fmt(windows);
  const rows = [];
  const agg = {};
  for(const strategy of ['A','B']) {
    let signals=0;
    let totals={};
    for(const key of horizons) totals[key]={n:0,sum:0,wins:0};
    for(const row of complete) {
      const x = row.strategies[strategy];
      signals += x.signals;
      for(const key of horizons){
        totals[key].n += x.samples[key];
        totals[key].sum += x.return_sums[key];
        totals[key].wins += x.positive[key];
      }
    }
    agg[strategy] = signals;
    horizons.forEach((key,i) => {
      const v=totals[key];
      rows.push(`<tr><td>${i?'':strategy==='A'?'A 기존 BUY':'B 피보나치'}</td><td>${key}</td><td>${fmt(v.n)}</td><td>${pct(v.n?v.sum/v.n:null)}</td><td>${pct(v.n?100*v.wins/v.n:null)}</td></tr>`);
    });
  }
  $('aCount').textContent=fmt(agg.A);
  $('bCount').textContent=fmt(agg.B);
  $('metrics').innerHTML=rows.join('');
  const diag={aBreakout:0,aSupport:0,bTrend:0,bTouch:0,bBounce:0,bVolume:0,bAllPrice:0,bSwing25:0,bEvaluated:0,liquidity:0,excluded:0};
  for(const x of complete){for(const k in diag) diag[k] += x.diagnostics[k] || 0;}
  $('diag').textContent = `누락 5분봉 시간축 보정 ${fmt(complete.reduce((s,x)=>s+(x.gap_filled||0),0))}개(코인별 조회 범위 합산) · 과거 24h 5억 통과 구간 ${fmt(diag.liquidity)}개 / 전체 ${fmt(windows)}개 · A 돌파 확인 ${fmt(diag.aBreakout)}개 / 지지 유지 ${fmt(diag.aSupport)}개 · B 선행 상승폭 2.5% 통과 ${fmt(diag.bSwing25)}개 / B 상세 조건까지 도달 ${fmt(diag.bEvaluated)}개 · 상세 진단: 추세 ${fmt(diag.bTrend)}개 / 피보나치 접촉 ${fmt(diag.bTouch)}개 / 반등 ${fmt(diag.bBounce)}개 / 거래량 ${fmt(diag.bVolume)}개 / 4조건 동시 충족 ${fmt(diag.bAllPrice)}개. (각 조건은 독립 집계입니다.)`;
  $('coinRows').innerHTML = all.map(x => `<tr><td>${x.market}</td><td>${fmt(x.windows||0)}</td><td>${x.error?'—':fmt(x.strategies.A.signals)}</td><td>${x.error?'—':fmt(x.strategies.B.signals)}</td><td>${x.error?'오류: '+String(x.error).slice(0,65):(x.windows?'정상 · 원본 '+fmt(x.history_rows)+' / 보정 '+fmt(x.gap_filled): (x.coverage_reason||'검증 구간 없음'))}</td></tr>`).join('');
  const ev=[];
  for(const x of complete) for(const strategy of ['A','B']) for(const e of x.strategies[strategy].examples){
    ev.push(`${strategy} · ${e.market} · ${e.time} · 1h ${pct(e.net['1h'])} / 24h ${pct(e.net['24h'])}`);
  }
  $('events').textContent=ev.length?ev.slice(0,30).join('\n'):'아직 BUY 신호 예시가 없습니다.';
  $('events').style.whiteSpace='pre-line';
  // New fields are evaluation diagnostics; they do not change A/B signals.
  const touchNames={tp_first:'익절 먼저',sl_first:'손절 먼저',same_bar:'동일봉 불명',gap_unknown:'공백 불명',none:'6h 둘 다 미도달'};
  const sum={};
  for(const strategy of ['A','B']) {
    const t={tp_first:0,sl_first:0,same_bar:0,gap_unknown:0,none:0};
    for(const x of complete) {
      const source=x.strategies[strategy].touch_counts||{};
      for(const k of Object.keys(t)) t[k] += source[k]||0;
    }
    sum[strategy]=`${strategy}: `+Object.keys(t).map(k=>`${touchNames[k]} ${fmt(t[k])}`).join(' · ');
  }
  $('touchSummary').textContent=sum.A+'\n'+sum.B;
  const scenarioDefs=[
    {id:'p2_s1',label:'+2% / -1%'},
    {id:'p2_s15',label:'+2% / -1.5%'},
    {id:'p3_s15',label:'+3% / -1.5%'},
    {id:'p3_s2',label:'+3% / -2%'}
  ];
  const scenarioResults=[];
  for(const strategy of ['A','B']) for(const scenario of scenarioDefs){
    const counts={tp_first:0,sl_first:0,same_bar:0,gap_unknown:0,time_exit:0};
    let resolved=0, netSum=0, wins=0;
    for(const x of complete){
      const rec=(x.strategies[strategy].scenarios||{})[scenario.id];
      if(!rec)continue;
      for(const status in counts)counts[status]+=rec.counts[status]||0;
      resolved+=rec.resolved||0;
      netSum+=rec.net_sum||0;
      wins+=rec.positive||0;
    }
    scenarioResults.push(`<tr><td>${strategy}</td><td>${scenario.label}</td><td>${fmt(counts.tp_first)}</td><td>${fmt(counts.sl_first)}</td><td>${fmt(counts.same_bar)}</td><td>${fmt(counts.gap_unknown)}</td><td>${fmt(counts.time_exit)}</td><td>${fmt(resolved)}</td><td>${pct(resolved?netSum/resolved:null)}</td><td>${pct(resolved?100*wins/resolved:null)}</td></tr>`);
  }
  $('scenarioRows').innerHTML=scenarioResults.join('');
  $('scenarioNotice').textContent='판정가능 = 익절/손절 선도달 또는 6시간 종가 청산. 불명 사례는 평균·수익 비율에서 제외하며, 한 날짜·중복 종목의 결과만으로 전략 우열을 확정하지 않습니다.';
  const v6Defs=[
    ['all','B 전체 BUY (기준)'],
    ['raw80','신호 전 80봉 원본'],
    ['momentum','15분 ≥0% · 30분 ≥+0.5%'],
    ['momentum_volume','방향 + 거래량 ≥1.5배'],
    ['raw_momentum_volume','원본80 + 방향 + 거래량']
  ];
  const v6Out=[];
  for(const [gate,label] of v6Defs){
    const t={signals:0,resolvable:0,net_sum:0,positive:0,one_hour_sum:0,three_hour_sum:0,future_6h_raw:0,
      statuses:{tp_first:0,sl_first:0,same_bar:0,gap_unknown:0,time_exit:0}};
    for(const x of complete){
      const v=(x.v6||{})[gate];if(!v)continue;
      for(const k of ['signals','resolvable','net_sum','positive','one_hour_sum','three_hour_sum','future_6h_raw'])t[k]+=v[k]||0;
      for(const k of Object.keys(t.statuses))t.statuses[k]+=(v.statuses||{})[k]||0;
    }
    const unknown=t.statuses.same_bar+t.statuses.gap_unknown;
    v6Out.push(`<tr><td>${label}</td><td>${fmt(t.signals)}</td><td>${fmt(t.statuses.tp_first)}</td><td>${fmt(t.statuses.sl_first)}</td><td>${fmt(unknown)}</td><td>${fmt(t.resolvable)}</td><td>${pct(t.resolvable?t.net_sum/t.resolvable:null)}</td><td>${pct(t.resolvable?100*t.positive/t.resolvable:null)}</td><td>${pct(t.signals?t.one_hour_sum/t.signals:null)}</td><td>${pct(t.signals?t.three_hour_sum/t.signals:null)}</td><td>${fmt(t.future_6h_raw)}</td></tr>`);
  }
  $('v6Rows').innerHTML=v6Out.join('');
  $('v6Notice').textContent='이 조건은 검증 목적의 사전 정보 분류입니다. 상승·거래량 기준은 임시 가설이며 최적화된 BUY 필터가 아닙니다. 적은 표본과 같은 종목 반복 신호, 현재 시점 코인 선정 편향, 원본 누락 문제 때문에 실전 우열은 확정할 수 없습니다.';


  // V7: B BUY is unchanged. This panel tests non-chasing delayed entries.
  const v7Defs=[['dip05','-0.5% 눌림 + 반등'],['dip10','-1.0% 눌림 + 반등']];
  const v7Out=[];
  const baseline={signals:0,resolvable:0,net_sum:0,positive:0,
    outcomes:{tp_first:0,sl_first:0,same_bar:0,gap_unknown:0,time_exit:0}};
  for(const x of complete){
    const rec=x.strategies.B.scenarios.p3_s2;
    baseline.signals+=x.strategies.B.signals;
    baseline.resolvable+=rec.resolved||0;
    baseline.net_sum+=rec.net_sum||0;
    baseline.positive+=rec.positive||0;
    for(const k of Object.keys(baseline.outcomes))baseline.outcomes[k]+=rec.counts[k]||0;
  }
  v7Out.push(`<tr><td>즉시 진입 · V5 기준</td><td>${fmt(baseline.signals)}</td><td>${fmt(baseline.signals)}</td><td>0</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>${fmt(baseline.outcomes.tp_first)}</td><td>${fmt(baseline.outcomes.sl_first)}</td><td>${fmt(baseline.outcomes.same_bar+baseline.outcomes.gap_unknown)}</td><td>${fmt(baseline.outcomes.time_exit)}</td><td>${fmt(baseline.resolvable)}</td><td>${pct(baseline.resolvable?baseline.net_sum/baseline.resolvable:null)}</td><td>${pct(baseline.resolvable?100*baseline.positive/baseline.resolvable:null)}</td><td>0분</td><td>—</td><td>—</td></tr>`);
  for(const [key,label] of v7Defs){
    const t={signals:0,entered:0,resolvable:0,net_sum:0,positive:0,wait_minutes_sum:0,
      same_signal_immediate_n:0,same_signal_immediate_sum:0,skipped_immediate_tp:0,
      not_entered:{no_pullback:0,no_rebound:0,breakdown:0,missed_price:0,gap_unknown:0},
      outcomes:{tp_first:0,sl_first:0,same_bar:0,gap_unknown:0,time_exit:0}};
    for(const x of complete){
      const v=(x.v7||{})[key]; if(!v)continue;
      for(const k of ['signals','entered','resolvable','net_sum','positive','wait_minutes_sum',
        'same_signal_immediate_n','same_signal_immediate_sum','skipped_immediate_tp'])t[k]+=v[k]||0;
      for(const k of Object.keys(t.not_entered))t.not_entered[k]+=(v.not_entered||{})[k]||0;
      for(const k of Object.keys(t.outcomes))t.outcomes[k]+=(v.outcomes||{})[k]||0;
    }
    const noEntry=t.signals-t.entered;
    const unknown=t.outcomes.same_bar+t.outcomes.gap_unknown;
    v7Out.push(`<tr><td>${label}</td><td>${fmt(t.signals)}</td><td>${fmt(t.entered)}</td><td>${fmt(noEntry)}</td><td>${fmt(t.not_entered.no_pullback)}</td><td>${fmt(t.not_entered.no_rebound)}</td><td>${fmt(t.not_entered.breakdown)}</td><td>${fmt(t.not_entered.missed_price)}</td><td>${fmt(t.not_entered.gap_unknown)}</td><td>${fmt(t.outcomes.tp_first)}</td><td>${fmt(t.outcomes.sl_first)}</td><td>${fmt(unknown)}</td><td>${fmt(t.outcomes.time_exit)}</td><td>${fmt(t.resolvable)}</td><td>${pct(t.resolvable?t.net_sum/t.resolvable:null)}</td><td>${pct(t.resolvable?100*t.positive/t.resolvable:null)}</td><td>${t.entered?(t.wait_minutes_sum/t.entered).toFixed(1)+'분':'—'}</td><td>${pct(t.same_signal_immediate_n?t.same_signal_immediate_sum/t.same_signal_immediate_n:null)}</td><td>${fmt(t.skipped_immediate_tp)}</td></tr>`);
  }
  $('v7Rows').innerHTML=v7Out.join('');
  $('v7Notice').textContent='미진입은 수익 0% 매매로 취급하지 않고 분모에서도 제외합니다. 즉시 진입 기준과 눌림 진입은 실제로 체결된 표본이 다르므로, 같은 신호 즉시 진입 평균도 별도로 제시합니다. 손익비는 매수가 대비 가격 +3%/-2%, 수수료 반영, 실체결·슬리피지 미반영. 완성된 봉의 정보만 쓰며 미래 최고·최저가는 진입 판단에 사용하지 않습니다.';

  // V9 uses the same already-determined V7 -1% entries; no re-timing.
  // Vertical result cards avoid horizontal table scrolling on a phone.
  const v9Root=$('v9Cards');v9Root.replaceChildren();
  const bV7={signals:0,entered:0,resolvable:0,net_sum:0,positive:0,
    outcomes:{tp_first:0,sl_first:0,same_bar:0,gap_unknown:0,time_exit:0}};
  for(const x of complete){
    const t=(x.v7||{}).dip10;if(!t)continue;
    for(const k of ['signals','entered','resolvable','net_sum','positive'])bV7[k]+=t[k]||0;
    for(const k of Object.keys(bV7.outcomes))bV7.outcomes[k]+=(t.outcomes||{})[k]||0;
  }
  const v9Defs=[['v7','기존 -1.0% 눌림 (V7)'],['support','V9 ① 눌림 저점 지지'],['strong','V9 ② 지지 + 강한 반등·거래량']];
  for(const [key,label] of v9Defs){
    const t=key==='v7'?bV7:{signals:0,v7_entered:0,entered:0,resolvable:0,
      net_sum:0,positive:0,wait_minutes_sum:0,rejected_v7_tp:0,rejected_v7_sl:0,
      rejected:{v7_skip:0,support_broken:0,weak_rebound:0,weak_volume:0,volume_unknown:0,data_unknown:0},
      outcomes:{tp_first:0,sl_first:0,same_bar:0,gap_unknown:0,time_exit:0}};
    if(key!=='v7')for(const x of complete){
      const r=(x.v9||{})[key];if(!r)continue;
      for(const f of ['signals','v7_entered','entered','resolvable','net_sum','positive','wait_minutes_sum','rejected_v7_tp','rejected_v7_sl'])t[f]+=r[f]||0;
      for(const f in t.rejected)t.rejected[f]+=(r.rejected||{})[f]||0;
      for(const f in t.outcomes)t.outcomes[f]+=(r.outcomes||{})[f]||0;
    }
    const u=t.outcomes.same_bar+t.outcomes.gap_unknown;
    const rows=[
      `B BUY ${fmt(t.signals)}개 · 가상 진입 ${fmt(t.entered)}개 · 미진입 ${fmt(t.signals-t.entered)}개`,
      `익절 먼저 ${fmt(t.outcomes.tp_first)}개 · 손절 먼저 ${fmt(t.outcomes.sl_first)}개 · 결과 불명 ${fmt(u)}개 · 6시간 청산 ${fmt(t.outcomes.time_exit)}개`,
      `판정 가능 ${fmt(t.resolvable)}개 · 평균 순수익 ${pct(t.resolvable?t.net_sum/t.resolvable:null)} · 수익 비율 ${pct(t.resolvable?100*t.positive/t.resolvable:null)}`,
    ];
    if(key!=='v7'){
      rows.push(`V7 단계 미진입 ${fmt(t.rejected.v7_skip)}개 · 지지 이탈 ${fmt(t.rejected.support_broken)}개 · 반등 약함 ${fmt(t.rejected.weak_rebound)}개`);
      if(key==='strong')rows.push(`거래량 부족 ${fmt(t.rejected.weak_volume)}개 · 거래량 판정 불가 ${fmt(t.rejected.volume_unknown)}개`);
      rows.push(`추가 필터가 제외한 V7 익절 ${fmt(t.rejected_v7_tp)}개 · 손절 ${fmt(t.rejected_v7_sl)}개`);
    }
    const card=document.createElement('div');card.className='eventcard';
    const heading=document.createElement('strong');heading.textContent=label;card.appendChild(heading);
    for(const r of rows){const item=document.createElement('div');item.textContent=r;card.appendChild(item);}
    v9Root.appendChild(card);
  }


  const details=[];
  for(const x of complete) for(const strategy of ['A','B']) {
    for(const e of x.strategies[strategy].examples) details.push({strategy,e});
  }
  details.sort((a,b)=>a.e.time.localeCompare(b.e.time));
  const root=$('qualityCards');root.replaceChildren();
  if(!details.length)root.textContent='아직 분석할 BUY 신호가 없습니다.';
  for(const {strategy,e} of details.slice(0,40)) {
    const q=e.quality;if(!q)continue;
    const card=document.createElement('div');card.className='eventcard';
    function line(value, title){const v=document.createElement('div');v.textContent=value;if(title){const b=document.createElement('strong');b.textContent=title+' ';v.prepend(b);}card.appendChild(v);}
    line(`${strategy} · ${e.market} · ${e.time}`, '신호');
    const bp=q.before_pct;
    line(`15분 ${pct(bp['15m'])} · 30분 ${pct(bp['30m'])} · 60분 ${pct(bp['60m'])} · 거래량 ${q.volume_ratio_20==null?'—':q.volume_ratio_20+'배'}`, '진입 전');
    line(`1시간 ${pct(e.net['1h'])} · 3시간 ${pct(e.net['3h'])} · 24시간 ${pct(e.net['24h'])}`, '종가 평가(보정 가능)');
    const moves=q.excursions['6h'];
    for(const key of ['1h','3h','6h']){
      const m=q.excursions[key];
      line(`최대 상승 ${pct(m.max_up_pct)} · 최대 하락 ${pct(m.max_down_pct)} (실제 봉의 고가·저가, 수수료 전)`, key+' 움직임');
    }
    line(`${touchNames[q.first_touch]}${q.first_touch_minutes==null?'':' · 최초 터치 '+q.first_touch_minutes+'분'} (목표 순수익 +2% / 손절 -1%)`, '익절·손절');
    const scenarioNames={tp_first:'익절 먼저',sl_first:'손절 먼저',same_bar:'동일봉 불명',gap_unknown:'공백 불명',time_exit:'6h 종가청산'};
    for(const d of scenarioDefs){
      const res=e.scenarios&&e.scenarios[d.id];
      if(res)line(`${d.label} → ${scenarioNames[res.status]||res.status}${res.minutes==null?'':' · '+res.minutes+'분'} · 순수익 ${pct(res.net_pct)}`, 'V5 가격기준');
    }
    if(e.v6_gates)line('원본80 '+(e.v6_gates.raw80?'통과':'제외')+' · 15/30분 방향 '+(e.v6_gates.momentum?'통과':'제외')+' · 방향+거래량 '+(e.v6_gates.momentum_volume?'통과':'제외'), 'V6 신호시점 분류');
    if(strategy==='B' && e.v7){
      const v7Names={entered:'눌림 진입',no_pullback:'눌림 없음 · 추격 포기',no_rebound:'반등 확인 안 됨',breakdown:'3% 급락 · 취소',missed_price:'원래 신호가 위 · 추격 포기',gap_unknown:'진입 전 캔들 공백 · 불명'};
      for(const [key,label] of [['dip05','-0.5% 눌림'],['dip10','-1.0% 눌림']]){
        const d=e.v7[key];if(!d)continue;
        line(`${label}: ${v7Names[d.status]||d.status}${d.minutes==null?'':' · '+d.minutes+'분 대기'}${d.entry_vs_signal_pct==null?'':' · 신호 종가 대비 진입가 '+pct(d.entry_vs_signal_pct)}${d.scenario?' · 이후 '+(scenarioNames[d.scenario.status]||d.scenario.status)+' / 순수익 '+pct(d.scenario.net_pct):''}`, 'V7 비추격 눌림');
      }
    }

    if(strategy==='B' && e.v9){
      const f=e.v9, reason={entered:'통과',v7_skip:'V7 미진입',support_broken:'눌림 저점 이탈',weak_rebound:'반등 몸통 부족',weak_volume:'거래량 부족',volume_unknown:'원본 거래량 부족',data_unknown:'누락봉으로 판정 불가'};
      line(`저점 지지 ${f.support?'통과':'제외'} · 강한 반등 ${f.strong?'통과':'제외'} · ${reason[f.reason]||f.reason} · 확인봉 몸통 ${pct(f.body_pct)} · 거래량 ${f.volume_ratio==null?'—':f.volume_ratio+'배'}`, 'V9 신호시점 실험');
    }
    if(q.gap_before_80 || moves.gap_bars)line(`과거 80봉 보정 ${q.gap_before_80}개 / 이후 6시간 보정 ${moves.gap_bars}개 · 해석 주의`, '데이터 품질');
    root.appendChild(card);
  }
  if(details.length>40){const note=document.createElement('small');note.textContent=`신호 ${fmt(details.length)}건 중 첫 40건만 표시`;root.appendChild(note);}
  $('progress').max=total||1;$('progress').value=done;
}
// Browser IndexedDB stores RAW candles (not synthetic gaps, not results).
// A user can save a JSON backup to preserve the same dataset across devices.
const SNAP_SCHEMA='bithumb-v8-raw1';
const DB_NAME='bithumb-daytrade-v8-snapshots';
const DB_STORE='rawRuns';
const SNAP_DAYS=['2026-11-01','2026-11-04','2026-11-07','2026-11-08'];
const snapshotKey=()=>`${$('day').value}|${$('limit').value}`;
const cohortKey=limit=>`cohort|${limit}`;
const makeCohort=snap=>({key:cohortKey(snap.limit),schema:SNAP_SCHEMA,
    limit:snap.limit,available_markets:snap.available_markets,
    created:snap.created,markets:snap.markets.slice()});
function openSnapshotDB(){
  return new Promise((resolve,reject)=>{
    if(!('indexedDB' in window))return reject(new Error('브라우저에 IndexedDB가 없습니다'));
    const req=indexedDB.open(DB_NAME,1);
    req.onupgradeneeded=()=>{if(!req.result.objectStoreNames.contains(DB_STORE))req.result.createObjectStore(DB_STORE,{keyPath:'key'});};
    req.onsuccess=()=>resolve(req.result);
    req.onerror=()=>reject(req.error||new Error('브라우저 원본 데이터 저장소 열기 실패'));
  });
}
async function snapshotStore(mode, key, value){
  const db=await openSnapshotDB();
  return new Promise((resolve,reject)=>{
    const tx=db.transaction(DB_STORE, mode==='get'?'readonly':'readwrite');
    const store=tx.objectStore(DB_STORE);
    let req;
    if(mode==='get')req=store.get(key);
    else if(mode==='put')req=store.put(value);
    else req=store.delete(key);
    let result;
    req.onsuccess=()=>{result=req.result;};
    tx.oncomplete=()=>{db.close();resolve(result);};
    tx.onerror=()=>{db.close();reject(tx.error||new Error('스냅샷 읽기·저장 실패'));};
    tx.onabort=()=>{db.close();reject(tx.error||new Error('스냅샷 작업 중단'));};
  });
}
// V10 full-KRW dataset: each original 5-market batch is a separate record.
// This avoids repeatedly writing one 100MB+ snapshot on a mobile device.
const ALL_KIND='all';
const ALL_MARKET_CAP=1000;
const allChunkKey=(day,i)=>`allchunk|${day}|${i}`;
const allMetaKey=day=>`allmeta|${day}`;
function validateAllCohort(c){
  if(!c||c.schema!==SNAP_SCHEMA||c.limit!==ALL_KIND||!Array.isArray(c.markets)||
     c.markets.length<1||c.markets.length>ALL_MARKET_CAP||
     new Set(c.markets).size!==c.markets.length||
     c.markets.some(m=>typeof m!=='string'||!/^KRW-[A-Z0-9]+$/.test(m)))
     throw new Error('전체 KRW 코인 목록이 올바르지 않습니다');
}
function validateAllChunk(chunk,day,batch){
  if(!chunk||chunk.schema!==SNAP_SCHEMA||chunk.date!==day||
     !Array.isArray(chunk.markets)||chunk.markets.join(',')!==batch.join(',')||
     !chunk.rawByMarket||typeof chunk.rawByMarket!=='object')
     throw new Error('고정 원본 묶음이 올바르지 않습니다');
  for(const m of batch){
    const rows=chunk.rawByMarket[m];
    if(!Array.isArray(rows)||rows.length>800||rows.some(r=>
       !r||typeof r!=='object'||r._gap_fill||typeof r.candle_date_time_kst!=='string'))
       throw new Error(`${m} 원본 봉이 올바르지 않습니다`);
  }
}
async function getAllCohort(){
  let cohort=await snapshotStore('get',cohortKey(ALL_KIND));
  if(cohort){validateAllCohort(cohort);return cohort;}
  $('status').textContent='현재 상장된 전체 KRW 코인 목록을 처음 한 번 고정하는 중…';
  const resp=await fetch('/api/daytrade-universe?limit=all',{cache:'no-store'});
  const data=await resp.json();
  if(!resp.ok||!data.ok)throw new Error(data.error||'전체 코인 목록 조회 실패');
  cohort={schema:SNAP_SCHEMA,key:cohortKey(ALL_KIND),limit:ALL_KIND,
          markets:data.markets,available_markets:data.available_markets,
          created:new Date().toLocaleString('ko-KR')};
  validateAllCohort(cohort);
  await snapshotStore('put',cohort.key,cohort);  // freeze before the first batch
  return cohort;
}
async function runAllBacktest(){
  if(running)return;
  running=true;$('start').disabled=true;
  for(const name of ['snapshotExport','snapshotImport','snapshotReset','cohortReset'])$(name).disabled=true;
  $('coinRows').innerHTML='';
  const date=$('day').value,fee=$('fee').value;
  const all=[];
  let total=0,saved=0,failures=0;
  try{
    const cohort=await getAllCohort();
    const markets=cohort.markets;
    total=markets.length;
    $('progress').max=total;$('progress').value=0;
    for(let i=0;i<total;i+=5){
      const batch=markets.slice(i,i+5),key=allChunkKey(date,i);
      let chunk=await snapshotStore('get',key);
      let resp;
      const qs=new URLSearchParams({date,markets:batch.join(','),fee,capture:'1'});
      if(chunk){
        validateAllChunk(chunk,date,batch);
        resp=await fetch('/api/daytrade-replay',{
          method:'POST',headers:{'Content-Type':'application/json'},cache:'no-store',
          body:JSON.stringify({schema:SNAP_SCHEMA,date,fee,
              snapshots:batch.map(market=>({market,rows:chunk.rawByMarket[market]}))})
        });
      }else{
        resp=await fetch('/api/daytrade-batch?'+qs.toString(),{cache:'no-store'});
      }
      if(!resp.ok)throw new Error(`코인 ${i+1}~${Math.min(i+5,total)} 응답 오류(${resp.status}) · 다시 시작하면 이어서 진행`);
      const r=await resp.json();
      if(!r.ok||!Array.isArray(r.results)||r.results.length!==batch.length)
        throw new Error(r.error||'분석 응답이 불완전합니다 · 다시 시작하면 이어서 진행');
      if(!chunk){
        const rawByMarket={};
        for(const item of r.results){
          if(Object.prototype.hasOwnProperty.call(item,'_raw_candles')){
            rawByMarket[item.market]=item._raw_candles;
            delete item._raw_candles;
          }
        }
        if(batch.every(m=>Array.isArray(rawByMarket[m]))){
          chunk={schema:SNAP_SCHEMA,key,date,markets:batch.slice(),rawByMarket};
          validateAllChunk(chunk,date,batch);
          await snapshotStore('put',key,chunk);
        }else{
          failures+=batch.length;
        }
      }
      if(chunk)saved+=batch.length;
      all.push(...r.results);
      $('progress').max=total;$('progress').value=i+batch.length;
      $('status').textContent=`전체 KRW ${i+batch.length}/${total}개 검사 · 원본 저장 ${saved}개 · ${chunk?'고정 데이터 재검사/저장':'일부 저장 실패'} · 중단 시 다시 실행하면 이어집니다`;
      // Rendering the entire table after every 5 coins becomes expensive on a phone.
      if((i+batch.length)%25===0||i+batch.length===total)
        paint(all,i+batch.length,total,{available_markets:cohort.available_markets});
      await snapshotStore('put',allMetaKey(date),{
        schema:SNAP_SCHEMA,key:allMetaKey(date),date,total,saved,
        created:cohort.created,updated:new Date().toLocaleString('ko-KR')});
    }
    paint(all,total,total,{available_markets:cohort.available_markets});
    const errors=all.filter(x=>x.error).length;
    const valid=all.filter(x=>!x.error&&x.windows>0).length;
    const completed=(saved===total&&errors===0&&failures===0);
    $('status').textContent=(completed?'✅ 완료':'⚠️ 검사 종료')+` · 전체 ${total}개 · 유효 ${valid}개 · 오류 ${errors}개 · 원본 고정 ${saved}/${total}개`;
    if(completed){
      const avgB=h=>{const n=all.reduce((t,x)=>t+x.strategies.B.samples[h],0);
        const sum=all.reduce((t,x)=>t+x.strategies.B.return_sums[h],0);return n?sum/n:null;};
      dateHistory.set(date,{date,scope:`전체 KRW ${total}개 / 편도 ${fee}% · ${cohort.created} 선정`,valid,
        a:all.reduce((t,x)=>t+x.strategies.A.signals,0),
        b:all.reduce((t,x)=>t+x.strategies.B.signals,0),
        b1:avgB('1h'),b3:avgB('3h'),b24:avgB('24h')});
      $('dateRows').innerHTML=Array.from(dateHistory.values()).sort((a,b)=>a.date.localeCompare(b.date)).map(x=>
        `<tr><td>${x.date}</td><td>${x.scope}</td><td>${fmt(x.valid)}</td><td>${fmt(x.a)}</td><td>${fmt(x.b)}</td><td>${pct(x.b1)}</td><td>${pct(x.b3)}</td><td>${pct(x.b24)}</td></tr>`).join('');
    }
  }catch(e){
    paint(all,all.length,total||1,{});
    $('status').textContent=`⚠️ ${e.message} · 저장 완료된 코인은 보존됩니다. 같은 날짜로 분석 시작을 다시 누르면 이어집니다.`;
  }finally{
    running=false;$('start').disabled=false;
    for(const name of ['snapshotExport','snapshotImport','snapshotReset','cohortReset'])$(name).disabled=false;
    await refreshSnapshotInfo();
  }
}
// Streaming gzip backup: do not build a giant uncompressed JSON string in RAM.
async function exportAllRaw(){
  const date=$('day').value,cohort=await snapshotStore('get',cohortKey(ALL_KIND));
  validateAllCohort(cohort);
  const enc=new TextEncoder();
  if(typeof CompressionStream==='undefined')throw new Error('이 브라우저는 압축 백업을 지원하지 않습니다');
  const stream=new ReadableStream({
    async start(ctrl){
      try{
        ctrl.enqueue(enc.encode(JSON.stringify({schema:SNAP_SCHEMA,mode:'all',date,
          cohort,created:new Date().toISOString()}).slice(0,-1)+',"chunks":['));
        for(let i=0;i<cohort.markets.length;i+=5){
          const chunk=await snapshotStore('get',allChunkKey(date,i));
          if(!chunk)throw new Error('아직 전체 검사가 완료되지 않아 백업할 수 없습니다');
          validateAllChunk(chunk,date,cohort.markets.slice(i,i+5));
          ctrl.enqueue(enc.encode((i?',':'')+JSON.stringify(chunk)));
        }
        ctrl.enqueue(enc.encode(']}'));ctrl.close();
      }catch(e){ctrl.error(e);}
    }
  });
  const blob=await new Response(stream.pipeThrough(new CompressionStream('gzip'))).blob();
  const url=URL.createObjectURL(blob),a=document.createElement('a');
  a.href=url;a.download=`daytrade_all_krw_${date}_v10.json.gz`;
  document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);
  $('status').textContent=`✅ 전체 KRW ${date} 원본 압축 백업 다운로드 요청 완료`;
}
async function importAllRaw(snap){
  if(snap.schema!==SNAP_SCHEMA||snap.mode!=='all'||!SNAP_DAYS.includes(snap.date)||
     !Array.isArray(snap.chunks))throw new Error('전체 KRW 백업 형식이 올바르지 않습니다');
  const cohort=snap.cohort;
  validateAllCohort(cohort);
  if(snap.chunks.length!==Math.ceil(cohort.markets.length/5))throw new Error('백업 파일의 코인 묶음이 부족합니다');
  const oldCohort=await snapshotStore('get',cohortKey(ALL_KIND));
  if(oldCohort&&oldCohort.markets.join(',')!==cohort.markets.join(','))
    throw new Error('이미 고정된 전체 KRW 목록과 다릅니다. 기존 전체 목록을 삭제하지 않고 덮어쓸 수 없습니다');
  const existing=await snapshotStore('get',allChunkKey(snap.date,0));
  if(existing&&!confirm(`${snap.date} 전체 KRW 고정 원본을 백업 파일 내용으로 덮어쓸까요?`))return;
  // Validate every chunk before writing any of them.
  for(let i=0;i<snap.chunks.length;i++){
    const chunk=snap.chunks[i];
    validateAllChunk(chunk,snap.date,cohort.markets.slice(i*5,i*5+5));
  }
  await snapshotStore('put',cohortKey(ALL_KIND),{...cohort,key:cohortKey(ALL_KIND)});
  for(let i=0;i<snap.chunks.length;i++){
    const chunk=snap.chunks[i];
    chunk.key=allChunkKey(snap.date,i*5);
    await snapshotStore('put',chunk.key,chunk);
  }
  await snapshotStore('put',allMetaKey(snap.date),{
    schema:SNAP_SCHEMA,key:allMetaKey(snap.date),date:snap.date,
    total:cohort.markets.length,saved:cohort.markets.length,
    updated:new Date().toLocaleString('ko-KR')});
  $('limit').value='all';$('day').value=snap.date;
  $('status').textContent='✅ 전체 KRW 원본 압축 백업 불러오기 완료';
  await refreshSnapshotInfo();
}
function validateSnapshot(snap,day,limit){
  if(!snap||snap.schema!==SNAP_SCHEMA||snap.date!==day||snap.limit!==limit||
     !Array.isArray(snap.markets)||snap.markets.length!==limit||
     !snap.rawByMarket||typeof snap.rawByMarket!=='object')throw new Error('저장된 데이터 형식이 맞지 않습니다');
  if(!SNAP_DAYS.includes(day)||![10,30,50].includes(limit))throw new Error('검사 설정 오류');
  const unique=new Set();
  for(const market of snap.markets){
    if(typeof market!=='string'||!/^KRW-[A-Z0-9]+$/.test(market)||unique.has(market))throw new Error('코인 목록 형식이 맞지 않습니다');
    unique.add(market);
    const rows=snap.rawByMarket[market];
    if(!Array.isArray(rows)||rows.length>800)throw new Error('원본 봉 개수 오류: '+market);
    for(const row of rows){
      if(!row||typeof row!=='object'||row._gap_fill||typeof row.candle_date_time_kst!=='string'||
         !['opening_price','high_price','low_price','trade_price','candle_acc_trade_price','candle_acc_trade_volume']
           .every(k=>Number.isFinite(Number(row[k]))))throw new Error('원본 봉 데이터 오류: '+market);
    }
  }
}
async function refreshSnapshotInfo(){
  const el=$('snapshotInfo');
  try{
    if($('limit').value==='all'){
      const cohort=await snapshotStore('get',cohortKey('all'));
      const meta=await snapshotStore('get',allMetaKey($('day').value));
      el.textContent=cohort
        ?`📌 전체 KRW ${cohort.markets.length}개 고정 · ${$('day').value} 원본 ${meta?meta.saved:0}/${cohort.markets.length}개 저장 · 중단돼도 분석 시작으로 이어서 진행`
        :'🔓 전체 KRW 목록 아직 미선정 · 첫 검사 시 목록부터 고정합니다';
      return;
    }
    const snap=await snapshotStore('get',snapshotKey());
    const cohort=await snapshotStore('get',cohortKey(Number($('limit').value)));
    el.textContent=snap
      ?`✅ 원본 고정 · ${snap.date} / ${snap.limit}개 · 원본 5분봉 ${fmt(snap.markets.reduce((n,m)=>n+snap.rawByMarket[m].length,0))}개 · 저장 ${snap.created} · 다시 검사해도 같은 원본`
      :cohort
        ?`📌 코인 목록 ${cohort.limit}개 고정됨 · ${$('day').value} 날짜 원본은 미고정 (처음 한 번 수집 후 고정)`
        :'🔓 아직 코인 목록·날짜 원본 미고정 · 첫 검사 시 두 가지를 고정합니다.';
  }catch(e){el.textContent='⚠️ 브라우저 데이터 저장 불가: '+e.message;}
}
async function startBacktest(){
  if($('limit').value==='all')return runAllBacktest();
  if(running)return;
  running=true;$('start').disabled=true;
  for(const name of ['snapshotExport','snapshotImport','snapshotReset','cohortReset'])$(name).disabled=true;
  $('coinRows').innerHTML='';
  const all=[];
  const date=$('day').value,limit=Number($('limit').value),fee=$('fee').value;
  let snap=null;
  let markets=[];
  let rankInfo={};
  try{
    const cohort=await snapshotStore('get',cohortKey(limit));
    snap=await snapshotStore('get',snapshotKey());
    if(snap){
      validateSnapshot(snap,date,limit);
      markets=snap.markets;
      rankInfo={available_markets:snap.available_markets};
      $('status').textContent=`✅ 이미 고정된 ${date} 원본 캔들로 재계산 · 빗썸 과거 봉 재요청 없음`;
    }else{
      let available;
      if(cohort){
        markets=cohort.markets;
        available=cohort.available_markets;
        if(markets.length!==limit)throw new Error('기존 코인 고정 목록이 잘못되었습니다');
        $('status').textContent=`📌 공통 고정된 ${limit}개 코인 목록으로 ${date} 원본 5분봉 첫 수집 중…`;
      }else{
        $('status').textContent='코인 목록 첫 고정: 현재 KRW 거래대금 순위 조회 중…';
        const uResp=await fetch('/api/daytrade-universe?limit='+limit,{cache:'no-store'});
        const u=await uResp.json();
        if(!uResp.ok||!u.ok)throw new Error(u.error||'코인 목록 조회 실패');
        markets=u.markets;
        if(markets.length!==limit)throw new Error('요청한 코인 수를 충족하지 않아 고정하지 않았습니다');
        available=u.available_markets;
      }
      rankInfo={available_markets:available};
      snap={schema:SNAP_SCHEMA,key:snapshotKey(),date,limit,available_markets:available,
            created:new Date().toLocaleString('ko-KR'),source:'exchange',markets:markets.slice(),rawByMarket:{}};
    }
    if(cohort && snap.markets.join(',')!==cohort.markets.join(',')){
      throw new Error('고정 코인 목록이 날짜별로 다릅니다. 해당 코인 수의 기존 고정 목록을 확인하세요.');
    }
    for(let i=0;i<markets.length;i+=5){
      const batch=markets.slice(i,i+5);
      $('status').textContent=`${snap&&Object.keys(snap.rawByMarket).length===markets.length?'고정 재검사':'원본 최초 고정'} · ${i}/${markets.length}개 완료 (${batch.join(', ')})`;
      let resp;
      if(snap.source==='exchange' && Object.keys(snap.rawByMarket).length!==markets.length){
        const qs=new URLSearchParams({date,markets:batch.join(','),fee,capture:'1'});
        resp=await fetch('/api/daytrade-batch?'+qs.toString(),{cache:'no-store'});
      }else{
        resp=await fetch('/api/daytrade-replay',{
          method:'POST',headers:{'Content-Type':'application/json'},cache:'no-store',
          body:JSON.stringify({schema:SNAP_SCHEMA,date,fee,
            snapshots:batch.map(market=>({market,rows:snap.rawByMarket[market]}))})
        });
      }
      const r=await resp.json();
      if(!resp.ok||!r.ok)throw new Error(r.error||'원본 데이터 분석 요청 실패');
      for(const item of r.results){
        if(Object.prototype.hasOwnProperty.call(item,'_raw_candles')){
          snap.rawByMarket[item.market]=item._raw_candles;
          delete item._raw_candles;
        }
      }
      all.push(...r.results);
      paint(all,all.length,markets.length,rankInfo);
    }
    const errors=all.filter(x=>x.error).length;
    const valid=all.filter(x=>!x.error&&x.windows>0).length;
    if(Object.keys(snap.rawByMarket).length===markets.length && !errors){
      validateSnapshot(snap,date,limit);
      await snapshotStore('put',snap.key,snap);
      if(!cohort)await snapshotStore('put',cohortKey(limit),makeCohort(snap));
      $('status').textContent=`✅ 완료 · ${all.length}개 조회, 유효 ${valid}개 · 같은 ${limit}개 코인과 원본 데이터 고정`;
    }else{
      $('status').textContent=`⚠️ 완료 · ${all.length}개 조회, 유효 ${valid}개, 오류 ${errors}개 · 데이터가 완전하지 않아 고정하지 않았습니다.`;
    }
    if(!errors){
      const totalSignals=kind=>all.reduce((t,x)=>t+x.strategies[kind].signals,0);
      const avgB=h=>{
        const n=all.reduce((t,x)=>t+x.strategies.B.samples[h],0);
        const sm=all.reduce((t,x)=>t+x.strategies.B.return_sums[h],0);
        return n?sm/n:null;
      };
      dateHistory.set(date,{date,scope:`${limit}개 / 편도 ${fee}% · ${snap.created} 고정`,valid,
          a:totalSignals('A'),b:totalSignals('B'),
          b1:avgB('1h'),b3:avgB('3h'),b24:avgB('24h')});
      $('dateRows').innerHTML=Array.from(dateHistory.values()).sort((a,b)=>a.date.localeCompare(b.date)).map(x=>
        `<tr><td>${x.date}</td><td>${x.scope}</td><td>${fmt(x.valid)}</td><td>${fmt(x.a)}</td><td>${fmt(x.b)}</td><td>${pct(x.b1)}</td><td>${pct(x.b3)}</td><td>${pct(x.b24)}</td></tr>`).join('');
    }
  }catch(e){
    $('status').textContent='⚠️ 중단: '+e.message+' · 이미 받은 결과는 아래 남겼습니다. 미완성 원본은 고정하지 않습니다.';
  }finally{
    running=false;$('start').disabled=false;
    for(const name of ['snapshotExport','snapshotImport','snapshotReset','cohortReset'])$(name).disabled=false;
    await refreshSnapshotInfo();
  }
}
$('day').addEventListener('change',refreshSnapshotInfo);
$('limit').addEventListener('change',refreshSnapshotInfo);
$('snapshotExport').addEventListener('click',async()=>{
  if(running)return;
  try{
    if($('limit').value==='all'){await exportAllRaw();return;}
    const snap=await snapshotStore('get',snapshotKey());
    if(!snap)throw new Error('이 날짜·코인 수의 고정 데이터가 없습니다. 먼저 검사를 완료하세요.');
    validateSnapshot(snap,snap.date,snap.limit);
    const blob=new Blob([JSON.stringify(snap)],{type:'application/json'});
    const url=URL.createObjectURL(blob);const a=document.createElement('a');
    a.href=url;a.download=`daytrade_raw_${snap.date}_${snap.limit}_v8.json`;
    document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),4000);
    $('status').textContent='✅ 원본 봉 JSON 백업 다운로드를 요청했습니다. 휴대폰 다운로드 폴더를 확인하세요.';
  }catch(e){$('status').textContent='⚠️ 백업 오류: '+e.message;}
});
$('snapshotImport').addEventListener('click',()=>{if(!running)$('snapshotFile').click();});
$('snapshotFile').addEventListener('change',async()=>{
  try{
    const f=$('snapshotFile').files[0];if(!f)return;
    if(f.size>40_000_000)throw new Error('40MB 초과 백업은 불러올 수 없습니다');
    let raw;
    if(f.name.endsWith('.gz')){
      if(typeof DecompressionStream==='undefined')throw new Error('이 브라우저는 압축 백업 해제를 지원하지 않습니다');
      raw=await new Response(f.stream().pipeThrough(new DecompressionStream('gzip'))).text();
    }else raw=await f.text();
    const snap=JSON.parse(raw);
    if(snap.mode==='all'){await importAllRaw(snap);return;}
    
    validateSnapshot(snap,snap.date,snap.limit);
    snap.key=`${snap.date}|${snap.limit}`;
    const old=await snapshotStore('get',snap.key);
    const cohort=await snapshotStore('get',cohortKey(snap.limit));
    if(cohort && cohort.markets.join(',')!==snap.markets.join(',')){
      throw new Error('다른 날짜들과 코인 목록이 다릅니다. 해당 코인 수 전체 고정을 초기화한 뒤 불러오세요.');
    }
    if(old && !confirm(`${snap.date} ${snap.limit}개 기존 고정 데이터를 백업 파일로 덮어쓸까요?`))return;
    snap.source='imported';
    await snapshotStore('put',snap.key,snap);
    if(!cohort)await snapshotStore('put',cohortKey(snap.limit),makeCohort(snap));
    $('day').value=snap.date;$('limit').value=String(snap.limit);
    $('status').textContent='✅ 원본 봉 백업을 불러왔습니다. 분석 시작을 누르면 동일 원본으로 재검사합니다.';
    await refreshSnapshotInfo();
  }catch(e){$('status').textContent='⚠️ 불러오기 실패: '+e.message;}
  finally{$('snapshotFile').value='';}
});
$('snapshotReset').addEventListener('click',async()=>{
  if(running)return;
  if($('limit').value==='all'){
    const day=$('day').value,cohort=await snapshotStore('get',cohortKey('all'));
    if(!cohort)return;
    if(!confirm(`${day} 전체 KRW ${cohort.markets.length}개 원본을 지울까요? 복구하려면 백업이 필요합니다.`))return;
    try{
      for(let i=0;i<cohort.markets.length;i+=5)await snapshotStore('delete',allChunkKey(day,i));
      await snapshotStore('delete',allMetaKey(day));
      await refreshSnapshotInfo();
      $('status').textContent='이 날짜 전체 KRW 고정 원본만 해제했습니다.';
    }catch(e){$('status').textContent='⚠️ 전체 고정 해제 실패: '+e.message;}
    return;
  }
  const key=snapshotKey();
  if(!confirm(`${key} 날짜의 원본 캔들만 삭제할까요? 같은 ${$('limit').value}개 코인 목록은 계속 고정됩니다. 백업하지 않은 캔들은 복구할 수 없습니다.`))return;
  try{await snapshotStore('delete',key);await refreshSnapshotInfo();
      $('status').textContent='🔓 이 날짜의 원본 봉만 해제했습니다. 코인 목록은 유지한 채 다음 검사에서 새 원본을 수집합니다.';}
  catch(e){$('status').textContent='⚠️ 고정 해제 실패: '+e.message;}
});
$('cohortReset').addEventListener('click',async()=>{
  if(running)return;
  if($('limit').value==='all'){
    const cohort=await snapshotStore('get',cohortKey('all'));
    if(!cohort)return;
    if(!confirm(`전체 KRW ${cohort.markets.length}개 목록과 4개 날짜 모든 원본을 삭제할까요? 백업하지 않은 데이터는 복구 불가입니다.`))return;
    try{
      for(const day of SNAP_DAYS){
        for(let i=0;i<cohort.markets.length;i+=5)await snapshotStore('delete',allChunkKey(day,i));
        await snapshotStore('delete',allMetaKey(day));
      }
      await snapshotStore('delete',cohortKey('all'));
      dateHistory.clear();$('dateRows').innerHTML='';
      await refreshSnapshotInfo();
      $('status').textContent='전체 KRW 목록·원본 초기화를 완료했습니다';
    }catch(e){$('status').textContent='⚠️ 전체 목록 해제 실패: '+e.message;}
    return;
  }
  const limit=Number($('limit').value);
  if(!confirm(`${limit}개 코인 고정 목록과 4개 날짜의 원본 봉을 모두 삭제하고 다음 검사 때 코인 목록을 새로 선정할까요? 백업하지 않은 스냅샷은 복원할 수 없습니다.`))return;
  try{
    for(const date of SNAP_DAYS)await snapshotStore('delete',`${date}|${limit}`);
    await snapshotStore('delete',cohortKey(limit));
    dateHistory.clear();$('dateRows').innerHTML='';
    await refreshSnapshotInfo();
    $('status').textContent='🔓 고정 코인 목록과 해당 코인 수의 모든 날짜 데이터를 초기화했습니다.';
  }catch(e){$('status').textContent='⚠️ 전체 초기화 실패: '+e.message;}
});
$('start').addEventListener('click',startBacktest);
paint([],0,1,{});
refreshSnapshotInfo();
</script></body></html>"""


def register_daytrade_review(app, deps):
    """Register three additive routes using existing app.py helpers."""
    api_get = deps["api_get"]
    get_markets = deps["get_markets"]
    completed_rows = deps["backtest_completed_rows"]
    make_windows = deps["make_backtest_windows"]
    replay_a = deps["backtest_replay_signal"]
    replay_b = deps["backtest_fibonacci_signal"]
    kst = deps["KST"]
    cache = {"at": 0, "ranked": []}
    cache_lock = Lock()

    def get_ranked():
        with cache_lock:
            if cache["ranked"] and time.time() - cache["at"] < 300:
                return cache["ranked"][:]
            markets = sorted(set(get_markets()))
            tickers = {}
            for pos in range(0, len(markets), 50):
                raw = api_get("/v1/ticker", {"markets": ",".join(markets[pos:pos + 50])})
                if not isinstance(raw, list):
                    raise ValueError("ticker 응답 형식이 올바르지 않습니다")
                for x in raw:
                    market = x.get("market")
                    if market in markets:
                        try:
                            amount = float(x.get("acc_trade_price_24h", 0))
                            if isfinite(amount) and amount >= 0:
                                tickers[market] = amount
                        except (TypeError, ValueError):
                            pass
            ranked = sorted(tickers, key=lambda m: (-tickers[m], m))
            if not ranked:
                raise ValueError("현재 거래대금 순위를 가져오지 못했습니다")
            cache.update(at=time.time(), ranked=ranked)
            return ranked[:]

    def parse_kst(value):
        stamp = datetime.fromisoformat(value)
        return stamp.replace(tzinfo=kst) if stamp.tzinfo is None else stamp.astimezone(kst)

    def fetch_history(market, cutoff, source_rows=None):
        """Read actual exchange candles; fill only short timestamp gaps for testing.

        Synthetic rows are explicitly tagged, zero-volume, and never treated as
        real traded entry or signal bars. Gaps longer than 30m are never filled.
        """
        by_time = {}
        if source_rows is None:
            # First capture: retrieve actual exchange candles exactly once.
            cursor = cutoff
            for _ in range(4):
                page = completed_rows(market, 5, cursor, count=200)
                if not page:
                    break
                for row in page:
                    by_time[row["candle_date_time_kst"]] = row
                oldest = min(parse_kst(r["candle_date_time_kst"]) for r in page)
                if oldest >= cursor:
                    raise ValueError("캔들 페이지 시간순서 오류")
                cursor = oldest
        else:
            # Subsequent replays are entirely offline from exchange history.
            # The posted source must contain ORIGINAL observations, never filled bars.
            if not isinstance(source_rows, list) or len(source_rows) > 800:
                raise ValueError("고정 원본 캔들은 코인당 800개 이하여야 합니다")
            required = ("candle_date_time_kst", "opening_price", "high_price",
                        "low_price", "trade_price", "candle_acc_trade_price",
                        "candle_acc_trade_volume")
            for row in source_rows:
                if not isinstance(row, dict) or any(field not in row for field in required):
                    raise ValueError("고정 원본 5분봉 필수 항목이 없습니다")
                if row.get("_gap_fill"):
                    raise ValueError("보정 캔들은 원본 스냅샷에 포함할 수 없습니다")
                stamp = parse_kst(row["candle_date_time_kst"])
                if stamp >= cutoff or stamp.minute % 5 or stamp.second or stamp.microsecond:
                    raise ValueError("스냅샷 캔들의 날짜·5분 간격이 올바르지 않습니다")
                prices = [float(row[k]) for k in required[1:]]
                if (not all(isfinite(v) for v in prices)
                        or min(prices[0:4]) <= 0
                        or min(prices[4:]) < 0
                        or not prices[2] <= min(prices[0], prices[3])
                        or not max(prices[0], prices[3]) <= prices[1]):
                    raise ValueError("고정 캔들 가격·거래량 데이터 오류")
                key = row["candle_date_time_kst"]
                if key in by_time:
                    raise ValueError("스냅샷 캔들 시간이 중복되었습니다")
                by_time[key] = row
        actual = sorted(by_time.values(), key=lambda r: parse_kst(r["candle_date_time_kst"]))
        if not actual:
            return [], {"actual": 0, "filled": 0, "unfilled_gaps": 0, "max_gap_minutes": 0}, []

        padded = [actual[0]]
        n_filled = 0
        n_unfilled = 0
        max_gap_minutes = 0
        step = timedelta(minutes=5)
        for row in actual[1:]:
            previous = padded[-1]
            a = parse_kst(previous["candle_date_time_kst"])
            b = parse_kst(row["candle_date_time_kst"])
            seconds = (b - a).total_seconds()
            if seconds <= 0:
                raise ValueError("과거 캔들 시간 중복 또는 역순")
            if seconds % 300 != 0:
                raise ValueError("5분봉 시각 정렬 오류")
            missing = round(seconds / 300) - 1
            if missing:
                max_gap_minutes = max(max_gap_minutes, int(seconds / 60) - 5)
            if 0 < missing <= MAX_GAP_FILL_BARS:
                flat_close = float(previous["trade_price"])
                for j in range(1, missing + 1):
                    t = a + j * step
                    artificial = dict(previous)
                    artificial["candle_date_time_kst"] = t.strftime("%Y-%m-%dT%H:%M:%S")
                    artificial["candle_date_time_utc"] = t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                    artificial["opening_price"] = flat_close
                    artificial["high_price"] = flat_close
                    artificial["low_price"] = flat_close
                    artificial["trade_price"] = flat_close
                    artificial["candle_acc_trade_price"] = 0.0
                    artificial["candle_acc_trade_volume"] = 0.0
                    artificial["_gap_fill"] = True
                    padded.append(artificial)
                    n_filled += 1
            elif missing > MAX_GAP_FILL_BARS:
                n_unfilled += 1
            padded.append(row)
        return padded, {
            "actual": len(actual), "filled": n_filled,
            "unfilled_gaps": n_unfilled, "max_gap_minutes": max_gap_minutes,
        }, actual

    def empty_strategy():
        return {"signals": 0, "samples": EMPTY.copy(),
                "return_sums": EMPTY.copy(), "positive": EMPTY.copy(),
                "touch_counts": {k: 0 for k in TOUCH_KEYS},
                "scenarios": {key: {
                    "counts": {kind: 0 for kind in EXIT_OUTCOMES},
                    "resolved": 0, "net_sum": 0.0, "positive": 0,
                } for key, _, _ in EXIT_SCENARIOS},
                "examples": []}

    def evaluate_market(market, date, fee, source_rows=None, capture=False):
        output = {
            "market": market, "windows": 0, "coverage_reason": "", "history_rows": 0,
            "gap_filled": 0, "unfilled_gaps": 0, "max_gap_minutes": 0, "coverage_rejects": 0,
            "strategies": {"A": empty_strategy(), "B": empty_strategy()},
            "v6": new_v6_summary(),
            "v7": new_v7_summary(),
            "v9": new_v9_summary(),
            "diagnostics": {name: 0 for name in (
                "aBreakout", "aSupport", "bTrend", "bTouch", "bBounce",
                "bVolume", "bAllPrice", "bSwing25", "bEvaluated", "liquidity", "excluded")},
        }
        try:
            day = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=kst)
            start = day
            end = day + timedelta(hours=12)
            cutoff = day + timedelta(days=1, hours=12)
            rows, coverage, original_rows = fetch_history(market, cutoff, source_rows)
            if capture:
                # Returned once to browser IndexedDB; subsequent calls pass the
                # same actual (not synthetic) candles back for offline replay.
                output["_raw_candles"] = original_rows
            output["history_rows"] = coverage["actual"]
            output["gap_filled"] = coverage["filled"]
            output["unfilled_gaps"] = coverage["unfilled_gaps"]
            output["max_gap_minutes"] = coverage["max_gap_minutes"]
            # Diagnose historical coverage without altering any BUY criteria.
            run = longest_run = 0
            previous = None
            for candle in rows:
                stamp = datetime.fromisoformat(candle["candle_date_time_kst"])
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=kst)
                else:
                    stamp = stamp.astimezone(kst)
                run = run + 1 if previous is not None and stamp - previous == timedelta(minutes=5) else 1
                longest_run = max(longest_run, run)
                previous = stamp
            if len(rows) < 576:
                output["coverage_reason"] = (f"원본 {coverage['actual']}봉 / 보정 {coverage['filled']}봉 / "
                                             f"최장 연속 {longest_run}봉 · 최소 576봉 필요")
                return output
            # 288 historical 5-minute candles + 288 strictly future candles.
            # Independent strategy replays only see the LAST 80 historical candles.
            windows = make_windows(rows, minute=5, lookback=288, forward_bars=288)
            last_event = {"A": None, "B": None}
            for w in windows:
                at = w["cutoff_kst"].astimezone(kst)
                if not (start <= at < end):
                    continue
                history = w["signal_rows"]
                future = w["evaluation_rows"]
                if len(history) != 288 or len(future) != 288:
                    continue
                # Avoid interpreting synthetic, non-traded candles as a real
                # signal or a filled entry. Exclude excessively sparse history.
                filled_in_window = sum(c.get("_gap_fill", False) for c in history + future)
                if (filled_in_window > MAX_SYNTHETIC_BARS_PER_WINDOW
                        or history[-1].get("_gap_fill")
                        or future[0].get("_gap_fill")):
                    output["coverage_rejects"] += 1
                    continue
                output["windows"] += 1
                value24 = sum(float(c["candle_acc_trade_price"]) for c in history)
                if not isfinite(value24) or value24 < 0:
                    raise ValueError("거래대금 데이터 오류")
                liquid = value24 >= LIQUIDITY_MIN_KRW
                output["diagnostics"]["liquidity"] += int(liquid)
                # Match B's existing 2.5% swing precondition on its last 80 bars.
                # This is diagnostic only: no change to A or B signal decisions.
                h80 = [float(c["high_price"]) for c in history[-80:]]
                l80 = [float(c["low_price"]) for c in history[-80:]]
                peak_idx = max(range(45, 77), key=lambda i: h80[i])
                low_idx = min(range(peak_idx - 30, peak_idx), key=lambda i: l80[i])
                swing_pass = l80[low_idx] > 0 and h80[peak_idx] / l80[low_idx] - 1 >= 0.025
                output["diagnostics"]["bSwing25"] += int(swing_pass)
                input_window = {
                    "cutoff_kst": w["cutoff_kst"],
                    "signal_rows": history[-80:],
                    "evaluation_rows": [],
                }
                a = replay_a(input_window)
                b = replay_b(input_window)
                if not isinstance(a, dict) or not isinstance(b, dict):
                    continue
                diagnostics = output["diagnostics"]
                diagnostics["bEvaluated"] += int("trend_ok" in b)
                diagnostics["aBreakout"] += int(a.get("breakout_seen") is True)
                diagnostics["aSupport"] += int(a.get("support_held") is True)
                labels = ("trend_ok", "touch_ok", "bounce_ok", "volume_ok")
                for source, name in zip(labels, ("bTrend", "bTouch", "bBounce", "bVolume")):
                    diagnostics[name] += int(b.get(source) is True)
                b_core = all(b.get(field) is True for field in labels)
                diagnostics["bAllPrice"] += int(b_core)
                # Previous B implementation used an incorrect 80-bar liquidity
                # criterion. Apply the same verified 288-bar criterion to A and B.
                signals = {"A": a.get("signal") == "BUY" and liquid,
                           "B": b_core and liquid}
                if not liquid and (a.get("signal") == "BUY" or b_core):
                    diagnostics["excluded"] += 1
                for strategy, matched in signals.items():
                    if not matched:
                        continue
                    prev = last_event[strategy]
                    if prev is not None and at - prev < timedelta(minutes=60):
                        continue
                    entry = float(future[0]["opening_price"])
                    if entry <= 0 or not isfinite(entry):
                        continue
                    result = output["strategies"][strategy]
                    # Only after the signal decision: no future-data leakage
                    # into A/B replay or the liquid/price BUY gates.
                    quality = analyze_trade_quality(history, future, entry, fee)
                    result["touch_counts"][quality["first_touch"]] += 1
                    scenarios = analyze_exit_scenarios(future, entry, fee)
                    for scenario_id, test in scenarios.items():
                        agg = result["scenarios"][scenario_id]
                        agg["counts"][test["status"]] += 1
                        if test["net_pct"] is not None:
                            agg["resolved"] += 1
                            agg["net_sum"] += test["net_pct"]
                            agg["positive"] += int(test["net_pct"] > 0)
                    net = {}
                    for h in HOURS:
                        exit_price = float(future[12 * h - 1]["trade_price"])
                        if exit_price <= 0 or not isfinite(exit_price):
                            raise ValueError("평가 가격 오류")
                        # Buy fee is paid in addition to acquired principal.
                        # Sell fee reduces proceeds. Fee is one-side %.
                        ret = ((exit_price / entry) * (1 - fee) / (1 + fee) - 1) * 100
                        key = f"{h}h"
                        result["samples"][key] += 1
                        result["return_sums"][key] += ret
                        result["positive"][key] += int(ret > 0)
                        net[key] = round(ret, 3)
                    # V6 is observational only: B BUY was determined above.
                    # No future candle or TP/SL outcome enters the gate flags.
                    v6_flags = v6_gate_flags(quality) if strategy == "B" else None
                    if strategy == "B":
                        add_v6_observation(output["v6"], v6_flags,
                                           scenarios["p3_s2"], quality, net)
                    # V7 does NOT gate or alter BUY. It measures two pullback
                    # entries after the same B signal has already been issued.
                    v7_details = None
                    if strategy == "B":
                        v7_details = {}
                        for key, dip_pct, _label in V7_VARIANTS:
                            decision = evaluate_v7_pullback(history, future, fee, dip_pct)
                            add_v7_observation(output["v7"], key, decision,
                                               scenarios["p3_s2"])
                            v7_details[key] = decision
                    # V9 is only a fixed-time quality comparison on V7 -1%.
                    v9_flags = None
                    if strategy == "B":
                        v9_flags = evaluate_v9_confirmation(history, future, v7_details["dip10"])
                        add_v9_observation(output["v9"], v9_flags, v7_details["dip10"])
                    result["signals"] += 1
                    last_event[strategy] = at
                    if len(result["examples"]) < 12:
                        result["examples"].append({
                            "market": market, "time": at.strftime("%m-%d %H:%M"),
                            "entry": entry, "net": net, "quality": quality,
                            "scenarios": scenarios,
                            "v6_gates": v6_flags,
                            "v7": v7_details,
                            "v9": v9_flags,
                        })
            if output["windows"] == 0:
                output["coverage_reason"] = (f"원본 {coverage['actual']}봉 / 보정 {coverage['filled']}봉 / "
                                             f"최장 연속 {longest_run}봉 / 30분 초과 공백 "
                                             f"{coverage['unfilled_gaps']}곳 / 검증 제외 {output['coverage_rejects']}구간; "
                                             "선택일 00~12시 유효 구간 없음")
            return output
        except Exception as exc:
            output["error"] = f"{type(exc).__name__}: {exc}"[:180]
            output["windows"] = 0
            output["strategies"] = {"A": empty_strategy(), "B": empty_strategy()}
            output["v6"] = new_v6_summary()
            output["v7"] = new_v7_summary()
            output["v9"] = new_v9_summary()
            return output

    @app.get("/backtest-daytrade")
    def daytrade_review_page():
        return render_template_string(PAGE)

    @app.get("/api/daytrade-universe")
    def daytrade_review_universe():
        try:
            limit_arg = request.args.get("limit", "30")
            if limit_arg == "all":
                # All currently listed KRW markets, including those with no
                # valid ticker rank. Historical 24h >= 500m KRW remains a
                # per-signal constraint; no liquidity rule is relaxed.
                markets = sorted({m for m in get_markets()
                                  if isinstance(m, str) and MARKET_PATTERN.fullmatch(m)})
                if not 1 <= len(markets) <= 1000:
                    raise ValueError("전체 KRW 코인 수가 정상 범위가 아닙니다")
                return jsonify({"ok": True, "markets": markets,
                                "available_markets": len(markets),
                                "selection": "현재 상장 전체 KRW 코인 (과거 상장 목록 아님)"})
            limit = int(limit_arg)
            if limit not in (10, 30, 50):
                return jsonify({"ok": False, "error": "코인 수가 잘못되었습니다"}), 400
            markets = get_ranked()
            return jsonify({"ok": True, "markets": markets[:limit],
                            "available_markets": len(markets),
                            "selection": "현재 시점 24시간 거래대금 순위 (과거 순위 아님)"})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)[:160]}), 502

    @app.get("/api/daytrade-batch")
    def daytrade_review_batch():
        date = request.args.get("date", "2026-11-08")
        if date not in DAYS:
            return jsonify({"ok": False, "error": "지원하지 않는 날짜입니다"}), 400
        names = request.args.get("markets", "").split(",")
        if not 1 <= len(names) <= 5 or len(set(names)) != len(names) or not all(
                MARKET_PATTERN.fullmatch(name) for name in names):
            return jsonify({"ok": False, "error": "코인은 1~5개만 선택할 수 있습니다"}), 400
        try:
            fee_percent = float(request.args.get("fee", "0.25"))
            if fee_percent not in (0.0, 0.04, 0.25):
                raise ValueError("지원하지 않는 수수료입니다")
            fee = fee_percent / 100
            # Validate against the exchange's listed KRW universe.
            allowed = set(get_markets())
            if not set(names).issubset(allowed):
                raise ValueError("현재 상장되지 않은 KRW 마켓이 포함되었습니다")
            capture = request.args.get("capture", "0") == "1"
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(evaluate_market, name, date, fee, None, capture): name
                           for name in names}
                result_by_market = {}
                for future in as_completed(futures):
                    result_by_market[futures[future]] = future.result()
            return jsonify({"ok": True, "date": date,
                            "results": [result_by_market[name] for name in names],
                            "caveats": ["당일 00~12시 진입, 최대 24시간 추적",
                                        "과거 시점 24h 거래대금 5억 이상",
                                        "현재 상장 KRW 마켓 기준 선정: 과거 당시의 상장 목록은 아님"]})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)[:160]}), 502


    @app.post("/api/daytrade-replay")
    def daytrade_review_replay():
        """Re-evaluate previously captured raw candles (NO exchange fetches).

        A replay is self-contained and is unaffected by current market ranking,
        delisting, API rate limits, or historical candle pagination. Original
        A/B/V5/V6/V7 logic is exactly the same evaluate_market() function.
        """
        try:
            # Five markets of 800 bars each normally take much less than 8 MB.
            if request.content_length is not None and request.content_length > 8_000_000:
                return jsonify({"ok": False, "error": "고정 데이터 요청 크기 제한(8MB)"}), 413
            # A reverse proxy may forward a chunked request without Content-Length.
            if request.content_length is None and len(request.get_data(cache=True)) > 8_000_000:
                return jsonify({"ok": False, "error": "고정 데이터 요청 크기 제한(8MB)"}), 413
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict) or payload.get("schema") != "bithumb-v8-raw1":
                raise ValueError("V8 원본 스냅샷 형식이 아닙니다")
            date = payload.get("date")
            if date not in DAYS:
                raise ValueError("지원하지 않는 날짜입니다")
            fee_percent = float(payload.get("fee", 0.25))
            if fee_percent not in (0.0, 0.04, 0.25):
                raise ValueError("지원하지 않는 수수료입니다")
            blocks = payload.get("snapshots")
            if (not isinstance(blocks, list) or not 1 <= len(blocks) <= 5
                    or any(not isinstance(b, dict) for b in blocks)):
                raise ValueError("스냅샷은 요청당 1~5개 코인이어야 합니다")
            names = [b.get("market") for b in blocks]
            if len(set(names)) != len(names) or any(
                    not isinstance(m, str) or not MARKET_PATTERN.fullmatch(m) for m in names):
                raise ValueError("고정된 코인 이름이 올바르지 않습니다")
            if any(not isinstance(b.get("rows"), list) or len(b["rows"]) > 800 for b in blocks):
                raise ValueError("고정 원본 5분봉 개수가 올바르지 않습니다")
            # No get_markets/get_ranked/completed_rows calls here. These records
            # are user-saved research snapshots, not current exchange quotes.
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(evaluate_market, b["market"], date,
                                       fee_percent / 100, b["rows"], False): b["market"]
                           for b in blocks}
                result_by_market = {}
                for future in as_completed(futures):
                    result_by_market[futures[future]] = future.result()
            return jsonify({"ok": True, "date": date, "from_snapshot": True,
                            "results": [result_by_market[name] for name in names]})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)[:160]}), 400
