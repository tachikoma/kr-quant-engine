# etf-equal-weight-rebalancing - Work Plan

## TL;DR (For humans)

**What you'll get:** ETF 로테이션 전략의 포지셔닝 버그를 수정합니다. 2026-06-25 사례(1위 종목=10%, 2위 종목=90%)처럼 RISE 매도 자금이 TIGER 200 하나에만 쏠리던 문제를 해결합니다. 수정 후에는 모든 목표 종목(buy_list)에 예산이 균등 분배됩니다(예: KODEX 반도체 19주 53% / TIGER 200 19주 43%). 추가로 `MAX_ASSET_PCT=0.50`을 **기본값**으로 설정하여 한 종목 쏠림을 방지하고, 매도 후 재계산 시 slippage 누락 버그도 함께 수정합니다.

**Why this approach:** 기존 슬롯 기반 로직("이미 보유 중인 target은 매수 제외")은 버그입니다. Oracle 분석으로 확인했고, Metis 갭 분석에서 6개 추가 발견사항을 반영했습니다. 2줄 핵심 수정 + 안전장치로 근본 원인을 해결합니다.

**What it will NOT do:** sell-to-rebalance(과대 포지션 축소)는 하지 않습니다. 모멘텀 가중치도 도입하지 않습니다. 랭킹 점수 계산식, sell_rank_buffer, max_positions은 변경하지 않습니다.

**Effort:** Short — 코드 수정 30분, 검증 1시간
**Risk:** Low — 핵심 수정 2줄, safe mode dry-run + 백테스트 비교로 이중 검증
**Decisions to sanity-check:** (1) 기존 보유 종목에도 추가 매수 → rich-get-richer bias, MAX_ASSET_PCT=0.50 기본값으로 상한 설정, (2) `runtime_state/etf_daily_state.json` 최초 1회 삭제 필요

## Scope
### Must have
1. `etf_shared.py`: Change `buy_list` from `[t for t in targets if t not in holdings][:slots]` to `targets[:max_positions]`
2. `live_trading/etf_daily_runner.py`: Add `max_asset_pct` to `RunnerConfig`, `_read_env_config()`, and both `build_rebalance_orders()` call sites. **Default value: 0.50** (MAX_ASSET_PCT env var로 오버라이드 가능, 미설정 시 0.50 적용)
3. Standalone test script reproducing the 2026-06-25 incident (not pytest — no test infra in project)
4. Re-run single-mode backtest; compare CAGR, MDD, Sharpe, position concentration
5. Dry-run live runner in safe mode to verify plan output
6. Clear `runtime_state/etf_daily_state.json` once (prevent catch-up from replaying stale buggy orders)
7. Fix pre-existing slippage inconsistency: second call site (L1452) doesn't pass `slippage=` and defaults to 0.0005 while first call site can be 0.0

### Must NOT have (guardrails, anti-slop, scope boundaries)
- ❌ No sell-to-rebalance logic (do NOT trim overweight positions)
- ❌ No momentum-weighted sizing (do NOT use softmax/scores for allocation)
- ❌ No changes to `rank_etfs()` scoring formula
- ❌ No changes to `sell_rank_buffer`, `max_positions`, `ETF_LIST`
- ❌ No new env vars (reuse existing `MAX_ASSET_PCT`)
- ❌ No pytest dependency or test config added (standalone script only)
- ❌ No changes to `run_etf_backtest.py` code (backtest re-run only, `build_rebalance_orders()` change applies automatically)

## Known trade-offs (from Metis analysis)
1. **Rich-get-richer bias**: 이미 보유 중인 target에 추가 매수하므로, 기존 보유가 큰 종목이 더 커질 수 있음. 완전한 균등 비중을 원한다면 sell-to-rebalance가 필요하지만, 이는 모멘텀 전략의 의도(승자는 보유)와 상충. `MAX_ASSET_PCT=0.50` 기본값으로 상한 설정. Oracle 분석 결과, Option A는 새 현금 균등분배로 인해 오히려 비중이 50/50으로 **수렴**(발산 아님)함이 수학적으로 증명됨. MAX_ASSET_PCT는 가격 상승으로 인한 극단적 쏠림만 방지.
2. **Backtest 수치 변경 불가피**: 같은 버그 수정이므로 성과 지표가 변합니다. 단순 비교가 아닌 "수정 전/후"를 모두 문서로 보관.

## Verification strategy
> All verification is agent-executed. No human intervention.
- **Test decision**: Tests-after — standalone assertion script (`uv run python scripts/test_rebalance_fix.py`)
- **Evidence**: `.omo/evidence/` directory with backtest comparison CSV and test output

