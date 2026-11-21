#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BITHUMB × Binance futures lead-lag research V1.1 (READ-ONLY).

Python >=3.9; no third-party dependencies. CLI and mobile-friendly local/Render web UI.
Usage:
  python futures_lead_research.py --selftest
  python futures_lead_research.py --coins ETH,XRP,SOL --days 6
  python futures_lead_research.py --web --port 10000
Optional environment: BINANCE_API_KEY (market-data only; never provide secret or trade access).
"""
import argparse
import bisect
import csv
import io
import json
import math
import os
import re
import statistics
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

VERSION = "1.1"
BINANCE = "https://fapi.binance.com"
BITHUMB = "https://api.bithumb.com"
PERIOD_MS = 15 * 60_000
CANDLE_MS = 5 * 60_000
HOURS_MS = 60 * 60_000
KST = timezone(timedelta(hours=9))
MAX_DAYS = 13
MAX_COINS = 20
DEFAULT_COINS = "ETH,XRP,SOL,DOGE,ADA,LINK,AVAX,SUI,BTC,PEPE"
STABLES = {"USDT","USDC","DAI","FDUSD","TUSD","USDE","USD1","USDD","PYUSD","RLUSD","EURC","EURI"}
MULTIPLIED = {"PEPE":"1000PEPE", "SHIB":"1000SHIB", "BONK":"1000BONK", "FLOKI":"1000FLOKI"}
RULES = {
    "A": "롱·OI·매수 동행",
    "B": "OI 감소·롱비율 상승",
    "C": "OI·매수 강세(비율 제외)",
    "M": "비교군: 가격 상승만",
}
FIELDS = ["코인","규칙","신호시각(KST)","진입시각(KST)","진입가(KRW)",
          "1시간 OI 변화(%)","1시간 롱숏비 변화(%)","최근1시간 매수/매도량비",
          "이전1시간 빗썸 상승률(%)","30분 순수익률(%)","60분 순수익률(%)","180분 순수익률(%)"]

class APIError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def fetch_json(base, path, params=None, headers=None):
    """Allowlisted hosts and paths only. Retry transient server/rate-limit failures."""
    if base not in (BINANCE, BITHUMB) or not path.startswith("/") or '//' in path:
        raise APIError("허용되지 않은 API 경로")
    url = base + path + ("?" + urlencode(params) if params else "")
    req = Request(url, headers={"User-Agent": "BithumbBinanceReadOnlyResearch/1.1",
                                "Accept":"application/json", **(headers or {})})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=14) as r:
                return json.loads(r.read(8_000_000).decode("utf-8"))
        except HTTPError as e:
            content = e.read(500).decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(1.5 * (attempt + 1)); continue
            if e.code == 451:
                raise APIError("지역/네트워크 제한으로 거래소 API에 접근할 수 없음 (HTTP 451)", 451)
            raise APIError(f"거래소 HTTP {e.code}: {content[:180]}", e.code)
        except (URLError, TimeoutError, OSError, ValueError) as e:
            if attempt < 2:
                time.sleep(attempt + 1); continue
            raise APIError(f"API 통신 실패: {str(e)[:150]}")


def timestamp_fmt(ms):
    return datetime.fromtimestamp(ms/1000, KST).strftime("%m-%d %H:%M")


def rounded_end(ts):
    # OI and top position ratio timestamps represent an END of period.
    return int(round(ts / PERIOD_MS) * PERIOD_MS)


def taker_end(ts):
    # Taker volume timestamps represent the START of period (important!).
    return int(ts // PERIOD_MS * PERIOD_MS + PERIOD_MS)


def history(path, symbol, start_ms, end_ms, headers=None):
    """Page 15m Binance metrics with bounded startTime/endTime windows.

    Asking for a 6-day range with ``limit=500`` in one request can be rejected
    as HTTP 400/-1130 (invalid endTime) even though both parameters are allowed.
    Request at most 400 intervals per page, keep exact non-overlapping ranges,
    and retry using 96-interval pages only if Binance still rejects endTime.
    Empty intermediate windows do not prevent scanning subsequent windows.
    """
    if end_ms < start_ms:
        return {}
    out = {}
    cursor = start_ms - PERIOD_MS  # Include raw taker start-time at first boundary.
    slots_per_page = 400
    for _ in range(40):  # 13 days with fallback 1-day windows needs < 20 calls.
        if cursor > end_ms:
            return out
        page_end = min(end_ms, cursor + slots_per_page * PERIOD_MS - 1)
        params = {
            "symbol": symbol, "period": "15m", "startTime": int(cursor),
            "endTime": int(page_end), "limit": 500,
        }
        try:
            data = fetch_json(BINANCE, path, params, headers)
        except APIError as e:
            if e.status == 400 and "endTime" in str(e) and slots_per_page > 96:
                slots_per_page = 96
                continue
            raise APIError(
                f"{path} 기간별 조회 실패 ({timestamp_fmt(cursor)}~"
                f"{timestamp_fmt(page_end)}, {slots_per_page}구간): {e}", e.status
            ) from e
        if not isinstance(data, list):
            raise APIError(f"바이낸스 반환 형식 오류 ({path}): {str(data)[:120]}")
        for row in data:
            raw = int(row["timestamp"])
            t = taker_end(raw) if path.endswith("takerlongshortRatio") else rounded_end(raw)
            if start_ms <= t <= end_ms:
                out[t] = row
        # Always advance by the *requested* window, even if Binance returns
        # fewer than 500 observations (e.g. no trades or missing data).
        cursor = page_end + 1
    raise APIError(f"바이낸스 페이지 조회 한도 초과 ({path})")


def bih_markets():
    data = fetch_json(BITHUMB, "/v1/market/all", {"isDetails":"false"})
    if not isinstance(data, list): raise APIError("빗썸 종목 목록 형식 오류")
    return {x.get("market","").split("-",1)[1] for x in data
            if x.get("market","").startswith("KRW-") and "-" in x.get("market","")}


def bin_symbols():
    data = fetch_json(BINANCE, "/fapi/v1/exchangeInfo")
    if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
        raise APIError("바이낸스 선물 종목 목록 형식 오류")
    return {x.get("baseAsset"): x.get("symbol") for x in data["symbols"]
            if x.get("quoteAsset") == "USDT" and x.get("contractType") == "PERPETUAL"
            and x.get("status") == "TRADING"}


def coins_parse(value):
    tokens = [x.strip().upper().replace("KRW-", "") for x in str(value).split(",")]
    tokens = list(dict.fromkeys(x for x in tokens if x))
    if len(tokens) > MAX_COINS: raise ValueError(f"최대 {MAX_COINS}종목까지 가능")
    if not tokens: raise ValueError("연구할 코인을 하나 이상 입력해 주세요")
    for token in tokens:
        if not re.fullmatch(r"[A-Z0-9]{2,16}", token):
            raise ValueError("종목명은 영문·숫자만 사용하세요")
    return tokens


def bih_candles(coin, start_ms, end_ms):
    """Fetch 5m closed Bithumb candles, paged by 'to' interpreted as KST."""
    result = {}
    cursor = end_ms
    for _ in range(25):
        to_kst = datetime.fromtimestamp(cursor/1000, KST).strftime("%Y-%m-%dT%H:%M:%S")
        data = fetch_json(BITHUMB, "/v1/candles/minutes/5", {
            "market": "KRW-"+coin, "to": to_kst, "count":200})
        if not isinstance(data,list): raise APIError("빗썸 캔들 응답 오류")
        if not data: break
        oldest = None
        for x in data:
            utc_s = x.get("candle_date_time_utc")
            if not utc_s: continue
            t = int(datetime.fromisoformat(utc_s).replace(tzinfo=timezone.utc).timestamp()*1000)
            oldest = t if oldest is None else min(oldest, t)
            if start_ms <= t and t + CANDLE_MS <= end_ms:
                result[t] = float(x["trade_price"])
        if oldest is None or oldest >= cursor or oldest <= start_ms or len(data) < 200:
            break
        cursor = oldest  # 'to' excludes candle opening at this time
    return result


def percent_change(curr, prior):
    return (curr/prior - 1)*100 if prior and prior > 0 and curr is not None else None


def row_features(t, idx, times, oi, lsr, taker, prices):
    """Only use fully closed intervals and previously available price candles."""
    if idx < 4: return None
    lookback = times[idx-4]
    # Must have an actual one-hour span; never bridge missing OI observations.
    if t - lookback != HOURS_MS: return None
    o0, o1 = oi.get(lookback), oi.get(t)
    if not o0 or not o1: return None
    oi_pct = percent_change(float(o1["sumOpenInterest"]),float(o0["sumOpenInterest"]))
    if oi_pct is None: return None
    ratio_pct = None
    if t in lsr and lookback in lsr:
        ratio_pct = percent_change(float(lsr[t]["longShortRatio"]),float(lsr[lookback]["longShortRatio"]))
    buy = sell = 0.0
    for tt in (t - 3*PERIOD_MS, t-2*PERIOD_MS, t-PERIOD_MS, t):
        r = taker.get(tt)
        if not r: return None
        buy += float(r.get("buyVol",0))
        sell += float(r.get("sellVol",0))
    if sell <= 0: return None
    # Both price points existed BEFORE or AT signal time.
    prev_close = prices.get(t-HOURS_MS-CANDLE_MS)
    last_close = prices.get(t-CANDLE_MS)
    if prev_close is None or last_close is None: return None
    momentum_pct = percent_change(last_close,prev_close)
    return {"oi":oi_pct,"ratio":ratio_pct,"taker":buy/sell,"momentum":momentum_pct}


def classify(f):
    if not f: return []
    flags = []
    momentum_up = f["momentum"] >= 0.5
    oi_up = f["oi"] >= 0.8
    buys = f["taker"] >= 1.08
    if momentum_up: flags.append("M")
    if oi_up and buys and momentum_up:
        flags.append("C")
    if f["ratio"] is not None and f["ratio"] >= 2.0 and buys and momentum_up:
        if oi_up: flags.append("A")
        if f["oi"] <= -0.8: flags.append("B")
    return flags


def produce_trades(coin, oi, lsr, taker, prices, since_ms, until_ms, fee_pct=0.5):
    times = sorted(x for x in oi if x <= until_ms)
    last_used = {r: -10**18 for r in RULES}
    result = []
    for idx,t in enumerate(times):
        if not (since_ms <= t <= until_ms - 180*60_000 - CANDLE_MS): continue
        f = row_features(t, idx, times, oi, lsr, taker, prices)
        rules = classify(f)
        if not rules: continue
        # Signal becomes known at 15-minute END t; earliest tradable observation
        # is the close of the NEXT 5m candle, which opens at t.
        entry = prices.get(t)
        # t is the opening of the entry candle; it closes at t+5m.
        if entry is None: continue
        exits = {minutes:prices.get(t + minutes*60_000) for minutes in (30,60,180)}
        if any(v is None for v in exits.values()): continue
        for rule in rules:
            if t - last_used[rule] < 180*60_000: continue
            last_used[rule] = t
            row = {"coin":coin, "rule":rule, "signal_ms":t,
                   "entry_ms":t+CANDLE_MS, "entry_price":entry, **f}
            for minutes,exitprice in exits.items():
                row[f"net{minutes}"] = (exitprice/entry-1)*100 - fee_pct
            result.append(row)
    return result


def describe(trades):
    out = []
    for rule,label in RULES.items():
        group = [r for r in trades if r["rule"] == rule]
        item = {"rule":rule,"label":label,"n":len(group),"coins":len({r['coin'] for r in group})}
        for mins in (30,60,180):
            vals = [r[f"net{mins}"] for r in group]
            item[f"avg{mins}"] = round(statistics.mean(vals),3) if vals else None
            item[f"med{mins}"] = round(statistics.median(vals),3) if vals else None
            item[f"win{mins}"] = round(100 * sum(v>0 for v in vals)/len(vals),1) if vals else None
        out.append(item)
    return out


def describe_daily(trades):
    daily = {}
    for r in trades:
        day = datetime.fromtimestamp(r["signal_ms"]/1000, KST).strftime("%Y-%m-%d")
        key = (day, r["rule"])
        daily.setdefault(key, []).append(r["net60"])
    return [{"day":day,"rule":rule,"n":len(v),
             "wins":sum(x>0 for x in v),"losses":sum(x<=0 for x in v),
             "mean_net60":round(statistics.mean(v),3)}
            for (day,rule),v in sorted(daily.items())]


def csv_text(trades):
    b = io.StringIO()
    w = csv.writer(b)
    w.writerow(FIELDS)
    for r in sorted(trades,key=lambda x:(x['signal_ms'],x['coin'],x['rule'])):
        w.writerow([r['coin'],r['rule'],timestamp_fmt(r['signal_ms']), timestamp_fmt(r['entry_ms']),
                    r['entry_price'],round(r['oi'],3),
                    '' if r['ratio'] is None else round(r['ratio'],3),round(r['taker'],3),
                    round(r['momentum'],3),round(r['net30'],3),round(r['net60'],3),round(r['net180'],3)])
    return b.getvalue()


def research(coins, days, on_progress=None, now_ms=None):
    if not (1 <= days <= MAX_DAYS): raise ValueError(f"기간은 1~{MAX_DAYS}일")
    now_ms = now_ms or int(time.time()*1000)
    # Use the last completed 15m period, and exclude the most recent 3h from
    # evaluation because its future returns are still unknown.
    end_ms = (now_ms//PERIOD_MS) * PERIOD_MS
    since_ms = end_ms - days*24*HOURS_MS
    fetch_since = since_ms - 2*HOURS_MS
    coins = coins_parse(coins) if isinstance(coins,str) else coins
    progress = on_progress or (lambda message, pct: None)
    progress("시장 공통 종목 확인", 3)
    bithumb = bih_markets()
    futures = bin_symbols()
    mapped,skip = [],[]
    for c in coins:
        fut = futures.get(c) or futures.get(MULTIPLIED.get(c,""))
        if c in STABLES or c not in bithumb or not fut:
            skip.append(c); continue
        mapped.append((c,fut))
    if not mapped:
        raise APIError("공통 종목이 없습니다. 빗썸 KRW와 바이낸스 USDT 무기한 선물 상장 여부를 확인해 주세요")
    rows,errors,warnings = [],[],[]
    ratio_disabled = False
    for k,(coin,symbol) in enumerate(mapped,1):
        progress(f"{coin} ({k}/{len(mapped)}) 15분봉 선물 데이터 수집", 5 + 87*(k-1)/len(mapped))
        try:
            oi = history("/futures/data/openInterestHist",symbol,fetch_since,end_ms)
            taker = history("/futures/data/takerlongshortRatio",symbol,fetch_since,end_ms)
            lsr = {}
            if not ratio_disabled:
                key = os.environ.get("BINANCE_API_KEY", "").strip()
                headers = {"X-MBX-APIKEY":key} if key else None
                try:
                    lsr = history("/futures/data/topLongShortPositionRatio",symbol,fetch_since,end_ms,headers)
                except APIError as e:
                    if e.status in (401,403):
                        ratio_disabled = True
                        warnings.append("상위 트레이더 포지션 비율 API 접근 거절: A/B 비율 규칙 분석 불가. 선택적으로 BINANCE_API_KEY 환경변수 설정 필요")
                    else:
                        raise
            progress(f"{coin} 빗썸 5분봉과 선물 지표 정렬", 5 + 87*(k-0.5)/len(mapped))
            prices = bih_candles(coin,fetch_since-HOURS_MS,end_ms)
            if not oi or not taker or not prices:
                errors.append(f"{coin}: 필요한 과거 데이터가 부족함 (OI={len(oi)}, 매수매도={len(taker)}, 빗썸={len(prices)})")
                continue
            matches = produce_trades(coin,oi,lsr,taker,prices,since_ms,end_ms)
            rows.extend(matches)
            if not matches:
                errors.append(f"{coin}: 조건에 맞는 완결 신호 0건 (OI={len(oi)}, 매수매도={len(taker)}, 비율={len(lsr)}, 빗썸={len(prices)})")
        except APIError as e:
            errors.append(f"{coin}: {e}")
        except (KeyError,ValueError,TypeError,ZeroDivisionError) as e:
            errors.append(f"{coin}: 데이터 구조/값 오류 - {str(e)[:130]}")
    progress("수익률과 비교군 집계", 97)
    return {"version":VERSION,"days":days,"since_kst":timestamp_fmt(since_ms),
            "until_kst":timestamp_fmt(end_ms),"requested":len(coins),"mapped":len(mapped),
            "mapped_coins":[a for a,b in mapped],"skipped":skip,"errors":errors,
            "warnings":warnings,"ratio_available":not ratio_disabled and any(r["ratio"] is not None for r in rows),
            "trades":rows,"summary":describe(rows),"daily":describe_daily(rows),"completed_at":timestamp_fmt(int(time.time()*1000))}


def selftest():
    # Deterministic, offline tests; NOT evidence of real market profitability.
    t = 20_000*PERIOD_MS
    oi = {}
    lsr = {}
    taker = {}
    prices = {}
    for i in range(30):
        t0 = t + i*PERIOD_MS
        oi[t0] = {"sumOpenInterest":str(1000+i*5),"timestamp":t0}
        lsr[t0] = {"longShortRatio":str(1+i*0.015),"timestamp":t0}
        taker[t0] = {"buyVol":"110", "sellVol":"100", "timestamp":t0-PERIOD_MS}
    for j in range(-20,110):
        prices[t+j*CANDLE_MS] = 100 + 0.08*j
    assert rounded_end(t+100) == t
    assert taker_end(t-PERIOD_MS) == t
    f = row_features(t+4*PERIOD_MS,4,sorted(oi),oi,lsr,taker,prices)
    assert f and f["oi"] > 0.8 and f["ratio"] > 2 and f["taker"] > 1
    assert "A" in classify(f)
    # No future data: if 1-hour past candle missing, cannot form signal.
    broken = dict(prices)
    del broken[t+4*PERIOD_MS - HOURS_MS - CANDLE_MS]
    assert row_features(t+4*PERIOD_MS,4,sorted(oi),oi,lsr,taker,broken) is None
    signals = produce_trades("TEST",oi,lsr,taker,prices,t,t+29*PERIOD_MS)
    assert signals and all(r['entry_ms']>r['signal_ms'] for r in signals)
    # The first 5m candle closes AFTER the signal; 30m forward close is exact.
    r = signals[0]
    expected = (prices[r['signal_ms']+30*60_000] / prices[r['signal_ms']] -1)*100 - 0.5
    assert abs(r['net30']-expected)<1e-9
    assert len([x for x in signals if x['rule']=='A']) <= 3  # 3h cooldown
    assert "30분 순수익률(%)" in csv_text(signals)
    print("SELFTEST PASS: timestamp alignment, closed-bar entry, no-lookahead, cooldown, fee, CSV")

# Mobile first, no external JS/CSS/CDN.
HTML = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>선물 선행성 연구 V1.1</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}body{background:#0e1421;color:#e6ebf5;font-family:system-ui,-apple-system,sans-serif;margin:0}main{max-width:1000px;padding:22px 14px 60px;margin:auto}h1{font-size:23px;margin:8px 0}h2{font-size:18px;margin:24px 0 12px}.sub{color:#a1aec1;font-size:14px;line-height:1.6}section{background:#172234;border:1px solid #2a3c52;border-radius:14px;padding:16px;margin:15px 0}.label{font-size:13px;color:#a5b4c8}input{width:100%;background:#0e1728;border:1px solid #41546d;border-radius:9px;color:white;padding:12px;margin:5px 0 13px;font-size:16px}button,.link{border:0;color:white;padding:12px 16px;border-radius:10px;background:#257ad2;font-size:15px;cursor:pointer;text-decoration:none;display:inline-block}button:disabled{opacity:.45}table{border-collapse:collapse;width:100%;font-size:13px;min-width:730px}td,th{border-bottom:1px solid #314056;padding:11px 8px;text-align:right}td:first-child,th:first-child{text-align:left}th{color:#a1b0c6;font-weight:500}.scroll{overflow:auto}.bad{color:#ffb68e}.ok{color:#85d8bd}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.stat{padding:9px;border:1px solid #334a65;border-radius:9px}.stat strong{display:block;margin-top:6px;font-size:18px}pre{white-space:pre-wrap;word-break:break-word;font-size:12px;color:#adbcd3}.muted{color:#97a5b9}small{font-size:12px}@media(max-width:600px){.stats{grid-template-columns:repeat(2,1fr)}table{font-size:12px}main{padding:12px}}</style></head><body><main>
<div class="label">BITHUMB × BINANCE / 독립 연구용 · 주문 기능 없음</div><h1>선물 데이터 선행성 연구 V1.1</h1><p class="sub">선물의 미결제약정(OI)·상위 트레이더 롱/숏 비율·시장가 매수/매도량 변화가 <b>빗썸 현물 가격 상승보다 먼저</b> 나타나는지 검증합니다. 투자 신호가 아닌 과거 통계 연구입니다.</p>
<section><div class="label">연구 종목 (쉼표로 구분, 최대 20개)</div><input id="coins" value="ETH,XRP,SOL,DOGE,ADA,LINK,AVAX,SUI,BTC,PEPE"/><div class="label">최근 연구 일수 (1~13)</div><input id="days" type="number" value="6" min="1" max="13"/><button id="go" onclick="start()">연구 실행</button><p class="sub">6일: 바이낸스 15분봉과 빗썸 5분봉을 실제 API로 수집합니다. 서버에 따라 수집이 오래 걸릴 수 있습니다. 조건에 맞는 신호가 0건이어도 정상입니다.</p></section>
<section><div class="label">진행 상태</div><h2 id="state">실행 전</h2><div class="sub" id="progress">실제 거래소 API를 호출하려면 연구 실행을 눌러주세요.</div></section>
<div id="result" hidden><section><h2>연구 범위</h2><div id="scope" class="stats"></div><p id="notice" class="sub"></p></section>
<section><h2>규칙별 성과 (편도 수수료 0.25% × 2 반영)</h2><p class="sub">30·60·180분 뒤 순수익률 평균 / 순이익 발생 비율. 수익률은 각 시점의 종가 기준이며, 실제 체결·슬리피지는 반영되지 않습니다.</p><div class="scroll"><table><thead><tr><th>규칙</th><th>건수</th><th>30분 평균</th><th>30분 승률</th><th>1시간 평균</th><th>1시간 승률</th><th>3시간 평균</th><th>3시간 승률</th></tr></thead><tbody id="summary"></tbody></table></div><p class="sub">A: OI↑ + 상위 롱비율↑ + 매수우세 + 가격↑<br/>B: OI↓ + 상위 롱비율↑ + 매수우세 + 가격↑<br/>C: OI↑ + 매수우세 + 가격↑ (비율 조건 제거)<br/>M: 가격↑만으로 선별한 비교군. 각 규칙당 같은 코인 연속 신호는 3시간 동안 중복 제외합니다.</p></section>
<section><h2>날짜별 결과 (1시간 후)</h2><p class="sub">KST 날짜별 순수익 건수·손실 건수·평균 순수익률입니다. 통계상 건수가 적은 날은 해석에 주의하세요.</p><div class="scroll"><table style="min-width:520px"><thead><tr><th>날짜</th><th>규칙</th><th>수익건</th><th>손실건</th><th>평균 순수익률</th></tr></thead><tbody id="daily"></tbody></table></div></section>
<section><h2>상세 기록</h2><a class="link" href="/api/csv" target="_blank">CSV 내려받기</a><p class="sub" id="count"></p><div class="scroll"><table><thead><tr><th>종목</th><th>규칙</th><th>신호시각(KST)</th><th>OI 1h</th><th>비율 1h</th><th>매수/매도</th><th>30m 순</th><th>1h 순</th><th>3h 순</th></tr></thead><tbody id="trades"></tbody></table></div></section>
<section><h2>데이터 오류 / 누락</h2><pre id="errors"></pre></section></div>
<section class="sub"><b>데이터 해석 주의</b><p>① 롱/숏 비율 상승은 신규 롱 매수의 직접 증거가 아닙니다. ② Binance OI·상위 비율 시각은 구간 종료이고 taker량 시각은 구간 시작이므로 맞춰서 비교합니다. ③ 신호 종료 후 다음 빗썸 5분봉 종가를 가상 진입가로 설정합니다. ④ 모든 기간은 최근 30일 이하만 사용하고 완전히 끝난 캔들만 계산합니다. ⑤ 일부 선물 API는 API 키가 필요할 수 있으며 접근 거절 시 해당 항목을 누락으로 표시합니다. 신규상장 코인 연령은 자동 검증하지 않습니다.</p></section></main>
<script>
const $=x=>document.getElementById(x);let timer=null;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function start(){
 let coins=$('coins').value.trim(),days=Number($('days').value);
 $('go').disabled=true;$('state').textContent='요청 접수';$('result').hidden=true;
 try{let r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({coins,days})});let j=await r.json();if(!r.ok)throw Error(j.error||'요청 실패');poll();if(timer)clearInterval(timer);timer=setInterval(poll,1400)}
 catch(e){$('state').textContent='실행 오류';$('progress').textContent=e.message;$('go').disabled=false}
}
async function poll(){try{let r=await fetch('/api/status');let j=await r.json();$('state').textContent=j.state==='running'?'수집 중':j.state==='done'?'연구 완료':j.state==='error'?'오류': '실행 전';$('progress').textContent=(j.progress||'') + (j.pct?' ('+Math.round(j.pct)+'%)':''); if(j.state!=='running'){clearInterval(timer);$('go').disabled=false;if(j.state==='done')show(j.result);if(j.state==='error')$('progress').textContent=j.error||'오류';}}catch(e){clearInterval(timer);$('go').disabled=false;$('progress').textContent='상태 조회 실패: '+e.message}}
function show(j){$('result').hidden=false;
 $('scope').innerHTML=[['기간',j.days+'일'],['공통 종목',j.mapped+'개'],['총 신호',j.trades.length+'건'],['롱/숏 비율',j.ratio_available?'수집 확인':'미확인/누락'],['기간 시작',j.since_kst],['기간 끝',j.until_kst]].map(x=>'<div class="stat"><small>'+esc(x[0])+'</small><strong>'+esc(x[1])+'</strong></div>').join('');
 $('notice').textContent='분석 종목: '+(j.mapped_coins||[]).join(', ')+' / 제외: '+(j.skipped||[]).join(', ');
 const fmt=v=>v===null?'—':Number(v).toFixed(2)+'%';
 $('summary').innerHTML=j.summary.map(x=>'<tr><td>'+esc(x.rule+' '+x.label)+'</td><td>'+x.n+'</td><td>'+fmt(x.avg30)+'</td><td>'+fmt(x.win30)+'</td><td>'+fmt(x.avg60)+'</td><td>'+fmt(x.win60)+'</td><td>'+fmt(x.avg180)+'</td><td>'+fmt(x.win180)+'</td></tr>').join('');
 $('daily').innerHTML=(j.daily||[]).map(x=>'<tr><td>'+esc(x.day)+'</td><td>'+esc(x.rule)+'</td><td>'+x.wins+'</td><td>'+x.losses+'</td><td>'+fmt(x.mean_net60)+'</td></tr>').join('');
 $('count').textContent='총 '+j.trades.length+'건 · 최근 신호부터 최대 150개 화면 표시, 전체는 CSV에서 확인';
 $('trades').innerHTML=j.trades.slice().sort((a,b)=>b.signal_ms-a.signal_ms).slice(0,150).map(x=>'<tr><td>'+esc(x.coin)+'</td><td>'+esc(x.rule)+'</td><td>'+esc(new Date(x.signal_ms).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}))+'</td><td>'+Number(x.oi).toFixed(2)+'%</td><td>'+(x.ratio===null?'—':Number(x.ratio).toFixed(2)+'%')+'</td><td>'+Number(x.taker).toFixed(2)+'</td><td>'+fmt(x.net30)+'</td><td>'+fmt(x.net60)+'</td><td>'+fmt(x.net180)+'</td></tr>').join('');
 $('errors').textContent=[...(j.warnings||[]),...(j.errors||[])].join('\n')||'없음';
}
poll();
</script></body></html>'''

JOB = {"state":"idle","progress":"", "pct":0,"result":None,"error":None}
JOB_LOCK = threading.Lock()


def job_worker(coins, days):
    def on_progress(msg,pct):
        with JOB_LOCK: JOB.update(progress=msg,pct=pct)
    try:
        result = research(coins,days,on_progress)
        with JOB_LOCK: JOB.update(state="done",result=result,pct=100,progress="분석 및 CSV 생성 완료")
    except Exception as e:
        with JOB_LOCK: JOB.update(state="error",error=str(e),pct=0)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Never log possible API credentials (only fixed-path GET/POST endpoints).
        print("HTTP:",fmt % args,flush=True)

    def respond(self, body, status=200, ctype="application/json; charset=utf-8",extra=None):
        if isinstance(body,str): body=body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type',ctype)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        for k,v in (extra or {}).items():self.send_header(k,v)
        self.end_headers();self.wfile.write(body)

    def do_GET(self):
        p = urlsplit(self.path).path
        if p in ('/','/health'):
            self.respond(HTML if p=='/' else 'ok',ctype='text/html; charset=utf-8' if p=='/' else 'text/plain')
        elif p=='/api/status':
            with JOB_LOCK: data=dict(JOB)
            self.respond(json.dumps(data,ensure_ascii=False))
        elif p=='/api/csv':
            with JOB_LOCK: result=JOB.get('result')
            if not result: return self.respond(json.dumps({'error':'완료된 연구가 없습니다'}),404)
            data = '\ufeff' + csv_text(result['trades'])
            self.respond(data,ctype='text/csv; charset=utf-8',extra={'Content-Disposition':'attachment; filename="BITHUMB_FUTURES_LEAD_V1_1.csv"'})
        else: self.respond(json.dumps({'error':'not found'}),404)

    def do_POST(self):
        if urlsplit(self.path).path != '/api/run':
            return self.respond(json.dumps({'error':'not found'}),404)
        try:
            length = int(self.headers.get('Content-Length','0'))
            if length <= 0 or length>4096: raise ValueError('잘못된 요청 크기')
            obj = json.loads(self.rfile.read(length))
            coins = coins_parse(obj.get('coins',DEFAULT_COINS))
            days = int(obj.get('days',6))
            if days < 1 or days > MAX_DAYS: raise ValueError('연구 기간 1~13일만 가능')
        except (ValueError,KeyError,TypeError,json.JSONDecodeError) as e:
            return self.respond(json.dumps({'error':str(e)},ensure_ascii=False),400)
        with JOB_LOCK:
            if JOB['state']=='running':
                return self.respond(json.dumps({'error':'연구가 이미 실행 중입니다'},ensure_ascii=False),409)
            JOB.update(state='running',progress='연구 시작',pct=0,result=None,error=None)
        threading.Thread(target=job_worker,args=(coins,days),daemon=True).start()
        self.respond(json.dumps({'ok':True}))


def main():
    p=argparse.ArgumentParser(description='Bithumb × Binance 선물 선행성 연구 V1.1 (읽기 전용)')
    p.add_argument('--web',action='store_true',help='모바일 대시보드 서버 시작')
    p.add_argument('--selftest',action='store_true',help='거래소 호출 없는 검증')
    p.add_argument('--coins',default=DEFAULT_COINS)
    p.add_argument('--days',type=int,default=6)
    p.add_argument('--port',type=int,default=int(os.environ.get('PORT',10000)))
    p.add_argument('--csv',default='BITHUMB_FUTURES_LEAD_V1_1.csv')
    args=p.parse_args()
    if args.selftest: return selftest()
    if args.web:
        httpd = ThreadingHTTPServer(('0.0.0.0',args.port),Handler)
        print(f'RESEARCH V{VERSION} web: http://127.0.0.1:{args.port} (READ-ONLY)',flush=True)
        httpd.serve_forever()
        return
    try:
        result = research(args.coins,args.days,lambda msg,pct:print(f'[{pct:.0f}%] {msg}',flush=True))
        for r in result['summary']:
            print(f"{r['rule']} {r['label']}: N={r['n']}, 30분 평균={r['avg30']}, 60분 평균={r['avg60']}, 180분 평균={r['avg180']}")
        if result['warnings'] or result['errors']:
            print('\n주의/누락:','; '.join(result['warnings']+result['errors']))
        with open(args.csv,'w',encoding='utf-8-sig',newline='') as f:f.write(csv_text(result['trades']))
        print('CSV:',args.csv)
    except (APIError, ValueError) as e:
        print('실행 불가:',e,flush=True)
        raise SystemExit(1)

if __name__ == '__main__': main()