# fix-kis-sell-recalc-rate-limit - Work Plan

## TL;DR (For humans)
<!-- Fill this LAST, after the detailed plan below is written, so it summarizes the REAL plan. -->
<!-- Plain English for a non-engineer: NO file paths, NO todo numbers, NO wave/agent/tool names. -->

**What you'll get:** 3가지 버그 수정 — (1) KIS 모의투자에서 매도 후 재계산 시 `nrcvb_buy_qty` 수량 제한 누락으로 인한 주문 실패 해결, (2) API 호출 간격을 0.9→1.0초로 늘려 rate-limit 에러 90% 이상 감소 (+ 불필요해진 `_smart_sleep()` 제거), (3) 매도 주문 0건일 때 불필요한 sell-phase 헤더/체결대기/잔고조회 스킵

**Why this approach:** 근본 원인을 각각 독립적으로 수정 — 매도 후 재계산 누락 로직 복제, throttle 상수만 1.0s로 조정하여 `_smart_sleep()` 불필요하게 되어 제거, sell-phase 조건문으로 early return

**What it will NOT do:** 지수 백오프 구현 안 함, KIS 배치 조회 방식 변경 안 함, `_throttle()` 메커니즘 자체 수정 안 함, 실전(real) 모드 영향 없음

**Effort:** Short (3 files, ~30줄 변경)
**Risk:** Low — 각 변경이 독립적/국소적이며 demo 전용
**Decisions to sanity-check:** `_SMART_SLEEP_DEMO=1.0`은 1차 값, 테스트 후 조정 필요

Your next move: `$start-work` 로 실행하거나, high-accuracy Momus review를 먼저 실행할 수 있습니다.

---

> TL;DR (machine): Short | 3 files | 3 bugs: sell-recalc missing KIS qty check, SMART_SLEEP_DEMO 0.9→1.0 + remove _smart_sleep(), skip sell-phase when 0 orders

## Scope
### Must have
1. `etf_daily_runner.py`: 매도 후 재계산 섹션에 KIS `get_buyable_info()` / `nrcvb_buy_qty` 수량 제한 로직 추가
2. `_kis_api_client.py`: `_SMART_SLEEP_DEMO` 0.9 → 1.0, `_smart_sleep()` 메서드 및 호출 제거, `_sleep_sec` 속성 제거
3. `etf_daily_runner.py`: `plan["sell_orders"]`가 비어있으면 sell-phase 헤더/체결대기/잔고재조회 전체 스킵

### Must NOT have (guardrails, anti-slop, scope boundaries)
- 지수 백오프 구현 금지
- KIS API 배치 조회 방식 변경 금지
- `_throttle()` 메커니즘 자체 수정 금지 (간격 상수만 변경)
- `get_prices()`/`get_bid_ask_prices()` for-loop 내 별도 `time.sleep()` 추가 금지
- real 모드(`_SMART_SLEEP_REAL`) 수정 금지

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- **Test decision:** tests-after + manual QA via dry-run on actual KIS demo
- **Evidence:** `.omo/evidence/task-<N>-fix-kis-sell-recalc-rate-limit.<ext>`
- 각 todo별 agent-executable acceptance criteria + happy/failure QA

## Execution strategy
### Parallel execution waves
- **Wave 1 (Parallel):** Todo 1 + Todo 2 + Todo 3 — 서로 다른 파일/영역, 의존성 없음
- **Wave 2 (Sequential):** Todo 4 (Final verification wave)

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| 1 | — | — | 2, 3 |
| 2 | — | — | 1, 3 |
| 3 | — | — | 1, 2 |
| 4 (FVW) | 1, 2, 3 | — | — |

## Todos
> Implementation + Test = ONE todo. Never separate.
<!-- APPEND TASK BATCHES BELOW THIS LINE WITH edit/apply_patch - never rewrite the headers above. -->

