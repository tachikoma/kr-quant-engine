# 모의투자 검증 절차 (live-order safety 변경 배포 전 게이트)

대상: PR #2 (`fix/live-order-fail-closed-20261104-review2`) — 취소 확정/원장/잠금/컷오프/주문 재전송 금지 변경.
이 절차는 **모의계좌(`MODE=demo`)에서만** 수행한다. 실계좌 키·계좌번호가 `.env`에 섞이지 않도록 한다.

> 단위 테스트(mock)는 증권사 실제 응답 계약을 증명하지 못한다. 아래 2·4단계의 실측 값이
> 코드 가정과 다르면 **배포하지 말고** 어댑터 판정을 수정한다.

## 0. 인증 환경변수 (어댑터가 실제로 읽는 이름)

| 브로커 | 필수 | 선택/불필요 | 모드 | 비고 |
|---|---|---|---|---|
| KIWOOM | `KIWOOM_APPKEY`, `KIWOOM_SECRETKEY` | `KIWOOM_ACCOUNT_NO` (**불필요**, 비워 둠) | `MODE=demo` → `mockapi.kiwoom.com` | 키 이름에 밑줄 없음 (`APP_KEY` 아님) |
| KIS | `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO` | `KIS_ACCOUNT_PROD_CD` (기본 `01`) | `MODE=demo` → vps | 계좌 `12345678-01` 형식 가능 |
| NH | `NHPLUG_APP_KEY`, `NHPLUG_APP_SECRET`, `NHPLUG_ACCT_NO` | — | `MODE=demo` → `moapi.nhplug.com` | 토큰은 실전 도메인에서 발급. 주문은 전역 `LIVE_ORDER_ENABLED=1` 필요 |
| KB | `KBSEC_APP_KEY`, `KBSEC_APP_SECRET` | `KBSEC_IP_ADDR`, `KBSEC_MAC_ADDR` | **코드상 모드 분기 없음** | 모의/실전은 `KBSEC_BASE_URL`로 구분해야 함(미검증). 주문은 전역 `LIVE_ORDER_ENABLED=1` 필요 |

`broker_preflight.py`의 Stage 0은 위 목록을 검사한다(값은 마스킹). 목록은
`tests/test_broker_preflight.py`가 어댑터 소스와 대조해 어긋남을 막는다.

### 모드 해석 (수정됨)
이전에는 NH가 `BROKER_MODE/ENV_MODE`만, KIS 클라이언트가 `ENV_MODE`만 읽어
`MODE=demo`만 설정하면 **인증은 모의, 주문 계층은 실전으로 동작**할 수 있었다.
이제 모든 어댑터가 `config_utils.get_mode()` (`MODE` > `BROKER_MODE` > `ENV_MODE`)를 따른다.
프리플라이트는 `--mode` 값을 `MODE/BROKER_MODE/ENV_MODE` 모두에 덮어써서 `.env`의 `MODE=real`로
붙는 사고를 막는다.

## 1. 준비
1. 검증 커밋을 체크아웃하고 `python3 -B -m unittest tests.test_live_order_safety_guards tests.test_broker_preflight`
   가 전부 통과하는지 확인한다.
2. `.env`: `MODE=demo`, `BROKER_TYPE=<브로커>`, 모의 키, `LOG_LEVEL=INFO`.
3. 평일 장중(09:00~15:30)에 진행한다.
4. `runtime_state/live_order_intents.json`이 없는지 확인한다.

## 2. 읽기 전용 점검
```
uv run python live_trading/broker_preflight.py --broker <KIS|KIWOOM|NH|KB> --mode demo
```
Stage 0(키 존재)·Stage 1(잔고/보유/시세/호가/주문가능) 전부 PASS여야 한다.
로그: `runtime_state/preflight_<broker>_<날짜>.json`.

## 3. 주문·취소 왕복
NH/KB는 먼저 `LIVE_ORDER_ENABLED=1`을 설정한다(모의 환경에 한함).
```
uv run python live_trading/broker_preflight.py --broker <...> --mode demo --with-order
```
- 1주 시장가 매수 → 체결 대기 → 매도, 이어서 -25% 지정가 매수 → 취소.
- `mode=real`이면 `--allow-real`을 주지 않는 한 거부한다.
- 출력되는 주문 상태 필드(`is_found/order_qty/filled_qty/remaining_qty/is_filled`)를 기록한다.
- `cancel_semantics` 항목이 **러너의 취소 확정 조건**(발견됨 & `remaining==0` & `filled<order_qty`)과
  실제 응답이 일치하는지를 자동 판정한다. FAIL이면 해당 증권사는 현재 코드로 재주문을 확정하지 못한다.

