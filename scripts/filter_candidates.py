#!/usr/bin/env python3
"""
후보 ETF 필터 스크립트

- pykrx에서 ETF 티커 목록을 조회하고, 과거 OHLCV 기반으로 유동성 필터(평균 거래대금/평균 거래량)를 적용합니다.
- 상위 N개 후보를 `outputs_grid/filtered_etf_list.json` 및 `.txt`에 저장합니다.
- 옵션으로 필터된 리스트로 간단 백테스트를 실행해 결과를 저장합니다.
"""
from __future__ import annotations

import sys
import os
import json
import traceback
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 프로젝트 루트의 .env 파일을 실행 초기에 로드하여 KRX 자격증명 등 환경변수를 설정합니다.
def _load_dotenv(dotenv_path: Path | str | None = None) -> None:
    if dotenv_path is None:
        dotenv_path = ROOT / ".env"
    p = Path(dotenv_path)
    if not p.exists():
        return
    try:
        with p.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        # 실패해도 무시하고 계속 진행 (환경변수가 없어도 동작하도록)
        pass


# .env를 먼저 로드한 뒤 pykrx를 import 합니다 (pykrx는 모듈 import 시 KRX 세션을 초기화함)
_load_dotenv()

import pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing

try:
    from pykrx import stock
except Exception:
    stock = None

import run_etf_backtest as rtb


OUT = Path("outputs_grid")
OUT.mkdir(exist_ok=True)


def fetch_all_etfs() -> list[str]:
    if stock is None:
        raise RuntimeError("pykrx 패키지가 필요합니다")
    return [str(t) for t in stock.get_etf_ticker_list()]


def analyze_ticker(ticker: str) -> Optional[dict]:
    try:
        print(f"[filter] 분석 시작: {ticker}")
        sys.stdout.flush()
        df = rtb.get_price(ticker)
    except Exception as e:
        print(f"[filter] 오류: {ticker} 데이터 조회 실패: {e}")
        return None
    if df is None or df.empty:
        print(f"[filter] 경고: {ticker} 조회결과가 없습니다")
        return None

    # 거래대금/거래량/종가가 없는 경우는 허용하되, 가능하면 해당 컬럼으로 필터링
    if "trading_value" in df.columns and "volume" in df.columns and "close" in df.columns:
        df2 = df.dropna(subset=["trading_value", "volume", "close"])
    else:
        df2 = df

    if df2.empty:
        print(f"[filter] 경고: {ticker} 유효한 행이 없습니다 (dropna 후)")
        return None

    try:
        avg_tv = float(df2["trading_value"].mean()) if "trading_value" in df2.columns else 0.0
    except Exception:
        avg_tv = 0.0
    avg_vol = float(df2["volume"].mean()) if "volume" in df2.columns else 0.0
    last_close = float(df2["close"].dropna().iloc[-1]) if not df2["close"].dropna().empty else 0.0
    return {
        "ticker": ticker,
        "avg_trading_value": avg_tv,
        "avg_volume": avg_vol,
        "last_close": last_close,
        "days": int(len(df2)),
    }


def _worker_analyze_ticker(ticker: str) -> Optional[dict]:
    """워커 프로세스에서 실행되는 함수: rtb 모듈을 로컬로 import 하여 데이터를 가져옵니다.
    ProcessPoolExecutor에서 안전하게 실행되도록 모듈 임포트를 내부에서 수행합니다.
    """
    try:
        import run_etf_backtest as rtb_local
    except Exception as e:  # pragma: no cover - 환경 문제 방지
        return {"ticker": ticker, "error": f"import 실패: {e}"}

    try:
        df = rtb_local.get_price(ticker)
    except Exception as e:
        return {"ticker": ticker, "error": str(e)}

    if df is None or df.empty:
        return None

    if "trading_value" in df.columns and "volume" in df.columns and "close" in df.columns:
        df2 = df.dropna(subset=["trading_value", "volume", "close"])
    else:
        df2 = df

    if df2.empty:
        return None

    try:
        avg_tv = float(df2["trading_value"].mean()) if "trading_value" in df2.columns else 0.0
    except Exception:
        avg_tv = 0.0
    avg_vol = float(df2["volume"].mean()) if "volume" in df2.columns else 0.0
    last_close = float(df2["close"].dropna().iloc[-1]) if not df2["close"].dropna().empty else 0.0
    return {
        "ticker": ticker,
        "avg_trading_value": avg_tv,
        "avg_volume": avg_vol,
        "last_close": last_close,
        "days": int(len(df2)),
    }


