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
.c5-grade{display:inline-block;margin-left:5px;border-radius:6px;padding:3px 6px;font-size:11px;font-weight:800;vertical-align:middle}
.c5-grade.A{color:#70f1bf;background:#153d33}.c5-grade.B{color:#9dc8ff;background:#203653}.c5-grade.C{color:#ffcd87;background:#453321}.c5-grade.unknown{color:#c7d3eb;background:#303b50}
.two{display:grid;grid-template-columns:1.2fr 1fr;gap:14px}.rules{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.rule{padding:12px;border-radius:10px;background:#10182a;border:1px solid #26334b}.rule b{display:block;font-size:13px;margin-bottom:4px}.rule span{font-size:12px;color:var(--muted)}
.note{font-size:12px;color:var(--muted);line-height:1.65}.good{color:var(--green)}.warn{color:var(--yellow)}.bad{color:var(--red)}
footer{margin-top:18px;color:var(--muted);font-size:11px}
@media(max-width:760px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.two{grid-template-columns:1fr}.rules{grid-template-columns:1fr}h1{font-size:22px}.v{font-size:21px}}
#rows td:first-child[data-signal]::after {
  content: attr(data-signal);
  display: inline-block;
  margin-left: 6px;
  padding: 2px 6px;
  border-radius: 6px;
  font-size: 10px;
  font-weight: 700;
  vertical-align: middle;
}

#rows td:first-child[data-signal="BUY"]::after {
  color: #4ade80;
  background: #123d35;
}

#rows td:first-child[data-signal="WATCH"]::after {
  color: #fbbf24;
  background: #40351e;
}

/* 코인 상세 분석 카드형 디자인 */
#coinDetailTitle {
  display: block;
  font-size: 18px;
  font-weight: 800;
  margin-bottom: 14px;
}

.coin-detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  white-space: normal;
}

.coin-detail-item {
  min-width: 0;
  background: #1b2943;
  border: 1px solid #34405a;
  border-radius: 12px;
  padding: 12px;
  white-space: normal;
}

.coin-detail-item.wide {
  grid-column: 1 / -1;
}

.coin-detail-label {
  font-size: 12px;
  color: #9ba9c4;
  margin-bottom: 6px;
}

.coin-detail-value {
  font-size: 16px;
  font-weight: 700;
  color: #edf3ff;
  overflow-wrap: anywhere;
}

.coin-detail-value.buy,
.coin-detail-value.target {
  color: #4ade80;
}

.coin-detail-value.watch {
  color: #fbbf24;
}

.coin-detail-value.stop {
  color: #ff8996;
}

/* 상세 분석 제목 BUY/WATCH 배지 */
#coinDetailTitle[data-signal]::after {
  content: attr(data-signal);
  float: right;
  margin-left: 8px;
  padding: 3px 9px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 800;
  line-height: 1.4;
}

#coinDetailTitle[data-signal="BUY"]::after {
  color: #4ade80;
  background: #123d35;
}

#coinDetailTitle[data-signal="WATCH"]::after {
  color: #fbbf24;
  background: #40351e;
}

</style>
</head>
<body><main>
<div class="top">
  <div><h1>빗썸 KRW · Signal Scanner</h1><div class="sub">5분봉 5이평(C5) 첫 회복을 포착하는 단타 신호 보조 도구</div></div>
  <div class="pill" id="connection">연결 확인 전</div>
</div>
<div class="grid">
  <div class="card"><div class="k">검색 대상</div><div class="v" id="count">— <small>마켓</small></div></div>
  <div class="card"><div class="k">진입 확인</div><div class="v good" id="buys">— <small>코인</small></div></div>
  <div class="card"><div class="k">관심 후보</div><div class="v" id="watches">— <small>코인</small></div></div>
  <div class="card">
  <div class="k">마지막 갱신</div>
  <div class="v" style="font-size:17px" id="updated">—</div>
  <div id="scanAge" style="font-size:12px;color:#9ba9c4;margin-top:6px;">스캔 기록 없음</div>
</div>

</div>
<div id="buyAlert" role="status" aria-live="polite" style="display:none;margin:14px 0;padding:14px;border:1px solid #2b8a68;border-radius:12px;color:#7bedbb;font-weight:700;"></div>


<details class="panel">
  <summary style="cursor:pointer;font-size:16px;font-weight:700;">
    최근 24시간 BUY 기록 · 눌러서 보기
  </summary>
  <div id="recentBuyAlerts"
       style="white-space:pre-line;line-height:1.8;margin-top:12px;">
    최근 신호를 불러오는 중...
  </div>
</details>


<div class="panel">
<h2>스캔 설정 · C5 신호</h2>
<div style="margin:10px 0 14px;">
  <a href="/backtest-daytrade"
     style="display:inline-block;
            padding:10px 12px;
            background:#1b2943;
            border:1px solid #354765;
            color:#edf3ff;
            border-radius:9px;
            text-decoration:none;
            font-weight:700;">
    📊 단타 A/B 검증판 열기
  </a>
</div>
<div class="controls">
  <div class="field"><label for="tf">캔들 주기</label><select id="tf"><option value="5" selected>5분 · C5 고정</option></select></div>
  <div class="field"><label for="limit">검색 코인 수</label><select id="limit"><option>20</option><option selected>40</option><option>80</option></select></div>
  <div class="field"><label for="vol">거래량 참고 기준 (BUY 필터 아님)</label><select id="vol"><option value="1.2">1.2배</option><option value="1.5" selected>1.5배</option><option value="2">2.0배</option></select></div>
  <div class="field"><label for="rr">C5 예시 손익비</label><select id="rr"><option value="1.5" selected>손절 -1% / 익절 +1.5%</option></select></div>
  <button onclick="scan()">지금 스캔</button><button class="secondary" onclick="toggleAuto()" id="autoBtn">자동 갱신 켜기</button>
</div>
<div class="status" id="status">스캔 버튼을 눌러 빗썸 공개 시세 데이터를 조회하세요. 처음 요청은 무료 서버 기동으로 지연될 수 있습니다.</div>
</div>
<div class="panel">
<h2>스캔 결과</h2>
<div id="scanResultsNotice"
     role="status"
     aria-live="polite"
     style="display:none;margin:10px 0;padding:10px 12px;border:1px solid #fbbf24;border-radius:10px;background:#40351e;color:#fbbf24;font-size:13px;font-weight:700;">
  ⚠️ 새 스캔 진행 중 — 현재 표는 이전 스캔 결과입니다.
</div>

<label for="resultLimit">표시 코인 수 <select id="resultLimit"><option value="5" selected>5개</option><option value="10">10개</option><option value="20">20개</option></select></label>
<div id="resultCount" style="margin:12px 0;color:#7bedbb;font-size:14px;">조건 충족 후보 계산 전</div>
<label for="resultSort">정렬 기준 <select id="resultSort"><option value="default">기본 추천순</option><option value="volume">24시간 거래대금 높은순</option><option value="change">24시간 상승률 높은순</option></select></label>
<div class="field" style="margin:12px 0;"><label for="coinSearch">🔎 코인 검색</label><input type="search" id="coinSearch" placeholder="코인 검색 (예: XRP, BTC)" autocomplete="off"></div>
<label for="signalFilter">신호 필터 <select id="signalFilter"><option value="ALL">전체 후보</option><option value="BUY">BUY만</option><option value="WATCH">WATCH만</option></select></label>
<div class="tablewrap"><table><thead><tr><th>마켓</th><th>확정봉 종가</th><th>24h 변동</th><th>거래대금</th><th>거래량 배수</th><th>C5 회복 상태</th><th>예시 손절가</th><th>예시 목표가</th><th>신호</th><th>종합 점수</th><th>5분봉 MA5</th><th>관찰 메모</th></tr></thead><tbody id="rows"><tr><td colspan="10">아직 스캔하지 않았습니다.</td></tr></tbody></table></div>
<div id="coinDetail" style="display:none;margin-top:14px;padding:14px;border:1px solid #34405a;border-radius:10px;">
  <strong id="coinDetailTitle">코인 상세 분석</strong>
<div id="coinDetailScanTime"
     style="margin-top:6px;margin-bottom:12px;font-size:12px;color:#9ba9c4;">
  스캔 기준 시각 확인 중
</div>

<div style="margin:12px 0;padding:12px;background:#1b2943;border:1px solid #34405a;border-radius:10px;">
  <label for="tradeFeeRate" style="display:block;margin-bottom:8px;font-size:13px;color:#a9b7d2;">
    거래 수수료 설정 (매수·매도 각각)
  </label>
  <select id="tradeFeeRate"
          style="width:100%;padding:10px;background:#11172b;color:#edf3ff;border:1px solid #34405a;border-radius:8px;">
    <option value="0.25">기본 수수료 0.25%</option>
    <option value="0.04">할인 쿠폰 0.04%</option>
    <option value="0">무료 수수료 0%</option>
  </select>
</div>

<div id="tradeRiskWarning"
     role="status"
     aria-live="polite"
     style="display:none;margin:12px 0;padding:12px 14px;background:#40272c;border:1px solid #fb7185;border-radius:10px;color:#fecdd3;font-size:13px;font-weight:700;line-height:1.6;">
  ⚠️ 진입 전 위험 조건을 확인하세요.
</div>

<div style="margin:12px 0;padding:14px;background:#1b2943;border:1px solid #34405a;border-radius:12px;">

  <div style="font-size:16px;font-weight:800;margin-bottom:12px;">
    💰 투자금별 예상 손익 계산기
  </div>

  <label for="tradeCapital" style="display:block;font-size:13px;color:#9ba9c4;margin-bottom:6px;">
    투자금 선택 (매수 수수료 포함 총액)
  </label>

  <select id="tradeCapital" style="width:100%;margin-bottom:12px;">
    <option value="100000">10만 원</option>
    <option value="300000" selected>30만 원</option>
    <option value="500000">50만 원</option>
    <option value="1000000">100만 원</option>
  </select>

  <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;">

    <div class="coin-detail-item">
      <div class="coin-detail-label">손절 시 예상 손익</div>
      <div id="tradeCapitalLoss" class="coin-detail-value stop">
        계산 대기
      </div>
    </div>

    <div class="coin-detail-item">
      <div class="coin-detail-label">목표가 도달 시 예상 손익</div>
      <div id="tradeCapitalGain" class="coin-detail-value target">
        계산 대기
      </div>
    </div>

  </div>

  <div style="font-size:11px;color:#9ba9c4;margin-top:10px;">
    수수료 반영 예상 금액 · 슬리피지 제외 · 자동 주문 없음
  </div>

</div>

  <div id="coinDetailBody" style="margin-top:10px;white-space:pre-wrap;line-height:1.7;"></div>
</div>
</div>
<div class="two">
<div class="panel"><h2>진입·청산 규칙</h2><div class="rules">
<div class="rule"><b class="good">진입 후보</b><span>직전 5분봉 종가가 당시 5이평 아래에서 마감</span></div>
<div class="rule"><b class="warn">재확인 대기</b><span>현재 5분봉이 확정되기 전에는 BUY로 인정하지 않음</span></div>
<div class="rule"><b class="good">진입 확인 · C5</b><span>양봉 종가가 5이평 위로 처음 회복하고 5이평이 전봉보다 상승 · 동일 종목 BUY 간 최소 60분 간격</span></div>
<div class="rule"><b class="bad">손절 참고</b><span>신호봉 종가 기준 -1.0% · 실제 진입가는 다음 봉 시가에 따라 달라짐</span></div>
<div class="rule"><b>익절 참고</b><span>신호봉 종가 기준 +1.5% · 실제 체결 시 목표가는 달라짐</span></div>
<div class="rule"><b>보유 관찰</b><span>이전 C5 검증과 같이 최대 60분 관찰을 참고 · 자동 주문은 실행하지 않음</span></div>
</div></div>
<div class="panel"><h2>신호 읽는 법</h2>
<p><span class="signal BUY">진입 확인</span> 규칙 조건을 모두 통과한 후보. 매수 권유나 수익 보장은 아님.</p>
<p><span class="signal WATCH">관심</span> 저항선 돌파 또는 재확인 진행 중. 추가 캔들 확인 필요.</p>
<p><span class="signal WAIT">대기</span> 명확한 셋업이 없음.</p>
<p class="note">C5는 단순이동평균(SMA5) 회복 신호입니다. 거래량 배수와 점수는 참고 지표이며 BUY 발생 조건에 추가되지 않습니다. 실전 스캐너 표시 기준가는 확정 신호봉 종가이므로 다음 봉 실제 진입가와 다를 수 있습니다.</p>
<p class="note">🔬 C5 관찰 등급(실험): A = 15분 상승 + 거래량 1.0배 이상, B = 15분 중립 + 거래량 0.8배 이상, C = 그 외 유효 데이터. 데이터가 부족하면 미확인으로 표시합니다. 등급은 BUY 차단 조건이 아니며 수익 보장이나 검증된 예측 모델이 아닙니다.</p>
</div>
</div>

<div class="panel">
  <h2>BUY 신호 기록</h2>

<button type="button" onclick="exportBuyHistory()">BUY 기록 CSV 백업</button>
<input type="file" id="buyRestoreFile" accept=".csv,text/csv" style="display:none" onchange="importBuyHistory(event)">
<button type="button" onclick="document.getElementById('buyRestoreFile').click()">BUY 기록 CSV 복원</button>
 
