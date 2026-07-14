# 변경 이력

주요 완료 작업 요약. 자세한 내용은 개별 커밋 참조.

## 2026-07 — 멀티 인덱스 리스크 시그널 (Phase 1)

**목표:** KOSPI 단일 시그널 외에 미국 지수(QQQ)를 추가하여, KOSPI risk_off 구간에서 미국 ETF로 회전하는 레짐 디커플링을 구현.

**변경:**
- `etf_shared.py`: `get_allowed_groups()` (hybrid/split 모드), `is_ticker_allowed()` 게이팅 헬퍼 추가
- `run_etf_backtest.py`: `get_us_index_data()` (yfinance 기반 미국 지수 조회·캐시), `is_us_risk_on()`, `run_etf_strategy()`에 `us_index_df`/`enable_multi_index_risk` 파라미터 추가
- `live_trading/etf_daily_runner.py`: `RunnerConfig`에 멀티 인덱스 필드 추가, `_load_us_risk_on()` 실시간 시그널, `_build_plan()`에서 하이브리드/스플릿 게이팅 적용
- `pyproject.toml`: `yfinance>=0.2.44` 의존성 추가
- `scripts/run_phase1_ab_and_compare.py`: baseline vs multi-index A/B 비교 스크립트
- `scripts/compare_phase1_results.py`: CAGR/MDD/Sharpe/Sortino 비교 + 게이트 체크 (Sharpe/Sortino 10%↑ AND MDD 1%p↓)
- `scripts/sweep_multi_index_split.py`: US 프록시(SPY/QQQ) × MA/slope 그리드 스윕

**환경변수:**
- `ENABLE_MULTI_INDEX_RISK=0|1` — 멀티 인덱스 활성화
- `MULTI_INDEX_GATING_MODE=hybrid|split` — 게이팅 모드 (split: 국내=KOSPI, 미국=US 지수 독립)
- `US_RISK_PROXY=QQQ` — 미국 리스크 프록시 심볼 (yfinance)
- `US_MARKET_MA_DAYS=100`, `US_MARKET_SLOPE_DAYS=20` — 미국 시그널 윈도우

**백테스트 결과 (single mode, 2016-01~2026-07):**

| 지표 | Baseline | split+QQQ | Δ |
|---|---|---|---|
| CAGR | 28.79% | 29.74% | +0.95% |
| Sharpe | 1.319 | 1.366 | +3.6% |
| Sortino | 2.058 | 2.138 | +3.9% |
| MDD | -19.61% | -19.57% | +0.04% |

**워크포워드 검증 (6폴드, 2019-08~2025-08):**

| 지표 | Baseline | split+QQQ | Δ |
|---|---|---|---|
| OOS CAGR | 17.36% | 18.08% | +0.72% |
| OOS Sharpe | 1.089 | 1.212 | +11.3% |
| OOS Sortino | 1.568 | 1.758 | +12.1% |
| OOS MDD | -15.34% | -13.02% | +2.32%p |

스윕 결과 QQQ가 모든 MA/slope 조합에서 동일하게 우세 (100/20, 120/10, 120/20, 140/20 모두 동일 결과). SPY는 모든 조합에서 baseline 대비 열세. MDD 개선은 전 폴드에서 일관적.

**파일:** `etf_shared.py`, `run_etf_backtest.py`, `live_trading/etf_daily_runner.py`, `scripts/walk_forward_validation.py`, `scripts/run_phase1_ab_and_compare.py`, `scripts/compare_phase1_results.py`, `scripts/sweep_multi_index_split.py`, `pyproject.toml`, `.env.sample`

---

## 2026-07 — 포트폴리오 리스크 관리 실험 및 성과 리포트 교정

**목표:** 현재 모멘텀 전략의 집중 위험을 완화할 수 있는 리밸런싱/청산 규칙을 구현하고 OOS로 검증.

**구현 및 검증 결과:**

| 실험 | 구현 | OOS 결과 | 결정 |
|---|---|---|---|
| 목표비중 리밸런싱 | `TARGET_WEIGHT_REBALANCE`, `REBALANCE_BAND_PCT` | CAGR 17.36→14.57%, MDD -15→-20% | 채택 안 함 |
| 비대칭 하드캡 | `TRIM_OVERWEIGHT_POSITIONS` | cap 70% CAGR 하락만, 85%는 미작동 | 채택 안 함 |
| 종목별 trailing stop | `ETF_EXIT_CHECK_DAYS`, `ETF_TRAILING_STOP_PCT` | 마지막 fold에만 미미한 개선 | 채택 안 함 |
| 포트폴리오 trailing stop | `ETF_PORTFOLIO_TRAILING_STOP_PCT` | 20%가 우수하나 엔드포인트 과적합 의심 | 채택 안 함 |

