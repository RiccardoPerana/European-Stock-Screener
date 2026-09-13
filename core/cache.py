"""
Company-year financial cache.
=============================

Local SQLite store for extracted financials, keyed by (LEI, fiscal year).

WHY THIS EXISTS
---------------
Extraction is expensive and coverage is moving. The archive currently holds
a complete five-year run for ~426 euro-area entities, but several countries
(Spain, Greece, Malta, Luxembourg) have a healthy 2021-2024 history and a
stalled 2025 feed. When those years land, the universe roughly doubles.

Without a cache, every re-run re-parses everything. With one, a re-run
extracts only what is new. The cache is keyed by company-year rather than
by company precisely because coverage arrives one year at a time.

WHY A LONG FACT TABLE
---------------------
Section 11 of the brief requires, for every value written: source, source
field, fiscal period, retrieval timestamp, units, reported-vs-calculated
flag, transformations and validation status. Twenty wide columns cannot
carry that. One row per (company, year, field) can, and it makes the
question that actually matters -- "which fields fail most often, and where"
-- a one-line query instead of a data-munging exercise.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
No valuation logic. No workbook writing. No network. This layer stores what
somebody else extracted and answers questions about completeness. Keeping
it dumb is what lets the ESEF extractor, a FactSet export and hand-typed
figures all populate the same store without knowing about each other.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from fields import (
    ALL_FIELDS,
    BY_KEY,
    FIELD_KEYS,
    REQUIRED_KEYS,
    Absence,
    apply_sign,
)

SCHEMA_VERSION = 3

# Version each table SEPARATELY.
#
# The first cut kept one global version and dropped every derived table
# whenever it changed. Adding the `price` table then bumped that global
# number, which silently destroyed 158 resolved listings that had taken a
# rate-limited half-hour of OpenFIGI calls to build. Adding a table must
# not disturb the tables next to it.
#
# A table is now rebuilt only when ITS OWN schema changed.
TABLE_VERSIONS = {
    "company_year":   1,
    "fact":           2,   # sign_flipped added
    "listing":        2,   # re-keyed on figi; is_primary, confidence added
    "share_estimate": 1,
    "price":          1,
    "share_count":    1,
}

# Cheap to rebuild from an external source, so these are dropped and
# recreated when their own version moves. `company_year` and `fact` are not:
# re-extracting 1,675 filings costs an hour, so those get additive column
# migrations instead.
DERIVED_TABLES = ("listing", "share_estimate", "price")
# share_count is NOT derived: it holds hand-entered figures
# that would be lost forever if a schema change dropped it.

# Provenance vocabulary. Kept as plain strings rather than an Enum so the
# database stays readable in any SQLite browser without this module.
DERIVATIONS = {
    "reported",       # single tag, taken as-is
    "summed",         # composite of several tags (see components)
    "extension",      # matched a company-defined element by local name
    "computed",       # derived from other fields, e.g. EBIT fallback
    "debt_proxy",     # borrowings not tagged on the balance sheet; total
                      # non-current liabilities used instead (see
                      # derive_debt_proxy in esef_extract.py)
    "assumed_zero",   # not found, and the field permits absence
    "manual",         # typed in by a human from the annual report
}

SOURCES = {"esef", "factset", "manual"}

STATUSES = {
    "complete",   # all 20 fields resolved (assumed zeros permitted)
    "partial",    # at least one REQUIRED field unresolved
    "failed",     # extraction errored out
}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_year (
    lei               TEXT    NOT NULL,
    fiscal_year       INTEGER NOT NULL,
    name              TEXT,
    country           TEXT,
    period_end        TEXT,
    source            TEXT    NOT NULL,
    source_ref        TEXT,
    extractor_version TEXT    NOT NULL,
    extracted_at      TEXT    NOT NULL,
    status            TEXT    NOT NULL,
    currency          TEXT,
    scale             INTEGER,
    PRIMARY KEY (lei, fiscal_year)
);

CREATE TABLE IF NOT EXISTS fact (
    lei         TEXT    NOT NULL,
    fiscal_year INTEGER NOT NULL,
    field       TEXT    NOT NULL,
    value       REAL,
    element     TEXT,
    derivation  TEXT    NOT NULL,
    components  TEXT,
    sign_flipped INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (lei, fiscal_year, field),
    FOREIGN KEY (lei, fiscal_year)
        REFERENCES company_year (lei, fiscal_year) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS listing (
    lei           TEXT NOT NULL,
    isin          TEXT NOT NULL,
    ticker        TEXT,
    exchange      TEXT,
    figi          TEXT,
    security_type TEXT,
    market_sector TEXT,
    name          TEXT,
    is_primary    INTEGER NOT NULL DEFAULT 0,
    confidence    TEXT    NOT NULL DEFAULT 'isin',
    resolved_at   TEXT NOT NULL,
    PRIMARY KEY (lei, figi)
);
-- Keyed on FIGI, not ISIN. One ISIN routinely maps to SEVERAL listings:
-- Kesko's B share trades in Helsinki and Frankfurt under one ISIN, and
-- Inputs!G14 wants the price from the PRIMARY listing specifically. An
-- ISIN-keyed table would silently keep whichever line was inserted last,
-- which is how a company ends up valued against a foreign secondary quote.

CREATE TABLE IF NOT EXISTS share_estimate (
    lei             TEXT    NOT NULL,
    fiscal_year     INTEGER NOT NULL,
    net_income      REAL,
    basic_eps       REAL,
    diluted_eps     REAL,
    wavg_basic      REAL,
    wavg_diluted    REAL,
    dilution_factor REAL,
    source          TEXT    NOT NULL,
    PRIMARY KEY (lei, fiscal_year)
);

CREATE TABLE IF NOT EXISTS price (
    lei        TEXT NOT NULL,
    as_of      TEXT NOT NULL,
    price      REAL NOT NULL,
    currency   TEXT NOT NULL,
    symbol     TEXT,
    mic        TEXT,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (lei, as_of)
);

CREATE TABLE IF NOT EXISTS share_count (
    lei        TEXT NOT NULL,
    as_of      TEXT NOT NULL,
    shares     REAL NOT NULL,
    source     TEXT NOT NULL,
    note       TEXT,
    entered_at TEXT NOT NULL,
    PRIMARY KEY (lei, as_of)
);

CREATE INDEX IF NOT EXISTS idx_listing_lei ON listing (lei);
CREATE INDEX IF NOT EXISTS idx_fact_field  ON fact (field, derivation);
CREATE INDEX IF NOT EXISTS idx_cy_status   ON company_year (status);
CREATE INDEX IF NOT EXISTS idx_cy_country  ON company_year (country);
"""


