#!/usr/bin/env python3
"""
Terminal report  --  display only, no logic
===========================================

Takes a RunSummary from pipeline.py and prints it. Every decision about
WHAT the answer is was made in pipeline.py; every decision about how it
LOOKS is made here. A GUI would replace this file and nothing else.

READING ORDER
-------------
Orientation first, answer last:

    1  how to read this        five lines, before anything scrolls past
    2  the policy in force
    3  progress                during the run, which takes minutes
    4  companies needing attention
    5  counts, size, why things voided
    6  files written
    7  RESEARCH QUEUE          last, because it is what you came for

The old order buried the legend at the very bottom, after four minutes of
output, and put the research queue sixth of eight. You cannot read a table
whose columns are explained below it.
"""

from __future__ import annotations

import sys

from config import strip_severity, utf8_stdout
from pipeline import PROCESS_CHECKS, SIZE_BUCKETS, RunSummary

BAR = "=" * 74
RULE = "-" * 74


def shorten(text: str, width: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# 1. Orientation
# ---------------------------------------------------------------------------

SHORT_GUIDE = """\
HOW TO READ THIS
  VALUE     what the model thinks one share is worth (Valuation!D83).
  UPSIDE    (value - price) / price. Beyond +/-25% the verdict changes.
  VERDICT   VOID means a check FAILED and the number must not be used --
            not even as a rough estimate.
  Only companies with findings are listed below; every company, plus its
  trigger price, is in results_DATE.xlsx.   Full reference:  --legend
"""

FULL_LEGEND = """
FULL LEGEND
===========

THE COLUMNS
  EXCH          Bloomberg code for the primary listing the price came from.
                  AV Vienna    BB Brussels   FH Helsinki   FP Paris
                  GY Xetra     IM Milan      LH Vilnius    NA Amsterdam
                  SM Madrid    PL Lisbon     LJ Ljubljana  and so on.
  PRICE         Valuation!D84, taken from Inputs!B14.
  VALUE         Valuation!D83. What the model thinks one share is worth.
  UPSIDE        Valuation!D85. (value - price) / price.
  TRIGGER       (results_DATE.xlsx only) Fair value divided by 1.25 --
                the price at which the
                verdict would flip to UNDERVALUED. It says how far the
                market would have to move for the answer to change.
                Note that fair value is not fixed as the price moves: D34
                measures leverage against market capitalisation, so a
                falling price shifts weight toward cheap debt and fair
                value rises slightly. The trigger therefore fires a little
                LATE, and a crossing should be confirmed by re-running the
                company rather than acted on directly.
  VERDICT       A short label for Valuation!D86. The full string is kept
                verbatim in results_DATE.xlsx and in every sidecar.
                  UNDERVALUED     upside above +25%, nothing holding it back
                  SUPPRESSED      upside above +25%, but a WARNING fired
                  OVERVALUED      upside below -25%
                  FAIRLY VALUED   in between
                  VOID            a check returned FAIL. The workbook will
                                  not stand behind the number, so it must
                                  not be used at all. Usually the company
                                  is loss-making, or an input was missing.

SEVERITIES INSIDE THE WORKBOOK
  FAIL     the valuation is void and must not be used
  WARNING  the result stands but is suspect; it suppresses the research flag
  NOTE     informational only -- a bound was binding, or a fallback was
           used. NOTE never suppresses anything.

PROCESS WARNINGS
  D121 and D122 are about the PIPELINE, not the company: the credit table
  being close to the size cut-off, or older than 14 months. They fire for
  every company at once, so they can empty the research queue on a day when
  nothing is wrong with any company. Exempt them in screen_policy.json if
  that is not what you want.

WHAT THE MODEL WILL NOT DO
  Terminal ROIC converges to WACC, so the model cannot recognise a durable
  competitive advantage and will mark quality compounders as overvalued.
  Growth comes from reinvestment x ROIC rather than realised history.
  Expect the UNDERVALUED bucket to skew capital-intensive and low-multiple.

FILES WRITTEN
  TICKER_DATE.xlsx             the filled-in workbook, one per company
  TICKER_DATE.provenance.json  where every number came from -- enough to
                               defend or reproduce it a year from now
  results_DATE.xlsx            every company on one sheet, sortable
  run_DATE.json                counts, policy, trigger prices, the queue
"""


def print_guide(full: bool = False) -> None:
    print(BAR)
    print("STOCK SCREEN")
    print(BAR)
    print(SHORT_GUIDE)
    if full:
        print(FULL_LEGEND)


def print_policy(summary_policy: dict) -> None:
    exempt = summary_policy.get("exempt_checks") or []
    print(f"Policy: flag when the verdict is "
          f"'{summary_policy['require_verdict']}' and no "
          f"{'/'.join(summary_policy['suppress_on_severity'])} fired.")
    print(f"        exempt from suppressing: "
          f"{', '.join(exempt) if exempt else '(none)'}")
    print("        an exempt check still fires and is still reported; it")
    print("        just no longer removes a company from the queue.")
    print()


def print_run_banner(total: int) -> None:
    """
    Sits directly above the progress bar, so the count and the thing
    counting are adjacent. Buried under the policy block it read as part
    of the legend rather than as the start of the run.
    """
    print(f"Screening {total} companies.")


# ---------------------------------------------------------------------------
# 3. Progress
# ---------------------------------------------------------------------------


def make_progress_printer(quiet: bool = False):
    """
    A one-line progress indicator for screen_universe's callback.

    The run takes roughly 1.4 seconds per company, so a silent terminal for
    four minutes looks like a hang. This overwrites a single line instead
    of streaming a table, which keeps the finished report in one clean
    block you can scroll back through or paste into an email.
    """
    def on_progress(done: int, total: int, result, lei: str) -> None:
        if quiet:
            return
        ticker = result.ticker if result is not None else "(skipped)"
        bar_width = 28
        filled = int(bar_width * done / total) if total else bar_width
        bar = "#" * filled + "." * (bar_width - filled)
        sys.stdout.write(f"\r  [{bar}] {done}/{total}  {ticker:<12}")
        sys.stdout.flush()
        if done == total:
            sys.stdout.write("\r" + " " * (bar_width + 30) + "\r")
            sys.stdout.flush()
    return on_progress


# ---------------------------------------------------------------------------
# 4. Companies needing attention
# ---------------------------------------------------------------------------


def print_findings(summary: RunSummary, show_all: bool = False) -> None:
    """
    Every company with something to say about it: flagged, held back, or
    void. "Needing attention" implied an action queue, which this is not --
    the research queue at the bottom is that. This is the exception list.

    TRIGGER is deliberately absent here. In this table every row is already
    flagged, suppressed or void: for a flagged name the trigger price is
    behind us, and for a void one there is no valid value to derive it
    from. It earns its place in results_DATE.xlsx and in a future
    watchlist, not in a column that is dead in every row shown.
    """
    rows = summary.companies if show_all else summary.findings
    heading = "EVERY COMPANY" if show_all else "FINDINGS"
    print(RULE)
    print(f"{heading}  --  {len(rows)} of {len(summary.companies)}")
    if not show_all:
        print("flagged for research, held back by a warning, or void")
    print(RULE)

    if not rows:
        print("  Nothing void, flagged or held back. Every company read")
        print("  FAIRLY VALUED or OVERVALUED with no findings.\n")
        return

    print(f"  {'COMPANY':<28}{'EXCH':<6}{'PRICE':>9}{'VALUE':>10}"
          f"{'UPSIDE':>9}  {'VERDICT':<15}NOTE")
    for r in sorted(rows, key=lambda c: (not c.research_flag, c.verdict)):
        vps = r.value_per_share if isinstance(r.value_per_share, (int, float)) else 0
        up = r.upside if isinstance(r.upside, (int, float)) else 0
        px = r.current_price if isinstance(r.current_price, (int, float)) else 0

        if r.is_void:
            fails = r.by_severity("FAIL")
            note = shorten(strip_severity(fails[0]["message"]), 32) if fails else "void"
            note += f"  (+{len(fails) - 1})" if len(fails) > 1 else ""
        elif r.research_flag:
            note = "RESEARCH"
        elif r.suppressed_by:
            held = [c for c in r.checks if c["cell"] in r.suppressed_by]
            note = shorten(strip_severity(held[0]["message"]), 32)
            note += f"  (+{len(held) - 1})" if len(held) > 1 else ""
        else:
            note = ""

        print(f"  {shorten(r.ticker, 8):<9}{shorten(r.name, 18):<19}"
              f"{r.exchange or '?':<6}{px:>9,.2f}{vps:>10,.2f}{up:>9.1%}  "
              f"{r.short_verdict:<15}{note}")
    print()


def print_why(summary: RunSummary) -> None:
    """
    Why each company landed where it did.

    Covers EVERY company with findings that produced a usable valuation --
    flagged and held back alike. An earlier draft capped this at eight
    rows, which silently dropped most of the suppressed names; those are
    exactly the ones you need reasoning for, because a company held back by
    a warning is a judgement call you may want to overrule.

    Void companies are excluded on purpose. When a check returns FAIL the
    workbook will not stand behind ANY of its numbers, so quoting a WACC or
    a terminal share for a void row would be presenting figures the model
    has already disowned. Why they voided is in RESULTS instead.
    """
    rows = [c for c in summary.findings if not c.is_void and c.why]
    if not rows:
        return
    print(RULE)
    print("WHY  --  what drove these verdicts")
    print(RULE)
    for r in sorted(rows, key=lambda c: (not c.research_flag, c.ticker)):
        print(f"  {r.ticker:<9}{r.short_verdict:<15}{r.why}")
        if r.suppressed_by:
            for msg in r.suppressor_messages:
                print(f"  {'':<9}{'':<15}held back: "
                      f"{shorten(strip_severity(msg), 46)}")
    voided = [c for c in summary.findings if c.is_void]
    if voided:
        print(f"\n  {len(voided)} void company(s) omitted: when a check FAILS the")
        print("  workbook disowns every number, so there is nothing to quote.")
    print()


# ---------------------------------------------------------------------------
# 5. Diagnostics
# ---------------------------------------------------------------------------


def print_counts(summary: RunSummary) -> None:
    print(RULE)
    print("RESULTS")
    print(RULE)
    for verdict, n in summary.verdict_counts.most_common():
        print(f"  {verdict:<38}{n:>5}")
    if summary.skipped:
        print(f"  {'could not be valued':<38}{len(summary.skipped):>5}")
    print()

    by_size = summary.by_size
    print(f"  {'by size':<24}{'n':>5}{'VOID':>7}{'UNDER':>7}{'FAIR':>7}"
          f"{'OVER':>7}")
    for _, _, label in SIZE_BUCKETS:
        c = by_size[label]
        n = sum(c.values())
        if not n:
            continue
        print(f"  {label:<24}{n:>5}{c['VOID']:>7}"
              f"{c['UNDERVALUED'] + c['SUPPRESSED']:>7}"
              f"{c['FAIRLY VALUED']:>7}{c['OVERVALUED']:>7}")
    print("\n  A void rate that climbs as size falls is the model meeting")
    print("  companies it cannot value, not a bug.")
    print()

    if summary.stale_prices:
        print(f"  {len(summary.stale_prices)} company(s) valued on a quote "
              f"more than a week old:")
        for r in summary.stale_prices[:5]:
            print(f"    {r.ticker:<9}{r.price_as_of}  "
                  f"({r.price_age_days} days)")
        print("  The workbook cannot see this. Re-run fetch_prices.py.\n")

    under = summary.undervalued
    queue = summary.research_queue
    if under:
        text = summary.check_text
        print("  THE POLICY FILTER")
        print(f"    {'undervalued by the workbook':<34}{len(under):>5}")
        print(f"    {'of those, flagged for research':<34}{len(queue):>5}")
        print(f"    {'held back by a warning':<34}{len(under) - len(queue):>5}"
              f"   ({summary.suppression_rate:.0%})")
        counts = summary.suppression_counts
        graded = set((summary.policy.get("graded_checks") or {}))
        if counts:
            print("\n    what held them back:")
            for cell, n in counts.most_common():
                share = n / len(under)
                if cell in PROCESS_CHECKS:
                    tag = "  <- about the PIPELINE, not the company"
                elif cell in graded:
                    # Already graded, so the routine cases are let through
                    # and only genuine outliers reach here. Telling the
                    # user to exempt it would undo that.
                    tag = "  <- already graded; these are the outliers"
                elif share > 0.5:
                    tag = "  <- fires for most; read it before exempting"
                else:
                    tag = ""
                print(f"      {n:>4} of {len(under)}  ({share:>4.0%})   {cell}  "
                      f"{shorten(strip_severity(text.get(cell, '')), 32)}{tag}")
        if summary.suppression_rate > 0.5:
            process = [c for c in counts if c in PROCESS_CHECKS]
            print("\n    More than half were held back, so the POLICY is doing")
            print("    much of the screening rather than the model.")
            if process:
                print(f"    {', '.join(process)} describe the PIPELINE and are")
                print("    safe to exempt in screen_policy.json.")
            print("    The rest describe the COMPANY. Before exempting one,")
            print("    ask whether the model has ALREADY acted on the risk.")
            print("    D103 was exempted on exactly that basis: D43 takes")
            print("    MAX(synthetic, actual) for the cost of debt, so the")
            print("    higher rate is already in the WACC and suppressing")
            print("    the flag as well charged the company twice.")
            print("    Where nothing corrects for it, the warning stands.")
        print()

    if summary.fail_reasons:
        text = summary.check_text
        print("  WHY COMPANIES WERE VOIDED")
        print("  A VOID means a check returned FAIL, so the workbook refuses")
        print("  to stand behind the number. It must not be used at all.")
        for cell, n in summary.fail_reasons.most_common():
            print(f"    {n:>4} companies   {cell}  "
                  f"{shorten(strip_severity(text.get(cell, '')), 44)}")
        print()


def print_files(summary: RunSummary) -> None:
    print(RULE)
    print("FILES")
    print(RULE)
    n = len(summary.companies)
    if summary.results_path:
        print(f"  {summary.results_path}")
        print("     every company on one sheet, sortable")
    print(f"  {summary.run_dir / 'TICKER_DATE.xlsx'}")
    print(f"     {n} completed workbooks, one per company")
    print(f"  {summary.run_dir / 'TICKER_DATE.provenance.json'}")
    print(f"     {n} sidecars: where each number came from")
    if summary.run_json_path:
        print(f"  {summary.run_json_path}")
        print("     counts, policy, trigger prices and the queue")
    print()


# ---------------------------------------------------------------------------
# 7. The answer
# ---------------------------------------------------------------------------


def print_research_queue(summary: RunSummary) -> None:
    """
    The last thing on screen, and only the list.

    Everything that used to sit under it -- the counts, the suppression
    breakdown, the policy warning -- is accounting, and accounting belongs
    in RESULTS. What is left is the answer: which companies, and where to
    find them. EXCH is included because a ticker without its exchange is
    not enough to actually go and look one up.
    """
    queue = summary.research_queue
    print(BAR)
    print("RESEARCH QUEUE  --  the companies worth investigating")
    print(BAR)

    if not queue:
        print("  (empty)")
        if summary.undervalued:
            print("  Every undervalued company was held back by a warning.")
            print("  See THE POLICY FILTER under RESULTS above.")
        else:
            print("  Nothing read UNDERVALUED. That is the model's view of")
            print("  the market, not a failure.")
        print()
        return

    print(f"  {'TICKER':<10}{'EXCH':<6}{'COMPANY':<32}{'PRICE':>9}{'UPSIDE':>9}")
    for r in sorted(queue, key=lambda c: -(c.upside or 0)):
        up = r.upside if isinstance(r.upside, (int, float)) else 0
        px = r.current_price if isinstance(r.current_price, (int, float)) else 0
        print(f"  {r.ticker:<10}{r.exchange or '?':<6}{shorten(r.name, 30):<32}"
              f"{px:>9,.2f}{up:>9.1%}")
    print()


def print_report(summary: RunSummary, show_all: bool = False,
                 full_legend: bool = False) -> None:
    """The whole report, in reading order. The queue is last."""
    print_findings(summary, show_all=show_all)
    print_why(summary)
    print_counts(summary)
    print_files(summary)
    print_research_queue(summary)
    if full_legend:
        print(FULL_LEGEND)
