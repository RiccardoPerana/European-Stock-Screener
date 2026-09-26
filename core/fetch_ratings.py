#!/usr/bin/env python3
"""
Credit spread ladder -- fetch and confirm
==========================================

Damodaran's two synthetic-rating tables (large- and small-market-cap,
non-financial firms) -- the ones refdata_tables.json's LARGE and SMALL
ladders are hand-copied from every January. This scrapes them instead.

WHY THIS DOES NOT JUST WRITE THE FILE ITSELF
---------------------------------------------
Every other Damodaran dataset this project pulls automatically
(industry betas, margins, country risk, via fetch_parameters.py) is a
clean downloadable .xls/.xlsx file: one sheet, unambiguous columns. This
is different --

  * both source pages are Excel-exported HTML, not a download;
  * the large-cap page's one <table> holds TWO ladders side by side --
    non-financial firms in the first four columns, financial-service
    firms in the next four -- and picking the wrong four would write a
    plausible-looking but wrong spread into every valuation, silently;
  * neither page's own "as of" text is trusted, even where one exists:
    the large-cap page's date is current, but the small-cap page's own
    caption still reads "as of January 2017" while the numbers on it are
    in fact refreshed -- a per-page date would be one page inconsistent
    with the other, so this stamps a single vintage itself instead (see
    apply()).

A `.xls` parse failing is loud. An HTML table quietly handing back the
wrong four columns is not -- and this number gets written straight into
every company's cost of debt. So `fetch()` only returns what it found, and
refuses a ladder that does not parse to exactly ROWS_EXPECTED rungs;
nothing here touches refdata_tables.json except `apply()`. The GUI calls
it from a confirm panel after showing `diff()`; the unattended quarterly
CI screen (scripts/ci_screen.py) applies it directly and logs the diff.

Applying an UNCHANGED table is still meaningful: it re-stamps the vintage,
recording that the ladder was checked against the live site today. Without
that, a year in which Damodaran's numbers happen not to move would age the
vintage past Valuation!D122's 14-month limit.

Run standalone from the command line:

    python core/fetch_ratings.py             fetch and show the diff
    python core/fetch_ratings.py --apply     fetch, show the diff, and write
                                             it (re-stamping the vintage)
"""

from __future__ import annotations

import argparse
import html
import json
import re
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import config

# .html, not .htm: Damodaran's site serves both for the large-cap page,
# but only .html carries a current "Date of Analysis" and gets updated
# each year -- .htm is a stale mirror frozen at an old vintage. Same
# table shape either way, so this only matters for staying current, not
# for parsing.
LARGE_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/ratings.html"
# No .html twin exists for the small-cap table (404) -- .htm is the only
# copy, so it is used even though its own caption text is stale (still
# reads "as of January 2017"); see apply()'s vintage note for why that
# text is never trusted anyway.
SMALL_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/smallrating.htm"

