#!/usr/bin/env python3
"""
Stock screening tool  --  start here
====================================

One command shows where you are and what to do next. Everything else is a
step in this pipeline, and you should not normally need to run them by
hand.

Run these from the repository root.

    python core/screen.py            show status and the next step
    python core/screen.py --setup    run whatever setup is still outstanding
    python core/screen.py --run      screen the universe
    python core/screen.py --analyse  break the last run down by company size

The pipeline, in order:

    1  coverage    which EU companies filed five years of ESEF reports
    2  extract     pull 20 financial line items x 5 years from the filings
    3  identity    LEI -> ISIN -> ticker, and the share count the accounts imply
    4  prices      one closing price per company
    5  shares      diluted share count, hand-entered where derivation fails
    6  parameters  risk-free rate, country risk, industry betas
    7  industries  map each company to a Damodaran industry
    8  screen      write each company into the workbook and read the verdict

Steps 1-2 are annual. Steps 4 and 6 are quarterly. The rest change rarely.
See --schedule.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import config
from cache import Cache

YEARS = config.FISCAL_YEARS

SCHEDULE = """
HOW OFTEN TO RUN WHAT
=====================
  Every quarter        prices, and the market and country parameters
                         python core/fetch_prices.py --source yahoo
                         python core/fetch_parameters.py --riskfree --growth
                         python core/screen.py --run
                       Only the price moves between quarters, and it moves
                       the verdict: a company reading FAIRLY VALUED in March
                       can read UNDERVALUED in September on price alone.

  Every February       the credit tables and industry parameters, after
                       Damodaran republishes in January
                         python core/fetch_parameters.py --inspect --industries
                         python core/fetch_ratings.py --apply
                       The second refreshes refdata_tables.json and stamps
                       its vintage. D122 warns once the table passes 14
                       months, so the workbook will remind you.

  Every year, after    the financial statements
  reporting season       python core/esef_coverage.py --refresh
  (roughly May)          python core/esef_extract.py
                       Company statements only change once a year. Running
                       this quarterly re-downloads identical numbers.

  When a company       its industry mapping
  changes business       python core/industry_worksheet.py --classify

  Never                the model conventions at Inputs!B63:B70. The
                       application verifies them and aborts on drift.
