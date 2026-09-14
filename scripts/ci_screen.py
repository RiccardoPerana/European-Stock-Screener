#!/usr/bin/env python3
"""
Unattended pipeline runner, for GitHub Actions.

    python scripts/ci_screen.py

Reuses gui/app.py's job_*() functions directly. Also runs credit-ladder
and industry-classification unattended (both need a confirmation click
in the desktop app). Writes the normal private report next to
financials.db, then a second, redacted pass (build_report.py's
public=True) into public/ as index.html, portfolio.html and
methodology.html -- the only files ever copied to GitHub Pages.
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
    """Minimal stand-in for gui/tasks.py's TaskRunner: progress(), say(), .cancelled."""

    cancelled = False

    def progress(self, done: int, total: int, detail: str = "") -> None:
        if total:
            print(f"[{done:>4}/{total}] {detail}", flush=True)

    def say(self, line: str) -> None:
        print(line, flush=True)


def step(name: str) -> None:
    print(f"\n=== {name} ===", flush=True)


def run_credit_ladder() -> None:
    """Fetch and apply Damodaran's rating tables without confirmation."""
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
    """Classify unmapped companies via Gemini. Skips quietly with no key set."""
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

    import app

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
    public_html = build_report.render(data, public=True, nav_links=True)
    public_dir = ROOT / "public"
    public_dir.mkdir(exist_ok=True)
    (public_dir / "index.html").write_text(public_html, encoding="utf-8")
    print(f"wrote {public_dir / 'index.html'}")

    step("public portfolio")
    marked = marked_portfolio(config.DB_PATH, config.OUT_DIR)
    portfolio_html = build_report.render_portfolio(marked, public=True,
                                                    nav_links=True)
    (public_dir / "portfolio.html").write_text(portfolio_html,
                                                encoding="utf-8")
    print(f"wrote {public_dir / 'portfolio.html'}")

    step("methodology")
    import shutil
    (public_dir / "methodology.html").write_text(
        build_report.build_methodology_page(nav_links=True), encoding="utf-8")
    shutil.copy(config.TEMPLATE_PATH, public_dir / "valuation_template.xlsx")
    print(f"wrote {public_dir / 'methodology.html'} and "
          f"{public_dir / 'valuation_template.xlsx'}")

    return 0


def marked_portfolio(db_path: Path, out_dir: Path) -> dict:
    """Sync the portfolio to the latest screen and mark it to current prices."""
    from cache import Cache
    from portfolio import Portfolio, mark, sync
    from track import RunFromJson, latest_run

    with Portfolio(config.PORTFOLIO_PATH) as pf, Cache(db_path) as db:
        run_path = latest_run(out_dir)
        if run_path is not None:
            doc = json.loads(run_path.read_text(encoding="utf-8"))
            sync(pf, RunFromJson(doc, db, config.FISCAL_YEARS))
        prices, history = {}, {}
        for row in pf.all_positions():
            lei = row["lei"]
            q = db.latest_price(lei)
            if q:
                prices[lei] = q
            h = db.price_history(lei)
            if h:
                history[lei] = h
        return mark(pf, prices, history=history)


if __name__ == "__main__":
    raise SystemExit(main())
