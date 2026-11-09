# 모의 서버 검증 결과 기록 — KIWOOM (2026-11-09)

절차: `docs/mock_trading_verification.md`. 양식: `docs/mock_trading_result_template.md`.
계좌번호·토큰·키는 적지 않았다. 수행하지 않은 항목은 `미수행 + 사유`로 적었다(미수행이 있어 판정은 **보류**).

## 0. 기본 정보
| 항목 | 값 |
|---|---|
| 브로커 | KIWOOM |
| MODE | demo (목적지 가드: `mockapi.kiwoom.com`) |
| 검증 커밋 | `350c854` (코드는 `da0b00d`와 동일, 이후 문서만 추가) |
| 단위 테스트 결과 | 188 / 188 통과 |
| 수행일시 (KST) / 수행자 | 2026-11-09 10:37~10:42 / 개발 VPS의 Claude Code (사용자 요청, 키는 사용자가 `.env`에 입력) |
| `LOG_LEVEL` | INFO |
| 시작 전 `runtime_state/live_order_intents.json` 없음 확인 | ■ |

## 1. 읽기 전용 점검
| 항목 | 결과 | 메모 |
|---|---|---|
| Stage 0 인증 변수 | PASS | `KIWOOM_APPKEY/SECRETKEY`. `KIWOOM_ACCOUNT_NO`는 선택 항목 SKIP |
| Stage 1 잔고/보유/시세/호가 | PASS | `adapter_init`, `get_cash`, `get_available_cash`, `get_holdings`, `get_prices`, `get_bid_ask_prices` PASS. `get_buyable_info`, `get_ticker_name`은 어댑터 미구현(선택)이라 SKIP |
| 리포트 파일 경로 | | `runtime_state/preflight_kiwoom_20261109_103727.json` (gitignored, PR에 미포함) |

참고: 토큰 응답에 `expires_in`이 없어 `[WARN] expires_in not found in token response, defaulting to 86400` 경고가 나온다(어댑터가 86400초로 간주). 11/6까지는 `.env`에 자리표시자 키가 들어 있어 인증 실패였고, 이번에 실제 모의 키로 처음 인증됐다.

## 2. 주문·취소 왕복
명령: `LIVE_ORDER_ENABLED=1 KIWOOM_LIVE_ORDER_ENABLED=1 ... broker_preflight --broker KIWOOM --mode demo --with-order` (INFO 로그 래퍼 사용, 10:37)
| 항목 | 결과 | 메모 |
|---|---|---|
| stage2-authorization (목적지 가드) | PASS | host=mockapi.kiwoom.com |
| 시장가 매수 → 매도 왕복 `roundtrip` | PASS | 둘 다 1/1 체결 확정 |
| 지정가(-25%) 매수 → 취소 | PASS | 취소 접수 `requested` |
| `cancel_semantics` | PASS | |
| `stage2_unresolved` 발생 여부 | 없음 | |
| `verify_flat_position` | PASS | holdings unchanged |

### 2-1. 응답 값 기록
| 시점 | is_found | order_qty | filled_qty | remaining_qty | is_filled |
|---|---|---|---|---|---|
| 시장가 매수 전량 체결 직후 | True | 1 | 1 | 0 | True |
| 시장가 매도 전량 체결 직후 | True | 1 | 1 | 0 | True |
| 지정가 미체결 직후 | True | 1 | 0 | 1 | False |
| 취소 직후 | True | 1 | 0 | 0 | False |
| 취소 후 반복 조회 (몇 초 뒤) | 미수행 | | | | 첫 재조회에서 확정되어 반복하지 않음 |
| 없는 주문번호 조회 | 미수행 | | | | 서버 대상 미수행. 단위 테스트에서만 확인 |

