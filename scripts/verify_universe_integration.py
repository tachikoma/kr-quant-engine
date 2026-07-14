"""하이브리드 검증 — 2단계(캐시된 데이터로 자동모드 백테스트 통합).

pykrx_utils.load_tax_classification()을 캐시된 티커로만 제한하여 패치한 뒤,
ETF_UNIVERSE_MODE=auto 로 run_etf_backtest 를 실제 구동한다.
캐시 범위 내 윈도우를 사용하므로 ETF 가격 네트워크 호출은 발생하지 않는다
(KRX 지수 1건 정도의 경미한 호출은 있을 수 있음).

실행:
    uv run python scripts/verify_universe_integration.py
"""

from __future__ import annotations

from pathlib import Path

import glob
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

OUTPUT_CURVE = "outputs_etf_only/etf_equity_curve.csv"


def _cached_window() -> tuple[str, str]:
    """캐시된 parquet 전체 범위를 기반으로 warmup 을 포함하는 윈도우를 계산한다."""
    frames = []
    for p in glob.glob("data_cache/*.parquet"):
        try:
            d = pd.read_parquet(p, columns=["date"])
            frames.append(pd.to_datetime(d["date"], errors="coerce"))
        except Exception:
            continue
    if not frames:
        raise RuntimeError("캐시된 parquet 없음")
    all_dates = pd.concat(frames).dropna().sort_values()
    lo = all_dates.min()
    hi = all_dates.max()
    # 최근 1년 윈도우 (캐시 범위 내라 ETF fetch 없음). warmup 120일은 캐시 안에 있음.
    start = hi - pd.Timedelta(days=365)
    if start < lo + pd.Timedelta(days=130):
        start = lo + pd.Timedelta(days=130)
    return start.strftime("%Y%m%d"), hi.strftime("%Y%m%d")


def main() -> int:
    os.environ["ETF_UNIVERSE_MODE"] = "auto"
    # ETF_LIST env 가 있으면 static 으로 강제되므로 제거
    os.environ.pop("ETF_LIST", None)

    cached = {p.split("/")[-1][:-8] for p in glob.glob("data_cache/*.parquet")}
    if not cached:
        print("✗ 캐시된 parquet 없음")
        return 1

    import pykrx_utils

    _orig = pykrx_utils.load_tax_classification

    def _patched(*args, **kwargs) -> pd.DataFrame:
        df = _orig(*args, **kwargs)
        return df[df["ISU_SRT_CD"].astype(str).isin(cached)]

    pykrx_utils.load_tax_classification = _patched

    start, end = _cached_window()
    print(f"[Step 2] 자동모드 백테스트 (캐시 윈도우 {start} ~ {end}, {len(cached)}종목 후보)")

    sys.argv = ["run_etf_backtest.py", "--start", start, "--end", end]
    import run_etf_backtest

    # etf_shared 가 패치된 로더로 자동 구축했는지 확인
    print(f"  → etf_shared.ETF_LIST 크기: {len(run_etf_backtest.ETF_LIST)}")
    print(f"  → universe_mode: {getattr(run_etf_backtest, 'UNIVERSE_MODE', '?')}")

    try:
        run_etf_backtest.main()
    except SystemExit as e:
        if e.code not in (None, 0):
            print(f"✗ backtest 종료 코드: {e.code}")
            return 1
    except Exception as exc:
        import traceback

        traceback.print_exc()
        print(f"✗ backtest 실행 예외: {exc}")
        return 1

    if not os.path.exists(OUTPUT_CURVE):
        print(f"✗ 출력 누락: {OUTPUT_CURVE}")
        return 1
    curve = pd.read_csv(OUTPUT_CURVE)
    if curve.empty:
        print("✗ equity curve 가 비어있음")
        return 1

    print(f"  → equity curve 행수: {len(curve)}, 출력: {OUTPUT_CURVE}")
    print("\n[Step 2] PASS: 자동모드 백테스트가 캐시된 데이터로 정상 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
