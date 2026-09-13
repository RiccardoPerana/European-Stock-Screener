#!/usr/bin/env python3
"""
Portfolio tracker  --  command-line entry point
==============================================

    python track.py --sync       apply the latest screening run
    python track.py              show the portfolio

--sync reads the most recent run_DATE.json produced by run_screen.py, so
the tracker never re-runs a valuation and never re-derives a verdict. The
screen decides; this records.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path

from cache import Cache
from portfolio import Portfolio, mark, sync

import config

RULE = "-" * 74
BAR = "=" * 74


def latest_run(out_dir: Path) -> Path | None:
    """The most recent run_DATE.json under valuations/."""
    runs = sorted(out_dir.glob("*/run_*.json"))
    return runs[-1] if runs else None


class RunFromJson:
    """
    A stand-in RunSummary rebuilt from run_DATE.json.

    The tracker deliberately consumes the run's own output rather than
    re-screening: a portfolio whose entries came from a second, slightly
    different evaluation would not be a record of what the screen actually
    said.
    """

    def __init__(self, doc: dict, db: Cache, years: list[int]):
        self.valuation_date = doc.get("valuation_date") or date.today().isoformat()
        queue = set(doc.get("research_queue") or [])
        triggers = doc.get("triggers") or {}
        self.companies = []

        fy0 = max(years)
        for ticker, t in triggers.items():
            lei = self._lei_for(db, ticker, years)
            if not lei:
                continue
            cy = db.get(lei, fy0)
            listing = db.primary_listing(lei) or {}
            value = t.get("value_per_share")
            price = t.get("price")
            upside = ((value - price) / price
                      if isinstance(value, (int, float))
                      and isinstance(price, (int, float)) and price else None)
            # The screen's own verdict, recorded in the run file -- falling
            # back to re-deriving one only for a run_DATE.json from before
            # "verdict" was added to the triggers payload.
            verdict = t.get("verdict") or self._verdict(upside)
            self.companies.append(_Company(
                lei=lei, ticker=ticker,
                name=(cy.name if cy else ticker) or ticker,
                exchange=listing.get("exchange") or "",
                current_price=price, value_per_share=value, upside=upside,
                verdict=verdict, research_flag=ticker in queue,
                is_void=False))

    @staticmethod
    def _verdict(upside):
        """Fallback only, for a run_DATE.json predating the recorded verdict
        (see __init__): re-derives one from upside so an old run file still
        loads, rather than the screen's own decision."""
        if upside is None:
            return "VOID - failed validation"
        if upside > 0.25:
            return "UNDERVALUED - investigate"
        if upside < -0.25:
            return "OVERVALUED - screen out"
        return "FAIRLY VALUED - no action"

    @staticmethod
    def _lei_for(db: Cache, ticker: str, years: list[int]) -> str | None:
        for lei in db.complete_entities(years):
            listing = db.primary_listing(lei)
            if listing and listing.get("ticker") == ticker:
                return lei
        return None


class _Company:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def print_portfolio(marked: dict) -> None:
    print(BAR)
    print("PORTFOLIO")
    print(BAR)
    inception = marked["inception_date"] or "not started"
    print(f"  live since {inception}    marked {marked['as_of']}")
    print(f"  {marked['basis']}\n")

    rows = marked["positions"]
    if not rows:
        print("  No positions yet. Run:  python core/track.py --sync\n")
        return

    print(f"  {'TICKER':<10}{'EXCH':<6}{'ENTERED':<12}{'STATUS':<9}"
          f"{'UPSIDE':>9}{'RETURN':>9}")
    for r in sorted(rows, key=lambda x: (x["status"] != "open",
                                         -x["return_pct"])):
        up = (f"{r['current_upside']:.0%}"
              if r["current_upside"] is not None else "-")
        flag = "  <- model cannot value this" if r["unvaluable"] else ""
        print(f"  {r['ticker']:<10}{r['exchange'] or '?':<6}"
              f"{r['entry_date']:<12}{r['status']:<9}{up:>9}"
              f"{r['return_pct']:>9.1%}{flag}")

    print(f"\n  {'invested':<22}{marked['invested']:>12,.2f} EUR"
          f"   ({marked['open']} open, {marked['closed']} closed)")
    print(f"  {'current value':<22}{marked['current_value']:>12,.2f} EUR")
    print(f"  {'return':<22}{marked['return_pct']:>12.1%}")

    if marked["missing_price"]:
        print(f"\n  {len(marked['missing_price'])} position(s) held at cost, "
              f"no current price:")
        print(f"    {', '.join(marked['missing_price'][:8])}")
        print("    Marked at entry rather than zero, so a data gap does not")
        print("    read as a total loss. Run fetch_prices.py.")

    unvaluable = [r["ticker"] for r in rows if r["unvaluable"]]
    if unvaluable:
        print(f"\n  {len(unvaluable)} position(s) the model can no longer "
              f"value: {', '.join(unvaluable)}")
        print("    Held, not sold. A void is a failure to measure, not a")
        print("    signal about the company -- but it is not a healthy")
        print("    holding either, so it is listed separately.")
    print()


def main() -> int:
    config.utf8_stdout()
    p = argparse.ArgumentParser(
        description="Track the screen's picks.",
        parents=[config.common_args(out_dir=True)])
    p.add_argument("--portfolio", type=Path, default=config.PORTFOLIO_PATH)
    p.add_argument("--sync", action="store_true",
                   help="Apply the most recent screening run")
    p.add_argument("--run", type=Path, default=None,
                   help="A specific run_DATE.json instead of the latest")
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    if not args.db.exists():
        print(f"{args.db} not found.")
        return 1

    with Cache(args.db) as db, Portfolio(args.portfolio) as pf:
        if args.sync:
            run_path = args.run or latest_run(args.out_dir)
            if not run_path or not run_path.exists():
                print(f"No screening run found under {args.out_dir}.")
                print("Run:  python core/run_screen.py")
                return 1
            doc = json.loads(run_path.read_text(encoding="utf-8"))
            summary = RunFromJson(doc, db, args.years)
            changes = sync(pf, summary)

            print(f"Applied {run_path.name}\n")
            for label, key in (("entered", "entered"), ("held", "held"),
                               ("cannot value", "unvaluable"),
                               ("not in run", "not_seen")):
                if changes[key]:
                    names = list(map(str, changes[key]))
                    shown = ", ".join(names[:8])
                    more = (f", and {len(names) - 8} more"
                            if len(names) > 8 else "")
                    print(f"  {label:<16}{len(names):>3}  {shown}{more}")
            for ticker, verdict in changes["exited"]:
                print(f"  {'exited':<16}     {ticker}: {verdict}")
            print()

        prices = {}
        for row in pf.all_positions():
            quote = db.latest_price(row["lei"])
            if quote:
                prices[row["lei"]] = quote

        marked = mark(pf, prices)
        print_portfolio(marked)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