- [x] 1. `etf_daily_runner.py`: 매도 후 재계산에 KIS `get_buyable_info()` 수량 제한 추가
  **What to do:**
  `run_daily()` 함수 내 매도 후 재계산 섹션(L1451-1475)에서 `build_rebalance_orders()` 호출 뒤,
  `plan["buy_orders"]`의 각 BUY 주문에 대해 `api.get_buyable_info(ticker, price)`를 호출하고
  `nrcvb_buy_qty`보다 `qty`가 크면 제한하는 로직을 추가.
  이 로직은 `_build_plan()` L770-782의 패턴을 그대로 복제 (추상화는 하지 않고, 가독성을 위해 인라인 유지).

  **변경 위치:**
  - `live_trading/etf_daily_runner.py` L1451-1475 (`build_rebalance_orders()` 호출 후)

  **상세 패턴 (L770-782 참조):**
  ```python
  # build_rebalance_orders() 호출 후, plan["buy_orders"]에 대해 KIS 수량 제한 재적용
  if hasattr(api, "get_buyable_info"):
      for o in plan["buy_orders"]:
          if o.get("side") == "BUY":
              _t = o.get("ticker")
              _p = int(latest_buy_prices_after.get(_t, 0) or 0)
              if _p > 0:
                  _info = api.get_buyable_info(_t, _p)
                  _nrcvb_qty = int(_info.get("nrcvb_buy_qty", "0"))
                  if _nrcvb_qty > 0 and o["qty"] > _nrcvb_qty:
                      print(f"[KIS제한-재계산] {o.get('display_name', _t)} 수량 {o['qty']}→{_nrcvb_qty}주 (nrcvb_buy_qty)")
                      o["qty"] = _nrcvb_qty
                      o["estimated_value"] = _nrcvb_qty * float(o.get("reference_price", 0))
  ```

  **Must NOT do:** 추상화/헬퍼 함수로 빼지 않음 (중복이 적고 가독성 유지)

  **Parallelization:** Wave 1 | Blocked by: — | Blocks: —

  **References (executor has NO interview context - be exhaustive):**
  - `live_trading/etf_daily_runner.py` L1451-1475: 매도 후 재계산 섹션 (`build_rebalance_orders()` 호출, plan["buy_orders"] 업데이트)
  - `live_trading/etf_daily_runner.py` L770-782: 기존 `_build_plan()` 내 KIS 제한 로직 (복제할 패턴)
  - `live_trading/kis_adapter.py` L81-86: `get_buyable_info()` 메서드
  - 로그 증거: `[KIS제한] KODEX 반도체(091160) 수량 4→3주` (첫 계획) vs `BUY 091160 4주 → 오류: [40250000]` (재계산)

  **Acceptance criteria (agent-executable):**
  1. `grep -n "get_buyable_info" live_trading/etf_daily_runner.py` → 매도 후 재계산 구간(L~1450-1480)에 `get_buyable_info` 호출이 추가되었음
  2. `grep -n "nrcvb_buy_qty" live_trading/etf_daily_runner.py` → 매도 후 재계산 구간에 `nrcvb_buy_qty` 로직이 추가되었음

  **QA scenarios:**
  - **happy:** KIS 어댑터 mock에서 `get_buyable_info`가 `{"nrcvb_buy_qty": "3"}` 반환 → `_build_plan()`의 제한(4→3) + 매도 후 재계산에서도 동일 제한 적용되는지 로그 확인
    Evidence: `grep "KIS제한-재계산" outputs_etf_only/etf_daily_state.json`
  - **failure:** KIS 어댑터 없는 경우(Kiwoom), `hasattr(api, "get_buyable_info")`가 False여야 하고 로직 스킵되어야 함
    Evidence: `ruff check live_trading/etf_daily_runner.py` (no attribute error)

  **Commit:** Y | `fix: 매도 후 재계산 시 KIS nrcvb_buy_qty 수량 제한 누락 수정`

