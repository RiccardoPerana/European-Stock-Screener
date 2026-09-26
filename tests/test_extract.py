"""Offline tests for esef_extract.py."""

from datetime import date

from esef_extract import FactIndex, _parse_period, extract

FY_END = date(2024, 12, 31)
CURRENT = "2024-01-01T00:00:00/2025-01-01T00:00:00"
PRIOR = "2023-01-01T00:00:00/2024-01-01T00:00:00"
INSTANT = "2025-01-01T00:00:00"
INSTANT_PRIOR = "2024-01-01T00:00:00"
EUR = "iso4217:EUR"


def doc(*facts) -> dict:
    """Build a minimal xBRL-JSON document from (concept, period, value, dims)."""
    out = {}
    for i, f in enumerate(facts):
        concept, period, value = f[0], f[1], f[2]
        extra = f[3] if len(f) > 3 else {}
        dims = {"concept": concept, "period": period,
                "entity": "lei:TEST", "unit": EUR, **extra}
        out[f"f{i}"] = {"value": str(value), "dimensions": dims}
    return {"documentInfo": {"documentType": "https://xbrl.org/2021/xbrl-json"},
            "facts": out}


def full_filing(**overrides):
    """A filing complete enough to resolve every required field."""
    facts = [
        ("ifrs-full:Revenue", CURRENT, 1_000_000_000),
        ("ifrs-full:ProfitLossFromOperatingActivities", CURRENT, 120_000_000),
        ("ifrs-full:DepreciationAndAmortisationExpense", CURRENT, 50_000_000),
        ("ifrs-full:ProfitLossBeforeTax", CURRENT, 100_000_000),
        ("ifrs-full:IncomeTaxExpenseContinuingOperations", CURRENT, 25_000_000),
        ("ifrs-full:ProfitLossAttributableToOwnersOfParent", CURRENT, 75_000_000),
        ("ifrs-full:CashAndCashEquivalents", INSTANT, 150_000_000),
        ("ifrs-full:CurrentAssets", INSTANT, 400_000_000),
        ("ifrs-full:CurrentLiabilities", INSTANT, 250_000_000),
        ("ifrs-full:Equity", INSTANT, 550_000_000),
        ("ifrs-full:Assets", INSTANT, 1_200_000_000),
        ("ifrs-full:PurchaseOfPropertyPlantAndEquipment", CURRENT, -60_000_000),
    ]
    facts.extend(overrides.get("extra", []))
    return doc(*facts)


def run_extract(document, **kw):
    return extract(document, lei="LEI_T", fiscal_year=2024, period_end=FY_END,
                   name="Test SpA", country="IT", source_ref="ref", **kw)


# ---------------------------------------------------------------------------

def test_period_parsing():
    start, end, dur = _parse_period(CURRENT)
    assert dur and start == date(2024, 1, 1) and end == FY_END, (start, end)
    start, end, dur = _parse_period(INSTANT)
    assert not dur and start is None and end == FY_END, end
    # Date-only form, still exclusive.
    _, end, _ = _parse_period("2025-01-01")
    assert end == FY_END, end
    # 52/53-week retailer year ending on a Saturday.
    start, end, _ = _parse_period("2024-01-29T00:00:00/2025-01-27T00:00:00")
    assert end == date(2025, 1, 26)


def test_resolves_current_year_not_comparative():
    d = full_filing(extra=[("ifrs-full:Revenue", PRIOR, 900_000_000),
                           ("ifrs-full:Equity", INSTANT_PRIOR, 500_000_000)])
    cy, _ = run_extract(d)
    assert cy.facts["revenue"].value == 1_000_000_000, cy.facts["revenue"].value
    assert cy.facts["total_equity"].value == 550_000_000


def test_dimensional_facts_excluded():
    d = full_filing(extra=[
        ("ifrs-full:Revenue", CURRENT, 400_000_000,
         {"ifrs-full:OperatingSegmentsAxis": "seg:North"}),
        ("ifrs-full:Revenue", CURRENT, 600_000_000,
         {"ifrs-full:OperatingSegmentsAxis": "seg:South"}),
    ])
    cy, index = run_extract(d)
    assert index.skipped_dimensional == 2, index.skipped_dimensional
    assert cy.facts["revenue"].value == 1_000_000_000


