#!/usr/bin/env python3
"""
Screening pipeline  --  logic only, no output
=============================================

This module works out the answer. It does not display it.

WHY THE SPLIT
-------------
Interleaving the company loop, the policy decision, the terminal table
and the file writing works exactly once, for exactly one kind of output. A
GUI cannot reuse a print statement, and neither can a web page or a
scheduled job, so each new interface would re-derive the same decisions in
a new place and the copies would drift.

So: `screen_universe()` returns a `RunSummary`. `report.py` turns that into
terminal output. A GUI would render the same object; a server would
serialise it. The decisions about WHAT the answer is are made once, here.

Nothing in this module prints. Progress is reported through an optional
callback so the caller decides whether that becomes a progress bar, a log
line, or nothing at all. Diagnostics go to `logging`, which a GUI can route
to a window and a batch job to a file.

THE TWO OUTPUTS THAT MUST NOT BE CONFLATED
------------------------------------------
VERDICT is Valuation!D86, computed by the workbook and reported verbatim.
Never re-derived here: a parallel Python rule would eventually drift from
the workbook's, and then two numbers would disagree with no way to tell
which was right.

RESEARCH FLAG is this application's own filter and is strictly narrower:
undervalued AND no WARNING. It lives in screen_policy.json so it can be
tuned against real distributions without touching the engine.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from cache import Cache
from write_workbook import readiness, value_company

log = logging.getLogger(__name__)

# D86 calls a company fairly valued while upside sits within +/-25%. In
# PRICE terms that band is value/1.25 .. value/0.75, so the price at which
# a name would flip to UNDERVALUED is value / 1.25.
UNDERVALUED_THRESHOLD = 0.25

# The watch trigger is deliberately looser than the true flip point.
# Fair value is not independent of the price it judges: D34 measures
# leverage against market capitalisation, so as the price falls the weight
# shifts toward cheap after-tax debt and fair value RISES. Measured on the
# demo company, value moved from 11.4538 at a price of 24.50 to 11.9688 at
# 8.50 -- about +4.5%. A trigger computed from today's fair value therefore
# fires slightly LATE. Widening it to 1.20 buys back that lag; the re-run
# on crossing is what actually decides.
WATCH_MARGIN = 0.20

# Market-cap buckets, EUR millions. The 4,300 boundary is the credit-table
# cut-off from refdata_tables.json -- the one size threshold the model
# itself reacts to.
SIZE_BUCKETS = [(0, 300, "micro   < 300m"),
                (300, 2_000, "small   300m - 2bn"),
                (2_000, 4_300, "mid     2bn - 4.3bn"),
                (4_300, float("inf"), "large   > 4.3bn")]

SHORT_VERDICT = {
    "UNDERVALUED - investigate": "UNDERVALUED",
    "OVERVALUED - screen out": "OVERVALUED",
    "FAIRLY VALUED - no action": "FAIRLY VALUED",
    "VOID - failed validation": "VOID",
}

# Process warnings describe the PIPELINE, not the company. They fire for
# every company at once, so they can empty the research queue on a day when
# nothing is wrong with any company.
PROCESS_CHECKS = {"D121", "D122"}


def bucket_for(market_cap_m: float | None) -> str:
    if not isinstance(market_cap_m, (int, float)):
        return SIZE_BUCKETS[-1][2]
    for low, high, label in SIZE_BUCKETS:
        if low <= market_cap_m < high:
            return label
    return SIZE_BUCKETS[-1][2]


# ---------------------------------------------------------------------------
# The result objects
# ---------------------------------------------------------------------------


@dataclass
class CompanyResult:
    """One company's valuation, decision and provenance."""

    lei: str
    ticker: str
    name: str
    country: str
    industry: str
    exchange: str

    value_per_share: float | None
    current_price: float | None
    upside: float | None
    verdict: str

    wacc: float | None = None
    relevered_beta: float | None = None
    implied_rating: str | None = None
    terminal_share_of_ev: float | None = None
    margin_vs_industry: float | None = None
    roic_vs_industry: float | None = None
    equity_method_over_assets: float | None = None
    implied_ev_ebit: float | None = None

    credit_table: str = ""
    market_cap_m: float | None = None
    price_as_of: str | None = None
    price_age_days: int | None = None

    checks: list[dict] = field(default_factory=list)

    research_flag: bool = False
    qualifies_on_verdict: bool = False
    suppressed_by: list[str] = field(default_factory=list)
    # The checks that WOULD suppress the flag, whatever today's verdict.
    # A FAIRLY VALUED company can still carry one, and a between-screens
    # price move must not let it into the portfolio (see price_update.py).
    blocking_checks: list[str] = field(default_factory=list)
    suppressor_messages: list[str] = field(default_factory=list)
    reinvestment_ratio: float | None = None

    workbook_path: str = ""
    provenance: dict = field(default_factory=dict)

    # -- derived views -------------------------------------------------

    @property
    def short_verdict(self) -> str:
        label = SHORT_VERDICT.get(self.verdict, self.verdict[:14])
        if label == "UNDERVALUED" and self.suppressed_by:
            return "SUPPRESSED"
        return label

    @property
    def is_void(self) -> bool:
        return any(c["severity"] == "FAIL" for c in self.checks)

    def by_severity(self, severity: str) -> list[dict]:
        return [c for c in self.checks if c["severity"] == severity]

    @property
    def size_bucket(self) -> str:
        return bucket_for(self.market_cap_m)

    @property
    def has_findings(self) -> bool:
        """
        Void, flagged, or suppressed -- the rows worth reading.

        A FAIRLY VALUED company with no findings is the population, not the
        signal. It stays in results_DATE.xlsx and off the screen.
        """
        return self.is_void or self.research_flag or bool(self.suppressed_by)

    @property
    def trigger_price(self) -> float | None:
        """
        The price at which this name would read UNDERVALUED today.

        Fair value divided by 1.25. Useful on its own -- it says how far
        the market would have to move for the verdict to change -- and it
        is what a between-quarters price watch would compare against.
        """
        if not isinstance(self.value_per_share, (int, float)):
            return None
        return self.value_per_share / (1 + UNDERVALUED_THRESHOLD)

    @property
    def watch_price(self) -> float | None:
        """Trigger price widened for the price-feedback lag. See WATCH_MARGIN."""
        if not isinstance(self.value_per_share, (int, float)):
            return None
        return self.value_per_share / (1 + WATCH_MARGIN)

    @property
    def why(self) -> str:
        """
        Why this verdict, in one line, from cells already read.

        A bare 'OVERVALUED - screen out' cannot be argued with; you have to
        open the workbook to learn anything. Every input needed to explain
        it was already read, so saying it costs nothing.
        """
        bits = []
        if isinstance(self.wacc, (int, float)):
            rating = f" ({self.implied_rating})" if self.implied_rating else ""
            bits.append(f"WACC {self.wacc:.1%}{rating}")
        if isinstance(self.terminal_share_of_ev, (int, float)):
            bits.append(f"{self.terminal_share_of_ev:.0%} of value is terminal")
        if isinstance(self.margin_vs_industry, (int, float)):
            bits.append(f"margin {self.margin_vs_industry:.2f}x industry")
        if isinstance(self.roic_vs_industry, (int, float)):
            bits.append(f"ROIC {self.roic_vs_industry:.2f}x industry")
        return ", ".join(bits)


