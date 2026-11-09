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
| 1 | 정상 실행 | 미수행 | | | | | 전략이 주문을 만들지 않음 (아래) |
| 2 | 컷오프 이후 | O | 0 | 실주문 중단 | 미생성 | O | 10:41:43 `[안전차단] 현재 시각이 실주문 컷오프(09:05) 이후입니다` |
| 3 | `--force-live` | 미수행 | | | | | 주문이 나오는 날에만 의미가 있음 |
| 4 | 동시 실행 | 미수행(KIS로 갈음) | | | | | 락은 브로커와 무관(`run_daily` 진입부). KIS 기록 참조 |
| 5 | 원장 차단 | O | 0 | 시작 직후 실패 | 임시 원장 → `live_order_intents.scenario5_kiwoom_20261109.json.keep` 보관 | O | `Live order journal requires broker reconciliation` |
| 6~7 | `kill -9`, 미체결 취소·재주문 | 미수행 | | | | | 주문이 필요 |
| 8 | DRY_RUN | O | 0 | `[완료] 오늘은 실행할 주문이 없습니다` | 미생성 | O | `실주문 모드: OFF` |
| 9~11 | 부분체결, 재시도 끄기+미체결 매도, 매수 미체결 | 미수행 | | | | | 주문이 필요 |

**미수행 사유:** 11/9 현재 전략은 `risk_on=False`, 목표 종목 0개로 라이브 실행이 주문을 만들지 않는다. 신호나 주문을 강제로 만드는 것은 전략 변경이라 하지 않았다. 컷오프(매도 09:05/매수 09:10)도 지나 있었다. 주문이 나오는 날 장 시작 직후에 수행해야 한다.

### 3-1. 로그 점검
| 확인 | 결과 |
|---|---|
| 7·9에서 `[취소확인]` 등 확인 | 미수행 |
| 정상 체결 흐름의 `stale/contradictory` 등 경고 | 러너 체결 흐름 미수행. 프리플라이트 흐름에서는 해당 경고 없음 |
| 수행한 시나리오 로그에 `[수동대조필요]` | 없음 |

## 4. 요율제한·토큰 만료
| 확인 | 수행 | 로그 접수 건수 | 증권사 내역 건수 | 중복 |
|---|---|---|---|---|
| 요율제한 유도 후 주문 | 미수행 | | | |
| 토큰 무효화 후 주문 | 미수행 | | | |

## 5. 마무리
| 항목 | 확인 |
|---|---|
| 증권사 앱에서 미체결 주문 0건 | 앱 확인 불가. 어댑터 조회로 지정가는 취소 확정(잔량 0), 시장가 두 건은 체결 확정 |
| 예상 밖의 보유 종목 | `verify_flat_position` PASS (holdings unchanged). 앱 대조는 불가 |
| 원장 파일 | 시나리오 5의 임시 원장만 `.keep`으로 보관 |
| 첨부 로그의 계좌번호·토큰·키 마스킹 | 키는 마스킹된 상태로만 기록 |

## 6. 이상 징후
- 토큰 응답에 `expires_in`이 없다(위 1절 참고). 24시간 가정이 실제 만료와 다르면 토큰 만료 시험(4절)에서 드러난다. 미검증.
- 이번에 새로 만든 로컬 보관 파일: `runtime_state/live_order_intents.scenario5_kiwoom_20261109.json.keep`.
- 그 외 특이사항 없음.

## 7. 판정
- 중복 주문 0건: ■ (프리플라이트 주문 건수와 조회 결과 일치, 재주문 없음)
- 원장/상태 파일이 시나리오 기대와 일치: 수행한 3개(2, 5, 8)만 일치
- 2-2 표가 모두 일치: 수행한 항목은 모두 O, 3개 항목 미수행
- 미수행 항목: 러너 시나리오 1, 3, 4(KIS로 갈음), 6, 7, 9, 10, 11, 없는 주문번호 조회(서버), 취소 후 반복 조회, 부분체결 취소, 요율제한, 토큰 만료

판정: **보류(미수행 있음)**
사유: Kiwoom 프리플라이트 주문·취소 왕복은 PASS이고 주문이 필요 없는 러너 시나리오 3개는 통과했다. 주문이 필요한 러너 시나리오와 요율제한·토큰 만료는 남았다. 머지 전 사람의 최종 승인이 필요하다.
