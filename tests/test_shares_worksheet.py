"""
shares_worksheet -- the share-count stage run over and over (as the GUI and
the quarterly CI screen do) must not re-date old counts, and must keep an
EPS-derived count in step with the latest accounts.
"""

from datetime import date

import pytest

import config
import shares_worksheet as sw
from cache import Cache, CompanyYear
from fields import REQUIRED_KEYS

LEI = "SHARESLEI00000000001"
YEARS = config.FISCAL_YEARS
FY0 = max(YEARS)


@pytest.fixture
def db(tmp_path):
    with Cache(tmp_path / "t.db") as cache:
        for year in YEARS:
            cy = CompanyYear(lei=LEI, fiscal_year=year, source="manual",
                             extractor_version="test", name="Share Co",
                             country="IT", period_end=f"{year}-12-31")
            for key in REQUIRED_KEYS:
                cy.set(key, 1.0, "manual")
            cy.set("revenue", 1_000_000_000, "manual")
            cache.put(cy)
        cache.put_listings(LEI, [{"isin": "IT0000", "ticker": "SHR",
                                  "exchange": "IM", "is_primary": True}])
        cache.put_price(LEI, "2026-01-02", 10.0, "EUR", "SHR", "XMIL", "t")
        # 50m shares x 10 EUR = 500m cap on 1bn revenue: inside the band.
        cache.put_share_estimate(LEI, FY0, wavg_diluted=50_000_000)
        yield cache


def _round_trip(db, tmp_path):
    """What job_shares does: import the worksheet, seed, re-export."""
    csv_path = tmp_path / "shares.csv"
    imported = sw.do_import(csv_path, db, YEARS) if csv_path.exists() else 0
    seeded = sw.seed_derived(db, YEARS)
    sw.do_export(csv_path, sw.gather(db, YEARS))
    return imported, seeded


def test_reimporting_the_export_does_not_redate_counts(db, tmp_path):
    db.put_share_count(LEI, "2025-03-01", 48_000_000, "annual report 2024")
    _round_trip(db, tmp_path)
    imported, _ = _round_trip(db, tmp_path)

    assert imported == 0
    latest = db.latest_share_count(LEI)
    assert latest["as_of"] == "2025-03-01"
    assert latest["source"] == "annual report 2024"


def test_eps_derived_count_is_seeded_then_refreshed(db, tmp_path):
    assert sw.seed_derived(db, YEARS) == 1
    assert db.latest_share_count(LEI)["shares"] == 50_000_000
    assert sw.seed_derived(db, YEARS) == 0          # unchanged: no new row

    # New accounts imply a different count: the derived figure follows.
    db.put_share_estimate(LEI, FY0, wavg_diluted=52_000_000)
    assert sw.seed_derived(db, YEARS) == 1
    latest = db.latest_share_count(LEI)
    assert latest["shares"] == 52_000_000
    assert latest["as_of"] == date.today().isoformat()


def test_a_hand_entered_count_is_never_replaced_by_a_derived_one(db, tmp_path):
    db.put_share_count(LEI, "2025-03-01", 48_000_000, "manual")
    assert sw.seed_derived(db, YEARS) == 0
    assert db.latest_share_count(LEI)["shares"] == 48_000_000
