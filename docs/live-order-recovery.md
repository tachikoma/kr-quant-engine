# Live ETF order recovery — fail closed

**Status: draft / not approved for VPS deployment.** Applies to `live_trading/etf_daily_runner.py` with `order_safety.py`.

## Before deployment
1. Confirm the actual VPS commit, broker, scheduler, and effective env. The production VPS audit dated 2026-11-04 showed `develop@cf1b383` while this fix PR targets newer `main`. Do **not** pull or merge blindly.
2. Run CI and simulate accepted orders, missing acknowledgements, partial fills, cancellation timeouts, bad broker status responses, crash/restart, and two concurrent processes. Test on the actual broker adapter in **demo** where available.
3. Confirm there are no outstanding orders, executions, or stale/unknown intents before enabling live. Keep `BLOCK_LIVE_AFTER_CUTOFF=1` and never schedule `--force-live`.
4. Confirm only one execution origin (VPS, GitHub Actions, or other scheduler) can submit orders for the same account. The file lock protects **only processes sharing the same VPS filesystem**, not other hosts.

## When a run is blocked
- **Do not re-run** with `DAILY_RUN_FORCE` or `--force-live`; those do not reconcile an uncertain broker submission.
- Do not delete `runtime_state/live_order_intent.json` to make a run succeed.
- Stop new live submission through the operator's scheduler and independently check the broker's official order history and current holdings.
- Compare **account, trading date, side, ticker, quantities, broker order IDs, executed quantities, pending quantities, cancels and cash** to each journal record. Some broker APIs return `is_found=false` or 0 remaining on transient errors; this alone is **not** evidence of cancellation.
- If the broker response to a submit was lost, locate the possible order from broker order history and confirm whether it was accepted or rejected, not just its absence from one API query.
- Capture evidence (redact credentials and account IDs in shared reports). Resolve ambiguous or partially executed orders manually before any further algorithmic trades.
- With explicit operator authorization **after complete reconciliation**, archive the intent file securely outside the active path and document date, run ID, order IDs, quantity, and final outcome. Do not build an automatic deletion or reset procedure.
- Restart in no-order/demo mode first; verify strategy holdings, cash, state and live cutoff settings before any approved live resumption.

## Behavior change
- Every broker order is preceded by a durable `UNKNOWN` marker. A successful acknowledgement stores the broker order ID. Abnormal responses leave the marker blocking future live runs.
- Automatic retries are allowed only if the original order cancellation is broker-confirmed and the unfilled quantity is known; submission of a cancellation request is not confirmation.
- Completed runs clear markers only when every accepted order is broker-verified as fully filled or terminally cancelled; partial/unverified outcomes require reconciliation.
- The runner lock serializes processes on one filesystem. A run halted before final state saving may leave a marker intentionally.
- By default a live submission past per-side cutoff is blocked even if the old environment has `BLOCK_LIVE_AFTER_CUTOFF=0`. Explicit `--force-live` remains a hazardous manual override, not a recovery tool.

## Remaining verification
- This is a first fail-closed layer, **not** a full broker order ledger or cross-host idempotency guarantee.
- Broker-native terminal status semantics and balance reconciliation require adapter-specific end-to-end tests; do not claim production safety based on offline tests alone.
- Strategy profitability, `ENABLE_MULTI_INDEX_RISK` and benchmark comparability are separate approval gates.
