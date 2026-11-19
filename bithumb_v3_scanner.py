"""BITHUMB V3.1 | 휴대폰(Pydroid 3) + Render 겸용 연구 스캐너.

필요: pip install requests
실행: python bithumb_v3_scanner.py  -> http://127.0.0.1:8080
Render: PORT 환경변수가 있으면 0.0.0.0:$PORT에 바인딩.
공개 빗썸 시세만 사용하며 인증/주문 기능은 절대 없습니다.

주의: 백테스트는 교육/검증용입니다. 역사적 '종목 선정'에 현재 24h 거래대금
순위를 사용하기 때문에 유니버스 선정 편향이 남습니다. 실전 수익 보장 없음.
"""
import json
import math
import os
import re
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

VERSION = "V3.1"
BASE = "https://api.bithumb.com/v1"
KST = timezone(timedelta(hours=9))
BUSY = threading.Lock()
MARKET_CACHE = {"at": 0, "markets": []}
MARKET_LOCK = threading.Lock()
VALID_TF = {5, 15, 60, 240}


def kst_now():
    return datetime.now(KST).replace(tzinfo=None)


def number(query, key, default, lo, hi, integer=False):
    try:
        v = float(query.get(key, [str(default)])[0])
        if not math.isfinite(v):
            raise ValueError(key)
        v = max(lo, min(hi, v))
        return int(v) if integer else v
    except (TypeError, ValueError, IndexError):
        return default


@dataclass(frozen=True)
class Settings:
    strategy: str = "pullback"
    tf: int = 15
    count: int = 20
    days: int = 14
    fee: float = 0.0025
    slip: float = 0.001
    capital: int = 1_000_000
    turnover_min: int = 500_000_000
    flow: str = "weak"
    trail: float = 3.0
    tp_r: float = 1.8
    tp_half_r: float = 0.0
    be_r: float = 0.0
    btc: bool = False
    max_positions: int = 1
    max_trades_day: int = 4
    rs: float = 0.0
    rs_days: int = 7

    @staticmethod
    def from_q(q):
        st = q.get("st", ["pullback"])[0]
        if st not in ("pullback", "trend"):
            st = "pullback"
        tf = number(q, "tf", 15, 5, 240, True)
        if tf not in VALID_TF or (st == "trend" and tf not in (60, 240)) or (st == "pullback" and tf not in (5, 15, 60)):
            tf = 60 if st == "trend" else 15
        flow = q.get("of", ["weak"])[0]
        if flow not in ("off", "weak", "strong"):
            flow = "weak"
        return Settings(
            strategy=st, tf=tf, count=number(q, "n", 20, 5, 60, True),
            days=number(q, "days", 14, 3, 30, True),
            fee=number(q, "fee", .25, 0, 1) / 100,
            slip=number(q, "slip", .1, 0, 1) / 100,
            capital=number(q, "amt", 1_000_000, 100_000, 100_000_000, True),
            turnover_min=number(q, "liquidity", 500_000_000, 0, 100_000_000_000, True),
            flow=flow, trail=number(q, "trail", 3, 1, 6),
            tp_r=number(q, "tp", 1.8, 1, 5),
            tp_half_r=number(q, "tp1", 0, 0, 5),
            be_r=number(q, "be", 0, 0, 5),
            btc=q.get("btc", ["off"])[0] == "on",
            max_positions=number(q, "maxpos", 1, 1, 5, True),
            max_trades_day=number(q, "maxday", 4, 1, 4, True),
            rs=number(q, "rs", 0, 0, 1), rs_days=number(q, "rsd", 7, 1, 14, True),
        )


def api(endpoint, params=None, retry=3):
    url = f"{BASE}/{endpoint}"
    last = "알 수 없는 오류"
    for attempt in range(retry):
        try:
            resp = requests.get(url, params=params, timeout=(5, 14), headers={"Accept": "application/json", "User-Agent": "bithumb-v3-research/1.0"})
            if resp.status_code in (429, 500, 502, 503, 504):
                last = f"API {resp.status_code}"
                time.sleep(min(.6 * (2 ** attempt), 3))
                continue
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError("비정상적인 API 반환 형식")
            return data
        except (requests.RequestException, ValueError) as e:
            last = str(e)[:150]
            if attempt < retry - 1:
                time.sleep(.5 * (attempt + 1))
    raise RuntimeError(f"빗썸 {endpoint}: {last}")


def universe(cfg):
    """요청 당 1분 캐시; KRW 상위 거래대금 종목 중 필터를 통과한 대상만 검사."""
    with MARKET_LOCK:
        if time.monotonic() - MARKET_CACHE["at"] < 60 and MARKET_CACHE["markets"]:
            rows = MARKET_CACHE["markets"]
        else:
            markets = [x["market"] for x in api("market/all") if x.get("market", "").startswith("KRW-")]
            rows = []
            for i in range(0, len(markets), 100):
                ticks = api("ticker", {"markets": ",".join(markets[i:i + 100])})
                for t in ticks:
                    try:
                        m = t["market"]
                        turn = float(t.get("acc_trade_price_24h", 0))
                        if math.isfinite(turn) and turn >= 0:
                            rows.append({"market": m, "turnover": turn})
                    except (KeyError, ValueError, TypeError):
                        continue
            rows.sort(key=lambda x: x["turnover"], reverse=True)
            MARKET_CACHE.update(at=time.monotonic(), markets=rows)
    eligible = [x for x in rows if x["turnover"] >= cfg.turnover_min]
    return eligible[:cfg.count], len(eligible)


def candles(market, tf, need):
    """페이지 사이 KST 시각 연결. Bithumb `to`가 KST라는 공식 문서 규격 준수.
    거래가 없어서 발생하는 누락 분봉은 합성하지 않음.
    """
    if tf not in VALID_TF:
        raise ValueError("지원하지 않는 캔들 단위")
    need = min(max(need, 10), 6500)
    seen, cursor = {}, None
    previous_oldest = None
    for _ in range(math.ceil(need / 200) + 3):
        if len(seen) >= need:
            break
        params = {"market": market, "count": min(200, need - len(seen))}
        if cursor:
            params["to"] = cursor
        data = api(f"candles/minutes/{tf}", params)
        if not data:
            break
        for x in data:
            try:
                at = datetime.strptime(x["candle_date_time_kst"], "%Y-%m-%dT%H:%M:%S")
                row = {"t": at, "o": float(x["opening_price"]), "h": float(x["high_price"]),
                       "l": float(x["low_price"]), "c": float(x["trade_price"]),
                       "v": float(x["candle_acc_trade_volume"]),
                       "turn": float(x.get("candle_acc_trade_price", 0))}
                if all(math.isfinite(row[k]) for k in ("o", "h", "l", "c", "v", "turn")) and row["l"] > 0 and row["h"] >= row["l"]:
                    seen[at] = row
            except (KeyError, TypeError, ValueError):
                continue
        oldest = data[-1]["candle_date_time_kst"].replace("T", " ")
        if oldest == previous_oldest:
            break
        previous_oldest, cursor = oldest, oldest
        if len(data) < params["count"]:
            break
        time.sleep(.11)
    out = [seen[t] for t in sorted(seen)]
    return out[-need:]