| 확인 항목 | 기대 | 의미 |
|---|---|---|
| 취소 직후 `remaining_qty` | 0 | 취소 확정 조건 (0이 아니면 재주문이 막힘 — 안전하지만 기능 상실) |
| 취소 후 `filled_qty < order_qty` | True | 전량체결과 취소 구분 |
| 취소 후 `is_filled` | False | KIS는 부분체결 취소 시 True일 수 있음 (러너는 체결수량으로 재검증) |
| 없는 주문번호 조회 | `is_found=False` | 미발견은 취소 증거가 아님 |
| 취소 반영 지연 | 몇 초 | `_wait_for_cancellations` 기본 대기(5초) 충분 여부 |

## 4. 러너 시나리오 (모의계좌)
러너는 `LIVE_ORDER_ENABLED=1`(또는 `{BROKER}_LIVE_ORDER_ENABLED=1`)이 필요하다. 모의 환경에서만 설정한다.

| # | 시나리오 | 방법 | 기대 결과 |
|---|---|---|---|
| 1 | 정상 실행 | 장 시작 직후 `uv run python live_trading/etf_daily_runner.py` | 체결 후 원장 삭제, 상태 `DONE` |
| 2 | 컷오프 이후 | 컷오프 이후 실행 | 주문 0건, 차단 로그 |
| 3 | 컷오프 이후 명시 승인 | `--force-live` | 연장된 컷오프 내에서만 주문 |
| 4 | 동시 실행 | 터미널 2개에서 동시 실행 | 한쪽 `already active` 종료 |
| 5 | 원장 차단 | `runtime_state/live_order_intents.json` 임시 생성 후 실행 | 주문 전 실패 |
| 6 | 강제 종료 | 주문 직후 `kill -9` | 원장 잔존, 재실행 차단 |
| 7 | 미체결 취소·재주문 | 체결 안 되는 조건 + `CANCEL_UNFILLED_ORDERS=1 RETRY_UNFILLED_ORDERS=1` | 취소 확정 후에만 `requested-filled` 수량 재주문 |
| 8 | DRY_RUN | `LIVE_ORDER_ENABLED=0` | 주문 0건, 원장 미생성 |

5·6 이후에는 증권사 주문내역과 대조한 뒤 원장 파일을 **삭제하지 말고 이름을 바꿔 보관**한다.

## 5. 요율제한·인증 만료 재시도
어댑터는 주문 전송의 네트워크 오류/5xx 재전송을 하지 않지만, 요율제한 코드(KIS `EGW00201/215` 등)와
토큰 만료는 "미접수" 응답으로 보고 재시도한다. 이 가정을 확인한다.
- 짧은 간격의 연속 호출로 요율제한을 유도한 뒤 주문이 **1건만** 생기는지 확인.
- 토큰을 무효화한 뒤 주문해 재발급 후 1건만 접수되는지 확인.
- 증권사 주문내역 건수와 로그를 대조한다. 2건이면 즉시 중단하고 보고한다.

## 6. 통과 기준과 기록
- 중복 주문 0건, 원장/상태 파일이 시나리오 기대와 일치, 3단계 표가 코드 가정과 일치.
- PR에 프리플라이트 JSON/로그와 시나리오별 결과를 첨부한다. 주문번호는 필요한 만큼만,
  계좌번호·토큰·키는 가린다.

### 결과 기록 템플릿
```
브로커/모드/커밋:
일시(KST):
[2] 읽기 전용 PASS/FAIL:
[3] cancel_semantics: PASS/FAIL  (취소 후 상태: found= order= filled= remaining= is_filled=)
[4] 시나리오 1~8: 
[5] 요율제한/토큰 만료 재시도 중복 여부:
이상 징후:
판정: PASS / FAIL
```

## 알려진 한계
- 프리플라이트 Stage 2와 위 시나리오는 실제 증권사 모의 서버로 실행해 본 적이 없다(작성 시점 기준 mock 검증만 완료).
- KB는 모의 환경 구분 방법이 코드에 없다. 사용 전 별도 확인 필요.
- DEBUG 로그에는 키움·KB·NH 토큰 응답이 남을 수 있다. 검증·운영 모두 `LOG_LEVEL=INFO`.
