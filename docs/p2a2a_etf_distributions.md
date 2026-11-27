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
| 배당락일(ex-date) | **파생** | 기준일 직전 거래일(T+2 결제 규칙). 원천 필드 아님 |
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

## 5. Total Return E2E (확보 구간 한정)

가격 캐시(비수정 종가) + 위 이벤트를 기존 `load_distributions` →
`add_distributions` → `add_total_return_price` 경로에 연결했다. E2E 구간은
**첫 확인 이벤트 배당락일의 전일 ~ 2026-09-28**로 제한했다(그 이전은 분배금이 없어
가격수익률과 동일해지므로 전체 기간 성과로 해석하면 안 된다). 첫 배당락일 전일 종가를
포함해 첫 분배금도 TR 지수에 반영된다.

| 티커 | E2E 구간 | 이벤트 | 가격수익률 | Total Return | 분배금 기여 | TR 항등식 오차 |
|---|---|---:|---:|---:|---:|---:|
| 069500 | 2022-01-26~2026-09-28 | 19 | 199.43% | 229.21% | +29.78%p | 0.0 |
| 091160 | 2022-04-27~2026-09-28 | 5 | 282.69% | 295.12% | +12.44%p | 0.0 |
| 091170 | 2022-04-27~2026-09-28 | 29 | 112.60% | 170.40% | +57.79%p | 0.0 |
| 498400 | 2025-01-13~2026-09-28 | 21 | 105.96% | 179.78% | +73.81%p | 0.0 |

- **항등식**: `Π(close_t + dist_t)/close_{t-1}` 와 지수비율 차이 0. 합성 fixture도 동일.
- **평가액 검산**(100좌): `가격손익 + 현금분배금 = 평가액 변화` (오차 0).
  예) 069500 가격손익 7,293,000원 + 분배금 379,900원 = 7,672,900원.
- **배당락일 처리**: 이벤트 `ex_date`가 전부 가격 거래일에 존재하며, 항등식이 성립한다.
- **적격 보유수량**: 엔진은 배당락일 체결 **이전** 보유 스냅샷으로 분배금을 계산한다.
  따라서 배당락일 신규 매수는 제외되고, 배당락일 매도는 포함된다(코드 경로 확인 +
  fixture). 이는 T+2 기준일 권리 규칙과 일치한다.
- **지급일 처리**: 백테스트는 배당락일에 현금을 반영하고 지급일(3~11일 후)을 기다리지
  않는다. 총수익은 같지만 현금 타이밍이 다르다 → blocker로 기록.
- **매도 이후 지급**: strict ledger 경로에서 receivable은 보유가 0이 되어도 지급일에
  지급된다(검증: `post_sale_still_payable=True`).
- **과세**: 엔진의 `ETF_DISTRIBUTION_TAX_PCT`는 **총 분배금**에 적용된다. 실제로는
  과세표준액에 과세되므로, 15.4% 가정 시 498400 100좌 기준 25,380원(총액 기준) vs
  29,969원(과세표준 기준)으로 **4,589원 과소 계상**된다. 세율 자체는 확정하지 않는다.
- **수정주가 중복 반영 방지**: KRX ETF 시세는 `adjStkPrc`를 무시하고 `TDD_CLSPRC`를
  반환하므로 분배금 가산이 이중 계상되지 않는다. 단 아래 가격 이상에 주의한다.

### 가격 데이터 이상

- 2026-07-31에 동결 다수 종목이 +17.9~30.0% 급등 후 08-03에 되돌림
  (0101N0·069500·091160·102110·367760·472150·498400, 지수 포함). 시장 전체에서 동시에
  발생해 **가격 캐시 이상**으로 판단하며, 해당 종목 E2E 수치는 이 왜곡을 포함한다.
- 091160은 2026-03-05에도 +18.5%가 있다.
- `pilot_validation.json`의 `e2e.<ticker>.total_return.anomaly_dates`에 기록했다.
  분할/병합 이벤트는 ledger에 없으며, 이 이상을 분배금으로 설명하지 않는다.

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

- `scripts/test_p2a2a_etf_distributions.py` 14개 PASS:
  날짜 파싱, 이벤트 정규화(금액/지급일/과세표준 오류), 중복·충돌, 배당락일 파생
  (휴장/캘린더 경계), 스케줄 기대 건수, coverage 상태, coverage 산출,
  전 구간 누락 시 VERIFIED 미부여, 첫 이벤트 TR 반영,
  TR E2E 항등식, 적격성, receivable 지급, 공지 표 파싱, JSON sanitize, 원천 인벤토리.
- `scripts/test_p2a1_data_evidence.py` 16개 PASS (위 보완 포함).
- 기존 회귀: `scripts/test_total_return.py`, `scripts/test_corporate_actions.py`,
  `scripts/test_backtest_integrity.py`, `scripts/test_approval_outputs.py`,
  `scripts/test_pit_universe.py` PASS.
- Codex 독립 리뷰 2회: 1차 P2 5건(첫 분배금 TR 누락, 전 구간 누락인데 VERIFIED,
  무효 지급일·과세표준 통과, 과세표준만 다른 중복, 원본 SHA 재직렬화) → 전부 수정 후
  **최종 APPROVE(P0/P1/P2 없음)**.
- 실행: `uv run python scripts/p2a2a_etf_distributions.py`.
  가격 캐시가 worktree에 없으면 `--cache-dir`로 읽기 전용 경로를 지정한다.

## 11. 한계

- 공식 원천 4/16종, 2022년 이후만. 2016-2021 구간은 미확보.
- 배당락일은 파생값이며 원천 문서로 확인하지 않았다.
- 공지 문서 자동 재조회는 WAF 429로 실패(수동 확인만).
- 가격 캐시의 2026-07-31 시장 전체 급등은 원인 미확인 데이터 이상이다.
- TIGER/ACE/PLUS/RISE 접근 실패는 이 환경의 네트워크·차단 정책에 따른 결과다.
