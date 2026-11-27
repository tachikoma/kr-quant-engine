# P2-B-2 — Forward Shadow 운영 준비 (미활성화)

> 이 문서는 **준비 상태**만 기록한다. 실제 Forward runtime 초기화, 운영 배포, 스케줄
> 활성화, 실주문은 모두 별도 승인 대상이며 아직 수행하지 않았다.

## 1. KRX 계정 공유 정책

- 별도 KRX 계정을 만들지 않는다. 기존 `kr-quant-engine/.env`에서 **`KRX_ID` / `KRX_PW`
  두 키만 메모리로 읽는다.** `.env` 전체 로딩·복사·수정·출력은 금지한다.
- `run_forward_shadow.py --krx-env-file <path>`가 두 키만 `os.environ`에 설정하고,
  키 이름만 stderr에 출력한다. `silence_krx_login_output()`이 pykrx의 로그인 ID 출력과
  import 시점 로그인 메시지를 억제한다.
- 별도 `krx.env` 파일은 만들지 않는다.

### 설치된 pykrx 인증 동작(소스 확인)

| 항목 | 확인 결과 |
|---|---|
| 자격증명 읽는 시점 | `pykrx/website/comm/webio.py:12-14` 모듈 import 시 `build_krx_session()` → `auth.py:176`이 env에서 읽음. 따라서 두 키 설정은 pykrx import **이전**에 완료해야 함 |
| 세션 유지 | `auth.py:21-34` `KRXSession`(쿠키·`is_authenticated`·1시간 만료), `auth.py:113` 프로세스 전역 `_auth_session` |
| 재로그인 | `auth.py:42-70` `refresh()`가 기존 세션 종료 후 새 세션 생성 → 이전 세션 무효. `auth.py:164-172` CD011 중복로그인은 `skipDup=Y`로 처리 |
| 프로세스 간 충돌 | 세션은 프로세스 로컬. 클라이언트 코드에는 타 프로세스 세션 감지·차단 로직 없음 → **시간 분리로만 회피** |
| 조회 경로 | ETF·지수·종목명 등 모든 KRX 조회가 `webio.Get/Post.read()` → `get_auth_session()` 경유 |

### 충돌 관찰 기록 (2026-11-27 KST)

- 기존 KRX 사용자: 실전 `etf_daily_runner`(GitHub Actions는 `workflow_dispatch`만, 스케줄 없음),
  Track A `p2a1-data` worktree(수동), `overreaction-open/close.timer`(09:05/17:10, 소스 확인 결과
  pykrx/KRX 미사용), 기타 수동 작업. 실행 중 KRX 프로세스 없음을 확인.
- 조회 전용 프로브(13:40)와 독립 TEST E2E(13:41)는 성공. 직후 재로그인/재조회에서
  `JSONDecodeError`가 발생 → **과도한 로그인/일시 차단 징후로 판단하고 추가 요청을 중단**.
- 실전 러너의 이후 인증·조회 정상 여부는 **미확인**. 쿨다운 후 재확인이 필요하다.

## 2. 운영 승인 manifest

- `build_manifest(..., approval=...)` / `initialize_runtime(..., approval=...)` /
  CLI `--approval-file`로 승인 기록을 받는다.
- 승인 기록 필수 필드: `status=APPROVED`, `approved_at`, `approver`, `decision_ref`.
- `approval.settings`(runtime_kind, initial_capital, observation_start_date, anchor_date,
  interval/offset/phase, 관측 창 시각)를 manifest에 결속하고 `manifest_sha256`로 보호한다.
- `verify_manifest`: APPROVED는 FORWARD만 허용하고 설정 불일치·필드 누락 시 fail-closed.
  미승인 FORWARD는 `test_only_forward` 플래그가 없으면 초기화 자체를 거부한다.
  TEST 경로는 기존 `UNAPPROVED_TEST_VALUES`를 유지한다.

### 승인 기록 예시(값은 예시, 실제 승인 시 작성)

