"""
Shared configuration and CLI plumbing.
======================================

WHY THIS EXISTS
---------------
The fiscal-year window [2021..2025] was written out longhand in fifteen
files, and the default paths to the database, the parameter store and the
template in nine. Rolling the screen forward to FY2026 therefore meant
fifteen correct edits, and the failure mode of getting it wrong is not a
crash: it is one script reading 2021-2025 while another reads 2022-2026,
which produces a valuation quietly built from mismatched years.

Everything that more than one script needs to agree on lives here. Scripts
now inherit their common flags from `common_args()` instead of redeclaring
them, so a new shared option is added in one place.

THE FISCAL WINDOW
-----------------
`FISCAL_YEARS` is resolved in this order, first hit wins:

    1. --years on the command line          (one run, explicit)
    2. screen_config.json, "fiscal_years"   (the project's own setting)
    3. the five years ending at DEFAULT_FY0 (the fallback below)

Rolling forward is now a one-line edit to screen_config.json, and the run
summary can print which window it used so a mismatch is visible rather
than inferred.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# True inside a PyInstaller-frozen build (sys.frozen), never in a normal
# `python gui/app.py` run. Checked once here rather than at every call
# site that cares.
FROZEN = bool(getattr(sys, "frozen", False))

# --------------------------------------------------------------------------
# Fiscal window
# --------------------------------------------------------------------------

# Section 3.1: exactly five consecutive years. D26 computes a four-year
# revenue CAGR from FY-4 and the normalisation medians at D7/D10 are
# five-point, so this is a hard requirement, not a preference.
YEAR_COUNT = 5

# Change this (or screen_config.json) once a year, after reporting season.
DEFAULT_FY0 = 2025

# The project directory: this file lives in <root>/core/config.py. Data
# and settings are anchored here rather than to "./" so the GUI and the
# in-process ingestion steps find them no matter which directory the app
# was launched from -- a wrong working directory was silently turning
# every data file into "not found".
#
# In a PyInstaller onefile build, __file__ resolves inside a fresh
# temporary extraction folder (sys._MEIPASS) that is wiped after every
# run -- anchoring financials.db, portfolio.db and every other writable
# file there would silently throw away all of it at exit. sys.executable
# is the actual .exe on disk instead, so data lands next to it and
# survives between launches, which is the whole point of "portable".
ROOT = (Path(sys.executable).resolve().parent if FROZEN
        else Path(__file__).resolve().parent.parent)

CONFIG_FILE = ROOT / "screen_config.json"


def _load_config(path: Path = CONFIG_FILE) -> dict:
    """Project settings, if the file exists. Absent is not an error."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A malformed config must not silently fall back to the built-in
        # window: that is exactly the mismatch this module exists to stop.
        raise SystemExit(f"{path} exists but could not be read as JSON.")


def fiscal_years(fy0: int | None = None) -> list[int]:
    """The five-year window, oldest first: FY-4 .. FY0."""
    if fy0 is None:
        cfg = _load_config()
        years = cfg.get("fiscal_years")
        if years:
            # Consecutive, not just ascending: [2020,2021,2023,2024,2025]
            # was passing this check (five items, sorted) while silently
            # skipping 2022 -- exactly the mismatched-window failure mode
            # this module exists to rule out.
            if (len(years) != YEAR_COUNT
                    or years != list(range(years[0], years[0] + YEAR_COUNT))):
                raise SystemExit(
                    f"screen_config.json: fiscal_years must be "
                    f"{YEAR_COUNT} consecutive ascending years, got {years}")
            return list(years)
        fy0 = int(cfg.get("latest_fiscal_year") or DEFAULT_FY0)
    return list(range(fy0 - YEAR_COUNT + 1, fy0 + 1))


FISCAL_YEARS = fiscal_years()

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

DB_PATH = ROOT / "financials.db"
PORTFOLIO_PATH = ROOT / "portfolio.db"
PARAMS_PATH = ROOT / "parameters.json"
TABLES_PATH = ROOT / "refdata_tables.json"
POLICY_PATH = ROOT / "screen_policy.json"
TEMPLATE_PATH = ROOT / "valuation_template.xlsx"
OUT_DIR = ROOT / "valuations"
ESEF_CACHE_DIR = ROOT / ".esef_cache"

# --------------------------------------------------------------------------
# HTTP identity
# --------------------------------------------------------------------------

# One identity for every outbound request. Two different user agents were
# in use, which makes the tool look like two clients to a rate limiter and
# makes it impossible for a data provider to attribute traffic correctly.
USER_AGENT = "esef-screening-tool/0.7 (research; non-commercial)"

# --------------------------------------------------------------------------
# Console
# --------------------------------------------------------------------------


def utf8_stdout() -> None:
    """
    Force UTF-8 on stdout and stderr.

    Windows pipes default to cp1252, which cannot encode the Lithuanian,
    Slovenian and Croatian company names in this universe. Printing to the
    console happens to work; redirecting to a file raises UnicodeEncodeError
    on the first "Akcine bendrove ..." it meets.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


# --------------------------------------------------------------------------
# Check messages
# --------------------------------------------------------------------------


def strip_severity(message: str) -> str:
    """
    'WARNING - estimates diverge' -> the part after the dash.

    Also collapses embedded whitespace (a check message can carry a
    newline from an Excel comment), so callers get one clean line back
    rather than needing their own follow-up normalisation.

    Lives here, not in pipeline.py, so build_report.py can use the exact
    same function without importing pipeline -- which would drag in
    write_workbook's openpyxl/COM dependencies for three lines of string
    splitting. This is the one shared, dependency-free home both sides
    already import from.
    """
    text = " ".join(str(message or "").split())
    return text.split(" - ", 1)[1] if " - " in text else text


# --------------------------------------------------------------------------
# Shared CLI flags
# --------------------------------------------------------------------------


def common_args(*, db: bool = True, params: bool = False,
                tables: bool = False, policy: bool = False,
                template: bool = False, out_dir: bool = False,
                cache_dir: bool = False,
                years: bool = True) -> argparse.ArgumentParser:
    """
    A parent parser carrying the flags every script shares.

    Use as:  argparse.ArgumentParser(parents=[common_args(params=True)])

    Only the flags a script actually uses are added, so `--help` stays
    honest about what the script reads.
    """
    p = argparse.ArgumentParser(add_help=False)
    if db:
        p.add_argument("--db", type=Path, default=DB_PATH)
    if params:
        p.add_argument("--params", type=Path, default=PARAMS_PATH)
    if tables:
        p.add_argument("--tables", type=Path, default=TABLES_PATH)
    if policy:
        p.add_argument("--policy", type=Path, default=POLICY_PATH)
    if template:
        p.add_argument("--template", type=Path, default=TEMPLATE_PATH)
    if out_dir:
        p.add_argument("--out-dir", type=Path, default=OUT_DIR)
    if cache_dir:
        p.add_argument("--cache-dir", type=Path, default=ESEF_CACHE_DIR)
    if years:
        p.add_argument("--years", type=int, nargs="+", default=FISCAL_YEARS,
                       help=f"Fiscal window, oldest first "
                            f"(default {FISCAL_YEARS[0]}-{FISCAL_YEARS[-1]})")
    return p
