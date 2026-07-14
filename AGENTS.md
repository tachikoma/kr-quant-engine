# AGENTS.md — kr-quant-engine

## Project overview

Korean ETF rotation backtest + daily live runner. Python 3.11, single-module layout (no monorepo). Uses `pykrx` for KRX OHLCV data.

## Key entrypoints

- `run_etf_backtest.py` — primary backtest (1800+ lines, procedural). Config via env vars. CLI: `--start`, `--end`, `--mode`. `run_etf_strategy()` supports optional `rebalance_observer` callback for diagnostics.
- `live_trading/etf_daily_runner.py` — daily live order runner (default: safe mode, no real orders). CLI: `--force-live`.
- `live_trading/kiwoom_adapter.py` — Kiwoom REST API adapter for live brokerage.
- `live_trading/kis_adapter.py` — Korea Investment & Securities (KIS) REST API adapter.
- `live_trading/kis/` — KIS API client/auth package (`KisApiClient`, `KisAuthManager`).
- `live_trading/telegram_notifier.py` — Telegram order/execution notification.
- Shared strategy logic: `etf_shared.py` (ETF_LIST, fees, ranking, order building).
- `etf_distributions.py` — ETF 현금분배금 CSV 로드 및 total-return 수익률 계산.
- `strategy_freeze.py` — 전략 동결 스냅샷 생성/검증 유틸리티. `strategy_freeze.json`과 함께 사용.
- Analysis scripts: `scripts/` (27 scripts). Key: `grid_backtest.py`, `correlation_analysis.py`, `apply_cap_and_retest.py`, `walk_forward_validation.py`, `parameter_stability.py`, `trade_performance_attribution.py`, `check_strategy_freeze.py`, `analyze_current_drawdown.py`.

## Commands

```
uv sync                     # install dependencies (uv, not pip)
uv run python run_etf_backtest.py              # single-mode backtest (add --start/--end/--mode)
uv run python live_trading/etf_daily_runner.py  # daily runner (safe mode)
uv run python live_trading/etf_daily_runner.py --force-live  # bypass safety cutoff
uv run scripts/grid_backtest.py                 # grid backtest (candidate pool / rebalance / positions)
uv run scripts/correlation_analysis.py          # drawdown correlation analysis
uv run scripts/walk_forward_validation.py       # rolling walk-forward parameter selection + OOS eval
uv run scripts/parameter_stability.py           # parameter neighborhood sensitivity check
uv run scripts/trade_performance_attribution.py # FIFO trade P&L attribution (cost-aware)
uv run scripts/check_strategy_freeze.py         # strategy freeze drift + OOS performance
uv run scripts/analyze_filter_frequency.py       # risk_on + 후보 0개 빈도 분석
uv run scripts/analyze_zero_candidate_impact.py  # 후보 0개 사건 포트폴리오 영향 분석
uv run scripts/analyze_current_drawdown.py       # 현재 MDD 기여도·리밸런싱 이력
uv run scripts/<script>.py                      # any analysis script
ruff check .                # lint (ruff only, no mypy/pytest config)
```

## Critical conventions

- **Only `ruff` for linting** — no mypy, no pytest, no pre-commit. Set `line-length=100`.
- **`.env` must be loaded before `import pykrx`** — the token expires otherwise. Both entrypoints call `load_dotenv()` at module level before the `from pykrx import stock` line.
- **Safety first** — `live_trading/etf_daily_runner.py` defaults to `LIVE_ORDER_ENABLED=0`. Live orders require `--force-live` CLI flag.
- **Env-var-driven config** — backtest mode (`ETF_BACKTEST_MODE=single|experiment`), slippage (`ETF_BASE_SLIPPAGE=0.0005`), slippage units accept `"5bp"`, `"0.5%"`, or `"0.0005"` via `config_utils.parse_pct_env()`.
- **BROKER_TYPE=KIWOOM|KIS** — selects live brokerage adapter (default KIWOOM).
- **TAXABLE_ETF_TICKERS** — 8 tickers subject to 15.4% dividend tax on trading gains.

## Key env vars