def test_capex_sign_and_composite_debt():
    d = full_filing(extra=[
        ("ifrs-full:NoncurrentPortionOfNoncurrentBorrowings", INSTANT, 180_000_000),
        ("ifrs-full:NoncurrentLeaseLiabilities", INSTANT, 45_000_000),
    ])
    cy, _ = run_extract(d)
    assert cy.facts["capex"].value == 60_000_000
    assert cy.facts["capex"].sign_flipped is True
    assert cy.facts["capex"].derivation == "summed"
    ltd = cy.facts["long_term_debt"]
    assert ltd.value == 225_000_000, ltd.value
    assert ltd.derivation == "summed"
    assert len(ltd.components) == 2


def test_partial_composite_records_what_it_found():
    d = full_filing(extra=[
        ("ifrs-full:NoncurrentPortionOfNoncurrentBorrowings", INSTANT, 180_000_000),
    ])
    cy, _ = run_extract(d)
    ltd = cy.facts["long_term_debt"]
    assert ltd.value == 180_000_000
    assert ltd.components == ["ifrs-full:NoncurrentPortionOfNoncurrentBorrowings"]


def test_extension_element_not_resolved():
    facts = [f for f in [
        ("ifrs-full:Revenue", CURRENT, 1_000_000_000),
        ("ifrs-full:ProfitLossBeforeTax", CURRENT, 100_000_000),
        ("ifrs-full:IncomeTaxExpenseContinuingOperations", CURRENT, 25_000_000),
        ("ifrs-full:ProfitLossAttributableToOwnersOfParent", CURRENT, 75_000_000),
        ("ifrs-full:DepreciationAndAmortisationExpense", CURRENT, 50_000_000),
        ("ifrs-full:CashAndCashEquivalents", INSTANT, 150_000_000),
        ("ifrs-full:CurrentAssets", INSTANT, 400_000_000),
        ("ifrs-full:CurrentLiabilities", INSTANT, 250_000_000),
        ("ifrs-full:Equity", INSTANT, 550_000_000),
        ("ifrs-full:Assets", INSTANT, 1_200_000_000),
        ("ifrs-full:PurchaseOfPropertyPlantAndEquipment", CURRENT, -60_000_000),
        # Company tagged operating profit with its own element.
        ("testco:RisultatoOperativo", CURRENT, 120_000_000),
    ]]
    cy, index = run_extract(doc(*facts))
    assert cy.facts["ebit"].value is None
    assert cy.missing_required == ["ebit"]
    assert cy.status == "partial"
    assert "testco:RisultatoOperativo" in index.extensions


def test_ebit_fallback_is_opt_in_and_flagged():
    facts_without_ebit = full_filing()["facts"]
    d = {"facts": {k: v for k, v in facts_without_ebit.items()
                   if "OperatingActivities" not in
                   v["dimensions"]["concept"]}}
    d["facts"]["fi"] = {"value": "8000000", "dimensions": {
        "concept": "ifrs-full:InterestExpense", "period": CURRENT,
        "entity": "lei:TEST", "unit": EUR}}

    off, _ = run_extract(d)
    assert off.facts["ebit"].value is None and off.status == "partial"

    on, _ = run_extract(d, ebit_fallback=True)
    assert on.facts["ebit"].value == 108_000_000, on.facts["ebit"].value
    assert on.facts["ebit"].derivation == "computed"
    assert on.status == "complete"


def test_non_calendar_fiscal_year():
    jun_dur = "2023-07-01T00:00:00/2024-07-01T00:00:00"
    jun_inst = "2024-07-01T00:00:00"
    d = doc(
        ("ifrs-full:Revenue", jun_dur, 500_000_000),
        ("ifrs-full:Revenue", CURRENT, 999_999_999),   # decoy calendar period
        ("ifrs-full:Equity", jun_inst, 300_000_000),
    )
    index = FactIndex(d)
    assert index.annual("Revenue", date(2024, 6, 30)).value == 500_000_000
    assert index.instant("Equity", date(2024, 6, 30)).value == 300_000_000


