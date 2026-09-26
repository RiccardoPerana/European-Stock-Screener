#!/usr/bin/env python3
"""
Price watch
===========

The gap this closes: "value every company" (write_workbook.py, via Excel
or LibreOffice) only needs to run quarterly, or whenever the financial
statements it reads change -- but the question "did today's price newly
cross a verdict boundary" is cheap to answer far more often than that,
and does not need a recalculation to answer it.

Every screen run already writes trigger_price and watch_price per
company into run_DATE.json's "triggers" block -- fair value divided by
1.25 and 1.20 (pipeline.CompanyResult.trigger_price / .watch_price), the
prices at which today's verdict would read UNDERVALUED.

This compares those stored triggers against whatever price is already in
the cache. No network call happens here -- refreshing that price is the
existing, separate, much cheaper "Update prices" stage
(fetch_prices.py), which touches nothing Excel-related.

WHY "CROSSED" IS NOT EXACT
--------------------------
trigger_price is computed from the LAST screen's fair value. Price is
not actually independent of that fair value: Inputs!B16 (market cap) is
part of the market-value weights the model relevers beta with, so a real
price move shifts fair value too, not just the comparison. watch_price
(a wider margin) exists to buy back that lag -- see
CompanyResult.watch_price. Both are reported here rather than only the
tighter one, so the choice of which to act on stays visible instead of
being made silently in this module.
"""

from __future__ import annotations

import json
from pathlib import Path

from cache import Cache
from track import iter_triggers, latest_run, leis_by_ticker


def check(out_dir: Path, db: Cache, years: list[int]) -> list[dict]:
    """
    Companies whose current cached price has moved past a trigger the
    last screen computed, and that the last screen did not already call
    UNDERVALUED (that is already on the research queue; nothing new to
    say about it here).

    `status` is "crossed" (at or below trigger_price -- the screen would
    read this UNDERVALUED today) or "approaching" (below the wider
    watch_price but not yet at trigger_price).
    """
    run_path = latest_run(out_dir)
    if run_path is None:
        return []
    try:
        doc = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not doc.get("triggers"):
        return []

    by_ticker = leis_by_ticker(db, years)
    out = []
    for lei, ticker, t in iter_triggers(doc, by_ticker.get):
        trigger_price = t.get("trigger_price")
        if not lei or trigger_price is None:
            continue
        if (t.get("verdict") or "").startswith("UNDERVALUED"):
            continue                       # already flagged; nothing new
        current = db.latest_price(lei)
        price = current and current.get("price")
        if price is None:
            continue
        watch_price = t.get("watch_price")
        crossed = price <= trigger_price
        approaching = not crossed and watch_price and price <= watch_price
        if not (crossed or approaching):
            continue
        out.append({
            "ticker": ticker, "lei": lei,
            "price": price, "price_as_of": current.get("as_of"),
            "trigger_price": trigger_price, "watch_price": watch_price,
            "run_price": t.get("price"), "run_verdict": t.get("verdict"),
            "status": "crossed" if crossed else "approaching",
        })
    out.sort(key=lambda r: (r["status"] != "crossed", r["ticker"]))
    return out
