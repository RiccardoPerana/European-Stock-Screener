"""Offline tests for fields.py and cache.py."""


import fields as F
from cache import CompanyYear

VERSION = "esef-0.1.0"
YEARS = [2021, 2022, 2023, 2024, 2025]


def build(lei, year, *, complete=True, source="esef", version=VERSION):
    """A company-year with plausible figures, in raw units (not millions)."""
    cy = CompanyYear(lei=lei, fiscal_year=year, source=source,
                     extractor_version=version, name=f"Test {lei}",
                     country="IT", period_end=f"{year}-12-31",
                     source_ref=f"{lei}-{year}-ESEF-IT-0")
    base = 1_000_000_000
    cy.set("revenue", base, "reported", element="Revenue")
    cy.set("ebit", base * 0.12, "computed")
    cy.set("depreciation_amortisation", base * 0.05, "reported")
    cy.set("pretax_income", base * 0.10, "reported")
    cy.set("tax_expense", base * 0.025, "reported")
    cy.set("net_income", base * 0.075, "reported")
    cy.set("cash", base * 0.15, "reported")
    cy.set("total_current_assets", base * 0.40, "reported")
    cy.set("total_current_liabilities", base * 0.25, "reported")
    cy.set("total_equity", base * 0.55, "reported", element="Equity")
    cy.set("total_assets", base * 1.20, "reported")
    if complete:
        # Providers and filings routinely give capex as a negative outflow.
        cy.set("capex", -base * 0.06, "reported",
               element="PurchaseOfPropertyPlantAndEquipment")
    cy.set("long_term_debt", base * 0.20, "summed",
           components=["NoncurrentPortionOfNoncurrentBorrowings",
                       "NoncurrentLeaseLiabilities"])
    return cy


def test_field_definitions():
    assert len(F.ALL_FIELDS) == 20
    optional = [f for f in F.ALL_FIELDS if f.absence is F.Absence.ZERO_IF_ABSENT]
    assert len(F.REQUIRED_KEYS) + len(optional) == 20
    assert F.cell(F.BY_KEY["revenue"], 0) == "B20"
    assert F.cell(F.BY_KEY["revenue"], 4) == "F20"
    assert F.cell(F.BY_KEY["capex"], 4) == "F44"
    assert F.cell(F.BY_KEY["acquisitions_net"], 0) == "B45"
    try:
        F.cell(F.BY_KEY["revenue"], 5)
        raise AssertionError("should reject out-of-range offset")
    except ValueError:
        pass


def test_capex_sign_is_forced_positive():
    cy = build("LEI_A", 2021)
    capex = cy.facts["capex"]
    assert capex.value == 60_000_000.0, capex.value
    # The transformation must be visible, not silent -- and separate from
    # the derivation, so a composite field can carry both.
    assert capex.sign_flipped is True
    assert capex.derivation == "reported", capex.derivation


def test_permitted_zeros_only_fill_optional_fields():
    cy = build("LEI_A", 2021)
    filled = cy.fill_permitted_zeros()
    assert "acquisitions_net" in filled
    assert "preferred_stock" in filled
    assert "minority_interest" in filled
    assert "long_term_debt" not in filled       # already resolved
    assert "revenue" not in filled              # REQUIRED, never assumed
    assert not cy.missing_required
    assert cy.status == "complete"
    assert len(cy.facts) == 20


def test_missing_required_blocks_completion():
    cy = build("LEI_B", 2021, complete=False)   # no capex
    cy.fill_permitted_zeros()
    assert cy.missing_required == ["capex"], cy.missing_required
    assert cy.status == "partial"


def test_roundtrip_preserves_provenance(db):
    cy = build("LEI_A", 2021)
    cy.fill_permitted_zeros()
    db.put(cy)

    got = db.get("LEI_A", 2021)
    assert got is not None
    assert got.name == "Test LEI_A" and got.country == "IT"
    assert len(got.facts) == 20
    assert got.facts["revenue"].element == "Revenue"
    assert got.facts["long_term_debt"].derivation == "summed"
    assert got.facts["long_term_debt"].components == [
        "NoncurrentPortionOfNoncurrentBorrowings", "NoncurrentLeaseLiabilities"]
    assert got.facts["capex"].sign_flipped is True
    assert got.assumed_zeros


