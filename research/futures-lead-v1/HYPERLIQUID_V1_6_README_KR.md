# Hyperliquid 공개 트레이더 파일 연구 V1.6

## 핵심

- URL: https://bithumb-futures-lead-v1.onrender.com/traders/offline
- 분석은 브라우저 내부에서 수행. 원본 CSV/JSON 파일은 서버로 보내지 않음.
- 외부 바이낸스·Hyperliquid API 조회 0회. API 418/429 차단과 관계없이 **분석 자체**는 가능.
- 거래 주문, 시드 문구, 개인키, 지갑 연결, 계정 로그인 불필요.
- 기존 G형 스캐너 V1.4.1 및 트레이더 API 연구 V1.5.1 보존.
- 실제 트레이더 원본 파일 확보와 실전 성과 검증은 별개. 현재 실거래 연구 결과 미확보.

## 실전 사용 순서

1. 제3자 내보내기 사이트 https://trade-export.hypedexer.com/ 방문. 이 사이트는 공식 Hyperliquid 앱이 아닌 외부 제공 서비스이므로 이용조건/개인정보/제공 형식을 확인.
2. 공개 지갑 주소를 입력하고 최근 2주 기간 설정, CSV 또는 JSON을 받아봄. **파일에 체결 항목이 없거나 제공처가 차단되면 수집 불가**일 수 있음. 파일 다운로드 지원 여부는 수집 환경에 따라 다름.
3. 공식 Hyperliquid 앱 https://app.hyperliquid.xyz/ 의 Trade History → Export CSV도 참고 가능하지만 자기 지갑 연결이 필요한 기능일 수 있음. 타인 지갑 기록을 자동 보장하지 않음.
4. V1.6 화면을 열고 `거래 기록 파일 선택` → `최근 6일` → `선택한 파일 분석` 실행.
5. 파일별 오류/제외 내역을 확인한 뒤 `왕복 거래 CSV 저장`, `원본 진단 CSV 저장`으로 결과 내려받기.
6. 생성된 **실제 데이터** CSV를 보내주면 트레이더별 단타 패턴을 비교할 수 있음.

연구 후보 공개 주소(실명/수익성 인증된 인물로 단정 금지):

- BobbyBigSize 표기 주소: `0x7fdafde5cfb5465924316eced2d3715494c517d1`
- x35767 표기 주소: `0x880ac484a1743862989a441d6d867238c7aa311c`
- 익명 주소: `0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36`

## 지원 형식

A) Hyperliquid 원시 체결 CSV/JSON 필수 값:

`time, coin, side, px, sz, startPosition, closedPnl, fee`

선택: `feeToken, tid, hash, wallet, alias, dir`. `feeToken`이 USDC 외 통화라면 분모 통일이 불가해 제외함. `side`는 B 매수, A 매도임. 출처 JSON의 `userFillsByTime` 배열도 지원하나 **Render에서 API 호출은 하지 않음**.

B) 이미 정리된 완결 거래 CSV 필수 값:

`coin, side, entry_kst, exit_kst, net_proxy_usdc`

또는 `entry_time, exit_time, net_usdc` 등의 대체 영문 필드. `side`는 LONG/SHORT, B/A 등 지원. 입력 자체가 완결 거래임을 전제로 하므로 미검증/조작된 파일은 검증할 수 없음.

시각: UNIX 밀리초/초, 명시적 ISO 시간대 우선. 시각 정보 없는 원시 체결 및 `entry_time/exit_time`은 UTC, `entry_kst/exit_kst`는 한국시간으로 간주. 타임존 표기가 없는 외부 CSV는 정확한 시각을 재확인할 것.

C) 미지원/주의:

- V1.5.1의 `DIAGNOSTICS.csv`는 실제 체결 기록이 아니라 오류 보고서라서 분석 대상 아님.
- 원시 체결에 `startPosition` 필드가 없다면 왕복 포지션을 신뢰성 있게 복원할 수 없어 거부됨. 출처/스키마를 확인한 뒤 별도 변환이 필요함.
- 5MB/파일, 50,000행/파일, 최대 12개 파일, CSV/JSON만 지원. ZIP 미지원.
- 파일 구간 시작 전부터 열려 있던 포지션, 뒤늦게 끊긴 포지션, 중간 체결이 빠진 기록, 포지션 방향 전환은 보수적으로 제외.
- 한 지갑 기록이 파일 여러 개에 걸쳐 나뉘면 각 파일 내에서만 포지션을 복원하므로 파일 경계를 넘는 거래는 누락될 수 있음.

## 결과 해석

- 5분~4시간 포지션만 단타 집계. 원본의 진입/청산 시점이 완결된 경우만 인정.
- 수익률보다 USDC 단위의 `closedPnl - fee` 추정 손익 합계를 표시. 계좌 전체 수익률이나 세후 실현 손익, 빗썸 현물 전략의 수익성을 뜻하지 않음.
- 포지션 미실현손익, 선물 펀딩, 타 거래소 헤지, 입출금, 누락된 거래는 계산되지 않음.
- 선택 기간 6/13일은 파일 안의 **가장 늦은 완결 거래 청산시각**을 기준으로 적용함. 따라서 빈 파일/불완전 파일은 기간 기준을 만들 수 없음.
- 닉네임은 지갑 소유자 인증이 아님. 공개 순위에 등장했다는 이유만으로 우수 트레이더라는 결론 금지.

## 검증

- 오프라인 Node.js 기능 테스트 16개 통과: CSV 따옴표/줄바꿈, 시간 해석, 롱/숏 체결, 부분 청산, 중복/불완전 체결, 추정 수익 계산, CSV 수식 삽입 방지 등.
- Chromium 모바일 화면에서 **가상 테스트 원시 체결 5행 → 2개 완결 거래**로 분석되는지 확인. 이는 실제 인물의 거래 성과가 아님.
- 빗썸·바이낸스·Hyperliquid 거래소 API 호출을 하지 않는지 정적 확인.

## 근거 자료

- Hyperliquid 공개 체결 필드 공식 문서: https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/info-endpoint
- Hyperliquid 과거 데이터 문서: https://www.hyperliquiddocs.io/historical-data.html
- 외부 내보내기 제공 사이트: https://trade-export.hypedexer.com/
- 공식 앱 CSV 내보내기 절차를 설명하는 독립 도움말: https://support.cryptact.com/hc/en-us/articles/36139200799001-How-to-get-trade-history-from-Hyperliquid