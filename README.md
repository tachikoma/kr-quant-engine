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
- `ETF_RETURN_BASIS=price|nav`: 랭킹 수익률 기준. `nav`는 NAV 기반 총수익률 근사(기본 `price`)
- `MIN_LISTING_DAYS`: 최소 상장 거래일 필터. 기본 `60`
- `MAX_PREMIUM_DISCOUNT`: NAV 대비 괴리율 절대값 임계. 기본 `0.02` (2%)

### 백테스트(run_etf_backtest.py)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `ETF_BACKTEST_MODE` | `single` | 실행 모드 (`single` / `experiment`) |
| `ETF_BASE_SLIPPAGE` | `0.0005` (5bp) | 기본 슬리피지 |
| `ETF_SPREAD_PCT` | `0.0005` (5bp) | 호가 스프레드 |
| `ETF_ENABLE_BENCHMARK` | `1` | KODEX200 비교 포함 여부 |
| `MAX_ASSET_PCT` | `0.50` | 자산별 최대 비중 제한 (0: 제한 없음) |
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

기본 동작에서는 종목명 조회 없이 티커를 그대로 사용하므로, 인증 정보 없이도 백테스트는 실행됩니다.

## 주요 결과물

### 백테스트 (`run_etf_backtest.py` → `outputs_etf_only/`)

**single 모드(기본):**

- `etf_equity_curve.csv` — 일별 포트폴리오 가치 (strategy + benchmark)
- `etf_trades.csv` — 체결 내역
- `performance.json` — 성과 지표 (CAGR, MDD, Sharpe 등)

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
- `data_cache/`: pykrx OHLCV parquet 캐시 (gitignored)
- `runtime_state/`: 데일리 러너 상태 (`etf_daily_state.json`, gitignored)
- `scripts/`: 분석/실험 스크립트 15개
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
uv run scripts/compare_filtered_vs_baseline.py           # 필터 vs 베이스라인 비교
uv run scripts/extract_top_losses.py                     # 최대 손실 거래 추출
uv run scripts/retest_excluding_tickers.py               # 특정 티커 제외 재백테스트
uv run scripts/monitor_outputs.py                        # 출력 파일 모니터링
uv run scripts/check_strategy_freeze.py                  # 동결 전략 변경 및 표본외 성과 점검
```

## 표본외 검증 기준

- `strategy_freeze.json`은 2026-07-13 종료 시점의 후보군과 실전 파라미터를 동결합니다.
- 2026-07-13까지의 데이터와 실험 결과는 모두 in-sample로 취급합니다.
- 표본외(out-of-sample) 시작일은 다음 거래일인 2026-07-14입니다.
- 후보군 또는 파라미터를 변경하면 기존 표본외 트랙과 섞지 말고 새 동결 버전을 만듭니다.
- `uv run scripts/check_strategy_freeze.py`는 현재 `.env` 포함 유효 설정의 변경 여부를 확인하고,
  `outputs_etf_only/etf_equity_curve.csv`에 표본외 관측치가 2개 이상이면 해당 성과를 출력합니다.

## Python/uv 운영

- **Python 버전**: `.python-version` 기준 3.11
- **버전 변경**: `uv python install 3.12 && uv python pin 3.12 && uv sync`
- **패키지 추가**: `uv add package-name`
- **패키지 제거**: `uv remove package-name`
- **락파일 갱신**: `uv lock`

## 알려진 한계 (Known Limitations)

- **총수익률 근사 한계**: 기본값(`ETF_RETURN_BASIS=price`)은 가격수익률(price return) 기준입니다. `ETF_RETURN_BASIS=nav`를 설정하면 NAV 기반으로 랭킹을 계산해 비분배형 ETF의 총수익률에 더 가깝게 근사합니다. 다만 고배당/커버드콜 등 분배형 ETF의 실제 분배금 재투자 성과는 완전히 반영되지 않으므로, 완전한 total return 검증에는 별도 분배금 이력 보충이 필요합니다.

## 주의사항

- pykrx 데이터는 제공처 정책/호출 제한을 준수해야 합니다.
- 백테스트는 미래 성과를 보장하지 않습니다.
- 실전 적용 전 etf_daily_runner 안전모드(LIVE_ORDER_ENABLED=0)와 소액 검증을 권장합니다.
