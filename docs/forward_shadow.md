# P2-B-1 — Forward Shadow MVP (설계·운영 인계 문서)

## 1. 범위와 원칙

- **독립 트랙(Track B)**: 동결된 ETF 모멘텀 v2 Baseline의 forward shadow만 구현한다. 새 전략
  엔진, 전략 최적화, 후보 A 채택, 아키텍처 확장은 하지 않는다.
- **재사용**: 랭킹·gating·주문생성·비용은 `etf_shared.py`, capacity 판정은 `etf_execution.py`,
  ETF 조회·정규화는 `pykrx_utils.py`를 그대로 호출한다. 체결 적용 순서는
  `run_etf_strategy(execution_mode="ohlcv_capacity")`를 따른다. 백테스트의 체결 헬퍼는 함수 내부
  클로저라 import할 수 없어 `engine.py`에 대응 구현을 두고, **불일치 0건**을 테스트로 강제한다.
  새 정책은 정의하지 않는다.
- **안전**: 실주문 API·브로커 어댑터·`run_etf_backtest`(import 시 `.env` 로드)를 import하지 않는다.
  실전 checkout, Track A worktree, `strategy_freeze.json`, cron/systemd는 변경하지 않는다.

## 2. 구현 구조

```
forward_shadow/
  config.py            freeze→실효 파라미터, 실효 설정 검증, manifest 생성/검증
  trading_calendar.py  KRX 거래일력(2026~2027), 범위 밖 차단, 고정 리밸런싱 위상
  warmup.py            warm-up bundle 검증·hash 고정
  market.py            provider, 데이터 검증, MA120/slope20, hash 고정 snapshot
  engine.py            신호(D) → 체결(D+1), carry, 비용·현금·원가, 평가
  store.py             실행 잠금, hash-chain ledger, COMMIT replay 복구, genesis 결속
  report.py            원장 기반 리포트 생성·복구, 독립 Telegram 경로
  runner.py            하루 처리, 관측 창, 휴장 등록, 읽기 전용 상태 검증
run_forward_shadow.py  별도 entrypoint
```

runtime root(`shadow_runtime/`, gitignored): `manifest.json`, `genesis.json`, `state.json`,
`events.jsonl`, `warmup.json`, `snapshots/<date>.json`, `reports/`, `cache/`, `.shadow.lock`.

manifest schema는 2다. `b95a65b`에서 만든 schema 1 runtime은 사용할 수 없다(새로 초기화).

## 3. TEST / FORWARD 분리

`--init --runtime-kind`로 고정되며 manifest hash와 GENESIS 이벤트에 결속되어 변경할 수 없다.

| | TEST | FORWARD |
|---|---|---|
| provider | fixture 또는 pykrx | **pykrx(live)만** |
| warm-up | 호출자가 파일 제공 | **live 조회만**(파일 거부) |
| 처리 시점 | 제한 없음 | 관측 창 안에서만 정상 관측 |
| 소급 시작 | 가능 | 시작일 관측 창이 닫혔으면 거부 |
| 관측 구분 | `TEST` | `FORWARD` / `RECOVERY` |

**관측 창**: `[D 18:00 KST, 다음 거래일 09:00 KST)`. 두 시각은 `UNAPPROVED_TEST_VALUE`다.
마감의 "다음 거래일"은 임시휴장 등록을 무시한 기본 거래일력으로 계산한다(미검증 등록이 창을
늘리지 못한다. 실제 임시휴장 전날은 그만큼 창이 짧아진다).

| 처리 시각 | 결과 |
|---|---|
| 창 이전(장중·미래 날짜) | `REJECTED_NOT_CONFIRMED`, 원장에 아무것도 기록하지 않음 |
| 창 안 | `FORWARD` |
| 창 이후 | `BLOCKED_STALE`. `--recover`를 명시하면 `RECOVERY`로 기록 |
| COMMIT 전에 창이 닫힘 | 진행분 `ABORT`. `--recover` 없으면 `BLOCKED_STALE`, 있으면 `RECOVERY` |
| COMMIT 확정 후 읽은 시각이 창 이후 | 처리는 유지하고 `RECOVERY`로 기록 |
| COMMIT 후 관측 확정 전에 중단 | 다음 실행에서 `RECOVERY`로 확정 |

FORWARD 일자는 먼저 `UNCONFIRMED`로 COMMIT되고, **COMMIT이 디스크에 확정된 뒤** 읽은 시각이
창 안일 때만 별도 COMMIT으로 `FORWARD`가 된다. `--status`는 `UNCONFIRMED`를 불일치로 보고한다.

