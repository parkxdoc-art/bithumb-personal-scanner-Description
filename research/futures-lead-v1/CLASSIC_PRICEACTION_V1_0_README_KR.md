# BITHUMB 클래식 가격행동 3전략 비교 연구 V1.0

- 연구실: https://bithumb-futures-lead-v1.onrender.com/classic
- 연구용 읽기 전용. 주문·자동매매·실전 BUY 알림 없음.
- 기존 G형, V2, V3 전략 로직 변경 없음. 기존 연구용 Render 서비스에 **별도 URL/별도 모듈**로 연결.
- 빗썸의 공식 공개 5분봉 API만 사용. 바이낸스·Hyperliquid·제3자 지갑 API 사용하지 않음.
- 설치: Python 3.9+ 기본 라이브러리만 필요. 별도 패키지 없음.

## 실행

```
python classic_priceaction_lab.py --selftest
python -m unittest -v test_classic_priceaction_v1
python classic_priceaction_lab.py --web --port 10020
python classic_priceaction_lab.py --days 6 --coins BTC,ETH,XRP,SOL,SUI
```

`--web` 독립 실행 시 `http://localhost:10020/classic`에서 접속.

## 세 전략의 연구용 정의

공개된 매매 개념을 빗썸에 맞게 정량화한 **새 규칙**. 트레이더들의 실제 매매/성과가 아니며 원전과 동일하지 않습니다.

1. `VOLMAN`: 최근 5분봉 첫 상승 돌파, 3봉 이상 눌림·20EMA 근처 지지, 첫 돌파 고점을 종가 재돌파한 두 번째 매수 시도.
2. `BROOKS`: 상승추세 중 첫 5분봉 상승 시도가 실패한 뒤, EMA20 근처 눌림과 두 번째 상승 종가 돌파를 확인(H2 개념을 단순화).
3. `RASCHKE`: 상승추세, 5분봉 ADX14 ≥ 30·+DI 우세, 상승하는 ADX와 EMA20 부근 눌림, 이후 종가 반등(Holy Grail 개념을 단순화).

## 공통 백테스트 설계

- 기본 BTC·ETH·XRP·SOL·DOGE·ADA·LINK·AVAX·SUI·PEPE 10종목, 최근 6일. 최대 20종목·13일.
- 5분봉에서 **이미 끝난 정시 1시간봉**만 집계. 1시간봉 EMA20 > EMA50, EMA20 상승, 종가가 EMA20 위.
- 24시간 5분봉 거래대금 합산 5억 원 이상. 결측 5분봉은 보간하지 않으며 필요한 전후 연속성이 깨진 신호는 제외.
- 신호가 나온 완료 5분봉 다음 **5분봉 종가**로 가상 진입, 그 후 **완료 5분봉 종가**에서만 목표·손절을 판정.
- 수수료: 왕복 0.50%. 순수익 +0.50% 익절, 가격 손절 -1.00%, 60분 만기.
- 슬리피지 0% 가정. 고가·저가에서 실제 체결되었다고 가정하지 않음.
- 코인·전략별 3시간 쿨다운, 전략별 전체 코인 합산 하루 최대 4건. 같은 시간 중복 코인은 이름순으로 선택, 수익률 사후선택 금지.
- 일별 결과에 거래가 없는 날짜 0건 포함. `총순수익률 포인트`는 거래별 순수익률 단순 합계로 복리 자산수익률 아님.
- 결과 CSV: `BITHUMB_CLASSIC_PRICEACTION_V1_0.csv`.
- 한 번의 동일 설정 결과는 완료 후 10분 동안 재사용해 API 요청 절감.

## 연구의 한계

- 세 트레이더의 실제 수익률이나 검증된 전략 우수성을 입증하지 않습니다. 결과는 이 프로그램의 정의와 데이터·비용 가정에만 유효합니다.
- API 조회 실패, 제한 418/429, 시장에서 봉 누락 등의 경우 해당 코인은 제외/오류를 표시합니다. 호출 제한이 있으면 추가 호출 중단.
- 현물 거래의 실제 슬리피지·호가·포지션 크기·가격 충격·세금은 반영하지 않습니다.
- 6일은 통계적으로 짧습니다. 13일 결과나 다른 기간도 확인하고, 좋은 전략을 고른 뒤 **새 기간의 표본 외 검증**이 필요합니다.
- 결과에 실제 매수/매도 주문을 연결하지 마세요. 손절 -1% 도달 시 5분봉 종가 체결 가정에서는 -1.5%보다 손실이 커질 수 있습니다.

## 공개 참고 자료

- Bob Volman, *Forex Price Action Scalping* — second-break/double-doji-with-trend 가격행동 개념.
- Al Brooks, *Trading Price Action* — H2 두 번째 진입 개념.
- Linda Bradford Raschke & Laurence Connors, *Street Smarts* — ADX>30, EMA20 눌림(Holy Grail).
- 빗썸 캔들 API https://apidocs.bithumb.com/reference/분minute-캔들-조회 — `to` 시각 KST, 분봉 최대 200개 조회.