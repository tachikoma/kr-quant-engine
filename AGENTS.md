# AGENTS.md — kr-quant-engine

## Project overview

Korean ETF rotation backtest + daily live runner. Python 3.11, single-module layout (no monorepo). Uses `pykrx` for KRX OHLCV data.

## Key entrypoints

- `run_etf_backtest.py` — primary backtest (1250+ lines, procedural). Config via env vars. CLI: `--start`, `--end`, `--mode`.
- `live_trading/etf_daily_runner.py` — daily live order runner (default: safe mode, no real orders). CLI: `--force-live`.
- `live_trading/kiwoom_adapter.py` — Kiwoom REST API adapter for live brokerage.
- `live_trading/kis_adapter.py` — Korea Investment & Securities (KIS) REST API adapter.
- `live_trading/kis/` — KIS API client/auth package (`KisApiClient`, `KisAuthManager`).
- `live_trading/telegram_notifier.py` — Telegram order/execution notification.
- Shared strategy logic: `etf_shared.py` (ETF_LIST, fees, ranking, order building).
- Analysis scripts: `scripts/` (15 scripts). Key: `grid_backtest.py`, `correlation_analysis.py`, `apply_cap_and_retest.py`.

## Commands

```
uv sync                     # install dependencies (uv, not pip)
uv run python run_etf_backtest.py              # single-mode backtest (add --start/--end/--mode)
uv run python live_trading/etf_daily_runner.py  # daily runner (safe mode)
uv run python live_trading/etf_daily_runner.py --force-live  # bypass safety cutoff
uv run scripts/grid_backtest.py                 # grid backtest (candidate pool / rebalance / positions)
uv run scripts/correlation_analysis.py          # drawdown correlation analysis
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
| `MAX_ASSET_PCT` | — | Per-asset position cap (e.g. `0.20`) |
| `ETF_USE_CACHE` | `1` | Use parquet data cache |
| `ETF_REFRESH_CACHE` | `0` | Force refetch data |
| `LIVE_ORDER_ENABLED` | `0` | Live order toggle (0=safe mode) |
| `BROKER_TYPE` | `KIWOOM` | Broker choice: `KIWOOM` or `KIS` |
| `PROTECT_EXTERNAL_HOLDINGS` | `1` | Skip sell for tickers outside strategy universe |
| `BLOCK_LIVE_AFTER_CUTOFF` | `1` | Block live orders past cutoff time |
| `APPLY_SLIPPAGE_IN_LIVE` | `0` | Apply artificial slippage in live mode |
| `LIVE_SLIPPAGE_PCT` | `0.0005` | Live slippage fraction |
| `LIVE_SPREAD_PCT` | `0.0005` | Live spread fallback |

Full list in `README.md` and `.env.sample`.

## Output layout

- `outputs_etf_only/` — backtest results: `etf_equity_curve.csv`, `etf_trades.csv`, `performance.json`
- `outputs_etf_only/` (experiment mode) — also `etf_trades_slip_5bp.csv`..`_30bp.csv`, `slippage_comparison.csv`
- `outputs_grid/` — grid backtest results: `grid_summary_*.csv`
- `data_cache/` — pykrx OHLCV parquet cache (gitignored)
- `runtime_state/` — daily runner state: `etf_daily_state.json` (gitignored)

## Architecture notes

- `etf_shared.py` holds the shared constants and core strategy: ETF_LIST, fees (buy 0.015%, sell 0.015%), `REBALANCE_STEP_DAYS=10`, `MARKET_MA_DAYS=120`, `MARKET_SLOPE_DAYS=20`, `ETF_MAX_POSITIONS=2`, `ETF_SELL_RANK_BUFFER=3`, `TAXABLE_ETF_TICKERS` (8 tickers).
- `config_utils.parse_pct_env()` — parses env var values as `5bp`, `0.5%`, or `0.0005`.
- Lint: `ruff check .` — see `[tool.ruff]` in `pyproject.toml`. No type checker, no test framework.
- `.env` is gitignored; copy `.env.sample` to create one.
- KIS adapter (`live_trading/kis/` package) shares a 7-method interface with KiwoomAdapter: `get_cash()`, `get_holdings()`, `get_prices()`, `get_bid_ask_prices()`, `place_order()`, `get_order_status()`, `cancel_order()`.
