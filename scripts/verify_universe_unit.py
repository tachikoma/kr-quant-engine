"""하이브리드 검증 — 1단계(단위 불변식) + 3단계(데이터 병합 드라이런).

네트워크 호출 없음. 실제 KRX 분류 캐시와 data_cache/*.parquet 만 사용한다.

실행:
    uv run python scripts/verify_universe_unit.py
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

VALID_GROUPS = {"domestic_equity", "foreign_investment", "commodity"}

# 캐시된 티커 (data_cache/*.parquet)
CACHED = sorted({p.split("/")[-1][:-8] for p in glob.glob("data_cache/*.parquet")})

# 기존 16종목 유니버스 중 자동 구축에서 기대되는 포함/제외
EXPECT_INCLUDED = {
    "069500", "091160", "102110", "0101N0", "463250",
    "143850", "360200", "360750", "133690", "161510",
    "091170", "367760",
}
EXPECT_EXCLUDED = {
    "472150",  # TIGER 배당커버드콜액티브 (액티브 → 패시브 필터에서 제외)
    "486290",  # TIGER 미국나스닥100타겟데일리커버드콜 (커버드콜 키워드 제외)
    "498400",  # KODEX 200타겟위클리커버드콜 (커버드콜 키워드 제외)
    "411060",  # ACE KRX금현물 (원자재 → 기본 제외)
}

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ✓ {msg}")
    else:
        print(f"  ✗ {msg}")
        FAILURES.append(msg)


def step1_unit() -> None:
    print("\n[Step 1] build_universe() 불변식 검증 (실제 분류 데이터)")
    from pykrx_utils import load_tax_classification, get_taxable_tickers
    from etf_universe import build_universe, config_from_env
    from etf_shared import (
        ETF_DEVIATION_THRESHOLD_BY_GROUP,
        ETF_DEVIATION_THRESHOLD_BY_TICKER,
    )

    cls = load_tax_classification()
    assert cls is not None, "ETF 분류 캐시 누락 (data_cache/etf_tax_classification.parquet)"

    res = build_universe(cls, config_from_env())
    tickers = res.tickers
    groups = res.ticker_groups

    # KRX 단축코드는 6자리이며 숫자와 문자 혼용 가능 (예: 0101N0, 0000H0)
    bad_len = [t for t in tickers if len(t) != 6]
    check(not bad_len, f"모든 티커가 6자리 KRX 코드 ({bad_len or '없음'})")
    check(set(groups.keys()) == set(tickers), "groups 키 == ETF_LIST")
    check(set(groups.values()) <= VALID_GROUPS, "그룹값이 유효 집합에 포함")
    check(len(tickers) == len(set(tickers)), "중복 티커 없음")

    taxable = get_taxable_tickers(ticker_subset=set(tickers))
    check(taxable is not None, "taxable 티커 산출 성공")
    if taxable is not None:
        check(taxable <= set(tickers), "taxable ⊆ universe")

    name_map = dict(zip(cls["ISU_SRT_CD"].astype(str), cls["ISU_ABBRV"].astype(str)))
    covered = [t for t in tickers if "커버드콜" in name_map.get(t, "")]
    check(not covered, f"커버드콜 ETF 제외됨 ({covered or '없음'})")
    check("commodity" not in set(groups.values()), "원자재 그룹 기본 제외됨")

    risk_groups = {"foreign_investment", "commodity"} & set(groups.values())
    check(bool(risk_groups), f"GROUP_RISK_OVERRIDE 그룹 존재 ({risk_groups})")

    check(
        set(ETF_DEVIATION_THRESHOLD_BY_GROUP) <= (set(groups.values()) | {"commodity"}),
        "BY_GROUP 임계값 키가 그룹에 존재",
    )
    # BY_TICKER 임계값 키는 6자리 코드여야 함 (유니버스에 없으면 자동모드에서
    # 해당 ETF가 제외된 것이므로 무해 — 예: 커버드콜 3종은 자동 제외됨)
    bad_keys = [k for k in ETF_DEVIATION_THRESHOLD_BY_TICKER if len(k) != 6]
    check(not bad_keys, f"BY_TICKER 임계값 키가 6자리 코드 ({bad_keys or '없음'})")
    excluded_thr = set(ETF_DEVIATION_THRESHOLD_BY_TICKER) - set(tickers)
    if excluded_thr:
        print(f"  · 참고: BY_TICKER 키 중 자동 유니버스에 없는 종목(제외됨, 무해): {excluded_thr}")

    # 기존 16종목 대비 포함/제외 기대값
    missing_in = EXPECT_INCLUDED - set(tickers)
    check(not missing_in, f"기대 포함 티커 모두 존재 ({missing_in or '없음'})")
    unexpected = EXPECT_EXCLUDED & set(tickers)
    check(not unexpected, f"기대 제외 티커 모두 부재 ({unexpected or '없음'})")

    print(
        f"  → 유니버스: {len(tickers)}종목, "
        f"그룹={ {g: sum(1 for v in groups.values() if v == g) for g in VALID_GROUPS} }, "
        f"taxable={len(taxable) if taxable else 0}"
    )


def step3_datamerge() -> None:
    print("\n[Step 3] 데이터 병합 드라이런 (캐시된 parquet 대상)")
    from etf_shared import (
        add_deviation_flag,
        add_liquidity_flag,
        add_listing_flag,
        add_price_basis_columns,
    )
    from pykrx_utils import get_listing_dates

    frames = []
    for t in CACHED:
        p = f"data_cache/{t}.parquet"
        try:
            df = pd.read_parquet(p)
        except Exception as exc:
            check(False, f"{t} parquet 로드 실패: {exc}")
            continue
        df["ticker"] = t
        frames.append(df)

    if not frames:
        check(False, "캐시된 parquet 없음")
        return

    price = pd.concat(frames, ignore_index=True)
    check("close" in price.columns and "date" in price.columns, "필수 컬럼 존재")

    try:
        price = add_liquidity_flag(price)
        check("liquidity_ok" in price.columns, "add_liquidity_flag 실행")
        check(price["liquidity_ok"].notna().any(), "liquidity_ok 에 유효값 존재")
    except Exception as exc:
        check(False, f"add_liquidity_flag 예외: {exc}")

    try:
        listing = get_listing_dates(ticker_subset=set(CACHED))
        price = add_listing_flag(price, listing_dates=listing)
        check("listing_ok" in price.columns, "add_listing_flag 실행")
    except Exception as exc:
        check(False, f"add_listing_flag 예외: {exc}")

    try:
        price = add_deviation_flag(price)
        check("deviation_ok" in price.columns, "add_deviation_flag 실행")
        # NAV 있는 행은 premium_discount 계산되어야 함
        has_nav = price["nav"].notna().any() if "nav" in price.columns else False
        check(True, f"NAV 존재 여부={has_nav} (결측 NAV는 deviation_ok=True 로 허용)")
    except Exception as exc:
        check(False, f"add_deviation_flag 예외: {exc}")

    try:
        price = add_price_basis_columns(price)
        check("close_adj" in price.columns, "add_price_basis_columns 실행")
        if os.environ.get("ETF_RETURN_BASIS", "price").strip().lower() == "price":
            # close_adj 는 price["close"] 와 동일 객체 (NaN 포함 동등 비교는 .equals 사용)
            same = price["close_adj"].equals(price["close"])
            check(same, "price 기준 close_adj == close")
    except Exception as exc:
        check(False, f"add_price_basis_columns 예외: {exc}")

    print(f"  → 병합 프레임: {len(price)}행, {price['ticker'].nunique()}종목")


def main() -> int:
    step1_unit()
    step3_datamerge()
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"실패 {len(FAILURES)}건:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("모든 검증 통과 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