@dataclass
class RunSummary:
    """Everything one screening run produced."""

    generated_at: str
    valuation_date: str
    run_dir: Path
    policy: dict
    parameter_fingerprint: dict
    fiscal_years: list[int] = field(default_factory=list)
    recalc_engine: str = ""

    companies: list[CompanyResult] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    results_path: Path | None = None
    run_json_path: Path | None = None

    # -- aggregates ----------------------------------------------------

    @property
    def verdict_counts(self) -> Counter:
        return Counter(c.verdict or "(blank)" for c in self.companies)

    @property
    def undervalued(self) -> list[CompanyResult]:
        return [c for c in self.companies if c.qualifies_on_verdict]

    @property
    def research_queue(self) -> list[CompanyResult]:
        return [c for c in self.companies if c.research_flag]

    @property
    def findings(self) -> list[CompanyResult]:
        return [c for c in self.companies if c.has_findings]

    @property
    def suppression_counts(self) -> Counter:
        counts = Counter()
        for c in self.undervalued:
            for cell in c.suppressed_by:
                counts[cell] += 1
        return counts

    @property
    def suppression_rate(self) -> float:
        n = len(self.undervalued)
        return (n - len(self.research_queue)) / n if n else 0.0

    @property
    def fail_reasons(self) -> Counter:
        counts = Counter()
        for c in self.companies:
            for chk in c.by_severity("FAIL"):
                counts[chk["cell"]] += 1
        return counts

    @property
    def check_text(self) -> dict[str, str]:
        """
        A readable message per check cell.

        Must come from a company where the check actually FIRED. Most
        companies pass most checks, so the first message seen for a cell
        is almost always the literal string "OK" -- which would give run
        summary lines like "14 of 41 (34%) D103 OK", saying nothing at all.
        """
        out: dict[str, str] = {}
        for c in self.companies:
            for chk in c.checks:
                cell, message = chk["cell"], str(chk["message"] or "")
                fired = chk["severity"] in ("FAIL", "WARNING", "NOTE")
                if fired and not message.strip().upper().startswith("OK"):
                    out[cell] = message
                elif cell not in out:
                    out[cell] = message
        return out

    @property
    def by_size(self) -> dict[str, Counter]:
        out = {label: Counter() for _, _, label in SIZE_BUCKETS}
        for c in self.companies:
            out[c.size_bucket][c.short_verdict] += 1
        return out

    @property
    def stale_prices(self) -> list[CompanyResult]:
        """
        Companies valued against a quote more than a week old.

        The workbook has no check for a stale price, so nothing downstream
        will mention it unless this does.
        """
        return [c for c in self.companies
                if isinstance(c.price_age_days, int) and c.price_age_days > 7]

    def to_dict(self) -> dict:
        """Serialisable summary -- run_DATE.json, and any future web view."""
        return {
            "generated_at": self.generated_at,
            "valuation_date": self.valuation_date,
            "fiscal_years": self.fiscal_years,
            "recalc_engine": self.recalc_engine,
            "parameter_fingerprint": self.parameter_fingerprint,
            "policy": self.policy,
            "counts": dict(self.verdict_counts),
            "could_not_be_valued": len(self.skipped),
            "skipped": [{"lei": lei, "reason": why} for lei, why in self.skipped],
            "undervalued": len(self.undervalued),
            "research_queue": [c.ticker for c in self.research_queue],
            "held_back_by": dict(self.suppression_counts),
            "suppression_rate": round(self.suppression_rate, 4),
            # Keyed by LEI: a ticker is only unique on its own exchange, so
            # two companies can share one. Readers: track.iter_triggers().
            "triggers": {
                c.lei: {
                    "lei": c.lei,
                    "ticker": c.ticker,
                    "value_per_share": c.value_per_share,
                    "price": c.current_price,
                    "trigger_price": c.trigger_price,
                    "watch_price": c.watch_price,
                    # The screen's own verdict, so a consumer of this file
                    # (track.py) records what was actually decided instead
                    # of re-deriving it from value_per_share/price with its
                    # own copy of UNDERVALUED_THRESHOLD.
                    "verdict": c.verdict,
                    "research_flag": c.research_flag,
                    # Whether the model could value it, and which checks
                    # would hold back a research flag whatever the verdict --
                    # what the between-screens price update (price_update.py)
                    # needs to decide an entry without re-running the
                    # workbook.
                    "void": c.is_void,
                    "blocking_checks": c.blocking_checks,
                }
                for c in self.companies
                if c.trigger_price is not None
            },
        }


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def reinvestment_ratio(result: dict) -> float | None:
    """
    How far apart the two reinvestment estimates are, as a multiple.

    D30, the workbook's own measure, is |a-b| / max(|a|,|b|), which is
    bounded near 1 and so cannot distinguish a 20% disagreement from a
    fivefold one. The ratio of larger to smaller can.

    OPPOSITE SIGNS ARE A CONTRADICTION, NOT A MAGNITUDE GAP. Taking abs()
    of both before dividing would score a cash-flow estimate of -30%
    against a sales-to-capital estimate of +49% at 1.63 -- a mild
    disagreement -- and pass the graded rule into the research queue.
    But one estimate says the company reinvests half its NOPAT and the
    other says it is liquidating capital. There is no magnitude at which
    those are reconcilable, so the divergence is infinite by construction
    and the check must always suppress.
    """
    a = result.get("reinvest_cashflow")
    b = result.get("reinvest_sales_to_capital")
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return None
    if a * b < 0:
        return float("inf")
    hi, lo = max(abs(a), abs(b)), min(abs(a), abs(b))
    if lo < 1e-9:
        return float("inf") if hi > 1e-9 else 1.0
    return hi / lo


