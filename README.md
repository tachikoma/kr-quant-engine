# kr_quant_engine

국내 ETF 로테이션 백테스트/하루 1회 실행 프로젝트입니다.

현재 운영 기준은 ETF 전용 시나리오이며, 실행 기준 스크립트는 run_etf_backtest.py 입니다.

## 현재 운영 방향

- 주 실행 경로: run_etf_backtest.py
- 실전 연결 전 검증 경로: live_trading/etf_daily_runner.py (기본 안전모드)
- 데이터 소스: pykrx (ETF/지수 OHLCV)
- 기본 리밸런싱: 10거래일 주기
- 기본 비교 기준: KODEX 200 Buy&Hold
- 기본 실험: 슬리피지 민감도(5/10/20/30bp)

## 빠른 시작

1. 의존성 동기화

```bash
uv sync
```

1. ETF 백테스트 실행

```bash
uv run python run_etf_backtest.py [--start YYYYMMDD] [--end YYYYMMDD] [--mode single|experiment]
```

CLI 인자:
- `--start`, `-s`: 백테스트 시작일 (기본: 20160101)
- `--end`, `-e`: 백테스트 종료일 (기본: 오늘)
- `--mode`, `-m`: 실행 모드 (기본: single, env `ETF_BACKTEST_MODE`보다 우선)

1. ETF 하루 1회 실행 러너(기본 안전모드)

```bash
uv run python live_trading/etf_daily_runner.py [--force-live]
```

기본값은 안전모드(LIVE_ORDER_ENABLED=0)로 실제 주문을 전송하지 않고 계획만 생성/기록합니다.

강제 우회(위험): 컷오프 안전장치를 우회하여 실주문을 허용하려면 아래처럼 실행하세요. 실제 주문이 발생하므로 신중히 사용하세요.

```bash
python live_trading/etf_daily_runner.py --force-live
```

## 환경 변수(.env)

### 공통

- `KRX_ID`, `KRX_PW`: pykrx 인증 (KRX 데이터 조회)
- `ENABLE_TICKER_NAME_LOOKUP=1`: 종목명 조회 활성화 (기본 0)
- `ENV_MODE=real|demo`: 운영 모드 (기본 real)
- `BROKER_TYPE=KIWOOM|KIS`: 증권사 선택 (기본 KIWOOM)
- `ETF_LIST`: 티커 쉼표 목록으로 ETF 후보풀 오버라이드 (예: `069500,091160`)
- `MIN_AVG_TRADING_VALUE`: trailing 60거래일 평균 거래대금 기준 유동성 임계값(원). 기본 `1000000000` (10억). 미달 시 리밸런싱 snapshot에서 제외 (백테스트/라이브 공통)
- `ETF_RETURN_BASIS=price|nav|total_return`: 랭킹 수익률 기준. `total_return`은 검증된
  현금분배금을 분배락일에 재투자합니다(기본 `price`).
- `ETF_DISTRIBUTIONS_FILE`: 정규화 분배금 CSV 경로(기본 `data/etf_distributions.csv`)
- `ETF_DISTRIBUTION_TAX_PCT`: 분배금 현금 귀속 시 적용할 세율(기본 `0`, gross return). 백테스트 전용(라이브는 증권사 예수금에 자동 반영)
- `MIN_LISTING_DAYS`: 최소 상장 거래일 필터. 기본 `60`
- `MAX_PREMIUM_DISCOUNT`: NAV 대비 괴리율 절대값 임계. 기본 `0.02` (2%)
- `TARGET_WEIGHT_REBALANCE=0|1`: `1`이면 남은 현금만 배분하지 않고 전체 포트폴리오
  평가액 기준으로 목표비중을 맞춥니다. 기본 `0`은 기존 주문 방식입니다.
- `REBALANCE_BAND_PCT`: 목표비중 리밸런싱의 무거래 허용 폭. 기본 `0.05`는 절대 비중
  5%p를 뜻하며, `TARGET_WEIGHT_REBALANCE=1`일 때 적용됩니다.
- `TRIM_OVERWEIGHT_POSITIONS=0|1`: 기존 승자 보유 방식은 유지하고
  `MAX_ASSET_PCT`를 넘는 보유분만 부분매도합니다. 기본 `0`.

