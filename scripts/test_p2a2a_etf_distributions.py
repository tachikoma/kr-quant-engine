"""P2-A-2a 분배금 정규화·coverage·회계 fixture."""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import p2a2a_etf_distributions as evidence


def _payload(rows: list[dict[str, str]], months: list[int] | None = None) -> dict:
    return {
        "dividList": rows,
        "annualTrend": {"money": []},
        "dividInfo": {
            "fNm": "KODEX 200",
            "dividPeriod": {
                "year": [{"yy": "2026", "mm": str(month)} for month in (months or [1, 4, 7, 10])]
            },
        },
    }


def _row(record: str = "20260430", pay: str = "20260506", amount: str = "446", tax: str = "446") -> dict:
    return {"basicD": record, "payD": pay, "dividA": amount, "taxDividA": tax, "dividY": "0.4423"}


def check_parse_date() -> None:
    assert evidence.parse_yyyymmdd("20260430") == date(2026, 4, 30)
    assert evidence.parse_yyyymmdd("2026-04-30") is None
    assert evidence.parse_yyyymmdd("20261399") is None
    assert evidence.parse_yyyymmdd(None) is None


def check_normalize_events() -> None:
    rows, errors = evidence.normalize_kodex_events(
        "069500",
        isin="KR7069500007",
        name="KODEX 200",
        payload=_payload([_row(), _row(record="20260731", pay="20260804", amount="183", tax="168")]),
        source_url="https://example.test/1",
        source_sha256="a" * 64,
        retrieved_at="2026-11-27T00:00:00+00:00",
    )
    assert not errors, errors
    assert len(rows) == 2
    assert rows[0]["amount_per_share"] == 446.0
    assert rows[0]["currency"] == "KRW"
    assert rows[0]["payment_date"] == "2026-05-06"

    _, errors = evidence.normalize_kodex_events(
        "069500",
        isin=None,
        name=None,
        payload=_payload([_row(amount="0"), _row(record="20260731", pay="20260701")]),
        source_url="u",
        source_sha256="b" * 64,
        retrieved_at="t",
    )
    assert any("invalid_amount" in error for error in errors)
    assert any("payment_before_record" in error for error in errors)

    rows, errors = evidence.normalize_kodex_events(
        "069500",
        isin=None,
        name=None,
        payload=_payload([_row(tax="999")]),
        source_url="u",
        source_sha256="b" * 64,
        retrieved_at="t",
    )
    assert any("invalid_taxable_base" in error for error in errors)
    assert len(rows) == 1 and np.isnan(rows[0]["taxable_base_per_share"])

    rows, errors = evidence.normalize_kodex_events(
        "069500",
        isin=None,
        name=None,
        payload=_payload([_row(pay="bad")]),
        source_url="u",
        source_sha256="b" * 64,
        retrieved_at="t",
    )
    assert any("invalid_payment_date" in error for error in errors)
    assert rows == []

    rows, errors = evidence.normalize_kodex_events(
        "069500",
        isin=None,
        name=None,
        payload=_payload([_row(tax="-1")]),
        source_url="u",
        source_sha256="b" * 64,
        retrieved_at="t",
    )
    assert any("invalid_taxable_base" in error for error in errors)
    assert np.isnan(rows[0]["taxable_base_per_share"])


def check_deduplicate() -> None:
    base = {
        "ticker": "069500",
        "record_date": "2026-04-30",
        "payment_date": "2026-05-06",
        "amount_per_share": 446.0,
    }
    rows, errors = evidence.deduplicate_events([dict(base), dict(base)], [])
    assert len(rows) == 1 and not errors

    conflict = dict(base)
    conflict["amount_per_share"] = 447.0
    rows, errors = evidence.deduplicate_events([dict(base), conflict], [])
    assert len(rows) == 1
    assert any("conflicting_duplicate" in error for error in errors)

    tax_conflict = dict(base, taxable_base_per_share=1.0)
    rows, errors = evidence.deduplicate_events(
        [dict(base, taxable_base_per_share=446.0), tax_conflict], []
    )
    assert len(rows) == 1
    assert any("conflicting_duplicate" in error for error in errors)


def check_ex_dates() -> None:
    calendar = [date(2026, 4, 29), date(2026, 4, 30), date(2026, 5, 4)]
    rows = [{"ticker": "069500", "record_date": "2026-04-30"}]
    rows, errors = evidence.attach_ex_dates(rows, calendar)
    assert rows[0]["ex_date"] == "2026-04-29", rows[0]
    assert rows[0]["ex_date_basis"] == "derived_previous_trading_day"
    assert not errors

    rows = [{"ticker": "069500", "record_date": "2026-04-28"}]
    rows, errors = evidence.attach_ex_dates(rows, calendar)
    assert any("record_date_not_trading_day" in error for error in errors)

    rows = [{"ticker": "069500", "record_date": "2026-04-29"}]
    rows, errors = evidence.attach_ex_dates(rows, calendar)
    assert rows[0]["ex_date"] == ""
    assert rows[0]["ex_date_basis"] == "unresolved_before_calendar"

    rows, errors = evidence.attach_ex_dates([{"ticker": "069500", "record_date": "2026-04-30"}], [])
    assert rows[0]["ex_date_basis"] == "unresolved_no_calendar"
    assert errors == ["calendar_empty"]