`RECOVERY` 일자는 state·원장·리포트의 `observation_counts`에 남으며 forward 관측과 구분된다.

## 4. 거래일 연속성과 리밸런싱 위상

- 처리 대상은 항상 "마지막 **정상 처리일**의 다음 거래일"(최초는 관측 시작일)이다. 건너뛰면
  `BLOCKED_GAP`(누락일 목록 포함)이며 현금·보유·신호·carry는 변하지 않는다.
- `BLOCKED_*`는 처리일이 아니다. `last_attempt`에만 남는다.
- 신호와 carry는 실행 예정일에 결속된다. 다른 날짜에는 실행을 거부한다.
- 결정일 = `(anchor로부터의 거래일 수 - offset) % 20 == 0`. 처리 성공 여부와 무관하다.
- 임시 휴장은 `--add-holiday DATE --reason "근거"`로 원장에 기록한다. 관측 시작일 이후이고
  아직 처리하지 않았으며 유효 데이터가 없는 거래일만 가능하며, 해당 일에 결속된 신호·carry는 다음 거래일로 옮긴다
  (백테스트도 실제 거래일만 본다). 과거 기록은 바뀌지 않는다.
- 등록한 휴장일을 건너뛰는 실행에서 지수 데이터로 검증한다. 그날 거래가 있었으면 등록을
  원장에서 **철회**하고 `BLOCKED_CALENDAR`(그 날부터 다시 처리). 조회 실패·불완전 응답은
  휴장 증거가 아니므로 진행하지 않는다.
- 거래일력 검증 범위는 2026-01-01~2027-12-31이다. 범위 밖은 `BLOCKED_CALENDAR`. 신호 실행일이
  범위 안이어야 하므로 **2027-12-30도 차단**된다. 2028년 일력 추가가 필요하다.

### 거래일력 근거

- 2026: 실전 러너 `_KRX_HOLIDAYS` 사본(테스트로 동일성 확인). 캐시 KOSPI 일자
  2026-01-02~09-28과 정확히 일치.