### 백테스트(run_etf_backtest.py)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `ETF_BACKTEST_MODE` | `single` | 실행 모드 (`single` / `experiment`) |
| `ETF_BASE_SLIPPAGE` | `0.0005` (5bp) | 기본 슬리피지 |
| `ETF_SPREAD_PCT` | `0.0005` (5bp) | 호가 스프레드 |
| `ETF_ENABLE_BENCHMARK` | `1` | KODEX200 비교 포함 여부 |
| `MAX_ASSET_PCT` | `0.50` | 자산별 최대 비중 제한 (0: 제한 없음) |
| `TARGET_WEIGHT_REBALANCE` | `0` | 전체 평가액 기준 목표비중 리밸런싱 활성화 |
| `REBALANCE_BAND_PCT` | `0.05` | 목표비중 대비 무거래 허용 폭(절대 비중) |
| `TRIM_OVERWEIGHT_POSITIONS` | `0` | 종목별 비중 상한 초과분만 부분매도 |
| `ETF_EXIT_CHECK_DAYS` | `0` | trailing exit 점검 주기(거래일, 0=비활성) |
| `ETF_TRAILING_STOP_PCT` | `0` | 보유 종가 고점 대비 trailing stop 비율 |
| `ETF_PORTFOLIO_TRAILING_STOP_PCT` | `0` | 포트폴리오 고점 대비 전량 청산 비율 |
| `ETF_USE_CACHE` | `1` | parquet 캐시 사용 |
| `ETF_REFRESH_CACHE` | `0` | 캐시 무시하고 재조회 |
| `ETF_TAXABLE_SELL_TAX_PCT` | `0.154` (15.4%) | 과세 ETF 매도 시 배당소득세율 |

과세 대상 ETF (`TAXABLE_ETF_TICKERS`): 미국S&P500(H), ACE 미국S&P500, TIGER 미국S&P500, TIGER 미국나스닥100, TIGER 배당커버드콜액티브, TIGER 미국나스닥100TDCC, KODEX 200TWCC, ACE KRX금현물

### 데일리 러너(live_trading/etf_daily_runner.py)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `LIVE_ORDER_ENABLED` | `0` | 실주문 모드 (1=활성, 0=안전모드) |
| `WAIT_UNTIL_MARKET_OPEN` | `1` | 장 시작까지 대기 |
| `DAILY_PLAN_TIME` | `08:50` | 계획 수립 시각 |
| `MARKET_OPEN_TIME` | `09:00` | 주문 실행 시각 |
| `DAILY_RUN_FORCE` | `0` | 같은 날 재실행 허용 |
| `FORCE_REBALANCE` | `0` | 리밸런싱 강제 실행 |
| `SELL_CUTOFF_TIME` | `09:05` | 매도 체결 컷오프 |
| `BUY_CUTOFF_TIME` | `09:10` | 매수 체결 컷오프 |
| `ORDER_POLL_INTERVAL_SEC` | `2` | 체결 조회 주기(초) |
| `SELL_FILL_TIMEOUT_SEC` | `300` | 매도 체결 최대 대기(초) |
| `BUY_FILL_TIMEOUT_SEC` | `300` | 매수 체결 최대 대기(초) |
| `CANCEL_UNFILLED_ORDERS` | `1` | 미체결 주문 취소 |
| `RETRY_UNFILLED_ORDERS` | `1` | 미체결 잔량 재주문 |
| `RETRY_ORDER_TYPE` | `MARKET` | 재주문 유형 |
| `RETRY_FILL_TIMEOUT_SEC` | `90` | 재주문 체결 최대 대기(초) |
| `PROTECT_EXTERNAL_HOLDINGS` | `1` | 전략 유니버스 외 종목 매도 제외 |
| `BLOCK_LIVE_AFTER_CUTOFF` | `1` | 컷오프 이후 실주문 차단 |
| `FORCE_LIVE_CUTOFF_EXTEND_MIN` | `15` | `--force-live` 시 컷오프 연장(분) |
| `APPLY_SLIPPAGE_IN_LIVE` | `0` | 실전 인위적 슬리피지 적용 |
| `LIVE_SLIPPAGE_PCT` | `0.0005` | 실전 슬리피지 |
| `LIVE_SPREAD_PCT` | `0.0005` | 실전 호가 스프레드 fallback |
| `MAX_LIVE_SPREAD_PCT` | `0.005` | 실전 bid-ask 스프레드 초과 시 매수 스킵 |
| `LIVE_CONCENTRATION_WARN_PCT` | `0.60` | 전략 종목 최대 비중 경고 임계값 |
| `LIVE_DRAWDOWN_WARN_PCT` | `0.15` | 실전 누적 고점 대비 낙폭 경고 임계값 |

