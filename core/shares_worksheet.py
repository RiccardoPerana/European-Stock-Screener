#!/usr/bin/env python3
"""
Share-count worksheet  --  Inputs!B15
=====================================

Every automated route for share counts is now closed:

  ESEF          point-in-time counts in 3% of filings, weighted averages in
                0%. Frequently tagged per share class, and the extractor
                deliberately keeps only consolidated totals.
  EPS division  net income / diluted EPS. Unusable for 32 of 158 -- no
                diluted EPS at all (20), EPS off by a large factor (6),
                net income too thin to divide (4), genuinely odd (2).
  Twelve Data   free plan does not cover European venues.
  Yahoo quote   HTTP 401. Behind crumb authentication.
  ESMA FITRS    publishes liquidity and tick-size calculations, not share
                counts, and its quantitative reporting was decommissioned
                in April 2026.

So the count is entered by hand. That is less painful than it sounds:
share counts change ANNUALLY, not daily, so this is 158 numbers once a
year rather than a recurring cost. And 158 numbers is unambiguously an
insubstantial extract from any terminal you have legitimate access to --
unlike the 150,000 statement cells, which is why that route was ruled out
and this one is not.

WHAT THIS DOES
--------------
--export writes a worksheet PRE-FILLED with the EPS-derived count wherever
that figure survives its own cross-check, and blank where it does not. You
fill the blanks -- roughly 30 of 158 -- rather than all of them.

--import reads it back, checks every row against the accounts and against
the D114 market-cap band, and stores it.

USAGE
-----
    python shares_worksheet.py --export shares.csv
    # fill the blank `shares` cells from annual reports or exchange pages
    python shares_worksheet.py --import shares.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

from cache import Cache

import config

COLUMNS = ["lei", "name", "ticker", "exchange", "shares", "source",
           "eps_derived", "price", "revenue_m", "implied_cap_rev", "note"]

# Same band as Valuation!D114. Checked here so a bad entry is caught at the
# keyboard rather than after a workbook recalculation.
CAP_REV_MIN, CAP_REV_MAX = 0.05, 30.0


def gather(db: Cache, years: list[int]) -> list[dict]:
    fy0 = max(years)
    rows = []
    for lei in db.complete_entities(years):
        listing = db.primary_listing(lei)
        if not (listing and listing.get("ticker")):
            continue
        cy = db.get(lei, fy0)
        est = db.share_estimate(lei, fy0) or {}
        price = db.latest_price(lei)
        existing = db.latest_share_count(lei)

        derived = est.get("wavg_diluted") or est.get("wavg_basic")
        revenue = cy.facts["revenue"].value if cy else None
        px = price["price"] if price else None

        # Trust the derived figure only if it is positive AND lands inside
        # the band. A count that fails its own sanity check is worse than a
        # blank, because a blank gets filled and a wrong number does not.
        usable = None
        if derived and derived > 0 and px and revenue:
            ratio = px * derived / revenue
            if CAP_REV_MIN <= ratio <= CAP_REV_MAX:
                usable = derived

        rows.append({
            "lei": lei,
            "name": (cy.name if cy else "") or "",
            "ticker": listing["ticker"],
            "exchange": listing.get("exchange") or "",
            "shares": (f"{existing['shares']:.0f}" if existing
                       else (f"{usable:.0f}" if usable else "")),
            "source": (existing["source"] if existing
                       else ("eps-derived" if usable else "")),
            "eps_derived": f"{derived:.0f}" if derived else "",
            "price": f"{px:.4f}" if px else "",
            "revenue_m": f"{revenue/1e6:.1f}" if revenue else "",
            "implied_cap_rev": (f"{px * usable / revenue:.2f}"
                                if usable and px and revenue else ""),
            "note": "" if usable or existing else "FILL IN",
        })
    return rows


def seed_derived(db: Cache, years: list[int]) -> int:
    """
    Store the EPS-derived counts that pass their own cross-check.

    `gather()` already computes which derived figures land inside the D114
    cap/revenue band -- exactly the ones `--export` pre-fills and expects
    the user to import unchanged. Doing that here means the share-count
    stage reflects real coverage after one click instead of staying at
    "never done" until someone hand-imports a worksheet. The rows it
    cannot vouch for are left for the worksheet.
    """
    today = date.today().isoformat()
    stored = 0
    for r in gather(db, years):
        if (r["shares"] and r["source"] == "eps-derived"
                and not db.latest_share_count(r["lei"])):
            db.put_share_count(r["lei"], today, float(r["shares"]),
                               "eps-derived", None)
            stored += 1
    return stored


def do_export(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        # Blanks first, so the work to be done is at the top of the file.
        for r in sorted(rows, key=lambda r: (bool(r["shares"]), r["name"])):
            w.writerow(r)

    blank = sum(1 for r in rows if not r["shares"])
    print(f"{len(rows)} companies written to {path}")
    print(f"  pre-filled from EPS : {len(rows) - blank}")
    print(f"  need a number       : {blank}")
    print("\nFill the `shares` column for rows marked FILL IN, in units of")
    print("SHARES (not millions), and set `source` to where you got it.")
    print("The annual report cover page or the exchange's instrument page")
    print("both carry it. Then re-run with --import.")


def do_import(path: Path, db: Cache, years: list[int]) -> int:
    fy0 = max(years)
    today = date.today().isoformat()
    ok = skipped = rejected = warned = 0

    with path.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            lei = (row.get("lei") or "").strip()
            raw = (row.get("shares") or "").strip().replace(",", "")
            if not lei or not raw:
                skipped += 1
                continue
            try:
                shares = float(raw)
            except ValueError:
                print(f"  REJECT {row.get('name','')[:34]:<36}"
                      f"'{raw}' is not a number")
                rejected += 1
                continue
            if shares <= 0:
                print(f"  REJECT {row.get('name','')[:34]:<36}"
                      f"share count must be positive")
                rejected += 1
                continue

            cy = db.get(lei, fy0)
            price = db.latest_price(lei)
            revenue = cy.facts["revenue"].value if cy else None
            if price and revenue:
                ratio = price["price"] * shares / revenue
                if not CAP_REV_MIN <= ratio <= CAP_REV_MAX:
                    # D114 would void this valuation anyway. Refusing it
                    # here keeps a typo out of the database instead of
                    # discovering it after a recalculation.
                    print(f"  REJECT {row.get('name','')[:34]:<36}"
                          f"cap/revenue = {ratio:,.2f}, outside "
                          f"{CAP_REV_MIN}-{CAP_REV_MAX}")
                    rejected += 1
                    continue

            est = db.share_estimate(lei, fy0) or {}
            derived = est.get("wavg_diluted") or est.get("wavg_basic")
            if derived and derived > 0:
                gap = abs(shares - derived) / shares
                if gap > 0.05:
                    print(f"  WARN   {row.get('name','')[:34]:<36}"
                          f"{shares/1e6:,.1f}m entered vs "
                          f"{derived/1e6:,.1f}m from the accounts "
                          f"({gap:.0%} apart)")
                    warned += 1

            db.put_share_count(lei, today, shares,
                               (row.get("source") or "manual").strip(),
                               (row.get("note") or "").strip() or None)
            ok += 1

    print(f"\n  stored   {ok:,}")
    print(f"  blank    {skipped:,}")
    print(f"  rejected {rejected:,}")
    print(f"  warned   {warned:,}")
    if warned:
        print("\n  A warning is not a rejection. The entered figure is kept,")
        print("  because a hand-checked number beats a derived one. But a")
        print("  large gap usually means the ticker points at the wrong")
        print("  company or a different share class -- worth a look.")
    return ok


def main() -> int:
    p = argparse.ArgumentParser(
        description="Manage the B15 share counts.",
        parents=[config.common_args()])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--export", type=Path, metavar="CSV")
    g.add_argument("--import", dest="import_", type=Path, metavar="CSV")
    args = p.parse_args()

    with Cache(args.db) as db:
        if args.export:
            rows = gather(db, args.years)
            if not rows:
                sys.exit("Nothing to export. Run resolve_identity and "
                         "fetch_prices first.")
            do_export(args.export, rows)
        else:
            if not args.import_.exists():
                sys.exit(f"{args.import_} not found.")
            do_import(args.import_, db, args.years)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
