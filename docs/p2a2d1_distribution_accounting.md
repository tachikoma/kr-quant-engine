# P2-A-2d-1: 분배금 지급일·미수금 회계 검증

- 실행: `scripts/p2a2d1_distribution_accounting.py`
- fixture: `scripts/test_p2a2d1_distribution_accounting.py`
- 산출물: `outputs_p2a2d1/` (gitignored)
- 입력: P2-A-2b ledger 74건(091170 29건, 069500 19건, 091160 5건, 498400 21건), 가격 캐시
- 원칙: 승인 경로는 BLOCKED 유지, 연구 seam은 승인 결과로 사용하지 않음. 세전 회계만 검증.

## 1. 기존 회계 경로와 발견된 결함

| 경로 | 분배금 처리 |
|---|---|
| legacy price | `add_distributions`가 배당락일에 `distribution`을 붙이고, 배당락일 종가 기준 현금을 **즉시** 반영(미수금 없음) |
| total return | 배당락일 종가에 분배금을 재투자 |
| strict ledger | 권리 발생 시 receivable, 지급일 현금 전환 |

검증 중 확인한 결함과 수정:

| # | 결함 | 영향 | 수정 |
|---|---|---|---|
| 1 | strict 인식일이 `max(event, record, ex)` = **기준일** | 배당락일 미수금 미인식 → 하루 동안 평가액 과소 | 분배금은 `ex_date`(없으면 event/record)에 인식 |
| 2 | 권리 스냅샷이 `record_date` 이상을 조회 | 배당락일 처리 시 기준일 스냅샷이 미래라 `MISSING_ENTITLEMENT_SNAPSHOT` | 배당락일 스냅샷을 권리 경계로 사용(배당락일 매수 제외·매도 포함) |
| 3 | `initial_state`에 `pending_receivables`/`processed_action_ids` 미복원 | 분할 실행 시 미수금 소실·지급 재처리 위험 | 두 상태와 `corporate_action_states`를 직렬화·복원 |
| 4 | PARTIAL ledger를 strict로 실행할 연구 경로 부재 | 회계 E2E 불가 | `allow_partial_ledger` 연구 seam 추가(승인 gate는 그대로 차단) |

## 2. 실제 변경 범위

- `run_etf_backtest.py`: `allow_partial_ledger`(연구 전용), 분배금 effective date, 배당락일 권리 스냅샷, restart 상태 복원.
- `scripts/p2a2b_corporate_actions.py`: PR #19 후속 — 금액 NaN/Inf 거부, `ex_date < record_date` 엄격 검증, preflight 보고서 필수 필드·상태 일관성(`APPROVED`↔`approval_valid`) 검사, preflight 출력 루트 인자.
- 신규: `scripts/p2a2d1_distribution_accounting.py`, fixture, 본 문서.
- 기본(비승인) 경로는 변경하지 않았다. legacy 기본 실행 재현 결과는 §6.

## 3. 실데이터 E2E 시나리오

실제 이벤트(091170: 2026-06-29/07-30/08-28, 498400: 2026-09-14)와 실제 가격을 사용했다.
각 시나리오는 해당 이벤트만 담은 부분 ledger로 실행했다.

| 시나리오 | 종목 | 이벤트 | 체결 | 결과 |
|---|---|---:|---:|---|
| hold_before_ex | 091170 | 1 | 0 | 08-28 미수금 59,000원, 09-02 현금 59,000원 |
| buy_on_ex | 091170 | 1 | 1 (08-28 매수) | 배당락일 매수분 미수금 0 |
| sell_on_ex | 498400 | 1 | 1 (09-14 매도) | 매도 후에도 미수금 300,000원 유지, 09-17 지급 |
| sell_after_ex | 091170 | 1 | 1 (08-31 매도) | 09-02 지급 +59,000원 |
| receivable_not_spendable | 091170 | 1 | 0 | 현금 12,000원 < 1좌(16,745원), 미수금 13,500원이 있어도 매수 없음 |
| end_before_payment | 091170 | 1 | 0 | 종료 시 미지급 미수금 1건 보존 |
| multi_event_hold | 091170 | 3 | 0 | 3회 지급 합계 174,000원 |

핵심 체크 **14/14 PASS**: 배당락일 인식, 지급 전 현금 0, 지급일 현금, 지급일 이중 계상 없음,
배당락일 매수 제외, 매도 후 권리 유지, 지급일 전 미수금 미사용, 종료일 미수금 보존,
다중 이벤트 전액 지급.

## 4. 날짜별 현금·미수금·평가액 검산

- 7개 시나리오, **85 거래일**에 대해 독립 원장으로 계산했다.
- 독립 계산: 체결 원장으로 보유수량을 재구성하고, **초기 현금 + 체결 cash flow + 독립
  지급액**으로 현금을, **보유수량 × 종가**로 보유평가를, 배당락일 직전 보유 × 주당
  분배금으로 미수금을 계산했다. 세 구성요소를 각각 엔진 값과 비교했다.
- 결과: 평가액·현금·보유평가 **최대 절대 차이 0.0** (전 행). 미수금이 현금으로 바뀌는
  지급일에도 추가 수익이 계상되지 않았다(평가액 불변, 현금·미수금만 대체).
