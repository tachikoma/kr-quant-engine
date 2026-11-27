# P2-B-1 — Forward Shadow MVP (설계·운영 인계 문서)

## 1. 범위와 원칙

- **독립 트랙**: 동결된 ETF 모멘텀 v2 Baseline의 forward shadow만 구현한다. 새 전략 엔진,
  전략 최적화, 후보 A 채택, 아키텍처 확장은 하지 않는다.
- **재사용**: 랭킹·gating·주문생성·비용은 `etf_shared.py`, OHLCV capacity·부분체결·carry는
  `etf_execution.py`, 가격 전처리는 `pykrx_utils.py` / `etf_shared.py`를 그대로 호출한다.
- **안전**: 실주문 API·브로커 어댑터를 import하지 않는다. 실전 checkout, Track A worktree,
  `strategy_freeze.json`, cron/systemd는 변경하지 않는다.
- **DRY RUN을 Shadow로 쓰지 않는다**: 기존 daily runner의 안전모드는 주문 실행 경로이며,
  상태·원장·신호 분리가 없으므로 Shadow로 사용하지 않았다.

## 2. 개발 환경 (조사 결과)

| 항목 | 확인 결과 |
|---|---|
| 기준 SHA | `b7b8374a4dc1973689e5922f3dba525e2bd7251b` (origin/main 동일) |
| PR #15 | MERGED (2026-11-26, 역사 데이터 신뢰성/blocker 감사) |
| 브랜치 | `feature/forward-shadow` |
| worktree | `/home/ubuntu/investment/kr-quant-engine-forward` (기존 생성됨) |
| tmux | `kqe-forward` (기존 세션, 재사용) |
| 기존 shadow/paper 구현 | 이 저장소에는 없음. 다른 로컬 저장소(quant-system 등)에 별도 구현 존재하나 전략이 달라 재사용하지 않음 |
| 운영 checkout | `/home/ubuntu/kr-quant-engine-forward` 아직 없음 (향후 경로) |
| cron/systemd | `crontab` 없음. `overreaction-open/close.timer`만 존재(다른 전략, 본 트랙과 무관). Shadow 타이머는 활성화하지 않음 |
| 디스크/메모리 | 42 GB 여유 / 3.4 GB available |
| `.env` | 없음. 복사하지 않음. 조회 전용 설정만 코드 기본값+freeze로 구성 |

## 3. freeze 대비 코드 기본값 차이 (실효 설정)

`strategy_freeze.json`은 해시 `4902b181…`로 검증되며 변경하지 않았다. 다음 값은 freeze에는
없거나 코드 기본값과 달라 **Shadow manifest에 명시적으로 고정**했다.

| 파라미터 | freeze | 코드/env 기본값 | Shadow 실효값 |
|---|---|---|---|
| `rebalance_step_days` | 20 | 10 | **20** |
| `max_asset_pct` | 0.85 | 0.50 | **0.85** |
| `spread_pct` | 0.0002 | 0.0005 | **0.0002** |
| `slippage` | 0.0005 | 0.0005 | 0.0005 |
| `momentum_weight_60/120` | 미기재 | 0.55 / 0.45 | **0.55 / 0.45** (명시) |
| `capacity_participation_rate` | 미기재 | 0.05 | **0.05** (명시) |
| `capacity_max_carry_days` | 미기재 | 1 | **1** (명시) |
| `buy/sell_fee_pct` | 미기재 | 0.00015 / 0.00015 | **0.00015 / 0.00015** (명시) |
| `sell_tax_pct` | 미기재 | 0.154 (과세 8종) | **0.154** (명시) |
| `rebalance_phase_offset` | 미기재 | 0 (연구용 opt-in) | **0** (명시) |
| `enable_multi_index_risk` | false | false | false → `us_risk_on=True` |
| `multi_index_gating_mode` | split | hybrid | **split** |

수수료는 0.015%(0.00015)이다. 1.5%로 오해하면 안 된다.

### 리밸런싱 anchor / phase 0

- 백테스트 공식 경로: `warmup_days = max(120, MA120+slope20) = 140`,
  `(i - warmup_days + rebalance_phase_offset) % 20 == 0`, `rebalance_phase_offset=0`.
- `rebalance_phase_offset`는 `approval_strict`·`ohlcv_capacity`·`initial_state` 경로에서
  거부되는 **연구용 opt-in**이며 freeze payload에 없다.
- P1-B-1 연구에서 위상 0/5/10/15의 성능 민감도가 큼을 확인했으나, **실전 리밸런싱 기준일은
  승인된 문서에서 확인되지 않았다.** 따라서 Shadow는 anchor를 `UNAPPROVED_TEST_VALUE`로
  기록하고 임의로 결정하지 않는다. 운영 시작 전 결정 항목으로 남긴다.

## 4. 구현 구조

```
forward_shadow/
  config.py    freeze→실효 파라미터, manifest 생성/검증, 코드·freeze 드리프트 fail-closed
  store.py     fcntl 실행잠금, append-only event ledger(hash chain), 원자적 state 기록,
               COMMIT 기반 replay 복구, 무효 꼬리 event ABORT 처리
  market.py    pykrx/fixture provider, 엄격 검증, MA120/slope20 계산, snapshot 저장
  engine.py    etf_shared.rank_etfs/build_gating_decision/build_rebalance_orders 재사용,
               etf_execution.decide_execution 재사용, 비용·현금·원가 정산
  report.py    일별 리포트 생성/파일 저장, opt-in Telegram 발송(기본 off)
  runner.py    하루 단위 오케스트레이션, 휴장/중복/데이터 보류, 커밋 및 리포트
run_forward_shadow.py   별도 entrypoint
tests/test_forward_shadow*.py  합성·실제·비교 E2E
```

