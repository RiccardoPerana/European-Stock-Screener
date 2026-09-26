#!/usr/bin/env python3
"""
Run analysis by size  --  does the model behave differently on small caps?
=========================================================================

The hypothesis: the model values small companies using parameters derived
from large ones, and that is why so many results are voided or suppressed.

It is a reasonable hypothesis with a plausible mechanism. B58, the
sales-to-capital ratio, is the median for the whole European industry, and
D104 fires when the cash-flow reinvestment estimate diverges from the one
built on B58. A company whose capital intensity is unlike its industry's
median will trip that check -- and a small company is more likely to be
unlike the median than a large one.

But the mechanism does not prove the effect, and the run has already
produced the evidence. This reads the provenance sidecars and splits the
results by market capitalisation.

WHAT TO LOOK FOR
----------------
If small caps really are the problem, the void and suppression rates should
rise steadily as size falls, and the checks doing the suppressing should
differ between buckets. If the rates are flat, the problem is the policy
rather than the companies, and a separate small-cap procedure would be
solving the wrong thing.

USAGE
-----
    python analyse_run.py
    python analyse_run.py --dir valuations
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import config
from pipeline import SIZE_BUCKETS

# Below this, a bucket's percentages are noise: with three companies, one
# outcome moves the rate by 33 points. Rates are still printed but marked,
# because an unmarked "100%" over one company invites a conclusion the data
# cannot support.
MIN_FOR_RATES = 5

# The screen's own size buckets (EUR millions), so this breakdown and the
# run summary's "by size" table always agree.
BUCKETS = [(label, low, high) for low, high, label in SIZE_BUCKETS]


def pick_run(root: Path, run_date: str | None) -> Path:
    """
    Choose which run to analyse.

    Runs live in dated subfolders. Without --date the newest is used, since
    that is almost always the one you just produced. Older flat layouts,
    where every run shared one directory, still work.
    """
    if run_date:
        candidate = root / run_date
        if candidate.is_dir():
            return candidate
        return root          # flat layout; load() filters by filename
    dated = sorted((d for d in root.iterdir()
                    if d.is_dir() and len(d.name) == 10
                    and d.name.count("-") == 2),
                   key=lambda d: d.name)
    if dated:
        if len(dated) > 1:
            print(f"  {len(dated)} runs available: "
                  f"{', '.join(d.name for d in dated[-4:])}")
            print(f"  Using the newest, {dated[-1].name}. "
                  "Use --date to pick another.\n")
        return dated[-1]
    return root


def load(directory: Path, run_date: str | None) -> list[dict]:
    """
    Read sidecars, optionally from one run only.

    Sidecars accumulate: every run writes TICKER_DATE.provenance.json, so a
    folder holding two runs has each company twice. That does not change
    the shape of a size comparison, but it does inflate every count and
    quietly double-weights any company screened on both days.
    """
    pattern = (f"*_{run_date}.provenance.json" if run_date
               else "*.provenance.json")
    records, dates = [], Counter()
    for path in sorted(directory.glob(pattern)):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            print(f"  could not read {path.name}")
            continue
        stem = path.name.rsplit(".provenance", 1)[0]
        dates[stem.rsplit("_", 1)[-1]] += 1

    if run_date is None and len(dates) > 1:
        print("  Sidecars from more than one run are present:")
        for d, n in sorted(dates.items()):
            print(f"    {d}   {n} companies")
        print("  Counts below mix them. Use --date YYYY-MM-DD for one run.\n")
    return records


def bucket_of(cap: float | None) -> str | None:
    if cap is None:
        return None
    for label, low, high in BUCKETS:
        if low <= cap < high:
            return label
    return None


def main() -> int:
    p = argparse.ArgumentParser(description="Split run results by size.")
    p.add_argument("--dir", type=Path, default=config.OUT_DIR)
    p.add_argument("--date", metavar="YYYY-MM-DD",
                   help="Analyse one run only. Sidecars accumulate across "
                        "runs, so without this a company screened twice is "
                        "counted twice.")
    args = p.parse_args()
    config.utf8_stdout()

    if not args.dir.exists():
        sys.exit(f"{args.dir} not found. Run run_screen.py first.")
    run_dir = pick_run(args.dir, args.date)
    records = load(run_dir, args.date if run_dir == args.dir else None)
    if not records:
        sys.exit(f"No provenance sidecars in {args.dir}. Run run_screen.py "
                 "first.")

    rows = defaultdict(list)
    unbucketed = 0
    for r in records:
        cap = (r.get("market_inputs") or {}).get("market_cap_millions")
        label = bucket_of(cap)
        if label is None:
            unbucketed += 1
            continue
        rows[label].append(r)

    print(f"{len(records)} companies with sidecars"
          + (f", {unbucketed} without a market cap" if unbucketed else ""))

    print("\n" + "=" * 78)
    print("OUTCOMES BY SIZE")
    print("=" * 78)
    print(f"{'bucket':<20}{'n':>5}{'void':>10}{'undervalued':>13}"
          f"{'suppressed':>13}{'median |upside|':>17}")
    print("-" * 78)
    thin_buckets: list[str] = []

    for label, _, _ in BUCKETS:
        group = rows.get(label, [])
        if not group:
            continue
        n = len(group)
        void = sum(1 for r in group
                   if str(r["results"]["verdict"]).startswith("VOID"))
        under = sum(1 for r in group
                    if r["decision"]["qualifies_on_verdict"])
        held = sum(1 for r in group if r["decision"]["suppressed_by"])
        upsides = [abs(r["results"]["upside"]) for r in group
                   if isinstance(r["results"].get("upside"), (int, float))]
        med = statistics.median(upsides) if upsides else 0
        thin = " (too few)" if n < MIN_FOR_RATES else ""
        print(f"{label:<20}{n:>5}{void:>6} {void/n:>3.0%}"
              f"{under:>9} {under/n:>3.0%}"
              f"{held:>9} {(held/under if under else 0):>3.0%}"
              f"{med:>16.0%}{thin}")
        if n < MIN_FOR_RATES:
            thin_buckets.append(label)

    if thin_buckets:
        print(f"\n  {len(thin_buckets)} bucket(s) hold fewer than "
              f"{MIN_FOR_RATES} companies. Their percentages move by tens of")
        print("  points on a single company and should not be read as a "
              "trend.")
    if len(records) < 40:
        print(f"\n  Only {len(records)} companies in total. A size comparison "
              "needs the full")
        print("  run: screen everything, then analyse.")

    print("\n  void %        of all companies in the bucket")
    print("  undervalued % of all companies in the bucket")
    print("  suppressed %  of the UNDERVALUED ones in that bucket")
    print("  median |upside| is how far the model lands from the market;")
    print("    a large figure means the model and the market disagree a lot,")
    print("    which is what you would expect where the inputs fit poorly.")

    print("\n" + "=" * 78)
    print("WHICH CHECKS FIRE, BY SIZE")
    print("=" * 78)
    per_bucket: dict[str, Counter] = {}
    text: dict[str, str] = {}
    for label, _, _ in BUCKETS:
        counter: Counter = Counter()
        for r in rows.get(label, []):
            for c in r["checks"]:
                if c["severity"] in ("FAIL", "WARNING"):
                    counter[c["cell"]] += 1
                    text.setdefault(c["cell"], str(c["message"]))
        per_bucket[label] = counter

    cells = sorted({c for counter in per_bucket.values() for c in counter})
    present = [b for b, _, _ in BUCKETS if rows.get(b)]
    head = f"{'check':<7}" + "".join(f"{b.split()[0]:>10}" for b in present)
    print(head + "   what it says")
    print("-" * 78)
    for cell in cells:
        line = f"{cell:<7}"
        for label in present:
            n = per_bucket[label][cell]
            total = len(rows[label])
            line += f"{(n / total if total else 0):>9.0%} "
        msg = text.get(cell, "")
        msg = msg.split(" - ", 1)[1] if " - " in msg else msg
        print(line + "  " + msg[:34])

    print("\n  Percentages are of the companies in that bucket. This table")
    print("  counts every check that FIRED, whether or not it suppresses the")
    print("  research flag -- exempting a check in screen_policy.json stops")
    print("  it vetoing the shortlist, it does not stop it being reported.")
    print("\n  A check whose rate CLIMBS as size falls is evidence for the")
    print("  small-cap hypothesis. A check that is flat across buckets is")
    print("  firing for reasons unrelated to size, and exempting it in")
    print("  screen_policy.json is the right response rather than building")
    print("  a separate procedure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