def test_prefers_eur_over_other_units():
    d = doc(
        ("ifrs-full:Revenue", CURRENT, 1_100_000, {"unit": "iso4217:USD"}),
        ("ifrs-full:Revenue", CURRENT, 1_000_000),
    )
    assert FactIndex(d).annual("Revenue", FY_END).value == 1_000_000


def test_non_numeric_and_interim_ignored():
    d = full_filing(extra=[
        ("ifrs-full:DescriptionOfAccountingPolicy", CURRENT, "not a number"),
        ("ifrs-full:Revenue", "2024-01-01T00:00:00/2024-07-01T00:00:00", 480_000_000),
    ])
    cy, index = run_extract(d)
    assert index.skipped_nonnumeric == 1
    assert cy.facts["revenue"].value == 1_000_000_000   # not the half-year


def test_complete_filing_reaches_all_20():
    cy, _ = run_extract(full_filing())
    assert not cy.missing_required, cy.missing_required
    assert cy.status == "complete"
    assert len(cy.facts) == 20
    assert len(cy.assumed_zeros) == 8   # every optional field absent here


def test_grouped_sum_does_not_double_count():
    """Two spellings of the same cash flow must contribute once, not twice."""
    d = full_filing(extra=[
        ("ifrs-full:PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
         CURRENT, -70_000_000),
        ("ifrs-full:PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
         CURRENT, -10_000_000),
    ])
    cy, _ = run_extract(d)
    # PP&E group: the ClassifiedAsInvestingActivities spelling wins over the
    # short form (-60m) already in full_filing. Intangibles adds -10m.
    assert cy.facts["capex"].value == 80_000_000, cy.facts["capex"].value
    assert len(cy.facts["capex"].components) == 2


def test_extension_ebit_matched_but_flagged():
    """Andritz tags operating profit as andritz:EarningsBefore...Ebit."""
    facts = {k: v for k, v in full_filing()["facts"].items()
             if "OperatingActivities" not in v["dimensions"]["concept"]}
    facts["x"] = {"value": "120000000", "dimensions": {
        "concept": "andritz:EarningsBeforeInterestAndTaxesEbit",
        "period": CURRENT, "entity": "lei:TEST", "unit": EUR}}
    cy, _ = run_extract({"facts": facts})
    assert cy.facts["ebit"].value == 120_000_000
    assert cy.facts["ebit"].derivation == "extension"
    assert cy.status == "complete"


def test_ebit_derivation_strips_finance_income():
    facts = {k: v for k, v in full_filing()["facts"].items()
             if "OperatingActivities" not in v["dimensions"]["concept"]}
    for tag, concept, val in [
        ("fc", "ifrs-full:FinanceCosts", 8_000_000),
        ("fi", "ifrs-full:FinanceIncome", 3_000_000),
        ("as", "ifrs-full:ShareOfProfitLossOfAssociatesAndJointVentures"
               "AccountedForUsingEquityMethod", 2_000_000),
    ]:
        facts[tag] = {"value": str(val), "dimensions": {
            "concept": concept, "period": CURRENT,
            "entity": "lei:TEST", "unit": EUR}}
    cy, _ = run_extract({"facts": facts}, ebit_fallback=True)
    # 100m pre-tax + 8m finance costs - 3m finance income - 2m associates
    assert cy.facts["ebit"].value == 103_000_000, cy.facts["ebit"].value
    assert cy.facts["ebit"].derivation == "computed"


def test_combined_capex_tag_not_double_counted():
    """A filer using the all-in element must not also get the components."""
    d = full_filing(extra=[
        ("ifrs-full:PurchaseOfPropertyPlantAndEquipmentIntangibleAssets"
         "OtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets",
         CURRENT, -95_000_000),
        ("ifrs-full:PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
         CURRENT, -10_000_000),
    ])
    cy, _ = run_extract(d)
    assert cy.facts["capex"].value == 95_000_000, cy.facts["capex"].value
    assert cy.facts["capex"].derivation == "reported"