**공통 교훈:** 승자 비중을 줄이거나 조기에 청산하는 규칙은 모멘텀 전략의 상승 참여율을 떨어뜨리지만, 하락 방어 효과는 충분히 나오지 않았다. 특히 OOS 마지막 fold에만 집중된 개선은 일반화되기 어렵다.

**기타 변경:**
- 성과 리포트 교정: MDD 고점/저점/회복일/진행 여부, Rolling CAGR/MDD/Sharpe/Sortino 누락 버그 수정, 회전율 gross/one-way 분리, 리밸런싱 판단/거래/무거래 분리, 청산/미청산 lot 보유기간 분리
- `analyze_current_drawdown.py`: 포지션별 고점 비중·가격 하락·손실 기여도 및 리밸런싱 판단 이력 분석 스크립트
- `portfolio_stop_sensitivity.py`: 포트폴리오 stop 민감도 분석 스크립트
- `walk_forward_validation.py`: 목표비중/cap/밴드/trailing stop 별 독립 실행, `WF_OUTPUT_DIR`로 기존 결과 보존

**파일:** `etf_shared.py`, `run_etf_backtest.py`, `live_trading/etf_daily_runner.py`, `scripts/walk_forward_validation.py`, `scripts/analyze_current_drawdown.py`, `scripts/portfolio_stop_sensitivity.py`, `scripts/check_strategy_freeze.py`, `scripts/test_rebalance_fix.py`, `strategy_freeze.json`, `.env.sample`, `README.md`, `AGENTS.md`

---

## 2026-07 — 실전 위험 모니터링 (비매매 경고)

**목표:** 매매 로직 변경 없이 포트폴리오 위험 상태를 실시간으로 파악하고 Telegram 알림에 포함.

**구현:**
- `_calculate_risk_snapshot()`: 주문 전 전략 보유분· 예수금 기준 평가액, 종목별 비중, 누적 고점 대비 낙폭 계산
- 고점·위험 스냅샷을 `runtime_state/etf_daily_state.json`에 누적 저장 (최초 실행 시 현재 평가액으로 초기화)
- 로그에 실계좌/모의 구분하여 위험 현황 출력, 경고 별도 표시
- `NO_ACTION` 실전 실행에도 Telegram 요약 발송
- 모의 잔고는 실계좌 고점을 덮어쓰지 않음
- 가격 누락 시 낙폭 계산 차단으로 잘못된 경고 방지

**경고 조건:**
- 단일 종목 비중 60% 이상 (`LIVE_CONCENTRATION_WARN_PCT`)
- 실계좌 고점 대비 낙폭 15% 이상 (`LIVE_DRAWDOWN_WARN_PCT`)
- `0` 설정 시 해당 경고 비활성

**주의:** 계좌 입출금도 평가액 변동으로 인식되어 낙폭에 영향. 큰 현금 이동 후에는 저장된 고점 기준 확인 필요.

**파일:** `live_trading/etf_daily_runner.py`, `live_trading/telegram_notifier.py`, `.env.sample`, `README.md`, `AGENTS.md`

---

## 2026-07 — 후보 0개 진단 프레임워크 및 영향 분석

**문제:** `risk_on + 후보 0개` 빈도(11.7%)가 5% 기준을 초과하여 정상 작동인지 조사 필요.

**변경:**
- `run_etf_backtest.py`: `run_etf_strategy()`에 선택적 `rebalance_observer` 콜백 추가. 각 리밸런싱 전후 포트폴리오 상태(의사결정, 주문, 체결)를 기록. 기존 11개 호출부 변경 없음 (keyword-only, 기본값 `None`).
- `scripts/analyze_filter_frequency.py`: 리밸런싱 시점별 risk_on 상태와 필터 통과 종목 수를 집계하는 진단 스크립트.
- `scripts/analyze_zero_candidate_impact.py`: `risk_on + 후보 0개` 사건의 실제 포트폴리오 영향을 분석. 기존 보유 유지 여부, 무포지션 비율, 이후 5/10/20/40거래일 KOSPI/고정 포트폴리오/실제 전략 수익률 비교.

**분석 결과:** 7건 중 6건은 기존 보유를 유지했으며, 실제 무포지션은 1건(1.7%)에 불과. 유일한 무포지션 사건(2018-03-20)에서도 시장 하락(-1.35%~-1.73%)을 회피. 현재 로직은 정상 방어 기능으로 판정.

**파일:** `run_etf_backtest.py`, `scripts/analyze_filter_frequency.py`, `scripts/analyze_zero_candidate_impact.py`

---

## 2026-07 — NAV 기반 랭킹 및 ETF 안전 필터 1차 적용

**문제:** 가격수익률만으로 랭킹하면 해외/커버드콜/고배당 ETF의 NAV 변화와 괴리율을 충분히 반영하지 못하고, 신규 상장 ETF가 충분한 이력 없이 후보에 들어올 수 있음.

