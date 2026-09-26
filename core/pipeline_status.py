#!/usr/bin/env python3
"""
Pipeline state
==============

Where do the companies come from, and when does each part need
refreshing? Eight stages run before a valuation is possible, and each one
goes stale on its own schedule -- statements once a year, prices daily,
Damodaran's industry data annually. Showing all of them is what explains
why the count of companies ready to value is 170 rather than 400, and what
warns you that the credit spreads are fourteen months old.

FRESHNESS
---------
Each stage carries how often it should be redone and when it last was.
The schedule is not arbitrary:

  statements   once a year. Annual reports change once a year; running
               this quarterly re-downloads identical numbers.
  prices       daily to quarterly, depending on whether you are watching
               positions between screens.
  parameters   Damodaran republishes the industry datasets each January.
  tables       the credit spread ladder. Once it passes fourteen months,
               Valuation!D122 puts a WARNING on every workbook, which holds
               every company back from the research queue -- a hard
               deadline rather than a suggestion.
  screen       quarterly, or whenever anything above it changed.

A stage whose inputs were refreshed after it last ran is stale even if its
own date looks recent, which is why `depends_on` exists.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

import config

# How many days before a stage is considered old. None means it never
# expires on its own; it only goes stale when something upstream changes.
SCHEDULE = {
    # Coverage at 180: the stale-scan alert fires once the market coverage
    # scan is over half a year old.
    "coverage": 180, "extract": 365, "identity": 365, "prices": 7,
    "shares": 365, "parameters": 365, "tables": 400, "industries": None,
    "screen": 92,
}


def _mtime(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime,
                                  timezone.utc).date().isoformat()


def _age_days(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (date.today() - date.fromisoformat(iso[:10])).days
    except ValueError:
        return None


def _count(db: Path, sql: str) -> int:
    if not db.exists():
        return 0
    try:
        # closing(), not `with sqlite3.connect(...)`: the latter only
        # manages the transaction and leaves the connection open, and on
        # Windows an open handle locks the database file.
        with closing(sqlite3.connect(db)) as conn:
            return conn.execute(sql).fetchone()[0] or 0
    except sqlite3.Error:
        return 0


def _latest(db: Path, sql: str) -> str | None:
    if not db.exists():
        return None
    try:
        with closing(sqlite3.connect(db)) as conn:
            return conn.execute(sql).fetchone()[0]
    except sqlite3.Error:
        return None


def _latest_date(db: Path, sql: str) -> str | None:
    """Like `_latest`, but trimmed to the calendar date.

    extracted_at/resolved_at/entered_at are full `datetime.isoformat()`
    timestamps; every other stage's `date` (as_of, a vintage, a run
    folder's stamp) is a plain YYYY-MM-DD. Compared as strings, a timestamp
    sorts after a same-day date (it is longer and shares the prefix), which
    would make a dependent stage look stale forever. Truncating keeps every
    stage's `date` on the same day-only footing.
    """
    value = _latest(db, sql)
    return value[:10] if value else None


def stages(db: Path = None, params_path: Path = None,
           tables_path: Path = None, template: Path = None,
           cache_dir: Path = None, out_dir: Path = None) -> list[dict]:
    """Every stage, in order, with its count, its date and its health."""
    db = db or config.DB_PATH
    params_path = params_path or config.PARAMS_PATH
    tables_path = tables_path or config.TABLES_PATH
    template = template or config.TEMPLATE_PATH
    cache_dir = cache_dir or config.ESEF_CACHE_DIR
    out_dir = out_dir or config.OUT_DIR
    years = config.FISCAL_YEARS

    params = {}
    if params_path.exists():
        try:
            params = json.loads(params_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            params = {}

    cached = len(list(cache_dir.glob("*.json"))) if cache_dir.exists() else 0
    # The same rule as Cache.complete_entities(): a usable record for every
    # year. Failed rows (e.g. financials, or years with no filing) do not
    # count.
    entities = _count(db, f"""
        SELECT COUNT(*) FROM (SELECT lei FROM company_year
        WHERE status = 'complete'
          AND fiscal_year IN ({','.join(map(str, years))})
        GROUP BY lei HAVING COUNT(DISTINCT fiscal_year) = {len(years)})""")
    listings = _count(db, "SELECT COUNT(DISTINCT lei) FROM listing "
                          "WHERE is_primary = 1")
    priced = _count(db, "SELECT COUNT(DISTINCT lei) FROM price")
    shares = _count(db, "SELECT COUNT(DISTINCT lei) FROM share_count")
    price_date = _latest(db, "SELECT MAX(as_of) FROM price")
    # When the price job itself last ran, as opposed to what trading day
    # its quotes are dated. The two diverge on a weekend or holiday -- a
    # fetch today stores Friday's close -- so the parent-freshness check
    # below compares on this; `date` (as_of) still drives the display and
    # the 7-day staleness check, which are about the quote's own age.
    price_ran_date = _latest_date(db, "SELECT MAX(fetched_at) FROM price")
    # Each stage's own last-write time, not the shared db file's mtime,
    # which moves on every write to ANY table. See _latest_date.
    extract_date = _latest_date(db, "SELECT MAX(extracted_at) FROM company_year")
    identity_date = _latest_date(db, "SELECT MAX(resolved_at) FROM listing")
    shares_date = _latest_date(db, "SELECT MAX(entered_at) FROM share_count")

    industries = len([k for k in params.get("industries", {})
                      if not k.startswith("_")])
    mapped = len([k for k in params.get("company_industry", {})
                  if not k.startswith("_")])
    missing = [b.get("cell", k) for k, b in
               (params.get("market_wide") or {}).items()
               if isinstance(b, dict) and b.get("value") is None]
    param_date = None
    for block in (params.get("market_wide") or {}).values():
        if isinstance(block, dict) and block.get("as_of"):
            param_date = max(param_date or "", block["as_of"])
    # When the Damodaran INDUSTRY table (B57:B60) was last parsed -- distinct
    # from param_date, which tracks the ECB rate and moves every quarter.
    industries_date = None
    for block in (params.get("industries") or {}).values():
        if isinstance(block, dict) and block.get("as_of"):
            industries_date = max(industries_date or "", block["as_of"])

    vintage = None
    if tables_path.exists():
        try:
            vintage = json.loads(
                tables_path.read_text(encoding="utf-8")).get("vintage")
        except json.JSONDecodeError:
            pass
    if vintage and len(vintage) == 7:
        vintage = f"{vintage}-01"

    runs = sorted(d.name for d in out_dir.glob("*") if d.is_dir()) \
        if out_dir.exists() else []

    excluded = {k for k in (params.get("excluded_companies") or {})
                if not k.startswith("_")}
    # Companies that actually need an industry: in the universe, listed,
    # not excluded. Excluded names have no company_industry entry by
    # design, so counting them would show phantom work to do.
    classifiable = max(listings - len(excluded), 0)

    # Two independent try blocks, not one, so a failure in the mapping
    # count cannot discard an already-correct `ready`.
    ready = 0
    if db.exists() and params:
        try:
            from cache import Cache
            from write_workbook import readiness
            with Cache(db) as c:
                ready = sum(1 for lei in c.complete_entities(years)
                            if lei not in excluded
                            and not readiness(lei, c, params, years))
        except Exception:                            # noqa: BLE001
            ready = 0

    need_mapping = None
    if db.exists() and params:
        try:
            from cache import Cache
            from industry_worksheet import unmapped
            with Cache(db) as c:
                # Exactly what the mapping panel counts, so the stage and
                # the panel can never disagree.
                need_mapping = len(unmapped(
                    c, {**params, "company_industry":
                        params.get("company_industry", {})}, years))
        except Exception:                            # noqa: BLE001
            need_mapping = None

    raw = [
        dict(key="coverage", title="Find the filings",
             blurb="Asks the European filings archive which companies have "
                   "published accounts for all five years.",
             count=cached, unit="countries searched",
             done=cached > 0, date=_mtime(cache_dir), job="coverage"),
        dict(key="extract", title="Read the statements",
             blurb="Pulls the twenty figures the model needs out of each "
                   "filing. A company is only usable with all five years.",
             count=entities, unit="companies with five complete years",
             done=entities > 0, date=extract_date, job="extract",
             depends_on="coverage"),
        dict(key="identity", title="Match to a stock listing",
             blurb="Finds the ticker and exchange each company trades on.",
             count=listings, unit=f"of {entities} have a primary listing",
             done=listings > 0, date=identity_date, job="identity",
             depends_on="extract"),
        dict(key="prices", title="Update prices",
             blurb="One quote per company. Everything downstream compares "
                   "value against this.",
             count=priced, unit=f"of {listings} priced",
             done=priced > 0, date=price_date, ran_date=price_ran_date,
             job="prices", depends_on="identity"),
        dict(key="shares", title="Count the shares",
             blurb="Needed to turn a company's total value into a value "
                   "per share.",
             count=shares, unit=f"of {listings} have a share count",
             done=shares > 0, date=shares_date, job="shares",
             depends_on="identity"),
        dict(key="parameters", title="Damodaran industry data",
             blurb="Industry margins, returns on capital and betas. "
                   "Republished each January.",
             count=industries, unit="industries"
             + (f" — missing {', '.join(missing)}" if missing else ""),
             done=bool(industries) and not missing, date=param_date,
             industries_date=industries_date, job="parameters"),
        dict(key="tables", title="Credit spread ladder",
             blurb="Turns interest cover into a borrowing cost.<br>"
                   "Past fourteen months the workbook warns on every "
                   "company, which empties the research queue.",
             count=0, unit=f"vintage {vintage}" if vintage else "not loaded",
             done=bool(vintage), date=vintage, job=""),
        dict(key="industries", title="Assign each company an industry",
             blurb="Maps every company in the screening universe to one of "
                   "Damodaran's industries. Excluded companies do not need "
                   "one.",
             count=(classifiable - need_mapping
                    if need_mapping is not None else mapped),
             unit=(f"of {classifiable} mapped"
                   + (f", {need_mapping} still to classify"
                      if need_mapping else "")),
             done=((need_mapping == 0) if need_mapping is not None
                   else (listings > 0 and mapped >= classifiable)),
             date=None, job="industries", depends_on="identity"),
        dict(key="screen", title="Value every company",
             blurb="Builds one workbook per company and reads the verdict "
                   "back out of it.",
             count=ready, unit="ready to value",
             done=bool(runs) and template.exists(),
             date=runs[-1] if runs else None, job="screen",
             depends_on="prices"),
    ]

    by_key = {s["key"]: s for s in raw}
    for s in raw:
        s["age_days"] = _age_days(s["date"])
        s["refresh_days"] = SCHEDULE.get(s["key"])
        s["health"] = "missing" if not s["done"] else "ok"
        s["message"] = ""

        if s["done"] and s["refresh_days"] and s["age_days"] is not None:
            if s["age_days"] > s["refresh_days"]:
                s["health"] = "stale"
                s["message"] = (f"last done {s['age_days']} days ago; "
                                f"due every {s['refresh_days']}")

        # Fresher inputs than output: the stage ran, but on older data.
        # Compared on ran_date where a stage has one (currently just
        # prices) rather than `date`: `date` there is the quote's trading
        # day, not when the fetch itself ran, and those two are different
        # things every weekend and holiday.
        parent = by_key.get(s.get("depends_on") or "")
        parent_recency = (parent or {}).get("ran_date") or (parent or {}).get("date")
        my_recency = s.get("ran_date") or s["date"]
        if (s["health"] == "ok" and parent and parent_recency and my_recency
                and parent_recency > my_recency):
            s["health"] = "stale"
            s["message"] = f"‘{parent['title']}’ has been updated since"

    # The credit ladder is the one hard deadline: past it, D122 warns on
    # every workbook and no company can reach the research queue.
    ladder = by_key["tables"]
    if ladder["age_days"] is not None and ladder["age_days"] > 425:
        ladder["health"] = "blocking"
        ladder["message"] = ("older than fourteen months — every valuation "
                             "carries a warning, so nothing can reach the "
                             "research queue until this is refreshed")
    return raw


def _calendar_reminders(by_key: dict, today: date | None = None) -> list[dict]:
    """
    The date-driven maintenance notices: the annual Damodaran refresh and
    the quarterly report-cycle scan. Unlike the
    age-based staleness above, these are triggered by the calendar.
    """
    today = today or date.today()
    out = []

    # Annual: Damodaran republishes the industry datasets and the credit
    # spreads each January. In Jan/Feb, nudge if either is from before this
    # January -- unless the credit ladder is already blocking, which shouts
    # louder on its own.
    if today.month in (1, 2) and by_key.get("tables", {}).get("health") \
            != "blocking":
        jan = f"{today.year}-01"
        ind = by_key.get("parameters", {}).get("industries_date")
        vint = by_key.get("tables", {}).get("date")
        if (not ind or ind[:7] < jan) or (not vint or vint[:7] < jan):
            out.append({
                "key": "damodaran-annual",
                "title": "Damodaran update",
                "health": "stale",
                "message": "New Damodaran sector matrices available from "
                           "NYU Stern. Update required.",
                "job": "parameters"})

    # Quarterly: a new calendar quarter has begun since the last scan.
    # Skipped when the screen is already flagged stale on its own.
    screen = by_key.get("screen", {})
    if screen.get("date") and screen.get("health") == "ok":
        try:
            sd = date.fromisoformat(screen["date"][:10])
            if (today.year, (today.month - 1) // 3) != \
                    (sd.year, (sd.month - 1) // 3):
                out.append({
                    "key": "report-cycle",
                    "title": "New quarter",
                    "health": "stale",
                    "message": "New financial report cycle active. Run "
                               "updated market scan.",
                    "job": "screen"})
        except ValueError:
            pass
    return out


def alerts(steps: list[dict]) -> list[dict]:
    """
    What needs attention, worst first. Shown on the Dashboard as
    notifications; the `job` each one names is fixed from the matching
    stage's button on the Settings page, not from here -- calendar-driven
    maintenance notices (annual Damodaran, quarterly scan) are added here
    alongside the stage-health ones.
    """
    order = {"blocking": 0, "missing": 1, "stale": 2}
    items = [{"key": s["key"], "title": s["title"], "health": s["health"],
              "message": s["message"] or "has never been run",
              "job": s["job"]}
             for s in steps if s["health"] != "ok"]
    items += _calendar_reminders({s["key"]: s for s in steps})
    return sorted(items, key=lambda a: order.get(a["health"], 9))
