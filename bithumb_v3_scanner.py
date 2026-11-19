"""BITHUMB V3.8 | 휴대폰(Pydroid 3) + Render 겸용 연구 스캐너.

필요: pip install requests
실행: python bithumb_v3_scanner.py  -> http://127.0.0.1:8080
Render: PORT 환경변수가 있으면 0.0.0.0:$PORT에 바인딩.
공개 빗썸 시세만 사용하며 인증/주문 기능은 절대 없습니다.

주의: 백테스트는 교육/검증용입니다. 역사적 '종목 선정'에 현재 24h 거래대금
순위를 사용하기 때문에 유니버스 선정 편향이 남습니다. 실전 수익 보장 없음.
"""
import csv
import json
import math
import os
import tempfile
import uuid
import zlib
import hashlib
from collections import deque
from pathlib import Path
import re
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

VERSION = "V3.8"
BASE = "https://api.bithumb.com/v1"
KST = timezone(timedelta(hours=9))
BUSY = threading.Lock()
MARKET_CACHE = {"at": 0, "markets": []}
MARKET_LOCK = threading.Lock()
VALID_TF = {5, 15, 60, 240}
PERIODS = {15, 30, 80, 180, 365}
MAX_BARS_PER_MARKET = 20000  # 장기 5분봉은 API/메모리 부담으로 미지원: 기간 거짓표시 방지
# V2의 4 worker 수집 구조 유지. 병렬 수를 늘리면 429가 증가할 수 있어 제한함.
MAX_WORKERS = 4
# Render / Pydroid 로컬 임시 캐시: 이전 조회 캔들을 재사용하고 변경된 최근 봉만 추가 수집
CANDLE_CACHE_DIR = Path(tempfile.gettempdir()) / "bithumb_v33_candle_cache"
CANDLE_CACHE_LOCK = threading.Lock()
SESSION_LOCAL = threading.local()
JOB_LOCK = threading.Lock()
JOB = None
EXPORT_FILES = {}  # 최근 연구 CSV(최대 3개)를 작업 전환 후에도 내려받을 수 있게 보관


class JobCancelled(Exception):
    pass


def ensure_active():
    with JOB_LOCK:
        if JOB is not None and JOB.get("running") and JOB.get("cancel"):
            raise JobCancelled("사용자가 검사를 중단했어요. 결과는 저장되지 않았습니다.")


def cancel_job(job_id):
    with JOB_LOCK:
        if not JOB or JOB["id"] != job_id or not JOB["running"]:
            return {"error":"진행 중인 작업이 없습니다."}
        JOB["cancel"] = True
        JOB["stage"] = "중지 요청됨 · 현재 API 응답 정리 중"
        return {"ok":True, "message":"중지 요청을 접수했습니다."}


def request_session():
    session = getattr(SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"Accept": "application/json", "User-Agent": "bithumb-v3-research/1.1"})
        SESSION_LOCAL.session = session
    return session


def _cache_path(market, tf):
    name = hashlib.sha256(f"{market}|{tf}".encode()).hexdigest()[:24]
    return CANDLE_CACHE_DIR / f"{name}.bin"


def cache_read(market, tf):
    path = _cache_path(market, tf)
    try:
        if time.time() - path.stat().st_mtime > 172800:
            return [], float("inf")
        payload = json.loads(zlib.decompress(path.read_bytes()).decode("utf-8"))
        rows = [{**x, "t": datetime.fromisoformat(x["t"])} for x in payload]
        return rows, max(0.0, time.time() - path.stat().st_mtime)
    except (OSError, ValueError, TypeError, zlib.error):
        return [], float("inf")


def cache_write(market, tf, rows):
    try:
        CANDLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(market, tf)
        payload = json.dumps([{**b, "t": b["t"].isoformat()} for b in rows[-MAX_BARS_PER_MARKET:]], separators=(",", ":"))
        raw = zlib.compress(payload.encode("utf-8"), level=3)
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_bytes(raw)
        with CANDLE_CACHE_LOCK:
            os.replace(tmp, path)
            files = list(CANDLE_CACHE_DIR.glob("*.bin"))
            # 디스크 캐시는 약 70MB만 유지. 오래된 항목부터 제거.
            total = sum(f.stat().st_size for f in files)
            if total > 70_000_000:
                for f in sorted(files, key=lambda x:x.stat().st_mtime):
                    if total <= 55_000_000:
                        break
                    size = f.stat().st_size
                    f.unlink(missing_ok=True)
                    total -= size
    except (OSError, TypeError, ValueError):
        # 캐시 문제로 본 작업을 중단하지는 않는다.
        pass


def job_update(stage, pct=None, done=None, total=None, errors=None):
    with JOB_LOCK:
        if not JOB or not JOB.get("running"):
            return
        JOB["stage"] = stage
        if pct is not None:
            JOB["percent"] = max(JOB.get("percent", 0), min(99, int(pct)))
        if done is not None:
            JOB["done_count"] = done
        if total is not None:
            JOB["total_count"] = total
        if errors is not None:
            JOB["error_count"] = errors


def start_job(kind, cfg):
    global JOB
    if kind not in ("scan", "backtest"):
        return {"error": "지원하지 않는 작업"}
    with JOB_LOCK:
        if JOB and JOB["running"]:
            if JOB["kind"] == kind and JOB["settings"] == cfg:
                return {"job": JOB["id"], "reused": True}
            return {"error": "다른 작업 진행 중입니다. 완료 후 실행해주세요."}
        if not BUSY.acquire(blocking=False):
            return {"error": "다른 스캔/백테스트가 진행 중입니다."}
        job_id = uuid.uuid4().hex[:20]
        JOB = dict(id=job_id, kind=kind, settings=cfg, running=True, percent=0,
                   stage="작업 준비 중", done_count=0, total_count=0, error_count=0,
                   started=time.monotonic(), error=None, result=None, cancel=False)
    def worker():
        try:
            result = (precursor_scan(cfg,progress=job_update) if kind=="scan" else precursor_backtest(cfg,progress=job_update)) if cfg.strategy=="precursor" else (scan(cfg, progress=job_update) if kind=="scan" else backtest(cfg, progress=job_update))
            with JOB_LOCK:
                JOB["result"] = result if "error" not in result else None
                JOB["error"] = result.get("error")
                if isinstance(result,dict) and result.get('_csv_file'):
                    EXPORT_FILES[job_id]=(result['_csv_file'],result['csv_name'])
                    while len(EXPORT_FILES)>3:
                        old_id=next(iter(EXPORT_FILES))
                        old_file,_=EXPORT_FILES.pop(old_id)
                        try:Path(old_file).unlink(missing_ok=True)
                        except OSError:pass
        except JobCancelled as exc:
            with JOB_LOCK:
                JOB["error"] = str(exc)
        except Exception as exc:
            with JOB_LOCK:
                JOB["error"] = str(exc)[:400]
            print("[V3] background job error", repr(exc))
        finally:
            with JOB_LOCK:
                JOB["running"] = False
                JOB["percent"] = 100
                JOB["stage"] = "완료" if not JOB["error"] else "오류"
            BUSY.release()
    threading.Thread(target=worker, daemon=True, name=f"v3-{kind}").start()
    return {"job": job_id, "reused": False}


def job_status(job_id):
    with JOB_LOCK:
        if JOB is None or JOB["id"] != job_id:
            return {"error": "진행 중인 작업이 없어요. 서버가 재시작됐을 수도 있습니다."}
        return dict(job=JOB["id"], kind=JOB["kind"], running=JOB["running"],
                    percent=JOB["percent"], stage=JOB["stage"],
                    done=JOB["done_count"], total=JOB["total_count"],
                    errors=JOB["error_count"], elapsed=int(time.monotonic()-JOB["started"]),
                    error=JOB["error"], result=None if JOB["running"] else ({k:v for k,v in JOB["result"].items() if k!="_csv_file"} if isinstance(JOB["result"],dict) else JOB["result"]))


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
    strategy: str = "precursor"
    tf: int = 60
    count: int = 40
    days: int = 15
    fee: float = 0.0025
    slip: float = 0.0  # V3.4 사용자 선택: 슬리피지 미반영(이상적 체결 가정)
    capital: int = 1_000_000
    turnover_min: int = 500_000_000
    flow: str = "weak"
    trail: float = 3.0
    tp_r: float = 1.8
    tp_half_r: float = 0.0
    be_r: float = 0.0
    btc: bool = False
    max_positions: int = 1
    max_trades_day: int = 2
    rs: float = 0.0
    rs_days: int = 7

    @staticmethod
    def from_q(q):
        st = q.get("st", ["precursor"])[0]
        if st not in ("pullback", "trend", "flow", "trap", "precursor"):
            st = "precursor"
        tf = number(q, "tf", (60 if st == "precursor" else 15 if st == "trap" else 60 if st in ("trend", "flow") else 15), 5, 240, True)
        if tf not in VALID_TF or (st in ("trend", "flow") and tf not in (60, 240)) or (st == "pullback" and tf not in (5, 15, 60)) or (st == "trap" and tf not in (15, 60)) or (st == "precursor" and tf not in (60, 240)):
            tf = 60 if st == "precursor" else 15 if st == "trap" else 60 if st in ("trend", "flow") else 15
        flow = q.get("of", ["weak"])[0]
        if flow not in ("off", "weak", "strong"):
            flow = "weak"
        return Settings(
            strategy=st, tf=tf, count=(int(q.get("n", ["40"])[0]) if q.get("n", ["40"])[0] in ("0", "40", "80") else 40),
            days=(int(q.get("days", ["15"])[0]) if q.get("days", ["15"])[0] in ("15", "30", "80", "180", "365") else 15),
            fee=number(q, "fee", .25, 0, 1) / 100,
            slip=0.0,
            capital=number(q, "amt", 1_000_000, 100_000, 100_000_000, True),
            turnover_min=number(q, "liquidity", 500_000_000, 0, 100_000_000_000, True),
            flow=flow, trail=number(q, "trail", 3, 1, 6),
            tp_r=number(q, "tp", 1.8, 1, 5),
            tp_half_r=number(q, "tp1", 0, 0, 5),
            be_r=number(q, "be", 0, 0, 5),
            btc=q.get("btc", ["off"])[0] == "on",
            max_positions=number(q, "maxpos", 1, 1, 5, True),
            max_trades_day=(min(2, number(q, "maxday", 2, 1, 4, True)) if st in ("trap", "precursor") else number(q, "maxday", (2 if st in ("trend", "flow") else 4), 1, 4, True)),
            rs=number(q, "rs", 0, 0, 1), rs_days=number(q, "rsd", 7, 1, 14, True),
        )


def api(endpoint, params=None, retry=3):
    url = f"{BASE}/{endpoint}"
    last = "알 수 없는 오류"
    for attempt in range(retry):
        try:
            resp = request_session().get(url, params=params, timeout=(5, 14))
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
    # 새 거래대금 반응 연구는 BTC(상대강도 비교 기준)와 스테이블코인을 매매 대상에서 제외한다.
    excluded = {"BTC", "USDT", "USDC", "DAI", "FDUSD", "TUSD", "PYUSD", "USD1", "USDD", "USDE"} if cfg.strategy in ("flow", "trap", "precursor") else set()
    eligible = [x for x in rows if x["turnover"] >= cfg.turnover_min
                and x["market"].split("-",1)[-1] not in excluded]
    return (eligible[:cfg.count] if cfg.count else eligible), len(eligible)


def candles(market, tf, need, cache_ttl=90):
    """KST 연결 기반 페이징 + 압축 캐시. 최신 200봉과 과거 페이지를 병합.
    1차 요청 결과에 공백이 있으면 이전 캐시를 버려 누락을 정상 봉처럼 취급하지 않는다.
    """
    if tf not in VALID_TF:
        raise ValueError("지원하지 않는 캔들 단위")
    if need > MAX_BARS_PER_MARKET:
        raise ValueError(f"{need:,}개 봉 요청은 1종목 {MAX_BARS_PER_MARKET:,}개 제한을 초과합니다.")
    need = max(need, 10)
    saved, age = cache_read(market, tf)
    if saved and len(saved) >= need and age < cache_ttl:
        return saved[-need:]
    seen = {b["t"]: b for b in saved}
    fetched = False
    page_no = 0
    cursor = None
    # 새 조회는 항상 최신 구간부터 시작하여 마감된 신규 봉을 반영한다.
    if not saved or age >= cache_ttl:
        data = api(f"candles/minutes/{tf}", {"market":market,"count":min(200,need)})
        fetched = True
        if not data:
            return []
        recent = []
        for x in data:
            try:
                at = datetime.strptime(x["candle_date_time_kst"], "%Y-%m-%dT%H:%M:%S")
                row = {"t":at,"o":float(x["opening_price"]),"h":float(x["high_price"]),
                       "l":float(x["low_price"]),"c":float(x["trade_price"]),
                       "v":float(x["candle_acc_trade_volume"]),
                       "turn":float(x.get("candle_acc_trade_price",0))}
                if all(math.isfinite(row[k]) for k in ("o","h","l","c","v","turn")) and row["l"]>0 and row["h"]>=row["l"]:
                    recent.append(row)
            except (ValueError, KeyError, TypeError):
                continue
        if not recent:
            return []
        if saved and min(x["t"] for x in recent) > saved[-1]["t"] + timedelta(minutes=tf):
            seen = {}  # 캐시와 신규 API 사이에 자료 공백이 있어 재조회
        for b in recent:
            seen[b["t"]] = b
        page_no += 1
    previous_oldest = None
    # 최신 200봉과 캐시 병합으로 충분하면 나머지 과거 API 페이지는 생략.
    for _ in range(math.ceil(need/200)+3):
        ensure_active()
        if len(seen) >= need:
            break
        if not seen:
            params = {"market":market, "count":min(200,need)}
        else:
            oldest = min(seen)
            cursor = oldest.strftime("%Y-%m-%d %H:%M:%S")
            if cursor == previous_oldest:
                break
            previous_oldest = cursor
            params = {"market":market,"count":min(200,need-len(seen)),"to":cursor}
        data = api(f"candles/minutes/{tf}",params)
        fetched = True
        page_no += 1
        if page_no == 1 or page_no % 5 == 0: job_update(f"과거 캔들 {market} · {page_no}페이지 조회 중")
        if not data:
            break
        added = 0
        for x in data:
            try:
                at=datetime.strptime(x["candle_date_time_kst"],"%Y-%m-%dT%H:%M:%S")
                row={"t":at,"o":float(x["opening_price"]),"h":float(x["high_price"]),
                     "l":float(x["low_price"]),"c":float(x["trade_price"]),
                     "v":float(x["candle_acc_trade_volume"]),
                     "turn":float(x.get("candle_acc_trade_price",0))}
                if all(math.isfinite(row[k]) for k in ("o","h","l","c","v","turn")) and row["l"]>0 and row["h"]>=row["l"]:
                    added += int(at not in seen)
                    seen[at]=row
            except (KeyError,TypeError,ValueError):
                continue
        if not added or len(data) < params["count"]:
            break
        time.sleep(.08)
    out = [seen[t] for t in sorted(seen)][-MAX_BARS_PER_MARKET:]
    if fetched and out:
        cache_write(market,tf,out)
    return out[-need:]


def enrich(bars, tf):
    """O(N) rolling ATR/거래량/거래대금. V3.1의 반복 sum(slice)을 제거.
    결측 봉은 기존과 같이 해당 시점부터 워밍업 재시작.
    """
    alpha50, alpha200 = 2/51, 2/201
    ema50 = ema200 = None
    atr_q, vol_q, turn_q, turn20_q = deque(), deque(), deque(), deque()
    atr_sum = vol_sum = turn_sum = turn20_sum = 0.0
    turn_window = 1440 // tf
    previous = None
    run = 0
    for b in bars:
        contiguous = previous is not None and b["t"]-previous["t"]==timedelta(minutes=tf)
        if not contiguous:
            run = 0
            ema50=ema200=b["c"]
            atr_q.clear();vol_q.clear();turn_q.clear();turn20_q.clear()
            atr_sum=vol_sum=turn_sum=turn20_sum=0.0
        else:
            run += 1
            ema50=alpha50*b["c"]+(1-alpha50)*ema50
            ema200=alpha200*b["c"]+(1-alpha200)*ema200
        old_close=previous["c"] if contiguous else b["c"]
        tr=max(b["h"]-b["l"],abs(b["h"]-old_close),abs(b["l"]-old_close))
        b["volavg"] = vol_sum/20 if len(vol_q)>=20 else None
        b["turnavg20"] = turn20_sum/20 if len(turn20_q)>=20 else None
        b["turn24"] = turn_sum + b["turn"] if len(turn_q)>=turn_window-1 else None
        atr_q.append(tr);atr_sum+=tr
        if len(atr_q)>14:atr_sum-=atr_q.popleft()
        b["atr"] = atr_sum/14 if len(atr_q)>=14 else None
        vol_q.append(b["v"]);vol_sum+=b["v"]
        if len(vol_q)>20:vol_sum-=vol_q.popleft()
        turn20_q.append(b["turn"]);turn20_sum+=b["turn"]
        if len(turn20_q)>20:turn20_sum-=turn20_q.popleft()
        turn_q.append(b["turn"]);turn_sum+=b["turn"]
        if len(turn_q)>turn_window-1:turn_sum-=turn_q.popleft()
        b["ema50"]=ema50 if run>=50 else None
        b["ema200"]=ema200 if run>=200 else None
        b["run"]=run
        b["dr"]=(2*b["c"]-b["h"]-b["l"])/max(1e-12,b["h"]-b["l"])
        previous=b
    return bars


def min_history(c):
    # 검증용 워밍업: 60분 추세는 EMA200, 눌림은 EMA50, RS 설정이 있으면 별도 창 보장
    return max((220 if c.strategy == "trend" else (80 if c.strategy in ("flow", "precursor") else 95)), c.rs_days * 1440 // c.tf + 22 if c.rs and c.strategy=="trend" else 0)


def load_series(markets, cfg, days=None, progress=None):
    days = cfg.days if days is None else days
    need = math.ceil(days * 1440 / cfg.tf) + min_history(cfg) + 4
    datasets, errors = {}, []

    def one(item):
        m = item["market"]
        try:
            ensure_active()
            bars = enrich(candles(m, cfg.tf, need, cache_ttl=(180 if days is None else 55)), cfg.tf)
            if len(bars) < min_history(cfg) + 5:
                return m, None, "충분한 연속 이력 없음"
            return m, bars, None
        except JobCancelled:
            return m, None, "중지 요청됨"
        except Exception as exc:
            return m, None, str(exc)[:170]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(one, item) for item in markets]
        done = 0
        for fut in as_completed(futures):
            ensure_active()
            done += 1
            market, data, err = fut.result()
            if err:
                errors.append(f"{market}: {err}")
            else:
                datasets[market] = data
            if progress:
                progress(market,done,len(markets),len(errors))
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




def entry_snapshot(bars, signal_ix, entry_open, level, cfg):
    """신호 마감 시점까지와 다음 봉 진입 시가만 사용한 재현 가능한 진입 특징.
    거래 결과/MFE/미래 종가는 절대 들어가지 않음.
    """
    b = bars[signal_ix]
    def pct(v, digits=3):
        return round(v, digits) if v is not None and math.isfinite(v) else None
    ema = b.get("ema200")
    past_ema = bars[signal_ix-5].get("ema200") if signal_ix >= 5 else None
    volume = (b["v"] / b["volavg"]) if b.get("volavg") and b["volavg"]>0 else None
    past6 = (100*(b["c"] / bars[signal_ix-6]["c"]-1)
             if signal_ix>=6 and b.get("run",0)>=6 and bars[signal_ix-6]["c"]>0 else None)
    length24 = int(1440/cfg.tf)
    past24 = (100*(b["c"] / bars[signal_ix-length24]["c"]-1)
              if signal_ix>=length24 and b.get("run",0)>=length24 and bars[signal_ix-length24]["c"]>0 else None)
    return {
        "volume_ratio": pct(volume,2),
        "ema_slope_5_pct": pct(100*(ema/past_ema-1) if ema and past_ema and past_ema>0 else None),
        "ema_distance_pct": pct(100*(b["c"]/ema-1) if ema and ema>0 else None),
        "breakout_atr": pct((b["c"]-level)/b["atr"] if b.get("atr") and b["atr"]>0 else None),
        "atr_pct": pct(100*b["atr"]/b["c"] if b.get("atr") and b["c"]>0 else None),
        "prior6_pct": pct(past6), "prior24_pct": pct(past24),
        "entry_gap_pct": pct(100*(entry_open/b["c"]-1) if b["c"]>0 else None),
        "turn24_billion": pct(b["turn24"]/1e9 if b.get("turn24") is not None else None,2),
    }

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


