# Final Verification Wave — Complete ✅

## F1: Plan Compliance Audit
- ✅ No old slot-based code (`slots`, `매수 슬롯` removed from `etf_shared.py`)
- ✅ `max_asset_pct` wired in 4 locations (field, env parse, 2 call sites) in `live_trading/etf_daily_runner.py`
- ✅ Slippage at both call sites (2nd call site previously hardcoded 0.0005, now uses config)
- ✅ No scope creep (no sell-to-rebalance, no momentum weighting)

## F2: Code Quality
- ✅ `ruff check etf_shared.py` — clean
- ✅ `ruff check scripts/test_rebalance_fix.py` — clean
- ⚠️ `live_trading/etf_daily_runner.py` — 6 pre-existing errors (1 F841 unused `target_set` ← now removed; 5 E402 intentional for `.env` before `import pykrx`)

## F3: Real Manual QA
- ✅ Test script `scripts/test_rebalance_fix.py` — all 6 scenarios PASS
- ✅ Backtest — CAGR +5.20% (46.91% vs 41.71%), MDD stable (-22.32% vs -22.16%), Sharpe improved
- ✅ Dry-run — "매수 종목=['367760', '091160'] (균등분배)" with "실주문 모드: OFF"

## F4: Scope Fidelity
- ✅ No sell-to-rebalance logic added
- ✅ No momentum/score weighting added
- ✅ No new env vars (reuses existing `MAX_ASSET_PCT`)
- ✅ No pytest dependency (standalone assert-based test)
- ✅ Default `max_asset_pct=0.50` in RunnerConfig