@dataclass
class Fact:
    """One extracted value with the provenance needed to defend it."""

    field: str
    value: float | None
    derivation: str
    element: str | None = None
    components: list[str] = dc_field(default_factory=list)
    sign_flipped: bool = False
    """True if the field's sign rule changed the extracted value.

    Orthogonal to `derivation`: a composite field can be both summed and
    sign-flipped, and capex is exactly that case. Folding the flip into
    the derivation string loses one of the two, and the flip is the one
    no workbook check can catch.
    """

    def __post_init__(self) -> None:
        if self.field not in BY_KEY:
            raise ValueError(f"unknown field {self.field!r}")
        if self.derivation not in DERIVATIONS:
            raise ValueError(f"unknown derivation {self.derivation!r}")


@dataclass
class CompanyYear:
    """One company's figures for one fiscal year, plus how they were obtained."""

    lei: str
    fiscal_year: int
    source: str
    extractor_version: str
    name: str | None = None
    country: str | None = None
    period_end: str | None = None
    source_ref: str | None = None
    currency: str = "EUR"
    scale: int = 1_000_000
    """Divisor applied to raw figures. Inputs!B9 is fixed to Millions, and
    filings report in units, so raw values are divided by 1e6 before write."""
    facts: dict[str, Fact] = dc_field(default_factory=dict)
    likely_financial: bool = False
    """Set by the extractor's balance-sheet heuristic. Not persisted --
    these company-years are recorded as failed with a stated reason."""

    blocked: list[str] = dc_field(default_factory=list)
    """Reasons this company-year is unusable despite having all 20 cells.

    Some fields are only optional CONDITIONALLY. Interest expense may
    legitimately be zero for a debt-free company, and must never be zero for
    one carrying debt -- the assumption is invisible in the cell count but
    fatal in the valuation. A blocked year reports as `partial` so it never
    reaches the workbook, and the reason travels with it."""

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"unknown source {self.source!r}")

    # -- construction helpers ------------------------------------------

    def set(self, key: str, value: float | None, derivation: str,
            element: str | None = None,
            components: Iterable[str] = ()) -> None:
        """Record one field, applying its sign convention automatically."""
        f = BY_KEY[key]
        flipped = False
        if value is not None:
            adjusted = apply_sign(f, value)
            # Surface the transformation rather than performing it silently.
            flipped = adjusted != value
            value = adjusted
        self.facts[key] = Fact(key, value, derivation, element,
                               list(components), flipped)

    def fill_permitted_zeros(self) -> list[str]:
        """
        Set unresolved ZERO_IF_ABSENT fields to 0.0.

        Call this once, AFTER extraction has done its best. Returns the keys
        it filled so the caller can log them. Required fields are never
        touched -- a gap there is an extraction failure and must stay a gap.
        """
        filled = []
        for f in ALL_FIELDS:
            if f.absence is not Absence.ZERO_IF_ABSENT:
                continue
            existing = self.facts.get(f.key)
            if existing is None or existing.value is None:
                self.facts[f.key] = Fact(f.key, 0.0, "assumed_zero")
                filled.append(f.key)
        return filled

    # -- inspection ----------------------------------------------------

    @property
    def missing_required(self) -> list[str]:
        """Required fields that did not resolve. Non-empty means unusable."""
        return [
            k for k in REQUIRED_KEYS
            if k not in self.facts or self.facts[k].value is None
        ]

    @property
    def assumed_zeros(self) -> list[str]:
        return sorted(
            k for k, fa in self.facts.items()
            if fa.derivation == "assumed_zero"
        )

    @property
    def status(self) -> str:
        if self.missing_required or self.blocked:
            return "partial"
        return "complete"

    def scaled_values(self) -> dict[str, float]:
        """Field values divided by `scale`, ready for the workbook."""
        return {
            k: fa.value / self.scale
            for k, fa in self.facts.items() if fa.value is not None
        }


