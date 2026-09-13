#!/usr/bin/env python3
"""
Portfolio tracker
=================

Records what the screen picked, when, and how those picks have done since.
Separate from the screen on purpose: the screen answers "what looks cheap
today", the tracker answers "was it right last time", and conflating them
means the second question never gets asked honestly.

THE RULES, ALL OF THEM
----------------------
ENTRY     A company entering the research queue that is not already held
          opens a position, at the price the screen valued it against.

EXIT      A held position closes when the verdict is NO LONGER UNDERVALUED.
          Not "when fairly valued": verdicts are only observed quarterly, so
          a company can run from UNDERVALUED to OVERVALUED between two runs
          without ever being seen as fairly valued in between. A rule
          keyed on fair value would hold that position forever, precisely
          when it had become most expensive.

VOID      A held position the model can no longer value is HELD and
          FLAGGED, never sold. A void is a failure to measure -- a missing
          input, a loss-making year -- not a signal about the company.
          Selling on it would mean one absent tag in one filing ejects a
          position for reasons having nothing to do with the business.
          But it must be visible, so it is reported separately rather than
          sitting quietly among the healthy holdings.

WEIGHT    Fixed STAKE per position at entry. Never rebalanced. The
          simplest rule that can be stated in one line and audited.

RE-ENTRY  A company that exits and later becomes undervalued again opens a
          SECOND position. Positions are keyed by position_id, not by
          company, so the record of the first round trip survives intact.

WHAT IS DELIBERATELY NOT MEASURED
---------------------------------
No benchmark. This was a considered decision: the return is reported
without claiming it beat anything. Index history can be backfilled later
if that changes, so nothing is lost by leaving it out now.

Returns are PRICE ONLY and exclude dividends, which understates a
portfolio of European industrials by a few percent a year. Stated plainly
wherever the number is shown, because a return that quietly omits
dividends is overstated by omission -- in the reader's favour, which is
the wrong direction for a track record.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

# Euros committed per position at entry. Never rebalanced.
STAKE = 100.0

UNDERVALUED = "UNDERVALUED - investigate"
VOID_PREFIX = "VOID"

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS position (
    position_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lei                   TEXT NOT NULL,
    ticker                TEXT NOT NULL,
    name                  TEXT,
    exchange              TEXT,
    entry_date            TEXT NOT NULL,
    entry_price           REAL NOT NULL,
    entry_value_per_share REAL,
    entry_upside          REAL,
    units                 REAL NOT NULL,
    last_seen_date        TEXT,
    last_verdict          TEXT,
    last_value_per_share  REAL,
    exit_date             TEXT,
    exit_price            REAL,
    exit_verdict          TEXT,
    status                TEXT NOT NULL DEFAULT 'open'
);

-- One OPEN position per company at a time. Re-entry after an exit is
-- allowed and creates a new row, which is why this is partial.
CREATE UNIQUE INDEX IF NOT EXISTS one_open_per_company
    ON position (lei) WHERE status = 'open';
"""


