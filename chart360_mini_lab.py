"""CHARTWALA360 inspired MINI structure research V0.3 — read-only.

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
VERSION = "CHART360 MINI V0.3"
MAX_MINUTE_PAGES = 8
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


def historical_1m(market, end, hours=12, getter=_minute_data):
    """Paginate *actual* exchange minutes: max 200 per API call, no fills."""
    if not (2 <= hours <= 24):
        raise ValueError("2~24 hours required")
    begin = end - timedelta(hours=hours, minutes=30)
    seen, cursor = {}, end + timedelta(minutes=1)
    pages = 0
    while pages < MAX_MINUTE_PAGES:
        rows = getter(market, 1, cursor, 200)
        pages += 1
        if not rows:
            break
        for r in rows:
            if begin <= r["t"] and r["t"] + timedelta(minutes=1) <= end:
                seen[r["t"]] = r
        oldest = min(r["t"] for r in rows)
        if oldest <= begin or oldest >= cursor:
            break
        cursor = oldest  # Bithumb 'to' is exclusive
    bars = sorted(seen.values(), key=lambda x:x["t"])
    return bars, {"pages":pages, "minutes_observed":len(bars),
                  "minutes_requested":int(hours*60+30),
                  "incomplete":len(bars) < hours*60 + 15}


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


def replay(market, minute, m15, h4, fee_each=.04, max_trades=8, scan_begin=None,
           require_h4=True):
    """A: required 4H bearish LH+LL; B: 4H information only, same entry rules."""
    """All indicators and references use data strictly available at signal."""
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
    diagnostic_end = minute[-1]["t"]+timedelta(minutes=1) if minute else None
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
            "entries":0,
        } for ev in relevant
    }
    stats={"m15_choch_events":len(events),"h4_context_ok":0,
           "pullbacks":0,"m1_breakouts":0,"entries":0,
           "reject_risk":0,"reject_gap":0}
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
        sig=("BUY_TEST" if score>=6 else "WATCH_TEST") if require_h4 else "B_ENTRY_TEST"
        exit=_simulate(minute,i+1,entry,stop,fee_each)
        trades.append({"market":market,"signal":sig,
                       "signal_kst":at.strftime("%Y-%m-%d %H:%M"),
                       "entry_kst":nextbar["t"].strftime("%Y-%m-%d %H:%M"),
                       "entry":round(entry,8),"stop":round(stop,8),
                       "target":round(entry+1.5*(entry-stop),8),
                       "risk_pct":round(risk,3),
                       "score":score,"score_total":7,
                       "volume_ratio":round(vratio,2),
                       "m15_choch_kst":ev["end"].strftime("%Y-%m-%d %H:%M"),
                       **exit})
        stats["entries"]+=1
        if trace is not None: trace["entries"]+=1
        cooldown_until=at+timedelta(minutes=65)
        if len(trades)>=max_trades:
            break
    # Mutually exclusive final reason per relevant M15 event. Signals
    # and exits above are not changed; the new audit is observation only.
    reasons = {
        "ENTRY_TEST": "가상 진입 기록",
        "M1_BREAKOUT_RISK_REJECT": "1분봉 돌파 후 손절 거리 기준 탈락",
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
        "variant":"A" if require_h4 else "B",
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
        "reason_counts":reason_counts,
        "primary_blocker_code":blocker,
        "primary_blocker_text":reasons[blocker],
        "event_details":event_rows,
        "note":"CHoCH 45시간 전체 기록과 이번 검사시간 내 이벤트는 별개. 이벤트별 단계 통과 집계이며 실제 주문 아님.",
    }
    resolved=[t["net_pct"] for t in trades if t["status"]=="RESOLVED"]
    wins=sum(x>0 for x in resolved)
    return {
        "market":market,"variant":"A" if require_h4 else "B",
        "h4_gate_required":require_h4,
        "trades":trades,"stats":stats,"diagnostic":diagnostic,
        "summary":{"signals":len(trades),"resolved":len(resolved),
                   "unknown":len(trades)-len(resolved),"positive":wins,
                   "nonpositive":len(resolved)-wins,
                   "mean_net_pct":round(sum(resolved)/len(resolved),3) if resolved else None},
        "rules":"4H LH+LL (last 6 vs previous 6); M15 CHoCH close above prior8 high following lower lows; post-break retracement; M1 five-bar high break by close & bullish body; next M1 open; pullback stop -0.1%; TP 1.5R / 60m; fee each side",
    }


def run_backtest(markets=("KRW-XRP",), hours=12, fee_each=.04,
                 now=None, getter=_minute_data):
    if not isinstance(markets,(list,tuple)) or not 1<=len(markets)<=5:
        raise ValueError("choose 1–5 markets")
    if any(m not in MARKETS for m in markets):
        raise ValueError("unsupported Bithumb KRW symbol")
    hours=int(hours)
    if hours not in (6,12,24):
        raise ValueError("hours: 6, 12 or 24")
    if fee_each not in (.04,.25):
        raise ValueError("fee each: 0.04 or 0.25")
    # 1h final margin makes every potential 60m exit verifiable by now.
    end=(now or datetime.now(KST)).astimezone(KST).replace(second=0,microsecond=0)-timedelta(minutes=2)
    output=[]
    for market in markets:
        try:
            one,audit=historical_1m(market,end,hours,getter)
            m15=[r for r in getter(market,15,end+timedelta(minutes=1),180)
                 if r["t"]+timedelta(minutes=15)<=end]
            h4=[r for r in getter(market,240,end+timedelta(minutes=1),100)
                if r["t"]+timedelta(hours=4)<=end]
            begin=end-timedelta(hours=hours)
            a=replay(market,one,m15,h4,fee_each,scan_begin=begin,require_h4=True)
            b=replay(market,one,m15,h4,fee_each,scan_begin=begin,require_h4=False)
            audit={**audit,"m15_bars":len(m15),"h4_bars":len(h4),
                   "first_1m_kst":one[0]["t"].strftime("%Y-%m-%d %H:%M") if one else None,
                   "last_1m_kst":one[-1]["t"].strftime("%Y-%m-%d %H:%M") if one else None}
            # A remains at the legacy top level for API compatibility.
            result={**a,"data_audit":audit,"variants":{"A":a,"B":b}}
            output.append(result)
        except Exception as exc:
            message=str(exc)[:200]
            base_d={"version":VERSION,"market":market,
                    "primary_blocker_code":"DATA_API_ERROR",
                    "primary_blocker_text":"빗썸 데이터 조회 실패",
                    "reason_counts":{"DATA_API_ERROR":1},"event_details":[]}
            base_s={"signals":0,"resolved":0,"unknown":0,"positive":0,
                    "nonpositive":0,"mean_net_pct":None}
            variants={variant:{"market":market,"variant":variant,
                               "h4_gate_required":variant=="A","trades":[],
                               "diagnostic":{**base_d,"variant":variant,
                                             "h4_gate_required":variant=="A"},
                               "summary":base_s,"stats":{}}
                      for variant in ("A","B")}
            output.append({"market":market,"error":message,"trades":[],
                           "variants":variants,"diagnostic":base_d,
                           "stats":{},"summary":base_s})
    alltrades=[t for r in output for t in r.get("trades",[])]
    known=[t["net_pct"] for t in alltrades if t["status"]=="RESOLVED"]
    def variant_total(variant):
        trades=[t for r in output
                for t in r["variants"][variant]["trades"]]
        resolved=[t["net_pct"] for t in trades if t["status"]=="RESOLVED"]
        return {"signals":len(trades),"resolved":len(resolved),
                "unknown":len(trades)-len(resolved),
                "positive":sum(x>0 for x in resolved),
                "nonpositive":sum(x<=0 for x in resolved),
                "mean_net_pct":round(sum(resolved)/len(resolved),3) if resolved else None}
    comparison={"A":variant_total("A"),"B":variant_total("B"),
                "extra_B_signals":sum(len(r["variants"]["B"]["trades"])-
                                       len(r["variants"]["A"]["trades"]) for r in output),
                "reference":"동일 캔들·수수료·청산 규칙, 4H LH+LL 필수 여부만 비교"}

    return {
        "version":VERSION,"mode":"historical_public_candles_only",
        "asof_kst":end.strftime("%Y-%m-%d %H:%M"),"lookback_hours":hours,
        "scan_begin_kst":(end-timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M"),
        "fee_each_pct":fee_each,"markets":output,"comparison":comparison,
        "total":{"signals":len(alltrades),"resolved":len(known),
                 "unknown":len(alltrades)-len(known),
                 "positive":sum(x>0 for x in known),
                 "negative_or_zero":sum(x<=0 for x in known),
                 "mean_net_pct":round(sum(known)/len(known),3) if known else None},
        "warnings":["실제 매매 아님, 1분봉 종가 확인 후 다음 1분봉 시가 체결 가정",
                    "거래소 1분봉 공백은 채우지 않으며 미확정은 손익 계산 제외",
                    "A: 4H LH+LL 필수, B: 4H 참고만. B는 종가·눌림·손절·수수료 기준 동일",
                    "A와 B의 같은 거래는 중복 표본이므로 합산 금지. BUY_TEST/B_ENTRY_TEST는 실전 주문 지시 아님",
                    "초간단 연구판: 4H/15M/1M 구조조건의 수치적 근사, FVG/오더블록 미구현",
                    "슬리피지와 유동성 충격은 0으로 가정, 수익성 입증 아님"],
    }


HTML=r'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>CHART360 MINI V0.3 · 구조전환 연구실</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}body{background:#101827;color:#eaf1ff;font:15px system-ui;margin:0}main{max-width:900px;margin:auto;padding:20px 14px}.panel{border:1px solid #30405a;border-radius:15px;background:#1a263b;padding:16px;margin:12px 0}h1{font-size:23px}h2{font-size:17px}p,small{color:#afc0d7}input,select,button{background:#253652;border:1px solid #445978;color:#fff;border-radius:9px;padding:10px;font:inherit}button{background:#3db7a4;color:#071b21;font-weight:800;cursor:pointer}button:disabled{opacity:.5}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:end}.field{display:flex;flex-direction:column;gap:6px}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:9px 7px;border-bottom:1px solid #374861;text-align:left;white-space:nowrap}.scroll{overflow-x:auto}#status{margin:10px 0}a{color:#79dfd0}</style></head><body><main>
<h1>CHART360 MINI V0.3</h1><p>WAIT. VERIFY. THEN ACT. · 4H → 15M → 1M 구조전환 <b>별도 실험판</b></p>
<section class="panel"><h2>빗썸 실제 캔들 · 간단 백테스트</h2>
<div class="row"><label class="field">시장<select id="market"><option>KRW-XRP</option><option>KRW-ETH</option><option>KRW-NEAR</option><option>KRW-SOL</option><option>KRW-DOGE</option></select></label>
<label class="field">과거 구간<select id="hours"><option value="6">6시간</option><option value="12" selected>12시간</option><option value="24">24시간</option></select></label>
<label class="field">편도 수수료<select id="fee"><option value=".04">0.04%</option><option value=".25">0.25%</option></select></label>
<button id="run">실제 캔들 백테스트</button></div>
<small>4H 하락 LH/LL · 15M 상승 CHoCH · 1M 눌림 후 고점 돌파 · 다음 봉 시가로 진입 가정 · 1.5R 익절, 구조적 손절, 60분 한도.</small>
<div id="status">검사 전</div></section>
<section class="panel"><h2>V0.3 · 단계별 진단 <small>진입 0건도 기록</small></h2>
<div id="diagnostic">4H → 15M → 1M에서 어느 단계가 막혔는지 검색 후 표시돼.</div>
<div class="row" style="margin:10px 0"><button id="diagnostic-csv" disabled>코인별 진단 CSV · 0건도 저장</button><button id="csv" disabled>가상 진입 CSV · 신호 있을 때만</button></div>
</section>
<section class="panel"><h2>연구 결과 <small>실거래 아님</small></h2><div id="results">검색 버튼을 눌러줘.</div></section>
<p>기존 BTC MTF V1.4.9는 수정하지 않은 별도 경로야. <a href="/">기존 스캐너로 이동</a></p>
</main><script>(()=>{
const $=id=>document.getElementById(id),escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let last=null;
$('run').onclick=async()=>{const b=$('run');b.disabled=true;last=null;$('csv').disabled=true;$('diagnostic-csv').disabled=true;$('status').textContent='실제 빗썸 4H·15M·1M 과거 봉 조회 및 사후 검사 중…';
try{const res=await fetch('/api/chart360',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({markets:[$('market').value],hours:Number($('hours').value),fee_each:Number($('fee').value)})});
 const d=await res.json();if(!res.ok||!d.ok)throw Error(d.error||'API 오류');last=d.result;
 const t=last.total,detail=last.markets[0];
 $('status').textContent='완료 · '+last.asof_kst+' KST · '+last.lookback_hours+'시간 · '+escape(detail.market)+' · 실측 1분봉 '+(detail.data_audit?.minutes_observed??'확인 불가')+'개';
 const diagnosis=detail.diagnostic||{},quality=detail.data_audit||{};
 const stageCols=[['15분 CHoCH · 검사구간',diagnosis.m15_events_in_test],['15분 CHoCH · 후속 90분 겹침',diagnosis.m15_events_overlapping_test],['4H LH+LL 충족 이벤트',diagnosis.h4_event_pass],['4H 자료 부족 이벤트',diagnosis.h4_missing_event],['4H 구조 불충족 이벤트',diagnosis.h4_failed_event],['15분 이후 눌림 이벤트',diagnosis.pullback_event],['1분 돌파 이벤트',diagnosis.m1_breakout_event],['가상 진입 이벤트',diagnosis.entry_event]];
 $('diagnostic').innerHTML='<p><b>우선 확인: '+escape(diagnosis.primary_blocker_text||'진단 불가')+'</b> · '+escape(diagnosis.primary_blocker_code||'NO_DATA')+'</p>'
   +'<div class="scroll"><table><thead><tr><th>검사 단계</th><th>발견 이벤트 수</th></tr></thead><tbody>'
   +stageCols.map(([label,num])=>'<tr><td>'+escape(label)+'</td><td>'+(num??'—')+'</td></tr>').join('')+'</tbody></table></div>'
   +'<p>마지막 실측 1분봉 기준 4H 상태: '+escape(diagnosis.h4_snapshot_code||'확인 불가')+' (사후 진단 전용) · 1분봉 관측 '+(quality.minutes_observed??'—')+'/'+(quality.minutes_requested??'—')+' · 15분봉 '+(quality.m15_bars??'—')+' · 4시간봉 '+(quality.h4_bars??'—')+' · 캔들 공백은 0거래로 처리하지 않아.</p>'
   +(detail.error?'<p>API 오류: '+escape(detail.error)+'</p>':'')
   +'<details><summary>15분 CHoCH 발생별 탈락 사유 자세히 보기</summary><div class="scroll"><table><tr><th>CHoCH 마감</th><th>4H LH</th><th>4H LL</th><th>눌림</th><th>1M 돌파</th><th>결과</th></tr>'
   +(diagnosis.event_details||[]).map(x=>'<tr><td>'+escape(x.choch_kst)+'</td><td>'+(x.h4_lower_high?'✓':'—')+'</td><td>'+(x.h4_lower_low?'✓':'—')+'</td><td>'+(x.pullback?'✓':'—')+'</td><td>'+(x.m1_breakout?'✓':'—')+'</td><td>'+escape(x.reason_text)+'</td></tr>').join('')
   +'</table></div></details>'
   +'<p>전체 15분봉 이력의 CHoCH '+(diagnosis.m15_events_all_history??0)+'회와 이번 검사시간 CHoCH는 별개야. 4H/눌림 수치는 이벤트별 관찰이며 합산할 수 없어.</p>';
 $('results').innerHTML='<p>가상 진입 '+t.signals+' · 확정 '+t.resolved+' · UNKNOWN '+t.unknown+' · 양수 '+t.positive+' · 비양수 '+t.negative_or_zero+' · 확정 가상 평균 '+(t.mean_net_pct??'—')+'%</p>'
 +(t.signals?'<div class="scroll"><table><tr><th>시간</th><th>점수</th><th>진입</th><th>손절</th><th>결과</th><th>순손익(%)</th></tr>'
 +(detail.trades||[]).map(x=>'<tr><td>'+escape(x.entry_kst)+'</td><td>'+x.score+'/7</td><td>'+x.entry+'</td><td>'+x.stop+'</td><td>'+escape(x.exit_reason)+'</td><td>'+escape(x.net_pct??'UNKNOWN')+'</td></tr>').join('')+'</table></div>':
 '<p>진입 0건: 데이터가 없는 게 아니라 단계별 진단을 위에서 확인해 줘.</p>')
 +'<p>WATCH/BUY TEST는 실거래가 아니고 미확정 수익률은 계산하지 않아.</p>';
 $('diagnostic-csv').disabled=false;
 $('csv').disabled=!(t.signals>0);
}catch(e){$('status').textContent='오류: '+String(e.message||e);}
finally{b.disabled=false;}
};
function downloadCsv(headers,rows,kind){
 const q=s=>'"'+String(s??'').replace(/"/g,'""')+'"';
 const csv=String.fromCharCode(0xFEFF)+[headers,...rows].map(row=>row.map(q).join(',')).join(String.fromCharCode(10));
 const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));
 const a=document.createElement('a');a.href=url;a.download='CHART360_MINI_V0_3_'+kind+'_'+last.asof_kst.replace(/[^0-9]/g,'')+'.csv';
 document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000);
}
$('diagnostic-csv').onclick=()=>{
 if(!last)return;
 const headers=['version','market','scan_begin_kst','asof_kst','lookback_hours','fee_each_pct','minute_bars_observed','minute_bars_requested','m15_bars','h4_bars','h4_snapshot_code','h4_snapshot_lower_high','h4_snapshot_lower_low','m15_events_all_history','m15_events_in_test','m15_events_overlapping_test','h4_event_pass','h4_missing_event','h4_failed_event','pullback_event','m1_breakout_event','entry_event','gap_event','risk_reject_event','primary_blocker_code','primary_blocker_text','reason_counts_json','event_details_json','total_trades','resolved','unknown','error'];
 const rows=last.markets.map(x=>{const d=x.diagnostic||{},a=x.data_audit||{},sm=x.summary||{};return [
 last.version,x.market,d.scan_begin_kst||last.scan_begin_kst,last.asof_kst,last.lookback_hours,last.fee_each_pct,
 a.minutes_observed??'',a.minutes_requested??'',a.m15_bars??'',a.h4_bars??'',
 d.h4_snapshot_code||'',d.h4_snapshot_lower_high??'',d.h4_snapshot_lower_low??'',
 d.m15_events_all_history??'',d.m15_events_in_test??'',d.m15_events_overlapping_test??'',
 d.h4_event_pass??'',d.h4_missing_event??'',d.h4_failed_event??'',d.pullback_event??'',
 d.m1_breakout_event??'',d.entry_event??'',d.gap_event??'',d.risk_reject_event??'',
 d.primary_blocker_code||'',d.primary_blocker_text||'',JSON.stringify(d.reason_counts||{}),
 JSON.stringify(d.event_details||[]),sm.signals??0,sm.resolved??0,sm.unknown??0,x.error||''
 ];});
 downloadCsv(headers,rows,'DIAGNOSTIC');
};
$('csv').onclick=()=>{
 if(!last||!last.total.signals)return;
 const h=['market','signal','signal_kst','entry_kst','entry','stop','target','risk_pct','score','score_total','volume_ratio','m15_choch_kst','status','net_pct','exit_reason','hold_minutes'];
 const rows=last.markets.flatMap(x=>(x.trades||[]).map(t=>h.map(k=>t[k]??'')));
 downloadCsv(h,rows,'TRADES');
};
})();</script></body></html>'''