def graded_pass(ratio: float | None, cell: str, policy: dict) -> bool:
    """
    True if a graded check should be ignored at this magnitude.

    A check firing on 77% of companies is describing the population, not
    flagging an exception -- but at the extreme it still means something.
    Grading keeps the tail and drops the noise.
    """
    rule = (policy["research_flag"].get("graded_checks") or {}).get(cell)
    if not rule or rule.get("metric") != "reinvestment_ratio" or ratio is None:
        return False
    return ratio < float(rule.get("suppress_above", 3.0))


def apply_policy(result: dict, policy: dict) -> dict:
    """Decide the research flag and record exactly why."""
    cfg = policy["research_flag"]
    verdict = str(result.get("verdict") or "").strip()
    suppressing = {s.upper() for s in cfg["suppress_on_severity"]}
    exempt = set(cfg.get("exempt_checks") or [])
    ratio = reinvestment_ratio(result)

    qualifies = verdict == cfg["require_verdict"]
    suppressors = [c for c in result["checks"]
                   if c["severity"] in suppressing
                   and c["cell"] not in exempt
                   and not graded_pass(ratio, c["cell"], policy)]
    return {
        "qualifies_on_verdict": qualifies,
        "research_flag": qualifies and not suppressors,
        # Only meaningful when the verdict qualified: a warning on a company
        # that was never undervalued is not a suppression.
        "suppressed_by": [c["cell"] for c in suppressors] if qualifies else [],
        "suppressor_messages": [c["message"] for c in suppressors]
                               if qualifies else [],
        "blocking_checks": [c["cell"] for c in suppressors],
        "reinvestment_ratio": ratio,
    }


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def fingerprint(*paths: Path) -> dict:
    """
    SHA-256 of each parameter store.

    The sidecar records parameter VALUES but not which version of the store
    produced them. Without a hash you cannot prove, a year later, what a
    historical valuation was actually built on -- which is most of the
    point of keeping a provenance record at all.
    """
    out = {}
    for path in paths:
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            out[Path(path).name] = digest[:16]
        except OSError:
            out[Path(path).name] = None
    return out


