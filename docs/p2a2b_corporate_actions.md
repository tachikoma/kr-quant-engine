# P2-A-2b: 동결 ETF 기업행위 실데이터 확보와 ledger 검증

- 실행: `scripts/p2a2b_corporate_actions.py` (읽기 전용 조회 + 산출물 생성)
- fixture: `scripts/test_p2a2b_corporate_actions.py`
- 산출물: `outputs_p2a2b/` (gitignored)
- 대상: 동결 16종, 2016-01-05~2026-09-28
- 원칙: 확인되지 않은 부재를 "이벤트 없음"으로 승격하지 않는다. 조회 실패와 공식 부재를
  구분한다. 기존 ledger template·동결 파일·운영 경로는 변경하지 않는다.

## 1. 원천별 접근 결과

| 원천 | 방법 | 결과 | 구조화 | 커버 | 판정 |
|---|---|---|---|---|---|
| KRX KIND | 공시 문서/검색 | **403** (Access Denied) | HTML | 분할·합병·정지·폐지·명칭 | 수동만 |
| 삼성 KODEX | 상품/공지 | API 200, 공지 HTML 429 | JSON/HTML | 분배금 | 분배금만 |
| 미래에셋 TIGER | `tigeretf.com`, `investments.miraeasset.com` | **403** | - | 공지·문서 | 불가 |
| 한국투자 ACE | 상품 페이지/API | 200 / API 타임아웃 | - | 공지·문서 | 불가 |
| 한화 PLUS | 웹 | **TCP 실패** | - | 공지·문서 | 불가 |
| KB RISE | 웹 | **TCP 실패** | - | 공지·문서 | 불가 |
| 예탁결제원 SEIBro | 웹 | WebSquare 셸 | - | 권리일정 | 미확인 |
| DART OpenAPI | 펀드공시(G) | **200** | 메타데이터 | 펀드 명칭·신고서 | 부분 |
| KRX 정보데이터시스템 | JSON(로그인) | **200** | 스냅샷 | 종목 기본정보·ISIN·상장상태 | 메타데이터만 |
| KIS 조회 전용 | API | **200** | 시세 | 가격 연속성(보조) | 보조 |

- DART 펀드공시는 운용사별 12,312~32,072건이다. 2016-2026 전체를 나열하는 것은 범위 밖으로
  두고, 명칭 변경이 의심되는 좁은 구간만 표본 확인했다.
- KIND·TIGER·PLUS·RISE·ACE 접근 실패는 **조회 실패**이지 "기업행위 없음"이 아니다.

## 2. 확보 이벤트

| 구분 | 건수 | 종목 | 근거 |
|---|---:|---|---|
| CASH_DISTRIBUTION | **74** | 069500, 091160, 091170, 498400 | KODEX 공식 API + 공지(2-A-2a) |
| SPLIT / REVERSE_SPLIT | 0 | - | 공식 공시 미확보 |
| SUSPENSION_START/END | 0 | - | KIND 403 |
| DELISTING | 0 | - | 16종 모두 현재 상장 |
| CASH_SETTLEMENT / REDEMPTION | 0 | - | 해당 없음/미확보 |

- 분배금 74건은 `outputs_p2a2b/corporate_action_ledger.csv`로 재구성했다. 금액은 세전,
  지급일은 원천 필드, `ex_date`는 **기준일 직전 거래일 파생값**이며 ledger note와 manifest에
  공식 확정값이 아님을 명시했다.
- 비분배 이벤트는 공식 근거를 확보하지 못했다. **"없음"이 아니라 "미확인"**이다.
- 참고: 국내 ETF는 액면분할·병합이 제도적으로 제한적이라는 보도가 다수이나, 이는 공식
  부재 확인 문서가 아니므로 추정 근거로만 둔다.

### 명칭 변경 관측(비승격)

| 티커 | 관측 명칭 | 관측일 | DART 접수번호 | 상태 |
|---|---|---|---|---|
| 161510 | 한화PLUS고배당주증권상장지수투자신탁(주식) | 2023-01-12 | 20230112000376 | UNVERIFIED |
| 367760 | KBRISE네트워크인프라증권상장지수투자신탁(주식) | 2024-01-17 | 20240117000280 | UNVERIFIED |

- DART 펀드공시에서 새 브랜드(PLUS/RISE) 사용은 확인되나, **명칭 변경 공지·효력일**은
  확인되지 않았다. ledger에 `NAME_CHANGE` 유형이 없어 강제 매핑하지 않고
  `corporate_action_events.csv`에 `ledger_mappable=false`로 분리했다.
- ISIN 연속성은 현재 KRX ISIN만 확인했고 변경 전후 문서가 없어 **미검증**이다.

## 3. Ledger·manifest 검증

- 74건 로드: `ex_date < record_date`, 지급일·금액·출처 문서 일치, 중복 ID 없음.
- manifest: `coverage_status=PARTIAL`, 검증 기간 2022-01-27~2026-09-17, 4종, 문서 4개
  (KODEX API URL + 응답 SHA256). canonical ledger hash를 loader 정규화 결과로 고정했다.
- loader approval report: **BLOCKED**, blocker는 `INCOMPLETE_COVERAGE` 1건.
- strict preflight 실제 실행(`--approval-strict`, 전용 출력 디렉터리): **BLOCKED**,
  `INCOMPLETE_COVERAGE` 1건. 이벤트 자체는 유효하고 **coverage만 부족**하다.
- 기존 `data/etf_corporate_actions.csv` template은 header-only 그대로 두었다.

## 4. 실데이터 E2E

확보된 유형(분배금)에만 수행했다. **생성된 ledger 이벤트를 다시 로드해** 금액·지급일·
적격성·매도 후 지급을 검증하며, 기대값과 다르면 `blocked`로 실패한다. 합성 fixture와 분리한다.