def test_reextraction_replaces_facts_wholesale(db):
    cy = build("LEI_C", 2021)
    cy.fill_permitted_zeros()
    db.put(cy)
    assert db.get("LEI_C", 2021).facts["ebit"].derivation == "computed"

    # Better extractor: EBIT now found as a real tag, not derived.
    cy2 = build("LEI_C", 2021, version="esef-0.2.0")
    cy2.set("ebit", 999.0, "reported", element="ProfitLossFromOperatingActivities")
    cy2.fill_permitted_zeros()
    db.put(cy2)

    got = db.get("LEI_C", 2021)
    assert got.facts["ebit"].value == 999.0
    assert got.facts["ebit"].derivation == "reported"
    assert len(got.facts) == 20, "stale facts left behind"


def test_version_invalidation_and_failures(db):
    cy = build("LEI_D", 2021)
    cy.fill_permitted_zeros()
    db.put(cy)

    assert db.has("LEI_D", 2021) is True
    assert db.has("LEI_D", 2021, extractor_version=VERSION) is True
    assert db.has("LEI_D", 2021, extractor_version="esef-0.2.0") is False

    db.mark_failed("LEI_E", 2021, "esef", VERSION, "package 404")
    assert db.has("LEI_E", 2021) is False
    assert db.get("LEI_E", 2021).facts == {}


def test_missing_years_drives_incremental_work(db):
    for y in (2021, 2022, 2023):
        cy = build("LEI_F", y)
        cy.fill_permitted_zeros()
        db.put(cy)
    assert db.missing_years("LEI_F", YEARS) == [2024, 2025]
    # This is the Spain case: four good years, one stalled.
    assert db.missing_years("LEI_NEW", YEARS) == YEARS


def test_complete_entities_and_intake(db):
    for y in YEARS:
        cy = build("LEI_G", y)
        cy.fill_permitted_zeros()
        db.put(cy)
    # Partial company must not qualify.
    for y in YEARS:
        cy = build("LEI_H", y, complete=False)
        cy.fill_permitted_zeros()
        db.put(cy)

    universe = db.complete_entities(YEARS)
    assert "LEI_G" in universe
    assert "LEI_H" not in universe

    intake = db.to_intake("LEI_G", YEARS)
    assert intake["identification"]["fiscal_year"] == "2025"
    assert len(intake["statements"]) == 20
    for key, series in intake["statements"].items():
        assert len(series) == 5, key
        assert all(v is not None for v in series), key
    # Scaling: 1e9 raw -> 1000 millions, matching Inputs!B9 = Millions.
    assert intake["statements"]["revenue"] == [1000.0] * 5
    assert intake["statements"]["capex"] == [60.0] * 5
    assert intake["provenance"]["sources"] == ["esef"]
    assert "acquisitions_net" in intake["provenance"]["assumed_zeros"]["2025"]

    try:
        db.to_intake("LEI_H", YEARS)
        raise AssertionError("should refuse a partial company")
    except ValueError as e:
        assert "capex" in str(e)


def test_diagnostics(db):
    report = dict((k, (u, a)) for k, u, a in db.field_failure_report())
    # capex is unresolved only for the deliberately partial LEI_H (5 years).
    assert report["capex"][0] == 5, report["capex"]
    assert report["revenue"][0] == 0
    assert report["acquisitions_net"][1] > 0   # assumed zeros counted

    s = db.stats()
    assert s["complete"] > 0 and s["partial"] > 0 and s["failed"] == 1
    assert s["assumed_zeros"] > 0


def test_clear_extractions_keeps_listings_prices_and_share_counts():
    import tempfile
    from pathlib import Path
    from cache import Cache

    with tempfile.TemporaryDirectory() as tmp:
        with Cache(Path(tmp) / "t.db") as db:
            for y in YEARS:
                db.put(build("LEI_R", y))
            db.put_listings("LEI_R", [{"isin": "IT000", "ticker": "R",
                                       "exchange": "IM", "is_primary": True}])
            db.put_price("LEI_R", "2026-01-02", 10.0, "EUR", "R", "XMIL", "t")
            db.put_share_count("LEI_R", "2026-01-02", 1e6, "manual")

            assert db.clear_extractions() == len(YEARS)
            assert db.stats()["company_years"] == 0
            assert db.stats()["facts"] == 0
            assert db.primary_listing("LEI_R")["ticker"] == "R"
            assert db.latest_price("LEI_R")["price"] == 10.0
            assert db.latest_share_count("LEI_R")["shares"] == 1e6
