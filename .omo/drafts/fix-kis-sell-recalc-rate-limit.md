---
slug: fix-kis-sell-recalc-rate-limit
status: awaiting-approval
intent: clear
pending-action: write .omo/plans/fix-kis-sell-recalc-rate-limit.md
approach: 3가지 이슈를 각각 수정. (1) 매도 후 재계산 섹션에 KIS get_buyable_info() 수량 제한 추가, (2) get_prices()/get_bid_ask_prices() for-loop 내 API 호출 간 _smart_sleep() 간격 추가, (3) 매도 주문이 0건일 때 sell-phase 헤더/대기/잔고재조회 스킵
---

# Draft: fix-kis-sell-recalc-rate-limit

## Findings (cited - path:lines)

### 이슈 1: 매도 후 재계산에 KIS `get_buyable_info()` 수량 제한 누락 (CRITICAL)
- `etf_daily_runner.py`의 `_build_plan()` (L770-782): `build_rebalance_orders()` 호출 후
  BUY 주문별 `api.get_buyable_info()` 호출하여 `nrcvb_buy_qty`로 수량 제한 — **적용됨** ✅
- 동일 파일 `run_daily()` (L1451-1475): 매도 후 `build_rebalance_orders()` 재호출 시
  `get_buyable_info()` 검증 **없음** ❌
- 로그 증거: `[KIS제한] KODEX 반도체(091160) 수량 4→3주 (nrcvb_buy_qty)`는 첫 계획에서 발생했으나,
  `BUY 091160 4주 → [40250000] 모의투자 주문가능금액이 부족합니다`는 매도 후 재계산에서 4주 그대로 주문하여 발생

### 이슈 2: for-loop 내 API 호출 간 rate-limit 미고려
- `live_trading/kis/_kis_api_client.py`:
  - `_throttle()` (L89-99): `_min_interval=0.9s`(demo) 간격 보장
  - `_get()` (L101-126) / `_post()` (L128-153): rate-limit(EGW00201/EGW00215) 발생 시
    `_retry_delay=0.9s` 후 재시도 (최대 3회)
  - `_smart_sleep()` (L182-183): but **paginated API(get_balance/get_order_fills)에만 사용**되고,
    `get_prices()`, `get_bid_ask_prices()`, `get_buyable_info()` for-loop에는 사용 안 됨
- `live_trading/kis_adapter.py`:
  - `get_prices()` (L111-126): for-loop으로 각 ticker 순차 호출
  - `get_bid_ask_prices()` (L128-154): for-loop으로 각 ticker 순차 호출
- 로그 증거: 23:55:36 ~ 23:56:31 사이 25+회 rate-limit hit, 각 `attempt=1/3` (1회 재시도 후 성공).
  이는 모든 API 호출이 첫 시도에 rate-limit에 걸리고 재시도에서 성공한다는 뜻 → 간격 부족

### 이슈 3: 매도 주문 0건이어도 sell-phase 헤더/대기 로직 실행
- `etf_daily_runner.py`:
  - `run_daily()` (L1330-1388): `plan["sell_orders"]`가 비어있어도
    `[주문] ─── 매도 단계 ───` 출력 (L1330), `[주문] 매도 체결 대기 중...` 출력 (L1339),
    `_poll_and_finalize_orders()` 호출 (L1340-1348), 매도 후 예수금/보유 재조회 (L1392-1422)
  - `plan["sell_orders"]`가 비어있으면 `sell_results`도 비어있어서 `_poll_and_finalize_orders()`는
    즉시 반환하지만, 불필요한 API 호출(get_available_cash, get_holdings, get_prices, get_bid_ask_prices) 발생
  - `_is_side_fully_filled([], [])` → True 반환 → can_buy=True → 정상 진행되므로 기능상 문제는 없으나
    혼동을 주고 rate-limit을 가중시킴

## Decisions (with rationale)

1. **이슈 1 (Critical)**: 매도 후 재계산 섹션에 KIS `get_buyable_info()` 제한 로직 추가
   - `_build_plan()`의 L770-782 로직을 `run_daily()`의 매도 후 재계산 섹션(L1451-1475)에도 복제
2. **이슈 2 (Medium)**: `_SMART_SLEEP_DEMO` 0.9→1.0, `_smart_sleep()` 제거
   - `_SMART_SLEEP_DEMO = 0.9` → `1.0` 으로 변경
   - 이로 인해 `_min_interval`, `_retry_delay` 기본값도 demo에서 1.0s로 상향
   - `_throttle()` 간격이 1.0s가 되어 모든 API 호출 간 충분한 간격 확보
   - `_smart_sleep()` 및 `_sleep_sec` 속성/메서드 제거
     (paginated API: `_throttle()`만으로 간격 보장 가능)
   - 1.0s는 1차 값이며 테스트 후 조정 필요
3. **이슈 3 (Low)**: `plan["sell_orders"]`가 비어있으면 sell-phase 로직 스킵
   - `run_daily()`의 sell-phase 시작 부분에 조건문 추가
   - 매도 단계 헤더, 체결 대기, 매도 후 잔고 재조회 모두 건너뜀

## Scope IN

- `live_trading/etf_daily_runner.py`: 이슈 1 + 이슈 3
- `live_trading/kis_adapter.py`: 이슈 2 (`get_prices()`, `get_bid_ask_prices()`)
- `live_trading/kis/_kis_api_client.py`: 이슈 2 (`_SMART_SLEEP_DEMO` 0.9→1.0, `_smart_sleep()` 제거)

## Scope OUT (Must NOT have)
- 지수 백오프 구현하지 않음
- KIS API 배치 조회 방식으로 변경하지 않음
- `_throttle()` 메커니즘 자체는 수정하지 않음 (간격 상수만 변경)
- for-loop 내 별도 `time.sleep()` 추가하지 않음 (`_throttle()` 간격으로 충분)

## Open questions

없음 — 사용자가 분석을 요청했고, 해결 방향을 지시함.

## Approval gate
status: approved
approved-at: 2026-06-23T13:00:00+09:00