### 독립 runtime root (`shadow_runtime/`, gitignored)

- `manifest.json` — 코드 SHA, freeze SHA, 실효 파라미터, anchor/초기자본/관측일(미승인 표기),
  가상체결 모델, 데이터 원천·한계
- `state.json` — 현금·보유·원가·peak·pending signal·carry·equity curve·last valid price
- `events.jsonl` — append-only, seq/event_id/observed_at/state_patch/prev_hash/hash
- `snapshots/<date>.json`, `reports/<date>.json|txt`, `logs/`, `cache/`, `.shadow.lock`

### 핵심 불변식

1. **D 신호 / D+1 체결**: 결정일 종가로 목표를 확정하고, 다음 거래일 시가에 스프레드를
   적용해 수량·가상체결을 계산한다. 미래 시가를 결정일에 참조하지 않는다.
2. **데이터 보류**: 16종 + KOSPI 중 하나라도 누락/무효/거래정지면 신호와 체결을 모두 보류한다.
   `is_risk_on()`의 결측 시 `True` 폴백을 쓰지 않는다. 후보 0개로 오인하지 않는다.
3. **stale 표기**: 평가가격이 last-valid 폴백이면 `stale_tickers`에 기록하고 리포트에 표시한다.
   신호·체결에는 stale 가격을 쓰지 않는다.
4. **idempotency**: `processed_dates` + event_id로 동일 거래일 재실행을 차단한다.
5. **동시 실행 차단**: `fcntl.LOCK_EX|LOCK_NB` (live runner와 동일 패턴).
6. **복구**: COMMIT event의 state digest로 replay 검증. COMMIT 후 state写入 전 crash는 replay로
   복구하고, 커밋되지 않은 꼬리 event는 `ABORT`로 표시해 적용하지 않는다.
7. **fail-closed**: freeze 해시·코드 fingerprint·실효 파라미터·anchor 상태가 manifest와 다르면
   실행을 중단한다. 원장 hash chain이 깨지면 중단한다.
8. **과거 재실행 금지**: 매일 전체 백테스트를 다시 돌려 state를 덮어쓰지 않는다. warm-up 데이터는
   지표 계산에만 쓰고 forward 실적에 등록하지 않는다.

## 5. 데이터·Strict Approval 한계

현재 blocker는 모두 유지된다 (docs/p2a1_data_evidence.md).

- PIT-CLASS-TAX: historical classification/tax 원천 없음 → `pit_membership_ok` 컬럼을 만들지
  않아 filter를 비활성화하고 리포트에 명시한다.
- CA-COVERAGE: `data/etf_corporate_actions.csv`는 헤더만 있는 템플릿.
- DISTRIBUTION: `data/etf_distributions.csv`는 헤더만. `price` return 기준만 사용.
- INDEPENDENT-SOURCE: KRX 외 구조화 원천 없음.
- OOS: 46거래일 freeze 모니터링은 독립 OOS가 아님.

실제 KOSPI 지수 조회는 KRX 인증 정보가 없어 실패한다. Shadow는 이를 `BLOCKED_DATA`로 처리하며
`risk_on=True`로 대체하지 않는다. 실제 데이터 E2E는 읽기 전용 캐시(`index.parquet`, 16종
parquet)를 별도 테스트 runtime에 주입해 수행하며, 이를 Forward 실적으로 등록하지 않는다.

OHLCV capacity는 `floor(volume * 5%)`의 진단용 근사치이며 호가창·체결시각·VI/거래정지 정보가
없다. 실제 체결 가능성으로 주장하지 않는다.

## 6. 검증 결과 요약

- 합성 E2E 10건: 정상 매수/매도, 20일 주기(0/20/40), D→D+1 분리, 비용·현금·수량,
  부분체결·carry 만료, 데이터 누락 보류, 휴장/중복, 장애 복구, 원장/state 불일치 차단,
  freeze/config drift 차단, 브로커 import 차단 — 모두 PASS.
- 실제 데이터 E2E 3건: 캐시 16종+KOSPI 검증·snapshot, 공통 전략과 랭킹 대조,
  신호→가상체결→정산→리포트, 중복 방지, 라이브 지수 실패 fail-closed — 모두 PASS.
- 기존 백테스트 대조 1건: 동일 합성 입력에서 첫 결정일·목표 종목 일치, equity 차이는
  capacity 모델·독립 정산에 기인 — PASS.

## 7. 운영 시작 전 결정 필요 (승인 대상)

1. 초기자본 확정 (현재 테스트값 1억 원)
2. Forward 관측 시작일 확정 (현재 테스트값)
3. 리밸런싱 anchor / decision offset 확정 (현재 `UNAPPROVED_TEST_VALUE`, offset 0)
4. Strict Approval blocker 해소 여부 및 가격 원천 승인
5. Telegram 발송 채널/스케줄 활성화 (현재 발송 기본 off, 정기 스케줄 없음)
6. PR 병합 및 운영 배포 (본 단계에서는 하지 않음)
