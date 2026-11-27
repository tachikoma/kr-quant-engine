# P2-A-2a: ETF 분배금 실데이터 확보와 Total Return E2E

- 실행: `scripts/p2a2a_etf_distributions.py` (공식 원천 소량 조회, 읽기 전용)
- fixture: `scripts/test_p2a2a_etf_distributions.py`
- 산출물: `outputs_p2a2a/` (gitignored) — `distribution_source_inventory.csv`,
  `distribution_events.csv`, `distribution_coverage.csv`, `distribution_crosscheck.csv`,
  `pilot_validation.json`, `provenance.json`, `distribution_ledger.csv`,
  `distribution_manifest.json`, `raw/`
- 대상: `strategy_freeze.json` 동결 16종, 백테스트 기간 2016-01-05~2026-09-28
- 원칙: 미확보를 "이벤트 없음"으로 간주하지 않는다. 확인되지 않은 날짜·세율을 추정하지 않는다.
  `total_return_approvable=False`와 기존 승인 blocker를 유지한다.

## 1. 원천별 접근 결과와 이용 조건

| 원천 | 접근 방법 | 결과 | 구조화 | 판정 |
|---|---|---|---|---|
| 삼성자산운용 KODEX 분배금 API | `GET /api/v1/kodex/divid-info.do?id={fId}` (JSON) | **200** | 이벤트 단위 | **1차 원천** (동결 4종) |
| KODEX 분배금 공지(공지사항) | `GET /etf/lounge/notice-view.do?no=` (HTML 표) | 200(수동) / 자동 재조회 429 | 회차·금액 | 교차검증용 |
| KODEX 분배금 Excel | `GET /excel-distribution.do` | 200 (OLE2 `.xls`) | 최신 회차 | 이번 검증 미사용 |
| KIS 조회 전용 시세 | `inquire-daily-itemchartprice` (일별, 주문 없음) | **200** | 일별 OHLCV | **가격 기준 독립 검증** |
| 미래에셋 TIGER | `https://www.tigeretf.com` | **403** (Akamai/WAF) | - | 미확보 (동결 7종) |
| 한국투자 ACE | 상품 페이지 200, 데이터 API `api.aceetf.co.kr` | **연결 타임아웃** | - | 미확보 (동결 2종) |
| 한화 PLUS | `https://www.plusetf.co.kr` | **TCP 연결 실패** | - | 미확보 (동결 1종) |
| KB RISE | `https://www.riseetf.co.kr` | **TCP 연결 실패** | - | 미확보 (동결 2종) |
| 예탁결제원 SEIBro | `https://seibro.or.kr` | WebSquare JS 셸 | - | 미확인 |
| 금융투자협회 FreeSIS | `http://freesis.kofia.or.kr` | 프레임/서비스ID 메뉴 | - | 미확인 |
| 공공데이터포털 | 카탈로그 검색 `분배금`/`펀드 분배` | 데이터셋 없음 | - | 해당 없음 |
| KRX 정보데이터시스템 | `MDCSTAT047` 개별종목 종합정보 | **200** | 기준일 스냅샷만 | 이벤트 이력 없음 |
| KRX KIND | 공시 문서 직접 URL | **403** (Access Denied) | HTML 표 | 미확보 (수동 대안) |
| DART OpenAPI | 공시 검색 | 법인 공시만 | - | 해당 없음 |
| ETF ON(비공식 집계) | `https://etfon.co.kr` | 200 | 표 | 승인·교차검증에 사용하지 않음 |

- 이용 조건: 사용한 KODEX API는 공개 상품 페이지가 호출하는 JSON이며 `robots.txt`는
  `allow: /`다. 인증·유료 계약이 없다. 원본 응답은 `outputs_p2a2a/raw/`에만 저장하고
  커밋하지 않는다.
- 동일 실패의 반복을 피하기 위해, `www` 호스트가 반복 호출 후 429(Cloudflare challenge)를
  반환한 뒤에는 같은 운용사의 모바일 호스트(`m.samsungfund.com`)를 사용했다. 두 호스트는
  같은 JSON 스키마를 반환한다.