- 2027: **잠정**. [우주항공청 「2027년 월력요항」](https://www.kasa.go.kr/prog/plcyBrf/brief/kor/sub01_01_04/view.do?plcyBrfNo=431)
  (2026-06-29)의 공휴일·대체공휴일 + KRX 연말 휴장. 평일 휴장 16일. 대체공휴일은 보도자료
  본문에서 확인했으나 첨부 전체 목록과 **KRX 휴장 공지는 확인하지 않았다.** KRX 공지 후 대조 필요.
  거래일력은 코드 fingerprint에 포함되므로 일력 수정은 기존 runtime을 무효화한다.
- 개장 지연일(연초·수능)은 휴장이 아니며, 관측 창 종료(09:00)는 지연 개장에도 보수적이다.

### 리밸런싱 anchor — 미승인

| 기준 | 정의 | 2026년 말 다음 결정일 |
|---|---|---|
| 연구 phase 0 | 백테스트 데이터 시작(2016-01-05)부터 `(i-140)%20==0` | 2026-12-09 (일력 투영) |
| 실전 러너 | **마지막 실행 리밸런싱일**부터 20거래일 이상(경로 의존) | 2026-12-07 (로컬 state의 마지막 2026-11-09 기준) |
| Shadow 기본값 | anchor = 관측 시작일, offset 0 | 관측 시작일 |

세 기준은 서로 다르다. 실전 기준일은 이 저장소의 승인 문서에 없고, 위 값은 로컬 checkout의
state 파일을 읽은 시점의 사실일 뿐이다. `--anchor-date`로 지정할 수 있으며
manifest에는 항상 `UNAPPROVED_TEST_VALUE`로 기록한다. 운영 시작 전 결정 항목이다.

## 5. 데이터 검증

일별(하나라도 실패하면 `BLOCKED_DATA`, 신호·체결 보류):

- 16종 모두의 OHLC·NAV(유한·양수), 거래량·거래대금(유한·0 이상), open/close ∈ [low, high]
- 날짜 중복 금지, 전일 종가 대비 ±35% 초과 차단(분할·오류 의심, 기업행위 ledger 없음)
- KOSPI 종가 유효성, MA120/slope20 계산 가능. 결측을 `risk_on=True`로 대체하지 않는다.

Warm-up(초기화 시 1회, 실패 시 runtime을 만들지 않음):

- KOSPI ≥140행, 관측 시작 **전 거래일**에 종료, 검증 범위 내 날짜는 거래일력과 완전 일치
- 각 ETF는 상장일 이후 KOSPI의 모든 일자를 빠짐없이 보유, 상장일 필수(상장 필터 60거래일 적용)
- bundle과 출처·행수·SHA256을 manifest에 고정. 이후 실행은 이 사본만 읽는다.

Snapshot: 조회 시각·원천·본문 SHA256 기록. 신호·체결·평가·원장 이벤트에 같은 해시를 결속한다.
장애 후 재실행은 **원장에 해시가 기록된 snapshot만** 재사용하고(신규 조회와 혼용 없음), 기록되지
않은 파일은 `*.unrecorded-N`으로 치우고 읽지 않는다. pykrx 가격 캐시는 사용하지 않는다.

P0-A의 규칙(정렬·중복 없음·보간 없음)을 적용하되 조용히 고치지 않고 거부한다. ETF 정규화는
`pykrx_utils`를 재사용한다. 지수 정규화 함수는 `run_etf_backtest`에 있어 재사용하지 않았다.

## 6. 실효 설정 (P1-5)

초기화와 매 실행에서 `etf_shared`/`etf_execution`의 **실제 모듈 값**을 freeze와 비교한다:
유니버스, 그룹 분류, risk override, 과세 종목, 그룹/티커별 괴리율, 유동성·상장·MA·포지션·주기,
모멘텀 가중치, 수수료, 세율, capacity, return basis, 분배금 파일 해시(데이터 행이 있으면 차단).
환경변수 snapshot drift 검사는 추가로 유지한다. `strategy_freeze.json`은 변경하지 않았다.

| 파라미터 | freeze | Shadow 실효값 |
|---|---|---|
| `rebalance_step_days` / `max_asset_pct` / `spread_pct` / `slippage` | 20 / 0.85 / 0.0002 / 0.0005 | 동일 |
| momentum 60/120, capacity, carry | 미기재 | 0.55/0.45, 5%, 1일 (명시) |
| 수수료 / 과세 | 미기재 | 0.015% / 15.4% (과세 8종) |
| `enable_multi_index_risk` / gating mode | false / split | `us_risk_on=True` / split |

## 7. 체결 정책 (백테스트와 동일)

1. 실행일의 carry를 먼저 1회 재시도(BUY는 현금 한도), 해당 종목은 당일 신규 매수 제외
2. carry 반영 후 보유로 gating 재계산. risk_off + split(멀티 인덱스 비활성)이면 후보가 없어도
   보유분을 매도(`allow_empty_target_sell`), 비허용 그룹은 강제청산
3. 모든 주문을 capacity 판정 후 순차 적용. BUY가 현금을 넘으면 `CASH_LIMITED`
4. carry는 `PARTIAL_CARRY`만 생성(거래량 0·capacity 0·현금부족은 carry 없음)
5. 부분 매도로 종목 수·85% 한도를 넘을 수 있다(백테스트 동일). 리포트 `limit_flags`로 표시
6. 모든 주문 시도는 진단 기록(`NO_ORDER`, `ZERO_VOLUME` 등 0주 포함)

## 8. 백테스트 정량 대조

`uv run python -m tests.shadow_compare`. 동일 자본·freeze 파라미터·OHLCV·상장일·지수·capacity.
백테스트는 자체 `load_etf_price` 전처리를 사용한다. 비교: 결정일·랭킹·목표·강제청산, 주문 방향·
요청/체결 수량·판정, 체결가·현금흐름·세금, 일별 현금·평가액·보유, 체결일의 수량·취득원가
(비체결일은 불변 확인), carry, MDD.

| 시나리오 | 거래일 | 결정 | 주문 | 체결(매도) | 최종 평가액(양쪽 동일) | MDD | 불일치 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 합성 normal | 75 | 4 | 13 | 13 (5) | 109,570,739 | -0.74% | 0 |
| 합성 thin rotation | 125 | 7 | 45 | 27 (13) | 78,587,923 | -22.93% | 0 |
| 합성 risk-off | 105 | 6 | 32 | 32 (16) | 90,024,462 | -12.93% | 0 |
| 실데이터 2026, 1억 | 178 | 9 | 15 | 15 (6) | 234,320,423 | -30.71% | 0 |
| 실데이터 2026, 500억 | 178 | 9 | 43 | 43 (21) | 76,520,392,831 | -32.16% | 0 |

부동소수 최대 절대오차는 1e-6원 미만이다. 500억 시나리오는 capacity를 강제하기 위한 값이다.
실데이터 구간은 2026-01-05~09-23(캐시)이며 **성과 근거가 아니라 파이프라인 대조**다.

대조의 한계: 분배금 0건, strict 승인 경로·trailing stop·멀티 인덱스는 freeze에서 비활성이라
대상이 아니다.

## 9. 운영 명령

```bash
# FORWARD 초기화 (관측 시작일의 전 거래일 18:00 KST 이후)
uv run python run_forward_shadow.py --init --runtime-kind FORWARD \
  --runtime-root shadow_runtime --observation-start YYYY-MM-DD \
  --krx-env-file ~/.config/kqe-shadow/krx.env
# 일별 처리 / 누락 복구
uv run python run_forward_shadow.py --runtime-root shadow_runtime --date YYYY-MM-DD \
  --krx-env-file ~/.config/kqe-shadow/krx.env [--recover]
uv run python run_forward_shadow.py --runtime-root shadow_runtime --status
uv run python run_forward_shadow.py --runtime-root shadow_runtime \
  --add-holiday YYYY-MM-DD --reason "KRX 공지 URL"
```

- `--status`는 읽기 전용이다: manifest·실효 설정·원장 replay·state digest·warm-up/snapshot 해시·
  리포트 존재를 검사하고 불일치면 exit 1.
- 리포트는 COMMIT에 포함되며, COMMIT 후 파일 생성 전에 중단되면 다음 실행에서 복구한다.
  발송 대기 항목을 파일보다 먼저 기록하므로 중단 후에도 재발송한다(발송 직후 중단 시 1회
  중복 가능).
- **KRX 자격증명**: `--krx-env-file`은 `KRX_ID`, `KRX_PW`만 읽는다. 조회 전용 별도 파일
  (권한 600)을 권장한다. pykrx는 로그인 ID를 출력하므로 provider가 출력을 차단한다. pykrx는
  중복 로그인 시 기존 세션을 대체하므로 **같은 계정을 쓰는 실전 러너와 동시 실행을 피한다.**
- **Telegram**: 기본 off. `--send-telegram`(또는 `SHADOW_TELEGRAM_SEND=1`)과
  `SHADOW_TELEGRAM_BOT_TOKEN`/`SHADOW_TELEGRAM_CHAT_ID`가 필요하며 실전 채널 변수로 대체하지
  않는다. 실제 발송은 검증하지 않았다(주입 sink·mock transport만 검증).

## 10. 알려진 한계와 미해결 항목

- **진행 불가 상태**: 특정 종목의 거래정지(시가 0), 상장폐지, ±35% 초과 변동처럼 유효 데이터가
  영구히 없으면 트랙은 그 날에서 멈춘다. 처리 정책은 freeze에 없으며 정의하지 않았다(운영자 결정).
- 코드·거래일력 수정은 manifest 무효화로 이어지며 runtime 이관 기능은 없다.
- 원장 hash chain은 우발적 손상·부분 수정 탐지용이다. 파일 전체를 다시 계산해 위조하는 행위자는
  방어 대상이 아니다.
- OHLCV capacity(`floor(volume × 5%)`)는 진단용 근사치이며 일 거래량은 장 마감 후에만 알 수 있다.
- 역사 데이터 한계는 유지된다(docs/p2a1_data_evidence.md): PIT-CLASS-TAX, CA-COVERAGE,
  DISTRIBUTION(가격수익 기준), INDEPENDENT-SOURCE, 46거래일 모니터링은 독립 OOS가 아님.

## 11. Track A와의 관계

Strict Approval blocker 해소는 **Track B 운영 시작의 전제조건이 아니다.** 위 데이터 한계는 리포트와
문서에 계속 명시하며, 이를 해결하기 위해 Track B를 중단하거나 범위를 넓히지 않는다.

## 12. 운영 시작 전 결정 필요 (승인 대상)

1. 초기자본 (현재 테스트값 1억 원)
2. Forward 관측 시작일
3. 리밸런싱 anchor / offset (§4)
4. 관측 창 시각(18:00 / 09:00)
5. KRX 조회 자격증명 준비와 실 API 검증, 2027 휴장일 KRX 공지 대조
6. Telegram 채널·정기 스케줄 활성화 (현재 없음)
7. PR 병합 및 운영 배포