### 증권사 API (BROKER_TYPE에 따라)

**키움(KIWOOM)** — `live_trading/kiwoom_adapter.py`:

- `KIWOOM_APPKEY`, `KIWOOM_SECRETKEY`
- `KIWOOM_ACCOUNT_NO`: 계좌번호 (선택, 미설정시 API 응답에서 추론)
- 예수금 조회: `get_cash()`/`get_available_cash()`/daily runner는 주문 가능 추정예수금(`qry_tp=3`)을 사용
- 엔드포인트: `KIWOOM_TOKEN_ENDPOINT`(`/oauth2/token`), `KIWOOM_CASH_ENDPOINT`(`/api/dostk/acnt`), `KIWOOM_HOLDINGS_ENDPOINT`(`/api/dostk/acnt`), `KIWOOM_PRICE_ENDPOINT`(`/api/dostk/mrkcond`), `KIWOOM_ORDER_ENDPOINT`(`/api/dostk/ordr`), `KIWOOM_ORDER_STATUS_ENDPOINT`(`/api/dostk/acnt`), `KIWOOM_ORDER_CANCEL_ENDPOINT`(`/api/dostk/ordr`)
- API ID: `KIWOOM_TOKEN_API_ID`(`au10001`), `KIWOOM_CASH_API_ID`(`kt00001`), `KIWOOM_HOLDINGS_API_ID`(`kt00018`), `KIWOOM_PRICE_API_ID`(`ka10004`), `KIWOOM_ORDER_BUY_API_ID`(`kt10000`), `KIWOOM_ORDER_SELL_API_ID`(`kt10001`), `KIWOOM_ORDER_CANCEL_API_ID`(`kt10003`), `KIWOOM_ORDER_STATUS_API_ID`(`kt00007`)
- 응답 키 매핑(계좌별 차이 대응): `KIWOOM_CASH_PATH`(`ord_alow_amt`), `KIWOOM_HOLDINGS_PATH`(`acnt_evlt_remn_indv_tot`), `KIWOOM_HOLDINGS_TICKER_KEY`(`stk_cd`), `KIWOOM_HOLDINGS_QTY_KEY`(`rmnd_qty`), `KIWOOM_PRICE_PATH`(`sel_fpr_bid`), `KIWOOM_PRICE_PATH_BUY`(`buy_fpr_bid`), `KIWOOM_PRICE_PATH_SELL`(`sel_fpr_bid`), `KIWOOM_ORDER_ID_PATH`(`ord_no`), `KIWOOM_ORDER_TICKER_KEY`, `KIWOOM_ORDER_QTY_KEY`, `KIWOOM_ORDER_PRICE_KEY`, `KIWOOM_ORDER_TYPE_KEY`, `KIWOOM_ORDER_ACCOUNT_KEY`, `KIWOOM_ORDER_STATUS_ACCOUNT_KEY`, `KIWOOM_ORDER_STATUS_LIST_KEY`, `KIWOOM_ORDER_STATUS_QRY_TP`, `KIWOOM_ORDER_CANCEL_ID_KEY`, `KIWOOM_ORDER_CANCEL_TICKER_KEY`, `KIWOOM_ORDER_CANCEL_QTY_KEY`, `KIWOOM_ORDER_CANCEL_ACCOUNT_KEY`
- 주문 유형: `MARKET→trde_tp=3`, `LIMIT→trde_tp=0`, IOC/FOK = 숫자 코드(13, 23)
- HTTP 디버그: `KIWOOM_HTTP_DEBUG_RESPONSE`, `KIWOOM_HTTP_DEBUG_BODY`, `KIWOOM_HTTP_DEBUG_BODY_LIMIT`, `KIWOOM_HTTP_RETRY_DELAY`, `KIWOOM_HTTP_MIN_INTERVAL`

**한투(KIS)** — `live_trading/kis_adapter.py`:

- `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO`, `KIS_ACCOUNT_PROD_CD=01`

### 텔레그램 알림 (`live_trading/telegram_notifier.py`)