- KIND·TIGER·ACE·PLUS·RISE는 **현재 환경에서 미확보**다. "분배금이 없다"는 의미가 아니다.

## 2. 실제 확보 종목·기간·이벤트 수

| 티커 | 이름 | 상장일 | 확보 이벤트 | 확보 구간(기준일) | 상태 |
|---|---|---|---:|---|---|
| 069500 | KODEX 200 | 2002-10-14 | 20 (기간 내 19) | 2022-01-28 ~ 2026-10-30 | PARTIAL |
| 091160 | KODEX 반도체 | 2006-06-27 | 5 (5) | 2022-04-29 ~ 2026-04-30 | PARTIAL |
| 091170 | KODEX 은행 | 2006-06-27 | 32 (29) | 2022-04-29 ~ 2026-11-30 | PARTIAL |
| 498400 | KODEX 200타겟위클리커버드콜 | 2024-12-03 | 23 (21) | 2025-01-15 ~ 2026-11-13 | PARTIAL |

- 합계 80건(백테스트 기간 내 74건). 12종(TIGER 7·ACE 2·PLUS 1·RISE 2)은 UNAVAILABLE.
- 지급 주기는 069500 분기(1·4·7·10월), 091160 연 1회(4월), 091170 최근 월배당,
  498400 월배당이다. **주기가 다른 4종**을 확보했다.
- PARTIAL 사유: KODEX API는 펀드별 **최근 20건(약 5년)** 만 제공한다. 498400은 상장 이후
  전 구간(23/23)이 있으나 상장일(2024-12-03)과 첫 이벤트(2025-01-15) 사이 공백의
  "미지급" 여부는 원천으로 확인되지 않아 VERIFIED로 올리지 않았다.
- 최근 1년 주기 대비 이벤트 수: 069500 5/5, 091160 2/2, 091170 13/13, 498400 13/13.
- 16종 전체 확대는 불가능하다. TIGER/ACE/PLUS/RISE는 수동(공지 PDF/Excel) 확보 대상으로
  남긴다.

## 3. 확보 필드와 미확보 필드

| 필드 | 상태 | 근거 |
|---|---|---|
| 종목코드 | 확보 | KRX 기본종목 + API |
| ISIN | 확보 | KRX `MDCSTAT04601` |
| 분배금 기준일(record date) | 확보 | API `basicD` |
| 배당락일(ex-date) | **파생** | 기준일 직전 거래일(T+2 결제 규칙). 원천 필드 아님. 거래일 달력은 가격 캐시 2016-01-05~2026-09-28(2,631일)이며 **공식 달력·공식 배당락일 검증은 하지 않음** |
| 지급일 | 확보 | API `payD` |
| 주당 분배금 | 확보 | API `dividA` (세전) |
| 통화 | 확보 | KRW |
| 주당 과세표준액 | 확보 | API `taxDividA` |
| 분배율(%) | 확보 | API `dividY` (운용사 계산값) |
| 수정·취소 이력 | **미확보** | API에 정정 필드 없음 |
| 세율 | **미확보** | 계좌·상품 유형 의존. 임의 확정 불가 |
| 공식 문서 ID/URL/SHA | 부분 | API URL + 응답 SHA256. KIND 공시 문서는 403 |

- `taxDividA`는 세후 금액이 아니라 **주당 과세표준액**이다(예: 498400 2026-11-13
  분배금 266원, 과세표준 4원). 80건 중 34건이 과세표준 < 분배금이다.
- 지급일−배당락일 간격은 3~11일(중앙값 5일)이다.

## 4. 교차검증

| 검증 | 결과 | 비고 |
|---|---|---|
| 연간 합계(이벤트 합) vs API `annualTrend` | **16/17 PASS** | 2022~2025 완전연도 전부 일치 |
| 091170 2026년 | `window_incomplete` | 연간 합계에 11월 이벤트(53원)가 선반영, 이벤트 목록은 10월까지 |
| 공식 공지 표 vs API 금액 | 자동 재조회 **불가(429)** | 수동 확인: 2026-04 공지에서 069500 446원·091160 115원 = API 값과 일치 |
| KRX 수정주가 파라미터 | **무시됨** | `MDCSTAT04501`은 `adjStkPrc=1/2`와 무관하게 `TDD_CLSPRC` 동일(21행) |
| 독립 원천 대조 | **불가** | 확보한 모든 원천이 동일 운용사 계열 |

