# Soft Trend-Bonus 검증 결과 (2026-08)

> 상태: **실험 종료 — 채택 안 함 (hard gate 유지)**. 본 문서는 worktree
> `experiment/soft-trend-bonus` 삭제 전 결과 보존용. develop 브랜치에는
> 코드 변경을 merge 하지 않는다.

## 1. 배경 및 가설

현재 전략의 `rank_etfs()`는 `trend_ok`(KOSPI/시장 추세 통과)를 **하드 게이트**
(필수 조건)로 적용한다. bear market 구간에서 대다수 ETF가 `trend_ok=False`가
되어 후보가 0개가 되고, 전략이 100% 현금에 머무는 "bear paralysis" 현상이
발생한다 (in-sample에서 `zero_candidate_events=1` 건 확인).

**가설:** `trend_ok`를 포함 조건에서 제외하고, 모멘텀 스코어에 보너스
항으로 반영하면 bear 구간에서도 후보가 0개가 되지 않아 참여율이 올라가고,
성과가 개선될 수 있다.

```
score = w*zscore(ret_60) + (1-w)*zscore(ret_120) + trend_bonus * trend_ok
```

- `trend_bonus=None` (또는 `ETF_TREND_BONUS` 미설정): 기존 하드 게이트
- `trend_bonus=float`: `ret` 가용성만으로 후보 포함, `trend_ok`는 보너스로 반영

## 2. 구현 개요

`etf_shared.py`의 `rank_etfs()`에 `trend_bonus: float | None = None` 파라미터
추가. 미설정 시 `_parse_trend_bonus_env()`가 환경변수 `ETF_TREND_BONUS`를 읽는다.

- `ETF_TREND_BONUS` 미설정/빈 값 → `None` → 하드 게이트 (기존 동작)
- `ETF_TREND_BONUS=1.0` 등 숫자 → 소프트 보너스 가중치 (float)
- 파싱 실패 → 경고 후 `None`(하드 게이트) 폴백
- `trend_ok` 컬럼 부재 시 소프트 스코어링 불가 → 하드 게이트로 폴백

그리드: `baseline_hard` + `bonus_0.0 / 0.5 / 1.0 / 1.5 / 2.0` (5단계).

## 3. In-sample sweep 결과 (2024-01-01 ~ 2026-08-31)

출처: `outputs_trend_bonus/sweep_summary.csv` (6 labels).

| label | mode | CAGR | MDD | Sharpe | Calmar | zero_cand | avg_cand | trades |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| baseline_hard | hard_gate | **110.5%** | **-23.9%** | **2.155** | **4.627** | 1 | 7.04 | 43 |
| bonus_0.0 | soft=0.0 | 89.8% | -51.8% | 1.557 | 1.735 | 0 | 13.04 | 37 |
| bonus_0.5 | soft=0.5 | 89.8% | -51.8% | 1.557 | 1.735 | 0 | 13.04 | 37 |
| bonus_1.0 | soft=1.0 | 89.8% | -51.8% | 1.557 | 1.735 | 0 | 13.04 | 37 |
| bonus_1.5 | soft=1.5 | 91.6% | -51.6% | 1.582 | 1.775 | 0 | 13.04 | 43 |
| bonus_2.0 | soft=2.0 | 101.1% | -52.1% | 1.657 | 1.941 | 0 | 13.04 | 37 |

**해석:**
- 하드 게이트가 CAGR(+9~+21%p), MDD(-24% vs -52%), Sharpe, Calmar 모든 지표에서
  **압승**. soft bonus는 bear paralysis를 완화(zero_candidate 1→0, avg_cand 7→13)
  했지만, 하락장에서 추세가 꺾인 ETF까지 매수 대상에 포함되어 MDD가 2배 악화됨.
- `bonus_0.0/0.5/1.0`는 수치가 완전히 동일 — `trend_ok`가 이진값이라 보너스가
  랭킹 순서를 바꾸지 못하고 단순히 점수만 평행 이동하기 때문.
- `bonus_2.0`만이 하드 게이트 대비 CAGR 갭을 좁히나(-9.4%p) 여전히 MDD는 2배 열등함.

## 4. Walk-forward OOS 결과

### 4.1 WF 3/1/1 (7 folds)

출처: `outputs_walk_forward_compare_trend_bonus.csv`.