class Portfolio:
    """Positions, stored alongside the screen's own cache."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        except Exception:
            self.conn.close()
            raise

    def __enter__(self) -> "Portfolio":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None

    # -- meta ----------------------------------------------------------

    @property
    def inception(self) -> str | None:
        """The day the portfolio went live. Shown in small type on the page."""
        row = self.conn.execute(
            "SELECT value FROM portfolio_meta WHERE key = 'inception_date'"
        ).fetchone()
        return row["value"] if row else None

    def set_inception(self, when: str) -> None:
        # Written once, on the first sync, and never overwritten: a moving
        # inception date would silently rewrite the track record.
        self.conn.execute(
            "INSERT OR IGNORE INTO portfolio_meta (key, value) VALUES "
            "('inception_date', ?)", (when,))
        self.conn.commit()

    @property
    def last_sync(self) -> str | None:
        """The date of the most recent screening run applied. Drives the
        'what changed' section: a position whose entry or exit date is this
        one moved at that scan."""
        row = self.conn.execute(
            "SELECT value FROM portfolio_meta WHERE key = 'last_sync_date'"
        ).fetchone()
        return row["value"] if row else None

    def set_last_sync(self, when: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO portfolio_meta (key, value) VALUES "
            "('last_sync_date', ?)", (when,))
        self.conn.commit()

    @property
    def prices_refreshed(self) -> str | None:
        """The date the held positions' prices were last pulled. The daily
        auto-refresh checks this so it fetches once per calendar day."""
        row = self.conn.execute(
            "SELECT value FROM portfolio_meta WHERE key = 'prices_refreshed'"
        ).fetchone()
        return row["value"] if row else None

    def set_prices_refreshed(self, when: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO portfolio_meta (key, value) VALUES "
            "('prices_refreshed', ?)", (when,))
        self.conn.commit()

    # -- reads ---------------------------------------------------------

    def open_positions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM position WHERE status = 'open' "
            "ORDER BY entry_date, ticker").fetchall()

    def closed_positions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM position WHERE status = 'closed' "
            "ORDER BY exit_date, ticker").fetchall()

    def all_positions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM position ORDER BY entry_date, ticker").fetchall()

    def holding(self, lei: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM position WHERE lei = ? AND status = 'open'",
            (lei,)).fetchone()

    # -- writes --------------------------------------------------------

    def enter(self, *, lei: str, ticker: str, name: str, exchange: str,
              when: str, price: float, value_per_share: float | None,
              upside: float | None) -> int:
        """Open a position. STAKE euros buys however many units that is."""
        if price is None or price <= 0:
            raise ValueError(f"{ticker}: cannot enter at price {price!r}")
        cur = self.conn.execute(
            "INSERT INTO position (lei, ticker, name, exchange, entry_date, "
            "entry_price, entry_value_per_share, entry_upside, units, "
            "last_seen_date, last_verdict, last_value_per_share, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'open')",
            (lei, ticker, name, exchange, when, price, value_per_share,
             upside, STAKE / price, when, UNDERVALUED, value_per_share))
        self.conn.commit()
        log.info("entered %s at %.4f on %s", ticker, price, when)
        return cur.lastrowid

    def exit(self, position_id: int, *, when: str, price: float | None,
             verdict: str) -> None:
        self.conn.execute(
            "UPDATE position SET status = 'closed', exit_date = ?, "
            "exit_price = ?, exit_verdict = ? WHERE position_id = ?",
            (when, price, verdict, position_id))
        self.conn.commit()
        log.info("exited position %d: %s", position_id, verdict)

    def observe(self, position_id: int, *, when: str, verdict: str,
                value_per_share: float | None) -> None:
        """Record that a held position was seen, without changing it."""
        self.conn.execute(
            "UPDATE position SET last_seen_date = ?, last_verdict = ?, "
            "last_value_per_share = COALESCE(?, last_value_per_share) "
            "WHERE position_id = ?",
            (when, verdict, value_per_share, position_id))
        self.conn.commit()


# ---------------------------------------------------------------------------
# Applying a screening run
# ---------------------------------------------------------------------------


def sync(pf: Portfolio, summary, today: str | None = None) -> dict:
    """
    Apply one screening run: open new positions, close the ones that no
    longer qualify, and note everything else.

    Takes a RunSummary from pipeline.py, so the tracker never re-derives a
    verdict. The workbook decides; this only records.
    """
    when = today or summary.valuation_date
    pf.set_inception(when)
    pf.set_last_sync(when)

    changes = {"entered": [], "exited": [], "held": [], "unvaluable": [],
               "not_seen": []}
    seen_leis = set()

    for r in summary.companies:
        seen_leis.add(r.lei)
        held = pf.holding(r.lei)

        if held is None:
            # Entry is the research queue, not merely the verdict: a
            # company held back by a warning was not a recommendation.
            if r.research_flag and isinstance(r.current_price, (int, float)):
                pf.enter(lei=r.lei, ticker=r.ticker, name=r.name,
                         exchange=r.exchange, when=when,
                         price=r.current_price,
                         value_per_share=r.value_per_share,
                         upside=r.upside)
                changes["entered"].append(r.ticker)
            continue

        # Already held.
        if r.is_void or str(r.verdict).startswith(VOID_PREFIX):
            # Held and flagged. A void is a failure to measure, not a sell.
            pf.observe(held["position_id"], when=when, verdict=r.verdict,
                       value_per_share=None)
            changes["unvaluable"].append(r.ticker)
        elif r.verdict != UNDERVALUED:
            pf.exit(held["position_id"], when=when, price=r.current_price,
                    verdict=r.verdict)
            changes["exited"].append((r.ticker, r.verdict))
        else:
            pf.observe(held["position_id"], when=when, verdict=r.verdict,
                       value_per_share=r.value_per_share)
            changes["held"].append(r.ticker)

    # A holding that did not appear in this run at all -- dropped out of
    # the universe, or failed before valuation. Held, and reported: an
    # unexplained disappearance must not look like a healthy position.
    for row in pf.open_positions():
        if row["lei"] not in seen_leis:
            changes["not_seen"].append(row["ticker"])

    return changes


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------


def performance_series(pf: Portfolio,
                       history: dict[str, list[tuple[str, float]]]) -> list[dict]:
    """
    The equal-weighted price return of the positions open on each date, one
    point per date a held price is known. It is the header figure carried
    back through time, so the last point equals it. A closed position is
    marked at its exit price up to its exit date and then drops out.

    `history` maps lei -> [(as_of, price), ...] oldest first (Cache.price_history).
    """
    positions = pf.all_positions()
    if not positions:
        return []

    dates: set[str] = set()
    for p in positions:
        dates.add(p["entry_date"])
        if p["exit_date"]:
            dates.add(p["exit_date"])
        for as_of, _ in history.get(p["lei"], []):
            dates.add(as_of)
    today = date.today().isoformat()
    dates = sorted(d for d in dates if d and d <= today)

    out = []
    for d in dates:
        rets = []
        for p in positions:
            if p["entry_date"] > d or not p["entry_price"]:
                continue
            if p["exit_date"] and p["exit_date"] <= d:
                px = p["exit_price"] or p["entry_price"]
            else:
                px = next((pr for as_of, pr
                           in reversed(history.get(p["lei"], []))
                           if as_of <= d), None)
                if px is None:
                    continue
            rets.append(px / p["entry_price"] - 1.0)
        if rets:
            out.append({"date": d, "return_pct": sum(rets) / len(rets),
                        "n": len(rets)})
    return out


def mark(pf: Portfolio, prices: dict[str, dict],
         history: dict[str, list[tuple[str, float]]] | None = None) -> dict:
    """
    Mark every position and total the portfolio.

    `prices` maps lei -> {"price": float, "as_of": str}, straight from the
    screen's own price table. Open positions are marked at the latest
    price; closed positions are locked at their exit price and never move
    again. If `history` is given, a performance_series() is attached.
    """
    rows, invested, current = [], 0.0, 0.0
    missing = []

    for p in pf.all_positions():
        invested += STAKE
        is_open = p["status"] == "open"

        if is_open:
            quote = prices.get(p["lei"]) or {}
            price = quote.get("price")
            as_of = quote.get("as_of")
            if not isinstance(price, (int, float)):
                # No current price: hold the position at cost rather than
                # at zero. Marking an unpriced holding to zero would show
                # a catastrophic loss caused by a data gap.
                price, as_of = p["entry_price"], None
                missing.append(p["ticker"])
        else:
            price, as_of = p["exit_price"], p["exit_date"]
            if not isinstance(price, (int, float)):
                price = p["entry_price"]

        value = p["units"] * price
        current += value

        vps = p["last_value_per_share"]
        upside = ((vps - price) / price
                  if isinstance(vps, (int, float)) and price else None)

        rows.append({
            "position_id": p["position_id"], "ticker": p["ticker"],
            "name": p["name"], "exchange": p["exchange"],
            "entry_date": p["entry_date"], "entry_price": p["entry_price"],
            "status": p["status"], "exit_date": p["exit_date"],
            "exit_verdict": p["exit_verdict"],
            "last_verdict": p["last_verdict"],
            "price": price, "price_as_of": as_of, "value": value,
            "return_pct": (value - STAKE) / STAKE,
            "current_upside": upside,
            "unvaluable": bool(p["last_verdict"]
                               and str(p["last_verdict"]).startswith(VOID_PREFIX)
                               and is_open),
        })

    return {
        "inception_date": pf.inception,
        "last_scan_date": pf.last_sync,
        "as_of": date.today().isoformat(),
        "series": performance_series(pf, history) if history else [],
        "positions": rows,
        "invested": invested,
        "current_value": current,
        "return_pct": (current - invested) / invested if invested else 0.0,
        "open": sum(1 for r in rows if r["status"] == "open"),
        "closed": sum(1 for r in rows if r["status"] == "closed"),
        "missing_price": missing,
        "basis": "price return, excluding dividends",
    }