def test_split_depreciation_and_amortisation():
    facts = {k: v for k, v in full_filing()["facts"].items()
             if "Depreciation" not in v["dimensions"]["concept"]}
    for tag, concept, val in [
        ("dp", "ifrs-full:AdjustmentsForDepreciationExpense", 38_000_000),
        ("am", "ifrs-full:AdjustmentsForAmortisationExpense", 12_000_000),
    ]:
        facts[tag] = {"value": str(val), "dimensions": {
            "concept": concept, "period": CURRENT,
            "entity": "lei:TEST", "unit": EUR}}
    cy, _ = run_extract({"facts": facts})
    assert cy.facts["depreciation_amortisation"].value == 50_000_000
    assert cy.facts["depreciation_amortisation"].derivation == "summed"


def test_net_income_rebuilt_from_total_profit():
    facts = {k: v for k, v in full_filing()["facts"].items()
             if "AttributableToOwnersOfParent" not in v["dimensions"]["concept"]}
    for tag, concept, val in [
        ("pl", "ifrs-full:ProfitLoss", 80_000_000),
        ("nci", "ifrs-full:ProfitLossAttributableToNoncontrollingInterests",
         5_000_000),
    ]:
        facts[tag] = {"value": str(val), "dimensions": {
            "concept": concept, "period": CURRENT,
            "entity": "lei:TEST", "unit": EUR}}
    cy, _ = run_extract({"facts": facts})
    assert cy.facts["net_income"].value == 75_000_000, cy.facts["net_income"].value
    assert cy.facts["net_income"].derivation == "computed"

    # No minority profit tagged anywhere -> the total IS the parent share.
    del facts["nci"]
    cy2, _ = run_extract({"facts": facts})
    assert cy2.facts["net_income"].value == 80_000_000


def test_pretax_rebuilt_from_profit_plus_tax():
    facts = {k: v for k, v in full_filing()["facts"].items()
             if "ProfitLossBeforeTax" not in v["dimensions"]["concept"]}
    facts["pl"] = {"value": "75000000", "dimensions": {
        "concept": "ifrs-full:ProfitLoss", "period": CURRENT,
        "entity": "lei:TEST", "unit": EUR}}
    cy, _ = run_extract({"facts": facts})
    # 75m profit + 25m tax charge
    assert cy.facts["pretax_income"].value == 100_000_000
    assert cy.facts["pretax_income"].derivation == "computed"


def _no_current_subtotals(extra_dur=(), extra_inst=()):
    """A filing with no CurrentAssets/CurrentLiabilities subtotals."""
    facts = {k: v for k, v in full_filing()["facts"].items()
             if v["dimensions"]["concept"] not in
             ("ifrs-full:CurrentAssets", "ifrs-full:CurrentLiabilities")}
    for i, c in enumerate(extra_dur):
        facts[f"ed{i}"] = {"value": "500", "dimensions": {
            "concept": c, "period": CURRENT, "entity": "lei:TEST", "unit": EUR}}
    for i, c in enumerate(extra_inst):
        facts[f"ei{i}"] = {"value": "500", "dimensions": {
            "concept": c, "period": INSTANT, "entity": "lei:TEST", "unit": EUR}}
    return {"facts": facts}


def test_retailer_without_subtotals_is_not_a_financial():
    """Kesko and Fnac Darty were wrongly excluded by the first heuristic."""
    d = _no_current_subtotals(extra_inst=["ifrs-full:Inventories"])
    cy, _ = run_extract(d)
    assert cy.likely_financial is False


def test_missing_subtotals_alone_is_not_enough():
    cy, _ = run_extract(_no_current_subtotals())
    assert cy.likely_financial is False


def test_bank_with_positive_markers_is_flagged():
    d = _no_current_subtotals(
        extra_inst=["ifrs-full:LoansAndAdvancesToCustomers",
                    "ifrs-full:DepositsFromCustomers"])
    cy, _ = run_extract(d)
    assert cy.likely_financial is True


