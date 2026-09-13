#!/usr/bin/env python3
"""
Build the portable StockScreen.exe.
====================================

Runs PyInstaller against app.spec, then copies beside the built .exe
everything the app needs on first launch that PyInstaller does not (and
should not) embed -- see app.spec's own docstring for why nothing
writable is bundled inside the exe itself.

WHAT GETS COPIED, AND WHY
--------------------------
Tracked project source the app reads at startup:
    valuation_template.xlsx   the DCF model (never written, only copied)
    screen_policy.json        suppression/threshold policy
    listing_overrides.json    hand-maintained listing corrections
    parameters.json           Damodaran/ECB parameters as of this build
    refdata_tables.json       the credit spread ladder as of this build

Deliberately NOT copied -- personal or derived, exactly what .gitignore
already says never to commit, so a portable build should not smuggle
them in either:
    financials.db, portfolio.db, .esef_cache/, .id_cache/, .damodaran/,
    valuations/, out/, output/, shares.csv, industries.csv, paste_me.txt,
    screen_config.json, the wikidata_*.json probe outputs.

A fresh copy of the .exe therefore starts with the pipeline's tracked
config but no run history -- the coverage/extract/identity/prices/shares
stages need running once, same as a fresh git clone would.

USAGE
-----
    python packaging/build_dist.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "packaging" / "dist"
BUILD = ROOT / "packaging" / "build"
SPEC = ROOT / "packaging" / "app.spec"

# Tracked source files the app reads at startup -- see the module
# docstring for why each one is (or is not) here.
SHIP_ALONGSIDE = [
    "valuation_template.xlsx",
    "screen_policy.json",
    "listing_overrides.json",
    "parameters.json",
    "refdata_tables.json",
]


def main() -> int:
    if DIST.exists():
        shutil.rmtree(DIST)

    code = subprocess.call([
        sys.executable, "-m", "PyInstaller", str(SPEC),
        "--distpath", str(DIST), "--workpath", str(BUILD),
        "--noconfirm",
    ])
    if code:
        return code

    exe = DIST / "StockScreen.exe"
    if not exe.exists():
        print(f"Build reported success but {exe} is missing.", file=sys.stderr)
        return 1

    missing = []
    for name in SHIP_ALONGSIDE:
        src = ROOT / name
        if not src.exists():
            missing.append(name)
            continue
        shutil.copy2(src, DIST / name)

    print(f"\nBuilt: {exe}")
    print(f"Distributable folder: {DIST}")
    if missing:
        print(f"\nWARNING: not found in the project root, so not copied: "
             f"{', '.join(missing)}")
        print("The .exe will still start, but whichever stage needs a "
              "missing file will report it missing, same as a fresh clone.")
    print("\nCopy the whole packaging/dist/ folder wherever you like -- "
          "the .exe reads and writes every data file next to itself.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
