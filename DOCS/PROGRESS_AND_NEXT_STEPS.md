# 진행상황 및 향후 작업

최종 갱신: 2026-07-21

## 현재 상태

ETF 로테이션 전략 백테스트 및 실전 러너가 안정화 단계에 있습니다.

**핵심 결과 (기본 설정, 2016-01~2026-07):**

| 지표 | 값 |
|---|---|
| CAGR | 24.27% |
| MDD | -35.79% (2026-06-22~07-21, 미회복) |
| Sharpe | 1.089 |
| Calmar | 0.678 |
| 최종자산 | 9,875,979원 (기본 1,000,000원 기준) |

위 수치는 `outputs_etf_only/performance.json`의 2026-07-21 종가 기준입니다. 현재 static
유니버스는 오늘의 정보로 확정한 후보군이므로, 이 과거 성과는 유니버스 선택 편향을
포함할 수 있는 in-sample 상한으로 해석합니다.

주의: 이 문서는 2026-07-21 기준 스냅샷입니다. `outputs_etf_only/` 아래의 산출물은
이후 백테스트 재실행으로 갱신될 수 있는 가변 결과이므로, 공식 기준은
`strategy_freeze.json`과 본 문서의 날짜 표기를 함께 보아야 합니다.

**운영 체계:**
- 백테스트: `run_etf_backtest.py` (single/experiment 모드)
- 데일리 러너: `live_trading/etf_daily_runner.py` (기본 안전모드)
- 증권사: 키움/한국투자증권 선택 가능 (`BROKER_TYPE`)
- 텔레그램 알림 연동
- 매매차익 과세 8개 ETF 자동 반영 (15.4%)
- 분배금 포함 수익률 (`total_return` 모드, CSV 기반)

**분석 스크립트** — 그리드 백테스트, walk-forward 검증, 파라미터 안정성, 거래별 성과 분해, MDD 기여도 분석, 포트폴리오 stop 민감도 등.

## 완료된 작업 요약

### Phase 2 — 유니버스 자동 구축 (2026-07-14, 검증 완료 2026-07-17)
16개 고정 `ETF_LIST`를 KRX 분류 기반 자동 구축으로 확대:

- `etf_universe.py`: `build_universe()` 순수 함수 + `config_from_env()`
- 필터: `ETF_REPLICA_METHD_TP_CD == "실물(패시브)"` + `IDX_ASST_CLSS_NM == "주식"` + `IDX_CALC_INST_NM2 == "일반"`
- 제외: 커버드콜(키워드), 원자재(기본), 레버리지/인버스(`"일반"` 필터로 자동 제외)
- 그룹 자동 매핑: `국내`→`domestic_equity`, `해외`/`국내&해외`→`foreign_investment`
- 결과: 573종목 (domestic_equity 318, foreign_investment 240, commodity 15)
- `ETF_LIST` env var가 auto보다 우선 (static 오버라이드)
- `TAXABLE_ETF_TICKERS`도 KRX 분류에서 자동 산출 (289종)
- 검증: 하이브리드 방식 (단위 불변식 12항목 + 캐시 기반 통합 백테스트 + 데이터 병합 드라이런) 모두 통과

**검증 결과 (2026-07-17):** Auto 유니버스(573종목)가 Static(16종목)보다 열등하여 static 유지 확정:
- Static: CAGR 25.7%, MDD -32.7%, Sharpe 1.22, 알파 +7.9%
- Auto: CAGR 12.2%, MDD -42.8%, Sharpe 0.56, 알파 -5.6% (벤치마크 미달)
- 원인: 모멘텀 잡음(573종목 중 소형/테마 ETF 빈번 교체), 거래 비용 증가, 포지션 품질 희석
- 상세 분석: `DOCS/UNIVERSE_AUTO_ANALYSIS.md`
- 조치: `.env`에서 `ETF_UNIVERSE_MODE=auto` 주석 처리, static 유지

