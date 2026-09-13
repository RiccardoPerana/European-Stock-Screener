#!/usr/bin/env python3
"""
Batch screen  --  command-line entry point
==========================================

Thin wrapper. It parses arguments, calls `pipeline.screen_universe()`, and
hands the result to `report.py`. It contains no valuation logic, no policy
and no formatting, so a GUI can call the same pipeline function and render
the same RunSummary without touching this file.

    python run_screen.py
    python run_screen.py --limit 10
    python run_screen.py --all          list every company, not just the
                                        ones needing attention
    python run_screen.py --legend       print the full column reference

OUTPUTS
    valuations/DATE/TICKER_DATE.xlsx             the completed workbook
    valuations/DATE/TICKER_DATE.provenance.json  where every number came from
    valuations/DATE/results_DATE.xlsx            one row per company
    valuations/DATE/run_DATE.json                counts, triggers, the queue
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path

import report
from cache import Cache
from pipeline import RunSummary, eligible_universe, screen_universe
from write_workbook import RecalcError

import config


def write_results_workbook(path: Path, summary: RunSummary) -> None:
    """One sheet, one row per company, ready to sort and filter."""
    import openpyxl
    from openpyxl.styles import Alignment, Font

    headers = ["Company", "Exchange", "Price", "Value per share", "Upside",
               "Trigger price", "Verdict", "Flag", "Ticker", "Country",
               "Industry", "Held back by", "WACC", "Implied rating",
               "Credit table", "Price as of", "Warnings", "Notes", "Fails"]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Screen"
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")

    for r in summary.companies:
        ws.append([
            r.name, r.exchange, r.current_price, r.value_per_share, r.upside,
            r.trigger_price, r.verdict,
            "RESEARCH" if r.research_flag else "",
            r.ticker, r.country, r.industry, ", ".join(r.suppressed_by),
            r.wacc, r.implied_rating, r.credit_table, r.price_as_of,
            len(r.by_severity("WARNING")), len(r.by_severity("NOTE")),
            len(r.by_severity("FAIL")),
        ])

    widths = (34, 9, 10, 14, 9, 13, 26, 10, 10, 8, 30, 16, 8, 13, 12, 12,
              9, 7, 6)
    for col, width in zip("ABCDEFGHIJKLMNOPQRS", widths):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=2):
        for i in (2, 3, 5):
            row[i].number_format = "#,##0.00"
        row[4].number_format = "0.0%"
        row[12].number_format = "0.00%"
    ws.freeze_panes = "A2"
    wb.save(path)
    wb.close()


def main() -> int:
    report.utf8_stdout()

    p = argparse.ArgumentParser(
        description="Run the screen over the universe.",
        parents=[config.common_args(params=True, tables=True, policy=True,
                                    template=True, out_dir=True)])
    p.add_argument("--engine", choices=("auto", "excel", "libreoffice"),
                   default="auto")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--all", action="store_true",
                   help="List every company, not only those needing attention")
    p.add_argument("--legend", action="store_true",
                   help="Print the full explanation of every column and term")
    p.add_argument("--quiet", action="store_true",
                   help="No progress indicator")
    p.add_argument("--log-file", type=Path, default=None,
                   help="Write diagnostics here as well as to the report")
    args = p.parse_args()

    handlers: list[logging.Handler] = []
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO, handlers=handlers or [logging.NullHandler()],
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s")

    for path in (args.params, args.tables, args.policy, args.template,
                 args.db):
        if not path.exists():
            print(f"{path} not found.")
            return 1
    params = json.loads(args.params.read_text(encoding="utf-8"))
    tables = json.loads(args.tables.read_text(encoding="utf-8"))
    policy = json.loads(args.policy.read_text(encoding="utf-8"))

    # One folder per run. After four quarterly screens a flat directory
    # holds 500+ files with only a date suffix to tell them apart.
    stamp = date.today().isoformat()
    run_dir = args.out_dir / stamp

    with Cache(args.db) as db:
        ready = eligible_universe(db, params, args.years)
        if not ready:
            print("Nothing is ready to value. "
                  "Run:  python core/write_workbook.py --readiness")
            return 1
        total = min(len(ready), args.limit) if args.limit else len(ready)

        # 1 and 2: orientation, before anything scrolls past.
        report.print_guide(full=args.legend)
        report.print_policy(policy["research_flag"])
        report.print_run_banner(total)

        try:
            # 3: progress while the run works.
            summary = screen_universe(
                db, params, tables, policy, args.template, run_dir,
                args.years, engine=args.engine, limit=args.limit,
                param_paths=(args.params, args.tables),
                on_progress=report.make_progress_printer(args.quiet))
        except RecalcError as e:
            print(f"\nRECALCULATION FAILED\n  {e}")
            return 1

    summary.results_path = run_dir / f"results_{stamp}.xlsx"
    write_results_workbook(summary.results_path, summary)
    summary.run_json_path = run_dir / f"run_{stamp}.json"
    summary.run_json_path.write_text(
        json.dumps(summary.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8")

    # 4 to 7: the report, research queue last.
    report.print_report(summary, show_all=args.all)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
