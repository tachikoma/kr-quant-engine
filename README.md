# kr_quant_engine

국내 ETF 로테이션 백테스트/하루 1회 실행 프로젝트입니다.

현재 운영 기준은 ETF 전용 시나리오이며, 실행 기준 스크립트는 run_etf_backtest.py 입니다.

## 현재 운영 방향

- 주 실행 경로: run_etf_backtest.py
- 실전 연결 전 검증 경로: live_trading/etf_daily_runner.py (기본 안전모드)
- 데이터 소스: pykrx (ETF/지수 OHLCV)
- 기본 리밸런싱: 20거래일 주기
- 기본 비교 기준: KODEX 200 Buy&Hold
- 기본 실험: 슬리피지 민감도(5/10/20/30bp)

## 비권장/유지 상태

아래 스크립트는 보관 목적의 유지 상태이며 legacy/scripts 경로로 이동했습니다. 신규 분석 기준으로는 사용하지 않습니다.

- legacy/scripts/run_backtest.py
- legacy/scripts/run_mixed_backtest.py
- legacy/scripts/run_small_cap_backtest.py

## 빠른 시작

1. 의존성 동기화

```bash
uv sync
```

1. ETF 백테스트 실행

```bash
uv run python run_etf_backtest.py
```

1. ETF 하루 1회 실행 러너(기본 안전모드)

```bash
uv run python live_trading/etf_daily_runner.py
```

기본값은 안전모드(LIVE_ORDER_ENABLED=0)로 실제 주문을 전송하지 않고 계획만 생성/기록합니다.

강제 우회(위험): 컷오프 안전장치를 우회하여 실주문을 허용하려면 아래처럼 실행하세요. 실제 주문이 발생하므로 신중히 사용하세요.

```bash
python live_trading/etf_daily_runner.py --force-live
```

## 환경 변수(.env)

- KRX_ID, KRX_PW: pykrx 인증이 필요한 환경에서 사용
- ENABLE_TICKER_NAME_LOOKUP=1: 종목명 조회를 강제로 켜고 싶을 때만 사용

ETF 백테스트(run_etf_backtest.py) 관련:

- ETF_BACKTEST_MODE=single|experiment: 실행 모드(기본 single)
- ETF_BASE_SLIPPAGE=0.0005: single 모드 기본 슬리피지(기본 5bp)
- ETF_ENABLE_BENCHMARK=1: single 모드에서 KODEX200 비교 포함 여부(기본 1)

하루 1회 실행 러너 관련:

- LIVE_ORDER_ENABLED=1: 실제 주문 전송 모드 활성화(기본 0)
- WAIT_UNTIL_MARKET_OPEN=1: 장 시작 시각까지 대기 후 주문 실행(기본 1)
- DAILY_PLAN_TIME=08:50: 계획 수립 시작 시각(기본 08:50)
- MARKET_OPEN_TIME=09:00: 주문 실행 시각(기본 09:00)
- DAILY_RUN_FORCE=1: 같은 날 재실행 강제(기본 0)
- SELL_CUTOFF_TIME=09:05: 매도 체결 확인/취소 컷오프 시각(기본 09:05)
- BUY_CUTOFF_TIME=09:10: 매수 체결 확인/취소 컷오프 시각(기본 09:10)
- ORDER_POLL_INTERVAL_SEC=2: 체결 조회 주기(초)
- SELL_FILL_TIMEOUT_SEC=300: 매도 체결 확인 최대 대기(초)
- BUY_FILL_TIMEOUT_SEC=300: 매수 체결 확인 최대 대기(초)
- CANCEL_UNFILLED_ORDERS=1: 컷오프 시 미체결 주문 취소 여부(기본 1)
- RETRY_UNFILLED_ORDERS=1: 미체결 잔량 재주문 1회 수행 여부(기본 1)
- RETRY_ORDER_TYPE=MARKET: 재주문 유형(기본 MARKET)
- RETRY_FILL_TIMEOUT_SEC=90: 재주문 체결 확인 최대 대기(초)

키움 주문 API 연동 시 추가:

