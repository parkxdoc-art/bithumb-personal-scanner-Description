# V1.5 — 공개 트레이더 체결 패턴 연구

## 목적과 범위

기존 **바이낸스 선물 G형 연구 V1.4.1은 변경하지 않습니다**. 같은 Render 서비스에 `/traders` 독립 페이지를 추가하며, 공개 Hyperliquid 체결 정보로 *체결 이력 → 왕복 포지션 → 5~240분 보유 습관*을 탐색합니다. 이 연구는 자동매매·카피트레이딩·지갑 연결·실전 BUY 알림이 아닙니다. 바이낸스 API도 호출하지 않습니다.

## 표본 — 지갑이 곧 개인 신원을 뜻하지 않습니다

- BobbyBigSize — `0x7fdafde5cfb5465924316eced2d3715494c517d1`
- x35767 — `0x880ac484a1743862989a441d6d867238c7aa311c`
- 0xb83d…6e36 — `0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36`

이 이름과 주소는 Hyperliquid 공개 순위표를 표시하는 LabelYX의 기존 페이지에서 확인된 **사전 관측 후보**입니다. 실제 사람인지, 지금도 같은 관리자가 거래하는지, 수익자인지는 확인되지 않았습니다. 이들은 추천 트레이더가 아닙니다.

## 연구 지표

- 최근 6일 또는 13일의 공개 체결 수 (체결 ≠ 거래 횟수)
- 관측 기간 내 시작과 끝이 모두 보이는 완결 왕복 포지션
- 그중 5~240분 보유 포지션 수, 롱·숏 비율, 중위 보유시간
- 공개 체결의 `closedPnl - fee` 기반 **진단용 추정치**: 중위 손익, 손익 양수 비율, 최대 손실
- 부분청산, 잘린 이력, 반대 포지션 뒤집힘, 중간 데이터 오류 시 완전한 왕복 거래로 추측하지 않음
- 지갑별 기록을 KST CSV로 내보내기

## 자료 수집

`POST https://api.hyperliquid.xyz/info`의 `userFillsByTime` (오픈 API; 인증키 불필요, 읽기 전용).
각 요청은 UTC 하루 이내, 2,000건 한도에 도달하면 구간 분할, 최대 요청 96회, 요청 간 간격 0.7초입니다. API 418/429 발생 시 즉시 중단, 다시 실행은 30분간 잠금(오류는 10분). 지갑 3개 순차 조회.

## 해석의 한계

1. **상위 지갑이라는 이유로 우수 트레이더를 보장할 수 없습니다.** 수익률, 위험, 입출금, 외부 헤지, 펀딩비와 청산은 종합 감사하지 않았습니다.
2. 관측 전에 열린 포지션, 10,000건 API 최근 체결 한도에 잘린 이력, 부분 청산이 기간 경계를 넘는 경우는 제외합니다. 이 때문에 결과 표본이 작을 수 있습니다.
3. 공개 `closedPnl`과 `fee`를 단순 결합한 값은 지갑 전체 투자성과가 아닙니다. 손익을 수익률로 바꾸려면 증거금·레버리지·자금 이동까지 필요합니다.
4. Hyperliquid 선물에서 보이는 포지션의 수익이 빗썸 현물의 수익률을 뜻하지 않습니다. **패턴 추출 후 별도 빗썸 백테스트**가 필요합니다.
5. 실제 API 접근 가능 여부 및 지갑 최신 체결 건수는 Live 연구 실행 후에만 알 수 있습니다. 오프라인 테스트가 API 수집 성공을 의미하지 않습니다.

## 배포

- 기존 GitHub: `parkxdoc-art/bithumb-personal-scanner-Description`
- 기존 브랜치: `futures-lead-research`
- 파일: `research/futures-lead-v1/hyperliquid_wallet_study_v15.py` 및 기존 `futures_lead_research.py`에 **새 라우트만 추가**
- 기존 Render 웹 서비스/주소 유지: https://bithumb-futures-lead-v1.onrender.com/traders
- 기존 메인 페이지는 여전히 V1.4.1, 독립 연구 페이지는 V1.5로 표시합니다. V2·V3는 변경하지 않습니다.

## 터미널 시험

`python hyperliquid_wallet_study_v15.py --selftest` (오프라인)

`python hyperliquid_wallet_study_v15.py --fetch --days 6` (실제 Hyperliquid 조회, 네트워크 필요)

통합 웹 서버: `python futures_lead_research.py --web` → `/traders` 이동.

## 공개 참고 문서

- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
- https://labelyx.com/whales
- https://labelyx.com/0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36