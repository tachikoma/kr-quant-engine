# 브로커 온보딩 절차 (문서/절차 게이트)

> 코드 강제 게이트 없음. 신규 브로커는 본 체크리스트를 PR에 첨부·승인받아야 `BROKER_TYPE=<신규>` 로 데모 진입 가능.
> GitHub Actions / VPS 이원 운영 모두 본 문서 절차를 따른다.

## 1. 목적

신규 브로커 어댑터(`live_trading/*_adapter.py`) 추가 시 모의투자로 바로 진입하지 않고,
**잔고조회 → 주문내역조회 → 주문 → 취소(원복) → 잔고 원복 확인**의 가역 시나리오를 통과한 뒤에만 전략을 연결한다.

## 2. 대상 구분

| 구분 | 예시 | 게이트 강도 | 실패 시 |
|---|---|---|---|
| 신규 | NH, KB, 향후 토스/미래에셋 등 | 필수 (데모 진입 전 PR 승인) | 데모 진입 불가 |
| 기존 | KIS, Kiwoom | 권장 (리트로핏·주기적 회귀) | 경고만, 운영 유지 |

기존 브로커는 본 문서를 1회 회귀 기록으로 사용한다. 인터페이스(`AbstractBroker` 개념) 리트로핏 후 1회 실행으로 충분.

## 3. 사전조건

- [ ] `.env` / GitHub Environment Secrets 분리 확인 (실계좌 키와 모의 키 혼용 금지)
- [ ] 샌드박스/모의투자 환경 우선 확인
  - KIS: `https://openapi.koreainvestment.com:9443` (모의 전용 도메인/앱키)
  - NH PLUG: `MODE=demo` → `https://moapi.nhplug.com:8443` (자동), 토큰은 `api.nhplug.com` 발급
  - Kiwoom/NH/KB 등 모의 미제공 시에만 실계좌 1주 테스트로 폴백 (사전 승인 필요)
- [ ] 장중(09:00~15:30 KST) + 거래일(`_is_trading_day()` 휴장일 참고) 확인 — 시간외/휴장일은 주문 테스트 스킵

## 4. 절차 (Stage 0~3, 수동)

### Stage 0 — Pre-check (로그 캡처)

```bash
# 자격증명·장시간 확인 (예시)
uv run python -c "from live_trading.etf_daily_runner import _is_trading_day; print(_is_trading_day())"
# 브로커 health (서버시간/시세 1회 조회로 대체 가능)
```

- [ ] 계좌번호 마스킹 확인 (로그에 `****` 처리)
- [ ] 장중/거래일 PASS

### Stage 1 — Read-only (멱등, 필수)

브로커 어댑터 직접 호출 또는 `etf_daily_runner --dry-run` 로그로 대체 가능.

> Helper: `uv run python live_trading/broker_preflight.py --broker NH --mode demo` (기본 read-only, 전략 사용 API 전수 검증 — `get_cash`/`get_holdings`/`get_prices`/`get_bid_ask_prices`/`get_buyable_info`/`get_ticker_name`)

- [ ] `get_balance()` 성공 — 현금/보유종목/평가액 파싱 오류 0건 (원시 응답 저장, 민감필드 제외)
- [ ] `get_orders(status=all, days=1)` 성공 — 페이징/필드 매핑 확인
- [ ] 필요 시 `get_positions()` 수량/평단가 일관성 확인

> VPS: `uv run python live_trading/etf_daily_runner.py` (safe mode, `LIVE_ORDER_ENABLED=0`) 로그로 갈음 가능
> Actions: `workflow_dispatch` 수동 실행 로그 첨부

### Stage 2 — Write 가역 테스트 (장중 1회, 필수 — 신규)

**원칙: 1주 · 시장가 · IOC/FOK · 즉시 원복**

> Helper: `uv run python live_trading/broker_preflight.py --broker NH --mode demo --with-order` (장중 Buy→Sell 1주 라운드트립 + 지정가 미체결→`cancel_order` 테스트. `place(buy)`→`get_status`→`place(sell)`→`get_status`→`place(limit 75%)`→`get_status`→`cancel`→취소확인→평탄화. 모의계좌 권장)

1. 테스트 전 `get_balance()` 스냅샷 저장
2. `069500` (KODEX 200) 등 유동성 높은 ETF 1주 매수 주문 (시장가 IOC 권장 — 미체결 시 자동 취소)
3. 체결 시 즉시 반대매매(매도 1주) 또는 `cancel_order()` 수행
4. 테스트 후 `get_balance()` 스냅샷과 비교 — 평탄화(flat) 확인
5. 미평탄 시 `cancel_all_orders()` + 반대매매로 강제 원복 후 재확인

- [ ] 주문 → 체결/거부 → 취소/반대매매 → 평탄화 전 과정 성공
- [ ] 1주 매매 비용(약 200~500원) 감수 기록
- [ ] 잔여 포지션 0 확인 (스냅샷 diff 첨부)

> 모의계좌면 실비용 없음. 실계좌 폴백 시에만 소액 비용 발생.

### Stage 3 — 연동 Smoke (선택)

- [ ] `uv run python live_trading/etf_daily_runner.py --help` / `--dry-run` 1회 완주
- [ ] Telegram 요약 수신 확인 (`DONE`/`NO_ACTION` 등)

## 5. 기록 양식 (PR 첨부)

PR 본문 또는 `runtime_state/preflight_<broker>_<YYYYMMDD>.log` (gitignored)에 아래 첨부:

```
브로커: NH / MODE: demo / 일시: 2026-09-01 13:20 KST (장중)
Stage0: PASS (거래일 OK)
Stage1: PASS (balance/orders 파싱 OK) — 로그 20줄 첨부
Stage2: PASS (069500 1주 IOC 매수→체결→매도 원복, 전후 현금 동일)
  - 전: cash 10,000,000 / 후: cash 9,999,712 (수수료 288원)
  - 잔여포지션: 0
Stage3: PASS (dry-run DONE)
원시응답: gist 또는 로그 파일 첨부 (계좌번호 마스킹)
```

## 6. 승인 및 데모 진입

- [ ] 리뷰어 1명 승인 (PR `Approved`)
- [ ] 승인 후에만 `BROKER_TYPE=<신규>` 로 Actions env (`nh-demo` 등) 또는 VPS `.env`에 등록
- [ ] 첫 3일간 GitHub Actions `daily_runner*.yml` 성공 + Telegram 수신 모니터링

## 7. 운영 이원화 메모

- **GitHub Actions**: `cron-job.org` → `repository_dispatch` (`run-daily`/`run-daily-nh`) — 본 절차 통과 후에만 신규 `event_type` 추가. 수동 검증은 Actions → Run workflow (force 옵션 off)로 수행
- **VPS 직접 실행**: `uv run python live_trading/etf_daily_runner.py` (safe mode 기본) — 본 체크리스트 PR 링크를 배포 메모에 남김. `--force-live`는 실전 전환 시에만 사용

## 8. 기존 브로커 회귀 (KIS/Kiwoom)

- 본 문서 Stage 1까지 1회 실행 후 PR 코멘트로 기록 (차단 아님)
- 이후 주 1회 또는 어댑터 변경 PR 시에만 재실행

---

*관련: `docs/broker.md` (엔드포인트·인증·레이트리밋 상세), `live_trading/etf_daily_runner.py` (안전모드 기본), `strategy_freeze.json` (전략 동결)*