class Cache:
    """SQLite-backed company-year store. Use as a context manager."""

    def __init__(self, path: Path | str = "financials.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def _migrate(self) -> None:
        """
        Bring an existing database up to the current schema.

        CREATE TABLE IF NOT EXISTS silently does nothing when the table is
        already there, so a database written by an older version keeps its
        old columns and the first INSERT fails on the new ones. Worse, the
        listing table's primary key changed from (lei, isin) to (lei, figi),
        and SQLite cannot alter a primary key in place.

        Derived tables are therefore dropped outright. They are rebuilt from
        GLEIF and OpenFIGI in minutes; the extracted financials are not, so
        those are left alone.
        """
        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, "
            "value TEXT NOT NULL);")
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        global_version = int(row[0]) if row else 0

        existing = {
            r[0] for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }

        def table_version(table: str) -> int:
            r = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (f"version:{table}",)).fetchone()
            if r:
                return int(r[0])
            # No per-table record: infer from the old global number so an
            # existing database is not needlessly rebuilt on first upgrade.
            if table == "listing":
                return 2 if global_version >= 2 else 1
            if table == "fact":
                return 2 if global_version >= 1 else 1
            return 1

        for table, want in TABLE_VERSIONS.items():
            have = table_version(table)
            if table in existing and have < want:
                if table in DERIVED_TABLES:
                    self._conn.execute(f"DROP TABLE {table}")
                elif table == "fact":
                    cols = {r[1] for r in
                            self._conn.execute("PRAGMA table_info(fact)")}
                    if "sign_flipped" not in cols:
                        self._conn.execute(
                            "ALTER TABLE fact ADD COLUMN sign_flipped "
                            "INTEGER NOT NULL DEFAULT 0")
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (f"version:{table}", str(want)))

        # Safety net for databases written before per-table versioning: the
        # fact table may lack the column regardless of what versions say.
        if "fact" in existing:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(fact)")}
            if "sign_flipped" not in cols:
                self._conn.execute(
                    "ALTER TABLE fact ADD COLUMN sign_flipped "
                    "INTEGER NOT NULL DEFAULT 0")
        self._conn.commit()

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One atomic write. A half-written company-year is worse than none."""
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # -- writing -------------------------------------------------------

    def put(self, cy: CompanyYear) -> None:
        """Insert or replace one company-year and all its facts."""
        with self._tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO company_year
                   (lei, fiscal_year, name, country, period_end, source,
                    source_ref, extractor_version, extracted_at, status,
                    currency, scale)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cy.lei, cy.fiscal_year, cy.name, cy.country, cy.period_end,
                 cy.source, cy.source_ref, cy.extractor_version,
                 datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 cy.status, cy.currency, cy.scale),
            )
            # Replace wholesale rather than merging: a re-extraction is a new
            # opinion about the whole year, and leaving stale facts behind
            # would silently mix two extractor versions in one company-year.
            conn.execute(
                "DELETE FROM fact WHERE lei = ? AND fiscal_year = ?",
                (cy.lei, cy.fiscal_year),
            )
            conn.executemany(
                """INSERT INTO fact
                   (lei, fiscal_year, field, value, element, derivation,
                    components, sign_flipped)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [
                    (cy.lei, cy.fiscal_year, fa.field, fa.value, fa.element,
                     fa.derivation,
                     json.dumps(fa.components) if fa.components else None,
                     int(fa.sign_flipped))
                    for fa in cy.facts.values()
                ],
            )

    def mark_failed(self, lei: str, fiscal_year: int, source: str,
                    extractor_version: str, reason: str) -> None:
        """
        Record a failed extraction so it is not retried blindly every run.

        Failures are as worth caching as successes: without this, a company
        whose filing cannot be parsed gets re-downloaded and re-parsed on
        every pass, forever.
        """
        with self._tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO company_year
                   (lei, fiscal_year, source, source_ref, extractor_version,
                    extracted_at, status)
                   VALUES (?,?,?,?,?,?, 'failed')""",
                (lei, fiscal_year, source, reason, extractor_version,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            conn.execute("DELETE FROM fact WHERE lei = ? AND fiscal_year = ?",
                         (lei, fiscal_year))

    # -- reading -------------------------------------------------------

    def has(self, lei: str, fiscal_year: int,
            extractor_version: str | None = None) -> bool:
        """
        True if this company-year is cached and usable.

        Pass `extractor_version` to invalidate rows produced by an older
        extractor -- when the field mapping improves, previously partial
        years deserve another attempt.
        """
        sql = ("SELECT status, extractor_version FROM company_year "
               "WHERE lei = ? AND fiscal_year = ?")
        row = self._conn.execute(sql, (lei, fiscal_year)).fetchone()
        if row is None or row["status"] == "failed":
            return False
        if extractor_version and row["extractor_version"] != extractor_version:
            return False
        return True

    def missing_years(self, lei: str, years: Iterable[int],
                      extractor_version: str | None = None) -> list[int]:
        """Which of `years` still need extracting for this company."""
        return [y for y in years if not self.has(lei, y, extractor_version)]

    def get(self, lei: str, fiscal_year: int) -> CompanyYear | None:
        row = self._conn.execute(
            "SELECT * FROM company_year WHERE lei = ? AND fiscal_year = ?",
            (lei, fiscal_year),
        ).fetchone()
        if row is None:
            return None

        cy = CompanyYear(
            lei=row["lei"], fiscal_year=row["fiscal_year"],
            source=row["source"], extractor_version=row["extractor_version"],
            name=row["name"], country=row["country"],
            period_end=row["period_end"], source_ref=row["source_ref"],
            currency=row["currency"] or "EUR",
            scale=row["scale"] or 1_000_000,
        )
        for f in self._conn.execute(
            "SELECT * FROM fact WHERE lei = ? AND fiscal_year = ?",
            (lei, fiscal_year),
        ):
            cy.facts[f["field"]] = Fact(
                field=f["field"], value=f["value"], derivation=f["derivation"],
                element=f["element"],
                components=json.loads(f["components"]) if f["components"] else [],
                sign_flipped=bool(f["sign_flipped"]),
            )
        return cy

    def complete_entities(self, years: Iterable[int]) -> list[str]:
        """
        LEIs holding a usable record for EVERY year in `years`.

        This is the screenable universe: only these can satisfy D111.
        """
        years = list(years)
        rows = self._conn.execute(
            f"""SELECT lei FROM company_year
                WHERE status = 'complete'
                  AND fiscal_year IN ({','.join('?' * len(years))})
                GROUP BY lei HAVING COUNT(DISTINCT fiscal_year) = ?""",
            (*years, len(years)),
        )
        return [r["lei"] for r in rows]

    # -- diagnostics ---------------------------------------------------

    def field_failure_report(self) -> list[tuple[str, int, int]]:
        """
        Per field: (key, unresolved count, assumed-zero count).

        This is the number that tells you where the extractor is weak and
        which taxonomy elements need more candidates. Sorted worst first.
        """
        unresolved = Counter()
        assumed = Counter()
        total_years = self._conn.execute(
            "SELECT COUNT(*) c FROM company_year WHERE status != 'failed'"
        ).fetchone()["c"]

        for row in self._conn.execute(
            "SELECT field, derivation, value, COUNT(*) n FROM fact "
            "GROUP BY field, derivation, value IS NULL"
        ):
            if row["value"] is None:
                unresolved[row["field"]] += row["n"]
            elif row["derivation"] == "assumed_zero":
                assumed[row["field"]] += row["n"]

        seen = Counter()
        for row in self._conn.execute(
            "SELECT field, COUNT(*) n FROM fact GROUP BY field"
        ):
            seen[row["field"]] = row["n"]

        out = []
        for key in FIELD_KEYS:
            # A field absent from the fact table entirely is unresolved for
            # every company-year, which a naive GROUP BY would miss.
            never_recorded = total_years - seen[key]
            out.append((key, unresolved[key] + never_recorded, assumed[key]))
        return sorted(out, key=lambda t: (-t[1], -t[2]))

    def unresolved_company_years(self, field: str) -> list[tuple[str, int]]:
        """
        (LEI, year) pairs where `field` did not resolve.

        Lets the concept survey draw its sample from known failures instead
        of the whole universe. Surveying 60 random filings when a field
        fails 16% of the time spends most of the run on companies that
        already work.
        """
        rows = self._conn.execute(
            """SELECT cy.lei, cy.fiscal_year
               FROM company_year cy
               LEFT JOIN fact f
                 ON f.lei = cy.lei AND f.fiscal_year = cy.fiscal_year
                AND f.field = ?
               WHERE cy.status != 'failed'
                 AND (f.value IS NULL OR f.field IS NULL)""",
            (field,),
        )
        return [(r["lei"], r["fiscal_year"]) for r in rows]

    def stats(self) -> dict:
        def scalar(sql: str, *args) -> int:
            return self._conn.execute(sql, args).fetchone()[0]

        return {
            "company_years": scalar("SELECT COUNT(*) FROM company_year"),
            "complete": scalar(
                "SELECT COUNT(*) FROM company_year WHERE status='complete'"),
            "partial": scalar(
                "SELECT COUNT(*) FROM company_year WHERE status='partial'"),
            "failed": scalar(
                "SELECT COUNT(*) FROM company_year WHERE status='failed'"),
            "entities": scalar("SELECT COUNT(DISTINCT lei) FROM company_year"),
            "facts": scalar("SELECT COUNT(*) FROM fact"),
            "assumed_zeros": scalar(
                "SELECT COUNT(*) FROM fact WHERE derivation='assumed_zero'"),
        }

    # -- identifiers and share counts ----------------------------------

    def put_listings(self, lei: str, rows: list[dict]) -> None:
        """Replace all known listings for one entity."""
        with self._tx() as conn:
            conn.execute("DELETE FROM listing WHERE lei = ?", (lei,))
            conn.executemany(
                """INSERT OR REPLACE INTO listing
                   (lei, isin, ticker, exchange, figi, security_type,
                    market_sector, name, is_primary, confidence, resolved_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [(lei, r["isin"], r.get("ticker"), r.get("exchange"),
                  r.get("figi") or f"{r['isin']}:{r.get('exchange') or '?'}",
                  r.get("security_type"),
                  r.get("market_sector"), r.get("name"),
                  int(bool(r.get("is_primary"))), r.get("confidence", "isin"),
                  datetime.now(timezone.utc).isoformat(timespec="seconds"))
                 for r in rows],
            )

    def primary_listing(self, lei: str) -> dict | None:
        """The one listing Inputs!B14 should be priced from."""
        row = self._conn.execute(
            "SELECT * FROM listing WHERE lei = ? AND is_primary = 1 LIMIT 1",
            (lei,)).fetchone()
        return dict(row) if row else None

    def listings(self, lei: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM listing WHERE lei = ? ORDER BY isin, exchange",
            (lei,))]

    def put_price(self, lei: str, as_of: str, price: float, currency: str,
                  symbol: str | None, mic: str | None, source: str) -> None:
        """Store one closing price. Keyed by date so history accumulates."""
        with self._tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO price
                   (lei, as_of, price, currency, symbol, mic, source, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (lei, as_of, price, currency, symbol, mic, source,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )

    def latest_price(self, lei: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM price WHERE lei = ? ORDER BY as_of DESC LIMIT 1",
            (lei,)).fetchone()
        return dict(row) if row else None

    def price_history(self, lei: str) -> list[tuple[str, float]]:
        """Every stored (as_of, price) for a company, oldest first. Used to
        chart portfolio performance over time."""
        return [(r["as_of"], r["price"]) for r in self._conn.execute(
            "SELECT as_of, price FROM price WHERE lei = ? ORDER BY as_of",
            (lei,)).fetchall()]

    def put_share_count(self, lei: str, as_of: str, shares: float,
                        source: str, note: str | None = None) -> None:
        """Store an authoritative share count for Inputs!B15."""
        with self._tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO share_count
                   (lei, as_of, shares, source, note, entered_at)
                   VALUES (?,?,?,?,?,?)""",
                (lei, as_of, shares, source, note,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))

    def latest_share_count(self, lei: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM share_count WHERE lei = ? ORDER BY as_of DESC "
            "LIMIT 1", (lei,)).fetchone()
        return dict(row) if row else None

    def put_share_estimate(self, lei: str, fiscal_year: int, **kw) -> None:
        """
        Store the share counts implied by EPS.

        ESEF almost never tags a share count -- 3% in a 60-filing probe --
        but it tags EPS about 86% of the time, and net income to owners of
        the parent is one of the 20 fields. Dividing one by the other
        recovers the weighted-average counts the filing used.

        These are NOT Inputs!B15. They are a cross-check on it: if a ticker
        resolves to a listing whose share count disagrees with the number
        the accounts imply, the mapping is wrong, or it points at a
        different share class, or at the wrong entity entirely.
        """
        with self._tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO share_estimate
                   (lei, fiscal_year, net_income, basic_eps, diluted_eps,
                    wavg_basic, wavg_diluted, dilution_factor, source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (lei, fiscal_year, kw.get("net_income"), kw.get("basic_eps"),
                 kw.get("diluted_eps"), kw.get("wavg_basic"),
                 kw.get("wavg_diluted"), kw.get("dilution_factor"),
                 kw.get("source", "esef-eps")),
            )

    def share_estimate(self, lei: str, fiscal_year: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM share_estimate WHERE lei = ? AND fiscal_year = ?",
            (lei, fiscal_year)).fetchone()
        return dict(row) if row else None

    # -- handoff -------------------------------------------------------

    def to_intake(self, lei: str, years: list[int]) -> dict:
        """
        Build the provider-neutral intake record for the workbook writer.

        `years` must be exactly five, oldest first: FY-4 .. FY0. Raises if
        any year is missing or any required field is unresolved, because
        writing a partial company-year into the workbook would trip D111 and
        waste a recalculation cycle.
        """
        if len(years) != 5:
            raise ValueError(f"need exactly 5 years, got {len(years)}")
        if years != sorted(years):
            raise ValueError("years must be ascending, FY-4 first")

        records = []
        for y in years:
            cy = self.get(lei, y)
            if cy is None:
                raise KeyError(f"{lei}: no cached data for FY{y}")
            if cy.missing_required:
                raise ValueError(
                    f"{lei} FY{y}: unresolved required fields "
                    f"{cy.missing_required}"
                )
            records.append(cy)

        latest = records[-1]
        return {
            "identification": {
                "lei": lei,
                "name": latest.name,
                "country": latest.country,
                "fiscal_year": str(latest.fiscal_year),
            },
            "statements": {
                key: [r.scaled_values().get(key) for r in records]
                for key in FIELD_KEYS
            },
            "provenance": {
                "currency": latest.currency,
                "scale": latest.scale,
                "years": years,
                "period_ends": [r.period_end for r in records],
                "sources": sorted({r.source for r in records}),
                "extractor_versions": sorted({r.extractor_version
                                              for r in records}),
                "assumed_zeros": {
                    str(r.fiscal_year): r.assumed_zeros
                    for r in records if r.assumed_zeros
                },
                "field_derivations": {
                    str(r.fiscal_year): {
                        k: fa.derivation for k, fa in r.facts.items()
                    } for r in records
                },
            },
        }