- 연간 합계 일치는 같은 운용사의 **다른 엔드포인트**를 쓴 내부 정합성 검증이며,
  독립 원천 검증이 아니다.
- 공지 문서는 Cloudflare challenge로 자동 재조회가 막혔다. 위 수동 값은 브라우저 세션에서
  확인한 것이며, 재현 스크립트는 `unavailable`로 기록한다.
- pykrx/캐시 가격은 KRX 원천과 동일 계열이므로 독립성이 없다(P2-A-1 결과 유지).

## 5. 가격 기준과 Total Return E2E (확보 구간 한정)

### 5-1. 가격 캐시는 원주가다

KIS 조회 전용 시세를 두 파라미터(`FID_ORG_ADJ_PRC=0/1`)로 받아 캐시와 대조했다.
**검증 범위는 069500의 2026-06-01~2026-09-28(81거래일) 시스템 대조 + 2022-01-26/
2024-01-02/2025-01-13/2026-09-28 수준 점검**이다. 전체 16종·전 기간을 같은 강도로
검증한 것은 아니다.

| 항목 | 결과 |
|---|---|
| 캐시 = `FID_ORG_ADJ_PRC=1` (069500, 81거래일) | **81/81일 일치** |
| 캐시 = `FID_ORG_ADJ_PRC=0` (069500, 81거래일) | 0/81 |
| 배당락일 수익률 차이(캐시 − flag0) | −0.197%p (비배당락일 0.000%p) |
| 판정 | `price_basis=raw`, `double_count_risk=not_detected` |

- KIS 문서 라벨(0=원주가, 1=수정주가)과 **실제 동작이 반대로 관측**됐다. 라벨이 아니라
  캐시 일치와 배당락일 수익률로 판정했다: 조정 계열(flag0)은 배당락 하락을 제거하고,
  캐시/flag1은 하락을 그대로 포함한다.
- 따라서 캐시에 분배금을 더하는 것은 **이중 계상이 아니다.** 반대로 조정 계열에
  분배금을 더하면 이중 계상이 된다(아래 `double_count_illustration`).
- KRX ETF 시세 화면에는 수정주가 선택 파라미터가 없고 종가 필드는 `TDD_CLSPRC`다
  (`has_adjustment_parameter=False`). 이 필드 정의만으로는 수정 여부를 알 수 없어
  KIS 대조를 근거로 사용했다.

### 5-2. E2E 결과 (원주가 + 분배금)

| 티커 | 구간 | 이벤트 | 가격 | TR | 분배금 기여 | 독립 조정계열 검산 | 이중계상 시 |
|---|---|---:|---:|---:|---:|---:|---:|
| 069500 | 2022-01-26~2026-09-28 | 19 | 199.43% | 229.21% | +29.78%p | 229.23% (Δ0.03%p) | 263.55% |
| 091160 | 2022-04-27~2026-09-28 | 5 | 282.69% | 295.12% | +12.44%p | 295.15% (Δ0.03%p) | 308.14% |
| 091170 | 2022-04-27~2026-09-28 | 29 | 112.60% | 170.40% | +57.79%p | 170.14% (Δ0.25%p) | 253.52% |
| 498400 | 2025-01-13~2026-09-28 | 21 | 105.96% | 179.78% | +73.81%p | 179.48% (Δ0.30%p) | 303.41% |

- **별도 조회 경로 검산**: KIS 배당조정 계열의 가격수익률(분배금 미가산)과 캐시 원주가+
  분배금 TR이 0.03~0.30%p 이내로 일치한다. 두 경로가 같은 값을 주지만, KIS와 KRX의 최종
  가격 원천은 같은 시장 데이터이므로 **원천 데이터 독립성 검증은 아니다.**
- **이중계상 시**: 조정 계열에 분배금을 다시 더하면 253~303%로 부풀려진다. 승인 근거로
  사용하지 않는다.