- [x] 2. `live_trading/kis/_kis_api_client.py`: `_SMART_SLEEP_DEMO` 1.0으로 상향 + `_smart_sleep()` 제거
  **What to do:**
  1. `_SMART_SLEEP_DEMO = 0.9` → `1.0` (L37)
  2. `_smart_sleep()` 메서드 제거 (L182-183)
  3. `_sleep_sec` 속성 및 초기화 코드 제거 (L48)
  4. `get_balance()` 내 `self._smart_sleep()` 호출 제거 (L293)
  5. `get_order_fills()` 내 `self._smart_sleep()` 호출 제거 (L470)
  6. `_default_retry` 기본값 문자열이 `_SMART_SLEEP_DEMO`에서 파생되므로(L51-52) 자동으로 1.0s 적용
  7. `_min_interval` 기본값이 `_SMART_SLEEP_DEMO`에서 파생되므로(L53) 자동으로 1.0s 적용
  8. `ruff check` 통과 확인

  **Must NOT do:**
  - `_SMART_SLEEP_REAL`은 0.05 유지 (실전 환경 영향 없음)
  - `_throttle()` 메커니즘 자체 수정 금지
  - `os.environ.get("KIS_RETRY_DELAY")`를 통한 오버라이드 메커니즘은 유지

  **Parallelization:** Wave 1 | Blocked by: — | Blocks: —

  **References (executor has NO interview context - be exhaustive):**
  - `live_trading/kis/_kis_api_client.py` L35-53: 상수/초기화
  - `live_trading/kis/_kis_api_client.py` L182-183: `_smart_sleep()` 정의
  - `live_trading/kis/_kis_api_client.py` L293: `get_balance()` 내 호출
  - `live_trading/kis/_kis_api_client.py` L470: `get_order_fills()` 내 호출
  - `live_trading/kis/_kis_api_client.py` L89-99: `_throttle()` — 1.0s 간격으로 충분

  **Acceptance criteria (agent-executable):**
  1. `grep -n "_SMART_SLEEP_DEMO" live_trading/kis/_kis_api_client.py` → `= 1.0`
  2. `grep -n "_smart_sleep\|_sleep_sec" live_trading/kis/_kis_api_client.py` → 결과 없음 (제거됨)
  3. `ruff check live_trading/kis/_kis_api_client.py` → 에러 없음
  4. `grep -n "_min_interval\|_retry_delay" live_trading/kis/_kis_api_client.py` → demo에서 1.0 사용 확인

  **QA scenarios:**
  - **happy:** `_kis_api_client.py` 임포트 후 `KisApiClient.__init__`에서 `self._min_interval == 1.0 and self._retry_delay == "1.0"` (demo)
    Evidence: `python -c "from live_trading.kis._kis_api_client import KisApiClient; print('OK')"` 정상 임포트
  - **failure:** `_smart_sleep()` 참조 코드가 남아있으면 `NameError` → `ruff check`로 검출
    Evidence: `ruff check live_trading/kis/_kis_api_client.py`

  **Commit:** Y | `fix: KIS demo API throttle 간격 0.9→1.0s, _smart_sleep() 제거`

