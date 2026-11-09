# 모의투자 검증 절차 (live-order safety 변경 배포 전 게이트)

대상: PR #2 (`fix/live-order-fail-closed-20261104-review2`) — 취소 확정/원장/잠금/컷오프/주문 재전송 금지 변경.
이 절차는 **모의계좌(`MODE=demo`)에서만** 수행한다. 실계좌 키·계좌번호가 `.env`에 섞이지 않도록 한다.

> **KB는 모의투자를 지원하지 않는다.** KB는 이 문서의 모의 검증 대상이 아니다. `MODE=demo`로 설정해도
> 실서버/실계좌에 붙는다. 게다가 KB 어댑터의 주문 필드 매핑이 확정되지 않아(아래 KB 절차 참고) 프리플라이트는
> **KB 주문 검증 자체를 거부**한다 — [KB 절차](#kb-모의투자-미지원-주문-매핑-미확정)를 본다.
> 아래 표·절차에서 KIS/키움/NH만 검증 대상이다.

> 단위 테스트(mock)는 증권사 실제 응답 계약을 증명하지 못한다. 아래 2·4단계의 실측 값이
> 코드 가정과 다르면 **배포하지 말고** 어댑터 판정을 수정한다.

## 0. 인증 환경변수 (어댑터가 실제로 읽는 이름)

| 브로커 | 필수 | 선택/불필요 | 모드 | 비고 |
|---|---|---|---|---|
| KIWOOM | `KIWOOM_APPKEY`, `KIWOOM_SECRETKEY` | `KIWOOM_ACCOUNT_NO` (**불필요**, 비워 둠) | `MODE=demo` → `mockapi.kiwoom.com` | 키 이름에 밑줄 없음 (`APP_KEY` 아님) |
| KIS | `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO` | `KIS_ACCOUNT_PROD_CD` (기본 `01`) | `MODE=demo` → vps | 계좌 `12345678-01` 형식 가능 |
| NH | `NHPLUG_APP_KEY`, `NHPLUG_APP_SECRET`, `NHPLUG_ACCT_NO` | — | `MODE=demo` → `moapi.nhplug.com` | 토큰은 실전 도메인에서 발급. 주문은 전역 `LIVE_ORDER_ENABLED=1` 필요 |
| KB | `KBSEC_APP_KEY`, `KBSEC_APP_SECRET` | `KBSEC_IP_ADDR`, `KBSEC_MAC_ADDR` | **모의투자 미지원** (모드 분기 없음) | 항상 실서버/실계좌. 주문 필드 매핑 미확정(TODO) → **어댑터가 주문 전송을 거부**, 러너는 시작 시 중단, 프리플라이트 주문 검증 거부. 주문은 전역 `LIVE_ORDER_ENABLED=1` 필요 |

`broker_preflight.py`의 Stage 0은 **필수/선택 인증 변수**(표의 `KIS_ACCOUNT_PROD_CD`, `KIWOOM_ACCOUNT_NO` 등)만
검사한다(값은 마스킹). `KBSEC_IP_ADDR`/`KBSEC_MAC_ADDR`와 `KBSEC_BASE_URL`은 검사하지 않는다.
필수 목록은 `tests/test_broker_preflight.py`가 정확한 값과 어댑터 소스 양쪽으로 대조한다.
출력/리포트의 메모에서는 위 변수 값의 기준 문자열(원본, 앞뒤 공백 제거본, 하이픈 제거본, 6자 이상 하이픈 조각)을
**길이 제한 전에** `***`로 치환한다(`_scrub`). 본문을 원문 열과 **디코딩한 열** 두 가지로 보고(퍼센트 인코딩은
hex 대·소문자 무관 UTF-8, JSON `\uXXXX`는 서로게이트 쌍 결합, `\n \r \t \b \f \/ \" \\`, 공백의 `+`),
모든 자격증명이 나타나는 모든 구간의 **합집합**을 가린다. 따라서 자격증명끼리 겹쳐도
(한 값의 뒷부분이 다른 값의 앞부분) 남는 조각이 없다. 잘못된 UTF-8 퍼센트 바이트는 각각 자기 구간만 가리는
대체문자 1개로 취급한다. 정규식 대신 Aho-Corasick 으로 매칭하므로 역추적이 없고 자동자 저장량은 패턴 총 길이에,
전체 시간·저장량은 입력 길이+일치 수 s(+구간 정렬 O(s log s))에 비례하며, 환경변수에 디코딩 불가 바이트(서로게이트)가 있어도 예외 없이 동작한다.
**알려진 한계:** 한 메시지 안에서 같은 값의 일부 글자만 인코딩해 되울리는 혼합 표기(예: 값 `ab%41cd`를
`%61b%41cd`로 출력)는 원문 열·디코딩 열 어느 쪽에도 온전히 보이지 않아 가려지지 않을 수 있다.
값 자체가 `%41` 같은 이스케이프 모양 글자를 포함하는 경우에 한한다. 4자 미만 값(예: `KIS_ACCOUNT_PROD_CD=01`)과 파생 형태가 4자 미만이거나 `-`/공백뿐인 경우는
비밀이 아니거나 진단 메모를 망가뜨리므로 치환하지 않는다(긴 값에서 나온 짧은 조각도 포함).
그 밖의 인코딩(예: base64)이나 증권사가 값을 가공해 되돌려 주는 경우는 막지 못한다. 이 변수에 없는 값(토큰 등)이나 증권사 오류 본문은 그대로 남을 수 있으므로
로그를 첨부할 때 눈으로 한 번 확인한다.

### 모드 해석 (수정됨)
이전에는 NH가 `BROKER_MODE/ENV_MODE`만, KIS 클라이언트가 `ENV_MODE`만 읽어
`MODE=demo`만 설정하면 **인증은 모의, 주문 계층은 실전으로 동작**할 수 있었다.
이제 키움·KIS·NH가 `config_utils.get_mode()` (`MODE` > `BROKER_MODE` > `ENV_MODE`)를 따른다.
**예외:** KB는 모의투자가 없어 모드 개념이 없고(항상 실계좌), NH는 `NHPLUG_BASE_URL`이 설정되면 모드보다 우선한다
(그래서 아래 "주문 목적지 가드"가 필요하다).
프리플라이트는 `--mode` 값을 `MODE/BROKER_MODE/ENV_MODE` 모두에 덮어써서 `.env`의 `MODE=real`로
붙는 사고를 막는다.

## 1. 준비
1. 검증 커밋을 체크아웃하고 `python3 -B -m unittest tests.test_live_order_safety_guards tests.test_broker_preflight`
   가 전부 통과하는지 확인한다.
2. `.env`: `MODE=demo`, `BROKER_TYPE=<KIS|KIWOOM|NH>`, 모의 키, `LOG_LEVEL=INFO`. (KB는 아래 별도 절차)
3. 평일 장중(09:00~15:30)에 진행한다.
4. `runtime_state/live_order_intents.json`이 없는지 확인한다.

## 2. 읽기 전용 점검
```
uv run python live_trading/broker_preflight.py --broker <KIS|KIWOOM|NH> --mode demo
```
Stage 0(키 존재)·Stage 1(잔고/보유/시세/호가/주문가능) 전부 PASS여야 한다.
로그: `runtime_state/preflight_<broker>_<날짜>.json`.

## 3. 주문·취소 왕복
NH는 먼저 `LIVE_ORDER_ENABLED=1`을 설정한다(NH 어댑터가 전역 변수를 요구하며, 모의 환경에 한함).
```
uv run python live_trading/broker_preflight.py --broker <KIS|KIWOOM|NH> --mode demo --with-order
```
- 1주 시장가 매수 → 체결 대기 → 매도, 이어서 -25% 지정가 매수 → 취소.
  지정가는 현재가×0.75를 **호가단위(ETF 5원, `ETF_TICK_KRW` — KRX 규정으로 확인 필요)로 내림**한다.
  현재가를 얻지 못하거나 계산 가격이 호가단위 미만이면 임의 가격으로 주문하지 않고
  `limit_price` FAIL + `stage2_unresolved`로 중단한다.
  시장가 매수가 체결되지 않아 취소되면 왕복을 검증하지 못한 것이므로 `roundtrip` FAIL
  (incomplete)로 기록한다. 장중 유동성이 있는 시간대에 재시도한다.
- `mode=real`이면 `--allow-real`을 주지 않는 한 거부한다.
- **주문 목적지 가드:** `--mode`가 아니라 어댑터가 실제로 쓰는 URL을 **파싱해 호스트명을 정확히 비교**한다
  (키움 `mockapi.kiwoom.com`, KIS `openapivts.koreainvestment.com`, NH `moapi.nhplug.com`).
  https가 아니거나, userinfo(`user@host`)가 있거나, 호스트가 다르면(접두/접미 도메인, 경로·쿼리에 표식만
  있는 경우 포함) 주문 전에 거부한다. `mode=real`도 `--allow-real` 없이는 거부한다.
  KB는 모의 호스트가 없고 주문 필드 매핑도 미확정이라(`kb_adapter.place_order`의 "주문 구분/가격 필드명
  확정" TODO) **mode/`--allow-real`과 무관하게 주문 검증을 거부**한다(`ORDER_MAPPING_UNVERIFIED`).
  가드는 `run_preflight`뿐 아니라 `_stage2_orders` 자체에도 있어, 이 함수를 직접 호출해도
  `broker`/`mode`(필수 인자)와 같은 검사를 다시 수행하고 실패하면 `stage2-authorization` FAIL을 남긴다.
  거부 사유(잘못된 목적지·매핑 미확정·모의 미지원)는 장중/라이브 플래그 SKIP보다 **먼저** 판정되므로
  장외 시간에 실행해도 FAIL로 보고되고 종료 코드가 1이다. 유효한 모의 설정이면 가드 PASS 뒤 `place_order SKIP`이다.
- **미해결 주문 처리:** 모든 주문은 체결 또는 취소 확정까지 확인한다. "체결"은 발견됨 & 체결수량≥주문수량
  & **잔량 0**일 때만 인정한다(수량 불일치는 미해결). 취소 요청이 실패해도 최종 상태를 조회하며,
  `order_id`가 비어 있으면(접수 여부 불명) 즉시 중단한다. 확정하지 못하거나 Stage 2 안에서 예상치 못한
  예외가 나면 `stage2_unresolved` FAIL을 기록하고 이후 주문을 내지 않는다 — 이때는 증권사 앱에서
  미체결 주문과 보유 1주를 수동으로 확인·정리한다.
- 출력되는 주문 상태 필드(`is_found/order_qty/filled_qty/remaining_qty/is_filled`)를 기록한다.
- `cancel_semantics` 항목이 러너의 취소 확정 조건(발견됨 & `remaining==0` & 관측한 최대 체결수량
  `<order_qty`)과 실제 응답이 일치하는지를 자동 판정한다(체결수량은 감소하지 않는 것으로 취급).
  FAIL이면 해당 증권사는 현재 코드로 재주문을 확정하지 못한다. 러너의 나머지 경로
  (재조회 실패 시 재주문 차단 등)까지 동일하게 검증하는 것은 아니다.

| 확인 항목 | 기대 | 의미 |
|---|---|---|
| 취소 직후 `remaining_qty` | 0 | 취소 확정 조건 (0이 아니면 재주문이 막힘 — 안전하지만 기능 상실) |
| 취소 후 `filled_qty < order_qty` | True | 전량체결과 취소 구분 |
| 취소 후 `is_filled` | False | KIS는 부분체결 취소 시 True일 수 있음 (러너는 체결수량으로 재검증) |
| 없는 주문번호 조회 | `is_found=False` | 미발견은 취소 증거가 아님 |
| 취소 반영 지연 | 몇 초 | `_wait_for_cancellations` 기본 대기(5초) 충분 여부 |
| 전량 체결 직후 `is_filled` | **True** (`order_qty`>0, `filled_qty==order_qty`, `remaining_qty==0`) | 러너 완료 판정은 수량만으로 체결을 인정하지 않고 증권사의 `is_filled=True`를 요구한다. 누락/False면 체결된 주문도 미확인으로 남아 매수가 막힌다 |
| 응답의 `order_qty` | 제출 수량과 같음, 취소 후에도 그대로 | 판정 목표는 **제출 수량**이다. 취소 뒤 `order_qty`를 줄여 보고하면 보수적으로 완료 불인정(매수 차단) |
| 부분체결 취소의 `filled_qty` | 취소 시점 체결수량, 이후 조회에서 감소하지 않음 | 체결수량은 단조 증가로 취급한다. 줄어드는 응답이면 상태 불확실(`status_error`) |
| 같은 주문 반복 조회 | 서로 모순되는 값이 나오지 않음 | 취소 확정 뒤 잔량 재출현, `filled+remaining>order_qty` 등은 재주문/완료를 막는다 |

## 4. 러너 시나리오 (모의계좌: KIS/키움/NH)
러너는 **전역** `LIVE_ORDER_ENABLED=1`이 필요하다. 모의 환경에서만 설정한다.
(`{BROKER}_LIVE_ORDER_ENABLED`는 러너가 직접 읽지 않는다. `config_utils.get_live_enabled()`에는 있으나 러너는 쓰지 않으며,
GitHub 워크플로(`daily_runner*.yml`)가 이 값을 전역 `LIVE_ORDER_ENABLED`로 옮겨 줄 때만 효과가 있다. 수동 실행에서는 전역 값만 설정한다.)

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

| 9 | 부분체결 후 취소·재주문 완료 | 7과 같은 설정에서 일부만 체결되는 지정가 | 원주문 `cancel_confirmed`, 재주문 수량=제출−체결, 재주문 체결 확인 뒤 `DONE`, 원장 보관(재주문이 있으면 삭제하지 않음) |
| 10 | 재시도 끄기 + 미체결 매도 | `RETRY_UNFILLED_ORDERS=0`, 체결 안 되는 매도 | 매수 0건, 상태 `BLOCKED_BUY_BY_UNFILLED_SELL`, 이후 원장 차단 |
| 11 | 매수 미체결 | 체결 안 되는 매수 주문 | 상태 `PARTIAL_FILLED`(`DONE` 아님), 추가 주문 없음 |

시나리오 7·9의 로그에서 `[취소확인]`, `[상태재조회]`, `[재시도-차단]` 줄을 확인한다.
`stale/contradictory`, `inconsistent quantities`, `cancel reversed`, `증권사 체결 확인 없음`이 **정상 체결 흐름에서**
나오면 어댑터 응답이 코드 가정과 다르다는 뜻이다 — 중단하고 해당 응답(`last_status`)을 기록해 보고한다.

5·6 이후에는 증권사 주문내역과 대조한 뒤 원장 파일을 **삭제하지 말고 이름을 바꿔 보관**한다.

### 중단 조건 (즉시 멈추고 증권사 앱에서 확인)
- 증권사 주문내역 건수가 로그의 접수 건수보다 많다(중복 의심).
- `stage2_unresolved` FAIL 또는 `[수동대조필요]` 로그.
- `.env`의 `MODE`와 무관하게 주문 목적지 가드가 거부하거나, 실계좌로 보이는 응답(잔고·계좌번호 불일치)이 나온다.
- 한 번 멈추면 원인 분석 전까지 재실행하지 않는다. 원장 파일은 삭제하지 않는다.

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
전체 양식(표 형식, 브로커 1개당 1부)은 `docs/mock_trading_result_template.md`. 아래는 요약본이다.
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
- **NH 주문 상태 조회/취소 (2026-11-06 모의 실측 후 수정):** 첫 `--with-order`에서 시장가 매수는 체결됐으나 어댑터가
  없는 필드(`ord_no`/`ord_qty`/`ccld_qty`)를 찾아 `is_found=False`로 답했다(1주 잔존). 실응답 기준
  `itg_orr_no/orr_qty/tot_cns_qty/ny_cns_qty/can_qty`로 고쳤고, 취소 요청도 `org_mkt_orr_no/all_pat_dit_cd/cor_qty`로
  바꾸고 실패 응답(`rsp_cd`)을 "요청됨"으로 보지 않게 했다. **미검증:** 주문 응답의 `mkt_orr_no`(취소 키)와 조회 행의 `itg_orr_no`가 같은 번호인지, 취소된 주문 행이
  `can_qty`/`ny_cns_qty`를 어떻게 보고하는지. 그래서 어댑터는 두 번호를 주문별로 따로 기억하고(주문 응답에
  `itg_orr_no`가 없으면 그 주문의 상태는 항상 "확인 불가"), 조회 행의 종목·매매구분이 다르면 같은 번호여도 다른
  주문으로 본다. 취소 확정은 `can_qty>0`이고 `체결+취소+미체결=주문`일 때만 가능하며, 거부·필드 누락·수량 불일치 행은
  미체결이 남은 것으로 보고해 재주문을 막는다. 수량은 음이 아닌 정수만 인정하고(소수·음수·문자는 "알 수 없음"), 값이
  있는데 잘못된 필드는 체결/취소 확정에 쓰지 않는다. 주문번호·조회번호·시장번호 중 하나라도 겹치면(앞자리 0만 달라도 포함) 해당 주문들의 조회·취소를 모두
  막고(`[NH][주문번호 충돌]` 경고), 그 번호는 이후 주문에도 쓸 수 없다(프로세스 수명 동안).
  모든 번호는 ASCII 숫자만 인정하는 하나의 정식 표기(앞자리 0 제거)로 비교·전송하며, `+7`·전각 숫자·밑줄·공백뿐인 값은
  신원으로 쓰지 않는다(주문번호가 그런 값이면 등록하지 않아 조회는 미확정, 취소는 거부), 응답에 `mkt_orr_no`가 없거나 이 프로세스가 낸 주문이 아니면 취소 요청을 보내지 않는다.
  **실측(2026-11-09, 모의서버 --with-order 1회):** 주문 응답은 `mkt_orr_no=624`만 주고 `itg_orr_no` 필드는 없었으며, 조회 행의
  `itg_orr_no`는 624로 같은 번호였다(체결 1/1). 그래서 응답에 `itg_orr_no` 필드가 아예 없을 때만 `mkt_orr_no`를 조회 번호로 쓰고
  (필드가 있는데 잘못된 값이면 미확정), 종목·매매구분·행 유일성 검증은 그대로 적용한다. 이후 같은 날 재실행에서 `mkt_orr_no`(680/681/682) 3건 모두 응답에 `itg_orr_no`가 없었고 조회로 확정됐다(시장가 매수→매도 체결, 지정가 미체결→취소 확정, 보유 불변). 취소 접수 응답은 `rsp_cd=00192`("취소주문이완료되었습니다")이며 취소 요청에서만 성공으로 인정한다(취소 확정은 상태 재조회). 표본이 적으므로 재검증 때
  로그의 `[NH] 주문응답 번호 mkt_orr_no=… itg_orr_no=…`와 조회 행 번호가 계속 같은지 확인하고, 다르면 알려 달라(다르면 NH는 미확정으로 막힌다).
- 프리플라이트 Stage 2와 위 시나리오는 실제 증권사 모의 서버로 실행해 본 적이 없다(작성 시점 기준 mock 검증만 완료).
- KB는 모의투자가 없고 주문 필드 매핑이 미확정이라 이 게이트로 주문 동작을 검증할 수 없다. 아래 KB 절차의 선행 조건을 충족하기 전에는 KB를 운영에 쓰지 않는다.
- DEBUG 로그에는 키움·KB·NH 토큰 응답이 남을 수 있다. 검증·운영 모두 `LOG_LEVEL=INFO`.

## KB (모의투자 미지원, 주문 매핑 미확정)
KB증권은 모의투자를 제공하지 않으므로 위 2~5단계의 "모의 서버 검증"을 수행할 수 없다. 또한
`kb_adapter.place_order`는 시장가/지정가 구분 필드를 보내지 않고(시장가는 가격 생략), 코드에
"apidoc_b2c에서 주문 구분/가격 필드명 확정" TODO가 남아 있어 **의도한 주문 유형이 KB에 전달되는지
검증되지 않았다.** 따라서 프리플라이트는 KB 주문 검증(`--with-order`)을 거부한다.

- **지금 가능한 것:** 읽기 전용 점검 `uv run python live_trading/broker_preflight.py --broker KB --mode real`.
  주문은 없지만 **실계좌에 대한 호출**이다. Stage 0에 `mock_support SKIP`이 표시된다.
- **코드 차단(세 겹):** ① `kb_adapter.place_order`는 `ORDER_MAPPING_VERIFIED=False`인 동안 요청을 보내기 전에
  `KbOrderMappingUnverified`를 던진다(브로커로 아무것도 나가지 않으므로 러너는 이를 "명확한 거절"로 처리해
  원장을 UNCERTAIN으로 남기지 않는다). ② 러너는 어댑터 생성 직후, 계획 수립·주문 이전에
  `_assert_order_mapping_verified`로 중단한다(`BROKER_TYPE=KB` + `LIVE_ORDER_ENABLED=1`로 실주문이 어댑터 초기화에 도달하면 시작하지 못함. 드라이런은 허용되며, 휴장일·이미 완료·컷오프로 먼저 반환되는 경우는 이 검사 이전이지만 그때도 주문은 나가지 않는다).
  ③ 프리플라이트는 `ORDER_MAPPING_UNVERIFIED`로 주문 검증을 거부한다. ①의 플래그와 ③의 집합이
  일치하는지는 테스트가 검사한다.
- **KB를 운영에 연결하기 전 선행 조건:**
  1. 증권사 API 문서로 주문 필드 매핑(주문 구분, 가격 필드)을 확정하고 `kb_adapter.place_order`를 수정한다.
  2. 그 변경을 리뷰한 뒤 `kb_adapter.ORDER_MAPPING_VERIFIED=True`로 바꾸고 `broker_preflight.ORDER_MAPPING_UNVERIFIED`에서
     `KB`를 제거한다(코드 변경이므로 PR 승인 필요).
  3. `docs/broker_onboarding.md` 정책에 따라 사전 승인을 받은 뒤 실계좌에서
     `--broker KB --mode real --allow-real --with-order`(`LIVE_ORDER_ENABLED=1`)로 1주 테스트를 수행한다.
- **실계좌 1주 테스트의 실제 노출(승인 시):** 수량은 주문당 1주지만 **금액 상한은 없다.** 시장가 매수·매도는
  체결가 변동과 수수료·세금이 붙고, -25% 지정가 매수가 체결되면(급락 등) 1주가 추가로 남아 수동 매도가
  필요하다. `stage2_unresolved`/`roundtrip` FAIL 시 즉시 증권사 앱에서 미체결·보유를 정리한다.
- **러너 시나리오(4장)는 KB에서 수행하지 않는다.** 러너의 취소·재주문 경로(`remaining_qty` 의미 등)는 KB 응답으로
  검증된 적이 없다. 매핑이 확정되기 전에는 KB 러너가 코드로 차단된다(위 ①②). 매핑 확정 후에도
  취소·재주문 경로는 별도로 검증해야 한다.
- KB의 모의 호스트가 없다는 점은 코드로 증명할 수 없다(`KBSEC_BASE_URL` 기본값은 `developer.kbsec.com`).
  증권사 문서/담당자로 재확인한다.
