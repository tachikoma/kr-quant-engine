# P2-A-1: 역사 데이터 신뢰성 및 승인 Blocker 점검

- 실행: `scripts/p2a1_data_evidence.py` (읽기 전용 조회·검증, 소량 표본)
- 산출물: `outputs_p2a1/` (`implementation_inventory.csv`, `approval_blockers.csv`,
  `data_source_inventory.csv`, `coverage_matrix.csv`, `cross_source_comparison.csv`,
  `pilot_validation.json`, `pilot/`, `approval_preflight/`, `provenance.json`)
- 대상: 동결 16종, 2016-01-05~2026-09-28, 초기자본 100만원
- 원칙: 데이터 미확보를 "이벤트 없음"으로 간주하지 않음. blocker를 우회하지 않음.
  신규 수집기·유료 계약·전략 최적화 없음.

## 1. 기존 구현 재사용 범위와 상태

| 구성요소 | 구현 | 상태 | 근거 |
|---|---|---|---|
| PIT membership 스냅샷/빌더 | `build_point_in_time_universe.py`, `pit_universe.py` | 구현 완료, **캐시 미포함** | 문서상 124 snapshots/74,129행/hash. 캐시는 gitignored |
| PIT 백테스트 연결 | `add_pit_membership_flag`, `build_pit_ticker_groups` | 구현·fixture 검증 완료 | `test_pit_universe.py` |
| PIT historical classification/tax | `restore_pit_classification.py`(추정) | **부분 구현** | 227종목 키워드 추정(96.1/91.6%), effective-dated 원천 없음 |
| 기업행위 ledger/manifest | `etf_corporate_actions.py`, `data/*` | 구현 완료, **실데이터 없음** | template 0 events, `coverage_status=INCOMPLETE` |
| strict approval preflight | `run_etf_backtest.py --approval-strict` | 구현·실행 검증 완료(차단) | `approval_report.json status=BLOCKED` |
| 분배금/Total Return | `etf_distributions.py`, `data/etf_distributions.csv` | 구현 완료, **실데이터 없음** | CSV header-only, loader 0 rows |
| 캐시 digest/provenance | `p0b.data_cache_digest`, freeze/manifest | **부분 구현** | 가격/freeze hash는 검증, 원천별 document/hash/coverage는 미완 |

> `implementation_inventory.csv`는 문서·코드 **수동 검토** 결과이고, `coverage_matrix.csv`의
> KRX 멤버십/가격 행은 이번 조회 결과(`snapshot_status`, `price_status`)에서 파생했습니다.
> 나머지 coverage 행은 수동 판정입니다.

재사용한 기존 검증 경로: `load_corporate_action_ledger()`의 blocker 판정, `--approval-strict`
실행, `etf_distributions.load_distributions()`, 가격 캐시와 `data_cache_digest`.

## 2. 현재 미해결 승인 blocker

| blocker | 내용 | 상태 |
|---|---|---|
| PIT-CLASS-TAX | historical classification/tax effective-dated 원천 없음 → strict PIT fail-closed | open |
| CA-COVERAGE | 공식 기업행위 coverage/manifest 없음. `EMPTY_LEDGER`, `INCOMPLETE_COVERAGE`(기간·티커·문서 0) | open |
| DISTRIBUTION | 분배금 이력 0건. total return 승인 근거 사용 불가 | open |
| INDEPENDENT-SOURCE | KRX 외 독립 구조화 원천 미확보(KIND 403, SEIBro JS, 운용사 차단/미확인) | open |
| OOS | 46거래일 freeze 모니터링은 독립 OOS가 아님 | open |

`approval_preflight` 실행 결과: `status=BLOCKED`, `event_count=0`,
blocker codes `EMPTY_LEDGER`, `INCOMPLETE_COVERAGE`(4건). 즉 기존 가드가 정상 동작합니다.

## 3. PIT 데이터 확보 가능성(실제 조회)

KRX(pykrx 1.2.8)로 과거 거래일 전종목 시세를 실제 조회했습니다.

| 조회일 | `get_etf_ticker_list` | `get_etf_ohlcv_by_ticker` | NAV | 거래대금 |
|---|---:|---:|---|---|
| 2016-08-01 | 174 | **220** | 제공 | 제공 |
| 2020-01-02 | 346 | 450 | 제공 | 제공 |
| 2024-01-02 | 701 | 812 | 제공 | 제공 |
| 2026-07-21 | 1,135 | **1,150** | 제공 | 제공 |

- 문서의 PIT 첫 스냅샷(220종목)·마지막(1,150종목)과 **OHLCV 행 수는 일치**하지만,
  **행 수 일치와 ticker 집합 일치는 별개**입니다.
- 스냅샷 캐시(`data_cache/pit_universe/`)가 이 환경에 없어 ticker 집합 비교를 수행하지 못했습니다.
  따라서 membership 재현성은 **부분 검증(행 수 기준)**으로 기록합니다.
  스냅샷이 있으면 각 조회일 as-of ticker 집합과 KRX OHLCV ticker 집합을 비교하도록 구현했습니다
  (`compare_membership`).