| 티커 | 기준일 | 100좌 수취액 | 지급일 전 | 지급일 | 매도 후 지급 | 지급 지연 |
|---|---|---:|---:|---:|---|---:|
| 069500 | 2026-07-31 | 18,300원 | 0 | 18,300원 | 유지 | 5일 |
| 091160 | 2026-04-30 | 11,500원 | 0 | 11,500원 | 유지 | 7일 |
| 091170 | 2026-08-31 | 5,900원 | 0 | 5,900원 | 유지 | 5일 |
| 498400 | 2026-09-15 | 30,000원 | 0 | 30,000원 | 유지 | 3일 |

- 적격성: 배당락일(파생) 이전 보유 스냅샷 기준. 배당락일 매수 제외·매도 포함.
- 지급일 전에는 현금 0, 지급일 당일에 전액 반영. 보유가 0이 되어도 receivable은 지급된다.
- **분할·거래정지·상장폐지·정산 실데이터 E2E는 수행하지 않았다.** 해당 실제 이벤트가
  없기 때문이며, 합성 fixture(`test_corporate_actions.py`)의 동작을 실데이터 검증으로
  대체하지 않는다.

## 5. 가격 이상 조사 연계

- 2026-03-05 / 07-31 / 08-03에 대해 KIND 공시를 확인하려 했으나 **403**으로 불가.
- 가격 연속성 스캔: 16종 중 2026-07-31 시장 전체 급등 외에 ±25% 이상 단일일 변동이 없다.
- 공식 기업행위 근거가 없으므로 **P2-A-2a의 가격 이상 판정(시장 변동, 캐시 오류 아님)을
  변경하지 않는다.** 캐시도 수정하지 않았다.

## 6. Coverage

| 이벤트 유형 | 상태 | 종목 수 | 사유 |
|---|---|---:|---|
| CASH_DISTRIBUTION | PARTIAL / UNAVAILABLE | 4 / 12 | KODEX 4종 2022년 이후 / 나머지 원천 미확보 |
| SPLIT, REVERSE_SPLIT | UNVERIFIED | 16 / 16 | 가격 단절 없음(보조)이나 공식 부재 문서 없음 |
| DELISTING | PARTIAL | 16 | 현재 상장 상태만 확인 |
| SUSPENSION_START/END | UNAVAILABLE | 16 / 16 | KIND 403 |
| CASH_SETTLEMENT, REDEMPTION | UNVERIFIED | 16 / 16 | 공식 정산·해지 문서 미확보 |

합계: PARTIAL 20, UNVERIFIED 64, UNAVAILABLE 44, VERIFIED 0. `INCOMPLETE_COVERAGE` 유지.

## 7. Track B 활용 데이터

- 공식 이벤트 공지 시점: **미확보**. `retrieved_at`은 API 응답 수집 시각이며 공식 게시
  시각이 아니다. Forward에서는 공지 원문의 게시일을 별도 필드로 기록해야 한다.
- 적용 거래일: 분배금은 `record_date`(공식) + `ex_date`(파생), 그 외 이벤트는 `event_date`.
- 거래정지·재개 상태: ledger `SUSPENSION_START/END` + lifecycle(ACTIVE/SUSPENDED).
- 분할·병합 비율: `ratio_num/ratio_den` (실데이터 미확보).
- 분배금 권리 발생·지급: `record_date`/`ex_date`/`payment_date` + receivable.
- 정산 대기: `DELISTED_UNSETTLED` → `CASH_SETTLEMENT` → `SETTLED`.
- 중복 방지 키: 분배금 `(ticker, record_date)`, 그 외 `(ticker, event_type, event_date)`.
- 기존 ledger 스키마를 공통 사용할 수 있다. 단 `NAME_CHANGE`는 미지원이므로 Forward에서도
  별도 evidence로 두고 ledger에 넣지 않는다.

## 8. 후속 엔진 정확성 개선

1. `ex_date` 파생값과 공식 확정값을 스키마에서 구분(현재는 동일 필드).
2. `NAME_CHANGE`/티커 변경 이벤트 유형 또는 별도 참조 테이블.
3. 지급일 현금 반영(현재 백테스트는 배당락일 반영).
4. 과세표준 기반 세금 계산(현재 총액 기준).
5. 가격 기준(원주가/조정) 고정 및 입력 검증.

## 9. 테스트·리뷰

- `scripts/test_p2a2b_corporate_actions.py` 6개 PASS: ledger 매핑, coverage 상태,
  가격 연속성(분할 후보 탐지), **분할 후보가 이벤트/VERIFIED로 승격되지 않음**,
  명칭 관측 비승격, 원천 인벤토리.
- 회귀 PASS: corporate_actions, approval_outputs, backtest_integrity, total_return,
  pit_universe, p2a2a fixtures 2종, p2a1 fixture.
- Codex 독립 리뷰 3회: P2 4건(ledger 경유 E2E, 분할 오확정 fixture, 수집·공지 시각 구분,
  지급일 당일 검증) → 전부 수정 후 **최종 APPROVE(P0/P1/P2 없음)**.
- 실행: `uv run python scripts/p2a2b_corporate_actions.py`
  (`--p2a2a-dir`로 P2-A-2a 산출물 경로 지정, `--skip-preflight`로 게이트 생략 가능).

## 10. 한계

- 비분배 이벤트 공식 원천 미확보. KIND/운용사 차단은 환경 의존이며, 수동 다운로드 대안은
  브라우저 세션에서 KIND 공시를 직접 받아 정규화하는 것이다.
- 분배금은 4/16종, 2022년 이후만.
- 명칭 변경은 관측만 있고 효력일이 없다.
- 실데이터 E2E는 분배금 유형에 한정된다.
