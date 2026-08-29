# 검증 종합 문서 — kr-quant-engine

- **작성일**: 2026-08-29
- **작성자**: kr-quant-engine
- **목적**: 지금까지 수행한 검증(백테스트-실전 정합성, trailing 간극, 유니버스 PIT 간극, 검증 이력)을 단일 문서로 정리

---

## 1. 백테스트-실전 정합성

백테스트(`run_etf_backtest.py`)와 실전 러너(`live_trading/etf_daily_runner.py`)는
핵심 랭킹/게이팅/주문 생성 로직을 `etf_shared.py`에서 공유한다. 양쪽 모두 동일 모듈을
import 하므로 의사결정 자체는 일치한다. 다만 실전 환경에만 존재하거나 적용 방식이
다른 항목이 있어 아래 표로 정리한다.

| 항목 | 공유 여부 | 백테스트 | 실전 러너 | 비고 |
|---|---|---|---|---|
| 유니버스 / 티커 그룹 | 공유 | `ETF_LIST`, `ETF_TICKER_GROUPS` | 동일 (`etf_shared`) | `ETF_UNIVERSE_MODE=auto` 시 `etf_universe.build_universe()`로 오버라이드 |
| 랭킹 파이프라인 `rank_etfs()` | 공유 | O | O | pit/유동성/상장/괴리율/추세 필터 동일 적용 |
| 게이팅 `build_gating_decision()` | 공유 | O | O | risk_on/off 허용 그룹·강제청산 일원화 |
| 주문 생성 `build_rebalance_orders()` | 공유 | O | O | 균등분배 + `MAX_ASSET_PCT` 캡 동일 |
| 전처리 플래그 (유동성/상장/괴리/가격기준) | 공유 | O | O | `add_liquidity_flag` 등 4종 공유 |
| 과세 ETF 매도세 | 공유 상수 | `ETF_TAXABLE_SELL_TAX_PCT` 적용 | 증권사 예수금에 자동 반영 | 동일 세율 상수 사용 |
| 슬리피지 | **불일치** | `ETF_BASE_SLIPPAGE`(5bp) 적용 | `APPLY_SLIPPAGE_IN_LIVE=0` 기본 — 인위 슬리피지 미적용, `LIVE_SLIPPAGE_PCT`는 활성 시만 | 실전은 체결가 기준 |
| 호가 스프레드 skip | 실전 전용 | 해당 없음 | `MAX_LIVE_SPREAD_PCT` 초과 시 BUY skip | 백테스트는 `ETF_SPREAD_PCT` 비용 반영만 |
| Trailing stop (`ETF_EXIT_CHECK_DAYS`/`ETF_TRAILING_STOP_PCT`) | **백테스트 전용** | O (실험 옵션) | **미적용** | 실전 러너에 미구현 |
| 포트폴리오 trailing stop | **백테스트 전용** | O (실험 옵션) | **미적용** | 동결 상태에서는 둘 다 0 |
| 결측가 fallback | 실전 중심 | — | `get_valuation_price()`/`update_last_valid_prices()`로 0평가 방지 | 백테스트는 캐시 완전성 가정 |
| 위험 모니터링 | 실전 전용 | — | `_calculate_risk_snapshot()` (비매매 경고) | 백테스트는 성과 리포트로 대체 |
| 휴장일 감지 | 실전 전용 | — | `_is_trading_day()` + `_KRX_HOLIDAYS` | 백테스트는 pykrx 캘린더 사용 |

**결론**: 의사결정(랭킹·게이팅·주문)은 공유 모듈로 일치. 실전 전용 가드(스프레드 skip,
결측가 fallback, 위험 모니터링, 휴장일)와 슬리피지 적용 차이만 존재하며, 이는 보수적
실전 보호 목적이다. Trailing stop은 백테스트 실험 옵션이며 실전에 적용되지 않으므로
정합성 위반이 아니다(동결값 0).

---

## 2. Trailing Stop 간극 실험

- **스크립트**: `scripts/trailing_stop_measure.py`
- **측정 파일**: `outputs_trailing/summary.json`
- **기간**: 2015-01-05 ~ 2025-08-04 (2600 거래일, 약 10.6년)
- **조건**: single 모드, slippage 5bp, rebalance 20일, price 기준, trailing OFF 기준선 vs 종목별 7% / 10%

| 케이스 | CAGR | MDD | Sharpe | 거래 수 |
|---|---:|---:|---:|---:|
| baseline (OFF) | 3.35% | -40.69% | 0.343 | 133 |
| caseA (7%) | 2.47% | -40.69% | 0.272 | 139 |
| caseB (10%) | 3.02% | -40.69% | 0.317 | 135 |

**결과 해석**:
- **MDD 개선 = 0**: 세 케이스 모두 MDD -40.69%로 동일. Trailing stop이 낙폭을 줄이지 못함.
- **CAGR 하락**: 7% 케이스는 -0.88%p, 10% 케이스는 -0.33%p 하락. 조기 청산이 상승 참여율을 떨어뜨림.
- Sharpe도 기준선(0.343) 대비 7%→0.272, 10%→0.317로 하락.

**결론**: Trailing stop은 하락 방어 효과 없이 수익률만 훼손 → 채택 안 함. 동결 파라미터
`trailing_stop_pct=0`, `portfolio_trailing_stop_pct=0` 유지.

---

## 3. 유니버스 PIT 간극 실험

- **스크립트**: `scripts/measure_universe_pit.py`
- **측정 파일**: `outputs_universe_pit/summary.json`
- **기간**: 2015-01-01 ~ 2025-08-04
- **조건**: 동일 조건(price 기준, slippage 5bp, rebalance 20일, trailing OFF, max_positions 2)

