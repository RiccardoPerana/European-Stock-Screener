#!/usr/bin/env python3
"""
Unattended pipeline runner, for GitHub Actions
===============================================

Runs the same stages gui/app.py's "Run Everything" button does, plus the
two stages that button deliberately leaves out because a human normally
confirms them (industry classification, the credit-spread ladder) -- here
they run unattended instead, since nobody is watching a CI job.

    python scripts/ci_screen.py

Reuses gui/app.py's job_* functions directly rather than reimplementing
the pipeline, so CI and the desktop app can never drift apart on what
"run everything" actually does.

WHAT THIS DOES NOT DO
----------------------
It does not publish anything. It writes the normal (private, full-price)
report next to financials.db, exactly like a local run, and additionally
a redacted report_<date>.public.html with no Yahoo Finance-derived EUR
figures (see core/build_report.py's `public=True` mode) into public/ --
the only thing the workflow should ever copy to GitHub Pages. See
README, "Data sources and their terms": Yahoo Finance is not licensed
for programmatic redistribution, so the private report and the
Portfolio page (built almost entirely from Yahoo price history) must
never leave this job.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "gui"))

import config                                                      # noqa: E402
import credentials                                                  # noqa: E402


class CIRunner:
    """The minimum job_*() needs: progress(), say(), .cancelled.

    Mirrors gui/tasks.py's TaskRunner interface without any of the
    threading/polling machinery a live dashboard needs -- a CI log is
    already a append-only stream, so this just prints.
    """

    cancelled = False

    def progress(self, done: int, total: int, detail: str = "") -> None:
        if total:
            print(f"[{done:>4}/{total}] {detail}", flush=True)

    def say(self, line: str) -> None:
        print(line, flush=True)


def step(name: str) -> None:
    print(f"\n=== {name} ===", flush=True)


def run_credit_ladder() -> None:
    """
    Fetch Damodaran's two rating tables and write them if they changed.

    The desktop app shows the diff and waits for a click
    (App.tables_check / tables_apply in gui/app.py); nobody is here to
    click, so this applies automatically and just prints the diff to the
    CI log, where it is easy to skim after the fact if a rung moved
    somewhere unexpected.
    """
    import fetch_ratings

    current = (json.loads(config.TABLES_PATH.read_text(encoding="utf-8"))
               if config.TABLES_PATH.exists() else {})
    fetched = fetch_ratings.fetch()
    changes = fetch_ratings.diff(current, fetched)
    if not changes:
        print("credit-spread ladder: no change.")
        return
    print(f"credit-spread ladder: {len(changes)} rung(s) changed:")
    for c in changes:
        print(f"  {c}")
    result = fetch_ratings.apply(config.TABLES_PATH, fetched)
    print(f"applied. new vintage: {result['vintage']}")


def run_industry_classification() -> None:
    """
    Classify any still-unmapped company via Gemini.

    Same code path as gui/app.py's App.industry_classify_gemini(), just
    called directly instead of through the App/HTTP layer. Skips quietly
    if GEMINI_API_KEY is not set -- newly-covered companies then stay
    unmapped and simply drop out of eligible_universe() until someone
    sets the key, rather than failing the whole run.
    """
    from cache import Cache
    from industry_worksheet import (GeminiError, classify_via_gemini,
                                     ingest, unmapped, valid_industries)

    api_key = credentials.resolve("gemini", allow_prompt=False)
    if not api_key:
        print("industries: no GEMINI_API_KEY set, skipping "
              "(newly-covered companies will stay unmapped).")
        return

    params = json.loads(config.PARAMS_PATH.read_text(encoding="utf-8"))
    params.setdefault("company_industry", {})
    valid = valid_industries(params)
    if not valid:
        print("industries: no industry list yet, skipping.")
        return

    with Cache(config.DB_PATH) as db:
        rows = unmapped(db, params, config.FISCAL_YEARS)
        if not rows:
            print("industries: nothing unmapped.")
            return
        print(f"industries: classifying {len(rows)} compan"
              f"{'y' if len(rows) == 1 else 'ies'} via Gemini...")
        try:
            reply = classify_via_gemini(rows, valid, api_key)
        except GeminiError as e:
            print(f"industries: Gemini call failed, skipping: {e}")
            return
        summary = ingest(reply, db, params, config.PARAMS_PATH, valid,
                          config.FISCAL_YEARS)
    print(f"industries: {summary}")


def main() -> int:
    config.utf8_stdout()
    runner = CIRunner()

    import app  # gui/app.py -- reuses its job_*() functions verbatim

    engine = app.recalc_engine()
    if not engine["ok"]:
        sys.exit(engine["message"])

    step("credit-spread ladder")
    run_credit_ladder()

    step("coverage")
    app.job_script(runner, job="coverage", db=config.DB_PATH)

    step("extract")
    app.job_script(runner, job="extract", db=config.DB_PATH)

    step("identity")
    app.job_script(runner, job="identity", db=config.DB_PATH)

    step("prices")
    app.job_prices(runner, db=config.DB_PATH, source="yahoo")

    step("shares")
    app.job_shares(runner, db=config.DB_PATH)

    step("parameters")
    app.job_script(runner, job="parameters", db=config.DB_PATH)

    step("industries")
    run_industry_classification()

    step("screen")
    result = app.job_screen(runner, db=config.DB_PATH,
                             out_dir=config.OUT_DIR, engine="libreoffice")
    run_dir = Path(result["run_dir"])
    print(f"\nscreened {result['valued']} companies, "
          f"{result['undervalued']} undervalued, "
          f"{len(result['queue'])} in the research queue.")

    step("public report")
    import build_report
    data = build_report.load(run_dir)
    public_html = build_report.render(data, public=True)
    public_dir = ROOT / "public"
    public_dir.mkdir(exist_ok=True)
    (public_dir / "index.html").write_text(public_html, encoding="utf-8")
    print(f"wrote {public_dir / 'index.html'}  "
          f"(no price/fair-value figures -- safe to publish)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