# A ladder that parses to anything else has almost certainly picked up
# the wrong table (or the page's shape changed) -- refuse rather than
# write a plausible-looking wrong number.
ROWS_EXPECTED = 15


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    # Damodaran's page declares charset=macintosh; the data cells this
    # module reads are pure ASCII regardless, so any of these decodes
    # them correctly -- this just avoids a crash on the decorative text.
    for enc in ("utf-8", "cp1252", "mac_roman"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _cells(tr_html: str) -> list[str]:
    out = []
    for td in re.finditer(r"<td\b[^>]*>(.*?)</td>", tr_html, re.S | re.I):
        text = re.sub(r"<[^>]+>", "", td.group(1))
        out.append(re.sub(r"\s+", " ", html.unescape(text)).strip())
    return out


def _first_table(page_html: str) -> list[list[str]]:
    m = re.search(r"<table\b.*?</table>", page_html, re.S | re.I)
    if not m:
        raise ValueError(
            "no <table> found on the page -- it has likely been "
            "restructured; this needs a human to check, not a re-run")
    return [_cells(tr) for tr in
            re.findall(r"<tr\b.*?</tr>", m.group(), re.S | re.I)]


def _ladder(rows: list[list[str]], *, from_col: int, rating_col: int,
           spread_col: int) -> list[dict]:
    """
    Every row that actually parses as a rung of the ladder, wherever it
    sits in the table. Header and spacer rows are skipped because they
    fail to parse, not because of a row position assumed in advance --
    that is what keeps this from being one <table> layout tweak away
    from silently reading the wrong cells.
    """
    out = []
    for r in rows:
        if len(r) <= max(from_col, rating_col, spread_col):
            continue
        rating_full = r[rating_col]
        if "/" not in rating_full:
            continue
        try:
            coverage_from = float(r[from_col])
            spread = round(
                float(r[spread_col].replace("%", "").strip()) / 100, 4)
        except ValueError:
            continue
        out.append({"coverage_from": coverage_from,
                    "rating": rating_full.split("/")[-1],
                    "rating_full": rating_full, "spread": spread})
    return sorted(out, key=lambda row: row["coverage_from"])


def fetch() -> dict:
    """
    The two ladders as Damodaran's site has them right now, shaped like
    refdata_tables.json's own "tables" block. Raises ValueError if
    either page did not yield exactly ROWS_EXPECTED rows, rather than
    return a short or padded ladder that D115/D120 would have to catch
    later.
    """
    # The large-cap page's one <table> holds both ladders side by side:
    # non-financial in columns 0-3, financial-service in 4-7. Only the
    # first four are the ones this project's model uses.
    large = _ladder(_first_table(_fetch(LARGE_URL)),
                    from_col=0, rating_col=2, spread_col=3)
    small = _ladder(_first_table(_fetch(SMALL_URL)),
                    from_col=0, rating_col=2, spread_col=3)

    for name, rows in (("LARGE", large), ("SMALL", small)):
        if len(rows) != ROWS_EXPECTED:
            raise ValueError(
                f"expected {ROWS_EXPECTED} rows for {name}, parsed "
                f"{len(rows)} -- the source page has likely changed "
                f"shape; check it by hand before trusting this")

    return {"LARGE": {"description": "Large non-financial service firms, "
                                     "market cap at or above the cut-off",
                      "rows": large},
            "SMALL": {"description": "Smaller non-financial service "
                                     "companies, market cap below the "
                                     "cut-off",
                      "rows": small}}


def diff(current: dict, fetched: dict) -> list[dict]:
    """
    Every rung that differs from what is currently in refdata_tables.json,
    compared by position (rating tier), not by matching coverage
    thresholds -- the threshold is exactly the number expected to move
    year to year, so keying on it would miss a shifted rung entirely.
    """
    changes = []
    for name in ("LARGE", "SMALL"):
        cur_rows = (current.get("tables", {}).get(name) or {}).get("rows", [])
        for i, now in enumerate(fetched[name]["rows"]):
            was = cur_rows[i] if i < len(cur_rows) else None
            if was is not None and was.get("rating_full") == now["rating_full"] \
                    and was.get("coverage_from") == now["coverage_from"] \
                    and abs(was.get("spread", 0) - now["spread"]) < 1e-9:
                continue
            changes.append({"table": name, "row": i, "was": was, "now": now,
                            "text": _describe(name, was, now)})
    return changes


def _describe(table: str, was: dict | None, now: dict) -> str:
    label = f"{table} — {now['rating_full']}"
    if was is None:
        return (f"{label}: new row (coverage ≥ {now['coverage_from']}, "
                f"spread {now['spread'] * 100:.2f}%)")
    bits = []
    if was.get("coverage_from") != now["coverage_from"]:
        bits.append(f"coverage ≥{was.get('coverage_from')} → "
                    f"≥{now['coverage_from']}")
    if was.get("rating_full") != now["rating_full"]:
        bits.append(f"rating {was.get('rating_full')} → {now['rating_full']}")
    if abs(was.get("spread", 0) - now["spread"]) > 1e-9:
        bits.append(f"spread {was.get('spread', 0) * 100:.2f}% → "
                    f"{now['spread'] * 100:.2f}%")
    return f"{label}: {', '.join(bits) or 'unchanged'}"


def apply(tables_path: Path, fetched: dict, *, today: date | None = None) -> dict:
    """
    Write the fetched ladders into refdata_tables.json. Only "tables",
    "vintage" and "next_expected" change -- size_cutoff is an
    independent, FIXED EUR threshold (see its own "basis" note in the
    file) and this must never touch it.

    vintage is stamped with today's date, not a date read off the source
    page: smallrating.htm's own "Date of Analysis" text is stale (it
    still says January 2017), so it cannot be trusted -- the honest
    vintage is "confirmed current as of the day this was applied".
    """
    today = today or date.today()
    current = json.loads(tables_path.read_text(encoding="utf-8"))
    current["tables"] = fetched
    current["vintage"] = today.strftime("%Y-%m")
    current["next_expected"] = f"{today.year + 1}-{today.month:02d}"
    tables_path.write_text(
        json.dumps(current, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return current


def main() -> int:
    config.utf8_stdout()
    p = argparse.ArgumentParser(
        description="Fetch Damodaran's credit spread ladders and show "
                    "what changed against refdata_tables.json.",
        parents=[config.common_args(db=False, tables=True, years=False)])
    p.add_argument("--apply", action="store_true",
                   help="Write the fetched tables after showing the diff")
    args = p.parse_args()

    current = (json.loads(args.tables.read_text(encoding="utf-8"))
              if args.tables.exists() else {})
    try:
        fetched = fetch()
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        print(f"Could not fetch Damodaran's tables: {e}")
        return 1

    changes = diff(current, fetched)
    if changes:
        print(f"{len(changes)} row(s) differ from {args.tables}:\n")
        for c in changes:
            print(f"  {c['text']}")
    else:
        print("No changes -- refdata_tables.json already matches "
              "Damodaran's site.")

    if args.apply:
        result = apply(args.tables, fetched)
        print(f"\nWrote {args.tables} (vintage {result['vintage']}).")
    else:
        print("\nRe-run with --apply to write "
              + ("these." if changes else "it and re-stamp the vintage."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
