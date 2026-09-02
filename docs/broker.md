# Broker 연동 및 운영

## 개요
- KIS: `live_trading/kis_adapter.py` + `live_trading/kis/` (KisApiClient)
- NH: `live_trading/nh_adapter.py` (PLUG REST, 독립 구현)
- 공통 실행: `live_trading/etf_daily_runner.py` (BROKER_TYPE으로 분기)

## GitHub Actions

### 워크플로우
- `daily_runner.yml` (KIS): `repository_dispatch` `run-daily` / `daily-run` job, env `kis-demo`
- `daily_runner_nh.yml` (NH): `repository_dispatch` `run-daily-nh` / `daily-run` job, env `nh-demo`, concurrency `etf-daily-nh-demo`

### Dispatch (cron-job.org)
- 외부 스케줄러: **cron-job.org**에서 매일 08:56 KST (23:56 UTC 전일)에 `POST https://api.github.com/repos/tachikoma/kr-quant-engine/dispatches`
  - Header: `Authorization: Bearer <PAT>`, `Accept: application/vnd.github+json`
  - Body KIS: `{"event_type":"run-daily"}`
  - Body NH: `{"event_type":"run-daily-nh"}`
- 워크플로우 내부에는 `on.schedule` 없음 — 외부 dispatch에만 의존
- 수동 실행: Actions → `ETF Daily Runner for KIS/NH` → `Run workflow` (force_live/force_rebalance/force_run 옵션)
- REBALANCE_STEP_DAYS: KIS `20` (기본값), NH `3` (nh-demo vars, 의도적 단기 검증용)

### 모니터링
- 정상: 매일 Telegram 요약 수신 (`DONE`/`NO_ACTION` 등). 미수신 시 장애 의심
- 확인: `gh run list --workflow=daily_runner_nh.yml --limit 5` / `gh run view <id> --log`
- 최근 NH 정상: 2026-08-30, 2026-08-31 08:56 KST `success` (repository_dispatch)
- 로그 레벨: 모의 환경은 `DEBUG` 유지, 실전 전환 시 `INFO` 권장

### Node 버전
- 2026-08-31 기준 `actions/checkout@v5`, `astral-sh/setup-uv@v6`, `actions/setup-python@v6` 으로 Node 24 대응 완료
- `actions/cache/restore/save@v4`는 아직 Node20 타깃이나 동작에는 영향 없으며, cache 액션 업데이트 시 추가 제거 예정

## 브로커별 상세

### NH (PLUG)
- Base URL: `https://api.nhplug.com:8443` (실전), `https://moapi.nhplug.com:8443` (모의, `MODE=demo`일 때 자동)
- Auth URL: 항상 `https://api.nhplug.com:8443` (토큰은 live에서만 발급)
- 계좌: `NHPLUG_ACCT_NO` 필수 (자동 선별 불가), demo/real 환경 분리 — 하이픈 포함 표기(`500-01-002017`)도 자동 정규화(숫자만)하여 `act_no`로 전송, 8~13자리 검증
- 토큰: `~/.nhplug/token-YYYYMMDD.json` 24h 캐시
- 레이트리밋: `NHPLUG_RATE_LIMIT` 기본 4/s, 초과 429 `IGW42902` 백오프
- 주문: `cashBuy/cashSell`은 `trading-bot-kis`와 동일한 `orr_qty(int)/orr_pr(int)/rmt_mkt_cd` 등 필수 필드를 포함, 기존 `ord_qty/ord_uv` 별칭도 호환 유지 — `11166 계좌번호 오류`는 페이로드 불일치 시에도 발생할 수 있음
- **모의(demo) currentPrice 미지원**: `moapi`의 `/krstock/quote/v1/currentPrice`는 `IGW40023`(모의투자에서 제공하지 않는 API)을 반환합니다. balance/주문 API는 정상 동작합니다.
  - **가격 라우팅 (NH demo 전용)**: demo(moapi)에서 quote(`/krstock/quote/v1/*`) 엔드포인트는 moapi를 먼저 호출하지 않고 **동일 토큰으로 실전 API(`api.nhplug.com:8443`)에 직접 라우팅**합니다. (토큰은 live에서만 발급되며 양쪽 모두 유효 — `[NH] demo quote direct to real API` 로그). moapi의 불필요한 `IGW40023` 호출(약 8~12s + 로그 스팸)을 제거합니다.
  - **가격 fallback 체인 (NH demo 전용)**:
    1. 실전 API quote 호출이 실패하면(IGW40023/401/네트워크) runner의 pykrx 어제종가 fallback으로 이어집니다.
    2. 보유종목은 balance `Output_1[].now_pr`(브로커 권위), 유니버스/후보는 pykrx 어제종가(`live_trading/pykrx_fallback.py`, `runtime_state/last_valid_prices.json` 캐시 TTL 1일).
  - **격리 보장**: 실전 API 라우팅은 **quote(`/krstock/quote/v1/*`) 엔드포인트에만** 적용됩니다. 주문/잔고/취소/조회는 항상 `moapi`(모의 격리)에 남습니다.
  - **KRX_ID/KRX_PW 필수** — pykrx 어제종가 fallback이 동작하려면 `.env`에 KRX 인증 정보가 필요합니다.
  - 실전(`MODE=real`)은 실전 API만 사용하며(재시도 없음) pykrx fallback을 사용하지 않습니다. 현재가 누락 시 예외로 종료합니다(fail loud).

### KIS
- 기존 문서 유지, `KIS_APP_KEY/SECRET`, `KIS_ACCOUNT_NO` 등 참조
