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
- 계좌: `NHPLUG_ACCT_NO` 필수 (자동 선별 불가), demo/real 환경 분리
- 토큰: `~/.nhplug/token-YYYYMMDD.json` 24h 캐시
- 레이트리밋: `NHPLUG_RATE_LIMIT` 기본 4/s, 초과 429 `IGW42902` 백오프

### KIS
- 기존 문서 유지, `KIS_APP_KEY/SECRET`, `KIS_ACCOUNT_NO` 등 참조
