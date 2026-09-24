"""
pipeline_status.stages() -- `ready` and `need_mapping` must fail
independently.

With one shared try/except, an exception raised while computing
need_mapping (the industry-mapping count) would reset an already-correctly-
computed `ready` back to 0, so the Dashboard could report nothing
screenable when the pipeline actually had companies ready to value.
"""

import tempfile
from pathlib import Path

import pytest

import pipeline_status as ps
from cache import Cache, CompanyYear
from fields import REQUIRED_KEYS

LEI = "TESTLEI00000000000001"
YEARS = [2021, 2022, 2023, 2024, 2025]


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        with Cache(path) as db:
            for year in YEARS:
                cy = CompanyYear(lei=LEI, fiscal_year=year, source="manual",
                                 extractor_version="test-1.0",
                                 name="Test Co", country="IT",
                                 period_end=f"{year}-12-31")
                for key in REQUIRED_KEYS:
                    cy.set(key, 1.0, "manual")
                db.put(cy)
            db.put_listings(LEI, [{
                "isin": "XX0000TEST", "ticker": "TEST", "exchange": "IM",
                "is_primary": True}])
        yield path


def test_a_failure_in_need_mapping_does_not_zero_out_ready(
        monkeypatch, db_path, tmp_path):
    import write_workbook
    import industry_worksheet

    # readiness() says every company is ready to value (falsy = no gaps).
    monkeypatch.setattr(write_workbook, "readiness",
                        lambda lei, c, params, years: [])
    # unmapped() blows up -- some future refactor, a bad params shape,
    # anything -- must not be able to touch `ready`.
    def boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(industry_worksheet, "unmapped", boom)

    # A params file that exists and is non-empty, so `if db.exists() and
    # params:` is actually entered (an absent file would make params {}
    # and skip the whole block, testing nothing).
    params_path = tmp_path / "parameters.json"
    params_path.write_text('{"company_industry": {}}', encoding="utf-8")

    steps = ps.stages(db=db_path, params_path=params_path,
                      tables_path=tmp_path / "missing.json",
                      template=tmp_path / "missing.xlsx",
                      cache_dir=tmp_path / "cache", out_dir=tmp_path / "out")

    screen = next(s for s in steps if s["key"] == "screen")
    assert screen["count"] == 1, (
        "the correctly-computed ready count must survive an unrelated "
        "exception in the industry-mapping count")


def test_prices_fetched_today_is_not_stale_against_identity_over_a_weekend(
        db_path, tmp_path):
    """
    identity.resolved_at is a real timestamp of when the script ran, always
    "now". A price fetched right now on a Saturday still correctly stores
    Friday's close as `as_of` -- the quote is genuinely dated Friday, not
    wrong. Comparing identity's "now" against prices' "Friday" would flag
    prices as needing a re-run they had just had, and the alert could never
    clear until markets reopened. So staleness uses ran_date (fetched_at):
    when the fetch itself ran, which is also "now" here.
    """
    from datetime import date, timedelta

    last_trading_day = (date.today() - timedelta(days=2)).isoformat()
    with Cache(db_path) as db:
        db.put_price(LEI, last_trading_day, 42.0, "EUR", "TEST", "IM",
                     "fixture")

    steps = ps.stages(db=db_path, params_path=tmp_path / "missing.json",
                      tables_path=tmp_path / "missing.json",
                      template=tmp_path / "missing.xlsx",
                      cache_dir=tmp_path / "cache", out_dir=tmp_path / "out")

    prices = next(s for s in steps if s["key"] == "prices")
    assert prices["date"] == last_trading_day
    assert prices["health"] == "ok", (
        f"prices fetched today should not read stale just because the "
        f"quote it fetched is dated {last_trading_day}: {prices['message']!r}")
