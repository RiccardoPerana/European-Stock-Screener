#!/usr/bin/env python3
"""
Parameter store  --  Inputs!B48:B60
===================================

Loads parameters.json, fetches what can be fetched, and reports what is
still missing. Nothing here is written by the pipeline: values are
hand-maintained and version-controlled, so a valuation can always be traced
to the exact parameter set that produced it.

WHAT IS AUTOMATED AND WHAT IS NOT
---------------------------------
B48, the risk-free rate, comes from the ECB Data Portal. Stable API, stable
series key, fetched on every run.

Everything else comes from Damodaran, published as Excel files whose sheet
names and header rows move between annual editions. Writing a parser
against a layout I have never seen would be guessing, and this project has
already lost time to three confident guesses about external data. So
--inspect downloads the files and prints their structure; the parser gets
written against what is actually there.

USAGE
-----
    python fetch_parameters.py --status
    python fetch_parameters.py --riskfree
    python fetch_parameters.py --inspect
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from esef_extract import USER_AGENT

import config

# ECB euro area AAA-rated central government bond, 10-year spot rate.
ECB_SERIES = "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y"
ECB_URL = ("https://data-api.ecb.europa.eu/service/data/YC/" + ECB_SERIES +
           "?lastNObservations=1&format=csvdata")

DAMODARAN = {
    "ctryprem.xlsx": "Country risk premiums and tax rates -> B49, B53, B54",
    "betaEurope.xls": "European industry unlevered betas -> B57",
    # waccEurope's own FAQ lists beta, cost of equity, D/(D+E), cost of
    # debt and cost of capital -- no sales-to-capital ratio. B58 lives in a
    # different file, and these two are the candidates.
    "capexEurope.xls": "Candidate for sales-to-capital -> B58",
    "fundgrEurope.xls": "Candidate for sales-to-capital -> B58",
    # margin.xls says "US companies" in its own header. Using it would have
    # scored European companies against American peer margins at D90.
    "marginEurope.xls": "European industry operating margins -> B59",
    "EVAEurope.xls": "European industry ROIC -> B60",
}
DAMODARAN_BASE = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/"


def fetch_ecb_riskfree() -> tuple[float | None, str | None, str]:
    """Latest 10-year euro-area AAA spot rate, as a FRACTION."""
    req = urllib.request.Request(ECB_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return None, None, f"HTTP {e.code}"
    except Exception as e:
        return None, None, f"{type(e).__name__}: {e}"

    rows = list(csv.DictReader(io.StringIO(body)))
    if not rows:
        return None, None, "empty response"
    row = rows[-1]
    try:
        # The ECB publishes this in PERCENT. The workbook stores fractions,
        # and Section 6 names this exact conversion as the likeliest silent
        # corruption in the pipeline, so it happens once, here, explicitly.
        percent = float(row["OBS_VALUE"])
        return percent / 100.0, row.get("TIME_PERIOD"), "ok"
    except (KeyError, ValueError) as e:
        return None, None, f"unparseable: {e}"


def _read_sheets(path: Path, max_rows: int = 24) -> dict[str, list[list]]:
    """
    Read the first rows of every sheet, without pandas.

    Damodaran publishes a mix of formats: the country file is .xlsx and the
    industry files are legacy .xls. openpyxl handles the first, xlrd the
    second, and neither pulls in a numeric stack this project does not
    otherwise need.
    """
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        out = {}
        for ws in wb.worksheets:
            rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= max_rows:
                    break
                rows.append(list(row))
            out[ws.title] = rows
        wb.close()
        return out

    import xlrd
    book = xlrd.open_workbook(path)
    out = {}
    for sheet in book.sheets():
        out[sheet.name] = [sheet.row_values(r)
                           for r in range(min(max_rows, sheet.nrows))]
    return out


def inspect_damodaran(out_dir: Path, refresh: bool = False) -> None:
    """Download each file and print its structure. No parsing assumptions."""
    try:
        import openpyxl  # noqa: F401
        import xlrd      # noqa: F401
    except ImportError:
        sys.exit("Readers missing. Run:  pip install openpyxl xlrd")

    out_dir.mkdir(parents=True, exist_ok=True)
    for filename, purpose in DAMODARAN.items():
        url = DAMODARAN_BASE + filename
        target = out_dir / filename
        print(f"\n{'=' * 66}\n{filename}\n  {purpose}\n{'=' * 66}")
        if refresh and target.exists():
            # The annual re-publish keeps the same filenames, so a cached
            # copy would otherwise mask a new edition indefinitely.
            target.unlink()
        if not target.exists():
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=90) as r:
                    target.write_bytes(r.read())
                print(f"  downloaded {target.stat().st_size:,} bytes")
            except Exception as e:
                print(f"  FAILED: {type(e).__name__}: {e}")
                print(f"  Try downloading by hand from {url}")
                continue
        else:
            print(f"  cached ({target.stat().st_size:,} bytes)")

        try:
            sheets = _read_sheets(target)
        except Exception as e:
            print(f"  could not open: {type(e).__name__}: {e}")
            continue

        for name, rows in sheets.items():
            width = max((len(r) for r in rows), default=0)
            print(f"\n  sheet '{name}'  ({width} columns, showing "
                  f"{min(len(rows), 24)} rows)")
            for i, row in enumerate(rows[:24]):
                cells = [str(v)[:22] for v in row[:14]
                         if v not in (None, "") and str(v) != "nan"]
                if cells:
                    print(f"    row {i}: {' | '.join(cells)}")


# Damodaran keys on country NAME; everything else in this pipeline keys on
# ISO code. Aliases cover the spellings his file has used.
NAME_TO_ISO = {
    "austria": "AT", "belgium": "BE", "cyprus": "CY", "germany": "DE",
    "estonia": "EE", "spain": "ES", "finland": "FI", "france": "FR",
    "greece": "GR", "croatia": "HR", "ireland": "IE", "italy": "IT",
    "lithuania": "LT", "luxembourg": "LU", "latvia": "LV", "malta": "MT",
    "netherlands": "NL", "netherlands (the)": "NL", "the netherlands": "NL",
    "portugal": "PT", "slovenia": "SI", "slovakia": "SK",
    "slovak republic": "SK", "slovakia (slovak republic)": "SK",
}


def _clean(value) -> str:
    return str(value).strip().lower() if value is not None else ""


def find_header(rows: list[list], *required: str) -> int | None:
    """
    Locate a header row by its CONTENT, not its position.

    Damodaran's layout shifts between annual editions -- 'ERPs by country'
    currently starts at row 7 behind six rows of settings. Hardcoding that
    would break silently next January, and a parser that reads the wrong
    row produces plausible numbers rather than an error.
    """
    for i, row in enumerate(rows):
        cells = [_clean(c) for c in row]
        if all(any(req in c for c in cells) for req in required):
            return i
    return None


def column_for(header: list, *keywords: str) -> int | None:
    """First column whose header contains all keywords."""
    for i, cell in enumerate(header):
        text = _clean(cell)
        if text and all(k in text for k in keywords):
            return i
    return None


def parse_ctryprem(path: Path) -> tuple[dict, float | None, list[str]]:
    """
    Extract country risk premium, total ERP and tax rate.

    Returns (by_iso, mature_market_erp, warnings).

    The mature-market ERP is not read from a labelled cell -- those move.
    It is DERIVED as the total ERP of a country whose country risk premium
    is zero, which is the definition, and stays correct however the sheet
    is rearranged.
    """
    sheets = _read_sheets(path, max_rows=400)
    warnings: list[str] = []
    out: dict[str, dict] = {}
    mature: float | None = None

    erps = sheets.get("ERPs by country")
    if erps is None:
        warnings.append("sheet 'ERPs by country' not found")
    else:
        h = find_header(erps, "country", "country risk premium")
        if h is None:
            warnings.append("could not locate the header in 'ERPs by country'")
        else:
            header = erps[h]
            c_name = column_for(header, "country") or 0
            c_crp = column_for(header, "country risk premium")
            c_erp = column_for(header, "total equity risk premium")
            for row in erps[h + 1:]:
                iso = NAME_TO_ISO.get(_clean(row[c_name] if row else None))
                if not iso:
                    continue
                rec = out.setdefault(iso, {})
                if c_crp is not None and isinstance(row[c_crp], (int, float)):
                    rec["crp"] = float(row[c_crp])
                if c_erp is not None and isinstance(row[c_erp], (int, float)):
                    rec["total_erp"] = float(row[c_erp])
            for rec in out.values():
                if rec.get("crp") is not None and abs(rec["crp"]) < 1e-9 \
                        and rec.get("total_erp"):
                    mature = rec["total_erp"]
                    break

    taxes = sheets.get("Country Tax Rates")
    if taxes is None:
        warnings.append("sheet 'Country Tax Rates' not found")
    else:
        h = find_header(taxes, "country", "tax rate")
        if h is None:
            warnings.append("could not locate the header in 'Country Tax Rates'")
        else:
            c_name = column_for(taxes[h], "country") or 0
            c_tax = column_for(taxes[h], "tax rate")
            for row in taxes[h + 1:]:
                iso = NAME_TO_ISO.get(_clean(row[c_name] if row else None))
                if iso and c_tax is not None and isinstance(row[c_tax],
                                                            (int, float)):
                    out.setdefault(iso, {})["tax_rate"] = float(row[c_tax])

    for rate in (r.get("crp") for r in out.values()):
        if rate is not None and rate > 1:
            warnings.append("a country risk premium above 1.0 was read -- the "
                            "file is in percent, not fractions. Do not write "
                            "these values without dividing by 100.")
            break
    return out, mature, warnings


# Column specs, read off the actual files. Each entry is
# (target, must-contain keywords, must-NOT-contain keywords).
INDUSTRY_SPECS = {
    "betaEurope.xls": [
        # THE column that matters. betaEurope carries BOTH "Unlevered beta"
        # and "Unlevered beta corrected for cash", two columns apart. D35
        # relevers at GROSS debt, so the cash-corrected figure would
        # understate the cost of equity for all 158 companies at once --
        # invisible to every check in the workbook. The exclusions are the
        # guard, and the test asserts the right column is chosen.
        ("unlevered_beta", ("unlevered", "beta"), ("corrected", "cash")),
    ],
    "EVAEurope.xls": [
        # Header sits at row 18, and the ROC column is past where the
        # inspector was printing. "ROC" alone is the return on capital;
        # "(ROC - Cost of Capital)" is the excess return, and "Cost of
        # Capital" is neither. The exclusions separate the three.
        ("roic", ("roc",), ("cost", "-", "bv", "eva", "capital")),
    ],
    "capexEurope.xls": [
        # capexEurope has 10 columns and the inspector showed 8. Sales to
        # invested capital is the likeliest occupant of the remainder.
        # "Net Cap Ex/Sales" also contains "sales", hence the exclusions.
        ("sales_to_capital", ("sales", "capital"), ("net", "cap ex", "/sales")),
    ],
    "marginEurope.xls": [
        # As-reported operating margin, matching row 21 which is as-reported
        # EBIT. Damodaran's lease adjustment exists for US-GAAP operating
        # leases; under IFRS 16 European filers already capitalise, so the
        # unadjusted column is the like-for-like one.
        ("ebit_margin", ("pre-tax", "unadjusted", "operating"), ()),
    ],
}


def parse_industry_file(path: Path, specs: list[tuple]) -> dict[str, dict]:
    """Pull named columns out of a Damodaran industry sheet."""
    sheets = _read_sheets(path, max_rows=400)
    rows = sheets.get("Industry Averages")
    if rows is None:
        raise KeyError(f"{path.name}: no 'Industry Averages' sheet")

    h = find_header(rows, "industry name")
    if h is None:
        raise KeyError(f"{path.name}: could not locate the header row")
    header = rows[h]

    cols = {}
    for target, include, exclude in specs:
        idx = None
        for i, cell in enumerate(header):
            text = _clean(cell)
            if not text or not all(k in text for k in include):
                continue
            if any(x in text for x in exclude):
                continue
            idx = i
            break
        if idx is None:
            # Print the whole header rather than only reporting a failure.
            # A miss is almost always a column named slightly differently,
            # and showing the real names turns a second round-trip into an
            # immediate fix.
            print(f"    {target:<18}NOT FOUND "
                  f"(wanted all of {include}, none of {exclude})")
            print(f"      header row {h} has {len(header)} columns:")
            for i, cell in enumerate(header):
                if cell not in (None, ""):
                    print(f"        [{i:>2}] {str(cell)[:56]}")
            continue
        cols[target] = idx
        print(f"    {target:<18}column {idx}: {str(header[idx])[:44]}")

    if not cols:
        return {}

    c_name = column_for(header, "industry name") or 0
    out: dict[str, dict] = {}
    for row in rows[h + 1:]:
        name = str(row[c_name]).strip() if row and row[c_name] else ""
        if not name or name.lower().startswith("total market"):
            continue
        rec = {}
        for target, idx in cols.items():
            if idx < len(row) and isinstance(row[idx], (int, float)):
                rec[target] = float(row[idx])
        if rec:
            out[name] = rec
    return out


def do_industries(params: dict, cache_dir: Path, params_path: Path) -> int:
    merged: dict[str, dict] = {}
    for filename, specs in INDUSTRY_SPECS.items():
        path = cache_dir / filename
        if not path.exists():
            print(f"  {filename}: not downloaded. Run --inspect first.")
            continue
        print(f"  {filename}")
        try:
            for name, rec in parse_industry_file(path, specs).items():
                merged.setdefault(name, {}).update(rec)
        except KeyError as e:
            print(f"    FAILED: {e}")

    if not merged:
        print("\n  Nothing parsed.")
        return 1

    industries = params["industries"]
    for name, rec in sorted(merged.items()):
        block = industries.setdefault(name, {})
        block.update(rec)
        block["source"] = "Damodaran European industry datasets"
        block["as_of"] = date.today().isoformat()

    params_path.write_text(json.dumps(params, indent=2, ensure_ascii=False),
                           encoding="utf-8")

    labels = {"unlevered_beta": "B57 unlevered beta",
              "sales_to_capital": "B58 sales-to-capital",
              "ebit_margin": "B59 EBIT margin",
              "roic": "B60 ROIC"}
    print(f"\n  {len(merged)} industries written to {params_path}")
    complete = True
    for key, label in labels.items():
        n = sum(1 for v in merged.values() if key in v)
        print(f"    {label:<24}{n}")
        if n == 0:
            complete = False
    if not complete:
        print("\n  A zero above means the column was not matched. The full")
        print("  header row is printed next to it -- send me that and the")
        print("  spec gets one line longer.")
    return 0


def do_countries(params: dict, path: Path, params_path: Path) -> int:
    by_iso, mature, warnings = parse_ctryprem(path)
    for w in warnings:
        print(f"  WARNING: {w}")

    wanted = [k for k in params["countries"] if not k.startswith("_")]
    filled = 0
    for iso in wanted:
        rec = by_iso.get(iso)
        if not rec:
            print(f"  {iso}: not found in the file")
            continue
        block = params["countries"][iso]
        block["crp"] = round(rec["crp"], 6) if rec.get("crp") is not None else None
        block["tax_rate"] = (round(rec["tax_rate"], 6)
                             if rec.get("tax_rate") is not None else None)
        block["source"] = "Damodaran ctryprem.xlsx"
        block["as_of"] = date.today().isoformat()
        filled += 1
        print(f"  {iso}  CRP {(_pct(block['crp'])):>8}   tax "
              f"{(_pct(block['tax_rate'])):>8}")

    if mature is not None:
        mw = params["market_wide"]["mature_market_erp"]
        mw["value"] = round(mature, 6)
        mw["as_of"] = date.today().isoformat()
        mw["note"] = ("Derived as the total ERP of a zero-CRP country, which "
                      "is the definition of the mature-market premium.")
        print(f"\n  B49 mature market ERP = {mature:.4%}  (derived)")

    params_path.write_text(json.dumps(params, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    print(f"\n  {filled}/{len(wanted)} countries written to {params_path}")
    print("\n  CHECK B54 BEFORE USING IT. Damodaran publishes ONE national")
    print("  rate. The brief wants the statutory/MARGINAL rate, which in")
    print("  Italy adds IRAP to IRES and in Germany adds trade tax. Where")
    print("  a local surcharge applies, override the value by hand and say")
    print("  so in the `source` field.")
    return 0


def _pct(v) -> str:
    return f"{v:.2%}" if isinstance(v, (int, float)) else "-"


def report_status(params: dict) -> int:
    missing = []

    print("=" * 66)
    print("MARKET-WIDE  (B48:B50)")
    print("=" * 66)
    for key, block in params["market_wide"].items():
        val = block.get("value")
        shown = f"{val:.4%}" if isinstance(val, (int, float)) else "MISSING"
        print(f"  {block['cell']}  {key:<26}{shown:>12}   {block.get('as_of') or ''}")
        if val is None:
            missing.append(block["cell"])

    countries = {k: v for k, v in params["countries"].items()
                 if not k.startswith("_")}
    have_crp = sum(1 for v in countries.values() if v.get("crp") is not None)
    have_tax = sum(1 for v in countries.values() if v.get("tax_rate") is not None)
    print(f"\n  B53  country risk premium   {have_crp}/{len(countries)}")
    print(f"  B54  statutory tax rate     {have_tax}/{len(countries)}")

    industries = {k: v for k, v in params["industries"].items()
                  if not k.startswith("_")}
    mapping = {k: v for k, v in params["company_industry"].items()
               if not k.startswith("_")}
    print(f"\n  B57:B60 industries defined  {len(industries)}")
    print(f"          companies mapped    {len(mapping)}")

    print("\n" + "-" * 66)
    if missing or have_crp == 0 or not industries:
        print("NOT READY. Still needed:")
        if missing:
            print(f"  - market-wide values for {', '.join(missing)}")
        if have_crp < len(countries):
            print(f"  - country risk premium and tax rate "
                  f"({len(countries) - have_crp} countries)")
        if not industries:
            print("  - the Damodaran industry table (B57:B60)")
        if not mapping:
            print("  - a company -> industry mapping. B57 drives the ENTIRE")
            print("    cost of equity, and no free source carries a usable")
            print("    classification, so this is 158 rows done by hand.")
        return 1
    print("Parameter store complete.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Manage the parameter store.",
        parents=[config.common_args(db=False, params=True, years=False)])
    # Damodaran's own downloads, unrelated to config.ESEF_CACHE_DIR -- its
    # own flag rather than common_args()'s cache_dir.
    p.add_argument("--cache-dir", type=Path, default=config.ROOT / ".damodaran")
    p.add_argument("--status", action="store_true")
    p.add_argument("--riskfree", action="store_true",
                   help="Fetch the ECB rate and write it into the file")
    p.add_argument("--inspect", action="store_true",
                   help="Download Damodaran's files and print their layout")
    p.add_argument("--refresh", action="store_true",
                   help="With --inspect, re-download even if a copy is cached")
    p.add_argument("--countries", action="store_true",
                   help="Parse ctryprem.xlsx into B49, B53 and B54")
    p.add_argument("--growth", action="store_true",
                   help="Set B50 to the risk-free rate, per Damodaran's "
                        "convention that terminal growth cannot exceed it")
    p.add_argument("--industries", action="store_true",
                   help="Parse the industry files into B57 and B59")
    args = p.parse_args()

    if not args.params.exists():
        sys.exit(f"{args.params} not found.")
    params = json.loads(args.params.read_text(encoding="utf-8"))

    other_jobs = args.inspect or args.countries or args.industries or args.growth

    if args.riskfree:
        rate, period, status = fetch_ecb_riskfree()
        if status != "ok":
            print(f"ECB fetch failed: {status}")
            # Only a hard failure when the rate was the sole job. In a full
            # refresh a transient ECB error must not skip the Damodaran
            # parse that follows.
            if not other_jobs:
                return 1
        else:
            block = params["market_wide"]["risk_free_rate"]
            block["value"] = round(rate, 6)
            block["as_of"] = period or date.today().isoformat()
            args.params.write_text(
                json.dumps(params, indent=2, ensure_ascii=False),
                encoding="utf-8")
            print(f"B48 risk-free rate = {rate:.4%}  ({period})")
            print(f"  stored as the FRACTION {rate:.6f} in {args.params}")
            print("  ECB publishes percent; the workbook wants a fraction.")

    if args.inspect:
        inspect_damodaran(args.cache_dir, refresh=args.refresh)

    if args.countries:
        src = args.cache_dir / "ctryprem.xlsx"
        if not src.exists():
            msg = f"{src} not found. Run --inspect first to download it."
            if not (args.inspect or args.industries or args.riskfree
                    or args.growth):
                sys.exit(msg)
            print(f"  skipping --countries: {msg}")
        else:
            do_countries(params, src, args.params)

    if args.growth:
        rf = params["market_wide"]["risk_free_rate"]["value"]
        if rf is None:
            print("  skipping --growth: no risk-free rate stored yet "
                  "(run --riskfree).")
        else:
            block = params["market_wide"]["long_run_nominal_growth"]
            block["value"] = rf
            block["as_of"] = params["market_wide"]["risk_free_rate"]["as_of"]
            block["source"] = (
                "Set equal to B48. Damodaran's convention caps terminal "
                "growth at the risk-free rate: a company growing faster than "
                "the economy forever eventually becomes the economy, and the "
                "risk-free rate is the market's own estimate of long-run "
                "nominal growth.")
            args.params.write_text(
                json.dumps(params, indent=2, ensure_ascii=False),
                encoding="utf-8")
            print(f"B50 long-run nominal growth = {rf:.4%}  (= B48)")
            print("  D70 requires WACC - g >= 0.5%, so this is a ceiling the")
            print("  workbook will enforce, not a value it will use blindly.")

    if args.industries:
        do_industries(params, args.cache_dir, args.params)

    if args.status or not (args.riskfree or args.inspect or args.countries
                           or args.industries or args.growth):
        return report_status(params)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