- 예: 091170 08-28 `현금 0 + 평가 16,360,000 + 미수금 59,000 = 16,419,000`;
  09-02 `현금 59,000 + 평가 16,465,000 + 미수금 0 = 16,524,000`.

## 5. 분할 실행·재시작 연속성

| 분할 지점 | 현금 | 보유 | 미수금 | processed ids | 평가액 | 체결 |
|---|---|---|---|---|---|---|
| 08-31 (지급 전) | 일치 | 일치 | 일치 | 일치 | 일치 | 일치 |
| 09-02 (지급 후) | 일치 | 일치 | 일치 | 일치 | 일치 | 일치 |

- 지급 후 분할에서도 동일 이벤트가 재처리되어 **중복 지급되지 않았다**(processed ids 복원).
- 지급 전 분할에서 미수금이 유지되어 다음 구간에서 지급되었다.
- **lifecycle 복원**: 합성 `SUSPENSION_START` fixture로 분할 실행 후 상태가 `SUSPENDED`로
  유지되는지 확인했다(복원 순서 결함 수정).

## 6. 기존 동결 전략 재현

P0-B 기준 실행(`--phases cost`, 동결 설정 cap 0.85·spread 2bp, `ohlcv_capacity`)을 재실행했다.

| 항목 | 기대값 | 재현값 | 차이 |
|---|---:|---:|---:|
| CAGR | 0.245411377867 | 0.2454113778672673 | 2.7e-13 |
| MDD | -0.5121244554 | -0.5121244554166072 | 1.7e-11 |
| Sharpe | 1.03913577 | 1.0391357658469746 | **4.2e-9** |

- CAGR·MDD는 1e-9 이내. Sharpe는 기대값이 8자리로 제시되어 **1e-9 비교가 불가능**하며,
  8자리 기준으로는 일치한다(원시 차이 4.2e-9는 제시 정밀도의 반올림 한계).
- legacy 기본 실행(비승인)도 동일 입력에서 이전 값(CAGR 23.6552%, MDD -51.7775%,
  Sharpe 1.0171)을 그대로 재현해 기본 경로 무변경을 확인했다.
- 거래 원장은 P0-B의 `assert_legacy_reconciled`를 통과했으나, 저장된 기준 원장이 없어
  행 단위 diff는 수행하지 않았다.

## 7. Strict Approval·잔여 blocker

- 실제 74건 ledger로 strict preflight를 실행: **BLOCKED**, `INCOMPLETE_COVERAGE`.
- 이번 회계 E2E는 `allow_partial_ledger` 연구 seam으로 실행했으며 승인 결과가 아니다.
- seam 실행의 최종 `approval_report`는 항상 **BLOCKED**이며 `RESEARCH_PARTIAL_LEDGER`
  blocker가 추가된다(연구 실행이 APPROVED를 반환할 수 없음).
- manifest VERIFIED 승격, guard 제거, ledger 교체, 승인 성과 표시는 하지 않았다.

## 8. Track B 연계

- 이벤트 적용일: 분배금은 `ex_date`(파생)에 권리 발생, `payment_date`에 현금.
- 필요한 상태: `pending_receivables`, `processed_action_ids`, `corporate_action_states`.
- 재시작/재연결 시 위 세 상태를 함께 넘겨야 중복 지급·미수금 소실을 막는다.
- 중복 방지 키: `event_id`(또는 ticker+record_date).
- 지급일이 비거래일이면 기존 명시적 calendar 정책을 그대로 사용한다(엔진은 달력을 추론하지 않음).
- 남은 연계: 브로커 정산·실계좌 미수금 대사, 세후 회계(P2-A-2d-2).

## 9. 테스트·리뷰

- `scripts/test_p2a2d1_distribution_accounting.py` 6개 PASS: 보유 타임라인, 독립 미수금
  (배당락일 매수 제외·매도 포함·지급 제거), 상태 시그니처(lifecycle 포함), 연구 seam guard,
  빈 거래일 연구 보고서 BLOCKED, 기대값 상수.
- `scripts/test_p2a2b_corporate_actions.py` PASS(후속 보완 포함).
- 회귀 PASS: corporate_actions, approval_outputs, backtest_integrity, total_return, pit_universe,
  p2a2a fixtures, p2a1 fixture.
- Codex 독립 리뷰 3회: P1 2건(lifecycle 복원 소실, seam의 APPROVED 반환)과 P2 5건
  (독립 현금·평가 재구성, 실패 종료, 미수금 차단 시나리오, CLI 전달, 빈 거래일 보고서)
  → 전부 수정 후 **최종 APPROVE(P0/P1/P2 없음)**.

## 10. 한계

- 실데이터 E2E는 091170·498400의 2026년 이벤트에 한정(전체 74건 전 기간 실행 아님).
- 배당락일은 파생값이며 세전 회계만 검증했다.
- 거래 원장 행 단위 기준 diff는 저장된 기준 원장 부재로 수행하지 않았다.
- 브로커 정산·실계좌 연계는 범위 밖이다.
