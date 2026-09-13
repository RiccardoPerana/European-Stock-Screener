#!/usr/bin/env python3
"""
ESEF field extractor
====================

Turns an ESEF filing into 20 numbers, or into an honest failure.

Reads xBRL-JSON from filings.xbrl.org, resolves each field in fields.py
against candidate IFRS taxonomy elements, and writes the result to the
company-year cache with full provenance.

THE THREE HARD PARTS
--------------------
Almost all extraction error lives here, so each is handled explicitly:

1. PERIOD SELECTION. A filing contains the current year AND the comparative
   prior year, often plus interim periods. Picking the wrong one gives a
   plausible number for the wrong year, which no downstream check catches.
   We match income and cash-flow facts to an annual DURATION ending on the
   fiscal year end, and balance-sheet facts to the INSTANT at that date.

2. DIMENSIONAL FACTS. Revenue is tagged once as a consolidated total and
   again per operating segment, per geography, per product line. They share
   a concept and a period. Taking the first match would silently return one
   segment. Only facts carrying no taxonomy dimensions are consolidated
   totals, so everything dimensionally qualified is discarded.

3. EXTENSION ELEMENTS. Companies may define their own concepts. We cannot
   resolve those without label matching, so they are left unresolved rather
   than guessed at. This is the main reason the real D111 pass rate will sit
   below the 426 the coverage probe reported.

WHAT IT DOES NOT DO
-------------------
No guessing. A field that does not resolve stays None. Optional fields
become 0.0 only via the cache's fill_permitted_zeros(), which records the
assumption as `assumed_zero` so you can count it.

USAGE
-----
    # first, refresh the coverage cache so it carries filing URLs
    python esef_coverage.py --refresh

    # then extract, starting small
    python esef_extract.py --limit 30
    python esef_extract.py --limit 30 --ebit-fallback
    python esef_extract.py                       # all complete entities
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from cache import Cache, CompanyYear
from fields import (
    ALL_FIELDS,
    ASSOCIATE_PROFIT_ELEMENTS,
    FINANCE_INCOME_ELEMENTS,
    NCI_PROFIT_ELEMENTS,
    NONCURRENT_ASSET_ELEMENTS,
    NONCURRENT_LIABILITY_ELEMENTS,
    TOTAL_LIABILITY_ELEMENTS,
    TOTAL_PROFIT_ELEMENTS,
    Field,
)

import config


def normalise(name: str) -> str:
    """Lowercase, letters and digits only, for pattern matching."""
    return "".join(ch for ch in name.lower() if ch.isalnum())

EXTRACTOR_VERSION = "esef-1.0.0"

# Dimensions defined by the OIM itself. Anything else in a fact's dimension
# map is a taxonomy axis (segment, geography, class of asset...), which means
# the fact is a breakdown rather than a consolidated total.
CORE_DIMENSIONS = frozenset(
    {"concept", "entity", "period", "unit", "language", "noteId"}
)

# An annual period, allowing for 52/53-week fiscal years and short stubs
# around a year-end change.
MIN_ANNUAL_DAYS = 350
MAX_ANNUAL_DAYS = 380

USER_AGENT = "esef-screening-tool/0.6 (research; non-commercial)"

# Positive evidence that a filer really is a bank, insurer or property
# company. Required in addition to the missing current/non-current split,
# because plenty of ordinary industrials simply never tag the CurrentAssets
# and CurrentLiabilities SUBTOTALS even though their balance sheet is
# classified -- IAS 1 requires the split, not the tagged subtotal.
#
# The asymmetry is deliberate. A financial that slips through becomes
# `partial` and is dropped anyway, costing nothing. An industrial wrongly
# excluded is silently gone from the universe, and nobody ever looks for it.
# So this demands proof rather than absence of proof.
FINANCIAL_MARKER_ELEMENTS = (
    "LoansAndAdvancesToCustomers", "LoansAndAdvancesToBanks",
    "DepositsFromCustomers", "DepositsFromBanks",
    "InterestRevenueCalculatedUsingEffectiveInterestMethod",
    "FinancialAssetsAtAmortisedCost", "FinancialAssetsAtFairValueThroughProfitOrLoss",
    "InsuranceContractLiabilities", "InsuranceRevenue",
    "LiabilitiesUnderInsuranceContractsAndReinsuranceContractsIssued",
    "InvestmentProperty", "RentalIncomeFromInvestmentProperty",
)

# A company holding inventories is running a trading or manufacturing
# operation, not a balance sheet of financial assets. This single negative
# marker is what separates Kesko and Fnac Darty from a bank.
NON_FINANCIAL_MARKER_ELEMENTS = ("Inventories", "CurrentInventories")

# Crude Section 3.2 prefilter on entity name.
#
# The archive carries no industry classification, so until the LEI -> ticker
# lookup table brings a GICS/ICB code with it, this is the only screen
# available. It is a FIRST LINE, not the policy: it will miss a bank named
# after its founder and will wrongly catch an industrial with "Holding" in
# its name. Every drop is listed so you can eyeball them.
#
# The workbook values the firm and bridges to equity, which is meaningless
# for a bank: it subtracts deposits as debt, nets out securities that are
# most of the balance sheet, and treats interest as financing when for a
# bank it is operating. The whole D33:D47 block measures something that
# does not exist.
EXCLUDE_NAME_PATTERNS = (
    "bank", "banca", "banque", "bancaire", "sparkasse", "raiffeisen",
    "kreditbank", "hypo", "bausparkasse", "landesbank", "volksbank",
    "insurance", "assicura", "assurance", "versicherung", "verzekering",
    "reinsurance", "ruck", "leasing",
    "immobilien", "immobiliare", "immobiliere", "real estate", "reit",
    "vastgoed", "properties", "property",
    "sicav", "sicaf", "investment fund", "capital partners",
    "sporitel", "zavarovaln", "rockcastle", "pension", "pankki",
    "asset management", "credito", "credit mutuel", "mediobanca",
    "paribas", "natixis", "bpce", "groep", "amundi", "coface", "allfunds",
    "euronext", "tikehau", "eurazeo", "osuuskunta", "finnvera", "taaleri",
    "capman", "aegon", "sampo", "kommunalkredit", "bpifrance", "hsbc",
    "klepierre", "gecina", "icade", "covivio", "unibail", "mercialys",
    "citycon", "kojamo", "vastned", "retail estates", "ascencio",
    "wereldhave", "plaza centers", "paref", "tour eiffel", "s immo",
    "finanzierungs", "funding", "finance b.v", "finance bv",
)
FETCH_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class XFact:
    """One numeric, undimensioned fact from a filing."""

    concept: str          # "ifrs-full:Revenue"
    prefix: str
    local_name: str
    value: float
    unit: str | None
    start: date | None    # None for instants
    end: date             # effective reporting date
    is_duration: bool

    @property
    def days(self) -> int | None:
        return (self.end - self.start).days if self.start else None

    @property
    def is_ifrs(self) -> bool:
        """False for company extension elements."""
        return self.prefix.startswith("ifrs")


def _parse_period(raw: str) -> tuple[date | None, date, bool]:
    """
    Parse an OIM period into (start, effective_end, is_duration).

    XBRL end instants are exclusive: a 31 December year end is written as
    the following 1 January at midnight. We normalise that back to the
    reporting date, otherwise every fiscal year is off by a day and nothing
    matches.
    """
    def to_date(token: str) -> tuple[date, bool]:
        token = token.strip()
        try:
            dt = datetime.fromisoformat(token.replace("Z", "+00:00"))
        except ValueError:
            # Unparseable as a datetime; fall back to the leading date.
            # Still treated as exclusive, since OIM period endpoints always
            # are -- returning False here would put the year end a day early.
            return date.fromisoformat(token[:10]), True
        midnight = dt.hour == dt.minute == dt.second == 0
        return dt.date(), midnight

    if "/" in raw:
        left, right = raw.split("/", 1)
        start, _ = to_date(left)
        end, end_midnight = to_date(right)
        if end_midnight:
            end -= timedelta(days=1)
        return start, end, True

    end, end_midnight = to_date(raw)
    if end_midnight:
        end -= timedelta(days=1)
    return None, end, False


class FactIndex:
    """Queryable view over the undimensioned numeric facts in one filing."""

    def __init__(self, document: dict) -> None:
        self.by_name: dict[str, list[XFact]] = defaultdict(list)
        self.extensions: set[str] = set()
        self.dimensional_names: set[str] = set()
        """Local names seen ONLY on dimensionally-qualified facts. Share
        counts are often broken down by class of share, so a concept absent
        from by_name may still be present in the filing as a breakdown."""
        self.skipped_dimensional = 0
        self.skipped_nonnumeric = 0

        for fact in (document.get("facts") or {}).values():
            dims = fact.get("dimensions") or {}

            if set(dims) - CORE_DIMENSIONS:
                self.skipped_dimensional += 1
                concept = dims.get("concept") or ""
                self.dimensional_names.add(concept.partition(":")[2] or concept)
                continue

            concept = dims.get("concept")
            period = dims.get("period")
            if not concept or not period:
                continue

            raw = fact.get("value")
            if raw is None or raw == "":
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                # Text blocks, dates, booleans. Not our concern.
                self.skipped_nonnumeric += 1
                continue

            prefix, _, local = concept.partition(":")
            if not local:
                prefix, local = "", concept

            try:
                start, end, is_duration = _parse_period(period)
            except (ValueError, IndexError):
                continue

            xf = XFact(concept=concept, prefix=prefix, local_name=local,
                       value=value, unit=dims.get("unit"),
                       start=start, end=end, is_duration=is_duration)
            self.by_name[local].append(xf)
            if not xf.is_ifrs:
                self.extensions.add(concept)

    # -- lookups -------------------------------------------------------

    def annual(self, local_name: str, period_end: date) -> XFact | None:
        """Annual-duration fact for a concept, ending on the fiscal year end."""
        candidates = [
            f for f in self.by_name.get(local_name, [])
            if f.is_duration
            and f.end == period_end
            and f.days is not None
            and MIN_ANNUAL_DAYS <= f.days <= MAX_ANNUAL_DAYS
        ]
        return self._best(candidates)

    def instant(self, local_name: str, period_end: date) -> XFact | None:
        """Instant fact for a concept, at the fiscal year end."""
        candidates = [
            f for f in self.by_name.get(local_name, [])
            if not f.is_duration and f.end == period_end
        ]
        return self._best(candidates)

    @staticmethod
    def _best(candidates: list[XFact]) -> XFact | None:
        """
        Choose among duplicates.

        Prefer a proper IFRS concept over an extension, and a EUR-denominated
        fact over anything else. A filing occasionally carries the same
        concept in two units (per-share and absolute, say).
        """
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda f: (
                not f.is_ifrs,
                not (f.unit or "").endswith("EUR"),
            ),
        )[0]

    def annual_period_ends(self) -> Counter:
        """End dates of annual-duration facts, by fact count."""
        return Counter(
            f.end for f in self._all()
            if f.is_duration and f.days is not None
            and MIN_ANNUAL_DAYS <= f.days <= MAX_ANNUAL_DAYS
        )

    def instant_dates(self) -> Counter:
        return Counter(f.end for f in self._all() if not f.is_duration)

    def infer_period_end(self, hint: date | None = None,
                         min_facts: int = 5) -> date | None:
        """
        Work out which date this filing actually reports on.

        The coverage cache derives its reporting_date from the package
        filename, and falls back to the library's `last_end_date` when the
        filename carries no ISO date -- which is common, since real packages
        are named things like "..._31.12.2023.json". `last_end_date` is the
        end of the LAST period anywhere in the report, including
        forward-looking facts in the notes, so it is often not the year end.

        If that date is off by even one day, nothing matches and every
        field comes back unresolved. So the document decides, and the hint
        only breaks ties.

        Both the current year and the comparative year appear as annual
        durations, so we take the latest with enough facts to be a primary
        statement rather than a stray disclosure.
        """
        candidates = [d for d, n in self.annual_period_ends().items()
                      if n >= min_facts]
        if not candidates:
            return None
        if hint is not None:
            near = [d for d in candidates if abs((d - hint).days) <= 45]
            if near:
                return min(near, key=lambda d: abs((d - hint).days))
        return max(candidates)

    def _all(self):
        for facts in self.by_name.values():
            yield from facts


# ---------------------------------------------------------------------------
# Field resolution
# ---------------------------------------------------------------------------

@dataclass
class Resolution:
    value: float | None
    derivation: str
    element: str | None = None
    components: list[str] = None

    def __post_init__(self):
        if self.components is None:
            self.components = []


def resolve_field(index: FactIndex, f: Field, period_end: date) -> Resolution:
    """Resolve one field, trying candidates in priority order."""
    lookup = index.instant if f.statement == "balance" else index.annual

    for element in f.elements:
        hit = lookup(element, period_end)
        if hit is not None:
            # Local-name matching also catches company extensions such as
            # andritz:EarningsBeforeInterestAndTaxesEbit. Worth having --
            # that IS the company's operating profit -- but it is a weaker
            # claim than a standard IFRS tag, so it is recorded separately
            # and stays countable.
            return Resolution(hit.value,
                              "reported" if hit.is_ifrs else "extension",
                              element=hit.concept)

    if f.sum_of:
        total = 0.0
        found: list[str] = []
        extension_used = False
        for group in f.sum_of:
            # One hit per component group. Alternate spellings of the same
            # cash flow live in the same group, so they cannot both be added.
            for element in group:
                hit = lookup(element, period_end)
                if hit is not None:
                    total += hit.value
                    found.append(hit.concept)
                    extension_used |= not hit.is_ifrs
                    break
        if found:
            # A partial sum is still recorded, with its components listed,
            # so a missing lease liability is visible rather than invisible.
            return Resolution(total, "extension" if extension_used else "summed",
                              components=found)
        # Nothing summed: fall through to the pattern tier rather than
        # returning here, or a field with both sum_of and pattern_require
        # never reaches its last resort.

    if f.pattern_require:
        matches = []
        for local, facts in index.by_name.items():
            norm = normalise(local)
            if not all(p in norm for p in f.pattern_require):
                continue
            if f.pattern_any and not any(p in norm for p in f.pattern_any):
                continue
            if any(d in norm for d in f.pattern_deny):
                continue
            hit = lookup(local, period_end)
            if hit is not None:
                matches.append(hit)
        if f.pattern_prefer and matches:
            top = [m for m in matches
                   if all(p in normalise(m.local_name) for p in f.pattern_prefer)]
            if top:
                matches = top
        # Rank the matches, then require the best tier to be unambiguous.
        # Two equally-good matches could be a partition to add up or the
        # same figure counted twice, and guessing is worse than a gap.
        def has_all(hit, terms):
            norm = normalise(hit.local_name)
            return all(t in norm for t in terms)

        def has_any(hit, terms):
            norm = normalise(hit.local_name)
            return any(t in norm for t in terms)

        tiers = []
        if f.pattern_prefer:
            clean = [m for m in matches if has_all(m, f.pattern_prefer)
                     and not has_any(m, f.pattern_demote)]
            tiers.append(clean)
            tiers.append([m for m in matches if has_all(m, f.pattern_prefer)])
        tiers.append(matches)

        for tier in tiers:
            if len(tier) == 1:
                return Resolution(tier[0].value, "extension",
                                  element=tier[0].concept)

    return Resolution(None, "reported")


def derive_exact(cy: CompanyYear, index: "FactIndex", period_end: date) -> None:
    """
    Rebuild net income and pre-tax income from lines that ARE tagged.

    Two gaps show up repeatedly and both are arithmetic, not estimation:

    NET INCOME. Every filing tags ProfitLoss (total, including minorities),
    but many never tag ProfitLossAttributableToOwnersOfParent. Row 26 wants
    the parent share, which is ProfitLoss less the minority share. Where no
    minority profit is tagged anywhere, the company has none and the total
    IS the parent share.

    PRE-TAX INCOME. Where ProfitLossBeforeTax is absent but ProfitLoss and
    the tax charge are present, pre-tax is their sum by construction.

    Both are recorded as `computed` so they stay countable, but neither is
    an approximation the way the EBIT fallback is.
    """
    def annual(names):
        for n in names:
            hit = index.annual(n, period_end)
            if hit is not None:
                return hit
        return None

    total = annual(TOTAL_PROFIT_ELEMENTS)

    net = cy.facts.get("net_income")
    if (net is None or net.value is None) and total is not None:
        nci = annual(NCI_PROFIT_ELEMENTS)
        cy.set("net_income", total.value - (nci.value if nci else 0.0),
               "computed", element=total.concept,
               components=[nci.concept] if nci else [])

    pretax = cy.facts.get("pretax_income")
    tax = cy.facts.get("tax_expense")
    if (pretax is None or pretax.value is None) and total is not None \
            and tax is not None and tax.value is not None:
        cy.set("pretax_income", total.value + tax.value, "computed",
               element=total.concept)

    # IAS 1 requires the current/non-current SPLIT, not a tagged SUBTOTAL.
    # Retailers and industrials routinely present a classified balance sheet
    # and never tag the total lines -- Kesko and Fnac Darty both do this.
    # Both subtotals fall out of the identity Assets = Equity + Liabilities,
    # so they are arithmetic, not estimation.
    def instant(names):
        for n in names:
            hit = index.instant(n, period_end)
            if hit is not None:
                return hit
        return None

    assets = cy.facts.get("total_assets")
    equity = cy.facts.get("total_equity")
    noncurrent_assets = instant(NONCURRENT_ASSET_ELEMENTS)
    noncurrent_liabs = instant(NONCURRENT_LIABILITY_ELEMENTS)

    tca = cy.facts.get("total_current_assets")
    if (tca is None or tca.value is None) and noncurrent_assets is not None \
            and assets is not None and assets.value is not None:
        cy.set("total_current_assets", assets.value - noncurrent_assets.value,
               "computed", element=noncurrent_assets.concept,
               components=["Assets"])

    tcl = cy.facts.get("total_current_liabilities")
    if (tcl is None or tcl.value is None) and noncurrent_liabs is not None:
        liabilities = instant(TOTAL_LIABILITY_ELEMENTS)
        if liabilities is not None:
            cy.set("total_current_liabilities",
                   liabilities.value - noncurrent_liabs.value, "computed",
                   element=liabilities.concept,
                   components=[noncurrent_liabs.concept])
        elif assets is not None and assets.value is not None \
                and equity is not None and equity.value is not None:
            cy.set("total_current_liabilities",
                   assets.value - equity.value - noncurrent_liabs.value,
                   "computed", element="Assets-Equity",
                   components=[noncurrent_liabs.concept])


def derive_debt_proxy(cy: CompanyYear, index: "FactIndex",
                      period_end: date) -> None:
    """
    Fill long-term debt from total non-current liabilities when the
    borrowings themselves were never tagged.

    WHY THIS IS NEEDED
    ------------------
    An audit of 170 companies found 38 that pay interest and carry
    liabilities their balance sheet cannot place, while recording zero
    debt. A concept survey of those 38 filings found the balance-sheet
    borrowings absent in every one -- what IS tagged is the cash-flow
    activity: ProceedsFromBorrowings, RepaymentsOfBorrowings,
    PaymentsOfLeaseLiabilities. The companies plainly have borrowings; the
    stock is simply not detail-tagged.

    That is an ESEF structural limit rather than a gap in our element
    list. Detailed tagging is required for the primary statements, and
    many filers present non-current liabilities as one subtotal with the
    breakdown in the notes, which are block-tagged as text.

    WHY THIS PARTICULAR SUBSTITUTION
    --------------------------------
    Invested capital is debt + equity - cash, and since
    assets - current liabilities = equity + non-current liabilities, using
    total non-current liabilities as debt is ALGEBRAICALLY IDENTICAL to
    the correct figure whenever those liabilities are borrowings. On SWUT
    it reproduced the exact figure the missing EUR 36.7m implied.

    WHAT IT COSTS
    -------------
    It treats pensions, deferred tax and long-run provisions as debt.
    Those are not borrowings. But the error runs one way only, and it is
    the safe way: invested capital rises so ROIC falls, more is subtracted
    in the equity bridge, and leverage rises so WACC rises. Every one of
    those pushes the valuation DOWN. For a screen looking for bargains,
    overstating debt is the direction to be wrong in.

    Measured on SWUT: invested capital 41.4 -> 82.0, ROIC 26.6% -> 13.4%,
    growth 3.8% -> 1.9%, WACC 8.2% -> 8.9%, value per share 210.6 -> 122.8.

    It is recorded as `debt_proxy`, never `reported`, so no consumer can
    mistake it for a figure the company published.
    """
    st = cy.facts.get("short_term_debt")
    lt = cy.facts.get("long_term_debt")
    # Only when NEITHER resolved. A company that tagged its short-term
    # borrowings and not its long-term ones is a different problem, and
    # substituting the whole non-current block there would double-count.
    # `is not None` rather than truthiness: a company that reported a
    # genuine 0.0 balance is resolved, not missing -- `.value` alone would
    # be falsy for that 0.0 and let the proxy overwrite it with an
    # estimate.
    if (st is not None and st.value is not None) \
            or (lt is not None and lt.value is not None):
        return

    # A COMPANY THAT PAYS NO INTEREST IS TAKEN AT ITS WORD.
    #
    # Without this gate the proxy fires on genuinely debt-free companies
    # too, handing them their pension and provision balances as borrowings
    # -- and the strict_interest guard below then blocks them for not
    # paying interest on debt they do not have. A regression test caught
    # exactly that.
    #
    # Interest paid is the signal that separates the two cases. Borrowings
    # that exist but were not tagged still cost money every year, and that
    # cost IS reliably tagged: interest_expense was absent in only 4% of
    # the universe against 32% for long-term debt. So the proxy applies
    # where the income statement proves there is something to proxy for,
    # and nowhere else.
    interest = cy.facts.get("interest_expense")
    if interest is None or not (interest.value or 0) > 0:
        return

    def instant(names):
        for n in names:
            hit = index.instant(n, period_end)
            if hit is not None:
                return hit
        return None

    tagged = instant(NONCURRENT_LIABILITY_ELEMENTS)
    if tagged is not None and tagged.value is not None:
        cy.set("long_term_debt", tagged.value, "debt_proxy",
               element=tagged.concept)
        return

    # Not tagged either: fall back on the balance-sheet identity.
    assets = cy.facts.get("total_assets")
    equity = cy.facts.get("total_equity")
    curr = cy.facts.get("total_current_liabilities")
    if all(f is not None and f.value is not None
           for f in (assets, equity, curr)):
        residual = assets.value - equity.value - curr.value
        # A negative residual means the identity does not close, so the
        # inputs disagree and substituting anything would be guesswork.
        if residual > 0:
            cy.set("long_term_debt", residual, "debt_proxy",
                   element="Assets-Equity-CurrentLiabilities",
                   components=["Assets", "Equity", "CurrentLiabilities"])


def ebit_from_pretax(cy: CompanyYear, index: "FactIndex",
                     period_end: date) -> float | None:
    """
    Fallback EBIT: pre-tax income, with the financial result stripped out.

        EBIT = pre-tax income + finance costs - finance income
               - share of profit of associates

    IFRS mandates no operating-profit subtotal, so
    ProfitLossFromOperatingActivities is optional and heavily extended.
    But pre-tax income, finance costs, finance income and the associates
    line ARE reliably tagged, so the derivation rests on solid ground.

    It still is not the number the company reports: it folds in any
    non-operating item the company excluded from its own subtotal. So it
    is recorded as `computed` and off by default. Turn it on, measure what
    it buys, and report separately on the companies that relied on it.
    """
    pretax = cy.facts.get("pretax_income")
    if pretax is None or pretax.value is None:
        return None

    ebit = pretax.value
    interest = cy.facts.get("interest_expense")
    if interest and interest.value:
        ebit += interest.value
    for element in FINANCE_INCOME_ELEMENTS:
        hit = index.annual(element, period_end)
        if hit is not None:
            ebit -= abs(hit.value)
            break
    for element in ASSOCIATE_PROFIT_ELEMENTS:
        hit = index.annual(element, period_end)
        if hit is not None:
            ebit -= hit.value
            break
    return ebit


def likely_financial(index: "FactIndex", period_end: date, *,
                     total_current_assets: float | None,
                     total_current_liabilities: float | None,
                     total_assets: float | None,
                     total_equity: float | None) -> bool:
    """
    IAS 1 lets a bank or insurer present assets in order of liquidity
    instead of a current/non-current split. When that happens CurrentAssets
    and CurrentLiabilities genuinely do not exist, while Assets and Equity
    do. That combination is the balance-sheet heuristic the brief asks for
    as a second line behind name matching (EXCLUDE_NAME_PATTERNS), and it
    catches the financials whose names give nothing away.

    Standalone (not a CompanyYear method) so tools/sector_audit.py can
    call the exact same check the extractor uses -- against the four
    already-resolved fields for a company already in financials.db, or
    against fresh ones resolved from a re-fetched filing for a company the
    name filter dropped -- without re-implementing it and risking drift.
    """
    unclassified = (
        total_current_assets is None
        and total_current_liabilities is None
        and total_assets is not None
        and total_equity is not None
    )
    has_inventories = any(
        index.instant(e, period_end) is not None
        for e in NON_FINANCIAL_MARKER_ELEMENTS
    )
    has_financial_marker = any(
        index.instant(e, period_end) is not None
        or index.annual(e, period_end) is not None
        for e in FINANCIAL_MARKER_ELEMENTS
    )
    return unclassified and has_financial_marker and not has_inventories


def extract(document: dict, *, lei: str, fiscal_year: int, period_end: date,
            name: str | None, country: str | None, source_ref: str | None,
            ebit_fallback: bool = False,
            trust_hint: bool = False,
            strict_interest: bool = True) -> tuple[CompanyYear, FactIndex]:
    """
    Resolve all 20 fields from one parsed filing.

    `period_end` is a HINT from the coverage cache. The document's own
    annual periods override it unless trust_hint is set, because the cached
    date is derived from a filename and is frequently wrong.
    """
    index = FactIndex(document)
    resolved_end = period_end if trust_hint else (
        index.infer_period_end(hint=period_end) or period_end)

    cy = CompanyYear(
        lei=lei, fiscal_year=fiscal_year, source="esef",
        extractor_version=EXTRACTOR_VERSION, name=name, country=country,
        period_end=resolved_end.isoformat(), source_ref=source_ref,
    )
    period_end = resolved_end

    for f in ALL_FIELDS:
        r = resolve_field(index, f, period_end)
        cy.set(f.key, r.value, r.derivation, element=r.element,
               components=r.components)

    derive_exact(cy, index, period_end)
    derive_debt_proxy(cy, index, period_end)

    cy.likely_financial = likely_financial(
        index, period_end,
        total_current_assets=cy.facts["total_current_assets"].value,
        total_current_liabilities=cy.facts["total_current_liabilities"].value,
        total_assets=cy.facts["total_assets"].value,
        total_equity=cy.facts["total_equity"].value,
    )

    if ebit_fallback:
        ebit = cy.facts.get("ebit")
        if ebit is None or ebit.value is None:
            derived = ebit_from_pretax(cy, index, period_end)
            if derived is not None:
                cy.set("ebit", derived, "computed")

    cy.fill_permitted_zeros()

    # Interest expense is optional only for a company with no debt.
    #
    # Section 11: "Never invent or substitute financial data. If a required
    # input cannot be obtained reliably, fail before writing the workbook."
    # A zero here for a leveraged company is invented data, and it biases in
    # one direction every time: D39 reads EBIT/interest as coverage, so zero
    # interest means infinite coverage, an AAA rating, the narrowest spread,
    # a lower WACC and a higher value. The workbook cannot see it, because
    # the cell is populated and D111 counts 100.
    if strict_interest:
        debt = sum(
            cy.facts[k].value or 0.0
            for k in ("short_term_debt", "long_term_debt")
        )
        interest = cy.facts.get("interest_expense")
        if debt > 0 and interest is not None \
                and interest.derivation == "assumed_zero":
            cy.blocked.append(
                "interest expense assumed zero despite "
                f"{debt:,.0f} of debt")

        # And the mirror image: interest paid on debt that was never
        # found. This is a contradiction rather than a judgement call --
        # a company does not pay interest on borrowings it does not have
        # -- and it ran unchecked through 47 of 170 companies, every one
        # of which was valued as though unlevered. The proxy above should
        # now prevent it, so reaching here means even the non-current
        # liability subtotal was missing.
        if debt <= 0 and interest is not None and (interest.value or 0) > 0:
            cy.blocked.append(
                f"pays {interest.value:,.0f} of interest with no debt "
                f"found; borrowings are untagged and no non-current "
                f"liability subtotal was available to stand in")

    return cy, index


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def safe_url(url: str) -> str:
    """
    Percent-encode the path so filenames with spaces do not blow up.

    Real ESEF packages carry names like
    "HYPO NOE_ESEF-Jahresfinanzbericht_31.12.2023.json". http.client
    rejects control characters and spaces outright, so the path has to be
    quoted -- but with "%" left safe, or already-encoded URLs get
    double-encoded and 404.
    """
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc,
        urllib.parse.quote(parts.path, safe="/%~"),
        urllib.parse.quote(parts.query, safe="=&%"),
        parts.fragment,
    ))


def fetch_json(url: str) -> dict:
    """Download and parse one xBRL-JSON document."""
    req = urllib.request.Request(safe_url(url), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_filing_index(cache_dir: Path) -> dict[tuple[str, int], dict]:
    """
    Build a (LEI, year) -> filing record map from the coverage cache.

    Reuses the cache esef_coverage.py already wrote, so there is one notion
    of "which filings exist" rather than two that can disagree.
    """
    index: dict[tuple[str, int], dict] = {}
    files = sorted(cache_dir.glob("*.json"))
    if not files:
        sys.exit(f"No coverage cache in {cache_dir}. Run esef_coverage.py first.")

    missing_url = 0
    for path in files:
        for rec in json.loads(path.read_text()):
            if not rec.get("json_url"):
                missing_url += 1
                continue
            year = date.fromisoformat(rec["reporting_date"]).year
            key = (rec["lei"], year)
            try:
                entry = int(rec["filing_index"].rsplit("-", 1)[-1])
            except (ValueError, AttributeError):
                entry = -1
            if key not in index or entry > index[key]["_entry"]:
                index[key] = {**rec, "_entry": entry}

    if missing_url:
        print(f"WARNING: {missing_url:,} cached filings have no json_url.\n"
              f"         Re-run: python core/esef_coverage.py --refresh\n",
              file=sys.stderr)
    return index


def read_universe(csv_path: Path) -> list[dict]:
    """Complete entities from a coverage CSV, in file order."""
    with csv_path.open(encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r["complete"] == "YES"]


def apply_exclusions(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split the universe into (kept, excluded) on the name prefilter."""
    kept, dropped = [], []
    for r in rows:
        name = (r.get("name") or "").lower()
        if any(pat in name for pat in EXCLUDE_NAME_PATTERNS):
            dropped.append(r)
        else:
            kept.append(r)
    return kept, dropped