<div id="buyStats" style="white-space:pre-wrap;line-height:1.8;margin:12px 0 18px;">성과 통계 준비 중...</div>

<div id="buyCompareStats"
     style="white-space:pre-wrap;line-height:1.8;margin:12px 0;padding:12px;border:1px solid #34405a;border-radius:10px;background:#1b2943;font-size:13px;">
  📊 BUY 신호 성과 비교
  강화 BUY 비교 데이터 수집 준비 중...
</div>

<details style="margin-top:14px;">
  <summary style="display:block;cursor:pointer;background:#123d35;color:#4ade80;border:1px solid #2b8a68;border-radius:10px;padding:12px 14px;font-size:15px;font-weight:800;">
    📂 BUY 목록 · 눌러서 보기
  </summary>

  <pre id="buyHistory"
       style="white-space:pre-wrap;font-family:inherit;font-size:14px;margin:14px 0 0;">
    기록을 불러오는 중...
  </pre>
</details>
  
</div>

<footer>신호 알림 전용 · 자동 주문 없음 · 공개 시장 데이터 기반 · 투자 판단 책임은 사용자에게 있습니다. 과거 조건의 유효성이 미래 수익을 보장하지 않습니다.</footer>
</main>
<script>
let autoTimer=null;
let scanning=false;

// C5 등급은 이미 조회된 거래량 배수와 15분 추세만 활용한다.
// BUY 발생 여부와 스캔 API 호출에는 영향을 주지 않는다.
function c5Grade(x) {
  const ratio = x?.vol_ratio;
  const trend = x?.trend15;
  if (ratio == null || ratio === "" || !Number.isFinite(Number(ratio)))
    return "미확인";
  const v = Number(ratio);
  if (trend === "상승" && v >= 1.0) return "A";
  if (trend === "중립" && v >= 0.8) return "B";
  if (["상승", "중립", "하락"].includes(trend)) return "C";
  return "미확인";
}

function isC5BuyHistory(x) {
  return x && String(x.tf) === "5" &&
    (x.strategy === "C5" || x.ma5_status === "MA5 상향 돌파");
}

function c5HistoryGrade(x) {
  if (!isC5BuyHistory(x)) return null;
  return ["A", "B", "C", "미확인"].includes(x.c5_grade)
    ? x.c5_grade : c5Grade(x);
}

function c5GradeBadge(x) {
  if (x.signal !== "BUY") return "";
  const grade = c5Grade(x);
  const css = grade === "미확인" ? "unknown" : grade;
  return `<span class="c5-grade ${css}" title="C5 실험용 등급 · BUY 판단에는 영향 없음">${grade}등급</span>`;
}

function parseBuyCsv(text) {
  const rows = [];
  let row = [];
  let value = "";
  let quoted = false;
  let s = String(text);

  if (s.charCodeAt(0) === 65279) {
    s = s.slice(1);
  }

  for (let i = 0; i < s.length; i++) {
    const c = s[i];

    if (c === '"') {
      if (quoted && s[i + 1] === '"') {
        value += '"';
        i++;
      } else {
        quoted = !quoted;
      }
    } else if (c === "," && !quoted) {
      row.push(value);
      value = "";
    } else if (!quoted && (c.charCodeAt(0) === 10 || c.charCodeAt(0) === 13)) {
      if (c.charCodeAt(0) === 13 && s.charCodeAt(i + 1) === 10) i++;
      row.push(value);
      if (row.some(x => x !== "")) rows.push(row);
      row = [];
      value = "";
    } else {
      value += c;
    }
  }

  if (quoted) throw new Error("CSV 형식 오류");
  row.push(value);
  if (row.some(x => x !== "")) rows.push(row);

  return rows;
}