def enrich(bars, tf):
    """각 봉 마감 시점까지만 이용해 EMA/ATR/거래량 평균 계산.
    누락 봉이 생기면 계산 워밍업을 다시 시작해 빈 봉을 메우지 않음.
    """
    a50, a200 = 2 / 51, 2 / 201
    ema50 = ema200 = None
    trs, vols, turns = [], [], []
    run = 0
    previous = None
    for b in bars:
        contiguous = previous is not None and b["t"] - previous["t"] == timedelta(minutes=tf)
        if not contiguous:
            run = 0
            ema50 = ema200 = b["c"]
            trs, vols, turns = [], [], []
        else:
            run += 1
            ema50 = a50 * b["c"] + (1 - a50) * ema50
            ema200 = a200 * b["c"] + (1 - a200) * ema200
        tr = max(b["h"] - b["l"], abs(b["h"] - (previous["c"] if contiguous else b["c"])),
                 abs(b["l"] - (previous["c"] if contiguous else b["c"])))
        b["volavg"] = sum(vols[-20:]) / 20 if len(vols) >= 20 else None
        b["turn24"] = sum(turns[-(1440 // tf - 1):]) + b["turn"] if len(turns) >= (1440 // tf - 1) else None
        trs.append(tr)
        vols.append(b["v"])
        turns.append(b["turn"])
        b["atr"] = sum(trs[-14:]) / 14 if len(trs) >= 14 else None
        b["ema50"] = ema50 if run >= 50 else None
        b["ema200"] = ema200 if run >= 200 else None
        b["run"] = run
        b["dr"] = (2 * b["c"] - b["h"] - b["l"]) / max(1e-12, b["h"] - b["l"])
        previous = b
    return bars


def min_history(c):
    # 검증용 워밍업: 60분 추세는 EMA200, 눌림은 EMA50, RS 설정이 있으면 별도 창 보장
    return max((220 if c.strategy == "trend" else 95), c.rs_days * 1440 // c.tf + 22 if c.rs else 0)


def load_series(markets, cfg, days=None):
    days = cfg.days if days is None else days
    need = min(6500, math.ceil(days * 1440 / cfg.tf) + min_history(cfg) + 4)
    datasets, errors = {}, []

    def one(item):
        m = item["market"]
        try:
            bars = enrich(candles(m, cfg.tf, need), cfg.tf)
            if len(bars) < min_history(cfg) + 5:
                return m, None, "충분한 연속 이력 없음"
            return m, bars, None
        except Exception as exc:
            return m, None, str(exc)[:170]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(one, item) for item in markets]
        for fut in as_completed(futures):
            market, data, err = fut.result()
            if err:
                errors.append(f"{market}: {err}")
            else:
                datasets[market] = data
    return datasets, errors


def complete(bars, tf):
    now = kst_now()
    return [b for b in bars if b["t"] + timedelta(minutes=tf) <= now]


def past(bars, end, n):
    if end < n or bars[end - 1]["run"] < n - 1:
        return None
    return bars[end - n:end]


def breakout_pb(bars, k, cfg):
    if k < 60 or bars[k]["run"] < 60:
        return None
    b = bars[k]
    box_len = {5: 24, 15: 16, 60: 12}[cfg.tf]
    box = past(bars, k, box_len)
    atr, volavg = b["atr"], b["volavg"]
    if not box or not atr or not volavg or atr <= 0 or volavg <= 0:
        return None
    hh, ll = max(x["h"] for x in box), min(x["l"] for x in box)
    if not 1.5 <= (hh - ll) / atr <= 6:
        return None
    if sum(x["h"] >= hh * .999 for x in box) < 2:
        return None
    if not (hh < b["c"] <= hh + 2 * atr and b["c"] > b["ema50"]):
        return None
    if b["v"] < 1.7 * volavg:
        return None
    if b["c"] <= b["o"] or (b["c"] - b["o"]) / max(b["h"] - b["l"], 1e-12) < .6:
        return None
    return hh


def check_pb(bars, k, hh, max_j, cfg):
    vk = bars[k]["v"]
    for j in range(k + 1, min(max_j, k + 6) + 1):
        b = bars[j]
        if b["t"] - bars[j-1]["t"] != timedelta(minutes=cfg.tf):
            return None
        if b["c"] < hh * .998:
            return None
        if b["l"] <= hh * 1.003 and b["c"] > hh and b["c"] > b["o"]:
            pb = bars[k+1:j+1]
            avg_v = sum(x["v"] for x in pb) / len(pb)
            delta = sum(x["dr"] * x["v"] for x in bars[k:j+1])
            if cfg.flow == "weak" and (avg_v > vk or b["dr"] < 0 or delta <= 0):
                return None
            if cfg.flow == "strong" and (avg_v > .7 * vk or b["dr"] < .3 or delta <= 0):
                return None
            return j
    return None


def pb_levels(bars, k, j, entry, hh, cfg):
    atr = bars[k]["atr"]
    stop = min(hh, bars[j]["l"]) - .5 * atr
    risk = (entry - stop) / entry if entry > 0 else 1
    if not (.005 <= risk <= .04) or (cfg.tp_r * risk < .01):
        return None
    if entry > hh + 2 * atr:
        return None
    return stop, entry + cfg.tp_r * (entry - stop), risk


def breakout_trend(bars, k, cfg):
    look = 48 if cfg.tf == 60 else 20
    if k < 210 or bars[k]["run"] < 210:
        return None
    b = bars[k]
    history = past(bars, k, look)
    if not history or b["volavg"] is None or b["atr"] is None:
        return None
    hh = max(x["h"] for x in history)
    prev_ema = bars[k - 5]["ema200"]
    if not (b["c"] > hh and b["c"] > b["ema200"] and b["ema200"] > prev_ema):
        return None
    if b["v"] < b["volavg"] * 1.5 or b["c"] > hh + b["atr"] * 2:
        return None
    return hh


def risk_levels_trend(bars, k, entry, cfg):
    atr = bars[k]["atr"]
    if not atr or entry <= 0:
        return None
    stop = entry - cfg.trail * atr
    risk = (entry - stop) / entry
    if not (.005 <= risk <= .10):
        return None
    return stop, None, risk


def btc_ok_by_time(btc_bars):
    vals = {}
    for i, b in enumerate(btc_bars):
        if i >= 205 and b["ema200"] and btc_bars[i-5]["ema200"]:
            vals[b["t"]] = b["c"] > b["ema200"] and b["ema200"] > btc_bars[i-5]["ema200"]
    return vals


def rs_by_time(datasets, cfg):
    """같은 과거 시각에 존재하던 샘플 코인끼리의 순위; 미래 종가 참조 안 함."""
    L = max(2, cfg.rs_days * 1440 // cfg.tf)
    ret = {}
    for market, bars in datasets.items():
        for i in range(L, len(bars)):
            if bars[i]["run"] < L:
                continue
            old = bars[i-L]["c"]
            if old > 0:
                ret.setdefault(bars[i]["t"], []).append((market, bars[i]["c"] / old - 1))
    ranks = {}
    for t, rows in ret.items():
        if len(rows) < 5:
            continue
        s = sorted(rows, key=lambda x: x[1], reverse=True)
        for pos, (m, _) in enumerate(s):
            ranks[(t, m)] = (pos + 1) / len(s)
    return ranks


def calc_outcome(entry, exits, fee):
    """슬리피지 반영 거래 체결가는 외부에서 산정; 매수/매도 수수료를 명목 현금흐름으로 계산."""
    weighted = sum(frac * price for frac, price in exits)
    gross = weighted / entry - 1
    net = weighted * (1 - fee) / (entry * (1 + fee)) - 1
    return gross, net


def simulate(bars, idx, stop0, tp, cfg):
    """확정봉 다음 봉 시가에서 진입. 동일 봉 stop/TP 동시 접촉은 stop 우선.
    TP/BE/트레일링의 그 봉 고가 관측으로 생긴 변경은 다음 봉부터 적용.
    분봉 누락 시 UNKNOWN: 임의 보간/수익 확정 금지.
    """
    if idx >= len(bars):
        return {"status": "UNKNOWN", "why": "미래봉 부족"}
    entry = bars[idx]["o"] * (1 + cfg.slip)
    if entry <= stop0 or stop0 <= 0:
        return None
    risk = entry - stop0
    if cfg.strategy == "pullback" and tp is not None:
        # 라이브 기준가와 다른 갭 시가로 TP 재설정: 같은 봉 도달 가능한 TP만 포함
        tp = entry + cfg.tp_r * risk
    tp1 = entry + cfg.tp_half_r * risk if cfg.strategy == "trend" and cfg.tp_half_r else None
    be_trigger = entry + cfg.be_r * risk if cfg.be_r else None
    fee, slip = cfg.fee, cfg.slip
    remaining, stops, hh = 1.0, stop0, entry
    sold = []
    limit = (48 if cfg.tf == 5 else 32 if cfg.tf == 15 else 24) if cfg.strategy == "pullback" else (120 if cfg.tf == 60 else 60)
    time_stop = {5: 8, 15: 8, 60: 6}.get(cfg.tf, 120)
    took_tp = False
    for j in range(idx, min(len(bars), idx + limit)):
        if j > idx and bars[j]["t"] - bars[j-1]["t"] != timedelta(minutes=cfg.tf):
            return {"status": "UNKNOWN", "why": "캔들 결측", "entry_at": bars[idx]["t"].isoformat()}
        b = bars[j]
        if b["l"] <= stops:
            px = min(stops, b["o"]) * (1 - slip)
            sold.append((remaining, px))
            gross, net = calc_outcome(entry, sold, fee)
            return {"status": "CLOSED", "exit_at": (b["t"] + timedelta(minutes=cfg.tf)).isoformat(),
                    "entry_at": bars[idx]["t"].isoformat(), "exit_price": px, "gross_pct": 100*gross,
                    "net_pct": 100*net, "why": "손절/트레일링", "risk_pct": 100*risk/entry}
        target = tp if cfg.strategy == "pullback" else tp1
        if target and not took_tp and b["h"] >= target:
            px = max(target, b["o"]) * (1 - slip)
            sold.append((.5, px))
            remaining = .5
            took_tp = True
        if cfg.strategy == "pullback" and not took_tp and j-idx+1 >= time_stop:
            sold.append((remaining, b["c"] * (1 - slip)))
            gross, net = calc_outcome(entry, sold, fee)
            return {"status": "CLOSED", "exit_at": (b["t"] + timedelta(minutes=cfg.tf)).isoformat(),
                    "entry_at": bars[idx]["t"].isoformat(), "exit_price": b["c"], "gross_pct": 100*gross,
                    "net_pct": 100*net, "why": "시간종료", "risk_pct": 100*risk/entry}
        # 종가 후 업데이트 -> 다음 봉부터 적용.
        if cfg.strategy == "pullback":
            if took_tp:
                stops = max(stops, b["l"])
        else:
            hh = max(hh, b["h"])
            atr = b["atr"] or bars[idx-1]["atr"]
            if atr:
                stops = max(stops, hh - cfg.trail * atr)
            if be_trigger and b["h"] >= be_trigger:
                # 수수료 반영 원금회복선은 진입체결가/(1-매도수수료) 근사; 슬립은 이후 매도 시 별도 처리
                breakeven = entry * (1 + fee) / max(1e-9, (1 - fee) * (1 - slip))
                stops = max(stops, breakeven)
    if idx + limit <= len(bars):
        b = bars[idx + limit - 1]
        sold.append((remaining, b["c"] * (1 - slip)))
        gross, net = calc_outcome(entry, sold, fee)
        return {"status": "CLOSED", "exit_at": (b["t"]+timedelta(minutes=cfg.tf)).isoformat(),
                "entry_at": bars[idx]["t"].isoformat(), "exit_price": b["c"], "gross_pct": 100*gross,
                "net_pct": 100*net, "why": "최대보유 종료", "risk_pct": 100*risk/entry}
    return {"status": "UNKNOWN", "entry_at": bars[idx]["t"].isoformat(), "why": "미래봉 부족"}


def detect_all(market, bars, cfg, btc_map=None, rs_map=None, for_scan=False):
    """각 전략에 일관된 시점 제한 적용. 마지막 캔들은 완료봉만 이용."""
    bars = complete(bars, cfg.tf)
    if len(bars) < min_history(cfg) + 5:
        return [], [], {"observed": len(bars), "gaps": 0}
    cutoff = bars[-1]["t"] - timedelta(days=cfg.days)
    start = len(bars)-1 if for_scan else min_history(cfg)
    events, watch = [], []
    gaps = sum(bars[i]["t"]-bars[i-1]["t"] != timedelta(minutes=cfg.tf) for i in range(1, len(bars)))
    # 눌림 진입 신호는 마지막 완료봉에서 retest이므로 앞 6개 돌파를 검사한다.
    if cfg.strategy == "pullback" and for_scan:
        ks = range(max(0,len(bars)-7), len(bars)-1)
    elif for_scan:
        ks = [len(bars)-1]
    else:
        ks = range(start, len(bars)-1)
    for k in ks:
        b = bars[k]
        if cfg.strategy == "pullback":
            hh = breakout_pb(bars, k, cfg)
            if hh is None:
                continue
            end = len(bars)-1 if for_scan else min(len(bars)-2,k+6)
            j = check_pb(bars,k,hh,end,cfg)
            if j is None:
                if for_scan and k+6>=len(bars)-1 and bars[-1]["c"]>=hh*.998:
                    watch.append({"market":market,"kind":"pullback","status":"눌림 대기","box":hh,"price":bars[-1]["c"],"time":b["t"].isoformat()})
                continue
            if for_scan and j != len(bars)-1:
                continue
            i = j+1
            entry = bars[i]["o"] if i<len(bars) else bars[j]["c"]
            lv = pb_levels(bars,k,j,entry,hh,cfg)
            if lv is None:
                continue
            stop,tp,risk = lv
            sig_time = bars[j]["t"]
            if bars[j].get("turn24") is None or bars[j]["turn24"] < cfg.turnover_min:
                continue
            context = {"box":hh,"retest":bars[j]["l"],"volume_ratio":round(b["v"]/max(b["volavg"],1e-12),2)}
        else:
            hh = breakout_trend(bars,k,cfg)
            if hh is None:
                if for_scan and b.get("ema200") is not None and b["run"] >= 210:
                    n = 48 if cfg.tf == 60 else 20
                    prior = past(bars,k,n)
                    if prior:
                        level = max(x["h"] for x in prior)
                        gap_pct = (level / b["c"] - 1) * 100
                        if (-0.3 <= gap_pct <= 1.5 and b["c"] > b["ema200"] > bars[k-5]["ema200"]
                                and (b.get("turn24") or 0) >= cfg.turnover_min):
                            watch.append({"market":market,"kind":"trend","status":"돌파 대기",
                                          "box":level,"price":b["c"],"time":b["t"].isoformat()})
                continue
            sig_time=b["t"]
            if b.get("turn24") is None or b["turn24"] < cfg.turnover_min:
                continue
            if btc_map is not None and not btc_map.get(sig_time,False):
                continue
            if rs_map is not None and (rs_map.get((sig_time,market),1.0)>cfg.rs):
                continue
            i=k+1
            entry=bars[i]["o"] if i<len(bars) else b["c"]
            lv=risk_levels_trend(bars,k,entry,cfg)
            if lv is None:
                continue
            stop,tp,risk=lv
            context={"box":hh,"volume_ratio":round(b["v"]/max(b["volavg"],1e-12),2),"ema200":b["ema200"]}
        if not for_scan and sig_time < cutoff:
            continue
        event={"market":market,"strategy":cfg.strategy,"signal_at":sig_time.isoformat(),
               "entry_at":(bars[i]["t"] if i < len(bars) else sig_time + timedelta(minutes=cfg.tf)).isoformat(),
               "price":entry,"stop":stop,"tp":tp,"risk_pct":100*risk,"context":context}
        if for_scan:
            events.append(event)
        elif i<len(bars):
            outcome=simulate(bars,i,stop,tp,cfg)
            if outcome is None:
                continue
            event.update(outcome)
            events.append(event)
    return events, watch, {"observed":len(bars),"gaps":gaps}


def daily_and_split(selected, cfg, cut=None):
    closed = sorted((x for x in selected if x.get("status")=="CLOSED"),key=lambda x:x["entry_at"])
    if not closed:
        return {"n":0,"win":0,"loss":0,"win_rate":None,"avg_net":None,"sum_net":None,"pnl_krw":0,"ev":None},[],{"train":None,"test":None}
    def summary(items):
        if not items:
            return {"n":0,"win":0,"loss":0,"win_rate":None,"avg_net":None,"sum_net":None,"pnl_krw":0,"ev":None}
        gains=[x["net_pct"] for x in items]
        return {"n":len(items),"win":sum(x>0 for x in gains),"loss":sum(x<=0 for x in gains),
                "win_rate":round(100*sum(x>0 for x in gains)/len(gains),1),"avg_net":round(sum(gains)/len(gains),3),
                "sum_net":round(sum(gains),3),"pnl_krw":int(round(sum(cfg.capital*x/100 for x in gains))),
                "ev":round(sum(gains)/len(gains),3)}
    days={}
    for t in closed:
        days.setdefault(t["entry_at"][:10],[]).append(t)
    daily=[{"date":d,**summary(ts)} for d,ts in sorted(days.items())]
    time_lo=datetime.fromisoformat(closed[0]["entry_at"])
    time_hi=datetime.fromisoformat(closed[-1]["entry_at"])
    if cut is None:
        cut=time_lo+(time_hi-time_lo)*.7
    return summary(closed),daily,{"train":summary([x for x in closed if datetime.fromisoformat(x["entry_at"])<cut]),
                                         "test":summary([x for x in closed if datetime.fromisoformat(x["entry_at"])>=cut])}


def select_trades(events, cfg):
    """신호 발생 시점 기준 선별. 이전 거래의 청산이 관측된 이후에만 손실 제한 갱신."""
    sorted_events=sorted(events,key=lambda x:(x["entry_at"],x.get("rank",999)))
    chosen, positions, settled={},[],[]
    selected=[]
    losses_by_day={}
    for event in sorted_events:
        t=event["entry_at"]
        # 해당 시점 이전에 끝난 손익만 확정: 미래 거래 수익으로 현재 선택하지 않음.
        existing=[]
        for p in positions:
            if p.get("exit_at") and p["exit_at"]<=t:
                if p.get("status")=="CLOSED" and p.get("net_pct",0)<=0:
                    day=p["exit_at"][:10]
                    losses_by_day[day]=losses_by_day.get(day,0)+1
            else:
                existing.append(p)
        positions=existing
        day=t[:10]
        if chosen.get(day,0)>=cfg.max_trades_day or losses_by_day.get(day,0)>=3 or len(positions)>=cfg.max_positions:
            continue
        if any(x["market"]==event["market"] for x in positions):
            continue
        chosen[day]=chosen.get(day,0)+1
        selected.append(event)
        positions.append(event)
    return selected


def simple_buy_ratio(market):
    try:
        ticks=api("trades/ticks",{"market":market,"count":200},retry=2)
        total=sum(float(x.get("trade_volume",0)) for x in ticks)
        if total<=0:
            return None
        buy=sum(float(x.get("trade_volume",0)) for x in ticks if x.get("ask_bid")=="BID")
        return round(100*buy/total,1)
    except Exception:
        return None


def scan(cfg):
    chosen,eligible=universe(cfg)
    if not chosen:
        return {"error":"거래대금 조건을 만족하는 종목을 찾지 못했습니다. API 연결도 확인해주세요."}
    bars, errors=load_series(chosen,cfg,days=max(4,math.ceil(min_history(cfg)*cfg.tf/1440)+1))
    btc_map=None
    if cfg.strategy=="trend" and cfg.btc:
        try:
            b=enrich(candles("KRW-BTC",cfg.tf,math.ceil(4*1440/cfg.tf)+min_history(cfg)+4),cfg.tf)
            btc_map=btc_ok_by_time(complete(b,cfg.tf))
            if not btc_map:
                raise ValueError("BTC 추세를 계산할 데이터 부족")
        except Exception as ex:
            return {"error":f"BTC 필터 사용 중 데이터 오류. 안전을 위해 신호 계산을 중단합니다: {ex}"}
    rs_map=rs_by_time({m:complete(b,cfg.tf) for m,b in bars.items()},cfg) if cfg.strategy=="trend" and cfg.rs else None
    signals, watch, quality=[],[],{}
    for item in chosen:
        m=item["market"]
        if m not in bars:
            continue
        sig,wt,q=detect_all(m,bars[m],cfg,btc_map,rs_map,True)
        quality[m]=q
        for s in sig:
            s["turnover_24h"]=item["turnover"]
            signals.append(s)
        watch+=wt
    for s in signals[:12]:
        s["buy_ratio"]=simple_buy_ratio(s["market"])
    signals.sort(key=lambda x:x["turnover_24h"],reverse=True)
    watch=watch[:10]
    return {"version":VERSION,"checked":len(bars),"eligible":eligible,"signals":signals,"watch":watch,
            "errors":errors[:20],"updated":kst_now().strftime("%Y-%m-%d %H:%M:%S KST"),
            "quality":quality,"notes":["미완성 현재 봉은 신호에 사용하지 않습니다.","진입가는 확정 신호 다음 봉의 예상 시가이며 실시간 확정 체결가가 아닙니다."]}


def backtest(cfg):
    needed = math.ceil(cfg.days * 1440 / cfg.tf) + min_history(cfg) + 4
    if needed > 6500:
        return {"error":"선택한 기간과 분봉 조합은 6,500개 봉 한도를 넘습니다. 5분봉은 14일 이하로 검사하세요."}
    chosen,eligible=universe(cfg)
    if not chosen:
        return {"error":"조건을 만족하는 마켓이 없습니다."}
    data,errors=load_series(chosen,cfg)
    if not data:
        return {"error":"백테스트 데이터를 받지 못했습니다: "+("; ".join(errors[:2]))}
    btc_map=None
    if cfg.strategy=="trend" and cfg.btc:
        try:
            btc_bars=enrich(candles("KRW-BTC",cfg.tf,min(6500,math.ceil(cfg.days*1440/cfg.tf)+min_history(cfg)+4)),cfg.tf)
            btc_map=btc_ok_by_time(complete(btc_bars,cfg.tf))
        except Exception as e:
            return {"error":f"BTC 필터 데이터 누락: {e}"}
    rs_map=rs_by_time({m:complete(b,cfg.tf) for m,b in data.items()},cfg) if cfg.strategy=="trend" and cfg.rs else None
    events,coverage=[],{}
    order={x["market"]:i for i,x in enumerate(chosen)}
    for m,bars in data.items():
        e,_,q=detect_all(m,bars,cfg,btc_map,rs_map)
        coverage[m]=q
        for t in e:
            t["rank"]=order.get(m,999)
        events+=e
    # 다음 봉 시가로 진입하지 못하는 UNKNOWN은 통계에 수익/손실로 가산하지 않음.
    selected=select_trades(events,cfg)
    end_time = max(b[-1]["t"] for b in data.values() if b)
    cut_time = end_time - timedelta(days=cfg.days * 0.3)
    stat,daily,split=daily_and_split(selected,cfg,cut_time)
    unknown=[x for x in selected if x.get("status")!="CLOSED"]
    # 변경 가능한 전략 인자와 거래 대상/현실적 한계를 함께 응답
    return {"version":VERSION,"kind":cfg.strategy,"markets":len(data),"eligible":eligible,
            "signals":len(events),"selected":len(selected),"unknown":len(unknown),
            "stat":stat,"daily":daily,"split":split,"trades":selected[-600:],
            "coverage":coverage,"errors":errors[:20],"settings":{"fee_pct":cfg.fee*100,"slip_pct":cfg.slip*100,
               "per_trade_krw":cfg.capital,"period_days":cfg.days,"maxday":cfg.max_trades_day,
               "btc":cfg.btc,"rs":cfg.rs,"tf":cfg.tf},
            "warnings":["현재 시점의 거래대금 상위 종목으로 과거를 검사합니다(생존·선택 편향).",
                        "거래가 없는 분봉은 보간하지 않으며 해당 결과는 UNKNOWN으로 분리합니다.",
                        "동일 봉에서 손절과 익절 모두 닿으면 손절부터 체결했다고 가정합니다.",
                        "봉 데이터만으로 호가, 실제 체결, 대기열, 부분 체결을 재현하지 못합니다."]}


def holding(q,cfg):
    sym=q.get("sym",[""])[0].strip().upper()
    if not re.fullmatch(r"(?:KRW-)?[A-Z0-9]{2,20}",sym):
        return {"error":"코인은 XRP 같은 심볼로 입력하세요."}
    market=sym if sym.startswith("KRW-") else "KRW-"+sym
    entry=number(q,"price",0,0,1e15)
    try:
        et=datetime.strptime(q.get("time",[""])[0],"%Y-%m-%dT%H:%M")
    except (ValueError,IndexError):
        return {"error":"진입 시각을 선택해주세요."}
    age=(kst_now()-et).total_seconds()/86400
    if entry<=0 or age<0 or age>30:
        return {"error":"진입가는 양수, 진입 시각은 최근 30일 이내여야 합니다."}
    need=min(6500,math.ceil((age+4)*1440/cfg.tf)+min_history(cfg))
    bars=complete(enrich(candles(market,cfg.tf,need),cfg.tf),cfg.tf)
    if not bars:
        return {"error":"캔들 자료가 없습니다."}
    first=next((i for i,b in enumerate(bars) if b["t"]<=et<b["t"]+timedelta(minutes=cfg.tf)),None)
    if first is None or first<15 or bars[first-1]["atr"] is None:
        return {"error":"해당 진입 시각을 포함하는 과거 캔들이 없거나 ATR 자료가 부족합니다."}
    atr=bars[first-1]["atr"]
    stop0=entry-cfg.trail*atr
    if stop0<=0:
        return {"error":"초기 손절가가 0 이하입니다."}
    stop=stop0
    hh=entry
    tp=entry+cfg.tp_half_r*(entry-stop0) if cfg.tp_half_r else None
    be_trigger=entry+cfg.be_r*(entry-stop0) if cfg.be_r else None
    partial=False
    sold=None
    gaps=0
    # 과거부터 확정봉까지만 재생. 진입 당시 봉 고가/저가가 진입 이전일 수 있어 근사치.
    for i in range(first,len(bars)):
        b=bars[i]
        if i>first and b["t"]-bars[i-1]["t"]!=timedelta(minutes=cfg.tf):
            gaps+=1
            break
        if b["l"]<=stop:
            sold=b["t"].isoformat()
            break
        if tp and not partial and b["h"]>=tp:
            partial=True
        hh=max(hh,b["h"])
        stop=max(stop,hh-cfg.trail*(b["atr"] or atr))
        if be_trigger and b["h"]>=be_trigger:
            stop=max(stop,entry*(1+cfg.fee)/max(.000001,(1-cfg.fee)*(1-cfg.slip)))
    price=bars[-1]["c"]
    return {"market":market,"entry":entry,"latest":price,"pnl_est_pct":round(100*((price*(1-cfg.slip)*(1-cfg.fee))/(entry*(1+cfg.slip)*(1+cfg.fee))-1),2),
            "stop":stop,"stop0":stop0,"tp1":tp,"partial":partial,"sold_at":sold,
            "gaps":gaps,"time_kst":bars[-1]["t"].isoformat(),
            "note":"실제 체결·개인 거래 이력은 읽지 않습니다. 입력값과 확정 분봉으로만 추정한 참고선입니다."}

# -------------------------------- 화면 --------------------------------
HTML = r'''<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>BITHUMB V3.1 | 빗썸 전략 스캐너</title>
<style>
:root{color-scheme:dark;--bg:#080e1c;--panel:#111c33;--inner:#0b1429;--line:#283752;--text:#e9effb;--muted:#a0aec8;--good:#49deb4;--bad:#fb7777;--blue:#688dff;--gold:#f2bd70}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Noto Sans KR",Roboto,Arial,sans-serif;padding:18px 13px 56px;line-height:1.5}
main{max-width:940px;margin:0 auto}header{margin:8px 0 16px}h1{font-size:25px;margin:6px 0 3px;letter-spacing:-.6px}h2{font-size:17px;margin:0 0 12px}h3{font-size:15px;margin:13px 0 5px}small,.muted{color:var(--muted)}.sub{font-size:13px;color:var(--muted);line-height:1.65}.version{font-weight:800;font-size:12px;background:#233d77;color:#bcd1ff;padding:5px 9px;border-radius:9px;vertical-align:middle}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin:13px 0}.stat{background:var(--panel);border:1px solid var(--line);padding:14px;border-radius:15px}.stat .value{font-weight:800;font-size:26px;margin-top:3px}.stat small{font-size:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:17px;padding:16px;margin:11px 0;overflow:hidden}.g{color:var(--good)}.r{color:var(--bad)}.gold{color:var(--gold)}
label{display:block;color:var(--muted);font-size:13px;margin:11px 0 5px}select,input{width:100%;min-width:0;border-radius:11px;border:1px solid #34435e;background:var(--inner);padding:11px;color:var(--text);font-size:14px;outline:none}
select:focus,input:focus{border-color:var(--blue)}.row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px}.row3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:13px}button{cursor:pointer;background:#253658;border:1px solid #34496b;color:#e8ecfb;border-radius:11px;font-size:14px;font-weight:750;padding:12px 15px;flex:1;min-width:0}button.primary{background:var(--blue);border-color:var(--blue);color:#fff}button:disabled{opacity:.45;cursor:wait}.msg{font-size:12px;margin-top:12px;white-space:pre-wrap;color:var(--muted);line-height:1.55}
.entry{background:var(--inner);border:1px solid var(--line);border-radius:14px;padding:13px;margin:10px 0}.entry .top{display:flex;justify-content:space-between;gap:8px;align-items:start}.coin{font-size:22px;font-weight:800}.tag{display:inline-block;border-radius:99px;padding:4px 10px;background:#123b32;color:var(--good);font-size:12px;font-weight:700;white-space:nowrap}.tag.watch{background:#3e3324;color:var(--gold)}.detail{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:11px}.cell{border-radius:9px;padding:9px;background:#16233e}.cell b{display:block;font-size:15px;overflow-wrap:anywhere}.cell small{font-size:11px}.note{font-size:12px;color:var(--muted);line-height:1.65;margin-top:10px}.warn{color:var(--gold);font-size:12px;line-height:1.6}
.scroll{overflow-x:auto}.scroll table{width:100%;border-collapse:collapse;font-size:12px;white-space:nowrap}th,td{text-align:right;padding:9px 7px;border-bottom:1px solid var(--line)}th:first-child,td:first-child{text-align:left}th{color:var(--muted);font-weight:500}.bigresult{font-size:26px;font-weight:800;letter-spacing:-.7px}.section{border-top:1px solid var(--line);margin:12px 0;padding-top:11px}
@media(min-width:720px){body{padding:23px 20px}.stats{grid-template-columns:repeat(4,minmax(0,1fr))}.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}.two>.card{align-self:start}.stats .value{font-size:28px}}
</style></head><body><main>
<header><div><span class="version">NEW V3.1</span> <span class="muted" style="font-size:12px">빗썸 KRW · 주문 없는 연구용</span></div><h1>눌림목 & 추세추종 스캐너</h1><div class="sub">봉 마감 확인 → 다음 봉 진입 가정. 실시간 후보와 과거 백테스트를 같은 규칙으로 계산합니다. 기존 CORE.56과 독립 프로그램입니다.</div></header>
<div class="stats"><div class="stat"><small>검사 마켓</small><div class="value" id="k_count">–</div></div><div class="stat"><small>진입 후보</small><div class="value g" id="k_sig">–</div></div><div class="stat"><small>관심 후보</small><div class="value" id="k_watch">–</div></div><div class="stat"><small>최근 갱신 (KST)</small><div class="value" id="k_time" style="font-size:15px">–</div></div></div>
<div class="two"><div class="card"><h2>① 스캔 설정</h2>
<label>전략</label><select id="st"><option value="pullback">눌림목 · 박스 돌파 → 재테스트</option><option value="trend">추세추종 · 상위 봉 고점 돌파</option></select>
<div class="row"><div><label>캔들</label><select id="tf"><option value="15">15분봉</option><option value="5">5분봉</option><option value="60">1시간봉</option><option value="240">4시간봉</option></select></div><div><label>검색 종목 수</label><select id="n"><option value="20">20개</option><option value="10">10개</option><option value="40">40개</option><option value="60">60개</option></select></div></div>
<div class="row"><div><label>24시간 거래대금 하한</label><select id="liquidity"><option value="500000000">5억원</option><option value="1000000000">10억원</option><option value="2000000000">20억원</option><option value="0">제한 없음</option></select></div><div><label>편도 수수료</label><select id="fee"><option value="0.25">0.25%</option><option value="0.04">0.04%</option><option value="0">0%</option></select></div></div>
<div class="row"><div><label>편도 슬리피지</label><select id="slip"><option value="0.1">0.1%</option><option value="0">0%</option><option value="0.2">0.2%</option><option value="0.3">0.3%</option></select></div><div><label>건당 투자금</label><select id="amt"><option value="1000000">100만원</option><option value="500000">50만원</option><option value="2000000">200만원</option><option value="5000000">500만원</option></select></div></div>
<div id="pbopts"><div class="row"><div><label>눌림목 익절 R</label><select id="tp"><option value="1.8">1.8 R</option><option value="1.5">1.5 R</option><option value="2">2.0 R</option><option value="3">3.0 R</option></select></div><div><label>캔들 매수세 근사 필터</label><select id="of"><option value="weak">약하게</option><option value="off">끄기</option><option value="strong">강하게</option></select></div></div></div>
<div id="tropts" style="display:none"><div class="row"><div><label>ATR 트레일링</label><select id="trail"><option value="3">3.0배</option><option value="2">2.0배</option><option value="4">4.0배</option></select></div><div><label>일부 익절 (절반)</label><select id="tp1"><option value="0">끄기</option><option value="1">1R에서 절반</option><option value="1.5">1.5R에서 절반</option><option value="2">2R에서 절반</option></select></div></div>
<div class="row"><div><label>본전선 이동</label><select id="be"><option value="0">끄기</option><option value="0.5">+0.5R 도달 시</option><option value="0.75">+0.75R 도달 시</option><option value="1">+1R 도달 시</option></select></div><div><label>BTC 추세 필터</label><select id="btc"><option value="off">끄기</option><option value="on">켜기</option></select></div></div>
<div class="row"><div><label>상대강도 상위</label><select id="rs"><option value="0">끄기</option><option value="0.3">상위 30%</option><option value="0.5">상위 50%</option><option value="0.2">상위 20%</option></select></div><div><label>상대강도 측정 기간</label><select id="rsd"><option value="7">7일</option><option value="14">14일</option></select></div></div></div>
<div class="row"><div><label>하루 최대 진입</label><select id="maxday"><option value="4">4건</option><option value="3">3건</option><option value="2">2건</option><option value="1">1건</option></select></div><div><label>동시 보유</label><select id="maxpos"><option value="1">1개</option><option value="2">2개</option><option value="3">3개</option><option value="5">5개</option></select></div></div>
<div class="actions"><button class="primary" id="scanBtn" onclick="doScan()">지금 스캔</button><button id="autoBtn" onclick="autoSwitch()">자동 갱신 켜기</button></div><div class="msg" id="scanMsg">서버 연결 확인 중…</div></div>
<div class="card"><h2>② 진입 확인</h2><div id="signals" class="sub">아직 스캔하지 않았어요.</div><div class="section"><h2>③ 눌림 대기 / 관심 후보</h2><div id="watch" class="sub">아직 스캔하지 않았어요.</div></div></div></div>
<div class="two"><div class="card"><h2>④ 보유 코인 손절선 (추세추종)</h2><div class="sub">진입가·진입 시각을 입력하면 확정 분봉 기반 ATR 트레일링을 재현합니다. 실제 주문 기록을 조회하지 않습니다.</div><label>종목 (예: XRP)</label><input id="h_sym" autocapitalize="characters" placeholder="XRP"><div class="row"><div><label>내 진입가 (원)</label><input id="h_price" type="number" min="0" step="any" placeholder="예: 3250"></div><div><label>진입 시각 (KST)</label><input id="h_time" type="datetime-local"></div></div><div class="actions"><button id="holdBtn" class="primary" onclick="doHold()">트레일링 확인</button></div><div class="msg" id="holdMsg"></div><div id="holdResult"></div></div>
<div class="card"><h2>⑤ 백테스트</h2><div class="sub">학습 기간 70% / 검증 30%. 분봉 결측 또는 미래 봉 부족은 UNKNOWN으로 따로 집계합니다.</div><div class="row"><div><label>기간</label><select id="days"><option value="14">14일</option><option value="7">7일</option><option value="30">30일</option><option value="3">3일</option></select></div><div><label>계산 조건</label><input value="봉 마감·손절 우선" disabled></div></div><div class="actions"><button class="primary" id="btBtn" onclick="doBT()">백테스트 실행</button><button id="csvBtn" onclick="exportCSV()" disabled>CSV 저장</button></div><div id="btMsg" class="msg">기간·분봉·종목 수가 늘어나면 API 조회 시간이 길어질 수 있어요.</div><div id="btResult"></div></div></div>
<div class="card"><h2>검증할 때 꼭 볼 점</h2><div class="warn">• 백테스트는 과거 신호가 형성된 뒤 다음 분봉 시가로만 진입합니다.<br>• 손절과 익절이 같은 분봉에 닿으면 손절 우선. 공백 봉은 임의 생성하지 않아요.<br>• 백테스트 대상 종목은 현재 24시간 거래대금 순위이므로 과거 당시의 상장·거래대금과 다를 수 있어요.<br>• 실거래 호가·주문 체결을 재현하지 못하며 신호를 매수 보장으로 해석하면 안 돼요.<br>• 주문/API 키 기능이 없어요. 데이터 오류·검증 불충분시 거래 0회도 정상입니다.</div></div>
</main><script>
const $=id=>document.getElementById(id);let autoTimer=null, scanning=false, lastBT=null;
const fmt=v=>v===null||v===undefined?'–':Number(v).toLocaleString('ko-KR',{maximumFractionDigits:Number(v)<100?5:0});
const pct=v=>v===null||v===undefined?'–':(Number(v)>0?'+':'')+Number(v).toFixed(2)+'%';
const krw=v=>(v>0?'+':'')+Number(v||0).toLocaleString('ko-KR')+'원';
const esc=s=>String(s??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const ids=['st','tf','n','liquidity','fee','slip','amt','tp','of','trail','tp1','be','btc','rs','rsd','maxday','maxpos','days'];
function qs(){const p=new URLSearchParams();ids.forEach(id=>p.set(id,$(id).value));return p.toString()}
function changeStrategy(){let trend=$('st').value==='trend';$('pbopts').style.display=trend?'none':'block';$('tropts').style.display=trend?'block':'none';const wanted=trend?['60','240']:['5','15','60'];
 [...$('tf').options].forEach(o=>o.hidden=!wanted.includes(o.value));if(!wanted.includes($('tf').value))$('tf').value=trend?'60':'15';}
$('st').onchange=changeStrategy;changeStrategy();
async function request(path){const r=await fetch(path);let d;try{d=await r.json()}catch(e){throw Error('서버 JSON 응답이 아닙니다')};if(!r.ok||d.error)throw Error(d.error||'HTTP '+r.status);return d}
async function ping(){try{const d=await request('/api/ping');$('scanMsg').textContent='서버 연결 완료 · '+esc(d.version)+' · 마지막 스캔을 실행할 수 있습니다.'}catch(e){$('scanMsg').textContent='연결 실패: '+e.message}}
function tiles(items){return `<div class="detail">${items.map(([k,v])=>`<div class="cell"><small>${esc(k)}</small><b>${esc(v)}</b></div>`).join('')}</div>`}
function card(s){return `<div class="entry"><div class="top"><div><div class="coin">${esc(s.market.replace('KRW-',''))} <small>${esc(s.strategy==='trend'?'추세추종':'눌림목')}</small></div><small>신호 확정: ${esc(s.signal_at.replace('T',' '))}</small></div><span class="tag">마감봉 확인</span></div>${tiles([['예상 진입가',fmt(s.price)+'원'],['구조 손절선',fmt(s.stop)+'원'],['초기 위험',Number(s.risk_pct).toFixed(2)+'%'],[s.tp?'1차 목표가':'돌파 고점',fmt(s.tp||s.context.box)+'원']])}<div class="note">거래량 ${esc(s.context.volume_ratio)}배 · 24시간 거래대금 ${s.turnover_24h?fmt(s.turnover_24h/100000000)+'억원':'–'}${s.buy_ratio==null?'':' · 최근 200체결 매수 비율 '+s.buy_ratio+'%'}</div></div>`}
async function doScan(){if(scanning)return;scanning=true;const b=$('scanBtn');b.disabled=true;b.textContent='조회 중…';$('scanMsg').textContent='빗썸 공개 시세로 완료봉을 검사 중입니다.';
try{const d=await request('/api/scan?'+qs());$('k_count').textContent=d.checked;$('k_sig').textContent=d.signals.length;$('k_watch').textContent=d.watch.length;$('k_time').textContent=d.updated.slice(11,19);
$('signals').innerHTML=d.signals.length?d.signals.map(card).join(''):'조건을 모두 통과한 진입 후보가 없습니다.';
$('watch').innerHTML=d.watch.length?d.watch.map(s=>`<div class="entry"><b>${esc(s.market)}</b><span class="tag watch">${esc(s.status||'관심')}</span><div class="sub">기준 고점 ${fmt(s.box)}원 · 마지막 완료봉 ${fmt(s.price)}원</div></div>`).join(''):'관심 후보가 없습니다.';
$('scanMsg').textContent=`검사 완료 ${d.checked}개 / 거래대금 조건 충족 ${d.eligible}개 · ${d.updated}`+(d.errors.length?`\n자료 부족/오류 ${d.errors.length}건: `+d.errors.slice(0,2).join(' / '):'');
}catch(e){$('scanMsg').textContent='스캔 오류: '+e.message}finally{scanning=false;b.disabled=false;b.textContent='지금 스캔'}}
function autoSwitch(){if(autoTimer){clearInterval(autoTimer);autoTimer=null;$('autoBtn').textContent='자동 갱신 켜기'}else{doScan();autoTimer=setInterval(()=>{if(!scanning)doScan()},90000);$('autoBtn').textContent='자동 갱신 끄기 (90초)'}}
async function doHold(){let b=$('holdBtn');b.disabled=true;b.textContent='계산 중…';try{let q=new URLSearchParams(qs());q.set('sym',$('h_sym').value);q.set('price',$('h_price').value);q.set('time',$('h_time').value);q.set('st','trend');if(!['60','240'].includes(q.get('tf')))q.set('tf','60');
let d=await request('/api/hold?'+q.toString());$('holdMsg').textContent=d.note;
$('holdResult').innerHTML=`<div class="entry"><h3>${esc(d.market)} · 최근 완료봉 ${esc(d.time_kst.replace('T',' '))}</h3>${tiles([['현재 참고 종가',fmt(d.latest)+'원'],['수수료·슬리피지 차감 추정',pct(d.pnl_est_pct)],['현재 손절 기준',fmt(d.stop)+'원'],['최초 손절 기준',fmt(d.stop0)+'원']])}<div class="note">일부 익절 ${d.tp1?fmt(d.tp1)+'원':'설정 없음'} · ${d.partial?'절반 익절 도달 가정':'미도달'}<br>${d.sold_at?'규칙상 과거 손절선에 닿은 시각: '+esc(d.sold_at):'완료봉 기준 미청산 가정'}${d.gaps?' · 봉 결측 발견: 이후 추정 중단':''}</div></div>`;
}catch(e){$('holdMsg').textContent='오류: '+e.message}finally{b.disabled=false;b.textContent='트레일링 확인'}}
function trStat(label,s){return `<tr><td>${label}</td><td>${s?.n??0}</td><td>${s?.win_rate??'–'}%</td><td>${s?.avg_net==null?'–':pct(s.avg_net)}</td><td>${s?.pnl_krw==null?'–':krw(s.pnl_krw)}</td></tr>`}
function renderBT(d){let s=d.stat;let h=`<div class="section"><small>검사 마켓 ${d.markets}개 · 후보 ${d.signals}건 · 선택 ${d.selected}건 · 미확정 ${d.unknown}건</small><div class="bigresult ${s.avg_net>0?'g':s.avg_net<0?'r':''}">${s.avg_net==null?'거래 없음':'평균 '+pct(s.avg_net)}</div><div class="sub">승률 ${s.win_rate??'–'}% · ${s.win}승 ${s.loss}패 · 동일 금액 건별 합산 ${krw(s.pnl_krw)}</div></div>`;
h+=`<h3>학습 / 검증 (시간순 70/30)</h3><div class="scroll"><table><tr><th>기간</th><th>거래</th><th>승률</th><th>평균순익</th><th>총손익</th></tr>${trStat('전체',d.stat)}${trStat('학습',d.split.train)}${trStat('검증',d.split.test)}</table></div>`;
h+=`<h3>날짜별</h3><div class="scroll"><table><tr><th>일자</th><th>건수</th><th>승/패</th><th>평균순익</th><th>손익(원)</th></tr>${d.daily.map(x=>`<tr><td>${x.date}</td><td>${x.n}</td><td>${x.win}/${x.loss}</td><td>${pct(x.avg_net)}</td><td class="${x.pnl_krw>0?'g':'r'}">${krw(x.pnl_krw)}</td></tr>`).join('')||'<tr><td colspan="5">데이터 없음</td></tr>'}</table></div>`;
h+=`<h3>최근 선택 거래</h3><div class="scroll"><table><tr><th>진입</th><th>마켓</th><th>종료사유</th><th>순수익</th></tr>${d.trades.slice(-30).reverse().map(t=>`<tr><td>${esc(t.entry_at.slice(5,16).replace('T',' '))}</td><td>${esc(t.market.replace('KRW-',''))}</td><td>${esc(t.why||'–')}</td><td class="${t.net_pct>0?'g':'r'}">${t.net_pct==null?'미확정':pct(t.net_pct)}</td></tr>`).join('')||'<tr><td colspan="4">거래 없음</td></tr>'}</table></div>`;
h+=`<div class="note">${d.warnings.map(x=>'• '+esc(x)).join('<br>')}${d.errors.length?'<br>• 데이터 부족: '+d.errors.map(esc).slice(0,3).join(' / '):''}</div>`;return h}
async function doBT(){const b=$('btBtn');b.disabled=true;b.textContent='백테스트 중…';$('csvBtn').disabled=true;$('btMsg').textContent='API 데이터를 모으고 가격 흐름을 검증하는 중입니다.';try{const d=await request('/api/backtest?'+qs());lastBT=d;$('btResult').innerHTML=renderBT(d);$('btMsg').textContent='계산 완료 · '+d.settings.period_days+'일 · '+d.settings.tf+'분봉 · 수수료 편도 '+d.settings.fee_pct+'% · 슬리피지 편도 '+d.settings.slip_pct+'%';$('csvBtn').disabled=false}
catch(e){$('btMsg').textContent='백테스트 오류: '+e.message}finally{b.disabled=false;b.textContent='백테스트 실행'}}
function exportCSV(){if(!lastBT)return;const d=lastBT;const objs=[...d.daily.map(x=>({row_type:'DAILY',...x})),...d.trades.map(x=>({row_type:'TRADE',version:d.version,...x}))];const columns=[...new Set(['row_type','version','date','market','signal_at','entry_at','exit_at','status','net_pct',...objs.flatMap(x=>Object.keys(x))])];const cell=v=>{const s=typeof v==='object'&&v!==null?JSON.stringify(v):String(v??'');return /[",\n\r]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s};
const csv='\ufeff'+[columns.join(','),...objs.map(x=>columns.map(k=>cell(x[k])).join(','))].join('\r\n');const blob=new Blob([csv],{type:'text/csv;charset=utf-8'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=`BITHUMB_V3_1_${d.kind}_${d.settings.tf}m_${d.settings.period_days}d.csv`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000)}
ping();
</script></body></html>'''


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # 요청 URL에는 전략 파라미터만 포함되지만 기본 로그는 간략화
        print("[V3]",self.address_string(),fmt % args)

    def send_body(self, status, body, content_type="application/json"):
        raw = (json.dumps(body, ensure_ascii=False, allow_nan=False) if content_type == "application/json" else body).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type+"; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self):
        route = urlparse(self.path)
        if route.path == "/":
            return self.send_body(200, HTML, "text/html")
        if route.path in ("/health", "/api/ping"):
            return self.send_body(200, {"ok": True, "version": VERSION, "local_kst":kst_now().isoformat(), "trading":False})
        if route.path not in ("/api/scan", "/api/backtest", "/api/hold"):
            return self.send_body(404, {"error":"지원하지 않는 경로입니다."})
        if not BUSY.acquire(blocking=False):
            return self.send_body(429, {"error":"다른 스캔/백테스트가 진행 중입니다. 해당 작업을 완료한 후 다시 실행하세요."})
        try:
            q=parse_qs(route.query)
            cfg=Settings.from_q(q)
            if route.path == "/api/scan":
                result=scan(cfg)
            elif route.path == "/api/backtest":
                result=backtest(cfg)
            else:
                if cfg.strategy != "trend":
                    from dataclasses import replace
                    cfg=replace(cfg,strategy="trend",tf=60)
                result=holding(q,cfg)
            return self.send_body(200 if "error" not in result else 422, result)
        except Exception as e:
            print("[V3] 오류:",repr(e))
            return self.send_body(502, {"error":str(e)[:300]})
        finally:
            BUSY.release()


def main():
    env_port=os.getenv("PORT")
    if env_port:
        host,ports="0.0.0.0",[int(env_port)]
    else:
        host,ports="127.0.0.1",list(range(8080,8090))
    server=None
    for port in ports:
        try:
            server=ThreadingHTTPServer((host,port),WebHandler)
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError("사용 가능한 서버 포트가 없습니다.")
    url=f"http://127.0.0.1:{server.server_port}"
    print("="*45)
    print(f"BITHUMB {VERSION} 연구용 스캐너 실행 완료")
    print("화면: "+url)
    print("종료하려면 실행 중인 Pydroid/Python을 중지하세요.")
    print("거래·주문 기능 없음. 공개 시세 분석 전용.")
    print("="*45)
    if not env_port:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("종료합니다.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
