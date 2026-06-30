# fix-kiwoom-cash-rate-limit - Work Plan

## TL;DR (For humans)
<!-- Fill this LAST -->

**What you'll get:** Kiwoom API 실전/모의투자에서 증거금 부족(RC4025)과 429 rate limiting 문제를 해결합니다. 매도 후 재조회된 보유종목에서 이미 매도한 종목을 자동 제외하고, API 호출 간격을 실전 0.1초/모의 0.6초로 조정하며, 네트워크 오류 시 지수 백오프를 적용합니다.

**Why this approach:** `cash += estimated_value`를 삭제하지 않고 **매도완료 종목을 2차 조회에서 제외**하는 것이 백테스트를 안전하게 유지하면서 근본 원인(D+2 미결제 종목 재진입 → 현금 이중가산)을 해결합니다. Throttle은 KIS 구현을 참조하여 실전/모의를 분기합니다.

**What it will NOT do:** KIS 어댑터를 변경하지 않습니다. 백테스트 로직을 변경하지 않습니다. async/await 리팩터링을 하지 않습니다. 새 env 변수를 추가하지 않습니다.

**Effort:** Medium
**Risk:** Low — backtest unaffected, live change is additive+defensive, throttle only affects timing
**Decisions to sanity-check:** (1) `get_available_cash()`가 hardcoded `qry_tp=3` (추정예수금) 사용 (2) 모의 throttle 0.6초 (한계 2/sec 대비 17% 여유)

Your next move: approve, or run a high-accuracy review first. Full execution detail follows below.

---

> TL;DR (machine): Medium | Low | 5 todos: add get_available_cash() + sold-ticker filter + ENV_MODE throttle + exp backoff + verification

## Scope
### Must have
1. `kiwoom_adapter.py`: Add `get_available_cash()` with hardcoded `qry_tp=3`, parse `ord_alow_amt` (same path as `get_cash()`)
2. `etf_daily_runner.py`: Before 2nd `build_rebalance_orders` call (line 1455), filter `refreshed_holdings` to remove tickers in `plan["sell_orders"]`
3. `kiwoom_adapter.py`: Change throttle default to ENV_MODE-based (real=0.1s, demo=0.6s), unify interval+retry, existing env vars override
4. `kiwoom_adapter.py`: Network errors → exponential backoff (`delay × 2^attempt`, cap 10s), 429 → fixed delay, API rate limit (return_code=5) → fixed delay
5. Verification: backtest identical output + live dry-run no crash + interval logging

### Must NOT have (guardrails, anti-slop, scope boundaries)
- Do NOT remove `cash += float(estimated_value)` from `etf_shared.py:313`
- Do NOT change KIS adapter (`live_trading/kis_adapter.py` or `live_trading/kis/`)
- Do NOT change backtest flow in `run_etf_backtest.py`
- Do NOT add new env vars for throttle (reuse `ENV_MODE` + existing `KIWOOM_HTTP_MIN_INTERVAL`, `KIWOOM_HTTP_RETRY_DELAY`)
- Do NOT refactor to async/await — keep `time.sleep` synchronous
- Do NOT change `_post()` signature or external interface
- Do NOT change `_DEFAULT_QRY_TP["KIWOOM_CASH"]` default — `get_available_cash()` uses its own hardcoded `qry_tp=3`

## Verification strategy
> Zero human intervention — all verification is agent-executed.
- **Test decision**: tests-after (no pytest infra exists; verify via backtest equivalence + dry-run invocation + log inspection)
- **Evidence**: `.omo/evidence/`

## Execution strategy
### Parallel execution waves
- Wave 1 (todos 1-2): Independent — `get_available_cash()` and sold-ticker filter have no code dependency
- Wave 2 (todos 3-4): kiwoom_adapter.py throttle refactoring — sequential (init then _post)
- Wave 3 (todo 5): Verification — after all changes

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| 1. get_available_cash | — | — | todo 2 |
| 2. sold-ticker filter | — | — | todo 1 |
| 3. throttle init | — | todo 4 | todo 1, 2 |
| 4. retry logic refactor | todo 3 | — | — |
| 5. verification | todos 1-4 | — | — |