### 멀티 인덱스 리스크 시그널 — 구현 교정 후 미채택 (2026-07-21)
- 기존 `split` 구현이 KOSPI risk-on일 때 US risk-off를 적용하지 않는 불일치를 수정
- 백테스트와 실전 러너가 공유 `GatingDecision`을 사용하고, 비허용 그룹만 선택적으로 청산
- 정확한 `split` 재검증 결과 전체 기간 CAGR 25.05%→24.02%, Sharpe 1.137→1.093으로 악화
- **워크포워드 OOS: CAGR 17.36%→15.94%, MDD -15.34%→-22.05%, Sharpe 1.089→0.985로 악화**
- 20거래일 블록 부트스트랩에서 split 우월 확률은 연평균수익/Sharpe/Sortino 기준 약 30~34%
- 결론: `ENABLE_MULTI_INDEX_RISK=0`으로 KOSPI 단일 시그널 유지, split은 실험 옵션으로 보존

### Static 유니버스 선택 편향 민감도 (2026-07-21)

- 현재 static baseline: CAGR 24.27%, MDD -35.79%, Sharpe 1.089
- 2023년 이전 상장 11종목만 유지: CAGR 24.07%(-0.21%p)
- 최근 테마 3종목 제외: CAGR 22.34%(-1.94%p), MDD -33.73%(+2.06%p)
- 최근 테마 기여의 대부분은 네트워크인프라 1종목에서 발생
- 커버드콜 3종목과 AI전력인프라는 거래가 없어 과거 성과 기여 0
- 상장폐지를 포함한 point-in-time 유니버스는 아니므로 전부 in-sample 진단으로 취급
- 상세: `DOCS/UNIVERSE_SELECTION_BIAS.md`

### Point-in-time ETF membership 구축 (2026-07-21)

- KRX 리밸런싱 시점별 ETF 전종목 스냅샷 124개 수집
- 74,129행, 고유 티커 1,370종목, 누락 스냅샷 0
- 첫/마지막 스냅샷 220/1,150종목
- 과거에 관찰됐지만 마지막 스냅샷에 없는 티커 220종목
- 현재 분류 캐시에 없는 역사적 티커 227종목
- 역사적 티커 227종목 OHLCV·NAV 수집 완료: 227개 파일, 184,985행, 빈 파일·중복 0
- membership와 역사적 티커 가격은 복원됐으나 과거 분류·나머지 1,143종목 가격 보강 전에는
  공식 백테스트에 미연결
- 상세: `DOCS/POINT_IN_TIME_UNIVERSE.md`

위 작업 흐름은 다음 작업 1번의 선행 조건입니다. split 게이팅을 먼저 바로잡아
기준 전략을 재동결했고, static 유니버스의 사후 선택 편향을 정리했으며,
membership와 과거 가격 수집 경로를 마련해야 시점별 유니버스와 가격을 결합한
PIT 백테스트를 안정적으로 시작할 수 있습니다.

### 2026-07 작업 흐름 요약

- `1f5dd19a`: `build_gating_decision()`으로 백테스트/실전 게이팅을 통합하고,
   비허용 보유분은 `forced_exit_tickers`로 분리해 `ETF_RISK_GATE_EXIT`로 청산
- `38a0db3`: `check_strategy_freeze.py`가 동결 설정과 현재 설정을 비교해
   `universe_mode`, 멀티 인덱스 파라미터, 분배금/슬리피지 차이를 검증
- `af194e11`: 선택 편향 분석을 전역 `ETF_LIST` 변조 없이 실행하도록 바꾸고,
   시나리오별로 `run_etf_strategy(..., universe_tickers=...)`를 직접 호출
- `7f4324bc`: `build_snapshot_dates()`와 `normalize_krx_etf_snapshot()`로 날짜별
   membership를 복원하고, 스냅샷 패널을 검증해 `pit_universe`를 구축
- `103e29c8`: `normalize_krx_etf_history()`와 `prefetch_pit_prices.py`로 역사적
   티커의 상장 기간 OHLCV·NAV를 티커별로 누적 저장
- `0f89961e`: membership와 가격 수집 완료 상태를 문서에 고정하고, 아직 남은
   과제(과거 분류 복원, 나머지 가격 보강)를 명시

### 성과 리포트 교정 (2026-07)
- MDD 고점/저점/회복일/진행 여부 및 현재 낙폭 추가
- Rolling CAGR/MDD/Sharpe/Sortino 누락 버그 수정
- 회전율 gross(500%)/one-way(248%) 분리
- 리밸런싱 판단/거래/무거래(122/64/58) 분리
- 청산(71.7일)/미청산(311.0일) lot 보유기간 분리
- 에쿼티 곡선에 리밸런싱 판단·주문 수 기록

