# 진행상황 및 향후 작업

작성일: 2026-05-11

요약
- 현재까지 실행 및 분석 작업을 정리하고, 관련 스크립트와 결과 일부를 저장했습니다.

이번 커밋에서 변경된 사항
- 추가된 스크립트
  - `scripts/correlation_analysis.py`: 드로우다운 윈도우 기간의 ETF 수익률 상관행렬과 히트맵을 생성합니다.
  - `scripts/apply_cap_and_retest.py`: 환경변수 `MAX_ASSET_PCT`을 적용한 재백테스트를 실행하는 헬퍼 스크립트입니다.
- 생성된 출력(예시)
  - `outputs_grid/corr_drawdown_window.csv`
  - `outputs_grid/corr_drawdown_heatmap.png`
  - `outputs_grid/cap_filtered_n12_cap0.20_pos2.csv`
  - `outputs_grid/cap_filtered_trades_n12_cap0.20_pos2.csv`

현재 상태(핵심 결과)
- 상관관계 분석: 드로우다운 윈도우(2018-01-29 → 2020-03-19)를 기준으로 상관행렬 생성(결과 파일 위 참조).
- per-asset cap 실험(기본값 `MAX_ASSET_PCT=0.20`)을 단일 실행으로 수행하여 결과 파일 생성 및 기초 통계 확인함.

권장되는 다음 작업(우선순위)
1. cap 그리드 실험: `MAX_ASSET_PCT` 값을 여러 값(예: 0.10, 0.15, 0.20)으로 돌려 성능·MDD 변화를 비교합니다.
2. 리밸런스/포지션 민감도 테스트: 리밸런스 주기(`REBALANCE_STEP_DAYS`)와 `MAX_POSITIONS` 민감도 실험.
3. `scripts/filter_and_retest_by_risk.py`를 이용한 리스크 기반 후보군 필터링 그리드 실행.
4. 결과 비교 보고서 및 최종 추천 필터/정책 문서화(시각화 포함).

실행 예시
```bash
# 드로우다운 상관분석 실행
uv run scripts/correlation_analysis.py

# cap 적용 재백테스트 (예: cap=0.10)
MAX_ASSET_PCT=0.10 uv run scripts/apply_cap_and_retest.py

# cap 그리드: 간단한 반복 예시
for cap in 0.10 0.15 0.20; do
  MAX_ASSET_PCT=$cap uv run scripts/apply_cap_and_retest.py
done
```

참고/메모
- `.env`는 반드시 `pykrx` import 이전에 로드되어야 합니다(로그인 토큰 만료 이슈 방지).
- 워커 병렬 실행 시 `CHUNK_SIZE`/`WORKERS` 튜닝으로 네트워크 병목을 완화하세요.

커밋 정보
- 커밋 메시지: `feat: 상관관계·cap 스크립트 추가 및 진행상황 문서화`

문의/다음 단계 제안
- cap 그리드 실험을 제가 바로 돌려드릴까요? (예: 0.10,0.15,0.20) 디폴트로 3개 값을 실행하도록 하겠습니다.