### 2-2. 코드 가정과의 일치 여부
| 확인 | 기대 | 실제 | 일치 (O/X) |
|---|---|---|---|
| 취소 직후 `remaining_qty` | 0 | 0 | O |
| 취소 후 `filled_qty < order_qty` | True | 0 < 1 | O |
| 취소 후 `is_filled` | False | False | O |
| 없는 주문번호 | `is_found=False` | 미수행 | 미수행 |
| 취소 반영 지연 | 5초 이내 | 첫 재조회에서 확정 | O |
| 전량 체결 직후 | `is_filled=True`, `filled==order>0`, `remaining==0` | True, 1==1, 0 | O |
| 취소 후 `order_qty` | 제출 수량 그대로 | 1 | O |
| 부분체결 취소의 `filled_qty` | 감소 없음 | 미수행(부분체결 재현 못함) | 미수행 |
| 반복 조회 | 모순 값 없음 | 미수행 | 미수행 |

## 3. 러너 시나리오 (2026-11-09, `BROKER_TYPE=KIWOOM MODE=demo`)
| # | 시나리오 | 수행 | 주문 건수 | 최종 상태 | 원장 파일 | 기대와 일치 | 메모 |
|---|---|---|---|---|---|---|---|
| 1 | 정상 실행 | 변형 수행 | 1 / 1 | `DONE` | 삭제됨 | O | 컷오프 이후라 `--force-live`로 실행(3과 겸함). 069500 1주 모의 매수 후 `FORCE_REBALANCE=1`로 매도, 체결 1/1(번호 0112412). 장 시작 직후의 정상 실행은 아님 |
| 2 | 컷오프 이후 | O | 0 | 실주문 중단 | 미생성 | O | 10:41:43 `[안전차단] 현재 시각이 실주문 컷오프(09:05) 이후입니다` |
| 3 | `--force-live` | O | 1 / 1 | `DONE` | 삭제됨 | O | `[보호] 전략 유니버스 외 보유종목 1개는 매도 대상에서 제외` — A0101N0(RISE AI전력인프라 ETF) 1,628주 그대로. 증권사 내역: 매수 0111266, 매도 0112412 |
| 4 | 동시 실행 | 미수행(KIS로 갈음) | | | | | 락은 브로커와 무관(`run_daily` 진입부). KIS 기록 참조 |
| 5 | 원장 차단 | O | 0 | 시작 직후 실패 | 임시 원장 → `live_order_intents.scenario5_kiwoom_20261109.json.keep` 보관 | O | `Live order journal requires broker reconciliation` |
| 6 | `kill -9` | O | 1 / 1 (재실행 0) | 접수 직후 프로세스 사망 | 남음 (`SELL 069500 1주 ACKED order_id=0113514`) → `live_order_intents.scenario6_kiwoom_20261109.json.keep` 보관 | O | 원장에 ACKED가 찍히는 순간 `kill -9`. 재실행은 `Live order journal requires broker reconciliation`으로 주문 전 실패. 증권사 내역에 매도 0113514 한 건 |
| 7 | 미체결 취소·재주문 | 미수행 | | | | | 러너의 최초 주문이 시장가라 즉시 체결됨. 모의 서버에서 미체결을 만들 수단 없음 |
| 8 | DRY_RUN | O | 0 | `[완료] 오늘은 실행할 주문이 없습니다` | 미생성 | O | `실주문 모드: OFF` |
| 9~11 | 부분체결, 재시도 끄기+미체결 매도, 매수 미체결 | 미수행 | | | | | 위와 같음 (가짜 어댑터 러너 테스트로만 검증) |

주문을 만든 방법: 전략 신호는 그대로 두고(`risk_on=False`), 모의 계좌에 069500 1주를 모의 매수해 `FORCE_REBALANCE=1`로 매도 대상으로 만들었다. 기존 보유 A0101N0(RISE AI전력인프라 ETF, 사용자 확인) 1,628주는 전략 유니버스(ETF_LIST) 밖이라 `[보호]` 규칙으로 제외되어 그대로다. Telegram 변수는 비워 외부 알림을 막았다. A0101N0는 `A`+숫자 6자리가 아닌 신형 코드라 어댑터의 티커 정규화에 걸리지 않으며, 유니버스에 신형 코드 ETF가 추가되면 별도 점검이 필요하다.