async function importBuyHistory(event) {

  alert("CSV 복원 함수 실행 확인!");

  const input = event.target;
  const file = input.files?.[0];
  if (!file) return;

  try {
    if (file.size > 2 * 1024 * 1024) {
      throw new Error("파일 크기가 너무 큽니다.");
    }

    const rows = parseBuyCsv(await file.text());
    if (rows.length < 2) {
      throw new Error("복원할 기록이 없습니다.");
    }

    const fields = [
      "market", "tf", "candle_time", "detected_at",
      "signal_price", "price_15m", "price_30m",
      "price_60m", "source_candle_15m",
      "source_candle_30m", "source_candle_60m"
    ];

    const header = rows[0].map(x => x.trim());
    if (fields.some(k => !header.includes(k))) {
      throw new Error("BUY 백업 파일 형식이 아닙니다.");
    }

    const key = "bithumb_v2_buy_history";
    const current = JSON.parse(
      localStorage.getItem(key) || "[]"
    );

    if (!Array.isArray(current)) {
      throw new Error("기존 기록 형식 오류");
    }

    const known = new Set(current.map(x =>
      `${x.market}|${x.tf}|${x.candle_time}`
    ));

    const added = [];

    for (const row of rows.slice(1)) {
      if (row.length !== header.length) {
        throw new Error("CSV 열 개수가 맞지 않습니다.");
      }

      const x = Object.fromEntries(
        fields.map(k => [k, row[header.indexOf(k)]])
      );

      if (!/^KRW-[A-Z0-9]+$/.test(x.market) ||
          !/^(1|3|5|10|30)$/.test(x.tf) ||
          !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(x.candle_time) ||
          !(Number(x.signal_price) > 0)) {
        throw new Error("BUY 기록 데이터 오류");
      }

      for (const k of [
        "signal_price", "price_15m",
        "price_30m", "price_60m"
      ]) {
        if (x[k] === "") {
          x[k] = null;
        } else {
          x[k] = Number(x[k]);
          if (!Number.isFinite(x[k]) || x[k] <= 0) {
            throw new Error("가격 데이터 오류");
          }
        }
      }

      const id = `${x.market}|${x.tf}|${x.candle_time}`;
      if (known.has(id)) continue;

      known.add(id);
      added.push(x);
    }

    if (!added.length) {
      alert("새롭게 복원할 기록이 없습니다.");
      return;
    }

    if (!confirm(`${added.length}개 기록을 추가할까요?`)) {
      return;
    }

    localStorage.setItem(
      key, JSON.stringify([...current, ...added])
    );

    showBuyHistory();
    showBuyStats();

    alert(`${added.length}개 BUY 기록 복원 완료!`);
  } catch (e) {
    alert("CSV 복원 실패: " + e.message);
  } finally {
    input.value = "";
  }
}

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
      "signal_price", "price_15m", "price_30m",
      "price_60m",
      "source_candle_15m", "source_candle_30m",
      "source_candle_60m",
      "value24", "vol_ratio", "score",
      "ma5_status", "trend15", "stop_price",
      "target_price", "strategy", "c5_grade"
    ];

    const safeCSV = value => {
      const s = String(value ?? "");
      const safe = /^[\s]*[=+\-@]/.test(s) ? "'" + s : s;
      return '"' + safe.replace(/"/g, '""') + '"';
    };

    const csv = [
      fields.join(","),
      ...history.map(x =>
        fields.map(k => safeCSV(
          k === "c5_grade" ? (c5HistoryGrade(x) ?? "") :
          k === "strategy" ? (isC5BuyHistory(x) ? "C5" : (x[k] ?? "")) : x[k]
        )).join(",")
      )
    ].join("\r\n");
    const blob = new Blob([csv], {

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

    const newBuyAlerts = [];

    for (const x of results) {
      if (x.signal !== "BUY" || !x.candle_time) continue;

      const id = `${x.market}|${tf}|${x.candle_time}`;
      if (known.has(id)) continue;

      history.push({
        market: x.market,
        tf: tf,
        candle_time: x.candle_time,
        signal_price: x.price,

        value24: x.value24 ?? null,
        vol_ratio: x.vol_ratio ?? null,
        score: x.score ?? null,
        ma5_status: x.ma5_info?.status ?? null,
        trend15: x.trend15 ?? null,
        strategy: "C5",
        c5_grade: c5Grade(x),
        stop_price: x.stop ?? null,
        target_price: x.target ?? null,

        price_15m: null,
        price_30m: null,
        price_60m: null,


        detected_at: new Date().toISOString()
      });
      known.add(id);
      

      newBuyAlerts.push(`🚨 새 BUY 신호: ${x.market} | C5 ${c5Grade(x)}등급 | ${tf}분봉 | ${x.candle_time}`);


    }

    localStorage.setItem(key, JSON.stringify(history.slice(-1000)));
    
    if (newBuyAlerts.length > 0) {
      const alertBox = document.getElementById("buyAlert");
      if (alertBox) {
        alertBox.textContent = newBuyAlerts.join("\n");
        alertBox.style.whiteSpace = "pre-line";
        alertBox.style.display = "block";
      }
    }

  } catch (e) {
    console.warn("BUY 기록 실패:", e);
  }
}

function formatBuyElapsed(candleTime) {
  const s = String(candleTime || "");
  const time = Date.parse(
    /(?:Z|[+-]\d{2}:\d{2})$/.test(s) ? s : s + "+09:00"
  );

  if (!Number.isFinite(time)) return "시간 확인 불가";

  const minutes = Math.floor((Date.now() - time) / 60000);

  if (minutes < 0) return "시각 확인 필요";
  if (minutes < 60) return `${minutes}분 전`;
  if (minutes < 1440)
    return `${Math.floor(minutes / 60)}시간 ${minutes % 60}분 전`;

  return `${Math.floor(minutes / 1440)}일 전`;
}

function showRecentBuyAlerts() {
  const area = document.getElementById("recentBuyAlerts");
  if (!area) return;

  try {
    const history = JSON.parse(
      localStorage.getItem("bithumb_v2_buy_history") || "[]"
    );

    if (!Array.isArray(history)) {
      throw new Error("BUY 기록 형식 오류");
    }

    const recent = history
      .filter(x => {
  if (!x || !x.market || !x.candle_time) return false;

  const s = String(x.candle_time);
  const signalTime = Date.parse(
    /(?:Z|[+-]\d{2}:\d{2})$/.test(s) ? s : s + "+09:00"
  );

  const ageMs = Date.now() - signalTime;

  return Number.isFinite(signalTime) &&
    ageMs >= 0 &&
    ageMs < 24 * 60 * 60 * 1000;
})

      .sort((a, b) =>
        String(b.detected_at || b.candle_time)
          .localeCompare(String(a.detected_at || a.candle_time))
      )
      .slice(0, 5);

    area.textContent = recent.length
      ? recent.map(x =>
          `🚨 ${x.market} | ${x.tf}분봉${c5HistoryGrade(x) ? ` | C5 ${c5HistoryGrade(x)}등급` : ""} | ${x.candle_time} | ⏱ ${formatBuyElapsed(x.candle_time)}`

        ).join("\n")
      : "아직 기록된 BUY 신호가 없습니다.";

  } catch (e) {
    area.textContent = "최근 BUY 신호를 불러오지 못했습니다.";
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
${c5HistoryGrade(x) ? `C5 관찰 등급: ${c5HistoryGrade(x)}등급\n` : ""}기준가: ${x.signal_price}원
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

    const compareBox = document.getElementById("buyCompareStats");

    if (compareBox) {
      const comparable = history.filter(x =>
        x &&
        x.vol_ratio != null &&
        x.score != null &&
        Number.isFinite(Number(x.vol_ratio)) &&
        Number.isFinite(Number(x.score))
      );

      const enhanced = comparable.filter(x =>
        Number(x.vol_ratio) >= 1.5 &&
        Number(x.score) >= 50
      );

      const getStats = (group, m) => {
        const valid = group.filter(x =>
          x[`source_candle_${m}m`] &&
          Number(x.signal_price) > 0 &&
          Number(x[`price_${m}m`]) > 0
        );

        if (!valid.length) return "평가 완료 0개";

        const returns = valid.map(x =>
          (Number(x[`price_${m}m`]) / Number(x.signal_price) - 1) * 100
        );

        const avg = returns.reduce((a, b) => a + b, 0)
          / returns.length;

        const winRate = returns.filter(r => r > 0).length
          / returns.length * 100;

        return `평균 ${avg >= 0 ? "+" : ""}${avg.toFixed(2)}% | 상승 ${winRate.toFixed(1)}% | ${valid.length}개`;
      };

      const enhancedText = [15, 30, 60].map(m =>
        `${m}분 후\n` +
        `기존 BUY(분석정보 보유): ${getStats(comparable, m)}\n` +
        `강화 BUY: ${getStats(enhanced, m)}`
      ).join("\n\n");

      const trendBuy = comparable.filter(x =>
        Number(x.tf) === 5
      );

      const trendGroups = [
        ["상승", trendBuy.filter(x => x.trend15 === "상승")],
        ["중립", trendBuy.filter(x => x.trend15 === "중립")],
        ["하락", trendBuy.filter(x => x.trend15 === "하락")],
        ["데이터 부족/미확인", trendBuy.filter(x =>
          !["상승", "중립", "하락"].includes(x.trend15)
        )]
      ];

      const trendText = trendBuy.length === 0
        ? "새로운 5분봉 BUY 기록 대기 중"
        : [15, 30, 60].map(m =>
            `${m}분 후\n` +
            trendGroups.map(([label, group]) =>
              `${label} BUY: ${getStats(group, m)}`
            ).join("\n")
          ).join("\n\n");

      compareBox.textContent =
        "📊 BUY 신호 성과 비교 (실험)\n" +
        `분석 가능한 신규 BUY: ${comparable.length}개\n` +
        `강화 조건 통과: ${enhanced.length}개\n` +
        "강화 기준: 거래량 1.5배 이상 + 점수 50 이상\n\n" +
        enhancedText + "\n\n" +
        "📈 15분봉 추세별 BUY 성과 (5분봉 BUY만)\n" +
        `분석 대상: ${trendBuy.length}개\n` +
        trendGroups.map(([label, group]) =>
          `${label} ${group.length}개`
        ).join(" | ") +
        "\n\n" + trendText;

      // C5 기록만 분리해서 등급별 이후 가격 변화를 비교한다.
      // 기존 저항선 BUY는 C5 성적에 포함하지 않는다.
      const c5Records = history.filter(isC5BuyHistory);
      const gradeGroups = ["A", "B", "C", "미확인"].map(grade => [
        grade, c5Records.filter(x => c5HistoryGrade(x) === grade)
      ]);
      const gradeText = [15, 30, 60].map(m =>
        `${m}분 후\n` + gradeGroups.map(([grade, group]) =>
          `${grade}등급: ${getStats(group, m)}`
        ).join("\n")
      ).join("\n\n");

      compareBox.textContent += "\n\n🔬 C5 관찰 등급별 성과 (실험)\n" +
        `C5 신호 총 ${c5Records.length}개 | ` +
        gradeGroups.map(([grade, group]) => `${grade} ${group.length}개`).join(" · ") +
        "\nA: 상승·거래량 ≥1.0 / B: 중립·거래량 ≥0.8 / C: 나머지 (정보 부족은 미확인)\n" +
        "기준가 대비 15/30/60분 가격 변화 · 수수료 및 중간 익절/손절 미반영\n\n" +
        gradeText;
    }


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

function bithumbGuideWon(value) {
  if (value == null || value === "") return "—";

  const price = Number(value);
  if (!Number.isFinite(price) || price <= 0) return "—";

  let tick;

  if (price >= 1000000) tick = 1000;
  else if (price >= 500000) tick = 500;
  else if (price >= 100000) tick = 100;
  else if (price >= 50000) tick = 50;
  else if (price >= 10000) tick = 10;
  else if (price >= 5000) tick = 5;
  else if (price >= 100) tick = 1;
  else if (price >= 10) tick = 0.01;
  else if (price >= 1) tick = 0.001;
  else tick = 0.0001;

  const rounded = Math.round(price / tick) * tick;
  const digits = tick >= 1 ? 0 :
    tick === 0.01 ? 2 :
    tick === 0.001 ? 3 : 4;

  return rounded.toLocaleString("ko-KR", {
    maximumFractionDigits: digits
  }) + "원";
}

async function scan(){
if (scanning) return;
scanning = true;
const coinDetailPanel = document.getElementById("coinDetail");
if (coinDetailPanel) {
  coinDetailPanel.style.display = "none";
}

const scanResultsNotice = document.getElementById("scanResultsNotice");

if (scanResultsNotice) {
  const hasPreviousScan = Boolean(
    document.getElementById("updated")?.dataset.scanResultsAt
  );

  scanResultsNotice.style.display = "block";

  scanResultsNotice.textContent = hasPreviousScan
    ? "⚠️ 새 스캔 진행 중 — 현재 표는 이전 스캔 결과입니다."
    : "⏳ 첫 스캔 진행 중 — 결과를 불러오는 중입니다.";
}

 const buyAlertBox = document.getElementById("buyAlert");
    if (buyAlertBox) {
      buyAlertBox.textContent = "";
      buyAlertBox.style.display = "none";
    }

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

    showRecentBuyAlerts();

  
const resultSort = document.getElementById("resultSort").value;


const allCandidates = d.results

.filter(x => {
  if (x.signal === "BUY") {
    return Number(x.value24) >= 500000000;
  }

if (x.signal === "WATCH") {
  return Number(x.value24) >= 500000000;
}


  return false;
})
  

.sort((a, b) => {
  if (resultSort === "volume") {
    return Number(b.value24) - Number(a.value24);
  }

  if (resultSort === "change") {
    return Number(b.change) - Number(a.change);
  }

  if (a.signal !== b.signal) {
    return a.signal === "BUY" ? -1 : 1;
  }

  if (a.signal === "WATCH") {
    const watchPriority = x =>
      (Number(x.vol_ratio) >= 1.5 ? 1 : 0) +
      (Number(x.score) >= 50 ? 1 : 0);

    return watchPriority(b) - watchPriority(a);
  }

  return 0;
});


const candidates = allCandidates.slice(
  0,
  Number(document.getElementById("resultLimit").value)
);


  const qualifiedCount = d.results.filter(x => (x.signal === "BUY" || x.signal === "WATCH") && Number(x.value24) >= 500000000).length;
  const selectedLimit = Number(document.getElementById("resultLimit").value);
  const resultCountBox = document.getElementById("resultCount");

 if (resultCountBox) {
  const watchBase = d.results.filter(x =>
    x.signal === "WATCH" &&
    Number(x.value24) >= 500000000
  );

  const watchVolCount = watchBase.filter(x =>
    Number(x.vol_ratio) >= 1.5
  ).length;

  const watchScoreCount = watchBase.filter(x =>
    Number(x.score) >= 50
  ).length;

  const watchBothCount = watchBase.filter(x =>
    Number(x.vol_ratio) >= 1.5 &&
    Number(x.score) >= 50
  ).length;

  resultCountBox.textContent =
    `🔎 5억 이상 BUY/WATCH ${qualifiedCount}개 | ` +
    `WATCH ${watchBase.length}개 | ` +
    `거래량 통과 ${watchVolCount}개 | ` +
    `점수 통과 ${watchScoreCount}개 | ` +
    `동시 통과 ${watchBothCount}개 | ` +
    `현재 표시 ${candidates.length}개`;
}


  d.count = totalMarkets;
  d.scanned = allResults.length;
  document.getElementById("connection").textContent="빗썸 공개 API 연결됨";
  document.getElementById("count").innerHTML=d.count+' <small>마켓</small>';
  document.getElementById("buys").innerHTML=allResults.filter(x=>x.signal==="BUY").length+' <small>코인</small>';
  
const totalWatchCount = allResults.filter(
  x => x.signal === "WATCH"
).length;

const refinedWatchCount = allCandidates.filter(
  x => x.signal === "WATCH"
).length;

document.getElementById("watches").innerHTML =
  refinedWatchCount + ' <small>코인</small>' +
  '<div style="font-size:12px;font-weight:400;color:#94a3b8;margin-top:6px;">전체 WATCH: ' +
  totalWatchCount + '개</div>';

  document.getElementById("updated").textContent=d.updated;
  document.getElementById("updated").dataset.scanResultsAt = String(Date.now());
  
const scanAgeBox = document.getElementById("scanAge");

if (scanAgeBox) {
  const refreshScanAge = () => {
    const scanAt = Number(
      document.getElementById("updated").dataset.scanResultsAt
    );

    if (!scanAt) {
      scanAgeBox.textContent = "스캔 기록 없음";
      return;
    }

    const minutes = Math.max(
      0,
      Math.floor((Date.now() - scanAt) / 60000)
    );

    if (minutes < 1) {
      scanAgeBox.textContent = "방금 스캔 완료";
    } else if (minutes < 60) {
      scanAgeBox.textContent = `스캔 완료 후 ${minutes}분 경과`;
    } else {
      const hours = Math.floor(minutes / 60);
      const remainingMinutes = minutes % 60;
      scanAgeBox.textContent =
        `스캔 완료 후 ${hours}시간 ${remainingMinutes}분 경과`;
    }
    
    if (minutes >= 15) {
      scanAgeBox.style.color = "#fb7185";
      scanAgeBox.style.fontWeight = "700";
      scanAgeBox.textContent =
        "⛔ 오래된 분석 · " + scanAgeBox.textContent;
    } else if (minutes >= 5) {
      scanAgeBox.style.color = "#fbbf24";
      scanAgeBox.style.fontWeight = "700";
      scanAgeBox.textContent =
        "⚠️ 갱신 권장 · " + scanAgeBox.textContent;
    } else {
      scanAgeBox.style.color = "#4ade80";
      scanAgeBox.style.fontWeight = "600";
    }

    // 상세 분석창이 열려 있으면 스캔 경과 시간도 갱신
    const detailPanel = document.getElementById("coinDetail");
    const detailScanTime = document.getElementById("coinDetailScanTime");

    if (
      detailPanel &&
      detailScanTime &&
      detailPanel.style.display !== "none"
    ) {
      const scanTime = new Date(scanAt).toLocaleString("ko-KR", {
        timeZone: "Asia/Seoul",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false
      });

      const ageText = minutes < 1
        ? "방금"
        : minutes < 60
          ? `${minutes}분 경과`
          : `${Math.floor(minutes / 60)}시간 ${minutes % 60}분 경과`;

      detailScanTime.textContent =
        `스캔 결과 생성: ${scanTime} KST · ${ageText}`;

      detailScanTime.style.color =
        minutes >= 15 ? "#fb7185" :
        minutes >= 5 ? "#fbbf24" : "#4ade80";

      detailScanTime.style.fontWeight =
        minutes >= 5 ? "700" : "600";
    }

  };

  refreshScanAge();

  if (!window.scanAgeTimer) {
    window.scanAgeTimer = setInterval(refreshScanAge, 30000);
  }
}

  const body=document.getElementById("rows");
  if(!candidates.length){body.innerHTML='<tr><td colspan="10">조건에 맞는 결과가 없습니다.</td></tr>'}
  else body.innerHTML = allCandidates.map(x => `
  <tr>
    <td><b>${x.market}</b></td>
    <td>${won(x.price)}</td>
    <td class="${x.change>=0?'good':'bad'}">${pct(x.change)}</td>
    <td>${money(x.value24)}</td>
    <td>${x.vol_ratio==null?'—':x.vol_ratio.toFixed(2)+'배'}</td>
    <td>${x.resistance}</td>
    <td>${bithumbGuideWon(x.stop)}</td>
    <td>${bithumbGuideWon(x.target)}</td>
    <td><span class="signal ${x.signal}">${x.label}</span>${c5GradeBadge(x)}</td>
    <td>${x.score ?? '-'}</td>
    <td>${x.ma5_info?.ma5 ?? '-'} (${x.ma5_info?.status ?? '-'})</td>
    <td>${x.note}</td>
  </tr>
`).join("");

 body.querySelectorAll("tr").forEach((row, index) => {
  const x = allCandidates[index];
  if (!x || !row.cells[0]) return;

  const btn = document.createElement("button");
  btn.type = "button";
  btn.title = "코인 상세 분석 보기";
  btn.setAttribute("aria-label", "코인 상세 분석 보기");
  btn.style.cssText = "margin-left:8px;padding:3px 6px;border:1px solid #475569;border-radius:6px;background:#24314f;color:#cbd5e1;vertical-align:middle;cursor:pointer;";
  btn.innerHTML = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 11v6 M12 7h.01"/></svg>';

  btn.onclick = (event) => {
    event.stopPropagation();
    if (scanning) return;
    const detail = document.getElementById("coinDetail");
    if (!detail) return;

if (
  detail.style.display === "block" &&
  detail.dataset.market === x.market
) {
  detail.style.display = "none";
  return;
}

detail.dataset.market = x.market;

    document.getElementById("coinDetailTitle").textContent =
      `${x.market} 상세 분석`;
    document.getElementById("coinDetailTitle").dataset.signal =
      x.signal === "BUY" ? "BUY" : "WATCH";

const detailScanTime = document.getElementById("coinDetailScanTime");

if (detailScanTime) {
  const scanAt = Number(
    document.getElementById("updated")?.dataset.scanResultsAt
  );

  if (Number.isFinite(scanAt) && scanAt > 0) {
    const scanTime = new Date(scanAt).toLocaleString("ko-KR", {
      timeZone: "Asia/Seoul",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    });

    const minutes = Math.max(
      0,
      Math.floor((Date.now() - scanAt) / 60000)
    );

    const ageText = minutes < 1
      ? "방금"
      : minutes < 60
        ? `${minutes}분 경과`
        : `${Math.floor(minutes / 60)}시간 ${minutes % 60}분 경과`;

    detailScanTime.textContent =
      `스캔 결과 생성: ${scanTime} KST · ${ageText}`;
      
detailScanTime.style.color =
  minutes >= 15 ? "#fb7185" :
  minutes >= 5 ? "#fbbf24" : "#4ade80";
detailScanTime.style.fontWeight = minutes >= 5 ? "700" : "600";

  } else {
    detailScanTime.textContent = "스캔 생성 시각 정보 없음";
  }
}

   
    const detailBody = document.getElementById("coinDetailBody");
    detailBody.replaceChildren();

    const detailGrid = document.createElement("div");
    detailGrid.className = "coin-detail-grid";

const riskEntryPrice = Number(x.price);
const riskStopPrice = Number(x.stop);
const riskTargetPrice = Number(x.target);

const riskValuesValid =
  Number.isFinite(riskEntryPrice) &&
  Number.isFinite(riskStopPrice) &&
  Number.isFinite(riskTargetPrice) &&
  riskEntryPrice > 0 &&
  riskStopPrice > 0 &&
  riskStopPrice < riskEntryPrice &&
  riskTargetPrice > riskEntryPrice;

const riskLossPct = riskValuesValid
  ? ((riskEntryPrice - riskStopPrice) / riskEntryPrice) * 100
  : null;

const riskGainPct = riskValuesValid
  ? ((riskTargetPrice - riskEntryPrice) / riskEntryPrice) * 100
  : null;

const riskReward = riskValuesValid
  ? (riskTargetPrice - riskEntryPrice) /
    (riskEntryPrice - riskStopPrice)
  : null;

const feeSelect = document.getElementById("tradeFeeRate");
const feePercent = Number(feeSelect?.value ?? 0.25);
const feeRate = Number.isFinite(feePercent) && feePercent >= 0
  ? feePercent / 100
  : 0.0025;

// 매수·매도 수수료를 각각 반영
const netEntryCost = riskValuesValid
  ? riskEntryPrice * (1 + feeRate)
  : null;

const netStopProceeds = riskValuesValid
  ? riskStopPrice * (1 - feeRate)
  : null;

const netTargetProceeds = riskValuesValid
  ? riskTargetPrice * (1 - feeRate)
  : null;

// 수수료 포함 예상 손실률
const netLossPct = riskValuesValid
  ? ((netEntryCost - netStopProceeds) / netEntryCost) * 100
  : null;

// 수수료 포함 예상 순수익률
const netGainPct = riskValuesValid
  ? ((netTargetProceeds - netEntryCost) / netEntryCost) * 100
  : null;

// 수수료 포함 실질 손익비
const netRiskReward =
  netLossPct != null &&
  netGainPct != null &&
  netLossPct > 0 &&
  netGainPct > 0
    ? netGainPct / netLossPct
    : null;

const tradeRiskWarning = document.getElementById("tradeRiskWarning");

function updateTradeRiskWarning(gainPct, rewardRatio) {
  if (!tradeRiskWarning) return;

  if (gainPct == null || !Number.isFinite(gainPct)) {
    tradeRiskWarning.style.display = "none";
    return;
  }

  if (gainPct <= 0) {
    tradeRiskWarning.textContent =
      "⚠️ 진입 전 주의! 수수료 반영 예상 순수익률이 0% 이하입니다.";
  } else if (rewardRatio != null && rewardRatio < 1) {
    tradeRiskWarning.textContent =
      "⚠️ 진입 전 주의! 수수료 반영 실질 손익비가 1:1 미만입니다.";
  } else {
    tradeRiskWarning.style.display = "none";
    return;
  }

  tradeRiskWarning.style.display = "block";
}

updateTradeRiskWarning(netGainPct, netRiskReward);

const capitalSelect = document.getElementById("tradeCapital");
const capitalLossEl = document.getElementById("tradeCapitalLoss");
const capitalGainEl = document.getElementById("tradeCapitalGain");

function refreshCapitalEstimate() {
  if (!capitalSelect || !capitalLossEl || !capitalGainEl) return;

  const capital = Number(capitalSelect.value);
  const fee = Number(feeSelect?.value ?? 0.25) / 100;

  if (
    !riskValuesValid ||
    !Number.isFinite(capital) ||
    capital <= 0 ||
    !Number.isFinite(fee) ||
    fee < 0 ||
    fee >= 1
  ) {
    capitalLossEl.textContent = "계산 불가";
    capitalGainEl.textContent = "계산 불가";
    return;
  }

  const quantity = capital / (riskEntryPrice * (1 + fee));

  const stopPnL =
    quantity * riskStopPrice * (1 - fee) - capital;

  const targetPnL =
    quantity * riskTargetPrice * (1 - fee) - capital;

  const formatPnL = value =>
    (value < 0 ? "-" : "+") +
    Math.round(Math.abs(value)).toLocaleString("ko-KR") +
    "원";

  capitalLossEl.textContent = formatPnL(stopPnL);
  capitalGainEl.textContent = formatPnL(targetPnL);

  capitalLossEl.className =
    "coin-detail-value " + (stopPnL < 0 ? "stop" : "target");

  capitalGainEl.className =
    "coin-detail-value " + (targetPnL < 0 ? "stop" : "target");
}

refreshCapitalEstimate();

if (capitalSelect) {
  capitalSelect.onchange = refreshCapitalEstimate;
}

    const detailItems = [
      ["현재가", won(x.price)],
      ["24시간 변동", pct(x.change),
        Number(x.change) >= 0 ? "buy" : "stop"],
      ["거래량 배수",
        x.vol_ratio == null ? "-" : Number(x.vol_ratio).toFixed(2) + "배"],
      ["종합 점수", x.score ?? "-"],
      

     ["손절 참고가", bithumbGuideWon(x.stop), "stop"],
     ["목표 참고가", bithumbGuideWon(x.target), "target"],
     ["예상 손절폭", riskLossPct == null ? "—" : `-${riskLossPct.toFixed(2)}%`, "stop"],
     ["예상 목표폭", riskGainPct == null ? "—" : `+${riskGainPct.toFixed(2)}%`, "target"],
     ["이론적 손익비", riskReward == null ? "—" : `1 : ${riskReward.toFixed(2)}`, "buy", true],
 
     ["수수료 포함 예상 손절폭", netLossPct == null ? "—" : `-${netLossPct.toFixed(2)}%`, "stop"],
     ["수수료 포함 예상 순수익률", netGainPct == null ? "—" : `${netGainPct >= 0 ? "+" : ""}${netGainPct.toFixed(2)}%`, netGainPct != null && netGainPct < 0 ? "stop" : "target"],
     ["수수료 반영 실질 손익비", netRiskReward == null ? "—" : `1 : ${netRiskReward.toFixed(2)}`, "buy", true],

      ["신호", `${x.signal ?? "-"} (${x.label ?? "-"})`,
        x.signal === "BUY" ? "buy" : "watch", true],
      ["24시간 거래대금", money(x.value24), "", true],
      ["C5 회복 상태", x.resistance ?? "-", "", true],
      ["5분봉 MA5", x.ma5_info?.ma5 ?? "-"],
      ["MA5 상태", x.ma5_info?.status ?? "-"],
      ["15분봉 추세", x.trend15 ?? "정보 없음"],
      ["C5 관찰 등급", x.signal === "BUY" ? `${c5Grade(x)}등급 (실험·BUY 조건 아님)` : "BUY 발생 시 평가", "", true],
      ["분석 메모", x.note ?? "-", "", true]
      ];
    detailItems.forEach(([label, value, color, wide]) => {
      const item = document.createElement("div");
      item.className = "coin-detail-item" + (wide ? " wide" : "");

      const labelEl = document.createElement("div");
      labelEl.className = "coin-detail-label";
      labelEl.textContent = label;

      const valueEl = document.createElement("div");
      valueEl.className = "coin-detail-value" + (color ? " " + color : "");
      valueEl.textContent = String(value ?? "-");

      item.appendChild(labelEl);
      item.appendChild(valueEl);
      detailGrid.appendChild(item);
    });

    detailBody.appendChild(detailGrid);
    if (feeSelect) {
  feeSelect.onchange = () => {
    if (!riskValuesValid) return;

    const rate = Number(feeSelect.value) / 100;
    if (!Number.isFinite(rate) || rate < 0 || rate >= 1) return;

    const buyCost = riskEntryPrice * (1 + rate);
    const stopProceeds = riskStopPrice * (1 - rate);
    const targetProceeds = riskTargetPrice * (1 - rate);

    const lossPct = ((buyCost - stopProceeds) / buyCost) * 100;
    const gainPct = ((targetProceeds - buyCost) / buyCost) * 100;

    const rewardRatio = lossPct > 0 && gainPct > 0
      ? gainPct / lossPct
      : null;

    const updateFeeCard = (label, value, color) => {
      const card = Array.from(detailGrid.children).find(
        el => el.querySelector(".coin-detail-label")?.textContent.trim() === label
      );

      const valueEl = card?.querySelector(".coin-detail-value");
      if (!valueEl) return;

      valueEl.textContent = value;
      valueEl.className = "coin-detail-value" + (color ? " " + color : "");
    };

    updateFeeCard(
      "수수료 포함 예상 손절폭",
      `-${lossPct.toFixed(2)}%`,
      "stop"
    );

    updateFeeCard(
      "수수료 포함 예상 순수익률",
      `${gainPct >= 0 ? "+" : ""}${gainPct.toFixed(2)}%`,
      gainPct < 0 ? "stop" : "target"
    );

    updateFeeCard(
      "수수료 반영 실질 손익비",
      rewardRatio == null ? "—" : `1 : ${rewardRatio.toFixed(2)}`,
      rewardRatio == null ? "" : "buy"
    );
 
    updateTradeRiskWarning(gainPct, rewardRatio);
    refreshCapitalEstimate(); 
  };
}
 
    detail.style.display = "block";
    detail.scrollIntoView({
      behavior: "smooth",
      block: "nearest"
    });
  };

  row.cells[0].appendChild(btn);
});
 
const searchBox = document.getElementById("coinSearch");
const signalFilterBox = document.getElementById("signalFilter");
function filterCoinResults() {
  if (!allCandidates.length) return;
const query = (searchBox.value || "").trim().toUpperCase();
const selectedSignal = signalFilterBox?.value || "ALL";

  let matched = 0;
  let shown = 0;

  
body.querySelectorAll("tr").forEach((row, index) => {

    const market = row.cells[0]?.textContent.trim().toUpperCase() || "";
const signal = allCandidates[index]?.signal;
const marketCell = row.cells[0];

if (marketCell) { marketCell.dataset.signal = signal;

  marketCell.style.color =
    signal === "BUY" ? "#4ade80" : "#fbbf24";
  marketCell.style.fontWeight = "700";
}
    
const isMatch = market.includes(query) &&
  (selectedSignal === "ALL" || allCandidates[index]?.signal === selectedSignal);


    if (isMatch) matched++;

    const visible = isMatch && shown < selectedLimit;
    row.style.display = visible ? "" : "none";

    if (visible) shown++;
  });

  if (resultCountBox) {
    resultCountBox.textContent =
      `✅ 조건 충족 ${qualifiedCount}개 / 최대 표시 ${selectedLimit}개 (현재 ${shown}개 표시)` +
      (query ? ` · 검색 일치 ${matched}개` : "");
  }
}

if (searchBox) {
  searchBox.oninput = filterCoinResults;
  filterCoinResults();
}

if (signalFilterBox) signalFilterBox.onchange = filterCoinResults;
const resultSortBox = document.getElementById("resultSort");
const defaultRanks = new Map(
  d.results.map((x, i) => [x.market, i])
);

if (resultSortBox) {
  resultSortBox.onchange = function() {
    const mode = resultSortBox.value;

    allCandidates.sort((a, b) => {
      if (mode === "volume") {
        return Number(b.value24) - Number(a.value24);
      }
      if (mode === "change") {
        return Number(b.change) - Number(a.change);
      }
  
const priority = x =>
  (x.signal === "BUY" ? 3 : 0) +
  (x.signal === "WATCH" && Number(x.vol_ratio) >= 1.5 ? 1 : 0) +
  (x.signal === "WATCH" && Number(x.score) >= 50 ? 1 : 0);

return priority(b) - priority(a) ||
  defaultRanks.get(a.market) - defaultRanks.get(b.market);

    });

    const rowsByMarket = new Map(
      Array.from(body.querySelectorAll("tr")).map(row => [
        row.cells[0]?.textContent.trim(),
        row
      ])
    );

    allCandidates.forEach(x => {
      const row = rowsByMarket.get(x.market);
      if (row) body.appendChild(row);
    });

    filterCoinResults();
  };
}

  status.textContent = `완료: ${d.scanned}/${d.count}개 코인 분석 · 실패 페이지 ${failedPages.length}개${failedPages.length ? " (" + failedPages.join(", ") + "페이지)" : ""}`;
  
    const scanIncomplete =
      failedPages.length > 0 || d.scanned < totalMarkets;

    if (scanIncomplete) {
      status.textContent =
        "⚠️ 일부 분석 실패 — 결과 불완전! " +
        status.textContent +
        " | BUY 신호 수는 전체 시장 기준이 아닙니다.";
      status.style.color = "#ffb86b";
    } else {
      status.style.color = "";
    }

    if (scanResultsNotice) {
      if (scanIncomplete) {
        scanResultsNotice.textContent =
          "⚠️ 일부 분석 실패 — 현재 표는 불완전한 새 스캔 결과입니다.";
        scanResultsNotice.style.display = "block";
      } else {
        scanResultsNotice.style.display = "none";
      }
    }


} catch (e) {
  document.getElementById("connection").textContent = "연결 실패";

  status.textContent =
    "오류: " + e.message +
    " · 잠시 후 다시 시도하세요. Render 무료 서버라면 기동에 시간이 걸릴 수 있습니다.";

  if (scanResultsNotice) {
    const hasPreviousScan = Boolean(
      document.getElementById("updated")?.dataset.scanResultsAt
    );

    scanResultsNotice.textContent = hasPreviousScan
      ? "⚠️ 새 스캔 실패 — 현재 표는 이전 스캔 결과입니다."
      : "⚠️ 첫 스캔 실패 — 표시할 스캔 결과가 없습니다.";

    scanResultsNotice.style.display = "block";
  }
}

finally {
  scanning = false;
}
}
function toggleAuto(){if(autoTimer){clearInterval(autoTimer);autoTimer=null;document.getElementById("autoBtn").textContent="자동 갱신 켜기"}else{scan();autoTimer=setInterval(scan,180000);document.getElementById("autoBtn").textContent="자동 갱신 끄기"}}

showRecentBuyAlerts();

setInterval(showRecentBuyAlerts, 60000);
document.getElementById("rows").addEventListener("click", function(event) {
  const cell = event.target.closest("td");
  if (!cell || cell.cellIndex !== 0) return;

  const market = cell.textContent.trim();
  if (!/^KRW-[A-Z0-9]+$/.test(market)) return;

  const symbol = market.slice(4);
  const url = `https://www.bithumb.com/react/trade/order/${symbol}-KRW`;

  window.open(url, "_blank", "noopener,noreferrer");
});

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
    return api_get(
        f"/v1/candles/minutes/{minute}",
        {"market": market, "count": count}
    )


def candles_before(market, minute, to_kst, count=80):
    """백테스트용: 지정한 KST 시각 이전의 캔들 조회."""
    if not isinstance(to_kst, datetime):
        raise ValueError("to_kst must be a datetime")

    if to_kst.tzinfo is not None:
        to_kst = to_kst.astimezone(KST)

    params = {
        "market": market,
        "count": max(1, min(int(count), 200)),
        "to": to_kst.strftime("%Y-%m-%dT%H:%M:%S")
    }

    return api_get(
        f"/v1/candles/minutes/{minute}",
        params
    )

def backtest_value24_candle_pages(market, cutoff_kst):
    """과거 24시간 거래대금 검증을 위한 캔들 수집."""
    first = candles_before(
        market, 5, cutoff_kst, count=200
    )

    if not isinstance(first, list) or not first:
        return []

    try:
        oldest = min(
            datetime.fromisoformat(
                c["candle_date_time_kst"]
            )
            for c in first
        )
    except (KeyError, TypeError, ValueError):
        return []

    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=KST)
    else:
        oldest = oldest.astimezone(KST)

    second = candles_before(
        market, 5, oldest, count=200
    )

    if not isinstance(second, list):
        return []

    return first + second

def backtest_value24_at_cutoff(market, cutoff_kst):
    """과거 기준 시각 직전 24시간 거래대금 검증."""
    from math import isfinite

    if cutoff_kst.tzinfo is None:
        cutoff_kst = cutoff_kst.replace(tzinfo=KST)
    else:
        cutoff_kst = cutoff_kst.astimezone(KST)

    start = cutoff_kst - timedelta(hours=24)
    raw = backtest_value24_candle_pages(
        market, cutoff_kst
    )

    by_time = {}
    duplicate_count = 0

    for candle in raw:
        try:
            dt = datetime.fromisoformat(
                candle["candle_date_time_kst"]
            )
            value = float(
                candle["candle_acc_trade_price"]
            )
        except (KeyError, TypeError, ValueError):
            continue

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        else:
            dt = dt.astimezone(KST)

        if not isfinite(value) or value < 0:
            continue

        if start <= dt < cutoff_kst:
            if dt in by_time:
                duplicate_count += 1
            by_time[dt] = value

    expected = [
        start + timedelta(minutes=5 * i)
        for i in range(288)
    ]

    missing = [
        dt for dt in expected
        if dt not in by_time
    ]

    ok = (
        len(missing) == 0
        and duplicate_count == 0
        and len(by_time) == 288
    )

    value24 = (
        round(sum(by_time[dt] for dt in expected), 2)
        if ok else None
    )

    return {
        "ok": ok,
        "value24": value24,
        "pass_500m": (
            value24 >= 500_000_000
            if ok else None
        ),
        "valid_candles": len(by_time),
        "missing_count": len(missing),
        "duplicate_count": duplicate_count
    }

@app.get("/api/backtest-value24-check")
def backtest_value24_check():
    """임시 진단: 과거 신호 시점의 24시간 거래대금 검사."""

    scenarios = {
        "eth_1101": (
            "KRW-ETH",
            datetime(2026, 11, 1, 7, 5, tzinfo=KST)
        ),
        "btc_1104": (
            "KRW-BTC",
            datetime(2026, 11, 4, 5, 40, tzinfo=KST)
        ),
        "eth_1107": (
            "KRW-ETH",
            datetime(2026, 11, 7, 7, 0, tzinfo=KST)
        )
    }

    case = request.args.get("case", "eth_1101")

    if case not in scenarios:
        return jsonify({
            "ok": False,
            "error": "알 수 없는 테스트 사례"
        }), 400

    try:
        market, cutoff = scenarios[case]

        result = backtest_value24_at_cutoff(
            market, cutoff
        )

        return jsonify({
            "test_only": True,
            "case": case,
            "market": market,
            "cutoff_kst": cutoff.isoformat(),
            **result
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "test_only": True,
            "error_type": type(e).__name__,
            "error": str(e)
        }), 500

def backtest_completed_rows(market, minute, cutoff_kst, count=80):
    """과거 기준 시점까지 완료된 캔들만 시간순으로 반환."""
    if not isinstance(cutoff_kst, datetime):
        raise ValueError("cutoff_kst must be datetime")

    if cutoff_kst.tzinfo is None:
        cutoff_kst = cutoff_kst.replace(tzinfo=KST)
    else:
        cutoff_kst = cutoff_kst.astimezone(KST)

    raw = candles_before(
        market, minute, cutoff_kst, count=count
    )

    if not isinstance(raw, list):
        return []

    completed = []

    for candle in raw:
        if not isinstance(candle, dict):
            continue

        try:
            start = datetime.fromisoformat(
                candle["candle_date_time_kst"]
            )
        except (KeyError, TypeError, ValueError):
            continue

        if start.tzinfo is None:
            start = start.replace(tzinfo=KST)
        else:
            start = start.astimezone(KST)

        if start + timedelta(minutes=minute) <= cutoff_kst:
            completed.append(candle)

    completed.sort(
        key=lambda x: x["candle_date_time_kst"]
    )

    return completed

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
    


def c5_live_reclaim(rows):
    """Return current confirmed C5 signal and cooldown reason.

    Matching V11.14.17: prior close < prior SMA5; green candle closing
    above current SMA5; current SMA5 > prior SMA5. Accept no more than
    one C5 BUY per market in 12 consecutive completed 5-minute bars.
    Missing 5-minute bars invalidate the affected lookback; no future bars.
    """
    if len(rows) < 25:
        return False, "5분봉 데이터 부족"

    times = []
    try:
        for r in rows:
            times.append(datetime.fromisoformat(r["candle_date_time_kst"]))
        closes = [float(r["trade_price"]) for r in rows]
        opens = [float(r["opening_price"]) for r in rows]
    except (KeyError, TypeError, ValueError):
        return False, "5분봉 데이터 오류"

    cooldown_end = -1
    latest_raw_signal = False
    current_reason = "조건 대기"
    for i in range(24, len(rows)):
        if any(times[j] - times[j - 1] != timedelta(minutes=5)
               for j in range(i - 23, i + 1)):
            if i == len(rows) - 1:
                current_reason = "연속 5분봉 부족"
            continue
        before_ma = sum(closes[i - 5:i]) / 5
        after_ma = sum(closes[i - 4:i + 1]) / 5
        raw = (closes[i - 1] < before_ma
               and closes[i] > opens[i]
               and closes[i] > after_ma
               and after_ma > before_ma)
        if i == len(rows) - 1:
            latest_raw_signal = raw
        if raw:
            if i < cooldown_end:
                if i == len(rows) - 1:
                    current_reason = "최근 C5 BUY 이후 60분 이내"
            else:
                cooldown_end = i + 12
                if i == len(rows) - 1:
                    return True, "C5 첫 회복 · BUY"
    return False, current_reason if latest_raw_signal else "조건 대기"


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
   
    trend15 = "정보 없음"

    if minute != 5:
        trend15 = "5분봉에서만 분석"
    else:
        groups15 = {}

        for c in rows:
            dt = datetime.fromisoformat(
                c["candle_date_time_kst"]
            )
            key = dt.replace(
                minute=(dt.minute // 15) * 15,
                second=0,
                microsecond=0
            )
            groups15.setdefault(key, {})[
                dt.minute % 15
            ] = float(c["trade_price"])

        complete15 = {
            key: values[10]
            for key, values in groups15.items()
            if all(m in values for m in (0, 5, 10))
        }

        latest_expected = now_kst.replace(
            tzinfo=None,
            minute=(now_kst.minute // 15) * 15,
            second=0,
            microsecond=0
        ) - timedelta(minutes=15)

        last11 = [
            complete15.get(
                latest_expected - timedelta(minutes=15 * i)
            )
            for i in range(10, -1, -1)
        ]

        valid_count = sum(
            v is not None for v in last11
        )

        if valid_count < 11:
            trend15 = (
                f"연속 15분봉 부족 ({valid_count}/11개)"
            )
        else:
            ma_now = sum(last11[1:]) / 10
            ma_prev = sum(last11[:-1]) / 10
            close_now = last11[-1]

            if close_now > ma_now and ma_now > ma_prev:
                trend15 = "상승"
            elif close_now < ma_now and ma_now < ma_prev:
                trend15 = "하락"
            else:
                trend15 = "중립"
 

    # C5 BUY signal. Old diagonal-resistance calculation is intentionally
    # removed from live scanning; the V2 UI and API result shape stay the same.
    signal = "WAIT"
    label = "대기"
    resistance = "C5 조건 대기"
    note = "5분봉 5이평 첫 회복 대기"
    avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else 0
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else None
    stop = current * 0.99 if current > 0 else None
    target = current * 1.015 if current > 0 else None

    c5_buy, c5_reason = c5_live_reclaim(rows)
    prev_ma5 = sum(closes[-6:-1]) / 5
    current_ma5 = sum(closes[-5:]) / 5
    ma_rising = current_ma5 > prev_ma5
    previous_below = closes[-2] < prev_ma5

    if c5_buy:
        signal = "BUY"
        label = "C5 진입 확인"
        resistance = "5이평 첫 회복"
        note = "직전봉 5이평 아래 → 양봉 종가 회복 · 5이평 상승 (다음 봉 진입 참고)"
    elif c5_reason == "최근 C5 BUY 이후 60분 이내":
        resistance = "C5 중복 제한"
        note = "동일 종목의 최근 C5 BUY 후 60분 내이므로 반복 신호 차단"
    elif previous_below and ma_rising and closes[-1] <= current_ma5 * 1.005:
        signal = "WATCH"
        label = "C5 회복 관찰"
        resistance = "5이평 회복 관찰"
        note = "직전봉 5이평 아래 · 이평 상승 중 · 완성된 양봉 회복 대기"
    else:
        note = c5_reason if c5_reason != "조건 대기" else note

    # Give the table useful MA5 context; no extra ticker/candle API calls.
    ma5_info = {"ma5": round(current_ma5, 8),
                "status": ("MA5 상향 돌파" if closes[-2] < prev_ma5
                           and closes[-1] > current_ma5
                           else "MA5 위 유지" if closes[-1] > current_ma5
                           else "MA5 아래")}
    ticker = ticker or {}
    change=float(ticker.get("signed_change_rate", 0))*100 if ticker else ((current/closes[-2]-1)*100 if closes[-2] else 0)
    value24=float(ticker.get("acc_trade_price_24h", 0)) if ticker else 0
    score = calculate_score(
        change, ma5_info, vol_ratio, value24, signal, 1.5
    )
    return {"market":market,"price":current,"change":change,"value24":value24,
            "vol_ratio":vol_ratio,"resistance":resistance,"signal":signal,"label":label,
            
            "candle_time": rows[-1]["candle_date_time_kst"],

            "recent_closes": [{"time": r["candle_date_time_kst"], "price": float(r["trade_price"])} for r in rows[-36:]] if minute == 5 else [],

            
            "trend15":trend15,"note":note,"stop":stop,"target":target,"ma5_info":ma5_info,"score":score}

def make_backtest_windows(rows, minute=5, lookback=80, forward_bars=12):
    """신호 판정용 과거 캔들과 사후 평가용 캔들을 분리."""
    if not isinstance(rows, list):
        return []

    if minute <= 0 or lookback < 35 or forward_bars < 1:
        return []

    valid = []

    for candle in rows:
        if not isinstance(candle, dict):
            continue

        try:
            start = datetime.fromisoformat(
                candle["candle_date_time_kst"]
            )

            if start.tzinfo is None:
                start = start.replace(tzinfo=KST)
            else:
                start = start.astimezone(KST)

            valid.append((start, candle))

        except (KeyError, TypeError, ValueError):
            continue

    valid.sort(key=lambda item: item[0])

    if len(valid) < lookback + forward_bars:
        return []

    windows = []

    for i in range(lookback - 1, len(valid) - forward_bars):
        chunk = valid[
            i - lookback + 1 : i + forward_bars + 1
        ]

        continuous = all(
            chunk[j][0] - chunk[j - 1][0]
            == timedelta(minutes=minute)
            for j in range(1, len(chunk))
        )

        if not continuous:
            continue

        cutoff = (
            chunk[lookback - 1][0]
            + timedelta(minutes=minute)
        )

        windows.append({
            "cutoff_kst": cutoff,
            "signal_rows": [
                candle for _, candle in chunk[:lookback]
            ],
            "evaluation_rows": [
                candle for _, candle in chunk[lookback:]
            ]
        })

    return windows
# 실험 전용: 기존 실시간 BUY 판정에는 영향 없음.
def backtest_fibonacci_signal(window, volume_multiple=1.5):
    data = prepare_backtest_signal_inputs(window)
    if data is None:
        return None

    rows = data["rows"]
    highs = data["highs"]
    lows = data["lows"]
    closes = data["closes"]
    current = data["current"]
    vol_ratio = data["vol_ratio"]
    if len(rows) < 80:
        return {"signal": "WAIT"}

    # 가장 최근 3개 봉 이전에 확정된 상승 고점 사용
    peak_i = max(range(len(rows) - 35, len(rows) - 3),
                 key=lambda i: highs[i])
    low_start = max(0, peak_i - 30)
    if peak_i - low_start < 5:
        return {"signal": "WAIT"}
    low_i = min(range(low_start, peak_i), key=lambda i: lows[i])
    swing_low = lows[low_i]
    swing_high = highs[peak_i]
    if swing_low <= 0 or (swing_high / swing_low - 1) < 0.025:
        return {"signal": "WAIT"}

    span = swing_high - swing_low
    fib_382 = swing_high - span * 0.382
    fib_618 = swing_high - span * 0.618
    recent_start = max(peak_i + 1, len(rows) - 6)
    pullback_lows = lows[recent_start:]
    if not pullback_lows:
        return {"signal": "WAIT"}

    # 완료된 80개 5분봉 거래대금으로 유동성 보수적 확인
    recent_value = sum(
        float(c.get("candle_acc_trade_price", 0))
        for c in rows
    )
    liquidity_ok = recent_value >= 500_000_000
    trend_ok = (
        sum(closes[-21:-1]) / 20
        > sum(closes[-41:-21]) / 20
    )
    touch_ok = (
        any(fib_618 <= low <= fib_382 for low in pullback_lows)
        and min(pullback_lows) >= fib_618 * 0.995
    )
    bounce_ok = (
        current > closes[-2]
        and current > float(rows[-1].get("opening_price", current))
        and current >= fib_618
        and current < swing_high * 1.01
    )
    volume_ok = (
        vol_ratio is not None
        and vol_ratio >= volume_multiple
    )

    is_buy = all((
        liquidity_ok, trend_ok, touch_ok,
        bounce_ok, volume_ok
    ))
    return {
        "signal": "BUY" if is_buy else "WAIT",
        "entry_close": current,
        "fib_382": round(fib_382, 8),
        "fib_618": round(fib_618, 8),
        "vol_ratio": vol_ratio,
        "liquidity_ok": liquidity_ok,
        "trend_ok": trend_ok,
        "touch_ok": touch_ok,
        "bounce_ok": bounce_ok,
        "volume_ok": volume_ok,
    }


@app.get("/api/backtest-ab-fibonacci")
def backtest_ab_fibonacci():
    """A: 기존 로직, B: 피보나치. 동일 데이터로 비교."""
    dates = [
        "2026-11-01", "2026-11-04",
        "2026-11-07", "2026-11-08"
    ]
    markets = [
        "KRW-BTC", "KRW-ETH", "KRW-XRP",
        "KRW-SOL", "KRW-DOGE", "KRW-ADA"
    ]
    stats = {
        name: {
            "signals": 0,
            "returns": {15: [], 30: [], 60: []},
            "examples": []
        }
        for name in ("A_existing", "B_fibonacci")
    }
    last_event = {name: {} for name in stats}
    tested_windows = 0
    errors = []

    for date in dates:
        cutoff = datetime.fromisoformat(
            date + "T12:00:00"
        ).replace(tzinfo=KST)

        for market in markets:
            try:
                if (
                    date == "2026-11-08"
                    and market in (
                        "KRW-BTC", "KRW-ETH", "KRW-XRP"
                    )
                ):
                    rows = backtest_completed_rows_2pages(
                        market, 5, cutoff
                    )
                else:
                    rows = backtest_completed_rows(
                        market, 5, cutoff, count=200
                    )

                windows = make_backtest_windows(
                    rows,
                    minute=5,
                    lookback=80,
                    forward_bars=12
                )
                tested_windows += len(windows)

                for window in windows:
                    signals = {
                        "A_existing": backtest_replay_signal(
                            window
                        ),
                        "B_fibonacci": backtest_fibonacci_signal(
                            window
                        ),
                    }

                    event_at = window["cutoff_kst"]
                    future = window["evaluation_rows"]

                    if len(future) != 12:
                        continue

                    entry = float(future[0]["opening_price"])
                    if entry <= 0:
                        continue

                    for name, signal in signals.items():
                        if (
                            not signal
                            or signal.get("signal") != "BUY"
                        ):
                            continue

                        previous = last_event[name].get(market)

                        if (
                            previous
                            and event_at - previous
                            < timedelta(minutes=60)
                        ):
                            continue

                        last_event[name][market] = event_at
                        bucket = stats[name]
                        bucket["signals"] += 1
                        returns = {}

                        for minutes, idx in (
                            (15, 2), (30, 5), (60, 11)
                        ):
                            gross = (
                                float(
                                    future[idx]["trade_price"]
                                ) / entry - 1
                            ) * 100

                            bucket["returns"][minutes].append(
                                gross
                            )
                            returns[f"{minutes}m"] = round(
                                gross, 3
                            )

                        if len(bucket["examples"]) < 5:
                            bucket["examples"].append({
                                "market": market,
                                "signal_time_kst": (
                                    window["signal_rows"][-1]
                                    ["candle_date_time_kst"]
                                ),
                                "entry_next_open": entry,
                                "gross_return_pct": returns,
                            })

            except (
                KeyError, TypeError, ValueError,
                requests.RequestException
            ) as exc:
                errors.append({
                    "date": date,
                    "market": market,
                    "error": str(exc)[:150]
                })

    results = {}

    for name, bucket in stats.items():
        result = {
            "signals": bucket["signals"],
            "examples": bucket["examples"]
        }

        for minutes, values in bucket["returns"].items():
            result[f"{minutes}m"] = {
                "samples": len(values),
                "avg_gross_return_pct": (
                    round(sum(values) / len(values), 3)
                    if values else None
                ),
                "positive_rate_pct": (
                    round(
                        100 * sum(v > 0 for v in values)
                        / len(values), 1
                    )
                    if values else None
                ),
            }

        results[name] = result

    return jsonify({
        "ok": len(errors) == 0,
        "scope": {
            "dates": dates,
            "markets": markets,
            "minute": 5,
            "tested_windows": tested_windows,
            "cooldown_minutes": 60
        },
        "comparison": results,
        "interpretation_notes": [
            "다음 봉 시가 진입, 15/30/60분 뒤 종가 청산",
            "수수료와 슬리피지 미반영 총수익률",
            "80봉 거래대금 5억원은 보수적인 필터",
            "같은 종목/전략의 60분 내 중복 신호 제외",
            "A전략은 기존 백테스트 로직을 재사용"
        ],
        "errors": errors,
    })
def prepare_backtest_signal_inputs(window):
    """과거 신호 판단용 캔들만 사용해 분석 자료 준비."""
    if not isinstance(window, dict):
        return None

    rows = window.get("signal_rows")
    cutoff = window.get("cutoff_kst")

    if (
        not isinstance(rows, list)
        or len(rows) < 35
        or not isinstance(cutoff, datetime)
    ):
        return None

    try:
        closes = [
            float(c["trade_price"]) for c in rows
        ]
        highs = [
            float(c["high_price"]) for c in rows
        ]
        lows = [
            float(c["low_price"]) for c in rows
        ]
        volumes = [
            float(c["candle_acc_trade_volume"])
            for c in rows
        ]
    except (KeyError, TypeError, ValueError):
        return None

    if any(
        high < low or volume < 0
        for high, low, volume
        in zip(highs, lows, volumes)
    ):
        return None

    avg_vol = sum(volumes[-21:-1]) / 20

    vol_ratio = (
        volumes[-1] / avg_vol
        if avg_vol > 0 else None
    )

    return {
        "cutoff_kst": cutoff,
        "rows": rows,
        "closes": closes,
        "highs": highs,
        "lows": lows,
        "volumes": volumes,
        "current": closes[-1],
        "vol_ratio": vol_ratio
    }


def backtest_replay_signal(window, volume_multiple=1.5, rr=2.0):
    """5분봉 과거 캔들만으로 실시간 BUY 조건 재현."""
    data = prepare_backtest_signal_inputs(window)
    if data is None:
        return None

    rows = data["rows"]
    closes = data["closes"]
    highs = data["highs"]
    lows = data["lows"]
    current = data["current"]
    vol_ratio = data["vol_ratio"]

    pivots = []
    for i in range(2, len(highs) - 2):
        if (
            highs[i] >= max(highs[i - 2:i])
            and highs[i] > max(highs[i + 1:i + 3])
        ):
            pivots.append(i)

    signal = "WAIT"
    breakout_seen = False
    support_held = False
    close_above = False
    retest = False
    vol_ok = False
    stop = None
    target = None
    line_now = None

    if len(pivots) >= 2:
        a, b = pivots[-2], pivots[-1]

        if highs[b] < highs[a] and b > a:
            slope = (highs[b] - highs[a]) / (b - a)
            line_now = highs[b] + slope * ((len(rows) - 1) - b)
            tolerance = 0.004

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
                    closes[j] >=
                    (highs[b] + slope * (j - b)) * (1 - tolerance)
                    for j in range(breakout_index + 1, len(rows) - 1)
                )
            )

            close_above = current > line_now * (1 + tolerance)

            retest = (
                lows[-1] <= line_now * (1 + tolerance)
                and current >= line_now
                and current > closes[-2]
            )

            vol_ok = (
                vol_ratio is not None
                and vol_ratio >= volume_multiple
            )

            if (
                close_above
                and vol_ok
                and retest
                and breakout_seen
                and support_held
            ):
                signal = "BUY"

            elif (
                close_above
                or (
                    current >= line_now * (1 - tolerance)
                    and vol_ok
                )
            ):
                signal = "WATCH"

            atrv = atr(rows)

            if line_now and atrv:
                stop = max(
                    0.0,
                    min(lows[-1], current - 1.2 * atrv)
                )
                risk = max(0.0, current - stop)
                target = current + rr * risk

    return {
        "signal": signal,
        "is_buy": signal == "BUY",
        "candle_time_kst": rows[-1]["candle_date_time_kst"],
        "cutoff_kst": data["cutoff_kst"].isoformat(),
        "entry_close": current,
        "vol_ratio": vol_ratio,
        "close_above": close_above,
        "volume_ok": vol_ok,
        "retest": retest,
        "breakout_seen": breakout_seen,
        "support_held": support_held,
        "stop_price": stop,
        "target_price": target,
        "historical_value24_verified": False
    }

@app.get("/api/backtest-synthetic-buy-check")
def backtest_synthetic_buy_check():
    """임시 진단: 가상 캔들로 BUY 판정 가능 여부 확인."""

    base = datetime(2026, 11, 8, tzinfo=KST)

    closes = [108.0] * 80
    closes[73:] = [
        111.5, 111.5, 114.0, 114.2,
        114.3, 114.35, 114.6
    ]

    rows = []

    for i, close in enumerate(closes):
        rows.append({
            "candle_date_time_kst": (
                base + timedelta(minutes=i * 5)
            ).strftime("%Y-%m-%dT%H:%M:%S"),
            "trade_price": close,
            "high_price": (
                120 if i == 30
                else 115 if i == 60
                else close + 0.5
            ),
            "low_price": (
                111.8 if i == 79
                else close - 1
            ),
            "candle_acc_trade_volume": (
                220 if i == 79
                else 100
            )
        })

    window = {
        "cutoff_kst": base + timedelta(minutes=400),
        "signal_rows": rows,
        "evaluation_rows": []
    }

    result = backtest_replay_signal(
        window, volume_multiple=1.5, rr=2.0
    )

    conditions = [
        "close_above",
        "volume_ok",
        "retest",
        "breakout_seen",
        "support_held"
    ]

    passed = (
        result is not None
        and result["signal"] == "BUY"
        and all(result[name] is True for name in conditions)
    )

    return jsonify({
        "ok": passed,
        "test_only": True,
        "synthetic_data": True,
        "signal": result["signal"] if result else None,
        "condition_results": (
            {name: result[name] for name in conditions}
            if result else {}
        ),
        "historical_value24_verified": False
    })

@app.get("/api/backtest-replay-check")
def backtest_replay_check():
    """임시 진단: 과거 BUY 조건별 통과 횟수 검사."""
    minute = 5
    cutoff = datetime(2026, 11, 7, 12, 0, tzinfo=KST)
    markets = ["KRW-BTC", "KRW-ETH", "KRW-XRP"]

    conditions = [
        "close_above",
        "volume_ok",
        "retest",
        "breakout_seen",
        "support_held"
    ]

    market_results = {}
    total_counts = {"BUY": 0, "WATCH": 0, "WAIT": 0}

    for market in markets:
        try:
            rows = backtest_completed_rows(
                market, minute, cutoff, count=200
            )

            windows = make_backtest_windows(
                rows,
                minute=minute,
                lookback=80,
                forward_bars=12
            )

            counts = {"BUY": 0, "WATCH": 0, "WAIT": 0}

            condition_pass_counts = {
                name: 0 for name in conditions
            }

            cumulative_pass_counts = {
                name: 0 for name in conditions
            }

            future_independent = True
            first_buy = None

            for window in windows:
                result = backtest_replay_signal(
                    window,
                    volume_multiple=1.5,
                    rr=2.0
                )

                without_future = {
                    "cutoff_kst": window["cutoff_kst"],
                    "signal_rows": window["signal_rows"],
                    "evaluation_rows": []
                }

                control = backtest_replay_signal(
                    without_future,
                    volume_multiple=1.5,
                    rr=2.0
                )

                if result is None or control is None:
                    raise ValueError("신호 재현 결과 없음")

                if result != control:
                    future_independent = False

                signal = result["signal"]

                if signal not in counts:
                    raise ValueError("알 수 없는 신호")

                counts[signal] += 1

                passed_so_far = True

                for name in conditions:
                    passed = result[name] is True

                    if passed:
                        condition_pass_counts[name] += 1

                    passed_so_far = passed_so_far and passed

                    if passed_so_far:
                        cumulative_pass_counts[name] += 1

                if signal == "BUY" and first_buy is None:
                    first_buy = {
                        "candle_time_kst": result["candle_time_kst"],
                        "entry_close": result["entry_close"],
                        "vol_ratio": result["vol_ratio"]
                    }

            market_ok = (
                len(windows) > 0
                and future_independent
                and sum(counts.values()) == len(windows)
            )

            market_results[market] = {
                "ok": market_ok,
                "completed_candles": len(rows),
                "window_count": len(windows),
                "signal_counts": counts,
                "condition_pass_counts": condition_pass_counts,
                "cumulative_pass_counts": cumulative_pass_counts,
                "future_evaluation_independent": future_independent,
                "first_buy": first_buy
            }

            for signal in total_counts:
                total_counts[signal] += counts[signal]

        except Exception as e:
            market_results[market] = {
                "ok": False,
                "error_type": type(e).__name__
            }

    all_ok = all(
        market_results[m].get("ok") is True
        for m in markets
    )

    return jsonify({
        "ok": all_ok,
        "test_only": True,
        "market_count": len(markets),
        "cutoff_kst": cutoff.isoformat(),
        "signal_bars": 80,
        "evaluation_bars": 12,
        "market_results": market_results,
        "total_signal_counts": total_counts,
        "historical_value24_verified": False
    })

@app.get("/api/backtest-eth-breakout-check")
def backtest_eth_breakout_check():
    """임시 진단: ETH 돌파 판정 기준선과 실제 종가 추적."""

    scenarios = {
        "eth_1107": (
            "KRW-ETH",
            datetime(2026, 11, 7, 12, 0, tzinfo=KST)
        ),
        "eth_1101": (
            "KRW-ETH",
            datetime(2026, 11, 1, 12, 0, tzinfo=KST)
        ),
        "btc_1104": (
            "KRW-BTC",
            datetime(2026, 11, 4, 12, 0, tzinfo=KST)
        ),
        "xrp_1108": (
            "KRW-XRP",
            datetime(2026, 11, 8, 12, 0, tzinfo=KST)
        )
    }

    case = request.args.get("case", "eth_1107")

    if case not in scenarios:
        return jsonify({
            "ok": False,
            "error": "알 수 없는 테스트 사례"
        }), 400

    market, cutoff = scenarios[case]
    minute = 5

    try:
        rows = backtest_completed_rows(
            market, minute, cutoff, count=200
        )
        windows = make_backtest_windows(
            rows, minute=minute,
            lookback=80, forward_bars=12
        )

        near_buy_count = 0
        examples = []

        for window in windows:
            result = backtest_replay_signal(
                window, volume_multiple=1.5, rr=2.0
            )
            if result is None:
                raise ValueError("과거 신호 재현 실패")

            if not (
                result["close_above"]
                and result["volume_ok"]
                and result["retest"]
                and not result["breakout_seen"]
            ):
                continue

            near_buy_count += 1
            if len(examples) >= 3:
                continue

            signal_rows = window["signal_rows"]
            highs = [float(c["high_price"]) for c in signal_rows]
            closes = [float(c["trade_price"]) for c in signal_rows]

            pivots = []
            for i in range(2, len(highs) - 2):
                if (
                    highs[i] >= max(highs[i - 2:i])
                    and highs[i] > max(highs[i + 1:i + 3])
                ):
                    pivots.append(i)

            if len(pivots) < 2:
                raise ValueError("저항선 고점 부족")

            a, b = pivots[-2], pivots[-1]
            if not (highs[b] < highs[a] and b > a):
                raise ValueError("하락 저항선 없음")

            slope = (highs[b] - highs[a]) / (b - a)
            tolerance = 0.004
            search_start = max(b + 1, len(signal_rows) - 8)
            search_end = len(signal_rows) - 2
            trace = []

            for i in range(b + 1, len(signal_rows)):
                previous_line = highs[b] + slope * ((i - 1) - b)
                current_line = highs[b] + slope * (i - b)
                previous_threshold = previous_line * (1 + tolerance)
                current_threshold = current_line * (1 + tolerance)
                previous_ok = closes[i - 1] <= previous_threshold
                current_ok = closes[i] > current_threshold

                trace.append({
                    "index": i,
                    "candle_time_kst": signal_rows[i]["candle_date_time_kst"],
                    "previous_close": closes[i - 1],
                    "previous_threshold": round(previous_threshold, 4),
                    "close": closes[i],
                    "current_threshold": round(current_threshold, 4),
                    "previous_at_or_below": previous_ok,
                    "current_strictly_above": current_ok,
                    "crossed": previous_ok and current_ok,
                    "inside_original_search": search_start <= i < search_end
                })

            examples.append({
                "signal_candle_kst": result["candle_time_kst"],
                "entry_close": result["entry_close"],
                "vol_ratio": result["vol_ratio"],
                "pivot_b_index": b,
                "search_start_index": search_start,
                "search_end_exclusive": search_end,
                "crossing_in_original_search": sum(
                    c["crossed"] and c["inside_original_search"]
                    for c in trace
                ),
                "crossing_outside_original_search": sum(
                    c["crossed"] and not c["inside_original_search"]
                    for c in trace
                ),
                "candles_after_pivot": trace
            })

        return jsonify({
            "ok": len(windows) > 0,
            "test_only": True,
            "market": market,
            "window_count": len(windows),
            "three_conditions_passed_but_no_breakout": near_buy_count,
            "examples": examples,
            "historical_value24_verified": False
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error_type": type(e).__name__
        }), 500

@app.get("/api/backtest-eth-followup-check")
def backtest_eth_followup_check():
    """임시 진단: ETH 돌파 이후 60분 검증."""

    scenarios = {
        "eth_1107": (
            "KRW-ETH",
            datetime(2026, 11, 7, 12, 0, tzinfo=KST),
            "2026-11-07T06:55:00"
        ),
        "eth_1101": (
            "KRW-ETH",
            datetime(2026, 11, 1, 12, 0, tzinfo=KST),
            "2026-11-01T07:00:00"
        ),
  
        "btc_1104": (
            "KRW-BTC",
            datetime(2026, 11, 4, 12, 0, tzinfo=KST),
            "2026-11-04T05:35:00"
        ),
        "xrp_1108": (
            "KRW-XRP",
            datetime(2026, 11, 8, 12, 0, tzinfo=KST),
            "2026-11-08T08:55:00"
        )
    }
    case = request.args.get("case", "eth_1107")

    if case not in scenarios:
        return jsonify({
            "ok": False,
            "error": "알 수 없는 테스트 사례"
        }), 400

    market, cutoff, event_time = scenarios[case]
    minute = 5


    try:
        rows = backtest_completed_rows(
            market, minute, cutoff, count=200
        )

        windows = make_backtest_windows(
            rows,
            minute=minute,
            lookback=80,
            forward_bars=36
        )

 
        by_time = {
            w["signal_rows"][-1]["candle_date_time_kst"]: w
            for w in windows
        }

        signal_windows = make_backtest_windows(
            rows,
            minute=minute,
            lookback=80,
            forward_bars=1
        )

        signal_by_time = {
            w["signal_rows"][-1]["candle_date_time_kst"]: w
            for w in signal_windows
        }

        event_window = by_time.get(event_time)

        if event_window is None:
            raise ValueError("기준 신호 캔들을 찾지 못함")

        event_result = backtest_replay_signal(
            event_window,
            volume_multiple=1.5,
            rr=2.0
        )

        if event_result is None:
            raise ValueError("기준 신호 분석 실패")
        event_cutoff = event_window["cutoff_kst"]

        value24_check = backtest_value24_at_cutoff(
            market, event_cutoff
        )
        future = event_window["evaluation_rows"]

        if len(future) != 36:
            raise ValueError("180분 평가 캔들 부족")

        entry = float(event_result["entry_close"])
        stop = event_result["stop_price"]
        target = event_result["target_price"]

        followup = []

        for index, candle in enumerate(future, start=1):
            candle_time = candle["candle_date_time_kst"]
            close = float(candle["trade_price"])
            high = float(candle["high_price"])
            low = float(candle["low_price"])

  
            next_window = signal_by_time.get(candle_time)
            next_result = (
                backtest_replay_signal(
                    next_window,
                    volume_multiple=1.5,
                    rr=2.0
                )
                if next_window is not None
                else None
            )

            followup.append({
                "minutes_after_signal_close": index * 5,
                "candle_time_kst": candle_time,
                "close": close,
                "high": high,
                "low": low,
                "return_pct": round(
                    (close / entry - 1) * 100, 3
                ),
                "replayed_signal": (
                    next_result["signal"]
                    if next_result else "분석 불가"
                ),
                "breakout_seen": (
                    next_result["breakout_seen"]
                    if next_result else None
                ),
                "support_held": (
                    next_result["support_held"]
                    if next_result else None
                ),
                "retest": (
                    next_result["retest"]
                    if next_result else None
                ),
                "stop_touched": (
                    stop is not None
                    and stop > 0
                    and low <= stop
                ),
                "target_touched": (
                    target is not None
                    and target > 0
                    and high >= target
                )
            })

        first_stop = next(
            (x["candle_time_kst"] for x in followup
             if x["stop_touched"]),
            None
        )

        first_target = next(
            (x["candle_time_kst"] for x in followup
             if x["target_touched"]),
            None
        )

        return jsonify({
            "ok": True,
            "test_only": True,
            "market": market,
            "event_candle_kst": event_time,
            "event_signal": event_result["signal"],
            "event_breakout_seen": event_result["breakout_seen"],
            "entry_reference_price": entry,
            "stop_reference_price": stop,
            "target_reference_price": target,
   
            "returns_pct": {
                "15m": followup[2]["return_pct"],
                "30m": followup[5]["return_pct"],
                "60m": followup[11]["return_pct"],
                "180m": followup[35]["return_pct"]
            },
         
            "first_stop_touch": first_stop,
            "first_target_touch": first_target,
            "same_candle_stop_target_ambiguity": any(
                x["stop_touched"] and x["target_touched"]
                for x in followup
            ),
 
            "buy_count_next_60m": sum(
                x["replayed_signal"] == "BUY"
                for x in followup[:12]
            ),
            "buy_count_next_180m": sum(
                x["replayed_signal"] == "BUY"
                for x in followup
            ),
           
            "first_three_candles": followup[:3],
            "all_followup_candles": followup,
            "fee_slippage_included": False,
            "historical_value24_verified": value24_check["ok"],
            "historical_value24_krw": value24_check["value24"],
            "historical_value24_pass_500m": value24_check["pass_500m"],
            "historical_value24_valid_candles": value24_check["valid_candles"],
            "historical_value24_missing_count": value24_check["missing_count"],
            "historical_value24_duplicate_count": value24_check["duplicate_count"],
            "eligible_buy_after_500m_filter": (
                event_result["signal"] == "BUY"
                and value24_check["ok"] is True
                and value24_check["pass_500m"] is True
            ) 
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error_type": type(e).__name__,
            "error": str(e)
        }), 500

def backtest_completed_rows_2pages(market, minute, cutoff_kst):
    """백테스트 전용: 과거 완료 캔들 최대 400개 수집."""

    recent = backtest_completed_rows(
        market, minute, cutoff_kst, count=200
    )

    if not recent:
        return []

    oldest = datetime.fromisoformat(
        recent[0]["candle_date_time_kst"]
    )

    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=KST)
    else:
        oldest = oldest.astimezone(KST)

    older = backtest_completed_rows(
        market, minute, oldest, count=200
    )

    by_time = {}

    for candle in older + recent:
        by_time[candle["candle_date_time_kst"]] = candle

    return [
        by_time[t] for t in sorted(by_time)
    ]



