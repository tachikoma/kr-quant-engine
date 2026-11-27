"""P2-A-2b ledger/coverage/연속성 fixture(네트워크 없음)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import p2a2b_corporate_actions as ca


def _events() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticker": "069500",
                "record_date": "2026-04-30",
                "ex_date": "2026-04-29",
                "payment_date": "2026-05-06",
                "amount_per_share": 446.0,
                "source_url": "https://example.test/api",
                "source_sha256": "a" * 64,
                "retrieved_at": "2026-11-27T00:00:00+00:00",
            },
            {
                "ticker": "498400",
                "record_date": "2026-09-15",
                "ex_date": "2026-09-14",
                "payment_date": "2026-09-17",
                "amount_per_share": 300.0,
                "source_url": "https://example.test/api2",
                "source_sha256": "b" * 64,
                "retrieved_at": "2026-11-27T00:00:00+00:00",
            },
        ]
    )


def check_ledger_rows() -> None:
    rows = ca.ledger_rows(_events())
    assert len(rows) == 2
    assert rows[0]["ticker"] == "069500"
    assert rows[0]["event_type"] == "CASH_DISTRIBUTION"
    assert rows[0]["event_date"] == "2026-04-29"
    assert rows[0]["cash_amount"] == "446"
    assert "파생" in rows[0]["notes"]
    assert len({row["event_id"] for row in rows}) == 2


def check_coverage_rows() -> None:
    metadata = {
        "069500": {"name": "KODEX 200", "listed_date": "2002-10-14"},
        "360750": {"name": "TIGER 미국S&P500", "listed_date": "2020-08-07"},
    }
    continuity = {
        "069500": {"split_like_move": False},
        "360750": {"split_like_move": False},
    }
    rows = ca.coverage_rows(
        ["069500", "360750"], metadata, {"069500": 19}, continuity
    )
    by_key = {(row["ticker"], row["event_type"]): row for row in rows}
    assert by_key[("069500", "CASH_DISTRIBUTION")]["status"] == "PARTIAL"
    assert by_key[("360750", "CASH_DISTRIBUTION")]["status"] == "UNAVAILABLE"
    assert by_key[("069500", "SPLIT")]["status"] == "UNVERIFIED"
    assert by_key[("069500", "SUSPENSION_START")]["status"] == "UNAVAILABLE"
    assert by_key[("069500", "DELISTING")]["status"] == "PARTIAL"
    assert by_key[("069500", "SPLIT")]["event_count"] == 0
    # 확보 이벤트가 없다고 이벤트 없음으로 승격하지 않는다.
    assert by_key[("360750", "CASH_DISTRIBUTION")]["reason"].startswith("공식 원천 접근 실패")


def check_split_candidate_not_event() -> None:
    # 가격 단절은 후보일 뿐 ledger 이벤트가 아니며 coverage도 VERIFIED로 올리지 않는다.
    metadata = {"069500": {"name": "KODEX 200", "listed_date": "2002-10-14"}}
    continuity = {"069500": {"split_like_move": True}}
    rows = ca.coverage_rows(["069500"], metadata, {}, continuity)
    by_type = {row["event_type"]: row for row in rows}
    assert by_type["SPLIT"]["status"] == "UNVERIFIED"
    assert by_type["SPLIT"]["event_count"] == 0
    assert "공식 공시 미확인" in by_type["SPLIT"]["reason"]
    # 이벤트 입력이 없으면 ledger 행도 만들어지지 않는다.
    assert ca.ledger_rows(pd.DataFrame(columns=_events().columns)) == []


def check_price_continuity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        normal = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"]),
                "close": [100.0, 101.0, 102.0],
            }
        )
        split = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"]),
                "close": [100.0, 50.0, 51.0],
            }
        )
        normal.to_parquet(cache / "069500.parquet", index=False)
        split.to_parquet(cache / "091160.parquet", index=False)
        rows = {row["ticker"]: row for row in ca.price_continuity(cache, ["069500", "091160", "999999"])}
    assert rows["069500"]["split_like_move"] is False
    assert rows["091160"]["split_like_move"] is True
    assert rows["999999"]["status"] == "cache_missing"


def check_name_observations_not_mappable() -> None:
    assert ca.NAME_OBSERVATIONS
    for item in ca.NAME_OBSERVATIONS:
        assert item["event_type"] == "NAME_CHANGE"
        assert item["ledger_mappable"] is False
        assert item["status"] == "UNVERIFIED"
        assert item["receipt_no"]


def check_source_survey() -> None:
    ids = {item["source_id"] for item in ca.SOURCE_SURVEY}
    assert {"kind", "dart", "krx_mdc", "seibro"} <= ids
    for item in ca.SOURCE_SURVEY:
        assert item["result"]
        assert item["usable"]


def main() -> None:
    checks = [
        ("ledger rows", check_ledger_rows),
        ("coverage rows", check_coverage_rows),
        ("split candidate not event", check_split_candidate_not_event),
        ("price continuity", check_price_continuity),
        ("name observations", check_name_observations_not_mappable),
        ("source survey", check_source_survey),
    ]
    for name, check in checks:
        try:
            check()
        except AssertionError as exc:
            print(f"FAIL {name}: {exc}")
            raise
        print(f"PASS {name}")
    print("\np2a2b corporate-action checks passed")


if __name__ == "__main__":
    main()