def test_current_subtotals_rebuilt_from_identity():
    """Kesko / Fnac case: classified balance sheet, subtotals untagged."""
    d = _no_current_subtotals(extra_inst=[
        "ifrs-full:Inventories",
        "ifrs-full:NoncurrentAssets",
        "ifrs-full:NoncurrentLiabilities",
    ])
    # Assets 1,200m, NoncurrentAssets 500m -> CurrentAssets 700m
    # Equity 550m, NoncurrentLiabilities 500m -> CurrentLiabilities 150m
    for key, concept, val in [("na", "ifrs-full:NoncurrentAssets", 500_000_000),
                              ("nl", "ifrs-full:NoncurrentLiabilities", 500_000_000)]:
        for k, v in d["facts"].items():
            if v["dimensions"]["concept"] == concept:
                v["value"] = str(val)
    cy, _ = run_extract(d)
    assert cy.facts["total_current_assets"].value == 700_000_000, \
        cy.facts["total_current_assets"].value
    assert cy.facts["total_current_assets"].derivation == "computed"
    assert cy.facts["total_current_liabilities"].value == 150_000_000, \
        cy.facts["total_current_liabilities"].value
    # And having rebuilt them, it must no longer look like a bank.
    assert cy.likely_financial is False


def test_pattern_tier_matches_a_lone_extension():
    facts = {k: v for k, v in full_filing()["facts"].items()
             if "Depreciation" not in v["dimensions"]["concept"]}
    facts["fr"] = {"value": "44000000", "dimensions": {
        "concept": "acme:AmortissementsDepreciationsEtProvisions",
        "period": CURRENT, "entity": "lei:TEST", "unit": EUR}}
    cy, _ = run_extract({"facts": facts})
    assert cy.facts["depreciation_amortisation"].value == 44_000_000
    assert cy.facts["depreciation_amortisation"].derivation == "extension"


def test_pattern_tier_refuses_ambiguity_and_deny_list():
    facts = {k: v for k, v in full_filing()["facts"].items()
             if "Depreciation" not in v["dimensions"]["concept"]}
    # Two plausible matches: could be a partition or a double count.
    for tag, concept in [
        ("a", "acme:Adjustmentsfordepreciationexpenseonrightofuseassets"),
        ("b", "acme:Adjustmentsfordepreciationexpenseotherthanrightofuseassets"),
    ]:
        facts[tag] = {"value": "20000000", "dimensions": {
            "concept": concept, "period": CURRENT,
            "entity": "lei:TEST", "unit": EUR}}
    cy, _ = run_extract({"facts": facts})
    assert cy.facts["depreciation_amortisation"].value is None
    assert "depreciation_amortisation" in cy.missing_required

    # And a balance-sheet item containing "amortised" must never match.
    facts2 = {k: v for k, v in full_filing()["facts"].items()
              if "Depreciation" not in v["dimensions"]["concept"]}
    facts2["x"] = {"value": "9000", "dimensions": {
        "concept": "ifrs-full:FinancialAssetsAtAmortisedCost",
        "period": CURRENT, "entity": "lei:TEST", "unit": EUR}}
    cy2, _ = run_extract({"facts": facts2})
    assert cy2.facts["depreciation_amortisation"].value is None


def _no_capex(extra):
    facts = {k: v for k, v in full_filing()["facts"].items()
             if "Purchase" not in v["dimensions"]["concept"]}
    for i, (concept, val) in enumerate(extra):
        facts[f"cx{i}"] = {"value": str(val), "dimensions": {
            "concept": concept, "period": CURRENT,
            "entity": "lei:TEST", "unit": EUR}}
    return {"facts": facts}


def test_capex_pattern_matches_combined_extension():
    cy, _ = run_extract(_no_capex([
        ("kesko:InvestmentsInPropertyPlantAndEquipmentAndIntangibleAssets",
         -88_000_000)]))
    assert cy.facts["capex"].value == 88_000_000, cy.facts["capex"].value
    assert cy.facts["capex"].derivation == "extension"
    assert cy.facts["capex"].sign_flipped is True


