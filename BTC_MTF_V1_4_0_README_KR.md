# BTC MTF V1.4.0 — 기존 BUY vs 강화 BUY 연구판

기존 V1.3.3은 btc-mtf-v1-3 브랜치에 보존. 이 브랜치는 별도 연구판.

## 비교 규칙
- 기존 BUY: V1.3.3 10점 반등 판정을 변경하지 않고 동일 시각에 계산.
- 강화 BUY: 기존 BUY 중 9점 이상, 일봉 MA20 상회/상향, 4시간봉 MA20 상회/상향과 MA7/14 상승, 1시간봉 반등, RSI14 50 이상, MACD 히스토그램 양수, 거래대금 1.1배 이상, BTC 장단기 비하락, 최근 봉 공백 없음, 저점/지지 위험 9% 이내, 선택 검색봉 확인을 모두 통과해야 한다.
- 탈락한 기존 BUY는 비교표와 CSV에 그대로 남기고 WATCH로 구분.
- 후보 전체 CSV에는 legacy_signal, strict_signal, strict_gate_passed, strict_failed_checks, strict_gate_checks, h4_ma20, h4_ma20_prev 등의 상세 칼럼을 기록한다.

## 백테스트 해석
- 동일 시각과 같은 완료봉, 같은 가상 진입/청산 가정으로 두 신호군 비교.
- 120시간 청산 중 누락봉이 청산 전에 있으면 UNKNOWN. 청산 후 누락은 이전에 확정한 결과를 취소하지 않음.
- 동일 시간봉에서 손절/익절 동시 도달 시 보수적 손절 우선.
- *주의*: 현재 상장 종목과 현재 24시간 거래대금으로 역사적 검색군을 선정하므로 미래정보 및 생존편향 존재.
- UNKNOWN은 승리나 손실로 계산하지 않음. 실제 체결가, 호가, 슬리피지 반영 불충분.
- 10월 31일 실패 사례를 보고 설계한 필터이므로 별도의 날짜에 검증하기 전에는 우수한 전략이라고 판단하지 않는다.

## 실행
1. pip install -r requirements.txt
2. python -m unittest discover -s tests -p test_mtf_v14.py
3. gunicorn bithumb_btc_mtf_v1:app --workers 1 --threads 4 --timeout 120

공개 시세 API만 사용, 자동주문 없음.
