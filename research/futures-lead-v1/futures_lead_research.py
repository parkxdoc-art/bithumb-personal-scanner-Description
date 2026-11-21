#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BITHUMB × Binance futures lead-lag research V1.4.1 (READ-ONLY).

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import hyperliquid_wallet_study_v15 as walletstudy
import classic_priceaction_lab as classiclab
import brooks_confirm_lab_V1_1 as brookslab

VERSION = "1.4.1"
BINANCE = "https://fapi.binance.com"
BITHUMB = "https://api.bithumb.com"
PERIOD_MS = 15 * 60_000
CANDLE_MS = 5 * 60_000
HOURS_MS = 60 * 60_000
KST = timezone(timedelta(hours=9))
MAX_DAYS = 13
MAX_COINS = 20
DEFAULT_COINS = "BTC,ETH,XRP,SOL,SUI"
STABLES = {"USDT","USDC","DAI","FDUSD","TUSD","USDE","USD1","USDD","PYUSD","RLUSD","EURC","EURI"}
MULTIPLIED = {"PEPE":"1000PEPE", "SHIB":"1000SHIB", "BONK":"1000BONK", "FLOKI":"1000FLOKI"}
RULES = {
    "G": "주력: 선물 선행 + BTC 비하락",
    "N": "비교 전용: 같은 코인·날짜·BTC 상승 가격횡보",
    "X": "비교 전용: 선물 조건 같고 BTC 하락",
}
FIELDS = ["코인","규칙","신호시각(KST)","진입시각(KST)","진입가(KRW)",
          "1시간 OI 변화(%)","최근1시간 매수/매도량비","이전1시간 빗썸 상승률(%)",
          "BTC 1시간 상승률(%)","15분 순수익률(%)","30분 순수익률(%)","60분 순수익률(%)","180분 순수익률(%)",
          "15분 최고 종가상승(%)","30분 최고 종가상승(%)","60분 최고 종가상승(%)",
          "60분내 순+0.5% 종가달성", "60분 먼저 도달", "첫 도달시각(KST)",
          "첫 도달 순수익률(%)", "60분 목표/손절 모의청산 순수익률(%)",
          "진입 후 첫5분 가격변화(%)", "첫5분 약세(-0.3% 이하)",
          "30분 이전 저점 구조손절가(KRW)","구조손절폭(가격%)", "구조손절 적용상태",
          "구조손절 먼저 도달", "구조손절 1시간 순수익률(%)",
          "5분 확인진입 적용", "확인진입 1시간 모의청산 순수익률(%)"]

class APIError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# Observe Binance 418 restriction of Render's outbound IP without bypassing it.
# Seed the observed ban only on Render, not for local research installations.
OBSERVED_RENDER_BAN_UNTIL_MS = 1795467298613
_IS_RENDER = bool(os.environ.get('RENDER_SERVICE_ID') or os.environ.get('RENDER'))
_BAN_LOCK = threading.RLock()
_BINANCE_BAN_UNTIL_MS = OBSERVED_RENDER_BAN_UNTIL_MS if _IS_RENDER else 0
_BINANCE_NEXT_REQUEST_MONO = 0.0
_BITHUMB_NEXT_REQUEST_MONO = 0.0
BINANCE_MIN_GAP_SECONDS = 2.0
BITHUMB_MIN_GAP_SECONDS = 0.25


def ban_until_ms():
    with _BAN_LOCK:
        return _BINANCE_BAN_UNTIL_MS if int(time.time()*1000) < _BINANCE_BAN_UNTIL_MS else 0


def ban_message(until_ms):
    stamp = datetime.fromtimestamp(until_ms/1000, KST).strftime('%Y-%m-%d %H:%M:%S KST')
    return (f'바이낸스 API 요청 중단: {stamp}까지 IP 사용 제한. '
            '더 실행해도 해제되지 않습니다. 차단 해제 후 제한된 속도로 다시 수집하세요.')


def request_gate(base):
    """Pace requests globally per server process; never request during ban."""
    global _BINANCE_NEXT_REQUEST_MONO, _BITHUMB_NEXT_REQUEST_MONO
    with _BAN_LOCK:
        until = _BINANCE_BAN_UNTIL_MS if base == BINANCE else 0
        if until and int(time.time()*1000) < until:
            raise APIError(ban_message(until), 418)
        next_at = _BINANCE_NEXT_REQUEST_MONO if base == BINANCE else _BITHUMB_NEXT_REQUEST_MONO
        remaining = next_at - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        after = time.monotonic()
        if base == BINANCE:
            _BINANCE_NEXT_REQUEST_MONO = after + BINANCE_MIN_GAP_SECONDS
        else:
            _BITHUMB_NEXT_REQUEST_MONO = after + BITHUMB_MIN_GAP_SECONDS


def register_limit_error(status, body, header_retry_after=None):
    """Honor 429 cooldown or 418 ban; never retry either status immediately."""
    global _BINANCE_BAN_UNTIL_MS
    now_ms = int(time.time()*1000)
    if status == 418:
        match = re.search(r'\buntil\s+(\d{13})\b', body)
        until = int(match.group(1)) if match else 0
        if until <= now_ms:
            until = now_ms + 24*HOURS_MS
    else:
        try:
            retry_sec = max(0.0, float(header_retry_after))
        except (TypeError, ValueError):
            retry_sec = 300.0
        until = now_ms + int(max(300.0, min(retry_sec, 3*24*3600))*1000)
    with _BAN_LOCK:
        _BINANCE_BAN_UNTIL_MS = max(_BINANCE_BAN_UNTIL_MS, until)
        until = _BINANCE_BAN_UNTIL_MS
    return ban_message(until)


