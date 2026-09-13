#!/usr/bin/env python3
"""
Price fetcher  --  Inputs!B14
=============================

Fetches one closing price per company for the resolved primary listing.

WHY EOD AND NOT INTRADAY
------------------------
Every other input to the model comes from an annual report. The price is
the only thing that moves daily, and it moves D85 and therefore D86 --
a company that reads FAIRLY VALUED on Friday can read UNDERVALUED on
Monday. For an annual research screen a closing price is ample, and it is
what keeps this free. Intraday would raise the cost floor sharply and add
nothing the research question needs.

SOURCE
------
Yahoo, or a CSV you supply. Twelve Data was removed: a coverage probe over
this universe priced 22 of 23 companies through Yahoo and 0 of 23 through
Twelve Data's free tier, which does not include the European exchanges
this screen depends on. Helsinki alone is a third of the universe and
returned "symbol not found" rather than a paywall message, meaning the
paid tiers may not carry it either.

The Yahoo endpoint is undocumented and not licensed for programmatic
access, which is why --source has no default and must be stated. Keep the
prices local: the publishable artefact is the derived valuation, never the
price series.

THE CURRENCY GATE
-----------------
Inputs!B8 fixes the reporting currency to EUR and Inputs!G14 requires the
price to come from the primary listing IN STATEMENT CURRENCY. A non-EUR
quote is therefore rejected outright rather than converted.

This matters because the workbook cannot catch it. D114 checks that market
cap over revenue lands between 0.05 and 30, which is far too wide: a Polish
listing quoted in zloty at about 4.3 to the euro needs a true ratio under 7
to slip through, and most industrials have one.

USAGE
-----
    python fetch_prices.py --source yahoo --limit 10
    python fetch_prices.py --source yahoo
    python fetch_prices.py --source yahoo --leis LEI1 LEI2 --backfill-since 2026-08-01
    python fetch_prices.py --source csv --csv prices.csv

No key is needed. The CSV path requires an explicit currency column: the
EUR gate below is the only thing between a foreign-currency quote and
Inputs!B14, and a defaulted currency walks straight through it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from cache import Cache
from esef_extract import USER_AGENT

import config

# Free tier is 8 requests/minute. 7.6s between calls leaves headroom for
# clock drift without wasting the day's 800-request budget.

# OpenFIGI hands us Bloomberg exchange codes; MICs are kept for
# reporting and for any future source that speaks them.
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Bloomberg exchange code -> Yahoo suffix.
YAHOO_SUFFIX = {
    "AV": ".VI", "BB": ".BR", "ET": ".TL", "FH": ".HE", "FP": ".PA",
    "GA": ".AT", "GR": ".F", "GY": ".DE", "ID": ".IR", "IM": ".MI",
    "LH": ".VS", "LR": ".RG", "NA": ".AS", "PL": ".LS", "SM": ".MC",
    "SQ": ".MC", "LX": ".LU", "SV": ".LJ", "CZ": ".ZA",
}

BLOOMBERG_TO_MIC = {
    "AV": "XWBO",   # Vienna
    "BB": "XBRU",   # Euronext Brussels
    "CY": "XCYS",   # Cyprus
    "CZ": "XZAG",   # Zagreb
    "ET": "XTAL",   # Nasdaq Tallinn
    "FH": "XHEL",   # Nasdaq Helsinki
    "FP": "XPAR",   # Euronext Paris
    "GA": "XATH",   # Athens
    "GR": "XFRA",   # Frankfurt
    "GY": "XETR",   # Xetra
    "ID": "XDUB",   # Euronext Dublin
    "IM": "XMIL",   # Borsa Italiana
    "LH": "XLIT",   # Nasdaq Vilnius
    "LR": "XRIS",   # Nasdaq Riga
    "LX": "XLUX",   # Luxembourg
    "MV": "XMAL",   # Malta
    "NA": "XAMS",   # Euronext Amsterdam
    "PL": "XLIS",   # Euronext Lisbon
    "SK": "XBRA",   # Bratislava
    "SM": "XMAD",   # BME Madrid
    "SQ": "XMAD",   # BME continuous market -- same venue, second Bloomberg
                    # code. Missing it would have dropped Spanish companies
                    # silently, with no error anywhere.
    "SV": "XLJU",   # Ljubljana
}


def http_json(url: str, params: dict, timeout: int = 30) -> dict:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{query}",
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_yahoo(symbol: str, exchange: str | None) -> tuple[dict, str]:
    """
    Yahoo's chart endpoint. Undocumented and not licensed for programmatic
    access -- selected only via --source yahoo, never by default.
    """
    suffix = YAHOO_SUFFIX.get(exchange or "")
    if suffix is None:
        return {}, f"no Yahoo suffix for exchange {exchange}"
    sym = symbol + suffix
    req = urllib.request.Request(
        YAHOO_URL.format(symbol=urllib.parse.quote(sym)) + "?range=5d&interval=1d",
        headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            doc = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {}, f"HTTP {e.code}"
    except Exception as e:
        return {}, type(e).__name__

    try:
        meta = doc["chart"]["result"][0]["meta"]
        stamp = meta.get("regularMarketTime")
        return {
            "close": meta["regularMarketPrice"],
            "currency": meta.get("currency"),
            "datetime": (datetime.fromtimestamp(stamp, timezone.utc)
                         .date().isoformat()
                         if stamp else date.today().isoformat()),
            "symbol": sym,
        }, "ok"
    except (KeyError, IndexError, TypeError):
        return {}, "unparseable response"


def fetch_yahoo_history(symbol: str, exchange: str | None,
                        since: date) -> tuple[list[tuple[str, float]], str]:
    """
    Daily closes from `since` to today for one listing. Same undocumented
    Yahoo chart endpoint as fetch_yahoo(), asked for a date range instead
    of a single quote. Returns [(YYYY-MM-DD, close), ...] oldest first and
    a status; the status is "ok" only when the quote is in EUR.
    """
    suffix = YAHOO_SUFFIX.get(exchange or "")
    if suffix is None:
        return [], f"no Yahoo suffix for exchange {exchange}"
    sym = symbol + suffix
    p1 = int(datetime(since.year, since.month, since.day,
                      tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.now(timezone.utc).timestamp())
    url = (YAHOO_URL.format(symbol=urllib.parse.quote(sym))
           + f"?period1={p1}&period2={p2}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            doc = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return [], f"HTTP {e.code}"
    except Exception as e:
        return [], type(e).__name__

    try:
        res = doc["chart"]["result"][0]
        currency = (res["meta"].get("currency") or "").upper()
        stamps = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return [], "unparseable response"

    out = []
    for ts, close in zip(stamps, closes):
        if not isinstance(close, (int, float)) or close <= 0:
            continue
        d = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        out.append((d, float(close)))
    return out, ("ok" if currency == "EUR" else f"quoted in {currency}")


def fetch_csv_prices(path: Path) -> dict[str, dict]:
    """
    Manual price file. Always licensed, because you obtained it yourself.

    Columns: lei, price, currency, as_of. A quarterly screen needs 158 rows
    four times a year, which is small enough to export by hand from any
    terminal you have legitimate access to -- unlike the 150,000 statement
    cells, which is why that route was ruled out and this one is not.
    """
    import csv as _csv
    out: dict[str, dict] = {}
    with path.open(encoding="utf-8-sig") as fh:
        reader = _csv.DictReader(fh)
        if "currency" not in (reader.fieldnames or []):
            # Do NOT default to EUR. The EUR gate below is the only thing
            # standing between a GBX-quoted London line and Inputs!B14, and
            # a defaulted currency walks straight through it. Section 11:
            # never invent or substitute financial data.
            raise SystemExit(
                f"{path} has no 'currency' column. Add one -- the price is "
                f"rejected unless it is explicitly EUR, and assuming EUR is "
                f"exactly the silent corruption the currency gate exists to "
                f"prevent.")
        for row in reader:
            lei = (row.get("lei") or "").strip()
            if not lei:
                continue
            out[lei] = {
                "close": float(row["price"]),
                "currency": (row.get("currency") or "").strip().upper(),
                "datetime": (row.get("as_of") or date.today().isoformat()).strip(),
                "symbol": (row.get("ticker") or "").strip() or None,
            }
    return out


def main(on_progress=None) -> int:
    """
    `on_progress(done, total, label)` lets the application draw a progress
    bar. The price fetch used to print its way down 158 companies while the
    bar sat still, because only the screen reported progress -- two long
    jobs behaving differently for no reason a user could see.
    """
    p = argparse.ArgumentParser(
        description="Fetch closing prices for B14.",
        parents=[config.common_args()])
    p.add_argument("--source", choices=("yahoo", "csv"),
                   required=True,
                   help="Price source. Required rather than defaulted: "
                        "'yahoo' uses an undocumented endpoint that is "
                        "not licensed for programmatic access, so it has "
                        "to be an explicit choice, never inherited from "
                        "a default.")
    p.add_argument("--csv", type=Path, default=config.ROOT / "prices.csv",
                   help="For --source csv. Columns: lei, price, currency, as_of")
    p.add_argument("--stale-days", type=int, default=7,
                   help="Flag a quote older than this many days")
    p.add_argument("--leis", nargs="*", default=None, metavar="LEI",
                   help="Restrict to these LEIs instead of every primary "
                        "listing. Used by the daily portfolio price refresh.")
    p.add_argument("--backfill-since", metavar="YYYY-MM-DD", default=None,
                   help="Fetch the daily close series back to this date and "
                        "store every day (per --leis). Fills a portfolio's "
                        "history when the app was not running to catch it.")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--refresh", action="store_true",
                   help="Re-fetch even where today's price is already stored")
    args = p.parse_args()

    csv_prices: dict[str, dict] = {}
    if args.source == "csv":
        if not args.csv.exists():
            sys.exit(f"{args.csv} not found.")
        csv_prices = fetch_csv_prices(args.csv)
        print(f"{len(csv_prices)} price(s) loaded from {args.csv}\n")

    # Yahoo is unmetered but should not be hammered; CSV needs no pacing.
    pause = 0.35 if args.source == "yahoo" else 0.0
    today = date.today().isoformat()
    counts = Counter()
    problems: list[str] = []

    with Cache(args.db) as db:
        # --leis names an explicit subset (the held portfolio); otherwise
        # price every company with five complete years.
        universe = list(args.leis) if args.leis \
            else db.complete_entities(args.years)
        targets = []
        for lei in universe:
            listing = db.primary_listing(lei)
            if listing and listing.get("ticker"):
                targets.append((lei, listing))
            else:
                counts["no_primary_listing"] += 1
        if args.limit:
            targets = targets[: args.limit]

        mins = len(targets) * pause / 60
        scope = "in the portfolio" if args.leis else "with a primary listing"
        rate = f"  (~{mins:.0f} min at {60 / pause:.0f}/min)" if pause else ""
        print(f"{len(targets)} companies {scope}{rate}\n")

        for i, (lei, listing) in enumerate(targets, 1):
            cy = db.get(lei, max(args.years))
            name = (cy.name if cy else lei) or lei
            symbol = listing["ticker"]
            mic = BLOOMBERG_TO_MIC.get(listing.get("exchange") or "")

            print(f"[{i:>4}/{len(targets)}] {name[:38]:<40}"
                  f"{symbol:<10}{mic or '?':<6}", end="", flush=True)

            if args.backfill_since and args.source == "yahoo":
                rows, status = fetch_yahoo_history(
                    symbol, listing.get("exchange"),
                    date.fromisoformat(args.backfill_since))
                time.sleep(pause)
                if status != "ok":
                    counts["fetch_failed"] += 1
                    problems.append(f"{name[:36]:<38}{symbol}: history {status}")
                    print(f"history {status}")
                    continue
                stored = 0
                for d, close in rows:
                    if d < args.backfill_since or d > today:
                        continue
                    db.put_price(lei, d, close, "EUR", symbol, mic, "yahoo")
                    stored += 1
                counts["ok"] += 1
                print(f"backfilled {stored} day(s)")
                continue

            existing = db.latest_price(lei)
            if existing and existing["as_of"] == today and not args.refresh:
                counts["cached"] += 1
                print(f"cached {existing['price']:.2f}")
                continue

            if args.source == "yahoo":
                # Keyed on the Bloomberg code, not the MIC: Yahoo uses its
                # own suffixes (.HE, .MI, .VS) and has no notion of a MIC.
                doc, status = fetch_yahoo(symbol, listing.get("exchange"))
            elif args.source == "csv":
                doc = csv_prices.get(lei, {})
                status = "ok" if doc else "not in CSV"
            else:
                # Never fall through to a default source. An earlier version
                # used `else: csv`, so --source yahoo silently fetched from
                # an empty CSV and reported "not in CSV" 158 times without
                # once mentioning that the requested source was ignored.
                raise SystemExit(f"unhandled --source {args.source!r}")
            time.sleep(pause)

            if status != "ok":
                counts["fetch_failed"] += 1
                problems.append(f"{name[:36]:<38}{symbol}.{mic}: {status}")
                print(status)
                continue

            currency = (doc.get("currency") or "").upper()
            if currency != "EUR":
                # Rejected, not converted. Inputs!B8 fixes EUR and the model
                # performs no currency conversion; a converted price would
                # also need a rate as of the same instant to be meaningful.
                counts["wrong_currency"] += 1
                problems.append(f"{name[:36]:<38}{symbol}.{mic}: "
                                f"quoted in {currency}, not EUR")
                print(f"REJECTED: {currency}")
                continue

            price = float(doc["close"])
            as_of = doc.get("datetime") or today

            # A zero or negative price is not a price. IEP Invest came back
            # at 0.00 from July: D85 is (value - price) / price, so a zero
            # divides by zero and a near-zero produces an upside in the
            # thousands of percent. Nothing in the workbook rejects it.
            if price <= 0:
                counts["zero_price"] += 1
                problems.append(f"{name[:36]:<38}{symbol}: "
                                f"price {price} as of {as_of}")
                print(f"REJECTED: price {price}")
                continue

            # Stale quotes are kept but flagged. A thin Austrian small cap
            # last trading a fortnight ago is still valuable information;
            # it just is not today's market view, and D85 will read as if
            # it were.
            try:
                age = (date.today() - date.fromisoformat(as_of)).days
            except ValueError:
                age = 0
            if age > args.stale_days:
                counts["stale"] += 1
                problems.append(f"{name[:36]:<38}{symbol}: last traded "
                                f"{as_of} ({age} days ago)")
            db.put_price(lei, as_of, price, currency,
                         doc.get("symbol") or symbol, mic, args.source)
            counts["ok"] += 1
            print(f"{price:>10,.2f} EUR  {as_of}")

        print("\n" + "=" * 66)
        print("PRICE FETCH")
        print("=" * 66)
        for key in ("ok", "cached", "fetch_failed", "wrong_currency",
                    "zero_price", "stale", "no_mic", "no_primary_listing"):
            if counts[key]:
                print(f"  {key:<22}{counts[key]:>6,}")

        priced = counts["ok"] + counts["cached"]
        print(f"\n  Priced: {priced:,} of {len(targets):,}")

        if problems:
            print(f"\n  PROBLEMS ({len(problems)}):")
            for line in problems[:25]:
                print(f"    {line}")
            if len(problems) > 25:
                print(f"    ... and {len(problems) - 25} more")
            print("\n  A ticker that misses is usually a symbology mismatch:")
            print(f"  OpenFIGI's symbol and {args.source}'s can differ on the")
            print("  same listing. Fix those in listing_overrides.json.")

        # Sanity: price x EPS-derived shares against revenue. This is the
        # same ratio D114 checks, computed here so a bad ticker is caught
        # BEFORE the workbook, not after.
        print("\n" + "-" * 66)
        print("IMPLIED MARKET CAP SANITY  (D114 pre-check)")
        print("-" * 66)
        bad_shares, real_outliers = [], []
        for lei, _ in targets:
            pr = db.latest_price(lei)
            est = db.share_estimate(lei, max(args.years))
            cy = db.get(lei, max(args.years))
            if not (pr and est and cy and est.get("wavg_diluted")):
                continue
            revenue = cy.facts["revenue"].value
            if not revenue:
                continue
            shares = est["wavg_diluted"]
            ratio = (pr["price"] * shares) / revenue
            label = (cy.name or lei)[:36]

            # Two very different causes produce an out-of-band ratio, and
            # they need opposite responses.
            #
            # A NEGATIVE or absurd share count means the EPS derivation
            # broke -- net income and EPS had different numerators, which
            # happens when EPS covers continuing operations only. That is
            # our bug and the company is fine.
            #
            # A clinical-stage biotech with no revenue and a real market
            # cap is genuinely out of band. D114 will fail it, correctly:
            # the model cannot value a company with no revenue to normalise.
            if shares <= 0 or not 1e5 <= shares <= 1e11:
                bad_shares.append(f"{label:<38}derived shares "
                                  f"{shares:,.0f}  (EPS derivation failed)")
            elif not 0.05 <= ratio <= 30:
                real_outliers.append(f"{label:<38}cap/revenue "
                                     f"{ratio:>9,.2f}")

        if bad_shares:
            print(f"\n  DERIVED SHARE COUNT UNUSABLE ({len(bad_shares)}):")
            for line in bad_shares:
                print(f"    {line}")
            print("    Net income and EPS did not share a numerator. B15")
            print("    cannot come from EPS for these companies.")
        if real_outliers:
            print(f"\n  GENUINELY OUTSIDE THE BAND ({len(real_outliers)}):")
            for line in real_outliers:
                print(f"    {line}")
            print("    D114 will fail these, correctly -- no revenue to")
            print("    normalise means the model has nothing to work with.")
        if not bad_shares and not real_outliers:
            print("    all within the band")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