def build_sidecar(result: CompanyResult, db: Cache, params: dict,
                  years: list[int], summary: RunSummary) -> dict:
    """Everything needed to defend or reproduce this valuation."""
    price = db.latest_price(result.lei) or {}
    shares = db.latest_share_count(result.lei) or {}
    listing = db.primary_listing(result.lei) or {}
    mw = params["market_wide"]

    return {
        "run": {
            "generated_at": summary.generated_at,
            "recalc_engine": summary.recalc_engine,
            "workbook": result.workbook_path,
            "parameter_fingerprint": summary.parameter_fingerprint,
        },
        "company": {
            "lei": result.lei, "ticker": result.ticker, "name": result.name,
            "country": result.country, "industry": result.industry,
            "fiscal_years": years,
        },
        "market_inputs": {
            "price": price.get("price"), "price_as_of": price.get("as_of"),
            "price_age_days": result.price_age_days,
            "price_source": price.get("source"),
            "price_symbol": price.get("symbol"), "price_mic": price.get("mic"),
            "listing_confidence": listing.get("confidence"),
            "listing_is_primary": bool(listing.get("is_primary")),
            "shares": shares.get("shares"), "shares_source": shares.get("source"),
            "shares_as_of": shares.get("as_of"),
            "market_cap_millions": result.market_cap_m,
        },
        "parameters": {
            "risk_free_rate": mw["risk_free_rate"]["value"],
            "risk_free_as_of": mw["risk_free_rate"]["as_of"],
            "mature_market_erp": mw["mature_market_erp"]["value"],
            "long_run_nominal_growth": mw["long_run_nominal_growth"]["value"],
            "country": params["countries"].get(result.country or ""),
            "industry": params["industries"].get(result.industry),
        },
        "credit_table": {"active": result.credit_table},
        "statements": result.provenance,
        "results": {
            "value_per_share": result.value_per_share,
            "current_price": result.current_price,
            "upside": result.upside,
            "verdict": result.verdict,
            "trigger_price": result.trigger_price,
            "watch_price": result.watch_price,
            "wacc": result.wacc,
            "relevered_beta": result.relevered_beta,
            "implied_rating": result.implied_rating,
            "terminal_share_of_ev": result.terminal_share_of_ev,
            "margin_vs_industry": result.margin_vs_industry,
            "roic_vs_industry": result.roic_vs_industry,
            "equity_method_over_assets": result.equity_method_over_assets,
            "implied_ev_ebit": result.implied_ev_ebit,
        },
        "checks": result.checks,
        "decision": {
            "verdict": result.verdict,
            "qualifies_on_verdict": result.qualifies_on_verdict,
            "research_flag": result.research_flag,
            "suppressed_by": result.suppressed_by,
            "suppressor_messages": result.suppressor_messages,
            "reinvestment_ratio": result.reinvestment_ratio,
        },
    }


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

