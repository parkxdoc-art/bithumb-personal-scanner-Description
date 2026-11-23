"""BITHUMB BTC MTF V1.4.9 | Read-only LIVE + historical snapshot research.

BTC: month/week/day/4h/1h/30m/15m/5m/3m/1m trends, all closed candles.
Coin scan: 5m/15m/1h/4h/day, BUY/WATCH with audit reasons.
Fees: 0.04% or 0.25% each side. No private endpoints or automatic orders.
Backtest: one dated KST snapshot, next 240m of observed 5m OHLC; no lookahead.
"""
from __future__ import annotations

import calendar
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request, Response
import btc_mtf_rebound_v13 as rebound
from btc_mtf_macd_v141 import all_macd_views
from btc_mtf_explain_v142 import build_explanation
from btc_mtf_combo_v143 import evaluate_combined
from btc_mtf_watch_v144 import classify_watch_tracks
from btc_mtf_replay_v145 import HourlyReplayPath
from btc_mtf_quality_v146 import snapshot_liquidity, investigate_missing_hour
from btc_mtf_study_v147 import compare_watch_cohorts

VERSION = "BTC MTF V1.4.9"
KST = ZoneInfo("Asia/Seoul")
API = "https://api.bithumb.com"
TIMEFRAMES = ("month", "week", "day", "4h", "1h", "30m", "15m", "5m", "3m", "1m")
SCAN_TF = ("5m", "15m", "1h", "4h", "day")
STRATEGIES = ("scalp", "swing")
STABLES = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USD1", "PYUSD", "USDE", "USDD", "RLUSD", "EURC", "EURI"}
MARKET_RE = re.compile(r"^KRW-[A-Z0-9]+$")
MIN_TURNOVER = 100_000_000  # UI default, selectable 50M/100M/500M/unlimited
FEE_CHOICES = (0.04, 0.25)
_CACHE = {}
_LOCK = threading.Lock()
app = Flask(__name__)


def now_kst():
    return datetime.now(KST)


def ttl_get(key):
    with _LOCK:
        value = _CACHE.get(key)
        return value[1] if value and value[0] > time.monotonic() else None


def ttl_put(key, value, seconds):
    with _LOCK:
        if len(_CACHE) > 1600:
            _CACHE.clear()
        _CACHE[key] = (time.monotonic() + seconds, value)


def api_get(path, params=None):
    err = None
    for attempt in range(3):
        try:
            r = requests.get(API + path, params=params, timeout=(4, 13), headers={
                "Accept": "application/json", "User-Agent": "bithumb-btc-mtf-personal-v1/1.0"
            })
            if r.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"temporary HTTP {r.status_code}")
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                raise RuntimeError("API array expected")
            return data
        except Exception as ex:
            err = ex
            if attempt < 2:
                time.sleep(.35 * (attempt + 1))
    raise RuntimeError(f"Bithumb {path}: {str(err)[:95]}")


def endpoint(tf):
    if tf not in TIMEFRAMES:
        raise ValueError("Unsupported timeframe")
    return "/v1/candles/" + ({"month": "months", "week": "weeks", "day": "days"}.get(tf, "minutes/" + str({"4h": 240, "1h": 60, "30m": 30, "15m": 15, "5m": 5, "3m": 3, "1m": 1}.get(tf, 5))))


def tf_end(start, tf):
    if tf == "month":
        m = start.month + 1
        y = start.year + (m > 12)
        m = (m - 1) % 12 + 1
        return start.replace(year=y, month=m, day=min(start.day, calendar.monthrange(y, m)[1]))
    if tf == "week":
        return start + timedelta(days=7)
    if tf == "day":
        return start + timedelta(days=1)
    return start + timedelta(minutes={"4h": 240, "1h": 60, "30m": 30, "15m": 15, "5m": 5, "3m": 3, "1m": 1}[tf])


def candle_close_cutoff(clock):
    """Treat a historical exact close as closed; keep 2s caution for LIVE.

    A live snapshot is at most 15 minutes old (see request_cfg).
    Older snapshots are completed historical observations, so a candle
    that closes EXACTLY at clock must be retained, not discarded by
    the 2-second live safety margin.
    """
    if clock >= now_kst() - timedelta(minutes=15):
        return clock - timedelta(seconds=2)
    return clock


def candles(market, tf, at=None, count=70):
    params = {"market": market, "count": min(200, count)}
    if at is not None:
        params["to"] = at.strftime("%Y-%m-%d %H:%M:%S")
    raw = api_get(endpoint(tf), params)
    clock = at or now_kst()
    cutoff = candle_close_cutoff(clock)
    result = {}
    for x in raw:
        try:
            start = datetime.fromisoformat(x["candle_date_time_kst"]).replace(tzinfo=KST)
            if tf_end(start, tf) > cutoff:
                continue  # historical exact close INCLUDED; live 2-second guard
            op, hi, lo, cl = [float(x[k]) for k in ("opening_price", "high_price", "low_price", "trade_price")]
            vol = float(x.get("candle_acc_trade_price") or 0)
            if not (math.isfinite(op) and math.isfinite(hi) and math.isfinite(lo) and math.isfinite(cl) and min(op, hi, lo, cl) > 0):
                continue
            result[start] = {"t": start, "o": op, "h": hi, "l": lo, "c": cl, "turnover": vol}
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    return sorted(result.values(), key=lambda r: r["t"])


def is_contiguous(rows, tf, n=10):
    if len(rows) < n:
        return False
    tail = rows[-n:]
    return all(tf_end(x["t"], tf) == y["t"] for x, y in zip(tail, tail[1:]))


def trend(rows, tf):
    if not is_contiguous(rows, tf, 10):
        return {"state": "unknown", "reason": "연속 마감봉 10개 부족", "bars": len(rows)}
    prices = [x["c"] for x in rows[-10:]]
    m5 = sum(prices[-5:]) / 5
    m10 = sum(prices) / 10
    c = prices[-1]
    state = "up" if c > m5 > m10 else "down" if c < m5 < m10 else "side"
    return {"state": state, "close": c, "ma5": m5, "ma10": m10,
            "closed_kst": tf_end(rows[-1]["t"], tf).isoformat(), "bars": len(rows)}


def aggregate(trends):
    states = [s for s in trends if s in ("up", "down", "side")]
    if len(states) < max(2, len(trends)//2):
        return "unknown"
    up, down = states.count("up"), states.count("down")
    return "up" if up > down and up >= (len(states)+1)//2 else "down" if down > up and down >= (len(states)+1)//2 else "side"


def historical_time(s):
    try:
        d = datetime.fromisoformat(str(s))
        if d.tzinfo is not None:
            raise ValueError("KST local date/time expected")
        d = d.replace(tzinfo=KST)
        if d.minute % 5 or d.second:
            raise ValueError("5분 단위 시각만 지원")
        if d > now_kst() - timedelta(minutes=245):
            raise ValueError("청산 240분이 지난 과거 시각만 검증 가능")
        if d < now_kst() - timedelta(days=360):
            raise ValueError("최근 360일 이내 시각 선택")
        return d
    except (TypeError, ValueError) as ex:
        raise ValueError(f"검증 시각 오류: {ex}")


def request_cfg(raw, need_market=False):
    if not isinstance(raw, dict):
        raise ValueError("요청 형식 오류")
    mode = raw.get("mode", "live")
    strategy = raw.get("strategy", "scalp")
    if strategy not in STRATEGIES:
        raise ValueError("단타/스윙 모드 오류")
    tf = raw.get("tf", "5m")
    if mode not in ("live", "backtest") or tf not in SCAN_TF:
        raise ValueError("모드 또는 시간봉 오류")
    fee = float(raw.get("fee", .04))
    if fee not in FEE_CHOICES:
        raise ValueError("편도 수수료는 0.04 또는 0.25만 선택")
    if mode == "backtest":
        at = historical_time(raw.get("at"))
    elif raw.get("snapshot_at"):
        try:
            at = datetime.fromisoformat(str(raw["snapshot_at"]).replace(" ", "T")).replace(tzinfo=KST)
            if abs((now_kst()-at).total_seconds()) > 900:
                raise ValueError("실시간 스캔 시각이 15분 이상 오래됨")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"실시간 기준 시각 오류: {exc}")
    else:
        at = now_kst()
    market = raw.get("market") if need_market else None
    if mode == "backtest" and strategy == "swing" and at > now_kst() - timedelta(hours=121):
        raise ValueError("스윙은 과거 기준 시각 이후 120시간이 지나야 검증할 수 있어")
    if need_market and (not isinstance(market, str) or not MARKET_RE.fullmatch(market) or market.split("-", 1)[1] in STABLES):
        raise ValueError("KRW 코인 마켓만 조회 가능")
    return mode, tf, fee, at, market, strategy


def market_list():
    cached = ttl_get("markets-v1")
    if cached is not None:
        return cached
    data = api_get("/v1/market/all", {"isDetails": "true"})
    names = {x["market"]: x.get("korean_name", x["market"]) for x in data if isinstance(x, dict) and MARKET_RE.fullmatch(str(x.get("market", ""))) and x["market"].split("-", 1)[1] not in STABLES}
    ttl_put("markets-v1", names, 600)
    return names


def universe():
    cached = ttl_get("ranked-markets-v1")
    if cached is not None:
        return cached
    names = market_list()
    ticker = {}
    ms = list(names)
    for i in range(0, len(ms), 50):
        try:
            arr = api_get("/v1/ticker", {"markets": ",".join(ms[i:i+50])})
            ticker.update({x["market"]: x for x in arr if isinstance(x, dict) and x.get("market")})
        except Exception:
            pass
    rows = []
    for market, name in names.items():
        if market == "KRW-BTC":
            continue  # BTC only on market trend board
        t = ticker.get(market) or {}
        turnover = float(t.get("acc_trade_price_24h") or 0)
        rows.append({"market": market, "name": name, "turnover": turnover,
                     "change24": round(float(t.get("signed_change_rate") or 0)*100, 3)})
    rows.sort(key=lambda x: (-x["turnover"], x["market"]))
    ttl_put("ranked-markets-v1", rows, 300)
    return rows


def btc_context(cfg):
    mode, selected, fee, at, _, strategy = cfg
    key = ("btc", mode, selected, at.strftime("%Y-%m-%d %H:%M"))
    cached = ttl_get(key)
    if cached is not None:
        return cached
    states = {}
    last4 = None
    rows4 = []
    def run_tf(tf):
        try:
            # at is the snapshot clock; 1m candles are not *currently forming*.
            rs = candles("KRW-BTC", tf, at, count=48)
            return tf, trend(rs, tf), rs
        except Exception as exc:
            return tf, {"state": "unknown", "reason": str(exc)[:100]}, []
    with ThreadPoolExecutor(max_workers=5) as pool:
        for tf, state, rows in pool.map(run_tf, TIMEFRAMES):
            states[tf] = state
            if tf == selected:
                last4 = rows
            if tf == "4h":
                rows4 = rows
    lng = aggregate([states[tf]["state"] for tf in ("month", "week", "day", "4h")])
    sht = aggregate([states[tf]["state"] for tf in ("1h", "30m", "15m", "5m", "3m", "1m")])
    selected_return = round((last4[-1]["c"]/last4[-5]["c"]-1)*100, 4) if last4 and is_contiguous(last4, selected, 5) else None
    context = {"tf": states, "long": lng, "short": sht,
               "selected_return": selected_return, "selected_tf": selected,
               "asof_kst": at.strftime("%Y-%m-%d %H:%M:%S")}
    ttl_put(key, context, 3600 if mode == "backtest" else 900)
    ttl_put(("btc-h4",mode,at.strftime("%Y-%m-%d %H:%M")), rows4, 3600 if mode == "backtest" else 900)
    return context


def coin_metrics(rows, tf, btc_ret):
    if not is_contiguous(rows, tf, 23) or btc_ret is None:
        return None, "종목 23봉 연속 이력 또는 같은 봉 BTC 수익률 부족"
    closes = [x["c"] for x in rows]
    last, prev = rows[-1], rows[-2]
    m5 = sum(closes[-5:]) / 5
    m20 = sum(closes[-20:]) / 20
    prev6high = max(r["h"] for r in rows[-7:-1])
    breakout = (last["c"] / prev6high - 1) * 100
    change4 = (closes[-1]/closes[-5]-1)*100
    relative = change4-btc_ret
    volprev = sum(x["turnover"] for x in rows[-21:-1])/20
    volratio = last["turnover"]/volprev if volprev>0 else 0
    body = (last["c"]-last["o"])/max(last["h"]-last["l"], 1e-14)
    tr = [max(x["h"]-x["l"], abs(x["h"]-y["c"]), abs(x["l"]-y["c"])) for x,y in zip(rows[-14:], rows[-15:-1])]
    atr = sum(tr)/len(tr)/last["c"]*100 if tr else None
    return {"close": last["c"], "ma5": m5, "ma20": m20,
            "breakout_pct": round(breakout, 4), "coin_return_pct": round(change4, 4),
            "btc_return_pct": btc_ret, "relative_pct": round(relative, 4),
            "volume_ratio": round(volratio, 3), "body_ratio": round(body, 3),
            "atr_pct": round(atr, 3) if atr is not None else None,
            "closed_kst": tf_end(last["t"], tf).strftime("%Y-%m-%d %H:%M")}, None


def coin_signal(metrics, ctx, fee):
    m = metrics
    conditions = {
        "trend": m["close"] > m["ma5"] > m["ma20"],
        "breakout": m["breakout_pct"] > 0,
        "relative": m["relative_pct"] >= .5,
        "volume": m["volume_ratio"] >= .7,
        "candle": m["body_ratio"] >= .5,
    }
    bear = ctx["long"] == "down" and ctx["short"] == "down"
    independent = m["relative_pct"] >= 1.5 and m["breakout_pct"] >= .25
    buy = all(conditions.values()) and (not bear or independent)
    watch = (conditions["trend"] or conditions["breakout"]) and m["relative_pct"] > 0 and m["body_ratio"] > 0
    if not buy and not watch:
        return None
    kind = "BUY" if buy else "WATCH"
    causes = []
    causes.append(f"같은 {ctx['selected_tf']} 기준 BTC 대비 상대강도 {m['relative_pct']:+.2f}%p (코인 {m['coin_return_pct']:+.2f}% / BTC {m['btc_return_pct']:+.2f}%)")
    causes.append(f"직전 6봉 고점 대비 마감 돌파폭 {m['breakout_pct']:+.2f}% · 거래대금 배수 {m['volume_ratio']:.2f}배")
    causes.append(f"종가 > MA5 > MA20 {'충족' if conditions['trend'] else '미충족'} · 양봉 몸통 비중 {m['body_ratio']*100:.0f}%")
    causes.append(f"BTC 장기 {state_ko(ctx['long'])}, 단기 {state_ko(ctx['short'])}" + (" · 약세장 상대강도 강화 충족" if bear and independent else ""))
    if kind == "WATCH":
        missing = [k for k, ok in conditions.items() if not ok]
        names = {"trend": "상승 배열", "breakout": "직전 고점 돌파", "relative": "BTC 대비 0.5%p 우위", "volume": "거래대금 0.7배", "candle": "양봉 몸통 50%"}
        causes.append("BUY 보류: " + (", ".join(names[k] for k in missing) if missing else "BTC 동시 강한 하락 중 독립 상승 강도 부족"))
    else:
        causes.append("모든 진입 품질 조건 충족 · 실제 주문이 아닌 연구용 BUY 후보")
    # Exchange fee applies per leg; backtest uses the same target levels.
    gross_tp1 = ((1+.01)*(1+fee/100)/(1-fee/100)-1)*100
    gross_tp2 = ((1+.10)*(1+fee/100)/(1-fee/100)-1)*100
    return {"signal": kind, "metrics": m, "explanation": causes,
            "plan": {"tp1_net": 1.0, "tp1_fraction": .7, "tp2_net": 10.0,
                     "tp2_fraction": .3, "stop_gross": -2.0, "max_minutes": 240,
                     "tp1_gross_pct": round(gross_tp1, 3), "tp2_gross_pct": round(gross_tp2, 3)}}