def check_expected_events() -> None:
    assert evidence.expected_events(date(2022, 1, 28), date(2026, 10, 30), {1, 4, 7, 10}) == 20
    assert evidence.expected_events(date(2022, 4, 29), date(2026, 4, 30), {4}) == 5
    assert evidence.expected_events(date(2025, 1, 15), date(2026, 11, 13), set(range(1, 13))) == 23
    assert evidence.expected_events(date(2025, 1, 15), date(2026, 1, 15), set(range(1, 13))) == 13


def check_coverage_status() -> None:
    assert (
        evidence.coverage_status(
            official=True, event_count=0, span_complete=None, covers_start=False,
            covers_end=False, conflicts=False,
        )
        == "UNAVAILABLE"
    )
    assert (
        evidence.coverage_status(
            official=False, event_count=3, span_complete=True, covers_start=True,
            covers_end=True, conflicts=False,
        )
        == "UNVERIFIED"
    )
    assert (
        evidence.coverage_status(
            official=True, event_count=3, span_complete=False, covers_start=True,
            covers_end=True, conflicts=False,
        )
        == "PARTIAL"
    )
    assert (
        evidence.coverage_status(
            official=True, event_count=3, span_complete=True, covers_start=True,
            covers_end=True, conflicts=False,
        )
        == "VERIFIED"
    )
    assert (
        evidence.coverage_status(
            official=True, event_count=3, span_complete=True, covers_start=False,
            covers_end=True, conflicts=False,
        )
        == "PARTIAL"
    )


def check_build_coverage() -> None:
    rows = [
        {"ticker": "069500", "record_date": f"{year}-01-30"} for year in range(2022, 2027)
    ]
    partial = evidence.build_coverage(
        ticker="069500",
        issuer="삼성자산운용(KODEX)",
        name="KODEX 200",
        listed_date=date(2002, 10, 14),
        rows=rows,
        months={1},
        conflicts=False,
        source_available=True,
    )
    assert partial["status"] == "PARTIAL", partial
    assert partial["total_return_approvable"] is False

    monthly_rows = [
        {"ticker": "498400", "record_date": f"{year}-{month:02d}-15"}
        for year in range(2016, 2027)
        for month in range(1, 13)
        if (year, month) <= (2026, 9)
    ]
    monthly_rows[0]["record_date"] = "2016-01-05"
    verified = evidence.build_coverage(
        ticker="498400",
        issuer="삼성자산운용(KODEX)",
        name="KODEX 200타겟위클리커버드콜",
        listed_date=date(2016, 1, 5),
        rows=monthly_rows,
        months=set(range(1, 13)),
        conflicts=False,
        source_available=True,
    )
    assert verified["status"] == "VERIFIED", verified

    unavailable = evidence.build_coverage(
        ticker="360750",
        issuer="미래에셋(TIGER)",
        name="TIGER 미국S&P500",
        listed_date=None,
        rows=[],
        months=set(),
        conflicts=False,
        source_available=False,
    )
    assert unavailable["status"] == "UNAVAILABLE"
    assert "미확보" in unavailable["reason"]


def check_missing_year_not_verified() -> None:
    rows = [
        {"ticker": "498400", "record_date": f"{year}-{month:02d}-15"}
        for year in range(2016, 2027)
        for month in range(1, 13)
        if (year, month) <= (2026, 9)
    ]
    rows[0]["record_date"] = "2016-01-05"
    without_2020 = [row for row in rows if not row["record_date"].startswith("2020")]
    result = evidence.build_coverage(
        ticker="498400",
        issuer="삼성자산운용(KODEX)",
        name="KODEX 200타겟위클리커버드콜",
        listed_date=date(2016, 1, 5),
        rows=without_2020,
        months=set(range(1, 13)),
        conflicts=False,
        source_available=True,
    )
    assert result["status"] == "PARTIAL", result
    assert result["recent_actual_events"] == result["recent_expected_events"]


def check_total_return_e2e() -> None:
    price = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-04-29", "2026-04-30", "2026-05-04", "2026-05-06"]),
            "ticker": ["069500"] * 4,
            "close": [100000.0, 100200.0, 99800.0, 101000.0],
        }
    )
    events = [
        {
            "ticker": "069500",
            "ex_date": "2026-05-04",
            "amount_per_share": 446.0,
            "payment_date": "2026-05-06",
            "source_url": "https://example.test",
        }
    ]
    with tempfile.TemporaryDirectory() as tmp:
        result = evidence.total_return_e2e(price, events, Path(tmp))
    assert result["event_count"] == 1
    assert result["ex_dates_all_in_price"] is True
    assert result["tr_identity_abs_diff"] < 1e-9
    assert result["equity_check"]["cash_dividends"] == 44600.0
    assert result["equity_check"]["identity_abs_diff"] < 1e-6
    # total return은 분배금을 재투자하므로 가격수익률보다 높아야 한다.
    assert result["total_return_pct"] > result["price_return_pct"]

    slice_price = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-05-01", "2026-05-04", "2026-05-06"]),
            "ticker": ["069500"] * 3,
            "close": [100.0, 95.0, 96.0],
        }
    )
    sliced = evidence.slice_from_prior_trading_day(slice_price, date(2026, 5, 4))
    assert len(sliced) == 3 and sliced["date"].iloc[0] == pd.Timestamp("2026-05-01")
    with tempfile.TemporaryDirectory() as tmp:
        first_event = evidence.total_return_e2e(
            slice_price,
            [
                {
                    "ticker": "069500",
                    "ex_date": "2026-05-04",
                    "amount_per_share": 5.0,
                    "payment_date": "2026-05-06",
                    "source_url": "https://example.test",
                }
            ],
            Path(tmp),
        )
    assert first_event["total_return_pct"] > first_event["price_return_pct"]


