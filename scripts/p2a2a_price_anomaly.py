"""P2-A-2a 부속: 2026-03-05 / 07-31 / 08-03 가격 이상 원인 조사(읽기 전용).

캐시 OHLCV, KRX 원천 재조회, KIS 조회 전용 시세(비수정/수정), NAV·거래량,
지수, 상장좌수, 기존 연구 산출물을 대조해
`실제 시장 변동` / `데이터 오류 확인` / `원인 미확인`으로 분류한다.

운영 캐시는 수정하지 않는다. 산출물은 `outputs_p2a2a/price_anomaly/`에만 쓴다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
    for column in ["TDD_CLSPRC", "NAV", "ACC_TRDVOL", "ACC_TRDVAL", "LIST_SHRS", "FLUC_RT"]:
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


def krx_refetch(ticker: str, start: date, end: date) -> pd.DataFrame:
    from pykrx import stock

    try:
        raw = stock.get_etf_ohlcv_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker
        )
    except (RuntimeError, ValueError, KeyError):
        return pd.DataFrame()
    if raw is None or raw.empty:
        return pd.DataFrame()
    frame = raw.reset_index().rename(
        columns={
            "날짜": "date",
            "NAV": "nav",
            "시가": "open",
            "고가": "high",
            "저가": "low",
            "종가": "close",
            "거래량": "volume",
            "거래대금": "trading_value",
        }
    )
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.sort_values("date").reset_index(drop=True)


def compare_frames(cache: pd.DataFrame, fresh: pd.DataFrame) -> dict[str, Any]:
    if cache.empty or fresh.empty:
        return {"status": "insufficient"}
    merged = cache.merge(fresh, on="date", suffixes=("_cache", "_krx"), how="inner")
    if merged.empty:
        return {"status": "no_overlap"}
    diffs = {}
    for column in ["open", "high", "low", "close", "volume", "trading_value", "nav"]:
        left = f"{column}_cache"
        right = f"{column}_krx"
        if left not in merged.columns or right not in merged.columns:
            continue
        delta = (merged[left] - merged[right]).abs().max()
        diffs[column] = None if pd.isna(delta) else float(delta)
    return {
        "status": "compared",
        "rows": len(merged),
        "max_abs_diff": diffs,
        "identical": bool(
            diffs and all(value is not None and value == 0 for value in diffs.values())
        ),
    }


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


def classify(date_result: dict[str, Any]) -> str:
    index_moves = [
        abs(value["return_pct"])
        for value in date_result.get("index", {}).values()
        if isinstance(value, dict) and value.get("return_pct") is not None
    ]
    breadth = date_result.get("breadth", {})
    if date_result.get("kis_confirmed") and (
        (index_moves and max(index_moves) >= 5)
        or breadth.get("share_gt_10pct", 0) >= 0.15
        or abs(breadth.get("median_return_pct", 0)) >= 2
    ):
        return "market_move_confirmed_by_independent_feed"
    if date_result.get("kis_contradicted"):
        return "data_error_confirmed"
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
                continue
            snapshot_row = row.iloc[0]
            cache = cache_window(args.cache_dir, ticker, day - pd.Timedelta(days=8), day + pd.Timedelta(days=8))
            fresh = krx_refetch(ticker, day - pd.Timedelta(days=8), day + pd.Timedelta(days=8))
            comparison = compare_frames(cache, fresh)
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
                    "cache_vs_krx_refetch": comparison,
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

        big = [
            item
            for item in day_result["tickers"]
            if abs(item["cache_return_pct"] or 0) >= 5
        ]
        diffs = [
            abs(item["kis_flag1_raw_return_pct"] - item["cache_return_pct"])
            for item in big
            if item["kis_flag1_raw_return_pct"] is not None
        ]
        day_result["kis_confirmed"] = bool(big and diffs and max(diffs) < 0.05)
        day_result["kis_max_abs_return_diff"] = round(max(diffs), 6) if diffs else None
        day_result["kis_missing"] = sum(
            1 for item in big if item["kis_flag1_raw_return_pct"] is None
        )
        day_result["kis_contradicted"] = any(
            item["kis_flag1_raw_return_pct"] is not None
            and abs(item["kis_flag1_raw_return_pct"] - item["cache_return_pct"]) > 1.0
            for item in day_result["tickers"]
        )
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