## Execution strategy
### Parallel execution waves
- Wave 1: Core logic fix + test + cleanup (independent — can run in parallel with Wave 2)
- Wave 2: Live runner wiring + documentation
- Wave 3: Backtest re-run & validation (tod 5 depends on Wave 1; tod 6 depends on Wave 2)
- Wave 4: Final verification (depends on all previous waves complete)

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
|---|---|---|---|
| 1. Core fix in etf_shared.py | — | 4, 5, F2 | 2 |
| 2. Live runner wiring | — | F2 | 1 |
| 3. State file cleanup | — | F1 | 1, 2 |
| 4. Test script | 1 | F1 | 2 |
| 5. Backtest re-run & compare | 1 | F3 | 2, 3, 4 |
| 6. Dry-run live runner | 2 | F3 | 4, 5 |

## Todos

> Implementation + Test = ONE todo. Never separate.
<!-- APPEND TASK BATCHES BELOW THIS LINE - never rewrite the headers above. -->

- [x] 1. **`etf_shared.py`: Fix buy-list selection from slot-based to target-based equal distribution**
  What to do / Must NOT do:
  - Line 349-350: Replace `slots = max(max_positions - len(holdings), 0)` / `buy_list = [ticker for ticker in targets if ticker not in holdings][:slots]` with `buy_list = targets[:max_positions]`
  - Note: `target_set = set(targets[:max_positions])` at line 249 is unused dead code after line 249 (always recomputed as needed). Not changing it — out of scope and harmless.
  - Line 351: Update print from `f"[주문계산] 매수 슬롯={slots}, 매수후보={buy_list}"` to `f"[주문계산] 매수 종목={buy_list} (균등분배, 종목당 약 {cash/len(buy_list):,.0f})"`
  - Must NOT change: sell logic, max_asset_pct cap logic (L386-394), scoring, target_set for sell rank buffer
  - Must NOT use softmax or momentum weighting for buy allocation
  Parallelization: Wave 1 | Blocked by: — | Blocks: 4, 5
  References: `etf_shared.py:349-350`, Oracle+Metis analysis above
  Acceptance criteria: `grep -n "buy_list" etf_shared.py` shows `buy_list = targets[:max_positions]` (not `slots`-based). Run `uv run python -c "from etf_shared import build_rebalance_orders; print('import OK')"` — no import error.
  QA scenarios:
  - Happy: `uv run python scripts/test_rebalance_fix.py` passes all assertions (requires todo 4 first)
  - Failure: `grep "slots" etf_shared.py` returns only the line count debug print (no `slots =` assignment on the buy_list line) — verify the old code is gone
  Commit: Y | `fix(etf_shared): equal-weight buy allocation across all target tickers`

- [x] 2. **`live_trading/etf_daily_runner.py`: Wire `max_asset_pct` (default=0.50) into RunnerConfig, env parsing, and both `build_rebalance_orders()` call sites + fix slippage inconsistency in second call site**
  What to do / Must NOT do:
  - **RunnerConfig** (L100-129): Add `max_asset_pct: float = 0.50` field (Oracle 권장 — 50% 단일종목 집중 상한. env var로 오버라이드 가능)
  - **_read_env_config()** (L302-332): Add parsing — read `MAX_ASSET_PCT` env var; if set and valid, use it; if unset/invalid, fallback to `0.50`
  - **First call site** (L750-768, `_build_plan()`): Add `max_asset_pct=cfg.max_asset_pct` to `build_rebalance_orders()` arguments
  - **Second call site** (L1452-1466, post-sell recalculation): Add `max_asset_pct=cfg.max_asset_pct` to `build_rebalance_orders()` arguments  
  - **Slippage fix (pre-existing bug)**: Second call site (L1452) does NOT pass `slippage=` parameter, defaulting to 0.0005. Add `slippage=(0.0 if cfg.enable_live_order and not cfg.apply_slippage_in_live else cfg.live_slippage_pct)` to match first call site's logic.
  - Must NOT change: any sell-side logic, cutoff times, polling intervals, or other params
  - Must NOT introduce new env vars (MAX_ASSET_PCT already exists, just not wired in live runner)
  Parallelization: Wave 2 | Blocked by: — | Blocks: 6
  References:
  - `live_trading/etf_daily_runner.py:100-129` (RunnerConfig), `:302-332` (_read_env_config), `:750-768` (1st call site), `:1452-1466` (2nd call site)
  - `etf_shared.py:181` (max_asset_pct param in function signature), `:386-394` (cap logic)
  - Oracle final recommendation: MAX_ASSET_PCT=0.50 as default
  - Metis finding #6: slippage inconsistency in second call site
  Acceptance criteria:
  - `grep -n "max_asset_pct" live_trading/etf_daily_runner.py` shows 4 occurrences: config field, env parsing, 1st call, 2nd call
  - `uv run python -c "from live_trading.etf_daily_runner import RunnerConfig; r = RunnerConfig(); assert r.max_asset_pct == 0.50; print('OK')"` — default is 0.50
  - `uv run python live_trading/etf_daily_runner.py` in safe mode (needs .env) — no AttributeError about max_asset_pct
  QA scenarios:
  - Happy (no env var): `MAX_ASSET_PCT="" uv run python -c "from live_trading.etf_daily_runner import _read_env_config; c = _read_env_config(); assert c.max_asset_pct == 0.50; print('default 0.50 OK')"`
  - Happy (with env var override): `MAX_ASSET_PCT=0.6 uv run python -c "from live_trading.etf_daily_runner import _read_env_config; c = _read_env_config(); assert c.max_asset_pct == 0.6; print('override 0.6 OK')"`
  - Happy (set to 0 to disable cap): `MAX_ASSET_PCT=0 uv run python -c "from live_trading.etf_daily_runner import _read_env_config; c = _read_env_config(); assert c.max_asset_pct == 0; print('disabled OK')"`
  - Failure (invalid env var): `MAX_ASSET_PCT=abc uv run python -c "from live_trading.etf_daily_runner import _read_env_config; c = _read_env_config(); assert c.max_asset_pct == 0.50; print('invalid -> default 0.50 OK')"`
  - Slippage: Verify second call site now passes `slippage=` by grep inspection
  Commit: Y | `feat(live): wire MAX_ASSET_PCT (default=0.50) into runner config and fix slippage gap in post-sell recalc`