- KIWOOM_ORDER_ENDPOINT
- KIWOOM_ORDER_STATUS_ENDPOINT
- KIWOOM_ORDER_CANCEL_ENDPOINT

권장 기본값(첨부 스펙 kt10000~kt10003 기준):

- KIWOOM_ORDER_ENDPOINT=/api/dostk/ordr
- KIWOOM_ORDER_CANCEL_ENDPOINT=/api/dostk/ordr
- KIWOOM_ORDER_BUY_API_ID=kt10000
- KIWOOM_ORDER_SELL_API_ID=kt10001
- KIWOOM_ORDER_CANCEL_API_ID=kt10003
- KIWOOM_DMST_STEX_TP=KRX
- KIWOOM_ORDER_TICKER_KEY=stk_cd
- KIWOOM_ORDER_QTY_KEY=ord_qty
- KIWOOM_ORDER_PRICE_KEY=ord_uv
- KIWOOM_ORDER_TYPE_KEY=trde_tp
- KIWOOM_ORDER_CANCEL_ID_KEY=orig_ord_no
- KIWOOM_ORDER_CANCEL_TICKER_KEY=stk_cd
- KIWOOM_ORDER_CANCEL_QTY_KEY=cncl_qty
- KIWOOM_ORDER_STATUS_ENDPOINT=/api/dostk/acnt
- KIWOOM_ORDER_STATUS_API_ID=kt00007
- KIWOOM_ORDER_STATUS_QRY_TP=1  (1:주문순, 4:체결내역만)
- KIWOOM_ORDER_STATUS_LIST_KEY=acnt_ord_cntr_prps_dtl

주문유형 참고:

- MARKET -> trde_tp=3
- LIMIT -> trde_tp=0
- IOC/FOK는 RETRY_ORDER_TYPE 또는 주문 호출값을 숫자 코드(예: 13, 23)로 지정 가능

선택 매핑 키(계좌별 응답 차이 대응):

- KIWOOM_ORDER_TICKER_KEY, KIWOOM_ORDER_QTY_KEY
- KIWOOM_ORDER_PRICE_KEY, KIWOOM_ORDER_TYPE_KEY, KIWOOM_ORDER_ACCOUNT_KEY
- KIWOOM_ORDER_ID_PATH
- KIWOOM_ORDER_STATUS_ACCOUNT_KEY
- KIWOOM_ORDER_CANCEL_ID_KEY, KIWOOM_ORDER_CANCEL_QTY_KEY, KIWOOM_ORDER_CANCEL_ACCOUNT_KEY

기본 동작에서는 종목명 조회 없이 티커를 그대로 사용하므로, 인증 정보 없이도 백테스트는 실행됩니다.

## 주요 결과물

run_etf_backtest.py 실행 후 outputs_etf_only 경로에 생성됩니다.

single 모드(기본):

- etf_equity_curve.csv
- etf_trades.csv
- performance.json

experiment 모드(ETF_BACKTEST_MODE=experiment):

- etf_equity_curve.csv
- etf_trades_slip_5bp.csv
- etf_trades_slip_10bp.csv
- etf_trades_slip_20bp.csv
- etf_trades_slip_30bp.csv
- slippage_comparison.csv

참고: 기존 산출물 폴더(outputs, outputs_mixed, outputs_small_cap)는 과거 실험 결과 보관용입니다.

## 디렉터리 가이드

- run_etf_backtest.py: ETF 백테스트 메인
- live_trading/etf_daily_runner.py: 실전 전 주문 계획/실행 러너(기본 안전모드)
- live_trading/kiwoom_adapter.py: 키움 연동 어댑터(선택)
- outputs_etf_only/: ETF 기준 결과물
- data_cache/: 데이터 캐시
- runtime_state/: 실전 러너 상태 파일(etf_daily_state.json)

## 주의사항

- pykrx 데이터는 제공처 정책/호출 제한을 준수해야 합니다.
- 백테스트는 미래 성과를 보장하지 않습니다.
- 실전 적용 전 etf_daily_runner 안전모드(LIVE_ORDER_ENABLED=0)와 소액 검증을 권장합니다.