@app.get("/api/backtest-multiday-check")
def backtest_multiday_check():
    """임시 진단: 6종목 X 3날짜 과거 신호 재현."""
    markets = [
        "KRW-BTC", "KRW-ETH", "KRW-XRP",
        "KRW-SOL", "KRW-DOGE", "KRW-ADA"
    ]
    dates = ["2026-11-01", "2026-11-04", "2026-11-07", "2026-11-08"]
    minute = 5
    volume_multiple = 1.5
    rr = 2.0

    results = []
    totals = {"BUY": 0, "WATCH": 0, "WAIT": 0}
    total_windows = 0

    for date in dates:
        cutoff = datetime.fromisoformat(
            date + "T12:00:00"
        ).replace(tzinfo=KST)

        for market in markets:
            try:
                rows = (
                    backtest_completed_rows_2pages(
                        market, minute, cutoff
                    )
      if date == "2026-11-08" and market in ("KRW-BTC", "KRW-ETH", "KRW-XRP")
                    else backtest_completed_rows(
                        market, minute, cutoff, count=200
                    )
                )

                windows = make_backtest_windows(
                    rows,
                    minute=minute,
                    lookback=80,
                    forward_bars=12
                )



                counts = {
                    "BUY": 0,
                    "WATCH": 0,
                    "WAIT": 0
                }

                conditions = [
                    "close_above",
                    "volume_ok",
                    "retest",
                    "breakout_seen",
                    "support_held"
                ]


                condition_pass_counts = {
                    name: 0 for name in conditions
                }

                cumulative_pass_counts = {
                    name: 0 for name in conditions
                }

                pair_pass_counts = {
                    "close_and_volume": 0,
                    "close_and_retest": 0,
                    "volume_and_retest": 0
                }


                future_independent = True
                buy_examples = []
                near_buy_examples = []

                for window in windows:
                    result = backtest_replay_signal(
                        window,
                        volume_multiple=volume_multiple,
                        rr=rr
                    )

                    control_window = {
                        "cutoff_kst": window["cutoff_kst"],
                        "signal_rows": window["signal_rows"],
                        "evaluation_rows": []
                    }

                    control = backtest_replay_signal(
                        control_window,
                        volume_multiple=volume_multiple,
                        rr=rr
                    )

                    if result is None or control is None:
                        raise ValueError(
                            "과거 신호 재현 실패"
                        )

                    if result != control:
                        future_independent = False

                    signal = result["signal"]

                    if signal not in counts:
                        raise ValueError(
                            "알 수 없는 신호"
                        )

                    counts[signal] += 1

                    if (
                        result.get("close_above") is True
                        and result.get("volume_ok") is True
                    ):
                        pair_pass_counts["close_and_volume"] += 1

                    if (
                        result.get("close_above") is True
                        and result.get("retest") is True
                    ):
                        pair_pass_counts["close_and_retest"] += 1

                    if (
                        result.get("volume_ok") is True
                        and result.get("retest") is True
                    ):
                        pair_pass_counts["volume_and_retest"] += 1

                    passed_so_far = True

                    for name in conditions:
                        passed = result.get(name) is True

                        if passed:
                            condition_pass_counts[name] += 1

                        passed_so_far = passed_so_far and passed

                        if passed_so_far:
                            cumulative_pass_counts[name] += 1

                    if (
                        result.get("close_above") is True
                        and result.get("volume_ok") is True
                        and result.get("retest") is True
                        and result.get("signal") != "BUY"
                        and len(near_buy_examples) < 3
                    ):
                        near_buy_examples.append({
                            "candle_time_kst": result.get("candle_time_kst"),
                            "cutoff_kst": result.get("cutoff_kst"),
                            "signal": result.get("signal"),
                            "entry_close": result.get("entry_close"),
                            "vol_ratio": result.get("vol_ratio"),
                            "hypothetical_returns_pct": (
                                {
                                    f"{m}m": round(
                                        (
                                            float(window["evaluation_rows"][idx]["trade_price"])
                                            / float(result["entry_close"]) - 1
                                        ) * 100, 3
                                    )
                                    for m, idx in ((15, 2), (30, 5), (60, 11))
                                }
                                if len(window["evaluation_rows"]) == 12
                                and float(result["entry_close"]) > 0
                                else {}
                            ),
                            "breakout_seen": result.get("breakout_seen"),
                            "support_held": result.get("support_held"),
                            "failed_conditions": [
                                name
                                for name in ("breakout_seen", "support_held")
                                if result.get(name) is not True
                            ]
                        })

                    if signal == "BUY" and len(buy_examples) < 3:
                        entry = float(
                            result["entry_close"]
                        )

                        future = window["evaluation_rows"]

                        if entry <= 0 or len(future) != 12:
                            raise ValueError(
                                "BUY 사후 평가 자료 오류"
                            )

                        returns = {}

                        for minutes, index in [
                            (15, 2),
                            (30, 5),
                            (60, 11)
                        ]:
                            price = float(
                                future[index]["trade_price"]
                            )

                            returns[f"{minutes}m"] = round(
                                (price / entry - 1) * 100,
                                3
                            )

                        buy_examples.append({
                            "candle_time_kst": result[
                                "candle_time_kst"
                            ],
                            "entry_close": entry,
                            "vol_ratio": result[
                                "vol_ratio"
                            ],
                            "return_pct": returns
                        })

                group_ok = (
                    len(windows) > 0
                    and future_independent
                    and sum(counts.values()) == len(windows)
                )

                results.append({
                    "date": date,
                    "market": market,
                    "ok": group_ok,
                    "completed_candles": len(rows),
                    "window_count": len(windows),
                    "signal_counts": counts,
                    "condition_pass_counts": condition_pass_counts,
                    "cumulative_pass_counts": cumulative_pass_counts,
                    "pair_pass_counts": pair_pass_counts,
                    "future_evaluation_independent": future_independent,
                    "buy_examples": buy_examples,
                    "near_buy_examples": near_buy_examples
                })

                total_windows += len(windows)

                for name in totals:
                    totals[name] += counts[name]

            except Exception as e:
                results.append({
                    "date": date,
                    "market": market,
                    "ok": False,
                    "error_type": type(e).__name__
                })

    all_ok = (
        len(results) == len(markets) * len(dates)
        and all(x["ok"] for x in results)
        and sum(totals.values()) == total_windows
    )

    return jsonify({
        "ok": all_ok,
        "test_only": True,
        "market_count": len(markets),
        "date_count": len(dates),
        "test_group_count": len(results),
        "total_windows": total_windows,
        "total_signal_counts": totals,
        "buy_condition_funnel": {
            name: sum(
                group.get("cumulative_pass_counts", {}).get(name, 0)
                for group in results
            )
            for name in (
                "close_above",
                "volume_ok",
                "retest",
                "breakout_seen",
                "support_held"
            )
        },
        "results": results,
        "fee_slippage_included": False,
        "historical_value24_verified": False
    })