**미수행 사유(7, 9, 10, 11):** 러너의 최초 주문이 시장가라 즉시 체결된다. 모의 서버에서 미체결·부분체결을 만들 수단이 없다.

### 3-1. 로그 점검
| 확인 | 결과 |
|---|---|
| 7·9에서 `[취소확인]` 등 확인 | 미수행 |
| 정상 체결 흐름의 `stale/contradictory` 등 경고 | 러너 체결 흐름 미수행. 프리플라이트 흐름에서는 해당 경고 없음 |
| 수행한 시나리오 로그에 `[수동대조필요]` | 없음 |

## 4. 요율제한·토큰 만료
| 확인 | 수행 | 로그 접수 건수 | 증권사 내역 건수 | 중복 |
|---|---|---|---|---|
| 요율제한 유도(12개 스레드 병렬 호출) 중 주문 | O (부분) | 주문 요청 1회 | 매수 1 / 매도 1 | 없음 |
| 토큰 무효화(로컬) 후 재발급하여 주문 | O | 주문 요청 1회 | 매수 1 / 매도 1 | 없음 |
| 서버가 거부하는 가짜 토큰으로 주문 | O | 주문 요청 1회(거부) | 매수 0 / 매도 0 | 없음 (주문 미접수) |

## 5. 마무리
| 항목 | 확인 |
|---|---|
| 증권사 앱에서 미체결 주문 0건 | 앱 확인 불가. 어댑터 조회로 지정가는 취소 확정(잔량 0), 시장가 두 건은 체결 확정 |
| 예상 밖의 보유 종목 | `verify_flat_position` PASS (holdings unchanged). 앱 대조는 불가 |
| 원장 파일 | 시나리오 5의 임시 원장만 `.keep`으로 보관 |
| 첨부 로그의 계좌번호·토큰·키 마스킹 | 키는 마스킹된 상태로만 기록 |

## 6. 이상 징후
- 토큰 응답에 `expires_in`이 없다(위 1절 참고). 24시간 가정이 실제 만료와 맞는지는 시간이 지나야 알 수 있어 미검증. 서버가 거부하는 토큰 시험 결과는 4절 참고.
- 이번에 새로 만든 로컬 보관 파일: `runtime_state/live_order_intents.scenario5_kiwoom_20261109.json.keep`.
- 그 외 특이사항 없음.

## 7. 판정
- 중복 주문 0건: ■ (프리플라이트, 러너 시나리오 1/3/6, 토큰·요율제한 시험 모두 증권사 내역과 일치, 재주문 없음)
- 원장/상태 파일이 시나리오 기대와 일치: 수행한 시나리오(1, 2, 3, 5, 6, 8)는 일치
- 2-2 표가 모두 일치: 수행한 항목은 모두 O, 3개 항목 미수행
- 미수행 항목: 러너 시나리오 7, 9, 10, 11(모의 서버에서 미체결·부분체결을 만들 수단 없음), 장 시작 직후의 정상 실행(1), 동시 실행(4, KIS로 갈음), 없는 주문번호 조회(서버), 취소 후 반복 조회, 부분체결 취소, 제한 응답을 받은 주문의 재시도 검증
- 미해결 발견: Kiwoom 실행 중 토큰 만료(8005, HTTP 200) 자동 복구 없음 → 해당 주문 유실(중복은 아님)

판정: **보류(미수행 있음)**
사유: Kiwoom 프리플라이트 왕복, 러너 시나리오 1/2/3/5/6/8, 토큰·요율제한 시험에서 중복 주문은 없었다. 토큰 만료 시 주문이 복구되지 않는 점은 개선 여부를 정해야 한다. 미체결/부분체결 시나리오는 서버에서 재현할 수 없어 라이브 첫날 위험으로 남는다. 머지 전 사람의 최종 승인이 필요하다.
