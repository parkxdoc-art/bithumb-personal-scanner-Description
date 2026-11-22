"""BITHUMB BTC MTF V1.0 | Read-only LIVE + historical snapshot research.

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

VERSION = "BTC MTF V1.0"
KST = ZoneInfo("Asia/Seoul")
API = "https://api.bithumb.com"
TIMEFRAMES = ("month", "week", "day", "4h", "1h", "30m", "15m", "5m", "3m", "1m")
SCAN_TF = ("5m", "15m", "1h", "4h", "day")
STABLES = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USD1", "PYUSD", "USDE", "USDD", "RLUSD", "EURC", "EURI"}
MARKET_RE = re.compile(r"^KRW-[A-Z0-9]+$")
MIN_TURNOVER = 500_000_000  # live only: 24h ticker value
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


def candles(market, tf, at=None, count=70):
    params = {"market": market, "count": min(200, count)}
    if at is not None:
        params["to"] = at.strftime("%Y-%m-%d %H:%M:%S")
    raw = api_get(endpoint(tf), params)
    clock = at or now_kst()
    result = {}
    for x in raw:
        try:
            start = datetime.fromisoformat(x["candle_date_time_kst"]).replace(tzinfo=KST)
            if tf_end(start, tf) > clock - timedelta(seconds=2):
                continue  # never use unfinished candle
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
    if need_market and (not isinstance(market, str) or not MARKET_RE.fullmatch(market) or market.split("-", 1)[1] in STABLES):
        raise ValueError("KRW 코인 마켓만 조회 가능")
    return mode, tf, fee, at, market


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
        t = ticker.get(market) or {}
        turnover = float(t.get("acc_trade_price_24h") or 0)
        rows.append({"market": market, "name": name, "turnover": turnover,
                     "change24": round(float(t.get("signed_change_rate") or 0)*100, 3)})
    rows.sort(key=lambda x: (-x["turnover"], x["market"]))
    ttl_put("ranked-markets-v1", rows, 300)
    return rows


def btc_context(cfg):
    mode, selected, fee, at, _ = cfg
    key = ("btc", mode, selected, at.strftime("%Y-%m-%d %H:%M"))
    cached = ttl_get(key)
    if cached is not None:
        return cached
    states = {}
    last4 = None
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
    lng = aggregate([states[tf]["state"] for tf in ("month", "week", "day", "4h")])
    sht = aggregate([states[tf]["state"] for tf in ("1h", "30m", "15m", "5m", "3m", "1m")])
    selected_return = round((last4[-1]["c"]/last4[-5]["c"]-1)*100, 4) if last4 and is_contiguous(last4, selected, 5) else None
    context = {"tf": states, "long": lng, "short": sht,
               "selected_return": selected_return, "selected_tf": selected,
               "asof_kst": at.strftime("%Y-%m-%d %H:%M:%S")}
    ttl_put(key, context, 3600 if mode == "backtest" else 900)
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
        rows = getter(market, "5m", at+timedelta(minutes=240), count=100)
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
        mode, tf, fee, at, market = cfg
        if market not in market_list():
            raise ValueError("현재 KRW 마켓 목록에 없음")
        ctx = btc_context(cfg)
        # For historical mode all inputs below are fetched <= the snapshot timestamp.
        rows = candles(market, tf, at, count=50)
        m, issue = coin_metrics(rows, tf, ctx.get("selected_return"))
        if m is None:
            return jsonify(ok=True, market=market, signal=None, issue=issue)
        signal = coin_signal(m, ctx, fee)
        if signal is None:
            return jsonify(ok=True, market=market, signal=None)
        if mode == "backtest":
            signal["backtest"] = backtest_exit(market, at, fee)
        return jsonify(ok=True, market=market, signal=signal)
    except Exception as ex:
        return jsonify(ok=False, error=str(ex)[:170]), 400


HTML = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BITHUMB · BTC MTF V1.0</title>
<style>
:root{color-scheme:dark;--bg:#0b1120;--panel:#161f33;--panel2:#202c46;--ink:#edf4ff;--muted:#93a4bd;--line:#34445f;--cyan:#45cfce;--green:#39d7a1;--yellow:#ffc56d;--red:#ff7d85}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font-family:system-ui,-apple-system,'Noto Sans KR',sans-serif;color:var(--ink);line-height:1.5}main{max-width:1100px;margin:auto;padding:18px 16px 48px}h1{font-size:1.4rem;line-height:1.3;margin:0}h2{font-size:1.08rem;margin:0 0 11px}small,.muted{color:var(--muted)}header{display:flex;justify-content:space-between;gap:10px;align-items:center;margin:5px 0 19px}.eyebrow{color:var(--cyan);font-size:12px;font-weight:700;letter-spacing:1px}.tag{border:1px solid var(--line);border-radius:999px;padding:4px 9px;color:var(--muted);white-space:nowrap;font-size:11px}.surface{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:17px;margin-bottom:14px}.tabs{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:15px}.tabs button{font-size:15px;font-weight:750;padding:13px}.tabs button.on{background:#145c64;border-color:var(--cyan);color:white}button,select,input{border:1px solid var(--line);border-radius:11px;background:var(--panel2);color:var(--ink);font:inherit;padding:10px;min-width:0}button{cursor:pointer}button:disabled{opacity:.42;cursor:default}button.primary{background:var(--cyan);border-color:var(--cyan);color:#09252a;font-weight:800}button.secondary{background:transparent}.toolbar{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.field{display:flex;flex-direction:column;gap:5px}.field label{color:var(--muted);font-size:12px;font-weight:650}.row{display:flex;gap:9px;align-items:center;flex-wrap:wrap}.timebox{display:flex;gap:8px}.timebox input{flex:1;min-width:0}.action{margin-top:14px}.action button{padding:11px 20px}.help{font-size:12px;color:var(--muted);margin-top:9px}.btc-agg{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}.agg{background:#101d32;border:1px solid var(--line);padding:12px;border-radius:12px}.agg strong{display:block;font-size:1.16rem;margin-top:4px}.tf-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}.tf{background:#101b2e;border:1px solid var(--line);border-radius:12px;padding:10px}.tf .name{font-size:12px;color:var(--muted)}.tf .status{font-size:16px;font-weight:750;margin-top:2px}.up{color:var(--green)}.down{color:var(--red)}.side{color:var(--yellow)}.unknown{color:var(--muted)}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.metric{padding:13px;background:#101b2e;border:1px solid var(--line);border-radius:12px}.metric strong{font-size:22px;display:block}.metric span{font-size:12px;color:var(--muted)}.bar{height:6px;background:#27354a;overflow:hidden;border-radius:8px;margin-top:12px}.bar i{display:block;background:var(--cyan);height:100%;width:0%;transition:width .2s}.result{border:1px solid var(--line);background:#101c30;border-radius:13px;margin-top:9px;padding:12px}.result summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px}.result summary::-webkit-details-marker{display:none}.result summary b{font-size:16px}.badge{font-size:11px;font-weight:900;padding:4px 8px;border-radius:7px}.badge.buy{background:#115e4c;color:#9ff9d5}.badge.watch{background:#59421e;color:#ffdca5}.push{margin-left:auto;color:var(--muted);font-size:12px}.reason{font-size:13px;color:#cfdaeb;margin-top:12px;padding-top:9px;border-top:1px solid var(--line)}.reason p{margin:5px 0}.pnl{font-size:13px;margin-top:7px}.empty{font-size:13px;color:var(--muted);padding:15px 0}.notice{font-size:12px;color:#b6c7df;margin-top:12px}.foot{margin-top:26px;color:var(--muted);font-size:12px}#error{color:var(--red);font-size:13px;margin-top:9px;white-space:pre-wrap}#scanstatus{color:var(--muted);font-size:13px}.hidden{display:none!important}
@media(max-width:760px){main{padding:14px 11px 30px}header{align-items:flex-start}.toolbar{grid-template-columns:1fr 1fr}.tf-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.surface{padding:13px}.btc-agg{grid-template-columns:1fr 1fr}}
@media(max-width:355px){.toolbar{grid-template-columns:1fr 1fr}.row{gap:6px}.action button{flex:1}}
</style></head><body><main>
<header><div><div class="eyebrow">PERSONAL RESEARCH · BITHUMB KRW</div><h1>BTC 추세 · 코인 검색기</h1><small>BTC MTF V1.0 · 자동매매 없는 개인 연구용</small></div><span class="tag">KRW ONLY</span></header>
<div class="tabs"><button id="live-tab" class="on" type="button">● 실시간 검색</button><button id="backtest-tab" type="button">◷ 과거 백테스트</button></div>
<section class="surface"><h2>검색 설정</h2><div class="toolbar">
<div class="field"><label for="tf">코인 검색 시간봉</label><select id="tf"><option value="5m">5분봉</option><option value="15m">15분봉</option><option value="1h">1시간봉</option><option value="4h">4시간봉</option><option value="day">일봉</option></select></div>
<div class="field"><label for="fee">편도 수수료</label><select id="fee"><option value="0.04">0.04%</option><option value="0.25">0.25%</option></select></div>
<div class="field"><label for="scope">검색 범위</label><select id="scope"><option value="20">거래대금 상위 20</option><option value="40" selected>거래대금 상위 40</option><option value="80">거래대금 상위 80</option><option value="all">전체 KRW</option></select></div>
<div class="field" id="backtest-date-field" style="display:none"><label for="at">과거 검사 시각 (한국시간)</label><input type="datetime-local" id="at" step="300"></div>
</div><div class="action row"><button class="primary" id="start" type="button">검색 시작</button><button class="secondary" id="csv" disabled type="button">CSV 받기</button><small id="scanstatus">검사 전</small></div><div id="error"></div>
<div class="help" id="live-help">현재 거래대금 상위 순서로 검색. 마감봉만 사용하며 BUY/WATCH는 참고 신호야. 수동 실행만 지원해.</div>
<div class="help hidden" id="backtest-help">선택한 과거 시각 <b>1회 스냅샷</b>에서 신호 판정 후 이어지는 240분간 5분봉으로 청산을 검증해. 일일 전체 거래 백테스트가 아니야. 현재 상장 목록·거래대금 순위를 사용하므로 생존편향/미래 정보 편향이 있어. 일봉·4시간봉은 최근 완료봉을 사용하고, 과거 시각에서 5분봉 시가로 진입해.</div>
</section>
<section class="surface"><h2>BTC 장기·단기 추세 <small id="btcts"></small></h2><div class="btc-agg"><div class="agg"><small>장기 · 월/주/일/4시간</small><strong id="long" class="unknown">검사 전</strong></div><div class="agg"><small>단기 · 1시간~1분</small><strong id="short" class="unknown">검사 전</strong></div></div><div id="tf-grid" class="tf-grid"></div><div class="notice">판정: 완료봉 종가와 MA5·MA10 배열. 종가 &gt; MA5 &gt; MA10은 상승, 반대는 하락, 그 외 횡보. 10봉 연속 자료 부족이면 미확인.</div></section>
<section class="surface"><h2>코인 후보 <small>BUY / WATCH</small></h2><div class="metrics"><div class="metric"><span>검사 완료</span><strong id="done">0</strong></div><div class="metric"><span>BUY</span><strong id="nbuy" class="up">0</strong></div><div class="metric"><span>WATCH</span><strong id="nwatch" class="side">0</strong></div><div class="metric"><span>오류 / 자료 부족</span><strong id="nerr">0</strong></div></div><div class="bar"><i id="progress"></i></div><div class="notice" id="btinfo"></div><div id="results" class="empty">검사 시작을 누르면 BUY·WATCH 후보만 나타나.</div></section>
<div class="foot">모든 신호는 연구용 가상 판정이며 투자 권유나 주문 지시가 아니야. 실제 체결과 손익은 슬리피지·호가·갭으로 달라질 수 있어. 빗썸 공개 API만 사용하고 계정·주문 API는 호출하지 않아.</div>
</main><script>
(()=>{
const $=id=>document.getElementById(id), ST={up:'상승',down:'하락',side:'횡보',unknown:'자료 부족'},TF={'month':'월','week':'주','day':'일','4h':'4시간','1h':'1시간','30m':'30분','15m':'15분','5m':'5분','3m':'3분','1m':'1분'},order=['month','week','day','4h','1h','30m','15m','5m','3m','1m'];
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let mode='live',busy=false,results=[],problems=0,completed=0,universe=[],runId=0,lastCfg=null;
const dt=(d)=>{const p=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+'T'+p(d.getHours())+':00'};
let d=new Date();d.setDate(d.getDate()-1);d.setHours(15,0,0,0);$('at').value=dt(d);
function modeSet(next){if(busy)return;mode=next;$('live-tab').classList.toggle('on',mode==='live');$('backtest-tab').classList.toggle('on',mode==='backtest');$('backtest-date-field').style.display=mode==='backtest'?'flex':'none';$('live-help').classList.toggle('hidden',mode!=='live');$('backtest-help').classList.toggle('hidden',mode!=='backtest');reset()}
$('live-tab').onclick=()=>modeSet('live');$('backtest-tab').onclick=()=>modeSet('backtest');
function reset(){results=[];completed=problems=0;$('results').innerHTML='검사 시작을 누르면 BUY·WATCH 후보만 나타나.';$('csv').disabled=true;['done','nbuy','nwatch','nerr'].forEach(k=>$(k).textContent='0');$('long').textContent='검사 전';$('short').textContent='검사 전';$('tf-grid').innerHTML='';$('progress').style.width='0%';$('scanstatus').textContent='검사 전';$('btinfo').textContent='';$('error').textContent='';$('btcts').textContent=''}
async function post(url,body){const rsp=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const j=await rsp.json();if(!rsp.ok||!j.ok)throw Error(j.error||'데이터 요청 실패');return j}
async function get(url){const rsp=await fetch(url);const j=await rsp.json();if(!rsp.ok||!j.ok)throw Error(j.error||'목록 조회 실패');return j}
function btcDraw(ctx){$('long').textContent=ST[ctx.long]||'미확인';$('long').className=ctx.long;$('short').textContent=ST[ctx.short]||'미확인';$('short').className=ctx.short;$('btcts').textContent='· '+ctx.asof_kst+' KST';$('tf-grid').innerHTML=order.map(k=>{const x=ctx.tf[k]||{state:'unknown'},why=x.reason?' · '+x.reason:'';return `<div class="tf" title="${esc(why)}"><div class="name">BTC ${TF[k]}</div><div class="status ${x.state}">${ST[x.state]||'자료 부족'}</div></div>`}).join('')}
function summary(){let b=results.filter(x=>x.signal?.signal==='BUY').length,w=results.filter(x=>x.signal?.signal==='WATCH').length;$('done').textContent=completed;$('nbuy').textContent=b;$('nwatch').textContent=w;$('nerr').textContent=problems;$('progress').style.width=universe.length?Math.round(completed/universe.length*100)+'%':'0%';$('scanstatus').textContent=busy?'검사 중 '+completed+'/'+universe.length:'검사 완료 '+completed+'/'+universe.length;}
function renderResults(){const sorted=results.slice().sort((a,b)=>{const pr=x=>x.signal.signal==='BUY'?0:1;return pr(a)-pr(b)||b.signal.metrics.relative_pct-a.signal.metrics.relative_pct});if(!sorted.length){$('results').innerHTML='아직 BUY·WATCH 조건을 충족한 종목이 없어. 신호가 없으면 거래하지 않는 게 원칙이야.';return;}
$('results').classList.remove('empty');$('results').innerHTML=sorted.map(x=>{const s=x.signal,m=s.metrics,bt=s.backtest,fee=Number(lastCfg.fee);const btLine=mode==='backtest'?(bt?.status==='RESOLVED'?`<div class="pnl ${bt.net_pct>=0?'up':'down'}">240분 가상 순손익 ${bt.net_pct>=0?'+':''}${Number(bt.net_pct).toFixed(3)}% · ${bt.stop?'손절':bt.tp2?'2차 익절':bt.tp1?'1차 익절 후 청산':'시간 만료/손절 전 청산'}${bt.ambiguous?' · 같은 봉 손절/익절 순서 불명':''}</div>`:`<div class="pnl side">백테스트 미확정: ${esc(bt?.reason||'가격 데이터 부족')}</div>`):'';
return `<details class="result"><summary><span class="badge ${s.signal.toLowerCase()}">${s.signal}</span><b>${esc(x.name||x.market)} <small>${esc(x.market)}</small></b><span class="push">상대강도 ${m.relative_pct>=0?'+':''}${m.relative_pct.toFixed(2)}%p · 상세 ▾</span></summary><div class="reason">${s.explanation.map(z=>`<p>• ${esc(z)}</p>`).join('')}<p>매도 가정: 순익 +1%에 70%, +10%에 잔량 30% · 가격 -2% 손절 · 최대 240분</p><p>수수료 편도 ${fee.toFixed(2)}%: 1차 목표 가격 상승 약 ${s.plan.tp1_gross_pct}% · 2차 ${s.plan.tp2_gross_pct}%</p><p>신호 마감 시각: ${esc(m.closed_kst)} KST</p>${btLine}</div></details>`}).join('')}
$('start').onclick=async()=>{if(busy)return;reset();busy=true;runId++;const id=runId,btn=$('start');btn.disabled=true;btn.textContent='검색 중…';let cfg={mode,tf:$('tf').value,fee:Number($('fee').value)};if(mode==='backtest')cfg.at=$('at').value.replace('T',' ')+':00';lastCfg=cfg;try{const [list,b]=await Promise.all([get('/api/universe'),post('/api/btc',cfg)]);if(runId!==id)return;btcDraw(b.context);if(mode==='live')cfg.snapshot_at=b.context.asof_kst;const count=$('scope').value;const items=list.items.filter(x=>mode==='backtest'||x.turnover>=500000000);universe=(count==='all'?items:items.slice(0,Number(count)));if(!universe.length)throw Error('검색 가능한 코인 목록 없음');$('btinfo').textContent=mode==='backtest'?'과거 1회 스냅샷 · 현재 상장/거래대금 순위로 대상 선택(생존/미래정보 편향 주의) · 미확정 별도 표시':'24시간 거래대금 5억원 이상 · 완료봉만 사용 · 신호 없는 종목 제외';let cursor=0;const work=async()=>{while(cursor<universe.length){const index=cursor++,coin=universe[index];try{const j=await post('/api/coin',{...cfg,market:coin.market});if(j.signal)results.push({market:coin.market,name:coin.name,signal:j.signal});if(j.issue)problems++;}catch(e){problems++;}completed++;if(completed%4===0||completed===universe.length){summary();renderResults();}await Promise.resolve();}};await Promise.all(Array.from({length:Math.min(5,universe.length)},work));}catch(e){$('error').textContent='검사를 완료하지 못했어: '+e.message;}finally{busy=false;btn.disabled=false;btn.textContent='검색 시작';summary();renderResults();$('csv').disabled=!completed;}};
$('csv').onclick=()=>{if(!lastCfg)return;const fields=['version','mode','snapshot_kst','tf','fee_each_pct','market','name','signal','closed_kst','relative_pct','btc_change_pct','coin_change_pct','breakout_pct','volume_ratio','atr_pct','reason','bt_status','bt_net_pct','bt_stop','bt_tp1','bt_tp2','bt_minutes','bt_ambiguous','bt_reason'];const quote=x=>'"'+String(x??'').replace(/"/g,'""')+'"';const lines=[fields.join(',')];for(const x of results){const s=x.signal,m=s.metrics,t=s.backtest||{};lines.push(['BTC-MTF-V1.0',lastCfg.mode,lastCfg.at||'LIVE',lastCfg.tf,lastCfg.fee,x.market,x.name,s.signal,m.closed_kst,m.relative_pct,m.btc_return_pct,m.coin_return_pct,m.breakout_pct,m.volume_ratio,m.atr_pct,s.explanation.join(' / '),t.status||'',t.net_pct??'',t.stop??'',t.tp1??'',t.tp2??'',t.minutes??'',t.ambiguous??'',t.reason??''].map(quote).join(','));}const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='BITHUMB_BTC_MTF_V1_'+lastCfg.mode.toUpperCase()+'.csv';document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),3000)};
})();
</script></body></html>'''


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, threaded=True)