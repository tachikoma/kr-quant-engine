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

## 남은 한계

Membership만 point-in-time으로 복원됐습니다. 전략 백테스트에 연결하려면 다음이
추가로 필요합니다.

1. 1,370종목의 일별 OHLCV·NAV를 상장 기간에 맞춰 수집
2. 사라진 227종목의 과거 자산군·복제방법·시장 분류 복원
3. 현재 분류와 과거 분류를 구분한 시점별 적격성 필터
4. 리밸런싱 날짜별 membership를 `rank_etfs()` 입력에 적용하는 백테스트 연결

이들이 완료되기 전에 현재 분류에 매칭되는 생존 종목만으로 백테스트하면
다시 생존 편향이 들어가므로 공식 point-in-time 결과로 취급하지 않습니다.
