import os
import time
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template_string, request
import requests

app = Flask(__name__)
BITHUMB = "https://api.bithumb.com"
KST = timezone(timedelta(hours=9))
CACHE = {"at": 0, "markets": None}
CACHE_SECONDS = 30

HTML = r"""
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bithumb Signal Scanner</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#141c2f;--panel2:#10182a;--line:#283650;--text:#edf3ff;--muted:#9ba9c4;--green:#36d399;--red:#ff6878;--yellow:#ffca68;--blue:#82aaff}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#0b1020,#111b31);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Noto Sans KR",sans-serif}
main{max-width:1180px;margin:auto;padding:24px 16px 50px}.top{display:flex;justify-content:space-between;gap:16px;align-items:center;flex-wrap:wrap}
h1{margin:0;font-size:26px;letter-spacing:-.5px}.sub{color:var(--muted);font-size:13px;margin-top:5px}
.pill{border:1px solid var(--line);border-radius:999px;padding:7px 11px;color:var(--muted);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:20px 0}
.card,.panel{background:rgba(20,28,47,.94);border:1px solid var(--line);border-radius:16px;padding:16px}
.k{font-size:12px;color:var(--muted)}.v{font-size:24px;font-weight:750;margin-top:5px}.v small{font-size:12px;color:var(--muted);font-weight:500}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:end}.field{display:flex;flex-direction:column;gap:5px}.field label{font-size:12px;color:var(--muted)}
select,input,button{background:#0d1527;border:1px solid #33415e;color:var(--text);border-radius:9px;padding:10px 11px;font:inherit}button{cursor:pointer;background:#315de7;border-color:#315de7;font-weight:700}button.secondary{background:#1b2943;border-color:#354765}
.panel{margin-top:14px}.panel h2{font-size:16px;margin:0 0 12px}.status{font-size:13px;color:var(--muted);margin:10px 0}
.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:760px}th,td{text-align:left;padding:11px 10px;border-bottom:1px solid #26324a;white-space:nowrap}th{font-size:11px;color:var(--muted);font-weight:600}td{font-size:13px}.signal{font-size:11px;border-radius:6px;padding:4px 7px;font-weight:700}.BUY{background:#123d35;color:#6cf0c2}.WATCH{background:#40351e;color:#ffd783}.WAIT{background:#243047;color:#c5d2ec}.SELL{background:#4a202c;color:#ff9cab}
.two{display:grid;grid-template-columns:1.2fr 1fr;gap:14px}.rules{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.rule{padding:12px;border-radius:10px;background:#10182a;border:1px solid #26334b}.rule b{display:block;font-size:13px;margin-bottom:4px}.rule span{font-size:12px;color:var(--muted)}
.note{font-size:12px;color:var(--muted);line-height:1.65}.good{color:var(--green)}.warn{color:var(--yellow)}.bad{color:var(--red)}
footer{margin-top:18px;color:var(--muted);font-size:11px}
@media(max-width:760px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.two{grid-template-columns:1fr}.rules{grid-template-columns:1fr}h1{font-size:22px}.v{font-size:21px}}
</style>
</head>
<body><main>
<div class="top">
  <div><h1>빗썸 KRW · Signal Scanner</h1><div class="sub">대각 저항 돌파 → 재확인 → 지지 반등을 찾는 단타 보조 도구</div></div>
  <div class="pill" id="connection">연결 확인 전</div>
</div>
<div class="grid">
  <div class="card"><div class="k">검색 대상</div><div class="v" id="count">— <small>마켓</small></div></div>
  <div class="card"><div class="k">진입 확인</div><div class="v good" id="buys">— <small>코인</small></div></div>
  <div class="card"><div class="k">관심 후보</div><div class="v" id="watches">— <small>코인</small></div></div>
  <div class="card"><div class="k">마지막 갱신</div><div class="v" style="font-size:17px" id="updated">—</div></div>
</div>
<div class="panel">
<h2>스캔 설정</h2>
<div class="controls">
  <div class="field"><label for="tf">캔들 주기</label><select id="tf"><option value="1">1분</option><option value="3">3분</option><option value="5" selected>5분</option><option value="10">10분</option><option value="30">30분</option></select></div>
  <div class="field"><label for="limit">검색 코인 수</label><select id="limit"><option>20</option><option selected>40</option><option>80</option></select></div>
  <div class="field"><label for="vol">최소 거래량 배수</label><select id="vol"><option value="1.2">1.2배</option><option value="1.5" selected>1.5배</option><option value="2">2.0배</option></select></div>
  <div class="field"><label for="rr">목표 손익비</label><select id="rr"><option value="1.5">1 : 1.5</option><option value="2" selected>1 : 2</option><option value="3">1 : 3</option></select></div>
  <button onclick="scan()">지금 스캔</button><button class="secondary" onclick="toggleAuto()" id="autoBtn">자동 갱신 켜기</button>
</div>
<div class="status" id="status">스캔 버튼을 눌러 빗썸 공개 시세 데이터를 조회하세요. 처음 요청은 무료 서버 기동으로 지연될 수 있습니다.</div>
</div>
<div class="panel">
<h2>스캔 결과</h2>
<div class="tablewrap"><table><thead><tr><th>마켓</th><th>확정봉 종가</th><th>24h 변동</th><th>거래대금</th><th>거래량 배수</th><th>저항선 상태</th><th>예시 손절가</th><th>예시 목표가</th><th>신호</th><th>종합 점수</th><th>5분봉 MA5</th><th>관찰 메모</th></tr></thead><tbody id="rows"><tr><td colspan="10">아직 스캔하지 않았습니다.</td></tr></tbody></table></div>
</div>
<div class="two">
<div class="panel"><h2>진입·청산 규칙</h2><div class="rules">
<div class="rule"><b class="good">진입 후보</b><span>최근 스윙 고점 2개로 계산한 하향 저항선을 종가가 상향 돌파하고 거래량 확인</span></div>
<div class="rule"><b class="warn">재확인 대기</b><span>돌파 후 저항선 근처까지 되돌림. 아직 반등 종가가 확인되지 않으면 진입 신호 아님</span></div>
<div class="rule"><b class="good">진입 확인</b><span>저항선 근처를 지지하고 양봉 종가가 선 위에 유지되며 거래량 조건 충족</span></div>
<div class="rule"><b class="bad">손절</b><span>재확인 저점 또는 ATR 기반 손절선 아래로 이탈. 수수료·슬리피지는 별도 고려</span></div>
<div class="rule"><b>익절 1차</b><span>설정한 손익비의 1R~2R 목표 구간. 저항선과 겹치면 보수적으로 판단</span></div>
<div class="rule"><b>추세 청산</b><span>돌파선 재이탈 또는 단기 저점 붕괴 시 매도 검토. 자동 주문은 실행하지 않음</span></div>
</div></div>
<div class="panel"><h2>신호 읽는 법</h2>
<p><span class="signal BUY">진입 확인</span> 규칙 조건을 모두 통과한 후보. 매수 권유나 수익 보장은 아님.</p>
<p><span class="signal WATCH">관심</span> 저항선 돌파 또는 재확인 진행 중. 추가 캔들 확인 필요.</p>
<p><span class="signal WAIT">대기</span> 명확한 셋업이 없음.</p>
<p class="note">대각선은 최근 확인된 스윙 고점 두 개를 연결한 단순 모델입니다. 차트마다 선을 직접 긋는 것과 동일하지 않을 수 있으며, 신호는 캔들 종가 확정 후 판단해야 합니다.</p>
</div>
</div>

<div class="panel">
  <h2>BUY 신호 기록</h2>

<button type="button" onclick="exportBuyHistory()">BUY 기록 CSV 백업</button>
  
<div id="buyStats" style="white-space:pre-wrap;line-height:1.8;margin:12px 0 18px;">성과 통계 준비 중...</div>

  <pre id="buyHistory" style="white-space:pre-wrap;font-family:inherit;font-size:14px;">기록을 불러오는 중...</pre>
</div>

<footer>신호 알림 전용 · 자동 주문 없음 · 공개 시장 데이터 기반 · 투자 판단 책임은 사용자에게 있습니다. 과거 조건의 유효성이 미래 수익을 보장하지 않습니다.</footer>
</main>
<script>
let autoTimer=null;
let scanning=false;

function exportBuyHistory() {
  try {
    const history = JSON.parse(
      localStorage.getItem("bithumb_v2_buy_history") || "[]"
    );

    if (!Array.isArray(history) || !history.length) {
      alert("백업할 BUY 기록이 없습니다.");
      return;
    }

    const fields = [
      "market", "tf", "candle_time", "detected_at",
      "signal_price", "price_15m", "price_30m", "price_60m",
      "source_candle_15m", "source_candle_30m",
      "source_candle_60m"
    ];

    const safeCSV = value => {
      const s = String(value ?? "");
      const safe = /^[\s]*[=+\-@]/.test(s) ? "'" + s : s;
      return '"' + safe.replace(/"/g, '""') + '"';
    };

    const csv = [
      fields.join(","),
      ...history.map(x =>
        fields.map(k => safeCSV(x[k])).join(",")
      )
    ].join("\r\n");

    const blob = new Blob(["\uFEFF", csv], {
      type: "text/csv;charset=utf-8;"
    });

    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "bithumb_v2_buy_history.csv";
    document.body.appendChild(a);
    a.click();
    a.remove();

    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) {
    console.error(e);
    alert("CSV 백업에 실패했습니다.");
  }
}

function saveBuySignals(results, tf) {
  try {
    const key = "bithumb_v2_buy_history";
    const history = JSON.parse(localStorage.getItem(key) || "[]");
    const known = new Set(history.map(x =>
      `${x.market}|${x.tf}|${x.candle_time}`
    ));

    for (const x of results) {
      if (x.signal !== "BUY" || !x.candle_time) continue;

      const id = `${x.market}|${tf}|${x.candle_time}`;
      if (known.has(id)) continue;

      history.push({
        market: x.market,
        tf: tf,
        candle_time: x.candle_time,
        signal_price: x.price,
        
        price_15m: null,
        price_30m: null,
        price_60m: null,

        detected_at: new Date().toISOString()
      });
      known.add(id);
    }

    localStorage.setItem(key, JSON.stringify(history.slice(-1000)));
  } catch (e) {
    console.warn("BUY 기록 실패:", e);
  }
}

function showBuyHistory() {
  const area = document.getElementById("buyHistory");
  if (!area) return;

  try {
    const history = JSON.parse(
      localStorage.getItem("bithumb_v2_buy_history") || "[]"
    );

    if (!history.length) {
      area.textContent = "아직 기록된 BUY 신호가 없습니다.";
      return;
    }

    area.textContent = history.slice(-20).reverse().map(x => {
      const result = (p) => {
        if (p == null || Number(x.signal_price) <= 0) return "대기";
        const r = (Number(p) / Number(x.signal_price) - 1) * 100;
        return `${p}원 (${r >= 0 ? "+" : ""}${r.toFixed(2)}%)`;
      };
      
      return `${x.market} | ${x.tf}분봉
신호: ${x.candle_time}
기준가: ${x.signal_price}원
15분: ${result(x.price_15m)}
사용 캔들: ${x.source_candle_15m || "시각 미기록"}
30분: ${result(x.price_30m)}
사용 캔들: ${x.source_candle_30m || "시각 미기록"}
60분: ${result(x.price_60m)}
사용 캔들: ${x.source_candle_60m || "시각 미기록"}
`;

    }).join("\n");

  } catch (e) {
    area.textContent = "BUY 기록을 읽을 수 없습니다.";
  }
}

function showBuyStats() {
  const area = document.getElementById("buyStats");
  if (!area) return;

  try {
    const history = JSON.parse(
      localStorage.getItem("bithumb_v2_buy_history") || "[]"
    );

    const lines = [`누적 BUY 신호: ${history.length}개`];

    for (const m of [15, 30, 60]) {
      const valid = history.filter(x =>
        x[`source_candle_${m}m`] &&
        Number(x.signal_price) > 0 &&
        Number(x[`price_${m}m`]) > 0
      );

      if (!valid.length) {
        lines.push(`${m}분: 집계 가능한 기록 없음`);
        continue;
      }

      const returns = valid.map(x =>
        (Number(x[`price_${m}m`]) / Number(x.signal_price) - 1) * 100
      );

      const avg = returns.reduce((a, b) => a + b, 0) / returns.length;
      const wins = returns.filter(r => r > 0).length;
      const winRate = wins / returns.length * 100;

      lines.push(
        `${m}분 | 평균 ${avg.toFixed(2)}% | 상승 비율 ${winRate.toFixed(1)}% | 표본 ${valid.length}개`
      );
    }

    area.textContent = lines.join("\n");
  } catch (e) {
    area.textContent = "성과 통계를 읽을 수 없습니다.";
  }
}

function updateBuyOutcomes(results, tf) {
  if (String(tf) !== "5") return;

  try {
    const key = "bithumb_v2_buy_history";
    const history = JSON.parse(localStorage.getItem(key) || "[]");
    const byMarket = new Map(results.map(x => [x.market, x]));
    let changed = false;

    for (const item of history) {
      if (String(item.tf) !== "5") continue;

      const x = byMarket.get(item.market);
      if (!x || !x.candle_time) continue;
      const start = Date.parse(item.candle_time + "+09:00") + 5 * 60000;
      if (!Number.isFinite(start)) continue;
      const candles = Array.isArray(x.recent_closes) ? x.recent_closes : [];

      for (const m of [15, 30, 60]) {
        const field = `price_${m}m`;
        if (item[field] != null) continue;

        const target = start + m * 60000;
        
        if (Date.now() < target) continue;

        const match = candles
          .filter(r => {
            const closedAt = Date.parse(r.time + "+09:00") + 5 * 60000;
            return Number.isFinite(closedAt) &&
              closedAt <= target &&
              closedAt >= start;
          })
          .at(-1);


        if (match) {
          item[field] = Number(match.price);
          
          item[`source_candle_${m}m`] = match.time;

          changed = true;
        }
      }
 
    }

    if (changed) localStorage.setItem(key, JSON.stringify(history));
  } catch (e) {
    console.warn("BUY 성과 갱신 실패:", e);
  }
}

function won(x){if(x==null||!Number.isFinite(Number(x)))return "—";return Number(x).toLocaleString("ko-KR",{maximumFractionDigits:4})+"원"}
function pct(x){return x==null?"—":(Number(x)>=0?"+":"")+Number(x).toFixed(2)+"%"}
function money(x){if(x==null)return "—";let n=Number(x);if(n>=1e8)return (n/1e8).toFixed(1)+"억";if(n>=1e4)return (n/1e4).toFixed(0)+"만";return n.toLocaleString("ko-KR")}
async function scan(){
if (scanning) return;
scanning = true;
 const status=document.getElementById("status");status.textContent="빗썸 데이터를 조회하고 조건을 계산하는 중…";
 document.getElementById("connection").textContent="조회 중";
 const qs=new URLSearchParams({tf:document.getElementById("tf").value,limit:document.getElementById("limit").value,vol:document.getElementById("vol").value,rr:document.getElementById("rr").value,page:0});
 let totalPages = 1;
 let scanAll = true;
 
 try{
  const allResults = [];
  let d;
  const failedPages = [];
    async function fetchPage(page) {
    const params = new URLSearchParams(qs);
    params.set("page", page);

    const r = await fetch("/api/scan?" + params.toString(), {
      signal: AbortSignal.timeout(60000)
    });
    if (!r.ok) throw new Error("API 오류: " + r.status);
    return await r.json();
  }

  async function runPage(page) {
    try {
      const result = await fetchPage(page);
      allResults.push(...result.results);
      status.textContent = `${allResults.length}/${result.count}개 코인 분석 중`;
      return result;
    } catch (e) {
      failedPages.push(page + 1);
      return null;
    }
  }

  d = await runPage(0);

  if (!d) {
    throw new Error("첫 페이지 조회 실패");
  }

  totalPages = Math.ceil(d.count / Number(qs.get("limit")));

  for (let page = 1; page < totalPages; page += 3) {
    const pages = Array.from(
      {length: Math.min(3, totalPages - page)},
      (_, i) => page + i
    );
    await Promise.all(pages.map(runPage));
  }
  
  const totalMarkets = d ? d.count : 0;
  if (!d) d = {count: 0, results: [], updated: "-"}; 
  d.results = allResults.sort((a,b)=>(Number(b.score)||0)-(Number(a.score)||0));
  
    saveBuySignals(allResults, document.getElementById("tf").value);

    updateBuyOutcomes(allResults, document.getElementById("tf").value);

    showBuyHistory();

    showBuyStats();

  const candidates = d.results.filter(x => (x.signal === "BUY" || x.signal === "WATCH") && Number(x.value24) >= 500000000).slice(0, 5);
  d.count = totalMarkets;
  d.scanned = allResults.length;
  document.getElementById("connection").textContent="빗썸 공개 API 연결됨";
  document.getElementById("count").innerHTML=d.count+' <small>마켓</small>';
  document.getElementById("buys").innerHTML=allResults.filter(x=>x.signal==="BUY").length+' <small>코인</small>';
  document.getElementById("watches").innerHTML=allResults.filter(x=>x.signal==="WATCH").length+' <small>코인</small>';
  document.getElementById("updated").textContent=d.updated;
  const body=document.getElementById("rows");
  if(!candidates.length){body.innerHTML='<tr><td colspan="10">조건에 맞는 결과가 없습니다.</td></tr>'}
  else body.innerHTML=candidates.map(x=>`<tr><td><b>${x.market}</b></td><td>${won(x.price)}</td><td class="${x.change>=0?'good':'bad'}">${pct(x.change)}</td><td>${money(x.value24)}</td><td>${x.vol_ratio==null?'—':x.vol_ratio.toFixed(2)+'배'}</td><td>${x.resistance}</td><td>${won(x.stop)}</td><td>${won(x.target)}</td><td><span class="signal ${x.signal}">${x.label}</span></td><td>${x.score ?? '-'}</td><td>${x.ma5_info?.ma5 ?? '-'} (${x.ma5_info?.status ?? '-'})</td><td>${x.note}</td></tr>`).join("");
  status.textContent = `완료: ${d.scanned}/${d.count}개 코인 분석 · 실패 페이지 ${failedPages.length}개${failedPages.length ? " (" + failedPages.join(", ") + "페이지)" : ""}`;
 }catch(e){document.getElementById("connection").textContent="연결 실패";status.textContent="오류: "+e.message+" · 잠시 후 다시 시도하세요. Render 무료 서버라면 기동에 시간이 걸릴 수 있습니다."}
finally {
  scanning = false;
}
}
function toggleAuto(){if(autoTimer){clearInterval(autoTimer);autoTimer=null;document.getElementById("autoBtn").textContent="자동 갱신 켜기"}else{scan();autoTimer=setInterval(scan,180000);document.getElementById("autoBtn").textContent="자동 갱신 끄기"}}
</script></body></html>
"""


