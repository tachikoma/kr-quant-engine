# AGENTS.md — kr-quant-engine

## Project overview

Korean ETF rotation backtest + daily live runner. Python 3.11, single-module layout (no monorepo). Uses `pykrx` for KRX OHLCV data.

## Key entrypoints

- `run_etf_backtest.py` — primary backtest (2400+ lines, procedural). Config via env vars. CLI: `--start`, `--end`, `--mode`. `run_etf_strategy()` supports optional `rebalance_observer` callback for diagnostics.
- `live_trading/etf_daily_runner.py` — daily live order runner (default: safe mode, no real orders). CLI: `--force-live`.
- `live_trading/kiwoom_adapter.py` — Kiwoom REST API adapter for live brokerage.
- `live_trading/kis_adapter.py` — Korea Investment & Securities (KIS) REST API adapter.
- `live_trading/kis/` — KIS API client/auth package (`KisApiClient`, `KisAuthManager`).
- `live_trading/telegram_notifier.py` — Telegram order/execution notification.
- Shared strategy logic: `etf_shared.py` (ETF_LIST, fees, ranking, order building).
- `etf_distributions.py` — ETF 현금분배금 CSV 로드 및 total-return 수익률 계산.
- `strategy_freeze.py` — 전략 동결 스냅샷 생성/검증 유틸리티. `strategy_freeze.json`과 함께 사용.
- Analysis scripts: `scripts/` (58 files — 57 스크립트 + 공용 헬퍼 `_proxy_utils.py`). Key: `grid_backtest.py`, `correlation_analysis.py`, `apply_cap_and_retest.py`, `walk_forward_validation.py`, `parameter_stability.py`, `trade_performance_attribution.py`, `check_strategy_freeze.py`, `track_oos_performance.py`, `factorial_ablation.py`, `restore_pit_classification.py`, `benchmark_comparison.py`, `pit_backtest.py`, `analyze_current_drawdown.py`, `analyze_proxy_signal.py`, `sweep_proxy_match.py`, `validate_proxy_stats.py`, `build_point_in_time_universe.py`, `prefetch_pit_prices.py`, `analyze_universe_selection_bias.py`, `sweep_multi_index_split.py`, `test_split_gating.py`.

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
uv run scripts/track_oos_performance.py         # v2 live OOS equity tracking (broker-only by default)
uv run scripts/factorial_ablation.py            # factor-wise isolated effect (CAGR/MDD/Sharpe deltas)
uv run scripts/restore_pit_classification.py    # PIT delisted ETF 227 historical classification restore
uv run scripts/benchmark_comparison.py          # multi-benchmark (KR/US/Gold/policy/cash) + market impact
uv run scripts/pit_backtest.py                  # PIT universe backtest (survivorship-bias-free) vs static
uv run scripts/analyze_filter_frequency.py       # risk_on + 후보 0개 빈도 분석
uv run scripts/analyze_zero_candidate_impact.py  # 후보 0개 사건 포트폴리오 영향 분석
uv run scripts/analyze_current_drawdown.py       # 현재 MDD 기여도·리밸런싱 이력
uv run scripts/analyze_proxy_signal.py           # SPY vs QQQ 시그널/포트폴리오/레짐 비교 분석
uv run scripts/sweep_proxy_match.py              # 프록시-지수 매칭 실험 (6 시나리오)
uv run scripts/validate_proxy_stats.py           # 통계적 검증 (bootstrap CI, 레짐 분석)
uv run scripts/<script>.py                      # any analysis script
ruff check .                # lint (ruff only, no mypy/pytest config)
```

## Critical conventions

- **Only `ruff` for linting** — no mypy, no pytest, no pre-commit. Set `line-length=100`.
- **`.env` must be loaded before `import pykrx`** — the token expires otherwise. Both entrypoints call `load_dotenv()` at module level before the `from pykrx import stock` line.
- **Safety first** — `live_trading/etf_daily_runner.py` defaults to `LIVE_ORDER_ENABLED=0`. Live orders require `--force-live` CLI flag.
- **Env-var-driven config** — backtest mode (`ETF_BACKTEST_MODE=single|experiment`), slippage (`ETF_BASE_SLIPPAGE=0.0005`), slippage units accept `"5bp"`, `"0.5%"`, or `"0.0005"` via `config_utils.parse_pct_env()`.
- **BROKER_TYPE=KIWOOM|KIS** — selects live brokerage adapter (default KIWOOM).
- **TAXABLE_ETF_TICKERS** — auto-computed from KRX classification in auto mode; hardcoded 8 tickers in static mode. Subject to 15.4% dividend tax on trading gains.
- **ETF_UNIVERSE_MODE** — `static` (default, hardcoded `ETF_LIST`) or `auto` (KRX-classification-based auto-build). `ETF_LIST` env var takes precedence over auto.

## Key env vars

| Var | Default | Purpose |
|---|---|---|
| `ETF_BACKTEST_MODE` | `single` | `experiment` runs 4 slippage scenarios (5/10/20/30bp) |
| `ETF_UNIVERSE_MODE` | `static` | `static` (hardcoded ETF_LIST) or `auto` (KRX-classification-based auto-build). `ETF_LIST` env var takes precedence over auto |
| `MAX_ASSET_PCT` | `0.50` | Per-asset position cap. `0` = unlimited |
| `ETF_RETURN_BASIS` | `price` | `nav` uses NAV as ranking basis for total-return approximation |
| `MIN_AVG_TRADING_VALUE` | `1000000000` | Min avg daily trading value (KRW) for ETF liquidity filter |
| `MAX_PREMIUM_DISCOUNT` | `0.02` | Default absolute price/NAV deviation threshold (fallback when group/ticker thresholds don't apply) |
| `LIVE_ORDER_ENABLED` | `0` | Live order toggle (0=safe mode) |
| `BROKER_TYPE` | `KIWOOM` | Broker choice: `KIWOOM` or `KIS` |
| `LOG_LEVEL` | `INFO` | 로그 레벨. `DEBUG`로 설정 시 필터별 탈락 티커 목록 포함 상세 로그 출력 |
| `TARGET_WEIGHT_REBALANCE` | `0` | `1`이면 전체 포트폴리오 평가액 기준 목표비중 리밸런싱. `0`은 기존 현금 배분 방식 |
| `WF_STATE_BASED` | `1` | 폴드 경계에서 실제 보유/현금/원가 이월. `0`이면 기존 슬라이싱+경계비용 경로 |
| `ETF_MOMENTUM_WEIGHT_60` | `0.55` | Momentum score weight for `ret_60` (ret_120 gets `1 - weight`). Used by factorial ablation |

> 전체 목록은 README.md 환경 변수 섹션 및 `.env.sample` 참조

## Output layout

- `outputs_etf_only/` — backtest 결과 (`etf_equity_curve.csv`, `etf_trades.csv`, `performance.json`, experiment/diagnostics CSV 포함)
- `outputs_*` — grid/walk_forward/stability/trade_analysis/compare/universe_bias/ablation/benchmark/pit 등 분석 결과 (상세는 README 디렉터리 가이드 참조)
- `data_cache/`/`runtime_state/` — pykrx OHLCV parquet 캐시 및 daily runner 상태(`etf_daily_state.json`, `oos_equity_history.json`), 모두 gitignored

## Architecture notes

- `etf_shared.py` holds shared constants/core strategy: ETF_LIST, fees (buy/sell 0.015%), `REBALANCE_STEP_DAYS=20`, `MARKET_MA_DAYS=120`, `MARKET_SLOPE_DAYS=20`, `ETF_MAX_POSITIONS=2`, `ETF_SELL_RANK_BUFFER=3`, `TAXABLE_ETF_TICKERS` (8 tickers).
- `etf_universe.py` exposes `build_universe()` (pure) + `config_from_env()` for auto-building the universe from KRX classification. Used when `ETF_UNIVERSE_MODE=auto`.
- `ETF_TICKER_GROUPS` (16 static / 573 auto) classifies `domestic_equity`/`foreign_investment`/`commodity`; risk_off 시 `foreign_investment`/`commodity`는 `is_ticker_risk_on()`으로 거래 유지. **검증: auto 유니버스는 static 대비 열등 (CAGR 12.2% vs 25.7%), static 유지 권장.**
- `rank_etfs()` applies filters step-by-step (pit_membership → liquidity → listing → deviation → trend/return), logging before/after counts; `pit_membership_ok` only runs when the column is present.
- `etf_distributions.py` builds a distribution-reinvested total-return index (`add_total_return_price()`) and computes per-holding cash; SHA-256 tracked for freeze drift. `strategy_freeze.py` + `strategy_freeze.json` pin universe/params and verify integrity.
- State-based walk-forward (`WF_STATE_BASED=1`, default): `walk_forward_validation.py` runs each fold's test segment from the previous fold's actual end state (holdings/cash/tax basis) instead of slicing a pre-run curve with a flat boundary cost.
- PIT integration: `pit_universe.add_pit_membership_flag()` adds as-of membership; `build_pit_ticker_groups()` merges 1,143 current + 227 restored = 1,370-ticker map. `run_etf_strategy(ticker_groups=...)` overrides groups for correct risk-off gating.
- Live risk monitoring (`_calculate_risk_snapshot()`): per-position weights/drawdown from peak equity before orders; alerts logged + Telegram, no order effect. Peak equity persisted in `runtime_state/etf_daily_state.json`.
- Holiday detection (`_is_trading_day()`): checks `_KRX_HOLIDAYS` (hardcoded 2026) at `run_daily()` entry; update annually and on mid-year 임시공휴일 (2026 지방선거 6/3, 제헌절 복원 7/17).
- `get_valuation_price()`/`update_last_valid_prices()` fall back to last valid close on missing data; `config_utils.parse_pct_env()`/`parse_fraction_env()` parse `5bp`/`0.5%`/`0.0005` and 0-1 caps.
- Broker token retry/cache/auth details are delegated to `docs/broker.md` (not duplicated here).

## Lint

`ruff check .` — see `[tool.ruff]` in `pyproject.toml`. No type checker, no test framework.

## References

- 검증 결과: `docs/verification_2026-08-29.md` 참조
- 전체 환경변수/디렉터리 가이드: `README.md`, `.env.sample`
- `.env` is gitignored; copy `.env.sample` to create one.