```json
{
  "status": "APPROVED",
  "approved_at": "<승인 시각 ISO-8601>",
  "approver": "<승인자>",
  "decision_ref": "<승인 근거>"
}
```

### 채택된 운영 설정

| 항목 | 값 |
|---|---|
| Baseline / 후보 A | 유지 / 비활성 |
| 초기자본 / 보유 | 5,000,000원 / 전액 현금 |
| 관측 시작일 | 2026-11-30 목표(지킬 수 없으면 소급 없이 다음 거래일 제안) |
| anchor / interval / offset | 2026-11-11 / 20 / 0 |
| 첫 결정일 | 2026-12-09 |
| 관측 창 | D 18:00 ~ 다음 거래일 09:00 KST |
| 이상 변동 | ±35% 초과 시 자동 보류 |
| runtime_kind | FORWARD |

### phase 0 근거(실제 백테스트 데이터)

`data_cache/index.parquet` 2,631행(2016-01-05~2026-09-28)에서 `(i-140)%20==0`을
`common_dates[:-1]`에 적용 → 결정 132회, 마지막 결정일 **2026-09-10**(i=2620).
거래일력으로 20거래일씩 전진 → 2026-10-14 → 2026-11-11 → **2026-12-09**.
2026-12-07은 실전 러너 경로(마지막 실행 2026-11-09 기준) 값으로 채택하지 않는다.

## 3. 독립 운영 경로·명령(미실행)

```bash
# 운영 checkout (개발 worktree와 분리)
git clone git@github.com:tachikoma/kr-quant-engine.git /home/ubuntu/kr-quant-engine-forward
cd /home/ubuntu/kr-quant-engine-forward && git checkout main && uv sync

# FORWARD runtime 초기화 (승인 기록 준비 후, 관측 시작일 18:00 KST 이전에)
uv run python run_forward_shadow.py --init --runtime-kind FORWARD \
  --runtime-root shadow_runtime/v001 \
  --initial-capital 5000000 --observation-start 2026-11-30 \
  --anchor-date 2026-11-11 --decision-offset 0 \
  --approval-file /path/to/approval.json \
  --krx-env-file /home/ubuntu/investment/kr-quant-engine/.env

# 일 실행 / 검증
uv run python run_forward_shadow.py --runtime-root shadow_runtime/v001 \
  --date YYYY-MM-DD --krx-env-file /home/ubuntu/investment/kr-quant-engine/.env
uv run python run_forward_shadow.py --status --runtime-root shadow_runtime/v001
```

## 4. Telegram

- 기존 봇 토큰 재사용 가능(신규 bot 불필요). 단, 실전 `.env`의 `TELEGRAM_CHAT_ID`는
  토픽 구분자가 없는 개인 채팅이므로 **그대로 재사용하면 실전 채널에 섞인다.**
- Shadow는 `SHADOW_TELEGRAM_BOT_TOKEN` / `SHADOW_TELEGRAM_CHAT_ID`를 별도로 읽고,
  `SHADOW_TELEGRAM_SEND=1`일 때만 발송한다. 운영 전 **전용 chat/topic ID 지정이 필요**하다.
- 발송 경로 자체는 `CapturingSink`로 검증 완료. 실제 발송은 전용 chat 확정 후 승인 대상.

## 5. 스케줄(비활성 템플릿)

`deploy/kqe-shadow.service` / `deploy/kqe-shadow.timer`는 참고용이다. 설치·enable·start 하지 않는다.
권장 실행 시각은 거래일 **18:10 KST**(18:00 확정 이후)이며, 다른 KRX 조회 후 최소 쿨다운을 둔다.
09:05/17:10의 `overreaction-*` 타이머는 KRX를 쓰지 않지만 충돌 회피를 위해 피한다.

## 6. 연속성

버전별 runtime(`vNNN`) + lineage 이관 정책을 채택한다. 원장·관측 이력·warm-up·위상을
보존하며, 이관 기능은 실제 코드/거래일력 변경 시점에 구현한다(이번 범위 외).