- `TELEGRAM_BOT_TOKEN`: 봇 토큰
- `TELEGRAM_CHAT_ID`: 수신 채팅/채널 ID
- 토큰/채팅ID 미설정 시 조용히 비활성화됨
- 실전 실행 요약에 주문 전 전략 평가액, 최대 종목 비중, 누적 고점 대비 낙폭 경고 포함
- 실계좌 고점은 `runtime_state/etf_daily_state.json`에 누적되며 최초 실행 시 현재 평가액으로 초기화
- 증권사 API가 없는 모의 잔고는 저장된 실계좌 고점과 위험 스냅샷을 갱신하지 않음
- 계좌 입출금은 수익이 아니어도 낙폭에 영향을 줄 수 있으므로 큰 현금 이동 후 고점 기준 확인 필요

기본 동작에서는 종목명 조회 없이 티커를 그대로 사용하므로, 인증 정보 없이도 백테스트는 실행됩니다.

## 주요 결과물

### 백테스트 (`run_etf_backtest.py` → `outputs_etf_only/`)

**single 모드(기본):**

- `etf_equity_curve.csv` — 일별 포트폴리오 가치 (strategy + benchmark)
- `etf_trades.csv` — 체결 내역
- `performance.json` — 성과 지표 (CAGR, MDD, Sharpe, Sortino, Calmar, CVaR95, Ulcer Index, Tail Ratio, Recovery Factor, 회전율 등)
- `performance_comparison.csv` — 전략과 KODEX200 Buy&Hold의 수익·위험·위험조정 성과 비교
- `monthly_returns.csv` / `annual_returns.csv` — 월별·연도별 수익률
- `rolling_metrics.csv` — 1년 롤링 CAGR, MDD, Sortino (차트 생성용 시계열)

**experiment 모드(`ETF_BACKTEST_MODE=experiment`):**

- `etf_equity_curve.csv` — 전체 통합 커브 (benchmark + 4개 슬리피지 시나리오)
- `etf_trades_slip_5bp.csv` / `_10bp` / `_20bp` / `_30bp.csv` — 슬리피지별 체결 내역
- `slippage_comparison.csv` — benchmark + 4개 시나리오 equity 비교

### 그리드 백테스트 (`scripts/grid_backtest.py` → `outputs_grid/`)

- `grid_summary_*.csv` — 그리드 실행 결과 요약

### 분석 스크립트 (`scripts/`)

스크립트별로 `outputs_grid/` 또는 콘솔 출력으로 결과 생성.

참고: 기존 산출물 폴더(`outputs`, `outputs_mixed`, `outputs_small_cap`)는 과거 실험 결과 보관용입니다.

## 디렉터리 가이드

- `run_etf_backtest.py`: ETF 백테스트 메인 (CLI: `--start`, `--end`, `--mode`)
- `etf_shared.py`: 공통 전략 상수/로직 (ETF_LIST, ranking, 주문 생성)
- `config_utils.py`: 환경변수 파싱 유틸 (`parse_pct_env`, `parse_fraction_env`)
- `pykrx_utils.py`: pykrx 호출 유틸 (FD 캡처, 캐시)
- `live_trading/etf_daily_runner.py`: 데일리 주문 계획/실행 러너
- `live_trading/kiwoom_adapter.py`: 키움 REST API 어댑터
- `live_trading/kis_adapter.py`: 한국투자증권(KIS) REST API 어댑터
- `live_trading/kis/`: KIS API 클라이언트/인증 패키지
- `live_trading/telegram_notifier.py`: 텔레그램 알림 전송
- `outputs_etf_only/`: 백테스트 결과물
- `outputs_grid/`: 그리드 백테스트 결과물
- `outputs_compare/`: 프록시 분석/비교 실험 결과 (`proxy_analysis/`, `proxy_match/`, gitignored)
- `data_cache/`: pykrx OHLCV parquet 캐시 (gitignored)
- `runtime_state/`: 데일리 러너 상태 (`etf_daily_state.json`, gitignored)
- `scripts/`: 분석/실험 스크립트 32개
- `DOCS/`: 추가 문서

## 분석/실험 스크립트 (`scripts/`)

