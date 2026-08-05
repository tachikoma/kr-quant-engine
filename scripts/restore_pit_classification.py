#!/usr/bin/env python3
"""사라진(상장폐지) ETF 227종목의 과거 분류를 복원한다.

현재 KRX 분류 캐시(`data_cache/etf_tax_classification.parquet`)에는 아직
상장 중인 종목만 존재하므로, PIT 스냅샷의 `index_name`(기초지수명)과 `name`
(종목명)으로부터 자산군/시장/레버리지/복제방법을 규칙 기반으로 추정한다.

- 추정값은 `data_cache/pit_universe/pit_classification_restored.parquet`에 저장
- 낮은 신뢰도 행은 `outputs_universe_bias/pit_classification_review.csv`로 출력해
  수동 검토 대상으로 분리한다.
- 복제방법(실물/합성)은 지수명만으로는 단정할 수 없어 기본값(실물(패시브))과
  신뢰도만 부여하며, 실제 백테스트 연결 전에 검토 CSV를 사람이 확정해야 한다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PANEL_PATH = ROOT / "data_cache" / "pit_universe" / "pit_universe_snapshots.parquet"
OUT_CACHE = ROOT / "data_cache" / "pit_universe" / "pit_classification_restored.parquet"
OUT_REVIEW = ROOT / "outputs_universe_bias" / "pit_classification_review.csv"
OUT_TABLE = ROOT / "outputs_universe_bias" / "pit_classification_restored.csv"

BOND_KEYWORDS = [
    "채권",
    "국채",
    "국고채",
    "회사채",
    "금융채",
    "크레딧",
    "단기자금",
    "단기유동성",
    "특수채",
    "특수은행채",
    "은행채",
    "money market",
    "bond",
    "credit",
    "ktb",
    "통안채",
    "국공채",
    "mmf",
]
COMMODITY_KEYWORDS = [
    "gsci",
    "wci gold",
    "gold futures",
    "gold excess",
    "oil futures",
    "wti",
    "crude",
    "copper",
    "commod",
    "metals",
    "silver",
    "natural gas futures",
    "energy futures",
    "원자재",
    "금선물",
]
REALESTATE_KEYWORDS = ["리츠", "부동산", "reit", "realty"]
CURRENCY_KEYWORDS = ["통화", "환율", "fx", "화폐", "달러선물"]
OTHER_KEYWORDS = [
    "cofr",
    "sofr",
    "cd금리",
    "금리투자",
    "tdf",
    "kofr",
    "탄소",
    "carbon",
    "자산배분",
    "multi-asset",
    "preferred securities",
]
MIXED_KEYWORDS = ["혼합"]
LEVERAGE_KEYWORDS = ["레버리지", "2x", "3x", "2배", "3배"]
INVERSE_KEYWORDS = ["인버스", "1x", "inverse"]

# "원유생산기업/금채굴기업/천연가스밸류체인" 등은 주식(운용사주)이므로 원자재에서 제외
EQUITY_COMPANY_KEYWORDS = ["원유생산", "금채굴", "gold miners", "밸류체인", "value chain", "생산기업"]

FOREIGN_INDEX_KEYWORDS = [
    "s&p",
    "msci",
    "nasdaq",
    "dow jones",
    "russell",
    "iboxx",
    "sector select",
    "world",
    "global",
    "eafc",
    "acwi",
    "ftse",
    "dax",
    "topix",
    "nikkei",
    "hang seng",
    "taiex",
    "stoxx",
    "solactive",
    "bloomberg",
    "vn30",
    "spac",
    "indxx",
    "germany",
    "china",
    "csi",
    "star 50",
    "chinext",
    "szse",
    "emerging markets",
    "latin america",
    "singapore",
    "barbell",
    "select sector",
    "tsmc",
    "google",
    "tesla",
    "nvidia",
    "factset",
    "akros",
    "ice silver",
    "us ",
    "us-",
    "us)",
    "메타버스",
    "애그테크",
    "엔비디아",
    "테슬라",
    "팔란티어",
    "국제금",
    "차이나",
    "tsmc",
]
FOREIGN_NAME_KEYWORDS = [
    "미국",
    "s&p",
    "nasdaq",
    "글로벌",
    "유럽",
    "중국",
    "일본",
    "베트남",
    "인도",
    "차이나",
    "엔비디아",
    "테슬라",
    "tsmc",
    "구글",
    "국제금",
    "라틴",
    "국제금커버드콜",
]

# 복제방법 신뢰도가 낮은 지수 제공자: 국내 채권/파생은 합성일 가능성
SYNTHETIC_LIKELY_INDEX = ["ktb", "국채", "gcsci", "gsci", "wci", "s&p"]
SYNTHETIC_LIKELY_NAME = ["합성", "파생", "선물"]


def _has_any(value: str, keywords: list[str]) -> bool:
    lowered = value.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def classify_row(name: str, index_name: str) -> dict:
    """단일 종목의 과거 분류를 추정한다. 신뢰도는 0.0~1.0."""
    name = name or ""
    index_name = index_name or ""
    idx_u = index_name.upper()
    name_u = name.upper()

    # 1. 자산군 (운용사주 주의: 원유생산기업/금채굴기업은 주식)
    if _has_any(index_name, EQUITY_COMPANY_KEYWORDS) or _has_any(name, EQUITY_COMPANY_KEYWORDS):
        asset_class, asset_conf = "주식", 0.8
    elif _has_any(index_name, MIXED_KEYWORDS) or _has_any(name, MIXED_KEYWORDS):
        asset_class, asset_conf = "혼합자산", 0.8
    elif _has_any(index_name, BOND_KEYWORDS) or _has_any(name, BOND_KEYWORDS):
        asset_class, asset_conf = "채권", 0.9
    elif _has_any(index_name, COMMODITY_KEYWORDS) or _has_any(name, COMMODITY_KEYWORDS):
        asset_class, asset_conf = "원자재", 0.9
    elif _has_any(index_name, REALESTATE_KEYWORDS) or _has_any(name, REALESTATE_KEYWORDS):
        asset_class, asset_conf = "부동산", 0.9
    elif _has_any(index_name, CURRENCY_KEYWORDS) or _has_any(name, CURRENCY_KEYWORDS):
        asset_class, asset_conf = "통화", 0.85
    elif _has_any(index_name, OTHER_KEYWORDS) or _has_any(name, OTHER_KEYWORDS):
        asset_class, asset_conf = "기타", 0.8
    else:
        asset_class, asset_conf = "주식", 0.7

    # 2. 시장 (국내/해외/국내&해외)
    #    Korea 한정 지수(MSCI Korea, KRX-Akros, K-테마)는 국내 우선 판정
    korea_marker = any(
        k in idx_u.lower() for k in ["korea", "krx-akros", "k-메타버스", "k-미국"]
    ) or any(k in name_u for k in ["코리아", "k-", "k-미국"])
    if korea_marker:
        market, market_conf = "국내", 0.75
    elif _has_any(index_name, FOREIGN_INDEX_KEYWORDS) or _has_any(name, FOREIGN_NAME_KEYWORDS):
        market, market_conf = "해외", 0.85
    elif any(k in idx_u for k in ["국내&해외", "국내및해외"]):
        market, market_conf = "국내&해외", 0.8
    else:
        market, market_conf = "국내", 0.7

    # 3. 레버리지/인버스
    if _has_any(name, LEVERAGE_KEYWORDS) or _has_any(index_name, LEVERAGE_KEYWORDS):
        calc_inst, calc_conf = "2X 레버리지", 0.95
    elif _has_any(name, INVERSE_KEYWORDS) or _has_any(index_name, INVERSE_KEYWORDS):
        calc_inst, calc_conf = "1X 인버스", 0.95
    else:
        calc_inst, calc_conf = "일반", 0.9

    # 4. 복제방법 (추정만 가능 — 지수명으로 단정 불가)
    if "합성" in name:
        # 종목명에 "(합성)" 명시 → 확정
        replica, replica_conf = "합성(패시브)", 0.95
    elif _has_any(name, ["액티브"]):
        replica, replica_conf = "실물(액티브)", 0.75
    elif _has_any(index_name, SYNTHETIC_LIKELY_INDEX) or _has_any(name, SYNTHETIC_LIKELY_NAME):
        replica, replica_conf = "합성(패시브)", 0.55
    elif asset_class in {"채권", "원자재", "통화"}:
        # 채권/원자재/통화는 합성(패시브)일 가능성이 있어 검토 대상
        replica, replica_conf = "합성(패시브)", 0.55
    elif market == "해외":
        # 해외 지수는 합성(패시브)인 경우가 많아 검토 대상
        replica, replica_conf = "합성(패시브)", 0.55
    else:
        # 국내 주식 일반 지수는 대부분 실물(패시브)
        replica, replica_conf = "실물(패시브)", 0.85

    return {
        "asset_class": asset_class,
        "asset_confidence": asset_conf,
        "market": market,
        "market_confidence": market_conf,
        "calc_inst": calc_inst,
        "calc_confidence": calc_conf,
        "replica": replica,
        "replica_confidence": replica_conf,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel-path", type=Path, default=PANEL_PATH)
    parser.add_argument("--review-threshold", type=float, default=0.6)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.panel_path.exists():
        raise FileNotFoundError(f"PIT 패널이 없습니다: {args.panel_path}")
    panel = pd.read_parquet(args.panel_path)
    no_cls = panel[~panel["has_current_classification"]]
    rows = (
        no_cls[["ticker", "isin", "name", "index_name"]]
        .drop_duplicates("ticker")
        .sort_values("ticker")
        .reset_index(drop=True)
    )

    restored = []
    for _, row in rows.iterrows():
        result = classify_row(str(row["name"]), str(row["index_name"]))
        restored.append(
            {
                "ticker": row["ticker"],
                "isin": row["isin"],
                "name": row["name"],
                "index_name": row["index_name"],
                **result,
                "min_confidence": min(
                    result["asset_confidence"],
                    result["market_confidence"],
                    result["calc_confidence"],
                    result["replica_confidence"],
                ),
            }
        )
    out = pd.DataFrame(restored)

    OUT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    OUT_REVIEW.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT_CACHE, index=False)
    out.to_csv(OUT_TABLE, index=False, encoding="utf-8-sig")

    review = out[out["min_confidence"] < args.review_threshold].copy()
    review.to_csv(OUT_REVIEW, index=False, encoding="utf-8-sig")

    print(f"복원 종목 수: {len(out)}")
    print(out["asset_class"].value_counts().to_string())
    print()
    print("시장 분류:")
    print(out["market"].value_counts().to_string())
    print()
    print("레버리지:")
    print(out["calc_inst"].value_counts().to_string())
    print()
    print(f"검토 필요(신뢰도 < {args.review_threshold}): {len(review)}")
    print(f"검토 CSV: {OUT_REVIEW}")
    print(f"캐시: {OUT_CACHE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