from threading import local

_API_LOCAL = local()

def api_get(path, params=None, timeout=12):
    session = getattr(_API_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        _API_LOCAL.session = session

    r = session.get(
        BITHUMB + path,
        params=params,
        timeout=timeout,
        headers={
            "accept": "application/json",
            "User-Agent": "BithumbSignalScanner/1.0"
        }
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("status") not in (None, "0000"):
        raise RuntimeError(data.get("message", "Bithumb API error"))
    return data


def get_markets():
    now = time.time()
    if CACHE["markets"] and now - CACHE["at"] < CACHE_SECONDS:
        return CACHE["markets"]
    data = api_get("/v1/market/all", {"isDetails": "false"})
    markets = [x["market"] for x in data if x.get("market", "").startswith("KRW-")]
    CACHE.update(at=now, markets=markets)
    return markets

def candles(market, minute, count=80):
    return api_get(f"/v1/candles/minutes/{minute}", {"market": market, "count": count})

def analyze_5m_ma5(raw):
    """완성된 5분봉으로 MA5와 돌파 상태 분석"""
    if not isinstance(raw, list) or len(raw) < 6:
        return {"ma5": None, "status": "데이터 부족"}

    now_kst = datetime.now(KST)
    completed = []

    for candle in raw:
        start = datetime.fromisoformat(
            candle["candle_date_time_kst"]
        ).replace(tzinfo=KST)

        if start + timedelta(minutes=5) <= now_kst:
            completed.append(candle)

    completed.sort(key=lambda x: x["candle_date_time_kst"])

    if len(completed) < 6:
        return {"ma5": None, "status": "완성된 캔들 부족"}

    closes = [float(x["trade_price"]) for x in completed]
    ma5 = sum(closes[-5:]) / 5
    prev_ma5 = sum(closes[-6:-1]) / 5

    if closes[-2] <= prev_ma5 and closes[-1] > ma5:
        status = "MA5 상향 돌파"
    elif closes[-1] > ma5:
        status = "MA5 위 유지"
    else:
        status = "MA5 아래"

    return {"ma5": round(ma5, 8), "status": status}



def ema(values, period):
    if len(values) < period: return None
    k = 2 / (period + 1)
    out = sum(values[:period]) / period
    for v in values[period:]:
        out = v * k + out * (1-k)
    return out

def atr(rows, period=14):
    if len(rows) < period + 1: return None
    trs=[]
    for i in range(1, len(rows)):
        hi=float(rows[i]["high_price"]); lo=float(rows[i]["low_price"])
        prev=float(rows[i-1]["trade_price"])
        trs.append(max(hi-lo, abs(hi-prev), abs(lo-prev)))
    return sum(trs[-period:])/period if len(trs)>=period else None


def calculate_score(change, ma5_info=None, vol_ratio=None, value24=None, signal=None, rr=None):
    score = 0
    if change >= 3:
        score = 25
    elif change >= 1:
        score = 15
    elif change > 0:
        score = 5
    ma5_info = ma5_info or {}
    ma5_status = ma5_info.get("status", "")

    if ma5_status == "MA5 상향 돌파":
        score += 25
    elif ma5_status == "MA5 위 유지":
        score += 15 
    if vol_ratio is not None:
        if vol_ratio >= 2.0:
            score += 20
        elif vol_ratio >= 1.5:
            score += 15
        elif vol_ratio >= 1.2:
            score += 10

    if value24 is not None:
        if value24 >= 100_000_000_000:
            score += 15
        elif value24 >= 30_000_000_000:
            score += 10
        elif value24 >= 10_000_000_000:
            score += 5
    if signal == "BUY":
        score += 10
    elif signal == "WATCH":
        score += 5

    if rr is not None and rr >= 2:
        score += 5
    return score
    


def scan_market(market, minute, volume_multiple, rr, ticker=None):
  
    raw = candles(market, minute, 80)
    if not isinstance(raw, list) or len(raw) < 35:
        return None
    ma5_info = analyze_5m_ma5(raw if minute == 5 else [])
    # Bithumb candle endpoint returns newest first; analysis uses oldest first.
    
    now_kst = datetime.now(KST)
    completed = []

    for candle in raw:
        start = datetime.fromisoformat(
            candle["candle_date_time_kst"]
        ).replace(tzinfo=KST)

        if start + timedelta(minutes=minute) <= now_kst:
            completed.append(candle)

    if len(completed) < 35:
        return None

    rows = list(reversed(completed))

    closes=[float(x["trade_price"]) for x in rows]
    highs=[float(x["high_price"]) for x in rows]
    lows=[float(x["low_price"]) for x in rows]
    volumes=[float(x["candle_acc_trade_volume"]) for x in rows]
    current=closes[-1]
    # Candidate pivot highs: higher than two neighbors on either side.
    pivots=[]
    for i in range(2, len(highs)-2):
        if highs[i] >= max(highs[i-2:i]) and highs[i] > max(highs[i+1:i+3]):
            pivots.append(i)
    # Use last two pivots, only treat a downward line as diagonal resistance.
    line_now=None; line_prev=None; resistance="선 없음"; signal="WAIT"; label="대기"
    note="뚜렷한 하향 저항 돌파 조건 없음"
    if len(pivots)>=2:
        a,b=pivots[-2],pivots[-1]
        if highs[b] < highs[a] and b>a:
            slope=(highs[b]-highs[a])/(b-a)
            line_now=highs[b]+slope*((len(rows)-1)-b)
            line_prev=highs[b]+slope*((len(rows)-2)-b)
            tolerance=0.004
            prev_close=closes[-2]; prev_vol=volumes[-2]
            avg_vol=sum(volumes[-21:-1])/20 if len(volumes)>=21 else 0
            vol_ratio=(volumes[-1]/avg_vol) if avg_vol>0 else None
            atrv=atr(rows)
            # retest tolerance around the line; last candle must hold line and close green.
            
            breakout_seen = False
          
            breakout_index = None

            for i in range(max(b + 1, len(rows) - 8), len(rows) - 2):
                previous_line = highs[b] + slope * ((i - 1) - b)
                breakout_line = highs[b] + slope * (i - b)
                if (
                    closes[i - 1] <= previous_line * (1 + tolerance)
                    and closes[i] > breakout_line * (1 + tolerance)
                ):
                    breakout_seen = True
                  
                    breakout_index = i

                    break

            support_held = (
                breakout_index is not None
                and all(
                    closes[j] >= (highs[b] + slope * (j - b)) * (1 - tolerance)
                    for j in range(breakout_index + 1, len(rows) - 1)
                )
            )

            close_above=current > line_now * (1+tolerance)
            retest=(lows[-1] <= line_now*(1+tolerance) and current >= line_now and current > closes[-2])
            vol_ok=vol_ratio is not None and vol_ratio >= volume_multiple
            # If breakout was very recent, mark WATCH; only retest bounce is BUY.
            
            
            if close_above and vol_ok and retest and breakout_seen and support_held:


                signal="BUY";label="진입 확인";resistance="돌파선 지지 확인"
                note="종가가 저항선 위 + 재확인 반등 + 거래량 조건"
            elif close_above or (current >= line_now*(1-tolerance) and vol_ok):
                signal="WATCH";label="관심";resistance="돌파/재확인 중"
                note="추가 캔들에서 지지 유지 여부 확인"
            else:
                resistance="저항선 아래"
            if line_now and atrv:
                stop=max(0.0, min(lows[-1], current-1.2*atrv))
                risk=max(0.0,current-stop)
                target=current+rr*risk
            else: stop=None; target=None
        else:
            vol_ratio=None;stop=None;target=None
    else:
        vol_ratio=None;stop=None;target=None
    # Momentum volume ratio for table even when no descending trendline.
    if 'vol_ratio' not in locals() or vol_ratio is None:
        avg_vol=sum(volumes[-21:-1])/20 if len(volumes)>=21 else 0
        vol_ratio=volumes[-1]/avg_vol if avg_vol>0 else None
    # Don't label a BUY without a valid descending line and actual retest logic.
    ticker = ticker or {}
    change=float(ticker.get("signed_change_rate", 0))*100 if ticker else ((current/closes[-2]-1)*100 if closes[-2] else 0)
    value24=float(ticker.get("acc_trade_price_24h", 0)) if ticker else 0
    score = calculate_score(
        change, ma5_info, vol_ratio, value24, signal, rr
    )
    return {"market":market,"price":current,"change":change,"value24":value24,
            "vol_ratio":vol_ratio,"resistance":resistance,"signal":signal,"label":label,
            
            "candle_time": rows[-1]["candle_date_time_kst"],

            "recent_closes": [{"time": r["candle_date_time_kst"], "price": float(r["trade_price"])} for r in rows[-36:]] if minute == 5 else [],

            "note":note,"stop":stop,"target":target,"target":target,"ma5_info":ma5_info,"score":score}

@app.get("/")
def index():
    return render_template_string(HTML)

@app.get("/api/scan")
def scan():
    scan_start = time.perf_counter()
    try:
        minute=int(request.args.get("tf","5"))
        if minute not in (1,3,5,10,30): minute=5
        limit=max(10,min(80,int(request.args.get("limit","40"))))
        page = max(0, int(request.args.get("page", "0")))
        volume_multiple=max(1.0,min(3.0,float(request.args.get("vol","1.5"))))
        rr=max(1.0,min(4.0,float(request.args.get("rr","2"))))
        markets=get_markets()
        # Fetch tickers in bulk for ranking, then analyze highest 24h turnover markets.
        
        tick_by_market = CACHE.get("tickers", {}) if time.time() - CACHE.get("tickers_at", 0) < 300 else {}
        batch_size=50
        for i in (range(0, len(markets), batch_size) if len(tick_by_market) != len(markets) else []):
            batch=markets[i:i + batch_size]
            try:
                tickers=api_get("/v1/ticker", {"markets": ",".join(batch)})
                if isinstance(tickers, list):
                    tick_by_market.update({
                        x.get("market"): x for x in tickers
                        if x.get("market")
                    })
            except Exception:
                continue

        
      
      
        if len(tick_by_market) == len(markets): CACHE.update(tickers=tick_by_market.copy(), tickers_at=time.time())     
        ranked=sorted(markets,key=lambda m: float(tick_by_market.get(m,{}).get("acc_trade_price_24h",0)),reverse=True)[page * limit:(page + 1) * limit]
        results=[]
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(
                    scan_market, market, minute,
                    volume_multiple, rr, tick_by_market.get(market)
                )
                for market in ranked
            ]
            for future in as_completed(futures):
                try:
                    item = future.result()
                    if item:
                        results.append(item)
                except Exception:
                    continue
            
        
                
                
            
                
        order={"BUY":0,"WATCH":1,"WAIT":2,"SELL":3}
        results.sort(key=lambda x:(order.get(x["signal"],9),-(x.get("vol_ratio") or 0)))
        return jsonify({"count":len(markets),"scanned":len(results),"results":results,
                        "updated":f"{datetime.now(KST):%H:%M:%S} KST · {time.perf_counter()-scan_start:.1f}초"})
    except Exception as e:
        return jsonify({"error":str(e)}), 502

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","10000")))
