"""ETF 유니버스 자동 구축 모듈.

KRX ETF 분류 데이터(`ETF_전종목기본종목` 캐시)를 기반으로
'실물(패시브) 주식형' ETF 유니버스를 자동 구축한다.

`etf_shared.py`에서 `ETF_UNIVERSE_MODE=auto` 시 사용된다.

필터 규칙 (기본):
  1. ETF_REPLICA_METHD_TP_CD == '실물(패시브)'   — 실물 + 패시브
  2. IDX_ASST_CLSS_NM == '주식'                  — 주식형
  3. IDX_CALC_INST_NM2 == '일반'                 — 1x (레버리지/인버스 제외)
  4. ETF_UNIVERSE_EXCLUDE_KEYWORDS 기준 이름 제외 — 기본: '커버드콜'
  5. ETF_UNIVERSE_INCLUDE_COMMODITY=1 시 원자재 ETF 추가

그룹 자동 분류:
  - IDX_MKT_CLSS_NM == '국내'      → domestic_equity
  - IDX_MKT_CLSS_NM == '해외'      → foreign_investment
  - IDX_MKT_CLSS_NM == '국내&해외' → foreign_investment
  - IDX_ASST_CLSS_NM == '원자재'   → commodity (include_commodity=True 시)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# ── 기본 필터 값 ────────────────────────────────────────────────
DEFAULT_REPLICA_METHOD = "실물(패시브)"
DEFAULT_ASSET_CLASS = "주식"
DEFAULT_CALC_INST = "일반"
COMMODITY_ASSET_CLASS = "원자재"

# ── 기본 제외 키워드 ─────────────────────────────────────────────
# 커버드콜 ETF는 패시브로 분류되지만 옵션 매도 전략으로 인해
# 모멘텀 회전 전략에 부적합 (상승 캡, 다른 수익 구조).
DEFAULT_EXCLUDE_KEYWORDS: tuple[str, ...] = ("커버드콜",)

# ── 그룹 매핑 규칙 ──────────────────────────────────────────────
GROUP_MAPPING: dict[str, str] = {
    "국내": "domestic_equity",
    "해외": "foreign_investment",
    "국내&해외": "foreign_investment",
}


@dataclass(frozen=True)
class UniverseConfig:
    """유니버스 구축 설정 (불변).

    freeze snapshot에 직렬화되어 방법론 동결(Tier 1)에 사용된다.
    """

    replica_method: str = DEFAULT_REPLICA_METHOD
    asset_class: str = DEFAULT_ASSET_CLASS
    calc_inst: str = DEFAULT_CALC_INST
    include_commodity: bool = False
    exclude_keywords: tuple[str, ...] = DEFAULT_EXCLUDE_KEYWORDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "replica_method": self.replica_method,
            "asset_class": self.asset_class,
            "calc_inst": self.calc_inst,
            "include_commodity": self.include_commodity,
            "exclude_keywords": list(self.exclude_keywords),
        }


@dataclass(frozen=True)
class UniverseResult:
    """유니버스 구축 결과 (불변).

    universe snapshot(Tier 2)에 직렬화되어 드리프트 감지에 사용된다.
    """

    tickers: list[str]
    ticker_groups: dict[str, str]
    config: UniverseConfig
    build_date: str
    universe_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tickers": self.tickers,
            "ticker_groups": self.ticker_groups,
            "config": self.config.to_dict(),
            "build_date": self.build_date,
            "universe_sha256": self.universe_sha256,
        }


def _compute_sha256(tickers: list[str], groups: dict[str, str]) -> str:
    """유니버스의 정규화된 SHA-256 해시를 계산한다."""
    payload = json.dumps(
        {"tickers": tickers, "groups": groups},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_universe(
    classification_df: pd.DataFrame,
    config: UniverseConfig | None = None,
    build_date: str | None = None,
) -> UniverseResult:
    """KRX ETF 분류 데이터로부터 유니버스를 구축한다.

    순수 함수: 동일한 입력에 대해 항상 동일한 결과를 반환한다.
    부작용 없음 (로깅만 수행).

    Parameters
    ----------
    classification_df
        `ETF_전종목기본종목().fetch()` 결과 (또는 캐시된 parquet).
        필수 컬럼: ISU_SRT_CD, ISU_ABBRV, ETF_REPLICA_METHD_TP_CD,
        IDX_ASST_CLSS_NM, IDX_CALC_INST_NM2, IDX_MKT_CLSS_NM.
    config
        유니버스 구축 설정. None이면 기본값 사용.
    build_date
        유니버스 구축 일자 (ISO 형식). None이면 오늘.

    Returns
    -------
    UniverseResult
        구축된 유니버스 (티커 목록, 그룹 매핑, 설정, 해시).
    """
    if config is None:
        config = UniverseConfig()

    if build_date is None:
        build_date = date.today().isoformat()

    df = classification_df

    # 1. 기본 필터: 실물(패시브) + 주식 + 일반(1x)
    base_mask = (
        (df["ETF_REPLICA_METHD_TP_CD"] == config.replica_method)
        & (df["IDX_ASST_CLSS_NM"] == config.asset_class)
        & (df["IDX_CALC_INST_NM2"] == config.calc_inst)
    )
    filtered = df[base_mask].copy()

    # 2. 원자재 ETF 추가 (옵션)
    if config.include_commodity:
        commodity_mask = (
            (df["ETF_REPLICA_METHD_TP_CD"] == config.replica_method)
            & (df["IDX_ASST_CLSS_NM"] == COMMODITY_ASSET_CLASS)
            & (df["IDX_CALC_INST_NM2"] == config.calc_inst)
        )
        commodity_df = df[commodity_mask].copy()
        if not commodity_df.empty:
            filtered = pd.concat([filtered, commodity_df], ignore_index=True)
            logger.info("원자재 ETF %d개 추가", len(commodity_df))

    # 3. 키워드 제외
    if config.exclude_keywords:
        pattern = "|".join(config.exclude_keywords)
        before = len(filtered)
        filtered = filtered[~filtered["ISU_ABBRV"].str.contains(pattern, na=False)]
        excluded_count = before - len(filtered)
        if excluded_count:
            logger.info("키워드 제외(%s): %d개 ETF 제외", pattern, excluded_count)

    # 4. 중복 제거 (원자재 추가 시 중복 가능)
    filtered = filtered.drop_duplicates(subset=["ISU_SRT_CD"])

    # 5. 그룹 매핑
    ticker_groups: dict[str, str] = {}
    mixed_tickers: list[str] = []

    for _, row in filtered.iterrows():
        ticker = str(row["ISU_SRT_CD"]).strip()
        market = str(row.get("IDX_MKT_CLSS_NM", "")).strip()
        asset_cls = str(row.get("IDX_ASST_CLSS_NM", "")).strip()

        if asset_cls == COMMODITY_ASSET_CLASS:
            group = "commodity"
        elif market in GROUP_MAPPING:
            group = GROUP_MAPPING[market]
            if market == "국내&해외":
                mixed_tickers.append(ticker)
        else:
            # 알 수 없는 분류 → domestic_equity (안전 fallback)
            group = "domestic_equity"
            logger.warning("알 수 없는 시장 분류(%s): %s → domestic_equity", market, ticker)

        ticker_groups[ticker] = group

    tickers = sorted(ticker_groups.keys())
    sha256 = _compute_sha256(tickers, ticker_groups)

    # 그룹별 통계
    group_counts: dict[str, int] = {}
    for g in ticker_groups.values():
        group_counts[g] = group_counts.get(g, 0) + 1

    logger.info(
        "유니버스 구축 완료: %d개 ETF (%s)",
        len(tickers),
        ", ".join(f"{g}={c}" for g, c in sorted(group_counts.items())),
    )
    if mixed_tickers:
        logger.info("국내&해외 혼합 분류 → foreign_investment: %s", mixed_tickers)

    return UniverseResult(
        tickers=tickers,
        ticker_groups=ticker_groups,
        config=config,
        build_date=build_date,
        universe_sha256=sha256,
    )


def config_from_env() -> UniverseConfig:
    """환경변수에서 UniverseConfig를 생성한다.

    환경변수:
    - ETF_UNIVERSE_EXCLUDE_KEYWORDS: 쉼표로 구분된 제외 키워드 (기본: '커버드콜')
    - ETF_UNIVERSE_INCLUDE_COMMODITY: '1'이면 원자재 ETF 포함 (기본: '0')
    """
    exclude_kw_str = os.environ.get("ETF_UNIVERSE_EXCLUDE_KEYWORDS", "커버드콜")
    exclude_keywords = tuple(k.strip() for k in exclude_kw_str.split(",") if k.strip())

    return UniverseConfig(
        include_commodity=os.environ.get("ETF_UNIVERSE_INCLUDE_COMMODITY", "0") == "1",
        exclude_keywords=exclude_keywords,
    )