@app.get("/api/backtest-eth-data-check")
def backtest_eth_data_check():
    """임시 진단: 11월 4일 ETH 과거 5분봉 연속성 검사."""
    market = "KRW-ETH"
    minute = 5
    lookback = 80
    forward_bars = 12

    cutoff = datetime(
        2026, 11, 4, 12, 0, tzinfo=KST
    )

    try:
        rows = backtest_completed_rows(
            market,
            minute,
            cutoff,
            count=200
        )

        if not rows:
            raise ValueError("과거 캔들 없음")

        windows = make_backtest_windows(
            rows,
            minute=minute,
            lookback=lookback,
            forward_bars=forward_bars
        )

        times = []

        for candle in rows:
            dt = datetime.fromisoformat(
                candle["candle_date_time_kst"]
            )

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            else:
                dt = dt.astimezone(KST)

            times.append(dt)

        times.sort()

        expected_seconds = minute * 60

        gaps = []
        run_lengths = []
        run_length = 1

        for previous, current in zip(
            times, times[1:]
        ):
            delta_seconds = int(
                (current - previous).total_seconds()
            )

            if delta_seconds == expected_seconds:
                run_length += 1
                continue

            run_lengths.append(run_length)
            run_length = 1

            missing_count = None

            if (
                delta_seconds > expected_seconds
                and delta_seconds % expected_seconds == 0
            ):
                missing_count = (
                    delta_seconds // expected_seconds - 1
                )

            gaps.append({
                "previous_candle_kst":
                    previous.isoformat(),
                "next_candle_kst":
                    current.isoformat(),
                "interval_minutes":
                    delta_seconds / 60,
                "missing_5m_candles":
                    missing_count,
                "duplicate_timestamp":
                    delta_seconds == 0
            })

        run_lengths.append(run_length)

        required_bars = lookback + forward_bars

        expected_from_runs = sum(
            max(0, length - required_bars + 1)
            for length in run_lengths
        )

        theoretical_windows = max(
            0,
            len(times) - required_bars + 1
        )

        matching = (
            len(windows) == expected_from_runs
        )

        return jsonify({
            "ok": matching,
            "test_only": True,
            "market": market,
            "cutoff_kst": cutoff.isoformat(),
            "completed_candles": len(times),
            "first_candle_kst":
                times[0].isoformat(),
            "last_candle_kst":
                times[-1].isoformat(),
            "data_continuous": len(gaps) == 0,
            "gap_count": len(gaps),
            "gap_examples": gaps[:20],
            "run_lengths": run_lengths,
            "longest_continuous_run":
                max(run_lengths),
            "theoretical_windows_without_gaps":
                theoretical_windows,
            "expected_windows_from_runs":
                expected_from_runs,
            "actual_window_count":
                len(windows),
            "window_count_matches":
                matching,
            "historical_value24_verified": False
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error_type": type(e).__name__,
            "error": str(e)
        }), 500