def trend_retest_entry(market, bars, cfg, k, level, btc_map=None, rs_map=None):
    """V3.4 실험군: 과거 돌파 k 이후 최대 4개 *마감봉*에서 재테스트+양봉 회복.
    시점 j까지의 자료만 확인하여 j+1 시가에 진입하며 미래 봉 성과로 진입하지 않는다.
    """
    tfdelta = timedelta(minutes=cfg.tf)
    atr = bars[k].get("atr")
    if not atr or atr <= 0:
        return None
    for j in range(k + 1, min(k + 5, len(bars) - 1)):
        b, prior = bars[j], bars[j - 1]
        if b["t"] - prior["t"] != tfdelta:
            break
        # 재테스트 도중 아래로 무너지면 같은 돌파에서 더 이상 진입하지 않음.
        if b["c"] < level - 0.5 * atr or b["l"] < level - 1.0 * atr:
            break
        if not (level - .5*atr <= b["l"] <= level + .35*atr):
            continue
        if not (b["c"] > b["o"] and level < b["c"] <= level + 1.5*atr):
            continue
        if not (b.get("ema200") is not None and b["c"] > b["ema200"]):
            continue
        if not b.get("volavg") or b["v"] < .8 * b["volavg"]:
            continue
        if b.get("turn24") is None or b["turn24"] < cfg.turnover_min:
            continue
        if btc_map is not None and not btc_map.get(b["t"], False):
            continue
        if rs_map is not None and rs_map.get((b["t"], market), 1.0) > cfg.rs:
            continue
        ni = j + 1
        if bars[ni]["t"] - b["t"] != tfdelta:
            break
        entry = bars[ni]["o"]
        lv = risk_levels_trend(bars, j, entry, cfg)
        if lv is None:
            continue
        stop, tp, risk = lv
        return {"market": market, "strategy": "trend", "entry_rule": "RETEST",
                "breakout_at": bars[k]["t"].isoformat(),
                "signal_at": b["t"].isoformat(), "entry_at": bars[ni]["t"].isoformat(),
                "price": entry, "stop": stop, "tp": tp, "risk_pct": risk*100,
                "context": {"box":level, "retest": b["l"],
                            "volume_ratio":round(b["v"]/max(b["volavg"],1e-12),2)},
                "features": entry_snapshot(bars,j,entry,level,cfg),
                "entry_ix":ni}
    return None


def exit_path_diagnostics(bars, entry_ix, result, cfg):
    """출입 이후 데이터는 오직 *사후 분석*에 사용한다. 진입 선택/신호에는 사용 불가.
    24시간이 끝까지 존재하며 정확히 연속한 경우에만 사후 상승을 판정한다.
    """
    if result.get("status") != "CLOSED" or not result.get("exit_at"):
        return {}
    exit_at = datetime.fromisoformat(result["exit_at"])
    tfdelta = timedelta(minutes=cfg.tf)
    exit_ix = None
    for ix in range(entry_ix, min(len(bars), entry_ix + (120 if cfg.tf==60 else 60 if cfg.tf==240 else 52))):
        if bars[ix]["t"] + tfdelta == exit_at:
            exit_ix = ix
            break
    if exit_ix is None:
        return {}
    entry_px = bars[entry_ix]["o"]
    observed = bars[entry_ix:exit_ix+1]
    result_diag = {"mfe_pct":round(100*(max(b["h"] for b in observed)/entry_px - 1),3),
                   "mae_pct":round(100*(min(b["l"] for b in observed)/entry_px - 1),3)}
    exit_px = result.get("exit_price")
    if not exit_px or exit_px <= 0:
        return result_diag
    future_bars = bars[exit_ix+1:exit_ix+1+int(1440/cfg.tf)]
    valid=[]
    prev = bars[exit_ix]
    for b in future_bars:
        if b["t"] - prev["t"] != tfdelta:
            break
        valid.append(b)
        prev=b
    # 재현할 수 있는 미래가 없으면 과거 공백을 상승 0으로 취급하지 않는다.
    hours6 = max(1,int(360/cfg.tf))
    hours24 = max(1,int(1440/cfg.tf))
    if len(valid) >= hours6:
        result_diag["post6_peak_pct"] = round(100*(max(b["h"] for b in valid[:hours6])/exit_px - 1),3)
    if len(valid) >= hours24:
        result_diag["post24_peak_pct"] = round(100*(max(b["h"] for b in valid[:hours24])/exit_px - 1),3)
        result_diag["post24_trough_pct"] = round(100*(min(b["l"] for b in valid[:hours24])/exit_px - 1),3)
        result_diag["post24_close_pct"] = round(100*(valid[hours24-1]["c"]/exit_px - 1),3)
    result_diag["hold_hours"] = round((exit_at-bars[entry_ix]["t"]).total_seconds()/3600,2)
    # MFE/MAE는 봉 고저가를 관측한 값. 손절 당일 봉의 체결 후 가격도 포함될 수 있음.
    # 정확한 거래 경로가 아님을 프런트엔드에서 명시한다.
    return result_diag


def detect_all(market, bars, cfg, btc_map=None, rs_map=None, for_scan=False, retest_out=None):
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
            if retest_out is not None and not for_scan:
                alternative = trend_retest_entry(market,bars,cfg,k,hh,btc_map,rs_map)
                if alternative and bars[alternative["entry_ix"]-1]["t"] >= cutoff:
                    alt_i = alternative.pop("entry_ix")
                    alt_outcome = simulate(bars,alt_i,alternative["stop"],alternative["tp"],cfg)
                    if alt_outcome:
                        alternative.update(alt_outcome)
                        alternative.update(exit_path_diagnostics(bars,alt_i,alt_outcome,cfg))
                        retest_out.append(alternative)
        if not for_scan and sig_time < cutoff:
            continue
        event={"market":market,"strategy":cfg.strategy,"signal_at":sig_time.isoformat(),
               "entry_at":(bars[i]["t"] if i < len(bars) else sig_time + timedelta(minutes=cfg.tf)).isoformat(),
               "price":entry,"stop":stop,"tp":tp,"risk_pct":100*risk,"context":context,
               "entry_rule":"BREAKOUT" if cfg.strategy=="trend" else "PULLBACK",
               "features":entry_snapshot(bars,(k if cfg.strategy=="trend" else j),entry,hh,cfg)}
        if for_scan:
            events.append(event)
        elif i<len(bars):
            outcome=simulate(bars,i,stop,tp,cfg)
            if outcome is None:
                continue
            event.update(outcome)
            event.update(exit_path_diagnostics(bars,i,outcome,cfg))
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


def scan(cfg, progress=None):
    if cfg.strategy=="trap":
        return trap_scan(cfg,progress)
    if cfg.strategy=="flow":
        return flow_scan(cfg,progress)
    if progress: progress("거래대금 상위 마켓 조회", 3, 0, 0, 0)
    chosen,eligible=universe(cfg)
    if progress:progress("검사 마켓 확정", 5, 0, len(chosen), 0)
    if not chosen:
        return {"error":"거래대금 조건을 만족하는 종목을 찾지 못했습니다. API 연결도 확인해주세요."}
    if progress: progress("캔들 수집 준비", 6, 0, len(chosen), 0)
    def scan_load(m,d,total,err):
        if progress:progress(f"캔들 수집: {m}", 6+int(78*d/max(total,1)), d,total,err)
    bars, errors=load_series(chosen,cfg,days=max(4,math.ceil(min_history(cfg)*cfg.tf/1440)+1), progress=scan_load)
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
    for idx,item in enumerate(chosen):
        m=item["market"]
        if progress:progress(f"신호 계산: {m}", 85+int(13*(idx+1)/max(1,len(chosen))),idx+1,len(chosen),len(errors))
        if m not in bars:
            continue
        sig,wt,q=detect_all(m,bars[m],cfg,btc_map,rs_map,True)
        quality[m]=q
        for s in sig:
            s["turnover_24h"]=item["turnover"]
            signals.append(s)
        watch+=wt
    for s in signals[:6]:
        s["buy_ratio"]=simple_buy_ratio(s["market"])
    for s in signals:
        # 정량 점수는 실제 가격·거래량·ATR·이평 조건으로 계산. 승률 예측값이 아님.
        vr = max(0.0, float(s["context"].get("volume_ratio") or 0))
        risk = max(0.0, float(s["risk_pct"]))
        market_bars=complete(bars[s["market"]],cfg.tf)
        b=market_bars[-1]
        ema=b.get("ema50") if cfg.strategy=="pullback" else b.get("ema200")
        past_ema=market_bars[-6].get("ema50" if cfg.strategy=="pullback" else "ema200") if len(market_bars)>=6 else None
        atr=b.get("atr") or 1e-9
        # 현재 추세와 최근 5봉 모멘텀
        ema_above = ema is not None and b["c"]>ema
        ema_slope = ema is not None and past_ema is not None and ema>past_ema
        trend=round(100*(int(ema_above)+int(ema_slope))/2)
        last5=market_bars[-5:]
        higher_lows=sum(last5[i]["l"] >= last5[i-1]["l"] for i in range(1,len(last5)))
        structure=round(25*higher_lows)
        box=float(s["context"].get("box",b["c"]))
        distance=abs(s["price"]-box)/max(atr,1e-9)
        timing=round(max(0,100-35*distance))
        if cfg.strategy == "pullback":
            touch_gap=abs(float(s["context"].get("retest",box))-box)/max(atr,1e-9)
            setup=round(max(0,100-35*touch_gap))
        else:
            breakout_gap=abs(b["c"]-box)/max(atr,1e-9)
            setup=round(max(0,100-35*abs(breakout_gap-.5)))
        entry=round(max(0,100-15*risk-10*distance))
        safety=round(max(0,100-16*risk))
        volume=round(min(100,vr/3*100))
        vals=[structure,timing,trend,setup,entry,safety,volume]
        s["score"]={"structure":structure,"timing":timing,"trend":trend,
                    "setup":setup,"entry":entry,"safety":safety,"volume":volume,
                    "total":round(sum(vals)/len(vals)),"pressure":s.get("buy_ratio")}
        if cfg.strategy == "pullback":
            s["why"]=(f"박스 상단 {box:,.4g}원 돌파 후 눌림 지지·양봉 마감을 확인했어요. "
                      f"거래량은 이전 평균의 {vr:.2f}배, 손절 거리 {risk:.2f}%예요. "
                      f"다음 봉 예상 진입 {s['price']:,.4g}원, 손절 {s['stop']:,.4g}원, "
                      f"1차 목표 {s['tp']:,.4g}원입니다. 상위 봉 추세는 별도로 확인하세요.")
        else:
            s["why"]=(f"고점 {box:,.4g}원 돌파와 EMA200 상승 조건을 확인했어요. "
                      f"거래량은 평균의 {vr:.2f}배, 손절 거리 {risk:.2f}%예요. "
                      f"초기 손절 {s['stop']:,.4g}원이며 추세추종은 ATR 트레일링으로 관리합니다.")

    signals.sort(key=lambda x:x["turnover_24h"],reverse=True)
    watch=watch[:10]
    return {"version":VERSION,"checked":len(bars),"eligible":eligible,"signals":signals,"watch":watch,
            "errors":errors[:20],"updated":kst_now().strftime("%Y-%m-%d %H:%M:%S KST"),
            "quality":quality,"notes":["미완성 현재 봉은 신호에 사용하지 않습니다.","진입가는 확정 신호 다음 봉의 예상 시가이며 실시간 확정 체결가가 아닙니다."]}


def validate_workload(cfg, n_markets):
    """선택 분봉과 기간을 그대로 지키며 처리 가능성을 미리 확인합니다."""
    needed = math.ceil(cfg.days * 1440 / cfg.tf) + min_history(cfg) + 4
    if needed > MAX_BARS_PER_MARKET:
        recommended = "1시간 또는 4시간봉" if cfg.tf in (5, 15) else "4시간봉"
        return {"error": (f"{cfg.days}일·{cfg.tf}분봉은 종목당 {needed:,}개 봉이 필요해요. "
                f"현재 서버의 종목당 한도는 {MAX_BARS_PER_MARKET:,}개입니다. "
                f"기간을 줄이거나 {recommended}을 선택해주세요. 일부 기간만 검증하고 전체 기간으로 표시하지 않습니다.")}
    # 실전과 동일한 비교군 상대강도는 전체 종목을 같은 시점에 메모리에 필요로 함.
    if cfg.strategy == "trend" and cfg.rs and needed*n_markets > 220_000:
        return {"error": (f"상대강도 비교에는 전체 종목의 과거 자료가 함께 필요해요 ({needed*n_markets:,}봉). "
                "장기·전체 검사는 상대강도 필터를 끄거나 40종목으로 줄여주세요.")}
    return None


def summarize_diagnostics(trades, capital):
    from collections import Counter, defaultdict
    why=Counter(); coins=defaultdict(list); quick=0
    for x in trades:
        if x.get("status") != "CLOSED":
            continue
        why[x.get("why", "기타")] += 1
        coins[x["market"]].append(x)
        if "손절" in x.get("why", ""):
            try:
                if (datetime.fromisoformat(x["exit_at"])-datetime.fromisoformat(x["entry_at"])) <= timedelta(minutes=45):
                    quick += 1
            except (ValueError, TypeError):
                pass
    cm=[]
    for market, xs in coins.items():
        wins=sum(x["net_pct"]>0 for x in xs)
        cm.append({"market":market,"n":len(xs),"win":wins,"loss":len(xs)-wins,
                   "win_rate":round(100*wins/len(xs),1),
                   "sum_net":round(sum(x["net_pct"] for x in xs),3),
                   "pnl_krw":int(round(sum(capital*x["net_pct"]/100 for x in xs)))})
    cum=peak=maximum_drawdown=0.0; losses=longest=0
    for x in sorted((a for a in trades if a.get("status")=="CLOSED"),key=lambda t:t.get("exit_at", "")):
        cum+=capital*x["net_pct"]/100
        peak=max(peak,cum)
        maximum_drawdown=max(maximum_drawdown,peak-cum)
        losses=losses+1 if x["net_pct"]<=0 else 0
        longest=max(longest,losses)
    cm.sort(key=lambda x:(-x["n"],-x["sum_net"]))
    return {"markets":cm,"reasons":[{"reason":k,"n":v} for k,v in why.most_common()],
            "quick_stop_45m":quick,"mdd_krw":round(maximum_drawdown),"max_lose_streak":longest}


def time_exit_analysis(trades):
    """청산 사유별 사후 움직임. 결과 누락은 별도 집계해 성공으로 취급하지 않음."""
    from collections import defaultdict
    groups=defaultdict(list)
    for t in trades:
        if t.get("status")=="CLOSED":
            groups[t.get("why","기타")].append(t)
    rows=[]
    for why,xx in sorted(groups.items(), key=lambda z:(-len(z[1]),z[0])):
        complete24=[x for x in xx if x.get("post24_peak_pct") is not None]
        rows.append({"why":why,"n":len(xx),
                     "avg_net":round(sum(x["net_pct"] for x in xx)/len(xx),3),
                     "avg_mfe":round(sum(x.get("mfe_pct",0) for x in xx)/len(xx),3),
                     "avg_mae":round(sum(x.get("mae_pct",0) for x in xx)/len(xx),3),
                     "post24_count":len(complete24),
                     "post24_up2":sum(x["post24_peak_pct"]>=2 for x in complete24),
                     "post24_avg_peak":round(sum(x["post24_peak_pct"] for x in complete24)/len(complete24),3) if complete24 else None})
    return rows




def upper_trim_stat(trades, cfg):
    """대형 수익 2건을 빼고도 결과가 유지되는지 확인. 실제 필터/매매 규칙 아님."""
    closed = [x for x in trades if x.get("status")=="CLOSED" and isinstance(x.get("net_pct"),(int,float))]
    rest = sorted(closed, key=lambda x:x["net_pct"], reverse=True)[2:]
    return {"original_n":len(closed),"removed":min(2,len(closed)),"left_n":len(rest),
            "avg_net":round(sum(x["net_pct"] for x in rest)/len(rest),3) if rest else None,
            "sum_net":round(sum(x["net_pct"] for x in rest),3) if rest else None}


def predeclared_gate(event, key):
    """사전에 고정한 단순 가설 3개. 결과 데이터에 맞춰 임계값 변경 금지."""
    f=event.get("features") or {}
    vol=f.get("volume_ratio")
    slope=f.get("ema_slope_5_pct")
    if key=="VOL2":
        return vol is not None and vol>=2.0
    if key=="SLOPE015":
        return slope is not None and slope>=0.15
    if key=="BOTH":
        return vol is not None and slope is not None and vol>=2.0 and slope>=0.15
    return key=="BASE"


def compare_fixed_gates(base_events, cfg, cut_time, baseline=None):
    """단일 사전 명시 필터 3개와 무필터 기준. 테스트를 보고 후보/문턱을 고르지 않음.
    필터는 *포트폴리오 선택 전에* 적용하여 슬롯 선별 왜곡 방지.
    """
    rules=[("BASE","기준 돌파 · 2회"),("VOL2","거래량 ≥ 2.0배"),
           ("SLOPE015","EMA200 5봉 기울기 ≥ +0.15%"),
           ("BOTH","두 조건 동시 충족")]
    c2=replace(cfg,max_trades_day=2)
    result=[];variant={}
    for key,label in rules:
        chosen=(baseline if key=="BASE" and baseline is not None
                else select_trades([x for x in base_events if predeclared_gate(x,key)],c2))
        st,_,split=daily_and_split(chosen,c2,cut_time)
        empty={"n":0,"win":0,"loss":0,"win_rate":None,"avg_net":None,"sum_net":None,"pnl_krw":0,"ev":None}
        split={"train":split["train"] or empty,"test":split["test"] or empty}
        test=[x for x in chosen if x.get("status")=="CLOSED" and datetime.fromisoformat(x["entry_at"])>=cut_time]
        result.append({"key":key,"label":label,"stat":st,"train":split["train"],"test":split["test"],
                       "test_no_top2":upper_trim_stat(test,cfg),"selected":len(chosen),
                       "unknown":sum(x.get("status")!="CLOSED" for x in chosen)})
        variant[key]=chosen
    return result,variant


def win_loss_features(trades):
    """진입 전에 알 수 있었던 특징의 승패별 통계. 사후 진입 필터 자동 결정에는 미사용."""
    keys=[("volume_ratio","진입 전 거래량 배수"), ("ema_slope_5_pct","EMA200 5봉 기울기 %"),
          ("ema_distance_pct","EMA200 위 이격 %"),("breakout_atr","돌파 폭 / ATR"),
          ("atr_pct","ATR / 가격 %"),("prior6_pct","직전 6봉 수익률 %"),
          ("prior24_pct","직전 24시간 수익률 %"),("entry_gap_pct","신호 대비 다음 시가 %")]
    win=[x for x in trades if x.get("status")=="CLOSED" and x.get("net_pct",0)>0]
    lose=[x for x in trades if x.get("status")=="CLOSED" and x.get("net_pct",0)<=0]
    def metric(rows,key):
        vals=[(x.get("features") or {}).get(key) for x in rows]
        nums=[v for v in vals if isinstance(v,(float,int)) and math.isfinite(v)]
        return {"n":len(nums),"avg":round(sum(nums)/len(nums),3) if nums else None}
    return [{"key":key,"label":label,"win":metric(win,key),"loss":metric(lose,key)} for key,label in keys]


def build_trade_diagnostics(trades):
    """90거래/손실 n건을 혼동하지 않도록 손실/회복 유형을 전체·검증으로 집계."""
    closed=[x for x in trades if x.get("status")=="CLOSED"]
    losses=[x for x in closed if x.get("net_pct",0)<=0]
    winners=[x for x in closed if x.get("net_pct",0)>0]
    stop=[x for x in losses if "손절" in x.get("why","")]
    complete24=[x for x in stop if isinstance(x.get("post24_close_pct"),(int,float))]
    no_rise=[x for x in losses if isinstance(x.get("mfe_pct"),(int,float)) and x["mfe_pct"]<1]
    gave_back=[x for x in losses if isinstance(x.get("mfe_pct"),(int,float)) and x["mfe_pct"]>=2]
    fast6=[x for x in stop if isinstance(x.get("hold_hours"),(int,float)) and x["hold_hours"]<=6]
    comeback=[x for x in complete24 if x["post24_close_pct"]>=2]
    return {"total":len(closed),"wins":len(winners),"losses":len(losses),
            "loss_never_up1":len(no_rise),"loss_gave_back2":len(gave_back),
            "stop_losses":len(stop),"stop_under6h":len(fast6),
            "stop_post24_observed":len(complete24),"stop_post24_close_up2":len(comeback),
            "avg_win":round(sum(x["net_pct"] for x in winners)/len(winners),3) if winners else None,
            "avg_loss":round(sum(x["net_pct"] for x in losses)/len(losses),3) if losses else None,
            "top_wins":[{"market":x["market"],"entry_at":x["entry_at"],"net_pct":round(x["net_pct"],3),
                         "features":x.get("features")} for x in sorted(winners,key=lambda x:-x["net_pct"])[:5]],
            "worst_losses":[{"market":x["market"],"entry_at":x["entry_at"],"net_pct":round(x["net_pct"],3),
                            "features":x.get("features")} for x in sorted(losses,key=lambda x:x["net_pct"])[:5]]}