def main():
    top_n = int(os.environ.get("TOP_N", "12"))
    min_avg_trading_value = float(os.environ.get("MIN_AVG_TRADING_VALUE", str(50_000_000)))
    min_avg_volume = float(os.environ.get("MIN_AVG_VOLUME", "0"))
    run_backtest = os.environ.get("RUN_BACKTEST", "1") in {"1", "true", "True", "yes"}
    max_positions = int(os.environ.get("MAX_POSITIONS", "3"))
    rebalance_days = int(os.environ.get("REBALANCE_STEP_DAYS", str(rtb.REBALANCE_STEP_DAYS)))

    print(f"[filter] TOP_N={top_n}, MIN_AVG_TRADING_VALUE={min_avg_trading_value:,}, MIN_AVG_VOLUME={min_avg_volume:,}")

    try:
        all_etfs = fetch_all_etfs()
    except Exception as e:
        print("ETF 목록 조회 실패:", e)
        return

    rows = []
    # 체크포인트/청크 처리 옵션
    start_index = int(os.environ.get("START_INDEX", "0"))
    chunk_size = int(os.environ.get("CHUNK_SIZE", "200"))
    checkpoint_path = OUT / "filter_checkpoint.json"
    # 개발/디버그용: 처리 티커 수 제한 (env: LIMIT_ETFS)
    limit = int(os.environ.get("LIMIT_ETFS", "0"))
    if limit > 0:
        print(f"[filter] LIMIT_ETFS 활성화: 처음 {limit}개 티커만 처리합니다")
        all_etfs = all_etfs[:limit]

    # 병렬 처리 설정
    workers_env = os.environ.get("WORKERS")
    default_workers = max(2, min((multiprocessing.cpu_count() or 4), 8))
    workers = int(workers_env) if workers_env and workers_env.isdigit() else default_workers
    print(f"[filter] 병렬 처리: WORKERS={workers}")
    sys.stdout.flush()

    total = len(all_etfs)
    processed = 0

    # 청크 단위로 ProcessPoolExecutor를 재생성하여 네트워크/워커 문제로 인한 정지를 완화합니다.
    # START_INDEX, CHUNK_SIZE 환경변수로 재시작/배치크기 설정이 가능합니다.
    if checkpoint_path.exists() and start_index == 0:
        try:
            cp = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            rows = cp.get("rows", [])
            start_index = int(cp.get("last_index", 0))
            processed = start_index
            print(f"[filter] 체크포인트 로드: last_index={start_index}, 누적행={len(rows)}")
        except Exception:
            print("[filter] 체크포인트 로드 실패 — 새로 시작합니다")

    for start in range(start_index, total, chunk_size):
        chunk = all_etfs[start : start + chunk_size]
        if not chunk:
            break
        print(f"[filter] 처리 범위: {start}..{start + len(chunk) - 1} (총 {len(chunk)}개)")
        sys.stdout.flush()

        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_worker_analyze_ticker, t): t for t in chunk}
            for fut in as_completed(futures):
                t = futures[fut]
                try:
                    info = fut.result()
                    if info is None:
                        pass
                    elif isinstance(info, dict) and info.get("error"):
                        print(f"[filter] 워커 오류: {t} -> {info.get('error')}")
                    else:
                        rows.append(info)
                except Exception as e:
                    print(f"[filter] 워커 예외: {t} -> {e}")
                    traceback.print_exc()

                processed += 1
                if processed % 10 == 0:
                    print(f"[filter] 진행 상태: {processed}/{total} 완료")

        # 청크 완료 후 체크포인트 저장
        try:
            checkpoint_path.write_text(json.dumps({"last_index": start + len(chunk), "rows": rows}, ensure_ascii=False), encoding="utf-8")
            print(f"[filter] 체크포인트 저장: {checkpoint_path} (last_index={start + len(chunk)})")
        except Exception:
            print("[filter] 체크포인트 저장 실패")

    if not rows:
        print("수집된 데이터 없음")
        return

    df = pd.DataFrame(rows).sort_values("avg_trading_value", ascending=False).reset_index(drop=True)

    # 우선 필터 적용 후 개수가 부족하면 상위 N으로 채움
    filtered = df[(df["avg_trading_value"] >= min_avg_trading_value) & (df["avg_volume"] >= min_avg_volume)].copy()
    if len(filtered) < top_n:
        print(f"[filter] 임계값 기준으로 후보 부족: {len(filtered)}개 → 상위 {top_n}개로 보완")
        filtered = df.head(top_n)
    else:
        filtered = filtered.head(top_n)

    final_list = filtered["ticker"].tolist()

    (OUT / "filtered_etf_list.json").write_text(json.dumps(final_list, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "filtered_etf_list.txt").write_text("\n".join(final_list), encoding="utf-8")
    print(f"[filter] 저장 완료: {OUT / 'filtered_etf_list.json'} (count={len(final_list)})")

    # 간단 백테스트 실행
    if run_backtest:
        print("[filter] 필터된 리스트로 간단 백테스트 실행")
        try:
            index_df = rtb.get_index_data()
            common_dates = list(index_df["date"])

            rtb.ETF_LIST = final_list
            rtb.REBALANCE_STEP_DAYS = int(rebalance_days)

            result, trades = rtb.run_etf_strategy(
                rtb.INITIAL_CASH,
                common_dates,
                index_df,
                use_market_filter=True,
                max_positions=int(max_positions),
                slippage=float(rtb.BASE_SLIPPAGE),
            )

            fname = OUT / f"filtered_curve_n{len(final_list)}_pos{max_positions}_reb{rebalance_days}.csv"
            result.to_csv(fname, index=False, encoding="utf-8-sig")
            trades.to_csv(OUT / f"filtered_trades_n{len(final_list)}_pos{max_positions}_reb{rebalance_days}.csv", index=False, encoding="utf-8-sig")

            stats = rtb.calc_stats(result, "equity")
            # 사람이 읽기 쉬운 형식으로 포맷하여 출력
            keys = ("cagr", "mdd", "sharpe", "total_return")
            stats_vals = {k: float(stats[k]) if (k in stats and stats[k] is not None) else None for k in keys}
            print("[filter][backtest] 결과 요약:")
            if stats_vals["cagr"] is not None:
                print(f"  CAGR: {stats_vals['cagr']:.2%}")
            else:
                print("  CAGR: N/A")
            if stats_vals["mdd"] is not None:
                print(f"  Max Drawdown: {stats_vals['mdd']:.2%}")
            else:
                print("  Max Drawdown: N/A")
            if stats_vals["sharpe"] is not None:
                print(f"  Sharpe Ratio: {stats_vals['sharpe']:.3f}")
            else:
                print("  Sharpe Ratio: N/A")
            if stats_vals["total_return"] is not None:
                print(f"  Total Return: {stats_vals['total_return']:.2%}")
            else:
                print("  Total Return: N/A")
        except Exception as e:
            print("[filter] 백테스트 실행 실패:", e)
            traceback.print_exc()


if __name__ == "__main__":
    main()
