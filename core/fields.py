"""
Canonical field definitions for the valuation model.
====================================================

Single source of truth for WHAT gets extracted, WHERE it lands in the
workbook, and HOW it must be shaped on the way. Everything downstream --
the ESEF extractor, the FactSet adapter, the manual intake path, the
workbook writer -- reads this module. Nothing hardcodes a row number.

THE ABSENCE PROBLEM
-------------------
Valuation!D111 requires COUNT(...)=100 across the 20 fields x 5 years. One
blank voids the valuation. But blanks arise for two completely different
reasons, and conflating them is how a pipeline produces a clean wrong
answer:

  1. The company genuinely has no such item. A firm with no subsidiaries
     has no minority interest. A firm that made no acquisitions has no
     acquisitions line. Zero is the correct economic reading.

  2. The extractor failed to find it. The tag was an extension element,
     or the label was unusual, or the statement was laid out oddly.
     Zero here is a fabrication, and the pipeline never invents financial
     data.

The two are indistinguishable from the outside: both look like a missing
tag. So this module splits the fields by `absence` policy, and every
assumed zero is recorded in provenance with derivation='assumed_zero'.

That gives you a measurable rate rather than a hidden assumption. If a
company-year needs six assumed zeros to reach 100 cells, that is not a
valuation you should trust, and the cache lets you count it and set a
threshold: Cache.field_failure_report() gives the rate per field.

TAXONOMY ELEMENTS
-----------------
The `elements` lists are ifrs-full taxonomy element names, in priority
order. The composite fields (debt, capex, EBIT) lean on the fallback tiers
below them: IFRS mandates no operating-profit subtotal, so
ProfitLossFromOperatingActivities is optional and widely extended.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Absence(Enum):
    """What to do when a field cannot be resolved from the source."""

    REQUIRED = "required"
    """Cannot be assumed. If missing, the company-year fails extraction.

    Every company that exists has revenue, total assets and equity. A gap
    here means the extractor failed, not that the item does not exist.
    """

    ZERO_IF_ABSENT = "zero_if_absent"
    """May legitimately not exist. Recorded as 0.0 with derivation
    'assumed_zero' so the assumption stays visible and countable.
    """


class Sign(Enum):
    """Sign transformation applied after extraction."""

    AS_REPORTED = "as_reported"
    FORCE_POSITIVE = "force_positive"
    """Take the absolute value.

    Only for fields where the model's convention is unambiguous and the
    economic sign cannot meaningfully be negative. Capex is the case that
    matters: Inputs!B44:F44 must be positive because Valuation!D23 ADDS
    it. Providers and filings return it negative about as often as not,
    and NO validation check catches the error -- it silently inverts
    reinvestment and therefore growth. This is the pipeline's job alone.
    """


@dataclass(frozen=True)
class Field:
    """One line item, from taxonomy tag through to workbook cell."""

    key: str
    label: str
    row: int
    """Row in Inputs. Columns B..F map to FY-4..FY0."""
    statement: str
    absence: Absence
    sign: Sign
    elements: tuple[str, ...]
    """Candidate taxonomy elements, highest priority first."""
    minus_of: tuple[tuple[str, ...], ...] = ()
    """Component groups SUBTRACTED from the `sum_of` total, in the same tier.

    Acquisitions are specified net of divestitures, and the taxonomy splits
    them: cash used obtaining control, cash from losing control. When a
    field has `minus_of`, every component on either side is taken as a
    MAGNITUDE: the element names already say which way the cash moved, and
    filers disagree about the sign they tag these flows with -- a sample of
    40 filings had both elements reported negative as well as positive.
    """
    sum_of: tuple[tuple[str, ...], ...] = ()
    """Fallback: build the field as a SUM over component GROUPS.

    Tried only after `elements` misses. A company that reports one combined
    "purchases of PP&E, intangibles and other non-current assets" line must
    use that, not a reconstruction -- and must not have both counted.

    Each inner tuple is one economic component with alternate tag names;
    the first name that resolves wins for that group, and the groups are
    then added. Grouping matters: capex needs PP&E purchases PLUS
    intangibles purchases, but the IFRS taxonomy carries two spellings of
    each, and a flat list would add both spellings of the same cash flow
    and double the number.

    Composite fields are where extraction gets hard and where vendors
    disagree with each other. Long-term debt is the important one: under
    IFRS 16 the non-current lease liability is a separate tag, and whether
    it belongs in 'debt' is exactly the definitional choice that moves
    Valuation!D34 (debt weight) and therefore WACC. The model's row 35 is
    explicitly 'long-term debt INCL. capitalised leases', so we include it
    and record that we did.
    """
    pattern_require: tuple[str, ...] = ()
    """Last-resort matching on NORMALISED local names (lowercased, letters
    and digits only). Used where the tail of failures is company extensions
    with no shared spelling -- AmortissementsDepreciationsEtProvisions,
    AmortizationDepreciationAndImapirmentsOnFixedAssets (sic), and so on.

    Applied only after `elements` and `sum_of` both miss, and only when
    EXACTLY ONE concept matches. Two matches could be a partition to sum or
    the same figure twice, and there is no safe way to tell, so the field
    stays unresolved instead."""

    pattern_any: tuple[str, ...] = ()
    """At least ONE of these must appear, where `pattern_require` needs all
    of them. Capex needs the asset class AND some flow verb: without the
    verb, the balance-sheet carrying amount of property, plant and equipment
    would match a cash-flow field."""

    pattern_prefer: tuple[str, ...] = ()
    """Tie-breaker. Concepts containing ALL of these outrank the rest, and
    only the top rank is considered for the exactly-one rule. Kesko reports
    both a combined DepreciationAmortisationAndImpairmentCharges line and a
    separate DepreciationAccordingToPlanAdjustment; requiring both
    'depreciat' and 'amorti' picks the combined line rather than abandoning
    the company."""

    pattern_deny: tuple[str, ...] = ()
    """Normalised substrings that veto a pattern match. FinancialAssetsAt
    AmortisedCost contains 'amortised' and is a balance-sheet item; without
    a deny list a pattern tier would happily book it as D&A."""

    note: str = ""


# ---------------------------------------------------------------------------
# Income statement -- Inputs rows 20-26
# ---------------------------------------------------------------------------

INCOME_STATEMENT = [
    Field(
        key="revenue", label="Revenue", row=20, statement="income",
        absence=Absence.REQUIRED, sign=Sign.AS_REPORTED,
        elements=("Revenue", "RevenueFromContractsWithCustomers",
                  "RevenueAndOperatingIncome"),
    ),
    Field(
        key="ebit", label="EBIT", row=21, statement="income",
        absence=Absence.REQUIRED, sign=Sign.AS_REPORTED,
        elements=("ProfitLossFromOperatingActivities",
                  "EarningsBeforeInterestAndTaxesEbit",
                  "OperatingProfitLoss"),
        note="IFRS mandates no operating-profit subtotal, so this tag is "
             "optional and heavily extended. Expect the lowest hit rate of "
             "any required field. Fallback: pre-tax income + finance costs "
             "- finance income, but that is a DERIVATION and must be "
             "recorded as one, not passed off as reported.",
    ),
    Field(
        key="depreciation_amortisation", label="Depreciation & amortisation",
        row=22, statement="income",
        absence=Absence.REQUIRED, sign=Sign.FORCE_POSITIVE,
        elements=(
            "DepreciationAndAmortisationExpense",
            "AdjustmentsForDepreciationAndAmortisationExpense",
            "DepreciationAmortisationAndImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss",
            "AdjustmentsForDepreciationAmortisationExpenseAndImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss",
            "AdjustmentsForDepreciationAndAmortisationExpenseAndImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss",
        ),
        sum_of=(
            ("AdjustmentsForDepreciationExpense", "DepreciationExpense"),
            ("AdjustmentsForAmortisationExpense", "AmortisationExpense",
             "AdjustmentsForAmortisationExpenseAndImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss"),
        ),
        pattern_require=("depreciat",),
        pattern_prefer=("amorti",),
        pattern_deny=("amortisedcost", "amortizedcost", "financialasset",
                      "investment", "rightofuseasset", "propertyplant"),
        note="Usually tagged in the cash flow reconciliation rather than the "
             "income statement. About a quarter of filers split depreciation "
             "and amortisation into two adjustments instead of one combined "
             "line, hence the fallback sum. Impairment is deliberately NOT "
             "summed in: the model wants D&A, and folding impairment into the "
             "non-cash addback would overstate it in exactly the years it "
             "matters most.",
    ),
    Field(
        key="interest_expense", label="Interest expense (gross)", row=23,
        statement="income",
        absence=Absence.ZERO_IF_ABSENT, sign=Sign.FORCE_POSITIVE,
        elements=("InterestExpense", "FinanceCosts",
                  "InterestExpenseAndOtherFinanceCosts"),
        pattern_require=("interest",),
        pattern_prefer=("expense",),
        pattern_deny=("income", "received", "revenue", "receivable",
                      "paid", "lease", "asset", "capitalised", "capitalized"),
        note="The model wants GROSS interest. FinanceCosts is often "
             "reported net of finance income, or bundled with FX and other "
             "items. Prefer InterestExpense where tagged.\n\n"
             "This field is ZERO_IF_ABSENT, which is dangerous here and is "
             "why it gets a pattern tier as well. Valuation!D39/D40 derive "
             "the interest coverage ratio, and zero interest means INFINITE "
             "coverage -- which resolves to the top of the RefData ladder "
             "and the cheapest default spread. A company with real debt and "
             "an untagged interest line would be silently rated AAA. The "
             "driver counts this case separately.",
    ),
    Field(
        key="pretax_income", label="Pre-tax income", row=24, statement="income",
        absence=Absence.REQUIRED, sign=Sign.AS_REPORTED,
        elements=("ProfitLossBeforeTax",),
    ),
    Field(
        key="tax_expense", label="Income tax expense", row=25, statement="income",
        absence=Absence.REQUIRED, sign=Sign.AS_REPORTED,
        elements=(
            "IncomeTaxExpenseContinuingOperations",
            "IncomeTaxExpenseBenefit",
        ),
    ),
    Field(
        key="net_income", label="Net income to common shareholders", row=26,
        statement="income",
        absence=Absence.REQUIRED, sign=Sign.AS_REPORTED,
        elements=("ProfitLossAttributableToOwnersOfParent",),
        note="Attributable to owners of parent, NOT total profit. The "
             "minority share is excluded here and deducted separately at "
             "Valuation!D79.",
    ),
]


# ---------------------------------------------------------------------------
# Balance sheet -- Inputs rows 30-40
# ---------------------------------------------------------------------------

BALANCE_SHEET = [
    Field(
        key="cash", label="Cash & cash equivalents", row=30, statement="balance",
        absence=Absence.REQUIRED, sign=Sign.AS_REPORTED,
        elements=("CashAndCashEquivalents", "Cash"),
    ),
    Field(
        key="short_term_investments",
        label="Short-term investments / marketable securities", row=31,
        statement="balance",
        absence=Absence.ZERO_IF_ABSENT, sign=Sign.AS_REPORTED,
        elements=("OtherCurrentFinancialAssets", "CurrentInvestments"),
        note="Highly variable tagging. Many companies genuinely hold none.",
    ),
    Field(
        key="total_current_assets", label="Total current assets", row=32,
        statement="balance",
        absence=Absence.REQUIRED, sign=Sign.AS_REPORTED,
        elements=("CurrentAssets",),
    ),
    Field(
        key="total_current_liabilities", label="Total current liabilities",
        row=33, statement="balance",
        absence=Absence.REQUIRED, sign=Sign.AS_REPORTED,
        elements=("CurrentLiabilities",),
    ),
    Field(
        key="short_term_debt",
        label="Short-term debt incl. current leases", row=34,
        statement="balance",
        absence=Absence.ZERO_IF_ABSENT, sign=Sign.AS_REPORTED,
        elements=(),
        sum_of=(
            ("ShorttermBorrowings", "CurrentBorrowings"),
            ("CurrentPortionOfLongtermBorrowings",),
            ("CurrentLeaseLiabilities", "LeaseLiabilitiesCurrent"),
        ),
    ),
    Field(
        key="long_term_debt",
        label="Long-term debt incl. capitalised leases", row=35,
        statement="balance",
        absence=Absence.ZERO_IF_ABSENT, sign=Sign.AS_REPORTED,
        elements=(),
        sum_of=(
            ("NoncurrentPortionOfNoncurrentBorrowings", "LongtermBorrowings",
             "NoncurrentBorrowings"),
            ("NoncurrentLeaseLiabilities", "LeaseLiabilitiesNoncurrent"),
        ),
        note="The IFRS 16 lease liability is included deliberately -- row 35 "
             "is specified as incl. capitalised leases. This is the single "
             "biggest definitional divergence against commercial vendors, "
             "some of which exclude it. It moves the debt weight at D34 and "
             "therefore WACC, so it is the first thing to check in the "
             "FactSet comparison.",
    ),
    Field(
        key="preferred_stock", label="Preferred stock", row=36,
        statement="balance",
        absence=Absence.ZERO_IF_ABSENT, sign=Sign.AS_REPORTED,
        elements=("PreferenceShares", "IssuedCapitalPreferenceShares"),
        note="Rare in continental Europe. Genuine zero in most cases.",
    ),
    Field(
        key="minority_interest", label="Minority / non-controlling interest",
        row=37, statement="balance",
        absence=Absence.ZERO_IF_ABSENT, sign=Sign.AS_REPORTED,
        elements=("NoncontrollingInterests",
                  "EquityAttributableToNoncontrollingInterests"),
        note="BALANCE SHEET stock, not the P&L share of profit. Several "
             "vendor schemas define their 'minorityInterest' field as the "
             "latter, which is a different number entirely.",
    ),
    Field(
        key="total_equity",
        label="Total equity (incl. minority interest and preferred)", row=38,
        statement="balance",
        absence=Absence.REQUIRED, sign=Sign.AS_REPORTED,
        elements=("Equity",),
        note="ifrs-full:Equity IS total equity including minorities, which "
             "is exactly what row 38 specifies. This is a field commercial "
             "vendors commonly get wrong by exposing parent-only equity. "
             "Do NOT substitute EquityAttributableToOwnersOfParent -- it "
             "understates invested capital at D16 and overstates ROIC.",
    ),
    Field(
        key="equity_method_investments",
        label="Equity-method investments / associates", row=39,
        statement="balance",
        absence=Absence.ZERO_IF_ABSENT, sign=Sign.AS_REPORTED,
        elements=(
            "InvestmentAccountedForUsingEquityMethod",
            "InvestmentsInAssociatesAndJointVenturesAccountedForUsingEquityMethod",
            "InvestmentsInAssociatesAccountedForUsingEquityMethod",
        ),
    ),
    Field(
        key="total_assets", label="Total assets", row=40, statement="balance",
        absence=Absence.REQUIRED, sign=Sign.AS_REPORTED,
        elements=("Assets",),
        note="Feeds only the holding-company diagnostic at D92/D110, outside "
             "the valuation chain -- but D111 fails without it.",
    ),
]


# ---------------------------------------------------------------------------
# Cash flow -- Inputs rows 44-45
# ---------------------------------------------------------------------------

CASH_FLOW = [
    Field(
        key="capex", label="Capital expenditure", row=44, statement="cashflow",
        absence=Absence.REQUIRED, sign=Sign.FORCE_POSITIVE,
        elements=(
            "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOther"
            "ThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets",
            "PurchaseOfPropertyPlantAndEquipmentAndPurchaseOfIntangible"
            "AssetsClassifiedAsInvestingActivities",
            "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssets"
            "ClassifiedAsInvestingActivities",
            "AcquisitionOfPropertyPlantAndEquipmentAndIntangibleAssets",
        ),
        sum_of=(
            ("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
             "PurchaseOfPropertyPlantAndEquipment",
             "AcquisitionOfPropertyPlantAndEquipment"),
            ("PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
             "PurchaseOfIntangibleAssetsOtherThanGoodwill"),
            ("PurchaseOfInvestmentProperty",),
            ("PurchaseOfOtherLongtermAssetsClassifiedAsInvestingActivities",),
        ),
        pattern_require=("propertyplantandequipment",),
        pattern_any=("purchase", "acquisition", "addition", "investment",
                     "payment", "expenditure"),
        pattern_deny=("proceeds", "disposal", "sale", "depreciat",
                      "impairment", "carryingamount", "revaluation",
                      "revenue", "profit"),
        note="MUST be positive. Valuation!D23 adds it. No check catches a "
             "sign error; it inverts reinvestment and therefore growth.\n\n"
             "The single combined element is tried FIRST. A filer using it "
             "reports one line covering PP&E, intangibles, investment "
             "property and other non-current assets, so summing the "
             "components as well would double the number.",
    ),
    Field(
        key="acquisitions_net", label="Acquisitions, net of divestitures",
        row=45, statement="cashflow",
        absence=Absence.ZERO_IF_ABSENT, sign=Sign.AS_REPORTED,
        elements=(),
        sum_of=(
            ("CashFlowsUsedInObtainingControlOfSubsidiariesOrOtherBusinessesClassifiedAsInvestingActivities",),
        ),
        minus_of=(
            ("CashFlowsFromLosingControlOfSubsidiariesOrOtherBusinessesClassifiedAsInvestingActivities",),
        ),
        note="Cash paid obtaining control less cash received losing it, so "
             "positive means net buying. Legitimately zero in any year with "
             "no M&A, which is most years for most companies.",
    ),
]


ALL_FIELDS: list[Field] = INCOME_STATEMENT + BALANCE_SHEET + CASH_FLOW
BY_KEY: dict[str, Field] = {f.key: f for f in ALL_FIELDS}
FIELD_KEYS: tuple[str, ...] = tuple(f.key for f in ALL_FIELDS)

REQUIRED_KEYS = frozenset(
    f.key for f in ALL_FIELDS if f.absence is Absence.REQUIRED
)

# Guard rails. D111 counts exactly 100 cells over 20 fields x 5 years; if
# this module ever drifts from that, the workbook write silently misaligns.
assert len(ALL_FIELDS) == 20, f"expected 20 fields, got {len(ALL_FIELDS)}"
assert len(BY_KEY) == 20, "duplicate field key"
assert {f.row for f in INCOME_STATEMENT} == set(range(20, 27))
assert {f.row for f in BALANCE_SHEET} == set(range(30, 41))
assert {f.row for f in CASH_FLOW} == {44, 45}


# Not model fields, but reliably tagged and needed to derive EBIT when the
# operating-profit subtotal is absent or tagged as an extension, and to run
# the balance-sheet heuristic that identifies financial companies.
NONCURRENT_ASSET_ELEMENTS = ("NoncurrentAssets",)
NONCURRENT_LIABILITY_ELEMENTS = ("NoncurrentLiabilities",)
TOTAL_LIABILITY_ELEMENTS = ("Liabilities",)

TOTAL_PROFIT_ELEMENTS = ("ProfitLoss",)
NCI_PROFIT_ELEMENTS = ("ProfitLossAttributableToNoncontrollingInterests",)

FINANCE_INCOME_ELEMENTS = ("FinanceIncome", "InvestmentIncome",
                           "InterestIncomeOnFinancialAssets")
ASSOCIATE_PROFIT_ELEMENTS = (
    "ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod",
)


def apply_sign(field: Field, value: float) -> float:
    """Apply the field's sign convention. Returns the value to store."""
    if field.sign is Sign.FORCE_POSITIVE:
        return abs(value)
    return value


def cell(field: Field, year_offset: int) -> str:
    """
    Workbook cell for a field at a given year offset.

    year_offset 0 = FY-4 (column B) .. 4 = FY0 (column F).
    """
    if not 0 <= year_offset <= 4:
        raise ValueError(f"year_offset must be 0..4, got {year_offset}")
    return f"{'BCDEF'[year_offset]}{field.row}"