def research_verdict(gates, coverage, requested, cfg):
    """판정표: 고정 가설 중 학습·검증 각각 순익이 있고 표본이 충분한지.
    검증 10건/학습 20건 미만인 전략은 '좋다'고 채택하지 않음.
    유니버스 생존 편향/슬리피지 0 때문에 PASS도 실전 적용 금지.
    """
    ratio=coverage/max(requested,1)
    if ratio<.8:
        return {"status":"DATA_INCOMPLETE","text":"검사 가능 종목이 80% 미만입니다. 전략 평가를 중단하고 데이터부터 점검하세요.","coverage":round(ratio*100,1)}
    base=next((x for x in gates if x["key"]=="BASE"),None)
    if not base or base["train"]["n"]<20 or base["test"]["n"]<10:
        return {"status":"INSUFFICIENT","text":"기준 전략의 학습 20건·검증 10건 미만입니다. 수익을 주장하지 말고 추가 기간을 확인하세요.","coverage":round(ratio*100,1)}
    passed=[x for x in gates if x["train"]["n"]>=20 and x["test"]["n"]>=10
            and x["train"]["avg_net"]>0 and x["test"]["avg_net"]>0
            and x["test_no_top2"]["avg_net"] is not None and x["test_no_top2"]["avg_net"]>0]
    if passed:
        return {"status":"RESEARCH_ONLY","text":"사전 정의 가설 일부가 학습·검증에서 모두 플러스였습니다. 아직 생존편향·체결오차가 남아 실전 채택은 금지하고 독립 재검증만 하세요.",
                "keys":[x["key"] for x in passed],"coverage":round(ratio*100,1)}
    return {"status":"DISCARD","text":"현재 검증 기준을 충족하는 전략이 없습니다. 이 방식의 실전 적용을 중단하고, 추가 조건 덧붙이기보다 전략 폐기를 권합니다.","coverage":round(ratio*100,1)}

def compare_portfolios(base_events, retest_events, cfg, cut_time, btc_map=None):
    """공통 유니버스·봉·손절/청산 규칙. 선별 횟수/진입 구조만 분리 비교."""
    configs=[("BREAKOUT_4", "기존 돌파 · 하루 최대 4회", base_events, replace(cfg,max_trades_day=4)),
             ("BREAKOUT_2", "기존 돌파 · 하루 최대 2회", base_events, replace(cfg,max_trades_day=2)),
             ("RETEST_2", "눌림 확인 · 하루 최대 2회 (실험)", retest_events, replace(cfg,max_trades_day=2))]
    if btc_map is not None and not cfg.btc:
        btcevents=[x for x in base_events if btc_map.get(datetime.fromisoformat(x["signal_at"]),False)]
        configs.append(("BTC_2", "BTC 상승 추세만 · 최대 2회", btcevents, replace(cfg,max_trades_day=2)))
    comparison=[]; variant_trades={}
    for key,label,events,v_cfg in configs:
        result=select_trades(events,v_cfg)
        st,_,sp=daily_and_split(result,v_cfg,cut_time)
        comparison.append({"key":key,"label":label,"stat":st,"train":sp["train"],
                           "test":sp["test"],"selected":len(result),
                           "unknown":sum(x.get("status")!="CLOSED" for x in result)})
        variant_trades[key]=result
    return comparison,variant_trades



# ================= V3.6 신규 독립 가설: 거래대금 급증 -> 가격 유지 -> 회복 =================
# 과거 실패한 'EMA200 상승·고점 돌파'를 조건에 포함하지 않는다.
FLOW_RULES = {"turnover_burst_min": 2.5, "historical_24h_krw_min": 500_000_000,
              "close_location_min": 0.75, "max_burst_body_pct": 12.0,
              "consolidation_bars": [1, 3], "max_drawdown_from_burst_close_pct": 1.5,
              "relative_to_btc_24h_pp": 2.0, "stop_buffer_atr": 0.25,
              "risk_min_pct": 0.7, "risk_max_pct": 6.0,
              "target_r": 2.0, "hold_hours": 24, "max_daily_trades": 2}


def flow_btc_reference(rows, tf):
    """BTC 24시간 수익률은 같은 시각까지 확정된 과거 봉만 사용."""
    n = 1440 // tf
    ref = {}
    for i in range(n, len(rows)):
        b = rows[i]
        if b["run"] < n or rows[i-n]["c"] <= 0:
            continue
        r = 100*(b["c"]/rows[i-n]["c"]-1)
        ref[b["t"]] = {"ret24_pct":r, "up24":r>0}
    return ref


def flow_relative(bars, i, btc_ref, tf):
    n = 1440 // tf
    if i < n or bars[i]["run"] < n or bars[i-n]["c"] <= 0:
        return None
    b = btc_ref.get(bars[i]["t"])
    if b is None:
        return None
    coin = 100*(bars[i]["c"]/bars[i-n]["c"]-1)
    return {"coin_24h_pct":round(coin,3), "btc_24h_pct":round(b["ret24_pct"],3),
            "relative_pp":round(coin-b["ret24_pct"],3), "btc_up24":b["up24"]}


def flow_surge(bars, k, cfg):
    """급증봉 k의 완료 당시 자료만 사용; 기준 거래대금은 20개 직전 봉."""
    b=bars[k]
    avg=b.get("turnavg20")
    if b.get("run",0)<60 or not avg or avg<=0 or not b.get("atr"):
        return None
    if b.get("turn24") is None or b["turn24"] < max(cfg.turnover_min, FLOW_RULES["historical_24h_krw_min"]):
        return None
    ratio=b["turn"]/avg
    span=b["h"]-b["l"]
    loc=(b["c"]-b["l"])/span if span>0 else 0
    if ratio < FLOW_RULES["turnover_burst_min"] or loc < .75 or b["c"]<=b["o"]:
        return None
    if b["c"] / b["o"] > 1.12:  # 단일 봉 +12% 초과 추격 금지
        return None
    return {"turnover_ratio":round(ratio,3), "close_location":round(loc,3),
            "surge_turn_krw":round(b["turn"]), "historical_24h_turn_krw":round(b["turn24"]),
            "burst_close":b["c"], "burst_low":b["l"]}


def flow_confirmation(bars, k, tf):
    """폭증 후 1~3봉의 거래대금 둔화와 회복 양봉. 모든 봉이 마감되어야 함."""
    b=bars[k]
    step=timedelta(minutes=tf) if k+1<len(bars) else None
    if step is None:
        return None
    # 확인 봉 j는 2번째 이후 (최소 한 개 눌림/횡보 봉 확보)
    for j in range(k+2,min(k+5,len(bars))):
        seq=bars[k+1:j]
        conf=bars[j]
        if any(bars[x]["t"]-bars[x-1]["t"]!=step for x in range(k+1,j+1)):
            break
        lows=[x["l"] for x in seq]
        if not lows or min(lows) < b["l"]-0.35*b["atr"]:
            break
        if min(x["c"] for x in seq) < b["c"]*.985:
            break
        if not any(x["turn"] < b["turn"]*.8 for x in seq):
            continue
        if not (conf["c"]>conf["o"] and conf["c"]>bars[j-1]["c"] and conf["c"]>=b["c"]*.99):
            continue
        if conf["c"]>b["c"]+1.5*b["atr"]:
            continue
        return j,min(lows+[conf["l"]])
    return None


def simulate_flow(bars, idx, stop0, cfg):
    """완료봉 신호 뒤 다음 시가; 같은 봉의 손절/목표 동시 접촉은 손절 우선.
    기간 끝까지 24시간 확보 불가 또는 캔들 누락이면 UNKNOWN 처리.
    """
    if idx>=len(bars):
        return {"status":"UNKNOWN", "why":"진입 봉 부족"}
    entry=bars[idx]["o"]
    if entry<=0 or stop0<=0 or stop0>=entry:
        return None
    risk_pct=100*(entry-stop0)/entry
    if not (FLOW_RULES["risk_min_pct"] <= risk_pct <= FLOW_RULES["risk_max_pct"]):
        return None
    target=entry+2*(entry-stop0)
    limit=max(1,int(FLOW_RULES["hold_hours"]*60/cfg.tf))
    high=entry;low=entry
    end=min(idx+limit,len(bars))
    def finish(j,px,reason):
        gross,net=calc_outcome(entry,[(1.0,px)],cfg.fee)
        return {"status":"CLOSED", "entry_at":bars[idx]["t"].isoformat(),
                "exit_at":(bars[j]["t"]+timedelta(minutes=cfg.tf)).isoformat(),
                "exit_price":px,"price":entry,"stop":stop0,"tp":target,
                "gross_pct":round(100*gross,4),"net_pct":round(100*net,4),
                "risk_pct":round(risk_pct,3),"why":reason,
                "mfe_pct":round(100*(high/entry-1),3),"mae_pct":round(100*(low/entry-1),3)}
    for j in range(idx,end):
        b=bars[j]
        if j>idx and b["t"]-bars[j-1]["t"]!=timedelta(minutes=cfg.tf):
            return {"status":"UNKNOWN", "entry_at":bars[idx]["t"].isoformat(),"why":"보유 중 캔들 누락"}
        high=max(high,b["h"]);low=min(low,b["l"])
        if b["l"]<=stop0:
            return finish(j,min(stop0,b["o"]),"구조 손절")
        if b["h"]>=target:
            return finish(j,max(target,b["o"]),"2R 목표")
    if idx+limit>len(bars):
        return {"status":"UNKNOWN","entry_at":bars[idx]["t"].isoformat(),"why":"미래 24시간 봉 부족"}
    return finish(end-1,bars[end-1]["c"],"24시간 종료")


def flow_events(market,bars,cfg,btc_ref,cutoff=None,live=False):
    """주어진 시장의 3군 후보. 시장내 거래 선택은 이후 동시 보유/일 2회 규칙 적용.
    live=True일 때도 마지막 완료봉의 신호만 노출, 미완성 봉 결과는 이용하지 않음.
    """
    events={"SURGE_2":[],"HOLD_2":[],"HOLD_RS_2":[],"HOLD_BTC_2":[]}
    watch=[]
    n=len(bars)
    step=timedelta(minutes=cfg.tf)
    for k in range(65,n):
        if k%1500==0:ensure_active()
        burst=flow_surge(bars,k,cfg)
        if not burst:continue
        now=bars[k]
        sr=flow_relative(bars,k,btc_ref,cfg.tf)
        if not live and k+1<n and bars[k+1]["t"]-now["t"]==step:
            if cutoff is None or bars[k+1]["t"]>=cutoff:
                stop=now["l"]-0.25*now["atr"]
                outcome=simulate_flow(bars,k+1,stop,cfg)
                if outcome:
                    ev={"market":market,"strategy":"flow","rule":"SURGE_2",
                        "signal_at":now["t"].isoformat(),"rank":0, "score":0,
                        "turnover_ratio":burst["turnover_ratio"],"relative_pp":sr["relative_pp"] if sr else None,
                        "historical_24h_turn_krw":burst["historical_24h_turn_krw"],
                        "signal_close":now["c"],"signal_index":k, **outcome}
                    events["SURGE_2"].append(ev)
        confirm=flow_confirmation(bars,k,cfg.tf)
        if not confirm:
            if live and k>=n-3:
                watch.append({"market":market,"status":"거래대금 급증 후 회복 확인 대기",
                              "box":now["c"],"price":bars[-1]["c"]})
            continue
        j,retest_low=confirm
        confirmed=bars[j]
        rs=flow_relative(bars,j,btc_ref,cfg.tf)
        # 확인 신호 k+1...j에 존재하는 캔들만 사용; 다음 봉 시가는 진입가 계산에만 사용
        if live:
            if j != n-1:continue
            price=confirmed["c"]
            stop=min(retest_low,now["l"])-.25*now["atr"]
            if not (stop>0 and FLOW_RULES["risk_min_pct"] <= 100*(price-stop)/price <= FLOW_RULES["risk_max_pct"]):
                continue
            if rs is None or rs["relative_pp"]<2:
                watch.append({"market":market,"status":"상대강도 기준 미충족",
                              "box":confirmed["c"],"price":price})
                continue
            events["HOLD_RS_2"].append({"market":market,"strategy":"flow", "signal_at":confirmed["t"].isoformat(),
                "price":price,"stop":stop,"tp":price+2*(price-stop),"risk_pct":round(100*(price-stop)/price,2),
                "turnover_24h":burst["historical_24h_turn_krw"],
                "context":{"box":now["c"],"volume_ratio":burst["turnover_ratio"]},
                "score":{"total":None,"volume":min(100,int(100*burst["turnover_ratio"]/4)),
                         "structure":None,"timing":None,"trend":None,"setup":None,"entry":None,"safety":None,
                         "pressure":None},
                "why":f"거래대금 {burst['turnover_ratio']:.2f}배 급증 뒤 1~3봉 가격 유지·재상승을 확인했어요. BTC 대비 24시간 상대강도 {rs['relative_pp']:+.2f}%p. 진입은 다음 봉 시가를 기다려야 하며 실제 체결가는 미확정입니다."})
            continue
        if j+1>=n or bars[j+1]["t"]-confirmed["t"]!=step:
            continue
        if cutoff is not None and bars[j+1]["t"]<cutoff:
            continue
        stop=min(retest_low,now["l"])-.25*now["atr"]
        outcome=simulate_flow(bars,j+1,stop,cfg)
        if not outcome:
            continue
        common={"market":market,"strategy":"flow","signal_at":confirmed["t"].isoformat(),
                "turnover_ratio":burst["turnover_ratio"],"close_location":burst["close_location"],
                "historical_24h_turn_krw":burst["historical_24h_turn_krw"],
                "signal_close":confirmed["c"],"relative_pp":(rs["relative_pp"] if rs else None),
                "coin24_pct":(rs["coin_24h_pct"] if rs else None),
                "btc24_pct":(rs["btc_24h_pct"] if rs else None),
                "btc_positive":(rs["btc_up24"] if rs else None),
                "surge_at":now["t"].isoformat(),**outcome}
        for key,eligible in (("HOLD_2",True),("HOLD_RS_2",rs is not None and rs["relative_pp"]>=2),
                             ("HOLD_BTC_2",rs is not None and rs["relative_pp"]>=2 and rs["btc_up24"])):
            if eligible:events[key].append({**common,"rule":key})
    return events,watch


def _flow_verdict(comp,coverage_n,requested_n):
    coverage = coverage_n/max(1,requested_n)
    if coverage < .80:
        return {"status":"DATA_INCOMPLETE","message":"요청 기간 전체를 확보한 종목 비율이 80% 미만입니다. 판단 불가"}
    main=next(x for x in comp if x["key"]=="HOLD_RS_2")
    if main["train"]["n"]<20 or main["test"]["n"]<10:
        return {"status":"INSUFFICIENT","message":"확정 표본이 부족합니다. 이 전략을 채택하지 마세요"}
    if (main["train"]["avg_net"]>0 and main["test"]["avg_net"]>0
        and main["test_no_top2"]["avg_net"] is not None and main["test_no_top2"]["avg_net"]>0):
        return {"status":"RESEARCH_ONLY","message":"사전 기준 통과. 외부 기간/당시 종목 구성에서 재검증 전에는 실전 금지"}
    return {"status":"REJECT","message":"사전에 고정한 학습·검증 기준 미통과. 이 가설을 실전 진입으로 채택하지 않습니다"}


def flow_backtest(cfg,progress=None):
    """신규 독립 연구판. 동일 역사 구간에서 급증 즉시 vs 유지 vs 상대강도 보수적 비교."""
    pre=validate_workload(cfg,cfg.count or 500)
    if pre and "error" in pre and "초과" in pre["error"]:return pre
    if progress: progress("신규 가설·데이터 범위 확인",2,0,0,0)
    chosen,eligible=universe(cfg)
    pre=validate_workload(cfg,len(chosen))
    if pre:return pre
    if not chosen:return {"error":"유효 거래대금 조건을 통과한 코인이 없습니다"}
    need=math.ceil(cfg.days*1440/cfg.tf)+min_history(cfg)+4
    if progress:progress("BTC 상대강도 기준 수집",4,0,len(chosen),0)
    try:
        bitcoin=complete(enrich(candles("KRW-BTC",cfg.tf,need),cfg.tf),cfg.tf)
        btc_ref=flow_btc_reference(bitcoin,cfg.tf)
        if len(btc_ref)<24:return {"error":"BTC 과거 데이터 부족: 상대강도를 계산할 수 없습니다"}
    except Exception as exc:
        return {"error":f"BTC 비교 기준 수집 실패(검사 중단): {exc}"}
    start=kst_now()-timedelta(days=cfg.days)
    end=kst_now()
    cut=end-timedelta(days=cfg.days*.3)
    errors=[];coverage={};all_events={x:[] for x in ("SURGE_2","HOLD_2","HOLD_RS_2","HOLD_BTC_2")}
    order={x["market"]:i for i,x in enumerate(chosen)}
    size=4 if need>=10000 else 8 if need>=5000 else 12
    processed=0
    for begin in range(0,len(chosen),size):
        ensure_active()
        batch=chosen[begin:begin+size]
        def track(m,d,total,e):
            if progress:progress(f"과거 봉 수집 {m}",5+int(80*(processed+d)/len(chosen)),processed+d,len(chosen),len(errors)+e)
        data,errs=load_series(batch,cfg,progress=track)
        errors.extend(errs)
        for market,bars in data.items():
            ensure_active()
            rows=complete(bars,cfg.tf)
            if not rows:continue
            complete_start=rows[0]["t"]
            is_full=complete_start<=start-timedelta(minutes=cfg.tf*min_history(cfg))
            coverage[market]={"first_at":complete_start.isoformat(), "last_at":rows[-1]["t"].isoformat(),
                              "full_period":is_full,"observed":len(rows),
                              "gaps":sum(rows[i]["t"]-rows[i-1]["t"]!=timedelta(minutes=cfg.tf) for i in range(1,len(rows)))}
            variants,_=flow_events(market,rows,cfg,btc_ref,cutoff=start)
            for key,xs in variants.items():
                for x in xs:x["rank"]=order.get(market,999)
                all_events[key].extend(xs)
        processed+=len(batch)
        if progress:progress(f"거래대금·회복 신호 정리 {processed}/{len(chosen)}",5+int(88*processed/len(chosen)),processed,len(chosen),len(errors))
    if not coverage:
        return {"error":"검사 가능한 시장 데이터를 확보하지 못했습니다: "+"; ".join(errors[:2])}
    ensure_active()
    if progress:progress("포트폴리오 및 학습/검증 비교",96,len(chosen),len(chosen),len(errors))
    cmp=[];variant_trades={}
    for key,label in (("SURGE_2","급증봉 다음 시가 (비교 기준)"),
                      ("HOLD_2","횡보 유지 뒤 재상승 확인"),
                      ("HOLD_RS_2","재상승 + BTC 대비 상대강도 +2%p"),
                      ("HOLD_BTC_2","상대강도 + BTC 자체 상승")):
        selected=select_trades(all_events[key],replace(cfg,max_trades_day=2))
        st,_,split=daily_and_split(selected,cfg,cut)
        tests=[x for x in selected if x.get("status")=="CLOSED" and datetime.fromisoformat(x["entry_at"])>=cut]
        cmp.append({"key":key,"label":label,"stat":st,"train":split["train"],"test":split["test"],
                    "test_no_top2":upper_trim_stat(tests,cfg),"signals":len(all_events[key]),
                    "selected":len(selected),"unknown":sum(x.get("status")!="CLOSED" for x in selected)})
        variant_trades[key]=selected
    main=variant_trades["HOLD_RS_2"]
    st,daily,split=daily_and_split(main,cfg,cut)
    diagnostics=summarize_diagnostics(main,cfg.capital)
    full_markets={m for m,info in coverage.items() if info.get("full_period")}
    fully_selected=select_trades([e for e in all_events["HOLD_RS_2"] if e["market"] in full_markets],replace(cfg,max_trades_day=2))
    full_stat,_,full_split=daily_and_split(fully_selected,cfg,cut)
    full_sensitivity={"full_markets":len(full_markets),"requested_markets":len(chosen),
                      "stat":full_stat,"train":full_split["train"],"test":full_split["test"]}
    verdict=_flow_verdict(cmp,len(full_markets),len(chosen))
    warnings=["V3.6은 새 연구 가설이며, 이전 EMA200 고점 돌파 전략과 별도입니다.",
              "매수세란 실제 주문 흐름이 아닌 캔들 거래대금·종가 위치에 대한 근사 설명입니다.",
              "스캔 대상은 현재 거래대금 순위로 뽑으므로 과거 당시 구성과 다를 수 있습니다.",
              "봉 마감 후 다음 봉 시가 진입 가정, 수수료 편도 0.25% 기본, 슬리피지 0% 가정.",
              "24시간 종료·캔들 결측은 구분하며 미확정 손익을 성공으로 계산하지 않습니다."]
    return {"version":VERSION,"kind":"flow","markets":len(coverage),"eligible":eligible,
       "requested_markets":len(chosen),"partial_markets":sum(not v["full_period"] for v in coverage.values()),
       "signals":len(all_events["HOLD_RS_2"]),"selected":len(main),
       "unknown":sum(x.get("status")!="CLOSED" for x in main),"stat":st,"split":split,"daily":daily,
       "trades":main,"comparisons":cmp,"variant_trades":variant_trades,"verdict":verdict,
       "rule_params":FLOW_RULES,"market_stats":diagnostics["markets"],"reasons":diagnostics["reasons"],
       "mdd_krw":diagnostics["mdd_krw"],"quick_stop_45m":diagnostics["quick_stop_45m"],
       "max_lose_streak":diagnostics["max_lose_streak"],
       "coverage":coverage,"full_sensitivity":full_sensitivity,"errors":errors,"error_count":len(errors),
       "settings":{"fee_pct":cfg.fee*100,"slip_pct":0,"per_trade_krw":cfg.capital,
                   "period_days":cfg.days,"tf":cfg.tf,"count":cfg.count,"maxday":2,
                   "turnover_min_krw":cfg.turnover_min,"scanned_at_kst":kst_now().isoformat(),
                   "start_kst":start.isoformat(),"cut_kst":cut.isoformat()},
       "warnings":warnings}