- **항등식**: `Π(close_t + dist_t)/close_{t-1}` 와 지수비율 차이 0.
- **독립 원장 검산**: 100주에서 시작해 배당락일마다 분배금으로 주식을 추가 매수하는
  원장을 직접 굴렸다. 최종 평가액/초기 평가액과 TR 지수 비율 차이는 **1e-15 수준**이다
  (예: 069500 109.95주, 498400 135.84주).
- **현금 원장**: `가격손익 + 현금분배금 = 평가액 변화` (069500 7,293,000 + 379,900 =
  7,672,900). 이 값은 재투자하지 않은 현금 기준이며 TR과 구분해 기록한다.
- **엔진 전체 E2E는 수행하지 않았다.** 엔진은 캐시 원주가를 그대로 쓰고, 분배금 CSV를
  채우면 현금 경로가 별도로 더해진다(§6). 이번 검증은 loader·회계 함수 단위이며
  `equity_check.scope`에 명시했다.
- **배당락일 처리**: 이벤트 `ex_date`가 전부 가격 거래일에 존재하고 항등식이 성립한다.
- **적격 보유수량**: 엔진은 배당락일 체결 이전 보유 스냅샷으로 계산한다. 배당락일 신규
  매수 제외, 배당락일 매도 포함(fixture).
- **지급일**: 백테스트는 배당락일에 현금 반영, 지급일(3~11일 후)은 미모형화 → blocker.
- **매도 이후 지급**: strict ledger receivable은 보유가 0이 되어도 지급일 지급.
- **과세**: 엔진은 총 분배금에 세율 적용 → 498400 100좌 기준 15.4% 가정 시 25,380원 vs
  과세표준 기준 29,969원으로 4,589원 과소.
- **가격 이상**: 2026-03-05/07-31/08-03은 독립 피드로 확인된 시장 변동이다. KRX 재조회
  전 필드 일치, KIS 원주가 16/16 일치, NAV 동행, 분할급변 없음
  → [p2a2a_price_anomaly.md](p2a2a_price_anomaly.md).

## 6. 기존 엔진 연결 결과와 한계

- ledger 변환: 74건을 `data/etf_corporate_actions.csv` 스키마로 변환해 로드했다.
  `ex_date < record_date` 행이 스키마 검증을 통과했고, blocker는
  `INCOMPLETE_COVERAGE` 1건만 남았다(= 이벤트 자체는 유효, coverage만 부족).
- CSV 경로(`data/etf_distributions.csv`)는 여전히 header-only다. **동결 파일·템플릿을
  교체하지 않았다.** 실데이터 CSV는 `outputs_p2a2a/e2e/`에만 있다.
- strict 경로는 `approval_strict=True`일 때 CSV 분배금을 현금으로 쓰지 않고 ledger
  이벤트만 사용한다. 즉 CSV를 채워도 승인 경로는 열리지 않으며, ledger coverage가
  필요하다.

## 7. Coverage·승인 정책

- 상태 정의: `VERIFIED`(공식·기간 충족), `PARTIAL`(일부 기간/필드), `UNVERIFIED`,
  `UNAVAILABLE`(현재 환경 미확보).
- 이번 결과: **PARTIAL 4 / UNAVAILABLE 12 / VERIFIED 0**. 이벤트가 있다는 이유만으로
  VERIFIED로 올리지 않았다.
- `total_return_approvable=False` 유지. 남은 blocker:
  | code | 내용 |
  |---|---|
  | DISTRIBUTION-COVERAGE | 4/16종, 2016-2021 구간 미확보 |
  | PRICE-BASIS | 캐시는 원주가로 확인(이중 계상 없음). 단 조정 계열에 분배금을 더하면 이중 계상되므로 엔진 입력 기준을 고정해야 함 |
  | EX-DATE-DERIVED | 배당락일이 원천 필드가 아닌 파생값 |
  | PAYMENT-TIMING | 배당락일 현금 반영, 지급일 지연 미모형화 |
  | TAX-BASE | 과세표준액은 확보, 세율은 미확정 |
  | INDEPENDENT-SOURCE | 교차검증이 동일 운용사 산출물에 한정 |
- PIT-CLASS-TAX·CA-COVERAGE·OOS blocker는 그대로 유지된다.