### 포트폴리오 리스크 관리 실험 (2026-07)
4개 리스크 관리 규칙을 구현하고 OOS로 검증했으나 모두 채택하지 않았습니다:

| 실험 | OOS 결과 | 채택 여부 |
|---|---|---|
| 목표비중 리밸런싱 | CAGR 17→15%, MDD -15→-20% | 미채택 |
| 비대칭 하드캡 (70%/85%) | cap 85% 미작동, 70% CAGR만 하락 | 미채택 |
| 종목별 trailing stop | 마지막 fold에만 미미한 개선 | 미채택 |
| 포트폴리오 trailing stop | 엔드포인트 과적합 의심 | 미채택 |

**핵심 교훈:** 승자 비중을 줄이거나 조기에 청산하는 규칙은 모멘텀 전략의 상승 참여율을 떨어뜨리지만, 하락 방어 효과는 충분하지 않았다. OOS 마지막 fold에만 집중된 개선은 일반화되기 어렵다.

### 현재 MDD 분석 (2026-07)
- 원인: 필터 오류가 아닌 구조적 집중 위험 (반도체 69.8% + 네트워크인프라 30.1%)
- 리밸런싱 판단은 정상 작동, 랭킹 1·2위 유지로 무문제 보유 반복
- `analyze_current_drawdown.py`로 포지션별 손실 기여도 추적 가능

### 기타 완료 작업
- 후보 0개 진단 프레임워크 구축 및 정상 방어 판정
- NAV 기반 랭킹, 상장일/괴리율 필터 적용
- ETF 균등분배 리밸런싱 수정
- KIS/키움 어댑터 안정화 (토큰 발급/retry/throttle)
- 전략 동결 (`strategy_freeze.json`) 시스템 구축

## 실험 옵션 보존 현황

기본값 `0`(비활성)으로 보존 중이며, `.env` 미변경으로 기존 전략 유지:

| 옵션 | 용도 |
|---|---|
| `TARGET_WEIGHT_REBALANCE` / `REBALANCE_BAND_PCT` | 목표비중 리밸런싱 |
| `TRIM_OVERWEIGHT_POSITIONS` | 비대칭 하드캡 |
| `ETF_EXIT_CHECK_DAYS` / `ETF_TRAILING_STOP_PCT` | 종목별 trailing stop |
| `ETF_PORTFOLIO_TRAILING_STOP_PCT` | 포트폴리오 trailing stop |

## 권장되는 다음 작업

0. **v2 OOS 추적 (구현 완료 2026-08-05):** `live_trading/etf_daily_runner.py`의
   `_record_oos_equity_history()`가 매 실행마다 일별 평가액을
   `runtime_state/oos_equity_history.json`에 기록하고, `scripts/track_oos_performance.py`가
   v2 OOS 시작일(2026-07-22) 이후 실전 성과를 계산합니다. 기본은 broker 평가액만
   사용하며 `--include-mock`으로 드라이런 평가액 포함이 가능합니다. v1 트랙과는 섞지 않습니다.

0.5. **상태 기반 walk-forward 교정 (구현 완료 2026-08-05):** `run_etf_strategy()`에
   `initial_state`(보유/현금/원가/peak)와 `return_final_state` 파라미터를 추가하고,
   `walk_forward_validation.py`가 폴드 경계에서 이전 폴드의 실제 종료 상태를 다음
   폴드 테스트 구간에 전달합니다(`WF_STATE_BASED=1` 기본). 인공 `boundary_cost_pct`
   대신 실제 보유 종목/현금/세금 원가가 이월되어 경계 전환 비용이 자연 반영됩니다.
   폴드 1은 기간 시작부터 fold test_end까지 연속 실행하며, 재조정 위상은
   `rebalance_phase_offset`으로 전체 기간 스케줄과 정렬됩니다.

0.75. **factorial ablation (구현 완료 2026-08-05):** `scripts/factorial_ablation.py`가
   KOSPI 필터, 그룹 override, 멀티 인덱스, MA/SLOPE 기간, 모멘텀 가중치의 단독 효과와
   상호작용을 분리합니다. `ETF_MOMENTUM_WEIGHT_60` env로 모멘텀 스코어 가중치를
   파라미터화했습니다. 기준(CAGR 25.5%, MDD -51.2%, Sharpe 1.02) 대비:
   - no_kospi_filter: CAGR 24.2%(−1.3%p) → KOSPI 필터 기여 확인
   - no_group_override: CAGR 24.0%(−1.5%p) → 그룹 override 기여 확인
   - enable_multi_index: CAGR 27.6%(+2.2%p)
   - **ma_90: CAGR 26.2%(+0.7%p), MDD -31.8%(개선 +19.4%p) — 짧은 MA가 낙폭을 크게 줄임**
   - ma_180: CAGR 19.5%(−5.9%p)
   - momentum_60_30: CAGR 28.3%(+2.8%p) but MDD -52.9%(악화)
   - momentum_60_100: CAGR 21.1%(−4.4%p)