| Var | Default | Purpose |
|---|---|---|
| `ETF_BACKTEST_MODE` | `single` | `experiment` runs 4 slippage scenarios (5/10/20/30bp) |
| `ETF_BASE_SLIPPAGE` | `0.0005` | Backtest slippage fraction |
| `ETF_SPREAD_PCT` | `0.0005` | Bid-ask spread |
| `ETF_ENABLE_BENCHMARK` | `1` | Include KODEX200 comparison in single mode |
| `MAX_ASSET_PCT` | `0.50` | Per-asset position cap. `0` = unlimited |
| `ETF_USE_CACHE` | `1` | Use parquet data cache |
| `ETF_REFRESH_CACHE` | `0` | Force refetch data |
| `LIVE_ORDER_ENABLED` | `0` | Live order toggle (0=safe mode) |
| `BROKER_TYPE` | `KIWOOM` | Broker choice: `KIWOOM` or `KIS` |
| `LIQUIDATE_ON_RISK_OFF` | `1` | risk_off 시 전량 매도(1) vs 보유 유지(0) |
| `MIN_AVG_TRADING_VALUE` | `1000000000` | Min avg daily trading value (KRW) for ETF liquidity filter |
| `ETF_RETURN_BASIS` | `price` | `nav` uses NAV as ranking basis for total-return approximation |
| `MIN_LISTING_DAYS` | `60` | Exclude ETFs younger than this many trading days when listing date is known |
| `MAX_PREMIUM_DISCOUNT` | `0.02` | Default absolute price/NAV deviation threshold (fallback when group/ticker thresholds don't apply) |
| `ETF_DEVIATION_THRESHOLD_BY_GROUP` | `domestic_equity=0.02,foreign_investment=0.02,commodity=0.02` | Group-specific price/NAV deviation thresholds |
| `ETF_DEVIATION_THRESHOLD_BY_TICKER` | `472150=0.02,486290=0.02,498400=0.02` | Per-ticker deviation override (e.g. covered-call ETFs) |
| `MAX_LIVE_SPREAD_PCT` | `0.005` | Skip live BUY orders when bid-ask spread exceeds threshold |
| `PROTECT_EXTERNAL_HOLDINGS` | `1` | Skip sell for tickers outside strategy universe |
| `BLOCK_LIVE_AFTER_CUTOFF` | `1` | Block live orders past cutoff time |
| `APPLY_SLIPPAGE_IN_LIVE` | `0` | Apply artificial slippage in live mode |
| `LIVE_SLIPPAGE_PCT` | `0.0005` | Live slippage fraction |
| `LIVE_SPREAD_PCT` | `0.0005` | Live spread fallback |
| `KIS_TOKEN_MAX_RETRIES` | `3` | KIS token issuance retry count (network/rate-limit/5xx) |
| `KIWOOM_TOKEN_CACHE_DIR` | `.kiwoom_token_cache/` | Kiwoom token cache directory |
| `LOG_LEVEL` | `INFO` | 로그 레벨. `DEBUG`로 설정 시 필터별 탈락 티커 목록 포함 상세 로그 출력 |
| `LOG_FILE` | (없음) | 파일 로깅 경로. 설정 시 일별 로테이션, 30일 보존 |
| `ETF_DISTRIBUTIONS_FILE` | `data/etf_distributions.csv` | 정규화 분배금 CSV 경로 |
| `ETF_DISTRIBUTION_TAX_PCT` | `0` | 분배금 현금 귀속 시 적용할 세율 (gross return 기준 0) |
| `TARGET_WEIGHT_REBALANCE` | `0` | `1`이면 전체 포트폴리오 평가액 기준 목표비중 리밸런싱. `0`은 기존 현금 배분 방식 |
| `REBALANCE_BAND_PCT` | `0.05` | 목표비중 무거래 허용 폭(절대 비중). `TARGET_WEIGHT_REBALANCE=1`일 때 적용 |
| `TRIM_OVERWEIGHT_POSITIONS` | `0` | `1`이면 기존 승자 보유 방식 유지 + `MAX_ASSET_PCT` 초과분만 부분매도. 기본 비활성 |
| `ETF_EXIT_CHECK_DAYS` | `0` | trailing exit 점검 주기(거래일, 0=비활성). 백테스트 실험 전용 |
| `ETF_TRAILING_STOP_PCT` | `0` | 보유 종가 고점 대비 trailing stop 비율 (0=비활성) |
| `WF_TARGET_WEIGHT_REBALANCE` | (env 기본값) | walk-forward 시 목표비중 방식 사용 여부 |
| `WF_TRIM_OVERWEIGHT_POSITIONS` | (env 기본값) | walk-forward 시 비대칭 하드캡 trim 사용 여부 |
| `WF_EXIT_CHECK_DAYS` | `0` | walk-forward trailing exit 점검 주기 |
| `WF_TRAILING_STOP_PCT` | `0` | walk-forward trailing stop 비율 |
| `WF_MAX_ASSET_PCT` | `0.50` | walk-forward 전용 종목 비중 상한 |
| `WF_REBALANCE_BAND_PCT` | `0.10` | walk-forward 전용 절대 비중 무거래 밴드 |
| `WF_OUTPUT_DIR` | `outputs_walk_forward` | walk-forward 결과 저장 경로. 기존 결과 보존용 |

Full list in `README.md` and `.env.sample`.

## Output layout

- `outputs_etf_only/` — backtest results: `etf_equity_curve.csv`, `etf_trades.csv`, `performance.json`
- `outputs_etf_only/` (experiment mode) — also `etf_trades_slip_5bp.csv`..`_30bp.csv`, `slippage_comparison.csv`
- `outputs_etf_only/` (diagnostics) — `rebalance_diagnostics.csv`, `zero_candidate_impact.csv`, `filter_frequency.csv`
- `outputs_grid/` — grid backtest results: `grid_summary_*.csv`
- `outputs_stability/` — parameter stability results
- `outputs_trade_analysis/` — trade performance attribution results
- `outputs_walk_forward/` — walk-forward validation results
- `outputs_walk_forward_compare/` — walk-forward 비교 결과 (target_weight, legacy 등 시나리오별). gitignored
- `data_cache/` — pykrx OHLCV parquet cache (gitignored)
- `runtime_state/` — daily runner state: `etf_daily_state.json` (gitignored)

## Architecture notes

- `etf_shared.py` holds the shared constants and core strategy: ETF_LIST, fees (buy 0.015%, sell 0.015%), `REBALANCE_STEP_DAYS=10`, `MARKET_MA_DAYS=120`, `MARKET_SLOPE_DAYS=20`, `ETF_MAX_POSITIONS=2`, `ETF_SELL_RANK_BUFFER=3`, `TAXABLE_ETF_TICKERS` (8 tickers).
- `ETF_TICKER_GROUPS` (16 tickers) classifies ETFs into `domestic_equity`/`foreign_investment`/`commodity`. When KOSPI is risk_off, `foreign_investment` and `commodity` groups remain tradable via `is_ticker_risk_on()`. Hardcoded, no env override.
- `add_liquidity_flag()` adds a `liquidity_ok` boolean column based on trailing 60-day avg trading value vs `MIN_AVG_TRADING_VALUE` (default 1B KRW). Always active in backtest. Filtering is as-of per rebalance snapshot.
- `add_listing_flag()` adds `listing_ok` from KRX `LIST_DD` when available; unknown listing dates are allowed.
- `add_deviation_flag()` adds `premium_discount`, `deviation_threshold`, and `deviation_ok` from `close` vs `nav`; missing NAV is allowed for price-only compatibility. Threshold resolution order: `ETF_DEVIATION_THRESHOLD_BY_TICKER` > `ETF_DEVIATION_THRESHOLD_BY_GROUP` > `MAX_PREMIUM_DISCOUNT`.
- `add_price_basis_columns()` sets `close_adj` from `close` by default or from `nav` when `ETF_RETURN_BASIS=nav`. NAV is a total-return approximation, not a full distribution-reinvested return for income ETFs.
- `add_total_return_price()` (in `etf_distributions.py`) builds a distribution-reinvested total-return index per ticker. Used when `ETF_RETURN_BASIS=total_return`.
- `config_utils.parse_pct_env()` — parses env var values as `5bp`, `0.5%`, or `0.0005`; `parse_fraction_env()` handles 0-1 caps such as `MAX_ASSET_PCT`.
- `etf_distributions.py` loads distribution CSV (`data/etf_distributions.csv`), merges ex-date events onto price data, builds a total-return index via `add_total_return_price()`, and computes per-holding distribution cash via `distribution_cash_for_holdings()`. SHA-256 of the CSV is tracked for freeze drift detection.
- `strategy_freeze.py` + `strategy_freeze.json` pin the candidate universe and live parameters at a point in time. `load_frozen_strategy()` verifies the SHA-256 integrity; `diff_payloads()` reports drift. `check_strategy_freeze.py` compares current `.env` against the frozen snapshot and prints OOS performance when available.
- `get_valuation_price()` / `update_last_valid_prices()` in `etf_shared.py` handle missing closing prices by falling back to the last known valid price per ticker. Used in the daily runner to prevent zero valuation on missing data.
- `rank_etfs()` in `etf_shared.py` applies filters step-by-step (liquidity → listing → deviation → trend/return), logging each step's before/after count and dropped tickers at `DEBUG` level. Summary logged at `INFO` level.
- Both `run_etf_backtest.py` and `live_trading/etf_daily_runner.py` use Python `logging` module with configurable level (`LOG_LEVEL`, default `INFO`) and optional file rotation (`LOG_FILE`, 30-day retention). `DEBUG` level exposes per-filter dropped ticker lists.
- `run_etf_strategy()` accepts an optional `rebalance_observer` keyword-only callback. When provided, it is called at each rebalance with a dict containing pre/post portfolio state, risk flags, targets, and order results. The callback does not affect backtest logic; observer errors are logged and re-raised.
- Exit-only trailing overlay (`ETF_EXIT_CHECK_DAYS` / `ETF_TRAILING_STOP_PCT`): backtest only, not applied in live runner. Tracks per-ticker peak close and sells at next-day open when drop exceeds threshold. Stopped tickers are excluded from same-day rebalance targets. Default inactive (both `0`).
- Lint: `ruff check .` — see `[tool.ruff]` in `pyproject.toml`. No type checker, no test framework.
- `.env` is gitignored; copy `.env.sample` to create one.
- KIS adapter (`live_trading/kis/` package) shares a 7-method interface with KiwoomAdapter: `get_cash()`, `get_holdings()`, `get_prices()`, `get_bid_ask_prices()`, `place_order()`, `get_order_status()`, `cancel_order()`.
- Token issuance retry: both KIS (`KisAuthManager.issue_token()`) and Kiwoom (`KiwoomAdapter._issue_token()`) retry on network errors (exponential backoff), rate-limit (429 / msg_cd), and 5xx server errors. HTTP 400/401 fail immediately (bad credentials). KIS retries up to `KIS_TOKEN_MAX_RETRIES` (default 3); Kiwoom reuses `KIWOOM_HTTP_MAX_RETRIES` (default 4). Both use the shared API retry delay (`KIS_RETRY_DELAY` / `KIWOOM_HTTP_RETRY_DELAY`) — no separate token-specific delay env var. Token-issuance rate-limit (KIS `EGW00133` "1분당 1회", Kiwoom `return_code=="5"` or "허용된 요청 개수를 초과") is handled separately: 60s wait + 1 retry, then fail.
- Token caching: both adapters cache tokens to disk (KIS: `KIS_{env_mode}_{YYYYMMDD}.json` in `KIS_CONFIG_PATH`; Kiwoom: `KIWOOM{YYYYMMDD}.json` in `KIWOOM_TOKEN_CACHE_DIR`). Lazy re-issuance via `_issue_token_if_needed()` checks cache validity before issuing. KIS cache filename includes `ENV_MODE` to prevent stale tokens on env switch.
- Auth-failure recovery: both `KisApiClient._get()`/`_post()` and `KiwoomAdapter._post()` detect 401 (KIS also checks token-expiry `msg_cd` in `{EGW00121, EGW00122, EGW00123}`), call `invalidate_token()` + re-issue once, then retry. `auth_retried` flag prevents infinite re-auth loops. `EGW00207` (IP whitelist) and `EGW00103/105` (bad appkey/secret) are NOT retried — they require portal configuration, not token re-issuance.
- Env-mismatch detection at token issuance: `KisAuthManager.issue_token()` parses the 400/401 response body and, when `msg_cd` is `EGW00103`/`EGW00105` (invalid AppKey/AppSecret), raises a clear "앱키/시크릿이 ENV_MODE 환경과 일치하지 않습니다" error instead of a raw HTTP message. This is where wrong-env appkey failures actually manifest (before `_check_env_mismatch()` runs).