def fetch_json(base, path, params=None, headers=None):
    """Allowlisted hosts and paths only. Retry transient server/rate-limit failures."""
    if base not in (BINANCE, BITHUMB) or not path.startswith("/") or '//' in path:
        raise APIError("허용되지 않은 API 경로")
    url = base + path + ("?" + urlencode(params) if params else "")
    req = Request(url, headers={"User-Agent": "BithumbBinanceReadOnlyResearch/1.4.1",
                                "Accept":"application/json", **(headers or {})})
    for attempt in range(3):
        request_gate(base)
        try:
            with urlopen(req, timeout=14) as r:
                return json.loads(r.read(8_000_000).decode("utf-8"))
        except HTTPError as e:
            content = e.read(500).decode("utf-8", "replace")
            if base == BINANCE and e.code in (418, 429):
                message = register_limit_error(e.code, content,
                    e.headers.get('Retry-After') if e.headers else None)
                raise APIError(message, e.code) from e
            if e.code in (500, 502, 503, 504) and attempt < 2:
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


def bih_candles(coin, start_ms, end_ms, with_lows=False):
    """Fetch 5m closed Bithumb candles, paged by 'to' interpreted as KST."""
    result = {}
    lows = {}
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
                if with_lows and x.get("low_price") is not None:
                    lows[t] = float(x["low_price"])
        if oldest is None or oldest >= cursor or oldest <= start_ms or len(data) < 200:
            break
        cursor = oldest  # 'to' excludes candle opening at this time
    return (result, lows) if with_lows else result


def percent_change(curr, prior):
    return (curr/prior - 1)*100 if prior and prior > 0 and curr is not None else None


def row_features(t, idx, times, oi, taker, prices):
    """Only use fully closed intervals and previously available price candles."""
    if idx < 4: return None
    lookback = times[idx-4]
    # Must have an actual one-hour span; never bridge missing OI observations.
    if t - lookback != HOURS_MS: return None
    o0, o1 = oi.get(lookback), oi.get(t)
    if not o0 or not o1: return None
    oi_pct = percent_change(float(o1["sumOpenInterest"]),float(o0["sumOpenInterest"]))
    if oi_pct is None: return None
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
    return {"oi":oi_pct,"taker":buy/sell,"momentum":momentum_pct}



# V1.4.1 screens ONE candidate class (G). N and X are retrospective controls only.
# Freeze V1.2 thresholds for out-of-sample comparisons; do not optimize to 6-day CSV.
def early_candidate(f):
    return bool(f and -0.5 <= f["momentum"] <= 0.3
                and f["oi"] >= 0.8 and f["taker"] >= 1.08)


def price_flat(f):
    return bool(f and -0.5 <= f["momentum"] <= 0.3)


def btc_momentum(t, btc_prices):
    if not btc_prices:
        return None
    return percent_change(btc_prices.get(t-CANDLE_MS),
                          btc_prices.get(t-HOURS_MS-CANDLE_MS))


