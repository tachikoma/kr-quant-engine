# 백테스트 무결성 상태

이 문서는 Phase 0~3의 구현 범위와 승인 경계를 고정한다. 구현 완료는 역사 데이터
완전성, 성과 승인 또는 live 확대 승인이 아니다.

관련 문서: [README](../README.md) · [기업행위 ledger](CORPORATE_ACTIONS.md) ·
[OHLCV capacity](EXECUTION_CAPACITY.md) · [PIT 유니버스](POINT_IN_TIME_UNIVERSE.md)

## 구현된 보호장치

| 단계 | 보호장치 | 해석 경계 |
|---|---|---|
| Phase 0 | 합성 fixture의 t+1 체결, 현금·수량·수수료·세금 검산, 분배금 귀속 순서, 입력·설정 provenance hash | 연구 경로 회귀 검증이며 역사 원천의 정확성 증명은 아님 |
| Phase 1 | 최신 snapshot-as-of PIT membership, 미래 snapshot 차단, 거래일 기반 coverage preflight | static 경로와 분리; historical source 미검증 시 승인 차단 |
| Phase 2 | manifest-bound 기업행위 ledger, payment-date receivable, split/reverse split, suspension/delisting/settlement lifecycle, strict 출력 격리 | strict 모드에서 blocker가 있으면 성과 산출 안 함 |
| Phase 3 | OHLCV 참여율, partial/one-day carry, zero-volume·missing-open·suspension·possible-lock 진단, AUM 시나리오, reconciliation, 원자적 출력 교체 | **OHLCV capacity scenario**이며 실제 역사적 체결이 아님 |
| Phase 4 | state-based 폴드 경계 수익률 앵커링(직전 폴드 종료 equity), frozen 고정 정책 OOS와 adaptive fold-selected OOS 분리 보고(`policy_type` 마커, 별도 산출물) | 구현·오프라인 검증 완료. 실데이터 전 기간 실행 결과는 별도 evidence |

기본 `execution_mode=legacy`와 static 연구 경로는 유지된다. strict/PIT/capacity
경로는 명시적 opt-in이며 서로의 산출물을 덮어쓰지 않는다.

## strict fail-closed 조건

### PIT

다음이면 PIT preflight가 중단한다.

- 첫 snapshot 이전 또는 trading calendar에 없는 결정일/snapshot
- 25 거래일을 넘는 as-of snapshot 공백
- 적격 종목의 usable price, group/classification, tax coverage 누락
- 현재 또는 추정·복원 분류를 historical effective-dated 증거로 대체
- historical classification/tax source가 검증되지 않음

현재 PIT membership·가격 파이프라인은 연결되어도, effective-dated historical
classification/tax source가 없으므로 strict PIT 승인 결과가 아니다.

### 기업행위 strict approval

다음은 approval blocker다.

- 빈/불완전 ledger·manifest, 기간·유니버스 coverage 불일치
- unknown/중복 이벤트, 잘못된 날짜·비율·금액, manifest URL/SHA provenance 불일치
- payment/settlement 원천 누락, 잘못된 lifecycle 순서, 미해결 reverse-split 분수
- stale/suspended/unsettled/delisted final holding 또는 최종일 usable raw close 부재

차단 실행은 `outputs_approval/`에 blocker/report/reproducibility만 기록하고
`outputs_etf_only/`와 정상 성과 지표를 만들지 않는다. 상세 계약은
[CORPORATE_ACTIONS.md](CORPORATE_ACTIONS.md)를 따른다.

## OHLCV capacity 시나리오

일봉 OHLCV는 volume 기반 가정 용량을 계산할 수 있을 뿐, order-book depth, queue
priority, 제출·응답 시각, 실제 매칭/미체결, VI·정지 사실 또는 lock 방향을 증명하지
못한다. `FULL`/`filled_qty`는 broker 체결 증거가 아니며 live 주문 크기 승인 또는
실제 체결 정확도 주장에 사용하지 않는다.

```bash
uv run python run_etf_backtest.py --mode single \
  --execution-mode ohlcv_capacity \
  --execution-participation-rate 0.05 \
  --execution-aum 10000000,100000000,1000000000 \
  --execution-output-dir outputs_execution
```

각 AUM은 fresh state로 독립 실행한다. `outputs_execution/`에는 아래 다섯 파일만
생성한다.