- `ticker_list`(174/1,135)와 OHLCV 행(220/1,150)의 종목 수 차이는 **원인 미확인**으로 남깁니다
  (거래 여부·API 응답 범위 차이로 추정되나 검증하지 않았습니다).
- 즉 **거래일별 OHLCV·NAV·거래대금은 공식 원천에서 조회 가능**하나, membership 집합 재현성은
  부분 검증입니다.
- 반면 **과거 시점별 자산군/시장 분류와 과세 유형**은 KRX 현재 분류/키워드 추정 외에
  effective-dated 원천이 없습니다. 상장일/폐지일도 공식 필드가 아닌 캐시 관측일 추정입니다.
- 거래정지/재개 이력은 pykrx ETF API에 없고 KIND가 차단되어 **미확인**입니다.
- 결론: membership·가격은 승인 경로에 사용 가능하나, classification/tax/상장폐지 provenance가
  없으므로 **PIT strict 승인 blocker는 유지**합니다.

## 4. 기업행위 데이터 확보 가능성

- `data/etf_corporate_actions.csv`는 header-only(0 events), manifest는 `coverage_status=INCOMPLETE`,
  검증 기간/티커/문서 모두 비어 있습니다.
- KRX KIND 공시 검색은 이 환경에서 **HTTP 403(Access Denied)** 으로 자동 조회 불가.
- DART OpenAPI는 정상 동작(corpCode ZIP 3,639,788 bytes, 최근 3개월 공시 5페이지·500건 조회)했지만,
  보고서명에 "분배"가 포함된 공시는 0건이었고 ETF 분할/폐지 이벤트를 정형 필드로 제공하지 않습니다.
  → 공시 원문을 사람이 정규화해야 하며, 자동 수집기 범위 밖입니다.
- 운용사 사이트는 HANARO 200(HTML), 삼성 302→403(리다이렉트 후 차단), TIGER 403,
  KBSTAR/KOSEF 연결 실패, ARIRANG 접속 실패로 **구조화 이력 확보 실패**입니다.
- 결론: 기업행위 원천 미확보. "이벤트 없음"이 아니라 **조회 불가/미확인**이며 blocker 유지.

## 5. 분배금·Total Return 확보 가능성

- `data/etf_distributions.csv`는 header-only(0 rows, SHA256 `bba9bc40…`).
- pykrx에는 ETF 분배금 API가 없고(주식 배당수익률만 제공), KRX 데이터 포털의 분배금 화면은
  웹 UI이며 자동 조회 bld/응답을 확인하지 못했습니다.
- SEIBro는 200 응답이지만 956 bytes JS 셸로 구조화 데이터 미확인, 운용사 페이지는 위와 같습니다.
- DART는 공시 검색만 가능하고 분배금 기준일/배당락/지급일/주당/수정·취소/세금을 정형 제공하지 않습니다.
- 결론: 분배금 이력 미확보 → **total return 성과를 승인 근거로 사용하지 않습니다.** 또한
  수정주가에 이미 반영된 분배금을 중복 계산하지 않는 기존 원칙을 유지합니다(데이터 없음).

## 6. 독립 원천 및 교차검증

| 원천 | 접근 | 판정 |
|---|---|---|
| KRX 정보데이터시스템(pykrx 경유) | **verified** | 공식, 가격/멤버십 원천 |
| KRX KIND | 403 차단 | 기업행위 자동 조회 불가 |
| 예탁결제원 SEIBro | JS 셸(200) | 구조화 API 미확인 |
| DART OpenAPI | verified(공시) | 분배금 정형 데이터 없음 |
| 운용사(HANARO/KODEX/TIGER/KBSTAR/KOSEF/ARIRANG) | 200/302/403/실패 혼재 | 구조화 이력 미확보 |
| 증권사 API(KIS/Kiwoom/NH) | 코드만 존재, 미호출 | 시세 원천이 KRX와 동일 계열, 기업행위/분배금 없음 |
| pykrx vs 기존 캐시 | 동일 원천 | **독립 원천 아님** |

교차검증 결과: 파일럿 5종의 KRX 시가·고가·저가·종가·거래량·거래대금을 기존 캐시와 대조했습니다.
- 날짜 중복·NaT·Inf/NaN 검증 통과, **비교 구간 날짜 집합 일치**, 예상/겹침 행 20/20×4·18/18×1,
  **모든 필드 최대 차이 0.0**, 5/5 티커 조회·비교 완료.
- 단, 이는 **조회된 행의 일치**이며 공식 거래일 calendar 전체를 검증한 것은 아닙니다
  (`official_calendar_verified=false`). 이는 동일 원천 내부 일치성이며,
독립 원천 교차검증은 **불가**합니다.

## 7. 소규모 E2E 검증(파일럿)

선정(이유 포함): `069500`(국내 대표·분배), `360750`(해외), `411060`(원자재/금),
`161510`(배당주), `498400`(커버드콜·고빈도 분배).