```bash
uv run scripts/grid_backtest.py                          # 그리드 백테스트 (후보풀/리밸런스/포지션)
uv run scripts/correlation_analysis.py                   # 드로우다운 상관분석
uv run scripts/apply_cap_and_retest.py                   # MAX_ASSET_PCT 적용 재백테스트
uv run scripts/filter_and_retest_by_risk.py              # 리스크 필터링 그리드
uv run scripts/filter_candidates.py                      # ETF 후보군 필터링
uv run scripts/compute_concentration.py                  # 집중도 분석
uv run scripts/analyze_filtered_results.py               # 필터링 결과 분석
uv run scripts/analyze_grid_results.py                   # 그리드 결과 분석
uv run scripts/analyze_grid_summary.py                   # 그리드 요약 분석
uv run scripts/analyze_drawdown_trades.py                # 드로우다운 기간 거래 분석
uv run scripts/analyze_current_drawdown.py               # 현재 MDD 기여도·리밸런싱 이력
uv run scripts/portfolio_stop_sensitivity.py             # 포트폴리오 stop 민감도 분석
uv run scripts/compare_filtered_vs_baseline.py           # 필터 vs 베이스라인 비교
uv run scripts/extract_top_losses.py                     # 최대 손실 거래 추출
uv run scripts/retest_excluding_tickers.py               # 특정 티커 제외 재백테스트
uv run scripts/monitor_outputs.py                        # 출력 파일 모니터링
uv run scripts/check_strategy_freeze.py                  # 동결 전략 변경 및 표본외 성과 점검
uv run scripts/walk_forward_validation.py                # 롤링 walk-forward 검증
uv run scripts/validate_etf_distributions.py             # 분배금 파일 범위·해시 점검
uv run scripts/parameter_stability.py                     # 현재 설정 주변값 안정성 검증
uv run scripts/trade_performance_attribution.py           # 비용 포함 거래별·종목별 성과 분해
uv run scripts/analyze_proxy_signal.py                    # SPY vs QQQ 시그널/포트폴리오/레짐 비교 분석
uv run scripts/sweep_proxy_match.py                       # 프록시-지수 매칭 실험 (6 시나리오)
uv run scripts/validate_proxy_stats.py                    # 통계적 검증 (bootstrap CI, 레짐 분석)
```

## 표본외 검증 기준

- `strategy_freeze.json`은 2026-07-13 종료 시점의 후보군과 실전 파라미터를 동결합니다.
- 2026-07-13까지의 데이터와 실험 결과는 모두 in-sample로 취급합니다.
- 표본외(out-of-sample) 시작일은 다음 거래일인 2026-07-14입니다.
- 후보군 또는 파라미터를 변경하면 기존 표본외 트랙과 섞지 말고 새 동결 버전을 만듭니다.
- `uv run scripts/check_strategy_freeze.py`는 현재 `.env` 포함 유효 설정의 변경 여부를 확인하고,
  `outputs_etf_only/etf_equity_curve.csv`에 표본외 관측치가 2개 이상이면 해당 성과를 출력합니다.

### Walk-forward 검증

`uv run scripts/walk_forward_validation.py`는 기본적으로 직전 3년 학습 구간에서
`리밸런싱 주기(10/20/30일) × 최대 보유 종목 수(1/2/3)` 중 Sharpe가 가장 높은 조합을 선택하고,
바로 다음 1년을 표본외로 평가합니다. 학습창은 1년씩 이동하며 결과는
`outputs_walk_forward/`에 저장됩니다. 이 분석은 전략 연구용이며 `strategy_freeze.json`의 공식
표본외 트랙을 대체하지 않습니다.

- `WF_REBALANCE_DAYS=10,20,30`
- `WF_MAX_POSITIONS=1,2,3`
- `WF_TRAIN_YEARS=3`
- `WF_TEST_YEARS=1`
- `WF_STEP_YEARS=1`
- `WF_ANCHORED=0`: `0`은 rolling window, `1`은 expanding window
- `WF_BOUNDARY_COST_PCT=0.0015`: 각 테스트 폴드 시작 시 전량 교체를 가정한 비용
- `WF_TARGET_WEIGHT_REBALANCE=0|1`: walk-forward 시 목표비중 주문 방식 사용 여부
- `WF_TRIM_OVERWEIGHT_POSITIONS=0|1`: 기존 방식에서 비중 상한 초과분 trim 여부
- `WF_MAX_ASSET_PCT=0.50`: walk-forward 전용 종목 비중 상한
- `WF_REBALANCE_BAND_PCT=0.10`: 목표비중 방식의 절대 비중 무거래 밴드
- `WF_OUTPUT_DIR=outputs_walk_forward`: 기존 결과를 보존할 별도 출력 경로 지정
- `WF_EXIT_CHECK_DAYS=0`: OOS trailing exit 점검 주기
- `WF_TRAILING_STOP_PCT=0`: OOS trailing stop 비율
- `WF_PORTFOLIO_TRAILING_STOP_PCT=0`: OOS 포트폴리오 trailing stop 비율

