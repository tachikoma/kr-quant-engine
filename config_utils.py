"""작업 환경 설정 유틸리티

환경변수로 슬리피지/스프레드 등을 읽어올 때 다양한 단위(예: '5bp', '0.5%', '0.0005')를 허용하고
검증 및 기본값 처리를 담당합니다.
"""
from __future__ import annotations

import os
from typing import Any


def parse_pct_env(name: str, default: float) -> float:
    """환경변수에서 비율(퍼센트/베이시스포인트/소수) 값을 안정적으로 파싱해서 소수 단위로 반환합니다.

    허용 포맷:
    - "5bp" -> 0.0005
    - "0.5%" -> 0.005
    - "0.0005" -> 0.0005

    안전 정책: 파싱 실패 또는 비정상적으로 큰 값(>10%)인 경우 기본값을 반환하고 경고를 출력합니다.
    """
    raw = os.environ.get(name)
    if raw is None:
        return float(default)

    s = str(raw).strip()
    if not s:
        return float(default)

    try:
        low = s.lower()
        if low.endswith("bp"):
            num = float(low[:-2].strip())
            return num / 10000.0
        if low.endswith("%"):
            num = float(low[:-1].strip())
            return num / 100.0

        # plain number
        v = float(s)
        # 방어: 값이 1(100%)보다 크면 단위 착오 가능성으로 경고 후 기본값 사용
        if abs(v) > 0.1:
            print(f"⚠️ 환경변수 {name} 값이 비정상적으로 큽니다: '{raw}'. 단위를 확인하세요. 기본값({default}) 사용")
            return float(default)
        return v
    except Exception:
        print(f"⚠️ 환경변수 {name} 파싱 실패: '{raw}' — 기본값({default}) 사용")
        return float(default)


def parse_fraction_env(name: str, default: float) -> float:
    """환경변수에서 0~1 사이 분수 값을 파싱한다. MAX_ASSET_PCT 등에 사용.

    parse_pct_env와 달리 >0.1 방어가 없으므로 0.20(20%) 같은 값을 정상 수용한다.
    """
    raw = os.environ.get(name)
    if raw is None:
        return float(default)
    s = str(raw).strip()
    if not s:
        return float(default)
    try:
        v = float(s)
        if v < 0 or v > 1:
            print(f"⚠️ 환경변수 {name}={raw}: 0~1 범위 초과, 기본값({default}) 사용")
            return float(default)
        return v
    except Exception:
        print(f"⚠️ 환경변수 {name} 파싱 실패: '{raw}' — 기본값({default}) 사용")
        return float(default)
