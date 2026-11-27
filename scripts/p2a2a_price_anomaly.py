"""P2-A-2a 부속: 2026-03-05 / 07-31 / 08-03 가격 이상 원인 조사(읽기 전용).

캐시 OHLCV, KRX 원천 재조회, KIS 조회 전용 시세(비수정/수정), NAV·거래량,
지수, 상장좌수, 기존 연구 산출물을 대조해
`실제 시장 변동` / `데이터 오류 확인` / `원인 미확인`으로 분류한다.

운영 캐시는 수정하지 않는다. 산출물은 `outputs_p2a2a/price_anomaly/`에만 쓴다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import p2a1_data_evidence as p2a1
import p2a2a_etf_distributions as evidence

OUTPUT_DIR = ROOT / "outputs_p2a2a" / "price_anomaly"
ANOMALY_DATES = [date(2026, 3, 5), date(2026, 7, 31), date(2026, 8, 3)]
METADATA_SNAPSHOT_DATE = "20260721"
KOSPI_CODES = {"KOSPI": "1001", "KOSPI200": "1028"}


def krx_session():
    from pykrx.website.comm.auth import build_krx_session

    session = build_krx_session()
    if session is None:
        raise RuntimeError("KRX login failed")
    return session


def krx_snapshot(session, trade_date: date) -> pd.DataFrame:
    """KRX 전종목 ETF 시세 스냅샷(NAV·상장좌수·등락률 포함)."""
    cache_path = ROOT / "outputs_p2a2a" / "raw" / "krx_snapshot" / f"{trade_date.isoformat()}.json"
    if cache_path.exists():
        payload = cache_path.read_bytes()
    else:
        response = session.session.post(
            "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd",
            data={
                "bld": "dbms/MDC/STAT/standard/MDCSTAT04301",
                "locale": "ko_KR",
                "trdDd": trade_date.strftime("%Y%m%d"),
                "share": "1",
                "money": "1",
                "csvxls_isNo": "false",
            },
            headers={
                "User-Agent": evidence.USER_AGENT,
                "Referer": (
                    "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/"
                    "index.cmd?menuId=MDC0201030104"
                ),
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=30,
        )
        payload = response.content
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(payload)
    rows = json.loads(payload.decode("utf-8")).get("output", [])
    frame = pd.DataFrame(rows)
    for column in [
        "TDD_CLSPRC",
        "TDD_OPNPRC",
        "TDD_HGPRC",
        "TDD_LWPRC",
        "NAV",
        "ACC_TRDVOL",
        "ACC_TRDVAL",
        "LIST_SHRS",
        "FLUC_RT",
    ]:
        if column in frame.columns:
            frame[column] = pd.to_numeric(
                frame[column].astype(str).str.replace(",", ""), errors="coerce"
            )
    return frame


def breadth(snapshot: pd.DataFrame) -> dict[str, Any]:
    rates = snapshot["FLUC_RT"].dropna()
    return {
        "etf_count": len(rates),
        "median_return_pct": round(float(rates.median()), 4),
        "mean_return_pct": round(float(rates.mean()), 4),
        "share_gt_5pct": round(float((rates.abs() > 5).mean()), 4),
        "share_gt_10pct": round(float((rates.abs() > 10).mean()), 4),
        "share_gt_15pct": round(float((rates.abs() > 15).mean()), 4),
        "share_up": round(float((rates > 0).mean()), 4),
    }


def cache_window(cache_dir: Path, ticker: str, start: date, end: date) -> pd.DataFrame:
    frame = pd.read_parquet(cache_dir / f"{ticker}.parquet")
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame[
        (frame["date"].dt.date >= start) & (frame["date"].dt.date <= end)
    ].copy()
    return frame.sort_values("date").reset_index(drop=True)


def missing_snapshot_row(ticker: str) -> dict[str, Any]:
    """스냅샷에 없는 종목도 미확보 행으로 남겨 집계에서 빠지지 않게 한다."""
    return {
        "ticker": ticker,
        "name": "",
        "cache_return_pct": None,
        "kis_flag1_raw_return_pct": None,
        "kis_flag0_adjusted_return_pct": None,
        "krx_snapshot_return_pct": None,
        "cache_vs_krx_day": {"status": "missing_snapshot", "identical": False},
        "cache_vs_kis": {"raw_abs_diff": None, "adjusted_abs_diff": None},
        "list_shares_prev": None,
        "list_shares_today": None,
        "list_shares_changed": None,
        "list_shares_ratio": None,
        "split_like": None,
        "snapshot_missing": True,
    }


def snapshot_day_frame(snapshot_row: pd.Series, day: date) -> pd.DataFrame:
    """저장된 KRX 스냅샷에서 당일 6개 필드+NAV를 꺼내 비교 프레임으로 만든다."""
    mapping = {
        "open": "TDD_OPNPRC",
        "high": "TDD_HGPRC",
        "low": "TDD_LWPRC",
        "close": "TDD_CLSPRC",
        "volume": "ACC_TRDVOL",
        "trading_value": "ACC_TRDVAL",
        "nav": "NAV",
    }
    row: dict[str, Any] = {"date": pd.Timestamp(day)}
    for target, source in mapping.items():
        value = snapshot_row.get(source)
        number = pd.to_numeric(str(value).replace(",", ""), errors="coerce")
        row[target] = float(number) if pd.notna(number) else float("nan")
    return pd.DataFrame([row])


KIS_BLOCK: list[str] = []


def kis_window(ticker: str, start: date, end: date, api) -> dict[str, pd.DataFrame]:
    """flag0(실측상 분배조정 계열)과 flag1(실측상 원주가)을 함께 가져온다.

    한 종목의 조회 실패는 그 종목만 미확인으로 남기고, rate limit은 이후 호출을 중단한다.
    """
    result = {}
    for label, flag in (("flag0", "0"), ("flag1", "1")):
        if KIS_BLOCK:
            result[label] = pd.DataFrame()
            result[f"{label}_error"] = KIS_BLOCK[0]
            continue
        try:
            frame = evidence.kis_daily_series(
                ticker, start, end, org_adj_prc=flag, api=api
            )
            result[label] = frame.rename(columns={"close": f"close_{label}"})[
                ["date", f"close_{label}"]
            ]
        except evidence.KIS_ERRORS as exc:
            reason = str(exc)[:160]
            result[label] = pd.DataFrame()
            result[f"{label}_error"] = reason
            if evidence.is_rate_limit_error(exc):
                KIS_BLOCK.append(f"kis_rate_limited: {reason}")
    return result


def impact(research_outputs: Path, cache_dir: Path, dates: list[date]) -> list[dict[str, Any]]:
    equity_path = research_outputs / "etf_equity_curve.csv"
    trades_path = research_outputs / "etf_trades.csv"
    rows: list[dict[str, Any]] = []
    if not equity_path.exists():
        return rows
    equity = pd.read_csv(equity_path)
    equity["date"] = pd.to_datetime(equity["date"])
    equity["return_pct"] = equity["equity_strategy"].pct_change() * 100
    trades = pd.read_csv(trades_path) if trades_path.exists() else pd.DataFrame()
    if not trades.empty:
        trades["date"] = pd.to_datetime(trades["date"])
    index_path = cache_dir / "index.parquet"
    index = pd.read_parquet(index_path) if index_path.exists() else pd.DataFrame()
    if not index.empty:
        index["date"] = pd.to_datetime(index["date"])
    for target in dates:
        match = equity[equity["date"] == pd.Timestamp(target)]
        if match.empty:
            rows.append({"date": target.isoformat(), "status": "no_row"})
            continue
        row = match.iloc[0]
        previous = equity[equity["date"] < pd.Timestamp(target)].tail(1)
        day_trades = trades[trades["date"] == pd.Timestamp(target)] if not trades.empty else trades
        idx_row = (
            index[index["date"] == pd.Timestamp(target)].iloc[0]
            if not index.empty and (index["date"] == pd.Timestamp(target)).any()
            else None
        )
        rows.append(
            {
                "date": target.isoformat(),
                "status": "ok",
                "holdings": str(row.get("holdings") or ""),
                "equity": float(row["equity_strategy"]),
                "equity_change_pct": round(float(row["return_pct"]), 4),
                "prev_equity": float(previous["equity_strategy"].iloc[0]) if not previous.empty else None,
                "distribution_cash": float(row.get("distribution_cash") or 0.0),
                "rebalance_decision": bool(row.get("rebalance_decision") or False),
                "rebalance_order_count": int(row.get("rebalance_order_count") or 0),
                "trades_today": len(day_trades),
                "market_risk_on": None if idx_row is None else bool(idx_row["risk_on"]),
                "market_ma": None if idx_row is None else float(idx_row["market_ma"]),
            }
        )
    return rows


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def strict_krx_comparison(cache: pd.DataFrame, fresh: pd.DataFrame) -> dict[str, Any]:
    """PR #15의 날짜·필드·결측 검증을 재사용해 전 필드/전 날짜 일치만 통과시킨다."""
    if cache.empty or fresh.empty:
        return {"status": "insufficient", "identical": False}
    comparison = p2a1.compare_price_frames(cache, fresh)
    expected_rows = len(cache)
    diffs = comparison.get("diffs") or {}
    fields_complete = bool(diffs) and all(value is not None for value in diffs.values())
    all_zero = fields_complete and all(float(value) == 0 for value in diffs.values())
    # 비교 범위 밖 날짜까지 포함해 양쪽 전체 날짜 집합이 같아야 한다.
    full_date_match = bool(
        set(pd.to_datetime(cache["date"])) == set(pd.to_datetime(fresh["date"]))
    )
    nav_ok = False
    if "nav" in cache.columns and "nav" in fresh.columns:
        merged = cache[["date", "nav"]].merge(fresh[["date", "nav"]], on="date", suffixes=("_c", "_f"))
        nav_ok = bool(
            len(merged) == expected_rows == len(fresh)
            and merged["nav_c"].notna().all()
            and merged["nav_f"].notna().all()
            and (merged["nav_c"] - merged["nav_f"]).abs().max() == 0
        )
    identical = bool(
        comparison.get("status") == "compared"
        and comparison.get("date_set_match")
        and full_date_match
        and comparison.get("overlap_rows") == expected_rows
        and not comparison.get("validation_errors")
        and all_zero
        and nav_ok
    )
    return {
        "status": comparison.get("status"),
        "identical": identical,
        "expected_rows": expected_rows,
        "overlap_rows": comparison.get("overlap_rows"),
        "date_set_match": comparison.get("date_set_match"),
        "full_date_set_match": full_date_match,
        "validation_errors": comparison.get("validation_errors"),
        "max_abs_diff": diffs,
        "nav_ok": nav_ok,
    }


