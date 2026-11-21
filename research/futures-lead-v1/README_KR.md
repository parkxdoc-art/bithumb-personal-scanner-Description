# BITHUMB × Binance 선물 선행성 연구 V1.3

2026-11-21 / 독립 연구 전용, **자동 주문·계좌 접근 없음**. GitHub: `parkxdoc-art/bithumb-personal-scanner-Description`, branch `futures-lead-research`. 서비스: https://bithumb-futures-lead-v1.onrender.com .

## 이번 정리

- V1.2에서 수익이 약했던 **A/B/C/M/E/P를 주력 연구 신호에서 제거**했습니다.
- **G형만 주력 연구 후보**로 보입니다: 빗썸 직전 1시간 수익률 -0.5~+0.3%, 바이낸스 1시간 OI +0.8% 이상, taker 매수/매도량 1.08배 이상, BTC 직전 1시간 수익률 0% 이상.
- **N**: G와 같은 종목·한국 날짜, ±3시간 이내, 가격횡보+BTC 비하락이지만 G의 선물 조건에는 해당하지 않는 가장 가까운 비교 기록(후행 사후 매칭). 최대 1:1, 후보 신호 아님.
- **X**: G의 OI/매수/횡보 기준은 충족하지만 BTC가 하락한 경우. BTC 필터 가치 분석을 위한 비교 기록일 뿐 후보 신호 아님.
- 상위 트레이더 롱/숏 비율 API는 G 기준에 사용하지 않으므로 요청하지 않습니다. 기존 V1.2 조건 수치를 그대로 유지해 사후 최적화 위험을 줄였습니다.

## 청산·보유 시간 실험

- 15/30/60/180분 종료 종가 기준 순수익률. 왕복 수수료 **0.5%** 적용; 슬리피지 0% 가정.
- 60분 이내 **첫 번째 5분 완료봉 종가**에서 순수익 +0.5%를 충족하면 '익절'. 이 조건은 가격 +1.0% 상승에 해당합니다.
- 순익 목표를 먼저 충족하기 전에 5분 완료봉 종가가 진입가 대비 **-1.0%** 이하라면 '손절'. 수수료 반영 순수익은 -1.5% 이하가 될 수 있습니다.
- 두 조건이 충족되지 않으면 60분 후 완료봉 종가로 가상 청산합니다.
- 실제 거래소 지정가·스탑 주문 체결을 시뮬레이션하는 건 아닙니다. 5분봉 **고가/저가가 아니라 종가만** 사용하기 때문에 목표·손절에 닿았더라도 수집된 종가가 해당 가격에 닿지 않으면 표시하지 않습니다.
- 각 신호의 시작점은 15분 데이터 구간 종료 시각이고, 진입은 **신호 뒤 첫 번째 5분봉 완료 종가**입니다. 신호 당시에는 미래 봉 사용하지 않으며, 평가 구간을 확보할 수 없는 최근 신호는 제외합니다.

## 사용법

1. Render 페이지 접속 → 코인 목록·기간(기본 최근 6일) 확인 → '연구 실행'.
2. **G형 성과**, **N/X 통계 비교**, G의 시간별 수익률·목표/손절 선후를 확인.
3. 새 CSV 다운로드: `BITHUMB_FUTURES_LEAD_V1_3.csv`.
4. 데이터 오류/누락이 있다면 그 부분을 먼저 확인. 종목마다 실제 수집이 실패할 수 있습니다.

## 파일

- `futures_lead_research.py`: 원본 1파일 웹·CLI 프로그램(파이썬 표준 라이브러리)
- `futures_lead_research_V1_3_FULL.txt`: 코드 열람 TXT
- `test_futures_lead_research.py`: 15개 오프라인 테스트
- `Procfile` / `requirements.txt`: 기존 Render 서비스에서 사용 중인 설정
- `README_KR.md`: 문서

## 로컬 테스트

```bash
python futures_lead_research.py --selftest
python test_futures_lead_research.py
python futures_lead_research.py --web --port 10000
```

## GitHub + Render

- GitHub 브랜치: `futures-lead-research`, 폴더: `research/futures-lead-v1/`.
- Render(기존 독립 연구 서비스만 업데이트): `bithumb-futures-lead-v1`, Free, Python, Singapore.
- Build: `pip install -r research/futures-lead-v1/requirements.txt`
- Start: `python research/futures-lead-v1/futures_lead_research.py --web`
- Live URL: https://bithumb-futures-lead-v1.onrender.com
- `main`, `feature/scanner-v2`, `v3-scanner`와 다른 Render 서비스는 변경하지 않습니다.

## 연구 한계

- 기존 V1.2 G형 결과는 6일간 10건·1시간 평균 -0.244%이므로 **실전 BUY 근거가 아닙니다**.
- G는 소표본이고, N은 사후 매칭이라 독립적 무작위 대조군이 아닙니다. G와 N의 표본 수가 다를 수 있습니다.
- 동일 코인에서 3시간 쿨다운이 있지만 신호 간 상관성과 시장 장세 변동이 존재합니다. N/X도 거래 권유가 아닙니다.
- Binance 과거 지표는 최대 최근 30일 제한이 있을 수 있으며 본 연구 UI 기간은 1~13일. 신규 상장 종목 나이는 검증하지 않습니다.
- 시장 API가 지역·네트워크·일시적인 오류로 실패할 수 있습니다. 자료가 없을 때 수익률을 임의로 생성하지 않습니다.