- `execution_summary.csv`: 요청·용량·가정 filled 수량 요약
- `execution_diagnostics.csv`: 주문별 capacity/carry/취소 사유
- `execution_trades.csv`: scenario 회계 반영 trade; broker 기록 아님
- `execution_reconciliation.csv`: 현금·보유수량·diagnostic/trade 수량 검산
- `execution_metadata.json`: 기간, AUM, 참여율, hash, 산출물 계약

모든 파일은 `diagnostic_only=true`, `executable_fill_claim=false`,
`orderbook_used=false`로 표기한다. 출력은 sibling staging directory에 완성한 뒤
directory swap/rollback으로 교체하므로 commit 실패 시 기존 다섯 산출물을 보존한다.
`outputs_etf_only/` 및 `outputs_approval/`과 겹치는 경로, `--approval-strict`,
non-single mode는 거부한다. 상세 한계는 [EXECUTION_CAPACITY.md](EXECUTION_CAPACITY.md)를
참고한다.

## 명령과 산출물

```bash
# legacy 연구 백테스트
uv run python run_etf_backtest.py --mode single

# PIT: coverage 미검증이면 fail-closed
uv run python scripts/pit_backtest.py

# 기업행위 strict approval: 현재 체크인 template은 의도적으로 차단됨
uv run python run_etf_backtest.py --approval-strict --mode single \
  --corporate-actions-ledger data/etf_corporate_actions.csv \
  --corporate-actions-manifest data/etf_corporate_actions_manifest.json \
  --approval-output-dir outputs_approval

# 핵심 회귀 fixture
uv run python scripts/test_backtest_integrity.py
uv run python scripts/test_pit_universe.py
uv run python scripts/test_corporate_actions.py
uv run python scripts/test_execution_realism.py
uv run python scripts/test_execution_integration.py
uv run python scripts/test_execution_outputs.py
uv run python scripts/test_walk_forward_validation.py
```

결과 경로는 legacy `outputs_etf_only/`, strict `outputs_approval/`, capacity
`outputs_execution/`이다. `uv run ruff check .`는 기존 Ruff debt 때문에 전체 green
증거가 아니며, 변경 범위의 scoped Ruff와 fixture 결과를 별도로 보존한다.

## 남은 blocker와 승인 전 evidence

1. `data/etf_distributions.csv`는 빈 template이다. 장기 검증 원천 전에는
   `total_return` 및 분배금 포함 성과를 승인 근거로 사용하지 않는다.
2. 공식 과거 기업행위, effective-dated tax/classification, 상장폐지·정산 원천과
   manifest coverage가 없다. strict corporate-action/PIT은 이를 대체 추정하지 않는다.
3. WFA boundary return 교정과 frozen-policy/adaptive-policy 분리는 구현·게이트
   승인되었다(`walk_forward_validation.py`, `fixed_policy_oos_*.csv/json`). 다만
   실데이터 전 기간 실행 결과가 남아 있지 않아, 실행 산출물 확정 전까지 WFA를 공식
   OOS/live 확대 근거로 쓰지 않는다.
4. 기존 repository-wide Ruff debt가 남아 있다. 전체 lint 실패를 green으로 보고하지 않는다.

성과 주장·동결 갱신·live 확대 전에는 다음 evidence가 필요하다.

- 전체 기간/유니버스의 PIT membership·가격·분류·tax effective-dated provenance/hash
- 모든 기업행위의 공식 document ID/URL/SHA, payment/settlement coverage,
  strict approval blocker 0건의 report
- Phase 0~3 fixture fresh 결과와 실행 기간, row count, input/config/freeze hash, commit
- WFA boundary correction 및 frozen/adaptive OOS 정책의 별도 승인과 실데이터 실행 산출물
  (`walk_forward_summary.json`의 `policy_type=adaptive_fold_selected`,
  `fixed_policy_oos_summary.json`의 `policy_type=frozen_fixed`) 확보
- 실체결을 주장할 경우 timestamped order book/브로커 주문·체결·취소 기록과
  matching/queue/impact 가정

이 evidence 전에는 static 성과나 OHLCV capacity 결과를 live 자금 확대, 실제 체결
정확도, 또는 승인 성과의 근거로 사용하지 않는다.