def flow_scan(cfg,progress=None):
    if progress:progress("거래대금 급증 후보 스캔 준비",3,0,0,0)
    chosen,eligible=universe(cfg)
    if not chosen:return {"error":"조건을 만족하는 종목이 없습니다"}
    need=120
    try:
        btc=complete(enrich(candles("KRW-BTC",cfg.tf,need),cfg.tf),cfg.tf)
        btc_ref=flow_btc_reference(btc,cfg.tf)
    except Exception as exc:
        return {"error":f"BTC 상대강도 참고 자료를 확보하지 못했습니다: {exc}"}
    def track(m,d,total,e):
        if progress:progress(f"최근 거래대금·봉 수집: {m}",5+int(85*d/max(1,total)),d,total,e)
    data,errors=load_series(chosen,cfg,days=max(4,math.ceil(120*cfg.tf/1440)+1),progress=track)
    signals=[];watch=[]
    for j,item in enumerate(chosen):
        if j%10==0:ensure_active()
        if progress:progress(f"반등 확인: {item['market']}",91+int(8*(j+1)/len(chosen)),j+1,len(chosen),len(errors))
        rows=complete(data.get(item['market'],[]),cfg.tf)
        if not rows:continue
        variants,wait=flow_events(item['market'],rows,cfg,btc_ref,live=True)
        signals+=variants["HOLD_RS_2"]
        watch+=wait
    return {"version":VERSION,"checked":len(data),"eligible":eligible,"signals":signals,
            "watch":watch[:15],"errors":errors,"updated":kst_now().strftime("%Y-%m-%d %H:%M:%S KST"),
            "notes":["신규 연구용 후보이며 진입가·체결 성공은 보장되지 않습니다."]}

def backtest(cfg, progress=None):
    if cfg.strategy=="trap":
        return trap_backtest(cfg,progress)
    if cfg.strategy=="flow":
        return flow_backtest(cfg,progress)
    if progress:progress("기간·봉 수 검사", 1, 0, 0, 0)
    needed = math.ceil(cfg.days * 1440 / cfg.tf) + min_history(cfg) + 4
    # 검증 전에도 빠르게 거절하여 Render 과부하 방지.
    pre = validate_workload(cfg, cfg.count if cfg.count else 500)
    if pre and needed>MAX_BARS_PER_MARKET:
        return pre
    ensure_active()
    chosen,eligible=universe(cfg)
    pre=validate_workload(cfg,len(chosen))
    if pre:
        return pre
    if progress:progress("검사 마켓 확정", 5, 0, len(chosen), 0)
    if not chosen:
        return {"error":"조건을 만족하는 마켓이 없습니다."}
    btc_map=None
    if cfg.strategy=="trend":
        try:
            btc_bars=enrich(candles("KRW-BTC",cfg.tf,needed),cfg.tf)
            btc_map=btc_ok_by_time(complete(btc_bars,cfg.tf))
        except Exception as e:
            if cfg.btc:
                return {"error":f"BTC 필터 데이터 누락: {e}"}
            # 선택적 BTC 비교만 건너뛰고 기준 전략은 그대로 검사한다.
            btc_map=None
    # 장기/전체 선택 시 메모리 폭주를 막기 위해 4~12개씩 수집·계산하고 폐기.
    batch_size = 4 if needed>=12000 else (8 if needed>=6000 else 12)
    all_events, retest_events, coverage, errors=[],[],{},[]
    order={x["market"]:i for i,x in enumerate(chosen)}
    # RSI 상대강도 필터는 단일 일괄 수집으로 동시간 비교를 유지.
    if cfg.strategy=="trend" and cfg.rs:
        data,errs=load_series(chosen,cfg,progress=lambda m,d,total,e:
             progress(f"캔들 수집: {m}",5+int(65*d/max(total,1)),d,total,e) if progress else None)
        errors+=errs
        rs_map=rs_by_time({m:complete(b,cfg.tf) for m,b in data.items()},cfg)
        for idx,(m,bars) in enumerate(data.items()):
            ensure_active()
            market_retests=[]
            ev,_,q=detect_all(m,bars,cfg,btc_map if cfg.btc else None,rs_map,retest_out=market_retests)
            for x in market_retests:x["rank"]=order.get(m,999)
            retest_events.extend(market_retests)
            rows=complete(bars,cfg.tf)
            first=rows[0]["t"];last=rows[-1]["t"]
            q.update(first_at=first.isoformat(),last_at=last.isoformat(),
                     full_period=first<=kst_now()-timedelta(days=cfg.days)-timedelta(minutes=cfg.tf*min_history(cfg)))
            coverage[m]=q
            for x in ev:x["rank"]=order.get(m,999)
            all_events+=ev
            if progress:progress(f"과거 신호 계산: {m}",72+int(25*(idx+1)/max(len(data),1)),idx+1,len(data),len(errors))
    else:
        processed=0
        for i in range(0,len(chosen),batch_size):
            ensure_active()
            batch=chosen[i:i+batch_size]
            def on_load(m,d,total,e):
                if progress:
                    progress(f"캔들 수집: {m} · 묶음 {i//batch_size+1}",
                             5+int(82*(processed+d)/max(1,len(chosen))),processed+d,len(chosen),len(errors)+e)
            data,errs=load_series(batch,cfg,progress=on_load)
            errors.extend(errs)
            for m,bars in data.items():
                ensure_active()
                market_retests=[]
                ev,_,q=detect_all(m,bars,cfg,btc_map if cfg.btc else None,None,retest_out=market_retests)
                for x in market_retests:x["rank"]=order.get(m,999)
                retest_events.extend(market_retests)
                rows=complete(bars,cfg.tf)
                first=rows[0]["t"];last=rows[-1]["t"]
                q.update(first_at=first.isoformat(),last_at=last.isoformat(),
                         full_period=first<=kst_now()-timedelta(days=cfg.days)-timedelta(minutes=cfg.tf*min_history(cfg)))
                coverage[m]=q
                for x in ev:x["rank"]=order.get(m,999)
                all_events+=ev
            processed+=len(batch)
            if progress:progress(f"신호 계산 완료 {processed}/{len(chosen)}",5+int(92*processed/max(1,len(chosen))),processed,len(chosen),len(errors))
            del data
    if not coverage:
        return {"error":"백테스트 데이터를 받지 못했습니다: "+("; ".join(errors[:2]))}
    ensure_active()
    if progress:progress("시간순 거래 제한·수익률·원인 분석",98,len(chosen),len(chosen),len(errors))
    selected=select_trades(all_events,cfg)
    end_time=kst_now()
    cut_time=end_time-timedelta(days=cfg.days*.3)
    stat,daily,split=daily_and_split(selected,cfg,cut_time)
    unknown=[x for x in selected if x.get("status")!="CLOSED"]
    diagnostics=summarize_diagnostics(selected,cfg.capital)
    comparisons,variant_trades=(compare_portfolios(all_events,retest_events,cfg,cut_time,btc_map)
                                if cfg.strategy=="trend" else ([],{}))
    after_exit=time_exit_analysis(selected)
    # V3.5 진단은 기본 돌파형 최대 2회 결과만 분석. 앞선 V3.4 비교군은 원본 보존.
    baseline2=variant_trades.get("BREAKOUT_2",selected) if cfg.strategy=="trend" else selected
    gates,gate_trades=(compare_fixed_gates(all_events,cfg,cut_time,baseline2) if cfg.strategy=="trend" else ([],{}))
    diagnostic=build_trade_diagnostics(baseline2)
    preentry=win_loss_features(baseline2) if cfg.strategy=="trend" else []
    test_sample=[x for x in baseline2 if x.get("status")=="CLOSED" and datetime.fromisoformat(x["entry_at"])>=cut_time]
    robust={"overall":upper_trim_stat(baseline2,cfg),"validation":upper_trim_stat(test_sample,cfg)}
    # 일부 기간만 확보한 신규상장/누락 종목을 빼도 결론이 뒤집히는지 민감도 검사.
    if cfg.strategy=="trend":
        full_markets={m for m,q in coverage.items() if q.get("full_period")}
        only_full=select_trades([x for x in all_events if x["market"] in full_markets], replace(cfg,max_trades_day=2))
        full_stat,_,full_split=daily_and_split(only_full,cfg,cut_time)
        full_sensitivity={"full_markets":len(full_markets),"requested_markets":len(chosen),
                          "stat":full_stat,"test":full_split["test"],"train":full_split["train"]}
    else:
        full_sensitivity={}
    verdict=(research_verdict(gates,len(coverage),len(chosen),cfg) if cfg.strategy=="trend"
             else {"status":"NOT_APPLICABLE","text":"추세추종 비교에만 적용되는 판정입니다."})
    # 데이터 결손 시 표본을 허위로 확대하지 않고 누락/상장일별 기록을 표시.
    return {"version":VERSION,"kind":cfg.strategy,"markets":len(coverage),"eligible":eligible,
            "requested_markets":len(chosen),"partial_markets":sum(not c.get("full_period") for c in coverage.values()),"signals":len(all_events),"selected":len(selected),"unknown":len(unknown),
            "stat":stat,"daily":daily,"split":split,"trades":selected,
            "comparisons":comparisons,"variant_trades":variant_trades,
            "retest_signals":len(retest_events),"exit_analysis":after_exit,
            "v35_diagnostic":diagnostic,"v35_features":preentry,
            "v35_gates":gates,"v35_gate_trades":gate_trades,
            "v35_robust":robust,"v35_verdict":verdict,"v35_full_only":full_sensitivity,
            "market_stats":diagnostics["markets"],"reasons":diagnostics["reasons"],"quick_stop_45m":diagnostics["quick_stop_45m"],"mdd_krw":diagnostics["mdd_krw"],"max_lose_streak":diagnostics["max_lose_streak"],
            "coverage":coverage,"errors":errors[:20],"error_count":len(errors),
            "settings":{"fee_pct":cfg.fee*100,"slip_pct":0.0,
               "per_trade_krw":cfg.capital,"period_days":cfg.days,"maxday":cfg.max_trades_day,
               "btc":cfg.btc,"rs":cfg.rs,"tf":cfg.tf,"count":cfg.count},
            "warnings":["V3.5는 V3.4 거래 선정/청산 규칙을 유지하고 2회 기준 손익·진입 특징 및 사전 정의한 필터 3개를 따로 비교합니다. 실전 우위는 검증 전입니다.",
                        "청산 후 24시간 고점/저점/종가는 사후 관찰용입니다. 실제 체결 수익을 뜻하지 않으며 진입 선택에 사용하지 않습니다.",
                        "슬리피지 0% 가정(실제 체결 지연·호가 차이로 실전 수익은 달라질 수 있음).",
                        "현재 시점의 거래대금 상위 종목으로 과거를 검사합니다(생존·선택 편향).",
                        "전체 기간을 확보하지 못한 종목은 partial_markets에 집계됩니다. 데이터 누락은 기간 전체처럼 보이지 않도록 표시합니다.",
                        "거래가 없는 분봉은 보간하지 않으며 해당 결과는 UNKNOWN으로 분리합니다.",
                        "동일 봉에서 손절과 익절 모두 닿으면 손절부터 체결했다고 가정합니다.",
                        "봉 데이터만으로 호가·실제 체결을 재현하지 못합니다."]}


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
    need=math.ceil((age+4)*1440/cfg.tf)+min_history(cfg)
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



# ================= V3.7 신규 연구 가설: 가짜 하락 이탈 -> 지지선 회복 =================
# V3.5/3.6 실패 전략은 별도 보관. 미래봉은 거래 결과 계산에만 사용.
TRAP_RULES = {
    "support_lookback_bars": 20, "min_sweep_atr": 0.10,
    "max_sweep_atr": 2.0, "reclaim_bars_after_sweep": 2,
    "reclaim_close_above_support_pct": 0.001,
    "retest_wait_bars": 3, "retest_touch_atr": 0.35,
    "breakout_wait_bars": 3, "stop_buffer_atr": 0.20,
    "min_risk_pct": 0.40, "max_risk_pct": 1.50,
    "takeprofit1_r": 1.5, "takeprofit2_r": 2.0,
    "holding_hours": [4, 8, 12], "max_daily_entries": 2,
}
TRAP_LABELS = {"A":"첫 회복", "B":"회복 뒤 지지 재확인", "C":"재확인 뒤 단기 고점 돌파"}


def trap_hour_context(bars, signal_ix, tf):
    """15분봉 4개가 완전히 있는 '닫힌 1시간봉'만 참조한다. 미래 관측 불가.
    60분봉 선택 시 최근 완료 1시간봉 7개를 그대로 참조한다.
    """
    if tf == 60:
        recent = bars[max(0,signal_ix-6):signal_ix+1]
        if len(recent)<7 or any(recent[j]["t"]-recent[j-1]["t"]!=timedelta(hours=1)
                                  for j in range(1,len(recent))):
            return {"state":"UNKNOWN", "hourly_close_change_pct":None}
        change=100*(recent[-1]["c"]/recent[0]["c"]-1)
    elif tf == 15:
        groups={}
        signal_end=bars[signal_ix]["t"]+timedelta(minutes=15)
        for b in bars[max(0,signal_ix-4*9):signal_ix+1]:
            start=b["t"].replace(minute=0,second=0,microsecond=0)
            groups.setdefault(start,[]).append(b)
        finished=[]
        for t,group in sorted(groups.items()):
            group.sort(key=lambda x:x["t"])
            if t+timedelta(hours=1)>signal_end:continue
            if [g["t"] for g in group] != [t+timedelta(minutes=15*j) for j in range(4)]:continue
            finished.append(group[-1]["c"])
        if len(finished)<7:
            return {"state":"UNKNOWN", "hourly_close_change_pct":None}
        change=100*(finished[-1]/finished[-7]-1)
    else:
        return {"state":"UNKNOWN", "hourly_close_change_pct":None}
    return {"state":"UP" if change>0.2 else "DOWN" if change<-.2 else "SIDE",
            "hourly_close_change_pct":round(change,3)}


def trap_sweep(bars,k,cfg):
    """지지선은 이탈봉 k 이전 20개 완료봉의 저가만으로 계산."""
    n=TRAP_RULES["support_lookback_bars"]
    if k<n+1:return None
    b=bars[k];atr=b.get("atr")
    if b.get("run",0)<max(40,n+1) or not atr or atr<=0:return None
    old=bars[k-n:k]
    support=min(x["l"] for x in old)
    gap=support-b["l"]
    if gap<TRAP_RULES["min_sweep_atr"]*atr or gap>TRAP_RULES["max_sweep_atr"]*atr:
        return None
    # 과거 완료봉 기준 일간 거래대금. 거래 없는 봉을 만들어 합산하지 않는다.
    if b.get("turn24") is None or b["turn24"]<cfg.turnover_min:return None
    return {"support":support,"sweep_low":b["l"],"atr":atr,
            "sweep_depth_atr":round(gap/atr,3),"historical_24h_turn_krw":int(b["turn24"])}


def trap_confirmations(bars,k,sweep,tf):
    """A는 첫 지지선 회복, B는 완료봉의 재접근-반등, C는 이미 확정된 고점 돌파."""
    step=timedelta(minutes=tf)
    support=sweep["support"];atr=sweep["atr"];lowest=sweep["sweep_low"]
    a=None
    for r in range(k,min(len(bars),k+TRAP_RULES["reclaim_bars_after_sweep"]+1)):
        if r>k and bars[r]["t"]-bars[r-1]["t"]!=step:break
        br=bars[r]
        if r>k and br["l"]<lowest:break
        if br["c"]>support*(1+TRAP_RULES["reclaim_close_above_support_pct"]) and br["c"]>=br["o"]:
            a=r;break
    if a is None:return {}
    result={"A":a}
    b_idx=None
    for j in range(a+1,min(len(bars),a+TRAP_RULES["retest_wait_bars"]+1)):
        if bars[j]["t"]-bars[j-1]["t"]!=step:break
        x=bars[j]
        if x["l"]<lowest or x["c"]<support*.998:break
        # 지지선 근처를 재방문한 '완료봉'이고 지지선 위에서 양봉 마감.
        if (x["l"]<=support+TRAP_RULES["retest_touch_atr"]*atr
                and x["c"]>support and x["c"]>x["o"]):
            b_idx=j;result["B"]=j;break
    if b_idx is None:return result
    pivot=max(x["h"] for x in bars[k:b_idx+1])
    for j in range(b_idx+1,min(len(bars),b_idx+TRAP_RULES["breakout_wait_bars"]+1)):
        if bars[j]["t"]-bars[j-1]["t"]!=step:break
        x=bars[j]
        if x["l"]<lowest or x["c"]<support*.998:break
        if x["c"]>pivot and x["c"]>x["o"]:
            result["C"]=j;break
    return result


def trap_simulate(bars, i, stop, cfg, hold_hours):
    """절반 1.5R, 나머지 2R. 같은 봉 손절/익절 동시 접촉 시 손절 먼저.
    수수료는 부분 체결 전체에 동일하게 편도 적용. 빠진 봉은 UNKNOWN.
    """
    if i>=len(bars):return {"status":"UNKNOWN","why":"다음 봉 시가 없음"}
    entry=bars[i]["o"]
    if not (0<stop<entry):return None
    risk=entry-stop;rp=100*risk/entry
    if not (TRAP_RULES["min_risk_pct"]<=rp<=TRAP_RULES["max_risk_pct"]):return None
    tp1=entry+1.5*risk;tp2=entry+2*risk
    limit=int(hold_hours*60/cfg.tf)
    sold=[];remaining=1.0;stop_live=stop;high=entry;low=entry
    end=min(len(bars),i+limit);step=timedelta(minutes=cfg.tf)
    def finish(j,px,why):
        sells=sold+[(remaining,px)] if remaining>0 else sold
        gross,net=calc_outcome(entry,sells,cfg.fee)
        return {"status":"CLOSED","entry_at":bars[i]["t"].isoformat(),
                "exit_at":(bars[j]["t"]+step).isoformat(),"price":entry,
                "stop":stop,"tp":tp2,"tp1":tp1,"risk_pct":round(rp,3),
                "gross_pct":round(gross*100,4),"net_pct":round(net*100,4),
                "exit_price":px,"why":why,"mfe_pct":round(100*(high/entry-1),3),
                "mae_pct":round(100*(low/entry-1),3),"hold_hours":hold_hours}
    for j in range(i,end):
        if j>i and bars[j]["t"]-bars[j-1]["t"]!=step:
            return {"status":"UNKNOWN","entry_at":bars[i]["t"].isoformat(),"why":"보유 중 봉 누락","hold_hours":hold_hours}
        b=bars[j];high=max(high,b["h"]);low=min(low,b["l"])
        if b["l"]<=stop_live:
            return finish(j,min(stop_live,b["o"]),"구조 손절" if not sold else "1차익절 후 손절")
        if remaining==1 and b["h"]>=tp1:
            sold.append((.5,max(tp1,b["o"])));remaining=.5
            # 이미 1차 목표 체결된 봉에서는 손절선 즉시 변경 X. 다음 봉에만 적용.
        if remaining==.5 and b["h"]>=tp2:
            return finish(j,max(tp2,b["o"]),"2차 목표")
        if sold and remaining==.5:
            stop_live=max(stop_live,entry*(1+cfg.fee)/max(.000001,1-cfg.fee))
    if i+limit>len(bars):
        return {"status":"UNKNOWN","entry_at":bars[i]["t"].isoformat(),"why":"보유시간 미래봉 부족","hold_hours":hold_hours}
    return finish(end-1,bars[end-1]["c"],f"{hold_hours}시간 종료")


