#!/usr/bin/env python3
"""
Identity resolver  --  LEI to ticker, and shares from EPS
=========================================================

Two jobs, done together because each validates the other.

1. IDENTIFIERS.  ESEF knows an entity only by its LEI. The model needs a
   ticker (Inputs!B7), and a ticker is what any price feed wants for
   Inputs!B14. Chain:

       LEI  --GLEIF-->  ISIN(s)  --OpenFIGI-->  ticker, exchange, type

   Both are free. OpenFIGI returns a security type, which is what finally
   settles the bond-only issuer problem properly: Samsung Electronics and
   ASFINAG have EUR debt listed in the EU and no European equity line, so
   they drop out because no equity instrument exists, not because their
   name happened to match a pattern.

2. SHARE COUNTS FROM EPS.  The share probe found point-in-time counts in
   3% of filings and weighted averages in 0%. ESEF cannot supply
   Inputs!B15. But it tags basic EPS in 87% of filings and diluted in 85%,
   and net income to owners of the parent is one of the 20 fields we
   already hold. So:

       weighted-average diluted shares = net income / diluted EPS

   That is a weighted average, not the point-in-time count the brief asks
   for, so it is NOT B15. Its value is as a CROSS-CHECK: when a ticker
   resolves to a listing whose share count disagrees with what the accounts
   imply, the mapping is wrong -- wrong entity, wrong share class, or a
   depositary receipt with a different ratio. That is the highest-risk
   error in the whole identifier chain and it is otherwise invisible.

CAVEAT ON THE EPS DERIVATION
----------------------------
EPS is sometimes presented for continuing operations only, while row 26 is
total net income to the parent. Where a company has discontinued
operations, the implied count will be off. The dilution FACTOR (basic EPS /
diluted EPS) is unaffected, since both share the same numerator.

USAGE
-----
    python resolve_identity.py --limit 20        # try a few first
    python resolve_identity.py
    python resolve_identity.py --figi-key KEY    # 10x the rate limit
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

from cache import Cache
from esef_extract import USER_AGENT, FactIndex, fetch_json, load_filing_index

import config
import credentials

GLEIF_ISIN_URL = "https://api.gleif.org/api/v1/lei-records/{lei}/isins"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
OPENFIGI_SEARCH_URL = "https://api.openfigi.com/v3/search"

# Bloomberg exchange codes for the euro-area home markets. Inputs!G14 wants
# the price from the PRIMARY listing in statement currency, and OpenFIGI
# returns every line an instrument trades on worldwide -- Verbund came back
# with 177, including London, New York and a dozen currency variants. Only
# the home-market line is the primary one.
COUNTRY_EXCHANGE = {
    "AT": ("AV",), "BE": ("BB",), "CY": ("CY",), "DE": ("GY", "GR"),
    "EE": ("ET",), "ES": ("SM", "SQ"), "FI": ("FH",), "FR": ("FP",),
    "GR": ("GA",), "HR": ("CZ",), "IE": ("ID",), "IT": ("IM",),
    "LT": ("LH",), "LU": ("LX",), "LV": ("LR",), "MT": ("MV",),
    "NL": ("NA",), "PT": ("PL",), "SI": ("SV",), "SK": ("SK",),
}

# OpenFIGI appends the trading currency to secondary lines: AMG1L is the
# Vilnius primary, AMG1LEUR a currency variant of it. The bare ticker wins.
CURRENCY_SUFFIXES = ("EUR", "USD", "GBP", "GBX", "CHF", "SEK", "NOK", "DKK",
                     "PLN", "CZK", "HUF", "BGN", "RON", "JPY")

# OpenFIGI's unauthenticated limits: 25 requests/minute, 10 jobs per request.
# With a key: 25 requests per 6 seconds, 100 jobs per request. Both are
# generous for 170 companies, but the unauthenticated path needs pacing or
# it starts returning 429s halfway through a run.
FIGI_JOBS_ANON, FIGI_JOBS_KEYED = 10, 100
FIGI_PAUSE_ANON, FIGI_PAUSE_KEYED = 2.6, 0.3
# /v3/search is limited separately from /v3/mapping and more tightly. The
# unauthenticated run hit 429 on five of twenty entities and lost them.
SEARCH_PAUSE_ANON, SEARCH_PAUSE_KEYED = 12.0, 3.2

BASIC_EPS = ("BasicEarningsLossPerShare",)
DILUTED_EPS = ("DilutedEarningsLossPerShare",)

# An equity line, as opposed to a bond, warrant or fund unit. OpenFIGI's
# marketSector is the coarse filter; securityType2 the precise one.
EQUITY_SECTORS = {"Equity"}
EQUITY_TYPES = {"Common Stock", "Depositary Receipt", "REIT", "Preference"}


def load_overrides(path: Path) -> dict:
    """Read the hand-maintained primary-listing corrections."""
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"{path} is not valid JSON: {e}")
    return doc.get("overrides", {})


def write_review_stub(path: Path, entries: list[dict]) -> int:
    """
    Append the flagged entities to the overrides file, pre-filled.

    The resolver's own pick goes in, so a correct one needs no edit and a
    wrong one needs a ticker changed. Existing entries are never touched --
    a decision already made must survive re-runs, which is the whole point
    of the file.
    """
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "overrides": {}}
    overrides = doc.setdefault("overrides", {})
    added = 0
    for e in entries:
        if e["lei"] in overrides:
            continue
        overrides[e["lei"]] = {
            "ticker": e["ticker"],
            "exchange": e["exchange"],
            "note": f"AUTO-FILLED for review: {e['name']}. Resolver picked "
                    f"{e['ticker']}.{e['exchange']}, which is outside the "
                    f"{e['country']} domicile. Confirm or correct.",
            "reviewed": None,
        }
        added += 1
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return added


def http_json(url: str, payload=None, headers=None, timeout=45):
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if payload is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# GLEIF: LEI -> ISIN
# ---------------------------------------------------------------------------

def gleif_isins(lei: str, cache_dir: Path, refresh: bool) -> list[str]:
    """All ISINs GLEIF associates with this LEI. Free, no registration."""
    path = cache_dir / f"gleif_{lei}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())

    isins: list[str] = []
    url = GLEIF_ISIN_URL.format(lei=lei) + "?page[size]=200"
    try:
        while url:
            doc = http_json(url)
            for rec in doc.get("data", []):
                isin = (rec.get("attributes") or {}).get("isin")
                if isin:
                    isins.append(isin)
            url = (doc.get("links") or {}).get("next")
    except urllib.error.HTTPError as e:
        if e.code != 404:      # 404 simply means no ISINs recorded
            raise
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return []

    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(isins))
    return isins


# ---------------------------------------------------------------------------
# OpenFIGI: ISIN -> ticker
# ---------------------------------------------------------------------------

def figi_map(isins: list[str], api_key: str | None,
             cache_dir: Path, refresh: bool) -> dict[str, list[dict]]:
    """Map ISINs to instruments, batched and rate-limited."""
    out: dict[str, list[dict]] = {}
    todo: list[str] = []
    for isin in isins:
        path = cache_dir / f"figi_{isin}.json"
        if path.exists() and not refresh:
            out[isin] = json.loads(path.read_text())
        else:
            todo.append(isin)

    batch_size = FIGI_JOBS_KEYED if api_key else FIGI_JOBS_ANON
    pause = FIGI_PAUSE_KEYED if api_key else FIGI_PAUSE_ANON
    headers = {"X-OPENFIGI-APIKEY": api_key} if api_key else {}

    cache_dir.mkdir(parents=True, exist_ok=True)
    for i in range(0, len(todo), batch_size):
        chunk = todo[i:i + batch_size]
        jobs = [{"idType": "ID_ISIN", "idValue": s} for s in chunk]
        try:
            results = http_json(OPENFIGI_URL, payload=jobs, headers=headers)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Back off once and retry the same chunk rather than losing it.
                time.sleep(30)
                try:
                    results = http_json(OPENFIGI_URL, payload=jobs,
                                        headers=headers)
                except Exception:
                    results = [{} for _ in chunk]
            else:
                results = [{} for _ in chunk]
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            results = [{} for _ in chunk]

        for isin, res in zip(chunk, results):
            rows = res.get("data", []) if isinstance(res, dict) else []
            out[isin] = rows
            (cache_dir / f"figi_{isin}.json").write_text(json.dumps(rows))
        time.sleep(pause)

    return out


EURO_AREA_EXCHANGES = {code for codes in COUNTRY_EXCHANGE.values()
                       for code in codes}

# Frankfurt and Xetra carry a secondary line for practically every European
# large cap. Kone, Campari and STMicroelectronics all resolved to .GR on the
# first pass -- a Finnish, an Italian and a French-Italian company, each
# about to be priced off a thin German quote that nothing downstream would
# question. So these venues count as a primary listing ONLY for German
# issuers; for anyone else their presence means a cross-listing was found
# and the home line was not.
CROSS_LISTING_VENUES = {"GY", "GR"}


def is_cross_listed_ticker(ticker: str) -> bool:
    """
    Borsa Italiana prefixes foreign shares with a digit: Kone's Helsinki
    line is KNEBV, its Milan cross-listing 1KNEBV. Several other venues do
    the same. A leading digit is therefore strong evidence of a secondary.
    """
    return bool(ticker) and ticker[0].isdigit()


def pick_primary(rows: list[dict], country: str | None) -> dict | None:
    """
    Choose the home-market line from every listing OpenFIGI returned.

    Getting this wrong is not cosmetic. A Finnish company priced off its
    Frankfurt secondary is being valued against a thinner, differently-timed
    quote, and nothing downstream would notice -- D114's market-cap sanity
    band is far too wide to catch a same-currency cross-listing.
    """
    # Country of ESEF filing is NOT country of primary listing. Campari,
    # STMicroelectronics, Ferrari, Brembo and Ariston are all Dutch-
    # incorporated and trade in Milan or Paris; filing country says NL and
    # the Amsterdam line does not exist. So any euro-area home market
    # qualifies, with the domicile merely PREFERRED.
    domicile = set(COUNTRY_EXCHANGE.get((country or "").upper(), ()))
    allowed = EURO_AREA_EXCHANGES - (CROSS_LISTING_VENUES - domicile)
    home = [r for r in rows if r.get("exchange") in allowed]
    if not home:
        # Returning None here is deliberate. The caller falls through to a
        # name search, which is far more likely to find the real home line
        # than accepting a German secondary would be to be correct.
        return None

    def rank(r):
        ticker = r.get("ticker") or ""
        # Prefer the bare ticker over its currency-suffixed twin, then
        # ordinary stock over a depositary receipt, then the shorter symbol.
        suffixed = any(ticker.endswith(c) for c in CURRENCY_SUFFIXES)
        common = "common" in str(r.get("security_type") or "").lower()
        return (r.get("exchange") not in domicile,
                is_cross_listed_ticker(ticker), suffixed, not common,
                len(ticker), ticker)

    best = sorted(home, key=rank)[0]
    # Record WHY it was chosen. A listing outside the domicile may be
    # perfectly correct -- Campari files in NL and trades in Milan -- or it
    # may be a secondary picked because the home line never came back. The
    # two are indistinguishable here, so they are separated for review
    # rather than merged into one confident answer.
    best["listing_tier"] = ("domicile" if best.get("exchange") in domicile
                            else "cross_listing_risk")
    return best


def figi_search(name: str, country: str | None, api_key: str | None,
                cache_dir: Path, refresh: bool) -> list[dict]:
    """
    Fallback when GLEIF holds no ISIN for an entity.

    GLEIF's ISIN mapping comes from national numbering agencies that joined
    the programme, and several have not -- Finnair and Anora are real listed
    companies that come back empty. Searching OpenFIGI by name recovers
    them, but name matching is fuzzy, so results are stored with
    confidence='name' and must be validated against the share count the
    accounts imply before anything is priced off them.
    """
    key = "".join(ch if ch.isalnum() else "_" for ch in name)[:60]
    path = cache_dir / f"search_{country}_{key}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())

    # One unfiltered search, then filter locally. Sending exchCode meant one
    # request per candidate exchange and, more importantly, hid whether an
    # empty result was a genuine miss or a rejected filter.
    headers = {"X-OPENFIGI-APIKEY": api_key} if api_key else {}
    payload = {"query": name[:80], "marketSecDes": "Equity"}
    pause = SEARCH_PAUSE_KEYED if api_key else SEARCH_PAUSE_ANON
    rows: list[dict] = []

    for attempt in (1, 2):
        try:
            doc = http_json(OPENFIGI_SEARCH_URL, payload=payload,
                            headers=headers)
            rows = doc.get("data", []) or []
            # Follow up to two more pages. "KONE OYJ" matches enough
            # instruments to push the Helsinki line off page one, and the
            # home listing is the entire point of the search.
            for _ in range(2):
                token = doc.get("next")
                if not token:
                    break
                time.sleep(pause)
                doc = http_json(OPENFIGI_SEARCH_URL,
                                payload={**payload, "start": token},
                                headers=headers)
                rows.extend(doc.get("data", []) or [])
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 1:
                # Back off and retry once. A rate limit should cost seconds,
                # not an entity -- these were silently lost before.
                time.sleep(pause * 5)
                continue
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:160]
            except Exception:
                pass
            print(f"\n      search HTTP {e.code}: {body}", end="")
            break
        except Exception as e:
            print(f"\n      search {type(e).__name__}: {e}", end="")
            break

    time.sleep(pause)

    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))
    return rows


def equity_listings(isin: str, rows: list[dict]) -> list[dict]:
    """Keep only equity instruments, normalised for the cache."""
    keep = []
    for r in rows:
        sector = r.get("marketSector")
        stype = r.get("securityType2") or r.get("securityType")
        if sector not in EQUITY_SECTORS:
            continue
        if stype and not any(t.lower() in str(stype).lower()
                             for t in EQUITY_TYPES):
            continue
        keep.append({
            "isin": isin,
            "ticker": r.get("ticker"),
            "exchange": r.get("exchCode"),
            "figi": r.get("figi"),
            "security_type": stype,
            "market_sector": sector,
            "name": r.get("name"),
        })
    return keep


# ---------------------------------------------------------------------------
# Shares implied by EPS
# ---------------------------------------------------------------------------

def eps_shares(index: FactIndex, period_end: date,
               net_income: float | None) -> dict:
    """Recover the weighted-average share counts the filing used."""
    def annual(names):
        for n in names:
            hit = index.annual(n, period_end)
            if hit is not None:
                return hit
        return None

    basic = annual(BASIC_EPS)
    diluted = annual(DILUTED_EPS)
    out = {
        "net_income": net_income,
        "basic_eps": basic.value if basic else None,
        "diluted_eps": diluted.value if diluted else None,
        "wavg_basic": None, "wavg_diluted": None, "dilution_factor": None,
    }
    if net_income and basic and basic.value:
        out["wavg_basic"] = net_income / basic.value
    if net_income and diluted and diluted.value:
        out["wavg_diluted"] = net_income / diluted.value
    elif net_income and basic and basic.value:
        # No diluted EPS tagged. Kesko, Inditex, Fortum, Hera and TF1 all
        # report basic only. Measured dilution was 1.0000 for 18 of 19
        # companies, so basic is a close stand-in -- and a close stand-in
        # beats no cross-check at all.
        out["wavg_diluted"] = net_income / basic.value
    # The factor survives even where the numerators disagree, because both
    # EPS figures share whatever numerator the company used.
    if basic and diluted and diluted.value:
        out["dilution_factor"] = basic.value / diluted.value
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Resolve LEIs to tickers.",
        parents=[config.common_args(cache_dir=True)])
    p.add_argument("--id-cache", type=Path, default=config.ROOT / ".id_cache")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--figi-key", default=None,
                   help="OpenFIGI API key. Prefer the OS credential store "
                        "(set via the GUI, or `python credentials.py --set "
                        "openfigi`) or the OPENFIGI_API_KEY environment "
                        "variable: a key passed on the command line lands "
                        "in your shell history.")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--overrides", type=Path,
                   default=config.ROOT / "listing_overrides.json",
                   help="Hand-maintained primary-listing corrections")
    p.add_argument("--write-review-stub", action="store_true",
                   help="Append flagged entities to the overrides file, "
                        "pre-filled with the resolver's pick")
    p.add_argument("--no-name-search", action="store_true",
                   help="Skip the OpenFIGI name fallback for entities with "
                        "no ISIN in GLEIF")
    p.add_argument("--no-shares", action="store_true",
                   help="Skip the EPS cross-check (no filing downloads)")
    args = p.parse_args()
    # allow_prompt=False: this runs unattended from the GUI (in-process, on
    # a background thread) as often as from a terminal, and a getpass()
    # nobody is watching there is a hang, not a prompt. credentials.resolve
    # still checks the explicit arg and OPENFIGI_API_KEY first, then falls
    # through to whatever was saved via the GUI's Settings page.
    args.figi_key = credentials.resolve("openfigi", explicit=args.figi_key,
                                        allow_prompt=False)

    filings = load_filing_index(args.cache_dir)
    overrides = load_overrides(args.overrides)
    if overrides:
        print(f"{len(overrides)} manual listing override(s) loaded\n")
    fy0 = max(args.years)

    with Cache(args.db) as db:
        universe = db.complete_entities(args.years)
        if not universe:
            sys.exit("No complete entities. Run esef_extract.py first.")
        if args.limit:
            universe = universe[: args.limit]

        print(f"Resolving {len(universe)} entities"
              f"{'' if args.figi_key else '  (no OpenFIGI key: ~10/min)'}\n")

        counts = Counter()
        no_equity: list[str] = []
        needs_review: list[dict] = []

        for i, lei in enumerate(universe, 1):
            cy = db.get(lei, fy0)
            name = (cy.name if cy else lei) or lei
            print(f"[{i:>4}/{len(universe)}] {name[:40]:<42}", end="", flush=True)

            country = cy.country if cy else None
            rows: list[dict] = []
            confidence = "isin"

            # --- 0. Manual override ---------------------------------------
            # A decision recorded by hand outranks anything the APIs say.
            # An override counts only once a human has signed it off.
            #
            # --write-review-stub pre-fills each flagged entity with the
            # resolver's own pick. Applying those immediately meant the six
            # cross_listing_risk warnings became six clean `manual` results
            # on the next run -- four of them still pointing at Vienna. The
            # stub silently blessed the answer it was written to question.
            # A warning that switches itself off is worse than no warning.
            ov = overrides.get(lei)
            reviewed = bool(ov and ov.get("reviewed"))

            if ov and ov.get("exclude"):
                # Exclusions are only ever written by hand, never auto-filled.
                counts["excluded_manually"] += 1
                print(f"excluded by override: {ov.get('note', '')[:44]}")
                continue
            if ov and ov.get("ticker") and not reviewed:
                counts["override_pending"] += 1
            if ov and ov.get("ticker") and reviewed:
                row = {"isin": f"MANUAL:{lei}", "ticker": ov["ticker"],
                       "exchange": ov.get("exchange"),
                       "figi": f"MANUAL:{lei}", "security_type": "Common Stock",
                       "market_sector": "Equity", "name": name,
                       "is_primary": True, "confidence": "manual",
                       "listing_tier": "manual"}
                db.put_listings(lei, [row])
                counts["has_primary"] += 1
                counts["manual"] += 1
                print(f"{ov['ticker']}.{ov.get('exchange')}  [OVERRIDE]")
                continue

            # --- 1. ISIN route --------------------------------------------
            isins = gleif_isins(lei, args.id_cache, args.refresh)
            if isins:
                counts["has_isin"] += 1
                mapped = figi_map(isins, args.figi_key, args.id_cache,
                                  args.refresh)
                for isin, res in mapped.items():
                    rows.extend(equity_listings(isin, res))
            else:
                counts["no_isin"] += 1

            primary = pick_primary(rows, country)

            # --- 2. Name fallback -----------------------------------------
            # Triggered whenever there is no usable home-market line, not
            # only when GLEIF held no ISIN at all. Kone and Valmet DO have
            # ISINs in GLEIF -- they are the US depositary receipts, so the
            # ISIN route returns 23 foreign listings and no Helsinki line.
            if primary is None and not args.no_name_search:
                found = figi_search(name, country, args.figi_key,
                                    args.id_cache, args.refresh)
                named = [r for r in equity_listings("", found) if r["ticker"]]
                for r in named:
                    r["isin"] = r["isin"] or f"NAME:{r['figi']}"
                candidate = pick_primary(named, country)
                if candidate is not None:
                    rows = named
                    primary = candidate
                    confidence = "name"
                    counts["by_name"] += 1

            if primary is not None:
                primary["is_primary"] = True
            for r in rows:
                r["confidence"] = confidence
            if primary is not None and primary.get("listing_tier") != "domicile":
                primary["confidence"] = f"{confidence}+non_domicile"

            if rows:
                db.put_listings(lei, rows)
                counts["has_equity"] += 1

            if primary is not None:
                counts["has_primary"] += 1
                tier = primary.get("listing_tier", "domicile")
                counts[tier] += 1
                pending = bool(ov and ov.get("ticker") and not reviewed)
                if tier != "domicile" or pending:
                    needs_review.append({
                        "lei": lei, "name": name, "country": country,
                        "ticker": primary["ticker"],
                        "exchange": primary["exchange"],
                    })
                flag = "  (UNVERIFIED name match)" if confidence == "name" else ""
                flag += "  [NON-DOMICILE]" if tier != "domicile" else ""
                flag += "  [OVERRIDE UNREVIEWED]" if pending else ""
                summary = (f"{primary['ticker']}.{primary['exchange']}"
                           f"  ({len(rows)} listings){flag}")
            elif rows:
                counts["no_home_listing"] += 1
                summary = f"{len(rows)} listings, NO euro-area line"
            elif isins:
                counts["debt_only"] += 1
                no_equity.append(name)
                summary = f"{len(isins)} ISIN(s), NO equity line"
            else:
                counts["unresolved"] += 1
                summary = "no ISIN, name search found nothing"

            # --- 3. EPS cross-check, always ------------------------------
            # Runs even for unresolved entities: the implied share count is
            # what will validate a name match once a price feed gives us the
            # listing's own share count.
            if not args.no_shares and cy is not None:
                rec = filings.get((lei, fy0))
                if rec:
                    try:
                        doc = fetch_json(rec["json_url"])
                        idx = FactIndex(doc)
                        period = idx.infer_period_end(
                            hint=date.fromisoformat(rec["reporting_date"]))
                        ni = cy.facts["net_income"].value
                        if period and ni:
                            est = eps_shares(idx, period, ni)
                            db.put_share_estimate(lei, fy0, **est)
                            if est["wavg_diluted"]:
                                counts["shares_derived"] += 1
                                summary += (f"  | {est['wavg_diluted']/1e6:,.1f}m "
                                            f"shares (x{est['dilution_factor']:.4f})")
                    except Exception:
                        counts["shares_failed"] += 1

            print(summary)

        print("\n" + "=" * 66)
        print("IDENTITY RESOLUTION")
        print("=" * 66)
        for key in ("has_isin", "no_isin", "by_name", "has_equity",
                    "has_primary", "domicile", "cross_listing_risk",
                    "manual", "excluded_manually", "override_pending",
                    "no_home_listing", "debt_only", "unresolved",
                    "shares_derived", "shares_failed"):
            if counts[key]:
                print(f"  {key:<18}{counts[key]:>6,}")

        screenable = counts["has_primary"]
        print(f"\n  Screenable universe: {screenable:,} of {len(universe):,}")
        print("  (an entity with no equity line has no price and no share")
        print("   count, so B14 and B15 have nothing to hold)")

        if needs_review:
            print(f"\n  NEEDS A HUMAN DECISION ({len(needs_review)}):")
            for e in needs_review[:20]:
                print(f"    {e['name'][:40]:<42}"
                      f"{e['ticker']}.{e['exchange']}   ({e['country']})")
            if len(needs_review) > 20:
                print(f"    ... and {len(needs_review) - 20} more")
            print("    Some are correct (Dutch-incorporated Italian groups)")
            print("    and some are secondaries picked because the home line")
            print("    never came back. Inputs!G14 wants the primary.")
            if args.write_review_stub:
                added = write_review_stub(args.overrides, needs_review)
                print(f"\n    {added} new entr(y/ies) in {args.overrides}.")
            print(f"\n    In {args.overrides}: correct any wrong ticker, then")
            print("    set \"reviewed\" to today's date. An entry with")
            print("    \"reviewed\": null is IGNORED and stays flagged here,")
            print("    so an auto-filled guess cannot pass itself off as a")
            print("    decision.")
            if not args.write_review_stub:
                print("\n    Re-run with --write-review-stub to pre-fill")
                print(f"    {args.overrides} with these for editing.")

        if no_equity:
            print(f"\n  Debt-only issuers dropped ({len(no_equity)}):")
            for n in no_equity[:15]:
                print(f"    {n[:60]}")
            if len(no_equity) > 15:
                print(f"    ... and {len(no_equity) - 15} more")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