def evaluate_confirmation(
    rows: list[dict[str, Any]],
    expected_tickers: list[str] | set[str] | None = None,
    tolerance: float = 0.05,
) -> dict[str, Any]:
    """검증 대상 전체의 유효한 비교가 있을 때만 확인 완료로 판정한다."""
    expected = sorted(
        {str(ticker) for ticker in (expected_tickers or [item.get("ticker") for item in rows])}
    )
    by_ticker = {
        str(item.get("ticker")): item for item in rows if str(item.get("ticker")) in expected
    }
    missing: list[str] = [ticker for ticker in expected if ticker not in by_ticker]
    mismatched: list[dict[str, Any]] = []
    checked = 0
    for ticker in expected:
        item = by_ticker.get(ticker)
        if item is None:
            continue
        cache_return = _finite(item.get("cache_return_pct"))
        kis_return = _finite(item.get("kis_flag1_raw_return_pct"))
        if cache_return is None or kis_return is None:
            missing.append(ticker)
            continue
        checked += 1
        diff = abs(cache_return - kis_return)
        if diff > tolerance:
            mismatched.append({"ticker": ticker, "abs_diff": round(diff, 6)})
    missing = sorted(set(missing))
    confirmed = bool(expected) and not missing and not mismatched and checked == len(expected)
    return {
        "kis_confirmed": confirmed,
        "kis_checked": checked,
        "kis_missing_tickers": missing,
        "expected_tickers": len(expected),
        "kis_mismatch_tickers": mismatched,
        "kis_max_abs_return_diff": round(
            max(
                (
                    abs(
                        _finite(item.get("cache_return_pct"))
                        - _finite(item.get("kis_flag1_raw_return_pct"))
                    )
                    for item in rows
                    if _finite(item.get("cache_return_pct")) is not None
                    and _finite(item.get("kis_flag1_raw_return_pct")) is not None
                ),
                default=0.0,
            ),
            6,
        ),
    }