- [x] 3. **Clear `runtime_state/etf_daily_state.json` — one-time migration to prevent stale buggy catch-up orders**
  What to do / Must NOT do:
  - Delete the file at `runtime_state/etf_daily_state.json` if it exists
  - The catch-up path (`_build_catchup_orders()`, L561-611) replays `remaining_qty` from previous orders stored in this state file. Buggy pre-fix orders would have wrong sizing. Clearing the file forces a clean state.
  - Must NOT modify `_build_catchup_orders()` code — just clear the stale data
  - Must NOT delete other files in `runtime_state/`
  Parallelization: Wave 1 | Blocked by: — | Blocks: —
  References: `live_trading/etf_daily_runner.py:561-611` (catchup orders), `:90-91` (STATE_PATH), Metis finding #2
  Acceptance criteria: `test ! -f runtime_state/etf_daily_state.json` or file is empty JSON `{}`
  QA scenarios:
  - Happy: `rm -f runtime_state/etf_daily_state.json && uv run python -c "from pathlib import Path; assert not Path('runtime_state/etf_daily_state.json').exists(); print('cleared')"`
  - Failure: file doesn't exist (already clean) — rm -f is idempotent
  Commit: Y | `chore: clear runtime state to prevent stale catch-up after rebalance fix`

- [x] 4. **Create standalone test script reproducing the 2026-06-25 incident**
  What to do / Must NOT do:
  - Create `scripts/test_rebalance_fix.py` (NOT pytest — project has no test infra)
  - Test scenario matching the incident: holdings={"091160": 3}, targets=["091160", "102110"], cash=5_850_000
  - Assertions:
    1. `len(buy_orders) == 2` (both targets get buy orders, not just the new one)
    2. `091160` buy qty > 10 (was 0 before fix, should be ~16 after fix)
    3. Estimated values of both buy orders are roughly equal (within 10% of each other)
    4. Total estimated buy value + existing position value ≈ available cash
  - Edge cases tested:
    - Empty holdings, both targets new: budget = cash / 2 (no regression)
    - Both targets held, no sells: both get budget = cash / 2 (new behavior)
    - Insufficient cash: graceful skip (qty=0)
    - max_asset_pct=0.50 with large existing position: buy capped
    - Single target (len(targets) < max_positions): budget = full cash
  - Must NOT import pytest or any test framework
  - Must NOT modify existing production code
  Parallelization: Wave 1 | Blocked by: 1 | Blocks: —
  References: `etf_shared.py:349-350` (the fix), `etf_shared.py:362-418` (buy loop), Metis finding #1 (no test infra)
  Acceptance criteria: `uv run python scripts/test_rebalance_fix.py` exits with code 0 and prints "ALL TESTS PASSED"
  QA scenarios:
  - Happy (all assertions pass): `uv run python scripts/test_rebalance_fix.py && echo "exit=$?"` → exit=0
  - Failure (fix not applied): `git stash` the fix temporarily, run test → should fail on assertion #2 (kodex qty == 0); then `git stash pop`
  - Edge case runs: test script covers all 5 edge cases listed above
  Commit: Y | `test: standalone rebalance fix test reproducing 2026-06-25 incident`