"""


def run(cmd: list[str]) -> int:
    script = Path(__file__).resolve().parent / cmd[0]
    print(f"\n$ python core/{' '.join(cmd)}\n")
    return subprocess.run([sys.executable, str(script), *cmd[1:]]).returncode


def status(db_path: Path, params_path: Path, template: Path,
           cache_dir: Path) -> list[dict]:
    """Each stage, whether it is done, and the command that does it."""
    steps = []

    coverage = len(list(cache_dir.glob("*.json"))) if cache_dir.exists() else 0
    steps.append({
        "name": "1 coverage", "done": coverage > 0,
        "detail": f"{coverage} countries cached" if coverage
                  else "not run",
        "cmd": ["esef_coverage.py"]})

    entities = prices = shares = listings = 0
    if db_path.exists():
        with Cache(db_path) as db:
            universe = db.complete_entities(YEARS)
            entities = len(universe)
            for lei in universe:
                listing = db.primary_listing(lei)
                if listing and listing.get("ticker"):
                    listings += 1
                if db.latest_price(lei):
                    prices += 1
                if db.latest_share_count(lei):
                    shares += 1

    steps.append({
        "name": "2 extract", "done": entities > 0,
        "detail": f"{entities} companies with 5 complete years"
                  if entities else "not run",
        "cmd": ["esef_extract.py"]})
    steps.append({
        "name": "3 identity", "done": listings > 0,
        "detail": f"{listings} of {entities} have a primary listing"
                  if entities else "needs step 2",
        "cmd": ["resolve_identity.py"]})
    steps.append({
        "name": "4 prices", "done": prices > 0,
        "detail": f"{prices} of {listings} priced" if listings
                  else "needs step 3",
        "cmd": ["fetch_prices.py", "--source", "yahoo"]})
    steps.append({
        "name": "5 shares", "done": shares > 0,
        "detail": f"{shares} of {listings} have a share count" if listings
                  else "needs step 3",
        "cmd": ["shares_worksheet.py", "--export", "shares.csv"]})

    params, mapped, industries, missing = {}, 0, 0, []
    if params_path.exists():
        params = json.loads(params_path.read_text(encoding="utf-8"))
        for key, block in (params.get("market_wide") or {}).items():
            if isinstance(block, dict) and block.get("value") is None:
                missing.append(block.get("cell", key))
        industries = len([k for k in params.get("industries", {})
                          if not k.startswith("_")])
        mapped = len([k for k in params.get("company_industry", {})
                      if not k.startswith("_")])

    steps.append({
        "name": "6 parameters", "done": bool(industries) and not missing,
        "detail": (f"{industries} industries"
                   + (f", missing {', '.join(missing)}" if missing else ""))
                  if params else "not run",
        "cmd": ["fetch_parameters.py", "--riskfree"]})
    steps.append({
        "name": "7 industries", "done": listings > 0 and mapped >= listings,
        "detail": f"{mapped} of {listings} mapped" if listings
                  else "needs step 3",
        "cmd": ["industry_worksheet.py", "--classify"]})

    ready = 0
    if db_path.exists() and params:
        from write_workbook import readiness
        excluded = params.get("excluded_companies", {})
        with Cache(db_path) as db:
            for lei in db.complete_entities(YEARS):
                if lei not in excluded and not readiness(lei, db, params,
                                                         YEARS):
                    ready += 1
    steps.append({
        "name": "8 screen", "done": ready > 0 and template.exists(),
        "detail": (f"{ready} companies ready to value"
                   if template.exists()
                   else f"{template} is missing"),
        "cmd": ["run_screen.py"]})
    return steps


def main() -> int:
    config.utf8_stdout()

    p = argparse.ArgumentParser(
        description="Screening tool: status, setup and runs.",
        parents=[config.common_args(params=True, template=True,
                                    cache_dir=True, years=False)])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--setup", action="store_true",
                   help="Run the next outstanding setup step")
    g.add_argument("--run", action="store_true", help="Screen the universe")
    g.add_argument("--analyse", action="store_true",
                   help="Break the last run down by company size")
    g.add_argument("--schedule", action="store_true",
                   help="How often to run each step")
    args = p.parse_args()

    if args.schedule:
        print(SCHEDULE)
        return 0

    steps = status(args.db, args.params, args.template, args.cache_dir)

    print("=" * 66)
    print("STOCK SCREENING TOOL")
    print("=" * 66)
    for s in steps:
        mark = "done" if s["done"] else "TODO"
        print(f"  [{mark}]  {s['name']:<14}{s['detail']}")

    outstanding = [s for s in steps if not s["done"]]

    if args.run:
        return run(["run_screen.py"])
    if args.analyse:
        return run(["analyse_run.py"])

    if args.setup:
        if not outstanding:
            print("\nSetup is complete. Run:  python core/screen.py --run")
            return 0
        step = outstanding[0]
        print(f"\nNext: {step['name']}")
        return run(step["cmd"])

    print()
    if outstanding:
        step = outstanding[0]
        print(f"NEXT STEP  --  {step['name']}")
        print("  python core/screen.py --setup")
        print(f"  (that runs: python core/{' '.join(step['cmd'])})")
        if len(outstanding) > 1:
            print(f"\n  {len(outstanding) - 1} more step(s) after that. Run "
                  "--setup repeatedly.")
    else:
        print("Everything is set up.")
        print("  python core/screen.py --run       screen the universe")
        print("  python core/screen.py --analyse   break results down by size")
    print("  python core/screen.py --schedule  how often to re-run each step")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