| 케이스 | 유니버스 크기 | CAGR | MDD | Sharpe | 거래 수 |
|---|---:|---:|---:|---:|---:|
| S (static, 16종) | 16 | 13.60% | -12.87% | 1.132 | 145 |
| A (auto 비-PIT, 524종) | 524 | 6.37% | -31.38% | 0.417 | 281 |
| P (auto PIT) | — | **SKIP** | — | — | — |

- **간극 (A − S)**: CAGR -7.23%p, MDD -18.50%p (auto가 정적 대비 열등)
- **P (PIT) 상태**: SKIP — `data_cache/pit_prices`(전체 1,370종) 및
  `pit_universe_snapshots.parquet` 부재로 생존편향 제거 백테스트 불가.
  따라서 (A − P) 간극은 이번 실행에서 산출되지 않음.

**생존편향 추론**:
- A(auto)는 현재 생존한 ETF만 포함하므로 과거 퇴출 종목의 손실이 누락된
  낙관적 편향이 있음. 그럼에도 A가 S보다 CAGR/MDD 모두 열등하다는 것은, 정적 유니버스가
  장기 생존성·유동성을 고려해 선별된 고품질 종목 위주임을 시사.
- PIT(P)가 측정되지 않아 (A − P) 생존편향 크기는 미확정. 다만 PIT가 A 대비 추가 손실
  종목을 포함하면 CAGR은 더 하락할 가능성이 높음.

**결론**: 정적 유니버스(static) 유지. auto는 생존편향에도 불구하고 열등하므로 채택 안 함.
PIT 데이터 구축 후 (A − P) 간극을 확정적으로 측정해야 함.

---

## 4. 검증 이력 타임라인

| 시기 | 작업 |
|---|---|
| 2026-06 | 인프라 구축: Kiwoom 현금 이중가산 수정, KIS 매도 후 재계산 수량제한, ETF 균등분배 리밸런싱 수정, NAV 기반 랭킹/안전 필터(유동성·상장·괴리율) 1차 적용, 후보 0개 진단 프레임워크 |
| 2026-07-07 ~ 07-09 | 포트폴리오 리스크 관리 실험: 목표비중 리밸런싱, 비대칭 하드캡, 종목별/포트폴리오 trailing stop 구현 및 OOS 검증 (모두 채택 안 함) |
| 2026-07-13 | 전략 동결 v1 (`freeze_date=2026-07-13`, `oos_start_date=2026-07-14`) |
| 2026-07-21 | split 게이팅 교정·재검토 반영 → v2 재동결 (`strategy_freeze.json`, `freeze_date=2026-07-21`) |
| 2026-07-22 ~ | v2 OOS 실전 추적 시작 (`runtime_state/oos_equity_history.json`, `scripts/track_oos_performance.py`) |
| 2026-08 | PIT 백테스트 연결, factorial ablation, 복수 벤치마크 비교, soft trend-bonus 검증 |

---

## 5. 동결 상태

- **파일**: `strategy_freeze.json`
- **freeze_date**: 2026-07-21 (v2)
- **oos_start_date**: 2026-07-22
- **universe_mode**: `static` (16종 고정)
- **핵심 파라미터**:
  - `trailing_stop_pct = 0.0`, `portfolio_trailing_stop_pct = 0.0` (trailing 미적용)
  - `exit_check_days = 0`
  - `return_basis = price`, `slippage = 0.0005`, `spread_pct = 0.0002`
  - `rebalance_step_days = 20`, `market_ma_days = 120`, `market_slope_days = 20`
  - `max_positions = 2`, `sell_rank_buffer = 3`, `max_asset_pct = 0.85`
  - `enable_multi_index_risk = false` (멀티 인덱스 미채택)
  - `liquidate_on_risk_off = true`
- **OOS 트랙**: 2026-07-22 ~ 현재 (v2 활성). v1(2026-07-14 시작)은 별도 트랙으로 혼합 금지.

---

## 6. 다음 단계

1. **PIT 데이터 구축**: `data_cache/pit_prices`(전체 1,370종 OHLCV) 및
   `pit_universe_snapshots.parquet` 수집 완료 후 `scripts/measure_universe_pit.py` 재실행 →
   (A − P) 생존편향 간극 확정. 간극이 유의미하면 auto/PIT 유니버스 채택 재검토.
2. **Trailing 구조 변경 시 재측정**: trailing stop 로직을 실전 러너에 이식하거나
   게이팅 구조를 변경할 경우, 본 문서 §2 실험을 동일 조건으로 재측정하여 채택 여부 재확인.
3. **OOS 모니터링 지속**: `scripts/track_oos_performance.py`로 v2 실전 성과 정기 점검,
   동결 파라미터 변경 시 신규 freeze 버전 생성.

---

## 부록: 재현 명령어

```bash
# Trailing Stop 간극 측정 (outputs_trailing/ 생성)
uv run python scripts/trailing_stop_measure.py

# 유니버스 PIT 간극 측정 (outputs_universe_pit/ 생성, PIT 부재 시 P=SKIP)
uv run python scripts/measure_universe_pit.py

# 동결 상태 / OOS 점검
uv run scripts/check_strategy_freeze.py
uv run scripts/track_oos_performance.py
```

**산출물 경로**:
- `outputs_trailing/summary.json`, `outputs_trailing/{baseline,caseA_7pct,caseB_10pct}_performance.json`
- `outputs_universe_pit/summary.json`, `outputs_universe_pit/{S,A}_equity_curve.csv`