def state_ko(s):
    return {"up": "상승", "side": "횡보", "down": "하락", "unknown": "자료 부족"}.get(s, "자료 부족")


def backtest_exit(market, at, fee, getter=candles):
    """Simulate one snapshot entry at the 5m open and 240m observed path.

    Both stop and TP within a 5-minute candle: conservatively stop first.
    Prior 5-minute candles are not traded, and no synthetic candles are made.
    """
    out = {"status": "UNKNOWN", "net_pct": None, "reason": "", "tp1": False, "tp2": False,
           "stop": False, "minutes": None, "ambiguous": False}
    try:
        rows = getter(market, "5m", at+timedelta(minutes=241), count=100)
    except Exception as ex:
        return {**out, "reason": "청산 경로 조회 실패: " + str(ex)[:80]}
    indexed = {r["t"]:r for r in rows}
    if any(at+timedelta(minutes=5*k) not in indexed for k in range(48)):
        return {**out, "reason": "240분 중 5분봉 누락: 미래 가격 경로 확인 불가"}
    entry = indexed[at]["o"]
    if entry <= 0:
        return {**out, "reason": "진입 시가 없음"}
    cost = fee / 100
    p1 = entry*(1.01)*(1+cost)/(1-cost)
    p2 = entry*(1.10)*(1+cost)/(1-cost)
    stop = entry*.98
    fraction, realized = 1.0, 0.0
    tp1 = tp2 = False
    def net(px):
        return (px*(1-cost)/(entry*(1+cost))-1)*100
    for k in range(48):
        b = indexed[at+timedelta(minutes=5*k)]
        op, hi, lo, cl = b["o"], b["h"], b["l"], b["c"]
        if lo <= stop:
            ambiguous = (hi >= p1 if not tp1 else hi >= p2)
            return {**out, "status": "RESOLVED", "net_pct": round(realized + fraction*net(min(op,stop)), 6),
                    "reason": "", "tp1": tp1, "tp2": tp2, "stop": True,
                    "minutes": (k+1)*5, "ambiguous": ambiguous}
        if not tp1 and hi >= p1:
            realized += .7*net(p1)
            fraction = .3
            tp1 = True
        if tp1 and hi >= p2:
            realized += fraction*net(p2)
            return {**out, "status": "RESOLVED", "net_pct": round(realized, 6),
                    "reason": "", "tp1": True, "tp2": True, "stop": False,
                    "minutes": (k+1)*5, "ambiguous": False}
        if k == 47:
            return {**out, "status": "RESOLVED", "net_pct": round(realized + fraction*net(cl), 6),
                    "reason": "", "tp1": tp1, "tp2": False, "stop": False,
                    "minutes": 240, "ambiguous": False}
    return {**out, "reason": "결과 확인 불가"}


def _avg(values):
    return sum(values)/len(values)


def swing_metrics(day, h4, h1, btc4, at):
    """Only completed candles at the snapshot and synchronized BTC 4h series."""
    # Historical outage gaps should not wipe out the entire market. Do not
    # fabricate missing candles: the indicator module validates real-bar
    # density and blocks BUY while recent hourly data is discontinuous.
    if not (len(day)>=24 and len(h4)>=34 and len(h1)>=16):
        return None, "스윙 판정용 실제 캔들 수 부족(일 24, 4시간 34, 1시간 16)"
    if at-tf_end(h4[-1]["t"],"4h") > timedelta(hours=8) or at-tf_end(h1[-1]["t"],"1h") > timedelta(hours=3):
        return None, "4시간/1시간 최신 완료봉이 오래됨"
    btcmap={b["t"]:b["c"] for b in btc4}
    if h4[-1]["t"] not in btcmap or h4[-5]["t"] not in btcmap:
        return None, "BTC와 코인의 동일 시각 4시간봉 부족"
    d=[x["c"] for x in day]; f=[x["c"] for x in h4]; hour=[x["c"] for x in h1]
    day_ma20=_avg(d[-20:]); day_prev20=_avg(d[-24:-4])
    ma7=_avg(f[-7:]); ma14=_avg(f[-14:]); ma30=_avg(f[-30:]); prev7=_avg(f[-10:-3])
    h1ma7=_avg(hour[-7:]); h1ma14=_avg(hour[-14:])
    ret16=(f[-1]/f[-5]-1)*100; btc16=(btcmap[h4[-1]["t"]]/btcmap[h4[-5]["t"]]-1)*100
    prev12high=max(x["h"] for x in h4[-13:-1]); high6=max(x["h"] for x in h4[-7:-1])
    lo_recent=min(x["l"] for x in h4[-7:-1]); lo_prior=min(x["l"] for x in h4[-13:-7])
    volbase=_avg([x["turnover"] for x in h4[-21:-1]]); ratio=h4[-1]["turnover"]/volbase if volbase else 0
    tr=[max(x["h"]-x["l"],abs(x["h"]-y["c"]),abs(x["l"]-y["c"])) for x,y in zip(h4[-14:],h4[-15:-1])]
    atr=_avg(tr)/f[-1]*100
    dayrise=(d[-1]/d[-2]-1)*100; ret24=(f[-1]/f[-7]-1)*100
    ext=(f[-1]/ma7-1)*100; support=lo_recent*.998
    risk=(f[-1]/support-1)*100
    breakout=(f[-1]/prev12high-1)*100
    return {
      "close":f[-1],"ma5":ma7,"ma20":ma14,"day_ma20":round(day_ma20,6),"h4_ma30":round(ma30,6),
      "coin_return_pct":round(ret16,4),"btc_return_pct":round(btc16,4),"relative_pct":round(ret16-btc16,4),
      "breakout_pct":round(breakout,4),"volume_ratio":round(ratio,3),"atr_pct":round(atr,3),
      "h4_distance_ma7_pct":round(ext,3),"structure_risk_pct":round(risk,3),"support":round(support,8),
      "day_rise_pct":round(dayrise,3),"rise24_pct":round(ret24,3),
      "h4_higher_low":lo_recent>lo_prior,"h4_higher_high":high6>max(x["h"] for x in h4[-13:-7]),
      "day_up":d[-1]>day_ma20 and day_ma20>day_prev20,
      "h4_up":f[-1]>ma7>ma14 and ma7>prev7,
      "h1_up":hour[-1]>h1ma7>h1ma14,
      "recovery":min(x["l"] for x in h4[-3:-1])<=ma7*1.016 and f[-1]>f[-2],
      "closed_kst":tf_end(h4[-1]["t"],"4h").strftime("%Y-%m-%d %H:%M"),
      "body_ratio":round((h4[-1]["c"]-h4[-1]["o"])/max(h4[-1]["h"]-h4[-1]["l"],1e-14),3),
    },None



def strict_swing_gate(m, ctx, day, h4):
    """V1.4 research gate; never uses candles after the snapshot."""
    h4_ma20 = sum(x["c"] for x in h4[-20:]) / 20
    h4_ma20_prev = sum(x["c"] for x in h4[-21:-1]) / 20
    checks = {
        "점수 9/10 이상": m["score"] >= 9,
        "일봉 MA20 상향 + 22봉 연속": m["day_up"] and is_contiguous(day, "day", 22),
        "4시간봉 MA20 상회·상향 + 21봉 연속": (
            is_contiguous(h4, "4h", 21) and m["close"] > h4_ma20 > h4_ma20_prev),
        "4시간봉 MA7/14 상승 배열": m["h4_up"],
        "1시간봉 반등 확인": m["checks"]["h1_confirm"],
        "RSI(14) 50 이상": m["rsi14"] >= 50,
        "MACD 히스토그램 양수": m["macd_hist"] > 0,
        "4시간 MACD 골든 상태 (실제 교차와 구분)": m["macd_h4"]["state"] == "GOLDEN",
        "4시간 거래대금 1.1배 이상": m["volume_ratio"] >= 1.1,
        "BTC 장·단기 비하락": (
            ctx["long"] in ("up", "side") and ctx["short"] in ("up", "side")),
        "최근 봉 공백 없음": not m.get("data_gap_recent", True),
        "저점·지지 9% 내 / 선택 시간봉 확인": (
            1.2 <= m["rebound_from_low_pct"] <= 9
            and 0 <= m["structure_risk_pct"] <= 9
            and m.get("timing_ok", False)),
    }
    return {
        "passed": all(checks.values()), "checks": checks,
        "missing": [k for k, v in checks.items() if not v],
        "h4_ma20": round(h4_ma20, 8),
        "h4_ma20_prev": round(h4_ma20_prev, 8)
    }


def swing_signal(m,ctx,fee):
    rel=m["relative_pct"];bear=ctx["long"]=="down" or ctx["short"]=="down"
    strong_rel=rel>=(2.0 if bear else 1.0)
    excessive=m["h4_distance_ma7_pct"]>7.5 or m["rise24_pct"]>22.0 or m["structure_risk_pct"]>12
    setup=m["breakout_pct"]>0 or m["recovery"]
    good=(m["day_up"] and m["h4_up"] and m["h1_up"] and strong_rel
          and m["h4_higher_low"] and setup and m["volume_ratio"]>=.7
          and m["structure_risk_pct"]<=8.0 and m.get("timing_ok",True) and not excessive)
    watch=((m["day_up"] or m["h4_up"]) and rel>=.3 and
           (m["h4_up"] or m["h4_higher_high"] or m["breakout_pct"]>0))
    if not good and not watch:return None
    kind="BUY" if good else "WATCH"
    checks={
      "일봉 MA20 상회·우상향":m["day_up"],
      "4시간봉 MA7·MA14 상승":m["h4_up"],
      "4시간봉 저점 상승":m["h4_higher_low"],
      "1시간봉 재상승":m["h1_up"],
      "BTC 대비 강도 충족":strong_rel,
      "4시간봉 돌파·눌림 회복":setup,
      "4시간봉 거래대금 0.7배 이상":m["volume_ratio"]>=.7,
      "구조적 손절 거리 8% 이내":m["structure_risk_pct"]<=8.0,
      "선택한 검색봉 상승 확인":m.get("timing_ok",True),
      "급등 추격 위험 낮음":not excessive,
    }
    causes=[
      f"스윙: 일봉 {'상승' if m['day_up'] else '미확정'} · 4시간봉 {'상승' if m['h4_up'] else '미확정'} · 1시간봉 {'상승' if m['h1_up'] else '미확정'}",
      f"동일한 16시간 동안 코인 {m['coin_return_pct']:+.2f}%, BTC {m['btc_return_pct']:+.2f}% → 상대강도 {rel:+.2f}%p",
      f"4시간봉 고점 돌파폭 {m['breakout_pct']:+.2f}% · 최근 고점/저점 상승 {m['h4_higher_high']}/{m['h4_higher_low']}",
      f"4시간봉 MA7 이격 {m['h4_distance_ma7_pct']:+.2f}% · 최근 24시간 {m['rise24_pct']:+.2f}% · 구조적 지지까지 {m['structure_risk_pct']:.2f}%",
      f"4시간봉 거래대금 배수 {m['volume_ratio']:.2f}배 · BTC 장기 {state_ko(ctx['long'])}, 단기 {state_ko(ctx['short'])}",
      f"선택 검색봉 {m.get('selected_tf','4h')} 추세 {'상승' if m.get('timing_ok',True) else '미확정'}",
    ]
    if kind=="WATCH":
        missing=[k for k,v in checks.items() if not v]
        causes.append("BUY 보류: "+(", ".join(missing) if missing else "상승 확인 대기"))
    else:
        causes.append("스윙 구조 조건 충족 · 체결 전 호가/집중보유/공시 위험 별도 확인")
    if excessive:causes.append("⚠ 급등 추격 위험: 이미 많이 오른 시점이므로 신규 진입은 WATCH")
    cost=fee/100
    tp1=((1.05)*(1+cost)/(1-cost)-1)*100
    tp2=((1.12)*(1+cost)/(1-cost)-1)*100
    return {"signal":kind,"metrics":m,"explanation":causes,"plan":{
      "tp1_net":5.0,"tp1_fraction":.5,"tp2_net":12.0,"tp2_fraction":.5,
      "stop_gross":-5.0,"max_minutes":7200,
      "tp1_gross_pct":round(tp1,3),"tp2_gross_pct":round(tp2,3),
      "support":m["support"],"support_risk_pct":m["structure_risk_pct"]}}