def test_capex_pattern_ignores_disposals_and_carrying_amounts():
    cy, _ = run_extract(_no_capex([
        ("ifrs-full:ProceedsFromSalesOfPropertyPlantAndEquipment", 4_000_000),
        ("ifrs-full:DepreciationPropertyPlantAndEquipment", 12_000_000),
        ("ifrs-full:GainsLossesOnDisposalsOfPropertyPlantAndEquipment", 900_000),
    ]))
    assert cy.facts["capex"].value is None, cy.facts["capex"].value
    assert "capex" in cy.missing_required


def test_kesko_depreciation_tie_break():
    """Kesko: a combined D&A line plus a separate depreciation adjustment."""
    facts = {k: v for k, v in full_filing()["facts"].items()
             if "Depreciation" not in v["dimensions"]["concept"]}
    for tag, concept, val in [
        ("a", "kesk:DepreciationAmortisationAndImpairmentCharges", 61_000_000),
        ("b", "kesk:DepreciationAccordingToPlanAdjustment", 55_000_000),
    ]:
        facts[tag] = {"value": str(val), "dimensions": {
            "concept": concept, "period": CURRENT,
            "entity": "lei:TEST", "unit": EUR}}
    cy, _ = run_extract({"facts": facts})
    # The combined line wins: it has both 'depreciat' and 'amorti'.
    assert cy.facts["depreciation_amortisation"].value == 61_000_000, \
        cy.facts["depreciation_amortisation"].value
    assert cy.facts["depreciation_amortisation"].derivation == "extension"


def test_interest_expense_prefers_the_gross_extension_line():
    facts = {k: v for k, v in full_filing()["facts"].items()}
    for tag, concept, val in [
        ("a", "kesk:InterestExpenseAndOtherFinanceCosts", 39_000_000),
        ("b", "ifrs-full:InterestExpenseOnLeaseLiabilities", 7_000_000),
        ("c", "kesk:InterestPaidAndOtherFinanceCosts", 38_000_000),
        ("d", "kesk:InterestIncomeAndOtherFinanceIncome", 4_000_000),
    ]:
        facts[tag] = {"value": str(val), "dimensions": {
            "concept": concept, "period": CURRENT,
            "entity": "lei:TEST", "unit": EUR}}
    cy, _ = run_extract({"facts": facts})
    # InterestExpenseAndOtherFinanceCosts is a listed element, so the
    # company's own gross line wins over lease-only interest, the cash-flow
    # "paid" figure and finance income -- recorded as an extension.
    assert cy.facts["interest_expense"].value == 39_000_000, \
        cy.facts["interest_expense"].value
    assert cy.facts["interest_expense"].derivation == "extension"


def test_interest_never_assumed_zero_when_it_resolves():
    facts = {k: v for k, v in full_filing()["facts"].items()}
    facts["i"] = {"value": "12000000", "dimensions": {
        "concept": "ifrs-full:InterestExpense", "period": CURRENT,
        "entity": "lei:TEST", "unit": EUR}}
    cy, _ = run_extract({"facts": facts})
    assert cy.facts["interest_expense"].value == 12_000_000
    assert "interest_expense" not in cy.assumed_zeros


def _with_debt_no_interest():
    facts = {k: v for k, v in full_filing()["facts"].items()}
    facts["ltd"] = {"value": "200000000", "dimensions": {
        "concept": "ifrs-full:NoncurrentPortionOfNoncurrentBorrowings",
        "period": INSTANT, "entity": "lei:TEST", "unit": EUR}}
    return {"facts": facts}


def test_debt_without_interest_blocks_the_year():
    cy, _ = run_extract(_with_debt_no_interest())
    assert cy.facts["interest_expense"].derivation == "assumed_zero"
    assert cy.blocked, "should be blocked"
    assert cy.status == "partial"
    # All 100 cells are present -- D111 would pass. The workbook cannot see
    # this, which is exactly why the pipeline has to.
    assert not cy.missing_required


def test_debt_free_company_keeps_its_zero():
    facts = {k: v for k, v in full_filing()["facts"].items()}
    cy, _ = run_extract({"facts": facts})
    assert cy.facts["interest_expense"].derivation == "assumed_zero"
    assert not cy.blocked
    assert cy.status == "complete"


