# 변경 이력

주요 완료 작업 요약. 자세한 내용은 개별 커밋 참조.

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