def swing_backtest_exit(market, at, fee, getter=candles, detailed=False):
    """Simulate 120h only on verified Bithumb 1h candles.

    V1.4.9 targeted recheck is triggered only when the replay actually
    encounters a missing hour. Do NOT fill missing hours with prices from
    30m or carry-forward data. Existing exit-order and fee logic unchanged.
    """
    out = {"status": "UNKNOWN", "net_pct": None, "reason": "", "tp1": False,
           "tp2": False, "stop": False, "minutes": None, "ambiguous": False}
    entry_at = at.replace(minute=0, second=0, microsecond=0)
    if entry_at < at:
        entry_at += timedelta(hours=1)
    path = HourlyReplayPath(market, entry_at, getter, detailed=detailed)
    def done(**fields):
        return {**out, **fields, "audit": path.audit()}

    first = path.hour(entry_at)
    if first is None or first.get("o", 0) <= 0:
        return done(reason="진입 시각 "+entry_at.strftime("%Y-%m-%d %H:%M")
                    +" 1시간봉 미확인 (재조회: "+path.audit()["status_code"]+")",
                    reason_code="ENTRY_HOUR_MISSING")
    entry = first["o"]
    cost = fee / 100
    p1 = entry * 1.05 * (1+cost)/(1-cost)
    p2 = entry * 1.12 * (1+cost)/(1-cost)
    stop = entry * .95
    rest, realized, tp1 = 1.0, 0.0, False
    def net(px):
        return (px*(1-cost)/(entry*(1+cost))-1)*100
    for i in range(120):
        r = path.hour(entry_at + timedelta(hours=i))
        if r is None:
            clock = (entry_at + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M")
            code = path.audit()["status_code"]
            return done(reason=(f"진입 후 {i}시간째 {clock} KST 1시간봉 미확인"
                                +f" (재조회 {code}); 이후 청산 미확정"),
                        reason_code="MISSING_HOURLY_BAR",
                        tp1=tp1, minutes=i*60)
        if r["l"] <= stop:
            return done(status="RESOLVED",
                        net_pct=round(realized + rest*net(min(r["o"], stop)), 6),
                        tp1=tp1, stop=True, minutes=(i+1)*60,
                        ambiguous=r["h"] >= (p2 if tp1 else p1))
        if not tp1 and r["h"] >= p1:
            tp1 = True
            realized += .5 * net(p1)
            rest = .5
        if tp1 and r["h"] >= p2:
            return done(status="RESOLVED",
                        net_pct=round(realized + rest*net(p2), 6),
                        tp1=True, tp2=True, minutes=(i+1)*60)
        if i == 119:
            return done(status="RESOLVED",
                        net_pct=round(realized + rest*net(r["c"]), 6),
                        tp1=tp1, minutes=7200)
    return done(reason="미확정 경로", reason_code="UNKNOWN_PATH")


@app.route("/api/replay-spotcheck",methods=["GET","POST"])
def replay_spotcheck_api():
    """Read-only V1.4.4 five early-WATCH candles, 2026-10-31 15:00 KST.

    This route does NOT retest the selection strategy, place orders, or
    reinterpret any missing candles as profit/loss.
    """
    try:
        raw = (request.get_json(silent=True) or {}) if request.method=="POST" else request.args
        fee = float(raw.get("fee", 0.04))
        if fee not in FEE_CHOICES:
            raise ValueError("편도 수수료는 0.04 또는 0.25만 지원")
        at = historical_time("2026-10-31 15:00:00")
        cached = ttl_get(("replay-spotcheck-v146",fee))
        if cached is not None:
            return jsonify(cached)
        targets = (
            ("KRW-BLUE","블루핀"), ("KRW-2Z","더블제로"),
            ("KRW-MBL","무비블록"), ("KRW-NXPC","넥스페이스"),
            ("KRW-STRAX","저트라"),
        )
        def one_target(pair):
            market, name = pair
            bt = swing_backtest_exit(market,at,fee,detailed=True)
            micro = None
            stamp = bt.get("audit",{}).get("first_unresolved_or_recovered_hour_kst")
            if bt.get("status") == "UNKNOWN" and bt.get("reason_code") in (
                    "MISSING_HOURLY_BAR","ENTRY_HOUR_MISSING") and stamp:
                try:
                    gap = datetime.strptime(stamp,"%Y-%m-%d %H:%M").replace(tzinfo=KST)
                    if at <= gap < at + timedelta(hours=121):
                        # Only forensic investigation AFTER a past snapshot.
                        # Never feed these 1m/5m prices into the exit engine.
                        micro = investigate_missing_hour(market,gap,candles)
                except (ValueError,TypeError):
                    pass
            return {"market":market,"name":name,"backtest":bt,"gap_micro":micro}
        with ThreadPoolExecutor(max_workers=2) as executor:
            items = list(executor.map(one_target, targets))
        payload = dict(ok=True,version=VERSION,
                       snapshot_kst=at.strftime("%Y-%m-%d %H:%M"),
                       fee_each_pct=fee,items=items,
                       note="과거 초기 WATCH 5종목의 가상 가격경로만 재조회. 실제 매매 아님, UNKNOWN은 손실/수익으로 변환하지 않음.")
        ttl_put(("replay-spotcheck-v146",fee),payload,120)
        return jsonify(payload)
    except Exception as ex:
        return jsonify(ok=False,error=str(ex)[:200]), 400


@app.post("/api/study-v147")
def study_v147_api():
    """Retrospective comparison of already-computed historical candidates.

    WATCH remains a research observation, not a real trade. The endpoint
    reads no exchange data, makes no orders and performs no lookahead scans.
    """
    try:
        if request.content_length and request.content_length > 2_000_000:
            raise ValueError("연구 비교 요청 용량 초과")
        raw = request.get_json(silent=True)
        if not isinstance(raw,dict) or raw.get("mode")!="backtest" or raw.get("strategy")!="swing":
            raise ValueError("스윙 과거 백테스트 결과만 비교 가능")
        fee = float(raw.get("fee",0.04))
        report = compare_watch_cohorts(
            raw.get("items"),scanned=raw.get("scanned",0),
            issues=raw.get("issues",0),fee_each=fee)
        return jsonify(ok=True,version=VERSION,study=report)
    except (TypeError,ValueError) as ex:
        return jsonify(ok=False,error=str(ex)[:180]),400


@app.get("/")
def page():
    return Response(HTML, content_type="text/html; charset=utf-8")


@app.get("/health")
def health():
    return jsonify(ok=True, version=VERSION, mode="read-only")


@app.get("/api/universe")
def universe_api():
    try:
        return jsonify(ok=True, items=universe(), version=VERSION, min_turnover_live=MIN_TURNOVER)
    except Exception as ex:
        return jsonify(ok=False, error=str(ex)[:150]), 502


@app.post("/api/btc")
def btc_api():
    try:
        cfg = request_cfg(request.get_json(silent=True))
        return jsonify(ok=True, context=btc_context(cfg), version=VERSION)
    except Exception as ex:
        return jsonify(ok=False, error=str(ex)[:160]), 400


@app.post("/api/coin")
def coin_api():
    try:
        raw = request.get_json(silent=True)
        cfg = request_cfg(raw, True)
        mode, tf, fee, at, market, strategy = cfg
        if market not in market_list():
            raise ValueError("현재 KRW 마켓 목록에 없음")
        ctx = btc_context(cfg)
        if strategy == "swing":
            with ThreadPoolExecutor(max_workers=3) as pool:
                fday=pool.submit(candles,market,"day",at,80)
                fh4=pool.submit(candles,market,"4h",at,90)
                fh1=pool.submit(candles,market,"1h",at,50)
                day,h4,h1=fday.result(),fh4.result(),fh1.result()
            btc4=ttl_get(("btc-h4",mode,at.strftime("%Y-%m-%d %H:%M")))
            if btc4 is None:
                btc4=candles("KRW-BTC","4h",at,50)
            baseline,issue=swing_metrics(day,h4,h1,btc4,at)
            if baseline is None:
                return jsonify(ok=True,market=market,signal=None,issue=issue)
            m,issue=rebound.bottom_rebound_metrics(day,h4,h1,btc4,at,baseline,is_contiguous,tf_end)
            if m is None:
                return jsonify(ok=True,market=market,signal=None,issue=issue)
            if tf in ("5m","15m"):
                timing_rows=candles(market,tf,at,count=18)
                m["timing_ok"]=trend(timing_rows,tf).get("state")=="up"
            else:
                m["timing_ok"]={"1h":m["checks"]["h1_confirm"],"4h":m["checks"]["ma_recover"],"day":m["day_ma20_distance_pct"]>=-6}[tf]
            m["selected_tf"]=tf
            macd_views=all_macd_views(day,h4,tf_end,is_contiguous)
            m["macd_h4"]=macd_views["h4"]
            m["macd_day"]=macd_views["day"]
            m["macd_day_chart"]=macd_views["day_chart"]
            explain=build_explanation(m,ctx,day,h4,h1,fee)
            # Observation only. Uses the 24 hours of COMPLETED 1h bars
            # before the snapshot, never the future 120h exit path.
            liquidity=snapshot_liquidity(h1,at)
            signal=rebound.rebound_signal(m,ctx,fee)
            if signal is not None:
                signal["explain"]=explain
                signal["liquidity_audit"]=liquidity
            if signal is not None:
                previous = signal["signal"]
                gate = strict_swing_gate(m,ctx,day,h4)
                combined = evaluate_combined(previous,gate,explain,m,h4,is_contiguous)
                signal["legacy_signal"] = previous
                signal["strict_gate"] = gate
                signal["strict_signal"] = ("BUY" if previous=="BUY" and gate["passed"] else "WATCH")
                signal["combined_gate"] = combined
                tracks = classify_watch_tracks(previous,gate,combined,explain,m,ctx)
                signal["watch_tracks"] = tracks
                signal["v143_signal"] = combined["signal"]
                signal["watch_kind"] = tracks["kind"]
                signal["signal"] = "BUY" if tracks["kind"] == "BUY" else (
                    "WATCH" if tracks["kind"] in ("EARLY_WATCH","TREND_WATCH") else "RESEARCH")
                signal["stage"] = tracks["label"]
                signal["explanation"].append(
                    "V1.3.3 원본 "+previous+" → V1.4.1 추세 "+signal["strict_signal"]
                    +" → V1.4.3 종합 "+combined["signal"]
                    +" → V1.4.9 분리관찰 "+tracks["kind"]
                )
                signal["explanation"].append(
                    "초기관찰 기준 미충족: "+(
                        ", ".join(tracks["early_missing"]) if tracks["early_missing"] else "없음")
                )
                signal["explanation"].append(
                    "추세확인 기준 미충족: "+(
                        ", ".join(tracks["trend_missing"]) if tracks["trend_missing"] else "없음")
                )
                signal["explanation"].append(
                    "종합 WATCH 보류: "+(
                        ", ".join(combined["missing_watch"]) if combined["missing_watch"] else "모든 관찰 조건 충족")
                )
                if signal["signal"] != "BUY":
                    signal["explanation"].append(
                        "종합 BUY 보류: "+", ".join(combined["missing_buy"])
                    )
                signal["explanation"].append(
                    "선행 유동성 관측 "+str(liquidity["checks_passed"])+"/4: "
                    +liquidity["label"]+" · 24시간 완료 1시간봉 "
                    +str(liquidity["hourly_bars_observed"])+"/24 · 관측 거래대금 "
                    +str(round(liquidity["observed_turnover_24h_krw"]/100_000_000,2))
                    +"억원 (매매 신호/기존 BUY 조건 영향 없음)"
                )
                signal["explanation"].append(
                    "MACD 4시간(12,26,9): " + macd_views["h4"]["state"]
                    + ", 이번 봉 교차 " + macd_views["h4"]["event"]
                    + " | 일봉(12,26,9): " + macd_views["day"]["state"]
                    + " | 일봉(3,10,16): " + macd_views["day_chart"]["state"]
                )
        else:
            rows=candles(market,tf,at,count=50)
            m,issue=coin_metrics(rows,tf,ctx.get("selected_return"))
            if m is None:
                return jsonify(ok=True,market=market,signal=None,issue=issue)
            signal=coin_signal(m,ctx,fee)
        diagnostic = None
        if strategy == "swing" and raw.get("want_diagnostic") is True:
            diagnostic = {
                "closed_kst": m["closed_kst"], "close": m["close"],
                "score": m["score"], "rsi14": m["rsi14"],
                "day_up": m["day_up"], "h4_up": m["h4_up"],
                "macd": macd_views,
                "explain": explain,
                "strict_gate": strict_swing_gate(m,ctx,day,h4),
                "combined_gate": signal["combined_gate"] if signal else evaluate_combined(
                    None,strict_swing_gate(m,ctx,day,h4),explain,m,h4,is_contiguous),
                "watch_tracks": signal["watch_tracks"] if signal else classify_watch_tracks(
                    None,strict_swing_gate(m,ctx,day,h4),
                    evaluate_combined(None,strict_swing_gate(m,ctx,day,h4),explain,m,h4,is_contiguous),
                    explain,m,ctx),
                "legacy_signal": signal["legacy_signal"] if signal else "NONE",
                "liquidity_audit": liquidity,
            }
        if signal is None:
            return jsonify(ok=True, market=market, signal=None, diagnostic=diagnostic)
        if mode == "backtest":
            signal["backtest"] = swing_backtest_exit(market,at,fee, detailed=signal.get("watch_kind") in ("EARLY_WATCH","TREND_WATCH","BUY")) if strategy == "swing" else backtest_exit(market,at,fee)
        return jsonify(ok=True, market=market, signal=signal, strategy=strategy, diagnostic=diagnostic)
    except Exception as ex:
        return jsonify(ok=False, error=str(ex)[:170]), 400


HTML = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BITHUMB · BTC MTF V1.4.9 · 3·7일 최대 50종목 비교 연구</title>
<style>
:root{color-scheme:dark;--bg:#0b1120;--panel:#161f33;--panel2:#202c46;--ink:#edf4ff;--muted:#93a4bd;--line:#34445f;--cyan:#45cfce;--green:#39d7a1;--yellow:#ffc56d;--red:#ff7d85}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font-family:system-ui,-apple-system,'Noto Sans KR',sans-serif;color:var(--ink);line-height:1.5}main{max-width:1100px;margin:auto;padding:18px 16px 48px}h1{font-size:1.4rem;line-height:1.3;margin:0}h2{font-size:1.08rem;margin:0 0 11px}small,.muted{color:var(--muted)}header{display:flex;justify-content:space-between;gap:10px;align-items:center;margin:5px 0 19px}.eyebrow{color:var(--cyan);font-size:12px;font-weight:700;letter-spacing:1px}.tag{border:1px solid var(--line);border-radius:999px;padding:4px 9px;color:var(--muted);white-space:nowrap;font-size:11px}.surface{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:17px;margin-bottom:14px}.tabs{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:15px}.tabs button{font-size:15px;font-weight:750;padding:13px}.tabs button.on{background:#145c64;border-color:var(--cyan);color:white}button,select,input{border:1px solid var(--line);border-radius:11px;background:var(--panel2);color:var(--ink);font:inherit;padding:10px;min-width:0}button{cursor:pointer}button:disabled{opacity:.42;cursor:default}button.primary{background:var(--cyan);border-color:var(--cyan);color:#09252a;font-weight:800}button.secondary{background:transparent}.toolbar{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.field{display:flex;flex-direction:column;gap:5px}.field label{color:var(--muted);font-size:12px;font-weight:650}.row{display:flex;gap:9px;align-items:center;flex-wrap:wrap}.timebox{display:flex;gap:8px}.timebox input{flex:1;min-width:0}.action{margin-top:14px}.action button{padding:11px 20px}.help{font-size:12px;color:var(--muted);margin-top:9px}.btc-agg{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}.agg{background:#101d32;border:1px solid var(--line);padding:12px;border-radius:12px}.agg strong{display:block;font-size:1.16rem;margin-top:4px}.tf-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}.tf{background:#101b2e;border:1px solid var(--line);border-radius:12px;padding:10px}.tf .name{font-size:12px;color:var(--muted)}.tf .status{font-size:16px;font-weight:750;margin-top:2px}.up{color:var(--green)}.down{color:var(--red)}.side{color:var(--yellow)}.unknown{color:var(--muted)}.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}.metric{padding:13px;background:#101b2e;border:1px solid var(--line);border-radius:12px}.metric strong{font-size:22px;display:block}.metric span{font-size:12px;color:var(--muted)}.bar{height:6px;background:#27354a;overflow:hidden;border-radius:8px;margin-top:12px}.bar i{display:block;background:var(--cyan);height:100%;width:0%;transition:width .2s}.result{border:1px solid var(--line);background:#101c30;border-radius:13px;margin-top:9px;padding:12px}.result summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;flex-wrap:wrap}.score-chip{border:1px solid #4c7087;background:#17374a;color:#c4f5f0;border-radius:7px;padding:3px 6px;font-weight:850;font-size:12px;white-space:nowrap}.entry-price{border:1px solid #466380;background:#243553;border-radius:8px;padding:4px 9px;font-size:13px;font-weight:750;color:#ebf6ff;white-space:nowrap}.entry-price.watch-entry{background:#352f24;color:#ffe8b3;border-color:#7b623c}.entry-note{border:1px solid #364b67;background:#172943;padding:10px;border-radius:9px;margin:8px 0}.entry-metrics{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}.entry-metrics span{border:1px solid #364b67;padding:6px 9px;border-radius:8px;font-size:12px}.result summary::-webkit-details-marker{display:none}.result summary b{font-size:16px}.badge{font-size:11px;font-weight:900;padding:4px 8px;border-radius:7px}.badge.buy{background:#115e4c;color:#9ff9d5}.badge.watch{background:#59421e;color:#ffdca5}.badge.research{background:#303d53;color:#bdd0e8}.push{margin-left:auto;color:var(--muted);font-size:12px}.reason{font-size:13px;color:#cfdaeb;margin-top:12px;padding-top:9px;border-top:1px solid var(--line)}.reason p{margin:5px 0}.pnl{font-size:13px;margin-top:7px}.empty{font-size:13px;color:var(--muted);padding:15px 0}.notice{font-size:12px;color:#b6c7df;margin-top:12px}.foot{margin-top:26px;color:var(--muted);font-size:12px}#error{color:var(--red);font-size:13px;margin-top:9px;white-space:pre-wrap}#scanstatus{color:var(--muted);font-size:13px}.insight-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:12px 0}
.insight-score{border:1px solid var(--line);background:#15253c;border-radius:12px;padding:11px 10px;text-align:center}
.insight-score strong{display:block;font-size:22px;color:var(--cyan)}
.insight-score span{font-size:11px;color:var(--muted)}
.insight-wrap{margin:14px 0;border:1px solid var(--line);border-radius:12px;padding:11px}
.insight-plan{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.insight-plan div{border:1px solid var(--line);border-radius:9px;padding:8px}
.insight-plan b{display:block;font-size:16px}.insight-plan small{font-size:11px}
.insight-proof{font-size:12px;margin:8px 0;color:var(--muted)}
.insight-proof span{display:block;margin:3px 0}
.study-scroll{overflow-x:auto;margin:9px 0 14px}.study-scroll table{border-collapse:collapse;width:100%;min-width:625px;font-size:12px}.study-scroll th,.study-scroll td{padding:8px 7px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}.study-scroll th{color:var(--muted)}#study-panel h3{font-size:13px;margin:14px 0 6px}#study-panel details{border-top:1px solid var(--line);padding-top:10px}
.hidden{display:none!important}
@media(max-width:760px){main{padding:14px 11px 30px}header{align-items:flex-start}.toolbar{grid-template-columns:1fr 1fr}.tf-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.surface{padding:13px}.btc-agg{grid-template-columns:1fr 1fr}}
@media(max-width:355px){.toolbar{grid-template-columns:1fr 1fr}.row{gap:6px}.action button{flex:1}}
</style></head><body><main>
<header><div><div class="eyebrow">PERSONAL RESEARCH · BITHUMB KRW</div><h1>BTC 추세 · 코인 검색기</h1><small>BTC MTF V1.4.9 · 3·7일 10/25/50종목 확장 비교</small></div><span class="tag">KRW ONLY</span></header>
<div class="tabs"><button id="live-tab" class="on" type="button">● 실시간 검색</button><button id="backtest-tab" type="button">◷ 과거 백테스트</button></div>
<section class="surface"><h2>검색 설정</h2><div class="tabs"><button id="scalp-tab" type="button">⚡ 단타</button><button id="swing-tab" class="on" type="button">🌱 스윙 · 점수제</button></div><div class="toolbar">
<div class="field"><label for="tf">코인 검색 시간봉</label><select id="tf"><option value="5m">5분봉</option><option value="15m">15분봉</option><option value="1h">1시간봉</option><option value="4h" selected>4시간봉</option><option value="day">일봉</option></select></div>
<div class="field"><label for="fee">편도 수수료</label><select id="fee"><option value="0.04">0.04%</option><option value="0.25">0.25%</option></select></div>
<div class="field"><label for="turnover">24시간 거래대금 하한</label><select id="turnover"><option value="50000000">5천만 원 이상</option><option value="100000000" selected>1억 원 이상</option><option value="500000000">5억 원 이상</option><option value="0">제한 없음</option></select></div>
<div class="field"><label for="scope">검색 범위</label><select id="scope"><option value="20">거래대금 상위 20</option><option value="40" selected>거래대금 상위 40</option><option value="80">거래대금 상위 80</option><option value="all">전체 KRW</option></select></div>
<div class="field" id="watch-filter-field"><label for="watch-filter">WATCH 표시 기준 (스윙)</label><select id="watch-filter"><option value="strict" selected>BUY + 초기/추세 WATCH (기본)</option><option value="early">초기 반등 WATCH만</option><option value="trend">추세 확인 WATCH만</option><option value="nine">연구 비교 · 원본 9점+</option><option value="ten">연구 비교 · 원본 10점</option><option value="all">모든 원본 후보(연구 보류 포함)</option></select></div><div class="field"><label for="focus">관찰 코인 지정 (선택)</label><input id="focus" placeholder="예: PIEVERSE" maxlength="18"></div><div class="field" id="backtest-date-field" style="display:none"><label for="at">과거 검사 시각 (한국시간)</label><input type="datetime-local" id="at" step="300"></div><div class="field" id="backtest-days-field" style="display:none"><label for="days">자체 백테스트</label><select id="days"><option value="1">1일 · 1회</option><option value="3">3일 · 매일 같은 시각</option><option value="7">7일 · 매일 같은 시각</option></select></div><div class="field" id="history-breadth-field" style="display:none"><label for="history-breadth">3·7일 스윙 검색 대상 (현재 거래대금 순위)</label><select id="history-breadth"><option value="10">상위 10종목 · 빠른 비교</option><option value="25" selected>상위 25종목 · 기본 추천</option><option value="50">상위 50종목 · 확장 연구</option></select></div>
</div><div class="action row"><button class="primary" id="start" type="button">검색 시작</button><button class="secondary" id="csv" disabled type="button">후보 전체 CSV</button><button class="secondary" id="issuecsv" disabled type="button">문제 내역 CSV</button><button class="secondary" id="focuscsv" disabled type="button">관찰 코인 MACD CSV</button><small id="scanstatus">검사 전</small></div><div id="error"></div>
<div class="help" id="strategy-help">V1.4.9: 3·7일 스윙 연구는 10/25/50종목 검색폭 선택(기본 25) 가능. 하루 1회씩 비교하며 4개 동시 작업은 유지. 지난 V1.4.8 과거 정각 완료봉 수정과 기존 BUY/WATCH 기준은 그대로. 선행 유동성 4개 기준도 유지. 이는 경고·설명용으로 BUY를 바꾸지 않아. 10월 31일 WATCH 5종목은 상단에서 1시간·30분·5분·1분봉 누락 원인을 조사할 수 있어. 미래 캔들은 신호에 사용하지 않고 자료 없으면 UNKNOWN 유지. 편도 수수료 0.04%.</div><div class="help" id="live-help">설정한 24시간 거래대금 하한을 넘는 KRW 코인만 검색해. 제한 없음을 선택하면 거래가 거의 없는 코인까지 포함되어 자료 부족이 늘 수 있어. 마감봉만 사용하며 자동 주문은 없어.</div>
<div class="help hidden" id="backtest-help">선택 시각의 과거 <b>완료봉만 사용</b>해 신호를 판정하고 단타는 240분/스윙은 120시간을 가상 청산해. 3·7일은 지정한 시각에 하루 1번씩 반복. 현재 상장 목록과 현재 24시간 거래대금 순위·하한을 쓰므로 생존·미래정보 편향이 있어. 미확정 봉은 따로 표시해.</div>
</section>
<section class="surface" id="spotcheck-panel"><h2>10월 31일 초기 WATCH 5종목 · 데이터 재검증 <small>전체 스캔 불필요</small></h2><div class="notice">2026-10-31 15:00 KST 초기 WATCH 5종목의 1시간봉 누락을 재확인하고, 미확정 시 같은 시간대 30분·5분·1분봉까지 조사해. 하위 분봉은 원인 진단만 하며 가상 수익률에 반영하지 않아. 7일을 초과한 체결 내역은 공식 공개 API의 과거 조회 범위 밖이야.</div><div class="action row"><button id="spotcheck-btn" class="secondary" type="button">5종목 청산 경로 재검증</button><button id="spotcheck-csv" class="secondary" disabled type="button">5종목 진단 CSV</button><small id="spotcheck-status">검증 전</small></div><div id="spotcheck-results" class="empty">버튼을 누르면 편도 수수료 설정(기본 0.04%)으로 5종목의 누락 시각을 확인해.</div></section>
<section class="surface"><h2>BTC 장기·단기 추세 <small id="btcts"></small></h2><div class="btc-agg"><div class="agg"><small>장기 · 월/주/일/4시간</small><strong id="long" class="unknown">검사 전</strong></div><div class="agg"><small>단기 · 1시간~1분</small><strong id="short" class="unknown">검사 전</strong></div></div><div id="tf-grid" class="tf-grid"></div><div class="notice">판정: 완료봉 종가와 MA5·MA10 배열. 종가 &gt; MA5 &gt; MA10은 상승, 반대는 하락, 그 외 횡보. 10봉 연속 자료 부족이면 미확인.</div></section>
<section class="surface" id="focus-panel"><h2>관찰 코인 MACD 분석 <small>신호 0건도 확인</small></h2><div class="notice">검색 설정에서 관찰 코인을 지정해. 4시간봉·일봉 MACD(12·26·9)와 빗썸 차트 비교용 일봉(3·10·16)의 골든/데드 상태와 실제 교차가 구분돼.</div><div id="focus-results" class="empty">관찰 코인(예: NEAR)을 지정한 뒤 검색해 줘.</div></section><section class="surface"><h2>코인 후보 <small>BUY / WATCH</small></h2><div class="metrics"><div class="metric"><span>검사 완료</span><strong id="done">0</strong></div><div class="metric"><span>기존 BUY</span><strong id="noldbuy" class="side">0</strong></div><div class="metric"><span>종합 BUY</span><strong id="nbuy" class="up">0</strong></div><div class="metric"><span>전체 WATCH</span><strong id="nwatch" class="side">0</strong></div><div class="metric"><span>초기 WATCH</span><strong id="nearly" class="side">0</strong></div><div class="metric"><span>추세 WATCH</span><strong id="ntrend" class="up">0</strong></div><div class="metric"><span>오류 / 자료 부족</span><strong id="nerr">0</strong></div></div><div class="bar"><i id="progress"></i></div><div class="notice" id="btinfo"></div><div id="watch-summary" class="notice"></div><div id="issue-summary" class="notice"></div><div id="bt-summary" class="notice"></div><div id="watch-bt-summary" class="notice"></div><div id="results" class="empty">검사 시작을 누르면 BUY·WATCH 후보만 나타나.</div></section>
<section class="surface hidden" id="study-panel"><h2>V1.4.9 · 25·50종목 확장 관찰 성과 비교</h2><div class="notice">초기 WATCH / 추세 WATCH / 기존 BUY / 연구 보류를 유동성 관측 품질과 함께 비교해. UNKNOWN은 수익·손실 평균에서 제외하고 데이터 완전도는 별도로 표시. 이 자료는 과거 WATCH 가격경로 가정이며 실제 체결 승률이 아냐.</div><div class="action row"><button id="study-summary-csv" type="button" class="secondary" disabled>그룹 비교 CSV</button><button id="study-rows-csv" type="button" class="secondary" disabled>후보별 비교 CSV</button><small id="study-status">백테스트 검색 완료 후 자동 비교</small></div><div id="study-results" class="notice">같은 시각·같은 검색 범위에서 나온 원본 후보 모두 비교. 화면 WATCH 필터와 관계없이 연구 보류까지 포함.</div></section><section class="surface" id="comparison-panel"><h2>기존 BUY ↔ 종합 BUY 비교</h2><div class="notice">원본 BUY·이전 강화 판정·새 종합 판정을 함께 비교해. 종합 보류도 저장되며 거래 신호가 아니야. 신호 품질 연구용 기준이야.</div><div id="comparison-results" class="empty">검사 전</div></section><div class="foot">모든 신호는 연구용 가상 판정이며 투자 권유나 주문 지시가 아니야. 실제 체결과 손익은 슬리피지·호가·갭으로 달라질 수 있어. 빗썸 공개 API만 사용하고 계정·주문 API는 호출하지 않아.</div>
</main><script>
(()=>{
const $=id=>document.getElementById(id), ST={up:'상승',down:'하락',side:'횡보',unknown:'자료 부족'},TF={'month':'월','week':'주','day':'일','4h':'4시간','1h':'1시간','30m':'30분','15m':'15분','5m':'5분','3m':'3분','1m':'1분'},order=['month','week','day','4h','1h','30m','15m','5m','3m','1m'];
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let mode='live',strategy='swing',busy=false,results=[],issueCases=[],focusDiagnostics=[],spotcheckRows=[],spotcheckFee=.04,spotcheckBusy=false,studyResult=null,problems=0,completed=0,universe=[],runId=0,lastCfg=null;
const dt=(d)=>{const p=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+'T'+p(d.getHours())+':00'};
let d=new Date();d.setDate(d.getDate()-1);d.setHours(15,0,0,0);$('at').value=dt(d);
function modeSet(next){if(busy||spotcheckBusy)return;mode=next;syncHistoricalBreadth();$('study-panel').classList.toggle('hidden',!(strategy==='swing'&&mode==='backtest'));$('live-tab').classList.toggle('on',mode==='live');$('backtest-tab').classList.toggle('on',mode==='backtest');$('backtest-date-field').style.display=mode==='backtest'?'flex':'none';$('backtest-days-field').style.display=mode==='backtest'?'flex':'none';$('live-help').classList.toggle('hidden',mode!=='live');$('backtest-help').classList.toggle('hidden',mode!=='backtest');reset()}
function strategySet(next){if(busy||spotcheckBusy)return;strategy=next;syncHistoricalBreadth();$('study-panel').classList.toggle('hidden',!(strategy==='swing'&&mode==='backtest'));$('scalp-tab').classList.toggle('on',next==='scalp');$('swing-tab').classList.toggle('on',next==='swing');$('tf').value=next==='swing'?'4h':'5m';$('strategy-help').textContent=next==='swing'?'스윙 V1.4.9: 과거 3·7일 검색폭 10/25/50코인 선택(기본 25). 4개 동시 작업 유지. 기존 BUY/WATCH 기준 변경 없음. 과거 완료봉/유동성 검증은 V1.4.8과 동일. 상단에서 10월 31일 WATCH 5개 종목의 5분/1분봉 공백 조사, CSV 제공.':'단타: 빠른 돌파·BTC 상대강도 · +1% 70% / +10% 30% · -2% 손절 / 240분.';$('watch-filter-field').style.display=next==='swing'?'flex':'none';$('comparison-panel').classList.toggle('hidden',next!=='swing');$('focus-panel').classList.toggle('hidden',next!=='swing');$('spotcheck-panel').classList.toggle('hidden',next!=='swing');if(next==='swing'){let d=new Date();d.setDate(d.getDate()-9);d.setHours(15,0,0,0);$('at').value=dt(d)}reset()}
function syncHistoricalBreadth(){
 const show=mode==='backtest'&&strategy==='swing'&&Number($('days').value)>1;
 $('history-breadth-field').style.display=show?'flex':'none';
}
$('days').onchange=()=>{if(busy||spotcheckBusy)return;syncHistoricalBreadth();};
$('scalp-tab').onclick=()=>strategySet('scalp');$('swing-tab').onclick=()=>strategySet('swing');
$('live-tab').onclick=()=>modeSet('live');$('backtest-tab').onclick=()=>modeSet('backtest');
function reset(){results=[];issueCases=[];focusDiagnostics=[];spotcheckRows=[];studyResult=null;$('study-summary-csv').disabled=true;$('study-rows-csv').disabled=true;$('study-status').textContent='백테스트 검색 완료 후 자동 비교';$('study-results').textContent='검색 전에 비교할 데이터가 없어.';$('spotcheck-csv').disabled=true;$('spotcheck-status').textContent='검증 전';$('spotcheck-results').textContent='10월 31일 WATCH 5종목을 전용 버튼으로 재검증할 수 있어.';completed=problems=0;$('focuscsv').disabled=true;$('focus-results').textContent='관찰 코인을 지정하고 검사해 줘.';$('results').innerHTML='검사 시작을 누르면 BUY·WATCH 후보만 나타나.';$('csv').disabled=true;$('issuecsv').disabled=true;$('issue-summary').textContent='';$('watch-summary').textContent='';$('comparison-results').textContent='검사 전';['done','noldbuy','nbuy','nwatch','nearly','ntrend','nerr'].forEach(k=>$(k).textContent='0');$('long').textContent='검사 전';$('short').textContent='검사 전';$('tf-grid').innerHTML='';$('progress').style.width='0%';$('scanstatus').textContent='검사 전';$('btinfo').textContent='';$('bt-summary').textContent='';$('watch-bt-summary').textContent='';$('error').textContent='';$('btcts').textContent=''}
async function post(url,body){const rsp=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const j=await rsp.json();if(!rsp.ok||!j.ok)throw Error(j.error||'데이터 요청 실패');return j}
async function get(url){const rsp=await fetch(url);const j=await rsp.json();if(!rsp.ok||!j.ok)throw Error(j.error||'목록 조회 실패');return j}
$('spotcheck-btn').onclick=async()=>{
 if(busy||spotcheckBusy)return;
 spotcheckBusy=true;
 const btn=$('spotcheck-btn');btn.disabled=true;btn.textContent='검증 중…';
 $('spotcheck-csv').disabled=true;$('spotcheck-results').textContent='1시간봉 재조회 후 30분·5분·1분봉이 실제로 존재하는지 확인 중…';
 $('spotcheck-status').textContent='요청 중';spotcheckRows=[];
 try{
  spotcheckFee=Number($('fee').value);
  const result=await post('/api/replay-spotcheck',{fee:spotcheckFee});
  spotcheckRows=result.items||[];
  const fmt=x=>x==null||!Number.isFinite(Number(x))?'—':Number(x).toFixed(3);
  const solved=spotcheckRows.filter(x=>x.backtest?.status==='RESOLVED').length;
  const recovered=spotcheckRows.reduce((z,x)=>z+(Number(x.backtest?.audit?.recovered_one_hour_candles)||0),0);
  $('spotcheck-status').textContent='완료 '+spotcheckRows.length+'건 / 확정 '+solved+' / 미확정 '+(spotcheckRows.length-solved)+' / 재조회 복구 '+recovered+'봉';
  $('spotcheck-results').innerHTML=spotcheckRows.map(x=>{
   const bt=x.backtest||{},a=bt.audit||{},micro=x.gap_micro||{};
   return '<div class="result"><b>'+esc(x.name)+' ('+esc(x.market)+')</b>'
     +'<p class="notice">가상 결과 '+(bt.status==='RESOLVED'?'확정 '+fmt(bt.net_pct)+'%':'UNKNOWN')+' · 상태 '+esc(a.status_code||'기록 없음')+'</p>'
     +'<p class="notice">최초 누락 시간 '+esc(a.first_unresolved_or_recovered_hour_kst||'없음')+' KST · 최초 누락 '+esc(a.initial_missing_hours??'—')+'시간봉 · 실제 복구 '+esc(a.recovered_one_hour_candles??0)+'개</p>'
     +'<p class="notice">1시간봉 재조회 '+esc(a.hourly_recheck_calls??0)+'회 · 30분봉 진단 '+esc(a.half_hour_diagnostic_calls??0)+'회 · 원인: '+esc(bt.reason||'추가 누락 전에 가상 청산 확인')+'</p>'
     +'<p class="notice">V1.4.9: 5분봉 '+esc(micro.five_min_bars_observed??'미확인')+'개 · 1분봉 '+esc(micro.one_min_bars_observed??'미확인')+'개 · 판정 '+esc(micro.status_text||'추가 검사 대상 아님')+'</p>'
     +'<p class="notice">하위 분봉이 있더라도 1시간봉을 합성해 매매 결과로 계산하지 않아.</p></div>';
  }).join('');
  $('spotcheck-csv').disabled=!spotcheckRows.length;
 }catch(e){$('spotcheck-status').textContent='검증 실패';$('spotcheck-results').textContent='재조회 요청 오류: '+String(e?.message||e);}
 finally{spotcheckBusy=false;btn.disabled=busy;btn.textContent='5종목 청산 경로 재검증';}
};
$('spotcheck-csv').onclick=()=>{
 if(!spotcheckRows.length)return;
 const q=x=>'"'+String(x??'').replace(/"/g,'""')+'"';
 const head=['version','snapshot_kst','fee_each_pct','market','name','status','net_pct','reason','reason_code','tp1','tp2','stop','minutes','audit_status','initial_missing_hours','sample_missing_kst','first_examined_missing_kst','hourly_recheck_calls','half_hour_diagnostic_calls','total_candle_api_calls','recovered_one_hour_candles','lower_tf_actual_bars_in_missing_hour','initial_api_error','detail_error','source_limitations','v146_micro_status','v146_5m_bars','v146_1m_bars','v146_5m_samples_kst','v146_1m_samples_kst','v146_micro_errors_json','v146_micro_explanation'];
 const records=[head.map(q).join(',')];
 for(const x of spotcheckRows){const b=x.backtest||{},a=b.audit||{},micro=x.gap_micro||{};
  records.push(['BTC-MTF-V1.4.9','2026-10-31 15:00',spotcheckFee,x.market,x.name,b.status,b.net_pct??'',b.reason||'',b.reason_code||'',b.tp1,b.tp2,b.stop,b.minutes??'',a.status_code||'',a.initial_missing_hours??'',(a.initial_missing_samples_kst||[]).join(' / '),a.first_unresolved_or_recovered_hour_kst||'',a.hourly_recheck_calls??'',a.half_hour_diagnostic_calls??'',a.total_candle_api_calls??'',a.recovered_one_hour_candles??'',a.lower_timeframe_bars_in_missing_hour??'',a.initial_api_error||'',a.detail_error||'',a.source_limitations||'',micro.status_code||'',micro.five_min_bars_observed??'',micro.one_min_bars_observed??'',(micro.five_min_samples_kst||[]).join(' / '),(micro.one_min_samples_kst||[]).join(' / '),JSON.stringify(micro.errors||{}),micro.status_text||''].map(q).join(','));
 }
 const blob=new Blob(['\ufeff'+records.join('\n')],{type:'text/csv;charset=utf-8'});
 const url=URL.createObjectURL(blob),a=document.createElement('a');
 a.href=url;a.download='BITHUMB_BTC_MTF_V1_4_9_SPOTCHECK_202610311500.csv';
 document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000);
};
const studyLabels={BUY:'종합 BUY',EARLY_WATCH:'초기 WATCH',TREND_WATCH:'추세 WATCH',RESEARCH:'연구 보류',ORIGINAL_BUY:'기존 원본 BUY',Q4:'유동성 4/4',Q23:'유동성 2~3/4',Q01:'유동성 0~1/4',Q_UNKNOWN:'유동성 자료 없음',COMPLETE_24H:'과거 24시간봉 완전',INCOMPLETE_24H:'과거 24시간봉 불완전',UNKNOWN_24H:'관측 이력 미확인'};
function studyFmt(n,suffix=''){return n===null||n===undefined||!Number.isFinite(Number(n))?'—':Number(n).toLocaleString('ko-KR',{maximumFractionDigits:2})+suffix}
function studyTable(rows){
 const body=rows.map(g=>'<tr><td>'+esc(studyLabels[g.label]||g.label)+'</td><td>'+g.candidates+' / '+g.unique_markets+'</td><td>'+g.resolved+' / '+g.unknown+'</td><td>'+g.positive+' / '+g.nonpositive+'</td><td>'+studyFmt(g.resolution_rate_pct,'%')+'</td><td>'+studyFmt(g.mean_hypothetical_net_pct,'%')+(g.small_sample&&g.resolved?' ⚠':'')+'</td></tr>').join('');
 return '<div class="study-scroll"><table><thead><tr><th>분류</th><th>후보 / 고유코인</th><th>확정 / 미확정</th><th>양수 / 비양수</th><th>확정률</th><th>확정분 평균</th></tr></thead><tbody>'+body+'</tbody></table></div>';
}
function renderStudy(){
 if(!studyResult)return;
 const z=studyResult,t=z.summary||{},uniqueDays=(z.by_day||[]).length;
 $('study-status').textContent='비교 완료 · 후보 '+t.candidates+'건 · '+uniqueDays+'개 기준일 · 실재 고유 종목 '+t.unique_markets+'개 · 확정 '+t.resolved+'건 · 미확정 '+t.unknown+'건';
 $('study-results').innerHTML='<p class="notice"><b>전체 검색 '+z.scanned+'건 · 검색 자료 오류 '+z.issues+'건 · 후보 '+t.candidates+'건 · 편도 수수료 '+Number(z.fee_each_pct).toFixed(2)+'%</b> · 동일 코인이 다른 날짜에 있으면 개별 스냅샷으로 집계되며 독립 표본은 아냐. 7일 검사 범위는 현재 거래대금 상위 최대 10개, 3일은 최대 20개 코인으로 제한돼.</p>'
  +'<h3>신호별 비교 · 모두 관찰용 가상 경로</h3>'+studyTable(z.by_kind||[])
  +'<h3>신호 시점 이전 유동성 4개 기준</h3>'+studyTable(z.by_quality||[])
  +'<h3>과거 24시간 1시간봉 자료 완전도</h3>'+studyTable(z.by_coverage||[])
  +'<details><summary>추가 비교: 기존 BUY / 날짜별 / 신호×유동성</summary>'
  +'<h3>기존 원본 BUY</h3>'+studyTable(z.original_buy?[z.original_buy]:[])
  +'<h3>날짜별</h3>'+studyTable(z.by_day||[])
  +'<h3>신호×유동성 (후보가 있는 그룹만)</h3>'+studyTable((z.by_kind_quality||[]).filter(x=>x.candidates))
  +'</details>'
  +'<p class="notice"><b>해석 주의:</b> '+esc((z.warnings||[]).join(' · '))+'</p>';
 $('study-summary-csv').disabled=false;$('study-rows-csv').disabled=false;
}
async function refreshStudy(){
 if(mode!=='backtest'||strategy!=='swing')return;
 $('study-status').textContent='후보 유형·선행 유동성·가상 경로 비교 계산 중…';
 const payload=results.map(x=>({
  market:x.market,at:x.at,kind:x.signal.watch_kind||'RESEARCH',
  legacy_signal:x.signal.legacy_signal||'NONE',
  liquidity_audit:x.signal.liquidity_audit||{},
  backtest:x.signal.backtest||{}
 }));
 const response=await post('/api/study-v147',{
  mode:'backtest',strategy:'swing',fee:Number(lastCfg?.fee||.04),
  scanned:completed,issues:problems,items:payload
 });
 studyResult=response.study;renderStudy();
}
function exportStudyCsv(rows,name,headers,fields){
 if(!studyResult)return;
 const q=x=>'"'+String(x??'').replace(/"/g,'""')+'"';
 const lines=[headers.map(q).join(',')];
 for(const row of rows)lines.push(fields.map(k=>q(row[k]??'')).join(','));
 const url=URL.createObjectURL(new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}));
 const a=document.createElement('a');a.href=url;
 a.download='BITHUMB_BTC_MTF_V1_4_9_'+name+'_'+String(lastCfg?.at||dt(new Date())).replace(/[^0-9]/g,'').slice(0,12)+'.csv';
 document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000);
}
$('study-summary-csv').onclick=()=>{
 if(!studyResult)return;
 const z=studyResult,rows=[];
 for(const [type,items] of [['TOTAL',[z.summary]],['LEGACY_BUY',[z.original_buy]],['KIND',z.by_kind],['QUALITY',z.by_quality],['COVERAGE',z.by_coverage],['KIND_QUALITY',z.by_kind_quality],['DAY',z.by_day]]){
  for(const x of items||[])if(x)rows.push({group_type:type,...x});
 }
 const fields=['version','fee_each_pct','group_type','label','candidates','unique_markets','unique_snapshots','resolved','unknown','positive','nonpositive','resolution_rate_pct','positive_fraction_resolved_pct','mean_hypothetical_net_pct','median_hypothetical_net_pct','min_hypothetical_net_pct','max_hypothetical_net_pct','small_sample'];
 exportStudyCsv(rows.map(x=>({version:'BTC-MTF-V1.4.9',fee_each_pct:z.fee_each_pct,...x})),'COHORT_SUMMARY',fields,fields);
};
$('study-rows-csv').onclick=()=>{
 if(!studyResult)return;
 const fields=['version','fee_each_pct','at','market','kind','legacy_signal','quality','coverage','quality_checks_passed','hourly_bars_observed','historical_turnover_krw','status','net_pct','backtest_reason'];
 exportStudyCsv((studyResult.rows||[]).map(x=>({version:'BTC-MTF-V1.4.9',fee_each_pct:studyResult.fee_each_pct,...x})),'COHORT_DETAILS',fields,fields);
};

function btcDraw(ctx){$('long').textContent=ST[ctx.long]||'미확인';$('long').className=ctx.long;$('short').textContent=ST[ctx.short]||'미확인';$('short').className=ctx.short;$('btcts').textContent='· '+ctx.asof_kst+' KST';$('tf-grid').innerHTML=order.map(k=>{const x=ctx.tf[k]||{state:'unknown'},why=x.reason?' · '+x.reason:'';return `<div class="tf" title="${esc(why)}"><div class="name">BTC ${TF[k]}</div><div class="status ${x.state}">${ST[x.state]||'자료 부족'}</div></div>`}).join('')}
function classifyIssue(reason){
 const msg=String(reason||'알 수 없는 문제');
 if(/이력|실제 캔들 수|연속 마감봉|밀도 부족|관측 간격/.test(msg))return '이력/캔들 공백';
 if(/최신|오래됨|동일 시각|신선도/.test(msg))return '시각/최신봉';
 if(/HTTP|API|fetch|timeout|요청|조회 실패|제한/.test(msg))return 'API/통신';
 return '기타';
}
function visibleResults(){
 const filter=$('watch-filter').value;
 return results.filter(x=>{
  if(strategy!=='swing')return true;
  const sig=x.signal,kind=sig.watch_kind||'',score=Number(sig.metrics?.score||0);
  if(filter==='all')return true;
  if(filter==='nine')return score>=9;
  if(filter==='ten')return score>=10;
  if(filter==='early')return kind==='EARLY_WATCH';
  if(filter==='trend')return kind==='TREND_WATCH';
  return kind==='BUY'||kind==='EARLY_WATCH'||kind==='TREND_WATCH';
 });
}
$('watch-filter').onchange=()=>{if(!busy){summary();renderResults();}};
function issueSummary(){
 if(!issueCases.length){$('issue-summary').textContent='';return}
 const counts={};for(const x of issueCases){const k=classifyIssue(x.reason);counts[k]=(counts[k]||0)+1;}
 const kinds=Object.entries(counts).sort((a,b)=>b[1]-a[1]).map(([k,v])=>k+' '+v+'건').join(' · ');
 const ex=issueCases[0];
 $('issue-summary').textContent='자료 확인 필요 '+issueCases.length+'건 ('+kinds+'). 예: '+ex.market+' — '+ex.reason+'. 정확한 종목별 이유는 문제 내역 CSV로 저장 가능.';
}
function trackBacktest(kind,label){
 const a=results.filter(x=>x.signal.watch_kind===kind),d=a.filter(x=>x.signal.backtest?.status==='RESOLVED'),w=d.filter(x=>Number(x.signal.backtest.net_pct)>0).length;
 const avg=d.length?(d.reduce((z,x)=>z+Number(x.signal.backtest.net_pct),0)/d.length).toFixed(2)+'%':'—';
 return label+' '+a.length+'건 / 경로 확인 '+d.length+'건(수익 '+w+'·손실 '+(d.length-w)+') / 미확정 '+(a.length-d.length)+' / 확인분 가상 평균 '+avg;
}
function summary(){
 issueSummary();
 const buy=results.filter(x=>x.signal.signal==='BUY').length,oldbuy=results.filter(x=>x.signal.legacy_signal==='BUY').length,watchAll=results.filter(x=>x.signal.signal==='WATCH').length,early=results.filter(x=>x.signal.watch_kind==='EARLY_WATCH').length,trendCount=results.filter(x=>x.signal.watch_kind==='TREND_WATCH').length,research=results.filter(x=>x.signal.signal==='RESEARCH').length;
 const watch=visibleResults().filter(x=>x.signal.signal==='WATCH').length;
 $('done').textContent=completed;$('noldbuy').textContent=strategy==='swing'?oldbuy:buy;$('nbuy').textContent=buy;$('nwatch').textContent=watch;$('nearly').textContent=early;$('ntrend').textContent=trendCount;$('nerr').textContent=problems;
 $('watch-summary').textContent=strategy==='swing'?'원본 BUY '+oldbuy+'건 → 엄격 BUY '+buy+'건 / 초기 WATCH '+early+'건 / 추세 WATCH '+trendCount+'건 · 화면 WATCH '+watch+'건 · 연구 보류 '+research+'건 · 원본과 각 판정 CSV 보존':'단타는 기존 판정 유지';
 $('progress').style.width=universe.length?Math.round(completed/universe.length*100)+'%':'0%';
 $('scanstatus').textContent=(busy?'검사 중 ':'검사 완료 ')+completed+'/'+universe.length;
 const ok=results.filter(x=>x.signal.signal==='BUY'&&x.signal.backtest&&x.signal.backtest.status==='RESOLVED'),unk=buy-ok.length,wins=ok.filter(x=>x.signal.backtest.net_pct>0).length;
 const oldResolved=results.filter(x=>x.signal.legacy_signal==='BUY'&&x.signal.backtest?.status==='RESOLVED');
 const oldWins=oldResolved.filter(x=>x.signal.backtest.net_pct>0).length;
 $('watch-bt-summary').textContent=mode==='backtest'&&strategy==='swing'?trackBacktest('EARLY_WATCH','초기 WATCH')+' · '+trackBacktest('TREND_WATCH','추세 WATCH')+' · WATCH는 진입 거래가 아니라 참고 가격 경로':'';
 $('bt-summary').textContent=mode!=='backtest'?'':strategy==='swing'
   ?'기존 BUY '+oldbuy+'건 (청산 확정 '+oldResolved.length+', 확정 승률 '+(oldResolved.length?(oldWins/oldResolved.length*100).toFixed(1)+'%':'—')+') / 종합 BUY '+buy+'건 (청산 확정 '+ok.length+', 확정 승률 '+(ok.length?(wins/ok.length*100).toFixed(1)+'%':'—')+'). 미확정 제외 · 현재 거래대금 선택 편향 존재.'
   :'BUY '+buy+'건 · 청산 확정 '+ok.length+' · 미확정 '+unk+' · 확정 승률 '+(ok.length?(wins/ok.length*100).toFixed(1)+'%':'—')+' · 확정 평균 순익 '+(ok.length?(ok.reduce((v,x)=>v+Number(x.signal.backtest.net_pct),0)/ok.length).toFixed(3)+'%':'—');
}
const macdStateText=s=>({GOLDEN:'골든 상태 (MACD선이 시그널선 위)',DEAD:'데드 상태 (MACD선이 시그널선 아래)',FLAT:'두 선 동일',UNKNOWN:'자료/최근 연속봉 부족'})[s]||'알 수 없음';
const macdEventText=s=>({GOLDEN_CROSS:'골든크로스 발생',DEAD_CROSS:'데드크로스 발생',NONE:'이번 봉 교차 없음'})[s]||'불명';
function explainCard(d){
 if(!d)return '';
 if(d.status==='INSUFFICIENT')return '<div class="insight-wrap">근거 점수: 완료봉 이력 부족</div>';
 const names=[['structure','Structure · 구조'],['timing','Timing · 타이밍'],
 ['trend','Trend · 추세'],['setup','Setup · 셋업'],
 ['entry','Entry · 진입 준비'],['safety','Safety · 안전(높을수록 안전)']];
 const fmt=x=>x===null||x===undefined||!Number.isFinite(Number(x))?'—':Number(x).toLocaleString('ko-KR',{maximumFractionDigits:8});
 const scores=names.map(([k,t])=>'<div class="insight-score"><strong>'+fmt(d.scores?.[k])+'</strong><span>'+esc(t)+'/100</span></div>').join('');
 const down=d.sell_pressure_proxy?.red_candle_turnover_pct;
 const red='<div class="insight-score"><strong>'+(down==null?'—':fmt(down)+'%')+'</strong><span>음봉 거래대금 비중 · 매도 압력 참고</span></div>';
 const reasons=names.map(([k,t])=>'<div class="insight-proof"><b>'+esc(t)+'</b>'+
  (d.factors?.[k]||[]).map(v=>'<span>'+(v.passed?'✓ ':'× ')+esc(v.label)+'</span>').join('')+'</div>').join('');
 const p=d.plan||{},c=d.context||{};
 const levels=[['돌파 관찰선',p.trigger_ref],['가상 손절선',p.stop_ref],
  ['수수료 포함 본전 참고',p.breakeven_ref],['1차 가상 목표 (1.5R)',p.target1_ref],
  ['2차 가상 목표 (3.0R)',p.target2_ref],['손절 거리',p.risk_pct==null?null:fmt(p.risk_pct)+'%']];
 const levelsHtml=levels.map(([label,v])=>'<div><b>'+esc(v==null?'미산정':fmt(v)==='—'?v:fmt(v))+(typeof v==='string'&&v.includes('%')?'':'')+'</b><small>'+esc(label)+'</small></div>').join('');
 const warnings=(d.warnings||[]).map(t=>'⚠ '+esc(t)).join(' · ');
 const dayTurnover=c.last_24h_closed_turnover_krw==null?'연속봉 부족으로 미산정':fmt(c.last_24h_closed_turnover_krw/100000000)+'억 원';
 const ctx='완료봉 24시간 거래대금 합계 '+dayTurnover+' · 4H 거래대금 배수 '+fmt(c.h4_rvol_turnover)+'배 · 4H RSI '+fmt(c.h4_rsi)+' · ATR '+fmt(c.h4_atr_pct)+'% · MA7 이격 '+fmt(c.h4_ma7_distance_pct)+'%';
 return '<section class="insight-wrap"><b>설명형 점수 · 4조건씩 / 100점 (연구용)</b>'
  +'<div class="insight-grid">'+scores+red+'</div>'
  +'<p class="entry-note">'+esc(d.summary||'진입 확정 전')+'</p>'
  +'<p class="notice">'+esc(ctx)+'</p>'
  +(warnings?'<p class="notice">'+warnings+'</p>':'')
  +'<b>별도 연구용 관찰 계획 (기존 120시간 백테스트 손절·목표와 다름)</b>'
  +'<p class="notice">다음 4시간봉 몸통이 돌파 관찰선 위에서 마감 + 같은 봉 거래대금 2.0배 이상이 확인돼야 재판정해. 현재 봉에서는 충족 여부를 알 수 없어.</p>'
  +'<div class="insight-plan">'+levelsHtml+'</div>'
  +'<p class="notice">'+esc(p.note||'')+'</p>'
  +(p.plan_valid?'':'<p class="notice">⚠ 손절 거리/봉 연속성 때문에 이 참고 계획은 부적합. 목표가는 표시하지 않아.</p>')
  +'<details><summary>점수별 통과/미통과 근거 펼치기</summary>'+reasons+'</details>'
  +'<p class="notice">음봉 거래대금 비율은 실제 매도 체결강도가 아니야. 호가벽·체결강도 미수집. 실거래 체결·호가단위·슬리피지 미검증. 점수는 확률 아님.</p></section>';
}
function liquidityCard(x){
 if(!x)return '';
 const amt=(Number(x.observed_turnover_24h_krw||0)/100000000).toFixed(2);
 const caveat=x.observed_turnover_is_complete?'24시간 완전 관측':'캔들 누락으로 거래대금 합계 불완전';
 return '<div class="insight-wrap"><b>선행 유동성 관측 '+esc(x.checks_passed??0)+'/4 · '+esc(x.label||'자료 없음')+'</b>'
  +'<p class="notice">스냅샷 이전 실제 1시간봉 '+esc(x.hourly_bars_observed??'—')+'/24 · 최근 6시간 '+esc(x.recent_6h_bars_observed??'—')+'/6 · 관측 거래대금 '+esc(amt)+'억원</p>'
  +'<p class="notice">'+esc(caveat)+' · 최장 연속 누락 '+esc(x.longest_missing_hours??'—')+'시간</p>'
  +'<p class="notice">주의: '+esc((x.warnings||[]).join(' / ')||'추가 경고 없음')+'</p>'
  +'<p class="notice">이 점수는 관측 품질 설명이며 성공 확률·BUY 조건이 아니야. 미래 청산 데이터 사용 안 함.</p></div>';
}
function macdCard(x,title){
 if(!x)return '<div class="result">'+esc(title)+' · 데이터 없음</div>';
 const last=x.last_event==='NONE'?'최근 24봉 교차 확인되지 않음':macdEventText(x.last_event)+' · '+esc(x.last_event_kst||'')+' ('+x.bars_since_cross+'봉 전)';
 return '<div class="result"><b>'+esc(title)+'</b><p class="reason">현재: '+macdStateText(x.state)+' · 완료봉 '+esc(x.closed_kst||'')+'</p>'
   +'<p class="notice">이번 완료봉: '+macdEventText(x.event)+' · 마지막 교차: '+last+'</p>'
   +'<p class="notice">MACD '+(x.macd??'—')+' / 시그널 '+(x.signal??'—')+' / 히스토그램 '+(x.hist??'—')+' (이전 '+(x.prev_hist??'—')+')</p></div>';
}
function renderFocus(){
 if(strategy!=='swing')return;
 if(!focusDiagnostics.length){$('focus-results').textContent='신호와 관계없이 관심 코인을 지정하면 MACD 진단을 보여줘.';return;}
 $('focus-results').innerHTML=focusDiagnostics.map(x=>{
  const d=x.diagnostic||{},v=d.macd||{},g=d.strict_gate||{},combined=d.combined_gate||{};
  return '<div><h3>'+esc(x.name||x.market)+' · '+esc(x.at)+'</h3>'
    +'<p class="notice">원본 '+esc(d.legacy_signal||'NONE')+' · 점수 '+(d.score??'—')+'/10 · RSI '+(d.rsi14??'—')+' · V1.4.3 '+esc(combined.signal||'RESEARCH')+' · 분리 WATCH '+esc(d.watch_tracks?.label||'연구 보류')+'</p>'+'<p class="notice">초기 WATCH 미충족: '+esc((d.watch_tracks?.early_missing||[]).join(' / ')||'없음')+'</p>'+'<p class="notice">추세 WATCH 미충족: '+esc((d.watch_tracks?.trend_missing||[]).join(' / ')||'없음')+'</p>'
    +liquidityCard(d.liquidity_audit)+explainCard(d.explain)+macdCard(v.h4,'4시간봉 MACD(12,26,9)')
    +macdCard(v.day,'일봉 MACD(12,26,9)')
    +macdCard(v.day_chart,'일봉 MACD(3,10,16) · 차트 비교')
    +'</div>';
 }).join('');
}
function renderComparison(){
 const old=results.filter(x=>x.signal.legacy_signal==='BUY');
 if(!old.length){$('comparison-results').textContent='기존 BUY 0건 · 비교할 신호가 없어.';return;}
 $('comparison-results').innerHTML=old.map(x=>{
  const si=x.signal,g=si.strict_gate||{},bt=si.backtest||{};
  const out=mode!=='backtest'?'LIVE · 사후 결과 미측정':
     bt.status==='RESOLVED'?'120시간 가상 결과 '+Number(bt.net_pct).toFixed(3)+'% (확정)':
     '미확정 · '+String(bt.reason||'봉 자료 부족');
  return '<div class="result"><strong>'+esc(x.name||x.market)+' ('+esc(x.market)+')</strong> · <span class="badge buy">기존 BUY</span> → <span class="badge '+si.signal.toLowerCase()+'">'+(si.signal==='BUY'?'엄격 BUY':si.signal==='WATCH'?esc(si.watch_tracks?.label||'관찰 WATCH'):'연구 보류')+'</span>'
  +'<p class="notice">반등 '+Number(si.metrics.score)+'/10 · '+esc(x.at||'')+' · '+esc(out)+'</p>'+'<p class="notice">V1.4.3 '+esc(si.v143_signal||'RESEARCH')+' · 초기 WATCH '+(si.watch_tracks?.early_ready?'통과':'보류')+' · 추세 WATCH '+(si.watch_tracks?.trend_ready?'통과':'보류')+'</p>'
  +(si.combined_gate?.missing_watch?.length?'<p class="reason">종합 미충족: '+esc(si.combined_gate.missing_watch.join(' / '))+'</p>':'<p class="reason">종합 관찰조건 통과</p>')+(si.signal!=='BUY'?'<p class="reason">종합 BUY 미충족: '+esc((si.combined_gate?.missing_buy||[]).join(' / '))+'</p>':'')
  +'</div>';
 }).join('');
}
function renderResults(){
 if(strategy==='swing'){renderComparison();renderFocus();}
 const sorted=visibleResults().sort((a,b)=>(a.signal.signal==='BUY'?0:1)-(b.signal.signal==='BUY'?0:1)||b.signal.metrics.relative_pct-a.signal.metrics.relative_pct);
 if(!sorted.length){$('results').textContent=results.length?'선택한 WATCH 종류의 후보가 없어. 다른 WATCH 필터 또는 연구 전체를 확인해 줘.':'원본 반등 후보가 없어. 자료 부족은 문제 내역 CSV를 확인해 줘.';return;}
 const num=x=>Number.isFinite(Number(x))?Number(x).toLocaleString('ko-KR',{maximumFractionDigits:8}):'—';
 $('results').classList.remove('empty');
 $('results').innerHTML=sorted.map(x=>{
  const si=x.signal,m=si.metrics,p=si.plan,bt=si.backtest,fee=Number(lastCfg.fee),base=Number(m.close);
  const audittext=mode==='backtest'&&strategy==='swing'&&bt?.audit?'<p class="notice">1시간봉 검증: '+esc(bt.audit.status_code||'')+' · 최초 누락 '+esc(bt.audit.first_unresolved_or_recovered_hour_kst||'없음')+' KST · 실제 복구 '+esc(bt.audit.recovered_one_hour_candles??0)+'봉 · 재조회 '+esc(bt.audit.hourly_recheck_calls??0)+'회</p>':'';
  const bttext=mode!=='backtest'?'':bt&&bt.status==='RESOLVED'
   ?'<p class="pnl '+(bt.net_pct>=0?'up':'down')+'">'+(si.signal==='BUY'?'BUY 가상거래':'진입 없는 참고 가격 경로')+' · '+(strategy==='swing'?'120시간':'240분')+' 순익 '+(bt.net_pct>=0?'+':'')+Number(bt.net_pct).toFixed(3)+'% · '+(bt.stop?'손절':bt.tp2?'2차 익절':bt.tp1?'1차 익절 후 청산':'만료')+(bt.ambiguous?' · 동일 봉 순서 불명':'')+'</p>'
   :'<p class="pnl side">미확정: '+esc(bt&&bt.reason||'봉 누락')+'</p>';
  return '<details class="result"><summary><span class="badge '+si.signal.toLowerCase()+'">'+si.signal+'</span><small>'+esc(si.stage||'')+'</small><span class="score-chip">'+(m.score_total===10?m.score+'/10점':'')+'</span><b>'+esc(x.name||x.market)+' <small>'+esc(x.market)+'</small></b><span class="entry-price '+(si.signal==='WATCH'?'watch-entry':'')+'">'+(si.signal==='BUY'?'진입 참고가 ':si.signal==='WATCH'?'WATCH 관찰가 ':'연구 참고가 ')+num(base)+'원</span><span class="push">'+esc(x.at||'')+' · 상대강도 '+(m.relative_pct>=0?'+':'')+Number(m.relative_pct).toFixed(2)+'%p · 상세 ▾</span></summary><div class="reason">'
   +'<div class="entry-note"><strong>'+(si.signal==='BUY'?'BUY 진입 참고가: ':si.signal==='WATCH'?'WATCH 관찰 기준가: ':'연구용 참고가: ')+num(base)+'원</strong> · '+(si.signal==='BUY'?'신호가 확인된 마지막 마감봉 종가. 실제 주문가는 다를 수 있어.':si.signal==='WATCH'?'BUY 조건 미충족. 관찰 참고 가격이며 매수를 뜻하지 않아.':'연구 기록만 보관. 종합 기준 불합격이며 실제 진입 추천이 아님.')+'</div>'
   +'<div class="entry-metrics"><span>1차 익절 예상 '+num(base*(1+p.tp1_gross_pct/100))+'원</span><span>2차 익절 예상 '+num(base*(1+p.tp2_gross_pct/100))+'원</span><span>가상 손절 '+num(base*(1+p.stop_gross/100))+'원</span></div>'
   +(si.legacy_signal==='BUY'&&si.signal!=='BUY'?'<p class="entry-note">기존 V1.3.3 BUY였지만 V1.4.9 종합 기준 미달 · 신규 BUY 아님</p>':'')+(si.signal==='RESEARCH'?'<p class="entry-note">연구 보류: BUY/WATCH 신호 아님, 원본 기록·탈락 사유만 보존.</p>':'')+(si.watch_kind==='EARLY_WATCH'?'<p class="entry-note">초기 반등 관찰용. 상위 추세 미확정 가능 · BUY 및 진입 신호 아님.</p>':'')+(si.watch_kind==='TREND_WATCH'?'<p class="entry-note">추세 확인 WATCH. BUY 진입 신호 아님.</p>':'')+(strategy==='swing'?liquidityCard(si.liquidity_audit)+explainCard(si.explain):'')+(strategy==='swing'&&m.macd_h4?macdCard(m.macd_h4,'4시간봉 MACD(12,26,9)'):'')+si.explanation.map(z=>'<p>• '+esc(z)+'</p>').join('')
   +'<p>가상 청산: 순익 +'+p.tp1_net+'% '+Math.round(p.tp1_fraction*100)+'%, +'+p.tp2_net+'% '+Math.round(p.tp2_fraction*100)+'% · 손절 '+p.stop_gross+'% · '+p.max_minutes/60+'시간</p>'
   +'<p>완료봉 참고가 '+num(base)+'원 · 1차 예상 '+num(base*(1+p.tp1_gross_pct/100))+'원 · 2차 '+num(base*(1+p.tp2_gross_pct/100))+'원 · 가상 손절 '+num(base*(1+p.stop_gross/100))+'원'+(m.support?' · 구조 지지 '+num(m.support)+'원':'')+' (실제 체결가 아님)</p>'
   +'<p>수수료 편도 '+fee.toFixed(2)+'% · 완료봉 '+esc(m.closed_kst)+' KST</p>'+bttext+audittext+'</div></details>';
 }).join('');
}
$('start').onclick=async()=>{
 if(busy||spotcheckBusy)return;reset();busy=true;runId++;const id=runId,btn=$('start');btn.disabled=true;btn.textContent='검색 중…';
 const cfg={mode,strategy,tf:$('tf').value,fee:Number($('fee').value),min_turnover_24h_krw:Number($('turnover').value)};
 if(mode==='backtest')cfg.at=$('at').value.replace('T',' ')+':00';lastCfg={...cfg};
 try{
  const list=await get('/api/universe');const scope=$('scope').value;
  let items=list.items.filter(x=>Number(x.turnover)>=cfg.min_turnover_24h_krw);
  const daysCount=mode==='backtest'?Number($('days').value):1;
  const expandedSwing=mode==='backtest'&&strategy==='swing'&&daysCount>1;
  const historicalLimit=expandedSwing?Number($('history-breadth').value):(daysCount===7?10:20);
  // Historical swing breadth overrides the generic LIVE/one-day scope.
  // Keep the existing 3/7-day scalp cap; no change to signal rules.
  if(expandedSwing){
   if(![10,25,50].includes(historicalLimit))throw Error('과거 스윙 검색 대상은 10·25·50 중 선택');
   items=items.slice(0,historicalLimit);
  }else{
   items=scope==='all'?items:items.slice(0,Number(scope));
   if(daysCount>1)items=items.slice(0,historicalLimit);
  }
  const pinned=$('focus').value.trim().toUpperCase().replace(/^KRW-/, '');let focusedMarket='';if(pinned){const found=list.items.find(x=>x.market==='KRW-'+pinned||x.name===$('focus').value.trim());if(found){focusedMarket=found.market;if(!items.some(x=>x.market===found.market))items=[found,...items];}else throw Error('관찰 코인을 찾지 못했어: '+pinned);}if(!items.length)throw Error('검색 가능한 종목이 없어');
  const dates=[];
  for(let day=0;day<daysCount;day++){
   const c={...cfg};
   if(mode==='backtest'){const d=new Date(cfg.at.replace(' ','T'));d.setDate(d.getDate()-day);c.at=dt(d).replace('T',' ')+':00';}
   dates.push(c);
  }
  lastCfg.historical_coin_limit=expandedSwing?historicalLimit:null;
  lastCfg.actual_selected_markets=items.length;
  lastCfg.history_breadth_mode=expandedSwing?'SWING_MULTI_DAY':(daysCount>1?'SCALP_MULTI_DAY':'GENERAL_SCOPE');
  universe=dates.flatMap(c=>items.map(x=>({...x,at:c.at||'LIVE'})));
  $('btinfo').textContent=mode==='backtest'
    ?daysCount+'일 × 하루 같은 시각 1회 · 실제 선택 '+items.length+'종목'+(expandedSwing?' / 설정 최대 '+historicalLimit+'종목(일반 검색 범위 대신 적용)':'')+' · 총 '+universe.length+'회 검사 · 4개 동시 작업 · 거래대금 하한 '+($('turnover').selectedOptions[0]?.textContent||'')+' · 현재 상장·거래대금 기준으로 선정: 과거 시점 모집단 아님! 생존·미래정보 편향 주의.'
    :'24시간 거래대금 '+($('turnover').selectedOptions[0]?.textContent||'')+' · BTC 후보 제외 · 반등 점수 및 진입 위치 필터';
  for(const c of dates){
   const b=await post('/api/btc',c);
   if(runId!==id)break;
   btcDraw(b.context);
   if(mode==='live')c.snapshot_at=b.context.asof_kst;
   let cursor=0;
   const work=async()=>{while(cursor<items.length){
    const coin=items[cursor++];
    try{
     const focused=!!focusedMarket&&coin.market===focusedMarket;
     const j=await post('/api/coin',{...c,market:coin.market,want_diagnostic:focused});
     if(focused&&j.diagnostic)focusDiagnostics.push({market:coin.market,name:coin.name,at:c.at||c.snapshot_at||'LIVE',diagnostic:j.diagnostic});
     if(j.signal)results.push({market:coin.market,name:coin.name,at:c.at||c.snapshot_at||'LIVE',turnover24h:coin.turnover,signal:j.signal});
     if(j.issue){problems++;issueCases.push({market:coin.market,name:coin.name,at:c.at||c.snapshot_at||'LIVE',turnover24h:coin.turnover,reason:j.issue});}
    }catch(e){problems++;issueCases.push({market:coin.market,name:coin.name,at:c.at||c.snapshot_at||'LIVE',turnover24h:coin.turnover,reason:'요청/API 오류: '+String(e?.message||e)});}
    completed++;
    if(completed%4===0||completed===universe.length){summary();renderResults();}
    await Promise.resolve();
   }};
   await Promise.all(Array.from({length:Math.min(4,items.length)},work));
  }
 }catch(ex){$('error').textContent='검사를 완료하지 못했어: '+ex.message;}
 finally{summary();renderResults();$('csv').disabled=!completed;$('issuecsv').disabled=!issueCases.length;$('focuscsv').disabled=!focusDiagnostics.length;
  if(mode==='backtest'&&strategy==='swing'){try{await refreshStudy();}catch(err){$('study-status').textContent='비교 실패: '+String(err?.message||err);$('study-results').textContent='전체 후보 CSV는 정상 저장 가능. 연구 비교 요청만 실패했어.';}}
  busy=false;btn.disabled=false;btn.textContent='검색 시작';
 }
};
$('focuscsv').onclick=()=>{
 if(!focusDiagnostics.length)return;
 const cols=['version','market','name','snapshot_kst','closed_kst','reference_price_krw','rebound_score','rsi14','legacy_signal','strict_gate_passed','macd_view','macd_params','macd_state','macd_current_bar_event','macd_last_event','macd_last_event_kst','bars_since_cross','continuous','macd_value','macd_signal','macd_hist','macd_hist_previous','structure_score','timing_score','trend_score','setup_score','entry_score','safety_score','red_candle_turnover_pct','breakout_watch_ref','hypothetical_stop_ref','fee_breakeven_ref','hypothetical_target1_ref','hypothetical_target2_ref','hypothetical_stop_distance_pct','explain_summary','explain_warnings','explain_factors_json','orderbook_status','combined_signal','combined_watch_ready','combined_buy_ready','combined_watch_failed','combined_buy_failed','combined_checks_json','completed_4h_breakout_ref','completed_4h_breakout_ok','v144_watch_kind','v144_early_ready','v144_trend_ready','v144_early_failed','v144_trend_failed','v144_early_checks_json','v144_trend_checks_json','v144_warnings','v144_prior_v143_signal','v146_quality_label','v146_quality_kind','v146_quality_passed','v146_hourly_bars_24','v146_recent6h_bars','v146_max_missing_streak_hours','v146_observed_turnover_24h_krw','v146_observed_turnover_complete','v146_missing_hours_kst','v146_quality_warnings'];
 const q=x=>'"'+String(x??'').replace(/"/g,'""')+'"';
 const lines=[cols.map(q).join(',')];
 for(const x of focusDiagnostics){const d=x.diagnostic||{};
  for(const key of ['h4','day','day_chart']){const v=d.macd?.[key]||{};
   lines.push(['BTC-MTF-V1.4.9',x.market,x.name,x.at,d.closed_kst,d.close,d.score,d.rsi14,d.legacy_signal,d.strict_gate?.passed,key,v.params,v.state,v.event,v.last_event,v.last_event_kst,v.bars_since_cross,v.continuous,v.macd,v.signal,v.hist,v.prev_hist,d.explain?.scores?.structure??'',d.explain?.scores?.timing??'',d.explain?.scores?.trend??'',d.explain?.scores?.setup??'',d.explain?.scores?.entry??'',d.explain?.scores?.safety??'',d.explain?.sell_pressure_proxy?.red_candle_turnover_pct??'',d.explain?.plan?.trigger_ref??'',d.explain?.plan?.stop_ref??'',d.explain?.plan?.breakeven_ref??'',d.explain?.plan?.target1_ref??'',d.explain?.plan?.target2_ref??'',d.explain?.plan?.risk_pct??'',d.explain?.summary??'',(d.explain?.warnings||[]).join(' / '),JSON.stringify(d.explain?.factors||{}),d.explain?.sell_pressure_proxy?.note??'',d.combined_gate?.signal??'',d.combined_gate?.watch_ready??'',d.combined_gate?.buy_ready??'',(d.combined_gate?.missing_watch||[]).join(' / '),(d.combined_gate?.missing_buy||[]).join(' / '),JSON.stringify(d.combined_gate?.watch_checks||{}),d.combined_gate?.prior_3bar_breakout_level??'',d.combined_gate?.latest_completed_4h_breakout??'',d.watch_tracks?.kind??'',d.watch_tracks?.early_ready??'',d.watch_tracks?.trend_ready??'',(d.watch_tracks?.early_missing||[]).join(' / '),(d.watch_tracks?.trend_missing||[]).join(' / '),JSON.stringify(d.watch_tracks?.early_checks||{}),JSON.stringify(d.watch_tracks?.trend_checks||{}),(d.watch_tracks?.warnings||[]).join(' / '),d.watch_tracks?.old_combined_signal??'',d.liquidity_audit?.label??'',d.liquidity_audit?.kind??'',d.liquidity_audit?.checks_passed??'',d.liquidity_audit?.hourly_bars_observed??'',d.liquidity_audit?.recent_6h_bars_observed??'',d.liquidity_audit?.longest_missing_hours??'',d.liquidity_audit?.observed_turnover_24h_krw??'',d.liquidity_audit?.observed_turnover_is_complete??'',(d.liquidity_audit?.missing_hour_samples_kst||[]).join(' / '),(d.liquidity_audit?.warnings||[]).join(' / ')].map(q).join(','));
  }
 }
 const url=URL.createObjectURL(new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}));
 const a=document.createElement('a');a.href=url;a.download='BITHUMB_BTC_MTF_V1_4_9_FOCUS_MACD_'+String(focusDiagnostics[0]?.at||dt(new Date())).replace(/[^0-9]/g,'').slice(0,12)+'.csv';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000);
};
$('issuecsv').onclick=()=>{
 if(!issueCases.length||!lastCfg)return;
 const quote=x=>'"'+String(x??'').replace(/"/g,'""')+'"';
 const rows=[['version','strategy','mode','snapshot_kst','tf','fee_each_pct','min_turnover_24h_krw','market_turnover_24h_krw','market','name','issue_type','issue_reason']];
 for(const x of issueCases){
  rows.push(['BTC-MTF-V1.4.9',lastCfg.strategy,lastCfg.mode,x.at,lastCfg.tf,lastCfg.fee,lastCfg.min_turnover_24h_krw,x.turnover24h,x.market,x.name,classifyIssue(x.reason),x.reason]);
 }
 const csv=rows.map(row=>row.map(quote).join(',')).join('\n');
 const url=URL.createObjectURL(new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8'}));
 const a=document.createElement('a');a.href=url;a.download='BITHUMB_BTC_MTF_V1_4_9_ISSUES_'+String(issueCases[0]?.at||dt(new Date())).replace(/[^0-9]/g,'').slice(0,12)+'.csv';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000);
};
$('csv').onclick=()=>{
 if(!lastCfg)return;
 const fields=['version','strategy','mode','snapshot_kst','tf','fee_each_pct','min_turnover_24h_krw','market_turnover_24h_krw','market','name','signal','closed_kst','entry_ref_price_krw','entry_ref_type','tp1_ref_price_krw','tp2_ref_price_krw','stop_ref_price_krw','relative_pct','btc_change_pct','coin_change_pct','breakout_pct','volume_ratio','atr_pct','rise24_pct','structure_risk_pct','rebound_from_low_pct','data_gap_recent','data_status','technical_score','quality_score','score_total','quality_btc_strength','quality_entry_zone','quality_risk_control','bb_position','rsi14','macd_hist','ichimoku_tenkan','ichimoku_kijun','score','stage','reason','tp1_net','tp1_fraction','tp2_net','tp2_fraction','stop_gross','max_minutes','bt_status','bt_net_pct','bt_stop','bt_tp1','bt_tp2','bt_minutes','bt_ambiguous','bt_reason','watch_filter','shown_on_screen','legacy_signal','strict_signal','strict_gate_passed','strict_failed_checks','strict_gate_checks','h4_ma20','h4_ma20_prev','macd_4h_state','macd_4h_event','macd_4h_last_cross','macd_4h_last_cross_kst','macd_4h_macd','macd_4h_signal','macd_4h_prev_hist','macd_day_state','macd_day_event','macd_day_macd','macd_day_signal','macd_day_chart_state','macd_day_chart_event','macd_day_chart_macd','macd_day_chart_signal','explain_version','structure_score','timing_score','trend_score','setup_score','entry_score','safety_score','red_candle_turnover_pct','24h_closed_turnover_krw','rvol_4h','atr_4h_pct','ma7_distance_pct','explain_summary','explain_warnings','breakout_watch_ref','hypothetical_stop_ref','fee_breakeven_ref','hypothetical_target1_ref','hypothetical_target2_ref','target1_rr','target2_rr','hypothetical_stop_distance_pct','future_bar_turnover_ratio_required','future_bar_confirmation_required','explain_plan_valid','explain_factors_json','explain_notice','orderbook_status','combined_rule','combined_signal','combined_watch_ready','combined_buy_ready','combined_watch_failed','combined_buy_failed','combined_watch_checks_json','combined_buy_checks_json','completed_h4_prior_high_breakout_ref','completed_h4_breakout_ok','combined_red_candle_turnover_pct','v143_signal','v144_watch_kind','v144_early_ready','v144_trend_ready','v144_early_failed','v144_trend_failed','v144_early_checks_json','v144_trend_checks_json','v144_warnings','bt_gap_audit_code','bt_initial_missing_hours','bt_initial_missing_samples_kst','bt_first_missing_hour_kst','bt_hourly_recheck_calls','bt_half_hour_diagnostic_calls','bt_recovered_one_hour_candles','bt_lower_tf_bars_in_gap','bt_total_candle_api_calls','bt_audit_initial_error','bt_audit_detail_error','bt_reason_code','v146_quality_label','v146_quality_kind','v146_quality_passed','v146_hourly_bars_24','v146_recent6h_bars','v146_max_missing_streak_hours','v146_observed_turnover_24h_krw','v146_observed_turnover_complete','v146_missing_hours_kst','v146_quality_warnings'];
 const quote=x=>'"'+String(x??'').replace(/"/g,'""')+'"',lines=[fields.join(',')];
 const shown=new Set(visibleResults());
 for(const x of results){
  const si=x.signal,m=si.metrics,t=si.backtest||{},p=si.plan;
  lines.push(['BTC-MTF-V1.4.9',lastCfg.strategy,lastCfg.mode,x.at,lastCfg.tf,lastCfg.fee,lastCfg.min_turnover_24h_krw,x.turnover24h,x.market,x.name,si.signal,m.closed_kst,m.close,si.signal==='BUY'?'BUY 마감봉 참고가':si.signal==='WATCH'?'WATCH 관찰 기준가':'연구 참고가',Number(m.close)*(1+p.tp1_gross_pct/100),Number(m.close)*(1+p.tp2_gross_pct/100),Number(m.close)*(1+p.stop_gross/100),m.relative_pct,m.btc_return_pct,m.coin_return_pct,m.breakout_pct,m.volume_ratio,m.atr_pct,m.rise24_pct??'',m.structure_risk_pct??'',m.rebound_from_low_pct??'',m.data_gap_recent??'',m.data_status??'',m.technical_score??'',m.quality_score??'',m.score_total??'',m.quality_checks?.btc_strength??'',m.quality_checks?.entry_zone??'',m.quality_checks?.risk_control??'',m.bb_position??'',m.rsi14??'',m.macd_hist??'',m.ichimoku_tenkan??'',m.ichimoku_kijun??'',m.score??'',si.stage||'',si.explanation.join(' / '),p.tp1_net,p.tp1_fraction,p.tp2_net,p.tp2_fraction,p.stop_gross,p.max_minutes,t.status||'',t.net_pct??'',t.stop??'',t.tp1??'',t.tp2??'',t.minutes??'',t.ambiguous??'',t.reason??'',$('watch-filter').value,shown.has(x),si.legacy_signal??'',si.strict_signal??'',si.strict_gate?.passed??'',(si.strict_gate?.missing||[]).join(' / '),JSON.stringify(si.strict_gate?.checks||{}),si.strict_gate?.h4_ma20??'',si.strict_gate?.h4_ma20_prev??'',m.macd_h4?.state??'',m.macd_h4?.event??'',m.macd_h4?.last_event??'',m.macd_h4?.last_event_kst??'',m.macd_h4?.macd??'',m.macd_h4?.signal??'',m.macd_h4?.prev_hist??'',m.macd_day?.state??'',m.macd_day?.event??'',m.macd_day?.macd??'',m.macd_day?.signal??'',m.macd_day_chart?.state??'',m.macd_day_chart?.event??'',m.macd_day_chart?.macd??'',m.macd_day_chart?.signal??'',si.explain?.version??'',si.explain?.scores?.structure??'',si.explain?.scores?.timing??'',si.explain?.scores?.trend??'',si.explain?.scores?.setup??'',si.explain?.scores?.entry??'',si.explain?.scores?.safety??'',si.explain?.sell_pressure_proxy?.red_candle_turnover_pct??'',si.explain?.context?.last_24h_closed_turnover_krw??'',si.explain?.context?.h4_rvol_turnover??'',si.explain?.context?.h4_atr_pct??'',si.explain?.context?.h4_ma7_distance_pct??'',si.explain?.summary??'',(si.explain?.warnings||[]).join(' / '),si.explain?.plan?.trigger_ref??'',si.explain?.plan?.stop_ref??'',si.explain?.plan?.breakeven_ref??'',si.explain?.plan?.target1_ref??'',si.explain?.plan?.target2_ref??'',si.explain?.plan?.target1_rr??'',si.explain?.plan?.target2_rr??'',si.explain?.plan?.risk_pct??'',si.explain?.plan?.requires_future_4h_turnover_ratio??'',si.explain?.plan?.requires_next_completed_4h_body_close_above_trigger??'',si.explain?.plan?.plan_valid??'',JSON.stringify(si.explain?.factors||{}),si.explain?.snapshot_notice??'',si.explain?.sell_pressure_proxy?.note??'',si.combined_gate?.rule??'',si.signal,si.combined_gate?.watch_ready??'',si.combined_gate?.buy_ready??'',(si.combined_gate?.missing_watch||[]).join(' / '),(si.combined_gate?.missing_buy||[]).join(' / '),JSON.stringify(si.combined_gate?.watch_checks||{}),JSON.stringify(si.combined_gate?.buy_checks||{}),si.combined_gate?.prior_3bar_breakout_level??'',si.combined_gate?.latest_completed_4h_breakout??'',si.combined_gate?.red_candle_turnover_pct??'',si.v143_signal??'',si.watch_kind??'',si.watch_tracks?.early_ready??'',si.watch_tracks?.trend_ready??'',(si.watch_tracks?.early_missing||[]).join(' / '),(si.watch_tracks?.trend_missing||[]).join(' / '),JSON.stringify(si.watch_tracks?.early_checks||{}),JSON.stringify(si.watch_tracks?.trend_checks||{}),(si.watch_tracks?.warnings||[]).join(' / '),t.audit?.status_code??'',t.audit?.initial_missing_hours??'',(t.audit?.initial_missing_samples_kst||[]).join(' / '),t.audit?.first_unresolved_or_recovered_hour_kst??'',t.audit?.hourly_recheck_calls??'',t.audit?.half_hour_diagnostic_calls??'',t.audit?.recovered_one_hour_candles??'',t.audit?.lower_timeframe_bars_in_missing_hour??'',t.audit?.total_candle_api_calls??'',t.audit?.initial_api_error??'',t.audit?.detail_error??'',t.reason_code??'',si.liquidity_audit?.label??'',si.liquidity_audit?.kind??'',si.liquidity_audit?.checks_passed??'',si.liquidity_audit?.hourly_bars_observed??'',si.liquidity_audit?.recent_6h_bars_observed??'',si.liquidity_audit?.longest_missing_hours??'',si.liquidity_audit?.observed_turnover_24h_krw??'',si.liquidity_audit?.observed_turnover_is_complete??'',(si.liquidity_audit?.missing_hour_samples_kst||[]).join(' / '),(si.liquidity_audit?.warnings||[]).join(' / ')].map(quote).join(','));
 }
 const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');
 a.href=url;a.download='BITHUMB_BTC_MTF_V1_4_9_'+lastCfg.strategy.toUpperCase()+'_'+lastCfg.mode.toUpperCase()+'_'+String(results[0]?.at||lastCfg.at||dt(new Date())).replace(/[^0-9]/g,'').slice(0,12)+'.csv';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000);
};

})();
</script></body></html>'''


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, threaded=True)