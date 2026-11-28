"""P2-A-2b ledger/coverage/연속성 fixture(네트워크 없음)."""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

import p2a1_data_evidence as p2a1
import pandas as pd

from etf_corporate_actions import CorporateAction, EventType

sys.path.insert(0, str(Path(__file__).resolve().parent))

import p2a2b_corporate_actions as ca


def _events() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticker": "069500",
                "isin": "KR7069500007",
                "name": "KODEX 200",
                "in_backtest_window": True,
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
                "isin": "KR7498400001",
                "name": "KODEX 200타겟위클리커버드콜",
                "in_backtest_window": True,
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
        ["069500", "360750"], metadata, "ok:2:abc", {"069500": 19}, continuity
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


def check_validate_events() -> None:
    universe = ["069500", "498400"]
    good = _events()
    assert ca.validate_events(good, universe) == []

    missing = good.drop(columns=["amount_per_share"])
    assert any("missing_columns" in issue for issue in ca.validate_events(missing, universe))

    bad_ticker = good.copy()
    bad_ticker.loc[0, "ticker"] = "69500"
    assert any("bad_ticker" in issue for issue in ca.validate_events(bad_ticker, universe))

    outside = good.copy()
    outside.loc[0, "ticker"] = "999999"
    assert any(
        "ticker_outside_universe" in issue for issue in ca.validate_events(outside, universe)
    )

    bad_date = good.copy()
    bad_date.loc[0, "record_date"] = "not-a-date"
    assert any("bad_date" in issue for issue in ca.validate_events(bad_date, universe))

    bad_amount = good.copy()
    bad_amount.loc[0, "amount_per_share"] = 0
    assert "bad_amount" in ca.validate_events(bad_amount, universe)
    inf_amount = good.copy()
    inf_amount.loc[0, "amount_per_share"] = float("inf")
    assert "bad_amount" in ca.validate_events(inf_amount, universe)

    duplicate = pd.concat([good, good.iloc[[0]]], ignore_index=True)
    assert any(
        "duplicate_ticker_record" in issue
        for issue in ca.validate_events(duplicate, universe)
    )

    # 날짜 범위와 플래그가 어긋나면 차단한다.
    mismatch = good.copy()
    mismatch.loc[0, "record_date"] = "2015-12-31"
    mismatch.loc[0, "ex_date"] = "2015-12-30"
    mismatch.loc[0, "payment_date"] = "2016-01-05"
    assert any(
        "in_backtest_window_mismatch" in issue
        for issue in ca.validate_events(mismatch, universe)
    )

    same_day = good.copy()
    same_day.loc[0, "ex_date"] = same_day.loc[0, "record_date"]
    assert any(
        "ex_not_before_record" in issue for issue in ca.validate_events(same_day, universe)
    )

    missing_ex = good.copy()
    missing_ex.loc[0, "ex_date"] = ""
    assert any(
        "missing_ex_date_in_window" in issue
        for issue in ca.validate_events(missing_ex, universe)
    )

    non_bool = good.copy()
    non_bool["in_backtest_window"] = "maybe"
    assert any(
        "non_boolean_in_backtest_window" in issue
        for issue in ca.validate_events(non_bool, universe)
    )


def check_load_events_sha_gate() -> None:
    import hashlib
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        events = _events()
        events_path = base / "distribution_events.csv"
        events.to_csv(events_path, index=False)
        digest = hashlib.sha256(events_path.read_bytes()).hexdigest()
        (base / "provenance.json").write_text(
            json.dumps({"artifacts": {"distribution_events.csv": {"sha256": digest}}}),
            encoding="utf-8",
        )
        loaded = ca.load_events(base, ["069500", "498400"])
        assert len(loaded) == 2

        (base / "provenance.json").write_text(
            json.dumps({"artifacts": {"distribution_events.csv": {"sha256": "0" * 64}}}),
            encoding="utf-8",
        )
        try:
            ca.load_events(base, ["069500", "498400"])
        except ca.InputValidationError:
            pass
        else:
            raise AssertionError("SHA 불일치가 차단되지 않았습니다")

        (base / "provenance.json").unlink()
        try:
            ca.load_events(base, ["069500", "498400"])
        except ca.InputValidationError:
            pass
        else:
            raise AssertionError("provenance 누락이 차단되지 않았습니다")