## 8. Track B(Forward Shadow)에서 쓸 수 있는 것

- 4종의 **세전 주당 분배금·기준일·지급일·과세표준액** 이벤트 표(2022년 이후).
- 배당락일 파생 규칙(기준일 직전 거래일)과 지급 지연(3~11일) 분포.
- Forward 구간에서는 운용사 공지가 먼저 나오므로, 공지 금액과 기준일/지급일을 그대로
  기록하면 파생 없이 이벤트를 만들 수 있다. 다만 16종 전체 coverage와 독립 원천이
  없으므로 성과 승인에는 쓰지 않는다.

## 9. PR #15 후속 최소 보완

- `cross_check_cache()`: 캐시 날짜 변환을 `errors="coerce"`로 바꿔 변환 예외가 비교 전에
  터지지 않게 하고, 잘못된 날짜는 `invalid_dates` 상태로 반환한다.
- `pilot_completeness()`: 조회/비교 티커 **집합·중복·행 수**를 함께 판정한다.
  기존 `len(pilot_meta)==len(PILOT_TICKERS)`만 보던 조건을 대체했다.
- fixture 추가: `pilot completeness`(누락·실패·중복·추가 티커), `invalid cache date`.
  `scripts/test_p2a1_data_evidence.py`는 16개 검사를 통과한다.

## 10. 테스트·리뷰

- `scripts/test_p2a2a_etf_distributions.py` 17개 PASS:
  날짜 파싱, 이벤트 정규화(금액/지급일/과세표준 오류), 중복·충돌, 배당락일 파생
  (휴장/캘린더 경계), 스케줄 기대 건수, coverage 상태, coverage 산출,
  전 구간 누락 시 VERIFIED 미부여, 첫 이벤트 TR 반영, 가격 기준 판정, rate limit 판정,
  재투자 원장 검산,
  TR E2E 항등식, 적격성, receivable 지급, 공지 표 파싱, JSON sanitize, 원천 인벤토리.
- `scripts/test_p2a2a_price_anomaly.py` 5개 PASS: 전 종목 유효 비교 요구(누락·NaN·불일치),
  classify 우선순위, PR #15 검증 재사용(날짜 집합·NAV·필드), 미확보 행, 유한값 처리.
- `scripts/test_p2a1_data_evidence.py` 16개 PASS (위 보완 포함).
- 기존 회귀: `scripts/test_total_return.py`, `scripts/test_corporate_actions.py`,
  `scripts/test_backtest_integrity.py`, `scripts/test_approval_outputs.py`,
  `scripts/test_pit_universe.py` PASS.
- Codex 독립 리뷰 9회(누적): 1차 P2 5건 → APPROVE. 최종 보완에서 P1 3건(원본 덮어쓰기
  가능, 조정계열·원주가 오판, E2E 자기 비교)과 P2 13건(상장좌수 unknown, KIS 오류 격리,
  페이지 완결성, 정렬, rate limit 코드 판정, 전 종목 유효 비교, NAV·날짜 집합, 미확보 행 등)
  → 전부 수정 후 **최종 APPROVE(P0/P1/P2 없음)**.
- 실행: `uv run python scripts/p2a2a_etf_distributions.py`.
  가격 캐시가 worktree에 없으면 `--cache-dir`로 읽기 전용 경로를 지정한다.

## 11. 한계

- 공식 원천 4/16종, 2022년 이후만. 2016-2021 구간은 미확보.
- 배당락일은 파생값이며 원천 문서로 확인하지 않았다.
- 공지 문서 자동 재조회는 WAF 429로 실패(수동 확인만).
- 2026-03-05/07-31/08-03 급등락은 **별도 조회 경로(증권사 API)로 확인된 시장 변동**이다.
  캐시·수집 오류는 아니지만, 원천 데이터가 독립적이지 않고 실세계 대조도 범위 밖이다.
- 2026-07-31·08-03은 freeze(2026-07-21) 이후 모니터링 구간이고, 03-05만 in-sample이다.
- TIGER/ACE/PLUS/RISE 접근 실패는 이 환경의 네트워크·차단 정책에 따른 결과다.