0.8. **복수 벤치마크·비용 비교 (구현 완료 2026-08-05):** `scripts/benchmark_comparison.py`가
   전략 커브를 KR(KODEX200), US(TIGER 미국S&P500선물(H)), Gold(ACE KRX금현물),
   현금, 그리고 KR/US/Gold 정책 포트폴리오(33/33/33, 50/30/20, 60/40)와 비교합니다.
   주문 크기 구간별 분포를 `outputs_benchmark/market_impact.csv`로 산출합니다.
   결과(전략: CAGR 24.0%, MDD -51.2%, Sharpe 0.99):
   - KR: CAGR 14.9%, MDD -40.8% | US: CAGR 12.2%, MDD -35.0% | Gold: CAGR 23.6%, MDD -32.1%, Sharpe 1.13
   - **policy 33/33/33: MDD -23.2%(최소), Sharpe 1.09, Calmar 0.83(최대) — 분산 효과 최대**
    - policy 60/40: CAGR 13.9%, MDD -33.7%

0.9. **PIT 일별 가격·과거 분류 확장 + 백테스트 연결 (구현 완료 2026-08-05):**
   `scripts/prefetch_pit_prices.py --scope all`로 전체 1,370종목 OHLCV·NAV 수집 완료.
   `pit_universe.add_pit_membership_flag()`(시점별 적격성 필터)와
   `build_pit_ticker_groups()`(현재+복원 분류 결합, 1,370종목 그룹 매핑)를 추가하고,
   `rank_etfs()`가 `pit_membership_ok`를 첫 필터로 적용. `scripts/pit_backtest.py`가
   PIT 백테스트를 실행·검증. 결과(2016-08-01~2026-07-21):
   - **PIT: CAGR 31.77%, MDD -59.14%, Sharpe 0.83** vs **static: CAGR 24.66%, MDD -36.65%, Sharpe 1.08**
   - PIT 유니버스가 CAGR +7.1%p이지만 MDD -22.5%p 악화, Sharpe 하락
   - 거래 내역 검증: membership 위반 0건 (look-ahead 바이어스 없음)

1. **~~PIT 일별 가격·과거 분류 확장~~ (완료 — 위 0.9 참조):** 남은 후보는
   복제방법(실물/합성) 저신뢰 61종목의 수동 검토 CSV 확정.
2. **상태 기반 walk-forward 교정:** fold 경계에서 선택된 파라미터의 실제 보유 종목·현금·세금
   원가를 전환하여 다음 테스트 구간을 실행. 이후 fold 확대와 embargo를 검토.
3. **교정된 기반의 factorial ablation:** 유니버스, KOSPI 필터, 그룹 override, 멀티 인덱스,
   MA/slope, 모멘텀 가중치의 단독 효과와 상호작용을 분리.
4. **복수 벤치마크·비용 비교:** KODEX200, 사전 정의 KR/US/Gold 정책 포트폴리오,
   현금·위험조정 벤치마크와 비교하고 주문 크기별 시장 충격을 추가.
5. **v2 OOS 추적:** 2026-07-21 재동결과 2026-07-22 OOS 시작을 기준으로 실전 성과를
   누적하며, v1 역사적 트랙과 섞지 않음.

## 참고

- `.env`는 반드시 `pykrx` import 이전에 로드되어야 합니다 (로그인 토큰 만료 이슈 방지).
- v1은 2026-07-13 동결·2026-07-14 OOS 시작의 역사적 트랙입니다. 현재 공식 트랙은
  `strategy_freeze.json`의 v2(2026-07-21 동결·2026-07-22 OOS 시작)이며 두 결과를 섞지 않습니다.
- KRX 단축코드는 6자리이며 숫자와 문자 혼용 가능 (예: `0101N0`, `0000H0`).