### 파라미터 주변값 안정성

`uv run scripts/parameter_stability.py`는 현재 설정을 중심으로 기본 27개 조합을 실행합니다.

- 리밸런싱: 현재값 ±5거래일
- 최대 보유 종목: 현재값 ±1
- 종목별 비중 한도: 현재값 ±15%p

`STABILITY_REBALANCE_DAYS`, `STABILITY_MAX_POSITIONS`, `STABILITY_MAX_ASSET_PCT`로 격자를
재정의할 수 있으며 결과는 `outputs_stability/`에 저장됩니다.

### 거래별 성과 분해

`uv run scripts/trade_performance_attribution.py`는 `outputs_etf_only/etf_trades.csv`의 비용 포함
`net_value`를 FIFO로 매칭해 완결 거래별 순손익·보유기간과 종목별 기여도를 계산합니다. 결과는
`outputs_trade_analysis/`에 저장됩니다. 예전 거래 파일처럼 `net_value`가 없으면 가격×수량으로
근사하며 요약의 `cost_aware`가 `false`로 표시됩니다.

## Python/uv 운영

- **Python 버전**: `.python-version` 기준 3.11
- **버전 변경**: `uv python install 3.12 && uv python pin 3.12 && uv sync`
- **패키지 추가**: `uv add package-name`
- **패키지 제거**: `uv remove package-name`
- **락파일 갱신**: `uv lock`

## 분배금 포함 수익률

`data/etf_distributions.csv`에 KRX KIND 또는 운용사 공시로 확인한 이벤트를 다음 스키마로
입력합니다.

```csv
ticker,ex_date,amount_per_share,payment_date,source
000000,2024-04-29,100,2024-05-03,공시 URL
```

- `ex_date`는 지급기준일이나 지급일이 아니라 실제 분배락 거래일이어야 합니다.
- `amount_per_share`는 ETF 1좌당 현금분배금(원)입니다.
- 동일 종목·분배락일의 중복 행은 합산됩니다.
- 가격 데이터에 없는 분배락일은 오류로 처리합니다.
- `ETF_RETURN_BASIS=total_return`일 때 파일이 비어 있으면 실행을 중단합니다.
- 랭킹은 분배금을 즉시 재투자한 지수를 사용하고, 포트폴리오는 분배락 직전 보유수량에
  분배금을 귀속해 당일 종가 자산에 반영합니다. 같은 날 신규 매수분에는 귀속하지 않습니다.
- 데일리 러너도 같은 total-return 랭킹을 사용하며, 실제 분배금 현금은 증권사 예수금에
  반영되므로 러너가 별도로 현금을 가산하지 않습니다.
- 현재 저장소의 CSV는 스키마 템플릿입니다. 장기 공시 이력을 채우기 전에는
  `total_return` 모드를 사용하면 안 됩니다.

분배금 파일이 바뀌면 SHA-256도 바뀌므로 `check_strategy_freeze.py`가 공식 표본외 설정 변경으로
감지합니다.

## 알려진 한계 (Known Limitations)

- `nav`는 분배형 ETF의 total return을 완전히 반영하지 않습니다. 정확한 검증에는 검증된
  전체 분배금 이력과 `total_return` 모드를 사용해야 합니다.
- `ETF_DISTRIBUTION_TAX_PCT`는 모든 분배금에 동일 세율을 적용하는 단순 모델입니다. 실제
  과표기준가별 과세액을 재현하려면 이벤트별 과세표준 데이터가 추가로 필요합니다.

## 주의사항

- pykrx 데이터는 제공처 정책/호출 제한을 준수해야 합니다.
- 백테스트는 미래 성과를 보장하지 않습니다.
- 실전 적용 전 etf_daily_runner 안전모드(LIVE_ORDER_ENABLED=0)와 소액 검증을 권장합니다.
- 데일리 러너는 `_KRX_HOLIDAYS`(2026년 KRX 공휴일 목록)로 휴장일을 감지하여 자동 중단합니다. 매년 초 갱신이 필요합니다.