def test_reported_zero_short_term_debt_is_not_overwritten_by_the_proxy():
    """
    A company that explicitly tags zero short-term borrowings, and pays
    real interest from a source the debt proxy has nothing to do with
    (e.g. lease interest), must keep that reported zero.

    derive_debt_proxy()'s "has this already resolved" guard must test
    `st.value is not None`, not truthiness -- 0.0 is falsy, so a genuinely
    reported zero would look exactly like "unresolved" and be silently
    overwritten with an estimated debt_proxy figure.
    """
    facts = {k: v for k, v in full_filing()["facts"].items()}
    facts["std"] = {"value": "0", "dimensions": {
        "concept": "ifrs-full:ShorttermBorrowings", "period": INSTANT,
        "entity": "lei:TEST", "unit": EUR}}
    facts["ie"] = {"value": "5000000", "dimensions": {
        "concept": "ifrs-full:InterestExpense", "period": CURRENT,
        "entity": "lei:TEST", "unit": EUR}}
    cy, _ = run_extract({"facts": facts})
    assert cy.facts["short_term_debt"].value == 0.0
    assert cy.facts["short_term_debt"].derivation != "debt_proxy"
    # Only short_term_debt resolved; long_term_debt is genuinely untagged,
    # so derive_debt_proxy's "NEITHER resolved" guard leaves it alone too
    # -- substituting the whole non-current block would double-count
    # against the short-term figure that already resolved. It ends up
    # assumed_zero (the normal absence default), never debt_proxy.
    assert cy.facts["long_term_debt"].derivation == "assumed_zero"


def test_reported_zero_noncurrent_liabilities_used_as_is():
    """
    Same truthiness bug, one step further down derive_debt_proxy(): when
    neither debt field resolved but NoncurrentLiabilities is explicitly
    tagged at 0, that reported zero must be used directly rather than
    falling through to the Assets-Equity-CurrentLiabilities residual,
    which has no reason to agree with a company that discloses it
    genuinely carries no non-current liabilities at all.
    """
    facts = {k: v for k, v in full_filing()["facts"].items()}
    facts["ncl"] = {"value": "0", "dimensions": {
        "concept": "ifrs-full:NoncurrentLiabilities", "period": INSTANT,
        "entity": "lei:TEST", "unit": EUR}}
    facts["ie"] = {"value": "5000000", "dimensions": {
        "concept": "ifrs-full:InterestExpense", "period": CURRENT,
        "entity": "lei:TEST", "unit": EUR}}
    cy, _ = run_extract({"facts": facts})
    assert cy.facts["long_term_debt"].value == 0.0
    assert cy.facts["long_term_debt"].derivation == "debt_proxy"


def test_strict_interest_can_be_disabled():
    cy, _ = run_extract(_with_debt_no_interest(), strict_interest=False)
    assert not cy.blocked
    assert cy.status == "complete"


ACQUIRED = "ifrs-full:CashFlowsUsedInObtainingControlOfSubsidiariesOrOtherBusinessesClassifiedAsInvestingActivities"
DIVESTED = "ifrs-full:CashFlowsFromLosingControlOfSubsidiariesOrOtherBusinessesClassifiedAsInvestingActivities"


def test_acquisitions_are_netted_of_divestitures():
    cy, _ = run_extract(full_filing(extra=[
        (ACQUIRED, CURRENT, 80_000_000),
        (DIVESTED, CURRENT, 30_000_000),
    ]))
    acq = cy.facts["acquisitions_net"]
    assert acq.value == 50_000_000, acq.value
    assert acq.derivation == "summed"
    assert len(acq.components) == 2


def test_acquisitions_netting_ignores_the_filers_sign_convention():
    # Some filers tag both flows negative; the element names already say
    # which way the cash moved, so the result must be the same.
    cy, _ = run_extract(full_filing(extra=[
        (ACQUIRED, CURRENT, -80_000_000),
        (DIVESTED, CURRENT, -30_000_000),
    ]))
    assert cy.facts["acquisitions_net"].value == 50_000_000


def test_divestitures_alone_give_a_negative_net():
    cy, _ = run_extract(full_filing(extra=[(DIVESTED, CURRENT, 30_000_000)]))
    assert cy.facts["acquisitions_net"].value == -30_000_000