| 경로 | 결과 | 내용 |
|---|---|---|
| 공식 조회→정규화→provenance→캐시 대조 | **검증 완료(동일 원천)** | 2023-01(4종)·2025-01(1종) 20/20·18/18거래일, SHA256·조회시점·pykrx 버전 기록 |
| 기업행위 ledger 변환→preflight | **차단 확인** | `BLOCKED`, `EMPTY_LEDGER`/`INCOMPLETE_COVERAGE` — 공식 이벤트 원천 없음 |
| 분배금→total return | **미검증** | CSV 0 rows, 원천 미확보 |
| 독립 원천 대조 | **불가** | 구조화 원천 미확보 |

파일럿 정규화 파일은 `outputs_p2a1/pilot/`에 두었고(gitignored), 원본 API 응답·인증정보는
커밋하지 않았습니다.

## 8. 확보 가능·불가능·미확인 구분

- **확보 가능(검증, 동일 원천)**: 거래일별 ETF 목록, OHLCV, NAV, 거래대금, 종목명/ISIN.
  독립 원천 교차검증은 불가하므로 승인용 독립 evidence로는 사용하지 않습니다.
- **부분 확보**: 상장/폐지일(관측일 기준), PIT membership(결정일 스냅샷).
- **현재 환경에서 미확보/미확인**: historical classification/tax, 거래정지/재개, 공식 기업행위 문서,
  분배금 이력, 독립 가격 원천. (영구적으로 확보 불가능하다는 의미가 아닙니다.)
- **미확인**: SEIBro·운용사 구조화 API, KRX 분배금 자동 엔드포인트.

## 9. 다음 우선순위와 예상 비용(개략)

| 우선순위 | 작업 | 예상 |
|---|---|---|
| 1 | SEIBro/운용사 분배금 원천 확보(API/약관 확인) | 2~4일 |
| 2 | 동결 16종 기업행위 공식 문서 수동 정규화 + manifest | 3~5일(문서 수에 비례) |
| 3 | effective-dated classification/tax 원천 확보·매핑 | 5~10일 + 데이터 이용 조건 |
| 4 | 분배금 CSV 확장 및 total-return 재검증 | 3~5일 |
| 5 | 독립 원천 가격 교차검증 파이프라인(소량) | 2~3일 |

비용은 데이터 접근 승인·약관에 따라 크게 달라질 수 있어 **불확실**합니다.

## 10. 테스트·리뷰

- `scripts/test_p2a1_data_evidence.py` (fixture 14/14): 정규화 SHA256 순서 독립성·값 민감성,
  env boolean만 노출, 파일럿 카테고리, 가격 경로 상태 계산(실패/부분/검증), JSON NaN→null,
  독립 원천 판정, snapshot/KRX/DART 상태 계산, 분배금 승인 가능성·stale preflight 차단,
  시크릿 리다크션, 빈 비교 CSV schema 유지, **가격 비교의 no_overlap·중복/NaT/Inf·날짜 집합 불일치·
  필드 누락·티커 누락 검출**, membership 집합 비교, provenance artifact 완전성, 산출물 schema.
- 분배금은 필드가 채워져도 **기간·티커 coverage와 공식 출처 검증 전에는 `total_return_approvable=False`**로
  고정되며, 경로 상태도 `blocked`입니다.
- E2E 상태는 상수로 두지 않고 실제 조회·대조 결과에서 계산하며, `--approval-strict`는 실행별
  고유 디렉터리에 기록하고 이번 실행에서 보고서가 생성됐는지(`report_written_this_run`)를 확인합니다.
- 산출물 디렉터리는 실행 시작 시 비워 과거 실행 파일이 provenance에 섞이지 않게 합니다.
- 기존 회귀: PIT/기업행위/distribution 관련 테스트(`test_pit_universe`, `test_corporate_actions`,
  `test_approval_outputs`, `test_backtest_integrity`) PASS.
- Codex 독립 리뷰(4차까지): P1 4건(시크릿 노출, 불완전 가격 대조, NaN 오승인, 캐시 필드 누락),
  P2 2건(분배금 승인 오판, 무효 입력/진단 유실), membership·문구 지적 → 전부 수정 후
  **최종 APPROVE(P0/P1/P2 이상 없음)**

## 11. 한계

- 소량 표본·단일 시점 조회이며 전체 기간 수집이 아닙니다.
- KIND/SEIBro/운용사 접근 실패는 네트워크·차단 정책에 따른 **환경 의존** 결과입니다.
- DART 공시 3개월 표본에서 "분배" 키워드 0건은 ETF 분배금 데이터 부재의 증명이 아닙니다.
- pykrx와 기존 캐시는 동일 원천이므로 교차검증으로 독립성을 주장하지 않습니다.
- 2026년은 부분연도, 46거래일은 모니터링입니다. PIT·기업행위·분배금 blocker는 유지됩니다.

## 재현

```bash
uv run python scripts/test_p2a1_data_evidence.py
uv run python scripts/p2a1_data_evidence.py     # 읽기 전용 소량 조회 (약 1~2분)
```
