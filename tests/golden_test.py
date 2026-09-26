#!/usr/bin/env python3
"""
Golden test and negative fixtures
=================================

Two questions this answers.

1. IS THE ENGINE STILL THE ENGINE?
   The demo company must still return D83 = 11.4538271570406 with all 27
   checks OK. Crucially it is checked TWICE: once from the file as shipped,
   and once after an openpyxl load-and-save round trip followed by a real
   recalculation. The second run is the one that matters -- it proves that
   writing through openpyxl does not damage the formulas, and that the
   recalculation engine reproduces the documented number rather than merely
   producing *a* number.

2. DO THE GATES ACTUALLY FIRE?
   Five faults are injected one at a time into an otherwise untouched demo
   workbook, and each must produce the specific FAIL it was designed to
   catch: a gate nobody has watched fail is a gate being trusted on faith,
   and each of these faults is otherwise a silent wrong answer.

USAGE
-----
    python golden_test.py
    python golden_test.py --engine excel
"""

from __future__ import annotations

# Run as a script: put the pipeline package (../core) on the import path.
# (pytest itself uses pythonpath=core from pytest.ini.)
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "core"))

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

import openpyxl

from write_workbook import RecalcError, recalculate

GOLDEN_VALUE = 11.4538271570406
GOLDEN_UPSIDE = -0.5325
GOLDEN_VERDICT = "OVERVALUED - screen out"
CHECKS = [f"D{r}" for r in range(96, 123)]
TOLERANCE = 1e-9


def read(path: Path) -> dict:
    wb = openpyxl.load_workbook(path, data_only=True)
    val = wb["Valuation"]
    out = {
        "value": val["D83"].value,
        "price": val["D84"].value,
        "upside": val["D85"].value,
        "verdict": val["D86"].value,
        "checks": {c: (str(val[c].value).strip() if val[c].value is not None
                       else None) for c in CHECKS},
    }
    wb.close()
    return out


def failing(checks: dict) -> list[str]:
    return [c for c, text in checks.items()
            if text and text.upper().startswith("FAIL")]


def not_ok(checks: dict) -> list[str]:
    return [c for c, text in checks.items()
            if not text or not text.upper().startswith("OK")]


# ---------------------------------------------------------------------------
# Fault injectors. Each takes an open workbook and breaks exactly one thing.
# ---------------------------------------------------------------------------

def break_missing_statement(wb) -> str:
    """A single blank statement cell. D111 counts and must see 99, not 100."""
    wb["Inputs"]["F20"] = None
    return "D111"


def break_missing_price(wb) -> str:
    """No share price. Under v2.0 this returned FAIRLY VALUED, silently."""
    wb["Inputs"]["B14"] = None
    return "D112"


def break_units(wb) -> str:
    """
    Share count in units instead of millions.

    The only gate closing a false POSITIVE: under v2.0 this produced
    14,856.02 per share against a 24.50 price and read UNDERVALUED.
    """
    ws = wb["Inputs"]
    ws["B15"] = float(ws["B15"].value) * 1e6
    return "D114"


def break_refdata_order(wb) -> str:
    """
    Unsorted coverage thresholds.

    D39/D40 use MATCH(...,1), which returns a silently wrong spread on
    unsorted data rather than an error -- so this must be caught by D115
    and not by the valuation looking odd.
    """
    ws = wb["RefData"]
    ws["A5"], ws["A6"] = ws["A6"].value, ws["A5"].value
    return "D115"


def break_credit_table(wb) -> str:
    """
    The loaded table contradicts market capitalisation.

    The two ladders cross over near 1.2x coverage, so a selection error is
    not conservative in a predictable direction. Hence FAIL, not WARNING.
    """
    ws = wb["RefData"]
    current = str(ws["B22"].value or "").upper()
    ws["B22"] = "SMALL" if current == "LARGE" else "LARGE"
    return "D120"


FIXTURES = [
    ("one statement cell blank", break_missing_statement),
    ("share price absent", break_missing_price),
    ("shares in units, not millions", break_units),
    ("RefData thresholds unsorted", break_refdata_order),
    ("credit table contradicts market cap", break_credit_table),
]


