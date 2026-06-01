# 진행상황 및 향후 작업

작성일: 2026-06-01

요약
- docs: README, AGENTS.md, .env.sample 문서를 현재 코드 기준으로 전면 갱신
- KIS 어댑터(`live_trading/kis_adapter.py`), 텔레그램 알림(`telegram_notifier.py`) 안정화 완료
- 백테스트 슬리피지/스프레드 모델, 매매차익 과세(TAXABLE_ETF_TICKERS) 반영 완료

이번 커밋에서 변경된 사항
- `README.md` — CLI 인자 문서화, KIS/텔레그렘/과세 env var 보강, scripts 15개 목록 추가, experiment 출력 상세화
- `AGENTS.md` — env var 테이블 확장, KIS/텔레그램/그리드 진입점 추가, output layout 보강
- `.env.sample` — `FORCE_REBALANCE`, `FORCE_LIVE_CUTOFF_EXTEND_MIN`, `ETF_USE_CACHE`, `ETF_REFRESH_CACHE`, `ETF_LIST` 오버라이드 추가
- `DOCS/PROGRESS_AND_NEXT_STEPS.md` — 최신 상태로 갱신

현재 상태(핵심 결과)
- ETF 백테스트 (single/experiment 모드) 정상 동작
- 데일리 러너 안전모드 + 실전 모드 (Kiwoom/KIS 선택 가능)
- 텔레그램 주문/체결 알림 연동
- 매매차익 과세 8개 ETF 자동 반영 (15.4%)
- MAX_ASSET_PCT 기반 자산별 비중 제한
- 그리드/상관관계/cap 분석 스크립트 15개 운영 중

권장되는 다음 작업(우선순위)
1. cap 그리드 실험: `MAX_ASSET_PCT` 값을 여러 값(예: 0.10, 0.15, 0.20)으로 돌려 성능·MDD 변화 비교
2. 리밸런스/포지션 민감도 테스트: `REBALANCE_STEP_DAYS`와 `MAX_POSITIONS` 민감도 실험
3. `scripts/filter_and_retest_by_risk.py`를 이용한 리스크 기반 후보군 필터링 그리드 실행
4. 결과 비교 보고서 및 최종 추천 필터/정책 문서화(시각화 포함)

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