ProgressFn = Callable[[int, int, "CompanyResult | None", str], None]


def eligible_universe(db: Cache, params: dict, years: list[int]) -> list[str]:
    """LEIs that are complete, not excluded, and have every prerequisite."""
    excluded = params.get("excluded_companies", {})
    return [lei for lei in db.complete_entities(years)
            if lei not in excluded
            and not readiness(lei, db, params, years)]


def screen_universe(db: Cache, params: dict, tables: dict, policy: dict,
                    template: Path, run_dir: Path, years: list[int],
                    engine: str = "auto", limit: int | None = None,
                    param_paths: tuple[Path, ...] = (),
                    on_progress: ProgressFn | None = None) -> RunSummary:
    """
    Value every ready company and decide the research flag for each.

    Returns a RunSummary. Prints nothing: pass `on_progress` if the caller
    wants to show progress, and read `summary` afterwards for everything
    else.

    Raises RecalcError if no recalculation engine is available -- that is
    not a per-company problem and continuing would produce a run of empty
    results.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = RunSummary(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        valuation_date=date.today().isoformat(),
        run_dir=run_dir,
        policy=policy["research_flag"],
        parameter_fingerprint=fingerprint(*param_paths),
        fiscal_years=list(years),
    )

    ready = eligible_universe(db, params, years)
    if limit:
        ready = ready[:limit]
    total = len(ready)
    log.info("screening %d companies", total)

    fy0 = max(years)
    for i, lei in enumerate(ready, 1):
        try:
            raw = value_company(lei, db, params, tables, template,
                                run_dir, years, engine)
        except (ValueError, KeyError, TypeError) as e:
            # TypeError is caught deliberately: a malformed cached value
            # reaching float() must skip one company, not kill the batch.
            log.warning("%s skipped: %s", lei, e)
            summary.skipped.append((lei, str(e)))
            if on_progress:
                on_progress(i, total, None, lei)
            continue

        cy = db.get(lei, fy0)
        listing = db.primary_listing(lei) or {}
        decision = apply_policy(raw, policy)

        result = CompanyResult(
            lei=lei, ticker=raw["ticker"],
            name=(cy.name if cy else lei) or lei,
            country=(cy.country if cy else "") or "",
            industry=raw["industry"],
            exchange=listing.get("exchange") or "",
            value_per_share=raw["value_per_share"],
            current_price=raw["current_price"],
            upside=raw["upside"],
            verdict=str(raw.get("verdict") or "").strip(),
            wacc=raw["wacc"], relevered_beta=raw["relevered_beta"],
            implied_rating=raw["implied_rating"],
            terminal_share_of_ev=raw["terminal_share_of_ev"],
            margin_vs_industry=raw["margin_vs_industry"],
            roic_vs_industry=raw["roic_vs_industry"],
            equity_method_over_assets=raw["equity_method_over_assets"],
            implied_ev_ebit=raw["implied_ev_ebit"],
            credit_table=raw["credit_table"],
            market_cap_m=raw["market_cap_m"],
            price_as_of=raw.get("price_as_of"),
            price_age_days=raw.get("price_age_days"),
            checks=raw["checks"],
            workbook_path=raw["file"],
            provenance=raw["provenance"],
            **decision,
        )
        summary.recalc_engine = raw["recalc_engine"]
        summary.companies.append(result)

        sidecar = build_sidecar(result, db, params, years, summary)
        Path(result.workbook_path).with_suffix(".provenance.json").write_text(
            json.dumps(sidecar, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8")

        if on_progress:
            on_progress(i, total, result, lei)

    return summary