**변경:**
- `pykrx_utils.py`: `fetch_etf_ohlcv_with_nav()` 추가. ETF 전용 OHLCV/NAV 조회 실패 시 일반 OHLCV로 폴백
- `etf_shared.py`: `ETF_RETURN_BASIS=price|nav`, `MIN_LISTING_DAYS`, `MAX_PREMIUM_DISCOUNT`, `MAX_LIVE_SPREAD_PCT` 설정 추가
- `etf_shared.py`: `add_listing_flag()`, `add_deviation_flag()` 추가 및 `rank_etfs()` 필터 확장
- `run_etf_backtest.py`, `live_trading/etf_daily_runner.py`: NAV/상장일/괴리율 전처리 파이프라인 공통 적용
- `live_trading/etf_daily_runner.py`: 실전 bid-ask 스프레드 및 실시간 괴리율 초과 시 BUY 주문 스킵

**잔존 한계:** `ETF_RETURN_BASIS=nav`는 NAV 기반 총수익률 근사입니다. 분배형 ETF의 실제 분배금 재투자 성과는 별도 분배금 이력 보충 전까지 완전히 반영되지 않습니다.

**파일:** `pykrx_utils.py`, `etf_shared.py`, `run_etf_backtest.py`, `live_trading/etf_daily_runner.py`, `README.md`, `.env.sample`, `AGENTS.md`

---

## 2026-06 — ETF 균등분배 리밸런싱 수정

**문제:** 슬롯 기반 `buy_list`(`targets not in holdings`)가 이미 보유 중인 target 종목을 매수 후보에서 제외하여, 예산이 하나의 신규 종목에 집중되는 현상 발생 (예: 2위 90%, 1위 10%).

**변경:**
- `etf_shared.py`: `buy_list = targets[:max_positions]` — 보유 여부와 무관하게 모든 rank-N target에 균등 분배
- `live_trading/etf_daily_runner.py`: `MAX_ASSET_PCT=0.50` 기본값 RunnerConfig + 두 `build_rebalance_orders()` 호출 지점에 연결
- 기존 slippage 불일치 수정: 2차 호출 지점이 0.0005 고정 대신 `slippage=` 파라미터를 전달하도록 변경

**성과 (single mode, 5bp 슬리피지, 2021–2026):**

| 지표 | Before | After | Δ |
|---|---|---|---|
| CAGR | 41.71% | 46.91% | +5.20% |
| MDD | -22.16% | -22.32% | -0.16% |
| Sharpe | 1.43 | 1.50 | +0.07 |
| 거래 수 | 84 | 116 | +32 |

**파일:** `etf_shared.py`, `live_trading/etf_daily_runner.py`, `scripts/test_rebalance_fix.py`

---

## 2026-06 — Kiwoom 현금 이중가산 및 rate-limit 수정

**문제:** D+2 미결제 매도대금으로 인해 Kiwoom이 2차 보유종목 조회에서 이미 매도 완료된 종목을 다시 반환 → 현금 이중가산 발생. 실전/모의 API throttle이 동일(0.1s)하여 모의 환경에서 429 에러 빈발.

**변경:**
- `kiwoom_adapter.py`: `get_available_cash()` 추가 (`qry_tp=3` 하드코딩, 추정예수금)
- `etf_daily_runner.py`: 2차 `build_rebalance_orders()` 입력에서 매도 완료 종목 필터링
- `kiwoom_adapter.py`: ENV_MODE 기반 throttle 기본값 (실전=0.1s, 모의=0.6s)
- `kiwoom_adapter.py`: 네트워크 오류에 지수 백오프 (2^attempt × delay, 최대 10s), 오류 유형별 개별 재시도 처리

**파일:** `live_trading/kiwoom_adapter.py`, `live_trading/etf_daily_runner.py`

---

## 2026-06 — KIS 매도 후 재계산 수량제한 및 모의 throttle 수정

**문제:** 매도 후 재계산 시 `nrcvb_buy_qty` 제한이 재적용되지 않아 KIS 모의에서 `[40250000]` 주문 실패 발생. 모의 API throttle(0.9s)이 불충분하여 사이클당 25회 이상 rate-limit hit.

**변경:**
- `etf_daily_runner.py`: 매도 후 재계산 구간에 `get_buyable_info()` / `nrcvb_buy_qty` 캡 적용 (초기 계획과 동일한 로직)
- `_kis_api_client.py`: 모의 throttle 0.9s → 1.0s; 불필요해진 `_smart_sleep()` 메서드 및 `_sleep_sec` 속성 제거
- `etf_daily_runner.py`: `plan["sell_orders"]`가 비어있으면 sell-phase 전체 스킵

**파일:** `live_trading/etf_daily_runner.py`, `live_trading/kis/_kis_api_client.py`