- [x] 5. **Re-run single-mode backtest and compare performance deltas**
  What to do / Must NOT do:
  - **Order is critical**: Capture "before" backtest results FIRST (while the fix is NOT yet applied), then apply the fix, then capture "after" results. Use `git stash` to temporarily revert the fix for the "before" run.
  - Compare metrics: CAGR, MDD, Sharpe Ratio, number of trades, average position concentration (calculate from `outputs_etf_only/etf_trades.csv` — note: `scripts/compute_concentration.py` reads grid output, not ETF output, so don't use it here)
  - Save both `performance_before.json` and `performance_after.json` to `.omo/evidence/`
  - Generate a comparison summary in `.omo/evidence/backtest_comparison.md`
  - Must NOT modify `run_etf_backtest.py` code
  - Must NOT change backtest parameters between runs
  Parallelization: Wave 3 | Blocked by: 1 (fix must be applied) | Blocks: F3
  References: `run_etf_backtest.py`, Metis finding #5 (no acceptance criteria for backtest)
  Acceptance criteria:
  - `test -f .omo/evidence/perf_before.json && test -f .omo/evidence/perf_after.json` — both exist
  - `.omo/evidence/backtest_comparison.md` contains: CAGR_before, CAGR_after, CAGR_delta, MDD_before, MDD_after, trade_count_before, trade_count_after, and a PASS/FAIL verdict
  - PASS verdict requires: (a) rank-1 position no longer <10% allocation at any checkpoint in the trades CSV, (b) CAGR delta is within ±3% of baseline (capping unexpected drift)
  QA scenarios:
  - Happy: comparison shows CAGR improves or stays within ±3%, MDD not significantly worse, trade count increases modestly
  - Failure: comparison shows CAGR drop >3% or MDD increase >5% — investigate before committing
  Commit: Y | `docs: backtest comparison before/after rebalance fix`

- [x] 6. **Dry-run live runner in safe mode to verify plan output**
  What to do / Must NOT do:
  - Run `uv run python live_trading/etf_daily_runner.py` (default: safe mode, no real orders)
  - Inspect the plan output: when a rebalance fires with existing holdings that are still targets, BOTH targets should appear in the buy list with roughly equal budgets
  - Save the plan output to `.omo/evidence/live_dry_run_output.txt`
  - Must NOT use `--force-live`
  - Must NOT modify any code
  Parallelization: Wave 3 | Blocked by: 2 (live runner wiring must be applied) | Blocks: F3
  References: `live_trading/etf_daily_runner.py`, `_print_plan()` at L1121
  Acceptance criteria:
  - `grep "매수 종목" .omo/evidence/live_dry_run_output.txt` shows both top-N targets listed
  - `grep "실주문 모드: OFF" .omo/evidence/live_dry_run_output.txt` confirms safe mode
  - Exit code is 0
  QA scenarios:
  - Happy: dry run completes, plan output shows equal-budget allocation for both targets
  - Failure: `grep "ERROR\|Traceback\|Error" .omo/evidence/live_dry_run_output.txt` — any errors must be resolved
  Commit: N (evidence only, no code change)

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [ ] F1. **Plan compliance audit** — grep all changed files: no old slot-based code remains, max_asset_pct wired in all 4 locations, slippage fixed in second call site, no accidental scope creep
- [ ] F2. **Code quality** — `ruff check .` passes on changed files. All prints/log messages are in Korean. No debug/test code leaked into production files.
- [ ] F3. **Real manual QA** — Run `uv run python scripts/test_rebalance_fix.py` passes all 5 test scenarios. Dry-run output shows correct allocation. Backtest delta is documented and within expected range.
- [ ] F4. **Scope fidelity** — Confirm none of "Must NOT have" items were violated. No sell-to-rebalance, no momentum weighting, no new env vars, no pytest dep added. Confirm default max_asset_pct=0.50 is set in RunnerConfig.

## Commit strategy
1. `fix(etf_shared): equal-weight buy allocation across all target tickers` — core fix
2. `feat(live): wire MAX_ASSET_PCT into runner config and fix slippage gap in post-sell recalc` — live runner wiring
3. `chore: clear runtime state to prevent stale catch-up after rebalance fix` — state cleanup
4. `test: standalone rebalance fix test reproducing 2026-06-25 incident` — test script
5. `docs: backtest comparison before/after rebalance fix` — evidence

Order: 1, 2, 3 can be in any order. 4 after 1. 5 after 1.

## Success criteria
- `build_rebalance_orders()` with holdings={"091160": 3}, targets=["091160", "102110"], cash=5.85M produces 2 buy orders with roughly equal budget
- Live runner plan output in safe mode shows both rank-1 and rank-2 in buy list with equal budget
- Backtest re-run shows rank-1 ETF consistently gets >20% allocation (was <10%)
- `MAX_ASSET_PCT` default is 0.50 in RunnerConfig; env var override works correctly
- `ruff check .` passes
- Script `uv run python scripts/test_rebalance_fix.py` exits 0
