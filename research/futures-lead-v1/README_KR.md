# 빗썸 × 바이낸스 선물 데이터 선행성 연구 V1.1

**기존 V2·V3와 완전히 분리된 읽기 전용 독립 연구판**입니다. 자동주문·계좌 조회·매수 알림이 없습니다.

## V1.1 Binance API 오류 수정 (2026-11-21)

- V1.0에서 최근 6일치를 한 번의 `startTime`~`endTime` 요청으로 보내 `HTTP 400 / -1130 / parameter 'endTime' is invalid` 오류가 발생했습니다.
- 15분 선물 데이터는 최대 **400구간씩** 시간을 나누어 불러옵니다. 서버가 동일 오류를 반환하면 해당 데이터 조회를 **96구간(1일)** 단위로 자동 축소해 재시도합니다.
- 데이터가 비는 페이지가 있어도 남은 기간을 탐색하며, 실제 존재하는 데이터만 분석합니다. 최신 30일 제한·완료봉 조건은 유지합니다.
- 화면·버전·CSV 이름을 V1.1로 통일했습니다. 8개 오프라인 회귀 테스트 통과. 실제 시장 결과를 보장하지 않습니다.
- 새 연구판 Live 주소: https://bithumb-futures-lead-v1.onrender.com
- GitHub 경로: `research/futures-lead-v1/` (독립 브랜치 `futures-lead-research`). **기존 V2·V3 서비스 수정 없음.**

## 생성 파일

- `futures_lead_research.py`: 웹 화면 + 연구 로직 + CLI (Python 표준 라이브러리만 사용)
- `futures_lead_research_V1_1_FULL.txt`: 전체 코드 열람용
- `test_futures_lead_research.py`: 오프라인 단위 테스트 6개
- `Procfile`: 지원 플랫폼의 실행 설정
- `requirements.txt`: 추가 Python 패키지 불필요

## 실행 / Render 배포

### GitHub
1. **새 분기 `futures-lead-research`** 를 생성합니다. 현재 V2/V3 코드·배포 브랜치는 유지합니다.
2. 파일은 신규 분기의 `research/futures-lead-v1/` 폴더에 저장합니다. **기존 루트 코드는 변경하지 않습니다.**
3. 신규 분기로 Render에서 **별도 Web Service**를 추가합니다. 기존 서비스 설정은 변경하지 않습니다.

### Render 신규 Web Service
- Runtime: `Python`
- Branch: `futures-lead-research`
- Build Command: `pip install -r research/futures-lead-v1/requirements.txt` (추가 패키지가 없어도 성공)
- Start Command: `python research/futures-lead-v1/futures_lead_research.py --web`
- Health Check Path: `/health`
- Optional environment variable: `BINANCE_API_KEY` (바이낸스 상위 트레이더 포지션 비율이 인증을 요구하는 경우 **공개 시장 데이터용 키만** 사용; **권한 제한**, 출금/주문 권한 금지)
- 주의: Render 환경에서 바이낸스 선물 API가 지역/IP 제한을 할 수 있습니다. 코드만으로 우회되지 않습니다.
- 새 배포 서비스의 URL은 **새 Render 서비스가 Live 된 뒤에만** 확인할 수 있습니다. 이 패키지 제작 시 배포/실거래 데이터 수집은 수행하지 않았습니다.

### 로컬 / 콘솔
```bash
python futures_lead_research.py --selftest
python -m unittest -v test_futures_lead_research
python futures_lead_research.py --web --port 10000
# 브라우저에서 http://127.0.0.1:10000

# 웹 없이 실제 통계 계산 후 CSV 출력
python futures_lead_research.py --coins ETH,XRP,SOL --days 6
```

## 수집 범위와 시간 정렬

