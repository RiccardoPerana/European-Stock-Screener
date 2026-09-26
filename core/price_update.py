#!/usr/bin/env python3
"""
Between-screens price update
============================

The quarterly screen (pipeline.py -> portfolio.sync) is the only thing
that values a company. This module answers a narrower question on the
days in between: given the fair value the LAST screen computed and
today's price, should the portfolio open or close a position now, rather
than waiting up to three months for the next screen to notice?

It re-uses the stored value_per_share from run_DATE.json. It never opens
the workbook, and it never overrides a verdict the screen actually
recorded -- the next screen's sync() is still authoritative, and will
close anything this opened that the screen no longer calls UNDERVALUED.

THE RULES
---------
ENTRY   A company not held, whose last screen found it valuable (not
        VOID) and free of any check that would suppress the research
        flag, enters when price <= value / (1 + UNDERVALUED_THRESHOLD):
        the same point at which D86 would read UNDERVALUED.

EXIT    A held position exits when price >= value * (1 + EXIT_AT_UPSIDE),
        i.e. with the default 0.0, once the price reaches fair value --
        the README's "exit target". Deliberately WIDER than the entry
        point: a price checked daily that sits near value / 1.25 would
        otherwise open and close a new position every other day, and
        each round trip is a permanent row in the track record.

VOID    A held position the model could not value is never sold on
        price, for the same reason portfolio.sync() never sells it.

STALE   A quote older than MAX_QUOTE_AGE_DAYS is ignored: acting on a
        thin stock's fortnight-old close would be acting on nothing.

Fair value is not independent of price (see pipeline.WATCH_MARGIN): a
lower price shifts the WACC weights and nudges value up a few percent.
So a price-driven entry fires slightly late, never early.
"""

from __future__ import annotations

from datetime import date

from cache import Cache
from pipeline import UNDERVALUED_THRESHOLD
from portfolio import VOID_PREFIX, Portfolio
from track import iter_triggers, leis_by_ticker

# Exit once upside falls to this. 0.0 = price reached fair value; -0.25
# would hold until the price-implied verdict reads OVERVALUED.
EXIT_AT_UPSIDE = 0.0

MAX_QUOTE_AGE_DAYS = 7

EXIT_VERDICT = "PRICE AT FAIR VALUE - exited between screens"


def _fresh_quote(db: Cache, lei: str, today: date) -> dict | None:
    q = db.latest_price(lei)
    if not q or not isinstance(q.get("price"), (int, float)) or q["price"] <= 0:
        return None
    try:
        age = (today - date.fromisoformat(q["as_of"])).days
    except (TypeError, ValueError):
        return None
    return q if age <= MAX_QUOTE_AGE_DAYS else None


def apply_prices(pf: Portfolio, run_doc: dict, db: Cache, years: list[int],
                 today: date | None = None) -> dict:
    """
    Open and close positions on today's cached prices against the last
    screen's fair values. Returns {"entered": [...], "exited": [...],
    "skipped_old_run": bool}.
    """
    today = today or date.today()
    by_ticker = leis_by_ticker(db, years)
    changes = {"entered": [], "exited": [], "skipped_old_run": False}

    # -- exits -------------------------------------------------------------
    for row in pf.open_positions():
        if str(row["last_verdict"] or "").startswith(VOID_PREFIX):
            continue
        value = row["last_value_per_share"]
        if not isinstance(value, (int, float)) or value <= 0:
            continue
        q = _fresh_quote(db, row["lei"], today)
        if q is None:
            continue
        upside = value / q["price"] - 1.0
        if upside <= EXIT_AT_UPSIDE:
            pf.exit(row["position_id"], when=q["as_of"], price=q["price"],
                    verdict=EXIT_VERDICT)
            changes["exited"].append((row["ticker"], f"{upside:+.1%}"))

    # -- entries -----------------------------------------------------------
    for lei, ticker, t in iter_triggers(run_doc, by_ticker.get):
        if "blocking_checks" not in t:
            # A run file written before the policy outcome was recorded:
            # no way to tell a clean company from a suppressed one, so
            # enter nothing rather than guess.
            changes["skipped_old_run"] = True
            break
        if t.get("void") or t["blocking_checks"]:
            continue
        value = t.get("value_per_share")
        if not lei or not isinstance(value, (int, float)) or value <= 0:
            continue
        if pf.holding(lei) is not None:
            continue
        q = _fresh_quote(db, lei, today)
        if q is None:
            continue
        upside = value / q["price"] - 1.0
        if upside > UNDERVALUED_THRESHOLD:
            listing = db.primary_listing(lei) or {}
            cy = db.get(lei, max(years))
            pf.enter(lei=lei, ticker=ticker,
                     name=(cy.name if cy else ticker) or ticker,
                     exchange=listing.get("exchange") or "",
                     when=q["as_of"], price=q["price"],
                     value_per_share=value, upside=upside)
            changes["entered"].append((ticker, f"{upside:+.1%}"))

    pf.set_prices_refreshed(today.isoformat())
    return changes
