# AGENTS.md — kr-quant-engine

## Project overview

Korean ETF rotation backtest + daily live runner. Python 3.11, single-module layout (no monorepo). Uses `pykrx` for KRX OHLCV data.

## Key entrypoints

- `run_etf_backtest.py` — primary backtest (1250+ lines, procedural). Config via env vars.
- `live_trading/etf_daily_runner.py` — daily live order runner (default: safe mode, no real orders).
- `live_trading/kiwoom_adapter.py` — Kiwoom REST API adapter for live brokerage.
- Shared strategy logic: `etf_shared.py` (ETF_LIST, fees, ranking, order building).

## Commands

```
uv sync                     # install dependencies (uv, not pip)
uv run python run_etf_backtest.py              # single-mode backtest
uv run python live_trading/etf_daily_runner.py  # daily runner (safe mode)
uv run python live_trading/etf_daily_runner.py --force-live  # bypass safety cutoff
uv run scripts/<script>.py                      # any analysis script
ruff check .                # lint (ruff only, no mypy/pytest config)
```

## Critical conventions

- **Only `ruff` for linting** — no mypy, no pytest, no pre-commit. Set `line-length=100`.
- **`.env` must be loaded before `import pykrx`** — the token expires otherwise. Both entrypoints call `load_dotenv()` at module level before the `from pykrx import stock` line.
- **Safety first** — `live_trading/etf_daily_runner.py` defaults to `LIVE_ORDER_ENABLED=0`. Live orders require `--force-live` CLI flag.
- **Env-var-driven config** — backtest mode (`ETF_BACKTEST_MODE=single|experiment`), slippage (`ETF_BASE_SLIPPAGE=0.0005`), slippage units accept `"5bp"`, `"0.5%"`, or `"0.0005"` via `config_utils.parse_pct_env()`.

## Key env vars

| Var | Default | Purpose |
|---|---|---|
| `ETF_BACKTEST_MODE` | `single` | `experiment` runs 4 slippage scenarios (5/10/20/30bp) |
| `ETF_BASE_SLIPPAGE` | `0.0005` | Backtest slippage fraction |
| `ETF_ENABLE_BENCHMARK` | `1` | Include KODEX200 comparison in single mode |
| `LIVE_ORDER_ENABLED` | `0` | Live order toggle (0=safe mode) |
| `PROTECT_EXTERNAL_HOLDINGS` | `1` | Skip sell for tickers outside strategy universe |
| `BLOCK_LIVE_AFTER_CUTOFF` | `1` | Block live orders past cutoff time |

Full list in `README.md` and `.env.sample`.

## Output layout

- `outputs_etf_only/` — backtest results: `etf_equity_curve.csv`, `etf_trades.csv`, `performance.json`
- `data_cache/` — pykrx OHLCV cache (gitignored)
- `runtime_state/` — daily runner state: `etf_daily_state.json` (gitignored)

## Architecture notes

- `etf_shared.py` holds the shared constants and core strategy: ETF_LIST, fees (buy 0.015%, sell 0.015%), `REBALANCE_STEP_DAYS=10`, `MARKET_MA_DAYS=120`, `MARKET_SLOPE_DAYS=20`.
- Lint: `ruff check .` — see `[tool.ruff]` in `pyproject.toml`. No type checker, no test framework.
- `.env` is gitignored; copy `.env.sample` to create one.
- Legacy files in `legacy/scripts/` and `legacy/core/` — not active. Don't use for new experiments.
