#!/usr/bin/env python3
"""
Unattended between-screens price update, for GitHub Actions.

    python scripts/ci_update_prices.py

Refreshes prices only for the companies the last screen valued plus
anything still held, applies core/price_update.py's entry/exit rules
against the last screen's fair values, and rebuilds public/portfolio.html.
Needs the state the quarterly ci_screen.py run left behind (financials.db,
portfolio.db and valuations/*/run_*.json); the other public pages are
restored as-is from that run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

import config                                                      # noqa: E402
from ci_screen import marked_portfolio, step                       # noqa: E402


def main() -> int:
    config.utf8_stdout()

    import fetch_prices
    from cache import Cache
    from portfolio import Portfolio
    from price_update import apply_prices
    from track import iter_triggers, latest_run, leis_by_ticker

    run_path = latest_run(config.OUT_DIR)
    if run_path is None or not config.DB_PATH.exists():
        sys.exit("No screening run to update. Run the 'General screening' "
                 "workflow first.")
    run_doc = json.loads(run_path.read_text(encoding="utf-8"))
    print(f"fair values from {run_path.name}")

    with Portfolio(config.PORTFOLIO_PATH) as pf, Cache(config.DB_PATH) as db:
        by_ticker = leis_by_ticker(db, config.FISCAL_YEARS)
        leis = {lei for lei, _, _ in iter_triggers(run_doc, by_ticker.get)
                if lei}
        leis |= {row["lei"] for row in pf.open_positions()}

    step(f"prices ({len(leis)} companies)")
    argv = sys.argv
    try:
        sys.argv = ["fetch_prices.py", "--source", "yahoo",
                    "--db", str(config.DB_PATH), "--refresh",
                    "--leis", *sorted(leis)]
        code = fetch_prices.main()
    finally:
        sys.argv = argv
    if code:
        # Per-company failures (a delisted name, a holiday) are reported
        # and returned as 0; nonzero means the fetch itself broke.
        sys.exit(f"price fetch failed (exit {code}).")

    step("portfolio")
    with Portfolio(config.PORTFOLIO_PATH) as pf, Cache(config.DB_PATH) as db:
        changes = apply_prices(pf, run_doc, db, config.FISCAL_YEARS)
    for label in ("entered", "exited"):
        for ticker, upside in changes[label]:
            print(f"  {label:<8}{ticker:<12}upside {upside}")
    if not (changes["entered"] or changes["exited"]):
        print("  no entries or exits.")
    if changes["skipped_old_run"]:
        print("  entries skipped: the run file predates the recorded policy "
              "outcome. The next general screening fixes this.")

    step("public portfolio")
    import build_report
    marked = marked_portfolio(config.DB_PATH, config.OUT_DIR, sync_run=False)
    public_dir = ROOT / "public"
    public_dir.mkdir(exist_ok=True)
    (public_dir / "portfolio.html").write_text(
        build_report.render_portfolio(marked, public=True, nav_links=True),
        encoding="utf-8")
    print(f"wrote {public_dir / 'portfolio.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
