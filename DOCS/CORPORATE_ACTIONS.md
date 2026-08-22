# ETF Corporate-Action Ledger (Phase 2B)

상위 상태 문서: [BACKTEST_INTEGRITY.md](BACKTEST_INTEGRITY.md) · [README](../README.md) ·
[OHLCV capacity](EXECUTION_CAPACITY.md)

## Scope

`etf_corporate_actions.py` provides a pure, strict ledger layer for manually
normalized corporate-action records. The default research backtest remains
unchanged. An explicit strict approval run can consume only a validated ledger
and writes to `outputs_approval/`, never `outputs_etf_only/`.

The checked-in CSV and manifest are intentionally incomplete templates. Their
incomplete coverage is an approval blocker, not evidence that no events exist.

## Normalized schema

`data/etf_corporate_actions.csv` uses these columns:

| Column | Meaning |
|---|---|
| `event_id`, `ticker`, `event_type`, `event_date` | Common required fields; ticker is exactly six digits |
| `record_date`, `ex_date`, `payment_date` | Cash-distribution dates |
| `settlement_date` | Settlement/redemption cash availability date |
| `ratio_num`, `ratio_den` | Explicit positive split ratio (`2:1`, `1:2`, etc.) |
| `cash_amount`, `currency` | Explicit positive KRW amount |
| `source_document_id`, `source_document`, `source_url`, `source_sha256` | Manifest-bound source provenance |
| `notes` | Human review context; never used to infer values |

Supported event types are `CASH_DISTRIBUTION`, `SPLIT`, `REVERSE_SPLIT`,
`SUSPENSION_START`, `SUSPENSION_END`, `DELISTING`, `CASH_SETTLEMENT`, and
`REDEMPTION`. Unknown events, duplicate IDs, invalid dates, non-KRW currency,
non-positive amounts/ratios, and malformed tickers are rejected.

## Provenance and manifest

Each approval-valid event must identify a `source_document_id` present exactly
once in the manifest, plus a nonblank URL and SHA-256. Its event URL and SHA-256
must exactly match that manifest document. If the optional legacy
`source_document` field is present, it must equal the document ID. Unknown
document IDs, duplicate manifest IDs, blank URLs, and URL/SHA mismatches are
rejected. The manifest must list the verification period,
covered six-digit tickers, every source document and hash, `coverage_status`,
and the canonical ledger SHA-256. The canonical hash is computed from sorted,
normalized event records, so event lookup/order is deterministic.

Only official KIND/issuer documents are acceptable historical evidence. No
adjusted-price discontinuity or current classification is a corporate-action
source.

## State and blockers

The pure APIs expose deterministic date/ticker lookup, distribution receivables,
record/ex-date entitlement, payment-date processing, split arithmetic, and
lifecycle states:

`ACTIVE` → `SUSPENDED` → `ACTIVE`, or `DELISTED_UNSETTLED` → `SETTLED`.
Per-ticker replay does not accept caller-created opening states. An event such
as `SUSPENSION_END` or settlement must have the required preceding lifecycle
event in this ledger.

Cash is receivable only on the documented payment date; a caller supplies any
non-trading-date mapping. No exchange calendar is inferred here. Total cost
basis is preserved through an exact split. An odd reverse split that creates an
unresolved fraction is blocked. Settlement keeps a delisted holding at zero
cash until its documented date, then pays quantity times authoritative cash,
sets quantity to zero, and marks it `SETTLED`.

Approval is blocked for empty/incomplete coverage, missing provenance/payment or
settlement, unknown events, unresolved fractions, invalid lifecycle transitions,
or a final suspended, stale, or unsettled/delisted holding. Prices are never
used to derive settlement or stale-position value.

## Strict approval execution

Run the explicit approval path with:

```bash
uv run python run_etf_backtest.py \
  --approval-strict \
  --mode single \
  --corporate-actions-ledger data/etf_corporate_actions.csv \
  --corporate-actions-manifest data/etf_corporate_actions_manifest.json \
  --approval-output-dir outputs_approval
```

The ledger and manifest are validated before KRX authentication. The checked-in
template is intentionally blocked. A blocked run writes only
`approval_report.json`, `approval_blockers.csv`, and `reproducibility.json` in
the approval output directory; it does not produce performance metrics. The
benchmark and experiment/risk-off comparison paths are unsupported in strict
approval mode.

Strict execution keeps cash distributions as receivables until payment date,
rejects orders for suspended or delisted holdings, applies explicit split
ratios, and uses authoritative settlement cash rather than a market-price
fallback. Any final stale, suspended, unsettled, or otherwise unresolved state
blocks approval.

The manifest verification period must contain the requested strategy calendar and
its verified ticker set must contain the complete strategy universe. Lifecycle
rejections are retained as `blocked_orders` diagnostics with date, ticker, intent,
state, event ID, and reason. Final approval requires a usable raw close on the
last strategy date; a last-valid-price fallback cannot certify strict approval.