def stratified_sample(rows: list[dict], n: int, seed: int = 0) -> list[dict]:
    """
    Pick n entities spread across countries, round-robin.

    The coverage CSV is sorted by country, so plain head-of-list slicing
    returns 30 Austrian companies -- which tells you about Austria's
    tagging conventions and nothing about anybody else's. A test sample
    that is not spread across countries cannot measure the extractor.
    """
    import random
    by_country: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_country[r["country"]].append(r)

    rng = random.Random(seed)
    for bucket in by_country.values():
        rng.shuffle(bucket)

    out: list[dict] = []
    countries = sorted(by_country, key=lambda c: -len(by_country[c]))
    while len(out) < n and any(by_country[c] for c in countries):
        for c in countries:
            if by_country[c] and len(out) < n:
                out.append(by_country[c].pop())
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    if args.reset and args.db.exists():
        args.db.unlink()
        print(f"deleted {args.db}\n")

    universe = read_universe(args.csv)

    if not args.no_exclude:
        universe, dropped = apply_exclusions(universe)
        if dropped:
            print(f"Section 3.2 name prefilter dropped {len(dropped):,} of "
                  f"{len(dropped) + len(universe):,} entities:")
            for r in dropped[:15]:
                print(f"    {r['country']}  {r['name'][:56]}")
            if len(dropped) > 15:
                print(f"    ... and {len(dropped) - 15:,} more "
                      f"(--no-exclude to keep them)")
            print()

    if args.sample:
        universe = stratified_sample(universe, args.sample, args.seed)
    elif args.limit:
        universe = universe[: args.limit]

    filings = load_filing_index(args.cache_dir)

    print(f"{len(universe):,} entities x {len(args.years)} years "
          f"= {len(universe) * len(args.years):,} company-years\n")

    counts = Counter()
    with Cache(args.db) as db:
        for i, row in enumerate(universe, 1):
            lei, name = row["lei"], row["name"]
            wanted = args.years if args.refresh else db.missing_years(
                lei, args.years, EXTRACTOR_VERSION)
            if not wanted:
                counts["cached"] += len(args.years)
                continue

            print(f"[{i:>4}/{len(universe)}] {name[:44]:<44}", end="", flush=True)
            marks = []
            pending: list[tuple[int, "CompanyYear"]] = []
            for year in wanted:
                rec = filings.get((lei, year))
                if rec is None:
                    db.mark_failed(lei, year, "esef", EXTRACTOR_VERSION,
                                   "no filing in coverage cache")
                    counts["no_filing"] += 1
                    marks.append("-")
                    continue

                try:
                    doc = fetch_json(rec["json_url"])
                except (urllib.error.URLError, json.JSONDecodeError,
                        TimeoutError) as exc:
                    db.mark_failed(lei, year, "esef", EXTRACTOR_VERSION,
                                   f"{type(exc).__name__}: {exc}")
                    counts["fetch_failed"] += 1
                    marks.append("!")
                    continue

                cy, _ = extract(
                    doc, lei=lei, fiscal_year=year,
                    period_end=date.fromisoformat(rec["reporting_date"]),
                    name=name, country=row["country"],
                    source_ref=rec.get("filing_index"),
                    ebit_fallback=args.ebit_fallback,
                    strict_interest=not args.allow_zero_interest,
                )
                interest = cy.facts.get("interest_expense")
                debt = sum(
                    (cy.facts[k].value or 0.0)
                    for k in ("short_term_debt", "long_term_debt")
                    if cy.facts.get(k) is not None
                )
                if interest is not None \
                        and interest.derivation == "assumed_zero" and debt > 0:
                    counts["interest_zero_despite_debt"] += 1

                debt = sum(
                    cy.facts[k].value or 0.0
                    for k in ("short_term_debt", "long_term_debt")
                )
                interest = cy.facts.get("interest_expense")
                if debt > 0 and interest is not None \
                        and interest.derivation == "assumed_zero":
                    counts["debt_without_interest"] += 1

                pending.append((year, cy))

            # A genuine financial looks like one in every year. A single
            # flagged year among four normal ones is a tagging quirk, and
            # excluding the whole company on that basis would quietly delete
            # a valid name from the universe.
            flagged = [cy for _, cy in pending if cy.likely_financial]
            is_financial = (
                bool(pending) and len(flagged) == len(pending)
                and not args.no_exclude
            )
            if flagged and not is_financial:
                counts["mixed_financial_signal"] += 1

            for year, cy in pending:
                if is_financial:
                    db.mark_failed(lei, year, "esef", EXTRACTOR_VERSION,
                                   "liquidity-ordered balance sheet with "
                                   "financial-institution markers")
                    counts["financial"] += 1
                    marks.append("F")
                    continue
                db.put(cy)
                counts[cy.status] += 1
                marks.append("." if cy.status == "complete" else "x")

            print(" " + "".join(marks))

        print("\n" + "=" * 62)
        print("EXTRACTION SUMMARY")
        print("=" * 62)
        for key in ("complete", "partial", "financial", "cached",
                    "no_filing", "fetch_failed", "mixed_financial_signal",
                    "interest_zero_despite_debt"):
            if counts[key]:
                print(f"  {key:<16}{counts[key]:>8,}")

        print(f"\n{'field':<32}{'unresolved':>12}{'assumed 0':>12}")
        print("-" * 56)
        for key, unresolved, assumed in db.field_failure_report():
            if unresolved or assumed:
                print(f"{key:<32}{unresolved:>12,}{assumed:>12,}")

        usable = db.complete_entities(args.years)
        print(f"\nEntities with all {len(args.years)} years complete: "
              f"{len(usable):,} of {len(universe):,}")
        if counts["interest_zero_despite_debt"]:
            print(f"\nWARNING: {counts['interest_zero_despite_debt']:,} "
                  "company-years carry debt but no interest expense.")
            print("Zero interest means infinite coverage at Valuation!D39,")
            print("which rates them AAA and applies the cheapest spread.")
            print("These are not safe to value until the field resolves.")

        if counts["debt_without_interest"]:
            print(f"\nWARNING: {counts['debt_without_interest']:,} company-years "
                  "carry debt but no interest expense.\nD39 divides EBIT by "
                  "interest, so these resolve to AAA and get the narrowest\n"
                  "spread on the ladder. Understates WACC, overstates value.")

        print("\nHigh 'unresolved' counts point at fields needing more")
        print("taxonomy element candidates in fields.py. High 'assumed 0'")
        print("on a field that should rarely be zero is the same signal.")

    return 0


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Extract the 20 model fields from ESEF filings.",
        parents=[config.common_args(cache_dir=True)])
    p.add_argument("--csv", type=Path, default=None,
                   help="Coverage CSV (default: newest in ./out)")
    p.add_argument("--limit", type=int, default=None,
                   help="Process the first N entities in CSV order")
    p.add_argument("--sample", type=int, default=None,
                   help="Process N entities spread across countries "
                        "(preferred over --limit for testing)")
    p.add_argument("--seed", type=int, default=0,
                   help="Sample seed, for a reproducible test set")
    p.add_argument("--allow-zero-interest", action="store_true",
                   help="Keep company-years where interest expense was "
                        "assumed zero despite the company carrying debt. "
                        "They will be rated AAA. Off by default.")
    p.add_argument("--reset", action="store_true",
                   help="Delete the cache database first. Use after an "
                        "extractor change, so the failure report counts "
                        "only the current run.")
    p.add_argument("--no-exclude", action="store_true",
                   help="Skip the Section 3.2 name prefilter")
    p.add_argument("--refresh", action="store_true",
                   help="Re-extract even if already cached")
    p.add_argument("--ebit-fallback", action="store_true",
                   help="Derive EBIT as pre-tax + interest when untagged. "
                        "Approximate; recorded as 'computed'. Off by default.")
    args = p.parse_args(argv)

    if args.csv is None:
        found = sorted(Path("./out").glob("esef_coverage_*.csv"))
        if not found:
            p.error("No coverage CSV in ./out -- run esef_coverage.py first")
        args.csv = found[-1]
    if not args.csv.exists():
        p.error(f"{args.csv} not found")

    args.years = sorted(args.years)
    return args


def main(argv=None) -> int:
    """Entry point, named to match the other pipeline scripts."""
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
