"""CHARTWALA360 inspired MINI structure research V0.5 — read-only.

H4 context -> M15 bullish CHoCH -> post-CHoCH pullback -> M1
closed swing-break -> next 1m open. This is a quantified experiment
from a visual educational example, not the source author's own code.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import math
import time
import requests

KST = ZoneInfo("Asia/Seoul")
API = "https://api.bithumb.com/v1/candles/minutes/"
VERSION = "CHART360 MINI V0.5"
MAX_MINUTE_PAGES = 72
MARKETS = ("KRW-XRP", "KRW-ETH", "KRW-NEAR", "KRW-SOL", "KRW-DOGE")


def _minute_data(market, tf, to, count=200):
    if market not in MARKETS or tf not in (1, 15, 240):
        raise ValueError("market/timeframe not supported")
    if not isinstance(to, datetime) or to.tzinfo is None:
        raise ValueError("KST-aware datetime required")
    err = None
    for retry in range(2):
        try:
            resp = requests.get(
                f"{API}{tf}", params={"market": market, "to": to.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S"),
                                          "count": max(1, min(200, int(count)))},
                headers={"Accept": "application/json", "User-Agent": "chart360-research/0.1"},
                timeout=(4, 12),
            )
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, list):
                raise RuntimeError("unexpected candles response")
            result = []
            for x in payload:
                try:
                    t = datetime.fromisoformat(x["candle_date_time_kst"]).replace(tzinfo=KST)
                    op, hi, lo, cl = [float(x[k]) for k in ("opening_price", "high_price", "low_price", "trade_price")]
                    vol = float(x.get("candle_acc_trade_price") or 0)
                    if not (min(op, hi, lo, cl) > 0 and hi >= max(op, cl)
                            and lo <= min(op, cl) and all(map(math.isfinite, [op, hi, lo, cl, vol]))):
                        continue
                    result.append({"t":t, "o":op, "h":hi, "l":lo, "c":cl, "v":vol})
                except (KeyError, ValueError, TypeError, OverflowError):
                    continue
            return sorted(result, key=lambda x: x["t"])
        except Exception as exc:
            err = exc
            if retry == 0:
                time.sleep(.3)
    raise RuntimeError(f"Bithumb {market} {tf}m: {str(err)[:140]}")


def historical_1m(market, end, hours=12, getter=_minute_data, scan_end=None):
    """Collect true exchange candles. Signal period ends before exit-only future."""
    if not (2<=hours<=168):raise ValueError("2~168 hours required")
    boundary=scan_end if scan_end is not None else end
    if boundary>end:raise ValueError("signal end cannot exceed fetched end")
    begin=boundary-timedelta(hours=hours,minutes=30)
    expected=int((end-begin).total_seconds()/60)
    limit=min(MAX_MINUTE_PAGES,max(8,math.ceil(expected/200)+6))
    seen,cursor={},end+timedelta(minutes=1)
    pages=0
    hit_begin=False
    while pages<limit:
        rows=getter(market,1,cursor,200)
        pages+=1
        if not rows:break
        for r in rows:
            if begin<=r["t"] and r["t"]+timedelta(minutes=1)<=end:
                seen[r["t"]]=r
        oldest=min(r["t"] for r in rows)
        if oldest<=begin:
            hit_begin=True
            break
        if oldest>=cursor:raise RuntimeError("1m pagination cursor not advancing")
        cursor=oldest
    bars=sorted(seen.values(),key=lambda x:x["t"])
    return bars,{"pages":pages,"minutes_observed":len(bars),
        "minutes_requested":expected,"minutes_missing":max(0,expected-len(bars)),
        "coverage_pct":round(len(bars)/expected*100,2),
        "incomplete":len(bars)<expected,
        "pagination_exhausted":pages>=limit and not hit_begin,
        "read_window_start_kst":begin.strftime("%Y-%m-%d %H:%M"),
        "read_window_end_kst":end.strftime("%Y-%m-%d %H:%M")}


def historical_15m(market,signal_begin,signal_end,getter=_minute_data):
    """Full CHoCH history, incl. 90m overlap and 17 15m warmup bars."""
    begin=signal_begin-timedelta(minutes=90+17*15)
    cursor=signal_end+timedelta(minutes=1)
    seen={}
    pages=0
    while pages<12:
        rows=getter(market,15,cursor,200)
        pages+=1
        if not rows:break
        for r in rows:
            if r["t"]>=begin and r["t"]+timedelta(minutes=15)<=signal_end:
                seen[r["t"]]=r
        oldest=min(r["t"] for r in rows)
        if oldest<=begin:break
        if oldest>=cursor:raise RuntimeError("15m pagination cursor not advancing")
        cursor=oldest
    bars=sorted(seen.values(),key=lambda x:x["t"])
    return bars,{"m15_pages":pages,"m15_bars":len(bars),
        "m15_first_kst":bars[0]["t"].strftime("%Y-%m-%d %H:%M") if bars else None}



def _h4_bearish(h4, at):
    r = [x for x in h4 if x["t"] + timedelta(hours=4) <= at]
    if len(r) < 12:
        return None
    r=r[-12:]
    if any(r[k+1]['t']-r[k]['t']!=timedelta(hours=4) for k in range(11)):
        return None
    a,b=r[:6],r[6:]
    return (max(x["h"] for x in b) < max(x["h"] for x in a),
            min(x["l"] for x in b) < min(x["l"] for x in a))


def _m15_chochs(m15):
    """Close above preceding 8 bar high after a lower-low structure."""
    events=[]
    for i in range(16,len(m15)):
        bar=m15[i]
        prior=m15[i-8:i]
        older=m15[i-16:i-8]
        window=m15[i-16:i+1]
        if any(window[j+1]["t"]-window[j]["t"]!=timedelta(minutes=15) for j in range(16)):
            continue
        if (bar["c"] > max(x["h"] for x in prior)
                and min(x["l"] for x in prior) < min(x["l"] for x in older)
                and bar["c"] > bar["o"]):
            events.append({"end":bar["t"]+timedelta(minutes=15),
                           "level":max(x["h"] for x in prior),
                           "close":bar["c"],"low":min(x["l"] for x in prior),
                           "turnover":bar["v"]})
    return events


def _simulate(minute, entry_index, entry_price, stop_price, fee_each, max_hold=60):
    """Strict 1m forward replay, first price is NEXT bar open. No price fills."""
    out={"status":"UNKNOWN","net_pct":None,"exit_reason":"NEXT_BAR_MISSING",
         "hold_minutes":None}
    if entry_index >= len(minute):
        return out
    tp=entry_price + 1.5*(entry_price-stop_price)
    prev=None
    for k in range(entry_index, min(len(minute),entry_index+max_hold)):
        r=minute[k]
        if prev is not None and r["t"]-prev != timedelta(minutes=1):
            return {**out,"exit_reason":"MISSING_1M_BAR","hold_minutes":k-entry_index}
        prev=r["t"]
        px=None
        reason=None
        if r["l"]<=stop_price:
            px=min(stop_price,r["o"])   # stop-first, gap adverse
            reason="STOP"
        elif r["h"]>=tp:
            px=tp
            reason="TP_1_5R"
        elif k==entry_index+max_hold-1:
            px=r["c"]
            reason="TIME_EXIT"
        if px is not None:
            fee=fee_each/100
            net=(px*(1-fee)/(entry_price*(1+fee))-1)*100
            return {"status":"RESOLVED","net_pct":round(net,4),
                    "exit_reason":reason,"hold_minutes":k-entry_index+1}
    return {**out,"exit_reason":"INSUFFICIENT_FUTURE_1M",
            "hold_minutes":max(0,len(minute)-entry_index)}


def replay(market, minute, m15, h4, fee_each=.04, max_trades=None, scan_begin=None,
           require_h4=True, confirmation_bars=0, scan_end=None):
    """A: H4 required; B: H4 informational; C: B plus one closed M1 confirmation."""
    if confirmation_bars not in (0,1):
        raise ValueError("confirmation_bars must be 0 or 1")
    variant="A" if require_h4 else ("C" if confirmation_bars else "B")
    if fee_each not in (.04,.25):
        raise ValueError("fee_each must be 0.04 or 0.25")
    minute=sorted(minute,key=lambda r:r["t"])
    m15=sorted(m15,key=lambda r:r["t"])
    h4=sorted(h4,key=lambda r:r["t"])
    events=_m15_chochs(m15)
    # An event is eligible if its 90-minute post-CHoCH window overlaps
    # the requested test interval. Count diagnostic events, not each 1m
    # retry or the full 45h of downloaded 15m indicator history.
    diagnostic_begin = scan_begin if scan_begin is not None else (
        minute[0]["t"] if minute else None)
    diagnostic_end = scan_end if scan_end is not None else (minute[-1]["t"]+timedelta(minutes=1) if minute else None)
    relevant = [ev for ev in events if diagnostic_begin is not None
                and diagnostic_end is not None
                and diagnostic_begin-timedelta(minutes=90) <= ev["end"] <= diagnostic_end]
    event_states = {
        ev["end"]: {
            "choch_kst":ev["end"].strftime("%Y-%m-%d %H:%M"),
            "inside_requested_window":ev["end"] >= diagnostic_begin,
            "observed_1m_bars":0,"h4_ok":False,
            "h4_data_missing":False,"h4_structure_failed":False,
            "h4_lower_high":False,"h4_lower_low":False,
            "pullback":False,"structure_invalidated":False,
            "m1_breakout":False,"m1_gap":False,"risk_rejected":False,
            "confirm_rejected":False,"confirm_gap":False,
            "entries":0,
        } for ev in relevant
    }
    stats={"m15_choch_events":len(events),"h4_context_ok":0,
           "pullbacks":0,"m1_breakouts":0,"entries":0,
           "reject_risk":0,"reject_gap":0,
           "confirm_rejected":0,"confirm_gap":0}
    trades=[]
    cooldown_until=None
    last_event=None
    pulled=False
    pull_low=None
    for i in range(25,len(minute)-1):
        r=minute[i]
        at=r["t"] + timedelta(minutes=1)
        if scan_begin is not None and at < scan_begin:
            continue  # indicator warm-up bars are not eligible entry times
        if scan_end is not None and at>=scan_end:
            continue  # future 60m is only for exits, not signals
        if cooldown_until and at < cooldown_until:
            continue
        ev=next((v for v in reversed(events)
                 if v["end"]<=at<=v["end"]+timedelta(minutes=90)),None)
        if ev is None:
            last_event=None
            continue
        if ev is not last_event:
            last_event=ev
            pulled=False
            pull_low=None
        context=_h4_bearish(h4,at)
        trace=event_states.get(ev["end"])
        if trace is not None:
            trace["observed_1m_bars"]+=1
            if context is None:
                trace["h4_data_missing"]=True
            else:
                trace["h4_lower_high"] = trace["h4_lower_high"] or bool(context[0])
                trace["h4_lower_low"] = trace["h4_lower_low"] or bool(context[1])
                if all(context):
                    trace["h4_ok"]=True
                else:
                    trace["h4_structure_failed"]=True
        if require_h4 and (context is None or not all(context)):
            continue
        if at==ev["end"]+timedelta(minutes=1):
            stats["h4_context_ok"]+=1
        # Watch a genuine retracement AFTER the completed 15m break.
        if at > ev["end"] and not pulled and ev["level"]*.985 <= r["l"] <= ev["close"]*.998:
            pulled=True
            pull_low=r["l"]
            stats["pullbacks"]+=1
            if trace is not None: trace["pullback"]=True
            continue
        if not pulled:
            continue
        pull_low=min(pull_low,r["l"])
        if pull_low < ev["level"]*.975:
            if trace is not None: trace["structure_invalidated"]=True
            pulled=False
            continue
        previous=minute[i-5:i]
        if len(previous)!=5 or any(
            previous[j+1]["t"]-previous[j]["t"]!=timedelta(minutes=1)
            for j in range(4)):
            stats["reject_gap"]+=1
            if trace is not None: trace["m1_gap"]=True
            continue
        reference=max(x["h"] for x in previous)
        impulse=r["c"]>r["o"] and (r["c"]-r["o"])/max(r["h"]-r["l"],1e-12)>.55
        if not (r["c"]>reference and r["c"]>ev["level"] and impulse):
            continue
        stats["m1_breakouts"]+=1
        if trace is not None: trace["m1_breakout"]=True
        avg_vol=sum(x["v"] for x in minute[i-20:i])/20
        vratio=r["v"]/avg_vol if avg_vol>0 else 0
        nextbar=minute[i+1]
        if nextbar["t"]-r["t"]!=timedelta(minutes=1):
            stats["reject_gap"]+=1
            if trace is not None: trace["m1_gap"]=True
            continue
        # C's only entry-change from B: wait for NEXT full 1m candle to
        # close bullish and ABOVE the already-known breakout close.
        # C enters on the FOLLOWING 1m open, never the confirmation close.
        entry_idx=i+1
        confirm_pass=None
        if confirmation_bars:
            if scan_end is not None and at+timedelta(minutes=1)>=scan_end:
                continue  # confirmation signal would close beyond test window
            if i+2>=len(minute):
                stats["confirm_gap"]+=1
                if trace is not None:trace["confirm_gap"]=True
                continue
            confirm=minute[i+1]
            entrybar=minute[i+2]
            if entrybar["t"]-confirm["t"]!=timedelta(minutes=1):
                stats["confirm_gap"]+=1
                if trace is not None:trace["confirm_gap"]=True
                continue
            confirm_pass=(confirm["c"]>confirm["o"] and
                          confirm["c"]>r["c"] and
                          confirm["c"]>ev["level"])
            if not confirm_pass:
                stats["confirm_rejected"]+=1
                if trace is not None:trace["confirm_rejected"]=True
                continue
            entry_idx=i+2
            nextbar=entrybar
        entry=nextbar["o"]
        stop=pull_low*.999
        risk=(entry-stop)/entry*100
        if risk<.25 or risk>3 or entry<=stop:
            stats["reject_risk"]+=1
            if trace is not None: trace["risk_rejected"]=True
            continue
        # 7 checked features, including a numeric volume subcondition.
        score=sum([bool(context and context[0]), bool(context and context[1]), True, pulled,
                   r["c"]>reference, impulse, vratio>=1.1])
        # B's score retains both H4 evidence flags for fair comparison, but
        # does not require either H4 flag to validate its entry structure.
        sig=("BUY_TEST" if score>=6 else "WATCH_TEST") if require_h4 else (
            "C_CONFIRM_TEST" if confirmation_bars else "B_ENTRY_TEST")
        exit=_simulate(minute,entry_idx,entry,stop,fee_each)
        candle_range=max(r["h"]-r["l"],1e-12)
        trailing20=minute[i-20:i]
        contiguous20=(len(trailing20)==20 and
                      all(trailing20[j+1]["t"]-trailing20[j]["t"]==timedelta(minutes=1)
                          for j in range(19)) and
                      r["t"]-trailing20[-1]["t"]==timedelta(minutes=1))
        trades.append({"market":market,"signal":sig,"variant":variant,
                       "breakout_kst":at.strftime("%Y-%m-%d %H:%M"),
                       "signal_kst":(at+timedelta(minutes=confirmation_bars)).strftime("%Y-%m-%d %H:%M"),
                       "entry_delay_minutes":confirmation_bars,
                       "entry_kst":nextbar["t"].strftime("%Y-%m-%d %H:%M"),
                       "entry":round(entry,8),"stop":round(stop,8),
                       "target":round(entry+1.5*(entry-stop),8),
                       "risk_pct":round(risk,3),
                       "score":score,"score_total":7,
                       "volume_ratio":round(vratio,2),
                       "breakout_body_pct":round(abs(r["c"]-r["o"])/candle_range*100,2),
                       "breakout_upper_wick_pct":round((r["h"]-max(r["c"],r["o"]))/candle_range*100,2),
                       "breakout_range_pct":round((r["h"]-r["l"])/r["c"]*100,3),
                       "prior20_1m_contiguous":contiguous20,
                       "minutes_after_choch":round((at-ev["end"]).total_seconds()/60),
                       "confirm_bullish_continuation":confirm_pass,
                       "m15_choch_kst":ev["end"].strftime("%Y-%m-%d %H:%M"),
                       **exit})
        stats["entries"]+=1
        if trace is not None: trace["entries"]+=1
        cooldown_until=at+timedelta(minutes=65)
        if max_trades is not None and len(trades)>=max_trades:
            break
    # Mutually exclusive final reason per relevant M15 event. Signals
    # and exits above are not changed; the new audit is observation only.
    reasons = {
        "ENTRY_TEST": "가상 진입 기록",
        "M1_BREAKOUT_RISK_REJECT": "1분봉 돌파 후 손절 거리 기준 탈락",
        "M1_CONFIRM_REJECTED":"다음 완료 1분봉이 양봉 종가 상승을 확인하지 못함",
        "M1_CONFIRM_DATA_GAP":"추가 확인봉 또는 그 다음 진입봉의 연속 자료 부족",
        "M1_DATA_GAP": "1분봉 연속성 또는 다음 진입봉 부족",
        "M1_NO_BREAKOUT": "눌림 확인 후 1분봉 스윙 고점 종가 돌파 없음",
        "PULLBACK_INVALIDATED": "눌림이 구조 무효화 가격 아래로 내려감",
        "M1_NO_PULLBACK": "4시간 하락구조 충족 후 눌림 조건 불충족",
        "H4_DATA_MISSING": "4시간봉 12개 완료봉 부족 또는 시각 불연속",
        "H4_LH_LL_FAILED": "4시간봉의 낮아진 고점/저점 조건 불충족",
        "M1_NO_CANDLES": "15분 CHoCH 이후 분석 가능한 1분봉 없음",
        "NO_CHoCH": "선택 구간 전후 90분에 유효한 15분 CHoCH 없음",
        "M1_NOT_EVALUATED": "1분봉 구간 또는 쿨다운 때문에 후속조건 미평가",
    }
    def cause(e):
        if e["entries"]: return "ENTRY_TEST"
        if e.get("confirm_rejected"): return "M1_CONFIRM_REJECTED"
        if e.get("confirm_gap"): return "M1_CONFIRM_DATA_GAP"
        if e["m1_breakout"] and e["risk_rejected"]: return "M1_BREAKOUT_RISK_REJECT"
        if e["m1_gap"]: return "M1_DATA_GAP"
        if e["pullback"] and e["structure_invalidated"]: return "PULLBACK_INVALIDATED"
        if e["pullback"]: return "M1_NO_BREAKOUT"
        if not require_h4:
            return "M1_NO_PULLBACK" if e["observed_1m_bars"] else "M1_NO_CANDLES"
        if e["h4_ok"]: return "M1_NO_PULLBACK"
        if e["h4_data_missing"]: return "H4_DATA_MISSING"
        if e["h4_structure_failed"]: return "H4_LH_LL_FAILED"
        if not e["observed_1m_bars"]: return "M1_NO_CANDLES"
        return "M1_NOT_EVALUATED"
    event_rows = list(event_states.values())
    for e in event_rows:
        e["reason_code"] = cause(e)
        e["reason_text"] = reasons[e["reason_code"]]
    from collections import Counter
    reason_counts = dict(Counter(e["reason_code"] for e in event_rows))
    # Snapshot context is a separate retrospective diagnostic.
    # It is NEVER fed into an earlier signal decision.
    h4_at_test_end = _h4_bearish(h4, diagnostic_end) if diagnostic_end else None
    if h4_at_test_end is None:
        h4_at_end_code = "H4_DATA_MISSING"
    elif all(h4_at_test_end):
        h4_at_end_code = "H4_BEARISH_LH_LL"
    elif not h4_at_test_end[0] and not h4_at_test_end[1]:
        h4_at_end_code = "H4_NO_LH_NO_LL"
    elif not h4_at_test_end[0]:
        h4_at_end_code = "H4_NO_LOWER_HIGH"
    else:
        h4_at_end_code = "H4_NO_LOWER_LOW"
    missing_test_minutes = (
        not minute or
        (diagnostic_begin is not None and diagnostic_end is not None
         and diagnostic_end <= diagnostic_begin)
    )
    blocker = ("M1_NO_CANDLES" if missing_test_minutes else
               max(reason_counts, key=lambda x:reason_counts[x])
               if reason_counts else "NO_CHoCH")
    diagnostic = {
        "version": VERSION, "market": market,
        "variant":variant,
        "confirmation_bars":confirmation_bars,
        "h4_gate_required":require_h4,
        "scan_begin_kst":diagnostic_begin.strftime("%Y-%m-%d %H:%M") if diagnostic_begin else None,
        "last_actual_1m_kst":minute[-1]["t"].strftime("%Y-%m-%d %H:%M") if minute else None,
        "m15_events_all_history":len(events),
        "h4_snapshot_code":h4_at_end_code,
        "h4_snapshot_lower_high":h4_at_test_end[0] if h4_at_test_end else None,
        "h4_snapshot_lower_low":h4_at_test_end[1] if h4_at_test_end else None,
        "m15_events_in_test":sum(bool(x["inside_requested_window"]) for x in event_rows),
        "m15_events_overlapping_test":len(event_rows),
        "h4_event_pass":sum(bool(x["h4_ok"]) for x in event_rows),
        "h4_missing_event":sum(bool(x["h4_data_missing"]) for x in event_rows),
        "h4_failed_event":sum(bool(x["h4_structure_failed"]) for x in event_rows),
        "pullback_event":sum(bool(x["pullback"]) for x in event_rows),
        "m1_breakout_event":sum(bool(x["m1_breakout"]) for x in event_rows),
        "entry_event":sum(bool(x["entries"]) for x in event_rows),
        "gap_event":sum(bool(x["m1_gap"]) for x in event_rows),
        "risk_reject_event":sum(bool(x["risk_rejected"]) for x in event_rows),
        "confirm_reject_event":sum(bool(x["confirm_rejected"]) for x in event_rows),
        "confirm_gap_event":sum(bool(x["confirm_gap"]) for x in event_rows),
        "reason_counts":reason_counts,
        "primary_blocker_code":blocker,
        "primary_blocker_text":reasons[blocker],
        "event_details":event_rows,
        "note":"CHoCH 45시간 전체 기록과 이번 검사시간 내 이벤트는 별개. 이벤트별 단계 통과 집계이며 실제 주문 아님.",
    }
    resolved=[t["net_pct"] for t in trades if t["status"]=="RESOLVED"]
    wins=sum(x>0 for x in resolved)
    return {
        "market":market,"variant":variant,
        "h4_gate_required":require_h4,
        "confirmation_bars":confirmation_bars,
        "trades":trades,"stats":stats,"diagnostic":diagnostic,
        "summary":{"signals":len(trades),"resolved":len(resolved),
                   "unknown":len(trades)-len(resolved),"positive":wins,
                   "nonpositive":len(resolved)-wins,
                   "mean_net_pct":round(sum(resolved)/len(resolved),3) if resolved else None},
        "rules":"4H LH+LL (last 6 vs previous 6); M15 CHoCH close above prior8 high following lower lows; post-break retracement; M1 five-bar high break by close & bullish body; next M1 open; pullback stop -0.1%; TP 1.5R / 60m; fee each side",
    }



def _daily_rows(market,variants,one,scan_begin,scan_end,error=None):
    """KST calendar dates, partial boundary days, including zero-trade dates."""
    today=scan_begin.replace(hour=0,minute=0,second=0,microsecond=0)
    stamps={r["t"] for r in one}
    out=[]
    while today<scan_end:
        tomorrow=today+timedelta(days=1)
        start=max(today,scan_begin)
        stop=min(tomorrow,scan_end)
        exp=int((stop-start).total_seconds()/60)
        obs=sum(start<=stamp<stop for stamp in stamps)
        day=today.strftime("%Y-%m-%d")
        for v in ("A","B","C"):
            trades=[t for t in variants[v]["trades"] if t["entry_kst"].startswith(day)]
            known=[t["net_pct"] for t in trades if t["status"]=="RESOLVED"]
            out.append({"date_kst":day,"market":market,"variant":v,
                "partial_day":exp<1440,
                "window_start_kst":start.strftime("%Y-%m-%d %H:%M"),
                "window_end_kst":stop.strftime("%Y-%m-%d %H:%M"),
                "minutes_expected":exp,"minutes_observed":obs,
                "minutes_missing":max(0,exp-obs),
                "coverage_pct":round(obs/exp*100,2) if exp else None,
                "data_status":"API_ERROR" if error else ("INCOMPLETE" if obs<exp else "COMPLETE"),
                "signals":len(trades),"resolved":len(known),
                "positive":sum(x>0 for x in known),
                "nonpositive":sum(x<=0 for x in known),
                "unknown":len(trades)-len(known),
                "tp":sum(t["exit_reason"]=="TP_1_5R" for t in trades),
                "stop":sum(t["exit_reason"]=="STOP" for t in trades),
                "time_exit":sum(t["exit_reason"]=="TIME_EXIT" for t in trades),
                "net_sum_pct":round(sum(known),4) if known else None,
                "mean_net_pct":round(sum(known)/len(known),4) if known else None,
                "unknown_reasons":dict(__import__("collections").Counter(
                    t["exit_reason"] for t in trades if t["status"]!="RESOLVED")),
            })
        today=tomorrow
    return out


def _paired_trades(market,variants):
    """Pair by original breakout minute, NEVER by delayed C entry minute."""
    bs={t["breakout_kst"]:t for t in variants["B"]["trades"]}
    cs={t["breakout_kst"]:t for t in variants["C"]["trades"]}
    out=[]
    for stamp in sorted(set(bs)|set(cs)):
        b=bs.get(stamp)
        c=cs.get(stamp)
        both=bool(b and c)
        resolved=bool(both and b["status"]=="RESOLVED" and c["status"]=="RESOLVED")
        delta=round(c["net_pct"]-b["net_pct"],4) if resolved else None
        out.append({"market":market,"breakout_kst":stamp,
           "match_status":"PAIRED" if both else ("B_ONLY" if b else "C_ONLY"),
           "B_entry_kst":b["entry_kst"] if b else None,
           "C_entry_kst":c["entry_kst"] if c else None,
           "B_exit_reason":b["exit_reason"] if b else None,
           "C_exit_reason":c["exit_reason"] if c else None,
           "B_net_pct":b["net_pct"] if b else None,
           "C_net_pct":c["net_pct"] if c else None,
           "C_minus_B_pctpoints":delta,
           "comparison":"C_BETTER" if delta is not None and delta>0
                        else ("C_WORSE" if delta is not None and delta<0
                              else ("SAME" if delta==0 else "UNRESOLVED_OR_UNMATCHED"))})
    return out


def _aggregate_daily(daily):
    groups={}
    for row in daily:
        key=(row["date_kst"],row["variant"])
        g=groups.setdefault(key,{"date_kst":key[0],"variant":key[1],
            "markets":0,"error_markets":0,"partial_day":False,
            "minutes_observed":0,"minutes_expected":0,
            "signals":0,"resolved":0,"positive":0,"nonpositive":0,
            "unknown":0,"tp":0,"stop":0,"time_exit":0,
            "net_sum_pct":0.0})
        g["markets"]+=1
        g["error_markets"]+=row["data_status"]=="API_ERROR"
        g["partial_day"]|=row["partial_day"]
        for col in ("minutes_observed","minutes_expected","signals","resolved",
                    "positive","nonpositive","unknown","tp","stop","time_exit"):
            g[col]+=row[col]
        if row["net_sum_pct"] is not None:g["net_sum_pct"]+=row["net_sum_pct"]
    out=[]
    for key in sorted(groups):
        g=groups[key]
        g["minutes_missing"]=max(0,g["minutes_expected"]-g["minutes_observed"])
        g["coverage_pct"]=round(g["minutes_observed"]/g["minutes_expected"]*100,2) if g["minutes_expected"] else None
        g["mean_net_pct"]=round(g["net_sum_pct"]/g["resolved"],4) if g["resolved"] else None
        g["net_sum_pct"]=round(g["net_sum_pct"],4) if g["resolved"] else None
        out.append(g)
    return out


def _pair_summary(pairs):
    matched=[p for p in pairs if p["match_status"]=="PAIRED"]
    known=[p for p in matched if p["C_minus_B_pctpoints"] is not None]
    return {"paired":len(matched),"resolved_pairs":len(known),
            "unresolved_pairs":len(matched)-len(known),
            "B_only":sum(p["match_status"]=="B_ONLY" for p in pairs),
            "C_only":sum(p["match_status"]=="C_ONLY" for p in pairs),
            "C_better":sum(p["comparison"]=="C_BETTER" for p in known),
            "C_worse":sum(p["comparison"]=="C_WORSE" for p in known),
            "same":sum(p["comparison"]=="SAME" for p in known),
            "mean_C_minus_B_pctpoints":
                round(sum(p["C_minus_B_pctpoints"] for p in known)/len(known),4)
                if known else None}



def run_backtest(markets=("KRW-XRP",), hours=12, fee_each=.04,
                 now=None, getter=_minute_data, asof_kst=None):
    """Up to 7d of original A/B/C rules, all entries timed to closed bars."""
    if not isinstance(markets,(list,tuple)) or not 1<=len(markets)<=5:
        raise ValueError("choose 1–5 markets")
    if any(m not in MARKETS for m in markets) or len(set(markets))!=len(markets):
        raise ValueError("unsupported/duplicate Bithumb KRW market")
    hours=int(hours)
    if hours not in (6,12,24,72,168):
        raise ValueError("hours: 6,12,24,72,168")
    if fee_each not in (.04,.25):
        raise ValueError("fee each: .04 or .25")
    if now is not None and asof_kst is not None:
        raise ValueError("now and asof_kst cannot be provided together")
    if asof_kst is not None:
        # Fixed KST cutoff shared across 5 independent 7d API requests.
        end=datetime.strptime(asof_kst,"%Y-%m-%d %H:%M").replace(tzinfo=KST)
        age=(datetime.now(KST)-end).total_seconds()
        if age<60 or age>5400:
            raise ValueError("asof_kst must be between 1 and 90 minutes old")
    else:
        end=(now or datetime.now(KST)).astimezone(KST).replace(
            second=0,microsecond=0)-timedelta(minutes=2)
    scan_end=end-timedelta(minutes=60)
    scan_begin=scan_end-timedelta(hours=hours)
    output=[]
    for market in markets:
        one=[]
        audit={}
        try:
            # 60 real minutes AFTER scan_end for exit evaluation only.
            one,audit=historical_1m(market,end,hours,getter,scan_end=scan_end)
            m15,m15_audit=historical_15m(market,scan_begin,scan_end,getter)
            h4=[r for r in getter(market,240,scan_end+timedelta(minutes=1),100)
                if r["t"]+timedelta(hours=4)<=scan_end]
            a=replay(market,one,m15,h4,fee_each,scan_begin=scan_begin,
                     require_h4=True,scan_end=scan_end)
            b=replay(market,one,m15,h4,fee_each,scan_begin=scan_begin,
                     require_h4=False,scan_end=scan_end)
            c=replay(market,one,m15,h4,fee_each,scan_begin=scan_begin,
                     require_h4=False,confirmation_bars=1,scan_end=scan_end)
            audit={**audit,**m15_audit,"h4_bars":len(h4),
                "first_1m_kst":one[0]["t"].strftime("%Y-%m-%d %H:%M") if one else None,
                "last_1m_kst":one[-1]["t"].strftime("%Y-%m-%d %H:%M") if one else None}
            out={**a,"data_audit":audit,"variants":{"A":a,"B":b,"C":c}}
        except Exception as exc:
            message=str(exc)[:200]
            d={"version":VERSION,"market":market,
               "primary_blocker_code":"DATA_API_ERROR",
               "primary_blocker_text":"빗썸 데이터 조회 실패",
               "reason_counts":{"DATA_API_ERROR":1},"event_details":[]}
            sm={"signals":0,"resolved":0,"unknown":0,"positive":0,
                "nonpositive":0,"mean_net_pct":None}
            variants={v:{"market":market,"variant":v,
                         "h4_gate_required":v=="A",
                         "confirmation_bars":1 if v=="C" else 0,
                         "trades":[],
                         "diagnostic":{**d,"variant":v,
                             "confirmation_bars":1 if v=="C" else 0,
                             "h4_gate_required":v=="A"},
                         "summary":sm,"stats":{}}
                      for v in ("A","B","C")}
            out={"market":market,"error":message,"trades":[],
                 "variants":variants,"diagnostic":d,"data_audit":audit,
                 "stats":{},"summary":sm}
        out["daily"]=_daily_rows(market,out["variants"],one,scan_begin,scan_end,
                                error=out.get("error"))
        out["pairs"]=_paired_trades(market,out["variants"])
        output.append(out)
    alltrades=[t for r in output for t in r.get("trades",[])]
    known=[t["net_pct"] for t in alltrades if t["status"]=="RESOLVED"]
    def variant_total(v):
        trades=[t for r in output for t in r["variants"][v]["trades"]]
        resolved=[t["net_pct"] for t in trades if t["status"]=="RESOLVED"]
        return {"signals":len(trades),"resolved":len(resolved),
                "unknown":len(trades)-len(resolved),
                "positive":sum(x>0 for x in resolved),
                "nonpositive":sum(x<=0 for x in resolved),
                "mean_net_pct":round(sum(resolved)/len(resolved),3) if resolved else None}
    comparisons={"A":variant_total("A"),"B":variant_total("B"),
                 "C":variant_total("C"),
                 "difference_B_minus_A":sum(len(r["variants"]["B"]["trades"])-
                     len(r["variants"]["A"]["trades"]) for r in output),
                 "difference_C_minus_B":sum(len(r["variants"]["C"]["trades"])-
                     len(r["variants"]["B"]["trades"]) for r in output),
                 "reference":"A 4H 필수 / B 돌파 다음 봉 시가 / C 추가 1분 확인 후 다음 시가. 4H 제외 및 1분 확인 외 진입 기준 동일"}
    daily=[r for m in output for r in m["daily"]]
    pairs=[p for m in output for p in m["pairs"]]
    return {
        "version":VERSION,"mode":"historical_public_candles_only",
        "asof_kst":end.strftime("%Y-%m-%d %H:%M"),
        "scan_begin_kst":scan_begin.strftime("%Y-%m-%d %H:%M"),
        "scan_end_kst":scan_end.strftime("%Y-%m-%d %H:%M"),
        "exit_horizon_minutes":60,"lookback_hours":hours,
        "lookback_days":hours/24,"fee_each_pct":fee_each,
        "markets":output,"comparison":comparisons,
        "daily_by_market":daily,"daily_overall":_aggregate_daily(daily),
        "pair_rows":pairs,"pair_summary":_pair_summary(pairs),
        "failed_markets":[m["market"] for m in output if m.get("error")],
        "total":{"signals":len(alltrades),"resolved":len(known),
                 "unknown":len(alltrades)-len(known),
                 "positive":sum(x>0 for x in known),
                 "negative_or_zero":sum(x<=0 for x in known),
                 "mean_net_pct":round(sum(known)/len(known),3) if known else None},
        "warnings":[
            "3/7일 신호 기간은 시작~종료까지 72/168시간의 연속 구간. KST 날짜별 첫째/마지막 날짜는 부분일일 수 있음",
            "마지막 신호 탐색 시각은 최신 완료분봉보다 60분 빠르며, 이후 60분은 청산 확인 전용",
            "자료 수집 공백을 거래 없음이나 손실로 간주하지 않음. 데이터 조회 실패 시 해당 종목 별도 표기",
            "A/B/C의 같은 돌파는 중복 표본, 승률과 평균은 UNKNOWN 제외한 확정분만",
            "후향적 실험일 뿐, 슬리피지 0%, 다음 봉 시가 체결 가정, 실거래 신호 아님"
        ]
    }



HTML=r'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>CHART360 MINI V0.5 · 구조전환 연구실</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}body{background:#101827;color:#eaf1ff;font:15px system-ui;margin:0}main{max-width:900px;margin:auto;padding:20px 14px}.panel{border:1px solid #30405a;border-radius:15px;background:#1a263b;padding:16px;margin:12px 0}h1{font-size:23px}h2{font-size:17px}p,small{color:#afc0d7}input,select,button{background:#253652;border:1px solid #445978;color:#fff;border-radius:9px;padding:10px;font:inherit}button{background:#3db7a4;color:#071b21;font-weight:800;cursor:pointer}button:disabled{opacity:.5}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:end}.field{display:flex;flex-direction:column;gap:6px}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:9px 7px;border-bottom:1px solid #374861;text-align:left;white-space:nowrap}.scroll{overflow-x:auto}#status{margin:10px 0}a{color:#79dfd0}</style></head><body><main>
<h1>CHART360 MINI V0.5</h1><p>WAIT. VERIFY. THEN ACT. · 4H → 15M → 1M 구조전환 <b>별도 실험판</b></p>
<section class="panel"><h2>빗썸 실제 캔들 · 간단 백테스트</h2>
<div class="row"><label class="field">시장<select id="market"><option>KRW-XRP</option><option>KRW-ETH</option><option>KRW-NEAR</option><option>KRW-SOL</option><option>KRW-DOGE</option><option value="ALL">5종목 한 번에 비교</option></select></label>
<label class="field">검증 기간<select id="hours"><option value="6">6시간</option><option value="12">12시간</option><option value="24">24시간</option><option value="72" selected>최근 3일</option><option value="168">최근 7일</option></select></label>
<label class="field">편도 수수료<select id="fee"><option value=".04">0.04%</option><option value=".25">0.25%</option></select></label>
<button id="run">실제 캔들 백테스트</button></div><p><small>다일자 검색은 완료봉 기준 연속 72시간/168시간이야. 마지막 60분은 가상 청산 확인 전용으로 확보하고 매수 신호는 만들지 않아. 5종목 선택 시 3·7일은 종목별 요청으로 나누어 같은 종료 시각에 맞춰 비교해.</small></p>
<small><b>A형:</b> 4H LH+LL 필수. <b>B형:</b> 4H 참고만, 1분 종가 돌파 뒤 바로 다음 시가 진입. <b>C형:</b> B형 돌파 뒤 추가 완료 1분봉이 양봉이며 돌파봉 종가보다 높은지 확인 후 그 다음 시가 진입. 공통: 15M CHoCH→눌림→1M 돌파, 구조적 손절·1.5R·60분·같은 수수료. 가상 연구 신호, 자동 주문 없음.</small>
<div id="status">검사 전</div></section>
<section class="panel"><h2>V0.5 · A/B/C 비교 및 단계별 진단 <small>진입 0건도 기록</small></h2>
<div id="ab-summary" class="scroll">A: 4H 필수 / B: 바로 진입 / C: 1분 확인 후 진입</div>
<div id="diagnostic">4H → 15M → 1M에서 어느 단계가 막혔는지 검색 후 표시돼.</div>
<div class="row" style="margin:10px 0"><button id="diagnostic-csv" disabled>A/B/C 진단 CSV</button><button id="csv" disabled>A/B/C 가상 진입 CSV</button><button id="daily-csv" disabled>날짜별 손익·분봉 CSV</button><button id="pairs-csv" disabled>B/C 동일 돌파 비교 CSV</button></div>
</section>
<section class="panel"><h2>날짜별 성과 · KST <small>처음·마지막 날짜는 부분일 수 있어</small></h2><div id="daily-view">검색 후 표시돼.</div></section>
<section class="panel"><h2>같은 돌파 B/C 짝 비교 <small>미확정 제외</small></h2><div id="pair-view">검색 후 표시돼.</div></section>
<section class="panel"><h2>가상 진입 기록 <small>실거래 아님</small></h2><div id="results">검색 버튼을 눌러줘.</div></section>
<p>기존 BTC MTF V1.4.9는 수정하지 않은 별도 경로야. <a href="/">기존 스캐너로 이동</a></p>
</main><script>(()=>{
const $=id=>document.getElementById(id),escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let last=null;
const marketChoice=()=>$('market').value==='ALL'?['KRW-XRP','KRW-ETH','KRW-NEAR','KRW-SOL','KRW-DOGE']:[$('market').value];

function combineRuns(parts){
 const root={...parts[0],markets:parts.flatMap(p=>p.markets),
   daily_by_market:parts.flatMap(p=>p.daily_by_market||[]),
   pair_rows:parts.flatMap(p=>p.pair_rows||[])};
 const totalFor=k=>{
   const trades=root.markets.flatMap(m=>m.variants?.[k]?.trades||[]);
   const resolved=trades.filter(t=>t.status==='RESOLVED').map(t=>t.net_pct);
   return {signals:trades.length,resolved:resolved.length,
      unknown:trades.length-resolved.length,
      positive:resolved.filter(x=>x>0).length,
      nonpositive:resolved.filter(x=>x<=0).length,
      mean_net_pct:resolved.length?+(resolved.reduce((a,b)=>a+b,0)/resolved.length).toFixed(3):null};
 };
 root.comparison={A:totalFor('A'),B:totalFor('B'),C:totalFor('C'),
   difference_B_minus_A:0,difference_C_minus_B:0};
 root.comparison.difference_B_minus_A=root.comparison.B.signals-root.comparison.A.signals;
 root.comparison.difference_C_minus_B=root.comparison.C.signals-root.comparison.B.signals;
 root.total={...root.comparison.A,negative_or_zero:root.comparison.A.nonpositive};
 root.failed_markets=root.markets.filter(m=>m.error).map(m=>m.market);
 const map=new Map();
 for(const r of root.daily_by_market){
   const key=r.date_kst+'|'+r.variant;
   let g=map.get(key);
   if(!g){g={date_kst:r.date_kst,variant:r.variant,markets:0,error_markets:0,
       partial_day:false,minutes_expected:0,minutes_observed:0,
       signals:0,resolved:0,positive:0,nonpositive:0,unknown:0,
       tp:0,stop:0,time_exit:0,net_sum_pct:0};map.set(key,g)}
   g.markets++;g.error_markets+=r.data_status==='API_ERROR'?1:0;
   g.partial_day ||= !!r.partial_day;
   for(const n of ['minutes_expected','minutes_observed','signals','resolved',
       'positive','nonpositive','unknown','tp','stop','time_exit'])g[n]+=Number(r[n]||0);
   g.net_sum_pct+=Number(r.net_sum_pct||0);
 }
 root.daily_overall=Array.from(map.values()).sort((a,b)=>
   a.date_kst.localeCompare(b.date_kst)||a.variant.localeCompare(b.variant)).map(g=>{
   g.minutes_missing=g.minutes_expected-g.minutes_observed;
   g.coverage_pct=g.minutes_expected?+(100*g.minutes_observed/g.minutes_expected).toFixed(2):null;
   g.mean_net_pct=g.resolved?+(g.net_sum_pct/g.resolved).toFixed(4):null;
   g.net_sum_pct=g.resolved?+g.net_sum_pct.toFixed(4):null;
   return g;
 });
 const paired=root.pair_rows.filter(p=>p.match_status==='PAIRED');
 const known=paired.filter(p=>p.C_minus_B_pctpoints!=null);
 root.pair_summary={paired:paired.length,resolved_pairs:known.length,
    unresolved_pairs:paired.length-known.length,
    B_only:root.pair_rows.filter(p=>p.match_status==='B_ONLY').length,
    C_only:root.pair_rows.filter(p=>p.match_status==='C_ONLY').length,
    C_better:known.filter(p=>p.C_minus_B_pctpoints>0).length,
    C_worse:known.filter(p=>p.C_minus_B_pctpoints<0).length,
    same:known.filter(p=>p.C_minus_B_pctpoints===0).length,
    mean_C_minus_B_pctpoints:known.length?
       +(known.reduce((a,p)=>a+p.C_minus_B_pctpoints,0)/known.length).toFixed(4):null};
 return root;
}
async function fetchStudy(markets,hours,fee,asof){
 const res=await fetch('/api/chart360',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({markets,hours,fee_each:fee,asof_kst:asof||null})});
 const data=await res.json();
 if(!res.ok||!data.ok)throw Error(data.error||'API 오류');
 return data.result;
}
async function loadStudy(){
 const markets=marketChoice(),hours=Number($('hours').value),fee=Number($('fee').value);
 // Single-market requests keep long 7day API queries below Render's 120s
 // timeout. The FIRST server-provided KST cutoff is reused unchanged.
 if(markets.length===1||hours<=24)return await fetchStudy(markets,hours,fee,null);
 const parts=[];
 let cutoff=null;
 for(let i=0;i<markets.length;i++){
   $('status').textContent='3·7일 분봉 조회 중: '+(i+1)+'/'+markets.length+' · '+markets[i];
   const part=await fetchStudy([markets[i]],hours,fee,cutoff);
   if(!cutoff)cutoff=part.asof_kst;
   parts.push(part);
 }
 return combineRuns(parts);
}

$('run').onclick=async()=>{const b=$('run');b.disabled=true;last=null;
 for(const id of ['csv','diagnostic-csv','daily-csv','pairs-csv'])$(id).disabled=true;
 $('status').textContent='동일 빗썸 완료봉으로 3·7일 백테스트 분석 중…';
try{last=await loadStudy();
 const compare=last.comparison||{},markets=last.markets||[];
 const rows=['A','B','C'].map(k=>({key:k,x:compare[k]||{}}));
 $('status').textContent='완료 · '+last.scan_begin_kst+' ~ '+last.scan_end_kst+' KST · '+markets.length+'종목 · 누락/실패 '+(last.failed_markets||[]).length+'종목';
 if((last.failed_markets||[]).length)$('status').textContent+=' · 데이터 조회 실패: '+last.failed_markets.join(', ');
 $('ab-summary').innerHTML='<p><b>A: 4시간 필수 / B: 1분 돌파 직후 / C: 1분 추가 양봉 종가 확인 후</b> · 셋 모두 연구용 가상값이고 거래를 합산하면 안 돼. <b>B와 C의 진입시각·체결가가 다르다</b>는 점도 주의해 줘.</p>'
  +'<table><thead><tr><th>방식</th><th>진입</th><th>확정</th><th>UNKNOWN</th><th>양수</th><th>비양수</th><th>확정분 평균 순손익</th></tr></thead><tbody>'
  +rows.map(v=>'<tr><td>'+v.key+(v.key==='A'?' · 4H 필수':v.key==='C'?' · 추가 확인':' · 즉시')+'</td><td>'+(v.x.signals??0)+'</td><td>'+(v.x.resolved??0)+'</td><td>'+(v.x.unknown??0)+'</td><td>'+(v.x.positive??0)+'</td><td>'+(v.x.nonpositive??0)+'</td><td>'+escape(v.x.mean_net_pct??'미산정')+(v.x.mean_net_pct==null?'':'%')+'</td></tr>').join('')+'</tbody></table>';
 const variantRows=markets.flatMap(x=>['A','B','C'].map(k=>({market:x.market,key:k,
   v:x.variants?.[k]||{},a:x.data_audit||{},error:x.error||''})));
 $('diagnostic').innerHTML='<p>A형은 4H 필수, B형은 4H 제외, C형은 B형보다 1분 늦은 <b>완료봉 상승 확인</b>이 핵심 차이야. 진입 0건도 기록해.</p>'
  +'<div class="scroll"><table><thead><tr><th>코인</th><th>A/B/C</th><th>15M CHoCH (검사구간)</th><th>4H 통과</th><th>눌림</th><th>1M 돌파</th><th>C 확인 실패</th><th>진입</th><th>주요 사유</th><th>1M 확보</th></tr></thead><tbody>'
  +variantRows.map(x=>{const d=x.v.diagnostic||{};return '<tr><td>'+escape(x.market)+'</td><td>'+x.key+'</td><td>'+(d.m15_events_in_test??'—')+'</td><td>'+(d.h4_event_pass??'—')+'</td><td>'+(d.pullback_event??'—')+'</td><td>'+(d.m1_breakout_event??'—')+'</td><td>'+(d.confirm_reject_event??'—')+'</td><td>'+(d.entry_event??0)+'</td><td>'+escape(d.primary_blocker_text||x.error||'진단 불가')+'</td><td>'+escape(x.a.minutes_observed??'—')+'/'+escape(x.a.minutes_requested??'—')+'</td></tr>'}).join('')+'</tbody></table></div>'
  +'<details><summary>15분 CHoCH 개별 이벤트 탈락 사유 확인</summary>'
  +variantRows.map(x=>{const d=x.v.diagnostic||{};return '<p><b>'+escape(x.market)+' '+x.key+'</b> · 15M 전체 이력 '+(d.m15_events_all_history??'—')+' · 검사 구간 '+(d.m15_events_in_test??'—')+' · 90분 겹침 '+(d.m15_events_overlapping_test??'—')+' · 최근 실측 1M 기준 H4 참고 '+escape(d.h4_snapshot_code||'—')+'</p>'
  +'<div class="scroll"><table><tr><th>CHoCH 마감</th><th>4H LH</th><th>4H LL</th><th>눌림</th><th>1분 돌파</th><th>탈락 사유</th></tr>'
  +(d.event_details||[]).map(e=>'<tr><td>'+escape(e.choch_kst)+'</td><td>'+(e.h4_lower_high?'✓':'—')+'</td><td>'+(e.h4_lower_low?'✓':'—')+'</td><td>'+(e.pullback?'✓':'—')+'</td><td>'+(e.m1_breakout?'✓':'—')+'</td><td>'+escape(e.reason_text)+'</td></tr>').join('')+'</table></div>';}).join('')+'</details>'
  +'<p>15M 전체 이력의 CHoCH와 검사구간 CHoCH는 다르며, 단계별 이벤트 수는 중복 집계될 수 있어. 1분봉 공백을 0거래로 채우지 않아.</p>';
 const trades=markets.flatMap(x=>['A','B','C'].flatMap(k=>(x.variants?.[k]?.trades||[]).map(t=>({...t,variant:k}))));
 $('results').innerHTML=trades.length?
 '<div class="scroll"><table><tr><th>코인</th><th>방식</th><th>시간</th><th>점수</th><th>진입</th><th>손절</th><th>결과</th><th>순손익%</th></tr>'
 +trades.map(t=>'<tr><td>'+escape(t.market)+'</td><td>'+escape(t.variant)+'</td><td>'+escape(t.entry_kst)+'</td><td>'+escape(t.score)+'/7</td><td>'+escape(t.entry)+'</td><td>'+escape(t.stop)+'</td><td>'+escape(t.exit_reason)+'</td><td>'+escape(t.net_pct??'UNKNOWN')+'</td></tr>').join('')+'</table></div>':
 '<p>이번 검사에서는 A/B/C 모두 가상 진입 0건. 위 비교 표와 진단 CSV에서 단계별 사유를 확인해 줘.</p>';

 const daily=last.daily_overall||[];
 $('daily-view').innerHTML='<p>일별 A/B/C 진입 시각을 KST 날짜로 분류해. 날짜 경계는 부분일일 수 있고, UNKNOWN은 손실에 포함하지 않아.</p>'
   +'<div class="scroll"><table><tr><th>날짜</th><th>방식</th><th>구간</th><th>진입</th><th>수익</th><th>비양수</th><th>UNKNOWN</th><th>평균 순손익</th><th>1m 확보율</th><th>오류시장</th></tr>'
   +daily.map(x=>'<tr><td>'+escape(x.date_kst)+'</td><td>'+escape(x.variant)+'</td><td>'+(x.partial_day?'부분':'전체')+'</td><td>'+x.signals+'</td><td>'+x.positive+'</td><td>'+x.nonpositive+'</td><td>'+x.unknown+'</td><td>'+escape(x.mean_net_pct??'미산정')+(x.mean_net_pct==null?'':'%')+'</td><td>'+escape(x.coverage_pct??'—')+'%</td><td>'+x.error_markets+'</td></tr>').join('')+'</table></div>';
 const pairs=last.pair_rows||[],summary=last.pair_summary||{};
 $('pair-view').innerHTML='<p>같은 1분봉 돌파 시각을 기준으로 B/C를 매칭했어. C는 한 봉 늦게 진입하므로 가상 체결가·손익이 달라. 일치하지 않는 돌파는 억지로 짝짓지 않아.</p>'
  +'<p>같은 돌파 '+(summary.paired??0)+' · 양쪽 확정 '+(summary.resolved_pairs??0)+' · C 개선 '+(summary.C_better??0)+' · C 악화 '+(summary.C_worse??0)+' · 동일 '+(summary.same??0)+' · 한쪽만 진입 B '+(summary.B_only??0)+' / C '+(summary.C_only??0)+' · 확정분 평균 차이 '+escape(summary.mean_C_minus_B_pctpoints??'미산정')+'%p</p>'
  +'<div class="scroll"><table><tr><th>종목</th><th>돌파 KST</th><th>짝 유형</th><th>B 순손익</th><th>C 순손익</th><th>C-B (%p)</th></tr>'
  +pairs.slice(0,100).map(p=>'<tr><td>'+escape(p.market)+'</td><td>'+escape(p.breakout_kst)+'</td><td>'+escape(p.match_status)+'</td><td>'+escape(p.B_net_pct??'UNKNOWN')+'</td><td>'+escape(p.C_net_pct??'UNKNOWN')+'</td><td>'+escape(p.C_minus_B_pctpoints??'비교불가')+'</td></tr>').join('')+'</table></div>'
  +(pairs.length>100?'<p>화면에는 상위 100건만 보여줘. 전체 결과는 CSV로 저장할 수 있어.</p>':'');
 $('daily-csv').disabled=false;
 $('pairs-csv').disabled=false;
 $('diagnostic-csv').disabled=false;
 $('csv').disabled=trades.length===0;
}catch(e){$('status').textContent='오류: '+String(e.message||e);}
finally{b.disabled=false;}
};
function downloadCsv(headers,rows,kind){
 const q=s=>'"'+String(s??'').replace(/"/g,'""')+'"';
 const csv=String.fromCharCode(0xFEFF)+[headers,...rows].map(row=>row.map(q).join(',')).join(String.fromCharCode(10));
 const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));
 const a=document.createElement('a');a.href=url;a.download='CHART360_MINI_V0_5_'+kind+'_'+last.asof_kst.replace(/[^0-9]/g,'')+'.csv';
 document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000);
}
$('diagnostic-csv').onclick=()=>{
 if(!last)return;
 const headers=['version','market','variant','h4_gate_required','scan_begin_kst','asof_kst','lookback_hours','fee_each_pct','minute_bars_observed','minute_bars_requested','minute_bars_missing','minute_coverage_pct','m15_bars','h4_bars','h4_snapshot_code','h4_snapshot_lower_high','h4_snapshot_lower_low','m15_events_all_history','m15_events_in_test','m15_events_overlapping_test','h4_event_pass','h4_missing_event','h4_failed_event','pullback_event','m1_breakout_event','entry_event','gap_event','risk_reject_event','confirm_reject_event','confirm_gap_event','confirmation_bars','primary_blocker_code','primary_blocker_text','reason_counts_json','event_details_json','total_trades','resolved','unknown','error'];
 const lines=last.markets.flatMap(m=>['A','B','C'].map(k=>{
  const v=m.variants?.[k]||{},d=v.diagnostic||{},a=m.data_audit||{},sm=v.summary||{};
  return [last.version,m.market,k,k==='A'?'TRUE':'FALSE',
   d.scan_begin_kst||last.scan_begin_kst,last.asof_kst,last.lookback_hours,last.fee_each_pct,
   a.minutes_observed??'',a.minutes_requested??'',a.minutes_missing??'',a.coverage_pct??'',a.m15_bars??'',a.h4_bars??'',
   d.h4_snapshot_code||'',d.h4_snapshot_lower_high??'',d.h4_snapshot_lower_low??'',
   d.m15_events_all_history??'',d.m15_events_in_test??'',d.m15_events_overlapping_test??'',
   d.h4_event_pass??'',d.h4_missing_event??'',d.h4_failed_event??'',d.pullback_event??'',
   d.m1_breakout_event??'',d.entry_event??'',d.gap_event??'',d.risk_reject_event??'',
   d.confirm_reject_event??'',d.confirm_gap_event??'',d.confirmation_bars??0,
   d.primary_blocker_code||'',d.primary_blocker_text||'',
   JSON.stringify(d.reason_counts||{}),JSON.stringify(d.event_details||[]),
   sm.signals??0,sm.resolved??0,sm.unknown??0,m.error||''];
 }));
 downloadCsv(headers,lines,'ABC_DIAGNOSTIC');
};
$('csv').onclick=()=>{
 if(!last)return;
 const fields=['market','variant','signal','signal_kst','entry_kst','entry','stop','target','risk_pct','score','score_total','volume_ratio','breakout_kst','entry_delay_minutes','breakout_body_pct','breakout_upper_wick_pct','breakout_range_pct','prior20_1m_contiguous','minutes_after_choch','confirm_bullish_continuation','m15_choch_kst','status','net_pct','exit_reason','hold_minutes'];
 const rows=last.markets.flatMap(m=>['A','B','C'].flatMap(k=>(m.variants?.[k]?.trades||[]).map(t=>fields.map(field=>field==='variant'?k:t[field]??''))));
 if(!rows.length)return;
 downloadCsv(fields,rows,'ABC_TRADES');
};

$('daily-csv').onclick=()=>{
 if(!last)return;
 const fields=['date_kst','market','variant','partial_day','window_start_kst','window_end_kst',
   'minutes_expected','minutes_observed','minutes_missing','coverage_pct','data_status',
   'signals','resolved','positive','nonpositive','unknown','tp','stop','time_exit',
   'net_sum_pct','mean_net_pct','unknown_reasons_json'];
 const rows=(last.daily_by_market||[]).map(d=>fields.map(k=>
   k==='unknown_reasons_json'?JSON.stringify(d.unknown_reasons||{}):d[k]??''));
 downloadCsv(fields,rows,'DAILY_MARKET');
};
$('pairs-csv').onclick=()=>{
 if(!last)return;
 const fields=['market','breakout_kst','match_status','B_entry_kst','C_entry_kst',
 'B_exit_reason','C_exit_reason','B_net_pct','C_net_pct',
 'C_minus_B_pctpoints','comparison'];
 const rows=(last.pair_rows||[]).map(p=>fields.map(k=>p[k]??''));
 downloadCsv(fields,rows,'BC_PAIRED_BREAKOUTS');
};
})();</script></body></html>'''
