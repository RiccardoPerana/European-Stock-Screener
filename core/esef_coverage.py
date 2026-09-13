#!/usr/bin/env python3
"""
ESEF coverage probe
===================

Answers one question before any pipeline code gets written:

    How many companies listed on EU regulated markets have a COMPLETE
    run of annual ESEF filings for the five fiscal years the valuation
    model needs?

The valuation workbook requires five consecutive fiscal years (FY-4..FY0)
and fails validation check D111 unless all 100 statement cells are numeric.
A company missing even one year's filing can never satisfy that gate, so
this probe establishes the ceiling on the addressable universe.

WHAT THIS MEASURES
------------------
Filing AVAILABILITY only. It counts whether a report exists in the archive
for each year. It does NOT open the reports or check whether the 20 line
items the model needs are tagged inside them. Real D111 pass rate will be
LOWER than the number this prints, because of taxonomy extensions and
untagged line items. Treat the output as an upper bound.

Two further reasons this is an upper bound:

  * Financials are not excluded. The archive carries no industry
    classification, so banks and insurers are still in the count. They must
    be removed later per Section 3.2 of the brief.
  * "Country" in the archive means the country where the filer has issued
    securities on EU regulated markets, not country of domicile. Domicile
    drives the country risk premium (Inputs!B53) and has to come from
    somewhere else.

DATA SOURCE
-----------
filings.xbrl.org, the public XBRL International index of ESEF filings,
via the official `xbrl-filings-api` client. Public regulatory data, no
credentials, no rate-limit agreement, bulk retrieval is the intended use.

USAGE
-----
    pip install xbrl-filings-api
    python esef_coverage.py                          # default euro-area set
    python esef_coverage.py --countries IT FR DE
    python esef_coverage.py --first-year 2021 --last-year 2025
    python esef_coverage.py --refresh                # ignore the cache

OUTPUT
------
    <out-dir>/esef_coverage_<timestamp>.csv    one row per entity
    <out-dir>/esef_coverage_<timestamp>.json   run summary
    stdout                                     human-readable summary
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import config

try:
    import xbrl_filings_api as xf
except ImportError:
    sys.exit(
        "Missing dependency. Install it with:\n"
        "    pip install xbrl-filings-api"
    )


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Euro-area EU member states. Deliberately NOT all of the EU.
#
# The model fixes Inputs!B8 to EUR and Inputs!G14 requires the share price to
# come from the primary listing IN STATEMENT CURRENCY. Inputs!B48 is a single
# euro-area AAA risk-free rate. A Swedish or Polish listing breaks all three,
# and validation check D114 only catches the mismatch when the resulting
# market-cap-to-revenue ratio lands outside 0.05..30 -- which it often will
# not. So the currency filter has to happen here, not downstream.
EURO_AREA = [
    "AT", "BE", "CY", "DE", "EE", "ES", "FI", "FR", "GR", "HR",
    "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PT", "SI", "SK",
]

# ESEF became mandatory for financial years starting on or after 2020-01-01,
# so 2020 is the earliest year with meaningful coverage.
EARLIEST_ESEF_YEAR = 2020

DEFAULT_OUT_DIR = config.ROOT / "out"


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class EntityCoverage:
    """Filing coverage for a single legal entity across the requested years."""

    lei: str
    name: str
    country: str
    years_present: set[int] = field(default_factory=set)
    # Month of each filing's reporting date, keyed by year. Used to detect
    # non-calendar fiscal years, which Section 3.3 of the brief flags as an
    # open problem: a company with a June year end cannot be lined up against
    # December reporters without an explicit alignment rule.
    end_months: dict[int, int] = field(default_factory=dict)
    # Archive-reported validation errors, summed across the entity's filings.
    # A filing with errors is more likely to parse badly downstream.
    error_count: int = 0

    def is_complete(self, required: list[int]) -> bool:
        return all(y in self.years_present for y in required)

    def missing(self, required: list[int]) -> list[int]:
        return [y for y in required if y not in self.years_present]

    @property
    def fiscal_month(self) -> int | None:
        """Modal fiscal year-end month, or None if the entity has no filings."""
        if not self.end_months:
            return None
        return Counter(self.end_months.values()).most_common(1)[0][0]

    @property
    def consistent_year_end(self) -> bool:
        """False if the fiscal year end moved during the window."""
        return len(set(self.end_months.values())) <= 1


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------

def fetch_country(country: str, cache_dir: Path, refresh: bool) -> list[dict]:
    """
    Fetch every ESEF filing recorded for one country.

    Returns plain dicts rather than library objects so results can be cached
    to disk and the aggregation logic can be unit-tested without the network.

    We filter server-side on country only. `reporting_date` is derived
    client-side by the library (from the package filename) and is not a
    filterable API field, so year bucketing happens locally. Country volumes
    are small enough that pulling everything is cheaper than fighting the
    filter syntax.
    """
    cache_file = cache_dir / f"{country}.json"

    if cache_file.exists() and not refresh:
        print(f"  {country}: reading from cache", flush=True)
        return json.loads(cache_file.read_text())

    print(f"  {country}: querying archive...", end=" ", flush=True)
    try:
        filings = xf.get_filings(
            filters={"country": country},
            flags=xf.GET_ENTITY,   # pull entity name + LEI alongside each filing
            limit=xf.NO_LIMIT,     # the client handles pagination
        )
    except Exception as exc:
        # One country failing should not abandon the whole run. Report and
        # carry on -- a partial picture is still decision-useful, provided
        # the gap is visible in the summary rather than silently absorbed.
        print(f"FAILED ({type(exc).__name__}: {exc})")
        return []

    records = []
    for f in filings:
        rdate: date | None = f.reporting_date or f.last_end_date
        if rdate is None:
            # No derivable period end. Cannot assign it to a fiscal year, so
            # it is unusable for this count.
            continue

        entity = f.entity
        records.append({
            "lei": (entity.identifier if entity else None) or "UNKNOWN",
            "name": (entity.name if entity else None) or "",
            "country": f.country or country,
            "reporting_date": rdate.isoformat(),
            "filing_index": f.filing_index or "",
            "language": f.language or "",
            "error_count": int(f.error_count or 0),
            # Retrieval URLs, so esef_extract.py can reuse this cache instead
            # of maintaining a second, divergent view of which filings exist.
            "json_url": f.json_url or "",
            "package_url": f.package_url or "",
        })

    print(f"{len(records)} filings")

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(records, indent=1))
    return records


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def deduplicate(records: list[dict]) -> list[dict]:
    """
    Collapse duplicate filings down to one per (entity, year).

    The archive holds several rows for the same annual report: one per
    language version, plus any corrected re-issues. `filing_index` encodes
    them as LEI-date-system-country-entry, where the highest entry number is
    the most recent publication. Counting raw rows would badly overstate
    coverage, so we keep the highest entry number per entity-year.
    """
    best: dict[tuple[str, int], dict] = {}

    for rec in records:
        year = date.fromisoformat(rec["reporting_date"]).year
        key = (rec["lei"], year)

        # Trailing integer of filing_index is the entry number. Missing or
        # malformed indexes sort lowest so a well-formed row always wins.
        try:
            entry = int(rec["filing_index"].rsplit("-", 1)[-1])
        except (ValueError, AttributeError, IndexError):
            entry = -1

        incumbent = best.get(key)
        if incumbent is None or entry > incumbent["_entry"]:
            best[key] = {**rec, "_entry": entry, "_year": year}

    return list(best.values())


def build_coverage(records: list[dict]) -> dict[str, EntityCoverage]:
    """Fold deduplicated filings into one EntityCoverage per entity."""
    coverage: dict[str, EntityCoverage] = {}

    for rec in records:
        lei = rec["lei"]
        year = rec["_year"]

        if lei not in coverage:
            coverage[lei] = EntityCoverage(
                lei=lei, name=rec["name"], country=rec["country"]
            )

        ent = coverage[lei]
        ent.years_present.add(year)
        ent.end_months[year] = date.fromisoformat(rec["reporting_date"]).month
        ent.error_count += rec["error_count"]

        # Entity names occasionally arrive blank on some rows; take any
        # non-empty one we encounter.
        if not ent.name and rec["name"]:
            ent.name = rec["name"]

    return coverage


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def summarise(
    coverage: dict[str, EntityCoverage], required: list[int]
) -> dict:
    """Build the run summary. Pure function so it can be tested offline."""
    complete = [e for e in coverage.values() if e.is_complete(required)]

    # How many years each entity has, to show whether near-misses are common.
    depth = Counter(
        len([y for y in required if y in e.years_present])
        for e in coverage.values()
    )

    # Which single year is most often the missing one. If coverage collapses
    # in the most recent year that is a reporting-lag artefact and the window
    # should shift back one year rather than the plan being abandoned.
    missing_by_year = Counter()
    for e in coverage.values():
        for y in e.missing(required):
            missing_by_year[y] += 1

    by_country = Counter(e.country for e in complete)

    non_calendar = [e for e in complete if e.fiscal_month != 12]
    moved_year_end = [e for e in complete if not e.consistent_year_end]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "required_years": required,
        "entities_seen": len(coverage),
        "entities_complete": len(complete),
        "completion_rate": (
            round(len(complete) / len(coverage), 4) if coverage else 0.0
        ),
        "years_held_distribution": dict(sorted(depth.items())),
        "missing_year_counts": dict(sorted(missing_by_year.items())),
        "complete_by_country": dict(by_country.most_common()),
        "complete_non_calendar_fy": len(non_calendar),
        "complete_moved_year_end": len(moved_year_end),
    }


def write_csv(path: Path, coverage: dict[str, EntityCoverage],
              required: list[int]) -> None:
    """One row per entity, complete ones first, so the file is scannable."""
    import csv

    rows = sorted(
        coverage.values(),
        key=lambda e: (not e.is_complete(required), e.country, e.name),
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "lei", "name", "country", "complete", "years_held",
            "years_present", "missing_years", "fiscal_month",
            "consistent_year_end", "archive_error_count",
        ])
        for e in rows:
            writer.writerow([
                e.lei,
                e.name,
                e.country,
                "YES" if e.is_complete(required) else "no",
                len([y for y in required if y in e.years_present]),
                " ".join(str(y) for y in sorted(e.years_present)),
                " ".join(str(y) for y in e.missing(required)),
                e.fiscal_month or "",
                "YES" if e.consistent_year_end else "no",
                e.error_count,
            ])


def print_report(summary: dict, csv_path: Path, json_path: Path) -> None:
    req = summary["required_years"]
    total = summary["entities_seen"]
    complete = summary["entities_complete"]

    print()
    print("=" * 62)
    print(f"ESEF COVERAGE  |  FY{req[0]}-FY{req[-1]}  ({len(req)} years required)")
    print("=" * 62)
    print(f"Entities seen in archive      {total:>8,}")
    print(f"Entities with all {len(req)} years    {complete:>8,}"
          f"   ({summary['completion_rate']:.1%})")
    print()

    print("Years held (of those required):")
    for years, count in summary["years_held_distribution"].items():
        bar = "#" * min(40, round(40 * count / max(total, 1)))
        print(f"  {years} year(s)  {count:>7,}  {bar}")
    print()

    print("Entities missing each year:")
    for year, count in summary["missing_year_counts"].items():
        print(f"  {year}  {count:>7,}")
    print("  (a spike on the latest year is reporting lag, not absence --")
    print("   rerun with --last-year shifted back one if so)")
    print()

    print("Complete entities by country:")
    for country, count in summary["complete_by_country"].items():
        print(f"  {country}  {count:>6,}")
    print()

    print(f"Non-calendar fiscal year ends   {summary['complete_non_calendar_fy']:>6,}")
    print(f"Fiscal year end changed         {summary['complete_moved_year_end']:>6,}")
    print()
    print("-" * 62)
    print("REMINDER: this counts filings that EXIST. It does not check that")
    print("the 20 line items the model needs are tagged inside them, and it")
    print("does not exclude banks, insurers or the other Section 3.2 types.")
    print("The real D111 pass rate will be lower than the figure above.")
    print("-" * 62)
    print(f"Per-entity detail : {csv_path}")
    print(f"Run summary       : {json_path}")
    print()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    today = date.today()
    # Default to the most recently completed reporting year. Filings for a
    # December year end land through the following spring, so at the start of
    # a year the previous year is not yet populated.
    default_last = today.year - 1 if today.month >= 6 else today.year - 2

    p = argparse.ArgumentParser(
        description="Count EU companies with a complete run of ESEF filings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[config.common_args(db=False, cache_dir=True, years=False)],
    )
    p.add_argument("--countries", nargs="+", default=EURO_AREA, metavar="XX",
                   help="ISO country codes (default: euro-area members)")
    p.add_argument("--last-year", type=int, default=default_last,
                   help=f"Latest fiscal year, FY0 (default: {default_last})")
    p.add_argument("--first-year", type=int, default=None,
                   help="Earliest fiscal year, FY-4 (default: last-year minus 4)")
    # NB: this --out-dir is unrelated to config.OUT_DIR (valuations/) -- it
    # is where the coverage CSV lands, so it stays its own flag rather than
    # common_args()'s out_dir, which points at run output.
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--refresh", action="store_true",
                   help="Ignore cached responses and re-query the archive")

    args = p.parse_args(argv)
    if args.first_year is None:
        args.first_year = args.last_year - 4

    if args.first_year > args.last_year:
        p.error("--first-year must not be later than --last-year")
    if args.first_year < EARLIEST_ESEF_YEAR:
        print(f"WARNING: ESEF only starts at FY{EARLIEST_ESEF_YEAR}. "
              f"FY{args.first_year} will show near-zero coverage.",
              file=sys.stderr)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    required = list(range(args.first_year, args.last_year + 1))

    print(f"Probing {len(args.countries)} countries "
          f"for FY{required[0]}-FY{required[-1]}\n")

    all_records: list[dict] = []
    for country in args.countries:
        all_records.extend(fetch_country(country, args.cache_dir, args.refresh))

    if not all_records:
        print("\nNo filings retrieved. Check network access to "
              "filings.xbrl.org and try again.", file=sys.stderr)
        return 1

    print(f"\n{len(all_records):,} raw filings -> deduplicating "
          "language versions and re-issues...")
    deduped = deduplicate(all_records)
    print(f"{len(deduped):,} unique entity-years")

    coverage = build_coverage(deduped)
    summary = summarise(coverage, required)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = args.out_dir / f"esef_coverage_{stamp}.csv"
    json_path = args.out_dir / f"esef_coverage_{stamp}.json"

    write_csv(csv_path, coverage, required)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(summary, indent=2))

    print_report(summary, csv_path, json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
