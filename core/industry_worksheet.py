#!/usr/bin/env python3
"""
Industry mapping  --  Inputs!B57:B60
====================================

B57 is the industry unlevered beta, and Valuation!D35 relevers it to get
the cost of equity. Map an engineering firm to Advertising and it gets the
wrong beta, the wrong WACC and a wrong valuation -- and NOTHING in the
workbook can see it. There is no diagnostic for "wrong industry".

No free source carries a usable classification: GLEIF gives a legal name,
OpenFIGI a security type, ESEF neither. Wikidata covers part of the
universe (industry_probe.py); the rest is decided here.

COMMANDS
--------
    --classify            the whole job: prompt on the clipboard, paste the
                          reply back, done
    --list                print the valid industry names
    --exclude TICKER      drop one company from the universe
    --exclude-financials  drop every company mapped to a bank, insurer,
                          broker, asset manager, REIT or real-estate industry
    --include TICKER      put an excluded company back
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from cache import Cache

import config

# Damodaran industries matching the business types Instructions!B21 declares
# the model invalid for.
SECTION_32_MARKERS = ("bank", "insur", "brokerage", "investment", "reit",
                      "real estate", "financial")


def valid_industries(params: dict) -> set:
    return {k for k in params["industries"] if not k.startswith("_")}


def save(params: dict, path: Path) -> None:
    path.write_text(json.dumps(params, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def unmapped(db, params, years: list[int]) -> list[dict]:
    """Companies with a primary listing but no industry, largest first."""
    fy0 = max(years)
    excluded = params.get("excluded_companies", {})
    out = []
    for lei in db.complete_entities(years):
        if params["company_industry"].get(lei) or lei in excluded:
            continue
        listing = db.primary_listing(lei)
        if not (listing and listing.get("ticker")):
            continue
        cy = db.get(lei, fy0)
        out.append({
            "lei": lei,
            "ticker": listing["ticker"],
            "name": (cy.name if cy else lei) or lei,
            "country": (cy.country if cy else "") or "",
            "revenue_m": ((cy.facts["revenue"].value or 0) / 1e6) if cy else 0,
        })
    out.sort(key=lambda r: -r["revenue_m"])
    return out


def find_lei(db, key: str, years: list[int]) -> str | None:
    for lei in db.complete_entities(years):
        if lei == key:
            return lei
        listing = db.primary_listing(lei)
        if listing and listing.get("ticker") == key:
            return lei
    return None


def audit_section_32(db, params, years: list[int], quiet: bool = False):
    """
    Report every company mapped to an excluded business type.

    The workbook subtracts deposits as debt, nets out securities that are
    most of a financial's balance sheet, and treats interest as financing
    when for these businesses it is operating. Its only internal defence is
    D110, which is a WARNING and does not void.
    """
    fy0 = max(years)
    hits = []
    for lei, industry in params["company_industry"].items():
        if lei.startswith("_"):
            continue
        if not any(m in industry.lower() for m in SECTION_32_MARKERS):
            continue
        cy = db.get(lei, fy0)
        listing = db.primary_listing(lei) or {}
        hits.append({"lei": lei, "ticker": listing.get("ticker") or lei,
                     "name": (cy.name if cy else lei) or lei,
                     "industry": industry})
    if hits and not quiet:
        print(f"\n  SECTION 3.2 REVIEW ({len(hits)}) -- these are mapped to a "
              "business type\n  the model is not valid for:")
        for h in sorted(hits, key=lambda h: h["industry"]):
            print(f"    {h['ticker']:<10}{h['name'][:32]:<34}{h['industry']}")
        print("\n  Drop them with:  --exclude-financials")
    return hits


def do_exclude(db, params, params_path: Path, leis: list[str],
               years: list[int], reason: str) -> int:
    """Move companies out of the screening universe, reversibly."""
    fy0 = max(years)
    for lei in leis:
        params["company_industry"].pop(lei, None)
        params.setdefault("excluded_companies", {})[lei] = reason
    save(params, params_path)
    for lei in leis:
        cy = db.get(lei, fy0)
        print(f"  excluded  {(cy.name if cy else lei)[:44]}")
    print(f"\n{len(leis)} company(ies) excluded. Reversible with --include.")
    return 0


# ---------------------------------------------------------------------------
# The classification flow
# ---------------------------------------------------------------------------

def build_prompt(rows: list[dict], valid: set) -> str:
    out = [
        "Classify each company into exactly one of the industry names in "
        "LIST A.",
        "Follow Damodaran's conventions:",
        "  - a supplier to carmakers is Auto Parts, not Auto & Truck",
        "  - a tank-storage operator is Transportation, not Oil/Gas",
        "  - a holding company with an identifiable main business is mapped",
        "    to that business, not Diversified",
        "",
        "Reply with one line per company, TICKER | Industry, and nothing "
        "else.",
        "Spell the industry exactly as it appears in LIST A.",
        "",
        "LIST A - valid industry names:",
    ]
    out += [f"  {n}" for n in sorted(valid)]
    out += ["", f"LIST B - {len(rows)} companies "
                "(ticker | name | country | revenue, EUR millions):"]
    out += [f"  {r['ticker']} | {r['name']} | {r['country']} | "
            f"{r['revenue_m']:,.0f}" for r in rows]
    return "\n".join(out)


def to_clipboard(text: str) -> bool:
    """Copy to the system clipboard. False if no tool is available."""
    for cmd in (["clip"], ["pbcopy"], ["xclip", "-selection", "clipboard"],
                ["wl-copy"]):
        if shutil.which(cmd[0]) is None:
            continue
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True)
            return True
        except Exception:
            continue
    return False


def parse_line(line: str) -> tuple[str, str] | None:
    """'- BRE | Auto Parts' -> ('BRE', 'Auto Parts'). None if unusable."""
    line = line.strip().lstrip("-*").strip()
    if not line:
        return None
    parts = re.split(r"\s*[|\t;]\s*|\s{2,}", line, maxsplit=1)
    if len(parts) != 2:
        return None
    key, industry = parts[0].strip(), parts[1].strip()
    return (key, industry) if key and industry else None


def ingest(raw: str, db, params, params_path: Path, valid: set,
           years: list[int]) -> dict:
    """
    Store TICKER | Industry lines. Returns a counts summary.

    Validation catches a hallucinated category. It cannot catch a category
    that is valid but wrong for the company -- and neither can the workbook.
    """
    rows = unmapped(db, params, years)
    by_ticker = {r["ticker"].upper(): r for r in rows}
    by_lei = {r["lei"]: r for r in rows}

    stored = rejected = unknown = 0
    for line in raw.splitlines():
        parsed = parse_line(line)
        if parsed is None:
            continue
        key, industry = parsed
        row = by_ticker.get(key.upper()) or by_lei.get(key)
        if row is None:
            print(f"  unknown company: {key}")
            unknown += 1
            continue
        if industry not in valid:
            near = [v for v in sorted(valid)
                    if industry.lower()[:6] in v.lower()]
            print(f"  REJECT {row['name'][:30]:<32}'{industry}'"
                  + (f"   did you mean {near[0]}?" if near else ""))
            rejected += 1
            continue
        params["company_industry"][row["lei"]] = industry
        stored += 1

    save(params, params_path)
    left = len(unmapped(db, params, years))
    print(f"\n  stored          {stored:,}")
    print(f"  rejected        {rejected:,}")
    print(f"  unknown         {unknown:,}")
    print(f"  still unmapped  {left:,}")
    if left:
        print("\n  Run --classify again for the remainder.")
    if stored:
        print("\n  A valid-but-wrong industry cannot be detected here, and")
        print("  nothing in the workbook can see a misclassification. Worth")
        print("  a skim before running the screen.")
    audit_section_32(db, params, years)
    return {"stored": stored, "rejected": rejected, "unknown": unknown,
            "still_unmapped": left}


def do_classify(db, params, params_path: Path, valid: set,
                years: list[int]) -> int:
    """Prompt, clipboard, paste back. One command, no intermediate files."""
    rows = unmapped(db, params, years)
    if not rows:
        print("Every company already has an industry.")
        audit_section_32(db, params, years)
        return 0

    text = build_prompt(rows, valid)
    copied = to_clipboard(text)

    print("=" * 70)
    print(f"STEP 1 of 2   --   {len(rows)} companies need an industry")
    print("=" * 70)
    if copied:
        print("\n  The prompt is ON YOUR CLIPBOARD. Paste it into any LLM.")
        print("  Nothing to open, nothing to select.\n")
    else:
        print("\n  No clipboard tool found. Copy the block below:\n")
        print(text)
        print()

    print("=" * 70)
    print("STEP 2 of 2   --   paste the reply here, then press Ctrl+Z and")
    print("                   Enter (Windows) or Ctrl+D (macOS / Linux)")
    print("=" * 70 + "\n")
    ingest(sys.stdin.read(), db, params, params_path, valid, years)
    return 0


def main() -> int:
    config.utf8_stdout()
    p = argparse.ArgumentParser(
        description="Map companies to industries.",
        parents=[config.common_args(params=True)])
    p.add_argument("--yes", action="store_true",
                   help="Skip the confirmation on --exclude-financials")

    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--classify", action="store_true",
                   help="Prompt on the clipboard, reply pasted back")
    g.add_argument("--list", action="store_true",
                   help="Print the valid industry names")
    g.add_argument("--exclude", metavar="TICKER_OR_LEI",
                   help="Drop one company from the universe")
    g.add_argument("--exclude-financials", action="store_true",
                   dest="exclude_financials",
                   help="Drop every company mapped to an excluded business "
                        "type")
    g.add_argument("--include", metavar="TICKER_OR_LEI",
                   help="Put a previously excluded company back")
    args = p.parse_args()

    if not args.params.exists():
        sys.exit(f"{args.params} not found.")
    params = json.loads(args.params.read_text(encoding="utf-8"))
    params.setdefault("company_industry", {})
    valid = valid_industries(params)
    if not valid:
        sys.exit("No industries in parameters.json. Run "
                 "fetch_parameters.py --industries first.")

    if args.list:
        for name in sorted(valid):
            print(name)
        return 0

    with Cache(args.db) as db:
        if args.classify:
            return do_classify(db, params, args.params, valid, args.years)

        if args.exclude_financials:
            hits = audit_section_32(db, params, args.years)
            if not hits:
                print("No company is mapped to an excluded business type.")
                return 0
            print("\n  This rule matches on industry name, so it is blunt. It")
            print("  catches payment processors and property DEVELOPERS that")
            print("  hold inventory at cost -- neither is what B21 excludes.")
            if not args.yes:
                try:
                    if input(f"\n  Exclude all {len(hits)}? [y/N] "
                             ).strip().lower() not in ("y", "yes"):
                        print("  nothing changed.")
                        return 0
                except (EOFError, KeyboardInterrupt):
                    print("\n  nothing changed.")
                    return 0
            print()
            return do_exclude(db, params, args.params,
                              [h["lei"] for h in hits], args.years,
                              "Section 3.2 business type; excluded in bulk")

        if args.include:
            lei = find_lei(db, args.include, args.years)
            if lei is None:
                print(f"No company with ticker or LEI '{args.include}'.")
                return 1
            if lei not in params.get("excluded_companies", {}):
                print("That company is not excluded.")
                return 1
            params["excluded_companies"].pop(lei)
            save(params, args.params)
            cy = db.get(lei, max(args.years))
            print(f"{(cy.name if cy else lei)[:44]} restored. It needs an "
                  f"industry again -- run --classify.")
            return 0

        if args.exclude:
            lei = find_lei(db, args.exclude, args.years)
            if lei is None:
                print(f"No company with ticker or LEI '{args.exclude}'.")
                return 1
            return do_exclude(db, params, args.params, [lei], args.years,
                              "Section 3.2 business type; excluded by hand")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