def check_metadata_dependent_coverage() -> None:
    rows = ca.coverage_rows(
        ["069500"],
        {},
        "ok:0:" + "a" * 64,
        {},
        {"069500": {"split_like_move": False}},
    )
    by_type = {row["event_type"]: row for row in rows}
    assert by_type["DELISTING"]["status"] == "UNVERIFIED"

    rows = ca.coverage_rows(
        ["069500"], {}, "login_failed", {}, {"069500": {"split_like_move": False}}
    )
    by_type = {row["event_type"]: row for row in rows}
    assert by_type["DELISTING"]["status"] == "UNAVAILABLE"


def check_preflight_gate() -> None:
    gated = p2a1.gate_preflight_report({"status": "BLOCKED"}, written=False)
    assert gated["status"] == "no_report" and gated["approval_valid"] is None
    gated = p2a1.gate_preflight_report(
        {"status": "BLOCKED", "approval_valid": False, "blockers": []}, written=True
    )
    assert gated["status"] == "BLOCKED"


def check_preflight_report_file() -> None:
    import json
    import os
    import tempfile
    from datetime import datetime, timezone

    started = "2026-11-27T00:00:00+00:00"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "approval_report.json"
        report, written, reason = ca.gate_report_file(path, started)
        assert (written, reason) == (False, "missing")

        path.write_text('{"status": "BLOCKED"}', encoding="utf-8")
        old = datetime(2026, 11, 26, tzinfo=timezone.utc).timestamp()
        os.utime(path, (old, old))
        report, written, reason = ca.gate_report_file(path, started)
        assert (written, reason) == (False, "stale")

        path.write_text("{not-json", encoding="utf-8")
        now = datetime(2026, 11, 28, tzinfo=timezone.utc).timestamp()
        os.utime(path, (now, now))
        report, written, reason = ca.gate_report_file(path, started)
        assert (written, reason) == (False, "invalid_json")

        path.write_text(json.dumps({"status": "BLOCKED"}), encoding="utf-8")
        os.utime(path, (now, now))
        report, written, reason = ca.gate_report_file(path, started)
        assert (written, reason) == (False, "invalid_shape")

        path.write_text(
            json.dumps({"status": "BLOCKED", "approval_valid": True, "blockers": []}),
            encoding="utf-8",
        )
        os.utime(path, (now, now))
        report, written, reason = ca.gate_report_file(path, started)
        assert (written, reason) == (False, "inconsistent_status")

        path.write_text(
            json.dumps({"status": "BLOCKED", "approval_valid": False, "blockers": []}),
            encoding="utf-8",
        )
        os.utime(path, (now, now))
        report, written, reason = ca.gate_report_file(path, started)
        assert (written, reason) == (True, "ok")
        assert report["status"] == "BLOCKED"


def check_duplicate_payment() -> None:
    from types import SimpleNamespace

    action = CorporateAction(
        event_id="P2A2B-069500-2026-07-31",
        ticker="069500",
        event_type=EventType.CASH_DISTRIBUTION,
        event_date=date(2026, 7, 30),
        record_date=date(2026, 7, 31),
        ex_date=date(2026, 7, 30),
        payment_date=date(2026, 8, 4),
        cash_amount=183,
        currency="KRW",
        source_document_id="kodex_api_069500",
        source_url="https://example.test",
        source_sha256="a" * 64,
    )
    result = ca.real_e2e(SimpleNamespace(events=(action,)))
    checks = result["069500"]["checks"]
    assert checks["duplicate_payment_zero"] is True
    assert checks["paid_on_payment_date_matches"] is True
    assert result["069500"]["scope"] == "ledger_loader_receivable_unit"


def check_split_candidate_not_event() -> None:
    # 가격 단절은 후보일 뿐 ledger 이벤트가 아니며 coverage도 VERIFIED로 올리지 않는다.
    metadata = {"069500": {"name": "KODEX 200", "listed_date": "2002-10-14"}}
    continuity = {"069500": {"split_like_move": True}}
    rows = ca.coverage_rows(["069500"], metadata, "ok:1:abc", {}, continuity)
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
        ("validate events", check_validate_events),
        ("load events sha gate", check_load_events_sha_gate),
        ("metadata coverage", check_metadata_dependent_coverage),
        ("preflight gate", check_preflight_gate),
        ("preflight report file", check_preflight_report_file),
        ("duplicate payment", check_duplicate_payment),
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