@app.get("/api/backtest-data-quality-matrix")
def backtest_data_quality_matrix():
    """임시 진단: BTC, ETH, XRP의 3개 날짜 데이터 품질."""
    markets = [
        "KRW-BTC", "KRW-ETH", "KRW-XRP",
        "KRW-SOL", "KRW-DOGE", "KRW-ADA"
    ]    
    dates = ["2026-11-01", "2026-11-04", "2026-11-07"]

    minute = 5
    lookback = 80
    forward_bars = 12
    required_bars = lookback + forward_bars

    results = []

    for date in dates:
        cutoff = datetime.fromisoformat(
            date + "T12:00:00"
        ).replace(tzinfo=KST)

        for market in markets:
            try:
                rows = backtest_completed_rows(
                    market,
                    minute,
                    cutoff,
                    count=200
                )

                if not rows:
                    raise ValueError("과거 캔들 없음")

                times = []

                for candle in rows:
                    dt = datetime.fromisoformat(
                        candle["candle_date_time_kst"]
                    )

                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=KST)
                    else:
                        dt = dt.astimezone(KST)

                    times.append(dt)

                times.sort()

                gaps = []
                run_lengths = []
                run_length = 1
                expected_seconds = minute * 60

                for previous, current in zip(
                    times, times[1:]
                ):
                    delta = int(
                        (current - previous).total_seconds()
                    )

                    if delta == expected_seconds:
                        run_length += 1
                        continue

                    run_lengths.append(run_length)
                    run_length = 1

                    missing = None

                    if (
                        delta > expected_seconds
                        and delta % expected_seconds == 0
                    ):
                        missing = (
                            delta // expected_seconds - 1
                        )

                    gaps.append({
                        "previous_kst": previous.isoformat(),
                        "next_kst": current.isoformat(),
                        "interval_minutes": delta / 60,
                        "missing_5m_candles": missing
                    })

                run_lengths.append(run_length)

                expected_windows = sum(
                    max(0, n - required_bars + 1)
                    for n in run_lengths
                )

                windows = make_backtest_windows(
                    rows,
                    minute=minute,
                    lookback=lookback,
                    forward_bars=forward_bars
                )

                matches = (
                    expected_windows == len(windows)
                )

                estimated_missing = sum(
                    gap["missing_5m_candles"] or 0
                    for gap in gaps
                )

                results.append({
                    "date": date,
                    "market": market,
                    "ok": matches,
                    "completed_candles": len(rows),
                    "requested_candles": 200,
                    "data_continuous": len(gaps) == 0,
                    "gap_count": len(gaps),
                    "estimated_missing_candles":
                        estimated_missing,
                    "gap_examples": gaps[:3],
                    "run_lengths": run_lengths,
                    "expected_window_count":
                        expected_windows,
                    "actual_window_count": len(windows),
                    "window_count_matches": matches
                })

            except Exception as e:
                results.append({
                    "date": date,
                    "market": market,
                    "ok": False,
                    "error_type": type(e).__name__
                })

    all_ok = (
        len(results) == len(markets) * len(dates)
        and all(x["ok"] for x in results)
    )

    return jsonify({
        "ok": all_ok,
        "test_only": True,
        "group_count": len(results),
        "groups_with_gaps": sum(
            x.get("gap_count", 0) > 0
            for x in results
        ),
        "total_gap_count": sum(
            x.get("gap_count", 0)
            for x in results
        ),
        "total_valid_windows": sum(
            x.get("actual_window_count", 0)
            for x in results
        ),
        "results": results,
        "historical_value24_verified": False
    })



@app.get("/")
def index():
    return render_template_string(HTML)

@app.get("/api/scan")
def scan():
    scan_start = time.perf_counter()
    try:
        minute=int(request.args.get("tf","5"))
        minute=5  # C5 uses confirmed 5m candles only
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
from daytrade_review import register_daytrade_review
register_daytrade_review(app, globals())
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","10000")))
