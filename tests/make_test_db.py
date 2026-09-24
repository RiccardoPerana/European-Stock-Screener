#!/usr/bin/env python3
"""
Build a synthetic database for development and testing.
=======================================================

The pipeline needs four independent data sources to converge before it can
value anything: extracted statements, a resolved listing, a price and a
share count. That makes it awkward to test the SCREENING logic, because
setting up a realistic case means running the whole ingestion chain.

This builds a cache directly from the demo company's own figures, which are
already in the template and already known to produce D83 = 11.4538271570406.
Three variants are created at different prices so a run exercises every
verdict branch rather than only one:

    DEMO1   priced at 24.50   -> OVERVALUED   (the shipped demo)
    DEMO2   priced at  6.00   -> UNDERVALUED  (a cheap name)
    DEMO3   priced at 11.50   -> FAIRLY VALUED

Not a substitute for real data. It is a fixture for exercising the code
paths around the engine without a live extraction run.

    python make_test_db.py --db ./test.db
"""

from __future__ import annotations

# Run as a script: put the pipeline package (../core) on the import path.
# (pytest itself uses pythonpath=core from pytest.ini.)
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "core"))

import argparse
from datetime import date
from pathlib import Path

import openpyxl

from cache import Cache, CompanyYear
from fields import ALL_FIELDS

import config

# Demo figures live in the template, so the fixture cannot drift from it.
SHARES = 420_000_000
VARIANTS = [
    ("DEMO1000000000000001", "DEMO1", "FICTIONAL DEMO ONE SA", "IT", 24.50),
    ("DEMO2000000000000002", "DEMO2", "FICTIONAL DEMO TWO NV", "NL", 6.00),
    ("DEMO3000000000000003", "DEMO3", "FICTIONAL DEMO THREE OY", "FI", 11.50),
]


def demo_statements(template: Path) -> dict[str, list[float]]:
    """Read the demo company's 20 fields x 5 years straight from the sheet."""
    ws = openpyxl.load_workbook(template)["Inputs"]
    out = {}
    for f in ALL_FIELDS:
        # Template is in millions; the cache stores raw units and divides
        # by `scale` on the way out, so multiply back up here.
        out[f.key] = [float(ws[f"{c}{f.row}"].value or 0) * 1e6
                      for c in "BCDEF"]
    return out


def build(db_path: Path, template: Path, years: list[int]) -> None:
    statements = demo_statements(template)
    today = date.today().isoformat()

    with Cache(db_path) as db:
        for lei, ticker, name, country, price in VARIANTS:
            for offset, year in enumerate(years):
                cy = CompanyYear(
                    lei=lei, fiscal_year=year, source="manual",
                    extractor_version="fixture-1.0", name=name,
                    country=country, period_end=f"{year}-12-31",
                    source_ref="synthetic fixture from valuation_template")
                for key, series in statements.items():
                    cy.set(key, series[offset], "manual")
                db.put(cy)

            db.put_listings(lei, [{
                "isin": f"XX0000{ticker}", "ticker": ticker,
                "exchange": {"IT": "IM", "NL": "NA", "FI": "FH"}[country],
                "figi": f"BBG000{ticker}", "security_type": "Common Stock",
                "market_sector": "Equity", "name": name,
                "is_primary": True, "confidence": "fixture"}])
            db.put_price(lei, today, price, "EUR", ticker, None, "fixture")
            db.put_share_count(lei, today, SHARES, "fixture",
                               "synthetic; matches Inputs!B15 = 420 million")

        print(f"{db_path}: {len(VARIANTS)} companies, "
              f"{len(VARIANTS) * len(years)} company-years")
        for lei, ticker, name, country, price in VARIANTS:
            print(f"  {ticker:<8}{country}  {price:>7.2f}  {name}")


def main() -> int:
    p = argparse.ArgumentParser(description="Build a synthetic test cache.")
    p.add_argument("--db", type=Path, default=Path("./test.db"))
    p.add_argument("--template", type=Path,
                   default=Path("./valuation_template.xlsx"))
    p.add_argument("--years", type=int, nargs="+",
                   default=config.FISCAL_YEARS,
                   help="Fiscal window, oldest first. Change the "
                        "default in config.py, not here.")
    args = p.parse_args()
    if args.db.exists():
        args.db.unlink()
    build(args.db, args.template, args.years)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