def check_entitlement() -> None:
    events = [
        {
            "ticker": "498400",
            "ex_date": "2026-09-16",
            "payment_date": "2026-09-18",
            "amount_per_share": 266.0,
            "taxable_base_per_share": 4.0,
        }
    ]
    price = pd.DataFrame(
        {"date": pd.to_datetime(["2026-09-15", "2026-09-16", "2026-09-17"]), "close": [1.0, 2.0, 3.0]}
    )
    result = evidence.entitlement_case(events, price)
    assert result["gross_cash"] == 26600.0
    assert result["buy_on_ex_date_entitled"] is False
    assert result["sell_on_ex_date_entitled"] is True
    assert result["payment_lag_days"] == 2
    # 과세표준액 기준 세금이 총액 기준보다 작아야 한다.
    assert result["taxable_base_15_4pct"] > result["gross_taxed_15_4pct"]
    assert result["tax_overstatement_if_gross_basis"] > 0


def check_receivable() -> None:
    events = [
        {
            "ticker": "498400",
            "record_date": "2026-09-15",
            "ex_date": "2026-09-16",
            "payment_date": "2026-09-18",
            "amount_per_share": 266.0,
            "source_url": "https://example.test",
            "source_sha256": "c" * 64,
        }
    ]
    result = evidence.receivable_case(events, quantity=10)
    assert result["status"] == "ok"
    assert result["receivable_amount"] == 2660.0
    assert result["expected_amount"] == 2660.0
    assert result["not_entitled_is_none"] is True
    assert result["paid_before_payment_date"] == 0.0
    assert result["paid_on_payment_date"] == 2660.0
    assert result["post_sale_still_payable"] is True


def check_notice_table() -> None:
    html = """
    <table><tr><th>종목코드</th><th>종목명</th><th>분배율</th><th>분배금</th></tr>
    <tr><td>069500</td><td>KODEX 200 ETF</td><td>0.44</td><td>446</td></tr>
    <tr><td>091160</td><td>KODEX 반도체 ETF</td><td>0.09</td><td>115</td></tr>
    <tr><td>-</td><td>-</td><td>-</td><td>-</td></tr></table>
    """
    table = evidence.parse_notice_table(html)
    assert table == {"069500": 446.0, "091160": 115.0}, table


def check_sanitize() -> None:
    payload = {
        "nan": float("nan"),
        "inf": np.inf,
        "int": np.int64(3),
        "bool": np.bool_(True),
        "date": date(2026, 4, 30),
        "nested": [np.float64(1.5)],
    }
    clean = evidence.sanitize_json(payload)
    assert clean["nan"] is None and clean["inf"] is None
    assert clean["int"] == 3 and clean["bool"] is True
    assert clean["date"] == "2026-04-30"
    assert clean["nested"] == [1.5]


def check_source_inventory() -> None:
    ids = {item["source_id"] for item in evidence.SOURCE_SURVEY}
    assert "kodex_api" in ids and "kind" in ids and "seibro" in ids
    primary = [item for item in evidence.SOURCE_SURVEY if item["usable_as_primary"] == "yes"]
    assert [item["source_id"] for item in primary] == ["kodex_api"]
    for item in evidence.SOURCE_SURVEY:
        assert item["access_result"]
        assert item["terms_note"]


def main() -> None:
    checks = [
        ("parse date", check_parse_date),
        ("normalize events", check_normalize_events),
        ("deduplicate", check_deduplicate),
        ("ex dates", check_ex_dates),
        ("expected events", check_expected_events),
        ("coverage status", check_coverage_status),
        ("build coverage", check_build_coverage),
        ("missing year not verified", check_missing_year_not_verified),
        ("total return e2e", check_total_return_e2e),
        ("entitlement", check_entitlement),
        ("receivable", check_receivable),
        ("notice table", check_notice_table),
        ("sanitize", check_sanitize),
        ("source inventory", check_source_inventory),
    ]
    for name, check in checks:
        try:
            check()
        except AssertionError as exc:
            print(f"FAIL {name}: {exc}")
            raise
        print(f"PASS {name}")
    print("\np2a2a evidence checks passed")


if __name__ == "__main__":
    main()
