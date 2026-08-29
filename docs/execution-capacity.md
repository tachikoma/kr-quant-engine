# Phase 3A: OHLCV Capacity Scenario

상위 상태 문서: [BACKTEST_INTEGRITY.md](BACKTEST_INTEGRITY.md) · [README](../README.md) ·
[기업행위 ledger](CORPORATE_ACTIONS.md)

## Scope

`etf_execution.py` is an isolated, dependency-free decision layer. It accepts
one requested BUY or SELL quantity and the next trading date's OHLCV bar. It
does not change the legacy strategy, order generation, accounting, or output
files. Phase 3A is a diagnostic scenario, not historical executable-fill
reconstruction.

Every decision is labelled `OHLCV_CAPACITY_SCENARIO`. A `FULL` result means
only that the requested quantity fits the selected bar-volume capacity. It is
not evidence that an order was submitted, queued, matched, or filled.

## Policy

- Default maximum participation is 5% of positive bar volume.
- Capacity is `floor(volume * participation_rate)`.
- Filled quantity is bounded by requested quantity and capacity.
- A partial remainder can be carried to exactly one following trading date;
  multi-day carry is rejected by the public API.
- A partial remainder on that carry date is cancelled; a later carry age is
  `CARRY_EXPIRED`.
- On the final carry date, zero volume, missing open, explicit suspension, and
  possible-lock diagnostics become `CARRY_CANCELLED` with the remainder and
  cancellation reason preserved.
- Carry state contains quantity, age, and `PARTIAL_CARRY` status only. Later
  strategy integration must
  price and cost each new date independently; no prior fill price is reused.
- A public carry state is only a positive `PARTIAL_CARRY` remainder at age one;
  age zero, age two, non-partial status, and non-positive quantities are invalid.
- An explicit suspension is authoritative and has priority over bar checks.
- Equal positive OHLC with zero volume is classified as
  `POSSIBLE_LIMIT_LOCK`. This is a conservative heuristic, not a confirmed
  KRX limit lock.

The participation rate is caller-selectable between 0 and 100%, with 5% as the
default. A missing open (`None`) produces `MISSING_OPEN`; no fallback price is
used. Non-finite or non-positive prices, impossible OHLC ranges, mismatched
date/ticker fields, invalid volume, and invalid authoritative value inputs are
rejected before a decision is produced. Zero-volume bars are diagnostic
outcomes, not proof that a market was halted.

`bar_value` is populated only when the source supplies an authoritative value.
When it is absent, `close_volume_notional_estimate` is reported separately as
`close * volume`; it is not labelled as actual trading value.

## OHLCV and KRX limitations

Daily OHLCV can show volume, trading value, missing opens, and price patterns.
It cannot establish queue priority, order-book depth, actual fill timing,
unfilled orders, VI occurrence, the side of a price lock, or the fact of a
halt. KRX price-limit behavior depends on the product and reference-price
rules; equal OHLC alone is insufficient to prove a lock.

The layer is therefore suitable for sensitivity and capacity diagnostics only.
It must not be used to claim historical executable-fill accuracy or to approve
live order sizing.

## Required future data for execution claims

An execution-realism integration would need timestamped order-book depth and
quotes, order submission and acknowledgement times, exchange halt/VI and
price-limit records, broker order status, cancellations, and a documented
matching/queue model. It would also need explicit assumptions for market impact,
fees, spread, and partial-fill pricing.

## Phase 3B2 runner scenario

The runner exposes a diagnostic-only CLI without changing the default legacy
backtest:

```bash
uv run python run_etf_backtest.py --mode single \
  --execution-mode ohlcv_capacity \
  --execution-participation-rate 0.05 \
  --execution-aum 10000000,100000000,1000000000 \
  --execution-output-dir outputs_execution
```

Each AUM is run from a fresh strategy state. The selected directory receives
only `execution_summary.csv`, `execution_diagnostics.csv`,
`execution_trades.csv`, `execution_reconciliation.csv`, and
`execution_metadata.json`. These files are explicitly marked
`diagnostic_only=true`, `executable_fill_claim=false`, and
`orderbook_used=false`; they are not broker or historical execution records.
The output path cannot overlap `outputs_etf_only/` or `outputs_approval/`, and
capacity mode is rejected together with `--approval-strict` or non-single
backtest modes.