def outcomes_for_entry(entry_bar, entry_price, prices, fee_pct=0.5, stop_price=None):
    """Evaluate post-entry fully closed 5m CLOSING prices only.

    Profit net +0.5% requires a gross +1.0% close at fee=0.5%.
    Stop is gross -1.0% (roughly net -1.5%). Close-based fills only:
    NOT a limit order, intrabar high/low, or guaranteed executable profit.
    """
    close_prices = [prices.get(entry_bar+i*CANDLE_MS) for i in range(1,37)]
    if any(c is None or c <= 0 for c in close_prices):
        return None
    result = {"first5_gross": (close_prices[0] / entry_price-1)*100}
    for m in (15,30,60,180):
        result[f"net{m}"] = (close_prices[m//5-1]/entry_price-1)*100-fee_pct
    for m in (15,30,60):
        result[f"peak{m}"] = (max(close_prices[:m//5])/entry_price-1)*100
    tp_gross = 0.5 + fee_pct  # 0.5% desired NET; 1.0% gross at fee 0.5.
    stop_gross = -1.0
    first = None
    for i,c in enumerate(close_prices[:12],start=1):
        gross=(c/entry_price-1)*100
        if gross >= tp_gross:
            first=("익절",i,gross-fee_pct)
            break
        if gross <= stop_gross:
            first=("손절",i,gross-fee_pct)
            break
    if first is None:
        first=("시간만료",12,result["net60"])
    result.update({"first_event":first[0],
                   "first_event_ms":entry_bar+(first[1]+1)*CANDLE_MS,
                   "first_event_net":first[2],
                   "strategy_net60":first[2],
                   "hit60": result["peak60"] >= tp_gross})
    result['first5_weak'] = result['first5_gross'] <= -0.3  # diagnostic, not a pre-entry filter
    if stop_price is not None:
        # Same price targets, 5m completed-close logic and 60m horizon as fixed-stop.
        sf = next(((('익절' if (c/entry_price-1)*100 >= tp_gross else '손절'),i,
                     (c/entry_price-1)*100-fee_pct)
                    for i,c in enumerate(close_prices[:12],start=1)
                    if (c/entry_price-1)*100 >= tp_gross or c <= stop_price), None)
        if sf is None:
            sf = ('시간만료',12,result['net60'])
        result['struct_event'] = sf[0]
        result['struct_net60'] = sf[2]
    else:
        result['struct_event'] = None
        result['struct_net60'] = None
    return result


def structural_stop(t, entry_price, lows):
    """Six previously closed 5m candles, excluding signal/entry candle.

    Use prior 30-minute LOW (not close), fixed buffer -0.15%. No future data.
    Report missing/too-close/too-wide explicitly instead of cherry-picking rows.
    """
    if lows is None:
        return (None,None,'저가 데이터 없음')
    prev = [lows.get(t-i*CANDLE_MS) for i in range(1,7)]
    if any(x is None or x <= 0 for x in prev):
        return (None,None,'30분 저가 누락')
    stop = min(prev) * 0.9985
    gap = (1-stop/entry_price)*100
    if gap <= 0:
        return (round(stop,9),round(gap,4),'손절선 역전')
    if gap < 0.30:
        return (round(stop,9),round(gap,4),'너무 가까움(<0.30%)')
    if gap > 3.0:
        return (round(stop,9),round(gap,4),'너무 넓음(>3.0%)')
    return (round(stop,9),round(gap,4),'비교 가능')


def make_row(coin, rule, t, features, btc1h, prices, fee_pct=0.5, lows=None):
    entry_price=prices.get(t)  # first 5m candle closes at t+5m
    if entry_price is None or entry_price<=0:
        return None
    stop, gap, status = structural_stop(t, entry_price, lows)
    outcome=outcomes_for_entry(t,entry_price,prices,fee_pct,
                               stop_price=stop if status=='비교 가능' else None)
    if outcome is None:
        return None
    # Confirmation signal is the NEXT 5m completed candle's CLOSE (t+10m).
    # Only after confirmation can the following completed candle's close be
    # used as entry (t+15m). This is a separate strategy on a selected subset.
    confirmation = prices.get(t+CANDLE_MS)
    confirm_eligible = confirmation is not None and confirmation > entry_price
    confirm_net = None
    if confirm_eligible and prices.get(t+2*CANDLE_MS):
        confirm_entry = prices[t+2*CANDLE_MS]
        confirm_out = outcomes_for_entry(t+2*CANDLE_MS,confirm_entry,prices,fee_pct)
        if confirm_out is not None:
            confirm_net = confirm_out['strategy_net60']
    return {"coin":coin,"rule":rule,"signal_ms":t,"entry_ms":t+CANDLE_MS,
            "entry_price":entry_price,"btc1h":btc1h,**features,**outcome,
            "stop_price":stop,"stop_gap":gap,"struct_status":status,
            "confirm_ok":confirm_net is not None,"confirm_net60":confirm_net}


def select_controls(g_rows, q_rows):
    """Post-hoc reference, not a trading signal: nearest same coin/KST day Q.
    A Q candidate must be price-flat and BTC nonnegative, but must NOT meet
    G's futures conditions. Avoid using the same control twice. Within 3h
    of G, use only one matched Q. If none exists, G remains but is unpaired.
    """
    chosen=[]
    seen=set()
    for g in sorted(g_rows,key=lambda x:(x['coin'],x['signal_ms'])):
        day=datetime.fromtimestamp(g['signal_ms']/1000,KST).date()
        options=[x for x in q_rows if x['coin']==g['coin']
                 and datetime.fromtimestamp(x['signal_ms']/1000,KST).date()==day
                 and abs(x['signal_ms']-g['signal_ms'])<=3*HOURS_MS
                 and (x['coin'],x['signal_ms']) not in seen]
        if not options:
            continue
        best=min(options,key=lambda x:(abs(x['signal_ms']-g['signal_ms']),x['signal_ms']))
        seen.add((best['coin'],best['signal_ms']))
        chosen.append(best)
    return chosen


def produce_trades(coin, oi, taker, prices, since_ms, until_ms, fee_pct=0.5, btc_prices=None, lows=None):
    """Only G is an active candidate; N/X are internal diagnostics.

    Align each signal to t (complete Binance 15m). Entry uses the first 5m
    candle closing AFTER t; all outcomes are from later fully completed bars.
    225m after t required for every signal to avoid horizon censoring.
    """
    times=sorted(x for x in oi if x<=until_ms)
    last_used={"G":-10**18,"X":-10**18}
    g_rows=[];x_rows=[];q_pool=[]
    for idx,t in enumerate(times):
        if not (since_ms<=t<=until_ms-225*60_000):
            continue
        f=row_features(t,idx,times,oi,taker,prices)
        if not price_flat(f):
            continue
        btc=btc_momentum(t,btc_prices)
        if btc is None:
            continue  # do NOT silently treat missing BTC as positive
        early=early_candidate(f)
        rule="G" if early and btc>=0 else "X" if early else "N" if btc>=0 else None
        if rule is None:
            continue
        if rule in ("G","X") and t-last_used[rule]<180*60_000:
            continue
        row=make_row(coin,rule,t,f,btc,prices,fee_pct,lows=lows)
        if row is None:
            continue
        if rule=="G": g_rows.append(row);last_used[rule]=t
        elif rule=="X": x_rows.append(row);last_used[rule]=t
        else: q_pool.append(row)
    return sorted(g_rows+select_controls(g_rows,q_pool)+x_rows,
                  key=lambda x:(x['signal_ms'],x['rule']))


def describe(trades):
    result=[]
    for rule,label in RULES.items():
        group=[r for r in trades if r['rule']==rule]
        item={"rule":rule,"label":label,"n":len(group),
              "coins":len({r['coin'] for r in group})}
        for m in (15,30,60,180):
            vals=[r[f'net{m}'] for r in group]
            item[f'avg{m}']=round(statistics.mean(vals),3) if vals else None
            item[f'win{m}']=round(100*sum(x>0 for x in vals)/len(vals),1) if vals else None
        item['hit60']=round(100*sum(r['hit60'] for r in group)/len(group),1) if group else None
        item['target_first']=sum(r['first_event']=='익절' for r in group)
        item['stop_first']=sum(r['first_event']=='손절' for r in group)
        item['timeout']=sum(r['first_event']=='시간만료' for r in group)
        item['strategy_avg60']=round(statistics.mean(r['strategy_net60'] for r in group),3) if group else None
        structural=[r for r in group if r.get('struct_status')=='비교 가능' and r.get('struct_net60') is not None]
        item['struct_n']=len(structural)
        item['struct_avg60']=round(statistics.mean(r['struct_net60'] for r in structural),3) if structural else None
        item['fixed_paired_avg60']=round(statistics.mean(r['strategy_net60'] for r in structural),3) if structural else None
        item['struct_excluded']=len(group)-len(structural)
        item['struct_wins']=sum(r['struct_net60']>r['strategy_net60'] for r in structural)
        item['weak5_n']=sum(r['first5_weak'] for r in group)
        item['weak5_fixed_avg']=round(statistics.mean(r['strategy_net60'] for r in group if r['first5_weak']),3) if any(r['first5_weak'] for r in group) else None
        item['strong5_fixed_avg']=round(statistics.mean(r['strategy_net60'] for r in group if not r['first5_weak']),3) if any(not r['first5_weak'] for r in group) else None
        confirmed=[r['confirm_net60'] for r in group if r.get('confirm_ok')]
        item['confirm_n']=len(confirmed)
        item['confirm_avg60']=round(statistics.mean(confirmed),3) if confirmed else None
        item['confirm_fixed_paired_avg60']=round(statistics.mean(r['strategy_net60'] for r in group if r.get('confirm_ok')),3) if confirmed else None
        result.append(item)
    return result


def describe_daily(trades):
    daily={}
    for r in trades:
        if r['rule'] != 'G':
            continue
        day=datetime.fromtimestamp(r['signal_ms']/1000,KST).strftime('%Y-%m-%d')
        daily.setdefault(day,[]).append(r['strategy_net60'])
    return [{"day":day,"n":len(values),"wins":sum(x>0 for x in values),
             "losses":sum(x<=0 for x in values),
             "mean":round(statistics.mean(values),3)} for day,values in sorted(daily.items())]


def csv_text(trades):
    b=io.StringIO();w=csv.writer(b);w.writerow(FIELDS)
    for r in sorted(trades,key=lambda x:(x['signal_ms'],x['coin'],x['rule'])):
        w.writerow([r['coin'],r['rule'],timestamp_fmt(r['signal_ms']),timestamp_fmt(r['entry_ms']),
                    r['entry_price'],round(r['oi'],3),round(r['taker'],3),round(r['momentum'],3),
                    round(r['btc1h'],3),
                    *[round(r[f'net{m}'],3) for m in (15,30,60,180)],
                    *[round(r[f'peak{m}'],3) for m in (15,30,60)],
                    'Y' if r['hit60'] else 'N',r['first_event'],
                    timestamp_fmt(r['first_event_ms']),round(r['first_event_net'],3),
                    round(r['strategy_net60'],3),
                    round(r['first5_gross'],3),'Y' if r['first5_weak'] else 'N',
                    '' if r['stop_price'] is None else r['stop_price'],
                    '' if r['stop_gap'] is None else r['stop_gap'],r['struct_status'],
                    r['struct_event'] or '',
                    '' if r['struct_net60'] is None else round(r['struct_net60'],3),
                    'Y' if r['confirm_ok'] else 'N',
                    '' if r['confirm_net60'] is None else round(r['confirm_net60'],3)])
    return b.getvalue()


def research(coins,days,on_progress=None,now_ms=None):
    if now_ms is None and ban_until_ms():
        raise APIError(ban_message(ban_until_ms()),418)
    if not 1<=days<=MAX_DAYS:
        raise ValueError(f'기간은 1~{MAX_DAYS}일만 가능')
    now_ms=now_ms or int(time.time()*1000)
    end_ms=(now_ms//PERIOD_MS)*PERIOD_MS
    since_ms=end_ms-days*24*HOURS_MS
    fetch_since=since_ms-2*HOURS_MS
    coins=coins_parse(coins) if isinstance(coins,str) else coins
    progress=on_progress or (lambda message,pct:None)
    progress('공통 종목 확인',3)
    bithumb=bih_markets()
    futures=bin_symbols()
    mapped=[];skip=[]
    for c in coins:
        symbol=futures.get(c) or futures.get(MULTIPLIED.get(c,''))
        if c in STABLES or c not in bithumb or not symbol:
            skip.append(c)
        else:
            mapped.append((c,symbol))
    if not mapped:
        raise APIError('빗썸 KRW+바이낸스 무기한 선물 공통 종목이 없습니다')
    progress('BTC 완료 5분봉 수집',4)
    # BTC history is required for ALL V1.4.1 comparisons; never use proxy.
    btc_prices, btc_lows=bih_candles('BTC',fetch_since-HOURS_MS,end_ms,with_lows=True)
    if not btc_prices:
        raise APIError('BTC 5분봉 수집 실패: 비교 연구 불가')
    rows=[];errors=[];warnings=[]
    def one_coin(coin,symbol):
        try:
            oi=history('/futures/data/openInterestHist',symbol,fetch_since,end_ms)
            taker=history('/futures/data/takerlongshortRatio',symbol,fetch_since,end_ms)
            if coin=='BTC':
                prices,lows=btc_prices,btc_lows
            else:
                prices,lows=bih_candles(coin,fetch_since-HOURS_MS,end_ms,with_lows=True)
            if not oi or not taker or not prices:
                return [],f'{coin}: 데이터 부족 (OI={len(oi)}, taker={len(taker)}, KRW={len(prices)})',None
            matched=produce_trades(coin,oi,taker,prices,since_ms,end_ms,
                                   btc_prices=btc_prices,lows=lows)
            warn=None if any(x['rule']=='G' for x in matched) else f'{coin}: G형 완료 신호 0건'
            return matched,None,warn
        except APIError as exc:
            return [],f'{coin}: {exc}',None
        except (KeyError,ValueError,TypeError,ZeroDivisionError) as exc:
            return [],f'{coin}: 데이터 값 오류: {str(exc)[:130]}',None
    # 1 worker ensures no Binance parallel API requests on a shared-IP server.
    # Aggregate in one thread to avoid races in diagnostics and CSV.
    with ThreadPoolExecutor(max_workers=1) as pool:
        fs={pool.submit(one_coin,coin,symbol):coin for coin,symbol in mapped}
        for i,future in enumerate(as_completed(fs),1):
            coin=fs[future]
            try:
                matched,error,warn=future.result()
                rows.extend(matched)
                if error: errors.append(error)
                if warn: warnings.append(warn)
            except Exception as exc:
                errors.append(f'{coin}: 처리 실패 {str(exc)[:130]}')
            progress(f'데이터 수집 {i}/{len(mapped)} 완료',5+87*i/len(mapped))
    rows.sort(key=lambda x:(x['signal_ms'],x['coin'],x['rule']))
    progress('G 및 동일 시기 비교군 집계',97)
    return {"version":VERSION,"days":days,"since_kst":timestamp_fmt(since_ms),
            "until_kst":timestamp_fmt(end_ms),"requested":len(coins),"mapped":len(mapped),
            "mapped_coins":[c for c,_ in mapped],"skipped":skip,"errors":errors,
            "warnings":warnings,"btc_gate_available":True,"trades":rows,
            "summary":describe(rows),"daily":describe_daily(rows),
            "g_count":sum(r['rule']=='G' for r in rows),
            "matched_count":sum(r['rule']=='N' for r in rows),
            "btc_down_count":sum(r['rule']=='X' for r in rows),
            "completed_at":timestamp_fmt(int(time.time()*1000))}


def selftest():
    t=20000*PERIOD_MS
    prices={t+i*CANDLE_MS:100 for i in range(-400,2000)}
    prices[t+2*CANDLE_MS]=101.1
    out=outcomes_for_entry(t,100,prices)
    assert out['first_event']=='익절' and abs(out['strategy_net60']-0.6)<1e-6
    assert out['first_event_ms']==t+3*CANDLE_MS
    assert out['hit60'] is True
    prices[t+2*CANDLE_MS]=100
    prices[t+1*CANDLE_MS]=98.9
    out=outcomes_for_entry(t,100,prices)
    assert out['first_event']=='손절' and out['strategy_net60']< -1.5
    f={'oi':0.9,'taker':1.1,'momentum':0.1}
    assert early_candidate(f) and not early_candidate(dict(f,momentum=0.6))
    assert rounded_end(t+20)==t and taker_end(t-PERIOD_MS)==t
    print('SELFTEST PASS V1.4.1: fees, close-first, no-missing-silent, early gate, timestamps')

HTML = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>선물 데이터 선행성 연구 V1.4.1</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}body{font-family:system-ui,-apple-system,sans-serif;background:#0b1321;color:#eaf1fb;margin:0}main{max-width:980px;margin:auto;padding:14px 12px 55px}h1{font-size:24px;margin:6px 0}h2{font-size:17px;margin:12px 0}section{background:#162237;border:1px solid #2d435b;border-radius:14px;padding:15px;margin:14px 0}p,.note{color:#aebfd0;line-height:1.6;font-size:13px}.status{border:1px solid #537089;border-radius:8px;padding:10px}.top{font-size:12px;color:#79bec6}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.stat{border:1px solid #364e65;border-radius:8px;padding:9px}.stat small{display:block;color:#aab8c8}.stat strong{display:block;font-size:19px;margin-top:5px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;min-width:690px;font-size:12px}td,th{border-bottom:1px solid #36475c;padding:10px 7px;text-align:right}td:first-child,th:first-child{text-align:left}th{color:#b5c6dc;font-weight:600}input{width:100%;background:#0d182a;border:1px solid #475c76;color:white;border-radius:8px;padding:12px;margin:4px 0 12px;font-size:16px}button,.link{background:#287fd5;color:#fff;border:0;border-radius:9px;padding:12px 15px;display:inline-block;text-decoration:none;font-size:14px;font-weight:600;cursor:pointer}button:disabled{opacity:.5}pre{white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.5;color:#b7c4d6}.good{color:#82d8bd}.bad{color:#ffb0a3}.sub{font-size:12px;color:#91a7bf}#result[hidden]{display:none}@media(max-width:620px){.stats{grid-template-columns:repeat(2,1fr)}table{font-size:12px}}</style></head><body><main>
<div class="top">BITHUMB × BINANCE · 독립 연구 · 자동매매 없음</div>
<h1>선물 데이터 선행성 연구 V1.4.1</h1>
<p><a href="/traders" style="color:#83d1ff">새 연구: 실제 공개 트레이더 체결 패턴 V1.5 보기 →</a></p>
<p>저성과 A/B/C/M/E/P 규칙을 주력 신호에서 제외하고, <b>G형만 연구 후보</b>로 남겼습니다. N(같은 BTC 상승 구간)과 X(BTC 하락 구간)는 <b>통계 비교용</b>이며 BUY 알림이 아닙니다.</p>
<section><p><b>안전장치:</b> 바이낸스 IP 차단이 감지되면 차단 해제 시각까지 요청하지 않습니다. 최초 확인된 Render 차단 종료 시각은 2026-11-24 05:54:58 KST입니다. 기본 5종목·6일부터 재검증하세요.</p></section>
<section><div class="sub">빗썸 KRW + 바이낸스 USDT 선물 공통 코인 (최대 20개)</div><input id="coins" value="BTC,ETH,XRP,SOL,SUI"><div class="sub">최근 연구 일수 (1~13)</div><input id="days" type="number" value="6" min="1" max="13"><button id="go" onclick="start()">연구 실행</button><p>기본 설정: 5개 코인, 최근 6일 (최대 20개·13일). IP 요청 제한 방지용 보수적 기본값. 거래소 자료는 실제 API에서 불러오며 수집 중 다소 시간이 걸릴 수 있습니다.</p></section>
<section><h2>진행 상태</h2><div id="state" class="status">실행 전</div><p id="progress">연구 실행을 누르면 수집을 시작합니다.</p></section>
<div id="result" hidden>
<section><h2>G형 연구 범위</h2><div id="scope" class="stats"></div><p id="notice"></p></section>
<section><h2>G형 vs 비교군 (순수익률, 왕복 0.5%)</h2><p>G = 지난 1시간 가격 -0.5~+0.3%, 선물 OI +0.8% 이상, taker 매수/매도비 1.08 이상, BTC 1시간 비하락. N은 G와 같은 코인·날짜의 3시간 이내 BTC 비하락 가격횡보 구간 중 G 조건에 해당하지 않는 1:1 근접 비교군. X는 같은 선물 조건이지만 BTC가 하락한 경우.</p><div class="scroll"><table><thead><tr><th>규칙</th><th>건수</th><th>15분</th><th>30분</th><th>1시간</th><th>3시간</th><th>+0.5%순 목표 먼저</th><th>-1% 총손절 먼저</th><th>1시간 모의청산 평균</th></tr></thead><tbody id="summary"></tbody></table></div></section>
<section><h2>G형 진입 후 5분·손절 비교 (동일 신호)</h2><p>구조 손절은 진입 전 30분간 완료된 5분봉 저가의 최솟값에 0.15% 버퍼를 적용합니다. 손절폭 0.30~3.00%만 비교 가능으로 표시하며, 제외 건수도 기록합니다. 5분 확인 진입은 다음 봉 상승 마감이 확인된 뒤 그 다음 봉 종가에 진입하는 별도 가정입니다.</p><div id="diagnostics" class="stats"></div></section>
<section><h2>G형 날짜별 기록 (1시간 고정손절 모의청산)</h2><div class="scroll"><table style="min-width:490px"><thead><tr><th>날짜(KST)</th><th>G건수</th><th>수익</th><th>손실</th><th>평균 순수익률</th></tr></thead><tbody id="daily"></tbody></table></div></section>
<section><h2>G형 및 비교 기록</h2><a class="link" href="/api/csv" target="_blank">CSV 내려받기</a><p id="count"></p><div class="scroll"><table><thead><tr><th>코인</th><th>규칙</th><th>신호(KST)</th><th>OI 1h</th><th>매수/매도</th><th>BTC1h</th><th>15분</th><th>30분</th><th>1시간</th><th>목표/손절</th><th>모의 순수익</th></tr></thead><tbody id="trades"></tbody></table></div></section>
<section><h2>데이터 오류 / 누락</h2><pre id="errors"></pre></section></div>
<section><h2>계산 기준과 한계</h2><p>① 손절 도달·익절 판정은 <b>완성된 5분봉 종가</b>만 사용합니다. G 신호 후 다음 5분봉 종가를 가상 진입가로 놓고, 그 이후 15/30/60/180분 성과를 계산합니다. ② 순익 +0.5% 목표는 가격 +1.0%(왕복 수수료 0.5%)에 해당합니다. 손절은 가격 -1.0%이고 수수료 포함 시 약 -1.5%입니다. ③ 1시간 내 목표와 손절 중 <b>어느 조건을 먼저 만족한 완료봉 종가가 있었는지</b> 평가합니다. 해당 봉의 종가로 모의 청산한 것이며 실제 조건부 주문 체결을 뜻하지 않습니다. ④ N 비교군은 사후적으로 같은 코인·날짜·BTC 조건에서 가까운 사례를 선정합니다. G는 N이 없더라도 보존되므로 완전한 대응 실험은 아닙니다. ⑤ 구조손절에는 진입 전 완료봉의 <b>실제 저가</b>를 사용하지만 진입 이후 조건 충족은 여전히 <b>종가만</b> 사용하므로 실전 손절체결을 의미하지 않습니다. 구조손절 비적용 사례는 성과 평균에서 제외하되 건수를 공개합니다. ⑥ 5분 약세군은 진입 후 사후에 분류되므로 미리 알 수 있는 조건이 아니며, 확인진입 평균도 확인된 일부 신호에만 해당합니다. ⑦ 실제 주문·호가·슬리피지·미세 유동성·선물 펀딩비는 반영하지 않았고, 연구 결과는 실전 BUY 권고가 아닙니다.</p></section>
</main><script>
const $=id=>document.getElementById(id);let timer=null;
const esc=s=>String(s??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const pct=v=>v===null||v===undefined?'—':Number(v).toFixed(2)+'%';
async function start(){let coins=$('coins').value.trim(),days=Number($('days').value);$('go').disabled=true;$('state').textContent='접수 중';$('result').hidden=true;try{let r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({coins,days})});let j=await r.json();if(!r.ok)throw Error(j.error||'요청 실패');if(timer)clearInterval(timer);await poll();timer=setInterval(poll,1400)}catch(e){$('go').disabled=false;$('state').textContent='실행 오류';$('progress').textContent=e.message}}
async function poll(){try{let r=await fetch('/api/status');let j=await r.json();$('state').textContent=j.binance_blocked_until_ms?'바이낸스 IP 요청 중단':(({running:'수집 중',done:'연구 완료',error:'오류',idle:'실행 전'})[j.state]||'상태 조회');$('progress').textContent=j.binance_blocked_notice||((j.progress||'')+(j.pct?' ('+Math.round(j.pct)+'%)':''));$('go').disabled=!!j.binance_blocked_until_ms;if(j.binance_blocked_until_ms){setTimeout(poll,60000)}if(j.state!=='running'){clearInterval(timer);$('go').disabled=!!j.binance_blocked_until_ms;if(j.state==='done')show(j.result);if(j.state==='error'&&!j.binance_blocked_until_ms)$('progress').textContent=j.error||'오류';}}catch(e){clearInterval(timer);$('go').disabled=false;$('progress').textContent='상태 오류 '+e.message}}
function show(j){$('result').hidden=false;$('scope').innerHTML=[['기간',j.days+'일'],['공통 코인',j.mapped+'개'],['G형',j.g_count+'건'],['N 대응',j.matched_count+'건'],['BTC 하락 X',j.btc_down_count+'건'],['기간 끝',j.until_kst]].map(x=>'<div class="stat"><small>'+esc(x[0])+'</small><strong>'+esc(x[1])+'</strong></div>').join('');$('notice').textContent='분석 종목: '+(j.mapped_coins||[]).join(', ')+' / 제외: '+(j.skipped||[]).join(', ');const g=(j.summary||[]).find(x=>x.rule==='G');if(g){const di=[['G 후보',g.n+'건'],['초기 5분 약세(-0.3% 이하)',g.weak5_n+'건'],['초기 약세군 고정손절 평균',pct(g.weak5_fixed_avg)],['초기 비약세군 고정손절 평균',pct(g.strong5_fixed_avg)],['구조손절 비교 가능',g.struct_n+'건 / 제외 '+g.struct_excluded+'건'],['같은 신호 고정손절 평균',pct(g.fixed_paired_avg60)],['같은 신호 구조손절 평균',pct(g.struct_avg60)],['구조손절이 유리한 신호',g.struct_wins+'건'],['1봉 상승 확인 후 진입',g.confirm_n+'건'],['확인진입 평균 순수익',pct(g.confirm_avg60)],['동일 확인군 기존 진입 평균',pct(g.confirm_fixed_paired_avg60)]];$('diagnostics').innerHTML=di.map(x=>'<div class="stat"><small>'+esc(x[0])+'</small><strong>'+esc(x[1])+'</strong></div>').join('')}else{$('diagnostics').textContent='G형 신호 없음'};$('summary').innerHTML=(j.summary||[]).map(x=>'<tr><td>'+esc(x.rule+' '+x.label)+'</td><td>'+x.n+'</td><td>'+pct(x.avg15)+'</td><td>'+pct(x.avg30)+'</td><td>'+pct(x.avg60)+'</td><td>'+pct(x.avg180)+'</td><td>'+x.target_first+'</td><td>'+x.stop_first+'</td><td>'+pct(x.strategy_avg60)+'</td></tr>').join('');$('daily').innerHTML=(j.daily||[]).map(x=>'<tr><td>'+esc(x.day)+'</td><td>'+x.n+'</td><td>'+x.wins+'</td><td>'+x.losses+'</td><td>'+pct(x.mean)+'</td></tr>').join('');$('count').textContent='G 후보 '+j.g_count+'건 · N/X는 비교 전용 · 최근 최대 150개 표시, 전체 CSV 제공';$('trades').innerHTML=(j.trades||[]).slice().sort((a,b)=>b.signal_ms-a.signal_ms).slice(0,150).map(x=>'<tr><td>'+esc(x.coin)+'</td><td>'+esc(x.rule)+'</td><td>'+esc(new Date(x.signal_ms).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}))+'</td><td>'+pct(x.oi)+'</td><td>'+Number(x.taker).toFixed(2)+'</td><td>'+pct(x.btc1h)+'</td><td>'+pct(x.net15)+'</td><td>'+pct(x.net30)+'</td><td>'+pct(x.net60)+'</td><td>'+esc(x.first_event)+'</td><td>'+pct(x.strategy_net60)+'</td></tr>').join('');$('errors').textContent=[...(j.errors||[]),...(j.warnings||[])].join('\n')||'없음';}
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
        if p == '/classic/brooks' or p.startswith('/api/brooks-v11/'):
            return brookslab.handle_get(self,p)
        if p == '/classic' or p.startswith('/api/classic/'):
            return classiclab.handle_get(self,p)
        if p == '/traders/offline':
            try:
                with open(os.path.join(os.path.dirname(__file__), 'trader_offline_v16.html'), encoding='utf-8') as fp:
                    offline_page = fp.read()
            except OSError:
                return self.respond('오프라인 연구판 파일을 찾을 수 없습니다', status=500, ctype='text/plain; charset=utf-8')
            return self.respond(offline_page, ctype='text/html; charset=utf-8')
        if p == '/traders':
            return self.respond(walletstudy.HTML,ctype='text/html; charset=utf-8')
        if p == '/api/wallet-study/status':
            return self.respond(json.dumps(walletstudy.status(),ensure_ascii=False))
        if p == '/api/wallet-study/csv':
            result=walletstudy.status().get('result')
            if not result: return self.respond(json.dumps({'error':'아직 완료된 결과 없음'},ensure_ascii=False),404)
            return self.respond(walletstudy.to_csv(result),ctype='text/csv; charset=utf-8',
                                extra={'Content-Disposition':'attachment; filename="HYPERLIQUID_WALLET_STUDY_V1_5_1.csv"'})
        if p == '/api/wallet-study/diagnostics.csv':
            result=walletstudy.status().get('result')
            if not result: return self.respond(json.dumps({'error':'아직 완료된 결과 없음'},ensure_ascii=False),404)
            return self.respond(walletstudy.to_diagnostics_csv(result),ctype='text/csv; charset=utf-8',
                                extra={'Content-Disposition':'attachment; filename="HYPERLIQUID_WALLET_STUDY_V1_5_1_DIAGNOSTICS.csv"'})
        if p in ('/','/health'):
            self.respond(HTML if p=='/' else 'ok',ctype='text/html; charset=utf-8' if p=='/' else 'text/plain')
        elif p=='/api/status':
            with JOB_LOCK: data=dict(JOB)
            ban = ban_until_ms()
            data['binance_blocked_until_ms'] = ban
            data['binance_blocked_notice'] = ban_message(ban) if ban else ''
            self.respond(json.dumps(data,ensure_ascii=False))
        elif p=='/api/csv':
            with JOB_LOCK: result=JOB.get('result')
            if not result: return self.respond(json.dumps({'error':'완료된 연구가 없습니다'}),404)
            data = '\ufeff' + csv_text(result['trades'])
            self.respond(data,ctype='text/csv; charset=utf-8',extra={'Content-Disposition':'attachment; filename="BITHUMB_FUTURES_LEAD_V1_4_1.csv"'})
        else: self.respond(json.dumps({'error':'not found'}),404)

    def do_POST(self):
        if urlsplit(self.path).path == '/api/brooks-v11/run':
            return brookslab.handle_post(self,'/api/brooks-v11/run')
        if urlsplit(self.path).path == '/api/classic/run':
            return classiclab.handle_post(self,'/api/classic/run')
        if urlsplit(self.path).path == '/api/wallet-study/run':
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=512: raise ValueError('요청 크기 오류')
                obj=json.loads(self.rfile.read(length))
                days=int(obj.get('days',6))
                ok,reason=walletstudy.start(days)
            except (ValueError,TypeError,KeyError,json.JSONDecodeError) as e:
                return self.respond(json.dumps({'error':str(e)},ensure_ascii=False),400)
            return self.respond(json.dumps({'ok':ok,'error':reason},ensure_ascii=False),
                                200 if ok else 400 if '기간은' in reason else 429)
        if urlsplit(self.path).path != '/api/run':
            return self.respond(json.dumps({'error':'not found'}),404)
        if ban_until_ms():
            return self.respond(json.dumps({'error':ban_message(ban_until_ms())},ensure_ascii=False),429)
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
    p=argparse.ArgumentParser(description='Bithumb × Binance 선물 선행성 연구 V1.4.1 (읽기 전용)')
    p.add_argument('--web',action='store_true',help='모바일 대시보드 서버 시작')
    p.add_argument('--selftest',action='store_true',help='거래소 호출 없는 검증')
    p.add_argument('--coins',default=DEFAULT_COINS)
    p.add_argument('--days',type=int,default=6)
    p.add_argument('--port',type=int,default=int(os.environ.get('PORT',10000)))
    p.add_argument('--csv',default='BITHUMB_FUTURES_LEAD_V1_4_1.csv')
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