## Todos
> Implementation + Test = ONE todo. Never separate.
<!-- APPEND TASK BATCHES BELOW THIS LINE WITH edit/apply_patch - never rewrite the headers above. -->
- [ ] 1. Add `get_available_cash()` to KiwoomAdapter
  **What to do**: Add `def get_available_cash(self) -> float` method to `KiwoomAdapter` in `kiwoom_adapter.py`. It must call the same endpoint (`/api/dostk/acnt`) and api_id (`kt00001`) as `get_cash()`, but with hardcoded `qry_tp=3` (추정예수금). Parse `ord_alow_amt` from response (same path as `get_cash()`). Signature: no arguments, returns float.
  **Must NOT do**: Do NOT modify `_build_account_payload()` or `_DEFAULT_QRY_TP`. Do NOT change `get_cash()` behavior. Do NOT add `ticker`/`price` params (unlike KIS — Kiwoom doesn't need per-ticker buyable info).
  **Implementation details**:
  ```python
  def get_available_cash(self) -> float:
      """추정예수금(D+2 매도대금 포함)을 반환한다. KIS duck-type 호환."""
      endpoint = "/api/dostk/acnt"
      api_id = "kt00001"
      # 직접 payload 구성 (hardcoded qry_tp=3, _build_account_payload 미사용)
      payload: dict[str, Any] = {}
      if self.account_no:
          payload["account_no"] = self.account_no
      payload["qry_tp"] = "3"
      data = self._post(endpoint, payload, api_id)
      self._raise_on_api_error(data, context="get_available_cash")
      path = "ord_alow_amt"
      value = self._get_by_path(data, path)
      if value is None:
          raise RuntimeError(f"Available cash response path not found: path={path}, top_keys={list(data.keys())}")
      return self._to_number(value)
  ```
  **Parallelization**: Wave 1 | Blocked by: — | Blocks: —
  **References**: `kiwoom_adapter.py:473-484` (`get_cash()` — copy/modify pattern), user confirmed `ord_alow_amt` is valid for `qry_tp=3`, `kis_adapter.py:75-79` (KIS signature reference)
  **Acceptance criteria** (agent-executable):
  - Run: `python -c "import ast; code=open('live_trading/kiwoom_adapter.py').read(); tree=ast.parse(code); [print(f.name) for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name=='get_available_cash']"` → prints "get_available_cash"
  - Run: `grep -n "def get_available_cash" live_trading/kiwoom_adapter.py` → returns line number
  - Run: `grep -A5 "def get_available_cash" live_trading/kiwoom_adapter.py | grep "qry_tp.*3"` → confirms qry_tp=3
  - Run: `ruff check live_trading/kiwoom_adapter.py` → no errors
  **QA scenarios**:
  - Happy: `grep -A15 "def get_available_cash" live_trading/kiwoom_adapter.py` → confirms method has `qry_tp = "3"` and `raise RuntimeError` guard
  - Failure: `grep -A15 "def get_available_cash" live_trading/kiwoom_adapter.py | grep "raise RuntimeError"` → confirms RuntimeError is raised when value is None
  - Evidence: `.omo/evidence/task-1-get-available-cash.txt`
  **Commit**: Y | `fix(kiwoom): add get_available_cash() with qry_tp=3 for estimated cash`

- [ ] 2. Add sold-ticker filter before 2nd build_rebalance_orders call
  **What to do**: In `live_trading/etf_daily_runner.py`, between the `refreshed_holdings` assignment (line 1423) and the `price_tickers` construction (line 1428), add logic to exclude tickers that were already sold in the current cycle. The sold-ticker set comes from `plan.get("sell_orders", [])` (from 1st `_build_plan()` call at line 787).
  **Implementation** (insert after line 1423's `print`, before line 1428):
  ```python
              # C3: D+2 미결제로 Kiwoom이 아직 반환하는 매도완료 종목을 제외
              sold_this_cycle = {o["ticker"] for o in plan.get("sell_orders", []) if o.get("ticker")}
              if sold_this_cycle:
                  _before_sold = len(refreshed_holdings)
                  refreshed_holdings = {t: q for t, q in refreshed_holdings.items() if t not in sold_this_cycle}
                  if len(refreshed_holdings) < _before_sold:
                      print(f"[방어] D+2 미결제 매도종목 {_before_sold - len(refreshed_holdings)}개를 보유에서 제외했습니다.")
  ```
  **Must NOT do**: Do NOT modify `plan` dict. Do NOT remove sell_orders from plan. Do NOT add this filter to the 1st `build_rebalance_orders` call (line 752) — only 2nd call.
  **Parallelization**: Wave 1 | Blocked by: — | Blocks: —
  **References**: `etf_daily_runner.py:1415-1427` (refreshed_holdings after sell), `:787` (sell_orders from 1st plan), `:1455` (2nd build_rebalance_orders call)
  **Acceptance criteria** (agent-executable):
  - Run: `grep -n "sold_this_cycle\|계산에서 제외" live_tracking/etf_daily_runner.py` (or `live_trading/etf_daily_runner.py`) → confirms filter code exists
  - Run: `ruff check live_trading/etf_daily_runner.py` → no errors
  **QA scenarios**:
  - Happy: Backtest `uv run python run_etf_backtest.py --end 20240101` exits 0 and `outputs_etf_only/performance.json` has `cagr > 0`
  - Failure: Run with `BROKER_TYPE=KIWOOM LIVE_ORDER_ENABLED=0` + dry-run → verify log shows excluded tickers count
  - Evidence: `.omo/evidence/task-2-sold-filter.txt`
  **Commit**: Y | `fix(daily-runner): filter already-sold tickers from 2nd build_rebalance_orders to prevent cash double-count`

- [ ] 3. Refactor KiwoomAdapter throttle initialization with ENV_MODE-based defaults
  **What to do**: In `kiwoom_adapter.py:__init__` (lines 80-82), change the default values of `http_min_interval` and `http_retry_delay` based on `ENV_MODE`. The env var override pattern: `ENV_MODE` sets the default, but if `KIWOOD_HTTP_MIN_INTERVAL` or `KIWOOM_HTTP_RETRY_DELAY` is explicitly set in env, that overrides. Both values use the same default (unified).
  **Implementation** (replace lines 80-82):
  ```python
      # Rate limit defaults by ENV_MODE: 실전=20/sec(0.05s), 모의=2/sec(0.5s)
      # We use 실전=0.1s(10/sec, 50% margin), 모의=0.6s(1.67/sec, 17% margin)
      _env_mode = os.environ.get("ENV_MODE", "real").lower()
      _default_interval = 0.1 if _env_mode == "real" else 0.6
      self.http_min_interval = float(os.environ.get("KIWOOM_HTTP_MIN_INTERVAL", str(_default_interval)))
      # Retry delay unified with throttle interval (same value)
      self.http_retry_delay = float(os.environ.get("KIWOOM_HTTP_RETRY_DELAY", str(self.http_min_interval)))
      # Keep existing env var references for backward compat
      self.http_max_retries = int(os.environ.get("KIWOOM_HTTP_MAX_RETRIES", "4"))
  ```
  Also remove the now-redundant `api_rate_limit_retry_delay` line (line 100) since retry delay is now the unified value:
  ```python
      # (removed) self.api_rate_limit_retry_delay = self.http_retry_delay
  ```
  Update `_retry_delay()` and `_is_api_rate_limited()` usage in `_post()` to use `self.http_retry_delay` directly (they already do).
  **Must NOT do**: Do NOT change `_throttle_request()` method — it already uses `self.http_min_interval` correctly. Do NOT add new env vars.
  **Parallelization**: Wave 2 | Blocked by: — | Blocks: todo 4
  **References**: `kiwoom_adapter.py:80-82` (current defaults), `kiwoom_adapter.py:70-74` (env_mode), `live_trading/kis/_kis_api_client.py` (reference pattern for real/demo throttling)
  **Acceptance criteria** (agent-executable):
  - Run: `ENV_MODE=demo KIWOOM_HTTP_MIN_INTERVAL="" KIWOOM_HTTP_RETRY_DELAY="" python -c "import os; os.environ['ENV_MODE']='demo'; exec(open('live_trading/kiwoom_adapter.py').read().split('class Ki')[0]); from live_trading.kiwoom_adapter import KiwoomAdapter; k=KiwoomAdapter.__new__(KiwoomAdapter); k.http_min_interval=0.6; print('demo ok')"` — this won't work well. Instead:
  - Create inline test: `ENV_MODE=demo python -c "from live_trading.kiwoom_adapter import KiwoomAdapter; print('skip init')"` — skip (needs API keys). Better: grep for logic
  - Run: `grep -A3 "http_min_interval" live_trading/kiwoom_adapter.py | head -10` → confirms `_default_interval = 0.1 if _env_mode == \"real\" else 0.6`
  - Run: `grep "http_retry_delay" live_trading/kiwoom_adapter.py | head -5` → confirms it references `self.http_min_interval` for default
  - Run: `grep -n "api_rate_limit_retry_delay" live_trading/kiwoom_adapter.py` → either NOT FOUND or only in comments
  - Run: `ruff check live_trading/kiwoom_adapter.py` → no errors
  **QA scenarios**:
  - Happy: Inspect code for the ENV_MODE branch pattern (grep-based)
  - Failure: Check that if `KIWOOD_HTTP_MIN_INTERVAL=0.05` is set, it overrides ENV_MODE default (code inspection)
  - Evidence: `.omo/evidence/task-3-throttle-init.txt`
  **Commit**: Y | `fix(kiwoom): ENV_MODE-based throttle defaults (real=0.1s, demo=0.6s) with unified retry delay`

- [ ] 4. Add exponential backoff for network errors and separate retry logic
  **What to do**: In `kiwoom_adapter.py:_post()` (lines 186-245), modify the three retry paths:
  - **Network error** (ConnectionError, Timeout): Change from fixed `self.http_retry_delay` to exponential backoff (`delay = self.http_retry_delay * (2 ** attempt)`, cap at 10s max)
  - **429**: Keep fixed delay from `_retry_delay()` (already reads Retry-After header). No change needed.
  - **API rate limit** (return_code=5): Keep fixed `self.http_retry_delay`. No change needed.
  - **Throttle timestamp update**: Move `_throttle_lock` update to AFTER each wait/sleep, not just after successful response (to prevent back-to-back retries from violating interval)
  **Implementation** — modify `_post()`:
  In the network error handler (lines 194-203), change:
  ```python
      except (requests.ConnectionError, requests.Timeout) as e:
          if attempt < self.http_max_retries:
              delay = min(self.http_retry_delay * (2 ** attempt), 10.0)  # exponential backoff, max 10s
              if self.http_debug_response:
                  print(
                      f"[HTTP][재시도] 네트워크 오류 ({type(e).__name__}) "
                      f"-> {delay:.1f}초 대기 후 재시도 (attempt {attempt+1}/{self.http_max_retries})"
                  )
              time.sleep(delay)
              continue
          raise RuntimeError(f"HTTP request failed (network error) after {self.http_max_retries} retries: {url}") from e
  ```
  Move the `_throttle_lock` timestamp update to update `_last_request_ts` WITHIN each retry path too (after the sleep, before continuing), so that retry waits are counted in the interval.
  Actually, the current code updates `_last_request_ts` at line 206-207 AFTER every response (even error responses). But for network errors, it doesn't update because the exception bypasses lines 205-207. So we need to add the update in the network error handler too, after the sleep, before `continue`.
  ```python
              time.sleep(delay)
              with self._throttle_lock:
                  self._last_request_ts = time.monotonic()  # count retry as a request
              continue
  ```
  Also add a THROTTLE update for the 429 path after its sleep (line 219):
  ```python
              time.sleep(self._retry_delay(response))
              with self._throttle_lock:
                  self._last_request_ts = time.monotonic()  # 429 retry counts
              continue
  ```
  And for API rate limit (return_code=5) after line 238:
  ```python
              time.sleep(wait_sec)
              with self._throttle_lock:
                  self._last_request_ts = time.monotonic()  # API rate limit retry counts
              continue
  ```
  **Must NOT do**: Do NOT change the 429 or API rate-limit retry delays (keep fixed). Do NOT change `_retry_delay()` method. Do NOT add configurable max_delay env var.
  **Parallelization**: Wave 2 | Blocked by: todo 3 | Blocks: —
  **References**: `kiwoom_adapter.py:186-245` (full _post method), `live_trading/kis/_kis_api_client.py` (reference: `attempt_count` and `delay * multiplier` pattern)
  **Acceptance criteria** (agent-executable):
  - Run: `grep "2 \*\* attempt\|exponential" live_trading/kiwoom_adapter.py` → confirms exp backoff pattern
  - Run: `grep "min.*10\.0" live_trading/kiwoom_adapter.py` → confirms max_delay=10 cap
  - Run: `grep -c "_last_request_ts = time.monotonic()" live_trading/kiwoom_adapter.py` → count ≥ 4 (success + network + 429 + api_limit)
  - Run: `ruff check live_trading/kiwoom_adapter.py` → no errors
  **QA scenarios**:
  - Happy: Code inspection confirms backoff pattern, separate retry types, throttle timestamp updates in all paths
  - Failure: Check that network error retries do NOT use fixed delay anymore (grep for `self.http_retry_delay` in `except` block)
  - Evidence: `.omo/evidence/task-4-exp-backoff.txt`
  **Commit**: Y | `fix(kiwoom): exponential backoff for network errors, separate retry types, throttle timestamp in all retry paths`

- [ ] 5. Verification — backtest equivalence + live dry-run + throttle logging
  **What to do**: Run three verifications:
  A. **Backtest equivalence**: Run `uv run python run_etf_backtest.py --end 20240101` and capture output/performance to `.omo/evidence/`. Compare with baseline from commit `15ed87a` (run backtest with current HEAD first, then verify after changes produce identical results).
  B. **Live dry-run**: Run with demo credentials (KiwoomAdapter unconditionally calls `_issue_token()` at init, so `.env` must have `KIWOOM_APPKEY`/`KIWOOM_SECRETKEY` for demo mode). Use `ENV_MODE=demo BROKER_TYPE=KIWOOM LIVE_ORDER_ENABLED=0 uv run python live_trading/etf_daily_runner.py`. Verify exception-free execution and log presence of sold-ticker filter messages. Note: token issuance is the ONLY API call made — no order placement occurs because `LIVE_ORDER_ENABLED=0`.
  C. **Throttle logging**: Verify via grep that ENV_MODE-based interval is applied in code, and confirm all four throttle timestamp updates exist.
  **Must NOT do**: Do NOT set `LIVE_ORDER_ENABLED=1`. Do NOT modify test files.
  **Parallelization**: Wave 3 | Blocked by: todos 1-4 | Blocks: —
  **References**: `kiwoom_adapter.py:110` (unconditional _issue_token), AGENTS.md (backtest commands)
  **Acceptance criteria** (agent-executable):
  - `uv run python run_etf_backtest.py --end 20240101 2>&1` exits 0, `outputs_etf_only/performance.json` exists and has CAGR > 0
  - `ruff check .` → no errors (only existing warnings allowed)
  - Code inspection confirms no unintended modifications outside scope
  **QA scenarios**:
  - Happy: Backtest completes, performance.json is valid JSON with expected keys
  - Failure: Backtest fails → check for Python/import errors first
  - Evidence: `.omo/evidence/task-5-verification.txt`
  **Commit**: N — verification only, no code changes

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [ ] F1. Scope fidelity: `git diff --name-only` shows ONLY files from scope IN: `live_trading/kiwoom_adapter.py`, `live_trading/etf_daily_runner.py`. No other files modified.
- [ ] F2. Code quality: `ruff check .` passes with zero new errors (only pre-existing warnings allowed, verify with `git stash && ruff check . && git stash pop`).
- [ ] F3. Must-NOT-Have compliance: grep diff for all scope OUT items — no async/await introduced, no KIS adapter changes, no `etf_shared.py` line 313 changes, no new env vars for throttle.
- [ ] F4. Backtest integrity: run `uv run python run_etf_backtest.py --end 20240101` → exits 0, `outputs_etf_only/performance.json` has CAGR > 0.

## Commit strategy
4 commits (one per todo 1-4, atomic), no commit for todo 5 (verification only). Commits go on current branch (main). Use conventional commit format: `fix(scope): message`.

## Success criteria
1. `get_available_cash()` exists in KiwoomAdapter with hardcoded `qry_tp=3` — no side effects on `get_cash()`
2. `etf_daily_runner.py` 2nd call filters out `plan["sell_orders"]` tickers from `refreshed_holdings`
3. Throttle defaults: real=0.1s, demo=0.6s with env var override
4. Network errors use `http_retry_delay × 2^attempt (max 10s)`, 429 uses fixed delay
5. `_last_request_ts` updated in ALL retry paths (4 locations)
6. Backtest identical output vs baseline (`15ed87a`)
7. Dry-run completes without exception