def trap_events(market,bars,cfg,cutoff=None,live=False):
    """A/B/C × 4/8/12h 비교를 한 번의 sweep 역사에서 동시 생성."""
    keys=[f"{stage}_{h}H" for stage in "ABC" for h in (4,8,12)]
    variants={k:[] for k in keys};signals=[];watch=[]
    n=len(bars);step=timedelta(minutes=cfg.tf)
    for k in range(95,n):
        if k%1200==0:ensure_active()
        sw=trap_sweep(bars,k,cfg)
        if not sw:continue
        confirmations=trap_confirmations(bars,k,sw,cfg.tf)
        if live and not confirmations and k>=n-3:
            watch.append({"market":market,"status":"지지선 재회복 대기", "box":sw["support"],"price":bars[-1]["c"]})
        for stage,j in confirmations.items():
            confirm=bars[j];stop=sw["sweep_low"]-TRAP_RULES["stop_buffer_atr"]*sw["atr"]
            if live:
                if j!=n-1:continue
                price=confirm["c"]
                rp=100*(price-stop)/price if price>0 else 0
                if stop<=0 or not (TRAP_RULES["min_risk_pct"]<=rp<=TRAP_RULES["max_risk_pct"]):continue
                hourly=trap_hour_context(bars,j,cfg.tf)
                rv=confirm["v"]/max(confirm.get("volavg") or 1,1e-12)
                signals.append({"market":market,"strategy":"trap","rule":stage,
                    "signal_at":confirm["t"].isoformat(),"price":price,"stop":stop,
                    "tp":price+2*(price-stop),"risk_pct":round(rp,3),
                    "turnover_24h":sw["historical_24h_turn_krw"],
                    "context":{"box":sw["support"],"volume_ratio":round(rv,2),"hourly":hourly},
                    "why":f"{TRAP_LABELS[stage]} 확인 완료. 이전 20봉 지지선 {sw['support']:,.5g}원을 이탈한 뒤 회복했어요. 1시간봉은 {hourly['state']}({hourly['hourly_close_change_pct']}%). 실제 진입은 다음 봉 시가가 확인된 후 검토하세요.",
                    "score":{"structure":None,"timing":None,"trend":None,"setup":None,"entry":None,"safety":None,"volume":None,"total":None,"pressure":None}})
                continue
            i=j+1
            if i>=n or bars[i]["t"]-confirm["t"]!=step:continue
            if cutoff is not None and bars[i]["t"]<cutoff:continue
            ctx=trap_hour_context(bars,j,cfg.tf)
            rv=confirm["v"]/max(confirm.get("volavg") or 1,1e-12)
            for hours in (4,8,12):
                outcome=trap_simulate(bars,i,stop,cfg,hours)
                if outcome is None:continue
                ev={"market":market,"strategy":"trap","rule":f"{stage}_{hours}H",
                    "signal_at":confirm["t"].isoformat(),"sweep_at":bars[k]["t"].isoformat(),
                    "rank":999,"support":sw["support"],"sweep_low":sw["sweep_low"],
                    "sweep_depth_atr":sw["sweep_depth_atr"],"preentry_hourly":ctx["state"],
                    "hourly_change_pct":ctx["hourly_close_change_pct"],
                    "volume_ratio":round(rv,3),"historical_24h_turn_krw":sw["historical_24h_turn_krw"],
                    **outcome}
                variants[f"{stage}_{hours}H"].append(ev)
    return variants,signals,watch


def _trap_verdict(comp,full_n,requested):
    full_ratio=full_n/max(1,requested)
    if full_ratio<.80:
        return {"status":"DATA_INCOMPLETE","message":"전체 기간 확보 종목이 80% 미만: 수익성 판단 보류"}
    # 미리 고정한 세 전략 × 세 보유기간. 최소 표본을 통과한 경우에만 합격 가능.
    eligible=[]
    for row in comp:
        train=row["train"];test=row["test"];trim=row["test_no_top2"]
        if train["n"]>=20 and test["n"]>=30 and train["avg_net"] is not None and test["avg_net"] is not None:
            if train["avg_net"]>0 and test["avg_net"]>0 and trim["avg_net"] is not None and trim["avg_net"]>0:
                eligible.append(row["key"])
    if eligible:
        return {"status":"RESEARCH_ONLY", "passing_variants":eligible,
                "message":"사전 기준 통과 후보 존재. 같은 데이터 9개 조건 비교로 생긴 선택 편향이 있으므로 별도 기간에서 재검증 필요"}
    enough=any(x["train"]["n"]>=20 and x["test"]["n"]>=30 for x in comp)
    return ({"status":"REJECT" if enough else "INSUFFICIENT",
             "message":"검증 기준 미달: 전략 중단 권고" if enough else "학습 20건·검증 30건 이상 표본 부족. 실전 금지"})


def trap_backtest(cfg,progress=None):
    """V3.7 독립 연구판. 기존 실패 전략과 코드 분리, 동일 표본의 9개 방식 비교."""
    chosen,eligible=universe(cfg)
    if not chosen:return {"error":"검사 가능한 KRW 코인이 없어요"}
    pre=validate_workload(cfg,len(chosen))
    if pre:return pre
    start=kst_now()-timedelta(days=cfg.days)
    end=kst_now();cut=end-timedelta(days=cfg.days*.3)
    need=math.ceil(cfg.days*1440/cfg.tf)+min_history(cfg)+4
    keys=[f"{x}_{h}H" for x in "ABC" for h in (4,8,12)]
    all_events={key:[] for key in keys};coverage={};errors=[]
    order={x["market"]:i for i,x in enumerate(chosen)}
    size=4 if need>=10000 else 8 if need>=5000 else 12
    processed=0
    for begin in range(0,len(chosen),size):
        ensure_active();batch=chosen[begin:begin+size]
        def track(m,d,total,e):
            if progress:progress(f"V3.7 과거 봉 수집 {m}",5+int(80*(processed+d)/len(chosen)),processed+d,len(chosen),len(errors)+e)
        data,errs=load_series(batch,cfg,progress=track);errors.extend(errs)
        for market,bars in data.items():
            ensure_active();rows=complete(bars,cfg.tf)
            if len(rows)<min_history(cfg)+5:continue
            first=rows[0]["t"]
            full=first<=start-timedelta(minutes=cfg.tf*min_history(cfg))
            gaps=sum(rows[i]["t"]-rows[i-1]["t"]!=timedelta(minutes=cfg.tf) for i in range(1,len(rows)))
            coverage[market]={"first_at":first.isoformat(),"last_at":rows[-1]["t"].isoformat(),
                "observed":len(rows),"gaps":gaps,"full_period":full}
            variants,_,_=trap_events(market,rows,cfg,cutoff=start)
            for key,xs in variants.items():
                for x in xs:x["rank"]=order.get(market,999)
                all_events[key].extend(xs)
        processed+=len(batch)
        if progress:progress(f"V3.7 신호 계산 {processed}/{len(chosen)}",5+int(88*processed/len(chosen)),processed,len(chosen),len(errors))
    if not coverage:return {"error":"과거 캔들을 수집할 수 없었어요: "+"; ".join(errors[:2])}
    ensure_active()
    if progress:progress("같은 데이터 9개 비교·CSV 집계",97,len(chosen),len(chosen),len(errors))
    comparison=[];variant_trades={}
    for key in keys:
        chosen_trades=select_trades(all_events[key],replace(cfg,max_trades_day=2,max_positions=1))
        stats,_,split=daily_and_split(chosen_trades,cfg,cut)
        testing=[t for t in chosen_trades if t.get("status")=="CLOSED" and datetime.fromisoformat(t["entry_at"])>=cut]
        stage,h=key.split("_")
        comparison.append({"key":key,"label":f"{TRAP_LABELS[stage]} · {h.lower()} 보유",
            "selected":len(chosen_trades),"unknown":sum(x.get("status")!="CLOSED" for x in chosen_trades),
            "signals":len(all_events[key]),"stat":stats,"train":split["train"],"test":split["test"],
            "test_no_top2":upper_trim_stat(testing,cfg)})
        variant_trades[key]=chosen_trades
    main=variant_trades["B_8H"]
    stat,daily,split=daily_and_split(main,cfg,cut)
    diag=summarize_diagnostics(main,cfg.capital)
    full_markets={m for m,q in coverage.items() if q["full_period"]}
    fully=select_trades([x for x in all_events["B_8H"] if x["market"] in full_markets],replace(cfg,max_trades_day=2,max_positions=1))
    full_stat,_,full_split=daily_and_split(fully,cfg,cut)
    sensitivity={"full_markets":len(full_markets),"requested_markets":len(chosen),
        "stat":full_stat,"train":full_split["train"],"test":full_split["test"]}
    verdict=_trap_verdict(comparison,len(full_markets),len(chosen))
    return {"version":VERSION,"kind":"trap","markets":len(coverage),"eligible":eligible,
        "requested_markets":len(chosen),"partial_markets":sum(not x["full_period"] for x in coverage.values()),
        "signals":len(all_events["B_8H"]),"selected":len(main),
        "unknown":sum(x.get("status")!="CLOSED" for x in main),
        "stat":stat,"split":split,"daily":daily,"trades":main,"comparisons":comparison,
        "variant_trades":variant_trades,"rule_params":TRAP_RULES,"verdict":verdict,
        "market_stats":diag["markets"],"reasons":diag["reasons"],"mdd_krw":diag["mdd_krw"],
        "quick_stop_45m":diag["quick_stop_45m"],"max_lose_streak":diag["max_lose_streak"],
        "coverage":coverage,"full_sensitivity":sensitivity,"errors":errors,"error_count":len(errors),
        "settings":{"fee_pct":cfg.fee*100,"slip_pct":0,"per_trade_krw":cfg.capital,
            "period_days":cfg.days,"tf":cfg.tf,"count":cfg.count,"maxday":2,
            "turnover_min_krw":cfg.turnover_min,"scanned_at_kst":kst_now().isoformat(),
            "start_kst":start.isoformat(),"cut_kst":cut.isoformat()},
        "warnings":["V3.7 가짜 지지선 이탈 후 회복 A/B/C 독립 연구. 미검증 가설이며 매수 권유 아님.",
            "15분봉은 끝난 4개 봉으로 구성한 1시간봉만 진입 전 참고값으로 표시. 상승장 필터로 쓰지 않음.",
            "1차 1.5R 절반, 2차 2R 나머지, 첫 익절 뒤 손절선은 다음 봉부터 순본절로 갱신.",
            "수수료 편도 0.25% 기본, 슬리피지 0% 이상적 체결 가정. 실거래와 다를 수 있음.",
            "현재 거래대금 상위 종목으로 과거를 검사하여 시점별 종목 선정 편향 있음.",
            "빈 분봉 임의 보간 금지, 부족한 봉은 UNKNOWN. 손절·익절 같은 봉은 손절 우선.",
            "9개 비교군에서 가장 좋은 성과만 사후 선택하면 선택 편향 발생. 통과해도 외부 재검증 필요."]}


def trap_scan(cfg,progress=None):
    chosen,eligible=universe(cfg)
    if not chosen:return {"error":"거래대금 조건에 맞는 KRW 마켓이 없어요"}
    if progress:progress("V3.7 후보 스캔 준비",2,0,len(chosen),0)
    def on_load(m,d,total,e):
        if progress:progress(f"최근 캔들 수집 {m}",5+int(88*d/len(chosen)),d,len(chosen),e)
    data,errors=load_series(chosen,cfg,days=max(3,math.ceil(190*cfg.tf/1440)+1),progress=on_load)
    signals=[];watch=[]
    for z,item in enumerate(chosen):
        ensure_active();market=item["market"]
        rows=complete(data.get(market,[]),cfg.tf)
        if not rows:continue
        _,found,waiting=trap_events(market,rows,cfg,live=True)
        signals.extend(found);watch.extend(waiting)
        if progress:progress(f"지지선 회복 확인 {z+1}/{len(chosen)}",94+int(5*(z+1)/len(chosen)),z+1,len(chosen),len(errors))
    order={"C":0,"B":1,"A":2}
    signals.sort(key=lambda x:(order.get(x["rule"],3),-x["turnover_24h"]))
    return {"version":VERSION,"checked":len(data),"eligible":eligible,
        "signals":signals,"watch":watch[:15],"errors":errors[:20],
        "updated":kst_now().strftime("%Y-%m-%d %H:%M:%S KST"),
        "notes":["시그널은 완성된 봉에서 확인. 다음 봉 체결가는 아직 미확정입니다."]}


# ---------------- V3.8: 상승 전조 탐색 연구 (거래/주문 신호 없음) ----------------
# 미리 확정한 4개 조건. 미래 라벨은 선별/조건에 절대 사용하지 않음.
PRECURSOR_RULES = {
    'BASE': '조건 없는 전체 관측',
    'TURNOVER_2X': '최근 완료봉 거래대금 ≥ 앞 20봉 평균 2배',
    'MOMENTUM_24H': '관측 전 24시간 종가 수익률 ≥ +2%',
    'NEAR_HIGH': '관측 전 24시간 고점 대비 종가 거리 ≤ 2%',
    'TURN_AND_HIGH': '거래대금 2배 + 이전 고점 2% 이내',
}
PRECURSOR_METRICS = ('up5_6h','up10_6h','up5_12h','up10_12h','up5_24h','up10_24h')
PRECURSOR_COLUMNS = [
    'row_type','version','market','at_kst','segment','status','reason','rule','rule_label',
    'n','up5_n','up10_n','up5_rate_pct','up10_rate_pct','baseline_up5_rate_pct','lift_pp',
    'train_n','test_n','available_n','train_rate','test_rate','test_baseline_rate','test_lift_pp',
    'case_count','feature_mean','up5_6h','up10_6h','up5_12h','up10_12h','up5_24h','up10_24h',
    'past_bar_pct','past_6h_pct','past_24h_pct','turnover_ratio','turnover_24h_krw',
    'atr_pct','distance_prior_high_pct','ema50_gap_pct','future_high_6h_pct',
    'future_high_12h_pct','future_high_24h_pct','anchor_close',
    'requested_days','tf_minutes','market_count_setting','requested_markets','sampling_notes',
    'checked_markets','full_markets','partial_markets','error_count','invalid_windows',
    'skipped_liquidity','fee_side_pct','slippage_pct','train_cut_kst','period_start_kst',
    'period_end_kst','sampling','target','market_rank_today',
    'first_at','last_at','observed_bars','gap_count','full_period',
    'verdict','message','warning','error',
]


