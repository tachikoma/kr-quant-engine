# History

Key completed changes to this codebase. See individual commits for full detail.

## 2026-06 — ETF equal-weight rebalancing fix

**Problem:** Slot-based `buy_list` (`targets not in holdings`) excluded already-held target tickers from buy allocation, causing the entire budget to flow to a single new ticker (e.g., rank-2 got 90%, rank-1 got only 10%).

**Changes:**
- `etf_shared.py`: `buy_list = targets[:max_positions]` (equal distribution across all rank-N targets, regardless of current holdings)
- `live_trading/etf_daily_runner.py`: Wired `MAX_ASSET_PCT=0.50` default into RunnerConfig + both `build_rebalance_orders()` call sites
- Fixed pre-existing slippage inconsistency: 2nd call site now passes `slippage=` parameter instead of defaulting to 0.0005

**Performance (single mode, 5bp slippage, 2021–2026):**

| Metric | Before | After | Δ |
|---|---|---|---|
| CAGR | 41.71% | 46.91% | +5.20% |
| MDD | -22.16% | -22.32% | -0.16% |
| Sharpe | 1.43 | 1.50 | +0.07 |
| Trade count | 84 | 116 | +32 |

**Files:** `etf_shared.py`, `live_trading/etf_daily_runner.py`, `scripts/test_rebalance_fix.py`

---

## 2026-06 — Kiwoom cash double-count & rate-limit fixes

**Problem:** D+2 unsettled sell proceeds caused Kiwoom to return already-sold tickers in the 2nd holdings refresh, leading to cash double-counting. Real/demo throttling was not differentiated (same 0.1s interval for both), causing 429 errors in demo.

**Changes:**
- `kiwoom_adapter.py`: Added `get_available_cash()` with hardcoded `qry_tp=3` (추정예수금)
- `etf_daily_runner.py`: Filter sold tickers from 2nd `build_rebalance_orders()` input
- `kiwoom_adapter.py`: ENV_MODE-based throttle defaults (real=0.1s, demo=0.6s)
- `kiwoom_adapter.py`: Exponential backoff for network errors (2^attempt × delay, cap 10s), distinct retry handling per error type

**Files:** `live_trading/kiwoom_adapter.py`, `live_trading/etf_daily_runner.py`

---

## 2026-06 — KIS sell-recalc qty limit & demo throttle fix

**Problem:** After sell recalculation, `nrcvb_buy_qty` limit was not re-applied, causing `[40250000]` order failures in KIS demo. Demo API throttle (0.9s) was insufficient, causing 25+ rate-limit hits per cycle.

**Changes:**
- `etf_daily_runner.py`: Applied `get_buyable_info()` / `nrcvb_buy_qty` cap in post-sell recalculation (same logic as initial plan)
- `_kis_api_client.py`: Demo throttle 0.9s → 1.0s; removed now-redundant `_smart_sleep()` method and `_sleep_sec` attribute
- `etf_daily_runner.py`: Skip sell-phase entirely when `plan["sell_orders"]` is empty

**Files:** `live_trading/etf_daily_runner.py`, `live_trading/kis/_kis_api_client.py`
