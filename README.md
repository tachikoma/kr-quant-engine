# kr_quant_engine

국내 ETF 로테이션 백테스트/드라이런 프로젝트입니다.

현재 운영 기준은 ETF 전용 시나리오이며, 실행 기준 스크립트는 run_etf_backtest.py 입니다.

## 현재 운영 방향

- 주 실행 경로: run_etf_backtest.py
- 실전 연결 전 검증 경로: live_trading/etf_dry_run.py
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

1. ETF 드라이런 실행(실주문 없음)

```bash
uv run python live_trading/etf_dry_run.py
```

## 환경 변수(.env)

- KRX_ID, KRX_PW: pykrx 인증이 필요한 환경에서 사용
- ENABLE_TICKER_NAME_LOOKUP=1: 종목명 조회를 강제로 켜고 싶을 때만 사용

기본 동작에서는 종목명 조회 없이 티커를 그대로 사용하므로, 인증 정보 없이도 백테스트는 실행됩니다.

## 주요 결과물

run_etf_backtest.py 실행 후 outputs_etf_only 경로에 생성됩니다.

- etf_equity_curve.csv
- etf_trades_slip_5bp.csv
- etf_trades_slip_10bp.csv
- etf_trades_slip_20bp.csv
- etf_trades_slip_30bp.csv
- slippage_comparison.csv

참고: 기존 산출물 폴더(outputs, outputs_mixed, outputs_small_cap)는 과거 실험 결과 보관용입니다.

## 디렉터리 가이드

- run_etf_backtest.py: ETF 백테스트 메인
- live_trading/etf_dry_run.py: 실전 전 주문 시뮬레이션
- live_trading/kiwoom_adapter.py: 키움 연동 어댑터(선택)
- outputs_etf_only/: ETF 기준 결과물
- data_cache/: 데이터 캐시

## 주의사항

- pykrx 데이터는 제공처 정책/호출 제한을 준수해야 합니다.
- 백테스트는 미래 성과를 보장하지 않습니다.
- 실전 적용 전 드라이런과 소액 검증을 권장합니다.