1. 빗썸의 `KRW-코인`과 바이낸스의 `USDT 무기한 선물`이 공통으로 존재하는 종목만 처리. 원칙적으로 동일 자산 `baseAsset` 매칭. `PEPE`, `SHIB`, `BONK`, `FLOKI`는 직접 선물이 없으면 `1000*` 계약과 매칭할 수 있음(가격이 아니라 비율/수량 변화율을 사용). 스테이블코인 제외.
2. 바이낸스 15분 데이터: `openInterestHist`(수량 OI), `topLongShortPositionRatio`(상위 트레이더 포지션 비율), `takerlongshortRatio`(시장가 매수량/매도량). 15분봉에서 최근 1시간 변화를 계산.
3. **시간정렬**: OI와 상위 롱/숏 비율의 `timestamp`는 구간 **종료**; 시장가 체결량의 `timestamp`는 구간 **시작**이므로 15분을 더해 같은 종료 구간으로 정렬.
4. 신호가 확정되는 시각 `t` 이후 최초 빗썸 5분봉(시작시각 `t`)의 **종가**를 가상 진입가로 사용. 따라서 진입시각은 `t+5분`. 완성되지 않은 5분봉은 사용하지 않음.
5. `t+30`, `t+60`, `t+180`에 시작하는 5분봉의 종가로 향후 30·60·180분 수익률 계산. 편도 수수료 0.25% × 2 = 0.5%를 고정 차감. 슬리피지/체결 현실성/시장 충격은 **미반영**.
6. 각 코인·규칙마다 신호 후 180분 동안 같은 규칙 중복 신호를 제거. 기록에는 **관찰 가능한 완결 신호만** 포함.
7. 바이낸스 과거 통계는 일반적으로 최근 약 30일 제한. 이 연구판은 기본 6일, 최대 13일이며 한 페이지 500건을 초과하면 페이지를 나눠 요청. 빗썸 5분봉 최대 200개/호출이므로 추가 페이징함.

## 조건 (임시 연구 가설, 실전매수 기준 아님)

공통: 빗썸 1시간 과거 가격 변화가 **+0.5% 이상**, 최근 1시간 바이낸스 시장가 매수량/매도량 비 **1.08 이상**, 기준 직전 1시간 비교. 아래 규칙만 달라짐.

- **A 롱·OI·매수 동행**: OI 수량이 **+0.8% 이상**, 상위 롱/숏 포지션 비율이 **+2% 이상** 상승 + 공통 조건.
- **B OI 감소·롱비율 상승**: OI 수량이 **-0.8% 이하**이고 상위 롱/숏 비율이 **+2% 이상** 상승 + 공통 조건. '숏 청산'이라고 **확정하지 않음**.
- **C OI·매수 강세(비율 제외)**: OI **+0.8% 이상** + 공통 조건. A와 비교해 상위 롱/숏 비율의 추가 예측력을 검사.
- **M 가격 상승만**: 빗썸 최근 1시간 가격 변화 **+0.5% 이상**. 선물 데이터 없이 매긴 단순 비교군.

이후 통계는 규칙별 건수/승률/평균 순수익률과 일자별 손익 건수를 보여줍니다. 단, 표본 간 시간·종목 구성이 다르므로 이 비교만으로 통계적으로 유의한 우위를 입증할 수 없습니다. 역사적 관계가 실제 매매 수익으로 연결된다는 보장은 없습니다.

## 정확성과 제한

- **바이낸스 공식 2026년 API 문서**는 상위 트레이더 포지션 비율 엔드포인트에 `X-MBX-APIKEY` 필수로 표시합니다. 이 호출에 401/403이 반환되면 자동으로 A/B를 배제하고 C/M만 분석하며 '비율 없음' 경고를 표시합니다. 절대로 비율 값을 임의 생성하지 않습니다.
- 거래소 통신 실패, 지표 누락, 페어 미상장, 일부 빈 캔들은 강제로 보간하거나 가짜 값으로 대체하지 않습니다. 결과가 없으면 오류 또는 0건을 보여줍니다.
- **신규 상장 14일 미만** 자동 판별은 이번 V1.1에 없습니다. 기존 V3 시장 전체 거래대금 집중도 연구와는 분리된 과제입니다.
- 소량 샘플·종목 선택·수수료/슬리피지 가정·신호 직후 종가 체결의 이상화 때문에 결과 해석은 매우 신중해야 합니다.
- 실거래 데이터 분석은 네트워크 연결과 서비스 배포가 가능한 환경에서 별도로 실행해야 합니다. **오프라인 테스트 통과는 실제 전략의 수익성 검증이 아닙니다.**

## 참조 문서

- https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data
- https://apidocs.bithumb.com/reference/분minute-캔들-조회
- https://apidocs.bithumb.com/docs/빠른-시작-가이드

## 커밋 메시지

```
feat: add independent Bithumb-Binance futures lead research V1.1
```