| label | pooled CAGR | pooled MDD | pooled Sharpe |
|---|---:|---:|---:|
| baseline_hard | 27.7% | -47.4% | 1.086 |
| bonus_1.0 | 26.2% | -51.7% | 0.930 |
| bonus_2.0 | **28.4%** | -49.3% | **1.092** |

→ `bonus_2.0`이 CAGR(+0.7%p), Sharpe(+0.006)에서 **근소 우위**.

### 4.2 WF 2/1/1 (8 folds)

출처: `outputs_walk_forward_compare_trend_bonus_2y1y.csv`.

| label | pooled CAGR | pooled MDD | pooled Sharpe |
|---|---:|---:|---:|
| baseline_hard | **26.2%** | -51.3% | **1.020** |
| bonus_1.0 | 21.8% | -49.2% | 0.934 |
| bonus_2.0 | 23.6% | -49.3% | 0.989 |

→ 하드 게이트가 `bonus_2.0` 대비 CAGR **+2.6%p 역전 우위**, Sharpe도 우수.

### 4.3 Robustness 판정

- OOS 우위 방향이 WF 윈도우에 따라 **불안정**: 3/1/1에서는 `bonus_2.0` 근소 우위,
  2/1/1에서는 hard 게이트가 역전 우위.
- 모든 soft 설정에서 MDD는 hard 대비 열등(-47~-52% vs -24~-51%, in-sample은
  -52% vs -24%).
- in-sample에서는 hard가 압승하나 OOS에서는 우위가 윈도우 의존적 → soft bonus는
  **robust한 개선이 아님** (채택 불가).

## 5. 최종 권고

1. **hard gate 유지** — bear paralysis 완화 효과(후보 0개 방지)는 있으나,
   하락장 참여로 인한 MDD 악화가 너무 커 risk-adjusted 성과가 열등.
2. **merge 금지** — `experiment/soft-trend-bonus` 브랜치의 `etf_shared.py`
   `trend_bonus` 파라미터/환경변수는 develop에 반영하지 않음. 실험 코드
   (`scripts/sweep_trend_bonus.py`)도 develop에 포함 안 함.
3. **모니터링 유지** — `zero_candidate_events`는 드물고(1건) 기존 보유 보호
   로직이 정상 방어이므로, bear paralysis 자체는 치명적 리스크로 판정되지 않음.
   기존 `analyze_zero_candidate_impact.py` 모니터링을 계속 사용.

## 6. 재현 방법

worktree 브랜치: `experiment/soft-trend-bonus` (develop 기준).

```bash
# in-sample sweep (baseline_hard + bonus 0.0/0.5/1.0/1.5/2.0)
uv run python scripts/sweep_trend_bonus.py --start 2024-01-01 --end 2026-08-31
# 출력: outputs_trend_bonus/sweep_summary.csv

# WF 3/1/1 (7 folds)
ETF_TREND_BONUS=2.0 uv run python scripts/walk_forward_validation.py \
    --train-years 3 --test-years 1 --output-dir outputs_walk_forward_bonus2.0
# WF 2/1/1 (8 folds)
ETF_TREND_BONUS=2.0 uv run python scripts/walk_forward_validation.py \
    --train-years 2 --test-years 1 --output-dir outputs_walk_forward_2y1y_bonus2.0
```

핵심 환경변수: `ETF_TREND_BONUS` (soft bonus 가중치, 미설정=hard gate).
`rank_etfs(trend_bonus=...)` 파라미터로도 직접 주입 가능.

## 7. 참고: 검증 전 quant 필터 진단 요약

soft bonus 실험 이전에 수행한 필터 파이프라인 진단 결과:

- **liquidity 필터**: 탈락 1건만 발생 — 정상 범위 (유동성 게이트 작동 양호).
- **deviation 필터**: 탈락 0건 — 괴리율 위반 없음 (정상).
- **trend/return 지배**: `trend_ok` + `ret_60/120` 조합이 후보 선정과 수익률을
  지배하는 구조가 정상적으로 작동. trend/return dominance는 **레짐 시그널**로
  해석되며, hard gate는 단순 필터가 아니라 **defensive alpha**(하락장 참여
  차단을 통한 MDD 방어)로 기능.

결론적으로 bear paralysis 완화 시도(soft bonus)는 필터 파이프라인 이상이 아닌,
전략의 의도된 defensive 성격을 훼손하므로 채택하지 않는다.