def classify(date_result: dict[str, Any]) -> str:
    # 원천 간 불일치를 먼저 본다. 불일치는 데이터 오류 확정이 아니라 원천 불일치다.
    if date_result.get("source_mismatch"):
        return "source_mismatch"
    if not date_result.get("kis_confirmed") or not date_result.get("evidence_complete"):
        return "unconfirmed"
    index_moves = [
        abs(move)
        for move in (
            _finite(value.get("return_pct"))
            for value in date_result.get("index", {}).values()
            if isinstance(value, dict)
        )
        if move is not None
    ]
    breadth = date_result.get("breadth", {})
    share_gt_10 = _finite(breadth.get("share_gt_10pct")) or 0.0
    median_return = _finite(breadth.get("median_return_pct")) or 0.0
    if (
        (index_moves and max(index_moves) >= 5)
        or share_gt_10 >= 0.15
        or abs(median_return) >= 2
    ):
        return "market_move_confirmed_by_independent_feed"
    return "unconfirmed"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(os.environ.get("ETF_PRICE_CACHE_DIR", ROOT / "data_cache")),
    )
    parser.add_argument(
        "--research-outputs",
        type=Path,
        default=ROOT / "outputs_etf_only",
    )
    args = parser.parse_args()

    evidence.load_env_file(Path(os.environ.get("ETF_ENV_FILE", ROOT / ".env")))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    session = krx_session()
    api = evidence.kis_api()

    events = pd.read_csv(
        ROOT / "outputs_p2a2a" / "distribution_events.csv", dtype={"ticker": str}
    )
    ex_dates = {
        date.fromisoformat(value)
        for value in events["ex_date"].dropna().astype(str)
        if value
    }

    tickers = evidence.freeze_universe()
    calendar = evidence.build_market_calendar(args.cache_dir, tickers)

    snapshots = {}
    for day in ANOMALY_DATES:
        snapshots[day] = krx_snapshot(session, day)
        if day in calendar:
            snapshots[calendar[calendar.index(day) - 1]] = krx_snapshot(
                session, calendar[calendar.index(day) - 1]
            )
    results = []
    for day in ANOMALY_DATES:
        snapshot = snapshots[day]
        day_result: dict[str, Any] = {
            "date": day.isoformat(),
            "is_trading_day": day in calendar,
            "breadth": breadth(snapshot),
            "index": {},
            "tickers": [],
            "ex_dates_nearby": sorted(
                value.isoformat()
                for value in ex_dates
                if abs((value - day).days) <= 7
            ),
        }
        for name, code in KOSPI_CODES.items():
            try:
                from pykrx import stock

                idx = stock.get_index_ohlcv_by_date(
                    (day - pd.Timedelta(days=10)).strftime("%Y%m%d"),
                    (day + pd.Timedelta(days=10)).strftime("%Y%m%d"),
                    code,
                )
                if idx is not None and not idx.empty:
                    series = idx["종가"].pct_change() * 100
                    day_result["index"][name] = {
                        "return_pct": round(float(series.loc[pd.Timestamp(day)]), 4)
                        if pd.Timestamp(day) in series.index
                        else None,
                        "close": float(idx.loc[pd.Timestamp(day), "종가"])
                        if pd.Timestamp(day) in idx.index
                        else None,
                    }
            except (RuntimeError, ValueError, KeyError) as exc:
                day_result["index"][name] = {"error": str(exc)[:120]}

        for ticker in tickers:
            row = snapshot[snapshot["ISU_SRT_CD"] == ticker]
            if row.empty:
                day_result["tickers"].append(missing_snapshot_row(ticker))
                continue
            snapshot_row = row.iloc[0]
            cache = cache_window(args.cache_dir, ticker, day - pd.Timedelta(days=8), day + pd.Timedelta(days=8))
            # 저장된 KRX 스냅샷(공식 원천)과 당일 행을 전 필드·NAV까지 대조한다.
            fresh = snapshot_day_frame(snapshot_row, day)
            cache_day = cache[cache["date"] == pd.Timestamp(day)]
            comparison = strict_krx_comparison(cache_day, fresh)
            comparison["source"] = "krx_snapshot_saved"
            kis = kis_window(ticker, day - pd.Timedelta(days=8), day + pd.Timedelta(days=8), api)
            cache_ret = None
            raw_ret = None
            adj_ret = None
            if not cache.empty:
                cache = cache.assign(ret=cache["close"].pct_change() * 100)
                match = cache[cache["date"] == pd.Timestamp(day)]
                cache_ret = round(float(match["ret"].iloc[0]), 4) if not match.empty else None
            if not kis["flag1"].empty:
                raw = kis["flag1"].copy()
                raw["ret"] = raw["close_flag1"].pct_change() * 100
                match = raw[raw["date"] == pd.Timestamp(day)]
                raw_ret = round(float(match["ret"].iloc[0]), 4) if not match.empty else None
            if not kis["flag0"].empty:
                adj = kis["flag0"].copy()
                adj["ret"] = adj["close_flag0"].pct_change() * 100
                match = adj[adj["date"] == pd.Timestamp(day)]
                adj_ret = round(float(match["ret"].iloc[0]), 4) if not match.empty else None

            shares_prev = None
            prev_day = calendar[calendar.index(day) - 1] if day in calendar else None
            prev_rows = snapshots.get(prev_day) if prev_day is not None else None
            if prev_rows is not None:
                prev = prev_rows[prev_rows["ISU_SRT_CD"] == ticker]
                if not prev.empty and pd.notna(prev["LIST_SHRS"].iloc[0]):
                    shares_prev = float(prev["LIST_SHRS"].iloc[0])
            value_today = snapshot_row.get("LIST_SHRS")
            shares_today = float(value_today) if pd.notna(value_today) else None

            day_result["tickers"].append(
                {
                    "ticker": ticker,
                    "name": str(snapshot_row.get("ISU_ABBRV") or ""),
                    "cache_return_pct": cache_ret,
                    "kis_flag1_raw_return_pct": raw_ret,
                    "kis_flag0_adjusted_return_pct": adj_ret,
                    "krx_snapshot_return_pct": round(float(snapshot_row["FLUC_RT"]), 4),
                    "cache_vs_krx_day": comparison,
                    "cache_vs_kis": {
                        "raw_abs_diff": None
                        if raw_ret is None or cache_ret is None
                        else round(abs(raw_ret - cache_ret), 6),
                        "adjusted_abs_diff": None
                        if adj_ret is None or cache_ret is None
                        else round(abs(adj_ret - cache_ret), 6),
                    },
                    "nav": float(snapshot_row["NAV"]) if pd.notna(snapshot_row.get("NAV")) else None,
                    "close": float(snapshot_row["TDD_CLSPRC"]),
                    "nav_premium_pct": round(
                        (float(snapshot_row["TDD_CLSPRC"]) / float(snapshot_row["NAV"]) - 1) * 100,
                        4,
                    )
                    if pd.notna(snapshot_row.get("NAV")) and float(snapshot_row["NAV"]) > 0
                    else None,
                    "volume": float(snapshot_row["ACC_TRDVOL"])
                    if pd.notna(snapshot_row.get("ACC_TRDVOL"))
                    else None,
                    "trading_value": float(snapshot_row["ACC_TRDVAL"])
                    if pd.notna(snapshot_row.get("ACC_TRDVAL"))
                    else None,
                    "list_shares_prev": shares_prev,
                    "list_shares_today": shares_today,
                    "list_shares_changed": (
                        None
                        if shares_prev is None or shares_today is None
                        else abs(shares_today - shares_prev) > 0
                    ),
                    "list_shares_ratio": (
                        None
                        if not shares_prev or shares_today is None
                        else round(shares_today / shares_prev, 6)
                    ),
                    # ETF 좌수는 설정/환매로 매일 변한다. 분할·병합은 ±50% 이상 급변으로 본다.
                    "split_like": (
                        None
                        if not shares_prev or shares_today is None
                        else bool(shares_today / shares_prev >= 1.5 or shares_today / shares_prev <= 0.5)
                    ),
                }
            )

        confirmation = evaluate_confirmation(day_result["tickers"], tickers)
        day_result.update(confirmation)
        krx_compared = [
            item
            for item in day_result["tickers"]
            if item["cache_vs_krx_day"].get("status") == "compared"
        ]
        krx_mismatch = [
            item["ticker"]
            for item in krx_compared
            if item["cache_vs_krx_day"].get("identical") is not True
        ]
        krx_incomplete = [
            item["ticker"]
            for item in day_result["tickers"]
            if item["cache_vs_krx_day"].get("status") != "compared"
        ]
        day_result["krx_compared"] = len(krx_compared)
        day_result["krx_mismatch_tickers"] = krx_mismatch
        day_result["krx_incomplete_tickers"] = krx_incomplete
        day_result["source_mismatch"] = bool(
            confirmation["kis_mismatch_tickers"] or krx_mismatch
        )
        day_result["evidence_complete"] = bool(
            not krx_incomplete and not krx_mismatch and not confirmation["kis_missing_tickers"]
        )
        day_result["kis_contradicted"] = bool(confirmation["kis_mismatch_tickers"])
        day_result["shares_changed_tickers"] = [
            item["ticker"]
            for item in day_result["tickers"]
            if item["list_shares_changed"] is True
        ]
        day_result["shares_unknown_tickers"] = [
            item["ticker"]
            for item in day_result["tickers"]
            if item["list_shares_changed"] is None
        ]
        day_result["split_like_tickers"] = [
            item["ticker"] for item in day_result["tickers"] if item["split_like"] is True
        ]
        day_result["max_list_shares_ratio"] = max(
            (
                max(item["list_shares_ratio"], 1 / item["list_shares_ratio"])
                for item in day_result["tickers"]
                if item["list_shares_ratio"]
            ),
            default=None,
        )
        day_result["classification"] = classify(day_result)
        results.append(day_result)

    impact_rows = impact(args.research_outputs, args.cache_dir, ANOMALY_DATES)
    summary = {
        "generated_at": evidence.utc_now(),
        "dates": [day.isoformat() for day in ANOMALY_DATES],
        "ticker_count": len(tickers),
        "calendar_source": "가격 캐시 거래일 union",
        "calendar_start": calendar[0].isoformat() if calendar else "",
        "calendar_end": calendar[-1].isoformat() if calendar else "",
        "results": results,
        "impact": impact_rows,
        "conclusion": {
            "classification": sorted({item["classification"] for item in results}),
            "note": (
                "KIS 비수정 종가가 캐시/ KRX와 같은 방향·크기의 변동을 보이면 캐시 손상이 아니라 "
                "시장 변동으로 본다. 다만 실세계 검증은 이 환경 범위 밖이다."
            ),
        },
    }
    (OUTPUT_DIR / "price_anomaly.json").write_text(
        json.dumps(evidence.sanitize_json(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    flat = []
    for item in results:
        for ticker_row in item["tickers"]:
            flat.append({"date": item["date"], **ticker_row})
    pd.DataFrame(flat).to_csv(OUTPUT_DIR / "price_anomaly_tickers.csv", index=False)
    pd.DataFrame(
        [
            {"date": item["date"], **item["breadth"], "classification": item["classification"]}
            for item in results
        ]
    ).to_csv(OUTPUT_DIR / "price_anomaly_breadth.csv", index=False)
    pd.DataFrame(impact_rows).to_csv(OUTPUT_DIR / "price_anomaly_impact.csv", index=False)
    print(
        f"[p2a2a-anomaly] 완료: {OUTPUT_DIR} "
        f"({', '.join(item['date'] + ':' + item['classification'] for item in results)})"
    )


if __name__ == "__main__":
    main()