def run_case(template: Path, work: Path, engine: str, mutate=None):
    target = work / "case.xlsx"
    if target.exists():
        target.unlink()
    shutil.copy(template, target)
    if mutate is not None:
        wb = openpyxl.load_workbook(target)
        expected = mutate(wb)
        wb.save(target)
        wb.close()
    else:
        # Round trip with no changes: proves the load-and-save itself is
        # harmless, which is the assumption every write in this pipeline
        # rests on.
        wb = openpyxl.load_workbook(target)
        wb.save(target)
        wb.close()
        expected = None
    recalculate(target, engine)
    return read(target), expected


def main() -> int:
    p = argparse.ArgumentParser(description="Golden test and gate fixtures.")
    p.add_argument("--template", type=Path,
                   default=Path("./valuation_template.xlsx"))
    p.add_argument("--engine", choices=("auto", "excel", "libreoffice"),
                   default="auto")
    args = p.parse_args()
    if not args.template.exists():
        sys.exit(f"{args.template} not found.")

    # TemporaryDirectory, not mkdtemp: mkdtemp leaves a copy of every
    # fixture workbook behind on each run, and this is run often.
    with tempfile.TemporaryDirectory() as tmp:
        return _run(Path(tmp), args)


def _run(work: Path, args) -> int:
    failures = 0

    print("=" * 68)
    print("1. GOLDEN FILE  --  the demo company as shipped")
    print("=" * 68)
    shipped = read(args.template)
    if shipped["value"] is None:
        print("  template carries no cached value; skipping to the round trip")
    else:
        delta = abs(float(shipped["value"]) - GOLDEN_VALUE)
        ok = delta < TOLERANCE
        print(f"  D83 cached      {shipped['value']!r}")
        print(f"  expected        {GOLDEN_VALUE!r}   "
              f"{'MATCH' if ok else f'DIFFERS by {delta:.3g}'}")
        failures += not ok

    print("\n" + "=" * 68)
    print("2. ROUND TRIP  --  openpyxl load/save, then a real recalculation")
    print("=" * 68)
    try:
        got, _ = run_case(args.template, work, args.engine)
    except RecalcError as e:
        print(f"  RECALCULATION FAILED: {e}")
        return 1

    checks = [
        ("D83 value per share", got["value"], GOLDEN_VALUE, TOLERANCE),
        ("D85 upside", got["upside"], GOLDEN_UPSIDE, 5e-5),
    ]
    for label, actual, expected, tol in checks:
        if actual is None:
            print(f"  {label:<24}None  -- nothing recalculated")
            failures += 1
            continue
        delta = abs(float(actual) - expected)
        ok = delta < tol
        print(f"  {label:<24}{float(actual):<22.13f}"
              f"{'MATCH' if ok else f'DIFFERS by {delta:.3g}'}")
        failures += not ok

    verdict_ok = str(got["verdict"]).strip() == GOLDEN_VERDICT
    print(f"  {'D86 verdict':<24}{got['verdict']}"
          f"{'' if verdict_ok else '   EXPECTED ' + GOLDEN_VERDICT}")
    failures += not verdict_ok

    problems = not_ok(got["checks"])
    read_count = sum(1 for v in got["checks"].values() if v)
    print(f"  {'checks':<24}{read_count}/27 read, "
          f"{len(problems)} not OK")
    for c in problems:
        print(f"      {c}  {got['checks'][c]}")
    failures += (read_count != 27) + bool(problems)

    print("\n" + "=" * 68)
    print("3. NEGATIVE FIXTURES  --  each fault must trip its own gate")
    print("=" * 68)
    for label, mutate in FIXTURES:
        try:
            got, expected = run_case(args.template, work, args.engine, mutate)
        except RecalcError as e:
            print(f"  {label:<38}RECALC FAILED: {e}")
            failures += 1
            continue

        fails = failing(got["checks"])
        hit = expected in fails
        verdict = str(got["verdict"] or "")
        voided = verdict.upper().startswith("VOID")
        status = "OK" if (hit and voided) else "MISSED"
        print(f"  {label:<38}{expected}  {status}")
        if not hit:
            print(f"      expected {expected} to FAIL; FAILs were: "
                  f"{fails or 'none'}")
        if not voided:
            print(f"      D86 read {verdict!r}; a FAIL must void the valuation")
        failures += not (hit and voided)

    print("\n" + "=" * 68)
    if failures:
        print(f"{failures} problem(s). Do not trust results until these pass.")
        return 1
    print("All green. The engine is unchanged, the round trip is lossless,")
    print("and all five FAIL gates have been watched firing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
