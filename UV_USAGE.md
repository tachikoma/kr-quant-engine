# uv 사용 가이드 (ETF 운영 기준)

## 1. 최초 실행

```bash
uv sync
uv run python run_etf_backtest.py
```

## 2. 권장 실행 명령

- ETF 백테스트

```bash
uv run python run_etf_backtest.py
```

- ETF 드라이런(실주문 없음)

```bash
uv run python live_trading/etf_dry_run.py
```

## 3. 가상환경 동작

uv sync 실행 시 uv가 가상환경을 관리하며, uv run으로 별도 활성화 없이 실행합니다.

## 4. Python 버전 관리

.python-version 기준 버전은 3.11입니다.

버전을 바꾸려면:

```bash
uv python install 3.12
uv python pin 3.12
uv sync
```

## 5. 의존성/락파일 관리

- 패키지 추가

```bash
uv add package-name
```

- 패키지 제거

```bash
uv remove package-name
```

- 락파일 갱신

```bash
uv lock
```

## 6. 운영 메모

- 현재 프로젝트의 주 실행 경로는 run_etf_backtest.py 입니다.
- legacy/scripts/run_backtest.py, legacy/scripts/run_mixed_backtest.py, legacy/scripts/run_small_cap_backtest.py는 유지 상태이며 신규 실험 기준으로는 사용하지 않습니다.