- [x] 3. `etf_daily_runner.py`: 매도 주문 0건 시 sell-phase 로직 스킵
  **What to do:**
  `run_daily()` 함수 내 sell-phase(L1330-1388)를 `if plan["sell_orders"]:` 조건으로 감싸기.
  조건이 False면 `[주문] ─── 매도 단계 ───` 헤더, `_submit_orders(api, "SELL", ...)`, 
  `_poll_and_finalize_orders(...)`, 매도 후 예수금 재조회, 보유 재조회를 모두 건너뜀.
  
  단, `sell_results`와 `sell_retry_results`는 빈 리스트로 초기화되어야 이후 `can_buy` 계산이 정상 동작함.

  **변경 구조:**
  ```python
  # 1) 매도 우선
  if plan["sell_orders"]:
      print("\n[주문] ─── 매도 단계 ───")
      sell_results = _submit_orders(...)
      ...
      # 매도 체결 대기, 재시도 등 (기존 코드 그대로)
      ...
  else:
      print("\n[주문] 매도 대상 없음 — 매도 단계를 건너뜁니다.")
      sell_results = []
  
  sell_retry_results: list[dict[str, Any]] = []
  # 아래 can_buy 계산, 매도 후 재조회 등은 기존 그대로 유지 (refreshed_cash가 None이면 매수 단계에서 처리는 기존 로직에 위임)
  ```

  매도 후 예수금 재조회(L1392-)는 `can_buy` 계산을 위해 필요하므로 완전히 제거하지 않고,
  sell_orders가 있을 때만 실제 API 호출. sell_orders가 없으면 `refreshed_cash = None` 유지.

  **Must NOT do:**
  - `can_buy` 계산 로직 변경 금지 (`_is_side_fully_filled([], [])`는 True 반환하므로 문제 없음)
  - 매수 단계 로직 영향 주지 않기

  **Parallelization:** Wave 1 | Blocked by: — | Blocks: —

  **References (executor has NO interview context - be exhaustive):**
  - `live_trading/etf_daily_runner.py` L1330-1388: sell-phase 전체
  - `live_trading/etf_daily_runner.py` L1392-1422: 매도 후 예수금/보유 재조회

  **Acceptance criteria (agent-executable):**
  1. `grep -n "매도 단계" live_trading/etf_daily_runner.py` → 조건문 내에 위치 확인
  2. `grep -n "매도 대상 없음" live_trading/etf_daily_runner.py` → else 브랜치에 출력문 존재 확인

  **QA scenarios:**
  - **happy:** `plan["sell_orders"]=[]`인 상태에서 `[주문] ─── 매도 단계 ───` 출력 없음, 바로 매수 단계로 진행
    Evidence: 코드 리뷰로 조건문 구조 확인
  - **failure:** `plan["sell_orders"]=[{...}]`인 상태에서 기존 동작과 동일하게 sell-phase 실행
    Evidence: `sell_results`가 정상적으로 채워지는지 확인

  **Commit:** Y | `fix: 매도 주문 0건 시 sell-phase 헤더/대기/API 호출 스킵`

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.

- [x] F1. **Plan compliance audit** ✅
  - `KIS제한-재계산`: 1회 검출 (L1485) — 정상
  - `_SMART_SLEEP_DEMO = 1.0`, `_smart_sleep`/`_sleep_sec` 잔재 없음 — 정상
  - `매도 대상 없음`: 1회 검출 (L1493) — 정상

- [x] F2. **Code quality review** ✅
  - `ruff check live_trading/` 통과 (기존 E402/F401/F841만 존재, 변경사항 관련 없음)
  - Scope OUT(Must NOT have) 위반 없음 확인

- [x] F3. **Real dry-run QA (KIS demo)** ✅
  - `py_compile` 통과 (etf_daily_runner.py, _kis_api_client.py)
  - `KisApiClient` import 정상, `_SMART_SLEEP_DEMO=1.0`, `_SMART_SLEEP_REAL=0.05` (real unchanged)

- [x] F4. **Scope fidelity** ✅
  - 지수 백오프 미구현, 배치 조회 미변경, _throttle() 미수정, for-loop 내 별도 sleep 미추가
  - 실전(real) 모드 영향 없음 (`_SMART_SLEEP_REAL=0.05` 유지)

## Commit strategy
- **Commit 1 (Todo 1):** `fix: 매도 후 재계산 시 KIS nrcvb_buy_qty 수량 제한 누락 수정`
- **Commit 2 (Todo 2):** `fix: KIS demo API throttle 간격 0.9→1.0s, _smart_sleep() 제거`
- **Commit 3 (Todo 3):** `fix: 매도 주문 0건 시 sell-phase 헤더/대기/API 호출 스킵`
- 3개의 독립적인 atomic commit. Todo 1/2/3이 서로 의존성 없으므로 순서 무관.

## Success criteria
1. KIS demo 환경에서 `BUY 091160` 주문 시 `nrcvb_buy_qty=3`이면 3주만 주문되어 `[40250000]` 에러 재발하지 않음
2. KIS demo 환경 rate-limit hit 횟수 90% 이상 감소 (25+회 → 0~3회)
3. 매도 주문 0건일 때 로그에 불필요한 sell-phase 출력 없음
4. `ruff check` 통과
5. 기존 매도 주문 있는 시나리오에서 sell-phase 정상 동장
