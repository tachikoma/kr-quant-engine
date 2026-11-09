# 운영 VPS 이식 계획 (KIWOOM 실전)

대상: 운영 VPS의 `cf1b383`(develop 계열) → `main`의 `25d3e0e`(PR #2 squash 머지) 이상.
수행: **사용자가 VPS에서 직접 실행**한다. 이 문서는 개발 환경에서 작성되었고 VPS를 확인하지 못했다.
`<repo>`는 VPS의 저장소 경로로 바꿔 읽는다. 명령 출력은 키·토큰·계좌번호를 가리고 공유한다.

## 0. 배경

- `cf1b383`은 `main`의 조상이다. 갈라진 이력이 없어 **fast-forward**로 올린다(cherry-pick 불필요).
- 전략 파일(`etf_shared.py`, `run_etf_backtest.py`, `strategy_freeze.json`, `etf_universe.py`)은 변경이 없다.
- 같이 올라가는 것: PR #2(주문 원장, 동시 실행 락, 마감 이후 차단, 부분체결 처리, Kiwoom 토큰 재발급) + NH/KB 어댑터, `MODE` 단일화, `broker_protocol`, `pykrx_fallback`, preflight.
- Kiwoom 실전 경로의 동작 변화: 주문 POST는 네트워크 오류에도 재전송하지 않음 / 토큰 무효(`8005`)는 재발급 후 1회 재전송 / 요율 기본 간격이 `MODE` 기준.

## 1. 하지 말 것

- **`.env.sample`을 `.env`로 복사·덮어쓰지 않는다.** 샘플 기본값이 `MODE=demo`라 실전이 모의로 바뀐다.
- 원장(`runtime_state/live_order_intents.json`)을 삭제하지 않는다. 필요하면 `.keep`으로 이름만 바꾼다.
- 주문이 나갈 수 있는 시간대(장 중, 스케줄 직전)에 작업하지 않는다.
- 문제가 생겨도 "아마 안 나갔을 것"으로 추정해 재주문하지 않는다.

## 2. 사전 확인 (읽기 전용)

```bash
cd <repo>
git rev-parse --short HEAD            # cf1b383 인지
git status --short                    # 비어 있어야 함
git branch --show-current

# 모드/주문 변수: 이름과 모드 값만 확인 (키 값은 출력하지 않음)
grep -E '^(ENV_MODE|MODE|BROKER_MODE|BROKER_TYPE)=' .env
grep -E '^(LIVE_ORDER_ENABLED|KIWOOM_LIVE_ORDER_ENABLED)=' .env

ls -la runtime_state/                 # live_order_intents.json 이 없어야 함
ls -la runtime_state/.etf_live_run.lock 2>/dev/null   # 실행 중인 프로세스 없는지
crontab -l; systemctl list-timers --all 2>/dev/null | head   # 스케줄러 종류/시각 확인
```

판정:
- `HEAD`가 `cf1b383`이 아니거나 작업 트리가 깨끗하지 않으면 **중단**하고 차이를 먼저 확인한다.
- `live_order_intents.json`이 있으면 **중단**하고 `docs/live_order_journal_runbook.md` 4절로 먼저 대조한다.
- `KIWOOM_LIVE_ORDER_ENABLED=0`이 있으면 이식 후 주문이 막힌다(브로커별 변수가 전역보다 우선). 의도인지 확인한다.
- Kiwoom 앱/HTS에서 **미체결 주문이 없는지** 직접 확인한다.

## 3. 준비 (장 마감 후, 스케줄 시각까지 충분한 여유)

```bash
# 3-1. 스케줄러 일시 정지 (cron 이면 해당 줄을 주석 처리, systemd 면 timer stop)
# 3-2. 백업 (값은 출력하지 않음)
STAMP=$(date +%Y%m%d_%H%M)
mkdir -p ~/rollout_backup_$STAMP
cp -a runtime_state ~/rollout_backup_$STAMP/runtime_state
cp -a .env ~/rollout_backup_$STAMP/.env && chmod 600 ~/rollout_backup_$STAMP/.env
git rev-parse HEAD > ~/rollout_backup_$STAMP/prev_commit.txt
```

기록해 둘 것: 이전 커밋, 백업 경로, 스케줄러 원래 설정.

## 4. 코드 반영

```bash
git fetch origin
git merge-base --is-ancestor HEAD origin/main && echo "fast-forward 가능"   # 출력이 나와야 함
git checkout main 2>/dev/null || git checkout -b main origin/main
git merge --ff-only origin/main
git rev-parse --short HEAD            # 25d3e0e 이상
uv sync
```

오프라인 테스트(자격증명 없이, 네트워크 호출 없음):

```bash
env -i PATH="$PATH" HOME="$HOME" .venv/bin/python -m unittest -v \
  tests.test_live_order_safety_guards tests.test_broker_preflight \
  tests.test_nh_adapter_order_status tests.test_kiwoom_adapter_token
```

전부 통과해야 한다(CI 기준 194개). 실패하면 **중단**한다.

## 5. 설정

```bash
# .env 에서 모드를 단일 변수로 명시 (편집기로 직접 수정, 다른 값은 건드리지 않음)
#   MODE=real
#   (ENV_MODE=real 이 남아 있으면 deprecated 경고만 나오며 동작은 같음)
#   BROKER_TYPE=KIWOOM
grep -E '^(MODE|BROKER_MODE|ENV_MODE|BROKER_TYPE)=' .env
```

- `MODE`, `BROKER_MODE`가 `ENV_MODE`보다 우선한다. `demo` 값이 하나라도 남아 있으면 실전이 모의로 읽힌다.
- 실전 주문을 켜는 변수는 기존 값을 유지한다(바꾸지 않는다).

## 6. 안전 모드 확인 실행

목적: 새 코드가 실계좌의 잔고/예수금을 정상으로 읽고 계획을 만드는지 본다. **주문은 나가지 않지만 실계좌 읽기 API를 호출한다.**
거래일 장 마감 후에 한다(휴장일에는 러너가 조기 종료해 확인이 안 된다).

```bash
LIVE_ORDER_ENABLED=0 KIWOOM_LIVE_ORDER_ENABLED=0 .venv/bin/python live_trading/etf_daily_runner.py 2>&1 | tee ~/rollout_backup_$STAMP/safe_run.log
```

확인할 로그:
- 모드가 `real`로 읽힘 (모의 호스트 주소가 나오면 **중단**)
- 잔고·예수금이 앱과 일치
- `유니버스 외 보유 매도 차단: ON`
- 예외 없이 `=== 실행 결과 ===`까지 도달
- `requires broker reconciliation`이 없음(안전 모드에서는 원래 검사하지 않는다)

그 후:
```bash
# 안전 모드 실행이 남긴 상태를 되돌린다 (당일 완료 표시, OOS 이력 등)
rm -f runtime_state/.etf_live_run.lock
cp -a ~/rollout_backup_$STAMP/runtime_state/. runtime_state/
```
텔레그램 알림이 나갈 수 있다(안전 모드 실행 알림).

## 7. 재개

1. 스케줄러를 원래 설정대로 다시 켠다.
2. 다음 실행 시각까지 로그를 볼 수 있는 상태로 둔다.

## 8. 첫 며칠 확인

| 항목 | 정상 | 이상 시 |
|---|---|---|
| 시작 직후 로그 | `requires broker reconciliation` 없음 | 원장 런북 4~5절 |
| 주문이 나간 날 | 원장 생성 → 정상 완료 후 자동 삭제 | `[안전차단]`/`[수동대조필요]` → 증권사 내역 대조 |
| 텔레그램 | 실행 알림 정상 | 알림 누락은 별도 원인 확인 |
| 보유/예수금 | 앱과 러너 상태 일치 | 불일치면 다음 실행 전 원인 정리 |

주의: 현재 `risk_on=False`라 **주문이 안 나가는 날은 원장 경로가 실행되지 않는다.** 주문이 나간 날의 로그를 최소 1회 확인해야 주문 경로가 검증된 것으로 본다.

## 9. 롤백

기준: 시작 실패, 모드 오인, 중복 주문 징후, 원치 않는 반복 차단.

1. 스케줄러 정지.
2. **원장이 있으면 먼저 대조**한다(`docs/live_order_journal_runbook.md` 4절). 구버전은 원장을 읽지 않아 롤백 즉시 차단이 사라진다.
3. `git checkout $(cat ~/rollout_backup_$STAMP/prev_commit.txt)` 후 `uv sync`.
4. 필요하면 `~/rollout_backup_$STAMP/.env`와 `runtime_state`를 복원(원장 파일은 삭제하지 않고 `.keep` 보관).
5. 스케줄러 재개 전에 증권사 보유/미체결을 확인한다.

## 10. 이식 후

- 깨끗한 실행이 쌓이고 주문이 나간 날이 1회 이상 확인되면 금액 증액 여부를 판단한다.
- 결과(날짜, 확인 항목, 이상 여부)를 `docs/`에 기록한다.

## 11. 남는 위험

- 미체결·부분체결(시나리오 7, 9, 10, 11)은 실전에서 처음 겪는다.
- 요율제한을 받은 주문 POST의 재시도는 검증하지 못했다.
- Kiwoom 토큰 `expires_in`은 86400초를 가정한다.
- 이 문서의 명령은 VPS에서 실행해 본 적이 없다. 경로, 스케줄러, 쉘 환경 차이에 맞게 조정한다.
