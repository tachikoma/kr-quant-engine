# Point-in-time ETF 유니버스 데이터

구축일: 2026-07-21

## 목적

현재 KRX ETF 기본정보만 사용하면 과거에 거래됐다가 사라진 종목이 누락됩니다.
이 데이터는 KRX의 지정 거래일별 ETF 전종목 시세를 저장해 백테스트 시점에
실제로 존재했던 ETF membership를 복원합니다.

## 원천과 스케줄

- 원천: KRX `MDCSTAT04301` ETF 전종목 시세
- 기간: 2016-01-06~2026-07-21
- 스냅샷: 140거래일 워밍업 후 20거래일 리밸런싱 시점 + 최종일
- 스냅샷 수: 124개
- 캐시: `data_cache/pit_universe/` (gitignored)
- 구축: `uv run scripts/build_point_in_time_universe.py`
- 오프라인 재검증: `uv run scripts/build_point_in_time_universe.py --offline`

스냅샷 날짜는 현재 전략의 리밸런싱 판단 스케줄과 맞춘 것입니다. 인접한
스냅샷 사이에 상장되거나 사라진 정확한 날짜는 알 수 없지만, 각 리밸런싱
시점의 선택 가능 membership는 정확히 반영합니다.

## 커버리지

| 지표 | 값 |
|---|---:|
| 스냅샷 | 124 |
| 전체 행 | 74,129 |
| 고유 티커 | 1,370 |
| 첫 스냅샷(2016-08-01) | 220 |
| 마지막 스냅샷(2026-07-21) | 1,150 |
| 전체 기간에 관찰됐지만 마지막에 없는 티커 | 220 |
| 첫 스냅샷 이후 새로 관찰된 티커 | 1,150 |
| 현재 분류 캐시에 없는 역사적 티커 | 227 |
| membership SHA-256 | `7b515e9e5abb8113cdb05bb205befc0ec64c643df006a169d2327a55d465ba86` |

2016-08-01에 관찰된 220종목 중 45종목은 2026-07-21 스냅샷에 없었습니다.
전체 기간에서는 220종목이 과거에 관찰됐지만 마지막 스냅샷에는 없었습니다.
이들을 백테스트 후보군에서 자동으로 제외하던 생존 편향을 membership 단계에서
제거했습니다.

## 산출물

| 파일 | 용도 |
|---|---|
| `pit_universe_snapshots.parquet` | 스냅샷별 티커·종목명·기초지수명·거래 정보 |
| `pit_universe_membership_events.parquet` | 인접 스냅샷 사이 ENTER/EXIT |
| `pit_universe_manifest.json` | 스케줄·커버리지·해시·한계 |
| `outputs_universe_bias/pit_universe_snapshot_counts.csv` | 날짜별 종목 수와 분류 누락 수 |
| `outputs_universe_bias/pit_universe_membership_events.csv` | 이벤트 검토용 CSV |

`current_*` 컬럼은 현재 KRX 분류 캐시와의 매칭 결과입니다. 과거 시점의 분류로
사용하면 안 됩니다.

## 현재 검증된 범위

- 124개 예상 스냅샷 누락 0
- 날짜·티커 중복 0
- 스냅샷별 종목 수 220~1,150
- ENTER 1,150건(최초 스냅샷 제외), EXIT 220건
- 캐시 오프라인 재구축 결과와 membership hash 일치

## PIT 백테스트 연결 (완료 2026-08-05)

전체 PIT 파이프라인이 연결됐습니다.

1. **전체 1,370종목 일별 OHLCV·NAV 수집 완료 (2026-08-05):**
   `scripts/prefetch_pit_prices.py --scope all`로 1,370종목 모두 수집, 오류·빈파일 0건.
   `data_cache/pit_prices/<ticker>.parquet`에 종목별 저장.
2. **사라진 227종목의 과거 자산군·복제방법·시장 분류 복원 완료 (2026-08-05):**
   `scripts/restore_pit_classification.py` (자세한 내용은 아래).
3. **시점별 적격성 필터 완료:** `pit_universe.add_pit_membership_flag()`가
   `pit_membership_ok` 컬럼을 추가하고, `rank_etfs()`가 이를 첫 필터로 적용.
   `build_pit_ticker_groups()`가 현재 KRX 분류(1,143종목) + 복원 분류(227종목)를
   결합해 1,370종목의 티커→그룹 매핑을 구축하고, `run_etf_strategy(ticker_groups=...)`
   로 전달해 risk-off 그룹 게이팅에 반영.
4. **백테스트 연결 완료:** `scripts/pit_backtest.py`가 전체 유니버스로 PIT 백테스트를
   실행하고 static 유니버스와 비교합니다. 출력: `outputs_pit/`.

### PIT 백테스트 결과 (2016-08-01~2026-07-21)

| 지표 | PIT (1,370종목) | static (16종목) |
|---|---:|---:|
| CAGR | 31.77% | 24.66% |
| MDD | -59.14% | -36.65% |
| Sharpe | 0.83 | 1.08 |
| 최종자산 | 13,175,955 | 7,882,627 |

PIT 유니버스가 CAGR은 높지만(+7.1%p) MDD는 크게 깊어지고(-22.5%p) Sharpe가
낮아집니다. 이는 과거 존재했던 고모멘텀/고변동 테마 ETF가 후보에 포함되면서
상승은 키우지만 하락 방어는 약해지는 특성 때문입니다. static 유니버스의
선택 편향(생존 편향)이 부분적으로는 '성과 하향 보수'로 작용했음을 시사합니다.
거래 내역 검증 결과 모든 거래가 해당 티커의 PIT membership 구간 내에서 발생해
look-ahead 바이어스가 없음을 확인했습니다.

### 과거 분류 복원 상세

`scripts/restore_pit_classification.py`가 PIT 스냅샷의 `index_name`(기초지수명)과
`name`(종목명) 키워드 기반 규칙으로 자산군(주식/채권/원자재/부동산/통화/기타),
시장(국내/해외), 레버리지, 복제방법을 추정합니다. 현재 KRX 분류(1,143종목)로
검증 시 자산군 96.1%, 시장 91.6% 정확도를 보였습니다. 결과는
`data_cache/pit_universe/pit_classification_restored.parquet`에 저장되고,
복제방법 신뢰도가 낮은 61종목은 `outputs_universe_bias/pit_classification_review.csv`
로 분리되어 수동 검토를 기다립니다. 자동 분류는 복제방법(실물/합성)을 지수명만으로
단정할 수 없으므로, 공식 채택 전에 검토 CSV를 확정하는 것을 권장합니다.