def precursor_anchor(bars, i, tf, start, end, split):
    """매일 KST 00:00에 끝나는 하나의 봉만 채택. 미래 24h는 라벨 산출만 사용."""
    r = bars[i]
    at = r['t'] + timedelta(minutes=tf)
    if not (start <= at < end and at.hour == 0 and at.minute == 0):
        return None, 'NOT_ANCHOR'
    history_n = 1440 // tf
    future_n = 1440 // tf
    if i < max(20, history_n) or i + future_n >= len(bars):
        return None, 'MISSING_WINDOW'
    # 전후 자료 모두 완전한 연속 봉인지 확인. 결측 봉을 보간하지 않는다.
    first=i-max(20,history_n)
    last=i+future_n
    for k in range(first+1,last+1):
        if bars[k]['t']-bars[k-1]['t']!=timedelta(minutes=tf):
            return None,'GAP_IN_WINDOW'
    if at < split and at + timedelta(hours=24) > split:
        # 학습 라벨 관측이 검증 구간으로 넘어가는 하루는 버림(24h embargo).
        return None,'EMBARGO'
    if at + timedelta(hours=24)>end:
        return None,'MISSING_FUTURE'
    cur = float(r['c'])
    if cur <= 0: return None,'BAD_PRICE'
    prev=bars[i-history_n]
    if prev['c']<=0: return None,'BAD_PRICE'
    prior_high=max(x['h'] for x in bars[i-history_n:i])
    base_turn=r.get('turnavg20')
    ratio=round(float(r['turn'])/base_turn,4) if base_turn and base_turn>0 else None
    past_24=(cur/prev['c']-1)*100
    past_6=(cur/bars[i-6*60//tf]['c']-1)*100 if 360%tf==0 and i>=360//tf else None
    # horizon 고점은 오직 라벨 산출용. 조건/정렬에는 사용하지 않음.
    highs={}
    labels={}
    for hours in (6,12,24):
        # 4h에서는 6h 창을 정확하게 측정할 수 없으므로 6h 라벨은 UNKNOWN.
        n=hours*60//tf if hours*60%tf==0 else None
        maxpct=(max(x['h'] for x in bars[i+1:i+n+1])/cur-1)*100 if n else None
        highs[hours]=maxpct
        for threshold in (5,10):
            labels[f'up{threshold}_{hours}h']=int(maxpct>=threshold) if maxpct is not None else None
    distance=(prior_high/cur-1)*100
    obj={
        'market':None,'at_kst':at.isoformat(),'segment':'train' if at<split else 'test',
        'anchor_close':round(cur,8),'past_bar_pct':round((cur/bars[i-1]['c']-1)*100,4),
        'past_6h_pct':round(past_6,4) if past_6 is not None else None,
        'past_24h_pct':round(past_24,4),
        'turnover_ratio':ratio,'turnover_24h_krw':round(float(r.get('turn24') or 0)),
        'atr_pct':round((r['atr']/cur)*100,4) if r.get('atr') is not None else None,
        'distance_prior_high_pct':round(distance,4),
        'ema50_gap_pct':round((cur/r['ema50']-1)*100,4) if r.get('ema50') else None,
        'future_high_6h_pct':round(highs[6],4) if highs[6] is not None else None,
        'future_high_12h_pct':round(highs[12],4) if highs[12] is not None else None,
        'future_high_24h_pct':round(highs[24],4) if highs[24] is not None else None,
        **labels,
    }
    obj['passed']={
        'BASE':True,
        'TURNOVER_2X':ratio is not None and ratio>=2,
        'MOMENTUM_24H':past_24>=2,
        'NEAR_HIGH':distance<=2,
        'TURN_AND_HIGH':ratio is not None and ratio>=2 and distance<=2,
    }
    return obj,None


def _precursor_empty():
    return {'n':0,**{k:0 for k in PRECURSOR_METRICS},'avail_n':{k:0 for k in PRECURSOR_METRICS},'feat_sum':{'past_24h_pct':0.0,'turnover_ratio':0.0,'atr_pct':0.0,'distance_prior_high_pct':0.0},'feat_n':{'past_24h_pct':0,'turnover_ratio':0,'atr_pct':0,'distance_prior_high_pct':0}}


def _precursor_add(bucket,obj):
    bucket['n']+=1
    for k in PRECURSOR_METRICS:
        if obj.get(k) is not None:
            bucket[k]+=int(obj[k]);bucket['avail_n'][k]+=1
    for k in bucket['feat_sum']:
        if obj.get(k) is not None:
            bucket['feat_sum'][k]+=float(obj[k]);bucket['feat_n'][k]+=1


def _precursor_rate(bucket,k='up5_24h'):
    return round(100*bucket[k]/bucket['avail_n'][k],2) if bucket['avail_n'][k] else None


def precursor_verdict(rows,full,requested):
    if full/max(1,requested)<.80:
        return {'status':'DATA_INCOMPLETE','message':'전 기간 연속 데이터 확보율이 80% 미만입니다. 채택/폐기 판단 보류.'}
    base_tr=next((x for x in rows if x['rule']=='BASE' and x['segment']=='train'),None)
    base_te=next((x for x in rows if x['rule']=='BASE' and x['segment']=='test'),None)
    if not base_tr or not base_te or min(base_tr['n'],base_te['n'])<100 or min(base_tr['up5_n'],base_te['up5_n'])<10:
        return {'status':'INSUFFICIENT','message':'학습/검증 각 100개 관측과 +5% 상승 10개가 없어 판정 보류.'}
    passed=[]
    for key in PRECURSOR_RULES:
        if key=='BASE':continue
        a=next(x for x in rows if x['rule']==key and x['segment']=='train')
        b=next(x for x in rows if x['rule']==key and x['segment']=='test')
        # 사전 고정: 양쪽 표본 각 30건, 양쪽 기본율보다 5%p 이상 높을 때만 후보.
        if a['n']>=30 and b['n']>=30 and a['lift_pp'] is not None and b['lift_pp'] is not None and a['lift_pp']>=5 and b['lift_pp']>=5:
            passed.append(key)
    if passed:
        return {'status':'CANDIDATE_ONLY','passing_rules':passed,'message':'양쪽 구간 기본률 대비 +5%p 이상인 관측 특징이 있습니다. 다중 비교/현재 종목선정 편향이 남아 독립 기간 재검증 필요. BUY 신호 아님.'}
    return {'status':'NO_SIGNAL','message':'사전 고정 4개 특징 중 재현성 있는 +5%p 개선을 확인하지 못했습니다. 매매 전략으로 만들지 않습니다.'}


def precursor_backtest(cfg,progress=None):
    """수익률 백테스트가 아닌 상승 전 특징 비교. 서버 디스크에 관측 CSV 저장."""
    if cfg.tf not in (60,240):return {'error':'상승 전조 탐색은 60분/240분봉만 지원해요.'}
    # 실시간 24h 거래대금은 종목 정렬에만 사용; 과거 관측 채택은 당시 24h 거래대금으로 재검사.
    markets,eligible=universe(replace(cfg,turnover_min=0))
    if not markets:return {'error':'빗썸 KRW 분석 대상이 없습니다.'}
    workload=validate_workload(cfg,len(markets))
    if workload:return workload
    end=kst_now()
    start=end-timedelta(days=cfg.days)
    split=start+timedelta(days=cfg.days*.70)
    need=math.ceil(cfg.days*1440/cfg.tf)+min_history(cfg)+4
    buckets={(r,g):_precursor_empty() for r in PRECURSOR_RULES for g in ('train','test')}
    groups={(g,p):_precursor_empty() for g in ('train','test') for p in ('up','not_up')}
    coverage={};errs=[];market_stats=[];byday={};counts={'valid':0,'rejected_liquidity':0,'invalid_windows':0,'embargo':0,'pending':0}
    obs_path=Path(tempfile.gettempdir())/('BITHUMB_V3_8_observations_'+uuid.uuid4().hex[:10]+'.csv')
    target_path=Path(tempfile.gettempdir())/('BITHUMB_V3_8_ALL_RESULTS_'+cfg.tf.__str__()+'m_'+str(cfg.days)+'d_'+uuid.uuid4().hex[:8]+'.csv')
    ranks={item['market']:i+1 for i,item in enumerate(markets)}
    batch_size=4 if need>=9000 else 8 if need>=4000 else 12
    handled=0
    try:
        with obs_path.open('w',encoding='utf-8-sig',newline='') as tmpf:
            obwriter=csv.DictWriter(tmpf,PRECURSOR_COLUMNS,extrasaction='ignore');obwriter.writeheader()
            for base in range(0,len(markets),batch_size):
                ensure_active();batch=markets[base:base+batch_size]
                def _prog(m,d,t,e):
                    if progress:progress(f'V3.8 과거 봉 수집 {m}',5+int(84*(handled+d)/len(markets)),handled+d,len(markets),len(errs)+e)
                data,batcherrors=load_series(batch,cfg,progress=_prog)
                errs.extend(batcherrors)
                for market,bars in data.items():
                    ensure_active();rows=complete(bars,cfg.tf)
                    if len(rows)<min_history(cfg)+30:continue
                    observed_start=rows[0]['t'];observed_end=rows[-1]['t']+timedelta(minutes=cfg.tf)
                    gaps=sum(rows[i]['t']-rows[i-1]['t']!=timedelta(minutes=cfg.tf) for i in range(1,len(rows)))
                    full=(observed_start<=start-timedelta(minutes=cfg.tf*(min_history(cfg)-2))
                          and observed_end>=end-timedelta(minutes=cfg.tf)*2 and gaps==0)
                    market_n=market_up=0
                    for i in range(min_history(cfg),len(rows)):
                        if i%128==0:ensure_active()
                        r=rows[i];at=r['t']+timedelta(minutes=cfg.tf)
                        if not (start<=at<end and at.hour==0 and at.minute==0):continue
                        obj,why=precursor_anchor(rows,i,cfg.tf,start,end,split)
                        if why:
                            if why=='EMBARGO':counts['embargo']+=1
                            elif why=='MISSING_FUTURE':counts['pending']+=1
                            else:counts['invalid_windows']+=1
                            continue
                        if not obj.get('turnover_24h_krw') or obj['turnover_24h_krw']<cfg.turnover_min:
                            counts['rejected_liquidity']+=1;continue
                        counts['valid']+=1;market_n+=1;market_up+=obj['up5_24h']
                        obj['market']=market;obj['row_type']='OBSERVATION';obj['version']=VERSION
                        obj['market_rank_today']=ranks[market]
                        obwriter.writerow({k:v for k,v in obj.items() if k!='passed'})
                        for rule,yes in obj['passed'].items():
                            if yes:_precursor_add(buckets[(rule,obj['segment'])],obj)
                        _precursor_add(groups[(obj['segment'],'up' if obj['up5_24h'] else 'not_up')],obj)
                        day=obj['at_kst'][:10]
                        d=byday.setdefault(day,{'n':0,'up5_n':0,'up10_n':0})
                        d['n']+=1;d['up5_n']+=obj['up5_24h'];d['up10_n']+=obj['up10_24h']
                    coverage[market]={'market':market,'first_at':observed_start.isoformat(),'last_at':observed_end.isoformat(),
                         'observed_bars':len(rows),'gap_count':gaps,'full_period':bool(full),'case_count':market_n,
                         'up5_n':market_up}
                    market_stats.append({'market':market,'n':market_n,'up5_n':market_up,
                         'up5_rate_pct':round(100*market_up/market_n,2) if market_n else None})
                handled+=len(batch)
                if progress:progress(f'V3.8 관측 집계 {handled}/{len(markets)}',5+int(87*handled/len(markets)),handled,len(markets),len(errs))
        full_n=sum(x['full_period'] for x in coverage.values())
        base={seg:buckets[('BASE',seg)] for seg in ('train','test')}
        metrics=[]
        for rule,label in PRECURSOR_RULES.items():
            for seg in ('train','test'):
                b=buckets[(rule,seg)];br=base[seg]
                current=_precursor_rate(b);baseline=_precursor_rate(br)
                metrics.append({'rule':rule,'rule_label':label,'segment':seg,'n':b['n'],
                    'up5_n':b['up5_24h'],'up10_n':b['up10_24h'],'up5_rate_pct':current,
                    'up10_rate_pct':_precursor_rate(b,'up10_24h'),
                    'baseline_up5_rate_pct':baseline,
                    'lift_pp':round(current-baseline,2) if current is not None and baseline is not None else None,
                    **{k:_precursor_rate(b,k) for k in PRECURSOR_METRICS}})
        feats=[]
        for (seg,p),g in groups.items():
            for field in g['feat_sum']:
                feats.append({'segment':seg,'status':p,'rule':field,'case_count':g['feat_n'][field],
                     'value':round(g['feat_sum'][field]/g['feat_n'][field],4) if g['feat_n'][field] else None})
        verdict=precursor_verdict(metrics,full_n,len(markets))
        error_n=len(errs)
        settings={'version':VERSION,'kind':'precursor','requested_days':cfg.days,'tf_minutes':cfg.tf,
             'market_count_setting':cfg.count,'requested_markets':len(markets),'checked_markets':len(coverage),
             'full_markets':full_n,'partial_markets':len(coverage)-full_n,
             'error_count':error_n,'invalid_windows':counts['invalid_windows'],
             'skipped_liquidity':counts['rejected_liquidity'], 'fee_side_pct':round(cfg.fee*100,4),
             'slippage_pct':0,'train_cut_kst':split.isoformat(),'period_start_kst':start.isoformat(),
             'period_end_kst':end.isoformat(), 'sampling':'KST midnight once per day per coin',
             'target':'future 6/12/24h intrabar high >= anchor close*(1+5% or 10%)',
             'sampling_notes':'Labels are lookahead observations, not executable profits'}
        notes=[
            '이번 연구는 상승 전 특징 탐색 전용. BUY, 손절/익절, 매매 수익률은 계산하지 않음.',
            '한 코인 하루 KST 자정 시점에 1회 관측하며 미래 24h 자료 확보된 경우만 라벨화.',
            '학습 관측 시각 뒤 24h가 검증 구간으로 넘어가는 경우는 경계 누출 방지 위해 제외.',
            '현재 거래대금 상위 40·80 종목은 과거 선정 편향이 남음. 전체 모드는 현재 상장된 전체 KRW 종목 기준.',
            '거래대금 하한 5억은 당시 24h 봉 누적거래대금으로 적용. 현재 거래대금 하한으로 과거 관측을 거르지 않음.',
            '미래 최대 고가 +5%/+10%는 실현 가능 수익률 또는 매수 성공률을 뜻하지 않음.',
            '봉 누락·시점 부족은 샘플에서 제외. 4h에서는 6h 목표 관측 불가로 UNKNOWN 기록.',
            '사전 고정 비교 규칙 4개 외 검증 기준을 임의 조정하지 않으며, 통과해도 독립 데이터 재검증 필요.',
        ]
        with target_path.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,PRECURSOR_COLUMNS,extrasaction='ignore');writer.writeheader()
            def put(kind,**kwargs):writer.writerow({'row_type':kind,'version':VERSION,**kwargs})
            put('RUN_CONFIG',**{k:v for k,v in settings.items() if k not in ('version','kind')})
            for k,v in PRECURSOR_RULES.items():put('RULE',rule=k,rule_label=v)
            for r in metrics:put('RULE_METRIC',**r)
            for x in feats:put('FEATURE_SUMMARY',rule=x['rule'],segment=x['segment'],status=x['status'],n=x['case_count'],feature_mean=x['value'])
            for h in (6,12,24):
                for threshold in (5,10):
                    k=f'up{threshold}_{h}h'
                    for seg in ('train','test'):
                        b=base[seg]
                        put('LABEL_SUMMARY',rule=k,segment=seg,n=b['n'],available_n=b['avail_n'][k],up5_n=b[k],up5_rate_pct=_precursor_rate(b,k))
            for day in sorted(byday):put('DAILY_SAMPLE',at_kst=day,**byday[day])
            for x in market_stats:put('MARKET_SAMPLE',**x)
            for x in coverage.values():put('COVERAGE',**x)
            for msg in errs:put('DATA_ERROR',error=msg)
            for msg in notes:put('WARNING',warning=msg)
            put('VERDICT',verdict=verdict['status'],message=verdict['message'])
            # 대용량 관측: 서버 디스크에서 한 행씩 복사해 RAM/JSON 폭증 방지.
            with obs_path.open('r',encoding='utf-8-sig',newline='') as rf:
                reader=csv.DictReader(rf)
                for row in reader:writer.writerow(row)
        try:obs_path.unlink()
        except OSError:pass
        if progress:progress('상승 전조 CSV 집계 완료',99,len(markets),len(markets),error_n)
        return {'version':VERSION,'kind':'precursor','settings':settings,'checked':len(coverage),
            'requested_markets':len(markets),'eligible':eligible,
            'full_markets':full_n,'partial_markets':len(coverage)-full_n,
            'counts':counts,'metrics':metrics,'features':feats,
            'horizons':[{ 'label':f'+{t}% / {h}h','segment':seg,
                  'cases':base[seg]['avail_n'][f'up{t}_{h}h'],'hits':base[seg][f'up{t}_{h}h'],
                  'rate':_precursor_rate(base[seg],f'up{t}_{h}h')}
                  for h in (6,12,24) for t in (5,10) for seg in ('train','test')],
            'verdict':verdict,'notes':notes,'errors':errs[:30],
            '_csv_file':str(target_path),'csv_ready':True,
            'csv_name':f'BITHUMB_V3_8_ALL_RESULTS_precursor_{cfg.tf}m_{cfg.days}d.csv'}
    except Exception:
        for f in (obs_path,target_path):
            try:f.unlink()
            except OSError:pass
        raise


def precursor_scan(cfg,progress=None):
    # 매수 후보를 만들지 않음. 현재 시점 특징만 읽어 주요 시장 상태를 참고용으로 표시.
    markets,eligible=universe(replace(cfg,turnover_min=0))
    def _on(m,d,t,e):
        if progress:progress(f'V3.8 현재 특징 수집 {m}',5+int(92*d/len(markets)),d,len(markets),e)
    data,errs=load_series(markets,cfg,days=max(8,math.ceil(min_history(cfg)*cfg.tf/1440)+2),progress=_on)
    watch=[]
    for item in markets:
        m=item['market'];rows=complete(data.get(m,[]),cfg.tf)
        if len(rows)<min_history(cfg)+1:continue
        b=rows[-1]
        if (b.get('turn24') or 0)<cfg.turnover_min:continue
        v=b.get('turnavg20')
        ratio=b['turn']/v if v else None
        w=1440//cfg.tf
        if len(rows)<=w:continue
        change=(b['c']/rows[-1-w]['c']-1)*100
        prior_high=max(r['h'] for r in rows[-1-w:-1])
        highgap=(prior_high/b['c']-1)*100
        watch.append({'market':m,'price':round(b['c'],8), 'at_kst':(b['t']+timedelta(minutes=cfg.tf)).isoformat(),
              'turnover_ratio':round(ratio,2) if ratio is not None else None,
              'past_24h_pct':round(change,2),'distance_prior_high_pct':round(highgap,2),
              'historical_turnover_24h_krw':round(b.get('turn24') or 0),
              'flags':{'turn_2x':ratio is not None and ratio>=2,'momentum':change>=2,'near_high':highgap<=2}})
    watch.sort(key=lambda x:(sum(x['flags'].values()),x['historical_turnover_24h_krw']),reverse=True)
    return {'version':VERSION,'kind':'precursor','checked':len(data),'eligible':eligible,
      'signals':[],'watch':watch[:20], 'errors':errs[:20],
      'updated':kst_now().strftime('%Y-%m-%d %H:%M:%S KST'),
      'notes':['현재 시점의 사전 정의된 특징만 참고로 표시합니다. BUY·진입가 계산 없음.']}

# -------------------------------- 화면 --------------------------------
HTML = r'''<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>BITHUMB V3.8 | 상승 전조 탐색</title>
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

/* V3.5: 모바일 중심 다크 대시보드 */
:root{color-scheme:dark;--bg:#090f20;--panel:#121a31;--inner:#0b1326;--line:#2c3654;--text:#edf2ff;--muted:#9ca9c3;--good:#50e0aa;--bad:#ff808b;--blue:#3f65ff;--gold:#ffc47b}
body{background:var(--bg)}.entry,.card,.stat{background:var(--panel);box-shadow:0 6px 24px rgba(0,0,0,.07)}.entry{border-color:var(--line)}
.cell{background:#19233e;border-radius:13px;padding:13px 9px;text-align:center}.cell b{font-size:19px;margin-top:4px;color:#f0f3ff}.cell small{font-size:12px;color:#a7b2c9}
.tag{background:#123d34;color:#5ce6b4}.tag.watch{background:#443421;color:#ffd088}.version{background:#263f82;color:#c4d8ff}select,input{color:#edf2ff;background:var(--inner);border-color:#394767}button{color:#e3eaff;background:#202b4c;border-color:#34466f}button.primary{color:#fff;background:#3f65ff}
.progress{margin:12px 0;background:#121e3b;border-radius:13px;padding:12px;border:1px solid #344467}.progress[hidden]{display:none}.phead{display:flex;align-items:center;justify-content:space-between;gap:8px;color:#c2d0ef;font-size:13px}.ptrack{height:12px;background:#2e3a5d;border-radius:99px;margin-top:10px;overflow:hidden}.pfill{height:100%;width:0%;background:#5278ff;border-radius:99px;transition:width .35s ease}.progress .msg{margin-top:7px}
.expanded{margin-top:9px}.expanded summary{cursor:pointer;color:#b4c8ff;font-weight:700;padding:9px;border-radius:10px;background:#202e55}.expanded[open] summary{margin-bottom:8px}
.scoregrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:12px 0}.scorecell{background:#1c2746;padding:10px;text-align:center;border-radius:14px}.scorecell strong{font-size:25px;display:block}.scorecell small{display:block;color:#b0bdd7;font-size:12px}.explainer{background:#153127;color:#65eab4;padding:12px;border-radius:12px;font-size:13px;line-height:1.65;margin-top:12px;font-weight:600}
@media(min-width:720px){.scoregrid{grid-template-columns:repeat(4,minmax(0,1fr))}}
</style></head><body><main>
<header><div><span class="version">NEW V3.8</span> <span class="muted" style="font-size:12px">빗썸 KRW · 주문 없는 연구용</span></div><h1>상승 전조 탐색 · 코인 상승 전 특징 연구</h1><div class="sub">매일 자정 시점의 거래대금·가격 구조를 관측하고 이후 6·12·24시간 +5%/+10% 도달 여부를 비교합니다. 이 버전은 <b>BUY 신호·매매 수익률을 계산하지 않는 관찰 연구</b>입니다. V2/CORE는 그대로 보존합니다.</div></header>
<div class="stats"><div class="stat"><small>검사 마켓</small><div class="value" id="k_count">–</div></div><div class="stat"><small>진입 후보 (V3.8 비활성)</small><div class="value g" id="k_sig">–</div></div><div class="stat"><small>관심 후보</small><div class="value" id="k_watch">–</div></div><div class="stat"><small>최근 갱신 (KST)</small><div class="value" id="k_time" style="font-size:15px">–</div></div></div>
<div class="two"><div class="card"><h2>① 스캔 설정</h2>
<label>전략</label><select id="st"><option value="pullback">눌림목 · 박스 돌파 → 재테스트</option><option value="trend">기존 추세추종 · 실패 전략 기록용</option><option value="flow">이전 V3.6 거래대금 급증 연구 (보관)</option><option value="trap">지난 V3.7 가짜 이탈 연구 (보관)</option><option value="precursor" selected>NEW V3.8 상승 전조 탐색 (매매 신호 없음)</option></select>
<div class="row"><div><label>캔들</label><select id="tf"><option value="15">15분봉</option><option value="5">5분봉</option><option value="60" selected>1시간봉</option><option value="240">4시간봉</option></select></div><div><label>검색 종목 수</label><select id="n"><option value="40">40개</option><option value="80">80개</option><option value="0">전체 (거래대금 하한 적용)</option></select></div></div>
<div class="row"><div><label>24시간 거래대금 하한</label><select id="liquidity"><option value="500000000">5억원</option><option value="1000000000">10억원</option><option value="2000000000">20억원</option><option value="0">제한 없음</option></select></div><div><label>편도 수수료</label><select id="fee"><option value="0.25">0.25%</option><option value="0.04">0.04%</option><option value="0">0%</option></select></div></div>
<div class="row"><div><label>건당 투자금</label><select id="amt"><option value="1000000">100만원</option><option value="500000">50만원</option><option value="2000000">200만원</option><option value="5000000">500만원</option></select></div></div>
<div id="pbopts"><div class="row"><div><label>눌림목 익절 R</label><select id="tp"><option value="1.8">1.8 R</option><option value="1.5">1.5 R</option><option value="2">2.0 R</option><option value="3">3.0 R</option></select></div><div><label>캔들 매수세 근사 필터</label><select id="of"><option value="weak">약하게</option><option value="off">끄기</option><option value="strong">강하게</option></select></div></div></div>
<div id="tropts" style="display:none"><div class="row"><div><label>ATR 트레일링</label><select id="trail"><option value="3">3.0배</option><option value="2">2.0배</option><option value="4">4.0배</option></select></div><div><label>일부 익절 (절반)</label><select id="tp1"><option value="0">끄기</option><option value="1">1R에서 절반</option><option value="1.5">1.5R에서 절반</option><option value="2">2R에서 절반</option></select></div></div>
<div class="row"><div><label>본전선 이동</label><select id="be"><option value="0">끄기</option><option value="0.5">+0.5R 도달 시</option><option value="0.75">+0.75R 도달 시</option><option value="1">+1R 도달 시</option></select></div><div><label>BTC 추세 필터</label><select id="btc"><option value="off">끄기</option><option value="on">켜기</option></select></div></div>
<div class="row"><div><label>상대강도 상위</label><select id="rs"><option value="0">끄기</option><option value="0.3">상위 30%</option><option value="0.5">상위 50%</option><option value="0.2">상위 20%</option></select></div><div><label>상대강도 측정 기간</label><select id="rsd"><option value="7">7일</option><option value="14">14일</option></select></div></div></div>
<div id="tradeOptions"><div class="row"><div><label>하루 최대 진입</label><select id="maxday"><option value="2" selected>2건 (연구 기본)</option><option value="1">1건</option><option value="3">3건</option><option value="4">4건 (비교용)</option></select></div><div><label>동시 보유</label><select id="maxpos"><option value="1">1개</option><option value="2">2개</option><option value="3">3개</option><option value="5">5개</option></select></div></div>
<div class="note">이 설정은 이전 매매형 전략에만 적용됩니다. V3.8 상승 전조 연구는 거래를 생성하지 않습니다.</div></div>
<div class="actions"><button class="primary" id="scanBtn" onclick="doScan()">지금 스캔</button><button id="autoBtn" onclick="autoSwitch()">자동 갱신 켜기</button><button id="scanStop" onclick="cancelCurrent('scan')" disabled>중지</button></div>
<div class="progress" id="scanProgress" hidden><div class="phead"><b id="scanStage">대기</b><b id="scanPct">0%</b></div><div class="ptrack"><div id="scanFill" class="pfill"></div></div><div class="msg" id="scanCount">0/0</div></div>
<div class="msg" id="scanMsg">서버 연결 확인 중…</div></div>
<div class="card"><h2>② 매수 신호 없음 · 연구 전용</h2><div id="signals" class="sub">V3.8은 BUY 신호를 만들지 않아요.</div><div class="section"><h2>③ 관찰 대상 · 현재 사전 특징</h2><div id="watch" class="sub">아직 스캔하지 않았어요.</div></div></div></div>
<div class="two"><div class="card" id="holdPanel"><h2>④ 보유 코인 손절선 (이전 추세추종 기록용)</h2><div class="sub">진입가·진입 시각을 입력하면 확정 분봉 기반 ATR 트레일링을 재현합니다. 실제 주문 기록을 조회하지 않습니다.</div><label>종목 (예: XRP)</label><input id="h_sym" autocapitalize="characters" placeholder="XRP"><div class="row"><div><label>내 진입가 (원)</label><input id="h_price" type="number" min="0" step="any" placeholder="예: 3250"></div><div><label>진입 시각 (KST)</label><input id="h_time" type="datetime-local"></div></div><div class="actions"><button id="holdBtn" class="primary" onclick="doHold()">트레일링 확인</button></div><div class="msg" id="holdMsg"></div><div id="holdResult"></div></div>
<div class="card"><h2>⑤ 상승 전조 연구 · V3.8 학습/검증 비교</h2><div class="sub">KST 자정에 코인당 매일 1번 관측해 이후 6/12/24시간에 +5%/+10% 도달 여부를 판별합니다. 전체 사례와 사전 정의된 4개 특징을 <b>학습 70% / 검증 30%</b>에서 비교해요. 거래를 가정하지 않으며 결과는 CSV 한 파일로 보낼 수 있습니다.</div><div class="row"><div><label>기간</label><select id="days"><option value="15">15일</option><option value="30">30일</option><option value="80">80일</option><option value="180">180일</option><option value="365">365일</option></select></div><div><label>분석 조건</label><input value="자정 1회 관측 · 미래24h 라벨" disabled></div></div><div class="actions"><button class="primary" id="btBtn" onclick="doBT()">상승 전조 분석 실행</button><button id="csvBtn" onclick="exportCSV()" disabled>전체 결과 CSV 저장</button><button id="btStop" onclick="cancelCurrent('backtest')" disabled>중지</button></div><div class="progress" id="btProgress" hidden><div class="phead"><b id="btStage">대기</b><b id="btPct">0%</b></div><div class="ptrack"><div id="btFill" class="pfill"></div></div><div class="msg" id="btCount">0/0</div></div><div id="btMsg" class="msg">기간 15·30·80·180·365일 / 40·80·전체. 1시간·4시간봉 전용. 결과는 CSV 파일 한 개로 받아서 보내줘. 직접적인 수익·승률 예측은 하지 않습니다.</div><div id="btResult"></div></div></div>
<div class="card"><h2>V3.8 결과를 읽을 때 주의</h2><div class="warn">• 이번 버전은 거래 없이 향후 고가 도달률만 관찰합니다. 수익률·진입 확률·BUY를 예측하지 않습니다.<br>• 한 코인 하루 한 번, KST 00:00 기준. 미래 24시간 자료가 없는 마지막 하루는 제외됩니다.<br>• 한 봉이라도 누락된 관측 구간은 제외하고 임의 보간하지 않습니다.<br>• 현재 상장된 KRW 종목·현재 거래대금 상위 순위에 기반한 과거 표본선정 편향이 남아 있습니다.<br>• 거래대금 하한은 관측 당시 과거 24시간 거래대금으로 재평가합니다.<br>• +5%/+10%는 미래 고가 도달이며 실제 체결·이익 실현을 보장하지 않습니다.<br>• 주문·API 키 기능은 없으며, 데이터 부족이면 합격/폐기 판단을 보류합니다.</div></div>
</main><script>
const $=id=>document.getElementById(id);let autoTimer=null, scanning=false, lastBT=null, activeJobs={scan:null,backtest:null};
const fmt=v=>v===null||v===undefined?'–':Number(v).toLocaleString('ko-KR',{maximumFractionDigits:Number(v)<100?5:0});
const pct=v=>v===null||v===undefined?'–':(Number(v)>0?'+':'')+Number(v).toFixed(2)+'%';
const krw=v=>(v>0?'+':'')+Number(v||0).toLocaleString('ko-KR')+'원';
const esc=s=>String(s??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
let latestResearchJobId=null;
const ids=['st','tf','n','liquidity','fee','amt','tp','of','trail','tp1','be','btc','rs','rsd','maxday','maxpos','days'];
function qs(){const p=new URLSearchParams();ids.forEach(id=>p.set(id,$(id).value));return p.toString()}
function changeStrategy(){let trend=$('st').value==='trend',flow=$('st').value==='flow',trap=$('st').value==='trap',precursor=$('st').value==='precursor';$('pbopts').style.display=(trend||flow||precursor||trap)?'none':'block';$('tropts').style.display=trend?'block':'none';$('tradeOptions').style.display=precursor?'none':'block';$('holdPanel').style.display=precursor?'none':'block';const wanted=precursor?['60','240']:trap?['15','60']:(trend||flow)?['60','240']:['5','15','60'];
 [...$('tf').options].forEach(o=>o.hidden=!wanted.includes(o.value));if(!wanted.includes($('tf').value))$('tf').value=precursor?'60':trap?'15':(trend||flow)?'60':'15'; if(trap||precursor){$('maxday').value=String(Math.min(2,Number($('maxday').value)));}}
$('st').onchange=()=>{changeStrategy();$('maxday').value=$('st').value==='pullback'?'4':'2'};changeStrategy();
async function request(path){const r=await fetch(path);let d;try{d=await r.json()}catch(e){throw Error('서버 JSON 응답이 아닙니다')};if(!r.ok||d.error)throw Error(d.error||'HTTP '+r.status);return d}
async function ping(){try{const d=await request('/api/ping');$('scanMsg').textContent='서버 연결 완료 · '+esc(d.version)+' · 마지막 스캔을 실행할 수 있습니다.'}catch(e){$('scanMsg').textContent='연결 실패: '+e.message}}
function tiles(items){return `<div class="detail">${items.map(([k,v])=>`<div class="cell"><small>${esc(k)}</small><b>${esc(v)}</b></div>`).join('')}</div>`}
function card(s){
const score=s.score||{};
const pts=[['Structure',score.structure],['Timing',score.timing],['Trend',score.trend],['Setup',score.setup],['Entry',score.entry],['Risk 안전도',score.safety],['Volume',score.volume],['매수체결 비율',score.pressure==null?'미집계':score.pressure+'%']];
const scoreHtml=`<div class="scoregrid">${pts.map(([name,num])=>`<div class="scorecell"><strong>${esc(num)}</strong><small>${esc(name)}</small></div>`).join('')}</div>`;
const rr=s.tp&&s.price>s.stop?((s.tp-s.price)/(s.price-s.stop)).toFixed(2)+'R':'트레일링';
const levels=tiles([['예상 진입가',fmt(s.price)+'원'],['손절가',fmt(s.stop)+'원'],['목표가',s.tp?fmt(s.tp)+'원':'ATR 추종'],['기대 R:R',rr]]);
return `<div class="entry"><div class="top"><div><div class="coin">${esc(s.market.replace('KRW-',''))} <small>${fmt(s.price)}원</small></div><small>신호 확정 ${esc(s.signal_at.replace('T',' '))}</small></div><span class="tag">${esc(s.strategy==='trap'?'V3.7 지지선 회복 연구 후보':s.strategy==='flow'?'거래대금 반응 연구 후보':s.strategy==='trend'?'추세추종 조건 충족':'눌림목 조건 충족')}</span></div>
<div class="note">24H ${s.turnover_24h?fmt(s.turnover_24h/100000000)+'억원':'–'} · 거래량 ${esc(s.context?.volume_ratio??'–')}배 · 위험폭 ${Number(s.risk_pct).toFixed(2)}% · 규칙 점수 <b>${esc(score.total??'–')}점</b></div>
<details class="expanded"><summary>분석 상세 보기</summary>${scoreHtml}<div class="explainer">${esc(s.why||'모든 전략 조건을 충족한 과거 마감봉을 확인했습니다. 실시간 호가와 체결 조건은 별도 확인이 필요합니다.')}</div>${levels}
<div class="note">※ 점수는 조건 충족 상태를 보여주는 실험용 규칙 점수이며, 승률·수익 확률이나 매수 추천이 아닙니다.</div></details></div>`}
function updateProgress(prefix,d){const container=$(prefix+'Progress');container.hidden=false;$(prefix+'Stage').textContent=d.stage;$(prefix+'Pct').textContent=d.percent+'%';$(prefix+'Fill').style.width=d.percent+'%';$(prefix+'Count').textContent=`검사 ${d.done}/${d.total||'?'}개 · 오류 ${d.errors||0}개 · 경과 ${d.elapsed}초`;}
async function runJob(kind,query,progressPrefix){const start=await request('/api/job/start?kind='+kind+'&'+query);activeJobs[kind]=start.job;$(kind==='scan'?'scanStop':'btStop').disabled=false;let failedPoll=0;
while(true){await new Promise(ok=>setTimeout(ok,1300));let s;
try{s=await request('/api/job/status?id='+encodeURIComponent(start.job));failedPoll=0}catch(e){if(++failedPoll>=4)throw e;continue;}
updateProgress(progressPrefix,s);if(!s.running){activeJobs[kind]=null;$(kind==='scan'?'scanStop':'btStop').disabled=true;if(s.error)throw Error(s.error);if(kind==='backtest'&&s.result?.kind==='precursor')latestResearchJobId=start.job;return s.result;}}
}
async function cancelCurrent(kind){const id=activeJobs[kind];if(!id)return;try{const r=await request('/api/job/cancel?id='+encodeURIComponent(id));$(kind==='scan'?'scanMsg':'btMsg').textContent=r.message}catch(e){$(kind==='scan'?'scanMsg':'btMsg').textContent='중지 오류: '+e.message}}
async function doScan(){if(scanning)return;scanning=true;const b=$('scanBtn');b.disabled=true;b.textContent='스캔 중…';$('scanMsg').textContent='실시간 진행률을 표시합니다.';
$('scanProgress').hidden=false;$('scanPct').textContent='0%';$('scanFill').style.width='0%';
try{const d=await runJob('scan',qs(),'scan');$('k_count').textContent=d.checked;$('k_sig').textContent=d.signals.length;$('k_watch').textContent=d.watch.length;$('k_time').textContent=d.updated.slice(11,19);
if(d.kind==='precursor'){
$('signals').innerHTML='이번 버전은 BUY 판단을 하지 않아요. 과거 상승 전 특징이 구별되는지 연구 중입니다.';
$('watch').innerHTML=(d.watch||[]).map(s=>`<div class="entry"><b>${esc(s.market.replace('KRW-',''))}</b> · ${fmt(s.price)}원<div class="note">직전 24h ${pct(s.past_24h_pct)} · 거래대금 ${esc(s.turnover_ratio??'–')}배 · 이전 고점 거리 ${pct(s.distance_prior_high_pct)}</div><small>연구용 관측 상태 · 매수 추천 아님</small></div>`).join('')||'조건을 만족하는 관측 종목 없음';
$('scanMsg').textContent='시장 특징 참고용 스캔 완료 · BUY 신호 없음 · '+d.updated;
return;
}
$('signals').innerHTML=d.signals.length?d.signals.map(card).join(''):'조건을 모두 통과한 진입 후보가 없습니다.';
$('watch').innerHTML=d.watch.length?d.watch.map(s=>`<div class="entry"><div class="top"><b>${esc(s.market)}</b><span class="tag watch">${esc(s.status||'관심')}</span></div><div class="sub">기준 고점 ${fmt(s.box)}원 · 마지막 완료봉 ${fmt(s.price)}원</div></div>`).join(''):'관심 후보가 없습니다.';
$('scanMsg').textContent=`검사 완료 ${d.checked}개 / 거래대금 조건 충족 ${d.eligible}개 · ${d.updated}`+(d.errors.length?`\n자료 부족/오류 ${d.errors.length}건: `+d.errors.slice(0,2).join(' / '):'');
}catch(e){$('scanMsg').textContent='스캔 오류: '+e.message}finally{activeJobs.scan=null;$('scanStop').disabled=true;scanning=false;b.disabled=false;b.textContent='지금 스캔'}}
function autoSwitch(){if(autoTimer){clearInterval(autoTimer);autoTimer=null;$('autoBtn').textContent='자동 갱신 켜기'}else{doScan();autoTimer=setInterval(()=>{if(!scanning)doScan()},90000);$('autoBtn').textContent='자동 갱신 끄기 (90초)'}}
async function doHold(){let b=$('holdBtn');b.disabled=true;b.textContent='계산 중…';try{let q=new URLSearchParams(qs());q.set('sym',$('h_sym').value);q.set('price',$('h_price').value);q.set('time',$('h_time').value);q.set('st','trend');if(!['60','240'].includes(q.get('tf')))q.set('tf','60');
let d=await request('/api/hold?'+q.toString());$('holdMsg').textContent=d.note;
$('holdResult').innerHTML=`<div class="entry"><h3>${esc(d.market)} · 최근 완료봉 ${esc(d.time_kst.replace('T',' '))}</h3>${tiles([['현재 참고 종가',fmt(d.latest)+'원'],['수수료 차감 추정',pct(d.pnl_est_pct)],['현재 손절 기준',fmt(d.stop)+'원'],['최초 손절 기준',fmt(d.stop0)+'원']])}<div class="note">일부 익절 ${d.tp1?fmt(d.tp1)+'원':'설정 없음'} · ${d.partial?'절반 익절 도달 가정':'미도달'}<br>${d.sold_at?'규칙상 과거 손절선에 닿은 시각: '+esc(d.sold_at):'완료봉 기준 미청산 가정'}${d.gaps?' · 봉 결측 발견: 이후 추정 중단':''}</div></div>`;
}catch(e){$('holdMsg').textContent='오류: '+e.message}finally{b.disabled=false;b.textContent='트레일링 확인'}}
function trStat(label,s){return `<tr><td>${label}</td><td>${s?.n??0}</td><td>${s?.win_rate??'–'}%</td><td>${s?.avg_net==null?'–':pct(s.avg_net)}</td><td>${s?.pnl_krw==null?'–':krw(s.pnl_krw)}</td></tr>`}
function renderPrecursor(d){
const v=d.verdict||{},M=d.metrics||[],H=d.horizons||[];
let out=`<div class="section"><div class="bigresult">상승 전 특징 비교 완료</div><div class="note">관측 ${d.counts.valid.toLocaleString()}건 · 완전 확보 ${d.full_markets}/${d.requested_markets}개 · 부분 자료 ${d.partial_markets}개 · 빈 창 ${d.counts.invalid_windows}건 · 당시 거래대금 하한 제외 ${d.counts.rejected_liquidity}건</div></div>`;
out+=`<div class="entry"><div class="coin ${v.status==='CANDIDATE_ONLY'?'gold':v.status==='NO_SIGNAL'?'r':''}">${esc(v.status)}</div><div class="note">${esc(v.message)}</div></div>`;
out+=`<h3>향후 6·12·24시간 +5%·+10% 고가 도달률</h3><div class="scroll"><table><tr><th>목표</th><th>구간</th><th>표본</th><th>도달</th><th>관측률</th></tr>${H.map(x=>`<tr><td>${esc(x.label)}</td><td>${esc(x.segment)}</td><td>${x.cases}</td><td>${x.hits}</td><td>${x.rate==null?'–':pct(x.rate)}</td></tr>`).join('')}</table></div>`;
out+=`<h3>미리 고정한 4개 특징 · 24시간 +5% 기준</h3><div class="note">기본 관측률과의 차이는 %p. 실현 가능한 거래 수익이나 매수 확률을 의미하지 않습니다.</div><div class="scroll"><table><tr><th>조건</th><th>구간</th><th>사례</th><th>도달률</th><th>기본률</th><th>차이(%p)</th></tr>${M.map(r=>`<tr><td>${esc(r.rule_label)}</td><td>${esc(r.segment)}</td><td>${r.n}</td><td>${r.up5_rate_pct==null?'–':pct(r.up5_rate_pct)}</td><td>${r.baseline_up5_rate_pct==null?'–':pct(r.baseline_up5_rate_pct)}</td><td class="${r.lift_pp>0?'g':'r'}">${r.lift_pp==null?'–':r.lift_pp>0?'+':''}${r.lift_pp??''}</td></tr>`).join('')}</table></div>`;
out+=`<h3>상승/비상승 사례의 진입 전 특징</h3><div class="scroll"><table><tr><th>구간</th><th>집단</th><th>과거 특징</th><th>평균</th><th>표본</th></tr>${(d.features||[]).map(x=>`<tr><td>${esc(x.segment)}</td><td>${x.status==='up'?'+5% 도달':'미도달'}</td><td>${esc(x.rule)}</td><td>${esc(x.value??'–')}</td><td>${x.case_count}</td></tr>`).join('')}</table></div>`;
out+=`<div class="note">CSV에는 전체 관측치의 직전 거래대금·ATR·가격 구조·6/12/24시간 미래 고가 라벨·시장별 데이터 확보율까지 담습니다. 사진 없이 파일 하나만 보내 주세요.</div>`;
return out;
}
function renderBT(d){if(d.kind==='precursor')return renderPrecursor(d);let s=d.stat;let h=`<div class="section"><small>검사 마켓 ${d.markets}/${d.requested_markets??d.markets}개 · 기간 일부만 확보 ${d.partial_markets??0}개 · 신호 ${d.signals}건 · 선택 ${d.selected}건 · 미확정 ${d.unknown}건</small><div class="bigresult ${s.avg_net>0?'g':s.avg_net<0?'r':''}">${s.avg_net==null?'거래 없음':'평균 '+pct(s.avg_net)}</div><div class="sub">승률 ${s.win_rate??'–'}% · ${s.win}승 ${s.loss}패 · 동일 금액 건별 합산 ${krw(s.pnl_krw)}</div></div>`;
if(d.kind==='trap'){
 const v=d.verdict||{};
 h+=`<h3>V3.7 지지선 이탈 실패 연구 · A/B/C × 4/8/12시간</h3><div class="note">A 첫 회복 / B 지지 재확인 / C 단기 고점 돌파. 모두 확인봉 마감 뒤 다음 시가 가정. <b>하루 2회, 없으면 0회</b>.</div><div class="scroll"><table><tr><th>비교</th><th>거래</th><th>승률</th><th>학습 평균</th><th>검증 평균</th><th>검증 상위2 제외</th></tr>${(d.comparisons||[]).map(x=>`<tr><td>${esc(x.label)}</td><td>${x.stat.n}</td><td>${x.stat.win_rate??'–'}%</td><td>${pct(x.train?.avg_net)}</td><td class="${x.test?.avg_net>0?'g':'r'}">${pct(x.test?.avg_net)}</td><td>${pct(x.test_no_top2?.avg_net)}</td></tr>`).join('')}</table></div>`;
 h+=`<div class="entry"><div class="coin ${v.status==='RESEARCH_ONLY'?'gold':'r'}">${esc(v.status||'미판정')}</div><div class="note">${esc(v.message||'')} · 이 결과는 수익률 예측이 아닌 가설 검증입니다.</div></div>`;
 h+=`<div class="detail">${tiles([['완전 기간 종목',d.full_sensitivity?.full_markets??'–'],['부분 데이터 종목',d.partial_markets],['CSV 비교군 거래 수',Object.values(d.variant_trades||{}).reduce((n,a)=>n+a.length,0)],['오류',d.error_count]])}</div>`;
 h+=`<div class="note">⭐ 백테스트가 끝나면 <b>전체 결과 CSV 저장</b> 파일 하나만 올려줘. 거래별 지지선·스윕 저점·1시간봉 상태·검증 결과·오류까지 저장돼.</div>`;
 return h;
}
if(d.kind==='flow'){
 const v=d.verdict||{};
 h+=`<h3>V3.6 거래대금 급증 후 가격 유지 연구</h3><div class="note">급증봉 직후 / 횡보 유지 / 상대강도 / BTC 상승 4개 연구군을 동일 코인·기간으로 비교. 일일 최대 2회, 동시 보유 제한 적용.</div><div class="scroll"><table><tr><th>전략</th><th>거래</th><th>승률</th><th>학습 평균</th><th>검증 평균</th><th>검증 상위2 제외</th></tr>${d.comparisons.map(x=>`<tr><td>${esc(x.label)}</td><td>${x.stat.n}</td><td>${x.stat.win_rate??'–'}%</td><td>${pct(x.train?.avg_net)}</td><td class="${x.test?.avg_net>0?'g':'r'}">${pct(x.test?.avg_net)}</td><td>${pct(x.test_no_top2?.avg_net)}</td></tr>`).join('')}</table></div>`;
 h+=`<div class="entry"><div class="coin ${v.status==='RESEARCH_ONLY'?'gold':'r'}">${esc(v.status||'미판정')}</div><div class="note">${esc(v.message||'')} · 실제 매매 신호가 아니라 가설 검증입니다.</div></div>`;
 h+=`<div class="detail">${tiles([['백테스트 대상',`${d.markets}/${d.requested_markets}개`],['부분 데이터 종목',d.partial_markets],['CSV 포함 선택 거래',Object.values(d.variant_trades||{}).reduce((n,a)=>n+a.length,0)],['데이터 오류',d.error_count]])}</div>`;
 h+=`<div class="note">⭐ 아래 <b>전체 결과 CSV 저장</b> 버튼을 눌러 나온 파일 하나만 보내줘. 사진은 필요 없어! CSV에 기간·설정·모든 전략 거래·일자별·종목별·결측·오류와 결론이 담겨 있어. 현재 거래대금 상위 과거 검사는 선택 편향이 남아 있어.</div>`;
 return h;
}
if(d.kind==='trend' && Array.isArray(d.comparisons)&&d.comparisons.length){
 h+=`<h3>V3.5 기존 비교 유지 (동일 기간·종목)</h3><div class="note">횟수 제한만 바꾼 돌파 4회/2회 비교와 눌림 재상승·BTC 필터 비교입니다. 수익 우위는 아직 검증 전입니다.</div><div class="scroll"><table><tr><th>비교 전략</th><th>거래</th><th>승률</th><th>평균순익</th><th>검증 평균</th><th>총 손익</th></tr>${d.comparisons.map(x=>`<tr><td>${esc(x.label)}</td><td>${x.stat.n}</td><td>${x.stat.win_rate??'–'}%</td><td class="${x.stat.avg_net>0?'g':'r'}">${pct(x.stat.avg_net)}</td><td>${pct(x.test?.avg_net)}</td><td>${krw(x.stat.pnl_krw)}</td></tr>`).join('')}</table></div>`;
}
if(Array.isArray(d.exit_analysis)&&d.exit_analysis.length){
 h+=`<h3>청산 사유별 · 청산 후 24시간 추적</h3><div class="note">24시간 고점은 사후 관찰용이며 실제 매도가 아닙니다. 기간·분봉이 부족하면 표본에서 제외합니다. MFE/MAE는 해당 봉 고저가 기준 관측치로 체결 가능 수익을 뜻하지 않습니다.</div><div class="scroll"><table><tr><th>청산 사유</th><th>건수</th><th>평균 순익</th><th>최대 유리(MFE)</th><th>최대 불리(MAE)</th><th>24시간 확인</th><th>+2% 고점</th></tr>${d.exit_analysis.map(x=>`<tr><td>${esc(x.why)}</td><td>${x.n}</td><td>${pct(x.avg_net)}</td><td>${pct(x.avg_mfe)}</td><td>${pct(x.avg_mae)}</td><td>${x.post24_count}/${x.n}</td><td>${x.post24_up2}/${x.post24_count||0}</td></tr>`).join('')}</table></div>`;
}

if(d.kind==='trend' && d.v35_verdict){
 const dd=d.v35_diagnostic||{}, vv=d.v35_verdict||{}, rb=d.v35_robust||{};
 const statline=(x)=>x?`표본 ${x.n}건 · 승 ${x.win} / 패 ${x.loss} · 평균 ${pct(x.avg_net)}`:'자료 없음';
 h+=`<div class="section"><h3>V3.5 · 손실 정밀 진단 / 채택·폐기 기준</h3><div class="entry"><div class="coin ${vv.status==='DISCARD'?'r':'gold'}">${esc(vv.status==='DISCARD'?'중단 권고':vv.status==='RESEARCH_ONLY'?'독립 검증 필요':vv.status==='DATA_INCOMPLETE'?'데이터 보완 필요':'표본 부족')}</div><div class="note">${esc(vv.text||'')} · 확보 마켓 비율 ${esc(vv.coverage??0)}%</div></div>
 <div class="detail">${tiles([['2회 기준 선택 거래',dd.total??0],['수익 / 손실',`${dd.wins??0}승 / ${dd.losses??0}패`],['+1% 유리 움직임도 없던 손실',dd.loss_never_up1??0],['+2% 유리했다가 손실',dd.loss_gave_back2??0],['6시간 이내 손절',dd.stop_under6h??0],['손절 뒤 24h 종가 +2%',`${dd.stop_post24_close_up2??0}/${dd.stop_post24_observed??0}`]])}</div>
 <div class="note">손절 후 24h는 완전한 봉 자료가 확보된 경우만 집계해. MFE는 고가 기준 관측이고 동일 봉 내 손절 시 고가 선후 관계는 알 수 없어.</div>
 <h3>전체 기간 확보 종목만 남긴 민감도 검사</h3><div class="note">전체 기간을 확보한 ${d.v35_full_only?.full_markets??0}/${d.v35_full_only?.requested_markets??0}개 마켓만 재선별한 결과야. 부분 수집 때문에 성과가 왜곡되었는지 살펴보는 용도야.</div><div class="scroll"><table><tr><th>구간</th><th>거래</th><th>평균순손익</th></tr><tr><td>전체</td><td>${d.v35_full_only?.stat?.n??0}</td><td>${pct(d.v35_full_only?.stat?.avg_net)}</td></tr><tr><td>검증</td><td>${d.v35_full_only?.test?.n??0}</td><td>${pct(d.v35_full_only?.test?.avg_net)}</td></tr></table></div>
 <h3>큰 수익 2건 제외 영향</h3><div class="scroll"><table><tr><th>구간</th><th>제외</th><th>남은 거래</th><th>평균 순손익</th></tr>${[['전체',rb.overall],['검증',rb.validation]].map(([label,r])=>`<tr><td>${label}</td><td>${r?.removed??0}</td><td>${r?.left_n??0}</td><td>${r?.avg_net==null?'–':pct(r.avg_net)}</td></tr>`).join('')}</table></div>
 <h3>미리 고정한 3가지 필터 · 데이터 내 검증</h3><div class="note">검증 결과를 보고 문턱값을 조정하지 않는 실험이야. 학습 20건·검증 10건 미만은 긍정 판단에서 제외. 기존 기준 전략도 같이 표시해.</div>
 <div class="scroll"><table><tr><th>규칙</th><th>학습 건수</th><th>학습 평균</th><th>검증 건수</th><th>검증 평균</th><th>검증 상위2 제외</th></tr>${(d.v35_gates||[]).map(g=>`<tr><td>${esc(g.label)}</td><td>${g.train.n}</td><td>${pct(g.train.avg_net)}</td><td>${g.test.n}</td><td class="${g.test.avg_net>0?'g':'r'}">${pct(g.test.avg_net)}</td><td>${pct(g.test_no_top2.avg_net)}</td></tr>`).join('')}</table></div>
 <h3>진입 전에 확인 가능한 특징 · 승/패 비교</h3><div class="scroll"><table><tr><th>항목</th><th>승 (표본)</th><th>패 (표본)</th></tr>${(d.v35_features||[]).map(f=>`<tr><td>${esc(f.label)}</td><td>${f.win.avg==null?'–':f.win.avg} (${f.win.n})</td><td>${f.loss.avg==null?'–':f.loss.avg} (${f.loss.n})</td></tr>`).join('')}</table></div>
 <h3>가장 큰 수익/손실 5건 · 사례 진단</h3><div class="scroll"><table><tr><th>분류</th><th>종목</th><th>진입</th><th>순손익</th><th>거래량 배수</th><th>EMA 기울기</th></tr>${[...(dd.top_wins||[]).map(x=>({...x,group:'수익'})),...(dd.worst_losses||[]).map(x=>({...x,group:'손실'}))].map(t=>`<tr><td>${t.group}</td><td>${esc(t.market.replace('KRW-',''))}</td><td>${esc(t.entry_at.slice(0,16).replace('T',' '))}</td><td class="${t.net_pct>0?'g':'r'}">${pct(t.net_pct)}</td><td>${esc(t.features?.volume_ratio??'–')}</td><td>${esc(t.features?.ema_slope_5_pct??'–')}%</td></tr>`).join('')}</table></div>
 <div class="warn">이 진단은 당시 거래대금 순위가 아니라 현재 거래대금 상위 종목의 역사적 수익을 계산하므로 생존·선택 편향이 남아. 판정 통과도 실제 매수 신호가 아니야.</div></div>`;
}
h+=`<h3>학습 / 검증 (시간순 70/30)</h3><div class="scroll"><table><tr><th>기간</th><th>거래</th><th>승률</th><th>평균순익</th><th>총손익</th></tr>${trStat('전체',d.stat)}${trStat('학습',d.split.train)}${trStat('검증',d.split.test)}</table></div>`;
h+=`<h3>날짜별</h3><div class="scroll"><table><tr><th>일자</th><th>건수</th><th>승/패</th><th>평균순익</th><th>손익(원)</th></tr>${d.daily.map(x=>`<tr><td>${x.date}</td><td>${x.n}</td><td>${x.win}/${x.loss}</td><td>${pct(x.avg_net)}</td><td class="${x.pnl_krw>0?'g':'r'}">${krw(x.pnl_krw)}</td></tr>`).join('')||'<tr><td colspan="5">데이터 없음</td></tr>'}</table></div>`;
h+=`<h3>종목별 결과</h3><div class="scroll"><table><tr><th>종목</th><th>거래</th><th>승/패</th><th>승률</th><th>순손익 합</th></tr>${(d.market_stats||[]).map(x=>`<tr><td>${esc(x.market.replace('KRW-',''))}</td><td>${x.n}</td><td>${x.win}/${x.loss}</td><td>${x.win_rate}%</td><td class="${x.sum_net>0?'g':'r'}">${pct(x.sum_net)}</td></tr>`).join('')||'<tr><td colspan="5">거래 없음</td></tr>'}</table></div>`;
h+=`<h3>청산 사유 / 빠른 손절</h3><div class="note">진입 후 45분 이내 손절 ${d.quick_stop_45m??0}건 · 최대 연속 손실 ${d.max_lose_streak??0}건 · 청산 기준 최대 낙폭 ${krw(-Math.abs(d.mdd_krw??0))}</div><div class="scroll"><table><tr><th>사유</th><th>건수</th></tr>${(d.reasons||[]).map(x=>`<tr><td>${esc(x.reason)}</td><td>${x.n}</td></tr>`).join('')}</table></div>`;
h+=`<h3>최근 선택 거래</h3><div class="scroll"><table><tr><th>진입</th><th>마켓</th><th>종료사유</th><th>순수익</th></tr>${d.trades.slice(-30).reverse().map(t=>`<tr><td>${esc(t.entry_at.slice(5,16).replace('T',' '))}</td><td>${esc(t.market.replace('KRW-',''))}</td><td>${esc(t.why||'–')}</td><td class="${t.net_pct>0?'g':'r'}">${t.net_pct==null?'미확정':pct(t.net_pct)}</td></tr>`).join('')||'<tr><td colspan="4">거래 없음</td></tr>'}</table></div>`;
h+=`<div class="note">${d.warnings.map(x=>'• '+esc(x)).join('<br>')}${d.errors.length?'<br>• 데이터 부족: '+d.errors.map(esc).slice(0,3).join(' / '):''}</div>`;return h}
async function doBT(){const days=Number($('days').value),tf=Number($('tf').value),n=$('n').value;
const approx=Math.ceil(days*1440/tf)+(tf===60||tf===240?220:95)+4;
if(approx>20000){$('btMsg').textContent=`${days}일·${tf}분봉은 코인당 약 ${approx.toLocaleString()}개 봉이 필요합니다. 서버 한도 20,000개를 넘으므로 기간을 줄이거나 큰 분봉으로 변경해 주세요.`;return;}
if(n==='0'||days>=180){if(!confirm(`${days}일 / ${n==='0'?'전체':n+'개'} / ${tf}분봉 검사는 최초 데이터 수집이 오래 걸릴 수 있습니다. 계속할까요?`))return;}
const b=$('btBtn');b.disabled=true;b.textContent='백테스트 중…';$('csvBtn').disabled=true;latestResearchJobId=null;$('btMsg').textContent='종목별 진행률을 표시합니다.';
$('btProgress').hidden=false;$('btPct').textContent='0%';$('btFill').style.width='0%';
try{const d=await runJob('backtest',qs(),'bt');lastBT=d;$('btResult').innerHTML=renderBT(d);$('btMsg').textContent=d.kind==='precursor'?('관측 완료 · '+d.settings.requested_days+'일 · '+d.settings.tf_minutes+'분봉 · BUY 신호 없음 · CSV 저장 가능'):('계산 완료 · '+d.settings.period_days+'일 · '+d.settings.tf+'분봉 · 수수료 편도 '+d.settings.fee_pct+'% · 슬리피지 0% 가정');$('csvBtn').disabled=false}
catch(e){$('btMsg').textContent='백테스트 오류: '+e.message}finally{activeJobs.backtest=null;$('btStop').disabled=true;b.disabled=false;b.textContent='상승 전조 분석 실행'}}
function exportCSV(){
 if(!lastBT)return;
 if(lastBT.kind==='precursor'){
  if(!latestResearchJobId){alert('CSV 작업 식별자가 없어요. 분석을 다시 실행해 주세요.');return;}
  const a=document.createElement('a');a.href='/api/job/csv?id='+encodeURIComponent(latestResearchJobId);a.download=lastBT.csv_name||'BITHUMB_V3_8_ALL_RESULTS.csv';document.body.appendChild(a);a.click();a.remove();return;
 }
 const d=lastBT, version=d.version||'V3.7', cfg=d.settings||{};
 let objs=[{row_type:'RUN_CONFIG',version,run_at_kst:cfg.scanned_at_kst,kind:d.kind,
   period_days:cfg.period_days,tf_minutes:cfg.tf,market_scope:cfg.count,
   fee_side_pct:cfg.fee_pct,slippage_pct:cfg.slip_pct,investment_krw:cfg.per_trade_krw,
   daily_limit:cfg.maxday,requested_markets:d.requested_markets,checked_markets:d.markets,
   partial_markets:d.partial_markets,full_markets:d.full_sensitivity?.full_markets,signal_count:d.signals,selected_count:d.selected,
   unknown_count:d.unknown,error_count:d.error_count},
 ...Object.entries(d.rule_params||{}).map(([rule,value])=>({row_type:'RULE',version,rule,value})),
 {row_type:'OVERALL',version,...(d.stat||{})},
 ...Object.entries(d.split||{}).map(([segment,s])=>({row_type:'SPLIT',version,segment,...(s||{})})),
 ...(d.comparisons||[]).map(x=>({row_type:'COMPARE',version,variant:x.key,label:x.label,
    train_n:x.train?.n,train_avg:x.train?.avg_net,test_n:x.test?.n,test_avg:x.test?.avg_net,
    test_no_top2_avg:x.test_no_top2?.avg_net,test_trimmed_n:x.test_no_top2?.left_n,
    selected:x.selected,unknown:x.unknown,...x.stat})),
 ...(d.daily||[]).map(x=>({row_type:'DAILY',version,...x})),
 ...(d.market_stats||[]).map(x=>({row_type:'MARKET',version,...x})),
 ...(d.reasons||[]).map(x=>({row_type:'EXIT_REASON',version,...x})),
 ...(d.trades||[]).map(x=>({row_type:'MAIN_TRADE',version,...x})),
 ...Object.entries(d.variant_trades||{}).flatMap(([variant,trades])=>trades.map(x=>({row_type:'VARIANT_TRADE',version,variant,...x}))),
 ...Object.entries(d.coverage||{}).map(([market,q])=>({row_type:'COVERAGE',version,market,...q})),
 ...(d.full_sensitivity?[{row_type:'FULL_ONLY',version,full_markets:d.full_sensitivity.full_markets,requested_markets:d.full_sensitivity.requested_markets,...d.full_sensitivity.stat,test_n:d.full_sensitivity.test?.n,test_avg_net:d.full_sensitivity.test?.avg_net}]:[]),
 ...(d.errors||[]).map((error,index)=>({row_type:'DATA_ERROR',version,error,index:index+1})),
 ...(d.warnings||[]).map(warning=>({row_type:'WARNING',version,warning})),
 ...(d.verdict?[{row_type:'VERDICT',version,...d.verdict}]:[])];
 if(d.kind==='trend'){
   objs=[...objs,...(d.exit_analysis||[]).map(x=>({row_type:'EXIT_DIAGNOSTIC',version,...x})),
     ...(d.v35_features||[]).map(x=>({row_type:'PREENTRY_FEATURE',version,...x})),
     ...(d.v35_gates||[]).map(x=>({row_type:'FIXED_GATE',version,...x})),
     ...Object.entries(d.v35_gate_trades||{}).flatMap(([gate,trades])=>trades.map(x=>({row_type:'FIXED_GATE_TRADE',version,gate,...x}))),
     {row_type:'V35_DECISION',version,...(d.v35_verdict||{})},
     {row_type:'V35_DIAG',version,...(d.v35_diagnostic||{})},
     {row_type:'V35_FULL_ONLY',version,...(d.v35_full_only||{})}];
 }
 const columns=[...new Set(['row_type','version','market','variant','signal_at','entry_at','exit_at','net_pct',
                              ...objs.flatMap(x=>Object.keys(x))])];
 const cell=v=>{const s=typeof v==='object'&&v!==null?JSON.stringify(v):String(v??'');
    return /^[=+@]|^-(?!\d)/.test(s)?'"\''+s.replace(/"/g,'""')+'"':/[",\n\r]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s};
 const csv='\ufeff'+[columns.join(','),...objs.map(x=>columns.map(k=>cell(x[k])).join(','))].join('\r\n');
 const blob=new Blob([csv],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');
 a.href=url;a.download=`BITHUMB_${version.replace('.','_')}_ALL_RESULTS_${d.kind}_${cfg.tf}m_${cfg.period_days}d.csv`;
 document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),3000);
}

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

    def download_csv(self,job_id):
        with JOB_LOCK:
            pair=EXPORT_FILES.get(job_id)
            filepath,filename=pair if pair else (None,None)
        if not filepath or not filename or not Path(filepath).is_file():
            return self.send_body(404,{"error":"이 작업의 CSV 파일을 찾지 못했어요. 서버가 재시작됐을 수 있습니다."})
        path=Path(filepath)
        self.send_response(200)
        self.send_header('Content-Type','text/csv; charset=utf-8')
        self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(path.stat().st_size))
        self.end_headers()
        try:
            with path.open('rb') as r:
                while True:
                    part=r.read(1<<16)
                    if not part:break
                    self.wfile.write(part)
        except (BrokenPipeError,ConnectionResetError):pass

    def do_GET(self):
        route = urlparse(self.path)
        if route.path == "/":
            return self.send_body(200, HTML, "text/html")
        if route.path in ("/health", "/api/ping"):
            return self.send_body(200, {"ok": True, "version": VERSION, "local_kst":kst_now().isoformat(), "trading":False})
        if route.path == "/api/job/csv":
            return self.download_csv(parse_qs(route.query).get('id',[''])[0])
        if route.path == "/api/job/status":
            return self.send_body(200, job_status(parse_qs(route.query).get("id",[""])[0]))
        if route.path == "/api/job/cancel":
            q=parse_qs(route.query)
            res=cancel_job(q.get("id",[""])[0])
            return self.send_body(200 if "error" not in res else 404,res)
        if route.path == "/api/job/start":
            q = parse_qs(route.query)
            result = start_job(q.get("kind",[""])[0], Settings.from_q(q))
            return self.send_body(200 if "error" not in result else 429, result)
        if route.path not in ("/api/scan", "/api/backtest", "/api/hold"):
            return self.send_body(404, {"error":"지원하지 않는 경로입니다."})
        if not BUSY.acquire(blocking=False):
            return self.send_body(429, {"error":"다른 스캔/백테스트가 진행 중입니다. 해당 작업을 완료한 후 다시 실행하세요."})
        try:
            q=parse_qs(route.query)
            cfg=Settings.from_q(q)
            if route.path == "/api/scan":
                result=precursor_scan(cfg) if cfg.strategy=="precursor" else scan(cfg)
            elif route.path == "/api/backtest":
                result=precursor_backtest(cfg) if cfg.strategy=="precursor" else backtest(cfg)
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
